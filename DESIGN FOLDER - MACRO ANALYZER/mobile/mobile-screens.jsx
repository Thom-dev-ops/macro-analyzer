// Mobile screens — 9 phone-sized layouts living inside iOS bezels.
// Reuses MA_DATA from data.js. Self-contained; no shared style obj.

const { useState: useStateMS } = React;

// ─── Shared chrome bits ────────────────────────────────────────
const ms = {
  // colors mirror the desk styles.css palette
  bg: "#0d0e0c",
  bg1: "#15171514",
  card: "#1b1d1a",
  card2: "#20221f",
  card3: "#272924",
  inset: "#11120f",
  line: "#2c2f29",
  line2: "#3a3d35",
  lineSoft: "#20231e",
  text: "#ece6d4",
  mute: "#b6b09c",
  mute2: "#807a68",
  mute3: "#5a5648",
  gold: "#d6b15a",
  green: "#6fb37a",
  red: "#d97758",
  amber: "#d6a04a",
  blue: "#6c92c4",
  mono: '"JetBrains Mono", "IBM Plex Mono", ui-monospace, Menlo, monospace',
  serif: '"Cormorant Garamond", "EB Garamond", Georgia, serif',
  sans: '"Inter Tight", "Söhne", -apple-system, system-ui, sans-serif',
};

// dark-mode iOS status bar uses white glyphs; our screen is near-black so good
const PHONE_W = 392;
const PHONE_H = 850;
const SCREEN_PAD_TOP = 56; // below dynamic island
const SCREEN_PAD_BOT = 28; // above home indicator

function PhoneShell({ children, title }) {
  return (
    <IOSDevice width={PHONE_W} height={PHONE_H} dark>
      <div style={{
        position: "absolute", inset: 0,
        background: ms.bg,
        backgroundImage:
          "radial-gradient(800px 400px at 20% -10%, rgba(214,177,90,0.06), transparent 60%)," +
          "radial-gradient(700px 360px at 90% 110%, rgba(108,146,196,0.05), transparent 60%)",
        color: ms.text,
        fontFamily: ms.sans, fontSize: 13, lineHeight: 1.45,
        overflow: "hidden",
        display: "flex", flexDirection: "column",
      }}>
        <div style={{ height: SCREEN_PAD_TOP }}></div>
        {title && <PhoneAppBar title={title} />}
        <div style={{ flex: 1, overflow: "hidden", padding: "0 14px" }}>
          {children}
        </div>
        <div style={{ height: SCREEN_PAD_BOT }}></div>
      </div>
    </IOSDevice>
  );
}

function PhoneAppBar({ title, sub, right }) {
  return (
    <div style={{
      display: "flex", alignItems: "baseline", justifyContent: "space-between",
      padding: "6px 16px 10px",
      borderBottom: `1px solid ${ms.lineSoft}`,
    }}>
      <div>
        <div style={{ fontFamily: ms.serif, fontSize: 22, lineHeight: 1.1, color: ms.text }}>{title}</div>
        {sub && <div style={{ fontFamily: ms.mono, fontSize: 9, letterSpacing: "0.16em", color: ms.mute3, textTransform: "uppercase", marginTop: 2 }}>{sub}</div>}
      </div>
      {right}
    </div>
  );
}

function PhTabs({ tabs, active }) {
  return (
    <div style={{
      display: "flex", gap: 4,
      padding: "8px 14px 0",
      fontFamily: ms.mono, fontSize: 10, letterSpacing: "0.14em",
      borderBottom: `1px solid ${ms.lineSoft}`,
    }}>
      {tabs.map(t => (
        <span key={t} style={{
          padding: "8px 10px",
          color: t === active ? ms.text : ms.mute2,
          borderBottom: `1px solid ${t === active ? ms.gold : "transparent"}`,
          textTransform: "uppercase",
        }}>{t}</span>
      ))}
    </div>
  );
}

function PhSectionHead({ num, title, sub, right }) {
  return (
    <div style={{
      display: "flex", alignItems: "baseline", justifyContent: "space-between",
      padding: "12px 2px 6px",
    }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        {num && <span style={{ fontFamily: ms.mono, fontSize: 9, color: ms.gold, border: `1px solid ${ms.line2}`, padding: "1px 5px", letterSpacing: "0.18em" }}>{num}</span>}
        <span style={{ fontFamily: ms.serif, fontSize: 14, color: ms.text }}>{title}</span>
      </div>
      {(sub || right) && (
        <span style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute2, letterSpacing: "0.12em", textTransform: "uppercase" }}>
          {right || sub}
        </span>
      )}
    </div>
  );
}

function PhBottomNav({ active = "Positioning" }) {
  const items = [
    { k: "Positioning", g: "◧" },
    { k: "Journal",     g: "◫" },
    { k: "Alerts",      g: "◉" },
    { k: "Dev",         g: "◐" },
  ];
  return (
    <div style={{
      position: "absolute", left: 14, right: 14, bottom: 36,
      display: "grid", gridTemplateColumns: `repeat(${items.length}, 1fr)`,
      padding: "8px 4px",
      background: "rgba(20,21,18,0.86)",
      backdropFilter: "blur(14px)",
      border: `1px solid ${ms.line}`,
      borderRadius: 14,
      fontFamily: ms.mono, fontSize: 9, letterSpacing: "0.14em",
      textTransform: "uppercase",
    }}>
      {items.map(i => (
        <div key={i.k} style={{
          display: "flex", flexDirection: "column", alignItems: "center", gap: 2,
          color: i.k === active ? ms.gold : ms.mute2,
        }}>
          <span style={{ fontSize: 13 }}>{i.g}</span>
          <span>{i.k}</span>
        </div>
      ))}
    </div>
  );
}

function fmtPx(v) {
  if (v == null) return "—";
  return v < 1000 ? v.toFixed(2) : v.toLocaleString();
}
function tierColor(t) { return t === 1 ? ms.gold : t === 2 ? ms.green : t === 3 ? ms.amber : ms.red; }
function sideColor(s) { return s === "LONG" ? ms.green : s === "SHORT" ? ms.red : ms.mute2; }

// ─── Mini sparkline / mini price chart ─────────────────────────
function MiniLine({ data, color = ms.gold, w = 60, h = 18, area = true }) {
  if (!data || !data.length) return null;
  const lo = Math.min(...data), hi = Math.max(...data);
  const r = hi - lo || 1;
  const x = (i) => (i / (data.length - 1 || 1)) * w;
  const y = (v) => h - ((v - lo) / r) * (h - 2) - 1;
  const line = "M " + data.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" L ");
  const a = area ? line + ` L ${w},${h} L 0,${h} Z` : "";
  return (
    <svg width={w} height={h} style={{ display: "block" }}>
      {area && <path d={a} fill={color} opacity="0.14" />}
      <path d={line} stroke={color} strokeWidth="1.2" fill="none" strokeLinecap="round" />
    </svg>
  );
}

// price chart with rails for asset-detail screen
function PhPriceChart({ series, entry, stop, target, h = 110 }) {
  if (!series || !series.length) return null;
  const W = 100, H = 100;
  const pad = { t: 6, r: 16, b: 6, l: 0 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const lo = Math.min(...series, stop, entry, target);
  const hi = Math.max(...series, stop, entry, target);
  const rng = hi - lo || 1;
  const xOf = (i) => pad.l + (i / (series.length - 1 || 1)) * iw;
  const yOf = (v) => pad.t + ih - ((v - lo) / rng) * ih;
  const line = "M " + series.map((v, i) => `${xOf(i).toFixed(2)},${yOf(v).toFixed(2)}`).join(" L ");
  const area = line + ` L ${xOf(series.length - 1).toFixed(2)},${pad.t + ih} L 0,${pad.t + ih} Z`;
  const last = series[series.length - 1];

  const Rail = ({ v, c, label, dashed }) => (
    <g>
      <line x1={pad.l} x2={W - pad.r} y1={yOf(v)} y2={yOf(v)} stroke={c} strokeWidth="0.4" strokeDasharray={dashed ? "1.4 1.4" : ""} opacity="0.85" />
      <text x={W - pad.r + 1.2} y={yOf(v) + 1.2} fontSize="2.4" fill={c} fontFamily={ms.mono} letterSpacing="0.04em">{label}</text>
    </g>
  );

  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" width="100%" style={{ height: h, display: "block" }}>
      <defs>
        <linearGradient id="ph-area" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor={ms.gold} stopOpacity="0.22" />
          <stop offset="100%" stopColor={ms.gold} stopOpacity="0" />
        </linearGradient>
      </defs>
      {[0.33, 0.66].map(p => (
        <line key={p} x1="0" x2={W - pad.r} y1={pad.t + ih * p} y2={pad.t + ih * p} stroke={ms.lineSoft} strokeWidth="0.25" />
      ))}
      <path d={area} fill="url(#ph-area)" />
      <path d={line} stroke={ms.gold} strokeWidth="0.8" fill="none" strokeLinejoin="round" />
      <Rail v={target} c={ms.green} label="TGT" dashed />
      <Rail v={entry}  c={ms.gold}  label="ENT" />
      <Rail v={stop}   c={ms.red}   label="STP" dashed />
      <circle cx={xOf(series.length - 1)} cy={yOf(last)} r="0.9" fill={ms.gold} />
      <circle cx={xOf(series.length - 1)} cy={yOf(last)} r="1.8" fill={ms.gold} opacity="0.25" />
    </svg>
  );
}

// ─── 1. POSITIONING HOME (refined) ─────────────────────────────
function MS_PositioningHome() {
  const D = window.MA_DATA;
  const f = D.regime.framework;
  const top = D.heroSignals.slice(0, 4);
  return (
    <PhoneShell>
      <PhoneAppBar
        title="Positioning"
        sub={`FRI · MAY 22 · 14:23 ET`}
        right={<span style={{ fontFamily: ms.mono, fontSize: 9, color: ms.gold, letterSpacing: "0.14em" }}>● LIVE</span>}
      />
      <PhTabs tabs={["Positioning","Journal","Dev"]} active="Positioning" />

      <div style={{ overflowY: "hidden", paddingBottom: 60 }}>
        {/* regime block */}
        <div style={{ padding: 12, marginTop: 10, border: `1px solid ${ms.line}`, background: ms.card, position: "relative" }}>
          <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: 3, background: ms.gold }}></div>
          <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
            <div>
              <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.18em" }}>FRAMEWORK REGIME</div>
              <div style={{ fontFamily: ms.serif, fontSize: 18, lineHeight: 1.1, color: ms.text, marginTop: 2 }}>{f.label}</div>
            </div>
            <div style={{ textAlign: "right" }}>
              <div style={{ fontFamily: ms.mono, fontSize: 22, color: ms.gold, lineHeight: 1 }}>{Math.round(f.confidence * 100)}<span style={{ fontSize: 11, color: ms.mute2 }}>%</span></div>
              <div style={{ fontFamily: ms.mono, fontSize: 8, color: ms.mute3, letterSpacing: "0.14em" }}>CONFIDENCE</div>
            </div>
          </div>
          <div style={{ marginTop: 8 }}>
            <MiniLine data={D.regime.confidenceTrace.slice(-60)} color={ms.gold} w={350} h={26} />
          </div>
          <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute2, letterSpacing: "0.06em", marginTop: 6 }}>
            active {f.sinceDays}d · ×{f.sizingModifier.toFixed(2)} size · {f.scoreModifier > 0 ? "+" : ""}{f.scoreModifier} score
          </div>
        </div>

        <PhSectionHead num="01" title="Hero signals" right={`≥75 · ${top.filter(s => s.score >= 75).length}/${top.length}`} />
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {top.map(s => (
            <div key={s.id} style={{
              display: "grid", gridTemplateColumns: "44px 1fr auto",
              alignItems: "center", gap: 10,
              padding: "10px 10px 10px 12px",
              border: `1px solid ${ms.line}`, background: ms.card,
              position: "relative",
            }}>
              <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: 3, background: tierColor(s.tier) }}></div>
              <div style={{
                fontFamily: ms.mono, fontSize: 22, lineHeight: 1, color: tierColor(s.tier),
                fontVariantNumeric: "tabular-nums",
              }}>{s.score}</div>
              <div style={{ minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                  <span style={{ fontFamily: ms.mono, fontSize: 13, letterSpacing: "0.04em" }}>{s.asset}</span>
                  <span style={{ fontFamily: ms.mono, fontSize: 9, color: sideColor(s.side), letterSpacing: "0.16em" }}>{s.side}</span>
                </div>
                <div style={{ fontFamily: ms.serif, fontSize: 11, color: ms.mute, fontStyle: "italic", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{s.setup}</div>
              </div>
              <div style={{ textAlign: "right" }}>
                <div style={{ fontFamily: ms.mono, fontSize: 11 }}>{fmtPx(s.entry)}</div>
                <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute2 }}>R/R {s.rr.toFixed(2)}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <PhBottomNav active="Positioning" />
    </PhoneShell>
  );
}

// ─── 2. ASSET DETAIL ───────────────────────────────────────────
function MS_AssetDetail() {
  const D = window.MA_DATA;
  const s = D.heroSignals[0]; // URA
  const series = D.priceSeries[s.asset];
  const distStop = ((s.entry - s.stop) / s.entry) * 100;
  const upside   = ((s.target - s.entry) / s.entry) * 100;
  return (
    <PhoneShell>
      {/* compact back / id row */}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "6px 16px 4px",
      }}>
        <span style={{ fontFamily: ms.mono, fontSize: 10, color: ms.mute, letterSpacing: "0.14em" }}>← /positioning</span>
        <span style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.16em" }}>{s.id}</span>
      </div>

      {/* hero block */}
      <div style={{ padding: "8px 16px 10px", borderBottom: `1px solid ${ms.lineSoft}`, position: "relative" }}>
        <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: 3, background: tierColor(1) }}></div>
        <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 10 }}>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontFamily: ms.mono, fontSize: 24, letterSpacing: "0.04em", lineHeight: 1 }}>{s.asset}</div>
            <div style={{ fontFamily: ms.serif, fontSize: 14, color: ms.mute, fontStyle: "italic", lineHeight: 1.2, marginTop: 2 }}>{s.name}</div>
          </div>
          <div style={{ textAlign: "right" }}>
            <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.16em" }}>SCORE</div>
            <div style={{ fontFamily: ms.mono, fontSize: 36, lineHeight: 1, color: tierColor(1) }}>{s.score}</div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
          <span style={{ fontFamily: ms.mono, fontSize: 10, padding: "3px 8px", border: `1px solid ${ms.gold}`, color: ms.gold, letterSpacing: "0.16em" }}>● TIER 1</span>
          <span style={{ fontFamily: ms.mono, fontSize: 10, padding: "3px 8px", border: `1px solid ${ms.green}`, color: ms.green, letterSpacing: "0.18em", background: "rgba(111,179,122,0.06)" }}>{s.side}</span>
          <span style={{ fontFamily: ms.mono, fontSize: 10, padding: "3px 8px", border: `1px solid ${ms.line2}`, color: ms.mute, letterSpacing: "0.1em" }}>R/R {s.rr.toFixed(2)}</span>
        </div>
      </div>

      {/* chart */}
      <div style={{ padding: "10px 4px 0" }}>
        <PhPriceChart series={series} entry={s.entry} stop={s.stop} target={s.target} h={120} />
      </div>

      {/* levels strip */}
      <div style={{
        display: "grid", gridTemplateColumns: "1fr 1fr 1fr",
        marginTop: 10,
        border: `1px solid ${ms.lineSoft}`, background: ms.inset,
      }}>
        {[
          { l: "ENTRY",  v: fmtPx(s.entry), c: ms.text, sub: "trigger" },
          { l: "STOP",   v: fmtPx(s.stop),  c: ms.red,  sub: `−${distStop.toFixed(1)}%` },
          { l: "TARGET", v: fmtPx(s.target),c: ms.green,sub: `+${upside.toFixed(1)}%` },
        ].map((c, i, arr) => (
          <div key={c.l} style={{ padding: "8px 10px", borderRight: i === arr.length - 1 ? "none" : `1px solid ${ms.lineSoft}` }}>
            <div style={{ fontFamily: ms.mono, fontSize: 8, color: ms.mute3, letterSpacing: "0.16em" }}>{c.l}</div>
            <div style={{ fontFamily: ms.mono, fontSize: 14, color: c.c }}>{c.v}</div>
            <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute2 }}>{c.sub}</div>
          </div>
        ))}
      </div>

      {/* Active position summary */}
      <PhSectionHead num="02" title="Open trade" right="t-2026-019" />
      <div style={{ padding: 10, border: `1px solid ${ms.line}`, background: ms.card }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 0, background: ms.inset, border: `1px solid ${ms.lineSoft}` }}>
          {[
            { l: "SIZE",  v: "$32,000", sub: "5.2% port", c: ms.text },
            { l: "P&L",   v: "+$2,054", sub: "+6.42%",    c: ms.green },
            { l: "AGE",   v: "11d",     sub: "@ score 74", c: ms.text },
          ].map((c, i, arr) => (
            <div key={c.l} style={{ padding: "8px 10px", borderRight: i === arr.length - 1 ? "none" : `1px solid ${ms.lineSoft}` }}>
              <div style={{ fontFamily: ms.mono, fontSize: 8, color: ms.mute3, letterSpacing: "0.14em" }}>{c.l}</div>
              <div style={{ fontFamily: ms.mono, fontSize: 13, color: c.c }}>{c.v}</div>
              <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute2 }}>{c.sub}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Bottom action bar */}
      <div style={{
        position: "absolute", left: 14, right: 14, bottom: 38,
        display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8,
      }}>
        <button style={{ background: ms.gold, color: ms.bg, border: 0, padding: "10px 0", fontFamily: ms.mono, fontSize: 11, letterSpacing: "0.16em" }}>LOG TRADE</button>
        <button style={{ background: "transparent", color: ms.text, border: `1px solid ${ms.line2}`, padding: "10px 0", fontFamily: ms.mono, fontSize: 11, letterSpacing: "0.16em" }}>REASONING ›</button>
      </div>
    </PhoneShell>
  );
}

// ─── 3. REASONING TRAIL ────────────────────────────────────────
function MS_Reasoning() {
  const D = window.MA_DATA;
  const s = D.heroSignals[0];
  const r = D.reasoning[s.id];
  return (
    <PhoneShell>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "6px 16px 4px",
      }}>
        <span style={{ fontFamily: ms.mono, fontSize: 10, color: ms.mute, letterSpacing: "0.14em" }}>← {s.asset}</span>
        <span style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.16em" }}>REASONING TRAIL</span>
      </div>

      <PhoneAppBar title="Why · why now" sub={`${s.asset} · score ${r.total}/100`} />

      {/* composite */}
      <PhSectionHead num="A" title="Composite" right={`${r.total} / 100`} />
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {r.components.map(c => {
          const pct = (c.score / c.max) * 100;
          const col = c.color === "green" ? ms.green : c.color === "amber" ? ms.amber : ms.red;
          return (
            <div key={c.label}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginBottom: 3 }}>
                <span style={{ color: ms.mute }}>{c.label}</span>
                <span style={{ fontFamily: ms.mono, color: ms.text }}>{c.score}<span style={{ color: ms.mute3 }}>/{c.max}</span></span>
              </div>
              <div style={{ height: 4, background: ms.inset, border: `1px solid ${ms.lineSoft}` }}>
                <div style={{ width: `${pct}%`, height: "100%", background: col, opacity: 0.85 }}></div>
              </div>
            </div>
          );
        })}
      </div>

      {/* modifiers */}
      <PhSectionHead title="Modifiers" />
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {r.modifiers.map(m => (
          <div key={m.label} style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: ms.mute }}>
            <span style={{ fontFamily: ms.mono, fontSize: 10 }}>{m.label}</span>
            <span style={{ fontFamily: ms.mono, color: m.value.startsWith("+") ? ms.green : m.value === "0" ? ms.mute3 : ms.red }}>{m.value}</span>
          </div>
        ))}
      </div>

      {/* sources */}
      <PhSectionHead num="C" title="Sources" right={`${r.sources.length} weighted`} />
      <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
        {r.sources.map(src => {
          const fc = src.freshness === "fresh" ? ms.green : src.freshness === "1d" ? ms.amber : ms.mute3;
          return (
            <div key={src.name} style={{
              display: "inline-flex", alignItems: "center", gap: 6,
              padding: "4px 8px",
              border: `1px solid ${ms.line}`, background: ms.card,
              fontSize: 10,
            }}>
              <span style={{ width: 6, height: 6, borderRadius: 3, background: fc }}></span>
              <span style={{ color: ms.text }}>{src.name}</span>
              <span style={{ fontFamily: ms.mono, color: ms.mute2 }}>{src.weight.toFixed(2)}</span>
              <span style={{ fontFamily: ms.mono, color: src.contrib > 0 ? ms.green : ms.red }}>{src.contrib > 0 ? "+" : ""}{src.contrib}</span>
            </div>
          );
        })}
      </div>

      {/* theses */}
      <PhSectionHead num="D" title="Theses" />
      <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
        {r.theses.map(t => (
          <div key={t.theme} style={{ display: "grid", gridTemplateColumns: "10px 1fr auto auto", gap: 8, alignItems: "center", padding: "6px 8px", border: `1px solid ${ms.lineSoft}`, background: ms.card }}>
            <span style={{ width: 6, height: 6, borderRadius: 3, background: t.direction === "bullish" ? ms.green : t.direction === "bearish" ? ms.red : ms.amber }}></span>
            <span style={{ fontFamily: ms.mono, fontSize: 11 }}>{t.theme}</span>
            <span style={{ fontSize: 10, color: ms.mute2, textTransform: "uppercase", letterSpacing: "0.1em" }}>{t.direction}</span>
            <span style={{ fontFamily: ms.mono, fontSize: 11, color: ms.text }}>{Math.round(t.confidence * 100)}%</span>
          </div>
        ))}
      </div>
    </PhoneShell>
  );
}

// ─── 4. ACTIVE TRADE DETAIL ────────────────────────────────────
function MS_ActiveTrade() {
  const D = window.MA_DATA;
  const t = D.activeTrades[0]; // URA
  const port = 612400;
  const pctOfPort = (t.sizeUsd / port) * 100;
  const distStop = t.side === "LONG" ? ((t.entry - t.stop) / t.entry) * 100 : ((t.stop - t.entry) / t.entry) * 100;
  const distTgt  = t.side === "LONG" ? ((t.target - t.entry) / t.entry) * 100 : ((t.entry - t.target) / t.entry) * 100;
  const live = t.entry * (1 + (t.side === "LONG" ? t.pnlPct : -t.pnlPct) / 100);
  const lo = Math.min(t.stop, t.target), hi = Math.max(t.stop, t.target);
  const progress = Math.max(0, Math.min(1, (live - lo) / (hi - lo)));

  return (
    <PhoneShell>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "6px 16px 4px",
      }}>
        <span style={{ fontFamily: ms.mono, fontSize: 10, color: ms.mute, letterSpacing: "0.14em" }}>← Active</span>
        <span style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.16em" }}>{t.id}</span>
      </div>

      <PhoneAppBar
        title={`${t.asset} · ${t.side}`}
        sub={`opened ${t.ageDays}d ago · score ${t.scoreAtOpen}→${t.scoreNow}`}
        right={<span style={{ fontFamily: ms.mono, fontSize: 9, color: ms.green, letterSpacing: "0.14em" }}>● RUNNING</span>}
      />

      {/* big P&L */}
      <div style={{ textAlign: "center", padding: "16px 0 4px" }}>
        <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.18em" }}>UNREALIZED P&L</div>
        <div style={{ fontFamily: ms.mono, fontSize: 44, lineHeight: 1, color: ms.green, marginTop: 4 }}>
          +${t.pnlUsd.toLocaleString()}
        </div>
        <div style={{ fontFamily: ms.mono, fontSize: 14, color: ms.green, marginTop: 4 }}>
          +{t.pnlPct.toFixed(2)}%
        </div>
      </div>

      {/* progress to target */}
      <div style={{ padding: "12px 4px 4px" }}>
        <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.16em", marginBottom: 6 }}>STOP ─── ENTRY ─── TARGET</div>
        <div style={{ position: "relative", height: 10, background: ms.inset, border: `1px solid ${ms.lineSoft}` }}>
          <div style={{ position: "absolute", inset: 0, background: `linear-gradient(90deg, ${ms.red} 0%, ${ms.gold} 50%, ${ms.green} 100%)`, opacity: 0.5 }}></div>
          <div style={{ position: "absolute", top: -3, bottom: -3, width: 2, left: `${progress * 100}%`, background: ms.text, transform: "translateX(-1px)" }}></div>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", fontFamily: ms.mono, fontSize: 9, color: ms.mute2, marginTop: 6, letterSpacing: "0.06em" }}>
          <span style={{ color: ms.red }}>stop {fmtPx(t.stop)}</span>
          <span>entry {fmtPx(t.entry)}</span>
          <span style={{ color: ms.green }}>target {fmtPx(t.target)}</span>
        </div>
      </div>

      {/* metric grid */}
      <div style={{
        display: "grid", gridTemplateColumns: "1fr 1fr",
        marginTop: 12, border: `1px solid ${ms.lineSoft}`, background: ms.inset,
      }}>
        {[
          { l: "SIZE",      v: `$${t.sizeUsd.toLocaleString()}`, sub: `${pctOfPort.toFixed(2)}% of port` },
          { l: "RISK $",    v: `$${Math.round(t.sizeUsd * distStop / 100).toLocaleString()}`, sub: `−${distStop.toFixed(1)}% to stop`, c: ms.red },
          { l: "UPSIDE $",  v: `$${Math.round(t.sizeUsd * distTgt / 100).toLocaleString()}`,  sub: `+${distTgt.toFixed(1)}% to target`, c: ms.green },
          { l: "REGIME",    v: "Commodity-Led",  sub: "intact since open" },
        ].map((c, i) => (
          <div key={c.l} style={{
            padding: "10px 12px",
            borderRight: i % 2 === 0 ? `1px solid ${ms.lineSoft}` : "none",
            borderTop: i > 1 ? `1px solid ${ms.lineSoft}` : "none",
          }}>
            <div style={{ fontFamily: ms.mono, fontSize: 8, color: ms.mute3, letterSpacing: "0.16em" }}>{c.l}</div>
            <div style={{ fontFamily: ms.mono, fontSize: 14, color: c.c || ms.text }}>{c.v}</div>
            <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute2, marginTop: 2 }}>{c.sub}</div>
          </div>
        ))}
      </div>

      {/* trim / close */}
      <div style={{
        position: "absolute", left: 14, right: 14, bottom: 38,
        display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 6,
      }}>
        <button style={{ background: "transparent", color: ms.text, border: `1px solid ${ms.line2}`, padding: "10px 0", fontFamily: ms.mono, fontSize: 11, letterSpacing: "0.14em" }}>TRIM ½</button>
        <button style={{ background: "transparent", color: ms.text, border: `1px solid ${ms.line2}`, padding: "10px 0", fontFamily: ms.mono, fontSize: 11, letterSpacing: "0.14em" }}>MOVE STOP</button>
        <button style={{ background: ms.red, color: ms.bg, border: 0, padding: "10px 0", fontFamily: ms.mono, fontSize: 11, letterSpacing: "0.16em" }}>CLOSE</button>
      </div>
    </PhoneShell>
  );
}

// ─── 5. TRADE LOG (quick entry) ────────────────────────────────
function MS_TradeLog() {
  return (
    <PhoneShell>
      <div style={{ display: "flex", justifyContent: "space-between", padding: "6px 16px 4px" }}>
        <span style={{ fontFamily: ms.mono, fontSize: 10, color: ms.mute, letterSpacing: "0.14em" }}>← Cancel</span>
        <span style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.16em" }}>NEW TRADE</span>
      </div>

      <PhoneAppBar title="Log entry" sub="One-tap fast" />

      {[
        { lbl: "ASSET", val: "URA", mono: true },
        { lbl: "SETUP", val: "Long Base Accumulation · Breakout retest" },
      ].map(f => (
        <div key={f.lbl} style={{ borderBottom: `1px solid ${ms.lineSoft}`, padding: "10px 4px" }}>
          <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.16em" }}>{f.lbl}</div>
          <div style={{ fontFamily: f.mono ? ms.mono : ms.serif, fontSize: f.mono ? 22 : 14, color: ms.text, marginTop: 4, fontStyle: f.mono ? "normal" : "italic" }}>{f.val}</div>
        </div>
      ))}

      {/* SIDE segmented */}
      <div style={{ padding: "10px 4px" }}>
        <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.16em", marginBottom: 6 }}>SIDE</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", border: `1px solid ${ms.line2}` }}>
          <div style={{ padding: "8px 0", textAlign: "center", fontFamily: ms.mono, fontSize: 11, letterSpacing: "0.18em", background: ms.green, color: ms.bg }}>LONG</div>
          <div style={{ padding: "8px 0", textAlign: "center", fontFamily: ms.mono, fontSize: 11, letterSpacing: "0.18em", color: ms.mute }}>SHORT</div>
        </div>
      </div>

      {/* Entry/Stop/Target row */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, padding: "10px 4px" }}>
        {[
          { l: "ENTRY",  v: "41.20", c: ms.text },
          { l: "STOP",   v: "39.40", c: ms.red },
          { l: "TARGET", v: "47.50", c: ms.green },
        ].map(f => (
          <div key={f.l} style={{ border: `1px solid ${ms.line}`, background: ms.card, padding: "8px 10px" }}>
            <div style={{ fontFamily: ms.mono, fontSize: 8, color: ms.mute3, letterSpacing: "0.16em" }}>{f.l}</div>
            <div style={{ fontFamily: ms.mono, fontSize: 18, color: f.c, marginTop: 2 }}>{f.v}</div>
          </div>
        ))}
      </div>

      <div style={{ padding: "4px 4px 0" }}>
        <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.16em" }}>SIZE $</div>
        <div style={{ fontFamily: ms.mono, fontSize: 26, color: ms.gold, marginTop: 2 }}>$32,000</div>
        <div style={{ fontFamily: ms.mono, fontSize: 10, color: ms.mute2 }}>5.2% of port · risk $1,397</div>
      </div>

      <div style={{ marginTop: 12, padding: "10px 12px", border: `1px solid ${ms.lineSoft}`, background: ms.card2 }}>
        <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.18em" }}>LINK SETUP</div>
        <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4 }}>
          <span style={{ fontSize: 12, color: ms.text }}>sig-ura-2605 · score 88</span>
          <span style={{ fontFamily: ms.mono, fontSize: 11, color: ms.gold }}>›</span>
        </div>
      </div>

      <div style={{
        position: "absolute", left: 14, right: 14, bottom: 38,
      }}>
        <button style={{ width: "100%", background: ms.gold, color: ms.bg, border: 0, padding: "12px 0", fontFamily: ms.mono, fontSize: 12, letterSpacing: "0.18em" }}>LOG TRADE ↵</button>
      </div>
    </PhoneShell>
  );
}

// ─── 6. JOURNAL — closed trades ────────────────────────────────
function MS_Journal() {
  const D = window.MA_DATA;
  const closed = (D.closedTrades || []).slice(0, 8);
  return (
    <PhoneShell>
      <PhoneAppBar
        title="Journal"
        sub="Closed · what worked, what didn't"
        right={<span style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute, letterSpacing: "0.14em" }}>30D</span>}
      />
      <PhTabs tabs={["Positioning","Journal","Dev"]} active="Journal" />

      {/* Strip stats */}
      <div style={{
        display: "grid", gridTemplateColumns: "1fr 1fr 1fr",
        marginTop: 10, border: `1px solid ${ms.lineSoft}`, background: ms.inset,
      }}>
        {[
          { l: "WIN RATE",  v: "62%", c: ms.text },
          { l: "AVG R",     v: "1.74R", c: ms.gold },
          { l: "EXPECT.",   v: "+2.1%", c: ms.green },
        ].map((c, i, arr) => (
          <div key={c.l} style={{ padding: "10px 12px", borderRight: i === arr.length - 1 ? "none" : `1px solid ${ms.lineSoft}` }}>
            <div style={{ fontFamily: ms.mono, fontSize: 8, color: ms.mute3, letterSpacing: "0.16em" }}>{c.l}</div>
            <div style={{ fontFamily: ms.mono, fontSize: 16, color: c.c }}>{c.v}</div>
          </div>
        ))}
      </div>

      <PhSectionHead title="Recent closes" right={`${closed.length}`} />
      <div style={{ display: "flex", flexDirection: "column" }}>
        {closed.map((c, i) => (
          <div key={c.id} style={{
            display: "grid", gridTemplateColumns: "auto 1fr auto",
            alignItems: "center", gap: 10,
            padding: "10px 4px",
            borderTop: i === 0 ? "none" : `1px solid ${ms.lineSoft}`,
          }}>
            <div style={{
              width: 28, textAlign: "center",
              fontFamily: ms.mono, fontSize: 14,
              color: c.pnlPct >= 0 ? ms.green : ms.red,
            }}>{c.pnlPct >= 0 ? "▲" : "▼"}</div>
            <div style={{ minWidth: 0 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
                <span style={{ fontFamily: ms.mono, fontSize: 13 }}>{c.asset}</span>
                <span style={{ fontFamily: ms.mono, fontSize: 9, color: sideColor(c.side), letterSpacing: "0.14em" }}>{c.side}</span>
                <span style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.12em" }}>{c.holdDays}d</span>
              </div>
              <div style={{ fontFamily: ms.serif, fontSize: 11, color: ms.mute, fontStyle: "italic", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                {c.lesson}
              </div>
            </div>
            <div style={{ textAlign: "right" }}>
              <div style={{ fontFamily: ms.mono, fontSize: 13, color: c.pnlPct >= 0 ? ms.green : ms.red }}>{c.pnlPct >= 0 ? "+" : ""}{c.pnlPct.toFixed(2)}%</div>
              <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.1em" }}>thesis · {c.thesis}</div>
            </div>
          </div>
        ))}
      </div>

      <PhBottomNav active="Journal" />
    </PhoneShell>
  );
}

// ─── 7. REGIME + THESIS BRIEF ──────────────────────────────────
function MS_Regime() {
  const D = window.MA_DATA;
  const f = D.regime.framework;
  const t = D.regime.thesis;
  return (
    <PhoneShell>
      <div style={{ display: "flex", justifyContent: "space-between", padding: "6px 16px 4px" }}>
        <span style={{ fontFamily: ms.mono, fontSize: 10, color: ms.mute, letterSpacing: "0.14em" }}>← Positioning</span>
        <span style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.16em" }}>REGIME · {t.version}</span>
      </div>

      <div style={{ padding: "10px 4px 14px" }}>
        <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.18em" }}>FRAMEWORK</div>
        <div style={{ fontFamily: ms.serif, fontSize: 28, lineHeight: 1.05, color: ms.text, marginTop: 4 }}>
          {f.label}
        </div>
        <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginTop: 8 }}>
          <span style={{ fontFamily: ms.mono, fontSize: 32, color: ms.gold, lineHeight: 1 }}>{Math.round(f.confidence * 100)}<span style={{ fontSize: 12, color: ms.mute2 }}>%</span></span>
          <span style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.16em" }}>CONFIDENCE · 90D</span>
        </div>
        <div style={{ marginTop: 6 }}>
          <MiniLine data={D.regime.confidenceTrace} color={ms.gold} w={350} h={32} />
        </div>
      </div>

      {/* tape values */}
      <div style={{
        display: "grid", gridTemplateColumns: "1fr 1fr 1fr",
        border: `1px solid ${ms.lineSoft}`, background: ms.inset,
      }}>
        {[
          { l: "BIAS",        v: f.bias.replace(/_/g, " ") },
          { l: "SIZE MOD",    v: `×${f.sizingModifier.toFixed(2)}` },
          { l: "SCORE MOD",   v: `${f.scoreModifier > 0 ? "+" : ""}${f.scoreModifier}` },
        ].map((c, i, arr) => (
          <div key={c.l} style={{ padding: "10px 12px", borderRight: i === arr.length - 1 ? "none" : `1px solid ${ms.lineSoft}` }}>
            <div style={{ fontFamily: ms.mono, fontSize: 8, color: ms.mute3, letterSpacing: "0.16em" }}>{c.l}</div>
            <div style={{ fontFamily: ms.mono, fontSize: 13, color: ms.text, marginTop: 2 }}>{c.v}</div>
          </div>
        ))}
      </div>

      <PhSectionHead title="Thesis" right={`${t.author} · rev ${t.lastRevised}`} />
      <div style={{ padding: 12, border: `1px solid ${ms.line}`, background: ms.card }}>
        <div style={{ fontFamily: ms.serif, fontSize: 14, color: ms.text, lineHeight: 1.5 }}>
          “{t.narrative}”
        </div>
      </div>

      <PhSectionHead title="Recent transitions" />
      <div style={{ display: "flex", flexDirection: "column", gap: 4, fontFamily: ms.mono, fontSize: 11 }}>
        {D.regime.transitions.slice().reverse().map((tr, i) => (
          <div key={i} style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: 10, color: ms.mute }}>
            <span style={{ color: ms.mute3 }}>{tr.date}</span>
            <span><span style={{ color: ms.mute2 }}>{tr.from}</span> → <span style={{ color: ms.text }}>{tr.to}</span></span>
          </div>
        ))}
      </div>
    </PhoneShell>
  );
}

// ─── 8. ALERTS / NOTIFICATIONS FEED ────────────────────────────
function MS_Alerts() {
  const items = [
    { t: "08:14", k: "SIGNAL", lvl: "high",  asset: "URA",  body: "Score breached 85 → TIER 1. Reactor restart cadence reaffirmed.", c: ms.gold },
    { t: "08:11", k: "REGIME", lvl: "info",  asset: null,   body: "Confidence in Commodity-Led Inflation crossed 78% (90d high).", c: ms.gold },
    { t: "07:58", k: "SIGNAL", lvl: "med",   asset: "TLT",  body: "SHORT setup confirmed — failed reclaim of 50DMA + supply schedule.", c: ms.amber },
    { t: "07:46", k: "TRADE",  lvl: "warn",  asset: "ITA",  body: "Position approaching invalidation: −0.62% with stop 1.4% away.", c: ms.red },
    { t: "07:31", k: "SOURCE", lvl: "info",  asset: null,   body: "Doomberg new note ingested · weight 0.92 · tagged energy/commodities.", c: ms.blue },
    { t: "06:12", k: "REGIME", lvl: "info",  asset: null,   body: "Yesterday's transition note saved · sized agents recalibrated.", c: ms.mute },
  ];
  return (
    <PhoneShell>
      <PhoneAppBar
        title="Alerts"
        sub="Today · 6 new"
        right={<span style={{ fontFamily: ms.mono, fontSize: 9, color: ms.gold, letterSpacing: "0.14em" }}>● 4 unread</span>}
      />

      {/* quick filters */}
      <div style={{ display: "flex", gap: 6, padding: "10px 0 4px", flexWrap: "wrap" }}>
        {["ALL","SIGNAL","REGIME","TRADE","SOURCE"].map((f, i) => (
          <span key={f} style={{
            fontFamily: ms.mono, fontSize: 10, letterSpacing: "0.14em",
            padding: "5px 10px", border: `1px solid ${i === 0 ? ms.gold : ms.line}`,
            color: i === 0 ? ms.gold : ms.mute,
          }}>{f}</span>
        ))}
      </div>

      <div style={{ display: "flex", flexDirection: "column" }}>
        {items.map((n, i) => (
          <div key={i} style={{
            display: "grid", gridTemplateColumns: "44px 1fr",
            gap: 10, padding: "12px 4px",
            borderTop: i === 0 ? "none" : `1px solid ${ms.lineSoft}`,
          }}>
            <div>
              <div style={{ fontFamily: ms.mono, fontSize: 11, color: ms.text }}>{n.t}</div>
              <div style={{ fontFamily: ms.mono, fontSize: 8, color: n.c, letterSpacing: "0.16em", marginTop: 2 }}>{n.k}</div>
            </div>
            <div>
              <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                {n.asset && <span style={{ fontFamily: ms.mono, fontSize: 12, color: ms.text, letterSpacing: "0.04em" }}>{n.asset}</span>}
                <span style={{ fontFamily: ms.mono, fontSize: 8, color: ms.mute3, letterSpacing: "0.14em", textTransform: "uppercase" }}>{n.lvl}</span>
              </div>
              <div style={{ fontSize: 12, color: ms.text, marginTop: 3, lineHeight: 1.4 }}>{n.body}</div>
            </div>
          </div>
        ))}
      </div>

      <PhBottomNav active="Alerts" />
    </PhoneShell>
  );
}

// ─── 9. SETTINGS — agents + spend ──────────────────────────────
function MS_Settings() {
  const D = window.MA_DATA;
  const r = D.reasoning["sig-ura-2605"];
  const totalSpend = r.agentBreakdown.reduce((a, b) => a + b.costUsd, 0);
  return (
    <PhoneShell>
      <PhoneAppBar title="Settings" sub="Agents · spend · sources" />

      {/* Spend card */}
      <div style={{ marginTop: 10, padding: 14, border: `1px solid ${ms.line}`, background: ms.card }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <div>
            <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.18em" }}>SPEND TODAY</div>
            <div style={{ fontFamily: ms.mono, fontSize: 26, color: ms.gold, lineHeight: 1, marginTop: 2 }}>$1.84</div>
          </div>
          <div style={{ textAlign: "right" }}>
            <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute3, letterSpacing: "0.14em" }}>CAP</div>
            <div style={{ fontFamily: ms.mono, fontSize: 13, color: ms.text }}>$5.00</div>
          </div>
        </div>
        <div style={{ height: 4, marginTop: 8, background: ms.inset, border: `1px solid ${ms.lineSoft}` }}>
          <div style={{ width: "37%", height: "100%", background: ms.gold }}></div>
        </div>
      </div>

      <PhSectionHead title="Agents" right={`${r.agentBreakdown.length} online`} />
      <div style={{ display: "flex", flexDirection: "column" }}>
        {r.agentBreakdown.map((a, i) => (
          <div key={a.agent} style={{
            display: "grid", gridTemplateColumns: "1fr auto auto",
            alignItems: "center", gap: 10,
            padding: "10px 4px",
            borderTop: i === 0 ? "none" : `1px solid ${ms.lineSoft}`,
          }}>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontFamily: ms.mono, fontSize: 12, color: ms.text }}>{a.agent}</div>
              <div style={{ fontFamily: ms.mono, fontSize: 9, color: ms.mute2 }}>{a.model}</div>
            </div>
            <div style={{ textAlign: "right", fontFamily: ms.mono, fontSize: 11, color: ms.mute }}>{a.latencyMs}<span style={{ color: ms.mute3 }}>ms</span></div>
            <div style={{ textAlign: "right", fontFamily: ms.mono, fontSize: 11, color: ms.text }}>${a.costUsd.toFixed(3)}</div>
          </div>
        ))}
      </div>

      <PhSectionHead title="Toggles" />
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {[
          { l: "Push alerts · TIER 1+",     on: true },
          { l: "Push alerts · regime change", on: true },
          { l: "Auto-trim @ +5R",            on: false },
          { l: "Quiet hours · 22:00 → 06:00", on: true },
        ].map(t => (
          <div key={t.l} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "10px 12px", border: `1px solid ${ms.lineSoft}`, background: ms.card }}>
            <span style={{ fontSize: 12, color: ms.text }}>{t.l}</span>
            <span style={{
              width: 32, height: 18, borderRadius: 12,
              background: t.on ? ms.gold : ms.line2,
              position: "relative",
            }}>
              <span style={{ position: "absolute", top: 2, left: t.on ? 16 : 2, width: 14, height: 14, borderRadius: 8, background: ms.bg, transition: "left 160ms" }}></span>
            </span>
          </div>
        ))}
      </div>
    </PhoneShell>
  );
}

Object.assign(window, {
  MS_PositioningHome, MS_AssetDetail, MS_Reasoning, MS_ActiveTrade,
  MS_TradeLog, MS_Journal, MS_Regime, MS_Alerts, MS_Settings,
});
