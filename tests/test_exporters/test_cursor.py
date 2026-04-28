"""Tests for ocf.exporters.cursor.

Mirrors the pattern of test_codex / test_claude_code:

1. Single-composer conversion against a synthetic state.vscdb fixture
   that mimics the real cursorDiskKV format ChatSyncer reverse-engineered.
2. Bulk export with manifest + skip detection.
3. Real-machine smoke against ``%APPDATA%/Cursor/User/globalStorage/state.vscdb``
   when present.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ocf.core.schema import is_valid, validate
from ocf.exporters.cursor import (
    CursorAdapter,
    _make_source_token,
    _split_source_token,
    discover,
    export_all,
    export_one,
    find_by_id,
    find_by_name,
    resolve_sources,
)


# ---------------------------------------------------------------------------
# Synthetic fixture: a state.vscdb with cursorDiskKV table mimicking real format
# ---------------------------------------------------------------------------

COMPOSER_ID = "11111111-2222-3333-4444-555555555555"
BUBBLE_USER_ID = "b-user-001"
BUBBLE_ASSISTANT_ID = "b-asst-002"
SECOND_COMPOSER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _build_state_vscdb(
    db_path: Path,
    *,
    composer_id: str = COMPOSER_ID,
    title: str = "Async-Diskussion",
    workspace_folder: str = "/home/user/projects/demo",
    model: str = "cursor-large",
    include_extra_keys: bool = True,
    include_tool_bubble: bool = False,
) -> None:
    """Create a minimal state.vscdb mimicking real Cursor storage."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        composer_obj = {
            "_v": 3,
            "composerId": composer_id,
            "workspaceFolder": workspace_folder,
            "title": title,
            "latestConversationSummary": {"model": model, "title": title},
            "fullConversationHeadersOnly": [
                {"bubbleId": BUBBLE_USER_ID},
                {"bubbleId": BUBBLE_ASSISTANT_ID},
            ],
            "conversation": [],  # empty -> separate-storage mode
            "createdAt": "2026-04-24T09:00:00Z",
        }
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"composerData:{composer_id}", json.dumps(composer_obj)),
        )

        # Bubbles
        bubble_user = {
            "_v": 2,
            "bubbleId": BUBBLE_USER_ID,
            "type": 1,
            "text": "Erklär mir async/await",
            "createdAt": "2026-04-24T09:00:00Z",
        }
        bubble_assistant = {
            "_v": 2,
            "bubbleId": BUBBLE_ASSISTANT_ID,
            "type": 2,
            "text": "async/await sind Syntax-Zucker für Promises.",
            "thinking": "Nutzer fragt nach async basics.",
            "codeBlocks": [{"language": "python", "code": "async def f():\n    pass"}],
            "createdAt": "2026-04-24T09:00:05Z",
        }
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"bubbleId:{composer_id}:{BUBBLE_USER_ID}", json.dumps(bubble_user)),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                f"bubbleId:{composer_id}:{BUBBLE_ASSISTANT_ID}",
                json.dumps(bubble_assistant),
            ),
        )

        if include_extra_keys:
            # Out-of-scope keys that the adapter MUST ignore
            conn.execute(
                "INSERT INTO cursorDiskKV VALUES (?, ?)",
                ("agentKv:bg-1", json.dumps({"shouldBeIgnored": True})),
            )
            conn.execute(
                "INSERT INTO cursorDiskKV VALUES (?, ?)",
                ("checkpointId:cp-1", json.dumps({"shouldBeIgnored": True})),
            )
            conn.execute(
                "INSERT INTO cursorDiskKV VALUES (?, ?)",
                ("codeBlockDiff:foo", json.dumps({"shouldBeIgnored": True})),
            )

        if include_tool_bubble:
            # Tool bubble — the most common real-world shape (~71% of bubbles)
            # carries toolFormerData with a tool call + result.
            tool_bubble_id = "b-tool-003"
            tool_bubble = {
                "_v": 2,
                "bubbleId": tool_bubble_id,
                "type": 2,
                # No text/thinking/codeBlocks — pure tool bubble
                "toolFormerData": {
                    "tool": 7,
                    "toolIndex": 1,
                    "modelCallId": "model_call_xyz",
                    "toolCallId": "toolu_01edit",
                    "name": "edit_file_v2",
                    "rawArgs": '{"relativeWorkspacePath":"foo.py","content":"def f(): pass"}',
                    "params": '{"relativeWorkspacePath":"foo.py"}',
                    "result": '{"afterContentId":"sha256:abc"}',
                    "status": "completed",
                    "userDecision": "accepted",
                },
                "tokenCount": {"inputTokens": 100, "outputTokens": 50},
                "createdAt": "2026-04-24T09:00:10Z",
            }
            conn.execute(
                "INSERT INTO cursorDiskKV VALUES (?, ?)",
                (
                    f"bubbleId:{composer_id}:{tool_bubble_id}",
                    json.dumps(tool_bubble),
                ),
            )
            # Add it to the headers so it appears in the conversation order
            cur = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?",
                (f"composerData:{composer_id}",),
            )
            row = cur.fetchone()
            composer_obj_existing = json.loads(row[0])
            composer_obj_existing["fullConversationHeadersOnly"].append(
                {"bubbleId": tool_bubble_id}
            )
            conn.execute(
                "UPDATE cursorDiskKV SET value = ? WHERE key = ?",
                (
                    json.dumps(composer_obj_existing),
                    f"composerData:{composer_id}",
                ),
            )

        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def cursor_state_db(tmp_path: Path) -> Path:
    """Single-composer state.vscdb (no tool bubble)."""
    db = tmp_path / "state.vscdb"
    _build_state_vscdb(db)
    return db


@pytest.fixture()
def cursor_state_db_with_tool(tmp_path: Path) -> Path:
    """Single-composer state.vscdb including a toolFormerData bubble."""
    db = tmp_path / "state.vscdb"
    _build_state_vscdb(db, include_tool_bubble=True)
    return db


@pytest.fixture()
def cursor_global_storage_dir(tmp_path: Path) -> Path:
    """A globalStorage directory containing a state.vscdb with two composers."""
    gs = tmp_path / "globalStorage"
    gs.mkdir()
    db = gs / "state.vscdb"
    _build_state_vscdb(
        db,
        composer_id=COMPOSER_ID,
        title="Async-Diskussion",
        workspace_folder="/home/user/projects/demo",
    )
    # Add a second composer in the same DB
    conn = sqlite3.connect(db)
    try:
        composer_obj = {
            "_v": 3,
            "composerId": SECOND_COMPOSER_ID,
            "workspaceFolder": "/home/user/projects/other",
            "title": "Refactor inventory",
            "latestConversationSummary": {
                "model": "cursor-large",
                "title": "Refactor inventory",
            },
            "fullConversationHeadersOnly": [{"bubbleId": "b-x"}],
            "conversation": [],
            "createdAt": "2026-04-25T09:00:00Z",
        }
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"composerData:{SECOND_COMPOSER_ID}", json.dumps(composer_obj)),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                f"bubbleId:{SECOND_COMPOSER_ID}:b-x",
                json.dumps(
                    {
                        "_v": 2,
                        "bubbleId": "b-x",
                        "type": 1,
                        "text": "How do I refactor this?",
                        "createdAt": "2026-04-25T09:00:00Z",
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return gs


# ---------------------------------------------------------------------------
# Token round-trip
# ---------------------------------------------------------------------------

def test_token_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    db.touch()
    token = _make_source_token(db, "abc-123")
    parsed_db, parsed_id = _split_source_token(token)
    # Compare as posix strings (Path normalization differs across platforms)
    assert parsed_db.as_posix() == db.as_posix()
    assert parsed_id == "abc-123"


def test_split_source_token_rejects_non_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Cursor source token"):
        _split_source_token(tmp_path / "regular_file.json")


# ---------------------------------------------------------------------------
# discover() / find_by_name() / find_by_id()
# ---------------------------------------------------------------------------

def test_discover_enumerates_composers(
    cursor_global_storage_dir: Path,
) -> None:
    tokens = discover(cursor_global_storage_dir)
    assert len(tokens) == 2


def test_discover_skips_dbs_without_table(tmp_path: Path) -> None:
    """A state.vscdb without cursorDiskKV table is skipped, not raised."""
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE other (k TEXT, v TEXT)")
        conn.commit()
    finally:
        conn.close()
    tokens = discover(tmp_path)
    assert tokens == []


def test_find_by_name_matches_title(cursor_global_storage_dir: Path) -> None:
    matches = find_by_name("Async", source_dir=cursor_global_storage_dir)
    assert len(matches) == 1
    _, cid = _split_source_token(matches[0])
    assert cid == COMPOSER_ID


def test_find_by_name_matches_workspace_folder(
    cursor_global_storage_dir: Path,
) -> None:
    matches = find_by_name("inventory", source_dir=cursor_global_storage_dir)
    assert len(matches) == 1


def test_find_by_id_uuid_match(cursor_global_storage_dir: Path) -> None:
    matches = find_by_id(COMPOSER_ID, source_dir=cursor_global_storage_dir)
    assert len(matches) == 1


def test_find_by_id_partial_uuid(cursor_global_storage_dir: Path) -> None:
    matches = find_by_id("11111111", source_dir=cursor_global_storage_dir)
    assert len(matches) == 1


def test_find_by_name_no_match(cursor_global_storage_dir: Path) -> None:
    assert find_by_name("nonexistent", source_dir=cursor_global_storage_dir) == []


# ---------------------------------------------------------------------------
# export_one
# ---------------------------------------------------------------------------

def test_export_one_validates_schema(cursor_state_db: Path) -> None:
    token = _make_source_token(cursor_state_db, COMPOSER_ID)
    doc = export_one(token)
    errors = validate(doc)
    assert errors == [], [e.message for e in errors]


def test_export_one_title_from_composer(cursor_state_db: Path) -> None:
    token = _make_source_token(cursor_state_db, COMPOSER_ID)
    doc = export_one(token)
    assert doc["conversation"]["title"] == "Async-Diskussion"


def test_export_one_default_model(cursor_state_db: Path) -> None:
    token = _make_source_token(cursor_state_db, COMPOSER_ID)
    doc = export_one(token)
    assert doc["conversation"]["default_model"] == "cursor-large"


def test_export_one_project_from_workspace_folder(cursor_state_db: Path) -> None:
    token = _make_source_token(cursor_state_db, COMPOSER_ID)
    doc = export_one(token)
    proj = doc["conversation"]["project"]
    assert proj["description"] == "/home/user/projects/demo"
    assert proj["name"] == "demo"


def test_export_one_messages_in_header_order(cursor_state_db: Path) -> None:
    token = _make_source_token(cursor_state_db, COMPOSER_ID)
    doc = export_one(token)
    ids = [m["id"] for m in doc["messages"]]
    assert ids == [BUBBLE_USER_ID, BUBBLE_ASSISTANT_ID]


def test_export_one_user_bubble_to_user_role(cursor_state_db: Path) -> None:
    token = _make_source_token(cursor_state_db, COMPOSER_ID)
    doc = export_one(token)
    user_msg = doc["messages"][0]
    assert user_msg["message"]["role"] == "user"
    assert user_msg["message"]["content"] == "Erklär mir async/await"


def test_export_one_assistant_with_thinking_and_code_blocks(
    cursor_state_db: Path,
) -> None:
    """Cursor's codeBlocks become first-class OCF code blocks (lang preserved)."""
    token = _make_source_token(cursor_state_db, COMPOSER_ID)
    doc = export_one(token)
    asst = doc["messages"][1]
    assert asst["message"]["role"] == "assistant"
    content = asst["message"]["content"]
    assert isinstance(content, list)
    types = [b["type"] for b in content]
    assert "text" in types
    assert "thinking" in types
    assert "code" in types
    code_block = next(b for b in content if b["type"] == "code")
    assert code_block["language"] == "python"
    assert code_block["code"] == "async def f():\n    pass"


def test_export_one_extra_keys_ignored(cursor_state_db: Path) -> None:
    """agentKv / checkpointId / codeBlockDiff entries don't leak into OCF."""
    token = _make_source_token(cursor_state_db, COMPOSER_ID)
    doc = export_one(token)
    # Only the two real bubbles produced messages
    assert len(doc["messages"]) == 2


def test_export_one_meta_cursor_includes_composer_v(
    cursor_state_db: Path,
) -> None:
    token = _make_source_token(cursor_state_db, COMPOSER_ID)
    doc = export_one(token)
    cm = doc["conversation"]["meta"]["cursor"]
    assert cm["_v"] == 3
    assert cm["composer_id"] == COMPOSER_ID
    assert cm["raw_format"] == "cursor-disk-kv"


def test_export_one_id_origin_source(cursor_state_db: Path) -> None:
    """Bubble UUIDs are real source IDs, not synthesized."""
    token = _make_source_token(cursor_state_db, COMPOSER_ID)
    doc = export_one(token)
    for m in doc["messages"]:
        assert m["id_origin"] == "source"


def test_export_one_unknown_composer_raises(cursor_state_db: Path) -> None:
    bogus = _make_source_token(cursor_state_db, "no-such-composer")
    with pytest.raises(ValueError, match="No composerData"):
        export_one(bogus)


# ---------------------------------------------------------------------------
# toolFormerData extraction (the 71% case)
# ---------------------------------------------------------------------------

def test_export_one_tool_bubble_produces_two_envelopes(
    cursor_state_db_with_tool: Path,
) -> None:
    """One tool bubble -> assistant tool_call envelope + role:tool envelope."""
    token = _make_source_token(cursor_state_db_with_tool, COMPOSER_ID)
    doc = export_one(token)
    # 2 normal bubbles (user, assistant) + 1 tool bubble × 2 envelopes = 4 total
    assert len(doc["messages"]) == 4


def test_tool_bubble_assistant_envelope_has_tool_calls(
    cursor_state_db_with_tool: Path,
) -> None:
    token = _make_source_token(cursor_state_db_with_tool, COMPOSER_ID)
    doc = export_one(token)
    # Find the tool-call envelope (third in the order: user, asst-content, asst-tool, tool-result)
    asst_with_tool = next(
        m
        for m in doc["messages"]
        if m["message"]["role"] == "assistant" and m["message"].get("tool_calls")
    )
    tcs = asst_with_tool["message"]["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "toolu_01edit"
    assert tcs[0]["id_origin"] == "source"
    assert tcs[0]["function"]["name"] == "edit_file_v2"
    # rawArgs preferred over params (raw model args)
    assert "streamingContent" in tcs[0]["function"]["arguments"] or "content" in tcs[0]["function"]["arguments"]


def test_tool_bubble_result_envelope_pairs_with_call(
    cursor_state_db_with_tool: Path,
) -> None:
    token = _make_source_token(cursor_state_db_with_tool, COMPOSER_ID)
    doc = export_one(token)
    tool_msg = next(m for m in doc["messages"] if m["message"]["role"] == "tool")
    assert tool_msg["message"]["tool_call_id"] == "toolu_01edit"
    assert "afterContentId" in tool_msg["message"]["content"]
    # synthesized id (we generate <bubble_id>-result)
    assert tool_msg["id"] == "b-tool-003-result"
    assert tool_msg["id_origin"] == "synthesized"


def test_tool_bubble_completed_status_no_explicit_status(
    cursor_state_db_with_tool: Path,
) -> None:
    """status='completed' -> we omit the OCF status field (=ok by default)."""
    token = _make_source_token(cursor_state_db_with_tool, COMPOSER_ID)
    doc = export_one(token)
    tool_msg = next(m for m in doc["messages"] if m["message"]["role"] == "tool")
    assert tool_msg.get("status") in (None, "ok")


def test_tool_bubble_error_status_propagates(tmp_path: Path) -> None:
    """status='error' -> OCF status='error'."""
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        composer = {
            "_v": 3,
            "name": "Test",
            "fullConversationHeadersOnly": [{"bubbleId": "b-1"}],
            "conversation": [],
        }
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"composerData:cid-err", json.dumps(composer)),
        )
        bubble = {
            "_v": 2,
            "type": 2,
            "toolFormerData": {
                "toolCallId": "toolu_err",
                "name": "edit_file_v2",
                "rawArgs": "{}",
                "result": "Permission denied",
                "status": "error",
                "error": "Permission denied",
            },
        }
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"bubbleId:cid-err:b-1", json.dumps(bubble)),
        )
        conn.commit()
    finally:
        conn.close()

    token = _make_source_token(db, "cid-err")
    doc = export_one(token)
    tool_msg = next(m for m in doc["messages"] if m["message"]["role"] == "tool")
    assert tool_msg.get("status") == "error"


def test_tool_bubble_cancelled_status(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                "composerData:cid-c",
                json.dumps(
                    {
                        "_v": 3,
                        "name": "T",
                        "fullConversationHeadersOnly": [{"bubbleId": "b-c"}],
                        "conversation": [],
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                "bubbleId:cid-c:b-c",
                json.dumps(
                    {
                        "_v": 2,
                        "type": 2,
                        "toolFormerData": {
                            "toolCallId": "toolu_c",
                            "name": "run_terminal_command_v2",
                            "rawArgs": '{"command":"rm -rf /"}',
                            "result": "{}",
                            "status": "cancelled",
                            "userDecision": "rejected",
                        },
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    token = _make_source_token(db, "cid-c")
    doc = export_one(token)
    tool_msg = next(m for m in doc["messages"] if m["message"]["role"] == "tool")
    assert tool_msg.get("status") == "cancelled"


def test_tool_call_cross_reference_validates(
    cursor_state_db_with_tool: Path,
) -> None:
    """Tool message's tool_call_id MUST match the prior assistant tool_calls[].id —
    OCF schema validation rule 6."""
    token = _make_source_token(cursor_state_db_with_tool, COMPOSER_ID)
    doc = export_one(token)
    # Schema validation already enforced by validate=True in export_one,
    # but make the cross-reference explicit:
    issued = set()
    for m in doc["messages"]:
        if m["message"]["role"] == "assistant":
            for tc in m["message"].get("tool_calls") or []:
                issued.add(tc["id"])
        elif m["message"]["role"] == "tool":
            assert m["message"]["tool_call_id"] in issued


def test_tool_bubble_unparseable_args_wrapped(tmp_path: Path) -> None:
    """rawArgs that don't parse as JSON should be wrapped, not break export."""
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                "composerData:cid-x",
                json.dumps(
                    {
                        "_v": 3,
                        "name": "T",
                        "fullConversationHeadersOnly": [{"bubbleId": "b-x"}],
                        "conversation": [],
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                "bubbleId:cid-x:b-x",
                json.dumps(
                    {
                        "_v": 2,
                        "type": 2,
                        "toolFormerData": {
                            "toolCallId": "toolu_garbled",
                            "name": "exotic_tool",
                            "rawArgs": "not json at all <garbled>",
                            "status": "completed",
                        },
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    token = _make_source_token(db, "cid-x")
    doc = export_one(token)
    # Should not raise; arguments wrapped as JSON-string
    asst = next(
        m for m in doc["messages"] if m["message"]["role"] == "assistant"
    )
    args = asst["message"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args)["_unparseable_args"] == "not json at all <garbled>"


# ---------------------------------------------------------------------------
# resolve_sources polymorphic
# ---------------------------------------------------------------------------

def test_resolve_sources_uuid_query(
    cursor_global_storage_dir: Path,
) -> None:
    files = resolve_sources(COMPOSER_ID, source_dir=cursor_global_storage_dir)
    assert len(files) == 1


def test_resolve_sources_text_query(cursor_global_storage_dir: Path) -> None:
    files = resolve_sources(
        "Refactor", source_dir=cursor_global_storage_dir
    )
    assert len(files) == 1


def test_resolve_sources_directory(cursor_global_storage_dir: Path) -> None:
    files = resolve_sources(cursor_global_storage_dir)
    assert len(files) == 2


# ---------------------------------------------------------------------------
# Source fingerprint / manifest skip detection
# ---------------------------------------------------------------------------

def test_source_fingerprint_changes_on_bubble_update(
    cursor_global_storage_dir: Path, tmp_path: Path
) -> None:
    """Modifying a bubble row must change the fingerprint."""
    adapter = CursorAdapter()
    tokens = adapter.discover(cursor_global_storage_dir)
    target = next(t for t in tokens if COMPOSER_ID in str(t))

    fp1 = adapter.source_fingerprint(target)

    # Modify the assistant bubble
    db = cursor_global_storage_dir / "state.vscdb"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE cursorDiskKV SET value = ? WHERE key = ?",
            (
                json.dumps({"_v": 2, "type": 2, "text": "new content"}),
                f"bubbleId:{COMPOSER_ID}:{BUBBLE_ASSISTANT_ID}",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    fp2 = adapter.source_fingerprint(target)
    assert fp1[2] != fp2[2], "sha256 component must change when bubble changes"


def _build_state_vscdb_with_null_bubble(db_path: Path) -> str:
    """Build a state.vscdb whose schema permits NULL ``value`` rows.

    The fixture's normal schema has ``value TEXT NOT NULL``, but real
    Cursor databases observed on disk DO permit NULL bubble values
    (encountered in 6/349 composers in production). This helper
    mirrors the relaxed schema so we can regression-test the
    NULL-tolerance of :meth:`CursorAdapter.source_fingerprint`.
    """
    composer_id = "00000000-1111-2222-3333-444444444444"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # Note: no NOT NULL constraint — matches real Cursor schema.
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        composer_obj = {
            "_v": 3,
            "composerId": composer_id,
            "title": "Session with NULL bubble",
            "fullConversationHeadersOnly": [
                {"bubbleId": "b-good"},
                {"bubbleId": "b-null"},  # references the NULL row
            ],
            "conversation": [],
            "createdAt": "2026-04-24T09:00:00Z",
        }
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"composerData:{composer_id}", json.dumps(composer_obj)),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                f"bubbleId:{composer_id}:b-good",
                json.dumps(
                    {
                        "_v": 2,
                        "bubbleId": "b-good",
                        "type": 1,
                        "text": "Hello",
                        "createdAt": "2026-04-24T09:00:00Z",
                    }
                ),
            ),
        )
        # The pathological row: key present, value NULL.
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, NULL)",
            (f"bubbleId:{composer_id}:b-null",),
        )
        conn.commit()
    finally:
        conn.close()
    return composer_id


def test_source_fingerprint_tolerates_null_bubble_value(
    tmp_path: Path,
) -> None:
    """Regression: a NULL ``value`` row in cursorDiskKV must not crash
    fingerprint computation. Reproduces the production failure where
    6/349 real composers crashed with ``AttributeError: 'NoneType'
    object has no attribute 'encode'``.
    """
    db = tmp_path / "state.vscdb"
    composer_id = _build_state_vscdb_with_null_bubble(db)
    token = _make_source_token(db, composer_id)

    adapter = CursorAdapter()
    # Must not raise.
    fp = adapter.source_fingerprint(token)
    assert isinstance(fp[2], str)
    assert len(fp[2]) == 64  # sha256 hex


def test_source_fingerprint_distinguishes_null_from_empty_string(
    tmp_path: Path,
) -> None:
    """A NULL value and an empty-string value must hash to different
    fingerprints — otherwise a real content change ('' -> NULL or vice
    versa) would be silently skipped by the manifest.
    """
    db_null = tmp_path / "null.vscdb"
    db_empty = tmp_path / "empty.vscdb"
    composer_id = _build_state_vscdb_with_null_bubble(db_null)

    # Build a parallel DB where the same row has '' instead of NULL.
    _build_state_vscdb_with_null_bubble(db_empty)
    conn = sqlite3.connect(db_empty)
    try:
        conn.execute(
            "UPDATE cursorDiskKV SET value = '' WHERE key = ?",
            (f"bubbleId:{composer_id}:b-null",),
        )
        conn.commit()
    finally:
        conn.close()

    adapter = CursorAdapter()
    fp_null = adapter.source_fingerprint(_make_source_token(db_null, composer_id))
    fp_empty = adapter.source_fingerprint(_make_source_token(db_empty, composer_id))
    assert fp_null[2] != fp_empty[2]


def test_discover_skips_zombie_composers_with_null_value(tmp_path: Path) -> None:
    """Regression: composer rows whose ``value`` is NULL ("zombies" — keys
    exist in cursorDiskKV but the payload was wiped) must be filtered
    out at discovery time. Otherwise they surface downstream as
    ``ValueError: No composerData for ...`` failures (observed: 4/349
    composers in a real production DB).
    """
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        # Real composer
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                "composerData:real-aaa",
                json.dumps(
                    {
                        "_v": 3,
                        "composerId": "real-aaa",
                        "fullConversationHeadersOnly": [],
                        "conversation": [],
                        "createdAt": "2026-04-24T09:00:00Z",
                    }
                ),
            ),
        )
        # Zombie composer — key present, value NULL
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, NULL)",
            ("composerData:zombie-bbb",),
        )
        conn.commit()
    finally:
        conn.close()

    tokens = discover(tmp_path)
    # Only the real composer is returned; the zombie was filtered.
    composer_ids = [_split_source_token(t)[1] for t in tokens]
    assert composer_ids == ["real-aaa"]


def test_export_one_empty_composer_raises_skip(tmp_path: Path) -> None:
    """Cursor creates a composer row whenever the user clicks "New
    Chat", even before they type anything (195/345 composers on a real
    machine). These have no bubbles and no archival value — the
    adapter must signal a skip via :class:`SkipExport`, not produce a
    schema-valid-but-empty OCF document.
    """
    from ocf.exporters._base import SkipExport
    db = tmp_path / "state.vscdb"
    composer_id = "abcd1234-0000-0000-0000-000000000000"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        # Composer with empty headers — no bubbles linked.
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                f"composerData:{composer_id}",
                json.dumps(
                    {
                        "_v": 3,
                        "composerId": composer_id,
                        "fullConversationHeadersOnly": [],
                        "conversation": [],
                        "createdAt": "2026-04-24T09:00:00Z",
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    token = _make_source_token(db, composer_id)
    with pytest.raises(SkipExport, match="no messages"):
        export_one(token)


def test_export_all_records_skipped_empty_composer(tmp_path: Path) -> None:
    """End-to-end: an empty composer must end up in ``result.skipped``
    (not ``failed``), and the manifest must remember the skip so the
    next run doesn't re-evaluate it."""
    db = tmp_path / "state.vscdb"
    composer_id = "abcd1234-0000-0000-0000-000000000001"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                f"composerData:{composer_id}",
                json.dumps(
                    {
                        "_v": 3,
                        "composerId": composer_id,
                        "fullConversationHeadersOnly": [],
                        "conversation": [],
                        "createdAt": "2026-04-24T09:00:00Z",
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    out = tmp_path / "out"
    result = export_all(out, source_dir=tmp_path)
    assert len(result.skipped) == 1
    assert len(result.new) == 0
    assert len(result.failed) == 0
    # No OCF file written
    assert not list(out.glob("*.ocf.json"))
    # Second run must still skip without re-doing work.
    result2 = export_all(out, source_dir=tmp_path)
    assert len(result2.skipped) == 1
    assert len(result2.new) == 0


def test_export_one_with_null_bubble_succeeds(tmp_path: Path) -> None:
    """End-to-end: a composer containing a NULL bubble should export
    successfully — the NULL bubble is silently skipped, the rest is
    converted normally.
    """
    db = tmp_path / "state.vscdb"
    composer_id = _build_state_vscdb_with_null_bubble(db)
    token = _make_source_token(db, composer_id)

    doc = export_one(token)
    assert is_valid(doc)
    # Only the non-NULL bubble produced a message.
    assert len(doc["messages"]) == 1
    assert doc["messages"][0]["message"]["role"] == "user"


# ---------------------------------------------------------------------------
# export_all bulk + manifest
# ---------------------------------------------------------------------------

def test_export_all_creates_files(
    cursor_global_storage_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    result = export_all(out, source_dir=cursor_global_storage_dir)
    assert len(result.new) == 2
    assert len(result.failed) == 0


def test_export_all_idempotent(
    cursor_global_storage_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    export_all(out, source_dir=cursor_global_storage_dir)
    second = export_all(out, source_dir=cursor_global_storage_dir)
    assert len(second.skipped) == 2
    assert len(second.new) == 0


# ---------------------------------------------------------------------------
# Real-machine smoke
# ---------------------------------------------------------------------------

REAL_GLOBAL_STORAGE_DB = (
    Path.home()
    / "AppData"
    / "Roaming"
    / "Cursor"
    / "User"
    / "globalStorage"
    / "state.vscdb"
)


@pytest.fixture()
def real_cursor_token() -> Path:
    """Pick a single composer from the real Cursor DB. Skips when unavailable."""
    if not REAL_GLOBAL_STORAGE_DB.exists():
        pytest.skip("No real Cursor state.vscdb on this machine")
    adapter = CursorAdapter()
    tokens = adapter.discover(REAL_GLOBAL_STORAGE_DB.parent)
    if not tokens:
        pytest.skip("Cursor DB exists but has no composerData entries")
    # Pick one in the middle to avoid the very first/last (those tend to
    # be edge cases — empty or in-progress sessions).
    return tokens[len(tokens) // 2]


def test_real_cursor_session_exports_to_valid_ocf(
    real_cursor_token: Path,
) -> None:
    """Smoke: real Cursor composer converts and validates.

    On a real machine roughly 56% of composers are empty (user clicked
    "New Chat" but never typed anything) and now skip via SkipExport.
    The smoke test must tolerate landing on one of those — keep
    sampling middle-ish tokens until we find one with content.
    """
    from ocf.exporters._base import SkipExport
    from ocf.exporters.cursor import CursorAdapter

    # If the picked token is empty, walk forward until we find a real one.
    adapter = CursorAdapter()
    all_tokens = adapter.discover()
    start = all_tokens.index(real_cursor_token) if real_cursor_token in all_tokens else 0
    last_skip: SkipExport | None = None
    for token in all_tokens[start:] + all_tokens[:start]:
        try:
            doc = export_one(token, validate=False)
        except SkipExport as exc:
            last_skip = exc
            continue
        errors = validate(doc)
        if errors:
            details = "\n".join(
                f"  - {e.json_path}: {e.message}" for e in errors[:5]
            )
            pytest.fail(
                f"Real Cursor session produced {len(errors)} "
                f"validation errors:\n{details}"
            )
        return
    pytest.skip(
        f"All real composers skipped (empty); last reason: {last_skip}"
    )
