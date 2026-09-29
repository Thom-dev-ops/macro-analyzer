"""Daily regime snapshots — writer + reader for the 90-day regime timeline.

The `macro_regimes` table exists in the schema but nothing was writing to
it. This module owns the daily-cadence classification snapshot: one row
per UTC date, keyed by that date so the writer is idempotent (re-running
the daily job on the same day updates the existing row instead of
piling on duplicates).

Consumers:
- `dashboard.desk_data.build_regime_section` — reads the last 90 days
  for the SPA's `regime.confidenceTrace` + `regime.transitions`.
- `scripts.daily_free_ingest` — calls `record_daily_regime_snapshot`
  once per run.

On first ever run, the writer backfills a plausible 84-day synthetic
history so the /home timeline chart has data immediately. That backfill
runs only when the table is empty; from then on we accumulate real
daily snapshots. Synthetic rows are tagged `classifier_version="backfill-v0"`
so they can be identified and pruned later if desired.
"""
from __future__ import annotations

import json
import random
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta


# 90-day window powers the SPA chart. Backfill = window - 1 (today gets
# the real classification).
_BACKFILL_DAYS = 84


@dataclass
class RegimeSnapshot:
    snapshot_date: date          # UTC date the classification is for
    framework_regime: str        # argmax(blend) — e.g. "commodity_led_inflation"
    confidence: float            # 0.0 - 1.0
    thesis_regime: str = ""      # e.g. "commodity_expansion"
    classifier_version: str = "" # tag ("blend-v1", "stub-v0", "backfill-v0", ...)
    # v1 additions. `blend` is the real output — `framework_regime` above
    # is just its argmax, kept so existing single-label consumers work.
    blend: dict[str, float] | None = None
    velocity: dict[str, float] | None = None
    leadership: dict[str, float] | None = None
    states: dict[str, str] | None = None
    evidence: list[str] | None = None
    coverage: float | None = None


def _classified_at_for(d: date) -> str:
    """UTC midnight ISO string for a given snapshot date. Stored in
    macro_regimes.classified_at (TEXT). Using midnight makes the
    per-date uniqueness check `substr(classified_at, 1, 10) = ?`."""
    return datetime(d.year, d.month, d.day, tzinfo=UTC).isoformat()


def _has_snapshot_for(conn: sqlite3.Connection, d: date) -> str | None:
    """Return existing regime_id for date d, or None."""
    row = conn.execute(
        "SELECT regime_id FROM macro_regimes "
        "WHERE substr(classified_at, 1, 10) = ? "
        "ORDER BY classified_at DESC LIMIT 1",
        (d.isoformat(),),
    ).fetchone()
    return row[0] if row else None


def _upsert_snapshot(conn: sqlite3.Connection, snap: RegimeSnapshot) -> str:
    """Insert or replace the snapshot for snap.snapshot_date. Returns
    the regime_id (existing if row already there, new UUID otherwise)."""
    existing_id = _has_snapshot_for(conn, snap.snapshot_date)
    regime_id = existing_id or str(uuid.uuid4())
    states = snap.states or {}
    payload: dict = {"synthetic": snap.classifier_version.startswith("backfill")}
    if snap.blend is not None:
        payload["blend"] = snap.blend
    if snap.velocity is not None:
        payload["velocity"] = snap.velocity
    if snap.leadership is not None:
        payload["leadership"] = snap.leadership
    if snap.evidence:
        payload["evidence"] = snap.evidence
    if snap.coverage is not None:
        payload["coverage"] = snap.coverage

    conn.execute(
        """
        INSERT OR REPLACE INTO macro_regimes (
            regime_id, classified_at, framework_regime, thesis_regime,
            liquidity_state, dollar_trend, rate_trend, volatility_state, breadth_state,
            confidence_score, classifier_version, evidence_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            regime_id,
            _classified_at_for(snap.snapshot_date),
            snap.framework_regime,
            snap.thesis_regime or "",
            states.get("liquidity_state"),
            states.get("dollar_trend"),
            states.get("rate_trend"),
            states.get("volatility_state"),
            states.get("breadth_state"),
            int(round(snap.confidence * 100)),  # schema is INTEGER
            snap.classifier_version or "stub-v0",
            json.dumps(payload),
        ),
    )
    return regime_id


def _table_is_empty(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) FROM macro_regimes").fetchone()
    return (row[0] or 0) == 0


def _synthesize_history(today: date, current_regime: str, current_conf: float) -> list[RegimeSnapshot]:
    """Generate a plausible 84-day back-history so the timeline chart
    has something to render on day 1. Two prior regimes fade into the
    current one; confidence ramps up over time with mild noise.
    """
    rng = random.Random(0xB1A5ED)  # deterministic — same backfill on repeats

    # Pick a plausible 3-segment arc based on the current framework.
    # Only valid framework_regime slugs (see desk_data._FRAMEWORK_REGIME_LABELS).
    if current_regime == "commodity_led_inflation":
        arc = ["transitional_chop", "risk_on_expansion", current_regime]
    elif current_regime == "risk_off_contraction":
        arc = ["risk_on_expansion", "transitional_chop", current_regime]
    elif current_regime == "risk_on_expansion":
        arc = ["risk_off_contraction", "transitional_chop", current_regime]
    else:
        arc = ["risk_on_expansion", "transitional_chop", current_regime]

    # Segment lengths: 3 uneven segments summing to _BACKFILL_DAYS.
    # Real regime durations vary a lot — pick from a spread rather than
    # equal thirds so the chart doesn't look mechanically generated.
    a = rng.randint(18, 34)              # first segment: 18-34 days
    b = rng.randint(20, 36)              # middle segment: 20-36 days
    c = _BACKFILL_DAYS - a - b           # tail runs to today
    if c < 8:                            # ensure tail is at least ~1 week
        b -= (8 - c)
        c = 8
    segments: list[tuple[str, int]] = [(arc[0], a), (arc[1], b), (arc[2], c)]

    # Per-segment confidence targets — the current regime's tail should
    # land near current_conf; earlier segments run lower + varied.
    seg_targets = [
        0.42 + rng.random() * 0.08,      # early: 0.42 - 0.50
        0.55 + rng.random() * 0.10,      # middle: 0.55 - 0.65
        max(0.55, min(0.90, current_conf)),
    ]

    snaps: list[RegimeSnapshot] = []
    day_offset = _BACKFILL_DAYS
    for seg_i, (regime, length) in enumerate(segments):
        target = seg_targets[seg_i]
        prev = seg_targets[seg_i - 1] if seg_i > 0 else 0.42
        for i in range(length):
            d = today - timedelta(days=day_offset)
            day_offset -= 1
            # smoothstep (non-linear ramp) so confidence rises with
            # inflection instead of straight lines.
            frac = i / max(1, length - 1)
            eased = frac * frac * (3 - 2 * frac)
            base = prev + (target - prev) * eased
            # Daily noise + occasional 2-day pullback (5% chance).
            noise = (rng.random() - 0.5) * 0.07
            if rng.random() < 0.05:
                noise -= 0.04
            conf = base + noise
            conf = max(0.30, min(0.92, conf))
            snaps.append(RegimeSnapshot(
                snapshot_date=d,
                framework_regime=regime,
                confidence=conf,
                classifier_version="backfill-v0",
            ))
    return snaps


def _snapshot_from_blend(rb, d: date) -> RegimeSnapshot:
    """Adapt a classifier_v1 RegimeBlend into a storable snapshot."""
    return RegimeSnapshot(
        snapshot_date=d,
        framework_regime=rb.dominant,
        confidence=rb.confidence,
        thesis_regime=rb.thesis_regime,
        classifier_version=rb.classifier_version,
        blend=rb.blend,
        velocity=rb.velocity,
        leadership=rb.leadership,
        states=rb.states,
        evidence=rb.evidence,
        coverage=rb.coverage,
    )


def classify_and_store(
    conn: sqlite3.Connection, *, as_of: date | None = None
) -> RegimeSnapshot | None:
    """Run the v1 blend classifier for `as_of` and upsert it. None if the
    classifier had too little data to produce an honest read."""
    from macro_positioning.regime.classifier_v1 import classify_regime_v1

    as_of = as_of or datetime.now(UTC).date()
    rb = classify_regime_v1(conn, as_of=as_of)
    if rb is None:
        return None
    snap = _snapshot_from_blend(rb, as_of)
    _upsert_snapshot(conn, snap)
    return snap


def replay_history(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date | None = None,
    step_days: int = 1,
    overwrite: bool = False,
) -> dict:
    """Re-derive real regime snapshots day-by-day over a date range.

    This is how a genuine regime timeline gets built: the classifier is
    point-in-time (every price and FRED read is `<= as_of`), so replaying
    it over history produces the same rows it would have produced live.

    `overwrite=False` skips dates that already carry a non-synthetic
    snapshot, so a replay can be resumed without clobbering live reads.
    Weekends/holidays with no fresh bars still classify — the classifier
    simply sees the last available closes, which is what a Sunday read is.
    """
    end = end or datetime.now(UTC).date()
    written = 0
    skipped = 0
    empty = 0
    d = start
    while d <= end:
        if not overwrite:
            existing = conn.execute(
                "SELECT classifier_version FROM macro_regimes "
                "WHERE substr(classified_at, 1, 10) = ? LIMIT 1",
                (d.isoformat(),),
            ).fetchone()
            if existing and not (existing[0] or "").startswith("backfill"):
                skipped += 1
                d += timedelta(days=step_days)
                continue
        if classify_and_store(conn, as_of=d) is None:
            empty += 1
        else:
            written += 1
        d += timedelta(days=step_days)
    conn.commit()
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "written": written,
        "skipped": skipped,
        "insufficient_data": empty,
    }


def record_daily_regime_snapshot(
    conn: sqlite3.Connection,
    *,
    seed_history: bool = True,
    hint_thesis_regime: str = "commodity_expansion",
) -> dict:
    """Idempotent daily writer.

    Primary path is the v1 blend classifier (real prices + FRED, no hints).
    `hint_thesis_regime` survives only for the legacy stub fallback, used
    when there isn't enough market data to classify honestly.

    On an empty table with `seed_history=True`, history is backfilled by
    *replaying the real classifier* over whatever price history exists —
    falling back to the old synthetic generator only if the classifier
    can't produce anything at all.
    """
    from macro_brain.agents.regime_classifier.classifier import classify_regime_stub

    today = datetime.now(UTC).date()
    seeded = 0

    if seed_history and _table_is_empty(conn):
        res = replay_history(
            conn, start=today - timedelta(days=_BACKFILL_DAYS), end=today - timedelta(days=1)
        )
        seeded = res["written"]
        if seeded == 0:
            # No usable price history at all — keep the chart populated
            # with the legacy synthetic arc rather than an empty panel.
            rr = classify_regime_stub(hint_thesis_regime=hint_thesis_regime)
            for snap in _synthesize_history(today, rr.framework_regime, rr.confidence):
                _upsert_snapshot(conn, snap)
                seeded += 1

    snap = classify_and_store(conn, as_of=today)
    if snap is not None:
        conn.commit()
        return {
            "today": today.isoformat(),
            "regime": snap.framework_regime,
            "confidence": snap.confidence,
            "blend": snap.blend,
            "classifier": snap.classifier_version,
            "backfilled": seeded,
        }

    # Fallback: not enough market data today. Record the stub read rather
    # than leaving a hole, and say so in the classifier_version.
    rr = classify_regime_stub(hint_thesis_regime=hint_thesis_regime)
    _upsert_snapshot(conn, RegimeSnapshot(
        snapshot_date=today,
        framework_regime=rr.framework_regime,
        confidence=rr.confidence,
        thesis_regime=rr.thesis_regime,
        classifier_version=rr.classifier_version or "stub-v0",
    ))
    conn.commit()
    return {
        "today": today.isoformat(),
        "regime": rr.framework_regime,
        "classifier": "stub-v0 (insufficient market data)",
        "backfilled": seeded,
    }


def load_regime_history(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    """Return the last `days` daily snapshots in chronological order.
    Each row: {date, framework_regime, confidence (0-1 float)}.
    De-duplicates: if a date has multiple rows, the most recent wins.
    """
    rows = conn.execute(
        """
        SELECT substr(classified_at, 1, 10) AS d,
               framework_regime,
               confidence_score,
               classifier_version,
               evidence_json,
               MAX(classified_at) AS latest_ts
        FROM macro_regimes
        WHERE classified_at >= date('now', ?)
        GROUP BY d
        ORDER BY d ASC
        """,
        (f"-{max(1, days)} day",),
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        payload: dict = {}
        if r[4]:
            try:
                payload = json.loads(r[4]) or {}
            except (ValueError, TypeError):
                payload = {}
        out.append({
            "date": r[0],
            "framework_regime": r[1],
            "confidence": round((r[2] or 0) / 100.0, 3),
            "classifier_version": r[3] or "",
            "synthetic": bool(payload.get("synthetic")),
            "blend": payload.get("blend"),
            "velocity": payload.get("velocity"),
            "leadership": payload.get("leadership"),
            "evidence": payload.get("evidence") or [],
            "coverage": payload.get("coverage"),
        })
    return out


def blend_trace(snapshots: list[dict]) -> dict[str, list[float | None]]:
    """Transpose per-day blends into one series per regime.

    Days classified before v1 (or by the stub) have no blend; those slots
    are None so the SPA can render a gap rather than a fabricated zero.
    """
    from macro_positioning.regime.classifier_v1 import FRAMEWORK_REGIMES

    series: dict[str, list[float | None]] = {r: [] for r in FRAMEWORK_REGIMES}
    for s in snapshots:
        blend = s.get("blend") or {}
        for regime in FRAMEWORK_REGIMES:
            v = blend.get(regime)
            series[regime].append(round(v, 4) if isinstance(v, (int, float)) else None)
    return series


def derive_transitions(snapshots: list[dict]) -> list[dict]:
    """Collapse consecutive same-regime rows into transition events.
    Returns [{date, from, to}] where `date` is the first day of the new
    regime. Uses framework-regime labels (human-friendly)."""
    from macro_positioning.dashboard.desk_data import _FRAMEWORK_REGIME_LABELS

    transitions: list[dict] = []
    prev = None
    run_len = 0
    for s in snapshots:
        cur = s.get("framework_regime")
        if cur != prev and prev is not None:
            transitions.append({
                "date": s["date"],
                "from": _FRAMEWORK_REGIME_LABELS.get(prev, prev),
                "to": _FRAMEWORK_REGIME_LABELS.get(cur, cur),
                "fromSlug": prev,
                "toSlug": cur,
                # How long the regime being left had held, and how
                # convinced the classifier was on the day it flipped.
                "priorRunDays": run_len,
                "confidence": s.get("confidence"),
                "synthetic": bool(s.get("synthetic")),
            })
            run_len = 1
        else:
            run_len += 1
        prev = cur
    return transitions


def since_days_for_current(snapshots: list[dict]) -> int:
    """How many days has the current (most recent) framework regime been
    active? Walks backwards from the end until the regime changes.
    """
    if not snapshots:
        return 0
    current = snapshots[-1]["framework_regime"]
    count = 0
    for s in reversed(snapshots):
        if s["framework_regime"] != current:
            break
        count += 1
    return count


# ---------------------------------------------------------------------------
# Smoothing — a regime you re-label every other day is not a regime
# ---------------------------------------------------------------------------

# EMA half-life in snapshots (~2 trading weeks). Short enough to catch a
# real handoff inside a month, long enough that a single hot week can't
# rename the regime.
_SMOOTH_HALF_LIFE = 10

# A challenger must beat the incumbent by this margin, for this many
# consecutive days, before the headline regime flips.
_HYSTERESIS_MARGIN = 0.04
_HYSTERESIS_DAYS = 3


def smooth_blends(snapshots: list[dict], half_life: int = _SMOOTH_HALF_LIFE) -> list[dict]:
    """Return per-day EMA-smoothed blends, chronological.

    Raw daily blends stay untouched in the DB — this is a serving-layer
    view. Days without a blend (stub/backfill rows) carry the last
    smoothed value forward rather than resetting it.
    """
    from macro_positioning.regime.classifier_v1 import FRAMEWORK_REGIMES

    alpha = 1.0 - 0.5 ** (1.0 / max(1, half_life))
    state: dict[str, float] | None = None
    out: list[dict] = []
    for s in snapshots:
        blend = s.get("blend")
        if isinstance(blend, dict) and blend:
            vals = {r: float(blend.get(r, 0.0)) for r in FRAMEWORK_REGIMES}
            state = vals if state is None else {
                r: state[r] + alpha * (vals[r] - state[r]) for r in FRAMEWORK_REGIMES
            }
        if state is None:
            out.append({"date": s["date"], "blend": None})
        else:
            total = sum(state.values()) or 1.0
            out.append({
                "date": s["date"],
                "blend": {r: round(v / total, 4) for r, v in state.items()},
            })
    return out


def dominant_with_hysteresis(
    smoothed: list[dict],
    *,
    margin: float = _HYSTERESIS_MARGIN,
    days: int = _HYSTERESIS_DAYS,
) -> list[dict]:
    """Assign a headline regime per day from smoothed blends, requiring a
    challenger to lead by `margin` for `days` running before it takes over.

    Without this the label flips on noise around a crossover — which reads
    as five regime changes in a month when it was really one handoff.
    """
    held: str | None = None
    streak_slug: str | None = None
    streak_len = 0
    out: list[dict] = []
    for row in smoothed:
        blend = row.get("blend")
        if not blend:
            out.append({"date": row["date"], "regime": held, "blend": None})
            continue
        leader = max(blend, key=lambda k: blend[k])
        if held is None:
            held = leader
        elif leader != held and blend[leader] - blend[held] >= margin:
            streak_len = streak_len + 1 if streak_slug == leader else 1
            streak_slug = leader
            if streak_len >= days:
                held = leader
                streak_len = 0
                streak_slug = None
        else:
            streak_len = 0
            streak_slug = None
        out.append({"date": row["date"], "regime": held, "blend": blend})
    return out
