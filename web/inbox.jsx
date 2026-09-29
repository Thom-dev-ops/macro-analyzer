// /inbox — manual input layer.
//
// Drop a chart screenshot and/or paste a blurb, attach metadata
// (ticker, side, conviction, timeframe, author + channel), preview
// the auto-detected suggestions, then submit. Submissions land in
// the documents table and propagate to the watchlist on next score.

const { useState: useIS, useEffect: useIE, useRef: useIR } = React;

// Renders the extracted_features_json (Claude vision output) inside a
// history row when expanded. Handles both the flat TradeRecord shape
// (trading_agent baseline import) and the richer "setups" array shape
// the live framework prompt returns. Falls back to a JSON dump for
// anything we don't have a custom renderer for.
// ── Helpers for the analysis renderer ──
function _classifyLevel(text) {
  // Tag a key-level string as resistance / support / neutral so we can
  // colorize the chip. Looks for explicit keywords; falls back to neutral.
  const t = String(text).toLowerCase();
  if (/resist|breakdown|ceiling|supply/.test(t)) return "resist";
  if (/support|floor|demand|bounce/.test(t)) return "support";
  return "neutral";
}
function _stripPrice(text) {
  // Many level strings start with "201.35 — major resistance / prior …"
  // — split price from descriptor for nicer chips.
  const m = String(text).match(/^([\d.,kKmM]+)\s*[—\-–:]\s*(.+)$/);
  if (m) return { price: m[1], desc: m[2] };
  return { price: null, desc: String(text) };
}
// Key-level entries can arrive as strings or as objects like
// {price, role, ...}. Normalize to {price, desc} for the chip.
function _levelToChip(l) {
  if (l && typeof l === "object") {
    const price = l.price != null ? String(l.price) : null;
    const desc = l.role || l.desc || l.label || l.note || "";
    if (price || desc) return { price, desc };
    return _stripPrice(JSON.stringify(l));
  }
  return _stripPrice(l);
}
function _levelClassify(l) {
  if (l && typeof l === "object") return _classifyLevel(l.role || l.desc || "");
  return _classifyLevel(l);
}

// Human label for a non-directional call_type — shown in place of a bias
// badge for drops the vision layer flagged as carrying no actionable call
// (a price-ticker screenshot, a chart with no setup, a recap, …).
const _CALL_TYPE_LABEL = {
  no_trade: "no trade",
  not_a_chart: "not a chart",
  retrospective: "recap",
  bidirectional: "both ways",
};
function _callTypeLabel(ct) {
  return _CALL_TYPE_LABEL[(ct || "").toLowerCase()] || null;
}

// Per-ticker summary shown in the collapsed section header: how fresh the
// setup is (newest drop age + decay status) and where sentiment sits (bull
// vs bear counts across the loaded drops).
function _summarizeDrops(drops) {
  if (!Array.isArray(drops) || drops.length === 0) return null;
  let bull = 0, bear = 0, neut = 0;
  let latestTs = 0, latestDrop = null;
  let freshestDecay = "unknown";
  const decayRank = { active: 3, aging: 2, stale: 1, unknown: 0 };
  for (const d of drops) {
    const b = (d.bias || "").toLowerCase();
    if (b.startsWith("bull")) bull++;
    else if (b.startsWith("bear")) bear++;
    else neut++;
    const ts = Date.parse(d.published_at || "") || 0;
    if (ts > latestTs) { latestTs = ts; latestDrop = d; }
    const ds = d.decay?.signal_status || "unknown";
    if ((decayRank[ds] || 0) > (decayRank[freshestDecay] || 0)) freshestDecay = ds;
  }
  const total = bull + bear + neut;
  const bullPct = Math.round((bull / total) * 100);
  const bearPct = Math.round((bear / total) * 100);
  const neutPct = Math.max(0, 100 - bullPct - bearPct);
  let dominant = "neutral", sentimentLabel = "";
  if (bull > bear && bull > neut) { dominant = "bull"; sentimentLabel = `${bullPct}% bullish`; }
  else if (bear > bull && bear > neut) { dominant = "bear"; sentimentLabel = `${bearPct}% bearish`; }
  else if (bull === bear && bull > 0) { dominant = "mixed"; sentimentLabel = "mixed"; }
  else { dominant = "neutral"; sentimentLabel = `${neutPct}% neutral`; }

  let ageLabel = "";
  if (latestTs) {
    const days = Math.max(0, Math.floor((Date.now() - latestTs) / 86400000));
    if (days === 0) ageLabel = "today";
    else if (days === 1) ageLabel = "1d ago";
    else if (days < 30) ageLabel = `${days}d ago`;
    else if (days < 365) ageLabel = `${Math.round(days / 30)}mo ago`;
    else ageLabel = `${Math.round(days / 365)}y ago`;
  }
  return {
    latest: latestDrop,
    ageLabel,
    freshness: freshestDecay,
    bull, bear, neut, bullPct, bearPct, neutPct,
    dominant, sentimentLabel,
    hasSentiment: total > 0,
  };
}

// ── Attribution card with inline reassign ──
// Renders the document's current FROM line as a clickable chip. Click
// → fetches the seeded author picklist → user selects → PATCH the
// document → header refreshes. Designed for bulk-import cleanup: when
// 90 drops land all tagged "Big_Nuts" but ~10% are actually joejoe55,
// the user can correct them inline without leaving the analysis card.
function ReassignableAttribution({ historyRow }) {
  const [editing, setEditing] = useIS(false);
  const [authors, setAuthors] = useIS([]);
  const [saving, setSaving] = useIS(false);
  const [author, setAuthor] = useIS(historyRow.author);
  const [channel, setChannel] = useIS(historyRow.user_metadata?.channel || "");
  const [parent, setParent] = useIS(historyRow.user_metadata?.parent_channel || "");

  useIE(() => {
    if (editing && authors.length === 0) {
      fetch("/api/manual/authors").then(r => r.json()).then(rows => {
        // Hide the synthetic archive author from the picker.
        setAuthors(rows.filter(a => a.channel !== "archive"));
      });
    }
  }, [editing]);

  async function pickAuthorForReassign(a) {
    setSaving(true);
    try {
      const r = await fetch(`/api/manual/inputs/${historyRow.document_id}/author`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          display_name: a.display_name,
          channel: a.channel,
          channel_type: a.channel_type,
        }),
      });
      if (!r.ok) {
        alert(`Reassign failed: ${r.status}`);
        return;
      }
      const data = await r.json();
      setAuthor(data.display_name);
      setChannel(data.channel || "");
      setParent(data.parent_channel || "");
      setEditing(false);
      // Tell the parent table to refresh too so the row re-renders.
      window.dispatchEvent(new CustomEvent("macro:author-reassigned", {
        detail: { document_id: historyRow.document_id, author_id: data.author_id }
      }));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="cd-pane-attribution">
      <span className="cd-pane-lbl">FROM</span>
      <strong>{author || "—"}</strong>
      {channel && <span className="dim"> · {channel}</span>}
      {parent && <span className="dim"> · {parent}</span>}
      <button
        className="filter-pill"
        style={{ marginLeft: "auto", fontSize: 10, padding: "2px 8px" }}
        onClick={() => setEditing(v => !v)}
        title="Re-attribute this drop to a different author"
      >
        {editing ? "✕ cancel" : "↻ reassign"}
      </button>
      {editing && (
        <div style={{ width: "100%", marginTop: 8 }}>
          <div className="dim mono" style={{ fontSize: 10, marginBottom: 6 }}>
            pick the actual author:
          </div>
          <div className="filter-pill-row">
            {authors.map(a => (
              <button
                key={a.author_id}
                className={`filter-pill ${a.display_name === author ? "on" : ""}`}
                title={`${a.channel || "—"}${a.parent_channel ? " · " + a.parent_channel : ""}`}
                disabled={saving}
                onClick={() => pickAuthorForReassign(a)}
              >
                {a.display_name}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Reconcile author voice with AI vision ──
// Cheap rule-based "synthesis" — no LLM call. Compares the user's typed
// blurb (and metadata side) against Claude's extracted bias/setup, surfaces
// agreement / disagreement, and unions the levels both spotted. Good enough
// to make the third tab useful today; can swap for an LLM compile later.
function _synthesize({ authorText = "", authorSide = "", aiBias = "", aiSetup = "", aiLevels = [] }) {
  const t = (authorText + " " + authorSide).toLowerCase();
  const authorBias =
    /\b(short|bear|bearish|breakdown|breaking down|down)\b/.test(t) ? "bearish"
    : /\b(long|bull|bullish|breakout|breaking out|up)\b/.test(t) ? "bullish"
    : /\b(watch|neutral|chop|range|sideways)\b/.test(t) ? "neutral"
    : null;
  const ai = (aiBias || "").toLowerCase();
  let alignment = null;
  if (authorBias && ai) {
    const norm = (s) => s.includes("bull") || s.includes("long") ? "bullish"
                     : s.includes("bear") || s.includes("short") ? "bearish"
                     : s.includes("neutral") ? "neutral" : s;
    alignment = norm(authorBias) === norm(ai) ? "agree" : "disagree";
  }
  // Surface levels both sides mention by simple substring match
  const sharedLevels = aiLevels.filter(l => {
    const price = String(l).match(/[\d.]+/)?.[0];
    return price && authorText.includes(price);
  });
  return { authorBias, alignment, sharedLevels };
}

// ── Three-perspective drop detail card ──
//   • Author tab: what the source said (blurb + their captured metadata)
//   • Vision tab: what Claude extracted from the image(s)
//   • Synthesis tab: alignment / disagreement between the two
function ChartDropDetail({ historyRow, attachmentPaths = [], onJumpToAsset }) {
  const [tab, setTab] = useIS("vision");
  const features = historyRow.extracted_features;
  const meta = historyRow.user_metadata?.resolved || {};
  const userMeta = historyRow.user_metadata?.user || {};
  const authorText = (historyRow.cleaned_text || historyRow.raw_text || "").trim();

  const setups = Array.isArray(features?.setups) ? features.setups : null;
  const top = setups?.[0] || features || {};
  const ticker = features?.ticker || meta.ticker;
  const tf = features?.timeframe || meta.timeframe;
  const aiBias = top?.bias;
  const aiSetup = top?.setup_type || top?.pattern;
  const aiLevels = top?.key_levels || top?.historical_levels || [];

  const synth = _synthesize({
    authorText,
    authorSide: meta.side,
    aiBias,
    aiSetup,
    aiLevels: aiLevels.map(l => typeof l === "string" ? l : JSON.stringify(l)),
  });

  return (
    <div className="cd-card">
      {/* === Headline (always visible) === */}
      <div className="cd-headline">
        <button
          className="cd-ticker"
          title={ticker ? `open ${ticker} on the positioning desk` : ""}
          disabled={!ticker || !onJumpToAsset}
          onClick={(e) => { e.stopPropagation(); ticker && onJumpToAsset?.(ticker); }}
        >
          {ticker || "?"}
        </button>
        {tf && <span className="cd-tf">{tf}</span>}
        {meta.side && <span className={`cd-badge cd-side-${meta.side.toLowerCase()}`}>{meta.side}</span>}
        {aiBias && !["no_trade", "not_a_chart", "retrospective"].includes(String(features?.call_type || "").toLowerCase())
          ? <span className={`cd-badge bias-${String(aiBias).toLowerCase()}`}>● AI: {aiBias}</span>
          : _callTypeLabel(features?.call_type) && (
              <span className="cd-badge dim" title="non-directional — no actionable call">
                AI: {_callTypeLabel(features?.call_type)}
              </span>
            )}
        {synth.alignment === "agree" && <span className="cd-badge cd-agree">↔ aligned</span>}
        {synth.alignment === "disagree" && <span className="cd-badge cd-disagree">⚡ split view</span>}
      </div>

      {/* === Tab strip === */}
      <div className="cd-tabs">
        <button className={`cd-tab ${tab === "author" ? "on" : ""}`} onClick={() => setTab("author")}>
          author
        </button>
        <button className={`cd-tab ${tab === "vision" ? "on" : ""}`} onClick={() => setTab("vision")}>
          claude vision
        </button>
        <button className={`cd-tab ${tab === "synthesis" ? "on" : ""}`} onClick={() => setTab("synthesis")}>
          synthesis
        </button>
      </div>

      {/* === AUTHOR === */}
      {tab === "author" && (
        <div className="cd-pane">
          <ReassignableAttribution historyRow={historyRow} />
          {authorText ? (
            <blockquote className="cd-quote">{authorText}</blockquote>
          ) : (
            <div className="dim">No text — chart-only drop.</div>
          )}
          <div className="cd-meta-grid">
            {userMeta.ticker && <div><span className="cd-pane-lbl">TICKER</span><span className="mono">{userMeta.ticker}</span></div>}
            {userMeta.side && <div><span className="cd-pane-lbl">SIDE</span><span className="mono">{userMeta.side}</span></div>}
            {userMeta.conviction && <div><span className="cd-pane-lbl">CONVICTION</span><span className="mono">{userMeta.conviction}/5</span></div>}
            {userMeta.timeframe && <div><span className="cd-pane-lbl">TIMEFRAME</span><span className="mono">{userMeta.timeframe}</span></div>}
            {userMeta.note && <div className="cd-meta-full"><span className="cd-pane-lbl">NOTE</span><span>{userMeta.note}</span></div>}
          </div>
        </div>
      )}

      {/* === CLAUDE VISION === */}
      {tab === "vision" && (
        <div className="cd-pane">
          {!features ? (
            <div className="dim">No vision analysis yet — click "analyze pending" above.</div>
          ) : features.error ? (
            <div className="dim">Vision error: {String(features.error).slice(0, 200)}</div>
          ) : (
            <VisionPane features={features} top={top} model={features.vision_model || features.model} />
          )}
        </div>
      )}

      {/* === SYNTHESIS === */}
      {tab === "synthesis" && (
        <div className="cd-pane">
          <SynthesisPane
            authorBias={synth.authorBias}
            authorText={authorText}
            authorSide={meta.side}
            aiBias={aiBias}
            aiSetup={aiSetup}
            aiSetupNotes={top?.notes}
            sharedLevels={synth.sharedLevels}
            alignment={synth.alignment}
            probable={top?.most_probable_next_move}
            invalidation={top?.invalidation_level || top?.invalidation}
          />
        </div>
      )}
    </div>
  );
}

// === VISION PANE — Claude's structured extraction, deduplicated ===
// Pull each field from setup-level FIRST (richer when present), fall back
// to top-level. Handles three known schema variants Claude returns:
//   • flat TradeRecord (trading_agent baseline import)
//   • {bias, pattern, ...} top-level + sparse setups[].entry/stop
//   • {setups: [{bias, pattern, ema_state, ...}]} fully-nested
function _pick(top, features, ...keys) {
  for (const k of keys) {
    if (top && top[k] != null) return top[k];
    if (features && features[k] != null) return features[k];
  }
  return null;
}
function VisionPane({ features, top, model }) {
  const keyLevels =
    _pick(top, features, "key_levels", "historical_levels", "historical_level_alignment") || [];
  const indicators = _pick(top, features, "indicators_visible") || [];
  const macd = _pick(top, features, "macd_state", "macd_ttm_state");
  const rsi = _pick(top, features, "rsi_state", "rsi_structure");
  const ema = _pick(top, features, "ema_state", "ema_structure");
  const invalidation = _pick(top, features, "invalidation_level", "invalidation");
  const probable = _pick(top, features, "most_probable_next_move", "next_move");
  const confluence = _pick(top, features, "confluence_score");
  const confluenceText = _pick(top, features, "overall_confluence");
  const fib = _pick(top, features, "fib_levels", "fib_confluence");
  const setupType = _pick(top, features, "setup_type", "pattern", "dominant_pattern");
  const notes = _pick(top, features, "notes");
  const bias = _pick(top, features, "bias");

  // Sentinels so we can show a placeholder when Claude returned literally
  // nothing extractable.
  const hasAny = setupType || keyLevels.length || macd || rsi || ema ||
                 indicators.length || fib || invalidation || probable || notes;

  return (
    <div className="cd-vision">
      {!hasAny && (
        <div className="dim cd-text">Claude returned no structured fields for this image.</div>
      )}

      {/* Headline read */}
      {(setupType || bias) && (
        <div className="cd-headread">
          {bias && <span className={`cd-badge bias-${String(bias).toLowerCase()}`}>● {bias}</span>}
          {setupType && <span className="cd-pattern">{setupType}</span>}
        </div>
      )}

      {/* Confluence — full-width meter, prose under */}
      {(confluence != null || confluenceText) && (
        <div className="cd-block">
          <div className="cd-block-head">
            <span className="cd-block-title">Confluence</span>
            {confluence != null && (
              <span className="cd-confluence">
                {[1,2,3,4,5].map(n => (
                  <span key={n} className={`cd-conf-dot ${n <= Number(confluence) ? "on" : ""}`} />
                ))}
                <span className="dim mono" style={{ marginLeft: 6 }}>{confluence}/5</span>
              </span>
            )}
          </div>
          {confluenceText && <p className="cd-block-text">{confluenceText}</p>}
        </div>
      )}

      {/* Key levels */}
      {keyLevels.length > 0 && (
        <div className="cd-block">
          <div className="cd-block-head"><span className="cd-block-title">Key levels</span></div>
          <div className="cd-chips">
            {(Array.isArray(keyLevels) ? keyLevels : [keyLevels]).map((l, i) => {
              const text = typeof l === "string" ? l : JSON.stringify(l);
              const cls = _classifyLevel(text);
              const { price, desc } = _stripPrice(text);
              return (
                <span key={i} className={`cd-chip cd-chip-${cls}`}>
                  {price && <span className="cd-chip-price mono">{price}</span>}
                  <span className="cd-chip-desc">{desc}</span>
                </span>
              );
            })}
          </div>
        </div>
      )}

      {/* Indicator state — three rows, gold key */}
      {(macd || rsi || ema) && (
        <div className="cd-block">
          <div className="cd-block-head"><span className="cd-block-title">Indicator state</span></div>
          <div className="cd-state-grid">
            {macd && <div className="cd-state-row"><span className="cd-state-key">MACD</span><span>{macd}</span></div>}
            {rsi && <div className="cd-state-row"><span className="cd-state-key">RSI</span><span>{rsi}</span></div>}
            {ema && <div className="cd-state-row"><span className="cd-state-key">EMA</span><span>{ema}</span></div>}
          </div>
        </div>
      )}

      {indicators.length > 0 && (
        <div className="cd-block">
          <div className="cd-block-head"><span className="cd-block-title">Indicators visible</span></div>
          <div className="cd-chips">
            {indicators.map((ind, i) => (
              <span key={i} className="cd-chip cd-chip-neutral"><span className="cd-chip-desc">{ind}</span></span>
            ))}
          </div>
        </div>
      )}

      {fib && (
        <div className="cd-block">
          <div className="cd-block-head"><span className="cd-block-title">Fibonacci</span></div>
          <p className="cd-block-text">{typeof fib === "string" ? fib : JSON.stringify(fib)}</p>
        </div>
      )}

      {/* Next move — emphasized as the operative read */}
      {probable && (
        <div className="cd-block cd-block-key">
          <div className="cd-block-head"><span className="cd-block-title">Most probable next move</span></div>
          <p className="cd-block-text">{probable}</p>
        </div>
      )}

      {invalidation && (
        <div className="cd-block">
          <div className="cd-block-head"><span className="cd-block-title">Invalidation</span></div>
          <p className="cd-block-text">{typeof invalidation === "string" ? invalidation : JSON.stringify(invalidation)}</p>
        </div>
      )}

      {notes && notes !== probable && (
        <div className="cd-block">
          <div className="cd-block-head"><span className="cd-block-title">Notes</span></div>
          <p className="cd-block-text">{notes}</p>
        </div>
      )}

      <details className="cd-collapsible">
        <summary className="cd-row-lbl-link">raw extraction JSON ▾</summary>
        <pre className="cd-raw mono">{JSON.stringify(features, null, 2)}</pre>
      </details>

      {model && <div className="cd-footer mono">{model}</div>}
    </div>
  );
}

// === SYNTHESIS PANE — agreement / disagreement, no LLM call ===
function SynthesisPane({ authorBias, authorText, authorSide, aiBias, aiSetup, aiSetupNotes, sharedLevels, alignment, probable, invalidation }) {
  if (!authorText && !aiBias) return <div className="dim">Nothing to synthesize yet.</div>;
  return (
    <div className="cd-synthesis">
      <div className="cd-synth-grid">
        <div className="cd-synth-cell">
          <div className="cd-row-lbl">Author read</div>
          <div className="cd-synth-bias">
            <span className={`cd-badge ${authorBias ? "bias-" + authorBias : ""}`}>
              {authorBias ? `● ${authorBias}` : "—"}
            </span>
            {authorSide && <span className={`cd-badge cd-side-${authorSide.toLowerCase()}`} style={{ marginLeft: 6 }}>{authorSide}</span>}
          </div>
        </div>
        <div className="cd-synth-cell">
          <div className="cd-row-lbl">Claude read</div>
          <div className="cd-synth-bias">
            <span className={`cd-badge ${aiBias ? "bias-" + String(aiBias).toLowerCase() : ""}`}>
              {aiBias ? `● ${aiBias}` : "—"}
            </span>
            {aiSetup && <div className="dim" style={{ marginTop: 4, fontSize: 12 }}>{aiSetup}</div>}
          </div>
        </div>
      </div>

      <div className="cd-row">
        <div className="cd-row-lbl">Alignment</div>
        <div className="cd-row-body">
          {alignment === "agree" ? (
            <div className="cd-text"><span className="cd-agree">●</span> Both author and Claude read the same direction. Higher conviction signal.</div>
          ) : alignment === "disagree" ? (
            <div className="cd-text"><span className="cd-disagree">●</span> Author and Claude disagree on direction. Worth a closer look — one read may be missing context.</div>
          ) : (
            <div className="dim cd-text">Author bias not detected from text — review manually.</div>
          )}
        </div>
      </div>

      {sharedLevels.length > 0 && (
        <div className="cd-row">
          <div className="cd-row-lbl">Shared levels</div>
          <div className="cd-row-body cd-chips">
            {sharedLevels.map((l, i) => (
              <span key={i} className="cd-chip cd-chip-shared">
                <span className="cd-chip-desc">{l}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {probable && (
        <div className="cd-row">
          <div className="cd-row-lbl">If correct, watch for</div>
          <div className="cd-row-body"><div className="cd-text cd-emphasis">{probable}</div></div>
        </div>
      )}

      {invalidation && (
        <div className="cd-row">
          <div className="cd-row-lbl">Invalidation</div>
          <div className="cd-row-body"><div className="cd-text">{typeof invalidation === "string" ? invalidation : JSON.stringify(invalidation)}</div></div>
        </div>
      )}

      <div className="cd-footer dim">
        Heuristic synthesis (rule-based). Future: LLM compile that reconciles author + chart in prose.
      </div>
    </div>
  );
}

// Legacy export kept until the inline-summary path is fully removed.
function ChartAnalysisDetail({ features, attachmentPaths = [], onJumpToAsset }) {
  if (!features) return null;
  const setups = Array.isArray(features.setups) ? features.setups : null;
  const ticker = features.ticker;
  const tf = features.timeframe;
  const sha = features.image_sha256;
  const model = features.vision_model || features.model;
  const chartDate = features.chart_date;
  const top = setups?.[0] || features;
  const bias = top?.bias;
  const setupType = top?.setup_type || top?.pattern;
  const keyLevels = top?.key_levels || top?.historical_levels || [];
  const indicators = top?.indicators_visible || [];
  const macd = top?.macd_state;
  const rsi = top?.rsi_state || top?.rsi_structure;
  const ema = top?.ema_state;
  const invalidation = top?.invalidation_level || top?.invalidation;
  const probable = top?.most_probable_next_move;
  const notes = top?.notes;
  const confluence = top?.confluence_score;
  const confluenceText = top?.overall_confluence;
  const fib = top?.fib_levels || top?.fib_confluence;

  const biasClass = bias ? `bias-${String(bias).toLowerCase()}` : "";

  return (
    <div className="ca-card">
      {/* === Headline === */}
      <div className="ca-headline">
        <button
          className="ca-ticker"
          title={ticker ? `open ${ticker} on the positioning desk` : ""}
          disabled={!ticker || !onJumpToAsset}
          onClick={(e) => { e.stopPropagation(); ticker && onJumpToAsset?.(ticker); }}
        >
          {ticker || "?"}
        </button>
        {tf && <span className="ca-tf">{tf}</span>}
        {bias && <span className={`ca-badge ${biasClass}`}>● {bias}</span>}
        {chartDate && <span className="ca-meta">chart · {chartDate}</span>}
        <span className="ca-meta ca-spacer">{model || ""}</span>
      </div>

      {/* === Setup tagline === */}
      {setupType && <div className="ca-setup">{setupType}</div>}

      {/* === Confluence meter === */}
      {(confluence != null || confluenceText) && (
        <div className="ca-section">
          <div className="ca-lbl">Confluence</div>
          <div className="ca-section-body">
            {confluence != null && (
              <span className="ca-confluence">
                {[1,2,3,4,5].map(n => (
                  <span key={n} className={`ca-conf-dot ${n <= Number(confluence) ? "on" : ""}`} />
                ))}
                <span className="dim mono" style={{ marginLeft: 6 }}>{confluence}/5</span>
              </span>
            )}
            {confluenceText && <div className="ca-text">{confluenceText}</div>}
          </div>
        </div>
      )}

      {/* === Levels — colorized chips === */}
      {keyLevels.length > 0 && (
        <div className="ca-section">
          <div className="ca-lbl">Key levels</div>
          <div className="ca-section-body ca-chips">
            {keyLevels.map((l, i) => {
              const text = typeof l === "string" ? l : JSON.stringify(l);
              const cls = _classifyLevel(text);
              const { price, desc } = _stripPrice(text);
              return (
                <span key={i} className={`ca-chip ca-chip-${cls}`}>
                  {price && <span className="ca-chip-price mono">{price}</span>}
                  <span className="ca-chip-desc">{desc}</span>
                </span>
              );
            })}
          </div>
        </div>
      )}

      {/* === Indicator state === */}
      {(macd || rsi || ema) && (
        <div className="ca-section">
          <div className="ca-lbl">Indicator state</div>
          <div className="ca-section-body ca-state-grid">
            {macd && <div className="ca-state-row"><span className="ca-state-key">MACD</span><span>{macd}</span></div>}
            {rsi && <div className="ca-state-row"><span className="ca-state-key">RSI</span><span>{rsi}</span></div>}
            {ema && <div className="ca-state-row"><span className="ca-state-key">EMA</span><span>{ema}</span></div>}
          </div>
        </div>
      )}

      {indicators.length > 0 && (
        <div className="ca-section">
          <div className="ca-lbl">Indicators visible</div>
          <div className="ca-section-body ca-chips">
            {indicators.map((ind, i) => (
              <span key={i} className="ca-chip ca-chip-neutral"><span className="ca-chip-desc">{ind}</span></span>
            ))}
          </div>
        </div>
      )}

      {fib && (
        <div className="ca-section">
          <div className="ca-lbl">Fibonacci</div>
          <div className="ca-section-body">
            <div className="ca-text">{typeof fib === "string" ? fib : JSON.stringify(fib)}</div>
          </div>
        </div>
      )}

      {invalidation && (
        <div className="ca-section">
          <div className="ca-lbl">Invalidation</div>
          <div className="ca-section-body">
            <div className="ca-text">{typeof invalidation === "string" ? invalidation : JSON.stringify(invalidation)}</div>
          </div>
        </div>
      )}

      {probable && (
        <div className="ca-section">
          <div className="ca-lbl">Most probable next move</div>
          <div className="ca-section-body">
            <div className="ca-text ca-text-emphasis">{probable}</div>
          </div>
        </div>
      )}

      {notes && notes !== probable && (
        <div className="ca-section">
          <div className="ca-lbl">Notes</div>
          <div className="ca-section-body">
            <div className="ca-text">{notes}</div>
          </div>
        </div>
      )}

      {/* Multi-image rollup */}
      {setups && setups.length > 1 && (
        <details className="ca-section ca-collapsible">
          <summary className="ca-lbl-link">{setups.length} setup entries (per image) ▾</summary>
          {setups.map((s, i) => (
            <div key={i} className="ca-multi-setup">
              <div className="ca-multi-setup-head">
                <span className={`ca-badge ${s.bias ? "bias-" + String(s.bias).toLowerCase() : ""}`}>
                  ● {s.bias || "—"}
                </span>
                <span style={{ marginLeft: 6 }}>{s.pattern || s.setup_type || "—"}</span>
              </div>
            </div>
          ))}
        </details>
      )}

      <details className="ca-section ca-collapsible">
        <summary className="ca-lbl-link">raw extraction JSON ▾</summary>
        <pre className="ca-raw-json mono">{JSON.stringify(features, null, 2)}</pre>
      </details>

      {sha && <div className="ca-footer mono">image_sha256: {sha.slice(0, 16)}…</div>}
    </div>
  );
}

const SIDES = ["LONG", "SHORT", "WATCH"];
const TIMEFRAMES = ["1H", "4H", "1D", "1W"];
// Delivery venue (where the idea reached you), NOT where the chart was
// drawn. TradingView is a charting platform, not a channel — a Big_Nuts
// TV chart forwarded to you in Stock Unlocked is channel_type=telegram.
const CHANNEL_TYPES = ["self", "telegram", "discord", "twitter", "other"];

function Inbox() {
  // Form state
  const [text, setText] = useIS("");
  const [ticker, setTicker] = useIS("");
  const [side, setSide] = useIS("");
  const [conviction, setConviction] = useIS(3);
  const [timeframe, setTimeframe] = useIS("");
  const [note, setNote] = useIS("");
  const [author, setAuthor] = useIS("");
  const [channel, setChannel] = useIS("");
  const [channelType, setChannelType] = useIS("self");
  // True when the user wants to type a one-off author not in the seeded
  // picklist. Default: hide the freeform input, show only the pill row.
  const [useFreeformAuthor, setUseFreeformAuthor] = useIS(false);

  // Image state — list of {file, url} so a single drop can carry several
  // chart views (e.g. 1H + 4H + context). Append on drop/paste/picker.
  const [images, setImages] = useIS([]);
  // URL of the image currently shown in the lightbox (null = closed).
  // Lets the user click any thumb to verify chart details at full size.
  const [lightboxUrl, setLightboxUrl] = useIS(null);
  const dropRef = useIR(null);

  // Close lightbox on Escape so it stays keyboard-friendly.
  useIE(() => {
    if (!lightboxUrl) return;
    function onKey(e) { if (e.key === "Escape") setLightboxUrl(null); }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [lightboxUrl]);

  // Async state
  const [previewData, setPreviewData] = useIS(null);
  const [submitting, setSubmitting] = useIS(false);
  const [submitMsg, setSubmitMsg] = useIS(null);

  // Server-side data
  const [history, setHistory] = useIS([]);
  const [authors, setAuthors] = useIS([]);
  const [draining, setDraining] = useIS(false);
  // When on, /save POSTs analyze=true and blocks ~20s/image while Claude
  // extracts the TradeRecord, then shows the result inline. Off = save
  // returns immediately and you click "analyze pending" later.
  const [analyzeOnSave, setAnalyzeOnSave] = useIS(true);
  // When true, the drop is marked as the operator's own chart. The
  // server runs signal extraction synchronously so the confirmation
  // panel can render the extracted signals immediately. Other drops
  // get batched in the next morning_run.
  const [isMyChart, setIsMyChart] = useIS(false);
  // Signal previews returned from /api/manual/ingest when is_self_authored=true.
  const [lastSignals, setLastSignals] = useIS([]);
  const [lastInlineErr, setLastInlineErr] = useIS(null);
  // Doc id whose history row is expanded to show full extracted_features_json.
  // Default behavior: when history loads, auto-expand the most recent
  // analyzed row so the user immediately sees what Claude extracted —
  // no clicking needed. They can collapse it or expand any other row.
  const [expandedDoc, setExpandedDoc] = useIS(null);
  const [autoExpandedOnce, setAutoExpandedOnce] = useIS(false);

  useIE(() => { refreshHistory(); refreshAuthors(); }, []);

  // When ReassignableAttribution PATCHes a row, refresh the history table
  // so the AUTHOR column re-renders with the new attribution.
  useIE(() => {
    function onReassigned() { refreshHistory(); refreshAuthors(); }
    window.addEventListener("macro:author-reassigned", onReassigned);
    return () => window.removeEventListener("macro:author-reassigned", onReassigned);
  }, []);

  // Auto-expand the most recent analyzed row once the history first loads.
  // This way the page lands with the latest extraction visible — the user
  // doesn't have to know to click. Subsequent refreshes preserve whatever
  // they have open / collapsed manually.
  useIE(() => {
    if (autoExpandedOnce || history.length === 0) return;
    const firstAnalyzed = history.find(h =>
      h.extracted_features && !h.extracted_features.error
    );
    if (firstAnalyzed) {
      setExpandedDoc(firstAnalyzed.document_id);
      setAutoExpandedOnce(true);
    }
  }, [history, autoExpandedOnce]);

  // Tracks refresh state so the button can show feedback and we don't
  // mask silent failures. Logs every step to the console for diagnosis.
  const [refreshState, setRefreshState] = useIS("idle"); // idle | loading | ok | error
  async function refreshHistory() {
    console.log("[inbox] refreshHistory → GET /api/manual/inputs?limit=20");
    setRefreshState("loading");
    try {
      const r = await fetch("/api/manual/inputs?limit=20");
      console.log("[inbox] refresh response", r.status, r.statusText);
      if (!r.ok) {
        const body = await r.text();
        console.warn("[inbox] refresh failed body:", body.slice(0, 300));
        setRefreshState("error");
        setSubmitMsg({ type: "err", text: `Refresh failed: ${r.status}` });
        return;
      }
      const data = await r.json();
      console.log("[inbox] refresh got", data.length, "rows");
      setHistory(data);
      setRefreshState("ok");
      // Re-trigger auto-expand of latest analyzed row on each successful refresh.
      setAutoExpandedOnce(false);
    } catch (e) {
      console.error("[inbox] refresh error", e);
      setRefreshState("error");
      setSubmitMsg({ type: "err", text: `Refresh error: ${String(e).slice(0, 100)}` });
    }
  }
  function refreshAuthors() {
    fetch("/api/manual/authors").then(r => r.json()).then(setAuthors);
  }

  // Drag-drop + paste-from-clipboard image handling
  useIE(() => {
    function onPaste(e) {
      const items = e.clipboardData?.items || [];
      const fresh = [];
      for (const it of items) {
        if (it.type && it.type.startsWith("image/")) {
          const f = it.getAsFile();
          if (f) fresh.push(f);
        }
      }
      if (fresh.length) attachFiles(fresh);
    }
    document.addEventListener("paste", onPaste);
    return () => document.removeEventListener("paste", onPaste);
  }, []);

  function attachFiles(fs) {
    const accepted = Array.from(fs).filter(f => f && f.type && f.type.startsWith("image/"));
    if (!accepted.length) return;
    setImages(prev => [
      ...prev,
      ...accepted.map(f => ({ file: f, url: URL.createObjectURL(f) })),
    ]);
    // Fire-and-forget OCR auto-fill on the freshly added files. Doesn't
    // block the UI — preview returns suggestions which we merge into any
    // form fields the user hasn't already touched.
    autoFillFromImages(accepted);
  }
  function removeImage(idx) {
    setImages(prev => {
      const it = prev[idx];
      if (it) URL.revokeObjectURL(it.url);
      return prev.filter((_, i) => i !== idx);
    });
  }
  function clearImages() {
    setImages(prev => {
      prev.forEach(it => URL.revokeObjectURL(it.url));
      return [];
    });
  }
  function onDrop(e) {
    e.preventDefault();
    if (e.dataTransfer.files?.length) attachFiles(e.dataTransfer.files);
  }
  function onDragOver(e) { e.preventDefault(); }

  async function runPreview() {
    // The "analyze" button works with or without an author — OCR runs
    // regardless, and the result fills the author field if it's blank.
    const payload = buildPayload();
    let r;
    if (images.length) {
      const fd = new FormData();
      fd.append("payload", JSON.stringify(payload));
      images.forEach(({ file: f }) => fd.append("files", f, f.name));
      r = await fetch("/api/manual/preview/multipart", { method: "POST", body: fd });
    } else {
      r = await fetch("/api/manual/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }
    if (!r.ok) {
      setSubmitMsg({ type: "err", text: "Preview failed: " + r.status });
      return;
    }
    const data = await r.json();
    setPreviewData(data);
    applyImageSuggestions(data.image_suggestions);
    if (!ticker && data.detected_tickers?.length) setTicker(data.detected_tickers[0]);
  }

  // OCR auto-fill on file drop. Posts a multipart preview with no metadata
  // (server only needs the bytes) and merges any extracted fields into the
  // form, but only where the user hasn't already typed something.
  async function autoFillFromImages(fileList) {
    console.log("[inbox] autoFillFromImages start", fileList.map(f => f.name));
    try {
      const fd = new FormData();
      // Minimal payload — we only care about image_suggestions. Author isn't
      // required for /preview/multipart so an empty AuthorRef is fine here.
      fd.append("payload", JSON.stringify({
        text: "",
        metadata: {},
        author: { display_name: author || "", channel: channel || null, channel_type: channelType || null },
      }));
      fileList.forEach(f => fd.append("files", f, f.name));
      const r = await fetch("/api/manual/preview/multipart", { method: "POST", body: fd });
      console.log("[inbox] /preview/multipart status", r.status);
      if (!r.ok) {
        const body = await r.text();
        console.warn("[inbox] preview failed:", body.slice(0, 300));
        setSubmitMsg({ type: "warn", text: `OCR preview failed: ${r.status}` });
        return;
      }
      const data = await r.json();
      console.log("[inbox] OCR response:", data.image_suggestions);
      applyImageSuggestions(data.image_suggestions);
      if (!ticker && data.detected_tickers?.length) setTicker(data.detected_tickers[0]);
    } catch (e) {
      console.warn("[inbox] OCR auto-fill error:", e);
      setSubmitMsg({ type: "warn", text: `OCR error: ${String(e).slice(0,100)}` });
    }
  }

  // Merge OCR suggestions into the form. User-set fields are NEVER
  // overwritten — first-typed wins. Sets a small inline banner explaining
  // what got auto-filled so the user can verify before saving.
  function applyImageSuggestions(sug) {
    console.log("[inbox] applyImageSuggestions input:", sug, "current state:", { channel, channelType, author, ticker, timeframe });
    if (!sug) return;
    const filled = [];
    const skipped = [];
    if (sug.channel) {
      if (!channel) { setChannel(sug.channel); filled.push("channel"); }
      else skipped.push(`channel (have "${channel}")`);
    }
    if (sug.channel_type) {
      if (!channelType || channelType === "self") {
        setChannelType(sug.channel_type); filled.push("type");
      } else skipped.push(`type (have "${channelType}")`);
    }
    if (sug.author) {
      if (!author) { setAuthor(sug.author); filled.push("author"); }
      else skipped.push(`author (have "${author}")`);
    }
    if (sug.ticker) {
      if (!ticker) { setTicker(sug.ticker); filled.push("ticker"); }
      else skipped.push(`ticker (have "${ticker}")`);
    }
    if (sug.timeframe) {
      if (!timeframe) { setTimeframe(sug.timeframe); filled.push("timeframe"); }
      else skipped.push(`tf (have "${timeframe}")`);
    }
    console.log("[inbox] filled:", filled, "skipped:", skipped, "format:", sug.detected_format);
    if (filled.length) {
      const fmt = sug.detected_format ? ` (${sug.detected_format.replace("_", " ")})` : "";
      setSubmitMsg({
        type: "ok",
        text: `Auto-filled${fmt}: ${filled.join(", ")} · please verify before saving.`,
      });
    } else if (sug.detected_format && sug.detected_format !== "unknown") {
      setSubmitMsg({
        type: "warn",
        text: `OCR matched ${sug.detected_format} but no new fields to fill (all already set).`,
      });
    } else {
      setSubmitMsg({
        type: "warn",
        text: `OCR ran but couldn't identify a known header format on this image.`,
      });
    }
  }

  function buildPayload() {
    return {
      text,
      metadata: {
        ticker: ticker || null,
        side: side || null,
        conviction: Number(conviction) || null,
        timeframe: timeframe || null,
        note: note || null,
      },
      author: {
        display_name: author,
        channel: channel || null,
        channel_type: channelType || null,
      },
      is_self_authored: isMyChart,
    };
  }

  async function submit() {
    if (!author.trim()) {
      setSubmitMsg({ type: "warn", text: "Author is required." });
      return;
    }
    if (!text.trim() && images.length === 0) {
      setSubmitMsg({ type: "warn", text: "Add some text or at least one image." });
      return;
    }
    setSubmitting(true);
    setSubmitMsg(
      analyzeOnSave && images.length
        ? { type: "warn", text: `Saving + analyzing ${images.length} image${images.length === 1 ? "" : "s"} via Claude (~${20 * images.length}s)…` }
        : null
    );
    try {
      const fd = new FormData();
      fd.append("payload", JSON.stringify(buildPayload()));
      // Field name `files` matches the FastAPI `list[UploadFile]` param.
      images.forEach(({ file: f }) => fd.append("files", f, f.name));
      // Toggle inline analysis on the server side. When off, save is fast
      // and the user runs `analyze pending` later (or auto-drains via cron).
      if (analyzeOnSave) fd.append("analyze", "true");
      const r = await fetch("/api/manual/ingest", { method: "POST", body: fd });
      if (!r.ok) {
        const err = await r.text();
        setSubmitMsg({ type: "err", text: "Submit failed: " + err.slice(0, 200) });
        return;
      }
      const data = await r.json();
      const imgNote = images.length > 1 ? ` · ${images.length} images` : "";
      // When inline-analyze ran, pending_vision should be false in the
      // saved row even though the response object reflects pre-drain state.
      // Surface that to the user explicitly.
      const visionStatus = analyzeOnSave && images.length
        ? " · analyzed ✓"
        : (data.pending_vision ? " · vision pending" : "");
      // Signals returned by inline extraction (only present for
      // is_self_authored drops). Render in the confirmation panel.
      setLastSignals(Array.isArray(data.signals) ? data.signals : []);
      setLastInlineErr(data.inline_extraction_error || null);
      const sigNote = Array.isArray(data.signals) && data.signals.length
        ? ` · ${data.signals.length} signal${data.signals.length === 1 ? "" : "s"} extracted`
        : (isMyChart ? " · no signals extracted" : "");
      setSubmitMsg({
        type: "ok",
        text: `Saved · ${data.detected_tickers.join(", ") || "no tickers"} · tags ${data.tags.join(", ")}` +
              imgNote + visionStatus + sigNote,
      });
      // Auto-expand the just-saved doc so the user sees the analysis inline.
      if (analyzeOnSave && images.length) setExpandedDoc(data.document_id);
      // Reset for next drop, keep author so consecutive drops are fast.
      setText(""); setTicker(""); setSide(""); setTimeframe("");
      setNote(""); setConviction(3); clearImages(); setPreviewData(null);
      refreshHistory(); refreshAuthors();
    } finally {
      setSubmitting(false);
    }
  }

  // Cross-view nav: ticker → positioning desk. The Inbox component lives
  // inside App which holds the `view` state. We can't reach setView from
  // here without prop drilling, so dispatch a window CustomEvent and let
  // App listen. App switches to /positioning and (when MA_DATA has a
  // matching signal) opens the reasoning trail for that ticker.
  function jumpToAsset(ticker) {
    if (!ticker) return;
    window.dispatchEvent(new CustomEvent("macro:open-asset", { detail: { ticker } }));
  }

  // Trigger Claude vision on all pending_vision drops. The endpoint is
  // synchronous (~20s/image) so we surface the button-disabled "analyzing…"
  // state until it returns, then refresh the history to show the results.
  async function drainPending() {
    setDraining(true);
    setSubmitMsg(null);
    try {
      const r = await fetch("/api/manual/vision/drain?limit=25", { method: "POST" });
      if (!r.ok) {
        setSubmitMsg({ type: "err", text: `Drain failed: ${r.status}` });
        return;
      }
      const data = await r.json();
      const parts = [];
      if (data.processed) parts.push(`${data.processed} analyzed`);
      if (data.failed) parts.push(`${data.failed} failed`);
      if (data.skipped_no_image) parts.push(`${data.skipped_no_image} skipped (no image)`);
      setSubmitMsg({
        type: data.failed ? "warn" : "ok",
        text: data.candidates === 0
          ? "Nothing pending."
          : `Drain: ${parts.join(", ")}.`,
      });
      refreshHistory();
    } catch (e) {
      setSubmitMsg({ type: "err", text: `Drain error: ${String(e).slice(0, 100)}` });
    } finally {
      setDraining(false);
    }
  }

  // Author autocomplete: pick from known authors → fills name + channel + channel_type.
  // Parent channel is informational (renders in breadcrumb on detail card)
  // but not editable from the form yet — it inherits from the seed.
  function pickAuthor(a) {
    console.log("[inbox] pickAuthor:", a);
    setAuthor(a.display_name || "");
    setChannel(a.channel || "");
    setChannelType(a.channel_type || "self");
    setUseFreeformAuthor(false);
  }

  // Persist a brand-new author so it shows up as a pill on future loads.
  // Uses whatever channel + channel_type the user has set in the form right
  // now — those become the seed defaults attached to this author.
  async function saveNewAuthor() {
    const name = (author || "").trim();
    if (!name) return;
    try {
      const r = await fetch("/api/manual/authors", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          display_name: name,
          channel: channel || null,
          channel_type: channelType || null,
        }),
      });
      if (!r.ok) {
        setSubmitMsg({ type: "err", text: `Save author failed: ${r.status}` });
        return;
      }
      const created = await r.json();
      setSubmitMsg({
        type: "ok",
        text: `Saved "${created.display_name}" as a known source.`,
      });
      setUseFreeformAuthor(false);
      refreshAuthors();
    } catch (e) {
      setSubmitMsg({ type: "err", text: `Save author error: ${String(e).slice(0,100)}` });
    }
  }

  return (
    <div className="inbox-view">
      <section className="block">
        <header className="block-head">
          <div className="block-title">
            <span className="block-num mono">I1</span>
            <span>Manual input</span>
            <span className="block-sub">pick source · drop chart · attribute</span>
          </div>
        </header>

        {/* Source picker — FIRST action above everything else. One click
            sets author + channel + channel_type from the seeded list.
            "+ new author" reveals the inline create-and-save flow. */}
        <div className="inbox-source-bar">
          <label className="inbox-label" style={{ marginTop: 0 }}>Source · pick first</label>
          <div className="filter-pill-row">
            {/* Hide synthetic/archive authors from the live picker — they
                exist for attribution joins, not for new-drop selection. */}
            {authors.filter(a => a.channel !== "archive").map(a => {
              const breadcrumb = [a.channel, a.parent_channel].filter(Boolean).join(" · ");
              return (
                <button
                  key={a.author_id}
                  className={`filter-pill ${author === a.display_name ? "on" : ""}`}
                  title={`${breadcrumb || "—"} · ${a.submission_count || 0} drops`}
                  onClick={() => pickAuthor(a)}
                >
                  {a.display_name}
                </button>
              );
            })}
            <button
              className={`filter-pill ${useFreeformAuthor ? "on" : ""}`}
              onClick={() => setUseFreeformAuthor(v => !v)}
            >
              {useFreeformAuthor ? "✕ new author" : "+ new author"}
            </button>
          </div>
          {useFreeformAuthor && (
            <div style={{ marginTop: 8 }}>
              <input
                className="inbox-input"
                placeholder="new author name (e.g. CryptoCred)"
                value={author}
                onChange={(e) => setAuthor(e.target.value)}
              />
              <div className="inbox-actions" style={{ marginTop: 6 }}>
                <input
                  className="inbox-input"
                  placeholder="group / channel (optional)"
                  value={channel}
                  onChange={(e) => setChannel(e.target.value)}
                  style={{ flex: 1, minWidth: 200 }}
                />
                <button
                  className="filter-pill on"
                  disabled={!author.trim()}
                  onClick={saveNewAuthor}
                >save as known source</button>
              </div>
            </div>
          )}
          {(author || channel) && !useFreeformAuthor && (() => {
            // Find parent_channel for the currently-selected author so the
            // status line reads as "person · channel · community · type".
            const a = authors.find(x => x.display_name === author);
            const parent = a?.parent_channel;
            return (
              <div className="inbox-source-status mono">
                <strong>{author || "?"}</strong>
                {channel ? <span className="dim"> · {channel}</span> : null}
                {parent ? <span className="dim"> · {parent}</span> : null}
                {channelType ? <span className="dim"> · {channelType}</span> : null}
              </div>
            );
          })()}
        </div>

        <div className="inbox-grid">
          {/* Left: image + text */}
          <div className="inbox-col">
            <div
              ref={dropRef}
              className={`inbox-drop ${images.length ? "has-file" : ""}`}
              onDrop={onDrop}
              onDragOver={onDragOver}
            >
              {images.length > 0 ? (
                <div className="inbox-drop-preview">
                  <div className="inbox-thumbs">
                    {images.map((it, idx) => (
                      <div
                        key={it.url}
                        className="inbox-thumb"
                        title="click to view full size"
                        onClick={() => setLightboxUrl(it.url)}
                      >
                        <img src={it.url} alt={`chart ${idx + 1}`} />
                        <button
                          className="inbox-thumb-x"
                          title="remove image"
                          onClick={(e) => { e.stopPropagation(); removeImage(idx); }}
                        >×</button>
                      </div>
                    ))}
                  </div>
                  <div className="inbox-drop-sub">
                    {images.length} image{images.length === 1 ? "" : "s"} attached ·
                    drop / paste / pick more
                  </div>
                  <div className="inbox-actions" style={{ marginTop: 4 }}>
                    <input type="file" accept="image/*" multiple onChange={(e) => {
                      if (e.target.files?.length) attachFiles(e.target.files);
                      e.target.value = "";
                    }} />
                    <button className="filter-pill" onClick={clearImages}>clear all</button>
                  </div>
                </div>
              ) : (
                <div className="inbox-drop-empty">
                  <div className="inbox-drop-headline">Drop chart screenshots here</div>
                  <div className="inbox-drop-sub">multiple images OK · paste from clipboard · or pick files</div>
                  <input type="file" accept="image/*" multiple onChange={(e) => {
                    if (e.target.files?.length) attachFiles(e.target.files);
                    e.target.value = "";
                  }} />
                </div>
              )}
            </div>

            <label className="inbox-label">Body / blurb</label>
            <textarea
              className="inbox-textarea"
              rows={6}
              placeholder={"Paste from Telegram/Discord/notes. Mention tickers ($BTC, $XRP). The pre-tagger and mention extractor read this."}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />

            <div className="inbox-actions">
              <button className="filter-pill on" onClick={runPreview} disabled={submitting}>analyze</button>
              <button className="filter-pill" onClick={submit} disabled={submitting}>
                {submitting ? (analyzeOnSave && images.length ? "analyzing…" : "saving…") : "save drop"}
              </button>
              {/* Inline-vision toggle. ON = save blocks until Claude finishes
                  (~20s/image) and result expands inline. OFF = fast save,
                  user clicks "analyze pending" later. */}
              <label className="inbox-msg" style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11 }}>
                <input
                  type="checkbox"
                  checked={analyzeOnSave}
                  onChange={(e) => setAnalyzeOnSave(e.target.checked)}
                />
                analyze on save
              </label>
              {/* My-chart toggle. ON = signal extraction runs inline so
                  the confirmation panel shows what was extracted. OFF =
                  chart batched in next morning_run. Auto-fills self
                  attribution. */}
              <label className="inbox-msg" style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11 }}>
                <input
                  type="checkbox"
                  checked={isMyChart}
                  onChange={(e) => {
                    const on = e.target.checked;
                    setIsMyChart(on);
                    if (on) {
                      // Auto-fill self attribution so the operator
                      // doesn't double-type. Seeded picklist has Me/self.
                      if (!author || author === "") setAuthor("Me");
                      if (!channel || channel === "") setChannel("self");
                      if (!channelType || channelType === "") setChannelType("self");
                    }
                  }}
                />
                my chart (extract now)
              </label>
              {submitMsg && (
                <span className={`inbox-msg ${submitMsg.type}`}>{submitMsg.text}</span>
              )}
            </div>

            {/* Inline extraction result — populated only on self-authored drops. */}
            {(lastSignals.length > 0 || lastInlineErr) && (
              <div className="inbox-preview-card" style={{ borderColor: "#4a4" }}>
                <div className="inbox-preview-row">
                  <span className="lbl">extracted signals</span>
                  <span>{lastSignals.length} from inline extraction</span>
                </div>
                {lastSignals.map((s, i) => (
                  <div key={s.signal_id || i} className="inbox-preview-row"
                       style={{ borderTop: "1px dashed #333", paddingTop: 4 }}>
                    <span className="lbl">
                      {s.asset_ticker} · {s.side}
                      {typeof s.conviction === "number" ? ` · conv ${s.conviction.toFixed(1)}` : ""}
                    </span>
                    <span style={{ fontSize: 11 }}>
                      {[
                        s.extractor_name,
                        s.horizon,
                        s.catalyst_type,
                        s.stop_loss ? `stop ${s.stop_loss}` : null,
                        s.target_1 ? `tgt ${s.target_1}` : null,
                        typeof s.cost_usd === "number" ? `$${s.cost_usd.toFixed(4)}` : null,
                      ].filter(Boolean).join(" · ")}
                      {s.thesis_summary ? (
                        <span className="dim" style={{ display: "block" }}>
                          {String(s.thesis_summary).slice(0, 180)}
                        </span>
                      ) : null}
                    </span>
                  </div>
                ))}
                {lastInlineErr && (
                  <div className="inbox-preview-row">
                    <span className="lbl">inline error</span>
                    <span style={{ color: "#c66", fontSize: 11 }}>{String(lastInlineErr).slice(0, 200)}</span>
                  </div>
                )}
              </div>
            )}

            {previewData && (
              <div className="inbox-preview-card">
                <div className="inbox-preview-row">
                  <span className="lbl">tickers</span>
                  <span>{previewData.detected_tickers?.length ? previewData.detected_tickers.join(", ") : "—"}</span>
                </div>
                <div className="inbox-preview-row">
                  <span className="lbl">tags</span>
                  <span>{previewData.suggested_tags?.join(", ") || "—"}</span>
                </div>
                <div className="inbox-preview-row">
                  <span className="lbl">routes to</span>
                  <span>{previewData.suggested_agents?.join(", ") || "—"}</span>
                </div>
                <div className="inbox-preview-row">
                  <span className="lbl">author known?</span>
                  <span>{previewData.suggested_author_id || "new"}</span>
                </div>
              </div>
            )}
          </div>

          {/* Right: metadata (per-drop fields only — source is set above) */}
          <div className="inbox-col">
            <label className="inbox-label">Ticker</label>
            <input
              className="inbox-input"
              placeholder='auto-detected from blurb · override here'
              value={ticker}
              onChange={(e) => setTicker(e.target.value.toUpperCase())}
            />

            <label className="inbox-label">Side</label>
            <div className="filter-pill-row">
              {SIDES.map(s => (
                <button key={s} className={`filter-pill ${side === s ? "on" : ""}`}
                        onClick={() => setSide(s)}>{s}</button>
              ))}
            </div>

            <label className="inbox-label">Conviction · {conviction}/5</label>
            <input
              type="range" min="1" max="5" step="1"
              value={conviction}
              onChange={(e) => setConviction(e.target.value)}
              className="inbox-range"
            />

            <label className="inbox-label">Timeframe</label>
            <div className="filter-pill-row">
              {TIMEFRAMES.map(t => (
                <button key={t} className={`filter-pill ${timeframe === t ? "on" : ""}`}
                        onClick={() => setTimeframe(t)}>{t}</button>
              ))}
            </div>

            <label className="inbox-label">One-line note (optional)</label>
            <input
              className="inbox-input"
              placeholder="e.g. wedge break, retest of POC, etc."
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />

            {/* Picker moved up to the top of the column (under "Author /
                source"). The bottom-of-form pill row was redundant. */}
          </div>
        </div>
      </section>

      {/* History */}
      <section className="block">
        <header className="block-head">
          <div className="block-title">
            <span className="block-num mono">I2</span>
            <span>Recent drops</span>
            <span className="block-sub">last 20 · text + chart submissions</span>
          </div>
          <div className="block-actions">
            <button
              className="filter-pill"
              onClick={refreshHistory}
              disabled={refreshState === "loading"}
            >
              {refreshState === "loading" ? "refreshing…" :
               refreshState === "ok" ? `refresh (${history.length} loaded)` :
               refreshState === "error" ? "refresh ✗" : "refresh"}
            </button>
            <button
              className="filter-pill"
              title="Run Claude vision on any pending_vision drops"
              onClick={drainPending}
              disabled={draining}
            >{draining ? "analyzing…" : "analyze pending"}</button>
          </div>
        </header>
        {history.length === 0 ? (
          <div className="inbox-empty">No drops yet. Add one above.</div>
        ) : (
          <table className="inbox-history">
            <thead>
              <tr>
                <th>when</th>
                <th>type</th>
                <th>ticker · side</th>
                <th>author</th>
                <th>tags</th>
                <th>vision</th>
              </tr>
            </thead>
            <tbody>
              {history.map(h => {
                const meta = h.user_metadata?.resolved || {};
                const tagsObj = h.tags || {};
                const tags = Array.isArray(tagsObj.tags) ? tagsObj.tags : [];
                const pending = !!tagsObj.pending_vision;
                const features = h.extracted_features;
                const hasAnalysis = features && !features.error;
                const isExpanded = expandedDoc === h.document_id;
                return (
                  <React.Fragment key={h.document_id}>
                    <tr
                      style={{ cursor: hasAnalysis ? "pointer" : "default" }}
                      onClick={() => hasAnalysis && setExpandedDoc(isExpanded ? null : h.document_id)}
                      title={hasAnalysis ? "click to view full extracted analysis" : undefined}
                    >
                      <td className="mono dim" title={`imported: ${(h.ingested_at || "").slice(0, 19).replace("T", " ")}`}>
                        {/* WHEN = chart's TradingView date (when the analysis
                            was actually made) — the real time-weighting axis.
                            Falls back to ingested_at for non-chart drops. */}
                        {(() => {
                          const pub = (h.published_at || "").slice(0, 10);
                          const ing = (h.ingested_at || "").slice(0, 10);
                          // If published_at differs meaningfully from ingestion,
                          // show it as the headline date. Otherwise just show
                          // ingestion (likely a same-day note with no chart).
                          if (pub && pub !== ing) {
                            // Render relative age too so the time axis reads naturally
                            const days = Math.floor(
                              (Date.now() - new Date(h.published_at).getTime()) / 86_400_000
                            );
                            const rel = days <= 0 ? "today"
                                      : days === 1 ? "yesterday"
                                      : days < 7 ? `${days}d ago`
                                      : days < 30 ? `${Math.floor(days/7)}w ago`
                                      : days < 365 ? `${Math.floor(days/30)}mo ago`
                                      : `${Math.floor(days/365)}y ago`;
                            return (
                              <>
                                <div>{pub}</div>
                                <div style={{ fontSize: 10, opacity: 0.6 }}>{rel}</div>
                              </>
                            );
                          }
                          return (h.ingested_at || "").slice(0, 19).replace("T", " ");
                        })()}
                      </td>
                      <td>
                        {h.content_type === "manual_chart" ? "chart" : "note"}
                        {Array.isArray(h.attachment_paths) && h.attachment_paths.length > 1 ? (
                          <span className="dim mono"> ·{h.attachment_paths.length}</span>
                        ) : null}
                      </td>
                      <td>
                        {/* Ticker fallback order: user-typed → Claude top-level
                            → Claude setups[0] → first non-null in setups[]
                            → em-dash. Some schema variants nest ticker
                            inside setups so the flat lookup misses. */}
                        {(() => {
                          const fromSetups = features?.setups?.find?.(s => s?.ticker)?.ticker;
                          const t = meta.ticker || features?.ticker || features?.asset || features?.instrument || fromSetups;
                          const sideRaw = meta.side || features?.direction || features?.trade_direction
                                       || features?.setups?.find?.(s => s?.side || s?.direction)?.side
                                       || features?.setups?.find?.(s => s?.side || s?.direction)?.direction;
                          return (
                            <>
                              <strong>{t || "—"}</strong>
                              {sideRaw ? (
                                <span className="dim mono"> · {String(sideRaw).toUpperCase()}</span>
                              ) : null}
                            </>
                          );
                        })()}
                      </td>
                      <td>
                        {/* Proper breadcrumb: person · channel · community.
                            Falls back to the raw slug if metadata is missing. */}
                        {(() => {
                          const person = h.author || h.author_id?.split(":").pop() || "—";
                          const ch = h.user_metadata?.channel;
                          const parent = h.user_metadata?.parent_channel;
                          return (
                            <>
                              <strong>{person}</strong>
                              {ch && <span className="dim"> · {ch}</span>}
                              {parent && <span className="dim"> · {parent}</span>}
                            </>
                          );
                        })()}
                      </td>
                      <td className="mono dim">{tags.join(", ")}</td>
                      <td>
                        {pending ? <span className="badge-pending">pending</span>
                          : hasAnalysis ? <span style={{ color: "#6dd28a" }}>{isExpanded ? "▼ collapse" : "▶ details"}</span>
                          : <span className="dim">—</span>}
                      </td>
                    </tr>
                    {/* Inline summary row was redundant with the expanded
                        Author/Vision sections — removed. The single
                        ▶/▼ glyph in the VISION column is the only affordance. */}
                    {isExpanded && hasAnalysis && (
                      <tr>
                        <td colSpan={6} style={{ background: "rgba(255,255,255,0.02)", padding: "16px 20px" }}>
                          <ChartDropDetail
                            historyRow={h}
                            attachmentPaths={h.attachment_paths || []}
                            onJumpToAsset={jumpToAsset}
                          />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      {/* I3 Trusted-source themes panel has MOVED to the /streams page
          (S6) — it's a cross-source conviction view, not a manual-input
          action. Defined below and exposed on window for streams.jsx. */}

      {/* Full-size image viewer — opens when a thumb is clicked. */}
      {lightboxUrl && (
        <div className="inbox-lightbox" onClick={() => setLightboxUrl(null)}>
          <img src={lightboxUrl} alt="full chart" />
        </div>
      )}
    </div>
  );
}

// ── Trusted-source themes panel ──
//
// Layout: sub-tab bar of authors (one pill per source, ranked by trust), and
// below it a dedicated per-source page. The per-source page leads with the
// author's conviction picks, then a chart-first gallery grouped by ticker so
// setups are scannable at a glance.
function TrustedSourceThemes() {
  const [data, setData] = useIS(null);
  const [loading, setLoading] = useIS(true);
  const [windowDays, setWindowDays] = useIS(90);
  const [accuracy, setAccuracy] = useIS({});
  const [activeId, setActiveId] = useIS(null);
  const [sortBy, setSortBy] = useIS("trust"); // trust | drops | analyzed | name

  useIE(() => {
    let cancelled = false;
    setLoading(true);
    fetch(`/api/manual/themes/trusted?window_days=${windowDays}`)
      .then(r => r.json())
      .then(d => { if (!cancelled) { setData(d); setLoading(false); } })
      .catch(() => { if (!cancelled) setLoading(false); });
    fetch(`/api/manual/accuracy/sources?window_days=${windowDays}`)
      .then(r => r.json())
      .then(d => {
        if (cancelled) return;
        const map = {};
        (d.sources || []).forEach(s => { map[s.author_id] = s; });
        setAccuracy(map);
      })
      .catch(() => {});
    function onReassign() {
      fetch(`/api/manual/themes/trusted?window_days=${windowDays}`)
        .then(r => r.json()).then(setData);
    }
    window.addEventListener("macro:author-reassigned", onReassign);
    return () => { cancelled = true; window.removeEventListener("macro:author-reassigned", onReassign); };
  }, [windowDays]);

  const rawAuthors = (data && data.authors) || [];
  const authors = React.useMemo(() => {
    const list = rawAuthors.slice();
    list.sort((a, b) => {
      if (sortBy === "name")     return String(a.display_name).localeCompare(String(b.display_name));
      if (sortBy === "drops")    return (b.n_drops || 0) - (a.n_drops || 0);
      if (sortBy === "analyzed") return (b.n_with_vision || 0) - (a.n_with_vision || 0);
      return (b.trust_weight || 0) - (a.trust_weight || 0); // default: trust
    });
    return list;
  }, [rawAuthors, sortBy]);
  const active = authors.find(a => a.author_id === activeId) || authors[0] || null;

  return (
    <section className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">I3</span>
          <span>Trusted sources</span>
          <span className="block-sub">
            per-source drilldown · conviction picks + chart gallery over the last {windowDays}d
          </span>
        </div>
        <div className="block-actions">
          <div className="filter-pill-row">
            {[14, 30, 90, 180].map(d => (
              <button key={d}
                className={`filter-pill ${windowDays === d ? "on" : ""}`}
                onClick={() => setWindowDays(d)}>{d}d</button>
            ))}
          </div>
        </div>
      </header>
      {loading ? (
        <div className="inbox-empty">Loading…</div>
      ) : authors.length === 0 ? (
        <div className="inbox-empty">No analyzed drops yet from trusted sources in this window.</div>
      ) : (
        <>
          <div className="ts-author-picker">
            <label className="ts-picker-lbl mono small muted">Source</label>
            <select
              className="ts-picker-select"
              value={active?.author_id || ""}
              onChange={(e) => setActiveId(e.target.value)}>
              {authors.map(a => (
                <option key={a.author_id} value={a.author_id}>
                  {a.display_name}
                  {a.channel ? ` · ${a.channel}` : ""}
                  {"  —  "}
                  {a.trust_weight.toFixed(1)}x trust · {a.n_with_vision}/{a.n_charts ?? a.n_drops} charts · {a.n_drops} drops
                </option>
              ))}
            </select>
            <span className="ts-picker-count mono small muted">
              {authors.length} source{authors.length === 1 ? "" : "s"}
            </span>
            <div className="ts-picker-sort">
              <span className="mono small muted">sort:</span>
              {[["trust","trust"],["drops","drops"],["analyzed","analyzed"],["name","A→Z"]].map(([k, lbl]) => (
                <button key={k}
                  className={`filter-pill ${sortBy === k ? "on" : ""}`}
                  onClick={() => setSortBy(k)}>{lbl}</button>
              ))}
            </div>
          </div>
          {active && (
            <TrustedAuthorDetail
              a={active}
              acc={accuracy[active.author_id]}
              windowDays={windowDays}
            />
          )}
        </>
      )}
    </section>
  );
}

// Drill-down rendered when a ticker chip is clicked — shows the drops
// behind that mention with their bias, setup, key levels, next-move.
function TickerDrillDown({ authorId, ticker, onClose }) {
  const [data, setData] = useIS(null);
  const [loading, setLoading] = useIS(true);
  useIE(() => {
    let cancelled = false;
    setLoading(true);
    const url = `/api/manual/themes/author/${encodeURIComponent(authorId)}/ticker/${encodeURIComponent(ticker)}?window_days=365`;
    fetch(url).then(r => r.json()).then(d => { if (!cancelled) { setData(d); setLoading(false); } })
      .catch(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [authorId, ticker]);

  return (
    <div className="ts-drill">
      <div className="ts-drill-head">
        <strong>{ticker}</strong>
        <span className="dim mono">
          {loading ? "loading…" : `${data?.n || 0} drop${(data?.n || 0) === 1 ? "" : "s"}`}
        </span>
        <button className="filter-pill" style={{ marginLeft: "auto", fontSize: 10 }}
                onClick={onClose}>✕ close</button>
      </div>
      {!loading && (!data?.drops || data.drops.length === 0) && (
        <div className="dim cd-text">No drops found for this ticker / author pair.</div>
      )}
      <div className="ts-drill-list">
        {(data?.drops || []).map(d => {
          const biasFirstWord = (d.bias || "").toLowerCase().split(/[\s—-]/)[0];
          const biasCls = biasFirstWord ? `bias-${biasFirstWord}` : "";
          return (
            <div key={d.document_id} className="ts-drill-row">
              {d.attachment_paths?.[0] && (
                <img
                  className="ts-drill-thumb"
                  src={`/${d.attachment_paths[0]}`}
                  alt={d.ticker}
                  onError={(e) => { e.target.style.display = "none"; }}
                />
              )}
              <div className="ts-drill-body">
                <div className="ts-drill-row-head">
                  {d.bias
                    ? <span className={`cd-badge ${biasCls}`}>● {d.bias}</span>
                    : _callTypeLabel(d.call_type) && (
                        <span className="cd-badge dim" title="non-directional — no actionable call">
                          {_callTypeLabel(d.call_type)}
                        </span>
                      )}
                  {d.timeframe && <span className="cd-tf">{d.timeframe}</span>}
                  <span className="dim mono">{(d.published_at || "").slice(0, 10)}</span>
                  {/* Freshness badge — active (green) / aging (gold) /
                      stale (dim) / unknown (dim). Drives whether the
                      setup is still actionable. */}
                  {d.decay && (
                    <span className={`ts-decay ts-decay-${d.decay.signal_status}`}
                          title={`${d.decay_label || d.decay.signal_status} · decay window ${d.decay.decay_window}d · weight ${d.decay.decay_weight}`}>
                      {d.decay_label || d.decay.signal_status}
                    </span>
                  )}
                  {d.confluence_score && (
                    <span className="ts-trust" style={{ marginLeft: "auto" }}>
                      {d.confluence_score}/5
                    </span>
                  )}
                </div>
                {_setupText(d.setup) && <div className="cd-pattern" style={{ fontSize: 14 }}>{_setupText(d.setup)}</div>}
                {_nextMoveText(d.next_move) && (
                  <div className="cd-block-text" style={{ fontSize: 12, marginTop: 4 }}>
                    <span className="cd-block-title" style={{ marginRight: 6 }}>NEXT</span>
                    {_nextMoveText(d.next_move)}
                  </div>
                )}
                {d.invalidation && (
                  <div className="cd-block-text dim" style={{ fontSize: 11 }}>
                    <span className="cd-block-title" style={{ marginRight: 6 }}>INVAL</span>
                    {typeof d.invalidation === "string" ? d.invalidation : JSON.stringify(d.invalidation)}
                  </div>
                )}
                {Array.isArray(d.key_levels) && d.key_levels.length > 0 && (
                  <div className="cd-chips" style={{ marginTop: 4 }}>
                    {d.key_levels.slice(0, 6).map((l, i) => {
                      const text = typeof l === "string" ? l : JSON.stringify(l);
                      const cls = _classifyLevel(text);
                      const { price, desc } = _stripPrice(text);
                      return (
                        <span key={i} className={`cd-chip cd-chip-${cls}`}>
                          {price && <span className="cd-chip-price mono">{price}</span>}
                          <span className="cd-chip-desc">{(desc || "").slice(0, 50)}</span>
                        </span>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// Per-author dedicated page: header stats + conviction pick chips (jump to
// section), then a chart gallery grouped by ticker. Loads drops for every
// distinct ticker in this author's conviction picks + top-mentions list in
// parallel, so the whole gallery renders once fetches settle.
function TrustedAuthorDetail({ a, acc, windowDays }) {
  const totalBias = Object.values(a.bias_distribution || {}).reduce((s, n) => s + n, 0);
  const pct = (k) => totalBias ? Math.round((a.bias_distribution[k] || 0) / totalBias * 100) : 0;

  const realPicks = (a.high_conviction_tickers || []).filter(hc => hc.ticker !== "UNKNOWN");
  const realTopTickers = (a.top_tickers || []).filter(([t]) => t !== "UNKNOWN");

  // Union of tickers to render as gallery sections, ordered by conviction
  // then mention count. Cap for speed — 12 sections is plenty per author.
  const galleryTickers = React.useMemo(() => {
    const seen = new Set();
    const order = [];
    const meta = {};
    for (const hc of realPicks) {
      if (seen.has(hc.ticker)) continue;
      seen.add(hc.ticker); order.push(hc.ticker);
      meta[hc.ticker] = { conv: Math.round(hc.conviction_score || 0), bias: hc.bias, mentions: hc.mentions };
    }
    for (const [t, c] of realTopTickers) {
      if (seen.has(t)) { meta[t].mentions = Math.max(meta[t].mentions || 0, c); continue; }
      seen.add(t); order.push(t);
      meta[t] = { conv: null, bias: "neutral", mentions: c };
    }
    return { order: order.slice(0, 12), meta };
  }, [a.author_id, a.high_conviction_tickers, a.top_tickers]);

  // dropsByTicker: { TICKER: {loading, drops:[]} }
  const [dropsByTicker, setDropsByTicker] = useIS({});
  const [dropDetail, setDropDetail] = useIS(null); // full-view drop for modal
  // Which section is expanded. Collapsed by default so the gallery reads as
  // a scannable table of contents (ticker + freshness + sentiment per row).
  const [expanded, setExpanded] = useIS(() => new Set());

  useIE(() => {
    // Reset when author or window changes.
    setDropsByTicker({});
    setExpanded(new Set());
    let cancelled = false;
    galleryTickers.order.forEach(t => {
      const url = `/api/manual/themes/author/${encodeURIComponent(a.author_id)}/ticker/${encodeURIComponent(t)}?window_days=${windowDays}`;
      fetch(url).then(r => r.json()).then(d => {
        if (cancelled) return;
        setDropsByTicker(prev => ({ ...prev, [t]: { loading: false, drops: d.drops || [] } }));
      }).catch(() => {
        if (cancelled) return;
        setDropsByTicker(prev => ({ ...prev, [t]: { loading: false, drops: [] } }));
      });
    });
    return () => { cancelled = true; };
  }, [a.author_id, windowDays, galleryTickers.order.join(",")]);

  function toggleExpand(t) {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t); else next.add(t);
      return next;
    });
  }

  function jumpTo(t) {
    setExpanded(prev => {
      if (prev.has(t)) return prev;
      const next = new Set(prev); next.add(t); return next;
    });
    // Wait a tick for the section to expand before scrolling.
    setTimeout(() => {
      const el = document.getElementById(`ts-gallery-${a.author_id}-${t}`);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 40);
  }

  function expandAll() { setExpanded(new Set(galleryTickers.order)); }
  function collapseAll() { setExpanded(new Set()); }

  return (
    <div className="ts-detail">
      {/* ── Author header ───────────────────────────────────────────── */}
      <div className="ts-detail-head">
        <div className="ts-detail-title">
          <strong>{a.display_name}</strong>
          {a.channel && <span className="dim"> · {a.channel}</span>}
          {a.parent_channel && a.parent_channel !== a.channel && (
            <span className="dim"> · {a.parent_channel}</span>
          )}
          {a.category && <span className="ts-cat">{a.category.replace("_", " ")}</span>}
        </div>
        <div className="ts-detail-meta dim mono">
          {a.n_with_vision}/{a.n_charts ?? a.n_drops} charts analyzed · {a.n_drops} drops
          {a.earliest_chart && a.latest_chart && (
            <span> · {a.earliest_chart} → {a.latest_chart}</span>
          )}
        </div>
        {totalBias > 0 && (
          <div className="ts-bias-bar"
               title={`bullish ${pct("bullish")}% · neutral ${pct("neutral")}% · bearish ${pct("bearish")}%`}>
            <div className="ts-bias-seg ts-bias-bull" style={{ width: `${pct("bullish")}%` }} />
            <div className="ts-bias-seg ts-bias-neut" style={{ width: `${pct("neutral")}%` }} />
            <div className="ts-bias-seg ts-bias-bear" style={{ width: `${pct("bearish")}%` }} />
          </div>
        )}
      </div>

      {/* ── Conviction picks · click to jump ────────────────────────── */}
      {realPicks.length > 0 && (
        <div className="ts-section">
          <div className="ts-section-title">Conviction picks · click to jump to charts</div>
          <div className="ts-chips">
            {realPicks.slice(0, 16).map(hc => {
              const allStale = hc.active_mentions === 0 && hc.mentions > 0;
              const conv = Math.round(hc.conviction_score || 0);
              const convClass = conv >= 75 ? "ts-conv-strong"
                              : conv >= 50 ? "ts-conv-mid"
                              : "ts-conv-soft";
              const patternStrong = (hc.pattern_strength_avg || 0) >= 75;
              const tt = [
                `conviction ${conv}/100`,
                `${hc.agreement_pct}% bias agreement`,
                hc.confluence_avg != null ? `Claude confluence avg ${hc.confluence_avg}/5` : null,
                hc.n_timeframes ? `${hc.n_timeframes} timeframe${hc.n_timeframes === 1 ? "" : "s"}` : null,
                `${hc.mentions} total mentions · ${hc.active_mentions} active / ${hc.stale_mentions} stale`,
              ].filter(Boolean).join(" · ");
              return (
                <button key={hc.ticker}
                  className={`ts-chip ts-chip-${hc.bias} ts-chip-button ${convClass} ${expanded.has(hc.ticker) ? "on" : ""} ${allStale ? "ts-chip-faded" : ""}`}
                  title={tt}
                  onClick={() => jumpTo(hc.ticker)}>
                  <strong>{hc.ticker}</strong>
                  <span className="ts-conv-score mono">{conv}</span>
                  {hc.n_timeframes > 1 && (
                    <span className="ts-tf-badge mono">{hc.n_timeframes}TF</span>
                  )}
                  {patternStrong && <span className="ts-pattern-badge mono">◆</span>}
                </button>
              );
            })}
          </div>
          <div className="ts-conv-legend dim mono">
            conviction = 30% confluence + 25% pattern + 20% TF coverage + 15% persistence + 10% freshness · ◆ = high-conviction pattern
          </div>
        </div>
      )}

      {a.top_setups && a.top_setups.length > 0 && (
        <div className="ts-section">
          <div className="ts-section-title">Recurring setups</div>
          <ul className="ts-setup-list">
            {a.top_setups.slice(0, 5).map(([s, c]) => (
              <li key={s}><span className="dim mono">×{c}</span> {s}</li>
            ))}
          </ul>
        </div>
      )}

      {/* ── Chart gallery grouped by ticker ─────────────────────────── */}
      <div className="ts-section">
        <div className="ts-gallery-toolbar">
          <div className="ts-section-title" style={{ marginRight: "auto" }}>
            Chart gallery · click a row to expand · click a chart for full detail
          </div>
          <button className="filter-pill" onClick={expandAll}>expand all</button>
          <button className="filter-pill" onClick={collapseAll}>collapse all</button>
        </div>
        {galleryTickers.order.length === 0 && (
          <div className="dim cd-text">No mentioned tickers in this window.</div>
        )}
        {galleryTickers.order.map(t => {
          const m = galleryTickers.meta[t];
          const bucket = dropsByTicker[t];
          const drops = bucket ? bucket.drops : null;
          const isOpen = expanded.has(t);
          const summary = _summarizeDrops(drops);
          return (
            <div key={t} id={`ts-gallery-${a.author_id}-${t}`}
                 className={`ts-gallery-section ${isOpen ? "on" : ""}`}>
              <button className="ts-gallery-head" onClick={() => toggleExpand(t)}
                      aria-expanded={isOpen}>
                <span className="ts-gallery-caret mono">{isOpen ? "▾" : "▸"}</span>
                <strong className="ts-gallery-ticker">{t}</strong>
                {m.conv != null && (
                  <span className={`ts-gallery-conv mono ${m.conv >= 75 ? "ts-conv-strong" : m.conv >= 50 ? "ts-conv-mid" : "ts-conv-soft"}`}>
                    <span className="ts-conv-score mono">{m.conv}</span>
                  </span>
                )}
                <span className="dim mono ts-gallery-count">
                  {drops == null ? "loading…" : `${drops.length} drop${drops.length === 1 ? "" : "s"}`}
                </span>
                {summary && (
                  <>
                    {summary.latest && (
                      <span className={`ts-freshness ts-decay-${summary.freshness}`}
                            title={`${summary.freshness} · latest drop ${summary.ageLabel}`}>
                        {summary.freshness.toUpperCase()} · {summary.ageLabel}
                      </span>
                    )}
                    {summary.sentimentLabel && (
                      <span className={`ts-sentiment ts-sent-${summary.dominant}`}
                            title={`${summary.bullPct}% bull · ${summary.neutPct}% neutral · ${summary.bearPct}% bear`}>
                        {summary.sentimentLabel}
                      </span>
                    )}
                    {summary.hasSentiment && (
                      <span className="ts-bias-bar ts-bias-bar-mini"
                            title={`${summary.bullPct}% bull · ${summary.neutPct}% neutral · ${summary.bearPct}% bear`}>
                        <span className="ts-bias-seg ts-bias-bull" style={{ width: `${summary.bullPct}%` }} />
                        <span className="ts-bias-seg ts-bias-neut" style={{ width: `${summary.neutPct}%` }} />
                        <span className="ts-bias-seg ts-bias-bear" style={{ width: `${summary.bearPct}%` }} />
                      </span>
                    )}
                  </>
                )}
              </button>
              {isOpen && (
                drops == null ? (
                  <div className="ts-gallery-loading dim mono">Loading charts…</div>
                ) : drops.length === 0 ? (
                  <div className="dim cd-text">No drops in this window.</div>
                ) : (
                  <div className="ts-gallery-grid">
                    {drops.map(d => <ChartGalleryCard key={d.document_id} d={d} onOpen={() => setDropDetail(d)} />)}
                  </div>
                )
              )}
            </div>
          );
        })}
      </div>

      {dropDetail && (
        <DropDetailModal d={dropDetail} onClose={() => setDropDetail(null)} />
      )}
    </div>
  );
}

// Big chart card in the gallery — chart-first, with bias/tf/date and a
// one-line setup. Click opens the full-detail modal.
function _setupText(s) {
  if (!s) return "";
  if (typeof s === "string") return s;
  if (typeof s === "object") return s.name || s.pattern || s.setup || JSON.stringify(s);
  return String(s);
}

// next_move / invalidation can arrive as objects (LLM extractor v2 shape):
// {direction, target, secondary_target, timeframe_context, notes}. Render as
// a readable one-liner. Falls through to string / JSON as needed.
function _nextMoveText(nm) {
  if (!nm) return "";
  if (typeof nm === "string") return nm;
  if (typeof nm === "object") {
    if (nm.notes) return nm.notes;
    const parts = [];
    if (nm.direction) parts.push(nm.direction);
    if (nm.target) parts.push(`→ ${nm.target}`);
    if (nm.secondary_target) parts.push(`(then ${nm.secondary_target})`);
    if (nm.timeframe_context) parts.push(`· ${nm.timeframe_context}`);
    if (parts.length) return parts.join(" ");
    return JSON.stringify(nm);
  }
  return String(nm);
}

function ChartGalleryCard({ d, onOpen }) {
  const biasFirstWord = (d.bias || "").toLowerCase().split(/[\s—-]/)[0];
  const biasCls = biasFirstWord ? `bias-${biasFirstWord}` : "";
  const img = d.attachment_paths?.[0];
  const setupText = _setupText(d.setup);
  const tf = typeof d.timeframe === "string" ? d.timeframe : "";
  const invalText = d.invalidation
    ? (typeof d.invalidation === "string" ? d.invalidation : JSON.stringify(d.invalidation))
    : "";
  return (
    <div className="ts-gallery-card">
      {img ? (
        <img className="ts-gallery-img" src={`/${img}`} alt={d.ticker}
             loading="lazy"
             onClick={onOpen}
             title="click to open full-size"
             onError={(e) => { e.target.style.display = "none"; }} />
      ) : (
        <div className="ts-gallery-img ts-gallery-img-missing dim mono">no chart</div>
      )}
      <div className="ts-gallery-card-body">
        <div className="ts-gallery-card-row">
          {d.bias && <span className={`cd-badge ${biasCls}`}>● {d.bias}</span>}
          {tf && <span className="cd-tf">{tf}</span>}
          <span className="dim mono">{(d.published_at || "").slice(0, 10)}</span>
          {d.decay && (
            <span className={`ts-decay ts-decay-${d.decay.signal_status}`}>
              {d.decay_label || d.decay.signal_status}
            </span>
          )}
          {d.confluence_score && (
            <span className="ts-trust" style={{ marginLeft: "auto" }}>{d.confluence_score}/5</span>
          )}
        </div>
        {setupText && <div className="ts-gallery-setup">{setupText}</div>}
        {_nextMoveText(d.next_move) && (
          <div className="cd-block-text" style={{ fontSize: 12 }}>
            <span className="cd-block-title" style={{ marginRight: 6 }}>NEXT</span>
            {_nextMoveText(d.next_move)}
          </div>
        )}
        {invalText && (
          <div className="cd-block-text dim" style={{ fontSize: 11 }}>
            <span className="cd-block-title" style={{ marginRight: 6 }}>INVAL</span>
            {invalText}
          </div>
        )}
        {Array.isArray(d.key_levels) && d.key_levels.length > 0 && (
          <div className="cd-chips" style={{ marginTop: 4 }}>
            {d.key_levels.slice(0, 6).map((l, i) => {
              const cls = _levelClassify(l);
              const { price, desc } = _levelToChip(l);
              return (
                <span key={i} className={`cd-chip cd-chip-${cls}`}>
                  {price && <span className="cd-chip-price mono">{price}</span>}
                  <span className="cd-chip-desc">{(desc || "").slice(0, 60)}</span>
                </span>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

// Full-detail modal for a single drop — reuses the existing drill-row body
// layout, just floated over the page.
function DropDetailModal({ d, onClose }) {
  useIE(() => {
    function onKey(e) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const biasFirstWord = (d.bias || "").toLowerCase().split(/[\s—-]/)[0];
  const biasCls = biasFirstWord ? `bias-${biasFirstWord}` : "";
  const img = d.attachment_paths?.[0];
  return (
    <div className="ts-modal-backdrop" onClick={onClose}>
      <div className="ts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="ts-modal-head">
          <strong>{d.ticker}</strong>
          <span className="dim mono">
            {(d.n_charts || 0) || 1} drop
          </span>
          <button className="filter-pill" style={{ marginLeft: "auto", fontSize: 10 }}
                  onClick={onClose}>✕ close</button>
        </div>
        <div className="ts-modal-body">
          {img && (
            <img className="ts-modal-img" src={`/${img}`} alt={d.ticker}
                 onError={(e) => { e.target.style.display = "none"; }} />
          )}
          <div className="ts-modal-text">
            <div className="ts-drill-row-head">
              {d.bias
                ? <span className={`cd-badge ${biasCls}`}>● {d.bias}</span>
                : _callTypeLabel(d.call_type) && (
                    <span className="cd-badge dim" title="non-directional — no actionable call">
                      {_callTypeLabel(d.call_type)}
                    </span>
                  )}
              {typeof d.timeframe === "string" && d.timeframe && <span className="cd-tf">{d.timeframe}</span>}
              <span className="dim mono">{(d.published_at || "").slice(0, 10)}</span>
              {d.decay && (
                <span className={`ts-decay ts-decay-${d.decay.signal_status}`}>
                  {d.decay_label || d.decay.signal_status}
                </span>
              )}
              {d.confluence_score && (
                <span className="ts-trust" style={{ marginLeft: "auto" }}>{d.confluence_score}/5</span>
              )}
            </div>
            {_setupText(d.setup) && <div className="cd-pattern" style={{ fontSize: 16 }}>{_setupText(d.setup)}</div>}
            {_nextMoveText(d.next_move) && (
              <div className="cd-block-text" style={{ fontSize: 13, marginTop: 6 }}>
                <span className="cd-block-title" style={{ marginRight: 6 }}>NEXT</span>
                {_nextMoveText(d.next_move)}
              </div>
            )}
            {d.invalidation && (
              <div className="cd-block-text dim" style={{ fontSize: 12 }}>
                <span className="cd-block-title" style={{ marginRight: 6 }}>INVAL</span>
                {typeof d.invalidation === "string" ? d.invalidation : JSON.stringify(d.invalidation)}
              </div>
            )}
            {Array.isArray(d.key_levels) && d.key_levels.length > 0 && (
              <div className="cd-chips" style={{ marginTop: 8 }}>
                {d.key_levels.slice(0, 8).map((l, i) => {
                  const cls = _levelClassify(l);
                  const { price, desc } = _levelToChip(l);
                  return (
                    <span key={i} className={`cd-chip cd-chip-${cls}`}>
                      {price && <span className="cd-chip-price mono">{price}</span>}
                      <span className="cd-chip-desc">{(desc || "").slice(0, 80)}</span>
                    </span>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// TrustedSourceThemes/TrustedAuthorDetail/TickerDrillDown render the I3
// conviction panel — mounted on the /streams page (S6). Exposed on window
// so streams.jsx can pick them up.
Object.assign(window, {
  Inbox, TrustedSourceThemes,
  TrustedAuthorDetail, ChartGalleryCard, DropDetailModal,
  TickerDrillDown,
});
