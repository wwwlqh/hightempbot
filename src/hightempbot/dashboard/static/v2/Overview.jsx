/* eslint-disable */
function KpiCell({ label, value, sub, tone, delta, sparkValues, sparkColor }) {
  return (
    <div className="kpi">
      <div className="kpi-label">{label}{delta != null && (
        <span className={"kpi-delta " + (delta >= 0 ? "pos" : "neg")}>{delta >= 0 ? "+" : ""}{delta.toFixed(1)}%</span>
      )}</div>
      <div className={"kpi-value" + (tone ? " " + tone : "")}>{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
      {sparkValues && (
        <div className="kpi-spark">
          <Sparkline values={sparkValues} color={sparkColor || "#1f8a5b"} width={60} height={18} />
        </div>
      )}
    </div>
  );
}

function OverviewPage({ d, range }) {
  const r = d.ratios;
  const cal = d.calibration || [];
  const calN = cal.reduce((sum, b) => sum + (b.n || 0), 0);
  const accountCurve = d.accountEquityCurve || d.equityCurve || [];
  const tradingCurve = d.tradingEquityCurve || d.equityCurve || [];
  const withdrawal = Number(d.returnTransferOutflow || 0);
  const realizedPnl = Number(d.realizedPnl ?? d.totalPnl ?? 0);
  const realizedPnlPct = d.initialBankroll > 0 ? (realizedPnl / d.initialBankroll) * 100 : null;
  const pnlSub = `${d.resolvedCount} resolved`;
  return (
    <div className="content" data-screen-label="Overview">
      <div className="kpi-grid">
        <KpiCell
          label="Net P&L"
          value={formatMoney(realizedPnl, true)}
          sub={pnlSub}
          tone={realizedPnl > 0 ? "pos" : realizedPnl < 0 ? "neg" : ""}
          delta={realizedPnlPct}
        />
        <KpiCell
          label="Capital"
          value={"$" + d.capital.toFixed(2)}
          sub="realized capital"
          tone={d.capital >= d.initialBankroll ? "pos" : "neg"}
        />
        <KpiCell label="Win rate" value={d.winRate.toFixed(1) + "%"} sub={`${d.wins}W · ${d.losses}L`} />
        <KpiCell label="Profit factor" value={r.profitFactor.toFixed(2)} sub="gross win / gross loss" tone={r.profitFactor > 1.5 ? "pos" : ""} />
        <KpiCell label="Withdrawal" value={formatMoney(withdrawal)} sub="returned pUSD" />
        <KpiCell label="Max DD" value={d.ddPct.toFixed(1) + "%"} sub="current peak-to-trough" tone={d.ddPct > 5 ? "neg" : ""} />
      </div>

      <div className="row-2">
        <div className="card lg">
          <div className="card-head">
            <div>
              <div className="card-title">Equity curve</div>
              <div style={{ fontSize: 11, color: "var(--fg3)", marginTop: 2 }}>Realized P&L after withdrawals</div>
            </div>
            <div className="card-actions"><span style={{display:"inline-block",width:8,height:8,borderRadius:"50%",background:"#1f6feb",marginRight:6}} />Account</div>
          </div>
          <EquityChart points={accountCurve} accent="#1f6feb" height={240} showWithdrawals={true} />
          <div style={{ borderTop: "1px solid var(--border)", marginTop: 8, paddingTop: 8 }}>
            <div style={{ fontSize: 11, color: "var(--fg3)", textTransform: "uppercase", letterSpacing: "0.08em", fontWeight: 700, marginBottom: 4 }}>Trading drawdown</div>
            <DrawdownChart points={tradingCurve} height={80} />
          </div>
        </div>
        <div className="card lg">
          <div className="card-head">
            <div className="card-title">Calibration · predicted vs observed</div>
          </div>
          <CalibrationChart buckets={cal} height={200} />
          <div style={{ fontSize: 11.5, color: "var(--fg2)", marginTop: 6, lineHeight: 1.5 }}>
            <strong>{calN} samples</strong> across {cal.length} LUT buckets. Bucket dot size = sample count. Closer to the diagonal = better forecast-market alignment.
          </div>
        </div>
      </div>

      <div className="row-3">
        <div className="card">
          <div className="card-head"><div className="card-title">Weekly P&L</div><div className="card-actions">8 weeks</div></div>
          <PnlBars data={d.weeklyPnl} height={140} />
        </div>
        <div className="card">
          <div className="card-head"><div className="card-title">P&L distribution per trade</div><div className="card-actions">{d.resolvedCount} trades</div></div>
          <PnlHistogram data={d.pnlDist} height={140} />
        </div>
        <div className="card">
          <div className="card-head"><div className="card-title">Streaks</div></div>
          <div style={{ display: "flex", gap: 18, alignItems: "center", marginBottom: 10 }}>
            <div>
              <div style={{ fontSize: 11, color: "var(--fg3)", textTransform: "uppercase", letterSpacing: "0.08em", fontWeight: 700 }}>Current</div>
              <div className="kpi-value pos" style={{ fontSize: 24 }}>{d.streaks.current}</div>
            </div>
            <div>
              <div style={{ fontSize: 11, color: "var(--fg3)", textTransform: "uppercase", letterSpacing: "0.08em", fontWeight: 700 }}>Longest W</div>
              <div className="kpi-value" style={{ fontSize: 18 }}>{d.streaks.longestW}</div>
            </div>
            <div>
              <div style={{ fontSize: 11, color: "var(--fg3)", textTransform: "uppercase", letterSpacing: "0.08em", fontWeight: 700 }}>Longest L</div>
              <div className="kpi-value" style={{ fontSize: 18 }}>{d.streaks.longestL}</div>
            </div>
          </div>
          <div style={{ fontSize: 10.5, color: "var(--fg3)", textTransform: "uppercase", letterSpacing: "0.08em", fontWeight: 700, marginBottom: 4 }}>Last 20</div>
          <div style={{ display: "flex", gap: 3, flexWrap: "wrap" }}>
            {d.streaks.last20.map((o, i) => (
              <span key={i} style={{
                width: 18, height: 18, borderRadius: 3, fontSize: 10.5, fontWeight: 700, fontFamily: "JetBrains Mono, monospace",
                display: "inline-grid", placeItems: "center",
                background: o === "W" ? "rgba(31,138,91,0.15)" : "rgba(193,53,42,0.15)",
                color: o === "W" ? "#1f8a5b" : "#c1352a",
              }}>{o}</span>
            ))}
          </div>
        </div>
      </div>

      <div className="row-2">
        <div className="card">
          <div className="card-head"><div className="card-title">Top performing stations</div><div className="card-actions">By P&L</div></div>
          <table className="tbl">
            <thead><tr><th>Station</th><th className="num">Bets</th><th className="num">Win %</th><th className="num">PF</th><th>Trend (14d)</th><th className="num">P&L</th></tr></thead>
            <tbody>
              {d.performanceByStation.slice(0, 6).map(r => (
                <tr key={r.key}>
                  <td><span className="flag">{r.flag}</span><strong>{r.key}</strong> <span className="muted">· {r.city}</span></td>
                  <td className="num">{r.n_bets}</td>
                  <td className="num">{r.win_rate}%</td>
                  <td className="num">{r.pf.toFixed(2)}</td>
                  <td><Sparkline values={d.stationSparks[r.key] || [0,0]} color={r.total_pnl >= 0 ? "#1f8a5b" : "#c1352a"} width={84} height={20} /></td>
                  <td className={"num " + (r.total_pnl >= 0 ? "pos" : "neg")}>{r.total_pnl >= 0 ? "+$" : "-$"}{Math.abs(r.total_pnl).toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="card">
          <div className="card-head"><div className="card-title">Strategy breakdown</div></div>
          <table className="tbl">
            <thead><tr><th>Strategy</th><th>Side</th><th className="num">Bets</th><th className="num">Win %</th><th className="num">ROI</th><th className="num">P&L</th></tr></thead>
            <tbody>
              {d.strategies.map(s => (
                <tr key={s.name}>
                  <td><strong>{s.name}</strong></td>
                  <td><span className={"pill " + (s.side === "NO" ? "pill-red" : "pill-green")}>{s.side}</span></td>
                  <td className="num">{s.n_bets}</td>
                  <td className="num">{s.win_rate.toFixed(1)}%</td>
                  <td className={"num " + (s.roi_pct > 0 ? "pos" : "neg")}>{s.roi_pct > 0 ? "+" : ""}{s.roi_pct.toFixed(1)}%</td>
                  <td className={"num " + (s.total_pnl >= 0 ? "pos" : "neg")}>{s.total_pnl >= 0 ? "+$" : "-$"}{Math.abs(s.total_pnl).toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

window.OverviewPage = OverviewPage;
