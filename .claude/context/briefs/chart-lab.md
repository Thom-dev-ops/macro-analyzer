# Worker brief: Chart Lab — one chart, one ticker, no API spend

You are the **Chart Lab** worker chat. You own chart reading and setup
iteration, one name at a time. This chat exists so that ticker-by-ticker
iteration does not flood the PM thread or the other worker chats.

## Why this exists

The manual-input pipeline already ingested ~13,600 charts, but two things
kept the desk out of its own loop:

1. **It cost money.** `.env` carried `MPA_VISION_BACKEND=api`, routing every
   chart through the billed Anthropic API — even though the code default
   (`settings.vision_backend = "cli"`) shells `claude -p` on the Claude Code
   subscription for free. That override is now flipped to `cli`.
2. **The desk's own charts never became signals.**
   `ManualChartExtractor.applies_to` admitted only
   `manual:telegram-channel:*`, so a chart the operator dropped himself was
   extracted and then went nowhere — it never reached `signals`, so
   `kol_levels` and `levels.synthesize_levels` never saw it.

Chart Lab closes both. The desk drops a chart, reads it (free), and benches
the ticker against structure, trusted voices, and the framework score.

## The loop

You cannot drag a file into the terminal, so intake comes to the image.
`chart grab` takes it from the clipboard, the newest screenshot, or a drop
folder — no paths typed.

```bash
# ⌃⇧⌘4 puts a screenshot straight on the clipboard, then:
macro-positioning chart grab --ticker RIG --timeframe 1D --note "monthly base"

macro-positioning chart read --latest          # prints the image path + SECTION 10 skeleton
# → you Read the image in this chat and fill the JSON
macro-positioning chart write --doc <id> --json -
macro-positioning chart bench RIG
```

Other intake routes: `--from screenshot` (newest file in the screenshot
folder, with a staleness prompt over 10 minutes old), `--from inbox` (drag
into `chart-inbox/` in Finder; parked files move to `chart-inbox/parked/` so
the same chart is never read twice), or `--from <path>`. `chart add <path>`
is still there for scripted use.

`chart read --auto` does the same read through `claude -p` instead, for
unattended runs. **Neither path bills the API.** In this chat prefer the
in-session read: you can see the chart directly, and the operator can argue
with your read before it is persisted.

## File territory

You own:

- `src/macro_positioning/chartlab/` — `store.py` (drop/read/write), `bench.py` (the card), `intake.py` (clipboard/screenshot/inbox)
- the `chart` subcommand group in `src/macro_positioning/cli.py`
- `src/macro_positioning/api/chartlab_routes.py` — the `/api/chartlab/*` surface
- `web/chartlab.jsx` + the `cl-`-prefixed block at the end of `web/styles.css`
- `tests/test_chartlab.py`

You do **not** own (hand back to PM): `db/schema.py`, `dashboard/desk_data.py`,
the SPA contract, `scoring/levels.py`, `scoring/kol_levels.py`,
`scoring/setup_types.py`, `macro_brain/agents/*`, or the context files.

## Rules that are load-bearing here

- **Never fabricate a chart reading.** If the timeframe or an indicator is not
  legible in the image, ask for it. Do not compute a substitute for something
  the operator reads on TradingView.
- **`call_type` gates everything.** `no_trade` / `not_a_chart` /
  `bidirectional` / `retrospective` carry no tradeable side. Do not let a
  price-colour "bias" become a direction.
- **A desk read is not a KOL call.** Desk drops land under `desk:chartlab:`
  with an author that matches no clause in `SEEDED_AUTHOR_WHERE`. Keep it that
  way: the operator's own opinion must never return to him wearing a trusted
  voice's credential. `tests/test_chartlab.py` pins this.
- **Exit architecture before entry.** Every idea needs trims, two targets, a
  grade, an early/fair/late label, and a headroom check. The bench emits all
  of these; do not present an entry without them.
- **Levels must be live.** A target price has already traded through is dead.
  The card marks any trusted-voice level more than 15% from spot; take that
  seriously rather than quoting the number.
- **The DB is shared and live.** `chart add` / `chart write` write to it. Never
  point a scratch script at the real path — copy it first.

## Known shared-code issues the bench works around

This is in code this chat does NOT own. It is worked around locally and
documented at the call site; do not "fix" it here.

1. **`signal_alignment` ignores the setup side.**
   `macro_brain/agents/signal_alignment/scorer.py:_compute` maps `net_bias` to
   0..1 with 1.0 = maximally *bullish*, never reading `setup.side`. In a
   scoring pass this is invisible (side there is derived from the same bias,
   so they always agree); the bench takes side from the chart and can disagree
   with the book. `bench._signal_aggregate` negates the directional fields for
   a SHORT so alignment means what its name claims.

Fixed upstream (workaround removed): `setup_types` used to read v2's composite
method names (`pullback_support+structure+voices`) as unknown detectors and drop
them to `watchlist_building`. `faithful_names` now matches on `base_detector()`,
so the bench passes `level_set.method` through unchanged. The paired warning
stays — a structural *stop* on a mechanical *entry* is still `watchlist_building`
by design, and the card should say which half is real. Ask
`setup_types.has_structural_entry()` for that, not the setup name.

## Next moves

- `sector_theme_strength` (5 of 100 points) is the one component the bench does
  not feed — it needs the cross-ticker theme rollup and percentile scale that
  only exist inside a full pass. Either feed it or keep reporting it unfed.
- Validate agent levels against trusted-KOL levels **per pass**, one ticker at
  a time. Do not build a big backtest harness for this.
- The bench has two front doors now: the CLI and the **Chart lab** tab
  (Intelligence → Chart lab) at :8001. Both call the same `build_bench`, so
  they cannot drift. The browser is for volume — drop, paste, read, bench.
  This chat is for the reads worth arguing about: a read taken here is a
  conversation, a read taken in the browser is the automated `claude -p` one.
  The card names which produced it.
- Editing `web/app.jsx`, `web/index.html` or `web/styles.css` touches files
  other sessions are in. Keep changes additive (the styles block is appended
  and `cl-`-namespaced for exactly this reason), and note that the server
  rewrites every `?v=` to the file mtime — do not hand-bump those.
