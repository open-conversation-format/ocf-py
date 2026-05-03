"""Markdown renderer for OCF documents.

Output design (decided with the project owner):

- **YAML front-matter** with platform / model / project / created_at /
  source_id / tags. Lets Obsidian's Dataview index sessions, lets a
  later Meilisearch importer assign filterable facets, and gives a
  human a quick scan of what they're looking at.
- **No emojis.** Pure plain-text headings; section roles are spelled out.
- **Thinking blocks default-collapsed** in ``<details>`` so you can
  read the conversation without the model's internal monologue, and
  expand when you actually want to see how it got there.
- **Tool calls as fenced JSON blocks** so a Markdown renderer can
  syntax-highlight them and a downstream parser can lift them back
  out cleanly.
- **Code blocks fenced with the language tag** when OCF tells us the
  language; otherwise an unmarked fence.
- **Resource refs** render as a short pointer line (the resource
  table at the top has full details).

The renderer is a pure function of the OCF document — no I/O, no
filesystem dependence. Determinism matters because the runner stores
a hash of the output for skip-detection.
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, timezone
from typing import Any, ClassVar

from ocf.renderers._base import Renderer


# Display labels for the simple profile. Keys match
# conversation.source.platform values produced by our adapters.
_PLATFORM_DISPLAY: dict[str, str] = {
    "cursor": "Cursor",
    "claude_code": "Claude Code",
    "codex": "Codex",
    "claude": "Claude",
    "chatgpt": "ChatGPT",
}

_ASSISTANT_LABEL: dict[str, str] = {
    "cursor": "Cursor",
    "claude_code": "Claude",
    "codex": "Codex",
    "claude": "Claude",
    "chatgpt": "ChatGPT",
}


_FRONT_MATTER_KEYS_ORDER = (
    "ocf_version",
    "id",
    "title",
    "platform",
    "model",
    "project",
    "project_id",
    "source_id",
    "source_tool",
    "created_at",
    "updated_at",
    "message_count",
    "tool_call_count",
    "tags",
)


class MarkdownRenderer(Renderer):
    """Render an OCF document as a single Markdown string.

    Two profiles via the ``simple`` constructor flag:

    - **Default (full)**: YAML front matter, role headings with
      timestamps, collapsed thinking blocks, fenced tool calls,
      resources table. Optimized for Obsidian/Meilisearch indexing
      and forensics — every signal preserved.
    - **Simple** (``simple=True``): Cursor-native-export-style. Title,
      one italic export-line, then ``**User**`` / ``**<Assistant>**``
      blocks separated by ``---``. No tool calls, no thinking, no
      timestamps. Optimized for human reading.
    """

    name: ClassVar[str] = "markdown"
    suffix: ClassVar[str] = ".md"
    mime: ClassVar[str] = "text/markdown"

    def __init__(self, *, simple: bool = False) -> None:
        self.simple = simple

    def render(self, doc: dict[str, Any]) -> str:
        if self.simple:
            return self._render_simple(doc)
        return self._render_full(doc)

    # ------------------------------------------------------------------
    # Full profile (existing behavior)
    # ------------------------------------------------------------------

    def _render_full(self, doc: dict[str, Any]) -> str:
        buf = io.StringIO()
        conv = doc.get("conversation") or {}

        fm = _build_front_matter(doc)
        buf.write("---\n")
        for key in _FRONT_MATTER_KEYS_ORDER:
            if key not in fm:
                continue
            buf.write(_yaml_line(key, fm[key]))
        buf.write("---\n\n")

        title = conv.get("title") or "Untitled Conversation"
        buf.write(f"# {_escape_md_inline(title)}\n\n")
        buf.write(_meta_block(doc))
        buf.write("\n")

        resources = doc.get("resources") or []
        if resources:
            buf.write("## Resources\n\n")
            buf.write(_render_resources(resources))
            buf.write("\n")

        buf.write("## Messages\n\n")
        messages = doc.get("messages") or []
        if not messages:
            buf.write("_(no messages)_\n")
            return buf.getvalue()

        default_model = conv.get("default_model")
        for env in messages:
            buf.write(_render_envelope(env, default_model=default_model))
            buf.write("\n")

        return buf.getvalue()

    # ------------------------------------------------------------------
    # Simple profile (Cursor-native-export style)
    # ------------------------------------------------------------------

    def _render_simple(self, doc: dict[str, Any]) -> str:
        """Cursor-style minimalist rendering.

        Drops:
        - YAML front matter, metadata block, resources table
        - Per-message timestamps and model labels
        - Thinking blocks (model-internal monologue)
        - Tool calls and tool-result messages (only assistant narrative
          text remains, matching what Cursor's native export emits)
        - System/developer messages (these are setup boilerplate, not
          conversation content)

        Keeps:
        - Conversation title as H1
        - One italic "Exported on ..." line
        - User / Assistant blocks with the platform-flavored role label
          (``**Cursor**`` for cursor, ``**Claude**`` for claude_code,
          ``**Codex**`` for codex, ``**Assistant**`` otherwise)
        - Inline code fences and language tags
        """
        buf = io.StringIO()
        conv = doc.get("conversation") or {}
        source = conv.get("source") or {}

        # ----- Title -----
        title = conv.get("title") or "Untitled Conversation"
        buf.write(f"# {_escape_md_inline(title)}\n")

        # ----- Export tagline -----
        platform = source.get("platform") or "unknown"
        platform_label = _PLATFORM_DISPLAY.get(platform, platform)
        export_dt = datetime.now(tz=timezone.utc)
        buf.write(
            f"_Exported on {export_dt.strftime('%Y-%m-%d %H:%M UTC')} "
            f"from {platform_label} via ocf-py_\n\n"
        )

        # ----- Messages -----
        assistant_label = _ASSISTANT_LABEL.get(platform, "Assistant")
        messages = doc.get("messages") or []
        rendered_blocks: list[str] = []
        for env in messages:
            block = _simple_envelope(env, assistant_label=assistant_label)
            if block is not None:
                rendered_blocks.append(block)

        if not rendered_blocks:
            buf.write("---\n\n_(no conversation content)_\n")
            return buf.getvalue()

        for block in rendered_blocks:
            buf.write("---\n\n")
            buf.write(block)
            buf.write("\n")

        return buf.getvalue()


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------

def _build_front_matter(doc: dict[str, Any]) -> dict[str, Any]:
    """Assemble the YAML front-matter dict — kept as a plain dict so
    the order is fully under our control via ``_FRONT_MATTER_KEYS_ORDER``."""
    conv = doc.get("conversation") or {}
    source = conv.get("source") or {}
    project = conv.get("project") or {}
    fm: dict[str, Any] = {
        "ocf_version": doc.get("ocf_version"),
        "id": conv.get("id"),
    }
    if isinstance(conv.get("title"), str) and conv.get("title"):
        fm["title"] = conv["title"]
    if isinstance(source.get("platform"), str):
        fm["platform"] = source["platform"]
    if isinstance(conv.get("default_model"), str):
        fm["model"] = conv["default_model"]
    if isinstance(project.get("name"), str) and project.get("name"):
        fm["project"] = project["name"]
    if isinstance(project.get("id"), str):
        fm["project_id"] = project["id"]
    if isinstance(source.get("original_id"), str):
        fm["source_id"] = source["original_id"]
    if isinstance(source.get("export_tool"), str):
        fm["source_tool"] = source["export_tool"]
    if isinstance(conv.get("created_at"), str):
        fm["created_at"] = conv["created_at"]
    if isinstance(conv.get("updated_at"), str) and conv["updated_at"]:
        fm["updated_at"] = conv["updated_at"]

    messages = doc.get("messages") or []
    fm["message_count"] = len(messages)
    fm["tool_call_count"] = _count_tool_calls(messages)

    # Tags: combine explicit conversation.tags + auto-derived facets.
    tags: list[str] = []
    for t in conv.get("tags") or []:
        if isinstance(t, str) and t:
            tags.append(t)
    platform = source.get("platform")
    if isinstance(platform, str) and platform:
        tags.append(f"platform/{platform}")
    proj_name = project.get("name")
    if isinstance(proj_name, str) and proj_name:
        tags.append(f"project/{_slugify(proj_name)}")
    # de-dupe while preserving order
    seen: set[str] = set()
    unique_tags = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            unique_tags.append(t)
    if unique_tags:
        fm["tags"] = unique_tags
    return fm


def _count_tool_calls(messages: list[dict[str, Any]]) -> int:
    n = 0
    for env in messages:
        msg = env.get("message") or {}
        calls = msg.get("tool_calls")
        if isinstance(calls, list):
            n += len(calls)
    return n


def _yaml_line(key: str, value: Any) -> str:
    """Emit a single YAML scalar or list line.

    Scope is intentionally narrow: scalars and flat string-lists. We
    don't need a full YAML lib for what front matter covers, and
    pulling one in would inflate dependencies.
    """
    if isinstance(value, list):
        if not value:
            return f"{key}: []\n"
        body = "\n".join(f"  - {_yaml_scalar(v)}" for v in value)
        return f"{key}:\n{body}\n"
    return f"{key}: {_yaml_scalar(value)}\n"


_YAML_NEEDS_QUOTING = re.compile(r"[:\-#&*!|>%@`{}\[\],?]|^\s|\s$")


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if not s:
        return '""'
    if "\n" in s or _YAML_NEEDS_QUOTING.search(s) or s.lower() in (
        "yes",
        "no",
        "true",
        "false",
        "null",
        "~",
    ):
        # Single-quote: doubles inner single-quotes per YAML 1.2.
        return "'" + s.replace("'", "''") + "'"
    return s


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    out = _SLUG_RE.sub("-", s.lower()).strip("-")
    return out or "unnamed"


# ---------------------------------------------------------------------------
# Header meta block
# ---------------------------------------------------------------------------

def _meta_block(doc: dict[str, Any]) -> str:
    conv = doc.get("conversation") or {}
    source = conv.get("source") or {}
    project = conv.get("project") or {}
    parts: list[str] = []

    facts: list[str] = []
    if isinstance(source.get("platform"), str):
        facts.append(f"**Platform:** {source['platform']}")
    if isinstance(conv.get("default_model"), str):
        facts.append(f"**Model:** {conv['default_model']}")
    if isinstance(project.get("name"), str) and project.get("name"):
        facts.append(f"**Project:** {project['name']}")
    if facts:
        parts.append(" · ".join(facts) + "\n")

    times: list[str] = []
    if isinstance(conv.get("created_at"), str):
        times.append(f"**Created:** {_pretty_ts(conv['created_at'])}")
    if isinstance(conv.get("updated_at"), str) and conv["updated_at"]:
        times.append(f"**Updated:** {_pretty_ts(conv['updated_at'])}")
    if times:
        parts.append(" · ".join(times) + "\n")

    counts: list[str] = []
    msg_count = len(doc.get("messages") or [])
    counts.append(f"**Messages:** {msg_count}")
    tool_count = _count_tool_calls(doc.get("messages") or [])
    if tool_count:
        counts.append(f"**Tool calls:** {tool_count}")
    if isinstance(source.get("original_id"), str):
        counts.append(f"**Source:** `{source['original_id']}`")
    parts.append(" · ".join(counts) + "\n")
    parts.append("\n---\n")
    return "".join(parts)


def _pretty_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return dt.strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

def _render_resources(resources: list[dict[str, Any]]) -> str:
    out = io.StringIO()
    out.write("| ID | Kind | Filename / Title | Bytes |\n")
    out.write("|---|---|---|---|\n")
    for r in resources:
        rid = str(r.get("id") or "")
        kind = str(r.get("kind") or "")
        name = r.get("filename") or r.get("title") or ""
        size = r.get("byte_size")
        size_str = str(size) if isinstance(size, int) else ""
        out.write(
            f"| `{rid}` | {kind} | {_escape_md_inline(str(name))} | {size_str} |\n"
        )
    out.write("\n")
    return out.getvalue()


# ---------------------------------------------------------------------------
# Message envelopes
# ---------------------------------------------------------------------------

_ROLE_HEADINGS = {
    "system": "System",
    "developer": "Developer",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool result",
}


def _render_envelope(env: dict[str, Any], *, default_model: str | None) -> str:
    msg = env.get("message") or {}
    role = msg.get("role") or "unknown"
    heading = _ROLE_HEADINGS.get(role, role.capitalize())

    # Role-specific subline: timestamp [+ model on assistant, + status on tool]
    subline_parts: list[str] = []
    ts = env.get("created_at")
    if isinstance(ts, str) and ts:
        subline_parts.append(_pretty_ts(ts))
    if role == "assistant":
        model = env.get("model") or default_model
        if isinstance(model, str) and model:
            subline_parts.append(f"`{model}`")
    if role == "tool":
        status = env.get("status")
        if isinstance(status, str) and status:
            subline_parts.append(f"status: `{status}`")
        tool_call_id = msg.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            subline_parts.append(f"call: `{tool_call_id}`")

    out = io.StringIO()
    out.write(f"### {heading}\n")
    if subline_parts:
        out.write("_" + " · ".join(subline_parts) + "_\n\n")
    else:
        out.write("\n")

    # Body: content blocks (string or list)
    content = msg.get("content")
    if isinstance(content, str):
        out.write(content.rstrip() + "\n")
    elif isinstance(content, list):
        for block in content:
            out.write(_render_block(block))
    elif content is None:
        # Assistant tool-call-only messages have content=null but tool_calls.
        # That's handled below.
        pass

    # Tool calls (assistant-only)
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        out.write("\n")
        for call in tool_calls:
            out.write(_render_tool_call(call))

    return out.getvalue()


def _simple_envelope(
    env: dict[str, Any], *, assistant_label: str
) -> str | None:
    """Render one message envelope in the Cursor-native simple style.

    Returns ``None`` when the envelope produces no visible output —
    this is how we drop tool/tool-result/system/developer messages
    silently without leaving empty ``---`` separators in the output.
    """
    msg = env.get("message") or {}
    role = msg.get("role")

    # Filter: only user + assistant survive in the simple profile.
    # Skipping these matches Cursor's native export, which emits only
    # the natural-language stream of the conversation.
    if role not in ("user", "assistant"):
        return None

    content = msg.get("content")
    body_parts: list[str] = []
    if isinstance(content, str):
        if content.strip():
            body_parts.append(content.rstrip())
    elif isinstance(content, list):
        for block in content:
            text = _simple_block(block)
            if text:
                body_parts.append(text)

    # Assistant messages with no visible content (e.g. tool-call-only)
    # are dropped — there's nothing for a reader to see.
    if not body_parts:
        return None

    label = "User" if role == "user" else assistant_label
    body = "\n\n".join(body_parts).rstrip()
    return f"**{label}**\n\n{body}\n"


def _simple_block(block: Any) -> str | None:
    """Pick the text-bearing OCF blocks for the simple profile.

    Drops thinking, tool_use/result, image, file, audio, resource_ref —
    mirrors what Cursor's own export omits.
    """
    if not isinstance(block, dict):
        return None
    btype = block.get("type")
    if btype == "text":
        text = block.get("text") or ""
        return text.rstrip() or None
    if btype == "code":
        code = block.get("code") or ""
        lang = block.get("language") or ""
        # Filename hint goes inline before the fence so the export
        # remains greppable for "where was this code from".
        filename = block.get("filename")
        head = ""
        if isinstance(filename, str) and filename:
            head = f"_File: `{filename}`_\n\n"
        return f"{head}```{lang}\n{code.rstrip()}\n```"
    # thinking, image_url, input_audio, file, resource_ref → skip
    return None


def _render_block(block: Any) -> str:
    """Render one OCF content block to Markdown."""
    if not isinstance(block, dict):
        return ""
    btype = block.get("type")

    if btype == "text":
        text = block.get("text") or ""
        return text.rstrip() + "\n\n"

    if btype == "thinking":
        thinking = block.get("thinking") or ""
        # Default-collapsed details; keeps the conversation scannable
        # but lets a curious reader expand without losing data.
        return (
            "<details>\n"
            "<summary>Thinking</summary>\n\n"
            f"{thinking.rstrip()}\n\n"
            "</details>\n\n"
        )

    if btype == "code":
        code = block.get("code") or ""
        lang = block.get("language") or ""
        filename = block.get("filename")
        head = ""
        if isinstance(filename, str) and filename:
            head = f"_File: `{filename}`_\n\n"
        return f"{head}```{lang}\n{code.rstrip()}\n```\n\n"

    if btype == "image_url":
        url = (block.get("image_url") or {}).get("url") or ""
        return f"![image]({url})\n\n"

    if btype == "input_audio":
        fmt = (block.get("input_audio") or {}).get("format") or "audio"
        return f"_(inline {fmt} audio omitted from Markdown render)_\n\n"

    if btype == "file":
        fobj = block.get("file") or {}
        filename = fobj.get("filename") or fobj.get("file_id") or "(file)"
        return f"_File attachment: `{filename}`_\n\n"

    if btype == "resource_ref":
        rid = block.get("resource_id") or ""
        return f"_(resource: `{rid}` — see Resources table)_\n\n"

    # Unknown block type — don't crash; leave a footprint so issues
    # show up immediately when a new content type lands in OCF.
    return f"_(unrendered block: `{btype}`)_\n\n"


def _render_tool_call(call: dict[str, Any]) -> str:
    """Render an assistant ``tool_calls[]`` entry as a fenced JSON block.

    The fence uses ``json`` so a Markdown viewer syntax-highlights, and
    a downstream parser can extract the call cleanly.
    """
    if not isinstance(call, dict):
        return ""
    fn = call.get("function") or {}
    name = fn.get("name") or "unknown"
    call_id = call.get("id") or ""
    args_raw = fn.get("arguments")
    # Pretty-print arguments if they parse as JSON. Otherwise leave as-is.
    pretty_args = args_raw
    if isinstance(args_raw, str):
        try:
            parsed = json.loads(args_raw)
            pretty_args = json.dumps(parsed, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pretty_args = args_raw
    head = f"#### Tool call: `{name}`"
    if call_id:
        head += f" (`{call_id}`)"
    return f"{head}\n\n```json\n{pretty_args}\n```\n\n"


# ---------------------------------------------------------------------------
# Markdown escaping (intentionally minimal)
# ---------------------------------------------------------------------------

_MD_INLINE_ESCAPES = re.compile(r"([_*`])")


def _escape_md_inline(s: str) -> str:
    """Escape only the characters that would damage inline rendering
    in a heading / table cell. Leaves regular text alone — full
    Markdown escaping would over-quote and hurt readability.
    """
    return _MD_INLINE_ESCAPES.sub(r"\\\1", s)


__all__ = ["MarkdownRenderer"]
