"""Tests for the tradeable-crypto universe and how tickers route through it.

The rule this file exists to protect: **a coin and a company can share a
ticker, and the desk must never confuse them.** Equities here are not a
fixed list — they are scored dynamically from whatever the channels say —
so the moment a bare ticker is allowed to mean a coin, every listing
collision (SKY is Skyline Champion, AI is C3.ai) becomes a stock priced as
a token. The split between `crypto_majors()` (bare routing allowed) and
`coinbase_tradeable()` (scope only) is what keeps that from happening, and
most of what follows is that split under pressure.

Assertions about the SHIPPED config are structural — "every major is
Coinbase-listed", "every override names a coin we track" — never "this
coin is major", because Coinbase's listings change and a listing change
should be a config refresh, not a red test.
"""

from __future__ import annotations

import json

import pytest

from macro_positioning.prices import symbol_map as sm


@pytest.fixture(autouse=True)
def _clean_cache():
    sm.reset_universe_cache()
    yield
    sm.reset_universe_cache()


# ── Routing ───────────────────────────────────────────────────────────


def test_a_major_pair_resolves_to_the_bare_coin():
    assert sm.resolve_symbol("BTC/USDT") == "BTC"
    assert sm.resolve_symbol("ZECUSDT") == "ZEC"
    assert sm.resolve_symbol("HYPE/USD") == "HYPE"
    assert sm.resolve_symbol("FET-USD") == "FET"


def test_a_non_major_coinbase_pair_is_keyed_so_it_cannot_be_read_as_a_stock():
    """AERO/USD does not become the key 'AERO' — that string is also how an
    equity would be written. It becomes 'AERO-USD', which is not."""
    coin = next(iter(sorted(sm.coinbase_tradeable() - sm.crypto_majors())))
    assert sm.resolve_symbol(f"{coin}/USD") == f"{coin}-USD"


def test_the_keyed_form_round_trips():
    """A key fed back through resolution must come out unchanged, or a
    second pass over stored data would quietly reclassify it."""
    coin = next(iter(sorted(sm.coinbase_tradeable() - sm.crypto_majors())))
    key = sm.resolve_symbol(f"{coin}/USD")
    assert sm.resolve_symbol(key) == key
    assert sm.is_crypto(key)


def test_a_dex_token_is_unpriceable():
    """The long tail of the KOL feed. No Coinbase book, no mark, no trade."""
    for raw in ("KINS/SOL", "JOTCHUA/USDC", "BULLSHIT/SOL", "牛来/USDT"):
        assert sm.resolve_symbol(raw) is None


def test_a_ticker_that_is_both_a_coin_and_a_stock_stays_a_stock_when_bare(monkeypatch, tmp_path):
    """The whole reason majors is a short list.

    SKY has a Coinbase book AND is Skyline Champion on the NYSE. Bare, it
    is the company. As a pair, it is the coin.
    """
    _write_universe(monkeypatch, tmp_path, majors=["BTC"], coinbase=["BTC", "SKY"],
                    yfinance={"SKY": "SKY33038-USD"})
    assert sm.resolve_symbol("SKY") == "SKY"
    assert sm.to_yfinance_symbol("SKY") == "SKY"
    assert sm.is_crypto("SKY") is False

    assert sm.resolve_symbol("SKY/USD") == "SKY-USD"
    assert sm.to_yfinance_symbol("SKY-USD") == "SKY33038-USD"
    assert sm.is_crypto("SKY-USD") is True


def test_a_ratio_chart_is_not_an_instrument():
    """The channels post relative-strength charts with the same shape as a
    pair. There is no book to buy, and reading the left side as a coin put
    C3.ai in the live feed as a token."""
    for raw in ("AI/NVDA", "AAPLCAT/AAPL", "MSTR/BTC"):
        assert sm.resolve_symbol(raw) is None


def test_a_crypto_quoted_pair_yields_no_usd_key():
    """ETH/BTC is a real book, but its levels are denominated in BTC. Mark
    an 0.031 entry against a $3,000 ETH-USD price and the position opens
    with a stop five orders of magnitude away — infinite risk that the R:R
    screen waves through, because the arithmetic is internally consistent.
    Everything here marks in USD; only a USD-equivalent quote resolves."""
    assert sm.resolve_symbol("ETH/BTC") is None
    assert sm.resolve_symbol("BASECAT/WETH") is None
    assert sm.resolve_symbol("KINS/SOL") is None
    # The same coin, quoted in dollars, is fine.
    assert sm.resolve_symbol("ETH/USDT") == "ETH"


def test_equity_routing_is_untouched():
    assert sm.resolve_symbol("AAPL") == "AAPL"
    assert sm.resolve_symbol("BRK.B") == "BRK.B"
    assert sm.resolve_symbol("GOLD") == "GC=F"
    assert sm.to_yfinance_symbol("NVDA") == "NVDA"
    assert sm.to_yfinance_symbol("VIX") == "^VIX"
    assert sm.to_yfinance_symbol("DXY") == "DX-Y.NYB"


def test_junk_extractions_are_still_rejected():
    for raw in ("UNKNOWN — likely a chart of BTC", "TOTAL2", "BTC.D", ""):
        assert sm.resolve_symbol(raw) is None


# ── The shipped config ────────────────────────────────────────────────


def _shipped() -> dict:
    from macro_positioning.core.settings import settings

    with open(settings.base_dir / sm._UNIVERSE_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_the_named_coins_are_tracked():
    """The desk asked for these by name. BTC, ETH, SOL, ZEC, HYPE, FET."""
    majors = sm.crypto_majors()
    for coin in ("BTC", "ETH", "SOL", "ZEC", "HYPE", "FET"):
        assert coin in majors, f"{coin} lost its bare-ticker routing"
        assert sm.to_yfinance_symbol(coin).endswith("-USD")


def test_every_major_is_actually_tradeable():
    """A major that Coinbase does not list is a coin the desk cannot buy —
    which is how TRX sat in the tracked set while being untradeable here."""
    assert sm.crypto_majors() <= sm.coinbase_tradeable()


def test_every_override_names_a_coin_in_scope():
    """An override for a coin nothing resolves to is dead config, and a
    stale one prices the wrong asset."""
    for base, symbol in sm.crypto_yf_overrides().items():
        assert base in sm.coinbase_tradeable(), f"override for untracked {base}"
        assert symbol.endswith("-USD"), f"{base} override {symbol} is not a yfinance crypto symbol"
        assert symbol.startswith(base), f"{base} override {symbol} names a different coin"


def test_unpriceable_coins_are_recorded_with_a_date():
    """`no_data` is a known gap, not a mystery. Each entry says when it was
    checked so a stale claim can be spotted and re-run."""
    for base, note in sm.crypto_without_prices().items():
        assert base in sm.coinbase_tradeable()
        assert note[:4].isdigit(), f"{base}: {note!r} does not start with a date"


def test_a_coin_with_no_price_source_still_resolves():
    """It is tradeable on Coinbase — the desk simply has no bars for it.
    Resolving it to None would report 'not a real asset', which is a
    different and much more misleading statement than 'no price data'."""
    gaps = sm.crypto_without_prices()
    if not gaps:
        pytest.skip("nothing in the no-data list right now")
    base = next(iter(gaps))
    assert sm.resolve_symbol(f"{base}/USD") is not None


def test_the_config_is_the_source_of_the_universe():
    shipped = _shipped()
    assert set(shipped["majors"]) == set(sm.crypto_majors())
    assert set(shipped["coinbase"]) <= set(sm.coinbase_tradeable())
    assert shipped["refreshed_at"]


# ── Degradation ───────────────────────────────────────────────────────


def _write_universe(monkeypatch, tmp_path, **payload):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "crypto_universe.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    from macro_positioning.core.settings import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    sm.reset_universe_cache()


def test_a_missing_config_narrows_the_desk_instead_of_breaking_it(monkeypatch, tmp_path):
    """Pricing must not go offline because a config file is absent on a
    fresh checkout — it falls back to the hand-maintained majors."""
    from macro_positioning.core.settings import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path)   # no config/ at all
    sm.reset_universe_cache()

    assert "BTC" in sm.crypto_majors()
    assert "ZEC" in sm.crypto_majors()
    assert sm.to_yfinance_symbol("BTC") == "BTC-USD"
    assert sm.resolve_symbol("BTC/USDT") == "BTC"
    # And with no Coinbase list, an unknown coin is out of scope rather
    # than silently admitted.
    assert sm.resolve_symbol("AERO/USD") is None


def test_a_corrupt_config_falls_back_rather_than_raising(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "crypto_universe.json").write_text("{not json", encoding="utf-8")
    from macro_positioning.core.settings import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    sm.reset_universe_cache()
    assert "ETH" in sm.crypto_majors()
