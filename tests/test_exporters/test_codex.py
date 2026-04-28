"""Tests for ocf.exporters.codex against the real Codex Responses-API format.

Three layers:

1. **Single-file conversion** against the synthetic format-accurate
   fixture: validates schema, asserts mapping rules.
2. **Bulk export with manifest**: incremental skip / force / dry-run /
   missing-source-dir behavior.
3. **Real-machine smoke**: when ``~/.codex/sessions/`` has rollout
   files, run the exporter against the most recent one. Pure
   validation - assert only "doesn't raise, produces valid OCF".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocf.core.schema import is_valid, validate
from ocf.exporters._manifest import load_manifest
from ocf.exporters.codex import (
    AmbiguousMatchError,
    discover,
    export_all,
    export_one,
    find_by_name,
    load_session_index,
    resolve_sources,
)


# ---------------------------------------------------------------------------
# export_one with format-accurate synthetic fixture
# ---------------------------------------------------------------------------

def test_export_one_returns_dict(codex_rollout_file: Path) -> None:
    doc = export_one(codex_rollout_file)
    assert isinstance(doc, dict)


def test_export_one_validates_against_schema(codex_rollout_file: Path) -> None:
    doc = export_one(codex_rollout_file)
    errors = validate(doc)
    assert errors == [], f"OCF doc invalid: {[e.message for e in errors]}"


def test_export_one_correct_ocf_version(codex_rollout_file: Path) -> None:
    doc = export_one(codex_rollout_file)
    assert doc["ocf_version"] == "0.1.0"


def test_export_one_session_meta_in_conversation_meta(
    codex_rollout_file: Path,
) -> None:
    """session_meta payload fields land in conversation.meta.codex."""
    doc = export_one(codex_rollout_file)
    cm = doc["conversation"]["meta"]["codex"]
    assert cm["session_id"] == "019dc9c5-4467-72b2-b4d5-62de12f45004"
    assert cm["originator"] == "Codex Desktop"
    assert cm["cli_version"] == "0.125.0-alpha.3"
    assert cm["source"] == "vscode"
    assert cm["model_provider"] == "openai"
    assert cm["raw_format"] == "responses-api-jsonl"


def test_export_one_default_model_from_turn_context(
    codex_rollout_file: Path,
) -> None:
    doc = export_one(codex_rollout_file)
    assert doc["conversation"]["default_model"] == "gpt-5.5"


def test_export_one_project_from_cwd(codex_rollout_file: Path) -> None:
    doc = export_one(codex_rollout_file)
    proj = doc["conversation"]["project"]
    assert proj["description"] == "C:\\Development\\Projekte\\OpenChatFormat"
    assert proj["name"] == "OpenChatFormat"


def test_export_one_title_from_inline_event(codex_rollout_file: Path) -> None:
    """thread_name_updated event populates conversation.title."""
    doc = export_one(codex_rollout_file)
    assert doc["conversation"]["title"] == "List files in OpenChatFormat"


def test_export_one_messages_count(codex_rollout_file: Path) -> None:
    """Each response_item -> one OCF envelope. event_msg + turn_context dropped.
    Fixture has: 1 developer msg + 1 user msg + 1 reasoning + 1 function_call
    + 1 function_call_output + 1 assistant msg = 6 OCF envelopes.
    """
    doc = export_one(codex_rollout_file)
    assert len(doc["messages"]) == 6


def test_export_one_role_sequence(codex_rollout_file: Path) -> None:
    doc = export_one(codex_rollout_file)
    roles = [m["message"]["role"] for m in doc["messages"]]
    # developer, user, assistant (reasoning), assistant (tool_call), tool, assistant
    assert roles == ["developer", "user", "assistant", "assistant", "tool", "assistant"]


def test_export_one_input_text_to_text(codex_rollout_file: Path) -> None:
    """Codex 'input_text' content blocks become OCF 'text' blocks (or string)."""
    doc = export_one(codex_rollout_file)
    user_msg = next(m for m in doc["messages"] if m["message"]["role"] == "user")
    # Single-block content gets collapsed to string shorthand.
    assert user_msg["message"]["content"] == "List files in this folder."


def test_export_one_output_text_to_text(codex_rollout_file: Path) -> None:
    doc = export_one(codex_rollout_file)
    final_assistant = doc["messages"][-1]
    assert final_assistant["message"]["role"] == "assistant"
    assert final_assistant["message"]["content"] == "Found 2 files: file1.md, file2.py."


def test_export_one_reasoning_to_thinking(codex_rollout_file: Path) -> None:
    """response_item.reasoning -> assistant message with thinking content."""
    doc = export_one(codex_rollout_file)
    reasoning_msg = next(
        m
        for m in doc["messages"]
        if isinstance(m["message"].get("content"), list)
        and any(b.get("type") == "thinking" for b in m["message"]["content"])
    )
    block = reasoning_msg["message"]["content"][0]
    assert block["thinking"] == "User wants directory listing."
    assert reasoning_msg["meta"]["codex_render"]["encrypted_content"] == "gAAAAAB..."


def test_export_one_function_call_in_tool_calls(codex_rollout_file: Path) -> None:
    """function_call -> assistant envelope with tool_calls[] (content=null)."""
    doc = export_one(codex_rollout_file)
    fc_msg = next(
        m
        for m in doc["messages"]
        if m["message"]["role"] == "assistant"
        and m["message"].get("tool_calls")
    )
    tc = fc_msg["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "shell_command"
    assert tc["id"] == "call_Y5YZhS0LthKy1jec4a3iHo1S"
    assert tc["id_origin"] == "source"
    assert fc_msg["message"]["content"] is None


def test_export_one_function_call_output_to_tool(codex_rollout_file: Path) -> None:
    doc = export_one(codex_rollout_file)
    tool_msg = next(m for m in doc["messages"] if m["message"]["role"] == "tool")
    assert tool_msg["message"]["tool_call_id"] == "call_Y5YZhS0LthKy1jec4a3iHo1S"
    assert "Exit code: 0" in tool_msg["message"]["content"]


def test_export_one_no_session_meta_raises(tmp_path: Path) -> None:
    """File without a session_meta event is not a recognizable Codex rollout."""
    bad = tmp_path / "rollout-broken.jsonl"
    bad.write_text(
        json.dumps({"type": "event_msg", "payload": {"type": "x"}}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="session_meta"):
        export_one(bad)


def test_export_one_assistant_model_attached(codex_rollout_file: Path) -> None:
    """Model from turn_context attaches to assistant messages."""
    doc = export_one(codex_rollout_file)
    for m in doc["messages"]:
        if m["message"]["role"] == "assistant":
            assert m.get("model") == "gpt-5.5"


def test_export_one_cli_version_preserved(codex_rollout_file: Path) -> None:
    """cli_version preserved in meta — distinguishes Codex versions for re-projection."""
    doc = export_one(codex_rollout_file)
    assert doc["conversation"]["meta"]["codex"]["cli_version"] == "0.125.0-alpha.3"


def test_export_one_base_instructions_preserved_in_meta(
    codex_rollout_file: Path,
) -> None:
    """base_instructions stay in meta only — not promoted to a system message."""
    doc = export_one(codex_rollout_file)
    cm = doc["conversation"]["meta"]["codex"]
    assert cm["base_instructions_present"] is True
    assert cm["base_instructions_text"] == "You are Codex, a coding agent."
    # Verify NO system message was injected
    assert not any(m["message"]["role"] == "system" for m in doc["messages"])


def test_export_one_id_origin_synthesized(codex_rollout_file: Path) -> None:
    """Codex events have no message ID — exporter synthesizes."""
    doc = export_one(codex_rollout_file)
    for m in doc["messages"]:
        assert m["id_origin"] == "synthesized"
    # tool_call id_origin = source (Codex provides call_id)
    fc_msg = next(
        m for m in doc["messages"] if m["message"].get("tool_calls")
    )
    assert fc_msg["message"]["tool_calls"][0]["id_origin"] == "source"


# ---------------------------------------------------------------------------
# Session index integration
# ---------------------------------------------------------------------------

def test_load_session_index_real_format(tmp_path: Path) -> None:
    """Session index is a JSONL with id/thread_name/updated_at rows."""
    p = tmp_path / "session_index.jsonl"
    p.write_text(
        json.dumps(
            {"id": "abc", "thread_name": "Hello", "updated_at": "2026-01-01T00:00:00Z"}
        )
        + "\n"
        + json.dumps(
            {"id": "def", "thread_name": "World", "updated_at": "2026-01-02T00:00:00Z"}
        )
        + "\n",
        encoding="utf-8",
    )
    index = load_session_index(p)
    assert index["abc"]["thread_name"] == "Hello"
    assert index["def"]["thread_name"] == "World"


def test_load_session_index_missing_returns_empty(tmp_path: Path) -> None:
    assert load_session_index(tmp_path / "no-such-file.jsonl") == {}


def test_load_session_index_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "session_index.jsonl"
    p.write_text(
        json.dumps({"id": "abc", "thread_name": "Hello"})
        + "\n"
        + "not json\n"
        + json.dumps({"id": "def", "thread_name": "World"})
        + "\n",
        encoding="utf-8",
    )
    index = load_session_index(p)
    assert set(index.keys()) == {"abc", "def"}


# ---------------------------------------------------------------------------
# discover() / find_by_name()
# ---------------------------------------------------------------------------

def test_discover_finds_all_rollouts(codex_multi_sessions_dir: Path) -> None:
    files = discover(codex_multi_sessions_dir)
    assert len(files) == 3


def test_discover_nonexistent_dir_returns_empty(tmp_path: Path) -> None:
    assert discover(tmp_path / "does-not-exist") == []


def test_find_by_name_matches_thread_name(
    codex_multi_sessions_dir: Path,
) -> None:
    """The killer use case: search by the assistant-generated title."""
    matches = find_by_name(
        "Spec design", source_dir=codex_multi_sessions_dir
    )
    assert len(matches) == 1
    assert "019aaa00" in matches[0].name


def test_find_by_name_matches_cwd_basename(
    codex_multi_sessions_dir: Path,
) -> None:
    matches = find_by_name(
        "openchatformat", source_dir=codex_multi_sessions_dir
    )
    assert len(matches) == 2


def test_find_by_name_multi_word_and(codex_multi_sessions_dir: Path) -> None:
    """All words must appear (substring AND)."""
    matches = find_by_name(
        "Roundtrip analysis", source_dir=codex_multi_sessions_dir
    )
    assert len(matches) == 1


def test_find_by_name_case_insensitive_default(
    codex_multi_sessions_dir: Path,
) -> None:
    matches = find_by_name(
        "OPENCHATFORMAT", source_dir=codex_multi_sessions_dir
    )
    assert len(matches) == 2


def test_find_by_name_no_match(codex_multi_sessions_dir: Path) -> None:
    assert find_by_name("nonexistent", source_dir=codex_multi_sessions_dir) == []


def test_find_by_name_empty_query_returns_empty(
    codex_multi_sessions_dir: Path,
) -> None:
    assert find_by_name("", source_dir=codex_multi_sessions_dir) == []


# ---------------------------------------------------------------------------
# resolve_sources polymorphic dispatch
# ---------------------------------------------------------------------------

def test_resolve_sources_directory(codex_multi_sessions_dir: Path) -> None:
    files = resolve_sources(codex_multi_sessions_dir)
    assert len(files) == 3


def test_resolve_sources_single_file(codex_rollout_file: Path) -> None:
    assert resolve_sources(codex_rollout_file) == [codex_rollout_file]


def test_resolve_sources_query(codex_multi_sessions_dir: Path) -> None:
    files = resolve_sources(
        "Python exporter", source_dir=codex_multi_sessions_dir
    )
    assert len(files) == 1


def test_resolve_sources_string_path(codex_rollout_file: Path) -> None:
    assert resolve_sources(str(codex_rollout_file)) == [codex_rollout_file]


# ---------------------------------------------------------------------------
# export_all bulk + manifest
# ---------------------------------------------------------------------------

def test_export_all_creates_files(
    codex_sessions_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    result = export_all(out, source_dir=codex_sessions_dir)
    assert len(result.new) == 1
    assert len(result.failed) == 0


def test_export_all_writes_valid_ocf(
    codex_sessions_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    export_all(out, source_dir=codex_sessions_dir)
    ocf_files = list(out.glob("rollout-*.ocf.json"))
    assert len(ocf_files) == 1
    with ocf_files[0].open("r", encoding="utf-8") as fh:
        doc = json.load(fh)
    assert is_valid(doc)


def test_export_all_idempotent(
    codex_sessions_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    export_all(out, source_dir=codex_sessions_dir)
    second = export_all(out, source_dir=codex_sessions_dir)
    assert len(second.new) == 0
    assert len(second.skipped) == 1


def test_export_all_force_re_exports(
    codex_sessions_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    export_all(out, source_dir=codex_sessions_dir)
    forced = export_all(out, source_dir=codex_sessions_dir, force=True)
    assert len(forced.updated) == 1


def test_export_all_dry_run(
    codex_sessions_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    result = export_all(out, source_dir=codex_sessions_dir, dry_run=True)
    assert len(result.new) == 1
    assert not (out / ".ocf-manifest.json").exists()


def test_export_all_missing_source_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Codex sessions"):
        export_all(tmp_path / "out", source_dir=tmp_path / "nope")


def test_export_all_with_explicit_sources(
    codex_multi_sessions_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    files = discover(codex_multi_sessions_dir)
    result = export_all(out, sources=files[:2])
    assert len(result.new) == 2


def test_export_all_with_query_resolved(
    codex_multi_sessions_dir: Path, tmp_path: Path
) -> None:
    """CLI pattern: resolve query first, then pass results."""
    out = tmp_path / "out"
    matches = find_by_name(
        "Python exporter", source_dir=codex_multi_sessions_dir
    )
    result = export_all(out, sources=matches)
    assert len(result.new) == 1


# ---------------------------------------------------------------------------
# AmbiguousMatchError
# ---------------------------------------------------------------------------

def test_ambiguous_match_error_carries_candidates(tmp_path: Path) -> None:
    candidates = [tmp_path / "a.jsonl", tmp_path / "b.jsonl"]
    err = AmbiguousMatchError("openchatformat", candidates)
    assert err.query == "openchatformat"
    assert err.candidates == candidates


# ---------------------------------------------------------------------------
# Real-machine smoke test
# ---------------------------------------------------------------------------

def test_real_codex_session_exports_to_valid_ocf(
    real_codex_session: Path,
) -> None:
    """Smoke test against a real Codex rollout from this machine.

    Asserts only that:
      - export_one() doesn't raise
      - The result validates against the OCF schema

    Skipped automatically when ~/.codex/sessions/ is empty or missing.
    Crucial because the synthetic fixture is a model of reality, not
    reality itself; this catches drift between assumption and truth.
    """
    doc = export_one(real_codex_session)
    errors = validate(doc)
    if errors:
        details = "\n".join(f"  - {e.json_path}: {e.message}" for e in errors[:5])
        pytest.fail(
            f"Real Codex session at {real_codex_session.name} produced "
            f"{len(errors)} validation errors:\n{details}"
        )
