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
  const [busy, setBusy] = React.useState(0);
  const [err, setErr] = React.useState(null);
  const [over, setOver] = React.useState(false);
  const fileRef = React.useRef(null);

  // Charts arrive as a batch off one screen and each one is about a
  // different ticker, so nothing is declared here. The queue below asks
  // per chart, which is the only order that survives a six-file drop.
  const send = React.useCallback((fileList) => {
    const files = [...(fileList || [])].filter(f => f && f.type.startsWith("image/"));
    if (!files.length) return;
    setBusy(files.length); setErr(null);
    const fd = new FormData();
    files.forEach(f => fd.append("files", f));
    fetch("/api/chartlab/drop", { method: "POST", body: fd })
      .then(r => (r.ok ? r.json() : r.json().then(j => Promise.reject(j.detail || r.status))))
      .then(j => {
        const bad = j.errors || [];
        if (bad.length) setErr(bad.map(e => `${e.filename}: ${e.error}`).join(" · "));
        onParked && onParked(j);
      })
      .catch(e => setErr(String(e)))
      .finally(() => setBusy(0));
  }, [onParked]);

  // Paste-from-clipboard: the shortest route from a chart on screen to a
  // chart parked, and the reason this page exists rather than the CLI.
  React.useEffect(() => {
    function onPaste(e) {
      const items = (e.clipboardData && e.clipboardData.items) || [];
      const imgs = [...items]
        .filter(it => it.type && it.type.startsWith("image/"))
        .map(it => it.getAsFile())
        .filter(Boolean);
      if (imgs.length) send(imgs);
    }
    document.addEventListener("paste", onPaste);
    return () => document.removeEventListener("paste", onPaste);
  }, [send]);

  return (
    <div className="block cl-drop-block">
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">U4</span>
          <span>Drop charts</span>
          <span className="block-sub">desk read · never counts as a trusted voice</span>
        </div>
      </header>
      <div className="block-body">
        <div
          className={`cl-dropzone${over ? " is-over" : ""}${busy ? " is-busy" : ""}`}
          onDragOver={e => { e.preventDefault(); setOver(true); }}
          onDragLeave={() => setOver(false)}
          onDrop={e => { e.preventDefault(); setOver(false); send(e.dataTransfer.files); }}
          onClick={() => fileRef.current && fileRef.current.click()}
        >
          {busy
            ? `parking ${busy} ${busy === 1 ? "chart" : "charts"}…`
            : "drop charts · paste from clipboard · or click to pick"}
          <div className="cl-dropzone-sub">
            several at once is fine — tag each one below
          </div>
          <input ref={fileRef} type="file" accept="image/*" multiple
                 style={{ display: "none" }}
                 onChange={e => { send(e.target.files); e.target.value = ""; }} />
        </div>
        {err && <div className="cl-err">{err}</div>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The queue — one row per parked chart, tagged where it sits
// ---------------------------------------------------------------------------

// A chart's declared metadata is what the read is handed and what the
// bench follows, so the row edits it in place rather than sending the
// operator somewhere else. Saves happen on change (selects) and on blur
// (free text): a Save button per row is one more thing to forget.
function ChartRow({ chart, vocab, onRead, reading, onSaved, onDeleted }) {
  const [ticker, setTicker] = React.useState(chart.ticker || "");
  const [timeframe, setTimeframe] = React.useState(chart.timeframe || "");
  const [cls, setCls] = React.useState(chart.asset_class || "");
  const [note, setNote] = React.useState(chart.note || "");
  const [state, setState] = React.useState(null);   // saving | saved | error text
  const [confirming, setConfirming] = React.useState(false);
  const [zoom, setZoom] = React.useState(false);

  // A row remounts on every list refresh; keep it showing what the
  // server last confirmed rather than a stale local draft.
  React.useEffect(() => {
    setTicker(chart.ticker || ""); setTimeframe(chart.timeframe || "");
    setCls(chart.asset_class || ""); setNote(chart.note || "");
  }, [chart.document_id, chart.ticker, chart.timeframe, chart.asset_class, chart.note]);

  const save = React.useCallback((patch) => {
    const body = {
      ticker, timeframe, note, asset_class: cls,
      ...patch,
    };
    const unchanged =
      (body.ticker || "") === (chart.ticker || "") &&
      (body.timeframe || "") === (chart.timeframe || "") &&
      (body.asset_class || "") === (chart.asset_class || "") &&
      (body.note || "") === (chart.note || "");
    if (unchanged) return;
    setState("saving");
    fetch(`/api/chartlab/chart/${chart.document_id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(r => (r.ok ? r.json() : r.json().then(j => Promise.reject(j.detail || r.status))))
      .then(() => { setState("saved"); onSaved && onSaved(); })
      .catch(e => setState(String(e)));
  }, [chart, ticker, timeframe, cls, note, onSaved]);

  function remove() {
    setState("saving");
    fetch(`/api/chartlab/chart/${chart.document_id}`, { method: "DELETE" })
      .then(r => (r.ok ? r.json() : r.json().then(j => Promise.reject(j.detail || r.status))))
      .then(j => onDeleted && onDeleted(j))
      .catch(e => { setState(String(e)); setConfirming(false); });
  }

  const untagged = !ticker;
  const gone = chart.has_read ? (chart.removed_signals || 0) : 0;

  return (
    <div className={`cl-queue-row${untagged ? " is-untagged" : ""}`}>
      <div className="cl-queue-top">
        {chart.image_url
          ? <img className="cl-thumb" src={chart.image_url} alt=""
                 title="click to enlarge" onClick={() => setZoom(z => !z)} />
          : <div className="cl-thumb cl-thumb-none" />}

        <input
          className="cl-input cl-input-tkr" list="cl-tickers" placeholder="ticker"
          value={ticker}
          onChange={e => setTicker(e.target.value.toUpperCase())}
          onBlur={() => save({})}
          onKeyDown={e => { if (e.key === "Enter") e.target.blur(); }}
        />
        <select className="cl-input cl-select" value={timeframe}
                onChange={e => { setTimeframe(e.target.value); save({ timeframe: e.target.value }); }}>
          <option value="">tf</option>
          {(vocab.timeframes || []).map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        <select className="cl-input cl-select" value={cls}
                onChange={e => { setCls(e.target.value); save({ asset_class: e.target.value }); }}>
          <option value="">class</option>
          {(vocab.asset_classes || []).map(t => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>

      {zoom && chart.image_url &&
        <img className="cl-zoom" src={chart.image_url} alt=""
             onClick={() => setZoom(false)} />}

      <div className="cl-queue-bot">
        <input className="cl-input cl-input-note" placeholder="note — what you see"
               value={note}
               onChange={e => setNote(e.target.value)}
               onBlur={() => save({})}
               onKeyDown={e => { if (e.key === "Enter") e.target.blur(); }} />

        {chart.has_read
          ? <span className="cl-badge cl-badge-ok">read</span>
          : (
            <button className="btn-mini" disabled={reading === chart.document_id}
                    onClick={() => onRead(chart)}>
              {reading === chart.document_id ? "reading…" : "read"}
            </button>
          )}

        {confirming
          ? (
            <span className="cl-confirm">
              <button className="btn-mini cl-danger" onClick={remove}>delete</button>
              <button className="btn-mini" onClick={() => setConfirming(false)}>keep</button>
            </span>
          )
          : (
            <button className="cl-x" title="remove this chart"
                    onClick={() => setConfirming(true)}>×</button>
          )}
      </div>

      <div className="cl-queue-foot muted">
        <span>{(chart.ingested_at || "").slice(0, 16).replace("T", " ")}</span>
        {state === "saving" && <span>saving…</span>}
        {state === "saved" && <span className="cl-saved">saved</span>}
        {state && state !== "saving" && state !== "saved" &&
          <span className="cl-save-err">{state}</span>}
        {confirming &&
          <span className="cl-save-err">
            {chart.has_read
              ? "deletes the chart and the signals its read emitted"
              : "deletes the chart and its image"}
          </span>}
        {chart.has_read && !confirming &&
          <span>a read is on file — re-read to move it</span>}
      </div>
    </div>
  );
}

function ChartQueue({ charts, vocab, onRead, reading, onSaved, onDeleted }) {
  if (!charts || !charts.length) {
    return <div className="muted cl-empty">No desk charts yet. Drop some above.</div>;
  }
  return (
    <div className="cl-queue">
      <datalist id="cl-tickers">
        {(vocab.tickers || []).map(t => <option key={t} value={t} />)}
      </datalist>
      {charts.map(c => (
        <ChartRow
          key={c.document_id} chart={c} vocab={vocab}
          onRead={onRead} reading={reading}
          onSaved={onSaved} onDeleted={onDeleted}
        />
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
      <header className="block-head">
        <div className="block-title">
          <span className="block-num mono">U4</span>
          <span>{card.ticker}</span>
          <span className="muted cl-resolved mono">{card.resolved_symbol}</span>
          <span className="block-sub mono">
            {clFmt(card.close)} · ATR {clFmt(card.atr)} · {card.n_bars} bars
          </span>
        </div>
      </header>

      <div className="block-body">
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
    </div>
  );
}

// ---------------------------------------------------------------------------

function ChartLab() {
  const [charts, setCharts] = React.useState([]);
  const [vocab, setVocab] = React.useState({});
  const [ticker, setTicker] = React.useState("");
  const [card, setCard] = React.useState(null);
  const [loading, setLoading] = React.useState(false);
  const [reading, setReading] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [note, setNote] = React.useState(null);

  const loadCharts = React.useCallback(() => {
    fetch("/api/chartlab/charts?limit=25")
      .then(r => r.json())
      .then(j => setCharts(j.charts || []))
      .catch(() => {});
  }, []);

  React.useEffect(loadCharts, [loadCharts]);

  // The picklists come from the server so they stay in lockstep with
  // the extraction contract instead of being retyped here.
  React.useEffect(() => {
    fetch("/api/chartlab/vocab")
      .then(r => r.json())
      .then(setVocab)
      .catch(() => {});
  }, []);

  function onDeleted(res) {
    const rm = (res && res.removed) || {};
    const bits = [];
    if (rm.signals) bits.push(`${rm.signals} signal${rm.signals === 1 ? "" : "s"}`);
    if (rm.call_outcomes) bits.push(`${rm.call_outcomes} scored call${rm.call_outcomes === 1 ? "" : "s"}`);
    setNote(bits.length ? `Chart deleted — ${bits.join(" and ")} went with it.` : "Chart deleted.");
    loadCharts();
  }

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
        <ChartDrop onParked={() => { setNote(null); loadCharts(); }} />
        <div className="block">
          <header className="block-head">
            <div className="block-title">
              <span className="block-num mono">U4</span>
              <span>Desk charts</span>
              {charts.length
                ? <span className="block-sub">{charts.length} parked</span>
                : null}
            </div>
            <div className="block-actions">
              <button className="btn-mini" onClick={loadCharts}>refresh</button>
            </div>
          </header>
          <div className="block-body">
            {note && (
              <div className="cl-note-bar">
                {note}
                <button className="cl-x" onClick={() => setNote(null)}>×</button>
              </div>
            )}
            <ChartQueue
              charts={charts} vocab={vocab} onRead={readChart} reading={reading}
              onSaved={loadCharts} onDeleted={onDeleted}
            />
          </div>
        </div>
      </div>

      <div className="cl-right">
        <div className="cl-bench-bar">
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
          <div className="cl-placeholder">
            Drop a chart, read it, then bench the ticker. The card composes your
            read with structure, trusted-voice levels and the framework grade.
          </div>}
      </div>
    </div>
  );
}
