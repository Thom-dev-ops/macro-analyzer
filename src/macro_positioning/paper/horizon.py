"""How long is this trade meant to take? Derived per position, not mandated.

The book aims for multi-week holds because that is where its profit
factor lives — but "multi-week" is not one number. A swing call on a
crypto name and a strategic call on a miner are different trades, and
the desk's own signals say which is which. So the hold horizon is set
at fill from the inputs on that trade, stored on the position, and used
to decide how long *opinions* (rank decay, support collapse, rotation)
have to wait and how many ticks they must persist before they can close
it.

Three inputs, in priority order:

  1. the signals' stated horizon — `dominant_horizon` on the persisted
     aggregate (intraday / swing / position / strategic);
  2. the setup — a breakout or structure trade carries a longer prior
     than mechanical rails;
  3. the sleeve's own record — the learning loop's days-to-peak prior,
     once that sleeve has n >= 30 closed trades. Absent below that.

None of this delays a target hit, a stop, or the trail. Reaching the
target closes the trade on day 2 or day 40 alike.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from macro_positioning.paper.models import Mandate


# Days a signal horizon implies. `intraday` is deliberately absent: an
# intraday read does not set a swing book's horizon (the blend already
# weights it 0.3x), so it falls through to the setup and the fallback.
_SIGNAL_DAYS = {"swing": 10.0, "position": 30.0, "strategic": 60.0}

# Setup multipliers on top of the signal horizon. Structure and breakout
# trades are the ones that run; mechanical rails are the ones that don't.
_SETUP_MULT = {
    "breakout_20d": 1.25,
    "breakdown_20d": 1.25,
    "pullback_support": 1.0,
    "rally_resistance": 1.0,
    "mechanical_v0": 0.8,
}
_STRUCTURE_HINTS = ("structure",)      # in `setup` text when v2 rails were used


@dataclass
class Horizon:
    expected_days: float
    min_hold_days: float
    confirm_ticks: int
    basis: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "expectedDays": round(self.expected_days, 1),
            "minHoldDays": round(self.min_hold_days, 1),
            "confirmTicks": self.confirm_ticks,
            "basis": list(self.basis),
        }


def derive_horizon(
    row: dict,
    mandate: Mandate,
    *,
    sleeve_prior_days: Optional[float] = None,
    sleeve_prior_n: int = 0,
) -> Horizon:
    """Set a position's horizon from what the desk knows about the trade."""
    basis: list[str] = []
    agg = row.get("signal_aggregate") or {}
    dom = (agg.get("dominant_horizon") or "").lower() or None

    if dom in _SIGNAL_DAYS:
        days = _SIGNAL_DAYS[dom]
        basis.append(f"signals read {dom} → {days:.0f}d")
    else:
        days = float(mandate.horizon_fallback_days)
        basis.append(
            f"no directional horizon on the signals ({dom or 'none'}) → "
            f"fallback {days:.0f}d"
        )

    method = (row.get("levelMethod") or "").lower()
    setup = (row.get("setup") or "").lower()
    mult = _SETUP_MULT.get(method)
    if mult is None and any(h in setup for h in _STRUCTURE_HINTS):
        mult = 1.25
    if mult is not None and mult != 1.0:
        days *= mult
        basis.append(f"{method or setup or 'setup'} ×{mult:g}")

    # The sleeve's own record earns a say only once it has one.
    if sleeve_prior_days and sleeve_prior_n >= mandate.learning_min_n:
        w = sleeve_prior_n / (sleeve_prior_n + mandate.learning_shrink_n)
        blended = days * (1 - w) + float(sleeve_prior_days) * w
        basis.append(
            f"sleeve record: peak at {sleeve_prior_days:.0f}d over n={sleeve_prior_n} "
            f"(w={w:.2f}) → {blended:.0f}d"
        )
        days = blended

    lo, hi = mandate.min_hold_bounds
    min_hold = max(lo, min(hi, days * mandate.min_hold_fraction))
    tlo, thi = mandate.confirm_ticks_bounds
    ticks = int(max(tlo, min(thi, round(days / mandate.confirm_days_divisor))))
    return Horizon(expected_days=days, min_hold_days=min_hold, confirm_ticks=ticks, basis=basis)


__all__ = ["Horizon", "derive_horizon"]
