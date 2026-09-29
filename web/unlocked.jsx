// /unlocked — the Stock Unlocked Trades call tracker.
//
// This channel is the one source that states its levels in words —
// ticker, side, entry, up to six targets, stop — so its calls can be
// resolved the honest way: walk the tape from the moment of the post and
// let the level that printed FIRST decide. The page shows that verdict
// next to the desk's own account ("Target 2 HIT", "stopped out") on
// every call, because the two disagree sometimes and the disagreement is
// the interesting part: KTOS gapped through its stop at the open and then
// ran to every target; SATL printed its stop by a cent the day before it
// hit T2.
//
// Reuses paper.jsx's `pctCell` and components.jsx's `Sparkline`; loads
// after paper.jsx in index.html for that reason.

function useUnlocked(path, deps = []) {
  const [data, setData] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const reload = React.useCallback(() => {
    let live = true;
    setLoading(true);
    fetch(`/api/stock-unlocked/${path}`)
      .then(r => (r.ok ? r.json() : r.json().then(j => Promise.reject(j.detail || r.status))))
      .then(j => { if (live) { setData(j); setErr(null); } })
      .catch(e => { if (live) setErr(String(e)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [path]);
  React.useEffect(reload, [reload, ...deps]);
  return { data, err, loading, reload };
}

const UNL_VERDICT_LABEL = {
  win: "win", loss: "loss", loss_ambiguous: "loss (tied bar)", open: "open",
  unresolved: "no level hit", unpriceable: "unpriceable", no_levels: "options",
  unscored: "not scored", void: "voided", not_triggered: "never triggered",
};
const UNL_VERDICT_TONE = {
  win: "pos", loss: "neg", loss_ambiguous: "neg", open: "", unresolved: "muted",
  unpriceable: "muted", no_levels: "muted", unscored: "muted", void: "muted", not_triggered: "muted",
};

function rCell(v, digits = 2) {
  if (v === null || v === undefined) return <span className="muted">—</span>;
  const cls = v > 0 ? "pos" : v < 0 ? "neg" : "";
  return <span className={`mono ${cls}`}>{v > 0 ? "+" : ""}{Number(v).toFixed(digits)}R</span>;
}

function unlPx(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  return n >= 100 ? n.toFixed(2) : n >= 1 ? n.toFixed(2) : n.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
}

function unlWhen(iso) {
  if (!iso) return "—";
  return String(iso).slice(0, 16).replace("T", " ");
}

function unlHours(h) {
  if (h === null || h === undefined) return "—";
  if (h < 48) return `${Number(h).toFixed(h < 10 ? 1 : 0)}h`;
  return `${(h / 24).toFixed(1)}d`;
}

function Unlocked() {
  const [tab, setTab] = React.useState(() => {
    try { return localStorage.getItem("unlocked.tab") || "tracker"; } catch (e) { return "tracker"; }
  });
  const pick = (t) => { setTab(t); try { localStorage.setItem("unlocked.tab", t); } catch (e) {} };
  return (
    <div className="paper-view unlocked-view">
      <div className="unl-tabs">
        <div className="seg">
          <button className={tab === "tracker" ? "seg-on" : ""} onClick={() => pick("tracker")}>Call tracker</button>
          <button className={tab === "book" ? "seg-on" : ""} onClick={() => pick("book")}>$50k copy book</button>
        </div>
        <span className="muted unl-tabs-note">
          {tab === "tracker"
            ? "every call resolved against the tape, next to the desk's own account"
            : "a paper book that follows the channel in and out — same engine as /paper and /cohort"}
        </span>
      </div>
      {tab === "tracker" ? <UnlockedTracker /> : <UnlockedBook />}
    </div>
  );
}

function UnlockedTracker() {
  const summary = useUnlocked("summary");
  const calls = useUnlocked("calls?verdict=all&limit=500");
  const [syncing, setSyncing] = React.useState(false);
  const [syncMsg, setSyncMsg] = React.useState(null);

  const runSync = () => {
    setSyncing(true);
    setSyncMsg(null);
    fetch("/api/stock-unlocked/sync", { method: "POST" })
      .then(r => r.json())
      .then(j => {
        setSyncMsg(`${j.new} new · ${j.scored} re-walked · ${j.errors} errors`);
        summary.reload(); calls.reload();
      })
      .catch(e => setSyncMsg(`sync failed: ${e}`))
      .finally(() => setSyncing(false));
  };

  const s = summary.data;
  const all = (calls.data && calls.data.calls) || [];
  const open = all.filter(c => c.verdict === "open");
  const resolved = all.filter(c => ["win", "loss", "loss_ambiguous", "unresolved"].includes(c.verdict));
  const options = all.filter(c => c.instrument === "option");
  const unpriceable = all.filter(c => c.verdict === "unpriceable");

  if (summary.loading && !s) {
    return <div className="block"><div className="empty-state muted">Loading the ledger…</div></div>;
  }
  if (summary.err && !s) {
    return <div className="block"><div className="empty-state muted">{summary.err}</div></div>;
  }

  return (
    <div className="paper-view">
      <UnlockedPreamble s={s} onSync={runSync} syncing={syncing} syncMsg={syncMsg} />
      <UnlockedScoreboard s={s} />
      <div className="two-col-6040">
        <UnlockedBreakdown s={s} />
        <div>
          <UnlockedCurve s={s} />
          <UnlockedReach s={s} />
        </div>
      </div>
      <UnlockedOpen calls={open} loading={calls.loading} />
      <UnlockedResolved calls={resolved} unpriceable={unpriceable} />
      <UnlockedOptions calls={options} s={s} />
    </div>
  );
}

// ── Preamble ─────────────────────────────────────────────────────────

function UnlockedPreamble({ s, onSync, syncing, syncMsg }) {
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">K1</span>
          <span>Stock Unlocked Trades — every call, resolved against the tape</span>
          <span className="block-sub">
            entry, targets and stop are stated in words, so each call is walked
            forward from the post and the level that printed first decides.
            The desk's own "HIT" / "stopped out" posts sit alongside, not instead.
          </span>
        </div>
        <div className="block-actions">
          {syncMsg && <span className="muted mono unl-sync-msg">{syncMsg}</span>}
          <button className="btn-mini" onClick={onSync} disabled={syncing}>
            {syncing ? "walking the tape…" : "sync now"}
          </button>
        </div>
      </header>
      <div className="cohort-note muted">
        A <b>win</b> is the first target printing before the stop; a <b>loss</b> is
        the stop printing first. R is realised to the furthest target reached
        before the stop, on the stated risk. Day trades get {s.horizon_days.day} days
        to resolve, swings {s.horizon_days.swing}; a call that touches neither level
        in that window counts in neither column. Options carry no levels and are
        kept on the desk's own premium numbers, unverified.
        {s.tracking_since ? ` Tracking since ${String(s.tracking_since).slice(0, 10)}.` : ""}
        {s.last_synced_at ? ` Last walk ${unlWhen(s.last_synced_at)} UTC.` : ""}
      </div>
    </div>
  );
}

// ── Scoreboard ───────────────────────────────────────────────────────

function UnlockedScoreboard({ s }) {
  const o = s.overall;
  const d = s.desk;
  const op = s.options_summary;
  const pf = o.profit_factor;
  const agreeN = d.tape_agrees_win + d.tape_agrees_loss;
  const claimN = d.claimed_win + d.claimed_loss;
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">K2</span>
          <span>Scoreboard</span>
          <span className="block-sub">
            {o.resolved} resolved · {o.open} open · {o.unresolved} timed out ·
            {" "}{o.unpriceable} unpriceable · {s.options} options
            {o.void ? ` · ${o.void} voided` : ""}{o.not_triggered ? ` · ${o.not_triggered} never triggered` : ""}
          </span>
        </div>
      </header>
      <div className="perf-stats">
        <div className="ps">
          <div className="ps-lbl">Setup win rate</div>
          <div className="ps-val mono">
            {o.win_rate === null ? <span className="muted">—</span> : `${o.win_rate.toFixed(0)}%`}
          </div>
          <div className="ps-sub">{o.wins}W · {o.losses}L · first target before stop</div>
        </div>
        <div className="ps">
          <div className="ps-lbl">Expectancy</div>
          <div className="ps-val">{rCell(o.avg_r)}</div>
          <div className="ps-sub">per resolved call · planned {o.avg_planned_r ?? "—"}R to T1</div>
        </div>
        <div className="ps">
          <div className="ps-lbl">Total</div>
          <div className="ps-val">{rCell(o.total_r)}</div>
          <div className="ps-sub">1R risked on every call</div>
        </div>
        <div className="ps">
          <div className="ps-lbl">Profit factor</div>
          <div className="ps-val mono">
            {pf === null || pf === undefined ? <span className="muted">—</span> : pf >= 999 ? "∞" : pf.toFixed(2)}
          </div>
          <div className="ps-sub">R won / R lost</div>
        </div>
        <div className="ps">
          <div className="ps-lbl">Time to resolve</div>
          <div className="ps-val mono">{unlHours(o.median_hours_to_resolve)}</div>
          <div className="ps-sub">median · streak {s.streak.kind ? `${s.streak.n}${s.streak.kind}` : "—"}</div>
        </div>
        <div className="ps">
          <div className="ps-lbl">Desk vs tape</div>
          <div className="ps-val mono">
            {claimN ? `${Math.round(100 * agreeN / claimN)}%` : <span className="muted">—</span>}
          </div>
          <div className="ps-sub">
            agree on {agreeN}/{claimN} · desk says {d.desk_win_rate === null ? "—" : `${d.desk_win_rate.toFixed(0)}%`} win
            {d.silent_resolved ? ` · ${d.silent_resolved} never followed up` : ""}
          </div>
        </div>
        <div className="ps">
          <div className="ps-lbl">Options (desk's numbers)</div>
          <div className="ps-val mono">
            {op.win_rate === null ? <span className="muted">—</span> : `${op.win_rate.toFixed(0)}%`}
          </div>
          <div className="ps-sub">
            {op.wins}W · {op.losses}L · avg {op.avg_pnl_pct === null ? "—" : `${op.avg_pnl_pct > 0 ? "+" : ""}${op.avg_pnl_pct.toFixed(1)}%`} premium
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Breakdown ────────────────────────────────────────────────────────

const UNL_AXES = [
  ["by_kind", "Day vs swing"],
  ["by_instrument", "Stock vs crypto"],
  ["by_direction", "Long vs short"],
  ["by_month", "By month"],
  ["by_author", "By alerter"],
  ["by_format", "Old vs new template"],
  ["windows", "Trailing"],
];

function UnlockedBreakdown({ s }) {
  const [axis, setAxis] = React.useState("by_kind");
  const rows = (s[axis] || []).map(r => ({
    ...r, key: axis === "windows" ? `last ${r.days}d` : r.key,
  }));
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">K3</span>
          <span>Where the edge is</span>
          <span className="block-sub">same rules, one slice at a time</span>
        </div>
      </header>
      <div className="perf-axis">
        {UNL_AXES.map(([k, label]) => (
          <button key={k} className={`btn-mini ${axis === k ? "on" : ""}`} onClick={() => setAxis(k)}>
            {label}
          </button>
        ))}
      </div>
      <div className="perf-table unl-breakdown">
        <div className="pt-head">
          <span>Slice</span>
          <span className="pt-num">Calls</span>
          <span className="pt-num">Resolved</span>
          <span className="pt-num">Win</span>
          <span className="pt-num">Avg R</span>
          <span className="pt-num">Total R</span>
          <span className="pt-num">PF</span>
        </div>
        {rows.map(r => (
          <div key={r.key} className="pt-row">
            <span className="pt-name mono">{r.key}</span>
            <span className="pt-num mono">{r.n}</span>
            <span className="pt-num mono">{r.resolved}</span>
            <span className="pt-num mono">
              {r.win_rate === null ? <span className="muted">—</span> : `${r.win_rate.toFixed(0)}%`}
              <i className="pt-tickers"> {r.wins}/{r.resolved}</i>
            </span>
            <span className="pt-num">{rCell(r.avg_r)}</span>
            <span className="pt-num">{rCell(r.total_r)}</span>
            <span className="pt-num mono">
              {r.profit_factor === null || r.profit_factor === undefined
                ? <span className="muted">—</span>
                : r.profit_factor >= 999 ? "∞" : r.profit_factor.toFixed(2)}
            </span>
          </div>
        ))}
        {!rows.length && <div className="empty-state muted">Nothing resolved on this slice yet.</div>}
      </div>
      <div className="cohort-note muted unl-foot">
        Under ten resolved calls a slice is a hint, not a number. Calls before
        March 2026 came through the old alert-bot template (several alerters,
        breakout triggers, no reliable quoted price); their first two hours are
        walked on hourly bars, not five-minute ones.
      </div>
    </div>
  );
}

// ── Cumulative R ─────────────────────────────────────────────────────

function UnlockedCurve({ s }) {
  const pts = s.curve || [];
  const series = [0, ...pts.map(p => p.cum_r)];
  const last = series[series.length - 1];
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">K4</span>
          <span>Cumulative R</span>
          <span className="block-sub">{pts.length} resolved, in the order they resolved</span>
        </div>
      </header>
      {series.length < 3 ? (
        <div className="empty-state muted">Two resolved calls make a line, not a curve.</div>
      ) : (
        <div className="paper-curve">
          <Sparkline
            data={series}
            width={320}
            height={64}
            color={last >= 0 ? "var(--pos)" : "var(--neg)"}
            area
            marker
          />
          <div className="paper-curve-foot muted mono">
            low {Math.min(...series).toFixed(2)}R · high {Math.max(...series).toFixed(2)}R · now {rCell(last)}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Target reach ─────────────────────────────────────────────────────

function UnlockedReach({ s }) {
  const rows = (s.target_reach || []).filter(r => r.eligible >= 3);
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">K5</span>
          <span>How far the winners run</span>
          <span className="block-sub">share of resolved calls that printed ≥ Tn before the stop</span>
        </div>
      </header>
      <div className="unl-reach">
        {rows.map(r => (
          <div key={r.target} className="unl-reach-row">
            <span className="mono unl-reach-lbl">T{r.target}</span>
            <span className="unl-reach-bar">
              <i style={{ width: `${Math.max(2, r.pct || 0)}%` }} />
            </span>
            <span className="mono unl-reach-val">
              {r.pct === null ? "—" : `${Math.round(r.pct)}%`}
              <i className="pt-tickers"> {r.hit}/{r.eligible}</i>
            </span>
          </div>
        ))}
        {!rows.length && <div className="empty-state muted">Not enough resolved calls.</div>}
      </div>
    </div>
  );
}

// ── Open calls ───────────────────────────────────────────────────────

function UnlockedOpen({ calls, loading }) {
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">K6</span>
          <span>Open calls</span>
          <span className="block-sub">
            {calls.length} live · marked at the last quote, progress is to the next unhit target
          </span>
        </div>
      </header>
      {loading && !calls.length ? (
        <div className="empty-state muted">Loading…</div>
      ) : !calls.length ? (
        <div className="empty-state muted">Nothing open. Every call has resolved or timed out.</div>
      ) : (
        <div className="perf-table unl-open">
          <div className="pt-head">
            <span>Ticker</span>
            <span>Posted</span>
            <span className="pt-num">Entry</span>
            <span className="pt-num">Stop</span>
            <span className="pt-num">Next target</span>
            <span className="pt-num">Mark</span>
            <span className="pt-num">Open R</span>
            <span className="pt-num">MFE / MAE</span>
            <span>Desk says</span>
          </div>
          {calls.map(c => {
            const p = c.progress || {};
            return (
              <div key={c.call_id} className="pt-row">
                <span className="pt-name mono">
                  {c.ticker}
                  <i className="pt-tickers">{c.instrument} {c.trade_kind} · {c.direction}</i>
                </span>
                <span className="muted mono unl-when">
                  {unlWhen(c.posted_at)}
                  <i className="pt-tickers"> until {String(c.horizon_end).slice(0, 10)}</i>
                </span>
                <span className="pt-num mono">{unlPx(c.entry)}</span>
                <span className="pt-num mono">{unlPx(c.stop)}</span>
                <span className="pt-num mono">
                  {unlPx(p.next_target)}
                  <i className="pt-tickers"> T{c.max_target + 1}/{c.n_targets}</i>
                </span>
                <span className="pt-num mono">
                  {unlPx(p.mark)}
                  {p.move_pct !== null && p.move_pct !== undefined && (
                    <i className={`pt-tickers ${p.move_pct >= 0 ? "pos" : "neg"}`}>
                      {" "}{p.move_pct >= 0 ? "+" : ""}{p.move_pct.toFixed(2)}%
                    </i>
                  )}
                </span>
                <span className="pt-num">{rCell(p.open_r)}</span>
                <span className="pt-num mono">
                  <span className="pos">{c.mfe_pct === null ? "—" : `${c.mfe_pct >= 0 ? "+" : ""}${Number(c.mfe_pct).toFixed(1)}%`}</span>
                  {" / "}
                  <span className="neg">{c.mae_pct === null ? "—" : `${Number(c.mae_pct).toFixed(1)}%`}</span>
                </span>
                <span className="muted pt-why">
                  {c.desk_verdict === "open" ? "nothing yet" : c.desk_verdict}
                  {c.desk_max_target ? ` · T${c.desk_max_target} reported` : ""}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Resolved ─────────────────────────────────────────────────────────

function UnlockedResolved({ calls, unpriceable }) {
  const [filter, setFilter] = React.useState("all");
  const shown = calls.filter(c =>
    filter === "all" ? true
      : filter === "wins" ? c.verdict === "win"
      : filter === "losses" ? c.verdict.startsWith("loss")
      : filter === "disagree" ? disagrees(c)
      : true
  );
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">K7</span>
          <span>Resolved calls</span>
          <span className="block-sub">
            {calls.length} · ≠ marks a call where the desk and the tape disagree
            {unpriceable.length ? ` · ${unpriceable.length} unpriceable (${unpriceable.map(c => c.ticker).join(", ")})` : ""}
          </span>
        </div>
        <div className="block-actions">
          <div className="seg">
            {[["all", "all"], ["wins", "wins"], ["losses", "losses"], ["disagree", "≠"]].map(([k, l]) => (
              <button key={k} className={filter === k ? "seg-on" : ""} onClick={() => setFilter(k)}>{l}</button>
            ))}
          </div>
        </div>
      </header>
      {!shown.length ? (
        <div className="empty-state muted">Nothing here.</div>
      ) : (
        <div className="perf-table unl-resolved">
          <div className="pt-head">
            <span>Ticker</span>
            <span>Posted</span>
            <span>Tape</span>
            <span className="pt-num">Reached</span>
            <span className="pt-num">R</span>
            <span className="pt-num">MFE / MAE</span>
            <span className="pt-num">Took</span>
            <span>Desk says</span>
          </div>
          {shown.map(c => (
            <div key={c.call_id} className={`pt-row ${disagrees(c) ? "unl-disagree" : ""}`}>
              <span className="pt-name mono">
                {c.ticker}
                <i className="pt-tickers">{c.instrument} {c.trade_kind} · {c.direction}{c.author ? ` · ${c.author}` : ""}</i>
              </span>
              <span className="muted mono unl-when">
                {unlWhen(c.posted_at)}
                <i className="pt-tickers"> {c.trigger ? `${c.trigger} ` : ""}{unlPx(c.entry)} → {unlPx(c.targets && c.targets[0])} / {unlPx(c.stop)}</i>
              </span>
              <span className={UNL_VERDICT_TONE[c.verdict] || ""}>
                {UNL_VERDICT_LABEL[c.verdict] || c.verdict}
                {c.resolution && c.resolution !== "1h" && <i className="pt-tickers"> {c.resolution.replace("_", " ")}</i>}
              </span>
              <span className="pt-num mono">T{c.max_target}/{c.n_targets}</span>
              <span className="pt-num">{rCell(c.realized_r)}</span>
              <span className="pt-num mono">
                <span className="pos">{c.mfe_pct === null ? "—" : `${c.mfe_pct >= 0 ? "+" : ""}${Number(c.mfe_pct).toFixed(1)}%`}</span>
                {" / "}
                <span className="neg">{c.mae_pct === null ? "—" : `${Number(c.mae_pct).toFixed(1)}%`}</span>
              </span>
              <span className="pt-num mono">{unlHours(c.hours_to_resolve)}</span>
              <span className="muted pt-why">
                {disagrees(c) && <b className="unl-neq">≠ </b>}
                {c.desk_verdict === "open" ? "never followed up" : c.desk_verdict}
                {c.desk_max_target ? ` · T${c.desk_max_target}` : ""}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function disagrees(c) {
  if (!["win", "loss"].includes(c.desk_verdict)) return false;
  if (!["win", "loss", "loss_ambiguous"].includes(c.verdict)) return false;
  return (c.desk_verdict === "win") !== (c.verdict === "win");
}

// ── Options ──────────────────────────────────────────────────────────

function UnlockedOptions({ calls, s }) {
  const [show, setShow] = React.useState(true);
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">K8</span>
          <span>Options — on the desk's own numbers</span>
          <span className="block-sub">
            {calls.length} positions · premium P&amp;L from their exit posts, size-weighted, unverified
          </span>
        </div>
        <div className="block-actions">
          <button className="btn-mini" onClick={() => setShow(!show)}>{show ? "hide" : "show"}</button>
        </div>
      </header>
      {!show ? (
        <div className="block-quiet muted">Hidden.</div>
      ) : !calls.length ? (
        <div className="empty-state muted">No options calls yet.</div>
      ) : (
        <div className="perf-table unl-options">
          <div className="pt-head">
            <span>Contract</span>
            <span>Posted</span>
            <span className="pt-num">Premium</span>
            <span>Exits</span>
            <span className="pt-num">P&amp;L</span>
            <span>Status</span>
          </div>
          {calls.map(c => {
            const o = c.option || {};
            const exits = o.exits || [];
            const pnl = o.pnl_pct;
            return (
              <div key={c.call_id} className="pt-row">
                <span className="pt-name mono">
                  {c.ticker} {o.type} {o.strike}
                  <i className="pt-tickers">exp {o.expiration || "—"}</i>
                </span>
                <span className="muted mono unl-when">{unlWhen(c.posted_at)}</span>
                <span className="pt-num mono">{o.premium === null || o.premium === undefined ? "—" : `$${o.premium}`}</span>
                <span className="muted unl-exits mono">
                  {exits.length ? exits.map((e, i) => (
                    <span key={i} className="unl-exit">
                      {e.size_pct.toFixed(0)}%{e.exit_premium ? `@$${e.exit_premium}` : " (no price)"}
                      {e.pnl_pct !== null && e.pnl_pct !== undefined && (
                        <i className={e.pnl_pct >= 0 ? "pos" : "neg"}> {e.pnl_pct >= 0 ? "+" : ""}{e.pnl_pct.toFixed(0)}%</i>
                      )}
                    </span>
                  )) : "still open"}
                </span>
                <span className="pt-num">
                  {pnl === null || pnl === undefined
                    ? <span className="muted">—</span>
                    : <span className={`mono ${pnl > 0 ? "pos" : pnl < 0 ? "neg" : ""}`}>{pnl > 0 ? "+" : ""}{pnl.toFixed(1)}%</span>}
                </span>
                <span className="muted pt-why">
                  {c.desk_verdict === "running" ? "running" : c.desk_verdict}
                  {o.remaining_pct ? ` · ${o.remaining_pct.toFixed(0)}% still on` : ""}
                  {o.pnl_complete === false ? " · an exit had no price" : ""}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}


// ── The $50k copy book ───────────────────────────────────────────────
//
// Same blocks as /paper and /cohort (BookHeader, PositionsBlock,
// EquityBlock, DecisionLog, ClosedTrades, PerformanceBlock) — one book UI,
// three data sources — so this file loads after paper.jsx and cohort.jsx.
// The book's id is resolved from /api/paper/books by kind, the way the
// cohort page does it, and every other read is the generic paper API
// with portfolio_id set.

function UnlockedBook() {
  const books = useCohortApi("books");
  const all = (books.data && books.data.books) || [];
  const book = all.find(b => b.kind === "unlocked" && b.status === "active");
  const id = book ? book.portfolioId : null;
  const q = id ? `portfolio_id=${encodeURIComponent(id)}` : "";

  const header = useCohortApi(id ? `portfolio?${q}` : null, [id]);
  const positions = useCohortApi(id ? `positions?status=open&${q}` : null, [id]);
  const [filter, setFilter] = React.useState("all");
  const decisions = useCohortApi(
    id ? `decisions?limit=250&${q}${filter === "all" ? "" : `&action=${filter}`}` : null,
    [id, filter]
  );
  // One point per day: the book ticks every two hours and was replayed
  // over a year, so a raw snapshot limit would show only its last weeks.
  const curve = useCohortApi(id ? `equity-curve?daily=true&limit=2000&${q}` : null, [id]);
  const perf = useCohortApi(id ? `performance?${q}` : null, [id]);

  const [tick, setTick] = React.useState(null);
  const [ticking, setTicking] = React.useState(false);
  const runDryTick = () => {
    setTicking(true);
    fetch("/api/paper/unlocked/tick?dry_run=true", { method: "POST" })
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
      <div className="paper-view">
        <div className="block">
          <div className="empty-state muted">
            <p>The Stock Unlocked book has not been opened yet.</p>
            <p className="mono" style={{ marginTop: 10 }}>
              .venv/bin/python scripts/paper_unlocked_tick.py --bootstrap --execute
            </p>
          </div>
        </div>
        <UnlockedCandidates />
      </div>
    );
  }

  const b = header.data;
  const pts = (curve.data && curve.data.points) || [];

  return (
    <div className="paper-view">
      {b && <BookHeader book={b} cadence="every 2h, behind the tracker tick" />}
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
          {b && <UnlockedMandate mandate={b.mandate} />}
          <UnlockedBookNote book={book} />
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
      <UnlockedCandidates />
    </div>
  );
}

// The funnel and the ranked calls in one block: the window is a handful
// of posts, not four hundred charts, so the denominator fits in a line.
function UnlockedCandidates() {
  const [show, setShow] = React.useState(true);
  const { data, loading } = useCohortApi(show ? "unlocked/candidates?limit=40" : null, [show]);
  const cands = (data && data.candidates) || [];
  const cov = (data && data.coverage) || null;
  const live = cands.filter(c => c.rank > 0);
  const closed = cands.filter(c => c.rank <= 0);

  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">B7</span>
          <span>Live calls, ranked</span>
          <span className="block-sub">
            {cov ? `${cov.considered} posts in the last ${cov.windowDays}d → ${cov.candidates} candidates` : "the channel's stated levels"}
            {closed.length ? ` · ${closed.length} closed by the desk or the tape (rank 0 — how a held name learns to leave)` : ""}
          </span>
        </div>
        <div className="block-actions">
          <button className="btn-mini" onClick={() => setShow(!show)}>{show ? "hide" : "show"}</button>
        </div>
      </header>
      {!show ? (
        <div className="block-quiet muted">Hidden.</div>
      ) : loading ? (
        <div className="empty-state muted">Screening…</div>
      ) : (
        <React.Fragment>
          {cov && (cov.skipped || []).length > 0 && (
            <div className="unl-funnel muted mono">
              {cov.skipped.map(s => (
                <span key={s.reason} className="unl-funnel-item" title={s.meaning}>
                  {s.count} {s.reason.replace(/_/g, " ")}
                </span>
              ))}
            </div>
          )}
          {!cands.length ? (
            <div className="empty-state muted">Nothing in the window clears the screens.</div>
          ) : (
            <div className="cohort-cands">
              {cands.map(c => <CohortCandidate key={c.ticker + c.side} c={c} />)}
            </div>
          )}
        </React.Fragment>
      )}
    </div>
  );
}

function UnlockedMandate({ mandate }) {
  const m = mandate || {};
  const pct = (v, d = 0) => `${(v * 100).toFixed(d)}%`;
  const rows = [
    ["Position size",
     `${pct(m.minPositionPct, 1)}–${pct(m.maxPositionPct)} of equity, by rank`,
     "a call that just clears the bar gets the floor; a rank-100 call gets the ceiling"],
    ["Rank", "built from the call, not from a percentile",
     "reward:risk to the last stated target, whether the desk has already reported a " +
     "target hit, freshness (decays fast — these are short calls), a day-trade haircut " +
     "against a two-hour tick, and the tracker's own expectancy on the call's slice once " +
     "it has ten resolved calls. An ordering, not a probability."],
    ["Entry / exit bar", `open above rank ${m.entryFloor}, close below ${m.exitFloor}`,
     "a call the desk has closed — stopped out, all targets hit — is ranked 0, so the " +
     "book follows the desk out within a couple of ticks"],
    ["Stops", `${pct(m.minStopPct, 1)}–${pct(m.maxStopPct)} from the mark`,
     "the channel's stops as posted; the bounds only catch a parse error"],
    ["Exits",
     `ladder at 0.75R / 1.5R / 2.5R · trail arms at ${m.trailArmsAtR}R · stale at ${m.staleAfterDays}d`,
     "quarters off at each rung with the stop ratcheted behind — the way the desk scales " +
     "out across its targets; the last stated target is a rung of its own"],
    ["Cash floor", `${pct(m.minCashPct)} — max ${pct(m.maxDeployedPct)} deployed`, null],
    ["Concentration", `${m.maxPositions} positions · ${pct(m.maxBucketPct)} per correlated bucket`,
     "half the flow is crypto alts — the signal book's caps would stop it after three names"],
    ["Fills", `${m.slippageBps}bps slippage, ${m.feeBps}bps fees`,
     "small caps and alts off a Telegram post"],
  ];
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">B4</span>
          <span>Mandate</span>
          <span className="block-sub">snapshotted when the book opened · config/paper_stock_unlocked.json</span>
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

// Why the ledger and the book disagree — on the page, because they will.
function UnlockedBookNote({ book }) {
  return (
    <div className="block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">B5</span>
          <span>Reading this next to the tracker</span>
        </div>
      </header>
      <div className="cohort-note muted" style={{ padding: "10px var(--pad-card) 14px" }}>
        The tracker scores every call at 1R and lets the first level that printed
        decide. This book sizes by rank (1.5–5% of equity), fills at the two-hour
        mark rather than the posted entry, scales out on its own ladder and trails
        the runner — so a call the tracker books as +3R can be a small win, a
        scratch or a trail-out here, and the two curves are meant to differ. The
        ledger says whether the calls were right; the book says whether a
        mechanical follower would have been paid for them.
        {book && book.notes ? ` ${book.notes}` : ""}
      </div>
    </div>
  );
}
