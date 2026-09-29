#!/usr/bin/env python3
"""Run one tick of the STOCK UNLOCKED book — the copy-trader on the
Stock Unlocked Trades channel.

Third book, same engine. `paper_trading_tick.py` trades the desk's
blended score; `paper_cohort_tick.py` trades the Feather Hands crowd's
chart calls; this one trades the Stock Unlocked channel's stated calls
straight out of the tracker ledger (`stock_unlocked_calls`), with the
entry, stop and targets the desk typed. Rules: `config/paper_stock_unlocked.json`.

The candidate source also ranks every call the desk (or the tape) has
closed at 0, so the engine's rank-decay exit follows the desk out of a
name as well as in. The composed valuation is OFF (`valuations_in={}`).

    .venv/bin/python scripts/paper_unlocked_tick.py --dry-run
    .venv/bin/python scripts/paper_unlocked_tick.py --bootstrap --execute
    .venv/bin/python scripts/paper_unlocked_tick.py --execute       # the launchd job
    .venv/bin/python scripts/paper_unlocked_tick.py --coverage
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import UTC, datetime                                    # noqa: E402

from macro_positioning.core.settings import settings                   # noqa: E402
from macro_positioning.db.schema import initialize_database            # noqa: E402
from macro_positioning.paper import store                              # noqa: E402
from macro_positioning.tracker import stock_unlocked as tracker        # noqa: E402
from macro_positioning.paper.stock_unlocked_book import (              # noqa: E402
    BOOK_NAME,
    book_candidates,
    load_book_mandate,
    load_source_config,
)
from macro_positioning.paper.engine import run_tick                    # noqa: E402
from macro_positioning.paper.models import Portfolio                   # noqa: E402


# One tick at a time, across processes. This job now has two callers —
# launchd every two hours, and the Telegram listener the moment the desk
# posts — and two overlapping passes would both read the same positions
# and both decide to open the same name. Non-blocking: if a pass is
# already running, the other one has nothing to add and stands down.
_LOCK_PATH = Path(tempfile.gettempdir()) / "macro-paper-unlocked-tick.lock"


@contextlib.contextmanager
def _tick_lock():
    fh = open(_LOCK_PATH, "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def _ensure_schema() -> None:
    """Create the `paper_*` tables the first time, and only then.

    Same reasoning as the signal book's tick: `initialize_database()` is
    not free on the live DB (it re-runs the documents dedupe, which takes
    a write lock the Telegram listener is often holding), so probe first
    and skip it on every subsequent run.
    """
    import sqlite3

    path = settings.sqlite_path
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10) as probe:
            found = probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_portfolios'"
            ).fetchone()
        if found:
            return
    except sqlite3.DatabaseError:
        pass
    initialize_database(path)


def find_unlocked_book(portfolio_id: str | None = None):
    """The Stock Unlocked book, by id or by name — never
    `store.get_portfolio(None)`, which returns the oldest active book."""
    if portfolio_id:
        return store.get_portfolio(portfolio_id)
    for pf in store.list_portfolios():
        if pf.name == BOOK_NAME and pf.status == "active":
            return pf
    return None


def main() -> int:
    """Take the cross-process lock, then run the tick."""
    with _tick_lock() as acquired:
        if not acquired:
            print("another unlocked tick is already running — standing down", file=sys.stderr)
            return 0
        return _run()


def _run() -> int:
    ap = argparse.ArgumentParser(description="Run one tick of the Stock Unlocked paper book.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true",
        help="compute the decision set and print it; write nothing (default)",
    )
    mode.add_argument(
        "--execute", action="store_true",
        help="commit the tick — fills, decisions and an equity snapshot are written",
    )
    ap.add_argument(
        "--bootstrap", action="store_true",
        help="open the book if it does not exist yet (no-op if it does)",
    )
    ap.add_argument(
        "--coverage", action="store_true",
        help="print the candidate funnel and exit — no tick, no prices, no writes",
    )
    ap.add_argument("--portfolio", default=None, help="portfolio_id (defaults to the Stock Unlocked book)")
    ap.add_argument("--json", action="store_true", help="emit the full result as JSON")
    ap.add_argument(
        "--retries", type=int, default=5,
        help="attempts when the commit is blocked by another writer (default 5)",
    )
    ap.add_argument(
        "--retry-wait", type=int, default=30,
        help="seconds before the first retry; backs off linearly (default 30)",
    )
    ap.add_argument("--quiet", action="store_true", help="only print on action or error")
    ap.add_argument("--no-sync", action="store_true",
                    help="screen the ledger as it stands; do not sync the tracker first")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    dry_run = not args.execute

    cfg = load_source_config()
    mandate = load_book_mandate()

    # Sync the ledger FIRST. The tracker has its own launchd job, but the
    # two intervals are independent and unordered — a book tick that fired
    # between a post and the next tracker pass screened a ledger that did
    # not contain the call yet. Sync here and the only latency left is this
    # job's own interval.
    if not args.no_sync:
        try:
            synced = tracker.sync()
            if synced.get("new"):
                print(f"tracker: {synced['new']} new call(s), {synced['scored']} scored")
        except Exception as exc:  # noqa: BLE001 — a stale ledger still ticks
            print(f"tracker sync failed ({exc}) — screening the ledger as it stands",
                  file=sys.stderr)

    candidates, coverage = book_candidates(mandate)

    if args.coverage:
        print(coverage.report())
        for r in candidates[:15]:
            c = (getattr(r, "source_row", {}) or {}).get("cohort", {})
            print(f"  {r.value:5.1f}  {r.ticker:<10} {r.side:<5} {c.get('timeframe', '')} "
                  f"desk={c.get('deskVerdict')} tape={c.get('tapeVerdict')}")
        return 0

    _ensure_schema()

    book = find_unlocked_book(args.portfolio)
    ephemeral = None
    if book is None:
        if dry_run:
            # Preview the opening tick before committing to a book. Held in
            # memory and never written, so --dry-run works before bootstrap.
            book = ephemeral = Portfolio(
                portfolio_id="pbook_unlocked_preview",
                name=f"{BOOK_NAME} (preview)",
                starting_equity=mandate.starting_equity,
                cash=mandate.starting_equity,
                mandate=mandate,
                created_at=datetime.now(UTC).isoformat(),
            )
            print(
                f"No Stock Unlocked book open yet — previewing the first tick against a "
                f"${mandate.starting_equity:,.0f} book held in memory. "
                "Run with --bootstrap --execute to open it for real.\n"
            )
        elif not args.bootstrap:
            print(
                "No Stock Unlocked book exists. Re-run with --bootstrap to open one "
                "(rules come from config/paper_stock_unlocked.json).",
                file=sys.stderr,
            )
            return 2
        else:
            book = store.create_portfolio(
                name=BOOK_NAME,
                mandate=mandate,
                notes=(
                    f"Copy-trader book on the {cfg.label} Trades channel. "
                    "Candidates come from the tracker ledger's stated calls, "
                    "not the desk score."
                ),
            )
            print(
                f"opened Stock Unlocked book {book.portfolio_id} — "
                f"${book.starting_equity:,.0f} {book.base_currency}, "
                f"{book.mandate.min_cash_pct * 100:.0f}% cash floor, "
                f"{book.mandate.min_position_pct * 100:.1f}–"
                f"{book.mandate.max_position_pct * 100:.0f}% per position, "
                f"{cfg.lookback_days}d lookback"
            )

    # `valuations_in={}` switches the composed view off — see the module
    # docstring. Empty dict, not None: None means "go compute it".
    def tick():
        return run_tick(
            book.portfolio_id, dry_run=dry_run, portfolio=ephemeral,
            candidates=candidates, valuations_in={},
        )

    result = tick()
    attempt = 1
    while result.retryable and attempt < args.retries:
        wait = args.retry_wait * attempt
        print(
            f"commit blocked by another writer (attempt {attempt}/{args.retries}); "
            f"retrying in {wait}s — nothing was written",
            file=sys.stderr,
        )
        time.sleep(wait)
        attempt += 1
        result = tick()

    if args.json:
        payload = result.as_dict()
        payload["coverage"] = coverage.as_dict()
        print(json.dumps(payload, indent=2, default=str))
    elif not (args.quiet and not result.fills and not result.error):
        print(result.report())
        # The coverage funnel goes UNDER the decision set on purpose: "3
        # fills" means nothing without "out of 41 candidates screened from
        # 512 calls, 61% of which nothing can price".
        print()
        print(coverage.report())

    if result.error:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
