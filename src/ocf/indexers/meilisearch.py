"""Meilisearch indexer for OCF sessions.

Pushes rendered session content + metadata into a Meilisearch index
so any agent (or human) can search across all archived sessions.

Document schema (per session)::

    {
        "id":         "423b57f7-a078-4e8c-8b41-c72a70ca4652",
        "session_id": "423b57f7-a078-4e8c-8b41-c72a70ca4652",
        "tool":       "claude-code-cli",
        "title":      "Research chat export formats",
        "project":    "OpenChatFormat",
        "model":      "claude-opus-4-6",
        "created_at": 1714089600,         # unix timestamp (filterable)
        "created_date": "2026-04-26",     # human-readable
        "content":    "# Research chat export formats\\n\\n..."
    }

The ``id`` is the OCF archive filename stem (``<uuid>``,
``rollout-<ts>-<uuid>``, ``composer-<uuid>``, ``chatgpt-<id>``, ...):
unique per archive file, so adapters cannot collide, and unchanged
when a re-export changes the tool label or creation date. Tool and
date live in their own (filterable) fields.

All write operations are upserts - re-indexing a session that already
exists just replaces it. This makes the indexer fully idempotent.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Attributes that Meilisearch should make filterable/sortable.
_FILTERABLE_ATTRIBUTES = [
    "tool",
    "project",
    "model",
    "created_at",
    "created_date",
]

_SORTABLE_ATTRIBUTES = [
    "created_at",
]

# Default index name
DEFAULT_INDEX = "ocf-sessions"

# Meilisearch's default wait_for_task timeout is 5000ms — too short for
# batches over ~200 docs or when the server is under load. Give tasks
# 5 minutes to finalize. Tasks that don't finish in that window will
# still be applied server-side; we just miss the confirmation and count
# the batch as failed locally.
_TASK_TIMEOUT_MS = 300_000


@dataclass
class IndexResult:
    """Summary of an index run."""

    indexed: int = 0
    skipped: int = 0
    failed: int = 0


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _extract_session_id(conv: dict[str, Any]) -> str:
    """Pull a clean session ID from the OCF conversation.

    Prefers the first UUID found in ``source.original_id`` or
    ``conversation.id``. Falls back to the raw value stripped of
    common prefixes and file extensions.
    """
    raw = (conv.get("source") or {}).get("original_id") or conv.get("id") or "unknown"
    # Best case: extract a UUID
    m = _UUID_RE.search(raw)
    if m:
        return m.group(0)
    # Fallback: strip known noise
    sid = raw
    for prefix in ("conv_", "conv_claude_code_", "conv_codex_", "conv_cursor_"):
        if sid.startswith(prefix):
            sid = sid[len(prefix):]
            break
    # Strip file extension (.jsonl, .json)
    sid = re.sub(r"\.(jsonl|json)$", "", sid)
    return sid


def _make_document(
    doc: dict[str, Any],
    tool_name: str,
    rendered_content: str,
    *,
    doc_key: str | None = None,
) -> dict[str, Any]:
    """Build a Meilisearch document from an OCF document + rendered text.

    ``doc_key`` (the OCF archive filename stem) is used as the document
    ``id`` on its own: unique per archive file, even where one session
    spans several files (Codex history pages, sub-agents), and stable
    when a document's tool label or creation date changes. Putting those
    into the id made a re-labelled document land next to its old copy.
    Without ``doc_key`` the legacy ``<tool>__<datetime>__<session>`` id
    is built.
    """
    conv = doc.get("conversation", {})

    session_id = _extract_session_id(conv)

    created_at_str = conv.get("created_at")
    created_ts: int = 0
    created_date: str = ""
    created_datetime: str = ""
    if created_at_str:
        try:
            dt = datetime.fromisoformat(
                created_at_str.replace("Z", "+00:00")
            )
            created_ts = int(dt.timestamp())
            created_date = dt.strftime("%Y-%m-%d")
            created_datetime = dt.strftime("%Y-%m-%dT%H-%M-%S")
        except (ValueError, OSError):
            pass

    # Build a human-readable document ID: <tool>__<datetime>__<session_id>
    # Meilisearch IDs allow only alphanumeric, hyphens, underscores.
    if doc_key:
        raw_id = doc_key
    else:
        dt_part = f"__{created_datetime}" if created_datetime else ""
        raw_id = f"{tool_name}{dt_part}__{session_id}"
    clean_id = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_id)

    return {
        "id": clean_id,
        "session_id": session_id,
        "tool": tool_name,
        "title": conv.get("title") or "",
        "project": _extract_project(conv),
        "model": conv.get("default_model") or "",
        "created_at": created_ts,
        "created_date": created_date,
        "content": rendered_content,
    }


def _extract_project(conv: dict[str, Any]) -> str:
    """Best-effort project name from conversation metadata."""
    project = conv.get("project")
    if isinstance(project, dict):
        name = project.get("name")
        if isinstance(name, str) and name:
            return name
    if isinstance(project, str) and project:
        return project
    # Fallback: source.cwd last path component
    cwd = (conv.get("source") or {}).get("cwd")
    if isinstance(cwd, str) and cwd:
        parts = cwd.replace("\\", "/").rstrip("/").split("/")
        return parts[-1] if parts else ""
    return ""


OCF_SUFFIX = ".ocf.json"
RECOVERED_SUFFIX = ".meili.json"

# Platform -> facet name where the platform alone is unambiguous.
_PLATFORM_TOOLS = {"codex": "codex", "cursor": "cursor"}


def archive_tool_name(doc: dict[str, Any], source_path: str | None = None) -> str:
    """Facet ``tool`` for an archived OCF document.

    1. ``conversation.meta.ocf_py.source_adapter`` (written by exports
       since 2026-10).
    2. Codex / Cursor follow from the platform.
    3. Claude Code: a manifest source path under
       ``local-agent-mode-sessions`` means Cowork. CLI and Desktop
       sessions share ``~/.claude/projects`` and cannot be told apart
       after the fact, so everything else is ``claude-code``.
    """
    conv = doc.get("conversation") or {}
    tagged = ((conv.get("meta") or {}).get("ocf_py") or {}).get("source_adapter")
    if isinstance(tagged, str) and tagged:
        return tagged
    platform = (conv.get("source") or {}).get("platform") or ""
    if platform in _PLATFORM_TOOLS:
        return _PLATFORM_TOOLS[platform]
    if source_path and "local-agent-mode-sessions" in source_path.replace("\\", "/"):
        return "claude-cowork-app"
    return platform.replace("_", "-") or "unknown"


def load_archive_provenance(archive_dir: Path) -> dict[str, str]:
    """Map OCF filename -> original source path from ``.ocf-manifest.json``."""
    path = Path(archive_dir) / ".ocf-manifest.json"
    try:
        exports = json.loads(path.read_text(encoding="utf-8")).get("exports", {})
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        e["ocf_path"]: e.get("source_path") or key
        for key, e in exports.items()
        if isinstance(e, dict) and e.get("ocf_path")
    }


def iter_archive_documents(
    archive_dir: Path,
    render: Callable[[dict[str, Any]], str],
) -> Iterator[tuple[Path, dict[str, Any] | None, str | None]]:
    """Yield ``(ocf_path, meili_doc, problem)`` for every archived OCF file.

    ``problem`` is ``None`` for an indexable document, ``"empty"`` for a
    document without messages (skipped, never indexed), or an error
    description (``meili_doc`` is then ``None``).
    """
    archive_dir = Path(archive_dir)
    provenance = load_archive_provenance(archive_dir)
    for path in sorted(archive_dir.rglob("*" + OCF_SUFFIX)):
        try:
            doc = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            yield path, None, f"unreadable: {exc}"
            continue
        if not doc.get("messages"):
            yield path, None, "empty"
            continue
        try:
            rendered = render(doc)
        except Exception as exc:  # noqa: BLE001
            yield path, None, f"render failed: {exc}"
            continue
        tool = archive_tool_name(doc, provenance.get(path.name))
        key = path.name[: -len(OCF_SUFFIX)]
        yield path, _make_document(doc, tool, rendered, doc_key=key), None


def iter_recovered_documents(
    recovered_dir: Path, *, skip_sessions: set[str] | frozenset[str] = frozenset()
) -> Iterator[dict[str, Any]]:
    """Index documents saved verbatim from an earlier index (``*.meili.json``).

    For sessions whose source and OCF document no longer exist anywhere,
    the old index copy is the only content left; it is pushed unchanged.
    Sessions in ``skip_sessions`` (already covered by an archive document,
    e.g. because another machine still had the source) are left out.
    """
    for path in sorted(Path(recovered_dir).glob("*" + RECOVERED_SUFFIX)):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not (isinstance(doc, dict) and doc.get("id") and doc.get("content")):
            continue
        if doc.get("session_id") in skip_sessions:
            continue
        yield doc


def ensure_index(
    client: Any,
    index_name: str = DEFAULT_INDEX,
) -> Any:
    """Create the index if needed and configure filterable attributes.

    Returns the index object.
    """
    # Create index (idempotent - Meilisearch ignores if exists)
    task = client.create_index(index_name, {"primaryKey": "id"})
    client.wait_for_task(task.task_uid, timeout_in_ms=_TASK_TIMEOUT_MS)

    index = client.index(index_name)

    # Set filterable + sortable attributes
    task = index.update_filterable_attributes(_FILTERABLE_ATTRIBUTES)
    client.wait_for_task(task.task_uid, timeout_in_ms=_TASK_TIMEOUT_MS)

    task = index.update_sortable_attributes(_SORTABLE_ATTRIBUTES)
    client.wait_for_task(task.task_uid, timeout_in_ms=_TASK_TIMEOUT_MS)

    return index


def index_documents(
    index: Any,
    documents: list[dict[str, Any]],
    *,
    batch_size: int = 50,
    client: Any = None,
) -> int:
    """Push documents into the index in batches. Returns count indexed."""
    total = 0
    for i in range(0, len(documents), batch_size):
        batch = documents[i : i + batch_size]
        task = index.add_documents(batch)
        if client:
            client.wait_for_task(task.task_uid, timeout_in_ms=_TASK_TIMEOUT_MS)
        total += len(batch)
    return total


def search(
    index: Any,
    query: str,
    *,
    tool: str | None = None,
    project: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search the index with optional facet filters."""
    filters: list[str] = []
    if tool:
        filters.append(f'tool = "{tool}"')
    if project:
        filters.append(f'project = "{project}"')

    params: dict[str, Any] = {"limit": limit}
    if filters:
        params["filter"] = " AND ".join(filters)

    result = index.search(query, params)
    return result.get("hits", [])
