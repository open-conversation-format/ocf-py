"""Tests for the Markdown renderer.

Two layers, mirroring the exporters:

1. Pure ``render(doc) -> str`` against synthetic OCF documents that
   exercise every content-block type and every assistant/tool shape.
2. Bulk ``render_all`` runner with manifest + skip detection +
   atomic write.

The MarkdownRenderer's contract (front-matter, no emojis,
collapsed thinking, fenced tool calls) is covered by individual
substring assertions so that a regression on any one of those
formatting rules surfaces immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ocf.renderers import RENDERERS, MarkdownRenderer, render_all, select_ocf_files


# ---------------------------------------------------------------------------
# Synthetic OCF docs
# ---------------------------------------------------------------------------

def _minimal_doc(**overrides: Any) -> dict[str, Any]:
    """Smallest schema-valid OCF doc; tests bolt extra fields on top."""
    base: dict[str, Any] = {
        "ocf_version": "0.1.0",
        "conversation": {
            "id": "conv_test_001",
            "title": "Test Conversation",
            "created_at": "2026-04-26T10:00:00Z",
            "default_model": "claude-sonnet-4-5",
            "source": {
                "platform": "cursor",
                "export_tool": "ocf-py",
                "original_id": "composer:abc-123",
            },
            "project": {
                "id": "proj_demo",
                "name": "Demo Project",
                "description": "/home/user/demo",
            },
        },
        "messages": [
            {
                "id": "msg_0001",
                "created_at": "2026-04-26T10:00:01Z",
                "message": {"role": "user", "content": "Hello, world!"},
            },
            {
                "id": "msg_0002",
                "created_at": "2026-04-26T10:00:02Z",
                "model": "claude-sonnet-4-5",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi back!"}],
                },
            },
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# render() — front matter
# ---------------------------------------------------------------------------

def test_render_starts_with_yaml_front_matter() -> None:
    out = MarkdownRenderer().render(_minimal_doc())
    assert out.startswith("---\n")
    fm_end = out.index("---\n", 4)
    fm = out[4:fm_end]
    # Required keys present
    for key in ("ocf_version:", "id:", "title:", "platform:", "model:"):
        assert key in fm, f"front matter missing key {key}: {fm}"


def test_front_matter_contains_message_count() -> None:
    out = MarkdownRenderer().render(_minimal_doc())
    assert "message_count: 2" in out


def test_front_matter_includes_derived_tags() -> None:
    out = MarkdownRenderer().render(_minimal_doc())
    # platform/cursor + project/demo-project tags auto-derived
    assert "platform/cursor" in out
    assert "project/demo-project" in out


def test_front_matter_quotes_strings_with_colons() -> None:
    """YAML scalars containing ':' (very common — source IDs!) must be
    single-quoted so the front matter remains parseable."""
    doc = _minimal_doc()
    out = MarkdownRenderer().render(doc)
    # source.original_id is "composer:abc-123" — colon inside
    assert "source_id: 'composer:abc-123'" in out


def test_front_matter_handles_missing_optional_fields() -> None:
    """A doc without title/project/model still renders — front matter
    just omits those keys."""
    doc = {
        "ocf_version": "0.1.0",
        "conversation": {
            "id": "conv_minimal",
            "created_at": "2026-04-26T10:00:00Z",
            "source": {"platform": "codex"},
        },
        "messages": [],
    }
    out = MarkdownRenderer().render(doc)
    assert "ocf_version: 0.1.0" in out
    assert "id: conv_minimal" in out
    assert "title:" not in out
    assert "model:" not in out
    assert "project:" not in out


# ---------------------------------------------------------------------------
# render() — body / no emojis / role headings
# ---------------------------------------------------------------------------

def test_render_no_emojis_anywhere() -> None:
    """Project policy: no emojis in rendered output."""
    out = MarkdownRenderer().render(_minimal_doc())
    # Common chat-emoji codepoints
    for emoji in ("👤", "🤖", "🔧", "↩️", "💭", "📝", "✅", "❌"):
        assert emoji not in out, f"unexpected emoji {emoji!r} in output"


def test_render_uses_role_headings_not_emojis() -> None:
    out = MarkdownRenderer().render(_minimal_doc())
    assert "### User" in out
    assert "### Assistant" in out


def test_render_includes_h1_title() -> None:
    out = MarkdownRenderer().render(_minimal_doc())
    assert "# Test Conversation" in out


def test_render_falls_back_to_untitled_when_title_missing() -> None:
    doc = _minimal_doc()
    doc["conversation"].pop("title")
    out = MarkdownRenderer().render(doc)
    assert "# Untitled Conversation" in out


# ---------------------------------------------------------------------------
# render() — content blocks
# ---------------------------------------------------------------------------

def test_render_text_block() -> None:
    doc = _minimal_doc()
    doc["messages"][1]["message"]["content"] = [
        {"type": "text", "text": "Some assistant prose."}
    ]
    out = MarkdownRenderer().render(doc)
    assert "Some assistant prose." in out


def test_render_thinking_block_collapsed_by_default() -> None:
    """User decision: thinking blocks default to <details>-collapsed
    so the conversation reads cleanly."""
    doc = _minimal_doc()
    doc["messages"][1]["message"]["content"] = [
        {"type": "thinking", "thinking": "Let me reason about this..."},
        {"type": "text", "text": "OK here we go."},
    ]
    out = MarkdownRenderer().render(doc)
    assert "<details>" in out
    assert "<summary>Thinking</summary>" in out
    assert "Let me reason about this..." in out
    assert "</details>" in out
    # The visible answer is still rendered after the collapsed block.
    assert "OK here we go." in out


def test_render_code_block_with_language_fence() -> None:
    doc = _minimal_doc()
    doc["messages"][1]["message"]["content"] = [
        {"type": "code", "code": "print('hi')", "language": "python"},
    ]
    out = MarkdownRenderer().render(doc)
    assert "```python\nprint('hi')\n```" in out


def test_render_code_block_with_filename_hint() -> None:
    doc = _minimal_doc()
    doc["messages"][1]["message"]["content"] = [
        {
            "type": "code",
            "code": "x = 1",
            "language": "python",
            "filename": "main.py",
        },
    ]
    out = MarkdownRenderer().render(doc)
    assert "_File: `main.py`_" in out
    assert "```python\nx = 1\n```" in out


def test_render_image_url_block() -> None:
    doc = _minimal_doc()
    doc["messages"][0]["message"]["content"] = [
        {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
    ]
    out = MarkdownRenderer().render(doc)
    assert "![image](https://example.com/x.png)" in out


def test_render_resource_ref_points_to_resources_table() -> None:
    doc = _minimal_doc()
    doc["resources"] = [
        {
            "id": "res1",
            "kind": "user_file",
            "filename": "data.csv",
            "byte_size": 42,
            "source": {"type": "inline", "data": "Zm9v"},
        }
    ]
    doc["messages"][0]["message"]["content"] = [
        {"type": "resource_ref", "resource_id": "res1"},
    ]
    out = MarkdownRenderer().render(doc)
    # Resource block is referenced inline AND listed in the resources table
    assert "resource: `res1`" in out
    assert "## Resources" in out
    assert "data.csv" in out


def test_render_unknown_block_does_not_crash() -> None:
    """A future block type should not blow up the renderer — we leave a
    visible placeholder so the issue is obvious."""
    doc = _minimal_doc()
    doc["messages"][1]["message"]["content"] = [
        {"type": "future_block_type_v2", "data": {"foo": 1}},
    ]
    out = MarkdownRenderer().render(doc)
    assert "future_block_type_v2" in out


# ---------------------------------------------------------------------------
# render() — assistant tool calls
# ---------------------------------------------------------------------------

def _doc_with_tool_call() -> dict[str, Any]:
    doc = _minimal_doc()
    doc["messages"] = [
        {
            "id": "msg_0001",
            "created_at": "2026-04-26T10:00:01Z",
            "message": {"role": "user", "content": "Find file."},
        },
        {
            "id": "msg_0002",
            "created_at": "2026-04-26T10:00:02Z",
            "model": "claude-sonnet-4-5",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "toolu_01edit",
                        "type": "function",
                        "function": {
                            "name": "edit_file_v2",
                            "arguments": '{"path":"foo.py","content":"x=1"}',
                        },
                    }
                ],
            },
        },
        {
            "id": "msg_0003",
            "created_at": "2026-04-26T10:00:03Z",
            "status": "ok",
            "message": {
                "role": "tool",
                "tool_call_id": "toolu_01edit",
                "content": '{"afterContentId":"sha256:abc"}',
            },
        },
    ]
    return doc


def test_render_tool_call_uses_json_fence() -> None:
    """User decision: tool calls render as fenced ```json``` so a
    Markdown viewer syntax-highlights and a downstream parser can lift
    them back out cleanly."""
    out = MarkdownRenderer().render(_doc_with_tool_call())
    assert "#### Tool call: `edit_file_v2`" in out
    assert "```json\n" in out
    # Pretty-printed JSON, not the original compact string
    assert '"path": "foo.py"' in out


def test_render_tool_result_paired_with_call_id() -> None:
    out = MarkdownRenderer().render(_doc_with_tool_call())
    assert "### Tool result" in out
    assert "call: `toolu_01edit`" in out
    assert "status: `ok`" in out


def test_render_tool_call_with_unparseable_arguments_left_as_is() -> None:
    """When the source format produced un-parseable JSON in
    tool_calls.function.arguments, we don't rewrite it — better to
    pass the bytes through verbatim than to corrupt them."""
    doc = _doc_with_tool_call()
    doc["messages"][1]["message"]["tool_calls"][0]["function"][
        "arguments"
    ] = "this is not valid JSON {"
    out = MarkdownRenderer().render(doc)
    assert "this is not valid JSON {" in out


def test_render_tool_call_count_in_front_matter() -> None:
    out = MarkdownRenderer().render(_doc_with_tool_call())
    assert "tool_call_count: 1" in out


# ---------------------------------------------------------------------------
# Renderer registry
# ---------------------------------------------------------------------------

def test_renderer_registry_includes_md_and_markdown_aliases() -> None:
    assert RENDERERS["md"] is MarkdownRenderer
    assert RENDERERS["markdown"] is MarkdownRenderer


def test_output_filename_replaces_ocf_json() -> None:
    renderer = MarkdownRenderer()
    src = Path("/tmp/foo/conv-abc.ocf.json")
    assert renderer.output_filename_for(src) == "conv-abc.md"


# ---------------------------------------------------------------------------
# Bulk runner — render_all
# ---------------------------------------------------------------------------

def _write_doc(path: Path, doc: dict[str, Any]) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_render_all_writes_md_files(tmp_path: Path) -> None:
    src = tmp_path / "conv.ocf.json"
    _write_doc(src, _minimal_doc())
    out = tmp_path / "rendered"
    result = render_all(MarkdownRenderer(), out, sources=[src])
    assert len(result.new) == 1
    assert (out / "conv.md").exists()
    text = (out / "conv.md").read_text(encoding="utf-8")
    assert "# Test Conversation" in text


def test_render_all_idempotent(tmp_path: Path) -> None:
    src = tmp_path / "conv.ocf.json"
    _write_doc(src, _minimal_doc())
    out = tmp_path / "rendered"
    render_all(MarkdownRenderer(), out, sources=[src])
    result = render_all(MarkdownRenderer(), out, sources=[src])
    assert len(result.skipped) == 1
    assert len(result.new) == 0


def test_render_all_force_re_renders(tmp_path: Path) -> None:
    src = tmp_path / "conv.ocf.json"
    _write_doc(src, _minimal_doc())
    out = tmp_path / "rendered"
    render_all(MarkdownRenderer(), out, sources=[src])
    result = render_all(MarkdownRenderer(), out, sources=[src], force=True)
    assert len(result.updated) == 1


def test_render_all_dry_run_writes_nothing(tmp_path: Path) -> None:
    src = tmp_path / "conv.ocf.json"
    _write_doc(src, _minimal_doc())
    out = tmp_path / "rendered"
    result = render_all(MarkdownRenderer(), out, sources=[src], dry_run=True)
    assert len(result.new) == 1
    assert not (out / "conv.md").exists()


def test_render_all_uses_separate_manifest_filename(tmp_path: Path) -> None:
    """The render manifest must NOT collide with an export manifest in
    the same directory — that would let a render run trash export
    skip-state and vice versa."""
    src = tmp_path / "conv.ocf.json"
    _write_doc(src, _minimal_doc())
    out = tmp_path / "rendered"
    render_all(MarkdownRenderer(), out, sources=[src])
    assert (out / ".ocf-render-manifest.json").exists()
    assert not (out / ".ocf-manifest.json").exists()


# ---------------------------------------------------------------------------
# select_ocf_files
# ---------------------------------------------------------------------------

def test_select_ocf_files_recurses_directory(tmp_path: Path) -> None:
    sub = tmp_path / "deep" / "deeper"
    sub.mkdir(parents=True)
    a = tmp_path / "a.ocf.json"
    b = sub / "b.ocf.json"
    _write_doc(a, _minimal_doc())
    _write_doc(b, _minimal_doc())
    selected = select_ocf_files([tmp_path])
    assert set(selected) == {a, b}


def test_select_ocf_files_filters_by_platform(tmp_path: Path) -> None:
    a = tmp_path / "a.ocf.json"
    b = tmp_path / "b.ocf.json"
    doc_a = _minimal_doc()
    doc_a["conversation"]["source"]["platform"] = "cursor"
    doc_b = _minimal_doc()
    doc_b["conversation"]["source"]["platform"] = "claude_code"
    _write_doc(a, doc_a)
    _write_doc(b, doc_b)
    selected = select_ocf_files([tmp_path], platform="cursor")
    assert selected == [a]


def test_select_ocf_files_filters_by_query_title(tmp_path: Path) -> None:
    a = tmp_path / "a.ocf.json"
    b = tmp_path / "b.ocf.json"
    doc_a = _minimal_doc()
    doc_a["conversation"]["title"] = "Workspace rename and session migration"
    doc_b = _minimal_doc()
    doc_b["conversation"]["title"] = "Some other thing"
    _write_doc(a, doc_a)
    _write_doc(b, doc_b)
    selected = select_ocf_files([tmp_path], query="workspace rename")
    assert selected == [a]


def test_select_ocf_files_filters_by_uuid_query(tmp_path: Path) -> None:
    a = tmp_path / "a.ocf.json"
    b = tmp_path / "b.ocf.json"
    doc_a = _minimal_doc()
    doc_a["conversation"]["id"] = "21d94edc-5327-42e2-9f85-ebf0e2d5f256"
    doc_b = _minimal_doc()
    doc_b["conversation"]["id"] = "11111111-2222-3333-4444-555555555555"
    _write_doc(a, doc_a)
    _write_doc(b, doc_b)
    selected = select_ocf_files(
        [tmp_path], query="21d94edc-5327-42e2-9f85-ebf0e2d5f256"
    )
    assert selected == [a]


def test_select_ocf_files_filters_by_project(tmp_path: Path) -> None:
    a = tmp_path / "a.ocf.json"
    b = tmp_path / "b.ocf.json"
    doc_a = _minimal_doc()
    doc_a["conversation"]["project"]["name"] = "DerJarl"
    doc_b = _minimal_doc()
    doc_b["conversation"]["project"]["name"] = "OpenChatFormat"
    _write_doc(a, doc_a)
    _write_doc(b, doc_b)
    selected = select_ocf_files([tmp_path], project="derjarl")
    assert selected == [a]


def test_select_ocf_files_no_filter_returns_all(tmp_path: Path) -> None:
    a = tmp_path / "a.ocf.json"
    _write_doc(a, _minimal_doc())
    selected = select_ocf_files([tmp_path])
    assert selected == [a]
