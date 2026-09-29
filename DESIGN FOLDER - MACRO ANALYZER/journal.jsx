// /journal — review desk view.

function Journal() {
  const D = window.MA_DATA;
  const [scope, setScope] = React.useState("30d");

  const closed = D.closedTrades;
  const wins = closed.filter(t => t.pnlPct > 0).length;
  const winRate = (wins / closed.length) * 100;
  const avgPnl = closed.reduce((s, t) => s + t.pnlPct, 0) / closed.length;
  const grossPos = closed.filter(t => t.pnlPct > 0).reduce((s, t) => s + t.pnlPct, 0);
  const grossNeg = Math.abs(closed.filter(t => t.pnlPct < 0).reduce((s, t) => s + t.pnlPct, 0));
  const profitFactor = grossNeg > 0 ? grossPos / grossNeg : 0;

  return (
    <div className="journal-view">
      <section className="block">
        <header className="block-head">
          <div className="block-title">
            <span className="block-num mono">J1</span>
            <span>Process scorecard · {D.processScorecard.days}d</span>
            <span className="block-sub">was the process clean — independent of outcome</span>
          </div>
          <div className="block-actions">
            <div className="filter-pill-row">
              {["7d","30d","90d","ALL"].map(s => (
                <button key={s} className={`filter-pill ${scope === s ? "on" : ""}`} onClick={() => setScope(s)}>{s}</button>
              ))}
            </div>
          </div>
        </header>
        <div className="ps-grid">
          <div className="ps-hero">
            <div className="ps-num mono gold">{D.processScorecard.score}</div>
            <div className="ps-num-lbl">PROCESS SCORE</div>
            <div className="ps-num-sub mono">+4 vs prior 30d</div>
          </div>
          <div className="ps-bars">
            {D.processScorecard.metrics.map(m => (
              <SubScoreBar key={m.label} label={m.label} score={m.value} max={m.of}
                           color={m.value >= 90 ? "green" : m.value >= 75 ? "amber" : "red"} />
            ))}
          </div>
          <div className="ps-aggs">
            <div className="ps-agg">
              <div className="ps-agg-lbl">WIN RATE</div>
              <div className="ps-agg-val mono">{winRate.toFixed(0)}%</div>
              <div className="ps-agg-sub muted">{wins} / {closed.length}</div>
            </div>
            <div className="ps-agg">
              <div className="ps-agg-lbl">AVG P&amp;L / TRADE</div>
              <div className={`ps-agg-val mono ${avgPnl >= 0 ? "pos" : "neg"}`}>{avgPnl >= 0 ? "+" : ""}{avgPnl.toFixed(2)}%</div>
            </div>
            <div className="ps-agg">
              <div className="ps-agg-lbl">PROFIT FACTOR</div>
              <div className="ps-agg-val mono">{profitFactor.toFixed(2)}</div>
            </div>
            <div className="ps-agg">
              <div className="ps-agg-lbl">SCORE↔OUTCOME ρ</div>
              <div className="ps-agg-val mono gold">+0.61</div>
              <div className="ps-agg-sub muted">framework predictive</div>
            </div>
          </div>
        </div>
      </section>

      <section className="two-col-7030">
        {/* Closed trades */}
        <div className="block">
          <header className="block-head sm">
            <div className="block-title">
              <span className="block-num mono">J2</span>
              <span>Closed trades</span>
              <span className="block-sub">last 10 · sorted by date</span>
            </div>
          </header>
          <table className="wl-table journal-table">
            <thead>
              <tr>
                <th>ASSET</th>
                <th>SIDE</th>
                <th className="num">P&amp;L</th>
                <th className="num">HOLD</th>
                <th className="num">SCORE@ENTRY</th>
                <th>REGIME@ENTRY</th>
                <th>THESIS?</th>
                <th>PLAN</th>
                <th>LESSON</th>
              </tr>
            </thead>
            <tbody>
              {closed.map(t => (
                <tr key={t.id}>
                  <td className="mono asset-cell">{t.asset}</td>
                  <td><SideLabel side={t.side} /></td>
                  <td className={`num ${t.pnlPct >= 0 ? "pos" : "neg"}`}>
                    {t.pnlPct >= 0 ? "+" : ""}{t.pnlPct.toFixed(2)}%
                  </td>
                  <td className="num muted">{t.holdDays}d</td>
                  <td className="num">
                    <span className={`wl-score tier-${t.scoreEntry >= 85 ? 1 : t.scoreEntry >= 70 ? 2 : 3}`}>
                      {t.scoreEntry}
                    </span>
                  </td>
                  <td className="muted small">{t.regimeEntry}</td>
                  <td>
                    <span className={`thesis-tag thesis-${t.thesis}`}>
                      {t.thesis === "yes" ? "✓ yes" : t.thesis === "no" ? "✕ no" : "◐ partial"}
                    </span>
                  </td>
                  <td>
                    <span className={`plan-tag ${t.planClean ? "clean" : "dirty"}`}>
                      {t.planClean ? "clean" : "dirty"}
                    </span>
                  </td>
                  <td className="lesson-cell">{t.lesson}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Missed trades */}
        <div className="block">
          <header className="block-head sm">
            <div className="block-title">
              <span className="block-num mono">J3</span>
              <span>Missed</span>
              <span className="block-sub">setups not taken</span>
            </div>
          </header>
          <div className="missed-list">
            {D.missedTrades.map(m => (
              <div key={m.asset} className="missed-row">
                <div className="missed-head">
                  <span className="mono asset-cell">{m.asset}</span>
                  <span className="wl-score tier-2">{m.scoreAtTime}</span>
                </div>
                <div className="missed-meta">
                  <span className={`missed-tag reason-${m.reason}`}>{m.reason.replace(/_/g, " ")}</span>
                  {m.validReal
                    ? <span className="missed-tag valid">valid in real time</span>
                    : <span className="missed-tag invalid muted">hindsight only</span>}
                </div>
                <div className="missed-lesson">{m.lesson}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="two-col-6040">
        {/* Source attribution leaderboard */}
        <div className="block">
          <header className="block-head sm">
            <div className="block-title">
              <span className="block-num mono">J4</span>
              <span>Source attribution · 30d</span>
              <span className="block-sub">which sources are earning their weight</span>
            </div>
          </header>
          <table className="wl-table">
            <thead>
              <tr>
                <th>SOURCE</th>
                <th className="num">WEIGHT</th>
                <th className="num">Δ 30D</th>
                <th className="num">ATTRIB</th>
                <th className="num">TRADES</th>
                <th>TAGS</th>
              </tr>
            </thead>
            <tbody>
              {D.sourceLeaderboard.map(s => (
                <tr key={s.name}>
                  <td className="src-name">{s.name}</td>
                  <td className="num mono"><WeightBar w={s.weight} /></td>
                  <td className={`num mono ${s.dWeight > 0 ? "pos" : s.dWeight < 0 ? "neg" : "muted"}`}>
                    {s.dWeight > 0 ? "+" : ""}{s.dWeight.toFixed(2)}
                  </td>
                  <td className={`num ${s.attribUsd >= 0 ? "pos" : "neg"}`}>
                    {s.attribUsd >= 0 ? "+" : ""}${(s.attribUsd / 1000).toFixed(2)}k
                  </td>
                  <td className="num muted">{s.trades}</td>
                  <td>
                    {s.tags.map(t => <span key={t} className="tag-chip">{t}</span>)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Thesis change log */}
        <div className="block">
          <header className="block-head sm">
            <div className="block-title">
              <span className="block-num mono">J5</span>
              <span>Thesis change log</span>
              <span className="block-sub">when the worldview shifted</span>
            </div>
          </header>
          <div className="changelog">
            {D.thesisChangelog.map((c, i) => (
              <div key={c.date} className="cl-row">
                <div className="cl-track">
                  <div className="cl-dot" style={{ background: i === 0 ? "var(--gold)" : "var(--text-mute-2)" }}></div>
                  {i < D.thesisChangelog.length - 1 && <div className="cl-line"></div>}
                </div>
                <div className="cl-body">
                  <div className="cl-head-row">
                    <span className="cl-date mono">{c.date}</span>
                    <span className="cl-vers mono">{c.from} → {c.to}</span>
                  </div>
                  <div className="cl-title">{c.title}</div>
                  <div className="cl-summary">{c.summary}</div>
                  {c.regimes.length > 0 && (
                    <div className="cl-regimes">
                      {c.regimes.map(r => (
                        <span key={r} className={`cl-regime ${r.startsWith("+") ? "added" : "removed"}`}>
                          {r}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}

function WeightBar({ w }) {
  return (
    <div className="weight-bar-cell">
      <div className="wb-track">
        <div className="wb-fill" style={{ width: `${w * 100}%` }}></div>
      </div>
      <span className="wb-num mono">{w.toFixed(2)}</span>
    </div>
  );
}

Object.assign(window, { Journal });
