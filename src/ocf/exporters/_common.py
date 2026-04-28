"""Shared types for exporters."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExportResult:
    """Outcome of an :func:`export_all` run.

    Attributes
    ----------
    new : list[Path]
        Source files that produced a freshly created OCF document.
    updated : list[Path]
        Source files whose existing OCF document was replaced because
        the source changed.
    skipped : list[Path]
        Source files unchanged since the last export (manifest hit).
    failed : list[tuple[Path, str]]
        ``(source_path, error_message)`` for files that errored during
        conversion or validation. The exporter does not abort on
        single-file failures; it collects them all here.
    out_dir : Path | None
        The directory written to (None for dry-run).
    """

    new: list[Path] = field(default_factory=list)
    updated: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)
    out_dir: Path | None = None

    @property
    def total_processed(self) -> int:
        return len(self.new) + len(self.updated) + len(self.skipped) + len(self.failed)

    def summary(self) -> str:
        parts = [
            f"{len(self.new)} new",
            f"{len(self.updated)} updated",
            f"{len(self.skipped)} skipped",
        ]
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return ", ".join(parts)
