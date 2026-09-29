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
