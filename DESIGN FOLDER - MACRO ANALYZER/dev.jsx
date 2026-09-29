// /dev — system room view.

function Dev() {
  const D = window.MA_DATA;
  return (
    <div className="dev-view">

      {/* Mgmt panel */}
      <section className="block">
        <header className="block-head">
          <div className="block-title">
            <span className="block-num mono">D1</span>
            <span>Project management</span>
            <span className="block-sub">live to-do · decisions · recent commits</span>
          </div>
        </header>
        <div className="mgmt-grid">
          <div className="mgmt-col">
            <div className="mgmt-col-head">TO-DO</div>
            <div className="todo-list">
              {D.mgmt.todos.map((t, i) => (
                <div key={i} className={`todo-row status-${t.status}`}>
                  <span className={`todo-dot status-${t.status}`}></span>
                  <span className="todo-title">{t.title}</span>
                  <span className="todo-owner mono muted">{t.owner.replace(" Agent", "")}</span>
                  <span className="todo-age mono muted">{t.age}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="mgmt-col">
            <div className="mgmt-col-head">DECISIONS LOG</div>
            <div className="decisions-list">
              {D.mgmt.decisions.map((d, i) => (
                <div key={i} className="decision-row">
                  <div className="decision-head">
                    <span className="mono muted">{d.date}</span>
                    <span className="decision-who muted">{d.who}</span>
                  </div>
                  <div className="decision-title">{d.title}</div>
                </div>
              ))}
            </div>
          </div>
          <div className="mgmt-col">
            <div className="mgmt-col-head">COMMITS · LAST 5</div>
            <div className="commit-list">
              {D.mgmt.commits.map(c => (
                <div key={c.hash} className="commit-row">
                  <span className="commit-hash mono">{c.hash}</span>
                  <span className="commit-msg">{c.msg}</span>
                  <span className="commit-author mono muted">{c.author}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section className="two-col-6040">
        {/* Brain activity */}
        <div className="block">
          <header className="block-head sm">
            <div className="block-title">
              <span className="block-num mono">D2</span>
              <span>Brain activity</span>
              <span className="block-sub">last {D.brainActivity.length} calls · refresh 1min</span>
            </div>
            <div className="block-actions">
              <span className="dot-live"></span>
              <span className="block-meta mono">spend today: <b>${D.costTracker.today.toFixed(2)}</b> / ${D.costTracker.capDaily}</span>
            </div>
          </header>
          <table className="wl-table dev-table">
            <thead>
              <tr>
                <th className="num">TS</th>
                <th>AGENT</th>
                <th>MODEL</th>
                <th className="num">LATENCY</th>
                <th className="num">TOK IN</th>
                <th className="num">TOK OUT</th>
                <th className="num">USD</th>
                <th>OK</th>
              </tr>
            </thead>
            <tbody>
              {D.brainActivity.map((c, i) => (
                <tr key={i}>
                  <td className="num mono muted">{c.ts}</td>
                  <td className="mono">{c.agent}</td>
                  <td className="mono muted">{c.model}</td>
                  <td className="num mono">{c.latencyMs}<span className="muted">ms</span></td>
                  <td className="num mono">{c.tokensIn.toLocaleString()}</td>
                  <td className="num mono">{c.tokensOut.toLocaleString()}</td>
                  <td className="num mono">${c.costUsd.toFixed(3)}</td>
                  <td>
                    {c.ok ? <span className="ok-dot ok"></span> : <span className="ok-dot fail"></span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Cost tracker */}
        <div className="block">
          <header className="block-head sm">
            <div className="block-title">
              <span className="block-num mono">D3</span>
              <span>Cost tracker</span>
              <span className="block-sub">LLM spend</span>
            </div>
          </header>
          <div className="cost-grid">
            <div className="cost-stat">
              <div className="cost-stat-lbl">TODAY</div>
              <div className="cost-stat-val mono">${D.costTracker.today.toFixed(2)}</div>
              <CostMeter spent={D.costTracker.today} cap={D.costTracker.capDaily} />
            </div>
            <div className="cost-stat">
              <div className="cost-stat-lbl">7D</div>
              <div className="cost-stat-val mono">${D.costTracker.week.toFixed(2)}</div>
              <div className="cost-stat-sub muted mono">avg ${(D.costTracker.week / 7).toFixed(2)}/d</div>
            </div>
            <div className="cost-stat">
              <div className="cost-stat-lbl">30D</div>
              <div className="cost-stat-val mono">${D.costTracker.month.toFixed(2)}</div>
              <CostMeter spent={D.costTracker.month} cap={D.costTracker.capMonthly} />
            </div>
          </div>
          <div className="cost-section-head">BY AGENT · TODAY</div>
          <div className="cost-bars">
            {D.costTracker.byAgent.map(a => {
              const max = Math.max(...D.costTracker.byAgent.map(x => x.usd));
              return (
                <div key={a.agent} className="cb-row">
                  <span className="cb-lbl mono">{a.agent}</span>
                  <div className="cb-track">
                    <div className="cb-fill" style={{ width: `${(a.usd / max) * 100}%` }}></div>
                  </div>
                  <span className="cb-val mono">${a.usd.toFixed(2)}</span>
                </div>
              );
            })}
          </div>
          <div className="cost-section-head">BY BACKEND</div>
          <div className="cost-bars">
            {D.costTracker.byBackend.map(b => {
              const max = Math.max(...D.costTracker.byBackend.map(x => x.usd));
              return (
                <div key={b.backend} className="cb-row">
                  <span className="cb-lbl">{b.backend}</span>
                  <div className="cb-track">
                    <div className="cb-fill" style={{ width: `${(b.usd / max) * 100}%`, background: "var(--gold)" }}></div>
                  </div>
                  <span className="cb-val mono">${b.usd.toFixed(2)}</span>
                </div>
              );
            })}
          </div>
        </div>
      </section>

      {/* Source health */}
      <section className="block">
        <header className="block-head sm">
          <div className="block-title">
            <span className="block-num mono">D4</span>
            <span>Source health</span>
            <span className="block-sub">freshness · weight · 30d attribution</span>
          </div>
          <div className="block-actions">
            <span className={`int-pill ${D.integration.tactical.connected ? "ok" : "off"}`}>
              {D.integration.tactical.connected ? "● tactical online" : "○ tactical offline"}
              <span className="muted mono"> · contract {D.integration.tactical.contractVersion} · {D.integration.tactical.mode}</span>
            </span>
          </div>
        </header>
        <table className="wl-table dev-table">
          <thead>
            <tr>
              <th>SOURCE</th>
              <th>KIND</th>
              <th className="num">LAST FETCH</th>
              <th>FRESHNESS</th>
              <th className="num">WEIGHT</th>
              <th className="num">ATTRIB 30D</th>
              <th>TAGS</th>
            </tr>
          </thead>
          <tbody>
            {D.sourceHealth.map(s => (
              <tr key={s.name}>
                <td className="src-name">{s.name}</td>
                <td className="muted small">{s.kind}</td>
                <td className="num mono muted">{s.lastFetch}</td>
                <td>
                  <FreshnessIndicator value={s.freshness} />
                </td>
                <td className="num"><WeightBar w={s.weight} /></td>
                <td className={`num ${s.attrib30d >= 0 ? "pos" : "neg"}`}>
                  {s.attrib30d >= 0 ? "+" : ""}${(s.attrib30d / 1000).toFixed(2)}k
                </td>
                <td>
                  {s.tags.map(t => <span key={t} className="tag-chip">{t}</span>)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

    </div>
  );
}

function CostMeter({ spent, cap }) {
  const pct = Math.min((spent / cap) * 100, 100);
  const warn = pct > 80;
  return (
    <div className="cost-meter">
      <div className="cm-track">
        <div className={`cm-fill ${warn ? "warn" : ""}`} style={{ width: `${pct}%` }}></div>
      </div>
      <span className="cm-cap mono muted">${cap} cap · {pct.toFixed(0)}%</span>
    </div>
  );
}

function FreshnessIndicator({ value }) {
  const cls = value > 0.85 ? "fresh" : value > 0.5 ? "stale" : "cold";
  const label = value > 0.85 ? "fresh" : value > 0.5 ? "stale" : "cold";
  return (
    <div className="freshness">
      <span className={`fr-dot ${cls}`}></span>
      <span className={`fr-lbl ${cls}`}>{label}</span>
      <span className="fr-num mono muted">{value.toFixed(2)}</span>
    </div>
  );
}

Object.assign(window, { Dev });
