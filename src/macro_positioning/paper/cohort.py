"""The cohort book's candidate source — the Feather Hands crowd's own calls.

The signal book (`paper/rank.py::load_candidates`) trades the desk's view:
a blended trade_score, the technical agent's levels, a composed target.
This module trades something narrower and more honest about what it is —
**the calls themselves**. A chart lands in Telegram, the vision pipeline
extracts a ticker, a side, an entry, a stop and a target, and this book
takes that trade as given. It does not re-decide the target, it does not
consult the desk's structure map, and it does not ask whether the macro
regime agrees. Those are the signal book's job, and running them here
would only reproduce the signal book's answer.

What it is FOR: the two equity curves side by side answer a question
neither one can answer alone — is the desk's blend actually better than
just following these people?

Three things stand between a posted chart and a position:

1. **Is it a trade?** Only `directional_long` / `directional_short`
   survive. A `bidirectional` chart marks both rails without picking a
   side; `no_trade` and `not_a_chart` carry no thesis at all. Reading a
   side out of any of them means inventing one.
2. **Can it be marked?** Roughly 60% of this cohort's flow is Solana
   memecoins (`KINS/SOL`, `JOTCHUA/USDC`) that neither yfinance nor
   Finnhub prices. Those are not silently dropped — every one is counted
   in `Coverage`, which the tick prints and `/api/paper/cohort/coverage`
   serves, so the book always says what fraction of the cohort it is
   actually measuring.
3. **Is the trade still there?** A call is perishable. The stop must
   still be on the right side of the tape, a `watching` breakout must
   have actually broken out, and the reward:risk the author drew must
   still be available at today's price — buying a 3R setup after it has
   run to 0.8R is not following the call, it is chasing it.

The rank that comes out is NOT a percentile. There is no distribution to
rank a Telegram post against; the number is built from the call's own
evidence (conviction, confluence, R:R, how live the author says it is,
how fresh it is, whether the rest of the room agrees, and the author's
measured alpha where the backtest has enough of a sample to have an
opinion). Every term lands on the decision row as a `(name, delta,
reason)` triple, so a 3.2% position is always traceable to the call.
Like the signal book's rank, it is an *ordering* and claims nothing more.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from macro_positioning.core.settings import settings
from macro_positioning.paper.models import Mandate
from macro_positioning.paper.rank import Component, RankRead, target_weight_for
from macro_positioning.paper.vocabulary import Band, TRADEABLE_SIDES
from macro_positioning.prices.symbol_map import resolve_symbol


logger = logging.getLogger(__name__)


COHORT_PATH = "config/paper_cohort.json"

# The book's name in `paper_portfolios`. The signal book is "Signal Book";
# these two strings are how a caller with no portfolio_id tells them apart.
BOOK_NAME = "Cohort Book — Feather Hands"

# Only the manual chart pipeline produces the levels this book needs. A
# newsletter signal on the same ticker is a different animal and does not
# belong in a book that measures one Telegram crowd.
EXTRACTOR = "manual_chart_extractor"


# ── Config ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CohortAuthor:
    author_id: str
    display: str


@dataclass(frozen=True)
class CohortConfig:
    """The roster and the screens, out of config/paper_cohort.json."""

    label: str = "Feather Hands"
    authors: tuple[CohortAuthor, ...] = ()
    lookback_days: int = 10
    call_types: tuple[str, ...] = ("directional_long", "directional_short")
    require_trigger: bool = True
    min_live_rr: float = 1.2
    max_candidates: int = 60
    rank_model: dict = field(default_factory=dict)

    @property
    def author_ids(self) -> tuple[str, ...]:
        return tuple(a.author_id for a in self.authors)

    def display_of(self, author_id: Optional[str]) -> str:
        for a in self.authors:
            if a.author_id == author_id:
                return a.display
        return author_id or "unknown"


@lru_cache(maxsize=2)
def load_cohort_config(path: str | Path | None = None) -> CohortConfig:
    """Read config/paper_cohort.json. Cached; pass `path` in tests.

    A malformed file is NOT quietly defaulted the way the mandate is: the
    mandate has meaningful dataclass defaults, but an empty roster would
    make this book trade nothing while looking like it ran. Raise instead.
    """
    p = Path(path) if path is not None else settings.base_dir / COHORT_PATH
    with open(p, "r", encoding="utf-8") as f:
        raw = json.load(f)
    c = raw.get("cohort") or {}
    authors = tuple(
        CohortAuthor(
            author_id=str(a["author_id"]),
            display=str(a.get("display") or a["author_id"]),
        )
        for a in (c.get("authors") or [])
        if a.get("author_id")
    )
    if not authors:
        raise ValueError(f"{p} defines no cohort authors — the book would trade nothing")
    d = CohortConfig()
    return CohortConfig(
        label=str(c.get("label", d.label)),
        authors=authors,
        lookback_days=int(c.get("lookback_days", d.lookback_days)),
        call_types=tuple(c.get("call_types") or d.call_types),
        require_trigger=bool(c.get("require_trigger", d.require_trigger)),
        min_live_rr=float(c.get("min_live_rr", d.min_live_rr)),
        max_candidates=int(c.get("max_candidates", d.max_candidates)),
        rank_model=dict(raw.get("rank_model") or {}),
    )


def load_cohort_mandate(path: str | Path | None = None) -> Mandate:
    """The cohort book's rulebook. Same shape as the signal book's, read
    from a different file — see the `$why_separate` note in that file."""
    from macro_positioning.paper.models import load_mandate

    return load_mandate(Path(path) if path is not None else settings.base_dir / COHORT_PATH)


def reset_cohort_cache() -> None:
    load_cohort_config.cache_clear()


# ── Coverage: what the book did NOT get to trade, and why ─────────────


@dataclass
class Coverage:
    """The honest denominator.

    This book measures a cohort whose flow it can only partly price. A
    tick that opens two positions out of four hundred calls has not
    "found two trades" — it has found two out of the slice it can see,
    and the size of that slice belongs next to the P&L. Skips are counted
    here rather than written as four hundred refusal rows, which would
    bury the decision log the desk actually reads.
    """

    window_days: int = 0
    considered: int = 0
    candidates: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    unpriceable_tickers: list[tuple[str, int]] = field(default_factory=list)
    by_author: dict[str, int] = field(default_factory=dict)
    generated_at: str = ""

    # Why a call never became a candidate. Ordered by how early the screen
    # runs, so the report reads as a funnel.
    REASONS = {
        "not_directional": "no side to take (bidirectional / no_trade / not_a_chart)",
        "unpriceable": "no mark — DEX pair or microcap outside the price stack",
        "no_levels": "missing entry, stop or target",
        "levels_incoherent": "stop and target on the same side of entry",
        "no_price_history": "priceable in principle, but no bars in the prices table",
        "not_triggered": "a watching call whose entry the tape has not reached",
        "stop_breached": "price is already through the stop — the setup is gone",
        "rr_collapsed": "the call's reward:risk is no longer available at this price",
        "superseded": "an older call on a name the cohort has since re-called",
        "over_limit": "ranked outside the working set (max_candidates)",
    }

    @property
    def priced_pct(self) -> Optional[float]:
        directional = self.considered - self.skipped.get("not_directional", 0)
        if directional <= 0:
            return None
        return 100.0 * (1.0 - self.skipped.get("unpriceable", 0) / directional)

    def as_dict(self) -> dict:
        return {
            "windowDays": self.window_days,
            "considered": self.considered,
            "candidates": self.candidates,
            "skipped": [
                {"reason": k, "count": v, "meaning": self.REASONS.get(k, k)}
                for k, v in sorted(self.skipped.items(), key=lambda kv: -kv[1])
            ],
            "unpriceableTickers": [
                {"ticker": t, "calls": n} for t, n in self.unpriceable_tickers
            ],
            "byAuthor": self.by_author,
            "pricedPct": round(self.priced_pct, 1) if self.priced_pct is not None else None,
            "generatedAt": self.generated_at,
        }

    def report(self) -> str:
        lines = [
            f"cohort coverage — {self.considered} calls in the last "
            f"{self.window_days}d → {self.candidates} candidates"
        ]
        if self.priced_pct is not None:
            lines.append(
                f"  {self.priced_pct:.0f}% of directional calls were priceable"
            )
        for reason, n in sorted(self.skipped.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {n:>5}  {reason:<18} {self.REASONS.get(reason, '')}")
        if self.unpriceable_tickers:
            top = ", ".join(f"{t} x{n}" for t, n in self.unpriceable_tickers[:8])
            lines.append(f"  unpriced flow: {top}")
        return "\n".join(lines)


# ── The author's measured edge ────────────────────────────────────────


def author_edge(
    author_ids: tuple[str, ...], *, db_path: Optional[Path] = None
) -> dict[str, dict]:
    """Per-author market-relative alpha from the `call_outcomes` backtest.

    ALPHA, not win rate. A fixed-horizon directional win rate in a
    trending market is mostly beta — OG Whales scored 12% directional and
    81% setup-resolution on the same calls, which is the whole argument.
    `call_outcomes.alpha_pct` is the call's return minus the market's over
    the same window.

    Best-effort: a missing table degrades every author to "no measured
    edge", which the rank component then says out loud instead of
    assuming one.
    """
    from macro_positioning.db.connect import read_connection

    if not author_ids:
        return {}
    conn = read_connection(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT author_id,
                   COUNT(*)          AS n,
                   AVG(alpha_pct)    AS alpha,
                   MAX(scored_at)    AS last_scored
              FROM call_outcomes
             WHERE alpha_pct IS NOT NULL
               AND author_id IN ({','.join('?' * len(author_ids))})
             GROUP BY author_id
            """,
            author_ids,
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        logger.debug("call_outcomes unavailable: %s", exc)
        return {}
    finally:
        conn.close()
    return {
        r["author_id"]: {
            "n": int(r["n"] or 0),
            "alpha": float(r["alpha"]) if r["alpha"] is not None else None,
            "lastScored": r["last_scored"],
        }
        for r in rows
    }


# ── Reading one call ──────────────────────────────────────────────────


def _detail(raw: Any) -> dict:
    if not raw:
        return {}
    try:
        d = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _f(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _age_days(ts: Optional[str], now: datetime) -> Optional[float]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (now - dt).total_seconds() / 86400.0


def _rr(entry: float, stop: float, target: float) -> Optional[float]:
    risk = abs(entry - stop)
    return abs(target - entry) / risk if risk > 0 else None


# Stages where the author says the trade is on. Anything else is a plan.
_LIVE_STAGES = {"entry", "active", "in_progress"}


# ── The candidate pass ────────────────────────────────────────────────


def cohort_candidates(
    mandate: Optional[Mandate] = None,
    *,
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    config: Optional[CohortConfig] = None,
    mark_fn=None,
) -> tuple[list[RankRead], Coverage]:
    """Every cohort call the book could act on today, strongest first.

    Returns `(reads, coverage)`. The reads go straight into
    `run_tick(candidates=...)`; the coverage is the denominator that makes
    the resulting P&L readable.

    `mark_fn` maps a resolved symbol to a last price and exists so tests
    can screen calls without a prices table. It defaults to the daily
    close already in the DB — deliberately NOT a live quote: this is a
    coarse screen, and the tick fetches real marks for the fill moments
    later.
    """
    from macro_positioning.db.connect import read_connection

    cfg = config or load_cohort_config()
    m = mandate or load_cohort_mandate()
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=cfg.lookback_days)).isoformat()

    cov = Coverage(window_days=cfg.lookback_days, generated_at=now.isoformat())
    skipped: Counter = Counter()
    unpriced: Counter = Counter()

    edges = author_edge(cfg.author_ids, db_path=db_path)

    conn = read_connection(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT signal_id, document_id, asset_ticker, side, conviction,
                   entry_zone_low, entry_zone_high, stop_loss, target_1, target_2,
                   horizon, thesis_summary, instrument_detail_json,
                   author_id, source_channel, extracted_at
              FROM signals
             WHERE status = 'active'
               AND extractor_name = ?
               AND extracted_at >= ?
               AND author_id IN ({','.join('?' * len(cfg.author_ids))})
             ORDER BY extracted_at DESC
            """,
            (EXTRACTOR, cutoff, *cfg.author_ids),
        ).fetchall()

        cov.considered = len(rows)
        if mark_fn is None:
            from macro_positioning.prices.fetcher import latest_close

            def mark_fn(sym: str) -> Optional[float]:      # noqa: F811
                return latest_close(sym, conn=conn)

        # Pass 1 — screen each call down to a trade the book could take.
        kept: list[dict] = []
        for r in rows:
            det = _detail(r["instrument_detail_json"])
            call_type = (det.get("call_type") or "").lower()
            side = (r["side"] or "").upper()
            if call_type not in cfg.call_types or side not in TRADEABLE_SIDES:
                skipped["not_directional"] += 1
                continue

            symbol = resolve_symbol(r["asset_ticker"] or "")
            if not symbol:
                skipped["unpriceable"] += 1
                unpriced[(r["asset_ticker"] or "?").upper()] += 1
                continue

            entry = _f(r["entry_zone_low"]) or _f(r["entry_zone_high"])
            stop = _f(r["stop_loss"])
            target = _f(r["target_1"]) or _f(r["target_2"])
            if entry is None or stop is None or target is None:
                skipped["no_levels"] += 1
                continue

            # An extraction can read a chart backwards. A long whose stop
            # sits above its entry, or whose target sits below it, is not a
            # trade the book can size — its "risk" and "reward" are the
            # same direction.
            long_ok = stop < entry < target
            short_ok = stop > entry > target
            if not (long_ok if side == "LONG" else short_ok):
                skipped["levels_incoherent"] += 1
                continue

            mark = mark_fn(symbol)
            if mark is None or mark <= 0:
                skipped["no_price_history"] += 1
                continue

            # The stop already went. Whatever this chart was, it is over.
            if (mark <= stop) if side == "LONG" else (mark >= stop):
                skipped["stop_breached"] += 1
                continue

            stage = (det.get("trade_stage") or "").lower()
            live = stage in _LIVE_STAGES
            if cfg.require_trigger and not live:
                triggered = (mark >= entry) if side == "LONG" else (mark <= entry)
                if not triggered:
                    skipped["not_triggered"] += 1
                    continue

            # The call's reward:risk, re-measured at today's price rather
            # than at the author's entry. A 3R setup bought after it has
            # run to 0.8R is not the trade that was posted.
            rr_live = _rr(mark, stop, target)
            if rr_live is None or rr_live < cfg.min_live_rr:
                skipped["rr_collapsed"] += 1
                continue

            kept.append({
                "row": r, "detail": det, "symbol": symbol, "side": side,
                "entry": entry, "stop": stop, "target": target,
                "mark": mark, "rr": _rr(entry, stop, target), "rr_live": rr_live,
                "live": live, "stage": stage,
                "age": _age_days(r["extracted_at"], now) or 0.0,
            })
    finally:
        conn.close()

    # Pass 2 — what the room as a whole is saying. Counted across every
    # call that survived the screens, before the per-symbol dedupe, so a
    # name three of them are long reads as agreement rather than as one
    # call that happened to be newest.
    voices: dict[tuple[str, str], set[str]] = defaultdict(set)
    for c in kept:
        voices[(c["symbol"], c["side"])].add(c["row"]["author_id"])

    # Pass 3 — one call per symbol: the freshest. `kept` is already in
    # extracted_at DESC order, so the first sighting wins and every older
    # call on that name is an update the room has moved past.
    reads: list[RankRead] = []
    seen: set[str] = set()
    by_author: Counter = Counter()
    for c in kept:
        if c["symbol"] in seen:
            skipped["superseded"] += 1
            continue
        seen.add(c["symbol"])
        reads.append(_read_for(c, cfg, m, voices, edges))
        by_author[cfg.display_of(c["row"]["author_id"])] += 1

    reads.sort(key=lambda r: (r.raw, r.value), reverse=True)
    if len(reads) > cfg.max_candidates:
        skipped["over_limit"] += len(reads) - cfg.max_candidates
        reads = reads[: cfg.max_candidates]

    cov.candidates = len(reads)
    cov.skipped = dict(skipped)
    cov.unpriceable_tickers = unpriced.most_common(20)
    cov.by_author = dict(by_author)
    return reads, cov


def _read_for(
    c: dict,
    cfg: CohortConfig,
    mandate: Mandate,
    voices: dict[tuple[str, str], set[str]],
    edges: dict[str, dict],
) -> RankRead:
    """Turn one screened call into a ranked, engine-ready read."""
    mdl = cfg.rank_model
    r = c["row"]
    det = c["detail"]
    author_id = r["author_id"]
    author = cfg.display_of(author_id)
    side = c["side"]

    base = float(mdl.get("base", 50.0))
    comps = [Component("base", base, f"a {cfg.label} call, taken at face value")]

    conviction = _f(r["conviction"])
    if conviction is not None:
        d = (conviction - 2.5) * float(mdl.get("conviction_per_point", 8.0))
        comps.append(Component(
            "conviction", d, f"{author} called it {conviction:g}/5"
        ))

    confluence = _f(det.get("confluence_score"))
    if confluence is not None:
        d = (confluence - 3.0) * float(mdl.get("confluence_per_point", 3.0))
        comps.append(Component(
            "confluence", d, f"{confluence:g}/5 things on the chart agreed"
        ))

    rr = c["rr"]
    if rr is not None:
        if rr >= 3.0:
            comps.append(Component("rr", float(mdl.get("rr_bonus_3r", 10.0)),
                                   f"{rr:.1f}R to target as drawn"))
        elif rr >= 2.0:
            comps.append(Component("rr", float(mdl.get("rr_bonus_2r", 5.0)),
                                   f"{rr:.1f}R to target as drawn"))
        elif rr < float(mdl.get("rr_thin_below", 1.2)):
            comps.append(Component("rr", float(mdl.get("rr_penalty_thin", -10.0)),
                                   f"only {rr:.1f}R — thin for the risk"))

    if c["live"]:
        comps.append(Component(
            "stage", float(mdl.get("stage_live_bonus", 8.0)),
            f"{author} is in it ({c['stage']}), not watching it",
        ))

    grace = float(mdl.get("stale_after_days", 2.0))
    if c["age"] > grace:
        d = max(
            float(mdl.get("stale_floor", -12.0)),
            (c["age"] - grace) * float(mdl.get("stale_per_day", -2.0)),
        )
        comps.append(Component("freshness", d, f"the call is {c['age']:.1f} days old"))

    agree = voices.get((c["symbol"], side), set())
    against = voices.get((c["symbol"], "SHORT" if side == "LONG" else "LONG"), set())
    if len(agree) > 1:
        comps.append(Component(
            "agreement", float(mdl.get("agreement_bonus", 10.0)),
            f"{len(agree)} of the cohort are {side} {c['symbol']}",
        ))
    if against:
        comps.append(Component(
            "opposition", float(mdl.get("opposition_penalty", -15.0)),
            f"{len(against)} of the cohort are the other way on {c['symbol']}",
        ))

    comps.append(_edge_component(author, edges.get(author_id), mdl))

    raw = sum(x.delta for x in comps)
    value = max(0.0, min(100.0, raw))
    read = RankRead(
        ticker=c["symbol"],
        side=side,
        value=value,
        band=Band.for_rank(value, entry_floor=mandate.entry_floor),
        target_weight_pct=target_weight_for(value, mandate),
        components=comps,
        tradeable=True,
        score=None,
        grade=None,
        # The call's timestamp IS the thesis date: it drives STALE_THESIS,
        # which in a copy book is the exit that does the most work. Almost
        # no call is ever explicitly closed by its author; the room simply
        # stops mentioning the name.
        scored_at=r["extracted_at"],
        anchored=False,          # no distribution — see the module docstring
        raw=raw,
    )
    timeframe = det.get("chart_timeframe") or r["horizon"] or ""
    pattern = det.get("pattern") or det.get("call_type") or "call"
    read.source_row = {                      # type: ignore[attr-defined]
        "id": r["signal_id"],
        "ticker": c["symbol"],
        "entry": c["entry"],
        "stop": c["stop"],
        "target": c["target"],
        "rr": round(rr, 2) if rr else None,
        "setup": " · ".join(x for x in (author, timeframe, pattern) if x),
        "hasLevels": True,
        "levelsReason": None,
        "levelProvenance": [{
            "source": author, "at": r["extracted_at"], "kind": "cohort_call",
        }],
        # The desk views the composed valuation would want. They are None
        # by design: this book does not re-decide the trade (see the
        # `composition` block in config/paper_cohort.json).
        "macro": None,
        "regimeFit": None,
        "trail": {},
        "cohort": {
            "author": author,
            "authorId": author_id,
            "channel": r["source_channel"],
            "signalId": r["signal_id"],
            "documentId": r["document_id"],
            "rawTicker": r["asset_ticker"],
            "callType": det.get("call_type"),
            "tradeStage": c["stage"] or None,
            "timeframe": timeframe or None,
            "pattern": det.get("pattern"),
            "thesis": r["thesis_summary"],
            "calledAt": r["extracted_at"],
            "ageDays": round(c["age"], 2),
            "rrAsDrawn": round(rr, 2) if rr else None,
            "rrAtMark": round(c["rr_live"], 2),
            "entryAsDrawn": c["entry"],
            "markAtScreen": c["mark"],
        },
    }
    return read


def _edge_component(author: str, edge: Optional[dict], mdl: dict) -> Component:
    """The author's measured alpha, capped so it tilts size without deciding it."""
    cap = float(mdl.get("edge_max_points", 8.0))
    min_n = int(mdl.get("edge_min_sample", 30))
    alpha_cap = float(mdl.get("edge_alpha_cap_pct", 5.0))
    if not edge or edge.get("alpha") is None:
        return Component("author_edge", 0.0, f"no measured edge for {author} yet")
    n = int(edge.get("n") or 0)
    if n < min_n:
        return Component(
            "author_edge", 0.0,
            f"{author}'s record is {n} scored calls — under the {min_n} bar to count",
        )
    alpha = max(-alpha_cap, min(alpha_cap, float(edge["alpha"])))
    delta = cap * (alpha / alpha_cap)
    return Component(
        "author_edge", delta,
        f"{author} runs {edge['alpha']:+.1f}% alpha over {n} scored calls "
        f"(backtest last run {(edge.get('lastScored') or '?')[:10]})",
    )


__all__ = [
    "BOOK_NAME",
    "CohortAuthor",
    "CohortConfig",
    "Coverage",
    "author_edge",
    "cohort_candidates",
    "load_cohort_config",
    "load_cohort_mandate",
    "reset_cohort_cache",
]
