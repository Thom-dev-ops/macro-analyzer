"""Tests for the cohort book's candidate source.

The engine is already covered by `test_paper_engine.py`; nothing here
re-tests sizing or exits. What these assert is the thing the cohort book
adds — **the screens between a posted chart and a position**, and the
rank built out of the call itself.

Every test writes synthetic signals to a throwaway DB and passes its own
`mark_fn`, so no test touches the network, the live DB, or the real
roster. The roster used here is a two-author fixture, on purpose: a test
that asserted against `config/paper_cohort.json` would start failing the
day someone adds a name to the cohort, which is a config edit and not a
regression.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from macro_positioning.db.schema import initialize_database
from macro_positioning.paper import store
from macro_positioning.paper.cohort import (
    Coverage,
    cohort_candidates,
    load_cohort_config,
    load_cohort_mandate,
    reset_cohort_cache,
)
from macro_positioning.paper.engine import run_tick
from macro_positioning.paper.vocabulary import Action, Intent


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

NUTS = "feather-hands:big-nuts"
FHT = "feather-hands-trading:feather-hands-trading"


# ── Fixtures ──────────────────────────────────────────────────────────


def _config_file(tmp_path: Path, **overrides) -> Path:
    """A two-author cohort config. Mandate blocks are left out so the
    Mandate dataclass defaults apply — this file is testing the screens,
    not the mandate."""
    cohort = {
        "label": "Test Cohort",
        "authors": [
            {"author_id": NUTS, "display": "Big_Nuts"},
            {"author_id": FHT, "display": "Feather Hands Trading"},
        ],
        "lookback_days": 10,
        "call_types": ["directional_long", "directional_short"],
        "require_trigger": True,
        "min_live_rr": 1.2,
        "max_candidates": 60,
    }
    cohort.update(overrides)
    p = tmp_path / "cohort.json"
    p.write_text(json.dumps({"cohort": cohort, "rank_model": {}}), encoding="utf-8")
    reset_cohort_cache()
    return p


@pytest.fixture
def db(tmp_path: Path) -> Path:
    d = tmp_path / "cohort.db"
    initialize_database(d)
    return d


def call(
    db: Path,
    ticker: str,
    *,
    side: str = "LONG",
    entry: float = 100.0,
    stop: float | None = 90.0,
    target: float = 130.0,
    conviction: float = 3.0,
    call_type: str | None = None,
    stage: str = "active",
    confluence: int = 3,
    author: str = NUTS,
    age_days: float = 0.5,
    signal_id: str | None = None,
) -> str:
    """Write one extracted chart call, the way the manual pipeline does."""
    sid = signal_id or f"sig_{ticker}_{side}_{age_days}_{author[-4:]}_{entry}"
    at = (NOW - timedelta(days=age_days)).isoformat()
    detail = {
        "call_type": call_type or ("directional_long" if side == "LONG" else "directional_short"),
        "trade_stage": stage,
        "confluence_score": confluence,
        "chart_timeframe": "4H",
        "pattern": "Bull flag",
    }
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            """
            INSERT INTO signals
                (signal_id, document_id, extracted_at, asset_ticker, side, conviction,
                 entry_zone_low, stop_loss, target_1, instrument_detail_json,
                 source_slug, source_channel, author_id, extractor_name,
                 extractor_version, status, horizon)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', 'Feather Hands', ?,
                    'manual_chart_extractor', 'v1', 'active', 'swing')
            """,
            (sid, f"doc_{sid}", at, ticker, side, conviction, entry, stop, target,
             json.dumps(detail), author),
        )
    conn.close()
    return sid


def outcomes(db: Path, author: str, *, n: int, alpha: float) -> None:
    """Seed the backtest table the author-edge component reads.

    `call_outcomes` is owned by `learning/call_accuracy.py`, not by
    `db/schema.py` — the schema module creates the table without
    `alpha_pct` and the backtester adds it. Going through the owner's
    ensure-table keeps the fixture on the same definition the real column
    comes from.
    """
    from macro_positioning.learning.call_accuracy import _ensure_table

    conn = sqlite3.connect(db)
    with conn:
        _ensure_table(conn)
        conn.executemany(
            """
            INSERT INTO call_outcomes
                (document_id, author_id, alpha_pct, resolved, scored_at)
            VALUES (?, ?, ?, 'win', ?)
            """,
            [(f"oc_{author}_{i}", author, alpha, NOW.isoformat()) for i in range(n)],
        )
    conn.close()


def load(db: Path, cfg_path: Path, marks: dict[str, float], **kw):
    return cohort_candidates(
        load_cohort_mandate(cfg_path),
        db_path=db,
        now=NOW,
        config=load_cohort_config(cfg_path),
        mark_fn=lambda sym: marks.get(sym.upper()),
        **kw,
    )


def component(read, name: str):
    return next((c for c in read.components if c.name == name), None)


# ── The screens ───────────────────────────────────────────────────────


def test_a_directional_call_becomes_a_candidate(db, tmp_path):
    cfg = _config_file(tmp_path)
    call(db, "SOL", entry=100, stop=90, target=130)
    reads, cov = load(db, cfg, {"SOL": 105})

    assert [r.ticker for r in reads] == ["SOL"]
    assert reads[0].side == "LONG"
    row = reads[0].source_row
    # The book takes the CALL's levels, not a level of its own devising.
    assert (row["stop"], row["target"]) == (90.0, 130.0)
    assert row["cohort"]["author"] == "Big_Nuts"
    assert cov.candidates == 1


@pytest.mark.parametrize("call_type", ["bidirectional", "no_trade", "not_a_chart"])
def test_a_call_with_no_side_is_never_traded(db, tmp_path, call_type):
    """Reading a direction out of a chart that did not state one is how a
    fake bias gets into the book. These are counted, not inferred."""
    cfg = _config_file(tmp_path)
    call(db, "BTC", call_type=call_type)
    reads, cov = load(db, cfg, {"BTC": 105})

    assert reads == []
    assert cov.skipped["not_directional"] == 1


def test_an_unpriceable_call_is_counted_not_silently_dropped(db, tmp_path):
    """Most of this cohort's flow is DEX pairs nothing can mark. The book
    is only honest if it says so — 'two fills' means nothing without the
    denominator."""
    cfg = _config_file(tmp_path)
    call(db, "KINS/SOL")
    call(db, "JOTCHUA/USDC")
    call(db, "KINS/SOL", age_days=1.5, entry=101)
    reads, cov = load(db, cfg, {})

    assert reads == []
    assert cov.skipped["unpriceable"] == 3
    assert ("KINS/SOL", 2) in cov.unpriceable_tickers
    assert cov.priced_pct == 0.0


def test_a_watching_call_waits_for_its_own_entry(db, tmp_path):
    """'Wants a breakout above 105' is a plan. Buying it at 98 is buying a
    trade its author has not taken."""
    cfg = _config_file(tmp_path)
    call(db, "ETH", entry=105, stop=95, target=140, stage="watching")

    below, cov = load(db, cfg, {"ETH": 98})
    assert below == []
    assert cov.skipped["not_triggered"] == 1

    above, _ = load(db, cfg, {"ETH": 106})
    assert [r.ticker for r in above] == ["ETH"]


def test_a_live_call_skips_the_trigger_check(db, tmp_path):
    """The author says they are in it. Their own money is the evidence."""
    cfg = _config_file(tmp_path)
    call(db, "ETH", entry=105, stop=95, target=140, stage="active")
    reads, _ = load(db, cfg, {"ETH": 100})
    assert [r.ticker for r in reads] == ["ETH"]


def test_a_setup_that_has_already_run_is_not_chased(db, tmp_path):
    """A 3R call bought at 0.8R is a different trade with the same levels."""
    cfg = _config_file(tmp_path)
    call(db, "HYPE", entry=100, stop=90, target=130)

    chased, cov = load(db, cfg, {"HYPE": 125})     # 0.5R left
    assert chased == []
    assert cov.skipped["rr_collapsed"] == 1

    fresh, _ = load(db, cfg, {"HYPE": 102})        # still ~2.3R
    assert [r.ticker for r in fresh] == ["HYPE"]


def test_a_call_whose_stop_already_went_is_over(db, tmp_path):
    cfg = _config_file(tmp_path)
    call(db, "SOL", entry=100, stop=90, target=130)
    reads, cov = load(db, cfg, {"SOL": 89})
    assert reads == []
    assert cov.skipped["stop_breached"] == 1


def test_a_backwards_extraction_is_refused(db, tmp_path):
    """A long whose stop sits above its entry has its risk and its reward
    pointing the same way. That is a misread chart, not a trade."""
    cfg = _config_file(tmp_path)
    call(db, "MSTR", side="LONG", entry=100, stop=110, target=130)
    reads, cov = load(db, cfg, {"MSTR": 105})
    assert reads == []
    assert cov.skipped["levels_incoherent"] == 1


def test_the_freshest_call_on_a_name_wins(db, tmp_path):
    """A cohort re-calling a name is updating it, not adding to it."""
    cfg = _config_file(tmp_path)
    call(db, "SOL", entry=100, stop=90, target=130, age_days=4)
    call(db, "SOL", entry=101, stop=95, target=140, age_days=0.5)
    reads, cov = load(db, cfg, {"SOL": 102})

    assert len(reads) == 1
    assert reads[0].source_row["stop"] == 95.0
    assert cov.skipped["superseded"] == 1


def test_two_pairs_on_the_same_coin_are_one_position(db, tmp_path):
    """BTC/USD and BTCUSDT are the same bet. The book holds it once."""
    cfg = _config_file(tmp_path)
    call(db, "BTC/USD", entry=100, stop=90, target=130, age_days=0.5)
    call(db, "BTCUSDT", entry=100, stop=90, target=130, age_days=2, author=FHT)
    reads, _ = load(db, cfg, {"BTC": 105})
    assert [r.ticker for r in reads] == ["BTC"]


# ── The rank ──────────────────────────────────────────────────────────


def test_conviction_and_confluence_move_the_rank(db, tmp_path):
    cfg = _config_file(tmp_path)
    call(db, "SOL", conviction=5.0, confluence=5)
    call(db, "ETH", conviction=1.0, confluence=1)
    reads, _ = load(db, cfg, {"SOL": 105, "ETH": 105})

    ranked = {r.ticker: r.value for r in reads}
    assert ranked["SOL"] > ranked["ETH"]
    assert component(reads[0], "conviction").delta > 0
    assert [r.ticker for r in reads][0] == "SOL"     # sorted strongest first


def test_the_room_agreeing_is_worth_rank_points(db, tmp_path):
    cfg = _config_file(tmp_path)
    call(db, "SOL", author=NUTS, age_days=0.5)
    call(db, "SOL", author=FHT, age_days=1.0)
    reads, _ = load(db, cfg, {"SOL": 105})

    agreement = component(reads[0], "agreement")
    assert agreement is not None and agreement.delta > 0
    assert "2 of the cohort" in agreement.reason


def test_the_room_disagreeing_costs_more_than_agreeing_pays(db, tmp_path):
    """One long and one short on the same name is not a consensus trade,
    and the book should not be sized as though it were."""
    cfg = _config_file(tmp_path)
    call(db, "SOL", side="LONG", entry=100, stop=90, target=130, author=NUTS, age_days=0.5)
    call(db, "SOL", side="SHORT", entry=100, stop=110, target=70, author=FHT, age_days=1.0)
    reads, _ = load(db, cfg, {"SOL": 105})

    opposition = component(reads[0], "opposition")
    assert opposition is not None and opposition.delta < 0
    assert abs(opposition.delta) > 10.0


def test_a_stale_call_decays(db, tmp_path):
    cfg = _config_file(tmp_path)
    call(db, "SOL", age_days=0.5)
    call(db, "ETH", age_days=8)
    reads, _ = load(db, cfg, {"SOL": 105, "ETH": 105})

    by_ticker = {r.ticker: r for r in reads}
    assert component(by_ticker["SOL"], "freshness") is None
    assert component(by_ticker["ETH"], "freshness").delta < 0
    assert by_ticker["SOL"].value > by_ticker["ETH"].value


def test_a_thin_record_does_not_count_as_an_edge(db, tmp_path):
    """Ten scored calls is not a measurement. The component says so
    instead of quietly assuming an edge."""
    cfg = _config_file(tmp_path)
    call(db, "SOL")
    outcomes(db, NUTS, n=10, alpha=4.0)
    reads, _ = load(db, cfg, {"SOL": 105})

    edge = component(reads[0], "author_edge")
    assert edge.delta == 0.0
    assert "under the" in edge.reason


def test_a_measured_edge_tilts_the_rank_without_deciding_it(db, tmp_path):
    cfg = _config_file(tmp_path)
    call(db, "SOL")
    outcomes(db, NUTS, n=100, alpha=5.0)
    reads, _ = load(db, cfg, {"SOL": 105})

    edge = component(reads[0], "author_edge")
    assert 0 < edge.delta <= 8.0
    assert "alpha" in edge.reason


def test_the_rank_is_never_anchored_to_a_distribution(db, tmp_path):
    """There is no universe to rank a Telegram post against, and the read
    says so rather than implying a percentile it does not have."""
    cfg = _config_file(tmp_path)
    call(db, "SOL")
    reads, _ = load(db, cfg, {"SOL": 105})
    assert reads[0].anchored is False
    assert reads[0].score is None


# ── The mandate, and the book it makes ────────────────────────────────


def test_the_shipped_mandate_takes_the_call_as_given():
    """The composed view is what the SIGNAL book adds. Running it here
    would fold the desk's opinion back into the book whose whole job is to
    measure the cohort without it."""
    m = load_cohort_mandate()
    assert m.use_composed_levels is False


def test_the_shipped_mandate_does_not_cap_away_its_own_universe():
    """The signal book's 20%/3-per-bucket caps stop a diversified book
    becoming one bet. This book IS one bet — crypto — and those caps would
    stop it trading after three names."""
    m = load_cohort_mandate()
    assert m.max_bucket_pct > 0.2
    assert m.max_positions_per_bucket > 3


def test_the_shipped_roster_is_the_feather_hands_crowd():
    cfg = load_cohort_config()
    ids = set(cfg.author_ids)
    assert "feather-hands:big-nuts" in ids
    assert "feather-hands-trading:feather-hands-trading" in ids
    # Other desks are deliberately out — blending five of them into one
    # equity curve loses whose calls made the money.
    assert not any("wolf-pack" in i or "gem-hunters" in i for i in ids)


def test_a_cohort_call_opens_a_position_on_its_own_levels(db, tmp_path):
    """End to end: the loader's reads drive the shared engine, and the
    position the engine opens carries the CALL's stop and target."""
    cfg = _config_file(tmp_path)
    # The 5% hard cap is measured from the FILL (102 here), not the call's
    # entry — the risk the book takes is from where it actually buys.
    call(db, "SOL", entry=100, stop=97.5, target=130, conviction=5.0, confluence=5)
    reads, _ = load(db, cfg, {"SOL": 102})
    pf = store.create_portfolio(
        name="Cohort Book — Feather Hands",
        mandate=load_cohort_mandate(cfg),
        db_path=db,
    )

    result = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, candidates=reads, valuations_in={},
        price_fn=lambda tickers: {"SOL": {"price": 102.0, "source": "test"}},
    )

    opened = [d for d in result.decisions if d.action == Action.OPEN]
    assert [d.ticker for d in opened] == ["SOL"]
    position = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert (position.stop, position.target) == (97.5, 130.0)
    assert "Big_Nuts" in (position.thesis or "")


def test_coverage_reports_the_denominator_not_just_the_fills(db, tmp_path):
    """The funnel is a first-class output. A tick that opened nothing
    because it could price nothing is a different result from a tick that
    looked at everything and liked none of it."""
    cfg = _config_file(tmp_path)
    call(db, "SOL", entry=100, stop=90, target=130)
    call(db, "KINS/SOL")
    call(db, "BTC", call_type="bidirectional")
    _, cov = load(db, cfg, {"SOL": 102})

    assert cov.considered == 3
    assert cov.candidates == 1
    assert cov.skipped["unpriceable"] == 1
    assert cov.skipped["not_directional"] == 1
    # 2 directional calls, 1 of them unpriceable.
    assert cov.priced_pct == 50.0
    d = cov.as_dict()
    assert d["pricedPct"] == 50.0
    assert {s["reason"] for s in d["skipped"]} <= set(Coverage.REASONS)
    assert "unpriceable" in cov.report()


# ── The author's stop, held as a resting order ────────────────────────
#
# `author_stops_only` is what makes this book a copy-trader on the risk
# side too. Two halves, tested separately:
#   · the stop FIRES on the traded range and fills AT the level, not on
#     the twice-daily mark at wherever the tape has got to;
#   · the engine never moves it — no breakeven ratchet at the first rung.
#
# The case behind it: Big_Nuts short ETH at 2446 with a stop at 2530. The
# tape pierced 2530 on 2026-09-04 and did not CLOSE above it until
# 2026-09-18 at 2611. Mark-priced, that call scores −3.75R. Held as the
# resting stop the author drew, it is −1R. Only the second number is a
# measurement of the call rather than of the book's polling interval.


def _book(db: Path, tmp_path: Path, *, author_stops: bool):
    import dataclasses
    m = load_cohort_mandate(_config_file(tmp_path))
    m = dataclasses.replace(m, author_stops_only=author_stops)
    return store.create_portfolio(
        name="Cohort Book — Feather Hands", mandate=m, db_path=db,
    )


def _eth_short(db: Path, pf) -> str:
    """A short at 2446 with the author's 2530 stop, already open."""
    from macro_positioning.paper.models import Position

    p = Position(
        position_id=store.new_position_id(), portfolio_id=pf.portfolio_id,
        ticker="ETH", side="SHORT", qty=1.0, avg_price=2446.0,
        opened_at="2026-09-01T12:00:00+00:00", status="open",
        stop=2530.0, target=2380.0, initial_risk=84.0,
        last_mark=2450.0, last_mark_at="2026-09-01T12:00:00+00:00",
        thesis="Big_Nuts · 4h · flat top wedge breakdown",
    )
    with store.connect(db) as conn:
        store.insert_position(conn, p)
    return p.position_id


def test_author_stop_fires_on_the_traded_range_and_fills_at_the_level(db, tmp_path):
    """Sep 4's CLOSE was 2456 — under the 2530 stop, so the mark rule sees
    nothing. Its HIGH was 2545.62. The stop fires anyway, and fills at 2530."""
    pf = _book(db, tmp_path, author_stops=True)
    _eth_short(db, pf)

    result = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, candidates=[], valuations_in={},
        price_fn=lambda t: {"ETH": {"price": 2456.08, "source": "test"}},
        range_fn=lambda t, since: {"ETH": {
            "high": 2545.62, "low": 2356.19, "bars": 3,
            "from": "2026-09-02", "to": "2026-09-04",
        }},
    )

    stops = [d for d in result.decisions if d.intent == Intent.STOP_HIT]
    assert len(stops) == 1, "the pierced stop must fire"
    order = [o for o in result.orders if o.intent == Intent.STOP_HIT][0]
    assert order.ref_price == 2530.0, "fills at the author's level, not the mark"
    # Slippage still applies — a resting stop is not a free fill.
    assert order.price > 2530.0
    assert order.realized_pnl < 0
    # ~−1R, not the −3.75R a 2611 mark fill would have booked.
    assert -1.2 < (order.realized_pnl / 84.0) < -1.0


def test_without_the_flag_the_same_position_survives_on_the_mark(db, tmp_path):
    """The default book prices stops off the mark — it only looks twice a
    day and does not pretend to have seen more than that."""
    pf = _book(db, tmp_path, author_stops=False)
    _eth_short(db, pf)

    result = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, candidates=[], valuations_in={},
        price_fn=lambda t: {"ETH": {"price": 2500.0, "source": "test"}},
        range_fn=lambda t, since: {"ETH": {
            "high": 2545.62, "low": 2356.19, "bars": 3,
            "from": "2026-09-02", "to": "2026-09-04",
        }},
    )
    assert not [d for d in result.decisions if d.intent == Intent.STOP_HIT]


def test_a_mark_through_the_stop_still_fires_under_author_stops(db, tmp_path):
    """The resting check is additive. A mark already past the stop is the
    plainest possible breach and must not be lost to it."""
    pf = _book(db, tmp_path, author_stops=True)
    _eth_short(db, pf)

    result = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, candidates=[], valuations_in={},
        price_fn=lambda t: {"ETH": {"price": 2540.0, "source": "test"}},
        range_fn=lambda t, since: {},          # no bars at all
    )
    stops = [d for d in result.decisions if d.intent == Intent.STOP_HIT]
    assert len(stops) == 1
    order = [o for o in result.orders if o.intent == Intent.STOP_HIT][0]
    assert order.ref_price == 2540.0, "no range to read — the mark is what was seen"


def test_the_engine_does_not_ratchet_an_author_stop_to_breakeven(db, tmp_path):
    """The first rung still takes its slice; the stop stays where Big_Nuts
    drew it. Moving it would replace the level being measured."""
    import dataclasses
    from macro_positioning.paper.models import Position

    pf = _book(db, tmp_path, author_stops=True)
    p = Position(
        position_id=store.new_position_id(), portfolio_id=pf.portfolio_id,
        ticker="SOL", side="LONG", qty=10.0, avg_price=100.0,
        opened_at="2026-08-01T12:00:00+00:00", status="open",
        stop=90.0, target=130.0, initial_risk=10.0,
        last_mark=100.0, last_mark_at="2026-08-01T12:00:00+00:00",
        exit_path="ladder", thesis="Big_Nuts · 1D",
    )
    with store.connect(db) as conn:
        store.insert_position(conn, p)

    # +1R: the first ladder rung trims 25% and, on the default book, would
    # pull the stop up to the 100.0 entry.
    run_tick(
        pf.portfolio_id, db_path=db, now=NOW, candidates=[], valuations_in={},
        price_fn=lambda t: {"SOL": {"price": 110.0, "source": "test"}},
        range_fn=lambda t, since: {},
    )
    after = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert after.rungs_taken >= 1, "the rung itself still fires"
    assert after.stop == 90.0, "the author's stop must not move to breakeven"


# ── No stop on the chart: the 5% house stop ───────────────────────────
#
# Plenty of this crowd's charts carry a target and no stop — the
# invalidation is obvious on the picture and never typed. Those used to be
# dropped as `no_levels`. They are now taken at a flat house stop 5%
# adverse of the price the book pays, tagged so the equity curve never
# credits the author with a level they did not draw.


def test_a_call_with_no_stop_is_taken_at_the_house_5pct(db, tmp_path):
    cfg = _config_file(tmp_path)
    call(db, "SOL", entry=100, stop=None, target=130, conviction=5.0, confluence=5)
    reads, cov = load(db, cfg, {"SOL": 102})

    assert cov.skipped.get("no_levels", 0) == 0, "a missing stop is no longer fatal"
    assert [r.ticker for r in reads] == ["SOL"]
    row = reads[0].source_row
    assert row["stopSource"] == "house_5.0%"
    # 5% adverse of the MARK the book pays, not of the author's entry.
    assert row["stop"] == pytest.approx(102 * 0.95)
    assert row["target"] == 130.0, "the author's target is untouched"


def test_a_stated_stop_is_never_replaced(db, tmp_path):
    cfg = _config_file(tmp_path)
    call(db, "SOL", entry=100, stop=97.5, target=130, conviction=5.0, confluence=5)
    reads, _ = load(db, cfg, {"SOL": 102})
    row = reads[0].source_row
    assert row["stopSource"] == "author"
    assert row["stop"] == 97.5


def test_a_target_inside_the_house_stop_is_not_a_trade(db, tmp_path):
    """No stop AND a target 2% away: 0.4R on a 5% house stop. The R:R
    screen must refuse it rather than the book opening a coin flip."""
    cfg = _config_file(tmp_path)
    call(db, "SOL", entry=100, stop=None, target=104, conviction=5.0, confluence=5)
    reads, cov = load(db, cfg, {"SOL": 102})
    assert reads == []
    assert sum(cov.skipped.values()) >= 1


def test_the_house_stop_survives_the_engines_width_cap(db, tmp_path):
    """A house stop sits exactly at max_stop_pct. The tick's mark is a
    second read of the same tape, so a hair of drift must not reject it as
    STOP_TOO_WIDE — the derivation already IS the cap."""
    cfg = _config_file(tmp_path)
    call(db, "SOL", entry=100, stop=None, target=140, conviction=5.0, confluence=5)
    reads, _ = load(db, cfg, {"SOL": 102})
    pf = store.create_portfolio(
        name="Cohort Book — Feather Hands",
        mandate=load_cohort_mandate(cfg), db_path=db,
    )

    result = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, candidates=reads, valuations_in={},
        # 101.5, not 102 — the drift that would put a 5% stop over a 5% cap.
        price_fn=lambda t: {"SOL": {"price": 101.5, "source": "test"}},
    )

    opened = [d for d in result.decisions if d.action == Action.OPEN]
    assert [d.ticker for d in opened] == ["SOL"]
    position = store.load_positions(pf.portfolio_id, db_path=db)[0]
    assert position.stop == pytest.approx(102 * 0.95)
    assert (position.source or {}).get("stopSource") == "house_5.0%"


def test_a_pierced_stop_fills_at_the_level_even_when_the_close_is_past_it(db, tmp_path):
    """HYPE, 2026-09-03: stop 87, intraday high 87.99, CLOSE 87.53.

    Both rules fire. The resting one has to win — the order filled at 87
    when the tape touched it, and 87.53 is just where price went next.
    Mark-first costs the book $5 of fill it never actually gave up, and
    turns a −1R stop into −1.2R on every pierce-and-close-through.
    """
    from macro_positioning.paper.models import Position

    pf = _book(db, tmp_path, author_stops=True)
    p = Position(
        position_id=store.new_position_id(), portfolio_id=pf.portfolio_id,
        ticker="HYPE", side="SHORT", qty=9.458, avg_price=84.13,
        opened_at="2026-09-01T12:00:00+00:00", status="open",
        stop=87.0, target=78.0, initial_risk=2.87,
        last_mark=84.0, last_mark_at="2026-09-01T12:00:00+00:00",
        thesis="Big_Nuts · 4h",
    )
    with store.connect(db) as conn:
        store.insert_position(conn, p)

    result = run_tick(
        pf.portfolio_id, db_path=db, now=NOW, candidates=[], valuations_in={},
        price_fn=lambda t: {"HYPE": {"price": 87.53, "source": "test"}},
        range_fn=lambda t, since: {"HYPE": {
            "high": 87.99, "low": 80.87, "bars": 2,
            "from": "2026-09-02", "to": "2026-09-03",
        }},
    )
    order = [o for o in result.orders if o.intent == Intent.STOP_HIT][0]
    assert order.ref_price == 87.0, "the resting stop filled before the close printed"
    # A stop that fills at its own level costs 1R plus the spread — never
    # more. Filling at the 87.53 close would have been ~1.2R.
    r = order.realized_pnl / (2.87 * 9.458)
    assert -1.1 < r < -1.0, f"expected about -1R, got {r:.2f}R"
