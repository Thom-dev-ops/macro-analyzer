"""FastAPI sub-router for the manual input layer.

Endpoints (all under /api/manual):
  POST /preview  — JSON payload, returns suggestions, no persistence.
  POST /ingest   — multipart (file optional + payload JSON), persists.
  GET  /inputs   — recent submissions for the /inbox history view.
  GET  /authors  — known authors for the SPA autocomplete.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import ValidationError

from macro_positioning.manual import processor
from macro_positioning.manual.authors import list_authors
from macro_positioning.manual.models import (
    AuthorRef,
    IngestResponse,
    ManualInputPayload,
    PreviewResponse,
)


router = APIRouter(prefix="/api/manual", tags=["manual-input"])


# ── Preview ──────────────────────────────────────────────────────────────────


@router.post("/preview", response_model=PreviewResponse)
async def preview(payload: ManualInputPayload) -> PreviewResponse:
    """JSON-only preview path (no image OCR). Kept for back-compat with the
    earlier client that posted application/json directly."""
    return processor.preview(payload)


@router.post("/preview/multipart", response_model=PreviewResponse)
async def preview_multipart(
    payload: str = Form(..., description="JSON-serialized ManualInputPayload"),
    files: Optional[list[UploadFile]] = File(None),
) -> PreviewResponse:
    """Multipart preview that runs heuristic OCR over uploaded image bytes.

    Files are saved under uploads/charts/YYYY-MM/{uuid}.{ext} so a later
    /ingest call can reference them — the SPA reuses the same paths to
    avoid re-uploading on save. To keep /preview side-effect light, OCR
    errors never bubble; missing fields stay null.
    """
    try:
        parsed = ManualInputPayload.model_validate_json(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())

    saved_paths: list[str] = []
    for upload in files or []:
        if not upload or not upload.filename:
            continue
        body = await upload.read()
        if not body:
            # Empty upload is a no-op for preview (don't 400 — user is mid-edit).
            continue
        saved_paths.append(processor.save_attachment(body, upload.filename))

    if saved_paths:
        parsed.attachment_paths = saved_paths
        parsed.attachment_path = saved_paths[0]

    response = processor.preview(parsed, image_paths=saved_paths)
    # Echo the saved paths so the client can attach them to the eventual
    # /ingest call without re-uploading the same bytes.
    if saved_paths:
        if response.image_suggestions is None:
            response.image_suggestions = {}
        response.image_suggestions["saved_paths"] = saved_paths
    return response


# ── Ingest (optional file + JSON payload) ────────────────────────────────────


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    payload: str = Form(..., description="JSON-serialized ManualInputPayload"),
    files: Optional[list[UploadFile]] = File(None),
    # Back-compat: clients still using the single-file form keep working.
    file: Optional[UploadFile] = File(None),
    analyze: bool = Form(default=False, description="Run Claude vision inline before responding."),
) -> IngestResponse:
    try:
        parsed = ManualInputPayload.model_validate_json(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())

    uploaded: list[UploadFile] = []
    if files:
        uploaded.extend(f for f in files if f and f.filename)
    if file is not None and file.filename:
        uploaded.append(file)

    saved_paths: list[str] = []
    for upload in uploaded:
        body = await upload.read()
        if not body:
            raise HTTPException(
                status_code=400,
                detail=f"Uploaded file '{upload.filename}' is empty.",
            )
        saved_paths.append(processor.save_attachment(body, upload.filename))

    if saved_paths:
        parsed.attachment_paths = saved_paths
        parsed.attachment_path = saved_paths[0]

    response = processor.ingest(parsed)

    # Optional inline drain — caller passes analyze=true to wait for Claude
    # vision. Adds ~20s/image latency. The endpoint stays the same; just
    # blocks until the extracted_features_json is written.
    if analyze and response.pending_vision:
        try:
            from macro_positioning.manual.vision_drainer import drain
            drain(document_id=response.document_id)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("inline drain failed")

    return response


# ── Listings ─────────────────────────────────────────────────────────────────


@router.get("/inputs")
def recent_inputs(limit: int = 50) -> list[dict]:
    limit = max(1, min(200, int(limit)))
    return processor.list_recent_inputs(limit=limit)


@router.get("/authors")
def authors(limit: int = 200) -> list[dict]:
    limit = max(1, min(500, int(limit)))
    return list_authors(limit=limit)


# ── Author management (explicit create) ──────────────────────────────────────


@router.get("/themes/trusted")
def themes_trusted(min_trust: float = 1.15, window_days: int = 90) -> dict:
    """Theme breakdown for every author with trust_weight ≥ min_trust.

    Drives the I3/S6 "Trusted sources" panel (now on /streams). Default
    1.15 includes both T0 and T1 sources — the Telegram-poller channels
    (Feather Hands, Gem Hunters, OG Whales, Wolf Pack, Ari Gold) plus the
    seeded macro sources. Re-runs are cheap; safe to poll.
    """
    from macro_positioning.learning.source_themes import trusted_source_themes
    rows = trusted_source_themes(min_trust=min_trust, window_days=window_days)
    return {
        "min_trust": min_trust,
        "window_days": window_days,
        "authors": [r.to_dict() for r in rows],
    }


# ── Extraction-validation review (temporary tooling) ────────────────────────
# Backs web/verify.html: lets the user eyeball the new vision extraction
# (call_type / direction / targets) against each chart image and mark it
# correct/wrong. Reads the JSONL the validation script streams to /tmp and
# persists verdicts alongside it. Pure file IO — no DB.
from pathlib import Path as _Path  # noqa: E402

_VAL_RESULTS = _Path("/tmp/val50_results.jsonl")
_VAL_VERDICTS = _Path("/tmp/val50_verdicts.json")


@router.get("/verify/list")
def verify_list() -> dict:
    """Return the streamed extraction results + any saved verdicts."""
    results = []
    if _VAL_RESULTS.exists():
        for line in _VAL_RESULTS.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            # /uploads is mounted at the project root's uploads/ dir.
            p = r.get("path") or ""
            r["image_url"] = "/" + p if p.startswith("uploads/") else p
            results.append(r)
    verdicts = {}
    if _VAL_VERDICTS.exists():
        try:
            verdicts = json.loads(_VAL_VERDICTS.read_text())
        except json.JSONDecodeError:
            verdicts = {}
    return {"results": results, "verdicts": verdicts, "n": len(results)}


@router.post("/verify/mark")
def verify_mark(payload: dict) -> dict:
    """Save one verdict + structured correction.

    payload: {path, verdict:'ok'|'wrong', notes?, correction?}
    correction is the ground-truth label the user typed (call_type, ticker,
    direction, is_forward_looking, final_target) — pre-filled with the model's
    extraction in the UI so they only fix what's wrong. This builds the
    answer-key dataset used to score the prompt and refine it.
    """
    path = payload.get("path")
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    verdicts = {}
    if _VAL_VERDICTS.exists():
        try:
            verdicts = json.loads(_VAL_VERDICTS.read_text())
        except json.JSONDecodeError:
            verdicts = {}
    verdicts[path] = {
        "verdict": payload.get("verdict"),
        "notes": payload.get("notes", ""),
        "correction": payload.get("correction") or {},
    }
    _VAL_VERDICTS.write_text(json.dumps(verdicts, indent=2))
    # PERSIST a full training label (append-only, never wiped) — this is the
    # labeled dataset for the future custom-trained extraction model.
    _append_training_label(path, verdicts[path])
    n_ok = sum(1 for v in verdicts.values() if v.get("verdict") == "ok")
    n_wrong = sum(1 for v in verdicts.values() if v.get("verdict") == "wrong")
    return {"saved": True, "n_ok": n_ok, "n_wrong": n_wrong, "n_total": len(verdicts)}


# ── Training-label catalog (for the future custom-trained model) ────────────
# Every verify verdict is appended here as a complete record: the chart image,
# the paired caption, the model's extraction, and the human ground-truth.
# Append-only JSONL; dedup at training time by taking the latest per image.
_TRAIN_LABELS = _Path("training_corpus/extraction_labels.jsonl")


def _append_training_label(path: str, verdict: dict) -> None:
    """Join the model extraction + caption + human verdict into one training
    record and append to the corpus. Best-effort; never breaks the mark."""
    import hashlib
    import sqlite3
    from datetime import datetime, timezone
    from macro_positioning.core.settings import settings as _s
    from macro_positioning.db.connect import write_connection

    try:
        with write_connection(_s.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT document_id, author, raw_text, extracted_features_json, "
                "attachment_paths_json FROM documents WHERE attachment_path=? LIMIT 1",
                (path,),
            ).fetchone()
        model_out = {}
        if row and row["extracted_features_json"]:
            try:
                model_out = json.loads(row["extracted_features_json"])
                if isinstance(model_out, list):
                    model_out = model_out[0] if model_out else {}
            except json.JSONDecodeError:
                model_out = {}
        # Ground truth = model output, overridden by any non-empty correction.
        corr = verdict.get("correction") or {}
        gt = {k: model_out.get(k) for k in
              ("call_type", "ticker", "bias", "is_forward_looking", "trade_stage")}
        gt["direction"] = (model_out.get("setups") or model_out.get("entries") or [{}])[0].get("direction") if isinstance(model_out.get("setups") or model_out.get("entries"), list) else None
        for k, v in corr.items():
            if v not in (None, ""):
                gt[k] = v
        # image sha for stable dedup/identity
        sha = None
        try:
            fp = _s.base_dir / path
            if fp.exists():
                sha = hashlib.sha256(fp.read_bytes()).hexdigest()
        except Exception:  # noqa: BLE001
            pass
        rec = {
            "image_path": path,
            "image_sha256": sha,
            "document_id": row["document_id"] if row else None,
            "author": row["author"] if row else None,
            "caption": (row["raw_text"] if row else "") or "",
            "model_output": model_out,
            "verdict": verdict.get("verdict"),
            "ground_truth": gt,
            "notes": verdict.get("notes", ""),
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        _TRAIN_LABELS.parent.mkdir(parents=True, exist_ok=True)
        with _TRAIN_LABELS.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 — labeling must never break the UI
        pass


@router.get("/accuracy/sources")
def accuracy_sources(window_days: Optional[int] = None) -> dict:
    """Per-source call-accuracy rollup from the backtester.

    Each source → win_rate (directional), avg_return_pct, setup_win_rate
    (target-before-stop), avg_r_planned, n_priceable / n_unpriceable. Powers
    the accuracy badge on the S6 trusted-source cards. Read-only; reflects
    whatever the last `backtest_calls()` run persisted to call_outcomes.
    """
    from macro_positioning.learning.call_accuracy import source_accuracy
    return {
        "window_days": window_days,
        "sources": source_accuracy(window_days=window_days),
    }


@router.get("/themes/family/{parent_channel}")
def themes_family(parent_channel: str, window_days: int = 90) -> dict:
    """Roll-up theme view for a community (e.g. "Feather Hands") that
    unions all member authors. Surfaces the community's recurring tickers,
    bias mix, and dominant setup types in one panel."""
    from macro_positioning.learning.source_themes import family_summary
    return family_summary(parent_channel=parent_channel, window_days=window_days)


@router.get("/themes/author/{author_id}/ticker/{ticker}")
def themes_author_ticker(author_id: str, ticker: str, window_days: int = 365) -> dict:
    """Drill-down: every drop where ``author_id`` mentioned ``ticker``.
    Powers the I3 "click a chip to see why" expansion."""
    from macro_positioning.learning.source_themes import author_ticker_drops
    drops = author_ticker_drops(author_id, ticker, window_days=window_days)
    return {
        "author_id": author_id,
        "ticker": ticker,
        "n": len(drops),
        "window_days": window_days,
        "drops": drops,
    }


@router.post("/vision/drain")
def drain_pending_vision(limit: int = 25, document_id: Optional[str] = None) -> dict:
    """Process pending_vision documents — calls Claude on each attachment,
    writes the extracted TradeRecord into extracted_features_json, clears
    the flag. Idempotent: hash-cached results return instantly. Safe to
    poll from the SPA or run on a cron."""
    from macro_positioning.manual.vision_drainer import drain
    summary = drain(limit=max(1, min(200, limit)), document_id=document_id)
    return summary.to_dict()


@router.patch("/inputs/{document_id}/author")
def reassign_author(document_id: str, ref: AuthorRef) -> dict:
    """Reassign a single manual-input document to a different author.

    Updates documents.author, documents.author_id, documents.source_id,
    and the channel/parent_channel inside user_metadata_json so the SPA
    breadcrumb reads correctly on next refresh. Idempotent. Upserts the
    target author so re-assigning to a brand-new name auto-creates them.
    """
    if not (ref.display_name or "").strip():
        raise HTTPException(status_code=400, detail="display_name is required")

    import json as _json
    import sqlite3 as _sql
    from macro_positioning.manual.authors import upsert_author
    from macro_positioning.core.settings import settings as _settings

    # Resolve target author + ensure they exist
    new_author_id = upsert_author(ref)

    # Pull parent_channel from the seeded row so the breadcrumb survives
    with _sql.connect(_settings.sqlite_path) as conn:
        row = conn.execute(
            "SELECT parent_channel FROM input_authors WHERE author_id=?",
            (new_author_id,),
        ).fetchone()
        parent_channel = row[0] if row else None

        # Pull current user_metadata so we patch in place (don't clobber)
        meta_row = conn.execute(
            "SELECT user_metadata_json FROM documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
        if meta_row is None:
            raise HTTPException(status_code=404, detail="document not found")
        try:
            meta = _json.loads(meta_row[0] or "{}")
        except _json.JSONDecodeError:
            meta = {}
        meta["channel"] = ref.channel
        meta["channel_type"] = ref.channel_type
        meta["parent_channel"] = parent_channel

        conn.execute(
            """
            UPDATE documents
            SET author_id=?, author=?, source_id=?, user_metadata_json=?
            WHERE document_id=?
            """,
            (
                new_author_id,
                ref.display_name,
                f"manual:{new_author_id}",
                _json.dumps(meta),
                document_id,
            ),
        )
        conn.commit()

    return {
        "document_id": document_id,
        "author_id": new_author_id,
        "display_name": ref.display_name,
        "channel": ref.channel,
        "channel_type": ref.channel_type,
        "parent_channel": parent_channel,
    }


@router.post("/authors")
def create_author(ref: AuthorRef) -> dict:
    """Create (or touch) a known-source author without needing a full drop.

    Lets the SPA pre-create a new author from the picklist UI so it shows
    up as a pill for future drops. Idempotent — re-posting the same
    name+channel returns the existing author_id and refreshes last_seen_at.
    """
    from macro_positioning.manual.authors import upsert_author
    if not (ref.display_name or "").strip():
        raise HTTPException(status_code=400, detail="display_name is required")
    author_id = upsert_author(ref)
    return {"author_id": author_id, "display_name": ref.display_name,
            "channel": ref.channel, "channel_type": ref.channel_type}
