# Worker brief: M3 — Setups classifier ("when to buy")

You are a worker chat in the Macro Analyzer project. PM coordinates; you implement inside declared file territory. This is the **M3** milestone in [`MASTER-PLAN.md`](../MASTER-PLAN.md) — the core goal of the platform.

## Why this exists

Everything up to now (extraction, per-source accuracy, theme/asset drift map) surfaces *what* is being said. The map even lets the user scrub time and see narratives migrate — a name that drifts from bottom-left → center over 4 weeks is a bearish narrative deflating; a name that stays top-left and grows is a fresh bull with money entering. **The user reads these trajectories visually today.** This brief encodes those trajectories into an automated classifier that emits **ranked trade setups with a "why."**

M3 defines the timing signal as:

> `f(conviction-weighted call density, setup freshness/decay, confluence across sources, macro/regime alignment)`

The 26-week bull/bear/lifecycle arrays on the drift map are the raw material for the first three. Skip macro/regime alignment for v1.

## Orientation (read in order)

1. [`.claude/context/STATE.md`](../STATE.md) — current state, PM/worker model
2. [`.claude/context/MASTER-PLAN.md`](../MASTER-PLAN.md) — M3 checklist and its relationship to M4 (`heroSignals[]` / `watchlist[]` stubs)
3. [`src/macro_positioning/dashboard/streams_builders.py`](../../../src/macro_positioning/dashboard/streams_builders.py) — where `build_theme_map` and `build_asset_map` live. Each theme/asset dict now carries:
   - `mentions_by_week[26]` — mention counts, oldest→newest, rightmost = current week
   - `bull_by_week[26]`, `bear_by_week[26]`, `weight_by_week[26]` — weighted directional contributions per week (mentions with `mention_only=True` skipped, weight = `max(0.01, trust) * max(0.0, conviction)`)
   - `lifecycle` (baked), plus the frontend now recomputes it per-week via `_compute_lifecycle`
   - `sources[]` — distinct source slugs
4. [`src/macro_positioning/learning/source_themes.py`](../../../src/macro_positioning/learning/source_themes.py) — per-source conviction feeds
5. [`src/macro_positioning/learning/call_accuracy.py`](../../../src/macro_positioning/learning/call_accuracy.py) — `source_accuracy()` returns `setup_win_rate` + `alpha` per source; feed this into the confluence + conviction terms
6. [`web/streams.jsx`](../../../web/streams.jsx) — current tabs. `_dirScoreAt`, `_lifecycleAt`, `_directionAt` at the top of the file mirror the classifier math you'll port to Python

## The classifier — six patterns

Every asset/theme with sufficient recent activity gets classified into **exactly one** bucket. Rules operate on the weekly arrays (all indices are `end = now`, `w0 = end - 1`).

Compute once per candidate:

```
recent_mentions      = mean(mentions_by_week[-2:])
prior_mentions       = mean(mentions_by_week[-6:-2])
peak_mentions_12w    = max(mentions_by_week[-12:])
score_now            = sum(bull_by_week[-2:] - bear_by_week[-2:]) / max(1, sum(weight_by_week[-2:]))
score_prior          = sum(bull_by_week[-6:-2] - bear_by_week[-6:-2]) / max(1, sum(weight_by_week[-6:-2]))
lifecycle_now        = _compute_lifecycle(mentions_by_week)   # already in streams_builders.py
momentum             = (recent_mentions - prior_mentions) / max(1, prior_mentions)
confluence           = distinct source count over last 4 weeks
weeks_active         = count(mentions_by_week[i] > 0)
```

Then classify (first match wins):

| Pattern | Rule | Meaning |
|---|---|---|
| `emerging_bull` | `weeks_active <= 3` AND `score_now > 0.3` AND `recent_mentions >= 2` | New bullish narrative, few people talking. Watch window. |
| `confirming_bull` | `score_now > 0.3` AND `momentum > 0.5` AND `lifecycle_now < 0.4` AND `confluence >= 3` | Multiple sources piling in, still fresh — actionable entry. |
| `crowded_bull` | `score_now > 0.3` AND `lifecycle_now > 0.6` | Late-cycle consensus. Exit / avoid. |
| `flipping_sentiment` | `sign(score_now) != sign(score_prior)` AND `abs(score_now - score_prior) > 0.4` | Direction reversal in the last month. Big warning if you're already in. |
| `zombie` | `recent_mentions > 0.5 * peak_mentions_12w` AND `abs(score_now) < 0.2` AND `weeks_active > 8` | Still talked about but no directional consensus. Rotation setup. |
| `dead` | `recent_mentions < 0.2 * peak_mentions_12w` OR (all recent buckets zero after live-tail gate) | Past narrative. Not a setup. |
| `neutral` | fallback | No signal. |

Only `emerging_bull`, `confirming_bull`, `flipping_sentiment`, and `zombie` are surfaced as setups. `crowded_bull`, `dead`, `neutral` are computed but not shown (they're the "why NOT" companion signals).

The same rules with sign flipped detect `emerging_bear` / `confirming_bear` / `crowded_bear` — v1 can emit these too, or gate behind a "show shorts" flag.

## Entry score

For each surfaced setup:

```
entry_score = freshness * momentum_norm * conviction * confluence_norm
```

where:
- `freshness = 1 - lifecycle_now`  (fresher = higher)
- `momentum_norm = clamp((momentum + 1) / 3, 0, 1)`  (tanh-ish squash, capping at 2x growth)
- `conviction = mean of source_accuracy.conviction across sources[]` — pull from `source_themes.py` /`call_accuracy.py`; fallback to per-source `trust_weight` when accuracy sample is under `_MIN_SAMPLE`
- `confluence_norm = min(confluence / 5, 1)`  (5+ sources = full credit)

Rank descending, cap at top 20.

## Payload shape (add to `build_streams_section`)

```python
{
    "setups": [
        {
            "id":          "SOL",
            "kind":        "asset",              # or "theme"
            "pattern":     "confirming_bull",
            "entry_score": 0.72,
            "score_now":   0.61,
            "lifecycle":   0.28,
            "momentum":    1.4,                  # 140% w/w mention growth
            "confluence":  4,
            "sources":     ["big_nuts", "og_whales", ...],
            "why":         "score +0.61 · momentum +140% w/w · 4 sources agree · lifecycle 0.28 (fresh)",
            "trajectory":  {                     # last 6 weeks for the sparkline
                "mentions": [3, 5, 8, 12, 20, 24],
                "score":    [0.1, 0.2, 0.3, 0.5, 0.55, 0.61],
            },
        },
        ...
    ]
}
```

## Where to wire

- **Backend:** new `build_setups(conn, *, now=None)` in [`streams_builders.py`](../../../src/macro_positioning/dashboard/streams_builders.py) alongside `build_asset_map` / `build_breakouts`. Add to `build_streams_section`'s output dict.
- **Frontend:** new tab `setups` in [`web/streams.jsx`](../../../web/streams.jsx) — model after the `breakouts` tab. Card per setup with pattern chip, entry_score bar, why-line, and a 6-week sparkline. Order the tab list so Setups is second (after Themes).
- **Do NOT** touch `heroSignals[]` / `watchlist[]` in `desk_data.py` in this pass. Those are M4. This brief ships the intelligence; the funnel plumbing is a separate handoff.

## Tests

Add to [`tests/test_streams_builders.py`](../../../tests/test_streams_builders.py):
- `test_build_setups_emerging_bull` — insert 3 bullish signals in the last 2 weeks with no prior activity → pattern is `emerging_bull`.
- `test_build_setups_confirming_bull` — 4 sources, growing mention count, positive score, lifecycle < 0.4 → `confirming_bull`.
- `test_build_setups_crowded_bull_not_surfaced` — high score + lifecycle > 0.6 → classified but NOT in output list.
- `test_build_setups_flipping_sentiment` — 4 weeks bearish then 2 weeks bullish → `flipping_sentiment`.
- `test_build_setups_dead_not_surfaced` — activity 12 weeks ago, silent since → filtered by live-tail (see `_LIVE_TAIL_WEEKS` in `streams_builders.py`), never reaches classifier.
- `test_build_setups_ranking` — three setups with different `entry_score` → ordered descending.
- `test_build_setups_empty_corpus` — no signals → `[]`, no exception.

## Non-goals (v1)

- No macro/regime alignment term (M3 line 4 — deferred; needs a cross-module dep on the regime classifier).
- No per-setup risk sizing / stops / targets. Those come from the KOL calls themselves; this brief ranks *which* setups deserve attention.
- No push notifications / alerts. Read-only surface for now.
- No `heroSignals[]` / `watchlist[]` un-stubbing — M4.

## Open decisions to raise back to PM before starting

1. **Surface placement** — new Setups tab in `/streams`, or feed straight into the `heroSignals[]` / `watchlist[]` stubs (M4)? Leaning tab-first, funnel later.
2. **Pattern scope** — full six patterns (recommended, matches the mental model), or MVP with `emerging_bull` + `confirming_bull` + `flipping_sentiment` only?
3. **Bear symmetry** — emit `emerging_bear` / `confirming_bear` in v1, or long-only until user asks?
4. **Conviction fallback** — when a source has <10 backtested calls, fall back to `trust_weight` (current default) or exclude that source's contribution from the conviction term?

## Coordination

- Branch: `claude/m3-setups` (PM will create the worktree).
- Territory: `src/macro_positioning/dashboard/streams_builders.py`, `web/streams.jsx`, `tests/test_streams_builders.py`. Anything else → hand back to PM.
- Schema-change protocol from [`.claude/context/briefs/README.md`](README.md) applies. No schema changes expected — everything you need is already in the weekly arrays.
- When done: summary of files touched, tests added, entry_score distribution on live data (`build_setups(conn)` output length and top-5 patterns), open questions.
