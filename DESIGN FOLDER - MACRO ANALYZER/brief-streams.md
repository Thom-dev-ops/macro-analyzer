# Streams view — design brief

**Project** · Macro Analyzer / Positioning Desk
**View** · `/streams`
**Build** · 2026.05.23-r4
**Owner** · L. Pirola
**Status** · Stage 1 design · ready for review

---

## 1 · Why this view exists

The positioning desk runs on a thesis-driven framework. A trade only makes sense if
the trader knows *what every contributing voice is currently saying, and how loud they
are saying it.*

Before `/streams`, that signal lived as a list of source pills inside reasoning
trails — readable but invisible at a glance. `/streams` lifts the input layer to its
own view so the trader can spot:

- **What's loud right now** — which themes dominate the desk's attention.
- **What's new** — emerging concepts before they show up in scored signals.
- **Who's repeating whom** — echo chambers that should be down-weighted.
- **Which sources are even in the room** — the registered source library.

The view is read-only. Decisions still happen on `/positioning`. `/streams` exists
to make the *why* legible.

---

## 2 · Information architecture

Four numbered blocks, top → bottom, each answering one question:

| #  | Block | Question it answers |
|----|--------------------------|------------------------------------------------|
| S1 | Theme map               | *Where do today's narratives sit?* |
| S2 | Emerging concepts        | *What's new this week?* |
| S3 | Source graph · echo ties | *Who is repeating whom?* |
| S4 | Source library           | *Which sources are registered, at what weight?* |

The numbering matches the rest of the desk (`01` hero signals, `02` watchlist, …).
`S` prefix marks the streams family so cross-references in journals or change-logs
stay legible.

---

## 3 · S1 · Theme map

### Encoding

- **X axis** — *Narrative lifecycle*. Left = emerging (low first-seen days,
  high velocity). Right = fading (long-lived, decelerating).
- **Y axis** — *Direction × consensus*. Top = bullish. Bottom = bearish.
  Mid band = mixed, labelled on the axis itself.
- **Bubble size** — *Share of attention* = `items × avgSourceWeight`, normalised
  against the loudest theme. Single largest theme caps at ~72px radius;
  smallest emerging theme at ~26px.
- **Bubble color & glow** — direction (green/red/amber radial gradient).
- **Dashed outer ring** — `stage === "emerging"`. Reinforces the X-axis position
  for the trader's peripheral vision.
- **Inner arrow glyph** — velocity sign (↗ accelerating · → flat · ↘ decelerating).
- **Tie strings** (dashed grey) — pairs of themes that share at least one
  contributing source. Reveals cross-theme overlap *visually* instead of as a
  list ("see also").

### Axis chrome (post-review iteration)

Axes were originally too quiet against the dotted backdrop. Updated treatment:

- **Inset gutter** along the bottom and left in `--bg-inset` so axis labels
  read against a clearly-marked margin, not floating in the plot.
- **Solid 2px axis lines** with arrowheads at both ends.
- **Endpoint labels** sized 14px, weight 600, coloured semantically:
  gold "← EMERGING", muted "FADING →", green "BULLISH ↑", red "↓ BEARISH",
  amber "MIXED" on the midline.
- **Tick marks + dashed gridlines** at the 25 / 50 / 75% positions.
- **Quadrant tags** in each corner ("FRESH · BULL", "EXTENDED · BULL",
  "FRESH · BEAR", "FADING · BEAR") — fine print but always visible.
- **Axis titles** below/beside the line: `NARRATIVE LIFECYCLE · first-seen × velocity`
  and `DIRECTION · consensus × tilt`.

### Interaction

- **Hover a bubble** → side panel populates with theme detail, blurb, stats,
  30-day spark, contributing sources (with item counts), and clickable asset
  chips.
- **Click a bubble** → pins the focus card. Click again or press the `UNPIN ×`
  button to release.
- **Asset chip click** → jumps into the existing asset detail page
  (`/asset/URA`, etc.).
- **Direction filter** at top of the block → constrains visible bubbles
  to all / bullish / bearish / mixed.
- **Focus dim** — when a bubble is hovered/pinned, every tie string and every
  unrelated bubble dims to ~10–40% opacity. The focused theme and its
  cross-theme ties stay full strength.

### Why a 2D map (and not just a ranked list)

A bar list answers *what's biggest*. A 2D map answers *what's biggest, in
which direction, at what life stage* in one read. The trader needs all three
to decide whether a theme is worth size or worth ignoring.

---

## 4 · S2 · Emerging concepts

Three to six cards (auto-filtered from themes with `stage === "emerging"`),
sized to fit responsively at 310px minimum width. Each card shows:

- **Eyebrow** — `NEW · 6d ago` plus a novelty score (0–100). Both gold.
- **Title** — serif, italic, ~19px. Concept name only.
- **Blurb** — one or two sentences in `--text-mute`. The story.
- **Stats strip** — velocity, item count, source count. Dashed top/bottom
  borders so this row reads as a distinct fact band, not body text.
- **Sparkline** — 30-day mention trace.
- **Foot** — first three contributing source names as pills, plus `+N` overflow.

Cards have a faint gold wash to mark them as flares against the rest of the
view's neutral surfaces.

---

## 5 · S3 · Source graph · echo ties

A node-link diagram, hand-laid (not force-simulated, for legibility).

### Encoding

- **Bubble** — one per registered source.
- **Bubble radius** — source weight (18–46px).
- **Outer ring color + thickness** — source tier:
  T1 gold / 2.4px, T2 green / 1.8px, T3 amber / 1.2px, T4 muted / 1.2px.
- **Inner fill opacity** — proportional to weight.
- **Internal label** — `T{tier}` badge, source short name, weight, kind ticker
  (e.g. `SUBSTACK`, `RSS`, `DATA`).
- **Tie strings** — curved bezier between every pair of sources that recurringly
  co-cite. Stroke thickness encodes echo weight (0.8 + weight × 3.6 px).
  Stroke color matches the *theme* the echo runs through, so the trader can
  trace a uranium chorus visually distinct from a rates chorus.
- **Glow halo** under each tie at 10% opacity — gives the strings a tube-like
  read at low contrast without hurting legibility.

### Layout

Four implicit clusters, marked in faint mono caps:

```
MACRO · RATES                    ENERGY · COMMODITIES
       └── bianco, fred, epb         └── doomberg, energy-cap, kalecki

NEWS · SOCIAL                    DATA · CRYPTO
       └── zerohedge, woodway        └── glassnode, fred-wti
```

The cluster labels are part of the SVG so they belong to the layout — there are
no quadrant boxes; the trader infers the cluster from where the bubbles sit.

### Interaction

- **Hover a bubble** — that source's ties stay full strength, everything else
  drops to ~12% opacity. Side panel populates with tier badge, kind, weight,
  topics, and a list of every echo tie this source participates in (other source
  name, weight, theme via).
- No click action (yet) — the source library table (S4) is the place for full
  detail.

---

## 6 · S4 · Source library

A standard `wl-table` listing every registered source, sorted by weight desc.
Columns:

| Source | Kind | Tier | Weight (number + bar) | Topics (chips) |
|--------|------|------|------------------------|----------------|

Kept simple on purpose — the table is the canonical reference. The graph above
is for discovery; the table is for verification.

---

## 7 · Design system notes

- Inherits the desk's existing tokens — near-black surfaces, parchment text,
  gold conviction accent, mono for all numerics, Cormorant Garamond for
  italic concept labels.
- Direction semantic colors locked at:
  `--green` `#6fb37a` · `--red` `#d97758` · `--amber` `#d6a04a` · `--gold` `#d6b15a`.
- New custom classes are namespaced:
  - `.theme-map`, `.tm-*` for the theme map
  - `.src-graph`, `.sg-*` for the source graph
  - `.emerging-*` for emerging cards
  - `.src-*` for source library bits
- All charts are hand-rolled inline SVG. No chart library. ViewBox is fixed
  (`1100 × 580` for the theme map, `1100 × 520` for the source graph) so the
  layouts stay readable down to ~960px container width and scale up cleanly to
  retina laptops.

---

## 8 · Open questions / next pass

1. **Drill-down from a bubble to its 30-day source stream** — should clicking a
   theme open a filtered feed of all 14 underlying items? Today it only opens
   the focus card.
2. **Live transitions** — when a theme crosses from `emerging` → `established`
   we should animate the dashed outer ring fading off. Worth doing once we have
   real ingestion.
3. **Density toggle** — Tweak panel could expose a "compact map" preset that
   strips quadrant tags and shrinks padding. Wait for feedback before building.
4. **Per-source position** — current coordinates are hand-tuned. If the registry
   grows past ~25 sources we'll need clustered force-layout with a stable seed,
   or move to a treemap.

---

## 9 · Files

```
data.js          — adds `sources[]`, `themes[]`, `echoLinks[]`
streams.jsx      — Streams, ThemeMapCanvas, SourceGraphCanvas, EmergingCard
styles.css       — Streams block (.theme-map, .src-graph, .emerging-*, .src-*)
app.jsx          — `/streams` route + nav tab
Macro Analyzer.html — script load order
```

End of brief.
