# Decisions Log

Append-only. Never delete entries. Most recent first.

---

## 2026-08-24 — Alerts: the tracker pushes, it no longer waits to be read

**Decision:** A new `macro_positioning.alerts` package turns scoring-pass
*state changes* into a Telegram push, delivered by a new hourly launchd
job (`com.macro.alert-watch`, `scripts/alert_watch.py`). Four decisions
inside that are worth recording:

1. **Provenance beats heuristics for filtering passes.**
   `trade_scores.pass_kind` (`scheduled` | `scheduled_delta` | `manual` |
   `whatif`) is now stamped by `run_scoring_pass`, and the alert
   evaluator reads only the two scheduled kinds. Row-count and score
   heuristics were tried first and were wrong: the 2026-08-21 A→D→A
   "round trip" was not a partial pass, it was hand-run passes scored
   under `transitional_chop` (−8) sitting in the same table as scheduled
   ones under `monetary_debasement_hard_asset` (+6). Historical rows were
   backfilled by launchd slot time (~10:0x / ~17:0x UTC → scheduled).

2. **The hourly pass writes changes, not snapshots.**
   `run_scoring_pass(skip_unchanged=True)` skips a ticker whose score,
   grade, tier and signal alignment all match its last row. A quiet hour
   writes ~3 rows instead of 66, which is what makes hourly cadence
   affordable on a live shared DB. Consequence: `desk_data` `dScore` now
   means "change since the score last moved", not "change since the last
   pass" — strictly more informative, but it is a semantic shift.

3. **One message per cycle, not one per alert.** A regime-modifier flip
   moves the whole board: 2026-08-20 crossed 14 names at once off a
   single cause. Per-alert delivery is how a channel gets muted, which
   would reproduce the original bug in a new place. `notify.send_batch`
   digests a cycle, keeps full levels detail for the loudest alert, and
   collapses the tail past `alert_digest_max_lines`.

4. **Telegram bot, not the Telethon user session.** The listener holds an
   exclusive lock on `data/telegram.session`; a second process opening it
   risks wedging ingestion (CLAUDE.md). A bot is a separate identity with
   no shared state, so the alert path cannot take ingestion down.

**Rationale:** The scoring layer already knew. ETH was graded A (82) on
2026-08-17 and tier_1 (86) on the 20th; BTC crossed A (83) on the 20th —
all before or at the breakout, all invisible because the only delivery
mechanism was "the operator opens the SPA." Two trades were missed.

**Validated by replay, not by assertion.** `rules.evaluate(as_of=...)`
replays history on a DB copy. Over 34 scheduled passes (2026-08-01 →
08-24) the shipped ruleset fires on ETH 08-17 and BTC 08-20 — the two
missed trades — at **0.65 messages/day**. The first draft fired 3.1
alerts/day; two thirds of that was `score_jump` landing in the D band
("QQQ +18 to 54"), hence `alert_score_jump_min_score=75`.

**Amended same day — the watch band at 75.** The operator asked to hear
about anything clearing 75, below the framework's own A boundary at 80.
Added as `grade_cross_watch` (medium severity, priority below the A and
tier_1 crossings). It buys real lead time: ETH cleared 75 on 08-12, five
days before its A cross and a week before the candle.

Two things fell out of that, both caught by replay rather than review:

- **Cooldown must be applied after per-ticker dedupe, not before.** One
  move can clear 75, 80 and 85 at once. Filtering by cooldown first let a
  suppressed `grade_cross_a` fall through and re-announce the same move
  as a quieter watch item.
- **Band re-arm belongs on the exit, not the entry.** Scores parked on a
  boundary re-fired every ~48h ("LMT cleared 75 · 75" on the 18th, 20th
  and 22nd). The first fix — requiring a crossing to *start* 3+ below the
  band — silenced ETH's 78 → 82 into A and BTC's 83 → 88 into tier_1,
  i.e. the whole point of the system: a band crossing normally does start
  just below the band. Replaced with `_REARM_MARGIN`: a band can only
  announce again once the score has actually dropped 3+ below it. Net
  0.76 messages/day with all four ETH/BTC alerts intact.

**Notification line format and colour semantics (operator-directed).**
The line is `<dot> TICKER  before→after  GRADE · tier  SIDE`, e.g.
`🟢 ETH  74→86  A · tier_1  LONG`. Colour encodes **conviction, not
alarm**: 🟢 high (crossed into A or tier_1 — tradeable), 🟡 medium
(cleared the 75 watch band), 🔴 low (a big jump that hasn't cleared a
band). The first cut used 🔴 for the strongest signals, which reads
backwards to anyone who looks at markets all day. `score_jump` dropped
from medium to low severity so all three colours carry distinct meaning.

`SIDE` resolves from `levels.side` (the side the technical agent laid
rails on — the actual proposed trade) and falls back to
`signal_bias.direction` (voice consensus) when no levels exist, which is
~12 of 79 scored tickers. It is **omitted, never defaulted**:
`side_from_signal_bias` returns LONG for anything that isn't a confident
short, so printing that as direction on a neutral name would be a
fabricated read.

**Telegram delivery is live** (bot `8854570939`, DM chat). Note for
future work: httpx logs the request URL at INFO, and the bot token is a
*path segment* of every Telegram API URL — unguarded, the hourly launchd
job would write the token to `~/Library/Logs/macro-alert-watch.err.log`
once an hour. `notify._quiet_httpx()` and `notify._redact()` close that;
the same exposure exists today for `MPA_FRED_API_KEY`, which is a query
parameter on every FRED URL the ingest job logs.

**Scope deliberately cut:** exit-side rules (stop breached, score
collapse) and price-vs-trigger-level crossing were considered and left
out of v1. Adding either is a new rule in `rules.py`; store, cooldown,
digest and delivery already handle them. There is no in-app alert feed —
the `alerts` table holds the record, nothing renders it yet.

---

## 2026-07-20 — `signal_alignment` is a first-class scoring component (15 pts)

**Decision:** The tracked-voice conviction aggregate
(`signals.aggregation.aggregate_for_ticker` → net long-vs-short weight
across KOL calls + insider filings + newsletters, recency- and
trust-weighted) is now a 9th scoring component, `signal_alignment`,
weighted **15/100**. Funded by trimming `macro_alignment` 20→15,
`sector_theme_strength` 10→5, `relative_strength` 5→3,
`psychological_execution_quality` 5→2. New `COMPONENT_WEIGHTS` still
sums to 100 (`macro_brain/types.py`). Scorer:
`macro_brain/agents/signal_alignment/scorer.py`, mapping
`0.5 + 0.5·clamp(net_bias/12, −1, +1)` with an AVOID-voice penalty; the
DB column `trade_scores.signal_alignment_score` was repurposed from the
raw 0..10 aggregate to the weighted 0..15 contribution (raw value still
lives in `signal_aggregate_json`).

**Rationale:** The signals pipeline was fully built, wired, and
persisted, but weighted **0** — so who's-positioning conviction, this
project's entire premise, could not move a ranking. Making it a
weighted component is what closes "sources → trustworthy conviction →
score." 15pts makes it a real driver, just under macro/technical (which
score the setup's *impersonal* quality), without letting it dominate the
chart read. Donor split favors trimming the two near-stub components
(psych, RS) and the *thematic* crowd signal (sector_theme) that
signal_alignment's *ticker-specific* crowd signal partly subsumes.

**Alternatives:** 0pts shadow-mode (rejected — user wanted it live);
10pts / 20pts (rejected — 15 balances differentiator-weight vs. not
swamping technicals); halving macro to 10 (rejected — macro stays a
top-tier component).

## 2026-06-09 — Source accuracy = SETUP-resolution (target-before-stop), not buy-and-hold

**Decision:** Per-source call accuracy is measured by `setup_win_rate` —
direction-aware "did price hit the take-profit before the stop within a
timeframe horizon" — NOT by `dir_win` (fixed-horizon forward return). The
backtester (`learning/call_accuracy.py`) only scores actionable
directional_long/short calls (excludes no_trade/not_a_chart/retrospective/
bidirectional) on the Coinbase-tradeable universe. Still TODO to fully trust
the numbers: winsorize r_multiple outliers, min-sample gate, ticker validation,
and a market-relative ALPHA measure (call return − BTC) to strip beta.

**Rationale:** The full-corpus backtest showed `dir_win` dominated by market
beta — crypto trended down over the window, so every source scored <50% / negative
avg-return regardless of skill. OG Whales (12% dir win but 81% setup win) is the
proof: their calls hit targets 81% of the time, but a dumb buy-and-hold-to-horizon
would have lost. Setup-resolution measures whether the *specific trade* worked,
which is what "is this source good" actually asks.

**Alternatives:** buy-and-hold forward return (rejected — measures beta not skill);
whole-corpus incl. shitcoins (rejected for scoring — untradeable, but kept for training).

## 2026-06-04 — Chart-vision extraction rebuilt to be caption- + context-aware

**Decision:** The vision extraction now reads the paired Telegram message
(`caption`) alongside the chart and outputs a strict JSON schema with
`call_type` (directional_long/short, bidirectional, retrospective, no_trade,
not_a_chart), `trade_stage` (watching/active/completed), `bias` = CURRENT
sentiment, plus nut-box/Elliott/dominance rules. Validated to 91% primary-field
row accuracy on 173 hand-verified charts and locked. Prompt in
`config/manual_chart_framework.md`; domain rules in memory (feedback_trade_call_types,
feedback_multistage_trades, reference_kol_slang).

**Rationale:** The original image-only extraction tagged ~everything "bullish
directional_long" — it ignored the message (which states the actual call:
direction, target, whether it already played out, conditional vs triggered),
and had no concept of bidirectional/retrospective/not-a-chart or trade lifecycle.
That made conviction/themes/accuracy untrustworthy. A manual verify-loop
(web/verify.html) surfaced each failure class; each became a prompt rule.

**Alternatives:** keep image-only (rejected — flying blind); per-image analysis
then merge (kept for albums, but caption now passed to every call); chase
trade_stage to 90%+ (rejected — inherently fuzzy even by hand; accepted as
secondary at 88%).

## 2026-06-04 — Extract the FULL corpus (cost not a constraint); training keeps shitcoins

**Decision:** Re-drain ALL ~6.3k charts with the locked prompt (not just the
tradeable subset), because: (a) accuracy backtesting scores RESOLVED trades, so
older history is the most valuable; (b) the full set 5×'s the weak-label
training corpus, and shitcoins are valid pattern-learning examples even though
they're excluded from scoring (untradeable on Coinbase). Scoring/conviction
still filter to the tradeable universe; TRAINING keeps everything.

**Rationale:** Earlier leaned "skip full run" when cost-sensitive, but with ~$35
acceptable the value flips — one good pass future-proofs both analytics and the
custom-model dataset. Run hit the credit wall at ~822; rest re-queued for resume.

## 2026-06-04 — Telegram dedupe/conviction keys on distinct AUTHOR, not channel

**Decision:** Cross-source conviction amplification (+0.25 per confirmation) keys
on distinct real-world *author identity*, not on distinct channel. Implemented via
three maps in `telegram_poller.py`:
- `_POST_AUTHOR_ALIASES` — forward `post_author` signature → seeded author
  (BigNuts/MadDog31/joejoe55 → Feather Hands family authors). Resolves Ari Gold's
  mass-relayed DM content to its true author.
- `_AUTHOR_IDENTITY` — collapses the Feather Hands crew + Feather Hands Trading
  channel + Market Traders groups into one `feather-hands-family` canonical identity.
- `_RELAY_AUTHOR_IDS = {ari-gold:ari-gold}` — pure relays never count as a
  confirming voice.
`_annotate_duplicate()` on a byte (sha256) match: same identity → `repost_count`;
relay → `relayed_count`; genuinely different author → `also_called_by` (the +0.25 driver).

**Rationale:** BigNuts broadcasts the same chart across Feather Hands, Market Traders,
and via Ari's relay. Channel-based crossover counted these as 3 independent votes,
massively inflating one source. User directive: "only discredit dupes... when cross
posted dedupe and rate with perhaps a .25 increase." Author identity is the only key
that distinguishes genuine independent confirmation (OG Whales sister-group landing on
the same setup) from one person echoing themselves. Verified: 85 raw → 11 genuine
confirmations (all OG Whales ↔ Big_Nuts, which the user explicitly wants to count).

**Alternatives:** (a) channel-based `also_seen_in` (original impl — rejected, inflates
BigNuts). (b) Register Market Traders monthly groups as tracked channels (rejected —
rolling monthly instances, and still wouldn't fix the family-identity problem).
(c) OCR the TradingView watermark to attribute joejoe55 separately (deferred — BigNuts
post_author granularity is the practical ceiling from TG metadata).

## 2026-06-04 — Telegram poller is read-only by construction (import-time guard)

**Decision:** `telegram_poller._enforce_read_only()` runs at import and raises if any
send/forward/delete/edit/reply token appears in the module source (tokens built from
split fragments so the guard doesn't trip itself).

**Rationale:** User's hard constraint — "We cannot get our Telegram banned." Read-only
MTProto calls (iter_messages, download_media, events.NewMessage) are indistinguishable
from normal client usage; any write path is a ban/TOS risk. Belt-and-suspenders on the
never-post promise so a future edit can't silently introduce a write.

**Alternatives:** Trust code review only (rejected — too easy to regress). Telegram
Desktop JSON export (rejected — user is not admin on these channels).

## 2026-06-01 — Insiders channel: reuse the manual-input pipeline, don't fork

**Decision:** The 10 free public-disclosure scrapers (House/Senate PTRs,
SEC Form 4/13F/13D-G, USAspending, LDA, ApeWisdom, StockTwits, UW) emit
source-agnostic `ScrapedEvent` objects, which a single `insiders.ingest.funnel()`
rewrites into `ManualInputPayload` and pushes through `manual.processor.ingest()`.
No new routing rules, no new mention/tag plumbing, no per-channel special-casing.

**Rationale:**
- `config/source_routing.json` already routes `manual` → all four downstream
  agents. `pre_tagger` already auto-tags manual drops. Forking would have meant
  re-implementing all of that.
- Per-author leaderboard machinery already keyed on `input_authors.author_id`
  via `documents.author_id` — every Congress member, Form-4 filer, hedge-fund
  CIK, and lobbying registrant naturally becomes a row, no schema changes.
- Each scraper is now ~150 LOC of source-specific parsing + a shared funnel.
  Adding the 11th source is one new module + one line in `cli.SOURCES`.

**Alternatives considered:**
- Standalone `insiders_documents` table + parallel pipeline → would have
  duplicated the manual-input fan-out logic and made the per-author
  leaderboard a join across two tables.
- Per-source FastAPI endpoints (one per scraper) → unnecessary HTTP layer
  for code that runs in-process from the morning_run scheduler.

---

## 2026-06-01 — Author = disclosing principal, not the related party

**Decision:** When a PTR or Form 4 discloses a position held by a spouse,
trust, LLC, or other related party, the `input_authors` row is keyed to
the **disclosing principal** (the Congress member, the Section-16 filer).
The related-party linkage is preserved in `user_metadata_json` and the
body text the mention_extractor sees.

**Rationale:** Per-author hit-rate aggregation needs one row per principal.
A Pelosi-self PTR and a Paul-Pelosi-spouse PTR are the same signal source
from the leaderboard's POV — splitting them would dilute attribution and
make the learning loop's per-author calibration meaningless.

**Alternatives considered:**
- One author row per actor (self, spouse, trust, ...) → fragments the
  leaderboard and creates duplicate rows for what's really one source.
- Drop related-party rows entirely → throws away signal explicitly
  required by the user ("family members, close friends, things of that sort").

---

## 2026-06-01 — Lobbying graph layer in scope for v1 (not deferred)

**Decision:** The LDA scraper writes typed edges to a new `lobbying_edges`
table (5 edge kinds, node-namespacing via `client:` / `registrant:` /
`lobbyist:` / `agency:` / `issue:` / `prev_role:` prefixes), backed by a
new `/api/insiders/lobbying-graph` endpoint and a `/05 influence` SPA tab
that renders a force-directed graph in-browser with a small Verlet-style
layout (~250 ticks, no external lib).

**Rationale:**
- LDA filings as a table of names burys the signal; the graph is the
  answer to the user's actual question ("where their funds are going").
- The data shape (typed edges with node-namespacing) is non-trivial
  enough that retrofitting later costs more than building now.
- In-browser SVG force layout keeps the SPA's all-CDN dependency story
  intact (no `react-force-graph-2d` bundled, no plotly).
- Sankey was scoped out; the force-directed view + side panel covers the
  user's intent at lower complexity.

**Alternatives considered:**
- `react-force-graph-2d` from CDN → another script tag + version pinning
  pain; the home-grown Verlet relaxation is ~50 LOC and fast enough at
  the per-quarter LDA scale.
- Defer the graph; ship the LDA scraper as just-another-channel → plan
  explicitly called this out as the high-leverage piece; would have made
  the source feel "shipped but unusable".


## 2026-05-09 — manual_entry/baseline_seed/ as the seed corpus location; vendor/ is source-only

**Decision:** The trading_agent's 392 chart screenshots (the foundational
set its chart-vision behaviors were derived from) live at
`manual_entry/baseline_seed/` in the main repo root. The `manual_entry/`
folder is a filesystem-only capture surface — root-level loose images are
the user's staging area; subfolders (`baseline_seed/`, future categorized
batches) hold curated sets. NOT git-tracked (142MB).

Separately, `vendor/trading_agent/` contains only the 2.3MB of source code
needed for porting (analysis/, agent/, signals/, config/, docs/, top-level
scripts) — explicitly excludes `dashboard/node_modules`, `trade_images/`,
`data_cache/`, `logs/`, `.git`. Reference-only; do not import.

Piece 2 bootstrap will programmatically drain `manual_entry/baseline_seed/`
through `/api/manual/ingest`, attributing all 392 images to a synthetic
author `archive:trading_agent_baseline`. Until Piece 2 ships, nothing in
`manual_entry/` has a `documents` row.

**Rationale:** Two distinct concerns — read-only training corpus (large,
filesystem-natural) vs. source-code reference (small, code-natural).
Conflating them puts 142MB of binaries next to Python files. User
explicitly framed the trade_images as "the foundational knowledge base
the trading_agent's behaviors were derived from" — that's training corpus
material, not vendor source.

**Alternatives:** Move trade_images into `vendor/trading_agent/trade_images/`
(rejected — bloats the vendored ref); leave at original
`trading_agent/trade_images/` indefinitely (rejected — user wants single
location for manual stuff); put under `data/manual_corpus/seed/` (rejected
— user explicitly created `manual_entry/` and that's the right name).

---

## 2026-05-09 — Manual input layer: relocate trading_agent into repo as vendor/, port not import

**Decision:** When building the manual input layer, first relocate the
sibling project `/Users/thom/Documents/Personal/Code Projects/trading_agent/`
into the macro-analyzer repo as `vendor/trading_agent/` (filesystem move,
preserved as a read-only reference). Then **selectively port** specific
files into `src/macro_positioning/manual/` rather than importing from
`vendor/`. Specifically: port `TradeRecord` Pydantic model, `chat_analyzer.py`,
`image_analyzer.py` prompt + schema, and `chart_analysis_framework.md`.
Do NOT port `image_analyzer.py`'s Anthropic API call — Piece 2 vision will
reuse the existing Gemini path in `brain/vision.py`.

Manual input layer also adds: new `input_authors` table for first-class
author/channel attribution (gap in trading_agent), four nullable columns
on `documents` (append-only schema change), and a dedicated `/inbox` SPA
route (4th nav tab). Build Piece 1 only first (capture + DB + UI, no LLM).

**Rationale:** trading_agent has working chart-vision, chat-export parsing,
a comprehensive `TradeRecord` schema, and a 352-line chart-analysis prompt
that took real effort to author. Rewriting any of it would be wasteful.
Vendoring keeps the source next to the work for diff-based porting and
avoids cross-repo path coupling. Single Gemini vision backend (already
unlimited on the account) is simpler than two LLM providers in the codebase.
Author/channel as first-class fields is necessary for the user's stated
goal of long-term per-author hit-rate tracking — free-text source_id
won't aggregate.

**Alternatives:** Import trading_agent as a Python package (rejected —
brittle path coupling, two repos to keep coherent); rewrite from scratch
in macro_positioning style (rejected — wastes the framework prompt and
schema work); use Claude Opus for vision per trading_agent's original
choice (rejected — Gemini is already wired, free, and equally capable for
chart structure).

---

## 2026-05-09 — LLM stack: Gemini for vision, separate deep_research slot for narrative; no own-LLM yet

**Decision:** Use Gemini 2.5 Pro (already wired, unlimited on the account)
for all chart vision and current-state synthesis tasks. Reserve a SEPARATE
future `deep_research` agent slot for narrative synthesis on the live web —
intended provider Perplexity Deep Research or OpenAI deep-research, called
under strict budget guards (per-call cost cap, per-day cap, only on
high-conviction setups). Do NOT conflate vision (Gemini, cheap, recurring)
with deep research (Perplexity, expensive, rare). Building our own LLM is
deferred until `training_corpus/` has years of outcome-labeled examples —
the logging contract is the runway for that.
**Rationale:** Right tool per job. Gemini multimodal is genuinely strong
for chart structure (S/R, trendlines, indicator state) and free on the
current account. Perplexity/OpenAI deep-research is unmatched for live
discourse aggregation ("what's the macro consensus on yields this week")
because it actually traverses sources — but expensive enough that gating
matters. Conflating them in one code path leads to either over-spending or
under-using vision. Own-LLM economics only work once we have labeled
training data, which we don't.
**Alternatives:** Single LLM path for everything (rejected — wrong-tool
problem); jump to fine-tuned own-LLM now (rejected — premature, no corpus);
skip Perplexity entirely (rejected — narrative synthesis on the live web
genuinely matters and prompt-engineered Gemini won't match a research
agent that traverses sources).

---

## 2026-05-09 — Time-weighting uses macro-appropriate horizons (NOT day/week-tight)

**Decision:** Mention extraction half-life defaults to 30d standalone; in the
watchlist resolver, half-life equals the extraction window length (7d window
→ 7d half-life, 90d → 90d). Technical scorer uses 5d/20d/60d momentum
horizons (≈ weekly/monthly/cycle). NO bias toward 1d / 7d windows in the
scoring layer.
**Rationale:** A macro thesis lives over weeks-to-months. Tighter half-lives
bias the system toward news-cycle noise. Stale-but-relevant content stays
weighted (a mention from 30d ago in a 90d window still counts at 0.79).
**Alternatives:** Tighter 14d-or-less half-lives (rejected — too tactical
for a macro analyzer).

---

## 2026-05-09 — yfinance is the default price provider; provider abstraction for later

**Decision:** Default `PriceProvider` is `YFinanceProvider`. No API key, free,
covers equities + ETFs + indices + crypto via symbol mapping. Provider
interface lets us swap to FMP / Finnhub / Polygon later without touching
scoring/runner.
**Rationale:** Ships today with zero infra. yfinance is fragile (Yahoo
scrape) but acceptable for daily bars while we're learning the loop. Phase 7
prod can pay for FMP if reliability matters.
**Alternatives:** FMP first (250/day free; needs key); CoinGecko for crypto
(rejected as primary — adds source).

---

## 2026-05-09 — SQLite WAL mode + caller-supplied connection pattern

**Decision:** `initialize_database()` enables WAL mode + busy_timeout=5000.
Read helpers in `prices/fetcher.py` accept optional `conn` param; use inside
transactions to avoid the inner-call's `initialize_database` deadlocking the
outer's BEGIN.
**Rationale:** Default rollback-journal locks the whole DB on writes. With
the FastAPI server holding read connections, CLI score-pass writes block
indefinitely. WAL eliminates the contention; the conn-passing pattern
eliminates DDL-inside-transaction deadlock.
**Alternatives:** PostgreSQL (overkill for single-operator); separate DB per
concern (operationally heavy).

---

## 2026-05-09 — Watchlist as a living object: anchors + theme + mentions

**Decision:** Active watchlist composed at runtime from three streams:
(1) anchors from `config/watchlist.json` always, (2) regime-aligned theme
tickers from `config/asset_themes.json` when current regime matches a
theme's `preferred_regimes`, (3) top mention-extracted tickers per window
above min count. Each entry carries `origins: [str]`.
**Rationale:** Static watchlists go stale fast. Macro themes shift; the
operator wants the system to surface what's actually being talked about
without manual curation. Origins make the source visible ("anchor",
"theme:uranium", "mentions:30d:w8.4").
**Alternatives:** Manual-only (rejected — defeats discovery); LLM-only
(rejected — token cost + nondeterminism for what regex + count handles).

---

## 2026-05-09 — Brain built inside macro-analyzer, not a separate repo

**Decision:** Keep `brain/` as a sub-package of `macro-analyzer` (`src/macro_positioning/brain/`) rather than extracting to a separate `macro-brain` repo.
**Rationale:** The original architecture doc planned a separate `macro-brain` repo, but building it in-repo was the pragmatic path: shared SQLite, shared models, single deployment, no HTTP contract overhead for what is currently a single-operator tool. Can extract later if GPU hosting or independent scaling is needed.
**Alternatives:** Separate `macro-brain` repo with `POST /brain/ingest` contract (as originally planned). Still valid if the system grows to need independent deployment.

---

## 2026-05-09 — Intelligence layer: pure functions on list[MarketObservation]

**Decision:** All three classifiers (quadrant, FCI, EPU) are implemented as pure `list[MarketObservation] → Pydantic model` functions with no side effects or network calls.
**Rationale:** Makes them trivially testable (23 tests with a factory helper `_obs(metric, value)`), composable (single FRED fetch powers all three), and safe to call in `_build_macro_indicators()` wrapped in try/except without state concerns.
**Alternatives:** Class-based providers that fetch their own data (rejected — redundant FRED calls, harder to test).

---

## 2026-05-09 — Institutional-terminal aesthetic: consumer chrome permanently banned

**Decision:** Strip and permanently ban: `backdrop-filter: blur()`, `radial-gradient` on body/panels, glow `box-shadow`, `@keyframes` animation, `linear-gradient` on nav/UI chrome, marketing hero copy.
**Rationale:** "This is not a consumer product. This should be straight tactical, to the point, very clear to read." Color is reserved for signal (green=bullish/easing, red=bearish/tightening, gold=high conviction/transitional). Every surface is flat `var(--surface)` + `1px solid var(--border)`.
**Alternatives:** None — this is a locked product direction from the user.

---

## 2026-05-09 — SPA dashboard (React/JSX) replaces server-rendered HTML

**Decision:** Old Python HTML-generation pipeline (`output_ui.py`, `tactical_ui.py`, etc.) is superseded by a React SPA at `web/positioning.jsx`. Old routes 307-redirect to the SPA. Old files retained for reference but not rendered.
**Rationale:** Enables component reuse, live data binding without full page reload, cleaner separation of data (FastAPI JSON endpoints) from presentation (JSX components).
**Alternatives:** HTMX on top of existing Python templates (simpler but harder to build the MacroIndicatorStrip and asset-class grouping interactions).

---

## 2026-05-09 — EPU composite: simple average, no additional normalization

**Decision:** EPU composite score = simple average of available EPU series values (no scale factors applied).
**Rationale:** EPU indices are already normalized to ~100 historical average by their designers. Unlike FCI sub-indicators (VIX, TED spread — measured in different units), EPU series are directly comparable. Simple average is defensible and transparent.
**Alternatives:** Weighted average (rejected — no evidence one EPU series is more predictive); z-score normalization (redundant given EPU's built-in normalization).

---

## 2026-05-09 — `format_prompt_blocks()` returns ("—","—","—") on empty input

**Decision:** `format_prompt_blocks([])` returns the sentinel tuple `("—", "—", "—")` rather than raising or returning empty strings.
**Rationale:** Prevents `KeyError` on `MACRO_ANALYSIS_PROMPT.format(...)` in the heuristic fallback path where `observations` may be an empty list. The LLM sees literal "—" and correctly treats it as "no data available", which is better than a missing key error or blank sections that confuse the model.
**Alternatives:** Guard clause in `synthesis.py` (rejected — more code for same effect); empty strings (rejected — blank section headers with no content confuse the model).

## 2026-08-25 — Text-native trade calls get a parser, not an LLM

Stock Unlocked Trades relays structured text ("ASAN Entry @ $8.35 / Target
1: $8.70 / Stop Loss: $7.84"), unlike every other Telegram source, which
posts charts. Options considered: route it to `llm_extractor`, or write a
deterministic parser.

Chose the parser (`signals/stock_unlocked_extractor.py`). When the ticker,
side, entry, targets and stop are stated in words, a regex reads them
exactly; an LLM re-reading the same words can only introduce error, at a
per-call cost, on a source that posts several times a day. The router sends
this source to that extractor exclusively — it never touches the LLM path.

The parse is written back to `documents.extracted_features_json` in the
vision-compatible shape, so the existing accuracy layer scores this source
alongside the chart sources instead of growing a parallel scoring path.

Signals are stamped with the call's `published_at`, not ingest time, so
backfilling a month of history does not read as a same-day burst in the
momentum and conviction windows.

## 2026-08-27 — The paper book is a semantic layer, not a backtest

A $50k simulated account (`src/macro_positioning/paper/`) now trades the
stack's own reads automatically: 1–5% per position sized by conviction, a
hard 30% cash floor, rotation out of weak holdings to fund strong ones.

The design choice worth recording is what it optimises for. A backtest
would have been cheaper — replay history, print a number. Instead every
decision the engine makes is persisted with an `Action`, an `Intent` and,
when it declines to trade, a `Blocker` (`paper/vocabulary.py`). Refusals
are first-class rows: "BTC scored 92 but the book was 70% deployed and no
holding was 0.15 weaker" is a record, not a silent skip. Without those
rows the book's behaviour is unfalsifiable, and an unfalsifiable P&L curve
is worth nothing.

**Conviction is fused, not chosen.** `trade_scores` is the spine because it
is the only surface carrying the technical agent's stop — the book will not
size a position it cannot invalidate. `signals/aggregation.py`'s
multi-window blend then moves the read up or down (+0.15 agreeing, −0.25
opposing). Every contribution is stored as a `(name, delta, reason)` triple
on the decision row, so a 4.1% position is always traceable to the reads
that produced it.

**The mandate is snapshotted, not referenced.** `config/paper_trading.json`
is copied onto the portfolio row at creation. Editing it later does not
retroactively rewrite a running book's rules, so an equity curve stays
interpretable against the mandate that made it.

**`paper_orders` is the source of truth, `cash` is a cache.** Every tick
reconciles `cash == starting_equity + Σ cash_delta` and refuses to trust a
book that fails. Deliberately separate from `trades` / `trade_plans`, which
remain the hand-traded funnel.

**Two things the first live run taught us**, both now guarded:
- A LevelSet is drawn at the scoring pass and goes stale against the tape.
  LMT, RTX and NOC all opened with their stops already on the wrong side of
  the live mark and stopped out on the next tick for the spread. The engine
  now refuses any entry whose stop or target the price has already passed
  (`Blocker.LEVELS_STALE`).
- A risk exit needs a cooldown. Without one, a name that stops out while
  still scoring well is bought straight back at a worse price, every tick,
  forever. Three days, and only after risk exits — rotation and cash-floor
  trims carry none, since those were forced by capital, not by the thesis
  failing.

**Boot no longer depends on a write lock.** `api/main.py` called
`initialize_database()` at import; `alert_watch.py` holds a write
transaction through its hourly price pass, so a reload inside that window
crash-looped the launchd job and took the SPA down. Schema init now retries
and then serves reads anyway — WAL keeps readers working throughout. The
tick script retries its commit for the same reason.

Automation: `com.macro.paper-tick`, 06:15 + 13:15, a quarter-hour behind
free-ingest. The tick prints its full decision set, so
`~/Library/Logs/macro-paper-tick.out.log` is itself the audit trail.

## 2026-08-28 — Paper-book attribution: sleeve, class, regime; and what "booked" means

Added `paper/sleeves.py` + `paper/performance.py` + `/api/paper/performance`,
so the book's P&L can be read as "the hard-asset sleeve made money" rather
than one number.

**The taxonomy composes, it does not redeclare.** `config/paper_sleeves.json`
references the themes already in `config/asset_themes.json` (agriculture,
precious_metals, crypto, technology_ai, defense, uranium, energy — the
desk's own vocabulary, already carrying `preferred_regimes`) and the
members of `config/correlation_buckets.json`, then adds explicit tickers
only for the gaps. There were fourteen unclassified names in the live
scored universe, three of which the book was already holding: FCX, COPX
and XLB had no home because `asset_themes.json` has no industrial-metals
theme. Coverage is now 71/71 and is reported on the endpoint, so the next
gap is visible instead of silently diluting a sleeve.

**Reporting map, not a trading rule.** Sleeves never gate a fill;
correlation buckets still own the caps. The two answer different
questions — "would this stack the same bet?" versus "where did the money
come from?" — and merging them would corrupt both.

**Resolution is at read time.** Re-classifying a ticker corrects the whole
history rather than leaving old positions mis-bucketed, which is the
opposite of the `mandate_json` decision (frozen at entry). The difference
is deliberate: a mandate is a commitment and must not move under a P&L
curve; a taxonomy is a description and should improve retroactively.

**Three separate percentages, never one.** `realizedPct` is booked,
`unrealizedPct` is an opinion, `totalReturnPct` is the book. `bookedShare`
answers "how much of the gain is off the table" and returns **null** when
realized and total disagree in sign — a book that has banked losses while
sitting on open gains has no meaningful "share of gains banked", and
rendering that as a negative percentage reads as if money were given back.

**Open positions never count toward the win rate.** They have no verdict
yet; counting them is how a losing book flatters itself. Returns are
measured on capital committed (sum of entry notionals from the fill log),
not on book equity, or every position looks like a rounding error.

**Regime rows overlap and say so.** A sleeve can express two regimes, so
those rows do not sum to the book. The payload carries `regimeNote` and
the UI prints it rather than implying a partition. Class and sleeve rows
*are* a partition, and a test asserts they sum to the book.

**The breakdown that actually tunes the mandate is `byExitReason`.** The
first days already show it working: six closed trades, all losses — three
`stop_hit` (the stale-level artifacts, now guarded) and three `make_room`
rotations out of agriculture and defense to fund crypto, which is the only
sleeve making money. That is a legible story about the rotation rule, and
it is invisible in a win rate.

## 2026-08-28 — "Conviction" 0–1 became "rank" 0–100, anchored on the real distribution

The paper book's sizing metric was a 0–1 number called conviction. Asked
what 0.28 meant, the honest answer was: 28% of the way along a straight
line between two hand-picked score anchors (55 → 0.00, 90 → 1.00). It had
the shape of a probability and none of the content, and it was read as
one — which is the failure, not the misreading.

Measured against 3,379 real scored rows, the old anchors sat badly: score
55 was the 17th percentile, not "no interest"; score 90 was the 99.7th and
had been reached nine times ever. The 0.28 entry bar therefore meant "the
40th percentile" — a below-median name — which is why it had never
rejected anything.

**Rank is the percentile of the score in the trailing 180 days of
scheduled passes.** Rank 72 means the name stands above ~72% of what the
desk scores. The modifiers (signals ±15/−25, cross-window −8, R:R ±10,
structure +5, momentum ±5) are now percentile POINTS, which is a scale
they can be read on. Bars moved to 60 in / 40 out.

**It is still not a probability, and the code says so in three places.**
Nothing here is calibrated against outcomes: `signal_calibration_history`
is empty, `trades` is empty, and the paper book's only closed trades are
artifacts of the stale-level bug. Rank claims an ordering and nothing
more. Making it mean a win rate needs closed trades to calibrate against.

**The entry bar is structurally redundant, and that is worth knowing.**
All 22 currently-tradeable names rank 66+, because `desk_data` only assigns
LONG/SHORT to tier_1/tier_2 rows — the side mapping has already done the
filtering. The bar is a safety net for the modifiers (a name the desk is
fading drops 25 points and can fall through it), not a primary filter. The
real gate on what gets bought is the 12-position cap.

**Sizing now spans the tradeable range, not 0–100.** A name that just
clears the bar gets the 1% minimum and rank 100 gets 5%. Under the old
scale nothing below 0.28 ever traded, so 1%–2.1% of the band was
unreachable.

**Ranks clamp at 100; ordering does not.** Nine names saturate the
displayed scale on a normal day. Ordering decides who takes the last slot,
so `RankRead.raw` keeps the unclamped value and the candidate sort uses
it. Display and every bar comparison use the clamped value.

**Migration.** `_migrate_paper_rank_scale()` renames the four columns
(`paper_positions.conviction_*`, `paper_decisions.conviction*`), renames
the intents (`new_conviction` → `cleared_bar`, and the two conviction_*
ones to rank_*), and clears the numeric values written under the old
meaning — decision headlines still carry the story in prose. Entry ranks
are NOT reconstructed: the score's percentile is recoverable but the
modifiers that were applied are not, and backfilling the base-only number
invented a drift ("rank 80 → 100, +20" on positions that had not moved).
An unknown entry rank beats a fabricated one.

A stored mandate carrying the old scale cannot be honoured — an entry
floor of 0.28 means "everything qualifies" on a 0–100 scale — so
`Mandate.from_stored` replaces it with the current config and the tick
re-stamps the row once, rather than warning forever. This is the single
exception to the mandate being frozen at creation.

## 2026-08-31 — Re-entry is gated on reclaim, not on the clock

The paper book refused to re-enter any name for three days after a risk
exit. Per the desk: a stop-out is not automatically a dead thesis. If
price broke the level and has since traded back above what the book paid,
the setup re-presented itself and refusing it leaves a valid trade on the
table.

Re-entry after a PRICE exit (`stop_hit`, `trail_giveback`,
`trail_round_trip`) is now gated on **reclaim**: price must be back above
(long) or below (short) the position's own `avg_price`, plus a 0.2% buffer
so a tick on the number does not count. What the book still will not do is
buy back a name trading *below* where it was stopped out — that is paying
twice for the same broken idea. `Blocker.COOLING_OFF` now names the exact
price that would reopen the trade, so the refusal is checkable.

`max_stops_per_window` (2) is the chop guard: break-reclaim-break-reclaim
is how a book dies by a thousand spreads, so past that count the name
waits out the window whatever price does.

**Thesis exits carry no cooldown at all.** `side_flip` and `rank_decay`
were changes of view, not level breaks — there is nothing for price to
reclaim, and the 60/40 entry/exit hysteresis is already a 20-point buffer
against churning them. Rotation and cash-floor trims were never exits of
conviction: the book needed the capital, so the name is eligible again the
moment there is room.

## 2026-08-31 — The funnel was not broken; the writer was held for 40 seconds

Reported as "no way to move a concept forward from the asset page". Two
real faults under it, one of them project-wide.

**`prices/fetcher.fetch_and_persist()` held the SQLite writer across ~100
network calls.** A single `with sqlite3.connect(...)` wrapped the whole
fetch loop, so the write transaction opened on the first ticker's bars and
stayed open through every remaining yfinance round-trip. The hourly
`alert_watch.py` job therefore held the writer for 40+ seconds at a
stretch, and anything else wanting to write in that window died on
`database is locked` — the paper tick, the API's boot-time schema init
(which crash-looped the launchd job and took the SPA down), and every
attempt to mark or promote a concept, which surfaced as a 500 from a
button that should always work. Fetch now completes before the connection
is opened. Measured during a live alert-watch run: worst writer wait went
from 41s to 0.00s.

`db/connect.py` is now the single place that knows the timeouts, and the
API modules that write use it instead of each picking their own (or none).

**The SPA never persisted plans, and lied when writes failed.** Promoting
a concept built a plan in `window.MA_DATA` and never called
`POST /api/funnel/plans` — the funnel appeared to move and hadn't, and the
plan vanished on reload. `markConcept` had a subtler version: a non-2xx
response is not a thrown error, so it fell past `if (r.ok)` and reported
"marked as a concept" on a 500. Both now return `persisted` and the UI
says "marked locally only — the write failed" when that is what happened.
Promotion also PATCHes `trade_concepts.trade_plan_id`, which had always
stayed NULL, breaking the last link of the lineage chain /live renders.

The asset page now carries the funnel action itself — mark → promote →
open plan, seeded with the technical agent's levels, since that page is
where the levels already are. Its previous action row (Log this trade /
chart_vision / Add to watchlist, plus a duplicate set in the footer) was
entirely inert.

## 2026-09-01 — The trail was a noise detector, not a trailing stop

Reported as: too many trades for a book meant to hold days to weeks. The
data agreed — **median hold 10.5 hours** across the first 18 closes, max
3.5 days.

The cause was the trailing exit. It compared open profit to its high-water
mark as a bare RATIO with no floor on how much profit had to exist first:

    peak > 0 and (peak - current) / peak >= 0.35

A position that ticked up $6 and back to $4 had "given back 33%" and was
closed. Five of the first seven trail exits fired on peak profits **under
1%**. NVDA peaked at +0.25% ($5.99) and was closed for **−$98.19** —
realising a loss the stop had never asked for. `trail_round_trip` was
worse: it fired whenever anything that had ever been green went red, which
is a description of trading, not an exit signal.

**The trail now arms at 1R.** Below 1× initial risk it is dormant and the
stop is the only price exit, which is what a stop is for. Once armed the
giveback allows half the run back (0.35 → 0.50), which is a swing-horizon
number rather than a day-trading one.

**R had to mean INITIAL risk.** The target trim moves the stop to
breakeven, so computing R from the live stop divides by zero and returns
None — silently disarming the trail for the rest of the trade, exactly
when a runner most needs it. `paper_positions.initial_risk` is banked at
entry and is now R's denominator.

**`min_hold_days` (3) stops the book being talked out of itself.** Rank
decay and rotation cannot close a position inside its first three days;
three of the eighteen closes were rotations out of six-hour-old positions.
Stops and hard side flips are exempt — risk and direction always act.

Replaying the 18 closes against these rules: **9 would still exit (every
one a stop — risk always acts) and 9 would have been held.** SOL is the
only trail exit that survives, at 1.09R, which is what the rule is for.
Three of the remaining nine were the stale-level bug and would not have
opened at all.

One caveat worth keeping: the 12 ticks in that window were mostly manual
runs during development. The scheduled cadence is twice a day, so the
exit rules get two chances daily to act, not twelve.

## 2026-09-01 — Targets are judged on four views, not one

The book priced a setup on four numbers the technical agent handed it —
entry, stop, target, R:R — and threw away everything behind them. The
platform holds four independent readings of the same trade, and only one
was reaching the decision.

An audit of the 87 live level sets showed the agent is already better than
assumed: **81% of stops sit on real chart structure** and 72% of targets
on tested resistance. The genuine gaps were that **macro regime never
touched a target** (the word "regime" in the rejection reasons means price
*scale*), **trusted voices supplied 5% of targets** (consulted, then
discarded as stale), and **entry is 65% mechanical** — the weakest rail,
and the one that sets R for every sizing and trail decision.

`paper/valuation.py` composes four views into a 0–100 `support`:

  structure       is the target a level that has been tested and held, or
                  a projection into open field?
  trusted_voices  do the KOL targets cluster near the agent's, and do
                  those people have a resolved record?
  regime          the brain's own `macro_alignment_score`, paired with the
                  regime the row was SCORED under — not today's global
                  read, which would describe a fit never measured.
  price_action    is the target reachable in ATR terms, is the trend with
                  the trade?

It drives four things: **size** (support shifts rank ±10 points), **admission**
(`Blocker.UNSUPPORTED_TARGET` below 35), **the levels themselves** (a target
the agent projected *past* a tested level is pulled back to it), and **exit
timing** (`Intent.SUPPORT_COLLAPSED` when the case falls below 20, subject
to `min_hold_days` — price acts fast, opinions do not).

Pulling targets back is the change with teeth. LMT's target moved 637.79 →
608.40 because the agent projected through resistance that had held 4× and
was tested 8 bars ago; R:R fell from ~3.0 to 0.99. That is not the
composition making the trade worse — it is the composition declining to
flatter it.

**Paper-side on purpose.** `scoring/levels.py` is shared with the
hand-traded desk, the alerts and the SPA cards. This layer judges its
output without moving it, so the composition earns its promotion to a
levels.py v3 on the paper book's own record rather than on assertion.

Every input is best-effort: a missing structure map or an unreadable KOL
table degrades that view to `absent` and thins the decision. It never
stops the tick.

## 2026-09-01 — A second paper book: follow the cohort, don't score it

The paper book measures the desk's blend. It cannot answer whether the
blend beats just following the people it blends. So there is now a
**second book on the same engine** whose candidates come from the Feather
Hands crowd's own calls — Big_Nuts, Feather Hands Trading, MadDog31,
joejoe55, Market Traders — read straight out of `signals` with the entry,
stop and target the chart was posted with.

**Nothing forked.** `run_tick(candidates=…)` and `valuations_in=…` were
already injection points, and every store/route function is keyed by
`portfolio_id`. The cohort book is a candidate loader
(`paper/cohort.py`), a mandate (`config/paper_cohort.json`), a tick job
and a page. Both curves are made by the same exit rules, the same cash
floor and the same fill model, so the difference between them is the
difference between the two ideas and not an artefact of two engines.

**The composed view is OFF for this book** (`use_composed_levels: false`,
`valuations_in={}`). Re-deciding the target with the desk's structure map
would fold the desk's opinion back into the book whose entire purpose is
to measure the cohort without it — and the "trusted voices" view would be
scoring these calls partly against themselves.

**Coverage is a first-class output, not a log line.** 63% of this
cohort's flow is Solana memecoins nothing in the price stack marks. In
the live 10-day window: 235 calls → 149 unpriceable, 34 with no side, 21
whose R:R had already collapsed, 16 untriggered → **3 candidates, 2
fills**. "Two fills" without that denominator invites exactly the wrong
conclusion, so `Coverage` is returned by the loader, printed by the tick,
served at `/api/paper/cohort/coverage` and rendered above the fold. Skips
are COUNTED, not written as 400 refusal rows — that would bury the
decision log the desk actually reads.

**Three screens do the work the desk's levels normally do:**
- *directional only* — `bidirectional`/`no_trade`/`not_a_chart` have no
  side, and inferring one from the chart is the fake-bias bug again.
- *a watching call waits for its own entry* — "wants a breakout above
  105" is a plan; buying it at 98 is buying a trade its author has not
  taken. Calls the author marks `active`/`entry` skip this.
- *R:R must still be available at the mark* (`min_live_rr: 1.2`) — a 3R
  setup bought after it has run to 0.8R is a different trade with the
  same levels. This is also what stops the book chasing, since the engine
  fills at the live mark and never at the entry as drawn.

**The rank is not a percentile and does not pretend to be.** There is no
distribution to rank a Telegram post against, so it is built from the
call: conviction, chart confluence, R:R, whether the author says they are
in it, freshness decay, whether the rest of the room agrees or opposes,
and per-author alpha from `call_outcomes` — gated at 30 scored calls and
capped at ±8 points, so a measurement tilts the size without deciding it.
`anchored=False` and a dedicated `CohortMandate` block on the page say so;
reusing paper.jsx's MandateBlock would have printed "a 0–100 percentile of
everything the desk scores", which is false here.

**The mandate is looser where the flow demands it and nowhere else.** The
signal book's 20%/3-per-bucket concentration caps would stop this book
after three names, because it *is* one bet — crypto. Raised to 55%/6:
that the book is crypto-correlated is the finding, not something to cap
away. `min_hold_days` 3 → 1 (this cohort updates hourly; three days would
make the book ignore its own source), `stale_after_days` 10 (the exit
that does the most work — almost no call is ever explicitly closed, the
room just stops mentioning the name), slippage 5bps → 15bps.

## 2026-09-01 — The crypto universe is Coinbase's list, not a hand-typed one

The tracked set was sixteen coins in a literal in `symbol_map.py`. It had
no ZEC — an $14B coin, #11 by cap, that the Feather Hands crowd calls
constantly — and it *did* have TRX, which Coinbase does not list here at
all. Both errors are the same error: a hand-maintained list drifts from
the exchange it is supposed to describe.

`config/crypto_universe.json` is now generated from three sources by
`scripts/refresh_crypto_universe.py`, and checked in so a listing change
arrives as a reviewable commit rather than as a silent behaviour change on
a morning tick: **405 Coinbase USD/USDC books** (the scope gate), **41
majors** (CoinGecko's cap ranking ∩ Coinbase, stablecoins and wrapped
tokens stripped), and **verified yfinance symbols** for both.

**Two sets, because there are two questions.** `coinbase_tradeable()`
answers "may the desk trade this?" and gates crypto PAIRS.
`crypto_majors()` answers "may a BARE ticker mean this coin?" — a much
narrower permission, because equities here are not a fixed list. They are
scored dynamically from whatever the channels say, so routing all 405
bases on bare tickers would price real companies as tokens the moment a
listing collided. SKY is a Coinbase book *and* Skyline Champion on the
NYSE; AI is a Coinbase book *and* C3.ai. The refresh script now checks
every cap-ranked candidate against Yahoo for an EQUITY quote and withholds
bare routing where one exists (PUMP and SKY on this run). The hand-written
always-major list overrides that check, because those collisions — SOL is
also Emeren Group — are ones this desk has already decided about.

A non-major Coinbase coin is keyed by its yfinance form: `AERO/USD` →
`AERO-USD`, the same trick the commodity aliases already used
(`GOLD` → `GC=F`). The key says what it is, so a coin can never be
confused with the equity sharing its ticker, and it round-trips.

**Existence is not verification.** The first pass accepted any symbol
yfinance answered for, and 17 coins came back empty — PEPE, UNI, APT, POL
among them, all of which Yahoo disambiguates with a CoinMarketCap id.
Resolving those by search alone would have been worse than the gap:
`PEPE24478-USD` is Pepe and `PEPE25912-USD` is PepeCoin, a different
asset. So **Coinbase is both the gate and the oracle** — a symbol is
accepted only when its close agrees with Coinbase's live price for the
book we would actually trade, within 20%. That took the verified set from
57 to 65 of 73, and it earned its keep immediately: TROLL was rejected
three times over, at 98%, 100% and no-data, because `TROLL-USD` on Yahoo
is somebody else's TROLL.

The eight that nothing prices (AI, BILL, BIO, CAP, MASK, POL, TROLL,
WLFI) are recorded in `no_data` with the date and the reason, and they
still RESOLVE. Returning None for them would report "not a real asset",
which is a different and much more misleading statement than "tradeable,
but we have no bars".

**The ingest was throwing raw pairs at yfinance.** Both price jobs fetched
`asset_ticker` straight out of `signals` — `BTC/USDT`, `KINS/SOL`,
`牛来/USDT` — none of which yfinance answers in that form, which is why
the crypto half of the tape never had bars while `BTC`, `ETH` and `SOL`
sat hardcoded in a top-up set. Both now resolve before fetching and union
in every major. Backfill: **90 of 111 crypto keys, 17,290 bars.**

**A slash is not always a pair.** Widening the coin set exposed two ways
the pair parser was too eager, both of which now resolve to nothing:

- *Ratio charts.* "AI/NVDA" and "AAPLCAT/AAPL" have a pair's shape and no
  book behind them. Reading the left side as a coin put C3.ai into the
  live-signals feed as a token.
- *Crypto-quoted pairs.* "ETH/BTC" and "BASECAT/WETH" are real books, but
  their levels are denominated in the quote coin. An ETH/BTC entry of
  0.031 marked against a $3,000 ETH-USD price opens a position whose stop
  is five orders of magnitude from the mark — infinite risk, and the R:R
  screen waves it through because the arithmetic is internally consistent.
  Everything the desk marks is in USD, so only a USD-equivalent quote
  produces a key.

Effect on the cohort book, same 10-day window: **3 candidates → 7, and the
priceable share of directional calls 26% → 32%.** ENA, HYPE and ETH all
reached the book, which now holds five positions instead of two. The
remaining 150 unpriceable calls are genuine DEX flow and ratio charts
(PONS/WETH, ANSEM/SOL, AI/NVDA) — the honest number.

Also fixed in passing: `BTC.D` and other dominance labels matched the
dot-class equity pattern (the same shape as `BRK.B`) and were being
fetched as stocks, despite a comment in that very function claiming they
were rejected.

## 2026-09-18 — The paper book learns from itself; holds come from the signals

Three weeks in: +1.5% total but −$265 realized, 25% win rate, profit
factor 0.35. Every dollar of gain was still open. Two bugs and three
design changes, in the order they were found.

**R was being computed from the moved stop.** After a target trim the
stop sits at breakeven, so `|entry − stop|` is ~0 and `performance.py`
reported R in the billions (NEAR, INJ, PLTR, GDXJ). It now uses the
`initial_risk` banked at fill — the same fix the engine got on Sep 1 —
and returns None rather than a number nobody should believe when the
banked risk is degenerate.

**Stops had no floor and no cap.** AVAX opened with a 0.3% stop and
realised −5.8R on a twelve-hour gap. Across eleven stop-outs the median
fill was 0.05R beyond the stop but the mean was 0.61R, driven entirely by
stops too tight to survive a tick. `min_stop_pct` 1.5% (`STOP_TOO_TIGHT`)
and — the user's one explicit hard limit — `max_stop_pct` 5% on spot
(`STOP_TOO_WIDE`), both measured from the fill, not the call's entry.

**Hold length is derived per trade, not mandated.** The aim is multi-week
because that is where the profit factor lives, but "multi-week" is not
one number. `paper/horizon.py` sets `expected_hold_days` at fill from the
signals' `dominant_horizon` (swing 10d / position 30d / strategic 60d),
the setup (breakouts and structure ×1.25, mechanical rails ×0.8) and —
once a sleeve has n ≥ 30 — its own days-to-peak record. From that:
`min_hold_days` = 40% (opinions wait that long) and `confirm_ticks` =
days/3 (opinions must persist that many consecutive ticks), both clamped
(3–21d, 2–8 ticks). Thesis exits — rank decay, support collapse, side
flip — now log a HOLD row "watching, k/n" on every unconfirmed tick.
Stops, target hits and the trail are untouched by any of this: reaching
the target closes the trade on day 2 or day 40 alike.

**Profit-taking is a sliding scale on trade quality.** Decided once at
fill and stored as `exit_path`. Rank ≥ 85 AND composed support ≥ 60 →
the TARGET path: nothing comes off below the composed target, which
closes the trade in full; a NEAR MISS (≥ 90% of the way, then 0.5R back
off the high without crossing) closes it too — the target was the idea,
not the tick. Everything else → the LADDER: 25% at 1R (stop to
breakeven), 25% at 2R (lock 1R), 25% at 3R (lock 2R), the last quarter
trails. Both paths: the trail arms at 1R and its allowed giveback
tightens with peak R (50% → 35% → 25%). Found by a fixture and fixed: a
position the ladder had reduced was being topped back up by the entry
pass, which re-armed the stop from a locked 108 to a fresh 192 and
stopped the whole thing out on the next dip. `PROFIT_TAKEN` now refuses
adds to a reduced position, and an add can only ever tighten a stop.

**`paper/learning.py` closes the loop.** Every tick, from the trailing
90 days of the book's own record, per sleeve: avg R / win% / avg win /
avg loss → a `book_record` rank component (clip(avgR, ±2) × 8 × w, cap
±10); mean fill-beyond-stop → `risk_mult`, which the size is divided by;
median days-to-peak on winners → the horizon prior, engaging at n ≥ 30.
**Everything is weighted `w = n/(n+30)`.** Seven trades is a nudge (w =
0.19), thirty is half-trusted. n = 0 changes nothing by construction.
Adjustments are re-derived each tick — no hidden state, so a bad month
un-learns itself as it rolls out. Per-rung ladder stats and input
attribution (which of rank / support / signal / regime moved first
ahead of a turn, from `paper_position_snapshots`) are computed and
reported but do NOT feed back in v1: they need a record of their own.

Verified three ways before it touched a tick: synthetic-record unit
tests for the arithmetic, the sign, the cap and the n = 0 case;
`scripts/paper_learning_report.py` printing each sleeve's adjustment
next to the raw stats with the formula re-evaluated inline (all ✓); and
a live tick with the loop on and off, diffed — nine rows moved, the
largest by 0.55 points, every one carrying the component that explains
it, fill set identical.

**The record surfaces in three places; the third is gated.** The paper
rank (L1). A read-only badge on concept cards and the asset page —
"📓 Crypto 50% · −0.6R · n=10" with the full record on hover — so a human
promoting a concept sees the machine's record on that class. Feeding
the shared `trade_scores` is deferred behind an evidence gate: n ≥ 30
closed trades under the current rules AND the adjusted rank predicting
realized R better than the unadjusted one out-of-sample. Not a date.

**The hard-asset hypothesis** — that Defense 0/7, Copper 0/5, Precious 0/2
are multi-week trades the book cut early — is now testable: every open
position snapshots every input every tick, and the loop reports
intended vs realized horizon per sleeve. The data will say.

**`paper_position_snapshots`** is the loop's raw material and the answer
to "which input moved first": one row per open position per tick with
rank, score, the four views' deltas, the signal blend, regime, unrealized
R, days held, exit path, and any thesis exit being watched.
