"""What is this trade actually worth, on everything the desk knows?

The book used to judge a setup on four numbers — entry, stop, target, R:R
— handed over by the technical agent, and threw away everything behind
them. That is a thin basis for a decision the platform has four
independent views on:

    structure       the chart itself: is the target a level that has been
                    tested and held, or a projection into open field?
    trusted_voices  the humans: do the KOL targets cluster near the
                    agent's, and are the people drawing them any good?
    regime          the macro read: does this asset belong in the regime
                    the desk currently believes it is in?
    price_action    the tape: is there room to the target, is price
                    trending with the trade, is the target reachable in
                    the horizon the book actually holds for?

`valuate()` composes those into a `TradeValuation`: a 0–100 `support`
score, a possibly-revised target and stop, and an itemised list of what
each view said. The engine uses it four ways — sizing (support feeds
rank), admission (an unsupported target is a `Blocker`), the levels
themselves (a target agreed by voices and structure beats a projection),
and exit timing (a thesis whose supports have gone is a thesis to close).

Deliberately paper-side. `scoring/levels.py` is shared with the
hand-traded desk, the alerts and the SPA cards; this layer judges its
output without moving it, so the composition can be proven on the paper
book's own record before anything the desk reads changes.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from macro_positioning.prices.structure import (
    Level,
    StructureMap,
    build_structure,
    next_resistance,
    next_support_below,
)


logger = logging.getLogger(__name__)


# How far a KOL target may sit from the agent's before the two are
# describing different trades rather than agreeing on one. Expressed in
# ATR so it scales with the instrument.
_AGREEMENT_ATR = 1.5

# A target closer than this (in ATR) is not worth the risk of the trade.
_MIN_TARGET_ATR = 1.0


@dataclass
class Evidence:
    """One view's verdict on the trade, with the numbers behind it."""

    view: str        # structure | trusted_voices | regime | price_action
    stance: str      # supports | neutral | opposes | absent
    delta: float     # rank points this view contributes
    detail: str      # the sentence the decision log renders
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "view": self.view,
            "stance": self.stance,
            "delta": round(self.delta, 2),
            "detail": self.detail,
            "data": self.data,
        }


@dataclass
class TradeValuation:
    ticker: str
    side: str
    support: float                      # 0..100
    evidence: list[Evidence] = field(default_factory=list)
    target: Optional[float] = None      # composed; may differ from the agent's
    stop: Optional[float] = None
    agent_target: Optional[float] = None
    target_source: str = "agent"
    rr: Optional[float] = None
    blockers: list[str] = field(default_factory=list)

    @property
    def is_long(self) -> bool:
        return self.side.upper() == "LONG"

    @property
    def target_moved(self) -> bool:
        if self.target is None or self.agent_target is None:
            return False
        return abs(self.target - self.agent_target) > 1e-9

    @property
    def headline(self) -> str:
        movers = sorted(
            [e for e in self.evidence if abs(e.delta) >= 1.0],
            key=lambda e: abs(e.delta), reverse=True,
        )[:2]
        tail = "; ".join(e.detail for e in movers)
        head = f"{self.ticker} target support {self.support:.0f}/100"
        if self.target_moved:
            head += f" (target {self.agent_target:g} → {self.target:g}, {self.target_source})"
        return f"{head} — {tail}" if tail else head

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "side": self.side,
            "support": round(self.support, 1),
            "target": self.target,
            "agentTarget": self.agent_target,
            "targetSource": self.target_source,
            "targetMoved": self.target_moved,
            "stop": self.stop,
            "rr": round(self.rr, 2) if self.rr is not None else None,
            "blockers": list(self.blockers),
            "evidence": [e.as_dict() for e in self.evidence],
            "headline": self.headline,
        }


# ── The four views ────────────────────────────────────────────────────


def _structure_view(
    *, side: str, entry: float, target: Optional[float], atr: float,
    structure: Optional[StructureMap], provenance: list[dict],
) -> tuple[Evidence, Optional[Level]]:
    """Is the target a level the chart has actually respected?

    An open-field projection is not wrong, but it is an assumption where a
    tested level is an observation, and the book should not pay the same
    size for both.
    """
    src = next(
        (p.get("source") for p in provenance if p.get("role") == "target"), None
    )
    lv: Optional[Level] = None
    if structure is not None and entry:
        lv = (
            next_resistance(structure, entry, min_distance=atr * _MIN_TARGET_ATR)
            if side == "LONG"
            else next_support_below(structure, entry, min_distance=atr * _MIN_TARGET_ATR)
        )

    if lv is not None:
        # A level that has been tested repeatedly and recently is worth
        # more than one touched once, a year ago.
        strong = lv.touches >= 3 and lv.last_touch_bars <= 60
        delta = 10.0 if strong else 5.0
        return (
            Evidence(
                "structure", "supports", delta,
                f"target sits on {lv.kind} {lv.price:g}, held {lv.touches}× "
                f"(last tested {lv.last_touch_bars} bars ago)"
                + (" — flipped level" if lv.flipped else ""),
                {"price": lv.price, "touches": lv.touches,
                 "lastTouchBars": lv.last_touch_bars, "strength": round(lv.strength, 3),
                 "flipped": lv.flipped},
            ),
            lv,
        )

    if src == "open_field":
        return (
            Evidence(
                "structure", "neutral", -8.0,
                "no tested level between price and the target — the target is a "
                "projection into open field, not something the chart has respected",
                {"agentSource": src},
            ),
            None,
        )
    return (
        Evidence("structure", "absent", 0.0,
                 "no structure map available for this name", {"agentSource": src}),
        None,
    )


def _voices_view(
    *, side: str, target: Optional[float], atr: float, kol,
) -> tuple[Evidence, Optional[float]]:
    """Do the trusted voices agree on where this goes?

    Returns the consensus target when it is worth adopting — a fresh,
    trusted cluster that the agent's own target is within reach of.
    """
    if kol is None or getattr(kol, "target", None) is None:
        return (
            Evidence("trusted_voices", "absent", 0.0,
                     "no trusted voice has drawn a target on this name", {}),
            None,
        )

    cons = kol.target
    n = len(cons.contributors)
    if target and atr > 0:
        gap_atr = abs(cons.price - target) / atr
    else:
        gap_atr = None

    data = {"consensus": cons.price, "contributors": n, "trusted": cons.trusted,
            "weight": round(cons.weight, 3), "gapAtr": round(gap_atr, 2) if gap_atr else None,
            "who": cons.basis}

    if not cons.trusted:
        return (
            Evidence("trusted_voices", "neutral", 0.0,
                     f"{n} voice(s) target {cons.price:g}, but none has a rated record yet",
                     data),
            None,
        )

    if gap_atr is not None and gap_atr <= _AGREEMENT_ATR:
        return (
            Evidence("trusted_voices", "supports", 12.0,
                     f"{n} trusted voice(s) target {cons.price:g}, within "
                     f"{gap_atr:.1f} ATR of the agent's — {cons.basis}", data),
            cons.price,
        )

    # They are looking at the same name and seeing a different trade.
    beyond = (cons.price > target) if side == "LONG" else (cons.price < target)
    if beyond:
        return (
            Evidence("trusted_voices", "supports", 6.0,
                     f"{n} trusted voice(s) target {cons.price:g}, further out than the "
                     f"agent's {target:g} — the agent's target is the conservative read",
                     data),
            None,
        )
    return (
        Evidence("trusted_voices", "opposes", -10.0,
                 f"{n} trusted voice(s) target {cons.price:g}, short of the agent's "
                 f"{target:g} — the people watching this expect less than the chart does",
                 data),
        cons.price,
    )


def _regime_view(*, macro_score: Optional[int], regime_label: Optional[str]) -> Evidence:
    """Does this name belong in the regime the desk believes it is in?

    Reuses the brain's own `macro_alignment_score` (0–20) rather than
    re-deriving the mapping — desk_data already reads >=12 as "fit" and
    <6 as "off", and two definitions of regime fit would drift apart.
    """
    if macro_score is None:
        return Evidence("regime", "absent", 0.0, "no regime alignment on this row", {})
    data = {"macroScore": macro_score, "regime": regime_label}
    if macro_score >= 12:
        return Evidence("regime", "supports", 8.0,
                        f"fits the {regime_label or 'current'} regime "
                        f"(alignment {macro_score}/20)", data)
    if macro_score < 6:
        return Evidence("regime", "opposes", -12.0,
                        f"fights the {regime_label or 'current'} regime "
                        f"(alignment {macro_score}/20) — the target assumes a tape "
                        "the desk does not think it is in", data)
    return Evidence("regime", "neutral", 0.0,
                    f"regime-neutral (alignment {macro_score}/20)", data)


def _price_action_view(
    *, side: str, entry: float, target: Optional[float], stop: Optional[float],
    atr: float, ret_20d: Optional[float],
) -> Evidence:
    """Is the target reachable, and is the tape going that way?

    Distance is measured in ATR because "how far" only means something
    against how far this instrument moves in a day. A target eight ATR
    away is not a swing target, it is a hope.
    """
    if not target or not entry or atr <= 0:
        return Evidence("price_action", "absent", 0.0, "no usable price context", {})

    dist_atr = abs(target - entry) / atr
    with_trend = None
    if ret_20d is not None:
        with_trend = (ret_20d > 0) if side == "LONG" else (ret_20d < 0)
    data = {"targetDistanceAtr": round(dist_atr, 2),
            "ret20d": round(ret_20d, 4) if ret_20d is not None else None,
            "withTrend": with_trend}

    if dist_atr > 8:
        return Evidence("price_action", "opposes", -10.0,
                        f"target is {dist_atr:.1f} ATR away — too far to reach on a "
                        "swing horizon", data)
    if with_trend is False:
        return Evidence("price_action", "opposes", -6.0,
                        f"20-day return is {ret_20d * 100:+.1f}%, against the "
                        f"{side.lower()} — buying into a tape going the other way", data)
    if with_trend is True and dist_atr <= 5:
        return Evidence("price_action", "supports", 6.0,
                        f"target {dist_atr:.1f} ATR away with a {ret_20d * 100:+.1f}% "
                        "20-day tailwind", data)
    return Evidence("price_action", "neutral", 0.0,
                    f"target {dist_atr:.1f} ATR away, tape flat", data)


# ── Composition ───────────────────────────────────────────────────────

# Support starts at the midpoint: the agent's levels are a real opinion,
# not a coin flip. The views move it from there.
_BASE_SUPPORT = 50.0


def valuate(
    *,
    ticker: str,
    side: str,
    entry: float,
    stop: Optional[float],
    target: Optional[float],
    atr: float,
    provenance: Optional[list[dict]] = None,
    structure: Optional[StructureMap] = None,
    kol=None,
    macro_score: Optional[int] = None,
    regime_label: Optional[str] = None,
    ret_20d: Optional[float] = None,
    min_support: float = 35.0,
) -> TradeValuation:
    """Compose every view the desk holds on one trade."""
    provenance = provenance or []
    ev: list[Evidence] = []

    struct_ev, struct_level = _structure_view(
        side=side, entry=entry, target=target, atr=atr,
        structure=structure, provenance=provenance,
    )
    ev.append(struct_ev)

    voices_ev, voice_target = _voices_view(
        side=side, target=target, atr=atr, kol=kol
    )
    ev.append(voices_ev)
    ev.append(_regime_view(macro_score=macro_score, regime_label=regime_label))
    ev.append(_price_action_view(
        side=side, entry=entry, target=target, stop=stop, atr=atr, ret_20d=ret_20d,
    ))

    support = _BASE_SUPPORT + sum(e.delta for e in ev)
    support = max(0.0, min(100.0, support))

    # ── Compose the target ────────────────────────────────────────────
    # Preference order, most-observed to most-assumed:
    #   1. a tested structural level the agent projected past
    #   2. a trusted-voice consensus that is SHORTER than the agent's
    #      (taking less off the table than the chart hopes for is the
    #      conservative error, and these are people with records)
    #   3. the agent's own target
    composed = target
    source = "agent"
    if struct_level is not None and target is not None:
        overshoots = (
            target > struct_level.price if side == "LONG" else target < struct_level.price
        )
        if overshoots:
            composed = struct_level.price
            source = "structure"
    if voice_target is not None and composed is not None:
        shorter = (
            voice_target < composed if side == "LONG" else voice_target > composed
        )
        if shorter and voices_ev.stance == "opposes":
            composed = voice_target
            source = "trusted_voices"

    rr = None
    if composed is not None and stop is not None and entry:
        risk = abs(entry - stop)
        if risk > 0:
            rr = abs(composed - entry) / risk

    blockers: list[str] = []
    if support < min_support:
        blockers.append(
            f"target support {support:.0f}/100 is under the {min_support:.0f} bar"
        )
    if composed is not None and atr > 0 and abs(composed - entry) / atr < _MIN_TARGET_ATR:
        blockers.append(
            f"composed target is {abs(composed - entry) / atr:.1f} ATR away — "
            "too close to pay for the risk"
        )

    return TradeValuation(
        ticker=ticker.upper(), side=side, support=support, evidence=ev,
        target=composed, stop=stop, agent_target=target, target_source=source,
        rr=rr, blockers=blockers,
    )


__all__ = ["Evidence", "TradeValuation", "valuate"]


# ── Gathering the inputs ──────────────────────────────────────────────


def valuate_reads(
    reads,
    *,
    db_path: Optional[Path] = None,
    min_support: float = 35.0,
    limit: int = 40,
    held_tickers=None,
) -> dict[str, TradeValuation]:
    """Valuate the candidates worth valuating, keyed by ticker.

    Only the reads that could actually be traded are priced: building a
    structure map means loading 200 bars per name, and doing that for the
    ~90 WATCH/AVOID rows in a pass would be most of the tick's runtime
    spent on trades the book will never take.

    Every input is best-effort. A missing structure map or a KOL table
    that will not load degrades that view to `absent` — it does not take
    the tick down.
    """
    from macro_positioning.db.connect import read_connection
    from macro_positioning.prices.fetcher import load_recent_prices
    from macro_positioning.prices.technicals import compute_technical_features
    from macro_positioning.scoring.kol_levels import author_weights, kol_levels_for_ticker

    tradeable = [r for r in reads if getattr(r, "tradeable", False)]
    # Held names first — the exit pass and the snapshots need them priced
    # whatever their rank — then the strongest candidates up to the limit.
    held = set(t.upper() for t in (held_tickers or ()))
    tradeable.sort(key=lambda r: (r.ticker.upper() not in held, -getattr(r, "raw", 0.0)))
    tradeable = tradeable[:limit]
    if not tradeable:
        return {}

    out: dict[str, TradeValuation] = {}
    with read_connection(db_path) as conn:
        try:
            weights = author_weights()
        except Exception as exc:
            logger.debug("author weights unavailable: %s", exc)
            weights = {}

        for read in tradeable:
            row = getattr(read, "source_row", {}) or {}
            entry = row.get("entry")
            if not entry or not row.get("hasLevels"):
                continue

            structure = None
            atr = 0.0
            ret_20d = None
            try:
                bars = load_recent_prices(read.ticker, days=200, conn=conn)
                feats = compute_technical_features(bars)
                atr = float(feats.get("atr14") or 0.0)
                structure = build_structure(bars, atr)
                if len(bars) >= 21 and bars[-21].close:
                    ret_20d = (bars[-1].close - bars[-21].close) / bars[-21].close
            except Exception as exc:
                logger.debug("structure unavailable for %s: %s", read.ticker, exc)

            kol = None
            try:
                kol = kol_levels_for_ticker(conn, read.ticker, atr=atr, weights=weights)
            except Exception as exc:
                logger.debug("kol levels unavailable for %s: %s", read.ticker, exc)

            out[read.ticker.upper()] = valuate(
                ticker=read.ticker,
                side=read.side,
                entry=float(entry),
                stop=row.get("stop"),
                target=row.get("target"),
                atr=atr,
                provenance=row.get("levelProvenance") or [],
                structure=structure,
                kol=kol,
                macro_score=row.get("macro"),
                # The regime the row was SCORED under, not today's global
                # read. `macro_alignment_score` was computed against this
                # one; pairing it with a different label would describe a
                # fit that was never measured.
                regime_label=(row.get("trail") or {}).get("active_framework_regime")
                or row.get("regimeFit"),
                ret_20d=ret_20d,
                min_support=min_support,
            )
    return out
