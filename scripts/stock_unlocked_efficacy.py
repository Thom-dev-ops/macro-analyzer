"""Print the Stock Unlocked Trades scoreboard.

The scoring itself lives in `macro_positioning.tracker.stock_unlocked`
— the persistent ledger behind `/unlocked` and the `com.macro.stock-
unlocked-tick` launchd job. This script is the terminal view of the same
numbers: it syncs the ledger (parsing any new posts and re-walking the
open calls), then prints every call with the tape's verdict next to the
desk's own, followed by the aggregates.

The method, briefly: walk the tape forward from the moment of the post
and let the level that printed FIRST decide, at hourly bars with the
call's own bar re-walked at 5 minutes. Options carry no stop or target —
only their premium P&L, which the desk reports and nothing here can
verify — so they are summarised separately and never mixed into the win
rate.

Usage:
  uv run python scripts/stock_unlocked_efficacy.py [--json out.json] [--no-sync] [--rescore]
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

warnings.filterwarnings("ignore")

from macro_positioning.tracker import stock_unlocked as tracker  # noqa: E402


def _pct(v) -> str:
    return "—" if v is None else f"{v:.0f}%"


def _num(v, fmt="{:+.2f}") -> str:
    return "—" if v is None else fmt.format(v)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, default=None, help="write the full result set")
    ap.add_argument("--no-sync", action="store_true", help="print the ledger as it stands")
    ap.add_argument("--rescore", action="store_true", help="re-walk every call")
    args = ap.parse_args()

    if not args.no_sync:
        tracker.sync(rescore=args.rescore)
    calls = tracker.load_calls()
    s = tracker.summary(calls)
    levelled = [c for c in calls if c.instrument != "option"]
    options = [c for c in calls if c.instrument == "option"]

    print(f"\nStock Unlocked Trades — {len(calls)} calls "
          f"({len(levelled)} with levels, {len(options)} options) · tracking since "
          f"{(s['tracking_since'] or '')[:10]}\n")
    print(f"{'date':11s} {'ticker':7s} {'kind':13s} {'dir':5s} {'tape':15s} "
          f"{'T':4s} {'R plan':>7s} {'R real':>7s} {'MFE%':>7s} {'MAE%':>7s}  desk")
    print("-" * 110)
    for c in sorted(levelled, key=lambda x: x.posted_at):
        desk = c.desk_verdict or "—"
        if c.desk_max_target:
            desk += f" (T{c.desk_max_target})"
        flag = " ≠" if (c.desk_verdict in ("win", "loss") and c.verdict in tracker.RESOLVED
                        and (c.desk_verdict == "win") != (c.verdict == "win")) else ""
        print(f"{c.posted_at.date().isoformat():11s} {c.ticker:7s} "
              f"{c.instrument + '/' + c.trade_kind:13s} {c.direction:5s} {c.verdict:15s} "
              f"{(str(c.max_target) + '/' + str(len(c.targets))):4s} "
              f"{_num(c.planned_r, '{:.2f}'):>7s} {_num(c.realized_r):>7s} "
              f"{_num(c.mfe_pct):>7s} {_num(c.mae_pct):>7s}  {desk}{flag}")

    o = s["overall"]
    print("\n── verified against the tape " + "─" * 60)
    print(f"  resolved            {o['resolved']} of {o['n']} level-bearing calls "
          f"({o['open']} open, {o['unresolved']} unresolved, {o['unpriceable']} unpriceable)")
    print(f"  setup win rate      {_pct(o['win_rate'])}  ({o['wins']}W / {o['losses']}L)")
    print(f"  avg realized R      {_num(o['avg_r'])}   (sum {o['total_r']:+.2f}R · "
          f"profit factor {_num(o['profit_factor'], '{:.2f}')})")
    print(f"  avg planned R:R     {_num(o['avg_planned_r'], '{:.2f}')} to T1 · "
          f"median {_num(o['median_hours_to_resolve'], '{:.1f}')}h to resolve")
    for axis in ("by_kind", "by_instrument", "by_direction"):
        for b in s[axis]:
            if b["resolved"]:
                print(f"  {b['key']:19s} {_pct(b['win_rate'])} win  ({b['wins']}/{b['resolved']})"
                      f"  avg R {_num(b['avg_r'])}")
    print("  target reach        " + "  ".join(
        f"≥T{r['target']} {_pct(r['pct'])} ({r['hit']}/{r['eligible']})"
        for r in s["target_reach"] if r["eligible"] >= 3))

    d = s["desk"]
    print("\n── the desk's own reporting " + "─" * 61)
    print(f"  claimed a target hit  {d['claimed_win']}   of which the tape agrees: {d['tape_agrees_win']}")
    print(f"  claimed stopped out   {d['claimed_loss']}   of which the tape agrees: {d['tape_agrees_loss']}")
    print(f"  never followed up     {d['silent_resolved']}  ({d['silent_lost']} of them lost)")
    for x in d["disagreements"]:
        print(f"    ≠ {x['ticker']:6s} {x['posted_at'][:10]}  desk says {x['desk']}, tape says {x['tape']} ({x['resolution']})")

    op = s["options_summary"]
    print("\n── options (self-reported premium P&L, unverified) " + "─" * 38)
    print(f"  {op['closed']} closed · {op['wins']}W / {op['losses']}L ({_pct(op['win_rate'])}) · "
          f"avg {_num(op['avg_pnl_pct'], '{:+.1f}')}% per position · {op['open']} open · {op['running']} running · {op['partial']} partial")
    for c in sorted(options, key=lambda x: x.posted_at):
        o_ = c.option or {}
        legs = "; ".join(
            f"{l['size_pct']:.0f}%@${l['exit_premium']} ({l['pnl_pct']:+.0f}%)" if l.get("exit_premium")
            else f"{l['size_pct']:.0f}% (no price)"
            for l in o_.get("exits") or []
        ) or "still open"
        pnl = o_.get("pnl_pct")
        tail = "" if pnl is None else f"  = {pnl:+.1f}%"
        print(f"  {c.posted_at.date()} {c.ticker:5s} {o_.get('type', ''):4s} "
              f"{(o_.get('strike') or 0):6.1f} @ ${o_.get('premium')}  →  {legs}{tail}")

    if args.json:
        args.json.write_text(json.dumps({"summary": s, "calls": [c.to_api() for c in calls]},
                                        indent=1, default=str))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
