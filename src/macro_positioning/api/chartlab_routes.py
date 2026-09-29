"""Chart Lab HTTP surface — the browser front door onto the same free path.

Nothing here reaches a billed API. Dropping a chart writes the same
`documents` row the CLI writes, and reading it runs the same `claude -p`
subprocess (`vision_backend="cli"`) the drainer uses. The browser hop is
this server, not Anthropic's.

The one thing the browser cannot do is argue with a read. A chart read
here is the automated one; a chart read in the chart-lab chat is a
conversation. Both land in `documents.extracted_features_json`, so the
bench card renders identically either way — the card names which
produced it so the desk can tell.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from macro_positioning.chartlab import store as chartlab_store

router = APIRouter(prefix="/api/chartlab", tags=["chartlab"])


@router.get("/charts")
def list_charts(
    limit: int = Query(20, ge=1, le=200),
    ticker: Optional[str] = Query(None),
) -> dict:
    """Recent desk chart drops, newest first."""
    rows = chartlab_store.list_charts(limit=limit, ticker=ticker)
    out = []
    for row in rows:
        path = row.get("attachment_path") or ""
        out.append(
            {
                "document_id": row["document_id"],
                "title": row.get("title"),
                "caption": row.get("raw_text") or "",
                "ingested_at": row.get("ingested_at"),
                "image_url": ("/" + path) if path.startswith("uploads/") else None,
                "has_read": bool(row.get("extracted_features_json")),
            }
        )
    return {"charts": out}


@router.post("/drop")
async def drop_chart(
    file: UploadFile = File(...),
    ticker: Optional[str] = Form(None),
    timeframe: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
) -> dict:
    """Park an uploaded chart in the desk namespace.

    Deliberately separate from `/api/manual/ingest`: that path files a
    chart as a manual drop, this one files it as a DESK read, which is
    what keeps the operator's own opinion out of trusted-voice
    consensus downstream.
    """
    import tempfile
    from pathlib import Path

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty upload")

    suffix = Path(file.filename or "chart.png").suffix or ".png"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)

    try:
        drop = chartlab_store.add_chart(
            tmp_path, ticker=ticker, timeframe=timeframe, note=note or ""
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    return {
        "document_id": drop.document_id,
        "ticker": drop.ticker,
        "source_id": drop.source_id,
        "image_url": "/" + drop.attachment_path,
    }


@router.post("/read/{document_id}")
def read_chart(document_id: str) -> dict:
    """Run the free `claude -p` read on a parked chart and persist it."""
    try:
        result = chartlab_store.read_chart_auto(document_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        # A vision failure is a 502: the request was fine, the reader wasn't.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "document_id": result.document_id,
        "ticker": result.ticker,
        "setups": result.setups,
        "signals": len(result.signals),
        "signal_error": result.signal_error,
    }


@router.get("/read/{document_id}")
def get_read(document_id: str) -> dict:
    """The stored read for one chart, for the browser to show or edit."""
    import json

    row = chartlab_store.get_chart(document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such document")
    raw = row.get("extracted_features_json")
    return {
        "document_id": document_id,
        "features": json.loads(raw) if raw else None,
        "template": chartlab_store.SECTION_10_TEMPLATE,
    }


@router.put("/read/{document_id}")
def put_read(document_id: str, features: dict) -> dict:
    """Attach a read the operator (or the chat) produced by hand."""
    try:
        result = chartlab_store.write_extraction(document_id, features)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "document_id": result.document_id,
        "ticker": result.ticker,
        "setups": result.setups,
        "signals": len(result.signals),
        "signal_error": result.signal_error,
    }


@router.get("/bench/{ticker}")
def bench(
    ticker: str,
    side: Optional[str] = Query(None, pattern="^(LONG|SHORT)$"),
    days: int = Query(260, ge=30, le=2000),
    fetch: bool = Query(True, description="allow a yfinance pull when bars are missing"),
) -> dict:
    """The single-ticker setup card, as the CLI renders it."""
    from dataclasses import asdict

    from macro_positioning.chartlab.bench import build_bench

    try:
        card = build_bench(ticker, days=days, fetch=fetch, side_override=side)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(card)
