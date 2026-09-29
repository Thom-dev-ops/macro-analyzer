"""Stock Unlocked tracker API — the read surface for /unlocked.

    GET  /api/stock-unlocked/summary     success rates, R, target reach, desk-vs-tape
    GET  /api/stock-unlocked/calls       the ledger (open first), live marks on open calls
    POST /api/stock-unlocked/sync        parse new posts + re-walk open calls

The ledger is written by `scripts/stock_unlocked_tracker_tick.py` on a
launchd schedule; `/sync` is the same pass on demand, for the button.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Query

from macro_positioning.tracker import stock_unlocked as tracker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock-unlocked", tags=["stock-unlocked"])


def _live_marks(calls: list[tracker.TrackedCall]) -> dict[str, dict]:
    """Best-effort spot for the open calls. A quote failure degrades the
    row to its last scored bar — it never 500s the page."""
    tickers = sorted({c.ticker for c in calls})
    if not tickers:
        return {}
    try:
        from macro_positioning.prices.spot import spot_prices

        return {k.upper(): v for k, v in spot_prices(tickers).items()}
    except Exception as exc:
        logger.warning("stock-unlocked marks failed: %s", exc)
        return {}


def _progress(c: tracker.TrackedCall, mark: Optional[float]) -> dict[str, Any]:
    """Where an open call sits between its stop and its next target,
    expressed the way the desk thinks about it: R from entry, and the
    share of the way to the next target."""
    if mark is None or not c.is_levelled:
        return {"mark": mark}
    sign = 1.0 if c.is_long else -1.0
    risk = abs(c.entry - c.stop) or None
    move = (mark - c.entry) * sign
    nxt = next((t for i, t in enumerate(c.targets, start=1) if i > c.max_target), None)
    return {
        "mark": mark,
        "move_pct": round(move / c.entry * 100, 2) if c.entry else None,
        "open_r": round(move / risk, 2) if risk else None,
        "next_target": nxt,
        "to_next_target_pct": (round((nxt - mark) * sign / mark * 100, 2) if nxt else None),
        "to_stop_pct": round((mark - c.stop) * sign / mark * 100, 2),
    }


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    return tracker.summary()


@router.get("/calls")
def get_calls(
    verdict: str = Query("all", pattern="^(all|open|resolved|options|unpriceable)$"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    calls = tracker.load_calls()
    if verdict == "open":
        calls = [c for c in calls if c.verdict == "open"]
    elif verdict == "resolved":
        calls = [c for c in calls if c.verdict in tracker.RESOLVED or c.verdict == "unresolved"]
    elif verdict == "options":
        calls = [c for c in calls if c.instrument == "option"]
    elif verdict == "unpriceable":
        calls = [c for c in calls if c.verdict == "unpriceable"]

    open_calls = [c for c in calls if c.verdict == "open"]
    marks = _live_marks(open_calls)
    out = []
    for c in calls[:limit]:
        d = c.to_api()
        if c.verdict == "open":
            q = marks.get(c.ticker.upper())
            mark = float(q["price"]) if q and q.get("price") else c.last_price
            d["progress"] = _progress(c, mark)
            d["mark_source"] = (q.get("source") if q else None) or "last_bar"
        out.append(d)
    return {"calls": out, "count": len(calls), "open": len(open_calls)}


@router.post("/sync")
def post_sync(rescore: bool = Query(False)) -> dict[str, Any]:
    result = tracker.sync(rescore=rescore)
    result["summary"] = tracker.summary()
    return result
