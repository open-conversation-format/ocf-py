"""Tests for ocf.core.canonical.

Covers every rule documented in mapping.md § "Hashing and canonical JSON".
Each rule is its own test so failures point at the exact contract clause.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from ocf.core.canonical import (
    CanonicalJSONError,
    dumps,
    dumps_str,
    sha256_hex,
)


# ---------------------------------------------------------------------------
# Rule 1 + 2: UTF-8, no BOM, no trailing newline
# ---------------------------------------------------------------------------

def test_returns_bytes() -> None:
    assert isinstance(dumps({"k": "v"}), bytes)


def test_no_utf8_bom() -> None:
    out = dumps({"k": "v"})
    assert not out.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM must not be present"


def test_no_trailing_newline() -> None:
    out = dumps({"k": "v"})
    assert not out.endswith(b"\n"), "Trailing newline must not be present"


# ---------------------------------------------------------------------------
# Rule 3: No whitespace between tokens
# ---------------------------------------------------------------------------

def test_no_inter_token_whitespace_in_object() -> None:
    assert dumps({"a": 1, "b": 2}) == b'{"a":1,"b":2}'


def test_no_inter_token_whitespace_in_array() -> None:
    assert dumps([1, 2, 3]) == b"[1,2,3]"


def test_no_whitespace_around_colon() -> None:
    out = dumps({"key": "value"})
    assert out == b'{"key":"value"}'


# ---------------------------------------------------------------------------
# Rule 4: Sorted keys (byte-wise lexicographic)
# ---------------------------------------------------------------------------

def test_top_level_keys_sorted() -> None:
    assert dumps({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_nested_keys_sorted() -> None:
    assert dumps({"z": {"y": 1, "x": 2}, "a": 0}) == b'{"a":0,"z":{"x":2,"y":1}}'


def test_keys_sorted_byte_wise() -> None:
    # ASCII byte order: uppercase < lowercase
    assert dumps({"a": 1, "B": 2}) == b'{"B":2,"a":1}'


# ---------------------------------------------------------------------------
# Rule 5 + 6: NFC normalization, non-ASCII unescaped
# ---------------------------------------------------------------------------

def test_nfc_normalizes_strings_in_values() -> None:
    precomposed = "café"  # "café" with U+00E9 (one codepoint)
    decomposed = "café"  # "café" with e + combining acute (two codepoints)
    assert dumps({"k": precomposed}) == dumps({"k": decomposed})


def test_nfc_normalizes_strings_in_keys() -> None:
    precomposed_key = "café"
    decomposed_key = "café"
    assert dumps({precomposed_key: 1}) == dumps({decomposed_key: 1})


def test_non_ascii_emitted_literally() -> None:
    out = dumps({"k": "café"})
    assert "café".encode("utf-8") in out
    assert b"\\u00e9" not in out, "Non-ASCII must be emitted literally, not escaped"


def test_chinese_unescaped() -> None:
    out = dumps({"k": "你好"})
    assert "你好".encode("utf-8") in out
    assert b"\\u" not in out


# ---------------------------------------------------------------------------
# Rule 7: Integer vs float preservation, shortest round-trip floats
# ---------------------------------------------------------------------------

def test_integer_no_decimal_point() -> None:
    assert dumps(42) == b"42"
    assert dumps({"n": 42}) == b'{"n":42}'


def test_integer_zero() -> None:
    assert dumps(0) == b"0"


def test_negative_integer() -> None:
    assert dumps(-7) == b"-7"


def test_float_keeps_decimal() -> None:
    # Floats with no fractional part still serialize with .0
    assert dumps(1.0) == b"1.0"


def test_float_short_repr() -> None:
    assert dumps(3.14) == b"3.14"


# ---------------------------------------------------------------------------
# Rule 8: NaN / Infinity rejected
# ---------------------------------------------------------------------------

def test_nan_rejected() -> None:
    with pytest.raises(CanonicalJSONError, match="NaN/Infinity"):
        dumps({"v": math.nan})


def test_positive_infinity_rejected() -> None:
    with pytest.raises(CanonicalJSONError, match="NaN/Infinity"):
        dumps({"v": math.inf})


def test_negative_infinity_rejected() -> None:
    with pytest.raises(CanonicalJSONError, match="NaN/Infinity"):
        dumps({"v": -math.inf})


# ---------------------------------------------------------------------------
# Rule 9: Datetime as ISO 8601 with Z suffix; naive rejected
# ---------------------------------------------------------------------------

def test_utc_datetime_z_suffix() -> None:
    dt = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)
    assert dumps({"t": dt}) == b'{"t":"2026-04-26T10:00:00Z"}'


def test_non_utc_datetime_converted_to_utc_z() -> None:
    # Berlin is UTC+1 (or +2 in DST). 12:00 CET == 11:00 UTC.
    cet = timezone(timedelta(hours=1))
    dt = datetime(2026, 4, 26, 12, 0, 0, tzinfo=cet)
    assert dumps({"t": dt}) == b'{"t":"2026-04-26T11:00:00Z"}'


def test_naive_datetime_rejected() -> None:
    naive = datetime(2026, 4, 26, 10, 0, 0)  # no tzinfo
    with pytest.raises(CanonicalJSONError, match="timezone-aware"):
        dumps({"t": naive})


# ---------------------------------------------------------------------------
# Rule 10: Booleans and null
# ---------------------------------------------------------------------------

def test_true_false_null() -> None:
    assert dumps({"a": True, "b": False, "c": None}) == b'{"a":true,"b":false,"c":null}'


# ---------------------------------------------------------------------------
# Type rejection: non-string keys, unsupported types
# ---------------------------------------------------------------------------

def test_non_string_key_rejected() -> None:
    with pytest.raises(CanonicalJSONError, match="strings"):
        dumps({1: "value"})


def test_set_rejected() -> None:
    with pytest.raises(CanonicalJSONError, match="Unsupported type"):
        dumps({"v": {1, 2, 3}})


def test_bytes_rejected() -> None:
    with pytest.raises(CanonicalJSONError, match="Unsupported type"):
        dumps({"v": b"bytes"})


# ---------------------------------------------------------------------------
# Hash determinism: equivalent inputs hash identically
# ---------------------------------------------------------------------------

def test_hash_stability_across_key_order() -> None:
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    assert sha256_hex(a) == sha256_hex(b)


def test_hash_stability_across_nfc_forms() -> None:
    a = {"name": "café"}
    b = {"name": "café"}
    assert sha256_hex(a) == sha256_hex(b)


def test_hash_differs_for_different_data() -> None:
    a = {"v": 1}
    b = {"v": 2}
    assert sha256_hex(a) != sha256_hex(b)


def test_hash_is_64_hex_chars() -> None:
    h = sha256_hex({"k": "v"})
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# dumps_str convenience wrapper
# ---------------------------------------------------------------------------

def test_dumps_str_matches_dumps() -> None:
    obj = {"a": 1, "b": "café"}
    assert dumps_str(obj) == dumps(obj).decode("utf-8")


# ---------------------------------------------------------------------------
# Empty / edge cases
# ---------------------------------------------------------------------------

def test_empty_object() -> None:
    assert dumps({}) == b"{}"


def test_empty_array() -> None:
    assert dumps([]) == b"[]"


def test_top_level_string() -> None:
    assert dumps("hello") == b'"hello"'


def test_top_level_integer() -> None:
    assert dumps(0) == b"0"


def test_nested_array_in_object() -> None:
    assert dumps({"items": [1, "two", None, True]}) == b'{"items":[1,"two",null,true]}'
