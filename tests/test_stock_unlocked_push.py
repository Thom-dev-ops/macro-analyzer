"""The push chain: a Telegram post → ledger → copy book.

This is the path that places trades without a human or a clock, so it is
tested rather than trusted. The subprocess runner is injected; nothing
here launches a tick or touches the live DB.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from macro_positioning.manual import telegram_poller as tp


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture
def synced(monkeypatch):
    """Stub the tracker so the chain can be exercised without price data."""
    calls = {"n": 0}

    class _FakeTracker:
        @staticmethod
        def sync():
            calls["n"] += 1
            return {"new": 1, "scored": 2}

    import macro_positioning.tracker.stock_unlocked as real
    for name in ("sync",):
        monkeypatch.setattr(real, name, _FakeTracker.sync)
    return calls


def test_ledger_syncs_then_the_book_runs_the_same_script_launchd_runs(synced, tmp_path):
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return _Proc(stdout="paper tick ptick_x\n  OPEN ENA")

    out = tp.push_stock_unlocked(tmp_path, runner=runner)

    assert synced["n"] == 1
    assert out["synced"] == {"new": 1, "scored": 2}
    assert out["book"] == "ok" and out["error"] is None
    assert seen["cmd"][0] == sys.executable
    assert seen["cmd"][1].endswith("scripts/paper_unlocked_tick.py")
    # --no-sync matters: the ledger was just synced in-process, and a second
    # sync inside the tick would double the work on every post.
    assert "--execute" in seen["cmd"] and "--no-sync" in seen["cmd"]
    assert seen["kw"]["cwd"] == str(tmp_path)


def test_the_kill_switch_stops_at_the_ledger(synced, tmp_path):
    def runner(cmd, **kw):                      # must never be reached
        raise AssertionError("book tick ran with run_book=False")

    out = tp.push_stock_unlocked(tmp_path, run_book=False, runner=runner)
    assert synced["n"] == 1 and out["book"] == "skipped"


def test_a_failing_book_tick_is_reported_not_raised(synced, tmp_path):
    out = tp.push_stock_unlocked(
        tmp_path, runner=lambda cmd, **kw: _Proc(returncode=1, stderr="boom")
    )
    assert out["book"] == "exit 1" and out["synced"] is not None


def test_a_hung_book_tick_cannot_escape_to_the_listener(synced, tmp_path):
    def runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))

    out = tp.push_stock_unlocked(tmp_path, runner=runner)
    assert "book:" in (out["error"] or "")


def test_a_broken_ledger_never_reaches_the_book(monkeypatch, tmp_path):
    import macro_positioning.tracker.stock_unlocked as real

    def boom():
        raise RuntimeError("db is locked")

    monkeypatch.setattr(real, "sync", boom)

    def runner(cmd, **kw):
        raise AssertionError("book tick ran on a failed sync")

    out = tp.push_stock_unlocked(tmp_path, runner=runner)
    assert "sync:" in (out["error"] or "") and out["book"] is None
