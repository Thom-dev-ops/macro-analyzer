"""Paper-book API — the read surface for /paper, plus the manual controls.

    GET  /api/paper/portfolio      book header: equity, cash, P&L, mandate
    GET  /api/paper/positions      open (or closed) book with live marks
    GET  /api/paper/decisions      THE decision log — fills, holds, refusals
    GET  /api/paper/orders         the fill blotter
    GET  /api/paper/equity-curve   snapshot series
    GET  /api/paper/candidates     what the book is looking at right now
    POST /api/paper/tick           run a tick (dry_run=true by default)
    POST /api/paper/positions/{id}/close   desk override

`/decisions` is the point of the whole layer: every row carries the
action, the intent, the rank components that produced it, and — for
a refusal — the blocker and the constraint state at the time. The UI
renders it; nothing has to be re-derived.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from macro_positioning.paper import store
from macro_positioning.paper.rank import load_candidates
from macro_positioning.paper.engine import close_position_manually, run_tick
from macro_positioning.paper.models import Position
from macro_positioning.paper.performance import performance
from macro_positioning.paper.sleeves import all_sleeves, classes
from macro_positioning.paper.vocabulary import Action, Blocker, Intent


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paper", tags=["paper"])


def _book_or_404(portfolio_id: Optional[str] = None):
    pf = store.get_portfolio(portfolio_id)
    if pf is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No paper book exists yet. Run "
                "`scripts/paper_trading_tick.py --bootstrap --execute` to open one."
            ),
        )
    return pf


def _mark_positions(positions: list[Position]) -> dict[str, dict]:
    """Best-effort live marks. A price failure must degrade the view, not
    500 it — the position's last stored mark carries the display."""
    if not positions:
        return {}
    try:
        from macro_positioning.prices.spot import spot_prices

        return {k.upper(): v for k, v in spot_prices([p.ticker for p in positions]).items()}
    except Exception as exc:
        logger.warning("paper marks failed, falling back to stored: %s", exc)
        return {}


@router.get("/portfolio")
def get_book(portfolio_id: Optional[str] = None) -> dict[str, Any]:
    pf = _book_or_404(portfolio_id)
    positions = store.load_positions(pf.portfolio_id)
    marks = _mark_positions(positions)

    equity = pf.cash
    deployed = 0.0
    unrealized = 0.0
    for p in positions:
        q = marks.get(p.ticker.upper())
        mark = float(q["price"]) if q and q.get("price") else (p.last_mark or p.avg_price)
        deployed += p.exposure(mark)
        equity += p.exposure(mark) * (1.0 if p.is_long else -1.0)
        unrealized += p.unrealized(mark)

    realized = store.realized_to_date(pf.portfolio_id)
    curve = store.equity_curve(pf.portfolio_id, limit=400)
    prev_equity = curve[-2]["equity"] if len(curve) >= 2 else pf.starting_equity

    return {
        **pf.as_dict(),
        "equity": round(equity, 2),
        "deployed": round(deployed, 2),
        "deployedPct": round(deployed / equity * 100, 2) if equity > 0 else 0.0,
        "cashPct": round(pf.cash / equity * 100, 2) if equity > 0 else 100.0,
        "minCashPct": round(pf.mandate.min_cash_pct * 100, 2),
        "headroomUsd": round(max(0.0, equity * pf.mandate.max_deployed_pct - deployed), 2),
        "openPositions": len(positions),
        "unrealizedPnl": round(unrealized, 2),
        "realizedPnl": round(realized, 2),
        "totalPnl": round(equity - pf.starting_equity, 2),
        "totalPnlPct": round((equity / pf.starting_equity - 1) * 100, 2),
        "sinceLastTickPnl": round(equity - prev_equity, 2),
    }


@router.get("/positions")
def get_positions(
    portfolio_id: Optional[str] = None,
    status: str = Query("open", pattern="^(open|closed|all)$"),
) -> dict[str, Any]:
    pf = _book_or_404(portfolio_id)
    positions = store.load_positions(
        pf.portfolio_id, status=None if status == "all" else status
    )
    marks = _mark_positions([p for p in positions if p.status == "open"])
    equity = pf.cash + sum(
        p.exposure(
            float(marks[p.ticker.upper()]["price"])
            if marks.get(p.ticker.upper(), {}).get("price")
            else p.last_mark
        )
        * (1.0 if p.is_long else -1.0)
        for p in positions
        if p.status == "open"
    )

    out = []
    for p in positions:
        q = marks.get(p.ticker.upper())
        mark = float(q["price"]) if q and q.get("price") else p.last_mark
        d = p.as_dict(mark)
        d["weightPct"] = (
            round(p.exposure(mark) / equity * 100, 2) if equity > 0 and p.status == "open" else 0.0
        )
        d["markSource"] = (q or {}).get("source") or "stored"
        # The story of a position is the gap between these two numbers.
        d["rankDrift"] = (
            round((p.rank_now or 0) - (p.rank_at_entry or 0), 2)
            if p.rank_at_entry is not None else None
        )
        out.append(d)
    return {"positions": out, "equity": round(equity, 2), "count": len(out)}


def _gloss(row: dict) -> Optional[str]:
    """Render an Intent's sentence from the decision row's own fields."""
    try:
        intent = Intent(row.get("intent"))
    except ValueError:
        return None
    text = intent.describe(
        ticker=row.get("ticker") or "",
        side=row.get("side") or "",
        rank=row.get("rank") or 0.0,
        rank_prev=row.get("rank_prev") or 0.0,
        target_weight_pct=row.get("target_weight_pct") or 0.0,
        weight_pct=row.get("current_weight_pct") or 0.0,
        from_pct=row.get("current_weight_pct") or 0.0,
        # `rationale.rank` is present-but-null on a HOLD row for a name that
        # had no read this tick, so the key lookup alone is not enough.
        score=((row.get("rationale") or {}).get("rank") or {}).get("score") or "",
    )
    return None if "{" in text else text


@router.get("/decisions")
def get_decisions(
    portfolio_id: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    ticker: Optional[str] = None,
    action: Optional[str] = None,
    executed_only: bool = False,
    position_id: Optional[str] = None,
) -> dict[str, Any]:
    """The decision log. Every row explains itself.

    `position_id` narrows the log to one position's whole life — the OPEN,
    every ADD and TRIM, and the EXIT. The log's own view is filtered to a
    single action at a time, which means a trade's arc is never visible in
    it: a half taken off at target and the stop-out that followed sit
    under different chips and, on a busy book, thousands of rows apart.
    """
    pf = _book_or_404(portfolio_id)
    rows = store.recent_decisions(
        pf.portfolio_id, limit=limit, ticker=ticker, action=action,
        executed_only=executed_only, position_id=position_id,
    )
    for r in rows:
        # Attach the vocabulary's own gloss, rendered against this row's
        # own numbers, so the UI never hard-codes a mapping from
        # `conviction_decay` to English. A template that still has unfilled
        # slots is dropped rather than shown half-rendered.
        r["intentMeaning"] = _gloss(r)
        r["isFill"] = bool(r["executed"])
    if position_id:
        # An arc is read for its prices — "trimmed half" means little
        # without the level it was trimmed at — so the fill rides along
        # rather than costing the caller a second round trip.
        fills = {
            o["order_id"]: o
            for o in store.recent_orders(
                pf.portfolio_id, limit=200, position_id=position_id,
            )
        }
        for r in rows:
            o = fills.get(r.get("order_id"))
            if o:
                r["fill"] = {
                    "qty": o["qty"], "price": o["price"],
                    "notional": o["notional"], "realizedPnl": o["realized_pnl"],
                }
    return {"decisions": rows, "count": len(rows)}


@router.get("/orders")
def get_orders(
    portfolio_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    ticker: Optional[str] = None,
) -> dict[str, Any]:
    pf = _book_or_404(portfolio_id)
    return {"orders": store.recent_orders(pf.portfolio_id, limit=limit, ticker=ticker)}


@router.get("/equity-curve")
def get_equity_curve(
    portfolio_id: Optional[str] = None, limit: int = Query(400, ge=2, le=2000),
    daily: bool = Query(False),
) -> dict[str, Any]:
    pf = _book_or_404(portfolio_id)
    return {
        "startingEquity": pf.starting_equity,
        "points": store.equity_curve(pf.portfolio_id, limit=limit, daily=daily),
    }


@router.get("/candidates")
def get_candidates(
    portfolio_id: Optional[str] = None, limit: int = Query(30, ge=1, le=200)
) -> dict[str, Any]:
    """What the book is looking at right now, ranked. Renders the queue
    between ticks — including names sitting just under the entry bar."""
    pf = _book_or_404(portfolio_id)
    reads = load_candidates(pf.mandate)
    held = {p.ticker.upper() for p in store.load_positions(pf.portfolio_id)}
    out = []
    for r in reads[:limit]:
        d = r.as_dict()
        d["held"] = r.ticker.upper() in held
        d["clearsEntryFloor"] = r.tradeable and r.value >= pf.mandate.entry_floor
        out.append(d)
    return {
        "candidates": out,
        "entryFloor": pf.mandate.entry_floor,
        "totalScored": len(reads),
    }


@router.get("/performance")
def get_performance(portfolio_id: Optional[str] = None) -> dict[str, Any]:
    """Attribution: what the book made, how much is banked, and where it
    came from — by sleeve, class, regime, side and exit reason.

    Marks are fetched once here and handed to the performance layer, so
    rendering this page does not hit the price providers twice.
    """
    pf = _book_or_404(portfolio_id)
    open_positions = store.load_positions(pf.portfolio_id)
    marks = _mark_positions(open_positions)

    equity = pf.cash
    for p in open_positions:
        q = marks.get(p.ticker.upper())
        mark = float(q["price"]) if q and q.get("price") else (p.last_mark or p.avg_price)
        equity += p.exposure(mark) * (1.0 if p.is_long else -1.0)

    return performance(
        pf.portfolio_id,
        marks=marks,
        equity=equity,
        cash=pf.cash,
        starting_equity=pf.starting_equity,
    )


@router.get("/sleeves")
def get_sleeves() -> dict[str, Any]:
    """The attribution taxonomy itself — sleeve, the class it rolls into,
    and the regimes it expresses. Reporting only; it never gates a fill."""
    return {
        "classes": classes(),
        "sleeves": [s.as_dict() for s in all_sleeves()],
    }


@router.get("/reconcile")
def get_reconciliation(portfolio_id: Optional[str] = None) -> dict[str, Any]:
    """Cash rebuilt from the fill log vs the cached balance. `ok: false`
    means a bug in the engine, not a market event."""
    pf = _book_or_404(portfolio_id)
    return store.reconcile(pf.portfolio_id)


@router.post("/tick")
def post_tick(
    portfolio_id: Optional[str] = None, dry_run: bool = True
) -> dict[str, Any]:
    """Run a tick on demand. Defaults to a dry run — the launchd job is
    what trades the book; this button is for looking before leaping."""
    pf = _book_or_404(portfolio_id)
    result = run_tick(pf.portfolio_id, dry_run=dry_run)
    return {**result.as_dict(), "report": result.report()}


@router.post("/positions/{position_id}/close")
def post_close(position_id: str, note: Optional[str] = None) -> dict[str, Any]:
    d = close_position_manually(position_id, note=note)
    if d is None:
        raise HTTPException(
            status_code=404,
            detail="position not found, already closed, or unpriceable right now",
        )
    return {"decision": d.as_dict()}


@router.get("/learning")
def get_learning(portfolio_id: Optional[str] = None) -> dict[str, Any]:
    """The recursive loop's current output: per-sleeve record, shrink
    weight, and the exact rank / size / hold adjustments the next tick
    will apply. `bySleeve[...].promotable` is the gate for feeding a
    sleeve into the shared score — n >= min_n in v1."""
    from macro_positioning.paper.learning import adjustments

    pf = _book_or_404(portfolio_id)
    return adjustments(pf.portfolio_id, mandate=pf.mandate).as_dict()


@router.get("/learning/sleeve/{ticker}")
def get_learning_for_ticker(ticker: str, portfolio_id: Optional[str] = None) -> dict[str, Any]:
    """The book's record on the sleeve a ticker belongs to — for the
    read-only badge on /concepts and the asset page."""
    from macro_positioning.paper.learning import adjustments
    from macro_positioning.paper.sleeves import sleeve_for_ticker

    pf = _book_or_404(portfolio_id)
    sl = sleeve_for_ticker(ticker)
    rec = adjustments(pf.portfolio_id, mandate=pf.mandate).by_sleeve.get(sl.id)
    return {"ticker": ticker.upper(), "sleeveId": sl.id, "sleeveLabel": sl.label,
            "record": rec.as_dict() if rec else None}


@router.get("/vocabulary")
def get_vocabulary() -> dict[str, Any]:
    """The semantic layer itself, as data.

    The SPA renders decision rows from these definitions rather than
    keeping a parallel copy of the wording, so a term added here shows up
    in the UI without a front-end change.
    """
    return {
        "actions": {a.value: a.template for a in Action},
        "intents": {i.value: i.template for i in Intent},
        "blockers": {b.value: b.template for b in Blocker},
    }
