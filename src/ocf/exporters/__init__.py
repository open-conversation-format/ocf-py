"""Reference exporters: source format -> OCF document.

Each submodule (``codex``, ``claude_code``, ``cursor``, ...) exposes:

- ``DEFAULT_SOURCE_DIR`` — the platform-default location for source data.
- ``export_one(source_path) -> dict`` — convert a single source file to
  an OCF document; validates against the schema before returning.
- ``export_all(out_dir, source_dir=None, ...) -> ExportResult`` — sweep
  all sources, write OCF files, maintain a manifest for incremental runs.

Common types and the manifest implementation live in
:mod:`ocf.exporters._manifest` and :mod:`ocf.exporters._common`.
"""

from ocf.exporters._common import ExportResult

__all__ = ["ExportResult"]
