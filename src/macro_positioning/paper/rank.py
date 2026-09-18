"""Rank — where a name stands against everything the desk scores.

**Rank is a 0–100 percentile.** A rank of 72 means the name sits above
roughly 72% of the scores in the trailing distribution. It is not a
probability, not a confidence, and not calibrated against outcomes —
nothing in this system yet establishes that a rank-80 name wins more
often than a rank-40 one. It is an *ordering*, and that is all it claims.

This replaced a 0–1 "conviction" number on 2026-08-28. That number was a
straight interpolation between two hand-picked score anchors (55 → 0.00,
90 → 1.00), which gave it the shape of a probability without the meaning
of one: 0.28 read as "28% confident" when what it actually described was
the 40th percentile — a below-median name. Anchoring on the live
distribution makes the number say what it appears to say.

The score is the spine (it is the only surface carrying the technical
agent's stop, and the book will not size a position it cannot
invalidate). Evidence the score does not capture then moves the rank in
percentile points:

    KOL signals agree           +15   ·   oppose         −25
    short bloc flipped          − 8
    3R+ to target               +10   ·   2R+  +5  ·  <1.2R  −10
    stop on real structure      + 5
    score improving / decaying  ± 5

Every one of those is stored on the decision row as a
`(name, delta, reason)` triple, so a 4.1% position is always traceable to
the reads that produced it.
"""

from __future__ import annotations

import bisect
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from macro_positioning.core.settings import settings
from macro_positioning.paper.models import Mandate
from macro_positioning.paper.vocabulary import Band, TRADEABLE_SIDES


logger = logging.getLogger(__name__)


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# ── The distribution the rank is measured against ─────────────────────

# Below this many observations the percentile is noise, and the fallback
# straight-line map is the more honest answer.
_MIN_SAMPLE = 50
_WINDOW_DAYS = 180

# Fallback anchors, used only when there is no usable history (a fresh
# DB, or a test). Deliberately the same anchors the old conviction scale
# used, so behaviour without history is unchanged and explicable.
_FALLBACK_FLOOR = 55.0
_FALLBACK_CEILING = 90.0


def load_score_distribution(
    *, db_path: Optional[Path] = None, window_days: int = _WINDOW_DAYS
) -> list[float]:
    """Sorted scores from recent *scheduled* passes.

    Scheduled only: hand-run and what-if passes are scored under
    different regime hints, and mixing them would move the ruler every
    time someone ran a backtest.
    """
    path = db_path or settings.sqlite_path
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10) as conn:
            rows = conn.execute(
                """
                SELECT adjusted_total_score
                  FROM trade_scores
                 WHERE adjusted_total_score IS NOT NULL
                   AND (pass_kind = 'scheduled' OR pass_kind IS NULL)
                   AND scored_at >= datetime('now', ?)
                """,
                (f"-{int(window_days)} day",),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        logger.debug("score distribution unavailable: %s", exc)
        return []
    return sorted(float(r[0]) for r in rows if r[0] is not None)


def percentile_of(score: float, distribution: list[float]) -> float:
    """What fraction of the distribution this score stands above, 0–100.

    Midpoint convention on ties, so a score sitting on a dense mode does
    not get credited with beating every name that equals it.
    """
    if not distribution:
        # No history: fall back to the straight line between the old
        # anchors so the engine still ranks, and says it is doing so.
        span = _FALLBACK_CEILING - _FALLBACK_FLOOR
        return _clamp((float(score) - _FALLBACK_FLOOR) / span * 100.0)
    n = len(distribution)
    below = bisect.bisect_left(distribution, score)
    equal = bisect.bisect_right(distribution, score) - below
    return _clamp((below + equal / 2.0) / n * 100.0)


def distribution_is_usable(distribution: list[float]) -> bool:
    return len(distribution) >= _MIN_SAMPLE


# ── The read ──────────────────────────────────────────────────────────


@dataclass
class Component:
    """One contribution to the rank, in percentile points, with its reason."""

    name: str
    delta: float
    reason: str

    def as_dict(self) -> dict:
        return {"name": self.name, "delta": round(self.delta, 2), "reason": self.reason}


@dataclass
class RankRead:
    ticker: str
    side: str
    value: float                      # 0–100
    band: Band
    target_weight_pct: float
    components: list[Component] = field(default_factory=list)
    tradeable: bool = True
    reason_untradeable: Optional[str] = None
    score: Optional[int] = None
    grade: Optional[str] = None
    scored_at: Optional[str] = None
    anchored: bool = True             # False when there was no distribution to rank against
    # The adjusted value BEFORE clamping into 0-100. A 92nd-percentile
    # name with every modifier in its favour lands at 122; nine such names
    # would all display 100 and their ordering would then be arbitrary,
    # which matters because ordering decides who gets the last slot.
    # Display and every bar comparison use `value`; ranking uses `raw`.
    raw: float = 0.0

    @property
    def headline(self) -> str:
        movers = sorted(
            [c for c in self.components if c.name != "base" and abs(c.delta) >= 1.0],
            key=lambda c: abs(c.delta),
            reverse=True,
        )[:2]
        tail = "; ".join(c.reason for c in movers)
        head = (
            f"{self.ticker} {self.side} ranks {self.value:.0f} "
            f"({self.band.value}, {self.target_weight_pct * 100:.1f}% target)"
        )
        return f"{head} — {tail}" if tail else head

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "side": self.side,
            "rank": round(self.value, 1),
            "band": str(self.band),
            "bandMeaning": self.band.describe(),
            "targetWeightPct": round(self.target_weight_pct * 100, 3),
            "components": [c.as_dict() for c in self.components],
            "tradeable": self.tradeable,
            "reasonUntradeable": self.reason_untradeable,
            "score": self.score,
            "grade": self.grade,
            "scoredAt": self.scored_at,
            "anchored": self.anchored,
            "raw": round(self.raw, 1),
            "headline": self.headline,
        }


def target_weight_for(rank: float, mandate: Mandate) -> float:
    """Size across the TRADEABLE range, not the whole 0–100.

    A name that just clears the entry bar gets the 1% minimum; a rank-100
    name gets the full 5%. Interpolating from rank 0 would spend the
    bottom of the band on sizes nothing is ever eligible for — under the
    old scale the entry bar sat at 0.28, so 1%–2.1% of the range was
    simply unreachable.
    """
    if rank < mandate.entry_floor:
        return 0.0
    span = max(1e-9, 100.0 - mandate.entry_floor)
    frac = _clamp((rank - mandate.entry_floor) / span, 0.0, 1.0)
    return mandate.min_position_pct + frac * (
        mandate.max_position_pct - mandate.min_position_pct
    )


def read_rank(
    row: dict, mandate: Mandate, distribution: Optional[list[float]] = None
) -> RankRead:
    """Rank one scored row against the distribution, then adjust it.

    `row` is a `dashboard.desk_data._signal_shape()`-style dict — the same
    shape the SPA's asset cards consume — augmented with the raw score
    row's `signal_aggregate`, `d_score` and `scored_at`.
    """
    ticker = str(row.get("asset") or row.get("ticker") or "").upper()
    side = str(row.get("side") or "").upper()
    score = row.get("score")
    components: list[Component] = []

    if side not in TRADEABLE_SIDES:
        return RankRead(
            ticker=ticker, side=side, value=0.0, band=Band.NONE,
            target_weight_pct=0.0, tradeable=False,
            reason_untradeable=f"reads {side or 'no side'}, not a tradeable direction",
            score=score, grade=row.get("grade"), scored_at=row.get("scored_at"),
            raw=0.0,
        )

    dist = distribution if distribution is not None else []
    anchored = distribution_is_usable(dist)
    base = percentile_of(float(score or 0), dist if anchored else [])
    components.append(
        Component(
            "base", base,
            (
                f"score {score} ranks {base:.0f} — above {base:.0f}% of the "
                f"{len(dist):,} scores in the last {_WINDOW_DAYS} days"
            ) if anchored else (
                f"score {score} maps to {base:.0f} on the fallback scale "
                f"({_FALLBACK_FLOOR:.0f}→0, {_FALLBACK_CEILING:.0f}→100) — "
                "not enough scoring history to rank against"
            ),
        )
    )
    value = base

    # ── Signal-layer agreement ────────────────────────────────────────
    agg = row.get("signal_aggregate") or {}
    blend = agg.get("blend") or {}
    direction = str(blend.get("bias_direction") or agg.get("bias_direction") or "neutral")
    confidence = float(blend.get("bias_confidence") or agg.get("bias_confidence") or 0.0)
    n_signals = int(agg.get("n_signals") or 0)
    wanted = "long" if side == "LONG" else "short"
    other = "short" if side == "LONG" else "long"

    if n_signals == 0:
        components.append(
            Component("signal_agreement", 0.0, "no KOL signals on this name — score stands alone")
        )
    elif direction == wanted:
        d = 15.0 * _clamp(confidence, 0.0, 1.0)
        value += d
        components.append(
            Component(
                "signal_agreement", d,
                f"{n_signals} signals blend {direction} at {confidence:.2f} confidence — "
                f"the desk agrees with the {side.lower()}",
            )
        )
    elif direction == other:
        d = -25.0 * _clamp(confidence, 0.0, 1.0)
        value += d
        components.append(
            Component(
                "signal_agreement", d,
                f"{n_signals} signals blend {direction} at {confidence:.2f} confidence — "
                f"the desk is on the other side of this {side.lower()}",
            )
        )
    else:
        components.append(
            Component(
                "signal_agreement", 0.0,
                f"{n_signals} signals but the blend reads {direction} — no directional help",
            )
        )

    # ── Cross-window divergence ───────────────────────────────────────
    cross = agg.get("cross_window") or {}
    if cross.get("recent_flip"):
        short_dir = ((cross.get("short_bloc") or {}).get("direction")) or "?"
        long_dir = ((cross.get("long_bloc") or {}).get("direction")) or "?"
        value -= 8.0
        components.append(
            Component(
                "cross_window", -8.0,
                f"recent signals flipped {long_dir} → {short_dir} against the longer thesis — "
                "the read is unstable",
            )
        )

    # ── Risk / reward from the technical agent ────────────────────────
    rr = float(row.get("rr") or 0.0)
    if row.get("hasLevels"):
        if rr >= 3.0:
            value += 10.0
            components.append(Component("risk_reward", 10.0, f"{rr:.1f}R to target — asymmetric"))
        elif rr >= 2.0:
            value += 5.0
            components.append(Component("risk_reward", 5.0, f"{rr:.1f}R to target — favourable"))
        elif 0 < rr < 1.2:
            value -= 10.0
            components.append(
                Component("risk_reward", -10.0, f"only {rr:.1f}R to target — thin payoff for the risk")
            )
        if row.get("levelStructural"):
            value += 5.0
            components.append(
                Component(
                    "level_quality", 5.0,
                    f"stop sits on structure, not a projection ({row.get('setup') or 'setup'})",
                )
            )

    # ── Score momentum ────────────────────────────────────────────────
    d_score = row.get("d_score")
    if d_score is not None:
        if d_score >= 5:
            value += 5.0
            components.append(
                Component("momentum", 5.0, f"score improved {d_score:+d} since the last pass")
            )
        elif d_score <= -5:
            value -= 5.0
            components.append(
                Component("momentum", -5.0, f"score deteriorated {d_score:+d} since the last pass")
            )

    raw = value
    value = _clamp(value)
    band = Band.for_rank(value, entry_floor=mandate.entry_floor)
    weight = target_weight_for(value, mandate)

    return RankRead(
        ticker=ticker, side=side, value=value, band=band,
        target_weight_pct=weight, components=components,
        tradeable=True, score=score, grade=row.get("grade"),
        scored_at=row.get("scored_at"), anchored=anchored, raw=raw,
    )


def score_age_days(scored_at: Optional[str], *, now: Optional[datetime] = None) -> Optional[float]:
    """How stale is this read? Feeds the STALE_THESIS exit."""
    if not scored_at:
        return None
    now = now or datetime.now(UTC)
    try:
        s = str(scored_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    except ValueError:
        return None
    return (now - dt).total_seconds() / 86400.0


def load_candidates(
    mandate: Mandate, *, db_path: Optional[Path] = None
) -> list[RankRead]:
    """Every scored name in the most recent pass, ranked strongest first.

    Reuses `desk_data._load_latest_scores()` — the CTE that ranks the
    current and prior trade_score per asset and joins the technical
    agent's LevelSet. Re-implementing that query here would be a second
    definition of "the current read", which is exactly the kind of drift
    that makes two panels disagree.
    """
    from macro_positioning.dashboard.desk_data import _load_latest_scores, _signal_shape

    distribution = load_score_distribution(db_path=db_path)
    reads: list[RankRead] = []
    for raw in _load_latest_scores():
        row = dict(_signal_shape(raw))
        row["signal_aggregate"] = raw.get("signal_aggregate") or {}
        row["d_score"] = raw.get("d_score")
        row["scored_at"] = raw.get("scored_at")
        row["grade"] = raw.get("grade")
        row["levels"] = raw.get("levels")
        row["levels_reason"] = raw.get("levels_reason")
        # Carried for paper/valuation.py: the brain's regime-alignment
        # subscore (0-20) and the reasoning trail behind it. `_signal_shape`
        # drops both because the SPA card does not render them.
        row["macro"] = raw.get("macro")
        row["trail"] = raw.get("trail") or {}
        read = read_rank(row, mandate, distribution)
        read.source_row = row  # type: ignore[attr-defined]
        reads.append(read)
    # Sort on the unclamped value so names pinned at 100 keep a stable,
    # evidence-based order rather than whatever the query returned.
    reads.sort(key=lambda r: (r.raw, r.value), reverse=True)
    return reads


__all__ = [
    "Component",
    "RankRead",
    "read_rank",
    "target_weight_for",
    "load_candidates",
    "load_score_distribution",
    "percentile_of",
    "distribution_is_usable",
    "score_age_days",
]
