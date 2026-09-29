"""Trusted-voice levels — what the humans drew, weighted by who was right.

The chart structure in `prices.structure` says where the levels are. This
module says which of them people the operator actually follows have called
out, and how much that should count.

Three weights multiply into one:

- **Track record** — `learning.call_accuracy.source_accuracy` gives each
  author a backtested `setup_win_rate` (target hit before stop). Authors
  without enough priceable calls to be `meaningful` still contribute, but
  at a floor weight: they can join a consensus, never drive one.
- **Recency** — a level drawn six weeks ago on a chart that has since
  moved is not the level being defended today. Half-life in days.
- **Conviction** — the extractor's 0–5 read on how hard the call was
  stated, normalised with a floor so a quiet mention still counts.

Only authors on the seeded allowlist are considered at all — the same rule
the conviction map uses. A channel the listener happened to pick up is not
a trusted voice just because it posted a number.

Every consensus carries its contributors, so the card can say *who* called
the level and *why* it counts, rather than presenting a number from
nowhere.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from macro_positioning.manual.authors import SEEDED_AUTHOR_WHERE

# Authors without a `meaningful` accuracy sample still speak, quietly.
_UNPROVEN_WEIGHT = 0.15

# A setup win rate at or below this is no better than a coin flip on a
# 1:1 setup — such authors are heard at the unproven floor, not above it.
_WIN_RATE_FLOOR = 0.4

# Recency half-life for a drawn level, in days.
_RECENCY_HALFLIFE_DAYS = 14.0

# Levels older than this are not part of today's picture at all.
_MAX_AGE_DAYS = 60

# Conviction (0–5) normalisation floor, so a 0-conviction mention still
# contributes something rather than vanishing.
_CONVICTION_FLOOR = 0.2

# Consensus clustering tolerance, in ATR.
_CLUSTER_ATR = 0.6

# Sides that express a level worth comparing with the agent's rails.
_DIRECTIONAL = {"LONG", "SHORT"}


@dataclass
class Contributor:
    """One human behind a level, and the standing they bring to it."""

    author_id: str
    display_name: str
    price: float
    weight: float
    setup_win_rate: float | None
    meaningful: bool
    n_calls: int
    conviction: float | None
    at: str
    thesis: str | None = None
    chart_url: str | None = None

    @property
    def credential(self) -> str:
        """The one-line "why this voice counts" the card shows."""
        if self.meaningful and self.setup_win_rate is not None:
            return f"{self.setup_win_rate * 100:.0f}% setup win over {self.n_calls} calls"
        return "unproven — too few resolved calls to rate"


@dataclass
class Consensus:
    """A level several trusted voices agree on."""

    price: float
    weight: float                    # summed contributor weight
    contributors: list[Contributor] = field(default_factory=list)
    trusted: bool = False            # at least one `meaningful` author

    @property
    def basis(self) -> str:
        names = [c.display_name for c in self.contributors[:3]]
        who = ", ".join(names)
        if len(self.contributors) > 3:
            who += f" +{len(self.contributors) - 3}"
        return f"{who} ({self.contributors[0].credential})" if self.contributors else ""


@dataclass
class KolLevels:
    entry: Consensus | None = None
    stop: Consensus | None = None
    target: Consensus | None = None
    n_signals: int = 0
    side: str | None = None          # dominant directional side by weight
    played_out: int = 0              # rows spot has already traded through


# ---------------------------------------------------------------------------
# Author weighting
# ---------------------------------------------------------------------------

def author_weights(
    *, window_days: int | None = 365, db_path: Path | None = None
) -> dict[str, dict]:
    """{author_id: {weight, setup_win_rate, meaningful, n_calls, display_name}}.

    Weight is the backtested setup win rate for authors with a meaningful
    sample, and a floor for everyone else. An author who resolves setups
    worse than `_WIN_RATE_FLOOR` is not amplified above that floor either —
    being consistently wrong shouldn't earn a loud voice.
    """
    from macro_positioning.learning.call_accuracy import source_accuracy

    out: dict[str, dict] = {}
    try:
        rows = source_accuracy(window_days=window_days, db_path=db_path)
    except Exception:
        return out  # no outcomes table yet — everyone falls back to the floor

    for r in rows:
        win = r.get("setup_win_rate")
        meaningful = bool(r.get("meaningful")) and win is not None
        weight = float(win) if (meaningful and win > _WIN_RATE_FLOOR) else _UNPROVEN_WEIGHT
        out[r["author_id"]] = {
            "weight": round(weight, 4),
            "setup_win_rate": round(float(win), 4) if win is not None else None,
            "meaningful": meaningful,
            "n_calls": int(r.get("n_priceable") or r.get("n_calls") or 0),
            "display_name": r.get("display_name") or r["author_id"],
        }
    return out


def _recency_weight(at: str | None, *, now: datetime | None = None) -> float:
    """Half-life decay on the age of the call. Unparseable dates get the
    benefit of the doubt at half weight rather than being dropped."""
    if not at:
        return 0.5
    now = now or datetime.now(UTC)
    try:
        stamp = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
    except ValueError:
        return 0.5
    days = max(0.0, (now - stamp).total_seconds() / 86400)
    if days > _MAX_AGE_DAYS:
        return 0.0
    return 0.5 ** (days / _RECENCY_HALFLIFE_DAYS)


def _conviction_weight(conviction: float | None) -> float:
    if conviction is None:
        return _CONVICTION_FLOOR
    return max(_CONVICTION_FLOOR, min(1.0, float(conviction) / 5.0))


def _played_out(row: dict, close: float | None) -> bool:
    """Has the market already settled this call?

    Age is not the only way a level goes stale. A LONG whose target sits
    at or below spot is not a target — it is a price the market traded
    through while the call aged, and adopting it points the trade
    backwards. The entry and stop drawn alongside it describe the same
    finished chart, so the whole row leaves the pool rather than
    contributing rails to a setup it was never drawn for.

    Without a spot price this cannot be judged, and the row stays: a
    missing close must not silently empty the consensus.
    """
    if close is None or close <= 0:
        return False
    side, target = row.get("side"), row.get("target")
    if target is None or side not in _DIRECTIONAL:
        return False
    return target <= close if side == "LONG" else target >= close


# ---------------------------------------------------------------------------
# Loading + consensus
# ---------------------------------------------------------------------------

def load_kol_levels(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    lookback_days: int = _MAX_AGE_DAYS,
    limit: int = 40,
) -> list[dict]:
    """Recent drawn levels for one ticker from seeded authors only."""
    try:
        rows = conn.execute(
            f"""
            SELECT s.signal_id, s.side, s.conviction,
                   s.entry_zone_low, s.entry_zone_high, s.stop_loss, s.target_1,
                   s.author_id, s.source_channel, s.thesis_summary,
                   COALESCE(d.published_at, s.extracted_at) AS at,
                   d.attachment_path,
                   COALESCE(ia.display_name, s.author_id) AS display_name
            FROM signals s
            LEFT JOIN documents d ON d.document_id = s.document_id
            LEFT JOIN input_authors ia ON ia.author_id = s.author_id
            WHERE s.asset_ticker = ?
              AND s.status = 'active'
              AND s.author_id IN (
                  SELECT author_id FROM input_authors WHERE {SEEDED_AUTHOR_WHERE}
              )
              AND datetime(COALESCE(d.published_at, s.extracted_at))
                  >= datetime('now', ?)
              AND (s.entry_zone_low IS NOT NULL OR s.entry_zone_high IS NOT NULL
                   OR s.stop_loss IS NOT NULL OR s.target_1 IS NOT NULL)
            ORDER BY datetime(COALESCE(d.published_at, s.extracted_at)) DESC
            LIMIT ?
            """,
            (ticker.upper(), f"-{int(lookback_days)} days", limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # signals/input_authors absent on a pre-migration DB

    return [
        {
            "signal_id": r[0], "side": (r[1] or "").upper(), "conviction": r[2],
            "entry_low": r[3], "entry_high": r[4], "stop": r[5], "target": r[6],
            "author_id": r[7], "channel": r[8], "thesis": r[9], "at": r[10],
            "chart": (f"/{r[11]}" if r[11] else None), "display_name": r[12],
        }
        for r in rows
    ]


def _cluster(points: list[Contributor], tolerance: float) -> Consensus | None:
    """Heaviest cluster of drawn prices, as one consensus level.

    Weighted mean within the winning cluster: three voices at 3,000 and one
    at 3,400 should land near 3,000, not at the arithmetic middle.
    """
    if not points or tolerance <= 0:
        return None
    ordered = sorted(points, key=lambda c: c.price)
    clusters: list[list[Contributor]] = [[ordered[0]]]
    for c in ordered[1:]:
        current = clusters[-1]
        centre = sum(x.price for x in current) / len(current)
        if abs(c.price - centre) <= tolerance:
            current.append(c)
        else:
            clusters.append([c])

    best = max(clusters, key=lambda cl: sum(c.weight for c in cl))
    total = sum(c.weight for c in best)
    if total <= 0:
        return None
    price = sum(c.price * c.weight for c in best) / total
    ranked = sorted(best, key=lambda c: c.weight, reverse=True)
    return Consensus(
        price=price,
        weight=round(total, 4),
        contributors=ranked,
        trusted=any(c.meaningful for c in ranked),
    )


def kol_levels_for_ticker(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    atr: float | None,
    weights: dict[str, dict] | None = None,
    now: datetime | None = None,
    close: float | None = None,
) -> KolLevels:
    """Weighted consensus of the entry / stop / target humans drew.

    `weights` comes from `author_weights()` — pass it in when scoring a
    whole watchlist so the accuracy rollup is computed once per pass.

    `close` is spot. Pass it wherever it is known: it is what lets a call
    the market has already resolved drop out of the pool instead of
    ageing quietly through the recency window.
    """
    if not atr or atr <= 0:
        return KolLevels()
    rows = load_kol_levels(conn, ticker)
    if not rows:
        return KolLevels()
    weights = weights if weights is not None else author_weights()

    entries: list[Contributor] = []
    stops: list[Contributor] = []
    targets: list[Contributor] = []
    side_weight: dict[str, float] = {}
    settled = 0

    for r in rows:
        aw = weights.get(r["author_id"]) or {
            "weight": _UNPROVEN_WEIGHT, "setup_win_rate": None,
            "meaningful": False, "n_calls": 0,
            "display_name": r["display_name"],
        }
        w = (
            float(aw["weight"])
            * _recency_weight(r["at"], now=now)
            * _conviction_weight(r["conviction"])
        )
        if w <= 0:
            continue
        if _played_out(r, close):
            settled += 1
            continue
        if r["side"] in _DIRECTIONAL:
            side_weight[r["side"]] = side_weight.get(r["side"], 0.0) + w

        def _contrib(price: float) -> Contributor:
            return Contributor(
                author_id=r["author_id"],
                display_name=aw["display_name"] or r["display_name"],
                price=float(price),
                weight=round(w, 5),
                setup_win_rate=aw["setup_win_rate"],
                meaningful=bool(aw["meaningful"]),
                n_calls=int(aw["n_calls"]),
                conviction=r["conviction"],
                at=(r["at"] or "")[:16].replace("T", " "),
                thesis=r["thesis"],
                chart_url=r["chart"],
            )

        zone = [v for v in (r["entry_low"], r["entry_high"]) if v]
        if zone:
            entries.append(_contrib(sum(zone) / len(zone)))
        if r["stop"]:
            stops.append(_contrib(r["stop"]))
        if r["target"]:
            targets.append(_contrib(r["target"]))

    tolerance = _CLUSTER_ATR * atr
    dominant = max(side_weight, key=side_weight.get) if side_weight else None
    return KolLevels(
        entry=_cluster(entries, tolerance),
        stop=_cluster(stops, tolerance),
        target=_cluster(targets, tolerance),
        n_signals=len(rows) - settled,
        side=dominant,
        played_out=settled,
    )
