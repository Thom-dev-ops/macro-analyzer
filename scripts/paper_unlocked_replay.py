#!/usr/bin/env python3
"""Replay the Stock Unlocked book over the channel's whole history.

The live tick opens positions from today forward. This walks the same
engine, with the same mandate and the same candidate screen, from the
first call in the ledger to now — one tick every two hours — with every
mark taken from the hourly bars the tracker already walks. The result is
the equity curve the $50k book would have printed had it been running
since the channel's first post, written into the ordinary `paper_*`
tables so the page renders it like any other book.

No lookahead, by construction:

  • the candidate screen at tick T sees only calls posted at or before
    T, with the desk's lifecycle posts up to T and the tape's verdict
    computed from bars up to T;
  • the mark at T is the close of the last hourly bar that ENDED at or
    before T (a bar stamped 13:30 closes at 14:30 and is not known at
    14:00); a name with no bar yet is unpriced and the engine holds;
  • the engine's own `now` is T, so cooldowns, minimum holds, staleness
    and the learning window all read the replay clock.

What this cannot reproduce: intra-bar fills. The engine fills at the
mark; a day trade that printed T1 and the stop inside the same two-hour
window resolves on this book by whichever the engine saw first at the
tick, which the tracker's 5-minute walk may disagree with. That is the
difference between a ledger and a book, and it is the reason both exist.

Usage:
  .venv/bin/python scripts/paper_unlocked_replay.py --dry-run        # count ticks, fetch nothing
  .venv/bin/python scripts/paper_unlocked_replay.py --execute        # replay into a fresh book
  .venv/bin/python scripts/paper_unlocked_replay.py --execute --from 2026-03-01
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import time
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

warnings.filterwarnings("ignore")

from macro_positioning.db.connect import write_connection                    # noqa: E402
from macro_positioning.paper import store                                     # noqa: E402
from macro_positioning.paper.engine import run_tick                           # noqa: E402
from macro_positioning.paper.stock_unlocked_book import (                     # noqa: E402
    BOOK_NAME, book_candidates, load_book_mandate, load_source_config,
)
from macro_positioning.prices.symbol_map import resolve_symbol                # noqa: E402
from macro_positioning.tracker import stock_unlocked as tracker               # noqa: E402

log = logging.getLogger("unlocked_replay")

# How long after its post a call's bars are kept: long enough to mark a
# position the book held past the call's own horizon.
_BARS_AFTER_DAYS = 45


# ── bars ───────────────────────────────────────────────────────────────

def _load_bars(calls: list[tracker.TrackedCall], now: datetime) -> dict[str, list]:
    """Hourly bars per call_id, fetched once. The tracker's fetchers, so
    Coinbase-first for crypto and the CMC-id map for Yahoo."""
    bars: dict[str, list] = {}
    for i, c in enumerate(calls, start=1):
        end = min(now, c.posted_at + timedelta(days=_BARS_AFTER_DAYS))
        try:
            b = tracker._bars(c, c.posted_at - timedelta(days=1), end)
        except Exception as exc:                    # noqa: BLE001
            log.warning("bars failed for %s: %s", c.ticker, exc)
            b = []
        bars[c.call_id] = [x for x in b if len(x) >= 5]
        if i % 25 == 0:
            print(f"  bars {i}/{len(calls)}", file=sys.stderr)
    return bars


class Tape:
    """Marks and as-of tape state from the cached bars."""

    def __init__(self, calls: list[tracker.TrackedCall], bars: dict[str, list]):
        self.bars = bars
        self.by_symbol: dict[str, list[tracker.TrackedCall]] = {}
        self.symbol_of: dict[str, str] = {}
        for c in calls:
            sym = _symbol(c)
            if sym:
                self.symbol_of[c.call_id] = sym
                self.by_symbol.setdefault(sym, []).append(c)

    def mark(self, symbol: str, t: datetime) -> Optional[float]:
        best: Optional[tuple[datetime, float]] = None
        for c in self.by_symbol.get(symbol.upper(), []):
            for b in self.bars.get(c.call_id, []):
                if b[0] + timedelta(hours=1) <= t and (best is None or b[0] > best[0]):
                    best = (b[0], b[4])
        # Bars are cached per call for 45 days; a mark older than a week
        # is a name the book should not be pricing off this tape.
        if best is None or t - best[0] > timedelta(days=7):
            return None
        return best[1]

    def as_of(self, c: tracker.TrackedCall, t: datetime) -> tracker.TrackedCall:
        """The call as the tracker would have scored it at T."""
        c = dataclasses.replace(c)
        c.desk_events = [e for e in c.desk_events if e["at"] <= t.isoformat()]
        c.desk_verdict, c.desk_max_target = _desk_state(c)
        bars = [b for b in self.bars.get(c.call_id, []) if c.posted_at - timedelta(hours=1) <= b[0] and b[0] + timedelta(hours=1) <= t]
        c.verdict, c.max_target, c.realized_r, c.resolved_at = _tape_state(c, bars, t)
        c.last_price = bars[-1][4] if bars else None
        return c


def _symbol(c: tracker.TrackedCall) -> Optional[str]:
    return resolve_symbol(f"{c.ticker}/USD" if c.instrument == "crypto" else c.ticker)


def _desk_state(c: tracker.TrackedCall) -> tuple[str, int]:
    verdict = None
    max_t = 0
    for e in c.desk_events:
        if e["kind"] == "void":
            return "void", max_t
        if e["kind"] == "target_hit" and e.get("target_index"):
            max_t = max(max_t, int(e["target_index"]))
        if verdict is None:
            if e["kind"] == "target_hit":
                verdict = "win"
            elif e["kind"] == "stop_hit":
                verdict = "loss"
            elif e["kind"] == "breakeven_exit":
                verdict = "flat"
    return verdict or "open", max_t


def _tape_state(c: tracker.TrackedCall, bars: list, t: datetime):
    """(verdict, max_target, realized_r, resolved_at) from bars up to T.
    Hourly only — ties book as a loss, as the tracker does when it cannot
    split them."""
    if not c.is_levelled or not bars:
        return ("open" if t < c.horizon_end else "unresolved"), 0, None, None
    long = c.is_long
    if c.trigger:
        armed = next((i for i, b in enumerate(bars)
                      if ((b[2] >= c.entry) if long else (b[3] <= c.entry))), None)
        if armed is None:
            return ("open" if t < c.horizon_end else "not_triggered"), 0, None, None
        bars = bars[armed:]
    horizon = [b for b in bars if b[0] <= c.horizon_end]
    t_at, s_at, max_t = tracker._first_touch(horizon, c)
    risk = abs(c.entry - c.stop)
    if s_at is not None and (t_at is None or s_at <= t_at):
        return "loss", 0, -1.0, s_at
    if t_at is not None:
        reached = c.targets[max(max_t, 1) - 1]
        return "win", max_t, (round(abs(reached - c.entry) / risk, 2) if risk else None), t_at
    return ("open" if t < c.horizon_end else "unresolved"), 0, None, None


# ── the replay ─────────────────────────────────────────────────────────

_RETIRE_NOTE = "closed: superseded by history replay"


def _retire_existing_book() -> Optional[str]:
    """Flatten and close any live Stock Unlocked book so the replay owns the name.

    Flattening first is the point. A closed book never ticks again, so any
    position left open in it stops having its stop and target enforced while
    still reading as open risk — that is how a 2.5R runner sat unmanaged past
    its target for three days. The book closes flat or it does not close.
    """
    retired = None
    for pf in store.list_portfolios():
        if pf.name == BOOK_NAME and pf.status == "active":
            fills = store.flatten_portfolio(pf.portfolio_id, reason=_RETIRE_NOTE)
            if fills:
                realized = sum(f.realized_pnl or 0.0 for f in fills)
                print(f"  flattened {len(fills)} open position(s) in {pf.portfolio_id} "
                      f"— realized ${realized:,.2f}")
            conn = write_connection()
            try:
                conn.execute(
                    "UPDATE paper_portfolios SET status='closed', "
                    "notes=COALESCE(notes,'') || ? WHERE portfolio_id=?",
                    (f" [{_RETIRE_NOTE}]", pf.portfolio_id),
                )
                conn.commit()
            finally:
                conn.close()
            retired = pf.portfolio_id
    return retired


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="plan the ticks, fetch nothing")
    mode.add_argument("--execute", action="store_true", help="replay into a fresh book")
    ap.add_argument("--from", dest="start", default=None, help="first tick date (default: first call)")
    ap.add_argument("--step-hours", type=int, default=2)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    now = datetime.now(UTC)
    cfg = load_source_config()
    mandate = load_book_mandate()
    all_calls = [c for c in tracker.load_calls()
                 if c.instrument != "option" and c.is_levelled and c.verdict not in tracker.EXCLUDED
                 and _symbol(c)]
    all_calls.sort(key=lambda c: c.posted_at)
    if not all_calls:
        print("no levelled calls in the ledger — run the tracker tick first", file=sys.stderr)
        return 2

    start = (datetime.fromisoformat(args.start).replace(tzinfo=UTC) if args.start
             else all_calls[0].posted_at)
    start = start.replace(minute=0, second=0, microsecond=0)
    step = timedelta(hours=args.step_hours)

    # Tick only while there is something to do: a call inside the lookback,
    # or (checked as we go) an open position.
    active_windows = [(c.posted_at, c.posted_at + timedelta(days=cfg.lookback_days)) for c in all_calls]

    def call_in_window(t: datetime) -> bool:
        return any(a <= t <= b for a, b in active_windows)

    ticks = []
    t = start
    while t <= now:
        ticks.append(t)
        t += step
    busy = sum(1 for t in ticks if call_in_window(t))
    print(f"{len(all_calls)} calls from {all_calls[0].posted_at.date()} · "
          f"{len(ticks)} ticks of {args.step_hours}h from {start.date()} · "
          f"{busy} inside a call window")
    if not args.execute:
        print("dry run — pass --execute to fetch bars and replay into a fresh book")
        return 0

    print("fetching bars…", file=sys.stderr)
    bars = _load_bars(all_calls, now)
    priced = sum(1 for c in all_calls if bars.get(c.call_id))
    print(f"  {priced}/{len(all_calls)} calls have bars")
    tape = Tape(all_calls, bars)

    retired = _retire_existing_book()
    book = store.create_portfolio(
        name=BOOK_NAME, mandate=mandate,
        notes=(f"Copy-trader book on the {cfg.label} Trades channel, replayed from "
               f"{start.date()} over the ledger's history on hourly marks; live ticks continue from "
               f"{now.date()}."),
    )
    # `created_at` stays the real one. Backdating it to the replay start
    # read better on the page and quietly broke the whole paper layer:
    # `store.get_portfolio(None)` used to mean "oldest active book", so a
    # backdated book captured the signal book's own tick. The curve's
    # start date comes from the equity snapshots, which carry the replay
    # clock; the book's birthday is when it was opened.
    print(f"opened {book.portfolio_id}" + (f" (retired {retired})" if retired else ""))

    fills = 0
    ran = 0
    t0 = time.time()
    for i, t in enumerate(ticks):
        open_positions = store.load_positions(book.portfolio_id)
        if not open_positions and not call_in_window(t):
            continue
        visible = [tape.as_of(c, t) for c in all_calls if c.posted_at <= t]
        reads, _cov = book_candidates(
            mandate, now=t, config=cfg, calls=visible,
            mark_fn=lambda sym: tape.mark(sym, t),
        )

        def price_fn(tickers: list[str], _t=t):
            out = {}
            for tk in tickers:
                m = tape.mark(tk, _t)
                if m is not None:
                    out[tk.upper()] = {"price": m, "source": "replay", "as_of": _t.isoformat()}
            return out

        result = run_tick(book.portfolio_id, dry_run=False, now=t, price_fn=price_fn,
                          candidates=reads, valuations_in={})
        ran += 1
        if result.error:
            print(f"[{t.isoformat(timespec='minutes')}] tick error: {result.error}", file=sys.stderr)
            if result.retryable:
                time.sleep(5)
                continue
        n_fills = len(result.fills)
        fills += n_fills
        if n_fills and not args.quiet:
            for d in result.fills:
                print(f"[{t.isoformat(timespec='minutes')}] {d.action.value:5s} {d.ticker:8s} {d.headline}")
        if ran % 200 == 0:
            print(f"  … {i + 1}/{len(ticks)} ticks, {ran} run, {fills} fills, "
                  f"{time.time() - t0:.0f}s", file=sys.stderr)

    pf = store.get_portfolio(book.portfolio_id)
    print(f"\nreplay done — {ran} ticks run, {fills} fills, cash ${pf.cash:,.0f} "
          f"on ${pf.starting_equity:,.0f}; book {book.portfolio_id} is live for the launchd tick")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
