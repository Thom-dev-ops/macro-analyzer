// Asset detail page + reasoning trail.

const TIER_LABELS = {
  1: { label: "TIER 1 · HIGH CONVICTION", color: "var(--gold)" },
  2: { label: "TIER 2 · QUALITY",         color: "var(--green)" },
  3: { label: "TIER 3 · PROBE",           color: "var(--amber)" },
  4: { label: "TIER 4 · AVOID",           color: "var(--red)" },
};

// ───────────────────────────────────────────────────────────────
// AssetPage — full page for a single signal/asset.
// ───────────────────────────────────────────────────────────────
function AssetPage({ signal, onBack, returnTo }) {
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onBack(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onBack]);
  if (!signal) return null;

  const D = window.MA_DATA;
  const r = D.reasoning[signal.id] || D.reasoning["sig-ura-2605"];
  const tier = r.tier ?? signal.tier;
  const total = r.total ?? signal.score;
  const series = D.priceSeries?.[signal.asset];
  const trades = (D.activeTrades || []).filter(t => t.asset === signal.asset);
  const portfolioEquity = D.portfolio?.equityUsd || 612400;
  const returnLabel = (returnTo || "positioning").toUpperCase();

  const distToStop  = ((signal.entry - signal.stop) / signal.entry) * 100;
  const upside      = ((signal.target - signal.entry) / signal.entry) * 100;
  const tInfo       = TIER_LABELS[tier];

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
          </div>

          <div className="ah-tier-block">
            <div className={`ah-tier-badge tier-${tier}`}>
              <span className="ah-tier-dot" style={{ background: tInfo.color }}></span>
              {tInfo.label}
            </div>
            <div className="ah-side-block">
              <span className={`ah-side-pill side-${signal.side.toLowerCase()}`}>{signal.side}</span>
              <span className="ah-rr mono">R/R · {signal.rr.toFixed(2)}</span>
            </div>
            <div className="ah-regime mono muted">
              regime fit · {signal.regimeFit?.replace(/_/g, " ") || "—"}
            </div>
          </div>
        </div>

        {/* Actions row — moved to the top */}
        <div className="ah-actions">
          <button className="btn-primary">Log this trade ↵</button>
          <button className="btn-secondary">Open chart_vision ⤴</button>
          <button className="btn-secondary">Raw feature vector</button>
          <span className="ah-actions-spacer"></span>
          <button className="btn-ghost mono">Add to watchlist +</button>
          <button className="btn-ghost mono">Share trail ↗</button>
        </div>
      </header>

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
            {series ? (
              <PriceChart
                series={series}
                entry={signal.entry}
                stop={signal.stop}
                target={signal.target}
                side={signal.side}
                height={260}
              />
            ) : (
              <div className="pc-empty mono muted">No price series for {signal.asset}</div>
            )}
          </div>
          <div className="ap-levels-strip">
            <div className="apl">
              <div className="apl-lbl mono">ENTRY</div>
              <div className="apl-val mono">{signal.entry < 1000 ? signal.entry.toFixed(2) : signal.entry.toLocaleString()}</div>
              <div className="apl-sub mono muted">trigger</div>
            </div>
            <div className="apl">
              <div className="apl-lbl mono">STOP</div>
              <div className="apl-val mono red">{signal.stop < 1000 ? signal.stop.toFixed(2) : signal.stop.toLocaleString()}</div>
              <div className="apl-sub mono red">−{distToStop.toFixed(1)}% to inval.</div>
            </div>
            <div className="apl">
              <div className="apl-lbl mono">TARGET</div>
              <div className="apl-val mono green">{signal.target < 1000 ? signal.target.toFixed(2) : signal.target.toLocaleString()}</div>
              <div className="apl-sub mono green">+{upside.toFixed(1)}% upside</div>
            </div>
            <div className="apl">
              <div className="apl-lbl mono">R / R</div>
              <div className="apl-val mono">{signal.rr.toFixed(2)}</div>
              <div className="apl-sub mono muted">{(upside / distToStop).toFixed(2)}× win/loss</div>
            </div>
          </div>
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
    const distToStop = Math.abs(signal.entry - signal.stop) / signal.entry;
    const riskUsd = Math.round(proposed * distToStop);
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
              <div className="ap-eg-val mono green">${Math.round(riskUsd * signal.rr).toLocaleString()}</div>
              <div className="ap-eg-sub mono muted">at target · R/R {signal.rr.toFixed(2)}</div>
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
// rendered standalone, e.g. the old drill sheet).
// ───────────────────────────────────────────────────────────────
function ReasoningTrail({ signal, hideHeader = false, hideFooter = false }) {
  if (!signal) return null;
  const D = window.MA_DATA;
  const r = D.reasoning[signal.id] || D.reasoning["sig-ura-2605"];
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

          <div className="rt-levels-grid">
            <div className="rt-l">
              <div className="rt-l-lbl">ENTRY</div>
              <div className="rt-l-val mono">{signal.entry < 1000 ? signal.entry.toFixed(2) : signal.entry.toLocaleString()}</div>
            </div>
            <div className="rt-l">
              <div className="rt-l-lbl">STOP</div>
              <div className="rt-l-val mono red">{signal.stop < 1000 ? signal.stop.toFixed(2) : signal.stop.toLocaleString()}</div>
            </div>
            <div className="rt-l">
              <div className="rt-l-lbl">TARGET</div>
              <div className="rt-l-val mono green">{signal.target < 1000 ? signal.target.toFixed(2) : signal.target.toLocaleString()}</div>
            </div>
            <div className="rt-l">
              <div className="rt-l-lbl">R/R</div>
              <div className="rt-l-val mono">{signal.rr.toFixed(2)}</div>
            </div>
          </div>
        </>
      )}

      {/* A · Composite breakdown */}
      <div className="rt-section">
        <div className="rt-section-head">
          <span className="rt-section-num mono">A</span>
          <span>Composite breakdown</span>
          <span className="rt-section-sub">total {r.total} / 100</span>
        </div>
        <div className="rt-bars">
          {r.components.map(c => (
            <SubScoreBar key={c.label} label={c.label} score={c.score} max={c.max} color={c.color} />
          ))}
        </div>
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

      {!hideFooter && (
        <div className="rt-footer">
          <button className="btn-primary">Log this trade ↵</button>
          <button className="btn-secondary">Open chart_vision ⤴</button>
          <button className="btn-secondary">Raw feature vector</button>
        </div>
      )}
    </div>
  );
}

Object.assign(window, { ReasoningTrail, AssetPage, AssetTradeBreakdown });
