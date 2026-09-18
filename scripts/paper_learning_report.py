#!/usr/bin/env python3
"""What the paper book has learned, and what it will do about it.

Prints the loop's per-sleeve adjustments NEXT TO the raw statistics they
were computed from, so the reader can check the arithmetic by eye. This
is verification step 2 of 3 for the recursive loop: the module alone,
against the real book, before it touches a tick.

    .venv/bin/python scripts/paper_learning_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from macro_positioning.paper import store                       # noqa: E402
from macro_positioning.paper.learning import adjustments, shrink  # noqa: E402
from macro_positioning.paper.models import load_mandate         # noqa: E402
from macro_positioning.paper.performance import performance     # noqa: E402


def main() -> int:
    pf = store.get_portfolio()
    if pf is None:
        print("no paper book", file=sys.stderr)
        return 2
    m = pf.mandate
    a = adjustments(pf.portfolio_id, mandate=m)
    raw = {s["id"]: s for s in performance(pf.portfolio_id)["bySleeve"]}

    print(f"paper learning — {a.as_of[:16]}  window {a.window_days}d  "
          f"w = n/(n+{a.shrink_n})  priors engage at n>={a.min_n}  rank cap ±{a.rank_cap:g}\n")
    hdr = (f"{'sleeve':<26} {'n':>3} {'win%':>5} {'avgR':>6} │ {'w':>5} {'rankΔ':>6} "
           f"= clip(avgR)×8×w │ {'stops':>5} {'slipR':>6} {'risk×':>6} │ {'peaks':>5} {'prior':>6}")
    print(hdr); print("─" * len(hdr))
    for r in sorted(a.by_sleeve.values(), key=lambda r: -r.n):
        clipped = max(-2.0, min(2.0, r.avg_r)) if r.avg_r is not None else 0.0
        check = max(-a.rank_cap, min(a.rank_cap, clipped * 8 * r.w))
        ok = "✓" if abs(check - r.rank_delta) < 1e-6 else "✗ MISMATCH"
        prior = f"{r.hold_prior_days:.1f}d" if r.hold_prior_days is not None else (
            f"({r.peak_days_median:.0f}d)" if r.peak_days_median is not None else "—")
        print(f"{r.label:<26} {r.n:>3} {r.win_rate or 0:>5.0f} "
              f"{r.avg_r if r.avg_r is not None else 0:>+6.2f} │ {r.w:>5.2f} {r.rank_delta:>+6.2f} "
              f"  {clipped:+.2f}×8×{r.w:.2f}={check:+.2f} {ok} │ {r.n_stops:>5} "
              f"{r.stop_slip_r if r.stop_slip_r is not None else 0:>6.2f} {r.risk_mult:>6.3f} │ "
              f"{r.n_peaks:>5} {prior:>6}")
        # cross-check n and win rate against performance.py's independent rollup
        p = raw.get(r.sleeve_id)
        if p and (p.get("trades") != r.n):
            print(f"    ⚠ performance.py counts {p.get('trades')} trades in this sleeve "
                  f"(window differs: performance is all-time, learning is {a.window_days}d)")
    print()
    print("what changes on the next tick:")
    for r in sorted(a.by_sleeve.values(), key=lambda r: r.rank_delta):
        parts = []
        if abs(r.rank_delta) >= 0.05:
            parts.append(f"rank {r.rank_delta:+.1f}")
        if r.risk_mult > 1.0:
            parts.append(f"size ÷{r.risk_mult:.3f}")
        if r.hold_prior_days is not None:
            parts.append(f"hold prior {r.hold_prior_days:.0f}d")
        if parts:
            print(f"  {r.label:<26} " + ", ".join(parts))
    quiet = [r.label for r in a.by_sleeve.values()
             if abs(r.rank_delta) < 0.05 and r.risk_mult <= 1.0 and r.hold_prior_days is None]
    if quiet:
        print(f"  (no change: {', '.join(quiet)})")
    print()
    print(f"attribution rows (report only): {len(a.attribution)}   ladder (report only): {a.ladder}")
    print(f"promotable to the shared score (n>={a.min_n}): "
          f"{[r.label for r in a.by_sleeve.values() if r.promotable] or 'none yet'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
