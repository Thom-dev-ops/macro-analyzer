"""Stock Unlocked Trades — the persistent call tracker.

`scripts/stock_unlocked_efficacy.py` proved the method: walk the tape
forward from the moment of the post and let the level that printed FIRST
decide. This module makes that a ledger instead of a report — every call
the channel posts gets a row in `stock_unlocked_calls`, open calls are
re-walked on each tick until they resolve or time out, and the numbers
the desk itself reports ("Target 2 HIT", "stopped out") are kept next to
the tape's verdict so the two can be compared call by call.

Three measurement rules, each of which moved the win rate by ~7 points
when it was wrong (see the project memory on this source):

  • Hourly bars are stamped at the START of the hour, so the bar that
    contains the call must be re-walked at 5 minutes — DUOT printed its
    first target seven minutes after the post.
  • MFE/MAE stop at resolution. A winner's run is measured until its
    stop finally prints, not to the end of the window.
  • Target and stop inside one bar is re-walked at 5m; if they still
    share a bar the bar's OPEN breaks the tie (a gap through the stop is
    a stop-out, whatever the day's high later printed); anything still
    tied books as a loss.

Two things the script did not do that a ledger must:

  • **Crypto goes to Coinbase first.** Yahoo's bare `ARB-USD` is not
    Arbitrum, and its `HYPE-USD` is nothing at all; the CMC-id form from
    `symbol_map` fixes the first and Coinbase's keyless candles fix the
    second. A first bar more than half away from the stated entry is
    treated as the wrong instrument, never scored.
  • **A call past its horizon is `unresolved`, not `open`.** It counts
    in neither the wins nor the losses and stops being re-fetched.

Options carry no stop or target, so they are never scored against the
tape. Their row exists so the desk's self-reported premium exits have
somewhere to live; the summary reports them separately, never blended
into the setup win rate.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import warnings
from dataclasses import dataclass, field, asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from macro_positioning.core.settings import settings
from macro_positioning.db.connect import read_connection, write_connection
from macro_positioning.prices.symbol_map import to_yfinance_symbol
from macro_positioning.signals.stock_unlocked_extractor import SOURCE_ID, parse_post

log = logging.getLogger(__name__)

# How far forward a call may resolve before it is frozen as `unresolved`.
HORIZON_DAYS = {"day": 3, "swing": 20}
# A first bar this far from the stated entry means the price feed is
# pricing a different instrument (Yahoo's ARB is not Arbitrum).
_MAX_FIRST_BAR_DEVIATION = 0.5
# Retry an unpriceable call for this long after its post; Coinbase and
# Yahoo both lag new listings by a day or two.
_UNPRICEABLE_RETRY_DAYS = 5

# An option with this much of the original size still on is a running
# position with banked profit, not a closed trade — it stays out of the
# closed count until the desk sells the rest (or lets it expire).
_OPTION_RUNNING_PCT = 20.0

RESOLVED = ("win", "loss", "loss_ambiguous")
LIFECYCLE_KINDS = ("target_hit", "stop_hit", "partial_exit", "stop_move", "option_exit",
                   "breakeven_exit", "void")
# Verdicts that keep a call out of every denominator: the desk cancelled
# it, or it was a breakout alert whose trigger never printed.
EXCLUDED = ("void", "not_triggered")


@dataclass
class TrackedCall:
    call_id: str
    posted_at: datetime
    ticker: str
    instrument: str
    trade_kind: str
    direction: str
    entry: Optional[float]
    stop: Optional[float]
    targets: list[float]
    notes: Optional[str]
    option: Optional[dict]
    market_price: Optional[float]
    author: Optional[str] = None          # the alerter, where the post names one
    trigger: Optional[str] = None         # above | below — a breakout alert, armed at the entry
    fmt: Optional[str] = None             # v1 (2025 bot template) | v2 (current)
    verdict: str = "unscored"
    max_target: int = 0
    planned_r: Optional[float] = None
    realized_r: Optional[float] = None
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None
    hours_to_resolve: Optional[float] = None
    resolved_at: Optional[datetime] = None
    resolution: Optional[str] = None
    price_symbol: Optional[str] = None
    price_source: Optional[str] = None
    last_price: Optional[float] = None
    desk_events: list[dict] = field(default_factory=list)
    desk_verdict: Optional[str] = None
    desk_max_target: int = 0
    first_scored_at: Optional[datetime] = None
    last_scored_at: Optional[datetime] = None
    score_error: Optional[str] = None

    # ── derived ──
    @property
    def horizon_end(self) -> datetime:
        return self.posted_at + timedelta(days=HORIZON_DAYS.get(self.trade_kind, 20))

    @property
    def is_levelled(self) -> bool:
        return (self.instrument != "option" and self.entry is not None
                and self.stop is not None and bool(self.targets))

    @property
    def is_long(self) -> bool:
        return self.direction != "short"

    def to_api(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("posted_at", "resolved_at", "first_scored_at", "last_scored_at"):
            v = d[k]
            d[k] = v.isoformat() if isinstance(v, datetime) else v
        d["horizon_end"] = self.horizon_end.isoformat()
        d["n_targets"] = len(self.targets)
        d["is_levelled"] = self.is_levelled
        return d


# ─── price bars ────────────────────────────────────────────────────────

def _fetch_yf(symbol: str, start: datetime, end: datetime, interval: str):
    """Bars from Yahoo as [(ts, open, high, low, close), ...] ascending."""
    import yfinance as yf

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = yf.download(
                symbol, start=start.date(), end=(end + timedelta(days=1)).date(),
                interval=interval, progress=False, auto_adjust=False, threads=False,
            )
    except Exception:
        return []
    if df is None or df.empty:
        return []
    if getattr(df.columns, "nlevels", 1) > 1:
        df.columns = df.columns.droplevel(1)
    idx = df.index
    df.index = idx.tz_localize("UTC") if getattr(idx, "tz", None) is None else idx.tz_convert("UTC")
    return [
        (ts.to_pydatetime(), float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]))
        for ts, r in df.iterrows()
    ]


def _fetch_coinbase(product: str, start: datetime, end: datetime, granularity: int):
    """Coinbase Exchange candles — public, keyless, 300 candles per call."""
    import requests

    out: list[tuple[datetime, float, float, float, float]] = []
    cursor = start
    span = timedelta(seconds=granularity * 290)
    while cursor < end:
        chunk_end = min(cursor + span, end)
        url = (f"https://api.exchange.coinbase.com/products/{product}/candles"
               f"?start={cursor.isoformat()}&end={chunk_end.isoformat()}"
               f"&granularity={granularity}")
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            rows = resp.json()
        except Exception:
            return out
        for t, low, high, op, close, _vol in rows:
            out.append((datetime.fromtimestamp(t, UTC), float(op), float(high), float(low), float(close)))
        cursor = chunk_end
    out.sort(key=lambda r: r[0])
    return out


def _bars(call: TrackedCall, start: datetime, end: datetime, fine: bool = False):
    """Bars at hourly (or 5-minute, when disambiguating).

    Crypto: Coinbase first — it is the venue this project scopes
    tradeability to and its `ARB-USD` is Arbitrum. Yahoo is the fallback
    through the CMC-id map. Equities: Yahoo.
    """
    if call.instrument == "crypto":
        product = f"{call.ticker}-USD"
        cb = _fetch_coinbase(product, start, end, 300 if fine else 3600)
        if cb:
            call.price_source, call.price_symbol = "coinbase", product
            return cb
        yf_symbol = to_yfinance_symbol(product)
        yf_bars = _fetch_yf(yf_symbol, start, end, "5m" if fine else "1h")
        if yf_bars:
            call.price_source, call.price_symbol = "yahoo", yf_symbol
        return yf_bars
    call.price_source, call.price_symbol = "yahoo", call.ticker
    return _fetch_yf(call.ticker, start, end, "5m" if fine else "1h")


def _first_touch(bars, call: TrackedCall):
    """(first target-1 touch, first stop touch, furthest target before stop)."""
    long = call.is_long
    t_at = s_at = None
    max_t = 0
    for ts, _op, hi, lo, *_ in bars:
        if s_at is None:
            for i, t in enumerate(call.targets, start=1):
                if (hi >= t) if long else (lo <= t):
                    max_t = max(max_t, i)
        if t_at is None and ((hi >= call.targets[0]) if long else (lo <= call.targets[0])):
            t_at = ts
        if s_at is None and ((lo <= call.stop) if long else (hi >= call.stop)):
            s_at = ts
        if t_at and s_at:
            break
    return t_at, s_at, max_t


def score_call(call: TrackedCall, *, now: Optional[datetime] = None) -> TrackedCall:
    """Walk the tape forward from the post; the level touched FIRST decides."""
    now = now or datetime.now(UTC)
    call.last_scored_at = now
    call.first_scored_at = call.first_scored_at or now
    call.score_error = None
    if not call.is_levelled:
        call.verdict = "no_levels"
        return call

    end = min(now, call.horizon_end)
    head_end = min(end, call.posted_at + timedelta(hours=2))
    head = [b for b in _bars(call, call.posted_at, head_end, fine=True)
            if call.posted_at <= b[0] <= head_end]
    hourly = _bars(call, call.posted_at - timedelta(days=1), end)
    coarse_head = False
    if not head:
        # Yahoo keeps 5-minute bars for sixty days. Past that the bar that
        # CONTAINS the call has to stand in, which can only overstate what
        # printed after the post — flagged on the row as `1h_coarse`.
        head = [b for b in hourly if call.posted_at - timedelta(hours=1) <= b[0] <= head_end]
        coarse_head = bool(head)
    tail = [b for b in hourly if head_end < b[0] <= end]
    bars = head + tail
    if not bars:
        call.verdict = "unpriceable"
        call.score_error = "no bars from any provider"
        return call
    first_open = bars[0][1]
    if call.entry and abs(first_open - call.entry) / call.entry > _MAX_FIRST_BAR_DEVIATION:
        call.verdict = "unpriceable"
        call.score_error = (f"first bar {first_open:g} is >{_MAX_FIRST_BAR_DEVIATION:.0%} "
                            f"from entry {call.entry:g} on {call.price_symbol} — wrong instrument?")
        return call
    call.last_price = bars[-1][1]

    long = call.is_long
    risk = abs(call.entry - call.stop)
    call.planned_r = round(abs(call.targets[0] - call.entry) / risk, 2) if risk else None

    # A breakout alert ("OPEN Above 4.17") is not a position until price
    # prints the entry; until then its stop sits on the wrong side of the
    # market and would "hit" on the first bar. Arm it at the trigger.
    if call.trigger:
        armed = next((i for i, b in enumerate(bars)
                      if ((b[2] >= call.entry) if long else (b[3] <= call.entry))), None)
        if armed is None:
            call.verdict = "open" if now < call.horizon_end else "not_triggered"
            call.resolution = None
            return call
        bars = bars[armed:]

    t_at, s_at, max_t = _first_touch(bars, call)
    call.max_target = max_t

    def _excursions(until: Optional[datetime]) -> None:
        window = [b for b in bars if until is None or b[0] <= until]
        if not window:
            return
        best = max(b[2] for b in window) if long else min(b[3] for b in window)
        worst = min(b[3] for b in window) if long else max(b[2] for b in window)
        call.mfe_pct = round((best / call.entry - 1) * 100 * (1 if long else -1), 2)
        call.mae_pct = round((worst / call.entry - 1) * 100 * (1 if long else -1), 2)

    def _close(verdict: str, at: datetime, r: Optional[float], resolution: str) -> None:
        call.verdict = verdict
        call.resolved_at = at
        call.realized_r = r
        call.resolution = resolution
        call.hours_to_resolve = round((at - call.posted_at).total_seconds() / 3600, 1)

    if t_at is None and s_at is None:
        call.verdict = "open" if now < call.horizon_end else "unresolved"
        call.resolution = None
        _excursions(None)
        return call

    resolution = "1h_coarse" if coarse_head else "1h"
    if t_at is not None and s_at is not None and t_at == s_at:
        fine = [b for b in _bars(call, t_at - timedelta(minutes=5),
                                 t_at + timedelta(hours=1), fine=True)
                if t_at <= b[0] <= t_at + timedelta(hours=1)]
        ft, fs, fmax = _first_touch(fine, call) if fine else (None, None, max_t)
        if ft and fs and ft != fs:
            t_at, s_at = ft, fs
            call.max_target = max(max_t, fmax)
            resolution = "5m"
        else:
            op = next((b[1] for b in (fine or bars) if b[0] >= t_at), None)
            if op is not None and ((op <= call.stop) if long else (op >= call.stop)):
                s_at, t_at = t_at, None
                resolution = "gap_open"
            elif op is not None and ((op >= call.targets[0]) if long else (op <= call.targets[0])):
                t_at, s_at = t_at, None
                resolution = "gap_open"
            else:
                call.max_target = 0            # nothing was reached before the stop, on this evidence
                _close("loss_ambiguous", t_at, -1.0, "ambiguous")
                _excursions(t_at)
                return call

    if s_at is not None and (t_at is None or s_at < t_at):
        call.max_target = 0
        _close("loss", s_at, -1.0, resolution)
        _excursions(s_at)
        return call

    reached = call.targets[max(call.max_target, 1) - 1]
    _close("win", t_at, round(abs(reached - call.entry) / risk, 2) if risk else None, resolution)
    _excursions(s_at)   # a winner's run ends where its stop finally printed
    return call


# ─── the desk's own account ────────────────────────────────────────────

def _option_key(parsed: dict) -> str:
    o = parsed.get("option") or {}
    return f"{parsed['ticker']}:{o.get('strike')}:{o.get('type')}"


def attach_desk_events(calls: list[TrackedCall], posts: list[tuple[str, datetime, dict]]) -> None:
    """Route every lifecycle post to the call it belongs to and derive the
    desk's verdict from the sequence.

    The desk's verdict uses the same rule as the tape's: a target
    reported before a stop is a win, a stop reported first is a loss.
    A stop that follows a target hit is the trailing stop closing a
    winner, not a reversal.
    """
    for c in calls:
        c.desk_events = []
        c.desk_verdict = None
        c.desk_max_target = 0

    by_ticker: dict[str, list[TrackedCall]] = {}
    for c in sorted(calls, key=lambda x: x.posted_at):
        by_ticker.setdefault(c.ticker, []).append(c)

    for doc_id, when, parsed in sorted(posts, key=lambda p: p[1]):
        kind = parsed["post_kind"]
        if kind not in LIFECYCLE_KINDS:
            continue
        prior = [c for c in by_ticker.get(parsed["ticker"], []) if c.posted_at <= when]
        if kind == "option_exit":
            prior = [c for c in prior if c.instrument == "option"
                     and _option_key({"ticker": c.ticker, "option": c.option}) == _option_key(parsed)]
        else:
            prior = [c for c in prior if c.instrument != "option"]
        if not prior:
            continue
        call = prior[-1]
        event = dict(parsed.get("event") or {})
        rec: dict[str, Any] = {"at": when.isoformat(), "kind": kind, "doc_id": doc_id}
        if kind == "option_exit":
            o = parsed.get("option") or {}
            rec.update({"exit_size": o.get("exit_size"), "exit_premium": o.get("exit_premium")})
        else:
            idx = event.get("target_index")
            if kind == "target_hit":
                if event.get("all_targets"):
                    idx = len(call.targets) or idx
                elif idx is None:
                    # "CELH 33.00 HIT!" — match the price to a level, else
                    # assume the next one up.
                    px = event.get("reported_price")
                    if px and call.targets:
                        idx = min(range(len(call.targets)),
                                  key=lambda i: abs(call.targets[i] - px)) + 1
                    else:
                        idx = min(call.desk_max_target + 1, len(call.targets) or 1)
            rec.update({"target_index": idx, "reported_price": event.get("reported_price"),
                        "text": event.get("text")})
            if kind == "target_hit" and idx:
                call.desk_max_target = max(call.desk_max_target, int(idx))
        call.desk_events.append(rec)

    for c in calls:
        if c.instrument == "option":
            c.desk_verdict = _option_desk_verdict(c)
            continue
        verdict = None
        for e in c.desk_events:
            if e["kind"] == "void":
                verdict = "void"                 # cancelled — overrides everything
                break
            if verdict is not None:
                continue
            if e["kind"] == "target_hit":
                verdict = "win"
            elif e["kind"] == "stop_hit":
                verdict = "loss"
            elif e["kind"] == "breakeven_exit":
                verdict = "flat"
        c.desk_verdict = verdict or "open"


def _option_desk_verdict(c: TrackedCall) -> Optional[str]:
    """Premium P&L from the desk's exit posts, stored on the option leg.

    Sizes are percentages of the ORIGINAL position; `ALL` closes whatever
    is left. An exit without a price cannot be valued — the P&L is then
    marked partial rather than guessed.
    """
    o = dict(c.option or {})
    prem = o.get("premium")
    legs = []
    remaining = 100.0
    pnl = 0.0
    complete = True
    for e in c.desk_events:
        if e["kind"] != "option_exit":
            continue
        size = e.get("exit_size")
        xp = e.get("exit_premium")
        pct = remaining if (size or "").upper() == "ALL" else float(str(size).rstrip("%") or 0)
        pct = max(0.0, min(pct, remaining))
        if pct == 0:
            continue          # a restated exit after "ALL" — nothing left to sell
        remaining -= pct
        if xp and prem:
            pnl += pct / 100.0 * (xp / prem - 1) * 100
        else:
            complete = False
        legs.append({"at": e["at"], "size_pct": pct, "exit_premium": xp,
                     "pnl_pct": round((xp / prem - 1) * 100, 1) if xp and prem else None})
    o["exits"] = legs
    o["remaining_pct"] = round(remaining, 1)
    o["pnl_pct"] = round(pnl, 1) if legs else None
    o["pnl_complete"] = complete
    c.option = o
    if not legs:
        return "open"
    if not complete and not legs[-1].get("exit_premium"):
        return "partial"          # cannot be valued
    if remaining >= _OPTION_RUNNING_PCT:
        return "running"          # scaled out, but most of it is still on
    return "win" if pnl > 0 else "loss" if pnl < 0 else "flat"


# ─── persistence ───────────────────────────────────────────────────────

def _load_posts(conn: sqlite3.Connection) -> list[tuple[str, datetime, dict]]:
    rows = conn.execute(
        "SELECT document_id, published_at, raw_text FROM documents "
        "WHERE source_id=? ORDER BY published_at ASC",
        (SOURCE_ID,),
    ).fetchall()
    out = []
    for doc_id, published_at, raw in rows:
        parsed = parse_post(raw)
        if not parsed or not published_at:
            continue
        when = datetime.fromisoformat(published_at)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        out.append((doc_id, when, parsed))
    return out


def _dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    d = datetime.fromisoformat(v)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _row_to_call(r: sqlite3.Row) -> TrackedCall:
    return TrackedCall(
        call_id=r["call_id"], posted_at=_dt(r["posted_at"]), ticker=r["ticker"],
        instrument=r["instrument"], trade_kind=r["trade_kind"], direction=r["direction"],
        entry=r["entry"], stop=r["stop"], targets=json.loads(r["targets_json"] or "[]"),
        notes=r["notes"], option=json.loads(r["option_json"]) if r["option_json"] else None,
        market_price=r["market_price"], author=r["author"], trigger=r["trigger"], fmt=r["fmt"],
        verdict=r["verdict"], max_target=r["max_target"] or 0,
        planned_r=r["planned_r"], realized_r=r["realized_r"], mfe_pct=r["mfe_pct"],
        mae_pct=r["mae_pct"], hours_to_resolve=r["hours_to_resolve"],
        resolved_at=_dt(r["resolved_at"]), resolution=r["resolution"],
        price_symbol=r["price_symbol"], price_source=r["price_source"],
        last_price=r["last_price"],
        desk_events=json.loads(r["desk_events_json"] or "[]"),
        desk_verdict=r["desk_verdict"], desk_max_target=r["desk_max_target"] or 0,
        first_scored_at=_dt(r["first_scored_at"]), last_scored_at=_dt(r["last_scored_at"]),
        score_error=r["score_error"],
    )


_UPSERT = """
INSERT INTO stock_unlocked_calls (
    call_id, posted_at, ticker, instrument, trade_kind, direction, entry, stop,
    targets_json, notes, option_json, market_price, verdict, max_target, planned_r,
    realized_r, mfe_pct, mae_pct, hours_to_resolve, resolved_at, resolution,
    price_symbol, price_source, last_price, desk_events_json, desk_verdict,
    desk_max_target, first_scored_at, last_scored_at, score_error,
    author, trigger, fmt
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(call_id) DO UPDATE SET
    author=excluded.author, trigger=excluded.trigger, fmt=excluded.fmt,
    posted_at=excluded.posted_at, ticker=excluded.ticker, instrument=excluded.instrument,
    trade_kind=excluded.trade_kind, direction=excluded.direction, entry=excluded.entry,
    stop=excluded.stop, targets_json=excluded.targets_json, notes=excluded.notes,
    option_json=excluded.option_json, market_price=excluded.market_price,
    verdict=excluded.verdict, max_target=excluded.max_target, planned_r=excluded.planned_r,
    realized_r=excluded.realized_r, mfe_pct=excluded.mfe_pct, mae_pct=excluded.mae_pct,
    hours_to_resolve=excluded.hours_to_resolve, resolved_at=excluded.resolved_at,
    resolution=excluded.resolution, price_symbol=excluded.price_symbol,
    price_source=excluded.price_source, last_price=excluded.last_price,
    desk_events_json=excluded.desk_events_json, desk_verdict=excluded.desk_verdict,
    desk_max_target=excluded.desk_max_target, first_scored_at=excluded.first_scored_at,
    last_scored_at=excluded.last_scored_at, score_error=excluded.score_error
"""


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.isoformat() if d else None


def _save(conn: sqlite3.Connection, c: TrackedCall) -> None:
    conn.execute(_UPSERT, (
        c.call_id, _iso(c.posted_at), c.ticker, c.instrument, c.trade_kind, c.direction,
        c.entry, c.stop, json.dumps(c.targets), c.notes,
        json.dumps(c.option) if c.option is not None else None, c.market_price,
        c.verdict, c.max_target, c.planned_r, c.realized_r, c.mfe_pct, c.mae_pct,
        c.hours_to_resolve, _iso(c.resolved_at), c.resolution, c.price_symbol,
        c.price_source, c.last_price, json.dumps(c.desk_events), c.desk_verdict,
        c.desk_max_target, _iso(c.first_scored_at), _iso(c.last_scored_at), c.score_error,
        c.author, c.trigger, c.fmt,
    ))


def _ensure_table(conn: sqlite3.Connection) -> None:
    from macro_positioning.db.schema import SCHEMA_STATEMENTS, _ADDED_COLUMNS

    for stmt in SCHEMA_STATEMENTS:
        if "stock_unlocked_calls" in stmt:
            conn.execute(stmt)
    have = {r[1] for r in conn.execute("PRAGMA table_info(stock_unlocked_calls)")}
    for table, col, typ in _ADDED_COLUMNS:
        if table == "stock_unlocked_calls" and col not in have:
            conn.execute(f"ALTER TABLE stock_unlocked_calls ADD COLUMN {col} {typ}")


def load_calls(*, db_path: Optional[Path] = None) -> list[TrackedCall]:
    conn = read_connection(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT * FROM stock_unlocked_calls ORDER BY posted_at DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            return []          # table not created yet — nothing tracked
        return [_row_to_call(r) for r in rows]
    finally:
        conn.close()


def _needs_scoring(c: TrackedCall, now: datetime) -> bool:
    if not c.is_levelled:
        return c.verdict != "no_levels"
    if c.verdict in ("unscored", "open"):
        return True
    if c.verdict == "unpriceable":
        return now < c.posted_at + timedelta(days=_UNPRICEABLE_RETRY_DAYS)
    return False


def sync(*, db_path: Optional[Path] = None, rescore: bool = False,
         now: Optional[datetime] = None) -> dict[str, Any]:
    """Bring the ledger up to date with the channel.

    1. Parse every post; upsert a row for each actionable call.
    2. Re-derive the desk's events and verdict for every call (cheap,
       and the routing depends on which calls exist).
    3. Walk the tape for every call that is still open, plus any that
       has never been scored. `rescore=True` re-walks everything — use
       after a scoring-rule change, never on the tick.
    """
    now = now or datetime.now(UTC)
    conn = write_connection(db_path)
    try:
        _ensure_table(conn)
        posts = _load_posts(conn)
        existing = {r["call_id"]: _row_to_call(r)
                    for r in conn.execute("SELECT * FROM stock_unlocked_calls").fetchall()}

        calls: list[TrackedCall] = []
        new = 0
        for doc_id, when, p in posts:
            if p["post_kind"] not in ("entry", "option_entry"):
                continue
            c = existing.get(doc_id)
            if c is None:
                new += 1
                c = TrackedCall(
                    call_id=doc_id, posted_at=when, ticker=p["ticker"],
                    instrument=p["instrument"], trade_kind=p["trade_kind"],
                    direction=p["direction"] or "long", entry=p["entry"], stop=p["stop"],
                    targets=list(p["targets"]), notes=p["notes"], option=p["option"],
                    market_price=p.get("market_price"), author=p.get("author"),
                    trigger=p.get("trigger"), fmt=p.get("format"),
                )
            else:
                # The parse is the source of truth for the stated levels; a
                # parser fix should flow through without a manual reset.
                c.entry, c.stop, c.targets = p["entry"], p["stop"], list(p["targets"])
                c.direction = p["direction"] or c.direction
                c.notes, c.market_price = p["notes"], p.get("market_price")
                c.author, c.trigger, c.fmt = p.get("author"), p.get("trigger"), p.get("format")
                if c.instrument == "option":
                    base = dict(p["option"] or {})
                    c.option = base
            calls.append(c)

        attach_desk_events(calls, posts)

        scored = 0
        errors = 0
        for c in calls:
            if c.desk_verdict == "void":
                c.verdict = "void"               # cancelled by the desk — never walked
                c.last_scored_at = now
                _save(conn, c)
                continue
            if rescore or _needs_scoring(c, now):
                try:
                    score_call(c, now=now)
                    scored += 1
                except Exception as exc:          # a feed hiccup must not stop the pass
                    log.exception("scoring %s %s failed", c.ticker, c.call_id)
                    c.score_error = f"{type(exc).__name__}: {exc}"
                    c.last_scored_at = now
                    errors += 1
            _save(conn, c)
        conn.commit()
        by_verdict: dict[str, int] = {}
        for c in calls:
            by_verdict[c.verdict] = by_verdict.get(c.verdict, 0) + 1
        return {"posts": len(posts), "calls": len(calls), "new": new,
                "scored": scored, "errors": errors, "by_verdict": by_verdict,
                "at": now.isoformat()}
    finally:
        conn.close()


# ─── the numbers ───────────────────────────────────────────────────────

def _bucket(calls: list[TrackedCall]) -> dict[str, Any]:
    resolved = [c for c in calls if c.verdict in RESOLVED]
    wins = [c for c in resolved if c.verdict == "win"]
    losses = [c for c in resolved if c.verdict != "win"]
    rs = [c.realized_r for c in resolved if c.realized_r is not None]
    pos = sum(r for r in rs if r > 0)
    neg = -sum(r for r in rs if r < 0)
    return {
        "n": len(calls),
        "resolved": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100.0 * len(wins) / len(resolved), 1) if resolved else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "total_r": round(sum(rs), 2) if rs else 0.0,
        "profit_factor": (round(pos / neg, 2) if neg else (None if not pos else float("inf"))),
    }


def _pf_json(v: Any) -> Any:
    return None if v is None else (999.0 if v == float("inf") else v)


def summary(calls: Optional[list[TrackedCall]] = None, *,
            db_path: Optional[Path] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    calls = load_calls(db_path=db_path) if calls is None else calls
    excluded = [c for c in calls if c.verdict in EXCLUDED]
    levelled = [c for c in calls if c.instrument != "option" and c.verdict not in EXCLUDED]
    options = [c for c in calls if c.instrument == "option" and c.verdict not in EXCLUDED]
    resolved = [c for c in levelled if c.verdict in RESOLVED]
    open_ = [c for c in levelled if c.verdict == "open"]

    overall = _bucket(levelled)
    overall["profit_factor"] = _pf_json(overall["profit_factor"])
    planned = [c.planned_r for c in levelled if c.planned_r]
    hours = [c.hours_to_resolve for c in resolved if c.hours_to_resolve is not None]
    overall.update({
        "open": len(open_),
        "unresolved": sum(1 for c in levelled if c.verdict == "unresolved"),
        "unpriceable": sum(1 for c in levelled if c.verdict == "unpriceable"),
        "void": sum(1 for c in excluded if c.verdict == "void"),
        "not_triggered": sum(1 for c in excluded if c.verdict == "not_triggered"),
        "avg_planned_r": round(sum(planned) / len(planned), 2) if planned else None,
        "median_hours_to_resolve": round(statistics.median(hours), 1) if hours else None,
        "expectancy_r": overall["avg_r"],
    })

    # How deep the winners ran: share of resolved calls that reached ≥Tn.
    max_n = max((len(c.targets) for c in levelled), default=0)
    reach = []
    for n in range(1, max_n + 1):
        eligible = [c for c in resolved if len(c.targets) >= n]
        hit = [c for c in eligible if c.max_target >= n]
        reach.append({"target": n, "eligible": len(eligible), "hit": len(hit),
                      "pct": round(100.0 * len(hit) / len(eligible), 0) if eligible else None})

    def _by(key) -> list[dict]:
        groups: dict[str, list[TrackedCall]] = {}
        for c in levelled:
            groups.setdefault(key(c), []).append(c)
        out = []
        for k, v in sorted(groups.items()):
            b = _bucket(v)
            b["profit_factor"] = _pf_json(b["profit_factor"])
            out.append({"key": k, **b})
        return out

    def _window(days: int) -> dict:
        since = now - timedelta(days=days)
        b = _bucket([c for c in levelled if c.posted_at >= since])
        b["profit_factor"] = _pf_json(b["profit_factor"])
        return {"days": days, **b}

    # Cumulative R in resolution order — the "equity curve" of a 1R bet.
    curve = []
    cum = 0.0
    for c in sorted(resolved, key=lambda x: x.resolved_at or x.posted_at):
        if c.realized_r is None:
            continue
        cum += c.realized_r
        curve.append({"at": _iso(c.resolved_at), "ticker": c.ticker,
                      "r": c.realized_r, "cum_r": round(cum, 2)})

    streak = 0
    streak_kind = None
    for c in sorted(resolved, key=lambda x: x.resolved_at or x.posted_at, reverse=True):
        kind = "W" if c.verdict == "win" else "L"
        if streak_kind is None:
            streak_kind = kind
        if kind != streak_kind:
            break
        streak += 1

    # The desk's account vs the tape's.
    claimed_win = [c for c in levelled if c.desk_verdict == "win"]
    claimed_loss = [c for c in levelled if c.desk_verdict == "loss"]
    agree_win = [c for c in claimed_win if c.verdict == "win"]
    agree_loss = [c for c in claimed_loss if c.verdict in ("loss", "loss_ambiguous")]
    silent = [c for c in levelled if c.desk_verdict == "open" and c.verdict in RESOLVED]
    disagreements = [
        {"call_id": c.call_id, "ticker": c.ticker, "posted_at": _iso(c.posted_at),
         "desk": c.desk_verdict, "tape": c.verdict, "resolution": c.resolution}
        for c in levelled
        if c.desk_verdict in ("win", "loss") and c.verdict in RESOLVED
        and (c.desk_verdict == "win") != (c.verdict == "win")
    ]
    desk = {
        "claimed_win": len(claimed_win), "tape_agrees_win": len(agree_win),
        "claimed_loss": len(claimed_loss), "tape_agrees_loss": len(agree_loss),
        "silent_resolved": len(silent),
        "silent_lost": sum(1 for c in silent if c.verdict != "win"),
        "disagreements": disagreements,
        "desk_win_rate": (round(100.0 * len(claimed_win) / (len(claimed_win) + len(claimed_loss)), 1)
                          if (claimed_win or claimed_loss) else None),
    }

    opt_closed = [c for c in options if c.desk_verdict in ("win", "loss", "flat")]
    opt_pnls = [c.option.get("pnl_pct") for c in opt_closed
                if c.option and c.option.get("pnl_pct") is not None]
    opts = {
        "n": len(options),
        "closed": len(opt_closed),
        "open": sum(1 for c in options if c.desk_verdict == "open"),
        "running": sum(1 for c in options if c.desk_verdict == "running"),
        "partial": sum(1 for c in options if c.desk_verdict == "partial"),
        "wins": sum(1 for c in opt_closed if c.desk_verdict == "win"),
        "losses": sum(1 for c in opt_closed if c.desk_verdict == "loss"),
        "win_rate": (round(100.0 * sum(1 for c in opt_closed if c.desk_verdict == "win")
                           / len(opt_closed), 1) if opt_closed else None),
        "avg_pnl_pct": round(sum(opt_pnls) / len(opt_pnls), 1) if opt_pnls else None,
        "total_pnl_pct": round(sum(opt_pnls), 1) if opt_pnls else None,
    }

    last_scored = max((c.last_scored_at for c in calls if c.last_scored_at), default=None)
    first_post = min((c.posted_at for c in calls), default=None)
    return {
        "as_of": now.isoformat(),
        "last_synced_at": _iso(last_scored),
        "tracking_since": _iso(first_post),
        "calls": len(calls),
        "levelled": len(levelled),
        "options": len(options),
        "overall": overall,
        "target_reach": reach,
        "by_kind": _by(lambda c: c.trade_kind),
        "by_instrument": _by(lambda c: c.instrument),
        "by_direction": _by(lambda c: c.direction),
        "by_month": _by(lambda c: c.posted_at.strftime("%Y-%m")),
        "by_author": _by(lambda c: c.author or ("desk" if c.fmt == "v2" else "unknown")),
        "by_format": _by(lambda c: {"v1": "2025 bot template", "v2": "current template"}.get(c.fmt or "", "unknown")),
        "windows": [_window(7), _window(30), _window(90)],
        "curve": curve,
        "streak": {"kind": streak_kind, "n": streak},
        "desk": desk,
        "options_summary": opts,
        "horizon_days": HORIZON_DAYS,
    }


__all__ = [
    "TrackedCall", "HORIZON_DAYS", "RESOLVED",
    "score_call", "attach_desk_events", "sync", "load_calls", "summary",
]
