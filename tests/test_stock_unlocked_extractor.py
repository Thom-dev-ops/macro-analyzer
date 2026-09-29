"""Tests for the Stock Unlocked text-call parser.

The channel types its calls by hand, so the interesting cases are the
ragged ones: shorts whose target percentages are printed positive, entries
written five different ways, markdown-escaped level lines, follow-up posts
that carry no `— UPDATE` marker, and the desk's own test messages — which
must never reach the scorer.
"""

from __future__ import annotations

from macro_positioning.signals.stock_unlocked_extractor import (
    parse_post,
    to_features,
)


def _post(body: str) -> dict:
    parsed = parse_post(body)
    assert parsed is not None, "template should parse"
    return parsed


def test_long_entry_with_four_targets():
    p = _post(
        "ASAN | STOCK SWING TRADE\n\n"
        "ASAN Entry @ $8.35\n\n"
        "Target 1: $8.70 (+4.19%)\nTarget 2: $9.00 (+7.78%)\n"
        "Target 3: $9.25 (+10.78%)\nTarget 4: $9.50 (+13.77%)\n\n"
        "Stop Loss: $7.84 (-6.11%)\n\n"
        "Notes: Ichinisan + Bobble (Daily)\nCurrent Market Price: $8.375"
    )
    assert p["post_kind"] == "entry"
    assert p["direction"] == "long"
    assert p["ticker"] == "ASAN"
    assert p["entry"] == 8.35
    assert p["stop"] == 7.84
    assert p["targets"] == [8.70, 9.00, 9.25, 9.50]
    assert p["trade_kind"] == "swing"
    assert p["asset_class"] == "equity"
    assert p["notes"].startswith("Ichinisan")


def test_short_entry_targets_printed_as_positive_percentages():
    """The desk prints a short's gains as +%, so only level ordering can
    tell direction. Targets below entry ⇒ short."""
    p = _post(
        "ENA | CRYPTO SWING TRADE\n\n"
        "ENA SHORT Entry @ $0.0920\n\n"
        "Target 1: $0.0892 (+3.04%)\nTarget 2: $0.0875 (+4.89%)\n\n"
        "Stop Loss: $0.0935 (-1.63%)"
    )
    assert p["direction"] == "short"
    assert p["asset_class"] == "crypto"
    assert p["stop"] > p["entry"]


def test_direction_inferred_when_side_word_absent():
    p = _post(
        "ZEC | CRYPTO DAY TRADE\n\nZEC Entry @ $511.50\n\n"
        "Target 1: $506\nTarget 2: $501\n\nStop Loss: $516"
    )
    assert p["direction"] == "short"
    assert p["trade_kind"] == "day"


def test_entry_written_without_dollar_sign_or_at():
    p = _post(
        "IPST | STOCK DAY TRADE\n\nIPST Entry 12.50\n\n"
        "Target 1: $13.30\n\nStop Loss: $11.90"
    )
    assert p["post_kind"] == "entry"
    assert p["entry"] == 12.50


def test_markdown_escaped_level_line_is_unescaped():
    p = _post(
        "ASAN | STOCK SWING TRADE\n\n"
        r"ASAN Target 1: $8.70 \(\+4.19%\)" + "\n\nCurrent Market Price: $8.7"
    )
    assert p["post_kind"] == "target_hit"


def test_bare_header_followups_are_still_lifecycle_posts():
    """No `— UPDATE` suffix, but these are follow-ups, not new calls."""
    assert _post("DUOT | STOCK DAY TRADE\n\nDUOT Stop for now.")["post_kind"] == "stop_hit"
    assert _post("CELH | STOCK DAY TRADE\n\nCELH 33.00 HIT!")["post_kind"] == "target_hit"
    assert _post(
        "KTOS | STOCK SWING TRADE\n\nKTOS closed some @ $57.20 Target 2"
    )["post_kind"] == "partial_exit"


def test_stop_move_is_not_a_stop_out():
    p = _post(
        "PENGU | CRYPTO DAY TRADE — UPDATE\n\n"
        "PENGU moving up my Stop Loss to Entry price @ $0.0099"
    )
    assert p["post_kind"] == "stop_move"


def test_misspelled_stop_out_still_classifies():
    p = _post(
        "ENA | CRYPTO SWING TRADE — UPDATE\n\nENA stoped out at @ $0.0935"
    )
    assert p["post_kind"] == "stop_hit"


def test_all_targets_hit_marks_the_whole_trade_done():
    p = _post("KTOS | STOCK SWING TRADE — UPDATE\n\nKTOS all Target s HIT!")
    assert p["post_kind"] == "target_hit"
    assert p["event"]["all_targets"] is True


def test_option_entry_keeps_premium_off_the_underlying():
    p = _post(
        "CRCL | OPTION SWING TRADE\n\nSymbol: CRCL\nType: CALL\nStrike: 95\n"
        "Expiration: 09/18/26\nEntry: $6.55\n\nCurrent Market Price: $90.9899"
    )
    assert p["post_kind"] == "option_entry"
    assert p["direction"] == "long"
    assert p["option"]["strike"] == 95
    assert p["option"]["premium"] == 6.55
    feats = to_features(p)
    # $6.55 is premium, not a level on CRCL — it must not become an entry.
    assert feats["setups"][0]["entry"] is None
    assert feats["call_type"] == "directional_long"


def test_put_is_a_short_on_the_underlying():
    p = _post(
        "AMD | OPTION SWING TRADE\n\nSymbol: AMD\nType: PUT\nStrike: 400\n"
        "Expiration: 09/16/26\nEntry: $5.65\n\nCurrent Market Price: $465.905"
    )
    assert p["direction"] == "short"
    assert to_features(p)["bias"] == "bearish"


def test_partial_option_exit_is_not_an_entry():
    p = _post(
        "CRCL | OPTION SWING TRADE — EXIT\n\nSymbol: CRCL\nType: CALL\nStrike: 80\n"
        "EXIT — 20% @ $8.90\n\nCurrent Market Price: $76.675"
    )
    assert p["post_kind"] == "option_exit"
    assert p["option"]["exit_size"] == "20%"
    assert p["option"]["exit_premium"] == 8.90
    assert to_features(p) is None


def test_declared_test_posts_are_excluded():
    assert _post("XYZ | STOCK SWING TRADE\n\nXYZ Test")["post_kind"] == "test"
    p = _post(
        "TSLA | STOCK SWING TRADE\n\nTSLA Entry @ $1\n\n"
        "Target 1: $2 (+100.00%)\n\nStop Loss: $0.50\n\n"
        "Notes: JUST A TEST\nCurrent Market Price: $325.38"
    )
    assert p["post_kind"] == "test"


def test_absurd_entry_versus_market_is_a_test_even_without_the_word():
    p = _post(
        "TSLA | STOCK SWING TRADE\n\nTSLA Entry @ $1\n\n"
        "Target 1: $2 (+100.00%)\nTarget 2: $3 (+200.00%)\n\n"
        "Current Market Price: $325.38"
    )
    assert p["post_kind"] == "test"
    assert to_features(p) is None


def test_the_word_test_about_price_action_is_not_a_test_post():
    """'that was a test to that 12.15 level' is real commentary."""
    p = _post(
        "DUOT | STOCK DAY TRADE\n\nDUOT\n\n"
        "Notes: Still in DUOT that was a test to that 12.15 level\n"
        "Current Market Price: $12.275"
    )
    assert p["post_kind"] == "commentary"


def test_non_matching_text_returns_none():
    assert parse_post("just a chat message") is None
    assert parse_post("") is None


def test_features_use_the_horizon_the_desk_declared():
    day = to_features(_post(
        "ACHR | STOCK DAY TRADE\n\nACHR Long Entry @ $6.65\n\n"
        "Target 1: $6.80\n\nStop Loss: $6.39"
    ))
    swing = to_features(_post(
        "TXT | STOCK SWING TRADE\n\nTXT Entry Long @ $89.00\n\n"
        "Target 1: $91.00\n\nStop Loss: $87.00"
    ))
    assert day["timeframe"] == "1H"      # scored over days, not weeks
    assert swing["timeframe"] == "1D"
