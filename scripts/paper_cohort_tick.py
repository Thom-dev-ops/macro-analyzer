#!/usr/bin/env python3
"""Run one tick of the COHORT book — the Feather Hands copy-trader.

The other book (`paper_trading_tick.py`) trades the desk's blended score.
This one trades the cohort's own calls: Big_Nuts, Feather Hands Trading,
MadDog31, joejoe55 and Market Traders, straight out of the `signals`
table, with the entry, stop and target the chart was posted with. Roster
and rules: `config/paper_cohort.json`.

Two books, one engine. Nothing here forks the engine's logic — the
cohort's calls are ranked into the same `RankRead` shape and injected
via `run_tick(candidates=...)`, so both equity curves are made by the
same exit rules, the same cash floor and the same fill model. Only the
candidate source and the mandate differ, which is the point: the
difference between the curves is the difference between the two ideas.

The composed valuation is switched OFF (`valuations_in={}`). Re-deciding
the target with the desk's structure map and trusted-voice consensus
would fold the desk's opinion back into a book whose whole purpose is to
measure the cohort without it — and the trusted-voice view would be
scoring these calls partly against themselves.

    # look, don't touch — reads live calls, writes nothing
    .venv/bin/python scripts/paper_cohort_tick.py --dry-run

    # open the book (idempotent — never creates a second one)
    .venv/bin/python scripts/paper_cohort_tick.py --bootstrap --execute

    # what the automated job runs
    .venv/bin/python scripts/paper_cohort_tick.py --execute

    # the funnel only: how much of the cohort's flow the book can see
    .venv/bin/python scripts/paper_cohort_tick.py --coverage
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import UTC, datetime                                    # noqa: E402

from macro_positioning.core.settings import settings                   # noqa: E402
from macro_positioning.db.schema import initialize_database            # noqa: E402
from macro_positioning.paper import store                              # noqa: E402
from macro_positioning.paper.cohort import (                           # noqa: E402
    BOOK_NAME,
    cohort_candidates,
    load_cohort_config,
    load_cohort_mandate,
)
from macro_positioning.paper.engine import run_tick                    # noqa: E402
from macro_positioning.paper.models import Portfolio                   # noqa: E402


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


def find_cohort_book(portfolio_id: str | None = None):
    """The cohort book, by id or by name.

    Deliberately NOT `store.get_portfolio(None)` — that returns the OLDEST
    active book, which is the signal book. A cohort tick that fell back to
    it would trade the wrong portfolio with the wrong mandate and the
    mistake would only show up in the P&L.
    """
    if portfolio_id:
        return store.get_portfolio(portfolio_id)
    for pf in store.list_portfolios():
        if pf.name == BOOK_NAME and pf.status == "active":
            return pf
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one tick of the cohort paper book.")
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
        help="open the cohort book if it does not exist yet (no-op if it does)",
    )
    ap.add_argument(
        "--coverage", action="store_true",
        help="print the candidate funnel and exit — no tick, no prices, no writes",
    )
    ap.add_argument("--portfolio", default=None, help="portfolio_id (defaults to the cohort book)")
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

    cfg = load_cohort_config()
    mandate = load_cohort_mandate()
    candidates, coverage = cohort_candidates(mandate)

    if args.coverage:
        print(coverage.report())
        for r in candidates[:15]:
            c = (getattr(r, "source_row", {}) or {}).get("cohort", {})
            print(f"  {r.value:5.1f}  {r.ticker:<6} {r.side:<5} {c.get('author', '')}")
        return 0

    _ensure_schema()

    book = find_cohort_book(args.portfolio)
    ephemeral = None
    if book is None:
        if dry_run:
            # Preview the opening tick before committing to a book. Held in
            # memory and never written, so --dry-run works before bootstrap.
            book = ephemeral = Portfolio(
                portfolio_id="pbook_cohort_preview",
                name=f"{BOOK_NAME} (preview)",
                starting_equity=mandate.starting_equity,
                cash=mandate.starting_equity,
                mandate=mandate,
                created_at=datetime.now(UTC).isoformat(),
            )
            print(
                f"No cohort book open yet — previewing the first tick against a "
                f"${mandate.starting_equity:,.0f} book held in memory. "
                "Run with --bootstrap --execute to open it for real.\n"
            )
        elif not args.bootstrap:
            print(
                "No cohort book exists. Re-run with --bootstrap to open one "
                "(rules come from config/paper_cohort.json).",
                file=sys.stderr,
            )
            return 2
        else:
            book = store.create_portfolio(
                name=BOOK_NAME,
                mandate=mandate,
                notes=(
                    f"Copy-trader book on the {cfg.label} cohort "
                    f"({', '.join(a.display for a in cfg.authors)}). "
                    "Candidates come from their own calls, not the desk score."
                ),
            )
            print(
                f"opened cohort book {book.portfolio_id} — "
                f"${book.starting_equity:,.0f} {book.base_currency}, "
                f"{book.mandate.min_cash_pct * 100:.0f}% cash floor, "
                f"{book.mandate.min_position_pct * 100:.1f}–"
                f"{book.mandate.max_position_pct * 100:.0f}% per position, "
                f"following {len(cfg.authors)} authors"
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
