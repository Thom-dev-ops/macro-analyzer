"""Scheduled refresh loop — daily ingest + pipeline + memo generation.

Run as:
  python -m macro_positioning.ingestion.scheduler --once        # single run
  python -m macro_positioning.ingestion.scheduler --cron        # stay running
  python -m macro_positioning.ingestion.scheduler --morning     # morning only

Cron cadence (defaults):
  07:00  — fetch overnight newsletters + news, run brain, generate memo
  12:00  — refresh FRED data + mid-day news
  16:30  — post-close recap: refresh charts, reconcile positions
  Sun    — weekly review: aggregate thesis performance, source scoring
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime

from macro_positioning.pipelines.run_pipeline import build_pipeline

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------

def morning_run() -> dict:
    """07:00 daily — overnight newsletter ingest + full pipeline + memo.

    Steps:
      1. fetch_and_persist() personal Gmail newsletters
      2. Fetch Google News RSS for all macro topics
      3. Run full pipeline with any freshly-ingested docs
    """
    logger.info("=== MORNING RUN START %s ===", datetime.now(UTC).isoformat())
    started = time.time()

    summary: dict = {
        "timestamp": datetime.now(UTC).isoformat(),
        "steps": {},
    }

    # 1. Personal Gmail newsletters
    try:
        from macro_positioning.ingestion import personal_gmail

        gmail_result = personal_gmail.fetch_and_persist(days=2)
        summary["steps"]["gmail"] = {"status": "ok", **gmail_result}
    except Exception as e:
        logger.error("Gmail step failed: %s", e, exc_info=True)
        summary["steps"]["gmail"] = {"status": "error", "error": str(e)}

    # 2. Google News RSS topics
    fresh_docs = []
    try:
        from macro_positioning.ingestion import google_news_rss

        fresh_docs = google_news_rss.fetch_all_macro_topics(max_items_per_topic=10)
        summary["steps"]["google_news"] = {
            "status": "ok",
            "documents_fetched": len(fresh_docs),
        }
    except Exception as e:
        logger.error("Google News step failed: %s", e, exc_info=True)
        summary["steps"]["google_news"] = {"status": "error", "error": str(e)}

    # 3. Podcast episodes — core-tier get full transcription via N8N/Gemini,
    #    secondary-tier stay show-notes-only
    try:
        from macro_positioning.brain.transcription import is_configured as audio_ready
        from macro_positioning.ingestion import podcast_rss

        transcribe_core = audio_ready()
        core_pods = [p for p in podcast_rss.PODCAST_SOURCES if p.priority == "core"]
        sec_pods = [p for p in podcast_rss.PODCAST_SOURCES if p.priority != "core"]

        pod_docs: list = []
        for p in core_pods:
            pod_docs.extend(podcast_rss.fetch_podcast(
                p.source_id, max_items=3, transcribe=transcribe_core,
            ))
        for p in sec_pods:
            pod_docs.extend(podcast_rss.fetch_podcast(
                p.source_id, max_items=5, transcribe=False,
            ))

        fresh_docs.extend(pod_docs)
        summary["steps"]["podcasts"] = {
            "status": "ok",
            "core_transcribed": transcribe_core,
            "documents_fetched": len(pod_docs),
        }
    except Exception as e:
        logger.error("Podcast step failed: %s", e, exc_info=True)
        summary["steps"]["podcasts"] = {"status": "error", "error": str(e)}

    # 3b. Substack — operator-curated newsletter feeds. Each post lands
    #     with source_id='substack:{slug}' so attribution stays clean.
    try:
        from macro_positioning.ingestion import substack

        substack_docs = substack.fetch_all(max_items_per_feed=10)
        fresh_docs.extend(substack_docs)
        summary["steps"]["substack"] = {
            "status": "ok",
            "documents_fetched": len(substack_docs),
            "feeds_configured": len(substack.load_feeds()),
        }
    except Exception as e:
        logger.error("Substack step failed: %s", e, exc_info=True)
        summary["steps"]["substack"] = {"status": "error", "error": str(e)}

    # 4. Run pipeline with fresh RSS + podcast + substack docs (Gmail persisted directly)
    try:
        pipeline = build_pipeline()
        result = pipeline.run(fresh_docs)
        summary["steps"]["pipeline"] = {
            "status": "ok",
            "documents_ingested": result.documents_ingested,
            "theses_extracted": result.theses_extracted,
            "memo_id": result.memo_id,
        }
    except Exception as e:
        logger.error("Pipeline step failed: %s", e, exc_info=True)
        summary["steps"]["pipeline"] = {"status": "error", "error": str(e)}

    # 5. Insiders: pull all free public-disclosure sources (House/Senate
    # PTRs, SEC Form 4/13F/13D-G, USAspending, LDA, social trending).
    # `catch_errors=True` so one bad source never aborts the morning run.
    try:
        from macro_positioning.insiders import cli as insiders_cli
        insiders_summary = insiders_cli.pull_all(catch_errors=True)
        summary["steps"]["insiders"] = {
            "status": "ok",
            "by_source": insiders_summary,
        }
    except Exception as e:
        logger.error("Insiders step failed: %s", e, exc_info=True)
        summary["steps"]["insiders"] = {"status": "error", "error": str(e)}

    # 6. Signals: extract structured positioning signals from any docs
    # the prior ingest steps just landed. Runs BEFORE scoring so the
    # composer sees today's fresh signals.
    try:
        from macro_positioning.signals.runner import extract_pending
        sig_summary = extract_pending(limit=500, since_days=14)
        summary["steps"]["signals"] = {
            "status": "ok",
            **sig_summary.to_dict(),
        }
    except Exception as e:
        logger.error("Signals step failed: %s", e, exc_info=True)
        summary["steps"]["signals"] = {"status": "error", "error": str(e)}

    # 7. Prices: yfinance OHLCV for the active watchlist + any tickers
    # that produced signals in the last 14d. Without this, every
    # technical feature degrades to {n_bars: 0} and the composer can't
    # synthesize entry/stop/target.
    try:
        import sqlite3 as _sqlite3
        from macro_positioning.core.settings import settings
        from macro_positioning.prices.fetcher import fetch_and_persist as fetch_prices_persist
        from macro_positioning.scoring.watchlist_resolver import resolve_watchlist

        from macro_positioning.prices.symbol_map import crypto_majors, resolve_symbol

        # Anchors + theme-aligned for active regime hint.
        resolved = resolve_watchlist(framework_regime="commodity_led_inflation")
        tickers: set[str] = {e.ticker for e in resolved.entries}
        tickers |= set(crypto_majors())
        # Plus recently-signalled tickers so the composer can score them.
        # Resolved first: these arrive as raw pairs ('BTC/USDT', 'KINS/SOL')
        # and yfinance answers none of them in that form.
        with _sqlite3.connect(settings.sqlite_path) as _conn:
            for (t,) in _conn.execute(
                "SELECT DISTINCT asset_ticker FROM signals "
                "WHERE extracted_at >= datetime('now','-14 day')"
            ).fetchall():
                key = resolve_symbol(str(t or ""))
                if key:
                    tickers.add(key)
        if tickers:
            price_result = fetch_prices_persist(sorted(tickers), days=200)
            summary["steps"]["prices"] = {
                "status": "ok",
                "tickers_requested": price_result.tickers_requested,
                "tickers_with_data": price_result.tickers_with_data,
                "bars_persisted": price_result.bars_persisted,
                "failures": len(price_result.failures or []),
            }
        else:
            summary["steps"]["prices"] = {
                "status": "ok",
                "skipped": "no tickers resolved",
            }
    except Exception as e:
        logger.error("Prices step failed: %s", e, exc_info=True)
        summary["steps"]["prices"] = {"status": "error", "error": str(e)}

    # 8. FRED: macro indicators. Incremental refresh by default; if the
    # table is empty (fresh DB), fall through to a 5y backfill so the
    # regime quadrant / FCI / EPU / COT panels have data.
    try:
        import sqlite3 as _sqlite3
        from macro_positioning.core.settings import settings
        from macro_positioning.market.fred_history import (
            backfill_series,
            incremental_refresh,
        )
        from macro_positioning.market.fred_provider import (
            ALL_SERIES,
            FREDMarketDataProvider,
        )

        if not settings.fred_api_key:
            summary["steps"]["fred"] = {
                "status": "skipped",
                "reason": "MPA_FRED_API_KEY not configured",
            }
        else:
            provider = FREDMarketDataProvider()
            with _sqlite3.connect(settings.sqlite_path) as _conn:
                n_rows = _conn.execute(
                    "SELECT COUNT(*) FROM fred_observations"
                ).fetchone()[0]
                if n_rows == 0:
                    # Fresh DB → backfill 5y of history so the indicator
                    # strip lights up immediately rather than waiting
                    # for the daily refresh to slowly populate.
                    total = 0
                    for sid in ALL_SERIES.keys():
                        try:
                            total += backfill_series(
                                provider, _conn, sid, start="2021-01-01"
                            )
                        except Exception:
                            logger.exception("FRED backfill failed for %s", sid)
                    summary["steps"]["fred"] = {
                        "status": "ok",
                        "mode": "backfill",
                        "rows_persisted": total,
                    }
                else:
                    counts = incremental_refresh(
                        provider, _conn, ALL_SERIES.keys(), window_days=14
                    )
                    summary["steps"]["fred"] = {
                        "status": "ok",
                        "mode": "incremental",
                        "rows_upserted": sum(counts.values()),
                        "series": len(counts),
                    }
    except Exception as e:
        logger.error("FRED step failed: %s", e, exc_info=True)
        summary["steps"]["fred"] = {"status": "error", "error": str(e)}

    # 9. Scoring pass: resolve watchlist + score each ticker + persist
    # to trade_scores. This is the step whose absence kept the dashboard
    # empty for weeks. Per-ticker errors are already isolated inside
    # run_scoring_pass; the outer try is for total-failure cases (e.g.
    # macro_brain import error).
    try:
        from macro_positioning.scoring.runner import run_scoring_pass
        score_summary = run_scoring_pass()
        summary["steps"]["scoring"] = {
            "status": "ok",
            "run_id": score_summary.run_id,
            "watchlist_size": score_summary.watchlist_size,
            "scored": score_summary.scored,
            "persisted": score_summary.persisted,
            "errors": len(score_summary.errors or []),
        }
    except Exception as e:
        logger.error("Scoring step failed: %s", e, exc_info=True)
        summary["steps"]["scoring"] = {"status": "error", "error": str(e)}

    summary["duration_seconds"] = round(time.time() - started, 2)
    logger.info("=== MORNING RUN END (%.2fs) ===", summary["duration_seconds"])
    return summary


def midday_refresh() -> dict:
    """12:00 — quick FRED + mid-day news refresh."""
    logger.info("=== MIDDAY REFRESH ===")
    started = time.time()
    summary: dict = {"timestamp": datetime.now(UTC).isoformat(), "steps": {}}

    try:
        from macro_positioning.ingestion import google_news_rss

        docs = google_news_rss.fetch_all_macro_topics(max_items_per_topic=5)
        summary["steps"]["google_news"] = {"status": "ok", "documents_fetched": len(docs)}
    except Exception as e:
        logger.error("Midday google_news failed: %s", e, exc_info=True)
        summary["steps"]["google_news"] = {"status": "error", "error": str(e)}

    summary["duration_seconds"] = round(time.time() - started, 2)
    return summary


def post_close_recap() -> dict:
    """16:30 — EOD reconciliation + position review."""
    logger.info("=== POST-CLOSE RECAP ===")
    return {"status": "pending_impl", "timestamp": datetime.now(UTC).isoformat()}


def weekly_review() -> dict:
    """Sunday — thesis outcome attribution + source weight recalibration."""
    logger.info("=== WEEKLY REVIEW ===")
    return {"status": "pending_impl", "timestamp": datetime.now(UTC).isoformat()}


# ---------------------------------------------------------------------------
# Scheduler runner
# ---------------------------------------------------------------------------

def _safe(fn):
    """Wrap a scheduled task so exceptions don't kill the scheduler."""
    def wrapped():
        try:
            logger.info("Scheduled task '%s' starting", fn.__name__)
            result = fn()
            logger.info("Scheduled task '%s' finished: %s", fn.__name__, result)
        except Exception as e:
            logger.error("Scheduled task '%s' crashed: %s", fn.__name__, e, exc_info=True)
    wrapped.__name__ = f"safe_{fn.__name__}"
    return wrapped


def run_cron(timezone_name: str = "America/Los_Angeles") -> None:
    """Start the APScheduler loop. Blocks the process.

    Requires the `apscheduler` optional dependency
    (installed via `pip install .[stream-a]`).
    """
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except ImportError as e:
        raise RuntimeError(
            "apscheduler not installed. Install optional deps: pip install .[stream-a]"
        ) from e

    # job_defaults make the scheduler resilient to laptop sleep: if the Mac is
    # asleep at a job's scheduled time, the fire is "missed". misfire_grace_time
    # =None tells APScheduler to run it anyway whenever the process next wakes
    # (no lateness limit), and coalesce=True collapses several missed fires
    # (e.g. asleep across two mornings) into a single catch-up run.
    sched = BlockingScheduler(
        timezone=timezone_name,
        job_defaults={"misfire_grace_time": None, "coalesce": True},
    )
    sched.add_job(_safe(morning_run), "cron", hour=7, minute=0, id="morning_run")
    sched.add_job(_safe(midday_refresh), "cron", hour=12, minute=0, id="midday_refresh")
    sched.add_job(_safe(post_close_recap), "cron", hour=16, minute=30, id="post_close_recap")
    sched.add_job(_safe(weekly_review), "cron", day_of_week="sun", hour=9, id="weekly_review")

    logger.info("Starting APScheduler loop (tz=%s)", timezone_name)
    sched.start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run morning_run once and exit")
    parser.add_argument("--morning", action="store_true", help="Run morning_run")
    parser.add_argument("--midday", action="store_true", help="Run midday_refresh")
    parser.add_argument("--close", action="store_true", help="Run post_close_recap")
    parser.add_argument("--weekly", action="store_true", help="Run weekly_review")
    parser.add_argument("--cron", action="store_true", help="Start cron scheduler")
    args = parser.parse_args()

    if args.cron:
        run_cron()
    elif args.morning or args.once:
        print(morning_run())
    elif args.midday:
        print(midday_refresh())
    elif args.close:
        print(post_close_recap())
    elif args.weekly:
        print(weekly_review())
    else:
        parser.print_help()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
