"""Confluence-score rubric — 0..8 with three subscores.

  Pattern    0..3  — 0 none / 1 weak / 2 textbook / 3 textbook + multi-timeframe
  Fib        0..3  — 0 none / 1 white confluence / 2 yellow / 3 green at breakout level
  Indicator  0..2  — 0 mixed-or-against / 1 partial / 2 full (MACD + RSI + Squeeze all agree)
                     `indicator_subscore()` derives this from the desk's own
                     MACD(14,26,9) + TTM Squeeze pane; `mtf_indicator_subscore()`
                     does it across timeframes and takes the weakest.

Tier mapping (sourced from config/risk_caps.json::confluence_tiers):
  0..insufficient_max   → "insufficient"   (do not trade)
  standard_min..below_high → "standard"     (3..5% allocation)
  high_conviction_min..  → "high_conviction" (7.5..8% allocation; rare)

Pure compute. No DB, no I/O. The remap from existing TradeRecord 1..5
scores into 0..8 lives in `from_legacy_score()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from macro_positioning.rules import load_caps


Tier = Literal["insufficient", "standard", "high_conviction"]


PATTERN_MIN, PATTERN_MAX = 0, 3
FIB_MIN, FIB_MAX = 0, 3
INDICATOR_MIN, INDICATOR_MAX = 0, 2


@dataclass(frozen=True)
class ConfluenceBreakdown:
    pattern: int
    fib: int
    indicator: int
    total: int
    tier: Tier

    def as_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "fib": self.fib,
            "indicator": self.indicator,
            "total": self.total,
            "tier": self.tier,
        }


def score_confluence(
    pattern: int,
    fib: int,
    indicator: int,
    *,
    caps_path: str | None = None,
) -> ConfluenceBreakdown:
    """Compose the three subscores into a total + tier.

    Each subscore is clamped to its declared range so callers can't
    smuggle in out-of-band values that distort downstream tier math.
    """
    p = _clamp(pattern, PATTERN_MIN, PATTERN_MAX)
    f = _clamp(fib, FIB_MIN, FIB_MAX)
    i = _clamp(indicator, INDICATOR_MIN, INDICATOR_MAX)
    total = p + f + i
    tier = tier_for_score(total, caps_path=caps_path)
    return ConfluenceBreakdown(pattern=p, fib=f, indicator=i, total=total, tier=tier)


def tier_for_score(total: int, *, caps_path: str | None = None) -> Tier:
    caps = load_caps(caps_path) if caps_path else load_caps()
    t = caps["confluence_tiers"]
    if total >= t["high_conviction_min"]:
        return "high_conviction"
    if total >= t["standard_min"]:
        return "standard"
    return "insufficient"


# ---------------------------------------------------------------------------
# Indicator subscore from the desk's own MACD + TTM Squeeze pane
#
# The rubric line is "0 mixed-or-against / 1 partial / 2 full (MACD + RSI +
# Squeeze all agree)". This derives it from a computed pane
# (`prices.technicals.indicator_pane`) so the subscore stops being typed in
# by hand. It reports a verdict per leg plus the reasons, so a score can
# always be argued with.
#
# Each leg is read the way he reads it on the chart: the MACD histogram's
# colour state, RSI against its own 14-period signal MA (NOT the 30/70
# bands), and the squeeze. The roll-up from three verdicts to 0..2 is
# scoring policy and is meant to be tuned against outcomes; what it must
# never do is invent a reading the pane didn't produce, so a missing leg is
# `unavailable` and can never count as agreement.
#
# The squeeze is direction-agnostic on purpose — compression says energy is
# stored, not which way it goes. It can support a side but never oppose one;
# direction comes from the histogram.
# ---------------------------------------------------------------------------

Verdict = Literal["agree", "neutral", "against", "unavailable"]


def _macd_verdict(macd: dict | None, side: str) -> tuple[Verdict, str]:
    if not macd:
        return "unavailable", "no MACD (short history)"
    state, cross = macd["hist_state"], macd["cross"]
    if side == "long":
        if state == "rising_positive":
            return "agree", "histogram rising above zero"
        if state == "falling_negative":
            return "against", "histogram falling below zero"
        if cross == "bull_cross":
            return "agree", "fresh bullish histogram cross"
        return "neutral", f"histogram {state.replace('_', ' ')}"
    if state == "falling_negative":
        return "agree", "histogram falling below zero"
    if state == "rising_positive":
        return "against", "histogram rising above zero"
    if cross == "bear_cross":
        return "agree", "fresh bearish histogram cross"
    if state == "recovering_negative":
        return "against", "histogram recovering off the lows"
    return "neutral", f"histogram {state.replace('_', ' ')}"


def _rsi_verdict(rsi: dict | None, side: str) -> tuple[Verdict, str]:
    """RSI read the way he reads it: against its own signal MA.

    "RSI below its MA" = momentum has ceased, whatever the absolute level —
    so that, not a 30/70 band, decides agree vs against. The extended /
    washed-out guards only ever downgrade an agreement to neutral; they
    never manufacture opposition on their own.
    """
    if not rsi:
        return "unavailable", "no RSI (short history)"
    val, ma, above = rsi["rsi"], rsi["rsi_signal"], rsi["above_signal"]
    where = f"RSI {val:.0f} {'>' if above else '<'} MA {ma:.0f}"
    if side == "long":
        if not above:
            return "against", f"{where} — momentum ceased"
        if rsi["extended"]:
            return "neutral", f"{where} but extended"
        return "agree", where
    if above:
        return "against", f"{where} — momentum returning"
    if rsi["washed_out"]:
        return "neutral", f"{where} but washed out"
    return "agree", where


def _squeeze_verdict(squeeze: dict | None) -> tuple[Verdict, str]:
    if not squeeze:
        return "unavailable", "no squeeze (short history)"
    if squeeze["fired"]:
        return "agree", "squeeze just fired"
    if squeeze["on"]:
        return "agree", f"squeeze on ({squeeze['bars_in_squeeze']} bars coiled)"
    return "neutral", "no squeeze"


def indicator_subscore(pane: dict, side: str) -> dict:
    """Derive the 0..2 Indicator subscore from a computed indicator pane.

    `side` is "long" or "short". Returns the subscore plus each leg's
    verdict and the reasons behind it:

      2  all three legs agree
      1  at least one agrees and nothing opposes
      0  anything opposes, or nothing agrees (includes a pane too short to
         read — absence of evidence scores zero, it does not score 1)
    """
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")

    legs = {
        "macd": _macd_verdict(pane.get("macd"), side),
        "rsi": _rsi_verdict(pane.get("rsi"), side),
        "squeeze": _squeeze_verdict(pane.get("squeeze")),
    }
    verdicts = {k: v for k, (v, _) in legs.items()}
    reasons = [f"{k}: {why}" for k, (_, why) in legs.items()]

    if "against" in verdicts.values():
        score = 0
    elif all(v == "agree" for v in verdicts.values()):
        score = 2
    elif "agree" in verdicts.values():
        score = 1
    else:
        score = 0

    return {
        "indicator": score,
        "side": side,
        "timeframe": pane.get("timeframe"),
        "verdicts": verdicts,
        "reasons": reasons,
        "summary": pane.get("summary"),
    }


def mtf_indicator_subscore(panes: dict[str, dict], side: str) -> dict:
    """Score the pane on every timeframe and report the alignment.

    The headline `indicator` is the WEAKEST READABLE timeframe's score, not
    the best or the average: one timeframe agreeing while another opposes is
    not confluence, and taking the max would let a 4h read paper over a
    broken weekly.

    A timeframe with no readable leg at all (monthly MACD needs ~34 monthly
    bars — about three years of daily history) is `unreadable`: it is
    excluded from the headline and listed separately. Missing history is not
    opposition, and letting it score 0 would cap every young ticker at zero
    forever. `aligned` lists the timeframes that scored 2.
    """
    per_tf = {tf: indicator_subscore(pane, side) for tf, pane in panes.items()}
    if not per_tf:
        raise ValueError("no panes to score")
    unreadable = [
        tf for tf, r in per_tf.items()
        if all(v == "unavailable" for v in r["verdicts"].values())
    ]
    scores = {tf: r["indicator"] for tf, r in per_tf.items()}
    readable = {tf: s for tf, s in scores.items() if tf not in unreadable}
    return {
        "indicator": min(readable.values()) if readable else 0,
        "side": side,
        "per_timeframe": per_tf,
        "scores": scores,
        "aligned": [tf for tf, s in readable.items() if s == 2],
        "opposed": [
            tf for tf, r in per_tf.items()
            if "against" in r["verdicts"].values()
        ],
        "unreadable": unreadable,
    }


def from_legacy_score(legacy_1_to_5: int) -> ConfluenceBreakdown:
    """Map an existing 1..5 TradeRecord confluence score into the 0..8 space.

    Used for one-shot backfill of historical TradeRecord rows where only
    the legacy composite is known. The mapping is intentionally lossy:
    we assume a balanced split across the three components, so caller
    sees a plausible breakdown but should NOT treat the subscores as
    "what the vision actually saw" for new trades.

    Mapping:
      1 → (1,0,0) = 1, insufficient
      2 → (1,1,0) = 2, insufficient
      3 → (2,1,1) = 4, insufficient
      4 → (2,2,1) = 5, standard
      5 → (3,3,2) = 8, high_conviction
    """
    table = {
        1: (1, 0, 0),
        2: (1, 1, 0),
        3: (2, 1, 1),
        4: (2, 2, 1),
        5: (3, 3, 2),
    }
    if legacy_1_to_5 not in table:
        raise ValueError(f"legacy confluence must be 1..5, got {legacy_1_to_5!r}")
    p, f, i = table[legacy_1_to_5]
    return score_confluence(p, f, i)


def _clamp(v: int, lo: int, hi: int) -> int:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v
