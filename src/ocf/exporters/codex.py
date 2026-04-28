"""Codex (OpenAI Codex CLI / Desktop / IDE Extension) -> OCF exporter.

All Codex frontends share the same on-disk storage at
``~/.codex/sessions/<year>/<month>/<day>/rollout-<ts>-<uuid>.jsonl``.
The session_meta event's ``originator`` field tells us which frontend
wrote the session ("Codex Desktop", "Codex CLI", etc.).

Codex uses the **OpenAI Responses API** event shape, not Chat Completions.
This adapter maps Responses-style events into OCF's wire-strict
Chat-Completions inner message form. See module docstring for the
full mapping table.

This file exposes both a class-based adapter (:class:`CodexAdapter`)
and module-level shim functions (:func:`discover`, :func:`export_one`,
etc.) backed by a default adapter instance — older call sites keep
working while new code uses the adapter directly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from ocf import __version__
from ocf.core.schema import validate_strict
from ocf.exporters._base import (
    AmbiguousMatchError,
    SourceAdapter,
    export_all as _export_all_generic,
)
from ocf.exporters._common import ExportResult
from ocf.utils.jsonl import iter_jsonl_tailsafe
from ocf.utils.paths import codex_sessions_dir, home

# Top-level Codex event types
EVENT_SESSION_META = "session_meta"
EVENT_TURN_CONTEXT = "turn_context"
EVENT_RESPONSE_ITEM = "response_item"
EVENT_MSG = "event_msg"

# response_item.payload.type subtypes
RI_MESSAGE = "message"
RI_REASONING = "reasoning"
RI_FUNCTION_CALL = "function_call"
RI_FUNCTION_CALL_OUTPUT = "function_call_output"
RI_WEB_SEARCH_CALL = "web_search_call"
RI_CUSTOM_TOOL_CALL = "custom_tool_call"
RI_CUSTOM_TOOL_CALL_OUTPUT = "custom_tool_call_output"

EM_THREAD_NAME_UPDATED = "thread_name_updated"


def session_index_path(source_dir: Path | None = None) -> Path:
    """Default location of ``session_index.jsonl``.

    Convention: alongside the sessions directory's parent. For the
    default ``~/.codex/sessions/``, the index is at
    ``~/.codex/session_index.jsonl``.
    """
    if source_dir is None:
        return home() / ".codex" / "session_index.jsonl"
    return Path(source_dir).parent / "session_index.jsonl"


def load_session_index(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load ``session_index.jsonl`` as ``{session_id: row}``."""
    p = path if path is not None else session_index_path()
    if not p.exists() or not p.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = row.get("id")
            if isinstance(sid, str):
                out[sid] = row
    except OSError:
        return {}
    return out


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class CodexAdapter(SourceAdapter):
    """SourceAdapter for OpenAI Codex sessions (CLI + Desktop + IDE)."""

    name: ClassVar[str] = "codex"
    mapping_id: ClassVar[str] = "codex-responses-jsonl-v1"
    rollout_glob: ClassVar[str] = "rollout-*.jsonl"

    def __init__(self, *, session_index_path_override: Path | None = None) -> None:
        self._index_path_override = session_index_path_override
        # Per-path cache to avoid re-reading the same index inside one
        # export run. Entries are NEVER reused across source_dirs.
        self._index_cache: dict[str, dict[str, dict[str, Any]]] = {}

    # ----- SourceAdapter interface ----------------------------------------

    def default_source_dirs(self) -> list[Path]:
        return [codex_sessions_dir()]

    def find_by_name(
        self,
        query: str,
        *,
        source_dirs: list[Path] | Path | None = None,
        case_sensitive: bool = False,
    ) -> list[Path]:
        if not query.strip():
            return []
        index = self._resolve_index_for_dirs(source_dirs)
        matches: list[Path] = []
        for path in self.discover(source_dirs):
            corpus = self._codex_search_corpus(path, index)
            if self._match(corpus, query, case_sensitive=case_sensitive):
                matches.append(path)
        return matches

    def export_one(
        self, source: Path, *, validate: bool = True
    ) -> dict[str, Any]:
        return _convert_codex_session(
            source,
            session_index=self._resolve_index_for_source(Path(source)),
            validate=validate,
        )

    # ----- Internal helpers -----------------------------------------------

    def _resolve_index_for_dirs(
        self, source_dirs: list[Path] | Path | None
    ) -> dict[str, dict[str, Any]]:
        """Pick the right session_index for a discover/find run."""
        if self._index_path_override is not None:
            return self._load_index_cached(self._index_path_override)
        if source_dirs is not None:
            for d in self._normalize_source_dirs(source_dirs):
                idx = self._load_index_cached(session_index_path(d))
                if idx:
                    return idx
        return self._load_index_cached(session_index_path())

    def _resolve_index_for_source(
        self, source: Path
    ) -> dict[str, dict[str, Any]]:
        """Pick the right session_index by walking up from a source file.

        Codex puts sessions at ``<root>/sessions/<y>/<m>/<d>/rollout-*``
        and the index at ``<root>/session_index.jsonl``. Walking parents
        until ``session_index.jsonl`` is found handles arbitrary roots
        (default home, custom dirs, test fixtures).
        """
        if self._index_path_override is not None:
            return self._load_index_cached(self._index_path_override)
        for parent in source.resolve().parents:
            candidate = parent / "session_index.jsonl"
            if candidate.exists():
                return self._load_index_cached(candidate)
            if parent == Path.home():
                break
        return self._load_index_cached(session_index_path())

    def _load_index_cached(
        self, path: Path
    ) -> dict[str, dict[str, Any]]:
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in self._index_cache:
            self._index_cache[key] = load_session_index(path)
        return self._index_cache[key]

    def _codex_search_corpus(
        self, path: Path, index: dict[str, dict[str, Any]]
    ) -> str:
        parts: list[str] = [path.name]
        # Folder names from any of our default source dirs
        for d in self.default_source_dirs():
            try:
                rel = path.resolve().relative_to(d.resolve())
                parts.extend(rel.parent.parts)
                break
            except ValueError:
                continue
        session_id, cwd, originator = _peek_session_meta(path)
        if session_id:
            parts.append(session_id)
            row = index.get(session_id)
            if row:
                tn = row.get("thread_name")
                if isinstance(tn, str) and tn:
                    parts.append(tn)
        if cwd:
            parts.append(cwd)
            parts.extend(_split_cwd(cwd))
        if originator:
            parts.append(originator)
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Module-level shims (backward-compat with v0 API)
# ---------------------------------------------------------------------------

DEFAULT_SOURCE_DIR_FN = codex_sessions_dir
MAPPING_ID = CodexAdapter.mapping_id
ROLLOUT_GLOB = CodexAdapter.rollout_glob

_default_adapter: CodexAdapter | None = None


def _adapter() -> CodexAdapter:
    global _default_adapter
    if _default_adapter is None:
        _default_adapter = CodexAdapter()
    return _default_adapter


def discover(source_dir: Path | None = None) -> list[Path]:
    return _adapter().discover(source_dir)


def find_by_name(
    query: str,
    *,
    source_dir: Path | None = None,
    case_sensitive: bool = False,
    session_index: dict[str, dict[str, Any]] | None = None,
) -> list[Path]:
    # Fresh adapter per call so the per-test source_dir wins over any
    # cached real-machine index from a prior call.
    adapter = CodexAdapter()
    if session_index is not None:
        # Pre-seed cache so the explicit index is used regardless of
        # source_dirs path.
        adapter._index_cache["__explicit__"] = session_index  # noqa: SLF001

        def _force(_dirs=None):  # noqa: ANN001
            return session_index
        adapter._resolve_index_for_dirs = _force  # type: ignore[assignment]
    return adapter.find_by_name(
        query, source_dirs=source_dir, case_sensitive=case_sensitive
    )


def find_by_id(
    session_id: str, *, source_dir: Path | None = None
) -> list[Path]:
    return _adapter().find_by_id(session_id, source_dirs=source_dir)


def export_one(
    source: Path,
    *,
    validate: bool = True,
    session_index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if session_index is not None:
        return _convert_codex_session(
            source, session_index=session_index, validate=validate
        )
    return _adapter().export_one(source, validate=validate)


def export_all(
    out_dir: Path,
    *,
    sources: Iterable[Path] | None = None,
    source_dir: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> ExportResult:
    """Bulk runner. Validates source_dir exists when no explicit sources given,
    matching the v0 API contract."""
    if sources is None and source_dir is None:
        src = codex_sessions_dir()
        if not src.exists():
            raise FileNotFoundError(
                f"Codex sessions directory not found: {src}. "
                "Pass source_dir=... explicitly if Codex is installed elsewhere."
            )
    if sources is None and source_dir is not None:
        if not Path(source_dir).exists():
            raise FileNotFoundError(
                f"Codex sessions directory not found: {source_dir}. "
                "Pass source_dir=... explicitly if Codex is installed elsewhere."
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
    source_dir: Path | None = None,
    case_sensitive: bool = False,
) -> list[Path]:
    return _adapter().resolve_sources(
        source, source_dirs=source_dir, case_sensitive=case_sensitive
    )


# ---------------------------------------------------------------------------
# Conversion logic (format-specific, called from CodexAdapter.export_one)
# ---------------------------------------------------------------------------

def _convert_codex_session(
    source: Path,
    *,
    session_index: dict[str, dict[str, Any]],
    validate: bool,
) -> dict[str, Any]:
    """Pure function: Codex rollout file -> OCF document dict."""
    events = list(iter_jsonl_tailsafe(source))

    sm = next((e for e in events if e.get("type") == EVENT_SESSION_META), None)
    if sm is None:
        raise ValueError(
            f"No session_meta event found in {source.name}; "
            "not a recognizable Codex rollout file."
        )
    sm_payload = sm.get("payload", {}) or {}
    session_id = sm_payload.get("id") or _hash_short(source.name)
    cwd = sm_payload.get("cwd") or None
    originator = sm_payload.get("originator")
    cli_version = sm_payload.get("cli_version")
    codex_source = sm_payload.get("source")
    model_provider = sm_payload.get("model_provider")
    base_instr = sm_payload.get("base_instructions") or {}
    base_instr_text = base_instr.get("text") if isinstance(base_instr, dict) else None

    started_at = _parse_iso(sm.get("timestamp")) or _parse_iso(
        sm_payload.get("timestamp")
    )
    ended_at: datetime | None = started_at

    title: str | None = None
    row = session_index.get(session_id)
    if row and isinstance(row.get("thread_name"), str):
        title = row["thread_name"]
    for ev in events:
        if ev.get("type") == EVENT_MSG:
            p = ev.get("payload", {}) or {}
            if p.get("type") == EM_THREAD_NAME_UPDATED:
                tn = p.get("thread_name")
                if isinstance(tn, str) and tn:
                    title = tn

    messages: list[dict[str, Any]] = []
    current_model: str | None = None
    default_model: str | None = None
    counter = 0

    def next_msg_id() -> str:
        nonlocal counter
        counter += 1
        return f"msg_{counter:04d}"

    for ev in events:
        ts = _parse_iso(ev.get("timestamp"))
        if ts is not None:
            ended_at = ts
        t = ev.get("type")
        p = ev.get("payload", {}) or {}

        if t == EVENT_TURN_CONTEXT:
            m = p.get("model")
            if isinstance(m, str) and m:
                current_model = m
                if default_model is None:
                    default_model = m
            continue

        if t == EVENT_RESPONSE_ITEM:
            sub = p.get("type")
            if sub == RI_MESSAGE:
                env = _envelope_from_message(
                    p, ts, next_msg_id, model=current_model
                )
                if env is not None:
                    messages.append(env)
                continue
            if sub == RI_REASONING:
                messages.append(
                    _envelope_from_reasoning(
                        p, ts, next_msg_id, model=current_model
                    )
                )
                continue
            if sub in (RI_FUNCTION_CALL, RI_WEB_SEARCH_CALL, RI_CUSTOM_TOOL_CALL):
                messages.append(
                    _envelope_from_function_call(
                        p, ts, next_msg_id, model=current_model, kind=sub
                    )
                )
                continue
            if sub in (RI_FUNCTION_CALL_OUTPUT, RI_CUSTOM_TOOL_CALL_OUTPUT):
                messages.append(
                    _envelope_from_function_call_output(
                        p, ts, next_msg_id, kind=sub
                    )
                )
                continue
            continue
        continue

    conversation: dict[str, Any] = {
        "id": f"conv_codex_{session_id}",
        "title": title,
        "created_at": _format_iso(started_at) or _now_iso(),
        "default_model": default_model,
        "source": {
            "platform": "codex",
            "export_tool": "ocf-py",
            "original_id": source.name,
        },
        "produced_by": {
            "tool": "ocf-py",
            "version": __version__,
            "mapping_id": CodexAdapter.mapping_id,
        },
        "meta": {
            "codex": {
                "session_id": session_id,
                "originator": originator,
                "cli_version": cli_version,
                "source": codex_source,
                "model_provider": model_provider,
                "absolute_path_at_capture": cwd,
                "base_instructions_present": bool(base_instr_text),
                "raw_format": "responses-api-jsonl",
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
    if base_instr_text:
        conversation["meta"]["codex"]["base_instructions_text"] = base_instr_text

    doc: dict[str, Any] = {
        "ocf_version": "0.1.0",
        "conversation": conversation,
        "messages": messages,
    }

    if validate:
        validate_strict(doc)

    return doc


# ---------------------------------------------------------------------------
# Envelope builders + small helpers
# ---------------------------------------------------------------------------

def _envelope_from_message(
    payload: dict[str, Any],
    ts: datetime | None,
    next_id,
    *,
    model: str | None,
) -> dict[str, Any] | None:
    role = payload.get("role")
    if role not in ("user", "assistant", "developer", "system"):
        return None
    content_blocks: list[dict[str, Any]] = []
    for block in payload.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt in ("input_text", "output_text", "text"):
            text = block.get("text", "")
            if isinstance(text, str):
                content_blocks.append({"type": "text", "text": text})

    inner: dict[str, Any] = {"role": role}
    if content_blocks:
        if len(content_blocks) == 1:
            inner["content"] = content_blocks[0]["text"]
        else:
            inner["content"] = content_blocks
    else:
        inner["content"] = None if role == "assistant" else ""

    envelope: dict[str, Any] = {
        "id": next_id(),
        "id_origin": "synthesized",
        "message": inner,
        "meta": {
            "codex_render": {
                "raw_event_type": "response_item.message",
                "original_role": role,
            }
        },
    }
    if ts is not None:
        envelope["created_at"] = _format_iso(ts)
    if role == "assistant" and model:
        envelope["model"] = model
    return envelope


def _envelope_from_reasoning(
    payload: dict[str, Any],
    ts: datetime | None,
    next_id,
    *,
    model: str | None,
) -> dict[str, Any]:
    summary = payload.get("summary") or []
    text_parts: list[str] = []
    for s in summary:
        if isinstance(s, dict):
            txt = s.get("text")
            if isinstance(txt, str):
                text_parts.append(txt)
        elif isinstance(s, str):
            text_parts.append(s)
    plain = payload.get("content")
    if isinstance(plain, str):
        text_parts.append(plain)
    thinking_text = "\n".join(p for p in text_parts if p) or "[encrypted reasoning]"

    envelope: dict[str, Any] = {
        "id": next_id(),
        "id_origin": "synthesized",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": thinking_text}],
        },
        "meta": {
            "codex_render": {
                "raw_event_type": "response_item.reasoning",
                "summary_present": bool(summary),
                "encrypted_content_present": bool(payload.get("encrypted_content")),
            }
        },
    }
    enc = payload.get("encrypted_content")
    if isinstance(enc, str):
        envelope["meta"]["codex_render"]["encrypted_content"] = enc
    if ts is not None:
        envelope["created_at"] = _format_iso(ts)
    if model:
        envelope["model"] = model
    return envelope


def _envelope_from_function_call(
    payload: dict[str, Any],
    ts: datetime | None,
    next_id,
    *,
    model: str | None,
    kind: str,
) -> dict[str, Any]:
    name = payload.get("name") or kind
    args = payload.get("arguments")
    if isinstance(args, str):
        args_str = args
    elif args is None:
        args_str = "{}"
    else:
        args_str = json.dumps(args, separators=(",", ":"))
    call_id = payload.get("call_id") or payload.get("id") or f"call_{next_id()}"

    envelope: dict[str, Any] = {
        "id": next_id(),
        "id_origin": "synthesized",
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "id_origin": "source"
                    if (payload.get("call_id") or payload.get("id"))
                    else "synthesized",
                    "type": "function",
                    "function": {"name": name, "arguments": args_str},
                }
            ],
        },
        "meta": {
            "codex_render": {"raw_event_type": f"response_item.{kind}"}
        },
    }
    if ts is not None:
        envelope["created_at"] = _format_iso(ts)
    if model:
        envelope["model"] = model
    return envelope


def _envelope_from_function_call_output(
    payload: dict[str, Any],
    ts: datetime | None,
    next_id,
    *,
    kind: str,
) -> dict[str, Any]:
    call_id = payload.get("call_id") or payload.get("id") or "call_unknown"
    output = payload.get("output")
    if isinstance(output, str):
        content_value: Any = output
    elif output is None:
        content_value = ""
    else:
        content_value = json.dumps(output, separators=(",", ":"))
    envelope: dict[str, Any] = {
        "id": next_id(),
        "id_origin": "synthesized",
        "message": {
            "role": "tool",
            "tool_call_id": call_id,
            "content": content_value,
        },
        "meta": {
            "codex_render": {"raw_event_type": f"response_item.{kind}"}
        },
    }
    if ts is not None:
        envelope["created_at"] = _format_iso(ts)
    return envelope


def _peek_session_meta(
    path: Path,
) -> tuple[str | None, str | None, str | None]:
    try:
        for ev in iter_jsonl_tailsafe(path):
            if ev.get("type") == EVENT_SESSION_META:
                p = ev.get("payload", {})
                return (
                    p.get("id") if isinstance(p.get("id"), str) else None,
                    p.get("cwd") if isinstance(p.get("cwd"), str) else None,
                    p.get("originator") if isinstance(p.get("originator"), str) else None,
                )
            return None, None, None
    except OSError:
        pass
    return None, None, None


def _split_cwd(cwd: str) -> list[str]:
    cwd_norm = cwd.replace("\\", "/")
    return [part for part in cwd_norm.split("/") if part]


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 4_000_000_000:
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc)
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s).astimezone(timezone.utc)
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
    parts = _split_cwd(cwd)
    return parts[-1] if parts else cwd


__all__ = [
    "DEFAULT_SOURCE_DIR_FN",
    "MAPPING_ID",
    "ROLLOUT_GLOB",
    "AmbiguousMatchError",
    "CodexAdapter",
    "session_index_path",
    "load_session_index",
    "discover",
    "find_by_name",
    "find_by_id",
    "resolve_sources",
    "export_one",
    "export_all",
]
