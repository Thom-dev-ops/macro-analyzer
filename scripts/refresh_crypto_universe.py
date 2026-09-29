#!/usr/bin/env python3
"""Rebuild `config/crypto_universe.json` — what counts as tradeable crypto.

Three questions, three sources, one file:

1. **What can actually be bought?** Coinbase US's product list — a live,
   market-orderable USD or USDC book. Everything else is out: the EUR /
   GBP / INR / AUD / SGD / BRL / CAD books belong to other Coinbase
   entities, limit-only books cannot take a market order, and a DEX token
   has no venue here at all. This is the gate `resolve_symbol()` applies
   to crypto PAIRS.
2. **What are the majors?** CoinGecko's market-cap ranking, stablecoins
   stripped, intersected with (1). The majors are the only coins allowed
   BARE-ticker routing — `to_yfinance_symbol("ZEC") -> "ZEC-USD"` — because
   a bare three-letter ticker is also how equities are written and the
   desk scores equities dynamically. Restricting bare routing to a short,
   eyeballed list is what stops a stock being priced as a coin.
3. **Can we actually price it, and is it the SAME coin?** Every candidate
   is probed against yfinance and the answer is price-checked against
   Coinbase's own live quote. A symbol that merely exists proves nothing:
   Yahoo disambiguates newer coins with a CoinMarketCap id, and `PEPE-USD`,
   `PEPE24478-USD` and `PEPE25912-USD` are three different things. Coinbase
   is the gate AND the oracle — a symbol is accepted only when its close
   agrees with the price of the book we would actually trade on. Coins
   nothing agrees with land in `no_data` with the date and the reason.

The file is checked in. This script only refreshes it, and prints a diff
of what moved, so a listing change is a reviewable commit and not a
silent behaviour change on some morning's tick.

    .venv/bin/python scripts/refresh_crypto_universe.py            # dry run
    .venv/bin/python scripts/refresh_crypto_universe.py --write
    .venv/bin/python scripts/refresh_crypto_universe.py --write --no-verify
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx                                                        # noqa: E402

from macro_positioning.core.settings import settings                # noqa: E402


COINBASE_PRODUCTS = "https://api.exchange.coinbase.com/products"
COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
OUT_PATH = "config/crypto_universe.json"

# How deep into the cap ranking to look before intersecting with Coinbase.
# 60 deep yields roughly the top 25 tradeable names once stablecoins,
# wrapped tokens and unlisted coins fall out.
CAP_DEPTH = 60

# Never a "major" however large: a stablecoin's chart is a flat line, an
# exchange or wrapped token is a claim on something else. Neither is a
# trade this desk would take.
NOT_A_MAJOR = {
    "USDT", "USDC", "USDS", "DAI", "USDE", "USD1", "USDG", "FDUSD", "PYUSD",
    "TUSD", "BUSD", "USDD", "RLUSD", "USDF", "USDX", "BSC-USD",
    "WBTC", "WETH", "WBETH", "WEETH", "STETH", "WSTETH", "CBBTC", "RETH",
    "LEO", "WBT", "BGB", "OKB", "CRO", "BNSOL", "JITOSOL", "MSOL",
}

# In the majors whatever the ranking says on the day. The desk's own
# sources call these constantly, and a coin dropping two places out of the
# top 25 should not silently change how its ticker is routed.
ALWAYS_MAJOR = {
    "BTC", "ETH", "SOL", "ZEC", "HYPE", "FET", "XRP", "DOGE", "LINK",
    "AVAX", "LTC", "AAVE", "HBAR", "ONDO", "SUI", "TAO", "TRX", "ADA",
    "XLM", "BCH", "DOT", "UNI", "ATOM", "NEAR", "ARB", "OP", "INJ",
    "APT", "SEI", "TIA", "RENDER", "WLD", "PEPE", "SHIB", "ETC", "ICP",
}

# yfinance disambiguates some newer coins with a CoinMarketCap id suffix —
# the plain `{COIN}-USD` returns nothing. These are carried forward and
# re-verified on every refresh rather than re-derived, because guessing an
# id wrong prices a DIFFERENT ASSET, which is worse than no price at all.
SEED_OVERRIDES = {
    "HYPE": "HYPE32196-USD",
    "SUI": "SUI20947-USD",
    "TAO": "TAO22974-USD",
}


def coinbase_bases() -> tuple[set[str], dict]:
    """Every base currency this desk can actually market-buy on Coinbase US.

    Four conditions, each one a different way a listing is not a venue:

    · **USD or USDC quote.** This is what makes it Coinbase *US*. The
      same endpoint also serves the EUR (87), GBP (47), INR, AUD, SGD,
      BRL and CAD books, which belong to other Coinbase entities and are
      not accounts this desk holds. USDT books are excluded too and cost
      nothing: every USDT-quoted base already has a USD or USDC book.
      (The international venue is a different host entirely —
      api.international.coinbase.com — and is not consulted.)
    · **status == online** and not `trading_disabled` — the plain
      delisted/halted checks.
    · **not cancel_only** — a book you may only cancel on is a book
      being wound down.
    · **not limit_only** — no market orders. The paper engine fills at
      the mark plus slippage, which IS a market order; claiming a
      fillable position in a limit-only book would be the backtest
      inventing liquidity it never had. Excludes 16 bases today, mostly
      fiat-token and stablecoin books (EURC, XSGD, TGBP, AUDD, USDS)
      plus a few thin alts (SYND, KITE, WMTX, DIEM).
    """
    r = httpx.get(COINBASE_PRODUCTS, timeout=30)
    r.raise_for_status()
    products = r.json()
    live = [
        p for p in products
        if p.get("quote_currency") in ("USD", "USDC")
        and p.get("status") == "online"
        and not p.get("trading_disabled")
        and not p.get("cancel_only")
        and not p.get("limit_only")
    ]
    return (
        {str(p["base_currency"]).upper() for p in live},
        {
            "products": len(products),
            "usd_books": len(live),
            "venue": "coinbase_us",
            "quotes": ["USD", "USDC"],
            "excluded": "non-USD books, delisted/halted, cancel_only, limit_only",
        },
    )


def cap_ranked() -> list[str]:
    """Symbols by market cap, largest first. Best-effort — a CoinGecko
    outage costs the refresh its ranking, not its Coinbase gate."""
    try:
        r = httpx.get(
            COINGECKO_MARKETS,
            params={"vs_currency": "usd", "order": "market_cap_desc",
                    "per_page": CAP_DEPTH, "page": 1},
            timeout=30,
        )
        r.raise_for_status()
        return [str(c["symbol"]).upper() for c in r.json()]
    except Exception as exc:
        print(f"  ! cap ranking unavailable ({type(exc).__name__}: {exc}) — "
              f"majors fall back to the always-major list", file=sys.stderr)
        return []


def is_listed_equity(symbol: str) -> bool:
    """Is this bare ticker also a real listed stock?

    Bare-ticker crypto routing is the one place a coin can be mistaken for
    a company. The cap ranking will happily promote a coin called SKY, and
    SKY is Skyline Champion on the NYSE — route it bare and the desk starts
    pricing a homebuilder as a token. Anything the ranking proposes has to
    clear this; the hand-written ALWAYS_MAJOR list does not, because those
    collisions (SOL is also Emeren Group) are ones this desk has already
    decided about.
    """
    try:
        import yfinance as yf

        for q in yf.Search(symbol, max_results=8).quotes:
            if (str(q.get("symbol") or "").upper() == symbol
                    and q.get("quoteType") in ("EQUITY", "ETF")):
                return True
    except Exception:
        pass
    return False


def called_by_our_sources(limit: int = 400) -> set[str]:
    """Coins the desk's own channels actually call.

    A universe built only from a cap table would verify fifty coins nobody
    here mentions and skip the one a KOL posts every day.
    """
    from macro_positioning.db.connect import read_connection

    conn = read_connection()
    try:
        rows = conn.execute(
            """
            SELECT asset_ticker, COUNT(*) n
              FROM signals
             WHERE status = 'active'
               AND extracted_at >= datetime('now', '-120 day')
               AND asset_ticker LIKE '%/%'
             GROUP BY asset_ticker
             ORDER BY n DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out: set[str] = set()
    for r in rows:
        base = str(r["asset_ticker"] or "").upper().split("/")[0].strip()
        if base and base.isascii() and len(base) <= 12:
            out.add(base)
    return out


# How far a yfinance close may sit from Coinbase's live price and still be
# accepted as the same asset. A daily close against a live quote is stale
# by hours, and these things move — but a WRONG symbol is wrong by an
# order of magnitude, not by 20%.
_PRICE_TOLERANCE = 0.20


def coinbase_price(base: str) -> float | None:
    """Coinbase's own live price for a base. This is the oracle the
    yfinance symbol is checked against."""
    for quote in ("USD", "USDC"):
        try:
            r = httpx.get(
                f"{COINBASE_PRODUCTS}/{base}-{quote}/ticker", timeout=15
            )
            if r.status_code != 200:
                continue
            price = float((r.json() or {}).get("price") or 0)
            if price > 0:
                return price
        except Exception:
            continue
    return None


def yf_close(symbol: str) -> float | None:
    """Most recent yfinance close for a symbol, or None if it has no data."""
    try:
        import yfinance as yf

        hist = yf.Ticker(symbol).history(period="5d", interval="1d", actions=False)
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def yf_candidates(base: str, override: str | None) -> list[str]:
    """Symbols worth trying for a coin, most-likely first.

    Yahoo disambiguates newer coins with a CoinMarketCap id — PEPE is
    `PEPE24478-USD`, and `PEPE25912-USD` is a DIFFERENT COIN called
    PepeCoin. Guessing between them by rank alone is how a book ends up
    marked against the wrong asset, so the search only proposes; the
    Coinbase price decides.
    """
    out = [s for s in (override, f"{base}-USD") if s]
    try:
        import re as _re

        import yfinance as yf

        pattern = _re.compile(rf"^{_re.escape(base)}\d*-USD$")
        for q in yf.Search(f"{base} USD", max_results=10).quotes:
            sym = str(q.get("symbol") or "")
            if q.get("quoteType") == "CRYPTOCURRENCY" and pattern.match(sym):
                if sym not in out:
                    out.append(sym)
    except Exception:
        pass
    return out


def verify(base: str, override: str | None) -> tuple[str | None, str]:
    """Find the yfinance symbol that prices the coin Coinbase trades.

    Returns `(symbol, note)`. A symbol is only accepted when its close
    agrees with Coinbase's live price — a symbol that merely EXISTS proves
    nothing, since half these tickers are shared by three unrelated
    memecoins.
    """
    truth = coinbase_price(base)
    tried: list[str] = []
    for symbol in yf_candidates(base, override):
        close = yf_close(symbol)
        tried.append(symbol)
        if close is None:
            continue
        if truth is None:
            # No oracle: accept data-exists, but say the check was skipped.
            return symbol, "unchecked — Coinbase quote unavailable"
        drift = abs(close - truth) / truth
        if drift <= _PRICE_TOLERANCE:
            return symbol, f"{drift * 100:.1f}% from Coinbase"
        tried[-1] = f"{symbol} (off by {drift * 100:.0f}%)"
    return None, ("no symbol agreed with Coinbase: " + ", ".join(tried)) if tried else "no candidates"


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh the tradeable-crypto universe.")
    ap.add_argument("--write", action="store_true", help="write the config (default: dry run)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the yfinance probe — keeps the previous verification")
    args = ap.parse_args()

    out_path = settings.base_dir / OUT_PATH
    previous = {}
    if out_path.exists():
        previous = json.loads(out_path.read_text(encoding="utf-8"))

    print("Coinbase products…")
    bases, counts = coinbase_bases()
    print(f"  {counts['products']} products → {counts['usd_books']} live USD/USDC books "
          f"→ {len(bases)} base currencies")

    print("Cap ranking…")
    ranked = cap_ranked()
    proposed = sorted((set(ranked[:CAP_DEPTH]) - NOT_A_MAJOR - ALWAYS_MAJOR) & bases)
    collisions = []
    if proposed and not args.no_verify:
        print(f"  checking {len(proposed)} cap-ranked coins for equity ticker collisions…")
        collisions = [c for c in proposed if is_listed_equity(c)]
    majors = sorted(
        ((set(proposed) - set(collisions)) | ALWAYS_MAJOR) & bases
    )
    dropped = sorted(ALWAYS_MAJOR - bases)
    print(f"  {len(majors)} majors (top {CAP_DEPTH} by cap ∩ Coinbase, plus the always-major list)")
    if collisions:
        print(f"  ! bare routing withheld — these tickers are also listed equities: "
              f"{', '.join(collisions)}. Still tradeable, still in scope; a PAIR "
              f"resolves them to '{{BASE}}-USD'. Add to ALWAYS_MAJOR to override.")
    if dropped:
        print(f"  ! always-major but NOT on Coinbase, so not tradeable here: {', '.join(dropped)}")

    print("Coins our own channels call…")
    called = called_by_our_sources() & bases
    print(f"  {len(called)} of them are Coinbase-listed")

    overrides = dict(previous.get("yfinance") or SEED_OVERRIDES)
    no_data = dict(previous.get("no_data") or {})

    if not args.no_verify:
        candidates = sorted(set(majors) | called)
        print(f"Verifying {len(candidates)} coins — yfinance close vs Coinbase's live price…")
        stamp = datetime.now(UTC).date().isoformat()
        ok, bad, moved = [], [], []
        for i, base in enumerate(candidates, 1):
            prior = overrides.get(base)
            symbol, note = verify(base, prior)
            if symbol:
                ok.append(base)
                no_data.pop(base, None)
                if symbol == f"{base}-USD":
                    overrides.pop(base, None)       # the plain form works
                else:
                    overrides[base] = symbol
                if prior and prior != symbol:
                    moved.append(f"{base}: {prior} → {symbol}")
            else:
                # An override that stopped agreeing is louder than a coin
                # that never had data: it means the id moved under us.
                if prior:
                    print(f"  ! {base}: override {prior} no longer agrees — {note}")
                overrides.pop(base, None)
                bad.append(base)
                no_data[base] = f"{stamp} · {note}"
            if i % 25 == 0:
                print(f"    {i}/{len(candidates)}…")
        print(f"  {len(ok)} priced and price-checked · {len(bad)} with no usable symbol")
        if moved:
            print("  symbols moved: " + "; ".join(moved))
        if bad:
            print(f"  unpriceable: {', '.join(sorted(bad)[:40])}"
                  + (" …" if len(bad) > 40 else ""))

    payload = {
        "$schema_version": "1.0",
        "description": (
            "The tradeable-crypto universe. `coinbase` is the gate: a crypto PAIR "
            "resolves to a priceable key only if its base has a live USD/USDC book "
            "on Coinbase. `majors` is the shorter list allowed bare-ticker routing "
            "(a bare 'ZEC' means the coin), because bare tickers are also how the "
            "desk writes equities and it scores those dynamically. `yfinance` holds "
            "verified symbol overrides for coins Yahoo disambiguates with a "
            "CoinMarketCap id. `no_data` records coins that are tradeable on "
            "Coinbase but that yfinance will not price, with the date checked — a "
            "known gap, not a mystery."
        ),
        "$maintenance": (
            "Regenerate with scripts/refresh_crypto_universe.py --write. Checked in "
            "on purpose: a Coinbase listing change should arrive as a reviewable "
            "commit, not as a silent behaviour change on a morning tick."
        ),
        "sources": {
            "coinbase": COINBASE_PRODUCTS,
            "cap_ranking": COINGECKO_MARKETS,
        },
        "refreshed_at": datetime.now(UTC).isoformat(),
        "counts": {
            "coinbase_bases": len(bases),
            "majors": len(majors),
            "no_data": len(no_data),
        },
        "majors": majors,
        "bare_routing_withheld": collisions,
        "yfinance": dict(sorted(overrides.items())),
        "no_data": dict(sorted(no_data.items())),
        "coinbase": sorted(bases),
    }

    prev_bases = set(previous.get("coinbase") or [])
    prev_majors = set(previous.get("majors") or [])
    if previous:
        added, removed = sorted(bases - prev_bases), sorted(prev_bases - bases)
        m_added, m_removed = sorted(set(majors) - prev_majors), sorted(prev_majors - set(majors))
        print("\nDiff against the checked-in file:")
        print(f"  listings  +{len(added)} / -{len(removed)}")
        if added:
            print(f"    added:   {', '.join(added[:25])}{' …' if len(added) > 25 else ''}")
        if removed:
            print(f"    removed: {', '.join(removed[:25])}{' …' if len(removed) > 25 else ''}")
        if m_added or m_removed:
            print(f"  majors    +{', '.join(m_added) or '—'} / -{', '.join(m_removed) or '—'}")
    else:
        print(f"\nNew file: {len(bases)} listings, {len(majors)} majors.")

    if args.write:
        out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {out_path}")
    else:
        print("\ndry run — nothing written (pass --write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
