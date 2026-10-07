/* eslint-disable */
function fmtPct(v, digits = 1) {
  return v == null || Number.isNaN(Number(v)) ? "-" : `${Number(v).toFixed(digits)}%`;
}

function fmtUsd(v) {
  return v == null || Number.isNaN(Number(v)) ? "-" : `$${Number(v).toFixed(2)}`;
}

function fmtPx(v) {
  return v == null || Number.isNaN(Number(v)) ? "-" : Number(v).toFixed(4);
}

function fmtFillLevels(levels) {
  if (!Array.isArray(levels) || levels.length === 0) return "-";
  return levels.map((l) => `${fmtUsd(l.usd)} @ ${fmtPx(l.price)}`).join(", ");
}

function fmtSignedUsd(v) {
  if (v == null || Number.isNaN(Number(v))) return "-";
  const n = Number(v);
  return `${n >= 0 ? "+$" : "-$"}${Math.abs(n).toFixed(2)}`;
}

function TradeSizeCell({ p }) {
  const hasTarget = p.targetSize != null && !Number.isNaN(Number(p.targetSize));
  return (
    <td className="num">
      <div className="trade-size">
        <span>{fmtUsd(p.size)}</span>
        {hasTarget && <span className="trade-target">/ {fmtUsd(p.targetSize)} target</span>}
      </div>
    </td>
  );
}

function OpenValueCell({ p }) {
  const hasApi = p.apiCurrentValue != null && !Number.isNaN(Number(p.apiCurrentValue));
  const pnl = p.apiCashPnl;
  return (
    <td className="num">
      <div className="trade-size">
        <span>{hasApi ? fmtUsd(p.apiCurrentValue) : "-"}</span>
        {pnl != null && !Number.isNaN(Number(pnl)) && (
          <span className={(Number(pnl) >= 0 ? "pos" : "neg") + " trade-target"}>{fmtSignedUsd(pnl)}</span>
        )}
      </div>
    </td>
  );
}

function TradeFillDetails({ fills = [], resolved }) {
  return (
    <tr className="trade-detail-row">
      <td colSpan={resolved ? 13 : 11}>
        <div className="trade-detail">
          <table className="tbl fill-table">
            <thead><tr>
              <th>Open time</th><th>Fill time</th>
              <th className="num">Fill px</th><th className="num">Entry Edge</th><th className="num">Realized</th>
              <th className="num">Size</th><th className="num">Shares</th><th>Fill depth</th>
              <th className="num">Limit</th><th>Order</th>{resolved && <th className="num">P&L</th>}
            </tr></thead>
            <tbody>
              {fills.map((f, i) => (
                <tr key={f.ledgerId || i}>
                  <td className="mono">{f.ts || "-"}</td>
                  <td className="mono">{f.fillTs || "-"}</td>
                  <td className="num">{fmtPx(f.fillPrice)}</td>
                  <td className="num">{fmtPct(f.edge)}</td>
                  <td className={"num " + (f.realizedEdge != null && f.realizedEdge >= 0 ? "pos" : f.realizedEdge != null ? "neg" : "muted")}>{fmtPct(f.realizedEdge)}</td>
                  <td className="num">{fmtUsd(f.size)}</td>
                  <td className="num">{f.fillSize == null ? "-" : Number(f.fillSize).toFixed(2)}</td>
                  <td className="mono fill-levels">{fmtFillLevels(f.priceLevels)}</td>
                  <td className="num">{fmtPx(f.limit)}</td>
                  <td className="mono muted">{f.orderId ? String(f.orderId).slice(0, 12) : "-"}</td>
                  {resolved && <td className={"num " + ((f.pnl || 0) >= 0 ? "pos" : "neg")}>{fmtSignedUsd(f.pnl)}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </td>
    </tr>
  );
}

function OpenTradeRow({ p, expanded, onToggle }) {
  const multi = (p.fillCount || 0) > 1;
  return (
    <React.Fragment>
      <tr className={"trade-row" + (expanded ? " expanded" : "")} onClick={onToggle}>
        <td className="mono">
          <button className="row-toggle" aria-expanded={expanded} aria-label="Toggle fill details" onClick={(e) => { e.stopPropagation(); onToggle(); }}>{expanded ? "-" : "+"}</button>
          {p.ts}
        </td>
        <td><span className="flag">{p.flag}</span><strong>{p.id}</strong> <span className="muted">· {p.city}</span></td>
        <td className="mono">{p.target.slice(5)}</td>
        <td>{p.bracket} {multi && <span className="fill-count">{p.fillCount} fills</span>}</td>
        <td><span className={"pill " + (p.side === "YES" ? "pill-green" : "pill-red")}>{p.side}</span></td>
        <td className="num">{fmtPct(p.fill)}</td>
        <td className="num pos">{p.edge == null ? "-" : "+" + fmtPct(p.edge)}</td>
        <td className={"num " + (p.realizedEdge != null && p.realizedEdge >= 0 ? "pos" : p.realizedEdge != null ? "neg" : "muted")}>{fmtPct(p.realizedEdge)}</td>
        <TradeSizeCell p={p} />
        <OpenValueCell p={p} />
        <td>
          <span className={"pill " + (p.apiConfirmed ? "pill-blue" : p.apiStatus === "API_MISMATCH" ? "pill-yellow" : "pill-muted")}>
            {p.apiConfirmed ? "API" : p.apiStatus === "API_MISMATCH" ? "CHECK" : "WAIT"}
          </span>
          {p.apiReminder && <div className="muted" style={{fontSize: 10, marginTop: 3}}>{p.apiReminder}</div>}
        </td>
      </tr>
      {expanded && <TradeFillDetails fills={p.fills} resolved={false} />}
    </React.Fragment>
  );
}

function ResolvedTradeRow({ p, expanded, onToggle }) {
  const multi = (p.fillCount || 0) > 1;
  return (
    <React.Fragment>
      <tr className={"trade-row" + (expanded ? " expanded" : "")} onClick={onToggle}>
        <td className="mono">
          <button className="row-toggle" aria-expanded={expanded} aria-label="Toggle fill details" onClick={(e) => { e.stopPropagation(); onToggle(); }}>{expanded ? "-" : "+"}</button>
          {p.ts}
        </td>
        <td><span className="flag">{p.flag}</span><strong>{p.id}</strong> <span className="muted">· {p.city}</span></td>
        <td className="mono">{p.target.slice(5)}</td>
        <td>{p.bracket} {multi && <span className="fill-count">{p.fillCount} fills</span>}</td>
        <td><span className={"pill " + (p.side === "YES" ? "pill-green" : "pill-red")}>{p.side}</span></td>
        <td className="num">{fmtPct(p.fill)}</td>
        <td className="num">{fmtPct(p.edge)}</td>
        <td className={"num " + (p.realizedEdge != null && p.realizedEdge >= 0 ? "pos" : p.realizedEdge != null ? "neg" : "muted")}>{fmtPct(p.realizedEdge)}</td>
        <td className="num">{fmtUsd(p.size)}</td>
        <td className="mono">{p.actual}</td>
        <td><span className={"pill " + (p.outcome === "WIN" || (p.outcome === "CLOSED" && (p.pnl || 0) > 0) ? "pill-green" : p.outcome === "MIXED" ? "pill-yellow" : "pill-red")}>{p.outcome}</span></td>
        <td className={"num " + (p.pnl >= 0 ? "pos" : "neg")}>{fmtSignedUsd(p.pnl)}</td>
        <td></td>
      </tr>
      {expanded && <TradeFillDetails fills={p.fills} resolved={true} />}
    </React.Fragment>
  );
}

function TradesPage({ d, range, setRange }) {
  // Window-scoped realized KPIs come from backend-computed aggregates. The
  // Net P&L card intentionally excludes unrealized Data API open marks.
  // The resolvedPositionsList below is
  // LIMIT-50 truncated for table-paint performance; computing KPIs from it
  // silently drops the oldest tail of the window and produces wrong totals
  // (see fix(dashboard) for the +$30 vs +$38 incident).
  const open = d.openPositionsList || [];
  const resolved = d.resolvedPositionsList || [];
  const [expandedKey, setExpandedKey] = React.useState(null);
  const toggleRow = (key) => setExpandedKey(expandedKey === key ? null : key);
  const RESOLVED_PAGE_SIZE = 20;
  const [resolvedPage, setResolvedPage] = React.useState(0);
  const resolvedPageCount = Math.max(1, Math.ceil(resolved.length / RESOLVED_PAGE_SIZE));
  const clampedResolvedPage = Math.min(resolvedPage, resolvedPageCount - 1);
  React.useEffect(() => {
    if (resolvedPage !== clampedResolvedPage) setResolvedPage(clampedResolvedPage);
  }, [resolvedPage, clampedResolvedPage]);
  const resolvedStart = clampedResolvedPage * RESOLVED_PAGE_SIZE;
  const resolvedEnd = Math.min(resolvedStart + RESOLVED_PAGE_SIZE, resolved.length);
  const pagedResolved = resolved.slice(resolvedStart, resolvedEnd);
  const settledW = (d.wins || 0) + (d.losses || 0);
  const winRateW = d.winRate || 0;
  const netPnlW = Number(d.realizedPnl ?? d.totalPnl ?? 0);
  // avgEdge can be 0 (valid: no edge across resolved bets) or absent (old
  // server payload pre-this-merge). Don't conflate them with `|| 0`.
  const avgEdge = d.avgEdge;
  const avgEdgePresent = avgEdge != null;
  return (
    <div className="content" data-screen-label="Trades">
      <div className="kpi-grid">
        <KpiCell label="Open" value={open.length} sub={`$${d.pendingExposure.toFixed(2)} exposure`} />
        <KpiCell label="Today" value={d.todayBets} sub={`$${d.todayVolume.toFixed(2)} volume`} />
        <KpiCell label="Resolved (window)" value={d.resolvedCount || 0} sub={range} />
        <KpiCell
          label="Win rate (window)"
          value={settledW ? winRateW.toFixed(1) + "%" : "—"}
          sub={settledW ? `${d.wins}W · ${d.losses}L` : "no resolved bets"}
          tone={settledW && winRateW >= 50 ? "pos" : settledW ? "neg" : ""}
        />
        <KpiCell
          label="Net P&L"
          value={(netPnlW >= 0 ? "+$" : "−$") + Math.abs(netPnlW).toFixed(2)}
          tone={netPnlW > 0 ? "pos" : netPnlW < 0 ? "neg" : ""}
        />
        <KpiCell
          label="Avg entry edge"
          value={settledW && avgEdgePresent ? avgEdge.toFixed(1) + "%" : "—"}
          sub="at signal"
        />
      </div>

      <div className="card flush">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "14px 16px", borderBottom: "1px solid var(--border)" }}>
          <div className="card-title">Open positions</div>
          <div className="card-actions">{open.length} pending</div>
        </div>
        <div className="tbl-scroll">
          <table className="tbl">
            <thead><tr>
              <th>Time</th><th>Station</th><th>Target</th><th>Bracket</th><th>Side</th>
              <th className="num">Fill</th><th className="num">Entry Edge</th><th className="num">Realized</th><th className="num">Size</th>
              <th className="num">Value / P&L</th><th>Status</th>
            </tr></thead>
            <tbody>
              {open.map((p, i) => (
                <OpenTradeRow
                  key={p.rowKey || i}
                  p={p}
                  expanded={expandedKey === (p.rowKey || i)}
                  onToggle={() => toggleRow(p.rowKey || i)}
                />
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card flush" style={{ marginTop: 14 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "14px 16px", borderBottom: "1px solid var(--border)" }}>
          <div className="card-title">Resolved bets</div>
          <div className="seg">
            {["7d","30d","All"].map(r => <button key={r} className={range === r ? "active" : ""} onClick={() => setRange(r)}>{r}</button>)}
          </div>
        </div>
        <div className="tbl-scroll">
          <table className="tbl">
            <thead><tr>
              <th>Time</th><th>Station</th><th>Target</th><th>Bracket</th><th>Side</th>
              <th className="num">Fill</th><th className="num">Entry Edge</th><th className="num">Realized</th><th className="num">Size</th>
              <th>Actual</th><th>Outcome</th><th className="num">P&L</th><th></th>
            </tr></thead>
            <tbody>
              {pagedResolved.map((p, i) => (
                <ResolvedTradeRow
                  key={p.rowKey || (resolvedStart + i)}
                  p={p}
                  expanded={expandedKey === (p.rowKey || (resolvedStart + i))}
                  onToggle={() => toggleRow(p.rowKey || (resolvedStart + i))}
                />
              ))}
            </tbody>
          </table>
        </div>
        {resolved.length > 0 && (
          <div className="table-pager">
            <span className="muted">
              Showing {resolvedStart + 1}–{resolvedEnd} of {resolved.length}
            </span>
            <div className="pager-actions">
              <button
                type="button"
                className="op-btn"
                disabled={clampedResolvedPage <= 0}
                onClick={() => setResolvedPage((p) => Math.max(0, p - 1))}
              >
                Previous
              </button>
              <button
                type="button"
                className="op-btn"
                disabled={clampedResolvedPage >= resolvedPageCount - 1}
                onClick={() => setResolvedPage((p) => Math.min(resolvedPageCount - 1, p + 1))}
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function StationsPage({ d }) {
  const groups = [
    ["bettable", "Bettable", "bettable"],
    ["lut_stale", "LUT stale (halt)", "warn"],
    ["no_lut", "LUT not yet seeded", "warn"],
    ["coverage_fail", "Failing coverage", "fail"],
    ["source_fail", "Unsupported source", "muted"],
  ];
  return (
    <div className="content" data-screen-label="Stations">
      <div className="card" style={{ display: "flex", alignItems: "center", gap: 14, padding: "12px 16px", marginBottom: 14 }}>
        {["Enrolled", "WU source", "Coverage >= 95%", "Active", "Bettable"].map((label, i, arr) => {
          const counts = [d.funnel.available_count, d.funnel.source_eligible_count, d.funnel.coverage_eligible_count, d.funnel.active_count, d.funnel.bettable_count];
          const final = i === arr.length - 1;
          return (
            <React.Fragment key={i}>
              <div style={{ flex: 1, padding: "8px 12px", borderRadius: 6, background: final ? "rgba(31,138,91,0.08)" : "rgba(0,0,0,0.025)" }}>
                <div style={{ fontSize: 10, color: "var(--fg3)", textTransform: "uppercase", letterSpacing: "0.08em", fontWeight: 700 }}>{label}</div>
                <div style={{ fontSize: 18, fontWeight: 700, color: final ? "var(--green)" : "var(--fg1)" }}>{counts[i]}</div>
              </div>
              {!final && <span style={{ color: "var(--fg4)", fontSize: 14 }}>›</span>}
            </React.Fragment>
          );
        })}
      </div>

      <div className="card flush">
        {groups.map(([key, title, cls]) => {
          const items = d.stations.filter(s => s.stage === key);
          if (!items.length) return null;
          return (
            <div key={key}>
              <div className={"stage-h " + cls}>
                <span className="stage-dot" />{title}<span className="count">({items.length})</span>
              </div>
              <div className="tbl-scroll">
                <table className="tbl">
                  <thead><tr>
                    <th>Station</th><th>City</th><th>Status</th><th>Source</th>
                    <th className="num">Coverage</th><th>Actual</th><th>Forecast</th>
                    <th className="num">LUT n</th><th className="num">Buckets</th><th className="num">LUT age</th>
                    <th>14d</th>
                  </tr></thead>
                  <tbody>
                    {items.map(s => (
                      <tr key={s.id}>
                        <td><span className="flag">{s.flag}</span><strong>{s.id}</strong></td>
                        <td className="muted">{s.city}</td>
                        <td>{s.status === "LIVE" ? <span className="pill pill-green">LIVE</span> : s.status === "DRY_RUN" ? <span className="pill pill-yellow">DRY_RUN</span> : <span className="pill pill-muted">{s.status}</span>}</td>
                        <td>{s.source && (s.source === "WU" ? <span className="pill pill-green">{s.source}</span> : <span className="pill pill-red">{s.source}</span>)}</td>
                        <td className="num">{s.coverage != null ? <span className={s.coverage >= 0.95 ? "pos" : "neg"}>{(s.coverage*100).toFixed(1)}%</span> : "—"}</td>
                        <td>{s.last_actual ? <span className={s.actual_fresh ? "pos" : ""} style={!s.actual_fresh ? { color: "var(--yellow)" } : {}}>{s.last_actual}</span> : <span className="muted">—</span>}</td>
                        <td>{s.fc_fresh ? <span className="pill pill-green">fresh</span> : s.last_actual ? <span className="pill pill-yellow">stale</span> : <span className="muted">—</span>}</td>
                        <td className="num">{s.lut_total_n || "—"}</td>
                        <td className="num">{s.lut_buckets ? `${s.lut_buckets}/8` : "0/8"}</td>
                        <td className="num">{s.lut_age != null ? (s.lut_stale ? <span className="neg">STALE {s.lut_age}h</span> : <span className="pos">{s.lut_age}h</span>) : <span className="muted">never</span>}</td>
                        <td>{d.stationSparks[s.id] && <Sparkline values={d.stationSparks[s.id]} color={(d.performanceByStation.find(p=>p.key===s.id)?.total_pnl ?? 0) >= 0 ? "#1f8a5b" : "#c1352a"} width={70} height={20} />}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// RiskPage removed 2026-05-06: superseded by halt/reduced-size banner + per-strategy
// caps surfaced on Overview/Performance. The standalone Risk tab held mostly
// hardcoded prototype values (legacy sizing display, hardcoded $10.40 etc.).

window.TradesPage = TradesPage; window.StationsPage = StationsPage;
