"""Setup-type classifier — detector × asset → framework vocabulary.

`macro_brain.agents.regime_classifier` scores macro alignment (15 of the
100 composite points) by asking one question: is this setup's *type* one
the active regime prefers? The scoring runner never answered it — it
passed ``setup_type=""``, so every ticker took the "no setup type given"
branch and scored ``0.5 × regime_confidence``. Across the 2026-08-20
passes that produced exactly two values, 3 and 4, for all 749 rows: 15%
of the score ranking nothing.

This module answers it. The technical agent already names what it found
(``breakout_20d``, ``pullback_support``, …); pairing that with what the
asset *is* (theme + class + relative strength) yields a name from the
framework's own vocabulary.

Honesty rules, because this feeds a score:

- No levels, or mechanical rails only, means no structural setup exists.
  That is ``watchlist_building`` — preferred under transitional chop and
  nowhere else, which is the correct read: nothing to enter yet. We do
  not dress a placeholder up as a regime-preferred setup.
- This classifier names the ENTRY. ``LevelSet.structural`` does not: it
  goes True as soon as a structure zone produces the *stop*, which v2
  does even when the entry is a mechanical rail at spot
  (``mechanical_v0+structure``). A structural stop is not a structural
  entry, so a mechanical rail stays ``watchlist_building`` however good
  its stop is — the honest read is "a level to watch, not a trigger to
  take". Call `has_structural_entry` when the flag and the name have to
  agree in the same sentence.
- v2 records how the rails were *fused* by appending suffixes to the
  detector name (``pullback_support+structure+voices``, levels.py). Those
  suffixes say nothing about what was detected, so every match here is
  made on the base detector — `base_detector` strips them. Matching the
  composite string instead dropped every v2 row to ``watchlist_building``
  and switched this component back off, which is the bug this paragraph
  exists to stop recurring.
- Short-side structure maps to the risk-off vocabulary even in a bullish
  regime. A breakdown is a breakdown.
- The specialised commodity names (uranium, precious metals, miners) are
  only used when the asset actually belongs to that theme.
- A detector can be faithfully describable by more than one name — a
  pullback to a defended level IS both ``pullback_to_support`` and
  ``support_retest``. Where that is true we pick the name in the ACTIVE
  regime's vocabulary, because the framework is asking "is this the kind
  of setup to take right now", and both names are honest answers. We
  never rename across meanings: a breakout has no chop-vocabulary
  synonym, so in chop it stays a breakout and scores the non-preferred
  0.3 — which is the framework declining to reward chasing.

Vocabulary per regime lives in `regime_classifier.PREFERRED_SETUPS`;
every string returned here exists in that union, so a match is a real
match and a non-match scores the honest 0.3 × confidence.
"""

from __future__ import annotations

from macro_brain.agents.regime_classifier.classifier import PREFERRED_SETUPS

# Themes whose breakouts are the *point* of a commodity-led regime.
_URANIUM = "uranium"
_PRECIOUS = "precious_metals"
_CRYPTO = "crypto"
_ENERGY = "energy"
_AGRICULTURE = "agriculture"

# Asset classes that carry commodity exposure in some form.
_COMMODITY_CLASSES = {
    "commodity",
    "commodity_and_equity",
    "commodity_and_miners",
    "commodity_equity",
}

# Producers rather than the commodity itself — the "miner relative
# strength" read only makes sense for these.
_MINERS = {
    "GDX", "GDXJ", "SILJ", "NEM", "AEM", "PAAS", "WPM", "FNV",
    "CCJ", "DNN", "UEC", "NXE", "URNM", "URA", "PALAF", "EOG", "OIH",
}

# Relative-strength margin (20d, vs benchmark) that counts as leadership.
_RS_LEAD_MARGIN = 0.02

# Detectors that constitute a real ENTRY pattern. Anything else — the
# mechanical rails (`mechanical_v0`), a watchlist placeholder, a name we
# have never seen — is a level to watch, not a trigger to take.
_ENTRY_DETECTORS = frozenset({
    "breakout_20d",
    "pullback_support",
    "breakdown_20d",
    "rally_resistance",
})


def base_detector(method: str | None) -> str:
    """The detector name with v2's fusion suffixes stripped.

    ``pullback_support+structure+voices`` → ``pullback_support``. The
    suffixes record which rails structure and trusted voices contributed;
    what was *detected* is the part before the first ``+``.
    """
    return (method or "").split("+", 1)[0]


def has_structural_entry(method: str | None, structural: bool) -> bool:
    """True when a structure detector produced the ENTRY, not merely the
    stop. This is the question `faithful_names` actually asks, and the one
    a caller should ask before pairing `LevelSet.structural` with a setup
    name — the flag alone is True for a mechanical entry with a structural
    stop, and the name for that is ``watchlist_building``.
    """
    return bool(structural) and base_detector(method) in _ENTRY_DETECTORS


def _has(themes: list[str] | None, key: str) -> bool:
    return key in (themes or [])


def _is_hard_asset(ticker: str, asset_class: str, themes: list[str] | None) -> bool:
    """Crypto and the monetary metals — the debasement-trade assets."""
    return (
        asset_class == "crypto"
        or _has(themes, _CRYPTO)
        or ticker in {"GLD", "SLV", "IAU", "PHYS"}
    )


def _leads_benchmark(rs_features: dict | None) -> bool:
    rs = rs_features or {}
    t = rs.get("ticker_pct20d")
    b = rs.get("benchmark_pct20d")
    if t is None or b is None:
        return False
    return (float(t) - float(b)) >= _RS_LEAD_MARGIN


def faithful_names(
    *,
    method: str | None,
    structural: bool,
    side: str,
    ticker: str,
    asset_class: str | None,
    themes: list[str] | None = None,
    rs_features: dict | None = None,
) -> list[str]:
    """Every framework name that honestly describes this setup, most
    specific first. More than one is common: a defended-level entry is a
    pullback AND a support retest."""
    ticker = (ticker or "").upper()
    asset_class = (asset_class or "equity").lower()
    themes = themes or []
    # v2 fuses rails and records it in the name; match on what was detected.
    base = base_detector(method)

    # Cash and bonds are not setups; holding them IS the position.
    if asset_class in {"cash_equivalent", "bond"}:
        return ["cash_preservation"]

    # No entry pattern fired → nothing to enter. This covers missing
    # levels, mechanical rails (with or without a structural stop) and
    # any detector this vocabulary does not know. It has to come before
    # the side branch: a SHORT sitting on a mechanical rail has no
    # breakdown to name, and `failed_breakout_short` would claim one.
    if not has_structural_entry(method, structural):
        return ["watchlist_building"]

    # Short-side structure keeps the risk-off vocabulary regardless of
    # the prevailing regime.
    if side == "SHORT" or base in {"breakdown_20d", "rally_resistance"}:
        return ["failed_breakout_short"]

    if base == "breakout_20d":
        # No chop-vocabulary synonym exists for a breakout, by design.
        if _has(themes, _URANIUM):
            return ["uranium_accumulation", "commodity_breakout", "breakout_continuation"]
        if _has(themes, _PRECIOUS):
            return ["precious_metals_continuation", "commodity_breakout", "breakout_continuation"]
        if _is_hard_asset(ticker, asset_class, themes):
            return ["hard_asset_breakout", "breakout_continuation"]
        if asset_class in _COMMODITY_CLASSES or _has(themes, _ENERGY) or _has(themes, _AGRICULTURE):
            return ["commodity_breakout", "breakout_continuation"]
        return ["breakout_continuation"]

    if base == "pullback_support":
        # Every pullback to a defended level is also a support retest —
        # that synonym is what lets a real setup score in chop.
        tail = ["pullback_to_support", "support_retest"]
        # A miner pulling back while still leading its benchmark is the
        # commodity regime's own setup, not a generic pullback.
        if ticker in _MINERS and _leads_benchmark(rs_features):
            return ["miner_relative_strength", *tail]
        if _has(themes, _URANIUM):
            return ["uranium_accumulation", *tail]
        if _has(themes, _PRECIOUS):
            return ["precious_metals_continuation", *tail]
        if _is_hard_asset(ticker, asset_class, themes):
            return ["scarcity_asset_pullback", *tail]
        if _leads_benchmark(rs_features):
            return ["relative_strength_continuation", *tail]
        return tail

    # Unreachable while `_ENTRY_DETECTORS` and the branches above stay in
    # step; kept so adding a detector to the set without a branch degrades
    # to the honest answer instead of raising.
    return ["watchlist_building"]


def classify_setup_type(
    *,
    method: str | None,
    structural: bool,
    side: str,
    ticker: str,
    asset_class: str | None,
    themes: list[str] | None = None,
    rs_features: dict | None = None,
    regime: str | None = None,
) -> str:
    """One framework name for this setup, preferring the active regime's
    vocabulary among the names that describe it honestly.

    Returns a vocabulary string — never an empty one, so macro alignment
    always has something real to score.
    """
    names = faithful_names(
        method=method,
        structural=structural,
        side=side,
        ticker=ticker,
        asset_class=asset_class,
        themes=themes,
        rs_features=rs_features,
    )
    preferred = PREFERRED_SETUPS.get(regime or "", set())
    for name in names:
        if name in preferred:
            return name
    return names[0]
