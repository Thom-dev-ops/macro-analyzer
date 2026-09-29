"""Theme + performance extraction for trusted manual-input sources.

Aggregates what each trusted author (Feather Hands family, Stock Unlocked,
etc.) is talking about and how their calls perform forward. Three lenses:

  1. **Themes**  — top tickers, dominant biases, recurring setup types,
                   and asset-cluster patterns across recent drops.
  2. **Performance** — forward returns N days after each ticker mention,
                       weighted by the author's trust_weight. Real metric
                       without needing closed trades — uses the `prices`
                       table the scoring runner already maintains.
  3. **Conviction watchlist** — tickers a trusted source mentions 2+ times
                                within a window with consistent bias.

Designed to power:
  - /journal sourceLeaderboard panel (trusted-source attribution column)
  - a future "Top picks from Feather Hands" SPA section
  - the regime tape's macro-sentiment overlay

Pure-SQLite queries — no LLM calls. Re-run anytime; cheap.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

from macro_positioning.core.settings import settings


# ── Small helpers ────────────────────────────────────────────────────────────


def _pick_features(features_json: Optional[str]) -> dict:
    """Parse extracted_features_json into a flat dict, handling the three
    schema variants Claude returns (flat / setups[] / sparse)."""
    if not features_json:
        return {}
    try:
        f = json.loads(features_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(f, dict):
        return {}
    # Merge setups[0] over top-level so setup-specific fields win
    out = {k: v for k, v in f.items() if k != "setups" and v is not None}
    setups = f.get("setups")
    if isinstance(setups, list) and setups and isinstance(setups[0], dict):
        for k, v in setups[0].items():
            if v is not None:
                out[k] = v
    return out


def _normalize_ticker(t: Optional[str]) -> Optional[str]:
    """Strip quote suffix so SOL/USDT, SOLUSD, SOL → SOL.

    For attribution we care about the underlying asset, not the pair.
    """
    if not t:
        return None
    t = str(t).strip().upper()
    # Strip pair quote (USDT, USD, USDC, USDC.E, BUSD, TUSD, BTC, ETH if paired)
    for sep in ["/", "-", "_"]:
        if sep in t:
            t = t.split(sep)[0]
            break
    # Trim suffix forms like SOLUSDT → SOL, BTCUSD → BTC
    for suffix in ("USDT", "USDC", "BUSD", "TUSD", "USD", "PERP"):
        if t.endswith(suffix) and len(t) > len(suffix) + 1:
            t = t[: -len(suffix)]
            break
    return t or None


# Pattern-strength taxonomy. Score reflects the technical-analysis weight
# of the named structure: well-defined high-probability setups score
# higher than vague directional reads. Tunable per user preference —
# these scores were chosen to match the chart-analysis framework prompt's
# emphasis on confluence + multi-touch confirmation.
_PATTERN_SCORES: list[tuple[tuple[str, ...], int]] = [
    # Tier 1: high-probability classical reversal / continuation patterns
    (("cup and handle", "inverse head and shoulders", "head and shoulders",
      "wave 5", "wave-5", "elliott wave", "complex inverse"), 90),
    # Tier 2: confirmed breakouts / wedge resolutions
    (("falling wedge breakout", "rising wedge breakdown",
      "ascending triangle breakout", "descending triangle breakdown",
      "bull flag", "bear flag", "breakout retest", "breakout pullback",
      "bullish breakout", "bearish breakdown", "wave 3"), 80),
    # Tier 3: pending / coiling structures
    (("symmetrical triangle", "pennant", "ascending channel",
      "descending channel", "rising channel", "compression",
      "tightening range", "fibonacci pullback", "golden pocket",
      "ttm squeeze"), 65),
    # Tier 4: pullbacks + measured moves
    (("pullback", "retest", "measured move", "ab=cd",
      "rounded bottom", "rounded top", "double bottom", "double top",
      "wedge", "triangle", "channel"), 55),
    # Tier 5: ranges / sideways / waiting
    (("range", "consolidation", "sideways", "chop",
      "no clear", "watching", "wait", "indecision"), 35),
]
_PATTERN_DEFAULT = 50  # mentioned but no recognised structure


def _pattern_strength(setup: Optional[str]) -> int:
    """Score a setup description 0-100 by pattern conviction.

    Tries lower-tier matches first so a sentence like "wedge breakout"
    matches the breakout tier, not the generic 'wedge' tier.
    """
    if not setup:
        return 0
    s = str(setup).lower()
    # Strength qualifiers: "clean", "textbook", "obvious" → +10
    qualifier_bonus = 0
    if any(q in s for q in ("clean ", "textbook", "obvious", "clear ", "strong ", "well-defined")):
        qualifier_bonus = 10
    if any(q in s for q in ("potential", "possible", "looks like", "appears", "early", "forming")):
        qualifier_bonus = -10
    for needles, score in _PATTERN_SCORES:
        for needle in needles:
            if needle in s:
                return max(0, min(100, score + qualifier_bonus))
    return _PATTERN_DEFAULT + qualifier_bonus


def _normalize_bias(b: Optional[str]) -> Optional[str]:
    if not b:
        return None
    s = str(b).strip().lower()
    if any(k in s for k in ("bull", "long")):
        return "bullish"
    if any(k in s for k in ("bear", "short")):
        return "bearish"
    if any(k in s for k in ("neutral", "chop", "range", "watch")):
        return "neutral"
    return None


# ── Result dataclasses ──────────────────────────────────────────────────────


@dataclass
class AuthorThemes:
    author_id: str
    display_name: str
    channel: Optional[str]
    parent_channel: Optional[str]
    trust_weight: float
    category: Optional[str]
    n_drops: int
    n_with_vision: int
    n_charts: int                        # drops with an image (analyzable denominator)
    top_tickers: list[tuple[str, int]]  # (ticker, count)
    bias_distribution: dict[str, int]    # {bullish: 12, bearish: 5, neutral: 3}
    top_setups: list[tuple[str, int]]
    top_indicators: list[tuple[str, int]]
    earliest_chart: Optional[str]
    latest_chart: Optional[str]
    high_conviction_tickers: list[dict]  # tickers mentioned ≥2x w/ consistent bias

    def to_dict(self) -> dict:
        return {
            "author_id": self.author_id,
            "display_name": self.display_name,
            "channel": self.channel,
            "parent_channel": self.parent_channel,
            "trust_weight": self.trust_weight,
            "category": self.category,
            "n_drops": self.n_drops,
            "n_with_vision": self.n_with_vision,
            "n_charts": self.n_charts,
            "top_tickers": self.top_tickers,
            "bias_distribution": self.bias_distribution,
            "top_setups": self.top_setups,
            "top_indicators": self.top_indicators,
            "earliest_chart": self.earliest_chart,
            "latest_chart": self.latest_chart,
            "high_conviction_tickers": self.high_conviction_tickers,
        }


# ── Public API ──────────────────────────────────────────────────────────────


def author_themes(
    author_id: str,
    *,
    window_days: int = 90,
    high_conviction_min: int = 1,  # quality-first now — 1 strong mention can qualify
    db_path: Optional[Path] = None,
) -> Optional[AuthorThemes]:
    """Aggregate themes for one author over the given window."""
    db_path = db_path or settings.sqlite_path
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        meta = conn.execute(
            "SELECT * FROM input_authors WHERE author_id = ?",
            (author_id,),
        ).fetchone()
        if meta is None:
            return None

        rows = conn.execute(
            """
            SELECT extracted_features_json, published_at, user_metadata_json,
                   tags_json, attachment_paths_json, attachment_path,
                   cleaned_text, raw_text
            FROM documents
            WHERE author_id = ?
            AND COALESCE(published_at, ingested_at) >= ?
            """,
            (author_id, cutoff),
        ).fetchall()

    # A "drop" only counts as CONTENT if it's a chart (the image + its caption)
    # or substantive text (mentions a ticker). Pure DM chatter — "Copy",
    # "Call me", "Let's catch up" — is dropped from the count entirely; a
    # DM/relay source shouldn't read "13/57" when 44 are one-word replies.
    #   n_charts = chart drops (analyzable via vision)
    #   n_drops  = content drops (charts + ticker-bearing text)
    from macro_positioning.scoring.mention_extractor import extract_tickers_from_text
    n_charts = 0
    n_drops = 0
    for r in rows:
        is_chart = bool(r["attachment_paths_json"] or r["attachment_path"])
        if is_chart:
            n_charts += 1
            n_drops += 1
        else:
            txt = r["cleaned_text"] or r["raw_text"] or ""
            if txt and extract_tickers_from_text(txt):
                n_drops += 1  # substantive text (has a ticker) — real content
    n_with_vision = 0
    ticker_counter: Counter[str] = Counter()
    bias_counter: Counter[str] = Counter()
    setup_counter: Counter[str] = Counter()
    indicator_counter: Counter[str] = Counter()
    chart_dates: list[str] = []
    # Per-ticker accumulators feeding the composite conviction score.
    # Frequency alone is a poor signal — 5 mentions might be 5 different
    # timeframe views of the SAME setup, not 5 independent reads. The
    # scoring formula weights *quality* signals over raw count:
    #   • Claude's confluence_score (per-chart 1-5 framework rating)
    #   • Multi-timeframe coverage (1h + 4h + 1d on same ticker = strong)
    #   • Persistence over time (mentions across 3 weeks ≫ 1 day)
    #   • Bias-strength language ("strongly bullish" > "bullish" > "watch")
    #   • Decay weight (recent > stale)
    per_ticker: dict[str, list[str]] = defaultdict(list)
    per_ticker_dates: dict[str, list[str]] = defaultdict(list)
    per_ticker_active: dict[str, int] = defaultdict(int)
    per_ticker_decay_sum: dict[str, float] = defaultdict(float)
    per_ticker_timeframes: dict[str, set] = defaultdict(set)
    per_ticker_confluence: dict[str, list[float]] = defaultdict(list)
    per_ticker_bias_strength: dict[str, list[int]] = defaultdict(list)
    per_ticker_setups: dict[str, list[str]] = defaultdict(list)
    # Cross-author confirmation — distinct OTHER trusted authors who posted
    # the same chart (byte-identical), recorded by the Telegram poller in
    # tags_json.also_called_by. A genuinely independent author landing on
    # the same setup is real confirmation (the sister-group crossover), so
    # each confirmed ticker gets a small conviction bump below. Keyed by
    # ticker → set of confirming author_ids (deduped, so Ari relaying the
    # same chart 10x can't stack).
    per_ticker_confirmers: dict[str, set] = defaultdict(set)
    # Pattern conviction = how "high-probability" the named structure is.
    # Cup-and-handle / wave 5 / breakout-retest score higher than
    # consolidation / range / waiting. Averaged into the composite below.
    per_ticker_pattern: dict[str, list[int]] = defaultdict(list)

    from macro_positioning.learning.signal_decay import compute_decay

    for r in rows:
        feat = _pick_features(r["extracted_features_json"])
        # Skip placeholders ({image_sha256: ...}) — only count rows where
        # Claude actually populated analysis fields. ticker/bias/pattern
        # are the canonical signals of a real extraction.
        has_real = any(
            feat.get(k) for k in ("ticker", "bias", "pattern", "setup_type",
                                  "dominant_pattern", "asset", "instrument", "direction")
        )
        if not feat or "error" in feat or not has_real:
            continue
        n_with_vision += 1
        if r["published_at"]:
            chart_dates.append(r["published_at"][:10])

        decay = compute_decay(r["published_at"], feat.get("timeframe"))

        # Ticker: prefer Claude's, fallback to user metadata
        ticker = _normalize_ticker(
            feat.get("ticker") or feat.get("asset") or feat.get("instrument")
        )
        if not ticker:
            try:
                um = json.loads(r["user_metadata_json"] or "{}")
                ticker = _normalize_ticker(um.get("user", {}).get("ticker"))
            except json.JSONDecodeError:
                pass
        if ticker:
            ticker_counter[ticker] += 1
            per_ticker_dates[ticker].append(r["published_at"] or "")
            per_ticker_decay_sum[ticker] += decay.decay_weight
            if decay.signal_status in ("active", "aging"):
                per_ticker_active[ticker] += 1
            # Capture per-quality signals for the composite conviction calc
            tf = feat.get("timeframe")
            if tf:
                per_ticker_timeframes[ticker].add(str(tf).strip().upper())
            # Confluence: Claude returns 1-5 int, sometimes "4/5" string
            cs = feat.get("confluence_score")
            if cs is not None:
                try:
                    if isinstance(cs, str) and "/" in cs:
                        cs = cs.split("/")[0]
                    per_ticker_confluence[ticker].append(float(cs))
                except (ValueError, TypeError):
                    pass
            # Bias strength from text: "strongly bullish" > "bullish" > "watch"
            raw_bias = str(feat.get("bias") or feat.get("direction") or "").lower()
            if "strong" in raw_bias and ("bull" in raw_bias or "long" in raw_bias):
                per_ticker_bias_strength[ticker].append(2)
            elif "bull" in raw_bias or "long" in raw_bias:
                per_ticker_bias_strength[ticker].append(1)
            elif "strong" in raw_bias and ("bear" in raw_bias or "short" in raw_bias):
                per_ticker_bias_strength[ticker].append(-2)
            elif "bear" in raw_bias or "short" in raw_bias:
                per_ticker_bias_strength[ticker].append(-1)
            elif raw_bias:
                per_ticker_bias_strength[ticker].append(0)
            # Setup string — for de-duping "5 mentions same pattern" +
            # for the per-ticker pattern-strength average.
            setup_str = (
                feat.get("setup_type") or feat.get("pattern") or feat.get("dominant_pattern")
            )
            if isinstance(setup_str, str) and setup_str.strip():
                per_ticker_setups[ticker].append(setup_str.strip()[:80])
                per_ticker_pattern[ticker].append(_pattern_strength(setup_str))
            # Cross-author confirmation from the poller's dedupe pass.
            try:
                tags = json.loads(r["tags_json"] or "{}")
                for entry in tags.get("also_called_by") or []:
                    cid = entry.get("author_id") if isinstance(entry, dict) else None
                    if cid:
                        per_ticker_confirmers[ticker].add(cid)
            except (json.JSONDecodeError, TypeError):
                pass

        bias = _normalize_bias(feat.get("bias") or feat.get("direction"))
        if bias:
            bias_counter[bias] += 1
            if ticker:
                per_ticker[ticker].append(bias)

        setup = feat.get("setup_type") or feat.get("pattern") or feat.get("dominant_pattern")
        if isinstance(setup, str) and setup.strip():
            # Truncate verbose patterns to 60 chars for grouping
            setup_counter[setup.strip()[:60]] += 1

        inds = feat.get("indicators_visible") or []
        if isinstance(inds, list):
            for ind in inds:
                if isinstance(ind, str):
                    indicator_counter[ind] += 1

    # High-conviction: tickers mentioned ≥ N times with ≥75% bias agreement
    high_conviction = []
    for ticker, biases in per_ticker.items():
        if len(biases) < high_conviction_min:
            continue
        bc = Counter(biases)
        top_bias, top_n = bc.most_common(1)[0]
        agreement = top_n / len(biases)
        if agreement >= 0.75:
            dates_all = sorted([d for d in per_ticker_dates[ticker] if d], reverse=True)
            n_total = len(biases)
            n_active = per_ticker_active.get(ticker, 0)
            decay_avg = (per_ticker_decay_sum.get(ticker, 0.0) / n_total) if n_total else 0.0

            # === Quality signals ===
            timeframes = sorted(per_ticker_timeframes.get(ticker, set()))
            n_timeframes = len(timeframes)
            confluence_list = per_ticker_confluence.get(ticker, [])
            avg_confluence = sum(confluence_list) / len(confluence_list) if confluence_list else None
            bias_strength_list = per_ticker_bias_strength.get(ticker, [])
            avg_bias_strength = sum(bias_strength_list) / len(bias_strength_list) if bias_strength_list else 0
            # Persistence: how many distinct chart dates the signal appears on
            distinct_days = len({d[:10] for d in dates_all if d})

            # === Composite conviction score (0-100) ===
            # Quality-weighted blend — a single high-quality multi-TF
            # confluent setup outscores 5 mentions of the same chart.
            #
            #   30% Claude's framework confluence (1-5 → 0-100)
            #   25% pattern conviction (cup-and-handle ≫ range)
            #   20% multi-timeframe coverage (1 TF = 25, 4+ = 100)
            #   15% persistence across distinct days (1d = 25 → 14d+ = 100)
            #   10% decay freshness (raw 0-1 → 0-100)
            #   bonus: ±10% from bias-strength language
            pattern_scores = per_ticker_pattern.get(ticker, [])
            avg_pattern_strength = (
                sum(pattern_scores) / len(pattern_scores) if pattern_scores else 50
            )
            score_confluence = ((avg_confluence or 2.5) - 1) / 4 * 100  # 1-5 → 0-100
            score_pattern = avg_pattern_strength
            score_timeframes = min(100, 25 + (n_timeframes - 1) * 25) if n_timeframes else 25
            score_persistence = min(100, 25 + (distinct_days - 1) * (75 / 13))  # ramps over 14d
            score_decay = decay_avg * 100
            base = (
                0.30 * score_confluence
                + 0.25 * score_pattern
                + 0.20 * score_timeframes
                + 0.15 * score_persistence
                + 0.10 * score_decay
            )
            # Amplify when language is emphatic (avg ±2 = "strongly bullish"). Cap 100.
            bias_modifier = 1.0 + (abs(avg_bias_strength) - 1.0) * 0.10
            # Cross-author confirmation bump — +25% per distinct independent
            # trusted author who posted the same chart (sister-group
            # crossover), capped at +50% so two confirmations max out the
            # bonus. Same-author reposts and pure relays (Ari) never reach
            # here — the poller only records genuinely different authors in
            # also_called_by. User directive: "when cross posted dedupe and
            # rate with perhaps a .25 increase."
            n_confirmers = len(per_ticker_confirmers.get(ticker, set()))
            confirm_modifier = 1.0 + min(n_confirmers, 2) * 0.25
            conviction = min(100.0, max(0.0, base * bias_modifier * confirm_modifier))

            # Setup diversity: how many distinct setup patterns Claude
            # extracted across this ticker's mentions. Helps the user
            # distinguish "5 charts saying cup-and-handle" (1 distinct)
            # from "wedge + flag + breakout from 3 timeframes" (3 distinct).
            n_distinct_setups = len({s.lower() for s in per_ticker_setups.get(ticker, []) if s})

            high_conviction.append({
                "ticker": ticker,
                "bias": top_bias,
                "agreement_pct": round(agreement * 100, 1),
                "conviction_score": round(conviction, 1),
                # Per-component visibility — lets the UI show what drove it
                "confluence_avg": round(avg_confluence, 2) if avg_confluence else None,
                "pattern_strength_avg": round(avg_pattern_strength, 1),
                "timeframes": timeframes,
                "n_timeframes": n_timeframes,
                "distinct_days": distinct_days,
                "n_distinct_setups": n_distinct_setups,
                "bias_strength_avg": round(avg_bias_strength, 2),
                "decay_weight_avg": round(decay_avg, 3),
                # Cross-author confirmation — how many distinct independent
                # trusted authors also posted this exact chart. >0 = the +25%
                # bump applied. Lets the UI badge "confirmed by N sources".
                "confirmed_by_n": n_confirmers,
                # Raw frequency kept for context but no longer the primary sort
                "mentions": n_total,
                "active_mentions": n_active,
                "stale_mentions": max(0, n_total - n_active),
                "first_seen": dates_all[-1][:10] if dates_all else None,
                "last_seen": dates_all[0][:10] if dates_all else None,
            })
    # Sort by composite conviction score — quality first, frequency last.
    high_conviction.sort(key=lambda x: -x["conviction_score"])

    chart_dates_sorted = sorted(chart_dates)
    return AuthorThemes(
        author_id=author_id,
        display_name=meta["display_name"],
        channel=meta["channel"],
        parent_channel=meta["parent_channel"],
        trust_weight=meta["trust_weight"] or 1.0,
        category=meta["category"],
        n_drops=n_drops,
        n_with_vision=n_with_vision,
        n_charts=n_charts,
        top_tickers=ticker_counter.most_common(15),
        bias_distribution=dict(bias_counter),
        top_setups=setup_counter.most_common(10),
        top_indicators=indicator_counter.most_common(8),
        earliest_chart=chart_dates_sorted[0] if chart_dates_sorted else None,
        latest_chart=chart_dates_sorted[-1] if chart_dates_sorted else None,
        high_conviction_tickers=high_conviction[:20],
    )


def trusted_source_themes(
    *,
    min_trust: float = 1.15,
    window_days: int = 90,
) -> list[AuthorThemes]:
    """Themes for every author whose trust_weight meets the threshold.

    Default 1.15 captures both T0 (≥1.4: Feather Hands family, Stock
    Unlocked, Forward Guidance, Ari Gold) and T1 (≥1.15: Market Traders,
    OG Whales, Wolf Pack, Gem Hunters) per `_trust_to_tier`. Lower the bar
    to include more sources.
    """
    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT author_id FROM input_authors "
            "WHERE COALESCE(trust_weight, 1.0) >= ? "
            "ORDER BY trust_weight DESC, display_name",
            (min_trust,),
        ).fetchall()

    out: list[AuthorThemes] = []
    for r in rows:
        t = author_themes(r["author_id"], window_days=window_days)
        if t and t.n_drops > 0:
            out.append(t)
    return out


def author_ticker_drops(
    author_id: str,
    ticker: str,
    *,
    window_days: int = 365,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """All drops where ``author_id`` mentioned ``ticker`` within the window.

    Powers the I3 "click a chip to see why" drill-down — returns each drop
    with bias, setup, key levels, next-move call, and chart attachments.
    Ticker matching normalises pair variations so SOL/USDT == SOL.
    """
    db_path = db_path or settings.sqlite_path
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    wanted = _normalize_ticker(ticker)
    if not wanted:
        return []

    out: list[dict] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT document_id, title, published_at, ingested_at,
                   attachment_paths_json, attachment_path,
                   user_metadata_json, extracted_features_json
            FROM documents
            WHERE author_id = ?
            AND COALESCE(published_at, ingested_at) >= ?
            ORDER BY COALESCE(published_at, ingested_at) DESC
            """,
            (author_id, cutoff),
        ).fetchall()

    for r in rows:
        feat = _pick_features(r["extracted_features_json"])
        candidates = [feat.get("ticker"), feat.get("asset"), feat.get("instrument")]
        try:
            um = json.loads(r["user_metadata_json"] or "{}")
            candidates.append(um.get("user", {}).get("ticker"))
            candidates.append(um.get("resolved", {}).get("ticker"))
        except json.JSONDecodeError:
            pass
        if not any(_normalize_ticker(c) == wanted for c in candidates if c):
            continue

        paths: list[str] = []
        if r["attachment_paths_json"]:
            try:
                paths = json.loads(r["attachment_paths_json"]) or []
            except json.JSONDecodeError:
                pass
        if not paths and r["attachment_path"]:
            paths = [r["attachment_path"]]

        # Per-drop time-decay snapshot — drives the freshness badge + the
        # weighted theme rollups (recent drops dominate).
        from macro_positioning.learning.signal_decay import (
            compute_decay,
            decay_label,
        )
        timeframe = feat.get("timeframe")
        decay = compute_decay(r["published_at"], timeframe)

        # Surface a ready-to-use chart URL list (`/uploads/charts/...` is
        # mounted) so the SPA can render thumbnails / click-through to
        # the full chart without joining attachment_paths itself.
        chart_urls = ["/" + p for p in paths]

        out.append({
            "document_id": r["document_id"],
            "title": r["title"],
            "published_at": r["published_at"],
            "ingested_at": r["ingested_at"],
            "attachment_paths": paths,
            "chart_urls": chart_urls,
            "ticker": feat.get("ticker") or feat.get("asset") or wanted,
            "bias": feat.get("bias"),
            "direction": feat.get("direction"),
            "setup": feat.get("setup_type") or feat.get("pattern") or feat.get("dominant_pattern"),
            "timeframe": timeframe,
            "key_levels": feat.get("key_levels") or feat.get("historical_levels") or [],
            "indicators": feat.get("indicators_visible") or [],
            "macd_state": feat.get("macd_state") or feat.get("macd_ttm_state"),
            "rsi_state": feat.get("rsi_state") or feat.get("rsi_structure"),
            "next_move": feat.get("most_probable_next_move") or feat.get("notes"),
            "invalidation": feat.get("invalidation_level") or feat.get("invalidation"),
            "confluence_score": feat.get("confluence_score"),
            # Decay block — surfaced as a freshness badge in the UI and
            # multiplied into the conviction-pick agreement score.
            "decay": decay.to_dict(),
            "decay_label": decay_label(decay),
        })
    # Sort by decay weight so freshest setups bubble to the top of the
    # drill-down (most actionable first, stale last).
    out.sort(key=lambda d: -d["decay"]["decay_weight"])
    return out


def family_summary(
    *,
    parent_channel: str = "Feather Hands",
    window_days: int = 90,
) -> dict:
    """Union the themes for every author whose parent_channel matches.

    For "Feather Hands" this aggregates Big_Nuts + MadDog31 + joejoe55 +
    Market Traders into one community-level view.
    """
    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT author_id FROM input_authors "
            "WHERE parent_channel = ? OR (channel = ? AND parent_channel IS NULL)",
            (parent_channel, parent_channel),
        ).fetchall()

    members: list[AuthorThemes] = []
    for r in rows:
        t = author_themes(r["author_id"], window_days=window_days)
        if t and t.n_with_vision > 0:
            members.append(t)

    # Roll-up: union top tickers (sum counts across members), max-of bias
    rollup_tickers: Counter[str] = Counter()
    rollup_bias: Counter[str] = Counter()
    rollup_setups: Counter[str] = Counter()
    n_drops = 0
    n_with_vision = 0
    earliest: Optional[str] = None
    latest: Optional[str] = None
    for m in members:
        n_drops += m.n_drops
        n_with_vision += m.n_with_vision
        for t, c in m.top_tickers:
            rollup_tickers[t] += c
        for b, c in m.bias_distribution.items():
            rollup_bias[b] += c
        for s, c in m.top_setups:
            rollup_setups[s] += c
        if m.earliest_chart and (not earliest or m.earliest_chart < earliest):
            earliest = m.earliest_chart
        if m.latest_chart and (not latest or m.latest_chart > latest):
            latest = m.latest_chart

    return {
        "parent_channel": parent_channel,
        "members": [
            {"display_name": m.display_name, "n_drops": m.n_drops,
             "n_with_vision": m.n_with_vision, "trust_weight": m.trust_weight}
            for m in members
        ],
        "n_drops": n_drops,
        "n_with_vision": n_with_vision,
        "top_tickers": rollup_tickers.most_common(20),
        "bias_distribution": dict(rollup_bias),
        "top_setups": rollup_setups.most_common(10),
        "earliest_chart": earliest,
        "latest_chart": latest,
    }
