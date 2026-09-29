"""FastAPI routes for the trading-rule framework.

Endpoints (v1):
  POST /api/integration/trade-check  — gate evaluator (advisory by default)

The route is a thin HTTP wrapper over `rules.gate.evaluate_trade_proposal`.
Same evaluator is importable in-process for a future native execution
layer — no FastAPI coupling in `rules/`.
"""

from __future__ import annotations

import sqlite3
from typing import Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from macro_positioning.core.settings import settings
from macro_positioning.db.connect import write_connection
from macro_positioning.rules.gate import (
    TradeProposal,
    evaluate_trade_proposal,
)


router = APIRouter(tags=["rules"])


def _open_conn() -> sqlite3.Connection:
    conn = write_connection()
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


Side = Literal["long", "short"]
Mode = Literal["advisory", "enforce"]


class ConfluenceSubscores(BaseModel):
    pattern: int = Field(..., ge=0, le=3)
    fib: int = Field(..., ge=0, le=3)
    indicator: int = Field(..., ge=0, le=2)


class TradeProposalPayload(BaseModel):
    ticker: str = Field(..., min_length=1)
    side: Side
    entry: float = Field(..., gt=0)
    stop: float = Field(..., gt=0)
    position_size: float = Field(..., gt=0)
    account_equity: float = Field(..., gt=0)
    confluence_subscores: ConfluenceSubscores
    setup_category: Optional[str] = None
    tps: list[float] = Field(default_factory=list)
    mode: Mode = "advisory"


@router.post("/api/integration/trade-check")
def trade_check(payload: TradeProposalPayload) -> dict:
    proposal = TradeProposal(
        ticker=payload.ticker,
        side=payload.side,
        entry=payload.entry,
        stop=payload.stop,
        position_size=payload.position_size,
        account_equity=payload.account_equity,
        confluence_subscores=(
            payload.confluence_subscores.pattern,
            payload.confluence_subscores.fib,
            payload.confluence_subscores.indicator,
        ),
        setup_category=payload.setup_category,
        tps=tuple(payload.tps),
    )
    conn = _open_conn()
    try:
        decision = evaluate_trade_proposal(proposal, conn, mode=payload.mode)
    finally:
        conn.close()
    return decision.as_dict()
