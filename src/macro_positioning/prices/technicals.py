"""Technical indicators — pure functions over a PriceBar list.

NO numpy/pandas dependency on purpose: keeps this module fast to import,
trivial to test, and easy to reason about. We have N≤200 bars per ticker;
manual loops are fine.

Indicators:
  - Simple moving average (SMA)
  - Exponential moving average (EMA) — recency-weighted, the preferred
    trend-following indicator for active traders
  - Average true range (ATR) over 14 bars
  - Relative strength index (RSI) over 14 bars
  - MACD(14,26,9) with the 4-state histogram colour read, and the TTM
    Squeeze (BB inside KC) — both ported 1:1 from his own Pine indicator
    (docs/indicators/macd_ttm_squeeze.pine) so the numbers match his chart
  - Multi-horizon price momentum: 1d / 3d / 5d / 20d / 60d % change
    (approximates daily / 3-day / weekly / monthly / cycle trend)
  - % distance from MA
  - Higher highs / higher lows over a window
  - Above/below SMA + EMA
  - Recent breakout / failed breakout flags

INTRADAY note: this module operates on whatever bars it's given. With
daily bars (current default), windows are in trading days. Intraday
support (4h/12h) needs the price fetcher to get intraday data first —
the math here is timeframe-agnostic. See `prices/provider.py` TODO.

Output is a flat dict of float/bool features the technical_scorer
heuristic consumes (per framework §5).
"""

from __future__ import annotations

from typing import Iterable

from macro_positioning.prices.provider import PriceBar


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------

def sma(closes: list[float], window: int) -> float | None:
    """Simple moving average of last `window` closes. None if too few bars."""
    if len(closes) < window or window <= 0:
        return None
    return sum(closes[-window:]) / window


def ema(closes: list[float], window: int) -> float | None:
    """Exponential moving average over `window`. None if too few bars.

    Recency-weighted: the most recent close gets weight α = 2/(window+1).
    EMA reacts faster to price changes than SMA — preferred for trend
    following per the trading framework's §5 momentum guidance.
    """
    if len(closes) < window or window <= 0:
        return None
    alpha = 2.0 / (window + 1)
    # Seed with SMA of the first `window` closes (Wilder/standard convention)
    seed = sum(closes[:window]) / window
    val = seed
    for c in closes[window:]:
        val = alpha * c + (1 - alpha) * val
    return val


def pct_change(closes: list[float], lookback: int) -> float | None:
    """% change of the most recent close vs `lookback` bars ago.

    Returns 0.0 for very small bases (avoids spurious infinities).
    None when too few bars.
    """
    if len(closes) <= lookback or lookback <= 0:
        return None
    base = closes[-(lookback + 1)]
    if abs(base) < 1e-9:
        return 0.0
    return (closes[-1] - base) / base


def pct_from(price: float, reference: float | None) -> float | None:
    """(price - reference) / reference. None when reference missing/zero."""
    if reference is None or reference == 0:
        return None
    return (price - reference) / reference


# ---------------------------------------------------------------------------
# ATR — average true range
# ---------------------------------------------------------------------------

def atr(bars: list[PriceBar], window: int = 14) -> float | None:
    """ATR over last `window` true-range values. Wilder's method."""
    if len(bars) < window + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        h = bars[i].high if bars[i].high is not None else bars[i].close
        l = bars[i].low if bars[i].low is not None else bars[i].close
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
    if len(trs) < window:
        return None
    return sum(trs[-window:]) / window


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

# RSI is read against its own signal MA (his TradingView setup: "RSI 14
# close with a 14-period signal MA"), not against the 30/70 bands.
RSI_WINDOW, RSI_SIGNAL = 14, 14
RSI_EXTENDED, RSI_WASHED_OUT = 80.0, 20.0


def rsi(closes: list[float], window: int = 14) -> float | None:
    """Relative strength index. Wilder smoothing approximation via SMA."""
    if len(closes) < window + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas[-window:]]
    losses = [max(-d, 0) for d in deltas[-window:]]
    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def rsi_series(closes: list[float], window: int = 14) -> list[float]:
    """RSI at every bar it can be computed for, oldest first.

    Same math as `rsi()` (whose value this series ends on), so the two can
    never disagree. One value per bar from index `window` onward.
    """
    if window <= 0 or len(closes) < window + 1:
        return []
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    out: list[float] = []
    for end in range(window, len(deltas) + 1):
        chunk = deltas[end - window : end]
        avg_gain = sum(max(d, 0) for d in chunk) / window
        avg_loss = sum(max(-d, 0) for d in chunk) / window
        if avg_loss == 0:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(100 - (100 / (1 + rs)))
    return out


def rsi_with_signal(
    closes: list[float],
    window: int = RSI_WINDOW,
    signal: int = RSI_SIGNAL,
) -> dict | None:
    """RSI(14) plus its 14-period signal MA — how he actually reads RSI.

    The read that matters is RSI versus its own MA, NOT the 30/70 bands:
    "RSI below its MA" means momentum has ceased, whatever the absolute
    level. `above_signal` is that read; `extended` / `washed_out` are only
    secondary guards.

    His condition is strictly *below*, so equality is NOT momentum ceasing —
    and that matters: a relentless run with no down closes pins RSI at 100
    and drags its own MA up to meet it, which a `>` test would read as
    momentum ceasing at the exact moment it is strongest.

    None until there are enough bars for the signal MA to exist.
    """
    series = rsi_series(closes, window)
    if len(series) < signal:
        return None
    ma = sum(series[-signal:]) / signal
    val = series[-1]
    prev = series[-2] if len(series) > 1 else val
    return {
        "rsi": val,
        "rsi_signal": ma,
        "above_signal": val >= ma,
        "rising": val > prev,
        "extended": val > RSI_EXTENDED,
        "washed_out": val < RSI_WASHED_OUT,
    }


# ---------------------------------------------------------------------------
# MACD + TTM Squeeze — the desk's own TradingView pane
#
# Ported 1:1 from the Pine v5 indicator "Custom MACD Histogram with TTM
# Squeeze" (source kept verbatim at docs/indicators/macd_ttm_squeeze.pine)
# so the system reads the SAME numbers he reads on the chart. Deliberate
# fidelity choices — do NOT "fix" these to textbook defaults:
#   - fast length is 14, not the textbook 12
#   - `hist_state` reproduces the four histogram colours he actually reads
#     (lime / green / red / faded-red), which carry the momentum read
#   - squeeze stdev is POPULATION stdev (Pine's ta.stdev), not sample
#   - the Keltner range is an EMA of TRUE RANGE (Pine's ta.tr), not of
#     high-low; the first bar's TR is high-low, as in Pine
# ---------------------------------------------------------------------------

MACD_FAST, MACD_SLOW, MACD_SIGNAL = 14, 26, 9
SQUEEZE_LENGTH, SQUEEZE_BB_MULT, SQUEEZE_KC_MULT = 20, 2.0, 1.5

# The four histogram colours, in the desk's own terms.
HIST_STATES = ("rising_positive", "fading_positive", "falling_negative", "recovering_negative")


def ema_series(values: list[float], window: int) -> list[float]:
    """Full EMA series, seeded the same way as `ema()` (SMA of first window).

    One value per bar from index `window - 1` onward, so
    len(result) == len(values) - window + 1. Empty list when too few bars.
    `ema()` returns this series' last element.
    """
    if window <= 0 or len(values) < window:
        return []
    alpha = 2.0 / (window + 1)
    val = sum(values[:window]) / window
    out = [val]
    for v in values[window:]:
        val = alpha * v + (1 - alpha) * val
        out.append(val)
    return out


def macd(
    closes: list[float],
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> dict | None:
    """MACD line / signal / histogram, with the histogram colour state.

    None when there aren't enough bars for the signal EMA to exist
    (needs slow + signal - 1 bars). `hist_state` is the read that matters:
    a shrinking positive bar ("fading_positive") is momentum rolling over
    even while the histogram is still above zero.
    """
    fast_s = ema_series(closes, fast)
    slow_s = ema_series(closes, slow)
    if not fast_s or not slow_s:
        return None
    # Align both series on the slow EMA's first bar (index slow - 1).
    offset = slow - fast
    if offset < 0 or len(fast_s) <= offset:
        return None
    macd_s = [fast_s[offset + i] - slow_s[i] for i in range(len(slow_s))]
    signal_s = ema_series(macd_s, signal)
    if len(signal_s) < 2:
        return None
    # signal_s[i] lines up with macd_s[signal - 1 + i]
    hist_s = [macd_s[signal - 1 + i] - signal_s[i] for i in range(len(signal_s))]

    hist_now, hist_prev = hist_s[-1], hist_s[-2]
    if hist_now >= 0:
        state = "rising_positive" if hist_now > hist_prev else "fading_positive"
    else:
        state = "falling_negative" if hist_now < hist_prev else "recovering_negative"

    cross = None
    if hist_prev <= 0 < hist_now:
        cross = "bull_cross"
    elif hist_prev >= 0 > hist_now:
        cross = "bear_cross"

    return {
        "macd": macd_s[-1],
        "signal": signal_s[-1],
        "hist": hist_now,
        "hist_prev": hist_prev,
        "hist_state": state,
        "above_signal": macd_s[-1] > signal_s[-1],
        "cross": cross,
    }


def _stdev_pop(values: list[float]) -> float:
    """Population standard deviation — matches Pine's ta.stdev."""
    n = len(values)
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / n) ** 0.5


def true_range_series(bars: list[PriceBar]) -> list[float]:
    """One true range per bar. First bar is high-low (Pine's ta.tr)."""
    out: list[float] = []
    for i, b in enumerate(bars):
        h = b.high if b.high is not None else b.close
        l = b.low if b.low is not None else b.close
        if i == 0:
            out.append(h - l)
            continue
        pc = bars[i - 1].close
        out.append(max(h - l, abs(h - pc), abs(l - pc)))
    return out


def ttm_squeeze(
    bars: list[PriceBar],
    length: int = SQUEEZE_LENGTH,
    bb_mult: float = SQUEEZE_BB_MULT,
    kc_mult: float = SQUEEZE_KC_MULT,
) -> dict | None:
    """TTM Squeeze: are the Bollinger Bands inside the Keltner Channels?

    Squeeze on = volatility compressed = energy stored. Returns the bands
    themselves plus two reads the Pine plot leaves to the eye: how many
    consecutive bars the squeeze has held (`bars_in_squeeze`) and whether
    it released on this bar (`fired`) — the release is the tradeable event.
    None when fewer than `length` bars.
    """
    if length <= 0 or len(bars) < length:
        return None
    closes = [b.close for b in bars]
    trs = true_range_series(bars)

    ema_close_s = ema_series(closes, length)
    ema_tr_s = ema_series(trs, length)
    if not ema_close_s or not ema_tr_s:
        return None

    on_s: list[bool] = []
    bb_up = bb_lo = kc_up = kc_lo = 0.0
    # Index i of each series lines up with bar index `length - 1 + i`.
    for i in range(len(ema_close_s)):
        window = closes[i : i + length]
        basis = sum(window) / length
        std = _stdev_pop(window)
        bb_up = basis + bb_mult * std
        bb_lo = basis - bb_mult * std
        kc_up = ema_close_s[i] + kc_mult * ema_tr_s[i]
        kc_lo = ema_close_s[i] - kc_mult * ema_tr_s[i]
        on_s.append(bb_lo > kc_lo and bb_up < kc_up)

    on = on_s[-1]
    bars_in_squeeze = 0
    if on:
        for v in reversed(on_s):
            if not v:
                break
            bars_in_squeeze += 1
    fired = (not on) and len(on_s) >= 2 and on_s[-2]

    return {
        "on": on,
        "bars_in_squeeze": bars_in_squeeze,
        "fired": fired,
        "bb_upper": bb_up,
        "bb_lower": bb_lo,
        "kc_upper": kc_up,
        "kc_lower": kc_lo,
    }


# ---------------------------------------------------------------------------
# The pane as one read — and the same pane across timeframes
# ---------------------------------------------------------------------------

# His chart timeframes are 12h / 1D / 3D and he checks them for conflict.
# 12h needs intraday bars the DB doesn't hold yet (prices/provider.py TODO),
# so the derivable pair is the default; 1W/1M are available on request.
PANE_TIMEFRAMES = ("1D", "3D")


def indicator_pane(bars: list[PriceBar], timeframe: str = "1D") -> dict:
    """MACD + Squeeze + RSI as a single read, the way the pane is read.

    RSI rides along because the confluence rubric's Indicator subscore is
    "MACD + RSI + Squeeze all agree" — one call returns all three so no
    caller has to remember the third.

    `summary` is a one-line human read for notes/journal/UI. Sub-dicts are
    None when history is too short; callers must handle that rather than
    assume a reading exists.
    """
    closes = [b.close for b in bars]
    m = macd(closes)
    s = ttm_squeeze(bars)
    r = rsi(closes, RSI_WINDOW)
    rs = rsi_with_signal(closes)

    parts: list[str] = []
    if m:
        parts.append(m["hist_state"].replace("_", " "))
        parts.append("macd>signal" if m["above_signal"] else "macd<signal")
        if m["cross"]:
            parts.append(m["cross"].replace("_", " ").upper())
    if s:
        if s["fired"]:
            parts.append("SQUEEZE FIRED")
        elif s["on"]:
            parts.append(f"squeeze on ({s['bars_in_squeeze']} bars)")
        else:
            parts.append("squeeze off")
    if rs:
        parts.append(
            f"rsi {rs['rsi']:.0f} {'>' if rs['above_signal'] else '<'} ma {rs['rsi_signal']:.0f}"
            + (" EXTENDED" if rs["extended"] else "")
        )
    elif r is not None:
        parts.append(f"rsi {r:.0f}")

    return {
        "timeframe": timeframe,
        "n_bars": len(bars),
        "macd": m,
        "squeeze": s,
        "rsi14": r,
        "rsi": rs,
        "summary": ", ".join(parts) if parts else f"insufficient history ({len(bars)} bars)",
    }


def indicator_pane_mtf(
    daily_bars: list[PriceBar],
    timeframes: tuple[str, ...] = PANE_TIMEFRAMES,
) -> dict[str, dict]:
    """The pane on each timeframe, aggregated up from daily bars.

    Weekly/monthly are exact aggregations of the daily history (see
    `prices/resample.py`), so no extra fetch is needed. Intraday timeframes
    are NOT derivable this way — they need intraday bars in the DB first
    (`prices/provider.py` TODO). Asking for one raises rather than silently
    handing back a daily read dressed up as 4h.
    """
    from macro_positioning.prices.resample import SUPPORTED, resample_bars

    out: dict[str, dict] = {}
    for tf in timeframes:
        if tf not in SUPPORTED:
            raise ValueError(
                f"timeframe {tf!r} can't be aggregated from daily bars; "
                f"derivable: {SUPPORTED}"
            )
        out[tf] = indicator_pane(resample_bars(daily_bars, tf), timeframe=tf)
    return out


# ---------------------------------------------------------------------------
# Structure detection — higher highs / higher lows
# ---------------------------------------------------------------------------

def higher_highs(highs: list[float], window: int = 20) -> bool:
    if len(highs) < window:
        return False
    recent_max = max(highs[-window // 2:])
    earlier_max = max(highs[-window:-window // 2])
    return recent_max > earlier_max


def higher_lows(lows: list[float], window: int = 20) -> bool:
    if len(lows) < window:
        return False
    recent_min = min(lows[-window // 2:])
    earlier_min = min(lows[-window:-window // 2])
    return recent_min > earlier_min


def lower_highs(highs: list[float], window: int = 20) -> bool:
    if len(highs) < window:
        return False
    recent_max = max(highs[-window // 2:])
    earlier_max = max(highs[-window:-window // 2])
    return recent_max < earlier_max


def lower_lows(lows: list[float], window: int = 20) -> bool:
    if len(lows) < window:
        return False
    recent_min = min(lows[-window // 2:])
    earlier_min = min(lows[-window:-window // 2])
    return recent_min < earlier_min


# ---------------------------------------------------------------------------
# Recent breakout heuristic
# ---------------------------------------------------------------------------

def recent_breakout(highs: list[float], lookback: int = 20) -> bool:
    """True when the last close pierces the prior `lookback` highs."""
    if len(highs) < lookback + 1:
        return False
    last = highs[-1]
    prior_max = max(highs[-(lookback + 1):-1])
    return last > prior_max


def recent_breakdown(lows: list[float], lookback: int = 20) -> bool:
    if len(lows) < lookback + 1:
        return False
    last = lows[-1]
    prior_min = min(lows[-(lookback + 1):-1])
    return last < prior_min


def prior_extreme(values: list[float], lookback: int = 20, *, high: bool = True) -> float | None:
    """The max (or min) of the `lookback` bars BEFORE the last one.

    This is the level a breakout/breakdown actually pierced — the level
    synthesizer stops just beyond it, so it must exclude the current bar.
    """
    if len(values) < lookback + 1:
        return None
    window = values[-(lookback + 1):-1]
    return max(window) if high else min(window)


def swing_low(lows: list[float], lookback: int = 10) -> float | None:
    """Lowest low of the last `lookback` bars — the recent defended floor."""
    if not lows:
        return None
    return min(lows[-lookback:])


def swing_high(highs: list[float], lookback: int = 10) -> float | None:
    """Highest high of the last `lookback` bars — the recent ceiling."""
    if not highs:
        return None
    return max(highs[-lookback:])


# ---------------------------------------------------------------------------
# Top-level: build a feature dict from a PriceBar list
# ---------------------------------------------------------------------------

def compute_volume_features(bars: list[PriceBar]) -> dict:
    """Return a flat volume features dict the volume_flow_confirmation
    scorer consumes. Volume bars with None are skipped (some sources
    don't report volume for FX/indices).
    """
    vols = [b.volume for b in bars if b.volume is not None]
    if not vols:
        return {"n_volume_bars": 0}
    closes = [b.close for b in bars]
    last_5 = vols[-5:] if len(vols) >= 5 else vols
    last_20 = vols[-20:] if len(vols) >= 20 else vols
    vol_5d_avg = sum(last_5) / len(last_5)
    vol_20d_avg = sum(last_20) / len(last_20)
    pct5 = pct_change(closes, 5) if len(closes) >= 6 else None
    return {
        "n_volume_bars": len(vols),
        "vol_5d_avg": vol_5d_avg,
        "vol_20d_avg": vol_20d_avg,
        "pct_change_5d": pct5,
    }


def compute_technical_features(bars: list[PriceBar]) -> dict:
    """Return a flat features dict the technical_scorer consumes.

    Keys:
      close, ma20, ma50, ma200, pct_from_ma20, pct_from_ma50, pct_from_ma200,
      atr14, rsi14, higher_highs, higher_lows, lower_highs, lower_lows,
      above_ma50, above_ma200, recent_breakout, recent_breakdown,
      prior_high_20, prior_low_20, swing_low_10, swing_high_10,
      macd, macd_signal, macd_hist, macd_hist_state, macd_above_signal,
      macd_cross, rsi14_signal, rsi14_above_signal,
      squeeze_on, squeeze_bars, squeeze_fired,
      bb_upper, bb_lower, kc_upper, kc_lower,
      n_bars
    """
    if not bars:
        return {"n_bars": 0}

    closes = [b.close for b in bars]
    highs = [b.high if b.high is not None else b.close for b in bars]
    lows = [b.low if b.low is not None else b.close for b in bars]
    last = closes[-1]

    ma20_v = sma(closes, 20)
    ma50_v = sma(closes, 50)
    ma200_v = sma(closes, 200)
    ema20_v = ema(closes, 20)
    ema50_v = ema(closes, 50)
    ema200_v = ema(closes, 200)

    macd_v = macd(closes) or {}
    sqz_v = ttm_squeeze(bars) or {}
    rsi_v = rsi_with_signal(closes) or {}

    return {
        "n_bars": len(bars),
        "close": last,
        # SMAs (lagging, smoother)
        "ma20": ma20_v,
        "ma50": ma50_v,
        "ma200": ma200_v,
        "pct_from_ma20": pct_from(last, ma20_v),
        "pct_from_ma50": pct_from(last, ma50_v),
        "pct_from_ma200": pct_from(last, ma200_v),
        # EMAs (recency-weighted, faster trend signal)
        "ema20": ema20_v,
        "ema50": ema50_v,
        "ema200": ema200_v,
        "pct_from_ema20": pct_from(last, ema20_v),
        "pct_from_ema50": pct_from(last, ema50_v),
        "pct_from_ema200": pct_from(last, ema200_v),
        # Volatility / momentum primitives
        "atr14": atr(bars, 14),
        "rsi14": rsi(closes, 14),
        # Multi-horizon momentum (approximates daily / 3-day / weekly /
        # monthly / cycle trend strength on daily bars)
        "pct_change_1d": pct_change(closes, 1),
        "pct_change_3d": pct_change(closes, 3),
        "pct_change_5d": pct_change(closes, 5),    # ≈ weekly
        "pct_change_20d": pct_change(closes, 20),  # ≈ monthly
        "pct_change_60d": pct_change(closes, 60),  # ≈ quarterly / cycle
        # Structure
        "higher_highs": higher_highs(highs, 20),
        "higher_lows": higher_lows(lows, 20),
        "lower_highs": lower_highs(highs, 20),
        "lower_lows": lower_lows(lows, 20),
        "above_ma50": (ma50_v is not None and last > ma50_v),
        "above_ma200": (ma200_v is not None and last > ma200_v),
        "above_ema20": (ema20_v is not None and last > ema20_v),
        "above_ema50": (ema50_v is not None and last > ema50_v),
        "recent_breakout": recent_breakout(highs, 20),
        "recent_breakdown": recent_breakdown(lows, 20),
        # Invalidation references for the level synthesizer: the level a
        # breakout pierced, and the nearest swing the trade must hold.
        "prior_high_20": prior_extreme(highs, 20, high=True),
        "prior_low_20": prior_extreme(lows, 20, high=False),
        "swing_low_10": swing_low(lows, 10),
        "swing_high_10": swing_high(highs, 10),
        # The desk's own MACD(14,26,9) + TTM Squeeze pane — same numbers he
        # reads on TradingView. None when there aren't enough bars.
        "macd": macd_v.get("macd"),
        "macd_signal": macd_v.get("signal"),
        "macd_hist": macd_v.get("hist"),
        "macd_hist_state": macd_v.get("hist_state"),
        "macd_above_signal": macd_v.get("above_signal"),
        "macd_cross": macd_v.get("cross"),
        "rsi14_signal": rsi_v.get("rsi_signal"),
        "rsi14_above_signal": rsi_v.get("above_signal"),
        "squeeze_on": sqz_v.get("on"),
        "squeeze_bars": sqz_v.get("bars_in_squeeze"),
        "squeeze_fired": sqz_v.get("fired"),
        "bb_upper": sqz_v.get("bb_upper"),
        "bb_lower": sqz_v.get("bb_lower"),
        "kc_upper": sqz_v.get("kc_upper"),
        "kc_lower": sqz_v.get("kc_lower"),
    }
