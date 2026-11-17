"""The sync engine: scan, wait for a pause, export, render, index.

One :meth:`SyncEngine.tick` does a full round:

1. **Scan** every tool's session storage. Plain session files are
   compared by mtime and size (cheap). Tools with an expensive scan
   (Cursor) expose :meth:`SourceAdapter.change_marker`; while it stays
   the same they are not scanned at all, and never more often than
   ``slow_scan``.
2. **Debounce.** A changed session waits until it has not changed for
   ``quiet`` seconds, or until it first changed ``max_delay`` seconds
   ago. The first scan after start is a catch-up: every source goes to
   the exporter at once and the manifest skips the unchanged ones.
3. **Export** the due sessions into the archive with the local
   per-machine manifest. Byte-identical results are not rewritten, so a
   synced folder does not upload them again.
4. **Render** the written OCF documents to Markdown.
5. **Index** their chunks into Meilisearch (upsert, then drop trailing
   chunks the session no longer has). If the index is unreachable the
   sessions wait in a backlog and are retried.
6. **Report** one activity per written session and refresh this
   machine's status file in ``<archive>/.ocf-machines/``.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ocf import __version__
from ocf.exporters._base import SourceAdapter, export_all
from ocf.exporters._common import ExportResult
from ocf.exporters._manifest import MANIFEST_FILENAME
from ocf.renderers._base import _RENDER_MANIFEST_NAME, Renderer, render_all
from ocf.sync import system
from ocf.sync.activity import Activity, ActivityLog
from ocf.sync.config import SyncConfig

logger = logging.getLogger(__name__)

OCF_SUFFIX = ".ocf.json"
HEARTBEAT_EVERY = 600.0
UPDATE_CHECK_EVERY = 3600.0
INDEX_RETRY_EVERY = 60.0
TRIM_BATCH = 50
PUSH_BATCH = 200


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

def build_adapters(tools: list[str], *, codex_imports: bool = False) -> dict[str, SourceAdapter]:
    """Adapter instances for the given CLI tool names."""
    from ocf.exporters.chatgpt import ChatGPTAdapter
    from ocf.exporters.claude_ai import ClaudeAiAdapter
    from ocf.exporters.claude_code import (
        ClaudeCodeAppAdapter,
        ClaudeCodeCliAdapter,
        ClaudeCoworkAppAdapter,
    )
    from ocf.exporters.codex import CodexAdapter
    from ocf.exporters.cursor import CursorAdapter

    registry: dict[str, type[SourceAdapter]] = {
        "chatgpt": ChatGPTAdapter,
        "claude-ai": ClaudeAiAdapter,
        "claude-code-cli": ClaudeCodeCliAdapter,
        "claude-code-app": ClaudeCodeAppAdapter,
        "claude-cowork-app": ClaudeCoworkAppAdapter,
        "codex": CodexAdapter,
        "cursor": CursorAdapter,
    }
    unknown = [t for t in tools if t not in registry]
    if unknown:
        raise ValueError(f"unknown tool(s): {', '.join(unknown)}")
    adapters = {t: registry[t]() for t in tools}
    if "codex" in adapters:
        adapters["codex"].include_imports = codex_imports  # type: ignore[attr-defined]
    return adapters


# ---------------------------------------------------------------------------
# Search index
# ---------------------------------------------------------------------------

class MeiliPusher:
    """Pushes the chunks of single sessions into the Meilisearch index."""

    def __init__(self, url: str, key: str, index_name: str) -> None:
        self.url = url
        self.key = key
        self.index_name = index_name
        self._client: Any = None
        self._index: Any = None

    def reset(self) -> None:
        self._client = self._index = None

    def _connect(self) -> tuple[Any, Any]:
        if self._index is None:
            import meilisearch

            from ocf.indexers.meilisearch import ensure_index

            client = meilisearch.Client(self.url, self.key, timeout=30)
            client.health()
            self._index = ensure_index(client, self.index_name)
            self._client = client
        return self._index, self._client

    def push(self, items: list[tuple[Path, bytes, dict[str, Any]]]) -> dict[Path, int]:
        """Index the given archive documents; returns chunks per path."""
        from ocf.indexers.meilisearch import (
            archive_tool_name,
            enqueue_documents,
            make_chunk_documents,
            wait_for_tasks,
        )

        index, client = self._connect()
        docs: list[dict[str, Any]] = []
        counts: dict[Path, int] = {}
        trims: list[str] = []
        for path, raw, doc in items:
            if not doc.get("messages"):
                counts[path] = 0
                continue
            key = path.name[: -len(OCF_SUFFIX)]
            chunks = make_chunk_documents(
                doc, archive_tool_name(doc), doc_key=key, archive_bytes=len(raw)
            )
            docs.extend(chunks)
            counts[path] = len(chunks)
            safe = key.replace("\\", "\\\\").replace('"', '\\"')
            trims.append(f'(doc = "{safe}" AND chunk > {len(chunks)})')
        uids = [enqueue_documents(index, docs[i : i + PUSH_BATCH])
                for i in range(0, len(docs), PUSH_BATCH)]
        for i in range(0, len(trims), TRIM_BATCH):
            uids.append(index.delete_documents(filter=" OR ".join(trims[i : i + TRIM_BATCH])).task_uid)
        failed = wait_for_tasks(client, uids)
        if failed:
            raise RuntimeError("; ".join(sorted(set(failed.values()))))
        return counts


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class Pending:
    """A session that changed and waits for a pause before it is written."""

    tool: str
    source: Path
    first_change: float
    last_change: float
    title: str = ""
    reconvert: bool = False
    """New to this tool's scan (e.g. reclassified from CLI to Desktop):
    export without the manifest's skip, so its labels are current."""

    def due_at(self, quiet: float, max_delay: float) -> float:
        return min(self.last_change + quiet, self.first_change + max_delay)


@dataclass
class EngineStatus:
    """Snapshot for the UI. ``state``: starting, idle, busy, paused,
    warning (index offline, update available) or error."""

    state: str = "starting"
    message: str = "Starting"
    last_scan_at: float | None = None
    last_write_at: float | None = None
    pending: list[Pending] = field(default_factory=list)
    index: str = "off"
    index_backlog: int = 0
    behind: int | None = None
    restart_needed: bool = False
    running_head: str | None = None
    written: int = 0
    problem: str = ""


@dataclass
class _Written:
    tool: str
    source: Path
    ocf: Path
    action: str


def _size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class SyncEngine:
    def __init__(
        self,
        config: SyncConfig,
        *,
        adapters: dict[str, SourceAdapter] | None = None,
        renderer: Renderer | None = None,
        pusher: Any = None,
        emit: Callable[[Activity], None] | None = None,
        on_status: Callable[[EngineStatus], None] | None = None,
        activity_log: ActivityLog | None = None,
        clock: Callable[[], float] = time.time,
        check_updates: bool = True,
    ) -> None:
        if config.archive is None:
            raise ValueError("no archive configured")
        self.config = config
        self.archive = Path(config.archive)
        self.md_dir = Path(config.archive_md) if config.archive_md else None
        self.adapters = (adapters if adapters is not None
                         else build_adapters(config.tools, codex_imports=config.codex_imports))
        if renderer is None and self.md_dir is not None:
            from ocf.renderers.markdown import MarkdownRenderer

            renderer = MarkdownRenderer()
        self.renderer = renderer if self.md_dir is not None else None
        self.state_dir = config.state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log = activity_log or ActivityLog(self.state_dir / "activity.jsonl")
        self._emit_cb = emit
        self._status_cb = on_status
        self.clock = clock

        self.status = EngineStatus()
        self.pusher: Any = None
        self._configure_index(pusher)
        self._seen: dict[str, dict[str, Any]] = {}
        self._sources: dict[str, dict[str, Path]] = {}
        self._markers: dict[str, Any] = {}
        self._last_scan: dict[str, float] = {}
        self._pending: dict[tuple[str, str], Pending] = {}
        self._catchup: dict[str, list[Path]] = {}
        self._reconvert: set[str] = set()
        self._index_backlog: dict[Path, None] = self._load_backlog()
        self._next_index_retry = 0.0
        self._seeded = False
        self._problems: dict[str, str] = {}
        self._session_failures: dict[str, str] = {}
        self._recent: list[dict[str, Any]] = []
        self._last_heartbeat = 0.0
        self._started = False
        self._started_at = 0.0
        self._paused = False
        self._lock = threading.RLock()

        self._repo = system.repo_root() if check_updates else None
        self.status.running_head = system.git_head(self._repo)
        self._code_at_start = system.code_fingerprint() if self._repo else ""
        self._next_update_check = 0.0 if self._repo else math.inf

    # -- lifecycle -------------------------------------------------------

    def _configure_index(self, pusher: Any = None) -> None:
        """Set up indexing from the config: a URL and a key index, a URL
        without key only collects the sessions to index later."""
        cfg = self.config
        if pusher is None and cfg.index_configured():
            key = cfg.resolved_key()
            if key:
                pusher = MeiliPusher(cfg.meili_url or "", key, cfg.index_name)
        self.pusher = pusher
        if pusher is not None:
            self.status.index = "pending"
        elif cfg.index_configured():
            self.status.index = "no-key"
        else:
            self.status.index = "off"

    def set_index(self, url: str | None, key: str | None, index_name: str) -> None:
        """Change the search index while running (e.g. a key was entered).

        Sessions written without index wait in the backlog and are pushed
        on the next round.
        """
        with self._lock:
            had_key = self.pusher is not None
            self.config.meili_url = url or None
            self.config.meili_key = key or None
            self.config.index_name = index_name or self.config.index_name
            self._configure_index()
            self._next_index_retry = 0.0
            self._problems.pop("index", None)
            if self.pusher is not None and not had_key:
                self._emit(Activity(
                    "info", "Search index enabled",
                    detail=f"{len(self._index_backlog)} session(s) to index"
                    if self._index_backlog else "",
                ))
            elif self.pusher is None and self.config.index_configured():
                self._emit(Activity("warning", "Search index skipped: no API key"))
            self._settle()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._started_at = self.clock()
            if self.pusher is not None:
                index = f"index {self.config.index_name} at {self.config.meili_url}"
            elif self.config.index_configured():
                index = "search index skipped: no API key"
            else:
                index = "no search index"
            render = "Markdown on" if self.renderer else "no Markdown"
            self._emit(Activity(
                "info",
                f"Sync started on {self.config.machine}",
                detail=f"{len(self.adapters)} tools, {render}, {index}",
            ))
            if self.status.index == "no-key":
                self._emit(Activity(
                    "warning", "Search index skipped: no API key",
                    detail="sessions are archived and wait for the index; "
                    "set the key in Settings",
                ))
            if self.archive.is_dir():
                self._seed_manifests()
                self._heartbeat(force=True)

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._emit(Activity("info", f"Sync stopped on {self.config.machine}"))
            self._heartbeat(force=True, stopped=True)
            self._started = False

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            if paused == self._paused:
                return
            self._paused = paused
            self._emit(Activity("info", "Sync paused" if paused else "Sync resumed"))
            self._settle()

    @property
    def paused(self) -> bool:
        return self._paused

    # -- the round -------------------------------------------------------

    def tick(self, *, force: bool = False) -> list[Activity]:
        """One scan-export-render-index round. ``force`` writes every
        waiting session now and rescans tools with a change marker."""
        with self._lock:
            self.start()
            now = self.clock()
            if not self.archive.is_dir():
                self._problem("archive", f"Archive not reachable: {self.archive}")
                self._settle()
                return []
            self._clear_problem("archive", announce="Archive reachable again")
            self._seed_manifests()

            # Plain scans take well under a second and run every few
            # seconds; they do not flip the state to busy (no blinking).
            for tool, adapter in self.adapters.items():
                try:
                    self._scan(tool, adapter, now, force)
                except Exception as exc:  # noqa: BLE001
                    self._problem(f"scan:{tool}", f"Scanning {tool} failed: {exc}", tool=tool)
                else:
                    self._clear_problem(f"scan:{tool}")
            self.status.last_scan_at = now

            written: list[_Written] = []
            catching_up = sum(len(v) for v in self._catchup.values())
            due = self._take_due(now, force)
            if due:
                total = sum(len(v) for v in due.values())
                self._set_status("busy", (
                    f"Catching up: comparing {total} sessions with the archive"
                    if catching_up else f"Exporting {total} changed session(s)"
                ))
                for tool, sources in due.items():
                    written.extend(self._export(tool, sources))

            produced: list[Activity] = []
            retry_index = (
                self.pusher is not None
                and self._index_backlog
                and (force or now >= self._next_index_retry)
            )
            if written or retry_index:
                steps = " and ".join(
                    s for s, on in (("rendering", self.renderer is not None),
                                    ("indexing", self.pusher is not None)) if on
                ) or "saving"
                self._set_status("busy", f"{steps.capitalize()} {len(written)} session(s)"
                                 if written else "Indexing waiting sessions")
                produced = self._finish(written, now)

            self._maybe_check_updates(now)
            self._heartbeat(force=bool(written))
            self._settle()
            return produced

    # -- scan + debounce ---------------------------------------------------

    def _scan(self, tool: str, adapter: SourceAdapter, now: float, force: bool) -> None:
        first = tool not in self._seen
        marker = adapter.change_marker()
        if not first and not force and marker is not None:
            if marker == self._markers.get(tool):
                return
            if now - self._last_scan.get(tool, 0.0) < self.config.slow_scan:
                return
        self._markers[tool] = marker
        self._last_scan[tool] = now

        # New sessions since the last scan (e.g. a fork in Claude Desktop)
        # must be seen in the tool's current metadata, not a cached copy.
        adapter.refresh()
        sources = adapter.discover()
        expensive = marker is not None
        current: dict[str, Any] = {}
        by_key: dict[str, Path] = {}
        for src in sources:
            key = str(src)
            try:
                current[key] = self._fingerprint(adapter, src, expensive)
            except Exception:  # noqa: BLE001 - vanished or unreadable; next scan
                continue
            by_key[key] = src

        if first:
            self._catchup[tool] = list(by_key.values())
        else:
            previous = self._seen.get(tool, {})
            for key, fp in current.items():
                if previous.get(key) != fp:
                    self._mark_pending(tool, adapter, by_key[key], now,
                                       reconvert=key not in previous)
        self._seen[tool] = current
        self._sources[tool] = by_key

    @staticmethod
    def _fingerprint(adapter: SourceAdapter, src: Path, expensive: bool) -> Any:
        if expensive:
            return adapter.source_fingerprint(src)[2]
        try:
            st = src.stat()
        except OSError:
            # Synthetic source tokens (``<zip>::<member>``) are no files.
            return adapter.source_fingerprint(src)
        return (st.st_mtime_ns, st.st_size)

    def _mark_pending(self, tool: str, adapter: SourceAdapter, src: Path, now: float,
                      *, reconvert: bool = False) -> None:
        key = (tool, str(src))
        pending = self._pending.get(key)
        if pending is not None:
            pending.last_change = now
            pending.reconvert |= reconvert
            return
        title = ""
        try:
            title = adapter.session_info(src).title or ""
        except Exception:  # noqa: BLE001
            pass
        self._pending[key] = Pending(tool, src, now, now, title, reconvert)

    def _take_due(self, now: float, force: bool) -> dict[str, list[Path]]:
        due: dict[str, dict[str, Path]] = {}
        for tool, sources in self._catchup.items():
            bucket = due.setdefault(tool, {})
            for src in sources:
                bucket[str(src)] = src
        self._catchup.clear()
        for key, pending in list(self._pending.items()):
            if force or now >= pending.due_at(self.config.quiet, self.config.max_delay):
                due.setdefault(pending.tool, {})[str(pending.source)] = pending.source
                if pending.reconvert:
                    self._reconvert.add(str(pending.source))
                del self._pending[key]
        return {tool: list(bucket.values()) for tool, bucket in due.items() if bucket}

    # -- export ------------------------------------------------------------

    def _export(self, tool: str, sources: list[Path]) -> list[_Written]:
        adapter = self.adapters[tool]
        fresh = [s for s in sources if str(s) in self._reconvert]
        known = [s for s in sources if str(s) not in self._reconvert]
        try:
            result = ExportResult()
            for group, force in ((known, False), (fresh, True)):
                if not group:
                    continue
                part = export_all(
                    adapter, self.archive, sources=group, force=force,
                    manifest_dir=self.state_dir, skip_identical=True, keep_newer=True,
                )
                for name in ("new", "updated", "skipped", "failed", "vanished", "kept_newer"):
                    getattr(result, name).extend(getattr(part, name))
            self._reconvert.difference_update(str(s) for s in fresh)
        except Exception as exc:  # noqa: BLE001 - e.g. archive gone mid-run
            self._problem(f"export:{tool}", f"Export of {tool} failed: {exc}", tool=tool)
            self._catchup.setdefault(tool, []).extend(sources)
            return []
        self._clear_problem(f"export:{tool}")
        out = [
            _Written(tool, src, self.archive / adapter.ocf_filename_for(src), action)
            for action, group in (("new", result.new), ("updated", result.updated))
            for src in group
        ]
        for src, message in result.failed:
            key = str(src)
            if self._session_failures.get(key) != message:
                self._session_failures[key] = message
                self._emit(Activity(
                    "error", f"Export failed: {src.name}", tool=tool, detail=message,
                ))
        kept = {str(src) for src, _ in result.kept_newer}
        for src, reason in result.kept_newer:
            key = f"kept:{src}"
            if self._session_failures.get(key) != reason:
                self._session_failures[key] = reason
                name = adapter.ocf_filename_for(src)
                title = ""
                try:
                    title = adapter.session_info(src).title or ""
                except Exception:  # noqa: BLE001
                    pass
                self._emit(Activity(
                    "warning", f"Kept the newer archive version: {title or name}",
                    tool=tool, title=title, doc=name[: -len(OCF_SUFFIX)],
                    detail=f"{reason}; this machine's copy of the session is older "
                    "(another machine continued it)",
                ))
        for src in [*result.new, *result.updated, *result.skipped]:
            self._session_failures.pop(str(src), None)
            if str(src) not in kept:
                self._session_failures.pop(f"kept:{src}", None)
        return out

    # -- render, index, report ------------------------------------------

    def _finish(self, written: list[_Written], now: float) -> list[Activity]:
        md_paths: dict[Path, Path] = {}
        if written and self.renderer is not None and self.md_dir is not None:
            try:
                result = render_all(
                    self.renderer, self.md_dir, sources=[w.ocf for w in written],
                    manifest_dir=self.state_dir, skip_identical=True,
                )
                for src, message in result.failed:
                    self._emit(Activity("error", f"Render failed: {src.name}", detail=message))
            except Exception as exc:  # noqa: BLE001
                self._problem("render", f"Rendering failed: {exc}")
            else:
                self._clear_problem("render")
            for w in written:
                md = self.md_dir / self.renderer.output_filename_for(w.ocf)
                if md.exists():
                    md_paths[w.ocf] = md

        loaded: dict[Path, tuple[bytes, dict[str, Any]]] = {}
        for w in written:
            try:
                raw = w.ocf.read_bytes()
                loaded[w.ocf] = (raw, json.loads(raw))
            except (OSError, json.JSONDecodeError) as exc:
                self._emit(Activity("error", f"Cannot read {w.ocf.name}", tool=w.tool, detail=str(exc)))

        chunks: dict[Path, int] = {}
        index_ok = False
        if self.pusher is not None:
            chunks, index_ok = self._push_index([w.ocf for w in written if w.ocf in loaded], loaded, now)
        elif self.config.index_configured():
            for w in written:
                self._index_backlog[w.ocf] = None
            self._save_backlog()

        produced: list[Activity] = []
        for w in written:
            if w.ocf not in loaded:
                continue
            raw, doc = loaded[w.ocf]
            conv = doc.get("conversation") or {}
            stem = w.ocf.name[: -len(OCF_SUFFIX)]
            title = (conv.get("title") or "").strip() or stem
            parts = [f"OCF {_size(len(raw))}"]
            md = md_paths.get(w.ocf)
            if md is not None:
                try:
                    parts.append(f"md {_size(md.stat().st_size)}")
                except OSError:
                    pass
            if self.pusher is not None:
                if index_ok and w.ocf in chunks:
                    n = chunks[w.ocf]
                    parts.append(f"{n} chunk{'s' if n != 1 else ''} indexed" if n else "not indexed (empty)")
                else:
                    parts.append("index pending")
            elif self.config.index_configured():
                parts.append("not indexed (no API key)")
            parts.append(f"{len(doc.get('messages') or []):,} messages")
            activity = Activity(
                w.action, title, tool=w.tool, title=title, doc=stem,
                detail=", ".join(parts), md_path=str(md) if md else "",
            )
            self._emit(activity)
            produced.append(activity)
            self._recent.append({"at": system.iso_utc(activity.ts), "action": w.action,
                                 "tool": w.tool, "title": title, "doc": stem})
        self._recent = self._recent[-20:]
        if produced:
            self.status.written += len(produced)
            self.status.last_write_at = now
        return produced

    def _push_index(
        self,
        fresh: list[Path],
        loaded: dict[Path, tuple[bytes, dict[str, Any]]],
        now: float,
    ) -> tuple[dict[Path, int], bool]:
        targets = list(dict.fromkeys([*self._index_backlog, *fresh]))
        items: list[tuple[Path, bytes, dict[str, Any]]] = []
        for path in targets:
            if path in loaded:
                items.append((path, *loaded[path]))
                continue
            try:
                raw = path.read_bytes()
                items.append((path, raw, json.loads(raw)))
            except (OSError, json.JSONDecodeError):
                self._index_backlog.pop(path, None)
        if not items:
            self._save_backlog()
            return {}, True
        try:
            counts = self.pusher.push(items)
        except Exception as exc:  # noqa: BLE001
            for path, _, _ in items:
                self._index_backlog[path] = None
            self._save_backlog()
            self._next_index_retry = now + INDEX_RETRY_EVERY
            self.status.index = "offline"
            self.status.index_backlog = len(self._index_backlog)
            self._problem(
                "index",
                f"Search index not reachable, {len(self._index_backlog)} session(s) waiting",
                detail=str(exc),
            )
            reset = getattr(self.pusher, "reset", None)
            if reset:
                reset()
            return {}, False
        retried = len([p for p in self._index_backlog if p not in fresh])
        self._index_backlog.clear()
        self._save_backlog()
        self.status.index = "ok"
        self.status.index_backlog = 0
        if retried:
            self._problems.pop("index", None)
            self._emit(Activity("info", f"{retried} waiting session(s) indexed"))
        else:
            self._clear_problem("index", announce="Search index reachable again")
        return counts, True

    # -- index backlog (survives restarts) -----------------------------------

    def _backlog_file(self) -> Path:
        return self.state_dir / "index-backlog.json"

    def _load_backlog(self) -> dict[Path, None]:
        try:
            names = json.loads(self._backlog_file().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {self.archive / n: None for n in names if isinstance(n, str)}

    def _save_backlog(self) -> None:
        path = self._backlog_file()
        try:
            if not self._index_backlog:
                path.unlink(missing_ok=True)
                return
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(sorted(p.name for p in self._index_backlog)), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("cannot save the index backlog", exc_info=True)

    # -- problems, status, heartbeat ---------------------------------------

    def _problem(self, key: str, message: str, *, tool: str = "", detail: str = "") -> None:
        if self._problems.get(key) != message:
            self._emit(Activity("error", message, tool=tool, detail=detail))
        self._problems[key] = message

    def _clear_problem(self, key: str, *, announce: str = "") -> None:
        if self._problems.pop(key, None) is not None and announce:
            self._emit(Activity("info", announce))

    def _set_status(self, state: str, message: str) -> None:
        self.status.state = state
        self.status.message = message
        self._publish_status()

    def _settle(self) -> None:
        """Derive the resting state after a round."""
        st = self.status
        st.pending = sorted(self._pending.values(),
                            key=lambda p: p.due_at(self.config.quiet, self.config.max_delay))
        st.index_backlog = len(self._index_backlog)
        hard = [m for k, m in self._problems.items() if k != "index"]
        if hard:
            st.state, st.message, st.problem = "error", hard[-1], hard[-1]
        elif self._paused:
            st.state, st.message, st.problem = "paused", "Paused", ""
        elif "index" in self._problems:
            st.state, st.message = "warning", self._problems["index"]
            st.problem = self._problems["index"]
        elif st.index == "no-key":
            waiting = (f", {st.index_backlog} session(s) wait for it"
                       if st.index_backlog else "")
            st.state, st.problem = "warning", ""
            st.message = f"Archive up to date, search index skipped (no API key){waiting}"
        else:
            st.problem = ""
            waiting = f", {len(st.pending)} waiting" if st.pending else ""
            st.state, st.message = "idle", f"Up to date{waiting}"
            if st.restart_needed or st.behind:
                st.state = "warning"
                st.message = ("Restart to run the updated code" if st.restart_needed
                              else f"Update available ({st.behind} commit(s)){waiting}")
        self._publish_status()

    def _publish_status(self) -> None:
        if self._status_cb is not None:
            try:
                self._status_cb(self.status)
            except Exception:  # noqa: BLE001
                logger.exception("status callback failed")

    def _emit(self, activity: Activity) -> None:
        try:
            self.log.append(activity)
        except OSError:
            logger.exception("cannot write activity log")
        if self._emit_cb is not None:
            try:
                self._emit_cb(activity)
            except Exception:  # noqa: BLE001
                logger.exception("activity callback failed")

    def _seed_manifests(self) -> None:
        """Copy the shared manifests once, when the archive is reachable."""
        if self._seeded:
            return
        self._seeded = True
        pairs = [(self.archive / MANIFEST_FILENAME, MANIFEST_FILENAME, "export")]
        if self.md_dir is not None:
            pairs.append((self.md_dir / _RENDER_MANIFEST_NAME, _RENDER_MANIFEST_NAME, "render"))
        for shared, name, label in pairs:
            local = self.state_dir / name
            if local.exists() or not shared.is_file():
                continue
            try:
                shutil.copyfile(shared, local)
            except OSError as exc:
                self._emit(Activity("warning", f"Cannot seed the local {label} manifest", detail=str(exc)))
                continue
            self._emit(Activity(
                "info", f"Local {label} manifest seeded from the archive",
                detail="sessions exported before stay skipped",
            ))

    def _maybe_check_updates(self, now: float) -> None:
        if now < self._next_update_check:
            return
        self._next_update_check = now + UPDATE_CHECK_EVERY
        behind = system.git_commits_behind(self._repo)
        head = system.git_head(self._repo)
        # Compare the code itself, not the commit: committing the running
        # code changes the hash but needs no restart.
        restart = system.code_fingerprint() != self._code_at_start
        if behind and behind != self.status.behind:
            self._emit(Activity(
                "warning", f"{behind} new ocf-py commit(s) on the remote",
                detail="pull, then restart OCF Sync, so every machine runs the same version",
            ))
        if restart and not self.status.restart_needed:
            self._emit(Activity(
                "warning", "ocf-py code changed on disk, restart OCF Sync",
                detail=f"started at {self.status.running_head}, checkout is now at {head}",
            ))
        self.status.behind = behind
        self.status.restart_needed = restart

    def _heartbeat(self, *, force: bool = False, stopped: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_heartbeat < HEARTBEAT_EVERY:
            return
        st = self.status
        data = {
            "machine": self.config.machine,
            "state": "stopped" if stopped else ("paused" if self._paused else "running"),
            "ocf_py": __version__,
            "code": st.running_head,
            "commits_behind": st.behind,
            "updated_at": system.iso_utc(now),
            "started_at": system.iso_utc(self._started_at),
            "last_scan_at": system.iso_utc(st.last_scan_at),
            "last_write_at": system.iso_utc(st.last_write_at),
            "tools": sorted(self.adapters),
            "waiting": len(self._pending),
            "index": st.index,
            "index_backlog": len(self._index_backlog),
            "written_since_start": st.written,
            "recent": list(reversed(self._recent[-10:])),
        }
        try:
            system.write_machine_status(self.archive, self.config.machine, data)
        except OSError as exc:
            logger.warning("cannot write machine status: %s", exc)
            return
        self._last_heartbeat = now


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

def run_loop(
    engine: SyncEngine,
    stop: threading.Event,
    wake: threading.Event,
) -> None:
    """Run rounds every ``interval`` seconds until ``stop`` is set.

    Setting ``wake`` starts a forced round right away ("Sync now"); a
    forced round also runs while paused.
    """
    engine.start()
    try:
        while not stop.is_set():
            forced = wake.is_set()
            wake.clear()
            if forced or not engine.paused:
                try:
                    engine.tick(force=forced)
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    logger.exception("sync round failed")
                    engine._problem("round", f"Sync round failed: {exc}")  # noqa: SLF001
                else:
                    engine._clear_problem("round")  # noqa: SLF001
            wake.wait(engine.config.interval)
    finally:
        engine.stop()


__all__ = [
    "EngineStatus",
    "MeiliPusher",
    "Pending",
    "SyncEngine",
    "build_adapters",
    "run_loop",
]
