"""Price fetcher — batch fetch tickers via a Provider, persist to `prices`.

Idempotent: re-running for the same day overwrites the bar (INSERT OR
REPLACE on the unique index). Safe to schedule daily after market close
without dedupe logic in the scheduler.

CLI: `macro-positioning prices fetch [--watchlist] [--ticker T] [--days N]`
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Iterable

from pydantic import BaseModel, Field

from macro_positioning.core.settings import settings
from macro_positioning.db.connect import write_connection
from macro_positioning.db.schema import initialize_database
from macro_positioning.prices.provider import PriceBar, PriceProvider, default_provider


logger = logging.getLogger(__name__)


class PriceFetchResult(BaseModel):
    started_at: str
    finished_at: str
    provider: str
    tickers_requested: int
    tickers_with_data: int
    bars_persisted: int
    failures: list[dict] = Field(default_factory=list)


def fetch_and_persist(
    tickers: Iterable[str],
    *,
    days: int = 200,
    timeframe: str = "1D",
    provider: PriceProvider | None = None,
) -> PriceFetchResult:
    """Fetch and write daily OHLCV for each ticker.

    Args:
      tickers: list of bare tickers (URA, BTC, DXY) — symbol map handles
        provider-specific translation.
      days: history depth. 200 covers 200DMA computation comfortably.
      timeframe: '1D' for daily (only supported value today).
      provider: explicit provider override. None uses default (yfinance).
    """
    p = provider or default_provider()
    started = datetime.now(UTC)
    tickers_list = [t.upper().strip() for t in tickers if t and t.strip()]

    initialize_database(settings.sqlite_path)
    failures: list[dict] = []
    bars_persisted = 0
    tickers_with_data = 0

    # Fetch everything FIRST, then write in one short transaction.
    #
    # This used to be a single `with sqlite3.connect(...)` wrapped around
    # the whole loop, so the write transaction opened on the first ticker's
    # bars and stayed open across every remaining network call — roughly a
    # hundred yfinance round-trips. The hourly alert-watch job therefore
    # held the SQLite writer for 40+ seconds at a stretch, and everything
    # else that wanted to write during that window failed with `database is
    # locked`: the paper tick, the API's boot-time schema init, and every
    # attempt to mark or promote a concept from the SPA, which surfaced to
    # the user as a 500 from a button that should always work.
    #
    # ~100 tickers x 200 bars is a few thousand rows in memory. That is a
    # trivial price for a lock held in milliseconds instead of minutes.
    fetched: list[tuple[str, list]] = []
    for ticker in tickers_list:
        try:
            bars = p.fetch_history(ticker, days=days, timeframe=timeframe)
        except Exception as exc:
            failures.append({"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not bars:
            failures.append({"ticker": ticker, "error": "no data returned"})
            continue
        tickers_with_data += 1
        fetched.append((ticker, bars))

    # Network is done; now take the writer.
    with write_connection() as conn:
        for ticker, bars in fetched:
            try:
                _persist_bars(conn, bars)
                bars_persisted += len(bars)
            except Exception as exc:
                failures.append({"ticker": ticker, "error": f"persist failed: {exc}"})
        conn.commit()

    finished = datetime.now(UTC)
    return PriceFetchResult(
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        provider=p.name,
        tickers_requested=len(tickers_list),
        tickers_with_data=tickers_with_data,
        bars_persisted=bars_persisted,
        failures=failures,
    )


def _persist_bars(conn: sqlite3.Connection, bars: list[PriceBar]) -> None:
    """INSERT OR REPLACE one row per bar. Unique index on
    (ticker, observed_at, timeframe) handles idempotency."""
    fetched_at = datetime.now(UTC).isoformat()
    rows = [
        (
            f"price-{b.ticker}-{b.observed_at}-{b.timeframe}",
            b.ticker,
            b.observed_at,
            b.timeframe,
            b.open,
            b.high,
            b.low,
            b.close,
            b.volume,
            b.provider,
            fetched_at,
        )
        for b in bars
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO prices (
            price_id, ticker, observed_at, timeframe,
            open, high, low, close, volume, provider, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


# ---------------------------------------------------------------------------
# Read helpers — used by scoring runner + technicals
# ---------------------------------------------------------------------------

def load_recent_prices(
    ticker: str,
    *,
    days: int = 200,
    timeframe: str = "1D",
    conn: sqlite3.Connection | None = None,
) -> list[PriceBar]:
    """Load most recent `days` bars for a ticker from the DB.

    `conn`: optional caller-supplied connection. Pass this when calling
    inside a transaction to avoid `initialize_database` re-running DDL
    while you hold an open transaction (causes deadlock).
    """
    own_conn = conn is None
    if own_conn:
        # Don't init the DB here — assume caller already ensured the DB
        # exists. Bare reads don't need the schema bootstrap and running
        # it (with PRAGMAs + dedupe) inside someone else's transaction
        # blocks. Fail loudly if the file is missing.
        conn = sqlite3.connect(settings.sqlite_path)
    try:
        cur = conn.execute(
            """
            SELECT ticker, observed_at, timeframe, open, high, low, close, volume, provider
            FROM prices
            WHERE ticker = ? AND timeframe = ?
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (ticker.upper(), timeframe, days),
        )
        rows = cur.fetchall()
    finally:
        if own_conn:
            conn.close()
    rows.reverse()
    return [
        PriceBar(
            ticker=r[0], observed_at=r[1], timeframe=r[2],
            open=r[3], high=r[4], low=r[5], close=r[6], volume=r[7], provider=r[8],
        )
        for r in rows
    ]


def latest_close(ticker: str, *, conn: sqlite3.Connection | None = None) -> float | None:
    """Most recent close for a ticker, or None if no data.

    `conn`: optional connection (same rationale as load_recent_prices).
    """
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(settings.sqlite_path)
    try:
        cur = conn.execute(
            "SELECT close FROM prices WHERE ticker = ? ORDER BY observed_at DESC LIMIT 1",
            (ticker.upper(),),
        )
        row = cur.fetchone()
    finally:
        if own_conn:
            conn.close()
    return float(row[0]) if row else None
