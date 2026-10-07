/* eslint-disable */
// Calendar styled like the reference: 7 days + Total column, weekly totals, monthly P&L header
function Calendar({ days, monthLabel, onPrev, onNext }) {
  const byDate = {};
  days.forEach(d => { byDate[d.date] = d; });

  const [year, mon] = monthLabel.split("-").map(Number);
  const first = new Date(Date.UTC(year, mon - 1, 1));
  const startDay = first.getUTCDay();
  const daysInMonth = new Date(Date.UTC(year, mon, 0)).getUTCDate();

  // Build cells (with leading + trailing greys for the dim "next month" days like the ref)
  const cells = [];
  for (let i = 0; i < startDay; i++) cells.push({ dim: true });
  for (let dn = 1; dn <= daysInMonth; dn++) {
    const dateStr = `${year}-${String(mon).padStart(2,"0")}-${String(dn).padStart(2,"0")}`;
    cells.push({ dn, date: dateStr, ...(byDate[dateStr] || {}) });
  }
  let nextDn = 1;
  while (cells.length % 7 !== 0) cells.push({ dn: nextDn++, dim: true });

  // Group into weeks of 7
  const weeks = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));

  const monthName = new Date(year, mon - 1, 1).toLocaleString("en-US", { month: "long", year: "numeric" });
  const monthlyPnl = days.reduce((a,d) => a + (d.pnl || 0), 0);
  const dayLabels = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
  const pnlTone = (pnl, trades) => {
    if (!trades) return "flat";
    return pnl > 0 ? "pos" : pnl < 0 ? "neg" : "flat";
  };
  const wlText = (wins, losses) => `${wins || 0}W / ${losses || 0}L`;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button className="cal-nav" onClick={onPrev}>‹</button>
          <div style={{ fontWeight: 700, fontSize: 16, color: "var(--fg1)" }}>{monthName}</div>
          <button className="cal-nav" onClick={onNext}>›</button>
        </div>
        <div style={{ fontSize: 13, color: "var(--fg2)" }}>
          Monthly P&L:&nbsp;
          <strong className={monthlyPnl >= 0 ? "pos" : "neg"} style={{ fontFamily: "JetBrains Mono, monospace", fontSize: 14 }}>
            {monthlyPnl >= 0 ? "+$" : "−$"}{Math.abs(monthlyPnl).toFixed(2)}
          </strong>
        </div>
      </div>

      <div className="cal2-grid">
        {dayLabels.map(d => <div key={d} className="cal2-dow">{d}</div>)}
        <div className="cal2-dow cal2-total-h">&nbsp;</div>

        {weeks.map((week, wi) => {
          const weekTotal = week.reduce((a,c) => a + (c.pnl || 0), 0);
          const weekTrades = week.reduce((a,c) => a + (c.n_trades || 0), 0);
          return (
            <React.Fragment key={wi}>
              {week.map((c, i) => {
                if (c.dim) {
                  return (
                    <div key={i} className="cal2-cell cal2-dim">
                      <div className="cal2-day-num">{c.dn || ""}</div>
                      <div className="cal2-doc-icon">▢</div>
                    </div>
                  );
                }
                const has = (c.n_trades || 0) > 0;
                const pnl = c.pnl || 0;
                const tone = pnlTone(pnl, c.n_trades || 0);
                return (
                  <div key={i} className={"cal2-cell " + (has ? "has " + tone : "empty")}>
                    <div className="cal2-cell-head">
                      <span className="cal2-day-num">{c.dn}</span>
                      <span className="cal2-doc-icon">▢</span>
                    </div>
                    <div className={"cal2-pnl " + (has ? tone : "muted")}>
                      {has ? (pnl >= 0 ? "+$" : "−$") + Math.abs(pnl).toFixed(2) : "$0"}
                    </div>
                    <div className="cal2-trades">{c.n_trades || 0} {(c.n_trades||0) === 1 ? "trade" : "trades"}</div>
                    {has && <div className="cal2-wl">{wlText(c.wins, c.losses)}</div>}
                  </div>
                );
              })}
              <div className={"cal2-week-total " + pnlTone(weekTotal, weekTrades)}>
                <div className="cal2-week-label">Week {wi + 1}</div>
                <div className={"cal2-week-pnl " + pnlTone(weekTotal, weekTrades)}>
                  {weekTotal >= 0 ? "+$" : "−$"}{Math.abs(weekTotal).toFixed(2)}
                </div>
                <div className="cal2-week-sub">{weekTrades} {weekTrades === 1 ? "trade" : "trades"}</div>
                {weekTrades > 0 && (
                  <div className="cal2-wl">
                    {wlText(
                      week.reduce((a,c) => a + (c.wins || 0), 0),
                      week.reduce((a,c) => a + (c.losses || 0), 0)
                    )}
                  </div>
                )}
              </div>
            </React.Fragment>
          );
        })}
      </div>
    </div>
  );
}

function CalendarPage({ d }) {
  const months = Object.keys(d.calendar || {}).sort();
  // Latest month with data, else the current month.
  const _now = new Date();
  const _currentYM = `${_now.getFullYear()}-${String(_now.getMonth()+1).padStart(2,"0")}`;
  const [month, setMonth] = React.useState(months[months.length - 1] || _currentYM);
  const days = (d.calendar && d.calendar[month]) || [];

  const prevMonth = () => {
    const [y,m] = month.split("-").map(Number);
    const nd = new Date(y, m - 2, 1);
    setMonth(`${nd.getFullYear()}-${String(nd.getMonth()+1).padStart(2,"0")}`);
  };
  const nextMonth = () => {
    const [y,m] = month.split("-").map(Number);
    const nd = new Date(y, m, 1);
    setMonth(`${nd.getFullYear()}-${String(nd.getMonth()+1).padStart(2,"0")}`);
  };

  const allDays = Object.values(d.calendar || {}).flat();
  const winDays = allDays.filter(x => (x.pnl || 0) > 0);
  const lossDays = allDays.filter(x => (x.pnl || 0) < 0);
  // Best/worst consider only gain/loss days respectively.
  const bestDay = winDays.reduce((a, x) => (x.pnl > (a?.pnl ?? -Infinity) ? x : a), null);
  const worstDay = lossDays.reduce((a, x) => (x.pnl < (a?.pnl ?? Infinity) ? x : a), null);
  const avgPerDay = allDays.length ? allDays.reduce((s,x) => s + (x.pnl||0), 0) / allDays.length : 0;

  return (
    <div className="content" data-screen-label="Calendar">
      <div className="kpi-grid">
        <KpiCell label="Trading days" value={allDays.filter(x => (x.n_trades||0) > 0).length} sub="all-time" />
        <KpiCell label="Win days" value={winDays.length} sub={`${(winDays.length / Math.max(1, winDays.length+lossDays.length) * 100).toFixed(0)}% of trading days`} tone="pos" />
        <KpiCell label="Loss days" value={lossDays.length} tone="neg" />
        <KpiCell label="Avg P&L / day" value={(avgPerDay >= 0 ? "+$" : "−$") + Math.abs(avgPerDay).toFixed(2)} tone={avgPerDay >= 0 ? "pos" : "neg"} />
        <KpiCell label="Best day" value={bestDay ? "+$" + bestDay.pnl.toFixed(2) : "—"} sub={bestDay?.date} tone="pos" />
        <KpiCell label="Worst day" value={worstDay ? "−$" + Math.abs(worstDay.pnl).toFixed(2) : "—"} sub={worstDay?.date} tone="neg" />
      </div>

      <div className="card">
        <Calendar days={days} monthLabel={month} onPrev={prevMonth} onNext={nextMonth} />
      </div>
    </div>
  );
}

window.Calendar = Calendar; window.CalendarPage = CalendarPage;
