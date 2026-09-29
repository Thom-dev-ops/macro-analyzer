// /cohort — the second paper book: the Feather Hands copy-trader.
//
// /paper trades the desk's blended score. This one trades the cohort's
// OWN calls — Big_Nuts, Feather Hands Trading, MadDog31, joejoe55,
// Market Traders — with the entry, stop and target the chart was posted
// with. Same engine, same exit rules, same fill model; only the
// candidate source and the mandate differ. The difference between the
// two equity curves is the difference between the two ideas, which is
// the only reason this page exists.
//
// The panel that must never be buried is COVERAGE. Roughly 60% of this
// cohort's flow is Solana memecoins nothing in the price stack can mark,
// so "two fills" is meaningless without "out of 41 candidates screened
// from 235 calls, 63% of which cannot be priced at all". The funnel sits
// above the fold, not in a log.
//
// This file deliberately REUSES paper.jsx's blocks (BookHeader,
// PositionsBlock, EquityBlock, DecisionLog, ClosedTrades,
// PerformanceBlock) rather than copying them: one book UI, two data
// sources. It must therefore load AFTER paper.jsx in index.html. The one
// block it does NOT reuse is MandateBlock — that one describes rank as a
// percentile of the desk's scored universe, which is true of the signal
// book and false here. See CohortMandate at the bottom.

// Same contract as paper.jsx's usePaper, with one difference: a null
// path is a no-op rather than a request. The cohort book's id has to be
// resolved from /books before anything else can be fetched, and firing
// portfolio_id=null at the API in the meantime would 404 a page that is
// merely still loading.
function useCohortApi(path, deps = []) {
  const [data, setData] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [loading, setLoading] = React.useState(!!path);
  const reload = React.useCallback(() => {
    if (!path) { setLoading(false); return () => {}; }
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

function Cohort() {
  const books = useCohortApi("books");
  const all = (books.data && books.data.books) || [];
  const book = all.find(b => b.kind === "cohort" && b.status === "active");
  const id = book ? book.portfolioId : null;
  const q = id ? `portfolio_id=${encodeURIComponent(id)}` : "";

  const header = useCohortApi(id ? `portfolio?${q}` : null, [id]);
  const positions = useCohortApi(id ? `positions?status=open&${q}` : null, [id]);
  const [filter, setFilter] = React.useState("all");
  const decisions = useCohortApi(
    id ? `decisions?limit=250&${q}${filter === "all" ? "" : `&action=${filter}`}` : null,
    [id, filter]
  );
  const curve = useCohortApi(id ? `equity-curve?limit=200&${q}` : null, [id]);
  const perf = useCohortApi(id ? `performance?${q}` : null, [id]);
  const roster = useCohortApi("cohort/roster");

  const [tick, setTick] = React.useState(null);
  const [ticking, setTicking] = React.useState(false);
  const runDryTick = () => {
    setTicking(true);
    fetch("/api/paper/cohort/tick?dry_run=true", { method: "POST" })
      .then(r => r.json())
      .then(j => setTick(j))
      .catch(e => setTick({ report: `tick failed: ${e}` }))
      .finally(() => setTicking(false));
  };

  if (books.loading) {
    return <div className="block"><div className="empty-state muted">Loading the book…</div></div>;
  }
  if (!book) {
    return (
      <div className="cohort-view">
        <CohortPreamble roster={roster.data} />
        <div className="block">
          <div className="empty-state muted">
            <p>The cohort book has not been opened yet.</p>
            <p className="mono" style={{ marginTop: 10 }}>
              .venv/bin/python scripts/paper_cohort_tick.py --bootstrap --execute
            </p>
            <p style={{ marginTop: 10 }}>
              Preview what it would do first with <span className="mono">--dry-run</span>,
              or read the funnel below — it works without a book.
            </p>
          </div>
        </div>
        <CoverageFunnel />
        <CohortCandidates />
      </div>
    );
  }

  const b = header.data;
  const pts = (curve.data && curve.data.points) || [];

  return (
    <div className="cohort-view">
      <CohortPreamble roster={roster.data} book={book} />
      {b && <BookHeader book={b} />}
      <CoverageFunnel />
      <PerformanceBlock perf={perf.data} loading={perf.loading} />
      <div className="two-col-6040">
        <PositionsBlock
          positions={(positions.data && positions.data.positions) || []}
          loading={positions.loading}
          onChanged={() => {
            positions.reload(); header.reload(); decisions.reload(); perf.reload();
          }}
        />
        <div>
          <EquityBlock points={pts} startingEquity={book.startingEquity} />
          {b && <CohortMandate mandate={b.mandate} roster={roster.data} />}
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
      <CohortCandidates />
    </div>
  );
}

// ── Who this book follows, and what they are measured at ──────────────

function CohortPreamble({ roster, book }) {
  const authors = (roster && roster.authors) || [];
  return (
    <div className="block cohort-preamble">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">C1</span>
          <span>{(roster && roster.label) || "Cohort"} — followed, not scored</span>
          <span className="block-sub">
            this book takes each call as posted: their ticker, their side, their
            entry, stop and target. The desk's score, structure map and composed
            target are switched off on purpose — that is what /paper measures.
          </span>
        </div>
      </header>
      <div className="cohort-roster">
        {authors.map(a => (
          <div key={a.authorId} className="cohort-author">
            <span className="ca-name">{a.display}</span>
            {a.alphaPct === null || a.alphaPct === undefined ? (
              <span className="ca-edge muted">no measured edge</span>
            ) : (
              <span className={`ca-edge mono ${a.alphaPct >= 0 ? "pos" : "neg"}`}>
                {a.alphaPct >= 0 ? "+" : ""}{Number(a.alphaPct).toFixed(1)}% alpha
              </span>
            )}
            <span className="ca-n muted mono">
              {a.scoredCalls ? `${a.scoredCalls} scored` : "—"}
            </span>
          </div>
        ))}
      </div>
      <div className="cohort-note muted">
        Alpha is market-relative — the call's return minus the market's over the
        same window — and comes from the last accuracy backtest, not from this
        book's own P&L. A record under the sample bar counts as no edge rather
        than as a small one.
        {book && book.lastTickAt ? ` Last tick ${String(book.lastTickAt).slice(0, 16).replace("T", " ")}.` : ""}
      </div>
    </div>
  );
}

// ── The denominator ───────────────────────────────────────────────────

function CoverageFunnel() {
  const { data, loading, err } = useCohortApi("cohort/coverage");
  if (err) return null;
  if (loading || !data) {
    return <div className="block"><div className="empty-state muted">Screening the cohort's calls…</div></div>;
  }
  const skipped = data.skipped || [];
  const worst = skipped.length ? Math.max(...skipped.map(s => s.count)) : 1;
  const priced = data.pricedPct;

  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">C2</span>
          <span>What the book can actually see</span>
          <span className="block-sub">
            {data.considered} calls in the last {data.windowDays} days →{" "}
            {data.candidates} candidates
          </span>
        </div>
      </header>
      {priced !== null && priced !== undefined && (
        <div className="cohort-priced">
          <div className="cp-figure mono">{Number(priced).toFixed(0)}%</div>
          <div className="cp-label">
            of the cohort's directional calls could be priced at all.
            <div className="muted">
              The rest is DEX pairs and microcaps no provider marks. Every P&L
              number on this page is measured on the priceable slice only —
              it is not a verdict on the calls this book never got to take.
            </div>
          </div>
        </div>
      )}
      <div className="cohort-funnel">
        {skipped.map(s => (
          <div key={s.reason} className="cf-row">
            <span className="cf-n mono">{s.count}</span>
            <span className="cf-reason">{s.reason}</span>
            <span className="cf-bar"><i style={{ width: `${(s.count / worst) * 100}%` }} /></span>
            <span className="cf-why muted">{s.meaning}</span>
          </div>
        ))}
      </div>
      {(data.unpriceableTickers || []).length > 0 && (
        <div className="cohort-unpriced muted">
          <span className="cu-label">unpriced flow:</span>
          {data.unpriceableTickers.slice(0, 12).map(t => (
            <span key={t.ticker} className="cu-chip mono">{t.ticker} ×{t.calls}</span>
          ))}
        </div>
      )}
    </div>
  );
}

// ── The calls the book is looking at ──────────────────────────────────

function CohortCandidates() {
  const [show, setShow] = React.useState(true);
  const { data, loading } = useCohortApi(show ? "cohort/candidates?limit=40" : null, [show]);
  const cands = (data && data.candidates) || [];

  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">C3</span>
          <span>Live calls, ranked</span>
          <span className="block-sub">
            the cohort's own levels — rank is built from the call, not from a
            percentile of a universe
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
      ) : loading ? (
        <div className="empty-state muted">Screening…</div>
      ) : !cands.length ? (
        <div className="empty-state muted">
          Nothing in the window clears the screens. The funnel above says why.
        </div>
      ) : (
        <div className="cohort-cands">
          {cands.map(c => <CohortCandidate key={c.ticker + c.side} c={c} />)}
        </div>
      )}
    </div>
  );
}

function CohortCandidate({ c }) {
  const [open, setOpen] = React.useState(false);
  const call = c.call || {};
  const lv = c.levels || {};
  const below = c.rank < 60;

  return (
    <div className={`cohort-cand ${below ? "below" : ""}`}>
      <div className="cc-head" onClick={() => setOpen(!open)}>
        <span className="mono cc-ticker">{c.ticker}</span>
        <SideLabel side={c.side} />
        <span className="mono cc-rank">{Number(c.rank).toFixed(0)}</span>
        <span className="cc-band">{c.band}</span>
        <span className="cc-author">{call.author}</span>
        <span className="cc-tf muted mono">{call.timeframe || "—"}</span>
        <span className="mono cc-rr">
          {lv.rr ? `${Number(lv.rr).toFixed(1)}R` : "—"}
          {call.rrAtMark ? <em className="muted"> · {Number(call.rrAtMark).toFixed(1)}R now</em> : null}
        </span>
        <span className="mono cc-w">{c.tradeable ? `${c.targetWeightPct}%` : "—"}</span>
        <span className="cc-toggle muted">{open ? "−" : "+"}</span>
      </div>
      {open && (
        <div className="cc-body">
          <div className="cc-levels mono">
            entry {px(lv.entry)} · stop {px(lv.stop)} · target {px(lv.target)}
            {call.markAtScreen ? ` · mark ${px(call.markAtScreen)}` : ""}
          </div>
          {call.thesis && <div className="cc-thesis">{call.thesis}</div>}
          <div className="cc-meta muted mono">
            {[call.callType, call.tradeStage && `stage ${call.tradeStage}`,
              call.pattern, call.rawTicker !== c.ticker && `posted as ${call.rawTicker}`,
              call.ageDays !== undefined && `${call.ageDays}d old`]
              .filter(Boolean).join(" · ")}
          </div>
          <div className="cc-components">
            {(c.components || []).map((k, i) => (
              <div key={i} className="cc-comp">
                <span className={`mono cc-delta ${k.delta > 0 ? "pos" : k.delta < 0 ? "neg" : ""}`}>
                  {k.delta > 0 ? "+" : ""}{k.delta}
                </span>
                <span className="cc-reason muted">{k.reason}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── The rulebook ──────────────────────────────────────────────────────

// NOT paper.jsx's MandateBlock. That one explains rank as "a 0–100
// percentile of everything the desk scores", which is true of the signal
// book and false here: there is no distribution to rank a Telegram post
// against, and the number is built from the call itself. A book whose
// whole claim is honesty about what it measures cannot mis-describe its
// own scale on its own page.
function CohortMandate({ mandate, roster }) {
  const m = mandate || {};
  const r = roster || {};
  const pct = (v, d = 0) => `${(v * 100).toFixed(d)}%`;
  const rows = [
    ["Position size",
     `${pct(m.minPositionPct, 1)}–${pct(m.maxPositionPct)} of equity, by rank`,
     "a call that just clears the bar gets the floor; a rank-100 call gets the ceiling"],
    ["Rank", `built from the call, not from a percentile`,
     "conviction, chart confluence, reward:risk, whether the author says they are IN it, " +
     "how fresh it is, whether the rest of the room agrees, and the author's measured " +
     "alpha where the backtest has a big enough sample. An ordering — not a probability, " +
     "and not calibrated against outcomes."],
    ["Entry / exit bar", `open above rank ${m.entryFloor}, close below ${m.exitFloor}`,
     "two bars is hysteresis — one would churn a call sitting on it"],
    ["The screens",
     `${r.lookbackDays || 10}d window · directional calls only · ≥${r.minLiveRr || 1.2}R still on the table`,
     "a watching call waits for its own entry to trigger; a setup that has already run is " +
     "not chased, because the same levels at a worse price are a different trade"],
    ["Cash floor", `${pct(m.minCashPct)} — max ${pct(m.maxDeployedPct)} deployed`,
     "looser than the signal book's 30%: this book is measuring a source, not surviving a drawdown"],
    ["Concentration",
     `${m.maxPositions} positions · ${pct(m.maxBucketPct)} per correlated bucket`,
     "raised on purpose — this book IS one bet (crypto), and the signal book's 20% cap " +
     "would stop it trading after three names"],
    ["Exits",
     `half off at target · ${pct(m.trailGivebackPct)} giveback trail · stale at ${m.staleAfterDays}d`,
     "stale does the most work here: almost no call is ever explicitly closed, the room " +
     "simply stops mentioning the name"],
    ["Fills", `${m.slippageBps}bps slippage, ${m.feeBps}bps fees`,
     "three times the signal book's — this is size taken in alts off a Telegram post"],
  ];
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">C4</span>
          <span>Mandate</span>
          <span className="block-sub">snapshotted when the book opened · config/paper_cohort.json</span>
        </div>
      </header>
      <dl className="paper-mandate">
        {rows.map(([k, v, hint]) => (
          <React.Fragment key={k}>
            <dt>{k}</dt>
            <dd>{v}{hint && <span className="pm-hint">{hint}</span>}</dd>
          </React.Fragment>
        ))}
      </dl>
    </div>
  );
}
