"""Walk-forward sizing sweep for the L2 champion.

This keeps the current L2 signal/gate stack fixed and varies only the NO and
TAIL capital fractions. Selection is leakage-safe:

    train A     -> select size -> test B
    train A+B   -> select size -> test C
    train A+B+C -> select size -> test D

Chunk A is training-only. Fixed A-D replay rows are diagnostics only and must
not be used as the decision source.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Callable, Mapping
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backtest.lib.live_match_eval import (  # noqa: E402
    ask_ladder_for_record,
    execution_min_edge_for_strategy,
    fee,
    simulate_walk_book,
)
from backtest.scripts.lut_range_chunks import Chunk, date_chunks, tp_sl_from_config  # noqa: E402
from backtest.scripts.measure_tp_sl import (  # noqa: E402
    RANKING_BR,
    _live_config_from_spec,
    find_exit_trigger,
    exit_walk_book,
    load_metrics_by_slug,
    load_prices_by_slug_side,
    metrics_at,
)
from backtest.scripts.sweep_l2_depth_features import (  # noqa: E402
    MARKET_DB,
    build_bets_for_config,
    record_columns,
)

DEFAULT_PARQUET = REPO_ROOT / "backtest" / "data" / "decision_table_may11plus_l2.parquet"
DEFAULT_CONFIG = REPO_ROOT / "backtest" / "configs" / "candidate_l2_depth.json"
OUT_SWEEP_CSV = REPO_ROOT / "backtest" / "results" / "l2_sizing_sweep.csv"
OUT_WALK_CSV = REPO_ROOT / "backtest" / "results" / "l2_sizing_walkforward.csv"
OUT_MD = REPO_ROOT / "backtest" / "results" / "l2_sizing_walkforward.md"

CHUNKS = ("A", "B", "C", "D")
SizeFracSpec = Mapping[str, float] | Callable[[str, str], float]
BookCapSpec = float | None | Callable[[str], float | None]


@dataclass(frozen=True)
class SizeCombo:
    no_frac: float
    tail_frac: float
    book_cap_frac: float | None = None

    @property
    def name(self) -> str:
        name = f"no{int(round(self.no_frac * 1000)):03d}_tail{int(round(self.tail_frac * 1000)):03d}"
        if self.book_cap_frac is not None:
            name += f"_cap{int(round(self.book_cap_frac * 100)):03d}"
        return name


def _float_list(raw: str | None, default: list[float]) -> list[float]:
    if not raw:
        return default
    return [float(part.strip()) for part in raw.split(",") if part.strip()]


def _default_no_fracs() -> list[float]:
    return [round(v, 3) for v in np.arange(0.05, 0.161, 0.01)] + [0.18, 0.20]


def _default_tail_fracs() -> list[float]:
    return [0.020, 0.025, 0.030, 0.035, 0.040, 0.050, 0.060, 0.080]


def _book_cap_fracs(raw: str | None) -> list[float | None]:
    if raw is None:
        return [None]
    out: list[float | None] = []
    for part in raw.split(","):
        value = part.strip().lower()
        if not value:
            continue
        if value in {"none", "off", "unlimited"}:
            out.append(None)
        else:
            out.append(float(value))
    return out or [None]


def _station_set(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def _with_suffix(path: Path, suffix: str | None) -> Path:
    if not suffix:
        return path
    clean = suffix.strip().strip("_")
    if not clean:
        return path
    return path.with_name(f"{path.stem}_{clean}{path.suffix}")


def _init_strat_stats() -> dict:
    return {
        "n": 0,
        "pnl": 0.0,
        "stake_total": 0.0,
        "target_total": 0.0,
        "max_stake": 0.0,
    }


def _init_path_chunk_stats() -> dict:
    return {
        "n": 0,
        "pnl": 0.0,
        "stake_total": 0.0,
        "target_total": 0.0,
        "max_dd_pct_peak": 0.0,
        "start_bankroll": None,
        "end_bankroll": None,
        "per_strat": defaultdict(_init_strat_stats),
    }


def _size_fraction(size_frac: SizeFracSpec, date: str, strat: str) -> float:
    if callable(size_frac):
        return float(size_frac(date, strat))
    return float(size_frac.get(strat, 0.0))


def _book_cap_fraction(book_cap_frac: BookCapSpec, date: str) -> float | None:
    if callable(book_cap_frac):
        return book_cap_frac(date)
    return book_cap_frac


def _chunk_label_for_date(chunks: list[Chunk], date: str) -> str | None:
    for chunk in chunks:
        if chunk.start <= date <= chunk.end:
            return chunk.label
    return None


def _optional_float(value) -> float | None:
    if value in ("", None):
        return None
    return float(value)


def _scheduled_size_fraction(
    chunks: list[Chunk],
    schedule: Mapping[str, Mapping[str, float]],
) -> Callable[[str, str], float]:
    def _inner(date: str, strat: str) -> float:
        label = _chunk_label_for_date(chunks, date)
        if label is None:
            return 0.0
        return float(schedule.get(label, {}).get(strat, 0.0))

    return _inner


def _scheduled_book_cap(
    chunks: list[Chunk],
    schedule: Mapping[str, float | None],
) -> Callable[[str], float | None]:
    def _inner(date: str) -> float | None:
        label = _chunk_label_for_date(chunks, date)
        if label is None:
            return None
        return schedule.get(label)

    return _inner


def _max_open_exposure(events: list[tuple[int, float]]) -> float:
    open_usd = 0.0
    max_open = 0.0
    for _ts, delta in sorted(events, key=lambda item: (item[0], item[1])):
        open_usd += delta
        max_open = max(max_open, open_usd)
    return max_open


def _cap_basis_usd(cap_basis: str, *, initial_bankroll: float, capital: float) -> float:
    if cap_basis == "initial":
        return float(initial_bankroll)
    return max(float(capital), 1e-9)


def simulate_window(
    *,
    bets: list[tuple],
    prices_by_ss: dict,
    metrics_idx: dict,
    records: list[dict],
    cfg,
    tp_sl: dict[str, tuple[float | None, float | None]],
    window_start: str,
    window_end: str,
    size_frac: SizeFracSpec,
    max_l2_ask_premium: float | None,
    book_cap_frac: BookCapSpec,
    cap_basis: str,
    chunk_lookup: Callable[[str], str | None] | None = None,
    bet_log: list | None = None,
) -> dict:
    """Mirror measure_tp_sl.simulate while also tracking exposure diagnostics.

    bet_log: when a list is passed, every executed bet appends one honest
    per-bet record (backtest/lib/honest_report.py::BET_COLUMNS). Additive only.
    """
    capital = cfg.initial_bankroll_usd
    peak = capital
    pnl_list: list[float] = []
    cap_curve = [capital]
    per_strat: dict[str, dict] = defaultdict(_init_strat_stats)
    events: list[tuple[int, float]] = []
    open_target_exits: list[tuple[int, float]] = []
    open_actual_exits: list[tuple[int, float]] = []
    daily_stake: dict[str, float] = defaultdict(float)
    daily_target: dict[str, float] = defaultdict(float)
    total_target = 0.0
    total_stake = 0.0
    max_single_stake = 0.0
    max_open_target = 0.0
    max_daily_target = 0.0
    max_open_actual_pct = 0.0
    max_daily_actual_pct = 0.0
    max_open_target_pct = 0.0
    max_daily_target_pct = 0.0
    cap_skips = 0
    over100_target_entries = 0
    over100_target_dates: set[str] = set()
    path_chunks: dict[str, dict] = defaultdict(_init_path_chunk_stats)

    for bet in bets:
        if len(bet) >= 10:
            date, i, side, fp_displayed, signal_p, liq, spread, will_win, strat, entry_ts = bet[:10]
        else:
            date, i, side, fp_displayed, signal_p, liq, spread, will_win, strat = bet[:9]
            entry_ts = int(records[int(i)]["entry_ts_unix"])

        if not (window_start <= date <= window_end):
            continue
        entry_ts_i = int(entry_ts)

        date_s = str(date)
        frac = _size_fraction(size_frac, date_s, strat)
        if frac <= 0.0:
            continue

        target_usd = capital * frac
        dd = (peak - capital) / peak if peak > 1e-9 else 0.0
        if dd >= cfg.max_dd:
            target_usd *= 0.5
        if target_usd < cfg.min_bet_usd or target_usd > capital:
            continue

        cap_base = _cap_basis_usd(
            cap_basis,
            initial_bankroll=cfg.initial_bankroll_usd,
            capital=capital,
        )
        open_target_exits = [
            (exit_ts, amount) for exit_ts, amount in open_target_exits
            if exit_ts > entry_ts_i
        ]
        open_target = sum(amount for _exit_ts, amount in open_target_exits)

        cap_frac = _book_cap_fraction(book_cap_frac, date_s)
        if cap_frac is not None:
            cap_usd = cap_base * cap_frac
            if open_target + target_usd > cap_usd + 1e-9:
                cap_skips += 1
                continue
            if daily_target[str(date)] + target_usd > cap_usd + 1e-9:
                cap_skips += 1
                continue

        min_edge = execution_min_edge_for_strategy(cfg, strat)
        rec = records[int(i)]
        entry_ladder = ask_ladder_for_record(rec, str(side), entry_ts)
        if max_l2_ask_premium is not None and entry_ladder:
            best_ask = min(price for price, _shares in entry_ladder)
            if best_ask - float(fp_displayed) > max_l2_ask_premium:
                continue

        result = simulate_walk_book(
            target_usd,
            float(fp_displayed),
            float(liq),
            float(signal_p),
            cfg.poly_fee_theta,
            min_edge,
            cfg.min_bet_usd,
            entry_spread=float(spread),
            ask_ladder=entry_ladder,
        )
        if result is None:
            continue

        stake, entry_vwap, _realized_edge = result
        slug = rec["market_slug"]
        close_ts = int(rec["close_ts_unix"])
        shares = stake / entry_vwap
        entry_fee = shares * fee(cfg.poly_fee_theta, entry_vwap)

        tp, sl = tp_sl.get(strat, (None, None))
        side_key = "Yes" if side == "YES" else "No"
        prices = prices_by_ss.get((slug, side_key), ())

        trigger = find_exit_trigger(entry_vwap, entry_ts_i, close_ts, prices, tp, sl)
        if trigger is not None:
            trigger_mid, exit_ts, _reason = trigger
            _vol_x, liq_x, spr_x = metrics_at(metrics_idx, slug, int(exit_ts))
            exit_vwap = exit_walk_book(stake, trigger_mid, liq_x, spr_x)
            net = shares * (exit_vwap - entry_vwap) - entry_fee - shares * fee(
                cfg.poly_fee_theta, exit_vwap
            )
            exposure_end_ts = int(exit_ts)
        else:
            if will_win:
                net = (
                    stake * (1.0 - entry_vwap) / entry_vwap
                    - stake * fee(cfg.poly_fee_theta, entry_vwap) / entry_vwap
                )
            else:
                net = -stake - entry_fee
            exposure_end_ts = close_ts

        capital += net
        peak = max(peak, capital)
        if capital < 1.0:
            break

        current_dd = (peak - capital) / peak if peak > 1e-9 else 0.0
        pnl_list.append(net)
        cap_curve.append(capital)
        if bet_log is not None:
            bet_log.append({
                "row_idx": int(i),
                "market_date": date,
                "side": side,
                "strategy": strat,
                "claimed_p": float(signal_p),
                "entry_price": float(fp_displayed),
                "fill_vwap": float(entry_vwap),
                "stake": float(stake),
                "won": bool(will_win),
                "pnl": float(net),
                "entry_ts": int(entry_ts_i),
                "exit_reason": (trigger[2] if trigger is not None else "close"),
            })
        total_target += target_usd
        total_stake += stake
        max_single_stake = max(max_single_stake, stake)
        daily_stake[str(date)] += stake
        daily_target[str(date)] += target_usd

        open_actual_exits = [
            (exit_ts, amount) for exit_ts, amount in open_actual_exits
            if exit_ts > entry_ts_i
        ]
        open_actual_now = sum(amount for _exit_ts, amount in open_actual_exits) + stake
        max_open_actual_pct = max(max_open_actual_pct, open_actual_now / cap_base * 100.0)
        max_daily_actual_pct = max(max_daily_actual_pct, daily_stake[str(date)] / cap_base * 100.0)

        open_target_exits.append((exposure_end_ts, target_usd))
        open_actual_exits.append((exposure_end_ts, stake))
        open_target += target_usd
        max_open_target = max(max_open_target, open_target)
        max_daily_target = max(max_daily_target, daily_target[str(date)])
        open_target_pct = open_target / cap_base * 100.0
        daily_target_pct = daily_target[str(date)] / cap_base * 100.0
        max_open_target_pct = max(max_open_target_pct, open_target_pct)
        max_daily_target_pct = max(max_daily_target_pct, daily_target_pct)
        if (
            open_target_pct > 100.0 + 1e-9
            or daily_target_pct > 100.0 + 1e-9
        ):
            over100_target_entries += 1
            over100_target_dates.add(str(date))
        events.append((entry_ts_i, stake))
        events.append((exposure_end_ts, -stake))

        ps = per_strat[strat]
        ps["n"] += 1
        ps["pnl"] += net
        ps["stake_total"] += stake
        ps["target_total"] += target_usd
        ps["max_stake"] = max(ps["max_stake"], stake)

        if chunk_lookup is not None:
            label = chunk_lookup(date_s)
            if label is not None:
                chunk_stats = path_chunks[label]
                if chunk_stats["start_bankroll"] is None:
                    chunk_stats["start_bankroll"] = capital - net
                chunk_stats["end_bankroll"] = capital
                chunk_stats["n"] += 1
                chunk_stats["pnl"] += net
                chunk_stats["stake_total"] += stake
                chunk_stats["target_total"] += target_usd
                chunk_stats["max_dd_pct_peak"] = max(
                    float(chunk_stats["max_dd_pct_peak"]),
                    current_dd,
                )
                cps = chunk_stats["per_strat"][strat]
                cps["n"] += 1
                cps["pnl"] += net
                cps["stake_total"] += stake
                cps["target_total"] += target_usd
                cps["max_stake"] = max(cps["max_stake"], stake)

    if not pnl_list:
        return {
            "n": 0,
            "pnl": 0.0,
            "max_dd_pct_peak": 0.0,
            "final_bankroll": cfg.initial_bankroll_usd,
            "stake_total": 0.0,
            "target_total": 0.0,
            "avg_stake": 0.0,
            "fill_ratio": 0.0,
            "max_single_stake": 0.0,
            "max_open_exposure": 0.0,
            "max_daily_notional": 0.0,
            "max_open_target_exposure": 0.0,
            "max_daily_target_notional": 0.0,
            "max_open_exposure_pct": 0.0,
            "max_daily_notional_pct": 0.0,
            "max_open_target_pct": 0.0,
            "max_daily_target_pct": 0.0,
            "cap_skips": cap_skips,
            "over100_target_entries": over100_target_entries,
            "over100_target_dates": len(over100_target_dates),
            "per_strat": {},
            "path_chunks": {},
        }

    cap_arr = np.array(cap_curve)
    peak_arr = np.maximum.accumulate(cap_arr)
    dd_pct = float(abs(((cap_arr - peak_arr) / np.where(peak_arr > 0, peak_arr, 1.0)).min()))
    pnl_arr = np.array(pnl_list)
    max_open = _max_open_exposure(events)
    max_daily = max(daily_stake.values()) if daily_stake else 0.0
    return {
        "n": len(pnl_arr),
        "pnl": float(pnl_arr.sum()),
        "max_dd_pct_peak": dd_pct,
        "final_bankroll": float(cap_arr[-1]),
        "stake_total": total_stake,
        "target_total": total_target,
        "avg_stake": total_stake / len(pnl_arr),
        "fill_ratio": total_stake / total_target if total_target > 0 else 0.0,
        "max_single_stake": max_single_stake,
        "max_open_exposure": max_open,
        "max_daily_notional": max_daily,
        "max_open_target_exposure": max_open_target,
        "max_daily_target_notional": max_daily_target,
        "max_open_exposure_pct": max_open_actual_pct,
        "max_daily_notional_pct": max_daily_actual_pct,
        "max_open_target_pct": max_open_target_pct,
        "max_daily_target_pct": max_daily_target_pct,
        "cap_skips": cap_skips,
        "over100_target_entries": over100_target_entries,
        "over100_target_dates": len(over100_target_dates),
        "per_strat": dict(per_strat),
        "path_chunks": {
            label: {
                **stats,
                "per_strat": dict(stats["per_strat"]),
            }
            for label, stats in path_chunks.items()
        },
    }


def _chunk_fields(label: str, result: dict) -> dict[str, float | int]:
    per = result.get("per_strat", {})
    no = per.get("NO", _init_strat_stats())
    tail = per.get("TAIL", _init_strat_stats())
    return {
        f"{label}_n": int(result["n"]),
        f"{label}_pnl": round(float(result["pnl"]), 4),
        f"{label}_dd": round(float(result["max_dd_pct_peak"]) * 100.0, 4),
        f"{label}_stake_total": round(float(result["stake_total"]), 4),
        f"{label}_avg_stake": round(float(result["avg_stake"]), 4),
        f"{label}_fill_ratio": round(float(result["fill_ratio"]), 4),
        f"{label}_max_single_stake": round(float(result["max_single_stake"]), 4),
        f"{label}_max_open_exposure": round(float(result["max_open_exposure"]), 4),
        f"{label}_max_open_exposure_pct": round(float(result["max_open_exposure_pct"]), 4),
        f"{label}_max_daily_notional": round(float(result["max_daily_notional"]), 4),
        f"{label}_max_daily_notional_pct": round(float(result["max_daily_notional_pct"]), 4),
        f"{label}_max_open_target_pct": round(float(result["max_open_target_pct"]), 4),
        f"{label}_max_daily_target_pct": round(float(result["max_daily_target_pct"]), 4),
        f"{label}_cap_skips": int(result["cap_skips"]),
        f"{label}_over100_target_entries": int(result["over100_target_entries"]),
        f"{label}_over100_target_dates": int(result["over100_target_dates"]),
        f"{label}_no_n": int(no["n"]),
        f"{label}_no_pnl": round(float(no["pnl"]), 4),
        f"{label}_no_avg_stake": round(float(no["stake_total"]) / int(no["n"]), 4) if int(no["n"]) else 0.0,
        f"{label}_tail_n": int(tail["n"]),
        f"{label}_tail_pnl": round(float(tail["pnl"]), 4),
        f"{label}_tail_avg_stake": (
            round(float(tail["stake_total"]) / int(tail["n"]), 4) if int(tail["n"]) else 0.0
        ),
    }


def score_combo(
    *,
    combo: SizeCombo,
    bets: list[tuple],
    records: list[dict],
    chunks: list[Chunk],
    config: dict,
    prices_by_ss: dict,
    metrics_idx: dict,
    cap_basis: str,
) -> dict:
    cfg = _live_config_from_spec(config, RANKING_BR)
    tp_sl = tp_sl_from_config(config)
    max_l2_ask_premium = (
        float(config["max_l2_ask_premium"]) if config.get("max_l2_ask_premium") is not None else None
    )
    size_frac = {"NO": combo.no_frac, "TAIL": combo.tail_frac, "YMID": 0.0, "YHIGH": 0.0}

    row: dict[str, float | int | str] = {
        "variant": combo.name,
        "description": f"NO {combo.no_frac:.1%}, TAIL {combo.tail_frac:.1%}",
        "no_frac": round(combo.no_frac, 5),
        "tail_frac": round(combo.tail_frac, 5),
        "book_cap_frac": "" if combo.book_cap_frac is None else round(combo.book_cap_frac, 5),
        "cap_basis": cap_basis,
    }

    totals = {
        "pnl": 0.0,
        "n": 0,
        "stake_total": 0.0,
        "target_total": 0.0,
        "max_dd": 0.0,
        "max_open_exposure": 0.0,
        "max_daily_notional": 0.0,
        "max_open_exposure_pct": 0.0,
        "max_daily_notional_pct": 0.0,
        "max_open_target_pct": 0.0,
        "max_daily_target_pct": 0.0,
        "positive_chunks": 0,
        "worst_chunk_pnl": None,
        "no_pnl": 0.0,
        "tail_pnl": 0.0,
        "cap_skips": 0,
        "over100_target_entries": 0,
        "over100_target_dates": 0,
    }

    for chunk in chunks:
        result = simulate_window(
            bets=bets,
            prices_by_ss=prices_by_ss,
            metrics_idx=metrics_idx,
            records=records,
            cfg=cfg,
            tp_sl=tp_sl,
            window_start=chunk.start,
            window_end=chunk.end,
            size_frac=size_frac,
            max_l2_ask_premium=max_l2_ask_premium,
            book_cap_frac=combo.book_cap_frac,
            cap_basis=cap_basis,
        )
        row.update(_chunk_fields(chunk.label, result))
        per = result.get("per_strat", {})
        pnl = float(result["pnl"])
        totals["pnl"] += pnl
        totals["n"] += int(result["n"])
        totals["stake_total"] += float(result["stake_total"])
        totals["target_total"] += float(result["target_total"])
        totals["max_dd"] = max(float(totals["max_dd"]), float(result["max_dd_pct_peak"]) * 100.0)
        totals["max_open_exposure"] = max(
            float(totals["max_open_exposure"]), float(result["max_open_exposure"])
        )
        totals["max_daily_notional"] = max(
            float(totals["max_daily_notional"]), float(result["max_daily_notional"])
        )
        totals["max_open_exposure_pct"] = max(
            float(totals["max_open_exposure_pct"]), float(result["max_open_exposure_pct"])
        )
        totals["max_daily_notional_pct"] = max(
            float(totals["max_daily_notional_pct"]), float(result["max_daily_notional_pct"])
        )
        totals["max_open_target_pct"] = max(
            float(totals["max_open_target_pct"]), float(result["max_open_target_pct"])
        )
        totals["max_daily_target_pct"] = max(
            float(totals["max_daily_target_pct"]), float(result["max_daily_target_pct"])
        )
        totals["cap_skips"] += int(result["cap_skips"])
        totals["over100_target_entries"] += int(result["over100_target_entries"])
        totals["over100_target_dates"] += int(result["over100_target_dates"])
        totals["positive_chunks"] += 1 if pnl > 0 else 0
        totals["worst_chunk_pnl"] = (
            pnl if totals["worst_chunk_pnl"] is None else min(float(totals["worst_chunk_pnl"]), pnl)
        )
        totals["no_pnl"] += float(per.get("NO", {}).get("pnl", 0.0))
        totals["tail_pnl"] += float(per.get("TAIL", {}).get("pnl", 0.0))

    continuous_all = simulate_window(
        bets=bets,
        prices_by_ss=prices_by_ss,
        metrics_idx=metrics_idx,
        records=records,
        cfg=cfg,
        tp_sl=tp_sl,
        window_start=chunks[0].start,
        window_end=chunks[-1].end,
        size_frac=size_frac,
        max_l2_ask_premium=max_l2_ask_premium,
        book_cap_frac=combo.book_cap_frac,
        cap_basis=cap_basis,
        chunk_lookup=lambda date: _chunk_label_for_date(chunks, date),
    )
    continuous_oos = simulate_window(
        bets=bets,
        prices_by_ss=prices_by_ss,
        metrics_idx=metrics_idx,
        records=records,
        cfg=cfg,
        tp_sl=tp_sl,
        window_start=chunks[1].start,
        window_end=chunks[-1].end,
        size_frac=size_frac,
        max_l2_ask_premium=max_l2_ask_premium,
        book_cap_frac=combo.book_cap_frac,
        cap_basis=cap_basis,
        chunk_lookup=lambda date: _chunk_label_for_date(chunks, date),
    )
    all_per = continuous_all.get("per_strat", {})
    all_path_chunks = continuous_all.get("path_chunks", {})
    all_chunk_pnls = [float(all_path_chunks.get(chunk.label, {}).get("pnl", 0.0)) for chunk in chunks]
    oos_per = continuous_oos.get("per_strat", {})
    oos_path_chunks = continuous_oos.get("path_chunks", {})
    oos_chunk_pnls = [float(oos_path_chunks.get(chunk.label, {}).get("pnl", 0.0)) for chunk in chunks[1:]]

    row.update({
        "total_n": int(continuous_all["n"]),
        "total_pnl": round(float(continuous_all["pnl"]), 4),
        "total_final_bankroll": round(float(continuous_all["final_bankroll"]), 4),
        "total_stake": round(float(continuous_all["stake_total"]), 4),
        "total_fill_ratio": (
            round(float(continuous_all["fill_ratio"]), 4)
        ),
        "max_chunk_dd": round(float(continuous_all["max_dd_pct_peak"]) * 100.0, 4),
        "max_open_exposure": round(float(continuous_all["max_open_exposure"]), 4),
        "max_open_exposure_pct": round(float(continuous_all["max_open_exposure_pct"]), 4),
        "max_daily_notional": round(float(continuous_all["max_daily_notional"]), 4),
        "max_daily_notional_pct": round(float(continuous_all["max_daily_notional_pct"]), 4),
        "max_open_target_pct": round(float(continuous_all["max_open_target_pct"]), 4),
        "max_daily_target_pct": round(float(continuous_all["max_daily_target_pct"]), 4),
        "cap_skips": int(continuous_all["cap_skips"]),
        "over100_target_entries": int(continuous_all["over100_target_entries"]),
        "over100_target_dates": int(continuous_all["over100_target_dates"]),
        "positive_chunks": sum(1 for pnl in all_chunk_pnls if pnl > 0),
        "worst_chunk_pnl": round(min(all_chunk_pnls) if all_chunk_pnls else 0.0, 4),
        "no_pnl": round(float(all_per.get("NO", {}).get("pnl", 0.0)), 4),
        "tail_pnl": round(float(all_per.get("TAIL", {}).get("pnl", 0.0)), 4),
        "oos_n": int(continuous_oos["n"]),
        "oos_pnl": round(float(continuous_oos["pnl"]), 4),
        "oos_final_bankroll": round(float(continuous_oos["final_bankroll"]), 4),
        "oos_max_dd": round(float(continuous_oos["max_dd_pct_peak"]) * 100.0, 4),
        "oos_worst_chunk_pnl": round(min(oos_chunk_pnls) if oos_chunk_pnls else 0.0, 4),
        "oos_positive_chunks": sum(1 for pnl in oos_chunk_pnls if pnl > 0),
        "oos_no_pnl": round(float(oos_per.get("NO", {}).get("pnl", 0.0)), 4),
        "oos_tail_pnl": round(float(oos_per.get("TAIL", {}).get("pnl", 0.0)), 4),
        "oos_fill_ratio": round(float(continuous_oos["fill_ratio"]), 4),
        "oos_max_open_exposure_pct": round(float(continuous_oos["max_open_exposure_pct"]), 4),
        "oos_max_open_target_pct": round(float(continuous_oos["max_open_target_pct"]), 4),
        "oos_cap_skips": int(continuous_oos["cap_skips"]),
        "oos_over100_target_dates": int(continuous_oos["over100_target_dates"]),
    })
    return row


def _f(row: dict, key: str) -> float:
    return float(row.get(key) or 0.0)


def _i(row: dict, key: str) -> int:
    return int(float(row.get(key) or 0))


def _stats(row: dict, chunks: tuple[str, ...]) -> dict[str, float | int]:
    pnls = [_f(row, f"{chunk}_pnl") for chunk in chunks]
    dds = [_f(row, f"{chunk}_dd") for chunk in chunks]
    ns = [_i(row, f"{chunk}_n") for chunk in chunks]
    return {
        "total": sum(pnls),
        "worst": min(pnls) if pnls else 0.0,
        "positive": sum(1 for pnl in pnls if pnl > 0),
        "max_dd": max(dds) if dds else 0.0,
        "n": sum(ns),
        "no_pnl": sum(_f(row, f"{chunk}_no_pnl") for chunk in chunks),
        "tail_pnl": sum(_f(row, f"{chunk}_tail_pnl") for chunk in chunks),
        "max_open_exposure_pct": max(_f(row, f"{chunk}_max_open_exposure_pct") for chunk in chunks),
        "max_daily_notional_pct": max(_f(row, f"{chunk}_max_daily_notional_pct") for chunk in chunks),
        "max_open_target_pct": max(_f(row, f"{chunk}_max_open_target_pct") for chunk in chunks),
        "max_daily_target_pct": max(_f(row, f"{chunk}_max_daily_target_pct") for chunk in chunks),
        "avg_stake": (
            sum(_f(row, f"{chunk}_stake_total") for chunk in chunks) / max(sum(ns), 1)
        ),
        "cap_skips": sum(_i(row, f"{chunk}_cap_skips") for chunk in chunks),
        "over100_target_entries": sum(_i(row, f"{chunk}_over100_target_entries") for chunk in chunks),
        "over100_target_dates": sum(_i(row, f"{chunk}_over100_target_dates") for chunk in chunks),
    }


def _eligible(row: dict, train_chunks: tuple[str, ...], max_train_dd: float) -> bool:
    stats = _stats(row, train_chunks)
    return (
        float(stats["max_dd"]) <= max_train_dd
        and int(stats["positive"]) == len(train_chunks)
        and int(stats["n"]) > 0
    )


def _train_key(row: dict, train_chunks: tuple[str, ...], selector: str) -> tuple:
    stats = _stats(row, train_chunks)
    if selector == "max_pnl_dd30":
        return (
            float(stats["total"]),
            float(stats["worst"]),
            -float(stats["max_dd"]),
            -float(stats["max_open_exposure_pct"]),
        )
    if selector == "exposure_adjusted_dd30":
        return (
            float(stats["total"]) / max(float(stats["max_open_exposure_pct"]), 1e-9),
            float(stats["total"]),
            float(stats["worst"]),
            -float(stats["max_dd"]),
        )
    return (
        float(stats["total"]) / max(float(stats["max_dd"]), 1e-9),
        float(stats["worst"]),
        float(stats["total"]),
        -float(stats["max_open_exposure_pct"]),
    )


def walk_forward(rows: list[dict], *, max_train_dd: float) -> list[dict]:
    selectors = ("risk_adjusted_dd30", "max_pnl_dd30", "exposure_adjusted_dd30")
    selected: list[dict] = []
    for selector in selectors:
        for idx, test_chunk in enumerate(CHUNKS[1:], start=1):
            train_chunks = CHUNKS[:idx]
            eligible = [row for row in rows if _eligible(row, train_chunks, max_train_dd)]
            if not eligible:
                raise RuntimeError(f"no eligible size rows for {selector} on {train_chunks}")
            winner = max(eligible, key=lambda row: _train_key(row, train_chunks, selector))
            train_stats = _stats(winner, train_chunks)
            selected.append({
                "selector": selector,
                "step": f"{'+'.join(train_chunks)}->{test_chunk}",
                "train_chunks": "+".join(train_chunks),
                "test_chunk": test_chunk,
                "variant": winner["variant"],
                "description": winner["description"],
                "no_frac": winner["no_frac"],
                "tail_frac": winner["tail_frac"],
                "book_cap_frac": winner.get("book_cap_frac", ""),
                "train_total": round(float(train_stats["total"]), 4),
                "train_worst": round(float(train_stats["worst"]), 4),
                "train_positive": int(train_stats["positive"]),
                "train_max_dd": round(float(train_stats["max_dd"]), 4),
                "train_max_open_exposure_pct": round(float(train_stats["max_open_exposure_pct"]), 4),
                "train_max_daily_notional_pct": round(float(train_stats["max_daily_notional_pct"]), 4),
                "train_max_open_target_pct": round(float(train_stats["max_open_target_pct"]), 4),
                "train_max_daily_target_pct": round(float(train_stats["max_daily_target_pct"]), 4),
                "train_over100_target_dates": int(train_stats["over100_target_dates"]),
                "train_over100_target_entries": int(train_stats["over100_target_entries"]),
                "train_cap_skips": int(train_stats["cap_skips"]),
                "test_pnl": round(_f(winner, f"{test_chunk}_pnl"), 4),
                "test_dd": round(_f(winner, f"{test_chunk}_dd"), 4),
                "test_n": _i(winner, f"{test_chunk}_n"),
                "test_no_pnl": round(_f(winner, f"{test_chunk}_no_pnl"), 4),
                "test_tail_pnl": round(_f(winner, f"{test_chunk}_tail_pnl"), 4),
                "test_avg_stake": round(_f(winner, f"{test_chunk}_avg_stake"), 4),
                "test_no_avg_stake": round(_f(winner, f"{test_chunk}_no_avg_stake"), 4),
                "test_tail_avg_stake": round(_f(winner, f"{test_chunk}_tail_avg_stake"), 4),
                "test_fill_ratio": round(_f(winner, f"{test_chunk}_fill_ratio"), 4),
                "test_max_open_exposure_pct": round(_f(winner, f"{test_chunk}_max_open_exposure_pct"), 4),
                "test_max_daily_notional_pct": round(_f(winner, f"{test_chunk}_max_daily_notional_pct"), 4),
                "test_max_open_target_pct": round(_f(winner, f"{test_chunk}_max_open_target_pct"), 4),
                "test_max_daily_target_pct": round(_f(winner, f"{test_chunk}_max_daily_target_pct"), 4),
                "test_over100_target_dates": _i(winner, f"{test_chunk}_over100_target_dates"),
                "test_over100_target_entries": _i(winner, f"{test_chunk}_over100_target_entries"),
                "test_cap_skips": _i(winner, f"{test_chunk}_cap_skips"),
            })
    return selected


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


def _selector_summary(
    rows: list[dict],
    selector: str,
    *,
    bets: list[tuple],
    records: list[dict],
    chunks: list[Chunk],
    config: dict,
    prices_by_ss: dict,
    metrics_idx: dict,
    cap_basis: str,
) -> dict:
    group = [row for row in rows if row["selector"] == selector]
    cfg = _live_config_from_spec(config, RANKING_BR)
    tp_sl = tp_sl_from_config(config)
    max_l2_ask_premium = (
        float(config["max_l2_ask_premium"]) if config.get("max_l2_ask_premium") is not None else None
    )
    size_schedule = {
        row["test_chunk"]: {
            "NO": float(row["no_frac"]),
            "TAIL": float(row["tail_frac"]),
            "YMID": 0.0,
            "YHIGH": 0.0,
        }
        for row in group
    }
    cap_schedule = {
        row["test_chunk"]: _optional_float(row.get("book_cap_frac"))
        for row in group
    }
    continuous = simulate_window(
        bets=bets,
        prices_by_ss=prices_by_ss,
        metrics_idx=metrics_idx,
        records=records,
        cfg=cfg,
        tp_sl=tp_sl,
        window_start=chunks[1].start,
        window_end=chunks[-1].end,
        size_frac=_scheduled_size_fraction(chunks, size_schedule),
        max_l2_ask_premium=max_l2_ask_premium,
        book_cap_frac=_scheduled_book_cap(chunks, cap_schedule),
        cap_basis=cap_basis,
        chunk_lookup=lambda date: _chunk_label_for_date(chunks, date),
    )
    path_chunks = continuous.get("path_chunks", {})
    chunk_pnls = [float(path_chunks.get(chunk.label, {}).get("pnl", 0.0)) for chunk in chunks[1:]]
    per = continuous.get("per_strat", {})
    return {
        "selector": selector,
        "test_total": round(float(continuous["pnl"]), 4),
        "test_final_bankroll": round(float(continuous["final_bankroll"]), 4),
        "worst_test": round(min(chunk_pnls) if chunk_pnls else 0.0, 4),
        "positive_tests": sum(1 for pnl in chunk_pnls if pnl > 0),
        "max_test_dd": round(float(continuous["max_dd_pct_peak"]) * 100.0, 4),
        "test_n": int(continuous["n"]),
        "test_no_pnl": round(float(per.get("NO", {}).get("pnl", 0.0)), 4),
        "test_tail_pnl": round(float(per.get("TAIL", {}).get("pnl", 0.0)), 4),
        "max_test_open_exposure_pct": round(float(continuous["max_open_exposure_pct"]), 4),
        "max_test_daily_notional_pct": round(float(continuous["max_daily_notional_pct"]), 4),
        "max_test_open_target_pct": round(float(continuous["max_open_target_pct"]), 4),
        "max_test_daily_target_pct": round(float(continuous["max_daily_target_pct"]), 4),
        "test_over100_target_dates": int(continuous["over100_target_dates"]),
        "test_over100_target_entries": int(continuous["over100_target_entries"]),
        "test_cap_skips": int(continuous["cap_skips"]),
        "last_variant": group[-1]["variant"],
        "last_no_frac": float(group[-1]["no_frac"]),
        "last_tail_frac": float(group[-1]["tail_frac"]),
        "last_book_cap_frac": group[-1].get("book_cap_frac", ""),
    }


def write_markdown(
    *,
    sweep_rows: list[dict],
    wf_rows: list[dict],
    bets: list[tuple],
    records: list[dict],
    config: dict,
    prices_by_ss: dict,
    metrics_idx: dict,
    chunks: list[Chunk],
    max_train_dd: float,
    out_md: Path,
    excluded_stations: set[str],
    original_station_count: int,
    filtered_station_count: int,
    cap_basis: str,
) -> None:
    summaries = [
        _selector_summary(
            wf_rows,
            selector,
            bets=bets,
            records=records,
            chunks=chunks,
            config=config,
            prices_by_ss=prices_by_ss,
            metrics_idx=metrics_idx,
            cap_basis=cap_basis,
        )
        for selector in dict.fromkeys(row["selector"] for row in wf_rows)
    ]
    fixed_ranked = sorted(
        sweep_rows,
        key=lambda row: (
            float(row["oos_max_dd"]) <= max_train_dd,
            int(row["oos_positive_chunks"]),
            float(row["oos_pnl"]),
            -float(row["oos_max_dd"]),
        ),
        reverse=True,
    )
    lines = [
        "# L2 Sizing Walk-Forward Sweep",
        "",
        (
            "Protocol: sizing is selected on prior chunks only. Chunk A is training-only; "
            "headline OOS rows run one continuous bankroll path from B through D."
        ),
        "Per-step rows below remain reset-chunk diagnostics used for selection.",
        f"Hard training gate: max train DD <= {max_train_dd:.2f}%.",
        "",
        f"Decision table: `{DEFAULT_PARQUET}`",
        f"Config: `{DEFAULT_CONFIG}`",
        f"Starting bankroll: `${RANKING_BR:.0f}`",
        f"Exposure/notional cap basis: `{cap_basis}`.",
    ]
    if excluded_stations:
        lines += [
            f"Excluded stations: `{', '.join(sorted(excluded_stations))}`",
            f"Station universe: {filtered_station_count}/{original_station_count} after exclusion.",
        ]
    else:
        lines.append(f"Station universe: {filtered_station_count}.")
    lines += [
        "",
        "## Chunks",
        "",
    ]
    for chunk in chunks:
        lines.append(f"- {chunk.label}: {chunk.start} to {chunk.end}")
    lines += [
        "",
        "## Walk-Forward Summary",
        "",
        "| selector | continuous B-D PnL | final BR | worst B-D segment | +segments | max DD | bets | NO | TAIL | actual open exp | target cap used | last pre-D size |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for summary in summaries:
        cap_label = (
            "uncapped" if summary["last_book_cap_frac"] in ("", None)
            else f"cap {float(summary['last_book_cap_frac']):.0%}"
        )
        lines.append(
            f"| {summary['selector']} | ${summary['test_total']:+.2f} | "
            f"${summary['test_final_bankroll']:.2f} | "
            f"${summary['worst_test']:+.2f} | {summary['positive_tests']}/3 | "
            f"{summary['max_test_dd']:.2f}% | {summary['test_n']} | "
            f"${summary['test_no_pnl']:+.2f} | ${summary['test_tail_pnl']:+.2f} | "
            f"{summary['max_test_open_exposure_pct']:.1f}% | "
            f"{summary['max_test_open_target_pct']:.1f}% | "
            f"NO {summary['last_no_frac']:.1%}, TAIL {summary['last_tail_frac']:.1%}, {cap_label} |"
        )
    lines += [
        "",
        "## Walk-Forward Steps",
        "",
        "These step rows show the reset-chunk tests used to choose the next size.",
        "",
        "| selector | step | selected size | train pnl | train maxDD | train target cap | test pnl | test DD | test avg stake | actual open exp | target cap used |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in wf_rows:
        cap_label = "uncapped" if row.get("book_cap_frac", "") in ("", None) else f"cap {float(row['book_cap_frac']):.0%}"
        lines.append(
            f"| {row['selector']} | {row['step']} | NO {float(row['no_frac']):.1%}, "
            f"TAIL {float(row['tail_frac']):.1%}, {cap_label} | ${float(row['train_total']):+.2f} | "
            f"{float(row['train_max_dd']):.2f}% | "
            f"{float(row['train_max_open_target_pct']):.1f}% | "
            f"${float(row['test_pnl']):+.2f} | {float(row['test_dd']):.2f}% | "
            f"${float(row['test_avg_stake']):.2f} | "
            f"{float(row['test_max_open_exposure_pct']):.1f}% | "
            f"{float(row['test_max_open_target_pct']):.1f}% |"
        )
    lines += [
        "",
        "## Fixed B-D Continuous Diagnostics",
        "",
        "These rows replay one fixed size from B through D without resetting bankroll. They are diagnostics, not the selector source.",
        "",
        "| rank | size | B-D PnL | final BR | maxDD | n | fill | actual open exp | target cap used | NO | TAIL |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(fixed_ranked[:20], start=1):
        cap_label = "uncapped" if row.get("book_cap_frac", "") in ("", None) else f"cap {float(row['book_cap_frac']):.0%}"
        lines.append(
            f"| {rank} | NO {float(row['no_frac']):.1%}, TAIL {float(row['tail_frac']):.1%}, {cap_label} | "
            f"${float(row['oos_pnl']):+.2f} | ${float(row['oos_final_bankroll']):.2f} | "
            f"{float(row['oos_max_dd']):.2f}% | "
            f"{row['oos_n']} | {float(row['oos_fill_ratio']):.2f} | "
            f"{float(row['oos_max_open_exposure_pct']):.1f}% | "
            f"{float(row['oos_max_open_target_pct']):.1f}% | "
            f"${float(row['oos_no_pnl']):+.2f} | ${float(row['oos_tail_pnl']):+.2f} |"
        )
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-fracs", help="Comma-separated NO capital fractions.")
    parser.add_argument("--tail-fracs", help="Comma-separated TAIL capital fractions.")
    parser.add_argument(
        "--book-cap-fracs",
        help="Comma-separated target exposure/notional caps. Use 'none' for uncapped.",
    )
    parser.add_argument(
        "--cap-basis",
        choices=("equity", "initial"),
        default="equity",
        help=(
            "Basis for target exposure/notional caps and pct diagnostics. "
            "equity matches live stake_basis_capital; initial preserves old diagnostics."
        ),
    )
    parser.add_argument(
        "--exclude-stations",
        help="Comma-separated station IDs to remove before signal generation.",
    )
    parser.add_argument(
        "--out-suffix",
        help="Suffix for result filenames, so diagnostic reruns do not overwrite defaults.",
    )
    parser.add_argument("--max-train-dd", type=float, default=30.0)
    args = parser.parse_args()

    config = json.loads(DEFAULT_CONFIG.read_text())
    df = pd.read_parquet(DEFAULT_PARQUET)
    original_station_count = int(df["station_id"].nunique())
    excluded_stations = _station_set(args.exclude_stations)
    if excluded_stations:
        df = df.loc[~df["station_id"].astype(str).str.upper().isin(excluded_stations)].copy()
    filtered_station_count = int(df["station_id"].nunique())
    records = df[record_columns(df)].to_dict("records")
    chunks = date_chunks(df["market_date"], 4)
    prices_by_ss = load_prices_by_slug_side(MARKET_DB)
    metrics_idx = load_metrics_by_slug(MARKET_DB)
    bets, conflict_stats = build_bets_for_config(
        df=df,
        config=config,
        records=records,
        prices_by_ss=prices_by_ss,
        metrics_idx=metrics_idx,
    )

    combos = [
        SizeCombo(no_frac=no_frac, tail_frac=tail_frac, book_cap_frac=book_cap_frac)
        for no_frac in _float_list(args.no_fracs, _default_no_fracs())
        for tail_frac in _float_list(args.tail_fracs, _default_tail_fracs())
        for book_cap_frac in _book_cap_fracs(args.book_cap_fracs)
    ]
    sweep_rows = [
        score_combo(
            combo=combo,
            bets=bets,
            records=records,
            chunks=chunks,
            config=config,
            prices_by_ss=prices_by_ss,
            metrics_idx=metrics_idx,
            cap_basis=args.cap_basis,
        )
        for combo in combos
    ]
    wf_rows = walk_forward(sweep_rows, max_train_dd=args.max_train_dd)

    out_sweep_csv = _with_suffix(OUT_SWEEP_CSV, args.out_suffix)
    out_walk_csv = _with_suffix(OUT_WALK_CSV, args.out_suffix)
    out_md = _with_suffix(OUT_MD, args.out_suffix)

    _write_csv(out_sweep_csv, sweep_rows)
    _write_csv(out_walk_csv, wf_rows)
    write_markdown(
        sweep_rows=sweep_rows,
        wf_rows=wf_rows,
        bets=bets,
        records=records,
        config=config,
        prices_by_ss=prices_by_ss,
        metrics_idx=metrics_idx,
        chunks=chunks,
        max_train_dd=args.max_train_dd,
        out_md=out_md,
        excluded_stations=excluded_stations,
        original_station_count=original_station_count,
        filtered_station_count=filtered_station_count,
        cap_basis=args.cap_basis,
    )

    summaries = [
        _selector_summary(
            wf_rows,
            selector,
            bets=bets,
            records=records,
            chunks=chunks,
            config=config,
            prices_by_ss=prices_by_ss,
            metrics_idx=metrics_idx,
            cap_basis=args.cap_basis,
        )
        for selector in dict.fromkeys(row["selector"] for row in wf_rows)
    ]
    print(json.dumps({
        "n_combos": len(combos),
        "excluded_stations": sorted(excluded_stations),
        "original_station_count": original_station_count,
        "filtered_station_count": filtered_station_count,
        "cap_basis": args.cap_basis,
        "conflict_stats": conflict_stats,
        "summaries": summaries,
        "out_sweep_csv": str(out_sweep_csv),
        "out_walk_csv": str(out_walk_csv),
        "out_md": str(out_md),
    }))


if __name__ == "__main__":
    main()
