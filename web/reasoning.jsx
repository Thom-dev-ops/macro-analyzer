// Asset detail page + reasoning trail.

// Position-size bands from feature_vector.assign_position_size_tier —
// a function of the composite score, NOT of anyone's conviction.
const TIER_LABELS = {
  1: { label: "TIER 1 · FULL SIZE", color: "var(--gold)",  rule: "composite ≥ 85" },
  2: { label: "TIER 2 · STANDARD",  color: "var(--green)", rule: "composite ≥ 70" },
  3: { label: "TIER 3 · PROBE",     color: "var(--amber)", rule: "composite ≥ 55" },
  4: { label: "TIER 4 · AVOID",     color: "var(--red)",   rule: "under 55, or no defined invalidation" },
};

// Small helper the levels strips use. Under $1000 → 2dp, otherwise
// thousands-separated. (Restores the helper the c21f673 asset-page
// refactor referenced without defining, which prevented AssetPage
// from mounting — fmtPrice threw ReferenceError.)
function fmtPrice(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return v < 1000 ? v.toFixed(2) : v.toLocaleString();
}

// ───────────────────────────────────────────────────────────────
// SignalTimelineBlock — 60-day sentiment-over-time chart.
// Reads signalTimeline built by desk_data.build_reasoning_section
// (which calls signals.aggregation.build_signal_timeline_for_ticker).
// Pure SVG in the house style — no external chart libraries.
// ───────────────────────────────────────────────────────────────
function SignalTimelineBlock({ timeline }) {
  const [showWindows, setShowWindows] = React.useState(true);
  const [showCoverage, setShowCoverage] = React.useState(true);

  const dates = timeline.dates;
  const N = dates.length;
  if (N < 2) return null;

  const W = 900, H = 220;
  const padL = 64, padR = 24, padT = 20, padB = 28;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  const xFor = (i) => padL + (i / (N - 1)) * plotW;
  const yFor = (v) => padT + (1 - (v + 1) / 2) * plotH;   // v in [-1, +1] → y
  const yCov = (pct) => padT + (1 - pct / 100) * plotH;   // 0..100 → y (mirror axis)

  const toPath = (arr) => arr
    .map((v, i) => `${i === 0 ? "M" : "L"}${xFor(i).toFixed(1)} ${yFor(v).toFixed(1)}`)
    .join(" ");

  const covPath = timeline.coverage
    .map((c, i) => `${i === 0 ? "M" : "L"}${xFor(i).toFixed(1)} ${yCov(c).toFixed(1)}`)
    .join(" ");
  const covAreaPath = `${covPath} L${xFor(N - 1).toFixed(1)} ${padT + plotH} L${xFor(0).toFixed(1)} ${padT + plotH} Z`;

  const xLabels = [];
  for (let i = 0; i < N; i += 10) xLabels.push({ i, label: dates[i].slice(5) });
  if ((N - 1) % 10 !== 0) xLabels.push({ i: N - 1, label: dates[N - 1].slice(5) });

  const C = {
    blend: "var(--blue, #2a78d6)",
    w1d:   "var(--orange, #eb6834)",
    w7d:   "var(--yellow, #eda100)",
    w28d:  "var(--teal, #1baf7a)",
    w90d:  "var(--purple, #7f77dd)",
    cov:   "var(--text-mute-3, #b4b2a9)",
    grid:  "var(--line-soft, rgba(0,0,0,0.08))",
    axis:  "var(--text-mute-2, #898781)",
  };

  const last = {
    blend: timeline.blend[N - 1],
    w1d:   timeline.windows["1d"][N - 1],
    w7d:   timeline.windows["7d"][N - 1],
    w28d:  timeline.windows["28d"][N - 1],
    w90d:  timeline.windows["90d"][N - 1],
    cov:   timeline.coverage[N - 1],
  };

  const dirWord = (v) => v > 0.05 ? "long" : v < -0.05 ? "short" : "neutral";
  const pct = (v) => `${Math.abs(Math.round(v * 100))}%`;

  return (
    <section className="block ap-timeline-block">
      <header className="block-head sm">
        <div className="block-title">
          <span className="block-num mono">S1</span>
          <span>Signal timeline</span>
          <span className="block-sub">
            60d sentiment · blend now <b className={`dir-${dirWord(last.blend)}`}>
              {dirWord(last.blend)} · {pct(last.blend)}
            </b> · coverage {Math.round(last.cov)}%
          </span>
        </div>
        <div className="block-actions">
          <div className="filter-pill-row">
            <button className={`filter-pill ${showWindows ? "on" : ""}`}
              onClick={() => setShowWindows(v => !v)}>windows</button>
            <button className={`filter-pill ${showCoverage ? "on" : ""}`}
              onClick={() => setShowCoverage(v => !v)}>coverage</button>
          </div>
        </div>
      </header>

      <div className="ap-timeline-wrap">
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
          role="img" aria-label={`Signal timeline for the last ${N} days`}
          className="signal-timeline-svg">

          {showCoverage && (
            <path d={covAreaPath} fill={C.cov} opacity="0.10" />
          )}

          {[1, 0.5, 0, -0.5, -1].map(v => (
            <line key={v} x1={padL} x2={W - padR}
              y1={yFor(v)} y2={yFor(v)}
              stroke={v === 0 ? C.axis : C.grid}
              strokeWidth={v === 0 ? 1 : 0.5}
              strokeDasharray={v === 0 ? "0" : "2 3"} />
          ))}

          {[{v: 1, l: "100%"}, {v: 0.5, l: "50%"}, {v: 0, l: "0"},
            {v: -0.5, l: "50%"}, {v: -1, l: "100%"}].map(({v, l}) => (
            <text key={v} x={padL - 8} y={yFor(v) + 3}
              fontSize="10" fill={C.axis} textAnchor="end"
              fontFamily="var(--mono)">{l}</text>
          ))}

          {xLabels.map(({i, label}) => (
            <text key={i} x={xFor(i)} y={H - 8}
              fontSize="10" fill={C.axis} textAnchor="middle"
              fontFamily="var(--mono)">{label}</text>
          ))}

          {showWindows && (
            <>
              <path d={toPath(timeline.windows["90d"])}
                fill="none" stroke={C.w90d} strokeWidth="1.25"
                strokeDasharray="1 2" opacity="0.85" />
              <path d={toPath(timeline.windows["28d"])}
                fill="none" stroke={C.w28d} strokeWidth="1.25"
                strokeDasharray="4 2" opacity="0.85" />
              <path d={toPath(timeline.windows["7d"])}
                fill="none" stroke={C.w7d} strokeWidth="1.25"
                strokeDasharray="2 2" opacity="0.9" />
              <path d={toPath(timeline.windows["1d"])}
                fill="none" stroke={C.w1d} strokeWidth="1.25"
                opacity="0.9" />
            </>
          )}

          <path d={toPath(timeline.blend)}
            fill="none" stroke={C.blend} strokeWidth="2.25" />

          <circle cx={xFor(N - 1)} cy={yFor(last.blend)}
            r="4" fill={C.blend} stroke="var(--bg-card, #fff)" strokeWidth="1.5" />

          {/* Axis title — one rotated label per half so LONG/SHORT are
              unambiguous without colliding with the tick-value column. */}
          {(() => {
            const topY = padT + plotH * 0.25;
            const botY = padT + plotH * 0.75;
            const tx = 14;
            return (
              <>
                <text x={tx} y={topY} fontSize="9" fontWeight="600" letterSpacing="0.14em"
                  fill={C.axis} fontFamily="var(--mono)"
                  transform={`rotate(-90 ${tx} ${topY})`} textAnchor="middle">LONG</text>
                <text x={tx} y={botY} fontSize="9" fontWeight="600" letterSpacing="0.14em"
                  fill={C.axis} fontFamily="var(--mono)"
                  transform={`rotate(-90 ${tx} ${botY})`} textAnchor="middle">SHORT</text>
              </>
            );
          })()}
        </svg>
      </div>

      <div className="ap-timeline-legend mono small">
        <span className="tl-lg-item"><span className="tl-swatch" style={{background: C.blend}}></span>blend {pct(last.blend)} {dirWord(last.blend)}</span>
        {showWindows && (
          <>
            <span className="tl-lg-item"><span className="tl-swatch tl-dash-solid" style={{background: C.w1d}}></span>1d {last.w1d === 0 ? "—" : `${pct(last.w1d)} ${dirWord(last.w1d)}`}</span>
            <span className="tl-lg-item"><span className="tl-swatch tl-dash-short" style={{background: C.w7d}}></span>7d {last.w7d === 0 ? "—" : `${pct(last.w7d)} ${dirWord(last.w7d)}`}</span>
            <span className="tl-lg-item"><span className="tl-swatch tl-dash-med"   style={{background: C.w28d}}></span>28d {last.w28d === 0 ? "—" : `${pct(last.w28d)} ${dirWord(last.w28d)}`}</span>
            <span className="tl-lg-item"><span className="tl-swatch tl-dash-dot"   style={{background: C.w90d}}></span>90d {last.w90d === 0 ? "—" : `${pct(last.w90d)} ${dirWord(last.w90d)}`}</span>
          </>
        )}
        {showCoverage && (
          <span className="tl-lg-item muted"><span className="tl-swatch" style={{background: C.cov, opacity: 0.5}}></span>coverage {Math.round(last.cov)}%</span>
        )}
      </div>
    </section>
  );
}

// Why the technical agent declined to produce levels. It never fabricates
// them: no price history or too few bars for ATR means no rails at all.
const LEVELS_REASON = {
  no_price: "no levels — no price history for this ticker",
  no_atr: "no levels — too few bars to compute ATR",
};

// ───────────────────────────────────────────────────────────────
// LevelProvenance — why each rail sits where it sits.
// A price with no reason behind it is the thing this desk is trying not
// to be: every entry/stop/target names its source (a swing zone with its
// touch count, a trusted voice with their resolution rate, or an honest
// open-field projection), and refused levels stay visible with the reason
// they were refused.
// ───────────────────────────────────────────────────────────────
const RAIL_SOURCE = {
  structure: { label: "chart structure", cls: "prov-structure" },
  trusted_voices: { label: "trusted voices", cls: "prov-voices" },
  open_field: { label: "open field", cls: "prov-open" },
  mechanical_v0: { label: "ATR fallback", cls: "prov-mech" },
};

function LevelProvenance({ provenance, rejected }) {
  const rows = (provenance || []).filter(p => ["entry", "stop", "target"].includes(p.role));
  const checks = (provenance || []).filter(p => (p.role || "").endsWith("_crosscheck"));
  if (!rows.length) return null;
  const srcOf = (s) => RAIL_SOURCE[s] || { label: (s || "").replace(/_/g, " "), cls: "prov-mech" };
  return (
    <div className="rt-section">
      <div className="rt-section-head">
        <span className="rt-section-num mono">L</span>
        <span>How these levels were set</span>
        <span className="rt-section-sub">{rows.length} rails · {checks.length} cross-checks</span>
      </div>
      <div className="prov-list">
        {rows.map(p => {
          const src = srcOf(p.source);
          return (
            <div key={p.role} className="prov-row">
              <div className="prov-role mono">{p.role.toUpperCase()}</div>
              <div className="prov-val mono">{fmtPrice(p.value)}</div>
              <div className="prov-body">
                <span className={`prov-chip mono ${src.cls}`}>{src.label}</span>
                <span className="prov-basis">{p.basis}</span>
                {(p.contributors || []).length > 0 && (
                  <div className="prov-who">
                    {p.contributors.map((c, i) => (
                      <span key={i} className="prov-author" title={c.thesis || ""}>
                        {c.display_name}
                        <span className="muted">
                          {" · "}
                          {c.meaningful && c.setup_win_rate != null
                            ? `${Math.round(c.setup_win_rate * 100)}% setup win`
                            : "unproven"}
                          {c.at ? ` · ${c.at}` : ""}
                        </span>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {checks.length > 0 && (
        <div className="prov-checks">
          {checks.map((p, i) => (
            <div key={i} className="prov-check mono small">
              <span className="prov-check-role">{p.role.replace("_crosscheck", "")} cross-check</span>
              {" · "}{fmtPrice(p.value)}{" · "}{p.basis}
              {p.who ? ` — ${p.who}` : ""}
            </div>
          ))}
        </div>
      )}

      {(rejected || []).length > 0 && (
        <div className="prov-rejected">
          <div className="prov-rej-head mono small">refused</div>
          {rejected.map((p, i) => (
            <div key={i} className="prov-check mono small">
              <span className="prov-check-role">{p.role}</span>
              {" · "}{fmtPrice(p.value)}{" · "}{p.reason}
              {p.who ? ` — ${p.who}` : ""}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ───────────────────────────────────────────────────────────────
// AssetSignalCalls — the tracked-voice calls behind this ticker.
// Answers "which charts drove this, and where does it sit in the tape":
// each call shows the chart it was read from, the levels the human drew,
// and how they compare with the agent's own rails.
// ───────────────────────────────────────────────────────────────
function AssetSignalCalls({ calls, signal }) {
  if (!calls || !calls.length) return null;
  const agentEntry = signal.entry || 0;
  const kolEntry = (c) => {
    const lo = c.entryLow, hi = c.entryHigh;
    if (lo && hi) return (lo + hi) / 2;
    return lo || hi || null;
  };
  return (
    <div className="rt-section">
      <div className="rt-section-head">
        <span className="rt-section-num mono">S</span>
        <span>Signals behind this asset</span>
        <span className="rt-section-sub">{calls.length} · newest first</span>
      </div>
      <ScoreNote>{MA_GLOSSARY.terms.conviction}</ScoreNote>
      <div className="asig-list">
        {calls.map(c => {
          const ke = kolEntry(c);
          // Divergence between the human's entry and the agent's rails —
          // the whole point of showing them side by side.
          const div = ke && agentEntry ? ((agentEntry - ke) / ke) * 100 : null;
          const sideCls = (c.side || "").toLowerCase();
          return (
            <div key={c.signalId} className="asig-row">
              {c.chartUrl ? (
                <a href={c.chartUrl} target="_blank" rel="noreferrer" className="asig-thumb-wrap">
                  <img className="asig-thumb" src={c.chartUrl} alt={`${signal.asset} chart`}
                       onError={(e) => { e.target.style.display = "none"; }} />
                </a>
              ) : (
                <div className="asig-thumb-none mono">no chart</div>
              )}
              <div className="asig-body">
                <div className="asig-head">
                  <span className={`side-label side-${sideCls}`}>{c.side || "—"}</span>
                  {c.conviction != null && (
                    <span className="asig-conv mono" title={MA_GLOSSARY.terms.conviction}>
                      conv {c.conviction.toFixed(1)}
                    </span>
                  )}
                  {c.horizon && <span className="asig-horizon mono">{c.horizon}</span>}
                  <span className="asig-when mono muted">{c.at}</span>
                  {c.channel && <span className="asig-chan mono muted">{c.channel}</span>}
                </div>
                {(ke || c.stop || c.target) && (
                  <div className="asig-levels mono small">
                    {ke && <span>entry {fmtPrice(ke)}</span>}
                    {c.stop ? <span className="red">stop {fmtPrice(c.stop)}</span> : null}
                    {c.target ? <span className="green">target {fmtPrice(c.target)}</span> : null}
                    {div != null && Math.abs(div) >= 1 && (
                      <span className="asig-div" title="agent entry vs this call's entry">
                        agent {div > 0 ? "+" : ""}{div.toFixed(1)}%
                      </span>
                    )}
                  </div>
                )}
                {c.thesis && <div className="asig-thesis">{c.thesis}</div>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ───────────────────────────────────────────────────────────────
// AssetPage — full page for a single signal/asset.
// ───────────────────────────────────────────────────────────────

// ── Funnel action for the asset page ───────────────────────────────────
// One button that knows where this asset already is:
//   not marked  → "Mark as concept"   (writes to trade_concepts)
//   active      → "Promote to plan →" (writes to trade_plans, goes to /identify)
//   promoted    → "Open plan →"       (navigates, nothing to write)
// Both writes go through the same helpers /positioning and /concepts use,
// so a mark made here is the same row as a mark made there.
function FunnelAction({ signal, busy, setBusy, onChanged, onPromote, onNav }) {
  const asset = signal.asset;
  const concepts = (window.MA_DATA.concepts || []);
  const concept = concepts.find(c => c.asset === asset && c.status === "active");
  const promoted = concepts.find(c => c.asset === asset && c.status === "promoted");
  const [note, setNote] = React.useState(null);

  const mark = () => {
    setBusy(true);
    markConcept({
      asset,
      side: signal.side,
      score: signal.score,
      tier: signal.tier,
      thesis: (signal.whyNow || []).join(" · "),
      source: "asset_page",
      reason: null,
    })
      .then(({ deduped, persisted }) => {
        // Never claim a write that did not land. An unsaved mark still
        // moves the funnel on screen, but the page has to say so.
        if (!persisted) setNote("marked locally only — the write failed");
        else setNote(deduped ? "already on the concept list" : "marked as a concept");
        onChanged();
      })
      .catch(() => setNote("could not mark — check the API"))
      .finally(() => setBusy(false));
  };

  const promote = () => {
    if (!concept) return;
    setBusy(true);
    // Seed the draft plan with the technical agent's levels — the whole
    // reason to promote from this page rather than from /concepts is that
    // the levels are right here.
    promoteConceptToPlan(concept, {
      seed: {
        side: signal.side,
        levelSide: (signal.levels && signal.levels.side) || null,
        entry: signal.hasLevels ? signal.entry : null,
        stop: signal.hasLevels ? signal.stop : null,
        targets: signal.hasLevels && signal.target
          ? [{ price: signal.target, weight: 1 }] : [],
        thesis: (signal.whyNow || []).join(" · "),
      },
    })
      .then(({ plan, persisted }) => {
        onChanged();
        if (!persisted) setNote("plan created locally only — API unreachable");
        if (onPromote) onPromote(plan.id);
      })
      .catch(() => setNote("could not promote — check the API"))
      .finally(() => setBusy(false));
  };

  let button;
  if (promoted && !concept) {
    button = (
      <button
        className="btn-primary"
        onClick={() => (onPromote ? onPromote(promoted.tradePlanId) : onNav && onNav("identify"))}
      >
        Open plan →
      </button>
    );
  } else if (concept) {
    button = (
      <button className="btn-primary" onClick={promote} disabled={busy}>
        {busy ? "promoting…" : "Promote to plan →"}
      </button>
    );
  } else {
    button = (
      <button className="btn-primary" onClick={mark} disabled={busy}>
        {busy ? "marking…" : "Mark as concept +"}
      </button>
    );
  }

  return (
    <>
      {button}
      {concept && (
        <span className="ah-funnel-state mono" title="This asset is on the concept list">
          ● concept since {String(concept.markedAt || "").slice(0, 10)}
        </span>
      )}
      {note && <span className="ah-funnel-note mono">{note}</span>}
    </>
  );
}

function AssetPage({ signal, onBack, returnTo, onPromote, onNav }) {
  // Where this asset already sits in the funnel, so the page offers the
  // ONE action that actually moves it on rather than a row of buttons
  // that do nothing. Recomputed on every render because marking or
  // promoting mutates MA_DATA in place.
  const [funnelBusy, setFunnelBusy] = React.useState(false);
  const [, forceFunnel] = React.useState(0);
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onBack(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onBack]);
  if (!signal) return null;

  const D = window.MA_DATA;
  const r = (D.reasoning || {})[signal.id] || (D.reasoning || {})["sig-ura-2605"] || {};
  const tier = r.tier ?? signal.tier ?? 3;
  const total = r.total ?? signal.score ?? 0;
  const series = D.priceSeries?.[signal.asset];
  const trades = (D.activeTrades || []).filter(t => t.asset === signal.asset);
  const portfolioEquity = D.portfolio?.equityUsd || 612400;
  const returnLabel = (returnTo || "positioning").toUpperCase();

  // Levels can be absent while the technical agent is still working.
  const hasLevels = (signal.entry || 0) > 0 && (signal.stop || 0) > 0 && (signal.target || 0) > 0;
  // Side-agnostic: a SHORT's stop sits above entry and its target below,
  // so signed math would render both distances backwards.
  const distToStop  = hasLevels ? (Math.abs(signal.entry - signal.stop) / signal.entry) * 100 : 0;
  const upside      = hasLevels ? (Math.abs(signal.target - signal.entry) / signal.entry) * 100 : 0;
  const rr          = signal.rr || 0;
  const tInfo       = TIER_LABELS[tier] || TIER_LABELS[3];

  return (
    <div className="asset-page" data-screen-label="Asset detail">

      {/* ── Breadcrumb / back row ─────────────────────────────── */}
      <div className="asset-crumb-row">
        <button className="asset-back" onClick={onBack} aria-label="Back">
          <span className="asset-back-glyph">←</span>
          <span className="asset-back-lbl mono">BACK TO /{returnLabel.toLowerCase()}</span>
        </button>
        <span className="asset-crumb-id mono">REASONING TRAIL · {signal.id || "—"}</span>
        <button className="asset-page-action mono" onClick={onBack}>ESC</button>
      </div>

      {/* ── HERO: ticker, score, tier, side + actions ─────────── */}
      <header className={`asset-hero tier-${tier}`}>
        <div className="ah-stripe" style={{ background: tInfo.color }}></div>

        <div className="ah-main">
          <div className="ah-ident">
            <div className="ah-ticker mono">{signal.asset}</div>
            <div className="ah-name">{signal.name}</div>
            <div className="ah-meta-row">
              <SideLabel side={signal.side} />
              <span className="ah-setup">{signal.setup}</span>
              <span className="ah-update mono muted">updated {signal.lastUpdate}</span>
              <PaperRecordBadge ticker={signal.asset} />
            </div>
          </div>

          <div className="ah-score-block">
            <div className="ah-score-lbl mono">COMPOSITE SCORE</div>
            <div className={`ah-score-num mono tier-${tier}`}>
              {total}
              <span className="ah-score-of muted mono">/100</span>
            </div>
            {signal.scorePrev != null && (
              <div className={`ah-score-d mono ${total - signal.scorePrev > 0 ? "pos" : total - signal.scorePrev < 0 ? "neg" : "muted"}`}>
                {total - signal.scorePrev > 0 ? "▲" : total - signal.scorePrev < 0 ? "▼" : "·"} {Math.abs(total - signal.scorePrev)} <span className="muted">vs prev</span>
              </div>
            )}
            <ScoreNote title={MA_GLOSSARY.composite.long}>
              {MA_GLOSSARY.composite.short}
            </ScoreNote>
          </div>

          <div className="ah-tier-block">
            <div className={`ah-tier-badge tier-${tier}`} title={MA_GLOSSARY.tier.long}>
              <span className="ah-tier-dot" style={{ background: tInfo.color }}></span>
              {tInfo.label}
            </div>
            <ScoreNote title={MA_GLOSSARY.tier.long}>{tInfo.rule}</ScoreNote>
            <div className="ah-side-block">
              <span className={`ah-side-pill side-${(signal.side || "watch").toLowerCase()}`}>{signal.side}</span>
              <span className="ah-rr mono">R/R · {rr.toFixed(2)}</span>
            </div>
            <div className="ah-regime mono muted">
              regime fit · {signal.regimeFit?.replace(/_/g, " ") || "—"}
            </div>
          </div>
        </div>

        {/* Tape-vs-price divergence flag — fires when the tape blend
            leans strongly one way while price is deeply on the other.
            Sits at hero level so overconfident composite scores get
            called out where the trader will actually see them. */}
        {(() => {
          const sw = r.signalWindows;
          // `series` was previously referenced as `shownSeries`, which
          // never got defined in c21f673 → AssetPage failed to render.
          if (!sw || !sw.blend || !series || series.length < 10) return null;
          const first = series[Math.max(0, series.length - 90)]; // 90d ago (or as far back as we have)
          const last  = series[series.length - 1];
          if (!first || !last) return null;
          const ret90 = (last / first) - 1;
          const blendDir  = sw.blend.direction;      // "long" | "short" | "neutral"
          const blendConf = sw.blend.confidence || 0;
          // Divergence: |ret| ≥ 15% AND tape conviction ≥ 55% pointing
          // opposite to price direction.
          const priceDir = ret90 >  0.15 ? "long"
                         : ret90 < -0.15 ? "short"
                         : "neutral";
          const opposed = (blendDir === "long"  && priceDir === "short")
                       || (blendDir === "short" && priceDir === "long");
          if (!opposed || blendConf < 0.55) return null;
          const retPct = (ret90 * 100).toFixed(1);
          const conviction = Math.round(blendConf * 100);
          return (
            <div className="ah-divergence mono">
              <span className="ah-div-badge">⚠ TAPE ≠ PRICE</span>
              <span className="ah-div-body">
                tape reads <b className={`dir-${blendDir}`}>{blendDir.toUpperCase()} · {conviction}%</b>
                {" but "}90d price is <b className={`dir-${priceDir}`}>{ret90 >= 0 ? "+" : ""}{retPct}%</b>.
                {" Composite may be overstating conviction — wait for structural confirmation before trusting the tape."}
              </span>
            </div>
          );
        })()}

        {/* Actions row — the funnel action is the primary one. This page
            is where the case for a trade is actually read, so it has to be
            a place the trade can be moved on from; until now every button
            here was inert and the only way forward was to leave, find the
            name on /concepts, and mark it there. */}
        <div className="ah-actions">
          <FunnelAction
            signal={signal}
            busy={funnelBusy}
            setBusy={setFunnelBusy}
            onChanged={() => forceFunnel(n => n + 1)}
            onPromote={onPromote}
            onNav={onNav}
          />
          <button className="btn-secondary">Raw feature vector</button>
          <span className="ah-actions-spacer"></span>
          <button className="btn-ghost mono">Share trail ↗</button>
        </div>

        {/* Signal-alignment runner — 7-window strip across the bottom
            of the hero. Lives here (not in the reasoning trail) because
            direction-across-horizons is a hero-tier fact: it belongs
            next to the composite score, not buried under it. */}
        {r.signalWindows && r.signalWindows.rows.length > 0 && (() => {
          const sw = r.signalWindows;
          const trendLabel = {
            flipping_short: "⚠ tape flipping SHORT",
            flipping_long:  "⚠ tape flipping LONG",
            stable_long:    "stable long across horizons",
            stable_short:   "stable short across horizons",
            mixed:          "mixed across horizons",
          }[sw.trend] || sw.trend;
          const trendClass = sw.recentFlip ? "trend-flip" : `trend-${sw.trend}`;
          return (
            <div className="ah-signal-runner">
              <div className="ahsr-header">
                <div className="ahsr-header-left">
                  <span className="ahsr-title mono">Signal alignment</span>
                  <span className="ahsr-title-sub mono muted">
                    KOL / extractor tape over trailing lookback windows · <b>past</b>, not forward horizons
                  </span>
                </div>
                <div className="ahsr-header-right">
                  <span className={`ahsr-trend mono ${trendClass}`}>{trendLabel}</span>
                </div>
              </div>
              <div className="ahsr-blend-row">
                <div className="ahsr-blend-lbl mono muted">Blended read</div>
                <div className={`ahsr-blend mono dir-${sw.blend.direction}`}>
                  {sw.blend.direction.toUpperCase()} · {Math.round(sw.blend.confidence * 100)}%
                </div>
                <div className="ahsr-blend-cov mono muted">
                  coverage {Math.round(sw.blend.coverage * 100)}% — share of windows w/ ≥3 signals
                </div>
              </div>
              <div className="ahsr-strip">
                {sw.rows.map(w => (
                  <div key={w.label} className={`ahsr-cell align-${w.alignment} dir-${w.direction}`}
                       title={`Trailing ${w.label} window (past). ${w.nSignals} signal${w.nSignals === 1 ? "" : "s"} · read ${w.direction} @ ${Math.round(w.confidence * 100)}%`}>
                    <div className="ahsr-cell-lbl mono">last {w.label}</div>
                    <div className={`ahsr-cell-dot dir-${w.direction}`}></div>
                    <div className={`ahsr-cell-conf mono`}>{Math.round(w.confidence * 100)}%</div>
                    <div className="ahsr-cell-n mono muted">{w.nSignals} signal{w.nSignals === 1 ? "" : "s"}</div>
                    <div className={`ahsr-cell-dir mono dir-${w.direction}`}>{w.direction}</div>
                  </div>
                ))}
              </div>
              <div className="ahsr-legend mono muted">
                each cell = one trailing lookback window ·
                <span className="ahsr-legend-dot ahsr-legend-long"></span> long ·
                <span className="ahsr-legend-dot ahsr-legend-short"></span> short ·
                <span className="ahsr-legend-dot ahsr-legend-neutral"></span> neutral ·
                % = weighted agreement across signals in that window
              </div>
            </div>
          );
        })()}
      </header>

      {/* ── Signal timeline — sentiment over time ─────────────────
          Day-by-day historical replay of the blend + per-window
          conviction (1d / 7d / 28d / 90d). Deliberately NOT joined
          to price — this reads what tracked voices have been saying
          on this ticker, whether price agrees or not. Sits right below
          the runner (extends the same story) and above the price row. */}
      {r.signalTimeline && r.signalTimeline.dates && r.signalTimeline.dates.length > 0 && (
        <SignalTimelineBlock timeline={r.signalTimeline} />
      )}

      {/* ── Price chart + Trade-on-this-asset side-by-side ────── */}
      <section className="ap-row">
        <div className="block ap-chart-block">
          <header className="block-head sm">
            <div className="block-title">
              <span className="block-num mono">P1</span>
              <span>Price · 90d</span>
              <span className="block-sub">entry · stop · target rails</span>
            </div>
            <div className="block-actions">
              <div className="filter-pill-row">
                {["30d","90d","6m","1y"].map((p, i) => (
                  <button key={p} className={`filter-pill ${i === 1 ? "on" : ""}`}>{p}</button>
                ))}
              </div>
            </div>
          </header>
          <div className="ap-chart-wrap">
            {series && hasLevels ? (
              <PriceChart
                series={series}
                entry={signal.entry}
                stop={signal.stop}
                target={signal.target}
                side={signal.side}
                height={260}
              />
            ) : (
              <div className="pc-empty mono muted">
                {series
                  ? (LEVELS_REASON[signal.levelsReason] || "no levels for this pass")
                  : `No price series for ${signal.asset}`}
              </div>
            )}
          </div>
          {hasLevels ? (
            <div className="ap-levels-strip">
              <div className="apl">
                <div className="apl-lbl mono">ENTRY</div>
                <div className="apl-val mono">{fmtPrice(signal.entry)}</div>
                <div className="apl-sub mono muted">trigger</div>
              </div>
              <div className="apl">
                <div className="apl-lbl mono">STOP</div>
                <div className="apl-val mono red">{fmtPrice(signal.stop)}</div>
                <div className="apl-sub mono red">−{distToStop.toFixed(1)}% to inval.</div>
              </div>
              <div className="apl">
                <div className="apl-lbl mono">TARGET</div>
                <div className="apl-val mono green">{fmtPrice(signal.target)}</div>
                <div className="apl-sub mono green">+{upside.toFixed(1)}% upside</div>
              </div>
              <div className="apl">
                <div className="apl-lbl mono">R / R</div>
                <div className="apl-val mono">{rr.toFixed(2)}</div>
                <div className="apl-sub mono muted">{distToStop > 0 ? (upside / distToStop).toFixed(2) : "—"}× win/loss</div>
              </div>
            </div>
          ) : null}
          {hasLevels && signal.levelMethod ? (
            <div className="ap-levels-prov mono small muted">
              <span className={signal.levelStructural ? "green" : ""}>
                {signal.levelStructural ? "structural" : "mechanical"}
              </span>
              {" · "}{signal.levelMethod}
              {signal.frameworkSetup && (
                <span title={MA_GLOSSARY.terms.setupType}>
                  {" · framework: "}{signal.frameworkSetup.replace(/_/g, " ")}
                </span>
              )}
              {(signal.levelNotes || []).length ? ` · ${signal.levelNotes[0]}` : ""}
            </div>
          ) : (
            <div className="sc-levels-pending mono small muted" style={{ margin: "0 16px 14px" }}>
              {LEVELS_REASON[signal.levelsReason] || "no levels — awaiting next scoring pass"}
            </div>
          )}
        </div>

        <AssetTradeBreakdown
          signal={signal}
          trades={trades}
          portfolioEquity={portfolioEquity}
        />
      </section>

      {/* ── The reasoning trail body ──────────────────────────── */}
      <ReasoningTrail signal={signal} hideHeader hideFooter />
    </div>
  );
}

// ───────────────────────────────────────────────────────────────
// Active trade(s) on this asset, with $/ % portfolio breakdown.
// ───────────────────────────────────────────────────────────────
function AssetTradeBreakdown({ signal, trades, portfolioEquity }) {
  if (!trades || !trades.length) {
    // Project a hypothetical sizing for the proposed setup.
    const proposed = Math.round(portfolioEquity * 0.04 / 100) * 100; // ~4%
    const hasLevels = (signal.entry || 0) > 0 && (signal.stop || 0) > 0;
    const distToStop = hasLevels ? Math.abs(signal.entry - signal.stop) / signal.entry : 0;
    const riskUsd = Math.round(proposed * distToStop);
    const rr = signal.rr || 0;
    return (
      <div className="block ap-trade-block ap-trade-empty">
        <header className="block-head sm">
          <div className="block-title">
            <span className="block-num mono">P2</span>
            <span>Active trade</span>
            <span className="block-sub">no open position · projected sizing</span>
          </div>
        </header>
        <div className="ap-empty-card">
          <div className="ap-empty-eyebrow mono">SUGGESTED SIZING · 4% of equity</div>
          <div className="ap-empty-grid">
            <div className="ap-eg">
              <div className="ap-eg-lbl mono">SIZE</div>
              <div className="ap-eg-val mono">${proposed.toLocaleString()}</div>
              <div className="ap-eg-sub mono muted">{((proposed / portfolioEquity) * 100).toFixed(1)}% of port</div>
            </div>
            <div className="ap-eg">
              <div className="ap-eg-lbl mono">RISK $</div>
              <div className="ap-eg-val mono red">${riskUsd.toLocaleString()}</div>
              <div className="ap-eg-sub mono muted">{(distToStop * 100).toFixed(1)}% to stop</div>
            </div>
            <div className="ap-eg">
              <div className="ap-eg-lbl mono">UPSIDE $</div>
              <div className="ap-eg-val mono green">${Math.round(riskUsd * rr).toLocaleString()}</div>
              <div className="ap-eg-sub mono muted">at target · R/R {rr.toFixed(2)}</div>
            </div>
          </div>
          <button className="btn-primary ap-empty-cta">Log trade at sizing ↵</button>
        </div>
      </div>
    );
  }

  return (
    <div className="block ap-trade-block">
      <header className="block-head sm">
        <div className="block-title">
          <span className="block-num mono">P2</span>
          <span>Active trade{trades.length > 1 ? "s" : ""} · {signal.asset}</span>
          <span className="block-sub">{trades.length} open · % of {portfolioEquity.toLocaleString()} equity</span>
        </div>
      </header>
      <div className="ap-trades">
        {trades.map(t => {
          const pctOfPort = (t.sizeUsd / portfolioEquity) * 100;
          const distToStop = t.side === "LONG"
            ? ((t.entry - t.stop) / t.entry) * 100
            : ((t.stop - t.entry) / t.entry) * 100;
          const distToTgt = t.side === "LONG"
            ? ((t.target - t.entry) / t.entry) * 100
            : ((t.entry - t.target) / t.entry) * 100;
          const scoreD = t.scoreNow - t.scoreAtOpen;
          // progress bar: 0 = stop, 1 = target. position = (entry+pnlPct/100*entry – stop)/(target-stop)
          const live = t.entry * (1 + (t.side === "LONG" ? t.pnlPct : -t.pnlPct) / 100);
          const lo = Math.min(t.stop, t.target), hi = Math.max(t.stop, t.target);
          const progress = Math.max(0, Math.min(1, (live - lo) / (hi - lo)));

          return (
            <div key={t.id} className={`ap-trade status-${t.status}`}>
              <div className="ap-tr-top">
                <div className="ap-tr-id mono">{t.id}</div>
                <div className="ap-tr-status">
                  {t.status === "near_invalidation" && <span className="tr-warn">⚠ near stop</span>}
                  {t.status === "watch" && <span className="tr-warn warn">◌ watch</span>}
                  {t.status === "running" && <span className="tr-ok">● running</span>}
                </div>
              </div>

              <div className="ap-tr-grid">
                <div className="ap-tr-cell">
                  <div className="ap-tr-lbl mono">SIZE</div>
                  <div className="ap-tr-val mono">${t.sizeUsd.toLocaleString()}</div>
                  <div className="ap-tr-sub mono muted">{pctOfPort.toFixed(2)}% of port</div>
                </div>
                <div className="ap-tr-cell">
                  <div className="ap-tr-lbl mono">P&amp;L</div>
                  <div className={`ap-tr-val mono ${t.pnlUsd >= 0 ? "pos" : "neg"}`}>
                    {t.pnlUsd >= 0 ? "+" : ""}${t.pnlUsd.toLocaleString()}
                  </div>
                  <div className={`ap-tr-sub mono ${t.pnlPct >= 0 ? "pos" : "neg"}`}>
                    {t.pnlPct >= 0 ? "+" : ""}{t.pnlPct.toFixed(2)}%
                  </div>
                </div>
                <div className="ap-tr-cell">
                  <div className="ap-tr-lbl mono">AGE</div>
                  <div className="ap-tr-val mono">{t.ageDays}d</div>
                  <div className="ap-tr-sub mono muted">opened @ {t.scoreAtOpen}</div>
                </div>
                <div className="ap-tr-cell">
                  <div className="ap-tr-lbl mono">SCORE Δ</div>
                  <div className="ap-tr-val mono">{t.scoreNow}</div>
                  <div className={`ap-tr-sub mono ${scoreD > 0 ? "pos" : scoreD < 0 ? "neg" : "muted"}`}>
                    {scoreD > 0 ? "+" : ""}{scoreD} since open
                  </div>
                </div>
              </div>

              <div className="ap-tr-progress">
                <div className="ap-tr-prog-rail">
                  <div className="ap-tr-prog-stop"></div>
                  <div className="ap-tr-prog-fill" style={{ width: `${progress * 100}%` }}></div>
                  <div className="ap-tr-prog-here" style={{ left: `${progress * 100}%` }}></div>
                </div>
                <div className="ap-tr-prog-axis mono">
                  <span className="red">stop {t.stop < 1000 ? t.stop.toFixed(2) : t.stop.toLocaleString()}</span>
                  <span className="muted">entry {t.entry < 1000 ? t.entry.toFixed(2) : t.entry.toLocaleString()}</span>
                  <span className="green">target {t.target < 1000 ? t.target.toFixed(2) : t.target.toLocaleString()}</span>
                </div>
              </div>

              <div className="ap-tr-foot">
                <div className="ap-tr-mini mono">
                  <span className="muted">to stop</span> <span className="red">−{distToStop.toFixed(1)}%</span>
                </div>
                <div className="ap-tr-mini mono">
                  <span className="muted">to target</span> <span className="green">+{distToTgt.toFixed(1)}%</span>
                </div>
                <div className="ap-tr-mini mono">
                  <span className="muted">regime @ open</span> <span>{t.regimeAtOpen}</span>
                </div>
                <div className="ap-tr-mini-actions">
                  <button className="btn-ghost mono">trim ½</button>
                  <button className="btn-ghost mono">close</button>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ───────────────────────────────────────────────────────────────
// ReasoningTrail — body of the trail; can be embedded with or
// without its own header / footer (header is shown only when
// rendered standalone, e.g. the drill sheet).
// ───────────────────────────────────────────────────────────────
function ReasoningTrail({ signal, hideHeader = false, hideFooter = false }) {
  if (!signal) return null;
  const D = window.MA_DATA;
  const r = D.reasoning[signal.id] || D.reasoning["sig-ura-2605"]; // fallback to URA detail
  return (
    <div className="rt-content">
      {!hideHeader && (
        <>
          <div className="rt-card-head">
            <div className="rt-asset-block">
              <div className="rt-asset mono">{signal.asset}</div>
              <div className="rt-name">{signal.name}</div>
              <div className="rt-meta-row">
                <SideLabel side={signal.side} />
                <span className="rt-setup">{signal.setup}</span>
              </div>
            </div>
            <ScoreChip score={r.total} prev={signal.scorePrev} size="lg" />
          </div>
          <TierIndicator tier={r.tier} />

          {/* Levels recap (hidden when technical agent hasn't populated them yet) */}
          {(signal.entry || 0) > 0 && (signal.stop || 0) > 0 && (signal.target || 0) > 0 ? (
            <div className="rt-levels-grid">
              <div className="rt-l">
                <div className="rt-l-lbl">ENTRY</div>
                <div className="rt-l-val mono">{fmtPrice(signal.entry)}</div>
              </div>
              <div className="rt-l">
                <div className="rt-l-lbl">STOP</div>
                <div className="rt-l-val mono red">{fmtPrice(signal.stop)}</div>
              </div>
              <div className="rt-l">
                <div className="rt-l-lbl">TARGET</div>
                <div className="rt-l-val mono green">{fmtPrice(signal.target)}</div>
              </div>
              <div className="rt-l">
                <div className="rt-l-lbl">R/R</div>
                <div className="rt-l-val mono">{(signal.rr || 0).toFixed(2)}</div>
              </div>
            </div>
          ) : (
            <div className="sc-levels-pending mono small muted" style={{ marginBottom: 8 }}>
              {LEVELS_REASON[signal.levelsReason] || "no levels — awaiting next scoring pass"}
            </div>
          )}
        </>
      )}

      {/* A · Composite breakdown */}
      <div className="rt-section">
        <div className="rt-section-head">
          <span className="rt-section-num mono">A</span>
          <span>Composite breakdown</span>
          <span className="rt-section-sub">total {r.total} / 100</span>
        </div>
        <ScoreNote>{MA_GLOSSARY.composite.long}</ScoreNote>
        <div className="rt-bars">
          {r.components.map(c => (
            <SubScoreBar key={c.label} label={c.label} score={c.score}
                         max={c.max} color={c.color} note={componentNote(c.label)}
                         flat={c.flat} flatNote={c.flatNote} />
          ))}
        </div>
        <ScoreNote title={MA_GLOSSARY.modifiers.long}>{MA_GLOSSARY.modifiers.short}</ScoreNote>
        <div className="rt-modifiers">
          {r.modifiers.map(m => (
            <div key={m.label} className="rt-mod-row">
              <span className="rt-mod-lbl">{m.label}</span>
              <span className={`rt-mod-val mono ${m.value.startsWith("+") ? "pos" : m.value === "0" ? "muted" : "neg"}`}>
                {m.value}
              </span>
            </div>
          ))}
        </div>
      </div>

      {/* B · Why now */}
      <div className="rt-section">
        <div className="rt-section-head">
          <span className="rt-section-num mono">B</span>
          <span>Why this · why now</span>
        </div>
        <ul className="rt-why">
          {signal.whyNow.map((b, i) => <li key={i}>{b}</li>)}
        </ul>
      </div>

      {/* L · Level provenance — the reason behind every rail */}
      <LevelProvenance provenance={signal.levelProvenance} rejected={signal.levelRejected} />

      {/* S · Signals behind this asset (charts + drawn levels) */}
      <AssetSignalCalls calls={r.assetSignals} signal={signal} />

      {/* C · Sources */}
      <div className="rt-section">
        <div className="rt-section-head">
          <span className="rt-section-num mono">C</span>
          <span>Contributing sources</span>
          <span className="rt-section-sub">{r.sources.length} · weighted</span>
        </div>
        <div className="rt-sources">
          {r.sources.map(s => (
            <SourcePill key={s.name} name={s.name} weight={s.weight} freshness={s.freshness} contrib={s.contrib} />
          ))}
        </div>
      </div>

      {/* D · Theses */}
      <div className="rt-section">
        <div className="rt-section-head">
          <span className="rt-section-num mono">D</span>
          <span>Contributing theses</span>
        </div>
        <div className="rt-theses">
          {r.theses.map(t => (
            <div key={t.theme} className={`rt-thesis-row dir-${t.direction}`}>
              <span className={`rt-thesis-dot dir-${t.direction}`}></span>
              <span className="rt-thesis-theme mono">{t.theme}</span>
              <span className="rt-thesis-dir">{t.direction}</span>
              <span className="rt-thesis-conf mono">{Math.round(t.confidence * 100)}%</span>
            </div>
          ))}
        </div>
      </div>

      {/* E · Agent breakdown */}
      <div className="rt-section">
        <div className="rt-section-head">
          <span className="rt-section-num mono">E</span>
          <span>Agent-by-agent</span>
          <span className="rt-section-sub">brain calls behind this score</span>
        </div>
        <table className="wl-table dev-table">
          <thead>
            <tr>
              <th>AGENT</th>
              <th>MODEL</th>
              <th className="num">LATENCY</th>
              <th className="num">USD</th>
              <th>OK</th>
            </tr>
          </thead>
          <tbody>
            {r.agentBreakdown.map(a => (
              <tr key={a.agent}>
                <td className="mono">{a.agent}</td>
                <td className="mono muted">{a.model}</td>
                <td className="num mono">{a.latencyMs}<span className="muted">ms</span></td>
                <td className="num mono">${a.costUsd.toFixed(3)}</td>
                <td>{a.ok ? <span className="ok-dot ok"></span> : <span className="ok-dot fail"></span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* No footer action row: the funnel action lives once, at the top of
          the page, next to the score it is a decision about. A second copy
          down here was three more buttons that did nothing. */}
    </div>
  );
}

Object.assign(window, { ReasoningTrail, AssetPage, AssetTradeBreakdown });
