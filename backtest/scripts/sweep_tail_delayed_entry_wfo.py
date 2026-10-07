"""Sweep delayed TAIL entries after the normal TAIL signal is satisfied.

The signal/gate stack is unchanged. For TAIL only, this waits for the YES
price history to trade at or below a threshold before entering. NO entries are
left unchanged so the result isolates TAIL entry timing.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backtest.scripts.lut_range_chunks import Chunk, date_chunks, tp_sl_from_config  # noqa: E402
from backtest.scripts.measure_tp_sl import (  # noqa: E402
    RANKING_BR,
    _live_config_from_spec,
    load_metrics_by_slug,
    load_prices_by_slug_side,
    metrics_at,
)
from backtest.scripts.sweep_l2_depth_features import MARKET_DB, build_bets_for_config, record_columns  # noqa: E402
from backtest.scripts.sweep_l2_sizing_walkforward import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_PARQUET,
    _chunk_label_for_date,
    simulate_window,
)

OUT_CSV = REPO_ROOT / "backtest" / "results" / "tail_delayed_entry_wfo.csv"
OUT_MD = REPO_ROOT / "backtest" / "results" / "tail_delayed_entry_wfo.md"

THRESHOLDS = (None, 0.03, 0.02, 0.01)


def _label(threshold: float | None) -> str:
    if threshold is None:
        return "immediate"
    return f"wait_le_{int(round(threshold * 100)):02d}c"


def _size_fraction(config: dict) -> dict[str, float]:
    return {
        "NO": float(config["NO"]["size_frac"]),
        "TAIL": float(config["TAIL"]["size_frac"]),
        "YMID": 0.0,
        "YHIGH": 0.0,
    }


def _find_delayed_entry(
    *,
    prices: list[tuple[int, float]],
    entry_ts: int,
    close_ts: int,
    threshold: float,
) -> tuple[int, float] | None:
    if not prices:
        return None
    keys = [ts for ts, _price in prices]
    start = bisect.bisect_left(keys, entry_ts)
    latest_entry_ts = close_ts - 4 * 60 * 60
    for ts, price in prices[start:]:
        if ts > latest_entry_ts:
            return None
        if float(price) <= threshold:
            return int(ts), float(price)
    return None


def _delayed_bets(
    *,
    bets: list[tuple],
    records: list[dict],
    prices_by_ss: dict,
    metrics_idx: dict,
    threshold: float | None,
) -> tuple[list[tuple], dict[str, float | int]]:
    if threshold is None:
        tail = [bet for bet in bets if str(bet[8]) == "TAIL"]
        return list(bets), {
            "tail_signals": len(tail),
            "tail_entered": len(tail),
            "tail_missed": 0,
            "avg_delay_minutes": 0.0,
            "max_delay_hours": 0.0,
        }

    out: list[tuple] = []
    tail_signals = 0
    tail_entered = 0
    tail_missed = 0
    delays: list[int] = []

    for bet in bets:
        if str(bet[8]) != "TAIL":
            out.append(bet)
            continue

        tail_signals += 1
        date, i, side, _fp, signal_p, _liq, _spread, will_win, strat, entry_ts = bet[:10]
        rec = records[int(i)]
        slug = str(rec["market_slug"])
        close_ts = int(rec["close_ts_unix"])
        entry_ts_i = int(entry_ts)
        prices = prices_by_ss.get((slug, "Yes"), [])
        hit = _find_delayed_entry(
            prices=prices,
            entry_ts=entry_ts_i,
            close_ts=close_ts,
            threshold=threshold,
        )
        if hit is None:
            tail_missed += 1
            continue

        delayed_ts, delayed_price = hit
        _vol, liq, spread = metrics_at(metrics_idx, slug, delayed_ts)
        out.append((
            date,
            i,
            side,
            delayed_price,
            signal_p,
            liq,
            spread,
            will_win,
            strat,
            delayed_ts,
        ))
        tail_entered += 1
        delays.append(max(0, delayed_ts - entry_ts_i))

    out.sort(key=lambda b: (b[0], b[9] if len(b) >= 10 else 0, b[1], b[8]))
    return out, {
        "tail_signals": tail_signals,
        "tail_entered": tail_entered,
        "tail_missed": tail_missed,
        "avg_delay_minutes": round(sum(delays) / len(delays) / 60.0, 4) if delays else 0.0,
        "max_delay_hours": round(max(delays) / 3600.0, 4) if delays else 0.0,
    }


def _run_window(
    *,
    bets: list[tuple],
    records: list[dict],
    config: dict,
    prices_by_ss: dict,
    metrics_idx: dict,
    chunks: list[Chunk],
    start: str,
    end: str,
) -> dict:
    return simulate_window(
        bets=bets,
        prices_by_ss=prices_by_ss,
        metrics_idx=metrics_idx,
        records=records,
        cfg=_live_config_from_spec(config, RANKING_BR),
        tp_sl=tp_sl_from_config(config),
        window_start=start,
        window_end=end,
        size_frac=_size_fraction(config),
        max_l2_ask_premium=(
            float(config["max_l2_ask_premium"])
            if config.get("max_l2_ask_premium") is not None else None
        ),
        book_cap_frac=None,
        cap_basis="equity",
        chunk_lookup=lambda date: _chunk_label_for_date(chunks, date),
    )


def _row_metrics(
    *,
    result: dict,
    chunks: list[Chunk],
    threshold: float | None,
    delay_stats: dict[str, float | int],
) -> dict:
    path_chunks = result.get("path_chunks", {})
    chunk_pnls = [float(path_chunks.get(chunk.label, {}).get("pnl", 0.0)) for chunk in chunks[1:]]
    per = result.get("per_strat", {})
    no = per.get("NO", {})
    tail = per.get("TAIL", {})
    tail_n = int(tail.get("n", 0))
    row = {
        "variant": _label(threshold),
        "tail_entry_threshold": "" if threshold is None else threshold,
        "oos_pnl": round(float(result["pnl"]), 4),
        "oos_final_bankroll": round(float(result["final_bankroll"]), 4),
        "oos_max_dd": round(float(result["max_dd_pct_peak"]) * 100.0, 4),
        "oos_positive_chunks": sum(1 for pnl in chunk_pnls if pnl > 0),
        "oos_worst_chunk": round(min(chunk_pnls) if chunk_pnls else 0.0, 4),
        "oos_n": int(result["n"]),
        "oos_no_n": int(no.get("n", 0)),
        "oos_no_pnl": round(float(no.get("pnl", 0.0)), 4),
        "oos_tail_n": tail_n,
        "oos_tail_pnl": round(float(tail.get("pnl", 0.0)), 4),
        "oos_tail_avg_stake": round(
            float(tail.get("stake_total", 0.0)) / tail_n, 4
        ) if tail_n else 0.0,
        "oos_max_open_exposure_pct": round(float(result["max_open_exposure_pct"]), 4),
    }
    row.update(delay_stats)
    return row


def _passes_gates(row: dict) -> bool:
    return (
        int(row["oos_positive_chunks"]) >= 3
        and float(row["oos_max_dd"]) <= 30.0
        and int(row["oos_tail_n"]) >= 5
    )


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_md(path: Path, rows: list[dict], baseline: dict, chunks: list[Chunk]) -> None:
    # `rows` arrives pre-ranked from run(); run() is the single owner of the order.
    lines = [
        "# TAIL Delayed Entry WFO Sweep",
        "",
        (
            "Protocol: normal TAIL condition must be satisfied first; then TAIL waits "
            "for YES price history to hit the threshold before entry. NO is unchanged. "
            "Delayed entries must still be at least 4h before bracket close. B-D is "
            "one continuous OOS bankroll path."
        ),
        "",
        "## Chunks",
        "",
    ]
    for chunk in chunks:
        lines.append(f"- {chunk.label}: {chunk.start} to {chunk.end}")
    lines += [
        "",
        "## Baseline",
        "",
        (
            f"- Immediate TAIL: PnL ${float(baseline['oos_pnl']):+.2f}; "
            f"TAIL ${float(baseline['oos_tail_pnl']):+.2f} on "
            f"{baseline['oos_tail_n']} bets; max DD {float(baseline['oos_max_dd']):.2f}%."
        ),
        "",
        "## Variants",
        "",
        "| rank | variant | PnL | maxDD | worst chunk | n | NO PnL | TAIL n | TAIL PnL | entered/missed | avg delay |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(rows, start=1):
        lines.append(
            f"| {rank} | {row['variant']} | ${float(row['oos_pnl']):+.2f} | "
            f"{float(row['oos_max_dd']):.2f}% | ${float(row['oos_worst_chunk']):+.2f} | "
            f"{row['oos_n']} | ${float(row['oos_no_pnl']):+.2f} | {row['oos_tail_n']} | "
            f"${float(row['oos_tail_pnl']):+.2f} | {row['tail_entered']}/{row['tail_missed']} | "
            f"{float(row['avg_delay_minutes']):.1f}m |"
        )
    lines += [
        "",
        "## Deployment Decision",
        "",
        (
            "Live TAIL keeps the signal band at YES ask `<= 0.03`, then blocks "
            "sizing and execution until the current YES ask is `<= 0.02`."
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run() -> dict:
    config = json.loads(DEFAULT_CONFIG.read_text())
    df = pd.read_parquet(DEFAULT_PARQUET)
    records = df[record_columns(df)].to_dict("records")
    chunks = date_chunks(df["market_date"], 4)
    base_bets, conflict_stats = build_bets_for_config(
        df=df,
        config=config,
        apply_tail_delay=False,
    )
    prices_by_ss = load_prices_by_slug_side(MARKET_DB)
    metrics_idx = load_metrics_by_slug(MARKET_DB)

    rows: list[dict] = []
    baseline_row: dict | None = None
    for threshold in THRESHOLDS:
        bets, delay_stats = _delayed_bets(
            bets=base_bets,
            records=records,
            prices_by_ss=prices_by_ss,
            metrics_idx=metrics_idx,
            threshold=threshold,
        )
        result = _run_window(
            bets=bets,
            records=records,
            config=config,
            prices_by_ss=prices_by_ss,
            metrics_idx=metrics_idx,
            chunks=chunks,
            start=chunks[1].start,
            end=chunks[-1].end,
        )
        row = _row_metrics(
            result=result,
            chunks=chunks,
            threshold=threshold,
            delay_stats=delay_stats,
        )
        row["passes_gates"] = _passes_gates(row)
        rows.append(row)
        if threshold is None:
            baseline_row = row

    if baseline_row is None:
        baseline_row = rows[0]
    ranked = sorted(rows, key=lambda r: (_passes_gates(r), float(r["oos_pnl"])), reverse=True)
    _write_csv(OUT_CSV, ranked)
    _write_md(OUT_MD, ranked, baseline_row, chunks)
    best = next((row for row in ranked if row["passes_gates"]), ranked[0])
    return {
        "n_variants": len(rows),
        "conflict_stats": conflict_stats,
        "baseline": baseline_row,
        "best": best,
        "out_csv": str(OUT_CSV),
        "out_md": str(OUT_MD),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(run(), sort_keys=True))


if __name__ == "__main__":
    main()
