"""Tests for ocf.indexers.meilisearch - archive-based indexing (no server)."""

from __future__ import annotations

import json
from pathlib import Path

from ocf.exporters.claude_code import ClaudeCodeCliAdapter, export_one
from ocf.exporters._base import export_all
from ocf.indexers.meilisearch import (
    archive_tool_name,
    iter_archive_documents,
    iter_recovered_documents,
    make_chunk_documents,
)

SID = "11111111-2222-3333-4444-555555555555"


def _session_jsonl(path: Path, text: str = "Hello") -> Path:
    events = [
        {"type": "user", "timestamp": "2026-04-26T10:00:05Z", "cwd": "/home/u/demo",
         "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
        {"type": "assistant", "timestamp": "2026-04-26T10:00:10Z",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "text", "text": "Hi"}]}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def _ocf(source: Path) -> dict:
    return export_one(source)


def test_archive_tool_name_rules() -> None:
    tagged = {"conversation": {"meta": {"ocf_py": {"source_adapter": "claude-code-app"}},
                               "source": {"platform": "claude_code"}}}
    claude = {"conversation": {"source": {"platform": "claude_code"}}}
    assert archive_tool_name(tagged) == "claude-code-app"
    assert archive_tool_name({"conversation": {"source": {"platform": "codex"}}}) == "codex"
    assert archive_tool_name({"conversation": {"source": {"platform": "cursor"}}}) == "cursor"
    cowork = r"C:\Users\u\AppData\Local\Claude-Data\claude_x\local-agent-mode-sessions\a\b\c.jsonl"
    assert archive_tool_name(claude, cowork) == "claude-cowork-app"
    assert archive_tool_name(claude, "/home/u/.claude/projects/p/x.jsonl") == "claude-code"
    assert archive_tool_name(claude) == "claude-code"


def test_iter_archive_documents(tmp_path: Path) -> None:
    archive = tmp_path / "ocf-archive"
    archive.mkdir()
    doc = _ocf(_session_jsonl(tmp_path / "src" / f"{SID}.jsonl"))
    (archive / f"{SID}.ocf.json").write_text(json.dumps(doc), encoding="utf-8")
    agent = _ocf(_session_jsonl(tmp_path / "src" / "agent-a1b2.jsonl", "Sub task"))
    (archive / "agent-a1b2.ocf.json").write_text(json.dumps(agent), encoding="utf-8")
    empty = dict(doc, messages=[])
    (archive / "empty.ocf.json").write_text(json.dumps(empty), encoding="utf-8")
    (archive / "broken.ocf.json").write_text("{not json", encoding="utf-8")
    (archive / ".ocf-manifest.json").write_text(json.dumps({"exports": {
        "C:/x/local-agent-mode-sessions/w/agent-a1b2.jsonl": {
            "ocf_path": "agent-a1b2.ocf.json",
            "source_path": "C:/x/local-agent-mode-sessions/w/agent-a1b2.jsonl"}}}),
        encoding="utf-8")

    out = {p.name: (d, problem) for p, d, problem in iter_archive_documents(archive)}

    chunks, problem = out[f"{SID}.ocf.json"]
    assert problem is None and len(chunks) == 1
    (c,) = chunks
    assert c["tool"] == "claude-code"
    assert (c["id"], c["doc"], c["session_id"]) == (f"{SID}__c1", SID, SID)
    assert (c["chunk"], c["chunks"], c["msg_from"], c["msg_to"]) == (1, 1, 0, 1)
    assert c["created_date"] == "2026-04-26" and c["updated_date"] == "2026-04-26"
    assert c["chunk_from"] == c["created_at"] and c["chunk_to"] >= c["chunk_from"]
    assert c["session_messages"] == 2 and c["session_words"] == c["words"] > 0
    assert c["archive_bytes"] == (archive / f"{SID}.ocf.json").stat().st_size
    assert "Hello" in c["content"] and "Exported on" not in c["content"]

    (agent_chunk,), _ = out["agent-a1b2.ocf.json"]
    assert agent_chunk["tool"] == "claude-cowork-app"
    assert agent_chunk["id"] == "agent-a1b2__c1"

    assert out["empty.ocf.json"] == (None, "empty")
    assert out["broken.ocf.json"][0] is None
    assert out["broken.ocf.json"][1].startswith("unreadable")


def _long_doc(turns: int, words_per_turn: int) -> dict:
    messages = []
    for i in range(turns):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({
            "id": f"m{i}", "created_at": f"2026-10-{1 + i // 4:02d}T10:00:00Z",
            "message": {"role": role, "content": " ".join(f"w{i}x{j}" for j in range(words_per_turn))},
        })
        if role == "assistant":
            messages.append({"id": f"t{i}", "message": {"role": "tool", "tool_call_id": "c",
                                                        "content": "tool output"}})
    return {"conversation": {"id": "conv_x", "title": "Long", "created_at": "2026-10-01T10:00:00Z",
                             "updated_at": "2026-10-09T12:00:00Z",
                             "source": {"platform": "claude_code", "original_id": f"{SID}.jsonl"}},
            "messages": messages}


def test_long_session_is_split_at_message_boundaries() -> None:
    doc = _long_doc(turns=24, words_per_turn=900)
    chunks = make_chunk_documents(doc, "claude-code-cli", doc_key=SID, chunk_words=4000)
    assert len(chunks) > 1
    assert [c["chunk"] for c in chunks] == list(range(1, len(chunks) + 1))
    assert all(c["chunks"] == len(chunks) for c in chunks)
    assert all(c["words"] <= 4000 for c in chunks)
    # contiguous, non-overlapping message ranges that cover every dialog message
    assert chunks[0]["msg_from"] == 0
    for a, b in zip(chunks, chunks[1:]):
        assert b["msg_from"] > a["msg_to"]
        assert b["chunk_from"] >= a["chunk_from"]
    # session_words counts the dialog text incl. one speaker label per message
    assert sum(c["words"] for c in chunks) == chunks[0]["session_words"] == 24 * 900 + 24
    assert chunks[0]["updated_date"] == "2026-10-09"
    # the last words are searchable: they are in the last chunk
    assert "w23x899" in chunks[-1]["content"]


def test_oversized_message_gets_chunks_of_its_own() -> None:
    doc = _long_doc(turns=3, words_per_turn=10)
    doc["messages"][0]["message"]["content"] = "\n".join(
        " ".join(f"big{line}x{j}" for j in range(500)) for line in range(20)
    )
    chunks = make_chunk_documents(doc, "claude-code-cli", doc_key=SID, chunk_words=4000,
                                  chunk_chars=10**9)  # isolate the word limit
    big = [c for c in chunks if c["msg_from"] == c["msg_to"] == 0]
    assert len(big) == 3 and all(c["words"] <= 4000 for c in big)
    assert chunks[-1]["msg_from"] > 0


def test_character_limit_caps_code_heavy_chunks() -> None:
    """Few words but long lines (code, URLs, blobs) must still fit one tool output."""
    doc = _long_doc(turns=4, words_per_turn=10)
    long_line = " ".join("https://example.org/" + "p" * 80 for _ in range(400))  # ~40k chars
    doc["messages"][0]["message"]["content"] = long_line + "\n" + "B" * 60000  # plus one blob
    chunks = make_chunk_documents(doc, "claude-code-cli", doc_key=SID,
                                  chunk_words=4000, chunk_chars=24000)
    assert all(len(c["content"]) <= 24000 for c in chunks)
    assert sum(c["content"].count("https://") for c in chunks) == 400
    assert sum(c["content"].count("B") for c in chunks) == 60000


def test_session_without_dialog_text_keeps_one_findable_chunk() -> None:
    doc = _long_doc(turns=2, words_per_turn=5)
    for env in doc["messages"]:
        if env["message"]["role"] in ("user", "assistant"):
            env["message"]["content"] = ""
    (chunk,) = make_chunk_documents(doc, "codex", doc_key="rollout-x")
    assert chunk["content"] == "" and chunk["title"] == "Long"
    assert chunk["msg_from"] is None and chunk["id"] == "rollout-x__c1"


def test_relabelled_document_keeps_its_ids(tmp_path: Path) -> None:
    """A re-export that adds the source_adapter label must upsert the same
    documents instead of creating second ones next to them."""
    archive = tmp_path / "ocf-archive"
    archive.mkdir()
    doc = _ocf(_session_jsonl(tmp_path / "src" / f"{SID}.jsonl"))
    target = archive / f"{SID}.ocf.json"

    target.write_text(json.dumps(doc), encoding="utf-8")
    (_, (before,), _), = iter_archive_documents(archive)
    doc["conversation"]["meta"]["ocf_py"] = {"source_adapter": "claude-code-app"}
    target.write_text(json.dumps(doc), encoding="utf-8")
    (_, (after,), _), = iter_archive_documents(archive)

    assert (before["tool"], after["tool"]) == ("claude-code", "claude-code-app")
    assert before["id"] == after["id"] == f"{SID}__c1"


def test_iter_recovered_documents(tmp_path: Path) -> None:
    content = "# Title\n\n" + "\n".join("word " * 300 for _ in range(30))
    good = {"id": "claude-code-app__2026-05-01T10-00-00__abc", "tool": "claude-code-app",
            "session_id": "abc", "title": "Old one", "created_at": 1777000000, "content": content}
    (tmp_path / "abc.meili.json").write_text(json.dumps(good), encoding="utf-8")
    (tmp_path / "abc.md").write_text("---\n---\n", encoding="utf-8")
    (tmp_path / "bad.meili.json").write_text(json.dumps({"id": "x"}), encoding="utf-8")

    (chunks,) = list(iter_recovered_documents(tmp_path))
    assert len(chunks) == 3 and [c["id"] for c in chunks] == ["abc__c1", "abc__c2", "abc__c3"]
    assert all(c["doc"] == "abc" and c["recovered"] and c["title"] == "Old one" for c in chunks)
    assert chunks[0]["session_words"] == 30 * 300 + 2
    # once the archive holds the session again, the recovered copy is left out
    assert list(iter_recovered_documents(tmp_path, skip_sessions={"abc"})) == []


def test_export_all_records_source_adapter(tmp_path: Path) -> None:
    """Archive documents carry the adapter that wrote them (tool facet)."""
    projects = tmp_path / ".claude" / "projects"
    _session_jsonl(projects / "home-u-demo" / f"{SID}.jsonl")
    out = tmp_path / "out"
    export_all(ClaudeCodeCliAdapter(), out, source_dirs=projects)
    written = json.loads((out / f"{SID}.ocf.json").read_text(encoding="utf-8"))
    assert written["conversation"]["meta"]["ocf_py"] == {"source_adapter": "claude-code-cli"}
    assert archive_tool_name(written) == "claude-code-cli"


class _FakeTask:
    def __init__(self, uid: int, status: str = "succeeded") -> None:
        self.task_uid = uid
        self.status = status
        self.error = None if status == "succeeded" else {"message": "payload too large"}


class _FakeIndex:
    def __init__(self) -> None:
        self.batches: list[list[dict]] = []

    def add_documents(self, docs: list[dict]) -> _FakeTask:
        self.batches.append(docs)
        return _FakeTask(len(self.batches))


class _FakeClient:
    def __init__(self, fail_uids: set[int] = frozenset()) -> None:
        self.waited: list[int] = []
        self.fail_uids = fail_uids

    def wait_for_task(self, uid: int, timeout_in_ms: int) -> _FakeTask:
        self.waited.append(uid)
        return _FakeTask(uid, "failed" if uid in self.fail_uids else "succeeded")


def test_index_archive_enqueues_then_waits_and_counts_failures(tmp_path: Path) -> None:
    import argparse

    from ocf.cli import _index_archive

    archive = tmp_path / "ocf-archive"
    archive.mkdir()
    for i in range(3):
        sid = f"{i:08d}-2222-3333-4444-555555555555"
        doc = _ocf(_session_jsonl(tmp_path / "src" / f"{sid}.jsonl", f"Hello {i}"))
        (archive / f"{sid}.ocf.json").write_text(json.dumps(doc), encoding="utf-8")
    args = argparse.Namespace(from_archive=archive, recovered=None, quiet=True)

    index, client = _FakeIndex(), _FakeClient()
    result = _index_archive(args, index, client)
    assert (result.indexed, result.chunks, result.failed) == (3, 3, 0)
    assert len(index.batches) == 1 and client.waited == [1]

    index, client = _FakeIndex(), _FakeClient(fail_uids={1})
    result = _index_archive(args, index, client)
    assert (result.indexed, result.failed) == (0, 3)
