"""Chart Lab — desk chart drops, reads, and the bench's honesty gates.

The guarantees worth pinning down here are the ones that would silently
corrupt the desk's own judgement if they broke:

- a desk read becomes a signal, but its author never counts as a trusted
  voice (your own opinion must not return to you wearing a KOL credential)
- a read that names no trade produces no trade
- the signal count is the number of signals, not the number of keys in
  the result envelope
- `signal_alignment` is read relative to the side actually proposed
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from macro_positioning.chartlab import DESK_SOURCE_PREFIX
from macro_positioning.core.settings import settings
from macro_positioning.db.schema import initialize_database


@pytest.fixture
def db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "chartlab.db"
    initialize_database(db_path)
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "database_url", "sqlite:///chartlab.db")
    return db_path


@pytest.fixture
def chart_image(tmp_path: Path) -> Path:
    # A 1x1 PNG is enough: nothing in the store path decodes the image.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
        "426082"
    )
    path = tmp_path / "chart.png"
    path.write_bytes(png)
    return path


def _read(**over) -> dict:
    base = {
        "asset_class": "crypto",
        "ticker": "BTC",
        "timeframe": "1D",
        "call_type": "directional_long",
        "is_forward_looking": True,
        "trade_stage": "watching",
        "bias": "bullish",
        "pattern": "reclaim",
        "confluence_score": 4,
        "setups": [
            {
                "direction": "long",
                "entry": 84000.0,
                "stop_loss": 79500.0,
                "invalidation": "close under 82k",
                "take_profits": [90400.0, 97800.0],
                "final_target": 97800.0,
                "status": "pending",
            }
        ],
        "indicators_visible": ["MACD"],
        "notes": "reclaimed the shelf",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Drop + read round trip
# ---------------------------------------------------------------------------

def test_add_chart_writes_a_pending_desk_document(db, chart_image):
    from macro_positioning.chartlab import store

    drop = store.add_chart(chart_image, ticker="btc", timeframe="1D", note="shelf")

    assert drop.source_id.startswith(DESK_SOURCE_PREFIX)
    assert drop.ticker == "BTC", "ticker is normalised to upper case"

    row = store.get_chart(drop.document_id)
    assert row["content_type"] == "manual_chart"
    assert row["extracted_features_json"] is None
    tags = json.loads(row["tags_json"])
    assert tags["pending_vision"] is True, (
        "a drop must park as pending so the router defers extraction "
        "until a read lands"
    )


def test_write_extraction_persists_and_clears_pending(db, chart_image):
    from macro_positioning.chartlab import store

    drop = store.add_chart(chart_image, ticker="BTC")
    result = store.write_extraction(drop.document_id, _read(), extract_signals=False)

    assert result.ticker == "BTC"
    assert result.setups == 1

    row = store.get_chart(drop.document_id)
    feats = json.loads(row["extracted_features_json"])
    assert feats["call_type"] == "directional_long"
    assert feats["analyzed_at"], "the read is stamped with a time"
    assert json.loads(row["tags_json"])["pending_vision"] is False


def test_latest_read_returns_the_newest_read_for_a_ticker(db, chart_image):
    from macro_positioning.chartlab import store

    first = store.add_chart(chart_image, ticker="BTC")
    store.write_extraction(first.document_id, _read(notes="older"), extract_signals=False)
    second = store.add_chart(chart_image, ticker="BTC")
    store.write_extraction(second.document_id, _read(notes="newer"), extract_signals=False)

    assert store.latest_read("BTC")["notes"] == "newer"
    assert store.latest_read("ETH") is None


# ---------------------------------------------------------------------------
# Routing and the separation from trusted voices
# ---------------------------------------------------------------------------

def test_desk_chart_routes_to_the_manual_chart_extractor():
    from macro_positioning.signals.router import choose_extractors

    doc = {
        "source_id": f"{DESK_SOURCE_PREFIX}desk",
        "content_type": "manual_chart",
        "tags_json": json.dumps({"pending_vision": False}),
        "attachment_paths_json": json.dumps(["uploads/charts/2026-09/x.png"]),
    }
    assert choose_extractors(doc) == ["manual_chart_extractor"]


def test_pending_desk_chart_is_deferred_not_routed():
    from macro_positioning.signals.router import choose_extractors

    doc = {
        "source_id": f"{DESK_SOURCE_PREFIX}desk",
        "content_type": "manual_chart",
        "tags_json": json.dumps({"pending_vision": True}),
        "attachment_paths_json": json.dumps(["uploads/charts/2026-09/x.png"]),
    }
    assert choose_extractors(doc) == [], (
        "an unread chart must not fall through to the generic attachment "
        "path, which would re-analyse it through a paid vision extractor"
    )


def test_desk_author_is_not_a_seeded_trusted_voice(db, chart_image):
    """The separation the whole desk namespace exists to guarantee."""
    from macro_positioning.chartlab import store
    from macro_positioning.manual.authors import SEEDED_AUTHOR_WHERE

    drop = store.add_chart(chart_image, ticker="BTC")

    conn = sqlite3.connect(db)
    seeded = {
        r[0]
        for r in conn.execute(
            f"SELECT author_id FROM input_authors WHERE {SEEDED_AUTHOR_WHERE}"
        )
    }
    conn.close()
    assert drop.author_id not in seeded, (
        "a desk read must never count toward trusted-voice consensus, "
        "conviction, or the positioning maps"
    )


# ---------------------------------------------------------------------------
# Honesty gates
# ---------------------------------------------------------------------------

def test_a_read_naming_no_trade_produces_no_signals(db, chart_image):
    from macro_positioning.chartlab import store

    drop = store.add_chart(chart_image)
    result = store.write_extraction(
        drop.document_id,
        _read(call_type="not_a_chart", ticker=None, setups=[], trade_stage=None),
    )
    assert result.setups == 0
    assert result.signals == [], "nothing was called, so nothing is signalled"


def test_signal_count_reads_the_envelope_not_its_keys():
    """`extract_for_document` returns a dict; counting the dict counts 4."""
    from macro_positioning.chartlab.store import _signal_dicts

    envelope = {
        "run_id": "abc",
        "signals": [{"signal_id": "s1"}],
        "by_extractor": {"manual_chart_extractor": 1},
        "error_message": None,
    }
    signals, error = _signal_dicts(envelope)
    assert len(signals) == 1
    assert error is None


def test_signal_dicts_surfaces_the_extractor_error():
    from macro_positioning.chartlab.store import _signal_dicts

    signals, error = _signal_dicts(
        {"run_id": "a", "signals": [], "error_message": "boom"}
    )
    assert signals == []
    assert error == "boom"


@pytest.mark.parametrize(
    "call_type",
    ["no_trade", "not_a_chart", "bidirectional", "retrospective"],
)
def test_non_directional_reads_yield_no_side(call_type):
    """call_type gates direction — a bias must not become a trade side."""
    from macro_positioning.chartlab.bench import _side_from_read

    side, basis = _side_from_read(_read(call_type=call_type, bias="bullish"))
    assert side is None, f"{call_type} carries no tradeable direction"
    assert call_type in basis


def test_directional_read_sets_the_side():
    from macro_positioning.chartlab.bench import _side_from_read

    assert _side_from_read(_read())[0] == "LONG"
    short = _read(
        call_type="directional_short",
        setups=[{"direction": "short", "entry": 1.0, "stop_loss": 2.0}],
    )
    assert _side_from_read(short)[0] == "SHORT"


# ---------------------------------------------------------------------------
# Bench composition
# ---------------------------------------------------------------------------

def test_desk_levels_measure_the_gap_in_r():
    from macro_positioning.chartlab.bench import _desk_levels

    class FakeLevels:
        side, entry, stop, target, rr = "LONG", 84379.06, 80459.72, 96137.08, 3.0

    out = _desk_levels(_read(), FakeLevels(), close=84379.06)
    # Desk risk is 84000 - 79500 = 4500.
    assert out["risk_per_unit"] == pytest.approx(4500.0)
    assert out["rows"]["entry"]["gap_r"] == pytest.approx(
        (84379.06 - 84000.0) / 4500.0
    )
    assert out["rr"] == pytest.approx((90400.0 - 84000.0) / 4500.0)
    assert out["entry_reached"] is False, "spot is above a long entry not yet retested"


def test_desk_levels_absent_without_a_setup():
    from macro_positioning.chartlab.bench import _desk_levels

    class FakeLevels:
        side, entry, stop, target, rr = "LONG", 1.0, 0.9, 1.3, 3.0

    assert _desk_levels(None, FakeLevels(), close=1.0) is None
    assert _desk_levels(_read(setups=[]), FakeLevels(), close=1.0) is None


def test_short_orientation_flips_the_signal_reading(monkeypatch):
    """A short into a bullish book must not score as aligned.

    `signal_alignment._compute` maps net_bias with 1.0 = maximally
    bullish and never reads the side, so the bench orients the aggregate
    to the side actually proposed before scoring.
    """
    import macro_positioning.chartlab.bench as bench

    fake = {"BTC": {"n_signals": 10, "net_bias": 5.0, "long_weight": 8.0, "short_weight": 1.0}}
    monkeypatch.setattr(
        "macro_positioning.signals.aggregation.aggregate_for_tickers",
        lambda tickers, **kw: fake,
    )
    monkeypatch.setattr(
        "macro_positioning.signals.aggregation.directional_scale",
        lambda aggregates, **kw: 4.0,
    )

    long_agg = bench._signal_aggregate("BTC", "LONG")
    short_agg = bench._signal_aggregate("BTC", "SHORT")

    assert long_agg["net_bias"] == pytest.approx(5.0)
    assert short_agg["net_bias"] == pytest.approx(-5.0)
    assert short_agg["long_weight"] == 1.0 and short_agg["short_weight"] == 8.0
    assert short_agg["_oriented_for"] == "SHORT"


def test_bench_without_price_bars_refuses_to_invent_a_card(db, monkeypatch):
    from macro_positioning.chartlab import bench

    monkeypatch.setattr(bench, "load_recent_prices", lambda *a, **k: [])
    card = bench.build_bench("NOSUCH", fetch=False)

    assert card.levels is None
    assert card.grade is None
    assert any("no price bars" in w for w in card.warnings)


# ---------------------------------------------------------------------------
# Intake — you cannot drag a file into a terminal
# ---------------------------------------------------------------------------

def test_inbox_files_are_moved_aside_once_parked(db, tmp_path, chart_image):
    """A consumed chart must not be re-read on the next grab."""
    from macro_positioning.chartlab import intake

    dropped = intake.inbox_dir() / "rig daily.png"
    dropped.write_bytes(chart_image.read_bytes())
    assert [p.name for p in intake.inbox_images()] == ["rig daily.png"]

    moved = intake.mark_parked(dropped)
    assert moved is not None and moved.exists()
    assert moved.parent.name == intake.INBOX_DONE_DIRNAME
    assert intake.inbox_images() == [], "a parked chart leaves the queue"


def test_mark_parked_refuses_files_outside_the_inbox(db, tmp_path, chart_image):
    """Guard against moving arbitrary files the caller names."""
    from macro_positioning.chartlab import intake

    outsider = tmp_path / "somewhere-else.png"
    outsider.write_bytes(chart_image.read_bytes())

    assert intake.mark_parked(outsider) is None
    assert outsider.exists(), "a file outside the inbox is left where it is"


def test_parked_name_collision_keeps_both(db, chart_image):
    from macro_positioning.chartlab import intake

    for _ in range(2):
        dropped = intake.inbox_dir() / "chart.png"
        dropped.write_bytes(chart_image.read_bytes())
        assert intake.mark_parked(dropped) is not None

    parked = list(intake.inbox_done_dir().iterdir())
    assert len(parked) == 2, "the second chart must not overwrite the first"


def test_inbox_ignores_non_images(db, chart_image):
    from macro_positioning.chartlab import intake

    (intake.inbox_dir() / "notes.txt").write_text("not a chart")
    (intake.inbox_dir() / "chart.png").write_bytes(chart_image.read_bytes())

    assert [p.name for p in intake.inbox_images()] == ["chart.png"]


def test_clipboard_grab_returns_none_when_clipboard_holds_text(monkeypatch):
    """osascript exits non-zero (-1700) for a text clipboard — a normal miss."""
    import subprocess

    from macro_positioning.chartlab import intake

    def fake_run(*a, **kw):
        return subprocess.CompletedProcess(a, returncode=1, stdout="", stderr="-1700")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert intake.grab_clipboard() is None


def test_newest_screenshot_picks_the_most_recent(db, tmp_path, chart_image, monkeypatch):
    import os

    from macro_positioning.chartlab import intake

    shots = tmp_path / "shots"
    shots.mkdir()
    monkeypatch.setattr(intake, "screenshot_dir", lambda: shots)

    older = shots / "old.png"
    newer = shots / "new.png"
    for p in (older, newer):
        p.write_bytes(chart_image.read_bytes())
    os.utime(older, (1_600_000_000, 1_600_000_000))

    assert intake.newest_screenshot() == newer


def test_newest_screenshot_respects_the_freshness_window(db, tmp_path, chart_image, monkeypatch):
    import os

    from macro_positioning.chartlab import intake

    shots = tmp_path / "shots"
    shots.mkdir()
    monkeypatch.setattr(intake, "screenshot_dir", lambda: shots)

    stale = shots / "stale.png"
    stale.write_bytes(chart_image.read_bytes())
    os.utime(stale, (1_600_000_000, 1_600_000_000))

    assert intake.newest_screenshot() is not None, "no window given → still found"
    assert intake.newest_screenshot(within_minutes=10) is None


# ---------------------------------------------------------------------------
# HTTP surface — the browser front door onto the same free path
# ---------------------------------------------------------------------------

@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from macro_positioning.api.main import app

    return TestClient(app)


def test_drop_route_files_the_chart_in_the_desk_namespace(client, chart_image):
    """The browser drop must NOT file as a manual drop — desk namespace only."""
    with chart_image.open("rb") as fh:
        resp = client.post(
            "/api/chartlab/drop",
            files={"file": ("chart.png", fh, "image/png")},
            data={"ticker": "rig", "timeframe": "1D", "note": "base"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "RIG"
    assert body["source_id"].startswith(DESK_SOURCE_PREFIX)
    assert body["image_url"].startswith("/uploads/")


def test_drop_route_rejects_an_empty_upload(client):
    resp = client.post(
        "/api/chartlab/drop", files={"file": ("chart.png", b"", "image/png")}
    )
    assert resp.status_code == 400


def test_drop_route_rejects_a_non_image(client):
    resp = client.post(
        "/api/chartlab/drop", files={"file": ("notes.txt", b"not a chart", "text/plain")}
    )
    assert resp.status_code == 400


def test_charts_route_lists_what_was_dropped(client, chart_image):
    with chart_image.open("rb") as fh:
        client.post(
            "/api/chartlab/drop",
            files={"file": ("chart.png", fh, "image/png")},
            data={"ticker": "RIG"},
        )
    body = client.get("/api/chartlab/charts").json()
    assert len(body["charts"]) == 1
    assert body["charts"][0]["has_read"] is False


def test_put_read_attaches_a_read_and_shows_up_as_read(client, chart_image):
    with chart_image.open("rb") as fh:
        doc = client.post(
            "/api/chartlab/drop",
            files={"file": ("chart.png", fh, "image/png")},
            data={"ticker": "BTC"},
        ).json()

    resp = client.put(f"/api/chartlab/read/{doc['document_id']}", json=_read())
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "BTC"
    assert resp.json()["setups"] == 1

    stored = client.get(f"/api/chartlab/read/{doc['document_id']}").json()
    assert stored["features"]["call_type"] == "directional_long"
    assert client.get("/api/chartlab/charts").json()["charts"][0]["has_read"] is True


def test_read_routes_404_on_an_unknown_document(client):
    assert client.get("/api/chartlab/read/nope").status_code == 404
    assert client.put("/api/chartlab/read/nope", json=_read()).status_code == 404
    assert client.post("/api/chartlab/read/nope").status_code == 404


def test_bench_route_returns_the_card_shape(client, monkeypatch):
    """No bars in a fresh test DB — the card must still be well-formed."""
    body = client.get("/api/chartlab/bench/NOSUCH?fetch=false").json()
    assert body["ticker"] == "NOSUCH"
    assert body["levels"] is None
    assert body["grade"] is None
    assert any("no price bars" in w for w in body["warnings"])


def test_bench_route_rejects_a_bad_side(client):
    assert client.get("/api/chartlab/bench/BTC?side=SIDEWAYS").status_code == 422


# ---------------------------------------------------------------------------
# Editing and binning a drop
#
# Delete is the one operation here that destroys desk history, so the
# guarantees worth pinning are that it reaches everything the chart
# spawned and that it cannot be pointed at anything outside the desk
# namespace by id alone.
# ---------------------------------------------------------------------------

def test_update_chart_meta_redeclares_the_caption(db, chart_image):
    from macro_positioning.chartlab import store

    drop = store.add_chart(chart_image, ticker="BTC", timeframe="1D")
    store.update_chart_meta(
        drop.document_id, ticker="rig", timeframe="4h", asset_class="equity",
        note="base reclaim",
    )

    row = store.get_chart(drop.document_id)
    assert "RIG" in row["raw_text"] and "4h" in row["raw_text"]
    assert "BTC" not in row["raw_text"], "the old declaration is replaced, not appended"
    declared = json.loads(row["user_metadata_json"])["user"]
    assert declared == {
        "ticker": "RIG", "timeframe": "4h", "note": "base reclaim",
        "asset_class": "equity",
    }
    assert json.loads(row["tags_json"])["tickers"] == ["RIG"]


def test_update_chart_meta_leaves_an_existing_read_alone(db, chart_image):
    """A read is evidence of what the model saw; retyping it would leave
    the emitted signals disagreeing with their own document."""
    from macro_positioning.chartlab import store

    drop = store.add_chart(chart_image, ticker="BTC")
    store.write_extraction(drop.document_id, _read(), extract_signals=False)
    store.update_chart_meta(drop.document_id, ticker="RIG")

    feats = json.loads(store.get_chart(drop.document_id)["extracted_features_json"])
    assert feats["ticker"] == "BTC"


def test_delete_chart_takes_the_row_the_signals_and_the_image(db, chart_image):
    from macro_positioning.chartlab import store
    from macro_positioning.core.settings import settings

    drop = store.add_chart(chart_image, ticker="BTC")
    stored = Path(settings.base_dir) / drop.attachment_path
    assert stored.is_file()

    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            INSERT INTO signals (signal_id, document_id, extracted_at,
                                 asset_ticker, side, conviction, source_slug,
                                 extractor_name, extractor_version)
            VALUES ('s1', ?, '2026-09-30T00:00:00Z', 'BTC', 'LONG', 3.0,
                    'desk:chartlab', 'manual_chart', 'v1')
            """,
            (drop.document_id,),
        )
        conn.commit()

    out = store.delete_chart(drop.document_id)

    assert out["removed"]["signals"] == 1
    assert out["removed"]["documents"] == 1
    assert out["removed"]["files"] == 1
    assert store.get_chart(drop.document_id) is None
    assert not stored.exists(), "a binned chart must not leave its image behind"
    with sqlite3.connect(db) as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE document_id = ?", (drop.document_id,)
        ).fetchone()[0]
    assert left == 0, (
        "signals from a misadded chart would keep feeding the conviction "
        "and positioning maps"
    )


def test_delete_chart_refuses_a_document_outside_the_desk_namespace(db):
    """The route takes a bare document_id — the namespace check is the
    only thing between it and any of the desk's other documents."""
    from macro_positioning.chartlab import store

    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            INSERT INTO documents (
                document_id, source_id, title, published_at, content_type,
                raw_text, cleaned_text, tags_json, ingested_at
            ) VALUES ('foreign', 'telegram:feather_hands', 'someone else',
                      '2026-09-30', 'telegram_message', 'x', 'x', '{}',
                      '2026-09-30')
            """
        )
        conn.commit()

    with pytest.raises(PermissionError):
        store.delete_chart("foreign")
    with pytest.raises(PermissionError):
        store.update_chart_meta("foreign", ticker="RIG")
    assert store.get_chart("foreign") is not None

    with pytest.raises(KeyError):
        store.delete_chart("nope")


def test_drop_route_parks_a_batch_and_names_the_one_that_failed(client, chart_image):
    png = chart_image.read_bytes()
    resp = client.post(
        "/api/chartlab/drop",
        files=[
            ("files", ("a.png", png, "image/png")),
            ("files", ("b.png", png, "image/png")),
            ("files", ("notes.txt", b"not a chart", "text/plain")),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["drops"]) == 2, "one bad file must not sink the good ones"
    assert [e["filename"] for e in body["errors"]] == ["notes.txt"]
    assert body["document_id"] == body["drops"][0]["document_id"], (
        "single-file callers still read the first drop off the top level"
    )
    assert len(client.get("/api/chartlab/charts").json()["charts"]) == 2


def test_chart_routes_patch_delete_and_guard(client, chart_image):
    with chart_image.open("rb") as fh:
        doc = client.post(
            "/api/chartlab/drop", files={"file": ("chart.png", fh, "image/png")}
        ).json()
    doc_id = doc["document_id"]

    patched = client.patch(
        f"/api/chartlab/chart/{doc_id}",
        json={"ticker": "rig", "timeframe": "1D", "asset_class": "equity"},
    )
    assert patched.status_code == 200
    assert patched.json()["ticker"] == "RIG"

    listed = client.get("/api/chartlab/charts").json()["charts"][0]
    assert (listed["ticker"], listed["timeframe"]) == ("RIG", "1D")

    assert client.delete("/api/chartlab/chart/nope").status_code == 404
    assert client.delete(f"/api/chartlab/chart/{doc_id}").status_code == 200
    assert client.get("/api/chartlab/charts").json()["charts"] == []


def test_vocab_route_offers_only_contract_values(client):
    from macro_positioning.chartlab.store import SECTION_10_TEMPLATE

    body = client.get("/api/chartlab/vocab").json()
    assert "1D" in body["timeframes"] and "null" not in body["timeframes"]
    assert "crypto" in body["asset_classes"]
    for field, key in (("timeframe", "timeframes"), ("asset_class", "asset_classes")):
        allowed = {p.strip() for p in SECTION_10_TEMPLATE[field].split("|")}
        assert set(body[key]) <= allowed, (
            "the form must not offer a value the extraction contract rejects"
        )
