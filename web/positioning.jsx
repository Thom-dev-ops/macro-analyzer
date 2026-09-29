// /positioning — trader desk view.

const { useState: useStateP, useMemo: useMemoP, useEffect: useEffectP } = React;

// ── Asset-class buckets ────────────────────────────────────────
// The scored watchlist mixes on-chain assets, exchange-listed tickers
// (single names, miners, commodity ETFs, bond/cash proxies) and two pure
// indices that can't be traded directly. Those read differently at the
// desk, so the table is bucketed rather than shown as one ranked list.
const _WL_CRYPTO = new Set(["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "AVAX", "LINK", "DOGE", "TON", "SUI", "DOT"]);
const _WL_MACRO  = new Set(["DXY", "VIX"]);

function assetGroup(r) {
  const cls = (r.assetClass || "").toLowerCase();
  const t = (r.asset || "").toUpperCase();
  if (cls === "crypto" || _WL_CRYPTO.has(t)) return "crypto";
  if (cls === "vol" || _WL_MACRO.has(t)) return "macro";
  return "stocks";  // equity, index, commodity/miner + bond/cash ETFs
}

const _WL_GROUPS = [
  ["stocks", "Stocks & ETFs"],
  ["crypto", "Crypto"],
  ["macro",  "Macro indices"],
];

function Positioning({ onOpenReasoning, onOpenTradeForm, onAdvanceToConcept }) {
  const D = window.MA_DATA;
  const [filter, setFilter] = useStateP("ALL");
  const [tierFilter, setTierFilter] = useStateP("ALL");
  const [regimeFilter, setRegimeFilter] = useStateP("ALL");
  const [classFilter, setClassFilter] = useStateP("ALL");
  const [wlQ, setWlQ] = useStateP("");
  const [sortBy, setSortBy] = useStateP("score");
  const [sortDir, setSortDir] = useStateP("desc");
  const [, force] = useStateP(0);
  const rerender = () => force(n => n + 1);

  // Map of asset → active concept id so the watchlist row can show
  // "marked" instead of the "advance" button after a click. Recomputed
  // on every render so concept retirement on /concepts is reflected here.
  const activeConceptByAsset = useMemoP(() => {
    const m = {};
    for (const c of (D.concepts || [])) {
      if (c.status === "active") m[c.asset] = c.id;
    }
    return m;
  }, [D.concepts && D.concepts.length, sortBy, sortDir, filter]);

  const advanceToConcept = (row) => {
    if (activeConceptByAsset[row.asset]) {
      // Already marked — just navigate.
      if (onAdvanceToConcept) onAdvanceToConcept();
      return;
    }
    markConcept({
      asset: row.asset,
      side: row.side,
      score: row.score,
      tier: row.tier,
      source: "watchlist_manual",
    }).then(() => {
      rerender();
      if (onAdvanceToConcept) onAdvanceToConcept();
    });
  };

  // ── Watchlist filtering + sorting ────────────────────────────
  const watchlist = useMemoP(() => {
    let rows = D.watchlist.slice();
    if (filter === "ACTIONABLE") rows = rows.filter(r => r.score >= 70);
    if (filter === "T1") rows = rows.filter(r => r.tier === 1);
    if (filter === "LONG") rows = rows.filter(r => r.side === "LONG");
    if (filter === "SHORT") rows = rows.filter(r => r.side === "SHORT");
    if (tierFilter !== "ALL") rows = rows.filter(r => String(r.tier) === tierFilter.replace("T", ""));
    if (regimeFilter !== "ALL") rows = rows.filter(r => r.regime === regimeFilter);
    if (classFilter !== "ALL") rows = rows.filter(r => assetGroup(r) === classFilter);
    if (wlQ.trim()) {
      const q = wlQ.toLowerCase();
      rows = rows.filter(r =>
        r.asset.toLowerCase().includes(q) ||
        (r.name || "").toLowerCase().includes(q) ||
        (r.assetClass || "").toLowerCase().includes(q)
      );
    }
    rows.sort((a, b) => {
      const av = a[sortBy], bv = b[sortBy];
      if (typeof av === "number") return sortDir === "desc" ? bv - av : av - bv;
      return sortDir === "desc" ? String(bv).localeCompare(String(av)) : String(av).localeCompare(String(bv));
    });
    return rows;
  }, [filter, tierFilter, regimeFilter, classFilter, wlQ, sortBy, sortDir]);

  // Split the (already filtered + sorted) rows into asset-class buckets so
  // crypto and listed tickers are scanned separately instead of interleaved.
  const wlGroups = useMemoP(() => {
    const g = { stocks: [], crypto: [], macro: [] };
    for (const r of watchlist) g[assetGroup(r)].push(r);
    return g;
  }, [watchlist]);

  const sortToggle = (col) => () => {
    if (sortBy === col) setSortDir(sortDir === "desc" ? "asc" : "desc");
    else { setSortBy(col); setSortDir("desc"); }
  };

  return (
    <div className="positioning-view">

      {/* ── Regime tape (sticky) ─────────────────────────── */}
      <RegimeTape regime={D.regime} />

      {/* ── Macro indicator tiles ────────────────────────── */}
      {D.regime.indicators && <MacroIndicatorStrip ind={D.regime.indicators} />}

      {/* ── Data-freshness strip ─────────────────────────── */}
      {D.dataHealth && D.dataHealth.sources && D.dataHealth.sources.length > 0 && (
        <DataFreshnessStrip dh={D.dataHealth} />
      )}

      {/* KPI strip rendered by app shell — single source of truth */}

      {/* ── Live signals (direct from extractors, last 72h) ─ */}
      {Array.isArray(D.liveSignals) && D.liveSignals.length > 0 && (
        <LiveSignalsPanel signals={D.liveSignals}
                          onAdvanceToConcept={onAdvanceToConcept} />
      )}

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
        <div className="hero-grid hero-grid-4">
          {D.heroSignals.slice(0, 4).map(s => (
            <SetupCard key={s.id} s={s} onOpen={onOpenReasoning} />
          ))}
        </div>
      </section>

      {/* ── Watchlist (active trades + log moved to /live) ─ */}
      <section>
        <div className="block">
          <header className="block-head">
            <div className="block-title">
              <span className="block-num mono">02</span>
              <span>Watchlist · scored</span>
              <span className="block-sub">
                {watchlist.length} of {D.watchlist.length}
                {" · "}
                {_WL_GROUPS.filter(([k]) => wlGroups[k].length)
                           .map(([k, lbl]) => `${wlGroups[k].length} ${lbl.toLowerCase()}`)
                           .join(" · ")}
                {(filter !== "ALL" || tierFilter !== "ALL" || regimeFilter !== "ALL" || classFilter !== "ALL" || wlQ.trim()) ? " · filtered" : ""}
                {" · sort "}{sortBy} {sortDir === "desc" ? "↓" : "↑"}
              </span>
            </div>
            <div className="block-actions wl-actions">
              <input className="src-search" placeholder="search ticker / name…"
                     value={wlQ} onChange={e => setWlQ(e.target.value)} />
              <div className="filter-pill-row" title="asset class">
                {[["ALL","all"],["stocks","stocks"],["crypto","crypto"],["macro","macro"]].map(([k, lbl]) => (
                  <button key={k} className={`filter-pill ${classFilter === k ? "on" : ""}`}
                          onClick={() => setClassFilter(k)}>{lbl}</button>
                ))}
              </div>
              <div className="filter-pill-row" title="side / posture">
                {["ALL","ACTIONABLE","LONG","SHORT"].map(f => (
                  <button key={f} className={`filter-pill ${filter === f ? "on" : ""}`}
                          onClick={() => setFilter(f)}>{f}</button>
                ))}
              </div>
              <div className="filter-pill-row" title="tier">
                {["ALL","T1","T2","T3","T4"].map(f => (
                  <button key={f} className={`filter-pill ${tierFilter === f ? "on" : ""}`}
                          onClick={() => setTierFilter(f)}>{f}</button>
                ))}
              </div>
              <div className="filter-pill-row" title="regime fit">
                {[["ALL","regime: all"],["fit","fit"],["mix","mixed"],["off","off"]].map(([k, lbl]) => (
                  <button key={k} className={`filter-pill ${regimeFilter === k ? "on" : ""}`}
                          onClick={() => setRegimeFilter(k)}>{lbl}</button>
                ))}
              </div>
            </div>
          </header>
          <table className="wl-table">
            <thead>
              <tr>
                <th onClick={sortToggle("asset")} className="sortable">ASSET {sortBy === "asset" && (sortDir === "desc" ? "↓" : "↑")}</th>
                <th onClick={sortToggle("side")} className="sortable" title={MA_GLOSSARY.terms.side}>SIDE {sortBy === "side" && (sortDir === "desc" ? "↓" : "↑")}</th>
                <th onClick={sortToggle("score")} className="sortable num" title={MA_GLOSSARY.composite.long}>
                  SCORE {sortBy === "score" && (sortDir === "desc" ? "↓" : "↑")}
                </th>
                <th onClick={sortToggle("dScore")} className="sortable num" title={MA_GLOSSARY.terms.dScore}>Δ 1D {sortBy === "dScore" && (sortDir === "desc" ? "↓" : "↑")}</th>
                <th onClick={sortToggle("tier")} className="sortable" title={MA_GLOSSARY.tier.long}>TIER {sortBy === "tier" && (sortDir === "desc" ? "↓" : "↑")}</th>
                <th onClick={sortToggle("regime")} className="sortable" title={MA_GLOSSARY.terms.regime}>REGIME {sortBy === "regime" && (sortDir === "desc" ? "↓" : "↑")}</th>
                <th onClick={sortToggle("tech")} className="sortable num" title={MA_GLOSSARY.terms.tech}>TECH {sortBy === "tech" && (sortDir === "desc" ? "↓" : "↑")}</th>
                <th onClick={sortToggle("vol")} className="sortable num" title={MA_GLOSSARY.terms.vol}>VOL {sortBy === "vol" && (sortDir === "desc" ? "↓" : "↑")}</th>
                <th onClick={sortToggle("rr")} className="sortable num" title={MA_GLOSSARY.terms.rr}>R/R {sortBy === "rr" && (sortDir === "desc" ? "↓" : "↑")}</th>
                <th onClick={sortToggle("last")} className="sortable num">LAST {sortBy === "last" && (sortDir === "desc" ? "↓" : "↑")}</th>
                <th>FUNNEL</th>
              </tr>
            </thead>
            <tbody>
              {_WL_GROUPS.map(([gkey, glabel]) => {
                const rows = wlGroups[gkey];
                if (!rows.length) return null;
                return (
                <React.Fragment key={gkey}>
                  <tr className="wl-group-head">
                    <td colSpan={11}>
                      <span className={`wl-group-tag wl-group-${gkey}`}>{glabel}</span>
                      <span className="wl-group-count mono">{rows.length}</span>
                    </td>
                  </tr>
                  {rows.map(r => {
                const marked = activeConceptByAsset[r.asset];
                return (
                <tr key={r.asset}
                    className={`tier-row tier-${r.tier} wl-clickable`}
                    onClick={() => onOpenReasoning(r)}
                    title={`Open ${r.asset} detail`}>
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
                  <td>
                    {marked
                      ? <span className="status-chip status-active">● marked</span>
                      : <button className="btn-ghost xs"
                                onClick={(e) => { e.stopPropagation(); advanceToConcept(r); }}>
                          advance →
                        </button>}
                  </td>
                </tr>
                );
                  })}
                </React.Fragment>
                );
              })}
            </tbody>
          </table>
          {/* Legend — what each column means, so the table is readable
              without going and looking the model up. */}
          <div className="wl-legend">
            <span><b>SCORE</b> {MA_GLOSSARY.composite.short} · 0–100</span>
            <span><b>Δ 1D</b> {MA_GLOSSARY.terms.dScore}</span>
            <span><b>TIER</b> {MA_GLOSSARY.tier.short}</span>
            <span><b>REGIME</b> {MA_GLOSSARY.terms.regime}</span>
            <span><b>TECH / VOL</b> letter grade of that component's share of its weight</span>
            <span><b>R/R</b> {MA_GLOSSARY.terms.rr}</span>
          </div>
        </div>

      </section>

    </div>
  );
}

// ── Macro indicator strip ──────────────────────────────────────
const _QUADRANT_COLOR = {
  boom:        "var(--gold)",
  goldilocks:  "var(--green)",
  stagflation: "var(--gold)",
  deflation:   "var(--red)",
  transitional:"var(--text-dim)",
};
const _FCI_COLOR = { tightening: "var(--red)", neutral: "var(--text-dim)", easing: "var(--green)" };
const _EPU_COLOR  = { elevated: "var(--red)", moderate: "var(--text-dim)", low: "var(--green)" };
const _COT_COLOR  = {
  extreme_long:  "var(--green)",
  elevated:      "var(--gold)",
  neutral:       "var(--text-dim)",
  suppressed:    "var(--gold)",
  extreme_short: "var(--red)",
};

function MacroIndicatorStrip({ ind }) {
  return (
    <div className="indicator-strip">
      <div className="ind-tile">
        <div className="ind-label">REGIME QUADRANT</div>
        <div className="ind-value" style={{ color: _QUADRANT_COLOR[ind.quadrant] || "var(--text)" }}>
          {(ind.quadrant || "—").toUpperCase()}
        </div>
        <div className="ind-sub">
          {ind.growthSignal} growth · {ind.inflationSignal} inflation ·{" "}
          <span className="mono">{ind.quadrantConf ? Math.round(ind.quadrantConf * 100) + "%" : "—"}</span> conf
        </div>
      </div>
      <div className="ind-tile">
        <div className="ind-label">FIN. CONDITIONS</div>
        <div className="ind-value" style={{ color: _FCI_COLOR[ind.fciLabel] || "var(--text)" }}>
          {(ind.fciLabel || "—").toUpperCase()}
        </div>
        <div className="ind-sub">
          FCI <span className="mono">{ind.fciScore != null ? (ind.fciScore >= 0 ? "+" : "") + ind.fciScore.toFixed(3) : "—"}</span>
        </div>
      </div>
      <div className="ind-tile">
        <div className="ind-label">GEO / POLICY RISK</div>
        <div className="ind-value" style={{ color: _EPU_COLOR[ind.epuLevel] || "var(--text)" }}>
          {(ind.epuLevel || "—").toUpperCase()}
        </div>
        <div className="ind-sub">
          EPU <span className="mono">{ind.epuComposite != null ? ind.epuComposite.toFixed(0) : "—"}</span>
          {ind.epuDriver ? <span className="muted"> · {ind.epuDriver.replace("EPU", "")}</span> : null}
        </div>
      </div>
      <div className="ind-tile">
        <div className="ind-label">COT POSITIONING</div>
        <div className="ind-value" style={{ color: _COT_COLOR[ind.cotTopSignal] || "var(--text-dim)" }}>
          {ind.cotTopSignal ? ind.cotTopSignal.replace(/_/g, " ").toUpperCase() : "—"}
        </div>
        <div className="ind-sub">
          <span className="mono">{ind.cotExtremesCount != null ? ind.cotExtremesCount : "—"}</span> extreme{ind.cotExtremesCount !== 1 ? "s" : ""}
          {ind.cotTopMarket ? <span className="muted"> · {ind.cotTopMarket}</span> : null}
          {ind.cotTopNetPctOi != null ? <span className="muted"> {ind.cotTopNetPctOi >= 0 ? "+" : ""}{ind.cotTopNetPctOi.toFixed(1)}%</span> : null}
        </div>
      </div>
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

// ── Data freshness strip ────────────────────────────────────────────────
// One chip per input/output source showing green/yellow/red based on
// how long since the last update. Lets the operator spot a stalled
// pipeline at a glance — no more silent-zero dashboards.
function DataFreshnessStrip({ dh }) {
  const sources = dh.sources || [];
  function fmtAge(min) {
    if (min == null) return "—";
    if (min < 60) return `${Math.round(min)}m`;
    if (min < 24 * 60) return `${Math.round(min / 60)}h`;
    return `${Math.round(min / (60 * 24))}d`;
  }
  const colorFor = {
    green: { bg: "rgba(60,160,80,0.15)", fg: "#7dd87d", dot: "#4caf50" },
    yellow: { bg: "rgba(200,160,40,0.18)", fg: "#e0c060", dot: "#e0a020" },
    red: { bg: "rgba(200,80,80,0.18)", fg: "#e07070", dot: "#cc3030" },
  };
  return (
    <div className="data-freshness-strip" style={{
      display: "flex", flexWrap: "wrap", gap: 8,
      padding: "8px 14px", marginBottom: 12,
      fontSize: 11, fontFamily: "monospace",
      borderTop: "1px solid #222", borderBottom: "1px solid #222",
    }}>
      <span style={{ color: "#888", marginRight: 8 }}>SOURCES ·</span>
      {sources.map(s => {
        const c = colorFor[s.status] || colorFor.red;
        return (
          <span key={s.key} title={s.last_ts || "never"}
                style={{
                  background: c.bg, color: c.fg,
                  padding: "2px 8px", borderRadius: 3,
                  display: "inline-flex", alignItems: "center", gap: 5,
                }}>
            <span style={{
              width: 6, height: 6, borderRadius: "50%",
              background: c.dot, display: "inline-block",
            }} />
            {s.label}
            <span style={{ opacity: 0.7 }}>
              · {fmtAge(s.minutes_since)}
              {s.docs_today > 0 ? ` · ${s.docs_today} today` : ""}
            </span>
          </span>
        );
      })}
    </div>
  );
}

// ── Live signals panel ──────────────────────────────────────────────────
// Reads directly from the signals table — visible BEFORE a scoring pass
// rolls them into trade_scores. So the positioning desk surfaces real
// extracted intelligence within seconds of ingestion.
// A live call carries its own thesis and levels — seed the concept with
// them so step ② starts from what the author actually said, not a blank.
function conceptSeedFromSignal({ ticker, side, conviction, score, thesis_summary,
                                 stop_loss, target_1, target_2, who, when }) {
  const levels = [
    stop_loss != null ? `stop ${stop_loss}` : null,
    target_1 != null ? `target ${target_1}` : null,
    target_2 != null ? `/ ${target_2}` : null,
  ].filter(Boolean).join(" ");
  const thesis = [
    thesis_summary,
    levels || null,
    who ? `— ${who}${when ? `, ${when}` : ""}` : null,
  ].filter(Boolean).join("\n");
  return {
    asset: ticker,
    side,
    // No score: the watchlist's score_at_mark is a 0-100 composite, and a
    // 0-5 conviction is not the same measurement. It travels in the
    // reason line instead, where its unit is stated.
    score: null,
    thesis,
    source: "live_signal",
    reason: `live signal · ${who || "unknown source"} · conviction ${
      conviction != null ? conviction.toFixed(1) : "—"}/5${
      score != null ? ` · weighted ${score}` : ""}`,
  };
}

// ── Mark a concept (funnel step ②) ──────────────────────────────────────
// Writes through to /api/funnel/concepts so the mark survives a reload,
// and mirrors the row into MA_DATA in the client shape /concepts renders.
// The POST de-dupes server-side: marking an asset that already has an
// active concept returns that concept instead of a second row.
async function markConcept({ asset, side, score, tier, thesis, source, reason }) {
  const local = {
    id: `concept-${Date.now().toString(36)}`,
    asset,
    source: source || "watchlist_manual",
    status: "active",
    suggestedBySystem: false,
    suggestionReason: reason || null,
    scoreAtMark: score != null ? score : null,
    tierAtMark: tier != null ? tier : null,
    sideAtMark: side || null,
    thesis: thesis || "",
    markedAt: new Date().toISOString().slice(0, 16).replace("T", " "),
    tradePlanId: null,
  };
  let deduped = false;
  // Whether the row actually reached `trade_concepts`. A non-2xx response
  // is NOT a thrown error, so the old code fell straight past `if (r.ok)`
  // and reported success on a 500 — the funnel said "marked" while the
  // write had failed, and the mark vanished on reload.
  let persisted = false;
  try {
    const r = await fetch("/api/funnel/concepts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        asset_id: asset,
        source: local.source,
        thesis_text: local.thesis,
        score_at_mark: local.scoreAtMark,
        tier_at_mark: local.tierAtMark != null ? String(local.tierAtMark) : null,
        side_at_mark: local.sideAtMark,
        suggestion_reason: local.suggestionReason,
      }),
    });
    if (r.ok) {
      const j = await r.json();
      const c = j.concept || {};
      deduped = !!j.deduped;
      local.id = c.concept_id || local.id;
      local.markedAt = (c.marked_at || local.markedAt).replace("T", " ").slice(0, 16);
      if (deduped) local.thesis = c.thesis_text || local.thesis;
      persisted = true;
    } else {
      console.warn("markConcept: server rejected the write", r.status);
    }
  } catch (e) {
    // Offline / static preview: keep the optimistic row so the funnel
    // still moves. It just won't survive the next reload.
  }
  const D = window.MA_DATA;
  const existing = (D.concepts || []).find(
    c => c.asset === asset && c.status === "active"
  );
  if (existing) {
    Object.assign(existing, { id: local.id, thesis: existing.thesis || local.thesis });
  } else {
    D.concepts = (D.concepts || []).concat([local]);
  }
  return { concept: local, deduped: deduped || !!existing, persisted };
}

// Tradeable directions a plan can actually carry.
function normalisePlanSide(...candidates) {
  for (const c of candidates) {
    const v = String(c || "").toUpperCase();
    if (v === "LONG" || v === "SHORT") return v;
  }
  return "LONG";
}

// ── Promote a concept to a draft plan (funnel step ③) ───────────────────
// Writes through to /api/funnel/plans, then mirrors the row into MA_DATA in
// the client shape /identify renders. Before this existed the SPA created
// plans in memory only: /concepts built the row, /identify rendered it, and
// a reload threw it away — the funnel looked like it moved and hadn't.
//
// Returns {plan, persisted}. `persisted:false` means the optimistic row is
// all there is (offline, or a static preview), so callers can say so rather
// than implying the plan is safe.
async function promoteConceptToPlan(concept, { seed } = {}) {
  const now = new Date().toISOString().slice(0, 16).replace("T", " ");
  const local = {
    id: `plan-${Date.now().toString(36)}`,
    conceptId: concept.id,
    asset: concept.asset,
    // A plan needs a tradeable direction. Tier-3 names carry side "WATCH"
    // and tier-avoid carry "AVOID" — real reads, but not something you can
    // place. Prefer the technical agent's own direction, then fall back to
    // LONG; the plan is a draft and /identify is where the side is set.
    side: normalisePlanSide(
      concept.sideAtMark, (seed && seed.side), (seed && seed.levelSide)
    ),
    entry: (seed && seed.entry) ?? null,
    stop: (seed && seed.stop) ?? null,
    targets: (seed && seed.targets) || [],
    sizeUsd: null,
    sizeR: 1.0,
    timeHorizon: "swing",
    thesis: concept.thesis || (seed && seed.thesis) || "",
    invalidation: "",
    gateStatus: "unchecked",
    status: "draft",
    tradeId: null,
    createdAt: now,
    activatedAt: null,
  };

  let persisted = false;
  try {
    const r = await fetch("/api/funnel/plans", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        asset_id: local.asset,
        side: local.side,
        concept_id: concept.id,
        entry: local.entry,
        stop: local.stop,
        targets: local.targets,
        size_r: local.sizeR,
        time_horizon: local.timeHorizon,
        thesis: local.thesis,
      }),
    });
    if (r.ok) {
      const j = await r.json();
      const pl = j.plan || {};
      if (pl.plan_id) {
        local.id = pl.plan_id;
        local.createdAt = (pl.created_at || local.createdAt).replace("T", " ").slice(0, 16);
        persisted = true;
      }
    }
  } catch (e) {
    // Offline / static preview: keep the optimistic row so the funnel
    // still moves. It just won't survive the next reload.
  }

  const D = window.MA_DATA;
  D.plans = (D.plans || []).concat([local]);
  concept.status = "promoted";
  concept.promotedAt = now;
  concept.tradePlanId = local.id;

  // Keep the server's concept row in step — without this the concept
  // reappears as "active" on the next load and can be promoted twice.
  if (persisted) {
    try {
      await fetch(`/api/funnel/concepts/${encodeURIComponent(concept.id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "promoted", trade_plan_id: local.id }),
      });
    } catch (e) { /* the plan is what matters; status catches up on reload */ }
  }
  return { plan: local, persisted };
}

// Age of a timestamp, in the shortest unit that still reads precisely.
function sigAge(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    const m = (Date.now() - d.getTime()) / 60000;
    if (m < 60) return `${Math.round(m)}m`;
    if (m < 24 * 60) return `${Math.round(m / 60)}h`;
    return `${Math.round(m / (60 * 24))}d`;
  } catch (e) { return "—"; }
}

// Absolute wall-clock stamp, local time, for hover titles and the modal.
function sigStamp(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso).replace("T", " ").slice(0, 16);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
           ` ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch (e) { return ""; }
}

function LiveSignalsPanel({ signals, onAdvanceToConcept }) {
  // signal_id of the card being drilled into — the modal fetches the full
  // provenance chain (chart, caption, extractor, model) on open.
  const [openId, setOpenId] = useStateP(null);
  // ticker → concept id for marks made this session, so a card can say
  // "marked" instead of offering the same mark twice.
  const [marked, setMarked] = useStateP({});
  const fmtAge = sigAge;

  const conceptFor = (ticker) => {
    if (marked[ticker]) return marked[ticker];
    const c = (window.MA_DATA.concepts || []).find(
      x => x.asset === ticker && x.status === "active"
    );
    return c ? c.id : null;
  };

  const toConcept = (s, { navigate = true } = {}) => {
    const who = s.source_channel || s.source_slug;
    return markConcept(conceptSeedFromSignal({
      ticker: s.ticker,
      side: s.side,
      conviction: s.conviction,
      score: s.weighted_score,
      thesis_summary: s.thesis_summary,
      stop_loss: s.stop_loss, target_1: s.target_1, target_2: s.target_2,
      who,
      when: sigStamp(s.published_at || s.extracted_at),
    })).then(({ concept }) => {
      setMarked(m => ({ ...m, [s.ticker]: concept.id }));
      if (navigate && onAdvanceToConcept) onAdvanceToConcept();
      return concept;
    });
  };
  const sideKind = (side) => {
    const s = (side || "").toUpperCase();
    if (s === "LONG" || s === "ADD") return "long";
    if (s === "SHORT" || s === "HEDGE") return "short";
    if (s === "EXIT" || s === "TRIM") return "trim";
    return "watch";
  };
  return (
    <section className="block live-signals-block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">01a</span>
          <span>Live signals</span>
          <span className="block-sub">
            {signals.length} most recent · last 7d ·
            direct from manual / insider / vision / LLM extractors ·
            click a card for its source
          </span>
        </div>
      </header>
      <div className="live-signals-grid">
        {signals.map(s => (
          <div key={s.signal_id}
               className={`live-signal-card side-${sideKind(s.side)}`}
               role="button" tabIndex={0}
               title="open source — chart, caption, extractor"
               onClick={() => setOpenId(s.signal_id)}
               onKeyDown={(e) => {
                 if (e.key === "Enter" || e.key === " ") {
                   e.preventDefault();
                   setOpenId(s.signal_id);
                 }
               }}>
            <div className="ls-head">
              <span className="ls-ticker">{s.ticker}</span>
              <span className={`ls-side ls-side-${sideKind(s.side)} mono`}>
                {s.side} · conv {s.conviction != null ? s.conviction.toFixed(1) : "—"}
              </span>
            </div>
            <div className="ls-meta mono">
              {[
                s.extractor_name,
                s.source_channel || s.source_slug,
                s.horizon,
                s.catalyst_type,
              ].filter(Boolean).join(" · ")}
            </div>
            <div className="ls-when mono"
                 title={`called ${sigStamp(s.published_at || s.extracted_at)}`
                        + (s.published_at ? ` · extracted ${sigStamp(s.extracted_at)}` : "")}>
              {s.published_at
                ? `called ${sigStamp(s.published_at)} · ${fmtAge(s.published_at)} ago`
                : `extracted ${sigStamp(s.extracted_at)} · ${fmtAge(s.extracted_at)} ago`}
            </div>
            {s.thesis_summary && (
              <div className="ls-thesis">{s.thesis_summary}</div>
            )}
            {(s.stop_loss != null || s.target_1 != null) && (
              <div className="ls-levels mono">
                {s.stop_loss != null && <>stop {s.stop_loss}</>}
                {s.stop_loss != null && s.target_1 != null && " · "}
                {s.target_1 != null && <>tgt {s.target_1}</>}
                {s.target_2 != null && <> / {s.target_2}</>}
              </div>
            )}
            <div className="ls-actions">
              <span className="ls-drill mono">source ↗</span>
              {conceptFor(s.ticker) ? (
                <span className="ls-marked mono" title="already an active concept">
                  ✓ concept
                </span>
              ) : (
                <button className="ls-mark mono"
                        title="mark as a concept (funnel step ②)"
                        onClick={(e) => { e.stopPropagation(); toConcept(s); }}>
                  → concept
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
      {openId && (
        <SignalProvenanceModal
          signalId={openId}
          onClose={() => setOpenId(null)}
          onOpenSignal={setOpenId}
          conceptFor={conceptFor}
          onMarkConcept={(sig, doc) => toConcept({
            ticker: sig.asset_ticker,
            side: sig.side,
            conviction: sig.conviction,
            weighted_score: sig.weighted_score,
            thesis_summary: sig.thesis_summary,
            stop_loss: sig.stop_loss,
            target_1: sig.target_1,
            target_2: sig.target_2,
            source_channel: sig.source_channel,
            source_slug: sig.source_slug,
            published_at: doc && doc.published_at,
            extracted_at: sig.extracted_at,
          }, { navigate: false })}
        />
      )}
    </section>
  );
}

// ── Signal provenance modal ─────────────────────────────────────────────
// "Where is this coming from?" — the full chain behind one live signal:
//   source channel → author → document (chart + caption) → extractor/model
// Fetched from /api/signals/{id}/provenance on open.
function SignalProvenanceModal({ signalId, onClose, onOpenSignal,
                                 conceptFor, onMarkConcept }) {
  const [data, setData] = useStateP(null);
  const [err, setErr] = useStateP(null);
  const [marking, setMarking] = useStateP(false);

  useEffectP(() => {
    let live = true;
    setData(null); setErr(null);
    fetch(`/api/signals/${signalId}/provenance`)
      .then(r => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(j => { if (live) setData(j); })
      .catch(e => { if (live) setErr(e.message || "failed to load"); });
    return () => { live = false; };
  }, [signalId]);

  useEffectP(() => {
    function onKey(e) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const sig = data && data.signal;
  const doc = data && data.document;
  const feat = data && data.features;
  const author = data && data.author;
  const images = (doc && doc.images) || [];
  // Chart extractors carry their per-chart read on the signal itself.
  const detail = (sig && sig.instrument_detail) || {};
  const setups = (feat && Array.isArray(feat.setups)) ? feat.setups : [];

  const num = (v) => (v === null || v === undefined || v === "" ? null : String(v));
  const Row = ({ k, v }) => (v === null || v === undefined || v === "" ? null : (
    <div className="sp-row">
      <span className="sp-k mono">{k}</span>
      <span className="sp-v">{v}</span>
    </div>
  ));

  return (
    <div className="sp-backdrop" onClick={onClose}>
      <div className="sp-modal" onClick={(e) => e.stopPropagation()}>
        <div className="sp-head">
          <strong>{sig ? sig.asset_ticker : "…"}</strong>
          {sig && (
            <span className="mono sp-head-meta">
              {sig.side} · conv {sig.conviction != null ? sig.conviction.toFixed(1) : "—"}
              {sig.weighted_score != null ? ` · score ${sig.weighted_score}` : ""}
              {sig.status && sig.status !== "active" ? ` · ${sig.status}` : ""}
            </span>
          )}
          {sig && (doc || sig.extracted_at) && (
            <span className="mono sp-head-when"
                  title="when the author posted it — the extraction clock below can lag by days">
              called {sigStamp((doc && doc.published_at) || sig.extracted_at)}
              {" · "}{sigAge((doc && doc.published_at) || sig.extracted_at)} ago
            </span>
          )}
          <div className="sp-head-actions">
            {sig && onMarkConcept && (
              conceptFor && conceptFor(sig.asset_ticker) ? (
                <span className="mono sp-marked" title="already an active concept">
                  ✓ concept
                </span>
              ) : (
                <button className="filter-pill sp-mark mono"
                        disabled={marking}
                        title="mark as a concept (funnel step ②)"
                        onClick={() => {
                          setMarking(true);
                          Promise.resolve(onMarkConcept(sig, doc))
                            .finally(() => setMarking(false));
                        }}>
                  {marking ? "marking…" : "→ concept"}
                </button>
              )
            )}
            <button className="filter-pill sp-close" onClick={onClose}>✕ close</button>
          </div>
        </div>

        {err && <div className="sp-body sp-empty">Could not load provenance: {err}</div>}
        {!err && !data && <div className="sp-body sp-empty">Loading source…</div>}

        {data && (
          <div className="sp-body">
            {/* ── Evidence column: what the extractor actually saw ── */}
            <div className="sp-media">
              {images.length > 0 ? images.map((im, i) => (
                <figure key={i} className="sp-figure">
                  <a href={im.url} target="_blank" rel="noreferrer">
                    <img className="sp-img" src={im.url} alt={sig.asset_ticker}
                         onError={(e) => { e.target.style.display = "none"; }} />
                  </a>
                  {(im.timeframe || im.role || im.note) && (
                    <figcaption className="mono sp-figcap">
                      {[im.timeframe, im.role, im.note].filter(Boolean).join(" · ")}
                    </figcaption>
                  )}
                </figure>
              )) : (
                <div className="sp-noimg">
                  No chart attached — this call was extracted from text.
                </div>
              )}

              {(doc && doc.text) ? (
                <div className="sp-textblock">
                  <div className="sp-sec-title mono">Source text{doc.text_truncated ? " (truncated)" : ""}</div>
                  <pre className="sp-pre">{doc.text}</pre>
                </div>
              ) : (sig.raw_excerpt && (
                <div className="sp-textblock">
                  <div className="sp-sec-title mono">Excerpt the extractor read</div>
                  <pre className="sp-pre">{sig.raw_excerpt}</pre>
                </div>
              ))}
            </div>

            {/* ── Chain + structured read ── */}
            <div className="sp-side">
              <div className="sp-sec">
                <div className="sp-sec-title mono">Provenance</div>
                <Row k="channel" v={sig.source_channel || sig.source_slug} />
                <Row k="author" v={author ? (
                  <>
                    {author.display_name}
                    {author.parent_channel && author.parent_channel !== author.display_name
                      ? <span className="dim"> · in {author.parent_channel}</span> : null}
                    {author.trust_weight != null
                      ? <span className="dim mono"> · trust {author.trust_weight}</span> : null}
                  </>
                ) : (doc && doc.author) || null} />
                <Row k="document" v={doc ? doc.title : null} />
                <Row k="called" v={doc && doc.published_at
                  ? <>{sigStamp(doc.published_at)} <span className="dim">· {sigAge(doc.published_at)} ago</span></>
                  : null} />
                {/* Only worth a line when the pipeline saw it appreciably
                    later than the author posted it. */}
                <Row k="ingested" v={(() => {
                  if (!doc || !doc.ingested_at) return null;
                  const lag = doc.published_at
                    ? (new Date(doc.ingested_at) - new Date(doc.published_at)) / 60000
                    : Infinity;
                  return Math.abs(lag) < 2 ? null
                    : <span className="dim">{sigStamp(doc.ingested_at)} · {sigAge(doc.ingested_at)} ago</span>;
                })()} />
                <Row k="source id" v={doc ? <span className="mono sp-mini">{doc.source_id}</span> : null} />
                <Row k="extractor" v={
                  <span className="mono sp-mini">
                    {sig.extractor_name} {sig.extractor_version}
                    {sig.extractor_confidence != null ? ` · conf ${sig.extractor_confidence}` : ""}
                  </span>
                } />
                <Row k="model" v={(sig.model_name || sig.model_provider)
                  ? <span className="mono sp-mini">{[sig.model_provider, sig.model_name].filter(Boolean).join(" / ")}</span>
                  : null} />
                <Row k="extracted" v={
                  <span className="mono sp-mini">
                    {sigStamp(sig.extracted_at)}
                    <span className="dim"> · {sigAge(sig.extracted_at)} ago</span>
                  </span>
                } />
                <Row k="signal id" v={<span className="mono sp-mini">{sig.signal_id}</span>} />
                {(doc && (doc.telegram_link || doc.url)) && (
                  <div className="sp-links">
                    {doc.telegram_link && (
                      <a className="filter-pill" href={doc.telegram_link}
                         target="_blank" rel="noreferrer">open in Telegram ↗</a>
                    )}
                    {doc.url && (
                      <a className="filter-pill" href={doc.url}
                         target="_blank" rel="noreferrer">original ↗</a>
                    )}
                  </div>
                )}
              </div>

              <div className="sp-sec">
                <div className="sp-sec-title mono">The call</div>
                {sig.thesis_summary && <div className="sp-thesis">{sig.thesis_summary}</div>}
                <Row k="horizon" v={[sig.horizon, sig.horizon_days ? `${sig.horizon_days}d` : null].filter(Boolean).join(" · ") || null} />
                <Row k="entry" v={sig.entry_zone_low != null
                  ? (sig.entry_zone_high != null && sig.entry_zone_high !== sig.entry_zone_low
                      ? `${sig.entry_zone_low} – ${sig.entry_zone_high}` : num(sig.entry_zone_low))
                  : null} />
                <Row k="stop" v={num(sig.stop_loss)} />
                <Row k="targets" v={[sig.target_1, sig.target_2].filter(v => v != null).join(" / ") || null} />
                <Row k="invalidation" v={sig.invalidation} />
                <Row k="catalyst" v={[sig.catalyst_type, sig.catalyst_summary, sig.catalyst_date].filter(Boolean).join(" · ") || null} />
                <Row k="conviction from" v={sig.conviction_raw} />
                <Row k="tags" v={Array.isArray(sig.thesis_tags) && sig.thesis_tags.length
                  ? sig.thesis_tags.join(", ") : null} />
              </div>

              {(feat || Object.keys(detail).length > 0) && (
                <div className="sp-sec">
                  <div className="sp-sec-title mono">Chart read</div>
                  <Row k="timeframe" v={detail.chart_timeframe || (feat && feat.timeframe)} />
                  <Row k="call type" v={detail.call_type || (feat && feat.call_type)} />
                  <Row k="bias" v={detail.bias || (feat && feat.bias)} />
                  <Row k="stage" v={detail.trade_stage || (feat && feat.trade_stage)} />
                  <Row k="pattern" v={detail.pattern || (feat && feat.pattern)} />
                  <Row k="confluence" v={(detail.confluence_score ?? (feat && feat.confluence_score)) != null
                    ? `${detail.confluence_score ?? feat.confluence_score}/5` : null} />
                  <Row k="indicators" v={(() => {
                    const ind = detail.indicators || (feat && feat.indicators_visible);
                    return Array.isArray(ind) && ind.length ? ind.join(", ") : null;
                  })()} />
                  {feat && feat.notes && <div className="sp-notes">{feat.notes}</div>}
                  {setups.map((st, i) => (
                    <div key={i} className="sp-setup mono">
                      setup {i + 1} · {[st.direction, st.status].filter(Boolean).join(" · ")}
                      {st.entry != null ? ` · entry ${st.entry}` : ""}
                      {st.stop_loss != null ? ` · stop ${st.stop_loss}` : ""}
                      {Array.isArray(st.take_profits) && st.take_profits.length
                        ? ` · tp ${st.take_profits.join(" / ")}` : ""}
                      {st.final_target != null ? ` · final ${st.final_target}` : ""}
                    </div>
                  ))}
                </div>
              )}

              {data.siblings && data.siblings.length > 0 && (
                <div className="sp-sec">
                  <div className="sp-sec-title mono">Also from this drop</div>
                  {data.siblings.map(sb => (
                    <button key={sb.signal_id} className="sp-sibling"
                            onClick={() => onOpenSignal(sb.signal_id)}>
                      <span className="mono">{sb.asset_ticker} · {sb.side}
                        {sb.conviction != null ? ` · conv ${sb.conviction.toFixed(1)}` : ""}</span>
                      {sb.thesis_summary && <span className="sp-sib-thesis">{sb.thesis_summary.slice(0, 90)}</span>}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

Object.assign(window, {
  Positioning, RegimeTape, DataFreshnessStrip, LiveSignalsPanel,
  SignalProvenanceModal,
});
