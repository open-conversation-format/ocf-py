"""Claude Code (CLI + IDE Extension + Desktop App) -> OCF exporter.

Storage layout
--------------
Claude Code's storage is fragmented across three locations on disk:

1. **Content store** (CLI + IDE Extension + Desktop App's primary writer):
   ``~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl``

2. **Desktop App metadata index** (Desktop App only):
   ``<APPDATA>/Claude/claude-code-sessions/<accountId>/<orgId>/local_<sessionId>.json``
   carrying ``{title, cwd, model, cliSessionId, completedTurns, archived, ...}``.
   The ``cliSessionId`` field maps back to the jsonl file under (1).

3. **Agent-mode sessions** (Desktop App's "Background Agents"):
   ``<APPDATA>/Claude/local-agent-mode-sessions/<acc>/<org>/<agent>/.claude/projects/<encoded>/*.jsonl``
   - same JSONL format as (1), but inside a sandboxed agent worktree.

This adapter scans (1) and (3) for content; uses (2) for title enrichment.

Event format
------------
Each line in a session jsonl is one event with a top-level ``type``:

- ``system`` - init / cwd / model metadata.
- ``user`` - user message; carries ``parentUuid`` (native threading!),
  ``promptId``, and a ``message`` payload with Anthropic-style content.
- ``assistant`` - assistant message; ``message.content`` may include
  ``text``, ``thinking`` (with ``signature``), ``tool_use`` blocks.
- ``tool_result`` - tool result; references a prior ``tool_use.id``.
- ``attachment`` - separate event when the user attaches files.
- ``ai-title`` - assistant-generated session title (rare).
- ``queue-operation``, ``last-prompt`` - runtime telemetry, dropped.

OCF mapping highlights
----------------------
- ``parentUuid`` -> OCF ``parent_id`` (native branching, no synthesis).
- Anthropic ``thinking`` block -> OCF ``thinking`` block (signature
  preserved in ``meta.claude_code_render``).
- Anthropic ``image`` content with base64 -> OCF ``image_url`` block
  with ``data:`` URI.
- Anthropic ``tool_use`` -> OCF ``tool_calls[]`` sibling on the
  assistant message envelope.
- ``tool_result`` event -> OCF ``role: "tool"`` envelope with
  ``tool_call_id`` from the source's ``tool_use_id``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

# Source-filename stems known to collide across multiple parent
# directories. The only one observed in production (1241 sessions
# across CLI projects/ + Desktop local-agent-mode-sessions/) is
# ``audit.jsonl`` (12 occurrences in Desktop sub-agent UUID dirs). UUID
# stems and ``agent-<hash>`` stems are globally unique and pass through
# unchanged. Add to this set only when a new generic stem is observed.
_COLLIDING_STEMS: frozenset[str] = frozenset({"audit"})


# Heartbeat / health-check templates that some editor plugins fire to
# probe the model. They land in ``~/.claude/projects/`` as one-shot
# sessions with one user message and one assistant reply, and they
# carry no archival value. Detection is intentionally narrow: a
# single-user-message session whose first prompt matches one of these
# substrings (case-insensitive). Not a regex — exact phrases keep the
# rule explainable. Add new patterns as they're observed.
_HEARTBEAT_PATTERNS: tuple[str, ...] = (
    "antworte exakt mit pong",
    "antworte exakt mit 'pong'",
    "antworte mit pong",
    'antworte mit json: {"ok"',
    'antworte mit einem json-objekt: {"status"',
    'gib zurück: {"status": "ok"',
    'gib zurueck: {"status": "ok"',
)

from ocf import __version__
from ocf.core.schema import validate_strict
from ocf.exporters._base import (
    AmbiguousMatchError,
    SessionInfo,
    SkipExport,
    SourceAdapter,
    export_all as _export_all_generic,
)
from ocf.exporters._common import ExportResult
from ocf.utils.jsonl import iter_jsonl_tailsafe
from ocf.utils.paths import (
    claude_code_desktop_agent_mode_sessions_dir,
    claude_code_desktop_sessions_metadata_dir,
    claude_code_projects_dir,
)

EVENT_SYSTEM = "system"
EVENT_USER = "user"
EVENT_ASSISTANT = "assistant"
EVENT_TOOL_RESULT = "tool_result"
EVENT_TOOL_USE = "tool_use"
EVENT_ATTACHMENT = "attachment"
EVENT_AI_TITLE = "ai-title"
EVENT_QUEUE_OPERATION = "queue-operation"
EVENT_LAST_PROMPT = "last-prompt"

# Content-block types inside Anthropic-style messages
CB_TEXT = "text"
CB_THINKING = "thinking"
CB_IMAGE = "image"
CB_TOOL_USE = "tool_use"
CB_TOOL_RESULT = "tool_result"


def metadata_index_path(metadata_dir: Path | None = None) -> Path:
    """Default metadata-index root (``~/AppData/.../claude-code-sessions/``)."""
    if metadata_dir is not None:
        return Path(metadata_dir)
    return claude_code_desktop_sessions_metadata_dir()


def load_metadata_index(
    metadata_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Load all Desktop-App metadata files, keyed by ``cliSessionId``.

    Each file at ``<root>/<acc>/<org>/local_<sid>.json`` is one
    session's metadata. We ignore failures and return what we can.

    Note: avoids ``Path.exists()`` guard because some Windows terminal
    emulators (cmder/ConEmu) return ``False`` for directories that
    actually exist — the underlying ``GetFileAttributesW`` is hooked
    differently than ``FindFirstFileW`` used by ``rglob``.
    """
    root = metadata_index_path(metadata_dir)
    index: dict[str, dict[str, Any]] = {}
    try:
        paths = list(root.rglob("local_*.json"))
    except OSError:
        return {}
    for path in paths:
        try:
            with path.open(encoding="utf-8") as fh:
                row = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(row, dict):
            continue
        sid = row.get("cliSessionId")
        if isinstance(sid, str):
            index[sid] = row
    return index


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ClaudeCodeAdapter(SourceAdapter):
    """SourceAdapter for Claude Code (CLI + IDE Extension + Desktop App)."""

    name: ClassVar[str] = "claude_code"
    mapping_id: ClassVar[str] = "claude-code-jsonl-v1"
    rollout_glob: ClassVar[str] = "*.jsonl"

    def __init__(
        self,
        *,
        metadata_dir_override: Path | None = None,
    ) -> None:
        self._metadata_dir_override = metadata_dir_override
        self._index_cache: dict[str, dict[str, dict[str, Any]]] = {}

    # ----- SourceAdapter interface ----------------------------------------

    def default_source_dirs(self) -> list[Path]:
        """Both the primary projects dir AND the agent-mode sandbox root.

        The agent-mode dir often does not exist when the Desktop App's
        background agents have not run; we still return it so that
        once it appears, scans pick it up automatically.
        """
        return [
            claude_code_projects_dir(),
            claude_code_desktop_agent_mode_sessions_dir(),
        ]

    def find_by_name(
        self,
        query: str,
        *,
        source_dirs: list[Path] | Path | None = None,
        case_sensitive: bool = False,
    ) -> list[Path]:
        if not query.strip():
            return []
        index = self._resolve_metadata_index()
        # Use the SAME source_dirs for relative-path corpus and discovery,
        # otherwise tests with custom roots get empty path-component corpus.
        effective_dirs = self._normalize_source_dirs(source_dirs)
        matches: list[Path] = []
        for path in self.discover(source_dirs):
            corpus = self._cc_search_corpus(path, index, effective_dirs)
            if self._match(corpus, query, case_sensitive=case_sensitive):
                matches.append(path)
        return matches

    def find_by_id(
        self,
        session_id: str,
        *,
        source_dirs: list[Path] | Path | None = None,
    ) -> list[Path]:
        """Match by cliSessionId (UUID). Default impl already handles
        filename matching; we keep that since Claude Code embeds the
        session UUID directly as the filename stem."""
        return super().find_by_id(session_id, source_dirs=source_dirs)

    def export_one(
        self, source: Path, *, validate: bool = True
    ) -> dict[str, Any]:
        return _convert_claude_code_session(
            source,
            metadata_index=self._resolve_metadata_index(),
            validate=validate,
        )

    def ocf_filename_for(self, source: Path) -> str:
        """Disambiguate filename collisions across the two source roots.

        The CLI's ``~/.claude/projects/`` uses ``<uuid>.jsonl`` (already
        unique). The Desktop App's ``local-agent-mode-sessions/`` tree
        also has ``agent-<hash>.jsonl`` files (also unique). The ONLY
        observed cross-directory collision is the literal ``audit.jsonl``
        used by Desktop sub-agent dirs — 12 of them on this user's
        machine, all colliding into one ``audit.ocf.json`` and losing
        11 sessions before this fix landed.

        Strategy: pass everything through unchanged unless the stem is
        on the explicit collision list (:data:`_COLLIDING_STEMS`); in
        that case append a 12-hex-char hash of the parent path so each
        source file gets a unique destination.
        """
        stem = source.stem
        if stem in _COLLIDING_STEMS:
            parent_hash = hashlib.sha256(
                str(source.parent).encode("utf-8")
            ).hexdigest()[:12]
            return f"{stem}-{parent_hash}.ocf.json"
        return f"{stem}.ocf.json"

    # ----- Session info (for ``ocf list``) ----------------------------------

    def session_info(self, source: Path) -> SessionInfo:
        """Cheap metadata peek for one Claude Code session.

        Strategy:
        1. Desktop metadata index (in-memory, zero I/O) — gives title,
           cwd, model for App/Cowork sessions.
        2. JSONL peek (first ~20 events) — gives cwd, model, created_at
           from ``system`` event, and title from ``ai-title`` if it
           appears early. CLI sessions rely on this.
        """
        session_id = source.stem
        index = self._resolve_metadata_index()
        meta = index.get(session_id) or {}

        title = meta.get("title") if isinstance(meta.get("title"), str) else None
        cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else None
        model = meta.get("model") if isinstance(meta.get("model"), str) else None
        created_at: datetime | None = None

        # Peek the JSONL: first few events for system metadata (cwd,
        # model, created_at), then a reverse scan of the tail for
        # ai-title (which comes after the first assistant turn and
        # can be at event 50+ in tool-heavy sessions).
        try:
            for i, ev in enumerate(iter_jsonl_tailsafe(source)):
                if i > 10:
                    break
                ts = _parse_iso(ev.get("timestamp"))
                if created_at is None and ts is not None:
                    created_at = ts
                etype = ev.get("type")
                if etype == EVENT_SYSTEM:
                    if not cwd and isinstance(ev.get("cwd"), str):
                        cwd = ev["cwd"]
                    if not model and isinstance(ev.get("model"), str):
                        model = ev["model"]
                elif etype == EVENT_AI_TITLE:
                    if not title and isinstance(ev.get("title"), str):
                        title = ev["title"]
        except OSError:
            pass

        # Reverse-peek for ai-title if we didn't find one yet
        if not title:
            title = _peek_ai_title_tail(source)

        return SessionInfo(
            source=source,
            session_id=session_id,
            title=title,
            project=_project_name(cwd) if cwd else None,
            created_at=created_at,
            model=model,
        )

    # ----- Internal helpers -----------------------------------------------

    def _resolve_metadata_index(self) -> dict[str, dict[str, Any]]:
        path = self._metadata_dir_override
        key = str(path) if path is not None else "__default__"
        if key not in self._index_cache:
            self._index_cache[key] = load_metadata_index(path)
        return self._index_cache[key]

    def _cc_search_corpus(
        self,
        path: Path,
        index: dict[str, dict[str, Any]],
        source_dirs: list[Path] | None = None,
    ) -> str:
        parts: list[str] = [path.name, path.stem]  # session UUID is the stem
        # Folder names from caller-supplied source_dirs first, then
        # default_source_dirs as fallback.
        candidate_dirs = list(source_dirs or [])
        candidate_dirs.extend(self.default_source_dirs())
        seen: set[str] = set()
        for d in candidate_dirs:
            key = str(d)
            if key in seen:
                continue
            seen.add(key)
            try:
                rel = path.resolve().relative_to(d.resolve())
                parts.extend(rel.parent.parts)
                break
            except ValueError:
                continue
        # Title and cwd from Desktop App metadata index, keyed by stem (UUID)
        sid = path.stem
        row = index.get(sid)
        if row:
            for key in ("title", "cwd", "originCwd"):
                v = row.get(key)
                if isinstance(v, str) and v:
                    parts.append(v)
                    if "cwd" in key.lower():
                        parts.extend(_split_path(v))
        # Cheap peek of first event for cwd if metadata index missed
        first_cwd = _peek_cwd(path)
        if first_cwd:
            parts.append(first_cwd)
            parts.extend(_split_path(first_cwd))
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Variant adapters: split by storage origin
# ---------------------------------------------------------------------------

class ClaudeCodeCliAdapter(ClaudeCodeAdapter):
    """Sessions created by Claude Code CLI or IDE extensions.

    Scans ``~/.claude/projects/`` and filters OUT sessions whose UUID
    appears in the Desktop App metadata index (those are owned by
    :class:`ClaudeCodeAppAdapter` instead).

    The partition is clean: CLI session UUID never appears in the
    Desktop App's ``claude-code-sessions/`` metadata directory, and
    Desktop App sessions always have an entry there.
    """

    name: ClassVar[str] = "claude_code_cli"

    def default_source_dirs(self) -> list[Path]:
        return [claude_code_projects_dir()]

    def discover(
        self, source_dirs: list[Path] | Path | None = None
    ) -> list[Path]:
        all_files = super().discover(source_dirs)
        index = self._resolve_metadata_index()
        return [f for f in all_files if f.stem not in index]


class ClaudeCodeAppAdapter(ClaudeCodeAdapter):
    """Sessions created or managed by the Claude Desktop App.

    Scans ``~/.claude/projects/`` and keeps ONLY sessions whose UUID
    has a matching entry in the Desktop App metadata index
    (``claude-code-sessions/<acc>/<org>/local_<cliSessionId>.json``).
    """

    name: ClassVar[str] = "claude_code_app"

    def default_source_dirs(self) -> list[Path]:
        return [claude_code_projects_dir()]

    def discover(
        self, source_dirs: list[Path] | Path | None = None
    ) -> list[Path]:
        all_files = super().discover(source_dirs)
        index = self._resolve_metadata_index()
        return [f for f in all_files if f.stem in index]


class ClaudeCoworkAppAdapter(ClaudeCodeAdapter):
    """Background Agent ("Cowork") sessions from the Claude Desktop App.

    Scans ``<APPDATA>/Claude/local-agent-mode-sessions/`` only.
    Each spawned background agent gets its own sandboxed worktree
    with ``.claude/projects/<encoded>/*.jsonl`` inside.
    """

    name: ClassVar[str] = "claude_cowork_app"

    def default_source_dirs(self) -> list[Path]:
        return [claude_code_desktop_agent_mode_sessions_dir()]


# ---------------------------------------------------------------------------
# Module-level shims (mirrors the codex.py pattern)
# ---------------------------------------------------------------------------
# These expose the *combined* adapter for backward-compatible library use.
# The CLI dispatches to the variant adapters above via _AdapterShim.

DEFAULT_SOURCE_DIR_FN = claude_code_projects_dir
MAPPING_ID = ClaudeCodeAdapter.mapping_id
ROLLOUT_GLOB = ClaudeCodeAdapter.rollout_glob

_default_adapter: ClaudeCodeAdapter | None = None


def _adapter() -> ClaudeCodeAdapter:
    global _default_adapter
    if _default_adapter is None:
        _default_adapter = ClaudeCodeAdapter()
    return _default_adapter


def discover(source_dir: list[Path] | Path | None = None) -> list[Path]:
    return _adapter().discover(source_dir)


def find_by_name(
    query: str,
    *,
    source_dir: list[Path] | Path | None = None,
    case_sensitive: bool = False,
    metadata_dir: Path | None = None,
) -> list[Path]:
    adapter = (
        ClaudeCodeAdapter(metadata_dir_override=metadata_dir)
        if metadata_dir is not None
        else ClaudeCodeAdapter()  # fresh — avoid singleton cache crossing tests
    )
    return adapter.find_by_name(
        query, source_dirs=source_dir, case_sensitive=case_sensitive
    )


def find_by_id(
    session_id: str,
    *,
    source_dir: list[Path] | Path | None = None,
) -> list[Path]:
    return ClaudeCodeAdapter().find_by_id(session_id, source_dirs=source_dir)


def export_one(
    source: Path,
    *,
    validate: bool = True,
    metadata_index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if metadata_index is not None:
        return _convert_claude_code_session(
            source, metadata_index=metadata_index, validate=validate
        )
    return _adapter().export_one(source, validate=validate)


def export_all(
    out_dir: Path,
    *,
    sources: Iterable[Path] | None = None,
    source_dir: list[Path] | Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> ExportResult:
    """Bulk runner. Validates source_dir(s) when given explicitly."""
    if sources is None and source_dir is not None:
        dirs = source_dir if isinstance(source_dir, list) else [source_dir]
        for d in dirs:
            if not Path(d).exists():
                raise FileNotFoundError(
                    f"Claude Code source directory not found: {d}"
                )
    return _export_all_generic(
        _adapter(),
        out_dir,
        sources=sources,
        source_dirs=source_dir,
        force=force,
        dry_run=dry_run,
    )


def resolve_sources(
    source: Path | str | None,
    *,
    source_dir: list[Path] | Path | None = None,
    case_sensitive: bool = False,
) -> list[Path]:
    return ClaudeCodeAdapter().resolve_sources(
        source, source_dirs=source_dir, case_sensitive=case_sensitive
    )


# ---------------------------------------------------------------------------
# Conversion logic
# ---------------------------------------------------------------------------

def _is_heartbeat_session(events: list[dict[str, Any]]) -> str | None:
    """Detect editor-extension heartbeat / health-check sessions.

    Returns the matched pattern (for the SkipExport reason) or None.
    A heartbeat session is characterized by exactly one user message
    whose text starts with one of :data:`_HEARTBEAT_PATTERNS`. We
    keep the rule narrow on purpose — anything longer or with a real
    follow-up is treated as a real (if short) conversation.
    """
    user_text: str | None = None
    user_count = 0
    for ev in events:
        if ev.get("type") != EVENT_USER:
            continue
        msg = ev.get("message") or {}
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        text: str | None = None
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == CB_TEXT:
                    raw = block.get("text") or ""
                    text = raw.strip() if isinstance(raw, str) else None
                    break
        if text:
            if user_text is None:
                user_text = text
            user_count += 1
            if user_count > 1:
                return None  # real conversation
    if user_count != 1 or user_text is None:
        return None
    low = user_text.lower()
    for pat in _HEARTBEAT_PATTERNS:
        if pat in low:
            return pat
    return None


def _convert_claude_code_session(
    source: Path,
    *,
    metadata_index: dict[str, dict[str, Any]],
    validate: bool,
) -> dict[str, Any]:
    """Pure function: Claude Code session jsonl -> OCF document dict."""
    events = list(iter_jsonl_tailsafe(source))

    matched = _is_heartbeat_session(events)
    if matched is not None:
        raise SkipExport(f"heartbeat session ({matched!r})")

    session_id = source.stem  # UUID is the filename stem
    cwd: str | None = None
    default_model: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    title: str | None = None

    # Look up Desktop App metadata if available
    meta_row = metadata_index.get(session_id) or {}
    if isinstance(meta_row.get("title"), str):
        title = meta_row["title"]
    if isinstance(meta_row.get("cwd"), str):
        cwd = meta_row["cwd"]
    if isinstance(meta_row.get("model"), str):
        default_model = meta_row["model"]

    messages: list[dict[str, Any]] = []
    counter = 0

    def next_msg_id() -> str:
        nonlocal counter
        counter += 1
        return f"msg_{counter:04d}"

    for ev in events:
        ts = _parse_iso(ev.get("timestamp"))
        if started_at is None and ts is not None:
            started_at = ts
        if ts is not None:
            ended_at = ts

        etype = ev.get("type")
        if etype == EVENT_SYSTEM:
            ev_cwd = ev.get("cwd")
            if isinstance(ev_cwd, str) and ev_cwd and not cwd:
                cwd = ev_cwd
            ev_model = ev.get("model")
            if isinstance(ev_model, str) and ev_model and not default_model:
                default_model = ev_model
            continue  # system event is metadata, no OCF message

        if etype == EVENT_AI_TITLE:
            t = ev.get("title")
            if isinstance(t, str) and t:
                title = t
            continue

        if etype in (EVENT_QUEUE_OPERATION, EVENT_LAST_PROMPT, EVENT_ATTACHMENT):
            # Telemetry / not yet modeled. attachment is potentially
            # interesting future work (-> resources[]).
            continue

        if etype == EVENT_USER:
            envelopes = _envelopes_from_user(ev, next_msg_id, ts)
            messages.extend(envelopes)
            continue

        if etype == EVENT_ASSISTANT:
            envelopes = _envelopes_from_assistant(
                ev, next_msg_id, ts, default_model
            )
            messages.extend(envelopes)
            continue

        if etype == EVENT_TOOL_RESULT:
            messages.append(_envelope_from_tool_result(ev, next_msg_id, ts))
            continue

    conversation: dict[str, Any] = {
        "id": f"conv_claude_code_{session_id}",
        "title": title,
        "created_at": _format_iso(started_at) or _now_iso(),
        "default_model": default_model,
        "source": {
            "platform": "claude_code",
            "export_tool": "ocf-py",
            "original_id": source.name,
        },
        "produced_by": {
            "tool": "ocf-py",
            "version": __version__,
            "mapping_id": ClaudeCodeAdapter.mapping_id,
        },
        "meta": {
            "claude_code": {
                "session_id": session_id,
                "absolute_path_at_capture": cwd,
                "raw_format": "claude-code-session-jsonl",
            }
        },
    }
    if ended_at is not None:
        conversation["updated_at"] = _format_iso(ended_at)
    if cwd:
        conversation["project"] = {
            "id": _project_id(cwd),
            "name": _project_name(cwd),
            "platform_id": None,
            "description": cwd,
        }
    # Carry Desktop-App metadata-index fields that the spec doesn't model
    # explicitly. Future v0.2 may promote some of these (completedTurns,
    # archived) to first-class fields.
    for key in ("completedTurns", "isArchived", "permissionMode", "effort"):
        if key in meta_row:
            conversation["meta"]["claude_code"][key] = meta_row[key]

    doc: dict[str, Any] = {
        "ocf_version": "0.1.0",
        "conversation": conversation,
        "messages": messages,
    }

    if validate:
        validate_strict(doc)

    return doc


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------

def _envelopes_from_user(
    ev: dict[str, Any], next_id, ts: datetime | None
) -> list[dict[str, Any]]:
    """User events. Each becomes one OCF envelope with role=user.

    Content array can contain text + image blocks. Anthropic's
    ``tool_result`` blocks sometimes appear inside user content too
    (when tool results are inlined back into the next user turn);
    we split those into a separate role:tool message preceding the
    user content.
    """
    msg = ev.get("message") or {}
    content = msg.get("content")
    parent_uuid = ev.get("parentUuid")
    msg_id_source = ev.get("uuid") or msg.get("id")
    id_origin = "source" if msg_id_source else "synthesized"
    msg_id = msg_id_source or next_id()

    user_content: list[dict[str, Any]] = []
    extra_envelopes: list[dict[str, Any]] = []

    if isinstance(content, str):
        user_content.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == CB_TEXT:
                txt = block.get("text", "")
                if isinstance(txt, str):
                    user_content.append({"type": "text", "text": txt})
            elif bt == CB_IMAGE:
                src = block.get("source") or {}
                if (
                    isinstance(src, dict)
                    and src.get("type") == "base64"
                    and isinstance(src.get("data"), str)
                ):
                    media = src.get("media_type", "image/png")
                    url = f"data:{media};base64,{src['data']}"
                    user_content.append(
                        {"type": "image_url", "image_url": {"url": url}}
                    )
            elif bt == CB_TOOL_RESULT:
                # Inline tool_result inside user content - emit as its own
                # role:tool envelope ahead of (or instead of) the user msg.
                extra_envelopes.append(
                    _envelope_from_tool_result_block(block, next_id, ts)
                )
    elif content is not None:
        # Unknown content shape; degrade to string repr.
        user_content.append({"type": "text", "text": str(content)})

    inner_content: Any
    if not user_content:
        inner_content = ""
    elif len(user_content) == 1 and user_content[0]["type"] == "text":
        inner_content = user_content[0]["text"]
    else:
        inner_content = user_content

    user_envelope: dict[str, Any] = {
        "id": msg_id,
        "id_origin": id_origin,
        "message": {"role": "user", "content": inner_content},
        "meta": {
            "claude_code_render": {
                "raw_event_type": "user",
                "promptId": ev.get("promptId"),
                "isSidechain": ev.get("isSidechain"),
            }
        },
    }
    if ts is not None:
        user_envelope["created_at"] = _format_iso(ts)
    if isinstance(parent_uuid, str) and parent_uuid:
        user_envelope["parent_id"] = parent_uuid

    # Tool results emitted from inside user content come BEFORE the user
    # message in conversation order (they answer prior assistant calls).
    return [*extra_envelopes, user_envelope]


def _envelopes_from_assistant(
    ev: dict[str, Any],
    next_id,
    ts: datetime | None,
    default_model: str | None,
) -> list[dict[str, Any]]:
    msg = ev.get("message") or {}
    content = msg.get("content")
    parent_uuid = ev.get("parentUuid")
    model = msg.get("model") or default_model
    msg_id_source = ev.get("uuid") or msg.get("id")
    id_origin = "source" if msg_id_source else "synthesized"
    msg_id = msg_id_source or next_id()

    ocf_content: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    thinking_signatures: list[str] = []

    blocks = content if isinstance(content, list) else []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == CB_TEXT:
            txt = block.get("text", "")
            if isinstance(txt, str) and txt:
                ocf_content.append({"type": "text", "text": txt})
        elif bt == CB_THINKING:
            thinking_text = block.get("thinking", "")
            if isinstance(thinking_text, str):
                ocf_content.append(
                    {"type": "thinking", "thinking": thinking_text or "[redacted]"}
                )
            sig = block.get("signature")
            if isinstance(sig, str):
                thinking_signatures.append(sig)
        elif bt == CB_TOOL_USE:
            tool_id = block.get("id") or f"toolu_{next_id()}"
            input_arg = block.get("input")
            if isinstance(input_arg, str):
                args_str = input_arg
            elif input_arg is None:
                args_str = "{}"
            else:
                args_str = json.dumps(input_arg, separators=(",", ":"))
            tool_calls.append(
                {
                    "id": tool_id,
                    "id_origin": "source" if block.get("id") else "synthesized",
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": args_str,
                    },
                }
            )

    inner: dict[str, Any] = {"role": "assistant"}
    if not ocf_content:
        inner["content"] = None
    elif len(ocf_content) == 1 and ocf_content[0]["type"] == "text":
        inner["content"] = ocf_content[0]["text"]
    else:
        inner["content"] = ocf_content
    if tool_calls:
        inner["tool_calls"] = tool_calls

    envelope: dict[str, Any] = {
        "id": msg_id,
        "id_origin": id_origin,
        "message": inner,
        "meta": {
            "claude_code_render": {
                "raw_event_type": "assistant",
                "anthropic_message_id": msg.get("id"),
            }
        },
    }
    if thinking_signatures:
        envelope["meta"]["claude_code_render"]["thinking_signatures"] = thinking_signatures
    if ts is not None:
        envelope["created_at"] = _format_iso(ts)
    if isinstance(parent_uuid, str) and parent_uuid:
        envelope["parent_id"] = parent_uuid
    if model:
        envelope["model"] = model

    # Anthropic usage in message.usage if present
    usage = msg.get("usage") or {}
    if isinstance(usage, dict):
        u: dict[str, Any] = {}
        if isinstance(usage.get("input_tokens"), int):
            u["input"] = usage["input_tokens"]
        if isinstance(usage.get("output_tokens"), int):
            u["output"] = usage["output_tokens"]
        if u:
            envelope["usage"] = u

    return [envelope]


def _envelope_from_tool_result(
    ev: dict[str, Any], next_id, ts: datetime | None
) -> dict[str, Any]:
    """Top-level ``tool_result`` event."""
    tool_use_id = ev.get("tool_use_id") or ev.get("toolUseId") or "call_unknown"
    output = ev.get("content")
    if not isinstance(output, str):
        output = json.dumps(output, separators=(",", ":")) if output else ""

    envelope: dict[str, Any] = {
        "id": next_id(),
        "id_origin": "synthesized",
        "message": {
            "role": "tool",
            "tool_call_id": tool_use_id,
            "content": output,
        },
        "meta": {"claude_code_render": {"raw_event_type": "tool_result"}},
    }
    if ts is not None:
        envelope["created_at"] = _format_iso(ts)
    return envelope


def _envelope_from_tool_result_block(
    block: dict[str, Any], next_id, ts: datetime | None
) -> dict[str, Any]:
    """``tool_result`` block found INSIDE user content."""
    tool_use_id = block.get("tool_use_id") or block.get("toolUseId") or "call_unknown"
    inner = block.get("content")
    if isinstance(inner, list):
        # Concat any text parts
        parts = []
        for b in inner:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text")
                if isinstance(t, str):
                    parts.append(t)
        output = "\n".join(parts) if parts else json.dumps(inner, separators=(",", ":"))
    elif isinstance(inner, str):
        output = inner
    else:
        output = "" if inner is None else json.dumps(inner, separators=(",", ":"))

    envelope: dict[str, Any] = {
        "id": next_id(),
        "id_origin": "synthesized",
        "message": {
            "role": "tool",
            "tool_call_id": tool_use_id,
            "content": output,
        },
        "meta": {
            "claude_code_render": {
                "raw_event_type": "tool_result_block_in_user_content",
                "is_error": bool(block.get("is_error")),
            }
        },
    }
    if ts is not None:
        envelope["created_at"] = _format_iso(ts)
    return envelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _peek_ai_title_tail(path: Path, tail_bytes: int = 8192) -> str | None:
    """Read the last few KB of a JSONL to find an ``ai-title`` event.

    ai-title events appear after the first assistant response, which
    can be dozens of events deep in tool-heavy sessions. Reading the
    whole file is too expensive for 1000+ sessions; instead we read
    the tail and parse any complete JSON lines found there.
    """
    try:
        size = path.stat().st_size
        if size == 0:
            return None
        with path.open("rb") as fh:
            offset = max(0, size - tail_bytes)
            fh.seek(offset)
            chunk = fh.read()
        # Decode and split into lines; first line may be partial — skip it
        text = chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        if offset > 0:
            lines = lines[1:]  # first line is likely partial
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if ev.get("type") == EVENT_AI_TITLE:
                t = ev.get("title")
                if isinstance(t, str) and t:
                    return t
    except OSError:
        pass
    return None


def _peek_cwd(path: Path) -> str | None:
    """Read first few events to find the cwd field."""
    try:
        for i, ev in enumerate(iter_jsonl_tailsafe(path)):
            if i > 5:
                break
            cwd = ev.get("cwd")
            if isinstance(cwd, str) and cwd:
                return cwd
    except OSError:
        pass
    return None


def _split_path(p: str) -> list[str]:
    n = p.replace("\\", "/")
    return [seg for seg in n.split("/") if seg]


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 4_000_000_000:
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except ValueError:
            return None
    return None


def _format_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_short(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _project_id(cwd: str) -> str:
    return f"proj_{_hash_short(cwd, 12)}"


def _project_name(cwd: str) -> str:
    parts = _split_path(cwd)
    return parts[-1] if parts else cwd


__all__ = [
    "DEFAULT_SOURCE_DIR_FN",
    "MAPPING_ID",
    "ROLLOUT_GLOB",
    "AmbiguousMatchError",
    "ClaudeCodeAdapter",
    "ClaudeCodeCliAdapter",
    "ClaudeCodeAppAdapter",
    "ClaudeCoworkAppAdapter",
    "metadata_index_path",
    "load_metadata_index",
    "discover",
    "find_by_name",
    "find_by_id",
    "resolve_sources",
    "export_one",
    "export_all",
]
