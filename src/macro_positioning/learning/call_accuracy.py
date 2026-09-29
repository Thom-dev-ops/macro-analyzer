"""Per-call accuracy backtester for trade-call sources.

Scores each chart-vision call (ticker + direction + entry/stop/targets +
timestamp) against subsequent real price action, then rolls the outcomes up
per author/channel. This is what makes conviction trustworthy: a channel
whose calls actually play out should weigh more than one that doesn't.

Two outcome measures per call:

  • DIRECTIONAL (always, when priceable): did price move the called
    direction over a timeframe-appropriate horizon? Signed forward return
    from the entry fill (close at-or-after the call) to the horizon close.

  • SETUP-RESOLUTION (when entry+stop+target extracted): walking daily
    high/low bars forward from the call, did price touch the first
    take-profit before the stop-loss? Yields win/loss/open + an R-multiple.

Scope (see prices/symbol_map.resolve_symbol):
  • Crypto — only the 16 Coinbase-US tracked coins; others `unpriceable`.
  • Equities — dynamic, any symbol yfinance can price.

Outcomes persist to `call_outcomes` (idempotent on document_id). The
per-source rollup (`source_accuracy`) feeds the S6 streams cards. v1 is
display-only — it does NOT auto-adjust trust_weight (that's signal_calibration,
deferred).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

from macro_positioning.core.settings import settings
from macro_positioning.learning.source_attribution import _parse_iso
from macro_positioning.learning.source_themes import _normalize_ticker
from macro_positioning.prices.symbol_map import resolve_symbol


logger = logging.getLogger(__name__)


# ── Timeframe → evaluation horizon (calendar days) ──────────────────────────
# A 15m/1h/4h scalp resolves in days; a daily swing in weeks; a weekly in
# months. The horizon bounds how far forward we look for the directional
# return and the setup resolution.
_HORIZON_BY_TF: dict[str, int] = {
    "1M": 2, "1MIN": 2, "5M": 2, "15M": 3, "30M": 3,
    "1H": 5, "2H": 5, "4H": 7,
    "1D": 20, "D": 20, "1DAY": 20,
    "3D": 30, "1W": 60, "W": 60, "1WK": 60, "1MO": 120,
}
_DEFAULT_HORIZON_DAYS = 20


def _horizon_days(timeframe: Optional[str]) -> int:
    if not timeframe:
        return _DEFAULT_HORIZON_DAYS
    return _HORIZON_BY_TF.get(str(timeframe).strip().upper(), _DEFAULT_HORIZON_DAYS)


_TF_BANDS: dict[str, str] = {
    "1M": "intraday", "1MIN": "intraday", "5M": "intraday",
    "15M": "intraday", "30M": "intraday", "1H": "intraday",
    "2H": "swing", "4H": "swing", "8H": "swing",
    "1D": "swing", "D": "swing", "1DAY": "swing",
    "3D": "position", "1W": "position", "W": "position",
    "1WK": "position", "1MO": "position",
}


def _tf_band(timeframe: Optional[str]) -> Optional[str]:
    """Which business is this call in?

    An author is not one edge. The same person posting 15m scalps and 1W
    Elliott counts is running two strategies with different hold times,
    different stop widths and — measured — an 8x difference in expectancy.
    Banding lets a weight be learned per (author, band) instead of per
    author, which is the difference between a usable prior and an average.
    """
    if not timeframe:
        return None
    return _TF_BANDS.get(str(timeframe).strip().upper())


def _stop_width(entry, stop) -> Optional[float]:
    """|entry - stop| / entry. None when either leg is missing."""
    try:
        e, st = float(entry), float(stop)
    except (TypeError, ValueError):
        return None
    if e <= 0 or st <= 0:
        return None
    return round(abs(e - st) / e, 6)


def _num(v) -> Optional[float]:
    """Coerce a price-ish value to float, tolerating strings like '0.2154'."""
    if v is None:
        return None
    try:
        f = float(str(v).replace(",", "").replace("$", "").strip())
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None


def _direction_from(value: Optional[str], bias: Optional[str]) -> Optional[str]:
    """Resolve 'long'/'short' from an explicit direction or the bias."""
    for src in (value, bias):
        s = str(src or "").lower()
        if any(w in s for w in ("long", "buy", "bull")):
            return "long"
        if any(w in s for w in ("short", "sell", "bear")):
            return "short"
    return None


def _extract_call(features_json: Optional[str]) -> Optional[dict]:
    """Parse one structured call from extracted_features_json.

    Handles the three shapes Claude returns: flat TradeRecord, `setups[]`,
    and `entries[]` (the live framework shape). Returns the PRIMARY setup
    only (entries[0]/setups[0]) for v1 — multi-target calls collapse to
    their first take-profit. Returns None for error/placeholder rows.
    """
    if not features_json:
        return None
    try:
        f = json.loads(features_json)
    except json.JSONDecodeError:
        return None
    if isinstance(f, list):
        f = f[0] if f else {}
    if not isinstance(f, dict) or "error" in f:
        return None

    # NEW SCHEMA: only score ACTIONABLE directional calls. Exclude
    # no_trade / not_a_chart (no setup), retrospective (already played out),
    # and bidirectional (no committed direction — "watching both ways").
    call_type = f.get("call_type")
    if call_type and call_type not in ("directional_long", "directional_short"):
        return None
    trade_stage = f.get("trade_stage")

    raw_ticker = f.get("ticker") or f.get("asset") or f.get("instrument")
    if not raw_ticker:
        return None
    timeframe = f.get("timeframe")
    bias = f.get("bias")

    # Pull the primary setup from whichever shape is present.
    setup = {}
    for key in ("entries", "setups"):
        arr = f.get(key)
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            setup = arr[0]
            break

    entry = _num(setup.get("entry") or setup.get("entry_price") or f.get("entry_price"))
    stop = _num(setup.get("stop_loss") or setup.get("stop") or f.get("stop_loss")
                or setup.get("invalidation") or f.get("invalidation_level"))
    tps = setup.get("take_profits") or setup.get("targets") or f.get("take_profits")
    target = None
    if isinstance(tps, list) and tps:
        target = _num(tps[0])
    elif tps is not None:
        target = _num(tps)
    # Direction priority: call_type (most authoritative) > setup > bias.
    direction = ("long" if call_type == "directional_long"
                 else "short" if call_type == "directional_short"
                 else _direction_from(setup.get("direction") or f.get("direction"), bias))

    return {
        "raw_ticker": str(raw_ticker),
        "ticker": _normalize_ticker(raw_ticker),
        "symbol": resolve_symbol(str(raw_ticker)),  # None = unpriceable
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "target": target,
        "timeframe": timeframe,
        "call_type": call_type,
        "trade_stage": trade_stage,
    }


# ── Price bars (with high/low for setup resolution) ─────────────────────────

def _load_bars(
    conn: sqlite3.Connection, symbols: set[str]
) -> dict[str, list[tuple[datetime, float, float, float]]]:
    """{symbol: [(dt, high, low, close), ...]} sorted ascending. The prices
    table is keyed by the yfinance symbol we persist under."""
    if not symbols:
        return {}
    ph = ",".join("?" * len(symbols))
    cur = conn.execute(
        f"""
        SELECT ticker, observed_at, high, low, close
        FROM prices
        WHERE ticker IN ({ph}) AND timeframe='1D' AND close IS NOT NULL
        ORDER BY ticker, observed_at ASC
        """,
        tuple(symbols),
    )
    out: dict[str, list[tuple[datetime, float, float, float]]] = defaultdict(list)
    for sym, observed_at, high, low, close in cur.fetchall():
        dt = _parse_iso(observed_at)
        if dt is None:
            continue
        c = float(close)
        out[sym].append((dt, float(high) if high else c, float(low) if low else c, c))
    return out


def _signed_return_at(bars, dt: datetime, horizon: int, direction: str):
    """Directional forward return for a series: fill at-or-after dt, exit at
    horizon (or last available), signed by direction. Returns float or None."""
    fill = next((c for bdt, _h, _l, c in bars if bdt >= dt), None)
    if not fill:
        return None
    target_dt = dt + timedelta(days=horizon)
    exit_close = next((c for bdt, _h, _l, c in bars if bdt >= target_dt), None)
    if exit_close is None and bars:
        exit_close = bars[-1][3]
    if exit_close is None:
        return None
    raw = (exit_close / fill) - 1.0
    return raw if direction == "long" else -raw


# A planned reward:risk above this is almost always a mis-extracted level
# (stop/target read off the chart wrong). Winsorize so one bad row can't blow
# up a source's avg R (we saw R=15 from a misread).
_MAX_SANE_R = 8.0


def _score_one(
    call: dict, bars: list[tuple[datetime, float, float, float]], dt: datetime,
    btc_bars=None,          # the BENCHMARK's bars — BTC or SPY, chosen by caller
) -> dict:
    """Compute directional + setup-resolution outcome + market-relative alpha."""
    horizon = _horizon_days(call["timeframe"])
    direction = call["direction"] or "long"  # default long if unstated

    # Entry fill = first close at-or-after the call timestamp.
    fill = None
    for bdt, _h, _l, c in bars:
        if bdt >= dt:
            fill = c
            fill_dt = bdt
            break
    result = {
        "horizon_days": horizon,
        "direction": direction,
        "fwd_return_pct": None,
        "alpha_pct": None,
        "resolved": "open",
        "r_multiple": None,
    }
    if fill is None:
        result["resolved"] = "open"  # no price yet after the call
        return result

    # ── Directional forward return + market-relative alpha ──
    signed = _signed_return_at(bars, dt, horizon, direction)
    if signed is not None:
        result["fwd_return_pct"] = round(signed * 100, 3)
        # Alpha = call's directional return − BTC's return in the SAME
        # direction/window. Strips market beta (crypto trend) so we measure
        # source skill, not "crypto went up/down". BTC-vs-BTC ≈ 0 (fine).
        if btc_bars:
            btc_signed = _signed_return_at(btc_bars, dt, horizon, direction)
            if btc_signed is not None:
                result["alpha_pct"] = round((signed - btc_signed) * 100, 3)

    # ── Setup resolution (needs explicit entry/stop/target) ──
    entry = call["entry"] or fill
    stop = call["stop"]
    target = call["target"]
    if entry and stop and target:
        # R-multiple of the setup as designed (winsorized — see _MAX_SANE_R).
        risk = abs(entry - stop)
        reward = abs(target - entry)
        r = (reward / risk) if risk else None
        result["r_multiple"] = round(r, 2) if (r is not None and r <= _MAX_SANE_R) else None
        # Walk bars within the horizon window; first touch wins.
        end_dt = fill_dt + timedelta(days=horizon)
        resolved = "open"
        for bdt, hi, lo, _c in bars:
            if bdt < fill_dt:
                continue
            if bdt > end_dt:
                break
            if direction == "long":
                hit_stop = lo <= stop
                hit_tgt = hi >= target
            else:
                hit_stop = hi >= stop
                hit_tgt = lo <= target
            if hit_tgt and hit_stop:
                # Both in same daily bar — ambiguous; count conservative loss.
                resolved = "loss"
                break
            if hit_tgt:
                resolved = "win"
                break
            if hit_stop:
                resolved = "loss"
                break
        result["resolved"] = resolved
    else:
        # No levels → fall back to the directional sign as win/loss.
        if result["fwd_return_pct"] is not None:
            result["resolved"] = "win" if result["fwd_return_pct"] > 0 else "loss"
    return result


# ── Backtest driver ─────────────────────────────────────────────────────────

def backtest_calls(*, db_path: Optional[Path] = None) -> dict:
    """Score every priceable call in `documents` and persist to call_outcomes.

    Idempotent — re-running re-scores (INSERT OR REPLACE on document_id).
    Returns summary counts. Prices must already be in the `prices` table
    (run the price backfill first).
    """
    db_path = db_path or settings.sqlite_path
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_table(conn)
        rows = conn.execute(
            """
            SELECT document_id, author_id, published_at, ingested_at,
                   extracted_features_json
            FROM documents
            WHERE author_id IS NOT NULL AND author_id != ''
              AND extracted_features_json IS NOT NULL
            """
        ).fetchall()

        # First pass: parse calls, collect symbols to load.
        parsed: list[tuple[sqlite3.Row, dict, datetime]] = []
        symbols: set[str] = set()
        n_unpriceable = 0
        for r in rows:
            call = _extract_call(r["extracted_features_json"])
            if not call:
                continue
            dt = _parse_iso(r["published_at"]) or _parse_iso(r["ingested_at"])
            if dt is None:
                continue
            if not call["symbol"]:
                n_unpriceable += 1
                _persist(conn, r["document_id"], r["author_id"], call, dt,
                         {"resolved": "unpriceable", "horizon_days": None,
                          "direction": call["direction"], "fwd_return_pct": None,
                          "r_multiple": None})
                continue
            parsed.append((r, call, dt))
            symbols.add(call["symbol"])

        # Both benchmarks, always — which one a call uses is decided per
        # call by `_benchmark_for`, not by what the corpus happens to hold.
        symbols.add("BTC")
        symbols.add("SPY")
        bars_by_sym = _load_bars(conn, symbols)
        benchmarks = {
            "BTC": bars_by_sym.get("BTC", []),
            "SPY": bars_by_sym.get("SPY", []),
        }

        scored = 0
        no_price = 0
        for r, call, dt in parsed:
            bars = bars_by_sym.get(call["symbol"], [])
            if not bars:
                no_price += 1
                _persist(conn, r["document_id"], r["author_id"], call, dt,
                         {"resolved": "no_price_data", "horizon_days": None,
                          "direction": call["direction"], "fwd_return_pct": None,
                          "r_multiple": None})
                continue
            outcome = _score_one(
                call, bars, dt,
                btc_bars=benchmarks.get(_benchmark_for(call.get("symbol"))) or [],
            )
            _persist(conn, r["document_id"], r["author_id"], call, dt, outcome)
            scored += 1
        conn.commit()

    return {
        "scored": scored,
        "unpriceable": n_unpriceable,
        "no_price_data": no_price,
        "symbols_loaded": len(bars_by_sym),
    }


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS call_outcomes (
            document_id   TEXT PRIMARY KEY,
            author_id     TEXT NOT NULL,
            raw_ticker    TEXT,
            ticker        TEXT,
            symbol        TEXT,
            direction     TEXT,
            timeframe     TEXT,
            entry_px      REAL,
            stop_px       REAL,
            target_px     REAL,
            horizon_days  INTEGER,
            fwd_return_pct REAL,
            resolved      TEXT,          -- win|loss|open|unpriceable|no_price_data
            r_multiple    REAL,
            alpha_pct     REAL,           -- call return − benchmark (BTC crypto / SPY equity)
            call_at       TEXT,
            scored_at     TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_call_outcomes_author ON call_outcomes(author_id)")
    # Idempotent migrations. Each column is added once, on a table that
    # already exists from an earlier version.
    cols = {c[1] for c in conn.execute("PRAGMA table_info(call_outcomes)").fetchall()}
    if "alpha_pct" not in cols:
        conn.execute("ALTER TABLE call_outcomes ADD COLUMN alpha_pct REAL")
    # How far the author put the stop, as a fraction of entry. A per-author
    # average hides that this crowd's tight-stop scalps and their 20%-stop
    # position trades are different businesses with different edges.
    if "stop_width_pct" not in cols:
        conn.execute("ALTER TABLE call_outcomes ADD COLUMN stop_width_pct REAL")
    # intraday | swing | position — banded from the chart the call was drawn
    # on. Measured Sep 2026: expectancy 0.81% / 1.97% / 6.69% across these
    # three, so an unsegmented author number averages an 8x spread into one
    # misleading figure.
    if "tf_band" not in cols:
        conn.execute("ALTER TABLE call_outcomes ADD COLUMN tf_band TEXT")
    # 1 when the symbol has a venue the desk can actually fill on — a
    # Coinbase book, or a listed equity. 0 for the DEX pairs and microcaps
    # with neither. Rows are kept either way (they are still evidence about
    # the caller) but every weight and headline number filters on this: an
    # edge on something nobody can buy is not an edge this desk can spend.
    if "real_asset" not in cols:
        conn.execute("ALTER TABLE call_outcomes ADD COLUMN real_asset INTEGER")


def _benchmark_for(symbol: Optional[str]) -> str:
    """Which market is this call's beta?

    Alpha means "return the caller produced beyond just being in this
    market". Subtracting BTC from an EQUITY call does not measure that —
    it measures how crypto did that week, and it mis-scores whole sources:
    Stock Unlocked (US equities, text calls) read -14.3% alpha in Sep 2026
    almost entirely because BTC outran its names over the same windows.
    Equities benchmark to SPY, crypto to BTC.
    """
    try:
        from macro_positioning.prices.symbol_map import is_crypto

        return "BTC" if is_crypto(symbol or "") else "SPY"
    except Exception:
        return "BTC"


def _is_real_asset(symbol: Optional[str]) -> bool:
    """Is this something the desk could actually put money into?

    The test is the VENUE. Anything with a Coinbase book counts, the
    whole alt tail included; anything without one does not, which is what
    removes the DEX pairs and microcaps this crowd posts by the hundred
    (KINS/SOL, JOTCHUA/USDC, SCHIFFY/GLD) — no venue, and usually no
    price feed to score against either. Listed equities are a separate
    venue and count.

    `book_tradeable` is the single definition, shared with the paper
    books, so a learned weight and a holdable position can never disagree
    about what was measurable.
    """
    if not symbol:
        return False
    try:
        from macro_positioning.prices.symbol_map import book_tradeable

        return bool(book_tradeable(symbol))
    except Exception:          # universe cache unavailable — do not claim
        return False


def _persist(conn, document_id, author_id, call, dt, outcome) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO call_outcomes (
            document_id, author_id, raw_ticker, ticker, symbol, direction,
            timeframe, entry_px, stop_px, target_px, horizon_days,
            fwd_return_pct, resolved, r_multiple, alpha_pct,
            stop_width_pct, tf_band, real_asset, call_at, scored_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            document_id, author_id, call.get("raw_ticker"), call.get("ticker"),
            call.get("symbol"), outcome.get("direction"), call.get("timeframe"),
            call.get("entry"), call.get("stop"), call.get("target"),
            outcome.get("horizon_days"), outcome.get("fwd_return_pct"),
            outcome.get("resolved"), outcome.get("r_multiple"),
            outcome.get("alpha_pct"),
            _stop_width(call.get("entry"), call.get("stop")),
            _tf_band(call.get("timeframe")),
            1 if _is_real_asset(call.get("symbol")) else 0,
            dt.isoformat(), datetime.now(UTC).isoformat(),
        ),
    )


# ── Per-source rollup ────────────────────────────────────────────────────────

def source_accuracy(
    *, window_days: Optional[int] = None, db_path: Optional[Path] = None,
    real_assets_only: bool = True, by_timeframe: bool = False,
) -> list[dict]:
    """Per-author accuracy rollup from call_outcomes.

    `real_assets_only` (the default) restricts every number to symbols the
    desk would actually hold — listed equities and crypto majors. The DEX
    pairs and the Coinbase alt tail stay in the table as evidence about
    the caller, but they do not get a vote in any published figure or any
    learned weight: an edge on a microcap nobody can size into is not an
    edge this desk can spend.

    `by_timeframe` splits each author into intraday / swing / position
    rows instead of one blended row. Use it wherever the number drives a
    decision — the three bands measured 0.81% / 1.97% / 6.69% expectancy
    in Sep 2026, and averaging them describes nobody.
    """
    db_path = db_path or settings.sqlite_path
    clauses: list[str] = []
    params: list = []
    if window_days:
        clauses.append("call_at >= ?")
        params.append((datetime.now(UTC) - timedelta(days=window_days)).isoformat())
    if real_assets_only:
        # NULL means scored before the column existed — excluded on purpose,
        # so a stale row can never silently count as tradeable.
        clauses.append("real_asset = 1")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM call_outcomes {where}", tuple(params)
        ).fetchall()
        names = dict(conn.execute(
            "SELECT author_id, display_name FROM input_authors"
        ).fetchall())

    by_author: dict[tuple, dict] = defaultdict(lambda: {
        "n_calls": 0, "n_priceable": 0, "n_unpriceable": 0,
        "dir_wins": 0, "dir_scored": 0, "ret_sum": 0.0, "ret_n": 0,
        "alpha_sum": 0.0, "alpha_n": 0, "alpha_wins": 0,
        "setup_wins": 0, "setup_losses": 0, "r_sum": 0.0, "r_n": 0,
        "stop_sum": 0.0, "stop_n": 0,
    })
    for r in rows:
        key = (r["author_id"], r["tf_band"] or "unbanded") if by_timeframe else (
            r["author_id"], None)
        a = by_author[key]
        a["n_calls"] += 1
        resolved = r["resolved"]
        if resolved in ("unpriceable", "no_price_data"):
            a["n_unpriceable"] += 1
            continue
        a["n_priceable"] += 1
        if r["fwd_return_pct"] is not None:
            a["ret_sum"] += r["fwd_return_pct"]; a["ret_n"] += 1
            a["dir_scored"] += 1
            if r["fwd_return_pct"] > 0:
                a["dir_wins"] += 1
        if r["alpha_pct"] is not None:
            a["alpha_sum"] += r["alpha_pct"]; a["alpha_n"] += 1
            if r["alpha_pct"] > 0:
                a["alpha_wins"] += 1
        if resolved == "win":
            a["setup_wins"] += 1
        elif resolved == "loss":
            a["setup_losses"] += 1
        if r["r_multiple"] is not None:
            a["r_sum"] += r["r_multiple"]; a["r_n"] += 1
        if r["stop_width_pct"] is not None:
            a["stop_sum"] += r["stop_width_pct"]; a["stop_n"] += 1

    out = []
    for (author_id, band), a in by_author.items():
        setup_n = a["setup_wins"] + a["setup_losses"]
        # Min-sample gate: a verdict needs enough priceable calls to mean
        # anything. Below this we still return the row but flag it.
        meaningful = a["n_priceable"] >= _MIN_SAMPLE
        out.append({
            "author_id": author_id,
            "display_name": names.get(author_id, author_id),
            "tf_band": band,
            "n_calls": a["n_calls"],
            "n_priceable": a["n_priceable"],
            "n_unpriceable": a["n_unpriceable"],
            "meaningful": meaningful,  # n_priceable >= _MIN_SAMPLE
            # Setup-resolution is the PRIMARY skill metric (target-before-stop).
            "setup_win_rate": round(a["setup_wins"] / setup_n, 4) if setup_n else None,
            "avg_r_planned": round(a["r_sum"] / a["r_n"], 2) if a["r_n"] else None,
            # Alpha = market-relative (BTC for crypto, SPY for equities).
            "alpha_win_rate": round(a["alpha_wins"] / a["alpha_n"], 4) if a["alpha_n"] else None,
            "avg_alpha_pct": round(a["alpha_sum"] / a["alpha_n"], 3) if a["alpha_n"] else None,
            # Buy-and-hold directional (kept for reference; beta-dominated).
            "win_rate": round(a["dir_wins"] / a["dir_scored"], 4) if a["dir_scored"] else None,
            "avg_return_pct": round(a["ret_sum"] / a["ret_n"], 3) if a["ret_n"] else None,
            # How far this author puts the stop, on average. The desk's own
            # max_stop_pct decides whether a book can take that at all.
            "avg_stop_width_pct": (
                round(a["stop_sum"] / a["stop_n"] * 100, 2) if a["stop_n"] else None
            ),
            "real_assets_only": real_assets_only,
        })
    # Rank meaningful sources first, then by setup win rate, then alpha.
    out.sort(key=lambda x: (
        x["meaningful"],
        x["setup_win_rate"] or -1,
        x["avg_alpha_pct"] or -999,
    ), reverse=True)
    return out


# Minimum priceable calls for a source's accuracy to be considered meaningful.
_MIN_SAMPLE = 10
