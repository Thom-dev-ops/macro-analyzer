"""Tests for paper-book attribution: sleeves, classes, regimes, and the stats.

The engine tests prove the book obeys its mandate. These prove the
*reporting* is honest — that a win is only counted once it is closed,
that realized and unrealized never get quietly added together into one
flattering number, and that a ticker nobody has classified shows up as
unclassified instead of being folded into whatever sleeve is nearest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from macro_positioning.db.schema import initialize_database
from macro_positioning.paper import store
from macro_positioning.paper.models import Mandate
from macro_positioning.paper.engine import run_tick
from macro_positioning.paper.performance import build_trade_records, performance, summarize
from macro_positioning.paper.sleeves import base_key, sleeve_for_ticker, coverage
from macro_positioning.prices.symbol_map import book_tradeable

from test_paper_engine import NOW, marks, read


@pytest.fixture
def book(tmp_path: Path):
    db = tmp_path / "paper.db"
    initialize_database(db)
    return db, store.create_portfolio(db_path=db)


# ── Taxonomy ──────────────────────────────────────────────────────────


def test_the_desks_own_vocabulary_resolves():
    cases = {
        "CORN": ("agriculture", "hard_assets"),
        "MOO": ("agriculture", "hard_assets"),
        "GDXJ": ("precious_metals", "hard_assets"),
        "PALAF": ("uranium", "hard_assets"),
        "FCX": ("industrial_metals", "hard_assets"),
        "XLB": ("industrial_metals", "hard_assets"),
        "NVDA": ("technology_ai", "equities"),
        "LMT": ("defense", "equities"),
        "SOL": ("crypto", "crypto"),
        "COIN": ("crypto", "crypto"),
        "TLT": ("rates", "rates_fx"),
        "XLV": ("healthcare", "equities"),
    }
    for ticker, (sleeve_id, class_id) in cases.items():
        s = sleeve_for_ticker(ticker)
        assert s.id == sleeve_id, f"{ticker} → {s.id}, expected {sleeve_id}"
        assert s.class_id == class_id


def test_a_crypto_pair_resolves_on_its_base_asset():
    # The KOL layer emits "SOL/USD" and "BTC/USDT"; attribution must not
    # lose those to `unclassified`.
    assert sleeve_for_ticker("SOL/USD").id == "crypto"
    assert sleeve_for_ticker("BTC/USDT").id == "crypto"


def test_a_resolved_alt_coin_key_resolves_on_its_base_too():
    # `resolve_symbol` keys a non-major Coinbase coin by its yfinance
    # form so it can never be confused with the equity of the same name.
    # Attribution has to see through that, or every alt the copy book
    # ever held reads as unclassified.
    assert base_key("AERO-USD") == "AERO"
    assert base_key("BRK-B") == "BRK-B"       # a hyphenated equity, not a pair
    assert sleeve_for_ticker("AERO-USD").id == "crypto_alts"
    assert sleeve_for_ticker("PENGU-USD").class_id == "crypto"


def test_majors_and_alts_are_separate_sleeves():
    # Both are crypto, and reading the alt tail's P&L as ordinary crypto
    # beta would flatter the majors. They roll up to one class, not one
    # sleeve.
    major, alt = sleeve_for_ticker("LINK"), sleeve_for_ticker("1INCH-USD")
    assert (major.id, alt.id) == ("crypto", "crypto_alts")
    assert major.class_id == alt.class_id == "crypto"


def test_the_books_hold_anything_with_a_venue_and_nothing_without():
    """The line is the VENUE, not the market cap.

    This used to assert the opposite for the alt tail — PENGU and AERO
    were excluded as "not a book this desk runs", which was a judgement
    about size. The desk's real constraint is whether an order can be
    filled: a Coinbase book means yes, and the tail is in. What stays out
    is what has no venue at all — the DEX pairs this crowd posts by the
    hundred, which also have no price feed to score against.
    """
    assert book_tradeable("BTC") and book_tradeable("LINK")
    assert book_tradeable("NVDA") and book_tradeable("BRK.B")
    # On Coinbase -> holdable, however small.
    assert book_tradeable("PENGU-USD")
    assert book_tradeable("AERO-USD")
    assert book_tradeable("1INCH-USD")
    # No Coinbase book -> the desk cannot fill it, whatever the chart says.
    assert not book_tradeable("KINS-USD")
    assert not book_tradeable("JOTCHUA-USD")
    # And a DEX pair never resolves to a key in the first place.
    from macro_positioning.prices.symbol_map import resolve_symbol
    assert resolve_symbol("KINS/SOL") is None
    assert resolve_symbol("SCHIFFY/GLD") is None


def test_a_ticker_nobody_declared_is_placed_by_its_sector():
    # The allowlist never named these — they are small-cap momentum the
    # copy book bought because its channel called them. Their own sector
    # is enough to place them, and before the fallback existed every one
    # of them sat in `unclassified`.
    assert sleeve_for_ticker("ABAT").id == "industrial_metals"   # explicit override
    assert sleeve_for_ticker("AAOI").id == "technology_ai"       # Technology
    assert sleeve_for_ticker("ABEO").id == "healthcare"          # Healthcare
    assert sleeve_for_ticker("ULCC").id == "industrials"         # Industrials / Airlines
    assert sleeve_for_ticker("SATL").id == "defense"             # Aerospace & Defense


def test_the_listed_crypto_proxies_beat_their_screener_sector():
    # A screener files the miners under Capital Markets. They are a
    # levered bet on the coin, and the declared membership says so.
    assert sleeve_for_ticker("IREN").id == "crypto"
    assert sleeve_for_ticker("WULF").id == "crypto"


def test_declaration_order_settles_a_ticker_claimed_twice():
    # QQQ is in the technology_ai theme AND the tech_indices bucket; the
    # first sleeve declared wins, and it must be deterministic.
    assert sleeve_for_ticker("QQQ").id == "technology_ai"


def test_an_unmapped_ticker_says_so_rather_than_guessing():
    s = sleeve_for_ticker("ZZQQ")
    assert s.id == "unclassified"
    assert s.class_id == "other"
    c = coverage(["SOL", "CORN", "ZZQQ"])
    assert c["unmapped"] == ["ZZQQ"]
    assert c["classified"] == 2


def test_sleeves_carry_the_regime_they_express():
    assert "commodity_led_inflation" in sleeve_for_ticker("CORN").regimes
    assert "risk_on_expansion" in sleeve_for_ticker("NVDA").regimes


# ── Stats ─────────────────────────────────────────────────────────────


def _open_and_close(db, pf, ticker, *, entry, exit_price, rank=80, at=NOW):
    """Open a position at `entry`, then force a full close at `exit_price`.

    The target is put out of reach on purpose: a price that touches the
    target trims half and leaves the position open, which is correct
    behaviour but the wrong shape for a round-trip fixture. A rank
    collapse is the deterministic full-exit path — after the minimum hold
    and persisting for the confirmation window, since thesis exits no
    longer act on one tick.
    """
    far_target = entry * 100
    run_tick(
        pf.portfolio_id, db_path=db, now=at, price_fn=marks(**{ticker: entry}),
        candidates=[read(ticker, rank, entry=entry, stop=entry * 0.96,
                         target=far_target, scored_at=at.isoformat())],
    )
    pos = next(x for x in store.load_positions(pf.portfolio_id, db_path=db)
               if x.ticker == ticker.upper())
    n = pos.confirm_ticks or Mandate().thesis_exit_confirm_ticks
    hold = pos.min_hold_days if pos.min_hold_days is not None else Mandate().min_hold_days
    start = at + timedelta(days=hold + 1)
    for i in range(n + 6):        # +6: ladder rungs may consume ticks first
        if not any(x.ticker == ticker.upper()
                   for x in store.load_positions(pf.portfolio_id, db_path=db)):
            break
        t = start + timedelta(hours=12 * i)
        run_tick(
            pf.portfolio_id, db_path=db, now=t, price_fn=marks(**{ticker: exit_price}),
            candidates=[read(ticker, 10, entry=exit_price, stop=exit_price * 0.96,
                             target=far_target, scored_at=t.isoformat())],
        )
    assert not [x for x in store.load_positions(pf.portfolio_id, db_path=db)
                if x.ticker == ticker.upper()], f"{ticker} should have closed"


def test_a_winner_and_a_loser_produce_the_right_win_rate(book):
    db, pf = book
    _open_and_close(db, pf, "CORN", entry=100.0, exit_price=120.0)          # win
    _open_and_close(db, pf, "GDXJ", entry=100.0, exit_price=95.0, at=NOW)   # loss

    r = performance(pf.portfolio_id, db_path=db, marks={})
    o = r["overall"]
    assert o["trades"] == 2
    assert o["wins"] == 1 and o["losses"] == 1
    assert o["winRate"] == 50.0
    assert o["grossProfit"] > 0 and o["grossLoss"] > 0
    assert o["profitFactor"] == pytest.approx(o["grossProfit"] / o["grossLoss"], abs=0.01)
    assert o["avgWinPct"] > 0 > o["avgLossPct"]


def test_an_open_position_is_not_counted_as_a_win(book):
    db, pf = book
    run_tick(pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(CORN=100.0),
             candidates=[read("CORN", 80)])
    # Up 30% on paper — and still worth nothing to the win rate.
    r = performance(pf.portfolio_id, db_path=db, marks={"CORN": {"price": 130.0}})
    o = r["overall"]
    assert o["trades"] == 0
    assert o["winRate"] is None
    assert o["openPositions"] == 1
    assert o["unrealized"] > 0
    assert o["realized"] == 0


def test_realized_and_unrealized_are_reported_separately(book):
    db, pf = book
    _open_and_close(db, pf, "GDXJ", entry=100.0, exit_price=90.0)     # booked loss
    run_tick(pf.portfolio_id, db_path=db, now=NOW + timedelta(days=3),
             price_fn=marks(SOL=100.0),
             candidates=[read("SOL", 90, scored_at=(NOW + timedelta(days=3)).isoformat())])

    r = performance(pf.portfolio_id, db_path=db, marks={"SOL": {"price": 200.0}})
    h = r["headline"]
    assert h["realizedPct"] < 0 < h["unrealizedPct"]
    # Signs disagree, so "share of gains banked" is meaningless and must
    # not be rendered as a negative percentage.
    assert h["bookedShare"] is None
    assert h["bookedShareNote"]


def test_booked_share_reports_when_both_point_the_same_way(book):
    db, pf = book
    _open_and_close(db, pf, "CORN", entry=100.0, exit_price=140.0)    # booked gain
    run_tick(pf.portfolio_id, db_path=db, now=NOW + timedelta(days=3),
             price_fn=marks(SOL=100.0),
             candidates=[read("SOL", 90, scored_at=(NOW + timedelta(days=3)).isoformat())])
    r = performance(pf.portfolio_id, db_path=db, marks={"SOL": {"price": 150.0}})
    h = r["headline"]
    assert h["realizedPct"] > 0 and h["unrealizedPct"] > 0
    assert 0 < h["bookedShare"] < 100


# ── Attribution ───────────────────────────────────────────────────────


def test_pnl_lands_in_the_right_sleeve_and_class(book):
    db, pf = book
    _open_and_close(db, pf, "CORN", entry=100.0, exit_price=130.0)    # agriculture, win
    _open_and_close(db, pf, "LMT", entry=100.0, exit_price=80.0)      # defense, loss

    r = performance(pf.portfolio_id, db_path=db, marks={})
    sleeves = {s["id"]: s for s in r["bySleeve"]}
    assert sleeves["agriculture"]["realized"] > 0
    assert sleeves["defense"]["realized"] < 0
    assert sleeves["agriculture"]["tickers"] == ["CORN"]

    classes_ = {c["id"]: c for c in r["byClass"]}
    assert classes_["hard_assets"]["realized"] > 0
    assert classes_["equities"]["realized"] < 0

    # Class totals must be a genuine partition of the book.
    assert sum(c["realized"] for c in r["byClass"]) == pytest.approx(
        r["overall"]["realized"], abs=0.01
    )


def test_regime_rows_overlap_and_say_so(book):
    db, pf = book
    _open_and_close(db, pf, "CORN", entry=100.0, exit_price=130.0)
    r = performance(pf.portfolio_id, db_path=db, marks={})
    assert r["regimeNote"]
    regimes = {x["id"] for x in r["byRegime"]}
    assert "commodity_led_inflation" in regimes


def test_exit_reasons_are_broken_out(book):
    db, pf = book
    # Stop out one name; let another die of rank decay.
    run_tick(pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(CORN=100.0, GDXJ=100.0),
             candidates=[read("CORN", 80), read("GDXJ", 80)])
    t1 = NOW + timedelta(days=1)
    run_tick(pf.portfolio_id, db_path=db, now=t1, price_fn=marks(CORN=85.0, GDXJ=101.0),
             candidates=[read("CORN", 80, entry=85.0, scored_at=t1.isoformat()),
                         read("GDXJ", 80, entry=101.0, scored_at=t1.isoformat())])
    # Rank decay waits for the position's minimum hold and confirmation window.
    gd = next(x for x in store.load_positions(pf.portfolio_id, db_path=db) if x.ticker == "GDXJ")
    start = NOW + timedelta(days=(gd.min_hold_days or 0) + 1)
    for i in range(gd.confirm_ticks or Mandate().thesis_exit_confirm_ticks):
        t = start + timedelta(hours=12 * i)
        run_tick(pf.portfolio_id, db_path=db, now=t, price_fn=marks(GDXJ=101.0),
                 candidates=[read("GDXJ", 2, entry=101.0, scored_at=t.isoformat())])
    r = performance(pf.portfolio_id, db_path=db, marks={})
    reasons = {x["id"]: x for x in r["byExitReason"]}
    assert "stop_hit" in reasons and "rank_decay" in reasons
    assert reasons["stop_hit"]["trades"] == 1
    assert reasons["rank_decay"]["trades"] == 1


def test_returns_are_measured_against_capital_committed(book):
    db, pf = book
    _open_and_close(db, pf, "CORN", entry=100.0, exit_price=150.0)
    (rec,) = [r for r in build_trade_records(pf.portfolio_id, db_path=db) if r.is_closed]
    # +50% on the money at risk, not a fraction of a percent of book equity.
    assert 45 < rec.realized_pct < 50
    # Closed after the position's minimum hold plus its confirmation window.
    assert rec.hold_days > 1.0
    assert rec.hold_days == pytest.approx(rec.hold_days, abs=0.01)
    assert rec.r_multiple is not None and rec.r_multiple > 3


def test_summarize_on_an_empty_book_is_all_zeros_not_a_crash():
    s = summarize([], equity=50_000)
    assert s["trades"] == 0
    assert s["winRate"] is None
    assert s["profitFactor"] is None
    assert s["realized"] == 0


def test_r_is_measured_against_initial_risk_not_the_moved_stop(book):
    """After a target trim the stop sits at breakeven. R must not explode."""
    from macro_positioning.paper.performance import TradeRecord

    t = TradeRecord(
        position_id="p", ticker="AAA", side="LONG", status="closed",
        sleeve_id="s", sleeve_label="S", class_id="c", class_label="C", regimes=(),
        opened_at=NOW.isoformat(), closed_at=NOW.isoformat(),
        qty_in=10.0, cost_in=1000.0, avg_entry=100.0, realized=50.0, fees=0.0,
        exit_intent="trail_giveback",
        stop=100.0,            # breakeven — the live stop is useless as a denominator
        initial_risk=5.0,      # what was actually risked per unit at entry
        rank_at_entry=80.0, rank_now=80.0,
    )
    assert t.r_multiple == pytest.approx(1.0)     # 50 / (5 × 10)

    # No banked risk AND a breakeven stop: say nothing rather than 1e11.
    t.initial_risk = None
    assert t.r_multiple is None
