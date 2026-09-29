from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS documents (
        document_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT,
        published_at TEXT NOT NULL,
        author TEXT,
        content_type TEXT NOT NULL,
        raw_text TEXT NOT NULL,
        cleaned_text TEXT NOT NULL,
        tags_json TEXT NOT NULL,
        ingested_at TEXT NOT NULL
    )
    """,
    # Dedup: two documents from the same source with the same URL are the
    # same story. NULL urls don't collide under SQLite's unique semantics,
    # so untitled/url-less sources still dedupe via their document_id PK.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_source_url
        ON documents (source_id, url)
        WHERE url IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS theses (
        thesis_id TEXT PRIMARY KEY,
        thesis TEXT NOT NULL,
        theme TEXT NOT NULL,
        horizon TEXT NOT NULL,
        direction TEXT NOT NULL,
        assets_json TEXT NOT NULL,
        catalysts_json TEXT NOT NULL,
        risks_json TEXT NOT NULL,
        implied_positioning_json TEXT NOT NULL,
        confidence REAL NOT NULL,
        freshness_score REAL NOT NULL,
        status TEXT NOT NULL,
        source_ids_json TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        extracted_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memos (
        memo_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        summary TEXT NOT NULL,
        consensus_views_json TEXT NOT NULL,
        divergent_views_json TEXT NOT NULL,
        suggested_positioning_json TEXT NOT NULL,
        risks_to_watch_json TEXT NOT NULL,
        thesis_ids_json TEXT NOT NULL
    )
    """,
    # ─── Trading framework §13 schema ──────────────────────────────────────
    # See docs/trading_framework.md and config/trading_framework.json.
    # These tables back the brain's scoring engine + manual trade journal.
    """
    CREATE TABLE IF NOT EXISTS assets (
        asset_id TEXT PRIMARY KEY,
        ticker TEXT NOT NULL,
        asset_name TEXT NOT NULL,
        asset_class TEXT NOT NULL,
        sector TEXT,
        theme TEXT,
        liquidity_profile TEXT,
        volatility_profile TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_ticker ON assets (ticker)
    """,
    """
    CREATE TABLE IF NOT EXISTS macro_regimes (
        regime_id TEXT PRIMARY KEY,
        classified_at TEXT NOT NULL,
        framework_regime TEXT NOT NULL,
        thesis_regime TEXT NOT NULL,
        liquidity_state TEXT,
        dollar_trend TEXT,
        rate_trend TEXT,
        volatility_state TEXT,
        breadth_state TEXT,
        confidence_score INTEGER NOT NULL,
        classifier_version TEXT,
        evidence_json TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_macro_regimes_classified_at
        ON macro_regimes (classified_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS technical_setups (
        setup_id TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        setup_type TEXT NOT NULL,
        market_structure TEXT NOT NULL,
        key_level REAL,
        entry_zone_low REAL,
        entry_zone_high REAL,
        invalidation_level REAL,
        target_zone_low REAL,
        target_zone_high REAL,
        risk_reward REAL,
        technical_score INTEGER NOT NULL,
        FOREIGN KEY (asset_id) REFERENCES assets (asset_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_technical_setups_asset
        ON technical_setups (asset_id, observed_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS volume_signals (
        volume_signal_id TEXT PRIMARY KEY,
        setup_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        relative_volume REAL,
        volume_pattern TEXT,
        volume_confirmation TEXT,
        volume_score INTEGER NOT NULL,
        FOREIGN KEY (setup_id) REFERENCES technical_setups (setup_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_scores (
        score_id TEXT PRIMARY KEY,
        setup_id TEXT NOT NULL,
        scored_at TEXT NOT NULL,
        regime_id TEXT,
        macro_alignment_score INTEGER NOT NULL,
        liquidity_score INTEGER NOT NULL,
        sector_theme_score INTEGER NOT NULL,
        technical_structure_score INTEGER NOT NULL,
        volume_flow_score INTEGER NOT NULL,
        risk_reward_score INTEGER NOT NULL,
        relative_strength_score INTEGER NOT NULL,
        psychology_score INTEGER NOT NULL,
        raw_total_score INTEGER NOT NULL,
        adjusted_total_score INTEGER NOT NULL,
        grade TEXT NOT NULL,
        position_size_tier TEXT NOT NULL,
        feature_vector_json TEXT,
        reasoning_trail_json TEXT,
        FOREIGN KEY (setup_id) REFERENCES technical_setups (setup_id),
        FOREIGN KEY (regime_id) REFERENCES macro_regimes (regime_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trade_scores_setup
        ON trade_scores (setup_id, scored_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
        trade_id TEXT PRIMARY KEY,
        setup_id TEXT,
        score_id TEXT,
        asset_id TEXT NOT NULL,
        entry_date TEXT NOT NULL,
        entry_price REAL NOT NULL,
        exit_date TEXT,
        exit_price REAL,
        position_size REAL NOT NULL,
        stop_loss REAL NOT NULL,
        target_price REAL,
        status TEXT NOT NULL,
        pnl REAL,
        pnl_percent REAL,
        execution_notes TEXT,
        was_it_thesis_at_close TEXT,
        lesson_at_close TEXT,
        hindsight_bias_check TEXT,
        FOREIGN KEY (setup_id) REFERENCES technical_setups (setup_id),
        FOREIGN KEY (score_id) REFERENCES trade_scores (score_id),
        FOREIGN KEY (asset_id) REFERENCES assets (asset_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trades_status_entry
        ON trades (status, entry_date DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS missed_trades (
        missed_trade_id TEXT PRIMARY KEY,
        setup_id TEXT NOT NULL,
        flagged_at TEXT NOT NULL,
        reason_missed TEXT NOT NULL,
        was_valid_in_real_time INTEGER NOT NULL,
        hindsight_bias_risk TEXT NOT NULL,
        lesson TEXT,
        rule_adjustment TEXT,
        FOREIGN KEY (setup_id) REFERENCES technical_setups (setup_id)
    )
    """,
    # ─── Inputs workstream: per-source per-trade attribution ──────────────
    """
    CREATE TABLE IF NOT EXISTS source_outcomes (
        outcome_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        trade_id TEXT NOT NULL,
        thesis_id TEXT,
        attribution_weight REAL NOT NULL,
        outcome_pnl REAL,
        outcome_pnl_percent REAL,
        contribution_type TEXT,
        recorded_at TEXT NOT NULL,
        FOREIGN KEY (trade_id) REFERENCES trades (trade_id),
        FOREIGN KEY (thesis_id) REFERENCES theses (thesis_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_source_outcomes_source
        ON source_outcomes (source_id, recorded_at DESC)
    """,
    # ─── Logging contract: every agent call logged for future fine-tune ───
    # See docs/logging_contract.md. This is the training corpus seed table.
    """
    CREATE TABLE IF NOT EXISTS agent_call_log (
        call_id TEXT PRIMARY KEY,
        agent_name TEXT NOT NULL,
        called_at TEXT NOT NULL,
        model_provider TEXT NOT NULL,
        model_name TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        input_payload_json TEXT NOT NULL,
        output_payload_json TEXT NOT NULL,
        context_json TEXT,
        latency_ms INTEGER,
        input_tokens INTEGER,
        output_tokens INTEGER,
        estimated_cost_usd REAL,
        success INTEGER NOT NULL,
        error_message TEXT,
        attributed_setup_id TEXT,
        attributed_trade_id TEXT,
        attributed_outcome_pnl REAL,
        call_type TEXT,
        quality_score REAL,
        model_version TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_call_log_agent_called
        ON agent_call_log (agent_name, called_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_call_log_attribution
        ON agent_call_log (attributed_trade_id)
        WHERE attributed_trade_id IS NOT NULL
    """,
    # ─── Decisions log: chat-driven architecture/scope decisions ──────────
    # Backs the mgmt panel; also future training pairs.
    """
    CREATE TABLE IF NOT EXISTS decisions (
        decision_id TEXT PRIMARY KEY,
        decided_at TEXT NOT NULL,
        topic TEXT NOT NULL,
        decision TEXT NOT NULL,
        rationale TEXT,
        alternatives_considered TEXT,
        chat_session_ref TEXT,
        affects_files TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_decisions_decided_at
        ON decisions (decided_at DESC)
    """,
    # ─── Live prices: daily OHLCV per ticker ──────────────────────────────
    # Populated by the prices/fetcher.py batch + the `prices fetch` CLI.
    # Keyed on ticker (string) rather than asset_id (FK) so we can fetch
    # before assets row exists; the scoring runner writes assets first
    # then trade_scores so this stays consistent.
    """
    CREATE TABLE IF NOT EXISTS prices (
        price_id TEXT PRIMARY KEY,
        ticker TEXT NOT NULL,
        observed_at TEXT NOT NULL,        -- ISO date (YYYY-MM-DD) for daily; ISO datetime for intraday
        timeframe TEXT NOT NULL DEFAULT '1D',
        open REAL,
        high REAL,
        low REAL,
        close REAL NOT NULL,
        volume INTEGER,
        provider TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    )
    """,
    # One bar per (ticker, observed_at, timeframe) — re-fetches replace via INSERT OR REPLACE
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_prices_ticker_observed
        ON prices (ticker, observed_at, timeframe)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_prices_ticker_observed_desc
        ON prices (ticker, observed_at DESC)
    """,
    # ─── Per-call accuracy backtest (learning/call_accuracy.py) ──────────
    # One row per scored trade-call: the call's ticker/direction/levels and
    # the resolved outcome vs real price action. Powers per-source accuracy
    # on the S6 streams cards. Idempotent on document_id.
    """
    CREATE TABLE IF NOT EXISTS call_outcomes (
        document_id   TEXT PRIMARY KEY,
        author_id     TEXT NOT NULL,
        raw_ticker    TEXT,
        ticker        TEXT,
        symbol        TEXT,
        direction     TEXT,
        timeframe     TEXT,
        entry_px      REAL,
        stop_px       REAL,
        target_px     REAL,
        horizon_days  INTEGER,
        fwd_return_pct REAL,
        resolved      TEXT,
        r_multiple    REAL,
        call_at       TEXT,
        scored_at     TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_call_outcomes_author
        ON call_outcomes (author_id)
    """,
    # ─── LLM-agent outputs (regime + narrative) ──────────────────────────
    # Persistent record of regime classifications and narrative snapshots.
    # Each row references the agent_call_log row that produced it via call_id
    # so we can reconstruct prompt/inputs for future fine-tuning.
    """
    CREATE TABLE IF NOT EXISTS regime_classifications (
        classification_id TEXT PRIMARY KEY,
        asof TEXT NOT NULL,
        label TEXT NOT NULL,
        confidence REAL,
        rationale TEXT,
        call_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_regime_classifications_asof
        ON regime_classifications (asof DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS narrative_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        asof TEXT NOT NULL,
        bullets_json TEXT NOT NULL,
        regime_label TEXT,
        call_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_narrative_snapshots_asof
        ON narrative_snapshots (asof DESC)
    """,
    # ─── Vision result cache (hash-dedupe) ────────────────────────────────
    # Same image bytes → same TradeRecord. Skips redundant Claude calls
    # when a chart is dropped twice or the drainer reruns over an already-
    # processed document. Keyed on sha256 of the original (pre-resize) bytes.
    """
    CREATE TABLE IF NOT EXISTS vision_cache (
        image_sha256 TEXT PRIMARY KEY,
        model TEXT NOT NULL,
        result_json TEXT NOT NULL,
        latency_ms REAL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_vision_cache_created
        ON vision_cache (created_at DESC)
    """,
    # ─── Manual input layer ───────────────────────────────────────────────
    # Per-author/channel attribution for chart screenshots and text drops
    # the user pastes into the /inbox route. See plans/manual-input-layer.
    """
    CREATE TABLE IF NOT EXISTS input_authors (
        author_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        channel TEXT,
        channel_type TEXT,
        notes TEXT,
        first_seen_at TEXT,
        last_seen_at TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_input_authors_last_seen
        ON input_authors (last_seen_at DESC)
    """,
    # Per-image rows for bulk chart drops. One trade idea = one `documents`
    # row + N attachments here. Per-image fields (timeframe, role) let
    # chart_vision adapt its prompt per chart, and let later analytics ask
    # things like "do trades with a `stop logic` chart attached outperform?"
    # Supersedes the flat `documents.attachment_paths_json` which stays
    # populated for back-compat with code reading it directly.
    """
    CREATE TABLE IF NOT EXISTS manual_chart_attachments (
        attachment_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        attachment_path TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        role TEXT,
        note TEXT,
        order_index INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY (document_id) REFERENCES documents (document_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_manual_chart_attachments_doc
        ON manual_chart_attachments (document_id, order_index)
    """,
    # ─── Trade review feedback loop ──────────────────────────────────────
    # One row per closed-trade review. Same 7-question framework every
    # time so structured fields (thesis_validity, execution_scores, etc)
    # accumulate into queryable signal that calibrates scorer + source
    # weights. Free-text `lesson` + `free_form_notes` capture what the
    # structure misses. JSON columns hold the multi-pick / multi-Likert
    # answers without table-explosion.
    """
    CREATE TABLE IF NOT EXISTS trade_reviews (
        review_id TEXT PRIMARY KEY,
        trade_id TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        thesis_validity TEXT,                 -- enum: fully_right | right_outcome_wrong_reason | right_thesis_wrong_outcome | fully_wrong
        sources_credited_json TEXT,           -- list of source_id (JSON array)
        execution_scores_json TEXT,           -- {entry, stop, sizing, exit} each 1-5
        setup_score_hindsight TEXT,           -- enum: over | right | under
        surprise_factor_json TEXT,            -- list of enum: macro | sector | liquidity | idiosyncratic | none
        surprise_note TEXT,
        lesson TEXT,                          -- one-line, capped client-side at ~200 chars
        would_retake TEXT,                    -- enum: yes | no | modified
        free_form_notes TEXT,
        FOREIGN KEY (trade_id) REFERENCES trades (trade_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trade_reviews_trade
        ON trade_reviews (trade_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trade_reviews_completed
        ON trade_reviews (completed_at DESC)
    """,
    # ─── Score hindsight overlay ─────────────────────────────────────────
    # Per-trade-review Q4 (setup_score_hindsight: over | right | under)
    # written by journal-feedback-loop's feedback_writer when a review
    # lands. Consumed by learning/score_outcome_correlation to surface
    # systematic scorer over/under-confidence patterns. One row per
    # review; FK to both trade_reviews and trades + score_id for the
    # composite "which score was being judged" lookup.
    """
    CREATE TABLE IF NOT EXISTS score_hindsight_overlay (
        overlay_id TEXT PRIMARY KEY,
        review_id TEXT NOT NULL,
        trade_id TEXT NOT NULL,
        score_id TEXT,
        hindsight_verdict TEXT NOT NULL,     -- enum: over | right | under
        recorded_at TEXT NOT NULL,
        FOREIGN KEY (review_id) REFERENCES trade_reviews (review_id),
        FOREIGN KEY (trade_id) REFERENCES trades (trade_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_score_hindsight_trade
        ON score_hindsight_overlay (trade_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_score_hindsight_verdict
        ON score_hindsight_overlay (hindsight_verdict, recorded_at DESC)
    """,
    # ─── Funnel spine: concepts → plans → trades ─────────────────────────
    # Concepts are marked watchlist items the operator wants to track as
    # potential setups. They survive past a single session and keep their
    # score snapshot, thesis note, and lineage to the trade_plan they
    # eventually promote into. Suggestions surfaced by the system land
    # here with suggested_by_system=1 until the operator marks them.
    """
    CREATE TABLE IF NOT EXISTS trade_concepts (
        concept_id TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL,
        source TEXT NOT NULL,                 -- watchlist_auto | watchlist_manual | inbox | other
        status TEXT NOT NULL,                 -- active | promoted | retired
        suggested_by_system INTEGER NOT NULL DEFAULT 0,
        suggestion_reason TEXT,
        score_at_mark REAL,
        tier_at_mark TEXT,
        side_at_mark TEXT,
        thesis_text TEXT,
        trade_plan_id TEXT,
        marked_at TEXT NOT NULL,
        promoted_at TEXT,
        retired_at TEXT,
        retire_reason TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (asset_id) REFERENCES assets (asset_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trade_concepts_status
        ON trade_concepts (status, marked_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trade_concepts_asset_active
        ON trade_concepts (asset_id, status)
    """,
    # A reviewed suggestion the desk passed on. The system keeps
    # proposing names from the watchlist; once the operator has looked at
    # one and said no, it stays out of the suggestion list until the case
    # for it actually changes — the score climbs past a threshold, the
    # side flips, or enough time passes that the old read is stale. The
    # snapshot columns are what "changed" is measured against, and
    # review_count raises the bar each time the same name is passed on.
    """
    CREATE TABLE IF NOT EXISTS concept_suggestion_reviews (
        asset_id TEXT PRIMARY KEY,
        verdict TEXT NOT NULL,                -- passed
        score_at_review REAL,
        side_at_review TEXT,
        note TEXT,
        review_count INTEGER NOT NULL DEFAULT 1,
        reviewed_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # Trade plans = step ③ Identify. Each plan captures the entry/stop/
    # targets/sizing the operator intends to act on, optionally linked
    # back to the concept it grew from. Status flips draft→live when
    # the operator activates it (creates a trades row); cancelled when
    # invalidated before entry; closed mirrors the trades.status close.
    # Rules-framework v1 columns (planned_*) capture the immutable
    # intent at the moment of submission for adherence scoring later.
    """
    CREATE TABLE IF NOT EXISTS trade_plans (
        plan_id TEXT PRIMARY KEY,
        -- Funnel spine linkage
        concept_id TEXT,
        asset_id TEXT,
        side TEXT,
        entry REAL,
        stop REAL,
        targets_json TEXT,                    -- JSON array of {price, weight}
        size_usd REAL,
        size_r REAL,
        time_horizon TEXT,                    -- intraday | swing | position
        thesis TEXT,
        invalidation TEXT,
        gate_status TEXT,                     -- pass | warn | block | unchecked
        gate_evaluation_json TEXT,
        status TEXT,                          -- draft | live | cancelled | closed
        trade_id TEXT UNIQUE,                 -- links to the actual trade row
        created_at TEXT NOT NULL,
        updated_at TEXT,
        activated_at TEXT,
        cancelled_at TEXT,
        -- Rules framework v1: immutable entry-time plan columns
        planned_entry REAL,
        planned_stop REAL,
        planned_tps_json TEXT,                -- JSON array of TP prices
        planned_size REAL,
        planned_account_equity REAL,          -- denominator at plan time (audit trail)
        planned_risk_pct REAL,               -- precomputed: |entry-stop| * size / equity
        planned_setup_category TEXT,          -- flag | pennant | channel | hs | cup | range | ema | breakout
        planned_confluence_score INTEGER,     -- 0..8 total
        planned_pattern_subscore INTEGER,     -- 0..3
        planned_fib_subscore INTEGER,         -- 0..3
        planned_indicator_subscore INTEGER,   -- 0..2
        planned_correlated_bucket TEXT,       -- derived from ticker via config/correlation_buckets.json
        planned_entry_strategy TEXT,          -- breakout_retest | breakout_impulse | dip_buy | range_fade | other
        notes TEXT,
        FOREIGN KEY (concept_id) REFERENCES trade_concepts (concept_id),
        FOREIGN KEY (asset_id) REFERENCES assets (asset_id),
        FOREIGN KEY (trade_id) REFERENCES trades (trade_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trade_plans_status
        ON trade_plans (status, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trade_plans_concept
        ON trade_plans (concept_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trade_plans_trade
        ON trade_plans (trade_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trade_plans_created
        ON trade_plans (created_at DESC)
    """,
    # ─── Trading Rule Framework v1: portfolio exposure time-series ─────
    # Optional in v1 — no background writer yet. Reserved for the v2
    # `portfolio cap compliance` dashboard metric, which needs to know
    # whether exposure was within caps AT THE TIME each trade opened,
    # not just at review time. Populated on-demand or via a future cron.
    """
    CREATE TABLE IF NOT EXISTS portfolio_exposure_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        taken_at TEXT NOT NULL,
        account_equity REAL NOT NULL,
        concurrent_trades INTEGER NOT NULL,
        pct_deployed REAL NOT NULL,
        bucket_exposures_json TEXT NOT NULL,    -- {bucket_id: {trade_count, pct_of_equity, tickers}}
        any_cap_breached INTEGER                -- 0/1; cheap flag for dashboard queries
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_portfolio_snap_taken
        ON portfolio_exposure_snapshots (taken_at DESC)
    """,
    # ─── FRED historical observations ─────────────────────────────────────
    # Append-only time series store for every catalogued FRED series.
    # PK includes realtime_end so revision vintages don't collide; the
    # default-vintage path uses '9999-12-31' so re-fetches stay idempotent.
    """
    CREATE TABLE IF NOT EXISTS fred_observations (
        series_id        TEXT NOT NULL,
        observation_date TEXT NOT NULL,
        value            REAL NOT NULL,
        realtime_start   TEXT,
        realtime_end     TEXT NOT NULL DEFAULT '9999-12-31',
        fetched_at       TEXT NOT NULL,
        PRIMARY KEY (series_id, observation_date, realtime_end)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fred_obs_series_date
        ON fred_observations (series_id, observation_date DESC)
    """,
    # Cursor table for the insiders scraper package. One row per source_slug
    # tracks the last filing/award ingested so repeat runs are incremental.
    """
    CREATE TABLE IF NOT EXISTS insiders_cursor (
        source_slug      TEXT PRIMARY KEY,
        last_external_id TEXT,
        last_run_at      TEXT NOT NULL,
        last_run_status  TEXT
    )
    """,
    # Typed edges produced by the LDA lobbying scraper. The /05 influence
    # SPA tab reads from this table directly; node identity is namespaced
    # by edge endpoint (client:, registrant:, lobbyist:, agency:, issue:,
    # member:) so the graph stays queryable without a nodes table.
    """
    CREATE TABLE IF NOT EXISTS lobbying_edges (
        edge_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        filing_id  TEXT NOT NULL,
        period     TEXT NOT NULL,
        from_node  TEXT NOT NULL,
        to_node    TEXT NOT NULL,
        edge_kind  TEXT NOT NULL,
        amount_usd REAL,
        raw_json   TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_lobbying_edges_period
        ON lobbying_edges(period)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_lobbying_edges_from
        ON lobbying_edges(from_node)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_lobbying_edges_to
        ON lobbying_edges(to_node)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_lobbying_edges_dedupe
        ON lobbying_edges(filing_id, from_node, to_node, edge_kind)
    """,
    # ─── Signal extraction layer ─────────────────────────────────────────
    # One row per directional signal extracted from a document. A House
    # PTR with three tickers produces three signals. A manual analyst
    # note discussing rotation from QQQ → XLE produces two signals
    # (EXIT QQQ, LONG XLE). Both rule-based (insider_extractor) and
    # LLM-based (llm_extractor) extractors write into this table.
    #
    # Composer reads `signals` and aggregates per asset_ticker to derive
    # the directional bias + conviction that feeds the scoring pass.
    # This replaces today's "mention count → watchlist promotion" with
    # "signal-weighted positioning bias."
    """
    CREATE TABLE IF NOT EXISTS signals (
        signal_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        extracted_at TEXT NOT NULL,
        extraction_run_id TEXT,

        -- Asset / instrument
        asset_ticker TEXT NOT NULL,
        asset_class TEXT,
        secondary_tickers_json TEXT,
        instrument_detail_json TEXT,

        -- Direction / sizing intent
        side TEXT NOT NULL,                   -- LONG|SHORT|HEDGE|EXIT|TRIM|ADD|WATCH|AVOID
        conviction REAL NOT NULL,             -- 0..5 normalized
        conviction_raw TEXT,                  -- source-native form before normalization
        position_size_hint REAL,
        position_size_unit TEXT,              -- pct_equity|usd|shares|r
        horizon TEXT,                         -- intraday|swing|position|strategic
        horizon_days INTEGER,

        -- Levels
        entry_zone_low REAL,
        entry_zone_high REAL,
        stop_loss REAL,
        target_1 REAL,
        target_2 REAL,
        invalidation TEXT,

        -- Thesis context
        thesis_summary TEXT,
        thesis_tags_json TEXT,
        macro_regime_tags_json TEXT,
        catalyst_type TEXT,                   -- earnings|macro_print|political|technical|flow|other
        catalyst_date TEXT,
        catalyst_summary TEXT,

        -- Provenance
        source_slug TEXT NOT NULL,
        source_channel TEXT,
        author_id TEXT,
        author_trust_weight REAL,
        source_trust_weight REAL,

        -- Extractor metadata
        extractor_name TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        extractor_confidence REAL,
        model_provider TEXT,
        model_name TEXT,
        raw_excerpt TEXT,
        extraction_call_id TEXT,

        -- Lifecycle
        status TEXT NOT NULL DEFAULT 'active', -- active|superseded|expired|invalidated
        expires_at TEXT,
        superseded_by TEXT,

        -- Composite weighting (snapshotted at extract time)
        weighted_score REAL,

        -- Audit
        latency_ms REAL,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cost_usd REAL,
        error_message TEXT,

        FOREIGN KEY (document_id) REFERENCES documents (document_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_signals_document
        ON signals (document_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_signals_ticker_active
        ON signals (asset_ticker, status, extracted_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_signals_source
        ON signals (source_slug, extracted_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_signals_run
        ON signals (extraction_run_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_signals_expires
        ON signals (status, expires_at)
        WHERE expires_at IS NOT NULL
    """,
    # Per-document extraction attempt log. Lets the runner answer
    # "which docs still need extraction?" without re-running successful
    # extractions or pounding the LLM on docs that erroneously produce
    # zero signals. Also gives us a forensic trail when an extractor
    # changes behaviour between versions.
    """
    CREATE TABLE IF NOT EXISTS signal_extraction_attempts (
        attempt_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        attempted_at TEXT NOT NULL,
        extractor_name TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        status TEXT NOT NULL,                 -- success|no_signal|error|skipped
        error_message TEXT,
        signals_produced INTEGER NOT NULL DEFAULT 0,
        latency_ms REAL,
        FOREIGN KEY (document_id) REFERENCES documents (document_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_signal_attempts_doc
        ON signal_extraction_attempts (document_id, attempted_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_signal_attempts_status
        ON signal_extraction_attempts (status, attempted_at DESC)
    """,
    # ─── Learning loop: source / channel trust weights ────────────────────
    # Per-channel trust weight maintained by the calibration loop. Replaces
    # the hard-coded `_CHANNEL_TRUST` dict in insider_extractor.py: the
    # extractor reads from this table first, falls back to the hard-coded
    # default if the row is missing. Snapshotted onto each Signal at
    # extract time so old signals stay reproducible.
    """
    CREATE TABLE IF NOT EXISTS source_trust_weights (
        source_channel  TEXT PRIMARY KEY,
        trust_weight    REAL NOT NULL,
        n_signals       INTEGER NOT NULL DEFAULT 0,
        n_trades_linked INTEGER NOT NULL DEFAULT 0,
        n_hits          INTEGER NOT NULL DEFAULT 0,
        precision       REAL,                  -- hits / trades_linked
        avg_pnl_pct     REAL,
        last_updated_at TEXT NOT NULL,
        baseline_weight REAL NOT NULL DEFAULT 1.0
    )
    """,
    # One row per calibration pass — both per-author and per-channel.
    # Append-only audit log so we can debug "why did this author's
    # trust_weight jump from 1.2 to 1.6 last Tuesday?".
    """
    CREATE TABLE IF NOT EXISTS signal_calibration_history (
        history_id      TEXT PRIMARY KEY,
        run_id          TEXT NOT NULL,
        recorded_at     TEXT NOT NULL,
        scope_kind      TEXT NOT NULL,         -- 'author' | 'channel'
        scope_key       TEXT NOT NULL,         -- author_id or source_channel
        trust_weight_before REAL,
        trust_weight_after  REAL NOT NULL,
        n_signals       INTEGER NOT NULL,
        n_trades_linked INTEGER NOT NULL,
        n_hits          INTEGER NOT NULL,
        precision       REAL,
        avg_pnl_pct     REAL,
        notes           TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_calibration_history_run
        ON signal_calibration_history (run_id, recorded_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_calibration_history_scope
        ON signal_calibration_history (scope_kind, scope_key, recorded_at DESC)
    """,
    # ─── Alerts ───────────────────────────────────────────────────────
    # One row per fired alert. The tracker computes a "buy now" state
    # twice-plus daily; without this table that state was only visible to
    # someone who happened to open the SPA (see: the Aug-2026 ETH/BTC
    # breakout, flagged A/tier_1 on the 17th, seen by nobody).
    #
    # The row is the durable record; delivery is best-effort on top of it.
    # `delivered_json` maps channel → status, so a send that failed (or a
    # channel that wasn't configured yet) can be retried on the next cycle
    # without re-deriving the alert.
    """
    CREATE TABLE IF NOT EXISTS alerts (
        alert_id        TEXT PRIMARY KEY,
        fired_at        TEXT NOT NULL,
        rule            TEXT NOT NULL,       -- 'grade_cross' | 'score_jump'
        severity        TEXT NOT NULL,       -- 'high' | 'medium'
        ticker          TEXT NOT NULL,
        title           TEXT NOT NULL,
        body            TEXT NOT NULL,
        score_before    INTEGER,
        score_after     INTEGER,
        grade_before    TEXT,
        grade_after     TEXT,
        tier_after      TEXT,
        side            TEXT,                -- 'LONG' | 'SHORT' | NULL
        score_id        TEXT,                -- trade_scores row that triggered it
        payload_json    TEXT,
        delivered_json  TEXT,                -- {"telegram": "ok" | "error: ..."}
        acked_at        TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_alerts_fired
        ON alerts (fired_at DESC)
    """,
    # Dedupe/cooldown lookup: "has (ticker, rule) fired in the last N hours?"
    """
    CREATE INDEX IF NOT EXISTS idx_alerts_dedupe
        ON alerts (ticker, rule, fired_at DESC)
    """,
    # ─── Paper trading book ───────────────────────────────────────────
    # A simulated account that executes the signal stack's own reads, so
    # "would this desk have made money" stops being an argument and
    # becomes a balance. Deliberately its OWN namespace: `trades` /
    # `trade_plans` are the hand-traded funnel and must not be polluted
    # with machine fills.
    #
    # The mandate (starting equity, sizing band, cash floor, caps) is
    # snapshotted onto the portfolio row as `mandate_json` at creation.
    # Editing config/paper_trading.json afterwards does NOT retroactively
    # rewrite a running book's rules — a P&L curve produced under one
    # mandate stays interpretable.
    """
    CREATE TABLE IF NOT EXISTS paper_portfolios (
        portfolio_id     TEXT PRIMARY KEY,
        name             TEXT NOT NULL,
        base_currency    TEXT NOT NULL DEFAULT 'USD',
        starting_equity  REAL NOT NULL,
        cash             REAL NOT NULL,
        status           TEXT NOT NULL DEFAULT 'active',  -- active | paused | closed
        mandate_json     TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        updated_at       TEXT,
        last_tick_at     TEXT,
        notes            TEXT
    )
    """,
    # One row per lot-less aggregated position. Adds average into
    # `avg_price`; trims realize against it and bank into
    # `realized_pnl`. `status='open'` rows are the book.
    """
    CREATE TABLE IF NOT EXISTS paper_positions (
        position_id       TEXT PRIMARY KEY,
        portfolio_id      TEXT NOT NULL,
        ticker            TEXT NOT NULL,
        side              TEXT NOT NULL,          -- LONG | SHORT
        qty               REAL NOT NULL,
        avg_price         REAL NOT NULL,
        opened_at         TEXT NOT NULL,
        closed_at         TEXT,
        status            TEXT NOT NULL,          -- open | closed
        stop              REAL,
        target            REAL,
        -- Conviction + provenance snapshotted at entry, then refreshed on
        -- every tick so the UI can show "bought at 0.71, reads 0.34 now".
        rank_at_entry       REAL,                  -- 0-100 percentile at entry
        rank_now            REAL,                  -- refreshed every tick
        target_weight_pct   REAL,
        thesis            TEXT,
        bucket_id         TEXT,
        source_json       TEXT,                   -- {score_id, grade, signal_agg summary}
        realized_pnl      REAL NOT NULL DEFAULT 0,
        fees_paid         REAL NOT NULL DEFAULT 0,
        high_water_price  REAL,                   -- best mark seen, for the giveback trail
        last_mark         REAL,
        last_mark_at      TEXT,
        partial_taken     INTEGER NOT NULL DEFAULT 0,  -- 1 once the target trim has fired
        FOREIGN KEY (portfolio_id) REFERENCES paper_portfolios (portfolio_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_paper_positions_open
        ON paper_positions (portfolio_id, status, ticker)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_paper_positions_closed
        ON paper_positions (portfolio_id, closed_at DESC)
    """,
    # Immutable fill log. Every cash movement in the book is one row here;
    # `cash` on the portfolio is the running total of these plus the
    # opening deposit, and the reconciler asserts that.
    """
    CREATE TABLE IF NOT EXISTS paper_orders (
        order_id      TEXT PRIMARY KEY,
        portfolio_id  TEXT NOT NULL,
        position_id   TEXT,
        decision_id   TEXT,
        tick_id       TEXT,
        ticker        TEXT NOT NULL,
        action        TEXT NOT NULL,        -- OPEN | ADD | TRIM | EXIT
        side          TEXT NOT NULL,        -- LONG | SHORT
        qty           REAL NOT NULL,
        price         REAL NOT NULL,        -- fill price, slippage applied
        ref_price     REAL,                 -- the mark before slippage
        notional      REAL NOT NULL,
        cash_delta    REAL NOT NULL,        -- signed effect on portfolio cash
        realized_pnl  REAL,                 -- populated on TRIM/EXIT
        fees          REAL NOT NULL DEFAULT 0,
        intent        TEXT NOT NULL,        -- the Intent vocabulary term
        rationale     TEXT,                 -- one human sentence
        price_source  TEXT,                 -- finnhub | yfinance-5m | db-stale
        filled_at     TEXT NOT NULL,
        FOREIGN KEY (portfolio_id) REFERENCES paper_portfolios (portfolio_id),
        FOREIGN KEY (position_id) REFERENCES paper_positions (position_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_paper_orders_filled
        ON paper_orders (portfolio_id, filled_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_paper_orders_position
        ON paper_orders (position_id, filled_at)
    """,
    # The semantic record: what the engine decided and *why*, including
    # the decisions it declined to execute. A REJECT row with a blocker
    # of `cash_floor` is the answer to "why didn't it buy the 92?" —
    # without it the book looks arbitrary.
    """
    CREATE TABLE IF NOT EXISTS paper_decisions (
        decision_id       TEXT PRIMARY KEY,
        portfolio_id      TEXT NOT NULL,
        tick_id           TEXT NOT NULL,
        decided_at        TEXT NOT NULL,
        ticker            TEXT NOT NULL,
        action            TEXT NOT NULL,      -- OPEN | ADD | TRIM | EXIT | HOLD | REJECT
        intent            TEXT NOT NULL,      -- why, from the Intent vocabulary
        side              TEXT,
        rank              REAL,                -- 0-100 percentile
        rank_prev         REAL,
        target_weight_pct REAL,
        current_weight_pct REAL,
        notional          REAL,
        executed          INTEGER NOT NULL DEFAULT 0,
        blocker           TEXT,               -- Blocker vocabulary term when executed=0
        headline          TEXT NOT NULL,      -- the sentence a human reads
        rationale_json    TEXT,               -- rank components + constraint state
        position_id       TEXT,
        order_id          TEXT,
        FOREIGN KEY (portfolio_id) REFERENCES paper_portfolios (portfolio_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_paper_decisions_tick
        ON paper_decisions (portfolio_id, decided_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_paper_decisions_ticker
        ON paper_decisions (portfolio_id, ticker, decided_at DESC)
    """,
    # Mark-to-market time series — one row per tick. This is what the
    # equity curve renders from; it is also the only record of what the
    # book looked like at a point in time, since positions mutate.
    """
    CREATE TABLE IF NOT EXISTS paper_equity_snapshots (
        snapshot_id    TEXT PRIMARY KEY,
        portfolio_id   TEXT NOT NULL,
        tick_id        TEXT,
        taken_at       TEXT NOT NULL,
        equity         REAL NOT NULL,
        cash           REAL NOT NULL,
        deployed       REAL NOT NULL,
        deployed_pct   REAL NOT NULL,
        cash_pct       REAL NOT NULL,
        open_positions INTEGER NOT NULL,
        unrealized_pnl REAL NOT NULL,
        realized_pnl_to_date REAL NOT NULL,
        positions_json TEXT,
        FOREIGN KEY (portfolio_id) REFERENCES paper_portfolios (portfolio_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_paper_equity_taken
        ON paper_equity_snapshots (portfolio_id, taken_at DESC)
    """,
    # One row per OPEN position per tick: every input the engine read on it
    # at that moment, next to where price and P&L were. This is the
    # trajectory the learning loop needs to answer "which input moved
    # first, and was it right" — a question the decision log cannot
    # answer because HOLD rows only record that nothing happened.
    """
    CREATE TABLE IF NOT EXISTS paper_position_snapshots (
        snapshot_id     TEXT PRIMARY KEY,
        portfolio_id    TEXT NOT NULL,
        position_id     TEXT NOT NULL,
        tick_id         TEXT,
        taken_at        TEXT NOT NULL,
        ticker          TEXT NOT NULL,
        side            TEXT NOT NULL,
        days_held       REAL,
        mark            REAL,
        unrealized_r    REAL,               -- open P&L in initial-risk units
        unrealized_pct  REAL,
        weight_pct      REAL,
        rank            REAL,
        score           INTEGER,
        read_side       TEXT,               -- LONG | SHORT | WATCH | AVOID this tick
        signal_direction TEXT,              -- blend bias_direction
        signal_confidence REAL,
        signal_n        INTEGER,
        support         REAL,               -- composed-view total
        support_structure REAL,             -- the four views' deltas
        support_voices  REAL,
        support_regime  REAL,
        support_price   REAL,
        regime_label    TEXT,
        macro_alignment INTEGER,
        exit_signal     TEXT,               -- thesis exit being watched, if any
        exit_signal_streak INTEGER,
        FOREIGN KEY (portfolio_id) REFERENCES paper_portfolios (portfolio_id),
        FOREIGN KEY (position_id) REFERENCES paper_positions (position_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_paper_pos_snap_position
        ON paper_position_snapshots (position_id, taken_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_paper_pos_snap_taken
        ON paper_position_snapshots (portfolio_id, taken_at DESC)
    """,
    # ─── Stock Unlocked call tracker ──────────────────────────────────
    # One row per actionable call from the Stock Unlocked Trades channel,
    # keyed by the document that posted it. The channel states entry,
    # targets and stop in words, so each call can be resolved against
    # the tape (which level printed FIRST) and the desk's own lifecycle
    # posts ("Target 2 HIT", "stopped out") are kept alongside so the
    # two accounts can be compared. `verdict` is the tape's word;
    # `desk_verdict` is the channel's. They disagree sometimes (KTOS
    # gapped through its stop and then ran to every target) and that
    # disagreement is data, not an error to reconcile away.
    #
    # Scoring is incremental: a call is re-walked while it is still
    # `open` and inside its horizon, then frozen. Nothing here is
    # derived from `signals` — the tracker reads the posts directly, so
    # it survives a signal re-extract.
    """
    CREATE TABLE IF NOT EXISTS stock_unlocked_calls (
        call_id            TEXT PRIMARY KEY,   -- documents.document_id of the entry post
        posted_at          TEXT NOT NULL,
        ticker             TEXT NOT NULL,
        instrument         TEXT NOT NULL,      -- stock | crypto | option
        trade_kind         TEXT NOT NULL,      -- day | swing
        direction          TEXT NOT NULL,      -- long | short
        entry              REAL,
        stop               REAL,
        targets_json       TEXT NOT NULL DEFAULT '[]',
        notes              TEXT,
        option_json        TEXT,               -- strike / expiry / premium for options
        market_price       REAL,               -- quoted in the post
        verdict            TEXT NOT NULL DEFAULT 'unscored',
                           -- unscored | open | win | loss | loss_ambiguous
                           -- | unresolved | unpriceable | no_levels
        max_target         INTEGER NOT NULL DEFAULT 0,
        planned_r          REAL,
        realized_r         REAL,
        mfe_pct            REAL,
        mae_pct            REAL,
        hours_to_resolve   REAL,
        resolved_at        TEXT,
        resolution         TEXT,               -- 1h | 5m | gap_open | ambiguous
        price_symbol       TEXT,
        price_source       TEXT,
        last_price         REAL,               -- last bar seen while open
        desk_events_json   TEXT NOT NULL DEFAULT '[]',
        desk_verdict       TEXT,               -- win | loss | open (the channel's word)
        desk_max_target    INTEGER NOT NULL DEFAULT 0,
        first_scored_at    TEXT,
        last_scored_at     TEXT,
        score_error        TEXT,
        author             TEXT,               -- the alerter, where the post names one
        trigger            TEXT,               -- above | below: a breakout alert armed at entry
        fmt                TEXT                -- v1 (2025 bot template) | v2 (current)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_su_calls_posted
        ON stock_unlocked_calls (posted_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_su_calls_verdict
        ON stock_unlocked_calls (verdict, posted_at DESC)
    """,
]


# Columns added to existing tables after their original CREATE TABLE shipped.
# Each entry is (table, column, type) — applied via idempotent ALTER TABLE
# in initialize_database() so existing DB files get the new columns without
# drop/recreate. New DBs get them from the CREATE TABLE statements above
# AND from the ALTER pass (which is a no-op on those because they exist).
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("stock_unlocked_calls", "author", "TEXT"),
    ("stock_unlocked_calls", "trigger", "TEXT"),
    ("stock_unlocked_calls", "fmt", "TEXT"),
    ("agent_call_log", "call_type", "TEXT"),
    ("agent_call_log", "quality_score", "REAL"),
    ("agent_call_log", "model_version", "TEXT"),
    ("documents", "author_id", "TEXT"),
    ("documents", "user_metadata_json", "TEXT"),
    ("documents", "attachment_path", "TEXT"),
    ("documents", "extracted_features_json", "TEXT"),
    # JSON array of all attachment paths for multi-image drops. The
    # singular `attachment_path` column above stays populated with the
    # first image for back-compat with anything reading it directly.
    ("documents", "attachment_paths_json", "TEXT"),
    # Hierarchy: a channel ("Market Traders") may be nested under a parent
    # community ("Feather Hands"). Nullable; rendered as a breadcrumb in
    # the SPA so attribution reads "Big_Nuts · Market Traders · Feather Hands".
    ("input_authors", "parent_channel", "TEXT"),
    # Trust multiplier the scoring layer applies to mentions / signals
    # from this author. 1.0 = baseline; >1 means "trust more, weight
    # heavier"; <1 means "trust less". Per user: Feather Hands family
    # (Big_Nuts, MadDog31, joejoe55) and Stock Unlocked = 1.5; Forward
    # Guidance = 1.5 for macro sentiment; defaults 1.0 for everyone else.
    ("input_authors", "trust_weight", "REAL"),
    # Notes the user wants to remember about this author (high-vol
    # short-term scalper, macro/swing only, prone to revenge trades, etc.)
    # Free-form, surfaced in the SPA tooltip.
    ("input_authors", "category", "TEXT"),  # "direct_trades" | "macro_sentiment" | "both"
    # Trade-close review status. Drives the /journal pending-reviews
    # queue: "closed_pending_review" rows pop up the framework
    # questionnaire; "closed_reviewed" rows are done. NULL = legacy /
    # not-yet-touched trades; keep them invisible to the queue.
    ("trades", "review_status", "TEXT"),
    # Funnel spine lineage: each closed/open trade links back to the
    # plan it was activated from (and through the plan, the concept).
    # Nullable so legacy trades without a plan still validate.
    ("trades", "plan_id", "TEXT"),
    # ─── Trading Rule Framework v1: per-trade rule fields ─────────────
    ("trades", "setup_category", "TEXT"),
    ("trades", "confluence_score", "INTEGER"),         # 0..8 composite
    ("trades", "pattern_subscore", "INTEGER"),         # 0..3
    ("trades", "fib_subscore", "INTEGER"),             # 0..3
    ("trades", "indicator_subscore", "INTEGER"),       # 0..2
    ("trades", "account_risk_pct", "REAL"),
    ("trades", "correlated_bucket", "TEXT"),
    ("trades", "entry_followed_retest", "INTEGER"),
    ("trades", "rule_adherence_score", "INTEGER"),
    # ─── Signal-layer integration into trade_scores ──────────────────
    # Set by scoring/runner.py after compose() — captures the
    # signal-aggregation read for each scored ticker. Composer itself
    # stays untouched; this column is an augmentation surface so
    # learning loop and dashboards can see what the signals layer
    # contributed without re-running aggregation.
    ("trade_scores", "signal_alignment_score", "INTEGER"),
    ("trade_scores", "signal_aggregate_json", "TEXT"),
    # Provenance of the scoring pass that produced this row:
    #   'scheduled' — full snapshot from the launchd free-ingest job
    #   'scheduled_delta' — hourly alert-watch pass, changed tickers only
    #   'manual'    — a hand-run pass (CLI, notebook, a worker chat)
    #   'whatif'    — run with framework_regime_hint, i.e. a backtest
    # The alerts evaluator compares 'scheduled' rows only. Mixing the
    # kinds is what made 2026-08-21 look like ETH round-tripping A→D→A
    # inside 90 minutes: those low passes were hand-run under
    # transitional_chop (−8 modifier) while the scheduled ones ran under
    # monetary_debasement_hard_asset (+6). Legacy rows are NULL and are
    # treated as untrusted for alerting.
    ("trade_scores", "pass_kind", "TEXT"),
    # Trade direction carried onto the alert so the notification can say
    # LONG/SHORT without re-reading the score row at delivery time.
    ("alerts", "side", "TEXT"),
    # Fingerprint of the scoring code that produced the row. Comparing a
    # score against one computed by different logic measures the change,
    # not the market — see scoring/logic_version.py.
    ("trade_scores", "logic_version", "TEXT"),
    # The |entry - stop| distance at the moment a paper position opened.
    # "R" has to mean INITIAL risk: the target trim moves the stop to
    # breakeven, and computing R off the live stop then divides by zero
    # and silently disarms the trailing exit for the rest of the trade.
    ("paper_positions", "initial_risk", "REAL"),
    # Thesis-exit confirmation. A rank-decay / support-collapse / side-flip
    # signal has to persist for N consecutive ticks before it closes a
    # multi-week position; these track the streak in progress.
    ("paper_positions", "exit_signal_intent", "TEXT"),
    ("paper_positions", "exit_signal_streak", "INTEGER"),
    ("paper_position_snapshots", "read_side", "TEXT"),
    # Per-position hold horizon, derived at fill (paper/horizon.py). The
    # mandate holds only bounds and a fallback.
    ("paper_positions", "expected_hold_days", "REAL"),
    ("paper_positions", "min_hold_days", "REAL"),
    ("paper_positions", "confirm_ticks", "INTEGER"),
    ("paper_position_snapshots", "expected_hold_days", "REAL"),
    # Exit path (target vs ladder), assigned at fill from rank + support.
    ("paper_positions", "exit_path", "TEXT"),
    ("paper_positions", "rungs_taken", "INTEGER"),
    ("paper_positions", "near_miss_armed", "INTEGER"),
    ("paper_position_snapshots", "exit_path", "TEXT"),
]


def _apply_added_columns(connection: sqlite3.Connection) -> None:
    """Idempotently ALTER TABLE for columns added after the table first shipped.

    SQLite has no IF NOT EXISTS clause for ALTER TABLE ADD COLUMN, so we
    introspect existing columns via PRAGMA table_info and only ADD missing
    ones. Safe to run on every initialize_database() call.
    """
    for table, column, col_type in _ADDED_COLUMNS:
        existing = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
            )


# ─── One-shot: paper book conviction(0-1) → rank(0-100 percentile) ────
# The paper book's sizing metric was renamed and re-anchored on
# 2026-08-28. The old number was an interpolation between two hand-picked
# score anchors, so 0.28 looked like "28% confident" while actually
# meaning "the 40th percentile". Rank is a percentile of the live score
# distribution, so the number says what it appears to say.
#
# The two scales cannot be mixed in one column, so this renames the
# columns and clears values that were written under the old meaning.
# Position entry ranks are recomputed from the score each position stored
# at entry; decision rows keep their English headline (which embeds the
# old number in prose) and drop the numeric, since a log row honestly
# records what was decided under the rules of its day.
_PAPER_RANK_RENAMES = [
    ("paper_positions", "conviction_at_entry", "rank_at_entry"),
    ("paper_positions", "conviction_now", "rank_now"),
    ("paper_decisions", "conviction", "rank"),
    ("paper_decisions", "conviction_prev", "rank_prev"),
]

_PAPER_INTENT_RENAMES = {
    "target_hit": "rung_taken",          # 2026-09-18: half-off-at-target became the ladder
    "new_conviction": "cleared_bar",
    "conviction_upgrade": "rank_upgrade",
    "conviction_decay": "rank_decay",
}


def _migrate_paper_rank_scale(connection: sqlite3.Connection) -> None:
    """Rename the paper book's conviction columns to rank and drop
    old-scale values. Idempotent: a no-op once the columns are renamed."""

    def columns(table: str) -> set[str]:
        try:
            return {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
        except sqlite3.DatabaseError:
            return set()

    renamed_any = False
    for table, old, new in _PAPER_RANK_RENAMES:
        cols = columns(table)
        if not cols or new in cols or old not in cols:
            continue
        connection.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
        renamed_any = True

    if not renamed_any:
        return

    # Decision rows: the headline still tells the story in English; the
    # numeric would be a percentile-shaped value on a 0-1 scale.
    connection.execute("UPDATE paper_decisions SET rank = NULL, rank_prev = NULL")

    # Positions: the entry rank is NOT reconstructable. The score's raw
    # percentile is recoverable from source_json, but the rank the engine
    # actually recorded also carried the signal / R:R / momentum
    # adjustments, and those are not stored per position. Backfilling the
    # base-only percentile produced a fake drift — every migrated position
    # showed "rank 80 → 100, +20" when nothing about it had changed.
    #
    # An unknown entry rank is better than an invented one. These go NULL;
    # `rank_now` refills on the next tick, and the position's stored
    # `source_json.components` still records what the entry read was made
    # of, in prose.
    connection.execute("UPDATE paper_positions SET rank_at_entry = NULL, rank_now = NULL")

    for old_intent, new_intent in _PAPER_INTENT_RENAMES.items():
        connection.execute(
            "UPDATE paper_decisions SET intent = ? WHERE intent = ?", (new_intent, old_intent)
        )
        connection.execute(
            "UPDATE paper_orders SET intent = ? WHERE intent = ?", (new_intent, old_intent)
        )

    # The stored mandate carries floors on the old scale; stamping the
    # scale marker is what makes models.Mandate.from_stored replace it.
    connection.execute(
        "UPDATE paper_portfolios SET mandate_json = "
        "json_set(mandate_json, '$.scale', 'conviction_0_1') "
        "WHERE json_extract(mandate_json, '$.scale') IS NULL"
    )


def _dedupe_existing_documents(connection: sqlite3.Connection) -> int:
    """Remove duplicate (source_id, url) rows keeping the earliest ingested_at.

    Called before creating the unique index so upgrades on databases that
    accumulated duplicates under the old INSERT OR REPLACE path don't fail.
    """
    # Only runs if the documents table already exists.
    table_exists = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
    ).fetchone()
    if not table_exists:
        return 0

    cursor = connection.execute(
        """
        DELETE FROM documents
        WHERE rowid NOT IN (
            SELECT MIN(rowid)
            FROM documents
            WHERE url IS NOT NULL
            GROUP BY source_id, url
        )
        AND url IS NOT NULL
        """
    )
    return cursor.rowcount or 0


def initialize_database(database_path: Path, *, allow_reinit: bool = False) -> None:
    # Defense against a past incident: a smoke test whose env-var override
    # was silently ignored fell back to the production DB path. Refuse to
    # create/re-create the production DB from scratch unless the caller
    # explicitly opts in. Existing production DBs (with our tables) pass
    # through — this is only about brand-new / freshly-wiped files at that
    # path. `allow_reinit=True` is the escape hatch for legit resets.
    if not allow_reinit:
        try:
            from macro_positioning.core.settings import settings as _settings
            prod_path = _settings.sqlite_path.resolve()
        except Exception:
            prod_path = None
        if prod_path is not None and database_path.resolve() == prod_path:
            exists = database_path.exists()
            size = database_path.stat().st_size if exists else 0
            has_schema = False
            if exists and size > 0:
                try:
                    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as _probe:
                        row = _probe.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'"
                        ).fetchone()
                        has_schema = row is not None
                except sqlite3.DatabaseError:
                    has_schema = False
            if not has_schema:
                raise RuntimeError(
                    "Refusing to initialize the production DB at "
                    f"{database_path} — file "
                    f"{'is missing' if not exists else f'exists ({size} bytes) but has no `documents` table'}. "
                    "This looks like an accidental smoke-test target. "
                    "If you truly intend to (re)create the production DB, "
                    "pass allow_reinit=True."
                )
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        # WAL mode = concurrent reads + single writer without lock contention.
        # Critical when the FastAPI server is reading desk_data while a CLI
        # score-pass writes to trade_scores. Default rollback-journal mode
        # would lock the whole DB on writes and stall both sides.
        # Persists in the file's pragma so we only need to set it once.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        # Wait up to 5s for any in-flight writer rather than failing fast
        # with `database is locked`. Safe with WAL because contention
        # windows are short.
        connection.execute("PRAGMA busy_timeout=5000")
        # Create base tables first (documents must exist before dedupe).
        connection.execute(SCHEMA_STATEMENTS[0])
        # Dedupe any pre-existing duplicates before the unique index lands.
        _dedupe_existing_documents(connection)
        for statement in SCHEMA_STATEMENTS[1:]:
            connection.execute(statement)
        # Apply column-add migrations for tables that existed before
        # new columns were introduced.
        _apply_added_columns(connection)
        _migrate_paper_rank_scale(connection)
        connection.commit()

    # Seed the manual-input known-authors picklist (idempotent — only
    # inserts on first boot, never overwrites user corrections). Imported
    # late to avoid a circular import via macro_positioning.manual.models.
    try:
        from macro_positioning.manual.authors import seed_known_authors
        seed_known_authors(db_path=database_path)
    except Exception:
        # Seeding is convenience, not correctness — never fail boot on it.
        import logging
        logging.getLogger(__name__).exception("seed_known_authors failed")
