"""Strategy-pattern interface for source-format adapters.

Each AI tool's session storage format is one :class:`SourceAdapter`:
Codex (Responses API JSONL), Claude Code (Anthropic JSONL), Cursor
(SQLite KV), and future ones (OpenCode, Kilo CLI, ...).

Adapters supply only the format-specific bits:

- ``default_source_dirs()`` - where the tool stores by default.
- ``export_one(source)`` - convert a single source to an OCF dict.
- Optional overrides for ``find_by_name``, ``find_by_id``,
  ``_search_corpus`` to add format-specific search semantics.

The :func:`export_all` runner here is fully generic - manifest,
incremental skip detection, atomic write, and schema validation work
identically for any adapter.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from ocf.core.canonical import dumps as canonical_dumps
from ocf.core.canonical import sha256_hex
from ocf.exporters._common import ExportResult
from ocf.exporters._manifest import (
    ManifestEntry,
    load_manifest,
    save_manifest,
)
from ocf.utils.hashing import sha256_file


_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
    r"[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$"
)


# ---------------------------------------------------------------------------
# Lightweight session metadata (for ``list`` UI)
# ---------------------------------------------------------------------------

@dataclass
class SessionInfo:
    """Minimal session metadata for display in ``ocf list``.

    Adapters produce these from :meth:`SourceAdapter.session_info`
    (cheap peek, no full conversion). The CLI uses them to render a
    human-readable table instead of dumping raw file paths.
    """

    source: Path
    session_id: str
    title: str | None = None
    project: str | None = None
    created_at: datetime | None = None
    model: str | None = None
    is_empty: bool = False
    """True if the source exists but contains no exportable messages.

    These are the same sources :meth:`SourceAdapter.export_one` would
    raise :class:`SkipExport` on (Cursor ghost composers, Claude Code
    heartbeats). Surfaced here so the ``ocf list`` UI can filter or
    count them without paying the full export cost.
    """


class AmbiguousMatchError(LookupError):
    """Multiple sessions matched a name- or id-based query.

    The CLI catches this to prompt the user; library callers can
    inspect :attr:`candidates` and decide programmatically.
    """

    def __init__(self, query: str, candidates: list[Path]) -> None:
        self.query = query
        self.candidates = candidates
        super().__init__(
            f"Query {query!r} matched {len(candidates)} sessions; "
            "specify which to export."
        )


class SkipExport(Exception):
    """Adapter signals "this source has no exportable content — skip it".

    Distinct from a conversion failure (which goes to :attr:`ExportResult.failed`).
    Use this when the source file is structurally valid but contains
    nothing worth archiving:

    - Cursor: composer entry with zero bubbles (user clicked "New Chat"
      and never typed anything, ~57% of composers on the test machine).
    - Claude Code: one-shot heartbeat / health-check templates
      (``"Antworte exakt mit 'PONG'"``, ``{"status": "ok"}`` JSON
      probes — observed in editor-extension keep-alive checks).

    The runner records the skip in the manifest with ``ocf_path=""``
    so subsequent runs don't re-evaluate the same empty source. The
    ``reason`` is stored for debugging.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class SourceAdapter(ABC):
    """Adapter contract for a single AI tool's session storage format.

    Subclasses implement format-specific behavior; the runner
    (:func:`export_all`) and bulk semantics live in this module.
    """

    name: ClassVar[str]
    """Short identifier: 'codex', 'claude_code', 'cursor', 'opencode', ..."""

    mapping_id: ClassVar[str]
    """Goes into ``conversation.produced_by.mapping_id`` of OCF documents."""

    rollout_glob: ClassVar[str] = "*.jsonl"
    """Default filename pattern for :meth:`discover`. Override per adapter."""

    # ------------------------------------------------------------------
    # Required overrides
    # ------------------------------------------------------------------

    @abstractmethod
    def default_source_dirs(self) -> list[Path]:
        """Where this tool stores sessions by default.

        Returns a list (possibly single-element) so adapters with
        fragmented storage (Claude Code: ``~/.claude/projects/`` PLUS
        ``%APPDATA%/Claude/local-agent-mode-sessions/``) can return all
        relevant locations and the default :meth:`discover` scans all
        of them.
        """

    @abstractmethod
    def export_one(self, source: Path, *, validate: bool = True) -> dict[str, Any]:
        """Convert one source file/locator to an OCF document dict.

        If ``validate`` is True, the produced dict is schema-validated
        before return; raises ``jsonschema.ValidationError`` on
        violation. Otherwise the dict is returned as-is and the caller
        is responsible for validation.
        """

    # ------------------------------------------------------------------
    # Session info (overridable — for ``ocf list`` display)
    # ------------------------------------------------------------------

    def session_info(self, source: Path) -> SessionInfo:
        """Lightweight metadata peek for one source.

        Default returns just the path and stem. Adapters override to
        peek the file for title, cwd, date. Called once per discovered
        source in ``ocf list``; keep it fast.
        """
        return SessionInfo(source=source, session_id=source.stem)

    # ------------------------------------------------------------------
    # Source fingerprinting (overridable for non-file sources)
    # ------------------------------------------------------------------

    def source_fingerprint(
        self, source: Path
    ) -> tuple[int, int, str]:
        """Return ``(mtime_ns, size, sha256_hex)`` for incremental skip detection.

        Default implementation treats ``source`` as a regular file —
        works for Codex (rollout-*.jsonl) and Claude Code (session jsonl).
        Adapters whose sources are not regular files (e.g. CursorAdapter
        with ``state.vscdb::composerData:<id>`` synthetic tokens) MUST
        override this to compute a stable fingerprint from whatever
        their source actually is (DB row hash, etc.).
        """
        stat = source.stat()
        return (stat.st_mtime_ns, stat.st_size, sha256_file(source))

    def ocf_filename_for(self, source: Path) -> str:
        """Map a source to its OCF output filename.

        Default: strip ``.jsonl`` or ``.json`` from ``source.name`` and
        append ``.ocf.json``. Adapters whose sources are synthetic
        tokens (e.g. CursorAdapter's ``state.vscdb::composerData:<id>``)
        override this to produce a filesystem-legal filename — Windows
        rejects ``:`` in filenames except for drive prefixes.
        """
        stem = source.name
        for ext in (".jsonl", ".json"):
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        return f"{stem}.ocf.json"

    # ------------------------------------------------------------------
    # Discovery (default impl, overridable)
    # ------------------------------------------------------------------

    def discover(
        self, source_dirs: list[Path] | Path | None = None
    ) -> list[Path]:
        """Find all session files under the given source directories.

        Defaults to :meth:`default_source_dirs`. A single ``Path`` is
        accepted as a convenience and treated as a one-element list.
        Sub-adapters with non-file source models (e.g. SQLite rows)
        override this entirely.
        """
        dirs = self._normalize_source_dirs(source_dirs)
        results: list[Path] = []
        for d in dirs:
            # EAFP: skip the exists()/is_dir() guard and let rglob
            # discover what's there.  Some Windows terminal emulators
            # (cmder/ConEmu) hook GetFileAttributesW in ways that make
            # Path.exists() return False for directories that DO exist,
            # while FindFirstFileW (used by rglob via os.scandir) works.
            try:
                results.extend(sorted(d.rglob(self.rollout_glob)))
            except OSError:
                continue
        return results

    def find_by_name(
        self,
        query: str,
        *,
        source_dirs: list[Path] | Path | None = None,
        case_sensitive: bool = False,
    ) -> list[Path]:
        """Fuzzy-match session files against ``query`` (whitespace AND).

        Default uses :meth:`_search_corpus` (path components only).
        Adapters override to add metadata (title indexes, cwd, ...).
        """
        if not query.strip():
            return []
        matches: list[Path] = []
        for path in self.discover(source_dirs):
            corpus = self._search_corpus(path)
            if self._match(corpus, query, case_sensitive=case_sensitive):
                matches.append(path)
        return matches

    def find_by_id(
        self,
        session_id: str,
        *,
        source_dirs: list[Path] | Path | None = None,
    ) -> list[Path]:
        """Find sessions whose UUID/identifier matches ``session_id``.

        Default impl: substring match against filename. Most adapters
        embed the session UUID in the filename (Codex's
        ``rollout-...-<uuid>.jsonl``, Claude Code's ``<sid>.jsonl``),
        so the default works. Override to read internal session_meta.
        """
        if not session_id.strip():
            return []
        # Normalize: hyphens optional in UUIDs, but most filenames use them
        sid = session_id.strip().lower()
        matches: list[Path] = []
        for path in self.discover(source_dirs):
            if sid in path.name.lower():
                matches.append(path)
        return matches

    # ------------------------------------------------------------------
    # Polymorphic dispatch
    # ------------------------------------------------------------------

    def resolve_sources(
        self,
        source: Path | str | None,
        *,
        source_dirs: list[Path] | Path | None = None,
        case_sensitive: bool = False,
    ) -> list[Path]:
        """Resolve a polymorphic ``source`` argument into source files.

        Resolution rules:

        1. ``None`` -> all sessions under ``source_dirs`` (or default).
        2. Existing directory -> recurse it.
        3. Existing file -> single-element list.
        4. UUID-shaped string -> :meth:`find_by_id`.
        5. Otherwise -> :meth:`find_by_name`.
        """
        if source is None:
            return self.discover(source_dirs)

        candidate = Path(source) if isinstance(source, str) else source
        if candidate.is_dir():
            return self.discover(candidate)
        if candidate.is_file():
            return [candidate]

        s = str(source).strip()
        if _UUID_PATTERN.match(s):
            return self.find_by_id(s, source_dirs=source_dirs)
        return self.find_by_name(
            s, source_dirs=source_dirs, case_sensitive=case_sensitive
        )

    # ------------------------------------------------------------------
    # Hooks for subclasses
    # ------------------------------------------------------------------

    def _search_corpus(self, path: Path) -> str:
        """Searchable metadata corpus for one source file.

        Default: filename + relative path components from the first
        matching ``default_source_dirs()`` entry. Adapters override to
        peek the file for additional metadata (cwd, title, etc.).
        """
        parts = [path.name]
        for d in self.default_source_dirs():
            try:
                rel = path.resolve().relative_to(d.resolve())
                parts.extend(rel.parent.parts)
                break
            except ValueError:
                continue
        return " ".join(parts)

    @staticmethod
    def _match(haystack: str, query: str, *, case_sensitive: bool = False) -> bool:
        """Whitespace-AND substring match. Helper for find_by_name."""
        if not case_sensitive:
            haystack = haystack.lower()
            query = query.lower()
        words = query.split()
        return bool(words) and all(w in haystack for w in words)

    def _normalize_source_dirs(
        self, source_dirs: list[Path] | Path | None
    ) -> list[Path]:
        if source_dirs is None:
            return list(self.default_source_dirs())
        if isinstance(source_dirs, Path):
            return [source_dirs]
        return [Path(d) for d in source_dirs]


# ---------------------------------------------------------------------------
# Generic export_all runner
# ---------------------------------------------------------------------------

def export_all(
    adapter: SourceAdapter,
    out_dir: Path,
    *,
    sources: Iterable[Path] | None = None,
    source_dirs: list[Path] | Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> ExportResult:
    """Generic bulk-export runner — works for any :class:`SourceAdapter`.

    Source resolution:

    1. If ``sources`` is given, those exact files are exported.
    2. Else ``adapter.discover(source_dirs)`` is run.

    The manifest, skip-detection (mtime+size, then sha256), atomic
    write, validation, and failure collection are identical across
    all adapters and live here.
    """
    if sources is not None:
        rollout_files = [Path(s) for s in sources]
    else:
        rollout_files = adapter.discover(source_dirs)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(out_dir)
    result = ExportResult(out_dir=out_dir if not dry_run else None)

    for src in rollout_files:
        try:
            mtime_ns, size, source_hash = adapter.source_fingerprint(src)
            entry = manifest.get_entry(src)

            stat_match = (
                entry is not None
                and entry.source_mtime_ns == mtime_ns
                and entry.source_size == size
            )
            if not force and stat_match:
                result.skipped.append(src)
                continue

            if not force and entry and entry.source_sha256 == source_hash:
                if not dry_run:
                    entry.source_mtime_ns = mtime_ns
                    entry.source_size = size
                    manifest.set_entry(src, entry)
                result.skipped.append(src)
                continue

            try:
                doc = adapter.export_one(src, validate=True)
            except SkipExport as skip:
                # Adapter says: nothing to export here. Record the skip
                # in the manifest (ocf_path="" sentinel) so the next run
                # treats this source as up-to-date and we don't waste
                # work re-evaluating an empty composer / heartbeat
                # source on every cron tick.
                if not dry_run:
                    new_entry = ManifestEntry(
                        source_path=str(src),
                        source_mtime_ns=mtime_ns,
                        source_size=size,
                        source_sha256=source_hash,
                        ocf_path="",  # sentinel: was skipped, no output
                        ocf_sha256="",
                        exported_at=_now_iso(),
                    )
                    manifest.set_entry(src, new_entry)
                result.skipped.append(src)
                continue
            ocf_filename = adapter.ocf_filename_for(src)
            ocf_path = out_dir / ocf_filename
            ocf_bytes = canonical_dumps(doc)
            ocf_hash = sha256_hex(doc)

            if dry_run:
                if entry is None:
                    result.new.append(src)
                else:
                    result.updated.append(src)
                continue

            tmp = ocf_path.with_suffix(ocf_path.suffix + ".tmp")
            tmp.write_bytes(ocf_bytes)
            os.replace(tmp, ocf_path)

            new_entry = ManifestEntry(
                source_path=str(src),
                source_mtime_ns=mtime_ns,
                source_size=size,
                source_sha256=source_hash,
                ocf_path=ocf_filename,
                ocf_sha256=ocf_hash,
                exported_at=_now_iso(),
            )
            manifest.set_entry(src, new_entry)

            if entry is None:
                result.new.append(src)
            else:
                result.updated.append(src)
        except Exception as exc:  # noqa: BLE001
            result.failed.append((src, f"{type(exc).__name__}: {exc}"))

    if not dry_run:
        save_manifest(out_dir, manifest)

    return result


def _ocf_filename_for(source: Path) -> str:
    stem = source.name
    for ext in (".jsonl", ".json"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    return f"{stem}.ocf.json"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "AmbiguousMatchError",
    "SessionInfo",
    "SkipExport",
    "SourceAdapter",
    "export_all",
]
