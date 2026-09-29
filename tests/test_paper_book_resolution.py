"""Which book does a caller get when it does not name one?

This is a regression test for a live incident. `store.get_portfolio(None)`
used to mean "the oldest active book", which held for as long as the desk
ran one. When the Stock Unlocked book was replayed over a year of history
the replay backdated its `created_at`, so it became the oldest active row
— and `paper_trading_tick.py`, which omitted the id, spent six days
filling desk-score candidates into the copy book under the copy book's
mandate while the signal book sat frozen. Resolving by NAME is what every
caller already assumed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from macro_positioning.db.connect import write_connection
from macro_positioning.db.schema import initialize_database
from macro_positioning.paper import store


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "paper.db"
    initialize_database(p)
    return p


def _backdate(db: Path, portfolio_id: str, when: str) -> None:
    conn = write_connection(db)
    try:
        conn.execute("UPDATE paper_portfolios SET created_at=? WHERE portfolio_id=?",
                     (when, portfolio_id))
        conn.commit()
    finally:
        conn.close()


def test_default_is_the_signal_book_even_when_another_book_is_older(db: Path):
    signal = store.create_portfolio(db_path=db)            # DEFAULT_BOOK_NAME
    copy = store.create_portfolio(name="Stock Unlocked Book", db_path=db)
    _backdate(db, copy.portfolio_id, "2020-01-01T00:00:00+00:00")

    got = store.get_portfolio(db_path=db)
    assert got is not None
    assert got.portfolio_id == signal.portfolio_id
    assert got.name == store.DEFAULT_BOOK_NAME


def test_an_explicit_id_still_wins(db: Path):
    store.create_portfolio(db_path=db)
    copy = store.create_portfolio(name="Stock Unlocked Book", db_path=db)
    assert store.get_portfolio(copy.portfolio_id, db_path=db).portfolio_id == copy.portfolio_id


def test_a_renamed_single_book_still_resolves_by_age(db: Path):
    """Back-compat: a DB whose only book was renamed has no Signal Book to
    find, and must not start returning None."""
    only = store.create_portfolio(name="The Desk", db_path=db)
    assert store.get_portfolio(db_path=db).portfolio_id == only.portfolio_id


def test_a_closed_signal_book_does_not_shadow_the_live_one(db: Path):
    dead = store.create_portfolio(db_path=db)
    conn = write_connection(db)
    try:
        conn.execute("UPDATE paper_portfolios SET status='closed' WHERE portfolio_id=?",
                     (dead.portfolio_id,))
        conn.commit()
    finally:
        conn.close()
    live = store.create_portfolio(db_path=db)
    assert store.get_portfolio(db_path=db).portfolio_id == live.portfolio_id


# ── Retiring a book must flatten it ───────────────────────────────────
#
# Second live incident, 2026-09-25: `_retire_existing_book()` flipped the
# Stock Unlocked book to status='closed' and left its 12 positions open.
# Nothing ticked them again, so META's 25% runner sat past its 758.68
# target (hit 2026-09-23) unmanaged, and three retired books between them
# stranded 25 open positions that still counted as live risk.

def _open_position(db: Path, portfolio_id: str, ticker: str, qty: float,
                   avg_price: float, mark: float) -> str:
    from macro_positioning.paper.models import Position
    pid = store.new_position_id()
    conn = write_connection(db)
    try:
        store.insert_position(conn, Position(
            position_id=pid, portfolio_id=portfolio_id, ticker=ticker, side="LONG",
            qty=qty, avg_price=avg_price, opened_at="2026-09-19T10:15:07+00:00",
            last_mark=mark, last_mark_at="2026-09-22T12:38:56+00:00",
        ))
        conn.commit()
    finally:
        conn.close()
    return pid


def test_flatten_closes_every_open_position_and_books_the_cash(db: Path):
    pf = store.create_portfolio(name="Stock Unlocked Book", db_path=db)
    _open_position(db, pf.portfolio_id, "META", 0.9448, 666.75, 741.25)
    _open_position(db, pf.portfolio_id, "AAPL", 7.4858, 336.63, 338.98)

    fills = store.flatten_portfolio(pf.portfolio_id, reason="superseded", db_path=db)

    assert len(fills) == 2
    assert store.load_positions(pf.portfolio_id, status="open", db_path=db) == []
    # META marked out at 741.25 against a 666.75 basis
    meta = next(f for f in fills if f.ticker == "META")
    assert meta.realized_pnl == pytest.approx(0.9448 * (741.25 - 666.75), rel=1e-6)
    # and the fill log still rebuilds cash — the flatten is not a side door
    assert store.reconcile(pf.portfolio_id, db_path=db)["ok"]


def test_flatten_prefers_a_live_mark_over_the_stale_one(db: Path):
    """The stranded runner's last_mark was 3 days old. If a feed is
    available the flatten must use it, not the frozen number."""
    pf = store.create_portfolio(name="Stock Unlocked Book", db_path=db)
    _open_position(db, pf.portfolio_id, "META", 1.0, 666.75, 741.25)

    fills = store.flatten_portfolio(
        pf.portfolio_id, reason="superseded",
        mark_fn=lambda t: 777.59, db_path=db,
    )
    assert fills[0].price == pytest.approx(777.59)
    assert fills[0].realized_pnl == pytest.approx(777.59 - 666.75)


def test_flatten_falls_back_when_the_feed_throws(db: Path):
    pf = store.create_portfolio(name="Stock Unlocked Book", db_path=db)
    _open_position(db, pf.portfolio_id, "META", 1.0, 666.75, 741.25)

    def dead_feed(_t):
        raise RuntimeError("finnhub down")

    fills = store.flatten_portfolio(
        pf.portfolio_id, reason="superseded", mark_fn=dead_feed, db_path=db,
    )
    assert fills[0].price == pytest.approx(741.25)   # the last mark, not a crash


def test_flatten_is_a_noop_on_a_book_with_nothing_open(db: Path):
    pf = store.create_portfolio(name="Stock Unlocked Book", db_path=db)
    assert store.flatten_portfolio(pf.portfolio_id, reason="x", db_path=db) == []
