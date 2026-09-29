# Open Questions & Blockers

Active items waiting on user input or external action.

---

## Alerts — Telegram delivery ✅ RESOLVED 2026-08-24

- [2026-08-24] Bot created and authorized; delivery verified end to end
  (bot `8854570939` → DM). Gmail token also re-authorized the same day —
  19 messages fetched, 18 new documents, first newsletter ingest since
  2026-08-11.

  **Still open:** whether the OAuth consent screen was *published*, or
  only re-authorized. If it is still in Testing status the refresh token
  dies 7 days after issue and `com.macro.gmail-token-check` will ping
  around **2026-08-31**. No ping = published = the weekly chore is gone.

  <details>Original entry — the setup steps, kept for reference:

  The alerts layer is built, tested, replayed against August
  history, and scheduled hourly (`com.macro.alert-watch`). It was
  deriving and recording alerts but could not deliver them until the
  operator created a Telegram bot — this is the one step that can't be
  automated, because @BotFather only talks to a human:

  1. Message **@BotFather** → `/newbot` → copy the token
  2. Send the new bot any message (bots cannot open a chat with you)
  3. `MPA_TELEGRAM_BOT_TOKEN=<token>` in `.env`, then
     `python scripts/alert_watch.py --whoami` to print the chat id
  4. `MPA_TELEGRAM_ALERT_CHAT_ID=<chat id>` in `.env`
  5. Verify with `python scripts/alert_watch.py --test-send`

  Nothing fired in the meantime is lost: undelivered alerts are retried
  for `alert_redelivery_window_hours` (24h) after the token lands.
  </details>

---

## Alerts layer — two owners, one package

- [2026-08-24] A concurrent session is committing to the same branch and
  package: `22615d4` added `alerts/direction_rules.py` (zone_arrival,
  tape_flip, horizon_divergence, conviction_build, proven_voice_call) and
  `86f240f` shipped levels v2. Formatting changed under the operator
  mid-session because of it.

  **Worth resolving:** those direction rules emit outside
  `rules.evaluate`, so they bypass the `logic_version` guard added in
  `e2f788f` — a scoring-logic change can still manufacture direction
  alerts, which is exactly what produced the false "FCX cleared 75". Either
  extend the guard to their emit path or agree an owner for the package.

---

## ML / learning loop scope

- [2026-05-09] Priority order for the 7 ML-loop items in STATE.md
  "Next Steps — ML / Learning Loop"? Source attribution aggregator is the
  smallest first move; correlation analysis needs more closed trades; full
  retraining needs multi-month corpus.

- [2026-05-09] When do we wire the FIRST real LLM-backed agent? Now
  partially answered: `chart_vision` goes Gemini-via-existing-brain/vision.py
  (manual input chat owns it). For `regime_classifier` and
  `narrative_synthesizer` — likely also Gemini. The deep_research agent
  (Perplexity/OpenAI) is a separate slot to design later under budget
  guards. See DECISIONS 2026-05-09 "LLM stack" entry.

## Deployment

- [2026-05-09] Deployment target for macro-analyzer — Render still the call
  (per D-2026-05-08-003)? Needed before the tactical-gate endpoint can be
  tested live with `Trading-Agent-V1-CODEX`.

## Resolved this session (kept for record)

- ~~[2026-05-09] composer.py stub_components / technical_structure~~
  RESOLVED in Phase 6c. Removed from stubs (technical_scorer now real);
  test updated to reflect new state.

## Deferred (not blocking; tracked elsewhere)

- COT data connector — Phase C in workstreams, deferred behind ML-loop work
  and the manual input layer.
- 4h/12h intraday timeframes — needs intraday yfinance fetch + per-tf
  feature compute. Tracked in STATE.md "Next Steps".
