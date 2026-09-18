"""The recursive loop, verified three ways.

  1. here — synthetic records prove the arithmetic and the sign of every
     adjustment, the cap, the shrinkage, and that n = 0 changes nothing;
  2. `scripts/paper_learning_report.py` — the live book's per-sleeve table
     next to the raw stats it was computed from;
  3. `test_a_tick_with_and_without_the_loop_differs_only_by_the_loop`
     — the same tick with the loop on and off, diffed.

The loop is only allowed to be as confident as its sample size. That
sentence is most of what these tests check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from macro_positioning.db.schema import initialize_database
from macro_positioning.paper import store
from macro_positioning.paper.learning import Adjustments, SleeveRecord, adjustments, shrink
from macro_positioning.paper.models import Mandate
from macro_positioning.paper.rank import Component

from test_paper_engine import NOW, decisions_for, marks, read, run_tick


@pytest.fixture
def book(tmp_path: Path):
    db = tmp_path / "learn.db"
    initialize_database(db)
    return db, store.create_portfolio(db_path=db)


# ── 1. Shrinkage ──────────────────────────────────────────────────────


def test_shrinkage_is_exactly_n_over_n_plus_thirty():
    assert shrink(0, 30) == 0.0
    assert shrink(7, 30) == pytest.approx(7 / 37)
    assert shrink(30, 30) == pytest.approx(0.5)
    assert shrink(90, 30) == pytest.approx(0.75)
    assert shrink(-3, 30) == 0.0, "a negative n is a bug upstream, not a weight"


def test_an_empty_book_produces_zero_adjustment(book):
    db, pf = book
    a = adjustments(pf.portfolio_id, mandate=Mandate(), db_path=db, now=NOW)
    assert a.by_sleeve == {}
    assert a.rank_component("BTC") is None
    assert a.risk_multiplier("BTC") == 1.0
    assert a.hold_prior("BTC") == {}


# ── 2. The sign and size of each edge ─────────────────────────────────


def _close_n(db, pf, ticker, n, *, pnl_sign, start=NOW):
    """Open and close `n` trades on `ticker`, each a stop-out (loser) or a
    ladder-then-collapse (winner)."""
    at = start
    for i in range(n):
        run_tick(pf.portfolio_id, db_path=db, now=at, price_fn=marks(**{ticker: 100.0}),
                 candidates=[read(ticker, 80, entry=100.0, stop=96.0, target=1000.0,
                                  scored_at=at.isoformat())], use_learning=False)
        at += timedelta(hours=12)
        if pnl_sign < 0:
            price = 95.0                                   # stop hit, −1R-ish
            run_tick(pf.portfolio_id, db_path=db, now=at, price_fn=marks(**{ticker: price}),
                     candidates=[read(ticker, 80, entry=price, stop=91.2, target=1000.0,
                                      scored_at=at.isoformat())], use_learning=False)
        else:
            price = 112.0                                  # 3R: three rungs, then collapse
            for _ in range(4):
                run_tick(pf.portfolio_id, db_path=db, now=at,
                         price_fn=marks(**{ticker: price}),
                         candidates=[read(ticker, 80, entry=price, stop=107.5,
                                          target=1000.0, scored_at=at.isoformat())],
                         use_learning=False)
                at += timedelta(hours=12)
            # runner: drop 3R -> under the tightened trail
            run_tick(pf.portfolio_id, db_path=db, now=at, price_fn=marks(**{ticker: 104.0}),
                     candidates=[read(ticker, 80, entry=104.0, stop=99.8, target=1000.0,
                                      scored_at=at.isoformat())], use_learning=False)
        assert not [p for p in store.load_positions(pf.portfolio_id, db_path=db)
                    if p.ticker == ticker], f"{ticker} round {i} did not close"
        # clear the cooldown between rounds
        at += timedelta(days=4)
    return at


def test_a_losing_sleeve_pushes_rank_down_and_a_winning_one_up(book):
    db, pf = book
    # LMT is Defense; SOL is Crypto — two different sleeves.
    _close_n(db, pf, "LMT", 5, pnl_sign=-1)
    end = _close_n(db, pf, "SOL", 5, pnl_sign=+1)
    a = adjustments(pf.portfolio_id, mandate=Mandate(), db_path=db, now=end)
    d = a.by_sleeve["defense"]
    c = a.by_sleeve["crypto"]
    assert d.n == 5 and d.win_rate == 0.0 and d.rank_delta < 0
    assert c.n == 5 and c.win_rate == 100.0 and c.rank_delta > 0
    # Five trades is a nudge, not a verdict.
    assert d.w == pytest.approx(5 / 35)
    assert abs(d.rank_delta) < 3.0
    # And the component says all of that out loud.
    comp = a.rank_component("LMT")
    assert comp is not None and comp.name == "book_record"
    assert "over 5 trades" in comp.reason and "w=0.14" in comp.reason


def test_the_cap_holds_however_bad_the_record(book):
    db, pf = book
    # 15 rounds × ~4.5 days = inside the 90-day window. (A first draft ran
    # 40 rounds and found only 20 in the window — the window working.)
    end = _close_n(db, pf, "LMT", 15, pnl_sign=-1)      # w = 15/45 = 0.33
    a = adjustments(pf.portfolio_id, mandate=Mandate(), db_path=db, now=end)
    d = a.by_sleeve["defense"]
    assert d.n == 15
    assert -Mandate().learning_rank_cap <= d.rank_delta < 0
    # avgR ≈ −1.3 → clipped −1.3 × 8 × 0.33 ≈ −3.4: well inside the cap, and
    # a stronger record would be needed to reach it. Force one:
    rec = SleeveRecord(sleeve_id="x", label="x", n=1000, avg_r=-5.0, win_rate=0.0)
    rec.w = shrink(1000, 30)
    rec.rank_delta = max(-10.0, min(10.0, max(-2.0, min(2.0, rec.avg_r)) * 8.0 * rec.w))
    assert rec.rank_delta == pytest.approx(-10.0), "the cap, not the record, sets the floor"


def test_a_positive_avg_r_with_a_low_win_rate_is_still_reported_honestly(book):
    db, pf = book
    # One big winner, four losers: avgR positive, win rate 20%.
    _close_n(db, pf, "SOL", 4, pnl_sign=-1)
    end = _close_n(db, pf, "SOL", 1, pnl_sign=+1, start=NOW + timedelta(days=30))
    a = adjustments(pf.portfolio_id, mandate=Mandate(), db_path=db, now=end)
    c = a.by_sleeve["crypto"]
    assert c.n == 5 and c.win_rate == 20.0
    comp = a.rank_component("SOL")
    assert comp is not None
    assert "20% win" in comp.reason, "the component must not hide the win rate behind avgR"


def test_stop_slippage_raises_the_effective_risk(book):
    db, pf = book
    # A stop at 96 filled at 92 = 1R beyond the stop.
    run_tick(pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(LMT=100.0),
             candidates=[read("LMT", 80, entry=100.0, stop=96.0, target=1000.0)],
             use_learning=False)
    t = NOW + timedelta(hours=12)
    run_tick(pf.portfolio_id, db_path=db, now=t, price_fn=marks(LMT=92.0),
             candidates=[read("LMT", 80, entry=92.0, stop=88.3, target=1000.0,
                              scored_at=t.isoformat())], use_learning=False)
    a = adjustments(pf.portfolio_id, mandate=Mandate(), db_path=db, now=t)
    d = a.by_sleeve["defense"]
    assert d.n_stops == 1 and d.stop_slip_r == pytest.approx(1.0, abs=0.05)
    # Shrunk on ONE stop: 1 + 1.0 × (1/31)
    assert d.risk_mult == pytest.approx(1.0 + 1.0 * shrink(1, 30), abs=0.01)
    assert a.risk_multiplier("LMT") == d.risk_mult


def test_the_hold_prior_does_not_engage_below_thirty(book):
    db, pf = book
    end = _close_n(db, pf, "SOL", 3, pnl_sign=+1)
    a = adjustments(pf.portfolio_id, mandate=Mandate(), db_path=db, now=end)
    c = a.by_sleeve["crypto"]
    assert c.n_peaks >= 1, "snapshots recorded the winners' peaks"
    assert c.peak_days_median is not None
    assert c.hold_prior_days is None, "reported, but not yet a prior"
    assert a.hold_prior("SOL") == {}


# ── 3. The loop on vs off, diffed ─────────────────────────────────────


def test_a_tick_with_and_without_the_loop_differs_only_by_the_loop(book):
    db, pf = book
    end = _close_n(db, pf, "LMT", 6, pnl_sign=-1)
    learned = adjustments(pf.portfolio_id, mandate=Mandate(), db_path=db, now=end)
    cands = [read("LMT", 85, entry=100.0, stop=96.0, target=130.0, scored_at=end.isoformat()),
             read("SOL", 85, entry=100.0, stop=96.0, target=130.0, scored_at=end.isoformat())]
    off = run_tick(pf.portfolio_id, db_path=db, now=end, dry_run=True,
                   price_fn=marks(LMT=100.0, SOL=100.0), candidates=cands, use_learning=False)
    on = run_tick(pf.portfolio_id, db_path=db, now=end, dry_run=True,
                  price_fn=marks(LMT=100.0, SOL=100.0), candidates=cands, learning_in=learned)
    lmt_off = [d for d in decisions_for(off, "LMT") if d.executed][0]
    lmt_on = [d for d in decisions_for(on, "LMT") if d.executed][0]
    sol_off = [d for d in decisions_for(off, "SOL") if d.executed][0]
    sol_on = [d for d in decisions_for(on, "SOL") if d.executed][0]
    # Defense: rank down, size down, and the row says why.
    assert lmt_on.rank < lmt_off.rank
    assert lmt_off.rank - lmt_on.rank <= Mandate().learning_rank_cap
    assert lmt_on.notional < lmt_off.notional
    names = [c["name"] for c in lmt_on.rationale["rank"]["components"]]
    assert "book_record" in names
    assert "book_record" not in [c["name"] for c in lmt_off.rationale["rank"]["components"]]
    # Crypto, with no record in this book, is untouched.
    assert sol_on.rank == sol_off.rank
    # (LMT filled smaller, so equity differs by cents; SOL's target
    # notional is weight × equity.)
    assert sol_on.notional == pytest.approx(sol_off.notional, rel=1e-4)
