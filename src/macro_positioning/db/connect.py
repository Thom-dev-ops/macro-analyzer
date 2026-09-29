"""One place that knows how long to wait for the SQLite writer.

This database has several concurrent writers — two API servers, the
Telegram listener, the free-ingest job, the alert watcher and the paper
tick. WAL keeps readers working throughout, but writes serialise, and
`alert_watch.py` holds its write transaction across a long price pass
every hour.

Each API module used to open its own connection with either no
`busy_timeout` or a 5-second one, which is shorter than that pass. The
symptom was not a slow app: it was `database is locked` bubbling up as a
500 from whichever button the user happened to press, so the funnel
looked broken for minutes at a time every hour, at random.

Thirty seconds is not a latency budget — it is the admission that a
human clicking "promote" would rather wait than lose the write.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from macro_positioning.core.settings import settings


# Long enough to outlast the alert-watch price pass, which is the longest
# write transaction anything in this project takes.
WRITE_TIMEOUT_MS = 30_000
READ_TIMEOUT_MS = 10_000


def write_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """A read/write connection that waits for the writer instead of failing.

    Callers own the connection (commit/close) — this only standardises how
    it is opened.
    """
    path = db_path or settings.sqlite_path
    conn = sqlite3.connect(path, timeout=WRITE_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={WRITE_TIMEOUT_MS}")
    return conn


def read_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """A read-only connection. Cannot block a writer, and won't be blocked
    by one under WAL — the timeout only covers checkpoint contention."""
    path = db_path or settings.sqlite_path
    conn = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, timeout=READ_TIMEOUT_MS / 1000
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={READ_TIMEOUT_MS}")
    return conn


__all__ = ["write_connection", "read_connection", "WRITE_TIMEOUT_MS", "READ_TIMEOUT_MS"]
