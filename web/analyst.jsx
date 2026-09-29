// Chief Analyst — chat over the Macro Analyzer dataset.
//
// Same component drives both the full /analyst page and the floating
// overlay reachable from the header pill / ⌘K. The backend at
// /api/analyst/ask runs a Claude tool-use loop that pulls from
// source_accuracy, KOL calls, themes, conviction, prices, technicals,
// and a read-only SQL escape hatch, then returns a synthesized answer
// with the tool trace attached.

const ANALYST_STORAGE_KEY = "ma_analyst_thread_v1";

const SUGGESTED_QUESTIONS = [
  "Based on all the inputs over the past 2 weeks, where are we expecting crypto to head over the next 7 days?",
  "Which trusted KOLs have the highest alpha in the last 30 days, and what are they calling right now?",
  "Any theme showing breakout momentum this week that isn't already in the top conviction list?",
  "What's the setup on BTC right now — nearest support, resistance, and any active calls near it?",
  "Which currently-active trade calls are closest to their stop, and who called them?",
];

function loadThread() {
  try {
    const raw = localStorage.getItem(ANALYST_STORAGE_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch (_) { return []; }
}
function saveThread(msgs) {
  try { localStorage.setItem(ANALYST_STORAGE_KEY, JSON.stringify(msgs.slice(-60))); } catch (_) {}
}

function AnalystTrace({ trace }) {
  const [open, setOpen] = React.useState(false);
  if (!trace || !trace.length) return null;
  return (
    <div className="analyst-trace">
      <button className="analyst-trace-toggle" onClick={() => setOpen(o => !o)}>
        {open ? "▾" : "▸"} tool calls · {trace.length}
      </button>
      {open && (
        <div className="analyst-trace-list">
          {trace.map((step, i) => (
            <div key={i} className="analyst-trace-row">
              <div className="analyst-trace-name">
                <span className="analyst-trace-tag">{step.tool}</span>
                {step.args_summary && <span className="analyst-trace-args">{step.args_summary}</span>}
                {step.error && <span className="analyst-trace-err">error</span>}
              </div>
              {step.result_summary && (
                <div className="analyst-trace-result">{step.result_summary}</div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AnalystMessage({ msg }) {
  if (msg.role === "user") {
    return (
      <div className="analyst-msg analyst-msg-user">
        <div className="analyst-msg-body">{msg.content}</div>
      </div>
    );
  }
  if (msg.role === "assistant") {
    return (
      <div className="analyst-msg analyst-msg-agent">
        <div className="analyst-msg-body">{msg.content}</div>
        {msg.meta && (
          <div className="analyst-msg-meta">
            {msg.meta.model && <span>{msg.meta.model}</span>}
            {msg.meta.cost_usd != null && <span>· ${msg.meta.cost_usd.toFixed(4)}</span>}
            {msg.meta.latency_ms != null && <span>· {(msg.meta.latency_ms/1000).toFixed(1)}s</span>}
          </div>
        )}
        <AnalystTrace trace={msg.trace} />
      </div>
    );
  }
  if (msg.role === "error") {
    return (
      <div className="analyst-msg analyst-msg-error">
        <div className="analyst-msg-body">⚠ {msg.content}</div>
      </div>
    );
  }
  return null;
}

function ChiefAnalyst({ mode = "page", onClose }) {
  const [messages, setMessages] = React.useState(() => loadThread());
  const [input, setInput] = React.useState("");
  const [deep, setDeep] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const scrollerRef = React.useRef(null);
  const inputRef = React.useRef(null);

  React.useEffect(() => { saveThread(messages); }, [messages]);
  React.useEffect(() => {
    if (scrollerRef.current) scrollerRef.current.scrollTop = scrollerRef.current.scrollHeight;
  }, [messages, busy]);
  React.useEffect(() => {
    if (mode === "overlay" && inputRef.current) inputRef.current.focus();
  }, [mode]);

  const send = async (text) => {
    const question = (text != null ? text : input).trim();
    if (!question || busy) return;
    const userMsg = { role: "user", content: question, ts: Date.now() };
    const nextHistory = messages.concat([userMsg]);
    setMessages(nextHistory);
    setInput("");
    setBusy(true);
    try {
      const historyForApi = nextHistory
        .filter(m => m.role === "user" || m.role === "assistant")
        .slice(-12)
        .map(m => ({ role: m.role, content: m.content }));
      const res = await fetch("/api/analyst/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          model: deep ? "opus" : "sonnet",
          history: historyForApi.slice(0, -1),
        }),
      });
      if (!res.ok) {
        const txt = await res.text();
        setMessages(m => m.concat([{ role: "error", content: `HTTP ${res.status}: ${txt.slice(0, 300)}`, ts: Date.now() }]));
      } else {
        const data = await res.json();
        setMessages(m => m.concat([{
          role: "assistant",
          content: data.answer || "(no answer)",
          trace: data.trace || [],
          meta: {
            model: data.model,
            cost_usd: data.cost_usd,
            latency_ms: data.latency_ms,
          },
          ts: Date.now(),
        }]));
      }
    } catch (e) {
      setMessages(m => m.concat([{ role: "error", content: String(e && e.message || e), ts: Date.now() }]));
    } finally {
      setBusy(false);
    }
  };

  const clear = () => {
    if (!messages.length) return;
    if (!confirm("Clear this Chief Analyst thread?")) return;
    setMessages([]);
  };

  const onKey = (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); }
    else if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    else if (e.key === "Escape" && mode === "overlay" && onClose) { e.preventDefault(); onClose(); }
  };

  return (
    <div className={`analyst analyst-${mode}`}>
      {mode === "overlay" && (
        <div className="analyst-overlay-head">
          <div className="analyst-overlay-title">Chief Analyst</div>
          <button className="analyst-close" onClick={onClose} aria-label="Close">×</button>
        </div>
      )}

      {mode === "page" && (
        <div className="analyst-page-head">
          <div>
            <div className="analyst-lede">
              Ask questions across trusted-KOL calls, per-source accuracy, themes, prices, and technicals.
              The analyst pulls its own data and shows the trace under each answer.
            </div>
          </div>
          <div className="analyst-head-actions">
            <button className="analyst-clear" onClick={clear} disabled={!messages.length}>clear</button>
          </div>
        </div>
      )}

      <div className="analyst-scroll" ref={scrollerRef}>
        {messages.length === 0 && (
          <div className="analyst-empty">
            <div className="analyst-empty-title">Try one of these</div>
            <div className="analyst-suggest">
              {SUGGESTED_QUESTIONS.map(q => (
                <button key={q} className="analyst-suggest-chip" onClick={() => send(q)}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => <AnalystMessage key={i} msg={m} />)}
        {busy && (
          <div className="analyst-msg analyst-msg-agent">
            <div className="analyst-thinking">
              <span className="analyst-dot"></span>
              <span className="analyst-dot"></span>
              <span className="analyst-dot"></span>
              <span className="analyst-thinking-label">
                {deep ? "reasoning (opus) — this can take 15–40s" : "pulling data + reasoning"}
              </span>
            </div>
          </div>
        )}
      </div>

      <div className="analyst-composer">
        <textarea
          ref={inputRef}
          className="analyst-input"
          rows={mode === "overlay" ? 2 : 3}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={onKey}
          placeholder="Ask the analyst…  (Enter to send · Shift+Enter for newline)"
          disabled={busy}
        />
        <div className="analyst-composer-row">
          <label className={`analyst-deep ${deep ? "on" : ""}`} title="Escalate this question to Opus (slower, deeper reasoning, ~5× cost)">
            <input type="checkbox" checked={deep} onChange={e => setDeep(e.target.checked)} />
            <span>deep · opus</span>
          </label>
          <button
            className="analyst-send"
            onClick={() => send()}
            disabled={busy || !input.trim()}
          >
            {busy ? "…" : "ask"}
          </button>
        </div>
      </div>
    </div>
  );
}

// Floating pill mounted in the header — opens the overlay.
// Also registers a ⌘K / Ctrl+K shortcut that opens it from anywhere.
function AnalystLauncher({ onNavPage }) {
  const [open, setOpen] = React.useState(false);

  React.useEffect(() => {
    function onKey(e) {
      const k = (e.key || "").toLowerCase();
      if ((e.metaKey || e.ctrlKey) && k === "k") {
        e.preventDefault();
        setOpen(o => !o);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <>
      <button
        className="analyst-pill"
        onClick={() => setOpen(true)}
        title="Chief Analyst (⌘K)"
      >
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
          <path d="M4 6h16M4 12h10M4 18h16"/>
          <circle cx="18" cy="12" r="2.3"/>
        </svg>
        <span>Ask analyst</span>
        <kbd className="analyst-pill-kbd">⌘K</kbd>
      </button>
      {open && (
        <div className="analyst-overlay-scrim" onClick={() => setOpen(false)}>
          <div className="analyst-overlay-panel" onClick={e => e.stopPropagation()}>
            <ChiefAnalyst mode="overlay" onClose={() => setOpen(false)} />
            {onNavPage && (
              <div className="analyst-overlay-foot">
                <button className="analyst-jump" onClick={() => { setOpen(false); onNavPage(); }}>
                  open full page →
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
