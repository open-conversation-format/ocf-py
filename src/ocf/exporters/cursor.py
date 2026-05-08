"""Cursor IDE -> OCF exporter.

Storage layout
--------------
Cursor stores chat state in SQLite databases at:

- ``%APPDATA%/Cursor/User/globalStorage/state.vscdb`` (multi-GB; primary)
- ``%APPDATA%/Cursor/User/workspaceStorage/<hash>/state.vscdb`` (per workspace)

The relevant table is ``cursorDiskKV`` with key/value JSON pairs.
Conversation data lives under two key prefixes:

- ``composerData:<composer_id>`` — session metadata, JSON object with
  ``_v``, ``workspaceFolder``, ``title``, ``latestConversationSummary``,
  ``fullConversationHeadersOnly``, sometimes inline ``conversation[]``
- ``bubbleId:<composer_id>:<bubble_id>`` — individual messages with
  ``type`` (1=user, 2=assistant), ``text``, ``thinking``, ``codeBlocks``

Cursor's storage has evolved through several format generations
(v0.x ``aiService.*`` keys; v1.x inline ``composerData.conversation[]``;
v1.x-2.0+ separate ``bubbleId`` keys). This adapter targets the
**current cursorDiskKV format with separate ``bubbleId`` storage** —
the dominant pattern in installs from late 2024 onward. Older formats
are out of scope for v0.1 (documented in the spec roadmap).

Aggressive simplification: like ChatSyncer's adapter, we extract only
what maps cleanly to OCF — text, thinking, codeBlocks. We ignore:

- ``agentKv:*`` — background-agent state (often partially in Cursor cloud)
- ``checkpointId:*`` — snapshot markers
- ``codeBlockDiff:*``, ``codeBlockPartialInlineDiffFates:*``,
  ``messageRequestContext:*``, ``ofsContent:*``, ``patch-graph:*``,
  ``inlineDiff:*``, ``expectedContent-*`` — internal IDE state

Source tokens
-------------
A Cursor "source" is a ``(state.vscdb path, composer_id)`` pair, not
a regular file. This adapter encodes them as path tokens:

    ``<absolute db path>::composerData:<composer_id>``

The ``::`` separator works on Windows (drive letters use a single
``:``), Linux, and macOS. The exporter never opens a token as a file
directly; ``export_one`` parses the token and queries the DB.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

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
from ocf.utils.paths import cursor_user_dir
from ocf.utils.sqlite_ro import has_table, open_ro

COMPOSER_KEY_PREFIX = "composerData:"
BUBBLE_KEY_PREFIX = "bubbleId:"
TOKEN_SEP = "::composerData:"

# Bubble field names we extract (everything else is dropped per
# aggressive-simplification policy). The order here is the order
# of content blocks in the produced OCF message.
BUBBLE_TEXT_FIELDS = ("text", "rawText")
BUBBLE_THINKING_FIELD = "thinking"
BUBBLE_CODE_BLOCKS_FIELD = "codeBlocks"


def _make_source_token(db_path: Path, composer_id: str) -> Path:
    """Encode (db, composer_id) as a Path-like token."""
    return Path(f"{db_path.as_posix()}{TOKEN_SEP}{composer_id}")


def _split_source_token(source: Path | str) -> tuple[Path, str]:
    s = str(source)
    if TOKEN_SEP not in s:
        raise ValueError(
            f"Not a Cursor source token (missing {TOKEN_SEP!r}): {s}"
        )
    db_str, key = s.split(TOKEN_SEP, 1)
    return Path(db_str), key


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class CursorAdapter(SourceAdapter):
    """SourceAdapter for Cursor IDE sessions (current cursorDiskKV format)."""

    name: ClassVar[str] = "cursor"
    mapping_id: ClassVar[str] = "cursor-disk-kv-v3"
    rollout_glob: ClassVar[str] = "state.vscdb"

    def default_source_dirs(self) -> list[Path]:
        user = cursor_user_dir()
        return [user / "globalStorage", user / "workspaceStorage"]

    def discover(
        self, source_dirs: list[Path] | Path | None = None
    ) -> list[Path]:
        """Walk the source dirs for ``state.vscdb`` files and enumerate
        every composer ID per DB. Returns Path-tokens.

        Tokens look like::

            C:/Users/.../state.vscdb::composerData:<composer_uuid>
        """
        dirs = self._normalize_source_dirs(source_dirs)
        tokens: list[Path] = []
        for d in dirs:
            db_files = self._find_dbs(d)
            for db in db_files:
                try:
                    with open_ro(db) as conn:
                        if not has_table(conn, "cursorDiskKV"):
                            continue
                        # value IS NOT NULL filters out "zombie" composer
                        # rows — keys that exist in cursorDiskKV but whose
                        # payload was wiped (observed: 4/349 composers in a
                        # real DB). They have nothing to export, so we drop
                        # them at discovery rather than letting them surface
                        # as conversion failures downstream.
                        for (key,) in conn.execute(
                            "SELECT key FROM cursorDiskKV "
                            "WHERE key LIKE ? "
                            "AND key IS NOT NULL "
                            "AND value IS NOT NULL",
                            (COMPOSER_KEY_PREFIX + "%",),
                        ):
                            composer_id = key[len(COMPOSER_KEY_PREFIX) :]
                            tokens.append(_make_source_token(db, composer_id))
                except sqlite3.Error:
                    continue
        return sorted(tokens, key=str)

    def find_by_name(
        self,
        query: str,
        *,
        source_dirs: list[Path] | Path | None = None,
        case_sensitive: bool = False,
    ) -> list[Path]:
        if not query.strip():
            return []
        matches: list[Path] = []
        for token in self.discover(source_dirs):
            corpus = self._cursor_search_corpus(token)
            if self._match(corpus, query, case_sensitive=case_sensitive):
                matches.append(token)
        return matches

    def find_by_id(
        self,
        session_id: str,
        *,
        source_dirs: list[Path] | Path | None = None,
    ) -> list[Path]:
        """Match by composer UUID (Cursor's stable session identifier)."""
        if not session_id.strip():
            return []
        sid = session_id.strip().lower()
        return [t for t in self.discover(source_dirs) if sid in str(t).lower()]

    def export_one(
        self, source: Path, *, validate: bool = True
    ) -> dict[str, Any]:
        return _convert_cursor_session(source, validate=validate)

    def ocf_filename_for(self, source: Path) -> str:
        """Cursor sources are ``<db>::composerData:<id>`` tokens — that
        ``:`` is illegal on Windows. Use just the composer UUID."""
        try:
            _, composer_id = _split_source_token(source)
        except ValueError:
            return super().ocf_filename_for(source)
        # composer UUIDs are themselves filesystem-safe on all platforms
        return f"composer-{composer_id}.ocf.json"

    def source_fingerprint(self, source: Path) -> tuple[int, int, str]:
        """Per-composer fingerprint over composerData + its bubble rows.

        DB-level mtime/size are coarse — they change whenever ANY
        composer in the DB updates. The sha256 component is per-composer
        so the manifest's hash check correctly skips composers whose
        data didn't change in this DB write.
        """
        db_path, composer_id = _split_source_token(source)
        db_stat = db_path.stat()

        h = hashlib.sha256()
        # Sentinels for absent rows. A NULL value is observed in real
        # Cursor DBs (rare — ~1.7% of composers in a 349-session DB
        # had at least one NULL bubble row). Hashing must tolerate it
        # without crashing; the conversion path either skips NULL
        # bubbles or raises ValueError for a NULL composer row.
        NULL_VALUE = b"<null>"
        MISSING_KEY = b"<no-key>"
        with open_ro(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?",
                (COMPOSER_KEY_PREFIX + composer_id,),
            ).fetchone()
            if row is None:
                return (db_stat.st_mtime_ns, db_stat.st_size, "missing")
            composer_value = row["value"]
            if composer_value is None:
                h.update(NULL_VALUE)
            else:
                h.update(composer_value.encode("utf-8"))
            for r in conn.execute(
                "SELECT key, value FROM cursorDiskKV "
                "WHERE key LIKE ? ORDER BY key",
                (f"{BUBBLE_KEY_PREFIX}{composer_id}:%",),
            ):
                k = r["key"]
                v = r["value"]
                h.update(k.encode("utf-8") if k is not None else MISSING_KEY)
                h.update(b"|")
                h.update(v.encode("utf-8") if v is not None else NULL_VALUE)
        return (db_stat.st_mtime_ns, db_stat.st_size, h.hexdigest())

    # ----- Session info (for ``ocf list``) ----------------------------------

    def session_info(self, source: Path) -> SessionInfo:
        """Peek composerData for title, workspace folder, and createdAt."""
        try:
            db_path, composer_id = _split_source_token(source)
        except ValueError:
            return SessionInfo(source=source, session_id=source.stem)

        title: str | None = None
        project: str | None = None
        created_at: datetime | None = None

        try:
            with open_ro(db_path) as conn:
                row = conn.execute(
                    "SELECT value FROM cursorDiskKV WHERE key = ?",
                    (COMPOSER_KEY_PREFIX + composer_id,),
                ).fetchone()
                if row and row["value"]:
                    cdata = json.loads(row["value"])
                    title = (
                        cdata.get("name")
                        or cdata.get("title")
                        or (cdata.get("latestConversationSummary") or {}).get("title")
                    )
                    if not isinstance(title, str):
                        title = None
                    wf = cdata.get("workspaceFolder")
                    if isinstance(wf, str) and wf:
                        parts = _split_path(wf)
                        project = parts[-1] if parts else wf
                    ca = cdata.get("createdAt")
                    if isinstance(ca, (int, float)):
                        created_at = datetime.fromtimestamp(
                            ca / 1000, tz=timezone.utc
                        )
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            pass

        return SessionInfo(
            source=source,
            session_id=composer_id,
            title=title,
            project=project,
            created_at=created_at,
        )

    # ----- helpers ---------------------------------------------------------

    def _find_dbs(self, root: Path) -> list[Path]:
        if not root.exists():
            return []
        if root.is_file() and root.name == "state.vscdb":
            return [root]
        if root.is_dir():
            return sorted(root.rglob("state.vscdb"))
        return []

    def _cursor_search_corpus(self, token: Path) -> str:
        """Title + workspace folder + composer_id form the search corpus."""
        try:
            db_path, composer_id = _split_source_token(token)
        except ValueError:
            return str(token)

        parts: list[str] = [composer_id]
        try:
            with open_ro(db_path) as conn:
                row = conn.execute(
                    "SELECT value FROM cursorDiskKV WHERE key = ?",
                    (COMPOSER_KEY_PREFIX + composer_id,),
                ).fetchone()
                if row is None or row["value"] is None:
                    return " ".join(parts)
                composer = json.loads(row["value"])
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            return " ".join(parts)

        # Title fields (Cursor schema has changed — check all known
        # locations). 'name' is the current location (verified late
        # 2024+); 'title' was older; latestConversationSummary.title
        # is the legacy fallback.
        for key in ("name", "title"):
            v = composer.get(key)
            if isinstance(v, str) and v:
                parts.append(v)
        lcs = composer.get("latestConversationSummary")
        if isinstance(lcs, dict):
            t = lcs.get("title")
            if isinstance(t, str) and t:
                parts.append(t)
        wf = composer.get("workspaceFolder")
        if isinstance(wf, str) and wf:
            parts.append(wf)
            parts.extend(_split_path(wf))
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Module-level shims
# ---------------------------------------------------------------------------

DEFAULT_SOURCE_DIR_FN = cursor_user_dir
MAPPING_ID = CursorAdapter.mapping_id

_default_adapter: CursorAdapter | None = None


def _adapter() -> CursorAdapter:
    global _default_adapter
    if _default_adapter is None:
        _default_adapter = CursorAdapter()
    return _default_adapter


def discover(source_dir: list[Path] | Path | None = None) -> list[Path]:
    return _adapter().discover(source_dir)


def find_by_name(
    query: str,
    *,
    source_dir: list[Path] | Path | None = None,
    case_sensitive: bool = False,
) -> list[Path]:
    return CursorAdapter().find_by_name(
        query, source_dirs=source_dir, case_sensitive=case_sensitive
    )


def find_by_id(
    session_id: str,
    *,
    source_dir: list[Path] | Path | None = None,
) -> list[Path]:
    return CursorAdapter().find_by_id(session_id, source_dirs=source_dir)


def export_one(
    source: Path | str, *, validate: bool = True
) -> dict[str, Any]:
    return _adapter().export_one(Path(source), validate=validate)


def export_all(
    out_dir: Path,
    *,
    sources: Iterable[Path] | None = None,
    source_dir: list[Path] | Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> ExportResult:
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
    return CursorAdapter().resolve_sources(
        source, source_dirs=source_dir, case_sensitive=case_sensitive
    )


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def _convert_cursor_session(
    source: Path | str, *, validate: bool
) -> dict[str, Any]:
    db_path, composer_id = _split_source_token(source)

    with open_ro(db_path) as conn:
        composer_row = conn.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (COMPOSER_KEY_PREFIX + composer_id,),
        ).fetchone()
        if composer_row is None or composer_row["value"] is None:
            raise ValueError(
                f"No composerData for {composer_id} in {db_path.name}"
            )
        try:
            composer = json.loads(composer_row["value"])
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Malformed composerData JSON for {composer_id}: {exc}"
            ) from exc

        bubbles: dict[str, dict[str, Any]] = {}
        for row in conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            (f"{BUBBLE_KEY_PREFIX}{composer_id}:%",),
        ):
            key = row["key"]
            value = row["value"]
            if key is None or value is None:
                continue
            # bubbleId:<cid>:<bid>
            rest = key[len(BUBBLE_KEY_PREFIX) :]
            sep = rest.find(":")
            if sep == -1:
                continue
            bid = rest[sep + 1 :]
            try:
                bubbles[bid] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue

    # Order bubbles per fullConversationHeadersOnly when present
    headers = composer.get("fullConversationHeadersOnly") or []
    order: list[str] = [
        h.get("bubbleId")
        for h in headers
        if isinstance(h, dict) and isinstance(h.get("bubbleId"), str)
    ]

    # Inline-storage fallback (older Cursor versions)
    inline_conversation = composer.get("conversation") or []
    if not bubbles and isinstance(inline_conversation, list) and inline_conversation:
        for i, b in enumerate(inline_conversation):
            if isinstance(b, dict):
                bid = b.get("bubbleId") or f"inline_{i}"
                bubbles[bid] = b
                if not order:
                    order.append(bid)

    if not order:
        order = list(bubbles.keys())

    # Build conversation block
    composer_v = composer.get("_v")
    workspace_folder = composer.get("workspaceFolder") if isinstance(
        composer.get("workspaceFolder"), str
    ) else None
    # Title resolution — current Cursor uses 'name', older versions used
    # 'title' or 'latestConversationSummary.title'. Try all in order.
    title: str | None = None
    for key in ("name", "title"):
        v = composer.get(key)
        if isinstance(v, str) and v:
            title = v
            break
    lcs = composer.get("latestConversationSummary") or {}
    if title is None and isinstance(lcs, dict):
        v = lcs.get("title")
        if isinstance(v, str) and v:
            title = v
    default_model = lcs.get("model") if isinstance(lcs, dict) else None
    # Fallback: also check 'modelId' or 'model' on composer itself
    if not default_model:
        for key in ("model", "modelId", "modelName"):
            v = composer.get(key)
            if isinstance(v, str) and v:
                default_model = v
                break

    started_at: datetime | None = _parse_ts(composer.get("createdAt"))
    ended_at: datetime | None = started_at

    messages: list[dict[str, Any]] = []
    for bid in order:
        bubble = bubbles.get(bid)
        if not bubble:
            continue
        envs = _bubble_to_envelopes(bid, bubble, default_model)
        for env in envs:
            messages.append(env)
        if envs:
            ts = _parse_ts(
                bubble.get("createdAt")
                or bubble.get("timestamp")
                or bubble.get("ts")
            )
            if ts:
                if started_at is None:
                    started_at = ts
                ended_at = ts

    # Empty composer — Cursor creates a row whenever the user clicks
    # "New Chat" but they may never type anything. On a real machine
    # this accounted for 195/345 composers (56.5%). Skip them to keep
    # the archive useful.
    if not messages:
        raise SkipExport(f"composer {composer_id} has no messages")

    conversation: dict[str, Any] = {
        "id": f"conv_cursor_{composer_id}",
        "title": title if isinstance(title, str) and title else None,
        "created_at": _format_iso(started_at) or _now_iso(),
        "default_model": default_model,
        "source": {
            "platform": "cursor",
            "export_tool": "ocf-py",
            "original_id": f"composer:{composer_id}",
        },
        "produced_by": {
            "tool": "ocf-py",
            "version": __version__,
            "mapping_id": f"{CursorAdapter.mapping_id}+{composer_v}"
            if composer_v is not None
            else CursorAdapter.mapping_id,
        },
        "meta": {
            "cursor": {
                "composer_id": composer_id,
                "_v": composer_v,
                "db_path": str(db_path),
                "workspace_folder": workspace_folder,
                "raw_format": "cursor-disk-kv",
            }
        },
    }
    if ended_at is not None:
        conversation["updated_at"] = _format_iso(ended_at)
    if workspace_folder:
        conversation["project"] = {
            "id": _project_id(workspace_folder),
            "name": _project_name(workspace_folder),
            "platform_id": None,
            "description": workspace_folder,
        }

    doc: dict[str, Any] = {
        "ocf_version": "0.1.0",
        "conversation": conversation,
        "messages": messages,
    }

    if validate:
        validate_strict(doc)
    return doc


def _bubble_to_envelopes(
    bubble_id: str, bubble: dict[str, Any], default_model: str | None
) -> list[dict[str, Any]]:
    """Convert one Cursor bubble to zero, one, or two OCF envelopes.

    Cursor bubbles fall into one of three shapes:

    1. **Content bubble** — ``text``/``thinking``/``codeBlocks`` populated.
       Produces ONE envelope: assistant or user with that content.

    2. **Tool bubble** — ``toolFormerData`` populated (assistant only;
       ~80% of "empty" assistant bubbles in real-world DBs).
       Produces TWO envelopes: assistant with ``tool_calls[]`` (and any
       content blocks if present) + role:tool message with the result.

    3. **Truly empty** — no content, no toolFormerData. Dropped.
    """
    btype = bubble.get("type")
    if btype == 1:
        role = "user"
    elif btype == 2:
        role = "assistant"
    else:
        return []

    content_blocks = _extract_content_blocks(bubble, role)

    # Tool data only makes sense on assistant bubbles
    tfd = bubble.get("toolFormerData") if role == "assistant" else None
    if isinstance(tfd, dict) and tfd.get("toolCallId"):
        return _envelopes_with_tool_call(
            bubble_id, bubble, content_blocks, tfd, default_model
        )

    # Pure content bubble
    if role == "user":
        return [
            _build_content_envelope(
                bubble_id, bubble, role, content_blocks, default_model,
                allow_empty_user=True,
            )
        ]
    # Assistant pure-content: drop if no content (no tool_calls fallback)
    if not content_blocks:
        return []
    return [
        _build_content_envelope(
            bubble_id, bubble, role, content_blocks, default_model,
            allow_empty_user=False,
        )
    ]


def _extract_content_blocks(
    bubble: dict[str, Any], role: str
) -> list[dict[str, Any]]:
    """Pull text/thinking/codeBlocks from a bubble in OCF content-block shape."""
    blocks: list[dict[str, Any]] = []

    text: str | None = None
    for field in BUBBLE_TEXT_FIELDS:
        v = bubble.get(field)
        if isinstance(v, str) and v.strip():
            text = v
            break
    if text:
        blocks.append({"type": "text", "text": text})

    thinking = bubble.get(BUBBLE_THINKING_FIELD)
    if (
        isinstance(thinking, str)
        and thinking.strip()
        and role == "assistant"
    ):
        blocks.append({"type": "thinking", "thinking": thinking})

    code_blocks = bubble.get(BUBBLE_CODE_BLOCKS_FIELD) or []
    if isinstance(code_blocks, list):
        for cb in code_blocks:
            if not isinstance(cb, dict):
                continue
            code = cb.get("code") or cb.get("rawCode")
            if not isinstance(code, str) or not code:
                continue
            block: dict[str, Any] = {"type": "code", "code": code}
            lang = cb.get("language") or cb.get("lang")
            if isinstance(lang, str) and lang:
                block["language"] = lang
            filename = cb.get("filename") or cb.get("file")
            if isinstance(filename, str) and filename:
                block["filename"] = filename
            blocks.append(block)
    return blocks


def _build_content_envelope(
    bubble_id: str,
    bubble: dict[str, Any],
    role: str,
    content_blocks: list[dict[str, Any]],
    default_model: str | None,
    *,
    allow_empty_user: bool,
) -> dict[str, Any]:
    """Build a content-only envelope (no tool_calls)."""
    if role == "user":
        if not content_blocks:
            inner_content: Any = "" if allow_empty_user else None
        elif len(content_blocks) == 1 and content_blocks[0]["type"] == "text":
            inner_content = content_blocks[0]["text"]
        else:
            inner_content = content_blocks
    else:
        if len(content_blocks) == 1 and content_blocks[0]["type"] == "text":
            inner_content = content_blocks[0]["text"]
        else:
            inner_content = content_blocks

    inner: dict[str, Any] = {"role": role, "content": inner_content}
    envelope: dict[str, Any] = {
        "id": bubble_id,
        "id_origin": "source",
        "message": inner,
        "meta": {
            "cursor_render": {
                "bubble_type": bubble.get("type"),
                "_v": bubble.get("_v"),
            }
        },
    }
    ts = _parse_ts(
        bubble.get("createdAt") or bubble.get("timestamp") or bubble.get("ts")
    )
    if ts is not None:
        envelope["created_at"] = _format_iso(ts)
    if role == "assistant" and default_model:
        envelope["model"] = default_model
    return envelope


def _envelopes_with_tool_call(
    bubble_id: str,
    bubble: dict[str, Any],
    content_blocks: list[dict[str, Any]],
    tfd: dict[str, Any],
    default_model: str | None,
) -> list[dict[str, Any]]:
    """Build the (assistant tool_call envelope, tool result envelope) pair.

    The assistant envelope keeps the bubble's id; the tool result
    envelope gets ``<bubble_id>-result`` (synthesized) so OCF's
    cross-reference rules (tool_call_id → tool_calls[].id) still hold.
    """
    tool_call_id = tfd["toolCallId"]
    name = tfd.get("name") or tfd.get("tool") or "unknown"
    if not isinstance(name, str):
        name = str(name)

    # Arguments preference: rawArgs (raw model output) → params → "{}"
    args = tfd.get("rawArgs")
    if not isinstance(args, str) or not args:
        args = tfd.get("params")
    if not isinstance(args, str) or not args:
        args = "{}"

    # Validate arguments parses as JSON; otherwise wrap as raw string.
    try:
        json.loads(args)
        args_str = args
    except json.JSONDecodeError:
        args_str = json.dumps({"_unparseable_args": args}, separators=(",", ":"))

    tool_call = {
        "id": tool_call_id,
        "id_origin": "source",
        "type": "function",
        "function": {"name": name, "arguments": args_str},
    }

    # Assistant envelope — content if present, plus tool_calls
    assistant_inner: dict[str, Any] = {"role": "assistant"}
    if content_blocks:
        if len(content_blocks) == 1 and content_blocks[0]["type"] == "text":
            assistant_inner["content"] = content_blocks[0]["text"]
        else:
            assistant_inner["content"] = content_blocks
    else:
        assistant_inner["content"] = None
    assistant_inner["tool_calls"] = [tool_call]

    cursor_render: dict[str, Any] = {
        "bubble_type": bubble.get("type"),
        "_v": bubble.get("_v"),
        "tool": tfd.get("tool"),
        "toolIndex": tfd.get("toolIndex"),
        "modelCallId": tfd.get("modelCallId"),
    }
    user_decision = tfd.get("userDecision")
    if isinstance(user_decision, str):
        cursor_render["user_decision"] = user_decision

    assistant_envelope: dict[str, Any] = {
        "id": bubble_id,
        "id_origin": "source",
        "message": assistant_inner,
        "meta": {"cursor_render": cursor_render},
    }
    ts = _parse_ts(
        bubble.get("createdAt") or bubble.get("timestamp") or bubble.get("ts")
    )
    if ts is not None:
        assistant_envelope["created_at"] = _format_iso(ts)
    if default_model:
        assistant_envelope["model"] = default_model

    # Tool result envelope
    result_value = tfd.get("result")
    if isinstance(result_value, str):
        tool_content: Any = result_value
    elif result_value is None:
        tool_content = ""
    else:
        tool_content = json.dumps(result_value, separators=(",", ":"))

    err = tfd.get("error")
    if isinstance(err, str) and err and (not tool_content or tool_content == "{}"):
        tool_content = err

    status = tfd.get("status")
    ocf_status: str | None = None
    if status == "error":
        ocf_status = "error"
    elif status == "cancelled":
        ocf_status = "cancelled"
    # "completed" / "loading" / unknown: leave status absent (=ok / unknown)

    tool_envelope: dict[str, Any] = {
        "id": f"{bubble_id}-result",
        "id_origin": "synthesized",
        "message": {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": tool_content,
        },
        "meta": {
            "cursor_render": {
                "bubble_type": bubble.get("type"),
                "tool_status": status,
            }
        },
    }
    if ocf_status is not None:
        tool_envelope["status"] = ocf_status
    if ts is not None:
        tool_envelope["created_at"] = _format_iso(ts)

    return [assistant_envelope, tool_envelope]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 4_000_000_000:
            n /= 1000.0  # millisecond timestamps
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


def _split_path(p: str) -> list[str]:
    n = p.replace("\\", "/")
    return [seg for seg in n.split("/") if seg]


def _hash_short(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _project_id(workspace_folder: str) -> str:
    return f"proj_{_hash_short(workspace_folder, 12)}"


def _project_name(workspace_folder: str) -> str:
    parts = _split_path(workspace_folder)
    return parts[-1] if parts else workspace_folder


__all__ = [
    "DEFAULT_SOURCE_DIR_FN",
    "MAPPING_ID",
    "AmbiguousMatchError",
    "CursorAdapter",
    "discover",
    "find_by_name",
    "find_by_id",
    "resolve_sources",
    "export_one",
    "export_all",
]
