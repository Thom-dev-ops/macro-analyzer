"""Command-line entrypoint for ad-hoc pipeline runs.

Usage:
    python -m macro_positioning.cli sample
    python -m macro_positioning.cli rss --feed alpha=https://example.com/feed.xml
    python -m macro_positioning.cli text --source-id local --title "My note" --file note.txt
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from macro_positioning.core.models import RawDocument
from macro_positioning.core.settings import settings
from macro_positioning.db.schema import initialize_database
from macro_positioning.ingestion.rss_connector import ingest_feeds
from macro_positioning.ingestion.sample_sources import sample_context, sample_documents
from macro_positioning.ingestion.source_lifecycle import (
    add_source,
    archive_source,
    count_by_priority,
    promote_source,
    retag_source,
    summarize_sources,
)
from macro_positioning.learning import (
    attribution as learning_attribution,
    author_attribution as learning_author_attribution,
    backfill_model_versions as learning_backfill_model_versions,
    backfill_quality_scores as learning_backfill_quality_scores,
    conviction_calibration as learning_conviction_calibration,
    mention_precision as learning_mention_precision,
    quality_summary as learning_quality_summary,
    regime_accuracy as learning_regime_accuracy,
    retrain_status as learning_retrain_status,
    score_outcome_correlation as learning_correlation,
    signal_attribution as learning_signal_attribution,
    signal_history as learning_signal_history,
    version_stats as learning_version_stats,
)
from macro_positioning.pipelines.run_pipeline import build_pipeline
from macro_positioning.prices.fetcher import fetch_and_persist as fetch_prices_persist
from macro_positioning.scoring.runner import run_scoring_pass
from macro_positioning.scoring.watchlist_resolver import resolve_watchlist


def _parse_feed_arg(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(
            f"--feed expects source_id=url form, got {raw!r}"
        )
    source_id, url = raw.split("=", 1)
    return source_id.strip(), url.strip()


def cmd_sample(_: argparse.Namespace) -> int:
    pipeline = build_pipeline()
    result = pipeline.run(sample_documents(), context=sample_context())
    print(result.model_dump_json(indent=2))
    return 0


def cmd_rss(args: argparse.Namespace) -> int:
    feeds: list[tuple[str, str]] = args.feed or []
    if not feeds:
        print("No feeds provided. Use --feed source_id=url", file=sys.stderr)
        return 2
    documents = ingest_feeds(feeds, max_items_per_feed=args.max_items)
    if not documents:
        print("Fetched 0 documents from feeds.", file=sys.stderr)
        return 1
    pipeline = build_pipeline()
    result = pipeline.run(documents)
    print(result.model_dump_json(indent=2))
    return 0


def cmd_text(args: argparse.Namespace) -> int:
    text_body = Path(args.file).read_text() if args.file else args.text
    if not text_body:
        print("Provide --file or --text", file=sys.stderr)
        return 2
    doc = RawDocument(
        source_id=args.source_id,
        title=args.title,
        url=args.url,
        published_at=datetime.now(timezone.utc),
        author=args.author,
        content_type="note",
        raw_text=text_body,
        tags=args.tag or [],
    )
    pipeline = build_pipeline()
    result = pipeline.run([doc])
    print(result.model_dump_json(indent=2))
    return 0


def cmd_sources_list(args: argparse.Namespace) -> int:
    rows = summarize_sources(include_archived=args.all)
    if not rows:
        print("(no sources)")
        return 0
    # Header + rows; minimal, monospaced, columnar output
    print(f"{'PRIORITY':<10}  {'TYPE':<14}  {'TRUST':>6}  {'SOURCE_ID':<28}  TAGS")
    print("-" * 100)
    for r in rows:
        tags_str = ",".join(r.routing_tags[:6]) + ("…" if len(r.routing_tags) > 6 else "")
        print(
            f"{r.priority:<10}  {r.source_type:<14}  {r.trust_weight:>6.2f}  {r.source_id:<28}  {tags_str}"
        )
    print()
    counts = count_by_priority()
    summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"summary: total={sum(counts.values())}  {summary}")
    return 0


def cmd_sources_add(args: argparse.Namespace) -> int:
    try:
        rec = add_source(
            args.source_id,
            name=args.name,
            source_type=args.type,
            author=args.author or "",
            priority=args.priority,
            trust_weight=args.trust,
            market_focus=args.focus or [],
            routing_tags=args.tag or [],
            fetch_cadence=args.cadence,
            freshness_sla_hours=args.sla,
            channels=[{"channel_type": "url", "label": "primary", "url": args.url}] if args.url else [],
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Onboarded {rec.source_id} (priority={rec.priority}, trust={rec.trust_weight}, tags={','.join(rec.routing_tags) or '-'})")
    return 0


def cmd_sources_archive(args: argparse.Namespace) -> int:
    try:
        rec = archive_source(args.source_id)
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Archived {rec.source_id} on {rec.archived_at}")
    return 0


def cmd_sources_promote(args: argparse.Namespace) -> int:
    try:
        rec = promote_source(args.source_id, args.to)
    except (KeyError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Promoted {rec.source_id} → priority={rec.priority}")
    return 0


def cmd_sources_retag(args: argparse.Namespace) -> int:
    try:
        rec = retag_source(args.source_id, add=args.add or [], remove=args.remove or [])
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"{rec.source_id} routing_tags: {','.join(rec.routing_tags) or '-'}")
    return 0


def cmd_prices_fetch(args: argparse.Namespace) -> int:
    """Fetch + persist daily OHLCV bars for tickers (yfinance default)."""
    if args.ticker:
        tickers = list(args.ticker)
    elif args.watchlist:
        # Resolve current watchlist (anchors + theme tickers for current regime)
        # Mention extraction skipped here — we just need the set to fetch.
        # Use a default regime; runner re-resolves at scoring time anyway.
        resolved = resolve_watchlist(framework_regime="commodity_led_inflation")
        tickers = [e.ticker for e in resolved.entries]
    else:
        print("Provide --watchlist or --ticker T (repeatable)", file=sys.stderr)
        return 2

    result = fetch_prices_persist(tickers, days=args.days)
    print(f"Price fetch via {result.provider}")
    print(f"  Requested      : {result.tickers_requested}")
    print(f"  With data      : {result.tickers_with_data}")
    print(f"  Bars persisted : {result.bars_persisted}")
    if result.failures:
        print(f"  Failures       : {len(result.failures)}")
        for f in result.failures[:8]:
            print(f"    - {f.get('ticker'):<6}: {f.get('error')}")
    return 0 if not result.failures else 1


def cmd_fred_backfill(args: argparse.Namespace) -> int:
    """Backfill maximum-available history for catalogued FRED series."""
    import sqlite3
    from macro_positioning.core.settings import settings
    from macro_positioning.db.schema import initialize_database
    from macro_positioning.market.fred_history import (
        backfill_series,
        latest_observation_date,
    )
    from macro_positioning.market.fred_provider import (
        ALL_SERIES,
        FREDMarketDataProvider,
    )

    initialize_database(settings.sqlite_path)
    provider = FREDMarketDataProvider()

    series_ids = list(args.series) if args.series else list(ALL_SERIES.keys())
    start = args.start or "1900-01-01"

    total = 0
    populated = 0
    failures: list[tuple[str, str]] = []

    with sqlite3.connect(settings.sqlite_path) as conn:
        for sid in series_ids:
            try:
                n = backfill_series(provider, conn, sid, start=start)
                total += n
                if n:
                    populated += 1
                latest = latest_observation_date(conn, sid)
                print(f"  {sid:<18} {n:>7} rows  latest={latest}")
            except Exception as e:
                failures.append((sid, f"{type(e).__name__}: {e}"))
                print(f"  {sid:<18} FAILED  {type(e).__name__}: {e}")

    print(f"\nBackfill complete: {populated}/{len(series_ids)} series populated, {total} rows total")
    if failures:
        print(f"Failures: {len(failures)}")
    return 0 if not failures else 1


def cmd_fred_refresh(args: argparse.Namespace) -> int:
    """Incremental refresh: last N days per series, idempotent."""
    import sqlite3
    from macro_positioning.core.settings import settings
    from macro_positioning.db.schema import initialize_database
    from macro_positioning.market.fred_history import incremental_refresh
    from macro_positioning.market.fred_provider import (
        ALL_SERIES,
        FREDMarketDataProvider,
    )

    initialize_database(settings.sqlite_path)
    provider = FREDMarketDataProvider()
    with sqlite3.connect(settings.sqlite_path) as conn:
        counts = incremental_refresh(
            provider, conn, ALL_SERIES.keys(), window_days=args.days,
        )
    written = sum(counts.values())
    print(f"Incremental refresh: {written} rows upserted across {len(counts)} series")
    return 0


def cmd_score_bootstrap(args: argparse.Namespace) -> int:
    """One-shot orchestrator: prices fetch → FRED backfill/refresh → score run.

    Designed for the very first run on a fresh DB or to manually
    repopulate the positioning desk without waiting for tomorrow's
    morning_run. Each step is independently skippable and partial
    failures don't abort downstream steps.
    """
    import sqlite3
    import time as _time

    from macro_positioning.core.settings import settings
    from macro_positioning.db.schema import initialize_database
    from macro_positioning.prices.fetcher import fetch_and_persist as fetch_prices_persist
    from macro_positioning.scoring.runner import run_scoring_pass
    from macro_positioning.scoring.watchlist_resolver import resolve_watchlist

    initialize_database(settings.sqlite_path)
    t_total = _time.time()
    summary: dict = {"steps": {}}

    # ── Step 1: prices ──────────────────────────────────────────────────
    if not args.skip_prices:
        t0 = _time.time()
        try:
            resolved = resolve_watchlist(framework_regime="commodity_led_inflation")
            tickers = [e.ticker for e in resolved.entries]
            if args.anchors_only:
                # Limit to anchors-only for first-pass speed (~50 tickers).
                anchors = {
                    e.ticker for e in resolved.entries
                    if any(o.startswith("anchor") for o in (e.origins or []))
                }
                tickers = [t for t in tickers if t in anchors]
            if not tickers:
                summary["steps"]["prices"] = {"status": "skipped",
                                              "reason": "no tickers resolved"}
            else:
                r = fetch_prices_persist(tickers, days=200)
                summary["steps"]["prices"] = {
                    "status": "ok",
                    "tickers_requested": r.tickers_requested,
                    "tickers_with_data": r.tickers_with_data,
                    "bars_persisted": r.bars_persisted,
                    "failures": len(r.failures or []),
                    "elapsed_s": round(_time.time() - t0, 1),
                }
                if args.verbose:
                    print(f"[prices] {r.bars_persisted} bars across "
                          f"{r.tickers_with_data}/{r.tickers_requested} tickers "
                          f"({summary['steps']['prices']['elapsed_s']}s)")
        except Exception as e:
            summary["steps"]["prices"] = {
                "status": "error", "error": f"{type(e).__name__}: {e}",
            }
            print(f"[prices] ERROR: {e}", file=sys.stderr)
    else:
        summary["steps"]["prices"] = {"status": "skipped", "reason": "--skip-prices"}

    # ── Step 2: FRED ────────────────────────────────────────────────────
    if not args.skip_fred:
        t0 = _time.time()
        try:
            if not settings.fred_api_key:
                summary["steps"]["fred"] = {
                    "status": "skipped",
                    "reason": "MPA_FRED_API_KEY not configured",
                }
            else:
                from macro_positioning.market.fred_history import (
                    backfill_series, incremental_refresh,
                )
                from macro_positioning.market.fred_provider import (
                    ALL_SERIES, FREDMarketDataProvider,
                )
                provider = FREDMarketDataProvider()
                with sqlite3.connect(settings.sqlite_path) as conn:
                    n_existing = conn.execute(
                        "SELECT COUNT(*) FROM fred_observations"
                    ).fetchone()[0]
                    if n_existing == 0:
                        total = 0
                        for sid in ALL_SERIES.keys():
                            try:
                                total += backfill_series(
                                    provider, conn, sid, start="2021-01-01"
                                )
                            except Exception:
                                pass
                        summary["steps"]["fred"] = {
                            "status": "ok", "mode": "backfill",
                            "rows_persisted": total,
                            "elapsed_s": round(_time.time() - t0, 1),
                        }
                    else:
                        counts = incremental_refresh(
                            provider, conn, ALL_SERIES.keys(), window_days=14,
                        )
                        summary["steps"]["fred"] = {
                            "status": "ok", "mode": "incremental",
                            "rows_upserted": sum(counts.values()),
                            "series": len(counts),
                            "elapsed_s": round(_time.time() - t0, 1),
                        }
                if args.verbose:
                    print(f"[fred] {summary['steps']['fred']}")
        except Exception as e:
            summary["steps"]["fred"] = {
                "status": "error", "error": f"{type(e).__name__}: {e}",
            }
            print(f"[fred] ERROR: {e}", file=sys.stderr)
    else:
        summary["steps"]["fred"] = {"status": "skipped", "reason": "--skip-fred"}

    # ── Step 3: scoring pass ────────────────────────────────────────────
    if not args.skip_score:
        t0 = _time.time()
        try:
            s = run_scoring_pass()
            summary["steps"]["scoring"] = {
                "status": "ok",
                "run_id": s.run_id,
                "framework_regime": s.framework_regime,
                "watchlist_size": s.watchlist_size,
                "scored": s.scored,
                "persisted": s.persisted,
                "errors": len(s.errors or []),
                "elapsed_s": round(_time.time() - t0, 1),
            }
            if args.verbose:
                print(f"[scoring] {summary['steps']['scoring']}")
        except Exception as e:
            summary["steps"]["scoring"] = {
                "status": "error", "error": f"{type(e).__name__}: {e}",
            }
            print(f"[scoring] ERROR: {e}", file=sys.stderr)
    else:
        summary["steps"]["scoring"] = {"status": "skipped", "reason": "--skip-score"}

    summary["total_elapsed_s"] = round(_time.time() - t_total, 1)

    import json as _json
    print(_json.dumps(summary, indent=2))

    # Non-zero only if every step failed.
    statuses = [v.get("status") for v in summary["steps"].values()]
    if all(s == "error" for s in statuses):
        return 1
    return 0


def cmd_score_run(args: argparse.Namespace) -> int:
    """Run a scoring pass: resolve watchlist (anchors + themes + mentions),
    score each ticker via macro_brain orchestrator, persist to trade_scores.
    """
    summary = run_scoring_pass(
        framework_regime_hint=args.regime_hint,
        persist=not args.dry_run,
        docs_window_days=args.window,
    )
    print(f"Scoring pass {summary.run_id[:8]}")
    print(f"  Regime    : {summary.framework_regime} (thesis: {summary.thesis_regime})")
    print(f"  Watchlist : {summary.watchlist_size} tickers")
    print(f"  Scored    : {summary.scored}{' (dry-run, not persisted)' if args.dry_run else f' (persisted: {summary.persisted})'}")
    if summary.mention_summary:
        print(f"  Mentions  :")
        for window, info in sorted(summary.mention_summary.items()):
            top_str = ", ".join(f"{t['ticker']}({t['docs']})" for t in info.get("top_5", []))
            print(f"    {window:>3}d : {info.get('total_docs_scanned', 0)} docs scanned, "
                  f"{info.get('tickers_above_threshold', 0)} tickers above threshold "
                  f"{f'· top: {top_str}' if top_str else ''}")
    if summary.errors:
        print(f"  Errors    : {len(summary.errors)}")
        for err in summary.errors[:5]:
            print(f"    - {err.get('ticker')}: {err.get('error')}")
    return 0 if not summary.errors else 1


# ---------------------------------------------------------------------------
# learning — read-side analytics over agent_call_log / source_outcomes / etc.
# ---------------------------------------------------------------------------

def _learning_connect():
    import sqlite3
    initialize_database(settings.sqlite_path)
    return sqlite3.connect(settings.sqlite_path)


def cmd_learning_attribution(args: argparse.Namespace) -> int:
    import json as _json
    conn = _learning_connect()
    try:
        rows = learning_attribution(conn, window_days=args.window)
    finally:
        conn.close()
    print(_json.dumps(rows, indent=2))
    return 0


def cmd_learning_signals(args: argparse.Namespace) -> int:
    import json as _json
    conn = _learning_connect()
    try:
        horizons = tuple(args.horizons) if args.horizons else (7, 30, 90)
        rows = learning_signal_attribution(
            conn, horizons=horizons, sort_mode=args.sort_mode
        )
    finally:
        conn.close()
    print(_json.dumps(rows, indent=2))
    return 0


def cmd_learning_authors(args: argparse.Namespace) -> int:
    import json as _json
    conn = _learning_connect()
    try:
        horizons = tuple(args.horizons) if args.horizons else (7, 30, 90)
        rows = learning_author_attribution(
            conn, horizons=horizons, include_meta=args.with_meta
        )
    finally:
        conn.close()
    print(_json.dumps(rows, indent=2))
    return 0


def cmd_learning_conviction(args: argparse.Namespace) -> int:
    import json as _json
    conn = _learning_connect()
    try:
        horizons = tuple(args.horizons) if args.horizons else (7, 30, 90)
        result = learning_conviction_calibration(conn, horizons=horizons)
    finally:
        conn.close()
    print(_json.dumps(result, indent=2))
    return 0


def cmd_learning_signal_history(args: argparse.Namespace) -> int:
    import json as _json
    conn = _learning_connect()
    try:
        rows = learning_signal_history(
            conn, args.source_id, horizon=args.horizon, bucket=args.bucket
        )
    finally:
        conn.close()
    print(_json.dumps(rows, indent=2))
    return 0


def cmd_learning_correlation(args: argparse.Namespace) -> int:
    import json as _json
    conn = _learning_connect()
    try:
        result = learning_correlation(conn)
    finally:
        conn.close()
    print(_json.dumps(result, indent=2))
    return 0


def cmd_learning_version_backfill(args: argparse.Namespace) -> int:
    import json as _json
    conn = _learning_connect()
    try:
        result = learning_backfill_model_versions(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    print(_json.dumps(result, indent=2))
    return 0


def cmd_learning_version_stats(args: argparse.Namespace) -> int:
    import json as _json
    conn = _learning_connect()
    try:
        result = learning_version_stats(conn)
    finally:
        conn.close()
    print(_json.dumps(result, indent=2))
    return 0


def cmd_learning_quality_backfill(args: argparse.Namespace) -> int:
    import json as _json
    conn = _learning_connect()
    try:
        result = learning_backfill_quality_scores(
            conn, since_days=args.since, dry_run=args.dry_run
        )
    finally:
        conn.close()
    print(_json.dumps(result, indent=2))
    return 0


def cmd_learning_quality_summary(args: argparse.Namespace) -> int:
    import json as _json
    conn = _learning_connect()
    try:
        result = learning_quality_summary(conn)
    finally:
        conn.close()
    print(_json.dumps(result, indent=2))
    return 0


def cmd_learning_regime_accuracy(args: argparse.Namespace) -> int:
    import json as _json
    conn = _learning_connect()
    try:
        result = learning_regime_accuracy(conn, lookback_months=args.lookback_months)
    finally:
        conn.close()
    print(_json.dumps(result, indent=2))
    return 0


def cmd_learning_retrain_status(args: argparse.Namespace) -> int:
    import json as _json
    conn = _learning_connect()
    try:
        result = learning_retrain_status(conn)
    finally:
        conn.close()
    print(_json.dumps(result, indent=2))


def _journal_connect():
    import sqlite3
    initialize_database(settings.sqlite_path)
    return sqlite3.connect(settings.sqlite_path)


def cmd_journal_pending(_: argparse.Namespace) -> int:
    import json as _json
    from macro_positioning.journal import repository as jrepo
    conn = _journal_connect()
    try:
        rows = jrepo.list_pending(conn)
    finally:
        conn.close()
    if not rows:
        print("(no pending reviews)")
        return 0
    print(_json.dumps(rows, indent=2, default=str))
    return 0


def cmd_journal_close(args: argparse.Namespace) -> int:
    from macro_positioning.journal import webhook as jwebhook
    conn = _journal_connect()
    try:
        try:
            result = jwebhook.receive_close_event(
                conn,
                trade_id=args.trade_id,
                exit_date=args.exit_date,
                exit_price=args.exit_price,
                pnl=args.pnl,
                pnl_percent=args.pnl_percent,
                execution_notes=args.notes,
            )
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        conn.commit()
    finally:
        conn.close()
    print(f"flipped {result['trade_id']} → {result['review_status']}")
    return 0


_THESIS_CHOICES = (
    "fully_right",
    "right_outcome_wrong_reason",
    "right_thesis_wrong_outcome",
    "fully_wrong",
)
_HINDSIGHT_CHOICES = ("over", "right", "under")
_RETAKE_CHOICES = ("yes", "no", "modified")
_SURPRISE_CHOICES = ("macro", "sector", "liquidity", "idiosyncratic", "none")


def _prompt_choice(label: str, choices: tuple[str, ...]) -> str:
    while True:
        print(f"{label} [{'|'.join(choices)}]: ", end="", flush=True)
        v = input().strip()
        if v in choices:
            return v
        print(f"  pick one of: {', '.join(choices)}")


def _prompt_int(label: str, lo: int, hi: int) -> int:
    while True:
        print(f"{label} [{lo}-{hi}]: ", end="", flush=True)
        v = input().strip()
        try:
            n = int(v)
        except ValueError:
            print("  enter an integer")
            continue
        if lo <= n <= hi:
            return n
        print(f"  in range {lo}-{hi}")


def _prompt_multi(label: str, choices: tuple[str, ...]) -> list[str]:
    print(f"{label} (comma-separated, choices: {', '.join(choices)}): ", end="", flush=True)
    raw = input().strip()
    if not raw:
        return []
    picks = [p.strip() for p in raw.split(",") if p.strip()]
    bad = [p for p in picks if p not in choices]
    if bad:
        print(f"  ignoring unknown: {bad}")
    return [p for p in picks if p in choices]


def cmd_journal_review(args: argparse.Namespace) -> int:
    from macro_positioning.journal import feedback_writer as jfw, repository as jrepo
    print(f"\n— Review trade {args.trade_id} —\n")
    review = {
        "thesis_validity": _prompt_choice("Q1 thesis validity", _THESIS_CHOICES),
    }
    print("Q2 sources_credited (comma-separated source_ids, or blank):")
    raw = input().strip()
    review["sources_credited"] = [s.strip() for s in raw.split(",") if s.strip()] if raw else []
    review["execution_scores"] = {
        "entry": _prompt_int("Q3a entry quality", 1, 5),
        "stop": _prompt_int("Q3b stop quality", 1, 5),
        "sizing": _prompt_int("Q3c sizing quality", 1, 5),
        "exit": _prompt_int("Q3d exit quality", 1, 5),
    }
    review["setup_score_hindsight"] = _prompt_choice("Q4 setup score in hindsight", _HINDSIGHT_CHOICES)
    review["surprise_factor"] = _prompt_multi("Q5 surprise factors", _SURPRISE_CHOICES)
    print("Q5b surprise note (optional, single line): ", end="", flush=True)
    review["surprise_note"] = input().strip() or None
    print("Q6 one-line lesson: ", end="", flush=True)
    review["lesson"] = input().strip()[:240]
    review["would_retake"] = _prompt_choice("Q7 would retake", _RETAKE_CHOICES)
    review["free_form_notes"] = None

    conn = _journal_connect()
    try:
        try:
            review_id = jrepo.insert_review(conn, args.trade_id, review)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        feedback = jfw.apply_review_feedback(conn, args.trade_id, review)
        conn.commit()
    finally:
        conn.close()
    print(
        f"\nsubmitted {review_id} · "
        f"source_outcomes={feedback['source_outcomes_written']} · "
        f"calibration_appended={feedback['calibration_appended']}"
    )
    return 0


def cmd_learning_mention_precision(args: argparse.Namespace) -> int:
    import json as _json
    conn = _learning_connect()
    try:
        result = learning_mention_precision(
            conn,
            k=args.k,
            score_threshold=args.threshold,
            horizon_days=args.horizon,
        )
    finally:
        conn.close()
    print(_json.dumps(result, indent=2))
    return 0


def cmd_rules_check(args: argparse.Namespace) -> int:
    """Run a TradeProposal through the gate evaluator and print the decision."""
    import json as _json
    import sqlite3

    from macro_positioning.rules.gate import (
        GateDecision,
        TradeProposal,
        evaluate_trade_proposal,
    )

    try:
        p, f, i = (int(x) for x in args.confluence.split(","))
    except (ValueError, AttributeError):
        print(
            "error: --confluence wants 3 comma-separated ints, e.g. 3,2,1",
            file=sys.stderr,
        )
        return 2

    tps_tuple: tuple[float, ...] = ()
    if args.tps:
        try:
            tps_tuple = tuple(float(x) for x in args.tps.split(","))
        except ValueError:
            print("error: --tps wants comma-separated numbers", file=sys.stderr)
            return 2

    proposal = TradeProposal(
        ticker=args.ticker,
        side=args.side,
        entry=args.entry,
        stop=args.stop,
        position_size=args.size,
        account_equity=args.equity,
        confluence_subscores=(p, f, i),
        setup_category=args.setup,
        tps=tps_tuple,
    )

    initialize_database(settings.sqlite_path)
    conn = sqlite3.connect(settings.sqlite_path)
    try:
        decision: GateDecision = evaluate_trade_proposal(
            proposal, conn, mode=args.mode
        )
    finally:
        conn.close()

    print(_json.dumps(decision.as_dict(), indent=2))
    return 0 if decision.approved else 1


def cmd_rules_plan(args: argparse.Namespace) -> int:
    """Capture an entry-time trade plan."""
    import json as _json
    import sqlite3

    from macro_positioning.rules import repository as rrepo
    from macro_positioning.rules.confluence import score_confluence
    from macro_positioning.rules.portfolio import bucket_for_ticker
    from macro_positioning.rules.risk import account_risk_pct

    tps: list[float] = []
    if args.tps:
        try:
            tps = [float(x) for x in args.tps.split(",")]
        except ValueError:
            print("error: --tps wants comma-separated numbers", file=sys.stderr)
            return 2

    confluence_total = pattern_s = fib_s = ind_s = None
    if args.confluence:
        try:
            p, f, i = (int(x) for x in args.confluence.split(","))
        except ValueError:
            print("error: --confluence wants 3 comma-separated ints", file=sys.stderr)
            return 2
        cb = score_confluence(p, f, i)
        confluence_total, pattern_s, fib_s, ind_s = cb.total, cb.pattern, cb.fib, cb.indicator

    initialize_database(settings.sqlite_path)
    conn = sqlite3.connect(settings.sqlite_path)
    try:
        # Resolve ticker → bucket
        row = conn.execute(
            """
            SELECT a.ticker
            FROM trades t
            LEFT JOIN assets a ON a.asset_id = t.asset_id
            WHERE t.trade_id = ?
            """,
            (args.trade_id,),
        ).fetchone()
        if row is None:
            print(f"error: unknown trade_id {args.trade_id!r}", file=sys.stderr)
            return 1
        bucket = bucket_for_ticker(row[0] or "")

        risk_pct = None
        if args.equity is not None:
            try:
                risk_pct = account_risk_pct(args.entry, args.stop, args.size, args.equity)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1

        plan_payload = {
            "planned_entry": args.entry,
            "planned_stop": args.stop,
            "planned_tps": tps,
            "planned_size": args.size,
            "planned_account_equity": args.equity,
            "planned_risk_pct": risk_pct,
            "planned_setup_category": args.category,
            "planned_confluence_score": confluence_total,
            "planned_pattern_subscore": pattern_s,
            "planned_fib_subscore": fib_s,
            "planned_indicator_subscore": ind_s,
            "planned_correlated_bucket": bucket,
            "planned_entry_strategy": args.strategy,
            "notes": args.notes,
        }
        try:
            plan_id = rrepo.save_plan(conn, args.trade_id, plan_payload)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except sqlite3.IntegrityError:
            print(f"error: plan already exists for trade {args.trade_id!r}", file=sys.stderr)
            return 1
        rrepo.hydrate_trade_rule_columns(
            conn, args.trade_id,
            setup_category=args.category,
            confluence_score=confluence_total,
            pattern_subscore=pattern_s,
            fib_subscore=fib_s,
            indicator_subscore=ind_s,
            account_risk_pct=risk_pct,
            correlated_bucket=bucket,
            entry_followed_retest=(
                1 if args.strategy == "breakout_retest"
                else 0 if args.strategy is not None
                else None
            ),
        )
        conn.commit()
    finally:
        conn.close()

    print(_json.dumps({
        "plan_id": plan_id,
        "trade_id": args.trade_id,
        "planned_risk_pct": risk_pct,
        "planned_confluence_score": confluence_total,
        "planned_correlated_bucket": bucket,
    }, indent=2))
    return 0


def cmd_rules_adherence(args: argparse.Namespace) -> int:
    """Show the rule-adherence breakdown for a trade (re-computed live)."""
    import json as _json
    import sqlite3

    from macro_positioning.journal import repository as jrepo
    from macro_positioning.rules import repository as rrepo
    from macro_positioning.rules.adherence import compute_adherence

    conn = sqlite3.connect(settings.sqlite_path)
    try:
        conn.row_factory = sqlite3.Row
        trade_row = conn.execute(
            "SELECT * FROM trades WHERE trade_id = ?", (args.trade_id,)
        ).fetchone()
        if trade_row is None:
            print(f"error: unknown trade_id {args.trade_id!r}", file=sys.stderr)
            return 1
        plan = rrepo.get_plan(conn, args.trade_id)
        review = jrepo.get_review(conn, args.trade_id)
        result = compute_adherence(dict(trade_row), plan, review)
    finally:
        conn.close()
    print(_json.dumps(result.as_dict(), indent=2))
    return 0


def cmd_insiders_pull(args: argparse.Namespace) -> int:
    from macro_positioning.insiders import cli as insiders_cli

    if args.source == "all":
        summary = insiders_cli.pull_all(since=args.since)
    else:
        summary = insiders_cli.pull_one(args.source, since=args.since, year=args.year)
    print(summary)
    return 0


def cmd_signals_extract(args: argparse.Namespace) -> int:
    """Run pending signal extraction over recent documents."""
    import json as _json

    from macro_positioning.signals.runner import extract_pending

    summary = extract_pending(
        limit=args.limit,
        since_days=args.since_days,
        extractor_filter=args.extractor,
        dry_run=args.dry_run,
    )
    print(_json.dumps(summary.to_dict(), indent=2))
    return 0


def cmd_signals_show(args: argparse.Namespace) -> int:
    """Print recent signals or signals for a specific ticker."""
    import json as _json

    from macro_positioning.signals import repository

    if args.ticker:
        rows = repository.load_active_signals_for_ticker(
            args.ticker, since_days=args.since_days
        )
    else:
        rows = repository.load_recent_signals(limit=args.limit)
    print(_json.dumps(rows, indent=2, default=str))
    return 0


def cmd_signals_status(_: argparse.Namespace) -> int:
    """Per-extractor counts + most recent extraction time."""
    import json as _json

    from macro_positioning.signals import repository

    print(_json.dumps(repository.signal_counts_by_extractor(), indent=2))
    return 0


def cmd_learning_recompute_trust(args: argparse.Namespace) -> int:
    """Run one pass of trust-weight calibration over closed trades."""
    import json as _json

    from macro_positioning.learning.signal_calibration import recompute_trust_weights

    run = recompute_trust_weights(
        link_window_days=args.link_window_days,
        neutral_band=args.neutral_band,
        alpha=args.alpha,
        min_signals_for_update=args.min_signals,
        dry_run=args.dry_run,
    )
    print(_json.dumps(run.summary(), indent=2))
    return 0


def cmd_insiders_status(_: argparse.Namespace) -> int:
    from macro_positioning.insiders import cli as insiders_cli

    rows = insiders_cli.status()
    if not rows:
        print("(no insiders_cursor rows yet — run `insiders pull` first)")
        return 0
    for row in rows:
        print(
            f"{row['source_slug']:<12} "
            f"last_id={row['last_external_id']!s:<24} "
            f"at={row['last_run_at']} "
            f"status={row['last_run_status']}"
        )
    return 0


# ---------------------------------------------------------------------------
# chart lab — single-chart, single-ticker bench
# ---------------------------------------------------------------------------

def _chart_fmt(value, digits: int = 4) -> str:
    """Prices vary from 0.0001 to 100000 across this book — don't force 2dp."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    if abs(v) >= 1:
        return f"{v:,.3f}".rstrip("0").rstrip(".")
    return f"{v:.{digits + 2}f}".rstrip("0").rstrip(".")


def cmd_chart_add(args: argparse.Namespace) -> int:
    """Park a chart image as a desk document awaiting a read."""
    from macro_positioning.chartlab import store as chartlab_store

    try:
        drop = chartlab_store.add_chart(
            args.image,
            ticker=args.ticker,
            timeframe=args.timeframe,
            note=args.note or "",
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"chart add failed: {exc}", file=sys.stderr)
        return 2

    print(f"Parked {Path(args.image).name}")
    print(f"  document_id : {drop.document_id}")
    print(f"  stored at   : {drop.attachment_path}")
    print(f"  source_id   : {drop.source_id}")
    print(f"  ticker      : {drop.ticker or '(undeclared)'}")
    print()
    print("Next: read it (free, either way)")
    print(f"  in this session : macro-positioning chart read --doc {drop.document_id}")
    print(f"  unattended      : macro-positioning chart read --doc {drop.document_id} --auto")
    return 0


def cmd_chart_grab(args: argparse.Namespace) -> int:
    """Park a chart without typing a path — clipboard, screenshot, or inbox.

    You cannot drag a file into this terminal, so intake comes to the
    image instead. Defaults to the clipboard because ⌃⇧⌘4 puts a
    screenshot there directly, which is the shortest path from "chart on
    screen" to "chart parked".
    """
    from macro_positioning.chartlab import intake
    from macro_positioning.chartlab import store as chartlab_store

    source = (args.source or "clipboard").lower()
    picked: list[tuple[Path, Path | None]] = []  # (image, inbox original)

    if source == "clipboard":
        got = intake.grab_clipboard()
        if got is None:
            print("Nothing image-shaped on the clipboard.", file=sys.stderr)
            print(file=sys.stderr)
            for line in intake.describe_sources():
                print(f"  {line}", file=sys.stderr)
            return 2
        picked.append((got, None))

    elif source == "screenshot":
        got = intake.newest_screenshot()
        if got is None:
            print(
                f"No image found in {intake.screenshot_dir()}.", file=sys.stderr
            )
            return 2
        age = intake.age_minutes(got)
        print(f"Newest screenshot: {got.name}  ({age:.0f} min old)")
        if age > 10 and not args.yes:
            # The newest file on the Desktop is not necessarily the chart
            # just taken — confirm rather than park a stale one silently.
            reply = input("That is not recent. Park it anyway? [y/N] ").strip().lower()
            if reply not in {"y", "yes"}:
                print("Nothing parked.")
                return 0
        picked.append((got, None))

    elif source == "inbox":
        images = intake.inbox_images()
        if not images:
            print(f"Inbox is empty: {intake.inbox_dir()}", file=sys.stderr)
            print("Drag any chart image in there, then run this again.", file=sys.stderr)
            return 2
        if args.ticker and len(images) > 1:
            print(
                f"{len(images)} images in the inbox but one --ticker given. "
                "Parking them all under that ticker; use one at a time if "
                "they are different names.",
            )
        picked.extend((img, img) for img in images)

    else:
        path = Path(source).expanduser()
        if not path.is_file():
            print(f"Not a file: {path}", file=sys.stderr)
            return 2
        picked.append((path, None))

    parked = 0
    for image, from_inbox in picked:
        try:
            drop = chartlab_store.add_chart(
                image,
                ticker=args.ticker,
                timeframe=args.timeframe,
                note=args.note or "",
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"skipped {image.name}: {exc}", file=sys.stderr)
            continue
        parked += 1
        if from_inbox is not None:
            intake.mark_parked(from_inbox)
        print(f"Parked {image.name}")
        print(f"  document_id : {drop.document_id}")
        print(f"  ticker      : {drop.ticker or '(undeclared)'}")

    if not parked:
        return 1
    print()
    print("Next: macro-positioning chart read --latest")
    return 0


def cmd_chart_list(args: argparse.Namespace) -> int:
    """Recent desk chart drops."""
    from macro_positioning.chartlab import store as chartlab_store

    rows = chartlab_store.list_charts(limit=args.limit, ticker=args.ticker)
    if not rows:
        print("No chart-lab drops yet. Add one: macro-positioning chart add <image> --ticker T")
        return 0
    for row in rows:
        read = "read" if row.get("extracted_features_json") else "PENDING"
        caption = (row.get("raw_text") or "").replace("\n", " ")[:44]
        print(
            f"{row['document_id'][:12]}  {row['ingested_at'][:16]}  "
            f"{read:<8}  {caption}"
        )
    return 0


def cmd_chart_read(args: argparse.Namespace) -> int:
    """Read a parked chart — in-session by default, `claude -p` with --auto.

    Without --auto this prints the image path and the SECTION 10 skeleton
    and stops. That is the zero-subprocess path: the operator's own
    Claude Code session Reads the image, fills the JSON, and hands it to
    `chart write`. With --auto the same read happens through the CLI
    backend so it can run unattended. Neither path bills the API.
    """
    import json as _json

    from macro_positioning.chartlab import store as chartlab_store

    if args.latest:
        doc = chartlab_store.latest_chart(args.ticker)
        if doc is None:
            print("No chart-lab drops found.", file=sys.stderr)
            return 2
        document_id = doc["document_id"]
    elif args.doc:
        document_id = args.doc
        doc = chartlab_store.get_chart(document_id)
        if doc is None:
            print(f"No such document: {document_id}", file=sys.stderr)
            return 2
    else:
        print("Provide --doc ID or --latest", file=sys.stderr)
        return 2

    if args.auto:
        try:
            result = chartlab_store.read_chart_auto(document_id, model=args.model)
        except (KeyError, ValueError, RuntimeError) as exc:
            print(f"read failed: {exc}", file=sys.stderr)
            return 1
        print(f"Read {result.document_id} → {result.ticker or '(no ticker)'}")
        print(f"  setups  : {result.setups}")
        print(f"  signals : {len(result.signals)}")
        if result.signal_error:
            print(f"  signal extraction error: {result.signal_error}")
        return 0

    path = doc.get("attachment_path") or ""
    abs_path = Path(path)
    if not abs_path.is_absolute():
        abs_path = Path(settings.base_dir) / path

    print(f"document_id : {document_id}")
    print(f"image       : {abs_path}")
    caption = (doc.get("raw_text") or "").strip()
    if caption:
        print(f"caption     : {caption}")
    print()
    print("Read the image above, then fill this and pipe it back:")
    print(f"  macro-positioning chart write --doc {document_id} --json -")
    print()
    print(_json.dumps(chartlab_store.SECTION_10_TEMPLATE, indent=2))
    return 0


def cmd_chart_write(args: argparse.Namespace) -> int:
    """Attach a chart read to a drop and emit its signals."""
    import json as _json

    from macro_positioning.chartlab import store as chartlab_store

    raw = sys.stdin.read() if args.json == "-" else Path(args.json).read_text()
    try:
        features = _json.loads(raw)
    except ValueError as exc:
        print(f"not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(features, dict):
        print("extraction must be a JSON object", file=sys.stderr)
        return 2

    try:
        result = chartlab_store.write_extraction(
            args.doc, features, extract_signals=not args.no_signals
        )
    except (KeyError, TypeError) as exc:
        print(f"write failed: {exc}", file=sys.stderr)
        return 2

    print(f"Wrote read for {result.document_id} → {result.ticker or '(no ticker)'}")
    print(f"  setups  : {result.setups}")
    print(f"  signals : {len(result.signals)}")
    if result.signal_error:
        print(f"  signal extraction error: {result.signal_error}")
    if result.ticker:
        print()
        print(f"Next: macro-positioning chart bench {result.ticker}")
    return 0


def cmd_chart_bench(args: argparse.Namespace) -> int:
    """The single-ticker setup card."""
    import json as _json

    from macro_positioning.chartlab.bench import build_bench

    try:
        card = build_bench(
            args.ticker,
            days=args.days,
            fetch=not args.no_fetch,
            side_override=args.side,
        )
    except ValueError as exc:
        print(f"bench failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        from dataclasses import asdict

        print(_json.dumps(asdict(card), indent=2, default=str))
        return 0

    f = _chart_fmt
    print(f"{card.ticker}  ({card.resolved_symbol or '?'})")
    print("─" * 58)
    print(
        f"price {f(card.close)}   ATR(14) {f(card.atr)}   bars {card.n_bars}"
    )

    if card.desk_read:
        read = card.desk_read
        stamp = (read.get("_ingested_at") or "")[:10]
        print()
        print(f"your chart read   {stamp}  [{read.get('timeframe') or '?'}]")
        print(
            f"  call_type {read.get('call_type') or '?'} · bias "
            f"{read.get('bias') or '?'} · pattern {read.get('pattern') or '—'}"
        )
        if read.get("notes"):
            print(f"  {read['notes']}")
    else:
        print()
        print("your chart read   none on file for this ticker")

    if card.supports or card.resistances:
        print()
        print("structure")
        for lv in card.supports[:3]:
            print(
                f"  sup  {f(lv['low'])}–{f(lv['high'])}  "
                f"{lv['touches']}x, {lv['last_touch_bars']}d ago"
                f"{'  [flipped]' if lv['flipped'] else ''}"
            )
        for lv in card.resistances[:3]:
            print(
                f"  res  {f(lv['low'])}–{f(lv['high'])}  "
                f"{lv['touches']}x, {lv['last_touch_bars']}d ago"
            )

    if card.levels:
        lv = card.levels
        print()
        print(f"levels   [{lv.get('method')} · {lv.get('version')}]  side {card.side}")
        print(f"  basis: {card.side_basis}")
        print(
            f"  entry {f(lv.get('entry'))}   stop {f(lv.get('stop'))}   "
            f"target {f(lv.get('target'))}   RR {lv.get('rr'):.2f}"
            if lv.get("rr") is not None
            else f"  entry {f(lv.get('entry'))}   stop {f(lv.get('stop'))}"
        )
        for prov in (lv.get("provenance") or [])[:5]:
            role = prov.get("role") or "?"
            who = f"  [{prov['who']}]" if prov.get("who") else ""
            print(f"    {role:<18}{f(prov.get('value'))}  {prov.get('basis') or ''}{who}")
        for rej in (lv.get("rejected") or [])[:3]:
            print(
                f"    rejected {rej.get('role') or '?'} {f(rej.get('value'))}"
                f"  ({rej.get('source') or '?'}): {rej.get('reason') or ''}"
            )

    if card.desk_levels:
        dl = card.desk_levels
        print()
        rr = f"  R:R {dl['rr']:.2f}" if dl.get("rr") is not None else ""
        print(f"your levels vs the agent{rr}")
        print(f"  {'':<8}{'yours':>12}{'agent':>14}   gap")
        for role in ("entry", "stop", "target"):
            row = dl["rows"][role]
            gap_r = row.get("gap_r")
            gap = f"{gap_r:+.2f}R" if gap_r is not None else "—"
            print(
                f"  {role:<8}{f(row['desk']):>12}{f(row['agent']):>14}   {gap}"
            )
        if dl.get("invalidation"):
            print(f"  invalidation: {dl['invalidation']}")
        if dl.get("entry_reached") is False:
            print("  entry not yet reached — price has not come to your level")

    print()
    if card.kol_n_signals:
        print(f"trusted voices   {card.kol_n_signals} signals"
              f"{f', {card.kol_played_out} played out' if card.kol_played_out else ''}")
        for label, consensus in (
            ("entry", card.kol_entry),
            ("stop", card.kol_stop),
            ("target", card.kol_target),
        ):
            if consensus:
                mark = "✓" if consensus["trusted"] else "·"
                drift = consensus.get("pct_from_spot")
                # A level far from spot is one the market has left behind;
                # say so on the row rather than leaving it to be inferred.
                away = f"  ({drift:+.0f}% vs spot)" if drift is not None else ""
                stale = "  ← price has left this behind" if drift is not None and abs(drift) > 15 else ""
                print(
                    f"  {mark} {label:<7}{f(consensus['price'])}{away}{stale}"
                )
                print(f"          {consensus['basis']}")
    else:
        print("trusted voices   no levels on file for this ticker")

    if card.setup_type:
        print()
        print(f"setup type   {card.setup_type}")
    if card.grade:
        print(
            f"grade        {card.grade}"
            f"{f'  ({card.score:.0f}/100)' if card.score is not None else ''}"
            f"{f'  ·  {card.timing}' if card.timing else ''}"
        )
        for row in card.components:
            val = row.get("value")
            mark = "  (stub)" if row.get("stub") else ""
            print(
                f"    {str(row['component']):<32}"
                f"{(val if val is not None else 0):.2f} × {row['weight']:>2}{mark}"
            )

    if card.exits:
        ex = card.exits
        print()
        print("exits")
        for trim in ex["trims"]:
            print(
                f"  trim {int(trim['fraction'] * 100)}%  @ {f(trim['at'])}"
                f"   {trim['basis']}"
            )
        print(f"  runner {int(ex['runner'] * 100)}%   stop {f(ex['stop'])}"
              f"   → breakeven at {f(ex['move_stop_to_breakeven_at'])}")
        if card.headroom_r is not None:
            print(f"  headroom {card.headroom_r:.1f}R to target 2")

    if card.warnings:
        print()
        for warn in card.warnings:
            print(f"!  {warn}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="macro-positioning",
        description="Macro Positioning Analyzer CLI",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable INFO-level logging"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sample = sub.add_parser("sample", help="run the pipeline against the bundled sample data")
    p_sample.set_defaults(func=cmd_sample)

    p_rss = sub.add_parser("rss", help="ingest RSS feeds and run the pipeline")
    p_rss.add_argument(
        "--feed",
        action="append",
        type=_parse_feed_arg,
        help="repeatable: source_id=feed_url",
    )
    p_rss.add_argument("--max-items", type=int, default=15)
    p_rss.set_defaults(func=cmd_rss)

    p_text = sub.add_parser("text", help="ingest a single text blob or file")
    p_text.add_argument("--source-id", required=True)
    p_text.add_argument("--title", required=True)
    p_text.add_argument("--file", help="path to a text file")
    p_text.add_argument("--text", help="inline raw text")
    p_text.add_argument("--url", default=None)
    p_text.add_argument("--author", default=None)
    p_text.add_argument("--tag", action="append", default=None)
    p_text.set_defaults(func=cmd_text)

    # ---- sources management ------------------------------------------------
    p_sources = sub.add_parser("sources", help="manage the canonical source registry (config/sources.json)")
    sources_sub = p_sources.add_subparsers(dest="sources_command", required=True)

    p_list = sources_sub.add_parser("list", help="list active sources (use --all to include archived)")
    p_list.add_argument("--all", action="store_true", help="include archived sources")
    p_list.set_defaults(func=cmd_sources_list)

    p_add = sources_sub.add_parser("add", help="onboard a new source")
    p_add.add_argument("source_id", help="snake_case unique id")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--type", required=True, help="newsletter|podcast|rss|api|gmail|manual_notes|chart")
    p_add.add_argument("--author", default=None)
    p_add.add_argument("--priority", default="trial", choices=["core", "secondary", "trial"])
    p_add.add_argument("--trust", type=float, default=0.5)
    p_add.add_argument("--focus", action="append", default=None, help="repeatable: market_focus tag")
    p_add.add_argument("--tag", action="append", default=None, help="repeatable: routing_tag")
    p_add.add_argument("--cadence", default="manual", help="ISO-8601 duration or 'manual' or 'realtime'")
    p_add.add_argument("--sla", type=int, default=None, help="freshness_sla_hours")
    p_add.add_argument("--url", default=None, help="primary channel URL")
    p_add.set_defaults(func=cmd_sources_add)

    p_arch = sources_sub.add_parser("archive", help="archive a source (soft delete)")
    p_arch.add_argument("source_id")
    p_arch.set_defaults(func=cmd_sources_archive)

    p_promote = sources_sub.add_parser("promote", help="change a source's priority")
    p_promote.add_argument("source_id")
    p_promote.add_argument("--to", required=True, choices=["core", "secondary", "trial", "archived"])
    p_promote.set_defaults(func=cmd_sources_promote)

    p_retag = sources_sub.add_parser("retag", help="adjust a source's routing_tags")
    p_retag.add_argument("source_id")
    p_retag.add_argument("--add", action="append", default=None, help="repeatable: tag to add")
    p_retag.add_argument("--remove", action="append", default=None, help="repeatable: tag to remove")
    p_retag.set_defaults(func=cmd_sources_retag)

    # ---- chart lab ----------------------------------------------------------
    p_chart = sub.add_parser(
        "chart",
        help="chart lab: drop one chart, read it free, bench one ticker",
    )
    chart_sub = p_chart.add_subparsers(dest="chart_command", required=True)

    p_cadd = chart_sub.add_parser("add", help="park a chart image awaiting a read")
    p_cadd.add_argument("image", help="path to the chart image (png/jpg/webp)")
    p_cadd.add_argument("--ticker", default=None, help="symbol on the chart (RIG, BTC)")
    p_cadd.add_argument("--timeframe", default=None, help="chart timeframe (15m, 4h, 1D, 1W)")
    p_cadd.add_argument("--note", default=None, help="one-line context for the read")
    p_cadd.set_defaults(func=cmd_chart_add)

    p_cgrab = chart_sub.add_parser(
        "grab",
        help="park a chart from the clipboard / newest screenshot / drop folder",
    )
    p_cgrab.add_argument(
        "--from",
        dest="source",
        default="clipboard",
        help=(
            "clipboard (default) | screenshot | inbox | an explicit path. "
            "⌃⇧⌘4 screenshots to the clipboard; ⇧⌘4 writes a file."
        ),
    )
    p_cgrab.add_argument("--ticker", default=None, help="symbol on the chart (RIG, BTC)")
    p_cgrab.add_argument("--timeframe", default=None, help="chart timeframe (15m, 4h, 1D, 1W)")
    p_cgrab.add_argument("--note", default=None, help="one-line context for the read")
    p_cgrab.add_argument("--yes", action="store_true", help="skip the stale-screenshot prompt")
    p_cgrab.set_defaults(func=cmd_chart_grab)

    p_clist = chart_sub.add_parser("list", help="recent chart-lab drops")
    p_clist.add_argument("--ticker", default=None, help="filter to one symbol")
    p_clist.add_argument("--limit", type=int, default=20, help="rows (default 20)")
    p_clist.set_defaults(func=cmd_chart_list)

    p_cread = chart_sub.add_parser(
        "read",
        help="read a parked chart (prints image + schema; --auto shells `claude -p`)",
    )
    g_read = p_cread.add_mutually_exclusive_group(required=True)
    g_read.add_argument("--doc", default=None, help="document_id from `chart add`")
    g_read.add_argument("--latest", action="store_true", help="most recent drop")
    p_cread.add_argument("--ticker", default=None, help="with --latest: newest for this symbol")
    p_cread.add_argument(
        "--auto",
        action="store_true",
        help="read via the `claude -p` CLI backend instead of in-session (still free)",
    )
    p_cread.add_argument("--model", default=None, help="override vision model for --auto")
    p_cread.set_defaults(func=cmd_chart_read)

    p_cwrite = chart_sub.add_parser("write", help="attach a chart read + emit signals")
    p_cwrite.add_argument("--doc", required=True, help="document_id from `chart add`")
    p_cwrite.add_argument("--json", required=True, help="path to SECTION 10 JSON, or - for stdin")
    p_cwrite.add_argument(
        "--no-signals",
        action="store_true",
        help="persist the read but skip signal extraction",
    )
    p_cwrite.set_defaults(func=cmd_chart_write)

    p_cbench = chart_sub.add_parser("bench", help="the single-ticker setup card")
    p_cbench.add_argument("ticker", help="symbol to bench (RIG, BTC, URA)")
    p_cbench.add_argument("--side", default=None, choices=["LONG", "SHORT"], help="force direction")
    p_cbench.add_argument("--days", type=int, default=260, help="bar history (default 260)")
    p_cbench.add_argument("--no-fetch", action="store_true", help="DB bars only, never hit yfinance")
    p_cbench.add_argument("--json", action="store_true", help="emit the card as JSON")
    p_cbench.set_defaults(func=cmd_chart_bench)

    # ---- prices -------------------------------------------------------------
    p_prices = sub.add_parser("prices", help="fetch + persist daily OHLCV bars")
    prices_sub = p_prices.add_subparsers(dest="prices_command", required=True)

    p_pf = prices_sub.add_parser("fetch", help="fetch daily prices for tickers (yfinance)")
    g = p_pf.add_mutually_exclusive_group(required=False)
    g.add_argument("--watchlist", action="store_true", help="fetch every ticker in active watchlist (anchors + themes)")
    p_pf.add_argument("--ticker", action="append", default=None, help="repeatable: bare ticker (URA, BTC, DXY)")
    p_pf.add_argument("--days", type=int, default=200, help="history depth (default 200, enough for 200DMA)")
    p_pf.set_defaults(func=cmd_prices_fetch)

    # ---- fred ---------------------------------------------------------------
    p_fred = sub.add_parser("fred", help="FRED historical data ops")
    fred_sub = p_fred.add_subparsers(dest="fred_command", required=True)

    p_bf = fred_sub.add_parser("backfill", help="backfill full available history")
    p_bf.add_argument("--series", action="append", default=None, help="repeatable: FRED series ID")
    p_bf.add_argument("--start", default=None, help="observation_start (default 1900-01-01)")
    p_bf.set_defaults(func=cmd_fred_backfill)

    p_rf = fred_sub.add_parser("refresh", help="incremental last-N-days refresh")
    p_rf.add_argument("--days", type=int, default=7, help="window days (default 7)")
    p_rf.set_defaults(func=cmd_fred_refresh)

    # ---- scoring ------------------------------------------------------------
    p_score = sub.add_parser("score", help="run brain scoring against the active watchlist")
    score_sub = p_score.add_subparsers(dest="score_command", required=True)

    p_run = score_sub.add_parser("run", help="resolve watchlist + score each ticker + persist")
    p_run.add_argument("--regime-hint", default=None, help="override thesis regime (e.g. 'commodity_expansion')")
    p_run.add_argument("--dry-run", action="store_true", help="compute but don't persist to trade_scores")
    p_run.add_argument("--window", type=int, default=90, help="document lookback days for mention extraction")
    p_run.set_defaults(func=cmd_score_run)

    p_boot = score_sub.add_parser(
        "bootstrap",
        help="one-shot orchestrator: prices fetch → FRED backfill/refresh → score run",
    )
    p_boot.add_argument("--skip-prices", action="store_true",
                        help="skip the yfinance price fetch step")
    p_boot.add_argument("--skip-fred", action="store_true",
                        help="skip the FRED refresh/backfill step")
    p_boot.add_argument("--skip-score", action="store_true",
                        help="skip the final scoring pass")
    p_boot.add_argument("--anchors-only", action="store_true",
                        help="limit price fetch to config/watchlist.json anchors (faster first pass)")
    p_boot.add_argument("--verbose", action="store_true",
                        help="print each step's status as it completes")
    p_boot.set_defaults(func=cmd_score_bootstrap)

    # ---- learning -----------------------------------------------------------
    p_learn = sub.add_parser("learning", help="read-side analytics over the data flywheel")
    learn_sub = p_learn.add_subparsers(dest="learning_command", required=True)

    p_attr = learn_sub.add_parser(
        "attribution",
        help="per-source closed-trade P&L (lens 1a) over a rolling window",
    )
    p_attr.add_argument("--window", type=int, default=30, help="rolling window in days (default 30)")
    p_attr.set_defaults(func=cmd_learning_attribution)

    p_sig = learn_sub.add_parser(
        "signals",
        help="per-source forward-return on every mention (lens 1b)",
    )
    p_sig.add_argument(
        "--horizon",
        dest="horizons",
        type=int,
        action="append",
        help="repeatable: forward-return horizon in days (defaults to 7,30,90)",
    )
    p_sig.add_argument(
        "--sort-mode",
        default="decay_weighted",
        choices=["decay_weighted", "raw_return"],
        help="default decay_weighted: hit_rate × log(1+n) × recency decay",
    )
    p_sig.set_defaults(func=cmd_learning_signals)

    p_auth = learn_sub.add_parser(
        "authors",
        help="per-author hit-rate + forward-return on manual drops (R1)",
    )
    p_auth.add_argument(
        "--horizon",
        dest="horizons",
        type=int,
        action="append",
        help="repeatable: forward-return horizon in days (defaults to 7,30,90)",
    )
    p_auth.add_argument(
        "--with-meta",
        action="store_true",
        help="include _meta diagnostic block (recommended on empty DB)",
    )
    p_auth.set_defaults(func=cmd_learning_authors)

    p_conv = learn_sub.add_parser(
        "conviction-calibration",
        help="bucket forward returns by user.conviction (1-5) (R2)",
    )
    p_conv.add_argument(
        "--horizon",
        dest="horizons",
        type=int,
        action="append",
        help="repeatable: forward-return horizon in days (defaults to 7,30,90)",
    )
    p_conv.set_defaults(func=cmd_learning_conviction)

    p_hist = learn_sub.add_parser(
        "signal-history",
        help="time-series of one source's signal performance (monthly buckets)",
    )
    p_hist.add_argument("--source-id", required=True)
    p_hist.add_argument("--horizon", type=int, default=30)
    p_hist.add_argument("--bucket", default="month", choices=["month"])
    p_hist.set_defaults(func=cmd_learning_signal_history)

    p_corr = learn_sub.add_parser(
        "correlation",
        help="Spearman ρ between trade scores and realized P&L",
    )
    p_corr.set_defaults(func=cmd_learning_correlation)

    p_mp = learn_sub.add_parser(
        "mention-precision",
        help="precision@k of mention-driven watchlist promotions",
    )
    p_mp.add_argument("--k", type=int, default=10)
    p_mp.add_argument("--threshold", type=int, default=70, help="adjusted_total_score that counts as 'good'")
    p_mp.add_argument("--horizon", type=int, default=30, help="horizon in days for the score-well check")
    p_mp.set_defaults(func=cmd_learning_mention_precision)

    # ---- learning > version (item 7) ---------------------------------------
    p_ver = learn_sub.add_parser(
        "version",
        help="agent_call_log.model_version helpers (item 7)",
    )
    ver_sub = p_ver.add_subparsers(dest="version_command", required=True)
    p_ver_bf = ver_sub.add_parser("backfill", help="set model_version where NULL (never overwrites)")
    p_ver_bf.add_argument("--dry-run", action="store_true")
    p_ver_bf.set_defaults(func=cmd_learning_version_backfill)
    p_ver_st = ver_sub.add_parser("stats", help="per-(agent, model_version) call counts + success rate")
    p_ver_st.set_defaults(func=cmd_learning_version_stats)

    # ---- learning > quality (item 4) ---------------------------------------
    p_qual = learn_sub.add_parser(
        "quality",
        help="agent_call_log.quality_score backfill + summary (item 4)",
    )
    qual_sub = p_qual.add_subparsers(dest="quality_command", required=True)
    p_qual_bf = qual_sub.add_parser("backfill", help="heuristic-score NULL rows (conservative)")
    p_qual_bf.add_argument("--since", type=int, default=None, help="only score rows from the last N days")
    p_qual_bf.add_argument("--dry-run", action="store_true")
    p_qual_bf.set_defaults(func=cmd_learning_quality_backfill)
    p_qual_sum = qual_sub.add_parser("summary", help="avg quality per agent + per (agent, model_version)")
    p_qual_sum.set_defaults(func=cmd_learning_quality_summary)

    # ---- learning > recompute-trust (signal calibration loop) --------------
    p_trust = learn_sub.add_parser(
        "recompute-trust",
        help="recompute author + channel trust weights from closed-trade outcomes",
    )
    p_trust.add_argument("--link-window-days", type=int, default=30,
                         help="max signal age before a trade to count as in-scope")
    p_trust.add_argument("--neutral-band", type=float, default=0.5,
                         help="pnl_pct within ±band counts as no-move (no credit)")
    p_trust.add_argument("--alpha", type=float, default=0.5,
                         help="aggressiveness of weight update (0..1)")
    p_trust.add_argument("--min-signals", type=int, default=3,
                         help="minimum linkable signals required before updating a scope")
    p_trust.add_argument("--dry-run", action="store_true",
                         help="compute but don't persist updates")
    p_trust.set_defaults(func=cmd_learning_recompute_trust)

    # ---- learning > regime-accuracy (item 5) -------------------------------
    p_ra = learn_sub.add_parser(
        "regime-accuracy",
        help="monthly rollup of regime classifier verdicts (item 5)",
    )
    p_ra.add_argument("--lookback-months", type=int, default=12)
    p_ra.set_defaults(func=cmd_learning_regime_accuracy)

    # ---- learning > retrain-status (item 6) --------------------------------
    p_rt = learn_sub.add_parser(
        "retrain-status",
        help="should_retrain flag + reason per agent (item 6)",
    )
    p_rt.set_defaults(func=cmd_learning_retrain_status)

    # ---- journal ------------------------------------------------------------
    p_journal = sub.add_parser(
        "journal",
        help="closed-trade review: list pending, mark closed, submit review",
    )
    journal_sub = p_journal.add_subparsers(dest="journal_command", required=True)

    p_jp = journal_sub.add_parser("pending", help="list trades awaiting review")
    p_jp.set_defaults(func=cmd_journal_pending)

    p_jc = journal_sub.add_parser("close", help="flip a trade to closed_pending_review")
    p_jc.add_argument("trade_id")
    p_jc.add_argument("--exit-date", default=None)
    p_jc.add_argument("--exit-price", type=float, default=None)
    p_jc.add_argument("--pnl", type=float, default=None)
    p_jc.add_argument("--pnl-percent", type=float, default=None)
    p_jc.add_argument("--notes", default=None)
    p_jc.set_defaults(func=cmd_journal_close)

    p_jr = journal_sub.add_parser(
        "review",
        help="interactively answer the 7 questions and submit the review",
    )
    p_jr.add_argument("trade_id")
    p_jr.set_defaults(func=cmd_journal_review)

    # ---- rules --------------------------------------------------------------
    p_rules = sub.add_parser(
        "rules",
        help="trading-rule framework: gate-check a proposal against confluence/risk/portfolio caps",
    )
    rules_sub = p_rules.add_subparsers(dest="rules_command", required=True)

    p_rc = rules_sub.add_parser(
        "check",
        help="evaluate a TradeProposal against all v1 rules (advisory by default)",
    )
    p_rc.add_argument("--ticker", required=True)
    p_rc.add_argument("--side", choices=["long", "short"], required=True)
    p_rc.add_argument("--entry", type=float, required=True)
    p_rc.add_argument("--stop", type=float, required=True)
    p_rc.add_argument("--size", type=float, required=True, help="position size in units")
    p_rc.add_argument(
        "--equity",
        type=float,
        required=True,
        help="account equity used as the risk-percent denominator",
    )
    p_rc.add_argument(
        "--confluence",
        required=True,
        help="three comma-separated subscores: pattern,fib,indicator (e.g. 3,2,1)",
    )
    p_rc.add_argument("--tps", default="", help="comma-separated take-profit prices (optional)")
    p_rc.add_argument(
        "--setup",
        default=None,
        help="setup category (flag|pennant|channel|hs|cup|range|ema|breakout)",
    )
    p_rc.add_argument(
        "--mode",
        choices=["advisory", "enforce"],
        default="advisory",
    )
    p_rc.set_defaults(func=cmd_rules_check)

    p_rp = rules_sub.add_parser(
        "plan",
        help="capture the immutable entry-time trade plan (one per trade)",
    )
    p_rp.add_argument("trade_id")
    p_rp.add_argument("--entry", type=float, required=True)
    p_rp.add_argument("--stop", type=float, required=True)
    p_rp.add_argument("--size", type=float, required=True)
    p_rp.add_argument("--tps", default="", help="comma-separated take-profit prices")
    p_rp.add_argument("--equity", type=float, default=None, help="account equity (denominator for risk_pct)")
    p_rp.add_argument(
        "--category",
        choices=["flag", "pennant", "channel", "hs", "cup", "range", "ema", "breakout"],
        default=None,
    )
    p_rp.add_argument(
        "--confluence",
        default=None,
        help="3 comma-separated subscores: pattern,fib,indicator",
    )
    p_rp.add_argument(
        "--strategy",
        choices=["breakout_retest", "breakout_impulse", "dip_buy", "range_fade", "other"],
        default="breakout_retest",
    )
    p_rp.add_argument("--notes", default=None)
    p_rp.set_defaults(func=cmd_rules_plan)

    p_ra = rules_sub.add_parser(
        "adherence",
        help="recompute and print the rule-adherence breakdown for a trade",
    )
    p_ra.add_argument("trade_id")
    p_ra.set_defaults(func=cmd_rules_adherence)

    # ── insiders: free public-disclosure scrapers ─────────────────────────
    p_ins = sub.add_parser(
        "insiders",
        help="ingest free public disclosures (House PTRs, etc.) into the manual pipeline",
    )
    ins_sub = p_ins.add_subparsers(dest="insiders_command", required=True)

    p_ins_pull = ins_sub.add_parser("pull", help="pull one or all insider sources")
    p_ins_pull.add_argument(
        "--source",
        default="all",
        help="source slug: 'house' (Piece 1), 'all' to fan out across registered sources",
    )
    p_ins_pull.add_argument(
        "--since",
        default=None,
        help="ISO date YYYY-MM-DD — only ingest filings on/after this date",
    )
    p_ins_pull.add_argument(
        "--year",
        type=int,
        default=None,
        help="calendar year of the source index (defaults to current year)",
    )
    p_ins_pull.set_defaults(func=cmd_insiders_pull)

    p_ins_status = ins_sub.add_parser("status", help="print the insiders_cursor table")
    p_ins_status.set_defaults(func=cmd_insiders_status)

    # ── signals: structured signal extraction from documents ──────────────
    p_sig = sub.add_parser(
        "signals",
        help="extract / inspect structured positioning signals from ingested docs",
    )
    sig_sub = p_sig.add_subparsers(dest="signals_command", required=True)

    p_sig_ex = sig_sub.add_parser(
        "extract",
        help="run pending signal extraction over recent documents",
    )
    p_sig_ex.add_argument("--limit", type=int, default=100,
                          help="max docs to process this run")
    p_sig_ex.add_argument("--since-days", type=int, default=30,
                          help="only consider docs ingested within this window")
    p_sig_ex.add_argument("--extractor", default=None,
                          help="restrict to one extractor: insider_extractor | llm_extractor")
    p_sig_ex.add_argument("--dry-run", action="store_true",
                          help="don't persist signals or attempts (parse-only)")
    p_sig_ex.set_defaults(func=cmd_signals_extract)

    p_sig_show = sig_sub.add_parser(
        "show", help="print recent signals (or signals for a ticker)",
    )
    p_sig_show.add_argument("--ticker", default=None,
                            help="filter to one ticker (active signals only)")
    p_sig_show.add_argument("--limit", type=int, default=50)
    p_sig_show.add_argument("--since-days", type=int, default=90,
                            help="lookback when --ticker is set")
    p_sig_show.set_defaults(func=cmd_signals_show)

    p_sig_status = sig_sub.add_parser(
        "status", help="per-extractor counts of signals in the DB",
    )
    p_sig_status.set_defaults(func=cmd_signals_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
