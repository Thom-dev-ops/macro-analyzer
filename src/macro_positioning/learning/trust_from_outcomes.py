"""Turn measured call outcomes into the trust weight the desk actually blends.

The gap this closes
-------------------
`call_accuracy.backtest_calls()` scores every chart call an author ever
posted and writes it to `call_outcomes`. The COHORT paper book already
reads that, via `paper/cohort.author_edge()`, so a caller whose measured
alpha decays gets sized down inside days of the next backtest.

The desk-wide composite did not. `input_authors.trust_weight` — the
number `signals/aggregation.py` multiplies every signal by — was seeded
by hand at 1.5 for the whole Telegram roster and never moved again. In
Sep 2026 that meant Market Traders (+3.4% measured alpha over 102 calls)
carried 1.4 while Big_Nuts (−0.0% over 546) carried 1.5: the blend was
weighting them in the wrong order, using numbers nobody had revisited
since the seed. This module is the missing edge of that loop.

What it measures, and what it refuses to
----------------------------------------
ALPHA, not win rate — a fixed-horizon directional win rate in a trending
crypto window is mostly beta, and would hand the highest trust to whoever
posts the most longs in a bull market.

REAL ASSETS only. The DEX pairs and the Coinbase alt tail stay in
`call_outcomes` as evidence about the caller but get no vote here: an
edge on a microcap the desk cannot size into is not an edge it can spend,
and those rows are both the majority of the corpus and the noisiest part
of it.

SHRUNK toward the seed. A weight is `prior + (measured - prior) * n/(n+k)`,
so eight calls barely move anything and five hundred move it most of the
way. Without this a new author with three lucky calls outranks a
measured veteran, which is how a calibration loop turns into a random
number generator.

BOUNDED. The result is clamped, because a measurement this noisy should
never be allowed to zero a source out or make it dominant on its own.

PER PERSON, not per author_id. The same trader appears under one id per
channel that relays them — Big_Nuts is both `feather-hands:big-nuts` and
`market-traders:big-nuts`. Weighting those separately splits the evidence
and then weights the same human differently depending on which channel
happened to carry the post: on the first run of this module Big_Nuts'
Feather Hands id fell to 1.04 on 537 measured calls while his Market
Traders id sat untouched at the 1.5 seed. Evidence is pooled across an
author's aliases and the resulting weight is written to all of them.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

from macro_positioning.core.settings import settings


logger = logging.getLogger(__name__)

# The seeded weight every Telegram author starts at. A source with no
# measurement stays here rather than being guessed at.
PRIOR = 1.5
# Shrinkage constant: n/(n+K) of the way from the prior to the measurement.
# At K=60 a 60-call sample gets half its measured move, 540 calls get 90%.
SHRINK_K = 60.0
# Alpha (percentage points) that maps to the top of the band. ±5% is a
# wide spread for a market-relative number over these horizons.
ALPHA_FULL_SCALE = 5.0
# Hard bounds. Trust is a multiplier on every signal a source emits, so
# the blend must stay legible even if the backtest goes strange.
MIN_WEIGHT, MAX_WEIGHT = 0.5, 2.5
# Below this many priceable, real-asset calls, do not move the weight.
MIN_CALLS = 20


def _identity_map(conn: sqlite3.Connection) -> dict[str, str]:
    """author_id -> the id that represents that PERSON.

    Aliases are matched on display_name within a channel family: the
    roster records `parent_channel` when a channel is a second banner for
    the same crowd, and a person's name is stable across those banners.
    The alias with the most measured calls becomes the canonical id, so
    the pooled weight is attributed where the evidence actually is.
    """
    rows = list(conn.execute(
        "SELECT author_id, display_name, channel, parent_channel FROM input_authors"
    ))
    counts = dict(conn.execute(
        "SELECT author_id, COUNT(*) FROM call_outcomes "
        "WHERE real_asset = 1 GROUP BY author_id"
    ))
    by_name: dict[str, list[str]] = {}
    families: dict[str, set[str]] = {}
    for author_id, display_name, channel, parent in rows:
        fam = (parent or channel or "").strip().lower()
        key = (display_name or "").strip().lower()
        if not key:
            continue
        by_name.setdefault(key, []).append(author_id)
        families.setdefault(key, set()).add(fam)

    out: dict[str, str] = {}
    for key, ids in by_name.items():
        # Only pool when the aliases really are one crowd. Two unrelated
        # desks that happen to share a handle must stay separate.
        if len(ids) > 1 and len(families[key]) > 1:
            continue
        canonical = max(ids, key=lambda a: counts.get(a, 0))
        for a in ids:
            out[a] = canonical
    return out


@dataclass
class TrustUpdate:
    author_id: str
    display_name: str
    n_calls: int
    avg_alpha_pct: float
    measured: float
    before: float
    after: float
    aliases: tuple[str, ...] = ()

    @property
    def moved(self) -> bool:
        return abs(self.after - self.before) >= 0.005


@dataclass
class TrustRun:
    ran_at: str
    considered: int = 0
    updated: int = 0
    skipped_small: int = 0
    updates: list[TrustUpdate] = field(default_factory=list)

    def report(self) -> str:
        lines = [
            f"trust from outcomes — {self.ran_at}",
            f"  {self.considered} authors with measured calls, "
            f"{self.updated} weights moved, {self.skipped_small} under the "
            f"{MIN_CALLS}-call floor",
        ]
        for u in sorted(self.updates, key=lambda x: -abs(x.after - x.before)):
            if not u.moved:
                continue
            arrow = "↑" if u.after > u.before else "↓"
            alias = f" [{len(u.aliases)} ids]" if len(u.aliases) > 1 else ""
            lines.append(
                f"  {arrow} {u.display_name[:26]:<26} {u.before:.2f} → {u.after:.2f}"
                f"   ({u.avg_alpha_pct:+.2f}% alpha over {u.n_calls} calls){alias}"
            )
        return "\n".join(lines)


def _measured_weight(avg_alpha_pct: float) -> float:
    """Map measured alpha onto the weight scale, before shrinkage.

    Zero alpha lands exactly on the prior — a source that matches the
    market is worth its seed, not a penalty. From there it moves linearly
    to the bounds at ±ALPHA_FULL_SCALE.
    """
    frac = max(-1.0, min(1.0, avg_alpha_pct / ALPHA_FULL_SCALE))
    span = (MAX_WEIGHT - PRIOR) if frac >= 0 else (PRIOR - MIN_WEIGHT)
    return PRIOR + frac * span


def recompute_trust_from_outcomes(
    *,
    window_days: Optional[int] = None,
    db_path: Optional[Path] = None,
    dry_run: bool = False,
) -> TrustRun:
    """Move `input_authors.trust_weight` toward each author's measured alpha.

    `window_days` limits the evidence to recent calls; None uses all of
    it. Returns the run even in `dry_run`, so a caller can show the moves
    before committing them.
    """
    from macro_positioning.learning.call_accuracy import source_accuracy

    run = TrustRun(ran_at=datetime.now(UTC).isoformat())
    rows = source_accuracy(
        window_days=window_days, db_path=db_path, real_assets_only=True
    )

    path = str(db_path or settings.sqlite_path)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        current = {
            r["author_id"]: (r["trust_weight"], r["display_name"])
            for r in conn.execute(
                "SELECT author_id, trust_weight, display_name FROM input_authors"
            )
        }
        identity = _identity_map(conn)
        aliases_of: dict[str, list[str]] = {}
        for alias, canonical in identity.items():
            aliases_of.setdefault(canonical, []).append(alias)

        # Pool the evidence per PERSON before weighing any of it. An alpha
        # is a per-call average, so recombining two aliases means weighting
        # each by its own call count, not averaging the averages.
        pooled: dict[str, dict] = {}
        for r in rows:
            author_id = r["author_id"]
            if author_id not in current:
                continue
            alpha, n = r.get("avg_alpha_pct"), (r.get("n_priceable") or 0)
            if alpha is None or n <= 0:
                continue
            canonical = identity.get(author_id, author_id)
            p = pooled.setdefault(canonical, {"alpha_sum": 0.0, "n": 0})
            p["alpha_sum"] += float(alpha) * n
            p["n"] += n

        for canonical, p in pooled.items():
            n = p["n"]
            alpha = p["alpha_sum"] / n
            run.considered += 1
            if n < MIN_CALLS:
                run.skipped_small += 1
                continue

            targets = aliases_of.get(canonical) or [canonical]
            before = current.get(canonical, (None, None))[0]
            before = PRIOR if before is None else float(before)
            measured = _measured_weight(alpha)
            # Shrink from the SEED, never from the stored weight. Anchoring
            # on the stored value makes this an exponential moving average:
            # run it daily and every source ratchets all the way to its raw
            # measurement, which is exactly the shrinkage this was meant to
            # apply. From the prior it is idempotent — the weight is a pure
            # function of the evidence, so running twice changes nothing and
            # a weight can always be re-derived from `call_outcomes`.
            after = PRIOR + (measured - PRIOR) * (n / (n + SHRINK_K))
            after = round(max(MIN_WEIGHT, min(MAX_WEIGHT, after)), 4)

            u = TrustUpdate(
                author_id=canonical,
                display_name=current.get(canonical, (None, canonical))[1] or canonical,
                n_calls=n, avg_alpha_pct=alpha,
                measured=round(measured, 4), before=before, after=after,
                aliases=tuple(sorted(targets)),
            )
            run.updates.append(u)
            # Write when ANY alias is out of step, not only when the
            # canonical weight moved. Gating on the canonical alone strands
            # the other ids: Big_Nuts' Feather Hands id was already at its
            # converged 1.04, so the pass was a no-op and his Market
            # Traders id kept the 1.5 seed — the same person weighted two
            # ways depending on which channel relayed the post.
            stale = [
                a for a in targets
                if current.get(a, (None, None))[0] is None
                or abs(float(current[a][0]) - after) >= 0.0005
            ]
            if stale:
                run.updated += 1
                if not dry_run:
                    conn.executemany(
                        "UPDATE input_authors SET trust_weight = ? WHERE author_id = ?",
                        [(after, a) for a in stale],
                    )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    logger.info(
        "trust_from_outcomes: %d considered, %d moved, %d under the call floor",
        run.considered, run.updated, run.skipped_small,
    )
    return run


__all__ = ["recompute_trust_from_outcomes", "TrustRun", "TrustUpdate",
           "PRIOR", "SHRINK_K", "MIN_CALLS"]
