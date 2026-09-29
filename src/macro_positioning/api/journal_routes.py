"""FastAPI routes for the journal feedback loop.

Endpoints:
  GET  /api/reviews/pending           — pending closed-trade queue
  GET  /api/reviews/recent            — lessons library feed
  GET  /api/reviews/{trade_id}        — fetch a saved review (or 404)
  POST /api/reviews/{trade_id}        — submit a 7-question review
  POST /api/integration/trade-close   — webhook from tactical executor
"""

from __future__ import annotations

import sqlite3
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from macro_positioning.core.settings import settings
from macro_positioning.db.connect import write_connection
from macro_positioning.journal import feedback_writer, repository, webhook


router = APIRouter(tags=["journal"])


# ---------------------------------------------------------------------------
# Connection helper — open per-request, WAL means concurrent readers OK.
# Mirrors the manual_input.py module-level _DB convention.
# ---------------------------------------------------------------------------


def _open_conn() -> sqlite3.Connection:
    conn = write_connection()
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


ThesisValidity = Literal[
    "fully_right",
    "right_outcome_wrong_reason",
    "right_thesis_wrong_outcome",
    "fully_wrong",
]
SetupScoreHindsight = Literal["over", "right", "under"]
SurpriseFactor = Literal["macro", "sector", "liquidity", "idiosyncratic", "none"]
WouldRetake = Literal["yes", "no", "modified"]


class ExecutionScores(BaseModel):
    entry: int = Field(..., ge=1, le=5)
    stop: int = Field(..., ge=1, le=5)
    sizing: int = Field(..., ge=1, le=5)
    exit: int = Field(..., ge=1, le=5)


class ReviewSubmission(BaseModel):
    thesis_validity: ThesisValidity
    sources_credited: list[str] = Field(default_factory=list)
    execution_scores: ExecutionScores
    setup_score_hindsight: SetupScoreHindsight
    surprise_factor: list[SurpriseFactor] = Field(default_factory=list)
    surprise_note: Optional[str] = None
    lesson: str = Field(..., min_length=1, max_length=240)
    would_retake: WouldRetake
    free_form_notes: Optional[str] = None

    @field_validator("lesson")
    @classmethod
    def _strip_lesson(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("lesson cannot be blank")
        return v


class TradeCloseEvent(BaseModel):
    trade_id: str = Field(..., min_length=1)
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    execution_notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


@router.get("/api/reviews/pending")
def pending_reviews() -> list[dict]:
    conn = _open_conn()
    try:
        return repository.list_pending(conn)
    finally:
        conn.close()


@router.get("/api/reviews/recent")
def recent_reviews(
    limit: int = Query(default=20, ge=1, le=200),
    ticker: Optional[str] = None,
    thesis_validity: Optional[ThesisValidity] = None,
) -> list[dict]:
    conn = _open_conn()
    try:
        return repository.recent_reviews(
            conn,
            limit=limit,
            ticker=ticker,
            thesis_validity=thesis_validity,
        )
    finally:
        conn.close()


@router.get("/api/reviews/{trade_id}")
def get_review(trade_id: str) -> dict:
    conn = _open_conn()
    try:
        review = repository.get_review(conn, trade_id)
    finally:
        conn.close()
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    return review


@router.post("/api/reviews/{trade_id}")
def submit_review(trade_id: str, payload: ReviewSubmission) -> dict:
    conn = _open_conn()
    try:
        review_dict = payload.model_dump()
        # Pydantic gives us a nested ExecutionScores model_dump → already a dict.
        try:
            review_id = repository.insert_review(conn, trade_id, review_dict)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        feedback = feedback_writer.apply_review_feedback(
            conn, trade_id, review_dict
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "review_id": review_id,
        "trade_id": trade_id,
        "review_status": repository.REVIEWED,
        "feedback_summary": feedback,
    }


# ---------------------------------------------------------------------------
# Integration webhook
# ---------------------------------------------------------------------------


@router.post("/api/integration/trade-close")
def trade_close(payload: TradeCloseEvent) -> dict:
    conn = _open_conn()
    try:
        try:
            result = webhook.receive_close_event(
                conn,
                trade_id=payload.trade_id,
                exit_date=payload.exit_date,
                exit_price=payload.exit_price,
                pnl=payload.pnl,
                pnl_percent=payload.pnl_percent,
                execution_notes=payload.execution_notes,
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        conn.commit()
    finally:
        conn.close()
    return result
