// /home — macro dashboard homepage.
//
// Top-of-desk overview: framework regime + macro indicators (compact) +
// a grid of asset categories (monetary, indices, crypto, commodities)
// each showing 4–6 tickers with price, day change, and sentiment. This
// is the macro-context landing page — positioning trades come after.

function Home({ onNav }) {
  const D = window.MA_DATA || {};
  const rawHome = D.macroHome || { categories: [] };
  const regime = D.regime || null;

  // Overlay REAL prices (D.livePrices, from the prices table + FRED) onto the
  // otherwise-mock macro tape. An asset with a live match shows its real value
  // + a "LIVE · as of <date>" stamp; everything else is flagged MOCK so a fake
  // number never masquerades as live. See build_live_prices_section.
  const livePrices = D.livePrices || {};
  const home = {
    ...rawHome,
    categories: (rawHome.categories || []).map(cat => ({
      ...cat,
      assets: (cat.assets || []).map(a => {
        const lp = livePrices[a.asset];
        return lp
          ? { ...a, value: lp.value, chgPct: lp.chgPct, chg30dPct: lp.chg30dPct, _live: lp }
          : { ...a, _mock: true };
      }),
    })),
  };

  return (
    <div className="home-view">
      {regime && <RegimeTape regime={regime} />}
      {regime && <RegimeQuadrantChart regime={regime} />}
      {regime && regime.indicators && (
        <MacroIndicatorStrip ind={regime.indicators} />
      )}

      <div className="home-meta mono small muted">
        Macro tape · <span className="home-live-dot" /> LIVE = real (prices + FRED) · MOCK = placeholder, not wired yet
      </div>

      <div className="home-cats">
        {home.categories.map(cat => (
          <HomeCategory key={cat.key} cat={cat} onNav={onNav} />
        ))}
      </div>
    </div>
  );
}

// Growth × Inflation quadrant — plots current macro position on a 2D
// grid where each quadrant maps to a framework regime, plus partial
// membership scores across all regimes (so we can see we're 45%
// commodity-led, 30% risk-on, etc. — the market is never one thing).
function RegimeQuadrantChart({ regime }) {
  const ind = regime.indicators || {};
  const framework = regime.framework || {};

  // Compute (x, y) in [-1..1]. x = inflation, y = growth.
  const _sig = (label) => {
    const s = (label || "").toLowerCase();
    if (s.includes("expan") || s.includes("stable") || s === "up") return 0.65;
    if (s.includes("hot") || s.includes("elevated") || s.includes("high")) return 0.85;
    if (s.includes("cool") || s.includes("moderate")) return 0.15;
    if (s.includes("contract") || s.includes("weak") || s === "down") return -0.65;
    if (s.includes("defla") || s.includes("low")) return -0.85;
    return 0;
  };
  // Prefer indicator strip's growth/inflation signals when present,
  // otherwise map from the framework regime itself.
  let inflX = _sig(ind.inflationSignal);
  let growY = _sig(ind.growthSignal);
  if (inflX === 0 && growY === 0) {
    const map = {
      commodity_led_inflation:       [ 0.7,  0.35],
      risk_on_expansion:             [-0.2,  0.7 ],
      risk_off_contraction:          [-0.4, -0.7 ],
      monetary_debasement_hard_asset:[ 0.6, -0.2 ],
      transitional_chop:             [ 0.05, 0.05],
    };
    const p = map[framework.slug] || [0, 0];
    inflX = p[0]; growY = p[1];
  }

  // Partial membership: distance from each regime's anchor → 1/(1+d).
  const anchors = [
    { slug: "risk_on_expansion",              label: "Risk-On Expansion",   x: -0.55, y:  0.7,  color: "var(--green, #25915d)" },
    { slug: "commodity_led_inflation",        label: "Commodity-Led Infl.", x:  0.7,  y:  0.4,  color: "var(--gold, #b0862f)" },
    { slug: "monetary_debasement_hard_asset", label: "Monetary Debasement", x:  0.65, y: -0.35, color: "var(--amber, #bb8d33)" },
    { slug: "risk_off_contraction",           label: "Risk-Off Contraction",x: -0.55, y: -0.7,  color: "var(--red, #cf5346)" },
    { slug: "transitional_chop",              label: "Transitional Chop",   x:  0,    y:  0,    color: "var(--text-mute-2, #8d897c)" },
  ];
  const anchorBySlug = Object.fromEntries(anchors.map(a => [a.slug, a]));

  // Build the historical trail: for each day in the trace, look up the
  // active framework regime (walk transitions), place the point near
  // that regime's anchor with deterministic jitter so the trail reads
  // as movement rather than a stack of dots at each anchor.
  const trace = regime.confidenceTrace || [];
  const dates = regime.confidenceTraceDates || [];
  const trans = (regime.transitions || []).slice().sort((a, b) => (a.date < b.date ? -1 : 1));

  // Reverse-map human labels → slugs (transitions carry human labels).
  const labelToSlug = {
    "Risk-On Expansion": "risk_on_expansion",
    "Commodity-Led Inflation": "commodity_led_inflation",
    "Monetary Debasement / Hard Asset": "monetary_debasement_hard_asset",
    "Risk-Off Contraction": "risk_off_contraction",
    "Transitional Chop": "transitional_chop",
  };
  const _slugAtDate = (isoDate) => {
    if (!trans.length) return framework.slug;
    if (isoDate < trans[0].date) return labelToSlug[trans[0].from] || framework.slug;
    let cur = labelToSlug[trans[0].to] || framework.slug;
    for (let i = 0; i < trans.length; i++) {
      if (isoDate >= trans[i].date) cur = labelToSlug[trans[i].to] || cur;
    }
    return cur;
  };
  // Simple deterministic hash → jitter in [-0.14, +0.14]
  const _jitter = (seed) => {
    let h = 0;
    for (let i = 0; i < seed.length; i++) h = ((h << 5) - h + seed.charCodeAt(i)) | 0;
    const jx = (((h & 0xffff) / 0xffff) - 0.5) * 0.28;
    const jy = ((((h >> 16) & 0xffff) / 0xffff) - 0.5) * 0.28;
    return [jx, jy];
  };
  const trailPoints = dates.length === trace.length && dates.length > 0
    ? dates.map((d, i) => {
        const slug = _slugAtDate(d);
        const a = anchorBySlug[slug] || anchorBySlug.transitional_chop;
        const [jx, jy] = _jitter(d);
        // Confidence tightens the point toward the anchor center.
        const tighten = 1 - Math.max(0, Math.min(1, trace[i])) * 0.4;
        return { date: d, x: a.x + jx * tighten, y: a.y + jy * tighten, color: a.color };
      })
    : [];
  const rawDist = anchors.map(a => Math.hypot(a.x - inflX, a.y - growY));
  const invD = rawDist.map(d => 1 / (0.35 + d * 1.2));
  const sum = invD.reduce((a, b) => a + b, 0) || 1;
  const membership = anchors.map((a, i) => ({ ...a, pct: invD[i] / sum }));
  membership.sort((a, b) => b.pct - a.pct);

  const W = 640, H = 420;
  const padL = 44, padR = 12, padT = 24, padB = 40;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const toX = (v) => padL + ((v + 1) / 2) * plotW;
  const toY = (v) => padT + ((1 - v) / 2) * plotH;

  return (
    <section className="block home-quadrant">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">RQ</span>
          <span>Regime map · growth × inflation</span>
          <span className="block-sub">
            partial membership across all regimes · single label is the dominant read, not the whole story
          </span>
        </div>
      </header>
      <div className="home-quadrant-wrap">
        <div className="home-quadrant-chart">
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" className="home-quadrant-svg">
            {/* Quadrant tints */}
            {anchors.slice(0, 4).map(a => {
              const cx = a.x > 0 ? padL + plotW * 0.5 : padL;
              const cy = a.y > 0 ? padT : padT + plotH * 0.5;
              return <rect key={a.slug} x={cx} y={cy} width={plotW * 0.5} height={plotH * 0.5} fill={a.color} opacity="0.06" />;
            })}
            {/* Axes */}
            <line x1={padL} y1={toY(0)} x2={padL + plotW} y2={toY(0)} stroke="var(--line-2)" strokeWidth="1" />
            <line x1={toX(0)} y1={padT} x2={toX(0)} y2={padT + plotH} stroke="var(--line-2)" strokeWidth="1" />
            {/* Axis labels */}
            <text x={padL + plotW - 4} y={toY(0) - 6} fontFamily="var(--mono)" fontSize="11" fill="var(--text-mute)" textAnchor="end">inflation ↑</text>
            <text x={padL + 4}       y={toY(0) - 6} fontFamily="var(--mono)" fontSize="11" fill="var(--text-mute)">← disinflation</text>
            <text x={toX(0) + 6}     y={padT + 12}  fontFamily="var(--mono)" fontSize="11" fill="var(--text-mute)">growth ↑</text>
            <text x={toX(0) + 6}     y={padT + plotH - 4} fontFamily="var(--mono)" fontSize="11" fill="var(--text-mute)">growth ↓</text>
            {/* Regime anchor labels */}
            {anchors.map(a => (
              <g key={a.slug}>
                <circle cx={toX(a.x)} cy={toY(a.y)} r="4" fill={a.color} opacity="0.55" />
                <text x={toX(a.x)} y={toY(a.y) - 10}
                      fontFamily="var(--sans)" fontSize="11" fontWeight="600"
                      fill={a.color} textAnchor="middle"
                      style={{ letterSpacing: "0.02em" }}>
                  {a.label}
                </text>
              </g>
            ))}
            {/* Historical trail — path connecting each day's position,
                colored by that day's regime. Fades in as time approaches now. */}
            {trailPoints.length > 1 && (() => {
              const segs = [];
              for (let i = 1; i < trailPoints.length; i++) {
                const p0 = trailPoints[i - 1], p1 = trailPoints[i];
                const t = i / (trailPoints.length - 1);        // 0..1 (old..new)
                const opacity = 0.10 + t * 0.55;               // fade in
                segs.push(
                  <line key={`s-${i}`}
                        x1={toX(p0.x)} y1={toY(p0.y)}
                        x2={toX(p1.x)} y2={toY(p1.y)}
                        stroke={p1.color} strokeWidth="1.4"
                        strokeLinecap="round" opacity={opacity} />
                );
              }
              return <g className="quadrant-trail">{segs}</g>;
            })()}
            {/* Small dot at each trail point so the path has texture */}
            {trailPoints.map((p, i) => {
              const t = i / Math.max(1, trailPoints.length - 1);
              return (
                <circle key={`d-${i}`}
                        cx={toX(p.x)} cy={toY(p.y)} r="1.6"
                        fill={p.color} opacity={0.15 + t * 0.55} />
              );
            })}
            {/* Date waypoints — 4 evenly-spaced labels along the trail */}
            {trailPoints.length >= 8 && (() => {
              const _fmt = (iso) => {
                const d = new Date(iso + "T00:00:00Z");
                return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
              };
              const marks = [];
              const N = 4;
              for (let k = 0; k < N; k++) {
                const idx = Math.round((k / (N - 1)) * (trailPoints.length - 1));
                if (k === N - 1) continue; // skip last — the NOW dot covers it
                const p = trailPoints[idx];
                marks.push(
                  <g key={`w-${k}`}>
                    <circle cx={toX(p.x)} cy={toY(p.y)} r="3" fill="var(--bg-card)" stroke={p.color} strokeWidth="1.4" />
                    <text x={toX(p.x)} y={toY(p.y) - 8}
                          fontFamily="var(--mono)" fontSize="10"
                          fill="var(--text-mute)" textAnchor="middle">
                      {_fmt(p.date)}
                    </text>
                  </g>
                );
              }
              return <g>{marks}</g>;
            })()}
            {/* Current position */}
            {(() => {
              const cx = toX(inflX), cy = toY(growY);
              return (
                <g>
                  <circle cx={cx} cy={cy} r="16" fill="var(--accent, #b0862f)" opacity="0.18" />
                  <circle cx={cx} cy={cy} r="8"  fill="var(--accent, #b0862f)" opacity="0.35" />
                  <circle cx={cx} cy={cy} r="4"  fill="var(--accent, #b0862f)" />
                  <text x={cx} y={cy + 22} fontFamily="var(--mono)" fontSize="11" fontWeight="700"
                        fill="var(--text)" textAnchor="middle">
                    NOW
                  </text>
                </g>
              );
            })()}
          </svg>
        </div>
        <div className="home-quadrant-panel">
          <div className="home-quadrant-panel-lbl mono small muted">Partial membership</div>
          {membership.map(m => (
            <div key={m.slug} className="home-membership-row">
              <div className="home-membership-head">
                <span className="home-membership-dot" style={{ background: m.color }}></span>
                <span className="home-membership-lbl" style={{ color: m.slug === framework.slug ? m.color : "var(--text)" }}>
                  {m.label}
                  {m.slug === framework.slug && <span className="mono small muted"> · dominant</span>}
                </span>
                <span className="home-membership-pct mono">{Math.round(m.pct * 100)}%</span>
              </div>
              <div className="home-membership-bar">
                <span style={{ width: `${m.pct * 100}%`, background: m.color }}></span>
              </div>
            </div>
          ))}
          <div className="home-quadrant-note mono small muted">
            position: growth {growY >= 0 ? "+" : ""}{growY.toFixed(2)} · inflation {inflX >= 0 ? "+" : ""}{inflX.toFixed(2)}
            {ind.quadrant && <> · quadrant read <b>{String(ind.quadrant).toUpperCase()}</b></>}
          </div>
        </div>
      </div>
    </section>
  );
}

// Regime timeline chart — horizontal band over the last N days of the
// confidence trace, with the trace overlaid as a line. Regime periods
// are inferred from `regime.transitions` (each entry is a change point).
function RegimeTimelineChart({ regime }) {
  const trace = regime.confidenceTrace || [];
  const n = trace.length;
  if (!n) return null;

  // Detect stub data: backend seeds confidenceTrace as [confidence]*84 until
  // a daily regime-snapshot table is wired up. If variance is ~0, don't
  // draw a misleading flat line — surface the pipeline gap instead.
  const traceMin = Math.min.apply(null, trace);
  const traceMax = Math.max.apply(null, trace);
  const traceIsStub = (traceMax - traceMin) < 0.02;

  if (traceIsStub) {
    return (
      <section className="block home-timeline">
        <header className="block-head">
          <div className="block-title">
            <span className="block-num mono">RG</span>
            <span>Regime timeline · last 90d</span>
            <span className="block-sub">
              current: <b>{regime.framework?.label || "—"}</b> · {Math.round((regime.framework?.confidence || 0) * 100)}% confidence
            </span>
          </div>
        </header>
        <div className="home-timeline-pending mono small muted">
          regime history not yet persisted — backend emits a flat confidence trace and one
          stub transition until a daily regime-snapshot writer lands (see
          <code> desk_data.build_regime_section</code>). The current framework read above is live;
          the 90-day chart will appear once daily snapshots accumulate.
        </div>
        {regime.transitions && regime.transitions.length > 0 && (
          <div className="home-timeline-transitions mono small muted">
            latest transition on record:&nbsp;
            <b>{regime.transitions[regime.transitions.length - 1].date}</b>
            {" · "}
            {regime.transitions[regime.transitions.length - 1].from}
            {" → "}
            {regime.transitions[regime.transitions.length - 1].to}
          </div>
        )}
      </section>
    );
  }

  const W = 900, H = 130;
  const padL = 8, padR = 8, padT = 28, padB = 22;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  // Build regime periods. When we have per-trace dates (real backend
  // path), map transition dates → trace indices for accurate segment
  // widths. Otherwise fall back to evenly-spaced anchors.
  const trans = (regime.transitions || []).slice();
  const dates = regime.confidenceTraceDates || [];
  const currentLabel = regime.framework?.label || "—";
  const segments = [];
  const _idxForDate = (d) => {
    if (!dates.length) return null;
    const t = new Date(d + "T00:00:00Z").getTime();
    let best = 0, bestDelta = Infinity;
    for (let i = 0; i < dates.length; i++) {
      const dt = Math.abs(new Date(dates[i] + "T00:00:00Z").getTime() - t);
      if (dt < bestDelta) { bestDelta = dt; best = i; }
    }
    return best;
  };
  if (!trans.length) {
    segments.push({ startIdx: 0, endIdx: n - 1, label: currentLabel });
  } else if (dates.length === n) {
    // Accurate: use each transition's actual date to place the boundary.
    let cursor = 0;
    trans.forEach((t) => {
      const end = _idxForDate(t.date);
      if (end != null && end > cursor) {
        segments.push({ startIdx: cursor, endIdx: end, label: t.from });
        cursor = end;
      }
    });
    segments.push({ startIdx: cursor, endIdx: n - 1, label: currentLabel });
  } else {
    // Fallback (no dates): evenly-spaced anchors as before.
    const anchors = trans.map((_, i) => Math.round(((i + 1) / (trans.length + 1)) * n));
    let cursor = 0;
    trans.forEach((t, i) => {
      const end = anchors[i];
      segments.push({ startIdx: cursor, endIdx: end, label: t.from });
      cursor = end;
    });
    segments.push({ startIdx: cursor, endIdx: n - 1, label: currentLabel });
  }

  const colorFor = (label) => {
    const s = (label || "").toLowerCase();
    if (s.includes("commodity")) return "var(--gold, #b0862f)";
    if (s.includes("dovish") || s.includes("liquidity")) return "var(--blue, #2b5ce6)";
    if (s.includes("risk-off") || s.includes("contraction")) return "var(--red, #cf5346)";
    if (s.includes("transitional") || s.includes("chop")) return "var(--text-mute-2, #8d897c)";
    return "var(--text-mute, #5f5c52)";
  };

  const pathD = trace.map((v, i) => {
    const x = padL + (i / (n - 1)) * plotW;
    const y = padT + (1 - v) * plotH;
    return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");

  // Current confidence — the last data point
  const currentConf = trace[n - 1];

  return (
    <section className="block home-timeline">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">RG</span>
          <span>Regime timeline · last 90d</span>
          <span className="block-sub">
            {dates.length === n
              ? "confidence trace overlaid on dated regime segments"
              : "confidence trace overlaid on approximate regime segmentation"}
            {" · "}{segments.length} regime{segments.length === 1 ? "" : "s"} · now
            {" "}<b>{Math.round(currentConf * 100)}%</b>
          </span>
        </div>
      </header>
      <div className="home-timeline-wrap">
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="home-timeline-svg">
          {/* Regime bands (background) */}
          {segments.map((seg, i) => {
            const x = padL + (seg.startIdx / (n - 1)) * plotW;
            const w = ((seg.endIdx - seg.startIdx) / (n - 1)) * plotW;
            const c = colorFor(seg.label);
            return (
              <g key={i}>
                <rect x={x} y={padT} width={Math.max(1, w)} height={plotH} fill={c} opacity="0.14" />
                <line x1={x} y1={padT} x2={x} y2={padT + plotH} stroke="var(--line-2)" strokeWidth="1" />
                <text x={x + 6} y={padT + 14} fontFamily="var(--mono)" fontSize="10.5" fill={c} fontWeight="600" style={{ letterSpacing: "0.04em" }}>
                  {seg.label.toUpperCase()}
                </text>
              </g>
            );
          })}
          {/* Confidence trace */}
          <path d={pathD} fill="none" stroke="var(--accent, #b0862f)" strokeWidth="1.75" strokeLinejoin="round" />
          {/* Endpoint dot */}
          {(() => {
            const x = padL + plotW;
            const y = padT + (1 - currentConf) * plotH;
            return <circle cx={x} cy={y} r="3.5" fill="var(--accent, #b0862f)" />;
          })()}
          {/* Y-axis ticks (0 / 50 / 100) */}
          {[0, 0.5, 1].map(v => {
            const y = padT + (1 - v) * plotH;
            return (
              <g key={v}>
                <line x1={padL} y1={y} x2={W - padR} y2={y} stroke="var(--line-soft)" strokeDasharray="2 3" />
                <text x={W - padR - 4} y={y - 3} fontFamily="var(--mono)" fontSize="9" fill="var(--text-mute-2)" textAnchor="end">
                  {Math.round(v * 100)}%
                </text>
              </g>
            );
          })}
          {/* X-axis: real date ticks at ~5 evenly-spaced trace indices */}
          {(() => {
            if (dates.length !== n) {
              return (
                <>
                  <text x={padL} y={H - 6} fontFamily="var(--mono)" fontSize="10" fill="var(--text-mute-2)">−{n - 1}d</text>
                  <text x={W - padR} y={H - 6} fontFamily="var(--mono)" fontSize="10" fill="var(--text-mute-2)" textAnchor="end">now</text>
                </>
              );
            }
            const ticks = 5;
            const _short = (iso) => {
              const d = new Date(iso + "T00:00:00Z");
              return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
            };
            const nodes = [];
            for (let t = 0; t < ticks; t++) {
              const idx = Math.round((t / (ticks - 1)) * (n - 1));
              const x = padL + (idx / (n - 1)) * plotW;
              const label = _short(dates[idx]);
              const anchor = t === 0 ? "start" : t === ticks - 1 ? "end" : "middle";
              nodes.push(
                <g key={t}>
                  <line x1={x} y1={padT + plotH} x2={x} y2={padT + plotH + 3} stroke="var(--text-mute-2)" strokeWidth="1" />
                  <text x={x} y={H - 6} fontFamily="var(--mono)" fontSize="10" fill="var(--text-mute-2)" textAnchor={anchor}>{label}</text>
                </g>
              );
            }
            return <>{nodes}</>;
          })()}
        </svg>
      </div>
      {regime.transitions && regime.transitions.length > 0 && (
        <div className="home-timeline-transitions mono small muted">
          {regime.transitions.map((t, i) => (
            <span key={i}>
              <b>{t.date}</b> · {t.from} → {t.to}
              {i < regime.transitions.length - 1 ? "   ·   " : ""}
            </span>
          ))}
        </div>
      )}
    </section>
  );
}

function HomeCategory({ cat, onNav }) {
  return (
    <section className="block home-cat-block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">{cat.key.slice(0, 2).toUpperCase()}</span>
          <span>{cat.label}</span>
          <span className="block-sub">{cat.blurb}</span>
        </div>
      </header>
      <div className="home-cat-grid">
        {(cat.assets || []).map(a => (
          <HomeAssetCard key={a.asset} a={a} />
        ))}
      </div>
    </section>
  );
}

function HomeAssetCard({ a }) {
  const chg = a.chgPct || 0;
  const chg30 = a.chg30dPct || 0;
  const dir = chg > 0 ? "pos" : chg < 0 ? "neg" : "muted";
  const dir30 = chg30 > 0 ? "pos" : chg30 < 0 ? "neg" : "muted";
  const sentimentClass = `home-sent home-sent-${a.sentiment || "neutral"}`;
  const isRate = (a.unit || "") === "%";
  const isPct = (a.unit || "") === "%" || (a.unit || "") === "z";
  const valStr = isRate
    ? `${a.value.toFixed(2)}%`
    : (a.unit ? `${_fmtValue(a.value)}${a.unit}` : _fmtValue(a.value));
  const live = a._live;
  const asOfLabel = live && live.asOf ? _agoLabel(live.asOf) : null;
  return (
    <div className="home-card">
      <div className="home-card-head">
        <span className="home-card-ticker mono">{a.asset}</span>
        <span className={sentimentClass}>{a.sentiment || "neutral"}</span>
      </div>
      <div className="home-card-name">{a.name}</div>
      <div className="home-card-val mono">{valStr}</div>
      <div className="home-card-chg mono">
        <span className={`home-chg ${dir}`}>{chg >= 0 ? "+" : ""}{chg.toFixed(isPct ? 3 : 2)}%</span>
        <span className="home-chg-sep muted">·</span>
        <span className={`home-chg-30 ${dir30}`}>30d {chg30 >= 0 ? "+" : ""}{chg30.toFixed(1)}%</span>
      </div>
      {/* Freshness stamp: real value → green LIVE + as-of; else MOCK. */}
      <div className="home-card-fresh mono small">
        {live ? (
          <span className="home-fresh-live" title={`live from prices/FRED · as of ${live.asOf}`}>
            ● LIVE{asOfLabel ? ` · ${asOfLabel}` : ""}
          </span>
        ) : (
          <span className="home-fresh-mock" title="placeholder — not wired to a live feed yet">
            ○ MOCK
          </span>
        )}
      </div>
      {/* Notes are hand-written mock narrative — only show on MOCK cards so a
          fabricated "spot ETF flows +$1.2B/wk" never sits beside a real price. */}
      {a.note && !live && <div className="home-card-note">{a.note}</div>}
    </div>
  );
}

// "2026-08-08" → "today" / "1d ago" / "3d ago" / "2026-08-08".
function _agoLabel(iso) {
  const t = Date.parse((iso || "").slice(0, 10));
  if (!t) return iso;
  const days = Math.floor((Date.now() - t) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "1d ago";
  if (days < 30) return `${days}d ago`;
  return (iso || "").slice(0, 10);
}

function _fmtValue(v) {
  if (v == null) return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  if (Math.abs(n) >= 10000) return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (Math.abs(n) >= 100)   return n.toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (Math.abs(n) >= 10)    return n.toFixed(2);
  return n.toFixed(3);
}

Object.assign(window, { Home });
