"""The recursive loop — what the book's own record changes about its next trade.

Nothing here is a model. It is a set of queries over the book's own
tables — closed positions, the fill log, and the per-tick snapshots —
that produce a small set of **shrunk, logged** adjustments the engine
reads on every tick:

    L1  rank      a sleeve's realized edge moves the rank of its next
                  candidate, capped at ±`learning_rank_cap` points
    L2  sizing    a sleeve's measured fill-beyond-stop inflates the risk
                  its next position is sized against
    L3  horizon   a sleeve's days-to-peak-R becomes a prior in the hold
                  derivation — only once it has n >= `learning_min_n`

Every stat is weighted `w = n / (n + learning_shrink_n)` before it is
allowed to change anything. With shrink_n = 30: seven trades are a nudge
(w = 0.19), thirty are half-trusted, ninety are three-quarters. n = 0 is
zero by construction. Adjustments are re-derived from the trailing
window on every tick — nothing accumulates in hidden state, so a bad
month un-learns itself as it rolls out of the window.

Every adjustment lands on the decision row as a `Component` carrying n,
the raw statistic and w. A rank that moved because of the book's own
history says so, in the same place the KOL blend and the composed view
say why they moved it.

Two things are computed but deliberately do NOT feed back in v1, because
they need a record of their own first: per-rung ladder statistics, and
input attribution (which of rank / support / signal / regime moved first
ahead of a turn). Both are reported on `/api/paper/learning`.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

from macro_positioning.paper import store
from macro_positioning.paper.models import Mandate
from macro_positioning.paper.performance import build_trade_records
from macro_positioning.paper.rank import Component
from macro_positioning.paper.sleeves import sleeve_for_ticker


logger = logging.getLogger(__name__)


def shrink(n: int, k: int) -> float:
    """w = n / (n + k). The whole loop's humility in one line."""
    n = max(0, int(n))
    return n / (n + k) if (n + k) > 0 else 0.0


@dataclass
class SleeveRecord:
    sleeve_id: str
    label: str
    n: int = 0
    wins: int = 0
    losses: int = 0
    avg_r: Optional[float] = None
    win_rate: Optional[float] = None          # 0..100
    avg_win_pct: Optional[float] = None
    avg_loss_pct: Optional[float] = None
    n_stops: int = 0
    stop_slip_r: Optional[float] = None       # mean fill beyond the stop, in R
    n_peaks: int = 0
    peak_days_median: Optional[float] = None  # winners: days to their peak R
    hold_days_median: Optional[float] = None
    w: float = 0.0
    # The adjustments this record produces.
    rank_delta: float = 0.0
    risk_mult: float = 1.0
    hold_prior_days: Optional[float] = None   # None below min_n
    promotable: bool = False

    def as_dict(self) -> dict:
        return {
            "sleeveId": self.sleeve_id, "label": self.label,
            "n": self.n, "wins": self.wins, "losses": self.losses,
            "avgR": round(self.avg_r, 3) if self.avg_r is not None else None,
            "winRate": round(self.win_rate, 1) if self.win_rate is not None else None,
            "avgWinPct": round(self.avg_win_pct, 2) if self.avg_win_pct is not None else None,
            "avgLossPct": round(self.avg_loss_pct, 2) if self.avg_loss_pct is not None else None,
            "nStops": self.n_stops,
            "stopSlipR": round(self.stop_slip_r, 3) if self.stop_slip_r is not None else None,
            "nPeaks": self.n_peaks,
            "peakDaysMedian": round(self.peak_days_median, 1) if self.peak_days_median is not None else None,
            "holdDaysMedian": round(self.hold_days_median, 1) if self.hold_days_median is not None else None,
            "w": round(self.w, 3),
            "rankDelta": round(self.rank_delta, 2),
            "riskMult": round(self.risk_mult, 3),
            "holdPriorDays": round(self.hold_prior_days, 1) if self.hold_prior_days is not None else None,
            "promotable": self.promotable,
        }


@dataclass
class Adjustments:
    as_of: str
    window_days: int
    shrink_n: int
    min_n: int
    rank_cap: float
    by_sleeve: dict[str, SleeveRecord] = field(default_factory=dict)
    attribution: list[dict] = field(default_factory=list)      # report only
    ladder: dict = field(default_factory=dict)                  # report only

    # ── What the engine reads ─────────────────────────────────────────

    def rank_component(self, ticker: str) -> Optional[Component]:
        rec = self.by_sleeve.get(sleeve_for_ticker(ticker).id)
        if rec is None or rec.n == 0 or abs(rec.rank_delta) < 0.05:
            return None
        return Component(
            "book_record", rec.rank_delta,
            f"book's own record on {rec.label}: {rec.avg_r:+.2f}R avg over {rec.n} "
            f"trades, {rec.win_rate:.0f}% win (w={rec.w:.2f})",
        )

    def risk_multiplier(self, ticker: str) -> float:
        rec = self.by_sleeve.get(sleeve_for_ticker(ticker).id)
        return rec.risk_mult if rec else 1.0

    def hold_prior(self, ticker: str) -> dict:
        rec = self.by_sleeve.get(sleeve_for_ticker(ticker).id)
        if rec is None or rec.hold_prior_days is None:
            return {}
        return {"peak_days": rec.hold_prior_days, "n": rec.n_peaks}

    def as_dict(self) -> dict:
        return {
            "asOf": self.as_of, "windowDays": self.window_days,
            "shrinkN": self.shrink_n, "minN": self.min_n, "rankCap": self.rank_cap,
            "sleeves": [r.as_dict() for r in sorted(self.by_sleeve.values(), key=lambda r: -r.n)],
            "attribution": self.attribution,
            "ladder": self.ladder,
        }


# ── The measurements ──────────────────────────────────────────────────


def _stop_slippage(
    conn: sqlite3.Connection, portfolio_id: str, since: str
) -> dict[str, list[float]]:
    """Per sleeve: how far beyond the stop each stop-out actually filled, in R.

    Positive = filled beyond the stop (the tick gapped through it). This
    is the number that turns a planned 1R loss into a realized 1.3R.
    """
    rows = conn.execute(
        """
        SELECT o.ticker, o.ref_price AS mark, p.stop, p.initial_risk, p.side
          FROM paper_orders o JOIN paper_positions p ON p.position_id = o.position_id
         WHERE o.portfolio_id = ? AND o.action = 'EXIT' AND o.intent = 'stop_hit'
           AND o.filled_at >= ? AND p.initial_risk > 0 AND p.stop IS NOT NULL
        """,
        (portfolio_id, since),
    ).fetchall()
    out: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        sign = 1.0 if (r["side"] or "").upper() == "LONG" else -1.0
        gap_r = (float(r["stop"]) - float(r["mark"])) * sign / float(r["initial_risk"])
        out[sleeve_for_ticker(r["ticker"]).id].append(max(0.0, gap_r))
    return out


def _peak_days(
    conn: sqlite3.Connection, portfolio_id: str, since: str
) -> tuple[dict[str, list[float]], list[dict]]:
    """Per sleeve: for each CLOSED winner, days from open to its peak R —
    from the per-tick snapshots. Also returns the attribution rows."""
    rows = conn.execute(
        """
        SELECT s.position_id, s.ticker, s.days_held, s.unrealized_r, s.rank, s.support,
               s.signal_direction, s.regime_label, s.taken_at,
               p.closed_at, p.realized_pnl, p.side
          FROM paper_position_snapshots s
          JOIN paper_positions p ON p.position_id = s.position_id
         WHERE s.portfolio_id = ? AND p.status = 'closed' AND p.closed_at >= ?
         ORDER BY s.position_id, s.taken_at
        """,
        (portfolio_id, since),
    ).fetchall()
    by_pos: dict[str, list] = defaultdict(list)
    for r in rows:
        by_pos[r["position_id"]].append(r)

    peaks: dict[str, list[float]] = defaultdict(list)
    attribution: list[dict] = []
    for pid, snaps in by_pos.items():
        with_r = [s for s in snaps if s["unrealized_r"] is not None]
        if not with_r:
            continue
        peak = max(with_r, key=lambda s: s["unrealized_r"])
        last = with_r[-1]
        won = (peak["realized_pnl"] or 0.0) > 0
        sid = sleeve_for_ticker(peak["ticker"]).id
        if won and peak["days_held"] is not None:
            peaks[sid].append(float(peak["days_held"]))
        attribution.append({
            "positionId": pid, "ticker": peak["ticker"], "sleeveId": sid, "won": won,
            "peakR": round(peak["unrealized_r"], 2), "peakDay": peak["days_held"],
            "exitDay": last["days_held"],
            "rankAtPeak": peak["rank"], "rankAtExit": last["rank"],
            "supportAtPeak": peak["support"], "supportAtExit": last["support"],
            "signalAtPeak": peak["signal_direction"], "signalAtExit": last["signal_direction"],
            "regime": peak["regime_label"],
        })
    return peaks, attribution


def _ladder_stats(conn: sqlite3.Connection, portfolio_id: str, since: str) -> dict:
    """Report only: how often each rung is reached, and what the runner
    did after. Feeds nothing yet — the ladder needs a record first."""
    rows = conn.execute(
        """
        SELECT intent, COUNT(*) AS n, AVG(realized_pnl) AS avg_realized
          FROM paper_orders
         WHERE portfolio_id = ? AND filled_at >= ?
           AND intent IN ('rung_taken', 'target_reached', 'near_miss',
                          'trail_giveback', 'trail_round_trip')
         GROUP BY intent
        """,
        (portfolio_id, since),
    ).fetchall()
    return {r["intent"]: {"n": r["n"], "avgRealized": round(r["avg_realized"] or 0.0, 2)}
            for r in rows}


# ── Composition ───────────────────────────────────────────────────────


def adjustments(
    portfolio_id: str,
    *,
    mandate: Mandate,
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Adjustments:
    """Everything the book's record says about its next trade, shrunk."""
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=mandate.learning_window_days)).isoformat()
    k, min_n, cap = mandate.learning_shrink_n, mandate.learning_min_n, mandate.learning_rank_cap
    out = Adjustments(as_of=now.isoformat(), window_days=mandate.learning_window_days,
                      shrink_n=k, min_n=min_n, rank_cap=cap)

    records = [
        t for t in build_trade_records(portfolio_id, db_path=db_path, marks={})
        if t.closed_at and t.closed_at >= since
    ]
    by_sleeve: dict[str, list] = defaultdict(list)
    for t in records:
        by_sleeve[t.sleeve_id].append(t)

    with store.connect(db_path, readonly=True) as conn:
        slips = _stop_slippage(conn, portfolio_id, since)
        peaks, attribution = _peak_days(conn, portfolio_id, since)
        out.ladder = _ladder_stats(conn, portfolio_id, since)
    out.attribution = attribution

    for sid in set(by_sleeve) | set(slips) | set(peaks):
        trades = by_sleeve.get(sid, [])
        label = trades[0].sleeve_label if trades else sleeve_for_ticker("").label
        rec = SleeveRecord(sleeve_id=sid, label=label, n=len(trades))
        if trades:
            rs = [t.r_multiple for t in trades if t.r_multiple is not None]
            wins = [t for t in trades if t.realized > 0]
            losses = [t for t in trades if t.realized < 0]
            rec.wins, rec.losses = len(wins), len(losses)
            rec.win_rate = 100.0 * len(wins) / len(trades)
            rec.avg_r = statistics.fmean(rs) if rs else None
            rec.avg_win_pct = statistics.fmean(t.realized_pct for t in wins) if wins else None
            rec.avg_loss_pct = statistics.fmean(t.realized_pct for t in losses) if losses else None
            rec.hold_days_median = statistics.median(
                [t.hold_days for t in trades if t.hold_days is not None] or [0.0]
            )
        rec.w = shrink(rec.n, k)

        # L1 — rank. avgR clipped to ±2 so one 8R outlier is not a mandate.
        if rec.avg_r is not None and rec.n > 0:
            rec.rank_delta = max(-cap, min(cap, max(-2.0, min(2.0, rec.avg_r)) * 8.0 * rec.w))

        # L2 — sizing. Slippage shrunk on its OWN n (stops, not all trades).
        if slips.get(sid):
            rec.n_stops = len(slips[sid])
            rec.stop_slip_r = statistics.fmean(slips[sid])
            rec.risk_mult = 1.0 + rec.stop_slip_r * shrink(rec.n_stops, k)

        # L3 — horizon prior. Engages only once the sleeve has a record.
        if peaks.get(sid):
            rec.n_peaks = len(peaks[sid])
            rec.peak_days_median = statistics.median(peaks[sid])
            if rec.n_peaks >= min_n:
                rec.hold_prior_days = rec.peak_days_median

        # Promotion to the shared score is gated on n alone in v1; the
        # out-of-sample predictive check is the next commit.
        rec.promotable = rec.n >= min_n
        out.by_sleeve[sid] = rec

    return out


__all__ = ["Adjustments", "SleeveRecord", "adjustments", "shrink"]
