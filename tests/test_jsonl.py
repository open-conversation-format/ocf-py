"""Tests for ocf.utils.jsonl - the tail-safe JSONL reader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocf.utils.jsonl import iter_jsonl_tailsafe, zstd


def _write(path: Path, text: str) -> Path:
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.mark.parametrize("sep", [" ", " ", "\x85", "\x0c", "\x0b", "\x1c", "\r"])
def test_unicode_line_separators_inside_strings_survive(tmp_path: Path, sep: str) -> None:
    """json.dumps(ensure_ascii=False) leaves these unescaped; str.splitlines()
    would break the line apart and drop it."""
    line = json.dumps({"type": "user", "text": f"a{sep}b"}, ensure_ascii=False)
    f = _write(tmp_path / "s.jsonl", line + "\n" + '{"type": "x"}\n')

    events = list(iter_jsonl_tailsafe(f))
    assert events == [{"type": "user", "text": f"a{sep}b"}, {"type": "x"}]


def test_crlf_line_endings(tmp_path: Path) -> None:
    f = _write(tmp_path / "s.jsonl", '{"a": 1}\r\n{"a": 2}\r\n')
    assert list(iter_jsonl_tailsafe(f)) == [{"a": 1}, {"a": 2}]


def test_partial_trailing_line_dropped(tmp_path: Path) -> None:
    f = _write(tmp_path / "s.jsonl", '{"a": 1}\n{"a": 2')
    assert list(iter_jsonl_tailsafe(f)) == [{"a": 1}]


def test_bad_mid_file_line_skipped(tmp_path: Path) -> None:
    f = _write(tmp_path / "s.jsonl", '{"a": 1}\nnot json\n{"a": 2}\n')
    assert list(iter_jsonl_tailsafe(f)) == [{"a": 1}, {"a": 2}]
    with pytest.raises(json.JSONDecodeError):
        list(iter_jsonl_tailsafe(f, on_error="raise"))


def test_zstd_compressed_file_read_transparently(tmp_path: Path) -> None:
    raw = '{"a": 1}\n{"a": " "}\n'.encode("utf-8")
    f = tmp_path / "s.jsonl.zst"
    f.write_bytes(zstd.compress(raw))
    assert list(iter_jsonl_tailsafe(f)) == [{"a": 1}, {"a": " "}]
