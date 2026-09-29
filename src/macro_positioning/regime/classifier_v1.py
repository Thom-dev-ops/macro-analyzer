"""Regime classifier v1 — blended, data-driven, point-in-time.

Replaces the hand-hinted `classify_regime_stub` (which always returned
`commodity_led_inflation` because the caller passed a hardcoded string).

The core idea: a regime is not a label the market wears, it's a
*leadership pattern*. So we measure who is leading, cross-sectionally,
across four asset baskets and four lookback windows, then score each
framework regime by how well the observed leadership matches its
signature. The output is a blend (weights summing to 1), not a winner.

Two design choices worth knowing about:

1.  **Volatility normalisation.** Raw returns can't be compared across
    baskets — ETH moves 20% in a week where GLD moves 2%. Every member
    return is divided by that member's own trailing realised vol scaled
    to the window, giving a t-stat-like "how unusual is this move for
    this asset". Only then are baskets compared.

2.  **`transitional_chop` is not a fifth signature.** It doesn't compete
    on leadership — it's computed from *dispersion* (nobody is leading)
    and *disagreement* (fast-window leadership contradicts slow-window
    leadership). That's what a handoff between regimes actually looks
    like, and it's why a rotation in progress reads as genuinely
    transitional instead of being forced into whichever regime happens
    to be marginally ahead.

The commodity-vs-debasement discriminator lives in the signature matrix:
`commodity_led_inflation` rewards hard-money strength *mildly* (gold
rises alongside copper and energy in a real inflation), while
`monetary_debasement_hard_asset` actively *penalises* cyclical-commodity
strength. Gold with copper → commodity-led. Gold without copper → debasement.

Read-only against the DB. Every input degrades gracefully: a missing or
stale series is dropped and reflected in `coverage`, never raised.
"""
from __future__ import annotations

import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from macro_positioning.market.fred_history import value_at_or_before


CLASSIFIER_VERSION = "regime_classifier@blend-v1"

FRAMEWORK_REGIMES: tuple[str, ...] = (
    "risk_on_expansion",
    "risk_off_contraction",
    "commodity_led_inflation",
    "monetary_debasement_hard_asset",
    "transitional_chop",
)

# Basket members. Order matters only for tie-breaks; membership is
# median-aggregated so a single missing/stale ticker is harmless.
BASKETS: dict[str, tuple[str, ...]] = {
    "hard_money":         ("GLD", "GDX", "SI=F", "SLV", "BTC", "ETH"),
    "cyclical_commodity": ("XLE", "USO", "CL=F", "HG=F", "COPX", "URA", "XME"),
    "equity_beta":        ("SPX", "QQQ", "IWM", "XLK"),
    "defensive":          ("TLT", "XLU", "XLP", "UUP"),
}

WINDOWS: tuple[int, ...] = (5, 20, 60, 120)
FAST_WINDOWS: tuple[int, ...] = (5, 20)
SLOW_WINDOWS: tuple[int, ...] = (60, 120)

# Recency-weighted composite. Sums to 1.0 across WINDOWS. The 5d slot is
# deliberately light — a week of tape is a tell, not a regime. Fast
# reaction lives in `velocity`, which renormalises over FAST_WINDOWS.
_WINDOW_WEIGHTS: dict[int, float] = {5: 0.20, 20: 0.35, 60: 0.28, 120: 0.17}

# A ticker whose most recent bar is older than this (calendar days before
# as_of) is dropped rather than silently contributing a stale return.
_MAX_STALE_DAYS = 12

# Trailing bars used for realised-vol normalisation.
_VOL_LOOKBACK = 60
_MIN_VOL = 0.002          # floor on daily vol so a flatlined series can't explode
_Z_CLAMP = 4.0            # cap per-member z so one blow-up can't own a basket

# How much cross-sectional leadership counts as "decisive". Used to turn
# dispersion into a 0..1 chop term.
_CHOP_REF = 1.10

# Softmax temperature. Lower = more decisive blends.
_TEMPERATURE = 0.55

# How hard a fast-vs-slow disagreement damps directional conviction, and
# how the chop term is built from dispersion (absolute) and disagreement
# (relative to the strongest directional score).
_DISAGREEMENT_DAMP = 0.35
_CHOP_DISPERSION_COEF = 0.55
_CHOP_HANDOFF_COEF = 0.75

# regime → {basket: coefficient}. Applied to composite leadership.
_SIGNATURES: dict[str, dict[str, float]] = {
    "risk_on_expansion": {
        "equity_beta": 1.00, "defensive": -0.50,
        "hard_money": -0.20, "cyclical_commodity": 0.20,
    },
    "risk_off_contraction": {
        "defensive": 1.00, "equity_beta": -1.00,
        "hard_money": 0.10, "cyclical_commodity": -0.30,
    },
    "commodity_led_inflation": {
        "cyclical_commodity": 1.00, "hard_money": 0.35,
        "equity_beta": -0.10, "defensive": -0.30,
    },
    "monetary_debasement_hard_asset": {
        "hard_money": 1.00, "cyclical_commodity": -0.45,
        "defensive": -0.20, "equity_beta": -0.10,
    },
}


@dataclass
class RegimeBlend:
    """Point-in-time regime read.

    `blend` weights sum to 1.0 across FRAMEWORK_REGIMES. `dominant` is
    argmax(blend) — provided only for backward compatibility with the
    consumers that still want a single slug (scoring runner, framework
    bias table). Prefer the blend.

    `velocity` is fast-window blend minus slow-window blend, per regime.
    Macro modifiers are applied identically to both, so velocity isolates
    the price-leadership handoff: positive = money rotating toward this
    regime, negative = rotating out of it.
    """
    as_of: date
    blend: dict[str, float]
    velocity: dict[str, float]
    dominant: str
    confidence: float
    leadership: dict[str, float]
    fast_leadership: dict[str, float]
    slow_leadership: dict[str, float]
    states: dict[str, str]
    evidence: list[str] = field(default_factory=list)
    coverage: float = 0.0
    classifier_version: str = CLASSIFIER_VERSION

    @property
    def thesis_regime(self) -> str:
        return _thesis_for(self.dominant, self.states)

    def top_movers(self, n: int = 2) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
        """(rising, falling) — regimes sorted by velocity, most extreme first."""
        ordered = sorted(self.velocity.items(), key=lambda kv: kv[1], reverse=True)
        rising = [kv for kv in ordered if kv[1] > 0.01][:n]
        falling = [kv for kv in reversed(ordered) if kv[1] < -0.01][:n]
        return rising, falling


# ---------------------------------------------------------------------------
# Price plumbing (point-in-time)
# ---------------------------------------------------------------------------

def _load_closes(
    conn: sqlite3.Connection, ticker: str, as_of: date, bars: int
) -> list[tuple[str, float]]:
    """Most recent `bars` daily closes at or before `as_of`, chronological.

    Deduplicated by date (the prices table can hold multiple rows per
    ticker/day from different providers) — latest fetch wins.
    """
    rows = conn.execute(
        """
        SELECT substr(observed_at, 1, 10) AS d, close, MAX(fetched_at)
        FROM prices
        WHERE ticker = ? AND timeframe = '1D' AND close IS NOT NULL
          AND substr(observed_at, 1, 10) <= ?
        GROUP BY d
        ORDER BY d DESC
        LIMIT ?
        """,
        (ticker.upper(), as_of.isoformat(), bars),
    ).fetchall()
    rows.reverse()
    return [(r[0], float(r[1])) for r in rows]


def _realised_vol(closes: list[float]) -> float | None:
    """Std-dev of daily log returns. None if too few points."""
    if len(closes) < 20:
        return None
    rets = []
    for prev, cur in zip(closes, closes[1:]):
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    if len(rets) < 15:
        return None
    try:
        return max(_MIN_VOL, statistics.stdev(rets))
    except statistics.StatisticsError:
        return None


def _member_z(
    conn: sqlite3.Connection, ticker: str, as_of: date
) -> dict[int, float] | None:
    """Vol-normalised return per window for one ticker.

    z[w] = return_over_w / (daily_vol * sqrt(w)) — a t-stat-like measure
    of how unusual the move is for that specific asset. Returns None if
    the series is missing, too short, or stale.
    """
    need = max(WINDOWS) + _VOL_LOOKBACK + 5
    series = _load_closes(conn, ticker, as_of, need)
    if len(series) < 25:
        return None

    last_date = date.fromisoformat(series[-1][0])
    if (as_of - last_date).days > _MAX_STALE_DAYS:
        return None  # stale feed (e.g. a delisted or broken symbol)

    closes = [c for _, c in series]
    vol = _realised_vol(closes[-(_VOL_LOOKBACK + 1):])
    if vol is None:
        return None

    out: dict[int, float] = {}
    for w in WINDOWS:
        if len(closes) < w + 1:
            continue
        a, b = closes[-1 - w], closes[-1]
        if a <= 0 or b <= 0:
            continue
        z = math.log(b / a) / (vol * math.sqrt(w))
        out[w] = max(-_Z_CLAMP, min(_Z_CLAMP, z))
    return out or None


def _basket_z(
    conn: sqlite3.Connection, as_of: date
) -> tuple[dict[str, dict[int, float]], dict[str, int]]:
    """Median member z per basket per window, plus member counts."""
    scores: dict[str, dict[int, float]] = {}
    counts: dict[str, int] = {}
    for basket, tickers in BASKETS.items():
        members = [z for t in tickers if (z := _member_z(conn, t, as_of))]
        counts[basket] = len(members)
        if not members:
            continue
        per_window: dict[int, float] = {}
        for w in WINDOWS:
            vals = [m[w] for m in members if w in m]
            if vals:
                per_window[w] = statistics.median(vals)
        if per_window:
            scores[basket] = per_window
    return scores, counts


def _leadership(
    basket_z: dict[str, dict[int, float]], windows: tuple[int, ...]
) -> dict[str, float]:
    """Cross-sectional leadership: each basket's z minus the mean z across
    baskets, weighted over `windows`. Positive = leading the field.

    Subtracting the cross-sectional mean is what makes this a rotation
    detector rather than a direction detector — an everything-up tape
    produces near-zero leadership everywhere, which is correct.
    """
    if not basket_z:
        return {}
    weight_sum = sum(_WINDOW_WEIGHTS[w] for w in windows)
    out: dict[str, float] = {b: 0.0 for b in basket_z}
    for w in windows:
        present = {b: z[w] for b, z in basket_z.items() if w in z}
        if len(present) < 2:
            continue
        mean_z = sum(present.values()) / len(present)
        wt = _WINDOW_WEIGHTS[w] / weight_sum
        for b, z in present.items():
            out[b] += (z - mean_z) * wt
    return {b: round(v, 4) for b, v in out.items()}


# ---------------------------------------------------------------------------
# Macro confirmation
# ---------------------------------------------------------------------------

def _fred_delta(
    conn: sqlite3.Connection, series_id: str, days: int, as_of: date
) -> tuple[float | None, float | None]:
    """(latest_value, change_over_`days`) at or before as_of."""
    cur = value_at_or_before(conn, series_id, as_of)
    if cur is None:
        return None, None
    cur_date, cur_val = cur
    prior = value_at_or_before(conn, series_id, cur_date - timedelta(days=days))
    return cur_val, (None if prior is None else cur_val - prior[1])


def _macro_modifiers(
    conn: sqlite3.Connection, as_of: date, leadership: dict[str, float]
) -> tuple[dict[str, float], dict[str, str], list[str], int]:
    """Bounded per-regime adjustments from rates / dollar / conditions.

    Price leadership sets the shape; these gate the *flavour* — most
    importantly whether a hard-money bid is an inflation hedge (breakevens
    rising with it) or a debasement bid (breakevens flat while gold runs).
    """
    mods: dict[str, float] = {r: 0.0 for r in FRAMEWORK_REGIMES}
    states: dict[str, str] = {}
    notes: list[str] = []
    available = 0

    hard_leads = leadership.get("hard_money", 0.0) > 0.15
    cyc_leads = leadership.get("cyclical_commodity", 0.0) > 0.15

    # --- Real yields (DFII10) ---
    real_y, d_real = _fred_delta(conn, "DFII10", 60, as_of)
    if real_y is not None:
        available += 1
        if d_real is not None:
            if d_real < -0.10:
                mods["monetary_debasement_hard_asset"] += 0.25
                mods["risk_on_expansion"] += 0.15
                notes.append(f"10y real yield falling ({d_real:+.2f} over 60d) — supports hard assets.")
            elif d_real > 0.10:
                mods["monetary_debasement_hard_asset"] -= 0.20
                notes.append(f"10y real yield rising ({d_real:+.2f} over 60d) — headwind for hard assets.")

    # --- Breakevens (T10YIE): the commodity-vs-debasement tell ---
    be, d_be = _fred_delta(conn, "T10YIE", 60, as_of)
    if be is not None:
        available += 1
        if d_be is not None:
            if d_be > 0.05:
                mods["commodity_led_inflation"] += 0.30
                notes.append(f"10y breakeven rising ({d_be:+.2f} over 60d) — inflation expectations confirming.")
            elif hard_leads:
                mods["monetary_debasement_hard_asset"] += 0.30
                notes.append(
                    f"Hard assets leading while 10y breakeven is flat ({be:.2f}%, {d_be:+.2f} over 60d) — "
                    "a debasement bid, not an inflation hedge."
                )

    # --- Dollar (DTWEXBGS) ---
    usd, d_usd = _fred_delta(conn, "DTWEXBGS", 60, as_of)
    if usd is not None:
        available += 1
        if d_usd is None:
            states["dollar_trend"] = "unknown"
        elif d_usd < -0.5:
            states["dollar_trend"] = "weakening"
            mods["monetary_debasement_hard_asset"] += 0.20
            mods["commodity_led_inflation"] += 0.10
            notes.append(f"Trade-weighted USD weakening ({d_usd:+.2f} over 60d).")
        elif d_usd > 0.5:
            states["dollar_trend"] = "strengthening"
            mods["risk_off_contraction"] += 0.10
            mods["commodity_led_inflation"] -= 0.10
            notes.append(f"Trade-weighted USD strengthening ({d_usd:+.2f} over 60d).")
        else:
            states["dollar_trend"] = "flat"

    # --- Financial conditions (NFCI) ---
    nfci, _ = _fred_delta(conn, "NFCI", 60, as_of)
    if nfci is not None:
        available += 1
        if nfci < -0.3:
            states["liquidity_state"] = "easing"
            mods["risk_on_expansion"] += 0.20
            mods["monetary_debasement_hard_asset"] += 0.10
            mods["risk_off_contraction"] -= 0.20
            notes.append(f"Financial conditions accommodative (NFCI {nfci:+.2f}).")
        elif nfci > 0.3:
            states["liquidity_state"] = "tightening"
            mods["risk_off_contraction"] += 0.30
            mods["risk_on_expansion"] -= 0.25
            notes.append(f"Financial conditions restrictive (NFCI {nfci:+.2f}).")
        else:
            states["liquidity_state"] = "neutral"

    # --- Credit (HY OAS) ---
    oas, d_oas = _fred_delta(conn, "BAMLH0A0HYM2", 20, as_of)
    if oas is not None:
        available += 1
        if d_oas is not None and d_oas > 0.25:
            mods["risk_off_contraction"] += 0.35
            mods["risk_on_expansion"] -= 0.20
            notes.append(f"HY credit spreads widening ({d_oas:+.2f} over 20d) — risk-off confirmation.")
        elif oas < 3.2:
            mods["risk_off_contraction"] -= 0.15
            notes.append(f"HY OAS tight at {oas:.2f}% — no credit stress.")

    # --- Volatility (VIX) ---
    vix, _ = _fred_delta(conn, "VIXCLS", 20, as_of)
    if vix is not None:
        available += 1
        if vix > 25:
            states["volatility_state"] = "elevated"
            mods["risk_off_contraction"] += 0.20
            notes.append(f"VIX elevated at {vix:.1f}.")
        elif vix < 16:
            states["volatility_state"] = "low"
            mods["risk_on_expansion"] += 0.10
            notes.append(f"VIX subdued at {vix:.1f} — no fear bid.")
        else:
            states["volatility_state"] = "normal"

    # --- Rate trend (nominal 10y), state only ---
    _, d_10y = _fred_delta(conn, "DGS10", 60, as_of)
    if d_10y is not None:
        states["rate_trend"] = (
            "rising" if d_10y > 0.15 else "falling" if d_10y < -0.15 else "flat"
        )

    # Cyclical strength with no breakeven confirmation is a supply story,
    # not a regime — trim it so energy spikes don't fake an inflation regime.
    if cyc_leads and d_be is not None and d_be <= 0.0:
        mods["commodity_led_inflation"] -= 0.15
        notes.append("Cyclical commodities leading without breakeven confirmation — discounted.")

    return mods, states, notes, available


def _breadth_state(conn: sqlite3.Connection, as_of: date) -> str:
    """Small-cap vs large-cap participation over 60d."""
    iwm = _member_z(conn, "IWM", as_of)
    spx = _member_z(conn, "SPX", as_of)
    if not iwm or not spx or 60 not in iwm or 60 not in spx:
        return "unknown"
    diff = iwm[60] - spx[60]
    return "broadening" if diff > 0.25 else "narrowing" if diff < -0.25 else "neutral"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _signature_scores(leadership: dict[str, float]) -> dict[str, float]:
    """Raw (pre-softmax) score for the four directional regimes."""
    scores: dict[str, float] = {}
    for regime, sig in _SIGNATURES.items():
        scores[regime] = sum(coef * leadership.get(b, 0.0) for b, coef in sig.items())
    return scores


def _chop_score(
    leadership: dict[str, float],
    fast: dict[str, float],
    slow: dict[str, float],
) -> tuple[float, float]:
    """(dispersion, disagreement).

    dispersion  — nobody is decisively leading.
    disagreement — fast-window leadership contradicts slow-window
                   leadership, i.e. a handoff is underway.

    Disagreement is cosine-based on purpose. A Euclidean distance between
    the two leadership vectors would flag a regime that merely
    *accelerated* (same leaders, bigger gaps) as a handoff, which is the
    opposite of the truth. What matters is whether the ordering flipped,
    and that is direction, not magnitude.
    """
    if not leadership:
        return 1.0, 0.0
    peak = max(abs(v) for v in leadership.values())
    dispersion = max(0.0, 1.0 - min(1.0, peak / _CHOP_REF))

    shared = sorted(set(fast) & set(slow))
    fmag = math.sqrt(sum(fast[b] ** 2 for b in shared)) if shared else 0.0
    smag = math.sqrt(sum(slow[b] ** 2 for b in shared)) if shared else 0.0
    if shared and fmag > 1e-9 and smag > 1e-9:
        cos = sum(fast[b] * slow[b] for b in shared) / (fmag * smag)
        cos = max(-1.0, min(1.0, cos))
        disagreement = (1.0 - cos) / 2.0   # aligned → 0, reversed → 1
    else:
        disagreement = 0.0

    return dispersion, disagreement


def _compose_raw(
    leadership: dict[str, float],
    fast: dict[str, float],
    slow: dict[str, float],
    mods: dict[str, float],
) -> dict[str, float]:
    """Directional signature scores + the chop term, on one scale.

    Two things happen here that a flat weighted sum would get wrong:

    - When fast and slow leadership disagree, *neither* story is reliable,
      so every directional score is damped toward zero (i.e. toward a
      uniform blend). Conviction should fall during a handoff, not stay
      pinned to whichever side is currently louder.
    - The chop term carries an absolute floor from dispersion (a flat tape
      is chop even though no score is large) *and* a relative term scaled
      to the strongest directional score (so a handoff between two strong
      regimes still reads as a handoff, instead of being drowned out by
      the leader it is handing off from).
    """
    directional = _signature_scores(leadership)
    dispersion, disagreement = _chop_score(leadership, fast, slow)

    peak = max((abs(v) for v in directional.values()), default=0.0)
    damp = 1.0 - _DISAGREEMENT_DAMP * disagreement

    raw = {r: v * damp for r, v in directional.items()}
    raw["transitional_chop"] = (
        _CHOP_REF * _CHOP_DISPERSION_COEF * dispersion
        + peak * _CHOP_HANDOFF_COEF * disagreement
    )
    for regime, delta in mods.items():
        raw[regime] = raw.get(regime, 0.0) + delta
    return raw


def _softmax(scores: dict[str, float], temperature: float = _TEMPERATURE) -> dict[str, float]:
    if not scores:
        return {}
    peak = max(scores.values())
    exps = {k: math.exp((v - peak) / temperature) for k, v in scores.items()}
    total = sum(exps.values()) or 1.0
    return {k: round(v / total, 4) for k, v in exps.items()}


def _thesis_for(framework_regime: str, states: dict[str, str]) -> str:
    """Map framework regime → thesis taxonomy (macro_brain.types.ThesisRegime).

    The thesis taxonomy predates the framework one and has no debasement
    entry; a debasement bid inside easy conditions is a liquidity wave,
    inside tight conditions it is an inflation shock.
    """
    liquidity = states.get("liquidity_state", "neutral")
    vol = states.get("volatility_state", "normal")
    if framework_regime == "risk_on_expansion":
        return "dovish_liquidity_wave"
    if framework_regime == "risk_off_contraction":
        return "broad_panic" if vol == "elevated" else "growth_scare"
    if framework_regime == "commodity_led_inflation":
        return "inflation_shock" if liquidity == "tightening" else "commodity_expansion"
    if framework_regime == "monetary_debasement_hard_asset":
        return "inflation_shock" if liquidity == "tightening" else "dovish_liquidity_wave"
    return "sideways_churn"


def classify_regime_v1(
    conn: sqlite3.Connection, *, as_of: date | None = None
) -> RegimeBlend | None:
    """Classify the regime as a blend at `as_of` (default: today).

    Returns None when there is not enough price history to say anything —
    callers should fall back rather than publish a fabricated read.
    """
    as_of = as_of or date.today()

    basket_z, counts = _basket_z(conn, as_of)
    if len(basket_z) < 3:
        return None  # can't measure rotation across fewer than 3 baskets

    lead_all = _leadership(basket_z, WINDOWS)
    lead_fast = _leadership(basket_z, FAST_WINDOWS)
    lead_slow = _leadership(basket_z, SLOW_WINDOWS)

    mods, states, notes, macro_available = _macro_modifiers(conn, as_of, lead_all)
    states.setdefault("dollar_trend", "unknown")
    states.setdefault("liquidity_state", "unknown")
    states.setdefault("volatility_state", "unknown")
    states.setdefault("rate_trend", "unknown")
    states["breadth_state"] = _breadth_state(conn, as_of)

    def _blend_for(lead: dict[str, float], f: dict[str, float], s: dict[str, float]) -> dict[str, float]:
        return _softmax(_compose_raw(lead, f, s, mods))

    blend = _blend_for(lead_all, lead_fast, lead_slow)
    # Velocity: same modifiers on both sides, so they cancel and what
    # remains is the pure leadership handoff.
    blend_fast = _blend_for(lead_fast, lead_fast, lead_slow)
    blend_slow = _blend_for(lead_slow, lead_fast, lead_slow)
    velocity = {r: round(blend_fast.get(r, 0.0) - blend_slow.get(r, 0.0), 4) for r in blend}

    ordered = sorted(blend.items(), key=lambda kv: kv[1], reverse=True)
    dominant = ordered[0][0]
    separation = ordered[0][1] - (ordered[1][1] if len(ordered) > 1 else 0.0)

    total_members = sum(len(v) for v in BASKETS.values())
    price_coverage = sum(counts.values()) / total_members
    macro_coverage = macro_available / 6.0
    coverage = round(0.65 * price_coverage + 0.35 * macro_coverage, 3)

    confidence = (0.35 + min(0.50, separation * 1.5)) * (0.6 + 0.4 * coverage)
    confidence = round(max(0.20, min(0.92, confidence)), 3)

    chop_dispersion, chop_disagreement = _chop_score(lead_all, lead_fast, lead_slow)
    evidence = _build_evidence(
        lead_all, velocity, blend, notes, chop_dispersion, chop_disagreement, counts
    )

    return RegimeBlend(
        as_of=as_of,
        blend=blend,
        velocity=velocity,
        dominant=dominant,
        confidence=confidence,
        leadership=lead_all,
        fast_leadership=lead_fast,
        slow_leadership=lead_slow,
        states=states,
        evidence=evidence,
        coverage=coverage,
    )


_BASKET_LABELS = {
    "hard_money": "hard money (gold/silver/miners/crypto)",
    "cyclical_commodity": "cyclical commodities (energy/copper/uranium)",
    "equity_beta": "equity beta",
    "defensive": "defensives (duration/staples/utilities/USD)",
}

_REGIME_LABELS = {
    "risk_on_expansion": "Risk-On Expansion",
    "risk_off_contraction": "Risk-Off Contraction",
    "commodity_led_inflation": "Commodity-Led Inflation",
    "monetary_debasement_hard_asset": "Monetary Debasement / Hard Asset",
    "transitional_chop": "Transitional Chop",
}


def _build_evidence(
    leadership: dict[str, float],
    velocity: dict[str, float],
    blend: dict[str, float],
    macro_notes: list[str],
    dispersion: float,
    disagreement: float,
    counts: dict[str, int],
) -> list[str]:
    ev: list[str] = []

    ranked = sorted(leadership.items(), key=lambda kv: kv[1], reverse=True)
    if ranked:
        top, top_v = ranked[0]
        bot, bot_v = ranked[-1]
        ev.append(
            f"Leadership: {_BASKET_LABELS.get(top, top)} {top_v:+.2f}, "
            f"lagging {_BASKET_LABELS.get(bot, bot)} {bot_v:+.2f} (vol-adjusted, cross-sectional)."
        )

    rising = sorted(velocity.items(), key=lambda kv: kv[1], reverse=True)
    if rising and rising[0][1] > 0.01:
        r, rv = rising[0]
        ev.append(f"Rotating toward {_REGIME_LABELS.get(r, r)} ({rv:+.0%} fast-vs-slow).")
    if rising and rising[-1][1] < -0.01:
        f, fv = rising[-1]
        ev.append(f"Rotating out of {_REGIME_LABELS.get(f, f)} ({fv:+.0%} fast-vs-slow).")

    if disagreement > 0.45:
        ev.append(
            f"Short-window leadership disagrees with long-window ({disagreement:.0%} of a full handoff) — "
            "regime change in progress."
        )
    if dispersion > 0.55:
        ev.append(f"No basket leading decisively ({dispersion:.0%} dispersion) — chop.")

    ev.extend(macro_notes)

    thin = [b for b, n in counts.items() if n < 2]
    if thin:
        ev.append(f"Thin price coverage for: {', '.join(thin)} — read with reduced confidence.")
    return ev
