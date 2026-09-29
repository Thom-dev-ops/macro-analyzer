# Macro Analyzer — Stage 1 design handoff

This folder is the visual + interaction reference for **Macro Analyzer / positioning desk, stage 1**.
Open `Macro Analyzer.html` in a browser to see the full mock; everything else is the source.

## What's in scope for stage 1

Three top-level views — same shell, same visual grammar:

- **`/positioning`** — the daily desk. Regime tape, KPI strip, hero signals, scored watchlist, active trades, trade log, mobile preview.
- **`/journal`** — the review desk. Process scorecard, closed trades, missed trades, source attribution, thesis change log.
- **`/dev`** — the system room. PM panel (todos / decisions / commits), brain activity, cost tracker, source health.

Drill-down: clicking any signal row or hero card opens a right-side **Reasoning Trail** sheet — composite breakdown, why-now bullets, contributing sources/theses, agent-by-agent telemetry.

## File map

| File | Role |
|---|---|
| `Macro Analyzer.html` | Entry — loads React/Babel + all JSX modules + `styles.css` |
| `styles.css` | All visual tokens (colors, density, accents) and component styles |
| `data.js` | Mock data (`window.MA_DATA`) — drop-in shape for the real backend |
| `components.jsx` | Atomic components (ScoreChip, TierIndicator, RegimeBadge, SubScoreBar, SourcePill, Sparkline, PnL, DrillSheet, SetupCard, SideLabel) |
| `app.jsx` | Shell — top bar, nav, KPI strip, view router, Tweaks panel |
| `positioning.jsx` | `/positioning` view + RegimeTape + ActiveTradesPanel + TradeLogPanel |
| `journal.jsx` | `/journal` view |
| `dev.jsx` | `/dev` view |
| `reasoning.jsx` | Reasoning trail sheet content |
| `mobile.jsx` | Phone preview frame for /positioning |
| `tweaks-panel.jsx` | Tweaks controls (accent / density / mobile preview toggle) |

## Design system (live in `styles.css`)

- **Aesthetic**: terminal/desk-research, near-black canvas (`--bg-0: #0d0e0c`), parchment-ivory text, gold accent for conviction. Mono for all numerics, serif for titles, sans for body.
- **Tokens** are CSS custom properties at `:root`. Accent and density are switchable via `data-accent` / `data-density` on `<html>`.
- **Numerics**: `JetBrains Mono`, tabular-nums, right-aligned in tables (use `.num`).
- **Tier colors**: tier-1 gold, tier-2 green, tier-3 amber, tier-4 muted red.
- **Regime colors**: framework label uses gold.

## Data contract

`window.MA_DATA` keys are the contract; backend should return the same shapes:

- `regime` — `{ framework: { label, slug, confidence, bias, sizingModifier, scoreModifier, sinceDays }, thesis: { label, narrative, version, author, lastRevised }, confidenceTrace: number[], transitions: [...] }`
- `kpis` — `{ cashPosture, activeTrades, pnlToday, pnlWeek, signalsHigh, spendToday }`
- `heroSignals[]` — id, asset, name, side, score, scorePrev, tier, setup, regimeFit, entry, stop, target, rr, whyNow[], sources[], lastUpdate
- `watchlist[]` — same shape, plus dScore, sub-scores (tech, vol, etc.)
- `activeTrades[]` — id, asset, side, entry, stop, target, sizeUsd, ageDays, pnlPct, pnlUsd, regimeAtOpen, scoreAtOpen, scoreNow, status
- `closedTrades[]`, `missedTrades[]`, `processScorecard`, `sourceLeaderboard[]`, `thesisChangelog[]`
- `brainActivity[]`, `costTracker`, `sourceHealth[]`, `mgmt: { todos, decisions, commits }`, `integration`
- `reasoning[signalId]` — composite breakdown, modifiers, sources, theses, agentBreakdown

## Tweaks (in-design controls)

The Tweaks panel persists user choices via the EDITMODE block in `app.jsx`:
- `accent` — gold | amber | green | violet | blue
- `density` — compact | default | cozy
- `showMobile` — toggles the phone preview block on /positioning

## Building this for real

When wiring this into the segment, the cleanest path is:
1. Keep `styles.css` and the atomic components (`components.jsx`) as-is — they're framework-agnostic.
2. Replace `data.js` with live fetches that match the same shapes.
3. Re-implement the view files (`positioning.jsx`, `journal.jsx`, `dev.jsx`) in your stack of choice; the JSX is straightforward and uses no React state libs beyond `useState`.
4. The Reasoning Trail (`reasoning.jsx` + `DrillSheet`) is the most opinionated piece — keep its information architecture (composite breakdown → why now → sources → theses → agent telemetry) even if the chrome changes.

## Stage 2+ notes

- Charts in setup cards are intentionally absent — when chart_vision is online, slot a 120×60 sparkline above the levels grid.
- Mobile preview is a static mock, not a responsive view; phone build is a separate target.
- Source health and cost tracker assume a single tactical contract (see `integration.tactical`) — versioning is part of the contract.
