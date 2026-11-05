"""Claude.ai (chat) -> OCF exporter.

Input format
------------
The official claude.ai data export (Settings > Privacy > Export data).
Since 2026-11 the export email links to a manifest with one ZIP per
category; ``conversations-NNN.zip`` holds ``conversations.json``, a list
of every conversation of the account (``uuid``, ``name``, ``summary``,
``created_at``, ``updated_at``, ``account``, ``chat_messages``). The
other ZIPs (``projects``, ``memories``, ``frames`` = artifacts,
``light_metadata``) are not conversations and are ignored.

Chats in the Desktop app's "Claude" mode live on Anthropic's servers
only; this export is the one complete source. There is no default
location: point ``--source-dir`` (or ``OCF_CLAUDE_AI_EXPORT_DIR``) at the
folder holding the export(s). Every export is a full snapshot, so the
same conversation appears in each one; the adapter exports every
conversation once, from the export with its newest ``updated_at``.
Keep old exports: a conversation deleted on claude.ai is missing from
later snapshots but stays exportable from the earlier ones.

Source tokens
-------------
``<container>::<conversation uuid>``, where the container is a
``conversations*.zip`` or an extracted ``conversations.json`` - the same
``::`` token style the ChatGPT and Cursor adapters use.

OCF mapping highlights
----------------------
- ``parent_message_uuid`` tree -> full tree with ``parent_id`` (all
  branches kept, chronological DFS order). The export has no "current
  leaf", so ``active_message_id`` points at the newest leaf.
- Assistant messages interleave ``thinking``, ``text``, ``tool_use`` and
  ``tool_result`` blocks (server-side tools run inside the turn). They
  are split into assistant envelopes with ``tool_calls`` and ``role:
  tool`` envelopes, in order.
- ``thinking`` is mostly hidden in the export; its ``summaries`` become
  the thinking text then.
- ``injected_prompt_block`` (date notes, memory, tool listings the
  client injects into the human turn) is dropped and counted.
- Images and documents are not part of the export (file pointers
  only); they become text placeholders. Attachments with
  ``extracted_content`` become OCF ``file`` blocks (text/plain), like
  attached documents in the Claude Code adapter.
- Search results (``knowledge``) become a Markdown list with title, URL
  and the excerpt the model saw.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

from ocf import __version__
from ocf.core.schema import validate_strict
from ocf.exporters._base import SessionInfo, SkipExport, SourceAdapter

TOKEN_SEP = "::"
ENV_EXPORT_DIR = "OCF_CLAUDE_AI_EXPORT_DIR"
CONVERSATIONS_MEMBER = "conversations.json"
ROOT_PARENT = "00000000-0000-4000-8000-000000000000"


class ClaudeAiAdapter(SourceAdapter):
    """SourceAdapter for the official claude.ai data export."""

    name: ClassVar[str] = "claude_ai"
    mapping_id: ClassVar[str] = "claude-ai-data-export-v1"
    rollout_glob: ClassVar[str] = "conversations*.zip"

    def default_source_dirs(self) -> list[Path]:
        raw = os.environ.get(ENV_EXPORT_DIR, "")
        return [Path(p) for p in raw.split(os.pathsep) if p.strip()]

    def discover(
        self, source_dirs: list[Path] | Path | None = None
    ) -> list[Path]:
        dirs = self._normalize_source_dirs(source_dirs)
        if not dirs:
            raise FileNotFoundError(
                "claude.ai chats are not stored locally; pass --source-dir with "
                "the folder holding your claude.ai export(s), or set "
                f"{ENV_EXPORT_DIR}"
            )
        best: dict[str, tuple[str, str, Path]] = {}
        for container in _containers(dirs):
            for uuid, conv in _conversations(container).items():
                rank = (str(conv.get("updated_at") or ""), str(container))
                if uuid not in best or rank > best[uuid][:2]:
                    best[uuid] = (*rank, container)
        return sorted(
            (Path(f"{container}{TOKEN_SEP}{uuid}") for uuid, (_, _, container) in best.items()),
            key=str,
        )

    def find_by_id(
        self,
        session_id: str,
        *,
        source_dirs: list[Path] | Path | None = None,
    ) -> list[Path]:
        sid = session_id.strip().lower()
        if not sid:
            return []
        return [s for s in self.discover(source_dirs) if _split_token(s)[1].lower().startswith(sid)]

    def export_one(
        self, source: Path, *, validate: bool = True
    ) -> dict[str, Any]:
        container, uuid = _split_token(source)
        conv = _conversations(container).get(uuid)
        if conv is None:
            raise ValueError(f"conversation {uuid} not found in {container}")
        return _convert_conversation(conv, source=container, validate=validate)

    def session_info(self, source: Path) -> SessionInfo:
        container, uuid = _split_token(source)
        conv = _conversations(container).get(uuid) or {}
        return SessionInfo(
            source=source,
            session_id=uuid,
            title=conv.get("name") or None,
            created_at=_parse_ts(conv.get("created_at")),
            model=None,
        )

    def source_fingerprint(self, source: Path) -> tuple[int, int, str]:
        container, uuid = _split_token(source)
        conv = _conversations(container).get(uuid) or {}
        raw = json.dumps(conv, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return (
            container.stat().st_mtime_ns,
            len(raw),
            hashlib.sha256(raw).hexdigest(),
        )

    def ocf_filename_for(self, source: Path) -> str:
        return f"claude-ai-{_split_token(source)[1]}.ocf.json"

    def _search_corpus(self, path: Path) -> str:
        info = self.session_info(path)
        return " ".join(filter(None, [info.session_id, info.title]))


# ---------------------------------------------------------------------------
# Source access
# ---------------------------------------------------------------------------

def _split_token(source: Path | str) -> tuple[Path, str]:
    s = str(source)
    idx = s.rfind(TOKEN_SEP)
    if idx < 0:
        raise ValueError(f"not a claude.ai source token: {source}")
    return Path(s[:idx]), s[idx + len(TOKEN_SEP):]


def _containers(dirs: list[Path]) -> list[Path]:
    found: list[Path] = []
    for d in dirs:
        if d.is_file():
            found.append(d)
            continue
        try:
            found.extend(sorted(d.rglob("conversations*.zip")))
            found.extend(sorted(d.rglob(CONVERSATIONS_MEMBER)))
        except OSError:
            continue
    return found


def _conversations(container: Path) -> dict[str, dict[str, Any]]:
    try:
        st = container.stat()
    except OSError:
        return {}
    return _load_container(str(container), st.st_mtime_ns, st.st_size)


@lru_cache(maxsize=8)
def _load_container(path: str, mtime_ns: int, size: int) -> dict[str, dict[str, Any]]:
    """Parse one export container once per run (keyed on mtime + size)."""
    container = Path(path)
    try:
        if container.suffix.lower() == ".zip":
            with zipfile.ZipFile(container) as zf:
                names = [n for n in zf.namelist() if n.rsplit("/", 1)[-1] == CONVERSATIONS_MEMBER]
                if not names:
                    return {}
                raw = zf.read(names[0])
        else:
            raw = container.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, list):
        return {}
    return {
        str(c["uuid"]): c
        for c in data
        if isinstance(c, dict) and c.get("uuid") and isinstance(c.get("chat_messages"), list)
    }


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def _convert_conversation(
    conv: dict[str, Any], *, source: Path, validate: bool
) -> dict[str, Any]:
    uuid = str(conv["uuid"])
    nodes = {
        str(m["uuid"]): m
        for m in conv.get("chat_messages") or []
        if isinstance(m, dict) and m.get("uuid")
    }

    messages: list[dict[str, Any]] = []
    anchor: dict[str, str | None] = {}
    known_calls: set[str] = set()
    skipped: dict[str, int] = defaultdict(int)

    for mid in _tree_order(nodes):
        msg = nodes[mid]
        parent = msg.get("parent_message_uuid")
        parent_anchor = anchor.get(parent) if isinstance(parent, str) else None
        envelopes = _convert_message(
            mid, msg, parent_anchor, known_calls=known_calls, skipped=skipped
        )
        messages.extend(envelopes)
        anchor[mid] = envelopes[-1]["id"] if envelopes else parent_anchor

    if not any(m["message"]["role"] in ("user", "assistant") for m in messages):
        raise SkipExport(f"conversation {uuid} has no user/assistant messages")

    meta: dict[str, Any] = {
        "conversation_id": uuid,
        "export_source": str(source),
        "raw_format": "claude-ai-data-export",
    }
    summary = conv.get("summary")
    if isinstance(summary, str) and summary.strip():
        meta["summary"] = summary
    if skipped:
        meta["skipped"] = dict(skipped)

    created = _parse_ts(conv.get("created_at")) or _first_ts(messages)
    conversation: dict[str, Any] = {
        "id": f"conv_claude_ai_{uuid}",
        "title": conv.get("name") or None,
        "created_at": _format_ts(created) or _now_iso(),
        "default_model": None,
        "source": {
            "platform": "claude",
            "export_tool": "ocf-py",
            "original_id": uuid,
            "original_url": f"https://claude.ai/chat/{uuid}",
        },
        "produced_by": {
            "tool": "ocf-py",
            "version": __version__,
            "mapping_id": ClaudeAiAdapter.mapping_id,
        },
        "meta": {"claude_ai": meta},
    }
    updated = _parse_ts(conv.get("updated_at"))
    if updated is not None:
        conversation["updated_at"] = _format_ts(updated)
    leaf = _newest_leaf(nodes)
    if leaf is not None and anchor.get(leaf):
        conversation["active_message_id"] = anchor[leaf]

    doc: dict[str, Any] = {
        "ocf_version": "0.1.0",
        "conversation": conversation,
        "messages": messages,
    }
    if validate:
        validate_strict(doc)
    return doc


def _tree_order(nodes: dict[str, dict[str, Any]]) -> list[str]:
    """DFS from the roots, children chronologically."""
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    for mid, msg in nodes.items():
        parent = msg.get("parent_message_uuid")
        if isinstance(parent, str) and parent in nodes and parent != mid:
            children[parent].append(mid)
        else:
            roots.append(mid)

    def key(mid: str) -> tuple[str, str]:
        return (str(nodes[mid].get("created_at") or ""), mid)

    order: list[str] = []
    seen: set[str] = set()
    stack = sorted(roots, key=key, reverse=True)
    while stack:
        mid = stack.pop()
        if mid in seen:
            continue
        seen.add(mid)
        order.append(mid)
        stack.extend(sorted(children.get(mid, []), key=key, reverse=True))
    order.extend(sorted((m for m in nodes if m not in seen), key=key))
    return order


def _newest_leaf(nodes: dict[str, dict[str, Any]]) -> str | None:
    parents = {m.get("parent_message_uuid") for m in nodes.values()}
    leaves = [mid for mid in nodes if mid not in parents]
    if not leaves:
        return None
    return max(leaves, key=lambda mid: (str(nodes[mid].get("created_at") or ""), mid))


def _convert_message(
    mid: str,
    msg: dict[str, Any],
    parent_anchor: str | None,
    *,
    known_calls: set[str],
    skipped: dict[str, int],
) -> list[dict[str, Any]]:
    sender = msg.get("sender")
    blocks = [b for b in msg.get("content") or [] if isinstance(b, dict)]
    files = {
        f.get("file_uuid"): f.get("file_name")
        for f in msg.get("files") or []
        if isinstance(f, dict)
    }
    if sender == "human":
        return _human_envelopes(mid, msg, blocks, files, parent_anchor, skipped)
    if sender == "assistant":
        return _assistant_envelopes(mid, msg, blocks, files, parent_anchor, known_calls, skipped)
    skipped[f"sender:{sender}"] += 1
    return []


def _human_envelopes(
    mid: str,
    msg: dict[str, Any],
    blocks: list[dict[str, Any]],
    files: dict[Any, Any],
    parent_anchor: str | None,
    skipped: dict[str, int],
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for b in blocks:
        btype = b.get("type")
        if btype == "text":
            if b.get("text"):
                parts.append({"type": "text", "text": b["text"]})
        elif btype == "injected_prompt_block":
            skipped["injected_prompt_block"] += 1
        elif btype in ("image", "document"):
            parts.append({"type": "text", "text": _file_placeholder(b, files)})
        else:
            skipped[f"block:{btype}"] += 1
    if not any(p["type"] == "text" for p in parts) and msg.get("text"):
        parts.insert(0, {"type": "text", "text": msg["text"]})
    for att in msg.get("attachments") or []:
        block = _attachment_block(att)
        if block is not None:
            parts.append(block)
    if not parts:
        skipped["empty"] += 1
        return []
    env = _envelope(mid, msg, parent_anchor)
    content: Any = parts[0]["text"] if len(parts) == 1 and parts[0]["type"] == "text" else parts
    env["message"] = {"role": "user", "content": content}
    return [env]


def _assistant_envelopes(
    mid: str,
    msg: dict[str, Any],
    blocks: list[dict[str, Any]],
    files: dict[Any, Any],
    parent_anchor: str | None,
    known_calls: set[str],
    skipped: dict[str, int],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    content: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    first_ts: str | None = None

    def next_id() -> str:
        return mid if not out else f"{mid}:{len(out)}"

    def parent() -> str | None:
        return out[-1]["id"] if out else parent_anchor

    def flush() -> None:
        nonlocal content, calls, first_ts
        if not content and not calls:
            return
        env = _envelope(next_id(), msg, parent(), first_ts)
        if len(content) == 1 and content[0]["type"] == "text":
            body: Any = content[0]["text"]
        else:
            body = content or None
        env["message"] = {"role": "assistant", "content": body}
        if calls:
            env["message"]["tool_calls"] = calls
        out.append(env)
        content, calls, first_ts = [], [], None

    for b in blocks:
        btype = b.get("type")
        if btype == "tool_result":
            flush()
            call_id = str(b.get("tool_use_id") or f"toolu_{mid}_{len(out)}")
            if call_id not in known_calls:
                calls.append(_tool_call(call_id, b.get("name") or "tool", "{}", synthesized=True))
                known_calls.add(call_id)
                flush()
            env = _envelope(next_id(), msg, parent(), b.get("start_timestamp"))
            env["message"] = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _tool_result_text(b, files) or "(no content)",
            }
            if b.get("name"):
                env["message"]["name"] = b["name"]
            if b.get("is_error"):
                env["status"] = "error"
            out.append(env)
            continue
        if first_ts is None:
            first_ts = b.get("start_timestamp")
        if btype == "text":
            if b.get("text"):
                content.append({"type": "text", "text": b["text"]})
        elif btype == "thinking":
            text = b.get("thinking") or "\n\n".join(
                s["summary"] for s in b.get("summaries") or []
                if isinstance(s, dict) and s.get("summary")
            )
            if text:
                content.append({"type": "thinking", "thinking": text})
            else:
                skipped["empty_thinking"] += 1
        elif btype == "tool_use":
            call_id = str(b.get("id") or f"toolu_{mid}_{len(calls)}")
            known_calls.add(call_id)
            calls.append(_tool_call(call_id, b.get("name") or "tool",
                                    json.dumps(b.get("input") or {}, ensure_ascii=False)))
        elif btype in ("image", "document"):
            content.append({"type": "text", "text": _file_placeholder(b, files)})
        else:
            skipped[f"block:{btype}"] += 1
    if not out and not content and not calls and msg.get("text"):
        content.append({"type": "text", "text": msg["text"]})
    flush()
    if not out:
        skipped["empty"] += 1
    return out


def _envelope(
    env_id: str, msg: dict[str, Any], parent_anchor: str | None, ts: Any = None
) -> dict[str, Any]:
    env: dict[str, Any] = {
        "id": env_id,
        "id_origin": "source" if env_id == msg.get("uuid") else "synthesized",
        "message": {},
    }
    if parent_anchor:
        env["parent_id"] = parent_anchor
    stamp = _parse_ts(ts) or _parse_ts(msg.get("created_at"))
    if stamp is not None:
        env["created_at"] = _format_ts(stamp)
    return env


def _tool_call(call_id: str, name: str, arguments: str, *, synthesized: bool = False) -> dict[str, Any]:
    return {
        "id": call_id,
        "id_origin": "synthesized" if synthesized else "source",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _file_placeholder(block: dict[str, Any], files: dict[Any, Any]) -> str:
    kind = "Image" if block.get("type") == "image" else "Document"
    name = block.get("title") or files.get(block.get("file_uuid"))
    return f"[{kind}: {name}]" if name else f"[{kind}]"


def _attachment_block(att: Any) -> dict[str, Any] | None:
    if not isinstance(att, dict) or not att.get("extracted_content"):
        return None
    data = base64.b64encode(str(att["extracted_content"]).encode("utf-8")).decode("ascii")
    file_obj: dict[str, Any] = {"file_data": f"data:text/plain;base64,{data}"}
    if att.get("file_name"):
        file_obj["filename"] = str(att["file_name"])
    return {"type": "file", "file": file_obj}


def _tool_result_text(block: dict[str, Any], files: dict[Any, Any]) -> str:
    pieces: list[str] = []
    for item in block.get("content") or []:
        if isinstance(item, str):
            pieces.append(item)
            continue
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "text" and item.get("text"):
            pieces.append(item["text"])
        elif itype == "knowledge":
            title = item.get("title") or item.get("url") or "result"
            head = f"- [{title}]({item['url']})" if item.get("url") else f"- {title}"
            body = (item.get("text") or "").strip()
            pieces.append(f"{head}\n  {body}" if body else head)
        elif itype == "image":
            pieces.append(_file_placeholder(item, files))
        else:
            pieces.append(f"[{itype}]")
    if not pieces and isinstance(block.get("message"), str):
        pieces.append(block["message"])
    return "\n".join(pieces)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _format_ts(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_ts(messages: list[dict[str, Any]]) -> datetime | None:
    stamps = [m["created_at"] for m in messages if m.get("created_at")]
    return _parse_ts(min(stamps)) if stamps else None


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["ClaudeAiAdapter", "ENV_EXPORT_DIR"]
