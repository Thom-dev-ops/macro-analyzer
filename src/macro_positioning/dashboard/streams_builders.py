"""Builders for the /streams SPA tab (S1 theme map, S2 concepts, S3 source graph).

Pure functions over a sqlite3.Connection so the SPA layer can call them
without owning DB lifecycle. Each builder returns JSON-serializable
dicts/lists matching the shapes the JSX already consumes (see
web/streams.jsx and web/data.mock.js streams: {...}).

Theme membership for a signal is the UNION of three sources, so the map
encompasses everything we process — not just whatever the extractors
happened to tag:
  1. Curated sector theme by ticker — config/asset_themes.json maps
     tickers → themes (uranium, technology_ai, energy, defense, ...).
     A signal on NVDA rolls up to technology_ai even if its free-text
     tags are noise.
  2. Real macro/regime tags from thesis_tags_json + macro_regime_tags_json,
     after filtering chart-pattern / sentiment / direction noise.
  3. Asset-class catch-all bucket (equities_broad, crypto_broad, ...) for
     any signal that matched neither of the above — so the long tail of
     undifferentiated retail mentions still appears as a bubble instead of
     silently vanishing.

No schema changes: tier and market_focus are derived in-builder.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from macro_positioning.manual.authors import seeded_author_ids


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_WINDOW_THEME_DAYS = 28
_WINDOW_CONCEPT_DAYS = 14
_WINDOW_NODE_DAYS = 90

# Weekly buckets emitted in mentions_by_week for the theme + asset maps.
# 26 weeks (~6 months) lets the frontend scrubber traverse a longer horizon
# with a user-selectable window (4W / 12W / 26W). The rightmost bucket is
# always the current week. Novelty stays pegged to the shorter theme window
# so "fresh" keeps its old meaning (< 28 days).
_MAP_WEEKS = 26
_MAP_WINDOW_DAYS = _MAP_WEEKS * 7

# Lifecycle scoring — how far along the fresh→extended arc a theme/asset is.
# Wider maps (26w) broke the old `1 - last_7d / max(all_buckets)`: a name
# that spiked once months ago but is still consistently mentioned today
# would show `ratio = last / early_peak` well below 1 and get slammed to
# the extended side. Two fixes here:
#   1. Compare recent activity to a ROLLING RECENT peak (trailing quarter),
#      not the all-time peak in the window. A name steady at ~10/wk for
#      four months lands with ratio ≈ 1 (fresh/middle), not 0.3 (extended).
#   2. Convex `** _LIFECYCLE_CURVE` so the initial part of the decline
#      pushes the bubble only a little — you have to be well past peak to
#      slide toward "extended". Keeps the middle populated instead of
#      everything piling up on the right of the scatter.
_LIFECYCLE_RECENT_WEEKS   = 2   # mean of these buckets = "how loud right now"
_LIFECYCLE_TRAILING_WEEKS = 12  # peak of these = "recent normal"
_LIFECYCLE_CURVE          = 1.7

# A name is a LIVE narrative only if it's been mentioned at all in the last
# ~4 weeks. Older-only names peaked long ago and shouldn't crowd the
# "extended" side of the scatter — the map is about what's currently in
# discourse, not what used to be.
_LIVE_TAIL_WEEKS   = 4
_LIVE_TAIL_MIN     = 1


def _compute_lifecycle(buckets: list[int]) -> float:
    """0=fresh/emerging, 1=fading. See constants above."""
    if not buckets:
        return 0.5
    recent = buckets[-_LIFECYCLE_RECENT_WEEKS:] or buckets
    recent_mean = sum(recent) / max(1, len(recent))
    trailing = buckets[-_LIFECYCLE_TRAILING_WEEKS:] if len(buckets) >= _LIFECYCLE_TRAILING_WEEKS else buckets
    peak = max(trailing) or 1
    ratio = min(1.0, recent_mean / peak)
    return max(0.0, min(1.0, (1.0 - ratio) ** _LIFECYCLE_CURVE))

# Max tickers returned by build_asset_map. The lifecycle scatter is for the
# most-discussed names; the front-end minor-cluster rollup folds the lower
# portion into a single "minor" bubble, so a generous cap is safe here — the
# tail is what makes narrative-drift look sparse otherwise.
_ASSET_MAP_CAP = 150
_ASSET_MIN_MENTIONS = 2

_BULL_SIDES = {"LONG", "ADD"}
_BEAR_SIDES = {"SHORT", "AVOID"}

# Cluster keys mirror web/streams.jsx _CLUSTERS focuses (must match exactly).
_CLUSTER_TOKENS: dict[str, set[str]] = {
    "macro":      {"macro", "rates", "fed"},
    "equities":   {"equities", "equity", "factor", "factors", "sector", "sectors"},
    "tech":       {"tech", "ai", "semis", "semiconductor", "semiconductors", "software"},
    "energy":     {"energy", "commodities", "oil", "uranium", "metals", "nat_gas", "gas"},
    "realassets": {"gold", "assets", "real_estate", "reit", "reits", "silver"},
    "crypto":     {"crypto", "btc", "eth", "digital", "onchain"},
    "credit":     {"credit", "fi", "income", "spreads", "bond", "bonds", "ig", "hy"},
    "fx":         {"fx", "geopolitics", "currency", "em", "dxy", "yen", "euro"},
    "social":     {"social", "news", "media", "twitter", "rss"},
}

# Asset-class → cluster fallback when no thesis tags match a token bucket.
_ASSET_CLASS_TO_CLUSTER: dict[str, str] = {
    "equity": "equities",
    "equities": "equities",
    "etf": "equities",
    "future": "macro",
    "rates": "macro",
    "macro": "macro",
    "fx": "fx",
    "currency": "fx",
    "commodity": "energy",
    "commodities": "energy",
    "energy": "energy",
    "crypto": "crypto",
    "credit": "credit",
    "bond": "credit",
    "gold": "realassets",
}

_TOKEN_SPLIT = re.compile(r"[\s,/_\-]+")

# Tag tokens that are NOT themes. The insider/social extractors leak chart
# patterns, sentiment/microstructure terms, and direction labels into the
# tag stream — a theme map should surface SECTORS / MACRO NARRATIVES, not
# "wedge_pattern" or "social_media_sentiment". We filter at consumption so
# the raw tags stay available for other surfaces (per-signal drill-downs,
# tag co-occurrence, etc.).
#
# Logic: split a candidate theme id on `_`; if EVERY part is in this set,
# drop the theme. So "social_media_sentiment" → ["social","media","sentiment"]
# (all denied → drop). "ai_capex" → ["ai","capex"] (ai keeps it → keep).
_NON_THEME_TOKENS: set[str] = {
    # chart patterns / technical structures / analysis verbs
    "technical", "analysis", "analyses", "analytic", "analytics",
    "resistance", "support", "breakout", "breakdown", "break",
    "wedge", "pattern", "patterns", "channel", "triangle", "pennant",
    "flag", "head", "shoulders", "cup", "handle", "setup", "setups",
    "play", "plays", "idea", "ideas", "potential", "theme",
    "fib", "fibonacci", "level", "levels", "zone", "zones",
    "ema", "sma", "macd", "rsi", "stoch", "stochastic",
    "bollinger", "ttm", "vwap", "atr", "adx",
    # market microstructure / order flow / strategy
    "momentum", "flow", "flows", "retail", "options", "option",
    "spike", "average", "moving", "ranking", "rank", "strategy",
    "meme", "signal", "signals", "spread", "premium", "skew",
    "positive", "negative",
    # sentiment / social platforms
    "social", "media", "sentiment", "trending", "trend", "buzz",
    "interest", "attention", "mention", "mentions", "chatter", "hype",
    "wallstreetbets", "wsb", "stocktwits", "twitter",
    "reddit", "ape", "apewisdom", "rss", "news", "headline",
    # generic direction / lifecycle words
    "stock", "stocks", "trade", "trading", "trades",
    "long", "short", "bullish", "bearish", "mixed", "neutral",
    "bull", "bear", "buy", "sell", "hold", "watch", "exit",
    "trim", "add", "event",
    # generic OHLC / numeric noise
    "high", "low", "open", "close", "volume",
    # Elliott-wave / structure vocabulary the vision extractor leaks as
    # tags ("abc_corrective_structure_in_progress", "elliott_wave_5_wave
    # _impulse_complete", "parabolic_blow_off_top", "multiple_lower_highs
    # _and_lower_lows", "descending_channel_from_june_highs") — chart
    # structures, not themes.
    "elliott", "wave", "waves", "impulse", "impulsive", "corrective",
    "correction", "abc", "structure", "structures", "progress",
    "complete", "completed", "parabolic", "blow", "blowoff", "off",
    "top", "tops", "bottom", "bottoms", "peak", "peaks", "climax",
    "rounding", "rounded", "shaped", "recovery",
    "ascending", "descending", "rising", "falling",
    "higher", "lower", "highs", "lows", "multiple",
    "early", "stage", "opportunity", "opportunities", "alert",
    "from", "and", "the", "in", "at", "of", "to", "above", "below",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    # count words that pad pattern tags ("5_wave", "u_shaped")
    "one", "two", "three", "four", "five", "u", "v", "w",
    "1", "2", "3", "4", "5",
}


def _is_real_theme(theme_id: str) -> bool:
    """True iff theme_id has at least one component that isn't generic
    noise. See _NON_THEME_TOKENS docstring."""
    if not theme_id:
        return False
    parts = [p for p in theme_id.split("_") if p]
    if not parts:
        return False
    return any(p not in _NON_THEME_TOKENS for p in parts)


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(UTC)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d
    except (TypeError, ValueError):
        return None


def _normalize_tag(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or None


# Friendly labels for curated themes + generic buckets. Anything not here
# gets word-wise title case with known acronyms upper-cased.
_EXPLICIT_LABELS: dict[str, str] = {
    "technology_ai":     "Technology / AI",
    "precious_metals":   "Precious Metals",
    "equities_broad":    "Equities · broad",
    "crypto_broad":      "Crypto · broad",
    "commodities_broad": "Commodities · broad",
    "rates_broad":       "Rates · broad",
    "fx_broad":          "FX · broad",
    "options_broad":     "Options · broad",
    "cash":              "Cash",
}
_ACRONYMS = {"ai", "fx", "etf", "us", "em", "btc", "eth", "ev", "reit", "ipo"}


def _title_label(theme_id: str) -> str:
    """Render a theme id as a clean display label.

    technology_ai → Technology / AI · risk_on_expansion → Risk On Expansion
    """
    if theme_id in _EXPLICIT_LABELS:
        return _EXPLICIT_LABELS[theme_id]
    parts = [p for p in str(theme_id).split("_") if p]
    if not parts:
        return str(theme_id)
    return " ".join(p.upper() if p in _ACRONYMS else p.capitalize() for p in parts)


# Asset-class → generic catch-all bucket id (the long-tail fallback).
_ASSET_CLASS_BUCKET: dict[str, str] = {
    "equity":      "equities_broad",
    "equities":    "equities_broad",
    "etf":         "equities_broad",
    "crypto":      "crypto_broad",
    "commodity":   "commodities_broad",
    "commodities": "commodities_broad",
    "rates":       "rates_broad",
    "bond":        "rates_broad",
    "credit":      "rates_broad",
    "fx":          "fx_broad",
    "currency":    "fx_broad",
    "option":      "options_broad",
    "cash":        "cash",
}

# Cached ticker → curated-theme index, loaded from config/asset_themes.json.
_TICKER_THEME_CACHE: dict[str, str] | None = None


def _ticker_theme_index() -> dict[str, str]:
    """Map TICKER (upper) → curated theme key from config/asset_themes.json.

    Cached per-process; the config is static at runtime. Returns {} if the
    file is missing or malformed so the builder degrades to tag + asset-class
    coverage only.
    """
    global _TICKER_THEME_CACHE
    if _TICKER_THEME_CACHE is not None:
        return _TICKER_THEME_CACHE
    idx: dict[str, str] = {}
    try:
        from macro_positioning.core.settings import settings as _settings
        path = _settings.base_dir / "config" / "asset_themes.json"
        data = json.loads(path.read_text())
        for key, spec in (data.get("themes") or {}).items():
            for tk in (spec.get("watchlist_tickers") or []):
                t = str(tk).strip().upper()
                if t:
                    idx.setdefault(t, key)
    except Exception:
        idx = {}
    _TICKER_THEME_CACHE = idx
    return idx


def _load_json_list(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        n = _normalize_tag(it)
        if n:
            out.append(n)
    return out


def _safe_table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        )
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------------------
# Signal loader (shared by S1 + S2)
# ---------------------------------------------------------------------------

def _load_signals(conn: sqlite3.Connection, window_days: int, now: datetime) -> list[dict]:
    if not _safe_table_exists(conn, "signals"):
        return []
    cutoff = (now - timedelta(days=window_days)).isoformat()
    # Only signals from authors the user has explicitly stated count toward
    # conviction. Auto-ingested authors (gov-insider, lobbying, social, …)
    # are excluded — see authors.seeded_author_ids.
    allowed = seeded_author_ids(conn)
    try:
        # Window + bucket by the POST date (when the call was actually made),
        # not extracted_at (when the LLM ran). A bulk backfill extracts
        # hundreds of old posts "today" — bucketing on extracted_at would
        # pile them all into the current week and fake a breakout. post_at =
        # document published_at (chart/TradingView date) → ingested_at →
        # extracted_at fallback. Mirrors _load_document_mentions + signal_decay.
        cur = conn.execute(
            """
            SELECT s.signal_id, s.extracted_at, s.side, s.conviction,
                   s.author_trust_weight, s.source_slug, s.author_id,
                   s.asset_class, s.thesis_tags_json, s.macro_regime_tags_json,
                   s.thesis_summary, s.asset_ticker,
                   COALESCE(d.published_at, d.ingested_at, s.extracted_at) AS post_at
              FROM signals s
              LEFT JOIN documents d ON d.document_id = s.document_id
             WHERE COALESCE(s.status, 'active') = 'active'
               AND COALESCE(d.published_at, d.ingested_at, s.extracted_at) >= ?
            """,
            (cutoff,),
        )
    except sqlite3.Error:
        return []
    out: list[dict] = []
    for r in cur.fetchall():
        if (r[6] or "") not in allowed:
            continue
        post_at = _parse_iso(r[12]) or _parse_iso(r[1])
        out.append({
            "signal_id":    r[0],
            "extracted_at": _parse_iso(r[1]),
            "post_at":      post_at,
            "side":         (r[2] or "").upper(),
            "conviction":   float(r[3] or 0.0),
            "trust":        float(r[4] or 1.0),
            "source_slug":  r[5] or "",
            "author_id":    r[6] or "",
            "asset_class":  (r[7] or "").lower(),
            "thesis_tags":  _load_json_list(r[8]),
            "regime_tags":  _load_json_list(r[9]),
            "thesis_summary": r[10] or "",
            "asset_ticker": r[11] or "",
            "mention_only": False,
        })
    return out


# Common non-curated tickers → bucket, so document-mention tickers that
# aren't in any asset_themes entry still land in a theme.
_COMMON_TICKER_THEME: dict[str, str] = {
    "SPY": "equities_broad", "QQQ": "equities_broad", "DIA": "equities_broad",
    "IWM": "equities_broad", "VIX": "equities_broad",
    "XLF": "equities_broad", "XLK": "equities_broad", "XLY": "equities_broad",
    "XLP": "equities_broad", "XLI": "equities_broad", "XLB": "equities_broad",
    "XLV": "equities_broad", "XLU": "equities_broad",
    "JPM": "equities_broad", "BAC": "equities_broad", "GS": "equities_broad",
    "WFC": "equities_broad",
    "TLT": "rates_broad", "TBT": "rates_broad",
    "DXY": "fx_broad",
}


def _theme_for_ticker(ticker: str, ticker_idx: dict[str, str]) -> str | None:
    """Map a bare ticker (from a document mention) to a theme id:
    curated sector → common-ticker bucket → equities_broad default."""
    tk = (ticker or "").strip().upper()
    if not tk:
        return None
    return ticker_idx.get(tk) or _COMMON_TICKER_THEME.get(tk) or "equities_broad"


# ---------------------------------------------------------------------------
# Keyword-based theme extraction from prose.
#
# The ticker scan can't see thematic, ticker-less views ("we're constructive
# on agriculture", "the uranium thesis is intact"). This vocabulary maps
# theme keywords → theme id so prose coverage flows into the map even before
# the LLM signal extractor runs on a document. Matched case-insensitively
# with word boundaries; multi-word phrases used wherever the single word
# would be too ambiguous.
# ---------------------------------------------------------------------------
_THEME_KEYWORDS: dict[str, list[str]] = {
    "uranium":         [r"uranium", r"yellowcake", r"nuclear", r"enrichment",
                        r"reactor", r"small modular", r"\bSMR\b"],
    "precious_metals": [r"\bgold\b", r"\bsilver\b", r"bullion",
                        r"precious metal", r"debasement"],
    "crypto":          [r"\bcrypto", r"bitcoin", r"ethereum", r"altcoin",
                        r"digital asset", r"stablecoin", r"\bdefi\b",
                        r"on[- ]?chain"],
    "technology_ai":   [r"artificial intelligence", r"semiconductor",
                        r"data ?cent(?:er|re)", r"AI capex",
                        r"AI infrastructure", r"machine learning",
                        r"hyperscaler", r"\bGPU\b"],
    "energy":          [r"energy security", r"\bcrude\b", r"\boil\b",
                        r"natural gas", r"nat gas", r"\bOPEC\b",
                        r"power grid", r"electricity", r"power demand",
                        r"\bLNG\b"],
    "defense":         [r"\bdefen[cs]e\b", r"military", r"\bNATO\b",
                        r"weapons", r"missile"],
    "agriculture":     [r"agriculture", r"farmland", r"\bfarming\b",
                        r"\bcrop", r"\bgrain", r"\bwheat\b", r"\bcorn\b",
                        r"soybean", r"fertili[sz]er", r"food security"],
    "inflation":       [r"inflation", r"\bCPI\b", r"rising prices",
                        r"cost of living", r"disinflation", r"stagflation"],
    "fed_policy":      [r"\bthe fed\b", r"\bFOMC\b", r"rate cut", r"rate hike",
                        r"interest rate", r"yield curve", r"treasury yield",
                        r"monetary policy", r"\bpowell\b", r"quantitative"],
    "recession_risk":  [r"recession", r"economic slowdown", r"hard landing",
                        r"soft landing", r"labou?r market", r"unemployment",
                        r"layoffs?"],
    "geopolitics":     [r"geopolitic", r"\bwar\b", r"sanction", r"tariff",
                        r"trade war", r"\btaiwan\b", r"\bukraine\b",
                        r"middle east"],
}

# Bull/bear cue words for a conservative document-level lexical lean. Used
# ONLY to give keyword-derived theme contributions a low-conviction
# direction; real extracted signals always outweigh them.
_BULL_CUES = re.compile(
    r"\b(?:bullish|long|buy(?:ing)?|accumulat\w*|breakout|upside|rally\w*|"
    r"constructive|overweight|outperform\w*|tailwind|surg\w*)\b",
    re.IGNORECASE,
)
_BEAR_CUES = re.compile(
    r"\b(?:bearish|short(?:ing)?|sell(?:ing)?|dump\w*|breakdown|downside|"
    r"underweight|underperform\w*|headwind|crash\w*|weakness|avoid)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Macro-factor themes — bull/bear is a CATEGORY ERROR for these (you can't be
# "bullish on the Fed"). Each gets a domain axis (e.g. dovish↔hawkish) read
# from dedicated cues, PLUS the implied broad-risk-market impact. The two
# poles map: pole_a → one market impact, pole_b → the other. Convention is
# the standard risk-asset read (dovish/cooling/receding/de-escalating = risk-on
# = bullish; the opposite = risk-off = bearish). Geopolitics is broad-risk:
# escalation can help oil/gold/defense specifically but hurts the tape overall.
_MACRO_FACTOR_AXES: dict[str, dict] = {
    "fed_policy": {
        "axis": "policy stance",
        "a": {"state": "dovish", "impact": "bullish", "cues": re.compile(
            r"\b(?:rate cuts?|cutting|dovish|easing|ease|pivot|accommodat\w*|"
            r"quantitative easing|\bQE\b|lower rates|pause|stimul\w*|"
            r"liquidity injection)\b", re.IGNORECASE)},
        "b": {"state": "hawkish", "impact": "bearish", "cues": re.compile(
            r"\b(?:rate hikes?|hiking|hawkish|tighten\w*|restrictive|"
            r"quantitative tightening|\bQT\b|higher for longer|raise rates?)\b",
            re.IGNORECASE)},
    },
    "inflation": {
        "axis": "inflation trend",
        "a": {"state": "cooling", "impact": "bullish", "cues": re.compile(
            r"\b(?:disinflation|cooling|decelerat\w*|softer|cooler|"
            r"below expectations|easing prices?|falling inflation)\b",
            re.IGNORECASE)},
        "b": {"state": "rising", "impact": "bearish", "cues": re.compile(
            r"\b(?:rising inflation|hot(?:ter)? inflation|sticky|accelerat\w*|"
            r"reaccelerat\w*|stagflation|CPI beat|surg\w* prices?|"
            r"higher prices)\b", re.IGNORECASE)},
    },
    "recession_risk": {
        "axis": "recession risk",
        "a": {"state": "receding", "impact": "bullish", "cues": re.compile(
            r"\b(?:soft landing|resilient|no recession|goldilocks|"
            r"strong labou?r|robust|reaccelerat\w*)\b", re.IGNORECASE)},
        "b": {"state": "elevated", "impact": "bearish", "cues": re.compile(
            r"\b(?:recession|hard landing|slowdown|contraction|layoffs?|"
            r"rising unemployment|downturn|deteriorat\w*)\b", re.IGNORECASE)},
    },
    "geopolitics": {
        "axis": "geopolitical tension",
        "a": {"state": "de-escalating", "impact": "bullish", "cues": re.compile(
            r"\b(?:ceasefire|truce|de-?escalat\w*|peace deal|diplomacy|"
            r"resolution|agreement)\b", re.IGNORECASE)},
        "b": {"state": "escalating", "impact": "bearish", "cues": re.compile(
            r"\b(?:\bwar\b|invasion|attack|strike|sanction|escalat\w*|"
            r"conflict|tension|missile|trade war|tariffs?)\b", re.IGNORECASE)},
    },
}


def _factor_read(text: str, theme_id: str) -> int:
    """Signed domain-axis vote for a macro-factor theme from one document.

    +1 = pole 'a' dominates (dovish/cooling/receding/de-escalating → bullish
    impact), -1 = pole 'b' (hawkish/rising/elevated/escalating → bearish),
    0 = neutral / not a macro-factor theme. Requires 2x dominance like
    _doc_lean, so a passing mention of both poles stays neutral.
    """
    axis = _MACRO_FACTOR_AXES.get(theme_id)
    if not axis or not text:
        return 0
    a = len(axis["a"]["cues"].findall(text))
    b = len(axis["b"]["cues"].findall(text))
    if a == 0 and b == 0:
        return 0
    if a >= b * 2 and a >= 1:
        return 1
    if b >= a * 2 and b >= 1:
        return -1
    return 0


def _factor_evidence(text: str, theme_id: str) -> tuple[int, str, str, str]:
    """Like _factor_read, but also returns the matched cue + a snippet so the
    UI can show WHY a document was read dovish/hawkish/etc.

    Returns (vote, state_label, matched_cue, snippet). vote/state follow
    _factor_read; snippet is ~40 chars of context either side of the cue.
    """
    axis = _MACRO_FACTOR_AXES.get(theme_id)
    if not axis or not text:
        return 0, "neutral", "", ""
    a = list(axis["a"]["cues"].finditer(text))
    b = list(axis["b"]["cues"].finditer(text))
    if not a and not b:
        return 0, "neutral", "", ""
    if len(a) >= len(b) * 2 and a:
        vote, state, m = 1, axis["a"]["state"], a[0]
    elif len(b) >= len(a) * 2 and b:
        vote, state, m = -1, axis["b"]["state"], b[0]
    else:
        vote, state, m = 0, "neutral", (a or b)[0]
    lo, hi = max(0, m.start() - 40), min(len(text), m.end() + 40)
    snippet = ("…" if lo > 0 else "") + re.sub(r"\s+", " ", text[lo:hi]).strip() + ("…" if hi < len(text) else "")
    return vote, state, m.group(0), snippet


def _fred_latest(conn: sqlite3.Connection, series_id: str) -> tuple[str, float] | None:
    """(observation_date, value) of the most recent obs for a FRED series."""
    try:
        row = conn.execute(
            "SELECT observation_date, MAX(value) FROM fred_observations "
            "WHERE series_id=? AND value IS NOT NULL "
            "AND observation_date=(SELECT MAX(observation_date) FROM fred_observations WHERE series_id=? AND value IS NOT NULL)",
            (series_id, series_id),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row or row[0] is None or row[1] is None:
        return None
    return row[0], float(row[1])


def build_fed_policy_primary(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict | None:
    """The Fed's ACTUAL policy from primary data (FRED), independent of any
    KOL commentary. Two reads:
      current  — FOMC target level + last move + how long it's been held
                 (DFEDTARU = Fed Funds upper target, set by the FOMC).
      expected — market-implied forward lean from the 2Y-vs-effective spread
                 (2Y above the funds rate ⇒ market prices higher-for-longer /
                 hikes ⇒ hawkish; below ⇒ cuts priced ⇒ dovish).
    Returns None if FRED coverage is missing.
    """
    if not _safe_table_exists(conn, "fred_observations"):
        return None
    now = _now(now)
    try:
        rows = conn.execute(
            "SELECT observation_date, MAX(value) FROM fred_observations "
            "WHERE series_id='DFEDTARU' AND value IS NOT NULL "
            "GROUP BY observation_date ORDER BY observation_date"
        ).fetchall()
    except sqlite3.Error:
        return None
    path = [(d, float(v)) for d, v in rows if v is not None]
    if not path:
        return None

    latest_date, level = path[-1]
    # Walk back to the most recent level change.
    last_change_date = None
    last_change_bps = 0
    for i in range(len(path) - 1, 0, -1):
        if abs(path[i][1] - path[i - 1][1]) > 1e-9:
            last_change_date = path[i][0]
            last_change_bps = round((path[i][1] - path[i - 1][1]) * 100)
            break

    days_held = 0
    if last_change_date:
        lc = _parse_iso(last_change_date)
        if lc:
            days_held = int((now - lc).total_seconds() / 86400.0)
    # Recent move (<~50d) reads as an active stance; otherwise "holding".
    if last_change_date and days_held < 50:
        action = "cutting" if last_change_bps < 0 else "hiking"
    else:
        action = "holding"

    current = {
        "level": level,
        "as_of": latest_date,
        "last_change_bps": last_change_bps,
        "last_change_date": last_change_date,
        "days_held": days_held,
        "action": action,
    }

    # Expected (market-implied): 2Y vs effective funds.
    dff = _fred_latest(conn, "DFF")
    dgs2 = _fred_latest(conn, "DGS2")
    expected = None
    if dff and dgs2:
        spread_bps = round((dgs2[1] - dff[1]) * 100)
        if spread_bps > 25:
            lean, impact = "hawkish", "bearish"
        elif spread_bps < -25:
            lean, impact = "dovish", "bullish"
        else:
            lean, impact = "neutral", "mixed"
        expected = {
            "two_year": dgs2[1],
            "effective": dff[1],
            "spread_bps": spread_bps,
            "market_lean": lean,
            "market_impact": impact,
        }

    return {"current": current, "expected_market": expected}


_THEME_COMBINED_RE: "re.Pattern[str] | None" = None


def _theme_combined_pattern() -> "re.Pattern[str]":
    """One combined named-group regex over the whole theme vocabulary, so a
    document is scanned in a single pass instead of once per theme.

    Keywords are lower-cased and matched against lower-cased text WITHOUT the
    IGNORECASE flag — case-folding a large alternation per char is the slow
    path; pre-lowering both sides is ~3× faster over the 12k-doc corpus.
    """
    global _THEME_COMBINED_RE
    if _THEME_COMBINED_RE is not None:
        return _THEME_COMBINED_RE
    parts = [
        f"(?P<{theme}>" + "|".join(f"(?:{k.lower()})" for k in kws) + ")"
        for theme, kws in _THEME_KEYWORDS.items()
    ]
    _THEME_COMBINED_RE = re.compile("|".join(parts))
    return _THEME_COMBINED_RE


def _themes_in_text(text: str) -> set[str]:
    """Theme ids whose keyword vocabulary appears in the prose (single pass)."""
    if not text:
        return set()
    found: set[str] = set()
    for m in _theme_combined_pattern().finditer(text.lower()):
        if m.lastgroup:
            found.add(m.lastgroup)
    return found


def _doc_lean(text: str) -> str:
    """Conservative document-level direction from bull/bear cue counts.
    Returns 'LONG' / 'SHORT' / '' (neutral). Deliberately blunt — a
    low-conviction nudge, never authoritative."""
    if not text:
        return ""
    bull = len(_BULL_CUES.findall(text))
    bear = len(_BEAR_CUES.findall(text))
    if bull == 0 and bear == 0:
        return ""
    if bull >= bear * 2 and bull >= 1:
        return "LONG"
    if bear >= bull * 2 and bear >= 1:
        return "SHORT"
    return ""


def _doc_source_key(source_id: str) -> str:
    """Readable source family for a document, consistent-ish with signal
    source_slugs. `forward_guidance` → forward_guidance;
    `manual:gov-insider:perdue` → gov-insider; `manual:telegram-channel:x`
    → telegram-channel."""
    sid = source_id or ""
    if ":" not in sid:
        return sid
    parts = sid.split(":")
    if parts[0] == "manual" and len(parts) >= 2:
        return parts[1]
    return parts[0]


# Short-lived cache so the 3 calls within one build_streams_section (theme
# map, asset map, concepts→theme map) scan the corpus once. Keyed by a cheap
# fingerprint (window + doc count + latest timestamp) so it's safe across
# tests with different DBs and self-invalidates when documents change.
_DOC_MENTION_CACHE: dict[tuple, list[dict]] = {}
_DOC_MENTION_CACHE_ORDER: list[tuple] = []
_DOC_MENTION_CACHE_MAX = 4


def _db_path_of(conn: sqlite3.Connection) -> str:
    """Best-effort path of the main attached database (for cache keying).
    Empty string for in-memory DBs."""
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
            if name == "main":
                return file or ""
    except sqlite3.Error:
        pass
    return ""


def _doc_corpus_fingerprint(conn: sqlite3.Connection, cutoff: str) -> tuple | None:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*), COALESCE(MAX(COALESCE(published_at, ingested_at)), '')
              FROM documents
             WHERE COALESCE(published_at, ingested_at) >= ?
               AND cleaned_text IS NOT NULL AND cleaned_text != ''
            """,
            (cutoff,),
        ).fetchone()
    except sqlite3.Error:
        return None
    # Include the DB path so distinct databases (e.g. per-test tmp dirs that
    # share a doc count + timestamp) never collide on the process-global cache.
    return (_db_path_of(conn), cutoff, row[0], row[1]) if row else None


def _load_document_mentions(
    conn: sqlite3.Connection, window_days: int, now: datetime
) -> list[dict]:
    """Scan the documents corpus and emit pseudo-signals for both ticker
    mentions and keyword-detected themes — what makes the theme/asset maps
    span ALL input sources, not just the handful that have been through LLM
    signal extraction.

    Ticker mentions are direction-neutral. Keyword themes carry a
    conservative low-conviction lexical lean. Pure regex; result cached per
    corpus fingerprint so repeated calls in one build don't re-scan.
    """
    from macro_positioning.scoring.mention_extractor import extract_tickers_from_text

    if not _safe_table_exists(conn, "documents"):
        return []
    cutoff = (now - timedelta(days=window_days)).isoformat()

    fp = _doc_corpus_fingerprint(conn, cutoff)
    if fp is not None and fp in _DOC_MENTION_CACHE:
        return _DOC_MENTION_CACHE[fp]

    # Document mentions span ALL ingested sources — not just seeded authors.
    # Mentions are direction-neutral (mention_only=True below), so they add
    # coverage/volume to the maps without polluting the conviction vote,
    # which is signal-only and still seeded-gated in _load_signals. This is
    # what makes the asset/theme scatter reflect the real universe of names
    # being talked about across the corpus.
    try:
        cur = conn.execute(
            """
            SELECT source_id, author_id, COALESCE(published_at, ingested_at) AS ts,
                   cleaned_text
              FROM documents
             WHERE COALESCE(published_at, ingested_at) >= ?
               AND cleaned_text IS NOT NULL AND cleaned_text != ''
            """,
            (cutoff,),
        )
    except sqlite3.Error:
        return []

    out: list[dict] = []
    for source_id, author_id, ts, text in cur.fetchall():
        text = text or ""
        tickers = extract_tickers_from_text(text)
        kw_themes = _themes_in_text(text)
        if not tickers and not kw_themes:
            continue
        dt = _parse_iso(ts)
        if dt is None:
            continue
        src = _doc_source_key(source_id or "")

        # (a) ticker mentions → per-ticker pseudo-signals (direction-neutral)
        for tk in tickers:
            out.append({
                "signal_id":    None,
                "extracted_at": dt,
                "post_at":      dt,
                "side":         "",
                "conviction":   0.0,
                "trust":        1.0,
                "source_slug":  src,
                "author_id":    author_id or "",
                "asset_class":  "",
                "thesis_tags":  [],
                "regime_tags":  [],
                "thesis_summary": "",
                "asset_ticker": tk,
                "mention_only": True,
            })

        # (b) keyword themes → per-theme pseudo-signals. These carry a
        # CONSERVATIVE lexical direction (low conviction) so "constructive
        # on agriculture" reads bullish, but real extracted signals always
        # outweigh them. forced_theme routes them straight to the theme in
        # aggregation (no ticker needed).
        if kw_themes:
            lean = _doc_lean(text)
            for theme in kw_themes:
                # Macro-factor themes also carry the matched domain cue +
                # snippet so the UI can show the inputs behind the read.
                f_vote, f_state, f_cue, f_snip = _factor_evidence(text, theme)
                out.append({
                    "signal_id":      None,
                    "extracted_at":   dt,
                    "post_at":        dt,
                    "side":           lean,          # "" | LONG | SHORT
                    "conviction":     0.5 if lean else 0.0,
                    "trust":          1.0,
                    "source_slug":    src,
                    "author_id":      author_id or "",
                    "asset_class":    "",
                    "thesis_tags":    [],
                    "regime_tags":    [],
                    "thesis_summary": "",
                    "asset_ticker":   "",
                    "forced_theme":   theme,
                    # Domain-axis vote for macro-factor themes (dovish/hawkish,
                    # rising/cooling, …); 0 for sector themes. Read from THIS
                    # doc's text, independent of the generic bull/bear lean.
                    "factor_vote":    f_vote,
                    "factor_state_doc": f_state,
                    "factor_cue":     f_cue,
                    "factor_snippet": f_snip,
                    "source_id":      source_id or "",
                    # Counts toward volume + (if lean) the direction vote,
                    # but at low weight. Not mention_only so the vote sees it.
                    "mention_only":   lean == "",
                })

    if fp is not None:
        _DOC_MENTION_CACHE[fp] = out
        _DOC_MENTION_CACHE_ORDER.append(fp)
        if len(_DOC_MENTION_CACHE_ORDER) > _DOC_MENTION_CACHE_MAX:
            old = _DOC_MENTION_CACHE_ORDER.pop(0)
            _DOC_MENTION_CACHE.pop(old, None)
    return out


def _signal_themes(s: dict, ticker_idx: dict[str, str] | None = None) -> set[str]:
    """All themes this signal belongs to: curated sector (by ticker) ∪ real
    macro/regime tags ∪ asset-class catch-all. See module docstring.

    ticker_idx is the result of _ticker_theme_index(); passed in by callers
    so it's computed once per build rather than per signal.
    """
    if ticker_idx is None:
        ticker_idx = _ticker_theme_index()
    # Keyword-derived prose contributions name their theme directly.
    forced = s.get("forced_theme")
    if forced:
        return {forced}
    # Document ticker-mentions carry only a ticker — map it straight to a
    # theme (curated → common bucket → equities_broad default).
    if s.get("mention_only"):
        t = _theme_for_ticker(s.get("asset_ticker", ""), ticker_idx)
        return {t} if t else set()
    out: set[str] = set()
    # 1. real macro/regime tags (noise-filtered)
    for t in (set(s["thesis_tags"]) | set(s["regime_tags"])):
        if _is_real_theme(t):
            out.add(t)
    # 2. curated sector theme by ticker (pair base, so ARB/USDT routes
    #    like ARB and FRONG/ETH doesn't route like ETH)
    tk = _base_ticker(s.get("asset_ticker") or "")
    if tk and tk in ticker_idx:
        out.add(ticker_idx[tk])
    # 3. asset-class catch-all so the long tail still shows up
    if not out:
        bucket = _ASSET_CLASS_BUCKET.get(s.get("asset_class", ""))
        if bucket:
            out.add(bucket)
    return out


# ---------------------------------------------------------------------------
# S1 — themeMap
# ---------------------------------------------------------------------------

def build_theme_map(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[dict]:
    """Per-theme card for the S1 scatter map. See web/streams.jsx ThemeMap.

    Corpus = signals (carry direction) + document ticker-mentions (carry
    coverage across ALL input sources). Direction is voted from signals
    only; mentions add volume/sources but stay direction-neutral.
    """
    now = _now(now)
    signals = _load_signals(conn, _MAP_WINDOW_DAYS, now)
    mentions = _load_document_mentions(conn, _MAP_WINDOW_DAYS, now)
    corpus = signals + mentions
    if not corpus:
        return []

    ticker_idx = _ticker_theme_index()
    by_theme: dict[str, list[dict]] = defaultdict(list)
    for s in corpus:
        if s["extracted_at"] is None:
            continue
        for theme in _signal_themes(s, ticker_idx):
            by_theme[theme].append(s)

    out: list[dict] = []
    for theme_id, sigs in by_theme.items():
        if len(sigs) < 3:
            continue  # noise filter per brief

        # _MAP_WEEKS weekly buckets, oldest → newest (rightmost = current week).
        buckets = [0] * _MAP_WEEKS
        first_seen: datetime | None = None
        sources: set[str] = set()
        for s in sigs:
            dt = s.get("post_at") or s["extracted_at"]
            if dt is None:
                continue
            age_days = (now - dt).total_seconds() / 86400.0
            week_idx_from_now = int(age_days // 7)  # 0 = current week
            if 0 <= week_idx_from_now < _MAP_WEEKS:
                buckets[_MAP_WEEKS - 1 - week_idx_from_now] += 1
            if first_seen is None or dt < first_seen:
                first_seen = dt
            slug = s["source_slug"] or s["author_id"]
            if slug:
                sources.add(slug)

        # Drop archival names with no recent activity so the extended edge
        # of the scatter only carries actually-fading live narratives.
        if sum(buckets[-_LIVE_TAIL_WEEKS:]) < _LIVE_TAIL_MIN:
            continue

        mentions_last_7d = buckets[-1]
        mentions_prev_7d = buckets[-2]
        max_window = max(buckets) or 1
        age_days = int((now - first_seen).total_seconds() / 86400.0) if first_seen else 0

        # Direction: trust*conviction-weighted vote of sides. Document
        # mentions have no side — they're skipped so they don't wash a
        # real directional signal toward "mixed".
        score = 0.0
        total_w = 0.0
        for s in sigs:
            if s.get("mention_only"):
                continue
            w = max(0.01, s["trust"]) * max(0.0, s["conviction"])
            if w <= 0:
                w = max(0.01, s["trust"])  # conviction may be 0 — keep a floor
            total_w += w
            if s["side"] in _BULL_SIDES:
                score += w
            elif s["side"] in _BEAR_SIDES:
                score -= w
        if total_w > 0 and abs(score) / total_w > 0.4:
            direction = "bullish" if score > 0 else "bearish"
        else:
            direction = "mixed"

        # Macro-factor themes (fed_policy, inflation, …): the bull/bear vote
        # above is a category error. Replace it with a domain-axis read
        # (dovish/hawkish, rising/cooling, …) from the factor votes, and
        # surface the implied broad-market impact separately.
        axis_cfg = _MACRO_FACTOR_AXES.get(theme_id)
        theme_kind = "macro_factor" if axis_cfg else "asset"
        factor_axis = factor_state = market_impact = None
        factor_tally = None
        factor_inputs = []
        if axis_cfg:
            fscore = sum(s.get("factor_vote", 0) for s in sigs)
            factor_axis = axis_cfg["axis"]
            if fscore > 0:
                factor_state, market_impact = axis_cfg["a"]["state"], axis_cfg["a"]["impact"]
            elif fscore < 0:
                factor_state, market_impact = axis_cfg["b"]["state"], axis_cfg["b"]["impact"]
            else:
                factor_state, market_impact = "neutral", "mixed"
            # For scatter positioning, the theme's "direction" band becomes
            # its market impact (bullish/bearish/mixed) — coherent with the
            # asset themes it shares the axis with.
            direction = market_impact

            # Inputs breakdown: per-doc reads that produced the net vote, so
            # the operator can audit WHY it's neutral/dovish/etc. Tally by
            # pole; list the evidence rows (source · date · cue · snippet).
            a_lbl, b_lbl = axis_cfg["a"]["state"], axis_cfg["b"]["state"]
            factor_tally = {a_lbl: 0, b_lbl: 0, "neutral": 0}
            for s in sigs:
                st = s.get("factor_state_doc")
                if st is None:
                    continue
                factor_tally[st] = factor_tally.get(st, 0) + 1
                if s.get("factor_cue"):
                    factor_inputs.append({
                        "source": s.get("source_slug") or s.get("author_id") or "?",
                        "date": s["post_at"].date().isoformat() if s.get("post_at") else "",
                        "state": st,
                        "cue": s.get("factor_cue"),
                        "snippet": s.get("factor_snippet") or "",
                    })
            factor_inputs.sort(key=lambda x: x["date"], reverse=True)
            factor_inputs = factor_inputs[:12]

        # Primary-source data for themes that have an authoritative feed.
        # Fed Policy: the FOMC's actual target-rate path from FRED, so the
        # panel leads with fact (current policy) and market-implied
        # expectation — the commentary factor read above is downgraded to
        # "expectation sentiment" in the UI.
        primary_data = None
        if theme_id == "fed_policy":
            primary_data = build_fed_policy_primary(conn, now=now)

        # Lifecycle: 1 - recent/max_window (0=emerging, 1=fading)
        lifecycle = _compute_lifecycle(buckets)
        # Novelty
        novelty = max(0.0, min(1.0, 1.0 - (age_days / _WINDOW_THEME_DAYS)))
        # Velocity: tanh-squashed week-over-week growth
        raw_velocity = (mentions_last_7d - mentions_prev_7d) / max(mentions_prev_7d, 1)
        velocity = max(0.0, min(1.0, (math.tanh(raw_velocity) + 1.0) / 2.0))

        out.append({
            "id":               theme_id,
            "label":            _title_label(theme_id),
            "direction":        direction,
            "theme_kind":       theme_kind,
            "factor_axis":      factor_axis,
            "factor_state":     factor_state,
            "market_impact":    market_impact,
            "factor_tally":     factor_tally,
            "factor_inputs":    factor_inputs,
            "primary_data":     primary_data,
            "lifecycle":        round(lifecycle, 3),
            "novelty":          round(novelty, 3),
            "velocity":         round(velocity, 3),
            "age_days":         age_days,
            "mentions_by_week": buckets,
            "sources":          sorted(sources),
        })

    out.sort(key=lambda t: sum(t["mentions_by_week"]), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Asset map — parallel to S1 themeMap but keyed on signals.asset_ticker.
# Same dict shape so the SPA can reuse the ThemeMap component.
# ---------------------------------------------------------------------------

# Junk sentinel values some extractors emit when they can't resolve a
# ticker — drop them rather than render an "N/A" bubble.
_JUNK_TICKERS = {"", "N/A", "NA", "NONE", "NULL", "?", "TBD", "UNKNOWN"}


def _base_ticker(raw: str) -> str:
    """Group key for asset aggregation: the BASE of pair notation.

    Vision/LLM extraction stores DEX pair tickers verbatim (FRONG/ETH,
    CHIIKAWA/SOL, ARB/USDT) — the asset being traded is the base; the
    quote currency must not be credited (a FRONG/ETH chart is not an
    Ethereum call) nor should the raw pair spawn its own junk card.
    Splits on "/" and "_" only — "-" stays intact for equity share
    classes like BRK-B. Returns "" for junk sentinels.
    """
    t = (raw or "").strip().upper()
    if t in _JUNK_TICKERS:  # check sentinels BEFORE splitting ("N/A" → "N")
        return ""
    for sep in ("/", "_"):
        if sep in t:
            t = t.split(sep, 1)[0].strip()
            break
    return "" if t in _JUNK_TICKERS else t


def build_asset_map(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[dict]:
    """Per-asset card mirroring build_theme_map's shape but grouped by
    `signals.asset_ticker` instead of theme tag. Lets the UI surface
    'what tickers are being talked about' alongside 'what narratives'."""
    now = _now(now)
    signals = _load_signals(conn, _MAP_WINDOW_DAYS, now)
    mentions = _load_document_mentions(conn, _MAP_WINDOW_DAYS, now)
    corpus = signals + mentions
    if not corpus:
        return []

    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for s in corpus:
        ticker = _base_ticker(s.get("asset_ticker") or "")
        if not ticker:
            continue
        if s["extracted_at"] is None:
            continue
        by_ticker[ticker].append(s)

    out: list[dict] = []
    for ticker, sigs in by_ticker.items():
        if len(sigs) < _ASSET_MIN_MENTIONS:
            continue

        buckets = [0] * _MAP_WEEKS
        first_seen: datetime | None = None
        sources: set[str] = set()
        for s in sigs:
            dt = s.get("post_at") or s["extracted_at"]
            if dt is None:
                continue
            age_days_f = (now - dt).total_seconds() / 86400.0
            wk = int(age_days_f // 7)
            if 0 <= wk < _MAP_WEEKS:
                buckets[_MAP_WEEKS - 1 - wk] += 1
            if first_seen is None or dt < first_seen:
                first_seen = dt
            slug = s["source_slug"] or s["author_id"]
            if slug:
                sources.add(slug)

        # Drop archival names with no recent activity so the extended edge
        # of the scatter only carries actually-fading live narratives.
        if sum(buckets[-_LIVE_TAIL_WEEKS:]) < _LIVE_TAIL_MIN:
            continue

        mentions_last_7d = buckets[-1]
        mentions_prev_7d = buckets[-2]
        max_window = max(buckets) or 1
        age_days = int((now - first_seen).total_seconds() / 86400.0) if first_seen else 0

        score = 0.0
        total_w = 0.0
        for s in sigs:
            if s.get("mention_only"):
                continue  # mentions carry no direction
            w = max(0.01, s["trust"]) * max(0.0, s["conviction"])
            if w <= 0:
                w = max(0.01, s["trust"])
            total_w += w
            if s["side"] in _BULL_SIDES:
                score += w
            elif s["side"] in _BEAR_SIDES:
                score -= w
        if total_w > 0 and abs(score) / total_w > 0.4:
            direction = "bullish" if score > 0 else "bearish"
        else:
            direction = "mixed"

        lifecycle = _compute_lifecycle(buckets)
        novelty = max(0.0, min(1.0, 1.0 - (age_days / _WINDOW_THEME_DAYS)))
        raw_velocity = (mentions_last_7d - mentions_prev_7d) / max(mentions_prev_7d, 1)
        velocity = max(0.0, min(1.0, (math.tanh(raw_velocity) + 1.0) / 2.0))

        out.append({
            "id":               ticker,
            "label":            ticker,
            "direction":        direction,
            "lifecycle":        round(lifecycle, 3),
            "novelty":          round(novelty, 3),
            "velocity":         round(velocity, 3),
            "age_days":         age_days,
            "mentions_by_week": buckets,
            "sources":          sorted(sources),
        })

    out.sort(key=lambda t: sum(t["mentions_by_week"]), reverse=True)
    # Cap the payload: there's a long tail of hundreds of one-off tickers
    # that aren't useful as scatter bubbles and bloat the SPA (the lifecycle
    # map is for the most-discussed names, not the entire mention universe).
    # The SPA's own minor-cluster rollup then folds the lower part of this.
    return out[:_ASSET_MAP_CAP]


# ---------------------------------------------------------------------------
# Breakouts — the "CHECK THIS OUT" feed (velocity + acceleration ranked)
# ---------------------------------------------------------------------------

_WINDOW_MOMENTUM_DAYS = 42   # 6 weekly buckets for slope / acceleration
_MOMENTUM_WEEKS = 6
_BREAKOUTS_CAP = 30


def _weekly_buckets(sigs: list[dict], now: datetime, n_weeks: int) -> tuple[list[int], datetime | None, set[str]]:
    """Bucket a signal list into n_weeks weekly counts (oldest -> newest)."""
    buckets = [0] * n_weeks
    first_seen: datetime | None = None
    sources: set[str] = set()
    for s in sigs:
        dt = s.get("post_at") or s.get("extracted_at")
        if dt is None:
            continue
        wk = int((now - dt).total_seconds() / 86400.0 // 7)  # 0 = current week
        if 0 <= wk < n_weeks:
            buckets[n_weeks - 1 - wk] += 1
        if first_seen is None or dt < first_seen:
            first_seen = dt
        slug = s.get("source_slug") or s.get("author_id")
        if slug:
            sources.add(slug)
    return buckets, first_seen, sources


def _vote_direction(sigs: list[dict]) -> str:
    """Trust*conviction-weighted bull/bear vote; mention-only rows abstain."""
    score = 0.0
    total_w = 0.0
    for s in sigs:
        if s.get("mention_only"):
            continue
        w = max(0.01, s.get("trust", 0.0)) * max(0.0, s.get("conviction", 0.0))
        if w <= 0:
            w = max(0.01, s.get("trust", 0.0))
        total_w += w
        if s.get("side") in _BULL_SIDES:
            score += w
        elif s.get("side") in _BEAR_SIDES:
            score -= w
    if total_w > 0 and abs(score) / total_w > 0.4:
        return "bullish" if score > 0 else "bearish"
    return "mixed"


# ---------------------------------------------------------------------------
# Real-asset gate for the discovery feeds (concepts + breakouts).
#
# One KOL channel pumping PumpSwap/DEX meme tokens ($LUCKY, CALLCAT,
# CATWIF…) generates fresh LLM tag-themes with perfect novelty/velocity and
# floods the feeds. User directive (Aug 2026): only main cryptos (~top 25)
# and assets listed on exchanges / OTC markets belong in discovery.
# ---------------------------------------------------------------------------

# The hand-maintained top-up this used to carry (BNB, ADA, TON, XLM…) is
# gone: config/crypto_universe.json derives the majors from a live cap
# ranking intersected with Coinbase's listings, which is the same set kept
# current instead of by hand.


def _crypto_majors() -> set[str]:
    """The crypto universe allowed in discovery: the ~top-25 majors.

    NOT the full Coinbase set — that is ~400 coins and would put every
    listed microcap into a feed whose whole purpose is to surface real
    assets. And deliberately NOT unioned with the prices table either:
    symbol collisions there vouch for memes (the channel's "SFM" is
    Safemoon, but SFM is also Sprouts Farmers Market on NYSE)."""
    try:
        from macro_positioning.prices.symbol_map import crypto_majors
        return set(crypto_majors())
    except Exception:
        return set()


# The vision extractor hardcodes asset_class="equity", so a Solana meme
# token often arrives as a bare "equity" ticker. Its thesis prose gives it
# away — detect crypto context lexically as a backstop.
_CRYPTO_CONTEXT_RE = re.compile(
    r"(?i)\b(solana|blockchain|on-?chain|pump\.?swap|uniswap|dexscreener|"
    r"altcoin|memecoin|meme coin|cryptocurrency|token|stablecoin|"
    r"\bFDV\b|liquidity pool|launchpad)\b"
)


def _signal_is_real_asset(s: dict, universe: set[str]) -> bool:
    """False only for junk CRYPTO: pair-notation tickers (FRONG/ETH — DEX
    listings always quote a pair), crypto-classed tickers, or tickers whose
    thesis prose reads crypto — unless the base is a major. The crypto
    check runs FIRST so a meme ticker colliding with a real equity symbol
    (Safemoon vs Sprouts Farmers Market, both "SFM") can't sneak through.
    Equities stay in even when unpriced — channels' equity calls are
    dynamically scored, not a fixed list (see symbol_map), and a small-cap
    on an index/OTC market is exactly what the user wants."""
    raw = (s.get("asset_ticker") or "").strip().upper()
    base = _base_ticker(raw)
    if not base:
        return False  # no ticker — contributes nothing either way
    is_pair = "/" in raw or "_" in raw
    is_crypto = (
        is_pair
        or (s.get("asset_class") or "").lower() in (
            "crypto", "cryptocurrency", "coin", "token")
        or bool(_CRYPTO_CONTEXT_RE.search(s.get("thesis_summary") or ""))
    )
    if is_crypto:
        return base in universe
    return True


def _theme_touches_real_asset(sigs: list[dict], universe: set[str]) -> bool:
    """A theme stays in discovery when its tickered signals are MAJORITY
    real assets — or none reference tickers at all (prose macro themes
    like fed_policy carry no ticker and must not drop). Majority, not
    any(): the Aug-2026 "crypto_pump" tag was 10 meme tokens + 1 genuine
    SOL call, and one real straggler must not carry ten memes into the
    feed."""
    real = junk = 0
    for s in sigs:
        if not (s.get("asset_ticker") or "").strip():
            continue
        if _signal_is_real_asset(s, universe):
            real += 1
        else:
            junk += 1
    if real + junk == 0:
        return True
    return real >= junk


def build_breakouts(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[dict]:
    """Ranked feed of tickers + themes whose mention-rate is accelerating.

    This is the culmination signal: it says "CHECK THIS OUT" so the operator
    can verify. Momentum math lives in learning.theme_momentum; here we bucket
    the corpus by ticker and by theme over 6 weeks, score each, attach the
    context needed to verify (direction, who's calling it, weekly shape), and
    rank by breakout_score.
    """
    from macro_positioning.learning.theme_momentum import compute_momentum

    now = _now(now)
    signals = _load_signals(conn, _WINDOW_MOMENTUM_DAYS, now)
    mentions = _load_document_mentions(conn, _WINDOW_MOMENTUM_DAYS, now)
    corpus = [s for s in (signals + mentions) if s.get("extracted_at") is not None]
    if not corpus:
        return []

    ticker_idx = _ticker_theme_index()
    universe = _crypto_majors()

    by_ticker: dict[str, list[dict]] = defaultdict(list)
    by_theme: dict[str, list[dict]] = defaultdict(list)
    for s in corpus:
        tk = _base_ticker(s.get("asset_ticker") or "")
        if tk:
            by_ticker[tk].append(s)
        for theme in _signal_themes(s, ticker_idx):
            by_theme[theme].append(s)

    out: list[dict] = []

    def _emit(kind: str, key: str, label: str, sigs: list[dict]) -> None:
        buckets, first_seen, sources = _weekly_buckets(sigs, now, _MOMENTUM_WEEKS)
        if sum(buckets) < 3:
            return
        mom = compute_momentum(buckets)
        # Only surface things actually worth a look; quiet/fading fall away.
        if mom.status in ("quiet", "fading"):
            return
        # Breadth: single-source spikes are demoted (one loud voice != a trend).
        n_sources = len(sources)
        breadth = min(1.0, 0.5 + 0.25 * n_sources)  # 1 src ->0.75, >=2 ->1.0
        adj_score = mom.breakout_score * breadth
        out.append({
            "kind": kind,
            "id": key,
            "label": label,
            "direction": _vote_direction(sigs),
            "n_sources": n_sources,
            "sources": sorted(sources)[:8],
            "mentions_by_week": buckets,
            "age_days": int((now - first_seen).total_seconds() / 86400.0) if first_seen else 0,
            "score": round(adj_score, 1),
            **mom.to_dict(),
        })

    for tk, sigs in by_ticker.items():
        if not any(_signal_is_real_asset(s, universe) for s in sigs):
            continue  # DEX meme token — not a tradeable asset for this desk
        _emit("asset", tk, tk, sigs)
    for theme_id, sigs in by_theme.items():
        if not _theme_touches_real_asset(sigs, universe):
            continue  # tag-theme spun up entirely by meme-token drops
        _emit("theme", theme_id, _title_label(theme_id), sigs)

    # Rank: breakouts first, then by adjusted score.
    _rank = {"breakout": 0, "building": 1, "peaking": 2}
    out.sort(key=lambda x: (_rank.get(x["status"], 9), -x["score"]))
    return out[:_BREAKOUTS_CAP]


# ---------------------------------------------------------------------------
# S2 — concepts
# ---------------------------------------------------------------------------

def _truncate_synopsis(text: str, limit: int = 180) -> str:
    text = (text or "").replace("\n", " ").replace("\r", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Prefer last sentence boundary
    for sep in (". ", "! ", "? "):
        idx = cut.rfind(sep)
        if idx > 40:
            return cut[: idx + 1].strip()
    # Else trim at last space
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 40 else cut).strip()


def build_concepts(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[dict]:
    """Pre-filter to novelty>0.7 AND velocity>0.4 (UI re-filters too)."""
    now = _now(now)
    themes = build_theme_map(conn, now=now)
    if not themes:
        return []

    # Reload signals at the concept window (14d) for items/sources counts
    sigs_14d = _load_signals(conn, _WINDOW_CONCEPT_DAYS, now)
    by_theme_14d: dict[str, list[dict]] = defaultdict(list)
    for s in sigs_14d:
        for tag in _signal_themes(s):
            by_theme_14d[tag].append(s)

    # Author display-name lookup
    display_by_slug = _author_display_map(conn)
    universe = _crypto_majors()

    out: list[dict] = []
    for t in themes:
        if t["novelty"] <= 0.7 or t["velocity"] <= 0.4:
            continue
        tid = t["id"]
        recent = by_theme_14d.get(tid, [])
        if not _theme_touches_real_asset(recent, universe):
            continue  # concept exists only because of meme-token drops
        items_count = len({s["signal_id"] for s in recent})
        slugs = [s["source_slug"] or s["author_id"] for s in recent]
        slug_counts = Counter([s for s in slugs if s])
        sources_count = len(slug_counts)
        source_names = [display_by_slug.get(slug, slug) for slug, _ in slug_counts.most_common(3)]

        # Synopsis from latest signal w/ thesis_summary
        recent_sorted = sorted(
            [s for s in recent if s.get("thesis_summary")],
            key=lambda s: s["extracted_at"] or now,
            reverse=True,
        )
        if recent_sorted:
            synopsis = _truncate_synopsis(recent_sorted[0]["thesis_summary"])
        else:
            synopsis = _fallback_synopsis(conn, tid)

        out.append({
            "id":            tid,
            "title":         _title_label(tid),
            "synopsis":      synopsis,
            "novelty":       t["novelty"],
            "velocity":      t["velocity"],
            "items_count":   items_count,
            "sources_count": sources_count,
            "source_names":  source_names,
            "age_days":      t["age_days"],
        })

    return out


def _fallback_synopsis(conn: sqlite3.Connection, theme_id: str) -> str:
    """First 180 chars of most recent document body that mentions the theme.

    Theme id may not appear verbatim in body text, so we just grab the
    most recent signal's parent doc cleaned_text. Keep cheap.
    """
    if not _safe_table_exists(conn, "documents"):
        return ""
    try:
        cur = conn.execute(
            """
            SELECT d.cleaned_text
              FROM signals s
              JOIN documents d ON d.document_id = s.document_id
             WHERE (s.thesis_tags_json LIKE ? OR s.macro_regime_tags_json LIKE ?)
               AND d.cleaned_text IS NOT NULL AND d.cleaned_text != ''
             ORDER BY s.extracted_at DESC
             LIMIT 1
            """,
            (f"%{theme_id}%", f"%{theme_id}%"),
        )
        row = cur.fetchone()
    except sqlite3.Error:
        return ""
    if not row or not row[0]:
        return ""
    return _truncate_synopsis(row[0])


# ---------------------------------------------------------------------------
# S3 — sourceGraph
# ---------------------------------------------------------------------------

def _trust_to_tier(trust: float | None) -> int:
    if trust is None:
        return 2  # baseline assumption
    if trust >= 1.4:
        return 0
    if trust >= 1.15:
        return 1
    if trust >= 0.85:
        return 2
    if trust >= 0.5:
        return 3
    return 4


def _author_display_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Map author_id → display_name. Also include source_slug fallback."""
    out: dict[str, str] = {}
    if not _safe_table_exists(conn, "input_authors"):
        return out
    try:
        cur = conn.execute("SELECT author_id, display_name FROM input_authors")
        for aid, name in cur.fetchall():
            if aid:
                out[aid] = name or aid
    except sqlite3.Error:
        return out
    return out


def _derive_market_focus(
    asset_classes: Iterable[str],
    theme_tokens: Iterable[str],
) -> str:
    """Pick a cluster key for the SPA grid. Tokens take precedence; asset
    class is fallback. Returns one of the _CLUSTERS keys in streams.jsx."""
    counts: Counter[str] = Counter()
    for tok in theme_tokens:
        if not tok:
            continue
        for part in _TOKEN_SPLIT.split(tok):
            if not part:
                continue
            for cluster, vocab in _CLUSTER_TOKENS.items():
                if part in vocab:
                    counts[cluster] += 1
    if counts:
        return counts.most_common(1)[0][0]
    for ac in asset_classes:
        if ac in _ASSET_CLASS_TO_CLUSTER:
            return _ASSET_CLASS_TO_CLUSTER[ac]
    return "macro"


def build_source_graph(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    explicit_tiers: dict[str, int] | None = None,
) -> dict:
    now = _now(now)

    if not _safe_table_exists(conn, "input_authors"):
        return {"nodes": [], "links": []}

    # Operator-assigned tiers from config/sources.json win over the
    # trust-weight heuristic. Keyed by source_id AND slugified display name.
    # Pass `explicit_tiers={}` to force the trust-weight fallback (tests).
    try:
        from macro_positioning.ingestion.source_lifecycle import _normalize_name
        if explicit_tiers is None:
            from macro_positioning.ingestion.source_lifecycle import explicit_tier_map
            explicit_tiers = explicit_tier_map()
    except Exception:
        explicit_tiers, _normalize_name = (explicit_tiers or {}), None

    cutoff = (now - timedelta(days=_WINDOW_NODE_DAYS)).isoformat()
    try:
        # Require a real last_seen_at — seeded-but-never-dropped authors
        # (NULL last_seen_at) are not active sources for the graph.
        cur = conn.execute(
            """
            SELECT author_id, display_name, trust_weight
              FROM input_authors
             WHERE last_seen_at IS NOT NULL
               AND last_seen_at >= ?
            """,
            (cutoff,),
        )
        author_rows = cur.fetchall()
    except sqlite3.Error:
        author_rows = []

    # Per-author signals in 90d (for market_focus)
    sigs_90d = _load_signals(conn, _WINDOW_NODE_DAYS, now)
    by_author: dict[str, list[dict]] = defaultdict(list)
    for s in sigs_90d:
        if s["author_id"]:
            by_author[s["author_id"]].append(s)

    nodes: list[dict] = []
    node_ids: set[str] = set()
    for aid, name, trust_raw in author_rows:
        if not aid:
            continue
        trust = float(trust_raw) if trust_raw is not None else None
        # Explicit operator tier wins; fall back to trust-weight heuristic.
        tier = None
        if explicit_tiers and _normalize_name is not None:
            tier = explicit_tiers.get(aid)
            if tier is None:
                tier = explicit_tiers.get(_normalize_name(name or ""))
        if tier is None:
            tier = _trust_to_tier(trust)
        weight = max(0.0, min(1.5, trust if trust is not None else 1.0)) / 1.5

        sigs = by_author.get(aid, [])
        asset_classes = [s["asset_class"] for s in sigs if s["asset_class"]]
        theme_tokens: list[str] = []
        for s in sigs:
            theme_tokens.extend(s["thesis_tags"])
            theme_tokens.extend(s["regime_tags"])
        focus = _derive_market_focus(asset_classes, theme_tokens) if sigs else "macro"

        nodes.append({
            "id":           aid,
            "name":         name or aid,
            "tier":         tier,
            "weight":       round(weight, 3),
            "market_focus": focus,
        })
        node_ids.add(aid)

    # Links via echo_ties — filter to pairs whose endpoints exist as nodes.
    links: list[dict] = []
    try:
        from macro_positioning.learning.source_attribution import echo_ties
        for pair in echo_ties(conn):
            a = pair.get("source_a")
            b = pair.get("source_b")
            if a in node_ids and b in node_ids:
                links.append({
                    "source":   a,
                    "target":   b,
                    "strength": float(pair.get("strength", 0.0)),
                })
    except Exception:
        links = []

    return {"nodes": nodes, "links": links}


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def build_streams_section(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict:
    """Returns {themeMap, assetMap, concepts, sourceGraph}. Each child is empty-safe."""
    now = _now(now)
    try:
        theme_map = build_theme_map(conn, now=now)
    except Exception:
        theme_map = []
    try:
        asset_map = build_asset_map(conn, now=now)
    except Exception:
        asset_map = []
    try:
        concepts = build_concepts(conn, now=now)
    except Exception:
        concepts = []
    try:
        source_graph = build_source_graph(conn, now=now)
    except Exception:
        source_graph = {"nodes": [], "links": []}
    try:
        breakouts = build_breakouts(conn, now=now)
    except Exception:
        breakouts = []
    return {
        "themeMap":    theme_map,
        "assetMap":    asset_map,
        "concepts":    concepts,
        "sourceGraph": source_graph,
        "breakouts":   breakouts,
    }
