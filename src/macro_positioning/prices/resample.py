"""Aggregate stored bars into higher timeframes.

The desk reads the same indicator pane on several timeframes, but the DB
only holds daily bars (intraday is a TODO in `prices/provider.py`). Weekly
and monthly don't need a new fetch — they're exact aggregations of the
daily history, provided that history is complete.

Convention matches what TradingView shows: the CURRENT bucket is included
even when it's still forming, because that's the candle on his screen. Use
`include_partial=False` for backtests, where a forming candle would leak
future information into the bar it sits in.

  - "3D" buckets three calendar days at a time from a fixed epoch Monday
  - "1W" buckets by ISO week (Mon-Sun), the TradingView default
  - "1M" buckets by calendar month

3D CAVEAT, stated plainly: TradingView builds multi-day candles by grouping
trading SESSIONS from its own anchor, so our 3D boundaries can differ from
his chart by up to two sessions. A fixed calendar epoch is used instead
because it is reproducible — buckets don't shift as history is added, which
a session-count anchor would do. Use 3D for confluence and momentum state;
do NOT read exact levels off a resampled 3D bar and call them his.

NO pandas: same reason as `technicals.py` — N is small and the import cost
isn't worth it.
"""

from __future__ import annotations

from datetime import date, timedelta

from macro_positioning.prices.provider import PriceBar


SUPPORTED = ("1D", "3D", "1W", "1M")

# Anchor for fixed-width multi-day buckets. A Monday, so 3D groups align
# with the start of a trading week rather than mid-week.
_EPOCH = date(1970, 1, 5)
_MULTI_DAY = {"3D": 3}


def _bucket_key(d: date, timeframe: str) -> tuple:
    if timeframe in _MULTI_DAY:
        return ((d - _EPOCH).days // _MULTI_DAY[timeframe],)
    if timeframe == "1W":
        iso = d.isocalendar()
        return (iso[0], iso[1])
    if timeframe == "1M":
        return (d.year, d.month)
    raise ValueError(f"unsupported timeframe {timeframe!r}; want one of {SUPPORTED}")


def _bucket_end(d: date, timeframe: str) -> date:
    """Last calendar day the bucket can contain — used to spot a forming bar."""
    if timeframe in _MULTI_DAY:
        width = _MULTI_DAY[timeframe]
        start = _EPOCH + timedelta(days=((d - _EPOCH).days // width) * width)
        return start + timedelta(days=width - 1)
    if timeframe == "1W":
        return d + timedelta(days=6 - d.weekday())
    if timeframe == "1M":
        nxt = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
        return nxt - timedelta(days=1)
    raise ValueError(f"unsupported timeframe {timeframe!r}")


def _as_date(observed_at: str) -> date:
    # Daily rows are "YYYY-MM-DD"; intraday rows are ISO datetimes.
    return date.fromisoformat(observed_at[:10])


def resample_bars(
    bars: list[PriceBar],
    timeframe: str,
    *,
    include_partial: bool = True,
) -> list[PriceBar]:
    """Aggregate daily `bars` into `timeframe` OHLCV bars, oldest first.

    "1D" is returned unchanged. A bucket's open is its first bar's open,
    close its last bar's close, high/low the extremes, volume the sum
    (None when any constituent bar has no volume — don't invent totals for
    sources that don't report it). `observed_at` is the last DAY the bucket
    actually contains, so a forming bar is dated today, not at week end.
    """
    if timeframe == "1D":
        return list(bars)
    if timeframe not in SUPPORTED:
        raise ValueError(f"unsupported timeframe {timeframe!r}; want one of {SUPPORTED}")
    if not bars:
        return []

    ordered = sorted(bars, key=lambda b: b.observed_at)
    buckets: dict[tuple, list[PriceBar]] = {}
    for b in ordered:
        buckets.setdefault(_bucket_key(_as_date(b.observed_at), timeframe), []).append(b)

    out: list[PriceBar] = []
    for key in sorted(buckets):
        group = buckets[key]
        last = group[-1]
        last_day = _as_date(last.observed_at)
        if not include_partial and last_day < _bucket_end(last_day, timeframe):
            continue  # still forming — drop it
        highs = [b.high if b.high is not None else b.close for b in group]
        lows = [b.low if b.low is not None else b.close for b in group]
        vols = [b.volume for b in group]
        out.append(PriceBar(
            ticker=last.ticker,
            observed_at=last.observed_at,
            timeframe=timeframe,
            open=group[0].open if group[0].open is not None else group[0].close,
            high=max(highs),
            low=min(lows),
            close=last.close,
            volume=sum(vols) if all(v is not None for v in vols) else None,
            provider=f"resampled:{last.timeframe}->{timeframe}",
        ))
    return out


def is_forming(bar: PriceBar) -> bool:
    """True when a resampled bar's bucket hasn't closed yet."""
    if bar.timeframe == "1D" or bar.timeframe not in SUPPORTED:
        return False
    d = _as_date(bar.observed_at)
    return d < _bucket_end(d, bar.timeframe)
