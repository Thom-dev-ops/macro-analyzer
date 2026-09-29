"""The tick — one pass of the paper book over the desk's current reads.

Order is the mandate, so the passes run in a fixed order:

  1. MARK      every held name and every candidate, then value the book.
  2. EXIT      stops, target trims, trailing giveback, rank decay,
               side flips, stale theses. Runs *before* buying so capital
               freed by an exit is spendable in the same tick.
  3. RANK      every scored name by percentile rank, strongest first.
  4. FILL      open / add toward each candidate's target weight, subject to
               the cash floor, the position cap, and the correlated-bucket
               caps — rotating out of materially weaker holdings when the
               only thing standing in the way is room.
  5. ENFORCE   if marks alone pushed the book past 70% deployed, trim the
               weakest holding back under the ceiling.
  6. PERSIST   every decision (fills, holds and refusals alike), the fill
               log, and one equity snapshot.

Every branch above emits a `Decision` carrying an `Action`, an `Intent`,
and — when it declined to trade — a `Blocker`. That is the deliverable:
the book is auditable line by line, including the trades it chose not to
make.

`dry_run=True` runs all six passes and returns the full decision set
without writing a byte.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from macro_positioning.paper import store
from macro_positioning.paper.rank import (
    Component,
    RankRead,
    load_candidates,
    score_age_days,
    target_weight_for,
)
from macro_positioning.paper.models import (
    BookState,
    Decision,
    Mandate,
    Order,
    Portfolio,
    Position,
)
from macro_positioning.paper.horizon import derive_horizon
from macro_positioning.paper.learning import Adjustments, adjustments as learn
from macro_positioning.paper.valuation import TradeValuation, valuate_reads
from macro_positioning.paper.vocabulary import Action, Band, Blocker, Intent
from macro_positioning.rules.portfolio import UNCORRELATED, bucket_for_ticker, bucket_label


logger = logging.getLogger(__name__)

PriceFn = Callable[[list[str]], dict[str, dict]]
RangeFn = Callable[[list[str], str], dict[str, dict]]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _default_price_fn(tickers: list[str]) -> dict[str, dict]:
    """Live marks. Isolated behind a callable so tests never touch the wire."""
    from macro_positioning.prices.spot import spot_prices

    return spot_prices(tickers)


def _default_range_fn(tickers: list[str], since: str) -> dict[str, dict]:
    """Traded high/low since a timestamp — what a resting stop needs.

    Only read when the mandate sets `author_stops_only`; the default book
    prices its stops off the mark. Same injection shape as `price_fn` so
    tests stay off the wire.
    """
    from macro_positioning.prices.spot import traded_ranges

    return traded_ranges(tickers, since=since)


@dataclass
class TickResult:
    tick_id: str
    portfolio_id: str
    ran_at: str
    dry_run: bool
    decisions: list[Decision] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    equity_before: float = 0.0
    equity_after: float = 0.0
    cash_before: float = 0.0
    cash_after: float = 0.0
    deployed_pct_before: float = 0.0
    deployed_pct_after: float = 0.0
    open_positions: int = 0
    candidates_considered: int = 0
    below_floor: int = 0        # directional, but not liked enough
    not_directional: int = 0    # WATCH / AVOID — never a trade in the first place
    unpriced: list[str] = field(default_factory=list)
    reconciliation: dict = field(default_factory=dict)
    committed: bool = False
    error: Optional[str] = None
    retryable: bool = False    # set when the tick failed on a DB lock, not on logic

    @property
    def fills(self) -> list[Decision]:
        return [d for d in self.decisions if d.executed]

    @property
    def refusals(self) -> list[Decision]:
        return [d for d in self.decisions if d.action == Action.REJECT]

    def as_dict(self) -> dict:
        return {
            "tickId": self.tick_id,
            "portfolioId": self.portfolio_id,
            "ranAt": self.ran_at,
            "dryRun": self.dry_run,
            "committed": self.committed,
            "equityBefore": round(self.equity_before, 2),
            "equityAfter": round(self.equity_after, 2),
            "cashBefore": round(self.cash_before, 2),
            "cashAfter": round(self.cash_after, 2),
            "deployedPctBefore": round(self.deployed_pct_before * 100, 2),
            "deployedPctAfter": round(self.deployed_pct_after * 100, 2),
            "openPositions": self.open_positions,
            "candidatesConsidered": self.candidates_considered,
            "belowFloor": self.below_floor,
            "notDirectional": self.not_directional,
            "unpriced": self.unpriced,
            "reconciliation": self.reconciliation,
            "error": self.error,
            "retryable": self.retryable,
            "decisions": [d.as_dict() for d in self.decisions],
            "orders": [o.as_dict() for o in self.orders],
        }

    def report(self) -> str:
        """Plain-text tick report. This is what lands in the launchd log,
        so the log itself is the audit trail."""
        lines = [
            f"paper tick {self.tick_id} — {self.ran_at}"
            + ("  [DRY RUN — nothing written]" if self.dry_run else ""),
            f"  equity ${self.equity_before:,.2f} → ${self.equity_after:,.2f}   "
            f"cash ${self.cash_after:,.2f} ({100 - self.deployed_pct_after * 100:.1f}%)   "
            f"deployed {self.deployed_pct_after * 100:.1f}%   positions {self.open_positions}",
            f"  candidates {self.candidates_considered} — "
            f"{self.not_directional} not directional (watch/avoid), "
            f"{self.below_floor} under the entry bar",
        ]
        if self.error:
            lines.append(f"  ERROR: {self.error}")
        acted = [d for d in self.decisions if d.action != Action.HOLD]
        if not acted:
            lines.append("  no action — every holding is at target and no candidate cleared the bar")
        for d in acted:
            flag = "✓" if d.executed else "·"
            blocker = f"   [{d.blocker}]" if d.blocker else ""
            lines.append(f"  {flag} {str(d.action):<7} {d.ticker:<8} {d.headline}{blocker}")
        holds = [d for d in self.decisions if d.action == Action.HOLD]
        if holds:
            lines.append(f"  held unchanged: {', '.join(d.ticker for d in holds)}")
        if self.reconciliation:
            lines.append(f"  reconciliation: {self.reconciliation}")
        return "\n".join(lines)


class _Tick:
    """Mutable working state for one tick.

    Holds the in-memory book (cash + positions), accumulates decisions
    and orders, and applies fills. Nothing touches SQLite until
    `commit()` — which is what makes `dry_run` free.
    """

    def __init__(
        self,
        portfolio: Portfolio,
        positions: list[Position],
        marks: dict[str, dict],
        *,
        tick_id: str,
        now: Optional[datetime] = None,
    ):
        self.pf = portfolio
        self.mandate: Mandate = portfolio.mandate
        self.positions = list(positions)
        self.marks = marks
        # ticker -> {"high","low","bars","from","to"} since each position's
        # last mark. Populated only under `author_stops_only`; empty
        # otherwise, which is what makes `stop_trigger()` fall back to the
        # mark for every other book.
        self.ranges: dict[str, dict] = {}
        self.cash = portfolio.cash
        self.tick_id = tick_id
        self.now = now or datetime.now(UTC)
        self.stamp = self.now.isoformat()
        self.decisions: list[Decision] = []
        self.orders: list[Order] = []
        self.touched: set[str] = set()      # position_ids mutated this tick
        self.opened: list[Position] = []    # positions created this tick
        # Tickers the exit pass already acted on. A position gets ONE
        # decision per tick: a name trimmed at its target must not be
        # topped straight back up by the entry pass a few lines later.
        self.acted: dict[str, Action] = {}
        # ticker -> TradeValuation for this tick, when one could be built.
        self.valuations: dict[str, TradeValuation] = {}
        self.reads: dict[str, RankRead] = {}     # ticker -> this tick's rank read
        self.learned: Optional[Adjustments] = None

    # ── Valuation ─────────────────────────────────────────────────────

    def state(self) -> BookState:
        pf = Portfolio(
            portfolio_id=self.pf.portfolio_id, name=self.pf.name,
            starting_equity=self.pf.starting_equity, cash=self.cash,
            mandate=self.mandate, created_at=self.pf.created_at,
            base_currency=self.pf.base_currency,
        )
        return BookState(pf, self.open_positions(), self.marks)

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if p.status == "open" and p.qty > 0]

    def mark_of(self, ticker: str) -> Optional[float]:
        q = self.marks.get(ticker.upper())
        return float(q["price"]) if q and q.get("price") else None

    def price_source(self, ticker: str) -> Optional[str]:
        q = self.marks.get(ticker.upper())
        return q.get("source") if q else None

    def stop_trigger(self, p: Position, mark: float) -> Optional[tuple[float, str]]:
        """Has this position's stop been hit, and at what price?

        Default: the stop is compared to the MARK and fills there — the
        book only looks twice a day, so that is all it honestly saw.

        Under `author_stops_only` the stop is the author's own level held
        as a RESTING order: it fires on the traded range since the last
        mark and fills AT the stop, not at wherever the tape happens to be
        when the book next looks. The difference is not cosmetic — ETH
        pierced 2530 on 2026-09-04 and did not CLOSE above it until
        2026-09-18, which is −1R against −3.75R on the same call.

        Returns `(fill_price, why)` or None.
        """
        if not p.stop or p.stop <= 0:
            return None
        # ORDER MATTERS. The resting check runs FIRST, because if the tape
        # went through the level then the order filled there — the mark is
        # simply where price got to afterwards, and honouring it would book
        # a worse fill than the resting stop actually had. HYPE is the case:
        # stop 87, pierced intraday on 2026-09-03 (high 87.99) and CLOSED at
        # 87.53. Mark-first fills at 87.53 for −$33.32; resting-first fills
        # at 87 for −$28.34, which is the −1R the stop was placed to cost.
        if self.mandate.author_stops_only:
            rng = self.ranges.get(p.ticker.upper())
            if rng:
                pierced = (
                    rng.get("low") is not None and float(rng["low"]) <= p.stop
                    if p.is_long
                    else rng.get("high") is not None and float(rng["high"]) >= p.stop
                )
                if pierced:
                    extreme = rng["low"] if p.is_long else rng["high"]
                    return (
                        float(p.stop),
                        f"traded through {p.stop:g} ({rng['bars']} bars "
                        f"{rng.get('from')}..{rng.get('to')}, "
                        f"extreme {float(extreme):g})",
                    )
        # No range to read, or the level was never touched: the mark is all
        # the book honestly saw.
        if p.stop_breached(mark):
            return (mark, "mark")
        return None

    @property
    def equity(self) -> float:
        return self.state().equity

    @property
    def deployed(self) -> float:
        return self.state().deployed

    def headroom(self) -> float:
        return max(0.0, self.equity * self.mandate.max_deployed_pct - self.deployed)

    def weight_of(self, position: Position) -> float:
        eq = self.equity
        return position.exposure(self.mark_of(position.ticker)) / eq if eq > 0 else 0.0

    # ── Decision logging ──────────────────────────────────────────────

    def decide(
        self,
        *,
        ticker: str,
        action: Action,
        intent: Intent,
        headline: str,
        side: Optional[str] = None,
        rank: Optional[float] = None,
        rank_prev: Optional[float] = None,
        target_weight_pct: Optional[float] = None,
        current_weight_pct: Optional[float] = None,
        notional: Optional[float] = None,
        executed: bool = False,
        blocker: Optional[Blocker] = None,
        rationale: Optional[dict] = None,
        position_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> Decision:
        d = Decision(
            decision_id=store.new_decision_id(),
            portfolio_id=self.pf.portfolio_id,
            tick_id=self.tick_id,
            decided_at=self.stamp,
            ticker=ticker.upper(),
            action=action,
            intent=intent,
            headline=headline,
            side=side,
            rank=round(rank, 2) if rank is not None else None,
            rank_prev=round(rank_prev, 2) if rank_prev is not None else None,
            target_weight_pct=(
                round(target_weight_pct * 100, 3) if target_weight_pct is not None else None
            ),
            current_weight_pct=(
                round(current_weight_pct * 100, 3) if current_weight_pct is not None else None
            ),
            notional=notional,
            executed=executed,
            blocker=blocker,
            rationale=rationale or {},
            position_id=position_id,
            order_id=order_id,
        )
        self.decisions.append(d)
        return d

    def constraint_state(self) -> dict:
        """The book's constraint surface at this instant — attached to
        every decision so a refusal can be re-read months later without
        re-deriving what the book looked like."""
        eq = self.equity
        return {
            "equity": round(eq, 2),
            "cash": round(self.cash, 2),
            "deployedPct": round(self.deployed_pct() * 100, 2),
            "maxDeployedPct": round(self.mandate.max_deployed_pct * 100, 2),
            "headroomUsd": round(self.headroom(), 2),
            "openPositions": len(self.open_positions()),
            "maxPositions": self.mandate.max_positions,
        }

    def deployed_pct(self) -> float:
        eq = self.equity
        return (self.deployed / eq) if eq > 0 else 0.0

    # ── Fills ─────────────────────────────────────────────────────────

    def _fill_price(self, ref: float, action: Action, side: str) -> float:
        """Cross the spread in the adverse direction.

        Buying (opening a long, covering a short) pays up; selling
        (trimming a long, opening a short) receives less. A paper book
        that fills at the mid flatters itself.
        """
        slip = self.mandate.slippage_bps / 10_000.0
        is_long = side.upper() == "LONG"
        buying = (action.is_entry and is_long) or (not action.is_entry and not is_long)
        return ref * (1 + slip) if buying else ref * (1 - slip)

    def fill(
        self,
        *,
        position: Position,
        action: Action,
        intent: Intent,
        qty: float,
        ref_price: float,
        decision: Optional[Decision] = None,
        rationale: Optional[str] = None,
    ) -> Order:
        """Apply a fill to the in-memory book and log the order.

        Cash effects, by construction:
            long  buy  → −notional      long  sell → +notional
            short sell → +notional      short buy  → −notional
        Fees always leave the book. `reconcile()` rebuilds cash from
        exactly these deltas.
        """
        side = position.side.upper()
        is_long = side == "LONG"
        price = self._fill_price(ref_price, action, side)
        notional = abs(qty * price)
        fees = notional * (self.mandate.fee_bps / 10_000.0)

        realized: Optional[float] = None
        if action.is_entry:
            # Weighted-average into the existing basis.
            new_qty = position.qty + qty
            if new_qty > 0:
                position.avg_price = (
                    position.avg_price * position.qty + price * qty
                ) / new_qty
            position.qty = new_qty
            cash_delta = (-notional if is_long else notional) - fees
        else:
            qty = min(qty, position.qty)
            notional = abs(qty * price)
            fees = notional * (self.mandate.fee_bps / 10_000.0)
            realized = (price - position.avg_price) * qty * (1.0 if is_long else -1.0)
            position.qty = max(0.0, position.qty - qty)
            position.realized_pnl += realized
            cash_delta = (notional if is_long else -notional) - fees
            if position.qty <= 1e-12:
                position.qty = 0.0
                position.status = "closed"
                position.closed_at = self.stamp

        position.fees_paid += fees
        position.last_mark = ref_price
        position.last_mark_at = self.stamp
        self.cash += cash_delta
        self.touched.add(position.position_id)

        order = Order(
            order_id=store.new_order_id(),
            portfolio_id=self.pf.portfolio_id,
            ticker=position.ticker,
            action=action,
            side=side,
            qty=round(qty, 10),
            price=price,
            notional=notional,
            cash_delta=cash_delta,
            intent=intent,
            filled_at=self.stamp,
            position_id=position.position_id,
            decision_id=decision.decision_id if decision else None,
            tick_id=self.tick_id,
            ref_price=ref_price,
            realized_pnl=realized,
            fees=fees,
            rationale=rationale or (decision.headline if decision else None),
            price_source=self.price_source(position.ticker),
        )
        self.orders.append(order)
        if decision is not None:
            decision.order_id = order.order_id
            decision.position_id = position.position_id
            decision.executed = True
        return order

    def open_position(
        self, read: RankRead, levels: dict, *, rank: float
    ) -> Position:
        p = Position(
            position_id=store.new_position_id(),
            portfolio_id=self.pf.portfolio_id,
            ticker=read.ticker,
            side=read.side,
            qty=0.0,
            avg_price=0.0,
            opened_at=self.stamp,
            status="open",
            stop=levels.get("stop") or None,
            target=levels.get("target") or None,
            initial_risk=(
                abs(float(self.mark_of(read.ticker) or 0) - float(levels["stop"]))
                if levels.get("stop") and self.mark_of(read.ticker) else None
            ),
            rank_at_entry=round(rank, 2),
            rank_now=round(rank, 2),
            target_weight_pct=round(read.target_weight_pct, 6),
            thesis=levels.get("setup") or None,
            bucket_id=bucket_for_ticker(read.ticker),
            source={
                "scoreId": (getattr(read, "source_row", {}) or {}).get("id"),
                "score": read.score,
                "grade": read.grade,
                "scoredAt": read.scored_at,
                "band": str(read.band),
                "components": [c.as_dict() for c in read.components],
                "targetSupport": None,   # filled in below when composed
                # "author" when the caller drew the stop, "house_5.0%" when
                # the book supplied one because they did not. An equity
                # curve must never confuse the two: one measures the call,
                # the other measures a sizing rule.
                "stopSource": (getattr(read, "source_row", {}) or {}).get(
                    "stopSource", "author"
                ),
            },
            high_water_price=self.mark_of(read.ticker),
        )
        self.positions.append(p)
        self.opened.append(p)
        return p

    # ── Persistence ───────────────────────────────────────────────────

    def _snapshot_row(self, p: Position) -> dict:
        """Every input the engine read on this position this tick."""
        mark = self.mark_of(p.ticker)
        read = self.reads.get(p.ticker.upper())
        val = self.valuations.get(p.ticker.upper())
        row_src = (getattr(read, "source_row", {}) or {}) if read else {}
        agg = row_src.get("signal_aggregate") or {}
        blend = agg.get("blend") or {}
        views = {e.view: e.delta for e in val.evidence} if val else {}
        held = score_age_days(p.opened_at, now=self.now)
        unreal_r = None
        if p.initial_risk and p.initial_risk > 0 and mark is not None:
            unreal_r = p.unrealized(mark) / (p.initial_risk * p.qty) if p.qty else None
        return {
            "portfolio_id": self.pf.portfolio_id,
            "position_id": p.position_id,
            "tick_id": self.tick_id,
            "taken_at": self.stamp,
            "ticker": p.ticker,
            "side": p.side,
            "days_held": round(held, 3) if held is not None else None,
            "mark": mark,
            "unrealized_r": round(unreal_r, 4) if unreal_r is not None else None,
            "unrealized_pct": round(p.unrealized_pct(mark) * 100, 4) if mark else None,
            "weight_pct": round(self.weight_of(p) * 100, 4),
            # A held name whose read went non-directional (WATCH/AVOID) has
            # rank 0 on the read; that is "no directional call", not a rank.
            # Keep the position's last real rank and record the read's side.
            "rank": read.value if (read and read.tradeable) else p.rank_now,
            "score": read.score if read else None,
            "read_side": read.side if read else None,
            "signal_direction": blend.get("bias_direction") or agg.get("bias_direction"),
            "signal_confidence": blend.get("bias_confidence") or agg.get("bias_confidence"),
            "signal_n": agg.get("n_signals"),
            "support": val.support if val else None,
            "support_structure": views.get("structure"),
            "support_voices": views.get("trusted_voices"),
            "support_regime": views.get("regime"),
            "support_price": views.get("price_action"),
            "regime_label": (row_src.get("trail") or {}).get("active_framework_regime"),
            "macro_alignment": row_src.get("macro"),
            "exit_signal": p.exit_signal_intent,
            "exit_signal_streak": p.exit_signal_streak,
            "expected_hold_days": p.expected_hold_days,
            "exit_path": p.exit_path,
        }

    def commit(self, db_path: Optional[Path], *, realized_before: float) -> dict:
        state = self.state()
        opened_ids = {p.position_id for p in self.opened}
        with store.connect(db_path) as conn:
            for p in self.opened:
                store.insert_position(conn, p)
            for p in self.positions:
                # Pre-existing rows: write back marks, rank and any
                # fill that landed this tick. Rows opened this tick were
                # just INSERTed with their final state.
                if p.position_id not in opened_ids:
                    store.update_position(conn, p)
            for o in self.orders:
                store.insert_order(conn, o)
            for d in self.decisions:
                store.insert_decision(conn, d)
            for p in self.open_positions():
                store.insert_position_snapshot(conn, self._snapshot_row(p))
            store.update_portfolio_cash(
                conn, self.pf.portfolio_id, self.cash, tick_at=self.stamp
            )
            if self.mandate.migrated_from_scale:
                # The stored mandate was written on a superseded scale and
                # was replaced at load. Persist the replacement so the
                # warning fires once, not on every tick forever.
                store.update_portfolio_mandate(
                    conn, self.pf.portfolio_id, self.mandate
                )
            realized_total = realized_before + sum(
                o.realized_pnl or 0.0 for o in self.orders
            )
            store.insert_equity_snapshot(
                conn,
                portfolio_id=self.pf.portfolio_id,
                tick_id=self.tick_id,
                equity=state.equity,
                cash=self.cash,
                deployed=state.deployed,
                open_positions=len(self.open_positions()),
                unrealized_pnl=state.unrealized_pnl,
                realized_pnl_to_date=realized_total,
                positions=[p.as_dict(self.mark_of(p.ticker)) for p in self.open_positions()],
                taken_at=self.stamp,
            )
        return store.reconcile(self.pf.portfolio_id, db_path=db_path)


# ── Thesis-exit confirmation ──────────────────────────────────────────


def _confirm(tick: _Tick, p: Position, intent: Intent) -> bool:
    """Has this thesis exit been signalled on enough consecutive ticks?

    A multi-week position should not be closed because one twice-daily
    read wobbled. The streak counts consecutive ticks the SAME intent has
    been signalled; a tick where it is not fires `_clear_signal` and the
    count starts over. Returns True when the streak reaches the mandate's
    `thesis_exit_confirm_ticks`, meaning the read has stayed bad long
    enough to be believed.
    """
    key = str(intent)
    if p.exit_signal_intent == key:
        p.exit_signal_streak += 1
    else:
        p.exit_signal_intent = key
        p.exit_signal_streak = 1
    tick.touched.add(p.position_id)
    need = p.confirm_ticks or tick.mandate.thesis_exit_confirm_ticks
    return p.exit_signal_streak >= need


def _clear_signal(tick: _Tick, p: Position) -> None:
    if p.exit_signal_intent is not None or p.exit_signal_streak:
        p.exit_signal_intent = None
        p.exit_signal_streak = 0
        tick.touched.add(p.position_id)


def _watching(tick: _Tick, p: Position, intent: Intent, why: str, **kw) -> None:
    """Log that a thesis exit is being watched but has not confirmed yet."""
    n = p.confirm_ticks or tick.mandate.thesis_exit_confirm_ticks
    tick.decide(
        ticker=p.ticker, action=Action.HOLD, intent=intent,
        headline=(
            f"{p.ticker}: {why} — watching, {p.exit_signal_streak}/{n} ticks. "
            f"Closes if it persists."
        ),
        side=p.side, position_id=p.position_id, executed=False, **kw,
    )


# ── Pass 2: exits ─────────────────────────────────────────────────────


def _exit_pass(tick: _Tick, reads: dict[str, RankRead]) -> None:
    m = tick.mandate
    before = len(tick.decisions)
    for p in list(tick.open_positions()):
        mark = tick.mark_of(p.ticker)
        read = reads.get(p.ticker.upper())
        rank_prev = p.rank_at_entry

        if mark is None:
            tick.decide(
                ticker=p.ticker, action=Action.HOLD, intent=Intent.NO_CHANGE,
                headline=Blocker.NO_PRICE.describe(ticker=p.ticker),
                side=p.side, position_id=p.position_id, blocker=Blocker.NO_PRICE,
                rank=p.rank_now, rank_prev=rank_prev,
                rationale={"constraints": tick.constraint_state()},
            )
            continue

        # Refresh the mark + high-water before any rule reads them.
        p.last_mark = mark
        p.last_mark_at = tick.stamp
        if p.high_water_price is None:
            p.high_water_price = mark
        else:
            p.high_water_price = max(p.high_water_price, mark) if p.is_long else min(
                p.high_water_price, mark
            )
        if read is not None and read.tradeable:
            p.rank_now = round(read.value, 4)
        tick.touched.add(p.position_id)

        weight = tick.weight_of(p)
        base_rationale = {
            "constraints": tick.constraint_state(),
            "position": p.as_dict(mark),
            "rank": read.as_dict() if read else None,
        }

        # 1. Stop — risk first, always.
        triggered = tick.stop_trigger(p, mark)
        if triggered is not None:
            stop_price, why = triggered
            d = tick.decide(
                ticker=p.ticker, action=Action.EXIT, intent=Intent.STOP_HIT,
                headline=Intent.STOP_HIT.describe(
                    ticker=p.ticker, stop=f"{p.stop:g}", price=f"{stop_price:g}"
                ),
                side=p.side, rank=p.rank_now, rank_prev=rank_prev,
                current_weight_pct=weight, notional=p.exposure(mark),
                position_id=p.position_id,
                rationale={**base_rationale, "stop": {
                    "trigger": why, "mark": mark, "filledAt": stop_price,
                    "resting": tick.mandate.author_stops_only,
                }},
            )
            tick.fill(position=p, action=Action.EXIT, intent=Intent.STOP_HIT,
                      qty=p.qty, ref_price=stop_price, decision=d)
            continue

        # 2. Side flip — the stack now reads the other way with real weight.
        #    Exempt from the minimum hold (direction is not an opinion) but
        #    NOT from confirmation: one tick reading SHORT is a wobble.
        flipped = (read is not None and read.tradeable and read.side != p.side
                   and read.value >= m.exit_floor)
        if flipped and not _confirm(tick, p, Intent.SIDE_FLIP):
            _watching(tick, p, Intent.SIDE_FLIP,
                      f"read flipped to {read.side} at rank {read.value:.0f}",
                      rank=read.value, rank_prev=rank_prev, current_weight_pct=weight,
                      rationale=base_rationale)
            continue
        if flipped:
            d = tick.decide(
                ticker=p.ticker, action=Action.EXIT, intent=Intent.SIDE_FLIP,
                headline=Intent.SIDE_FLIP.describe(
                    ticker=p.ticker, side_prev=p.side, side=read.side
                ),
                side=p.side, rank=read.value, rank_prev=rank_prev,
                current_weight_pct=weight, notional=p.exposure(mark),
                position_id=p.position_id, rationale=base_rationale,
            )
            tick.fill(position=p, action=Action.EXIT, intent=Intent.SIDE_FLIP,
                      qty=p.qty, ref_price=mark, decision=d)
            continue

        # 3. Profit-taking, by exit path. Assigned at fill from rank and
        #    composed support (`_assign_exit_path`); legacy rows without one
        #    take the ladder.
        #
        #    TARGET path (high-quality setups): held for the composed target.
        #    Nothing comes off below it — the four-view target was built so
        #    the book would have a level worth waiting for. Two exits:
        #      · the target is reached → close in full;
        #      · NEAR MISS: price covered >= `near_miss_reach_pct` of the
        #        way, then gave back >= `near_miss_giveback_r` from its high
        #        without crossing. The target was the idea, not the tick.
        #    LADDER path (everything else): thirds in R — at each rung take
        #    a slice and ratchet the stop. The agent's target is a rung too
        #    if it sits between.
        #    BOTH: the trail arms at 1R and its giveback tightens with R.
        path = p.exit_path or "ladder"
        r_now = p.r_multiple(mark)
        r_peak = p.r_multiple(p.high_water_price)

        if path == "target" and p.target:
            if p.target_reached(mark):
                d = tick.decide(
                    ticker=p.ticker, action=Action.EXIT, intent=Intent.TARGET_REACHED,
                    headline=Intent.TARGET_REACHED.describe(
                        ticker=p.ticker, target=f"{p.target:g}", price=f"{mark:g}",
                        source=(p.source or {}).get("targetSource") or "agent",
                    ),
                    side=p.side, rank=p.rank_now, rank_prev=rank_prev,
                    current_weight_pct=weight, notional=p.exposure(mark),
                    position_id=p.position_id, rationale=base_rationale,
                )
                tick.fill(position=p, action=Action.EXIT, intent=Intent.TARGET_REACHED,
                          qty=p.qty, ref_price=mark, decision=d)
                continue
            span = abs(p.target - p.avg_price)
            if span > 0:
                reach_hw = abs(p.high_water_price - p.avg_price) / span
                if reach_hw >= m.near_miss_reach_pct:
                    p.near_miss_armed = True
                    tick.touched.add(p.position_id)
                if (p.near_miss_armed and r_peak is not None and r_now is not None
                        and (r_peak - r_now) >= m.near_miss_giveback_r):
                    d = tick.decide(
                        ticker=p.ticker, action=Action.EXIT, intent=Intent.NEAR_MISS,
                        headline=Intent.NEAR_MISS.describe(
                            ticker=p.ticker, reach_pct=min(reach_hw, 0.999) * 100,
                            target=f"{p.target:g}", high_water=f"{p.high_water_price:g}",
                            giveback_r=r_peak - r_now, price=f"{mark:g}",
                        ),
                        side=p.side, rank=p.rank_now, rank_prev=rank_prev,
                        current_weight_pct=weight, notional=p.exposure(mark),
                        position_id=p.position_id, rationale=base_rationale,
                    )
                    tick.fill(position=p, action=Action.EXIT, intent=Intent.NEAR_MISS,
                              qty=p.qty, ref_price=mark, decision=d)
                    continue
        else:
            # Ladder: the next rung not yet taken. The agent's target is
            # folded in as a rung at its own R if it sits between two.
            rungs = list(m.ladder_rungs)
            if p.target and p.initial_risk:
                tgt_r = abs(p.target - p.avg_price) / p.initial_risk
                if not any(abs(tgt_r - r[0]) < 0.25 for r in rungs):
                    rungs.append((tgt_r, 0.25, "hold"))
            rungs.sort(key=lambda r: r[0])
            if p.rungs_taken < len(rungs) and r_now is not None:
                at_r, take, stop_to = rungs[p.rungs_taken]
                if r_now >= at_r and p.qty > 0:
                    # `take` is a share of the ORIGINAL size; rescale to what
                    # is left so three 25% rungs leave a 25% runner.
                    remaining_share = max(1e-9, 1.0 - sum(r[1] for r in rungs[:p.rungs_taken]))
                    qty = p.qty * min(1.0, take / remaining_share)
                    # Say what actually happens to the stop. On a book that
                    # holds the author's level, the rung takes its slice and
                    # the stop does not move — a headline promising
                    # "stop to breakeven" there would be describing a write
                    # the engine is about to skip.
                    stop_to_says = (
                        f"{p.stop:g} — unchanged, the author's"
                        if m.author_stops_only and p.stop
                        else stop_to.replace("_", " ")
                    )
                    d = tick.decide(
                        ticker=p.ticker, action=Action.TRIM, intent=Intent.RUNG_TAKEN,
                        headline=Intent.RUNG_TAKEN.describe(
                            ticker=p.ticker, r=at_r, take_pct=take * 100,
                            stop_to=stop_to_says,
                        ),
                        side=p.side, rank=p.rank_now, rank_prev=rank_prev,
                        current_weight_pct=weight, notional=abs(qty * mark),
                        position_id=p.position_id, rationale=base_rationale,
                    )
                    tick.fill(position=p, action=Action.TRIM, intent=Intent.RUNG_TAKEN,
                              qty=qty, ref_price=mark, decision=d)
                    p.rungs_taken += 1
                    sign = 1.0 if p.is_long else -1.0
                    # Ratcheting the stop is the BOOK's risk management,
                    # not the author's. On a copy-trader that silently
                    # replaces the level being measured, so the rung still
                    # takes its slice but the stop stays where it was drawn.
                    if not m.author_stops_only:
                        if stop_to == "breakeven":
                            p.stop = p.avg_price
                        elif stop_to.startswith("lock_") and p.initial_risk:
                            lock_r = float(stop_to[len("lock_"):-1])
                            p.stop = p.avg_price + sign * lock_r * p.initial_risk
                    continue

        # 4. Trailing giveback — armed once the trade has made something
        #    worth protecting, and TIGHTENING as it makes more.
        #
        #    The giveback is a RATIO, so without an arming threshold a
        #    position that ticked up $6 and back down $2 had "given back
        #    67%" and was closed. Five of the first seven trail exits fired
        #    on peak profits under 1%; NVDA peaked at +0.25% ($5.99) and
        #    was closed for −$98.19. The trail stays dormant until the trade
        #    is up `trail_arms_at_r` of its INITIAL risk. Once armed, the
        #    allowed giveback steps down with peak R (`trail_giveback_by_r`)
        #    — a 3R runner is not allowed to hand back half of it.
        peak = p.unrealized(p.high_water_price)
        current = p.unrealized(mark)
        trail_armed = r_peak is not None and r_peak >= m.trail_arms_at_r
        giveback_allowed = m.trail_giveback_pct
        if r_peak is not None:
            for at_r, gb in sorted(m.trail_giveback_by_r):
                if r_peak >= at_r:
                    giveback_allowed = gb
        if trail_armed and peak > 0 and (peak - current) / peak >= giveback_allowed:
            round_trip = current <= 0
            intent = Intent.TRAIL_ROUND_TRIP if round_trip else Intent.TRAIL_GIVEBACK
            headline = (
                Intent.TRAIL_ROUND_TRIP.describe(
                    ticker=p.ticker, high_water=f"{p.high_water_price:g}"
                )
                if round_trip
                else Intent.TRAIL_GIVEBACK.describe(
                    ticker=p.ticker,
                    giveback_pct=(peak - current) / peak * 100,
                    high_water=f"{p.high_water_price:g}",
                )
            )
            d = tick.decide(
                ticker=p.ticker, action=Action.EXIT, intent=intent, headline=headline,
                side=p.side, rank=p.rank_now, rank_prev=rank_prev,
                current_weight_pct=weight, notional=p.exposure(mark),
                position_id=p.position_id,
                rationale={**base_rationale, "trail": {
                    "peakR": round(r_peak, 3) if r_peak is not None else None,
                    "givebackAllowed": giveback_allowed}},
            )
            tick.fill(position=p, action=Action.EXIT, intent=intent,
                      qty=p.qty, ref_price=mark, decision=d)
            continue

        # How long the book has actually held this. A swing position is
        # not allowed to be talked out of itself in the first few days by
        # a read that moved — only price (the stop) and a hard directional
        # flip get to act that fast.
        held_days = score_age_days(p.opened_at, now=tick.now) or 0.0
        # The position's own horizon (derived at fill); the mandate's
        # constant is only the fallback for rows that predate it.
        min_hold = p.min_hold_days if p.min_hold_days is not None else m.min_hold_days
        young = held_days < min_hold

        # 5. Rank decay below the exit bar — confirmed over consecutive ticks.
        decayed = (read is not None and read.tradeable and read.value < m.exit_floor
                   and not young)
        if decayed and not _confirm(tick, p, Intent.RANK_DECAY):
            _watching(tick, p, Intent.RANK_DECAY,
                      f"rank {read.value:.0f} is under the {m.exit_floor:.0f} exit bar",
                      rank=read.value, rank_prev=rank_prev, current_weight_pct=weight,
                      rationale=base_rationale)
            continue
        if decayed:
            d = tick.decide(
                ticker=p.ticker, action=Action.EXIT, intent=Intent.RANK_DECAY,
                headline=Intent.RANK_DECAY.describe(
                    ticker=p.ticker, rank=read.value,
                    rank_prev=rank_prev or 0.0, exit_floor=m.exit_floor,
                ),
                side=p.side, rank=read.value, rank_prev=rank_prev,
                current_weight_pct=weight, notional=p.exposure(mark),
                position_id=p.position_id, rationale=base_rationale,
            )
            tick.fill(position=p, action=Action.EXIT, intent=Intent.RANK_DECAY,
                      qty=p.qty, ref_price=mark, decision=d)
            continue

        # 6. The case fell apart — the composed view that justified this
        #    trade no longer holds. Regime flipped against it, the level the
        #    target sat on broke, the trusted voices turned. Subject to the
        #    minimum hold like any other thesis exit: price gets to act
        #    fast, opinions do not.
        val = tick.valuations.get(p.ticker.upper())
        support_at_entry = (p.source or {}).get("targetSupport")
        collapsed = (val is not None and not young
                     and val.support < m.exit_target_support)
        if collapsed and not _confirm(tick, p, Intent.SUPPORT_COLLAPSED):
            _watching(tick, p, Intent.SUPPORT_COLLAPSED,
                      f"composed support fell to {val.support:.0f}/100",
                      rank=p.rank_now, rank_prev=rank_prev, current_weight_pct=weight,
                      rationale={**base_rationale, "composition": val.as_dict()})
            continue
        if collapsed:
            why = "; ".join(
                e.detail for e in val.evidence if e.stance == "opposes"
            ) or "no view still supports the target"
            d = tick.decide(
                ticker=p.ticker, action=Action.EXIT, intent=Intent.SUPPORT_COLLAPSED,
                headline=Intent.SUPPORT_COLLAPSED.describe(
                    ticker=p.ticker, support=val.support,
                    support_prev=support_at_entry if support_at_entry is not None else 50.0,
                    why=why,
                ),
                side=p.side, rank=p.rank_now, rank_prev=rank_prev,
                current_weight_pct=weight, notional=p.exposure(mark),
                position_id=p.position_id,
                rationale={**base_rationale, "composition": val.as_dict()},
            )
            tick.fill(position=p, action=Action.EXIT, intent=Intent.SUPPORT_COLLAPSED,
                      qty=p.qty, ref_price=mark, decision=d)
            continue

        # 7. Stale — the read behind this position has gone cold, or the
        #    name fell out of the scored universe entirely.
        age = score_age_days(read.scored_at, now=tick.now) if read else None
        if age is None and read is None:
            age = score_age_days(p.opened_at, now=tick.now)
        if age is not None and age > m.stale_after_days:
            d = tick.decide(
                ticker=p.ticker, action=Action.EXIT, intent=Intent.STALE_THESIS,
                headline=Intent.STALE_THESIS.describe(ticker=p.ticker, age_days=int(age)),
                side=p.side, rank=p.rank_now, rank_prev=rank_prev,
                current_weight_pct=weight, notional=p.exposure(mark),
                position_id=p.position_id, rationale=base_rationale,
            )
            tick.fill(position=p, action=Action.EXIT, intent=Intent.STALE_THESIS,
                      qty=p.qty, ref_price=mark, decision=d)
            continue

        # Nothing fired — the read recovered, so any streak resets. A
        # thesis exit has to be signalled on CONSECUTIVE ticks.
        _clear_signal(tick, p)
        tick.decide(
            ticker=p.ticker, action=Action.HOLD, intent=Intent.NO_CHANGE,
            headline=Intent.NO_CHANGE.describe(
                ticker=p.ticker, rank=p.rank_now or 0.0
            ),
            side=p.side, rank=p.rank_now, rank_prev=rank_prev,
            current_weight_pct=weight, target_weight_pct=p.target_weight_pct,
            notional=p.exposure(mark), position_id=p.position_id,
            rationale=base_rationale,
        )

    # Whatever the exit pass did to a name, the entry pass leaves it alone
    # for the rest of this tick.
    for d in tick.decisions[before:]:
        if d.action != Action.HOLD:
            tick.acted[d.ticker.upper()] = d.action


# ── Bucket accounting ─────────────────────────────────────────────────


def _bucket_state(tick: _Tick) -> dict[str, dict]:
    eq = tick.equity
    out: dict[str, dict] = {}
    for p in tick.open_positions():
        b = p.bucket_id or bucket_for_ticker(p.ticker)
        slot = out.setdefault(b, {"count": 0, "exposure": 0.0, "tickers": []})
        slot["count"] += 1
        slot["exposure"] += p.exposure(tick.mark_of(p.ticker))
        slot["tickers"].append(p.ticker)
    for b, slot in out.items():
        slot["pct"] = (slot["exposure"] / eq) if eq > 0 else 0.0
    return out


def _reranked(
    read: RankRead, shift: float, val, mandate: Mandate, component: Optional[Component] = None
) -> RankRead:
    """A copy of `read` with the composed view folded into its rank.

    Returns a copy rather than mutating: the same RankRead object is also
    the exit pass's view of a held position, and silently moving its rank
    would make an exit decision on a number the exit rules never saw.
    """
    import copy

    from macro_positioning.paper.rank import target_weight_for

    out = copy.copy(read)
    out.components = list(read.components) + [
        component if component is not None else Component(
            "composition", shift,
            f"composed view scores the target {val.support:.0f}/100 "
            f"({val.target_source} target)",
        )
    ]
    out.raw = read.raw + shift
    out.value = max(0.0, min(100.0, read.value + shift))
    out.band = Band.for_rank(out.value, entry_floor=mandate.entry_floor)
    out.target_weight_pct = target_weight_for(out.value, mandate)
    try:
        out.source_row = read.source_row  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return out


# ── Pass 4: entries + rotation ────────────────────────────────────────


def _make_room(
    tick: _Tick,
    read: RankRead,
    needed: float,
    *,
    need_slot: bool = False,
    bucket: Optional[str] = None,
) -> tuple[float, list[str]]:
    """Free room for `read` by trimming holdings that are materially weaker.

    Room comes in three flavours and the book can be short of any of them:
      * capital  — `needed` dollars of headroom under the cash floor;
      * a slot   — the position count is at the cap (`need_slot=True`, which
                   forces a full exit rather than a partial trim, since half
                   a position still occupies a slot);
      * bucket room — the correlated-bucket cap is full (`bucket=...`
                   restricts eligibility to that bucket's members).

    "Materially weaker" is the mandate's `rotation_edge`. Without it the
    book would churn on coin-flip differences and pay the spread for the
    privilege. A holding whose remainder would fall below the minimum
    position size is closed outright rather than left as a stub.

    Returns (dollars freed, names funded from).
    """
    m = tick.mandate
    freed = 0.0
    funded_by: list[str] = []
    eligible = [
        p for p in tick.open_positions()
        if (p.rank_now if p.rank_now is not None else 0.0)
        <= read.value - m.rotation_edge
        and tick.mark_of(p.ticker) is not None
        and p.ticker.upper() != read.ticker.upper()
        and p.ticker.upper() not in tick.acted
        # A position opened yesterday has not had a chance to work. Three
        # of the first eighteen closes were rotations out of positions
        # held for six hours.
        and (score_age_days(p.opened_at, now=tick.now) or 0.0)
            >= (p.min_hold_days if p.min_hold_days is not None else m.min_hold_days)
        and (bucket is None or (p.bucket_id or bucket_for_ticker(p.ticker)) == bucket)
    ]
    eligible.sort(key=lambda p: p.rank_now if p.rank_now is not None else 0.0)

    for p in eligible:
        if freed >= needed and not need_slot:
            break
        mark = tick.mark_of(p.ticker)
        exposure = p.exposure(mark)
        want = exposure if need_slot else min(exposure, needed - freed)
        # Don't leave an unmanageable stub behind.
        if exposure - want < max(m.min_ticket_usd, tick.equity * m.min_position_pct * 0.5):
            want = exposure
        qty = want / mark
        weight = tick.weight_of(p)
        action = Action.EXIT if qty >= p.qty - 1e-12 else Action.TRIM
        d = tick.decide(
            ticker=p.ticker, action=action, intent=Intent.MAKE_ROOM,
            headline=Intent.MAKE_ROOM.describe(
                ticker=p.ticker, rank=p.rank_now or 0.0,
                rotating_into=read.ticker, rival_rank=read.value,
            ),
            side=p.side, rank=p.rank_now,
            rank_prev=p.rank_at_entry,
            current_weight_pct=weight, notional=want,
            position_id=p.position_id,
            rationale={
                "constraints": tick.constraint_state(),
                "rotatingInto": read.ticker,
                "rivalRank": round(read.value, 2),
                "rotationEdge": m.rotation_edge,
                "position": p.as_dict(mark),
            },
        )
        tick.fill(position=p, action=action, intent=Intent.MAKE_ROOM,
                  qty=qty, ref_price=mark, decision=d)
        freed += want
        funded_by.append(p.ticker)
        if need_slot and p.status == "closed":
            # One slot is all that was missing.
            break
    return freed, funded_by


def _entry_pass(
    tick: _Tick, candidates: list[RankRead], cooldown: Optional[dict] = None
) -> int:
    """Walk candidates strongest-first, filling toward each target weight.

    Returns `(below_floor, not_directional)` — the two very different
    reasons a candidate never reached a decision row. Lumping them
    together, as this did at first, made the tick report read "64 below
    the entry floor" when almost all of those were WATCH/AVOID reads that
    were never a directional call to begin with. "We did not like it
    enough" and "it was not a trade" are not the same sentence.

    Both are summarised rather than logged one row each, so the decision
    log stays readable.
    """
    m = tick.mandate
    below_floor = 0
    not_directional = 0

    for read in candidates:
        if not read.tradeable:
            not_directional += 1
            continue
        if read.value < m.entry_floor:
            below_floor += 1
            continue

        # One decision per position per tick, and no buying back into a
        # risk exit inside the cooldown window.
        acted = tick.acted.get(read.ticker.upper())
        if acted is not None:
            tick.decide(
                ticker=read.ticker, action=Action.REJECT, intent=Intent.MANDATE_BLOCK,
                headline=Blocker.JUST_ACTED.describe(
                    ticker=read.ticker, acted_action=str(acted)
                ),
                side=read.side, rank=read.value,
                target_weight_pct=read.target_weight_pct,
                blocker=Blocker.JUST_ACTED,
                rationale={"rank": read.as_dict(),
                           "constraints": tick.constraint_state()},
            )
            continue
        row = getattr(read, "source_row", {}) or {}
        held = tick.state().position_for(read.ticker)
        reclaimed_from: Optional[dict] = None

        # A read on the OTHER side of a name the book holds is not an add —
        # it is a flip, and the exit pass owns flips (it is already watching
        # or closing). Treating it as an add sized the long to the short's
        # target weight. Skip quietly; the exit pass logged the decision.
        if held is not None and held.side.upper() != read.side.upper():
            continue

        # 1. Sizing — a target three views agree on is worth more size than
        #    one projected into open field with nobody watching. Support of
        #    50 is neutral; the shift is capped at +/-10 rank points so the
        #    composition tilts the size without overruling the rank.
        v0 = tick.valuations.get(read.ticker.upper())
        if v0 is not None:
            shift = max(-10.0, min(10.0, (v0.support - 50.0) / 5.0))
            if abs(shift) >= 0.5:
                read = _reranked(read, shift, v0, tick.mandate)

        # L1 — the book's own record on this sleeve, shrunk by n/(n+30).
        # The cap is the mandate's; the component carries n, the stat and w
        # so the row can be read months later.
        book_comp = tick.learned.rank_component(read.ticker) if tick.learned else None
        if book_comp is not None:
            read = _reranked(read, book_comp.delta, None, tick.mandate, component=book_comp)
        eq = tick.equity
        current_exposure = held.exposure(tick.mark_of(read.ticker)) if held else 0.0
        current_weight = (current_exposure / eq) if eq > 0 else 0.0
        target_notional = read.target_weight_pct * eq
        gap = target_notional - current_exposure

        # Attached to every decision on this candidate, fills and refusals
        # alike: a name turned away deserves the same four-view account as
        # one taken, or the log only explains the trades that happened.
        rationale = {
            "rank": read.as_dict(),
            "constraints": tick.constraint_state(),
            "levels": {
                "entry": row.get("entry"), "stop": row.get("stop"),
                "target": row.get("target"), "rr": row.get("rr"),
                "setup": row.get("setup"),
            },
        }
        if v0 is not None:
            rationale["composition"] = v0.as_dict()

        def reject(blocker: Blocker, **ctx) -> None:
            tick.decide(
                ticker=read.ticker, action=Action.REJECT, intent=Intent.MANDATE_BLOCK,
                headline=blocker.describe(**ctx), side=read.side,
                rank=read.value, target_weight_pct=read.target_weight_pct,
                current_weight_pct=current_weight, notional=max(gap, 0.0),
                blocker=blocker, executed=False, rationale=rationale,
                position_id=held.position_id if held else None,
            )

        mark = tick.mark_of(read.ticker)
        if mark is None or mark <= 0:
            reject(Blocker.NO_PRICE, ticker=read.ticker)
            continue

        # A position the book cannot invalidate is a position it will not size.
        if not row.get("hasLevels") or not row.get("stop"):
            reject(
                Blocker.NO_LEVELS,
                ticker=read.ticker,
                reason=row.get("levelsReason") or "no LevelSet on the score row",
            )
            continue

        # Levels are drawn at the scoring pass; the tape keeps moving. If the
        # stop is already on the wrong side of the live mark, the setup was
        # invalidated between the pass and this tick — opening it would buy a
        # position that stops out on the very next tick for the cost of the
        # spread, which is exactly what the first live run did to LMT, RTX
        # and NOC before this check existed.
        # The composed view on this trade, if one could be built.
        val = tick.valuations.get(read.ticker.upper())

        # 2. Admission — a target nothing agrees with is not a trade.
        if val is not None and val.blockers:
            reject(Blocker.UNSUPPORTED_TARGET, ticker=read.ticker,
                   support=val.support, why="; ".join(val.blockers))
            continue

        stop = float(row["stop"])
        # A HOUSE stop was derived from the price the book is about to pay,
        # at the book's own policy distance — it does not need to be
        # measured against the floor and the cap, it IS them. Measuring it
        # anyway rejects on rounding: the candidate's mark and this tick's
        # mark are two reads of the same tape seconds apart, and a 0.1%
        # drift puts an exactly-5% stop over a 5% cap.
        stop_source = str(row.get("stopSource") or "author")
        if stop_source == "author":
            # A stop the overnight move can walk through is not risk control.
            risk_pct = abs(mark - stop) / mark if mark else 0.0
            if risk_pct < m.min_stop_pct - 1e-9:  # a stop AT the floor passes
                reject(Blocker.STOP_TOO_TIGHT, ticker=read.ticker,
                       risk_pct=risk_pct * 100, floor_pct=m.min_stop_pct * 100)
                continue
            if risk_pct > m.max_stop_pct + 1e-9:
                reject(Blocker.STOP_TOO_WIDE, ticker=read.ticker,
                       risk_pct=risk_pct * 100, cap_pct=m.max_stop_pct * 100)
                continue
        wrong_side = mark <= stop if read.side == "LONG" else mark >= stop
        if wrong_side:
            reject(Blocker.LEVELS_STALE, ticker=read.ticker, price=f"{mark:g}",
                   rail="stop", level=f"{stop:g}")
            continue
        target = row.get("target")
        if target:
            target = float(target)
            past_target = mark >= target if read.side == "LONG" else mark <= target
            if past_target:
                reject(Blocker.LEVELS_STALE, ticker=read.ticker, price=f"{mark:g}",
                       rail="target", level=f"{target:g}")
                continue

        # ── Re-entry after a price exit ───────────────────────────────
        # A stop-out is not automatically a dead thesis. If price broke
        # the level and has since climbed back above what the book paid,
        # the setup re-presented itself and refusing it is leaving a valid
        # trade on the table. What the book will NOT do is buy back into a
        # name still trading below where it was stopped out — that is
        # paying twice for the same broken idea.
        exited = (cooldown or {}).get(read.ticker.upper())
        if exited and held is None:
            entry_price = float(exited.get("entry_price") or 0.0)
            buffer = m.reentry_reclaim_buffer_pct
            if read.side == "LONG":
                reclaim_at = entry_price * (1 + buffer)
                reclaimed = mark >= reclaim_at
            else:
                reclaim_at = entry_price * (1 - buffer)
                reclaimed = mark <= reclaim_at

            if int(exited.get("exit_count") or 0) >= m.max_stops_per_window:
                # Break, reclaim, break, reclaim is how a book dies by a
                # thousand spreads. Past this count the name waits out the
                # window whatever price does.
                reject(Blocker.CHOPPING, ticker=read.ticker,
                       exit_count=int(exited.get("exit_count") or 0),
                       window_days=m.reentry_cooldown_days)
                continue
            if not entry_price or not reclaimed:
                reject(
                    Blocker.COOLING_OFF, ticker=read.ticker,
                    exit_price=f"{float(exited.get('exit_price') or 0):g}",
                    exit_date=str(exited.get("filled_at"))[:10],
                    entry_price=f"{entry_price:g}",
                    price=f"{mark:g}",
                    reclaim_at=(
                        f"{reclaim_at:g}" if entry_price else "a recorded entry level"
                    ),
                )
                continue
            reclaimed_from = exited

        # A position the ladder has reduced is smaller ON PURPOSE. Topping
        # it back up would undo the risk-off and — worse — re-arm the stop
        # to the fresh LevelSet, throwing away the lock the rungs ratcheted
        # in. (Found by a fixture: three rungs, then an ADD moved the stop
        # from a locked 108 to 192 and the next dip stopped the whole
        # position out.)
        if held is not None and (held.rungs_taken or held.near_miss_armed):
            reject(Blocker.PROFIT_TAKEN, ticker=read.ticker, rungs=held.rungs_taken)
            continue

        if gap <= 0 or gap < eq * m.add_threshold_pct:
            if held is not None:
                reject(
                    Blocker.ALREADY_AT_TARGET, ticker=read.ticker,
                    weight_pct=current_weight * 100,
                    target_weight_pct=read.target_weight_pct * 100,
                )
            continue

        if gap < m.min_ticket_usd:
            reject(Blocker.MIN_TICKET, ticker=read.ticker, notional=gap,
                   min_ticket=m.min_ticket_usd)
            continue

        funded_by: list[str] = []

        # A full book is a room problem, not a verdict on the name: try to
        # rotate the weakest holding out of its slot before turning it away.
        if held is None and len(tick.open_positions()) >= m.max_positions:
            _, freed_names = _make_room(tick, read, gap, need_slot=True)
            funded_by += freed_names
            if len(tick.open_positions()) >= m.max_positions:
                reject(Blocker.MAX_POSITIONS, open_positions=len(tick.open_positions()),
                       max_positions=m.max_positions)
                continue

        # Correlated-bucket caps — the same map the real-money gate uses.
        # `uncorrelated` is the catch-all for tickers in no bucket, so it is
        # explicitly exempt: capping it would mean "the book may hold at most
        # three names nobody has grouped yet", which is the opposite of what
        # the cap is for. Named buckets stack a single bet; this one does not.
        bucket = bucket_for_ticker(read.ticker)
        if bucket != UNCORRELATED:
            buckets = _bucket_state(tick)
            slot = buckets.get(bucket, {"count": 0, "exposure": 0.0, "pct": 0.0, "tickers": []})
            if held is None and slot["count"] >= m.max_positions_per_bucket:
                # Same logic one level down: rotate within the bucket before
                # refusing, so a strong read can displace a weak neighbour.
                _, freed_names = _make_room(tick, read, gap, need_slot=True, bucket=bucket)
                funded_by += freed_names
                buckets = _bucket_state(tick)
                slot = buckets.get(
                    bucket, {"count": 0, "exposure": 0.0, "pct": 0.0, "tickers": []}
                )
                if slot["count"] >= m.max_positions_per_bucket:
                    reject(Blocker.BUCKET_COUNT, bucket_count=slot["count"],
                           bucket_label=bucket_label(bucket),
                           max_per_bucket=m.max_positions_per_bucket)
                    continue
            bucket_room = max(0.0, eq * m.max_bucket_pct - slot["exposure"])
            if bucket_room < m.min_ticket_usd:
                reject(Blocker.BUCKET_CAP, bucket_label=bucket_label(bucket),
                       bucket_pct=slot["pct"] * 100,
                       max_bucket_pct=m.max_bucket_pct * 100, ticker=read.ticker)
                continue
            gap = min(gap, bucket_room)

        # Room. Rotate if the only thing missing is capital.
        available = tick.headroom()
        if available < gap:
            _, freed_names = _make_room(tick, read, gap - available)
            funded_by += freed_names
            available = tick.headroom()

        if available < m.min_ticket_usd:
            weakest = min(
                tick.open_positions(),
                key=lambda p: p.rank_now if p.rank_now is not None else 0.0,
                default=None,
            )
            if weakest is not None:
                reject(
                    Blocker.NO_ROTATION_EDGE, weakest=weakest.ticker,
                    weakest_rank=weakest.rank_now or 0.0,
                    rotation_edge=m.rotation_edge, ticker=read.ticker,
                    rank=read.value,
                )
            else:
                reject(Blocker.CASH_FLOOR, deployed_pct=tick.deployed_pct() * 100,
                       max_deployed_pct=m.max_deployed_pct * 100)
            continue

        fill_notional = min(gap, available)
        # L2 — size for the risk the sleeve ACTUALLY realises. A sleeve whose
        # stops fill 0.6R through on the twice-daily tick is carrying 1.6R
        # of real risk per planned R; its size comes down to match.
        risk_mult = tick.learned.risk_multiplier(read.ticker) if tick.learned else 1.0
        if risk_mult > 1.0:
            fill_notional = fill_notional / risk_mult
            rationale["riskMultiplier"] = round(risk_mult, 3)
        # Longs consume cash; never let the book go cash-negative.
        if read.side == "LONG":
            fill_notional = min(fill_notional, max(0.0, tick.cash))
        if fill_notional < m.min_ticket_usd:
            reject(Blocker.CASH_FLOOR, deployed_pct=tick.deployed_pct() * 100,
                   max_deployed_pct=m.max_deployed_pct * 100)
            continue

        # 3. Levels — prefer the composed target over the agent's when the
        #    composition pulled it back to something the chart or the
        #    trusted voices actually support.
        open_row = dict(row)
        if (val is not None and m.use_composed_levels and val.target is not None
                and val.target_moved):
            open_row["target"] = val.target
            open_row["setup"] = f"{row.get('setup') or 'setup'} · target from {val.target_source}"

        qty = fill_notional / mark
        position = held or tick.open_position(read, open_row, rank=read.value)
        if held is None:
            # The horizon is set once, at fill, from this trade's own
            # inputs. A read that wobbles later does not shorten it.
            prior = tick.learned.hold_prior(read.ticker) if tick.learned else {}
            hz = derive_horizon(
                row, m,
                sleeve_prior_days=prior.get("peak_days"),
                sleeve_prior_n=int(prior.get("n") or 0),
            )
            position.expected_hold_days = round(hz.expected_days, 2)
            position.min_hold_days = round(hz.min_hold_days, 2)
            position.confirm_ticks = hz.confirm_ticks
            rationale["horizon"] = hz.as_dict()
            # Exit path: a high-quality setup is held for its composed
            # target; anything else ladders out in R. Decided once, here,
            # so a wobbling read cannot switch policy mid-hold.
            support = val.support if val is not None else None
            if (read.value >= m.target_path_min_rank
                    and support is not None and support >= m.target_path_min_support):
                position.exit_path = "target"
            else:
                position.exit_path = "ladder"
            rationale["exitPath"] = {
                "path": position.exit_path, "rank": round(read.value, 1),
                "support": round(support, 1) if support is not None else None,
                "minRank": m.target_path_min_rank, "minSupport": m.target_path_min_support,
            }
        if val is not None:
            # Bank the case that justified the entry, so a later collapse is
            # measurable against it rather than against an assumed 50.
            position.source = {**(position.source or {}),
                               "targetSupport": round(val.support, 1),
                               "targetSource": val.target_source}
        position.rank_now = round(read.value, 2)
        position.target_weight_pct = round(read.target_weight_pct, 6)
        if held is not None:
            # A top-up re-arms the plan against the current LevelSet — but a
            # stop only ever tightens on an add. Loosening it would hand back
            # protection the position had already earned.
            new_stop = row.get("stop")
            if new_stop and not m.author_stops_only:
                if position.is_long:
                    position.stop = max(position.stop or 0.0, float(new_stop))
                else:
                    position.stop = min(position.stop or float("inf"), float(new_stop))
            elif new_stop and m.author_stops_only:
                # A fresh call on a name already held carries the author's
                # CURRENT stop. Take it as stated — including looser, which
                # the signal book refuses — because on this book the stop is
                # a reading of the author, not a ratchet the book owns.
                position.stop = float(new_stop)
            position.target = row.get("target") or position.target

        action = Action.ADD if held is not None else Action.OPEN
        if reclaimed_from is not None:
            intent = Intent.RECLAIMED_SETUP
            headline = Intent.RECLAIMED_SETUP.describe(
                ticker=read.ticker,
                stop=f"{float(reclaimed_from.get('exit_price') or 0):g}",
                exit_date=str(reclaimed_from.get("filled_at"))[:10],
                entry=f"{float(reclaimed_from.get('entry_price') or 0):g}",
                price=f"{mark:g}", rank=read.value,
            )
        elif funded_by:
            intent = Intent.ROTATION_IN
            headline = Intent.ROTATION_IN.describe(
                ticker=read.ticker, rank=read.value,
                funded_by=", ".join(funded_by),
            )
        elif held is not None:
            intent = Intent.RANK_UPGRADE
            headline = Intent.RANK_UPGRADE.describe(
                ticker=read.ticker, rank=read.value,
                rank_prev=position.rank_at_entry or read.value,
                target_weight_pct=read.target_weight_pct * 100,
            )
        else:
            intent = Intent.CLEARED_BAR
            headline = Intent.CLEARED_BAR.describe(
                ticker=read.ticker, rank=read.value,
                score=read.score, band=read.band.value,
            )

        rationale["fundedBy"] = funded_by
        if reclaimed_from is not None:
            rationale["reclaimed"] = dict(reclaimed_from)
        d = tick.decide(
            ticker=read.ticker, action=action, intent=intent, headline=headline,
            side=read.side, rank=read.value,
            rank_prev=position.rank_at_entry,
            target_weight_pct=read.target_weight_pct,
            current_weight_pct=current_weight, notional=fill_notional,
            position_id=position.position_id, rationale=rationale,
        )
        tick.fill(position=position, action=action, intent=intent,
                  qty=qty, ref_price=mark, decision=d)

    return below_floor, not_directional


# ── Pass 5: cash-floor enforcement ────────────────────────────────────


def _enforce_cash_floor(tick: _Tick) -> None:
    """Marks move; the mandate does not. If the book drifted past the
    deployment ceiling on price alone, trim the weakest holding back
    under it — the 30% reserve is a floor, not a target."""
    m = tick.mandate
    guard = 0
    while tick.deployed_pct() > m.max_deployed_pct + m.rebalance_tolerance_pct and guard < 20:
        guard += 1
        excess = tick.deployed - tick.equity * m.max_deployed_pct
        candidates = [p for p in tick.open_positions() if tick.mark_of(p.ticker) is not None]
        if not candidates:
            return
        weakest = min(
            candidates, key=lambda p: p.rank_now if p.rank_now is not None else 0.0
        )
        mark = tick.mark_of(weakest.ticker)
        exposure = weakest.exposure(mark)
        want = min(exposure, excess)
        if exposure - want < m.min_ticket_usd:
            want = exposure
        qty = want / mark
        action = Action.EXIT if qty >= weakest.qty - 1e-12 else Action.TRIM
        d = tick.decide(
            ticker=weakest.ticker, action=action, intent=Intent.CASH_FLOOR_BREACH,
            headline=Intent.CASH_FLOOR_BREACH.describe(
                deployed_pct=tick.deployed_pct() * 100, ticker=weakest.ticker,
                max_deployed_pct=m.max_deployed_pct * 100,
            ),
            side=weakest.side, rank=weakest.rank_now,
            rank_prev=weakest.rank_at_entry,
            current_weight_pct=tick.weight_of(weakest), notional=want,
            position_id=weakest.position_id,
            rationale={
                "constraints": tick.constraint_state(),
                "position": weakest.as_dict(mark),
            },
        )
        tick.fill(position=weakest, action=action, intent=Intent.CASH_FLOOR_BREACH,
                  qty=qty, ref_price=mark, decision=d)


# ── The tick ──────────────────────────────────────────────────────────


def run_tick(
    portfolio_id: Optional[str] = None,
    *,
    dry_run: bool = False,
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    price_fn: Optional[PriceFn] = None,
    range_fn: Optional[RangeFn] = None,
    candidates: Optional[list[RankRead]] = None,
    portfolio: Optional[Portfolio] = None,
    valuations_in: Optional[dict] = None,
    learning_in: Optional[Adjustments] = None,
    use_learning: bool = True,
) -> TickResult:
    """Run one pass of the book. See the module docstring for the order.

    `price_fn` and `candidates` are injection points: tests drive the
    engine with synthetic marks and reads and never touch the wire.
    `portfolio` lets a caller run against a book that is not in the DB —
    which is how `--dry-run` previews the very first tick before any book
    has been opened. Passing an unsaved book with `dry_run=False` would
    write orders against a portfolio_id with no row, so it is refused.
    """
    if portfolio is not None:
        if not dry_run:
            raise ValueError("an unsaved portfolio can only be run as a dry run")
        pf = portfolio
    else:
        pf = (
            store.get_portfolio(portfolio_id, db_path=db_path)
            if portfolio_id
            else store.get_or_create_portfolio(db_path=db_path)
        )
    if pf is None:
        raise ValueError(f"no paper portfolio {portfolio_id!r}")

    tick_id = store.new_tick_id()
    positions = store.load_positions(pf.portfolio_id, db_path=db_path)
    reads = candidates if candidates is not None else load_candidates(pf.mandate)
    by_ticker = {r.ticker.upper(): r for r in reads}

    wanted = {p.ticker.upper() for p in positions} | {
        r.ticker.upper() for r in reads if r.tradeable and r.value >= pf.mandate.entry_floor
    }
    fetch = price_fn or _default_price_fn
    marks = fetch(sorted(wanted)) if wanted else {}
    marks = {k.upper(): v for k, v in (marks or {}).items()}

    # The window a stop-out stays "recent" for. Inside it the book needs
    # a reclaim to buy the name back; outside it the episode is history.
    cooldown_since = (
        (now or datetime.now(UTC)) - timedelta(days=pf.mandate.reentry_cooldown_days)
    ).isoformat()
    cooldown = store.recent_price_exits(
        pf.portfolio_id, since=cooldown_since, db_path=db_path
    )

    # The composed view: structure + trusted voices + regime + price action
    # on every candidate worth pricing. Best-effort — a failure here costs
    # the tick its richest input, not its ability to run.
    try:
        valuations = (
            valuations_in
            if valuations_in is not None
            else valuate_reads(
                reads, db_path=db_path, min_support=pf.mandate.min_target_support,
                held_tickers=[p.ticker for p in positions],
            )
        )
    except Exception as exc:
        logger.warning("composed valuation unavailable this tick: %s", exc)
        valuations = {}

    # The recursive loop: what the book's own record says about its next
    # trade. Best-effort — a failure here means "no adjustment", not a
    # stopped tick.
    learned: Optional[Adjustments] = None
    if learning_in is not None:
        learned = learning_in
    elif use_learning:
        try:
            learned = learn(pf.portfolio_id, mandate=pf.mandate, db_path=db_path, now=now)
        except Exception as exc:
            logger.warning("learning loop unavailable this tick: %s", exc)

    tick = _Tick(pf, positions, marks, tick_id=tick_id, now=now)
    tick.valuations = valuations
    tick.reads = by_ticker
    tick.learned = learned

    # A book that holds the author's stop as a resting order needs the
    # range the tape covered since it last looked, not just where the tape
    # is now. One window per position (they were marked at different
    # times), best-effort: losing it costs the resting check, not the tick.
    if pf.mandate.author_stops_only and positions:
        rfetch = range_fn or _default_range_fn
        for p in positions:
            since = p.last_mark_at or p.opened_at
            if not since:
                continue
            try:
                got = rfetch([p.ticker.upper()], since) or {}
            except Exception as exc:
                logger.warning(
                    "traded range unavailable for %s since %s: %s",
                    p.ticker, since, exc,
                )
                continue
            for k, v in got.items():
                tick.ranges[k.upper()] = v
    opening = tick.state()
    result = TickResult(
        tick_id=tick_id,
        portfolio_id=pf.portfolio_id,
        ran_at=tick.stamp,
        dry_run=dry_run,
        equity_before=opening.equity,
        cash_before=pf.cash,
        deployed_pct_before=opening.deployed_pct,
        candidates_considered=len(reads),
        unpriced=sorted(t for t in wanted if t not in marks),
    )

    try:
        _exit_pass(tick, by_ticker)
        result.below_floor, result.not_directional = _entry_pass(tick, reads, cooldown)
        _enforce_cash_floor(tick)
    except Exception as exc:               # a half-applied tick must not persist
        logger.exception("paper tick failed")
        result.error = f"{type(exc).__name__}: {exc}"
        result.decisions = tick.decisions
        result.orders = tick.orders
        return result

    closing = tick.state()
    result.decisions = tick.decisions
    result.orders = tick.orders
    result.equity_after = closing.equity
    result.cash_after = tick.cash
    result.deployed_pct_after = closing.deployed_pct
    result.open_positions = len(tick.open_positions())

    if dry_run:
        for d in result.decisions:
            if d.executed:
                d.rationale = {**(d.rationale or {}), "dryRun": True}
        return result

    try:
        realized_before = store.realized_to_date(pf.portfolio_id, db_path=db_path)
        recon = tick.commit(db_path, realized_before=realized_before)
    except sqlite3.OperationalError as exc:
        # The live DB has other writers — `alert_watch.py` in particular
        # holds a write transaction through a long price pass. The whole
        # tick commits in one transaction, so a lock here means NOTHING
        # was written and the caller can simply run the tick again.
        result.error = f"commit blocked: {exc}"
        result.retryable = True
        logger.warning("paper tick commit blocked, nothing written: %s", exc)
        return result
    result.reconciliation = recon
    result.committed = True
    if not recon.get("ok"):
        # The fill log and the cached balance disagree. Say so loudly —
        # this is a code bug, not a market event.
        result.error = (
            f"cash reconciliation failed: {recon}. The order log is the source of "
            "truth; investigate before trusting this book's P&L."
        )
        logger.error(result.error)
    return result


def close_position_manually(
    position_id: str, *, db_path: Optional[Path] = None, price_fn: Optional[PriceFn] = None,
    note: Optional[str] = None,
) -> Optional[Decision]:
    """Desk override — close a position by hand, logged like any other
    decision so the audit trail has no holes."""
    p = store.get_position(position_id, db_path=db_path)
    if p is None or p.status != "open":
        return None
    pf = store.get_portfolio(p.portfolio_id, db_path=db_path)
    if pf is None:
        return None
    fetch = price_fn or _default_price_fn
    marks = {k.upper(): v for k, v in (fetch([p.ticker]) or {}).items()}
    tick = _Tick(pf, [p], marks, tick_id=store.new_tick_id())
    mark = tick.mark_of(p.ticker)
    if mark is None:
        return None
    d = tick.decide(
        ticker=p.ticker, action=Action.EXIT, intent=Intent.MANUAL_OVERRIDE,
        headline=note or Intent.MANUAL_OVERRIDE.describe(ticker=p.ticker),
        side=p.side, rank=p.rank_now, rank_prev=p.rank_at_entry,
        current_weight_pct=tick.weight_of(p), notional=p.exposure(mark),
        position_id=p.position_id,
        rationale={"constraints": tick.constraint_state(), "position": p.as_dict(mark)},
    )
    tick.fill(position=p, action=Action.EXIT, intent=Intent.MANUAL_OVERRIDE,
              qty=p.qty, ref_price=mark, decision=d)
    realized_before = store.realized_to_date(pf.portfolio_id, db_path=db_path)
    tick.commit(db_path, realized_before=realized_before)
    return d


__all__ = ["run_tick", "close_position_manually", "TickResult"]
