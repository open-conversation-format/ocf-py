"""Meilisearch indexer for OCF sessions.

Pushes rendered session content + metadata into a Meilisearch index
so any agent (or human) can search across all archived sessions.

Document schema (per session)::

    {
        "id":         "claude_code_cli__423b57f7",
        "session_id": "423b57f7-a078-4e8c-8b41-c72a70ca4652",
        "tool":       "claude-code-cli",
        "title":      "Research chat export formats",
        "project":    "OpenChatFormat",
        "model":      "claude-opus-4-6",
        "created_at": 1714089600,         # unix timestamp (filterable)
        "created_date": "2026-04-26",     # human-readable
        "content":    "# Research chat export formats\\n\\n..."
    }

The ``id`` field combines tool + session stem to avoid collisions
across adapters (a UUID can appear in both CLI and App).

All write operations are upserts - re-indexing a session that already
exists just replaces it. This makes the indexer fully idempotent.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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
) -> dict[str, Any]:
    """Build a Meilisearch document from an OCF document + rendered text."""
    conv = doc.get("conversation", {})

    session_id = _extract_session_id(conv)

    created_at_str = conv.get("created_at")
    created_ts: int = 0
    created_date: str = ""
    if created_at_str:
        try:
            dt = datetime.fromisoformat(
                created_at_str.replace("Z", "+00:00")
            )
            created_ts = int(dt.timestamp())
            created_date = dt.strftime("%Y-%m-%d")
        except (ValueError, OSError):
            pass

    # Build a stable document ID from tool + session.
    # Meilisearch IDs allow only alphanumeric, hyphens, underscores.
    clean_id = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{tool_name}__{session_id}")

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


def ensure_index(
    client: Any,
    index_name: str = DEFAULT_INDEX,
) -> Any:
    """Create the index if needed and configure filterable attributes.

    Returns the index object.
    """
    # Create index (idempotent - Meilisearch ignores if exists)
    task = client.create_index(index_name, {"primaryKey": "id"})
    client.wait_for_task(task.task_uid)

    index = client.index(index_name)

    # Set filterable + sortable attributes
    task = index.update_filterable_attributes(_FILTERABLE_ATTRIBUTES)
    client.wait_for_task(task.task_uid)

    task = index.update_sortable_attributes(_SORTABLE_ATTRIBUTES)
    client.wait_for_task(task.task_uid)

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
            client.wait_for_task(task.task_uid)
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
