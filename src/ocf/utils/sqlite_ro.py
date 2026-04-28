"""Safe read-only SQLite access for Cursor's state.vscdb.

Cursor uses SQLite in WAL mode, which permits concurrent readers
alongside one writer. Our adapter opens with ``mode=ro`` (URI form)
so we never touch the DB writeable. If a future Cursor version
holds an exclusive lock, this module will be the place to add a
snapshot-via-tempfile fallback.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def open_ro(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open ``db_path`` read-only via SQLite URI mode.

    Cursor's main globalStorage DB can be 3+ GB. Opening read-only
    avoids holding a writer lock and never modifies the file on disk.
    """
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    )
    return cur.fetchone() is not None


__all__ = ["open_ro", "has_table"]
