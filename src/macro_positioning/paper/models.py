"""Typed shapes for the paper book, plus the mandate loader.

Everything here is a plain dataclass with an `as_dict()` — the API and
the SPA consume dicts, the engine reasons over objects, and nothing
depends on an ORM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from macro_positioning.core.settings import settings
from macro_positioning.paper.vocabulary import Action, Band, Blocker, Intent


MANDATE_PATH = "config/paper_trading.json"

# The scale the floors are expressed on. Rank is a 0–100 percentile; the
# scale before it ("conviction") was 0–1 and its floors are meaningless
# here, so a stored mandate carrying the old scale is replaced at load
# rather than silently misread as "everything clears the bar".
SCALE = "rank_0_100"
LEGACY_SCALES = {"conviction_0_1", None, ""}


# ── Mandate ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Mandate:
    """The book's rulebook, flattened out of config/paper_trading.json.

    Snapshotted onto the portfolio row at creation: editing the JSON
    afterwards does not retroactively rewrite a running book's rules, so
    an equity curve stays interpretable against the mandate that made it.
    """

    starting_equity: float = 50_000.0
    base_currency: str = "USD"

    min_position_pct: float = 0.01
    max_position_pct: float = 0.05
    min_ticket_usd: float = 250.0
    add_threshold_pct: float = 0.0075

    min_cash_pct: float = 0.30
    rebalance_tolerance_pct: float = 0.005

    # All three are RANK points on the 0–100 percentile scale.
    entry_floor: float = 60.0
    exit_floor: float = 40.0
    rotation_edge: float = 15.0

    max_positions: int = 12
    max_bucket_pct: float = 0.20
    max_positions_per_bucket: int = 3

    trim_at_target_pct: float = 0.5           # legacy; superseded by the exit paths
    # ── Exit paths (paper/engine.py, Step 3). Assigned at fill from rank
    # and composed support; stored on the position.
    target_path_min_rank: float = 85.0
    target_path_min_support: float = 60.0
    near_miss_reach_pct: float = 0.90
    near_miss_giveback_r: float = 0.5
    ladder_rungs: tuple = ((1.0, 0.25, "breakeven"), (2.0, 0.25, "lock_1r"), (3.0, 0.25, "lock_2r"))
    trail_giveback_by_r: tuple = ((1.0, 0.50), (2.0, 0.35), (3.0, 0.25))
    trail_giveback_pct: float = 0.50
    trail_arms_at_r: float = 1.0
    min_hold_days: float = 10.0
    # Consecutive ticks a thesis exit must be signalled before it fires.
    # Two ticks a day, so 4 = the read has to stay bad for two days.
    thesis_exit_confirm_ticks: int = 4
    # ── Horizon derivation (paper/horizon.py). These are BOUNDS and a
    # fallback; the per-position number comes from the trade's inputs.
    horizon_fallback_days: float = 20.0
    min_hold_fraction: float = 0.4
    min_hold_bounds: tuple = (3.0, 21.0)
    confirm_ticks_bounds: tuple = (2, 8)
    confirm_days_divisor: float = 3.0
    # ── Learning loop (paper/learning.py)
    learning_shrink_n: int = 30       # w = n / (n + this)
    learning_min_n: int = 30          # hold/ladder priors engage at this n
    learning_window_days: int = 90
    learning_rank_cap: float = 10.0
    # Composed-view thresholds (paper/valuation.py).
    min_target_support: float = 35.0
    exit_target_support: float = 20.0
    use_composed_levels: bool = True
    stale_after_days: int = 21
    reentry_cooldown_days: int = 3
    reentry_reclaim_buffer_pct: float = 0.002
    max_stops_per_window: int = 2

    slippage_bps: float = 5.0
    fee_bps: float = 0.0
    # Minimum stop distance as a fraction of price. The book checks price
    # twice a day; a stop tighter than the ordinary overnight move is
    # not a stop, it is an invitation to fill 5R through it (AVAX, Sep 15:
    # 0.3% stop, −5.8R realised).
    min_stop_pct: float = 0.015
    # HARD cap on stop distance for spot positions — the user's one explicit
    # hard limit. Options would carry their own; the book only trades spot.
    max_stop_pct: float = 0.05

    # Bumped when the meaning of the numbers changes, not their values.
    # A mandate stored under an older scale cannot be read on this one.
    scale: str = SCALE
    # Set when this instance REPLACED a stored mandate written on an older
    # scale. The tick re-stamps the portfolio row when it sees this, so the
    # correction happens once instead of warning on every run.
    migrated_from_scale: Optional[str] = None

    @property
    def max_deployed_pct(self) -> float:
        """The other side of the cash floor. 30% cash → 70% deployable."""
        return 1.0 - self.min_cash_pct

    @classmethod
    def from_config(cls, raw: dict) -> "Mandate":
        book = raw.get("book", {})
        sizing = raw.get("sizing", {})
        cash = raw.get("cash", {})
        conv = raw.get("rank") or raw.get("conviction", {})
        conc = raw.get("concentration", {})
        exits = raw.get("exits", {})
        exe = raw.get("execution", {})
        hz = raw.get("horizon", {})
        lr = raw.get("learning", {})
        paths = exits.get("paths", {})
        d = cls()  # defaults, for any key the JSON omits
        return cls(
            starting_equity=float(book.get("starting_equity_usd", d.starting_equity)),
            base_currency=str(book.get("base_currency", d.base_currency)),
            min_position_pct=float(sizing.get("min_position_pct", d.min_position_pct)),
            max_position_pct=float(sizing.get("max_position_pct", d.max_position_pct)),
            min_ticket_usd=float(sizing.get("min_ticket_usd", d.min_ticket_usd)),
            add_threshold_pct=float(sizing.get("add_threshold_pct", d.add_threshold_pct)),
            min_cash_pct=float(cash.get("min_cash_pct", d.min_cash_pct)),
            rebalance_tolerance_pct=float(
                cash.get("rebalance_tolerance_pct", d.rebalance_tolerance_pct)
            ),
            entry_floor=float(conv.get("entry_floor", d.entry_floor)),
            exit_floor=float(conv.get("exit_floor", d.exit_floor)),
            rotation_edge=float(conv.get("rotation_edge", d.rotation_edge)),
            max_positions=int(conc.get("max_positions", d.max_positions)),
            max_bucket_pct=float(conc.get("max_bucket_pct", d.max_bucket_pct)),
            max_positions_per_bucket=int(
                conc.get("max_positions_per_bucket", d.max_positions_per_bucket)
            ),
            trim_at_target_pct=float(exits.get("trim_at_target_pct", d.trim_at_target_pct)),
            target_path_min_rank=float(paths.get("target", {}).get("min_rank", d.target_path_min_rank)),
            target_path_min_support=float(paths.get("target", {}).get("min_support", d.target_path_min_support)),
            near_miss_reach_pct=float(paths.get("near_miss", {}).get("reach_pct", d.near_miss_reach_pct)),
            near_miss_giveback_r=float(paths.get("near_miss", {}).get("giveback_r", d.near_miss_giveback_r)),
            ladder_rungs=tuple(
                (float(r["at_r"]), float(r["take"]), str(r["stop_to"]))
                for r in paths.get("ladder", {}).get("rungs", [])
            ) or d.ladder_rungs,
            trail_giveback_by_r=tuple(
                (float(r["at_r"]), float(r["giveback"]))
                for r in paths.get("trail", {}).get("giveback_by_r", [])
            ) or d.trail_giveback_by_r,
            trail_giveback_pct=float(exits.get("trail_giveback_pct", d.trail_giveback_pct)),
            trail_arms_at_r=float(exits.get("trail_arms_at_r", d.trail_arms_at_r)),
            min_hold_days=float(exits.get("min_hold_days", d.min_hold_days)),
            thesis_exit_confirm_ticks=int(
                exits.get("thesis_exit_confirm_ticks", d.thesis_exit_confirm_ticks)
            ),
            horizon_fallback_days=float(hz.get("fallback_days", d.horizon_fallback_days)),
            min_hold_fraction=float(hz.get("min_hold_fraction", d.min_hold_fraction)),
            min_hold_bounds=tuple(hz.get("min_hold_bounds", d.min_hold_bounds)),
            confirm_ticks_bounds=tuple(hz.get("confirm_ticks_bounds", d.confirm_ticks_bounds)),
            confirm_days_divisor=float(hz.get("confirm_days_divisor", d.confirm_days_divisor)),
            learning_shrink_n=int(lr.get("shrink_n", d.learning_shrink_n)),
            learning_min_n=int(lr.get("min_n", d.learning_min_n)),
            learning_window_days=int(lr.get("window_days", d.learning_window_days)),
            learning_rank_cap=float(lr.get("rank_cap", d.learning_rank_cap)),
            min_target_support=float(
                raw.get("composition", {}).get("min_target_support", d.min_target_support)
            ),
            exit_target_support=float(
                raw.get("composition", {}).get("exit_target_support", d.exit_target_support)
            ),
            use_composed_levels=bool(
                raw.get("composition", {}).get("use_composed_levels", d.use_composed_levels)
            ),
            stale_after_days=int(exits.get("stale_after_days", d.stale_after_days)),
            reentry_cooldown_days=int(
                exits.get("reentry_cooldown_days", d.reentry_cooldown_days)
            ),
            reentry_reclaim_buffer_pct=float(
                exits.get("reentry_reclaim_buffer_pct", d.reentry_reclaim_buffer_pct)
            ),
            max_stops_per_window=int(
                exits.get("max_stops_per_window", d.max_stops_per_window)
            ),
            slippage_bps=float(exe.get("slippage_bps", d.slippage_bps)),
            fee_bps=float(exe.get("fee_bps", d.fee_bps)),
            min_stop_pct=float(sizing.get("min_stop_pct", d.min_stop_pct)),
            max_stop_pct=float(sizing.get("max_stop_pct", d.max_stop_pct)),
            scale=str(raw.get("scale") or SCALE),
        )

    def as_dict(self) -> dict:
        return {
            "startingEquity": self.starting_equity,
            "baseCurrency": self.base_currency,
            "minPositionPct": self.min_position_pct,
            "maxPositionPct": self.max_position_pct,
            "minTicketUsd": self.min_ticket_usd,
            "addThresholdPct": self.add_threshold_pct,
            "minCashPct": self.min_cash_pct,
            "maxDeployedPct": self.max_deployed_pct,
            "rebalanceTolerancePct": self.rebalance_tolerance_pct,
            "entryFloor": self.entry_floor,
            "exitFloor": self.exit_floor,
            "rotationEdge": self.rotation_edge,
            "maxPositions": self.max_positions,
            "maxBucketPct": self.max_bucket_pct,
            "maxPositionsPerBucket": self.max_positions_per_bucket,
            "trimAtTargetPct": self.trim_at_target_pct,
            "targetPathMinRank": self.target_path_min_rank,
            "targetPathMinSupport": self.target_path_min_support,
            "nearMissReachPct": self.near_miss_reach_pct,
            "nearMissGivebackR": self.near_miss_giveback_r,
            "ladderRungs": [list(r) for r in self.ladder_rungs],
            "trailGivebackByR": [list(r) for r in self.trail_giveback_by_r],
            "trailGivebackPct": self.trail_giveback_pct,
            "trailArmsAtR": self.trail_arms_at_r,
            "minHoldDays": self.min_hold_days,
            "thesisExitConfirmTicks": self.thesis_exit_confirm_ticks,
            "horizonFallbackDays": self.horizon_fallback_days,
            "minHoldFraction": self.min_hold_fraction,
            "minHoldBounds": list(self.min_hold_bounds),
            "confirmTicksBounds": list(self.confirm_ticks_bounds),
            "confirmDaysDivisor": self.confirm_days_divisor,
            "learningShrinkN": self.learning_shrink_n,
            "learningMinN": self.learning_min_n,
            "learningWindowDays": self.learning_window_days,
            "learningRankCap": self.learning_rank_cap,
            "minTargetSupport": self.min_target_support,
            "exitTargetSupport": self.exit_target_support,
            "useComposedLevels": self.use_composed_levels,
            "staleAfterDays": self.stale_after_days,
            "reentryCooldownDays": self.reentry_cooldown_days,
            "reentryReclaimBufferPct": self.reentry_reclaim_buffer_pct,
            "maxStopsPerWindow": self.max_stops_per_window,
            "slippageBps": self.slippage_bps,
            "feeBps": self.fee_bps,
            "minStopPct": self.min_stop_pct,
            "maxStopPct": self.max_stop_pct,
            "scale": self.scale,
        }

    @classmethod
    def from_stored(cls, raw: str | dict | None) -> "Mandate":
        """Rehydrate from a portfolio row's `mandate_json`.

        A mandate is normally frozen at book creation so an equity curve
        stays interpretable against the rules that made it. The one thing
        that overrides that is a change of *scale*: an entry floor of
        0.28 was the 40th percentile under the old conviction number and
        is "everything qualifies" under rank, so honouring it would be
        worse than replacing it. Old-scale mandates therefore fall back
        to the current config, loudly.
        """
        if not raw:
            return load_mandate()
        d = json.loads(raw) if isinstance(raw, str) else raw
        if d.get("scale") in LEGACY_SCALES:
            import logging

            logging.getLogger(__name__).warning(
                "paper mandate stored on the legacy %r scale — replacing it with the "
                "current config. Floors on the old 0-1 conviction scale cannot be "
                "read as rank percentiles.", d.get("scale"),
            )
            current = load_mandate()
            return replace(current, migrated_from_scale=str(d.get("scale") or "unversioned"))
        base = cls()
        return cls(
            starting_equity=float(d.get("startingEquity", base.starting_equity)),
            base_currency=str(d.get("baseCurrency", base.base_currency)),
            min_position_pct=float(d.get("minPositionPct", base.min_position_pct)),
            max_position_pct=float(d.get("maxPositionPct", base.max_position_pct)),
            min_ticket_usd=float(d.get("minTicketUsd", base.min_ticket_usd)),
            add_threshold_pct=float(d.get("addThresholdPct", base.add_threshold_pct)),
            min_cash_pct=float(d.get("minCashPct", base.min_cash_pct)),
            rebalance_tolerance_pct=float(
                d.get("rebalanceTolerancePct", base.rebalance_tolerance_pct)
            ),
            entry_floor=float(d.get("entryFloor", base.entry_floor)),
            exit_floor=float(d.get("exitFloor", base.exit_floor)),
            rotation_edge=float(d.get("rotationEdge", base.rotation_edge)),
            max_positions=int(d.get("maxPositions", base.max_positions)),
            max_bucket_pct=float(d.get("maxBucketPct", base.max_bucket_pct)),
            max_positions_per_bucket=int(
                d.get("maxPositionsPerBucket", base.max_positions_per_bucket)
            ),
            trim_at_target_pct=float(d.get("trimAtTargetPct", base.trim_at_target_pct)),
            target_path_min_rank=float(d.get("targetPathMinRank", base.target_path_min_rank)),
            target_path_min_support=float(d.get("targetPathMinSupport", base.target_path_min_support)),
            near_miss_reach_pct=float(d.get("nearMissReachPct", base.near_miss_reach_pct)),
            near_miss_giveback_r=float(d.get("nearMissGivebackR", base.near_miss_giveback_r)),
            ladder_rungs=tuple(tuple(r) for r in d.get("ladderRungs", [])) or base.ladder_rungs,
            trail_giveback_by_r=tuple(tuple(r) for r in d.get("trailGivebackByR", [])) or base.trail_giveback_by_r,
            trail_giveback_pct=float(d.get("trailGivebackPct", base.trail_giveback_pct)),
            trail_arms_at_r=float(d.get("trailArmsAtR", base.trail_arms_at_r)),
            min_hold_days=float(d.get("minHoldDays", base.min_hold_days)),
            thesis_exit_confirm_ticks=int(
                d.get("thesisExitConfirmTicks", base.thesis_exit_confirm_ticks)
            ),
            horizon_fallback_days=float(d.get("horizonFallbackDays", base.horizon_fallback_days)),
            min_hold_fraction=float(d.get("minHoldFraction", base.min_hold_fraction)),
            min_hold_bounds=tuple(d.get("minHoldBounds", base.min_hold_bounds)),
            confirm_ticks_bounds=tuple(d.get("confirmTicksBounds", base.confirm_ticks_bounds)),
            confirm_days_divisor=float(d.get("confirmDaysDivisor", base.confirm_days_divisor)),
            learning_shrink_n=int(d.get("learningShrinkN", base.learning_shrink_n)),
            learning_min_n=int(d.get("learningMinN", base.learning_min_n)),
            learning_window_days=int(d.get("learningWindowDays", base.learning_window_days)),
            learning_rank_cap=float(d.get("learningRankCap", base.learning_rank_cap)),
            min_target_support=float(d.get("minTargetSupport", base.min_target_support)),
            exit_target_support=float(d.get("exitTargetSupport", base.exit_target_support)),
            use_composed_levels=bool(d.get("useComposedLevels", base.use_composed_levels)),
            stale_after_days=int(d.get("staleAfterDays", base.stale_after_days)),
            reentry_cooldown_days=int(
                d.get("reentryCooldownDays", base.reentry_cooldown_days)
            ),
            reentry_reclaim_buffer_pct=float(
                d.get("reentryReclaimBufferPct", base.reentry_reclaim_buffer_pct)
            ),
            max_stops_per_window=int(
                d.get("maxStopsPerWindow", base.max_stops_per_window)
            ),
            slippage_bps=float(d.get("slippageBps", base.slippage_bps)),
            fee_bps=float(d.get("feeBps", base.fee_bps)),
            min_stop_pct=float(d.get("minStopPct", base.min_stop_pct)),
            max_stop_pct=float(d.get("maxStopPct", base.max_stop_pct)),
            scale=str(d.get("scale") or SCALE),
        )


@lru_cache(maxsize=1)
def load_mandate(path: str | Path | None = None) -> Mandate:
    """Read config/paper_trading.json. Cached; pass `path` in tests."""
    p = Path(path) if path is not None else settings.base_dir / MANDATE_PATH
    try:
        with open(p, "r", encoding="utf-8") as f:
            return Mandate.from_config(json.load(f))
    except (OSError, json.JSONDecodeError):
        # A missing or malformed mandate file must not take the book
        # offline — the dataclass defaults ARE the documented mandate.
        return Mandate()


def reset_mandate_cache() -> None:
    load_mandate.cache_clear()


# ── Book objects ──────────────────────────────────────────────────────


@dataclass
class Position:
    position_id: str
    portfolio_id: str
    ticker: str
    side: str                      # LONG | SHORT
    qty: float
    avg_price: float
    opened_at: str
    status: str = "open"
    closed_at: Optional[str] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    rank_at_entry: Optional[float] = None
    rank_now: Optional[float] = None
    target_weight_pct: Optional[float] = None
    thesis: Optional[str] = None
    bucket_id: Optional[str] = None
    source: dict = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    initial_risk: Optional[float] = None   # |entry - stop| at open; R's denominator
    exit_signal_intent: Optional[str] = None   # thesis exit being watched
    expected_hold_days: Optional[float] = None # horizon derived at fill
    exit_path: Optional[str] = None            # 'target' | 'ladder', set at fill
    rungs_taken: int = 0                       # ladder rungs already taken
    near_miss_armed: bool = False              # reached >= reach_pct of the way
    min_hold_days: Optional[float] = None      # opinions wait this long
    confirm_ticks: Optional[int] = None        # ...and must persist this many ticks
    exit_signal_streak: int = 0                # consecutive ticks it has been signalled
    high_water_price: Optional[float] = None
    last_mark: Optional[float] = None
    last_mark_at: Optional[str] = None
    partial_taken: bool = False

    @property
    def is_long(self) -> bool:
        return self.side.upper() == "LONG"

    def exposure(self, price: Optional[float] = None) -> float:
        """Absolute notional at `price` (or the last mark). A short's
        exposure counts positively toward deployment — the book is at
        risk either way."""
        p = price if price is not None else (self.last_mark or self.avg_price)
        return abs(self.qty * (p or 0.0))

    def cost_basis(self) -> float:
        return abs(self.qty * self.avg_price)

    def unrealized(self, price: Optional[float] = None) -> float:
        p = price if price is not None else (self.last_mark or self.avg_price)
        if p is None:
            return 0.0
        direction = 1.0 if self.is_long else -1.0
        return (p - self.avg_price) * self.qty * direction

    def unrealized_pct(self, price: Optional[float] = None) -> float:
        basis = self.cost_basis()
        if basis <= 0:
            return 0.0
        return self.unrealized(price) / basis

    def r_multiple(self, price: Optional[float] = None) -> Optional[float]:
        """Open P&L in units of INITIAL risk.

        Deliberately prefers `initial_risk`, banked at entry, over the live
        stop. Once the target trim moves the stop to breakeven the live
        distance is zero, and computing R from it returns None — which
        silently disarmed the trailing exit for the rest of the trade.
        """
        risk_per_unit = self.initial_risk
        if not risk_per_unit or risk_per_unit <= 0:
            if not self.stop or self.stop <= 0:
                return None
            risk_per_unit = abs(self.avg_price - self.stop)
        if risk_per_unit <= 0:
            return None
        p = price if price is not None else (self.last_mark or self.avg_price)
        direction = 1.0 if self.is_long else -1.0
        return ((p - self.avg_price) * direction) / risk_per_unit

    def stop_breached(self, price: float) -> bool:
        if not self.stop or self.stop <= 0:
            return False
        return price <= self.stop if self.is_long else price >= self.stop

    def target_reached(self, price: float) -> bool:
        if not self.target or self.target <= 0:
            return False
        return price >= self.target if self.is_long else price <= self.target

    def as_dict(self, price: Optional[float] = None) -> dict:
        p = price if price is not None else self.last_mark
        return {
            "positionId": self.position_id,
            "ticker": self.ticker,
            "side": self.side,
            "qty": round(self.qty, 8),
            "avgPrice": round(self.avg_price, 6),
            "mark": round(p, 6) if p else None,
            "openedAt": self.opened_at,
            "closedAt": self.closed_at,
            "status": self.status,
            "stop": self.stop,
            "target": self.target,
            "rankAtEntry": self.rank_at_entry,
            "rankNow": self.rank_now,
            "targetWeightPct": self.target_weight_pct,
            "thesis": self.thesis,
            "bucketId": self.bucket_id,
            "source": self.source,
            "exposure": round(self.exposure(p), 2),
            "costBasis": round(self.cost_basis(), 2),
            "unrealized": round(self.unrealized(p), 2),
            "unrealizedPct": round(self.unrealized_pct(p) * 100, 2),
            "realizedPnl": round(self.realized_pnl, 2),
            "rMultiple": (
                round(self.r_multiple(p), 2) if self.r_multiple(p) is not None else None
            ),
            "initialRisk": self.initial_risk,
            "highWaterPrice": self.high_water_price,
            "partialTaken": self.partial_taken,
            "exitSignal": self.exit_signal_intent,
            "expectedHoldDays": self.expected_hold_days,
            "exitPath": self.exit_path,
            "rungsTaken": self.rungs_taken,
            "nearMissArmed": self.near_miss_armed,
            "minHoldDays": self.min_hold_days,
            "confirmTicks": self.confirm_ticks,
            "exitSignalStreak": self.exit_signal_streak,
        }


@dataclass
class Order:
    """An immutable fill. The order log is the book's source of truth —
    `reconcile()` rebuilds cash from these rows and asserts it matches."""

    order_id: str
    portfolio_id: str
    ticker: str
    action: Action
    side: str
    qty: float
    price: float
    notional: float
    cash_delta: float
    intent: Intent
    filled_at: str
    position_id: Optional[str] = None
    decision_id: Optional[str] = None
    tick_id: Optional[str] = None
    ref_price: Optional[float] = None
    realized_pnl: Optional[float] = None
    fees: float = 0.0
    rationale: Optional[str] = None
    price_source: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "orderId": self.order_id,
            "ticker": self.ticker,
            "action": str(self.action),
            "side": self.side,
            "qty": round(self.qty, 8),
            "price": round(self.price, 6),
            "refPrice": self.ref_price,
            "notional": round(self.notional, 2),
            "cashDelta": round(self.cash_delta, 2),
            "realizedPnl": round(self.realized_pnl, 2) if self.realized_pnl is not None else None,
            "fees": round(self.fees, 4),
            "intent": str(self.intent),
            "rationale": self.rationale,
            "priceSource": self.price_source,
            "filledAt": self.filled_at,
            "positionId": self.position_id,
            "decisionId": self.decision_id,
            "tickId": self.tick_id,
        }


@dataclass
class Decision:
    """What the engine decided, executed or not.

    A REJECT with a `blocker` is as important a row as a fill — it is the
    record of a trade the book deliberately did not take, and why.
    """

    decision_id: str
    portfolio_id: str
    tick_id: str
    decided_at: str
    ticker: str
    action: Action
    intent: Intent
    headline: str
    side: Optional[str] = None
    rank: Optional[float] = None
    rank_prev: Optional[float] = None
    target_weight_pct: Optional[float] = None
    current_weight_pct: Optional[float] = None
    notional: Optional[float] = None
    executed: bool = False
    blocker: Optional[Blocker] = None
    rationale: dict = field(default_factory=dict)
    position_id: Optional[str] = None
    order_id: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "decisionId": self.decision_id,
            "tickId": self.tick_id,
            "decidedAt": self.decided_at,
            "ticker": self.ticker,
            "action": str(self.action),
            "intent": str(self.intent),
            "headline": self.headline,
            "side": self.side,
            "rank": self.rank,
            "rankPrev": self.rank_prev,
            "targetWeightPct": self.target_weight_pct,
            "currentWeightPct": self.current_weight_pct,
            "notional": round(self.notional, 2) if self.notional is not None else None,
            "executed": self.executed,
            "blocker": str(self.blocker) if self.blocker else None,
            "rationale": self.rationale,
            "positionId": self.position_id,
            "orderId": self.order_id,
        }


@dataclass
class Portfolio:
    portfolio_id: str
    name: str
    starting_equity: float
    cash: float
    mandate: Mandate
    created_at: str
    base_currency: str = "USD"
    status: str = "active"
    updated_at: Optional[str] = None
    last_tick_at: Optional[str] = None
    notes: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "portfolioId": self.portfolio_id,
            "name": self.name,
            "baseCurrency": self.base_currency,
            "startingEquity": round(self.starting_equity, 2),
            "cash": round(self.cash, 2),
            "status": self.status,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "lastTickAt": self.last_tick_at,
            "notes": self.notes,
            "mandate": self.mandate.as_dict(),
        }


@dataclass
class BookState:
    """The book marked to market at a point in time — the denominator
    every weight in the engine is computed against."""

    portfolio: Portfolio
    positions: list[Position]
    marks: dict[str, dict]                  # ticker → spot_price() payload
    as_of: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def cash(self) -> float:
        return self.portfolio.cash

    def mark_of(self, ticker: str) -> Optional[float]:
        q = self.marks.get(ticker.upper())
        return float(q["price"]) if q and q.get("price") else None

    @property
    def deployed(self) -> float:
        return sum(p.exposure(self.mark_of(p.ticker)) for p in self.positions)

    @property
    def equity(self) -> float:
        """Cash plus *signed* market value — longs add, shorts subtract.

        A short's proceeds already sit in cash, so the open liability has
        to come back off or the book would show a 5% short as a 5% gain
        the moment it was opened. Short 10 ETH at 4,000: cash 90k, short
        market value 40k, equity 50k. ETH to 3,800: 90k − 38k = 52k, i.e.
        the 2k of open profit and nothing else.
        """
        total = self.cash
        for p in self.positions:
            mark = self.mark_of(p.ticker)
            total += p.exposure(mark) * (1.0 if p.is_long else -1.0)
        return total

    @property
    def deployed_pct(self) -> float:
        eq = self.equity
        return (self.deployed / eq) if eq > 0 else 0.0

    @property
    def cash_pct(self) -> float:
        eq = self.equity
        return (self.cash / eq) if eq > 0 else 1.0

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized(self.mark_of(p.ticker)) for p in self.positions)

    def weight_of(self, ticker: str) -> float:
        eq = self.equity
        if eq <= 0:
            return 0.0
        t = ticker.upper()
        return sum(
            p.exposure(self.mark_of(p.ticker)) for p in self.positions if p.ticker.upper() == t
        ) / eq

    def position_for(self, ticker: str) -> Optional[Position]:
        t = ticker.upper()
        for p in self.positions:
            if p.ticker.upper() == t:
                return p
        return None

    def headroom(self, mandate: Mandate) -> float:
        """Dollars still deployable before the cash floor bites."""
        return max(0.0, self.equity * mandate.max_deployed_pct - self.deployed)


__all__ = [
    "Mandate",
    "load_mandate",
    "reset_mandate_cache",
    "Position",
    "Order",
    "Decision",
    "Portfolio",
    "BookState",
    "Band",
]
