"""Telethon-powered ingest from Telegram channels + DMs.

Phase-1 entrypoint for piping pre-existing trade-call content into the
chart-vision pipeline without manual screenshotting. Two modes:

  • backfill(chat_id) — paginated historical pull. Idempotent.
  • listen(chat_ids)  — long-running event handler for live capture.

Both modes share the same downstream processing — for each new
(or historical) message we:
  1. Download attached photos to uploads/charts/YYYY-MM/{uuid}.{ext}.
  2. Hash-dedupe via vision_cache → skip Claude re-runs on identical bytes.
  3. Cross-source dedupe via documents.tags_json.image_sha256_list →
     instead of inserting a duplicate, record an "also_seen_in" tag on
     the existing row (this becomes a higher-conviction signal).
  4. Detect Telegram-native forwards (msg.forward.chat_id maps to a
     known channel) and attribute to the original source.
  5. Group multi-photo albums (shared grouped_id) into one document
     with N attachments.
  6. Insert documents row with pending_vision=true. The existing
     vision_drainer picks it up automatically.

This module is **read-only**. No code path posts, forwards, deletes,
or otherwise mutates Telegram state. The HARD_SAFETY_GUARD assert
at module load fails the import if any write-capable symbol leaks in.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from macro_positioning.core.settings import settings
from macro_positioning.manual.authors import slugify, slugify_author, upsert_author
from macro_positioning.manual.models import AuthorRef


logger = logging.getLogger(__name__)


# ── post_author → seeded author resolution ──────────────────────────────────
# Telegram channel forwards strip the sender user but preserve `post_author`
# (the per-person signature on signed channel posts). Ari Gold's DM is ~89%
# BigNuts content relayed from the Feather Hands / Market Traders groups, and
# the ONLY way to attribute it to the right person is this signature. Maps
# normalized post_author → (display_name, channel) of a SEEDED author so the
# relayed content lands under the same author_id as that person's direct
# posts — which is what makes same-author dedupe (no conviction inflation)
# work. joejoe55's TradingView charts ride inside Feather Hands posts under
# post_author=BigNuts, so there's no separate joejoe55 signature to map.
_POST_AUTHOR_ALIASES: dict[str, tuple[str, str]] = {
    "bignuts":  ("Big_Nuts", "Feather Hands"),
    "maddog31": ("MadDog31", "Feather Hands"),
    "joejoe55": ("joejoe55", "Feather Hands"),
}


# ── Author identity for conviction dedup ────────────────────────────────────
# Distinct trusted *authors* posting the same chart = confirmation (+0.25).
# But several of our author_ids are the SAME real-world entity, and counting
# them as separate votes inflates a single source. The Feather Hands crew
# (Big_Nuts, MadDog31, joejoe55) run both the Feather Hands Trading broadcast
# channel and the rolling Market Traders groups — a chart anywhere across
# those is ONE call. Map every family author_id to a single canonical
# identity so same-family dupes register as reposts, not confirmations.
_AUTHOR_IDENTITY: dict[str, str] = {
    "feather-hands:big-nuts": "feather-hands-family",
    "feather-hands:maddog31": "feather-hands-family",
    "feather-hands:joejoe55": "feather-hands-family",
    "feather-hands-trading:feather-hands-trading": "feather-hands-family",
    "market-traders:market-traders": "feather-hands-family",
}

# Pure relays carry OTHER people's content and have no independent analytical
# voice. Ari Gold mass-forwards the Feather Hands crew's posts; his
# co-occurrence on a chart is never confirmation. (When his forwards resolve
# to a real post_author they're attributed to that person instead; only the
# unresolvable remainder stays "Ari Gold".)
_RELAY_AUTHOR_IDS: frozenset[str] = frozenset({"ari-gold:ari-gold"})


def _canonical_identity(author_id: Optional[str]) -> str:
    return _AUTHOR_IDENTITY.get(author_id or "", author_id or "")


def _norm_post_author(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _seeded_author_from_post_author(
    post_author: Optional[str],
) -> Optional[tuple[str, str, str]]:
    """Resolve a forward's post_author signature to a seeded author.

    Returns (author_id, display_name, channel) or None if unrecognized.
    """
    key = _norm_post_author(post_author)
    hit = _POST_AUTHOR_ALIASES.get(key)
    if not hit:
        return None
    display, channel = hit
    author_id = upsert_author(
        AuthorRef(display_name=display, channel=channel, channel_type="telegram")
    )
    return author_id, display, channel


# ── Hard safety guard ────────────────────────────────────────────────────────
# Read-only by construction. If a future edit adds any sending capability
# this fails at import time so it can't slip into production unnoticed.
def _enforce_read_only() -> None:
    # Build forbidden tokens from split fragments so this guard's own
    # source doesn't trip the scan.
    src = Path(__file__).read_text(encoding="utf-8")
    parts = [
        ("send", "_message"), ("send", "_file"), ("send", "_reaction"),
        ("forward", "_messages"), ("delete", "_messages"), ("edit", "_message"),
        (".re", "ply("), (".re", "spond("),
    ]
    forbidden = [a + b for a, b in parts]
    found = [tok for tok in forbidden if tok in src]
    if found:
        raise AssertionError(
            f"telegram_poller.py contains forbidden write-capable "
            f"tokens (read-only-only module): {found}. "
            "If you're intentionally adding write capability, lift "
            "this guard explicitly and update docs."
        )


_enforce_read_only()


# ── Telethon client (lazy) ──────────────────────────────────────────────────


def _make_client():
    """Same session file Phase-0 smoke test uses."""
    try:
        from telethon import TelegramClient  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "telethon not installed. Run `uv add telethon`."
        ) from e
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise RuntimeError(
            "Telegram credentials missing. Set MPA_TELEGRAM_API_ID + "
            "MPA_TELEGRAM_API_HASH in .env."
        )
    settings.telegram_session_path.parent.mkdir(parents=True, exist_ok=True)
    session_str = str(settings.telegram_session_path).removesuffix(".session")
    return TelegramClient(
        session_str,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )


# ── Per-message ingestion helpers ───────────────────────────────────────────


def _channel_config_by_chat_id(chat_id: int) -> Optional[tuple[str, dict]]:
    """Find the (slug, config) tuple for a chat_id, if it's one we track."""
    for slug, cfg in settings.telegram_channels.items():
        if cfg.get("chat_id") == chat_id:
            return slug, cfg
    return None


def _author_for_channel(slug: str, cfg: dict) -> str:
    """Upsert + return the author_id used for this channel."""
    ref = AuthorRef(
        display_name=cfg["author_display"],
        channel=cfg["author_display"],  # channel IS the author for broadcasts
        channel_type="telegram",
    )
    return upsert_author(ref)


async def _download_photo(message: Any, base_dir: Path) -> Optional[tuple[str, bytes]]:
    """Download a single message's photo (if any) to uploads/charts/YYYY-MM/.
    Returns (relative_path, raw_bytes) or None if no photo attachment.

    Filters out non-image media — videos (mp4/mov/webm), voice notes
    (ogg/m4a/mp3), PDFs, generic files. Claude vision can't process
    them and they'd just choke the drainer queue. Detection is by
    Telethon media-class name + mime_type on documents (gifs and
    stickers ride MessageMediaDocument with image/* mimes; videos
    and voice notes don't)."""
    if not message.media:
        return None
    media_cls = type(message.media).__name__
    if media_cls == "MessageMediaPhoto":
        pass  # always an image
    elif media_cls == "MessageMediaDocument":
        # Documents include photos-as-files, gifs, stickers, voice,
        # video, generic files. Mime tells us which.
        doc = getattr(message.media, "document", None)
        mime = (getattr(doc, "mime_type", "") or "").lower()
        if not mime.startswith("image/"):
            return None
    else:
        return None
    today = datetime.now(UTC)
    sub = base_dir / "uploads" / "charts" / f"{today.year:04d}-{today.month:02d}"
    sub.mkdir(parents=True, exist_ok=True)
    target = sub / f"{uuid.uuid4().hex}"
    # download_media returns the actual path with extension appended
    saved = await message.download_media(file=str(target))
    if not saved:
        return None
    raw_bytes = Path(saved).read_bytes()
    try:
        rel = str(Path(saved).resolve().relative_to(base_dir.resolve()))
    except ValueError:
        rel = str(saved)
    return rel, raw_bytes


def _find_existing_doc_by_sha(
    image_sha256_list: list[str],
    conn: sqlite3.Connection,
) -> Optional[str]:
    """Cross-source dedupe: if any attached image's sha256 already exists
    in another manual_chart document, return that document_id. The poller
    will add an 'also_seen_in' annotation instead of inserting a duplicate."""
    if not image_sha256_list:
        return None
    placeholders = ",".join("?" * len(image_sha256_list))
    row = conn.execute(
        f"""
        SELECT document_id FROM documents
        WHERE content_type = 'manual_chart'
        AND EXISTS (
            SELECT 1 FROM json_each(
                COALESCE(json_extract(tags_json, '$.image_sha256_list'), '[]')
            )
            WHERE value IN ({placeholders})
        )
        LIMIT 1
        """,
        image_sha256_list,
    ).fetchone()
    return row[0] if row else None


def _find_existing_doc_by_msg_ids(
    chat_id: int, msg_ids: list[int], conn: sqlite3.Connection,
) -> Optional[str]:
    """Idempotent re-ingest: if these exact (chat_id, msg_id) pairs were
    already ingested, skip silently."""
    if not msg_ids:
        return None
    pairs = [f"{chat_id}:{m}" for m in msg_ids]
    placeholders = ",".join("?" * len(pairs))
    row = conn.execute(
        f"""
        SELECT document_id FROM documents
        WHERE EXISTS (
            SELECT 1 FROM json_each(
                COALESCE(json_extract(tags_json, '$.telegram_message_keys'), '[]')
            )
            WHERE value IN ({placeholders})
        )
        LIMIT 1
        """,
        pairs,
    ).fetchone()
    return row[0] if row else None


def _annotate_duplicate(
    document_id: str,
    incoming_author_id: str,
    incoming_author_display: str,
    incoming_slug: str,
    new_msg_keys: list[str],
    conn: sqlite3.Connection,
) -> None:
    """Record a duplicate-bytes observation on an existing document.

    Conviction is keyed on distinct *author*, not channel — because the
    same person (e.g. BigNuts) broadcasts the same chart across Feather
    Hands, Market Traders, and via Ari's DM relay. Counting those as
    separate votes would massively inflate a single source. So:

      • Same author as the existing doc → bump `repost_count`. A weaker
        "they keep pumping this" signal; never amplifies conviction.
      • DIFFERENT trusted author posts the same chart → append to
        `also_called_by` (list of {author_id, display, slug}). THIS is
        the genuine confirmation signal — independent traders landing on
        the same setup — and drives the +0.25 conviction bump downstream.

    `also_called_by` is deduped by author_id, so even if Ari relays the
    same BigNuts chart ten times it contributes at most one entry (and
    only if BigNuts isn't already the origin author).
    """
    row = conn.execute(
        "SELECT tags_json, author_id FROM documents WHERE document_id=?",
        (document_id,),
    ).fetchone()
    if not row:
        return
    try:
        tags = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        tags = {}
    origin_author_id = row[1]

    incoming_canon = _canonical_identity(incoming_author_id)
    origin_canon = _canonical_identity(origin_author_id)

    if incoming_author_id in _RELAY_AUTHOR_IDS:
        # Pure relay (Ari mass-forward) — content carried, not authored.
        # Never a confirming voice. Track separately for audit.
        tags["relayed_count"] = int(tags.get("relayed_count") or 0) + 1
    elif incoming_canon == origin_canon:
        # Same real-world entity (same author, or same family across
        # channels) — re-post. Do NOT amplify conviction.
        tags["repost_count"] = int(tags.get("repost_count") or 0) + 1
    else:
        # Genuinely different trusted author landed on the same chart —
        # real confirmation. Dedupe by canonical identity so two members
        # of the same other-family count once.
        called_by = tags.get("also_called_by") or []
        if not any(_canonical_identity(e.get("author_id")) == incoming_canon
                   for e in called_by):
            called_by.append({
                "author_id": incoming_author_id,
                "display": incoming_author_display,
                "slug": incoming_slug,
            })
        tags["also_called_by"] = called_by

    # Always track message keys so future re-ingests of the same message
    # idempotently short-circuit at the _find_existing_doc_by_msg_ids check.
    keys = tags.get("telegram_message_keys") or []
    for k in new_msg_keys:
        if k not in keys:
            keys.append(k)
    tags["telegram_message_keys"] = keys

    conn.execute(
        "UPDATE documents SET tags_json=? WHERE document_id=?",
        (json.dumps(tags), document_id),
    )


def _extract_forward_meta(first: Any) -> dict:
    """Pull the forward header into a plain dict for storage + attribution.

    Channel forwards anonymize the sender (from_name/sender_id are None) but
    expose post_author + the origin chat. We capture all of it so attribution
    and downstream audit can see exactly where a relayed chart came from.
    """
    fwd = getattr(first, "forward", None)
    if fwd is None:
        return {}
    origin_title = None
    try:
        chat = getattr(fwd, "chat", None)
        if chat is not None:
            origin_title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
    except Exception:  # noqa: BLE001
        pass
    return {
        "post_author": getattr(fwd, "post_author", None),
        "from_name": getattr(fwd, "from_name", None),
        "origin_chat_id": getattr(fwd, "chat_id", None),
        "origin_title": origin_title,
    }


async def _resolve_attribution(
    messages: list[Any],
    fallback_slug: str,
    fallback_cfg: dict,
    cfg_by_chat_id_callable,
) -> tuple[str, str, dict, dict]:
    """Pick the right attribution given a message bundle.

    Priority order:
      1. Forward from a TRACKED channel → attribute to that origin channel
         (keeps Ari's Feather-Hands relays sharing the channel's author_id,
         so they dedupe against direct Feather Hands posts as same-author).
      2. Forward whose post_author resolves to a SEEDED person (BigNuts →
         Big_Nuts) → attribute to that person. Routes Ari's Market-Traders
         BigNuts relays to the canonical Big_Nuts author regardless of which
         monthly group instance they came from.
      3. Sender user_id matches a known_senders entry in the channel config
         → attribute to the named individual author within the group.
      4. Fall back to the channel itself.

    Returns (author_id, channel_slug, channel_cfg, forward_meta).
    """
    first = messages[0]
    fwd_meta = _extract_forward_meta(first)

    # ── 1. Forward from a tracked channel ─────────────────────────────────
    fwd = getattr(first, "forward", None)
    if fwd is not None:
        origin_id = (
            getattr(fwd, "chat_id", None)
            or getattr(getattr(fwd, "chat", None), "id", None)
        )
        if origin_id is not None:
            origin = cfg_by_chat_id_callable(int(origin_id))
            if origin:
                origin_slug, origin_cfg = origin
                author_id = _author_for_channel(origin_slug, origin_cfg)
                return author_id, origin_slug, origin_cfg, fwd_meta

        # ── 2. Forward → seeded person via post_author signature ──────────
        resolved = _seeded_author_from_post_author(fwd_meta.get("post_author"))
        if resolved:
            author_id, display, _channel = resolved
            person_cfg = dict(fallback_cfg, author_display=display)
            return author_id, fallback_slug, person_cfg, fwd_meta

    # ── 3. Per-sender attribution within a group ──────────────────────────
    # known_senders: {str(user_id): "Display Name"} in the channel config.
    known_senders: dict[str, str] = fallback_cfg.get("known_senders") or {}
    if known_senders:
        sender_id = getattr(first, "sender_id", None) or getattr(first, "from_id", None)
        if sender_id is not None:
            sender_id_str = str(int(sender_id))
            display = known_senders.get(sender_id_str)
            if display:
                ref = AuthorRef(
                    display_name=display,
                    channel=display,
                    channel_type="telegram",
                )
                author_id = upsert_author(ref)
                sender_cfg = dict(fallback_cfg, author_display=display)
                return author_id, fallback_slug, sender_cfg, fwd_meta

    # ── 4. Channel-level fallback ─────────────────────────────────────────
    author_id = _author_for_channel(fallback_slug, fallback_cfg)
    return author_id, fallback_slug, fallback_cfg, fwd_meta


# ── Core: turn a bundle of messages into one document row ───────────────────


async def _ingest_bundle(
    messages: list[Any],
    observed_chat_id: int,
    observed_slug: str,
    observed_cfg: dict,
    base_dir: Path,
    db_path: Path,
    dry_run: bool = False,
) -> str:
    """Insert (or dedupe-annotate) one document for a bundle of Telegram
    messages. A bundle is either a single message or a multi-photo album
    sharing one grouped_id. Returns one of:
        'imported' · 'dedup_bytes' · 'dedup_msg_id' · 'empty' · 'dry_run'
    """
    if not messages:
        return "empty"
    msg_keys = [f"{observed_chat_id}:{m.id}" for m in messages]

    # ─ Pre-existing message-id check (idempotent re-runs).
    with sqlite3.connect(db_path) as conn:
        existing = _find_existing_doc_by_msg_ids(observed_chat_id, [m.id for m in messages], conn)
    if existing:
        return "dedup_msg_id"

    # ─ Resolve attribution FIRST — the byte-dedupe below compares the
    #   incoming author against the existing doc's author to decide
    #   repost (same author) vs cross-author confirmation. Forwards may
    #   redirect to the original channel or to a seeded person (BigNuts).
    author_id, attrib_slug, attrib_cfg, fwd_meta = await _resolve_attribution(
        messages, observed_slug, observed_cfg, _channel_config_by_chat_id,
    )
    author_display = attrib_cfg["author_display"]

    # ─ Download photos.
    downloaded: list[tuple[str, bytes]] = []
    for m in messages:
        if dry_run:
            # Just count the media without actually downloading
            if m.media:
                downloaded.append(("(dry_run)", b""))
            continue
        result = await _download_photo(m, base_dir)
        if result:
            downloaded.append(result)

    attachment_paths = [p for p, _b in downloaded]
    image_shas = [
        hashlib.sha256(b).hexdigest() for _p, b in downloaded if b
    ]

    # ─ Cross-source dedupe by image bytes (author-aware).
    if image_shas and not dry_run:
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            existing = _find_existing_doc_by_sha(image_shas, conn)
            if existing:
                _annotate_duplicate(
                    existing, author_id, author_display, attrib_slug,
                    msg_keys, conn,
                )
                conn.commit()
                return "dedup_bytes"

    # ─ Build combined body text (preserving message order).
    body_parts = [(m.message or "").strip() for m in messages if (m.message or "").strip()]
    body = "\n\n".join(body_parts)

    # ─ Skip empties (no media + no text).
    if not attachment_paths and not body:
        return "empty"

    if dry_run:
        return "dry_run"

    # ─ Insert document row.
    document_id = uuid.uuid4().hex
    first = messages[0]
    published_at = first.date.isoformat() if first.date else datetime.now(UTC).isoformat()
    pending_vision = bool(attachment_paths)
    grouped_id = getattr(first, "grouped_id", None)

    # Title: ticker hints aren't easily extractable here without OCR; use
    # author + date so the recent-drops row reads cleanly.
    title = f"{author_display} · {published_at[:10]}"

    tags_payload = {
        "tags": sorted({"manual", "telegram", "chart"} | ({"vision"} if pending_vision else set())),
        "agents": [
            "narrative_synthesizer", "regime_classifier",
            "sector_theme_scorer", "technical_scorer",
        ],
        "pending_vision": pending_vision,
        "source": "telegram_poller",
        "channel_slug": attrib_slug,
        "observed_in_channel_slug": observed_slug,
        "telegram_message_keys": msg_keys,
        "telegram_grouped_id": grouped_id,
        "image_sha256_list": image_shas,
        "is_forward": getattr(first, "forward", None) is not None,
        "is_dm": bool(observed_cfg.get("is_dm")),
        # Forward provenance — who originally posted this (post_author) and
        # where it came from, when relayed (e.g. via Ari's DM). Empty dict
        # for non-forwards. Lets the UI show "BigNuts via Ari · from Market
        # Traders" and lets attribution be audited.
        "forward_meta": fwd_meta or None,
    }
    user_meta = {
        "user": {"ticker": None, "side": None, "conviction": None,
                 "timeframe": None, "note": None},
        "resolved": {"ticker": None, "side": None, "conviction": None,
                     "timeframe": None, "note": None},
        "channel": author_display,
        "channel_type": "telegram",
        "telegram_original_source": attrib_slug if attrib_slug != observed_slug else None,
    }
    now = datetime.now(UTC).isoformat()
    primary_path = attachment_paths[0] if attachment_paths else None
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            INSERT INTO documents (
                document_id, source_id, title, url, published_at,
                author, content_type, raw_text, cleaned_text,
                tags_json, ingested_at, author_id,
                user_metadata_json, attachment_path,
                extracted_features_json, attachment_paths_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                f"manual:telegram-channel:{attrib_slug}",
                title,
                None,
                published_at,
                author_display,
                "manual_chart" if pending_vision else "manual_note",
                body,
                body,
                json.dumps(tags_payload),
                now,
                author_id,
                json.dumps(user_meta),
                primary_path,
                # Pre-populate with sha256 list so the vision drainer's
                # cache lookup can short-circuit when bytes already analysed.
                json.dumps({"image_sha256_list": image_shas}) if image_shas else None,
                json.dumps(attachment_paths) if attachment_paths else None,
            ),
        )
        conn.commit()
    return "imported"


# ── Stock Unlocked: push-driven ledger + book ───────────────────────────────


STOCK_UNLOCKED_SLUG = "stock_unlocked"


def push_stock_unlocked(
    base_dir: Path, *, run_book: bool = True, runner=None, timeout: float = 600.0
) -> dict:
    """Bring the Stock Unlocked ledger current, then run the copy book.

    Called from the listener the moment the desk posts. Module level and
    injectable because this is the path that places trades off a push —
    it needs a test, and a closure inside `listen()` cannot have one.

    Never raises: the caller is a Telethon event handler, and losing the
    listener costs far more than losing one tick.
    """
    report: dict = {"synced": None, "book": None, "error": None}
    try:
        from macro_positioning.tracker import stock_unlocked as su_tracker

        report["synced"] = su_tracker.sync()
    except Exception as exc:  # noqa: BLE001
        logger.exception("stock-unlocked tracker sync failed")
        report["error"] = f"sync: {exc}"
        return report
    if not run_book:
        report["book"] = "skipped"
        return report

    run = runner or subprocess.run
    script = Path(base_dir) / "scripts" / "paper_unlocked_tick.py"
    try:
        # --no-sync: the ledger is already current from the sync above.
        # Subprocess, not an import: identical to what launchd runs, and a
        # failure in the engine cannot take the Telethon session with it.
        proc = run(
            [sys.executable, str(script), "--execute", "--quiet", "--no-sync"],
            cwd=str(base_dir), capture_output=True, text=True, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — includes TimeoutExpired
        logger.exception("stock-unlocked book tick failed")
        report["error"] = f"book: {exc}"
        return report

    if proc.returncode != 0:
        logger.error("stock-unlocked book tick exited %s: %s",
                     proc.returncode, (proc.stderr or "").strip()[-400:])
        report["book"] = f"exit {proc.returncode}"
    else:
        lines = (proc.stdout or "").strip().splitlines()
        if lines:
            logger.info("stock-unlocked book tick: %s", " | ".join(lines[-6:]))
        report["book"] = "ok"
    return report


# ── Backfill mode ───────────────────────────────────────────────────────────


async def backfill(
    chat_id: int,
    slug: str,
    cfg: dict,
    *,
    since_days: Optional[int] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Walk historical messages in chat_id, grouping albums, emitting docs."""
    client = _make_client()
    await client.start()
    try:
        entity = await client.get_entity(chat_id)
    except Exception as e:  # noqa: BLE001
        await client.disconnect()
        raise RuntimeError(f"can't resolve chat_id {chat_id}: {e}") from e

    cutoff = None
    if since_days:
        cutoff = datetime.now(UTC) - timedelta(days=since_days)

    base_dir = settings.base_dir
    db_path = settings.sqlite_path

    stats: dict[str, int] = defaultdict(int)
    seen_msg_ids = 0
    is_dm = bool(cfg.get("is_dm"))
    my_id: Optional[int] = None
    if is_dm:
        me = await client.get_me()
        my_id = me.id

    # Buffer for album grouping. Telethon yields newest-first; for stable
    # grouping we accumulate then flush per grouped_id.
    album_buckets: dict[int, list[Any]] = defaultdict(list)
    standalones: list[Any] = []

    iter_kwargs: dict[str, Any] = {}
    if cutoff:
        iter_kwargs["offset_date"] = None  # iter_messages doesn't take offset_date this way; filter manually
    if limit:
        iter_kwargs["limit"] = limit

    async for m in client.iter_messages(entity, **iter_kwargs):
        if cutoff and m.date and m.date < cutoff:
            break
        # DM filter: skip user's own messages
        if is_dm and my_id is not None and m.sender_id == my_id:
            stats["skipped_own_dm_message"] += 1
            continue
        seen_msg_ids += 1
        gid = getattr(m, "grouped_id", None)
        if gid is not None:
            album_buckets[gid].append(m)
        else:
            standalones.append(m)

    # Emit standalones one at a time, albums as bundles
    for m in standalones:
        try:
            outcome = await _ingest_bundle(
                [m], chat_id, slug, cfg, base_dir, db_path, dry_run=dry_run,
            )
            stats[outcome] += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("ingest failure on msg %s", getattr(m, "id", "?"))
            stats["failed"] += 1

    for gid, msgs in album_buckets.items():
        # Sort within album so first message is earliest
        msgs.sort(key=lambda x: x.id)
        try:
            outcome = await _ingest_bundle(
                msgs, chat_id, slug, cfg, base_dir, db_path, dry_run=dry_run,
            )
            stats[outcome] += 1
        except Exception:  # noqa: BLE001
            logger.exception("ingest failure on album %s", gid)
            stats["failed"] += 1

    await client.disconnect()
    stats["messages_scanned"] = seen_msg_ids
    stats["albums_seen"] = len(album_buckets)
    return dict(stats)


# ── Listen mode ─────────────────────────────────────────────────────────────


async def listen(channel_slugs: list[str]) -> None:
    """Long-running event handler. Subscribes to NewMessage on each chat
    and processes them through the same ingest path as backfill.

    Albums arrive as separate events ~ms apart. We buffer per grouped_id
    for 2s then flush. SIGTERM interrupts cleanly via asyncio cancellation.
    """
    try:
        from telethon import events  # type: ignore
    except ImportError as e:
        raise RuntimeError("telethon not installed.") from e

    client = _make_client()
    await client.start()

    base_dir = settings.base_dir
    db_path = settings.sqlite_path

    # Resolve all configured chats up front so we know which to subscribe to.
    chat_ids: list[int] = []
    cfgs_by_id: dict[int, tuple[str, dict]] = {}
    is_dm_chats: set[int] = set()
    for slug in channel_slugs:
        cfg = settings.telegram_channels.get(slug)
        if not cfg:
            logger.warning("unknown channel slug %r; skipping", slug)
            continue
        chat_ids.append(cfg["chat_id"])
        cfgs_by_id[cfg["chat_id"]] = (slug, cfg)
        if cfg.get("is_dm"):
            is_dm_chats.add(cfg["chat_id"])

    me = await client.get_me()
    my_id = me.id

    # Per-chat album buffer: (chat_id, grouped_id) → list of messages
    album_flushers: dict[tuple[int, int], asyncio.Task] = {}
    album_buckets: dict[tuple[int, int], list[Any]] = {}

    # Auto-extract newly-captured posts with the locked vision prompt so the
    # forward pipeline is capture→analyze in one process (no manual drain).
    # Runs in a worker thread so the API call never blocks the event loop;
    # posts are infrequent (a few/hour) so a small drain per ingest is cheap.
    def _drain_blocking() -> None:
        try:
            from macro_positioning.manual.vision_drainer import drain
            drain(limit=10)
        except Exception:  # noqa: BLE001 — never kill the listener on a drain error
            logger.exception("auto-extract drain failed")

    # ── Stock Unlocked: the desk posts, the desk gets followed ──────────
    #
    # This channel states its levels in words, so its ledger is a regex
    # parse away — and it has to be immediate. ENA was called at 23:50 on
    # 2026-09-24 and printed its first target 62 minutes later; the
    # two-hourly jobs meant the ledger did not contain the call while it
    # was live and the copy book never saw it. This listener already holds
    # an open connection and fires the moment a message lands, so the
    # whole chain hangs off that event instead of a clock.
    #
    # Ledger first, in-process (fast, and it keeps the page current even
    # if the book is off). Then the book, as a SUBPROCESS running the same
    # script launchd runs: one code path, and a failure in the engine
    # cannot take the listener's Telethon session down with it.
    #
    # Debounced, because the desk posts in bursts — an entry, then three
    # target updates inside a minute. One pass once the burst settles.
    _SU_DEBOUNCE_SECONDS = 20.0
    _su_pulse: dict[str, Optional[asyncio.Task]] = {"task": None}
    # Kill switch: the book places (paper) trades off a push, so it must be
    # possible to turn that off without editing code or stopping ingest.
    _su_push_trades = os.environ.get("MPA_UNLOCKED_PUSH_TICK", "1") != "0"

    def _su_sync_and_trade_blocking() -> None:
        push_stock_unlocked(base_dir, run_book=_su_push_trades)

    async def _su_pulse_after_burst() -> None:
        try:
            await asyncio.sleep(_SU_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return                      # a newer post restarted the timer
        await asyncio.to_thread(_su_sync_and_trade_blocking)

    def _schedule_extract(outcome: str, slug: Optional[str] = None) -> None:
        if outcome != "imported":
            return
        asyncio.create_task(asyncio.to_thread(_drain_blocking))
        if slug == "stock_unlocked":
            pending = _su_pulse.get("task")
            if pending and not pending.done():
                pending.cancel()
            _su_pulse["task"] = asyncio.create_task(_su_pulse_after_burst())

    async def _flush_album(chat_id: int, gid: int, after_seconds: float = 2.0) -> None:
        await asyncio.sleep(after_seconds)
        msgs = album_buckets.pop((chat_id, gid), [])
        album_flushers.pop((chat_id, gid), None)
        if not msgs:
            return
        slug, cfg = cfgs_by_id[chat_id]
        msgs.sort(key=lambda x: x.id)
        try:
            outcome = await _ingest_bundle(msgs, chat_id, slug, cfg, base_dir, db_path)
            logger.info("album %s/%s → %s (n=%d)", chat_id, gid, outcome, len(msgs))
            _schedule_extract(outcome, slug)
        except Exception:  # noqa: BLE001
            logger.exception("album ingest failure %s/%s", chat_id, gid)

    @client.on(events.NewMessage(chats=chat_ids))
    async def _on_new_message(event):  # noqa: ANN001
        m = event.message
        chat_id = event.chat_id
        if chat_id in is_dm_chats and m.sender_id == my_id:
            return  # skip our own DM replies

        gid = getattr(m, "grouped_id", None)
        if gid is not None:
            key = (chat_id, gid)
            album_buckets.setdefault(key, []).append(m)
            if key not in album_flushers:
                album_flushers[key] = asyncio.create_task(_flush_album(chat_id, gid))
            return

        slug, cfg = cfgs_by_id[chat_id]
        try:
            outcome = await _ingest_bundle([m], chat_id, slug, cfg, base_dir, db_path)
            logger.info("msg %s/%s → %s", chat_id, m.id, outcome)
            _schedule_extract(outcome, slug)
        except Exception:  # noqa: BLE001
            logger.exception("standalone ingest failure %s/%s", chat_id, m.id)

    logger.info("listener up; subscribed to %d chats: %s", len(chat_ids), channel_slugs)
    await client.run_until_disconnected()
