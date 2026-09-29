// App shell + routing — sidebar rail + calm canvas (design-folder shell).

const { useState: useS, useMemo: useM, useEffect: useE } = React;

// Live wall-clock for the page head. Updates every 30s; uses ET (America/New_York)
// since that's the trading session reference. Falls back to local tz if Intl
// can't resolve the zone (older browsers/restrictive environments).
function useLiveClock() {
  const [now, setNow] = React.useState(new Date());
  React.useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 30 * 1000);
    return () => clearInterval(t);
  }, []);
  let timeStr = "", dayStr = "";
  try {
    timeStr = new Intl.DateTimeFormat("en-US", {
      hour: "2-digit", minute: "2-digit", hour12: false,
      timeZone: "America/New_York",
    }).format(now) + " ET";
    const dow = new Intl.DateTimeFormat("en-US", {
      weekday: "short", timeZone: "America/New_York",
    }).format(now).toUpperCase();
    const md = new Intl.DateTimeFormat("en-US", {
      month: "short", day: "numeric", timeZone: "America/New_York",
    }).format(now).toUpperCase();
    dayStr = `${dow} · ${md}`;
  } catch (e) {
    timeStr = now.toLocaleTimeString();
    dayStr = now.toDateString();
  }
  return { timeStr, dayStr };
}

// Side-panel nav: top-level groups; the funnel + discovery groups expand
// into subsections when active (or when a child view is active).
const NAV = [
  {
    id: "home", label: "Home",
    icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M4 11.5 12 4.5l8 7"/><path d="M6 10.5V19h12v-8.5"/><path d="M10 19v-5h4v5"/></svg>,
  },
  {
    id: "positioning", label: "Positioning",
    icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="7.5"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"/><circle cx="12" cy="12" r="2.3"/></svg>,
    children: [
      { id: "positioning", label: "Desk",      num: "P" },
      { id: "concepts",    label: "Concepts",  num: "C" },
      { id: "identify",    label: "Identify",  num: "I" },
      { id: "live",        label: "Live",      num: "L" },
    ],
  },
  {
    id: "streams", label: "Intelligence",
    icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="6" cy="12" r="2.3"/><circle cx="18" cy="6" r="2.3"/><circle cx="18" cy="18" r="2.3"/><path d="M8.1 10.9 15.9 7.1M8.1 13.1 15.9 16.9"/></svg>,
    children: [
      { id: "streams",   label: "Theme trends", num: "U1" },
      { id: "sources",   label: "Sources",      num: "U2" },
      { id: "influence", label: "Influence",    num: "U2b" },
      { id: "inbox",     label: "Manual input", num: "U3" },
      { id: "chartlab",  label: "Chart lab",    num: "U4" },
    ],
  },
  {
    id: "paper", label: "Paper trading",
    icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M3.5 17.5 9 11l3.5 3.5L20.5 6"/><path d="M15.5 6h5v5"/><path d="M3.5 20.5h17"/></svg>,
    // Three books, one section. Each trades a different source on the
    // same engine: the model's own signal stack, the Feather Hands
    // cohort's calls as they are made, and Stock Unlocked's text calls.
    children: [
      { id: "paper",    label: "Macro Analyzer Model", num: "T1" },
      { id: "cohort",   label: "FeatherHands",         num: "T2" },
      { id: "unlocked", label: "Stock Unlocked",       num: "T3" },
    ],
  },
  {
    id: "journal", label: "Journal",
    icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M6 4.5h10a2 2 0 0 1 2 2v13H8a2 2 0 0 1-2-2Z"/><path d="M6 17.5h12"/><path d="M9.5 9h5M9.5 12.5h5"/></svg>,
  },
  {
    id: "dev", label: "Dev",
    icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M8.5 8 5 12l3.5 4M15.5 8 19 12l-3.5 4"/></svg>,
  },
];

// Which top-level group owns each view (for highlighting + expansion).
const GROUP_OF = {
  home: "home",
  positioning: "positioning", concepts: "positioning", identify: "positioning", live: "positioning",
  journal: "journal",
  paper: "paper", cohort: "paper", unlocked: "paper",
  streams: "streams", sources: "streams", influence: "streams", inbox: "streams", chartlab: "streams",
  dev: "dev",
};

// Page-head eyebrow + title per view.
const SECTIONS = {
  home:        ["Macro tape", "Home"],
  positioning: ["Live desk", "Positioning"],
  concepts:    ["Funnel", "Concepts"],
  identify:    ["Funnel", "Identify"],
  live:        ["Funnel", "Live trades"],
  journal:     ["Review", "Journal"],
  paper:       ["Paper trading", "Macro Analyzer Model"],
  cohort:      ["Paper trading", "FeatherHands"],
  unlocked:    ["Paper trading", "Stock Unlocked"],
  streams:     ["Intelligence", "Theme trends"],
  sources:     ["Intelligence", "Sources"],
  influence:   ["Intelligence", "Influence"],
  inbox:       ["Intelligence", "Manual input"],
  chartlab:    ["Intelligence", "Chart lab"],
  dev:         ["System", "Dev"],
};

function App() {
  const [view, setView] = useS("home");
  const [assetSig, setAssetSig] = useS(null);
  // remember which top-level view to return to from an asset page
  const [returnTo, setReturnTo] = useS("positioning");
  const clock = useLiveClock();

  // Cross-page focus: when /concepts promotes a concept, /identify
  // jumps to that draft plan; when /identify activates a plan, /live
  // jumps to the newly opened trade. Cleared on the next manual nav.
  const [focusPlanId, setFocusPlanId] = useS(null);
  const [focusTradeId, setFocusTradeId] = useS(null);
  const nav = (v) => { setFocusPlanId(null); setFocusTradeId(null); setAssetSig(null); setView(v); };

  const goAsset = (sig) => {
    setReturnTo(view === "asset" ? returnTo : view);
    setAssetSig(sig);
    setView("asset");
    window.scrollTo({ top: 0, behavior: "instant" });
  };
  const goBack = () => {
    setView(returnTo || "positioning");
    setAssetSig(null);
  };

  // Cross-view nav from /manual input: when a user clicks a chart's
  // ticker in the analyzed extraction, we dispatch macro:open-asset
  // → open the asset detail page if MA_DATA has a matching signal.
  useE(() => {
    function onOpenAsset(e) {
      const wanted = String(e.detail?.ticker || "").toUpperCase().split("/")[0];
      try {
        const D = window.MA_DATA || {};
        const pools = [
          ...(D.signals || []),
          ...(D.heroSignals || []),
          ...(D.watchlist?.tickers || []),
          ...(D.watchlist || []),
        ];
        const match = pools.find(p =>
          (p.asset || p.ticker || p.symbol || "")
            .toString().toUpperCase().split("/")[0] === wanted
        );
        if (match && (match.id || match.asset)) {
          goAsset(match);
          return;
        }
      } catch (_) { /* best-effort */ }
      nav("positioning");
    }
    window.addEventListener("macro:open-asset", onOpenAsset);
    return () => window.removeEventListener("macro:open-asset", onOpenAsset);
  }, [view, returnTo]);

  // Tweaks
  const [tw, setTweak] = useTweaks(/*EDITMODE-BEGIN*/{
    "accent": "blue",
    "density": "default",
    "theme": "light"
  }/*EDITMODE-END*/);

  useE(() => {
    document.documentElement.setAttribute("data-accent", tw.accent);
    document.documentElement.setAttribute("data-density", tw.density);
    document.documentElement.setAttribute("data-theme", tw.theme);
  }, [tw.accent, tw.density, tw.theme]);

  const D = window.MA_DATA;
  const group = view === "asset" ? GROUP_OF[returnTo] : GROUP_OF[view];
  const sec = view === "asset"
    ? ["Signal", assetSig ? assetSig.asset : "Asset"]
    : (SECTIONS[view] || ["", ""]);

  return (
    <div className="app">
      <aside className="rail">
        <div className="rail-brand">
          <div className="brand-mark">M</div>
          <div className="rail-brand-text">
            <div className="brand-name">Macro Analyzer</div>
            <div className="brand-sub">Positioning desk</div>
          </div>
        </div>
        <nav className="rail-nav">
          {NAV.map((n) => (
            <React.Fragment key={n.id}>
              <button className={group === n.id ? "on" : ""} onClick={() => nav(n.id)}>
                {n.icon}<span>{n.label}</span>
              </button>
              {n.children && group === n.id && (
                <div className="rail-sub">
                  {n.children.map((c) => (
                    <button key={c.id} className={view === c.id ? "on" : ""} onClick={() => nav(c.id)}>
                      <span className="rail-sub-num">{c.num}</span>
                      <span>{c.label}</span>
                    </button>
                  ))}
                </div>
              )}
            </React.Fragment>
          ))}
        </nav>
        <div className="rail-spacer"></div>
        <div className="rail-foot">
          <div className="rail-regime">
            <span className="rail-regime-dot"></span>
            <div>
              <div className="rail-regime-label">{D.regime.framework.label}</div>
              <div className="rail-regime-conf">{Math.round(D.regime.framework.confidence * 100)}% confidence · {D.regime.framework.sinceDays}d</div>
            </div>
          </div>
          <div className="rail-foot-row">
            <button
              className="theme-toggle"
              onClick={() => setTweak("theme", tw.theme === "light" ? "dark" : "light")}
              title="Toggle light / dark"
              aria-label="Toggle light or dark theme"
            >
              {tw.theme === "light" ? (
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.9A9 9 0 1 1 11.1 3a7 7 0 0 0 9.9 9.9Z"/></svg>
              ) : (
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2.4M12 19.6V22M2 12h2.4M19.6 12H22M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M19.1 4.9l-1.7 1.7M6.6 17.4l-1.7 1.7"/></svg>
              )}
            </button>
            <span className="rail-status"><span className="dot-live"></span> LIVE · {(D.sourceHealth || []).length || 26} sources</span>
          </div>
        </div>
      </aside>

      <main className="canvas">
        <header className="page-head">
          <div>
            <div className="page-eyebrow">{sec[0]}</div>
            <h1 className="page-title">{sec[1]}</h1>
          </div>
          <div className="page-head-right">
            <div className="tb-clock">
              <span className="tb-clock-time">{clock.timeStr}</span>
              <span className="tb-clock-day">{clock.dayStr}</span>
            </div>
          </div>
        </header>

        {view !== "asset" && group === "positioning" && (
          <FunnelRail view={view} onNav={nav} />
        )}

        {view === "positioning" && <KpiStrip />}
        {view === "positioning" && (
          <Positioning
            onOpenReasoning={goAsset}
            onOpenTradeForm={() => {}}
            onAdvanceToConcept={() => nav("concepts")}
          />
        )}
        {view === "concepts" && (
          <Concepts
            onPromote={(planId) => { setFocusPlanId(planId); setView("identify"); }}
          />
        )}
        {view === "identify" && (
          <Identify
            focusPlanId={focusPlanId}
            onActivated={(tradeId) => { setFocusTradeId(tradeId); setView("live"); }}
          />
        )}
        {view === "home" && <Home onNav={nav} />}
        {view === "live" && <Live focusTradeId={focusTradeId} />}
        {view === "journal" && <Journal />}
        {view === "paper" && <Paper />}
        {view === "cohort" && <Cohort />}
        {view === "unlocked" && <Unlocked />}
        {view === "sources" && <Sources />}
        {view === "streams" && <Streams />}
        {view === "dev" && <Dev />}
        {view === "inbox" && <Inbox />}
        {view === "chartlab" && <ChartLab />}
        {view === "influence" && <Influence />}
        {view === "asset" && assetSig && (
          <AssetPage
            signal={assetSig}
            onBack={goBack}
            returnTo={returnTo}
            onPromote={(planId) => { setFocusPlanId(planId); setAssetSig(null); setView("identify"); }}
            onNav={nav}
          />
        )}

        <footer className="app-foot">
          <span>Macro Analyzer · Internal · L. Pirola</span>
          <span>git@macro:hl-research · 5 agents online</span>
        </footer>
      </main>

      <TweaksPanel title="Tweaks">
        <TweakSection label="Theme">
          <TweakRadio
            label="Appearance"
            value={tw.theme}
            options={["light", "dark"]}
            onChange={(v) => setTweak("theme", v)}
          />
        </TweakSection>
        <TweakSection label="Accent">
          <TweakRadio
            label="Accent color"
            value={tw.accent}
            options={["blue", "gold", "green", "violet", "amber"]}
            onChange={(v) => setTweak("accent", v)}
          />
        </TweakSection>
        <TweakSection label="Density">
          <TweakRadio
            label="Row density"
            value={tw.density}
            options={["compact", "default", "cozy"]}
            onChange={(v) => setTweak("density", v)}
          />
        </TweakSection>
      </TweaksPanel>
    </div>
  );
}

function KpiStrip() {
  const k = window.MA_DATA.kpis;
  return (
    <div className="kpi-strip">
      <div className="kpi">
        <div className="kpi-lbl">Cash posture</div>
        <div className="kpi-val">{k.cashPosture.label}</div>
        <div className="kpi-sub">
          {k.cashPosture.pct}% cash <span className={k.cashPosture.delta >= 0 ? "pos" : "neg"}>
            {k.cashPosture.delta >= 0 ? "+" : ""}{k.cashPosture.delta}pp
          </span>
        </div>
      </div>
      <div className="kpi">
        <div className="kpi-lbl">Active trades</div>
        <div className="kpi-val">{k.activeTrades.count}</div>
        <div className="kpi-sub">${(k.activeTrades.exposureUsd / 1000).toFixed(1)}k exposure</div>
      </div>
      <div className="kpi">
        <div className="kpi-lbl">P&amp;L today</div>
        <div className={`kpi-val ${k.pnlToday.usd >= 0 ? "pos" : "neg"}`}>
          {k.pnlToday.usd >= 0 ? "+" : ""}${(k.pnlToday.usd / 1000).toFixed(2)}k
        </div>
        <div className={`kpi-sub ${k.pnlToday.pct >= 0 ? "pos" : "neg"}`}>
          {k.pnlToday.pct >= 0 ? "+" : ""}{k.pnlToday.pct.toFixed(2)}%
        </div>
      </div>
      <div className="kpi">
        <div className="kpi-lbl">P&amp;L 7d</div>
        <div className={`kpi-val ${k.pnlWeek.usd >= 0 ? "pos" : "neg"}`}>
          {k.pnlWeek.usd >= 0 ? "+" : ""}${(k.pnlWeek.usd / 1000).toFixed(2)}k
        </div>
        <div className={`kpi-sub ${k.pnlWeek.pct >= 0 ? "pos" : "neg"}`}>
          {k.pnlWeek.pct >= 0 ? "+" : ""}{k.pnlWeek.pct.toFixed(2)}%
        </div>
      </div>
      <div className="kpi">
        <div className="kpi-lbl">Signals ≥ 75</div>
        <div className="kpi-val gold">{k.signalsHigh.count}</div>
        <div className="kpi-sub">
          <span className={k.signalsHigh.deltaVsYesterday >= 0 ? "pos" : "neg"}>
            {k.signalsHigh.deltaVsYesterday >= 0 ? "+" : ""}{k.signalsHigh.deltaVsYesterday}
          </span> vs yesterday
        </div>
      </div>
      <div className="kpi">
        <div className="kpi-lbl">Spend today</div>
        <div className="kpi-val">${k.spendToday.usd.toFixed(2)}</div>
        <div className="kpi-sub">cap ${k.spendToday.capUsd}</div>
        <div className="kpi-meter"><i style={{ width: `${(k.spendToday.usd / k.spendToday.capUsd) * 100}%` }}></i></div>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
