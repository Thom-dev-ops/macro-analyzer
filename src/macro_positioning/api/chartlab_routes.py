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

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile

from macro_positioning.chartlab import store as chartlab_store

router = APIRouter(prefix="/api/chartlab", tags=["chartlab"])


@router.get("/charts")
def list_charts(
    limit: int = Query(20, ge=1, le=200),
    ticker: Optional[str] = Query(None),
) -> dict:
    """Recent desk chart drops, newest first."""
    import json

    rows = chartlab_store.list_charts(limit=limit, ticker=ticker)
    out = []
    for row in rows:
        path = row.get("attachment_path") or ""
        try:
            declared = (json.loads(row.get("user_metadata_json") or "{}") or {}).get(
                "user"
            ) or {}
        except (TypeError, ValueError):
            declared = {}
        out.append(
            {
                "document_id": row["document_id"],
                "title": row.get("title"),
                "caption": row.get("raw_text") or "",
                "ingested_at": row.get("ingested_at"),
                "image_url": ("/" + path) if path.startswith("uploads/") else None,
                "has_read": bool(row.get("extracted_features_json")),
                "ticker": declared.get("ticker") or "",
                "timeframe": declared.get("timeframe") or "",
                "asset_class": declared.get("asset_class") or "",
                "note": declared.get("note") or "",
            }
        )
    return {"charts": out}


@router.post("/drop")
async def drop_chart(
    file: Optional[UploadFile] = File(None),
    files: list[UploadFile] = File(default_factory=list),
    ticker: Optional[str] = Form(None),
    timeframe: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
) -> dict:
    """Park one or many uploaded charts in the desk namespace.

    Deliberately separate from `/api/manual/ingest`: that path files a
    chart as a manual drop, this one files it as a DESK read, which is
    what keeps the operator's own opinion out of trusted-voice
    consensus downstream.

    A screenshot batch is the normal case — six charts off one screen,
    each about a different ticker — so the metadata fields here are only
    the shared hint for the whole batch. Per-chart ticker, timeframe and
    class are declared afterwards through PATCH /chart/{id}, which is
    the only sane way round when the files arrive together.

    One bad file in a batch does not sink the good ones: it comes back
    named in `errors`. Everything failing is a 400, which is also what a
    single bad upload gets.
    """
    import tempfile
    from pathlib import Path

    uploads = [f for f in ([file] if file is not None else []) + list(files) if f]
    if not uploads:
        raise HTTPException(status_code=400, detail="no file uploaded")

    drops: list[dict] = []
    errors: list[dict] = []
    for upload in uploads:
        name = upload.filename or "chart.png"
        raw = await upload.read()
        if not raw:
            errors.append({"filename": name, "error": "empty upload"})
            continue

        suffix = Path(name).suffix or ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)
        # NamedTemporaryFile invents its own stem, so hand add_chart the
        # real filename — it is what the extension check reads.
        named = tmp_path.with_name(Path(name).name)
        try:
            tmp_path.rename(named)
        except OSError:
            named = tmp_path

        try:
            drop = chartlab_store.add_chart(
                named, ticker=ticker, timeframe=timeframe, note=note or ""
            )
        except (FileNotFoundError, ValueError) as exc:
            errors.append({"filename": name, "error": str(exc)})
            continue
        finally:
            named.unlink(missing_ok=True)

        drops.append(
            {
                "document_id": drop.document_id,
                "ticker": drop.ticker,
                "source_id": drop.source_id,
                "image_url": "/" + drop.attachment_path,
                "filename": name,
            }
        )

    if not drops:
        detail = errors[0]["error"] if len(errors) == 1 else "; ".join(
            f"{e['filename']}: {e['error']}" for e in errors
        )
        raise HTTPException(status_code=400, detail=detail)

    # The first drop's fields stay hoisted for single-file callers.
    return {**drops[0], "drops": drops, "errors": errors}


@router.patch("/chart/{document_id}")
def patch_chart(
    document_id: str,
    ticker: Optional[str] = Body(None),
    timeframe: Optional[str] = Body(None),
    note: Optional[str] = Body(None),
    asset_class: Optional[str] = Body(None),
) -> dict:
    """Re-declare a parked chart's ticker, timeframe, class and note."""
    try:
        return chartlab_store.update_chart_meta(
            document_id,
            ticker=ticker,
            timeframe=timeframe,
            note=note,
            asset_class=asset_class,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete("/chart/{document_id}")
def delete_chart(document_id: str) -> dict:
    """Bin a misadded chart — the row, its signals, and its image."""
    try:
        return chartlab_store.delete_chart(document_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/vocab")
def vocab() -> dict:
    """Picklist values for the drop form."""
    return chartlab_store.vocabulary()


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
