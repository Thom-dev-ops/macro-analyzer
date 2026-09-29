// Mobile preview frame for /positioning — phone-friendly subset.

function MobilePreview() {
  const D = window.MA_DATA;
  const top3 = D.heroSignals.slice(0, 3);
  const top4 = D.activeTrades.slice(0, 4);
  const f = D.regime.framework;

  return (
    <div className="phone-frame">
      <div className="phone-bezel">
        <div className="phone-notch"></div>
        <div className="phone-screen">
          {/* status */}
          <div className="ph-status">
            <span className="mono">9:41</span>
            <span className="ph-status-right mono">●●●●● ◌ 76%</span>
          </div>
          {/* app bar */}
          <div className="ph-appbar">
            <div className="ph-tabs">
              <span className="ph-tab on">Positioning</span>
              <span className="ph-tab">Journal</span>
              <span className="ph-tab">Dev</span>
            </div>
          </div>
          {/* regime tape compact */}
          <div className="ph-regime">
            <div className="ph-regime-head">
              <span className="ph-regime-dot" style={{ background: "var(--gold)" }}></span>
              <span className="ph-regime-label">{f.label.toUpperCase()}</span>
              <span className="ph-regime-conf mono gold">{Math.round(f.confidence * 100)}%</span>
            </div>
            <div className="ph-regime-spark">
              <Sparkline data={D.regime.confidenceTrace} width={300} height={22} color="var(--gold)" marker />
            </div>
            <div className="ph-regime-meta mono muted">
              active {f.sinceDays}d · bias real-asset · ×{f.sizingModifier.toFixed(2)} size
            </div>
          </div>
          {/* hero signals */}
          <div className="ph-section-head">
            <span>HERO SIGNALS</span>
            <span className="muted mono">≥75 · {top3.filter(s => s.score >= 75).length}/3</span>
          </div>
          <div className="ph-cards">
            {top3.map(s => (
              <div key={s.id} className={`ph-card tier-${s.tier}`}>
                <div className="ph-card-stripe" style={{ background: s.tier === 1 ? "var(--gold)" : s.tier === 2 ? "var(--green)" : "var(--amber)" }}></div>
                <div className="ph-card-top">
                  <div>
                    <div className="ph-asset mono">{s.asset}</div>
                    <div className="ph-meta">
                      <SideLabel side={s.side} />
                      <span className="ph-setup">{s.setup}</span>
                    </div>
                  </div>
                  <ScoreChip score={s.score} prev={s.scorePrev} size="sm" />
                </div>
                <div className="ph-levels">
                  <span><span className="muted mono">E</span> <span className="mono">{s.entry < 1000 ? s.entry.toFixed(2) : s.entry.toLocaleString()}</span></span>
                  <span><span className="muted mono">S</span> <span className="mono red">{s.stop < 1000 ? s.stop.toFixed(2) : s.stop.toLocaleString()}</span></span>
                  <span><span className="muted mono">T</span> <span className="mono green">{s.target < 1000 ? s.target.toFixed(2) : s.target.toLocaleString()}</span></span>
                  <span><span className="muted mono">R</span> <span className="mono">{s.rr.toFixed(2)}</span></span>
                </div>
                <div className="ph-card-why">{s.whyNow[0]}</div>
              </div>
            ))}
          </div>
          {/* active trades compact */}
          <div className="ph-section-head">
            <span>ACTIVE TRADES</span>
            <span className="muted mono">{D.activeTrades.length}</span>
          </div>
          <div className="ph-trades">
            {top4.map(t => (
              <div key={t.id} className="ph-trade">
                <span className="mono ph-trade-asset">{t.asset}</span>
                <SideLabel side={t.side} />
                <span className={`mono ph-trade-pnl ${t.pnlPct >= 0 ? "pos" : "neg"}`}>
                  {t.pnlPct >= 0 ? "+" : ""}{t.pnlPct.toFixed(2)}%
                </span>
                <span className="mono muted ph-trade-age">{t.ageDays}d</span>
              </div>
            ))}
          </div>
          {/* outcome cta */}
          <div className="ph-cta">
            <button className="ph-cta-btn">＋ Log close on phone</button>
          </div>
        </div>
        <div className="phone-home"></div>
      </div>
    </div>
  );
}

Object.assign(window, { MobilePreview });
