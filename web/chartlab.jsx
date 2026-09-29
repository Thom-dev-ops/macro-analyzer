// /chartlab — drop a chart, read it, bench the ticker.
//
// The browser front door onto the same free path the CLI uses: a drop
// writes a `desk:chartlab:` document, a read runs `claude -p` as a local
// subprocess (no billed API), and the card is `chartlab.bench.build_bench`
// rendered rather than recomputed. Nothing is scored in this file — every
// number arrives from /api/chartlab/bench and is only formatted here.
//
// The one thing this page cannot do is argue with a read. A read taken
// here is the automated one; a read taken in the chart-lab chat is a
// conversation about what the chart actually shows. Both write the same
// column, so the card is identical — the read panel names which produced
// it.

function clFmt(v, dash = "—") {
  if (v === null || v === undefined || Number.isNaN(v)) return dash;
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  if (Math.abs(n) >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (Math.abs(n) >= 1) return String(Number(n.toFixed(3)));
  return String(Number(n.toFixed(6)));
}

function clPct(v) {
  if (v === null || v === undefined) return null;
  return `${v >= 0 ? "+" : ""}${v.toFixed(0)}%`;
}

// ---------------------------------------------------------------------------
// Drop + read
// ---------------------------------------------------------------------------

function ChartDrop({ onParked }) {
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const [ticker, setTicker] = React.useState("");
  const [timeframe, setTimeframe] = React.useState("1D");
  const [note, setNote] = React.useState("");
  const [over, setOver] = React.useState(false);
  const fileRef = React.useRef(null);

  const send = React.useCallback((files) => {
    const file = files && files[0];
    if (!file) return;
    setBusy(true); setErr(null);
    const fd = new FormData();
    fd.append("file", file);
    if (ticker) fd.append("ticker", ticker);
    if (timeframe) fd.append("timeframe", timeframe);
    if (note) fd.append("note", note);
    fetch("/api/chartlab/drop", { method: "POST", body: fd })
      .then(r => (r.ok ? r.json() : r.json().then(j => Promise.reject(j.detail || r.status))))
      .then(j => { setNote(""); onParked && onParked(j); })
      .catch(e => setErr(String(e)))
      .finally(() => setBusy(false));
  }, [ticker, timeframe, note, onParked]);

  // Paste-from-clipboard: the shortest route from a chart on screen to a
  // chart parked, and the reason this page exists rather than the CLI.
  React.useEffect(() => {
    function onPaste(e) {
      const items = (e.clipboardData && e.clipboardData.items) || [];
      for (const it of items) {
        if (it.type && it.type.startsWith("image/")) {
          const f = it.getAsFile();
          if (f) { send([f]); return; }
        }
      }
    }
    document.addEventListener("paste", onPaste);
    return () => document.removeEventListener("paste", onPaste);
  }, [send]);

  return (
    <div className="block cl-drop-block">
      <div className="block-head">
        <div className="block-title">Drop a chart</div>
        <div className="block-sub">desk read · never counts as a trusted voice</div>
      </div>
      <div className="cl-meta">
        <input className="cl-input" placeholder="ticker (RIG)" value={ticker}
               onChange={e => setTicker(e.target.value.toUpperCase())} />
        <input className="cl-input cl-input-sm" placeholder="1D" value={timeframe}
               onChange={e => setTimeframe(e.target.value)} />
        <input className="cl-input cl-input-wide" placeholder="note — what you see"
               value={note} onChange={e => setNote(e.target.value)} />
      </div>
      <div
        className={`cl-dropzone${over ? " is-over" : ""}${busy ? " is-busy" : ""}`}
        onDragOver={e => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={e => { e.preventDefault(); setOver(false); send(e.dataTransfer.files); }}
        onClick={() => fileRef.current && fileRef.current.click()}
      >
        {busy ? "parking…" : "drop a chart · paste from clipboard · or click to pick"}
        <input ref={fileRef} type="file" accept="image/*" style={{ display: "none" }}
               onChange={e => send(e.target.files)} />
      </div>
      {err && <div className="cl-err">{err}</div>}
    </div>
  );
}

function ChartQueue({ charts, onRead, reading, onPick }) {
  if (!charts || !charts.length) {
    return <div className="muted cl-empty">No desk charts yet. Drop one above.</div>;
  }
  return (
    <div className="cl-queue">
      {charts.map(c => (
        <div className="cl-queue-row" key={c.document_id}>
          {c.image_url
            ? <img className="cl-thumb" src={c.image_url} alt="" onClick={() => onPick(c)} />
            : <div className="cl-thumb cl-thumb-none" />}
          <div className="cl-queue-body">
            <div className="cl-queue-title">{c.caption || c.title || "(no caption)"}</div>
            <div className="cl-queue-sub muted">
              {(c.ingested_at || "").slice(0, 16).replace("T", " ")}
            </div>
          </div>
          {c.has_read
            ? <span className="cl-badge cl-badge-ok">read</span>
            : (
              <button className="btn-mini" disabled={reading === c.document_id}
                      onClick={() => onRead(c)}>
                {reading === c.document_id ? "reading…" : "read it"}
              </button>
            )}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The card
// ---------------------------------------------------------------------------

function ClRead({ read }) {
  if (!read) {
    return (
      <div className="cl-sec">
        <div className="cl-sec-h">your chart read</div>
        <div className="muted">none on file for this ticker</div>
      </div>
    );
  }
  const backend = read.vision_backend === "chartlab" ? "read in chat" : "automated read";
  return (
    <div className="cl-sec">
      <div className="cl-sec-h">
        your chart read
        <span className="cl-tag">{backend}</span>
        <span className="muted cl-when">{(read._ingested_at || "").slice(0, 10)}</span>
      </div>
      <div className="cl-readline">
        <b>{read.call_type || "?"}</b> · bias {read.bias || "?"} ·{" "}
        {read.pattern || "no pattern named"} · [{read.timeframe || "?"}]
      </div>
      {read.notes && <div className="muted cl-notes">{read.notes}</div>}
    </div>
  );
}

function ClDeskLevels({ desk, side }) {
  if (!desk) return null;
  const rows = ["entry", "stop", "target"];
  return (
    <div className="cl-sec">
      <div className="cl-sec-h">
        your levels vs the agent
        {desk.rr !== null && desk.rr !== undefined &&
          <span className="cl-tag">R:R {desk.rr.toFixed(2)}</span>}
      </div>
      <table className="cl-table">
        <thead>
          <tr><th></th><th>yours</th><th>agent</th><th>gap</th></tr>
        </thead>
        <tbody>
          {rows.map(k => {
            const r = desk.rows[k] || {};
            const gap = r.gap_r;
            return (
              <tr key={k}>
                <td className="cl-role">{k}</td>
                <td className="mono">{clFmt(r.desk)}</td>
                <td className="mono">{clFmt(r.agent)}</td>
                <td className={`mono ${gap > 0 ? "pos" : gap < 0 ? "neg" : ""}`}>
                  {gap === null || gap === undefined ? "—" : `${gap >= 0 ? "+" : ""}${gap.toFixed(2)}R`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {desk.invalidation && <div className="muted cl-notes">invalidation: {desk.invalidation}</div>}
      {desk.entry_reached === false &&
        <div className="muted cl-notes">entry not yet reached — price has not come to your level</div>}
    </div>
  );
}

function ClStructure({ supports, resistances }) {
  const rows = [
    ...(resistances || []).slice(0, 3).map(l => ({ ...l, tag: "res" })).reverse(),
    ...(supports || []).slice(0, 3).map(l => ({ ...l, tag: "sup" })),
  ];
  if (!rows.length) return null;
  return (
    <div className="cl-sec">
      <div className="cl-sec-h">structure</div>
      {rows.map((l, i) => (
        <div className="cl-zone" key={i}>
          <span className={`cl-zone-tag cl-zone-${l.tag}`}>{l.tag}</span>
          <span className="mono">{clFmt(l.low)}–{clFmt(l.high)}</span>
          <span className="muted">
            {l.touches}× · {l.last_touch_bars}d ago{l.flipped ? " · flipped" : ""}
          </span>
        </div>
      ))}
    </div>
  );
}

function ClLevels({ levels, side, basis }) {
  if (!levels) return null;
  return (
    <div className="cl-sec">
      <div className="cl-sec-h">
        levels
        <span className="cl-tag">{levels.method} · {levels.version}</span>
        <span className={`cl-side cl-side-${(side || "").toLowerCase()}`}>{side}</span>
      </div>
      <div className="muted cl-notes">{basis}</div>
      <div className="cl-rails">
        <div><span className="muted">entry</span> <b className="mono">{clFmt(levels.entry)}</b></div>
        <div><span className="muted">stop</span> <b className="mono">{clFmt(levels.stop)}</b></div>
        <div><span className="muted">target</span> <b className="mono">{clFmt(levels.target)}</b></div>
        <div><span className="muted">RR</span> <b className="mono">{levels.rr ? levels.rr.toFixed(2) : "—"}</b></div>
      </div>
      {(levels.provenance || []).slice(0, 5).map((p, i) => (
        <div className="cl-prov" key={i}>
          <span className="cl-role">{p.role}</span>
          <span className="mono">{clFmt(p.value)}</span>
          <span className="muted">{p.basis}{p.who ? ` — ${p.who}` : ""}</span>
        </div>
      ))}
      {(levels.rejected || []).slice(0, 3).map((r, i) => (
        <div className="cl-prov cl-prov-rej" key={`r${i}`}>
          <span className="cl-role">rejected {r.role}</span>
          <span className="mono">{clFmt(r.value)}</span>
          <span className="muted">{r.reason}</span>
        </div>
      ))}
    </div>
  );
}

function ClVoices({ card }) {
  const rows = [
    ["entry", card.kol_entry], ["stop", card.kol_stop], ["target", card.kol_target],
  ].filter(([, c]) => c);
  return (
    <div className="cl-sec">
      <div className="cl-sec-h">
        trusted voices
        {card.kol_n_signals
          ? <span className="cl-tag">{card.kol_n_signals} signals
              {card.kol_played_out ? ` · ${card.kol_played_out} played out` : ""}</span>
          : null}
      </div>
      {!rows.length && <div className="muted">no levels on file for this ticker</div>}
      {rows.map(([label, c]) => {
        // A level price has already traded through is dead, however
        // credentialled the voice behind it. Say so on the row.
        const stale = c.pct_from_spot !== null && Math.abs(c.pct_from_spot) > 15;
        return (
          <div className="cl-voice" key={label}>
            <div className="cl-voice-top">
              <span className="cl-role">{label}</span>
              <span className="mono">{clFmt(c.price)}</span>
              {c.pct_from_spot !== null &&
                <span className="muted">({clPct(c.pct_from_spot)} vs spot)</span>}
              {stale && <span className="cl-stale">price has left this behind</span>}
            </div>
            <div className="muted cl-voice-basis">{c.basis}</div>
          </div>
        );
      })}
    </div>
  );
}

function ClGrade({ card }) {
  if (!card.grade) return null;
  const tone = /^A/.test(card.grade) ? "pos" : card.grade === "avoid" ? "neg" : "";
  return (
    <div className="cl-sec">
      <div className="cl-sec-h">
        grade
        <span className={`cl-grade ${tone}`}>{card.grade}</span>
        {card.score !== null && <span className="cl-tag">{Math.round(card.score)}/100</span>}
        {card.timing && <span className="cl-tag">{card.timing}</span>}
        {card.setup_type && <span className="cl-tag">{card.setup_type}</span>}
      </div>
      {(card.components || []).map((c, i) => (
        <div className="cl-comp" key={i}>
          <span className="cl-comp-name">
            {String(c.component).replace(/_/g, " ")}
            {c.stub && <span className="cl-stub">unfed</span>}
          </span>
          <span className="cl-comp-bar">
            <span className="cl-comp-fill" style={{ width: `${(c.value || 0) * 100}%` }} />
          </span>
          <span className="mono cl-comp-val">{(c.value || 0).toFixed(2)} × {c.weight}</span>
        </div>
      ))}
    </div>
  );
}

function ClExits({ exits, headroom }) {
  if (!exits) return null;
  return (
    <div className="cl-sec">
      <div className="cl-sec-h">
        exits
        {headroom !== null && headroom !== undefined &&
          <span className="cl-tag">{headroom.toFixed(1)}R headroom</span>}
      </div>
      {(exits.trims || []).map((t, i) => (
        <div className="cl-exit" key={i}>
          <span className="cl-role">trim {Math.round(t.fraction * 100)}%</span>
          <span className="mono">{clFmt(t.at)}</span>
          <span className="muted">{t.basis}</span>
        </div>
      ))}
      <div className="cl-exit">
        <span className="cl-role">runner {Math.round(exits.runner * 100)}%</span>
        <span className="mono">stop {clFmt(exits.stop)}</span>
        <span className="muted">breakeven at {clFmt(exits.move_stop_to_breakeven_at)}</span>
      </div>
    </div>
  );
}

function BenchCard({ card }) {
  return (
    <div className="block cl-card">
      <div className="block-head">
        <div className="block-title">
          {card.ticker}
          <span className="muted cl-resolved">{card.resolved_symbol}</span>
        </div>
        <div className="block-sub mono">
          {clFmt(card.close)} · ATR {clFmt(card.atr)} · {card.n_bars} bars
        </div>
      </div>

      {(card.warnings || []).map((w, i) => (
        <div className="cl-warn" key={i}>{w}</div>
      ))}

      <ClRead read={card.desk_read} />
      <ClDeskLevels desk={card.desk_levels} side={card.side} />
      <ClLevels levels={card.levels} side={card.side} basis={card.side_basis} />
      <ClStructure supports={card.supports} resistances={card.resistances} />
      <ClVoices card={card} />
      <ClGrade card={card} />
      <ClExits exits={card.exits} headroom={card.headroom_r} />
    </div>
  );
}

// ---------------------------------------------------------------------------

function ChartLab() {
  const [charts, setCharts] = React.useState([]);
  const [ticker, setTicker] = React.useState("");
  const [card, setCard] = React.useState(null);
  const [loading, setLoading] = React.useState(false);
  const [reading, setReading] = React.useState(null);
  const [err, setErr] = React.useState(null);

  const loadCharts = React.useCallback(() => {
    fetch("/api/chartlab/charts?limit=25")
      .then(r => r.json())
      .then(j => setCharts(j.charts || []))
      .catch(() => {});
  }, []);

  React.useEffect(loadCharts, [loadCharts]);

  const runBench = React.useCallback((t) => {
    const want = (t || "").trim().toUpperCase();
    if (!want) return;
    setLoading(true); setErr(null);
    fetch(`/api/chartlab/bench/${encodeURIComponent(want)}`)
      .then(r => (r.ok ? r.json() : r.json().then(j => Promise.reject(j.detail || r.status))))
      .then(j => setCard(j))
      .catch(e => { setErr(String(e)); setCard(null); })
      .finally(() => setLoading(false));
  }, []);

  function readChart(c) {
    setReading(c.document_id); setErr(null);
    fetch(`/api/chartlab/read/${c.document_id}`, { method: "POST" })
      .then(r => (r.ok ? r.json() : r.json().then(j => Promise.reject(j.detail || r.status))))
      .then(j => {
        loadCharts();
        if (j.ticker) { setTicker(j.ticker); runBench(j.ticker); }
      })
      .catch(e => setErr(String(e)))
      .finally(() => setReading(null));
  }

  return (
    <div className="cl-view">
      <div className="cl-left">
        <ChartDrop onParked={j => {
          loadCharts();
          if (j.ticker) setTicker(j.ticker);
        }} />
        <div className="block">
          <div className="block-head">
            <div className="block-title">Desk charts</div>
            <div className="block-actions">
              <button className="btn-mini" onClick={loadCharts}>refresh</button>
            </div>
          </div>
          <ChartQueue
            charts={charts} onRead={readChart} reading={reading}
            onPick={c => { if (c.has_read) loadCharts(); }}
          />
        </div>
      </div>

      <div className="cl-right">
        <div className="block cl-bench-bar">
          <input
            className="cl-input cl-input-bench"
            placeholder="bench a ticker — RIG, BTC, URA"
            value={ticker}
            onChange={e => setTicker(e.target.value.toUpperCase())}
            onKeyDown={e => { if (e.key === "Enter") runBench(ticker); }}
          />
          <button className="btn-mini" onClick={() => runBench(ticker)} disabled={loading}>
            {loading ? "benching…" : "bench"}
          </button>
        </div>
        {err && <div className="cl-err">{err}</div>}
        {card && <BenchCard card={card} />}
        {!card && !loading && !err &&
          <div className="muted cl-empty">
            Drop a chart, read it, then bench the ticker. The card composes your
            read with structure, trusted-voice levels and the framework grade.
          </div>}
      </div>
    </div>
  );
}
