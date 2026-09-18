#!/usr/bin/env python3
"""Run one tick of the paper book.

This is what launchd invokes (com.macro.paper-tick, 06:15 + 13:15 — a
quarter-hour behind the free-ingest scoring pass so the book always
trades the freshest reads). The full decision set is printed, so the
launchd log IS the audit trail: every fill, every hold, and every name
the mandate turned away, with the reason.

    # look, don't touch — reads live scores, writes nothing
    .venv/bin/python scripts/paper_trading_tick.py --dry-run

    # open the $50k book (idempotent — never creates a second one)
    .venv/bin/python scripts/paper_trading_tick.py --bootstrap --execute

    # what the automated job runs
    .venv/bin/python scripts/paper_trading_tick.py --execute
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import UTC, datetime                                 # noqa: E402

from macro_positioning.db.schema import initialize_database          # noqa: E402
from macro_positioning.core.settings import settings                  # noqa: E402
from macro_positioning.paper import store                             # noqa: E402
from macro_positioning.paper.models import Portfolio, load_mandate    # noqa: E402
from macro_positioning.paper.engine import run_tick                   # noqa: E402


def _ensure_schema() -> None:
    """Create the `paper_*` tables the first time this runs, and only then.

    `initialize_database()` is not free on the live DB — it re-runs the
    documents dedupe, which takes a write lock the Telegram listener is
    often holding. Once the paper tables exist there is nothing for it to
    do, so probe first and skip the whole thing on every subsequent tick.
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
        pass       # brand-new or unreadable file — let initialize_database decide
    initialize_database(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one paper-trading tick.")
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
        help="open the book if it does not exist yet (no-op if one is already open)",
    )
    ap.add_argument("--portfolio", default=None, help="portfolio_id (defaults to the active book)")
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
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    dry_run = not args.execute

    _ensure_schema()

    book = store.get_portfolio(args.portfolio)
    ephemeral = None
    if book is None:
        if dry_run:
            # Preview the opening tick before committing to a book. The
            # portfolio is built in memory and never written, so
            # `--dry-run` works on a virgin DB.
            book = ephemeral = Portfolio(
                portfolio_id="pbook_preview",
                name=f"{store.DEFAULT_BOOK_NAME} (preview)",
                starting_equity=load_mandate().starting_equity,
                cash=load_mandate().starting_equity,
                mandate=load_mandate(),
                created_at=datetime.now(UTC).isoformat(),
            )
            print(
                "No paper book open yet — previewing the first tick against a "
                f"${book.starting_equity:,.0f} book held in memory. "
                "Run with --bootstrap --execute to open it for real.\n"
            )
        elif not args.bootstrap:
            print(
                "No paper book exists. Re-run with --bootstrap to open one "
                "(starting equity comes from config/paper_trading.json).",
                file=sys.stderr,
            )
            return 2
        else:
            book = store.create_portfolio()
            print(
                f"opened paper book {book.portfolio_id} — "
                f"${book.starting_equity:,.0f} {book.base_currency}, "
                f"{book.mandate.min_cash_pct * 100:.0f}% cash floor, "
                f"{book.mandate.min_position_pct * 100:.0f}–"
                f"{book.mandate.max_position_pct * 100:.0f}% per position"
            )

    # The live DB has other writers; `alert_watch.py` runs hourly and holds
    # a write transaction through a long price pass. A blocked commit writes
    # nothing at all, so re-running the whole tick is safe — and losing a
    # tick to a collision is worse than waiting a couple of minutes for it.
    result = run_tick(book.portfolio_id, dry_run=dry_run, portfolio=ephemeral)
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
        result = run_tick(book.portfolio_id, dry_run=dry_run, portfolio=ephemeral)

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, default=str))
    elif not (args.quiet and not result.fills and not result.error):
        print(result.report())

    if result.error:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
