"""Symbol mapping — translate our internal tickers to provider-specific symbols.

Our watchlist uses bare tickers (URA, BTC, DXY). Different price providers
need different symbol formats:
  - yfinance:   crypto needs BTC-USD; indices need ^DXY / ^VIX
  - FMP:        equities use bare; crypto uses BTCUSD
  - Finnhub:    equities use bare; crypto uses BINANCE:BTCUSDT

Keep this isolated so swapping providers doesn't ripple into runner/scoring.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache

from macro_positioning.core.settings import settings


logger = logging.getLogger(__name__)


# A plausible US equity/ETF symbol: 1–5 uppercase letters, optional .CLASS
# (BRK.B). Excludes spaces, digits, ':', and over-long verbose labels.
_PLAUSIBLE_EQUITY = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


# Crypto tickers — these need provider-specific suffixes
_CRYPTO_TICKERS = {"BTC", "ETH", "SOL", "DOGE", "ADA", "DOT", "MATIC", "AVAX", "LINK", "LTC", "XRP", "BNB"}

# ── The tradeable crypto universe ───────────────────────────────────────────
# Two sets, because they answer two different questions.
#
#   coinbase_tradeable()  — every coin with a live USD/USDC book on Coinbase
#                           (~400). This is the SCOPE gate: a crypto PAIR
#                           resolves to a priceable key only if its base is
#                           in here. Everything else is a DEX token this desk
#                           does not trade and cannot mark.
#
#   crypto_majors()       — the top-25-ish by cap (~40 once the always-major
#                           list is folded in). These are the only coins
#                           allowed BARE-ticker routing, i.e. a naked "ZEC"
#                           is read as the coin. That restriction matters:
#                           equities are NOT a fixed list here — they are
#                           scored dynamically from whatever the channels say
#                           — so routing all 400 Coinbase bases on bare
#                           tickers would price real stocks as coins the
#                           moment a listing collided (AI, APE, BAND, SKY…).
#
# Both come from config/crypto_universe.json, rebuilt by
# scripts/refresh_crypto_universe.py. The fallback below is what a checkout
# with no config gets: correct, just narrower.
_UNIVERSE_PATH = "config/crypto_universe.json"

_FALLBACK_MAJORS = frozenset({
    "BTC", "ETH", "SOL", "XRP", "DOGE", "AVAX", "LINK", "LTC",
    "AAVE", "FET", "HBAR", "ONDO", "SUI", "TAO", "TRX", "HYPE", "ZEC",
})

_FALLBACK_OVERRIDES = {
    "HYPE": "HYPE32196-USD",
    "SUI": "SUI20947-USD",
    "TAO": "TAO22974-USD",
}


@lru_cache(maxsize=1)
def _universe() -> dict:
    """config/crypto_universe.json, or the fallback if it is not there.

    A missing or broken universe file must narrow the desk's scope, never
    take pricing offline: the fallback is the old hand-maintained list.
    """
    try:
        with open(settings.base_dir / _UNIVERSE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "crypto universe unavailable (%s) — falling back to the built-in "
            "majors list. Run scripts/refresh_crypto_universe.py --write.", exc,
        )
        raw = {}
    majors = frozenset(raw.get("majors") or _FALLBACK_MAJORS)
    return {
        "majors": majors,
        "coinbase": frozenset(raw.get("coinbase") or ()) | majors,
        "yfinance": dict(raw.get("yfinance") or _FALLBACK_OVERRIDES),
        "no_data": dict(raw.get("no_data") or {}),
        "refreshed_at": raw.get("refreshed_at"),
    }


def crypto_majors() -> frozenset[str]:
    """Coins a bare ticker may mean. Short and eyeballed on purpose."""
    return _universe()["majors"]


def coinbase_tradeable() -> frozenset[str]:
    """Every coin with a live Coinbase USD/USDC book. The scope gate."""
    return _universe()["coinbase"]


def crypto_yf_overrides() -> dict[str, str]:
    """Coins Yahoo disambiguates with a CoinMarketCap id. Every entry was
    price-checked against Coinbase's own quote when it was written."""
    return _universe()["yfinance"]


def crypto_without_prices() -> dict[str, str]:
    """Coinbase-tradeable coins yfinance will not price, and when that was
    last checked. A known gap, deliberately not hidden — these still
    resolve, and surface downstream as 'no bars' rather than as 'not a
    real asset', which is a different and much more misleading thing."""
    return _universe()["no_data"]


def book_tradeable(key: str) -> bool:
    """May a paper book actually HOLD this resolved key?

    The line is the VENUE, not the market cap: if it has a Coinbase book
    the desk can buy it, and if it does not, the desk cannot — whatever
    the chart says. That rules out the DEX pairs and the microcaps this
    crowd posts constantly (KINS/SOL, JOTCHUA/USDC, SCHIFFY/GLD), which
    have no venue here and mostly no price feed either. It rules IN the
    Coinbase alt tail — AERO, PENGU, TRUMP, 1INCH — which an earlier
    version of this function excluded as "not a book this desk runs".
    That was a judgement about size; the desk's actual constraint is
    whether the order can be filled.

    Equities are a different venue and unaffected: a listed ticker stays
    tradeable.

    The key form carries the crypto answer, which is why the two forms
    exist: `resolve_symbol` gives a major its bare ticker and every other
    Coinbase coin the `-USD` yfinance form, so a coin can never be
    mistaken for the equity that shares its ticker.
    """
    k = (key or "").upper().strip()
    if not k:
        return False
    base, sep, tail = k.partition("-")
    if sep and tail in _CRYPTO_QUOTE_SUFFIXES:
        # A `-USD` key is crypto by construction; it is holdable exactly
        # when Coinbase lists it.
        return base in coinbase_tradeable()
    return True


def reset_universe_cache() -> None:
    _universe.cache_clear()


# Back-compat: several call sites test membership against this name. It is
# the SCOPE set (majors ∪ Coinbase), snapshotted at import — the same
# lifetime the hand-maintained literal had.
_TRACKED_CRYPTO = coinbase_tradeable()

# Quote/pair suffixes that mark a raw ticker as a crypto pair rather than a
# bare equity symbol. Order matters — strip the longest first.
_CRYPTO_QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "TUSD", "PERP", "USD")

# What may appear on the RIGHT of a slash and still yield a USD-priced key.
# Two different things are excluded here, for the same reason:
#
#   RATIO charts — "AI/NVDA", "AAPLCAT/AAPL". Not an instrument at all;
#   there is no book to buy. Reading the left side as a coin is how "AI"
#   lands in the live feed as a token when the chart was C3.ai vs Nvidia.
#
#   CRYPTO-QUOTED pairs — "ETH/BTC", "BASECAT/WETH", "KINS/SOL". These are
#   real books, but their levels are denominated in the quote coin: an
#   ETH/BTC entry of 0.031 is not comparable to a $3,000 ETH-USD mark. A
#   book that marked one against the other would open a position whose
#   stop sits five orders of magnitude from the price — infinite risk, and
#   the R:R screen would wave it through because the arithmetic is
#   internally consistent. Everything this desk marks is in USD, so only a
#   USD-equivalent quote produces a key.
_PAIR_QUOTES = frozenset({
    "USD", "USDT", "USDC", "BUSD", "TUSD", "DAI", "USDE", "FDUSD", "PYUSD",
    "USDS", "RLUSD", "PERP",
})

# Commodity/FX aliases mapped to a yfinance-priceable symbol. Calls say
# "GOLD"/"XAU"; price them off the continuous futures contract.
_EQUITY_ALIASES = {
    "GOLD": "GC=F", "XAU": "GC=F", "XAUUSD": "GC=F",
    "SILVER": "SI=F", "XAG": "SI=F", "XAGUSD": "SI=F",
}



def _crypto_base(raw: str) -> tuple[str | None, bool]:
    """Parse a raw call ticker into (base, is_crypto_pair).

    Returns (base_coin, True) when the raw ticker is a crypto pair
    (FARTCOIN/USDT, BTCUSD, AVAXUSDT.P → FARTCOIN/BTC/AVAX), or
    (base, False) when it's a bare symbol that should be treated as an
    equity candidate (AAPL, COIN, GOLD, BRK-B).
    """
    t = (raw or "").upper().strip()
    if not t:
        return None, False
    t = t.removesuffix(".P")  # AVAXUSDT.P → AVAXUSDT (perp marker)

    # Explicit '/' is the crypto-pair convention (FARTCOIN/USDT, SOL/USDT) —
    # but ONLY when the right side is something a book is quoted in. A
    # ratio chart (AI/NVDA) has the same shape and is not an instrument.
    if "/" in t:
        head, _, quote = t.partition("/")
        if quote.strip() in _PAIR_QUOTES:
            return (head or None), True
        return None, False

    # '-' / '_' separators: crypto only when the tail is a known quote
    # (BTC-USD) — otherwise it's a hyphenated equity (BRK-B) left bare.
    for sep in ("-", "_"):
        if sep in t:
            head, _, tail = t.partition(sep)
            if tail in _CRYPTO_QUOTE_SUFFIXES:
                return (head or None), True
            return (t, False)

    # No separator: a trailing quote suffix marks a pair (BTCUSD, FETUSD).
    # 'TUSD' can shadow 'FET'+'USD', so collect every candidate and PREFER
    # one that's a tracked coin; fall back to the longest-suffix strip.
    candidates = [
        t[: -len(suf)] for suf in _CRYPTO_QUOTE_SUFFIXES
        if t.endswith(suf) and len(t) > len(suf) + 1
    ]
    for c in candidates:
        if c in _TRACKED_CRYPTO:
            return c, True
    if candidates:
        return min(candidates, key=len), True  # most aggressive strip
    return t, False


def resolve_symbol(raw_ticker: str) -> str | None:
    """Map a raw call ticker to the in-scope BARE key for accuracy scoring.

    This returns the key the `prices` table is stored under (bare ticker for
    crypto, the symbol/alias for equities) — NOT the yfinance fetch symbol.
    Use `to_yfinance_symbol(key)` to get the fetch symbol. Keeping the two
    separate lets storage + lookup agree on one key while fetch translates.

    • Crypto pair → its bare coin ONLY if in the Coinbase-US tracked set
      (BTCUSD→'BTC', HYPEUSDT→'HYPE'); otherwise None (untradeable).
    • Bare symbol → equity candidate, with commodity aliases applied
      (GOLD→'GC=F'); returned as the key for a dynamic yfinance lookup.

    Returns None = `unpriceable`.
    """
    base, is_pair = _crypto_base(raw_ticker)
    if not base:
        return None
    if is_pair:
        # A major keeps its bare key (BTCUSD -> 'BTC'). Any other Coinbase
        # coin is keyed by its yfinance form (AERO/USD -> 'AERO-USD'), the
        # same trick the commodity aliases use (GOLD -> 'GC=F'): the key
        # says what it is, so a coin can never be confused with the equity
        # that shares its ticker. Not on Coinbase at all -> unpriceable.
        if base in crypto_majors():
            return base
        if base in coinbase_tradeable():
            return f"{base}-USD"
        return None
    # Bare symbol — commodity alias, else treat as a dynamic equity ticker.
    alias = _EQUITY_ALIASES.get(base)
    if alias:
        return alias
    # Dominance tickers (BTC.D, ETH.D) read as a valid dot-class equity —
    # the same shape as BRK.B — so they slipped through the gate below and
    # were fetched as stocks. They are TradingView chart labels, not
    # instruments.
    if base.endswith(".D") and base[:-2] in coinbase_tradeable():
        return None
    # Sanity-gate equity candidates so junk extractions (macro labels,
    # crypto-cap indices, verbose "UNKNOWN — likely..." strings, TOTAL2)
    # don't get fetched. A real US equity/ETF symbol is 1–5 letters,
    # optionally with one dot-class (BRK.B). Anything with spaces, digits,
    # ':', or >6 chars is rejected as unpriceable.
    if _PLAUSIBLE_EQUITY.match(base):
        return base
    return None

# Index / FX tickers — these need ^ prefix on yfinance
_YF_INDICES = {
    "DXY": "DX-Y.NYB",   # Dollar index — Yahoo's specific symbol
    "VIX": "^VIX",
    "SPX": "^GSPC",
    "NDX": "^NDX",
    "RUT": "^RUT",
}

# Some tickers we use that need explicit yfinance overrides (data quality / availability)
_YF_OVERRIDES = {
    # Add as needed when yfinance's default doesn't match what we expect
}


def to_yfinance_symbol(ticker: str) -> str:
    """Convert our (bare) ticker to yfinance's symbol format.

    Examples:
      URA  -> URA
      BTC  -> BTC-USD
      HYPE -> HYPE32196-USD   (CMC-id disambiguated)
      DXY  -> DX-Y.NYB
      VIX  -> ^VIX
    """
    t = ticker.upper().strip()
    if t in _YF_OVERRIDES:
        return _YF_OVERRIDES[t]
    if t in _YF_INDICES:
        return _YF_INDICES[t]
    overrides = crypto_yf_overrides()
    # The id overrides are keyed by COIN, and a coin's ticker can belong to a
    # company too — SKY is both a Coinbase listing and Skyline Champion on the
    # NYSE. So a bare ticker only reaches them by being a major; the equity
    # keeps the bare form. (`SKY/USD` still resolves, as the key `SKY-USD`,
    # which the branch below maps to the coin.)
    if t in crypto_majors():
        return overrides.get(t, f"{t}-USD")
    if t in _CRYPTO_TICKERS:
        return f"{t}-USD"
    # A key already in yfinance crypto form — either resolve_symbol's key for
    # a non-major Coinbase coin, or a caller passing the symbol back in. Map
    # it through the id overrides so the round trip is stable.
    if t.endswith("-USD"):
        base = t[: -len("-USD")]
        if base in overrides:
            return overrides[base]
        if base in coinbase_tradeable():
            return t
    return t


def is_crypto(ticker: str) -> bool:
    """Is this KEY a coin?

    Deliberately key-aware rather than name-aware. A bare "AI" is C3.ai the
    company even though `AI` also has a Coinbase book — bare tickers only
    read as crypto for the majors. A non-major coin arrives already keyed
    as `AI-USD`, and that is unambiguous.
    """
    t = ticker.upper().strip()
    if t in _CRYPTO_TICKERS or t in crypto_majors():
        return True
    if t.endswith("-USD"):
        return t[: -len("-USD")] in coinbase_tradeable()
    return False


def is_index(ticker: str) -> bool:
    return ticker.upper().strip() in _YF_INDICES
