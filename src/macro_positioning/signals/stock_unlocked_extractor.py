"""Deterministic parser for the Stock Unlocked Trades Telegram channel.

Every other Telegram source in this project posts *charts*, so the ingest
path is chart-vision (`manual/vision.py` → `manual_chart_extractor`). Stock
Unlocked is the first text-native source: it relays nothing but actual
calls, in a fixed template the desk types by hand:

    ASAN | STOCK SWING TRADE

    ASAN Entry @ $8.35

    Target 1: $8.70 (+4.19%)
    ...
    Stop Loss: $7.84 (-6.11%)

    Notes: Ichinisan + Bobble (Daily)
    Current Market Price: $8.375

Because the ticker, side, entry, every target and the stop are stated in
words, a regex parse is *more* accurate than a vision model reading levels
off a chart — and it is free. No LLM is involved anywhere in this module.

Three things the template does not make easy, and how we handle them:

1. **The header's `— UPDATE` / `— EXIT` suffix is unreliable.** Plenty of
   real updates are posted with a bare header ("ASAN Target 1: $8.70",
   "DUOT Stop for now."). The post kind is therefore classified from the
   BODY; the header suffix is only a hint.

2. **Follow-ups are lifecycle events, not new calls.** A "Target 2 HIT"
   post is the same trade, later. Emitting a second Signal for it would
   double-count the ticker in conviction (see the multi-stage-trades and
   conviction-allowlist notes). Lifecycle posts return no Signal; they
   update the status of the open Signal they belong to.

3. **The desk posts test messages** ("XYZ Test", TSLA entry @ $1 with
   targets $2/$3/$4 while the market price is $325). Those are filtered:
   an entry that deviates absurdly from the quoted market price is junk,
   never a call.

Options posts carry a premium, a strike and an expiry but no stop or
target. They are emitted as a directional call on the UNDERLYING (CALL =
long, PUT = short) with the option leg preserved in `instrument_detail`;
the premium is NOT a price level on the underlying, so it never becomes
an entry_zone. Scoring the underlying is directionally honest — it is not
a claim about premium P&L, which this project has no model for.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from time import perf_counter
from typing import Any, Optional

from macro_positioning.core.settings import settings
from macro_positioning.signals.base import (
    ExtractionResult,
    Signal,
    SignalCatalystType,
    SignalHorizon,
    SignalSide,
    SignalStatus,
)

log = logging.getLogger(__name__)

SOURCE_ID = "manual:telegram-channel:stock_unlocked"

# ─── grammar ───────────────────────────────────────────────────────────

_HEADER_RE = re.compile(
    r"^(?P<ticker>[A-Z0-9][A-Z0-9._-]{0,9})\s*\|\s*"
    r"(?P<cls>STOCK|OPTION|CRYPTO)\s+(?P<kind>SWING|DAY)\s+TRADE"
    r"(?:\s*[—–-]+\s*(?P<status>UPDATE|EXIT))?\s*$",
    re.IGNORECASE,
)

# The desk's client escapes markdown: "Target 1: $8.70 \(\+4.19%\)".
_UNESCAPE_RE = re.compile(r"\\([()+\-.%$])")

_NUM = r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)"

_TARGET_RE = re.compile(rf"^Target\s*(\d)\s*:\s*{_NUM}", re.IGNORECASE | re.MULTILINE)
_STOP_RE = re.compile(rf"^Stop\s*Loss\s*:\s*{_NUM}", re.IGNORECASE | re.MULTILINE)
_MKT_RE = re.compile(rf"^Current\s+Market\s+Price\s*:\s*{_NUM}", re.IGNORECASE | re.MULTILINE)
_NOTES_RE = re.compile(r"^Notes?\s*:\s*(?:Notes?\s*:\s*)?(.+)$", re.IGNORECASE | re.MULTILINE)

# Option block
_OPT_SYMBOL_RE = re.compile(r"^Symbol\s*:\s*([A-Z0-9._-]+)", re.IGNORECASE | re.MULTILINE)
_OPT_TYPE_RE = re.compile(r"^Type\s*:\s*(CALL|PUT)", re.IGNORECASE | re.MULTILINE)
_OPT_STRIKE_RE = re.compile(rf"^Strike\s*:\s*{_NUM}", re.IGNORECASE | re.MULTILINE)
_OPT_EXPIRY_RE = re.compile(r"^Expiration\s*:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
                            re.IGNORECASE | re.MULTILINE)
_OPT_ENTRY_RE = re.compile(rf"^Entry\s*:\s*{_NUM}", re.IGNORECASE | re.MULTILINE)
_OPT_EXIT_RE = re.compile(
    rf"^EXIT\s*[—–-]+\s*(?P<size>ALL|[0-9]+%)(?:\s*@\s*{_NUM})?",
    re.IGNORECASE | re.MULTILINE,
)

# Action line (first non-empty body line): "TICKER [LONG|SHORT] Entry [@] $x"
_ENTRY_RE = re.compile(
    rf"\b(?:Entry|Entries)\b(?:\s+(?:long|short))?\s*@?\s*{_NUM}",
    re.IGNORECASE,
)
_SIDE_RE = re.compile(r"\b(LONG|SHORT)\b", re.IGNORECASE)

_STOP_HIT_RE = re.compile(
    r"\b(stopp?ed\s+out|stopp?ed\s+for\s+now|stopp?ed\b|stop\s+hit|stop\s+for\s+now)",
    re.IGNORECASE,
)
# A target report takes three forms: "Target 2 ... HIT", a bare restatement
# of the level ("ASAN Target 1: $8.70"), or just the price ("CELH 33.00 HIT!").
_TARGET_HIT_RE = re.compile(
    r"\bTarget\s*\d?\s*s?\b[^\n]*?\bHIT\b"
    r"|\ball\s+Target\s*s?\s*HIT\b"
    r"|\bTarget\s*\d\s*[:@]"
    r"|[0-9](?:[0-9.,]*)\s*HIT\b",
    re.IGNORECASE,
)
_HIT_PRICE_RE = re.compile(rf"HIT\s*(?:!|\s)*@?\s*{_NUM}", re.IGNORECASE)
_STOP_MOVE_RE = re.compile(
    r"\b(moving\s+(?:up|down)?\s*my\s+stop|lowering\s+my\s+sl|raising\s+my\s+sl"
    r"|moving\s+my\s+sl|stop\s+loss\s+to\s+entry)",
    re.IGNORECASE,
)
_PARTIAL_RE = re.compile(
    r"\b(taking\s+some|took\s+some|closed\s+some|taking\s+profit|all\s+out|trimm?ing)",
    re.IGNORECASE,
)
# "JUST A TEST" is a test post; "that was a test to that 12.15 level" is a
# real trade note about price probing support. Only match declarations.
_TEST_RE = re.compile(
    r"^\s*(?:just\s+a\s+)?test\b|\btest\s+(?:post|message)\b",
    re.IGNORECASE,
)

# An entry this far from the quoted market price is a test post, not a call.
_MAX_ENTRY_DEVIATION = 0.40


def _f(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_post(text: Optional[str]) -> Optional[dict]:
    """Parse one channel post. Returns None if it isn't this channel's format.

    The returned dict always carries `post_kind`; only `entry` and
    `option_entry` are actionable calls.
    """
    if not text:
        return None
    body = _UNESCAPE_RE.sub(r"\1", text.strip())
    lines = [ln.strip() for ln in body.splitlines()]
    if not lines:
        return None

    header = _HEADER_RE.match(lines[0])
    if not header:
        return _parse_v1(body, lines)

    cls = header.group("cls").upper()
    out: dict[str, Any] = {
        "ticker": header.group("ticker").upper(),
        "instrument": cls.lower(),                       # stock | option | crypto
        "asset_class": {"STOCK": "equity", "CRYPTO": "crypto",
                        "OPTION": "option"}[cls],
        "trade_kind": header.group("kind").lower(),      # swing | day
        "header_status": (header.group("status") or "").lower() or None,
        "direction": None,
        "entry": None, "targets": [], "stop": None,
        "trigger": None,
        "market_price": _f(m.group(1)) if (m := _MKT_RE.search(body)) else None,
        "notes": (n.group(1).strip() if (n := _NOTES_RE.search(body)) else None),
        "author": None,
        "format": "v2",
        "option": None,
        "event": None,
        "post_kind": "commentary",
    }

    # Action line = first non-empty line after the header.
    action = next((ln for ln in lines[1:] if ln), "")
    out["action_line"] = action

    # ── levels ──
    for idx, val in _TARGET_RE.findall(body):
        v = _f(val)
        if v is not None:
            out["targets"].append(v)
    if s := _STOP_RE.search(body):
        out["stop"] = _f(s.group(1))

    # ── options block ──
    opt_type = _OPT_TYPE_RE.search(body)
    if opt_type:
        out["option"] = {
            "symbol": (m.group(1).upper() if (m := _OPT_SYMBOL_RE.search(body)) else out["ticker"]),
            "type": opt_type.group(1).upper(),
            "strike": _f(m.group(1)) if (m := _OPT_STRIKE_RE.search(body)) else None,
            "expiration": (m.group(1) if (m := _OPT_EXPIRY_RE.search(body)) else None),
            "premium": None, "exit_size": None, "exit_premium": None,
        }
        out["direction"] = "long" if opt_type.group(1).upper() == "CALL" else "short"
        if x := _OPT_EXIT_RE.search(body):
            out["option"]["exit_size"] = x.group("size").upper()
            out["option"]["exit_premium"] = _f(x.group(2))
            out["post_kind"] = "option_exit"
        elif e := _OPT_ENTRY_RE.search(body):
            out["option"]["premium"] = _f(e.group(1))
            out["post_kind"] = "option_entry"
        else:
            out["post_kind"] = "commentary"
        return _flag_tests(out, body)

    # ── stock / crypto ──
    entry_m = _ENTRY_RE.search(action) or _ENTRY_RE.search(body)
    side_m = _SIDE_RE.search(action)
    if side_m:
        out["direction"] = side_m.group(1).lower()

    if entry_m and ("entry" in action.lower() or not out["targets"]):
        out["entry"] = _f(entry_m.group(1))
    if entry_m and "entry" in action.lower():
        out["post_kind"] = "entry"
    elif _STOP_MOVE_RE.search(body):
        out["post_kind"] = "stop_move"
    elif _STOP_HIT_RE.search(action) or _STOP_HIT_RE.search(out["notes"] or ""):
        out["post_kind"] = "stop_hit"
    elif _TARGET_HIT_RE.search(action):
        out["post_kind"] = "target_hit"
    elif _PARTIAL_RE.search(action):
        out["post_kind"] = "partial_exit"
    elif out["targets"] and out["stop"] and out["entry"] is None:
        # Levels restated without the word "Entry" — still a call.
        out["post_kind"] = "entry"

    # Lifecycle detail: which target, at what price.
    if out["post_kind"] in ("target_hit", "partial_exit", "stop_hit", "stop_move"):
        tgt_idx = None
        if t := re.search(r"\bTarget\s*(\d)", action, re.IGNORECASE):
            tgt_idx = int(t.group(1))
        px = None
        if h := _HIT_PRICE_RE.search(action):
            px = _f(h.group(1))
        elif at := re.search(rf"@\s*{_NUM}", action):
            px = _f(at.group(1))
        out["event"] = {
            "target_index": tgt_idx,
            "reported_price": px,
            "all_targets": bool(re.search(r"\ball\s+Target", action, re.IGNORECASE)),
            "text": action,
        }

    # Direction from geometry when unstated: targets below entry ⇒ short.
    # (The desk prints target moves as positive percentages even on shorts,
    # so the printed sign cannot be trusted — the level ordering can.)
    if out["direction"] is None and out["entry"] and out["targets"]:
        out["direction"] = "short" if out["targets"][0] < out["entry"] else "long"
    if out["direction"] is None and out["post_kind"] == "entry":
        out["direction"] = "long"

    return _flag_tests(out, body)


# ─── the 2025 format ───────────────────────────────────────────────────
# Before March 2026 the desk posted through an alert bot with a different
# template — an emoji prefix, `Ticker:` restated on its own line, targets
# as `Pt1: 4.29 (+2.88%)` or `PT 1: $224`, `Risk Spread`, and `Alert By:
# <alerter>` at the foot. Two things that format has and the current one
# does not: a BREAKOUT TRIGGER ("OPEN Above 4.17" — the entry is a level
# price has not reached yet, and the stop sits ABOVE the market until it
# does) and several alerters posting under one channel. Both are carried
# through: `trigger` tells the tracker to arm the call at the entry rather
# than at the post, and `author` lets the record be split by alerter.
#
# Old-format options posts ("SPY 633 calls August 6 @ 1.40") carry no
# structure worth a parse and return None.

_V1_HEADER_RE = re.compile(
    r"^[^A-Za-z0-9]*(?P<ticker>[A-Z0-9][A-Z0-9._-]{0,11})\s*\|\s*(?P<kind>[A-Z ]+?)\s*$",
    re.IGNORECASE,
)
_V1_TARGET_RE = re.compile(rf"^P[Tt]\s*(\d)\s*:?\s*{_NUM}", re.IGNORECASE | re.MULTILINE)
_V1_STOP_RE = re.compile(rf"^(?:Stop\s*Loss|SL)\s*:?\s*{_NUM}", re.IGNORECASE | re.MULTILINE)
_V1_ABOVE_RE = re.compile(rf"\b(?:fills?\s+)?(?P<dir>Above|Below)\s*{_NUM}", re.IGNORECASE)
_V1_ENTRY_RE = re.compile(
    rf"\b(?:entry|in|fill(?:ed)?|filled\s+at|filled)\b\s*(?:@|at)?\s*{_NUM}"
    rf"|^[A-Z0-9._-]+\s*[–-]?\s*(?:FILLED\s*)?@\s*{_NUM}",
    re.IGNORECASE,
)
_V1_ENTRY_RANGE_RE = re.compile(rf"\bEntry\s+{_NUM}\s*[-–]\s*{_NUM}", re.IGNORECASE)
_V1_ALERT_BY_RE = re.compile(r"Alert\s+By\s*:\s*(\S+)", re.IGNORECASE)
_V1_NOTE_RE = re.compile(r"^Notes?\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
# "PT 4 HIT @ $0.238", "Pt1: 4.29 (+2.88%) Hit!", "TAKING PT 2 NOW", "All pts
# hit!", "All pts met", and the bare restatement "UAMY Pt1: 3.75 (+5.63%)".
_V1_HIT_RE = re.compile(
    r"\bP[Tt]\s*(\d)\b[^\n]*?\b(?:hit|met)\b|\bTAKING\s+PT\s*(\d)\b|\ball\s+pts?\s+(?:hit|met)\b"
    r"|^\S+\s+P[Tt]\s*(\d)\s*:",
    re.IGNORECASE,
)
_V1_STOP_HIT_RE = re.compile(
    r"\b(stopp?ed(?:\s+out)?|stop\s+hit|SL\s+hit|stop\s+loss\s*(?::\s*\S+\s*)?hit)\b",
    re.IGNORECASE,
)
_V1_FLAT_RE = re.compile(r"\ball\s+out\s+at\s+entry\b|\bout\s+at\s+(?:entry|breakeven|b/?e)\b", re.IGNORECASE)
_V1_VOID_RE = re.compile(r"^\S+\s+(?:void|ignore|testing|test)\b", re.IGNORECASE)
_V1_STOP_MOVE_RE = re.compile(r"\b(?:moving|move|raise|raising)\b[^\n]*\b(?:SL|stop)\b|\bSL\s+to\s+entry\b",
                              re.IGNORECASE)


def _parse_v1(body: str, lines: list[str]) -> Optional[dict]:
    header = _V1_HEADER_RE.match(lines[0])
    if not header:
        return None
    kind_words = header.group("kind").upper()
    if "OPTION" in kind_words:
        return None
    known = ("SWING", "DAY", "CRYPTO", "STOCK")
    if not any(k in kind_words for k in known):
        return None
    raw_ticker = header.group("ticker").upper()
    ticker = raw_ticker
    for suffix in ("USDT", "USDC", "USD"):
        if ticker.endswith(suffix) and len(ticker) > len(suffix) + 1:
            ticker = ticker[: -len(suffix)]
            break
    is_crypto = "CRYPTO" in kind_words or ticker != raw_ticker
    out: dict[str, Any] = {
        "ticker": ticker,
        "instrument": "crypto" if is_crypto else "stock",
        "asset_class": "crypto" if is_crypto else "equity",
        "trade_kind": "day" if "DAY" in kind_words else "swing",
        "header_status": None,
        "direction": None,
        "entry": None, "targets": [], "stop": None,
        "trigger": None,
        # The bot's quoted price is unreliable in this format ("NO DATA",
        # 3.4e-10 for PENGU) — never used to judge a call.
        "market_price": None,
        "notes": (n.group(1).strip() if (n := _V1_NOTE_RE.search(body)) else None),
        "author": (a.group(1).strip().lower() if (a := _V1_ALERT_BY_RE.search(body)) else None),
        "format": "v1",
        "option": None,
        "event": None,
        "post_kind": "commentary",
    }
    rest = [ln for ln in lines[1:] if ln and not ln.lower().startswith("ticker:")]
    action = rest[0] if rest else ""
    out["action_line"] = action

    for _idx, val in _V1_TARGET_RE.findall(body):
        v = _f(val)
        if v is not None:
            out["targets"].append(v)
    if st := _V1_STOP_RE.search(body):
        out["stop"] = _f(st.group(1))

    if re.search(r"\bshort\b", action, re.IGNORECASE):
        out["direction"] = "short"

    if _V1_VOID_RE.match(action):
        out["post_kind"] = "void"
    elif _V1_FLAT_RE.search(action):
        out["post_kind"] = "breakeven_exit"
    elif _V1_STOP_HIT_RE.search(action):
        out["post_kind"] = "stop_hit"
    elif h := _V1_HIT_RE.search(action):
        out["post_kind"] = "target_hit"
        idx = h.group(1) or h.group(2) or h.group(3)
        out["event"] = {
            "target_index": int(idx) if idx else None,
            "reported_price": _f(m.group(1)) if (m := re.search(rf"@\s*{_NUM}", action)) else None,
            "all_targets": bool(re.search(r"\ball\s+pts?\s+(?:hit|met)", action, re.IGNORECASE)),
            "text": action,
        }
    elif _V1_STOP_MOVE_RE.search(action):
        out["post_kind"] = "stop_move"
    elif out["targets"] and out["stop"] is not None:
        if ab := _V1_ABOVE_RE.search(action):
            out["entry"] = _f(ab.group(2))
            out["trigger"] = ab.group("dir").lower()
            if out["direction"] is None:
                out["direction"] = "short" if out["trigger"] == "below" else "long"
        elif rg := _V1_ENTRY_RANGE_RE.search(action):
            out["entry"] = _f(rg.group(1))
        elif en := _V1_ENTRY_RE.search(action):
            out["entry"] = _f(en.group(1) or en.group(2))
        if out["entry"] is not None:
            out["post_kind"] = "entry"
    if out["post_kind"] in ("stop_hit", "breakeven_exit", "stop_move") and out["event"] is None:
        out["event"] = {
            "target_index": None,
            "reported_price": _f(m.group(1)) if (m := re.search(rf"@\s*{_NUM}", action)) else None,
            "all_targets": False,
            "text": action,
        }
    if out["direction"] is None and out["entry"] and out["targets"]:
        out["direction"] = "short" if out["targets"][0] < out["entry"] else "long"
    if out["direction"] is None and out["post_kind"] == "entry":
        out["direction"] = "long"
    return out


def _flag_tests(out: dict, body: str) -> dict:
    """Demote obvious test posts so they never reach the scorer."""
    action = out.get("action_line") or ""
    # "XYZ Test" — a ticker and nothing but the word test.
    bare_test = re.fullmatch(r"[A-Z0-9._-]{1,10}\s+test\.?", action, re.IGNORECASE)
    if bare_test or _TEST_RE.search(out.get("notes") or "") or _TEST_RE.search(action):
        out["post_kind"] = "test"
        return out
    entry = out.get("entry")
    mkt = out.get("market_price")
    if entry and mkt and mkt > 0:
        if abs(entry - mkt) / mkt > _MAX_ENTRY_DEVIATION:
            out["post_kind"] = "test"
    return out


# ─── projection into the vision-compatible feature shape ───────────────
# `learning/call_accuracy.py` scores calls out of
# `documents.extracted_features_json`. Writing this channel's parse into
# that same shape means Stock Unlocked is backtested by the *existing*
# accuracy layer (setup_win_rate, winsorized R, BTC-relative alpha)
# alongside every chart source — no parallel scoring path.

_TIMEFRAME = {"day": "1H", "swing": "1D"}


def to_features(parsed: dict) -> Optional[dict]:
    """Canonical extracted_features_json payload for an actionable call."""
    if parsed["post_kind"] not in ("entry", "option_entry"):
        return None
    direction = parsed.get("direction") or "long"
    setup: dict[str, Any] = {
        "entry": parsed.get("entry"),
        "stop_loss": parsed.get("stop"),
        "take_profits": list(parsed.get("targets") or []),
        "direction": direction,
        "status": "active",
    }
    if parsed["post_kind"] == "option_entry":
        # Premium is not a level on the underlying; leave entry unset so the
        # backtester fills at the post-time close instead of at $6.55.
        setup["entry"] = None
        setup["option"] = parsed["option"]
    return {
        "ticker": parsed["ticker"],
        "asset_class": parsed["asset_class"],
        "call_type": f"directional_{direction}",
        "direction": direction,
        "bias": "bullish" if direction == "long" else "bearish",
        "timeframe": _TIMEFRAME.get(parsed["trade_kind"], "1D"),
        "trade_stage": "active",
        "setups": [setup],
        "notes": parsed.get("notes"),
        "confluence_score": 4.0 if (setup["stop_loss"] and setup["take_profits"]) else 3.0,
        "vision_backend": "stock_unlocked_parser",
        "vision_model": "regex_v1",
    }


# ─── the extractor ─────────────────────────────────────────────────────

_HORIZON = {"day": SignalHorizon.INTRADAY, "swing": SignalHorizon.SWING}


class StockUnlockedExtractor:
    """Text-native trade calls → Signal rows. Deterministic, zero cost."""

    name = "stock_unlocked_extractor"
    version = "v1"

    def applies_to(self, document: dict) -> bool:
        return (document.get("source_id") or "") == SOURCE_ID

    def extract(self, document: dict, *, run_id: Optional[str] = None) -> ExtractionResult:
        t0 = perf_counter()
        doc_id = document["document_id"]
        text = document.get("raw_text") or document.get("cleaned_text") or ""
        parsed = parse_post(text)

        def _done(status: str, *, signals=None, err=None) -> ExtractionResult:
            return ExtractionResult(
                document_id=doc_id,
                extractor_name=self.name,
                extractor_version=self.version,
                status=status,
                signals=signals or [],
                error_message=err,
                latency_ms=(perf_counter() - t0) * 1000,
            )

        if parsed is None:
            return _done("no_signal", err="does not match the Stock Unlocked template")

        # Persist the structured parse on the document so the accuracy layer
        # (which reads extracted_features_json) picks these calls up.
        features = to_features(parsed)
        _write_features(doc_id, features, parsed)

        if parsed["post_kind"] == "test":
            return _done("no_signal", err="test post — excluded from scoring")
        if features is None:
            # Lifecycle post: no new call, but it moves the open one along.
            applied = _apply_lifecycle(doc_id, parsed)
            return _done("no_signal", err=f"{parsed['post_kind']} | lifecycle_applied={applied}")

        targets = parsed.get("targets") or []
        is_option = parsed["post_kind"] == "option_entry"
        side = SignalSide.LONG if parsed["direction"] == "long" else SignalSide.SHORT
        conviction = 4.0 if (parsed.get("stop") and targets) else 3.0

        signal = Signal(
            document_id=doc_id,
            # Stamp the call's own timestamp, NOT ingest time: a backfill of a
            # month of history must not read as a same-day burst of activity
            # in the momentum / conviction windows.
            extracted_at=document.get("published_at") or None,
            extraction_run_id=run_id,
            asset_ticker=parsed["ticker"],
            asset_class=parsed["asset_class"],
            side=side,
            conviction=conviction,
            conviction_raw=f"{parsed['instrument']}_{parsed['trade_kind']}_trade",
            horizon=_HORIZON.get(parsed["trade_kind"]),
            entry_zone_low=parsed.get("entry"),
            entry_zone_high=parsed.get("entry"),
            stop_loss=parsed.get("stop"),
            target_1=targets[0] if targets else None,
            target_2=targets[1] if len(targets) > 1 else None,
            thesis_summary=parsed.get("notes"),
            thesis_tags=[f"{parsed['instrument']}_{parsed['trade_kind']}"],
            catalyst_type=SignalCatalystType.TECHNICAL,
            extractor_name=self.name,
            extractor_version=self.version,
            extractor_confidence=0.95,          # stated in words, not read off a chart
            model_provider="stock_unlocked_parser",
            model_name="regex_v1",
            raw_excerpt=text[:600],
            instrument_detail={
                "trade_kind": parsed["trade_kind"],
                "instrument": parsed["instrument"],
                "targets": targets,
                "market_price_at_post": parsed.get("market_price"),
                "option": parsed.get("option"),
                "post_kind": parsed["post_kind"],
            },
            status=SignalStatus.ACTIVE,
            source_slug="manual",
            source_channel="telegram-channel:stock_unlocked",
            author_id=document.get("author_id"),
        )
        if signal.extracted_at is None:
            signal.extracted_at = document.get("ingested_at")
        return _done("success", signals=[signal])


# ─── document + lifecycle writes ───────────────────────────────────────

def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or settings.sqlite_path))
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _write_features(doc_id: str, features: Optional[dict], parsed: dict,
                    *, db_path: Optional[Path] = None) -> None:
    """Store the parse on the document row. Idempotent."""
    payload = features or {
        "ticker": parsed["ticker"],
        "call_type": "lifecycle" if parsed["post_kind"] != "test" else "test",
        "post_kind": parsed["post_kind"],
        "event": parsed.get("event"),
        "notes": parsed.get("notes"),
        "vision_backend": "stock_unlocked_parser",
    }
    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                "UPDATE documents SET extracted_features_json=? WHERE document_id=?",
                (json.dumps(payload), doc_id),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        log.exception("failed writing extracted_features_json for %s", doc_id)


def _apply_lifecycle(doc_id: str, parsed: dict, *, db_path: Optional[Path] = None) -> bool:
    """Move the open Signal for this ticker to its new lifecycle state.

    A "Target 3 HIT" or "stopped out" post is the same trade, later — so it
    updates the standing call rather than minting a second one. Only the most
    recent ACTIVE signal for the ticker from this channel is touched.
    """
    kind = parsed["post_kind"]
    if kind not in ("stop_hit", "target_hit", "partial_exit"):
        return False
    event = parsed.get("event") or {}
    all_targets = bool(event.get("all_targets"))
    if kind == "stop_hit":
        new_status = SignalStatus.INVALIDATED.value
    elif kind == "target_hit" and all_targets:
        new_status = SignalStatus.EXPIRED.value      # played out in full
    else:
        return False                                  # partial progress: still active
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                """
                SELECT signal_id FROM signals
                 WHERE asset_ticker=? AND source_channel='telegram-channel:stock_unlocked'
                   AND status=? ORDER BY extracted_at DESC LIMIT 1
                """,
                (parsed["ticker"], SignalStatus.ACTIVE.value),
            ).fetchone()
            if not row:
                return False
            conn.execute("UPDATE signals SET status=? WHERE signal_id=?", (new_status, row[0]))
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        log.exception("lifecycle update failed for %s", doc_id)
        return False


def reconcile_lifecycle(*, db_path: Optional[Path] = None) -> dict:
    """Replay every lifecycle post in publication order over the signals.

    `extract_pending()` walks documents in INGEST order, which for a
    backfill bears no relation to the order the desk posted them — a
    "stopped out" can be processed before the entry it closes, and then
    silently applies to nothing. Replaying chronologically makes the final
    state independent of ingest order, and is idempotent: every call
    recomputes each signal's status from scratch.

    A call is EXPIRED when its last stated target prints, INVALIDATED when
    the desk says it stopped, and otherwise stays ACTIVE — a partial
    ("Target 2 HIT" of four) is progress, not completion.
    """
    conn = _connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        docs = conn.execute(
            """
            SELECT document_id, published_at, raw_text FROM documents
             WHERE source_id=? ORDER BY published_at ASC
            """,
            (SOURCE_ID,),
        ).fetchall()
        sigs = conn.execute(
            """
            SELECT signal_id, asset_ticker, extracted_at, instrument_detail_json
              FROM signals WHERE source_channel='telegram-channel:stock_unlocked'
             ORDER BY extracted_at ASC
            """
        ).fetchall()

        by_ticker: dict[str, list[dict]] = {}
        final: dict[str, str] = {}
        for s in sigs:
            detail = {}
            try:
                detail = json.loads(s["instrument_detail_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            rec = {
                "signal_id": s["signal_id"],
                "at": s["extracted_at"] or "",
                "n_targets": len(detail.get("targets") or []),
            }
            by_ticker.setdefault(s["asset_ticker"], []).append(rec)
            final[s["signal_id"]] = SignalStatus.ACTIVE.value

        applied = 0
        for d in docs:
            parsed = parse_post(d["raw_text"])
            if not parsed or parsed["post_kind"] not in ("stop_hit", "target_hit"):
                continue
            when = d["published_at"] or ""
            candidates = [r for r in by_ticker.get(parsed["ticker"], []) if r["at"] <= when]
            if not candidates:
                continue
            open_sig = candidates[-1]
            event = parsed.get("event") or {}
            if parsed["post_kind"] == "stop_hit":
                final[open_sig["signal_id"]] = SignalStatus.INVALIDATED.value
                applied += 1
            else:
                idx = event.get("target_index")
                done = bool(event.get("all_targets")) or (
                    idx is not None and open_sig["n_targets"] and idx >= open_sig["n_targets"]
                )
                if done:
                    final[open_sig["signal_id"]] = SignalStatus.EXPIRED.value
                    applied += 1

        counts: dict[str, int] = {}
        for sig_id, status in final.items():
            conn.execute("UPDATE signals SET status=? WHERE signal_id=?", (status, sig_id))
            counts[status] = counts.get(status, 0) + 1
        conn.commit()
        return {"signals": len(final), "events_applied": applied, "by_status": counts}
    finally:
        conn.close()
