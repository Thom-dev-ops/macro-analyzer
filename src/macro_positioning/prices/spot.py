"""Intraday spot-price fetcher for the "refresh for live data" button.

The batch `fetcher.py` writes daily bars from yfinance to the SQLite `prices`
table via the launchd scheduler. That leaves the dashboard as stale as the
last daily run — often yesterday's close, and multi-day-old on weekends.

`spot_price()` here fills that gap: on-demand, best-effort intraday quotes
that overlay the daily-bar snapshot without touching the `prices` table.
Providers tried in order:
  1. Finnhub `/quote` (real-time equities, if MPA_FINNHUB_API_KEY set)
  2. yfinance intraday (5m/15m bars — best-effort, ~15-min delayed)
  3. Fall back to the DB's latest daily close (never live, but never wrong)

The TTL guard on the top-level `refresh_snapshot()` prevents hammering
upstream if the user mashes the button.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import UTC, datetime
from typing import Iterable

import httpx

from macro_positioning.core.settings import settings
from macro_positioning.prices.fetcher import latest_close
from macro_positioning.prices.symbol_map import (
    is_crypto,
    to_yfinance_symbol,
)


logger = logging.getLogger(__name__)


FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
_FINNHUB_TIMEOUT = 4.0
_YF_TIMEOUT_HINT = 6.0  # yfinance doesn't take timeout kwarg cleanly; noted for humans

# Simple per-ticker TTL cache so button spam doesn't hammer upstream. Keyed by
# UPPER ticker. Values are (fetched_at_epoch, result_dict).
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL_SECONDS = 30.0


def _is_crypto(ticker: str) -> bool:
    # One definition, in symbol_map: key-aware, so a Coinbase listing whose
    # ticker is also a stock (AI, SKY) does not stop Finnhub quoting the
    # stock. See symbol_map.is_crypto.
    return is_crypto(ticker)


def _from_finnhub(ticker: str) -> dict | None:
    """Real-time equity quote via Finnhub `/quote`. Returns None on any failure
    (missing key, non-equity, network error, empty payload). Free tier: 60/min."""
    key = settings.finnhub_api_key
    if not key or _is_crypto(ticker):
        return None
    try:
        with httpx.Client(timeout=_FINNHUB_TIMEOUT) as c:
            r = c.get(FINNHUB_QUOTE_URL, params={"symbol": ticker.upper(), "token": key})
            r.raise_for_status()
            j = r.json() or {}
    except Exception as exc:
        logger.debug("finnhub /quote(%s) failed: %s", ticker, exc)
        return None
    price = j.get("c")
    prev = j.get("pc")
    if not price or price == 0:  # Finnhub returns c=0 for unknown symbols
        return None
    chg_pct = None
    if prev:
        try:
            chg_pct = round((float(price) - float(prev)) / float(prev) * 100, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            chg_pct = None
    ts = j.get("t")
    as_of = (
        datetime.fromtimestamp(int(ts), tz=UTC).isoformat()
        if ts else datetime.now(UTC).isoformat()
    )
    return {
        "price": float(price),
        "prev_close": float(prev) if prev else None,
        "chg_pct_1d": chg_pct,
        "source": "finnhub",
        "as_of": as_of,
    }


def _from_yfinance_intraday(ticker: str) -> dict | None:
    """Latest intraday bar via yfinance. ~15-min delayed for equities,
    real-ish-time for crypto. Returns None on failure."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    symbol = to_yfinance_symbol(ticker)
    interval = "15m" if _is_crypto(ticker) else "5m"
    try:
        hist = yf.Ticker(symbol).history(period="2d", interval=interval, auto_adjust=False, actions=False)
    except Exception as exc:
        logger.debug("yfinance intraday(%s) failed: %s", symbol, exc)
        return None
    if hist is None or hist.empty:
        return None
    try:
        last = hist.iloc[-1]
        price = float(last["Close"])
    except (KeyError, IndexError, ValueError, TypeError):
        return None
    if not price or price != price:  # NaN
        return None
    # Prev-day close = first bar's open on a two-day pull (rough — yfinance
    # doesn't cleanly expose prior daily close for intraday bars). For a
    # crisper 1d %, fall back to the daily bar in the DB.
    prev = None
    chg_pct = None
    db_prev = latest_close(ticker)
    if db_prev and db_prev > 0:
        prev = float(db_prev)
        chg_pct = round((price - prev) / prev * 100, 2)
    as_of = hist.index[-1].isoformat() if len(hist.index) else datetime.now(UTC).isoformat()
    return {
        "price": price,
        "prev_close": prev,
        "chg_pct_1d": chg_pct,
        "source": f"yfinance-{interval}",
        "as_of": as_of,
    }


def _from_db(ticker: str) -> dict | None:
    """Last resort: yesterday's close from the `prices` table. Marked
    source='db-stale' so the UI can badge it as not-really-live."""
    price = latest_close(ticker)
    if price is None:
        return None
    # No intraday change info from a stale bar.
    return {
        "price": float(price),
        "prev_close": None,
        "chg_pct_1d": None,
        "source": "db-stale",
        "as_of": None,
    }


def spot_price(ticker: str, *, use_cache: bool = True) -> dict | None:
    """Freshest available price for a ticker.

    Returns `{price, prev_close, chg_pct_1d, source, as_of}` or None if
    every provider (Finnhub → yfinance intraday → DB) failed.
    """
    t = (ticker or "").upper().strip()
    if not t:
        return None

    now = time.time()
    if use_cache:
        hit = _CACHE.get(t)
        if hit and (now - hit[0]) < _TTL_SECONDS:
            return hit[1]

    for probe in (_from_finnhub, _from_yfinance_intraday, _from_db):
        try:
            result = probe(t)
        except Exception as exc:
            logger.debug("spot probe %s(%s) errored: %s", probe.__name__, t, exc)
            continue
        if result:
            _CACHE[t] = (now, result)
            return result
    return None


def spot_prices(tickers: Iterable[str], *, use_cache: bool = True) -> dict[str, dict]:
    """Batch wrapper. Serial today (yfinance/Finnhub free tiers don't reward
    parallelism much and the tape is ~15 tickers)."""
    out: dict[str, dict] = {}
    for t in tickers:
        q = spot_price(t, use_cache=use_cache)
        if q is not None:
            out[t.upper()] = q
    return out


def clear_cache() -> None:
    """Test hook — drop the TTL cache."""
    _CACHE.clear()


def traded_ranges(
    tickers: Iterable[str], *, since: str, db_path=None
) -> dict[str, dict]:
    """High/low each ticker actually traded through since `since`.

    A twice-daily mark answers "where is it now"; a resting stop needs
    "where has it BEEN". The paper engine's default stop rule compares the
    stop to the mark, so a level the tape pierced intraday and closed back
    above is never seen — ETH traded through 2530 on Sep 4 2026 and did
    not close above it until Sep 18, a 14-day, 3R difference in where a
    stop-out landed. Books that honour an author's stop as a resting order
    read the range instead.

    `since` is an ISO timestamp — the position's last mark. Bars are daily,
    so only bars STRICTLY AFTER that date count: the same day's high may
    have printed before the last tick, and re-reading it would fire a stop
    on a move the book already saw and held through.

    Returns `{TICKER: {"high": float, "low": float, "bars": int,
    "from": str, "to": str}}`, omitting tickers with no bars in the window.
    """
    wanted = [str(t or "").upper().strip() for t in tickers]
    wanted = [t for t in wanted if t]
    if not wanted or not since:
        return {}
    since_day = str(since)[:10]

    out: dict[str, dict] = {}
    path = str(db_path or settings.sqlite_path)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30) as conn:
            marks = ",".join("?" * len(wanted))
            rows = conn.execute(
                f"""
                SELECT ticker,
                       MAX(high) AS hi, MIN(low) AS lo, COUNT(*) AS n,
                       MIN(observed_at) AS first_at, MAX(observed_at) AS last_at
                  FROM prices
                 WHERE ticker IN ({marks})
                   AND timeframe = '1D'
                   AND observed_at > ?
                   AND high IS NOT NULL AND low IS NOT NULL
                 GROUP BY ticker
                """,
                (*wanted, since_day),
            ).fetchall()
    except sqlite3.Error:
        logger.exception("traded_ranges failed for %s since %s", wanted, since_day)
        return {}

    for ticker, hi, lo, n, first_at, last_at in rows:
        if hi is None or lo is None:
            continue
        out[str(ticker).upper()] = {
            "high": float(hi), "low": float(lo), "bars": int(n),
            "from": first_at, "to": last_at,
        }
    return out
