"""Strategy-pattern base for live session watching.

Format-agnostic data structures. Each adapter (cursor, claude_code,
codex) ships its own ``Watcher`` subclass that knows how to take a
fast :meth:`snapshot` of the current state and emit
:class:`WatchEvent` deltas between two snapshots.

Why a poll-and-diff design instead of true filesystem-events:

- Cursor's state.vscdb is SQLite-WAL — filesystem mtime is updated on
  checkpoint, not per write. ``inotify`` / ``ReadDirectoryChangesW``
  see nothing useful there. Polling is the floor.
- For symmetry across all three adapters, polling everywhere is one
  mental model. The interval is configurable; 5 s is the current
  default — cheap enough that no UI lag is visible, infrequent
  enough that even a 3 GB DB scan finishes well within the tick.
- No native dependencies (``watchdog``, ``inotify-simple``); pure
  stdlib + the project's existing helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

EventKind = Literal[
    "session_started",     # new session/composer appeared
    "message_appended",    # bubble/event count grew
    "tool_call",           # new tool_use / tool_result detected
    "session_warning",     # 30-min empty (or other adapter-specific warning)
    "session_idle",        # 60-min empty: probable ghost session
]

Severity = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class WatchEvent:
    """One observation. Pushed onto the event log by the watch loop."""

    timestamp: datetime
    adapter: str                  # "cursor" / "claude_code" / "codex"
    session_id: str
    kind: EventKind
    severity: Severity = "info"
    title: str | None = None
    detail: str | None = None
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    tokens_in: int | None = None
    tokens_out: int | None = None


# ---------------------------------------------------------------------------
# Snapshot building blocks
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionState:
    """Snapshot of one session at one polling tick.

    Adapter-agnostic: Cursor composer, Claude Code session jsonl, and
    Codex rollout all serialize down to these fields. Adapter-specific
    extras live in :attr:`extra` (untyped) so subclasses can carry
    state without a schema change here.
    """

    session_id: str
    title: str | None
    created_at: datetime | None
    bubble_count: int
    user_messages: int
    assistant_messages: int
    tool_calls: int
    tokens_in: int | None
    tokens_out: int | None
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchSnapshot:
    """All sessions visible to the watcher at one polling tick.

    Two snapshots get diffed by the watcher to produce a stream of
    :class:`WatchEvent`s.
    """

    taken_at: datetime
    sessions: dict[str, SessionState]   # session_id -> state


# ---------------------------------------------------------------------------
# Per-watcher mutable state (warnings fired etc.)
# ---------------------------------------------------------------------------

@dataclass
class WatchState:
    """State that persists across polls but isn't part of a snapshot.

    - fired_*_warning: fire each empty-composer warning exactly once
      per session (otherwise we'd alarm every tick once the threshold
      is crossed).
    - seen_session_started: dedupe new-session events.
    - last_totals: per-session running totals (user/asst/tool/tokens)
      from the last full bubble scan. The watcher uses this to compute
      a real *delta* on the next message_appended event instead of
      reporting raw totals as if they were a delta.
    """

    fired_30min_warning: set[str] = field(default_factory=set)
    fired_60min_warning: set[str] = field(default_factory=set)
    seen_session_started: set[str] = field(default_factory=set)
    last_totals: dict[str, dict[str, int | None]] = field(default_factory=dict)


__all__ = [
    "EventKind",
    "Severity",
    "WatchEvent",
    "SessionState",
    "WatchSnapshot",
    "WatchState",
]
