"""Tests for ocf.exporters.claude_ai against the claude.ai data export shape."""

from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

import pytest

from ocf.core.schema import validate
from ocf.exporters._base import SkipExport, export_all
from ocf.exporters.claude_ai import ENV_EXPORT_DIR, ClaudeAiAdapter

ROOT = "00000000-0000-4000-8000-000000000000"
CONV = "085ed72c-5df2-4daf-97b8-ef9b24ea275d"


def _msg(uuid, sender, parent, created, content, *, text="", attachments=(), files=()):
    return {
        "uuid": uuid, "text": text, "content": content, "sender": sender,
        "created_at": created, "updated_at": created,
        "attachments": list(attachments), "files": list(files),
        "parent_message_uuid": parent,
    }


def _conversation(updated="2026-10-12T07:30:35.036096Z", answer="Software is never solved."):
    human = _msg("m-1", "human", ROOT, "2026-10-12T07:25:17.704736Z", [
        {"type": "injected_prompt_block", "injection_source": "date_note", "prompt": "Today is ..."},
        {"type": "text", "text": "Is software solved?", "citations": []},
        {"type": "image", "file_uuid": "f-1", "source": {}},
    ], attachments=[{"file_name": "notes.txt", "file_size": 5, "file_type": "text/plain",
                     "extracted_content": "Hällo"}],
        files=[{"file_uuid": "f-1", "file_name": "chart.png"}])
    assistant = _msg("m-2", "assistant", "m-1", "2026-10-12T07:25:30.000000Z", [
        {"type": "thinking", "thinking": "", "thinking_hidden": True,
         "summaries": [{"summary": "Checking sources"}], "start_timestamp": "2026-10-12T07:25:30Z"},
        {"type": "text", "text": "Let me search.", "citations": []},
        {"type": "tool_use", "id": "toolu_1", "name": "web_search", "input": {"query": "software solved"}},
        {"type": "tool_result", "tool_use_id": "toolu_1", "name": "web_search", "is_error": False,
         "content": [{"type": "knowledge", "title": "An essay", "url": "https://example.org/essay",
                      "text": "Software is a process."}],
         "start_timestamp": "2026-10-12T07:25:40Z"},
        {"type": "text", "text": answer, "citations": [], "start_timestamp": "2026-10-12T07:25:45Z"},
    ])
    # an edited prompt: a second branch from the same root, newer
    edited = _msg("m-3", "human", ROOT, "2026-10-12T07:29:00.000000Z",
                  [{"type": "text", "text": "Is software solved yet?"}])
    reply = _msg("m-4", "assistant", "m-3", "2026-10-12T07:30:00.000000Z",
                 [{"type": "text", "text": "No."}])
    return {
        "uuid": CONV, "name": "Software is solved", "summary": "A short chat.",
        "created_at": "2026-10-12T07:25:17.704736Z", "updated_at": updated,
        "account": {"uuid": "acc-1"}, "chat_messages": [human, assistant, edited, reply],
    }


def _empty_conversation():
    return {"uuid": "e2ac3378-0000-4000-8000-000000000001", "name": "", "summary": "",
            "created_at": "2026-10-01T10:00:00Z", "updated_at": "2026-10-01T10:00:00Z",
            "account": {"uuid": "acc-1"},
            "chat_messages": [_msg("e-1", "human", ROOT, "2026-10-01T10:00:00Z", [])]}


def _write_export(folder: Path, conversations: list[dict]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    zpath = folder / "conversations-000.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("conversations.json", json.dumps(conversations))
    return zpath


@pytest.fixture()
def export_dir(tmp_path: Path) -> Path:
    root = tmp_path / "claude-exports"
    _write_export(root / "2026-11-05", [_conversation(), _empty_conversation()])
    return root


def _convert(export_dir: Path) -> dict:
    a = ClaudeAiAdapter()
    src = next(s for s in a.discover(export_dir) if str(s).endswith(CONV))
    return a.export_one(src)


def test_discover_needs_a_source_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_EXPORT_DIR, raising=False)
    with pytest.raises(FileNotFoundError):
        ClaudeAiAdapter().discover()


def test_discover_one_token_per_conversation(export_dir: Path) -> None:
    sources = ClaudeAiAdapter().discover(export_dir)
    assert len(sources) == 2
    assert all("conversations-000.zip::" in str(s) for s in sources)


def test_export_one_is_schema_valid(export_dir: Path) -> None:
    doc = _convert(export_dir)
    assert validate(doc) == []
    conv = doc["conversation"]
    assert conv["title"] == "Software is solved"
    assert conv["source"]["platform"] == "claude"
    assert conv["source"]["original_url"] == f"https://claude.ai/chat/{CONV}"
    assert conv["meta"]["claude_ai"]["summary"] == "A short chat."
    assert conv["meta"]["claude_ai"]["skipped"] == {"injected_prompt_block": 1}


def test_human_message_blocks(export_dir: Path) -> None:
    user = _convert(export_dir)["messages"][0]
    assert user["id"] == "m-1" and "parent_id" not in user
    content = user["message"]["content"]
    assert content[0] == {"type": "text", "text": "Is software solved?"}
    assert content[1] == {"type": "text", "text": "[Image: chart.png]"}
    data = base64.b64encode("Hällo".encode("utf-8")).decode("ascii")
    assert content[2] == {"type": "file", "file": {"file_data": f"data:text/plain;base64,{data}",
                                                   "filename": "notes.txt"}}


def test_assistant_turn_split_around_tool_result(export_dir: Path) -> None:
    msgs = _convert(export_dir)["messages"]
    first, tool, second = msgs[1], msgs[2], msgs[3]
    assert first["id"] == "m-2" and first["parent_id"] == "m-1"
    assert first["message"]["content"] == [
        {"type": "thinking", "thinking": "Checking sources"},
        {"type": "text", "text": "Let me search."},
    ]
    assert first["message"]["tool_calls"][0]["id"] == "toolu_1"
    assert json.loads(first["message"]["tool_calls"][0]["function"]["arguments"]) == {"query": "software solved"}
    assert tool["message"]["role"] == "tool" and tool["message"]["tool_call_id"] == "toolu_1"
    assert "[An essay](https://example.org/essay)" in tool["message"]["content"]
    assert "Software is a process." in tool["message"]["content"]
    assert tool["parent_id"] == first["id"]
    assert second["message"] == {"role": "assistant", "content": "Software is never solved."}
    assert second["parent_id"] == tool["id"]
    assert second["created_at"] == "2026-10-12T07:25:45Z"


def test_branches_kept_and_newest_leaf_active(export_dir: Path) -> None:
    doc = _convert(export_dir)
    by_id = {m["id"]: m for m in doc["messages"]}
    assert "parent_id" not in by_id["m-3"]  # second root branch (edited prompt)
    assert by_id["m-4"]["parent_id"] == "m-3"
    assert doc["conversation"]["active_message_id"] == "m-4"


def test_unmatched_tool_result_gets_synthesized_call(tmp_path: Path) -> None:
    conv = _conversation()
    conv["chat_messages"][1]["content"] = [
        {"type": "tool_result", "tool_use_id": "toolu_x", "name": "memory_read",
         "content": [{"type": "text", "text": "remembered"}], "is_error": True},
    ]
    root = tmp_path / "exp"
    _write_export(root, [conv])
    a = ClaudeAiAdapter()
    doc = a.export_one(a.discover(root)[0])
    assert validate(doc) == []
    call, tool = doc["messages"][1], doc["messages"][2]
    assert call["message"]["tool_calls"][0]["id"] == "toolu_x"
    assert call["message"]["tool_calls"][0]["id_origin"] == "synthesized"
    assert tool["message"]["tool_call_id"] == "toolu_x" and tool["status"] == "error"


def test_empty_conversation_is_skipped(export_dir: Path, tmp_path: Path) -> None:
    a = ClaudeAiAdapter()
    empty = next(s for s in a.discover(export_dir) if not str(s).endswith(CONV))
    with pytest.raises(SkipExport):
        a.export_one(empty)
    out = tmp_path / "out"
    result = export_all(a, out, source_dirs=export_dir)
    assert len(result.new) == 1 and len(result.skipped) == 1 and not result.failed
    assert [p.name for p in out.glob("*.ocf.json")] == [f"claude-ai-{CONV}.ocf.json"]


def test_newest_snapshot_wins_across_exports(export_dir: Path) -> None:
    _write_export(export_dir / "2026-12-05", [_conversation(updated="2026-11-20T09:00:00Z",
                                                            answer="Still not solved.")])
    a = ClaudeAiAdapter()
    sources = [s for s in a.discover(export_dir) if str(s).endswith(CONV)]
    assert len(sources) == 1 and "2026-12-05" in str(sources[0])
    assert a.export_one(sources[0])["messages"][3]["message"]["content"] == "Still not solved."


def test_fingerprint_depends_on_conversation_content(export_dir: Path, tmp_path: Path) -> None:
    a = ClaudeAiAdapter()
    src = next(s for s in a.discover(export_dir) if str(s).endswith(CONV))
    copy_dir = tmp_path / "copy"
    _write_export(copy_dir, [_conversation()])
    same = a.discover(copy_dir)[0]
    assert a.source_fingerprint(src)[2] == a.source_fingerprint(same)[2]
    changed_dir = tmp_path / "changed"
    _write_export(changed_dir, [_conversation(answer="Changed.")])
    assert a.source_fingerprint(a.discover(changed_dir)[0])[2] != a.source_fingerprint(src)[2]


def test_extracted_conversations_json_is_read(tmp_path: Path) -> None:
    folder = tmp_path / "extracted"
    folder.mkdir()
    (folder / "conversations.json").write_text(json.dumps([_conversation()]), encoding="utf-8")
    sources = ClaudeAiAdapter().discover(folder)
    assert [str(s).rsplit("::", 1)[1] for s in sources] == [CONV]
