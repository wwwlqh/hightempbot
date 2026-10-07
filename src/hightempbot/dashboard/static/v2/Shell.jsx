/* eslint-disable */
const SIDEBAR_ICONS = {
  overview: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg>,
  performance: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/></svg>,
  trades: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18"/><path d="M3 12h18"/><path d="M3 18h12"/></svg>,
  stations: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21 10c0 7-9 13-9 13S3 17 3 10a9 9 0 1118 0z"/><circle cx="12" cy="10" r="3"/></svg>,
  calibration: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3v9l6 3"/></svg>,
  calendar: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>,
  operator: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7l8-4z"/><path d="M9 12l2 2 4-5"/></svg>,
  models: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>,
  risk: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 9v4M12 17h.01"/><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>,
  strategy: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/></svg>,
};

function Sidebar({ page, setPage, d }) {
  const items = [
    { key: "overview", label: "Overview" },
    { key: "performance", label: "Performance" },
    { key: "trades", label: "Trade journal", count: Number(d.todayBets || 0) },
    { key: "strategy", label: "Strategy", count: d.strategiesConfig ? Object.keys(d.strategiesConfig).filter(k => !k.startsWith("__")).length : null },
    { key: "operator", label: "Operator", count: d.operator && d.operator.state !== "LIVE" ? 1 : null },
    { key: "calendar", label: "Calendar" },
    { key: "calibration", label: "Calibration" },
    { key: "models", label: "Forecast models" },
    { key: "stations", label: "Stations", count: d.stations.length },
  ];
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">H</div>
        <div>
          <div className="brand-name">HighTempBot</div>
          <div className="brand-sub">Weather trading journal</div>
        </div>
      </div>
      <div className="nav-section">
        <div className="nav-heading">Workspace</div>
        {items.map(it => (
          <div key={it.key} className={"nav-item" + (page === it.key ? " active" : "")} onClick={() => setPage(it.key)}>
            {SIDEBAR_ICONS[it.key]}
            <span>{it.label}</span>
            {it.count != null && <span className="nav-count">{it.count}</span>}
          </div>
        ))}
      </div>
      <div className="sidebar-footer">
        <span className="health-dot" />
        <span>Bot online · {d.uptime}</span>
      </div>
    </aside>
  );
}

function Topbar({ title, sub, mode, range, setRange }) {
  return (
    <div className="topbar">
      <div>
        <div className="topbar-title">{title}</div>
        {sub && <div className="topbar-sub">{sub}</div>}
      </div>
      <div className="topbar-spacer" />
      <span className={"badge " + (mode === "LIVE" ? "badge-live" : "badge-dry")}>{mode}</span>
      {setRange && (
        <div className="seg">
          {["7d", "30d", "All"].map(r => (
            <button key={r} className={range === r ? "active" : ""} onClick={() => setRange(r)}>{r}</button>
          ))}
        </div>
      )}
    </div>
  );
}

function HaltedBanner({ ddPct, halt }) {
  // 2026-05-20: halt-on-DD replaced the prior halve-on-DD rule. No reduced
  // band anymore — banner is binary (off / HALTED).
  const isHalted = ddPct >= halt;
  if (!isHalted) return null;
  return (
    <div className="halt-banner halt">
      <div className="halt-left">
        <span className="halt-dot" />
        <span className="halt-status">HALTED</span>
        <span className="halt-msg">
          Drawdown <strong>{ddPct.toFixed(1)}%</strong> exceeded halt threshold <strong>{halt}%</strong>. All new bets paused until capital recovers above threshold.
        </span>
      </div>
      <div className="halt-bar">
        <div className="halt-bar-track">
          <div className="halt-bar-fill halt" style={{ width: `${Math.min(100, (ddPct / halt) * 100)}%` }} />
        </div>
        <div className="halt-bar-labels">
          <span>0%</span>
          <span>halt {halt}%</span>
        </div>
      </div>
    </div>
  );
}

window.Sidebar = Sidebar; window.Topbar = Topbar; window.HaltedBanner = HaltedBanner;
