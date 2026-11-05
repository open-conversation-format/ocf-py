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
)
from ocf.renderers.markdown import MarkdownRenderer

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

    out = {p.name: (d, problem) for p, d, problem in
           iter_archive_documents(archive, MarkdownRenderer(simple=True).render)}

    session_doc, problem = out[f"{SID}.ocf.json"]
    assert problem is None
    assert session_doc["tool"] == "claude-code"
    assert session_doc["id"] == SID
    assert session_doc["created_date"] == "2026-04-26"
    assert session_doc["session_id"] == SID
    assert "Hello" in session_doc["content"]

    agent_doc, _ = out["agent-a1b2.ocf.json"]
    assert agent_doc["tool"] == "claude-cowork-app"
    assert agent_doc["id"] == "agent-a1b2"

    assert out["empty.ocf.json"] == (None, "empty")
    assert out["broken.ocf.json"][0] is None
    assert out["broken.ocf.json"][1].startswith("unreadable")


def test_relabelled_document_keeps_its_id(tmp_path: Path) -> None:
    """A re-export that adds the source_adapter label must upsert the same
    document instead of creating a second one next to it."""
    archive = tmp_path / "ocf-archive"
    archive.mkdir()
    doc = _ocf(_session_jsonl(tmp_path / "src" / f"{SID}.jsonl"))
    target = archive / f"{SID}.ocf.json"
    render = MarkdownRenderer(simple=True).render

    target.write_text(json.dumps(doc), encoding="utf-8")
    (_, before, _), = iter_archive_documents(archive, render)
    doc["conversation"]["meta"]["ocf_py"] = {"source_adapter": "claude-code-app"}
    target.write_text(json.dumps(doc), encoding="utf-8")
    (_, after, _), = iter_archive_documents(archive, render)

    assert (before["tool"], after["tool"]) == ("claude-code", "claude-code-app")
    assert before["id"] == after["id"] == SID


def test_iter_recovered_documents(tmp_path: Path) -> None:
    good = {"id": "claude-code-app__2026-05-01T10-00-00__abc", "tool": "claude-code-app",
            "session_id": "abc", "content": "# Title\n\ntext"}
    (tmp_path / "abc.meili.json").write_text(json.dumps(good), encoding="utf-8")
    (tmp_path / "abc.md").write_text("---\n---\n", encoding="utf-8")
    (tmp_path / "bad.meili.json").write_text(json.dumps({"id": "x"}), encoding="utf-8")
    assert list(iter_recovered_documents(tmp_path)) == [good]
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
