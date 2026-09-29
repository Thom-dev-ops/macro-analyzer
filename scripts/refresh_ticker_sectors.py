#!/usr/bin/env python3
"""Rebuild config/ticker_sectors.json — the sector fallback's evidence.

`config/paper_sleeves.json` is an allowlist. It holds the names this desk
had already formed an opinion about: the index ETFs, the megacaps, the
thematic baskets. The copy books do not trade that list — they trade
whatever their channel posts, which is mostly small-cap momentum nobody
had written down. So roughly sixty positions landed in `unclassified`,
not because they were unclassifiable (ABAT is American Battery
Technology; everyone knows what that is) but because nobody had typed
them in yet.

A ticker's own sector is enough to place it. This script asks the price
provider for the sector and industry of every ticker the books have ever
held, plus every name in the two call ledgers, and writes the answer to
`config/ticker_sectors.json`. `paper/sleeves.py` routes anything the
allowlist misses through `$sector_fallback` in the sleeves config.

Checked in on purpose, for the same reason `crypto_universe.json` is: a
classification change should arrive as a reviewable commit, not as a
silent attribution change on a morning tick. It is also why this reads a
file at runtime rather than calling yfinance from the request path — the
performance endpoint resolves a sleeve per position and must not depend
on a network round-trip.

Crypto is deliberately absent: coins are classified from
`config/crypto_universe.json`, which knows majors from the alt tail.

Usage:
  .venv/bin/python scripts/refresh_ticker_sectors.py            # report only
  .venv/bin/python scripts/refresh_ticker_sectors.py --write
  .venv/bin/python scripts/refresh_ticker_sectors.py --write --only ABAT,KEEL
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

warnings.filterwarnings("ignore")

from macro_positioning.core.settings import settings          # noqa: E402
from macro_positioning.paper.sleeves import base_key          # noqa: E402
from macro_positioning.prices.symbol_map import (             # noqa: E402
    coinbase_tradeable,
)

log = logging.getLogger("ticker_sectors")

OUT_PATH = "config/ticker_sectors.json"

# Where a ticker can come from. Every one of these is a name some book
# either holds, held, or could be handed tomorrow.
_SOURCES = (
    ("paper_positions", "SELECT DISTINCT ticker FROM paper_positions"),
    ("assets", "SELECT DISTINCT ticker FROM assets"),
    ("unlocked_calls",
     "SELECT DISTINCT ticker FROM stock_unlocked_calls WHERE instrument <> 'crypto'"),
)


def _candidates(db_path: Path) -> set[str]:
    out: set[str] = set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for label, sql in _SOURCES:
            try:
                rows = [r[0] for r in conn.execute(sql)]
            except sqlite3.Error as exc:
                log.warning("skipping %s: %s", label, exc)
                continue
            log.info("%-16s %4d tickers", label, len(rows))
            out |= {base_key(t) for t in rows if t}
    finally:
        conn.close()
    coins = coinbase_tradeable()
    # A bare major IS the coin here (BTC, ETH, LINK); the universe file
    # classifies those and a yfinance lookup on them is noise.
    return {t for t in out if t and t not in coins}


def _lookup(ticker: str) -> dict | None:
    import yfinance as yf

    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:                                  # noqa: BLE001
        log.warning("%-8s lookup failed: %s", ticker, exc)
        return None
    sector = info.get("sector")
    industry = info.get("industry")
    if not sector and not industry:
        return None
    return {
        "name": info.get("shortName") or info.get("longName") or ticker,
        "sector": sector,
        "industry": industry,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="write config/ticker_sectors.json")
    ap.add_argument("--only", default=None, help="comma-separated tickers, instead of the DB sweep")
    ap.add_argument("--refresh-all", action="store_true",
                    help="re-look-up tickers already in the file (default: only new ones)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    out_file = settings.base_dir / OUT_PATH
    try:
        existing = json.loads(out_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    known: dict[str, dict] = dict(existing.get("tickers") or {})

    if args.only:
        wanted = {base_key(t) for t in args.only.split(",") if t.strip()}
    else:
        wanted = _candidates(settings.sqlite_path)

    todo = sorted(wanted if args.refresh_all else (wanted - set(known)))
    log.info("%d tickers in scope, %d to look up", len(wanted), len(todo))

    found = 0
    for t in todo:
        row = _lookup(t)
        if row is None:
            log.info("%-8s no sector — left out", t)
            continue
        known[t] = row
        found += 1
        log.info("%-8s %-42.42s %s / %s", t, row["name"], row["sector"], row["industry"])

    log.info("resolved %d/%d; file will hold %d tickers", found, len(todo), len(known))
    if not args.write:
        log.info("(dry run — pass --write to save)")
        return 0

    payload = {
        "$schema_version": "1.0",
        "description": (
            "ticker -> {name, sector, industry}. The evidence behind "
            "`$sector_fallback` in config/paper_sleeves.json, which places any "
            "ticker the sleeve allowlist does not name. Data only: the mapping "
            "from industry/sector to sleeve lives in the sleeves config."
        ),
        "$maintenance": "Regenerate with scripts/refresh_ticker_sectors.py --write.",
        "refreshed_at": datetime.now(UTC).isoformat(),
        "counts": {"tickers": len(known)},
        "tickers": dict(sorted(known.items())),
    }
    out_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    log.info("wrote %s", out_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
