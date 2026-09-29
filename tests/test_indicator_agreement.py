"""Timeframe aggregation + the Indicator subscore derived from the pane.

Covers `prices/resample.py`, `technicals.indicator_pane[_mtf]`, and
`rules/confluence.indicator_subscore` / `mtf_indicator_subscore`.
"""

import pytest

from macro_positioning.prices.provider import PriceBar
from macro_positioning.prices.resample import is_forming, resample_bars
from macro_positioning.prices.technicals import indicator_pane, indicator_pane_mtf
from macro_positioning.rules.confluence import (
    indicator_subscore,
    mtf_indicator_subscore,
    score_confluence,
)


def _bar(day, close, *, high=None, low=None, open_=None, volume=100, tf="1D"):
    return PriceBar(
        ticker="TEST", observed_at=day, timeframe=tf,
        open=open_ if open_ is not None else close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close, volume=volume,
    )


# --- resampling ---------------------------------------------------------

def test_weekly_buckets_run_monday_to_sunday():
    # Mon 05 - Fri 09 = ISO week 2; Mon 12 - Fri 16 = ISO week 3.
    bars = [
        _bar("2026-01-05", 10, high=11, low=9, open_=10),
        _bar("2026-01-09", 14, high=15, low=13),
        _bar("2026-01-12", 14, high=16, low=12, open_=14),
        _bar("2026-01-16", 12, high=13, low=8),
    ]
    wk = resample_bars(bars, "1W")
    assert len(wk) == 2
    assert (wk[0].open, wk[0].high, wk[0].low, wk[0].close) == (10, 15, 9, 14)
    assert (wk[1].open, wk[1].high, wk[1].low, wk[1].close) == (14, 16, 8, 12)
    assert wk[0].observed_at == "2026-01-09"      # last day actually in the bucket
    assert wk[0].timeframe == "1W"


def test_three_day_buckets_are_three_calendar_days_wide():
    bars = [_bar(f"2026-01-{d:02d}", 10 + d, high=20 + d, low=d) for d in (5, 6, 7, 8)]
    d3 = resample_bars(bars, "3D")
    assert len(d3) == 2                     # 05-07 together, 08 opens the next
    assert (d3[0].high, d3[0].low, d3[0].close) == (27, 5, 17)
    assert d3[0].observed_at == "2026-01-07"
    assert d3[1].close == 18


def test_three_day_buckets_do_not_shift_when_older_history_is_added():
    # The reason for a fixed calendar epoch: a session-count anchor would
    # re-bucket all of history every time an older bar showed up.
    recent = [_bar(f"2026-01-{d:02d}", 10 + d) for d in (5, 6, 7, 8, 9)]
    with_older = [_bar(f"2025-11-{d:02d}", 5) for d in (3, 4)] + recent
    tail_a = [(b.observed_at, b.close) for b in resample_bars(recent, "3D")]
    tail_b = [(b.observed_at, b.close) for b in resample_bars(with_older, "3D")]
    assert tail_a == tail_b[-len(tail_a):]


def test_monthly_buckets_by_calendar_month():
    bars = [_bar("2026-01-30", 10), _bar("2026-02-02", 20), _bar("2026-02-20", 15)]
    mo = resample_bars(bars, "1M")
    assert [b.close for b in mo] == [10, 15]


def test_volume_sums_but_stays_none_when_a_bar_has_no_volume():
    both = resample_bars([_bar("2026-01-05", 10), _bar("2026-01-09", 11)], "1W")
    assert both[0].volume == 200
    missing = resample_bars(
        [_bar("2026-01-05", 10), _bar("2026-01-09", 11, volume=None)], "1W"
    )
    assert missing[0].volume is None


def test_forming_bucket_is_included_by_default_and_droppable():
    # Fri 2026-01-09 sits mid-week, so week 2 is still forming.
    bars = [_bar("2026-01-05", 10), _bar("2026-01-09", 11)]
    assert len(resample_bars(bars, "1W")) == 1
    assert is_forming(resample_bars(bars, "1W")[0]) is True
    assert resample_bars(bars, "1W", include_partial=False) == []


def test_closed_bucket_is_not_forming():
    wk = resample_bars([_bar("2026-01-11", 10)], "1W")   # Sunday = week end
    assert is_forming(wk[0]) is False
    assert resample_bars([_bar("2026-01-11", 10)], "1W", include_partial=False) != []


def test_daily_passes_through_and_unknown_timeframe_raises():
    bars = [_bar("2026-01-05", 10)]
    assert resample_bars(bars, "1D") == bars
    with pytest.raises(ValueError, match="unsupported timeframe"):
        resample_bars(bars, "12h")


def test_out_of_order_input_is_sorted_before_bucketing():
    bars = [_bar("2026-01-09", 14), _bar("2026-01-05", 10, open_=10)]
    wk = resample_bars(bars, "1W")
    assert (wk[0].open, wk[0].close) == (10, 14)


def test_empty_input_gives_empty_output():
    assert resample_bars([], "1W") == []


# --- the pane -----------------------------------------------------------

def _ramp(n, *, start=100.0, step=1.5, accel=0.0, wiggle=1.0):
    bars = []
    price = start
    for i in range(n):
        price = start + step * i + accel * i * i
        bars.append(_bar(f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                         price, high=price + wiggle, low=price - wiggle))
    return bars


def test_the_default_timeframes_are_the_derivable_half_of_his_1d_3d_12h():
    from macro_positioning.prices.technicals import PANE_TIMEFRAMES
    assert PANE_TIMEFRAMES == ("1D", "3D")


def test_pane_bundles_all_three_legs_with_a_readable_summary():
    pane = indicator_pane(_ramp(80), timeframe="1D")
    assert pane["timeframe"] == "1D"
    assert pane["macd"] is not None
    assert pane["squeeze"] is not None
    assert pane["rsi14"] is not None
    assert "squeeze" in pane["summary"] and "rsi" in pane["summary"]


def test_pane_carries_the_rsi_signal_ma_read():
    pane = indicator_pane(_ramp(80))
    assert pane["rsi"] is not None
    assert pane["rsi"]["rsi"] == pane["rsi14"]
    assert "ma" in pane["summary"]


def test_pane_says_so_when_history_is_too_short():
    pane = indicator_pane(_ramp(5))
    assert pane["macd"] is None and pane["squeeze"] is None
    assert "insufficient history" in pane["summary"]


def test_mtf_pane_aggregates_from_daily_and_refuses_intraday():
    panes = indicator_pane_mtf(_ramp(300), timeframes=("1D", "1W"))
    assert set(panes) == {"1D", "1W"}
    assert panes["1W"]["n_bars"] < panes["1D"]["n_bars"]
    with pytest.raises(ValueError, match="can't be aggregated from daily"):
        indicator_pane_mtf(_ramp(300), timeframes=("12h",))


# --- the subscore -------------------------------------------------------

def _pane(*, state="rising_positive", cross=None, above=True,
          rsi=60.0, rsi_ma=50.0, sqz_on=False, fired=False, bars_in=0,
          timeframe="1D"):
    return {
        "timeframe": timeframe,
        "macd": {
            "macd": 1.0, "signal": 0.5, "hist": 0.5, "hist_prev": 0.2,
            "hist_state": state, "above_signal": above, "cross": cross,
        },
        "squeeze": {
            "on": sqz_on, "bars_in_squeeze": bars_in, "fired": fired,
            "bb_upper": 1.0, "bb_lower": 0.0, "kc_upper": 1.0, "kc_lower": 0.0,
        },
        "rsi14": rsi,
        "rsi": {
            "rsi": rsi, "rsi_signal": rsi_ma, "above_signal": rsi >= rsi_ma,
            "rising": True, "extended": rsi > 80.0, "washed_out": rsi < 20.0,
        },
        "summary": "test pane",
    }


def test_full_agreement_scores_two():
    out = indicator_subscore(_pane(fired=True, rsi=60), "long")
    assert out["indicator"] == 2
    assert set(out["verdicts"].values()) == {"agree"}


def test_a_coiled_squeeze_also_counts_as_agreement():
    out = indicator_subscore(_pane(sqz_on=True, bars_in=7, rsi=60), "long")
    assert out["indicator"] == 2
    assert "7 bars coiled" in " ".join(out["reasons"])


def test_one_leg_opposing_scores_zero_even_with_two_agreeing():
    # RSI under its MA = momentum ceased, whatever the absolute level.
    out = indicator_subscore(_pane(fired=True, rsi=55, rsi_ma=60), "long")
    assert out["indicator"] == 0
    assert out["verdicts"]["rsi"] == "against"
    assert "momentum ceased" in " ".join(out["reasons"])


def test_partial_agreement_scores_one():
    out = indicator_subscore(_pane(state="fading_positive", rsi=60), "long")
    assert out["indicator"] == 1
    assert out["verdicts"]["macd"] == "neutral"
    assert out["verdicts"]["rsi"] == "agree"


def test_nothing_agreeing_scores_zero():
    out = indicator_subscore(
        _pane(state="fading_positive", rsi=85, rsi_ma=80), "long"
    )   # macd neutral, rsi above MA but extended -> neutral
    assert out["indicator"] == 0
    assert "agree" not in out["verdicts"].values()


def test_rsi_is_read_against_its_ma_not_the_thirty_seventy_bands():
    # 35 over a 30 MA agrees with a long; 75 under an 80 MA does not.
    assert indicator_subscore(_pane(rsi=35, rsi_ma=30), "long")["verdicts"]["rsi"] == "agree"
    assert indicator_subscore(_pane(rsi=75, rsi_ma=80), "long")["verdicts"]["rsi"] == "against"


def test_extended_rsi_downgrades_to_neutral_but_never_opposes():
    out = indicator_subscore(_pane(rsi=88, rsi_ma=70), "long")
    assert out["verdicts"]["rsi"] == "neutral"
    assert "extended" in " ".join(out["reasons"])


def test_rsi_above_its_ma_opposes_a_short():
    out = indicator_subscore(_pane(state="falling_negative", above=False,
                                   rsi=55, rsi_ma=45), "short")
    assert out["verdicts"]["rsi"] == "against"
    assert "momentum returning" in " ".join(out["reasons"])


def test_washed_out_rsi_downgrades_a_short_to_neutral():
    out = indicator_subscore(_pane(state="falling_negative", above=False,
                                   rsi=15, rsi_ma=25), "short")
    assert out["verdicts"]["rsi"] == "neutral"
    assert "washed out" in " ".join(out["reasons"])


def test_a_fresh_cross_counts_even_when_the_histogram_is_still_negative():
    out = indicator_subscore(
        _pane(state="recovering_negative", cross="bull_cross", rsi=55, rsi_ma=50),
        "long",
    )
    assert out["verdicts"]["macd"] == "agree"


def test_short_side_mirrors_the_long_read():
    falling = _pane(state="falling_negative", above=False, rsi=35, rsi_ma=45)
    assert indicator_subscore(falling, "short")["verdicts"]["macd"] == "agree"
    assert indicator_subscore(falling, "long")["verdicts"]["macd"] == "against"
    rising = _pane(state="rising_positive", rsi=65, rsi_ma=55)
    assert indicator_subscore(rising, "short")["verdicts"]["macd"] == "against"


def test_a_recovering_histogram_opposes_a_short():
    out = indicator_subscore(
        _pane(state="recovering_negative", rsi=35, rsi_ma=45), "short"
    )
    assert out["verdicts"]["macd"] == "against"
    assert out["indicator"] == 0


def test_the_squeeze_can_support_a_side_but_never_oppose_one():
    for side in ("long", "short"):
        for kwargs in ({"fired": True}, {"sqz_on": True}, {}):
            out = indicator_subscore(_pane(rsi=50, rsi_ma=50, **kwargs), side)
            assert out["verdicts"]["squeeze"] != "against"


def test_missing_legs_score_zero_rather_than_counting_as_agreement():
    blind = {"timeframe": "1M", "macd": None, "squeeze": None, "rsi14": None, "rsi": None}
    out = indicator_subscore(blind, "long")
    assert out["indicator"] == 0
    assert set(out["verdicts"].values()) == {"unavailable"}


def test_side_must_be_long_or_short():
    with pytest.raises(ValueError, match="side must be"):
        indicator_subscore(_pane(), "sideways")


def test_subscore_drops_straight_into_the_confluence_rubric():
    ind = indicator_subscore(_pane(fired=True, rsi=60), "long")["indicator"]
    assert score_confluence(3, 3, ind).total == 8


# --- multi-timeframe ----------------------------------------------------

def test_mtf_takes_the_weakest_readable_timeframe():
    panes = {
        "1D": _pane(fired=True, rsi=60, timeframe="1D"),                    # 2
        "1W": _pane(state="fading_positive", rsi=60, timeframe="1W"),       # 1
    }
    out = mtf_indicator_subscore(panes, "long")
    assert out["indicator"] == 1
    assert out["aligned"] == ["1D"]


def test_mtf_flags_the_timeframe_that_opposes():
    panes = {
        "1D": _pane(fired=True, rsi=60, timeframe="1D"),
        "1W": _pane(state="falling_negative", rsi=60, timeframe="1W"),
    }
    out = mtf_indicator_subscore(panes, "long")
    assert out["indicator"] == 0
    assert out["opposed"] == ["1W"]


def test_an_unreadable_timeframe_is_excluded_not_counted_as_zero():
    panes = {
        "1D": _pane(fired=True, rsi=60, timeframe="1D"),
        "1M": {"timeframe": "1M", "macd": None, "squeeze": None,
               "rsi14": None, "rsi": None},
    }
    out = mtf_indicator_subscore(panes, "long")
    assert out["unreadable"] == ["1M"]
    assert out["indicator"] == 2
    assert out["scores"]["1M"] == 0


def test_mtf_scores_zero_when_no_timeframe_is_readable():
    blind = {"timeframe": "1M", "macd": None, "squeeze": None, "rsi14": None, "rsi": None}
    out = mtf_indicator_subscore({"1M": blind}, "long")
    assert out["indicator"] == 0


def test_mtf_needs_at_least_one_pane():
    with pytest.raises(ValueError, match="no panes"):
        mtf_indicator_subscore({}, "long")
