"""The measurement → trust-weight loop.

`call_outcomes` is what the desk measured; `input_authors.trust_weight`
is what the blend actually multiplies a signal by. Until Sep 2026 nothing
connected them: every Telegram author sat on a hand-seeded 1.5 while the
backtest quietly said one of them ran +3.4% alpha and another 0.0%.

These tests pin the four properties that make the connection safe to run
unattended on a daily schedule.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from macro_positioning.db.schema import initialize_database
from macro_positioning.learning.call_accuracy import _ensure_table
from macro_positioning.learning.trust_from_outcomes import (
    MIN_CALLS,
    PRIOR,
    recompute_trust_from_outcomes,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    d = tmp_path / "trust.db"
    initialize_database(d)
    with sqlite3.connect(d) as conn:
        _ensure_table(conn)
    return d


def _author(db: Path, author_id: str, name: str, channel: str,
            parent: str | None = None, weight: float = PRIOR) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO input_authors
               (author_id, display_name, channel, parent_channel, trust_weight)
               VALUES (?,?,?,?,?)""",
            (author_id, name, channel, parent, weight),
        )


def _outcomes(db: Path, author_id: str, n: int, alpha: float, *,
              real: int = 1, band: str = "swing", symbol: str = "BTC") -> None:
    with sqlite3.connect(db) as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO call_outcomes
               (document_id, author_id, symbol, direction, resolved,
                fwd_return_pct, alpha_pct, real_asset, tf_band,
                stop_width_pct, call_at, scored_at)
               VALUES (?,?,?,'long','win',?,?,?,?,0.05,
                       '2026-06-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')""",
            [(f"{author_id}-{band}-{symbol}-{i}", author_id, symbol,
              alpha, alpha, real, band) for i in range(n)],
        )


def _weight(db: Path, author_id: str) -> float:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT trust_weight FROM input_authors WHERE author_id=?", (author_id,)
        ).fetchone()[0]


def test_running_it_twice_changes_nothing(db):
    """Shrinkage anchors on the SEED, not on the stored weight.

    Anchoring on the stored value makes this an exponential moving
    average: on a daily schedule every source ratchets all the way to its
    raw measurement and the shrinkage it was built for stops existing.
    """
    _author(db, "a:one", "One", "Chan")
    _outcomes(db, "a:one", 100, alpha=3.0)

    recompute_trust_from_outcomes(db_path=db)
    after_first = _weight(db, "a:one")
    assert after_first > PRIOR

    run = recompute_trust_from_outcomes(db_path=db)
    assert run.updated == 0, "a second pass must be a no-op"
    assert _weight(db, "a:one") == after_first


def test_a_thin_sample_does_not_move_the_weight(db):
    """Three lucky calls must not outrank a measured veteran."""
    _author(db, "a:thin", "Thin", "Chan")
    _outcomes(db, "a:thin", MIN_CALLS - 1, alpha=5.0)

    run = recompute_trust_from_outcomes(db_path=db)
    assert run.skipped_small == 1
    assert _weight(db, "a:thin") == PRIOR


def test_more_evidence_moves_the_weight_further(db):
    """Same alpha, more calls — shrinkage lets the bigger sample speak."""
    _author(db, "a:small", "Small", "Chan")
    _author(db, "a:big", "Big", "Chan")
    _outcomes(db, "a:small", 30, alpha=4.0)
    _outcomes(db, "a:big", 600, alpha=4.0)

    recompute_trust_from_outcomes(db_path=db)
    assert _weight(db, "a:big") > _weight(db, "a:small") > PRIOR


def test_the_dex_tail_gets_no_vote(db):
    """An edge on a microcap the desk cannot size into is not an edge it
    can spend. Those rows stay in the table but must not move a weight."""
    _author(db, "a:memes", "Memes", "Chan")
    _outcomes(db, "a:memes", 400, alpha=9.0, real=0)

    run = recompute_trust_from_outcomes(db_path=db)
    assert run.updated == 0
    assert _weight(db, "a:memes") == PRIOR


def test_one_person_two_channels_gets_one_pooled_weight(db):
    """Big_Nuts posts under Feather Hands and under Market Traders. Both
    ids must end on the SAME weight, computed from the pooled evidence —
    otherwise the blend rates the same human differently depending on
    which channel relayed the post."""
    _author(db, "fh:nuts", "Big_Nuts", "Feather Hands")
    _author(db, "mt:nuts", "Big_Nuts", "Market Traders", parent="Feather Hands")
    _outcomes(db, "fh:nuts", 300, alpha=-3.0)
    _outcomes(db, "mt:nuts", 100, alpha=-3.0, symbol="ETH")

    run = recompute_trust_from_outcomes(db_path=db)
    assert _weight(db, "fh:nuts") == _weight(db, "mt:nuts") < PRIOR
    pooled = [u for u in run.updates if u.display_name == "Big_Nuts"][0]
    assert pooled.n_calls == 400, "evidence from both ids counts once, together"


def test_an_alias_left_behind_gets_caught_up(db):
    """The canonical id can already be at its converged value while a
    sibling still carries the seed. Gating the write on 'did the canonical
    move' strands that sibling forever — which is how Big_Nuts ended up
    weighted 1.04 on Feather Hands and 1.50 on Market Traders."""
    _author(db, "fh:nuts", "Big_Nuts", "Feather Hands")
    _author(db, "mt:nuts", "Big_Nuts", "Market Traders", parent="Feather Hands")
    _outcomes(db, "fh:nuts", 400, alpha=-3.0)

    recompute_trust_from_outcomes(db_path=db)
    converged = _weight(db, "fh:nuts")
    assert _weight(db, "mt:nuts") == converged

    # Knock one alias out of step; the next pass must repair it even
    # though the canonical is unchanged.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE input_authors SET trust_weight=? WHERE author_id='mt:nuts'",
            (PRIOR,),
        )
    run = recompute_trust_from_outcomes(db_path=db)
    assert run.updated == 1
    assert _weight(db, "mt:nuts") == converged


def test_unrelated_desks_sharing_a_handle_stay_separate(db):
    """Pooling is by person, and two channels with no family link are not
    assumed to be one just because a handle collides."""
    _author(db, "x:alpha", "Trader", "Desk X")
    _author(db, "y:alpha", "Trader", "Desk Y")
    _outcomes(db, "x:alpha", 200, alpha=4.0)
    _outcomes(db, "y:alpha", 200, alpha=-4.0, symbol="ETH")

    recompute_trust_from_outcomes(db_path=db)
    assert _weight(db, "x:alpha") > PRIOR > _weight(db, "y:alpha")


# ── Segmentation and the benchmark ────────────────────────────────────


def test_timeframe_segmentation_splits_one_author_into_its_businesses(db):
    """An author is not one edge. The same person posting 15m scalps and
    1W Elliott counts measured 0.81% vs 6.69% expectancy in Sep 2026;
    a single blended row describes neither."""
    from macro_positioning.learning.call_accuracy import source_accuracy

    _author(db, "a:both", "Both", "Chan")
    _outcomes(db, "a:both", 50, alpha=0.5, band="intraday")
    _outcomes(db, "a:both", 50, alpha=6.0, band="position", symbol="ETH")

    blended = source_accuracy(db_path=db)
    assert len(blended) == 1
    assert blended[0]["avg_alpha_pct"] == pytest.approx(3.25, abs=0.01)

    split = source_accuracy(db_path=db, by_timeframe=True)
    bands = {r["tf_band"]: r["avg_alpha_pct"] for r in split}
    assert bands["intraday"] == pytest.approx(0.5, abs=0.01)
    assert bands["position"] == pytest.approx(6.0, abs=0.01)


def test_real_assets_only_is_the_default(db):
    from macro_positioning.learning.call_accuracy import source_accuracy

    _author(db, "a:mix", "Mix", "Chan")
    _outcomes(db, "a:mix", 20, alpha=1.0, real=1)
    _outcomes(db, "a:mix", 80, alpha=9.0, real=0, symbol="ETH")

    assert source_accuracy(db_path=db)[0]["avg_alpha_pct"] == pytest.approx(1.0, abs=0.01)
    everything = source_accuracy(db_path=db, real_assets_only=False)[0]
    assert everything["avg_alpha_pct"] == pytest.approx(7.4, abs=0.01)


def test_equities_benchmark_to_spy_and_crypto_to_btc():
    """Subtracting BTC from an equity call measures how crypto did that
    week, not the caller. Stock Unlocked read -14.3% alpha in Sep 2026
    almost entirely from this."""
    from macro_positioning.learning.call_accuracy import _benchmark_for

    assert _benchmark_for("BTC") == "BTC"
    assert _benchmark_for("ETH") == "BTC"
    assert _benchmark_for("SOL-USD") == "BTC"
    assert _benchmark_for("AAPL") == "SPY"
    assert _benchmark_for("MSTR") == "SPY"
    assert _benchmark_for(None) == "SPY" or _benchmark_for(None) == "BTC"
