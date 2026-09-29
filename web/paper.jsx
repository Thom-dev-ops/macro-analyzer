// /paper — the paper-trading book.
//
// A $50k simulated account that executes the desk's own reads on a fixed
// mandate: 1–5% per position sized by RANK, a hard 30% cash floor, and
// rotation out of weak holdings to fund stronger ones.
//
// Rank is a 0–100 percentile: 72 means the name stands above roughly 72%
// of everything the desk currently scores. It is an ordering, not a
// probability — nothing here is calibrated against outcomes yet, and the
// UI should never imply otherwise.
//
// The decision log is the centrepiece, not a footnote. Every fill, every
// hold and every REFUSAL is a row that explains itself — including the
// conviction components that produced the size and the constraint state
// at the moment of the call. A book you cannot interrogate is a book you
// cannot trust.

const PAPER_ACTION_TONE = {
  OPEN: "open", ADD: "open", TRIM: "trim", EXIT: "exit",
  HOLD: "hold", REJECT: "reject",
};

function usd(n, digits = 0) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  return "$" + Number(n).toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

function px(n, digits = 2) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  const v = Number(n);
  return v >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 })
                   : v.toFixed(digits);
}

// One shared fetch helper: the paper API is the only source for this page
// (no MA_DATA snapshot), so a dead endpoint should show as an empty state,
// never as a blank screen.
function usePaper(path, deps = []) {
  const [data, setData] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const reload = React.useCallback(() => {
    let live = true;
    setLoading(true);
    fetch(`/api/paper/${path}`)
      .then(r => (r.ok ? r.json() : r.json().then(j => Promise.reject(j.detail || r.status))))
      .then(j => { if (live) { setData(j); setErr(null); } })
      .catch(e => { if (live) setErr(String(e)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [path]);
  React.useEffect(reload, [reload, ...deps]);
  return { data, err, loading, reload };
}

function Paper() {
  const book = usePaper("portfolio");
  const positions = usePaper("positions?status=open");
  const [filter, setFilter] = React.useState("all");
  const decisions = usePaper(
    `decisions?limit=250${filter === "all" ? "" : `&action=${filter}`}`, [filter]
  );
  const curve = usePaper("equity-curve?limit=200");
  const perf = usePaper("performance");
  const [tick, setTick] = React.useState(null);
  const [ticking, setTicking] = React.useState(false);

  const runDryTick = () => {
    setTicking(true);
    fetch("/api/paper/tick?dry_run=true", { method: "POST" })
      .then(r => r.json())
      .then(j => setTick(j))
      .catch(e => setTick({ report: `tick failed: ${e}` }))
      .finally(() => setTicking(false));
  };

  if (book.err) {
    return (
      <div className="block">
        <div className="empty-state muted">
          <p>{book.err}</p>
          <p className="mono" style={{ marginTop: 10 }}>
            .venv/bin/python scripts/paper_trading_tick.py --dry-run
          </p>
        </div>
      </div>
    );
  }
  if (!book.data) return <div className="block"><div className="empty-state muted">Loading the book…</div></div>;

  const b = book.data;
  const pts = (curve.data && curve.data.points) || [];

  return (
    <div className="paper-view">
      <BookHeader book={b} />
      <PerformanceBlock perf={perf.data} loading={perf.loading} />
      <div className="two-col-6040">
        <PositionsBlock
          positions={(positions.data && positions.data.positions) || []}
          loading={positions.loading}
          onChanged={() => {
            positions.reload(); book.reload(); decisions.reload(); perf.reload();
          }}
        />
        <div>
          <EquityBlock points={pts} startingEquity={b.startingEquity} />
          <MandateBlock mandate={b.mandate} />
        </div>
      </div>
      <DecisionLog
        decisions={(decisions.data && decisions.data.decisions) || []}
        loading={decisions.loading}
        filter={filter}
        onFilter={setFilter}
        onRunDryTick={runDryTick}
        ticking={ticking}
        tick={tick}
        onDismissTick={() => setTick(null)}
      />
      <ClosedTrades trades={(perf.data && perf.data.trades) || []} />
      <CandidateQueue />
    </div>
  );
}

// ── Book header ───────────────────────────────────────────────────────

function BookHeader({ book, cadence = "06:15 · 13:15 daily" }) {
  // The cash meter fills toward the 70% ceiling. Amber inside the last
  // five points of headroom, red if the floor has been breached — which
  // should only ever be transient, between a mark and the next tick.
  const deployed = book.deployedPct;
  const ceiling = 100 - book.minCashPct;
  const pct = Math.min(100, (deployed / ceiling) * 100);
  const tone = deployed > ceiling ? "neg" : deployed > ceiling - 5 ? "warn" : "";

  return (
    <div className="kpi-strip paper-kpis">
      <div className="kpi">
        <div className="kpi-lbl">Equity</div>
        <div className="kpi-val">{usd(book.equity)}</div>
        <div className={`kpi-sub ${book.totalPnl >= 0 ? "pos" : "neg"}`}>
          {book.totalPnl >= 0 ? "+" : ""}{usd(book.totalPnl)} ({book.totalPnlPct >= 0 ? "+" : ""}
          {book.totalPnlPct}%) since {usd(book.startingEquity)}
        </div>
      </div>
      <div className="kpi">
        <div className="kpi-lbl">Cash</div>
        <div className="kpi-val">{book.cashPct}%</div>
        <div className="kpi-sub">{usd(book.cash)} · floor {book.minCashPct}%</div>
      </div>
      <div className="kpi">
        <div className="kpi-lbl">Deployed</div>
        <div className={`kpi-val ${tone}`}>{deployed}%</div>
        <div className="kpi-sub">{usd(book.headroomUsd)} headroom to the ceiling</div>
        <div className="kpi-meter"><i className={tone} style={{ width: `${pct}%` }}></i></div>
      </div>
      <div className="kpi">
        <div className="kpi-lbl">Open P&amp;L</div>
        <div className={`kpi-val ${book.unrealizedPnl >= 0 ? "pos" : "neg"}`}>
          {book.unrealizedPnl >= 0 ? "+" : ""}{usd(book.unrealizedPnl)}
        </div>
        <div className="kpi-sub">{book.openPositions} positions</div>
      </div>
      <div className="kpi">
        <div className="kpi-lbl">Realized</div>
        <div className={`kpi-val ${book.realizedPnl >= 0 ? "pos" : "neg"}`}>
          {book.realizedPnl >= 0 ? "+" : ""}{usd(book.realizedPnl)}
        </div>
        <div className="kpi-sub">banked on closed lots</div>
      </div>
      <div className="kpi">
        <div className="kpi-lbl">Last tick</div>
        <div className="kpi-val mono" style={{ fontSize: "1.05rem" }}>
          {book.lastTickAt ? String(book.lastTickAt).slice(5, 16).replace("T", " ") : "never"}
        </div>
        <div className="kpi-sub">{cadence}</div>
      </div>
    </div>
  );
}

// ── Positions ─────────────────────────────────────────────────────────

function PositionsBlock({ positions, loading, onChanged }) {
  const close = (p) => {
    if (!window.confirm(`Close ${p.ticker} at market? This is logged as a manual override.`)) return;
    fetch(`/api/paper/positions/${p.positionId}/close`, { method: "POST" })
      .then(() => onChanged())
      .catch(() => onChanged());
  };

  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">B1</span>
          <span>Open book</span>
          <span className="block-sub">
            {positions.length} positions · rank at entry → rank now
          </span>
        </div>
      </header>
      {loading && !positions.length ? (
        <div className="empty-state muted">Marking the book…</div>
      ) : !positions.length ? (
        <div className="empty-state muted">
          Flat. Nothing has cleared the entry bar, or the book has not ticked yet.
        </div>
      ) : (
        <div className="paper-pos-list">
          {positions.map(p => {
            const drift = p.rankDrift;
            return (
              <div key={p.positionId} className="paper-pos">
                <div className="pp-ident">
                  <span className="mono pp-ticker">{p.ticker}</span>
                  <SideLabel side={p.side} />
                  <span className="pp-weight mono">{p.weightPct}%</span>
                </div>
                <div className="pp-levels">
                  <span><i>entry</i> <b className="mono">{px(p.avgPrice)}</b></span>
                  <span><i>mark</i> <b className="mono">{px(p.mark)}</b></span>
                  <span><i>stop</i> <b className="mono">{px(p.stop)}</b></span>
                  <span><i>target</i> <b className="mono">{px(p.target)}</b></span>
                </div>
                <div className="pp-conv">
                  <i>rank</i>
                  <span className="mono muted">{Math.round(p.rankAtEntry ?? 0)}→</span>
                  <span className="mono">{Math.round(p.rankNow ?? 0)}</span>
                  {!!drift && (
                    <span className={`mono ${drift > 0 ? "pos" : "neg"}`}>
                      {" "}{drift > 0 ? "+" : ""}{Math.round(drift)}
                    </span>
                  )}
                  {p.partialTaken && <span className="pp-flag">half off</span>}
                </div>
                <div className="pp-pnl">
                  <PnL usd={p.unrealized} pct={p.unrealizedPct} size="sm" />
                  {p.rMultiple !== null && p.rMultiple !== undefined && (
                    <span className="mono muted pp-r">{p.rMultiple.toFixed(2)}R</span>
                  )}
                </div>
                <button className="btn-mini" onClick={() => close(p)} title="Close at market">
                  close
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Equity curve ──────────────────────────────────────────────────────

function EquityBlock({ points, startingEquity }) {
  const series = points.map(p => p.equity);
  const last = series.length ? series[series.length - 1] : startingEquity;
  const delta = last - startingEquity;
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">B2</span>
          <span>Equity curve</span>
          <span className="block-sub">
            {points.length} {points.length === 1 ? "tick" : "ticks"}
          </span>
        </div>
      </header>
      {series.length < 2 ? (
        <div className="empty-state muted">
          One tick is a dot, not a curve. Check back after the next pass.
        </div>
      ) : (
        <div className="paper-curve">
          <Sparkline
            data={series}
            width={320}
            height={64}
            color={delta >= 0 ? "var(--pos)" : "var(--neg)"}
            area
            marker
          />
          <div className="paper-curve-foot muted mono">
            {usd(Math.min(...series))} — {usd(Math.max(...series))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Mandate ───────────────────────────────────────────────────────────

function MandateBlock({ mandate }) {
  const m = mandate || {};
  // Rank is a 0–100 percentile of the live score distribution. The hints
  // spell that out: a bare number on a card is exactly how the previous
  // 0–1 "conviction" scale got read as a confidence percentage.
  const pctOf = (r) => `top ${(100 - r).toFixed(0)}%`;
  const rows = [
    ["Position size",
     `${(m.minPositionPct * 100).toFixed(0)}–${(m.maxPositionPct * 100).toFixed(0)}% of equity, by rank`,
     `a name that just clears the bar gets ${(m.minPositionPct * 100).toFixed(0)}%; rank 100 gets ` +
     `${(m.maxPositionPct * 100).toFixed(0)}%, interpolated in between`],
    ["Cash floor",
     `${(m.minCashPct * 100).toFixed(0)}% — max ${(m.maxDeployedPct * 100).toFixed(0)}% deployed`,
     "a floor, not a target — the book can sit lighter"],
    ["Entry / exit bar",
     `open above rank ${m.entryFloor}, close below ${m.exitFloor}`,
     `rank is a 0–100 percentile of everything the desk scores, adjusted for signals, R:R ` +
     `and momentum — NOT a probability. Open only the ${pctOf(m.entryFloor)}; close anything ` +
     `that falls out of the ${pctOf(m.exitFloor)}. Two bars is hysteresis — one would churn ` +
     `names sitting on it.`],
    ["Rotation edge", `${m.rotationEdge} percentile points before displacing a holding`,
     "a closer call than that is not worth the spread"],
    ["Concentration",
     `${m.maxPositions} positions · ${(m.maxBucketPct * 100).toFixed(0)}% per correlated bucket`, null],
    ["Exits",
     `half off at target · ${(m.trailGivebackPct * 100).toFixed(0)}% giveback trail · stale at ${m.staleAfterDays}d`, null],
    ["Re-entry", `${m.reentryCooldownDays}d cooldown after a risk exit`,
     "rotation and cash-floor trims carry none — those were capital, not thesis"],
    ["Fills", `${m.slippageBps}bps slippage, ${m.feeBps}bps fees`, null],
  ];
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">B3</span>
          <span>Mandate</span>
          <span className="block-sub">snapshotted when the book opened</span>
        </div>
      </header>
      <dl className="paper-mandate">
        {rows.map(([k, v, hint]) => (
          <React.Fragment key={k}>
            <dt>{k}</dt>
            <dd>
              {v}
              {hint && <span className="pm-hint">{hint}</span>}
            </dd>
          </React.Fragment>
        ))}
      </dl>
    </div>
  );
}


// ── Performance + attribution ─────────────────────────────────────────

const PERF_AXES = [
  { id: "bySleeve", label: "Sleeve" },
  { id: "byClass", label: "Asset class" },
  { id: "byRegime", label: "Regime" },
  { id: "bySide", label: "Side" },
  { id: "byExitReason", label: "Exit reason" },
];

function pctCell(v, digits = 2) {
  if (v === null || v === undefined) return <span className="muted">—</span>;
  const n = Number(v);
  return (
    <span className={`mono ${n > 0 ? "pos" : n < 0 ? "neg" : "muted"}`}>
      {n > 0 ? "+" : ""}{n.toFixed(digits)}%
    </span>
  );
}

function moneyCell(v) {
  if (v === null || v === undefined) return <span className="muted">—</span>;
  const n = Number(v);
  return (
    <span className={`mono ${n > 0 ? "pos" : n < 0 ? "neg" : "muted"}`}>
      {n > 0 ? "+" : n < 0 ? "\u2212" : ""}{usd(Math.abs(n), Math.abs(n) < 100 ? 2 : 0)}
    </span>
  );
}

function PerformanceBlock({ perf, loading }) {
  const [axis, setAxis] = React.useState("bySleeve");
  if (loading && !perf) {
    return <div className="block"><div className="empty-state muted">Computing attribution…</div></div>;
  }
  if (!perf) return null;

  const h = perf.headline;
  const o = perf.overall;
  const rows = perf[axis] || [];

  // Only a partition of the book sums to it; regimes overlap by design.
  const partition = axis === "byClass" || axis === "bySleeve" || axis === "bySide";

  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">B2</span>
          <span>Performance</span>
          <span className="block-sub">
            {o.trades} closed · {o.openPositions} open · {h.totalFills} fills ·
            {" "}{h.turnoverX}× turnover
          </span>
        </div>
      </header>

      <div className="perf-stats">
        <div className="ps">
          <div className="ps-lbl">Total return</div>
          <div className="ps-val">{pctCell(h.totalReturnPct)}</div>
          <div className="ps-sub">{usd(h.equity)} on {usd(h.startingEquity)}</div>
        </div>
        <div className="ps">
          <div className="ps-lbl">Booked</div>
          <div className="ps-val">{pctCell(h.realizedPct)}</div>
          <div className="ps-sub">{moneyCell(o.realized)} realized</div>
        </div>
        <div className="ps">
          <div className="ps-lbl">Open</div>
          <div className="ps-val">{pctCell(h.unrealizedPct)}</div>
          <div className="ps-sub">{moneyCell(o.unrealized)} unrealized</div>
        </div>
        <div className="ps">
          <div className="ps-lbl">Banked share</div>
          <div className="ps-val mono">
            {h.bookedShare === null || h.bookedShare === undefined
              ? <span className="muted">n/a</span>
              : `${h.bookedShare.toFixed(0)}%`}
          </div>
          <div className="ps-sub">
            {h.bookedShareNote || "of total P&L is off the table"}
          </div>
        </div>
        <div className="ps">
          <div className="ps-lbl">Win rate</div>
          <div className="ps-val mono">
            {o.winRate === null ? <span className="muted">—</span> : `${o.winRate.toFixed(0)}%`}
          </div>
          <div className="ps-sub">{o.wins}W · {o.losses}L{o.scratches ? ` · ${o.scratches}S` : ""}</div>
        </div>
        <div className="ps">
          <div className="ps-lbl">Profit factor</div>
          <div className="ps-val mono">
            {o.profitFactor === null ? <span className="muted">—</span> : o.profitFactor.toFixed(2)}
          </div>
          <div className="ps-sub">
            {usd(o.grossProfit, 0)} won / {usd(o.grossLoss, 0)} lost
          </div>
        </div>
        <div className="ps">
          <div className="ps-lbl">Expectancy</div>
          <div className="ps-val">{pctCell(o.expectancyPct, 2)}</div>
          <div className="ps-sub">
            per closed trade{o.avgR !== null && o.avgR !== undefined ? ` · ${o.avgR.toFixed(2)}R avg` : ""}
          </div>
        </div>
        <div className="ps">
          <div className="ps-lbl">Avg win / loss</div>
          <div className="ps-val ps-pair">
            {pctCell(o.avgWinPct)} <span className="muted">/</span> {pctCell(o.avgLossPct)}
          </div>
          <div className="ps-sub">
            {o.avgHoldDays !== null && o.avgHoldDays !== undefined
              ? `${o.avgHoldDays}d average hold` : "no closed trades yet"}
          </div>
        </div>
      </div>

      <div className="perf-axis">
        {PERF_AXES.map(a => (
          <button
            key={a.id}
            className={`btn-mini ${axis === a.id ? "on" : ""}`}
            onClick={() => setAxis(a.id)}
          >
            {a.label}
          </button>
        ))}
        {!partition && <span className="perf-axis-note muted">{perf.regimeNote}</span>}
        {perf.unclassified && perf.unclassified.length > 0 && (
          <span className="perf-axis-note neg">
            unmapped: {perf.unclassified.join(", ")}
          </span>
        )}
      </div>

      {!rows.length ? (
        <div className="empty-state muted">Nothing to attribute yet.</div>
      ) : (
        <div className="perf-table">
          <div className="pt-head">
            <span>{PERF_AXES.find(a => a.id === axis).label}</span>
            <span className="pt-num">Exposure</span>
            <span className="pt-num">Booked</span>
            <span className="pt-num">Open</span>
            <span className="pt-num">Total P&amp;L</span>
            <span className="pt-num">Trades</span>
            <span className="pt-num">Win rate</span>
            <span className="pt-num">Avg R</span>
          </div>
          {rows.map(r => (
            <div key={r.id} className="pt-row">
              <span className="pt-name">
                {r.label}
                <i className="pt-tickers">{r.tickers.slice(0, 6).join(" ")}
                  {r.tickers.length > 6 ? ` +${r.tickers.length - 6}` : ""}</i>
              </span>
              <span className="pt-num mono">
                {r.exposurePct ? `${r.exposurePct.toFixed(1)}%` : <span className="muted">—</span>}
              </span>
              <span className="pt-num">{moneyCell(r.realized)}</span>
              <span className="pt-num">{moneyCell(r.unrealized)}</span>
              <span className="pt-num">{moneyCell(r.totalPnl)}</span>
              <span className="pt-num mono">
                {r.trades}{r.openPositions ? <i className="pt-open"> +{r.openPositions}</i> : null}
              </span>
              <span className="pt-num mono">
                {r.winRate === null ? <span className="muted">—</span> : `${r.winRate.toFixed(0)}%`}
              </span>
              <span className="pt-num mono">
                {r.avgR === null || r.avgR === undefined
                  ? <span className="muted">—</span>
                  : <span className={r.avgR > 0 ? "pos" : r.avgR < 0 ? "neg" : ""}>{r.avgR.toFixed(2)}</span>}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


// ── Closed-trade blotter ──────────────────────────────────────────────

function ClosedTrades({ trades }) {
  const [show, setShow] = React.useState(true);
  const closed = trades.filter(t => t.status === "closed");
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">B6</span>
          <span>Closed trades</span>
          <span className="block-sub">
            {closed.length} round trips · return is on capital committed, not book equity
          </span>
        </div>
        <div className="block-actions">
          <button className="btn-mini" onClick={() => setShow(!show)}>
            {show ? "hide" : "show"}
          </button>
        </div>
      </header>
      {!show ? (
        <div className="block-quiet muted">Hidden.</div>
      ) : !closed.length ? (
        <div className="empty-state muted">
          No round trips yet. Every position is still open.
        </div>
      ) : (
        <div className="perf-table">
          <div className="pt-head">
            <span>Ticker</span>
            <span>Sleeve</span>
            <span className="pt-num">Committed</span>
            <span className="pt-num">P&amp;L</span>
            <span className="pt-num">Return</span>
            <span className="pt-num">R</span>
            <span className="pt-num">Held</span>
            <span>Closed because</span>
          </div>
          {closed.map(t => (
            <div key={t.positionId} className="pt-row">
              <span className="pt-name mono">
                {t.ticker}<i className="pt-tickers">{t.side.toLowerCase()}</i>
              </span>
              <span className="muted pt-sleeve">{t.sleeve}</span>
              <span className="pt-num mono">{usd(t.costIn)}</span>
              <span className="pt-num">{moneyCell(t.realized)}</span>
              <span className="pt-num">{pctCell(t.realizedPct)}</span>
              <span className="pt-num mono">
                {t.rMultiple === null ? <span className="muted">—</span> : t.rMultiple.toFixed(2)}
              </span>
              <span className="pt-num mono">{t.holdDays === null ? "—" : `${t.holdDays}d`}</span>
              <span className="muted pt-why">{String(t.exitIntent || "").replace(/_/g, " ")}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── The decision log ──────────────────────────────────────────────────

function DecisionLog({
  decisions, loading, filter, onFilter, onRunDryTick, ticking, tick, onDismissTick,
}) {
  const [open, setOpen] = React.useState(null);
  const filters = ["all", "OPEN", "ADD", "TRIM", "EXIT", "REJECT", "HOLD"];

  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">B4</span>
          <span>Decision log</span>
          <span className="block-sub">
            what the book did, and what it refused to do — click any row for the reasoning
          </span>
        </div>
        <div className="block-actions">
          <div className="paper-filters">
            {filters.map(f => (
              <button
                key={f}
                className={`btn-mini ${filter === f ? "on" : ""}`}
                onClick={() => onFilter(f)}
              >
                {f === "all" ? "all" : f.toLowerCase()}
              </button>
            ))}
          </div>
          <button className="btn-secondary" onClick={onRunDryTick} disabled={ticking}>
            {ticking ? "running…" : "dry-run a tick"}
          </button>
        </div>
      </header>

      {tick && (
        <div className="paper-tick-preview">
          <div className="ptp-head">
            <span className="mono">dry run — nothing written</span>
            <button className="btn-mini" onClick={onDismissTick}>dismiss</button>
          </div>
          <pre className="mono">{tick.report}</pre>
        </div>
      )}

      {loading && !decisions.length ? (
        <div className="empty-state muted">Reading the log…</div>
      ) : !decisions.length ? (
        <div className="empty-state muted">
          No decisions yet{filter !== "all" ? ` matching ${filter.toLowerCase()}` : ""}.
        </div>
      ) : (
        <div className="paper-log">
          {decisions.map(d => {
            const tone = PAPER_ACTION_TONE[d.action] || "hold";
            const isOpen = open === d.decision_id;
            return (
              <div key={d.decision_id} className={`paper-dec tone-${tone} ${isOpen ? "on" : ""}`}>
                <button
                  className="pd-row"
                  onClick={() => setOpen(isOpen ? null : d.decision_id)}
                >
                  <span className={`pd-action mono tone-${tone}`}>{d.action}</span>
                  <span className="pd-ticker mono">{d.ticker}</span>
                  <span className="pd-headline">{d.headline}</span>
                  {d.blocker && <span className="pd-blocker mono">{d.blocker}</span>}
                  <span className="pd-when muted mono">
                    {String(d.decided_at).slice(5, 16).replace("T", " ")}
                  </span>
                </button>
                {isOpen && <DecisionDetail d={d} />}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function DecisionDetail({ d }) {
  const r = d.rationale || {};
  const rk = r.rank || {};
  const comps = rk.components || [];
  const cons = r.constraints || {};
  const lv = r.levels || {};
  const comp = r.composition || null;
  const book = (((r.rank || {}).components) || []).find(c => c.name === "book_record") || null;
  const horizon = r.horizon || null;
  const path = r.exitPath || null;

  // The grid has to count the columns that will actually render, or the
  // last one wraps under the first and reads as a second row of the same
  // panel. Two are unconditional; the rest earn their place.
  const columns = 2 + (comp ? 1 : 0)
    + ((book || horizon || path) ? 1 : 0)
    + (d.position_id ? 1 : 0);

  return (
    <div className={`pd-detail ${columns >= 4 ? "wider" : columns === 3 ? "wide" : ""}`}>
      <div className="pdd-col">
        <div className="pdd-h">Why this rank</div>
        {comps.length ? (
          <ul className="pdd-comps">
            {comps.map((c, i) => (
              <li key={i}>
                <span className={`mono pdd-delta ${c.delta > 0 ? "pos" : c.delta < 0 ? "neg" : "muted"}`}>
                  {c.delta > 0 ? "+" : ""}{Number(c.delta).toFixed(0)}
                </span>
                <span>{c.reason}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">
            {rk.headline || d.intentMeaning || "No rank breakdown on this row — it was an exit, not an entry."}
          </p>
        )}
        {rk.rank !== undefined && (
          <div className="pdd-rank mono">
            rank {Number(rk.rank).toFixed(0)}/100 · {rk.band} · target{" "}
            {Number(rk.targetWeightPct).toFixed(1)}%
            {rk.anchored === false && " · fallback scale, no ranking history"}
          </div>
        )}
      </div>

      {comp && (comp.evidence || []).length > 0 && (
        <div className="pdd-col">
          <div className="pdd-h">
            What the desk's four views said · support {Math.round(comp.support)}/100
          </div>
          <ul className="pdd-comps">
            {comp.evidence.map((e, i) => (
              <li key={i} className={`pdd-ev stance-${e.stance}`}>
                <span className={`mono pdd-delta ${e.delta > 0 ? "pos" : e.delta < 0 ? "neg" : "muted"}`}>
                  {e.delta > 0 ? "+" : ""}{Number(e.delta).toFixed(0)}
                </span>
                <span>
                  <b className="pdd-view">{String(e.view).replace(/_/g, " ")}</b>
                  {" — "}{e.detail}
                </span>
              </li>
            ))}
          </ul>
          {comp.targetMoved && (
            <div className="pdd-rank mono">
              target {px(comp.agentTarget)} → {px(comp.target)} ({comp.targetSource})
              {comp.rr ? ` · ${Number(comp.rr).toFixed(2)}R` : ""}
            </div>
          )}
        </div>
      )}

      {(book || horizon || path) && (
        <div className="pdd-col">
          <div className="pdd-h">What the book's own record says</div>
          {book ? (
            <p className="pdd-book">
              <span className={`mono pdd-delta ${book.delta > 0 ? "pos" : book.delta < 0 ? "neg" : "muted"}`}>
                {book.delta > 0 ? "+" : ""}{Number(book.delta).toFixed(1)}
              </span>{" "}{book.reason}
            </p>
          ) : (
            <p className="muted">No record on this sleeve yet — nothing to learn from.</p>
          )}
          {horizon && (
            <dl className="pdd-kv">
              <dt>horizon</dt>
              <dd className="mono">{horizon.expectedDays}d expected · opinions wait {horizon.minHoldDays}d · confirm {horizon.confirmTicks} ticks</dd>
              <dt>basis</dt><dd>{(horizon.basis || []).join(" · ")}</dd>
            </dl>
          )}
          {path && (
            <div className="pdd-rank mono">
              exit path: <b>{path.path}</b> (rank {path.rank} vs {path.minRank} · support {path.support ?? "—"} vs {path.minSupport})
            </div>
          )}
          {r.riskMultiplier && (
            <div className="pdd-rank mono">sized ÷{r.riskMultiplier} for measured stop slippage</div>
          )}
        </div>
      )}

      <div className="pdd-col">
        <div className="pdd-h">The book at that moment</div>
        <dl className="pdd-kv">
          {cons.equity !== undefined && (<><dt>equity</dt><dd className="mono">{usd(cons.equity)}</dd></>)}
          {cons.cash !== undefined && (<><dt>cash</dt><dd className="mono">{usd(cons.cash)}</dd></>)}
          {cons.deployedPct !== undefined && (
            <><dt>deployed</dt>
              <dd className="mono">{cons.deployedPct}% of {cons.maxDeployedPct}% ceiling</dd></>
          )}
          {cons.headroomUsd !== undefined && (<><dt>headroom</dt><dd className="mono">{usd(cons.headroomUsd)}</dd></>)}
          {cons.openPositions !== undefined && (
            <><dt>positions</dt><dd className="mono">{cons.openPositions} / {cons.maxPositions}</dd></>
          )}
          {d.notional !== null && d.notional !== undefined && (
            <><dt>notional</dt><dd className="mono">{usd(d.notional)}</dd></>
          )}
          {lv.stop && (
            <><dt>levels</dt>
              <dd className="mono">
                {px(lv.entry)} / {px(lv.stop)} / {px(lv.target)}
                {lv.rr ? ` · ${Number(lv.rr).toFixed(1)}R` : ""}
              </dd></>
          )}
          {r.fundedBy && r.fundedBy.length > 0 && (
            <><dt>funded by</dt><dd className="mono">{r.fundedBy.join(", ")}</dd></>
          )}
          {r.rotatingInto && (
            <><dt>rotating into</dt>
              <dd className="mono">{r.rotatingInto} (rank {Math.round(r.rivalRank ?? 0)})</dd></>
          )}
          {r.cooldown && (
            <><dt>cooldown</dt>
              <dd className="mono">{r.cooldown.intent} on {String(r.cooldown.filled_at).slice(0, 10)}</dd></>
          )}
        </dl>
        <div className="pdd-intent muted">{d.intent} — {d.intentMeaning}</div>
      </div>

      {d.position_id && <PositionArc d={d} />}
    </div>
  );
}

// The whole life of one position, shown on every row that belongs to it.
//
// The log filters to a single action at a time and caps at 250 rows, so a
// trade's arc is structurally invisible in it: the half taken off at
// target sits under the `trim` chip and the stop-out under `exit`, and on
// a book with 1,600 decisions they can be 40 rows apart. An EXIT that
// closed a half-sized position read as a full loss with no hint that the
// other half had already left.
function PositionArc({ d }) {
  const { data, loading } = usePaper(
    `decisions?position_id=${encodeURIComponent(d.position_id)}` +
    `&executed_only=true&limit=100`,
    [d.position_id],
  );
  const legs = ((data || {}).decisions || [])
    .slice()
    .sort((a, b) => String(a.decided_at).localeCompare(String(b.decided_at)));
  if (loading && !legs.length) return null;
  if (legs.length < 2) return null;   // a lone OPEN is not yet an arc

  const realized = legs.reduce(
    (t, l) => t + Number((l.fill || {}).realizedPnl || 0), 0,
  );
  return (
    <div className="pdd-col">
      <div className="pdd-h">This position, start to finish</div>
      <ol className="pdd-arc">
        {legs.map(l => {
          const f = l.fill || {};
          const tone = PAPER_ACTION_TONE[l.action] || "hold";
          return (
            <li key={l.decision_id} className={l.decision_id === d.decision_id ? "on" : ""}>
              <span className={`pd-action mono tone-${tone}`}>{l.action}</span>
              <span className="mono pdd-arc-when">
                {String(l.decided_at).slice(5, 16).replace("T", " ")}
              </span>
              <span className="mono pdd-arc-fill">
                {f.qty ? `${Number(f.qty).toFixed(2)} @ ${px(f.price)}` : "—"}
              </span>
              <span className="mono pdd-arc-notional">{usd(f.notional)}</span>
            </li>
          );
        })}
      </ol>
      <div className="pdd-rank mono">
        realized{" "}
        <span className={realized > 0 ? "pos" : realized < 0 ? "neg" : "muted"}>
          {realized >= 0 ? "+" : "−"}{usd(Math.abs(realized), 2).slice(1)}
        </span>
        {" "}over {legs.length} legs
      </div>
    </div>
  );
}

// ── Candidate queue ───────────────────────────────────────────────────

function CandidateQueue() {
  const [show, setShow] = React.useState(false);
  const { data, loading } = usePaper(show ? "candidates?limit=40" : "candidates?limit=1", [show]);
  const cands = show ? ((data && data.candidates) || []) : [];

  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">B5</span>
          <span>Candidate queue</span>
          <span className="block-sub">
            every scored name by rank — 0–100 percentile, not a probability
            {data && data.totalScored ? ` · ${data.totalScored} scored` : ""}
          </span>
        </div>
        <div className="block-actions">
          <button className="btn-mini" onClick={() => setShow(!show)}>
            {show ? "hide" : "show"}
          </button>
        </div>
      </header>
      {!show ? (
        <div className="block-quiet muted">
          Hidden by default — this is the whole scored universe, not a shortlist.
        </div>
      ) : loading ? (
        <div className="empty-state muted">Ranking…</div>
      ) : (
        <div className="paper-cands">
          {cands.map(c => (
            <div key={c.ticker} className={`paper-cand ${c.clearsEntryFloor ? "" : "below"}`}>
              <span className="mono pc-ticker">{c.ticker}</span>
              <SideLabel side={c.side} />
              <span className="mono pc-rank">{Number(c.rank).toFixed(0)}</span>
              <span className="pc-band">{c.band}</span>
              <span className="mono pc-w">
                {c.clearsEntryFloor ? `${c.targetWeightPct}%` : "—"}
              </span>
              {c.held && <span className="pc-held">held</span>}
              <span className="pc-why muted">{c.headline}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
