"""ChatGPT -> OCF exporter.

Input format
------------
One ChatGPT conversation as returned by the web client's internal API
(``chatgpt.com/backend-api/conversation/<id>``). This is what
pionxzh/chatgpt-exporter writes in its "JSON" format (one
``ChatGPT-<title>.json`` per conversation, usually bundled in ZIPs),
and the same object shape as the entries of OpenAI's official
``conversations.json``. The official multi-conversation files are not
supported yet; a single conversation wrapped in a one-element list is.

ChatGPT keeps no local history, so there is no default storage
location: point ``--source-dir`` (or ``OCF_CHATGPT_EXPORT_DIR``) at a
folder containing the JSON files and/or the export ZIPs. ZIPs are read
in place - extracting several of them into one folder silently loses
conversations, because different chats share filenames
(``ChatGPT-New_chat.json``).

Source tokens
-------------
A JSON file is a regular path. A ZIP member is encoded as
``<zip path>::<member name>``, the same ``::`` token style the Cursor
adapter uses.

OCF mapping highlights
----------------------
- ``mapping`` tree -> full tree with ``parent_id`` (all branches kept,
  chronological DFS order), ``current_node`` -> ``active_message_id``.
  Links are rebuilt from ``parent`` only, because the October 2026
  official export dropped ``children``.
- Assistant message with ``recipient`` != ``all`` -> ``tool_calls[]``.
  The following ``tool`` message answers it. Tool messages without an
  explicit call (memory/context injections, image generation) get a
  synthesized assistant tool call so the pair stays schema-valid.
- ``thoughts`` -> ``thinking`` blocks; ``reasoning_recap`` is dropped
  (its duration is already in ``metadata.finished_duration_sec``).
- Citation markers in assistant text are replaced with the ``alt``
  text of ``metadata.content_references``; leftover private-use
  marker sequences are removed.
- Images and audio are not part of the export (file-service pointers)
  or only allowed for the user role in OCF; they become text
  placeholders, audio transcriptions become text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from ocf import __version__
from ocf.core.schema import validate_strict
from ocf.exporters._base import SessionInfo, SkipExport, SourceAdapter

ZIP_SEP = "::"
ENV_EXPORT_DIR = "OCF_CHATGPT_EXPORT_DIR"
_HEAD_BYTES = 4096

_SKIP_CONTENT_TYPES = frozenset(
    {"reasoning_recap", "user_editable_context", "model_editable_context"}
)
_DROPPED_PART_TYPES = frozenset(
    {
        "audio_asset_pointer",
        "real_time_user_audio_video_asset_pointer",
        "video_container_asset_pointer",
    }
)

# Private-use characters ChatGPT wraps around inline citation / entity
# markers ( start,  end,  separator, ...).
_PUA_SEQUENCE = re.compile("[^]*")
_PUA_CHAR = re.compile("[-]")
_HEAD_FIELDS = {
    key: re.compile(rf'"{key}"\s*:\s*("(?:[^"\\]|\\.)*"|[-0-9.eE]+|null)')
    for key in ("id", "title", "create_time", "default_model_slug")
}


class ChatGPTAdapter(SourceAdapter):
    """SourceAdapter for ChatGPT conversation JSON exports."""

    name: ClassVar[str] = "chatgpt"
    mapping_id: ClassVar[str] = "chatgpt-conversation-json-v1"
    rollout_glob: ClassVar[str] = "*.json"

    def default_source_dirs(self) -> list[Path]:
        raw = os.environ.get(ENV_EXPORT_DIR, "")
        return [Path(p) for p in raw.split(os.pathsep) if p.strip()]

    def discover(
        self, source_dirs: list[Path] | Path | None = None
    ) -> list[Path]:
        dirs = self._normalize_source_dirs(source_dirs)
        if not dirs:
            raise FileNotFoundError(
                "ChatGPT has no local storage; pass --source-dir with the "
                f"folder holding your export JSON/ZIP files, or set "
                f"{ENV_EXPORT_DIR}"
            )
        found: list[Path] = []
        for d in dirs:
            if d.is_file():
                candidates = [d]
            else:
                try:
                    candidates = sorted(d.rglob("*.json")) + sorted(d.rglob("*.zip"))
                except OSError:
                    continue
            for path in candidates:
                suffix = path.suffix.lower()
                if suffix == ".zip":
                    found.extend(_zip_conversation_members(path))
                elif suffix == ".json" and _looks_like_conversation(_read_head(path)):
                    found.append(path)
        return found

    def resolve_sources(
        self,
        source: Path | str | None,
        *,
        source_dirs: list[Path] | Path | None = None,
        case_sensitive: bool = False,
    ) -> list[Path]:
        if source is not None:
            candidate = Path(source)
            if candidate.is_file() and candidate.suffix.lower() == ".zip":
                return _zip_conversation_members(candidate)
        return super().resolve_sources(
            source, source_dirs=source_dirs, case_sensitive=case_sensitive
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
        return [
            s for s in self.discover(source_dirs)
            if (_peek_fields(s).get("id") or "").lower() == sid
        ]

    def export_one(
        self, source: Path, *, validate: bool = True
    ) -> dict[str, Any]:
        conv, decode_errors = _load_conversation(source)
        return _convert_conversation(
            conv, source=source, decode_errors=decode_errors, validate=validate
        )

    def session_info(self, source: Path) -> SessionInfo:
        fields = _peek_fields(source)
        created = fields.get("create_time")
        return SessionInfo(
            source=source,
            session_id=fields.get("id") or Path(_member_or_name(source)).stem,
            title=fields.get("title"),
            created_at=_parse_ts(created) if created else None,
            model=fields.get("default_model_slug"),
        )

    def source_fingerprint(self, source: Path) -> tuple[int, int, str]:
        token = _split_zip_token(source)
        if token is None:
            return super().source_fingerprint(source)
        zip_path, member = token
        with zipfile.ZipFile(zip_path) as zf:
            info = zf.getinfo(member)
        return (
            zip_path.stat().st_mtime_ns,
            info.file_size,
            f"zip-crc32:{info.CRC:08x}:{info.file_size}",
        )

    def ocf_filename_for(self, source: Path) -> str:
        conv_id = _peek_fields(source).get("id")
        if conv_id and re.fullmatch(r"[A-Za-z0-9_-]+", conv_id):
            return f"chatgpt-{conv_id}.ocf.json"
        stem = Path(_member_or_name(source)).stem
        digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", stem)[:80]
        return f"chatgpt-{safe}-{digest}.ocf.json"

    def _search_corpus(self, path: Path) -> str:
        return " ".join(
            filter(None, [_member_or_name(path), _peek_fields(path).get("title")])
        )


# ---------------------------------------------------------------------------
# Source access (plain JSON files and ZIP members)
# ---------------------------------------------------------------------------

def _split_zip_token(source: Path | str) -> tuple[Path, str] | None:
    s = str(source)
    idx = s.find(ZIP_SEP)
    if idx < 0:
        return None
    # Path() turns "/" into "\" on Windows; ZIP member names use "/".
    return Path(s[:idx]), s[idx + len(ZIP_SEP):].replace("\\", "/")


def _member_or_name(source: Path | str) -> str:
    token = _split_zip_token(source)
    return token[1].rsplit("/", 1)[-1] if token else Path(source).name


def _zip_conversation_members(zip_path: Path) -> list[Path]:
    members: list[Path] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".json"):
                    continue
                with zf.open(info) as fh:
                    head = fh.read(_HEAD_BYTES).decode("utf-8", "replace")
                if _looks_like_conversation(head):
                    members.append(Path(f"{zip_path}{ZIP_SEP}{info.filename}"))
    except (zipfile.BadZipFile, OSError):
        return []
    return members


def _read_bytes(source: Path | str, limit: int | None = None) -> bytes:
    token = _split_zip_token(source)
    if token is None:
        with open(source, "rb") as fh:
            return fh.read() if limit is None else fh.read(limit)
    zip_path, member = token
    with zipfile.ZipFile(zip_path) as zf, zf.open(member) as fh:
        return fh.read() if limit is None else fh.read(limit)


def _read_head(source: Path | str) -> str:
    try:
        return _read_bytes(source, _HEAD_BYTES).decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return ""


def _looks_like_conversation(head: str) -> bool:
    stripped = head.lstrip("﻿ \t\r\n")
    return (
        stripped[:1] in ("{", "[")
        and '"create_time"' in head
        and '"title"' in head
    )


def _peek_fields(source: Path | str) -> dict[str, Any]:
    """Cheap metadata peek: these keys come first in the API response."""
    head = _read_head(source)
    out: dict[str, Any] = {}
    for key, rx in _HEAD_FIELDS.items():
        m = rx.search(head)
        if not m:
            continue
        try:
            value = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if value is not None:
            out[key] = value
    return out


def _load_conversation(source: Path | str) -> tuple[dict[str, Any], bool]:
    raw = _read_bytes(source)
    decode_errors = False
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-8-sig", errors="replace")
        decode_errors = True
    data = json.loads(text)
    if isinstance(data, list):
        convs = [c for c in data if isinstance(c, dict) and c]
        if len(convs) != 1:
            raise ValueError(
                f"{_member_or_name(source)} holds {len(convs)} conversations; "
                "multi-conversation files are not supported yet"
            )
        data = convs[0]
    if not isinstance(data, dict) or not isinstance(data.get("mapping"), dict):
        raise ValueError(f"{_member_or_name(source)} is not a ChatGPT conversation")
    return data, decode_errors


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def _convert_conversation(
    conv: dict[str, Any],
    *,
    source: Path | str,
    decode_errors: bool,
    validate: bool,
) -> dict[str, Any]:
    conv_id = str(conv.get("id") or conv.get("conversation_id") or "") or (
        hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
    )
    nodes = {
        nid: n for nid, n in conv["mapping"].items()
        if isinstance(n, dict) and n
    }

    messages: list[dict[str, Any]] = []
    anchor: dict[str, str | None] = {}
    call_by_node: dict[str, str] = {}
    used_calls: set[str] = set()
    skipped: dict[str, int] = defaultdict(int)

    for nid in _tree_order(nodes):
        node = nodes[nid]
        parent = node.get("parent")
        parent_anchor = anchor.get(parent) if isinstance(parent, str) else None
        envelopes = _convert_node(
            nid, node, parent_anchor,
            call_by_node=call_by_node, used_calls=used_calls, skipped=skipped,
        )
        if envelopes:
            messages.extend(envelopes)
            anchor[nid] = envelopes[-1]["id"]
        else:
            anchor[nid] = parent_anchor

    if not any(m["message"]["role"] in ("user", "assistant") for m in messages):
        raise SkipExport(f"conversation {conv_id} has no user/assistant messages")

    created = _parse_ts(conv.get("create_time")) or _first_ts(messages)
    default_model = conv.get("default_model_slug") or next(
        (m["model"] for m in messages if m.get("model")), None
    )
    current = conv.get("current_node")

    meta: dict[str, Any] = {
        "conversation_id": conv_id,
        "export_source": str(source),
        "raw_format": "chatgpt-backend-api-conversation",
    }
    for key in (
        "gizmo_id", "gizmo_type", "is_archived", "is_starred",
        "is_temporary_chat", "conversation_origin", "memory_scope",
    ):
        if conv.get(key) is not None:
            meta[key] = conv[key]
    if decode_errors:
        meta["decode_errors"] = True
    if skipped:
        meta["skipped"] = dict(skipped)

    conversation: dict[str, Any] = {
        "id": f"conv_chatgpt_{conv_id}",
        "title": conv.get("title") if isinstance(conv.get("title"), str) else None,
        "created_at": _format_ts(created) or _now_iso(),
        "default_model": default_model,
        "source": {
            "platform": "chatgpt",
            "export_tool": "ocf-py",
            "original_id": conv_id,
            "original_url": f"https://chatgpt.com/c/{conv_id}",
        },
        "produced_by": {
            "tool": "ocf-py",
            "version": __version__,
            "mapping_id": ChatGPTAdapter.mapping_id,
        },
        "meta": {"chatgpt": meta},
    }
    updated = _parse_ts(conv.get("update_time"))
    if updated is not None:
        conversation["updated_at"] = _format_ts(updated)
    if isinstance(current, str) and anchor.get(current):
        conversation["active_message_id"] = anchor[current]

    doc: dict[str, Any] = {
        "ocf_version": "0.1.0",
        "conversation": conversation,
        "messages": messages,
    }
    if validate:
        validate_strict(doc)
    return doc


def _tree_order(nodes: dict[str, dict[str, Any]]) -> list[str]:
    """DFS from the roots, children chronologically; cycles appended last."""
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    for nid, node in nodes.items():
        parent = node.get("parent")
        if isinstance(parent, str) and parent in nodes and parent != nid:
            children[parent].append(nid)
        else:
            roots.append(nid)

    def key(nid: str) -> tuple[float, str]:
        msg = nodes[nid].get("message") or {}
        ts = msg.get("create_time")
        return (float(ts) if isinstance(ts, (int, float)) else 0.0, nid)

    order: list[str] = []
    seen: set[str] = set()
    stack = sorted(roots, key=key, reverse=True)
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        order.append(nid)
        stack.extend(sorted(children.get(nid, []), key=key, reverse=True))
    order.extend(sorted((n for n in nodes if n not in seen), key=key))
    return order


def _convert_node(
    nid: str,
    node: dict[str, Any],
    parent_anchor: str | None,
    *,
    call_by_node: dict[str, str],
    used_calls: set[str],
    skipped: dict[str, int],
) -> list[dict[str, Any]]:
    msg = node.get("message")
    if not isinstance(msg, dict):
        return []
    author = msg.get("author") or {}
    role = author.get("role")
    content = msg.get("content") or {}
    ctype = content.get("content_type")
    md = msg.get("metadata") or {}

    if md.get("is_visually_hidden_from_conversation"):
        skipped["hidden"] += 1
        return []
    if ctype in _SKIP_CONTENT_TYPES:
        skipped[ctype] += 1
        return []

    msg_id = str(msg.get("id") or nid)
    envelope = _base_envelope(msg_id, msg, md, parent_anchor)
    chatgpt_meta = envelope["meta"]["chatgpt"]

    if role in ("system", "developer"):
        text = _parts_text(content)
        if not md.get("is_user_system_message") or not text:
            skipped["system"] += 1
            return []
        envelope["message"] = {"role": "system", "content": text}
        return [envelope]

    if role == "user":
        text = _parts_text(content)
        if not text:
            skipped["empty"] += 1
            return []
        envelope["message"] = {"role": "user", "content": text}
        return [envelope]

    if role == "assistant":
        if md.get("model_slug"):
            envelope["model"] = md["model_slug"]
        recipient = msg.get("recipient") or "all"
        if ctype == "thoughts":
            blocks = _thinking_blocks(content)
            if not blocks:
                skipped["empty"] += 1
                return []
            envelope["message"] = {"role": "assistant", "content": blocks}
            return [envelope]
        if recipient != "all" and ctype in ("text", "code"):
            args = content.get("text") if ctype == "code" else _parts_text(content)
            call_id = f"call_{msg_id}"
            call_by_node[nid] = call_id
            chatgpt_meta["recipient"] = recipient
            envelope["message"] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call(call_id, recipient, args or "")],
            }
            return [envelope]
        if ctype == "code":
            code = content.get("text") or ""
            if not code.strip():
                skipped["empty"] += 1
                return []
            block: dict[str, Any] = {"type": "code", "code": code}
            if content.get("language") not in (None, "", "unknown"):
                block["language"] = str(content["language"]).lower()
            envelope["message"] = {"role": "assistant", "content": [block]}
            return [envelope]
        text = _strip_markers(
            _apply_content_references(_parts_text(content), md.get("content_references"))
        )
        if not text.strip():
            skipped["empty"] += 1
            return []
        envelope["message"] = {"role": "assistant", "content": text}
        return [envelope]

    if role == "tool":
        text = _tool_text(content) or _tool_metadata_text(md)
        if not text.strip():
            skipped["empty_tool"] += 1
            return []
        tool_name = author.get("name") or "tool"
        envelope["message"] = {"role": "tool", "content": text, "name": tool_name}
        if ctype == "system_error":
            envelope["status"] = "error"
        parent = node.get("parent")
        call_id = call_by_node.get(parent) if isinstance(parent, str) else None
        if call_id and call_id not in used_calls:
            used_calls.add(call_id)
            envelope["message"]["tool_call_id"] = call_id
            return [envelope]
        # Output without an explicit call: synthesize the call it answers.
        call_id = f"call_{msg_id}"
        synth: dict[str, Any] = {
            "id": f"{msg_id}-call",
            "id_origin": "synthesized",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call(call_id, tool_name, "{}")],
            },
            "meta": {"chatgpt": {"synthesized_tool_call": True}},
        }
        if parent_anchor:
            synth["parent_id"] = parent_anchor
        if "created_at" in envelope:
            synth["created_at"] = envelope["created_at"]
        used_calls.add(call_id)
        envelope["message"]["tool_call_id"] = call_id
        envelope["parent_id"] = synth["id"]
        return [synth, envelope]

    skipped[f"role:{role}"] += 1
    return []


def _base_envelope(
    msg_id: str,
    msg: dict[str, Any],
    md: dict[str, Any],
    parent_anchor: str | None,
) -> dict[str, Any]:
    content = msg.get("content") or {}
    chatgpt_meta: dict[str, Any] = {"content_type": content.get("content_type")}
    if msg.get("status") not in (None, "finished_successfully"):
        chatgpt_meta["status"] = msg["status"]
    envelope: dict[str, Any] = {
        "id": msg_id,
        "id_origin": "source",
        "message": {},
        "meta": {"chatgpt": chatgpt_meta},
    }
    if parent_anchor:
        envelope["parent_id"] = parent_anchor
    ts = _parse_ts(msg.get("create_time"))
    if ts is not None:
        envelope["created_at"] = _format_ts(ts)
    duration = md.get("finished_duration_sec")
    if isinstance(duration, (int, float)) and duration >= 0:
        envelope["duration_ms"] = int(duration * 1000)
    return envelope


def _tool_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "id_origin": "synthesized",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _parts_text(content: dict[str, Any]) -> str:
    """Text of ``parts`` (text / multimodal_text), with media placeholders."""
    pieces: list[str] = []
    for part in content.get("parts") or []:
        if isinstance(part, str):
            if part:
                pieces.append(part)
        elif isinstance(part, dict):
            ptype = part.get("content_type")
            if ptype == "image_asset_pointer":
                pieces.append(_image_placeholder(part))
            elif ptype in _DROPPED_PART_TYPES:
                continue
            elif isinstance(part.get("text"), str) and part["text"]:
                pieces.append(part["text"])
    return "\n\n".join(pieces)


def _image_placeholder(part: dict[str, Any]) -> str:
    pointer = part.get("asset_pointer") or ""
    if pointer.startswith("data:"):
        pointer = "inline data"
    size = ""
    if part.get("width") and part.get("height"):
        size = f" {part['width']}x{part['height']}"
    return f"[Image{size}: {pointer}]" if pointer else f"[Image{size}]"


def _thinking_blocks(content: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for thought in content.get("thoughts") or []:
        if not isinstance(thought, dict):
            continue
        summary = (thought.get("summary") or "").strip()
        body = (thought.get("content") or "").strip()
        text = f"{summary}\n\n{body}" if summary and body else summary or body
        if text:
            blocks.append({"type": "thinking", "thinking": text})
    return blocks


def _tool_text(content: dict[str, Any]) -> str:
    ctype = content.get("content_type")
    if ctype in ("code", "execution_output"):
        return content.get("text") or ""
    if ctype == "tether_browsing_display":
        parts = [content.get("result") or "", content.get("summary") or ""]
        return "\n\n".join(p for p in parts if p)
    if ctype == "tether_quote":
        head = "\n".join(p for p in (content.get("title"), content.get("url")) if p)
        return "\n\n".join(p for p in (head, content.get("text") or "") if p)
    if ctype == "computer_output":
        state = content.get("state") or {}
        detail = " ".join(p for p in (state.get("title"), state.get("url")) if p)
        return f"[Screenshot] {detail}".strip()
    if ctype == "system_error":
        return f"{content.get('name') or 'error'}: {content.get('text') or ''}".strip()
    if ctype == "citable_code_output":
        return content.get("output_str") or ""
    return _parts_text(content) or content.get("text") or ""


def _tool_metadata_text(md: dict[str, Any]) -> str:
    """Tool results whose payload lives in metadata, not in ``content``.

    ``web.run`` leaves ``parts`` empty and puts the hits into
    ``search_result_groups``; deep-research / async tasks only carry
    their title and prompt in ``async_task_*``.
    """
    lines: list[str] = []
    for group in md.get("search_result_groups") or []:
        if not isinstance(group, dict):
            continue
        for entry in group.get("entries") or []:
            if not isinstance(entry, dict) or not entry.get("url"):
                continue
            title = entry.get("title") or entry["url"]
            lines.append(f"- [{title}]({entry['url']})")
            if entry.get("snippet"):
                lines.append(f"  {entry['snippet']}")
    if lines:
        return "\n".join(lines)
    if md.get("async_task_title"):
        kind = md.get("async_task_type") or "task"
        parts = [f"[{kind}] {md['async_task_title']}", md.get("async_task_prompt") or ""]
        return "\n\n".join(p for p in parts if p)
    return ""


def _apply_content_references(text: str, refs: Any) -> str:
    """Replace citation markers with their ``alt`` rendering.

    References without ``alt`` only remove marker sequences (text with
    private-use characters); plain-prose ``matched_text`` stays as is.
    """
    if not text or not isinstance(refs, list):
        return text
    items = [r for r in refs if isinstance(r, dict) and r.get("matched_text")]
    items.sort(key=lambda r: r.get("start_idx") if isinstance(r.get("start_idx"), int) else -1,
               reverse=True)
    for ref in items:
        matched = ref["matched_text"]
        alt = ref.get("alt")
        if isinstance(alt, str) and alt:
            replacement = alt
        elif _PUA_CHAR.search(matched):
            replacement = ""
        else:
            continue
        start, end = ref.get("start_idx"), ref.get("end_idx")
        if (
            isinstance(start, int) and isinstance(end, int)
            and text[start:end] == matched
        ):
            text = text[:start] + replacement + text[end:]
        else:
            text = text.replace(matched, replacement, 1)
    return text


def _strip_markers(text: str) -> str:
    return _PUA_CHAR.sub("", _PUA_SEQUENCE.sub("", text))


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _format_ts(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_ts(messages: list[dict[str, Any]]) -> datetime | None:
    stamps = [m["created_at"] for m in messages if m.get("created_at")]
    if not stamps:
        return None
    return datetime.strptime(min(stamps), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["ChatGPTAdapter", "ENV_EXPORT_DIR"]
