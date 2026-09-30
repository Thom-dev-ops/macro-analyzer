"""Chart Lab persistence — drop a chart, attach a read, emit signals.

Writes the same `documents` row the manual/Telegram path writes (same
upload dir, same `extracted_features_json` column), so everything
downstream — `signals`, `kol_levels`, `levels.synthesize_levels`, the
verify loop, `call_accuracy` — sees a desk chart as a first-class
document. The only difference is `source_id`, which carries
`DESK_SOURCE_PREFIX` so the desk's own read can be told apart from a
trusted voice's call at every consumer.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from macro_positioning.chartlab import (
    DESK_AUTHOR_CHANNEL,
    DESK_AUTHOR_NAME,
    DESK_SOURCE_PREFIX,
)
from macro_positioning.core.settings import settings
from macro_positioning.manual.authors import slugify, upsert_author
from macro_positioning.manual.models import AuthorRef
from macro_positioning.manual.processor import save_attachment

# The live DB has concurrent writers (API servers + the Telegram
# listener). 30s + retry is the house rule — see the write-lock
# contention note in the project context.
_BUSY_TIMEOUT_MS = 30_000

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# The SECTION 10 contract, as a fill-in skeleton. `chart read --template`
# prints this; the operator's Claude Code session fills it from the image
# and `chart write` persists it. Field names and allowed values are
# copied from config/manual_chart_framework.md SECTION 10 — keep them in
# lockstep with that file, which is the prompt the automated path uses.
SECTION_10_TEMPLATE: dict = {
    "asset_class": "crypto | equity | commodity | index | fx | unknown",
    "ticker": "symbol exactly as shown on the chart",
    "timeframe": "15m | 1h | 4h | 1D | 1W | null",
    "call_type": (
        "directional_long | directional_short | bidirectional | "
        "retrospective | no_trade | not_a_chart"
    ),
    "is_forward_looking": True,
    "trade_stage": "watching | active | completed",
    "bias": "bullish | bearish | neutral",
    "pattern": "dominant pattern name, or null",
    "confluence_score": 3,
    "setups": [
        {
            "direction": "long | short",
            "entry": 0.0,
            "stop_loss": 0.0,
            "invalidation": "plain-language condition that kills the thesis",
            "take_profits": [0.0],
            "final_target": 0.0,
            "status": "pending | triggered | completed",
        }
    ],
    "indicators_visible": ["MACD", "RSI"],
    "notes": "one-line context",
}


@dataclass
class ChartDrop:
    """A chart the desk has parked, awaiting a read."""

    document_id: str
    attachment_path: str
    source_id: str
    author_id: str
    ticker: str | None


@dataclass
class WriteResult:
    """Outcome of attaching a read to a chart."""

    document_id: str
    ticker: str | None
    setups: int
    signals: list[dict] = field(default_factory=list)
    signal_error: str | None = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.sqlite_path, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    return conn


def desk_source_id(label: str = DESK_AUTHOR_NAME) -> str:
    """`documents.source_id` for a desk drop under `label`."""
    return f"{DESK_SOURCE_PREFIX}{slugify(label)}"


def add_chart(
    image_path: str | Path,
    *,
    ticker: str | None = None,
    timeframe: str | None = None,
    note: str = "",
    label: str = DESK_AUTHOR_NAME,
) -> ChartDrop:
    """Park a chart image as a desk document awaiting extraction.

    The row is written `pending_vision`, which is what keeps the signal
    router's hands off it (`router._pending_vision`) until a read lands.
    """
    path = Path(image_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"no such image: {path}")
    if path.suffix.lower() not in _IMAGE_EXTS:
        raise ValueError(
            f"{path.name}: not an image extension this pipeline reads "
            f"({', '.join(sorted(_IMAGE_EXTS))})"
        )

    author_id = upsert_author(
        AuthorRef(
            display_name=DESK_AUTHOR_NAME,
            channel=DESK_AUTHOR_CHANNEL,
            channel_type="self",
            notes="chart-lab desk drop — deliberately NOT a seeded trusted voice",
        )
    )
    stored_path = save_attachment(path.read_bytes(), path.name)

    ticker_norm = (ticker or "").strip().upper() or None
    document_id = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    source_id = desk_source_id(label)

    caption, title = _caption_for(ticker_norm, timeframe, note)

    tags_payload = {
        "tags": sorted({"manual", "chart", "vision", "chartlab"}),
        "agents": [],
        "pending_vision": True,
        "tickers": [ticker_norm] if ticker_norm else [],
    }
    user_meta = {
        "user": {"ticker": ticker_norm, "timeframe": timeframe, "note": note},
        "resolved": {},
        "channel": DESK_AUTHOR_CHANNEL,
        "channel_type": "self",
        "origin": "chartlab",
    }

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO documents (
                document_id, source_id, title, url, published_at, author,
                content_type, raw_text, cleaned_text, tags_json, ingested_at,
                author_id, user_metadata_json, attachment_path,
                extracted_features_json, attachment_paths_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                source_id,
                title,
                None,
                now,
                DESK_AUTHOR_NAME,
                "manual_chart",
                caption,
                caption,
                json.dumps(tags_payload),
                now,
                author_id,
                json.dumps(user_meta),
                stored_path,
                None,
                json.dumps([stored_path]),
            ),
        )
        conn.commit()

    return ChartDrop(
        document_id=document_id,
        attachment_path=stored_path,
        source_id=source_id,
        author_id=author_id,
        ticker=ticker_norm,
    )


def _caption_for(
    ticker: str | None, timeframe: str | None, note: str, asset_class: str | None = None
) -> tuple[str, str]:
    """(caption, title) for a drop's declared metadata.

    One place, so `add_chart` and `update_chart_meta` cannot drift into
    describing the same chart two different ways.
    """
    caption = " · ".join(b for b in (ticker, timeframe, asset_class, note) if b)
    title = " · ".join([b for b in (ticker, timeframe, "chart lab") if b])
    return caption, (title or "Chart lab drop")


def _require_desk_chart(conn: sqlite3.Connection, document_id: str) -> sqlite3.Row:
    """Fetch a document and refuse it unless it is a desk chart drop.

    Both callers below mutate or destroy a row by id alone, so the
    namespace check is the whole safety story: without it the chart-lab
    HTTP surface would be a general delete/edit for any of the ~56k
    documents in the desk's database.
    """
    row = conn.execute(
        "SELECT * FROM documents WHERE document_id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no such document: {document_id}")
    if not str(row["source_id"] or "").startswith(DESK_SOURCE_PREFIX):
        raise PermissionError(
            f"{document_id} is not a chart-lab drop "
            f"(source_id={row['source_id']!r}) — refusing to touch it"
        )
    return row


# Every table that keys rows off documents.document_id. A misadded chart
# that was already read has emitted signals; leaving them behind means
# the delete only hides the chart while its readings keep feeding the
# conviction and positioning maps.
_DOC_CHILD_TABLES = (
    "signals",
    "signal_extraction_attempts",
    "call_outcomes",
    "manual_chart_attachments",
)


def update_chart_meta(
    document_id: str,
    *,
    ticker: str | None = None,
    timeframe: str | None = None,
    note: str | None = None,
    asset_class: str | None = None,
) -> dict:
    """Re-declare what a parked chart is: ticker, timeframe, class, note.

    This is the operator's declaration, not the read. It is the caption
    the vision pass is given and the ticker hint the bench follows, so
    fixing it before a read changes the read. It deliberately does NOT
    rewrite an extraction already on file — a read is evidence of what
    the model saw, and silently retyping its ticker would leave the
    emitted signals disagreeing with the document they came from.
    Re-read the chart to move a read.
    """
    ticker_norm = (ticker or "").strip().upper() or None
    tf = (timeframe or "").strip() or None
    cls = (asset_class or "").strip().lower() or None
    note_txt = (note or "").strip()

    with _connect() as conn:
        row = _require_desk_chart(conn, document_id)
        try:
            tags = json.loads(row["tags_json"] or "{}")
        except (TypeError, ValueError):
            tags = {}
        try:
            meta = json.loads(row["user_metadata_json"] or "{}")
        except (TypeError, ValueError):
            meta = {}

        tags["tickers"] = [ticker_norm] if ticker_norm else []
        meta["user"] = {
            "ticker": ticker_norm,
            "timeframe": tf,
            "note": note_txt,
            "asset_class": cls,
        }
        caption, title = _caption_for(ticker_norm, tf, note_txt, cls)

        conn.execute(
            """
            UPDATE documents
               SET raw_text = ?, cleaned_text = ?, title = ?,
                   tags_json = ?, user_metadata_json = ?
             WHERE document_id = ?
            """,
            (
                caption,
                caption,
                title,
                json.dumps(tags),
                json.dumps(meta),
                document_id,
            ),
        )
        conn.commit()

    return {
        "document_id": document_id,
        "ticker": ticker_norm,
        "timeframe": tf,
        "asset_class": cls,
        "note": note_txt,
        "caption": caption,
    }


def delete_chart(document_id: str, *, remove_file: bool = True) -> dict:
    """Delete a misadded desk chart, its signals, and its image.

    Refuses any document outside the desk namespace. Returns the row
    counts removed so the caller can say what actually went.
    """
    removed: dict[str, int] = {}
    paths: list[str] = []

    with _connect() as conn:
        row = _require_desk_chart(conn, document_id)
        for key in ("attachment_path",):
            if row[key]:
                paths.append(str(row[key]))
        try:
            extra = json.loads(row["attachment_paths_json"] or "[]")
        except (TypeError, ValueError):
            extra = []
        paths += [str(p) for p in extra if p]

        for table in _DOC_CHILD_TABLES:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE document_id = ?", (document_id,)  # noqa: S608
            )
            if cur.rowcount:
                removed[table] = cur.rowcount
        conn.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
        conn.commit()

        # Only orphan images go. A path shared with another document
        # (a re-drop of the same screenshot) stays on disk.
        still_used = set()
        for path in set(paths):
            hit = conn.execute(
                "SELECT 1 FROM documents WHERE attachment_path = ? LIMIT 1", (path,)
            ).fetchone()
            if hit:
                still_used.add(path)

    files_removed = 0
    if remove_file:
        upload_root = Path(settings.chart_upload_dir).resolve()
        for rel in set(paths) - still_used:
            abs_path = Path(rel)
            if not abs_path.is_absolute():
                abs_path = Path(settings.base_dir) / rel
            try:
                resolved = abs_path.resolve()
                # Never unlink outside the upload directory, whatever the
                # column happens to say.
                resolved.relative_to(upload_root)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                resolved.unlink(missing_ok=True)
                files_removed += 1

    removed["documents"] = 1
    removed["files"] = files_removed
    return {"document_id": document_id, "removed": removed}


def vocabulary(*, ticker_limit: int = 150) -> dict:
    """The picklists the drop form offers.

    Timeframes and asset classes are split out of SECTION_10_TEMPLATE
    rather than retyped, so the form can only ever offer values the
    extraction contract already accepts.

    Tickers are what the desk has actually been talking about, put
    through `resolve_symbol` first: raw `signals.asset_ticker` carries
    exchange pairs (`BTC/USDT`), DEX tokens and the odd `N/A`, and the
    house rule is that discovery surfaces show real assets. The resolver
    also collapses a pair onto its base, so `BTC/USDT` and `BTC/USD`
    suggest one `BTC`. It is a suggestion list, not a constraint — the
    field stays free text for anything the resolver has not met.
    """
    from macro_positioning.prices.symbol_map import resolve_symbol

    def _options(field: str) -> list[str]:
        raw = str(SECTION_10_TEMPLATE.get(field) or "")
        return [
            part.strip()
            for part in raw.split("|")
            if part.strip() and part.strip() != "null"
        ]

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT asset_ticker, COUNT(*) AS n
              FROM signals
             WHERE extracted_at >= date('now', '-180 day')
               AND asset_ticker IS NOT NULL AND asset_ticker <> ''
             GROUP BY asset_ticker
             ORDER BY n DESC
            """
        ).fetchall()

    tickers: list[str] = []
    seen: set[str] = set()
    for row in rows:
        resolved = resolve_symbol(str(row["asset_ticker"]))
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        tickers.append(resolved)
        if len(tickers) >= int(ticker_limit):
            break

    return {
        "timeframes": _options("timeframe"),
        "asset_classes": _options("asset_class"),
        "tickers": tickers,
    }


def get_chart(document_id: str) -> dict | None:
    """The full document row for a chart, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
    return dict(row) if row else None


def list_charts(*, limit: int = 20, ticker: str | None = None) -> list[dict]:
    """Recent desk chart drops, newest first."""
    sql = [
        "SELECT document_id, title, raw_text, attachment_path, ingested_at,",
        "       extracted_features_json, tags_json, user_metadata_json",
        "FROM documents",
        "WHERE source_id LIKE ?",
    ]
    params: list = [f"{DESK_SOURCE_PREFIX}%"]
    if ticker:
        # Match the declared ticker in either the caption or the read.
        sql.append(
            "AND (upper(coalesce(raw_text,'')) LIKE ?"
            " OR upper(coalesce(json_extract(extracted_features_json,'$.ticker'),'')) = ?)"
        )
        params += [f"%{ticker.strip().upper()}%", ticker.strip().upper()]
    sql.append("ORDER BY ingested_at DESC LIMIT ?")
    params.append(int(limit))

    with _connect() as conn:
        rows = conn.execute("\n".join(sql), params).fetchall()
    return [dict(r) for r in rows]


def latest_chart(ticker: str | None = None) -> dict | None:
    """Most recent desk drop, optionally for one ticker."""
    rows = list_charts(limit=1, ticker=ticker)
    return rows[0] if rows else None


def latest_read(ticker: str) -> dict | None:
    """The newest desk chart read that resolved to `ticker`.

    Returns the parsed `extracted_features_json` with `_document_id` and
    `_ingested_at` folded in, so the bench can date the read it quotes.
    """
    want = (ticker or "").strip().upper()
    if not want:
        return None
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT document_id, ingested_at, attachment_path, extracted_features_json
            FROM documents
            WHERE source_id LIKE ?
              AND extracted_features_json IS NOT NULL
              AND upper(coalesce(json_extract(extracted_features_json,'$.ticker'),'')) = ?
            ORDER BY ingested_at DESC LIMIT 1
            """,
            (f"{DESK_SOURCE_PREFIX}%", want),
        ).fetchall()
    if not rows:
        return None
    row = dict(rows[0])
    try:
        feats = json.loads(row["extracted_features_json"] or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(feats, dict):
        return None
    feats["_document_id"] = row["document_id"]
    feats["_ingested_at"] = row["ingested_at"]
    feats["_attachment_path"] = row["attachment_path"]
    return feats


def write_extraction(
    document_id: str,
    features: dict,
    *,
    extract_signals: bool = True,
) -> WriteResult:
    """Attach a chart read to a drop and (optionally) emit its signals.

    Mirrors `vision_drainer._store_result` — same column, same
    `pending_vision` clear — so a desk read and a drained read leave the
    row in an identical state.
    """
    if not isinstance(features, dict):
        raise TypeError("features must be a JSON object")
    doc = get_chart(document_id)
    if doc is None:
        raise KeyError(f"no such document: {document_id}")

    stamped = dict(features)
    stamped.setdefault("analyzed_at", datetime.now(UTC).isoformat())
    # Attribution for the downstream extractor's model_provider/model_name.
    stamped.setdefault("vision_backend", "chartlab")
    stamped.setdefault("vision_model", "claude-code-session")

    ticker = (stamped.get("ticker") or "").strip().upper() or None
    setups = stamped.get("setups")
    n_setups = len(setups) if isinstance(setups, list) else 0

    with _connect() as conn:
        conn.execute(
            """
            UPDATE documents
               SET extracted_features_json = ?,
                   tags_json = json_set(tags_json, '$.pending_vision', json('false'))
             WHERE document_id = ?
            """,
            (json.dumps(stamped), document_id),
        )
        conn.commit()

    result = WriteResult(document_id=document_id, ticker=ticker, setups=n_setups)
    if not extract_signals:
        return result

    try:
        from macro_positioning.signals.runner import extract_for_document

        fresh = get_chart(document_id) or {}
        outcome = extract_for_document(fresh)
        result.signals, extract_error = _signal_dicts(outcome)
        if extract_error:
            result.signal_error = extract_error
    except Exception as exc:  # noqa: BLE001 — the read is persisted either way
        result.signal_error = f"{type(exc).__name__}: {exc}"
    return result


def _signal_dicts(outcome) -> tuple[list[dict], str | None]:
    """Normalize `extract_for_document` output into (signals, error).

    It returns a dict — `{run_id, signals, by_extractor, error_message}`
    — so read the key. Treating it as an object and falling back to the
    value itself silently counts the dict's own keys as signals.
    """
    if outcome is None:
        return [], None
    if isinstance(outcome, dict):
        raw = outcome.get("signals") or []
        error = outcome.get("error_message")
    else:
        raw = getattr(outcome, "signals", None) or []
        error = getattr(outcome, "error_message", None)

    out: list[dict] = []
    for sig in raw:
        if isinstance(sig, dict):
            out.append(sig)
        elif hasattr(sig, "model_dump"):
            out.append(sig.model_dump())
        else:
            out.append({"repr": repr(sig)})
    return out, error


def read_chart_auto(document_id: str, *, model: str | None = None) -> WriteResult:
    """Read a parked chart via the `claude -p` CLI backend, then persist it.

    Forces `vision_backend="cli"` for the duration regardless of the
    ambient setting: this command exists precisely to avoid billed API
    calls, so it must not silently inherit `MPA_VISION_BACKEND=api`.
    """
    doc = get_chart(document_id)
    if doc is None:
        raise KeyError(f"no such document: {document_id}")
    path = doc.get("attachment_path")
    if not path:
        raise ValueError(f"{document_id} has no attachment to read")
    abs_path = Path(path)
    if not abs_path.is_absolute():
        abs_path = Path(settings.base_dir) / path

    from macro_positioning.manual import vision

    previous = settings.vision_backend
    try:
        settings.vision_backend = "cli"
        features = vision.analyze_manual_chart(
            abs_path,
            model=model,
            caption=doc.get("raw_text") or "",
        )
    finally:
        settings.vision_backend = previous

    if isinstance(features, dict) and features.get("error"):
        raise RuntimeError(f"vision read failed: {features['error']}")
    return write_extraction(document_id, features)
