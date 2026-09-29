// /streams — visual map view.
//   S1  Theme map        — 2D bubble map: x=age, y=direction, size=attention
//   S2  Emerging concepts — flare cards (kept)
//   S3  Source graph      — node-link diagram of sources + echo ties
//   S4  Source library    — full registry

const { useState: useStreamsS, useMemo: useStreamsM, useEffect: useStreamsE } = React;

// Hand-placed layout coordinates (normalized 0..1) — stable, designed,
// not force-simulated. Keys = theme.id.
const THEME_POS = {
  "th-power-grid":      { x: 0.10, y: 0.18 },
  "th-mfg-onshoring":   { x: 0.16, y: 0.40 },
  "th-stablecoin-liq":  { x: 0.08, y: 0.78 },
  "th-energy-uranium":  { x: 0.50, y: 0.28 },
  "th-real-debasement": { x: 0.74, y: 0.22 },
  "th-rates-term-prem": { x: 0.58, y: 0.78 },
  "th-ai-tech":         { x: 0.88, y: 0.55 },
};

// Source positions, clustered: macro/rates top-left, energy right,
// news/social bottom, data spread.
const SRC_POS = {
  "bianco":     { x: 0.30, y: 0.22 },
  "fred-cpi":   { x: 0.14, y: 0.34 },
  "fred-dgs10": { x: 0.20, y: 0.52 },
  "fred-dgs30": { x: 0.10, y: 0.20 },
  "goldman":    { x: 0.35, y: 0.46 },
  "epb":        { x: 0.42, y: 0.22 },
  "doomberg":   { x: 0.75, y: 0.30 },
  "energy-cap": { x: 0.86, y: 0.46 },
  "cap-explo":  { x: 0.72, y: 0.58 },
  "kalecki":    { x: 0.90, y: 0.18 },
  "fred-wti":   { x: 0.60, y: 0.42 },
  "woodway":    { x: 0.26, y: 0.82 },
  "zerohedge":  { x: 0.10, y: 0.86 },
  "reuters":    { x: 0.46, y: 0.84 },
  "glassnode":  { x: 0.62, y: 0.76 },
};

const DIR_COLOR = {
  bullish: "var(--green)",
  bearish: "var(--red)",
  mixed:   "var(--amber)",
};

function Streams({ onOpenAsset }) {
  const D = window.MA_DATA;
  const baseThemes = D.themes || [];
  // Dev-only: ?tmstress=N pads the theme set to production density so the
  // declutter layout can be eyeballed at scale. No-op without the flag.
  const themes = useStreamsM(() => {
    // Gated on localStorage ONLY (not the URL) so a stray ?tmstress= query
    // can never trip a normal user into the synthetic stress dataset.
    const m = (typeof localStorage !== "undefined") && localStorage.getItem("ma_tmstress")
      && [null, localStorage.getItem("ma_tmstress")];
    if (!m) return baseThemes;
    const N = Math.max(baseThemes.length, Math.min(60, parseInt(m[1], 10) || 28));
    const dirs = ["bullish", "bearish", "mixed"];
    const stems = ["Social media trend", "Meme stock", "Options strategy", "Momentum spike",
      "Rank spike", "Flow event", "Retail interest", "Trending stock", "Moving average",
      "Social media buzz", "Short interest", "Gamma squeeze", "Sector rotation", "Breadth thrust",
      "Sentiment flip", "Credit spread", "Vol regime", "Carry unwind", "Dispersion bid", "Skew steepening"];
    const out = [...baseThemes];
    const tmpl = baseThemes[0];
    for (let i = 0; out.length < N; i++) {
      const src = baseThemes[i % baseThemes.length];
      out.push({
        ...tmpl, ...src,
        id: "th-stress-" + i,
        label: stems[i % stems.length] + (i >= stems.length ? " " + (i + 1) : ""),
        direction: dirs[i % 3],
        items: 2 + (i * 3) % 9,
        firstSeenDays: 3 + (i * 7) % 70,
        velocity: ((i % 5) - 2) * 0.12,
        avgSourceWeight: src.avgSourceWeight,
        consensus: 0.4 + ((i * 13) % 50) / 100,
        stage: (3 + (i * 7) % 70) < 14 ? "emerging"
             : (((i % 5) - 2) * 0.12 < 0 && (3 + (i * 7) % 70) > 40) ? "fading"
             : "established",
      });
    }
    return out;
  }, [baseThemes]);
  const sources = D.sources || [];
  const echoes = D.echoLinks || [];
  const srcById = useStreamsM(() => Object.fromEntries(sources.map(s => [s.id, s])), [sources]);

  const [dirFilter, setDirFilter] = useStreamsS("all");
  const [hoveredTheme, setHoveredTheme] = useStreamsS(null);
  const [pinnedTheme, setPinnedTheme] = useStreamsS(null);

  return (
    <div data-screen-label="Streams">

      {/* ── S1 · THEME MAP — 2D bubble map ─────────────────────── */}
      <section className="block">
        <header className="block-head">
          <div className="block-title">
            <span className="block-num mono">S1</span>
            <span>Theme map</span>
            <span className="block-sub">2D — age × direction · bubble size = share of attention</span>
          </div>
          <div className="block-actions">
            <div className="filter-pill-row">
              <span className="filter-pill-lbl mono">dir</span>
              {["all","bullish","bearish","mixed"].map(d => (
                <button key={d} className={`filter-pill ${dirFilter === d ? "on" : ""}`} onClick={() => setDirFilter(d)}>{d}</button>
              ))}
            </div>
          </div>
        </header>

        <ThemeMapCanvas
          themes={themes}
          srcById={srcById}
          dirFilter={dirFilter}
          hoveredTheme={hoveredTheme}
          pinnedTheme={pinnedTheme}
          setHovered={setHoveredTheme}
          setPinned={setPinnedTheme}
          onAssetClick={onOpenAsset}
        />
      </section>

      {/* ── S2 · Emerging concepts ─────────────────────────────── */}
      <section className="block">
        <header className="block-head">
          <div className="block-title">
            <span className="block-num mono">S2</span>
            <span>Emerging concepts</span>
            <span className="block-sub">new this week · high velocity · low item count</span>
          </div>
          <div className="block-actions">
            <span className="mono muted">novelty &gt; 0.7 · velocity &gt; 0.4</span>
          </div>
        </header>
        <div className="emerging-grid">
          {themes.filter(t => t.stage === "emerging").map(t => (
            <EmergingCard key={t.id} theme={t} srcById={srcById} />
          ))}
        </div>
      </section>

      {/* ── S3 · Source graph (echo chambers visualised) ───────── */}
      <section className="block">
        <header className="block-head">
          <div className="block-title">
            <span className="block-num mono">S3</span>
            <span>Source graph · echo ties</span>
            <span className="block-sub">bubble = source · ring = tier · thread thickness = echo strength</span>
          </div>
          <div className="block-actions">
            <span className="src-legend mono">
              <span className="src-legend-item"><i className="src-legend-tier tier-1"></i>T1</span>
              <span className="src-legend-item"><i className="src-legend-tier tier-2"></i>T2</span>
              <span className="src-legend-item"><i className="src-legend-tier tier-3"></i>T3</span>
              <span className="src-legend-item"><i className="src-legend-tier tier-4"></i>T4</span>
            </span>
          </div>
        </header>

        <SourceGraphCanvas
          sources={sources}
          echoes={echoes}
          themes={themes}
        />
      </section>

      {/* ── S4 · Source library ────────────────────────────────── */}
      <section className="block">
        <header className="block-head">
          <div className="block-title">
            <span className="block-num mono">S4</span>
            <span>Source library</span>
            <span className="block-sub">{sources.length} registered · ranked by weight</span>
          </div>
        </header>
        <table className="wl-table src-table">
          <thead>
            <tr>
              <th>SOURCE</th>
              <th>KIND</th>
              <th>TIER</th>
              <th className="num">WEIGHT</th>
              <th>TOPICS</th>
            </tr>
          </thead>
          <tbody>
            {sources.slice().sort((a,b) => b.weight - a.weight).map(s => (
              <tr key={s.id}>
                <td className="mono">{s.name}</td>
                <td className="mono muted">{s.kind}</td>
                <td><span className={`src-tier tier-${s.tier}`}>T{s.tier}</span></td>
                <td className="num mono">
                  <div className="src-weight">
                    <span>{s.weight.toFixed(2)}</span>
                    <div className="src-weight-rail">
                      <i style={{ width: `${s.weight * 100}%` }}></i>
                    </div>
                  </div>
                </td>
                <td>
                  <div className="src-topics">
                    {s.topics.map(t => <span key={t} className="src-topic">{t}</span>)}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// THEME MAP CANVAS  —  SVG bubble map + tie lines between themes
//                      that share sources.
// ═══════════════════════════════════════════════════════════════
function ThemeMapCanvas({ themes, srcById, dirFilter, hoveredTheme, pinnedTheme, setHovered, setPinned, onAssetClick }) {
  const W = 1100, H = 580;
  const pad = { l: 96, r: 64, t: 56, b: 76 };
  const innerW = W - pad.l - pad.r;
  const innerH = H - pad.t - pad.b;

  // ── Time-lapse machinery ────────────────────────────────────
  // step 0..NWEEKS, NWEEKS = now. weeksAgo = NWEEKS - step.
  // Themes drift right as they mature (rewind pushes them left, shrinks
  // them, and pops the freshest ones out of existence) so the trader can
  // watch emerging narratives enter from the left and age across the map.
  const NWEEKS = 4;
  const NOW_IDX = 29; // last index of the 30-day mention trace

  const [step, setStep] = useStreamsS(() => {
    const s = parseInt(localStorage.getItem("ma_streams_step") || "", 10);
    return Number.isFinite(s) && s >= 0 && s <= NWEEKS ? s : NWEEKS;
  });
  const [playing, setPlaying] = useStreamsS(false);
  const weeksAgo = NWEEKS - step;

  // Fading-theme treatment — how the dead/dying tail is decluttered.
  //   lane     · park them as small dots in a decay gutter along the bottom
  //   collapse · fold each direction's fading themes into one summary bubble
  //   dim      · keep them in-band but shrunk + faded into the background
  const [fadeMode, setFadeMode] = useStreamsS(
    () => localStorage.getItem("ma_streams_fademode") || "lane"
  );
  const [fadeOpen, setFadeOpen] = useStreamsS(false); // collapse-mode expansion
  useStreamsE(() => { localStorage.setItem("ma_streams_fademode", fadeMode); }, [fadeMode]);

  useStreamsE(() => { localStorage.setItem("ma_streams_step", String(step)); }, [step]);
  useStreamsE(() => {
    if (!playing) return;
    if (step >= NWEEKS) { setPlaying(false); return; }
    const id = setTimeout(() => setStep(s => Math.min(NWEEKS, s + 1)), 950);
    return () => clearTimeout(id);
  }, [playing, step]);
  const togglePlay = () => {
    if (step >= NWEEKS) { setStep(0); setPlaying(true); }
    else setPlaying(p => !p);
  };

  const hashPhase = (id) => {
    let h = 0; for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 97;
    return h / 97;
  };
  const traceWin = (trace, wa) => {
    const idx = NOW_IDX - wa * 7;
    if (idx < 0) return 0;
    let s = 0; for (let i = Math.max(0, idx - 6); i <= idx; i++) s += trace[i] || 0;
    return s;
  };
  const attnAt  = (t, wa) => traceWin(t.trace, wa) * t.avgSourceWeight;
  const existsAt = (t, wa) => (t.firstSeenDays - wa * 7) > 0;
  const ageAt   = (t, wa) => t.firstSeenDays - wa * 7;
  const driftOf = (t) => 0.030 + Math.max(0, t.velocity) * 0.052;
  const isFading = (t) => t.stage === "fading";

  // ── Computed declutter layout ───────────────────────────────
  // Production carries ~28 themes. Two things were piling them up:
  //  1) the old hand-placed table only covered 7 themes, so the rest
  //     collapsed onto map-center;
  //  2) fading bearish narratives (old + low band + low velocity) all
  //     resolve to the same bottom-right corner, so no amount of
  //     repulsion truly separates them.
  // Fix: LIVE themes get a semantic position —
  //   x = age (younger → left, older → right, sqrt-spread)
  //   y = direction band (bullish high · mixed mid · bearish low), fanned
  // — relaxed for collisions in the UPPER ~82% of the canvas. FADING themes
  // leave the live field entirely and settle into a DECAY LANE along the
  // bottom: small, dimmed, packed by recency, no drift (a dead narrative
  // has stopped moving). Born left → mature across → sink as they fade.
  const GUTTER_Y = 0.95;     // decay-lane row (normalised)
  const LIVE_FLOOR = 0.84;   // live bands stay above this
  const basePos = useStreamsM(() => {
    const list = themes.filter(t => !isFading(t));
    const faded = themes.filter(isFading);
    const P = {};
    if (list.length) {
      const maxAge = Math.max(...list.map(t => t.firstSeenDays), 1);
      const bandY = { bullish: 0.24, mixed: 0.46, bearish: 0.66 };
      list.forEach(t => {
        const hand = THEME_POS[t.id];
        if (hand) { P[t.id] = { x: hand.x, y: Math.min(LIVE_FLOOR, hand.y) }; return; }
        const ageN = Math.sqrt(Math.max(0, t.firstSeenDays) / maxAge); // 0..1
        const hp = hashPhase(t.id);                                    // 0..1 stable
        const x = 0.07 + ageN * 0.84;
        let y = (bandY[t.direction] ?? 0.45) + (hp - 0.5) * 0.30;      // ±0.15 fan
        y = Math.min(LIVE_FLOOR, Math.max(0.06, y));
        P[t.id] = { x, y };
      });
      // pixel-space collision relaxation (Gauss–Seidel, x-damped for age)
      const mAttn = Math.max(...list.map(t => t.items * t.avgSourceWeight), 1);
      const radPx = t => 26 + ((t.items * t.avgSourceWeight) / mAttn) * 46;
      const ids = list.map(t => t.id);
      const byId = Object.fromEntries(list.map(t => [t.id, t]));
      for (let iter = 0; iter < 110; iter++) {
        for (let i = 0; i < ids.length; i++) {
          for (let j = i + 1; j < ids.length; j++) {
            const a = ids[i], b = ids[j];
            const ax = pad.l + P[a].x * innerW, ay = pad.t + P[a].y * innerH;
            const bx = pad.l + P[b].x * innerW, by = pad.t + P[b].y * innerH;
            let dx = bx - ax, dy = by - ay;
            let d = Math.hypot(dx, dy) || 0.01;
            const minD = radPx(byId[a]) + radPx(byId[b]) + 16;
            if (d < minD) {
              const push = (minD - d) / 2;
              const ux = dx / d, uy = dy / d;
              P[a].x -= (ux * push * 0.35) / innerW; P[a].y -= (uy * push) / innerH;
              P[b].x += (ux * push * 0.35) / innerW; P[b].y += (uy * push) / innerH;
              P[a].x = Math.min(0.95, Math.max(0.04, P[a].x));
              P[b].x = Math.min(0.95, Math.max(0.04, P[b].x));
              P[a].y = Math.min(LIVE_FLOOR, Math.max(0.06, P[a].y));
              P[b].y = Math.min(LIVE_FLOOR, Math.max(0.06, P[b].y));
            }
          }
        }
      }
    }
    // ── fading tail, treated per mode ─────────────────────────
    const laneLike = fadeMode === "lane" || (fadeMode === "collapse" && fadeOpen);
    if (laneLike) {
      // decay lane: pack left→right, most-recently-active first
      const ordered = [...faded].sort((a, b) => a.firstSeenDays - b.firstSeenDays);
      const n = ordered.length;
      ordered.forEach((t, i) => {
        const frac = n <= 1 ? 0.5 : i / (n - 1);
        P[t.id] = { x: 0.04 + frac * 0.90, y: GUTTER_Y, gutter: true };
      });
    } else if (fadeMode === "dim") {
      // in-band, shrunk + faded: keep direction semantics, jitter on y
      const bandY = { bullish: 0.26, mixed: 0.50, bearish: 0.74 };
      const maxAge = Math.max(...themes.map(t => t.firstSeenDays), 1);
      faded.forEach(t => {
        const hp = hashPhase(t.id);
        const ageN = Math.sqrt(Math.max(0, t.firstSeenDays) / maxAge);
        const x = Math.min(0.95, 0.10 + ageN * 0.82);
        let y = (bandY[t.direction] ?? 0.5) + (hp - 0.5) * 0.22; // wider fan = jitter
        y = Math.min(0.93, Math.max(0.08, y));
        P[t.id] = { x, y, faded: true };
      });
    } else {
      // collapse (folded): individual fading themes are hidden behind clusters
      faded.forEach(t => { P[t.id] = { x: 0.5, y: 0.5, hidden: true }; });
    }
    return P;
  }, [themes, innerW, innerH, fadeMode, fadeOpen]);

  // Collapse-mode summary nodes — one per direction band that has fading themes
  const fadeClusters = useStreamsM(() => {
    if (fadeMode !== "collapse" || fadeOpen) return [];
    const byDir = {};
    themes.filter(isFading).forEach(t => {
      if (dirFilter !== "all" && t.direction !== dirFilter) return;
      (byDir[t.direction] = byDir[t.direction] || []).push(t);
    });
    const bandY = { bullish: 0.24, mixed: 0.46, bearish: 0.66 };
    return Object.entries(byDir).map(([dir, members]) => ({
      id: "fade-cluster-" + dir,
      direction: dir,
      count: members.length,
      items: members.reduce((s, m) => s + m.items, 0),
      x: 0.93, y: bandY[dir] ?? 0.5,
    }));
  }, [themes, fadeMode, fadeOpen, dirFilter]);

  const inGutter = (t) => !!(basePos[t.id] && basePos[t.id].gutter);
  const isDimmedFade = (t) => !!(basePos[t.id] && basePos[t.id].faded);
  const isHiddenFade = (t) => !!(basePos[t.id] && basePos[t.id].hidden);
  const parked = (t) => isFading(t); // fading themes have stopped moving
  const normX = (t, wa) => {
    const base = (basePos[t.id] || { x: 0.5 }).x;
    if (parked(t)) return base;                   // fading themes don't drift
    return Math.min(0.95, Math.max(0.035, base - wa * driftOf(t)));
  };
  const normY = (t, wa) => {
    const base = (basePos[t.id] || { y: 0.5 }).y;
    if (parked(t)) return base;
    return base + Math.sin(hashPhase(t.id) * 6.28 + wa * 0.9) * 0.014;
  };
  const centerAt = (t, wa) => ({
    x: pad.l + normX(t, wa) * innerW,
    y: pad.t + normY(t, wa) * innerH,
  });

  const visible = themes.filter(t => dirFilter === "all" || t.direction === dirFilter);
  const maxAttn = Math.max(...themes.map(t => t.items * t.avgSourceWeight));

  // Label thinning: when nothing is selected, name only the top themes by
  // attention; the rest reveal their name on hover. The "N items · X%"
  // sub-line is reserved for the hovered/pinned theme. This is what clears
  // the text pile-up in dense production data.
  const LABEL_CAP = 9;
  const labeledIds = useStreamsM(() => {
    const ranked = [...visible].sort(
      (a, b) => (b.items * b.avgSourceWeight) - (a.items * a.avgSourceWeight)
    );
    return new Set(ranked.slice(0, LABEL_CAP).map(t => t.id));
  }, [visible]);

  // base ("now") geometry — bubbles are drawn at this size, then translated
  // and scaled per displayed week so motion animates with one CSS transition.
  const posOf = (t) => centerAt(t, weeksAgo);
  const radOf = (t) => {
    if (inGutter(t)) return 13;        // decay-lane dots
    if (isDimmedFade(t)) return 17;    // shrunk in-band fades
    const w = (t.items * t.avgSourceWeight) / maxAttn;
    return 26 + w * 46; // 26..72 px
  };
  const scaleAt = (t, wa) => {
    if (parked(t)) return 1;
    const now = attnAt(t, 0);
    if (now <= 0) return 1;
    return Math.max(0.30, Math.min(1.12, attnAt(t, wa) / now));
  };
  // breadcrumb trail: theme's path from emergence up to the displayed week
  const trailFor = (t) => {
    if (parked(t)) return []; // parked / fading themes leave no trail
    const pts = [];
    for (let wa = weeksAgo; wa <= NWEEKS; wa++) {
      if (!existsAt(t, wa)) break;
      pts.push(centerAt(t, wa));
    }
    return pts;
  };

  // Tie lines: pairs of LIVE themes that share ≥1 contributing source.
  // (Parked / fading themes don't sprout threads across the map.)
  const tieThemes = visible.filter(t => !inGutter(t));
  const ties = [];
  for (let i = 0; i < tieThemes.length; i++) {
    for (let j = i + 1; j < tieThemes.length; j++) {
      const a = tieThemes[i], b = tieThemes[j];
      const aSrc = new Set(a.sources.map(s => s.id));
      const shared = b.sources.filter(s => aSrc.has(s.id));
      if (shared.length) ties.push({ a, b, shared: shared.length });
    }
  }

  const focusId = pinnedTheme || hoveredTheme;
  const focused = visible.find(t => t.id === focusId);
  const timeLabel = weeksAgo === 0 ? "NOW" : `\u2212${weeksAgo}W`;

  return (
    <div className="theme-map">
      {/* ── Time-lapse control ─────────────────────────────────── */}
      <div className="tm-timebar">
        <button className="tm-play" onClick={togglePlay}>
          <span className={`tm-play-ico ${playing ? "pause" : "play"}`}></span>
          {playing ? "PAUSE" : (weeksAgo === 0 ? "REPLAY" : "PLAY")}
        </button>
        <div className="tm-time-track">
          <input type="range" className="tm-time-range"
                 min={0} max={NWEEKS} step={1} value={step}
                 onChange={(e) => { setPlaying(false); setStep(parseInt(e.target.value, 10)); }} />
          <div className="tm-time-ticks mono">
            <span>{`\u2212${NWEEKS}W`}</span>
            <span>NARRATIVE  DRIFT</span>
            <span>NOW</span>
          </div>
        </div>
        <div className="tm-time-now">
          <div className="tm-time-now-val mono">{timeLabel}</div>
          <div className="tm-time-now-lbl">{weeksAgo === 0 ? "live" : "weeks ago"}</div>
        </div>
      </div>
      <div className="tm-subbar">
        <div className="tm-time-cap mono">
          scrub or press play — emerging themes enter at the left and drift right as they age; the gold ring fades as a narrative matures.
        </div>
        <div className="tm-fade-ctl mono">
          <span className="tm-fade-lbl">fading</span>
          {["lane", "collapse", "dim"].map(m => (
            <button key={m}
                    className={`tm-fade-opt ${fadeMode === m ? "on" : ""}`}
                    onClick={() => { setFadeMode(m); setFadeOpen(false); }}>
              {m}
            </button>
          ))}
        </div>
      </div>

      <svg viewBox={`0 0 ${W} ${H}`} className="theme-map-svg" preserveAspectRatio="xMidYMid meet">
        {/* axis backdrop */}
        <defs>
          <pattern id="tm-dots" width="22" height="22" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="0.8" fill="var(--line-soft)" />
          </pattern>
          <radialGradient id="tm-bull" cx="50%" cy="50%" r="50%">
            <stop offset="0%"  stopColor="var(--green)" stopOpacity="0.42" />
            <stop offset="70%" stopColor="var(--green)" stopOpacity="0.14" />
            <stop offset="100%" stopColor="var(--green)" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="tm-bear" cx="50%" cy="50%" r="50%">
            <stop offset="0%"  stopColor="var(--red)" stopOpacity="0.42" />
            <stop offset="70%" stopColor="var(--red)" stopOpacity="0.14" />
            <stop offset="100%" stopColor="var(--red)" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="tm-mixed" cx="50%" cy="50%" r="50%">
            <stop offset="0%"  stopColor="var(--amber)" stopOpacity="0.42" />
            <stop offset="70%" stopColor="var(--amber)" stopOpacity="0.14" />
            <stop offset="100%" stopColor="var(--amber)" stopOpacity="0" />
          </radialGradient>
        </defs>
        <rect x={pad.l} y={pad.t} width={W - pad.l - pad.r} height={H - pad.t - pad.b}
              fill="url(#tm-dots)" opacity="0.5" />

        {/* ── Axis system ─────────────────────────────────────── */}
        {(() => {
          const innerW = W - pad.l - pad.r;
          const innerH = H - pad.t - pad.b;
          const midX = pad.l + innerW * 0.5;
          const midY = pad.t + innerH * 0.5;
          const ax = "var(--text)";
          const axDim = "var(--text-mute-2)";
          return (
            <g className="tm-axes">
              {/* gutter band — pushes axes into a clearly labelled margin */}
              <rect x={0} y={H - pad.b} width={W} height={pad.b}
                    fill="var(--bg-inset)" opacity="0.55" />
              <rect x={0} y={0} width={pad.l} height={H}
                    fill="var(--bg-inset)" opacity="0.55" />

              {/* frame */}
              <rect x={pad.l} y={pad.t} width={innerW} height={innerH}
                    fill="none" stroke="var(--line-2)" strokeWidth="1" />

              {/* X axis baseline (bottom) */}
              <line x1={pad.l} x2={W - pad.r} y1={H - pad.b} y2={H - pad.b}
                    stroke={ax} strokeWidth="2" />
              {/* X axis arrowheads */}
              <polygon points={`${pad.l - 4},${H - pad.b} ${pad.l + 8},${H - pad.b - 5} ${pad.l + 8},${H - pad.b + 5}`} fill={ax} />
              <polygon points={`${W - pad.r + 4},${H - pad.b} ${W - pad.r - 8},${H - pad.b - 5} ${W - pad.r - 8},${H - pad.b + 5}`} fill={ax} />

              {/* X axis ticks at 25 / 50 / 75% */}
              {[0.25, 0.5, 0.75].map((t, i) => {
                const x = pad.l + innerW * t;
                return (
                  <g key={i}>
                    <line x1={x} x2={x} y1={H - pad.b} y2={H - pad.b + 7} stroke={ax} strokeWidth="1.2" />
                    <line x1={x} x2={x} y1={pad.t} y2={H - pad.b}
                          stroke="var(--line)" strokeWidth="1" strokeDasharray="2 6" opacity={t === 0.5 ? 1 : 0.6} />
                  </g>
                );
              })}

              {/* Y axis baseline (left) */}
              <line x1={pad.l} x2={pad.l} y1={pad.t} y2={H - pad.b}
                    stroke={ax} strokeWidth="2" />
              {/* Y axis arrowheads */}
              <polygon points={`${pad.l},${pad.t - 4} ${pad.l - 5},${pad.t + 8} ${pad.l + 5},${pad.t + 8}`} fill={ax} />
              <polygon points={`${pad.l},${H - pad.b + 4} ${pad.l - 5},${H - pad.b - 8} ${pad.l + 5},${H - pad.b - 8}`} fill={ax} />

              {/* Y axis ticks at 25 / 50 / 75% */}
              {[0.25, 0.5, 0.75].map((t, i) => {
                const y = pad.t + innerH * t;
                return (
                  <g key={i}>
                    <line x1={pad.l - 7} x2={pad.l} y1={y} y2={y} stroke={ax} strokeWidth="1.2" />
                    <line x1={pad.l} x2={W - pad.r} y1={y} y2={y}
                          stroke="var(--line)" strokeWidth="1" strokeDasharray="2 6" opacity={t === 0.5 ? 1 : 0.6} />
                  </g>
                );
              })}

              {/* Mid cross-hair (subtle, slightly stronger than other gridlines) */}
              <line x1={midX} x2={midX} y1={pad.t} y2={H - pad.b}
                    stroke="var(--text-mute-3)" strokeWidth="1" strokeDasharray="3 5" opacity="0.5" />
              <line x1={pad.l} x2={W - pad.r} y1={midY} y2={midY}
                    stroke="var(--text-mute-3)" strokeWidth="1" strokeDasharray="3 5" opacity="0.5" />

              {/* X axis ENDPOINT labels — louder + bigger */}
              <text x={pad.l + 6} y={H - pad.b + 26}
                    fontFamily="var(--mono)" fontSize="14" fill="var(--gold)"
                    letterSpacing="0.22em" fontWeight="600">← EMERGING</text>
              <text x={W - pad.r - 6} y={H - pad.b + 26} textAnchor="end"
                    fontFamily="var(--mono)" fontSize="14" fill="var(--text-mute)"
                    letterSpacing="0.22em" fontWeight="600">FADING →</text>
              {/* X axis title (centered, below) */}
              <text x={midX} y={H - pad.b + 56} textAnchor="middle"
                    fontFamily="var(--mono)" fontSize="11" fill={axDim}
                    letterSpacing="0.32em" fontWeight="500">NARRATIVE  LIFECYCLE   ·   first-seen × velocity</text>

              {/* Y axis ENDPOINT labels — louder + bigger */}
              <g transform={`translate(${pad.l - 30}, ${pad.t + 8}) rotate(-90)`}>
                <text textAnchor="end" fontFamily="var(--mono)" fontSize="14"
                      fill="var(--green)" letterSpacing="0.22em" fontWeight="600">BULLISH ↑</text>
              </g>
              <g transform={`translate(${pad.l - 30}, ${H - pad.b - 8}) rotate(-90)`}>
                <text textAnchor="start" fontFamily="var(--mono)" fontSize="14"
                      fill="var(--red)" letterSpacing="0.22em" fontWeight="600">↓ BEARISH</text>
              </g>
              {/* Y axis title */}
              <g transform={`translate(${pad.l - 64}, ${midY}) rotate(-90)`}>
                <text textAnchor="middle" fontFamily="var(--mono)" fontSize="11"
                      fill={axDim} letterSpacing="0.32em" fontWeight="500">DIRECTION  ·  consensus × tilt</text>
              </g>
              {/* MIXED midline label */}
              <text x={pad.l - 10} y={midY + 4} textAnchor="end"
                    fontFamily="var(--mono)" fontSize="11" fill="var(--amber)"
                    letterSpacing="0.18em" fontWeight="500">MIXED</text>

              {/* Quadrant tags (top + bottom corners, subtle) */}
              <text x={pad.l + 12} y={pad.t + 18}
                    fontFamily="var(--mono)" fontSize="10" fill={axDim}
                    letterSpacing="0.22em" opacity="0.85">↖ FRESH · BULL</text>
              <text x={W - pad.r - 12} y={pad.t + 18} textAnchor="end"
                    fontFamily="var(--mono)" fontSize="10" fill={axDim}
                    letterSpacing="0.22em" opacity="0.85">EXTENDED · BULL ↗</text>
              <text x={pad.l + 12} y={H - pad.b - 10}
                    fontFamily="var(--mono)" fontSize="10" fill={axDim}
                    letterSpacing="0.22em" opacity="0.85">↙ FRESH · BEAR</text>
              <text x={W - pad.r - 12} y={H - pad.b - 10} textAnchor="end"
                    fontFamily="var(--mono)" fontSize="10" fill={axDim}
                    letterSpacing="0.22em" opacity="0.85">FADING · BEAR ↘</text>
            </g>
          );
        })()}

        {/* decay-lane divider — present in lane mode (or when a cluster is expanded) */}
        {(fadeMode === "lane" || (fadeMode === "collapse" && fadeOpen)) && (() => {
          const ly = pad.t + 0.885 * innerH;
          return (
            <g style={{ transition: "opacity .4s ease" }}>
              <line x1={pad.l - 10} y1={ly} x2={W - pad.r} y2={ly}
                    stroke="var(--line-2)" strokeWidth="1" strokeDasharray="2 6" opacity="0.7" />
              <text x={pad.l - 10} y={ly + 16} fontFamily="var(--mono)" fontSize="9.5"
                    fill="var(--text-mute-3)" letterSpacing="0.22em">DECAY LANE · fading out, no recent mentions</text>
              {fadeMode === "collapse" && fadeOpen && (
                <text x={W - pad.r} y={ly + 16} textAnchor="end" fontFamily="var(--mono)" fontSize="9.5"
                      fill="var(--gold)" letterSpacing="0.16em" style={{ cursor: "pointer" }}
                      onClick={() => setFadeOpen(false)}>▾ FOLD BACK</text>
              )}
            </g>
          );
        })()}

        {/* drift trails — each theme's path from emergence to the shown week */}
        <g className="tm-trails" style={{ transition: "opacity .5s ease" }}>
          {visible.map(t => {
            const pts = trailFor(t);
            if (pts.length < 2) return null;
            const dimmed = focusId && focusId !== t.id;
            return (
              <polyline key={t.id}
                        points={pts.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ")}
                        fill="none"
                        stroke={DIR_COLOR[t.direction]}
                        strokeWidth="1.5"
                        strokeDasharray="1 5"
                        strokeLinecap="round"
                        opacity={dimmed ? 0.05 : 0.26} />
            );
          })}
        </g>

        {/* tie strings (a "now" analysis — fade out while time-travelling) */}
        <g style={{ opacity: weeksAgo === 0 ? 1 : 0.1, transition: "opacity .5s ease" }}>
          {ties.map((t, i) => {
            const pa = posOf(t.a), pb = posOf(t.b);
            const involved = !focusId || focusId === t.a.id || focusId === t.b.id;
            return (
              <path key={i}
                    d={`M ${pa.x},${pa.y} Q ${(pa.x+pb.x)/2},${(pa.y+pb.y)/2 - 18} ${pb.x},${pb.y}`}
                    stroke="var(--text-mute-3)"
                    strokeWidth={0.6 + t.shared * 0.45}
                    fill="none"
                    opacity={focusId ? (involved ? 0.55 : 0.08) : 0.22}
                    strokeDasharray="3 4" />
            );
          })}
        </g>

        {/* theme bubbles */}
        {visible.map(t => {
          if (isHiddenFade(t)) return null; // folded behind a collapse cluster
          const { x, y } = posOf(t);
          const r = radOf(t);
          const sc = scaleAt(t, weeksAgo);
          const here = existsAt(t, weeksAgo);
          const fade = inGutter(t) || isDimmedFade(t);
          const focused = focusId === t.id;
          const dimmed = focusId && !focused;
          const grad = t.direction === "bullish" ? "tm-bull" : t.direction === "bearish" ? "tm-bear" : "tm-mixed";
          const color = DIR_COLOR[t.direction];
          const ringOp = (here && !fade) ? Math.max(0, Math.min(1, (28 - ageAt(t, weeksAgo)) / 26)) : 0;
          const baseOp = fade ? (focused ? 0.95 : (dimmed ? 0.22 : 0.42)) : (dimmed ? 0.4 : 1);
          const showName = fade
            ? focused
            : (focusId ? focused : labeledIds.has(t.id));
          return (
            <g key={t.id}
               className={`tm-node dir-${t.direction} stage-${t.stage} ${fade ? "is-fading" : ""}`}
               style={{
                 transform: `translate(${x}px, ${y}px) scale(${sc})`,
                 transformOrigin: "0px 0px",
                 opacity: here ? baseOp : 0,
                 pointerEvents: here ? "auto" : "none",
                 transition: "transform .85s cubic-bezier(.45,.05,.3,1), opacity .55s ease",
                 cursor: "pointer",
               }}
               onMouseEnter={() => setHovered(t.id)}
               onMouseLeave={() => setHovered(null)}
               onClick={() => setPinned(pinnedTheme === t.id ? null : t.id)}>
              {/* halo — suppressed for fading dots */}
              {!fade && <circle r={r + 14} fill={`url(#${grad})`} />}
              {/* core */}
              <circle r={r} fill="var(--bg-card)" stroke={color}
                      strokeWidth={focused ? 2.2 : (fade ? 1 : 1.4)}
                      strokeDasharray={fade ? "2 2.5" : "none"} />
              {/* emerging ring — fades off as the narrative matures */}
              {ringOp > 0.02 && (
                <circle r={r + 4} fill="none" stroke="var(--gold)" strokeWidth="1"
                        strokeDasharray="2 3" opacity={ringOp * 0.85}
                        style={{ transition: "opacity .55s ease" }} />
              )}
              {/* velocity arrow — only on live bubbles */}
              {!fade && (
                <g transform={`translate(${r - 14}, ${-r + 12})`}>
                  <text fontFamily="var(--mono)" fontSize="10" fill={t.velocity > 0.1 ? "var(--green)" : t.velocity < 0 ? "var(--red)" : "var(--text-mute)"} textAnchor="middle">
                    {t.velocity > 0.1 ? "↗" : t.velocity < 0 ? "↘" : "→"}
                  </text>
                </g>
              )}
              {/* label — thinned: top themes by default, only the active one when focused */}
              {showName && (
                <g style={{ transition: "opacity .35s ease" }}>
                  <text textAnchor="middle" y={fade ? (r + 14) : (r > 40 ? -2 : -4)}
                        fontFamily="var(--serif)" fontStyle="italic"
                        fontSize={fade ? 12 : (r > 50 ? 16 : 13)} fill="var(--text)"
                        style={{ paintOrder: "stroke" }} stroke="var(--bg-0)" strokeWidth="3" strokeLinejoin="round">
                    {wrapLabel(t.label)[0]}
                  </text>
                  {!fade && wrapLabel(t.label)[1] && (
                    <text textAnchor="middle" y={r > 40 ? 14 : 11}
                          fontFamily="var(--serif)" fontStyle="italic"
                          fontSize={r > 50 ? 16 : 13} fill="var(--text)"
                          style={{ paintOrder: "stroke" }} stroke="var(--bg-0)" strokeWidth="3" strokeLinejoin="round">
                      {wrapLabel(t.label)[1]}
                    </text>
                  )}
                </g>
              )}
              {/* items count — only for the hovered / pinned theme */}
              {focused && (
                <text textAnchor="middle" y={fade ? (r + 30) : (r > 40 ? 32 : 24)}
                      fontFamily="var(--mono)" fontSize="11" fill={color} letterSpacing="0.08em"
                      style={{ paintOrder: "stroke" }} stroke="var(--bg-0)" strokeWidth="3" strokeLinejoin="round">
                  {t.items} items · {Math.round(t.consensus * 100)}%
                </text>
              )}
            </g>
          );
        })}

        {/* collapse-mode summary bubbles — one per direction band */}
        {fadeClusters.map(c => {
          const cx = pad.l + c.x * innerW, cy = pad.t + c.y * innerH;
          const color = DIR_COLOR[c.direction];
          const r = 22 + Math.min(20, c.count * 3);
          return (
            <g key={c.id} className="tm-fade-cluster"
               style={{ cursor: "pointer", transition: "opacity .4s ease" }}
               transform={`translate(${cx}, ${cy})`}
               onClick={() => setFadeOpen(true)}>
              <circle r={r + 7} fill="none" stroke={color} strokeWidth="1" strokeDasharray="2 3" opacity="0.5" />
              <circle r={r} fill="var(--bg-inset)" stroke={color} strokeWidth="1.4" opacity="0.92" />
              <text textAnchor="middle" y={-2} fontFamily="var(--mono)" fontSize="18" fill={color} fontWeight="600">{c.count}</text>
              <text textAnchor="middle" y={13} fontFamily="var(--mono)" fontSize="8.5" fill="var(--text-mute-2)" letterSpacing="0.12em">FADING</text>
              <text textAnchor="middle" y={r + 16} fontFamily="var(--mono)" fontSize="9" fill="var(--text-mute-3)" letterSpacing="0.1em">click to expand</text>
            </g>
          );
        })}
      </svg>

      {/* Focus panel — appears when a theme is hovered or pinned */}
      <div className={`tm-focus ${focused ? "on" : ""}`}>
        {focused ? (
          <ThemeFocusCard theme={focused} srcById={srcById} onAssetClick={onAssetClick} pinned={!!pinnedTheme} onUnpin={() => setPinned(null)} />
        ) : (
          <div className="tm-focus-hint mono">
            <div className="tm-focus-hint-title">HOVER · CLICK A BUBBLE</div>
            <div className="tm-focus-hint-sub">{themes.length} themes mapped · {DIR_COLOR ? "" : ""}drag-to-explore</div>
            <ul className="tm-focus-hint-list">
              <li><span className="tm-legend-dot bull"></span> bullish · upper half</li>
              <li><span className="tm-legend-dot bear"></span> bearish · lower half</li>
              <li><span className="tm-legend-dot mix"></span> mixed · middle band</li>
              <li><span className="tm-legend-dash"></span> dashed ring = emerging</li>
              <li><span className="tm-legend-dot fade"></span> small dashed dot = fading (dead/dying)</li>
              <li><span className="tm-legend-thread"></span> threads connect themes sharing a source</li>
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

function wrapLabel(s) {
  if (s.length <= 14) return [s, ""];
  // split on " / " or first space past half
  if (s.includes(" / ")) {
    const [a, b] = s.split(" / ");
    return [a + " /", b];
  }
  const half = Math.floor(s.length / 2);
  let cut = s.indexOf(" ", half);
  if (cut < 0) cut = s.lastIndexOf(" ", half);
  if (cut < 0) return [s, ""];
  return [s.slice(0, cut), s.slice(cut + 1)];
}

function ThemeFocusCard({ theme: t, srcById, onAssetClick, pinned, onUnpin }) {
  const dItems = t.items - t.itemsPrev;
  return (
    <div className={`tm-focus-card dir-${t.direction}`}>
      <div className="tm-focus-head">
        <div className="tm-focus-name">{t.label}</div>
        <div className="tm-focus-meta">
          <span className={`theme-dir mono dir-${t.direction}`}>{t.direction}</span>
          {pinned && (
            <button className="tm-focus-unpin mono" onClick={onUnpin}>UNPIN ×</button>
          )}
        </div>
      </div>
      <p className="tm-focus-blurb">{t.blurb}</p>
      <div className="tm-focus-stats">
        <div className="tm-fs"><div className="tm-fs-val mono">{t.items}</div><div className="tm-fs-lbl mono">items 30d</div></div>
        <div className="tm-fs"><div className={`tm-fs-val mono ${dItems > 0 ? "pos" : dItems < 0 ? "neg" : "muted"}`}>{dItems >= 0 ? "+" : ""}{dItems}</div><div className="tm-fs-lbl mono">Δ 7d</div></div>
        <div className="tm-fs"><div className={`tm-fs-val mono ${t.velocity > 0.1 ? "pos" : t.velocity < 0 ? "neg" : "muted"}`}>{t.velocity >= 0 ? "+" : ""}{t.velocity.toFixed(2)}</div><div className="tm-fs-lbl mono">velocity</div></div>
        <div className="tm-fs"><div className="tm-fs-val mono">{Math.round(t.consensus * 100)}<span className="muted">%</span></div><div className="tm-fs-lbl mono">consensus</div></div>
      </div>
      <div className="tm-focus-spark">
        <Sparkline series={t.trace} width={300} height={36} />
        <div className="tm-focus-spark-axis mono"><span>−30d</span><span>mentions/day</span><span>now</span></div>
      </div>
      <div className="tm-focus-row">
        <div className="tm-focus-row-lbl mono">SOURCES</div>
        <div className="tm-focus-srcs">
          {t.sources.map(s => {
            const meta = srcById[s.id];
            if (!meta) return null;
            return (
              <span key={s.id} className={`theme-src dir-${s.direction}`}>
                <span className="theme-src-dot"></span>
                <span className="theme-src-name mono">{meta.name}</span>
                <span className="theme-src-w mono muted">·{s.items}</span>
              </span>
            );
          })}
        </div>
      </div>
      <div className="tm-focus-row">
        <div className="tm-focus-row-lbl mono">ASSETS</div>
        <div className="tm-focus-srcs">
          {t.assets.map(a => (
            <button key={a} className="theme-asset-chip mono"
                    onClick={() => {
                      const sig = (window.MA_DATA.heroSignals || []).find(s => s.asset === a);
                      if (sig && onAssetClick) onAssetClick(sig);
                    }}>{a}</button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// SOURCE GRAPH CANVAS — node-link diagram of sources + echo ties.
// ═══════════════════════════════════════════════════════════════
function SourceGraphCanvas({ sources, echoes, themes }) {
  const W = 1100, H = 520;
  const pad = { l: 40, r: 40, t: 30, b: 30 };
  const [hover, setHover] = useStreamsS(null); // source id

  const posOf = (id) => {
    const p = SRC_POS[id] || { x: 0.5, y: 0.5 };
    return {
      x: pad.l + p.x * (W - pad.l - pad.r),
      y: pad.t + p.y * (H - pad.t - pad.b),
    };
  };
  const radOf = (s) => 18 + s.weight * 28; // 18..46

  const themeColor = (id) => {
    const t = themes.find(x => x.id === id);
    if (!t) return "var(--text-mute-3)";
    return DIR_COLOR[t.direction];
  };

  // cluster labels
  const clusters = [
    { x: 0.20, y: 0.05, label: "MACRO · RATES" },
    { x: 0.80, y: 0.05, label: "ENERGY · COMMODITIES" },
    { x: 0.20, y: 0.95, label: "NEWS · SOCIAL" },
    { x: 0.66, y: 0.92, label: "DATA · CRYPTO" },
  ];

  const focusId = hover;
  const involves = (sid) => !focusId || focusId === sid || echoes.some(e => (e.a === focusId && e.b === sid) || (e.b === focusId && e.a === sid));

  return (
    <div className="src-graph">
      <svg viewBox={`0 0 ${W} ${H}`} className="src-graph-svg" preserveAspectRatio="xMidYMid meet">
        <defs>
          <radialGradient id="sg-glow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.4" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
          </radialGradient>
          <pattern id="sg-dots" width="20" height="20" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="0.6" fill="var(--line-soft)" />
          </pattern>
        </defs>
        <rect x={pad.l} y={pad.t} width={W - pad.l - pad.r} height={H - pad.t - pad.b} fill="url(#sg-dots)" opacity="0.4" />

        {/* cluster labels */}
        {clusters.map((c, i) => (
          <text key={i}
                x={pad.l + c.x * (W - pad.l - pad.r)}
                y={pad.t + c.y * (H - pad.t - pad.b)}
                textAnchor="middle"
                fontFamily="var(--mono)" fontSize="10"
                fill="var(--text-mute-3)" letterSpacing="0.22em">
            {c.label}
          </text>
        ))}

        {/* tie strings (echoes) — curved with dotted fade */}
        {echoes.map((e, i) => {
          const pa = posOf(e.a), pb = posOf(e.b);
          const focused = !focusId || e.a === focusId || e.b === focusId;
          const color = themeColor(e.theme);
          // bezier control: perpendicular bulge
          const mx = (pa.x + pb.x) / 2, my = (pa.y + pb.y) / 2;
          const dx = pb.x - pa.x, dy = pb.y - pa.y;
          const len = Math.sqrt(dx*dx + dy*dy) || 1;
          const nx = -dy / len, ny = dx / len;
          const bulge = Math.min(60, len * 0.18);
          const cx = mx + nx * bulge, cy = my + ny * bulge;
          return (
            <g key={i} opacity={focused ? 1 : 0.12}>
              <path d={`M ${pa.x},${pa.y} Q ${cx},${cy} ${pb.x},${pb.y}`}
                    stroke={color}
                    strokeWidth={0.8 + e.weight * 3.6}
                    fill="none"
                    opacity="0.55"
                    strokeLinecap="round" />
              {/* faint overstroke for glow */}
              <path d={`M ${pa.x},${pa.y} Q ${cx},${cy} ${pb.x},${pb.y}`}
                    stroke={color}
                    strokeWidth={(0.8 + e.weight * 3.6) * 2.6}
                    fill="none" opacity="0.10"
                    strokeLinecap="round" />
            </g>
          );
        })}

        {/* source bubbles */}
        {sources.map(s => {
          const { x, y } = posOf(s.id);
          const r = radOf(s);
          const focused = focusId === s.id;
          const dimmed = focusId && !involves(s.id);
          // tier ring color
          const tierColor = s.tier === 1 ? "var(--gold)" : s.tier === 2 ? "var(--green)" : s.tier === 3 ? "var(--amber)" : "var(--text-mute-3)";
          return (
            <g key={s.id}
               transform={`translate(${x}, ${y})`}
               className={`sg-node tier-${s.tier} kind-${s.kind}`}
               style={{ opacity: dimmed ? 0.3 : 1, cursor: "pointer" }}
               onMouseEnter={() => setHover(s.id)}
               onMouseLeave={() => setHover(null)}>
              {/* glow on focus */}
              {focused && <circle r={r + 14} fill="url(#sg-glow)" />}
              {/* outer tier ring */}
              <circle r={r} fill="var(--bg-card)" stroke={tierColor} strokeWidth={s.tier === 1 ? 2.4 : s.tier === 2 ? 1.8 : 1.2} />
              {/* inner weight fill */}
              <circle r={r - 5} fill={tierColor} opacity={0.06 + s.weight * 0.16} />
              {/* tier badge inside */}
              <text textAnchor="middle" y={-r/3 - 1}
                    fontFamily="var(--mono)" fontSize="9"
                    fill={tierColor} letterSpacing="0.16em">T{s.tier}</text>
              {/* source name */}
              <text textAnchor="middle" y={2}
                    fontFamily="var(--mono)" fontSize={r > 30 ? 11 : 10}
                    fill="var(--text)" letterSpacing="0.04em">
                {abbrev(s.name)}
              </text>
              {/* weight */}
              <text textAnchor="middle" y={r/3 + 10}
                    fontFamily="var(--mono)" fontSize="9"
                    fill="var(--text-mute-2)" letterSpacing="0.1em">
                w {s.weight.toFixed(2)}
              </text>
              {/* kind ticker */}
              <text textAnchor="middle" y={r + 14}
                    fontFamily="var(--mono)" fontSize="9"
                    fill="var(--text-mute-3)" letterSpacing="0.18em">
                {s.kind.toUpperCase()}
              </text>
            </g>
          );
        })}
      </svg>

      {/* legend / hover panel */}
      <div className="sg-side">
        {hover ? (
          <SourceHoverCard source={sources.find(s => s.id === hover)} echoes={echoes} themes={themes} sources={sources} />
        ) : (
          <div className="sg-side-hint mono">
            <div className="sg-side-title">HOVER A BUBBLE</div>
            <div className="sg-side-sub">{sources.length} sources · {echoes.length} echo ties</div>
            <ul className="sg-side-list">
              <li><span className="sg-leg-ring tier-1"></span> T1 · primary, high-weight</li>
              <li><span className="sg-leg-ring tier-2"></span> T2 · trusted research</li>
              <li><span className="sg-leg-ring tier-3"></span> T3 · monitored</li>
              <li><span className="sg-leg-ring tier-4"></span> T4 · noise floor</li>
              <li><span className="sg-leg-thread"></span> thread = recurring co-citation</li>
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

function abbrev(name) {
  return name.length <= 14 ? name : name.slice(0, 13) + "…";
}

function SourceHoverCard({ source: s, echoes, themes, sources }) {
  if (!s) return null;
  const links = echoes.filter(e => e.a === s.id || e.b === s.id);
  const tierColor = s.tier === 1 ? "var(--gold)" : s.tier === 2 ? "var(--green)" : s.tier === 3 ? "var(--amber)" : "var(--text-mute-3)";
  return (
    <div className="sg-card">
      <div className="sg-card-head">
        <span className="sg-card-tier mono" style={{ borderColor: tierColor, color: tierColor }}>T{s.tier}</span>
        <div className="sg-card-name">{s.name}</div>
      </div>
      <div className="sg-card-meta mono">
        <span>{s.kind}</span>
        <span className="muted">·</span>
        <span>weight {s.weight.toFixed(2)}</span>
      </div>
      <div className="sg-card-topics">
        {s.topics.map(t => <span key={t} className="src-topic">{t}</span>)}
      </div>
      <div className="sg-card-section">
        <div className="sg-card-section-lbl mono">ECHO TIES · {links.length}</div>
        <div className="sg-card-echoes">
          {links.sort((a,b) => b.weight - a.weight).map((e, i) => {
            const otherId = e.a === s.id ? e.b : e.a;
            const other = sources.find(x => x.id === otherId);
            const theme = themes.find(t => t.id === e.theme);
            return (
              <div key={i} className="sg-card-echo">
                <span className="mono">{other?.name || otherId}</span>
                <span className="sg-card-echo-w mono">w {e.weight.toFixed(2)}</span>
                <span className="sg-card-echo-via">via {theme?.label || e.theme}</span>
              </div>
            );
          })}
          {!links.length && <div className="muted mono">no recurring echoes</div>}
        </div>
      </div>
    </div>
  );
}

// ─── Emerging concept cards (unchanged) ────────────────────────
function EmergingCard({ theme: t, srcById }) {
  return (
    <div className={`emerging-card dir-${t.direction}`}>
      <div className="emerging-eyebrow mono">
        <span>NEW · {t.firstSeenDays}d ago</span>
        <span className="emerging-novelty">novelty {Math.round(t.novelty * 100)}</span>
      </div>
      <div className="emerging-title">{t.label}</div>
      <div className="emerging-blurb">{t.blurb}</div>
      <div className="emerging-stats">
        <div className="emerging-stat">
          <div className="emerging-stat-val mono pos">+{t.velocity.toFixed(2)}</div>
          <div className="emerging-stat-lbl mono">velocity</div>
        </div>
        <div className="emerging-stat">
          <div className="emerging-stat-val mono">{t.items}</div>
          <div className="emerging-stat-lbl mono">items</div>
        </div>
        <div className="emerging-stat">
          <div className="emerging-stat-val mono">{t.sources.length}</div>
          <div className="emerging-stat-lbl mono">sources</div>
        </div>
      </div>
      <div className="emerging-spark">
        <Sparkline series={t.trace} width={220} height={28} />
      </div>
      <div className="emerging-foot mono">
        {t.sources.slice(0, 3).map(s => (
          <span key={s.id} className="emerging-src">{srcById[s.id]?.name || s.id}</span>
        ))}
        {t.sources.length > 3 && <span className="emerging-src muted">+{t.sources.length - 3}</span>}
      </div>
    </div>
  );
}

Object.assign(window, { Streams });
