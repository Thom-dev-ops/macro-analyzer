// /concepts — step ② of the funnel.
//
// Three stacked sections: suggested (system-proposed promotions from
// the watchlist), active (marked concepts awaiting decision), history
// (promoted + retired). Marking from a suggestion or from the
// watchlist on /positioning creates an active concept row; promoting
// jumps to /identify with a draft plan seeded from the concept.

// Should a passed-on suggestion come back? The bar itself is computed
// server-side (api/funnel.py owns the policy) and arrives on the review
// row; this only compares today's numbers against it. A name returns when
// its score clears the bar, when its side flips, or when the pass goes
// stale — and it says which, so the row can explain itself.
function reraiseReason(sug, review) {
  if (!review) return null;              // never passed on — show normally
  if (review.reraiseAboveScore != null && sug.score != null &&
      sug.score >= review.reraiseAboveScore) {
    return `score ${review.scoreAtReview} → ${sug.score} since you passed`;
  }
  if (review.sideAtReview && sug.side && review.sideAtReview !== sug.side) {
    return `side flipped ${review.sideAtReview} → ${sug.side} since you passed`;
  }
  if (review.reraiseAfter && new Date(review.reraiseAfter) <= new Date()) {
    return `passed ${String(review.reviewedAt).slice(0, 10)} · that read is stale`;
  }
  return null;                            // still suppressed
}

function Concepts({ onPromote }) {
  const D = window.MA_DATA;
  const [, force] = React.useState(0);
  const rerender = () => force(n => n + 1);
  const [showHistory, setShowHistory] = React.useState(false);
  const [showPassed, setShowPassed] = React.useState(false);
  const [openSug, setOpenSug] = React.useState(null);

  const concepts = D.concepts || [];
  const allSuggestions = D.conceptSuggestions || [];
  const reviews = D.conceptSuggestionReviews || [];
  const reviewByAsset = {};
  for (const r of reviews) reviewByAsset[r.asset] = r;

  // Split, don't drop: a passed name stays visible in its own fold so the
  // desk can see what it has already said no to, and take it back.
  const suggestions = [];
  const passed = [];
  for (const s of allSuggestions) {
    const review = reviewByAsset[s.asset];
    const back = reraiseReason(s, review);
    if (!review || back) suggestions.push({ ...s, reraised: back });
    else passed.push({ ...s, review });
  }
  const active = concepts.filter(c => c.status === "active");
  const history = concepts.filter(c => c.status !== "active");

  // Marking goes through the same write-through helper the watchlist and
  // live signals use (positioning.jsx), so a suggestion accepted here
  // survives a reload like any other concept. The suggestion's own levels
  // seed the thesis — a concept marked off a 5.53R setup should not open
  // as a blank box.
  const markFromSuggestion = (sug) => {
    const levels = [
      sug.entry != null ? `entry ${sug.entry}` : null,
      sug.stop != null ? `stop ${sug.stop}` : null,
      sug.target != null ? `target ${sug.target}` : null,
      sug.rr != null ? `${sug.rr}R` : null,
    ].filter(Boolean).join(" · ");
    const thesis = [sug.thesis || null, levels || null, sug.reason]
      .filter(Boolean).join("\n");
    markConcept({
      asset: sug.asset,
      side: sug.side,
      score: sug.score,
      tier: sug.tier,
      thesis,
      source: sug.origin === "voice" ? "voice_suggestion" : "watchlist_auto",
      reason: sug.reason,
    }).then(() => {
      D.conceptSuggestions = allSuggestions.filter(s => s.asset !== sug.asset);
      D.conceptSuggestionReviews = (D.conceptSuggestionReviews || [])
        .filter(x => x.asset !== sug.asset);
      rerender();
    });
  };

  // Passing writes through to /api/funnel/suggestion-reviews so the name
  // stays gone across reloads. The optimistic local row carries the same
  // thresholds the server computes, so the fold is right immediately.
  const passSuggestion = async (sug) => {
    const prior = reviewByAsset[sug.asset];
    const count = prior ? (prior.reviewCount || 1) + 1 : 1;
    const local = {
      asset: sug.asset,
      verdict: "passed",
      scoreAtReview: sug.score != null ? sug.score : null,
      sideAtReview: sug.side || null,
      note: null,
      reviewCount: count,
      reviewedAt: new Date().toISOString(),
      reraiseAboveScore: sug.score != null ? sug.score + 8 * Math.min(count, 3) : null,
      reraiseAfter: new Date(Date.now() + 45 * 86400000).toISOString(),
    };
    try {
      const r = await fetch("/api/funnel/suggestion-reviews", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          asset_id: sug.asset,
          verdict: "passed",
          score_at_review: local.scoreAtReview,
          side_at_review: local.sideAtReview,
        }),
      });
      if (r.ok) Object.assign(local, (await r.json()).review || {});
    } catch (e) {
      // Offline preview — keep the optimistic row.
    }
    D.conceptSuggestionReviews = (D.conceptSuggestionReviews || [])
      .filter(x => x.asset !== sug.asset).concat([local]);
    rerender();
  };

  const unpassSuggestion = async (asset) => {
    try {
      await fetch(`/api/funnel/suggestion-reviews/${encodeURIComponent(asset)}`,
                  { method: "DELETE" });
    } catch (e) { /* offline preview */ }
    D.conceptSuggestionReviews = (D.conceptSuggestionReviews || [])
      .filter(x => x.asset !== asset);
    rerender();
  };

  const updateThesis = (id, text) => {
    const c = D.concepts.find(x => x.id === id);
    if (c) { c.thesis = text; rerender(); }
  };

  const retireConcept = (id) => {
    const c = D.concepts.find(x => x.id === id);
    if (c) {
      c.status = "retired";
      c.retiredAt = new Date().toISOString().slice(0, 16).replace("T", " ");
      rerender();
    }
  };

  // Promotion goes through the shared write-through helper so the draft
  // plan reaches `trade_plans` instead of living in MA_DATA until the next
  // reload. Navigation waits for the POST — landing on /identify with an
  // id the server has never heard of is worse than a moment's delay.
  const promoteConcept = (c) => {
    promoteConceptToPlan(c).then(({ plan, persisted }) => {
      if (!persisted) {
        console.warn("plan not persisted — offline or preview", plan.id);
      }
      rerender();
      if (onPromote) onPromote(plan.id);
    });
  };

  return (
    <div className="concepts-view">
      <section className="block">
        <header className="block-head">
          <div className="block-title">
            <span className="block-num mono">C1</span>
            <span>Suggested by system</span>
            <span className="block-sub">
              {suggestions.length} candidate{suggestions.length === 1 ? "" : "s"} ·
              high-score watchlist rows not yet marked
              {passed.length > 0 && (
                <>
                  {" · "}
                  <button className="link-btn" onClick={() => setShowPassed(v => !v)}>
                    {passed.length} passed {showPassed ? "▲" : "▼"}
                  </button>
                </>
              )}
            </span>
          </div>
        </header>
        {suggestions.length === 0 ? (
          <div className="empty-state muted small">
            {passed.length > 0
              ? `Nothing new — you've passed on ${passed.length} name${
                  passed.length === 1 ? "" : "s"}, and they'll come back if the case changes.`
              : "No system suggestions right now — mark anything on the watchlist manually."}
          </div>
        ) : (
          <div className="concepts-list">
            {suggestions.map(s => (
              <div key={s.asset} className="concept-row suggested concept-row-clickable"
                   onClick={() => setOpenSug(s)}
                   title="Click to see what sources are saying">
                <div className="concept-asset">
                  <div className="mono asset-cell">{s.asset}</div>
                  <SideLabel side={s.side} />
                  <PaperRecordBadge ticker={s.asset} compact />
                  {s.origin === "voice" && (
                    <span className="concept-source-chip mono small voice-chip"
                          title="a trusted voice called this; the watchlist doesn't score it">
                      voice · unscored
                    </span>
                  )}
                </div>
                <div className="concept-score">
                  {s.score != null ? (
                    <>
                      <span className={`wl-score tier-${s.tier}`}>{s.score}</span>
                      <span className={`mono small ${s.dScore > 0 ? "pos" : s.dScore < 0 ? "neg" : "muted"}`}>
                        {s.dScore > 0 ? "+" : ""}{s.dScore} Δ
                      </span>
                    </>
                  ) : (
                    <>
                      <span className="wl-score tier-voice" title="never scored — surfaced by a trusted call">
                        {s.conviction != null ? s.conviction.toFixed(1) : "—"}
                      </span>
                      <span className="mono small muted">conv</span>
                    </>
                  )}
                </div>
                <div className="concept-reason muted small">
                  {s.reason}
                  {s.reraised && (
                    <div className="sug-reraised mono small">↻ back: {s.reraised}</div>
                  )}
                </div>
                <div className="concept-actions">
                  <button className="btn-primary sm"
                    onClick={(e) => { e.stopPropagation(); markFromSuggestion(s); }}>
                    mark as concept ↵
                  </button>
                  <button className="btn-ghost sm"
                    title="reviewed — hide until the case for it changes"
                    onClick={(e) => { e.stopPropagation(); passSuggestion(s); }}>
                    pass
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
        {showPassed && passed.length > 0 && (
          <div className="concepts-list passed-list">
            {passed.map(s => (
              <div key={s.asset} className="concept-row passed">
                <div className="concept-asset">
                  <div className="mono asset-cell">{s.asset}</div>
                  <SideLabel side={s.side} />
                </div>
                <div className="concept-score">
                  <span className={`wl-score tier-${s.tier}`}>{s.score}</span>
                  <span className="mono small muted">now</span>
                </div>
                <div className="concept-reason muted small">
                  passed {String(s.review.reviewedAt).slice(0, 10)}
                  {s.review.reviewCount > 1 ? ` · ${s.review.reviewCount}×` : ""}
                  {" · "}
                  {s.review.reraiseAboveScore != null
                    ? `back at score ${Math.round(s.review.reraiseAboveScore)}`
                    : "back on any change"}
                  {s.review.reraiseAfter
                    ? ` or on ${String(s.review.reraiseAfter).slice(0, 10)}`
                    : ""}
                </div>
                <div className="concept-actions">
                  <button className="btn-ghost sm"
                    title="put it back in the suggestion list now"
                    onClick={() => unpassSuggestion(s.asset)}>
                    un-pass
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="block">
        <header className="block-head">
          <div className="block-title">
            <span className="block-num mono">C2</span>
            <span>Active concepts</span>
            <span className="block-sub">
              {active.length} marked · awaiting promotion to plan
            </span>
          </div>
        </header>
        {active.length === 0 ? (
          <div className="empty-state muted small">
            No active concepts. Mark something from the watchlist or the suggestion list above.
          </div>
        ) : (
          <div className="concepts-list">
            {active.map(c => (
              <div key={c.id} className="concept-row active">
                <div className="concept-asset">
                  <div className="mono asset-cell">{c.asset}</div>
                  <SideLabel side={c.sideAtMark || "LONG"} />
                  <span className="concept-source-chip mono small muted">{c.source}</span>
                </div>
                <div className="concept-score">
                  <span className={`wl-score tier-${c.tierAtMark || 3}`}>{c.scoreAtMark}</span>
                  <span className="mono small muted">@ mark</span>
                </div>
                <textarea
                  className="concept-thesis form-input"
                  placeholder="Thesis · why this, why now, what invalidates"
                  value={c.thesis || ""}
                  onChange={(e) => updateThesis(c.id, e.target.value)}
                />
                <div className="concept-meta mono small muted">marked {c.markedAt}</div>
                <div className="concept-actions">
                  <button className="btn-primary sm" onClick={() => promoteConcept(c)}>
                    promote to plan →
                  </button>
                  <button className="btn-ghost sm" onClick={() => retireConcept(c.id)}>
                    retire
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="block">
        <header className="block-head sm">
          <div className="block-title">
            <span className="block-num mono">C3</span>
            <span>History</span>
            <span className="block-sub">
              {history.length} promoted / retired · lineage record
            </span>
          </div>
          <div className="block-actions">
            <button className="filter-pill" onClick={() => setShowHistory(v => !v)}>
              {showHistory ? "hide" : "show"}
            </button>
          </div>
        </header>
        {showHistory && (
          history.length === 0 ? (
            <div className="empty-state muted small">No promoted or retired concepts yet.</div>
          ) : (
            <table className="wl-table">
              <thead>
                <tr>
                  <th>ASSET</th>
                  <th>STATUS</th>
                  <th className="num">SCORE@MARK</th>
                  <th>SOURCE</th>
                  <th>MARKED</th>
                  <th>RESOLVED</th>
                  <th>LINK</th>
                </tr>
              </thead>
              <tbody>
                {history.map(c => (
                  <tr key={c.id}>
                    <td className="mono asset-cell">{c.asset}</td>
                    <td>
                      <span className={`status-chip status-${c.status}`}>{c.status}</span>
                    </td>
                    <td className="num">{c.scoreAtMark}</td>
                    <td className="mono small muted">{c.source}</td>
                    <td className="mono small muted">{c.markedAt}</td>
                    <td className="mono small muted">{c.promotedAt || c.retiredAt || "—"}</td>
                    <td className="mono small">
                      {c.tradePlanId
                        ? <span className="plan-link">→ {c.tradePlanId}</span>
                        : <span className="muted">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        )}
      </section>
      <DrillSheet open={!!openSug} onClose={() => setOpenSug(null)}
        title={openSug ? openSug.asset : ""}
        subtitle={openSug
          ? (openSug.score != null
              ? `${openSug.side} · score ${openSug.score} · T${openSug.tier}`
              : `${openSug.side} · unscored · conviction ${openSug.conviction ?? "—"}/5`)
          : ""}>
        {openSug && <SuggestionDetailPanel s={openSug} />}
      </DrillSheet>
    </div>
  );
}

function SuggestionDetailPanel({ s }) {
  const D = window.MA_DATA;
  const wlRow = (D.watchlist || []).find(w => w.asset === s.asset);
  const heroRow = (D.heroSignals || []).find(h => h.asset === s.asset);
  const themeMentions = ((D.streams && D.streams.themeMap) || [])
    .filter(t => (t.assets || t.tickers || []).map(x => (x || "").toUpperCase()).includes(s.asset.toUpperCase()))
    .slice(0, 6);
  const assetMentions = ((D.streams && D.streams.assetMap) || [])
    .filter(t => (t.label || "").toUpperCase() === s.asset.toUpperCase())
    .slice(0, 4);
  const sources = wlRow?.origins || heroRow?.sources || [];

  return (
    <div className="rt-content">
      <div className="rt-card-head">
        <div className="rt-asset-block">
          <div className="rt-asset mono">{s.asset}</div>
          <div className="rt-name">{wlRow?.name || s.asset}</div>
          <div className="rt-meta-row">
            <SideLabel side={s.side} />
            <span className="rt-setup">regime {s.regime}</span>
          </div>
        </div>
        <div className={`wl-score tier-${s.tier}`} style={{ fontSize: 24, padding: "6px 12px" }}>
          {s.score}
        </div>
      </div>

      <div className="rt-section">
        <div className="rt-section-head">
          <span className="rt-section-num mono">A</span>
          <span>Why suggested</span>
        </div>
        <p style={{ margin: "6px 0 0 0", lineHeight: 1.5 }}>{s.reason}</p>
        {s.thesis && (
          <p className="muted" style={{ margin: "6px 0 0 0", lineHeight: 1.5 }}>{s.thesis}</p>
        )}
        <div className="mono small muted" style={{ marginTop: 6 }}>
          {s.score != null
            ? `score ${s.score} · Δ ${s.dScore >= 0 ? "+" : ""}${s.dScore} · tier T${s.tier} · regime ${s.regime}`
            : `unscored · conviction ${s.conviction ?? "—"}/5 · called by ${s.voice || "a trusted source"}`}
        </div>
        {(s.entry != null || s.stop != null || s.target != null) && (
          <div className="mono small" style={{ marginTop: 6 }}>
            {[s.entry != null ? `entry ${s.entry}` : null,
              s.stop != null ? `stop ${s.stop}` : null,
              s.target != null ? `target ${s.target}` : null,
              s.rr != null ? `${s.rr}R` : null,
              s.price != null ? `last ${s.price}` : null].filter(Boolean).join(" · ")}
          </div>
        )}
      </div>

      {sources.length > 0 && (
        <div className="rt-section">
          <div className="rt-section-head">
            <span className="rt-section-num mono">B</span>
            <span>Watchlist origins · sources talking about this</span>
          </div>
          <div className="tag-row" style={{ marginTop: 8 }}>
            {sources.map((src, i) => <span key={i} className="tag-chip">{src}</span>)}
          </div>
        </div>
      )}

      {(themeMentions.length > 0 || assetMentions.length > 0) && (
        <div className="rt-section">
          <div className="rt-section-head">
            <span className="rt-section-num mono">C</span>
            <span>Live narrative context</span>
          </div>
          {assetMentions.map(t => (
            <div key={"a-" + t.label} className="mono small" style={{ padding: "4px 0" }}>
              · asset chatter <b>{t.label}</b> · {t.direction} · {t.age_days}d old · {(t.items || []).length || 0} items
            </div>
          ))}
          {themeMentions.map(t => (
            <div key={"t-" + t.label} className="mono small" style={{ padding: "4px 0" }}>
              · theme <b>{t.label}</b> · {t.direction} · {t.age_days}d old
            </div>
          ))}
        </div>
      )}

      {wlRow && (
        <div className="rt-section">
          <div className="rt-section-head">
            <span className="rt-section-num mono">D</span>
            <span>Watchlist breakdown</span>
          </div>
          <div className="rt-modifiers">
            <div className="rt-mod-row"><span className="rt-mod-lbl">tech</span><span className="rt-mod-val mono">{wlRow.tech}</span></div>
            <div className="rt-mod-row"><span className="rt-mod-lbl">vol</span><span className="rt-mod-val mono">{wlRow.vol}</span></div>
            <div className="rt-mod-row"><span className="rt-mod-lbl">R/R</span><span className="rt-mod-val mono">{(wlRow.rr || 0).toFixed(2)}</span></div>
            <div className="rt-mod-row"><span className="rt-mod-lbl">regime</span><span className="rt-mod-val mono">{wlRow.regime}</span></div>
            <div className="rt-mod-row"><span className="rt-mod-lbl">last scored</span><span className="rt-mod-val mono">{wlRow.last}</span></div>
          </div>
        </div>
      )}

      {sources.length === 0 && themeMentions.length === 0 && assetMentions.length === 0 && !wlRow && (
        <div className="empty-state muted small" style={{ padding: "1rem" }}>
          No additional context available yet — score-only suggestion. Once the ticker shows up in a live theme or has a watchlist origin, more detail will appear here.
        </div>
      )}
    </div>
  );
}

Object.assign(window, { Concepts });
