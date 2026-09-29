"""The Stock Unlocked book's candidate source — the tracker ledger.

Third paper book, same engine. The signal book trades the desk's blend;
the cohort book trades chart-vision extractions of the Feather Hands
crowd; this one trades the Stock Unlocked Trades channel, whose calls
state entry, targets and stop in words. The levels come straight from
`stock_unlocked_calls` (see `tracker/stock_unlocked.py`), so the book is
copying the desk's numbers, not a model's reading of a chart.

What the ledger gives this book that the cohort book has to do without:

  • **The desk's own lifecycle.** "Stopped out" and "all targets HIT"
    are posted as they happen. A call the desk has closed is ranked at 0
    here, which walks the engine's rank-decay exit out of the position
    inside a couple of ticks — the book follows the desk out as well as
    in. The tape's verdict does the same: a call whose stop has already
    printed is not a candidate, whatever the desk has said yet.
  • **A measured edge on the call's own slice.** The tracker's
    expectancy by (stock|crypto) × (day|swing) tilts the rank once the
    slice has ten resolved calls. Self-referential on purpose: the book
    exists to find out whether the tracker's numbers are worth money.

What it deliberately does NOT do: consult the desk's structure map,
composed target or regime. Those are the signal book's job.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

from macro_positioning.core.settings import settings
from macro_positioning.paper.cohort import Coverage
from macro_positioning.paper.models import Mandate
from macro_positioning.paper.rank import Component, RankRead, target_weight_for
from macro_positioning.paper.vocabulary import Band
from macro_positioning.prices.symbol_map import book_tradeable, resolve_symbol
from macro_positioning.tracker import stock_unlocked as tracker


logger = logging.getLogger(__name__)

CONFIG_PATH = "config/paper_stock_unlocked.json"

# The book's name in `paper_portfolios` — how it is told apart from the
# signal book and the cohort book when no portfolio_id is given.
BOOK_NAME = "Stock Unlocked Book"


@dataclass(frozen=True)
class SourceConfig:
    label: str = "Stock Unlocked"
    lookback_days: int = 5
    min_live_rr: float = 1.0
    max_candidates: int = 30
    target: str = "last"                  # last | first
    rank_model: dict = field(default_factory=dict)


@lru_cache(maxsize=2)
def load_source_config(path: str | Path | None = None) -> SourceConfig:
    p = Path(path) if path is not None else settings.base_dir / CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        raw = json.load(f)
    s = raw.get("source") or {}
    d = SourceConfig()
    return SourceConfig(
        label=str(s.get("label", d.label)),
        lookback_days=int(s.get("lookback_days", d.lookback_days)),
        min_live_rr=float(s.get("min_live_rr", d.min_live_rr)),
        max_candidates=int(s.get("max_candidates", d.max_candidates)),
        target=str(s.get("target", d.target)),
        rank_model=dict(raw.get("rank_model") or {}),
    )


def load_book_mandate(path: str | Path | None = None) -> Mandate:
    from macro_positioning.paper.models import load_mandate

    return load_mandate(Path(path) if path is not None else settings.base_dir / CONFIG_PATH)


def reset_config_cache() -> None:
    load_source_config.cache_clear()


# Extra skip reasons this source has that the cohort funnel does not.
_REASONS = {
    **Coverage.REASONS,
    "option": "an options call — premium only, no stop or target on the underlying",
    "tape_resolved": "the tape already printed the stop or the final target",
    "desk_closed": "the desk posted stopped-out / all targets hit",
    "timed_out": "past its horizon with neither level touched",
}


def _rr(entry: float, stop: float, target: float) -> Optional[float]:
    risk = abs(entry - stop)
    return abs(target - entry) / risk if risk > 0 else None


def _slice_edges(calls: list[tracker.TrackedCall]) -> dict[str, dict]:
    """avg realised R per (instrument, trade_kind) slice, from the ledger."""
    out: dict[str, dict] = {}
    groups: dict[str, list[float]] = {}
    for c in calls:
        if c.verdict in tracker.RESOLVED and c.realized_r is not None:
            groups.setdefault(f"{c.instrument}/{c.trade_kind}", []).append(c.realized_r)
    for k, rs in groups.items():
        out[k] = {"n": len(rs), "avg_r": sum(rs) / len(rs)}
    return out


def book_candidates(
    mandate: Optional[Mandate] = None,
    *,
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    config: Optional[SourceConfig] = None,
    calls: Optional[list[tracker.TrackedCall]] = None,
    mark_fn: Optional[Callable[[str], Optional[float]]] = None,
) -> tuple[list[RankRead], Coverage]:
    """Every channel call the book could act on now, strongest first.

    Also emits a rank-0 read for every call the desk or the tape has
    closed inside the lookback, so a held position follows the desk out.
    `mark_fn` defaults to a live spot quote — the working set is a
    handful of names, and the screen's mark should be the fill's mark.
    """
    cfg = config or load_source_config()
    m = mandate or load_book_mandate()
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=cfg.lookback_days)

    all_calls = calls if calls is not None else tracker.load_calls(db_path=db_path)
    edges = _slice_edges(all_calls)
    window = [c for c in all_calls if c.posted_at >= cutoff]

    cov = Coverage(window_days=cfg.lookback_days, generated_at=now.isoformat(),
                   label=cfg.label)
    cov.REASONS = _REASONS                                   # type: ignore[misc]
    cov.considered = len(window)
    skipped: Counter = Counter()
    unpriced: Counter = Counter()

    if mark_fn is None:
        from macro_positioning.prices.spot import spot_price

        def mark_fn(sym: str) -> Optional[float]:            # noqa: F811
            q = spot_price(sym)
            return float(q["price"]) if q and q.get("price") else None

    closed: list[tracker.TrackedCall] = []
    kept: list[dict] = []
    for c in sorted(window, key=lambda x: x.posted_at, reverse=True):
        if c.instrument == "option":
            skipped["option"] += 1
            continue
        if not c.is_levelled:
            skipped["no_levels"] += 1
            continue
        raw_ticker = f"{c.ticker}/USD" if c.instrument == "crypto" else c.ticker
        symbol = resolve_symbol(raw_ticker)
        if not symbol:
            skipped["unpriceable"] += 1
            unpriced[c.ticker] += 1
            continue
        # Priceable is not the same as in-mandate. Coinbase lists four
        # hundred coins; this book holds the majors and listed equities,
        # and the alt tail (FARTCOIN, PENGU, TRUMP, 1INCH …) is a book
        # the desk does not run. Counted, not silently dropped — the
        # channel calls these and the coverage line should say so.
        if not book_tradeable(symbol):
            skipped["crypto_not_major"] += 1
            continue
        # Closed by the desk or by the tape — never a fresh entry, but a
        # held position needs to hear about it.
        if c.desk_verdict == "loss" or (
            c.desk_verdict == "win" and c.desk_max_target >= len(c.targets)
        ):
            skipped["desk_closed"] += 1
            closed.append(c)
            continue
        if c.verdict in tracker.RESOLVED and (
            c.verdict != "win" or c.max_target >= len(c.targets)
        ):
            skipped["tape_resolved"] += 1
            closed.append(c)
            continue
        if c.verdict == "unresolved":
            skipped["timed_out"] += 1
            closed.append(c)
            continue

        side = "SHORT" if c.direction == "short" else "LONG"
        target = c.targets[-1] if cfg.target == "last" else c.targets[0]
        long_ok = c.stop < c.entry < target
        short_ok = c.stop > c.entry > target
        if not (long_ok if side == "LONG" else short_ok):
            skipped["levels_incoherent"] += 1
            continue

        mark = mark_fn(symbol)
        if mark is None or mark <= 0:
            skipped["no_price_history"] += 1
            continue
        if (mark <= c.stop) if side == "LONG" else (mark >= c.stop):
            skipped["stop_breached"] += 1
            continue
        # A breakout alert ("OPEN Above 4.17") is a plan until price clears
        # the level; buying it underneath is buying a trade the desk has
        # not taken. The tracker's `max_target` says whether it ever armed.
        if c.trigger and ((mark < c.entry) if side == "LONG" else (mark > c.entry)) \
                and c.verdict == "open" and not c.max_target and not c.desk_max_target:
            skipped["not_triggered"] += 1
            continue
        rr_live = _rr(mark, c.stop, target)
        if rr_live is None or rr_live < cfg.min_live_rr:
            skipped["rr_collapsed"] += 1
            continue

        kept.append({
            "call": c, "symbol": symbol, "side": side, "target": target,
            "mark": mark, "rr": _rr(c.entry, c.stop, target), "rr_live": rr_live,
            "age": (now - c.posted_at).total_seconds() / 86400.0,
        })

    # One read per symbol: the freshest call wins, older ones are updates
    # the desk has moved past.
    reads: list[RankRead] = []
    seen: set[str] = set()
    by_kind: Counter = Counter()
    for k in kept:
        if k["symbol"] in seen:
            skipped["superseded"] += 1
            continue
        seen.add(k["symbol"])
        reads.append(_read_for(k, cfg, m, edges))
        by_kind[f"{k['call'].instrument} {k['call'].trade_kind}"] += 1

    reads.sort(key=lambda r: (r.raw, r.value), reverse=True)
    if len(reads) > cfg.max_candidates:
        skipped["over_limit"] += len(reads) - cfg.max_candidates
        reads = reads[: cfg.max_candidates]

    # Rank-0 reads for closed calls, so the engine's rank-decay exit walks
    # the book out of a name the desk is out of. Only where no live call
    # on the same symbol is standing.
    for c in closed:
        raw_ticker = f"{c.ticker}/USD" if c.instrument == "crypto" else c.ticker
        symbol = resolve_symbol(raw_ticker)
        if not symbol or symbol in seen or not book_tradeable(symbol):
            continue
        seen.add(symbol)
        reads.append(_closed_read(c, symbol, m))

    cov.candidates = len(reads) - len([r for r in reads if r.value == 0.0])
    cov.skipped = dict(skipped)
    cov.unpriceable_tickers = unpriced.most_common(20)
    cov.by_author = dict(by_kind)
    return reads, cov


def _read_for(k: dict, cfg: SourceConfig, mandate: Mandate, edges: dict[str, dict]) -> RankRead:
    mdl = cfg.rank_model
    c: tracker.TrackedCall = k["call"]
    side = k["side"]
    comps = [Component("base", float(mdl.get("base", 55.0)),
                       f"a {cfg.label} call with stated entry, targets and stop")]

    rr = k["rr"]
    if rr is not None:
        if rr >= 3.0:
            comps.append(Component("rr", float(mdl.get("rr_bonus_3r", 10.0)),
                                   f"{rr:.1f}R to the last target as posted"))
        elif rr >= 2.0:
            comps.append(Component("rr", float(mdl.get("rr_bonus_2r", 5.0)),
                                   f"{rr:.1f}R to the last target as posted"))
        elif rr < float(mdl.get("rr_thin_below", 1.0)):
            comps.append(Component("rr", float(mdl.get("rr_penalty_thin", -10.0)),
                                   f"only {rr:.1f}R to the last target — thin"))

    if c.desk_max_target:
        d = min(float(mdl.get("progress_max", 8.0)),
                c.desk_max_target * float(mdl.get("progress_bonus_per_target", 4.0)))
        comps.append(Component("progress", d,
                               f"the desk has reported T{c.desk_max_target} of {len(c.targets)} hit"))

    grace = float(mdl.get("stale_after_days", 1.0))
    if k["age"] > grace:
        d = max(float(mdl.get("stale_floor", -16.0)),
                (k["age"] - grace) * float(mdl.get("stale_per_day", -4.0)))
        comps.append(Component("freshness", d, f"the call is {k['age']:.1f} days old"))

    if c.trade_kind == "day":
        comps.append(Component("day_trade", float(mdl.get("day_trade_penalty", -6.0)),
                               "a day trade against a two-hour tick"))

    comps.append(_edge_component(c, edges, mdl))

    raw = sum(x.delta for x in comps)
    value = max(0.0, min(100.0, raw))
    read = RankRead(
        ticker=k["symbol"], side=side, value=value,
        band=Band.for_rank(value, entry_floor=mandate.entry_floor),
        target_weight_pct=target_weight_for(value, mandate),
        components=comps, tradeable=True, score=None, grade=None,
        scored_at=c.posted_at.isoformat(), anchored=False, raw=raw,
    )
    read.source_row = _source_row(c, k["symbol"], k["target"], rr, k)   # type: ignore[attr-defined]
    return read


def _closed_read(c: tracker.TrackedCall, symbol: str, mandate: Mandate) -> RankRead:
    side = "SHORT" if c.direction == "short" else "LONG"
    if c.desk_verdict == "loss":
        why = "the desk posted stopped out"
    elif c.desk_verdict == "win" and c.desk_max_target >= len(c.targets):
        why = "the desk posted the final target hit"
    elif c.verdict == "unresolved":
        why = "the call timed out with neither level touched"
    else:
        why = f"the tape printed {'the final target' if c.verdict == 'win' else 'the stop'}"
    comps = [Component("base", 0.0, f"closed — {why}")]
    read = RankRead(
        ticker=symbol, side=side, value=0.0,
        band=Band.for_rank(0.0, entry_floor=mandate.entry_floor),
        target_weight_pct=0.0, components=comps, tradeable=True,
        scored_at=c.posted_at.isoformat(), anchored=False, raw=0.0,
    )
    read.source_row = _source_row(c, symbol, c.targets[-1], None, None)   # type: ignore[attr-defined]
    read.source_row["hasLevels"] = False
    read.source_row["levelsReason"] = why
    return read


def _source_row(c: tracker.TrackedCall, symbol: str, target: float,
                rr: Optional[float], k: Optional[dict]) -> dict[str, Any]:
    kind = f"{c.instrument} {c.trade_kind}"
    return {
        "id": c.call_id,
        "ticker": symbol,
        "entry": c.entry,
        "stop": c.stop,
        "target": target,
        "rr": round(rr, 2) if rr else None,
        "setup": f"Stock Unlocked · {kind} · T1 {c.targets[0]:g} → T{len(c.targets)} {c.targets[-1]:g}",
        "hasLevels": True,
        "levelsReason": None,
        "levelProvenance": [{"source": "Stock Unlocked", "at": c.posted_at.isoformat(),
                             "kind": "stated_call"}],
        # Swings carry the signal-horizon the engine's horizon derivation
        # reads; day trades fall through to the mandate's 3-day fallback.
        "signal_aggregate": {"dominant_horizon": "swing" if c.trade_kind == "swing" else None},
        "macro": None,
        "regimeFit": None,
        "trail": {},
        "cohort": {
            "author": "Stock Unlocked",
            "authorId": "stock-unlocked:stock-unlocked",
            "channel": "telegram-channel:stock_unlocked",
            "signalId": None,
            "documentId": c.call_id,
            "rawTicker": c.ticker,
            "callType": f"directional_{c.direction}",
            "tradeStage": ("in_progress" if c.desk_max_target else "entry"),
            "timeframe": c.trade_kind,
            "pattern": c.notes,
            "thesis": c.notes,
            "calledAt": c.posted_at.isoformat(),
            "ageDays": round(k["age"], 2) if k else None,
            "rrAsDrawn": round(rr, 2) if rr else None,
            "rrAtMark": round(k["rr_live"], 2) if k else None,
            "entryAsDrawn": c.entry,
            "markAtScreen": k["mark"] if k else None,
            "targets": list(c.targets),
            "deskMaxTarget": c.desk_max_target,
            "deskVerdict": c.desk_verdict,
            "tapeVerdict": c.verdict,
            "tapeMaxTarget": c.max_target,
        },
    }


def _edge_component(c: tracker.TrackedCall, edges: dict[str, dict], mdl: dict) -> Component:
    key = f"{c.instrument}/{c.trade_kind}"
    e = edges.get(key)
    cap = float(mdl.get("edge_max_points", 8.0))
    min_n = int(mdl.get("edge_min_sample", 10))
    r_cap = float(mdl.get("edge_r_cap", 0.75))
    if not e:
        return Component("slice_edge", 0.0, f"no resolved {key} calls in the ledger yet")
    if e["n"] < min_n:
        return Component("slice_edge", 0.0,
                         f"{key} has {e['n']} resolved calls — under the {min_n} bar")
    r = max(-r_cap, min(r_cap, e["avg_r"]))
    return Component("slice_edge", cap * (r / r_cap),
                     f"{key} runs {e['avg_r']:+.2f}R avg over {e['n']} resolved calls")


__all__ = [
    "BOOK_NAME", "SourceConfig", "book_candidates",
    "load_source_config", "load_book_mandate", "reset_config_cache",
]
