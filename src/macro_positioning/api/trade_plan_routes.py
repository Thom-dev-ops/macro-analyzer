"""FastAPI routes for trade-plan capture.

Endpoints:
  POST /api/trades/{trade_id}/plan  — create the immutable entry-time plan
  GET  /api/trades/{trade_id}/plan  — fetch the plan if one exists

A plan is the audit-trail anchor for the rule-adherence score that
journal/feedback_writer writes at review-submit time. Plans are
append-only — the trade_plans table's UNIQUE(trade_id) constraint
enforces one plan per trade; re-POST returns 409.
"""

from __future__ import annotations

import sqlite3
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from macro_positioning.core.settings import settings
from macro_positioning.db.connect import write_connection
from macro_positioning.rules import repository as rrepo
from macro_positioning.rules.confluence import score_confluence
from macro_positioning.rules.portfolio import bucket_for_ticker
from macro_positioning.rules.risk import account_risk_pct


router = APIRouter(tags=["rules"])


def _open_conn() -> sqlite3.Connection:
    conn = write_connection()
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


EntryStrategy = Literal[
    "breakout_retest", "breakout_impulse", "dip_buy", "range_fade", "other"
]
SetupCategory = Literal[
    "flag", "pennant", "channel", "hs", "cup", "range", "ema", "breakout"
]


class ConfluenceSubscoresIn(BaseModel):
    pattern: int = Field(..., ge=0, le=3)
    fib: int = Field(..., ge=0, le=3)
    indicator: int = Field(..., ge=0, le=2)


class TradePlanPayload(BaseModel):
    planned_entry: float = Field(..., gt=0)
    planned_stop: float = Field(..., gt=0)
    planned_size: float = Field(..., gt=0)
    planned_tps: list[float] = Field(default_factory=list)
    planned_account_equity: Optional[float] = Field(default=None, gt=0)
    planned_setup_category: Optional[SetupCategory] = None
    planned_confluence_subscores: Optional[ConfluenceSubscoresIn] = None
    planned_entry_strategy: Optional[EntryStrategy] = "breakout_retest"
    notes: Optional[str] = None


@router.post("/api/trades/{trade_id}/plan")
def create_plan(trade_id: str, payload: TradePlanPayload) -> dict:
    # Pre-flight: trade exists, no prior plan
    conn = _open_conn()
    try:
        if rrepo.get_plan(conn, trade_id) is not None:
            raise HTTPException(status_code=409, detail="plan already exists for this trade")

        # Resolve ticker → bucket for cache + carry on the trades row
        row = conn.execute(
            """
            SELECT a.ticker
            FROM trades t
            LEFT JOIN assets a ON a.asset_id = t.asset_id
            WHERE t.trade_id = ?
            """,
            (trade_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown trade_id: {trade_id!r}")
        ticker = row[0] or ""
        bucket = bucket_for_ticker(ticker)

        # Confluence breakdown (composite + tier) for the plan row + trades cache
        confluence_total: Optional[int] = None
        pattern_s: Optional[int] = None
        fib_s: Optional[int] = None
        ind_s: Optional[int] = None
        if payload.planned_confluence_subscores is not None:
            cb = score_confluence(
                payload.planned_confluence_subscores.pattern,
                payload.planned_confluence_subscores.fib,
                payload.planned_confluence_subscores.indicator,
            )
            confluence_total = cb.total
            pattern_s = cb.pattern
            fib_s = cb.fib
            ind_s = cb.indicator

        # Risk % (only if equity supplied)
        risk_pct: Optional[float] = None
        if payload.planned_account_equity is not None:
            try:
                risk_pct = account_risk_pct(
                    payload.planned_entry,
                    payload.planned_stop,
                    payload.planned_size,
                    payload.planned_account_equity,
                )
            except ValueError:
                risk_pct = None

        plan_payload = {
            "planned_entry": payload.planned_entry,
            "planned_stop": payload.planned_stop,
            "planned_tps": payload.planned_tps,
            "planned_size": payload.planned_size,
            "planned_account_equity": payload.planned_account_equity,
            "planned_risk_pct": risk_pct,
            "planned_setup_category": payload.planned_setup_category,
            "planned_confluence_score": confluence_total,
            "planned_pattern_subscore": pattern_s,
            "planned_fib_subscore": fib_s,
            "planned_indicator_subscore": ind_s,
            "planned_correlated_bucket": bucket,
            "planned_entry_strategy": payload.planned_entry_strategy,
            "notes": payload.notes,
        }

        try:
            plan_id = rrepo.save_plan(conn, trade_id, plan_payload)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except sqlite3.IntegrityError:
            # Race: another writer beat us to the UNIQUE constraint
            raise HTTPException(status_code=409, detail="plan already exists for this trade")

        # Cache the rule-derived columns on the trades row so the
        # dashboard panels don't have to JOIN trade_plans every query.
        rrepo.hydrate_trade_rule_columns(
            conn,
            trade_id,
            setup_category=payload.planned_setup_category,
            confluence_score=confluence_total,
            pattern_subscore=pattern_s,
            fib_subscore=fib_s,
            indicator_subscore=ind_s,
            account_risk_pct=risk_pct,
            correlated_bucket=bucket,
            entry_followed_retest=(
                1 if payload.planned_entry_strategy == "breakout_retest"
                else 0 if payload.planned_entry_strategy is not None
                else None
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "plan_id": plan_id,
        "trade_id": trade_id,
        "planned_risk_pct": risk_pct,
        "planned_confluence_score": confluence_total,
        "planned_correlated_bucket": bucket,
    }


@router.get("/api/trades/{trade_id}/plan")
def get_plan(trade_id: str) -> dict:
    conn = _open_conn()
    try:
        plan = rrepo.get_plan(conn, trade_id)
    finally:
        conn.close()
    if plan is None:
        raise HTTPException(status_code=404, detail="no plan recorded for this trade")
    return plan
