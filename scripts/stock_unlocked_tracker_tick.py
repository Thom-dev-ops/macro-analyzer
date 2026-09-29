"""Tick the Stock Unlocked call tracker.

Parses any new channel posts into the ledger, re-walks every open call
against the tape, and prints the running scoreboard. Owned by launchd
(`com.macro.stock-unlocked-tick`, every two hours); run by hand for an
immediate refresh.

Usage:
  .venv/bin/python scripts/stock_unlocked_tracker_tick.py
  .venv/bin/python scripts/stock_unlocked_tracker_tick.py --rescore   # after a rule change
"""

from __future__ import annotations

import argparse
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

warnings.filterwarnings("ignore")

from macro_positioning.tracker import stock_unlocked as tracker  # noqa: E402


def _pct(v) -> str:
    return "—" if v is None else f"{v:.0f}%"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rescore", action="store_true",
                    help="re-walk every call, not just the open ones")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    started = datetime.now(UTC)
    result = tracker.sync(rescore=args.rescore)
    calls = tracker.load_calls()
    s = tracker.summary(calls)
    o = s["overall"]

    print(f"[{started.isoformat(timespec='seconds')}] stock-unlocked tick — "
          f"{result['posts']} posts, {result['calls']} calls, {result['new']} new, "
          f"{result['scored']} scored, {result['errors']} errors")
    print(f"  setup win rate {_pct(o['win_rate'])}  ({o['wins']}W / {o['losses']}L of "
          f"{o['resolved']} resolved) · avg R {o['avg_r'] if o['avg_r'] is not None else '—'} · "
          f"total {o['total_r']:+.2f}R · PF {o['profit_factor'] if o['profit_factor'] is not None else '—'}")
    print(f"  open {o['open']} · unresolved {o['unresolved']} · unpriceable {o['unpriceable']} · "
          f"options {s['options_summary']['n']} ({_pct(s['options_summary']['win_rate'])} on the desk's numbers)")
    if not args.quiet:
        for c in calls:
            if c.verdict == "open":
                print(f"  OPEN  {c.posted_at.date()} {c.ticker:6s} {c.trade_kind:5s} {c.direction:5s} "
                      f"entry {c.entry:g} stop {c.stop:g} T1 {c.targets[0]:g} "
                      f"last {c.last_price if c.last_price is not None else '—'} "
                      f"mfe {c.mfe_pct} mae {c.mae_pct} · horizon ends {c.horizon_end.date()}")
        for v, n in sorted(result["by_verdict"].items()):
            print(f"  {v:15s} {n}")
    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
