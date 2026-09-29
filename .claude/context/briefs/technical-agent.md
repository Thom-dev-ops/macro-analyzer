# Worker brief: Technical agent — personalized levels ("levels pending" killer)

Drafted 2026-08-02 by the M3-setups worker from operator direction; PM to
assign (territory overlaps scoring worker: `scoring/runner.py`, `macro_brain/`,
`web/components.jsx` + `web/reasoning.jsx`).

## Operator mandate (verbatim intent, 2026-08-02)

> If we build the technical agent, it should be based off all the image
> extraction from my own personal traits and my personal appetite for
> trading, positioning, charts, etc.

This is NOT a generic ATR-level generator. The agent must encode the
operator's own chart methodology and risk appetite. Every asset below
already exists — the job is fusion, not collection.

## Why this exists

Every hero/watchlist card shows "levels pending — awaiting technical agent."
Consequences today (verified 2026-08-02):

1. `composer.py:94` returns "Risk/reward inputs incomplete" for EVERY ticker
   — the risk_reward sub-score is dead weight in the 0-100 scale.
2. No stop distance → the sizing framework (`config/risk_caps.json`
   risk-per-trade) has nothing to size against.
3. Scores can't distinguish "great name someday" from "structural entry NOW."
4. The 2026-08-02 pass: 150 rows, zero levels persisted, despite full price
   coverage for the hero tickers — `trade_scores` has no level columns and
   neither JSON blob carries them (persistence gap, see v0).

## Personal-methodology assets (all live in-repo)

| Asset | Where | What it encodes |
|---|---|---|
| Locked chart framework | `config/manual_chart_framework.md` | The operator's chart-reading methodology (SECTION 10 = JSON schema); validated 91-100% on primary fields |
| Gold labels | `training_corpus/extraction_labels.jsonl` (191, operator-verified, append-only) | Ground truth of how the operator reads structure, levels, bias |
| Weak labels | `training_corpus/weak_labels.jsonl` (6,291) | Full-corpus extractions under the locked prompt |
| Risk appetite (codified) | `config/risk_caps.json` | 1% risk/trade (1.5% high-conviction), 3-5% allocation bands (7.5-8% HC), confluence tiers 0-8, max 5 concurrent / 40% deployed / bucket caps |
| Rules engine | `src/macro_positioning/rules/` (gate, risk, portfolio, confluence, adherence) | `evaluate_trade_proposal` — proposals must PASS this, not bypass it |
| KOL levels | `signals` rows from chart calls (entry/stop/target as drawn by KOLs) | Human-drawn levels for the same tickers |
| Whose levels resolve | `learning/call_accuracy.py` `source_accuracy()` | setup_win_rate per author — weight KOL levels by demonstrated accuracy |

## Phasing

**v0 — persistence fix (small, unblocks UI):** `runner.py:527` already
synthesizes entry=close / stop=2×ATR / target=3R. Persist into
`feature_vector_json` (and reasoning trail), render in `components.jsx` /
`reasoning.jsx`. Kills "levels pending" with honest "mechanical v0" labeling.

**v1 — side- and structure-aware:** LONG vs SHORT stops; entries at
structure (20-bar breakout trigger, pullback-to-support from
`compute_technical_features`), stops below invalidation not blind 2×ATR;
setup detector gates "no structural entry" → no levels rather than fake ones.

**v2 — personalization (the mandate):**
- Cross-check mechanical levels against trusted-KOL levels for the same
  ticker, weighted by `setup_win_rate` (only `meaningful` sources).
- Learn level placement from the gold-label bank — where does the OPERATOR
  put entries/stops relative to structure on verified charts?
- Emit R/R + risk-per-trade sized per `risk_caps.json`; route every proposed
  level set through `rules.gate.evaluate_trade_proposal`; confluence tier
  determines standard vs high-conviction sizing eligibility.

## Non-goals

- No auto-execution — levels are decision support.
- Don't replace the KOL setups classifier (S8) — it's an input lens; this
  agent serves the allocator (see DECISIONS 2026-08-02 surface split).

## Open questions for PM

1. v0 label in UI: "mechanical levels (v0)" vs holding UI until v1?
2. Where does the setup detector live — `macro_brain/agents/technical_scorer`
   extension or a new agent module?
3. Vision re-extraction budget if v2 wants level coordinates re-read from
   chart images (Anthropic credits currently near-zero per STATE).
