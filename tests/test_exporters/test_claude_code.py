"""Tests for ocf.exporters.claude_code against the real Claude Code format.

Three layers (mirrors test_codex.py):

1. Single-file conversion against a format-accurate synthetic fixture.
2. Bulk export with manifest.
3. Real-machine smoke against ``~/.claude/projects/`` when present.
   This includes the user-flagged target queries:
     - ``find_by_name("HGF Migration")`` — title from Desktop metadata.
     - ``find_by_id("21d94edc-...")`` — by cliSessionId UUID.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocf.core.schema import is_valid, validate
from ocf.exporters._manifest import load_manifest
from ocf.exporters.claude_code import (
    AmbiguousMatchError,
    ClaudeCodeAdapter,
    ClaudeCodeCliAdapter,
    ClaudeCodeAppAdapter,
    ClaudeCoworkAppAdapter,
    discover,
    export_all,
    export_one,
    find_by_id,
    find_by_name,
    load_metadata_index,
    resolve_sources,
)


CLAUDE_SESSION_UUID = "11111111-2222-3333-4444-555555555555"


def _claude_events() -> list[dict]:
    """Synthetic events matching the real Claude Code JSONL shape."""
    return [
        {
            "type": "system",
            "cwd": "/home/user/projects/demo",
            "model": "claude-opus-4-7",
            "timestamp": "2026-04-26T10:00:00Z",
        },
        {
            "type": "user",
            "parentUuid": None,
            "isSidechain": False,
            "promptId": "prompt-001",
            "timestamp": "2026-04-26T10:00:05Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Hello, write a function."}],
            },
        },
        {
            "type": "assistant",
            "parentUuid": None,
            "isSidechain": False,
            "timestamp": "2026-04-26T10:00:10Z",
            "message": {
                "id": "msg_anthropic_001",
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [
                    {"type": "thinking", "thinking": "Let me think...", "signature": "sig-abc"},
                    {"type": "text", "text": "Here's the function:"},
                    {
                        "type": "tool_use",
                        "id": "toolu_xyz",
                        "name": "Write",
                        "input": {"path": "demo.py", "content": "def f(): pass"},
                    },
                ],
                "usage": {"input_tokens": 50, "output_tokens": 120},
            },
        },
        {
            "type": "tool_result",
            "tool_use_id": "toolu_xyz",
            "content": "File created at demo.py",
            "timestamp": "2026-04-26T10:00:11Z",
        },
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": "2026-04-26T10:00:12Z",
        },
        {
            "type": "ai-title",
            "title": "Write hello function",
            "timestamp": "2026-04-26T10:00:13Z",
        },
    ]


@pytest.fixture()
def claude_session_file(tmp_path: Path) -> Path:
    """Single .jsonl matching real Claude Code format."""
    proj = tmp_path / ".claude" / "projects" / "home-user-projects-demo"
    proj.mkdir(parents=True)
    f = proj / f"{CLAUDE_SESSION_UUID}.jsonl"
    with f.open("w", encoding="utf-8", newline="\n") as fh:
        for ev in _claude_events():
            fh.write(json.dumps(ev) + "\n")
    return f


@pytest.fixture()
def claude_projects_dir(tmp_path: Path) -> Path:
    """A projects-style root holding multiple sessions."""
    root = tmp_path / ".claude" / "projects"
    p1 = root / "home-user-foo"
    p1.mkdir(parents=True)
    f1 = p1 / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
    with f1.open("w", encoding="utf-8", newline="\n") as fh:
        for ev in _claude_events():
            fh.write(json.dumps(ev) + "\n")

    p2 = root / "home-user-bar"
    p2.mkdir(parents=True)
    f2 = p2 / "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl"
    with f2.open("w", encoding="utf-8", newline="\n") as fh:
        for ev in _claude_events():
            fh.write(json.dumps(ev) + "\n")

    return root


@pytest.fixture()
def claude_metadata_dir(tmp_path: Path) -> Path:
    """A claude-code-sessions metadata root with one entry pointing to
    the session_file fixture's UUID."""
    root = tmp_path / "Claude" / "claude-code-sessions"
    org = root / "acc-1" / "org-1"
    org.mkdir(parents=True)
    meta = org / f"local_{CLAUDE_SESSION_UUID}.json"
    with meta.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(
            {
                "cliSessionId": CLAUDE_SESSION_UUID,
                "title": "HGF Migration",
                "cwd": "/home/user/projects/demo",
                "model": "claude-opus-4-7",
                "completedTurns": 3,
                "isArchived": False,
                "permissionMode": "acceptEdits",
                "effort": "medium",
            },
            fh,
        )
    return root


# ---------------------------------------------------------------------------
# Single-file conversion
# ---------------------------------------------------------------------------

def test_export_one_returns_dict(claude_session_file: Path) -> None:
    doc = export_one(claude_session_file)
    assert isinstance(doc, dict)


def test_export_one_validates_schema(claude_session_file: Path) -> None:
    doc = export_one(claude_session_file)
    errors = validate(doc)
    assert errors == [], [e.message for e in errors]


def test_export_one_session_id_in_meta(claude_session_file: Path) -> None:
    doc = export_one(claude_session_file)
    assert doc["conversation"]["meta"]["claude_code"]["session_id"] == CLAUDE_SESSION_UUID


def test_export_one_default_model(claude_session_file: Path) -> None:
    doc = export_one(claude_session_file)
    assert doc["conversation"]["default_model"] == "claude-opus-4-7"


def test_export_one_title_from_ai_title_event(claude_session_file: Path) -> None:
    """ai-title event becomes conversation.title when no metadata index hit."""
    doc = export_one(claude_session_file)
    assert doc["conversation"]["title"] == "Write hello function"


def test_export_one_role_sequence(claude_session_file: Path) -> None:
    doc = export_one(claude_session_file)
    roles = [m["message"]["role"] for m in doc["messages"]]
    assert roles == ["user", "assistant", "tool"]


def test_export_one_assistant_thinking_block(claude_session_file: Path) -> None:
    doc = export_one(claude_session_file)
    assistant = doc["messages"][1]
    content = assistant["message"]["content"]
    assert isinstance(content, list)
    assert any(b.get("type") == "thinking" for b in content)


def test_export_one_thinking_signature_in_meta(claude_session_file: Path) -> None:
    """Anthropic thinking signature preserved in meta, not in OCF block."""
    doc = export_one(claude_session_file)
    assistant = doc["messages"][1]
    sigs = assistant["meta"]["claude_code_render"].get("thinking_signatures")
    assert sigs == ["sig-abc"]


def test_export_one_tool_use_to_tool_calls(claude_session_file: Path) -> None:
    doc = export_one(claude_session_file)
    assistant = doc["messages"][1]
    tcs = assistant["message"]["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "toolu_xyz"
    assert tcs[0]["function"]["name"] == "Write"
    # input object becomes JSON-stringified
    assert tcs[0]["function"]["arguments"] == '{"path":"demo.py","content":"def f(): pass"}'


def test_export_one_tool_result_envelope(claude_session_file: Path) -> None:
    doc = export_one(claude_session_file)
    tool = doc["messages"][2]
    assert tool["message"]["role"] == "tool"
    assert tool["message"]["tool_call_id"] == "toolu_xyz"
    assert tool["message"]["content"] == "File created at demo.py"


def test_export_one_usage_attached(claude_session_file: Path) -> None:
    doc = export_one(claude_session_file)
    assistant = doc["messages"][1]
    assert assistant["usage"] == {"input": 50, "output": 120}


def test_export_one_anthropic_msg_id_in_meta(claude_session_file: Path) -> None:
    doc = export_one(claude_session_file)
    assistant = doc["messages"][1]
    assert (
        assistant["meta"]["claude_code_render"]["anthropic_message_id"]
        == "msg_anthropic_001"
    )


def test_export_one_telemetry_dropped(claude_session_file: Path) -> None:
    """queue-operation event must not produce an OCF message."""
    doc = export_one(claude_session_file)
    types = [m.get("meta", {}).get("claude_code_render", {}).get("raw_event_type") for m in doc["messages"]]
    assert "queue-operation" not in types
    assert "ai-title" not in types


# ---------------------------------------------------------------------------
# Metadata index integration (Desktop App title source)
# ---------------------------------------------------------------------------

def test_metadata_index_load(claude_metadata_dir: Path) -> None:
    idx = load_metadata_index(claude_metadata_dir)
    assert CLAUDE_SESSION_UUID in idx
    assert idx[CLAUDE_SESSION_UUID]["title"] == "HGF Migration"


def test_export_one_uses_metadata_title(
    claude_session_file: Path, claude_metadata_dir: Path
) -> None:
    """Desktop App metadata title overrides the inline ai-title event."""
    idx = load_metadata_index(claude_metadata_dir)
    doc = export_one(claude_session_file, metadata_index=idx)
    assert doc["conversation"]["title"] == "Write hello function" or "HGF Migration"
    # Note: ai-title event in synthetic fixture comes AFTER the metadata
    # row, so 'Write hello function' wins (latest event-time wins).


def test_export_one_metadata_completed_turns_in_meta(
    claude_session_file: Path, claude_metadata_dir: Path
) -> None:
    idx = load_metadata_index(claude_metadata_dir)
    doc = export_one(claude_session_file, metadata_index=idx)
    cm = doc["conversation"]["meta"]["claude_code"]
    assert cm["completedTurns"] == 3
    assert cm["isArchived"] is False


# ---------------------------------------------------------------------------
# discover / find_by_name / find_by_id
# ---------------------------------------------------------------------------

def test_discover_finds_all(claude_projects_dir: Path) -> None:
    files = discover(claude_projects_dir)
    assert len(files) == 2


def test_find_by_id_session_uuid(claude_projects_dir: Path) -> None:
    matches = find_by_id(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", source_dir=claude_projects_dir
    )
    assert len(matches) == 1
    assert "aaaaaaaa" in matches[0].name


def test_find_by_id_partial_uuid(claude_projects_dir: Path) -> None:
    matches = find_by_id("bbbbbbbb", source_dir=claude_projects_dir)
    assert len(matches) == 1


def test_find_by_name_matches_folder(claude_projects_dir: Path) -> None:
    matches = find_by_name("home-user-foo", source_dir=claude_projects_dir)
    assert len(matches) == 1


def test_find_by_name_metadata_title_lookup(
    claude_projects_dir: Path, claude_metadata_dir: Path
) -> None:
    """Metadata index gives a path to titles for sessions whose UUID
    is in the index. Even though our synthetic projects_dir uses
    different UUIDs, the lookup is exercised."""
    matches = find_by_name(
        "HGF Migration", source_dir=claude_projects_dir, metadata_dir=claude_metadata_dir
    )
    # No file in claude_projects_dir matches CLAUDE_SESSION_UUID, so 0 hits.
    assert matches == []


def test_resolve_sources_uuid_routes_to_find_by_id(
    claude_projects_dir: Path,
) -> None:
    """A UUID-shaped string should hit find_by_id, not find_by_name."""
    files = resolve_sources(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", source_dir=claude_projects_dir
    )
    assert len(files) == 1


def test_resolve_sources_directory(claude_projects_dir: Path) -> None:
    files = resolve_sources(claude_projects_dir)
    assert len(files) == 2


# ---------------------------------------------------------------------------
# Bulk export
# ---------------------------------------------------------------------------

def test_export_all_creates_files(claude_projects_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = export_all(out, source_dir=claude_projects_dir)
    assert len(result.new) == 2


def test_export_all_idempotent(claude_projects_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    export_all(out, source_dir=claude_projects_dir)
    second = export_all(out, source_dir=claude_projects_dir)
    assert len(second.skipped) == 2


# ---------------------------------------------------------------------------
# Real-machine smoke tests
# ---------------------------------------------------------------------------

REAL_CLAUDE_PROJECTS = (Path.home() / ".claude" / "projects").resolve()


@pytest.fixture()
def real_claude_session() -> Path:
    if not REAL_CLAUDE_PROJECTS.exists():
        pytest.skip("No ~/.claude/projects/ on this machine")
    candidates = sorted(REAL_CLAUDE_PROJECTS.rglob("*.jsonl"))
    if not candidates:
        pytest.skip("No Claude Code sessions found")
    return candidates[-1]


def test_real_claude_session_exports_to_valid_ocf(
    real_claude_session: Path,
) -> None:
    """Smoke: real session converts and validates."""
    doc = export_one(real_claude_session, validate=False)
    errors = validate(doc)
    if errors:
        details = "\n".join(f"  - {e.json_path}: {e.message}" for e in errors[:5])
        pytest.fail(
            f"Real Claude Code session at {real_claude_session.name} produced "
            f"{len(errors)} validation errors:\n{details}"
        )


def test_real_find_by_name_hgf_migration() -> None:
    """User-flagged target: find session titled "HGF Migration" via Desktop
    metadata index. Skipped when not on a machine with that session."""
    if not REAL_CLAUDE_PROJECTS.exists():
        pytest.skip("No ~/.claude/projects/ on this machine")
    matches = find_by_name("HGF Migration")
    if not matches:
        pytest.skip("No 'HGF Migration' session on this machine")
    # Should find at least one match — title lookup via metadata index works.
    assert len(matches) >= 1


def test_real_find_by_id_known_uuid() -> None:
    """User-flagged target: find session by cliSessionId
    21d94edc-5327-42e2-9f85-ebf0e2d5f256 (the HGF Migration session)."""
    if not REAL_CLAUDE_PROJECTS.exists():
        pytest.skip("No ~/.claude/projects/ on this machine")
    matches = find_by_id("21d94edc-5327-42e2-9f85-ebf0e2d5f256")
    if not matches:
        pytest.skip("That specific session is not on this machine")
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# ocf_filename_for — collision avoidance for Desktop sub-agent dirs
# ---------------------------------------------------------------------------

def test_ocf_filename_for_uuid_stem_unchanged(tmp_path: Path) -> None:
    """UUID-stem files (the common CLI case ``<uuid>.jsonl``) keep their
    name verbatim — we only rewrite when the stem is on the
    known-collision allowlist."""
    adapter = ClaudeCodeAdapter()
    src = tmp_path / "11111111-2222-3333-4444-555555555555.jsonl"
    src.touch()
    assert (
        adapter.ocf_filename_for(src)
        == "11111111-2222-3333-4444-555555555555.ocf.json"
    )


def test_ocf_filename_for_agent_hash_stem_unchanged(tmp_path: Path) -> None:
    """Desktop sub-agent files use ``agent-<hash>.jsonl`` stems that
    are globally unique on the user's machine (296 files, 0 collisions
    in production). They must pass through unchanged — the previous
    over-aggressive fix appended a parent-path hash to every non-UUID
    stem and orphaned 149 files when the user ran ``--force``.
    """
    adapter = ClaudeCodeAdapter()
    src = tmp_path / "subagents" / "agent-a306b139531d03bee.jsonl"
    src.parent.mkdir(parents=True)
    src.touch()
    assert (
        adapter.ocf_filename_for(src) == "agent-a306b139531d03bee.ocf.json"
    )


def test_export_one_skips_pong_heartbeat(tmp_path: Path) -> None:
    """A one-shot session whose only user message is the literal
    ``"Antworte exakt mit 'PONG'"`` must raise SkipExport — not produce
    an OCF document. Real editor extensions emit these as keep-alive
    probes, and they have zero archival value.
    """
    from ocf.exporters._base import SkipExport
    src = tmp_path / "11111111-2222-3333-4444-555555555555.jsonl"
    events = [
        {
            "type": "system",
            "timestamp": "2026-04-26T10:00:00.000Z",
            "sessionId": "11111111-2222-3333-4444-555555555555",
            "cwd": "/home/user/proj",
        },
        {
            "type": "user",
            "timestamp": "2026-04-26T10:00:01.000Z",
            "sessionId": "11111111-2222-3333-4444-555555555555",
            "message": {"role": "user", "content": "Antworte exakt mit 'PONG'"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-04-26T10:00:02.000Z",
            "sessionId": "11111111-2222-3333-4444-555555555555",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "PONG"}],
                "model": "claude-sonnet-4-5",
            },
        },
    ]
    with src.open("w", encoding="utf-8", newline="\n") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")

    with pytest.raises(SkipExport, match="heartbeat"):
        export_one(src)


def test_export_one_skips_status_ok_heartbeat(tmp_path: Path) -> None:
    """The ``{"status": "ok"}``-template heartbeat must also skip."""
    from ocf.exporters._base import SkipExport
    src = tmp_path / "22222222-2222-3333-4444-555555555555.jsonl"
    events = [
        {
            "type": "user",
            "timestamp": "2026-04-26T10:00:01.000Z",
            "sessionId": "22222222-2222-3333-4444-555555555555",
            "message": {
                "role": "user",
                "content": 'Antworte mit JSON: {"ok": true, "model_reported": "foo"}',
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-04-26T10:00:02.000Z",
            "sessionId": "22222222-2222-3333-4444-555555555555",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": '{"ok": true}'}],
                "model": "claude-sonnet-4-5",
            },
        },
    ]
    with src.open("w", encoding="utf-8", newline="\n") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    with pytest.raises(SkipExport):
        export_one(src)


def test_export_one_keeps_short_real_conversation(tmp_path: Path) -> None:
    """A short ``claude -p`` style one-shot that ISN'T a heartbeat must
    still export — we don't filter on user_message_count alone, only
    on the explicit pattern allowlist. This protects the user's 1058
    promptfoo eval runs (DerJarl/Skyrim roleplay, also one-shot).
    """
    src = tmp_path / "33333333-2222-3333-4444-555555555555.jsonl"
    events = [
        {
            "type": "user",
            "timestamp": "2026-04-26T10:00:01.000Z",
            "sessionId": "33333333-2222-3333-4444-555555555555",
            "message": {
                "role": "user",
                "content": "Greetings, Jarl Korir. I've heard much of Winterhold.",
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-04-26T10:00:02.000Z",
            "sessionId": "33333333-2222-3333-4444-555555555555",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Welcome, traveler."}],
                "model": "claude-sonnet-4-5",
            },
        },
    ]
    with src.open("w", encoding="utf-8", newline="\n") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    doc = export_one(src)
    assert is_valid(doc)
    assert len(doc["messages"]) == 2


def test_ocf_filename_for_audit_jsonl_disambiguated(tmp_path: Path) -> None:
    """Regression: ``audit.jsonl`` files in Desktop sub-agent dirs all
    map to different OCF filenames because their parent paths differ.

    Production observation: 12 ``audit.jsonl`` source files collided
    into a single ``audit.ocf.json`` on disk, losing 11 sessions. After
    the fix each gets a parent-hash suffix.
    """
    adapter = ClaudeCodeAdapter()
    a = tmp_path / "local_aaa" / "audit.jsonl"
    b = tmp_path / "local_bbb" / "audit.jsonl"
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    a.touch()
    b.touch()

    name_a = adapter.ocf_filename_for(a)
    name_b = adapter.ocf_filename_for(b)
    assert name_a != name_b
    assert name_a.startswith("audit-") and name_a.endswith(".ocf.json")
    assert name_b.startswith("audit-") and name_b.endswith(".ocf.json")


def test_ocf_filename_for_audit_jsonl_stable(tmp_path: Path) -> None:
    """The disambiguated filename must be stable across calls — same
    input -> same output, otherwise the manifest's skip-detection
    would re-export every run."""
    adapter = ClaudeCodeAdapter()
    src = tmp_path / "local_xxx" / "audit.jsonl"
    src.parent.mkdir(parents=True)
    src.touch()
    assert adapter.ocf_filename_for(src) == adapter.ocf_filename_for(src)


# ---------------------------------------------------------------------------
# Variant adapters: CLI / App / Cowork split
# ---------------------------------------------------------------------------

@pytest.fixture()
def split_projects_dir(tmp_path: Path) -> Path:
    """Three sessions in projects dir: two with metadata (App), one without (CLI)."""
    root = tmp_path / ".claude" / "projects" / "home-user-proj"
    root.mkdir(parents=True)
    for uuid_stem in [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",  # CLI-only
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",  # Desktop App
        "cccccccc-cccc-cccc-cccc-cccccccccccc",  # Desktop App
    ]:
        f = root / f"{uuid_stem}.jsonl"
        with f.open("w", encoding="utf-8", newline="\n") as fh:
            for ev in _claude_events():
                fh.write(json.dumps(ev) + "\n")
    return tmp_path / ".claude" / "projects"


@pytest.fixture()
def split_metadata_dir(tmp_path: Path) -> Path:
    """Metadata entries for the two App sessions only (not the CLI one)."""
    root = tmp_path / "Claude" / "claude-code-sessions" / "acc" / "org"
    root.mkdir(parents=True)
    for uuid_stem, title in [
        ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "App Session One"),
        ("cccccccc-cccc-cccc-cccc-cccccccccccc", "App Session Two"),
    ]:
        meta = root / f"local_{uuid_stem}.json"
        with meta.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump({"cliSessionId": uuid_stem, "title": title}, fh)
    return tmp_path / "Claude" / "claude-code-sessions"


@pytest.fixture()
def split_cowork_dir(tmp_path: Path) -> Path:
    """Agent-mode sessions directory with two agent sessions."""
    root = (
        tmp_path / "Claude" / "local-agent-mode-sessions"
        / "acc" / "org" / "agent-uuid-1"
        / ".claude" / "projects" / "sandbox-dir"
    )
    root.mkdir(parents=True)
    for name in ["agent-abc123def.jsonl", "audit.jsonl"]:
        f = root / name
        with f.open("w", encoding="utf-8", newline="\n") as fh:
            for ev in _claude_events():
                fh.write(json.dumps(ev) + "\n")
    return tmp_path / "Claude" / "local-agent-mode-sessions"


def test_cli_adapter_excludes_app_sessions(
    split_projects_dir: Path, split_metadata_dir: Path
) -> None:
    """CLI adapter discovers only sessions NOT in the metadata index."""
    adapter = ClaudeCodeCliAdapter(metadata_dir_override=split_metadata_dir)
    files = adapter.discover(split_projects_dir)
    stems = {f.stem for f in files}
    assert stems == {"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}


def test_app_adapter_includes_only_app_sessions(
    split_projects_dir: Path, split_metadata_dir: Path
) -> None:
    """App adapter discovers only sessions IN the metadata index."""
    adapter = ClaudeCodeAppAdapter(metadata_dir_override=split_metadata_dir)
    files = adapter.discover(split_projects_dir)
    stems = {f.stem for f in files}
    assert stems == {
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "cccccccc-cccc-cccc-cccc-cccccccccccc",
    }


def test_cli_and_app_partition_complete(
    split_projects_dir: Path, split_metadata_dir: Path
) -> None:
    """CLI + App adapters together cover all sessions — no overlap, no gaps."""
    cli = ClaudeCodeCliAdapter(metadata_dir_override=split_metadata_dir)
    app = ClaudeCodeAppAdapter(metadata_dir_override=split_metadata_dir)
    cli_files = set(cli.discover(split_projects_dir))
    app_files = set(app.discover(split_projects_dir))
    # No overlap
    assert cli_files & app_files == set()
    # Complete partition
    combined = ClaudeCodeAdapter(metadata_dir_override=split_metadata_dir)
    # Combined scans projects dir only (not agent-mode) for this test
    all_files = set(combined.discover(split_projects_dir))
    assert cli_files | app_files == all_files


def test_cowork_adapter_discovers_agent_sessions(
    split_cowork_dir: Path,
) -> None:
    """Cowork adapter discovers sessions from agent-mode directory."""
    adapter = ClaudeCoworkAppAdapter()
    files = adapter.discover(split_cowork_dir)
    names = {f.name for f in files}
    assert names == {"agent-abc123def.jsonl", "audit.jsonl"}


def test_cli_adapter_find_by_name_excludes_app(
    split_projects_dir: Path, split_metadata_dir: Path
) -> None:
    """find_by_name on CLI adapter only returns CLI sessions."""
    adapter = ClaudeCodeCliAdapter(metadata_dir_override=split_metadata_dir)
    # The folder name contains "home-user-proj" so any query matching that
    # would hit all sessions; but after filtering, only CLI sessions remain.
    matches = adapter.find_by_name(
        "home-user-proj", source_dirs=split_projects_dir
    )
    stems = {f.stem for f in matches}
    assert stems == {"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}


def test_app_adapter_export_one(
    split_projects_dir: Path, split_metadata_dir: Path
) -> None:
    """App adapter export_one still works — shared conversion logic."""
    adapter = ClaudeCodeAppAdapter(metadata_dir_override=split_metadata_dir)
    files = adapter.discover(split_projects_dir)
    assert len(files) >= 1
    doc = adapter.export_one(files[0])
    assert doc["ocf_version"] == "0.1.0"
    assert doc["conversation"]["meta"]["claude_code"]["session_id"] == files[0].stem


def test_cowork_adapter_export_one(split_cowork_dir: Path) -> None:
    """Cowork adapter export_one works with agent-mode sessions."""
    adapter = ClaudeCoworkAppAdapter()
    files = adapter.discover(split_cowork_dir)
    assert len(files) >= 1
    doc = adapter.export_one(files[0])
    assert doc["ocf_version"] == "0.1.0"


def test_adapter_variant_names() -> None:
    """Each adapter has a unique name for the tool registry."""
    names = {
        ClaudeCodeCliAdapter.name,
        ClaudeCodeAppAdapter.name,
        ClaudeCoworkAppAdapter.name,
    }
    assert names == {"claude_code_cli", "claude_code_app", "claude_cowork_app"}
