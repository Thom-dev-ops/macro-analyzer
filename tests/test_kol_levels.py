"""Trusted-voice level consensus (scoring/kol_levels.py)."""

from datetime import UTC, datetime, timedelta

from macro_positioning.scoring.kol_levels import (
    Contributor,
    _cluster,
    _conviction_weight,
    _played_out,
    _recency_weight,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _contrib(price, weight, *, name="A", meaningful=True, win=0.8):
    return Contributor(
        author_id=name.lower(), display_name=name, price=price, weight=weight,
        setup_win_rate=win, meaningful=meaningful, n_calls=50,
        conviction=3.0, at="2026-08-20 10:00",
    )


# --- recency ------------------------------------------------------------

def test_a_call_from_today_outweighs_one_from_a_fortnight_ago():
    fresh = _recency_weight(NOW.isoformat(), now=NOW)
    old = _recency_weight((NOW - timedelta(days=14)).isoformat(), now=NOW)
    assert fresh > old
    assert abs(old - 0.5) < 0.01, "14 days is the half-life"


def test_levels_past_the_age_cap_carry_no_weight():
    ancient = (NOW - timedelta(days=90)).isoformat()
    assert _recency_weight(ancient, now=NOW) == 0.0


def test_unparseable_dates_get_half_weight_rather_than_being_dropped():
    assert _recency_weight("not-a-date", now=NOW) == 0.5
    assert _recency_weight(None, now=NOW) == 0.5


# --- conviction ---------------------------------------------------------

def test_conviction_scales_but_never_silences():
    assert _conviction_weight(5.0) == 1.0
    assert _conviction_weight(0.0) == 0.2, "a quiet mention still counts"
    assert _conviction_weight(None) == 0.2
    assert _conviction_weight(2.5) == 0.5


# --- consensus clustering ----------------------------------------------

def test_agreeing_voices_form_one_consensus_level():
    c = _cluster([_contrib(3000, 0.8), _contrib(3020, 0.6), _contrib(2990, 0.5)], tolerance=60)
    assert c is not None
    assert 2990 <= c.price <= 3020
    assert len(c.contributors) == 3


def test_the_heavier_cluster_wins_over_the_more_numerous_one():
    """Two unproven voices shouldn't outvote one backtested author."""
    light = [_contrib(4000, 0.05, name="Unproven1", meaningful=False, win=None),
             _contrib(4010, 0.05, name="Unproven2", meaningful=False, win=None)]
    heavy = [_contrib(3000, 0.9, name="Proven")]
    c = _cluster(light + heavy, tolerance=60)
    assert c.price == 3000
    assert c.contributors[0].display_name == "Proven"


def test_consensus_price_is_weight_biased_not_a_plain_average():
    c = _cluster([_contrib(3000, 0.9), _contrib(3050, 0.1)], tolerance=100)
    assert c.price < 3025, "the heavier voice should pull the level"


def test_contributors_are_ranked_by_weight():
    c = _cluster([_contrib(3000, 0.2, name="Quiet"), _contrib(3010, 0.9, name="Loud")], tolerance=100)
    assert [x.display_name for x in c.contributors] == ["Loud", "Quiet"]


def test_a_consensus_names_who_called_it_and_why_they_count():
    c = _cluster([_contrib(3000, 0.8, name="OG Whales", win=0.83)], tolerance=60)
    assert "OG Whales" in c.basis
    assert "83% setup win" in c.basis
    assert c.trusted is True


def test_unproven_voices_are_labelled_as_such():
    c = _cluster([_contrib(3000, 0.15, name="New", meaningful=False, win=None)], tolerance=60)
    assert c.trusted is False
    assert "unproven" in c.contributors[0].credential


def test_no_points_or_no_tolerance_yields_no_consensus():
    assert _cluster([], tolerance=60) is None
    assert _cluster([_contrib(3000, 0.8)], tolerance=0) is None


def test_zero_weight_points_do_not_produce_a_level():
    assert _cluster([_contrib(3000, 0.0), _contrib(3010, 0.0)], tolerance=60) is None


# --- calls the market has already settled -------------------------------

def test_a_long_target_at_or_below_spot_has_played_out():
    row = {"side": "LONG", "target": 172.0}
    assert _played_out(row, 174.33) is True
    assert _played_out(row, 172.0) is True, "touching the target still resolves it"
    assert _played_out(row, 169.0) is False


def test_a_short_target_at_or_above_spot_has_played_out():
    row = {"side": "SHORT", "target": 172.0}
    assert _played_out(row, 169.0) is True
    assert _played_out(row, 174.33) is False


def test_without_spot_nothing_is_dropped():
    row = {"side": "LONG", "target": 172.0}
    assert _played_out(row, None) is False
    assert _played_out(row, 0.0) is False


def test_rows_with_no_target_or_no_direction_survive():
    assert _played_out({"side": "LONG", "target": None}, 174.33) is False
    assert _played_out({"side": "WATCH", "target": 172.0}, 174.33) is False
