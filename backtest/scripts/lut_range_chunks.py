"""Candidate #1 LUT-range chunk diagnostic.

Sweeps contiguous ranges of the walk-forward LUT observed hit rate
(`hits_cum / n_cum`) and replays Candidate #1 on four chronological chunks.

The goal is not to optimize a new strategy in the old broad-search sense. It is
to answer one narrow question: does Candidate #1 get more robust if we only bet
when the row's LUT-observed YES probability falls inside a particular range?

Run:
    python backtest/lut_range_chunks.py
    python backtest/lut_range_chunks.py --step 0.02 --top 25
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backtest.lib.live_match_eval import candidates_3strats  # noqa: E402
from backtest.scripts.measure_tp_sl import (  # noqa: E402
    CONFIG_PATH,
    MARKET_DB,
    PARQUET_OUT,
    RANKING_BR,
    STRATEGIES,
    _candidate_kwargs,
    _live_config_from_spec,
    _size_fracs,
    _stake_caps_for_bankroll,
    _strategy_config,
    _tail_delayed_entry_threshold,
    apply_tail_delayed_entry,
    load_metrics_by_slug,
    load_prices_by_slug_side,
    simulate,
)

OUT_CSV = REPO_ROOT / "backtest" / "results" / "lut_range_chunks.csv"
OUT_MD = REPO_ROOT / "backtest" / "results" / "lut_range_chunks.md"


@dataclass(frozen=True)
class Chunk:
    label: str
    start: str
    end: str


def probability_ranges(step: float) -> list[tuple[float, float]]:
    """All contiguous probability windows over a fixed grid."""
    if step <= 0 or step > 1:
        raise ValueError("--step must be in (0, 1]")
    n_steps = round(1.0 / step)
    if not math.isclose(n_steps * step, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("--step must divide 1.0 exactly")

    points = [round(i * step, 10) for i in range(n_steps + 1)]
    ranges: list[tuple[float, float]] = []
    for i, lo in enumerate(points[:-1]):
        for hi in points[i + 1:]:
            ranges.append((lo, hi))
    return ranges


def date_chunks(market_dates: Iterable[str], n_chunks: int = 4) -> list[Chunk]:
    """Split available market dates into n chronological chunks."""
    if n_chunks < 1:
        raise ValueError("n_chunks must be >= 1")
    dates = sorted({str(d) for d in market_dates})
    if not dates:
        raise ValueError("no market dates available")

    split = np.array_split(np.array(dates, dtype=object), n_chunks)
    chunks: list[Chunk] = []
    for offset, part in enumerate(split):
        if len(part) == 0:
            continue
        chunks.append(Chunk(chr(ord("A") + offset), str(part[0]), str(part[-1])))
    return chunks


def lut_observed_yes(df: pd.DataFrame) -> np.ndarray:
    """Pure walk-forward LUT observed YES probability for each decision row."""
    n = df["n_cum"].fillna(0).to_numpy(dtype=float)
    hits = df["hits_cum"].fillna(0).to_numpy(dtype=float)
    out = np.full(len(df), np.nan, dtype=float)
    np.divide(hits, n, out=out, where=n > 0)
    return out


def in_range(values: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Half-open [lo, hi), except hi=1.0 is right-inclusive."""
    if hi >= 1.0:
        return ~np.isnan(values) & (values >= lo) & (values <= hi)
    return ~np.isnan(values) & (values >= lo) & (values < hi)


def bet_lut_value(bet: tuple, lut_yes: np.ndarray, mode: str) -> float:
    """Return the LUT value used to filter a generated bet."""
    row_idx = int(bet[1])
    yes_prob = float(lut_yes[row_idx])
    if mode == "yes":
        return yes_prob
    if mode == "bet":
        side = bet[2]
        return yes_prob if side == "YES" else 1.0 - yes_prob
    raise ValueError(f"unknown LUT mode: {mode}")


def filter_bets_by_lut(
    bets: list[tuple],
    lut_yes: np.ndarray,
    lo: float,
    hi: float,
    mode: str,
) -> list[tuple]:
    values = np.array([bet_lut_value(bet, lut_yes, mode) for bet in bets], dtype=float)
    mask = in_range(values, lo, hi)
    return [bet for bet, keep in zip(bets, mask) if bool(keep)]


def tp_sl_from_config(config: dict) -> dict[str, tuple[float | None, float | None]]:
    out: dict[str, tuple[float | None, float | None]] = {}
    for strat in STRATEGIES:
        s = _strategy_config(config, strat)
        tp = s.get("tp")
        sl = s.get("sl")
        out[strat] = (
            float(tp) if tp is not None else None,
            float(sl) if sl is not None else None,
        )
    return out


def chunk_result_fields(prefix: str, result: dict) -> dict[str, float | int]:
    per = result.get("per_strat", {})
    return {
        f"{prefix}_n": int(result["n"]),
        f"{prefix}_pnl": round(float(result["pnl"]), 4),
        f"{prefix}_dd": round(float(result["max_dd_pct_peak"]) * 100, 4),
        f"{prefix}_no_n": int(per.get("NO", {}).get("n", 0)),
        f"{prefix}_no_pnl": round(float(per.get("NO", {}).get("pnl", 0.0)), 4),
        f"{prefix}_tail_n": int(per.get("TAIL", {}).get("n", 0)),
        f"{prefix}_tail_pnl": round(float(per.get("TAIL", {}).get("pnl", 0.0)), 4),
    }


def score_range(
    *,
    lo: float,
    hi: float,
    bets: list[tuple],
    lut_yes: np.ndarray,
    lut_mode: str,
    chunks: list[Chunk],
    prices_by_ss: dict,
    metrics_idx: dict,
    df_records: list[dict],
    cfg,
    tp_sl: dict[str, tuple[float | None, float | None]],
    size_frac: dict[str, float],
    max_stake: dict[str, float],
) -> dict:
    filtered = filter_bets_by_lut(bets, lut_yes, lo, hi, lut_mode)
    row: dict[str, float | int | str] = {
        "range": f"{lo:.2f}-{hi:.2f}",
        "lo": lo,
        "hi": hi,
    }
    total_pnl = 0.0
    total_n = 0
    worst_pnl = None
    positive_chunks = 0
    min_chunk_n = None

    for chunk in chunks:
        result = simulate(
            filtered,
            prices_by_ss,
            metrics_idx,
            df_records,
            cfg,
            tp_sl,
            chunk.start,
            chunk.end,
            size_frac=size_frac,
            max_stake=max_stake,
        )
        row.update(chunk_result_fields(chunk.label, result))
        pnl = float(result["pnl"])
        n = int(result["n"])
        total_pnl += pnl
        total_n += n
        worst_pnl = pnl if worst_pnl is None else min(worst_pnl, pnl)
        positive_chunks += 1 if pnl > 0 else 0
        min_chunk_n = n if min_chunk_n is None else min(min_chunk_n, n)

    row["total_n"] = total_n
    row["total_pnl"] = round(total_pnl, 4)
    row["pnl_per_bet"] = round(total_pnl / total_n, 4) if total_n else 0.0
    row["worst_chunk_pnl"] = round(float(worst_pnl or 0.0), 4)
    row["positive_chunks"] = positive_chunks
    row["min_chunk_n"] = int(min_chunk_n or 0)
    return row


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(
    *,
    rows: list[dict],
    baseline: dict,
    chunks: list[Chunk],
    path: Path,
    top: int,
    min_chunk_n: int,
) -> None:
    filtered = [r for r in rows if r["min_chunk_n"] >= min_chunk_n]
    top_rows = filtered[:top]
    lines = [
        "# Candidate #1 LUT Range Chunk Scan",
        "",
        "Ranks contiguous LUT-observed YES-probability ranges by robust A/B/C/D chunk performance.",
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
        f"- n={baseline['total_n']} pnl=${baseline['total_pnl']:+.2f} "
        f"worst_chunk=${baseline['worst_chunk_pnl']:+.2f} "
        f"positive_chunks={baseline['positive_chunks']}/{len(chunks)}",
        "",
        "## Top Ranges",
        "",
        "| range | n | pnl | pnl/bet | worst chunk | positive chunks | min chunk n |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in top_rows:
        lines.append(
            f"| {row['range']} | {row['total_n']} | ${row['total_pnl']:+.2f} | "
            f"${row['pnl_per_bet']:+.2f} | ${row['worst_chunk_pnl']:+.2f} | "
            f"{row['positive_chunks']}/{len(chunks)} | {row['min_chunk_n']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=float, default=0.02, help="probability grid step")
    parser.add_argument("--bankroll", type=float, default=RANKING_BR)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--min-chunk-n", type=int, default=20)
    parser.add_argument(
        "--lut-mode",
        choices=("yes", "bet"),
        default="yes",
        help="'yes' filters by bracket YES LUT; 'bet' uses NO-side complement for NO bets",
    )
    parser.add_argument("--csv", type=Path, default=OUT_CSV)
    parser.add_argument("--md", type=Path, default=OUT_MD)
    parser.add_argument("--no-write-md", action="store_true")
    args = parser.parse_args()

    config = json.loads(CONFIG_PATH.read_text())
    df = pd.read_parquet(PARQUET_OUT)
    chunks = date_chunks(df["market_date"], n_chunks=4)
    lut_yes = lut_observed_yes(df)
    df_records = df[["market_slug", "entry_ts_unix", "close_ts_unix"]].to_dict("records")

    prices_by_ss = load_prices_by_slug_side(MARKET_DB)
    metrics_idx = load_metrics_by_slug(MARKET_DB)
    cfg = _live_config_from_spec(config, args.bankroll)
    kw = _candidate_kwargs(config)
    bets = candidates_3strats(df, cfg, **kw)
    bets, tail_delayed_entry_stats = apply_tail_delayed_entry(
        bets=bets,
        records=df_records,
        prices_by_ss=prices_by_ss,
        metrics_idx=metrics_idx,
        threshold=_tail_delayed_entry_threshold(config),
    )
    bets.sort(key=lambda b: (b[0], b[9] if len(b) >= 10 else 0, b[1], b[8]))

    common = dict(
        bets=bets,
        lut_yes=lut_yes,
        lut_mode=args.lut_mode,
        chunks=chunks,
        prices_by_ss=prices_by_ss,
        metrics_idx=metrics_idx,
        df_records=df_records,
        cfg=cfg,
        tp_sl=tp_sl_from_config(config),
        size_frac=_size_fracs(config),
        max_stake=_stake_caps_for_bankroll(config, args.bankroll),
    )

    baseline = score_range(lo=0.0, hi=1.0, **common)
    rows = [score_range(lo=lo, hi=hi, **common) for lo, hi in probability_ranges(args.step)]
    rows.sort(
        key=lambda r: (
            int(r["positive_chunks"]),
            float(r["worst_chunk_pnl"]),
            float(r["total_pnl"]),
            int(r["total_n"]),
        ),
        reverse=True,
    )

    write_csv(rows, args.csv)
    if not args.no_write_md:
        write_markdown(
            rows=rows,
            baseline=baseline,
            chunks=chunks,
            path=args.md,
            top=args.top,
            min_chunk_n=args.min_chunk_n,
        )

    eligible = [r for r in rows if int(r["min_chunk_n"]) >= args.min_chunk_n]
    top_rows = eligible[:args.top]
    print(json.dumps({
        "bankroll": args.bankroll,
        "lut_mode": args.lut_mode,
        "step": args.step,
        "chunks": [chunk.__dict__ for chunk in chunks],
        "baseline": baseline,
        "tail_delayed_entry": tail_delayed_entry_stats,
        "top": top_rows,
        "csv": str(args.csv),
        "md": None if args.no_write_md else str(args.md),
    }))


if __name__ == "__main__":
    main()
