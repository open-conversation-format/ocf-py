"""Tests for the Cursor watcher.

Two layers:

1. Pure ``diff`` against synthetic snapshots — exercises every event
   kind (session_started, message_appended, tool_call,
   session_warning, session_idle) plus the ``WatchState``
   fire-once-per-session contract. No real DB involved.
2. End-to-end ``snapshot`` against a real synthetic state.vscdb
   (using the same fixture pattern as the cursor exporter tests)
   to confirm the SQL queries actually match the live schema.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ocf.watchers._base import (
    SessionState,
    WatchEvent,
    WatchSnapshot,
    WatchState,
)
from ocf.watchers.cursor import (
    EMPTY_IDLE_AFTER,
    EMPTY_WARNING_AFTER,
    CursorWatcher,
)


# ---------------------------------------------------------------------------
# Helpers to build synthetic snapshots
# ---------------------------------------------------------------------------

def _session(
    sid: str,
    *,
    bubble_count: int = 0,
    title: str | None = None,
    created_at: datetime | None = None,
) -> SessionState:
    return SessionState(
        session_id=sid,
        title=title,
        created_at=created_at,
        bubble_count=bubble_count,
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        tokens_in=None,
        tokens_out=None,
        extra={},
    )


def _snapshot(taken_at: datetime, sessions: list[SessionState]) -> WatchSnapshot:
    return WatchSnapshot(
        taken_at=taken_at,
        sessions={s.session_id: s for s in sessions},
    )


# ---------------------------------------------------------------------------
# diff(): session_started fires for new sessions only once
# ---------------------------------------------------------------------------

def test_session_started_fires_for_new_composer(tmp_path: Path) -> None:
    """A composer that wasn't in the previous snapshot triggers a
    session_started event. Subsequent ticks where it's still around
    don't re-fire."""
    watcher = CursorWatcher(db_path=tmp_path / "nonexistent.vscdb")
    state = WatchState()
    t0 = datetime(2026, 4, 30, 10, 0, 0, tzinfo=timezone.utc)

    prev = _snapshot(t0, [])
    current = _snapshot(t0 + timedelta(seconds=5), [_session("c1", title="Test")])

    events = list(watcher.diff(prev, current, state))
    started = [e for e in events if e.kind == "session_started"]
    assert len(started) == 1
    assert started[0].session_id == "c1"
    assert started[0].title == "Test"
    assert started[0].severity == "info"
    assert "c1" in state.seen_session_started

    # Second tick: still there, no re-fire.
    next_snap = _snapshot(t0 + timedelta(seconds=10), [_session("c1", title="Test")])
    events2 = list(watcher.diff(current, next_snap, state))
    assert not [e for e in events2 if e.kind == "session_started"]


# ---------------------------------------------------------------------------
# diff(): message_appended on bubble growth
# ---------------------------------------------------------------------------

def test_message_appended_fires_when_bubble_count_grows(tmp_path: Path) -> None:
    """We can't read real bubble bodies in this unit test (no DB),
    so this exercises the diff contract: when bubble_count grew,
    a message_appended event is emitted with the delta in the detail."""
    # The snapshot scan reads the DB to count user/asst/tool stats —
    # for a unit-only test that path goes through _scan_delta_bubbles.
    # We bypass the DB by writing a minimal real one.
    db = tmp_path / "state.vscdb"
    _build_minimal_db(db, composer_id="c2", bubble_count=3, with_tool=True)
    watcher = CursorWatcher(db_path=db)
    state = WatchState()
    t0 = datetime(2026, 4, 30, 10, 0, 0, tzinfo=timezone.utc)

    prev = _snapshot(t0, [_session("c2", bubble_count=0, title="Growing")])
    current = _snapshot(
        t0 + timedelta(seconds=5),
        [_session("c2", bubble_count=3, title="Growing")],
    )

    events = list(watcher.diff(prev, current, state))
    appended = [e for e in events if e.kind == "message_appended"]
    assert len(appended) == 1
    assert "+3 bubble" in (appended[0].detail or "")


def test_tool_call_event_fires_when_tool_bubble_added(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    _build_minimal_db(db, composer_id="c3", bubble_count=3, with_tool=True)
    watcher = CursorWatcher(db_path=db)
    state = WatchState()
    t0 = datetime(2026, 4, 30, 10, 0, 0, tzinfo=timezone.utc)

    prev = _snapshot(t0, [_session("c3", bubble_count=0)])
    current = _snapshot(t0 + timedelta(seconds=5), [_session("c3", bubble_count=3)])

    events = list(watcher.diff(prev, current, state))
    kinds = {e.kind for e in events}
    assert "tool_call" in kinds


# ---------------------------------------------------------------------------
# diff(): empty-composer warnings at 30 / 60 minutes
# ---------------------------------------------------------------------------

def test_session_warning_fires_after_30_min_empty(tmp_path: Path) -> None:
    """An empty composer that's older than EMPTY_WARNING_AFTER fires
    a session_warning. Fires exactly once via WatchState."""
    watcher = CursorWatcher(db_path=tmp_path / "nope.vscdb")
    state = WatchState()
    created = datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc)
    now = created + EMPTY_WARNING_AFTER + timedelta(seconds=5)

    prev = _snapshot(
        now - timedelta(seconds=5),
        [_session("c4", bubble_count=0, created_at=created)],
    )
    current = _snapshot(
        now,
        [_session("c4", bubble_count=0, created_at=created)],
    )

    # First tick after threshold: warning fires.
    events = list(watcher.diff(prev, current, state))
    warnings = [e for e in events if e.kind == "session_warning"]
    assert len(warnings) == 1
    assert warnings[0].severity == "warning"

    # Second tick: warning does NOT re-fire.
    later = now + timedelta(seconds=5)
    next_snap = _snapshot(
        later, [_session("c4", bubble_count=0, created_at=created)]
    )
    events2 = list(watcher.diff(current, next_snap, state))
    assert not [e for e in events2 if e.kind == "session_warning"]


def test_session_idle_fires_after_60_min_empty(tmp_path: Path) -> None:
    """At 60+ minutes the event upgrades to session_idle (severity
    error: probable ghost session)."""
    watcher = CursorWatcher(db_path=tmp_path / "nope.vscdb")
    state = WatchState()
    created = datetime(2026, 4, 30, 8, 0, 0, tzinfo=timezone.utc)
    # First fire the 30-min warning so its state is set.
    state.fired_30min_warning.add("c5")
    now = created + EMPTY_IDLE_AFTER + timedelta(seconds=5)

    prev = _snapshot(
        now - timedelta(seconds=5),
        [_session("c5", bubble_count=0, created_at=created)],
    )
    current = _snapshot(
        now, [_session("c5", bubble_count=0, created_at=created)]
    )

    events = list(watcher.diff(prev, current, state))
    idles = [e for e in events if e.kind == "session_idle"]
    assert len(idles) == 1
    assert idles[0].severity == "error"
    assert "ghost" in (idles[0].detail or "").lower()


def test_session_with_bubbles_does_not_warn(tmp_path: Path) -> None:
    """An old composer that has bubbles does NOT warn — even if it's
    been around forever."""
    watcher = CursorWatcher(db_path=tmp_path / "nope.vscdb")
    state = WatchState()
    created = datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 4, 30, 10, 0, 0, tzinfo=timezone.utc)
    prev = _snapshot(
        now - timedelta(seconds=5),
        [_session("c6", bubble_count=42, created_at=created)],
    )
    current = _snapshot(
        now, [_session("c6", bubble_count=42, created_at=created)]
    )
    events = list(watcher.diff(prev, current, state))
    assert not [
        e for e in events if e.kind in ("session_warning", "session_idle")
    ]


# ---------------------------------------------------------------------------
# snapshot() end-to-end against a real synthetic state.vscdb
# ---------------------------------------------------------------------------

def _build_minimal_db(
    db_path: Path, *, composer_id: str, bubble_count: int, with_tool: bool
) -> None:
    """Write a minimal cursorDiskKV table with a composer and N bubbles."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)"
        )
        composer = {
            "_v": 3,
            "composerId": composer_id,
            "name": "Synthetic",
            "fullConversationHeadersOnly": [
                {"bubbleId": f"b{i}"} for i in range(bubble_count)
            ],
            "conversation": [],
            "createdAt": int(
                datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
            ),
        }
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"composerData:{composer_id}", json.dumps(composer)),
        )
        for i in range(bubble_count):
            bubble = {
                "_v": 2,
                "bubbleId": f"b{i}",
                "type": 1 if i % 2 == 0 else 2,
                "text": f"line {i}",
            }
            if with_tool and i == bubble_count - 1:
                bubble["toolFormerData"] = {
                    "tool": 1,
                    "toolCallId": f"tool_{i}",
                    "name": "read_file_v2",
                    "rawArgs": "{}",
                    "status": "completed",
                }
            conn.execute(
                "INSERT INTO cursorDiskKV VALUES (?, ?)",
                (f"bubbleId:{composer_id}:b{i}", json.dumps(bubble)),
            )
        conn.commit()
    finally:
        conn.close()


def test_snapshot_reads_real_db(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    _build_minimal_db(db, composer_id="real-1", bubble_count=4, with_tool=True)
    snap = CursorWatcher(db_path=db).snapshot()

    assert "real-1" in snap.sessions
    s = snap.sessions["real-1"]
    assert s.bubble_count == 4
    assert s.title == "Synthetic"
    assert s.created_at is not None


def test_snapshot_returns_empty_when_db_missing(tmp_path: Path) -> None:
    snap = CursorWatcher(db_path=tmp_path / "no.vscdb").snapshot()
    assert snap.sessions == {}


def test_snapshot_skips_db_without_cursorDiskKV_table(tmp_path: Path) -> None:
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE other_table (k TEXT, v TEXT)")
        conn.commit()
    finally:
        conn.close()
    snap = CursorWatcher(db_path=db).snapshot()
    assert snap.sessions == {}


def test_snapshot_tolerates_null_composer_value(tmp_path: Path) -> None:
    """Real Cursor DBs occasionally have NULL value rows (zombie
    composers). The watcher must not crash on them."""
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, NULL)", ("composerData:zombie",)
        )
        conn.commit()
    finally:
        conn.close()
    snap = CursorWatcher(db_path=db).snapshot()
    # Zombie filtered at the SQL level (value IS NOT NULL).
    assert "zombie" not in snap.sessions
