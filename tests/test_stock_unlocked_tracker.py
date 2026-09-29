"""The Stock Unlocked tracker's scoring and desk-vs-tape logic, offline.

Bars are injected by monkeypatching `_bars`, so nothing here touches
Coinbase or Yahoo. Each case is one of the measurement rules that moved
the win rate when it was wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from macro_positioning.tracker import stock_unlocked as su

T0 = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def _call(**kw) -> su.TrackedCall:
    base = dict(
        call_id="d1", posted_at=T0, ticker="XYZ", instrument="stock", trade_kind="day",
        direction="long", entry=10.0, stop=9.5, targets=[10.5, 11.0, 11.5], notes=None,
        option=None, market_price=10.0,
    )
    base.update(kw)
    return su.TrackedCall(**base)


def _patch_bars(monkeypatch, hourly, fine=None):
    def fake(call, start, end, fine_=False, **_):
        src = (fine if fine is not None else hourly) if fine_ else hourly
        call.price_source, call.price_symbol = "test", call.ticker
        return [b for b in src if start <= b[0] <= end]

    monkeypatch.setattr(su, "_bars", lambda call, start, end, fine=False: fake(call, start, end, fine))


def test_target_first_is_a_win_to_the_furthest_target(monkeypatch):
    h = T0 + timedelta(hours=3)
    bars = [
        (h, 10.0, 10.6, 9.9),                       # T1
        (h + timedelta(hours=1), 10.6, 11.1, 10.4), # T2
        (h + timedelta(hours=2), 11.0, 11.2, 9.4),  # then the stop
    ]
    _patch_bars(monkeypatch, bars)
    c = su.score_call(_call(), now=T0 + timedelta(days=1))
    assert c.verdict == "win"
    assert c.max_target == 2
    assert c.realized_r == pytest.approx(2.0)      # 11.0 - 10.0 over 0.5 risk
    assert c.mfe_pct == pytest.approx(12.0)         # measured to the stop, not beyond
    assert c.resolved_at == h


def test_stop_first_is_a_loss_even_if_targets_print_later(monkeypatch):
    h = T0 + timedelta(hours=3)
    bars = [(h, 10.0, 10.2, 9.4), (h + timedelta(hours=1), 9.5, 12.0, 9.5)]
    _patch_bars(monkeypatch, bars)
    c = su.score_call(_call(), now=T0 + timedelta(days=1))
    assert c.verdict == "loss" and c.realized_r == -1.0 and c.max_target == 0


def test_gap_through_the_stop_is_a_stop_out(monkeypatch):
    # KTOS 2026-08-06: one bar holds both levels at 1h and at 5m; the open
    # is below the stop, so the day's later high does not rescue it.
    h = T0 + timedelta(hours=3)
    both = [(h, 9.3, 11.0, 9.2)]
    _patch_bars(monkeypatch, both, fine=both)
    c = su.score_call(_call(), now=T0 + timedelta(days=1))
    assert c.verdict == "loss" and c.resolution == "gap_open"


def test_both_levels_in_one_bar_resolved_at_five_minutes(monkeypatch):
    h = T0 + timedelta(hours=3)
    hourly = [(h, 10.0, 11.0, 9.2)]
    fine = [(h, 10.0, 10.6, 9.9), (h + timedelta(minutes=5), 10.5, 10.5, 9.2)]
    _patch_bars(monkeypatch, hourly, fine=fine)
    c = su.score_call(_call(), now=T0 + timedelta(days=1))
    assert c.verdict == "win" and c.resolution == "5m"


def test_nothing_touched_is_open_then_unresolved(monkeypatch):
    bars = [(T0 + timedelta(hours=i), 10.0, 10.2, 9.8) for i in range(1, 30)]
    _patch_bars(monkeypatch, bars)
    assert su.score_call(_call(), now=T0 + timedelta(days=1)).verdict == "open"
    assert su.score_call(_call(), now=T0 + timedelta(days=4)).verdict == "unresolved"


def test_wrong_instrument_is_unpriceable(monkeypatch):
    # Yahoo's ARB-USD is not Arbitrum: first bar 0.0006 against a 0.161 entry.
    _patch_bars(monkeypatch, [(T0 + timedelta(hours=1), 0.0006, 0.0007, 0.0005)])
    c = su.score_call(_call(entry=0.161, stop=0.143, targets=[0.172]), now=T0 + timedelta(days=1))
    assert c.verdict == "unpriceable" and "wrong instrument" in (c.score_error or "")


def test_desk_events_route_to_the_latest_prior_call_and_derive_a_verdict():
    a = _call(call_id="a", posted_at=T0)
    b = _call(call_id="b", posted_at=T0 + timedelta(days=5))
    posts = [
        ("e1", T0 + timedelta(hours=2), {"post_kind": "target_hit", "ticker": "XYZ",
                                         "event": {"target_index": 1}}),
        ("e2", T0 + timedelta(hours=5), {"post_kind": "stop_hit", "ticker": "XYZ", "event": {}}),
        ("e3", T0 + timedelta(days=5, hours=1), {"post_kind": "stop_hit", "ticker": "XYZ", "event": {}}),
        ("e4", T0 + timedelta(days=6), {"post_kind": "target_hit", "ticker": "XYZ",
                                        "event": {"all_targets": True}}),
    ]
    su.attach_desk_events([a, b], posts)
    assert a.desk_verdict == "win" and a.desk_max_target == 1      # stop after a target = trailed out
    assert [e["kind"] for e in a.desk_events] == ["target_hit", "stop_hit"]
    assert b.desk_verdict == "loss" and b.desk_max_target == 3     # "all targets" = last stated


def test_option_premium_pnl_is_size_weighted_and_all_closes_the_rest():
    o = _call(call_id="o", instrument="option", entry=None, stop=None, targets=[],
              option={"type": "CALL", "strike": 100.0, "premium": 5.0})
    posts = [
        ("x1", T0 + timedelta(hours=1), {"post_kind": "option_exit", "ticker": "XYZ",
                                         "option": {"type": "CALL", "strike": 100.0,
                                                    "exit_size": "70%", "exit_premium": 6.5}}),
        ("x2", T0 + timedelta(hours=2), {"post_kind": "option_exit", "ticker": "XYZ",
                                         "option": {"type": "CALL", "strike": 100.0,
                                                    "exit_size": "ALL", "exit_premium": 2.5}}),
        ("x3", T0 + timedelta(hours=3), {"post_kind": "option_exit", "ticker": "XYZ",
                                         "option": {"type": "CALL", "strike": 100.0,
                                                    "exit_size": "100%", "exit_premium": None}}),
    ]
    su.attach_desk_events([o], posts)
    # 70% × +30%  +  30% × −50%  =  +21 − 15  =  +6%; the restated exit after ALL is ignored.
    assert o.option["pnl_pct"] == pytest.approx(6.0)
    assert o.option["remaining_pct"] == 0
    assert o.desk_verdict == "win"


def test_summary_counts_only_resolved_in_the_win_rate():
    calls = [
        _call(call_id="1", verdict="win", realized_r=2.0, resolved_at=T0),
        _call(call_id="2", verdict="loss", realized_r=-1.0, resolved_at=T0 + timedelta(hours=1)),
        _call(call_id="3", verdict="open"),
        _call(call_id="4", verdict="unresolved"),
        _call(call_id="5", verdict="unpriceable"),
    ]
    s = su.summary(calls, now=T0 + timedelta(days=1))
    o = s["overall"]
    assert (o["n"], o["resolved"], o["wins"], o["losses"]) == (5, 2, 1, 1)
    assert o["win_rate"] == 50.0 and o["total_r"] == 1.0 and o["profit_factor"] == 2.0
    assert o["open"] == 1 and o["unresolved"] == 1 and o["unpriceable"] == 1
    assert [p["cum_r"] for p in s["curve"]] == [2.0, 1.0]
