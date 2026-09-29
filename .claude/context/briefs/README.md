# Worker-Chat Briefs

Each file in this directory is a self-contained kickoff prompt for a worker chat session. Paste the file contents into a fresh Claude Code session in the appropriate worktree.

## Coordination model

- **PM chat** (the chat that produced these briefs) owns: architecture decisions, schema changes, `desk_data.py` + `web/data.mock.js` SPA contract, `STATE.md` / `DECISIONS.md` / `OPEN-QUESTIONS.md` upkeep, merging worker branches to main, deployment.
- **Worker chats** own: feature implementation inside their declared file territory. They do NOT touch schema, the SPA contract, or the context files. When they need any of those, they hand back a request to PM.

## Workflow per worker

1. PM creates a worktree off main: `git worktree add .claude/worktrees/<slug> -b claude/<slug>`
2. User pastes brief into a fresh chat in that worktree
3. Worker implements + commits to its branch
4. Worker hands back a summary (files touched, tests added, open questions)
5. PM reviews, merges to main, updates STATE.md

## Briefs

- [`heuristic-scorers.md`](heuristic-scorers.md) — volume_flow, sector_theme, relative_strength, liquidity_alignment scorers (no LLM cost) — SHIPPED
- [`fred-history.md`](fred-history.md) — historical FRED persistence (`fred_observations` table, backfill CLI, NFCI 4w change wiring) — foundational for time-series consumers
- [`ml-learning-loop.md`](ml-learning-loop.md) — source attribution, score-outcome correlation, retraining infra
- [`llm-agents.md`](llm-agents.md) — regime_classifier + narrative_synthesizer wired to Gemini
- [`manual-input.md`](manual-input.md) — drag-drop charts/text → chart_vision (already in flight; this is the refresh brief)
- [`chart-lab.md`](chart-lab.md) — desk chart reading + single-ticker setup bench on the CLI, no API spend

## Schema-change protocol

When a worker needs a new column/table:
1. Worker stops, writes a short request: `<table>.<column> :: <type> :: <why>`
2. PM adds it to `src/macro_positioning/db/schema.py`, commits to main
3. Worker pulls main into its worktree, continues
