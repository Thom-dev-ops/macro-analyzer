"""Project manual/vision.py output into `signals` rows.

The manual-input chart pipeline (`src/macro_positioning/manual/vision.py`)
runs a locked-prompt Claude vision extractor on every chart image forwarded
through the Telegram listener. Its structured output — ticker, call_type,
direction, bias, timeframe, pattern, setups[] with entry/stop/tp/target,
trade_stage, confluence_score — is stored on the document row as
`documents.extracted_features_json`.

This extractor does NOT re-run vision. It reads that JSON and projects it
into the `signals` schema. Free, fast, and higher-quality than the legacy
`vision_extractor` path (which was hitting "no ticker resolvable" on every
Ari-DM forward because it expected user-tagged metadata).

Multiple `setups[]` on one chart → multiple Signal rows sharing the same
document_id, ticker, and provenance but carrying different level sets.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from time import perf_counter
from typing import Any, Optional

from macro_positioning.chartlab import DESK_SOURCE_PREFIX
from macro_positioning.core.settings import settings
from macro_positioning.signals.base import (
    ExtractionResult,
    Signal,
    SignalCatalystType,
    SignalHorizon,
    SignalSide,
    SignalStatus,
)

log = logging.getLogger(__name__)


# ─── timeframe → horizon ───────────────────────────────────────────────
_HORIZON_MAP: dict[str, SignalHorizon] = {
    "1M": SignalHorizon.INTRADAY, "1MIN": SignalHorizon.INTRADAY,
    "5M": SignalHorizon.INTRADAY, "15M": SignalHorizon.INTRADAY, "30M": SignalHorizon.INTRADAY,
    "1H": SignalHorizon.INTRADAY, "2H": SignalHorizon.INTRADAY, "4H": SignalHorizon.INTRADAY,
    "1D": SignalHorizon.SWING, "3D": SignalHorizon.SWING,
    "1W": SignalHorizon.POSITION,
    "1MO": SignalHorizon.STRATEGIC,   # 1M ambiguity: minutes vs month. Vision uses "1M" for monthly.
}
# Note: the vision output uses "1M" for MONTHLY (never minutes), so remap.
_HORIZON_MAP["1M"] = SignalHorizon.STRATEGIC


def _horizon_from_timeframe(tf: Optional[str]) -> Optional[SignalHorizon]:
    if not tf:
        return None
    return _HORIZON_MAP.get(tf.strip().upper())


# ─── call_type + trade_stage → side + status ───────────────────────────
def _side_for_setup(setup: dict, feat: dict) -> SignalSide:
    """A setup's direction wins; fall back to the chart-level call_type."""
    d = (setup.get("direction") or "").lower()
    if d == "long":
        return SignalSide.LONG
    if d == "short":
        return SignalSide.SHORT
    ct = (feat.get("call_type") or "").lower()
    if ct == "directional_long":
        return SignalSide.LONG
    if ct == "directional_short":
        return SignalSide.SHORT
    return SignalSide.WATCH


def _side_no_setup(feat: dict) -> SignalSide:
    ct = (feat.get("call_type") or "").lower()
    bias = (feat.get("bias") or "").lower()
    if ct == "directional_long":
        return SignalSide.LONG
    if ct == "directional_short":
        return SignalSide.SHORT
    if ct in ("bidirectional", "no_trade", "retrospective"):
        return SignalSide.WATCH
    if bias == "bullish":
        return SignalSide.WATCH
    if bias == "bearish":
        return SignalSide.WATCH
    return SignalSide.WATCH


def _status_from_stage(feat: dict, setup: Optional[dict]) -> SignalStatus:
    """Retrospective calls & completed setups mark the signal expired."""
    ct = (feat.get("call_type") or "").lower()
    if ct == "retrospective":
        return SignalStatus.EXPIRED
    stage = (feat.get("trade_stage") or "").lower()
    if stage == "completed":
        return SignalStatus.EXPIRED
    if setup:
        s = (setup.get("status") or "").lower()
        if s in ("closed", "invalidated"):
            return SignalStatus.INVALIDATED
        if s == "completed":
            return SignalStatus.EXPIRED
    return SignalStatus.ACTIVE


# ─── conviction ────────────────────────────────────────────────────────
def _conviction(feat: dict, setup: Optional[dict]) -> tuple[float, str]:
    """Confluence score is the primary signal; setup status adjusts.

    Returns (conviction, conviction_raw).
    """
    conf = feat.get("confluence_score")
    try:
        base = float(conf) if conf is not None else 2.5
    except (TypeError, ValueError):
        base = 2.5
    base = max(0.0, min(5.0, base))

    # Watching + no_trade get a haircut vs active setups
    stage = (feat.get("trade_stage") or "").lower()
    if stage == "watching":
        base = max(0.5, base - 0.5)
    elif stage == "active":
        base = min(5.0, base + 0.25)
    if (feat.get("call_type") or "").lower() == "bidirectional":
        # A bidirectional chart is uncertain by definition
        base = min(base, 2.0)

    raw = f"confluence={conf}|stage={stage or 'unset'}|call_type={feat.get('call_type')}"
    return base, raw


# ─── ticker cleanup ────────────────────────────────────────────────────
def _clean_ticker(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    t = str(raw).strip().upper()
    if not t:
        return None
    # Ratio charts like "BTCUSD/XAUUSD" → keep as-is; the composer can
    # parse. Single-pair "BTC/USDT" also kept as-is; asset_ticker is a
    # string, and the pair encodes the crypto quote currency.
    return t


def _asset_class(feat: dict) -> Optional[str]:
    v = (feat.get("asset_class") or "").lower()
    return v or None


# ─── source metadata ───────────────────────────────────────────────────
def _source_slug(document: dict) -> str:
    sid = document.get("source_id", "") or ""
    return sid.split(":", 1)[0] if ":" in sid else (sid or "unknown")


def _channel_from_source(document: dict) -> Optional[str]:
    sid = document.get("source_id", "") or ""
    # `manual:telegram-channel:ari_gold` → `telegram-channel:ari_gold`
    if sid.count(":") >= 2:
        return sid.split(":", 1)[1]
    return None


# ─── extracted_features loader ─────────────────────────────────────────
def _load_extracted_features(document: dict) -> Optional[dict]:
    """The runner's pending_documents() query does NOT select the
    extracted_features_json column. Fetch it lazily from the DB.
    """
    raw = document.get("extracted_features_json")
    if raw:
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            pass
    doc_id = document.get("document_id")
    if not doc_id:
        return None
    try:
        conn = sqlite3.connect(str(settings.sqlite_path))
        try:
            row = conn.execute(
                "SELECT extracted_features_json FROM documents WHERE document_id = ?",
                (doc_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        log.exception("Failed to fetch extracted_features_json for %s", doc_id)
        return None
    if not row or not row[0]:
        return None
    try:
        feat = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    # Cache back onto the document so downstream code isn't surprised
    document["extracted_features_json"] = row[0]
    return feat


# ─── the extractor ─────────────────────────────────────────────────────
def _vision_failure_is_retryable(feat: dict) -> bool:
    """Did the vision run fail for a reason that a re-run could fix?

    Delegates to the drainer's classifier so "permanent" means the same
    thing on both sides of the pipeline. Infra blips and malformed-request
    400s are retryable; `not_a_chart` and `no_attachment` are verdicts.
    """
    from macro_positioning.manual.vision_drainer import _merged_is_transient

    imgs = feat.get("_images") or []
    if _merged_is_transient(imgs):
        return True
    # Single-result shape: no per-image list, just a top-level error.
    return _merged_is_transient([{"error": feat.get("error")}])


# Source namespaces whose chart reads this extractor projects.
#
# `manual:telegram-channel:` — a KOL's chart forwarded into the listener.
# `desk:chartlab:`          — a chart the desk read itself (chartlab.store).
#   Desk reads are admitted here so they become signals the bench can use,
#   but the author behind them matches no clause in SEEDED_AUTHOR_WHERE, so
#   they never reach trusted-voice consensus, conviction, or the positioning
#   maps. The operator's own opinion must not return to them wearing a KOL's
#   credential.
_ALLOWED_SOURCE_PREFIXES = ("manual:telegram-channel:", DESK_SOURCE_PREFIX)


class ManualChartExtractor:
    """Project `documents.extracted_features_json` into Signal rows.

    Applies only to `content_type='manual_chart'` docs from
    `manual:telegram-channel:*` sources where the manual vision pipeline
    has already produced structured output.
    """

    name = "manual_chart_extractor"
    version = "v1"

    def applies_to(self, document: dict) -> bool:
        if (document.get("content_type") or "") != "manual_chart":
            return False
        sid = document.get("source_id") or ""
        if not sid.startswith(_ALLOWED_SOURCE_PREFIXES):
            return False
        # Confirm structured output exists (cheap column check via load)
        feat = _load_extracted_features(document)
        return bool(feat and feat.get("ticker"))

    def extract(
        self, document: dict, *, run_id: Optional[str] = None
    ) -> ExtractionResult:
        t0 = perf_counter()
        doc_id = document["document_id"]
        feat = _load_extracted_features(document)

        if not feat:
            return ExtractionResult(
                document_id=doc_id,
                extractor_name=self.name,
                extractor_version=self.version,
                status="skipped",
                error_message="no extracted_features_json",
                latency_ms=(perf_counter() - t0) * 1000,
            )

        # A vision run that FAILED writes its error into the same column a
        # successful one writes features to, so an error blob is not an
        # empty chart — it is a chart nobody has read yet. Returning
        # `no_signal` here would tombstone the doc: pending_documents()
        # treats (success, no_signal) as terminal, so the row would never
        # be reconsidered even after the vision run is repaired. That is
        # exactly how 519 chart docs went dark behind a `temperature is
        # deprecated` 400 in Sept 2026 while the step logged STEP OK.
        # `error` keeps the doc pending, which is the truth.
        # A PERMANENT verdict (no_attachment, not_a_chart) is a real answer
        # and still earns `no_signal` below — re-running it would churn the
        # same doc every pass forever. Only the RETRYABLE class is deferred,
        # using the drainer's own classifier so the two agree by
        # construction.
        if feat.get("error") and _vision_failure_is_retryable(feat):
            return ExtractionResult(
                document_id=doc_id,
                extractor_name=self.name,
                extractor_version=self.version,
                status="error",
                error_message=f"vision did not complete: {str(feat.get('error'))[:200]}",
                latency_ms=(perf_counter() - t0) * 1000,
            )

        ticker = _clean_ticker(feat.get("ticker"))
        if not ticker:
            return ExtractionResult(
                document_id=doc_id,
                extractor_name=self.name,
                extractor_version=self.version,
                status="no_signal",
                error_message="no ticker in extracted_features_json",
                latency_ms=(perf_counter() - t0) * 1000,
            )

        call_type = (feat.get("call_type") or "").lower()
        # no_trade with no setups → an audit no_signal so the doc is done.
        setups: list[dict] = feat.get("setups") or []
        if call_type == "no_trade" and not setups:
            return ExtractionResult(
                document_id=doc_id,
                extractor_name=self.name,
                extractor_version=self.version,
                status="no_signal",
                error_message=f"call_type=no_trade | bias={feat.get('bias')} | notes captured on doc",
                latency_ms=(perf_counter() - t0) * 1000,
            )

        # Provenance shared across every setup this chart emits
        provenance = dict(
            source_slug=_source_slug(document),
            source_channel=_channel_from_source(document),
            author_id=document.get("author_id"),
        )

        asset_class = _asset_class(feat)
        horizon = _horizon_from_timeframe(feat.get("timeframe"))
        pattern = feat.get("pattern") or None
        notes = feat.get("notes") or None
        indicators = feat.get("indicators_visible") or []
        vision_model = feat.get("vision_model")
        vision_backend = feat.get("vision_backend")
        analyzed_at = feat.get("analyzed_at")

        signals: list[Signal] = []

        # If setups[] exists, emit one signal per setup. Otherwise emit a
        # single WATCH/direction signal from the chart-level fields.
        if setups:
            for i, setup in enumerate(setups):
                side = _side_for_setup(setup, feat)
                status = _status_from_stage(feat, setup)
                conviction, conv_raw = _conviction(feat, setup)
                tps = [
                    float(t) for t in (setup.get("take_profits") or [])
                    if t is not None
                ]
                signals.append(
                    Signal(
                        document_id=doc_id,
                        extraction_run_id=run_id,
                        asset_ticker=ticker,
                        asset_class=asset_class,
                        side=side,
                        conviction=conviction,
                        conviction_raw=conv_raw,
                        horizon=horizon,
                        entry_zone_low=_float(setup.get("entry")),
                        entry_zone_high=_float(setup.get("entry_high") or setup.get("entry")),
                        stop_loss=_float(setup.get("stop_loss")),
                        target_1=tps[0] if tps else None,
                        target_2=tps[1] if len(tps) >= 2 else _float(setup.get("final_target")),
                        invalidation=setup.get("invalidation") or None,
                        thesis_summary=notes,
                        thesis_tags=[p for p in [pattern] if p],
                        catalyst_type=SignalCatalystType.TECHNICAL,
                        extractor_name=self.name,
                        extractor_version=self.version,
                        extractor_confidence=_confluence_to_confidence(feat),
                        model_provider=vision_backend or "manual_vision",
                        model_name=vision_model,
                        raw_excerpt=_excerpt(document, feat, setup_idx=i),
                        instrument_detail={
                            "chart_timeframe": feat.get("timeframe"),
                            "call_type": feat.get("call_type"),
                            "bias": feat.get("bias"),
                            "trade_stage": feat.get("trade_stage"),
                            "pattern": pattern,
                            "confluence_score": feat.get("confluence_score"),
                            "indicators": indicators,
                            "final_target": setup.get("final_target"),
                            "setup_index": i,
                            "setup_status": setup.get("status"),
                            "analyzed_at": analyzed_at,
                        },
                        status=status,
                        **provenance,
                    )
                )
        else:
            # No structured setups — emit one chart-level signal that still
            # captures side + bias + pattern so the composer can see it.
            side = _side_no_setup(feat)
            status = _status_from_stage(feat, None)
            conviction, conv_raw = _conviction(feat, None)
            signals.append(
                Signal(
                    document_id=doc_id,
                    extraction_run_id=run_id,
                    asset_ticker=ticker,
                    asset_class=asset_class,
                    side=side,
                    conviction=conviction,
                    conviction_raw=conv_raw,
                    horizon=horizon,
                    thesis_summary=notes,
                    thesis_tags=[p for p in [pattern] if p],
                    catalyst_type=SignalCatalystType.TECHNICAL,
                    extractor_name=self.name,
                    extractor_version=self.version,
                    extractor_confidence=_confluence_to_confidence(feat),
                    model_provider=vision_backend or "manual_vision",
                    model_name=vision_model,
                    raw_excerpt=_excerpt(document, feat, setup_idx=None),
                    instrument_detail={
                        "chart_timeframe": feat.get("timeframe"),
                        "call_type": feat.get("call_type"),
                        "bias": feat.get("bias"),
                        "trade_stage": feat.get("trade_stage"),
                        "pattern": pattern,
                        "confluence_score": feat.get("confluence_score"),
                        "indicators": indicators,
                        "analyzed_at": analyzed_at,
                    },
                    status=status,
                    **provenance,
                )
            )

        elapsed = (perf_counter() - t0) * 1000
        return ExtractionResult(
            document_id=doc_id,
            extractor_name=self.name,
            extractor_version=self.version,
            status="success",
            signals=signals,
            latency_ms=elapsed,
        )


# ─── helpers ───────────────────────────────────────────────────────────
def _float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _confluence_to_confidence(feat: dict) -> Optional[float]:
    c = feat.get("confluence_score")
    if c is None:
        return None
    try:
        return max(0.0, min(1.0, float(c) / 5.0))
    except (TypeError, ValueError):
        return None


def _excerpt(document: dict, feat: dict, setup_idx: Optional[int]) -> str:
    caption = (document.get("cleaned_text") or "").strip().splitlines()
    head = caption[0][:120] if caption else ""
    tf = feat.get("timeframe") or "?"
    ct = feat.get("call_type") or "?"
    prefix = f"[{tf} · {ct}"
    if setup_idx is not None:
        prefix += f" · setup#{setup_idx + 1}"
    prefix += "]"
    return f"{prefix} {head}".strip()
