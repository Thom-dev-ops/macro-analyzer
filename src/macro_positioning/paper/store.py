"""SQLite persistence for the paper book.

Thin, explicit CRUD over the five `paper_*` tables. No ORM, same WAL +
busy_timeout posture the rest of the project uses, because the API
server, the launchd tick and a worker chat all touch this DB at once.

The invariant this module defends:

    cash == starting_equity + Σ paper_orders.cash_delta

`paper_orders` is the immutable fill log and therefore the source of
truth; `paper_portfolios.cash` is a cached running total. `reconcile()`
checks one against the other, and the engine refuses to commit a tick
that fails it — a book whose cash cannot be rebuilt from its own fills
is a book whose P&L means nothing.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from macro_positioning.core.settings import settings
from macro_positioning.paper.models import Decision, Mandate, Order, Portfolio, Position, load_mandate
from macro_positioning.paper.vocabulary import Action, Blocker, Intent


DEFAULT_BOOK_NAME = "Signal Book"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# The live DB has three other writers (two API servers and the Telegram
# listener) and carries a large WAL, so a write can genuinely wait behind
# someone else's transaction for tens of seconds. The tick runs twice a
# day and is never latency-sensitive; waiting beats failing.
_WRITE_TIMEOUT_MS = 30_000
_READ_TIMEOUT_MS = 10_000


@contextmanager
def connect(db_path: Optional[Path] = None, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Open the project DB with the house pragmas.

    Read paths use a URI ro connection so a long SPA poll can never
    block the tick's writer.
    """
    path = db_path or settings.sqlite_path
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_READ_TIMEOUT_MS / 1000)
        conn.execute(f"PRAGMA busy_timeout={_READ_TIMEOUT_MS}")
    else:
        conn = sqlite3.connect(path, timeout=_WRITE_TIMEOUT_MS / 1000)
        conn.execute(f"PRAGMA busy_timeout={_WRITE_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            conn.commit()
    finally:
        conn.close()


def _loads(raw: Any) -> Any:
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


# ── Portfolios ────────────────────────────────────────────────────────


def _row_to_portfolio(r: sqlite3.Row) -> Portfolio:
    return Portfolio(
        portfolio_id=r["portfolio_id"],
        name=r["name"],
        base_currency=r["base_currency"],
        starting_equity=float(r["starting_equity"]),
        cash=float(r["cash"]),
        status=r["status"],
        mandate=Mandate.from_stored(r["mandate_json"]),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        last_tick_at=r["last_tick_at"],
        notes=r["notes"],
    )


def create_portfolio(
    *,
    name: str = DEFAULT_BOOK_NAME,
    mandate: Optional[Mandate] = None,
    db_path: Optional[Path] = None,
    notes: Optional[str] = None,
) -> Portfolio:
    """Open a new book. Cash starts at the mandate's full equity — the
    opening deposit is the only cash movement not backed by an order,
    which is why `reconcile()` starts from `starting_equity`."""
    m = mandate or load_mandate()
    pf = Portfolio(
        portfolio_id=_new_id("pbook"),
        name=name,
        base_currency=m.base_currency,
        starting_equity=m.starting_equity,
        cash=m.starting_equity,
        mandate=m,
        created_at=_now(),
        notes=notes,
    )
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO paper_portfolios
                (portfolio_id, name, base_currency, starting_equity, cash,
                 status, mandate_json, created_at, updated_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pf.portfolio_id, pf.name, pf.base_currency, pf.starting_equity,
                pf.cash, pf.status, json.dumps(m.as_dict()), pf.created_at,
                pf.created_at, pf.notes,
            ),
        )
    return pf


def get_portfolio(
    portfolio_id: Optional[str] = None, *, db_path: Optional[Path] = None
) -> Optional[Portfolio]:
    """Fetch a book by id, or the oldest active one when id is omitted.
    The desk runs a single book; the id parameter exists so a what-if
    book can be run beside it without a schema change."""
    with connect(db_path, readonly=True) as conn:
        if portfolio_id:
            row = conn.execute(
                "SELECT * FROM paper_portfolios WHERE portfolio_id = ?", (portfolio_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM paper_portfolios WHERE status = 'active' "
                "ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
    return _row_to_portfolio(row) if row else None


def get_or_create_portfolio(
    *, name: str = DEFAULT_BOOK_NAME, db_path: Optional[Path] = None
) -> Portfolio:
    return get_portfolio(db_path=db_path) or create_portfolio(name=name, db_path=db_path)


def list_portfolios(*, db_path: Optional[Path] = None) -> list[Portfolio]:
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM paper_portfolios ORDER BY created_at ASC"
        ).fetchall()
    return [_row_to_portfolio(r) for r in rows]


def update_portfolio_mandate(
    conn: sqlite3.Connection, portfolio_id: str, mandate: Mandate
) -> None:
    """Re-stamp a portfolio's mandate. Used ONLY to heal a mandate stored
    on a superseded scale — a mandate is otherwise frozen at creation so
    an equity curve stays interpretable against the rules that made it."""
    conn.execute(
        "UPDATE paper_portfolios SET mandate_json = ?, updated_at = ? WHERE portfolio_id = ?",
        (json.dumps(mandate.as_dict()), _now(), portfolio_id),
    )


def update_portfolio_cash(
    conn: sqlite3.Connection, portfolio_id: str, cash: float, *, tick_at: Optional[str] = None
) -> None:
    conn.execute(
        """
        UPDATE paper_portfolios
           SET cash = ?, updated_at = ?, last_tick_at = COALESCE(?, last_tick_at)
         WHERE portfolio_id = ?
        """,
        (round(cash, 6), _now(), tick_at, portfolio_id),
    )


# ── Positions ─────────────────────────────────────────────────────────


def _row_to_position(r: sqlite3.Row) -> Position:
    return Position(
        position_id=r["position_id"],
        portfolio_id=r["portfolio_id"],
        ticker=r["ticker"],
        side=r["side"],
        qty=float(r["qty"]),
        avg_price=float(r["avg_price"]),
        opened_at=r["opened_at"],
        closed_at=r["closed_at"],
        status=r["status"],
        stop=r["stop"],
        target=r["target"],
        rank_at_entry=r["rank_at_entry"],
        rank_now=r["rank_now"],
        target_weight_pct=r["target_weight_pct"],
        thesis=r["thesis"],
        bucket_id=r["bucket_id"],
        source=_loads(r["source_json"]) or {},
        realized_pnl=float(r["realized_pnl"] or 0.0),
        fees_paid=float(r["fees_paid"] or 0.0),
        initial_risk=r["initial_risk"],
        exit_signal_intent=r["exit_signal_intent"],
        exit_signal_streak=int(r["exit_signal_streak"] or 0),
        expected_hold_days=r["expected_hold_days"],
        min_hold_days=r["min_hold_days"],
        confirm_ticks=r["confirm_ticks"],
        exit_path=r["exit_path"],
        rungs_taken=int(r["rungs_taken"] or 0),
        near_miss_armed=bool(r["near_miss_armed"]),
        high_water_price=r["high_water_price"],
        last_mark=r["last_mark"],
        last_mark_at=r["last_mark_at"],
        partial_taken=bool(r["partial_taken"]),
    )


def load_positions(
    portfolio_id: str, *, status: Optional[str] = "open", db_path: Optional[Path] = None
) -> list[Position]:
    sql = "SELECT * FROM paper_positions WHERE portfolio_id = ?"
    params: list[Any] = [portfolio_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY opened_at ASC"
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_position(r) for r in rows]


def get_position(position_id: str, *, db_path: Optional[Path] = None) -> Optional[Position]:
    with connect(db_path, readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM paper_positions WHERE position_id = ?", (position_id,)
        ).fetchone()
    return _row_to_position(row) if row else None


def insert_position(conn: sqlite3.Connection, p: Position) -> None:
    conn.execute(
        """
        INSERT INTO paper_positions
            (position_id, portfolio_id, ticker, side, qty, avg_price, opened_at,
             closed_at, status, stop, target, rank_at_entry, rank_now,
             target_weight_pct, thesis, bucket_id, source_json, realized_pnl,
             fees_paid, initial_risk, high_water_price, last_mark, last_mark_at,
             partial_taken, exit_signal_intent, exit_signal_streak,
             expected_hold_days, min_hold_days, confirm_ticks,
             exit_path, rungs_taken, near_miss_armed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            p.position_id, p.portfolio_id, p.ticker, p.side, p.qty, p.avg_price,
            p.opened_at, p.closed_at, p.status, p.stop, p.target,
            p.rank_at_entry, p.rank_now, p.target_weight_pct,
            p.thesis, p.bucket_id, json.dumps(p.source or {}), p.realized_pnl,
            p.fees_paid, p.initial_risk, p.high_water_price, p.last_mark, p.last_mark_at,
            1 if p.partial_taken else 0, p.exit_signal_intent, p.exit_signal_streak,
            p.expected_hold_days, p.min_hold_days, p.confirm_ticks,
            p.exit_path, p.rungs_taken, 1 if p.near_miss_armed else 0,
        ),
    )


def update_position(conn: sqlite3.Connection, p: Position) -> None:
    conn.execute(
        """
        UPDATE paper_positions
           SET qty = ?, avg_price = ?, status = ?, closed_at = ?, stop = ?,
               target = ?, rank_now = ?, target_weight_pct = ?,
               thesis = ?, source_json = ?, realized_pnl = ?, fees_paid = ?,
               high_water_price = ?, last_mark = ?, last_mark_at = ?,
               partial_taken = ?, exit_signal_intent = ?, exit_signal_streak = ?,
               expected_hold_days = ?, min_hold_days = ?, confirm_ticks = ?,
               exit_path = ?, rungs_taken = ?, near_miss_armed = ?
         WHERE position_id = ?
        """,
        (
            p.qty, p.avg_price, p.status, p.closed_at, p.stop, p.target,
            p.rank_now, p.target_weight_pct, p.thesis,
            json.dumps(p.source or {}), p.realized_pnl, p.fees_paid,
            p.high_water_price, p.last_mark, p.last_mark_at,
            1 if p.partial_taken else 0, p.exit_signal_intent, p.exit_signal_streak,
            p.expected_hold_days, p.min_hold_days, p.confirm_ticks,
            p.exit_path, p.rungs_taken, 1 if p.near_miss_armed else 0,
            p.position_id,
        ),
    )


def new_position_id() -> str:
    return _new_id("ppos")


# ── Orders ────────────────────────────────────────────────────────────


def new_order_id() -> str:
    return _new_id("pord")


def insert_order(conn: sqlite3.Connection, o: Order) -> None:
    conn.execute(
        """
        INSERT INTO paper_orders
            (order_id, portfolio_id, position_id, decision_id, tick_id, ticker,
             action, side, qty, price, ref_price, notional, cash_delta,
             realized_pnl, fees, intent, rationale, price_source, filled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            o.order_id, o.portfolio_id, o.position_id, o.decision_id, o.tick_id,
            o.ticker, str(o.action), o.side, o.qty, o.price, o.ref_price,
            o.notional, o.cash_delta, o.realized_pnl, o.fees, str(o.intent),
            o.rationale, o.price_source, o.filled_at,
        ),
    )


# Exits the re-entry rule cares about. Deliberately only the PRICE-based
# ones. A `side_flip` or `rank_decay` exit was a change of view, not a
# level break — there is nothing for price to reclaim, and the 60/40
# entry/exit hysteresis is already a 20-point buffer against churning
# those. Rotation (`make_room`) and cash-floor trims are not exits of
# conviction at all: the book needed the capital, so buying the name back
# is legitimate the moment there is room.
PRICE_EXIT_INTENTS = ("stop_hit", "trail_giveback", "trail_round_trip")


def recent_price_exits(
    portfolio_id: str, *, since: str, db_path: Optional[Path] = None
) -> dict[str, dict]:
    """Names stopped or trailed out since `since`, with what it would take
    to legitimately buy them back.

    Returns per ticker: the most recent exit (intent, time, fill price),
    the price the book had actually paid (`entry_price` — the "original
    trade level" a reclaim has to get back above), the side, and how many
    times this name has done it inside the window.
    """
    placeholders = ", ".join("?" for _ in PRICE_EXIT_INTENTS)
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(
            f"""
            SELECT o.ticker, o.intent, o.filled_at, o.price AS exit_price,
                   p.avg_price AS entry_price, p.side
              FROM paper_orders o
              JOIN paper_positions p ON p.position_id = o.position_id
             WHERE o.portfolio_id = ?
               AND o.action = 'EXIT'
               AND o.intent IN ({placeholders})
               AND o.filled_at >= ?
             ORDER BY o.filled_at DESC
            """,
            (portfolio_id, *PRICE_EXIT_INTENTS, since),
        ).fetchall()

    out: dict[str, dict] = {}
    for r in rows:
        t = r["ticker"].upper()
        if t not in out:
            out[t] = dict(r)
            out[t]["exit_count"] = 0
        out[t]["exit_count"] += 1
    return out


def recent_orders(
    portfolio_id: str, *, limit: int = 100, ticker: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    sql = "SELECT * FROM paper_orders WHERE portfolio_id = ?"
    params: list[Any] = [portfolio_id]
    if ticker:
        sql += " AND ticker = ?"
        params.append(ticker.upper())
    sql += " ORDER BY filled_at DESC LIMIT ?"
    params.append(limit)
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── Decisions ─────────────────────────────────────────────────────────


def new_decision_id() -> str:
    return _new_id("pdec")


def new_tick_id() -> str:
    return _new_id("ptick")


def insert_decision(conn: sqlite3.Connection, d: Decision) -> None:
    conn.execute(
        """
        INSERT INTO paper_decisions
            (decision_id, portfolio_id, tick_id, decided_at, ticker, action,
             intent, side, rank, rank_prev, target_weight_pct,
             current_weight_pct, notional, executed, blocker, headline,
             rationale_json, position_id, order_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            d.decision_id, d.portfolio_id, d.tick_id, d.decided_at, d.ticker,
            str(d.action), str(d.intent), d.side, d.rank, d.rank_prev,
            d.target_weight_pct, d.current_weight_pct, d.notional,
            1 if d.executed else 0, str(d.blocker) if d.blocker else None,
            d.headline, json.dumps(d.rationale or {}), d.position_id, d.order_id,
        ),
    )


def recent_decisions(
    portfolio_id: str, *, limit: int = 200, ticker: Optional[str] = None,
    action: Optional[str] = None, executed_only: bool = False,
    db_path: Optional[Path] = None,
) -> list[dict]:
    sql = "SELECT * FROM paper_decisions WHERE portfolio_id = ?"
    params: list[Any] = [portfolio_id]
    if ticker:
        sql += " AND ticker = ?"
        params.append(ticker.upper())
    if action:
        sql += " AND action = ?"
        params.append(action.upper())
    if executed_only:
        sql += " AND executed = 1"
    sql += " ORDER BY decided_at DESC, rowid DESC LIMIT ?"
    params.append(limit)
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["rationale"] = _loads(d.pop("rationale_json", None)) or {}
        d["executed"] = bool(d.get("executed"))
        out.append(d)
    return out


# ── Equity snapshots ──────────────────────────────────────────────────


def insert_equity_snapshot(
    conn: sqlite3.Connection,
    *,
    portfolio_id: str,
    tick_id: Optional[str],
    equity: float,
    cash: float,
    deployed: float,
    open_positions: int,
    unrealized_pnl: float,
    realized_pnl_to_date: float,
    positions: list[dict],
    taken_at: Optional[str] = None,
) -> str:
    snapshot_id = _new_id("psnap")
    conn.execute(
        """
        INSERT INTO paper_equity_snapshots
            (snapshot_id, portfolio_id, tick_id, taken_at, equity, cash,
             deployed, deployed_pct, cash_pct, open_positions, unrealized_pnl,
             realized_pnl_to_date, positions_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id, portfolio_id, tick_id, taken_at or _now(),
            round(equity, 4), round(cash, 4), round(deployed, 4),
            round(deployed / equity, 6) if equity > 0 else 0.0,
            round(cash / equity, 6) if equity > 0 else 1.0,
            open_positions, round(unrealized_pnl, 4), round(realized_pnl_to_date, 4),
            json.dumps(positions),
        ),
    )
    return snapshot_id


def insert_position_snapshot(conn: sqlite3.Connection, row: dict) -> None:
    """One open position, one tick, every input. See the schema comment."""
    cols = (
        "snapshot_id", "portfolio_id", "position_id", "tick_id", "taken_at", "ticker",
        "side", "days_held", "mark", "unrealized_r", "unrealized_pct", "weight_pct",
        "rank", "score", "read_side", "signal_direction", "signal_confidence", "signal_n",
        "support", "support_structure", "support_voices", "support_regime",
        "support_price", "regime_label", "macro_alignment", "exit_signal",
        "exit_signal_streak", "expected_hold_days", "exit_path",
    )
    row = {**row, "snapshot_id": _new_id("ppsnap")}
    conn.execute(
        f"INSERT INTO paper_position_snapshots ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        tuple(row.get(c) for c in cols),
    )


def position_snapshots(
    portfolio_id: str, *, position_id: Optional[str] = None,
    since: Optional[str] = None, db_path: Optional[Path] = None,
) -> list[dict]:
    sql = "SELECT * FROM paper_position_snapshots WHERE portfolio_id = ?"
    params: list[Any] = [portfolio_id]
    if position_id:
        sql += " AND position_id = ?"; params.append(position_id)
    if since:
        sql += " AND taken_at >= ?"; params.append(since)
    sql += " ORDER BY position_id, taken_at"
    with connect(db_path, readonly=True) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def equity_curve(
    portfolio_id: str, *, limit: int = 500, db_path: Optional[Path] = None
) -> list[dict]:
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT snapshot_id, taken_at, equity, cash, deployed, deployed_pct,
                   cash_pct, open_positions, unrealized_pnl, realized_pnl_to_date
              FROM paper_equity_snapshots
             WHERE portfolio_id = ?
             ORDER BY taken_at DESC
             LIMIT ?
            """,
            (portfolio_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def realized_to_date(portfolio_id: str, *, db_path: Optional[Path] = None) -> float:
    with connect(db_path, readonly=True) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM paper_orders WHERE portfolio_id = ?",
            (portfolio_id,),
        ).fetchone()
    return float(row[0] or 0.0)


# ── Reconciliation ────────────────────────────────────────────────────


def reconcile(
    portfolio_id: str, *, db_path: Optional[Path] = None, tolerance: float = 0.01
) -> dict:
    """Rebuild cash from the fill log and compare against the stored value.

    A mismatch means an order was written without its cash effect (or the
    reverse) — the engine treats that as a hard failure and rolls the
    tick back rather than compounding a phantom balance.
    """
    with connect(db_path, readonly=True) as conn:
        pf = conn.execute(
            "SELECT starting_equity, cash FROM paper_portfolios WHERE portfolio_id = ?",
            (portfolio_id,),
        ).fetchone()
        if not pf:
            return {"ok": False, "reason": "no such portfolio"}
        flows = conn.execute(
            "SELECT COALESCE(SUM(cash_delta), 0) FROM paper_orders WHERE portfolio_id = ?",
            (portfolio_id,),
        ).fetchone()
    expected = float(pf["starting_equity"]) + float(flows[0] or 0.0)
    actual = float(pf["cash"])
    drift = actual - expected
    return {
        "ok": abs(drift) <= tolerance,
        "expected_cash": round(expected, 4),
        "actual_cash": round(actual, 4),
        "drift": round(drift, 6),
    }


__all__ = [
    "connect", "create_portfolio", "get_portfolio", "get_or_create_portfolio",
    "list_portfolios", "update_portfolio_cash", "update_portfolio_mandate", "load_positions", "get_position",
    "insert_position", "update_position", "new_position_id", "insert_order",
    "new_order_id", "recent_orders", "recent_price_exits", "insert_decision", "new_decision_id",
    "new_tick_id", "recent_decisions", "insert_equity_snapshot", "equity_curve",
    "insert_position_snapshot", "position_snapshots",
    "realized_to_date", "reconcile",
]
