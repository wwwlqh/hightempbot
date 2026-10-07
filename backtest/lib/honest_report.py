"""Honest per-bet metrics and per-bet parquet persistence.

PnL alone hid model overconfidence, a thin return on stake and a °C/°F split.
For any bet stream this reports, by strategy and by unit: n, win rate ± SE,
claimed vs realized probability, ROS, max drawdown and a reliability table.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

# Per-bet record columns. Append only; shadow_replay reads these names.
BET_COLUMNS = [
    "period",         # replay period / chunk label (e.g. "A", "B-D", "TRAIN")
    "strategy",       # NO / TAIL / YMID / YHIGH
    "side",           # NO / YES
    "station_id",     # ICAO
    "market_date",    # YYYY-MM-DD (== live ledger target_date)
    "bracket_index",
    "bracket_kind",   # low / mid / high
    "bracket_label",  # raw Polymarket label, encodes the native unit
    "bracket_unit",   # C or F, derived from the label
    "lo_c",           # bracket lower bound in Celsius (ROUND-rule; -inf for low tails)
    "hi_c",           # bracket upper bound in Celsius (ROUND-rule; +inf for high tails)
    "claimed_p",      # model claimed P(this bet wins) — raw signal, pre-fill
    "entry_price",    # displayed mid at entry
    "fill_vwap",      # realized VWAP after walking the book
    "stake",          # dollars staked
    "won",            # did the bet's side win at resolution (bool)
    "pnl",            # net dollars
    "entry_ts",       # Unix seconds
    "exit_reason",    # close / tp / sl
    "row_idx",        # decision-table row index (debug / re-join)
]

# Claimed-probability bins, finer near 0.9–1.0 where the bets are.
DEFAULT_BINS = (0.0, 0.02, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99, 1.0)

_UNIT_RX = re.compile(r"[CF]")


def unit_from_label(label) -> str:
    """Return 'C' or 'F' (or '?' if unknown) from a Polymarket bracket label."""
    if label is None:
        return "?"
    m = _UNIT_RX.search(str(label).upper())
    return m.group(0) if m else "?"


def proportion_se(k: int, n: int) -> float:
    """Standard error of a win-rate proportion: sqrt(p(1-p)/n)."""
    if n <= 0:
        return 0.0
    p = k / n
    return math.sqrt(max(p * (1.0 - p), 0.0) / n)


def _max_drawdown_abs(pnls_in_order: np.ndarray) -> float:
    """Max peak-to-trough dollar drawdown of a cumulative-PnL curve starting 0."""
    if len(pnls_in_order) == 0:
        return 0.0
    cum = np.concatenate([[0.0], np.cumsum(pnls_in_order)])
    peak = np.maximum.accumulate(cum)
    return float((peak - cum).max())


def reliability_table(records: list[dict], bins=DEFAULT_BINS) -> list[dict]:
    """Bucket bets by claimed P(win); report realized frequency and n per bin."""
    if not records:
        return []
    claimed = np.array([float(r["claimed_p"]) for r in records], dtype=float)
    won = np.array([1.0 if r["won"] else 0.0 for r in records], dtype=float)
    out: list[dict] = []
    edges = list(bins)
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        if i == len(edges) - 2:
            mask = (claimed >= lo) & (claimed <= hi)
        else:
            mask = (claimed >= lo) & (claimed < hi)
        n = int(mask.sum())
        if n == 0:
            continue
        cm = float(claimed[mask].mean())
        rf = float(won[mask].mean())
        out.append({
            "bin_lo": round(lo, 4),
            "bin_hi": round(hi, 4),
            "n": n,
            "claimed_mean": round(cm, 4),
            "realized_freq": round(rf, 4),
            "gap_pp": round((cm - rf) * 100.0, 2),
        })
    return out


def slice_metrics(records: list[dict], bins=DEFAULT_BINS) -> dict:
    """Honest metrics for one slice of bets (already filtered)."""
    n = len(records)
    if n == 0:
        return {
            "n": 0, "wins": 0, "win_rate": 0.0, "win_rate_se": 0.0,
            "claimed_mean": 0.0, "realized_mean": 0.0, "overconfidence_pp": 0.0,
            "pnl": 0.0, "staked": 0.0, "ros_pct": 0.0, "max_dd_abs": 0.0,
            "reliability": [],
        }
    claimed = np.array([float(r["claimed_p"]) for r in records], dtype=float)
    won = np.array([1 if r["won"] else 0 for r in records], dtype=int)
    stake = np.array([float(r["stake"]) for r in records], dtype=float)
    pnl = np.array([float(r["pnl"]) for r in records], dtype=float)
    ts = np.array([int(r.get("entry_ts", 0) or 0) for r in records], dtype=np.int64)
    order = np.argsort(ts, kind="stable")

    wins = int(won.sum())
    win_rate = wins / n
    claimed_mean = float(claimed.mean())
    staked = float(stake.sum())
    total_pnl = float(pnl.sum())
    return {
        "n": n,
        "wins": wins,
        "win_rate": round(win_rate, 4),
        "win_rate_se": round(proportion_se(wins, n), 4),
        "claimed_mean": round(claimed_mean, 4),
        "realized_mean": round(win_rate, 4),
        "overconfidence_pp": round((claimed_mean - win_rate) * 100.0, 2),
        "pnl": round(total_pnl, 4),
        "staked": round(staked, 2),
        "ros_pct": round(total_pnl / staked * 100.0, 4) if staked > 0 else 0.0,
        "max_dd_abs": round(_max_drawdown_abs(pnl[order]), 4),
        "reliability": reliability_table(records, bins),
    }


def _group(records: list[dict], key) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in records:
        out.setdefault(str(key(r)), []).append(r)
    return out


def summarize(records: list[dict], *, portfolio_dd_pct: float | None = None,
              bins=DEFAULT_BINS) -> dict:
    """Full honest summary of a per-bet stream."""
    summary = {
        "overall": slice_metrics(records, bins),
        "by_strategy": {
            k: slice_metrics(v, bins)
            for k, v in sorted(_group(records, lambda r: r["strategy"]).items())
        },
        "by_unit": {
            k: slice_metrics(v, bins)
            for k, v in sorted(_group(records, lambda r: r.get("bracket_unit", "?")).items())
        },
        "by_strategy_unit": {
            k: slice_metrics(v, bins)
            for k, v in sorted(
                _group(records, lambda r: f'{r["strategy"]}/{r.get("bracket_unit", "?")}').items()
            )
        },
    }
    if portfolio_dd_pct is not None:
        summary["portfolio_dd_pct"] = round(float(portfolio_dd_pct), 4)
    return summary


# --------------------------------------------------------------------------- meta join

def attach_meta(records: list[dict], df: pd.DataFrame) -> list[dict]:
    """Fill station / bracket bounds / unit onto each record via its row_idx."""
    if not records:
        return records
    station = df["station_id"].to_numpy()
    md = df["market_date"].to_numpy()
    bidx = df["bracket_index"].to_numpy() if "bracket_index" in df.columns else None
    bkind = df["bracket_kind"].to_numpy() if "bracket_kind" in df.columns else None
    blabel = df["bracket_label"].to_numpy() if "bracket_label" in df.columns else None
    lo_c = df["lo_c"].to_numpy() if "lo_c" in df.columns else None
    hi_c = df["hi_c"].to_numpy() if "hi_c" in df.columns else None
    for r in records:
        i = int(r["row_idx"])
        r["station_id"] = str(station[i])
        r.setdefault("market_date", str(md[i]))
        r["market_date"] = str(md[i])
        if bidx is not None:
            r["bracket_index"] = int(bidx[i])
        if bkind is not None:
            r["bracket_kind"] = str(bkind[i])
        label = str(blabel[i]) if blabel is not None else None
        r["bracket_label"] = label
        r["bracket_unit"] = unit_from_label(label)
        if lo_c is not None:
            r["lo_c"] = float(lo_c[i])
        if hi_c is not None:
            r["hi_c"] = float(hi_c[i])
    return records


# --------------------------------------------------------------------------- persistence

def persist_bets(records: list[dict], path: str | Path, *, period: str | None = None) -> Path:
    """Write the per-bet stream to a parquet at `path`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for r in records:
        row = {c: r.get(c) for c in BET_COLUMNS}
        if period is not None and row.get("period") in (None, ""):
            row["period"] = period
        rows.append(row)
    frame = pd.DataFrame(rows, columns=BET_COLUMNS)
    if "entry_ts" in frame:
        frame["entry_ts"] = pd.to_numeric(frame["entry_ts"], errors="coerce").astype("Int64")
    if "won" in frame:
        frame["won"] = frame["won"].astype("boolean")
    frame.to_parquet(path, index=False)
    return path


# --------------------------------------------------------------------------- rendering

def _fmt_slice(name: str, m: dict) -> str:
    if m["n"] == 0:
        return f"  {name:<12} n=   0  (no bets)"
    return (
        f"  {name:<12} n={m['n']:>4}  "
        f"win={m['win_rate']*100:>5.1f}%+/-{m['win_rate_se']*100:>4.1f}  "
        f"claim={m['claimed_mean']*100:>5.1f}%  real={m['realized_mean']*100:>5.1f}%  "
        f"overconf={m['overconfidence_pp']:>+5.1f}pp  "
        f"ROS={m['ros_pct']:>+6.2f}%  pnl=${m['pnl']:>+9.2f}  "
        f"staked=${m['staked']:>9.0f}  maxDD=${m['max_dd_abs']:>8.2f}"
    )


def _fmt_reliability(rel: list[dict], indent: str = "    ") -> list[str]:
    if not rel:
        return [f"{indent}(no bets)"]
    lines = [f"{indent}{'claimed bin':>13} {'n':>5} {'claimed':>8} {'realized':>9} {'gap_pp':>7}"]
    for row in rel:
        lines.append(
            f"{indent}[{row['bin_lo']:.2f},{row['bin_hi']:.2f}] "
            f"{row['n']:>5} {row['claimed_mean']*100:>7.1f}% "
            f"{row['realized_freq']*100:>8.1f}% {row['gap_pp']:>+7.1f}"
        )
    return lines


def render_text(summary: dict, title: str = "HONEST REPORT") -> str:
    lines = ["=" * 100, title, "=" * 100]
    o = summary["overall"]
    lines.append(_fmt_slice("OVERALL", o))
    if "portfolio_dd_pct" in summary:
        lines.append(f"  (portfolio bankroll max drawdown: {summary['portfolio_dd_pct']:.2f}% of peak)")
    lines.append("")
    lines.append("By strategy:")
    for name, m in summary["by_strategy"].items():
        lines.append(_fmt_slice(name, m))
    lines.append("")
    lines.append("By bracket unit (C=non-US 1C brackets, F=US 2F brackets):")
    for name, m in summary["by_unit"].items():
        lines.append(_fmt_slice(name, m))
    lines.append("")
    lines.append("By strategy x unit:")
    for name, m in summary["by_strategy_unit"].items():
        lines.append(_fmt_slice(name, m))
    lines.append("")
    lines.append("Reliability (OVERALL): claimed P(win) bin -> realized frequency")
    lines.extend(_fmt_reliability(o["reliability"]))
    for name, m in summary["by_strategy"].items():
        if m["n"] == 0:
            continue
        lines.append("")
        lines.append(f"Reliability ({name}):")
        lines.extend(_fmt_reliability(m["reliability"]))
    lines.append("=" * 100)
    return "\n".join(lines)
