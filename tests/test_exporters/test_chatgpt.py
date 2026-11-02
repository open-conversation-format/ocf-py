"""Tests for ocf.exporters.chatgpt.

Fixtures are synthetic but mirror the real ``/backend-api/conversation``
shape observed in 1451 exported conversations (pionxzh/chatgpt-exporter
JSON format): node tree in ``mapping``, ``recipient``-based tool calls,
``thoughts`` / ``reasoning_recap``, ``content_references`` citations,
``web.run`` results in ``metadata.search_result_groups``.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest

from ocf.core.schema import validate
from ocf.exporters._base import SkipExport, export_all
from ocf.exporters.chatgpt import ChatGPTAdapter

CONV_ID = "6717f2f3-d9cc-8009-a769-5bd238c2fe65"
T0 = 1729622772.0


def _msg(mid, role, content, *, t, recipient="all", name=None, metadata=None):
    return {
        "id": mid,
        "author": {"role": role, "name": name, "metadata": {}},
        "create_time": t,
        "update_time": None,
        "content": content,
        "status": "finished_successfully",
        "end_turn": None,
        "weight": 1.0,
        "metadata": metadata or {},
        "recipient": recipient,
        "channel": None,
    }


def _text(*parts):
    return {"content_type": "text", "parts": list(parts)}


def _conversation(nodes: list[tuple[str, str | None, dict | None]], *, current: str,
                  children: bool = True, **extra: Any) -> dict[str, Any]:
    """Build a conversation; ``nodes`` = (node_id, parent_id, message)."""
    mapping: dict[str, Any] = {}
    for nid, parent, message in nodes:
        mapping[nid] = {"id": nid, "message": message, "parent": parent}
        if children:
            mapping[nid]["children"] = [n for n, p, _ in nodes if p == nid]
    conv = {
        "id": CONV_ID,
        "title": "Test Unterhaltung",
        "create_time": T0,
        "update_time": T0 + 600,
        "mapping": mapping,
        "default_model_slug": "gpt-4o",
        "gizmo_id": None,
        "is_archived": False,
        "current_node": current,
    }
    conv.update(extra)
    return conv


def _simple_conv(**extra: Any) -> dict[str, Any]:
    return _conversation(
        [
            ("root", None, None),
            ("u1", "root", _msg("u1", "user", _text("Hallo?"), t=T0 + 1)),
            ("a1", "u1", _msg("a1", "assistant", _text("Hi!"), t=T0 + 2,
                              metadata={"model_slug": "gpt-4o"})),
        ],
        current="a1",
        **extra,
    )


def _write(tmp_path: Path, conv: Any, name: str = "ChatGPT-Test.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(conv), encoding="utf-8")
    return path


def _export(tmp_path: Path, conv: Any) -> dict[str, Any]:
    return ChatGPTAdapter().export_one(_write(tmp_path, conv))


def _by_id(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in doc["messages"]}


# ---------------------------------------------------------------------------
# Conversation-level mapping
# ---------------------------------------------------------------------------

def test_simple_conversation_is_valid_ocf(tmp_path: Path) -> None:
    doc = _export(tmp_path, _simple_conv())
    assert validate(doc) == []
    conv = doc["conversation"]
    assert conv["id"] == f"conv_chatgpt_{CONV_ID}"
    assert conv["title"] == "Test Unterhaltung"
    assert conv["created_at"] == "2024-10-22T18:46:12Z"
    assert conv["default_model"] == "gpt-4o"
    assert conv["source"]["platform"] == "chatgpt"
    assert conv["source"]["original_url"] == f"https://chatgpt.com/c/{CONV_ID}"
    assert conv["active_message_id"] == "a1"
    assert [m["message"]["role"] for m in doc["messages"]] == ["user", "assistant"]
    assert doc["messages"][1]["model"] == "gpt-4o"
    assert doc["messages"][1]["parent_id"] == "u1"


def test_branches_kept_in_chronological_order(tmp_path: Path) -> None:
    conv = _conversation(
        [
            ("root", None, None),
            ("u1", "root", _msg("u1", "user", _text("Frage"), t=T0 + 1)),
            ("a_old", "u1", _msg("a_old", "assistant", _text("alt"), t=T0 + 2)),
            ("a_new", "u1", _msg("a_new", "assistant", _text("neu"), t=T0 + 5)),
            ("u2", "a_new", _msg("u2", "user", _text("weiter"), t=T0 + 6)),
        ],
        current="u2",
    )
    doc = _export(tmp_path, conv)
    assert [m["id"] for m in doc["messages"]] == ["u1", "a_old", "a_new", "u2"]
    msgs = _by_id(doc)
    assert msgs["a_old"]["parent_id"] == "u1"
    assert msgs["a_new"]["parent_id"] == "u1"
    assert doc["conversation"]["active_message_id"] == "u2"


def test_tree_built_from_parent_without_children(tmp_path: Path) -> None:
    conv = _simple_conv()
    for node in conv["mapping"].values():
        del node["children"]
    conv["mapping"]["stray"] = {}
    doc = _export(tmp_path, conv)
    assert [m["id"] for m in doc["messages"]] == ["u1", "a1"]


def test_empty_conversation_raises_skip(tmp_path: Path) -> None:
    conv = _conversation([("root", None, None)], current="root")
    with pytest.raises(SkipExport):
        _export(tmp_path, conv)


def test_single_conversation_wrapped_in_list(tmp_path: Path) -> None:
    doc = _export(tmp_path, [_simple_conv()])
    assert len(doc["messages"]) == 2


def test_multi_conversation_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="2 conversations"):
        _export(tmp_path, [_simple_conv(), _simple_conv()])


def test_invalid_utf8_is_tolerated_and_flagged(tmp_path: Path) -> None:
    raw = json.dumps(_simple_conv()).replace("Hallo?", "Hallo XX").encode("utf-8")
    raw = raw.replace(b"XX", b"\xed\xa0")
    path = tmp_path / "ChatGPT-Broken.json"
    path.write_bytes(raw)
    doc = ChatGPTAdapter().export_one(path)
    assert doc["conversation"]["meta"]["chatgpt"]["decode_errors"] is True


# ---------------------------------------------------------------------------
# Message mapping
# ---------------------------------------------------------------------------

def test_tool_call_paired_with_result(tmp_path: Path) -> None:
    conv = _conversation(
        [
            ("root", None, None),
            ("u1", "root", _msg("u1", "user", _text("Rechne"), t=T0 + 1)),
            ("c1", "u1", _msg("c1", "assistant",
                              {"content_type": "code", "language": "unknown", "text": "1+1"},
                              t=T0 + 2, recipient="python")),
            ("r1", "c1", _msg("r1", "tool", {"content_type": "execution_output", "text": "2"},
                              t=T0 + 3, name="python")),
            ("a1", "r1", _msg("a1", "assistant", _text("Ergebnis: 2"), t=T0 + 4)),
        ],
        current="a1",
    )
    doc = _export(tmp_path, conv)
    assert validate(doc) == []
    msgs = _by_id(doc)
    call = msgs["c1"]["message"]["tool_calls"][0]
    assert call["function"] == {"name": "python", "arguments": "1+1"}
    assert msgs["r1"]["message"]["tool_call_id"] == call["id"]
    assert msgs["r1"]["message"]["name"] == "python"
    assert msgs["r1"]["message"]["content"] == "2"


def test_tool_output_without_call_gets_synthesized_call(tmp_path: Path) -> None:
    conv = _conversation(
        [
            ("root", None, None),
            ("u1", "root", _msg("u1", "user", _text("Bild bitte"), t=T0 + 1)),
            ("t1", "u1", _msg("t1", "tool", _text("Bild erstellt"), t=T0 + 2,
                              name="t2uay3k.sj1i4kz")),
        ],
        current="t1",
    )
    doc = _export(tmp_path, conv)
    assert validate(doc) == []
    assert [m["id"] for m in doc["messages"]] == ["u1", "t1-call", "t1"]
    msgs = _by_id(doc)
    synth = msgs["t1-call"]
    assert synth["id_origin"] == "synthesized"
    assert synth["parent_id"] == "u1"
    assert synth["meta"]["chatgpt"]["synthesized_tool_call"] is True
    assert msgs["t1"]["parent_id"] == "t1-call"
    assert msgs["t1"]["message"]["tool_call_id"] == synth["message"]["tool_calls"][0]["id"]


def test_thoughts_become_thinking_and_recap_hidden_skipped(tmp_path: Path) -> None:
    conv = _conversation(
        [
            ("root", None, None),
            ("u1", "root", _msg("u1", "user", _text("Denk nach"), t=T0 + 1)),
            ("th", "u1", _msg("th", "assistant", {
                "content_type": "thoughts",
                "thoughts": [{"summary": "Plan", "content": "Erst A, dann B."}],
            }, t=T0 + 2, metadata={"finished_duration_sec": 6})),
            ("rc", "th", _msg("rc", "assistant",
                              {"content_type": "reasoning_recap", "content": "Nachgedacht für 6s"},
                              t=T0 + 3)),
            ("hd", "rc", _msg("hd", "assistant", _text("versteckt"), t=T0 + 4,
                              metadata={"is_visually_hidden_from_conversation": True})),
            ("a1", "hd", _msg("a1", "assistant", _text("Antwort"), t=T0 + 5)),
        ],
        current="a1",
    )
    doc = _export(tmp_path, conv)
    msgs = _by_id(doc)
    assert set(msgs) == {"u1", "th", "a1"}
    assert msgs["th"]["message"]["content"] == [
        {"type": "thinking", "thinking": "Plan\n\nErst A, dann B."}
    ]
    assert msgs["th"]["duration_ms"] == 6000
    assert msgs["a1"]["parent_id"] == "th"
    assert doc["conversation"]["meta"]["chatgpt"]["skipped"] == {
        "reasoning_recap": 1, "hidden": 1,
    }


def test_citations_replaced_with_alt_and_markers_stripped(tmp_path: Path) -> None:
    marker = "citeturn0search1"
    text = f"MinIO nutzt Erasure Coding {marker}. Fakt."
    refs = [
        {"type": "grouped_webpages", "matched_text": marker,
         "start_idx": text.index(marker), "end_idx": text.index(marker) + len(marker),
         "alt": "([MinIO](https://min.io))"},
        {"type": "attribution", "matched_text": "Fakt.", "start_idx": None, "end_idx": None},
    ]
    conv = _conversation(
        [
            ("root", None, None),
            ("u1", "root", _msg("u1", "user", _text("?"), t=T0 + 1)),
            ("a1", "u1", _msg("a1", "assistant", _text(text), t=T0 + 2,
                              metadata={"content_references": refs})),
        ],
        current="a1",
    )
    doc = _export(tmp_path, conv)
    assert _by_id(doc)["a1"]["message"]["content"] == (
        "MinIO nutzt Erasure Coding ([MinIO](https://min.io)). Fakt."
    )


def test_web_run_results_taken_from_metadata(tmp_path: Path) -> None:
    groups = [{"type": "search_result_group", "domain": "min.io", "entries": [
        {"type": "search_result", "url": "https://min.io/docs", "title": "MinIO Docs",
         "snippet": "Erasure coding", "attribution": "min.io"},
    ]}]
    conv = _conversation(
        [
            ("root", None, None),
            ("u1", "root", _msg("u1", "user", _text("Such"), t=T0 + 1)),
            ("c1", "u1", _msg("c1", "assistant", _text('{"search_query": []}'),
                              t=T0 + 2, recipient="web.run")),
            ("r1", "c1", _msg("r1", "tool", _text(""), t=T0 + 3, name="web.run",
                              metadata={"search_result_groups": groups})),
        ],
        current="r1",
    )
    doc = _export(tmp_path, conv)
    assert _by_id(doc)["r1"]["message"]["content"] == (
        "- [MinIO Docs](https://min.io/docs)\n  Erasure coding"
    )


def test_multimodal_parts_become_text(tmp_path: Path) -> None:
    content = {"content_type": "multimodal_text", "parts": [
        {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-abc",
         "width": 800, "height": 600},
        {"content_type": "audio_transcription", "text": "Was siehst du?", "direction": "in"},
        {"content_type": "audio_asset_pointer", "asset_pointer": "sediment://x"},
        "Bitte beschreiben.",
    ]}
    conv = _conversation(
        [
            ("root", None, None),
            ("u1", "root", _msg("u1", "user", content, t=T0 + 1)),
        ],
        current="u1",
    )
    doc = _export(tmp_path, conv)
    assert _by_id(doc)["u1"]["message"]["content"] == (
        "[Image 800x600: file-service://file-abc]\n\nWas siehst du?\n\nBitte beschreiben."
    )


def test_system_error_tool_result_has_error_status(tmp_path: Path) -> None:
    conv = _conversation(
        [
            ("root", None, None),
            ("u1", "root", _msg("u1", "user", _text("Such"), t=T0 + 1)),
            ("c1", "u1", _msg("c1", "assistant", _text("bad"), t=T0 + 2,
                              recipient="browser.search")),
            ("r1", "c1", _msg("r1", "tool", {"content_type": "system_error",
                                             "name": "tool_error", "text": "Invalid call"},
                              t=T0 + 3, name="browser")),
        ],
        current="r1",
    )
    r1 = _by_id(_export(tmp_path, conv))["r1"]
    assert r1["status"] == "error"
    assert r1["message"]["content"] == "tool_error: Invalid call"


# ---------------------------------------------------------------------------
# Sources: discovery, ZIPs, filenames, incremental export
# ---------------------------------------------------------------------------

def _zip(path: Path, members: dict[str, Any]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, conv in members.items():
            zf.writestr(name, json.dumps(conv))
    return path


def test_discover_requires_a_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCF_CHATGPT_EXPORT_DIR", raising=False)
    with pytest.raises(FileNotFoundError, match="--source-dir"):
        ChatGPTAdapter().discover()


def test_discover_uses_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, _simple_conv())
    monkeypatch.setenv("OCF_CHATGPT_EXPORT_DIR", str(tmp_path))
    assert len(ChatGPTAdapter().discover()) == 1


def test_discover_skips_unrelated_json(tmp_path: Path) -> None:
    _write(tmp_path, _simple_conv())
    (tmp_path / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    _zip(tmp_path / "other.zip", {"config.json": {"a": 1}})
    assert len(ChatGPTAdapter().discover(tmp_path)) == 1


def test_same_member_name_in_two_zips_stays_distinct(tmp_path: Path) -> None:
    conv_a = _simple_conv(id="aaaaaaaa-0000-0000-0000-000000000001")
    conv_b = _simple_conv(id="bbbbbbbb-0000-0000-0000-000000000002")
    _zip(tmp_path / "part-01.zip", {"ChatGPT-New_chat.json": conv_a})
    _zip(tmp_path / "part-02.zip", {"ChatGPT-New_chat.json": conv_b})
    adapter = ChatGPTAdapter()
    sources = adapter.discover(tmp_path)
    assert len(sources) == 2
    assert {adapter.ocf_filename_for(s) for s in sources} == {
        "chatgpt-aaaaaaaa-0000-0000-0000-000000000001.ocf.json",
        "chatgpt-bbbbbbbb-0000-0000-0000-000000000002.ocf.json",
    }
    docs = [adapter.export_one(s) for s in sources]
    assert {d["conversation"]["source"]["original_id"] for d in docs} == {
        conv_a["id"], conv_b["id"],
    }


def test_resolve_single_zip_file(tmp_path: Path) -> None:
    zip_path = _zip(tmp_path / "part.zip", {"ChatGPT-A.json": _simple_conv()})
    assert len(ChatGPTAdapter().resolve_sources(str(zip_path))) == 1


def test_session_info_peeks_header(tmp_path: Path) -> None:
    info = ChatGPTAdapter().session_info(_write(tmp_path, _simple_conv()))
    assert info.session_id == CONV_ID
    assert info.title == "Test Unterhaltung"
    assert info.model == "gpt-4o"
    assert info.created_at is not None and info.created_at.year == 2024


def test_export_all_from_zip_is_incremental(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _zip(src / "part.zip", {"ChatGPT-A.json": _simple_conv()})
    out = tmp_path / "out"
    adapter = ChatGPTAdapter()
    first = export_all(adapter, out, source_dirs=[src])
    assert len(first.new) == 1 and not first.failed
    assert (out / f"chatgpt-{CONV_ID}.ocf.json").exists()
    second = export_all(adapter, out, source_dirs=[src])
    assert len(second.skipped) == 1 and not second.new


# ---------------------------------------------------------------------------
# Real-machine smoke (opt-in)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("OCF_CHATGPT_EXPORT_DIR"),
    reason="OCF_CHATGPT_EXPORT_DIR not set",
)
def test_real_exports_validate() -> None:
    adapter = ChatGPTAdapter()
    sources = adapter.discover()[:25]
    assert sources
    for source in sources:
        try:
            doc = adapter.export_one(source)
        except SkipExport:
            continue
        assert validate(doc) == []
