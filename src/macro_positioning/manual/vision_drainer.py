"""Pending-vision drainer — turns pending_vision=true documents into
extracted_features_json by calling analyze_manual_chart() on each
attachment, then clearing the flag.

Designed to be cheap to re-run (each pass picks up new pending rows) and
safe to interrupt — partial work is committed per-document. Intended to
run on a cron, on demand from the SPA, or as a background task on save.

Usage:
    from macro_positioning.manual.vision_drainer import drain
    summary = drain()                # process all pending
    summary = drain(limit=10)        # batch
    summary = drain(document_id="x") # single doc
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Optional

from macro_positioning.core.settings import settings
from macro_positioning.manual.vision import analyze_manual_chart


logger = logging.getLogger(__name__)


@dataclass
class DrainerSummary:
    candidates: int = 0      # rows seen (pending_vision=true)
    processed: int = 0       # successfully analyzed
    skipped_no_image: int = 0
    failed: int = 0          # analyze returned {"error": ...}
    transient: int = 0       # failed but TRANSIENT — left pending for retry

    def to_dict(self) -> dict:
        return asdict(self)


def _pending_query(document_id: Optional[str], limit: int) -> tuple[str, tuple]:
    """Build the SELECT for pending docs.

    `pending_vision` is stored inside `tags_json` (JSON1 extract).
    `attachment_paths_json` is the source of truth for image paths;
    `attachment_path` is the back-compat single-image fallback.
    """
    base = (
        "SELECT document_id, attachment_path, attachment_paths_json, tags_json, "
        "raw_text "
        "FROM documents "
        "WHERE json_extract(tags_json, '$.pending_vision') = 1 "
    )
    if document_id:
        return (base + " AND document_id = ?", (document_id,))
    return (base + " ORDER BY ingested_at ASC LIMIT ?", (limit,))


# Extensions Claude vision can actually process. Anything else
# (mp4/mov/webm video, ogg/m4a/mp3 voice notes, pdf, etc.) gets
# filtered out before we waste an API call on a guaranteed 400.
# Telegram poller pulls all attached media types; this is the
# downstream filter.
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def _is_image_path(path: str) -> bool:
    from os.path import splitext
    return splitext(path)[1].lower() in _IMAGE_EXTS


def _resolve_paths(attachment_path: Optional[str], paths_json: Optional[str]) -> list[str]:
    if paths_json:
        try:
            paths = json.loads(paths_json)
            if isinstance(paths, list) and paths:
                return [str(p) for p in paths if _is_image_path(str(p))]
        except json.JSONDecodeError:
            pass
    if attachment_path and _is_image_path(attachment_path):
        return [attachment_path]
    return []


def _merge_results(per_image: list[dict]) -> dict:
    """Merge multiple per-image TradeRecord results from one document.

    Strategy: take the first non-error result as the base, then carry over
    any non-null fields from subsequent results that the base left null.
    Fields like `key_levels` (list) are unioned. Always include the full
    per-image breakdown under `_images` for auditability.
    """
    cleans = [r for r in per_image if "error" not in r]
    if not cleans:
        return {"error": "all images failed", "_images": per_image}

    base = dict(cleans[0])
    for extra in cleans[1:]:
        for k, v in extra.items():
            if v is None:
                continue
            cur = base.get(k)
            if cur is None:
                base[k] = v
            elif isinstance(cur, list) and isinstance(v, list):
                # Stable de-dup, preserve order
                merged = list(cur)
                for x in v:
                    if x not in merged:
                        merged.append(x)
                base[k] = merged
    base["_images"] = per_image
    base["_merged_at"] = datetime.now(UTC).isoformat()
    return base


def drain(
    *,
    document_id: Optional[str] = None,
    limit: int = 25,
) -> DrainerSummary:
    """Process pending_vision documents. Returns counts."""
    summary = DrainerSummary()
    sql, params = _pending_query(document_id, limit)
    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()

    summary.candidates = len(rows)
    if not rows:
        return summary

    for row in rows:
        doc_id = row["document_id"]
        paths = _resolve_paths(row["attachment_path"], row["attachment_paths_json"])
        if not paths:
            summary.skipped_no_image += 1
            _clear_pending(doc_id, error="no_attachment")
            continue

        # Pass the paired message text as caption context — it states the
        # actual call (direction, target, retrospective, conditional) that
        # the chart pixels alone can't convey. Same caption for every image
        # in an album bundle.
        caption = (row["raw_text"] or "") if "raw_text" in row.keys() else ""
        per_image = [analyze_manual_chart(p, caption=caption) for p in paths]
        merged = _merge_results(per_image)

        if "error" in merged:
            summary.failed += 1
            # Distinguish TRANSIENT infra failures (out-of-credits, rate
            # limit, 5xx, network) from PERMANENT ones (Claude says "not a
            # chart", non-JSON, no_attachment). Transient → keep
            # pending_vision=1 so a later pass retries once the condition
            # clears; clearing it would permanently burn the doc (this is
            # exactly how a mid-run credit exhaustion silently killed 6k
            # charts before). Permanent → clear; re-running won't change
            # the answer and would waste API calls. Error payload is kept
            # either way for audit.
            transient = _merged_is_transient(per_image)
            if transient:
                summary.transient += 1
                _store_result(doc_id, merged, clear_pending=False)
            else:
                _store_result(doc_id, merged, clear_pending=True)
            continue

        _store_result(doc_id, merged, clear_pending=True)
        summary.processed += 1

    return summary


# Substrings that mark an error as transient/retryable rather than a real
# "this image isn't analyzable" verdict. Matched case-insensitively against
# every per-image error string for a document.
_TRANSIENT_MARKERS = (
    "credit balance is too low",
    "rate_limit", "rate limit", "429",
    "overloaded", "overloaded_error",
    "500", "502", "503", "504",
    "internal server error", "bad gateway", "service unavailable",
    "gateway timeout", "timeout", "timed out",
    "connection", "connecterror", "read error",
    "api_error", "temporarily",
)

# A 400 `invalid_request_error` is almost never a verdict on the IMAGE —
# it is our own request shape being wrong: a sampling param the model no
# longer accepts, a stale model id, a bad header. Re-running after the
# code is fixed WILL change the answer, which is the definition of
# retryable. Burning the doc instead is how 519 charts went dark behind
# `temperature is deprecated for this model` in Sept 2026: permanent by
# classification, trivially fixable in fact.
#
# The exception is a 400 that IS about the image (too large, unsupported
# media type, corrupt) — re-sending the same bytes will fail identically,
# so those stay permanent.
_REQUEST_SHAPE_MARKERS = (
    "invalid_request_error",
    "is deprecated",
    "unexpected keyword",
    "unsupported parameter",
    "model not found", "not_found_error",
)
_IMAGE_FAULT_MARKERS = (
    "image", "media type", "media_type", "exceeds", "too large",
    "could not process", "unsupported format", "base64",
)


def _merged_is_transient(per_image: list[dict]) -> bool:
    """True if ANY per-image error looks transient/retryable.

    Conservative: if a doc has a mix of a real not-a-chart verdict and a
    transient failure on another image, we still retry (the transient one
    might have masked a real chart). Better to re-run than to silently
    drop a recoverable doc.
    """
    for img in per_image:
        if not isinstance(img, dict):
            continue
        err = str(img.get("error", "")).lower()
        if not err:
            continue
        if any(m in err for m in _TRANSIENT_MARKERS):
            return True
        # Our request was malformed, not the image — retryable once the
        # code is fixed, unless the 400 is specifically about the bytes.
        if any(m in err for m in _REQUEST_SHAPE_MARKERS) and not any(
            m in err for m in _IMAGE_FAULT_MARKERS
        ):
            return True
    return False


def _store_result(document_id: str, result: dict, *, clear_pending: bool) -> None:
    """Write extracted_features_json + (optionally) flip pending_vision off."""
    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        if clear_pending:
            # Patch tags_json with json_set so we don't blow away other keys.
            conn.execute(
                """
                UPDATE documents SET
                  extracted_features_json = ?,
                  tags_json = json_set(tags_json, '$.pending_vision', json('false'))
                WHERE document_id = ?
                """,
                (json.dumps(result), document_id),
            )
        else:
            conn.execute(
                "UPDATE documents SET extracted_features_json = ? WHERE document_id = ?",
                (json.dumps(result), document_id),
            )
        conn.commit()


def _clear_pending(document_id: str, *, error: str) -> None:
    """Mark a doc done with an explanatory note (no image to analyze)."""
    payload = {"error": error, "skipped_at": datetime.now(UTC).isoformat()}
    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            UPDATE documents SET
              extracted_features_json = ?,
              tags_json = json_set(tags_json, '$.pending_vision', json('false'))
            WHERE document_id = ?
            """,
            (json.dumps(payload), document_id),
        )
        conn.commit()
