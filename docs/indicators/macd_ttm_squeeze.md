# MACD + TTM Squeeze — reference guide

The desk's own TradingView pane: **"Custom MACD Histogram with TTM Squeeze"**.
Pine source kept verbatim at [`macd_ttm_squeeze.pine`](macd_ttm_squeeze.pine) —
that file is the source of truth. The Python port exists so the system can
score off the **same numbers on the chart**, never a substitute computed from
something else.

## Parameters — do not "correct" these

| Input | Value | Note |
| --- | --- | --- |
| MACD fast | **14** | *not* the textbook 12 — the whole pane shifts if this is changed |
| MACD slow | 26 | |
| MACD signal | 9 | |
| Squeeze length | 20 | |
| BB multiplier | 2.0 | |
| KC multiplier | 1.5 | |

Three implementation details also have to match Pine, or the port drifts from
the chart while still looking plausible:

- EMAs are **SMA-seeded** (Pine's `ta.ema`).
- Squeeze stdev is **population**, not sample (Pine's `ta.stdev`).
- The Keltner range is an EMA of **true range** (Pine's `ta.tr`), and the
  first bar's TR is `high - low`.

These are pinned by [`tests/test_technicals_macd_squeeze.py`](../../tests/test_technicals_macd_squeeze.py).

## How the pane is read

### The histogram — four colours, four meanings

The colour is the momentum *derivative*, which is the read the bar height alone
doesn't give you. `hist_state` reproduces it exactly:

| Colour on the chart | `hist_state` | Meaning |
| --- | --- | --- |
| Lime | `rising_positive` | above zero **and building** — momentum with you |
| Green | `fading_positive` | above zero but **shrinking** — momentum rolling over while still positive |
| Red | `falling_negative` | below zero and **deepening** — downside building |
| Faded red | `recovering_negative` | below zero but **improving** — the turn, not yet the trend |

The useful pair is green and faded red: both are *turns in progress*, visible
before the histogram crosses zero.

A perfectly linear trend parks the histogram at exactly **zero** (the signal
EMA catches a constant MACD) and Pine paints that green, not lime. Flat
histogram = constant momentum, not building momentum.

`cross` flags the histogram's zero-line flip on the latest bar
(`bull_cross` / `bear_cross`) — i.e. the MACD line crossing its signal.

### The squeeze — energy, not direction

Squeeze **on** = Bollinger Bands entirely inside the Keltner Channels =
volatility compressed = energy stored. It says *nothing* about direction; the
histogram supplies that. Two reads the Pine plot leaves to the eye are computed
here:

- `bars_in_squeeze` — how long the current compression has held. Longer coil,
  bigger release.
- `fired` — the squeeze released **on this bar**. This is the tradeable event;
  "on" is the setup, "fired" is the trigger.

## Using it in code

```python
from macro_positioning.prices.fetcher import load_recent_prices
from macro_positioning.prices.technicals import indicator_pane, indicator_pane_mtf
from macro_positioning.rules.confluence import indicator_subscore, mtf_indicator_subscore

bars = load_recent_prices("PLTR", days=400)

pane = indicator_pane(bars)                    # one timeframe
pane["summary"]                                # "rising positive, macd>signal, squeeze off, rsi 61"

panes = indicator_pane_mtf(bars)               # {"1D": ..., "3D": ...} — his readable pair
mtf_indicator_subscore(panes, "long")          # -> Indicator subscore 0..2 + why
```

Individual pieces: `macd()`, `ttm_squeeze()`, `ema_series()`,
`true_range_series()` in [`prices/technicals.py`](../../src/macro_positioning/prices/technicals.py).
`compute_technical_features()` also carries the whole pane as flat keys
(`macd_hist_state`, `rsi14_above_signal`, `squeeze_on`, `squeeze_fired`, …), so anything
already consuming that dict can read the pane without new plumbing.

## Scoring — the Indicator subscore

The confluence rubric's third leg is *"Indicator 0..2 — 0 mixed-or-against /
1 partial / 2 full (MACD + RSI + Squeeze all agree)"*. It used to be typed in
by hand (`cli --confluence p,f,i`); `indicator_subscore(pane, side)` derives it.

Per-leg verdicts are `agree` / `neutral` / `against` / `unavailable`:

| Leg | Agrees with a long | Opposes a long |
| --- | --- | --- |
| MACD | histogram `rising_positive`, or a fresh `bull_cross` | histogram `falling_negative` |
| RSI(14) | **at or above its 14-period signal MA** | **below its MA** — momentum ceased |
| Squeeze | `fired`, or `on` (coiled) | **never** — compression has no direction |

RSI is read against its **own MA**, not the 30/70 bands — his stated rule is
"RSI below its MA = momentum ceased", whatever the absolute level. So RSI 35
over a 30 MA agrees with a long, and RSI 75 under an 80 MA does not.
`extended` (>80) and `washed_out` (<20) only ever downgrade an agreement to
neutral; they never manufacture opposition on their own.

The comparison is *at or above* deliberately: a relentless run with no down
closes pins RSI at 100 and drags its own MA up to meet it, and a strict `>`
would read that as momentum ceasing at its strongest point.

Short side mirrors it, with one asymmetry: a `recovering_negative` histogram
**opposes** a short (the thing you're short is turning up), while it's an early
agree for a long.

Rolled up: **2** all three agree · **1** at least one agrees and nothing
opposes · **0** anything opposes, or nothing agrees.

Two deliberate choices worth arguing with:

- **`unavailable` never counts as agreement.** A pane too short to read scores
  0, not 1 — absence of evidence isn't confluence.
- **The thresholds are scoring policy, not chart readings.** The RSI bands and
  the agree/neutral/against split are tunable constants at the top of
  [`rules/confluence.py`](../../src/macro_positioning/rules/confluence.py)
  (`RSI_LONG_AGREE`, …); tune them against outcomes. What they must never do is
  invent a reading the pane didn't produce.

Every result carries `reasons` (e.g. `"squeeze: squeeze on (7 bars coiled)"`),
so a score can always be argued with rather than taken on faith.

## Timeframes

His chart timeframes are **12h / 1D / 3D**, and he checks them against each
other for conflict. Of those, 1D and 3D are derivable from the stored daily
bars, so `PANE_TIMEFRAMES` defaults to `("1D", "3D")`; **12h needs intraday
history the DB doesn't hold yet.** 1W and 1M are also available on request.

Aggregation lives in
[`prices/resample.py`](../../src/macro_positioning/prices/resample.py) — no
extra fetch. Weeks are ISO Mon–Sun (TradingView's convention) and the
**current bucket is included while still forming**, because that's the candle
on screen (`include_partial=False` for backtests, where a forming candle leaks
future information).

**3D caveat:** TradingView builds multi-day candles by grouping trading
*sessions* from its own anchor, so our 3D boundaries can differ from his chart
by up to two sessions. A fixed calendar epoch (a Monday) is used instead
because it's reproducible — buckets don't shift as history is added, which a
session-count anchor would do. Use 3D for momentum state and confluence; don't
read exact levels off a resampled 3D bar and call them his.

`mtf_indicator_subscore()` takes the **weakest readable** timeframe, not the
best or the average: a daily agreeing while the weekly opposes is not
confluence. A timeframe with no readable leg is reported as `unreadable` and
excluded, so missing history can't cap a young ticker at zero.

### Limits, stated plainly

- **No 12h — one of his three timeframes is missing.** Intraday bars can't be
  aggregated from daily ones; they need intraday history in the DB first
  (`prices/provider.py` TODO). Asking `indicator_pane_mtf()` for `"12h"`
  raises rather than quietly handing back a daily read labelled 12h.
- **Monthly MACD needs ~34 monthly bars** — about three years of daily
  history. With ~400 days stored, a 1M pane returns squeeze + RSI only, and
  `macd` is `None`. Such a timeframe is reported `unreadable`, not scored 0.
