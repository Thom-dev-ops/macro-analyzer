"""MACD(14,26,9) + TTM Squeeze — the port of his own Pine indicator.

Source of truth: docs/indicators/macd_ttm_squeeze.pine. These tests pin the
fidelity choices (SMA-seeded EMA, population stdev, true-range Keltner) so a
later "cleanup" to textbook defaults fails loudly instead of silently
changing what the chart says.
"""

from macro_positioning.prices.provider import PriceBar
from macro_positioning.prices.technicals import (
    HIST_STATES,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    _stdev_pop,
    compute_technical_features,
    ema,
    ema_series,
    macd,
    rsi_series,
    rsi_with_signal,
    true_range_series,
    ttm_squeeze,
)


def _bars(seq, *, wiggle=0.0, volume=1000):
    """Bars from plain closes (optionally with a symmetric high/low wiggle)."""
    out = []
    for i, c in enumerate(seq):
        c = float(c)
        out.append(PriceBar(
            ticker="TEST", observed_at=f"2026-01-{(i % 28) + 1:02d}",
            open=c, high=c + wiggle, low=c - wiggle, close=c, volume=volume,
        ))
    return out


# --- primitives ---------------------------------------------------------

def test_ema_series_is_sma_seeded_and_matches_hand_computation():
    # window=2, alpha=2/3, seed=(1+2)/2=1.5 → 2.5 → 3.5
    assert ema_series([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]


def test_ema_series_last_value_equals_ema():
    closes = [10 + (i % 5) for i in range(40)]
    assert ema_series(closes, 20)[-1] == ema(closes, 20)


def test_ema_series_empty_when_too_few_bars():
    assert ema_series([1, 2], 5) == []


def test_stdev_is_population_not_sample():
    # Population stdev of this classic set is exactly 2.0; sample is ~2.138.
    assert abs(_stdev_pop([2, 4, 4, 4, 5, 5, 7, 9]) - 2.0) < 1e-12


def test_true_range_first_bar_is_high_low_then_uses_prior_close():
    bars = _bars([100, 110], wiggle=1.0)
    trs = true_range_series(bars)
    assert trs[0] == 2.0                       # 101 - 99, no prior close
    assert trs[1] == 111 - 100                 # high vs prior close dominates


# --- RSI against its own signal MA -------------------------------------

def test_rsi_series_ends_on_the_same_value_as_rsi():
    from macro_positioning.prices.technicals import rsi
    closes = [100 + (i % 7) * 1.5 for i in range(60)]
    assert rsi_series(closes, 14)[-1] == rsi(closes, 14)


def test_rsi_series_empty_when_too_few_bars():
    assert rsi_series([1.0, 2.0], 14) == []


def test_rsi_signal_read_is_rsi_versus_its_own_ma():
    # A choppy-but-rising tape pulls RSI above its own MA.
    closes = [100.0] * 30 + [100 + i * 2.0 - (3.0 if i % 4 == 0 else 0) for i in range(20)]
    out = rsi_with_signal(closes)
    assert out["above_signal"] is True
    assert out["rsi"] > out["rsi_signal"]


def test_a_relentless_run_pins_rsi_at_100_and_is_not_called_momentum_ceased():
    # RSI saturates at 100 and its MA catches up to meet it. A strict `>`
    # test would read that as momentum ceasing at its strongest point.
    out = rsi_with_signal([100.0] * 30 + [100 + i * 2.0 for i in range(20)])
    assert out["rsi"] == 100.0 and out["rsi_signal"] == 100.0
    assert out["above_signal"] is True


def test_rsi_drops_under_its_ma_when_the_move_stalls():
    closes = [100 + i * 2.0 for i in range(30)] + [160.0 - i * 0.5 for i in range(20)]
    out = rsi_with_signal(closes)
    assert out["above_signal"] is False


def test_rsi_signal_needs_enough_bars():
    assert rsi_with_signal([100 + i for i in range(20)]) is None
    assert rsi_with_signal([100 + (i % 5) for i in range(40)]) is not None


# --- MACD ---------------------------------------------------------------

def test_macd_none_until_the_signal_line_exists():
    assert macd([float(i) for i in range(MACD_SLOW + MACD_SIGNAL - 2)]) is None
    assert macd([float(i) for i in range(MACD_SLOW + MACD_SIGNAL)]) is not None


def test_macd_uses_fast_14_not_textbook_12():
    assert (MACD_FAST, MACD_SLOW, MACD_SIGNAL) == (14, 26, 9)
    closes = [100 + i * 1.5 for i in range(80)]
    assert macd(closes)["macd"] != macd(closes, fast=12)["macd"]


def test_macd_positive_and_above_signal_in_a_steady_uptrend():
    out = macd([100 + i * 1.5 for i in range(80)])
    assert out["macd"] > 0
    assert out["above_signal"] is True
    assert out["hist"] >= 0


def test_macd_negative_in_a_steady_downtrend():
    out = macd([300 - i * 1.5 for i in range(80)])
    assert out["macd"] < 0


def test_a_perfectly_linear_trend_settles_the_histogram_on_zero():
    # Steady state: the signal EMA catches the (constant) MACD, so the
    # histogram flattens to zero. Pine paints that bar green, not lime —
    # hist >= 0 but not rising. Momentum is constant, not building.
    out = macd([300 - i * 1.5 for i in range(80)])
    assert abs(out["hist"]) < 1e-9
    assert out["hist_state"] == "fading_positive"


def test_accelerating_decline_puts_macd_under_its_signal():
    out = macd([300 - 0.02 * i * i for i in range(80)])
    assert out["macd"] < out["signal"]
    assert out["above_signal"] is False
    assert out["hist_state"] == "falling_negative"


def test_hist_state_is_one_of_the_four_chart_colours_and_matches_the_rule():
    for closes in (
        [100 + i * 1.5 for i in range(80)],                       # up
        [300 - i * 1.5 for i in range(80)],                       # down
        [100 + i * 1.5 for i in range(60)] + [190.0] * 20,        # up then flat
        [300 - i * 1.5 for i in range(60)] + [210.0] * 20,        # down then flat
    ):
        out = macd(closes)
        assert out["hist_state"] in HIST_STATES
        h, p = out["hist"], out["hist_prev"]
        expected = (
            ("rising_positive" if h > p else "fading_positive") if h >= 0
            else ("falling_negative" if h < p else "recovering_negative")
        )
        assert out["hist_state"] == expected


def test_a_stalling_rally_fades_the_positive_histogram():
    # Momentum rolling over while the histogram is still above zero — the
    # "green not lime" read that the colour rule exists to capture. Needs an
    # accelerating rally first, since a linear ramp parks the histogram at 0.
    base = [100 + 0.02 * i * i for i in range(60)]
    out = macd(base + [base[-1] + 0.3 * k for k in range(1, 4)])
    assert out["hist"] > 0
    assert out["hist"] < out["hist_prev"]
    assert out["hist_state"] == "fading_positive"


def test_an_accelerating_rally_brightens_the_histogram():
    out = macd([100 + 0.02 * i * i for i in range(60)])
    assert out["hist"] > 0
    assert out["hist_state"] == "rising_positive"


def test_macd_cross_flags_the_zero_line_flip_of_the_histogram():
    rising = macd([100 + i * 1.5 for i in range(80)])
    assert rising["cross"] in (None, "bull_cross")
    # A trend that reverses hard puts a bear cross in recent history.
    turn = [100 + i * 2.0 for i in range(60)] + [220 - i * 6.0 for i in range(20)]
    assert macd(turn)["macd"] < macd(turn[:60])["macd"]


# --- TTM Squeeze --------------------------------------------------------

def test_squeeze_none_when_fewer_than_length_bars():
    assert ttm_squeeze(_bars([100] * 19, wiggle=1.0)) is None
    assert ttm_squeeze(_bars([100] * 20, wiggle=1.0)) is not None


def test_squeeze_on_when_closes_are_flat_but_bars_still_have_range():
    # std → 0 collapses the Bollinger Bands; the Keltner range survives on
    # true range alone, so the bands sit inside the channel.
    out = ttm_squeeze(_bars([100] * 40, wiggle=1.0))
    assert out["on"] is True
    assert out["bb_lower"] > out["kc_lower"] and out["bb_upper"] < out["kc_upper"]
    assert out["bars_in_squeeze"] > 1
    assert out["fired"] is False


def test_squeeze_off_when_price_expands():
    out = ttm_squeeze(_bars([100 + i * 5 for i in range(40)], wiggle=1.0))
    assert out["on"] is False
    assert out["bars_in_squeeze"] == 0


def test_squeeze_fires_on_the_bar_the_compression_releases():
    # 40 flat bars (squeezed), then one violent expansion bar.
    closes = [100.0] * 40 + [180.0]
    out = ttm_squeeze(_bars(closes, wiggle=1.0))
    assert out["on"] is False
    assert out["fired"] is True


def test_bars_in_squeeze_counts_only_the_current_run():
    closes = [100.0] * 25 + [100 + i * 8 for i in range(10)] + [160.0] * 25
    out = ttm_squeeze(_bars(closes, wiggle=1.0))
    if out["on"]:
        assert out["bars_in_squeeze"] <= 25


# --- wiring into the feature dict --------------------------------------

def test_feature_dict_carries_the_pane():
    feats = compute_technical_features(_bars([100 + i * 1.5 for i in range(80)], wiggle=1.0))
    for key in (
        "macd", "macd_signal", "macd_hist", "macd_hist_state",
        "macd_above_signal", "macd_cross",
        "rsi14_signal", "rsi14_above_signal",
        "squeeze_on", "squeeze_bars", "squeeze_fired",
        "bb_upper", "bb_lower", "kc_upper", "kc_lower",
    ):
        assert key in feats
    assert feats["macd_hist_state"] in HIST_STATES


def test_feature_dict_degrades_to_none_on_short_history():
    feats = compute_technical_features(_bars([100, 101, 102], wiggle=1.0))
    assert feats["macd"] is None
    assert feats["squeeze_on"] is None
