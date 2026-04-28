"""Incremental-export manifest.

The manifest tracks what was exported in previous runs, keyed by the
absolute source path. A daily cron run can use the manifest to skip
sources that have not changed since the last export.

Layered change detection:

1. **mtime + size**: cheapest, ``stat()`` only. If both match, skip.
2. **source SHA-256**: read file, hash. If same, skip but update
   stored mtime/size to current values (so next ``stat`` matches).
3. **OCF document SHA-256**: only computed when content changed —
   stored alongside the entry so future runs can detect cosmetic
   re-saves of an OCF that produces identical output.

Manifest file lives at ``<out_dir>/.ocf-manifest.json`` (hidden).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
MANIFEST_FILENAME = ".ocf-manifest.json"


@dataclass
class ManifestEntry:
    """One row per source file."""

    source_path: str
    source_mtime_ns: int
    source_size: int
    source_sha256: str
    ocf_path: str  # relative to out_dir
    ocf_sha256: str
    exported_at: str  # ISO 8601 UTC

    def matches_stat(self, stat_result: os.stat_result) -> bool:
        return (
            stat_result.st_mtime_ns == self.source_mtime_ns
            and stat_result.st_size == self.source_size
        )


@dataclass
class Manifest:
    """In-memory representation of ``<out_dir>/.ocf-manifest.json``."""

    manifest_version: int = MANIFEST_VERSION
    last_run_at: str = ""
    exports: dict[str, ManifestEntry] = field(default_factory=dict)

    def get_entry(self, source: Path) -> ManifestEntry | None:
        return self.exports.get(str(source.resolve()))

    def set_entry(self, source: Path, entry: ManifestEntry) -> None:
        self.exports[str(source.resolve())] = entry

    def remove_entry(self, source: Path) -> None:
        self.exports.pop(str(source.resolve()), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "last_run_at": self.last_run_at,
            "exports": {k: asdict(v) for k, v in self.exports.items()},
        }


def manifest_path(out_dir: Path, filename: str = MANIFEST_FILENAME) -> Path:
    return out_dir / filename


def load_manifest(
    out_dir: Path, filename: str = MANIFEST_FILENAME
) -> Manifest:
    """Load the manifest from ``<out_dir>``. Returns an empty manifest
    if the file does not exist or is incompatible.

    The ``filename`` parameter lets the renderer keep its own manifest
    (``.ocf-render-manifest.json``) alongside the export manifest in
    the same directory without conflicts.
    """
    path = manifest_path(out_dir, filename)
    if not path.exists():
        return Manifest()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return Manifest()

    if data.get("manifest_version") != MANIFEST_VERSION:
        # Incompatible — start fresh. Future versions can add migration.
        return Manifest()

    exports_raw = data.get("exports", {})
    exports = {k: ManifestEntry(**v) for k, v in exports_raw.items()}
    return Manifest(
        manifest_version=data["manifest_version"],
        last_run_at=data.get("last_run_at", ""),
        exports=exports,
    )


def save_manifest(
    out_dir: Path, manifest: Manifest, filename: str = MANIFEST_FILENAME
) -> None:
    """Atomically write the manifest to ``<out_dir>/<filename>``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest.last_run_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = manifest_path(out_dir, filename)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest.to_dict(), fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


__all__ = [
    "MANIFEST_VERSION",
    "MANIFEST_FILENAME",
    "ManifestEntry",
    "Manifest",
    "manifest_path",
    "load_manifest",
    "save_manifest",
]
