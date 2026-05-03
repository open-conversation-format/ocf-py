"""Rich-based live UI for ``ocf watch``.

Layout:

    +-- ocf watch | <adapter> -------- <wallclock> --+
    | Stats: total / active / empty / warnings       |
    +-------------------------------------------------+
    | Recent events (most-recent first):              |
    | <hh:mm:ss>  <kind>  <session>  <detail>         |
    | ...                                             |
    +-------------------------------------------------+
    | <footer hint: q quit>                           |
    +-------------------------------------------------+

Decoupled from the watcher logic: we receive ``WatchEvent``s plus the
latest snapshot stats and just render them. Pure presentation.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Iterable

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ocf.watchers._base import WatchEvent, WatchSnapshot


_KIND_STYLE: dict[str, str] = {
    "session_started": "cyan",
    "message_appended": "green",
    "tool_call": "magenta",
    "session_warning": "yellow",
    "session_idle": "bold red",
}

_KIND_LABEL: dict[str, str] = {
    "session_started": "new",
    "message_appended": "msg",
    "tool_call": "tool",
    "session_warning": "WARN",
    "session_idle": "GHOST",
}


class WatchUI:
    """Holds the rolling event log and renders the layout on demand."""

    def __init__(
        self,
        adapter_name: str,
        max_events: int = 30,
    ) -> None:
        self.adapter_name = adapter_name
        self.events: deque[WatchEvent] = deque(maxlen=max_events)
        # Aggregated counters across the whole session of the watcher
        self.total_tool_calls = 0
        self.total_messages = 0
        self.total_tokens_in = 0
        self.total_tokens_out = 0

    def push(self, events: Iterable[WatchEvent]) -> None:
        for ev in events:
            self.events.append(ev)
            self.total_tool_calls += ev.tool_calls
            self.total_messages += ev.user_messages + ev.assistant_messages
            if ev.tokens_in:
                self.total_tokens_in += ev.tokens_in
            if ev.tokens_out:
                self.total_tokens_out += ev.tokens_out

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def render(self, snapshot: WatchSnapshot) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._header(snapshot), name="header", size=3),
            Layout(self._stats_panel(snapshot), name="stats", size=6),
            Layout(self._events_panel(), name="events", ratio=1),
            Layout(self._footer(), name="footer", size=1),
        )
        return layout

    # ------------------------------------------------------------------
    # Sub-panels
    # ------------------------------------------------------------------

    def _header(self, snapshot: WatchSnapshot) -> Panel:
        title = Text(
            f"ocf watch · {self.adapter_name}",
            style="bold white on dark_blue",
        )
        ts = snapshot.taken_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        right = Text(ts, style="dim")
        # Header is just a single-line panel with title left, time right
        return Panel(
            Text.assemble(title, "  ·  ", right),
            border_style="dark_blue",
            padding=(0, 1),
        )

    def _stats_panel(self, snapshot: WatchSnapshot) -> Panel:
        sessions = list(snapshot.sessions.values())
        total = len(sessions)
        empty = sum(1 for s in sessions if s.bubble_count == 0)
        active = total - empty

        # Warning sessions: any empty session whose age >= 30min,
        # surfaced from the event log (we don't recompute timing here).
        warning_count = sum(
            1
            for ev in self.events
            if ev.kind in ("session_warning", "session_idle")
        )

        table = Table.grid(expand=True, padding=(0, 2))
        table.add_column(justify="left", ratio=1)
        table.add_column(justify="left", ratio=1)
        table.add_column(justify="left", ratio=1)
        table.add_column(justify="left", ratio=1)
        table.add_row(
            Text.assemble(("Sessions ", "dim"), (f"{total}", "bold")),
            Text.assemble(("Active ", "dim"), (f"{active}", "bold green")),
            Text.assemble(("Empty ", "dim"), (f"{empty}", "bold yellow")),
            Text.assemble(("Warnings ", "dim"), (f"{warning_count}", "bold red")),
        )
        table.add_row(
            Text.assemble(
                ("Messages ", "dim"), (f"{self.total_messages}", "bold")
            ),
            Text.assemble(
                ("Tool calls ", "dim"), (f"{self.total_tool_calls}", "bold magenta")
            ),
            Text.assemble(
                ("Tokens in ", "dim"),
                (f"{_fmt_tokens(self.total_tokens_in)}", "bold"),
            ),
            Text.assemble(
                ("Tokens out ", "dim"),
                (f"{_fmt_tokens(self.total_tokens_out)}", "bold"),
            ),
        )
        return Panel(table, title="Stats", border_style="grey50")

    def _events_panel(self) -> Panel:
        table = Table(
            expand=True,
            show_header=True,
            header_style="bold dim",
            box=None,
        )
        table.add_column("Time", width=8, no_wrap=True)
        table.add_column("Kind", width=6, no_wrap=True)
        table.add_column("Session", width=10, no_wrap=True)
        table.add_column("Title", width=28, no_wrap=True)
        table.add_column("Detail", overflow="fold")

        # Most recent first
        for ev in reversed(self.events):
            ts = ev.timestamp.astimezone(timezone.utc).strftime("%H:%M:%S")
            kind_style = _KIND_STYLE.get(ev.kind, "white")
            kind_label = _KIND_LABEL.get(ev.kind, ev.kind)
            sid = ev.session_id[:8]
            title = ev.title or "—"
            detail = ev.detail or ""
            table.add_row(
                Text(ts, style="dim"),
                Text(kind_label, style=kind_style),
                Text(sid, style="dim"),
                Text(title, overflow="ellipsis"),
                Text(detail, style=kind_style if ev.severity != "info" else None),
            )
        return Panel(table, title="Recent events", border_style="grey50")

    def _footer(self) -> Text:
        return Text(
            "  Ctrl+C to quit",
            style="dim",
        )


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def get_console() -> Console:
    return Console()


__all__ = ["WatchUI", "get_console"]
