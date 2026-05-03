"""Live session watchers — Strategy-pattern parallel to exporters.

Each adapter type ships its own Watcher class; the CLI dispatches via
the :data:`WATCHERS` registry. Today only Cursor — Codex and
Claude Code follow once the Cursor watcher has stabilized in real
use against the Cursor ghost-session bug.
"""

from ocf.watchers._base import (
    SessionState,
    WatchEvent,
    WatchSnapshot,
    WatchState,
)
from ocf.watchers.cursor import CursorWatcher

WATCHERS: dict[str, type] = {
    "cursor": CursorWatcher,
}

__all__ = [
    "CursorWatcher",
    "SessionState",
    "WATCHERS",
    "WatchEvent",
    "WatchSnapshot",
    "WatchState",
]
