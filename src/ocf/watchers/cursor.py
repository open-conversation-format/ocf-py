"""Cursor watcher: poll the live state.vscdb and emit deltas.

The DB is read-only (``mode=ro`` URI), same as the exporter. SQLite
won't deadlock against the running Cursor IDE — multiple readers
coexist with one writer in WAL mode.

Snapshot strategy:

- One ``SELECT key, value FROM cursorDiskKV`` for ``composerData:%``
  to enumerate all composers and capture title + createdAt.
- One ``SELECT COUNT(*)`` per composer for bubble_count — cheap, the
  table is indexed on ``key``. Avoids loading huge bubble payloads.
- Token / tool-call rollup is *deferred*: those need per-bubble JSON
  parsing, which we only do when the bubble_count actually grew
  (i.e. when there's something new). Keeps a steady-state poll under
  ~100 ms even with hundreds of composers.

Diff rules (from the project owner's requirements):

- New composer appears -> ``session_started`` (info).
- bubble_count grows -> ``message_appended`` per delta, plus
  ``tool_call`` events for any new tool_use bubbles in the delta.
- A composer with bubble_count == 0 and createdAt > 30 min ago
  -> ``session_warning`` (fired once via :class:`WatchState`).
- Same after 60 min -> ``session_idle`` (probable ghost session,
  fired once via :class:`WatchState`).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator

from ocf.utils.paths import cursor_user_dir
from ocf.utils.sqlite_ro import has_table, open_ro
from ocf.watchers._base import (
    SessionState,
    WatchEvent,
    WatchSnapshot,
    WatchState,
)


COMPOSER_KEY_PREFIX = "composerData:"
BUBBLE_KEY_PREFIX = "bubbleId:"

EMPTY_WARNING_AFTER = timedelta(minutes=30)
EMPTY_IDLE_AFTER = timedelta(minutes=60)


class CursorWatcher:
    """Poll a Cursor state.vscdb and emit watch events.

    Construct with the path to ``state.vscdb`` (or omit to use the
    default user directory). Call :meth:`snapshot` repeatedly and feed
    consecutive snapshots through :meth:`diff` to get events.
    """

    name = "cursor"

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            db_path = cursor_user_dir() / "globalStorage" / "state.vscdb"
        self.db_path = Path(db_path)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> WatchSnapshot:
        """One read of the DB. Returns a snapshot suitable for diffing.

        Cheap path: list composers + count bubbles. Per-composer
        bubble JSON is only parsed for composers that the caller will
        also pass to :meth:`diff`, which knows it grew.
        """
        if not self.db_path.exists():
            return WatchSnapshot(taken_at=datetime.now(tz=timezone.utc), sessions={})

        sessions: dict[str, SessionState] = {}
        with open_ro(self.db_path) as conn:
            if not has_table(conn, "cursorDiskKV"):
                return WatchSnapshot(
                    taken_at=datetime.now(tz=timezone.utc), sessions={}
                )

            # Enumerate composers + read minimal metadata.
            for row in conn.execute(
                "SELECT key, value FROM cursorDiskKV "
                "WHERE key LIKE ? AND value IS NOT NULL",
                (COMPOSER_KEY_PREFIX + "%",),
            ):
                composer_id = row["key"][len(COMPOSER_KEY_PREFIX):]
                try:
                    cdata = json.loads(row["value"])
                except (json.JSONDecodeError, TypeError):
                    continue

                title = (
                    cdata.get("name")
                    or cdata.get("title")
                    or (cdata.get("latestConversationSummary") or {}).get("title")
                )
                created_at = _parse_created_at(cdata.get("createdAt"))

                # Bubble count via COUNT(*). bubble bodies are read on
                # demand by the diff function, not here.
                count_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM cursorDiskKV "
                    "WHERE key LIKE ? AND value IS NOT NULL",
                    (f"{BUBBLE_KEY_PREFIX}{composer_id}:%",),
                ).fetchone()
                bubble_count = int(count_row["n"]) if count_row else 0

                sessions[composer_id] = SessionState(
                    session_id=composer_id,
                    title=title if isinstance(title, str) else None,
                    created_at=created_at,
                    bubble_count=bubble_count,
                    # Aggregated stats are only useful when bubble_count
                    # changed; we carry zeros at snapshot time and let
                    # diff() roll them up from the delta bubbles only.
                    user_messages=0,
                    assistant_messages=0,
                    tool_calls=0,
                    tokens_in=None,
                    tokens_out=None,
                    extra={},
                )

        return WatchSnapshot(
            taken_at=datetime.now(tz=timezone.utc),
            sessions=sessions,
        )

    # ------------------------------------------------------------------
    # Diff
    # ------------------------------------------------------------------

    def diff(
        self,
        prev: WatchSnapshot,
        current: WatchSnapshot,
        state: WatchState,
    ) -> Iterator[WatchEvent]:
        """Yield events for the deltas between two snapshots.

        ``state`` is mutated to remember which warnings fired so each
        threshold alarms exactly once per session.
        """
        for sid, curr_session in current.sessions.items():
            prev_session = prev.sessions.get(sid)

            # --- session_started ---------------------------------------
            if prev_session is None and sid not in state.seen_session_started:
                state.seen_session_started.add(sid)
                yield WatchEvent(
                    timestamp=current.taken_at,
                    adapter=self.name,
                    session_id=sid,
                    kind="session_started",
                    severity="info",
                    title=curr_session.title,
                    detail=(
                        "new composer (empty)"
                        if curr_session.bubble_count == 0
                        else f"new composer with {curr_session.bubble_count} bubbles"
                    ),
                )

            # --- message_appended + tool_call --------------------------
            prev_count = prev_session.bubble_count if prev_session else 0
            if curr_session.bubble_count > prev_count:
                # Re-scan all bubbles for current totals; compute delta
                # against the totals we stored on the previous append
                # (so the event detail is a true delta, not raw totals).
                u_total, a_total, t_total, ti_total, to_total = (
                    self._scan_delta_bubbles(sid, prev_count)
                )
                last = state.last_totals.get(sid, {})
                prev_u = last.get("user") or 0
                prev_a = last.get("assistant") or 0
                prev_t = last.get("tools") or 0
                prev_ti = last.get("tokens_in") or 0
                prev_to = last.get("tokens_out") or 0
                delta_user = u_total - prev_u
                delta_asst = a_total - prev_a
                delta_tools = t_total - prev_t
                delta_ti = (ti_total or 0) - prev_ti
                delta_to = (to_total or 0) - prev_to
                state.last_totals[sid] = {
                    "user": u_total,
                    "assistant": a_total,
                    "tools": t_total,
                    "tokens_in": ti_total,
                    "tokens_out": to_total,
                }
                yield WatchEvent(
                    timestamp=current.taken_at,
                    adapter=self.name,
                    session_id=sid,
                    kind="message_appended",
                    severity="info",
                    title=curr_session.title,
                    detail=(
                        f"+{curr_session.bubble_count - prev_count} bubble(s); "
                        f"u+{delta_user} a+{delta_asst} tools+{delta_tools}"
                    ),
                    user_messages=delta_user,
                    assistant_messages=delta_asst,
                    tool_calls=delta_tools,
                    tokens_in=delta_ti if delta_ti else None,
                    tokens_out=delta_to if delta_to else None,
                )
                if delta_tools:
                    yield WatchEvent(
                        timestamp=current.taken_at,
                        adapter=self.name,
                        session_id=sid,
                        kind="tool_call",
                        severity="info",
                        title=curr_session.title,
                        detail=f"+{delta_tools} tool call(s)",
                        tool_calls=delta_tools,
                    )

            # --- empty-composer warnings ------------------------------
            if curr_session.bubble_count == 0 and curr_session.created_at:
                age = current.taken_at - curr_session.created_at
                if (
                    age >= EMPTY_IDLE_AFTER
                    and sid not in state.fired_60min_warning
                ):
                    state.fired_60min_warning.add(sid)
                    yield WatchEvent(
                        timestamp=current.taken_at,
                        adapter=self.name,
                        session_id=sid,
                        kind="session_idle",
                        severity="error",
                        title=curr_session.title,
                        detail=(
                            f"composer empty after {int(age.total_seconds()/60)} min "
                            "- probable ghost session"
                        ),
                    )
                elif (
                    age >= EMPTY_WARNING_AFTER
                    and sid not in state.fired_30min_warning
                ):
                    state.fired_30min_warning.add(sid)
                    yield WatchEvent(
                        timestamp=current.taken_at,
                        adapter=self.name,
                        session_id=sid,
                        kind="session_warning",
                        severity="warning",
                        title=curr_session.title,
                        detail=(
                            f"composer still empty after "
                            f"{int(age.total_seconds()/60)} min"
                        ),
                    )

    # ------------------------------------------------------------------
    # Internal: scan only the new bubbles for stats
    # ------------------------------------------------------------------

    def _scan_delta_bubbles(
        self, composer_id: str, prev_count: int
    ) -> tuple[int, int, int, int | None, int | None]:
        """Walk the bubbles for one composer and roll up stats.

        We can't cheaply scan only "new" bubbles because Cursor's
        bubble keys aren't ordered by insertion time; the cheapest
        thing is to recompute the totals. The fix-up at the call site
        ensures we only call this when bubble_count actually grew, so
        per-tick cost is bounded.

        Returns ``(user_msgs, assistant_msgs, tool_calls, tokens_in, tokens_out)``
        as totals across all current bubbles of this composer.
        """
        user_msgs = 0
        asst_msgs = 0
        tool_calls = 0
        tokens_in: int = 0
        tokens_out: int = 0
        any_tokens_seen = False

        with open_ro(self.db_path) as conn:
            for row in conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key LIKE ?",
                (f"{BUBBLE_KEY_PREFIX}{composer_id}:%",),
            ):
                if row["value"] is None:
                    continue
                try:
                    b = json.loads(row["value"])
                except (json.JSONDecodeError, TypeError):
                    continue
                btype = b.get("type")
                if btype == 1:
                    user_msgs += 1
                elif btype == 2:
                    asst_msgs += 1
                if isinstance(b.get("toolFormerData"), dict):
                    tool_calls += 1
                tc = b.get("tokenCount") if isinstance(b.get("tokenCount"), dict) else None
                if tc:
                    if isinstance(tc.get("inputTokens"), int):
                        tokens_in += tc["inputTokens"]
                        any_tokens_seen = True
                    if isinstance(tc.get("outputTokens"), int):
                        tokens_out += tc["outputTokens"]
                        any_tokens_seen = True

        # Only return totals — caller computes the delta. We could also
        # subtract prev_count's contribution, but that requires storing
        # the previous totals; current approach keeps state minimal at
        # the cost of a per-bubble scan when something changed.
        return (
            user_msgs,
            asst_msgs,
            tool_calls,
            tokens_in if any_tokens_seen else None,
            tokens_out if any_tokens_seen else None,
        )


def _parse_created_at(value: object) -> datetime | None:
    """Cursor stores createdAt as ms since epoch. Be permissive about
    other shapes that occasionally show up (string ISO)."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


__all__ = [
    "CursorWatcher",
    "EMPTY_WARNING_AFTER",
    "EMPTY_IDLE_AFTER",
]
