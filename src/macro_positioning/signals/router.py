"""Route documents to the appropriate extractor.

Rule of thumb:
  - Docs whose source already encodes direction + ticker + size in
    structured fields (the insiders package: house, senate, form4,
    13d/g, 13f, usaspending, lda) → rule-based `insider_extractor`.
    No LLM needed — it's deterministic and free.
  - Everything else (manual prose drops, news articles, blogs) →
    `llm_extractor` for prose extraction with Gemini/Claude.

A document can flow through multiple extractors — e.g. a manual chat
drop that the user typed prose into AND attached structured metadata
to. Each extractor's output is persisted as its own Signal rows.
"""

from __future__ import annotations


from macro_positioning.chartlab import DESK_SOURCE_PREFIX
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from macro_positioning.signals.base import ExtractorProtocol


# Source-id prefixes that the insider extractor handles directly.
# All come from insiders.cli.SOURCES; the prefix is the source_slug.
_INSIDER_SOURCE_SLUGS = {
    "house", "senate", "form4", "13dg", "13f",
    "usaspending", "lda",
    # social channels also have structured ticker/symbol output even
    # though their "direction" is implicit. Route them to the insider
    # extractor too so we get cheap signals; the LLM can re-extract
    # later if we want richer thesis context.
    "apewisdom", "stocktwits", "uw",
}


def _source_slug_of(document: dict) -> str:
    """Pull the source_slug from documents.source_id.

    source_id format is `{slug}:{author_id}` for manual and insiders
    (e.g. `manual:gov-insider-nancy-pelosi`). The slug for the routing
    decision is the prefix before the first colon.
    """
    source_id = document.get("source_id", "") or ""
    if ":" in source_id:
        return source_id.split(":", 1)[0]
    return source_id


def _pending_vision(document: dict) -> bool:
    """True when a chart doc is still waiting on the vision drainer.

    Such a doc's `cleaned_text` is only the post caption — the chart image
    has not been OCR'd/described yet. Extracting now reads a fragment and
    invites garbage (URL-laundered tickers, hallucinated theses, inflated
    conviction). We defer until the drainer clears the flag and enriches
    the body. The flag lives in documents.tags_json["pending_vision"].
    """
    raw = document.get("tags_json")
    if not raw:
        return False
    try:
        import json as _json
        tags = _json.loads(raw)
    except (TypeError, ValueError):
        return False
    return bool(isinstance(tags, dict) and tags.get("pending_vision"))


def _has_attachment(document: dict) -> bool:
    """Doc has at least one chart screenshot to pass to vision."""
    raw = document.get("attachment_paths_json")
    if raw:
        try:
            import json as _json
            arr = _json.loads(raw)
            if arr:
                return True
        except (TypeError, ValueError):
            pass
    return bool(document.get("attachment_path"))


def is_structured_insider(document: dict) -> bool:
    """True when the doc has insider-structured metadata we can parse without LLM."""
    slug = _source_slug_of(document)
    # Manual drops can also be "insider"-tagged when the funnel rewrites
    # a ScrapedEvent into a manual payload — those carry the original
    # slug inside user_metadata_json["resolved"]["source"] / source_slug
    # but for routing the documents.source_id is `manual:gov-insider-*`.
    if slug == "manual":
        # Check if author_id encodes one of our insider channels
        author_id = (document.get("author_id") or "").lower()
        return any(t in author_id for t in ("gov-insider", "corp-insider", "fed-spend", "lobbying", "large-holder"))
    return slug in _INSIDER_SOURCE_SLUGS


def choose_extractors(document: dict) -> list[str]:
    """Return ordered list of extractor names to apply to this doc.

    Fan-out rules (multiple extractors per doc is normal — each
    contributes different fields to the same ticker):
      - Insider-structured docs → insider_extractor only. These rarely
        have charts and never have prose theses worth LLM cost.
      - Chart docs with attachments → vision_extractor + llm_extractor.
        Vision contributes levels (stop, target, setup type). LLM
        contributes thesis / horizon / catalyst from the prose body.
      - Pure-prose docs → llm_extractor.

    Order matters when one extractor wants to read another's output;
    today they're independent so order only affects logging.
    """
    if is_structured_insider(document):
        return ["insider_extractor"]
    # Chart docs awaiting OCR carry only their caption as body — defer ALL
    # extraction until the vision drainer enriches them. Returning []
    # leaves the doc pending, so it is re-considered on the next run once
    # pending_vision clears. See _pending_vision.
    if _pending_vision(document):
        return []
    # Telegram-channel chart drops go through the manual/vision.py locked
    # prompt at ingest time; its structured output lives on the document
    # row as extracted_features_json. Project it directly instead of
    # re-running the legacy vision_extractor (which errors ~50% of the
    # time on "no ticker resolvable"). llm_extractor is skipped here —
    # captions on chart forwards are 3–5 words and the vision output
    # already carries the thesis in `notes`.
    # Stock Unlocked relays trade calls as structured TEXT, not charts:
    # ticker, side, entry, every target and the stop are stated in words.
    # A deterministic parse of those words beats an LLM re-reading them,
    # and costs nothing — so this source never touches llm_extractor.
    if (document.get("source_id") or "") == "manual:telegram-channel:stock_unlocked":
        return ["stock_unlocked_extractor"]
    slug = _source_slug_of(document)
    source_id = document.get("source_id", "") or ""
    content_type = document.get("content_type") or ""
    if slug == "manual" and content_type == "manual_chart":
        if source_id.startswith("manual:telegram-channel:"):
            return ["manual_chart_extractor"]
    # Chart Lab: a chart the desk read itself. Same structured output on
    # the same column, so the same projector applies — the separate
    # `desk:` namespace is what keeps the desk's own read out of
    # trusted-voice consensus downstream.
    if slug == "desk" and content_type == "manual_chart":
        if source_id.startswith(DESK_SOURCE_PREFIX):
            return ["manual_chart_extractor"]
    if _has_attachment(document):
        # Vision first so its levels are persisted before the LLM call,
        # which on slow Gemini days can take several seconds.
        return ["vision_extractor", "llm_extractor"]
    return ["llm_extractor"]


def build_registry() -> dict[str, "ExtractorProtocol"]:
    """Lazy-instantiate the extractor registry.

    Kept lazy so importing `signals` doesn't pull in the LLM SDK at
    module load.
    """
    from macro_positioning.signals.insider_extractor import InsiderExtractor
    from macro_positioning.signals.llm_extractor import LLMExtractor
    from macro_positioning.signals.manual_chart_extractor import ManualChartExtractor
    from macro_positioning.signals.stock_unlocked_extractor import StockUnlockedExtractor
    from macro_positioning.signals.vision_extractor import VisionExtractor

    return {
        "insider_extractor": InsiderExtractor(),
        "llm_extractor": LLMExtractor(),
        "manual_chart_extractor": ManualChartExtractor(),
        "stock_unlocked_extractor": StockUnlockedExtractor(),
        "vision_extractor": VisionExtractor(),
    }
