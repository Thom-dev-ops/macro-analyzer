// App shell + routing.

const { useState: useS, useMemo: useM, useEffect: useE } = React;

function App() {
  // view: "positioning" | "journal" | "dev" | "asset"
  const [view, setView] = useS("positioning");
  const [assetSig, setAssetSig] = useS(null);
  // remember which top-level view to return to from an asset page
  const [returnTo, setReturnTo] = useS("positioning");

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

  // Tweaks
  const [tw, setTweak] = useTweaks(/*EDITMODE-BEGIN*/{
    "accent": "blue",
    "density": "default",
    "theme": "light",
    "showMobile": true
  }/*EDITMODE-END*/);

  useE(() => {
    document.documentElement.setAttribute("data-accent", tw.accent);
    document.documentElement.setAttribute("data-density", tw.density);
    document.documentElement.setAttribute("data-theme", tw.theme);
  }, [tw.accent, tw.density, tw.theme]);

  const D = window.MA_DATA;

  const NAV = [
    { id: "positioning", label: "Positioning", icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="7.5"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"/><circle cx="12" cy="12" r="2.3"/></svg> },
    { id: "journal", label: "Journal", icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M6 4.5h10a2 2 0 0 1 2 2v13H8a2 2 0 0 1-2-2Z"/><path d="M6 17.5h12"/><path d="M9.5 9h5M9.5 12.5h5"/></svg> },
    { id: "streams", label: "Streams", icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="6" cy="12" r="2.3"/><circle cx="18" cy="6" r="2.3"/><circle cx="18" cy="18" r="2.3"/><path d="M8.1 10.9 15.9 7.1M8.1 13.1 15.9 16.9"/></svg> },
    { id: "dev", label: "Dev", icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M8.5 8 5 12l3.5 4M15.5 8 19 12l-3.5 4"/></svg> },
  ];
  const SECTIONS = { positioning: ["Live desk", "Positioning"], journal: ["Review", "Journal"], streams: ["Discovery", "Streams"], dev: ["System", "Dev"] };
  const sec = view === "asset" ? ["Signal", assetSig ? assetSig.asset : "Asset"] : (SECTIONS[view] || ["", ""]);

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
            <button key={n.id} className={view === n.id ? "on" : ""} onClick={() => { setView(n.id); setAssetSig(null); }}>
              {n.icon}<span>{n.label}</span>
            </button>
          ))}
        </nav>
        <div className="rail-spacer"></div>
        <div className="rail-foot">
          <div className="rail-regime">
            <span className="rail-regime-dot"></span>
            <div>
              <div className="rail-regime-label">{D.regime.framework.label}</div>
              <div className="rail-regime-conf">{Math.round(D.regime.framework.confidence * 100)}% confidence · 14d</div>
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
            <span className="rail-status"><span className="dot-live"></span> LIVE · 26 sources</span>
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
              <span className="tb-clock-time">14:23 ET</span>
              <span className="tb-clock-day">FRI · MAY 22</span>
            </div>
          </div>
        </header>

      {view === "positioning" && <KpiStrip />}
      {view === "positioning" && (
        <>
          <Positioning
            onOpenReasoning={goAsset}
            onOpenTradeForm={() => {}}
          />
          {tw.showMobile && (
            <section className="block">
              <header className="block-head sm">
                <div className="block-title">
                  <span className="block-num mono">P9</span>
                  <span>Mobile preview · /positioning</span>
                  <span className="block-sub">phone subset · same data, same grammar</span>
                </div>
              </header>
              <div className="mobile-preview-wrap">
                <MobilePreview />
              </div>
            </section>
          )}
        </>
      )}
      {view === "journal" && <Journal />}
      {view === "streams" && <Streams onOpenAsset={goAsset} />}
      {view === "dev" && <Dev />}
      {view === "asset" && assetSig && (
        <AssetPage signal={assetSig} onBack={goBack} returnTo={returnTo} />
      )}

      <footer className="app-foot">
        <span>Macro Analyzer · Internal</span>
        <span>5 agents online · synced 14:23 ET</span>
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
        <TweakSection label="Mobile preview">
          <TweakToggle
            label="Show on positioning"
            value={tw.showMobile}
            onChange={(v) => setTweak("showMobile", v)}
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
