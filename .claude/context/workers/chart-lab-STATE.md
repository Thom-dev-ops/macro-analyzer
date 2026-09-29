# Chart Lab — worker state

_Last updated: 2026-09-25 (built; loop verified end to end on a temp DB copy)_

Kickoff brief: [`../briefs/chart-lab.md`](../briefs/chart-lab.md). Read that
first — it carries the rules and the known shared-code workarounds.

## What this chat owns

Chart reading and setup iteration, one ticker at a time, on the CLI, at no
API cost. Built on the current branch (`feat/scoring-alerts`), NOT a separate
worktree: 98 files are uncommitted there and the bench's dependencies
(`kol_levels.py`, `technicals.py`, `fetcher.py`) are among them, so a worktree
off main or off HEAD would bench against stale copies. Revisit once the branch
lands.

## Built

- `src/macro_positioning/chartlab/__init__.py` — `DESK_SOURCE_PREFIX` and desk attribution
- `src/macro_positioning/chartlab/store.py` — `add_chart`, `write_extraction`,
  `latest_read`, `read_chart_auto` (forces the free `cli` backend)
- `src/macro_positioning/chartlab/bench.py` — `build_bench()`, the setup card
- `src/macro_positioning/chartlab/intake.py` — clipboard (osascript `«class PNGf»`),
  newest screenshot, and a Finder-droppable `chart-inbox/` (parked files move aside)
- `chart {grab,add,list,read,write,bench}` in `cli.py`
- `src/macro_positioning/api/chartlab_routes.py` — `/api/chartlab/{charts,drop,read,bench}`
- `web/chartlab.jsx` + appended `cl-` styles; tab at Intelligence → Chart lab
- `tests/test_chartlab.py` — 33 tests, all green

## Changed outside the package (small, deliberate)

- `.env:16` — `MPA_VISION_BACKEND=api` → `cli`. **This is what stopped the
  spend.** Backup at `.env.bak-chartlab`. Note it also makes the Telegram
  listener's auto-drain shell `claude -p` per chart — free, but a subprocess
  with a 120s timeout where there used to be an API call. Watch listener
  throughput.
- `signals/manual_chart_extractor.py` — `applies_to` now admits
  `desk:chartlab:` alongside `manual:telegram-channel:` (`_ALLOWED_SOURCE_PREFIXES`)
- `signals/router.py` — routes the `desk` slug to `manual_chart_extractor`

## Verified

Full round trip on a **copy** of the live DB in the scratchpad (live DB never
written): `add` → `read` → `write` → `bench`.

- A `not_a_chart` read with no ticker produced **0 signals** — the gate holds.
- A directional read produced 1 signal and drove the bench's side.
- `chart bench BTC` renders price/structure/levels/trusted voices/grade/exits,
  and flags stale KOL levels (−24% from spot → "price has left this behind").
- `pytest tests/` — 1222 passed. The single failure
  (`test_regime_classifier_v1.py`) is another session's untracked in-flight
  work and touches nothing here.

## Platform surface verified in the browser

Paste-from-clipboard → `POST /api/chartlab/drop` → 200 → chart queued with a
"read it" button. `GET /api/chartlab/bench/BTC` renders the full card — same
content as the CLI, including the `unfed` marker on sector_theme_strength and
both honesty warnings. Enter in the bench box works (an earlier miss was the
test harness's key injection, not the handler).

One smoke-test document was created in the LIVE db during that check and then
deleted, guarded by assertions that it was desk-namespaced, unread and had no
signals. Live `desk:chartlab` count is back to 0.

Gotcha for the next session: the SPA lives at `/web/index.html` (`/` 307s to
it) and the server rewrites every `?v=` query to the file's mtime, so cache
busting is automatic — do not hand-bump versions in `index.html`.

## Open

- `sector_theme_strength` (5/100) is the only unfed grade component; it needs
  a pass-scoped theme rollup. Reported as `(stub)` rather than faked.
- Two shared-code bugs found and worked around, not fixed — see the brief.
  Both were filed as separate tasks.
- The `TradeRecord` landmine is untouched: `vision.py:290` validation ALWAYS
  fails on SECTION 10 output (the model requires a top-level `direction` the
  schema never emits) and silently falls through to the raw dict. The pipeline
  works *because* it fails — `model_dump()` would drop `call_type`/`setups`.
  Do not "fix" that model without fixing `ManualChartExtractor` in the same
  change.

## Next

1. Bench real names one at a time; check agent levels against trusted-KOL
   levels per pass. No big backtest harness.
2. Decide whether to feed `sector_theme_strength` or keep it honest-unfed.
