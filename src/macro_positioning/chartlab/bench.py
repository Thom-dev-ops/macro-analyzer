"""Chart Lab bench — one ticker, one card, everything that already exists.

This module composes rather than computes. Every number on the card is
produced by the module that owns it:

- price/technicals/volume  → `prices.fetcher` + `prices.technicals` (yfinance, free)
- support/resistance zones → `prices.structure.build_structure`
- trusted-voice levels     → `scoring.kol_levels.kol_levels_for_ticker`
- the rails themselves     → `scoring.levels.synthesize_levels` (v2 fusion)
- framework setup name     → `scoring.setup_types.classify_setup_type`
- the grade                → `macro_brain.orchestrator.composer.compose`

The grade goes through `compose()` on purpose: that is the path carrying
the framework's 100-point weights, which is how `volume_flow_confirmation`
(15 points) stays in the screen instead of being quietly dropped — a
recurring failure in hand-rolled screens.

The only judgement added here is the exit architecture — trims, two
targets, headroom, and an early/fair/late read on where price sits in the
entry→target span.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from macro_brain.orchestrator.composer import compose
from macro_brain.types import SetupContext
from macro_positioning.chartlab.store import latest_read
from macro_positioning.core.settings import settings
from macro_positioning.market.fred_history import change_over, latest_value
from macro_positioning.prices.fetcher import fetch_and_persist, load_recent_prices
from macro_positioning.prices.structure import build_structure
from macro_positioning.prices.technicals import (
    compute_technical_features,
    compute_volume_features,
    pct_change,
)
from macro_positioning.scoring.kol_levels import author_weights, kol_levels_for_ticker
from macro_positioning.scoring.levels import synthesize_levels
from macro_positioning.scoring.setup_types import (
    classify_setup_type,
    has_structural_entry,
)

_BUSY_TIMEOUT_MS = 30_000

# Trim ladder from the desk's exit architecture: two scale-outs and a
# runner, never a single all-out exit.
_TRIM_FRACTIONS = (0.25, 0.25)



# ---------------------------------------------------------------------------
# Framework context — the inputs the composer needs beyond the chart
# ---------------------------------------------------------------------------
#
# Without these, five of the nine weighted components score a neutral 0.5
# for want of data and the composite says more about what the bench failed
# to supply than about the setup. Each one below is a local read — the
# same source `scoring.runner` uses for a full pass — so feeding them
# costs nothing and keeps a bench grade comparable to a scored one.
#
# `sector_theme_strength` (5 points) is deliberately NOT fed: it needs the
# cross-ticker theme rollup and percentile scale that only exist within a
# whole pass. It is reported as unfed rather than faked.

_BENCH_UNFED_COMPONENTS = {"sector_theme_strength"}


def _regime_read():
    """The active RegimeRead, or None when the blend can't produce one."""
    try:
        from macro_positioning.scoring.runner import _regime_read_from_blend

        with _connect() as conn:
            return _regime_read_from_blend(conn)
    except Exception:  # noqa: BLE001 — regime is context, not a gate
        return None


def _liquidity_features(conn, regime) -> dict:
    """NFCI snapshot, shaped as the liquidity_alignment scorer expects."""
    try:
        from macro_positioning.scoring.runner import _BULLISH_FRAMEWORK_REGIMES

        framework_regime = getattr(regime, "framework_regime", None)
        regime_bullish = framework_regime in _BULLISH_FRAMEWORK_REGIMES
        nfci_latest = latest_value(conn, "NFCI")
        nfci_4w_change = change_over(conn, "NFCI", days=28)
    except Exception:  # noqa: BLE001
        return {"source": "missing"}
    return {
        "nfci_latest": nfci_latest,
        "nfci_4w_change": nfci_4w_change,
        "regime_bullish": regime_bullish,
        "source": "fred:NFCI" if nfci_latest is not None else "missing",
    }


def _relative_strength_features(ticker: str, bars: list, asset_class: str) -> dict:
    """20d return vs the benchmark for this asset class."""
    try:
        from macro_positioning.scoring.runner import (
            _benchmark_for,
            _load_benchmarks_config,
        )

        bench_ticker = _benchmark_for(asset_class, _load_benchmarks_config())
        bench_bars = load_recent_prices(bench_ticker, days=60)
        return {
            "ticker_pct20d": pct_change([b.close for b in bars], 20),
            "benchmark_pct20d": (
                pct_change([b.close for b in bench_bars], 20) if bench_bars else None
            ),
            "benchmark_ticker": bench_ticker,
        }
    except Exception:  # noqa: BLE001
        return {}


def _signal_aggregate(ticker: str, side: str = "LONG") -> dict:
    """This ticker's signal aggregate, scaled against a real peer set and
    oriented to the side actually being proposed.

    `directional_scale` normalises net_bias against the spread of the
    whole pass. Handing it one ticker would make that ticker its own
    scale — every name would look maximally convicted. So the scale is
    computed over the active watchlist and only then is this ticker's
    aggregate pulled out of it.

    The orientation matters. `signal_alignment._compute` maps net_bias to
    0..1 with 1.0 = maximally BULLISH; it never reads the setup's side.
    Inside a scoring pass that is invisible, because the side there is
    itself derived from the bias (`side_from_signal_bias`) and the two
    always agree. The bench takes its side from the chart and from
    trusted-voice consensus, which can disagree with the book — and a
    short into a bullish book would otherwise score a perfect 15/15.

    So for a SHORT the directional fields are negated before scoring:
    "alignment" then means what the component name claims — do tracked
    signals support THIS trade — rather than "how bullish is this name".
    The shared scorer is left untouched; changing it would move every
    persisted trade score.
    """
    try:
        from macro_positioning.signals.aggregation import (
            aggregate_for_tickers,
            directional_scale,
        )

        peers: list[str] = []
        try:
            from macro_positioning.scoring.watchlist_resolver import resolve_watchlist

            resolved = resolve_watchlist(framework_regime="commodity_led_inflation")
            peers = [e.ticker for e in resolved.entries]
        except Exception:  # noqa: BLE001 — peers are for scale only
            peers = []
        universe = {*peers, ticker.upper()}
        aggregates = aggregate_for_tickers(universe)
        scale = directional_scale(aggregates)
        agg = aggregates.get(ticker.upper()) or {}
        if not agg:
            return {}
        oriented = {**agg, "pass_scale": scale}
        if (side or "LONG").upper() == "SHORT":
            oriented["net_bias"] = -float(agg.get("net_bias") or 0.0)
            oriented["long_weight"] = agg.get("short_weight")
            oriented["short_weight"] = agg.get("long_weight")
            oriented["_oriented_for"] = "SHORT"
        return oriented
    except Exception:  # noqa: BLE001
        return {}


@dataclass
class BenchCard:
    """Everything the desk needs to judge one chart."""

    ticker: str
    resolved_symbol: str | None
    close: float | None
    atr: float | None
    n_bars: int

    desk_read: dict | None = None
    side: str = "LONG"
    side_basis: str = ""

    supports: list[dict] = field(default_factory=list)
    resistances: list[dict] = field(default_factory=list)

    levels: dict | None = None
    levels_reason: str | None = None
    setup_type: str = ""

    kol_entry: dict | None = None
    kol_stop: dict | None = None
    kol_target: dict | None = None
    kol_n_signals: int = 0
    kol_played_out: int = 0

    grade: str | None = None
    score: float | None = None
    components: list[dict] = field(default_factory=list)

    desk_levels: dict | None = None
    exits: dict | None = None
    headroom_r: float | None = None
    timing: str = ""
    warnings: list[str] = field(default_factory=list)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.sqlite_path, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    return conn


def _side_from_read(read: dict | None) -> tuple[str | None, str]:
    """Direction the desk's own chart read implies, if it implies one.

    `call_type` gates this: a `no_trade` / `not_a_chart` read carries no
    direction, and a bias derived from one is fabricated. Bidirectional
    and retrospective reads are likewise not a side to trade.
    """
    if not read:
        return None, ""
    call_type = (read.get("call_type") or "").strip().lower()
    if call_type in {"no_trade", "not_a_chart", "bidirectional", "retrospective"}:
        return None, f"desk read is {call_type} — no directional side"
    setups = read.get("setups")
    if isinstance(setups, list) and setups:
        first = setups[0] if isinstance(setups[0], dict) else {}
        direction = (first.get("direction") or "").strip().lower()
        if direction in {"long", "short"}:
            return direction.upper(), "desk chart read (setup direction)"
    if call_type == "directional_long":
        return "LONG", "desk chart read (call_type)"
    if call_type == "directional_short":
        return "SHORT", "desk chart read (call_type)"
    bias = (read.get("bias") or "").strip().lower()
    if bias == "bullish":
        return "LONG", "desk chart read (bias)"
    if bias == "bearish":
        return "SHORT", "desk chart read (bias)"
    return None, ""


def _level_dict(level) -> dict:
    return {
        "price": level.price,
        "low": level.low,
        "high": level.high,
        "kind": level.kind,
        "touches": level.touches,
        "last_touch_bars": level.last_touch_bars,
        "strength": level.strength,
        "flipped": level.flipped,
        "round_number": level.round_number,
        "basis": level.basis,
    }


def _consensus_dict(consensus, close: float | None = None) -> dict | None:
    if consensus is None:
        return None
    pct_from_spot = None
    if close and consensus.price:
        pct_from_spot = (consensus.price - close) / close * 100.0
    return {
        "price": consensus.price,
        "pct_from_spot": pct_from_spot,
        "weight": consensus.weight,
        "trusted": consensus.trusted,
        "basis": consensus.basis,
        "contributors": [
            {
                "display_name": c.display_name,
                "price": c.price,
                "credential": c.credential,
                "at": c.at,
                "thesis": c.thesis,
            }
            for c in consensus.contributors
        ],
    }



def _desk_levels(read: dict | None, level_set, close: float | None) -> dict | None:
    """The desk's own numbers, side by side with the agent's.

    You drew these. The agent derived its own from structure and trusted
    voices. Where they disagree is the interesting part of the card — and
    a gap measured in R is the unit that makes the disagreement mean
    something, rather than a raw price difference that reads as small on
    a $84k chart and enormous on a $0.20 one.
    """
    if not read:
        return None
    setups = read.get("setups")
    if not isinstance(setups, list) or not setups:
        return None
    first = setups[0] if isinstance(setups[0], dict) else {}

    entry = first.get("entry")
    stop = first.get("stop_loss")
    tps = first.get("take_profits")
    tp1 = tps[0] if isinstance(tps, list) and tps else first.get("final_target")

    risk = None
    if entry is not None and stop is not None:
        try:
            risk = abs(float(entry) - float(stop))
        except (TypeError, ValueError):
            risk = None

    def gap(mine, agent) -> float | None:
        """Agent-vs-desk distance, in units of the desk's own risk."""
        if mine is None or agent is None or not risk:
            return None
        try:
            return (float(agent) - float(mine)) / risk
        except (TypeError, ValueError):
            return None

    rows = {
        "entry": {"desk": entry, "agent": level_set.entry, "gap_r": gap(entry, level_set.entry)},
        "stop": {"desk": stop, "agent": level_set.stop, "gap_r": gap(stop, level_set.stop)},
        "target": {"desk": tp1, "agent": level_set.target, "gap_r": gap(tp1, level_set.target)},
    }
    desk_rr = None
    if risk and entry is not None and tp1 is not None:
        try:
            desk_rr = abs(float(tp1) - float(entry)) / risk
        except (TypeError, ValueError):
            desk_rr = None

    triggered = None
    if close is not None and entry is not None:
        try:
            is_long = (first.get("direction") or "long").lower() == "long"
            triggered = (close <= float(entry)) if is_long else (close >= float(entry))
        except (TypeError, ValueError):
            triggered = None

    return {
        "rows": rows,
        "risk_per_unit": risk,
        "rr": desk_rr,
        "invalidation": first.get("invalidation"),
        "status": first.get("status"),
        "entry_reached": triggered,
    }


def _exit_architecture(level_set, structure, close: float | None) -> tuple[dict, float | None, str]:
    """Trims, a second target, headroom, and an early/fair/late read.

    `target` from `synthesize_levels` is target 1. Target 2 is the next
    structural level beyond it when one exists; when nothing overhead
    does, it is an R-multiple projection, labelled as the open-field
    convention it is rather than posed as a level.
    """
    entry = float(level_set.entry)
    stop = float(level_set.stop)
    t1 = float(level_set.target)
    risk = abs(entry - stop)
    is_long = level_set.side != "SHORT"
    sign = 1 if is_long else -1

    t2: float | None = None
    t2_basis = ""
    pool = structure.resistances if is_long else structure.supports
    beyond = [
        lv for lv in (pool or [])
        if (lv.price > t1 if is_long else lv.price < t1)
    ]
    if beyond:
        nxt = min(beyond, key=lambda lv: abs(lv.price - t1))
        t2 = nxt.price
        t2_basis = f"next {nxt.kind} zone — {nxt.basis}" if nxt.basis else f"next {nxt.kind} zone"
    elif risk > 0:
        t2 = entry + sign * risk * 3.0
        t2_basis = "3R projection — no structure overhead (open field), not a level"

    headroom_r: float | None = None
    if risk > 0 and t2 is not None:
        headroom_r = abs(t2 - entry) / risk

    # Where does spot sit in the entry→target span? Buying the back half
    # of a move is late even when the setup is sound.
    timing = "unknown"
    if close and risk > 0:
        span = abs(t1 - entry)
        if span > 0:
            progress = (close - entry) * sign / span
            if progress < 0.15:
                timing = "early"
            elif progress <= 0.55:
                timing = "fair"
            else:
                timing = "late"

    exits = {
        "trims": [
            {"fraction": _TRIM_FRACTIONS[0], "at": t1, "basis": "target 1 — first scale"},
            {
                "fraction": _TRIM_FRACTIONS[1],
                "at": t2,
                "basis": t2_basis or "target 2",
            },
        ],
        "runner": round(1.0 - sum(_TRIM_FRACTIONS), 2),
        "stop": stop,
        "move_stop_to_breakeven_at": t1,
        "risk_per_unit": risk,
        "rr_target_1": level_set.rr,
    }
    return exits, headroom_r, timing


def build_bench(
    ticker: str,
    *,
    days: int = 260,
    fetch: bool = True,
    side_override: str | None = None,
) -> BenchCard:
    """Assemble the single-ticker setup card."""
    want = (ticker or "").strip().upper()
    if not want:
        raise ValueError("ticker required")

    resolved: str | None = want
    try:
        from macro_positioning.prices.symbol_map import resolve_symbol

        resolved = resolve_symbol(want) or want
    except Exception:  # noqa: BLE001 — symbol map is an optimization, not a gate
        resolved = want

    bars = load_recent_prices(want, days=days)
    if not bars and fetch:
        fetch_and_persist([want], days=days)
        bars = load_recent_prices(want, days=days)

    feats = compute_technical_features(bars)
    vol_feats = compute_volume_features(bars) if bars else {"n_volume_bars": 0}
    close = feats.get("close")
    atr = feats.get("atr14")

    card = BenchCard(
        ticker=want,
        resolved_symbol=resolved,
        close=close,
        atr=atr,
        n_bars=int(feats.get("n_bars") or 0),
    )
    if not bars:
        card.warnings.append(
            f"no price bars for {want} — nothing to bench. "
            "Check the symbol, or run `macro-positioning prices fetch --ticker "
            f"{want}`."
        )
        return card

    read = latest_read(want)
    card.desk_read = read

    structure = build_structure(bars, atr, last_close=close)
    card.supports = [_level_dict(lv) for lv in structure.supports]
    card.resistances = [_level_dict(lv) for lv in structure.resistances]

    with _connect() as conn:
        try:
            weights = author_weights()
        except Exception:  # noqa: BLE001 — accuracy rollup is optional context
            weights = {}
        kol = kol_levels_for_ticker(
            conn, want, atr=atr, weights=weights, close=close
        )

    card.kol_entry = _consensus_dict(kol.entry, close)
    card.kol_stop = _consensus_dict(kol.stop, close)
    card.kol_target = _consensus_dict(kol.target, close)
    card.kol_n_signals = kol.n_signals
    card.kol_played_out = kol.played_out

    # Side precedence: explicit override, then the desk's own read of the
    # chart in front of it, then trusted-voice consensus, then long.
    side, basis = None, ""
    if side_override:
        side, basis = side_override.strip().upper(), "explicit --side"
    if side is None:
        side, basis = _side_from_read(read)
    if side is None and kol.side:
        side, basis = kol.side.upper(), f"trusted-voice consensus ({kol.n_signals} signals)"
    if side is None:
        side, basis = "LONG", "default — no directional read available"
        card.warnings.append(
            "No directional read: neither your chart nor a trusted voice names a "
            "side. The card below assumes LONG — treat it as structure, not a call."
        )
    card.side, card.side_basis = side, basis

    level_set, reason = synthesize_levels(
        feats, side, structure=structure, kol=kol
    )
    card.levels_reason = reason
    if level_set is None:
        card.warnings.append(f"no levels: {reason}")
        return card

    card.levels = level_set.to_dict()
    if not level_set.structural:
        card.warnings.append(
            "Rails are mechanical ATR placeholders, not structure — no real "
            "setup exists on this chart yet (watchlist, not entry)."
        )

    card.setup_type = classify_setup_type(
        method=level_set.method,
        structural=level_set.structural,
        side=side,
        ticker=want,
        asset_class=(read or {}).get("asset_class") or "equity",
        themes=None,
        rs_features=None,
        regime=_regime_key_of(_regime_read()),
    )

    # v2 can adopt a real structure zone for the stop (structural=True)
    # while the ENTRY is still a mechanical rail at spot. The classifier
    # names the entry, so it says `watchlist_building` — right about the
    # entry, and still odd-looking beside a structural stop and an A
    # grade. Say which half is real rather than let the card read as a
    # contradiction.
    if level_set.structural and not has_structural_entry(
        level_set.method, level_set.structural
    ):
        card.warnings.append(
            "Stop is structural but the ENTRY is a mechanical rail at spot — "
            "no entry pattern fired. Classified `watchlist_building`: this is a "
            "level to watch, not a trigger to take. Wait for the retest."
        )

    card.desk_levels = _desk_levels(read, level_set, close)
    if card.desk_levels and card.desk_levels.get("rr") is not None:
        if card.desk_levels["rr"] < 2.0:
            card.warnings.append(
                f"Your own R:R is {card.desk_levels['rr']:.1f} — under 2R before "
                "costs. The agent's rails give "
                f"{level_set.rr:.1f}R; the difference is where your stop sits."
            )

    exits, headroom_r, timing = _exit_architecture(level_set, structure, close)
    card.exits, card.headroom_r, card.timing = exits, headroom_r, timing
    if headroom_r is not None and headroom_r < 2.0:
        card.warnings.append(
            f"Headroom only {headroom_r:.1f}R to target 2 — thin for the risk taken."
        )

    asset_class = (read or {}).get("asset_class") or "equity"
    regime = _regime_read()
    with _connect() as conn:
        liquidity = _liquidity_features(conn, regime)
    sig_agg = _signal_aggregate(want, side)
    # The book disagreeing with the trade in front of you is a fact worth
    # stating outright, not something to leave buried in a sub-score.
    book_dir = (sig_agg.get("bias_direction") or "").upper()
    if book_dir in {"LONG", "SHORT"} and book_dir != side:
        card.warnings.append(
            f"Tracked signals lean {book_dir} ({sig_agg.get('n_signals')} signals, "
            f"{float(sig_agg.get('bias_confidence') or 0):.0%} conf) against this "
            f"{side}. You are trading opposite the book."
        )
    score = _grade(
        ticker=want,
        asset_class=asset_class,
        setup_type=card.setup_type,
        feats=feats,
        vol_feats=vol_feats,
        level_set=level_set,
        regime=regime,
        liquidity=liquidity,
        rs_features=_relative_strength_features(want, bars, asset_class),
        signal_aggregate=sig_agg,
    )
    if score is not None:
        card.grade = getattr(score, "grade", None)
        card.score = getattr(score, "adjusted_total_score", None)
        card.components = _component_rows(score)
        stub_weight = sum(r["weight"] for r in card.components if r["stub"])
        if stub_weight >= 25:
            card.warnings.append(
                f"{stub_weight} of 100 grade points are neutral stubs — the bench "
                "supplies chart inputs only (technicals, volume, R:R). Read the "
                "grade as structure quality, not a full framework score; "
                "`score run` is what scores macro, liquidity, themes and signals."
            )
    return card


def _regime_key_of(regime) -> str | None:
    """The framework regime name, for setup-type vocabulary selection."""
    if regime is None:
        return None
    return getattr(regime, "framework_regime", None) or getattr(regime, "regime", None)


def _grade(
    *,
    ticker: str,
    asset_class: str,
    setup_type: str,
    feats: dict,
    vol_feats: dict,
    level_set,
    regime=None,
    liquidity: dict | None = None,
    rs_features: dict | None = None,
    signal_aggregate: dict | None = None,
) -> Any | None:
    """Score through the framework composer so every weight applies."""
    try:
        setup = SetupContext(
            setup_id=f"chartlab-{ticker.lower()}",
            asset_ticker=ticker,
            asset_class=asset_class,
            setup_type=setup_type,
            active_regime=regime,
            entry_zone=level_set.entry,
            stop_loss=level_set.stop,
            target=level_set.target,
            technical_features=feats,
            volume_features=vol_feats,
            liquidity_features=liquidity or {},
            relative_strength_features=rs_features or {},
            signal_aggregate=signal_aggregate or {},
        )
        return compose(setup)
    except Exception:  # noqa: BLE001 — a card without a grade still helps
        return None


def _component_rows(score) -> list[dict]:
    """Per-component rows for the card, weights included."""
    from macro_brain.types import COMPONENT_WEIGHTS

    rows: list[dict] = []
    subs = getattr(score, "sub_scores", None) or []
    for sub in subs:
        component = getattr(sub, "component", None)
        if component is None:
            continue
        notes = getattr(sub, "notes", "") or ""
        rows.append(
            {
                "component": component,
                "value": getattr(sub, "value", None),
                "weight": COMPONENT_WEIGHTS.get(component, 0),
                "notes": notes,
                # A stub is a neutral 0.5 standing in for an input the
                # bench never supplied (regime, liquidity, themes, tracked
                # signals). It is not a reading of this chart, and a grade
                # resting on several of them says more about what the bench
                # was not given than about the setup.
                "stub": (
                    component in _BENCH_UNFED_COMPONENTS
                    or notes.startswith("stub")
                ),
            }
        )
    return rows
