"""Regime snapshot writer + reader.

Idempotency, backfill, transition derivation, and desk_data
integration all get one focused test so a regression won't blank
/home's timeline chart.
"""
from __future__ import annotations

import sqlite3

import pytest

from macro_positioning.db.schema import initialize_database
from macro_positioning.regime.snapshots import (
    derive_transitions,
    load_regime_history,
    record_daily_regime_snapshot,
    since_days_for_current,
)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "regime.db"
    initialize_database(p)
    with sqlite3.connect(p) as c:
        yield c


def test_first_write_backfills_84_days_plus_today(db):
    res = record_daily_regime_snapshot(db, seed_history=True)
    assert res["backfilled"] == 84
    hist = load_regime_history(db, days=90)
    # 84 backfilled + 1 today's classification, possibly less if today's date
    # collides with a backfill row on the boundary.
    assert 84 <= len(hist) <= 85


def test_second_write_same_day_is_idempotent(db):
    record_daily_regime_snapshot(db, seed_history=True)
    hist_before = load_regime_history(db, days=90)
    record_daily_regime_snapshot(db, seed_history=True)
    hist_after = load_regime_history(db, days=90)
    assert len(hist_before) == len(hist_after)


def test_backfill_skipped_when_table_non_empty(db):
    record_daily_regime_snapshot(db, seed_history=True)
    # Wipe today's row so the writer has fresh work but leave history in place
    db.execute("DELETE FROM macro_regimes WHERE substr(classified_at,1,10) = date('now')")
    db.commit()
    res = record_daily_regime_snapshot(db, seed_history=True)
    assert res["backfilled"] == 0  # existing rows → no re-seeding


def test_transitions_collapse_consecutive_same_regime(db):
    record_daily_regime_snapshot(db, seed_history=True)
    hist = load_regime_history(db, days=90)
    trans = derive_transitions(hist)
    # Backfill inserts 3 segments → 2 transition events.
    assert len(trans) == 2
    for t in trans:
        # Contract is additive — the SPA reads these by name, and v1 also
        # attaches slugs plus how long the outgoing regime had held.
        assert {"date", "from", "to"} <= set(t.keys())
        # Human-readable labels, not slugs
        assert "_" not in t["from"]
        assert "_" not in t["to"]
        assert t["fromSlug"] and t["toSlug"]
        assert t["priorRunDays"] >= 1


def test_since_days_for_current_walks_back_from_end(db):
    record_daily_regime_snapshot(db, seed_history=True)
    hist = load_regime_history(db, days=90)
    since = since_days_for_current(hist)
    # Current segment is the tail of the backfill (~28 days) plus today
    assert 20 <= since <= 35


def test_confidence_trace_has_real_variance(db):
    """The whole point of this table — the trace has to move so the
    /home timeline chart renders instead of showing the pending pill."""
    record_daily_regime_snapshot(db, seed_history=True)
    hist = load_regime_history(db, days=90)
    confs = [s["confidence"] for s in hist]
    assert max(confs) - min(confs) >= 0.05  # SPA gate is 0.02


def test_load_history_empty_table_returns_empty(db):
    hist = load_regime_history(db, days=90)
    assert hist == []
