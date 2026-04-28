"""Strategy-pattern interface for OCF document renderers.

A :class:`Renderer` consumes a validated OCF document dict and produces
a single output string in some target format (Markdown today, HTML/RSS
later). Renderers are deliberately format-agnostic about *where* the
OCF came from — they read the conversation+messages structure defined
by the spec, full stop. Source-format peculiarities (Cursor's
toolFormerData, Claude's parentUuid, etc.) have already been
normalized into OCF by the export step and need no re-handling here.

Why a layer at all instead of "just write a function":

- The :func:`render_all` runner here is generic: directory walk, manifest
  for incremental skip detection, stable output filenames. Renderer
  subclasses only need to implement :meth:`render` and declare a
  filename ``suffix``.
- Future renderers (HTML, single-page-app, RSS, full-text index for
  Meilisearch) drop in without touching the runner or CLI.
- Selection (``--query``, ``--platform``, ``--project``) operates on
  OCF fields and works identically across renderers.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from ocf.core.canonical import sha256_hex
from ocf.exporters._common import ExportResult
from ocf.exporters._manifest import (
    Manifest,
    ManifestEntry,
    load_manifest,
    save_manifest,
)
from ocf.utils.hashing import sha256_file


_RENDER_MANIFEST_NAME = ".ocf-render-manifest.json"

_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
    r"[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$"
)


class Renderer(ABC):
    """Convert a validated OCF document dict into one output string.

    Subclasses declare:

    - :attr:`name` — short id, e.g. ``"markdown"``.
    - :attr:`suffix` — output filename extension *including* the dot
      (``".md"``, ``".html"``).
    - :attr:`mime` — MIME type, used by future ``ocf show`` to pick a
      viewer.

    Subclasses implement :meth:`render`. Everything else (filenames,
    manifest, atomic writes) lives in this module.
    """

    name: ClassVar[str]
    suffix: ClassVar[str]
    mime: ClassVar[str] = "text/plain"

    @abstractmethod
    def render(self, doc: dict[str, Any]) -> str:
        """Convert one OCF document to a single output-format string."""

    def output_filename_for(self, ocf_path: Path) -> str:
        """Map an OCF input file to a renderer-output filename.

        Default: replace ``.ocf.json`` with the renderer's suffix.
        Subclasses can override for layouts that need a different
        scheme (e.g. one HTML file with embedded assets).
        """
        stem = ocf_path.name
        if stem.endswith(".ocf.json"):
            stem = stem[: -len(".ocf.json")]
        elif stem.endswith(".json"):
            stem = stem[: -len(".json")]
        return f"{stem}{self.suffix}"


# ---------------------------------------------------------------------------
# Selection: OCF-field-based filtering against a discovered file set
# ---------------------------------------------------------------------------

def _ocf_corpus_for_match(doc: dict[str, Any]) -> str:
    """Concatenated searchable text for fuzzy-name matching.

    We index the conversation title, project name, platform, model,
    and source.original_id — the same fields users would naturally
    search by. Whitespace-AND match against the user's query.
    """
    conv = doc.get("conversation") or {}
    parts: list[str] = []
    title = conv.get("title")
    if isinstance(title, str):
        parts.append(title)
    proj = conv.get("project") or {}
    if isinstance(proj.get("name"), str):
        parts.append(proj["name"])
    if isinstance(proj.get("description"), str):
        parts.append(proj["description"])
    source = conv.get("source") or {}
    if isinstance(source.get("platform"), str):
        parts.append(source["platform"])
    if isinstance(source.get("original_id"), str):
        parts.append(source["original_id"])
    if isinstance(conv.get("default_model"), str):
        parts.append(conv["default_model"])
    if isinstance(conv.get("id"), str):
        parts.append(conv["id"])
    return " ".join(parts)


def _whitespace_and_match(haystack: str, query: str) -> bool:
    h = haystack.lower()
    words = query.lower().split()
    return bool(words) and all(w in h for w in words)


def select_ocf_files(
    inputs: Iterable[Path],
    *,
    query: str | None = None,
    platform: str | None = None,
    project: str | None = None,
    since: datetime | None = None,
    doc_loader=None,
) -> list[Path]:
    """Filter a list of OCF candidate paths against OCF-field criteria.

    The ``doc_loader`` callable is dependency-injected for testing —
    real callers pass :func:`_load_ocf` which reads + parses JSON.
    """
    if doc_loader is None:
        doc_loader = _load_ocf

    expanded: list[Path] = []
    for p in inputs:
        if p.is_dir():
            expanded.extend(sorted(p.rglob("*.ocf.json")))
        elif p.is_file():
            expanded.append(p)

    if not (query or platform or project or since):
        return expanded

    matched: list[Path] = []
    for p in expanded:
        try:
            doc = doc_loader(p)
        except (OSError, ValueError):
            continue
        conv = doc.get("conversation") or {}

        if platform is not None:
            src = conv.get("source") or {}
            if (src.get("platform") or "").lower() != platform.lower():
                continue

        if project is not None:
            proj = conv.get("project") or {}
            haystack = " ".join(
                str(proj.get(k) or "") for k in ("id", "name", "description")
            )
            if not _whitespace_and_match(haystack, project):
                continue

        if since is not None:
            created = conv.get("created_at")
            if isinstance(created, str):
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                except ValueError:
                    dt = None
                if dt is None or dt < since:
                    continue

        if query is not None:
            # If query looks like a UUID, prefer exact ID match;
            # otherwise fall back to fuzzy-corpus search.
            if _UUID_PATTERN.match(query.strip()):
                qid = query.strip().lower()
                conv_id = (conv.get("id") or "").lower()
                src_id = ((conv.get("source") or {}).get("original_id") or "").lower()
                if qid not in conv_id and qid not in src_id:
                    continue
            else:
                if not _whitespace_and_match(_ocf_corpus_for_match(doc), query):
                    continue

        matched.append(p)
    return matched


def _load_ocf(path: Path) -> dict[str, Any]:
    import json
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Generic render runner
# ---------------------------------------------------------------------------

def render_all(
    renderer: Renderer,
    out_dir: Path,
    *,
    sources: Iterable[Path],
    force: bool = False,
    dry_run: bool = False,
) -> ExportResult:
    """Render each OCF document in ``sources`` to ``out_dir``.

    Mirrors :func:`ocf.exporters._base.export_all`: per-source manifest
    keyed on (mtime, size, sha256), atomic writes via .tmp+rename, all
    failures collected without aborting the whole run. The result is
    an :class:`ExportResult` (re-used for symmetry; "new/updated/skipped"
    semantics carry over).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = _load_render_manifest(out_dir)
    result = ExportResult(out_dir=out_dir if not dry_run else None)

    for src in sources:
        try:
            stat = src.stat()
            mtime_ns = stat.st_mtime_ns
            size = stat.st_size
            source_hash = sha256_file(src)
            entry = manifest.get_entry(src)

            if not force and entry and entry.source_sha256 == source_hash:
                result.skipped.append(src)
                continue

            doc = _load_ocf(src)
            output_text = renderer.render(doc)
            output_filename = renderer.output_filename_for(src)
            output_path = out_dir / output_filename
            output_bytes = output_text.encode("utf-8")
            output_hash = sha256_hex({"_render": output_text})

            if dry_run:
                if entry is None:
                    result.new.append(src)
                else:
                    result.updated.append(src)
                continue

            tmp = output_path.with_suffix(output_path.suffix + ".tmp")
            tmp.write_bytes(output_bytes)
            os.replace(tmp, output_path)

            new_entry = ManifestEntry(
                source_path=str(src),
                source_mtime_ns=mtime_ns,
                source_size=size,
                source_sha256=source_hash,
                ocf_path=output_filename,
                ocf_sha256=output_hash,
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
        _save_render_manifest(out_dir, manifest)
    return result


def _load_render_manifest(out_dir: Path) -> Manifest:
    """Load the renderer manifest (separate from the export manifest)."""
    return load_manifest(out_dir, filename=_RENDER_MANIFEST_NAME)


def _save_render_manifest(out_dir: Path, manifest: Manifest) -> None:
    save_manifest(out_dir, manifest, filename=_RENDER_MANIFEST_NAME)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "Renderer",
    "render_all",
    "select_ocf_files",
]
