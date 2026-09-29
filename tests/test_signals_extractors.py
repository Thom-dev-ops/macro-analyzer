"""Extractor + runner tests.

Insider extractor against a realistic insider-funneled document.
LLM extractor with the backend monkeypatched to a fake response.
Runner over a small queue with both extractors.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from macro_positioning.core.settings import settings
from macro_positioning.db.schema import initialize_database
from macro_positioning.signals import repository
from macro_positioning.signals.base import ExtractionResult, Signal, SignalSide
from macro_positioning.signals.insider_extractor import InsiderExtractor
from macro_positioning.signals.llm_extractor import LLMExtractor
from macro_positioning.signals.router import choose_extractors, is_structured_insider
from macro_positioning.signals.runner import extract_pending


@pytest.fixture
def db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "signals.db"
    initialize_database(db_path)
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "database_url", "sqlite:///signals.db")
    return db_path


def _insert_doc(db_path, **fields) -> str:
    defaults = dict(
        document_id="doc-test",
        source_id="manual:gov-insider-nancy-pelosi",
        title="Pelosi · LONG · NVDA",
        url=None,
        published_at="2026-05-01",
        author="Nancy Pelosi",
        content_type="manual_note",
        raw_text="$NVDA purchase $1,001 - $15,000",
        cleaned_text="$NVDA purchase $1,001 - $15,000",
        tags_json=json.dumps({"tickers": ["NVDA"], "tags": ["manual"]}),
        ingested_at="2026-05-01",
        author_id="gov-insider-nancy-pelosi",
        user_metadata_json=json.dumps({
            "user": {"ticker": "NVDA", "side": "LONG", "conviction": 3,
                     "timeframe": "1W", "note": "Pelosi · purchase · NVDA"},
            "resolved": {"ticker": "NVDA", "side": "LONG", "conviction": 3,
                         "timeframe": "1W", "note": "Pelosi · purchase · NVDA"},
            "channel": "gov_insider",
            "channel_type": "other",
        }),
    )
    defaults.update(fields)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO documents (document_id, source_id, title, url,
                published_at, author, content_type, raw_text, cleaned_text,
                tags_json, ingested_at, author_id, user_metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(defaults.values()),
        )
        # Seed author trust
        if defaults.get("author_id"):
            conn.execute(
                """INSERT OR REPLACE INTO input_authors
                   (author_id, display_name, channel, trust_weight)
                   VALUES (?, ?, ?, ?)""",
                (defaults["author_id"], defaults.get("author"),
                 "gov_insider", 1.2),
            )
        conn.commit()
    return defaults["document_id"]


# ── Router ──────────────────────────────────────────────────────────────────


def test_router_routes_insider_docs():
    doc = {"source_id": "manual:gov-insider-nancy-pelosi",
           "author_id": "gov-insider-nancy-pelosi"}
    assert is_structured_insider(doc)
    assert choose_extractors(doc) == ["insider_extractor"]


def test_router_routes_prose_to_llm():
    doc = {"source_id": "manual:big-nuts", "author_id": "big-nuts"}
    assert not is_structured_insider(doc)
    assert choose_extractors(doc) == ["llm_extractor"]


def test_router_defers_pending_vision_charts():
    """A chart doc still awaiting OCR (pending_vision) must not be extracted
    yet — its body is only the caption. Returns [] so it stays pending."""
    import json
    doc = {
        "source_id": "manual:telegram-channel:gem_hunters",
        "author_id": "gem-hunters:gem-hunters",
        "tags_json": json.dumps({"pending_vision": True}),
        "attachment_paths_json": json.dumps(["uploads/charts/2025-09/x.jpg"]),
    }
    assert choose_extractors(doc) == []


def test_router_runs_chart_once_vision_drained():
    """Same doc after the drainer clears pending_vision → normal fan-out."""
    import json
    doc = {
        "source_id": "manual:telegram-channel:gem_hunters",
        "author_id": "gem-hunters:gem-hunters",
        "tags_json": json.dumps({"pending_vision": False}),
        "attachment_paths_json": json.dumps(["uploads/charts/2025-09/x.jpg"]),
    }
    assert choose_extractors(doc) == ["vision_extractor", "llm_extractor"]


def test_router_handles_direct_insider_slug():
    doc = {"source_id": "form4:0001234567"}
    assert is_structured_insider(doc)


# ── Insider extractor ───────────────────────────────────────────────────────


def test_insider_extractor_happy_path(db):
    _insert_doc(db)
    extractor = InsiderExtractor()
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        doc = dict(conn.execute(
            "SELECT * FROM documents WHERE document_id='doc-test'"
        ).fetchone())

    result = extractor.extract(doc)
    assert result.status == "success"
    assert len(result.signals) == 1
    s = result.signals[0]
    assert s.asset_ticker == "NVDA"
    assert s.side == SignalSide.LONG
    assert s.conviction == 3.0
    assert s.source_slug == "gov_insider"
    assert s.source_channel == "gov_insider"
    assert s.author_id == "gov-insider-nancy-pelosi"
    assert s.author_trust_weight == 1.2
    assert s.source_trust_weight == 1.2  # _CHANNEL_TRUST["gov_insider"]
    assert s.extractor_name == "insider_extractor"
    assert s.model_provider == "rule"


def test_insider_extractor_no_tickers(db):
    _insert_doc(
        db,
        document_id="doc-empty",
        tags_json=json.dumps({"tickers": [], "tags": ["manual"]}),
        user_metadata_json=json.dumps({
            "user": {}, "resolved": {}, "channel": "gov_insider",
        }),
    )
    extractor = InsiderExtractor()
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        doc = dict(conn.execute(
            "SELECT * FROM documents WHERE document_id='doc-empty'"
        ).fetchone())
    result = extractor.extract(doc)
    assert result.status == "no_signal"
    assert result.signals == []


def test_insider_extractor_multi_ticker(db):
    _insert_doc(
        db,
        document_id="doc-multi",
        tags_json=json.dumps({"tickers": ["AAPL", "MSFT", "NVDA"],
                              "tags": ["manual"]}),
        user_metadata_json=json.dumps({
            "user": {"ticker": "AAPL", "side": "LONG", "conviction": 4},
            "resolved": {"ticker": "AAPL"},
            "channel": "corp_insider",
        }),
    )
    extractor = InsiderExtractor()
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        doc = dict(conn.execute(
            "SELECT * FROM documents WHERE document_id='doc-multi'"
        ).fetchone())
    result = extractor.extract(doc)
    assert result.status == "success"
    tickers = sorted(s.asset_ticker for s in result.signals)
    assert tickers == ["AAPL", "MSFT", "NVDA"]
    # All share the same channel + source_trust
    assert all(s.source_channel == "corp_insider" for s in result.signals)


# ── LLM extractor (mocked backend) ──────────────────────────────────────────


def test_llm_extractor_happy_path(db, monkeypatch):
    _insert_doc(
        db,
        document_id="doc-prose",
        source_id="manual:big-nuts",
        author_id="big-nuts",
        cleaned_text="rotating from QQQ into XLE — Fed pivot near",
        raw_text="rotating from QQQ into XLE — Fed pivot near",
        tags_json=json.dumps({"tickers": ["QQQ", "XLE"], "tags": ["manual"]}),
        user_metadata_json=json.dumps({"user": {}, "resolved": {},
                                       "channel": "manual"}),
    )

    fake_response = {
        "signals": [
            {
                "asset_ticker": "QQQ",
                "asset_class": "etf",
                "side": "EXIT",
                "conviction": 3.0,
                "horizon": "swing",
                "horizon_days": 14,
                "thesis_summary": "rotation out of mega-cap growth",
                "thesis_tags": ["rotation"],
                "catalyst_type": "macro_print",
                "raw_excerpt": "rotating from QQQ",
                "extractor_confidence": 0.8,
            },
            {
                "asset_ticker": "XLE",
                "asset_class": "etf",
                "side": "LONG",
                "conviction": 3.5,
                "horizon": "swing",
                "thesis_summary": "rotation into energy on Fed pivot",
                "thesis_tags": ["fed_pivot", "rotation"],
                "catalyst_type": "macro_print",
                "raw_excerpt": "into XLE — Fed pivot",
                "extractor_confidence": 0.75,
            },
        ],
        "no_signal_reason": None,
    }

    class FakeBackendResult:
        text = json.dumps(fake_response)
        model = "gemini-2.5-pro"
        latency_ms = 123.0
        input_tokens = 1200
        output_tokens = 350
        cost_usd = 0.005

    def fake_generate(backend, system_prompt, user_prompt, **_):
        return FakeBackendResult()

    monkeypatch.setattr(
        "macro_positioning.signals.llm_extractor.generate", fake_generate
    )
    monkeypatch.setattr(settings, "brain_primary_backend", "gemini", raising=False)

    extractor = LLMExtractor()
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        doc = dict(conn.execute(
            "SELECT * FROM documents WHERE document_id='doc-prose'"
        ).fetchone())

    result = extractor.extract(doc, run_id="test-run")
    assert result.status == "success"
    assert len(result.signals) == 2

    by_ticker = {s.asset_ticker: s for s in result.signals}
    assert by_ticker["QQQ"].side == SignalSide.EXIT
    assert by_ticker["XLE"].side == SignalSide.LONG
    assert by_ticker["XLE"].thesis_tags == ["fed_pivot", "rotation"]
    assert all(s.extractor_name == "llm_extractor" for s in result.signals)
    assert all(s.model_name == "gemini-2.5-pro" for s in result.signals)
    assert all(s.extraction_call_id for s in result.signals)
    # Audit row in agent_call_log
    with sqlite3.connect(db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM agent_call_log WHERE agent_name='signal_llm_extractor'"
        ).fetchone()[0]
    assert n == 1


def test_llm_extractor_no_signal_response(db, monkeypatch):
    _insert_doc(
        db,
        document_id="doc-newsy",
        source_id="manual:newsbot",
        author_id="newsbot",
        cleaned_text="The market opened mixed today, traders cautious.",
        tags_json=json.dumps({"tickers": [], "tags": ["manual"]}),
        user_metadata_json=json.dumps({"user": {}, "resolved": {},
                                       "channel": "manual"}),
    )

    class FakeBackendResult:
        text = '{"signals": [], "no_signal_reason": "pure market color"}'
        model = "gemini-2.5-pro"
        latency_ms = 50.0
        input_tokens = 400
        output_tokens = 20
        cost_usd = 0.0007

    monkeypatch.setattr(
        "macro_positioning.signals.llm_extractor.generate",
        lambda *a, **k: FakeBackendResult(),
    )

    extractor = LLMExtractor()
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        doc = dict(conn.execute(
            "SELECT * FROM documents WHERE document_id='doc-newsy'"
        ).fetchone())
    result = extractor.extract(doc)
    assert result.status == "no_signal"
    assert result.signals == []
    assert "pure market color" in (result.error_message or "")


def test_llm_extractor_malformed_json(db, monkeypatch):
    _insert_doc(
        db, document_id="doc-bad",
        source_id="manual:newsbot", author_id="newsbot",
        cleaned_text="some prose",
        user_metadata_json=json.dumps({"user": {}, "resolved": {},
                                       "channel": "manual"}),
    )

    class FakeBackendResult:
        text = "not actually JSON {{"
        model = "gemini-2.5-pro"
        latency_ms = 10.0
        input_tokens = 100
        output_tokens = 5
        cost_usd = 0.0001

    monkeypatch.setattr(
        "macro_positioning.signals.llm_extractor.generate",
        lambda *a, **k: FakeBackendResult(),
    )

    extractor = LLMExtractor()
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        doc = dict(conn.execute(
            "SELECT * FROM documents WHERE document_id='doc-bad'"
        ).fetchone())
    result = extractor.extract(doc)
    assert result.status == "error"
    assert "JSON parse failure" in (result.error_message or "")


# ── Runner ──────────────────────────────────────────────────────────────────


def test_runner_extracts_and_records_attempts(db, monkeypatch):
    # Two docs — one insider, one prose
    _insert_doc(db, document_id="doc-ins")
    _insert_doc(
        db, document_id="doc-prose",
        source_id="manual:big-nuts", author_id="big-nuts",
        cleaned_text="long AAPL into earnings",
        tags_json=json.dumps({"tickers": ["AAPL"], "tags": ["manual"]}),
        user_metadata_json=json.dumps({"user": {}, "resolved": {},
                                       "channel": "manual"}),
    )

    fake = {
        "signals": [{
            "asset_ticker": "AAPL", "side": "LONG", "conviction": 4.0,
            "thesis_summary": "earnings beat", "catalyst_type": "earnings",
            "extractor_confidence": 0.9, "raw_excerpt": "long AAPL",
        }],
        "no_signal_reason": None,
    }

    class FakeBackendResult:
        text = json.dumps(fake)
        model = "gemini-2.5-pro"
        latency_ms = 80.0
        input_tokens = 800
        output_tokens = 60
        cost_usd = 0.0016

    monkeypatch.setattr(
        "macro_positioning.signals.llm_extractor.generate",
        lambda *a, **k: FakeBackendResult(),
    )

    summary = extract_pending(limit=10, since_days=365, db_path=db)
    assert summary.docs_seen == 2
    assert summary.docs_with_signals == 2
    assert summary.signals_written == 2
    assert "insider_extractor" in summary.by_extractor
    assert "llm_extractor" in summary.by_extractor

    # Re-run should pick up zero new docs (attempts table dedupes)
    summary2 = extract_pending(limit=10, since_days=365, db_path=db)
    assert summary2.docs_seen == 0


# ── Chart docs: a failed vision run must not tombstone the doc ──────────────
#
# Regression for Sept 2026: every Telegram chart from Sep 4–22 came back
# `400 invalid_request_error: temperature is deprecated for this model`.
# The drainer called that PERMANENT and cleared pending_vision; the
# extractor called it `no_signal`; pending_documents() treats no_signal as
# terminal. Net effect — 519 charts silently stopped becoming signals, the
# ingest step logged `STEP OK 0 signals` twice a day, and the cohort paper
# book saw an empty tape while its shorts ran through their stops.


_TEMP_400 = (
    "Error code: 400 - {'type': 'error', 'error': {'type': "
    "'invalid_request_error', 'message': '`temperature` is deprecated "
    "for this model.'}}"
)


def _insert_chart_doc(db_path, doc_id: str, features: dict) -> str:
    _insert_doc(
        db_path,
        document_id=doc_id,
        source_id="manual:telegram-channel:feather_hands",
        title="Big_Nuts · chart",
        content_type="manual_chart",
        raw_text="ETH bull flag",
        cleaned_text="ETH bull flag",
        author="Big_Nuts",
        author_id="feather-hands:big-nuts",
        tags_json=json.dumps({"tags": ["manual"]}),
        user_metadata_json=json.dumps({"channel": "feather_hands"}),
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE documents SET extracted_features_json = ? WHERE document_id = ?",
            (json.dumps(features), doc_id),
        )
        conn.commit()
    return doc_id


def test_failed_vision_run_leaves_chart_doc_pending(db):
    """A retryable vision failure is `error`, not `no_signal`.

    `error` is the only status pending_documents() does not treat as
    terminal, so the doc comes back round once the vision run is repaired.
    """
    from macro_positioning.signals.manual_chart_extractor import ManualChartExtractor

    doc_id = _insert_chart_doc(
        db, "doc-chart-failed",
        {"error": "all images failed", "_images": [{"error": _TEMP_400}]},
    )
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        doc = dict(conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (doc_id,)
        ).fetchone())

    result = ManualChartExtractor().extract(doc, run_id="t")
    assert result.status == "error", "a fixable 400 is not a verdict on the chart"

    repository.record_attempt(result, db_path=db)
    still_pending = repository.pending_documents(
        limit=10, since_days=365,
        extractor_name="manual_chart_extractor", db_path=db,
    )
    assert doc_id in {d["document_id"] for d in still_pending}


def test_permanent_vision_verdict_is_terminal(db):
    """`not_a_chart` IS an answer — it must stay tombstoned, or every
    screenshot in the channel is re-read on every pass forever."""
    from macro_positioning.signals.manual_chart_extractor import ManualChartExtractor

    doc_id = _insert_chart_doc(
        db, "doc-chart-notachart",
        {"error": "all images failed", "_images": [{"error": "not_a_chart"}]},
    )
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        doc = dict(conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (doc_id,)
        ).fetchone())

    result = ManualChartExtractor().extract(doc, run_id="t")
    assert result.status == "no_signal"

    repository.record_attempt(result, db_path=db)
    pending = repository.pending_documents(
        limit=10, since_days=365,
        extractor_name="manual_chart_extractor", db_path=db,
    )
    assert doc_id not in {d["document_id"] for d in pending}


def test_request_shape_400_is_retryable_but_image_faults_are_not():
    """The drainer's classifier is what both sides key off — pin it."""
    from macro_positioning.manual.vision_drainer import _merged_is_transient

    assert _merged_is_transient([{"error": _TEMP_400}])
    assert _merged_is_transient([{"error": "overloaded_error"}])
    assert not _merged_is_transient([{"error": "not_a_chart"}])
    assert not _merged_is_transient([{"error": "no_attachment"}])
    # A 400 about the bytes themselves will fail identically on a re-run.
    assert not _merged_is_transient([
        {"error": "invalid_request_error: image exceeds 5 MB maximum"}
    ])
