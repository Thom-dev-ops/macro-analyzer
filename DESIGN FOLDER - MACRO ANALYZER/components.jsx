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
  const labels = { 1: "TIER 1 · HIGH CONVICTION", 2: "TIER 2 · QUALITY", 3: "TIER 3 · PROBE", 4: "TIER 4 · AVOID" };
  return (
    <div className={`tier-ind tier-${tier}`}>
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
function SubScoreBar({ label, score, max, color = "amber" }) {
  const pct = (score / max) * 100;
  return (
    <div className="ssb">
      <div className="ssb-row">
        <span className="ssb-label">{label}</span>
        <span className="ssb-num mono">{score}<span className="muted">/{max}</span></span>
      </div>
      <div className="ssb-track">
        <div className={`ssb-fill ${color}`} style={{ width: `${pct}%` }}></div>
      </div>
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
  const sign = usd >= 0 ? "+" : "";
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

// ─── Setup card (hero) ───────────────────────────────────────────
function SetupCard({ s, onOpen, active = false }) {
  const distToInval = ((s.entry - s.stop) / s.entry) * 100;
  const upside = ((s.target - s.entry) / s.entry) * 100;
  return (
    <div className={`setup-card tier-${s.tier} ${active ? "active" : ""}`} onClick={() => onOpen(s)}>
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
      </div>

      <div className="sc-levels">
        <div className="sc-level">
          <div className="sc-l-lbl">ENTRY</div>
          <div className="sc-l-val mono">{s.entry < 1000 ? s.entry.toFixed(2) : s.entry.toLocaleString()}</div>
        </div>
        <div className="sc-level">
          <div className="sc-l-lbl">STOP</div>
          <div className="sc-l-val mono red">{s.stop < 1000 ? s.stop.toFixed(2) : s.stop.toLocaleString()}</div>
          <div className="sc-l-sub mono">−{distToInval.toFixed(1)}%</div>
        </div>
        <div className="sc-level">
          <div className="sc-l-lbl">TARGET</div>
          <div className="sc-l-val mono green">{s.target < 1000 ? s.target.toFixed(2) : s.target.toLocaleString()}</div>
          <div className="sc-l-sub mono">+{upside.toFixed(1)}%</div>
        </div>
        <div className="sc-level">
          <div className="sc-l-lbl">R/R</div>
          <div className="sc-l-val mono">{s.rr.toFixed(2)}</div>
        </div>
      </div>

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
  Sparkline, PnL, DrillSheet, SetupCard, SideLabel, ConvictionStripe, PriceChart,
});
