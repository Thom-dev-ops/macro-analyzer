"""Composing a target from structure, voices and the agent's own rails.

The regression these guard is a real one. On 2026-09-05 the book opened
PLTR at 174.42 against an agent target of 244.83, and the composed view
replaced that target with a trusted-voice consensus at 172 — a level
*below* the entry, drawn weeks earlier for a trade that started at 160.
The position was past its target the instant it filled, trimmed half on
the phantom "target hit", ratcheted its stop to breakeven above spot and
stopped out on the next tick for the round-trip slippage.
"""

import pytest

from macro_positioning.paper.valuation import valuate
from macro_positioning.scoring.kol_levels import Consensus, Contributor, KolLevels

ATR = 8.0
ENTRY = 174.417165
STOP = 161.85135          # 12.5658 of risk, as the live position carried
AGENT_TARGET = 244.827441


def _voices(price, *, trusted=True):
    c = Contributor(
        author_id="ari_gold", display_name="Ari Gold", price=price, weight=0.6,
        setup_win_rate=0.55 if trusted else None, meaningful=trusted,
        n_calls=40 if trusted else 2, conviction=2.5, at="2026-08-08 16:20",
    )
    return KolLevels(target=Consensus(price=price, weight=0.6,
                                      contributors=[c], trusted=trusted))


def _valuate(kol, *, side="LONG", entry=ENTRY, target=AGENT_TARGET):
    return valuate(
        ticker="PLTR", side=side, entry=entry, stop=STOP, target=target,
        atr=ATR, kol=kol, min_support=35.0,
    )


# --- the played-out call ------------------------------------------------

def test_a_voice_target_behind_the_entry_is_never_adopted():
    val = _valuate(_voices(172.0))
    assert val.target == pytest.approx(AGENT_TARGET), (
        "172 sits below a 174.42 long entry — it is not a shorter read of "
        "the same move, it is a finished trade"
    )
    assert val.target_source == "agent"


def test_the_played_out_call_is_reported_as_stale_not_as_opposition():
    val = _valuate(_voices(172.0))
    voices = next(e for e in val.evidence if e.view == "trusted_voices")
    assert voices.stance == "stale"
    assert voices.delta == 0.0, "a dead call neither supports nor argues"
    assert "already played out" in voices.detail


def test_a_short_is_gated_on_its_own_side():
    val = _valuate(_voices(190.0), side="SHORT", target=140.0)
    voices = next(e for e in val.evidence if e.view == "trusted_voices")
    assert voices.stance == "stale"
    assert val.target == pytest.approx(140.0)


# --- the gate does not swallow the legitimate case ----------------------

def test_a_nearer_target_still_ahead_of_entry_is_adopted():
    val = _valuate(_voices(200.0))
    assert val.target == pytest.approx(200.0), (
        "200 is short of the chart's 244.83 but still ahead of entry — "
        "the conservative read the composition exists to take"
    )
    assert val.target_source == "trusted_voices"


def test_a_target_beyond_the_agents_leaves_the_agent_target_alone():
    val = _valuate(_voices(300.0))
    assert val.target == pytest.approx(AGENT_TARGET)
    assert val.target_source == "agent"


# --- R:R carries the sign ----------------------------------------------

def test_reward_behind_the_entry_cannot_report_positive_r():
    val = valuate(
        ticker="PLTR", side="LONG", entry=ENTRY, stop=STOP, target=172.0,
        atr=ATR, kol=None, min_support=35.0,
    )
    assert val.rr is not None and val.rr < 0, (
        "abs() used to render this as +0.2R, which is how it cleared the bar"
    )
    assert any("behind the" in b for b in val.blockers)


def test_a_real_long_still_reports_positive_r():
    val = _valuate(None)
    assert val.rr == pytest.approx((AGENT_TARGET - ENTRY) / (ENTRY - STOP), rel=1e-3)
    assert not any("behind the" in b for b in val.blockers)
