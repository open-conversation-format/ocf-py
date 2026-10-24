"""Tail-safe JSONL reader.

Reading append-only JSONL files (Claude Code session logs, Codex
rollouts) requires care: the last line may be partially written when
we read. Per the OCF spec's partial-tail-safe contract:

- A line ending in ``\\n`` is durable.
- A non-final line that fails JSON parse SHOULD be skipped (logged).
- A final line missing ``\\n`` MUST be dropped (writer crashed mid-line).

This module implements those rules.

Files ending in ``.zst`` are decompressed transparently (Codex >= 0.16x
stores finished rollouts as ``rollout-*.jsonl.zst``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:
    from compression import zstd  # Python >= 3.14
except ImportError:  # pragma: no cover - depends on interpreter version
    from backports import zstd

ZSTD_SUFFIX = ".zst"


def read_maybe_compressed(path: Path) -> bytes:
    """Return the file's bytes, zstd-decompressed if it ends in ``.zst``."""
    raw = path.read_bytes()
    if path.name.endswith(ZSTD_SUFFIX) and raw:
        return zstd.decompress(raw)
    return raw


def iter_jsonl_tailsafe(path: Path, *, on_error: str = "skip") -> Iterator[dict[str, Any]]:
    """Yield parsed JSON objects from a JSONL file, skipping bad/partial lines.

    Parameters
    ----------
    path : Path
        File to read.
    on_error : {"skip", "raise"}
        - ``"skip"`` (default): non-final lines that fail JSON parse
          are silently skipped.
        - ``"raise"``: any parse failure raises.

    Behavior:
        - Empty file → no events.
        - File ends with ``\\n`` → all lines complete.
        - File does NOT end with ``\\n`` → last line dropped.
        - Mid-file unparseable line → skipped or raises per ``on_error``.
    """
    if on_error not in ("skip", "raise"):
        raise ValueError(f"on_error must be 'skip' or 'raise', got {on_error!r}")

    raw = read_maybe_compressed(path)
    if not raw:
        return

    text = raw.decode("utf-8", errors="replace")

    # Drop partial trailing line if file does not end with newline.
    if not text.endswith("\n"):
        last_nl = text.rfind("\n")
        text = text[: last_nl + 1] if last_nl >= 0 else ""

    # Split on "\n" only: str.splitlines() also breaks on U+2028/U+2029,
    # U+0085, form feed etc., which may legally appear unescaped inside
    # JSON strings - the line would fall apart and be dropped as invalid.
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if on_error == "raise":
                raise
            continue
        if isinstance(obj, dict):
            yield obj


__all__ = ["ZSTD_SUFFIX", "iter_jsonl_tailsafe", "read_maybe_compressed"]
