"""Codex Desktop's external agent imports (copies of Claude Code sessions)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocf.exporters._base import SkipExport, export_all
from ocf.exporters.codex import CodexAdapter, import_turns, load_import_registry

TID = "01a1ea34-6b56-7b03-a976-79f35e4bc84f"


def _rollout(path: Path, turns: list[str]) -> Path:
    ts = "2026-11-17T15:51:06.327Z"
    events = [{"timestamp": ts, "type": "session_meta",
               "payload": {"id": TID, "timestamp": ts, "cwd": r"C:\Users\u\Desktop",
                           "originator": "Codex Desktop", "cli_version": "0.166.0"}}]
    for n, turn in enumerate(turns, 1):
        events += [
            {"timestamp": ts, "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
            {"timestamp": ts, "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": f"Question {n}"}]}},
            {"timestamp": ts, "type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": f"Answer {n}"}]}},
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def _adapter(tmp_path: Path, **kw) -> CodexAdapter:
    adapter = CodexAdapter(session_index_path_override=tmp_path / "none.jsonl", **kw)
    registry = {TID: {"imported_thread_id": TID,
                      "source_path": r"\\?\C:\Users\u\.claude\projects\p\abc.jsonl"}}
    adapter._index_cache["__external_imports__"] = registry  # noqa: SLF001
    return adapter


def test_import_turns_counts_imported_and_native() -> None:
    events = [{"payload": {"turn_id": "external-import-turn-1"}},
              {"payload": {"turn_id": "external-import-turn-1"}},
              {"payload": {"turn_id": "019a-native"}}, {"payload": {}}]
    assert import_turns(events) == (1, 1)


def test_pure_import_is_skipped_by_default(tmp_path: Path) -> None:
    src = _rollout(tmp_path / "s" / f"rollout-x-{TID}.jsonl",
                   ["external-import-turn-1", "external-import-turn-2"])
    with pytest.raises(SkipExport, match="another agent"):
        _adapter(tmp_path).export_one(src)


def test_pure_import_is_exported_when_enabled(tmp_path: Path) -> None:
    src = _rollout(tmp_path / "s" / f"rollout-x-{TID}.jsonl", ["external-import-turn-1"])
    doc = _adapter(tmp_path, include_imports=True).export_one(src)
    assert doc["conversation"]["meta"]["codex"]["imported"] == {
        "imported_turns": 1, "native_turns": 0,
        "from": r"C:\Users\u\.claude\projects\p\abc.jsonl",
    }


def test_import_continued_in_codex_is_always_exported(tmp_path: Path) -> None:
    src = _rollout(tmp_path / "s" / f"rollout-x-{TID}.jsonl",
                   ["external-import-turn-1", "019a1ea4-native-turn"])
    doc = _adapter(tmp_path).export_one(src)
    assert doc["conversation"]["meta"]["codex"]["imported"]["native_turns"] == 1
    assert len(doc["messages"]) == 4


def test_native_session_has_no_import_marker(tmp_path: Path) -> None:
    src = _rollout(tmp_path / "s" / f"rollout-x-{TID}.jsonl", ["019a-native"])
    doc = _adapter(tmp_path).export_one(src)
    assert "imported" not in doc["conversation"]["meta"]["codex"]


def test_load_import_registry(tmp_path: Path) -> None:
    path = tmp_path / "external_agent_session_imports.json"
    path.write_text(json.dumps({"records": [{"imported_thread_id": TID, "source_path": "x"}],
                                "detected_connector_records": []}), encoding="utf-8")
    assert load_import_registry(path) == {TID: {"imported_thread_id": TID, "source_path": "x"}}
    assert load_import_registry(tmp_path / "missing.json") == {}


def test_source_that_vanishes_during_export_is_skipped_not_failed(tmp_path: Path) -> None:
    src = _rollout(tmp_path / "s" / f"rollout-x-{TID}.jsonl", ["019a-native"])
    adapter = _adapter(tmp_path)
    src.unlink()  # compressed or moved by Codex after discovery
    result = export_all(adapter, tmp_path / "out", sources=[src])
    assert result.failed == []
    assert result.vanished == [src]
    assert result.skipped == [src]
