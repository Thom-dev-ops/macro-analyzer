"""Chart Lab — single-chart, single-ticker bench for setup iteration.

The desk drops one chart, reads it, and asks "is this a trade?" without
running a watchlist pass and without spending a cent on vision.

Two things make that possible and both are deliberate:

- **The vision step is pluggable and free either way.** `chart read`
  shells `claude -p` (the Claude Code subscription — see
  `manual.vision._generate_via_cli`); `chart write` takes JSON the
  operator's own Claude Code session produced by Reading the image.
  Neither path touches the billed Anthropic API.
- **Desk reads are not KOL calls.** A chart the desk reads itself lands
  under `DESK_SOURCE_PREFIX` with an author that matches none of
  `manual.authors.SEEDED_AUTHOR_WHERE`, so it becomes a signal the bench
  can use but never contributes to trusted-voice consensus, conviction,
  or the positioning maps. Your own opinion must not come back to you
  wearing a KOL's credential.
"""

# documents.source_id prefix for a desk-authored chart drop. The router
# and ManualChartExtractor both gate on this exact string.
DESK_SOURCE_PREFIX = "desk:chartlab:"

# Default attribution for a desk drop. `slugify_author` turns this into
# `chart-lab:desk`, which matches no clause in SEEDED_AUTHOR_WHERE — that
# exclusion is the whole point, so change it only with that in mind.
DESK_AUTHOR_CHANNEL = "chart-lab"
DESK_AUTHOR_NAME = "desk"

__all__ = ["DESK_SOURCE_PREFIX", "DESK_AUTHOR_CHANNEL", "DESK_AUTHOR_NAME"]
