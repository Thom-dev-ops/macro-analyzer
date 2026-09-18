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


SLEEVES_PATH = "config/paper_sleeves.json"
THEMES_PATH = "config/asset_themes.json"
BUCKETS_PATH = "config/correlation_buckets.json"

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


@lru_cache(maxsize=1)
def _taxonomy(
    sleeves_path: Optional[str] = None,
    themes_path: Optional[str] = None,
    buckets_path: Optional[str] = None,
) -> tuple[tuple[Sleeve, ...], dict[str, str]]:
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

    return tuple(ordered), index


def reset_cache() -> None:
    """Drop the cached taxonomy. Used in tests and after editing the config."""
    _taxonomy.cache_clear()


def all_sleeves() -> tuple[Sleeve, ...]:
    return _taxonomy()[0]


def sleeve_for_ticker(ticker: str) -> Sleeve:
    """Resolve a ticker to its sleeve. Unmapped tickers report as
    `unclassified` rather than being quietly folded somewhere plausible —
    a big unclassified bucket is a to-do, and hiding it loses that."""
    if not ticker:
        return UNCLASSIFIED_SLEEVE
    ordered, index = _taxonomy()
    # Crypto pairs arrive as "SOL/USD" or "BTC/USDT" from the KOL layer.
    base = str(ticker).upper().split("/")[0].strip()
    sleeve_id = index.get(base)
    if sleeve_id is None:
        return UNCLASSIFIED_SLEEVE
    for s in ordered:
        if s.id == sleeve_id:
            return s
    return UNCLASSIFIED_SLEEVE


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
        str(t).upper().split("/")[0]
        for t in tickers
        if sleeve_for_ticker(t).id == UNCLASSIFIED
    })
    total = len({str(t).upper().split("/")[0] for t in tickers})
    return {
        "total": total,
        "classified": total - len(unmapped),
        "unmapped": unmapped,
        "pct": round((total - len(unmapped)) / total * 100, 1) if total else 100.0,
    }


__all__ = [
    "Sleeve", "sleeve_for_ticker", "all_sleeves", "classes", "regimes_of",
    "coverage", "reset_cache", "UNCLASSIFIED",
]
