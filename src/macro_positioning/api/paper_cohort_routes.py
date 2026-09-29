"""Cohort-book API — the second paper book, and how to tell them apart.

    GET  /api/paper/books              every open book, tagged signal | cohort
    GET  /api/paper/cohort/coverage    the candidate funnel: what it can't see
    GET  /api/paper/cohort/candidates  the cohort's live calls, ranked
    GET  /api/paper/cohort/roster      who is in the cohort, and their record
    POST /api/paper/cohort/tick        run a cohort tick (dry run by default)

Everything else the cohort book needs is already served by
`paper_routes.py`: every endpoint there takes `portfolio_id`, so
`/api/paper/positions?portfolio_id=…` renders this book as readily as the
signal book. `/books` is what turns that parameter into a UI — it is the
only thing a front end needs in order to offer a switcher.

The one endpoint with no counterpart is `/coverage`. This book measures a
cohort whose flow it can only partly price: roughly 60% of the calls are
Solana memecoins nothing in the price stack can mark. A P&L without that
denominator next to it invites the wrong conclusion, so the funnel is a
first-class part of the read surface rather than a log line.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from macro_positioning.paper import store
from macro_positioning.paper.cohort import (
    BOOK_NAME,
    author_edge,
    cohort_candidates,
    load_cohort_config,
    load_cohort_mandate,
)
from macro_positioning.paper.engine import run_tick


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paper", tags=["paper"])


def _kind(name: Optional[str]) -> str:
    from macro_positioning.paper.stock_unlocked_book import BOOK_NAME as UNLOCKED_BOOK

    n = name or ""
    if n == BOOK_NAME:
        return "cohort"
    if n == UNLOCKED_BOOK:
        return "unlocked"
    return "signal"


def _cohort_book():
    """The cohort book by name — never `get_portfolio(None)`, which returns
    the oldest active book and would hand back the signal book."""
    for pf in store.list_portfolios():
        if pf.name == BOOK_NAME and pf.status == "active":
            return pf
    return None


@router.get("/books")
def get_books() -> dict[str, Any]:
    """Every book the desk runs, so a UI can offer the switch.

    `kind` is what the caller actually needs: the two books answer
    different questions and should never be summed into one number.
    """
    books = []
    for pf in store.list_portfolios():
        realized = store.realized_to_date(pf.portfolio_id)
        books.append({
            "portfolioId": pf.portfolio_id,
            "name": pf.name,
            "kind": _kind(pf.name),
            "status": pf.status,
            "baseCurrency": pf.base_currency,
            "startingEquity": pf.starting_equity,
            "cash": pf.cash,
            "realizedToDate": round(realized, 2),
            "createdAt": pf.created_at,
            "lastTickAt": pf.last_tick_at,
            "notes": pf.notes,
        })
    return {
        "books": books,
        "kinds": {
            "signal": "the desk's blended score, technical-agent levels, composed target",
            "cohort": "the Feather Hands crowd's own calls, taken at face value",
            "unlocked": "the Stock Unlocked channel's stated calls, out of the tracker ledger",
        },
    }


@router.get("/cohort/coverage")
def get_coverage(window_days: Optional[int] = Query(None, ge=1, le=120)) -> dict[str, Any]:
    """The candidate funnel — every call the window held, and where each went.

    This is the book's denominator. `pricedPct` is the headline: it says
    what fraction of the cohort's directional flow the book is able to
    measure at all.
    """
    import dataclasses

    cfg = load_cohort_config()
    if window_days:
        cfg = dataclasses.replace(cfg, lookback_days=window_days)
    try:
        _, coverage = cohort_candidates(load_cohort_mandate(), config=cfg)
    except Exception as exc:
        logger.exception("cohort coverage failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return coverage.as_dict()


@router.get("/cohort/candidates")
def get_candidates(limit: int = Query(40, ge=1, le=200)) -> dict[str, Any]:
    """What the cohort book is looking at right now, strongest first.

    Each row carries the rank components AND the call behind it — author,
    channel, chart timeframe, the levels as drawn, and the reward:risk
    both as drawn and at the current mark. A row is explainable without
    another request.
    """
    try:
        reads, coverage = cohort_candidates(load_cohort_mandate())
    except Exception as exc:
        logger.exception("cohort candidates failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    out = []
    for r in reads[:limit]:
        row = getattr(r, "source_row", {}) or {}
        d = r.as_dict()
        d["call"] = row.get("cohort")
        d["levels"] = {
            "entry": row.get("entry"), "stop": row.get("stop"),
            "target": row.get("target"), "rr": row.get("rr"),
            "setup": row.get("setup"),
        }
        out.append(d)
    return {"candidates": out, "coverage": coverage.as_dict()}


@router.get("/cohort/roster")
def get_roster() -> dict[str, Any]:
    """Who the book follows, and what the backtest measured on each.

    `alphaPct` is market-relative — the call's return minus the market's
    over the same window. A win rate would mostly be measuring the tape.
    `counts` is null where the backtest has too thin a sample to have an
    opinion; the rank model gates on the same number.
    """
    cfg = load_cohort_config()
    edges = author_edge(cfg.author_ids)
    return {
        "label": cfg.label,
        "lookbackDays": cfg.lookback_days,
        "callTypes": list(cfg.call_types),
        "requireTrigger": cfg.require_trigger,
        "minLiveRr": cfg.min_live_rr,
        "authors": [
            {
                "authorId": a.author_id,
                "display": a.display,
                "scoredCalls": (edges.get(a.author_id) or {}).get("n"),
                "alphaPct": (edges.get(a.author_id) or {}).get("alpha"),
                "backtestLastRun": (edges.get(a.author_id) or {}).get("lastScored"),
            }
            for a in cfg.authors
        ],
    }


@router.post("/cohort/tick")
def post_tick(dry_run: bool = True) -> dict[str, Any]:
    """Run a cohort tick. Dry by default — the same contract as
    `/api/paper/tick`, so the button behaves the same on both books.

    `valuations_in={}` switches the composed view off: this book takes the
    call as given rather than re-deciding it with the desk's machinery.
    """
    pf = _cohort_book()
    if pf is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No cohort book exists yet. Run "
                "`scripts/paper_cohort_tick.py --bootstrap --execute` to open one."
            ),
        )
    try:
        reads, coverage = cohort_candidates(pf.mandate)
        result = run_tick(
            pf.portfolio_id, dry_run=dry_run, candidates=reads, valuations_in={}
        )
    except Exception as exc:
        logger.exception("cohort tick failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    payload = result.as_dict()
    payload["coverage"] = coverage.as_dict()
    return payload
