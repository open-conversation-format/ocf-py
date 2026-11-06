"""Meilisearch indexer for OCF sessions.

Pushes rendered session content + metadata into a Meilisearch index
so any agent (or human) can search across all archived sessions.

Sessions are split into chunks
------------------------------
Meilisearch indexes at most 65,535 word positions per attribute and
silently ignores the rest, so the late part of a long session - the
part you need to pick up where it ended - would not be searchable as
one document. Each session is therefore split into chunks of at most
``CHUNK_WORDS`` words / ``CHUNK_CHARS`` characters, cut at message
boundaries, one index document per chunk. Search with
``distinct: "doc"`` for one hit per session;
filter ``doc = <key>`` to search inside one session; fetch
``<key>__c<n-1>`` / ``<key>__c<n+1>`` to page backwards and forwards.
Every chunk also carries the size of the whole session, so an agent
can decide from the hit alone whether to read step by step or load
the whole archive file.

Document schema (per chunk)::

    {
        "id":            "423b57f7-a078-4e8c-8b41-c72a70ca4652__c3",
        "doc":           "423b57f7-a078-4e8c-8b41-c72a70ca4652",  # archive file stem
        "session_id":    "423b57f7-a078-4e8c-8b41-c72a70ca4652",
        "tool":          "claude-code-cli",
        "title":         "Research chat export formats",
        "project":       "OpenChatFormat",
        "model":         "claude-opus-4-6",
        "created_at":    1714089600,      # session start (unix, filterable)
        "created_date":  "2026-04-26",
        "updated_at":    1714262400,      # last activity of the session
        "updated_date":  "2026-04-28",
        "chunk":         3,               # 1-based position ...
        "chunks":        5,               # ... of this many
        "msg_from":      410,             # index range in the OCF document's
        "msg_to":        633,             # "messages" (inclusive)
        "chunk_from":    1714150000,      # time range of the chunk (unix)
        "chunk_to":      1714170000,
        "words":         3980,            # words in this chunk
        "session_words": 18200,           # whole session (dialog text)
        "session_chars": 121000,
        "session_messages": 1490,         # OCF messages incl. tool calls
        "archive_bytes": 2400000,         # size of the OCF file, if known
        "content":       "**User**\\n\\n..."
    }

``doc`` is the OCF archive filename stem (``<uuid>``,
``rollout-<ts>-<uuid>``, ``composer-<uuid>``, ``chatgpt-<id>``, ...):
unique per archive file, so adapters cannot collide, and unchanged
when a re-export changes the tool label or creation date. Tool and
dates live in their own (filterable) fields.

All write operations are upserts - re-indexing a session that already
exists just replaces its chunks. A session that shrinks to fewer chunks
leaves its old trailing chunks behind; rebuild into a fresh index when
that matters.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ocf.renderers.markdown import simple_message_blocks

logger = logging.getLogger(__name__)

# Attributes that Meilisearch should make filterable/sortable.
_FILTERABLE_ATTRIBUTES = [
    "tool",
    "project",
    "model",
    "created_at",
    "created_date",
    "updated_at",
    "updated_date",
    "doc",
    "session_id",
    "chunk",
    "chunk_from",
    "chunk_to",
]

_SORTABLE_ATTRIBUTES = [
    "created_at",
    "updated_at",
    "chunk",
    "chunk_from",
]

# Default index name
DEFAULT_INDEX = "ocf-sessions"

# Chunk size: whichever limit is hit first. Small enough that one chunk
# fits a single tool output (about 30,000 characters) and far below
# Meilisearch's position limit. The character limit matters for code
# and URL heavy text, where 4,000 words can be 80,000+ characters.
CHUNK_WORDS = 4000
CHUNK_CHARS = 24000

# Messages already start with a speaker label (**User** / **Claude**), so
# a blank line separates them without adding countable words.
CHUNK_SEPARATOR = "\n\n"

# Meilisearch's default wait_for_task timeout is 5000ms — too short for
# batches over ~200 docs or when the server is under load. Give tasks
# 5 minutes to finalize. Tasks that don't finish in that window will
# still be applied server-side; we just miss the confirmation and count
# the batch as failed locally.
_TASK_TIMEOUT_MS = 300_000


@dataclass
class IndexResult:
    """Summary of an index run (``indexed`` counts sessions)."""

    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    chunks: int = 0


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


def _clean_id(raw: str) -> str:
    """Meilisearch ids allow only alphanumerics, hyphens and underscores."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw)


def _ts(value: Any) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, OSError):
        return 0


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""


def _session_fields(doc: dict[str, Any], tool_name: str) -> dict[str, Any]:
    """Fields shared by every chunk of one session."""
    conv = doc.get("conversation", {})
    messages = doc.get("messages") or []
    created = _ts(conv.get("created_at"))
    updated = _ts(conv.get("updated_at")) or max(
        (_ts(m.get("created_at")) for m in messages), default=0
    ) or created
    return {
        "session_id": _extract_session_id(conv),
        "tool": tool_name,
        "title": conv.get("title") or "",
        "project": _extract_project(conv),
        "model": conv.get("default_model") or "",
        "created_at": created,
        "created_date": _date(created),
        "updated_at": updated,
        "updated_date": _date(updated),
        "session_messages": len(messages),
    }


def _split_line(line: str, words_limit: int, chars_limit: int) -> list[str]:
    """Split one overlong line at spaces; giant tokens (blobs) by characters."""
    out: list[str] = []
    buf: list[str] = []
    words = chars = 0
    for tok in line.split():
        while len(tok) > chars_limit:
            if buf:
                out.append(" ".join(buf))
                buf, words, chars = [], 0, 0
            out.append(tok[:chars_limit])
            tok = tok[chars_limit:]
        if not tok:
            continue
        if buf and (words + 1 > words_limit or chars + len(tok) + 1 > chars_limit):
            out.append(" ".join(buf))
            buf, words, chars = [], 0, 0
        buf.append(tok)
        words += 1
        chars += len(tok) + 1
    if buf:
        out.append(" ".join(buf))
    return out


def _split_long_block(
    text: str, words_limit: int = CHUNK_WORDS, chars_limit: int = CHUNK_CHARS
) -> list[str]:
    """Split one oversized message at line breaks, spaces as a last resort."""
    pieces: list[str] = []
    current: list[str] = []
    words = chars = 0
    for line in text.split("\n"):
        lw, lc = len(line.split()), len(line) + 1
        if lw > words_limit or lc > chars_limit:
            if current:
                pieces.append("\n".join(current))
                current, words, chars = [], 0, 0
            pieces.extend(_split_line(line, words_limit, chars_limit))
            continue
        if current and (words + lw > words_limit or chars + lc > chars_limit):
            pieces.append("\n".join(current))
            current, words, chars = [], 0, 0
        current.append(line)
        words += lw
        chars += lc
    if current:
        pieces.append("\n".join(current))
    return pieces


def chunk_blocks(
    blocks: list[tuple[int, str]],
    words_limit: int = CHUNK_WORDS,
    chars_limit: int = CHUNK_CHARS,
) -> list[tuple[int | None, int | None, str]]:
    """Group ``(message index, text)`` blocks into ``(msg_from, msg_to, text)``.

    Cuts only between messages; a single message over either limit is
    split into pieces of its own. Always returns at least one (possibly
    empty) chunk, so a session without dialog text stays findable by
    title and project.
    """
    chunks: list[tuple[int | None, int | None, str]] = []
    parts: list[str] = []
    first: int | None = None
    last: int | None = None
    words = chars = 0

    def flush() -> None:
        nonlocal parts, first, last, words, chars
        if parts:
            chunks.append((first, last, CHUNK_SEPARATOR.join(parts)))
        parts, first, last, words, chars = [], None, None, 0, 0

    for idx, text in blocks:
        bw, bc = len(text.split()), len(text) + len(CHUNK_SEPARATOR)
        if bw > words_limit or bc > chars_limit:
            flush()
            chunks.extend(
                (idx, idx, piece) for piece in _split_long_block(text, words_limit, chars_limit)
            )
            continue
        if parts and (words + bw > words_limit or chars + bc > chars_limit):
            flush()
        if first is None:
            first = idx
        last = idx
        parts.append(text)
        words += bw
        chars += bc
    flush()
    return chunks or [(None, None, "")]


def make_chunk_documents(
    doc: dict[str, Any],
    tool_name: str,
    *,
    doc_key: str,
    archive_bytes: int | None = None,
    chunk_words: int = CHUNK_WORDS,
    chunk_chars: int = CHUNK_CHARS,
) -> list[dict[str, Any]]:
    """Build the index documents (one per chunk) for one OCF document."""
    base = _session_fields(doc, tool_name)
    blocks = simple_message_blocks(doc)
    base["session_words"] = sum(len(text.split()) for _, text in blocks)
    base["session_chars"] = sum(len(text) for _, text in blocks)
    if archive_bytes is not None:
        base["archive_bytes"] = archive_bytes
    messages = doc.get("messages") or []
    chunks = chunk_blocks(blocks, chunk_words, chunk_chars)
    out: list[dict[str, Any]] = []
    for n, (lo, hi, text) in enumerate(chunks, 1):
        entry = {
            **base,
            "id": _clean_id(f"{doc_key}__c{n}"),
            "doc": doc_key,
            "chunk": n,
            "chunks": len(chunks),
            "msg_from": lo,
            "msg_to": hi,
            "chunk_from": _ts(messages[lo].get("created_at")) if lo is not None else base["created_at"],
            "chunk_to": _ts(messages[hi].get("created_at")) if hi is not None else base["updated_at"],
            "words": len(text.split()),
            "content": text,
        }
        out.append(entry)
    return out


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
_PLATFORM_TOOLS = {"codex": "codex", "cursor": "cursor", "chatgpt": "chatgpt", "claude": "claude-ai"}


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
    archive_dir: Path, *, chunk_words: int = CHUNK_WORDS, chunk_chars: int = CHUNK_CHARS
) -> Iterator[tuple[Path, list[dict[str, Any]] | None, str | None]]:
    """Yield ``(ocf_path, chunk_docs, problem)`` for every archived OCF file.

    ``problem`` is ``None`` for an indexable document, ``"empty"`` for a
    document without messages (skipped, never indexed), or an error
    description (``chunk_docs`` is then ``None``).
    """
    archive_dir = Path(archive_dir)
    provenance = load_archive_provenance(archive_dir)
    for path in sorted(archive_dir.rglob("*" + OCF_SUFFIX)):
        try:
            raw = path.read_bytes()
            doc = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            yield path, None, f"unreadable: {exc}"
            continue
        if not doc.get("messages"):
            yield path, None, "empty"
            continue
        try:
            tool = archive_tool_name(doc, provenance.get(path.name))
            key = path.name[: -len(OCF_SUFFIX)]
            chunks = make_chunk_documents(
                doc, tool, doc_key=key, archive_bytes=len(raw),
                chunk_words=chunk_words, chunk_chars=chunk_chars,
            )
        except Exception as exc:  # noqa: BLE001
            yield path, None, f"render failed: {exc}"
            continue
        yield path, chunks, None


def iter_recovered_documents(
    recovered_dir: Path,
    *,
    skip_sessions: set[str] | frozenset[str] = frozenset(),
    chunk_words: int = CHUNK_WORDS,
    chunk_chars: int = CHUNK_CHARS,
) -> Iterator[list[dict[str, Any]]]:
    """Chunk documents saved verbatim from an earlier index (``*.meili.json``).

    For sessions whose source and OCF document no longer exist anywhere,
    the old index copy is the only content left. Its metadata is kept
    and its text split like any other session; ``doc`` is the session
    id, which is also the file name in the recovered folder. Sessions in
    ``skip_sessions`` (already covered by an archive document, e.g.
    because another machine still had the source) are left out.
    """
    for path in sorted(Path(recovered_dir).glob("*" + RECOVERED_SUFFIX)):
        old = json.loads(path.read_text(encoding="utf-8"))
        if not (isinstance(old, dict) and old.get("id") and old.get("content")):
            continue
        sid = str(old.get("session_id") or path.name[: -len(RECOVERED_SUFFIX)])
        if sid in skip_sessions:
            continue
        pieces = _split_long_block(old["content"], chunk_words, chunk_chars) or [""]
        created = int(old.get("created_at") or 0)
        base = {
            key: old[key] for key in ("tool", "title", "project", "model") if key in old
        }
        base.update(
            session_id=sid, doc=sid, created_at=created, created_date=_date(created),
            updated_at=created, updated_date=_date(created), session_messages=0,
            session_words=len(old["content"].split()), session_chars=len(old["content"]),
            recovered=True,
        )
        yield [
            {
                **base,
                "id": _clean_id(f"{sid}__c{n}"),
                "chunk": n,
                "chunks": len(pieces),
                "msg_from": None,
                "msg_to": None,
                "chunk_from": created,
                "chunk_to": created,
                "words": len(piece.split()),
                "content": piece,
            }
            for n, piece in enumerate(pieces, 1)
        ]


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


def enqueue_documents(index: Any, documents: list[dict[str, Any]]) -> int:
    """Enqueue one document batch without waiting; returns the task uid.

    Meilisearch merges consecutive enqueued additions into large internal
    batches, so enqueueing everything first and waiting once at the end
    (:func:`wait_for_tasks`) is far faster than waiting per batch.
    """
    return index.add_documents(documents).task_uid


def wait_for_tasks(client: Any, task_uids: list[int]) -> dict[int, str]:
    """Wait for enqueued tasks; return ``{task uid: error}`` for failed ones."""
    failed: dict[int, str] = {}
    for uid in task_uids:
        task = client.wait_for_task(uid, timeout_in_ms=_TASK_TIMEOUT_MS)
        if getattr(task, "status", "succeeded") != "succeeded":
            error = getattr(task, "error", None) or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            failed[uid] = f"{getattr(task, 'status', '?')}: {message or 'no details'}"
    return failed


def search(
    index: Any,
    query: str,
    *,
    tool: str | None = None,
    project: str | None = None,
    limit: int = 10,
    per_session: bool = True,
) -> list[dict[str, Any]]:
    """Search the index with optional facet filters.

    ``per_session`` returns only the best chunk of each session.
    """
    filters: list[str] = []
    if tool:
        filters.append(f'tool = "{tool}"')
    if project:
        filters.append(f'project = "{project}"')

    params: dict[str, Any] = {"limit": limit}
    if per_session:
        params["distinct"] = "doc"
    if filters:
        params["filter"] = " AND ".join(filters)

    result = index.search(query, params)
    return result.get("hits", [])
