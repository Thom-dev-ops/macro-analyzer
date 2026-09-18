// Atomic components for the Macro Analyzer dashboard.
// Style-system uses CSS vars from index.html. All numbers monospaced.

const { useState, useMemo, useEffect, useRef } = React;

// ─── Score chip — 0..100 number + tier color + Δ arrow ───────────
function ScoreChip({ score, prev, size = "md" }) {
  const tier = score >= 85 ? 1 : score >= 70 ? 2 : score >= 55 ? 3 : 4;
  const tierClass = `tier-${tier}`;
  const d = prev != null ? score - prev : null;
  const sizes = {
    sm: { num: 14, lbl: 8, pad: "4px 7px" },
    md: { num: 22, lbl: 9, pad: "6px 10px" },
    lg: { num: 34, lbl: 9, pad: "10px 14px" },
  };
  const s = sizes[size];
  return (
    <div className={`score-chip ${tierClass}`} style={{ padding: s.pad }}>
      <div className="sc-num" style={{ fontSize: s.num }}>{score}</div>
      {d != null && (
        <div className={`sc-delta ${d > 0 ? "pos" : d < 0 ? "neg" : "flat"}`}
             style={{ fontSize: s.lbl }}>
          {d > 0 ? "▲" : d < 0 ? "▼" : "·"} {Math.abs(d)}
        </div>
      )}
    </div>
  );
}

// ─── Tier indicator — small bar + label ──────────────────────────
function TierIndicator({ tier }) {
  // Sizing bands, not belief levels — see MA_GLOSSARY.tier.
  const labels = { 1: "TIER 1 · FULL SIZE", 2: "TIER 2 · STANDARD", 3: "TIER 3 · PROBE", 4: "TIER 4 · AVOID" };
  return (
    <div className={`tier-ind tier-${tier}`}
         title={typeof MA_GLOSSARY !== "undefined" ? MA_GLOSSARY.tier.long : undefined}>
      <span className="tier-bar"></span>
      <span>{labels[tier]}</span>
    </div>
  );
}

// ─── Regime badge — pill with name + confidence dot ──────────────
function RegimeBadge({ kind, label, confidence }) {
  const pct = Math.round((confidence || 0) * 100);
  return (
    <div className={`regime-badge ${kind}`}>
      <span className="rb-kind">{kind === "framework" ? "FRAMEWORK" : "THESIS"}</span>
      <span className="rb-label">{label}</span>
      {confidence != null && (
        <span className="rb-conf">
          <span className="rb-dot" style={{ opacity: 0.4 + confidence * 0.6 }}></span>
          {pct}%
        </span>
      )}
    </div>
  );
}

// ─── Sub-score bar — labeled horizontal bar w/ fill ──────────────
function SubScoreBar({ label, score, max, color = "amber", note, flat, flatNote }) {
  const pct = (score / max) * 100;
  return (
    <div className={`ssb ${flat ? "ssb-is-flat" : ""}`}>
      <div className="ssb-row">
        <span className="ssb-label">{label}</span>
        {/* A component returning the same value for every asset is a
            constant offset — it moves the total but ranks nothing. */}
        {flat && <span className="ssb-flat mono" title={flatNote}>not ranking</span>}
        <span className="ssb-num mono">{score}<span className="muted">/{max}</span></span>
      </div>
      <div className="ssb-track">
        <div className={`ssb-fill ${color}`} style={{ width: `${pct}%` }}></div>
      </div>
      {/* What this component actually measures — so the bar doesn't
          need a lookup elsewhere to be readable. */}
      {note && <div className="ssb-note">{note}</div>}
      {flat && flatNote && <div className="ssb-note ssb-note-flat">{flatNote}</div>}
    </div>
  );
}

// ─── Source pill — name + weight + freshness dot ─────────────────
function SourcePill({ name, weight, freshness, contrib }) {
  const fclass = typeof freshness === "number"
    ? freshness > 0.85 ? "fresh" : freshness > 0.5 ? "stale" : "cold"
    : freshness === "fresh" ? "fresh" : freshness === "1d" ? "stale" : "cold";
  return (
    <div className="source-pill">
      <span className={`sp-dot ${fclass}`}></span>
      <span className="sp-name">{name}</span>
      <span className="sp-weight mono">{Number(weight).toFixed(2)}</span>
      {contrib != null && (
        <span className={`sp-contrib mono ${contrib > 0 ? "pos" : contrib < 0 ? "neg" : ""}`}>
          {contrib > 0 ? "+" : ""}{contrib}
        </span>
      )}
    </div>
  );
}

// ─── Sparkline — pure shape, no axes ─────────────────────────────
function Sparkline({ data, width = 120, height = 28, color = "var(--accent)", area = true, marker = false }) {
  if (!data || !data.length) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const stepX = width / (data.length - 1 || 1);
  const points = data.map((v, i) => [i * stepX, height - ((v - min) / range) * (height - 2) - 1]);
  const path = "M " + points.map(p => p.join(",")).join(" L ");
  const areaPath = area
    ? path + ` L ${width},${height} L 0,${height} Z`
    : "";
  const last = points[points.length - 1];
  return (
    <svg width={width} height={height} className="spark" preserveAspectRatio="none">
      {area && <path d={areaPath} fill={color} opacity="0.10" />}
      <path d={path} stroke={color} strokeWidth="1.25" fill="none" strokeLinecap="round" />
      {marker && last && <circle cx={last[0]} cy={last[1]} r="2" fill={color} />}
    </svg>
  );
}

// ─── P&L cell — number + percent, color-coded ────────────────────
function PnL({ usd, pct, size = "md" }) {
  const cls = usd > 0 ? "pos" : usd < 0 ? "neg" : "flat";
  // Sign the number, don't lean on colour alone: a loss rendered as "$1"
  // reads as a gain to anyone not distinguishing red from green.
  const sign = usd >= 0 ? "+" : "\u2212";
  const fmt = (n) => Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
  const sizes = {
    sm: { num: 12, sub: 10 },
    md: { num: 16, sub: 11 },
    lg: { num: 22, sub: 11 },
  };
  const s = sizes[size];
  return (
    <div className={`pnl ${cls}`}>
      <div className="pnl-num mono" style={{ fontSize: s.num }}>
        {sign}${fmt(usd)}
      </div>
      <div className="pnl-pct mono" style={{ fontSize: s.sub }}>
        {sign}{Math.abs(pct).toFixed(2)}%
      </div>
    </div>
  );
}

// ─── Drill-down sheet — slides from right ────────────────────────
function DrillSheet({ open, onClose, title, subtitle, children }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  return (
    <>
      <div className={`sheet-scrim ${open ? "open" : ""}`} onClick={onClose}></div>
      <aside className={`sheet ${open ? "open" : ""}`}>
        <header className="sheet-head">
          <div>
            <div className="sheet-title">{title}</div>
            {subtitle && <div className="sheet-sub">{subtitle}</div>}
          </div>
          <button className="sheet-close" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="sheet-body">{children}</div>
      </aside>
    </>
  );
}

// ─── Tier 1 conviction stripe (used on hero card top) ────────────
function ConvictionStripe({ tier }) {
  const colors = { 1: "var(--gold)", 2: "var(--green)", 3: "var(--amber)", 4: "var(--red)" };
  return <div className="conviction-stripe" style={{ background: colors[tier] }}></div>;
}

// ─── Side label (LONG / SHORT / WATCH / AVOID) ───────────────────
function SideLabel({ side }) {
  return <span className={`side-label side-${side.toLowerCase()}`}>{side}</span>;
}

// Price formatter for level rails. toLocaleString() alone emits up to 3
// fraction digits (2,251.458); sub-$1000 names need 2, and sub-$1 names
// (small-cap crypto) need real precision or they collapse to 0.00.
function fmtPrice(v) {
  const n = Number(v) || 0;
  const digits = Math.abs(n) >= 1000 ? 0 : Math.abs(n) >= 1 ? 2 : 4;
  return n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

// Why the technical agent declined to produce levels. It never fabricates
// them: no price history or too few bars for ATR means no rails at all.
const LEVELS_REASON = {
  no_price: "no levels — no price history for this ticker",
  no_atr: "no levels — too few bars to compute ATR",
};

// ─── Setup card (hero) ───────────────────────────────────────────
function SetupCard({ s, onOpen, active = false }) {
  const hasLevels = (s.entry || 0) > 0 && (s.stop || 0) > 0 && (s.target || 0) > 0;
  // Side-agnostic distances: on a SHORT the stop sits above entry and the
  // target below, so signed math would render backwards.
  const distToInval = hasLevels ? (Math.abs(s.entry - s.stop) / s.entry) * 100 : 0;
  const upside      = hasLevels ? (Math.abs(s.target - s.entry) / s.entry) * 100 : 0;
  // Provenance: v1 detectors are structural; mechanical rails are honest
  // placeholders and get flagged so a rail is never read as a real setup.
  const mechanical  = hasLevels && s.levelStructural === false;
  // Compact tape-vs-price divergence chip: fires when the composite score's
  // signal blend leans one way but 90d price trend is the opposite.
  const D = window.MA_DATA || {};
  const series = D.priceSeries?.[s.asset];
  const rt = (D.reasoning || {})[s.id]?.signalWindows?.blend;
  let divergence = null;
  if (series && series.length >= 10 && rt) {
    const first = series[Math.max(0, series.length - 90)];
    const last  = series[series.length - 1];
    if (first && last) {
      const ret90 = (last / first) - 1;
      const priceDir = ret90 > 0.15 ? "long" : ret90 < -0.15 ? "short" : "neutral";
      const opposed = (rt.direction === "long"  && priceDir === "short")
                   || (rt.direction === "short" && priceDir === "long");
      if (opposed && (rt.confidence || 0) >= 0.55) {
        divergence = { tape: rt.direction, price: priceDir, ret90 };
      }
    }
  }
  return (
    <div className={`setup-card tier-${s.tier} ${active ? "active" : ""} ${divergence ? "divergent" : ""}`}
         onClick={() => onOpen(s)}>
      <ConvictionStripe tier={s.tier} />
      <div className="sc-head">
        <div className="sc-asset-block">
          <div className="sc-asset mono">{s.asset}</div>
          <div className="sc-name">{s.name}</div>
        </div>
        <ScoreChip score={s.score} prev={s.scorePrev} size="md" />
      </div>
      <div className="sc-meta">
        <SideLabel side={s.side} />
        <span className="sc-setup">{s.setup}</span>
        {mechanical && (
          <span className="sc-mech-chip mono"
                title="No structural entry — 2×ATR stop / 3R target placeholder rails">
            mech
          </span>
        )}
        {divergence && (
          <span className="sc-div-chip mono"
                title={`Tape reads ${divergence.tape.toUpperCase()} but 90d price is ${(divergence.ret90 * 100).toFixed(1)}%`}>
            ⚠ tape≠price
          </span>
        )}
      </div>

      {hasLevels ? (
        <div className="sc-levels">
          <div className="sc-level">
            <div className="sc-l-lbl">ENTRY</div>
            <div className="sc-l-val mono">{fmtPrice(s.entry)}</div>
          </div>
          <div className="sc-level">
            <div className="sc-l-lbl">STOP</div>
            <div className="sc-l-val mono red">{fmtPrice(s.stop)}</div>
            <div className="sc-l-sub mono">−{distToInval.toFixed(1)}%</div>
          </div>
          <div className="sc-level">
            <div className="sc-l-lbl">TARGET</div>
            <div className="sc-l-val mono green">{fmtPrice(s.target)}</div>
            <div className="sc-l-sub mono">+{upside.toFixed(1)}%</div>
          </div>
          <div className="sc-level">
            <div className="sc-l-lbl">R/R</div>
            <div className="sc-l-val mono">{(s.rr || 0).toFixed(2)}</div>
          </div>
        </div>
      ) : (
        <div className="sc-levels-pending mono small muted">
          {LEVELS_REASON[s.levelsReason] || "no levels — awaiting next scoring pass"}
        </div>
      )}

      <ul className="sc-why">
        {s.whyNow.slice(0, 3).map((b, i) => <li key={i}>{b}</li>)}
      </ul>

      <footer className="sc-foot">
        <div className="sc-srcs">
          {s.sources.slice(0, 3).map((src, i) => (
            <span key={i} className="sc-src-chip">{src}</span>
          ))}
          {s.sources.length > 3 && <span className="sc-src-chip more">+{s.sources.length - 3}</span>}
        </div>
        <div className="sc-actions">
          <span className="sc-updated mono">{s.lastUpdate}</span>
          <button className="btn-mini">Reasoning →</button>
        </div>
      </footer>
    </div>
  );
}

// ─── Per-source detail panel (used in /streams + /dev DrillSheets) ──
//
// Renders the same card format as the TrustedAuthorCard on /inbox:
//   Name · group · parent     [trust ×]   [CATEGORY]
//   N analyzed · date range
//   [bias bar — bullish / neutral / bearish %]
//   CONVICTION PICKS  →  ticker chips (color-coded by bias)
//   TOP TICKERS · CLICK TO DRILL DOWN  →  ticker chips
//   RECURRING SETUPS  →  setup list
//
// For T0 trusted KOLs we hydrate `extras` from `/api/manual/themes/trusted`.
// For Substack / sell-side / RSS sources, the same skeleton degrades to
// "top themes" / "channels" / "tags" so every source looks consistent.
function SourceDetailPanel({ s }) {
  const [extras, setExtras] = React.useState(null);
  // tier is a string ("T0".."T4" | infra | self); channels are dicts
  // ({channel_type,label,url}) — detect KOL/manual sources accordingly.
  const _chanTypes = (s.channels || []).map(c => typeof c === "string" ? c : (c.channel_type || ""));
  const isManualKOL =
    s.tier === "T0" || s.tier === 0 ||
    ["social", "manual", "chart"].includes((s.kind || "").toLowerCase()) ||
    _chanTypes.some(c => /manual|telegram|chat/i.test(c));

  // Best-effort hydration for manual KOL sources from the existing API.
  // Match is fuzzy: registry ids/names drift from the seeded author slugs
  // (emoji, underscores, group↔member aliasing), so normalize both sides.
  React.useEffect(() => {
    if (!isManualKOL || !s.source_id) return;
    let cancelled = false;
    const norm = (x) => String(x || "").toLowerCase().replace(/[^a-z0-9]/g, "");
    const keys = new Set([norm(s.source_id), norm(s.name), norm(s.author)].filter(Boolean));
    fetch(`/api/manual/themes/trusted?window_days=90`)
      .then(r => r.ok ? r.json() : null)
      .then(j => {
        if (cancelled || !j) return;
        const match = (j.authors || []).find(a => {
          const aKeys = [norm(a.display_name), ...(String(a.author_id || "").split(":").map(norm))];
          return aKeys.some(k => k && keys.has(k));
        });
        if (match) setExtras(match);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [isManualKOL, s.source_id, s.name]);

  // Pull derived fields, falling back to whatever the source node carries.
  const D = window.MA_DATA || {};
  const themesForSource = (D.streams?.themeMap || [])
    .filter(t => (t.sources || []).includes(s.source_id || s.id))
    .sort((a, b) => (b.mentions_by_week?.[3] || 0) - (a.mentions_by_week?.[3] || 0));

  const trustX   = (extras?.trust_weight ?? s.weight ?? 0).toFixed ? (extras?.trust_weight ?? s.weight ?? 0).toFixed(1) : "—";
  const category = extras?.category || s.research_style || "";
  // Header context: short kind label ("Social", "Newsletter"), NOT the
  // full market_focus array (that rendered as a run-together blob).
  const group    = s.group || s.kind || "";
  const parent   = s.parent_channel || "";
  // market_focus is a list — join readably for the meta grid.
  const focusStr = Array.isArray(s.market_focus)
    ? s.market_focus.join(" · ")
    : (s.market_focus || "");

  // bias distribution: manual API gives bullish/neutral/bearish %; otherwise
  // fall back to single-direction source-level signal.
  const bias = extras?.bias_distribution || (
    s.direction ? { [s.direction]: 1 } : null
  );
  const totalBias = bias ? Object.values(bias).reduce((a, b) => a + b, 0) : 0;
  const pct = (k) => totalBias ? Math.round((bias[k] || 0) / totalBias * 100) : 0;

  // Conviction picks + top tickers
  const realPicks = (extras?.high_conviction_tickers || []).filter(hc => hc.ticker !== "UNKNOWN");
  const realTopTickers = (extras?.top_tickers || []).filter(([t]) => t !== "UNKNOWN");
  const topSetups = extras?.top_setups || [];

  // For non-KOL sources: synthesise "themes" chips from themeMap
  const synthChips = themesForSource.map(t => ({
    label: t.label, count: t.mentions_by_week?.[3] || 0, bias: t.direction
  }));

  const analyzedLine = extras
    ? `${extras.n_with_vision}/${extras.n_charts ?? extras.n_drops} charts analyzed · ${extras.n_drops} drops${extras.earliest_chart && extras.latest_chart ? ` · ${extras.earliest_chart} → ${extras.latest_chart}` : ""}`
    : (s.items7d != null ? `${s.items7d} items · last 7d` : null);

  return (
    <div className="ts-card source-detail-card">
      <div className="ts-card-head">
        <strong>{s.name || s.author}</strong>
        {group && <span className="dim"> · {group}</span>}
        {parent && <span className="dim"> · {parent}</span>}
        <span className="ts-trust mono">{trustX}x</span>
        {category && <span className="ts-cat">{category.replace(/_/g, " ")}</span>}
      </div>

      {analyzedLine && (
        <div className="ts-card-meta dim mono">{analyzedLine}</div>
      )}

      {totalBias > 0 && (
        <div className="ts-bias-bar"
          title={`bullish ${pct("bullish")}% · neutral ${pct("neutral")}% · bearish ${pct("bearish")}%`}>
          <div className="ts-bias-seg ts-bias-bull" style={{ width: `${pct("bullish")}%` }} />
          <div className="ts-bias-seg ts-bias-neut" style={{ width: `${pct("neutral")}%` }} />
          <div className="ts-bias-seg ts-bias-bear" style={{ width: `${pct("bearish")}%` }} />
        </div>
      )}

      {realPicks.length > 0 ? (
        <div className="ts-section">
          <div className="ts-section-title">Conviction picks</div>
          <div className="ts-chips">
            {realPicks.slice(0, 10).map(hc => (
              <span key={hc.ticker} className={`ts-chip ts-chip-${hc.bias}`}>
                <strong>{hc.ticker}</strong>
                <span className="dim mono"> ×{hc.mentions}</span>
              </span>
            ))}
          </div>
        </div>
      ) : synthChips.length > 0 && (
        <div className="ts-section">
          <div className="ts-section-title">Top themes</div>
          <div className="ts-chips">
            {synthChips.slice(0, 8).map(c => (
              <span key={c.label} className={`ts-chip ts-chip-${c.bias}`}>
                <strong>{c.label}</strong>
                <span className="dim mono"> ×{c.count}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {realTopTickers.length > 0 && (
        <div className="ts-section">
          <div className="ts-section-title">Top tickers · click to drill down</div>
          <div className="ts-chips">
            {realTopTickers.map(([t, c]) => (
              <span key={t} className="ts-chip ts-chip-neutral">
                <strong>{t}</strong><span className="dim mono"> ×{c}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {topSetups.length > 0 && (
        <div className="ts-section">
          <div className="ts-section-title">Recurring setups</div>
          <ul className="ts-setup-list">
            {topSetups.slice(0, 5).map(([setup, c]) => (
              <li key={setup}><span className="dim mono">×{c}</span> {setup}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Source-metadata footer always shown, so non-KOL sources still
          surface the channel / fetch / onboarded info. */}
      <div className="ts-section ts-meta-grid">
        <div className="ts-meta-cell">
          <div className="ts-meta-lbl mono">CHANNELS</div>
          <div className="ts-meta-val">
            {(s.channels || []).length
              ? s.channels.map(c => typeof c === "string" ? c : (c.channel_type || c.label || "?")).join(" · ")
              : "—"}
          </div>
        </div>
        <div className="ts-meta-cell">
          <div className="ts-meta-lbl mono">MARKET FOCUS</div>
          <div className="ts-meta-val">{focusStr || "—"}</div>
        </div>
        <div className="ts-meta-cell">
          <div className="ts-meta-lbl mono">FETCH</div>
          <div className="ts-meta-val">{s.fetch_cadence || "—"}</div>
        </div>
        <div className="ts-meta-cell">
          <div className="ts-meta-lbl mono">FRESHNESS SLA</div>
          <div className="ts-meta-val">{s.freshness_sla_hours ? `${s.freshness_sla_hours}h` : "—"}</div>
        </div>
        <div className="ts-meta-cell">
          <div className="ts-meta-lbl mono">ONBOARDED</div>
          <div className="ts-meta-val">{s.onboarded_at || "—"}</div>
        </div>
        <div className="ts-meta-cell">
          <div className="ts-meta-lbl mono">30D ATTRIBUTION</div>
          <div className={`ts-meta-val mono ${(s.attrib30d || 0) >= 0 ? "pos" : "neg"}`}>
            {(s.attrib30d || 0) >= 0 ? "+" : ""}${((s.attrib30d || 0) / 1000).toFixed(2)}k
          </div>
        </div>
        <div className="ts-meta-cell">
          <div className="ts-meta-lbl mono">FRESHNESS</div>
          <div className="ts-meta-val">{s.freshness || "—"}</div>
        </div>
      </div>

      {s.latestTitle && (
        <div className="ts-section">
          <div className="ts-section-title">Latest item</div>
          <div className="detail-title">{s.latestTitle}</div>
          {s.latestSnippet && <p className="detail-snippet muted small">{s.latestSnippet}</p>}
        </div>
      )}
    </div>
  );
}

// ─── Likert — 1..N button row, mobile-stackable ──────────────────
function Likert({ value, onChange, min = 1, max = 5, labels = null }) {
  const buttons = [];
  for (let i = min; i <= max; i++) {
    const on = value === i;
    buttons.push(
      <button
        key={i}
        type="button"
        className={`likert-btn ${on ? "on" : ""}`}
        onClick={() => onChange(i)}
        aria-pressed={on}
      >
        <span className="likert-num mono">{i}</span>
        {labels && labels[i - min] && (
          <span className="likert-lbl">{labels[i - min]}</span>
        )}
      </button>
    );
  }
  return <div className="likert-row">{buttons}</div>;
}

// ─── EnumPicker — radio chip row ─────────────────────────────────
function EnumPicker({ value, onChange, options }) {
  // options: array of {value,label} OR map {value: label}
  const entries = Array.isArray(options)
    ? options
    : Object.entries(options).map(([v, l]) => ({ value: v, label: l }));
  return (
    <div className="enum-picker">
      {entries.map(o => {
        const on = value === o.value;
        return (
          <button
            key={o.value}
            type="button"
            className={`enum-chip ${on ? "on" : ""}`}
            onClick={() => onChange(o.value)}
            aria-pressed={on}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// ─── MultiPicker — multi-select chips ────────────────────────────
function MultiPicker({ values, onChange, options }) {
  const entries = Array.isArray(options)
    ? options
    : Object.entries(options).map(([v, l]) => ({ value: v, label: l }));
  const set = new Set(values || []);
  const toggle = (v) => {
    const next = new Set(set);
    if (next.has(v)) next.delete(v); else next.add(v);
    onChange(Array.from(next));
  };
  return (
    <div className="enum-picker multi">
      {entries.map(o => {
        const on = set.has(o.value);
        return (
          <button
            key={o.value}
            type="button"
            className={`enum-chip ${on ? "on" : ""}`}
            onClick={() => toggle(o.value)}
            aria-pressed={on}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// ─── Price chart — line + entry/stop/target rails ──────────────
function PriceChart({ series, entry, stop, target, side = "LONG", height = 220 }) {
  if (!series || !series.length) return null;
  const W = 100, H = 100; // viewbox; svg scales to container
  const pad = { t: 6, r: 12, b: 14, l: 0 };
  const innerW = W - pad.l - pad.r;
  const innerH = H - pad.t - pad.b;

  const lo = Math.min(...series, stop, entry, target);
  const hi = Math.max(...series, stop, entry, target);
  const range = hi - lo || 1;
  const yOf = (v) => pad.t + innerH - ((v - lo) / range) * innerH;
  const xOf = (i) => pad.l + (i / (series.length - 1 || 1)) * innerW;

  const line = "M " + series.map((v, i) => `${xOf(i).toFixed(2)},${yOf(v).toFixed(2)}`).join(" L ");
  const area = line + ` L ${xOf(series.length - 1).toFixed(2)},${pad.t + innerH} L ${pad.l},${pad.t + innerH} Z`;

  const last = series[series.length - 1];
  const first = series[0];
  const chgPct = ((last - first) / first) * 100;
  const chgUp = chgPct >= 0;

  const Rail = ({ y, label, color, val, dashed }) => (
    <g>
      <line x1={pad.l} x2={W - pad.r} y1={y} y2={y}
            stroke={color} strokeWidth="0.4"
            strokeDasharray={dashed ? "1.2 1.2" : ""} opacity="0.85" />
      <rect x={W - pad.r + 0.5} y={y - 2.4} width={pad.r - 0.5} height="4.8" fill={color} opacity="0.18" />
      <text x={W - pad.r + 1.2} y={y + 1.5} fontSize="2.6" fill={color}
            fontFamily="var(--mono)" letterSpacing="0.04em">{label}</text>
    </g>
  );

  const fmt = (v) => v < 1000 ? v.toFixed(2) : v.toLocaleString();

  return (
    <div className="price-chart">
      <div className="pc-head">
        <div className="pc-last mono">{fmt(last)}</div>
        <div className={`pc-chg mono ${chgUp ? "pos" : "neg"}`}>
          {chgUp ? "+" : ""}{chgPct.toFixed(2)}% <span className="muted">90d</span>
        </div>
        <div className="pc-legend">
          <span className="pc-leg"><i className="pc-sw entry"></i>entry</span>
          <span className="pc-leg"><i className="pc-sw stop"></i>stop</span>
          <span className="pc-leg"><i className="pc-sw target"></i>target</span>
        </div>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
           width="100%" style={{ height, display: "block" }}>
        <defs>
          <linearGradient id="pc-area" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%"  stopColor="var(--accent)" stopOpacity="0.22" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0.00" />
          </linearGradient>
        </defs>
        {/* gridlines */}
        {[0.25, 0.5, 0.75].map(p => (
          <line key={p} x1={pad.l} x2={W - pad.r}
                y1={pad.t + innerH * p} y2={pad.t + innerH * p}
                stroke="var(--line-soft)" strokeWidth="0.25" />
        ))}
        <path d={area} fill="url(#pc-area)" />
        <path d={line} stroke="var(--accent)" strokeWidth="0.7" fill="none"
              strokeLinejoin="round" strokeLinecap="round" />
        <Rail y={yOf(target)} label={`TGT ${fmt(target)}`} color="var(--green)" dashed />
        <Rail y={yOf(entry)}  label={`ENT ${fmt(entry)}`}  color="var(--accent)" />
        <Rail y={yOf(stop)}   label={`STP ${fmt(stop)}`}   color="var(--red)"   dashed />
        {/* current price marker */}
        <circle cx={xOf(series.length - 1)} cy={yOf(last)} r="0.9" fill="var(--accent)" />
        <circle cx={xOf(series.length - 1)} cy={yOf(last)} r="1.8" fill="var(--accent)" opacity="0.25" />
      </svg>
      <div className="pc-axis mono">
        <span>−90d</span><span>−60d</span><span>−30d</span><span>now</span>
      </div>
    </div>
  );
}

// Export
Object.assign(window, {
  ScoreChip, TierIndicator, RegimeBadge, SubScoreBar, SourcePill,
  Sparkline, PnL, DrillSheet, SetupCard, SideLabel, ConvictionStripe,
  SourceDetailPanel, PriceChart,
  Likert, EnumPicker, MultiPicker,
});


// ─── Paper-book record badge ─────────────────────────────────────
// What the paper book has actually made on this ticker's sleeve —
// win rate, average win, average loss, sample size — read-only, from
// /api/paper/learning/sleeve/{ticker}. Sits on concept cards and the
// asset page so a human promoting a concept sees the machine's record
// on that class. It does NOT move the desk's score: that promotion is
// gated on the loop earning it (see DECISIONS 2026-09-18).
const _paperRecordCache = {};
function PaperRecordBadge({ ticker, compact = false }) {
  const [rec, setRec] = React.useState(_paperRecordCache[ticker] || null);
  React.useEffect(() => {
    if (!ticker || _paperRecordCache[ticker]) return;
    let live = true;
    fetch(`/api/paper/learning/sleeve/${encodeURIComponent(ticker)}`)
      .then(r => (r.ok ? r.json() : null))
      .then(j => { if (live && j) { _paperRecordCache[ticker] = j; setRec(j); } })
      .catch(() => {});
    return () => { live = false; };
  }, [ticker]);
  if (!rec || !rec.record || !rec.record.n) return null;
  const r = rec.record;
  const tone = (r.avgR ?? 0) > 0 ? "pos" : (r.avgR ?? 0) < 0 ? "neg" : "muted";
  const title = `Paper book on ${rec.sleeveLabel}: ${r.n} closed · ${r.winRate}% win · ` +
    `avg win ${r.avgWinPct != null ? r.avgWinPct + "%" : "—"} · avg loss ${r.avgLossPct != null ? r.avgLossPct + "%" : "—"} · ` +
    `${(r.avgR ?? 0).toFixed(2)}R avg. Shrunk to w=${r.w} (n/(n+30)); moves paper rank by ${r.rankDelta > 0 ? "+" : ""}${r.rankDelta}. Read-only here.`;
  return (
    <span className={`paper-record-badge mono ${tone}`} title={title}>
      📓 {rec.sleeveLabel.split(" ")[0]} {r.winRate}% · {(r.avgR ?? 0) >= 0 ? "+" : ""}{(r.avgR ?? 0).toFixed(1)}R
      {!compact && <span className="muted"> · n={r.n}</span>}
    </span>
  );
}
