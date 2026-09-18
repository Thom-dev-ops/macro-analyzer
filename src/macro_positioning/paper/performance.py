"""Performance attribution for the paper book.

Answers three questions the raw decision log cannot:

  1. **How much did it make, and how much of that is actually banked?**
     Open P&L is an opinion; realized P&L is a fact. Both are reported,
     plus `bookedShare` — the fraction of total gains that has been taken
     off the table. A book that is +8% entirely on paper is a different
     book from one that is +8% with half of it realized.

  2. **Where did it come from?** Sliced by sleeve (Agriculture, Copper,
     Crypto, Defense…), by class (Hard assets / Equities / Crypto /
     Rates & FX), and by regime expression. Taxonomy lives in
     `paper/sleeves.py`.

  3. **Is the edge real?** Win rate, profit factor, expectancy, average
     R, and — the one that actually tunes the mandate — a breakdown by
     EXIT REASON. If `stop_hit` is deeply negative while `target_hit` is
     barely positive, the stops are too tight, and no amount of staring
     at a win rate will tell you that.

Everything is computed from `paper_orders`, the immutable fill log — not
from the positions table, whose `avg_price` mutates as a position is
added to. A closed position's numbers are therefore reproducible from
the fills that made it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Optional

from macro_positioning.paper import store
from macro_positioning.paper.models import Position
from macro_positioning.paper.sleeves import (
    UNCLASSIFIED,
    classes,
    coverage,
    sleeve_for_ticker,
)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _safe_div(a: float, b: float) -> Optional[float]:
    return (a / b) if b else None


def _pct(a: float, b: float, digits: int = 2) -> float:
    return round(a / b * 100, digits) if b else 0.0


# ── One round trip ────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    """A position reduced to the numbers that judge it.

    `cost_in` is the capital the book actually committed (sum of entry
    notionals), so `realized_pct` is a return on the money at risk — not
    on book equity, which would make every position look tiny.
    """

    position_id: str
    ticker: str
    side: str
    status: str
    sleeve_id: str
    sleeve_label: str
    class_id: str
    class_label: str
    regimes: tuple[str, ...]
    opened_at: Optional[str]
    closed_at: Optional[str]
    qty_in: float
    cost_in: float
    avg_entry: float
    realized: float
    fees: float
    exit_intent: Optional[str]
    stop: Optional[float]
    initial_risk: Optional[float]       # per-unit risk banked at entry
    rank_at_entry: Optional[float]
    rank_now: Optional[float]
    unrealized: float = 0.0
    open_exposure: float = 0.0
    mark: Optional[float] = None

    @property
    def realized_pct(self) -> float:
        return _pct(self.realized, self.cost_in)

    @property
    def unrealized_pct(self) -> float:
        return _pct(self.unrealized, self.cost_in)

    @property
    def total_pnl(self) -> float:
        return self.realized + self.unrealized

    @property
    def total_pct(self) -> float:
        return _pct(self.total_pnl, self.cost_in)

    @property
    def r_multiple(self) -> Optional[float]:
        """Realized P&L in units of the risk taken at ENTRY.

        Must be the banked `initial_risk`, not the live stop: the target
        trim moves the stop to breakeven, after which |entry − stop| is
        ~0 and this would report R in the billions (it did — NEAR, INJ,
        PLTR and GDXJ all showed R ≈ 1e11 for exactly that reason). The
        live stop is only a fallback for positions that predate banking,
        and a degenerate risk returns None rather than a number nobody
        should believe.
        """
        if not self.qty_in or not self.avg_entry:
            return None
        per_unit = self.initial_risk
        if not per_unit or per_unit <= 0:
            per_unit = abs(self.avg_entry - self.stop) if self.stop else 0.0
        # Under 0.05% of entry the "risk" is rounding, not a stop.
        if per_unit <= self.avg_entry * 0.0005:
            return None
        return round(self.realized / (per_unit * self.qty_in), 3)

    @property
    def hold_days(self) -> Optional[float]:
        a, b = _parse(self.opened_at), _parse(self.closed_at) or datetime.now(UTC)
        return round((b - a).total_seconds() / 86400, 2) if a else None

    @property
    def is_closed(self) -> bool:
        return self.status == "closed"

    def as_dict(self) -> dict:
        return {
            "positionId": self.position_id,
            "ticker": self.ticker,
            "side": self.side,
            "status": self.status,
            "sleeve": self.sleeve_label,
            "sleeveId": self.sleeve_id,
            "class": self.class_label,
            "classId": self.class_id,
            "regimes": list(self.regimes),
            "openedAt": self.opened_at,
            "closedAt": self.closed_at,
            "holdDays": self.hold_days,
            "costIn": round(self.cost_in, 2),
            "avgEntry": round(self.avg_entry, 6),
            "mark": self.mark,
            "realized": round(self.realized, 2),
            "realizedPct": self.realized_pct,
            "unrealized": round(self.unrealized, 2),
            "unrealizedPct": self.unrealized_pct,
            "totalPnl": round(self.total_pnl, 2),
            "totalPct": self.total_pct,
            "rMultiple": self.r_multiple,
            "fees": round(self.fees, 2),
            "exitIntent": self.exit_intent,
            "rankAtEntry": self.rank_at_entry,
            "rankNow": self.rank_now,
        }


def build_trade_records(
    portfolio_id: str,
    *,
    db_path: Optional[Path] = None,
    marks: Optional[dict[str, dict]] = None,
) -> list[TradeRecord]:
    """Reduce every position — open and closed — to a TradeRecord.

    Entry legs (OPEN/ADD) give the capital committed and the true average
    entry; exit legs (TRIM/EXIT) give realized P&L. Reading the fill log
    rather than the position row means a partially-trimmed position still
    reports the return on what it actually risked.
    """
    positions = store.load_positions(portfolio_id, status=None, db_path=db_path)
    orders = store.recent_orders(portfolio_id, limit=100_000, db_path=db_path)
    by_position: dict[str, list[dict]] = {}
    for o in orders:
        by_position.setdefault(o.get("position_id") or "", []).append(o)

    marks = {k.upper(): v for k, v in (marks or {}).items()}
    records: list[TradeRecord] = []

    for p in positions:
        legs = sorted(by_position.get(p.position_id, []), key=lambda o: o["filled_at"] or "")
        qty_in = cost_in = realized = fees = 0.0
        exit_intent: Optional[str] = None
        for leg in legs:
            action = (leg.get("action") or "").upper()
            fees += float(leg.get("fees") or 0.0)
            if action in ("OPEN", "ADD"):
                qty_in += float(leg.get("qty") or 0.0)
                cost_in += abs(float(leg.get("notional") or 0.0))
            else:
                realized += float(leg.get("realized_pnl") or 0.0)
                exit_intent = leg.get("intent") or exit_intent

        q = marks.get(p.ticker.upper())
        mark = float(q["price"]) if q and q.get("price") else p.last_mark
        sleeve = sleeve_for_ticker(p.ticker)

        records.append(
            TradeRecord(
                position_id=p.position_id,
                ticker=p.ticker,
                side=p.side,
                status=p.status,
                sleeve_id=sleeve.id,
                sleeve_label=sleeve.label,
                class_id=sleeve.class_id,
                class_label=sleeve.class_label,
                regimes=sleeve.regimes,
                opened_at=p.opened_at,
                closed_at=p.closed_at,
                qty_in=qty_in,
                cost_in=cost_in,
                avg_entry=(cost_in / qty_in) if qty_in else p.avg_price,
                realized=realized - fees,
                fees=fees,
                exit_intent=exit_intent,
                stop=p.stop,
                initial_risk=getattr(p, "initial_risk", None),
                rank_at_entry=p.rank_at_entry,
                rank_now=p.rank_now,
                unrealized=p.unrealized(mark) if p.status == "open" else 0.0,
                open_exposure=p.exposure(mark) if p.status == "open" else 0.0,
                mark=round(mark, 6) if mark else None,
            )
        )
    return records


# ── Aggregation ───────────────────────────────────────────────────────


def summarize(records: Iterable[TradeRecord], *, equity: float = 0.0) -> dict:
    """Win/loss, profit factor, expectancy and exposure for a set of trades.

    Closed trades drive the win/loss statistics — an open position has no
    verdict yet, and counting it as one is how a losing book flatters
    itself. Open positions still contribute exposure and unrealized P&L,
    reported alongside but never mixed in.
    """
    recs = list(records)
    closed = [r for r in recs if r.is_closed]
    open_ = [r for r in recs if not r.is_closed]

    wins = [r for r in closed if r.realized > 0]
    losses = [r for r in closed if r.realized < 0]
    scratches = [r for r in closed if r.realized == 0]

    gross_profit = sum(r.realized for r in wins)
    gross_loss = abs(sum(r.realized for r in losses))
    realized = sum(r.realized for r in recs)          # includes partial trims on open rows
    unrealized = sum(r.unrealized for r in open_)
    exposure = sum(r.open_exposure for r in open_)
    rs = [r.r_multiple for r in closed if r.r_multiple is not None]
    holds = [r.hold_days for r in closed if r.hold_days is not None]

    return {
        "trades": len(closed),
        "openPositions": len(open_),
        "wins": len(wins),
        "losses": len(losses),
        "scratches": len(scratches),
        "winRate": _pct(len(wins), len(wins) + len(losses)) if (wins or losses) else None,
        "grossProfit": round(gross_profit, 2),
        "grossLoss": round(gross_loss, 2),
        # Capped rather than infinite so a run of pure winners doesn't
        # render as "∞ edge" on a sample of three.
        "profitFactor": (
            round(gross_profit / gross_loss, 2) if gross_loss
            else (None if not gross_profit else 99.0)
        ),
        "realized": round(realized, 2),
        "unrealized": round(unrealized, 2),
        "totalPnl": round(realized + unrealized, 2),
        "avgWinPct": round(statistics.fmean(r.realized_pct for r in wins), 2) if wins else None,
        "avgLossPct": round(statistics.fmean(r.realized_pct for r in losses), 2) if losses else None,
        "expectancyPct": (
            round(statistics.fmean(r.realized_pct for r in closed), 3) if closed else None
        ),
        "expectancyUsd": round(gross_profit - gross_loss, 2) if closed else None,
        "avgR": round(statistics.fmean(rs), 2) if rs else None,
        "avgHoldDays": round(statistics.fmean(holds), 1) if holds else None,
        "bestPct": round(max((r.realized_pct for r in closed), default=0.0), 2) if closed else None,
        "worstPct": round(min((r.realized_pct for r in closed), default=0.0), 2) if closed else None,
        "exposure": round(exposure, 2),
        "exposurePct": _pct(exposure, equity) if equity else None,
        "capitalDeployed": round(sum(r.cost_in for r in recs), 2),
        "fees": round(sum(r.fees for r in recs), 2),
    }


def _group(records: list[TradeRecord], key, label_of, *, equity: float) -> list[dict]:
    buckets: dict[str, list[TradeRecord]] = {}
    for r in records:
        buckets.setdefault(key(r), []).append(r)
    out = []
    for k, rows in buckets.items():
        out.append({
            "id": k,
            "label": label_of(k, rows),
            "tickers": sorted({r.ticker for r in rows}),
            **summarize(rows, equity=equity),
        })
    out.sort(key=lambda d: d["totalPnl"], reverse=True)
    return out


def performance(
    portfolio_id: str,
    *,
    db_path: Optional[Path] = None,
    marks: Optional[dict[str, dict]] = None,
    equity: Optional[float] = None,
    cash: Optional[float] = None,
    starting_equity: Optional[float] = None,
) -> dict:
    """The whole attribution report.

    `equity`/`cash`/`starting_equity` are injectable so the API can pass
    the marks it already fetched instead of hitting the price providers
    twice for one page render.
    """
    records = build_trade_records(portfolio_id, db_path=db_path, marks=marks)

    if equity is None or cash is None or starting_equity is None:
        pf = store.get_portfolio(portfolio_id, db_path=db_path)
        starting_equity = starting_equity if starting_equity is not None else (
            pf.starting_equity if pf else 0.0
        )
        cash = cash if cash is not None else (pf.cash if pf else 0.0)
        if equity is None:
            # Same signed-exposure convention as BookState.equity: longs add
            # their market value, shorts subtract the open liability.
            equity = cash + sum(
                r.open_exposure * (1.0 if r.side.upper() == "LONG" else -1.0)
                for r in records if not r.is_closed
            )

    overall = summarize(records, equity=equity)
    realized = overall["realized"]
    unrealized = overall["unrealized"]
    total = realized + unrealized

    headline = {
        "startingEquity": round(starting_equity, 2),
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        # The three percentage reads the desk actually asks for.
        "totalReturnPct": _pct(equity - starting_equity, starting_equity),
        "realizedPct": _pct(realized, starting_equity),      # "% booked"
        "unrealizedPct": _pct(unrealized, starting_equity),  # still an opinion
        # Of everything made so far, how much is off the table? Undefined
        # when realized and total disagree in sign — "share of gains
        # banked" means nothing when what has been banked is losses, and a
        # negative percentage there reads as if the book gave money back.
        "bookedShare": (
            _pct(realized, total)
            if total and realized and (realized > 0) == (total > 0)
            else None
        ),
        "bookedShareNote": (
            None if (total and realized and (realized > 0) == (total > 0))
            else "realized and open P&L point in opposite directions — read them separately"
        ),
        "totalFills": len(store.recent_orders(portfolio_id, limit=100_000, db_path=db_path)),
        "turnoverX": round(overall["capitalDeployed"] / starting_equity, 2) if starting_equity else None,
    }

    class_labels = classes()

    # Regimes overlap by construction — a sleeve can express two of them —
    # so these rows deliberately do NOT sum to the book. Flagged in the
    # payload so the UI can say so rather than implying a partition.
    regime_rows: dict[str, list[TradeRecord]] = {}
    for r in records:
        for reg in (r.regimes or ("unaligned",)):
            regime_rows.setdefault(reg, []).append(r)
    by_regime = [
        {
            "id": reg,
            "label": reg.replace("_", " "),
            "tickers": sorted({x.ticker for x in rows}),
            **summarize(rows, equity=equity),
        }
        for reg, rows in regime_rows.items()
    ]
    by_regime.sort(key=lambda d: d["totalPnl"], reverse=True)

    return {
        "headline": headline,
        "overall": overall,
        "bySleeve": _group(
            records, lambda r: r.sleeve_id,
            lambda k, rows: rows[0].sleeve_label, equity=equity,
        ),
        "byClass": _group(
            records, lambda r: r.class_id,
            lambda k, rows: class_labels.get(k, rows[0].class_label), equity=equity,
        ),
        "byRegime": by_regime,
        "regimeNote": (
            "A sleeve can express more than one regime, so these rows overlap "
            "and do not sum to the book."
        ),
        "bySide": _group(
            records, lambda r: r.side.upper(), lambda k, rows: k, equity=equity,
        ),
        "byExitReason": _group(
            [r for r in records if r.is_closed and r.exit_intent],
            lambda r: r.exit_intent or "unknown",
            lambda k, rows: str(k).replace("_", " "), equity=equity,
        ),
        "trades": [r.as_dict() for r in sorted(
            records, key=lambda r: (r.is_closed, -(r.total_pnl))
        )],
        "coverage": coverage([r.ticker for r in records]),
        "unclassified": sorted({r.ticker for r in records if r.sleeve_id == UNCLASSIFIED}),
    }


__all__ = ["TradeRecord", "build_trade_records", "summarize", "performance"]
