"""Tests for the paper-trading book.

Every test drives `run_tick()` with synthetic conviction reads and
synthetic marks — no network, no live DB, no scoring pass. What is being
asserted is the *mandate*: 1–5% sizing off conviction, a hard 30% cash
floor, rotation only on a real conviction edge, and an exit vocabulary
that names the right reason for the right event.

The last two tests are the ones that matter most in review: cash always
reconciles against the immutable fill log, and every refusal leaves a
decision row explaining itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from macro_positioning.db.schema import initialize_database
from macro_positioning.paper import store
from macro_positioning.paper.rank import RankRead, target_weight_for
from macro_positioning.paper.engine import run_tick
from macro_positioning.paper.models import Mandate, Position
from macro_positioning.paper.vocabulary import Action, Band, Blocker, Intent


NOW = datetime(2026, 8, 27, 14, 0, tzinfo=UTC)


@pytest.fixture
def book(tmp_path: Path):
    """A fresh $50k book on a throwaway DB."""
    db = tmp_path / "paper.db"
    initialize_database(db)
    pf = store.create_portfolio(db_path=db)
    return db, pf


def read(
    ticker: str,
    rank: float,
    *,
    side: str = "LONG",
    entry: float = 100.0,
    stop: float = 96.0,      # 4% — inside the 5% hard cap
    target: float = 130.0,
    rr: float = 3.0,
    scored_at: str | None = None,
    mandate: Mandate | None = None,
) -> RankRead:
    """A rank read with a fixed 0-100 value, bypassing the ranking maths.

    Sizing, rotation and the exit rules are what these tests exercise;
    the ranking itself is asserted separately in `test_rank_*`.
    """
    m = mandate or Mandate()
    r = RankRead(
        ticker=ticker.upper(),
        side=side,
        value=rank,
        band=Band.for_rank(rank, entry_floor=m.entry_floor),
        target_weight_pct=target_weight_for(rank, m),
        components=[],
        score=int(55 + rank / 100 * 35),
        grade="A",
        scored_at=scored_at or NOW.isoformat(),
    )
    r.source_row = {  # type: ignore[attr-defined]
        "asset": ticker.upper(), "side": side, "hasLevels": True,
        "entry": entry, "stop": stop, "target": target, "rr": rr,
        "setup": "test setup", "levelStructural": True,
    }
    return r


def marks(**prices: float):
    def _fn(tickers):
        return {
            t.upper(): {"price": prices[t.upper()], "source": "test"}
            for t in tickers
            if t.upper() in prices
        }
    return _fn


def _position_horizon(pf, db, ticker="AAA"):
    """The confirm-tick count and min hold the engine actually stored on
    the position — derived at fill, so tests read it rather than assume."""
    for p in store.load_positions(pf.portfolio_id, db_path=db):
        if p.ticker == ticker.upper():
            return (p.confirm_ticks or Mandate().thesis_exit_confirm_ticks,
                    p.min_hold_days if p.min_hold_days is not None else Mandate().min_hold_days)
    return Mandate().thesis_exit_confirm_ticks, Mandate().min_hold_days


def confirm_ticks(pf, db, *, start, candidates, price_fn, valuations_in=None, n=None,
                  ticker="AAA"):
    """Run the same read on consecutive ticks until a thesis exit confirms.

    Thesis exits (rank decay, support collapse, side flip) must persist for
    the position's own `confirm_ticks` before they act. Ticks are 12h
    apart, like the scheduled cadence. Returns the last tick's result.
    """
    if n is None:
        n, _ = _position_horizon(pf, db, ticker)
    r = None
    for i in range(n + 6):     # +6: rungs / trims may consume ticks first
        if i >= n and not any(p.ticker == ticker.upper()
                              for p in store.load_positions(pf.portfolio_id, db_path=db)):
            break
        at = start + timedelta(hours=12 * i)
        cands = [read(c.ticker, c.value, side=c.side,
                      entry=c.source_row["entry"], stop=c.source_row["stop"],
                      target=c.source_row["target"], scored_at=at.isoformat())
                 for c in candidates]
        r = run_tick(pf.portfolio_id, db_path=db, now=at, price_fn=price_fn,
                     candidates=cands, valuations_in=valuations_in)
    return r


def settle(pf, db, *, start, candidates, price_fn, max_ticks=6):
    """Tick at a fixed price until the position stops acting (rungs fire one
    per tick). Returns (last result, ticks used)."""
    r, i = None, 0
    for i in range(max_ticks):
        at = start + timedelta(hours=12 * i)
        cands = [read(c.ticker, c.value, side=c.side, entry=c.source_row["entry"],
                      stop=c.source_row["stop"], target=c.source_row["target"],
                      scored_at=at.isoformat()) for c in candidates]
        r = run_tick(pf.portfolio_id, db_path=db, now=at, price_fn=price_fn, candidates=cands)
        if not any(d.executed for d in r.decisions):
            break
    return r, i


def strong_valuations(*tickers, support=90.0):
    from macro_positioning.paper.valuation import TradeValuation
    return {t: TradeValuation(ticker=t, side="LONG", support=support, target=10_000.0,
                              agent_target=10_000.0) for t in tickers}


# Past any minimum hold a fixture can derive: no signal horizon → the
# 20-day fallback → 8-day hold. (A `position` read would be 12d.)
AFTER_HOLD = NOW + timedelta(days=11)


def decisions_for(result, ticker: str):
    return [d for d in result.decisions if d.ticker == ticker.upper()]


# ── Sizing ────────────────────────────────────────────────────────────


def test_rank_maps_across_the_one_to_five_percent_band(book):
    db, pf = book
    m = Mandate()
    r = run_tick(
        pf.portfolio_id, db_path=db, now=NOW,
        price_fn=marks(AAA=100.0, BBB=100.0, CCC=100.0),
        # rank 100 = top of the scale; 80 = halfway up the tradeable
        # range; 60 = the entry bar itself.
        candidates=[read("AAA", 100), read("BBB", 80), read("CCC", 60)],
    )
    by = {d.ticker: d for d in r.decisions if d.executed}
    equity = r.equity_before

    assert by["AAA"].notional == pytest.approx(equity * 0.05, rel=1e-3)
    assert by["BBB"].notional == pytest.approx(equity * 0.03, rel=1e-3)
    # A name that only just clears the bar gets the 1% minimum — the whole
    # band is reachable now, rather than starting at 2.1%.
    assert by["CCC"].notional == pytest.approx(equity * 0.01, rel=1e-3)
    assert all(d.action == Action.OPEN for d in by.values())
    assert by["AAA"].intent == Intent.CLEARED_BAR
    assert m.entry_floor == 60.0


def test_below_the_entry_bar_never_trades(book):
    db, pf = book
    r = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0),
        candidates=[read("AAA", 59)],          # one point under the bar
    )
    assert r.fills == []
    assert r.below_floor == 1
    assert store.load_positions(pf.portfolio_id, db_path=db) == []


def test_a_name_with_no_stop_is_not_sized(book):
    db, pf = book
    r = read("AAA", 90)
    r.source_row["hasLevels"] = False
    r.source_row["stop"] = None
    r.source_row["levelsReason"] = "no_atr"
    result = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0), candidates=[r]
    )
    (d,) = decisions_for(result, "AAA")
    assert d.action == Action.REJECT and d.blocker == Blocker.NO_LEVELS
    assert "no_atr" in d.headline


# ── The cash floor ────────────────────────────────────────────────────


def test_book_never_deploys_past_seventy_percent(book):
    db, pf = book
    # 20 strong names at 5% each would be 100% deployed.
    cands = [read(f"T{i:02d}", 95, entry=100.0) for i in range(20)]
    price = {f"T{i:02d}": 100.0 for i in range(20)}
    r = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(**price), candidates=cands
    )
    assert r.deployed_pct_after <= 0.70 + 1e-6
    assert r.cash_after >= r.equity_after * 0.30 - 1e-6
    # And it said why it stopped, rather than silently truncating.
    blocked = [d for d in r.refusals if d.blocker in (Blocker.CASH_FLOOR, Blocker.MAX_POSITIONS,
                                                      Blocker.NO_ROTATION_EDGE)]
    assert blocked, "a truncated buy list must leave refusal rows"


def test_marks_pushing_the_book_over_the_ceiling_trigger_a_trim(book):
    db, pf = book
    # Open to ~70% deployed, then rally the book so deployment drifts over.
    # Targets are deliberately out of reach so the rally does not trigger
    # target trims — the floor must be the only thing that acts.
    cands = [read(f"T{i}", 100, target=10_000.0) for i in range(14)]
    price = {f"T{i}": 100.0 for i in range(14)}
    # Target path (rank 100 + strong support) with an unreachable target, so
    # no rung trims fire on the rally — only the floor can act.
    run_tick(pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(**price), candidates=cands,
             valuations_in=strong_valuations(*[f"T{i}" for i in range(14)]))

    # Everything doubles: exposure doubles, cash does not.
    rallied = {k: 200.0 for k in price}
    r2 = run_tick(
        pf.portfolio_id, db_path=db, now=NOW + timedelta(days=1),
        price_fn=marks(**rallied),
        # Same reads, so nothing exits on conviction; only the floor acts.
        candidates=[read(f"T{i}", 100, entry=200.0, stop=192.0, target=10_000.0,
                         scored_at=(NOW + timedelta(days=1)).isoformat()) for i in range(14)],
        valuations_in=strong_valuations(*[f"T{i}" for i in range(14)]),
    )
    assert r2.deployed_pct_after <= 0.70 + 0.006
    floor_trims = [
        d for d in r2.decisions if d.intent == Intent.CASH_FLOOR_BREACH and d.executed
    ]
    assert floor_trims, "the ceiling breach must produce an explicit trim"


# ── Rotation ──────────────────────────────────────────────────────────


def _fill_the_book(db, pf, rank: float, n: int = 14):
    cands = [read(f"H{i}", rank) for i in range(n)]
    price = {f"H{i}": 100.0 for i in range(n)}
    return run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(**price), candidates=cands
    ), price


def test_a_materially_stronger_read_rotates_out_a_weak_holding(book):
    db, pf = book
    _, price = _fill_the_book(db, pf, 62)
    held = {p.ticker for p in store.load_positions(pf.portfolio_id, db_path=db)}
    assert held, "setup should have opened positions"

    # Past the minimum hold: rotation may not churn a position that has
    # not had a chance to work.
    later = AFTER_HOLD      # rotation cannot touch a position inside its minimum hold
    cands = [read(t, 62, scored_at=later.isoformat()) for t in sorted(held)]
    cands.insert(0, read("STAR", 92, scored_at=later.isoformat()))
    r = run_tick(
        pf.portfolio_id, db_path=db, now=later,
        price_fn=marks(STAR=100.0, **price), candidates=cands,
    )
    star = [d for d in decisions_for(r, "STAR") if d.executed]
    assert star and star[0].action == Action.OPEN
    assert star[0].intent == Intent.ROTATION_IN
    freed = [d for d in r.decisions if d.intent == Intent.MAKE_ROOM and d.executed]
    assert freed, "the fill had to be funded by trimming something"
    assert r.deployed_pct_after <= 0.70 + 1e-6


def test_a_marginally_stronger_read_does_not_churn_the_book(book):
    db, pf = book
    _, price = _fill_the_book(db, pf, 70)
    held = {p.ticker for p in store.load_positions(pf.portfolio_id, db_path=db)}

    later = NOW + timedelta(days=5)
    cands = [read(t, 70, scored_at=later.isoformat()) for t in sorted(held)]
    cands.insert(0, read("MEH", 78, scored_at=later.isoformat()))   # +8 points < the 15-point edge
    r = run_tick(
        pf.portfolio_id, db_path=db, now=later,
        price_fn=marks(MEH=100.0, **price), candidates=cands,
    )
    assert not [d for d in r.decisions if d.intent == Intent.MAKE_ROOM and d.executed]
    meh = decisions_for(r, "MEH")
    assert meh and meh[0].action == Action.REJECT
    assert meh[0].blocker in (Blocker.NO_ROTATION_EDGE, Blocker.CASH_FLOOR,
                              Blocker.MAX_POSITIONS)


# ── Exits ─────────────────────────────────────────────────────────────


def _open_one(db, pf, **kw):
    r = read("AAA", 80, **kw)
    run_tick(pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0), candidates=[r])
    return store.load_positions(pf.portfolio_id, db_path=db)[0]


def test_stop_hit_closes_the_position(book):
    db, pf = book
    _open_one(db, pf)
    later = NOW + timedelta(days=1)
    r = run_tick(
        pf.portfolio_id, db_path=db, now=later, price_fn=marks(AAA=95.0),
        candidates=[read("AAA", 80, scored_at=later.isoformat())],
    )
    (d,) = [x for x in decisions_for(r, "AAA") if x.executed]
    assert d.action == Action.EXIT and d.intent == Intent.STOP_HIT
    assert store.load_positions(pf.portfolio_id, db_path=db) == []


def test_the_ladder_takes_a_quarter_at_each_rung_and_ratchets_the_stop(book):
    db, pf = book
    p0 = _open_one(db, pf, target=1000.0)          # 4% stop → 1R = 4 points
    # 1R: first rung, stop to breakeven.
    r1 = run_tick(pf.portfolio_id, db_path=db, now=NOW + timedelta(days=1),
                  price_fn=marks(AAA=104.5),
                  candidates=[read("AAA", 80, entry=104.5, stop=100.3, target=1000.0)])
    trims = [d for d in decisions_for(r1, "AAA") if d.executed]
    assert trims and trims[0].action == Action.TRIM and trims[0].intent == Intent.RUNG_TAKEN
    assert "1.0R" in trims[0].headline and "25%" in trims[0].headline
    p1 = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert p1.qty == pytest.approx(p0.qty * 0.75, rel=1e-6)
    assert p1.rungs_taken == 1
    assert p1.stop == pytest.approx(p1.avg_price, rel=1e-9)
    # 2R: second rung, stop locks 1R.
    r2 = run_tick(pf.portfolio_id, db_path=db, now=NOW + timedelta(days=2),
                  price_fn=marks(AAA=108.5),
                  candidates=[read("AAA", 80, entry=108.5, stop=104.2, target=1000.0)])
    assert [d for d in decisions_for(r2, "AAA") if d.intent == Intent.RUNG_TAKEN and d.executed]
    p2 = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert p2.qty == pytest.approx(p0.qty * 0.50, rel=1e-6)
    assert p2.stop == pytest.approx(p2.avg_price + p2.initial_risk, rel=1e-6)
    assert p2.realized_pnl > 0


def test_side_flip_closes_rather_than_holding_the_wrong_way(book):
    db, pf = book
    _open_one(db, pf)
    flip = read("AAA", 75, side="SHORT", entry=101.0, stop=110.0, target=80.0)
    # One tick reading SHORT is a wobble: the book watches, it does not act.
    r1 = run_tick(pf.portfolio_id, db_path=db, now=NOW + timedelta(days=1),
                  price_fn=marks(AAA=101.0), candidates=[flip])
    (d,) = decisions_for(r1, "AAA")
    assert d.action == Action.HOLD and d.intent == Intent.SIDE_FLIP
    assert "1/" in d.headline and "watching" in d.headline
    # Persisting for the confirmation window, it acts — direction is exempt
    # from the minimum hold, so this is still day 1-3. The watching tick
    # above already counted, so the remaining ticks start 12h later.
    r = confirm_ticks(pf, db, start=NOW + timedelta(days=1, hours=12),
                      candidates=[flip], price_fn=marks(AAA=101.0),
                      n=_position_horizon(pf, db)[0] - 1)
    exits = [d for d in decisions_for(r, "AAA") if d.action == Action.EXIT]
    assert exits and exits[0].intent == Intent.SIDE_FLIP


def test_rank_decay_below_the_exit_bar_closes_the_position(book):
    db, pf = book
    _open_one(db, pf)
    # Past the minimum hold and persisting for the confirmation window.
    # Inside the hold the position is left alone, which
    # `test_a_young_position_is_not_talked_out_of_or_rotated_away` covers.
    r = confirm_ticks(pf, db, start=AFTER_HOLD, price_fn=marks(AAA=101.0),
                      candidates=[read("AAA", 25, entry=101.0)])   # under the 40 exit bar
    exits = [d for d in decisions_for(r, "AAA") if d.action == Action.EXIT]
    assert exits and exits[0].intent == Intent.RANK_DECAY
    assert "exit bar" in exits[0].headline


def test_a_read_nobody_has_refreshed_goes_stale(book):
    db, pf = book
    _open_one(db, pf)
    later = NOW + timedelta(days=30)
    r = run_tick(
        pf.portfolio_id, db_path=db, now=later, price_fn=marks(AAA=101.0),
        candidates=[read("AAA", 80, scored_at=NOW.isoformat())],   # 30 days old
    )
    exits = [d for d in decisions_for(r, "AAA") if d.action == Action.EXIT]
    assert exits and exits[0].intent == Intent.STALE_THESIS


def test_trailing_giveback_closes_a_runner(book):
    db, pf = book
    _open_one(db, pf, target=1000.0)
    # Run to 200 (25R on a 4-point risk) and let the ladder rungs settle,
    # leaving the runner with the trail armed and tightened to 25%.
    up = read("AAA", 80, entry=200.0, stop=192.0, target=1000.0)
    _, used = settle(pf, db, start=NOW + timedelta(days=1), candidates=[up],
                     price_fn=marks(AAA=200.0))
    held = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert held.qty > 0, "the runner survives the rungs"
    # Then give back more than the tightened trail allows.
    t2 = NOW + timedelta(days=2, hours=12 * used)
    r = run_tick(pf.portfolio_id, db_path=db, now=t2, price_fn=marks(AAA=150.0),
                 candidates=[read("AAA", 80, entry=150.0, stop=144.0, target=1000.0,
                                  scored_at=t2.isoformat())])
    exits = [d for d in decisions_for(r, "AAA") if d.action == Action.EXIT]
    assert exits and exits[0].intent == Intent.TRAIL_GIVEBACK
    assert exits[0].rationale["trail"]["givebackAllowed"] == pytest.approx(0.25)


def test_a_short_makes_money_when_price_falls(book):
    db, pf = book
    r1 = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0),
        candidates=[read("AAA", 90, side="SHORT", entry=100.0, stop=104.0, target=70.0)],
    )
    assert r1.equity_after == pytest.approx(r1.equity_before, rel=1e-3)  # no instant profit
    later = NOW + timedelta(days=1)
    r2 = run_tick(
        pf.portfolio_id, db_path=db, now=later, price_fn=marks(AAA=90.0),
        candidates=[read("AAA", 90, side="SHORT", entry=90.0, stop=93.6, target=70.0,
                         scored_at=later.isoformat())],
    )
    assert r2.equity_after > r1.equity_after


# ── The audit trail ───────────────────────────────────────────────────


def test_cash_reconciles_against_the_fill_log_through_a_full_cycle(book):
    db, pf = book
    # Open, add, trim at target, stop out, rotate — then check the books.
    run_tick(pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0, BBB=50.0),
             candidates=[read("AAA", 40), read("BBB", 50, entry=50.0, stop=45.0, target=65.0)])
    t1 = NOW + timedelta(days=1)
    run_tick(pf.portfolio_id, db_path=db, now=t1, price_fn=marks(AAA=101.0, BBB=66.0),
             candidates=[read("AAA", 90, scored_at=t1.isoformat()),
                         read("BBB", 50, entry=66.0, stop=45.0, target=65.0,
                              scored_at=t1.isoformat())])
    t2 = NOW + timedelta(days=2)
    r = run_tick(pf.portfolio_id, db_path=db, now=t2, price_fn=marks(AAA=89.0, BBB=70.0),
                 candidates=[read("AAA", 90, scored_at=t2.isoformat()),
                             read("BBB", 50, entry=70.0, stop=45.0, target=65.0,
                                  scored_at=t2.isoformat())])
    assert r.reconciliation["ok"], r.reconciliation
    assert store.reconcile(pf.portfolio_id, db_path=db)["ok"]
    assert r.error is None


def test_every_refusal_leaves_a_row_saying_why(book):
    db, pf = book
    cands = [read(f"T{i:02d}", 90) for i in range(20)]
    price = {f"T{i:02d}": 100.0 for i in range(20)}
    r = run_tick(pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(**price),
                 candidates=cands)
    rows = store.recent_decisions(pf.portfolio_id, db_path=db, limit=500)
    rejects = [d for d in rows if d["action"] == "REJECT"]
    assert rejects, "the book turned names away; it must say so"
    for d in rejects:
        assert d["blocker"], "a refusal without a blocker is unexplainable"
        assert d["headline"] and len(d["headline"]) > 20
        assert d["rationale"].get("constraints"), "refusals carry the constraint state"


def test_dry_run_writes_nothing(book):
    db, pf = book
    r = run_tick(pf.portfolio_id, db_path=db, now=NOW, dry_run=True,
                 price_fn=marks(AAA=100.0), candidates=[read("AAA", 90)])
    assert r.fills, "a dry run still computes the decisions"
    assert not r.committed
    assert store.load_positions(pf.portfolio_id, db_path=db) == []
    assert store.recent_decisions(pf.portfolio_id, db_path=db) == []
    assert store.get_portfolio(pf.portfolio_id, db_path=db).cash == pf.starting_equity


def test_an_unpriceable_name_is_never_filled(book):
    db, pf = book
    r = run_tick(pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(),
                 candidates=[read("GHOST", 90)])
    (d,) = decisions_for(r, "GHOST")
    assert d.action == Action.REJECT and d.blocker == Blocker.NO_PRICE
    assert r.unpriced == ["GHOST"]


def test_a_risk_exit_puts_the_name_in_cooldown(book):
    db, pf = book
    _open_one(db, pf)
    t1 = NOW + timedelta(days=1)
    run_tick(pf.portfolio_id, db_path=db, now=t1, price_fn=marks(AAA=95.0),
             candidates=[read("AAA", 90, scored_at=t1.isoformat())])
    assert store.load_positions(pf.portfolio_id, db_path=db) == []

    # Next day the name still scores well. The book must not buy it back.
    t2 = NOW + timedelta(days=2)
    r = run_tick(pf.portfolio_id, db_path=db, now=t2, price_fn=marks(AAA=95.0),
                 candidates=[read("AAA", 90, entry=95.0, stop=91.5,
                                  scored_at=t2.isoformat())])
    (d,) = decisions_for(r, "AAA")
    assert d.action == Action.REJECT and d.blocker == Blocker.COOLING_OFF
    assert not r.fills

    # Once the window passes, it is eligible again.
    t3 = NOW + timedelta(days=6)
    r3 = run_tick(pf.portfolio_id, db_path=db, now=t3, price_fn=marks(AAA=95.0),
                  candidates=[read("AAA", 90, entry=95.0, stop=91.5,
                                   scored_at=t3.isoformat())])
    assert [d for d in decisions_for(r3, "AAA") if d.executed]


# ── Ranking ───────────────────────────────────────────────────────────


def _row(**over):
    row = {
        "asset": "AAA", "side": "LONG", "score": 76, "hasLevels": True,
        "rr": 1.5, "levelStructural": False, "d_score": 0,
        "setup": "test", "signal_aggregate": {},
    }
    row.update(over)
    return row


# A synthetic distribution: 101 scores evenly spread 0..100, so the
# percentile of score N is N. Keeps the arithmetic in these tests
# readable instead of dependent on whatever the live tape looks like.
FLAT_DIST = [float(i) for i in range(101)]


def test_rank_is_the_percentile_of_the_score(read_rank=None):
    from macro_positioning.paper.rank import read_rank

    m = Mandate()
    # Score 70 against a flat 0..100 distribution ranks at 70.
    r = read_rank(_row(score=70, rr=1.5), m, FLAT_DIST)
    base = next(c for c in r.components if c.name == "base")
    assert base.delta == pytest.approx(70.0, abs=0.5)
    assert r.anchored is True
    assert "above 70% of the" in base.reason


def test_rank_falls_back_to_the_old_anchors_without_history():
    from macro_positioning.paper.rank import read_rank

    m = Mandate()
    # No distribution to rank against: the straight 55→0, 90→100 line.
    assert read_rank(_row(score=55, rr=1.5), m, []).value == pytest.approx(0.0, abs=0.01)
    r90 = read_rank(_row(score=90, rr=1.5), m, [])
    assert r90.value == pytest.approx(100.0, abs=0.01)
    assert r90.anchored is False
    assert "not enough scoring history" in r90.components[0].reason


def test_percentile_uses_the_midpoint_on_ties():
    from macro_positioning.paper.rank import percentile_of

    # Half the distribution equals 50; a score of 50 beats the 25 below
    # it and splits the tie, landing at 50 rather than 25 or 75.
    dist = sorted([float(i) for i in range(25)] + [50.0] * 50 + [90.0] * 25)
    assert percentile_of(50.0, dist) == pytest.approx(50.0, abs=0.5)
    assert percentile_of(0.0, dist) == pytest.approx(0.5, abs=0.6)
    assert percentile_of(95.0, dist) == pytest.approx(100.0, abs=0.5)


def test_signals_agreeing_lift_the_rank_and_opposing_sink_it():
    from macro_positioning.paper.rank import read_rank

    m = Mandate()
    agree = {"n_signals": 20, "blend": {"bias_direction": "long", "bias_confidence": 1.0}}
    oppose = {"n_signals": 20, "blend": {"bias_direction": "short", "bias_confidence": 1.0}}
    base = read_rank(_row(score=70), m, FLAT_DIST).value
    up = read_rank(_row(score=70, signal_aggregate=agree), m, FLAT_DIST)
    down = read_rank(_row(score=70, signal_aggregate=oppose), m, FLAT_DIST)
    # Modifiers are percentile POINTS, not fractions.
    assert up.value == pytest.approx(base + 15, abs=0.01)
    assert down.value == pytest.approx(base - 25, abs=0.01)
    reasons = " ".join(c.reason for c in down.components)
    assert "other side" in reasons


def test_a_name_the_desk_is_fading_needs_a_much_better_score():
    from macro_positioning.paper.rank import read_rank

    m = Mandate()
    oppose = {"n_signals": 20, "blend": {"bias_direction": "short", "bias_confidence": 1.0}}
    # 70th percentile with the desk shorting it lands under the bar…
    assert read_rank(_row(score=70, signal_aggregate=oppose), m, FLAT_DIST).value < m.entry_floor
    # …while the same read with nobody objecting clears it.
    assert read_rank(_row(score=70), m, FLAT_DIST).value >= m.entry_floor


def test_ranking_refuses_a_non_directional_read():
    from macro_positioning.paper.rank import read_rank

    r = read_rank(_row(side="WATCH", score=95), Mandate(), FLAT_DIST)
    assert r.tradeable is False
    assert r.target_weight_pct == 0.0


def test_rank_headline_names_what_moved_it():
    from macro_positioning.paper.rank import read_rank

    r = read_rank(
        _row(score=80, rr=3.5, levelStructural=True, d_score=8,
             signal_aggregate={"n_signals": 9,
                               "blend": {"bias_direction": "long", "bias_confidence": 0.9}}),
        Mandate(), FLAT_DIST,
    )
    assert "AAA LONG ranks" in r.headline
    assert "%" in r.headline
    assert len(r.components) >= 4
    # Everything stacked: 80th percentile + 13.5 + 10 + 5 + 5, capped at 100.
    assert r.value == pytest.approx(100.0, abs=0.01)


def test_rank_never_leaves_the_zero_to_one_hundred_range():
    from macro_positioning.paper.rank import read_rank

    m = Mandate()
    oppose = {"n_signals": 5, "blend": {"bias_direction": "short", "bias_confidence": 1.0}}
    floorish = read_rank(_row(score=2, rr=0.5, signal_aggregate=oppose), m, FLAT_DIST)
    assert floorish.value == 0.0
    top = read_rank(
        _row(score=100, rr=5.0, levelStructural=True, d_score=20,
             signal_aggregate={"n_signals": 5,
                               "blend": {"bias_direction": "long", "bias_confidence": 1.0}}),
        m, FLAT_DIST,
    )
    assert top.value == 100.0


# ── Stale levels ──────────────────────────────────────────────────────


def test_a_stop_already_on_the_wrong_side_is_refused(book):
    db, pf = book
    # Level set says stop 90 but the tape is at 85 — the setup died between
    # the scoring pass and this tick. Buying it would stop out immediately.
    r = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=92.0),
        candidates=[read("AAA", 90, entry=100.0, stop=96.0, target=130.0)],
    )
    (d,) = decisions_for(r, "AAA")
    assert d.action == Action.REJECT and d.blocker == Blocker.LEVELS_STALE
    assert "stop" in d.headline
    assert not r.fills


def test_a_short_whose_stop_is_below_the_tape_is_refused(book):
    db, pf = book
    r = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=108.0),
        candidates=[read("AAA", 90, side="SHORT", entry=100.0, stop=104.0, target=80.0)],
    )
    (d,) = decisions_for(r, "AAA")
    assert d.action == Action.REJECT and d.blocker == Blocker.LEVELS_STALE


def test_price_already_through_the_target_is_refused(book):
    db, pf = book
    r = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=135.0),
        candidates=[read("AAA", 90, entry=100.0, stop=129.6, target=130.0)],
    )
    (d,) = decisions_for(r, "AAA")
    assert d.action == Action.REJECT and d.blocker == Blocker.LEVELS_STALE
    assert "target" in d.headline


def test_not_a_trade_is_counted_separately_from_not_good_enough(book):
    db, pf = book
    watch = read("WATCHY", 90)
    watch.side = "WATCH"
    watch.tradeable = False
    r = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0, WATCHY=10.0),
        candidates=[watch, read("AAA", 10)],
    )
    assert r.not_directional == 1, "a WATCH read was never a directional call"
    assert r.below_floor == 1, "a real long we didn't like enough is a different thing"
    assert "not directional" in r.report()


def test_names_pinned_at_one_hundred_keep_a_stable_order():
    from macro_positioning.paper.rank import read_rank

    m = Mandate()
    strong = {"n_signals": 30, "blend": {"bias_direction": "long", "bias_confidence": 1.0}}
    # Both saturate the displayed 0-100 range…
    a = read_rank(_row(asset="A", score=92, rr=3.5, levelStructural=True, d_score=8,
                       signal_aggregate=strong), m, FLAT_DIST)
    b = read_rank(_row(asset="B", score=99, rr=3.5, levelStructural=True, d_score=8,
                       signal_aggregate=strong), m, FLAT_DIST)
    assert a.value == b.value == 100.0
    # …but the evidence still separates them, so the last slot is not a
    # coin flip decided by query order.
    assert b.raw > a.raw


def test_the_trail_ignores_noise_below_the_arming_threshold(book):
    db, pf = book
    # Entry 100, stop 90 -> 1R is a move to 110. This position peaks at
    # 100.5 (+0.05R) and falls back: a 60% "giveback" of five cents.
    _open_one(db, pf, target=1000.0)
    t1 = NOW + timedelta(days=1)
    run_tick(pf.portfolio_id, db_path=db, now=t1, price_fn=marks(AAA=100.5),
             candidates=[read("AAA", 80, entry=100.5, target=1000.0,
                              scored_at=t1.isoformat())])
    t2 = NOW + timedelta(days=2)
    r = run_tick(pf.portfolio_id, db_path=db, now=t2, price_fn=marks(AAA=100.2),
                 candidates=[read("AAA", 80, entry=100.2, target=1000.0,
                                  scored_at=t2.isoformat())])
    assert not [d for d in decisions_for(r, "AAA") if d.action == Action.EXIT], (
        "a trail that fires on a five-cent round trip is a noise detector"
    )
    assert store.load_positions(pf.portfolio_id, db_path=db)


def test_the_trail_still_protects_a_real_run(book):
    db, pf = book
    _open_one(db, pf, target=10_000.0)          # entry 100, stop 96 -> 1R = 104
    up = read("AAA", 80, entry=140.0, stop=134.4, target=10_000.0)
    _, used = settle(pf, db, start=NOW + timedelta(days=1), candidates=[up],
                     price_fn=marks(AAA=140.0))
    # 140 -> 115 hands back most of a 10R run. The runner is closed.
    t2 = NOW + timedelta(days=2, hours=12 * used)
    r = run_tick(pf.portfolio_id, db_path=db, now=t2, price_fn=marks(AAA=115.0),
                 candidates=[read("AAA", 80, entry=115.0, stop=110.4, target=10_000.0,
                                  scored_at=t2.isoformat())])
    exits = [d for d in decisions_for(r, "AAA") if d.action == Action.EXIT]
    assert exits and exits[0].intent in (Intent.TRAIL_GIVEBACK, Intent.TRAIL_ROUND_TRIP,
                                         Intent.STOP_HIT)


def test_a_breakeven_stop_does_not_disarm_the_trail(book):
    db, pf = book
    p = _open_one(db, pf, target=1000.0)
    # First rung moves the stop to breakeven. R must still be computable
    # from the banked initial risk, or the trail silently disarms.
    t1 = NOW + timedelta(days=1)
    run_tick(pf.portfolio_id, db_path=db, now=t1, price_fn=marks(AAA=104.5),
             candidates=[read("AAA", 80, entry=104.5, stop=100.3, target=1000.0,
                              scored_at=t1.isoformat())])
    held = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert held.rungs_taken == 1 and held.stop == pytest.approx(held.avg_price)
    assert held.initial_risk and held.initial_risk > 0
    assert held.r_multiple(104.5) is not None
    assert held.r_multiple(104.5) > 1.0


def test_a_young_position_is_not_talked_out_of_or_rotated_away(book):
    db, pf = book
    _open_one(db, pf)
    # Rank collapses the very next day. On a swing book that is not a
    # reason to close inside the minimum hold.
    t1 = NOW + timedelta(days=1)
    r = run_tick(pf.portfolio_id, db_path=db, now=t1, price_fn=marks(AAA=101.0),
                 candidates=[read("AAA", 10, entry=101.0, scored_at=t1.isoformat())])
    assert not [d for d in decisions_for(r, "AAA") if d.action == Action.EXIT]
    assert store.load_positions(pf.portfolio_id, db_path=db)

    # Past the minimum hold AND persisting for the confirmation window,
    # the same read does close it.
    r2 = confirm_ticks(pf, db, start=AFTER_HOLD, price_fn=marks(AAA=101.0),
                       candidates=[read("AAA", 10, entry=101.0)])
    exits = [d for d in decisions_for(r2, "AAA") if d.action == Action.EXIT]
    assert exits and exits[0].intent == Intent.RANK_DECAY


def test_a_stop_still_fires_inside_the_minimum_hold(book):
    db, pf = book
    _open_one(db, pf)
    # Risk always acts. The minimum hold is about being talked out of a
    # thesis, not about ignoring a broken level.
    t1 = NOW + timedelta(hours=6)
    r = run_tick(pf.portfolio_id, db_path=db, now=t1, price_fn=marks(AAA=95.0),
                 candidates=[read("AAA", 80, scored_at=t1.isoformat())])
    exits = [d for d in decisions_for(r, "AAA") if d.action == Action.EXIT]
    assert exits and exits[0].intent == Intent.STOP_HIT


def test_a_runner_that_round_trips_gets_its_own_sentence(book):
    db, pf = book
    # Target path (no rungs): reaches 2R, then comes all the way back under
    # entry but above the stop. It did not give back 755% of anything — it
    # round-tripped.
    _open_one(db, pf, target=1000.0)
    held = store.load_positions(pf.portfolio_id, db_path=db)[0]
    with store.connect(db) as conn:
        conn.execute("UPDATE paper_positions SET exit_path='target' WHERE position_id=?",
                     (held.position_id,))
    t1 = NOW + timedelta(days=1)
    run_tick(pf.portfolio_id, db_path=db, now=t1, price_fn=marks(AAA=108.0),
             candidates=[read("AAA", 80, entry=108.0, stop=103.7, target=1000.0,
                              scored_at=t1.isoformat())])
    t2 = NOW + timedelta(days=2)
    r = run_tick(pf.portfolio_id, db_path=db, now=t2, price_fn=marks(AAA=98.0),
                 candidates=[read("AAA", 80, entry=98.0, stop=94.0, target=1000.0,
                                  scored_at=t2.isoformat())])
    exits = [d for d in decisions_for(r, "AAA") if d.action == Action.EXIT]
    assert exits and exits[0].intent == Intent.TRAIL_ROUND_TRIP
    assert "%" not in exits[0].headline
    assert "underwater" in exits[0].headline


def _stop_out(db, pf, *, entry=100.0, stop_price=95.0, at=None):
    """Open AAA at `entry`, then stop it out at `stop_price`."""
    at = at or NOW
    run_tick(pf.portfolio_id, db_path=db, now=at, price_fn=marks(AAA=entry),
             candidates=[read("AAA", 80, entry=entry, stop=96.0, target=130.0,
                              scored_at=at.isoformat())])
    t = at + timedelta(hours=6)
    run_tick(pf.portfolio_id, db_path=db, now=t, price_fn=marks(AAA=stop_price),
             candidates=[read("AAA", 80, entry=entry, stop=96.0, target=130.0,
                              scored_at=t.isoformat())])
    assert store.load_positions(pf.portfolio_id, db_path=db) == []
    return t


def test_a_setup_that_reclaims_the_level_is_taken_again(book):
    db, pf = book
    _stop_out(db, pf, entry=100.0, stop_price=95.0)

    # Price broke the stop, then recovered back above what the book paid.
    # That is the setup re-presenting itself, not a dead thesis.
    later = NOW + timedelta(days=1)
    r = run_tick(
        pf.portfolio_id, db_path=db, now=later, price_fn=marks(AAA=103.0),
        candidates=[read("AAA", 80, entry=103.0, stop=99.0, target=130.0,
                         scored_at=later.isoformat())],
    )
    fills = [d for d in decisions_for(r, "AAA") if d.executed]
    assert fills and fills[0].action == Action.OPEN
    assert fills[0].intent == Intent.RECLAIMED_SETUP
    assert "reclaimed" in fills[0].headline
    assert store.load_positions(pf.portfolio_id, db_path=db)


def test_a_name_still_below_where_it_stopped_out_is_not_bought_back(book):
    db, pf = book
    _stop_out(db, pf, entry=100.0, stop_price=95.0)

    # A bounce off the low is not a reclaim — 94 is still under the 100
    # the book paid. Buying here is paying twice for the same broken idea.
    later = NOW + timedelta(days=1)
    r = run_tick(
        pf.portfolio_id, db_path=db, now=later, price_fn=marks(AAA=94.0),
        candidates=[read("AAA", 80, entry=94.0, stop=90.5, target=130.0,
                         scored_at=later.isoformat())],
    )
    (d,) = decisions_for(r, "AAA")
    assert d.action == Action.REJECT and d.blocker == Blocker.COOLING_OFF
    # And it says exactly what price would reopen it.
    assert "100" in d.headline and "94" in d.headline


def test_a_thesis_exit_carries_no_cooldown_at_all(book):
    db, pf = book
    _open_one(db, pf)
    # Rank collapse, not a level break: nothing for price to reclaim.
    confirm_ticks(pf, db, start=AFTER_HOLD, price_fn=marks(AAA=101.0),
                  candidates=[read("AAA", 20, entry=101.0)])
    assert store.load_positions(pf.portfolio_id, db_path=db) == []

    # The read recovers. The 60/40 hysteresis is the churn guard here, so
    # there is no extra waiting period to serve.
    t2 = AFTER_HOLD + timedelta(days=3)
    r = run_tick(pf.portfolio_id, db_path=db, now=t2, price_fn=marks(AAA=101.0),
                 candidates=[read("AAA", 85, entry=101.0, scored_at=t2.isoformat())])
    fills = [d for d in decisions_for(r, "AAA") if d.executed]
    assert fills and fills[0].action == Action.OPEN
    assert fills[0].intent == Intent.CLEARED_BAR


def test_a_name_that_keeps_stopping_out_is_benched(book):
    db, pf = book
    t = _stop_out(db, pf, entry=100.0, stop_price=95.0)
    # Reclaim and stop out a second time.
    t2 = t + timedelta(hours=6)
    run_tick(pf.portfolio_id, db_path=db, now=t2, price_fn=marks(AAA=103.0),
             candidates=[read("AAA", 80, entry=103.0, stop=99.0, target=130.0,
                              scored_at=t2.isoformat())])
    t3 = t2 + timedelta(hours=6)
    run_tick(pf.portfolio_id, db_path=db, now=t3, price_fn=marks(AAA=94.0),
             candidates=[read("AAA", 80, entry=103.0, stop=99.0, target=130.0,
                              scored_at=t3.isoformat())])
    assert store.load_positions(pf.portfolio_id, db_path=db) == []

    # Third reclaim inside the window: the book stops paying to find out.
    t4 = t3 + timedelta(hours=6)
    r = run_tick(pf.portfolio_id, db_path=db, now=t4, price_fn=marks(AAA=110.0),
                 candidates=[read("AAA", 80, entry=110.0, stop=106.0, target=140.0,
                                  scored_at=t4.isoformat())])
    (d,) = decisions_for(r, "AAA")
    assert d.action == Action.REJECT and d.blocker == Blocker.CHOPPING
    assert "2 times" in d.headline


def test_the_cooldown_expires_with_the_window(book):
    db, pf = book
    _stop_out(db, pf, entry=100.0, stop_price=95.0)
    # Well past the window, still below the old entry — no longer relevant.
    later = NOW + timedelta(days=10)
    r = run_tick(
        pf.portfolio_id, db_path=db, now=later, price_fn=marks(AAA=94.0),
        candidates=[read("AAA", 80, entry=94.0, stop=90.5, target=130.0,
                         scored_at=later.isoformat())],
    )
    fills = [d for d in decisions_for(r, "AAA") if d.executed]
    assert fills and fills[0].action == Action.OPEN


# ── The composed view ─────────────────────────────────────────────────


def _valuation(ticker, support, *, target=130.0, agent_target=130.0,
               source="agent", blockers=(), opposes=()):
    from macro_positioning.paper.valuation import Evidence, TradeValuation

    return TradeValuation(
        ticker=ticker, side="LONG", support=support,
        evidence=[Evidence("regime", "opposes", -12.0, d) for d in opposes],
        target=target, agent_target=agent_target, target_source=source,
        blockers=list(blockers),
    )


def test_an_unsupported_target_is_refused_and_says_which_views_dissent(book):
    db, pf = book
    r = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0),
        candidates=[read("AAA", 90)],
        valuations_in={"AAA": _valuation(
            "AAA", 28.0, blockers=["target support 28/100 is under the 35 bar"])},
    )
    (d,) = decisions_for(r, "AAA")
    assert d.action == Action.REJECT and d.blocker == Blocker.UNSUPPORTED_TARGET
    assert "28/100" in d.headline
    assert not r.fills


def test_a_well_supported_target_sizes_bigger_than_a_projected_one(book, tmp_path):
    db, pf = book
    strong = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, dry_run=True,
        price_fn=marks(AAA=100.0), candidates=[read("AAA", 80)],
        valuations_in={"AAA": _valuation("AAA", 90.0)},
    )
    weak = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, dry_run=True,
        price_fn=marks(AAA=100.0), candidates=[read("AAA", 80)],
        valuations_in={"AAA": _valuation("AAA", 40.0)},
    )
    s = [d for d in strong.decisions if d.executed][0]
    w = [d for d in weak.decisions if d.executed][0]
    assert s.notional > w.notional
    # And the size is explained, not just different.
    comps = [c["name"] for c in s.rationale["rank"]["components"]]
    assert "composition" in comps


def test_a_composed_target_overrides_the_agents(book):
    db, pf = book
    run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0),
        candidates=[read("AAA", 80, target=130.0)],
        valuations_in={"AAA": _valuation(
            "AAA", 70.0, target=118.0, agent_target=130.0, source="structure")},
    )
    p = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert p.target == pytest.approx(118.0), "the tested level beats the projection"
    assert "structure" in (p.thesis or "")
    assert p.source["targetSupport"] == pytest.approx(70.0)


def test_a_collapsed_case_closes_the_position_after_the_minimum_hold(book):
    db, pf = book
    run_tick(pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0),
             candidates=[read("AAA", 80)],
             valuations_in={"AAA": _valuation("AAA", 70.0)})

    collapsed = {"AAA": _valuation("AAA", 10.0,
                                   opposes=["regime flipped against the long"])}
    # Inside the minimum hold, an opinion does not get to close a position.
    t1 = NOW + timedelta(days=1)
    r1 = run_tick(pf.portfolio_id, db_path=db, now=t1, price_fn=marks(AAA=101.0),
                  candidates=[read("AAA", 80, entry=101.0, scored_at=t1.isoformat())],
                  valuations_in=collapsed)
    assert not [d for d in decisions_for(r1, "AAA") if d.action == Action.EXIT]

    # Past it, and persisting for the confirmation window, the collapse is
    # actionable — and names the dissent.
    r2 = confirm_ticks(pf, db, start=AFTER_HOLD, price_fn=marks(AAA=101.0),
                       candidates=[read("AAA", 80, entry=101.0)],
                       valuations_in=collapsed)
    exits = [d for d in decisions_for(r2, "AAA") if d.action == Action.EXIT]
    assert exits and exits[0].intent == Intent.SUPPORT_COLLAPSED
    assert "regime flipped" in exits[0].headline
    assert "70" in exits[0].headline, "the collapse is measured from the entry case"


def test_a_missing_valuation_never_blocks_the_tick(book):
    db, pf = book
    r = run_tick(pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0),
                 candidates=[read("AAA", 80)], valuations_in={})
    assert r.fills, "no composed view is a thinner decision, not a stopped book"
    assert r.error is None


def test_a_stop_inside_the_overnight_range_is_refused(book):
    db, pf = book
    # 0.3% stop on a book that checks price twice a day: AVAX, Sep 15, −5.8R.
    r = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0),
        candidates=[read("AAA", 90, entry=100.0, stop=99.7, target=130.0)],
    )
    (d,) = decisions_for(r, "AAA")
    assert d.action == Action.REJECT and d.blocker == Blocker.STOP_TOO_TIGHT
    assert "0.30%" in d.headline
    # A 1.5% stop is the floor and passes.
    r2 = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(BBB=100.0),
        candidates=[read("BBB", 90, entry=100.0, stop=98.5, target=130.0)],
    )
    assert [d for d in decisions_for(r2, "BBB") if d.executed]


def test_a_stop_wider_than_five_percent_is_refused(book):
    db, pf = book
    r = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0),
        candidates=[read("AAA", 90, entry=100.0, stop=93.0, target=130.0)],   # 7% stop
    )
    (d,) = decisions_for(r, "AAA")
    assert d.action == Action.REJECT and d.blocker == Blocker.STOP_TOO_WIDE
    assert "7.0%" in d.headline
    # Exactly 5% is inside the cap.
    r2 = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(BBB=100.0),
        candidates=[read("BBB", 90, entry=100.0, stop=95.0, target=130.0)],
    )
    assert [d for d in decisions_for(r2, "BBB") if d.executed]


# ── Horizon ───────────────────────────────────────────────────────────


def test_horizon_comes_from_the_signals_not_the_mandate():
    from macro_positioning.paper.horizon import derive_horizon

    m = Mandate()
    swing = derive_horizon({"signal_aggregate": {"dominant_horizon": "swing"}}, m)
    strat = derive_horizon({"signal_aggregate": {"dominant_horizon": "strategic"}}, m)
    none = derive_horizon({"signal_aggregate": {}}, m)
    assert swing.expected_days < none.expected_days < strat.expected_days
    assert swing.min_hold_days < strat.min_hold_days
    assert swing.confirm_ticks < strat.confirm_ticks
    # Bounds hold whatever the signal says.
    assert m.min_hold_bounds[0] <= strat.min_hold_days <= m.min_hold_bounds[1]
    assert m.confirm_ticks_bounds[0] <= strat.confirm_ticks <= m.confirm_ticks_bounds[1]
    assert any("signals read strategic" in b for b in strat.basis)


def test_a_breakout_holds_longer_than_mechanical_rails():
    from macro_positioning.paper.horizon import derive_horizon

    m = Mandate()
    row = {"signal_aggregate": {"dominant_horizon": "swing"}}
    bo = derive_horizon({**row, "levelMethod": "breakout_20d"}, m)
    mech = derive_horizon({**row, "levelMethod": "mechanical_v0"}, m)
    assert bo.expected_days > mech.expected_days


def test_the_sleeve_record_only_speaks_once_it_has_thirty_trades():
    from macro_positioning.paper.horizon import derive_horizon

    m = Mandate()
    row = {"signal_aggregate": {"dominant_horizon": "swing"}}
    base = derive_horizon(row, m).expected_days
    thin = derive_horizon(row, m, sleeve_prior_days=60.0, sleeve_prior_n=7)
    assert thin.expected_days == pytest.approx(base), "n=7 is not a record yet"
    full = derive_horizon(row, m, sleeve_prior_days=60.0, sleeve_prior_n=30)
    assert base < full.expected_days < 60.0, "at n=30 the record pulls halfway"


def test_the_horizon_is_banked_on_the_position_at_fill(book):
    db, pf = book
    r = read("AAA", 80)
    r.source_row["signal_aggregate"] = {"dominant_horizon": "position"}
    r.source_row["levelMethod"] = "breakout_20d"
    run_tick(pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0), candidates=[r])
    p = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert p.expected_hold_days == pytest.approx(30.0 * 1.25)
    assert p.min_hold_days == pytest.approx(min(21.0, 37.5 * 0.4))
    assert p.confirm_ticks == 8
    # And a later wobbly read does not shorten it.
    r2 = read("AAA", 80, entry=101.0)
    r2.source_row["signal_aggregate"] = {"dominant_horizon": "intraday"}
    run_tick(pf.portfolio_id, db_path=db, now=NOW + timedelta(days=1),
             price_fn=marks(AAA=101.0), candidates=[r2])
    p2 = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert p2.expected_hold_days == p.expected_hold_days


def test_a_position_the_ladder_reduced_is_not_topped_back_up(book):
    db, pf = book
    _open_one(db, pf, target=1000.0)
    up = read("AAA", 80, entry=200.0, stop=192.0, target=1000.0)
    settle(pf, db, start=NOW + timedelta(days=1), candidates=[up], price_fn=marks(AAA=200.0))
    held = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert held.rungs_taken == 3
    assert held.stop == pytest.approx(held.avg_price + 2 * held.initial_risk, rel=1e-6)
    # The entry pass must not refill it, and must say why.
    t = NOW + timedelta(days=6)
    r = run_tick(pf.portfolio_id, db_path=db, now=t, price_fn=marks(AAA=200.0),
                 candidates=[read("AAA", 95, entry=200.0, stop=192.0, target=1000.0,
                                  scored_at=t.isoformat())])
    rej = [d for d in decisions_for(r, "AAA") if d.action == Action.REJECT]
    assert rej and rej[0].blocker == Blocker.PROFIT_TAKEN
    after = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert after.qty == pytest.approx(held.qty)
    assert after.stop == pytest.approx(held.stop), "the locked stop survives"


# ── Exit paths ────────────────────────────────────────────────────────


def _open_target_path(db, pf, *, target=120.0):
    """A high-quality setup: rank >= 85 and support >= 60 → target path."""
    from macro_positioning.paper.valuation import TradeValuation
    r = read("AAA", 92, entry=100.0, stop=96.0, target=target)
    v = {"AAA": TradeValuation(ticker="AAA", side="LONG", support=75.0,
                               target=target, agent_target=target, target_source="structure")}
    run_tick(pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0),
             candidates=[r], valuations_in=v)
    p = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert p.exit_path == "target", p.exit_path
    return p


def test_a_high_quality_setup_is_held_for_its_target_not_laddered(book):
    db, pf = book
    p0 = _open_target_path(db, pf, target=120.0)
    # 2R up: a ladder position would have trimmed twice. This one holds.
    t1 = NOW + timedelta(days=1)
    r = run_tick(pf.portfolio_id, db_path=db, now=t1, price_fn=marks(AAA=108.0),
                 candidates=[read("AAA", 92, entry=108.0, stop=103.7, target=120.0,
                                  scored_at=t1.isoformat())])
    assert not [d for d in decisions_for(r, "AAA") if d.executed]
    assert store.load_positions(pf.portfolio_id, db_path=db)[0].qty == pytest.approx(p0.qty)


def test_reaching_the_target_closes_the_trade_in_full(book):
    db, pf = book
    _open_target_path(db, pf, target=120.0)
    t1 = NOW + timedelta(days=1)
    r = run_tick(pf.portfolio_id, db_path=db, now=t1, price_fn=marks(AAA=120.5),
                 candidates=[read("AAA", 92, entry=120.5, stop=115.7, target=120.0,
                                  scored_at=t1.isoformat())])
    (d,) = [x for x in decisions_for(r, "AAA") if x.executed]
    assert d.action == Action.EXIT and d.intent == Intent.TARGET_REACHED
    assert "structure target" in d.headline
    assert store.load_positions(pf.portfolio_id, db_path=db) == []


def test_a_near_miss_that_turns_is_closed_rather_than_ridden_back(book):
    db, pf = book
    _open_target_path(db, pf, target=120.0)     # 4-point risk; 5R to target
    # 94% of the way there (118.8) — arms the near-miss — then turns.
    t1 = NOW + timedelta(days=1)
    run_tick(pf.portfolio_id, db_path=db, now=t1, price_fn=marks(AAA=118.8),
             candidates=[read("AAA", 92, entry=118.8, stop=114.0, target=120.0,
                              scored_at=t1.isoformat())])
    p = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert p.near_miss_armed is True
    # 0.5R back off the high (2 points) without ever crossing 120.
    t2 = NOW + timedelta(days=2)
    r = run_tick(pf.portfolio_id, db_path=db, now=t2, price_fn=marks(AAA=116.5),
                 candidates=[read("AAA", 92, entry=116.5, stop=111.8, target=120.0,
                                  scored_at=t2.isoformat())])
    (d,) = [x for x in decisions_for(r, "AAA") if x.executed]
    assert d.action == Action.EXIT and d.intent == Intent.NEAR_MISS
    assert "94%" in d.headline and "turned" in d.headline


def test_a_lower_quality_setup_takes_the_ladder(book):
    db, pf = book
    from macro_positioning.paper.valuation import TradeValuation
    # Rank clears the bar for the book but not for the target path.
    r = read("AAA", 70, entry=100.0, stop=96.0, target=130.0)
    v = {"AAA": TradeValuation(ticker="AAA", side="LONG", support=75.0,
                               target=130.0, agent_target=130.0)}
    run_tick(pf.portfolio_id, db_path=db, now=NOW, price_fn=marks(AAA=100.0),
             candidates=[r], valuations_in=v)
    p = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert p.exit_path == "ladder"
