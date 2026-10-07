/* eslint-disable */
// Strategy tab. Renders ONE COLUMN PER ENABLED SLEEVE from d.strategiesConfig
// (v2_data.py serializes only enabled entries of the live STRATEGY_CONFIGS
// registry) over a Basics / Filters / Entry rule / Sizing & exit grouping.
// 2026-08-09: was hard-coded to TAIL/NO columns, so FLIP_MODE=1 (NO disabled,
// FLIP enabled) rendered "No enabled strategies" — now fully payload-driven.

function _fmtPct(x) {
  if (x == null) return "—";
  const pct = Number(x) * 100;
  return (Number.isInteger(pct) ? pct.toFixed(0) : pct.toFixed(1)) + "%";
}

function _fmtNum(x, digits) {
  if (x == null) return "—";
  const n = Number(x);
  if (!Number.isFinite(n)) return "—";
  return digits == null ? String(n) : n.toFixed(digits);
}

function _Lit({ children }) {
  return <span className="lit">{children}</span>;
}

function _Na() {
  return <span className="val-na">&mdash;</span>;
}

function StrategyPage({ d }) {
  const cfg = (d && d.strategiesConfig) || {};
  const lutMinN = (cfg.__lut_min_n && cfg.__lut_min_n.value) || 30;
  const execPolicy = cfg.__execution_policy || {};
  const disabled = (cfg.__disabled && cfg.__disabled.value) || [];
  const names = Object.keys(cfg).filter((k) => !k.startsWith("__"));

  if (!names.length) {
    return (
      <div className="content" data-screen-label="Strategy">
        <div className="card" style={{ padding: 18 }}>
          <div className="muted">No enabled strategies in STRATEGY_CONFIGS.</div>
        </div>
      </div>
    );
  }

  const fillPriceCell = (c) => {
    if (!c) return <_Na />;
    if (c.side === "NO") {
      return <>no_bid &ge; <_Lit>{_fmtNum(c.fp_min, 2)}</_Lit></>;
    }
    return (
      <>
        yes_ask &isin; [<_Lit>{_fmtNum(c.fp_min, 3)}</_Lit>, <_Lit>{_fmtNum(c.fp_max, 2)}</_Lit>]
      </>
    );
  };

  const consensusCell = (c) => {
    if (!c || c.consensus_skip_threshold == null) return <_Na />;
    return (
      <>
        max yes_ask &lt; <_Lit>{_fmtNum(c.consensus_skip_threshold, 2)}</_Lit>
      </>
    );
  };

  const edgeCell = (c) => {
    if (!c || c.min_edge == null || c.max_edge == null) return <_Na />;
    const signed = (v) => (v > 0 ? "+" : "") + _fmtNum(v, 2);
    return (
      <>
        (1-p_E) - fp - fee &isin; [<_Lit>{signed(c.min_edge)}</_Lit>, <_Lit>{signed(c.max_edge)}</_Lit>]
      </>
    );
  };

  const voteCell = (c) => {
    if (!c || !c.vote_signals || !c.vote_n_required) return <_Na />;
    const signalList = c.vote_signals.join(", ");
    return (
      <>
        <_Lit>{c.vote_n_required}</_Lit>/<_Lit>{c.vote_signals.length}</_Lit> signals &ge; <_Lit>{_fmtNum(c.alpha_ratio, 1)}</_Lit> x fp
        <span className="muted"> ({signalList})</span>
      </>
    );
  };

  const delayedEntryCell = (c) => {
    if (!c || c.delayed_entry_fp_max == null) return <_Na />;
    return <>yes_ask &le; <_Lit>{_fmtNum(c.delayed_entry_fp_max, 2)}</_Lit></>;
  };

  const ceilingCell = (c) => {
    if (!c || !c.signal_name_for_ceiling || c.max_edge_for_ceiling == null) return <_Na />;
    return (
      <>
        {c.signal_name_for_ceiling} - no_bid &ge; <_Lit>{_fmtNum(c.fp_min_for_ceiling, 2)}</_Lit> - edge &le; <_Lit>+{_fmtNum(c.max_edge_for_ceiling, 2)}</_Lit>
      </>
    );
  };

  const walkerCell = (c) => {
    if (!c || c.execution_min_edge == null) return <_Na />;
    return <>VWAP edge &ge; <_Lit>{_fmtNum(c.execution_min_edge, 2)}</_Lit></>;
  };

  const cadenceCell = () => {
    const minutes = execPolicy.scan_interval_minutes;
    if (minutes == null) return <_Na />;
    return (
      <>
        new slots tick <_Lit>{execPolicy.new_slot_tick}</_Lit>; {execPolicy.topups_every_tick ? "top-ups every " : "top-ups off"}
        {execPolicy.topups_every_tick ? <_Lit>{minutes + "m"}</_Lit> : null}
      </>
    );
  };

  const minBetCell = () =>
    execPolicy.min_bet_usd == null ? <_Na /> : <>min order <_Lit>${_fmtNum(execPolicy.min_bet_usd, 2)}</_Lit></>;

  const feeCell = () =>
    execPolicy.poly_fee_theta == null ? <_Na /> : <>theta <_Lit>{_fmtPct(execPolicy.poly_fee_theta)}</_Lit></>;

  const disabledCell = () =>
    disabled.length ? <_Lit>{disabled.join(", ")}</_Lit> : <_Na />;

  const tpCell = (c) => {
    if (!c || c.tp == null) return <_Na />;
    return (
      <>
        yes_price &ge; entry + <_Lit>{_fmtNum(c.tp, 2)}</_Lit>
      </>
    );
  };

  const slCell = (c) => {
    if (!c || c.sl == null) return <_Na />;
    return <_Lit>{_fmtNum(c.sl, 2)}</_Lit>;
  };

  const ROWS = [
    { section: "Basics" },
    { gate: "Side", cell: (c) => (c ? c.side : <_Na />) },
    { gate: "Capital", cell: (c) => (c ? <_Lit>{_fmtPct(c.capital_frac)}</_Lit> : <_Na />) },
    { gate: "Local hour", cell: (c) => (c ? <_Lit>{c.entry_hours || "—"}</_Lit> : <_Na />) },
    { gate: "Fill price", cell: fillPriceCell },

    { section: "Filters" },
    { gate: "Min samples", cell: () => <>n_cum &ge; <_Lit>{lutMinN}</_Lit></> },
    { gate: "Volume", cell: (c) => (c ? <>vol24h &ge; <_Lit>{_fmtNum(c.min_bvol, 0)}</_Lit></> : <_Na />) },
    { gate: "Consensus skip", cell: consensusCell },

    { section: "Entry rule" },
    { gate: "Edge", cell: edgeCell },
    { gate: "Vote", cell: voteCell },
    { gate: "Entry trigger", cell: delayedEntryCell },
    { gate: "Open-ended brackets", cell: ceilingCell },

    { section: "Sizing & exit" },
    { gate: "Slot cap", cell: (c) => (c ? <><_Lit>{_fmtPct(c.capital_frac)}</_Lit> capital</> : <_Na />) },
    { gate: "Top-up cadence", cell: () => cadenceCell() },
    { gate: "Order fill depth", cell: walkerCell },
    { gate: "Min order", cell: () => minBetCell() },
    { gate: "Fee model", cell: () => feeCell() },
    { gate: "TP", cell: tpCell },
    { gate: "SL", cell: slCell },
    { gate: "Disabled sleeves", cell: () => disabledCell() },
  ];

  return (
    <div className="content" data-screen-label="Strategy">
      <div className="card flush">
        <table className="cmp">
          <thead>
            <tr>
              <th>Gate</th>
              {names.map((n) => (
                <th key={n} className="col-strat">
                  <span className={"mark " + n.toLowerCase()}>{n}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ROWS.map((r, i) => {
              if (r.section) {
                return (
                  <tr key={i} className="section-h">
                    <td colSpan={names.length + 1}>{r.section}</td>
                  </tr>
                );
              }
              return (
                <tr key={i}>
                  <td className="col-gate">{r.gate}</td>
                  {names.map((n) => (
                    <td key={n} className="val">{r.cell(cfg[n])}</td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

window.StrategyPage = StrategyPage;
