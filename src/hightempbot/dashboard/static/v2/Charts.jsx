/* eslint-disable */
// Charting primitives — SVG, no deps. All sized by viewBox.

function EmptyChart({ height = 140, label = "No data" }) {
  return (
    <svg viewBox={`0 0 480 ${height}`} width="100%" height={height} style={{ display: "block" }}>
      <line x1="28" x2="468" y1={height - 24} y2={height - 24} stroke="#e8e5dd" />
      <text x="240" y={height / 2} textAnchor="middle" fontSize="11" fill="#8b9099" fontFamily="Inter, sans-serif">{label}</text>
    </svg>
  );
}

function Sparkline({ values, color = "#1f8a5b", width = 80, height = 22, fill = true }) {
  if (!values || !values.length) return null;
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const stepX = width / Math.max(values.length - 1, 1);
  const pts = values.map((v, i) => [i * stepX, height - 2 - ((v - min) / range) * (height - 4)]);
  const path = pts.map((p, i) => `${i ? "L" : "M"} ${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const area = path + ` L ${(pts[pts.length-1][0]).toFixed(1)} ${height} L 0 ${height} Z`;
  return (
    <svg className="sparkline" viewBox={`0 0 ${width} ${height}`} width={width} height={height}>
      {fill && <path d={area} fill={color} fillOpacity="0.12" />}
      <path d={path} stroke={color} strokeWidth="1.4" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function formatMoney(v, signed = false) {
  const value = Number(v || 0);
  const prefix = value < 0 ? "-$" : (signed ? "+$" : "$");
  return prefix + Math.abs(value).toFixed(2);
}

// Equity curve with drawdown shaded underneath (the "underwater" view)
function EquityChart({ points, accent = "#1f6feb", height = 260, showWithdrawals = false }) {
  if (!points || !points.length) return <EmptyChart height={height} />;
  const W = 1000, H = height;
  const padL = 44, padR = 16, padT = 18, padB = 28;
  const ys = points.map(p => p.y);
  const minY = Math.min(0, ...ys), maxY = Math.max(...ys, 1);
  const xStep = (W - padL - padR) / Math.max(points.length - 1, 1);
  const sy = v => padT + (1 - (v - minY) / (maxY - minY)) * (H - padT - padB);
  const sx = i => padL + i * xStep;
  // running max → drawdown series
  let runMax = -Infinity;
  const dd = ys.map(y => { runMax = Math.max(runMax, y); return y - runMax; });
  const minDD = Math.min(...dd);
  const ddRange = -minDD || 1;
  const ddH = 60;
  const sdy = v => H - padB - ((v - minDD) / ddRange) * ddH + ddH; // ignore — separate plot below

  const eqPath = points.map((p, i) => `${i ? "L" : "M"} ${sx(i)} ${sy(p.y)}`).join(" ");
  const eqArea = eqPath + ` L ${sx(points.length - 1)} ${sy(0)} L ${sx(0)} ${sy(0)} Z`;
  const withdrawalMarks = showWithdrawals
    ? points.map((p, i) => Number(p.withdrawal || 0) > 0 ? { p, i } : null).filter(Boolean)
    : [];

  // gridlines
  const gridSteps = 4;
  const gridLines = Array.from({ length: gridSteps + 1 }, (_, i) => {
    const v = minY + (i / gridSteps) * (maxY - minY);
    return { y: sy(v), label: (v >= 0 ? "+$" : "-$") + Math.abs(v).toFixed(0) };
  });

  // x axis labels — every 3rd
  const xLabels = points.map((p, i) => i % 3 === 0 || i === points.length - 1 ? { x: sx(i), label: p.x } : null).filter(Boolean);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} style={{ display: "block" }}>
      <defs>
        <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={accent} stopOpacity="0.18" />
          <stop offset="100%" stopColor={accent} stopOpacity="0" />
        </linearGradient>
      </defs>
      {gridLines.map((g, i) => (
        <g key={i}>
          <line x1={padL} x2={W - padR} y1={g.y} y2={g.y} stroke="#e8e5dd" strokeDasharray="2 4" />
          <text x={padL - 6} y={g.y + 3} textAnchor="end" fontSize="10" fill="#8b9099" fontFamily="JetBrains Mono, monospace">{g.label}</text>
        </g>
      ))}
      <line x1={padL} x2={W - padR} y1={sy(0)} y2={sy(0)} stroke="#b3b8be" />
      <path d={eqArea} fill="url(#eqGrad)" />
      {withdrawalMarks.map(({ p, i }) => (
        <g key={"w" + i}>
          <line x1={sx(i)} x2={sx(i)} y1={padT} y2={H - padB} stroke="#b07e0a" strokeWidth="1.4" strokeDasharray="4 5" />
          <circle cx={sx(i)} cy={sy(p.y)} r="4" fill="#b07e0a" stroke="#fff" strokeWidth="2" />
          <rect x={Math.min(W - padR - 78, Math.max(padL, sx(i) - 39))} y={padT + 4} width="78" height="20" rx="4" fill="#b07e0a" />
          <text x={Math.min(W - padR - 39, Math.max(padL + 39, sx(i)))} y={padT + 18} textAnchor="middle" fontSize="10.5" fill="#fff" fontFamily="JetBrains Mono, monospace" fontWeight="700">-{formatMoney(p.withdrawal)}</text>
        </g>
      ))}
      <path d={eqPath} fill="none" stroke={accent} strokeWidth="2" strokeLinecap="round" />
      {points.map((p, i) => i === points.length - 1 ? (
        <g key="last">
          <circle cx={sx(i)} cy={sy(p.y)} r="4" fill={accent} stroke="#fff" strokeWidth="2" />
          <rect x={sx(i) - 38} y={sy(p.y) - 26} width="76" height="20" rx="4" fill={accent} />
          <text x={sx(i)} y={sy(p.y) - 12} textAnchor="middle" fontSize="11" fill="#fff" fontFamily="JetBrains Mono, monospace" fontWeight="700">{formatMoney(p.y, true)}</text>
        </g>
      ) : null)}
      {xLabels.map((x, i) => (
        <text key={i} x={x.x} y={H - 10} textAnchor="middle" fontSize="10" fill="#8b9099" fontFamily="JetBrains Mono, monospace">{x.label}</text>
      ))}
    </svg>
  );
}

// Underwater drawdown chart (separate small panel, often paired w/ equity)
function DrawdownChart({ points, height = 90 }) {
  if (!points || !points.length) return <EmptyChart height={height} />;
  const W = 1000, H = height;
  const padL = 44, padR = 16, padT = 8, padB = 18;
  let runMax = -Infinity;
  const dd = points.map(p => { runMax = Math.max(runMax, p.y); return p.y - runMax; });
  const minDD = Math.min(-1, ...dd);
  const xStep = (W - padL - padR) / Math.max(points.length - 1, 1);
  const sy = v => padT + (v / minDD) * (H - padT - padB); // 0 at top, minDD at bottom
  const sx = i => padL + i * xStep;
  const path = dd.map((v, i) => `${i ? "L" : "M"} ${sx(i)} ${sy(v)}`).join(" ");
  const area = path + ` L ${sx(dd.length - 1)} ${sy(0)} L ${sx(0)} ${sy(0)} Z`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} style={{ display: "block" }}>
      <line x1={padL} x2={W - padR} y1={sy(0)} y2={sy(0)} stroke="#e8e5dd" />
      <path d={area} fill="rgba(193,53,42,0.12)" />
      <path d={path} stroke="#c1352a" strokeWidth="1.5" fill="none" />
      <text x={padL - 6} y={sy(0) + 3} textAnchor="end" fontSize="10" fill="#8b9099" fontFamily="JetBrains Mono, monospace">0%</text>
      <text x={padL - 6} y={H - padB + 3} textAnchor="end" fontSize="10" fill="#8b9099" fontFamily="JetBrains Mono, monospace">${minDD.toFixed(0)}</text>
    </svg>
  );
}

// Calibration plot — predicted vs observed, with ideal diagonal
function CalibrationChart({ buckets, height = 240 }) {
  const W = 360, H = height;
  const pad = 36;
  const sx = v => pad + v * (W - pad * 2);
  const sy = v => H - pad - v * (H - pad * 2);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} style={{ display: "block" }}>
      {/* grid */}
      {[0, 0.25, 0.5, 0.75, 1].map((v, i) => (
        <g key={i}>
          <line x1={sx(0)} x2={sx(1)} y1={sy(v)} y2={sy(v)} stroke="#e8e5dd" />
          <line x1={sx(v)} x2={sx(v)} y1={sy(0)} y2={sy(1)} stroke="#e8e5dd" />
          <text x={pad - 6} y={sy(v) + 3} textAnchor="end" fontSize="9.5" fill="#8b9099" fontFamily="JetBrains Mono, monospace">{(v*100).toFixed(0)}%</text>
          <text x={sx(v)} y={H - pad + 14} textAnchor="middle" fontSize="9.5" fill="#8b9099" fontFamily="JetBrains Mono, monospace">{(v*100).toFixed(0)}%</text>
        </g>
      ))}
      {/* ideal diagonal */}
      <line x1={sx(0)} y1={sy(0)} x2={sx(1)} y2={sy(1)} stroke="#b3b8be" strokeDasharray="3 4" />
      {/* data points */}
      {buckets.map((b, i) => {
        const r = Math.max(3, Math.min(11, Math.sqrt(b.n) * 1.4));
        const tone = Math.abs(b.observed - b.predicted) < 0.04 ? "#1f8a5b" : Math.abs(b.observed - b.predicted) < 0.08 ? "#b07e0a" : "#c1352a";
        return <circle key={i} cx={sx(b.predicted)} cy={sy(b.observed)} r={r} fill={tone} fillOpacity="0.55" stroke={tone} strokeWidth="1.5" />;
      })}
      {/* connecting line */}
      <path d={buckets.map((b, i) => `${i ? "L" : "M"} ${sx(b.predicted)} ${sy(b.observed)}`).join(" ")}
            stroke="#1f6feb" strokeWidth="1.5" fill="none" strokeOpacity="0.4" />
      {/* axis labels */}
      <text x={W/2} y={H - 6} textAnchor="middle" fontSize="10" fill="#525a66" fontWeight="600">Predicted probability</text>
      <text x={10} y={H/2} textAnchor="middle" fontSize="10" fill="#525a66" fontWeight="600" transform={`rotate(-90, 10, ${H/2})`}>Observed hit rate</text>
    </svg>
  );
}

// Weekly P&L bars
function PnlBars({ data, height = 140 }) {
  if (!data || !data.length) return <EmptyChart height={height} />;
  const W = 480, H = height;
  const padT = 16, padB = 26, padL = 36, padR = 12;
  const vals = data.map(d => d.pnl);
  const max = Math.max(...vals, 0), min = Math.min(...vals, 0);
  const range = (max - min) || 1;
  const sy = v => padT + (1 - (v - min) / range) * (H - padT - padB);
  const barW = ((W - padL - padR) / data.length) - 6;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} style={{ display: "block" }}>
      <line x1={padL} x2={W-padR} y1={sy(0)} y2={sy(0)} stroke="#b3b8be" />
      {data.map((d, i) => {
        const x = padL + i * ((W - padL - padR) / data.length) + 3;
        const y0 = sy(0), y = sy(d.pnl);
        const tone = d.pnl >= 0 ? "#1f8a5b" : "#c1352a";
        return (
          <g key={i}>
            <rect x={x} y={Math.min(y0, y)} width={barW} height={Math.abs(y - y0)} fill={tone} fillOpacity="0.85" rx="2" />
            <text x={x + barW/2} y={H - 10} textAnchor="middle" fontSize="9.5" fill="#8b9099" fontFamily="JetBrains Mono, monospace">{d.week}</text>
            <text x={x + barW/2} y={d.pnl >= 0 ? y - 4 : y + 11} textAnchor="middle" fontSize="9.5" fill={tone} fontFamily="JetBrains Mono, monospace" fontWeight="700">{d.pnl >= 0 ? "+" : ""}{d.pnl.toFixed(0)}</text>
          </g>
        );
      })}
    </svg>
  );
}

// P&L distribution histogram
function PnlHistogram({ data, height = 140 }) {
  if (!data || !data.length) return <EmptyChart height={height} />;
  const W = 480, H = height;
  const padT = 12, padB = 24, padL = 28, padR = 12;
  const max = Math.max(...data.map(d => d.n));
  const sy = v => padT + (1 - v / max) * (H - padT - padB);
  const slot = (W - padL - padR) / data.length;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} style={{ display: "block" }}>
      {data.map((d, i) => {
        const x = padL + i * slot + 2;
        const y = sy(d.n);
        const tone = parseFloat(d.bin) >= 0 ? "#1f8a5b" : "#c1352a";
        return (
          <g key={i}>
            <rect x={x} y={y} width={slot - 4} height={H - padB - y} fill={tone} fillOpacity="0.7" rx="2" />
            <text x={x + (slot-4)/2} y={H - 8} textAnchor="middle" fontSize="9.5" fill="#8b9099" fontFamily="JetBrains Mono, monospace">{d.bin}</text>
          </g>
        );
      })}
      <line x1={padL} x2={W-padR} y1={H-padB} y2={H-padB} stroke="#b3b8be" />
    </svg>
  );
}

Object.assign(window, { EmptyChart, Sparkline, EquityChart, DrawdownChart, CalibrationChart, PnlBars, PnlHistogram, formatMoney });
