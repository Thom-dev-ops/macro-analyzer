// /positioning — trader desk view.

const { useState: useStateP, useMemo: useMemoP } = React;

function Positioning({ onOpenReasoning, onOpenTradeForm }) {
  const D = window.MA_DATA;
  const [filter, setFilter] = useStateP("ALL");
  const [sortBy, setSortBy] = useStateP("score");
  const [sortDir, setSortDir] = useStateP("desc");

  // ── Watchlist filtering + sorting ────────────────────────────
  const watchlist = useMemoP(() => {
    let rows = D.watchlist.slice();
    if (filter === "ACTIONABLE") rows = rows.filter(r => r.score >= 70);
    if (filter === "T1") rows = rows.filter(r => r.tier === 1);
    if (filter === "LONG") rows = rows.filter(r => r.side === "LONG");
    if (filter === "SHORT") rows = rows.filter(r => r.side === "SHORT");
    rows.sort((a, b) => {
      const av = a[sortBy], bv = b[sortBy];
      if (typeof av === "number") return sortDir === "desc" ? bv - av : av - bv;
      return sortDir === "desc" ? String(bv).localeCompare(String(av)) : String(av).localeCompare(String(bv));
    });
    return rows;
  }, [filter, sortBy, sortDir]);

  const sortToggle = (col) => () => {
    if (sortBy === col) setSortDir(sortDir === "desc" ? "asc" : "desc");
    else { setSortBy(col); setSortDir("desc"); }
  };

  return (
    <div className="positioning-view">

      {/* ── Regime tape (sticky) ─────────────────────────── */}
      <RegimeTape regime={D.regime} />

      {/* KPI strip rendered by app shell — single source of truth */}

      {/* ── Hero signals ────────────────────────────────── */}
      <section className="block">
        <header className="block-head">
          <div className="block-title">
            <span className="block-num mono">01</span>
            <span>Hero signals</span>
            <span className="block-sub">today's highest-conviction setups · click for reasoning trail</span>
          </div>
          <div className="block-actions">
            <span className="dot-live"></span>
            <span className="block-meta mono">refreshed 08:14:22 · next 08:29</span>
          </div>
        </header>
        <div className="hero-grid">
          {D.heroSignals.slice(0, 3).map(s => (
            <SetupCard key={s.id} s={s} onOpen={onOpenReasoning} />
          ))}
        </div>
        <div className="hero-grid hero-grid-2">
          {D.heroSignals.slice(3).map(s => (
            <SetupCard key={s.id} s={s} onOpen={onOpenReasoning} />
          ))}
        </div>
      </section>

      {/* ── Watchlist + Active trades two-col ───────────── */}
      <section className="two-col">
        {/* Watchlist scored table */}
        <div className="block">
          <header className="block-head">
            <div className="block-title">
              <span className="block-num mono">02</span>
              <span>Watchlist · scored</span>
              <span className="block-sub">{watchlist.length} of {D.watchlist.length} · sortable</span>
            </div>
            <div className="block-actions">
              <div className="filter-pill-row">
                {["ALL","ACTIONABLE","T1","LONG","SHORT"].map(f => (
                  <button key={f} className={`filter-pill ${filter === f ? "on" : ""}`}
                          onClick={() => setFilter(f)}>{f}</button>
                ))}
              </div>
            </div>
          </header>
          <table className="wl-table">
            <thead>
              <tr>
                <th onClick={sortToggle("asset")}>ASSET</th>
                <th>SIDE</th>
                <th onClick={sortToggle("score")} className="sortable num">
                  SCORE {sortBy === "score" && (sortDir === "desc" ? "↓" : "↑")}
                </th>
                <th onClick={sortToggle("dScore")} className="sortable num">Δ 1D</th>
                <th>TIER</th>
                <th>REGIME</th>
                <th className="num">TECH</th>
                <th className="num">VOL</th>
                <th onClick={sortToggle("rr")} className="sortable num">R/R</th>
                <th className="num">LAST</th>
              </tr>
            </thead>
            <tbody>
              {watchlist.map(r => (
                <tr key={r.asset} className={`tier-row tier-${r.tier}`}>
                  <td className="mono asset-cell">{r.asset}</td>
                  <td><SideLabel side={r.side} /></td>
                  <td className="num">
                    <span className={`wl-score tier-${r.tier}`}>{r.score}</span>
                  </td>
                  <td className={`num ${r.dScore > 0 ? "pos" : r.dScore < 0 ? "neg" : "muted"}`}>
                    {r.dScore > 0 ? "+" : ""}{r.dScore}
                  </td>
                  <td><span className={`tier-dot tier-${r.tier}`}></span><span className="tier-num mono">T{r.tier}</span></td>
                  <td>
                    <span className={`reg-fit reg-${r.regime}`}>
                      {r.regime === "fit" ? "● fit" : r.regime === "mix" ? "◐ mixed" : "○ off"}
                    </span>
                  </td>
                  <td className="num">{r.tech}</td>
                  <td className="num">{r.vol}</td>
                  <td className="num">{r.rr.toFixed(2)}</td>
                  <td className="num muted">{r.last}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Right column: Active trades + Trade log */}
        <div className="right-col">
          <ActiveTradesPanel trades={D.activeTrades} />
          <TradeLogPanel onSubmit={onOpenTradeForm} />
        </div>
      </section>

    </div>
  );
}

// ── Regime tape ────────────────────────────────────────────────
function RegimeTape({ regime }) {
  const f = regime.framework, t = regime.thesis;
  const color = f.slug === "commodity_led_inflation" ? "var(--gold)"
              : f.slug === "dovish_liquidity_wave" ? "var(--accent)"
              : f.slug === "risk_off_contraction" ? "var(--red)"
              : "var(--text-dim)";
  return (
    <section className="regime-tape">
      <div className="rt-left">
        <div className="rt-kind">FRAMEWORK REGIME</div>
        <div className="rt-name">
          <span className="rt-dot" style={{ background: color }}></span>
          {f.label}
        </div>
        <div className="rt-meta">
          <span>active {f.sinceDays}d</span>
          <span className="sep">·</span>
          <span>bias <b>{f.bias.replace(/_/g, " ")}</b></span>
          <span className="sep">·</span>
          <span>size mod <b className="mono">×{f.sizingModifier.toFixed(2)}</b></span>
          <span className="sep">·</span>
          <span>score mod <b className="mono">{f.scoreModifier > 0 ? "+" : ""}{f.scoreModifier}</b></span>
        </div>
      </div>

      <div className="rt-mid">
        <div className="rt-kind">THESIS · {t.version} · {t.author}</div>
        <div className="rt-thesis">{t.narrative}</div>
        <div className="rt-meta muted">last revised {t.lastRevised}</div>
      </div>

      <div className="rt-right">
        <div className="rt-conf">
          <div className="rt-conf-label">CONFIDENCE 90D</div>
          <div className="rt-conf-row">
            <span className="rt-conf-num mono gold">{Math.round(f.confidence * 100)}<span className="muted">%</span></span>
            <Sparkline data={regime.confidenceTrace} width={140} height={36} color={color} />
          </div>
          <div className="rt-tx muted mono">
            {regime.transitions[regime.transitions.length - 1].date} ·
            {" "}{regime.transitions[regime.transitions.length - 1].from} → {regime.transitions[regime.transitions.length - 1].to}
          </div>
        </div>
      </div>
    </section>
  );
}

// ── Active trades ──────────────────────────────────────────────
function ActiveTradesPanel({ trades }) {
  return (
    <div className="block">
      <header className="block-head sm">
        <div className="block-title">
          <span className="block-num mono">03</span>
          <span>Active trades</span>
          <span className="block-sub">{trades.length} open · refreshed 5min</span>
        </div>
      </header>
      <div className="trades-list">
        {trades.map(t => {
          const distToStop = t.side === "LONG"
            ? ((t.entry - t.stop) / t.entry) * 100
            : ((t.stop - t.entry) / t.entry) * 100;
          const scoreD = t.scoreNow - t.scoreAtOpen;
          return (
            <div key={t.id} className={`trade-row status-${t.status}`}>
              <div className="tr-asset">
                <div className="tr-asset-name mono">{t.asset}</div>
                <SideLabel side={t.side} />
              </div>
              <div className="tr-levels">
                <div className="tr-l">
                  <span className="tr-l-lbl">entry</span>
                  <span className="mono">{t.entry < 1000 ? t.entry.toFixed(2) : t.entry.toLocaleString()}</span>
                </div>
                <div className="tr-l">
                  <span className="tr-l-lbl">stop</span>
                  <span className="mono">{t.stop < 1000 ? t.stop.toFixed(2) : t.stop.toLocaleString()}</span>
                </div>
                <div className="tr-l">
                  <span className="tr-l-lbl">target</span>
                  <span className="mono">{t.target < 1000 ? t.target.toFixed(2) : t.target.toLocaleString()}</span>
                </div>
              </div>
              <div className="tr-score">
                <span className="tr-l-lbl">score</span>
                <div>
                  <span className="mono muted">{t.scoreAtOpen}→</span>
                  <span className="mono">{t.scoreNow}</span>
                  <span className={`mono ${scoreD > 0 ? "pos" : scoreD < 0 ? "neg" : "muted"}`}>
                    {" "}{scoreD > 0 ? "+" : ""}{scoreD}
                  </span>
                </div>
              </div>
              <div className="tr-pnl">
                <PnL usd={t.pnlUsd} pct={t.pnlPct} size="sm" />
                <span className="tr-age muted mono">{t.ageDays}d</span>
              </div>
              <div className="tr-status">
                {t.status === "near_invalidation" && <span className="tr-warn">⚠ near stop</span>}
                {t.status === "watch" && <span className="tr-warn warn">◌ watch</span>}
                {t.status === "running" && <span className="tr-ok">● running</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Trade log inline form ──────────────────────────────────────
function TradeLogPanel({ onSubmit }) {
  const [tab, setTab] = useStateP("log");
  const [asset, setAsset] = useStateP("URA");
  const [entry, setEntry] = useStateP("");
  const [stop, setStop] = useStateP("");
  const [size, setSize] = useStateP("");
  const [submitted, setSubmitted] = useStateP(false);

  const handleSubmit = (e) => {
    e.preventDefault();
    setSubmitted(true);
    setTimeout(() => setSubmitted(false), 1800);
  };

  return (
    <div className="block">
      <header className="block-head sm">
        <div className="block-title">
          <span className="block-num mono">04</span>
          <span>Trade log</span>
          <span className="block-sub">manual entry · one-tap fast</span>
        </div>
        <div className="tab-row">
          <button className={`tab ${tab === "log" ? "on" : ""}`} onClick={() => setTab("log")}>Log entry</button>
          <button className={`tab ${tab === "close" ? "on" : ""}`} onClick={() => setTab("close")}>Close trade</button>
        </div>
      </header>
      <form className="log-form" onSubmit={handleSubmit}>
        {tab === "log" ? (
          <>
            <div className="form-row">
              <label>
                <span className="form-lbl">ASSET</span>
                <input className="form-input mono" value={asset} onChange={e => setAsset(e.target.value.toUpperCase())} placeholder="URA" />
              </label>
              <label>
                <span className="form-lbl">SIDE</span>
                <div className="seg">
                  <button type="button" className="seg-on">LONG</button>
                  <button type="button">SHORT</button>
                </div>
              </label>
            </div>
            <div className="form-row">
              <label>
                <span className="form-lbl">ENTRY</span>
                <input className="form-input mono" value={entry} onChange={e => setEntry(e.target.value)} placeholder="41.20" inputMode="decimal" />
              </label>
              <label>
                <span className="form-lbl">STOP</span>
                <input className="form-input mono" value={stop} onChange={e => setStop(e.target.value)} placeholder="39.40" inputMode="decimal" />
              </label>
              <label>
                <span className="form-lbl">SIZE $</span>
                <input className="form-input mono" value={size} onChange={e => setSize(e.target.value)} placeholder="32000" inputMode="decimal" />
              </label>
            </div>
            <div className="form-row">
              <label className="grow">
                <span className="form-lbl">LINK SETUP (optional)</span>
                <select className="form-input">
                  <option>sig-ura-2605 · URA · 88</option>
                  <option>sig-gld-2605 · GLD · 84</option>
                  <option>sig-xop-2605 · XOP · 78</option>
                  <option>(none)</option>
                </select>
              </label>
            </div>
          </>
        ) : (
          <>
            <div className="form-row">
              <label className="grow">
                <span className="form-lbl">CLOSE WHICH TRADE</span>
                <select className="form-input">
                  <option>t-2026-019 · URA · +6.42%</option>
                  <option>t-2026-018 · GLD · +2.75%</option>
                  <option>t-2026-017 · XOP · +1.81%</option>
                </select>
              </label>
              <label>
                <span className="form-lbl">EXIT</span>
                <input className="form-input mono" placeholder="42.10" inputMode="decimal" />
              </label>
            </div>
            <div className="form-row">
              <label>
                <span className="form-lbl">WAS IT THE THESIS?</span>
                <div className="seg seg-3">
                  <button type="button" className="seg-on">YES</button>
                  <button type="button">PARTIAL</button>
                  <button type="button">NO</button>
                </div>
              </label>
            </div>
            <div className="form-row">
              <label className="grow">
                <span className="form-lbl">LESSON · ONE LINE</span>
                <input className="form-input" placeholder="Held through 50DMA wobble — paid off." />
              </label>
            </div>
          </>
        )}
        <div className="form-actions">
          <button type="submit" className="btn-primary">{tab === "log" ? "Log trade ↵" : "Close & log lesson ↵"}</button>
          {submitted && <span className="form-ok">✓ logged · brain weights updating</span>}
        </div>
      </form>
    </div>
  );
}

Object.assign(window, { Positioning, RegimeTape });
