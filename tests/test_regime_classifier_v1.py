"""Tests for the v1 blended regime classifier.

Each test synthesises a price tape with a known leadership pattern and
asserts the classifier recovers the regime that pattern implies. The
interesting cases are the discriminators: gold-with-copper must read as
commodity-led inflation while gold-without-copper reads as debasement,
and a tape where nobody leads (or where fast and slow leadership
disagree) must read as transitional chop rather than being forced into
whichever regime is marginally ahead.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from macro_positioning.db.schema import initialize_database
from macro_positioning.regime.classifier_v1 import (
    BASKETS,
    FRAMEWORK_REGIMES,
    classify_regime_v1,
)
from macro_positioning.regime.snapshots import (
    classify_and_store,
    dominant_with_hysteresis,
    load_regime_history,
    smooth_blends,
)


AS_OF = date(2026, 8, 20)
_BARS = 260


@pytest.fixture
def db(tmp_path: Path):
    p = tmp_path / "regime.db"
    initialize_database(p)
    conn = sqlite3.connect(p)
    yield conn
    conn.close()


def _write_tape(
    conn: sqlite3.Connection,
    drifts: dict[str, float],
    *,
    as_of: date = AS_OF,
    bars: int = _BARS,
    fast_drifts: dict[str, float] | None = None,
    fast_bars: int = 25,
) -> None:
    """Write synthetic daily closes with a per-basket log drift.

    `drifts` maps basket name → daily log drift applied to every member.
    `fast_drifts`, when given, overrides the drift for the final
    `fast_bars` days — that's how a leadership handoff is staged.

    A small deterministic wobble is added so realised vol is non-zero
    (the classifier divides by it) and identical across baskets, which
    keeps the vol normalisation from favouring any one basket.
    """
    rows = []
    for basket, tickers in BASKETS.items():
        drift = drifts.get(basket, 0.0)
        fast = (fast_drifts or {}).get(basket)
        for ticker in tickers:
            price = 100.0
            for i in range(bars):
                d = as_of - timedelta(days=bars - 1 - i)
                step = drift if (fast is None or i < bars - fast_bars) else fast
                wobble = 0.006 * math.sin(i * 1.7 + len(ticker))
                price *= math.exp(step + wobble)
                rows.append((
                    f"{ticker}-{d.isoformat()}", ticker, f"{d.isoformat()}T00:00:00+00:00",
                    "1D", price, price, price, price, 1000.0, "test",
                    datetime.now(UTC).isoformat(),
                ))
    conn.executemany(
        "INSERT OR REPLACE INTO prices (price_id, ticker, observed_at, timeframe, "
        "open, high, low, close, volume, provider, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def _write_fred(conn: sqlite3.Connection, values: dict[str, list[tuple[str, float]]]) -> None:
    rows = [
        (sid, d, v, None, None, datetime.now(UTC).isoformat())
        for sid, points in values.items()
        for d, v in points
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO fred_observations (series_id, observation_date, value, "
        "realtime_start, realtime_end, fetched_at) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def _flat_series(sid: str, value: float, *, as_of: date = AS_OF) -> dict:
    """A FRED series that hasn't moved — used to neutralise a modifier."""
    return {sid: [
        ((as_of - timedelta(days=n)).isoformat(), value) for n in (0, 30, 90, 180)
    ]}


# ---------------------------------------------------------------------------
# Blend shape
# ---------------------------------------------------------------------------

def test_blend_is_a_distribution_over_all_regimes(db):
    _write_tape(db, {"equity_beta": 0.0025})
    read = classify_regime_v1(db, as_of=AS_OF)
    assert read is not None
    assert set(read.blend) == set(FRAMEWORK_REGIMES)
    assert all(0.0 <= v <= 1.0 for v in read.blend.values())
    assert read.blend[read.dominant] == max(read.blend.values())
    assert sum(read.blend.values()) == pytest.approx(1.0, abs=1e-3)


def test_returns_none_when_there_is_no_price_history(db):
    assert classify_regime_v1(db, as_of=AS_OF) is None


def test_stale_tickers_are_dropped_not_silently_used(db):
    # Whole tape ends six weeks before as_of → every member is stale.
    _write_tape(db, {"equity_beta": 0.0025}, as_of=AS_OF - timedelta(days=42))
    assert classify_regime_v1(db, as_of=AS_OF) is None


# ---------------------------------------------------------------------------
# Regime signatures
# ---------------------------------------------------------------------------

def test_equity_leadership_reads_risk_on(db):
    _write_tape(db, {"equity_beta": 0.0030, "defensive": -0.0010})
    read = classify_regime_v1(db, as_of=AS_OF)
    assert read.dominant == "risk_on_expansion"


def test_defensive_leadership_reads_risk_off(db):
    _write_tape(db, {"defensive": 0.0025, "equity_beta": -0.0030})
    read = classify_regime_v1(db, as_of=AS_OF)
    assert read.dominant == "risk_off_contraction"


def test_gold_with_copper_and_rising_breakevens_reads_commodity_led(db):
    """Hard money AND cyclicals leading together, with inflation
    expectations confirming — that is a real inflation, not debasement."""
    _write_tape(db, {"cyclical_commodity": 0.0032, "hard_money": 0.0026})
    _write_fred(db, {
        "T10YIE": [
            (AS_OF.isoformat(), 2.90),
            ((AS_OF - timedelta(days=60)).isoformat(), 2.40),
        ],
    })
    read = classify_regime_v1(db, as_of=AS_OF)
    assert read.dominant == "commodity_led_inflation"
    assert read.blend["commodity_led_inflation"] > read.blend["monetary_debasement_hard_asset"]


def test_gold_without_copper_and_flat_breakevens_reads_debasement(db):
    """The discriminator: same gold strength, but cyclicals lagging and
    breakevens flat. That is a store-of-value bid, not an inflation hedge."""
    _write_tape(db, {"hard_money": 0.0032, "cyclical_commodity": -0.0008})
    _write_fred(db, _flat_series("T10YIE", 2.30))
    read = classify_regime_v1(db, as_of=AS_OF)
    assert read.dominant == "monetary_debasement_hard_asset"
    assert read.blend["monetary_debasement_hard_asset"] > read.blend["commodity_led_inflation"]
    assert any("debasement bid" in e for e in read.evidence)


def test_flat_tape_reads_transitional_chop(db):
    """Nobody leading → chop, not a coin-flip between directional regimes."""
    _write_tape(db, {})
    read = classify_regime_v1(db, as_of=AS_OF)
    assert read.dominant == "transitional_chop"
    # No directional regime may claim meaningful weight — the four of them
    # should sit near-uniform rather than one edging ahead on noise.
    directional = [v for r, v in read.blend.items() if r != "transitional_chop"]
    assert max(directional) - min(directional) < 0.05


# ---------------------------------------------------------------------------
# Velocity — the handoff measure
# ---------------------------------------------------------------------------

def test_velocity_points_toward_the_incoming_regime(db):
    """Equity-led for most of the year, hard-money-led in the last month.
    The blend still carries the old regime; velocity must show the new one
    arriving and the old one leaving."""
    _write_tape(
        db,
        {"equity_beta": 0.0028},
        fast_drifts={"equity_beta": -0.0010, "hard_money": 0.0060},
        fast_bars=25,
    )
    read = classify_regime_v1(db, as_of=AS_OF)
    assert read.velocity["monetary_debasement_hard_asset"] > 0.05
    assert read.velocity["risk_on_expansion"] < 0.0
    rising, falling = read.top_movers()
    assert rising[0][0] == "monetary_debasement_hard_asset"
    assert falling[0][0] == "risk_on_expansion"


def test_handoff_in_progress_raises_chop_weight(db):
    """Fast and slow leadership pointing different ways is itself the
    signal that a regime change is underway — chop should carry real
    weight, and directional conviction should fall, while the tape is
    genuinely contested.

    The drifts are sized so the short windows have flipped to hard money
    while 60d/120d still favour equity. A bigger fast move would show up
    in the slow windows too, and would correctly read as decisive rather
    than contested."""
    _write_tape(
        db,
        {"equity_beta": 0.0028},
        fast_drifts={"equity_beta": -0.0015, "hard_money": 0.0035},
        fast_bars=18,
    )
    read = classify_regime_v1(db, as_of=AS_OF)
    assert read.blend["transitional_chop"] > 0.15
    assert any("regime change in progress" in e for e in read.evidence)
    # Directional conviction is damped: the regime being handed off from
    # must not still look like the strongest directional story.
    assert read.blend["risk_on_expansion"] < read.blend["transitional_chop"]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_snapshot_persists_blend_and_state_columns(db):
    _write_tape(db, {"hard_money": 0.0032, "cyclical_commodity": -0.0008})
    _write_fred(db, {
        **_flat_series("T10YIE", 2.30),
        **_flat_series("NFCI", -0.55),
        **_flat_series("VIXCLS", 14.5),
    })
    snap = classify_and_store(db, as_of=AS_OF)
    assert snap is not None
    db.commit()

    row = db.execute(
        "SELECT framework_regime, liquidity_state, volatility_state, "
        "confidence_score, classifier_version FROM macro_regimes"
    ).fetchone()
    assert row[0] == "monetary_debasement_hard_asset"
    assert row[1] == "easing"
    assert row[2] == "low"
    assert 20 <= row[3] <= 92
    assert row[4] == "regime_classifier@blend-v1"

    history = load_regime_history(db, days=5)
    assert history[-1]["blend"]["monetary_debasement_hard_asset"] > 0
    assert history[-1]["synthetic"] is False


# ---------------------------------------------------------------------------
# Smoothing + hysteresis
# ---------------------------------------------------------------------------

def _series(dates_and_leaders: list[tuple[str, str]]) -> list[dict]:
    out = []
    for d, leader in dates_and_leaders:
        blend = {r: 0.1 for r in FRAMEWORK_REGIMES}
        blend[leader] = 0.6
        out.append({"date": d, "blend": blend})
    return out


def test_smoothing_carries_forward_across_days_without_a_blend(db):
    snaps = [
        {"date": "2026-01-01", "blend": {r: (0.6 if r == "risk_on_expansion" else 0.1)
                                         for r in FRAMEWORK_REGIMES}},
        {"date": "2026-01-02", "blend": None},   # stub/backfill row
        {"date": "2026-01-03", "blend": None},
    ]
    out = smooth_blends(snaps)
    assert out[0]["blend"] is not None
    assert out[2]["blend"] == out[0]["blend"]


def test_hysteresis_ignores_a_two_day_challenger(db):
    days = [(f"2026-01-{n:02d}", "risk_on_expansion") for n in range(1, 21)]
    days[10] = ("2026-01-11", "commodity_led_inflation")
    days[11] = ("2026-01-12", "commodity_led_inflation")
    out = dominant_with_hysteresis(smooth_blends(_series(days)))
    assert {r["regime"] for r in out} == {"risk_on_expansion"}


def test_hysteresis_accepts_a_sustained_challenger(db):
    days = [(f"2026-01-{n:02d}", "risk_on_expansion") for n in range(1, 11)]
    days += [(f"2026-01-{n:02d}", "commodity_led_inflation") for n in range(11, 29)]
    out = dominant_with_hysteresis(smooth_blends(_series(days)))
    assert out[0]["regime"] == "risk_on_expansion"
    assert out[-1]["regime"] == "commodity_led_inflation"
