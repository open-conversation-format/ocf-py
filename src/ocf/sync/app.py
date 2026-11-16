"""``ocf app``: the sync engine with a tray icon and an activity window.

The tray icon shows the state at a glance (green: up to date, blue:
writing, grey: paused, amber: needs attention, red: error). The window
lists every write with time, tool, session and sizes, the sessions
waiting for a pause, and the status of every machine that syncs into
the archive. Closing the window keeps the sync running in the tray.

Threads: tkinter owns the main thread, the engine runs in a worker
thread, pystray runs detached. Engine callbacks and tray menu clicks
only put messages into a queue that the tkinter loop drains.
"""

from __future__ import annotations

import dataclasses
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

import pystray
from PIL import ImageTk

from ocf.sync import secrets, system
from ocf.sync.activity import Activity
from ocf.sync.config import SyncConfig, state_root
from ocf.sync.engine import EngineStatus, SyncEngine, run_loop
from ocf.sync.icons import COLORS, status_icon

logger = logging.getLogger(__name__)

APP_NAME = "OCF Sync"
ACTION_LABELS = {
    "new": "Added",
    "updated": "Updated",
    "error": "Error",
    "warning": "Warning",
    "info": "Info",
}
MAX_ROWS = 1000
STALE_AFTER = 25 * 60  # a running machine refreshes its status every 10 min


def _clock(ts: float | None) -> str:
    if not ts:
        return "-"
    lt = time.localtime(ts)
    if time.strftime("%Y%m%d", lt) == time.strftime("%Y%m%d"):
        return time.strftime("%H:%M:%S", lt)
    return time.strftime("%b %d %H:%M", lt)


def _ago(ts: float | None) -> str:
    if not ts:
        return "-"
    secs = max(0, time.time() - ts)
    if secs < 90:
        return f"{secs:.0f} s ago"
    if secs < 5400:
        return f"{secs / 60:.0f} min ago"
    if secs < 172800:
        return f"{secs / 3600:.0f} h ago"
    return f"{secs / 86400:.0f} days ago"


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


class SyncApp:
    def __init__(self, config: SyncConfig, *, show: bool = False) -> None:
        self.config = config
        self.inbox: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.status = EngineStatus()
        self.activities: list[Activity] = []
        self.notified: set[str] = set()
        self._row_activity: dict[str, Activity] = {}
        self.engine: SyncEngine | None = None
        self.worker: threading.Thread | None = None
        self.settings_dialog: SettingsDialog | None = None

        self.root = tk.Tk()
        self.root.withdraw()
        self._build_window()
        self.icon = pystray.Icon(
            "ocf-sync",
            status_icon("starting"),
            f"{APP_NAME} - starting",
            menu=pystray.Menu(
                pystray.MenuItem("Open OCF Sync", self._from_tray(self.show), default=True),
                pystray.MenuItem("Sync now", self._from_tray(self.sync_now)),
                pystray.MenuItem(
                    lambda item: "Resume" if self.engine and self.engine.paused else "Pause",
                    self._from_tray(self.toggle_pause),
                ),
                pystray.MenuItem("Settings...", self._from_tray(self.open_settings)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Open archive folder", self._from_tray(self.open_archive)),
                pystray.MenuItem("Quit", self._from_tray(self.quit)),
            ),
        )
        if config.archive is None:
            self.message_var.set("Not set up yet")
            self.root.after(300, lambda: self.open_settings(first_run=True))
        else:
            self._start_engine()
            if show:
                self.show()

    # -- engine lifecycle ------------------------------------------------

    def _start_engine(self) -> None:
        self.engine = SyncEngine(
            self.config,
            emit=lambda a: self.inbox.put(("activity", a)),
            on_status=lambda s: self.inbox.put(
                ("status", dataclasses.replace(s, pending=list(s.pending)))
            ),
        )
        self.activities = []
        for activity in self.engine.log.tail(300):
            self.activities.append(activity)
        self._refill_activity()
        self._update_where()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.worker = threading.Thread(
            target=run_loop, args=(self.engine, self.stop_event, self.wake_event),
            name="ocf-sync-engine", daemon=True,
        )
        self.worker.start()

    def _restart_engine(self) -> None:
        """Stop the running engine (finishing its round), start a new one."""
        if self.worker is None or not self.worker.is_alive():
            self._start_engine()
            return
        self.message_var.set("Restarting with the new settings ...")
        self.stop_event.set()
        self.wake_event.set()

        def wait() -> None:
            if self.worker is not None and self.worker.is_alive():
                self.root.after(200, wait)
            else:
                self._start_engine()

        self.root.after(200, wait)

    def apply_settings(self, *, restart: bool) -> None:
        self._update_where()
        if restart or self.engine is None:
            self._restart_engine()
            return
        engine = self.engine
        cfg = self.config
        wake = self.wake_event

        def update() -> None:
            engine.set_index(cfg.meili_url, None, cfg.index_name)
            wake.set()

        threading.Thread(target=update, daemon=True).start()

    # -- window ----------------------------------------------------------

    def _build_window(self) -> None:
        root = self.root
        root.title(APP_NAME)
        root.geometry("1040x620")
        root.minsize(720, 420)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        self._window_icon = ImageTk.PhotoImage(status_icon("idle", 64))
        root.iconphoto(True, self._window_icon)

        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        import tkinter.font as tkfont

        base = tkfont.nametofont("TkDefaultFont")
        line = base.metrics("linespace")
        style.configure("Treeview", rowheight=line + 8)
        title_font = base.copy()
        title_font.configure(size=base.cget("size") + 5, weight="bold")
        status_font = base.copy()
        status_font.configure(size=base.cget("size") + 1, weight="bold")
        self._fonts = (title_font, status_font)

        outer = ttk.Frame(root, padding=(16, 12, 16, 12))
        outer.pack(fill="both", expand=True)

        # Header: icon, title, state, buttons
        head = ttk.Frame(outer)
        head.pack(fill="x")
        self.state_images = {s: ImageTk.PhotoImage(status_icon(s, 40)) for s in COLORS}
        self.state_label = ttk.Label(head, image=self.state_images["starting"])
        self.state_label.pack(side="left", padx=(0, 12))
        text = ttk.Frame(head)
        text.pack(side="left", fill="x", expand=True)
        ttk.Label(text, text=APP_NAME, font=title_font).pack(anchor="w")
        self.message_var = tk.StringVar(value="Starting")
        ttk.Label(text, textvariable=self.message_var, font=status_font).pack(anchor="w")
        self.facts_var = tk.StringVar(value="")
        ttk.Label(text, textvariable=self.facts_var, foreground="#555").pack(anchor="w")

        buttons = ttk.Frame(head)
        buttons.pack(side="right")
        self.pause_button = ttk.Button(buttons, text="Pause", command=self.toggle_pause, width=11)
        self.pause_button.pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Sync now", command=self.sync_now, width=11).pack(side="right")
        ttk.Button(buttons, text="Settings", command=self.open_settings, width=11).pack(
            side="right", padx=(0, 6))
        self.progress = ttk.Progressbar(buttons, mode="indeterminate", length=90)

        # Banner: missing API key, updates, restart
        self.banner_var = tk.StringVar(value="")
        self.banner = tk.Frame(outer, background="#fff4d6")
        tk.Label(self.banner, textvariable=self.banner_var, anchor="w", justify="left",
                 padx=10, pady=6, background="#fff4d6", foreground="#6b4a00",
                 wraplength=820).pack(side="left", fill="x", expand=True)
        self.banner_button = ttk.Button(self.banner, text="Enter API key",
                                        command=self.open_settings)

        self.where_var = tk.StringVar(value="")
        self.where_label = ttk.Label(outer, textvariable=self.where_var, foreground="#555")
        self.where_label.pack(fill="x", pady=(10, 6))
        self._update_where()

        # Tabs
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        self.activity_tree = self._tree(
            self.tabs,
            [("time", "Time", 110, False), ("action", "Action", 80, False),
             ("tool", "Tool", 130, False), ("session", "Session", 360, True),
             ("details", "Details", 330, True)],
        )
        self.tabs.add(self.activity_tree.master, text="Activity")
        self.waiting_tree = self._tree(
            self.tabs,
            [("session", "Session", 420, True), ("tool", "Tool", 140, False),
             ("changed", "Last change", 120, False), ("writes", "Writes", 140, False)],
        )
        self.tabs.add(self.waiting_tree.master, text="Waiting")
        self.machines_tree = self._tree(
            self.tabs,
            [("machine", "Machine", 140, False), ("state", "State", 160, False),
             ("write", "Last write", 140, False), ("code", "Code", 90, False),
             ("index", "Index", 110, False), ("recent", "Last session written", 380, True)],
        )
        self.tabs.add(self.machines_tree.master, text="Machines")

        tree = self.activity_tree
        for kind, color in (("error", "#c62828"), ("warning", "#9a6700"), ("info", "#6b6b6b")):
            tree.tag_configure(kind, foreground=color)
        tree.bind("<Double-1>", lambda e: self._open_selected("md"))
        tree.bind("<Button-3>", self._context_menu)
        self.menu = tk.Menu(root, tearoff=False)
        self.menu.add_command(label="Open Markdown", command=lambda: self._open_selected("md"))
        self.menu.add_command(label="Open OCF file", command=lambda: self._open_selected("ocf"))
        self.menu.add_command(label="Copy session file name", command=self._copy_doc)

        # Footer
        foot = ttk.Frame(outer)
        foot.pack(fill="x", pady=(10, 0))
        self.show_info = tk.BooleanVar(value=True)
        ttk.Checkbutton(foot, text="Show system messages", variable=self.show_info,
                        command=self._refill_activity).pack(side="left")
        self.autostart_var = tk.BooleanVar(value=system.autostart_enabled())
        if sys.platform == "win32":
            ttk.Checkbutton(foot, text="Start at login", variable=self.autostart_var,
                            command=self._toggle_autostart).pack(side="left", padx=(16, 0))
        ttk.Button(foot, text="Log folder", command=lambda: self._open(state_root())).pack(side="right")
        if self.config.archive_md:
            ttk.Button(foot, text="Markdown folder",
                       command=lambda: self._open(self.config.archive_md)).pack(side="right", padx=(0, 6))
        ttk.Button(foot, text="Archive folder", command=self.open_archive).pack(side="right", padx=(0, 6))

    def _tree(self, parent: tk.Widget, columns: list[tuple[str, str, int, bool]]) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        for key, label, width, stretch in columns:
            tree.heading(key, text=label, anchor="w")
            tree.column(key, width=width, stretch=stretch, anchor="w")
        bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        return tree

    # -- activity ----------------------------------------------------------

    def _visible(self, a: Activity) -> bool:
        return self.show_info.get() or a.kind in ("new", "updated", "error")

    def _add_activity(self, a: Activity, *, live: bool = True) -> None:
        self.activities.append(a)
        if len(self.activities) > MAX_ROWS * 2:
            self.activities = self.activities[-MAX_ROWS:]
        if self._visible(a):
            self._insert_row(a, at_top=True)
        if live and a.kind == "error" and a.message not in self.notified:
            self.notified.add(a.message)
            try:
                self.icon.notify(a.message, APP_NAME)
            except Exception:  # noqa: BLE001
                pass

    def _insert_row(self, a: Activity, *, at_top: bool) -> None:
        tree = self.activity_tree
        values = (_clock(a.ts), ACTION_LABELS.get(a.kind, a.kind), a.tool,
                  a.title or a.message, a.detail)
        iid = tree.insert("", 0 if at_top else "end", values=values,
                          tags=(a.kind,))
        self._row_activity[iid] = a
        children = tree.get_children()
        if len(children) > MAX_ROWS:
            for old in children[MAX_ROWS:]:
                self._row_activity.pop(old, None)
                tree.delete(old)

    def _refill_activity(self) -> None:
        tree = self.activity_tree
        tree.delete(*tree.get_children())
        self._row_activity = {}
        for a in reversed(self.activities[-MAX_ROWS:]):
            if self._visible(a):
                self._insert_row(a, at_top=False)

    def _selected(self) -> Activity | None:
        sel = self.activity_tree.selection()
        return self._row_activity.get(sel[0]) if sel else None

    def _open_selected(self, what: str) -> None:
        a = self._selected()
        if a is None or not a.doc:
            return
        if what == "md" and a.md_path:
            self._open(a.md_path)
        elif self.config.archive:
            self._open(Path(self.config.archive) / f"{a.doc}.ocf.json")

    def _copy_doc(self) -> None:
        a = self._selected()
        if a and a.doc:
            self.root.clipboard_clear()
            self.root.clipboard_append(a.doc)

    def _context_menu(self, event: tk.Event) -> None:
        row = self.activity_tree.identify_row(event.y)
        if row:
            self.activity_tree.selection_set(row)
            a = self._row_activity.get(row)
            if a and a.doc:
                self.menu.tk_popup(event.x_root, event.y_root)

    # -- status ----------------------------------------------------------

    def _apply_status(self, st: EngineStatus) -> None:
        self.status = st
        state = st.state if st.state in COLORS else "busy"
        self.state_label.configure(image=self.state_images[state])
        self.message_var.set(st.message)
        facts = [f"Last check {_clock(st.last_scan_at)}",
                 f"last write {_clock(st.last_write_at)}",
                 f"{st.written} written since start"]
        if self.config.meili_url:
            index = {"ok": "ok", "offline": f"offline, {st.index_backlog} waiting",
                     "pending": "not contacted yet", "no-key": "skipped, no API key",
                     }.get(st.index, st.index)
            facts.append(f"search index {self.config.index_name}: {index}")
        else:
            facts.append("search index off")
        self.facts_var.set("   |   ".join(facts))
        self.pause_button.configure(
            text="Resume" if self.engine and self.engine.paused else "Pause")
        if st.state == "busy":
            if not self.progress.winfo_ismapped():
                self.progress.pack(side="right", padx=(0, 10))
                self.progress.start(12)
        elif self.progress.winfo_ismapped():
            self.progress.stop()
            self.progress.pack_forget()

        banner, key_banner = "", False
        if st.index == "no-key":
            waiting = (f" {st.index_backlog} session(s) wait for it."
                       if st.index_backlog else "")
            banner = ("No API key for the search index: sessions are archived, but not "
                      f"indexed (not searchable) until a key is set.{waiting}")
            key_banner = True
        elif st.restart_needed:
            banner = (f"The ocf-py checkout changed since this app started (running "
                      f"{st.running_head}). Quit and start OCF Sync again to use the new code.")
        elif st.behind:
            banner = (f"{st.behind} new ocf-py commit(s) on the remote. Run 'git pull' and "
                      "'uv sync --extra app' in the ocf-py checkout, then restart OCF Sync, "
                      "so all machines run the same version.")
        self.banner_var.set(banner)
        if key_banner and not self.banner_button.winfo_ismapped():
            self.banner_button.pack(side="right", padx=8, pady=4)
        elif not key_banner and self.banner_button.winfo_ismapped():
            self.banner_button.pack_forget()
        if banner and not self.banner.winfo_ismapped():
            self.banner.pack(fill="x", pady=(10, 0), before=self.where_label)
        elif not banner and self.banner.winfo_ismapped():
            self.banner.pack_forget()

        self.icon.icon = status_icon(state)
        self.icon.title = f"{APP_NAME} - {st.message}"[:127]
        self.tabs.tab(1, text=f"Waiting ({len(st.pending)})" if st.pending else "Waiting")

    def _refresh_waiting(self) -> None:
        tree = self.waiting_tree
        tree.delete(*tree.get_children())
        quiet, max_delay = self.config.quiet, self.config.max_delay
        now = time.time()
        for p in self.status.pending:
            due = p.due_at(quiet, max_delay)
            writes = "now" if due <= now else f"in {due - now:.0f} s ({_clock(due)})"
            if self.engine and self.engine.paused:
                writes = "paused"
            tree.insert("", "end", values=(p.title or p.source.name, p.tool, _clock(p.last_change), writes))

    def _refresh_machines(self) -> None:
        tree = self.machines_tree
        tree.delete(*tree.get_children())
        for m in system.read_machine_statuses(self.config.archive):
            seen = system.parse_iso_utc(m.get("updated_at"))
            state = m.get("state", "?")
            if state != "stopped" and seen and time.time() - seen > STALE_AFTER:
                state = f"silent since {_ago(seen)}"
            elif state == "stopped":
                state = f"stopped {_ago(seen)}"
            behind = m.get("commits_behind")
            code = (m.get("code") or "?") + (f" (-{behind})" if behind else "")
            index = m.get("index") or "-"
            if m.get("index_backlog"):
                index += f", {m['index_backlog']} waiting"
            recent = m.get("recent") or []
            last = (f"{recent[0].get('title')} ({recent[0].get('tool')})" if recent else "-")
            name = m.get("machine", "?")
            if name == self.config.machine:
                name += " (this)"
            tree.insert("", "end", values=(
                name, state, _clock(system.parse_iso_utc(m.get("last_write_at"))),
                code, index, last,
            ))

    # -- actions -----------------------------------------------------------

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._refresh_machines()

    def hide(self) -> None:
        self.root.withdraw()

    def sync_now(self) -> None:
        self.wake_event.set()

    def toggle_pause(self) -> None:
        if self.engine is None:
            return
        threading.Thread(target=self.engine.set_paused, args=(not self.engine.paused,),
                         daemon=True).start()

    def open_settings(self, first_run: bool = False) -> None:
        if self.settings_dialog is not None and self.settings_dialog.winfo_exists():
            self.settings_dialog.lift()
            self.settings_dialog.focus_force()
            return
        self.show()
        self.settings_dialog = SettingsDialog(self, first_run=first_run)

    def _update_where(self) -> None:
        cfg = self.config
        where = (f"Machine {cfg.machine}   |   Archive {cfg.archive or '(not set)'}"
                 + (f"   |   Markdown {cfg.archive_md}" if cfg.archive_md else ""))
        self.where_var.set(where)

    def open_archive(self) -> None:
        if self.config.archive:
            self._open(self.config.archive)

    def _open(self, path: Path | str) -> None:
        try:
            system.open_path(path)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Cannot open {path}:\n{exc}")

    def _toggle_autostart(self) -> None:
        want = self.autostart_var.get()
        try:
            system.set_autostart(want)
        except Exception as exc:  # noqa: BLE001
            self.autostart_var.set(not want)
            messagebox.showerror(APP_NAME, f"Cannot change autostart:\n{exc}")

    def quit(self) -> None:
        self.message_var.set("Stopping ...")
        self.stop_event.set()
        self.wake_event.set()
        self.root.after(100, self._finish_quit, time.time())

    def _finish_quit(self, started: float) -> None:
        if self.worker is not None and self.worker.is_alive() and time.time() - started < 20:
            self.root.after(200, self._finish_quit, started)
            return
        try:
            self.icon.stop()
        finally:
            self.root.destroy()

    def _from_tray(self, fn: Callable[[], None]) -> Callable[..., None]:
        return lambda *_: self.inbox.put(("call", fn))

    # -- loop ------------------------------------------------------------

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.inbox.get_nowait()
                if kind == "activity":
                    self._add_activity(payload)
                elif kind == "status":
                    self._apply_status(payload)
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.root.after(250, self._drain)

    def _tick_views(self) -> None:
        if self.root.state() != "withdrawn":
            self._refresh_waiting()
        self.root.after(1000, self._tick_views)

    def _tick_machines(self) -> None:
        if self.root.state() != "withdrawn":
            self._refresh_machines()
        self.root.after(30_000, self._tick_machines)

    def run(self) -> int:
        self.icon.run_detached()
        self.root.after(250, self._drain)
        self.root.after(1000, self._tick_views)
        self.root.after(2000, self._tick_machines)
        self.root.mainloop()
        return 0


class SettingsDialog(tk.Toplevel):
    """Folders, search index (URL, index name, API key) and timing."""

    def __init__(self, app: SyncApp, *, first_run: bool = False) -> None:
        super().__init__(app.root)
        self.app = app
        cfg = app.config
        self.title(f"{APP_NAME} settings")
        self.transient(app.root)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        stored_key = secrets.get_key(cfg.meili_url) or ""
        self.v_archive = tk.StringVar(value=str(cfg.archive or ""))
        self.v_md = tk.StringVar(value=str(cfg.archive_md or ""))
        self.v_url = tk.StringVar(value=cfg.meili_url or "")
        self.v_index = tk.StringVar(value=cfg.index_name)
        self.v_key = tk.StringVar(value=stored_key)
        self.v_show = tk.BooleanVar(value=False)
        self.v_interval = tk.StringVar(value=f"{cfg.interval:g}")
        self.v_quiet = tk.StringVar(value=f"{cfg.quiet:g}")
        self.v_max = tk.StringVar(value=f"{cfg.max_delay:g}")
        self.v_test = tk.StringVar(value="")

        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        hint = {"foreground": "#666", "wraplength": 560, "justify": "left"}
        row = 0

        def section(text: str) -> None:
            nonlocal row
            ttk.Label(body, text=text, font=app._fonts[1]).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(14 if row else 0, 6))
            row += 1

        def field(label: str, var: tk.Variable, browse: bool = False, **entry: Any) -> ttk.Entry:
            nonlocal row
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
            widget = ttk.Entry(body, textvariable=var, width=58, **entry)
            widget.grid(row=row, column=1, sticky="we", pady=3)
            if browse:
                ttk.Button(body, text="Browse...", command=lambda: self._browse(var)).grid(
                    row=row, column=2, padx=(6, 0))
            row += 1
            return widget

        def note(text: str) -> None:
            nonlocal row
            ttk.Label(body, text=text, **hint).grid(row=row, column=1, columnspan=2,
                                                     sticky="w", pady=(0, 4))
            row += 1

        if first_run:
            ttk.Label(body, text=(
                "Choose the shared archive folder (the same on every machine, e.g. on a "
                "cloud drive). The Markdown folder and the search index are optional."
            ), **hint).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
            row += 1

        section("Folders")
        field("Archive", self.v_archive, browse=True)
        field("Markdown", self.v_md, browse=True)
        note("Leave Markdown empty to skip rendering.")

        section("Search index (Meilisearch)")
        field("URL", self.v_url)
        field("Index", self.v_index)
        self.key_entry = field("API key", self.v_key, show="*")
        ttk.Checkbutton(body, text="Show", variable=self.v_show,
                        command=self._toggle_show).grid(row=row - 1, column=2, padx=(6, 0))
        store = ("Windows Credential Manager" if sys.platform == "win32"
                 else "the system credential store")
        note(f"The key is kept in {store}, never in a file. Without a key the "
             "sessions are still archived; indexing is skipped with a warning, and the "
             "skipped sessions are indexed as soon as a key is set.")
        if not stored_key and os.environ.get("MEILI_MASTER_KEY"):
            note("Currently used: MEILI_MASTER_KEY from the environment.")
        test_row = ttk.Frame(body)
        test_row.grid(row=row, column=1, columnspan=2, sticky="w", pady=(2, 0))
        ttk.Button(test_row, text="Test connection", command=self._test).pack(side="left")
        self.test_label = ttk.Label(test_row, textvariable=self.v_test, wraplength=430)
        self.test_label.pack(side="left", padx=(10, 0))
        row += 1

        section("Timing (seconds)")
        field("Scan every", self.v_interval)
        field("Write after quiet", self.v_quiet)
        field("Write at least every", self.v_max)
        note("A changed session is written once it has been quiet this long, "
             "and at least this often while it keeps changing.")

        buttons = ttk.Frame(body)
        buttons.grid(row=row, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", command=self._save).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda e: self.destroy())
        self.update_idletasks()
        x = app.root.winfo_rootx() + max(0, (app.root.winfo_width() - self.winfo_width()) // 2)
        y = app.root.winfo_rooty() + 40
        self.geometry(f"+{x}+{y}")
        self.grab_set()
        self.focus_force()

    def _browse(self, var: tk.StringVar) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(parent=self, initialdir=var.get() or None,
                                         mustexist=True)
        if chosen:
            var.set(str(Path(chosen)))

    def _toggle_show(self) -> None:
        self.key_entry.configure(show="" if self.v_show.get() else "*")

    def _test(self) -> None:
        url = self.v_url.get().strip()
        key = self.v_key.get().strip()
        name = self.v_index.get().strip() or "ocf-sessions"
        if not url:
            self.v_test.set("Enter a URL first.")
            return
        self.v_test.set("Testing ...")

        def work() -> None:
            ok, text = _test_meili(url, key, name)
            self.app.inbox.put(("call", lambda: self._show_test(ok, text)))

        threading.Thread(target=work, daemon=True).start()

    def _show_test(self, ok: bool, text: str) -> None:
        if self.winfo_exists():
            self.v_test.set(text)
            self.test_label.configure(foreground="#2e7d32" if ok else "#c62828")

    def _number(self, var: tk.StringVar, label: str) -> float | None:
        try:
            value = float(var.get().replace(",", "."))
        except ValueError:
            value = 0.0
        if value <= 0:
            messagebox.showerror(APP_NAME, f"{label}: enter a number of seconds.", parent=self)
            return None
        return value

    def _save(self) -> None:
        cfg = self.app.config
        archive = self.v_archive.get().strip()
        if not archive:
            messagebox.showerror(APP_NAME, "Choose the archive folder.", parent=self)
            return
        if not Path(archive).is_dir() and not messagebox.askyesno(
            APP_NAME, f"{archive} does not exist (cloud drive not mounted?).\n"
            "Save anyway? The sync waits until the folder is reachable.", parent=self):
            return
        timing = [self._number(v, label) for v, label in (
            (self.v_interval, "Scan every"), (self.v_quiet, "Write after quiet"),
            (self.v_max, "Write at least every"))]
        if None in timing:
            return
        url = self.v_url.get().strip().rstrip("/") or None
        key = self.v_key.get().strip()
        if url:
            try:
                secrets.set_key(url, key)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(APP_NAME, f"Cannot store the API key:\n{exc}", parent=self)
                return
        old = (cfg.archive, cfg.archive_md)
        cfg.archive = Path(archive)
        cfg.archive_md = Path(self.v_md.get().strip()) if self.v_md.get().strip() else None
        cfg.meili_url = url
        cfg.meili_key = None
        cfg.index_name = self.v_index.get().strip() or "ocf-sessions"
        cfg.interval, cfg.quiet, cfg.max_delay = timing
        try:
            cfg.save()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Cannot save the settings:\n{exc}", parent=self)
            return
        self.destroy()
        self.app.apply_settings(restart=old != (cfg.archive, cfg.archive_md))


def _test_meili(url: str, key: str, index_name: str) -> tuple[bool, str]:
    """Check that the server answers and accepts the key for the index."""
    try:
        import meilisearch
        from meilisearch.errors import MeilisearchApiError
    except ImportError:
        return False, "meilisearch package missing (uv sync --extra app)"
    try:
        client = meilisearch.Client(url, key or None, timeout=10)
        client.health()
    except Exception as exc:  # noqa: BLE001
        return False, f"Server not reachable: {exc}"
    try:
        client.get_raw_index(index_name)
    except MeilisearchApiError as exc:
        if exc.code == "index_not_found":
            return True, f"Key accepted. Index {index_name} will be created."
        if exc.code in ("missing_authorization_header", "invalid_api_key"):
            return False, "Key missing or rejected by the server."
        return False, f"{exc.code or exc.status_code}: {exc.message}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Request failed: {exc}"
    try:
        docs = client.index(index_name).get_stats().number_of_documents
        return True, f"Key accepted. Index {index_name}: {docs:,} documents."
    except Exception:  # noqa: BLE001
        return True, f"Key accepted. Index {index_name} found."


def _setup_logging() -> None:
    log_dir = state_root()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "sync.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    sys.excepthook = lambda t, v, tb: logging.getLogger("ocf").critical(
        "unhandled exception", exc_info=(t, v, tb))


def _dpi_aware() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass


def run_app(config: SyncConfig, *, show: bool = False) -> int:
    _setup_logging()
    _dpi_aware()
    lock = system.InstanceLock(state_root() / "sync.lock")
    if not lock.acquire():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, "OCF Sync is already running on this machine.\n"
                            "Look for its icon in the notification area.")
        root.destroy()
        return 1
    try:
        app = SyncApp(config, show=show)
        return app.run()
    except Exception:
        logger.exception("app crashed")
        raise
    finally:
        lock.release()


__all__ = ["SyncApp", "run_app"]
