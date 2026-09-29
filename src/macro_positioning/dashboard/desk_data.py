"""Desk snapshot — the unified MA_DATA payload for the new SPA dashboard.

Lives at `web/index.html` (Claude Design output). The SPA reads
`window.MA_DATA` once on mount, set by `web/data.js` which is served
dynamically by `desk_routes.py` from this module.

This module's job is to return a single dict that matches the contract
in `web/HANDOFF.md` AND the actual JSX field accesses in
`web/{positioning,journal,dev,reasoning,mobile,app}.jsx` — the mock
in `web/data.mock.js` is the canonical schema reference. The shapes
here MUST match the mock's keys precisely or React renders blank.

Where real backend data exists, use it; where it doesn't, return an
empty-but-structurally-valid shape so the UI still renders without
errors. Each section is its own focused builder so we can swap stubs
for real data section-by-section.

Contract sections (16 — see HANDOFF.md):
1. regime              — REAL via macro_brain regime classifier
2. kpis                — STUB (zero-state until trades + brain log)
3. heroSignals[]       — STUB (top scored setups; needs scoring run)
4. watchlist[]         — STUB (full scored watchlist)
5. activeTrades[]      — REAL (empty until first manual trade logged)
6. reasoning{}         — STUB (per-setup explain blob; keyed by signalId)
7. closedTrades[]      — REAL (empty until first trade closed)
8. missedTrades[]      — REAL (empty until first miss logged)
9. processScorecard    — STUB shape (days/score/metrics)
10. sourceLeaderboard[]— REAL (from source_outcomes table; empty initially)
11. thesisChangelog[]  — REAL (seeded from current thesis version)
12. brainActivity[]    — REAL via existing brain_panel
13. sourceHealth[]     — REAL from sources.json + freshness scoring
14. costTracker        — REAL via brain_panel cost data
15. mgmt               — REAL via existing mgmt_data builder
16. integration        — REAL via tactical_client
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Optional

from macro_positioning.core.settings import settings
from macro_positioning.dashboard.checklist import load_checklist
from macro_positioning.dashboard.mgmt_data import _git_recent_commits, _load_decisions
from macro_positioning.ingestion.freshness import freshness_label
from macro_positioning.ingestion.source_lifecycle import load_sources
from macro_positioning.manual.authors import SEEDED_AUTHOR_WHERE

from macro_brain.agents.regime_classifier.classifier import classify_regime_stub


# ---------------------------------------------------------------------------
# Macro indicator strip — 1-hour in-process cache
# ---------------------------------------------------------------------------

_INDICATORS_CACHE: dict = {"ts": 0.0, "data": None}
_INDICATORS_TTL_S = 3600

# Minimal FRED series needed for the three classifiers (avoids fetching all 50+)
_INDICATOR_FRED_SERIES = {
    "A191RL1Q225SBEA": ("growth", "Real GDP QoQ Annualised", "%"),
    "INDPRO":          ("growth", "Industrial Production Index", "index"),
    "T10YIE":          ("rates", "10Y Breakeven Inflation", "%"),
    "CPIAUCSL":        ("inflation", "CPI All Urban Consumers", "index"),
    "NFCI":            ("financial_conditions", "Chicago Fed NFCI", "index"),
    "ANFCI":           ("financial_conditions", "Adjusted NFCI", "index"),
    "VIXCLS":          ("financial_conditions", "VIX", "index"),
    "TEDRATE":         ("financial_conditions", "TED Spread", "%"),
    "BAMLH0A0HYM2":    ("financial_conditions", "HY OAS Spread", "%"),
    "USEPUINDXD":      ("geopolitics", "US EPU (daily)", "index"),
    "GEPUCURRENT":     ("geopolitics", "Global EPU", "index"),
    "EPUTRADE":        ("geopolitics", "Trade Policy Uncertainty", "index"),
    "EPUFISCAL":       ("geopolitics", "Fiscal Policy Uncertainty", "index"),
    "EPUMONETARY":     ("geopolitics", "Monetary Policy Uncertainty", "index"),
    "EMVNATSEC":       ("geopolitics", "Equity Vol: National Security", "index"),
}


def _build_indicator_observations() -> tuple[list, bool]:
    """Return (observations, used_db). DB-first: read latest values from
    fred_observations and synthesise MarketObservation rows. On cold cache
    (no rows) fall back to a live FRED HTTP fetch. SPA contract preserved
    because the upstream classifiers only inspect metric/source/value.
    """
    import sqlite3 as _sqlite3
    from macro_positioning.core.models import MarketObservation
    from macro_positioning.core.settings import settings as _settings
    from macro_positioning.db.schema import initialize_database
    from macro_positioning.market.fred_history import latest_value

    obs: list = []
    try:
        initialize_database(_settings.sqlite_path)
        with _sqlite3.connect(_settings.sqlite_path) as conn:
            for sid, (market, metric, unit) in _INDICATOR_FRED_SERIES.items():
                v = latest_value(conn, sid)
                if v is None:
                    continue
                obs.append(MarketObservation(
                    observation_id=f"db-{sid}",
                    market=market,
                    metric=metric,
                    value=f"{v} {unit}".strip(),
                    interpretation=f"{metric}: {v} {unit}",
                    source=f"FRED:{sid}",
                ))
        if obs:
            return obs, True
    except Exception:
        import sys
        print("[desk] DB-first indicator read failed; falling back to live FRED.", file=sys.stderr)

    # Cold-cache fallback: live FRED HTTP fetch (original path).
    try:
        from macro_positioning.market.fred_provider import FREDMarketDataProvider
        provider = FREDMarketDataProvider(series=_INDICATOR_FRED_SERIES, timeout=10.0)
        return provider.gather(theses=[]), False
    except Exception:
        return [], False


def _build_macro_indicators() -> dict | None:
    """Fetch FRED + COT data, run classifiers, return indicator strip dict.

    Caches result for 1 hour to avoid FRED round-trips on every dashboard load.
    Returns None on any failure so the strip silently hides itself.
    """
    now = datetime.now(UTC).timestamp()
    if _INDICATORS_CACHE["ts"] > now - _INDICATORS_TTL_S and _INDICATORS_CACHE["data"] is not None:
        return _INDICATORS_CACHE["data"]

    try:
        from macro_positioning.market.macro_indicators import (
            classify_growth_inflation_quadrant,
            compute_fci,
            compute_geopolitical_risk,
            compute_cot_positioning,
        )
        from macro_positioning.market.cot_provider import fetch_cot_readings

        obs, used_db = _build_indicator_observations()

        if used_db:
            # DB-first: pass conn to compute_fci for richer reads;
            # synthesise observations for quadrant + EPU.
            import sqlite3 as _sqlite3
            from macro_positioning.core.settings import settings as _settings
            with _sqlite3.connect(_settings.sqlite_path) as _conn:
                fci = compute_fci(obs, conn=_conn)
        else:
            fci = compute_fci(obs)

        quadrant = classify_growth_inflation_quadrant(obs)
        epu = compute_geopolitical_risk(obs)

        cot_readings = fetch_cot_readings()
        cot = compute_cot_positioning(cot_readings)

        top_extreme = cot.extremes[0] if cot.extremes else None

        result: dict = {
            "quadrant":         quadrant.quadrant,
            "growthSignal":     quadrant.growth_signal,
            "inflationSignal":  quadrant.inflation_signal,
            "quadrantConf":     quadrant.confidence,
            "fciLabel":         fci.label,
            "fciScore":         fci.score,
            "epuLevel":         epu.level,
            "epuComposite":     epu.composite_score,
            "epuDriver":        epu.dominant_driver,
            "cotTopSignal":     top_extreme.signal if top_extreme else "neutral",
            "cotTopMarket":     top_extreme.market if top_extreme else None,
            "cotTopNetPctOi":   top_extreme.net_pct_oi if top_extreme else None,
            "cotExtremesCount": len(cot.extremes),
            "cotAsOf":          cot.as_of.isoformat() if cot.as_of else None,
        }

        _INDICATORS_CACHE["ts"] = now
        _INDICATORS_CACHE["data"] = result
        return result

    except Exception as exc:
        import sys
        print(f"[desk] macro indicators build failed: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

_FRAMEWORK_REGIME_LABELS = {
    "risk_on_expansion": "Risk-On Expansion",
    "risk_off_contraction": "Risk-Off Contraction",
    "commodity_led_inflation": "Commodity-Led Inflation",
    "monetary_debasement_hard_asset": "Monetary Debasement / Hard Asset",
    "transitional_chop": "Transitional Chop",
}

_REGIME_BIASES = {
    "risk_on_expansion": ("bullish", 1.25, 10),
    "risk_off_contraction": ("defensive", 0.5, -15),
    "commodity_led_inflation": ("real_asset_bullish", 1.1, 8),
    "monetary_debasement_hard_asset": ("scarcity_asset_bullish", 1.0, 6),
    "transitional_chop": ("neutral_to_cautious", 0.65, -8),
}

# Map source_type → human-readable "kind" displayed in the UI
_SOURCE_TYPE_DISPLAY = {
    "newsletter": "Newsletter",
    "podcast": "Podcast",
    "rss": "RSS",
    "api": "API",
    "gmail": "Gmail",
    "manual_notes": "Notes",
    "chart": "Chart",
}


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def build_regime_section() -> dict:
    """Active regime read with real 90-day confidence trace + transitions
    from `macro_regimes` (populated by daily_free_ingest via the
    regime.snapshots writer). Falls back to the stub classifier + a flat
    trace if the table is empty."""
    from macro_positioning.regime.snapshots import (
        derive_transitions,
        load_regime_history,
        since_days_for_current,
    )

    # Load real history first — this is the primary path once daily
    # snapshots exist.
    history: list[dict] = []
    try:
        with sqlite3.connect(settings.sqlite_path) as conn:
            history = load_regime_history(conn, days=90)
    except sqlite3.Error:
        history = []

    if history:
        # Most-recent snapshot drives the "current" read.
        last = history[-1]
        current_slug = last["framework_regime"]
        current_conf = last["confidence"]
        bias, sizing, score_mod = _REGIME_BIASES.get(current_slug, ("neutral", 1.0, 0))
        trace = [round(s["confidence"], 3) for s in history]
        trace_dates = [s["date"] for s in history]
        transitions = derive_transitions(history)
        since_days = since_days_for_current(history)
    else:
        # Fallback: no daily snapshots yet → keep the stub shape so the
        # SPA still renders (it will show the "pending" pill).
        rr = classify_regime_stub(hint_thesis_regime="commodity_expansion")
        current_slug = rr.framework_regime
        current_conf = rr.confidence
        bias, sizing, score_mod = _REGIME_BIASES.get(current_slug, ("neutral", 1.0, 0))
        trace = [round(current_conf, 2)] * 84
        trace_dates = []
        transitions = [{
            "date": datetime.now(UTC).date().isoformat(),
            "from": "Transitional Chop",
            "to": _FRAMEWORK_REGIME_LABELS.get(current_slug, current_slug),
        }]
        since_days = 14

    out = {
        "framework": {
            "label": _FRAMEWORK_REGIME_LABELS.get(current_slug, current_slug),
            "slug": current_slug,
            "confidence": round(current_conf, 2),
            "bias": bias,
            "sizingModifier": sizing,
            "scoreModifier": score_mod,
            "sinceDays": since_days,
        },
        "thesis": {
            "label": "Two-Speed · Liquidity ↔ Structural",
            "narrative": (
                "Liquidity sets the pace, structural energy / resource stress sets "
                "leadership. Capital rotating from index beta into real assets faster "
                "than fundamentals are repaired."
            ),
            "version": "v3",
            "author": "Lindsey",
            "lastRevised": "2026-04-22",
        },
        "confidenceTrace": trace,
        "confidenceTraceDates": trace_dates,  # ISO YYYY-MM-DD per trace index
        "transitions": transitions,
    }
    out["indicators"] = _build_macro_indicators()  # None = strip hides itself
    return out


def build_kpis_section() -> dict:
    """KPI strip aggregates — all sourced from real tables with graceful
    fallbacks. Each block runs in its own try so one missing table
    doesn't blank the whole strip.
    """
    rows = _load_latest_scores()
    high = sum(1 for r in rows if r["score"] >= 75)
    # delta-vs-yesterday: count of yesterday's >=75 rows, looked up from the
    # *prior* pass for each asset.
    high_yesterday = sum(
        1 for r in rows
        if r.get("prior_score") is not None and r["prior_score"] >= 75
    )

    active = _build_active_trades_kpi()
    pnl_today = _build_pnl_today_kpi()
    cash = _build_cash_posture_kpi()
    spend = _build_spend_today_kpi()

    return {
        "cashPosture": cash,
        "activeTrades": active,
        "pnlToday": pnl_today,
        "pnlWeek": {"usd": 0.0, "pct": 0.0},   # weekly P&L deferred — needs daily series
        "signalsHigh": {"count": high, "deltaVsYesterday": high - high_yesterday},
        "spendToday": spend,
    }


def _build_active_trades_kpi() -> dict:
    """Open trades count + total USD exposure (position_size × entry_price)."""
    if not settings.sqlite_path.exists():
        return {"count": 0, "exposureUsd": 0}
    try:
        with sqlite3.connect(settings.sqlite_path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(position_size * entry_price), 0) AS exposure
                FROM trades
                WHERE status = 'open'
                """
            ).fetchone()
        return {
            "count": int(row[0] or 0),
            "exposureUsd": float(row[1] or 0.0),
        }
    except Exception:
        return {"count": 0, "exposureUsd": 0}


def _build_pnl_today_kpi() -> dict:
    """Realized PnL from trades closed today + unrealized from open trades.

    Unrealized = sum over open trades of (latest_close - entry_price) *
    position_size. Latest_close pulled from `prices` table; missing
    price → that trade contributes zero.
    """
    if not settings.sqlite_path.exists():
        return {"usd": 0.0, "pct": 0.0}
    try:
        with sqlite3.connect(settings.sqlite_path) as conn:
            realized_row = conn.execute(
                """
                SELECT COALESCE(SUM(pnl), 0) AS realized,
                       COALESCE(SUM(position_size * entry_price), 0) AS basis
                FROM trades
                WHERE status = 'closed'
                  AND DATE(exit_date) = DATE('now')
                """
            ).fetchone()
            realized = float(realized_row[0] or 0.0)
            basis_closed = float(realized_row[1] or 0.0)

            # Unrealized over open trades. Join to latest price per ticker.
            unrealized_row = conn.execute(
                """
                SELECT COALESCE(SUM(
                    (latest.close - t.entry_price) * t.position_size
                ), 0) AS unrealized,
                COALESCE(SUM(t.position_size * t.entry_price), 0) AS basis
                FROM trades t
                JOIN assets a ON a.asset_id = t.asset_id
                JOIN (
                    SELECT ticker, close,
                           ROW_NUMBER() OVER (PARTITION BY ticker
                                              ORDER BY observed_at DESC) AS rn
                    FROM prices
                ) latest ON latest.ticker = a.ticker AND latest.rn = 1
                WHERE t.status = 'open'
                """
            ).fetchone()
            unrealized = float(unrealized_row[0] or 0.0)
            basis_open = float(unrealized_row[1] or 0.0)
        total_usd = realized + unrealized
        basis = basis_closed + basis_open
        pct = (total_usd / basis * 100.0) if basis > 0 else 0.0
        return {"usd": round(total_usd, 2), "pct": round(pct, 2)}
    except Exception:
        return {"usd": 0.0, "pct": 0.0}


def _build_cash_posture_kpi() -> dict:
    """Latest portfolio_exposure_snapshots row → posture label + pct."""
    if not settings.sqlite_path.exists():
        return {"label": "Neutral", "pct": 50, "delta": 0}
    try:
        with sqlite3.connect(settings.sqlite_path) as conn:
            row = conn.execute(
                """
                SELECT pct_deployed
                FROM portfolio_exposure_snapshots
                ORDER BY taken_at DESC
                LIMIT 1
                """
            ).fetchone()
            prev = conn.execute(
                """
                SELECT pct_deployed
                FROM portfolio_exposure_snapshots
                WHERE taken_at < (
                    SELECT MAX(taken_at) FROM portfolio_exposure_snapshots
                )
                ORDER BY taken_at DESC
                LIMIT 1
                """
            ).fetchone()
    except Exception:
        return {"label": "Neutral", "pct": 50, "delta": 0}
    if not row:
        return {"label": "Neutral", "pct": 50, "delta": 0}
    pct = float(row[0] or 0.0)
    cash_pct = 100.0 - pct
    if pct < 30:
        label = "Defensive"
    elif pct < 70:
        label = "Neutral"
    else:
        label = "Aggressive"
    delta = 0
    if prev:
        delta = round(pct - float(prev[0] or 0.0), 1)
    return {"label": label, "pct": round(cash_pct, 1), "delta": delta}


def _build_spend_today_kpi() -> dict:
    """Sum estimated_cost_usd from agent_call_log since midnight."""
    if not settings.sqlite_path.exists():
        return {"usd": 0.0, "capUsd": 25.0}
    try:
        with sqlite3.connect(settings.sqlite_path) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(estimated_cost_usd), 0)
                FROM agent_call_log
                WHERE called_at >= DATE('now')
                """
            ).fetchone()
        return {"usd": round(float(row[0] or 0.0), 4), "capUsd": 25.0}
    except Exception:
        return {"usd": 0.0, "capUsd": 25.0}


# ---------------------------------------------------------------------------
# Scored signals — pulled from trade_scores (populated by `score run`)
# ---------------------------------------------------------------------------

_TIER_LOOKUP = {"tier_1": 1, "tier_2": 2, "tier_3": 3, "avoid": 4}


def _load_latest_scores() -> list[dict]:
    """Pull the most-recent + previous trade_score per asset, joining
    technical_setups + assets, so we can compute dScore (today vs prior
    pass) without a second query.

    The window function ranks scores by scored_at DESC; rn=1 is current,
    rn=2 is prior. Outer LEFT JOIN brings rn=2 onto the rn=1 row.
    """
    if not settings.sqlite_path.exists():
        return []
    with sqlite3.connect(settings.sqlite_path) as conn:
        cur = conn.execute(
            """
            WITH ranked AS (
                SELECT
                    ts.score_id,
                    ts.scored_at,
                    ts.adjusted_total_score,
                    ts.raw_total_score,
                    ts.macro_alignment_score,
                    ts.liquidity_score,
                    ts.sector_theme_score,
                    ts.technical_structure_score,
                    ts.volume_flow_score,
                    ts.risk_reward_score,
                    ts.relative_strength_score,
                    ts.psychology_score,
                    ts.signal_alignment_score,
                    ts.grade,
                    ts.position_size_tier,
                    ts.reasoning_trail_json,
                    ts.feature_vector_json,
                    tset.asset_id AS asset_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY tset.asset_id
                        ORDER BY ts.scored_at DESC
                    ) AS rn
                FROM trade_scores ts
                JOIN technical_setups tset ON tset.setup_id = ts.setup_id
            ),
            current AS (
                SELECT * FROM ranked WHERE rn = 1
            ),
            prior AS (
                SELECT asset_id,
                       adjusted_total_score AS prior_score,
                       scored_at AS prior_scored_at
                FROM ranked WHERE rn = 2
            )
            SELECT
                c.score_id, c.scored_at, c.adjusted_total_score, c.raw_total_score,
                c.macro_alignment_score, c.liquidity_score, c.sector_theme_score,
                c.technical_structure_score, c.volume_flow_score, c.risk_reward_score,
                c.relative_strength_score, c.psychology_score,
                c.grade, c.position_size_tier, c.reasoning_trail_json,
                a.ticker, a.asset_name, a.asset_class,
                p.prior_score, p.prior_scored_at,
                c.signal_alignment_score,
                ts_full.signal_aggregate_json,
                c.feature_vector_json
            FROM current c
            JOIN assets a ON a.asset_id = c.asset_id
            LEFT JOIN prior p ON p.asset_id = c.asset_id
            LEFT JOIN trade_scores ts_full ON ts_full.score_id = c.score_id
            ORDER BY c.adjusted_total_score DESC
            """
        )
        rows = cur.fetchall()

    out = []
    for r in rows:
        try:
            trail = json.loads(r[14]) if r[14] else {}
        except Exception:
            trail = {}
        try:
            signal_agg = json.loads(r[21]) if r[21] else {}
        except Exception:
            signal_agg = {}
        # feature_vector_json carries the technical agent's LevelSet
        # (or the reason it declined to produce one).
        try:
            feature_vec = json.loads(r[22]) if r[22] else {}
        except Exception:
            feature_vec = {}
        levels = feature_vec.get("levels") or None
        levels_reason = feature_vec.get("levels_reason")
        prior_score = r[18]
        d_score = (r[2] - prior_score) if prior_score is not None else 0
        out.append({
            "score_id": r[0],
            "scored_at": r[1],
            "score": r[2],
            "raw_score": r[3],
            "macro": r[4],
            "liquidity": r[5],
            "sector_theme": r[6],
            "tech": r[7],
            "vol": r[8],
            "rr": r[9],
            "rs": r[10],
            "psych": r[11],
            "grade": r[12],
            "tier": r[13],
            "trail": trail,
            "ticker": r[15],
            "name": r[16],
            "asset_class": r[17],
            "prior_score": prior_score,
            "prior_scored_at": r[19],
            "signal": r[20],
            "signal_aggregate": signal_agg,
            "levels": levels,
            "levels_reason": levels_reason,
            "d_score": d_score,
        })
    return out


def _origins_to_pretty(origins: list[str]) -> list[str]:
    """Render watchlist origins for display:
       'anchor'             → 'anchor'
       'theme:uranium'      → 'uranium'
       'mentions:7d:12'     → '7d · 12 mentions'
    """
    out = []
    for o in origins or []:
        if o == "anchor":
            out.append("anchor")
        elif o.startswith("theme:"):
            out.append(o.split(":", 1)[1])
        elif o.startswith("mentions:"):
            parts = o.split(":")
            window = parts[1] if len(parts) > 1 else "?"
            count = parts[2] if len(parts) > 2 else "?"
            out.append(f"{window} · {count} mentions")
        else:
            out.append(o)
    return out


def _tier_str(tier_str: str) -> int:
    return _TIER_LOOKUP.get(tier_str, 4)


def _side_from_tier(tier_str: str) -> str:
    """Coarse heuristic until brain emits side: T1/T2 = LONG, T3 = WATCH, avoid = AVOID."""
    if tier_str == "avoid":
        return "AVOID"
    if tier_str == "tier_3":
        return "WATCH"
    return "LONG"


def _levels_for_row(r: dict) -> dict:
    """Flatten the persisted LevelSet into the SPA card shape.

    The SPA calls .toFixed() on entry/stop/target, so a level-less row
    still gets numeric zeros — `hasLevels` gates rendering, and
    `levelsReason` says *why* there are none (no_price / no_atr) rather
    than the old blanket "awaiting technical agent".
    """
    lv = r.get("levels") or None
    if not lv:
        return {
            "hasLevels": False,
            "entry": 0.0, "stop": 0.0, "target": 0.0, "rr": 0.0,
            "setup": "watchlist scoring pass",
            "levelMethod": None,
            "levelStructural": False,
            "riskPct": 0.0,
            "levelNotes": [],
            "levelsReason": r.get("levels_reason"),
        }
    return {
        "hasLevels": True,
        "entry": round(float(lv.get("entry") or 0.0), 6),
        "stop": round(float(lv.get("stop") or 0.0), 6),
        "target": round(float(lv.get("target") or 0.0), 6),
        "rr": round(float(lv.get("rr") or 0.0), 2),
        "setup": lv.get("setup") or "watchlist scoring pass",
        "levelMethod": lv.get("method"),
        "levelStructural": bool(lv.get("structural")),
        "riskPct": round(float(lv.get("riskPct") or 0.0), 4),
        "levelNotes": lv.get("notes") or [],
        # The framework-vocabulary name macro alignment scores against.
        "frameworkSetup": lv.get("frameworkSetup"),
        # Where each rail came from (structure zone / trusted voice /
        # open-field projection) and what was refused, with reasons.
        "levelProvenance": lv.get("provenance") or [],
        "levelRejected": lv.get("rejected") or [],
        "levelsReason": None,
    }


def _side_with_levels(tier_str: str, levels: dict | None) -> str:
    """Tier decides WATCH/AVOID; the technical agent decides LONG vs SHORT."""
    base = _side_from_tier(tier_str)
    if base == "LONG" and (levels or {}).get("side") == "SHORT":
        return "SHORT"
    return base


def _why_now_bullets(r: dict, origins: list[str], lv: dict) -> list[str]:
    """Scannable "why is this here" bullets — shared by hero cards and
    watchlist rows so a row opened from the table reads identically to
    the same asset opened from a hero card."""
    trail = r.get("trail") or {}
    why: list[str] = []
    if trail.get("active_thesis_regime"):
        why.append(
            f"Regime fit: {trail['active_thesis_regime']} "
            f"({trail.get('active_framework_regime', '—')})"
        )
    if trail.get("regime_modifier"):
        why.append(f"Regime modifier: {trail['regime_modifier']:+d}")
    if origins:
        why.append("Watchlist source: " + ", ".join(_origins_to_pretty(origins)))
    if lv.get("hasLevels"):
        why.append(
            f"Levels: {lv['setup']} · {lv['rr']:.2f}R · "
            f"{lv['riskPct'] * 100:.1f}% to invalidation"
        )
    return why


def _signal_shape(r: dict) -> dict:
    """The object the SPA's AssetPage consumes. Hero cards and watchlist
    rows both emit it, so every scored asset opens a full detail page —
    not just the top 5."""
    origins = (r.get("trail") or {}).get("watchlist_origins", []) or []
    lv = _levels_for_row(r)
    return {
        "id": r["score_id"],
        "asset": r["ticker"],
        "name": r["name"] or r["ticker"],
        "side": _side_with_levels(r["tier"], r.get("levels")),
        "score": r["score"],
        "scorePrev": r["prior_score"] if r["prior_score"] is not None else r["score"],
        "tier": _tier_str(r["tier"]),
        "regimeFit": (r.get("trail") or {}).get("active_framework_regime", "unknown"),
        "whyNow": _why_now_bullets(r, origins, lv),
        "sources": _origins_to_pretty(origins),
        "lastUpdate": (r["scored_at"] or "")[11:16],  # HH:MM slice
        # entry/stop/target/rr/setup + level provenance
        **lv,
    }


def build_hero_signals_section() -> list[dict]:
    """Top 5 scored setups (from trade_scores).

    Schema (per data.mock.js heroSignals[]):
      id, asset, name, side, score, scorePrev, tier, setup,
      regimeFit, entry, stop, target, rr, whyNow[], sources[], lastUpdate
    """
    return [_signal_shape(r) for r in _load_latest_scores()[:5]]


def build_watchlist_section() -> list[dict]:
    """Full scored watchlist (every ticker scored in the most recent pass).

    Schema (per data.mock.js watchlist[]):
      asset, side, score, dScore, tier, regime, tech, vol, rr, last
    Plus extras the SPA may use later: name, origins[], assetClass.
    """
    rows = _load_latest_scores()
    out = []
    for r in rows:
        origins = r["trail"].get("watchlist_origins", []) if r["trail"] else []
        out.append({
            # Table columns …
            "assetClass": r["asset_class"],
            "dScore": r["d_score"],
            "regime": "fit" if r["macro"] >= 12 else ("mix" if r["macro"] >= 6 else "off"),
            "tech": _grade_letter(r["tech"], 20),
            "vol": _grade_letter(r["vol"], 15),
            "last": (r["scored_at"] or "")[11:16],
            "origins": _origins_to_pretty(origins),
            # … plus the full AssetPage signal shape (id, levels, whyNow),
            # so clicking any row opens the same detail page a hero card
            # does. rr/side/setup come from the technical agent here.
            **_signal_shape(r),
        })
    return out


def _grade_letter(score: int, max_score: int) -> str:
    """Convert 0..max sub-score to A/B/C letter grade for compact display."""
    if max_score <= 0:
        return "—"
    pct = score / max_score
    if pct >= 0.9: return "A"
    if pct >= 0.8: return "A-"
    if pct >= 0.7: return "B+"
    if pct >= 0.6: return "B"
    if pct >= 0.5: return "B-"
    if pct >= 0.4: return "C+"
    if pct >= 0.3: return "C"
    return "D"


def build_price_series_section(days: int = 180) -> dict[str, list[float]]:
    """Daily closes per scored ticker — {ticker: [close, …]} oldest first.

    The asset page's PriceChart and the SetupCard divergence check both
    read `MA_DATA.priceSeries`; before this the key existed only in
    data.mock.js, so every real asset rendered "No price series".
    """
    rows = _load_latest_scores()
    tickers = [r["ticker"] for r in rows if r.get("ticker")]
    if not tickers or not settings.sqlite_path.exists():
        return {}
    cutoff = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
    out: dict[str, list[float]] = {}
    with sqlite3.connect(settings.sqlite_path) as conn:
        placeholders = ",".join("?" * len(tickers))
        cur = conn.execute(
            f"""
            SELECT ticker, observed_at, close
            FROM prices
            WHERE ticker IN ({placeholders})
              AND timeframe = '1D'
              AND observed_at >= ?
            ORDER BY ticker, observed_at ASC
            """,
            (*tickers, cutoff),
        )
        for ticker, _observed_at, close in cur.fetchall():
            if close is None:
                continue
            out.setdefault(ticker, []).append(round(float(close), 6))
    # A 2-point line is noise, not a chart.
    return {t: v for t, v in out.items() if len(v) >= 10}


def build_active_trades_section() -> list[dict]:
    """Trades user has open.

    Schema (per data.mock.js activeTrades[]):
      id, asset, side, entry, stop, target, sizeUsd, ageDays,
      pnlPct, pnlUsd, regimeAtOpen, scoreAtOpen, scoreNow, status
    """
    # TODO: SELECT * FROM trades WHERE status='active'
    return []


def _asset_signals(conn: sqlite3.Connection, ticker: str, limit: int = 8) -> list[dict]:
    """The tracked-voice calls behind one ticker — what the asset page
    means by "where it sits in the signals".

    Each row carries the call as drawn (side, conviction, entry/stop/
    target), who made it, and the chart image it was extracted from so
    the operator can check the agent's levels against the human's.
    """
    try:
        rows = conn.execute(
            """
            SELECT s.signal_id, s.side, s.conviction, s.horizon,
                   s.entry_zone_low, s.entry_zone_high, s.stop_loss,
                   s.target_1, s.thesis_summary, s.source_channel,
                   s.author_id, s.extracted_at,
                   d.attachment_path, d.attachment_paths_json, d.url,
                   d.published_at
            FROM signals s
            LEFT JOIN documents d ON d.document_id = s.document_id
            WHERE s.asset_ticker = ?
              AND s.status = 'active'
            ORDER BY datetime(COALESCE(d.published_at, s.extracted_at)) DESC
            LIMIT ?
            """,
            (ticker, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # signals table not present on a pre-migration DB

    out: list[dict] = []
    for r in rows:
        chart = r[12]
        if not chart and r[13]:
            try:
                paths = json.loads(r[13]) or []
                chart = paths[0] if paths else None
            except Exception:
                chart = None
        out.append({
            "signalId": r[0],
            "side": r[1],
            "conviction": round(float(r[2]), 2) if r[2] is not None else None,
            "horizon": r[3],
            "entryLow": r[4],
            "entryHigh": r[5],
            "stop": r[6],
            "target": r[7],
            "thesis": (str(r[8])[:220] if r[8] else None),
            "channel": r[9],
            "authorId": r[10],
            # Served by the /uploads mount in api/main.py.
            "chartUrl": (f"/{chart}" if chart else None),
            "url": r[14],
            "at": (r[15] or r[11] or "")[:16].replace("T", " "),
        })
    return out


def build_reasoning_section() -> dict:
    """Per-score reasoning trail keyed by score_id (matches heroSignals.id).

    Schema (per data.mock.js reasoning[id]):
      total, tier, components[{label,score,max,color}],
      modifiers[{label,value}], sources[{name,weight,freshness,contrib,tags}],
      theses[{theme,direction,confidence}],
      agentBreakdown[{agent,model,latencyMs,costUsd,ok}],
      signalTimeline[{dates,blend,coverage,n_signals,windows}]
    """
    rows = _load_latest_scores()
    out: dict[str, dict] = {}
    # One timeline per ticker even if a ticker is scored twice this
    # pass — the replay reads history, not the current score row.
    from macro_positioning.signals.aggregation import (
        build_signal_timeline_for_ticker,
    )
    timeline_cache: dict[str, dict] = {}
    def _timeline_for(ticker: str) -> dict:
        if ticker not in timeline_cache:
            try:
                timeline_cache[ticker] = build_signal_timeline_for_ticker(
                    ticker, days=60
                )
            except Exception:
                timeline_cache[ticker] = {}
        return timeline_cache[ticker]

    def _flat_components(rows: list[dict], specs: list[tuple]) -> dict[str, int | None]:
        """Components that returned the same value for every asset in this
        pass — they contribute a constant offset and rank nothing.

        Computed from the data rather than hardcoded, so a component stops
        being flagged the moment it starts discriminating (risk/reward did
        exactly that once the technical agent began emitting levels).
        """
        flat: dict[str, int | None] = {}
        if len(rows) < 3:
            return flat  # too few assets for "no spread" to mean anything
        for _label, key, _max_v in specs:
            values = {r.get(key) or 0 for r in rows}
            if len(values) == 1:
                flat[key] = values.pop()
        return flat

    component_specs = [
        ("Macro alignment", "macro", 15),
        ("Liquidity", "liquidity", 15),
        ("Sector strength", "sector_theme", 5),
        ("Technical structure", "tech", 20),
        ("Volume confirm", "vol", 15),
        ("Risk / Reward", "rr", 10),
        ("Signal conviction", "signal", 15),
        ("Relative strength", "rs", 3),
        ("Psychology · clean", "psych", 2),
    ]

    flat_map = _flat_components(rows, component_specs)

    for r in rows:
        comps = []
        for label, key, max_v in component_specs:
            score = r.get(key) or 0  # coerce None (pre-signal_alignment rows) → 0
            color = "green" if (score / max_v) >= 0.7 else "amber" if (score / max_v) >= 0.5 else "red"
            comp = {"label": label, "score": score, "max": max_v, "color": color}
            if key in flat_map:
                comp["flat"] = True
                comp["flatNote"] = (
                    f"same {flat_map[key]}/{max_v} for every asset this pass — "
                    "a constant offset, not a ranking input"
                )
            comps.append(comp)

        modifiers = []
        trail = r.get("trail", {}) or {}
        if trail.get("regime_modifier") is not None:
            modifiers.append({
                "label": f"Regime fit · {trail.get('active_framework_regime', '—')}",
                "value": f"{trail['regime_modifier']:+d}",
            })
        for adj in trail.get("conservative_adjustments_applied", []) or []:
            modifiers.append({"label": adj.replace("_", " "), "value": "applied"})

        origins = trail.get("watchlist_origins", []) or []
        sources_pretty = [
            {
                "name": o,
                "weight": 1.0 if o == "anchor" else 0.7,
                "freshness": "fresh",
                "contrib": 0,
                "tags": [],
            }
            for o in _origins_to_pretty(origins)
        ]

        # Signal-alignment multi-window strip. Emitted only when the
        # scored aggregate carried the multi-window shape (new scoring
        # passes); older rows without the matrix render nothing.
        sig_agg = r.get("signal_aggregate") or {}
        windows_raw = sig_agg.get("windows") or {}
        blend = sig_agg.get("blend") or {}
        cross = sig_agg.get("cross_window") or {}
        signal_windows = None
        if windows_raw:
            # UI expects an ordered list — sort by the DEFAULT_WINDOWS
            # order so tactical → thesis reads left→right.
            from macro_positioning.signals.aggregation import DEFAULT_WINDOWS
            order = [w[0] for w in DEFAULT_WINDOWS]
            diverging = set(cross.get("diverging_windows") or [])
            aligned = set(cross.get("aligned_windows") or [])
            rows_out = []
            for label in order:
                if label not in windows_raw:
                    continue
                w = windows_raw[label]
                rows_out.append({
                    "label": label,
                    "direction": w.get("bias_direction") or "neutral",
                    "confidence": w.get("bias_confidence") or 0.0,
                    "nSignals": w.get("n_signals") or 0,
                    "netBias": w.get("net_bias") or 0.0,
                    "alignment": (
                        "diverging" if label in diverging
                        else "aligned" if label in aligned
                        else "neutral"
                    ),
                })
            signal_windows = {
                "rows": rows_out,
                "blend": {
                    "direction": blend.get("bias_direction") or "neutral",
                    "confidence": blend.get("bias_confidence") or 0.0,
                    "alignmentScore": blend.get("alignment_score") or 0,
                    "coverage": blend.get("coverage") or 0.0,
                },
                "trend": cross.get("trend") or "mixed",
                "recentFlip": bool(cross.get("recent_flip")),
                "shortBloc": cross.get("short_bloc") or {},
                "longBloc": cross.get("long_bloc") or {},
            }

        entry = {
            "total": r["score"],
            "tier": _tier_str(r["tier"]),
            "components": comps,
            "modifiers": modifiers,
            "sources": sources_pretty,
            "theses": [],
            "agentBreakdown": [
                {"agent": "regime_classifier", "model": "stub@v0", "latencyMs": 0, "costUsd": 0.0, "ok": True},
                {"agent": "psychology_evaluator", "model": "heuristic", "latencyMs": 0, "costUsd": 0.0, "ok": True},
                {"agent": "score_composer", "model": "heuristic+stubs", "latencyMs": 0, "costUsd": 0.0, "ok": True},
            ],
        }
        if signal_windows is not None:
            entry["signalWindows"] = signal_windows
        # The tracked-voice calls behind this ticker, with their charts.
        try:
            with sqlite3.connect(settings.sqlite_path) as _sconn:
                asset_signals = _asset_signals(_sconn, r["ticker"])
        except Exception:
            asset_signals = []
        if asset_signals:
            entry["assetSignals"] = asset_signals
        # Technical agent provenance for the reasoning trail: which
        # detector fired, whether it was structural, and its notes.
        lv = r.get("levels") or None
        if lv:
            entry["levels"] = {
                "side": lv.get("side"),
                "entry": lv.get("entry"),
                "stop": lv.get("stop"),
                "target": lv.get("target"),
                "rr": lv.get("rr"),
                "riskPct": lv.get("riskPct"),
                "method": lv.get("method"),
                "setup": lv.get("setup"),
                "structural": bool(lv.get("structural")),
                "version": lv.get("version"),
                "notes": lv.get("notes") or [],
                "frameworkSetup": lv.get("frameworkSetup"),
                "provenance": lv.get("provenance") or [],
                "rejected": lv.get("rejected") or [],
            }
        elif r.get("levels_reason"):
            entry["levels"] = {"reason": r["levels_reason"]}
        # 60-day historical replay of the blend + per-window conviction,
        # for the "sentiment over time" chart on the asset page. Sentiment
        # is deliberately not price-joined — the chart says what tracked
        # voices have been saying, whether price agrees or not.
        tl = _timeline_for(r["ticker"]) if r.get("ticker") else {}
        if tl.get("dates"):
            entry["signalTimeline"] = tl
        out[r["score_id"]] = entry
    return out


def build_closed_trades_section() -> list[dict]:
    """Closed trades for /journal.

    Schema (per data.mock.js closedTrades[]):
      id, asset, side, entry, exit, pnlPct, holdDays, scoreEntry,
      regimeEntry, thesis, planClean, lesson
    """
    return []


def build_missed_trades_section() -> list[dict]:
    """Missed trades log.

    Schema (per data.mock.js missedTrades[]):
      asset, scoreAtTime, reason, validReal, hindsightRisk, lesson
    """
    return []


def build_process_scorecard_section() -> dict:
    """Process discipline scorecard — repurposed as the Trading Rule
    Framework v1 success-metrics surface (J1 on /journal).

    Schema (unchanged from before — the SPA renders any 6 metrics):
      days: int
      score: int          # 0..100 overall (mean of per-metric scores)
      metrics: [{label, value, of}]

    Six metrics, all 30d window, all on the 0..100 scale:
      1. Rule adherence — mean of trades.rule_adherence_score
      2. Confluence-tier hit rate — win-rate for confluence>=6 trades
      3. Risk-per-trade compliance — % of trades with risk under their tier cap
      4. Portfolio cap compliance — % of trade-open moments under all caps
         (defaults to 100 until the snapshot writer lands; v2)
      5. Setup-category diversification — 100*(1-HHI), higher = more diverse
      6. Plan→outcome fidelity — 100*(1-mean entry+stop relative error)

    Trades without the required inputs (no plan, no confluence_score,
    etc) are excluded from the metrics they can't contribute to, so
    a partial book still yields a meaningful score. Overall `score` is
    the unweighted mean of the six metric values.
    """
    metrics: list[dict] = []
    if not settings.sqlite_path.exists():
        return _scorecard_zero_state()

    try:
        from macro_positioning.rules import load_caps
        caps = load_caps()
    except Exception:
        return _scorecard_zero_state()

    horizon_days = 30
    cutoff = (datetime.now(timezone.utc) - timedelta(days=horizon_days)).isoformat()

    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT trade_id, asset_id, exit_date, pnl_percent,
                   confluence_score, account_risk_pct, setup_category,
                   entry_price, stop_loss, position_size,
                   rule_adherence_score
            FROM trades
            WHERE status = 'closed'
              AND (exit_date IS NULL OR exit_date >= ?)
            """,
            (cutoff,),
        ).fetchall()
        trades = [dict(r) for r in rows]

        # 1. Rule adherence — mean of recorded scores
        adherence_vals = [t["rule_adherence_score"] for t in trades if t["rule_adherence_score"] is not None]
        adherence_value = round(sum(adherence_vals) / len(adherence_vals)) if adherence_vals else 0
        metrics.append({"label": "Rule adherence (30d mean)", "value": adherence_value, "of": 100})

        # 2. Confluence-tier hit rate — win rate among high-confluence trades
        hi_conf = [t for t in trades if t["confluence_score"] is not None and t["confluence_score"] >= 6 and t["pnl_percent"] is not None]
        if hi_conf:
            wins = sum(1 for t in hi_conf if t["pnl_percent"] > 0)
            hit_rate = round(100 * wins / len(hi_conf))
        else:
            hit_rate = 0
        metrics.append({"label": "High-confluence hit rate", "value": hit_rate, "of": 100})

        # 3. Risk-per-trade compliance
        std_cap = caps["trade_level"]["max_account_risk_per_trade_pct"]
        hc_cap = caps["trade_level"]["high_conviction_account_risk_per_trade_pct"]
        risk_population = [t for t in trades if t["account_risk_pct"] is not None]
        if risk_population:
            within = 0
            for t in risk_population:
                cap = hc_cap if (t["confluence_score"] is not None and t["confluence_score"] >= 7) else std_cap
                if t["account_risk_pct"] <= cap:
                    within += 1
            risk_pct_value = round(100 * within / len(risk_population))
        else:
            risk_pct_value = 0
        metrics.append({"label": "Risk-per-trade compliance", "value": risk_pct_value, "of": 100})

        # 4. Portfolio cap compliance — no snapshot writer in v1; check
        # the latest snapshot if any exist, else default optimistic.
        cap_value = _portfolio_cap_compliance(conn)
        metrics.append({"label": "Portfolio cap compliance", "value": cap_value, "of": 100})

        # 5. Setup-category diversification — 100*(1-HHI)
        cats = [t["setup_category"] for t in trades if t["setup_category"]]
        if cats:
            from collections import Counter
            counts = Counter(cats)
            total = sum(counts.values())
            hhi = sum((n / total) ** 2 for n in counts.values())
            diversity_value = round(100 * (1 - hhi))
        else:
            diversity_value = 0
        metrics.append({"label": "Setup-category diversification", "value": diversity_value, "of": 100})

        # 6. Plan→outcome fidelity — entry+stop relative error vs plan
        fidelity_rows = conn.execute(
            """
            SELECT t.entry_price, t.stop_loss,
                   p.planned_entry, p.planned_stop
            FROM trade_plans p
            JOIN trades t ON t.trade_id = p.trade_id
            WHERE t.status = 'closed'
              AND (t.exit_date IS NULL OR t.exit_date >= ?)
              AND p.planned_entry > 0 AND p.planned_stop > 0
            """,
            (cutoff,),
        ).fetchall()
        if fidelity_rows:
            errors: list[float] = []
            for r in fidelity_rows:
                if r["entry_price"] is not None:
                    errors.append(abs(r["entry_price"] - r["planned_entry"]) / r["planned_entry"])
                if r["stop_loss"] is not None:
                    errors.append(abs(r["stop_loss"] - r["planned_stop"]) / r["planned_stop"])
            mean_err = sum(errors) / len(errors) if errors else 0.0
            fidelity_value = max(0, min(100, round(100 * (1 - mean_err))))
        else:
            fidelity_value = 0
        metrics.append({"label": "Plan→outcome fidelity", "value": fidelity_value, "of": 100})

    overall = round(sum(m["value"] for m in metrics) / len(metrics)) if metrics else 0
    return {"days": horizon_days, "score": overall, "metrics": metrics}


def _scorecard_zero_state() -> dict:
    return {
        "days": 30,
        "score": 0,
        "metrics": [
            {"label": "Rule adherence (30d mean)", "value": 0, "of": 100},
            {"label": "High-confluence hit rate", "value": 0, "of": 100},
            {"label": "Risk-per-trade compliance", "value": 0, "of": 100},
            {"label": "Portfolio cap compliance", "value": 0, "of": 100},
            {"label": "Setup-category diversification", "value": 0, "of": 100},
            {"label": "Plan→outcome fidelity", "value": 0, "of": 100},
        ],
    }


def _portfolio_cap_compliance(conn: sqlite3.Connection) -> int:
    """% of recorded exposure snapshots that did NOT breach any cap.

    No writer in v1, so the snapshots table is typically empty —
    default to 100 ("no breaches recorded") so the panel renders
    without the field looking broken. Once a snapshot tick lands,
    this becomes an honest measurement.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN any_cap_breached = 0 THEN 1 ELSE 0 END) AS clean FROM portfolio_exposure_snapshots"
    ).fetchone()
    if row is None or not row["total"]:
        return 100  # default optimistic
    return round(100 * (row["clean"] or 0) / row["total"])


def build_source_leaderboard_section() -> list[dict]:
    """Source attribution leaderboard for /journal.

    Dual-lens fed from learning/source_attribution.py:
      - closed-trade P&L attribution (attribution_30d) → real attribUsd / trades
      - per-mention forward-return tracking (signal_attribution) → weight from
        30d hit_rate when no closed trades yet (so the panel fills before the
        first close)

    Schema (per data.mock.js sourceLeaderboard[]):
      name, weight, dWeight, attribUsd, trades, tags
    """
    if not settings.sqlite_path.exists():
        return []

    try:
        from macro_positioning.learning.source_attribution import (
            attribution_30d,
            signal_attribution,
        )
    except Exception:
        return []

    with sqlite3.connect(settings.sqlite_path) as conn:
        try:
            closed = attribution_30d(conn)
            signals = signal_attribution(conn)
        except Exception:
            return []

        sources = {s.source_id: s for s in load_sources(include_archived=False)}

    def _name(source_id: str) -> str:
        s = sources.get(source_id)
        return s.name if s else source_id

    def _tags(source_id: str, fallback: list[str]) -> list[str]:
        s = sources.get(source_id)
        if s and s.routing_tags:
            return list(s.routing_tags)
        return fallback or []

    by_source: dict[str, dict] = {}

    for row in signals:
        sid = row["source_id"]
        h30 = row.get("horizons", {}).get(30) or {}
        h7 = row.get("horizons", {}).get(7) or {}
        # Prefer 30d hit_rate; fall back to 7d when 30d has no price data yet.
        hr = h30.get("hit_rate") if h30.get("n_with_price_data") else h7.get("hit_rate") or 0.0
        weight = max(0.0, min(1.0, float(hr or 0.0)))
        by_source[sid] = {
            "name": _name(sid),
            "weight": round(weight, 2),
            "dWeight": 0.0,
            "attribUsd": 0,
            "trades": 0,
            "tags": _tags(sid, row.get("verticals", [])),
        }

    for row in closed:
        sid = row["source_id"]
        entry = by_source.setdefault(
            sid,
            {
                "name": _name(sid),
                "weight": 0.0,
                "dWeight": 0.0,
                "attribUsd": 0,
                "trades": 0,
                "tags": _tags(sid, []),
            },
        )
        # source_outcomes rows expose pnl % only (no per-trade notional yet);
        # surface weighted_pnl_pct in basis-points-ish units so the column has
        # signed magnitude. Replace with real USD once outcome notional lands.
        entry["attribUsd"] = int(round(float(row.get("weighted_pnl_pct", 0) or 0) * 100))
        entry["trades"] = int(row.get("n_outcomes", 0) or 0)
        # Closed-trade hit rate trumps signal-derived weight when available:
        # positive weighted_pnl_pct → 0.5..1, negative → 0..0.5.
        wpp = float(row.get("weighted_pnl_pct", 0) or 0)
        entry["weight"] = round(max(0.0, min(1.0, 0.5 + wpp / 200.0)), 2)

    rows = list(by_source.values())
    rows.sort(key=lambda r: (r["attribUsd"], r["weight"]), reverse=True)
    return rows[:20]


def build_score_correlation_section() -> dict:
    """Spearman ρ between score components and realized PnL.

    Fed by learning/score_outcome_correlation.py. Empty until closed
    trades exist; shape preserved so the SPA can render zero-state.

    Schema (per data.mock.js scoreCorrelation):
      n_pairs: int
      components: list of {
        name: str, spearman: float|null, p_value: float|null, n: int
      }
    """
    if not settings.sqlite_path.exists():
        return {"n_pairs": 0, "components": []}

    try:
        from macro_positioning.learning.score_outcome_correlation import (
            score_outcome_correlation,
        )
    except Exception:
        return {"n_pairs": 0, "components": []}

    with sqlite3.connect(settings.sqlite_path) as conn:
        try:
            raw = score_outcome_correlation(conn)
        except Exception:
            return {"n_pairs": 0, "components": []}

    component_keys = [
        "adjusted_total",
        "macro_alignment",
        "liquidity",
        "sector_theme",
        "technical_structure",
        "volume_flow",
        "risk_reward",
        "relative_strength",
        "psychology",
    ]
    components = []
    for key in component_keys:
        c = raw.get(key) or {}
        components.append({
            "name": key,
            "spearman": c.get("spearman"),
            "p_value": c.get("p_value"),
            "n": int(c.get("n", 0) or 0),
        })

    return {
        "n_pairs": int(raw.get("n_pairs", 0) or 0),
        "components": components,
    }


def build_thesis_changelog_section() -> list[dict]:
    """Thesis revision log.

    Schema (per data.mock.js thesisChangelog[]):
      date, from, to, title, summary, regimes[]
    """
    return [
        {
            "date": "2026-04-22",
            "from": "v2.4",
            "to": "v3.0",
            "title": "Capital Rotation Through Hyper-Liquidity Cycle",
            "summary": (
                "Reframed as two-speed market. Energy + AI explicitly linked. "
                "Cash treated as strategic, not residual."
            ),
            "regimes": [
                "+commodity_expansion",
                "+monetary_debasement_hard_asset",
            ],
        },
    ]


def build_brain_activity_section() -> list[dict]:
    """Recent brain calls.

    Schema (per data.mock.js brainActivity[]):
      ts (HH:MM:SS), agent, model, latencyMs, tokensIn, tokensOut, costUsd, ok
    """
    try:
        from macro_positioning.brain.observability import recent_calls
        calls = recent_calls(limit=10)
    except Exception:
        return []

    out = []
    for c in calls:
        ts_iso = getattr(c, "timestamp", None)
        ts_short = "—"
        if ts_iso:
            try:
                if hasattr(ts_iso, "strftime"):
                    ts_short = ts_iso.strftime("%H:%M:%S")
                else:
                    ts_short = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00")).strftime("%H:%M:%S")
            except Exception:
                ts_short = str(ts_iso)[:8]

        out.append({
            "ts": ts_short,
            "agent": getattr(c, "call_type", "unknown"),
            "model": getattr(c, "model", None) or getattr(c, "backend", "—"),
            "latencyMs": int(getattr(c, "latency_ms", 0) or 0),
            "tokensIn": getattr(c, "input_tokens", 0) or 0,
            "tokensOut": getattr(c, "output_tokens", 0) or 0,
            "costUsd": float(getattr(c, "estimated_cost_usd", 0.0) or 0.0),
            "ok": bool(getattr(c, "success", False)),
        })
    return out


def build_source_health_section() -> list[dict]:
    """Per-source freshness + last fetch + current weight.

    Schema (per data.mock.js sourceHealth[]):
      name, kind, lastFetch (HH:MM), freshness (0..1), weight, attrib30d, tags
      + detail fields: source_id, author, market_focus, research_style,
        fetch_cadence, freshness_sla_hours, channels, onboarded_at
    """
    sources = load_sources(include_archived=False)

    # Wire real 30d attribution where closed-trade outcomes exist
    attrib_map: dict[str, float] = {}
    if settings.sqlite_path.exists():
        try:
            from macro_positioning.learning.source_attribution import attribution_30d
            with sqlite3.connect(settings.sqlite_path) as conn:
                attrib_map = {
                    r["source_id"]: r["weighted_pnl_pct"]
                    for r in attribution_30d(conn)
                }
        except Exception:
            pass

    # Map source_id → display name so member/group references render nicely.
    name_by_id = {s.source_id: s.name for s in sources}

    rows = []
    for s in sources:
        # TODO: pull last_fetched from real fetch-tracking tables
        sla = s.freshness_sla_hours or 0
        score = 1.0 if sla else 1.0  # placeholder until fetch tracking
        # Operator-assigned conviction tier (T0..T4) or infra/self bucket.
        tier = getattr(s, "tier", None) or ""
        # Group structure: a group source lists its member source_ids
        # (group_members); a member lists its parent group(s)
        # (group_membership). Both preserved from sources.json via the
        # extra="allow" SourceRecord model. Emit for tier-then-group nesting.
        group_members = list(getattr(s, "group_members", None) or [])
        group_membership = list(getattr(s, "group_membership", None) or [])
        # Serialize channel objects to plain dicts for JSON.
        channels = []
        for c in (s.channels or []):
            if hasattr(c, "model_dump"):
                channels.append(c.model_dump())
            elif isinstance(c, dict):
                channels.append(c)
            else:
                channels.append({"channel_type": str(c)})
        rows.append({
            # Core health fields
            "name": s.name,
            "kind": _SOURCE_TYPE_DISPLAY.get(s.source_type, s.source_type),
            "tier": tier,
            "lastFetch": "—",
            "freshness": round(score, 2),
            "weight": s.trust_weight,
            "attrib30d": round(attrib_map.get(s.source_id, 0), 2),
            "tags": s.routing_tags[:5],
            # Detail fields (used by SourceDetailPanel drilldown)
            "source_id": s.source_id,
            "author": s.author or "",
            "market_focus": s.market_focus or "",
            "research_style": s.research_style or "",
            "fetch_cadence": s.fetch_cadence or "",
            "freshness_sla_hours": s.freshness_sla_hours,
            "channels": channels,
            "onboarded_at": s.onboarded_at or "",
            # Group nesting
            "is_group": bool(group_members),
            "group_members": group_members,
            "group_membership": group_membership,
            "group_member_names": [name_by_id.get(m, m) for m in group_members],
        })
    # Sort by conviction tier (T0 first), then by weight desc within tier.
    _tier_order = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4, "infra": 5, "self": 6, "": 9}
    rows.sort(key=lambda r: (_tier_order.get(r["tier"], 9), -float(r["weight"] or 0)))
    return rows


def build_cost_tracker_section() -> dict:
    """Aggregated LLM spend.

    Schema (per data.mock.js costTracker):
      today, week, month, capDaily, capMonthly,
      byAgent[{agent, usd}], byBackend[{backend, usd}], spike (bool)
    """
    try:
        from macro_positioning.brain.observability import call_stats
        stats = call_stats()
    except Exception:
        stats = {}
    today = float(stats.get("cost_usd_today", 0.0) or 0.0)
    week = float(stats.get("cost_usd_week", 0.0) or 0.0)
    month = float(stats.get("cost_usd_month", 0.0) or 0.0)
    return {
        "today": round(today, 2),
        "week": round(week, 2),
        "month": round(month, 2),
        "capDaily": 25.0,
        "capMonthly": 600.0,
        "byAgent": [],
        "byBackend": [],
        "spike": today > 25.0,
    }


def build_mgmt_section() -> dict:
    """Project mgmt: todos + decisions + commits.

    Schema (per data.mock.js mgmt):
      todos: [{status, title, owner, age}]
      decisions: [{date, title, who}]
      commits: [{hash, author, msg}]

    `status` in todos: "in_flight" | "todo" | "done" | "blocked"
    """
    cl = load_checklist()
    status_map = {"in_progress": "in_flight", "todo": "todo", "done": "done"}
    items = list(cl.items)
    order = {"in_progress": 0, "todo": 1, "done": 2}
    items.sort(key=lambda i: (order.get(i.status, 9), -1 if i.priority == "critical" else 0))

    todos = []
    for it in items[:10]:
        todos.append({
            "status": status_map.get(it.status, it.status),
            "title": it.title,
            "owner": "Application Agent",
            "age": "—",
        })

    decisions = []
    for d in sorted(_load_decisions(), key=lambda x: x.decided_at, reverse=True)[:6]:
        decisions.append({
            "date": d.decided_at.split("T")[0],
            "title": d.topic,
            "who": "Operator + Application Agent",
        })

    commits = []
    for c in _git_recent_commits(limit=8):
        commits.append({
            "hash": c.short_sha,
            "author": c.author,
            "msg": c.subject,
        })

    return {"todos": todos, "decisions": decisions, "commits": commits}


def build_streams_section_wrapper() -> dict | None:
    """SPA /streams payload — opens its own short-lived connection.

    Returns None when no live data is available, so the snapshot omits
    the `streams` key entirely and the data.mock.js fallback survives
    the shallow Object.assign in web/data.mock.js.
    """
    from macro_positioning.dashboard.streams_builders import build_streams_section
    if not settings.sqlite_path.exists():
        return None
    with sqlite3.connect(settings.sqlite_path) as conn:
        payload = build_streams_section(conn)
    if (not payload["themeMap"] and not payload.get("assetMap")
            and not payload["concepts"] and not payload["sourceGraph"]["nodes"]):
        return None
    return payload


def build_integration_section() -> dict:
    """Tactical-executor integration status.

    Schema (per data.mock.js integration):
      tactical: {connected, lastPoll, contractVersion, schemaDrift, mode}
    """
    try:
        from macro_positioning.integration import tactical_client
        snap = tactical_client.fetch_tactical_snapshot()
    except Exception:
        snap = {}
    return {
        "tactical": {
            "connected": bool(snap.get("connected", False)),
            "lastPoll": snap.get("last_poll", "—"),
            "contractVersion": snap.get("contract_version", "v3"),
            "schemaDrift": bool(snap.get("schema_drift", False)),
            "mode": "manual",  # per D-2026-05-08-009
        }
    }


# ---------------------------------------------------------------------------
# Top-level snapshot
# ---------------------------------------------------------------------------

def build_live_signals_section() -> list[dict]:
    """Top 8 freshest signals from the signals table.

    Reads directly from the signals layer — this is what makes
    extraction output visible on the dashboard BEFORE a scoring pass
    rolls it into trade_scores. Order: weighted_score DESC, falling
    back to extracted_at DESC. Time window: last 72h.

    Each row carries enough for the SPA panel: ticker, side, conviction,
    weighted score, author, source channel, extracted_at, and a thesis
    summary if the extractor produced one.
    """
    if not settings.sqlite_path.exists():
        return []
    try:
        with sqlite3.connect(settings.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            # Hero signals only surface authors the user has explicitly
            # stated — the seeded allowlist (SEEDED_AUTHOR_WHERE). Pull a wide
            # pool (not 8) so the real-ticker filter below still yields 8 after
            # dropping memecoin "first call" spam.
            rows = conn.execute(
                f"""
                SELECT
                    s.signal_id, s.asset_ticker, s.side, s.conviction, s.weighted_score,
                    s.source_slug, s.source_channel, s.author_id,
                    s.horizon, s.catalyst_type, s.thesis_summary,
                    s.extractor_name, s.extractor_confidence,
                    s.stop_loss, s.target_1, s.target_2,
                    s.extracted_at, s.status,
                    -- when the call was MADE, not when we read it: the
                    -- extraction clock lags the drop by hours to days.
                    COALESCE(d.published_at, d.ingested_at) AS published_at
                FROM signals s
                LEFT JOIN documents d ON d.document_id = s.document_id
                WHERE s.status = 'active'
                  AND datetime(s.extracted_at) >= datetime('now', '-72 hours')
                  AND s.author_id IN (
                      SELECT author_id FROM input_authors WHERE {SEEDED_AUTHOR_WHERE}
                  )
                ORDER BY s.weighted_score DESC NULLS LAST, s.extracted_at DESC
                LIMIT 120
                """
            ).fetchall()
            # Real-ticker filter: keep priced names (equities + majors) or
            # coins with a Coinbase book; drop trenching/memecoin junk (TOBY,
            # UNEE, CULT, NYAN…). "Live signals" is the core feed, not a
            # memecoin scanner. Resolving first is what makes the crypto half
            # work at all: these tickers arrive as PAIRS (BTC/USDT, KINS/SOL),
            # and a raw pair never matched a coin set.
            from macro_positioning.prices.symbol_map import is_crypto, resolve_symbol
            _priced = {row[0] for row in conn.execute("SELECT DISTINCT ticker FROM prices")}
    except sqlite3.OperationalError:
        # signals table not yet created (pre-migration DB)
        return []

    def _is_real(t: str) -> bool:
        key = resolve_symbol(t or "")
        if not key:
            return False
        # An equity still has to have bars — `resolve_symbol` lets any
        # plausible 1–5 letter string through, which is how junk like TOBY
        # gets this far. A coin only has to be tradeable.
        return key in _priced or is_crypto(key)

    rows = [r for r in rows if _is_real(r["asset_ticker"])][:8]
    out: list[dict] = []
    for r in rows:
        out.append({
            "signal_id": r["signal_id"],
            "ticker": r["asset_ticker"],
            "side": r["side"],
            "conviction": float(r["conviction"]) if r["conviction"] is not None else None,
            "weighted_score": (
                round(float(r["weighted_score"]), 3)
                if r["weighted_score"] is not None else None
            ),
            "source_slug": r["source_slug"],
            "source_channel": r["source_channel"],
            "author_id": r["author_id"],
            "horizon": r["horizon"],
            "catalyst_type": r["catalyst_type"],
            "thesis_summary": (
                str(r["thesis_summary"])[:160]
                if r["thesis_summary"] else None
            ),
            "extractor_name": r["extractor_name"],
            "extractor_confidence": (
                float(r["extractor_confidence"])
                if r["extractor_confidence"] is not None else None
            ),
            "stop_loss": r["stop_loss"],
            "target_1": r["target_1"],
            "target_2": r["target_2"],
            "extracted_at": r["extracted_at"],
            "published_at": r["published_at"],
        })
    return out


# ── Concept suggestions (funnel step ② intake) ────────────────────────────
#
# What earns a place in "suggested by system": not a high score on its own,
# but a name the desk could act on today. Two streams feed it.
#
#   scored — the top of the live watchlist, filtered down to setups with
#            real levels behind them (a stop off structure, or a target a
#            trusted voice actually drew) rather than the 2×ATR/3R
#            placeholders the scorer fills in while structure forms.
#   voice  — a trusted author making a levelled call on a ticker the
#            watchlist does not score at all. Without this stream those
#            names are invisible here: they never enter the scoring set,
#            so no score threshold can ever surface them.
#
# Anything already being worked — an active concept, a live or draft plan,
# an open trade — is excluded; suggesting it again is noise.

SUGGEST_SCORE_FLOOR = 70          # tier-1/2 territory on the 0-100 composite
SUGGEST_MAX_TIER = 2
SUGGEST_MIN_RR = 2.0
SUGGEST_VOICE_MIN_CONVICTION = 3.5
SUGGEST_VOICE_MAX_AGE_DAYS = 21
# A lower R bar than the scored stream on purpose, and measured from
# today's price rather than the author's entry: what matters is the trade
# still available, not the one they got. Chart calls are also staged —
# the stored targets are waypoints, not the thesis's ceiling — so the
# ratio here understates more often than it flatters.
SUGGEST_VOICE_MIN_RR = 1.0
# A call is only actionable while price is still near where it was made.
# Past this, the levels are a chart annotation from a move that already
# happened (CCJ called at 15.47 while it trades at 106.96).
SUGGEST_VOICE_MAX_DRIFT = 0.15
# Stops closer than this to entry are extraction artifacts, not risk
# definitions, and they manufacture enormous R ratios.
SUGGEST_VOICE_MIN_RISK_PCT = 0.02
SUGGEST_VOICE_MAX_RISK_PCT = 0.35
# How much of the call's original risk band price may have eaten and the
# idea still be worth taking. Past half, the trade is mostly a loss in
# progress — and measuring R from a price sitting just above the stop
# manufactures huge ratios out of nearly-invalidated calls.
SUGGEST_VOICE_MAX_RISK_CONSUMED = 0.5
# R is capped for ranking only. An 800-to-1124 target off a stop 2% away
# computes as 25R and would outrank every real setup on the list.
SUGGEST_RANK_RR_CAP = 8.0
SUGGEST_LIMIT = 8
SUGGEST_VOICE_LIMIT = 3


def _latest_prices(conn: sqlite3.Connection) -> dict[str, float]:
    """Most recent close per ticker, for judging whether a call is still live."""
    try:
        rows = conn.execute(
            """
            SELECT ticker, close FROM prices p
            WHERE observed_at = (
                SELECT MAX(observed_at) FROM prices WHERE ticker = p.ticker
            )
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {
        str(t).upper(): float(c)
        for t, c in rows if t and c is not None
    }


def _assets_already_in_flight(conn: sqlite3.Connection) -> set[str]:
    """Tickers with an active concept, an open plan, or an open trade."""
    taken: set[str] = set()
    for sql in (
        "SELECT asset_id FROM trade_concepts WHERE status = 'active'",
        "SELECT asset_id FROM trade_plans WHERE status IN ('draft', 'live')",
        "SELECT asset_id FROM trades WHERE status = 'open'",
    ):
        try:
            taken.update(str(r[0]).upper() for r in conn.execute(sql) if r[0])
        except sqlite3.OperationalError:
            continue
    # trades/plans key by asset_id; map those back to tickers.
    try:
        rows = conn.execute(
            "SELECT asset_id, ticker FROM assets WHERE asset_id IN "
            "(SELECT asset_id FROM trade_plans WHERE status IN ('draft','live') "
            " UNION SELECT asset_id FROM trades WHERE status='open')"
        ).fetchall()
        taken.update(str(t).upper() for _, t in rows if t)
    except sqlite3.OperationalError:
        pass
    return taken


def _level_basis(row: dict) -> tuple[bool, str | None]:
    """Is this setup levelled by something real, and who drew it?

    Mechanical rails alone don't qualify — those are the scorer's
    placeholder until structure forms, and suggesting a trade off them
    would be suggesting arithmetic, not a setup.
    """
    for prov in row.get("levelProvenance") or []:
        if prov.get("source") != "trusted_voices":
            continue
        contributors = prov.get("contributors") or []
        if contributors:
            lead = contributors[0].get("display_name")
            extra = len(contributors) - 1
            if lead:
                return True, (f"{lead} +{extra}" if extra > 0 else lead)
        who = prov.get("who")
        if who:
            # `who` is a rendered summary ("Name (64% setup win over 380
            # calls)") — the name alone is what belongs in a one-line reason.
            return True, who.split(" (")[0].split(",")[0]
    return bool(row.get("levelStructural")), None


def _scored_suggestions(rows: list[dict], taken: set[str]) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        if (r.get("asset") or "").upper() in taken:
            continue
        if r.get("side") not in ("LONG", "SHORT"):
            continue
        if (r.get("score") or 0) < SUGGEST_SCORE_FLOOR:
            continue
        if (r.get("tier") or 99) > SUGGEST_MAX_TIER:
            continue
        if not r.get("hasLevels") or (r.get("rr") or 0) < SUGGEST_MIN_RR:
            continue
        levelled, voice = _level_basis(r)
        if not levelled:
            continue
        basis = (
            f"target drawn by {voice}" if voice
            else "stop off market structure"
        )
        # `regime` is only worth a clause when it's fit/mix. It reads "off"
        # for nearly every name right now — setup types aren't in the
        # regime's PREFERRED_SETUPS, so macro_alignment lands ≤6/15 across
        # the board — and a constant is not information. The penalty is
        # already inside the score this row leads with.
        regime = r.get("regime")
        reason = (
            f"score {r['score']} · T{r.get('tier')} · {r.get('rr'):.2f}R · {basis}"
            + (f" · regime {regime}" if regime in ("fit", "mix") else "")
        )
        out.append({
            "asset": r["asset"],
            "side": r["side"],
            "score": r["score"],
            "dScore": r.get("dScore", 0),
            "tier": r.get("tier"),
            "regime": r.get("regime"),
            "reason": reason,
            "origin": "scored",
            "entry": r.get("entry"),
            "stop": r.get("stop"),
            "target": r.get("target"),
            "rr": r.get("rr"),
            "setup": r.get("setup"),
            "voice": voice,
            "sources": r.get("origins") or [],
            "whyNow": r.get("whyNow") or [],
        })
    out.sort(key=lambda s: (-(s["score"] or 0), -(s["rr"] or 0)))
    return out


def _voice_suggestions(conn: sqlite3.Connection, scored: set[str],
                       taken: set[str], *, now: datetime) -> list[dict]:
    cutoff = (now - timedelta(days=SUGGEST_VOICE_MAX_AGE_DAYS)).isoformat()
    try:
        rows = conn.execute(
            f"""
            SELECT s.asset_ticker, s.side, s.conviction, s.weighted_score,
                   s.thesis_summary, s.entry_zone_low, s.stop_loss,
                   s.target_1, s.target_2, s.source_channel, s.source_slug,
                   s.extracted_at, s.author_trust_weight, s.horizon,
                   ia.display_name, s.asset_class
            FROM signals s
            LEFT JOIN input_authors ia ON ia.author_id = s.author_id
            WHERE s.status = 'active'
              AND s.side IN ('LONG', 'SHORT')
              AND s.conviction >= ?
              AND s.extracted_at >= ?
              AND s.stop_loss IS NOT NULL
              AND s.target_1 IS NOT NULL
              AND s.author_id IN (
                  SELECT author_id FROM input_authors WHERE {SEEDED_AUTHOR_WHERE}
              )
            ORDER BY s.conviction DESC, s.extracted_at DESC
            """,
            (SUGGEST_VOICE_MIN_CONVICTION, cutoff),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        import sys
        print(f"[desk] voice suggestions unavailable: {exc}", file=sys.stderr)
        return []

    # The same real-asset gate the discovery feeds use: one channel's DEX
    # meme pairs would otherwise dominate this stream, since they arrive
    # with maximum conviction and a full set of levels.
    from macro_positioning.dashboard.streams_builders import (
        _crypto_majors,
        _signal_is_real_asset,
    )
    universe = _crypto_majors()
    prices = _latest_prices(conn)

    best: dict[str, dict] = {}
    for r in rows:
        ticker = (r[0] or "").upper()
        if not ticker or ticker in scored or ticker in taken or ticker in best:
            continue
        if not _signal_is_real_asset(
            {"asset_ticker": ticker, "asset_class": r[15],
             "thesis_summary": r[4]}, universe
        ):
            continue
        side, conviction = r[1], r[2]
        entry, stop, t1, t2 = r[5], r[6], r[7], r[8]
        # Furthest target: these calls are staged, and the author is
        # playing for the far one.
        target = max([t for t in (t1, t2) if t is not None], default=None)
        if not (entry and stop and target):
            continue
        risk_pct = abs(entry - stop) / entry if entry else 0
        if not (SUGGEST_VOICE_MIN_RISK_PCT <= risk_pct <= SUGGEST_VOICE_MAX_RISK_PCT):
            continue

        # Everything below asks the same question: is this call still live?
        # Without a price we cannot answer it, and an unverifiable call is
        # not an actionable one (gold called at 1264 would sail through).
        price = prices.get(ticker)
        if not price:
            continue
        if abs(price - entry) / entry > SUGGEST_VOICE_MAX_DRIFT:
            continue
        long_side = side == "LONG"
        if long_side and not (stop < price < target):
            continue          # stop already taken out, or target already hit
        if not long_side and not (target < price < stop):
            continue
        # Fraction of the original entry→stop band price has already eaten.
        # Negative means the call is in profit.
        consumed = (entry - price) / (entry - stop) if entry != stop else 1.0
        if not long_side:
            consumed = (price - entry) / (stop - entry)
        if consumed > SUGGEST_VOICE_MAX_RISK_CONSUMED:
            continue
        rr = round(abs(target - price) / abs(price - stop), 2)
        if rr < SUGGEST_VOICE_MIN_RR:
            continue

        who = r[14] or r[9] or r[10] or "a trusted source"
        best[ticker] = {
            "asset": ticker,
            "side": side,
            # No composite score: this name was never scored — that is the
            # whole reason it needs surfacing. Conviction travels instead.
            "score": None,
            "dScore": 0,
            "tier": None,
            "conviction": conviction,
            "regime": "unscored",
            "reason": (
                f"not scored · {who} at conviction {conviction:.1f}/5 · "
                f"{rr:.2f}R left from {price:g} (stop {stop:g}, target "
                f"{target:g}) · called at {entry:g}"
            ),
            "price": price,
            "origin": "voice",
            "entry": entry,
            "stop": stop,
            "target": target,
            "target2": t2,
            "rr": rr,
            "setup": r[13] or "",
            "voice": who,
            "thesis": r[4],
            "sources": [who],
            "whyNow": [],
        }
    # Conviction first, then how much room the call has — ties at 5/5 are
    # common, and the wider trade is the better use of a slot.
    ranked = sorted(
        best.values(),
        key=lambda s: (
            -(s.get("conviction") or 0),
            -min(s.get("rr") or 0, SUGGEST_RANK_RR_CAP),
        ),
    )
    return ranked[:SUGGEST_VOICE_LIMIT]


def build_concept_suggestions_section(
    watchlist_rows: list[dict] | None = None,
) -> list[dict]:
    """Names the system puts forward as concepts, most actionable first.

    Takes the already-built watchlist rows so a payload build doesn't pay
    for a second full score load.
    """
    if not settings.sqlite_path.exists():
        return []
    rows = watchlist_rows if watchlist_rows is not None else build_watchlist_section()
    now = datetime.now(UTC)
    with sqlite3.connect(settings.sqlite_path) as conn:
        taken = _assets_already_in_flight(conn)
        scored = _scored_suggestions(rows, taken)
        voice = _voice_suggestions(
            conn,
            {(r.get("asset") or "").upper() for r in rows},
            taken,
            now=now,
        )
    # Scored names lead — they cleared the full model. The voice tail keeps
    # its slots rather than being truncated away: an unscored name a trusted
    # voice called is exactly what the score-ranked list can never show, so
    # letting scored names fill the cap first would defeat the stream.
    keep = max(1, SUGGEST_LIMIT - len(voice))
    return scored[:keep] + voice


def build_funnel_concepts_section() -> list[dict] | None:
    """Marked concepts (funnel step ②) from `trade_concepts`.

    Returns None while the table is empty so the mock rows in
    data.mock.js survive the shallow merge — the funnel view keeps its
    demo content until the desk marks something real, then switches to
    live rows. Shape is the SPA's client contract (camelCase), not the
    API's row shape.
    """
    if not settings.sqlite_path.exists():
        return None
    try:
        with sqlite3.connect(settings.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT concept_id, asset_id, source, status,
                       suggested_by_system, suggestion_reason,
                       score_at_mark, tier_at_mark, side_at_mark,
                       thesis_text, trade_plan_id,
                       marked_at, promoted_at, retired_at
                FROM trade_concepts
                ORDER BY marked_at DESC
                LIMIT 200
                """
            ).fetchall()
    except sqlite3.OperationalError:
        return None       # pre-migration DB — keep the mock
    if not rows:
        return None

    def _stamp(v: Optional[str]) -> Optional[str]:
        return str(v).replace("T", " ")[:16] if v else None

    return [{
        "id": r["concept_id"],
        "asset": r["asset_id"],
        "source": r["source"],
        "status": r["status"],
        "suggestedBySystem": bool(r["suggested_by_system"]),
        "suggestionReason": r["suggestion_reason"],
        "scoreAtMark": r["score_at_mark"],
        "tierAtMark": r["tier_at_mark"],
        "sideAtMark": r["side_at_mark"],
        "thesis": r["thesis_text"] or "",
        "tradePlanId": r["trade_plan_id"],
        "markedAt": _stamp(r["marked_at"]),
        "promotedAt": _stamp(r["promoted_at"]),
        "retiredAt": _stamp(r["retired_at"]),
    } for r in rows]


def build_suggestion_reviews_section() -> list[dict]:
    """Suggestions the desk has passed on, with their re-raise bar.

    The policy (how far a score must climb, how long a pass stays good)
    lives in api/funnel.py; this just serves the computed thresholds so
    the SPA can filter without re-deriving them.
    """
    if not settings.sqlite_path.exists():
        return []
    from macro_positioning.api.funnel import _row_to_suggestion_review
    try:
        with sqlite3.connect(settings.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM concept_suggestion_reviews ORDER BY reviewed_at DESC"
            ).fetchall()
    except sqlite3.OperationalError:
        return []         # pre-migration DB
    return [_row_to_suggestion_review(r) for r in rows]


def build_data_health_section() -> dict:
    """Per-source freshness so the operator can spot stalled pipelines.

    Each entry: {label, last_ts, minutes_since, docs_today, status}.
    `status` ∈ green/yellow/red based on:
      - missing → red
      - >24h stale → red
      - 6-24h stale → yellow
      - <6h stale → green

    Covers every input prefix the design promised (manual, gmail, news,
    podcasts, substack, insiders) PLUS the downstream stages (prices,
    fred, trade_scores, signals) so the operator sees the entire
    pipeline at a glance.
    """
    from datetime import UTC, datetime as _dt

    if not settings.sqlite_path.exists():
        return {"sources": [], "as_of": _dt.now(UTC).isoformat()}

    now = _dt.now(UTC)

    def _classify(last_ts: str | None) -> tuple[float | None, str]:
        if not last_ts:
            return None, "red"
        try:
            ts = _dt.fromisoformat(last_ts.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None, "red"
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        minutes = (now - ts).total_seconds() / 60.0
        if minutes > 24 * 60:
            return minutes, "red"
        if minutes > 6 * 60:
            return minutes, "yellow"
        return minutes, "green"

    def _doc_source(*patterns: str) -> dict:
        """Aggregate freshness for any source_id matching one of the
        provided LIKE patterns. Patterns can be exact (`'google_news'`)
        or wildcarded (`'substack:%'`). Multiple patterns OR-ed.
        """
        if not patterns:
            return {"last_ts": None, "minutes_since": None,
                    "docs_today": 0, "status": "red"}
        clauses = " OR ".join("source_id LIKE ?" for _ in patterns)
        with sqlite3.connect(settings.sqlite_path) as conn:
            row = conn.execute(
                f"""
                SELECT MAX(ingested_at) AS last_ts,
                       SUM(CASE WHEN DATE(ingested_at, 'localtime') = DATE('now', 'localtime')
                                THEN 1 ELSE 0 END) AS today
                FROM documents
                WHERE {clauses}
                """,
                tuple(patterns),
            ).fetchone()
        last_ts = row[0] if row else None
        docs_today = int(row[1] or 0) if row else 0
        minutes, status = _classify(last_ts)
        return {
            "last_ts": last_ts, "minutes_since": minutes,
            "docs_today": docs_today, "status": status,
        }

    def _table_source(sql: str) -> dict:
        try:
            with sqlite3.connect(settings.sqlite_path) as conn:
                row = conn.execute(sql).fetchone()
        except sqlite3.OperationalError:
            return {"last_ts": None, "minutes_since": None,
                    "docs_today": 0, "status": "red"}
        last_ts = row[0] if row else None
        docs_today = int(row[1] or 0) if row and len(row) > 1 else 0
        minutes, status = _classify(last_ts)
        return {
            "last_ts": last_ts, "minutes_since": minutes,
            "docs_today": docs_today, "status": status,
        }

    # Gmail-ingested newsletters land under their bare newsletter slug
    # (e.g. 'finimize', 'kaoboy_musings'), NOT a 'gmail:' prefix — so the
    # Gmail tile must match those source_ids explicitly or it stays red even
    # when ingestion is healthy. Source of truth: gmail_connector. Substack
    # versions of the same publication carry a 'substack:' prefix, so the
    # bare slug is unambiguously the Gmail path (no double-counting).
    try:
        from macro_positioning.ingestion.gmail_connector import NEWSLETTER_SOURCES
        _gmail_patterns = ["gmail:%", "personal_gmail:%", "personal_gmail"]
        _gmail_patterns += [s.source_id for s in NEWSLETTER_SOURCES]
    except Exception:
        _gmail_patterns = ["gmail:%", "personal_gmail:%", "personal_gmail"]

    sources = [
        {"key": "manual", "label": "Manual drops",
         **_doc_source("manual:%")},
        {"key": "gmail", "label": "Gmail",
         **_doc_source(*_gmail_patterns)},
        {"key": "news", "label": "Google News",
         **_doc_source("google_news", "google_news:%",
                       "news:%", "googlenews:%")},
        {"key": "podcasts", "label": "Podcasts",
         **_doc_source("podcast:%", "podcast_%",
                       "forward_guidance", "wolf_of_all_streets",
                       "real_vision_journeyman", "moonshots_diamandis")},
        {"key": "substack", "label": "Substack",
         **_doc_source("substack:%")},
        {"key": "insiders", "label": "Insider scrapers",
         **_table_source(
             "SELECT MAX(last_run_at) AS last_ts, COUNT(*) "
             "FROM insiders_cursor "
             "WHERE DATE(last_run_at, 'localtime') = DATE('now', 'localtime')"
         )},
        {"key": "signals", "label": "Signal extraction",
         **_table_source(
             "SELECT MAX(extracted_at) AS last_ts, "
             "       SUM(CASE WHEN DATE(extracted_at, 'localtime') = DATE('now', 'localtime') "
             "                THEN 1 ELSE 0 END) "
             "FROM signals"
         )},
        {"key": "prices", "label": "Prices (yfinance)",
         **_table_source(
             "SELECT MAX(fetched_at) AS last_ts, "
             "       SUM(CASE WHEN DATE(fetched_at, 'localtime') = DATE('now', 'localtime') "
             "                THEN 1 ELSE 0 END) "
             "FROM prices"
         )},
        {"key": "fred", "label": "FRED",
         **_table_source(
             "SELECT MAX(fetched_at) AS last_ts, "
             "       SUM(CASE WHEN DATE(fetched_at, 'localtime') = DATE('now', 'localtime') "
             "                THEN 1 ELSE 0 END) "
             "FROM fred_observations"
         )},
        {"key": "trade_scores", "label": "Scoring pass",
         **_table_source(
             "SELECT MAX(scored_at) AS last_ts, "
             "       SUM(CASE WHEN DATE(scored_at, 'localtime') = DATE('now', 'localtime') "
             "                THEN 1 ELSE 0 END) "
             "FROM trade_scores"
         )},
    ]
    return {
        "sources": sources,
        "as_of": now.isoformat(),
    }


def build_desk_snapshot() -> dict:
    """Assemble the full MA_DATA dict the SPA expects.

    Each section is independent — failure in one shouldn't blank the
    whole dashboard. On exception, return that section's zero-state shape.
    """
    sections = [
        ("regime", build_regime_section, _empty_regime),
        ("kpis", build_kpis_section, build_kpis_section),
        ("heroSignals", build_hero_signals_section, list),
        ("liveSignals", build_live_signals_section, list),
        ("dataHealth", build_data_health_section, lambda: {"sources": [], "as_of": None}),
        ("concepts", build_funnel_concepts_section, lambda: None),
        ("conceptSuggestionReviews", build_suggestion_reviews_section, list),
        ("watchlist", build_watchlist_section, list),
        ("priceSeries", build_price_series_section, dict),
        ("activeTrades", build_active_trades_section, list),
        ("reasoning", build_reasoning_section, dict),
        ("closedTrades", build_closed_trades_section, list),
        ("missedTrades", build_missed_trades_section, list),
        ("processScorecard", build_process_scorecard_section, build_process_scorecard_section),
        ("sourceLeaderboard", build_source_leaderboard_section, list),
        ("scoreCorrelation", build_score_correlation_section, lambda: {"n_pairs": 0, "components": []}),
        ("thesisChangelog", build_thesis_changelog_section, list),
        ("brainActivity", build_brain_activity_section, list),
        ("sourceHealth", build_source_health_section, list),
        ("costTracker", build_cost_tracker_section, _empty_cost_tracker),
        ("mgmt", build_mgmt_section, _empty_mgmt),
        ("integration", build_integration_section, _empty_integration),
        ("streams", build_streams_section_wrapper, _empty_streams),
    ]
    out: dict[str, Any] = {}
    for key, builder, fallback in sections:
        try:
            value = builder()
        except Exception as exc:
            import sys
            print(f"[desk] section {key} failed: {exc}", file=sys.stderr)
            try:
                value = fallback()
            except Exception:
                value = [] if "[]" in str(builder.__doc__ or "") else {}
        # `streams` (and any future opt-in section) returns None when it
        # wants the mock fallback in data.mock.js to survive the shallow
        # Object.assign merge — emit nothing rather than an empty stub.
        if value is None:
            continue
        out[key] = value

    # Suggestions are derived from the watchlist rows just built — passing
    # them in avoids a second full score load on an already slow payload.
    try:
        out["conceptSuggestions"] = build_concept_suggestions_section(
            out.get("watchlist") or []
        )
    except Exception as exc:
        import sys
        print(f"[desk] section conceptSuggestions failed: {exc}", file=sys.stderr)
        out["conceptSuggestions"] = []
    return out


def _empty_regime() -> dict:
    return {
        "framework": {
            "label": "Unknown", "slug": "unknown", "confidence": 0.0,
            "bias": "neutral", "sizingModifier": 1.0, "scoreModifier": 0,
            "sinceDays": 0,
        },
        "thesis": {"label": "—", "narrative": "—", "version": "—", "author": "—", "lastRevised": "—"},
        "confidenceTrace": [],
        "transitions": [{"date": "—", "from": "—", "to": "—"}],
    }


def _empty_cost_tracker() -> dict:
    return {
        "today": 0.0, "week": 0.0, "month": 0.0,
        "capDaily": 25.0, "capMonthly": 600.0,
        "byAgent": [], "byBackend": [], "spike": False,
    }


def _empty_mgmt() -> dict:
    return {"todos": [], "decisions": [], "commits": []}


def _empty_integration() -> dict:
    return {"tactical": {"connected": False, "lastPoll": "—", "contractVersion": "—", "schemaDrift": False, "mode": "manual"}}


def _empty_streams() -> dict:
    return {"themeMap": [], "assetMap": [], "concepts": [], "sourceGraph": {"nodes": [], "links": []}}
