"""Canonical JSON serialization for OCF.

Implements the canonical-JSON contract documented in the OCF spec
(``mapping.md`` § "Hashing and canonical JSON"):

1. UTF-8 encoded bytes, no BOM.
2. No trailing newline.
3. No whitespace between tokens (compact form).
4. Object keys sorted byte-wise lexicographic.
5. Strings normalized to Unicode NFC before serialization.
6. Non-ASCII Unicode emitted literally (not ``\\uXXXX`` escaped).
7. Integers serialize without decimal point; floats use shortest
   round-trip-safe representation.
8. NaN and Infinity are rejected (raise ``CanonicalJSONError``).
9. Datetimes serialize as ISO 8601 with ``Z`` suffix; naive
   datetimes are rejected.
10. Booleans and null serialize as ``true`` / ``false`` / ``null``.

The deterministic byte output enables stable SHA-256 hashing for
content addressing, redaction policy hashes, and cross-implementation
comparison.

All public callers should use :func:`dumps`. The internal
:func:`_normalize` walks the value tree to apply NFC and reject
NaN/Infinity / naive datetimes before delegating compact serialization
to ``orjson``.
"""

from __future__ import annotations

import hashlib
import math
import unicodedata
from datetime import datetime, timezone
from typing import Any

import orjson


class CanonicalJSONError(ValueError):
    """Raised when a value cannot be canonicalized.

    Common causes:
      - NaN or Infinity floats (not representable in JSON).
      - Naive datetime objects (no tzinfo set).
      - Unsupported Python types (set, bytes, custom objects, etc.).
      - Non-string dict keys (JSON requires string keys).
    """


# Internal sentinel for the recursion: tracks whether we're inside a dict
# value vs. a top-level call. Only used for clearer error messages.

def _normalize(value: Any) -> Any:
    """Recursively prepare ``value`` for canonical serialization.

    Returns a new structure where:
      - All strings are NFC-normalized.
      - All datetimes are timezone-aware and converted to UTC.
      - All floats are finite (NaN/Infinity raise).
      - All dict keys are strings.

    Pass-through for ``int``, ``bool``, ``None``. Containers (``dict``,
    ``list``, ``tuple``) recurse.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise CanonicalJSONError(
                f"NaN/Infinity not allowed in canonical JSON: {value!r}"
            )
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise CanonicalJSONError(
                "Datetime must be timezone-aware (no naive datetimes): "
                f"{value!r}"
            )
        # Convert to UTC; orjson with OPT_UTC_Z then emits Z suffix.
        return value.astimezone(timezone.utc)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalJSONError(
                    f"Object keys must be strings, got {type(k).__name__}: "
                    f"{k!r}"
                )
            out[unicodedata.normalize("NFC", k)] = _normalize(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    raise CanonicalJSONError(
        f"Unsupported type for canonical JSON: {type(value).__name__}"
    )


def dumps(value: Any) -> bytes:
    """Serialize ``value`` to canonical-JSON bytes per the OCF spec.

    Returns
    -------
    bytes
        UTF-8 encoded bytes. No BOM, no trailing newline, no whitespace
        between tokens, sorted keys, NFC-normalized strings.

    Raises
    ------
    CanonicalJSONError
        If the value contains NaN/Infinity, naive datetimes,
        non-string dict keys, or unsupported types.
    """
    normalized = _normalize(value)
    options = orjson.OPT_SORT_KEYS | orjson.OPT_UTC_Z
    return orjson.dumps(normalized, option=options)


def dumps_str(value: Any) -> str:
    """Convenience wrapper: canonical-JSON as a Python ``str`` (UTF-8 decoded)."""
    return dumps(value).decode("utf-8")


def sha256_hex(value: Any) -> str:
    """Compute the SHA-256 hex digest over ``dumps(value)``.

    This is the canonical way to compute content hashes for OCF
    documents and redaction policy hashes — given identical logical
    input, all conforming implementations produce identical bytes
    via :func:`dumps` and therefore identical hashes.
    """
    return hashlib.sha256(dumps(value)).hexdigest()


__all__ = [
    "CanonicalJSONError",
    "dumps",
    "dumps_str",
    "sha256_hex",
]
