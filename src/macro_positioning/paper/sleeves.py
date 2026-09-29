"""Attribution taxonomy — which sleeve, class and regime a position expresses.

Three axes over the same book:

    sleeve — Agriculture, Copper & industrial metals, Crypto, Defense …
    class  — Hard assets, Equities, Crypto, Rates & FX, Volatility, Cash
    regime — which macro regime the sleeve is an expression of

This is a REPORTING map and never gates a fill. Position caps come from
`config/correlation_buckets.json`, which answers a different question
("would this stack the same bet?"). This file answers "did the hard-asset
book make money, or was it all crypto beta?"

Membership composes from what already exists rather than redeclaring it:
`config/asset_themes.json` (the themes the sector scorer already knows),
the correlation buckets, plus explicit tickers for the gaps — of which
there were fourteen in the live scored universe, including three names
the book was already holding.

Resolution happens at READ time, not entry time. Re-classifying a ticker
here corrects the whole history rather than leaving old positions in the
wrong bucket forever.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from macro_positioning.core.settings import settings
from macro_positioning.prices.symbol_map import coinbase_tradeable, crypto_majors


SLEEVES_PATH = "config/paper_sleeves.json"
THEMES_PATH = "config/asset_themes.json"
BUCKETS_PATH = "config/correlation_buckets.json"
SECTORS_PATH = "config/ticker_sectors.json"

UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Sleeve:
    id: str
    label: str
    class_id: str
    class_label: str
    regimes: tuple[str, ...] = ()
    note: Optional[str] = None
    members: frozenset[str] = field(default_factory=frozenset)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "classId": self.class_id,
            "classLabel": self.class_label,
            "regimes": list(self.regimes),
            "note": self.note,
        }


UNCLASSIFIED_SLEEVE = Sleeve(
    id=UNCLASSIFIED,
    label="Unclassified",
    class_id="other",
    class_label="Unclassified",
    note="Not mapped in config/paper_sleeves.json yet.",
)


def _load(path: str, override: Optional[Path] = None) -> dict:
    p = override if override is not None else settings.base_dir / path
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# Quote legs that mark a key as a crypto pair rather than an equity. A
# resolved key arrives here in one of three shapes — bare ('ETH'), the
# yfinance form for a non-major coin ('AERO-USD'), or the raw pair the
# KOL layer writes ('SOL/USDT') — and all three name the same asset.
_QUOTE_LEGS = frozenset({"USD", "USDT", "USDC", "BUSD", "TUSD", "PERP"})


def base_key(ticker: str) -> str:
    """The bare asset a ticker names, whatever shape it arrived in.

    'SOL/USDT' -> 'SOL', 'AERO-USD' -> 'AERO', 'BRK-B' -> 'BRK-B'
    (the tail is not a quote leg, so it is a hyphenated equity and is
    left alone), 'NVDA' -> 'NVDA'.
    """
    t = str(ticker or "").upper().strip()
    if not t:
        return ""
    t = t.split("/")[0].strip()
    for sep in ("-", "_"):
        head, found, tail = t.partition(sep)
        if found and tail in _QUOTE_LEGS:
            return head
    return t


@lru_cache(maxsize=1)
def _sector_index() -> dict[str, dict]:
    """ticker -> {name, sector, industry}, from config/ticker_sectors.json.

    The hand-written membership in `paper_sleeves.json` is an allowlist
    of names this desk already had an opinion about — index ETFs, the
    megacaps, the thematic baskets. The copy books trade whatever their
    channel posts, which is mostly small-cap momentum nobody wrote down,
    and every one of those landed in `unclassified` for no better reason
    than that nobody had typed it in yet. This file closes that gap: a
    ticker with a known sector resolves through `$sector_fallback` below
    instead of falling out of the attribution entirely.

    Refresh with scripts/refresh_ticker_sectors.py --write.
    """
    return {
        str(k).upper(): v
        for k, v in ((_load(SECTORS_PATH).get("tickers") or {}).items())
        if isinstance(v, dict)
    }


def reset_sector_cache() -> None:
    _sector_index.cache_clear()


@lru_cache(maxsize=1)
def _taxonomy(
    sleeves_path: Optional[str] = None,
    themes_path: Optional[str] = None,
    buckets_path: Optional[str] = None,
) -> tuple[tuple[Sleeve, ...], dict[str, str], dict[str, dict[str, str]]]:
    """Build the ordered sleeve list and the ticker → sleeve_id index."""
    cfg = _load(SLEEVES_PATH, Path(sleeves_path) if sleeves_path else None)
    themes = (_load(THEMES_PATH, Path(themes_path) if themes_path else None)
              .get("themes") or {})
    buckets_raw = (_load(BUCKETS_PATH, Path(buckets_path) if buckets_path else None)
                   .get("buckets") or [])
    buckets = {b.get("bucket_id"): b for b in buckets_raw}
    classes = cfg.get("classes") or {}

    ordered: list[Sleeve] = []
    index: dict[str, str] = {}

    for spec in cfg.get("sleeves") or []:
        members: set[str] = set()
        theme_key = spec.get("theme")
        if theme_key and theme_key in themes:
            members |= {
                t.upper() for t in (themes[theme_key].get("watchlist_tickers") or [])
            }
        for b in spec.get("buckets") or []:
            members |= {t.upper() for t in ((buckets.get(b) or {}).get("members") or [])}
        # Crypto membership is not written down here. It comes from
        # config/crypto_universe.json, which a Coinbase listing change
        # already updates through scripts/refresh_crypto_universe.py —
        # retyping forty coins into a second file would only guarantee
        # the two disagree the first time one of them moved.
        universe = spec.get("crypto_universe")
        if universe == "majors":
            members |= set(crypto_majors())
        elif universe == "coinbase":
            members |= set(coinbase_tradeable())
        members |= {t.upper() for t in (spec.get("tickers") or [])}

        class_id = spec.get("class") or "other"
        sleeve = Sleeve(
            id=spec.get("id") or UNCLASSIFIED,
            label=spec.get("label") or spec.get("id") or "?",
            class_id=class_id,
            class_label=(classes.get(class_id) or {}).get("label") or class_id,
            regimes=tuple(spec.get("regimes") or []),
            note=spec.get("note"),
            members=frozenset(members),
        )
        ordered.append(sleeve)
        # Declaration order decides ties: the first sleeve claiming a
        # ticker keeps it (QQQ is technology, not broad market).
        for t in members:
            index.setdefault(t, sleeve.id)

    fb = cfg.get("$sector_fallback") or {}
    fallback = {
        "by_industry": {str(k): v for k, v in (fb.get("by_industry") or {}).items()},
        "by_sector": {str(k): v for k, v in (fb.get("by_sector") or {}).items()},
    }
    return tuple(ordered), index, fallback


def reset_cache() -> None:
    """Drop the cached taxonomy. Used in tests and after editing the config."""
    _taxonomy.cache_clear()
    _sector_index.cache_clear()


def all_sleeves() -> tuple[Sleeve, ...]:
    return _taxonomy()[0]


def sleeve_for_ticker(ticker: str) -> Sleeve:
    """Resolve a ticker to its sleeve.

    Three passes, narrowest evidence first: the declared membership, then
    the ticker's own sector where `config/ticker_sectors.json` knows it,
    then `unclassified`. The last is still reported honestly rather than
    folded somewhere plausible — a big unclassified bucket is a to-do,
    and hiding it loses that — but it should now mean "nothing anywhere
    knows this name", not "nobody typed it in".
    """
    if not ticker:
        return UNCLASSIFIED_SLEEVE
    ordered, index, fallback = _taxonomy()
    # Crypto arrives as "SOL/USD", "BTC/USDT" or the resolved "AERO-USD".
    base = base_key(ticker)
    sleeve_id = index.get(base)
    if sleeve_id is None:
        sleeve_id = _sleeve_from_sector(base, fallback)
    if sleeve_id is None:
        return UNCLASSIFIED_SLEEVE
    for s in ordered:
        if s.id == sleeve_id:
            return s
    return UNCLASSIFIED_SLEEVE


def _sleeve_from_sector(base: str, fallback: dict[str, dict[str, str]]) -> Optional[str]:
    """Industry first, sector second. Industry is the one that carries the
    thesis — "Aerospace & Defense" is a defense name whether the screener
    filed it under Industrials or Technology, and "Other Industrial Metals
    & Mining" is the critical-minerals trade while its parent sector,
    Basic Materials, also holds the gold miners."""
    row = _sector_index().get(base)
    if not row:
        return None
    industry = str(row.get("industry") or "")
    sector = str(row.get("sector") or "")
    return fallback["by_industry"].get(industry) or fallback["by_sector"].get(sector)


def classes() -> dict[str, str]:
    """class_id → label, in the order the config declares them."""
    cfg = _load(SLEEVES_PATH)
    out = {k: (v.get("label") or k) for k, v in (cfg.get("classes") or {}).items()}
    out.setdefault("other", "Unclassified")
    return out


def regimes_of(ticker: str) -> tuple[str, ...]:
    return sleeve_for_ticker(ticker).regimes


def coverage(tickers: list[str]) -> dict:
    """How much of a universe this taxonomy actually covers. Surfaced on
    the performance endpoint so an unmapped name is visible rather than
    silently diluting a sleeve's numbers."""
    unmapped = sorted({
        base_key(t) for t in tickers if sleeve_for_ticker(t).id == UNCLASSIFIED
    })
    total = len({base_key(t) for t in tickers})
    return {
        "total": total,
        "classified": total - len(unmapped),
        "unmapped": unmapped,
        "pct": round((total - len(unmapped)) / total * 100, 1) if total else 100.0,
    }


__all__ = [
    "Sleeve", "sleeve_for_ticker", "all_sleeves", "classes", "regimes_of",
    "coverage", "reset_cache", "reset_sector_cache", "base_key", "UNCLASSIFIED",
]
