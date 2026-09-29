"""Stock Unlocked book API — the third paper book.

    GET  /api/paper/unlocked/coverage    the candidate funnel
    GET  /api/paper/unlocked/candidates  the channel's live calls, ranked
    POST /api/paper/unlocked/tick        run a tick (dry run by default)

Everything else comes from `paper_routes.py` with `portfolio_id=…`, and
`/api/paper/books` tags this book `kind: "unlocked"` so the UI can find it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from macro_positioning.paper import store
from macro_positioning.paper.engine import run_tick
from macro_positioning.paper.stock_unlocked_book import (
    BOOK_NAME,
    book_candidates,
    load_book_mandate,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paper/unlocked", tags=["paper"])


def _book():
    for pf in store.list_portfolios():
        if pf.name == BOOK_NAME and pf.status == "active":
            return pf
    return None


@router.get("/coverage")
def get_coverage() -> dict[str, Any]:
    try:
        _, coverage = book_candidates(load_book_mandate())
    except Exception as exc:
        logger.exception("unlocked coverage failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return coverage.as_dict()


@router.get("/candidates")
def get_candidates(limit: int = Query(40, ge=1, le=200)) -> dict[str, Any]:
    """What the book is looking at now, strongest first — including the
    rank-0 rows for calls the desk has closed, which are how a held
    position learns to leave."""
    try:
        reads, coverage = book_candidates(load_book_mandate())
    except Exception as exc:
        logger.exception("unlocked candidates failed")
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


@router.post("/tick")
def post_tick(dry_run: bool = True) -> dict[str, Any]:
    pf = _book()
    if pf is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No Stock Unlocked book exists yet. Run "
                "`scripts/paper_unlocked_tick.py --bootstrap --execute` to open one."
            ),
        )
    try:
        reads, coverage = book_candidates(pf.mandate)
        result = run_tick(pf.portfolio_id, dry_run=dry_run, candidates=reads, valuations_in={})
    except Exception as exc:
        logger.exception("unlocked tick failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    payload = result.as_dict()
    payload["coverage"] = coverage.as_dict()
    return payload
