"""File and bytes hashing helpers.

For canonical-JSON document hashing, see :mod:`ocf.core.canonical`
(``sha256_hex``). This module covers raw-file hashing — used by
exporter manifests for incremental change detection.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming chunks


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of ``path``'s bytes (streaming)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Return the SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


__all__ = ["sha256_file", "sha256_bytes"]
