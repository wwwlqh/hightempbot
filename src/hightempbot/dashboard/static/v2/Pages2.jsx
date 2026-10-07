/* eslint-disable */
function PerformancePage({ d }) {
  const r = d.ratios;
  const accountCurve = d.accountEquityCurve || d.equityCurve || [];
  const tradingCurve = d.tradingEquityCurve || d.equityCurve || [];
  const withdrawal = Number(d.returnTransferOutflow || 0);
  const netPnl = Number(d.realizedPnl ?? d.totalPnl ?? 0);
  return (
    <div className="content" data-screen-label="Performance">
      <div className="kpi-grid">
        <KpiCell label="Net P&L" value={formatMoney(netPnl, true)} sub={`${d.resolvedCount} resolved`} tone={netPnl >= 0 ? "pos" : "neg"} />
        <KpiCell label="Withdrawal" value={formatMoney(withdrawal)} sub="returned pUSD" />
        <KpiCell label="Sortino" value={r.sortino.toFixed(2)} sub="downside-only" />
        <KpiCell label="Calmar" value={r.calmar.toFixed(2)} sub="return / max DD" />
        <KpiCell label="Expectancy" value={"$" + r.expectancy.toFixed(2)} sub="per trade" />
        <KpiCell label="DD halt" value={(d.ddHaltThreshold || 40).toFixed(0) + "%"} sub="drawdown halt threshold" />
      </div>

      <div className="row-2">
        <div className="card lg">
          <div className="card-head"><div className="card-title">Account equity · trading drawdown</div><div className="card-actions">All time</div></div>
          <EquityChart points={accountCurve} accent="#1f6feb" height={260} showWithdrawals={true} />
          <DrawdownChart points={tradingCurve} height={90} />
        </div>
        <div className="card">
          <div className="card-head"><div className="card-title">Weekly P&L</div></div>
          <PnlBars data={d.weeklyPnl} height={170} />
          <div className="divider" />
          <div className="card-title" style={{ marginBottom: 8 }}>P&L distribution</div>
          <PnlHistogram data={d.pnlDist} height={140} />
        </div>
      </div>

      <div className="card">
        <div className="card-head"><div className="card-title">By station</div><div className="card-actions">{d.performanceByStation.length} stations</div></div>
        <div className="tbl-scroll">
          <table className="tbl">
            <thead><tr>
              <th>Station</th><th>City</th>
              <th className="num">Bets</th><th className="num">Resolved</th>
              <th className="num">W</th><th className="num">L</th>
              <th className="num">Win %</th><th className="num">PF</th>
              <th>14d trend</th><th className="num">P&L</th>
            </tr></thead>
            <tbody>
              {d.performanceByStation.map(r => (
                <tr key={r.key}>
                  <td><span className="flag">{r.flag}</span><strong>{r.key}</strong></td>
                  <td className="muted">{r.city}</td>
                  <td className="num">{r.n_bets}</td>
                  <td className="num">{r.n_resolved}</td>
                  <td className="num pos">{r.wins}</td>
                  <td className="num neg">{r.losses}</td>
                  <td className="num">{r.win_rate}%</td>
                  <td className="num">{r.pf.toFixed(2)}</td>
                  <td><Sparkline values={d.stationSparks[r.key] || [0,0]} color={r.total_pnl >= 0 ? "#1f8a5b" : "#c1352a"} width={100} height={22} /></td>
                  <td className={"num " + (r.total_pnl >= 0 ? "pos" : "neg")}>{r.total_pnl >= 0 ? "+$" : "-$"}{Math.abs(r.total_pnl).toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function CalibrationPage({ d }) {
  const stations = d.performanceByStation.map(s => s.key);
  const [station, setStation] = React.useState(stations[0]);
  const cal = (d.calibrationByStation && d.calibrationByStation[station]) || d.calibration || [];
  const stationMeta = d.performanceByStation.find(s => s.key === station);
  const totalN = cal.reduce((a,b)=>a+(b.n||0),0);
  const wellCalibrated = cal.filter(b => Math.abs((b.observed || 0) - (b.predicted || 0)) * 100 < 4).length;
  if (stations.length === 0) {
    return (
      <div className="content" data-screen-label="Calibration">
        <div className="card" style={{ padding: 24, textAlign: "center", color: "var(--fg3)" }}>
          No stations have resolved bets yet. Calibration is scoped per-station via the Performance table.
        </div>
      </div>
    );
  }

  return (
    <div className="content" data-screen-label="Calibration">
      <div className="card" style={{ display: "flex", alignItems: "center", gap: 14, padding: "12px 16px", marginBottom: 14, flexWrap: "wrap" }}>
        <div style={{ fontSize: 11, color: "var(--fg3)", textTransform: "uppercase", letterSpacing: "0.08em", fontWeight: 700 }}>Station</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {stations.map(s => {
            const m = d.performanceByStation.find(x => x.key === s);
            return (
              <button key={s} onClick={() => setStation(s)} className={"station-pill" + (station === s ? " active" : "")}>
                <span className="flag">{m?.flag}</span>
                <strong>{s}</strong>
                <span className="muted" style={{ fontSize: 11 }}>{m?.city}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="kpi-grid">
        <KpiCell label="Station" value={station} sub={stationMeta?.city} />
        <KpiCell label="Buckets populated" value={`${cal.filter(b=>b.n>0).length}/8`} sub="LUT bucket grid" />
        <KpiCell label="Total resolutions" value={totalN} sub="seeded into LUT" />
        <KpiCell label="Well-calibrated buckets" value={`${wellCalibrated}/${cal.length}`} sub="within 4pp of diagonal" tone={wellCalibrated >= cal.length / 2 ? "pos" : "neg"} />
        <KpiCell label="EMOS μ (latest)" value={stationMeta?.emos_mu != null ? stationMeta.emos_mu.toFixed(1) + "°C" : "—"} sub="ensemble mean" />
        <KpiCell label="EMOS σ (latest)" value={stationMeta?.emos_sigma != null ? stationMeta.emos_sigma.toFixed(2) + "°C"  : "—"} sub="ensemble spread" />
      </div>

      <div className="row-2">
        <div className="card lg">
          <div className="card-head">
            <div>
              <div className="card-title">EMOS + LUT calibration · {station}</div>
              <div style={{ fontSize: 11, color: "var(--fg3)", marginTop: 2 }}>predicted bucket probability vs observed hit rate</div>
            </div>
            <div className="card-actions">8 LUT buckets · n={totalN}</div>
          </div>
          <CalibrationChart buckets={cal} height={320} />
          <div style={{ fontSize: 12, color: "var(--fg2)", marginTop: 8, lineHeight: 1.55 }}>
            Each dot is one of the 8 fixed LUT buckets for <strong>{station}</strong>. Size = trade count in that bucket. <strong>Green</strong> dots are within 4pp of the diagonal, <strong>amber</strong> within 8pp, <strong>red</strong> further. The dashed line is perfect calibration. Buckets with n &lt; 10 are unreliable.
          </div>
        </div>
        <div className="card">
          <div className="card-head"><div className="card-title">LUT bucket detail · {station}</div></div>
          <table className="tbl">
            <thead><tr><th>Bucket</th><th className="num">n</th><th className="num">Pred</th><th className="num">Obs</th><th className="num">Δ pp</th></tr></thead>
            <tbody>
              {cal.map((b, i) => {
                const delta = (b.observed - b.predicted) * 100;
                const tone = Math.abs(delta) < 4 ? "pos" : Math.abs(delta) < 8 ? "" : "neg";
                return (
                  <tr key={i}>
                    <td><strong>{b.bucket}</strong></td>
                    <td className="num">{b.n}</td>
                    <td className="num mono">{(b.predicted*100).toFixed(0)}%</td>
                    <td className="num mono">{(b.observed*100).toFixed(0)}%</td>
                    <td className={"num " + tone}>{delta >= 0 ? "+" : ""}{delta.toFixed(1)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function ModelsPage({ d }) {
  const stations = d.performanceByStation.map(s => s.key);
  const [station, setStation] = React.useState(stations[0]);
  const stationMeta = d.performanceByStation.find(s => s.key === station);
  const ensemble = (d.ensembleByStation && d.ensembleByStation[station]) || d.ensembleByStation?.[stations[0]] || [];
  // Per-model values may be null (no forecast yet, or no per-model accuracy data).
  // Filter to numeric values for median / spread; render "—" elsewhere.
  const validTmax = ensemble.map(m => m.tmax).filter(v => v != null && !isNaN(v));
  const ensembleMedian = validTmax.length
    ? [...validTmax].sort((a,b)=>a-b)[Math.floor(validTmax.length/2)]
    : null;
  const tmaxSpread = validTmax.length >= 2
    ? Math.max(...validTmax) - Math.min(...validTmax)
    : null;
  const targetDate = d.targetDate || "—";

  return (
    <div className="content" data-screen-label="Models">
      {stations.length === 0 && (
        <div className="card" style={{ padding: 24, textAlign: "center", color: "var(--fg3)" }}>
          No stations have resolved bets yet. The forecast-models view scopes per-station via the Performance table.
        </div>
      )}
      {stations.length > 0 && <>
      <div className="card" style={{ display: "flex", alignItems: "center", gap: 14, padding: "12px 16px", marginBottom: 14, flexWrap: "wrap" }}>
        <div style={{ fontSize: 11, color: "var(--fg3)", textTransform: "uppercase", letterSpacing: "0.08em", fontWeight: 700 }}>Station</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {stations.map(s => {
            const m = d.performanceByStation.find(x => x.key === s);
            return (
              <button key={s} onClick={() => setStation(s)} className={"station-pill" + (station === s ? " active" : "")}>
                <span className="flag">{m?.flag}</span>
                <strong>{s}</strong>
                <span className="muted" style={{ fontSize: 11 }}>{m?.city}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="kpi-grid">
        <KpiCell label="Target date" value={targetDate} sub="next resolution" />
        <KpiCell label="Ensemble median" value={ensembleMedian != null ? ensembleMedian.toFixed(1) + "°C" : "—"} sub={`${validTmax.length}/${ensemble.length} models with forecast`} />
        <KpiCell label="EMOS μ" value={stationMeta?.emos_mu != null ? stationMeta.emos_mu.toFixed(1) + "°C" : "—"} sub="walk-forward refit" />
        <KpiCell label="EMOS σ" value={stationMeta?.emos_sigma != null ? stationMeta.emos_sigma.toFixed(2) + "°C" : "—"} sub="ensemble spread" />
        <KpiCell label="Model spread" value={tmaxSpread != null ? tmaxSpread.toFixed(1) + "°C" : "—"} sub="max − min" />
        <KpiCell label="Forecast freshness" value={d.lastScanAgo || "—"} sub="last bot tick" />
      </div>

      <div className="card">
        <div className="card-head">
          <div>
            <div className="card-title">9-model ensemble · {station} · target {targetDate}</div>
            <div style={{ fontSize: 11, color: "var(--fg3)", marginTop: 2 }}>EMOS + LUT consumes the per-model Tmax to form the bucket distribution. Median highlighted.</div>
          </div>
        </div>
        <table className="tbl">
          <thead><tr><th>Model</th><th>Source</th><th className="num">Tmax</th><th className="num">Δ vs median</th><th className="num">Avg accuracy 30d</th><th>In-band</th></tr></thead>
          <tbody>
            {ensemble.map((m, i) => {
              const hasTmax = m.tmax != null && !isNaN(m.tmax);
              const delta = (hasTmax && ensembleMedian != null) ? (m.tmax - ensembleMedian) : null;
              const inBand = delta != null && Math.abs(delta) < 0.6;
              const hasAcc = m.accuracy != null && !isNaN(m.accuracy);
              return (
                <tr key={i} style={inBand ? { background: "rgba(31,138,91,0.05)" } : {}}>
                  <td><strong>{m.name}</strong></td>
                  <td className="muted">{m.source}</td>
                  <td className="num mono">{hasTmax ? <strong>{m.tmax.toFixed(1)}°C</strong> : <span className="muted">—</span>}</td>
                  <td className={"num mono " + (delta != null && delta >= 0 ? "pos" : delta != null ? "neg" : "muted")}>
                    {delta != null ? (delta >= 0 ? "+" : "") + delta.toFixed(1) : "—"}
                  </td>
                  <td className="num">{hasAcc ? (m.accuracy * 100).toFixed(0) + "%" : <span className="muted">—</span>}</td>
                  <td>{
                    !hasTmax ? <span className="pill pill-muted">no fcst</span> :
                    inBand ? <span className="pill pill-green">within 0.6°C</span> :
                    <span className="pill pill-muted">outlier</span>
                  }</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="card">
        <div className="card-head">
          <div>
            <div className="card-title">30-day model accuracy · {station}</div>
            <div style={{ fontSize: 11, color: "var(--fg3)", marginTop: 2 }}>Per-model accuracy is not yet wired to the live ledger; values shown as "—" until per-model history accrues.</div>
          </div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10 }}>
          {ensemble.map((m, i) => {
            const hasAcc = m.accuracy != null && !isNaN(m.accuracy);
            const accPct = hasAcc ? m.accuracy * 100 : 0;
            const tone = !hasAcc ? "var(--fg3)" : m.accuracy >= 0.7 ? "var(--green)" : m.accuracy >= 0.62 ? "var(--fg1)" : "var(--red)";
            return (
              <div key={i} style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "10px 12px", background: "var(--bg-surface)" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                  <strong style={{ fontSize: 13 }}>{m.name}</strong>
                  <span className="muted" style={{ fontSize: 10.5, fontFamily: "JetBrains Mono, monospace" }}>{m.source}</span>
                </div>
                <div style={{ marginTop: 6, fontFamily: "JetBrains Mono, monospace", fontSize: 16, fontWeight: 700, color: tone }}>
                  {hasAcc ? accPct.toFixed(0) + "%" : "—"}
                </div>
                <div style={{ height: 4, background: "rgba(0,0,0,0.05)", borderRadius: 2, marginTop: 6, overflow: "hidden" }}>
                  <div style={{ width: accPct + "%", height: "100%", background: !hasAcc ? "var(--fg4)" : m.accuracy >= 0.7 ? "var(--green)" : m.accuracy >= 0.62 ? "var(--accent)" : "var(--red)" }} />
                </div>
              </div>
            );
          })}
        </div>
      </div>
      </>}
    </div>
  );
}

window.PerformancePage = PerformancePage;
window.CalibrationPage = CalibrationPage;
window.ModelsPage = ModelsPage;
