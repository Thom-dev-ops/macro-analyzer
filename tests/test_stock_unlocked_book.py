"""The Stock Unlocked book's candidate screen, offline (marks injected)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from macro_positioning.paper.stock_unlocked_book import (
    book_candidates, load_book_mandate, load_source_config,
)
from macro_positioning.tracker.stock_unlocked import TrackedCall

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
CFG = Path(__file__).resolve().parents[1] / "config" / "paper_stock_unlocked.json"


def _call(**kw) -> TrackedCall:
    base = dict(
        call_id="d", posted_at=NOW - timedelta(hours=6), ticker="DDD", instrument="stock",
        trade_kind="swing", direction="long", entry=3.56, stop=3.40,
        targets=[3.66, 3.76, 3.86, 3.96], notes=None, option=None, market_price=3.56,
        verdict="open", desk_verdict="open",
    )
    base.update(kw)
    return TrackedCall(**base)


def _run(calls, marks):
    return book_candidates(
        load_book_mandate(CFG), config=load_source_config(CFG), calls=calls, now=NOW,
        mark_fn=lambda sym: marks.get(sym),
    )


def test_fresh_levelled_call_becomes_a_candidate_with_the_last_target():
    reads, cov = _run([_call()], {"DDD": 3.58})
    assert cov.candidates == 1
    r = reads[0]
    assert r.ticker == "DDD" and r.side == "LONG" and r.value >= 60
    assert r.source_row["target"] == 3.96
    assert r.source_row["signal_aggregate"]["dominant_horizon"] == "swing"


def test_options_and_closed_calls_are_not_candidates():
    calls = [
        _call(call_id="o", ticker="TSLA", instrument="option", entry=None, stop=None,
              targets=[], option={"type": "CALL", "strike": 380, "premium": 2.8}),
        _call(call_id="s", ticker="SATL", verdict="loss", realized_r=-1.0),
        _call(call_id="w", ticker="ARB", instrument="crypto", desk_verdict="win",
              desk_max_target=4, entry=0.161, stop=0.143, targets=[0.172, 0.179, 0.188, 0.2]),
    ]
    reads, cov = _run(calls, {"SATL": 5.4, "ARB": 0.22})
    assert cov.candidates == 0
    assert cov.skipped == {"option": 1, "tape_resolved": 1, "desk_closed": 1}
    # ...but the closed ones are handed to the engine at rank 0 so a held
    # position learns to leave.
    zeros = {r.ticker: r for r in reads if r.value == 0.0}
    assert set(zeros) == {"SATL", "ARB"}
    assert "final target" in zeros["ARB"].components[0].reason
    assert zeros["ARB"].source_row["hasLevels"] is False


def test_a_call_that_has_run_past_its_reward_is_not_chased():
    _, cov = _run([_call()], {"DDD": 3.90})       # 0.06 left against 0.50 risk
    assert cov.skipped.get("rr_collapsed") == 1


def test_stale_and_day_trade_calls_rank_lower():
    fresh, _ = _run([_call()], {"DDD": 3.58})
    stale, _ = _run([_call(posted_at=NOW - timedelta(days=4))], {"DDD": 3.58})
    day, _ = _run([_call(trade_kind="day")], {"DDD": 3.58})
    assert stale[0].value < fresh[0].value
    assert day[0].value < fresh[0].value
    assert day[0].source_row["signal_aggregate"]["dominant_horizon"] is None


def test_slice_edge_tilts_once_the_slice_has_ten_resolved_calls():
    history = [
        _call(call_id=f"h{i}", ticker=f"H{i}", posted_at=NOW - timedelta(days=30 + i),
              verdict="win", realized_r=1.5, resolved_at=NOW - timedelta(days=29 + i))
        for i in range(10)
    ]
    reads, _ = _run(history + [_call()], {"DDD": 3.58})
    edge = next(c for c in reads[0].components if c.name == "slice_edge")
    assert edge.delta > 0 and "stock/swing" in edge.reason
