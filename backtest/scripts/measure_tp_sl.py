"""TP/SL measurement harness with realistic exit modeling and bankroll sweep.

Improvements over v1:
1. Per-snapshot spread (from metrics table) instead of fixed 0.005 haircut
2. Walk-book impact on exit (linear impact above 5% of liquidity, mirroring entry)
3. Bankroll grid: $100, $300, $500, $1k, $10k, $100k in one run

Output (single-line JSON on stdout):
{
  "train_pnl":   <BR1000 train pnl, used by ce-optimize for ranking>,
  "test_pnl":    <BR1000 test pnl>,
  "train_dd":    <BR1000>, "test_dd": <BR1000>,
  "total_n_train": ..., "total_n_test": ...,
  "no_pnl_train": ..., "no_pnl_test": ...,
  "ymid_pnl_train": ..., "ymid_pnl_test": ...,
  "tail_pnl_train": ..., "tail_pnl_test": ...,
  "results_by_bankroll": {
    "BR100":   {train_pnl, test_pnl, train_dd, test_dd, train_return_pct, test_return_pct, ...},
    "BR300":   {...},
    "BR500":   {...},
    "BR1000":  {...},
    "BR10000": {...},
    "BR100000":{...}
  },
  "config": {...}
}

Run: python backtest/measure_tp_sl.py
"""
from __future__ import annotations

import bisect
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backtest.lib.live_match_eval import (  # noqa: E402
    LiveConfig, apply_no_tail_conflict_policy, ask_ladder_for_record,
    candidates_3strats, execution_min_edge_for_strategy, fee, simulate_walk_book,
)
from backtest.lib.sweep_lib import MARKET_DB, PARQUET_OUT  # noqa: E402
from backtest.lib import honest_report as hr  # noqa: E402

TRAIN_START = "2026-02-04"
TRAIN_END = "2026-04-04"
TEST_START = "2026-04-05"
TEST_END = "2026-05-02"

BANKROLLS = [100.0, 300.0, 500.0, 1000.0, 10000.0, 50000.0, 100000.0, 200000.0, 500000.0]
RANKING_BR = 100.0  # dry-run .env uses INITIAL_BANKROLL=100

# Default metrics when missing
DEFAULT_SPREAD = 0.02       # 2-cent spread fallback
DEFAULT_LIQUIDITY = 100.0   # $100 fallback (reasonable for cheap markets)
EXIT_IMPACT_FACTOR = 0.30   # mirrors entry walk-book
EXIT_NO_IMPACT_ZONE = 0.05  # first 5% of liquidity fills at bid_top

CONFIG_PATH = Path(os.environ.get(
    "HTB_TP_SL_CONFIG",
    REPO_ROOT / "backtest" / "configs" / "candidate_l2_depth.json",
))
STRATEGIES = ("NO", "YMID", "TAIL", "YHIGH")


def _strategy_config(config: dict, name: str) -> dict:
    if name == "YMID":
        return config.get("YMID") or config.get("YES_mid") or {}
    return config.get(name, {})


def _enabled(strategy_cfg: dict) -> bool:
    return bool(strategy_cfg.get("enabled", True))


def _entry_hours(strategy_cfg: dict, default) -> tuple[int, ...]:
    value = strategy_cfg.get("entry_local_hours")
    if value is None:
        value = strategy_cfg.get("entry_hours")
    if value is None:
        value = default
    if isinstance(value, int):
        return (value,)
    return tuple(int(v) for v in value)


def _live_config_from_spec(config: dict, bankroll: float) -> LiveConfig:
    no = _strategy_config(config, "NO")
    yhigh = _strategy_config(config, "YHIGH")
    cfg = LiveConfig(
        initial_bankroll_usd=float(bankroll),
        signal_col=no.get("signal", "p_E"),
        no_min_fill_price=float(no.get("no_min_fill_price", 0.70)),
        no_min_edge=float(no.get("no_min_edge", 0.025)),
        max_edge=float(no.get("max_edge", 0.20)),
        min_bvol=float(config.get("min_bvol", 50.0)),
        max_bet_capital_frac=max(_size_fracs(config).values() or [0.01]),
        max_dd=float(config.get("max_dd_halve_threshold", 0.40)),
        min_bet_usd=float(config.get("min_bet_usd", 1.0)),
        poly_fee_theta=float(config.get("poly_fee_theta", 0.05)),
        entry_local_hour=int(config.get("entry_local_hour", 0)),
        no_execution_min_edge=(
            float(no["execution_min_edge"]) if no.get("execution_min_edge") is not None else None
        ),
        tail_execution_min_edge=(
            float(_strategy_config(config, "TAIL")["execution_min_edge"])
            if _strategy_config(config, "TAIL").get("execution_min_edge") is not None else None
        ),
        ymid_execution_min_edge=(
            float(_strategy_config(config, "YMID")["execution_min_edge"])
            if _strategy_config(config, "YMID").get("execution_min_edge") is not None else None
        ),
        yhigh_min_edge_walk=float(yhigh.get("min_edge", 0.025)),
    )
    return cfg


def _candidate_kwargs(config: dict) -> dict:
    no = _strategy_config(config, "NO")
    ymid = _strategy_config(config, "YMID")
    tail = _strategy_config(config, "TAIL")
    yhigh = _strategy_config(config, "YHIGH")
    global_hour = int(config.get("entry_local_hour", 0))
    consensus_t = config.get("consensus_skip_threshold")
    no_consensus_t = no.get("consensus_skip_threshold")
    tail_consensus_t = tail.get("consensus_skip_threshold")
    ymid_consensus_t = ymid.get("consensus_skip_threshold")
    yhigh_consensus_t = yhigh.get("consensus_skip_threshold")
    return dict(
        enable_no=_enabled(no),
        enable_yes_mid=_enabled(ymid),
        enable_tail=_enabled(tail),
        enable_yhigh=_enabled(yhigh),
        consensus_skip_threshold=float(consensus_t) if consensus_t is not None else None,
        no_consensus_skip_threshold=float(no_consensus_t) if no_consensus_t is not None else None,
        tail_consensus_skip_threshold=float(tail_consensus_t) if tail_consensus_t is not None else None,
        ymid_consensus_skip_threshold=float(ymid_consensus_t) if ymid_consensus_t is not None else None,
        yhigh_consensus_skip_threshold=float(yhigh_consensus_t) if yhigh_consensus_t is not None else None,
        yes_mid_signal=ymid.get("signal", "p_Shrink_n50"),
        yes_mid_alpha=float(ymid.get("alpha", 1.3)),
        yes_mid_fp_min=float(ymid.get("fp_min", 0.10001)),
        yes_mid_fp_max=float(ymid.get("fp_max", 0.50)),
        tail_vote_signals=tuple(tail.get("vote_signals", ("p_E", "p_B_50", "p_L_loose", "p_Shrink_n10"))),
        tail_alpha=float(tail.get("alpha", 2.0)),
        tail_n_required=int(tail.get("n_required", 4)),
        tail_fp_min=float(tail.get("fp_min", 0.001)),
        tail_fp_max=float(tail.get("fp_max", 0.10)),
        tail_bracket_kinds=tuple(tail["bracket_kinds"]) if tail.get("bracket_kinds") is not None else None,
        yhigh_signal=yhigh.get("signal", "p_B_50"),
        yhigh_fp_min=float(yhigh.get("fp_min", 0.50)),
        yhigh_fp_max=float(yhigh.get("fp_max", 1.00)),
        yhigh_min_edge=float(yhigh.get("min_edge", 0.025)),
        yhigh_max_edge=float(yhigh.get("max_edge", 0.30)),
        no_signal_for_high_bracket=no.get("signal_for_high_bracket", "p_B_50"),
        no_min_fill_price_for_high=float(no.get("no_min_fill_price_for_high", 0.50)),
        no_max_edge_for_high=float(no.get("max_edge_for_high", 0.30)),
        no_entry_hours=_entry_hours(no, tuple(range(24))),
        yes_mid_entry_hours=_entry_hours(ymid, (global_hour,)),
        tail_entry_hours=_entry_hours(tail, (global_hour,)),
        yhigh_entry_hours=_entry_hours(yhigh, (global_hour,)),
    )


def _no_tail_conflict_config(config: dict) -> tuple[str, str]:
    return (
        str(config.get("no_tail_conflict_policy", "allow_both")),
        str(config.get("no_tail_conflict_scope", "same_bracket")),
    )


def _tail_delayed_entry_threshold(config: dict) -> float | None:
    raw = _strategy_config(config, "TAIL").get("delayed_entry_fp_max")
    if raw in (None, ""):
        return None
    return float(raw)


def _find_tail_delayed_entry(
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


def apply_tail_delayed_entry(
    *,
    bets: list[tuple],
    records: list[dict],
    prices_by_ss: dict,
    metrics_idx: dict,
    threshold: float | None,
) -> tuple[list[tuple], dict[str, float | int]]:
    """Rewrite TAIL bets to the first later YES snapshot at-or-below threshold."""
    tail_signals = sum(1 for bet in bets if len(bet) >= 9 and str(bet[8]) == "TAIL")
    if threshold is None:
        return list(bets), {
            "tail_signals": tail_signals,
            "tail_entered": tail_signals,
            "tail_missed": 0,
            "avg_delay_minutes": 0.0,
            "max_delay_hours": 0.0,
        }

    out: list[tuple] = []
    tail_entered = 0
    tail_missed = 0
    delays: list[int] = []

    for bet in bets:
        if len(bet) < 9 or str(bet[8]) != "TAIL":
            out.append(bet)
            continue
        if len(bet) < 10:
            raise ValueError("TAIL delayed entry requires bet entry_ts_unix in tuple slot 9")

        date, i, side, _fp, signal_p, _liq, _spread, will_win, strat, entry_ts = bet[:10]
        rec = records[int(i)]
        slug = str(rec["market_slug"])
        close_ts = int(rec["close_ts_unix"])
        entry_ts_i = int(entry_ts)
        hit = _find_tail_delayed_entry(
            prices=prices_by_ss.get((slug, "Yes"), []),
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
            *bet[10:],
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


def _size_fracs(config: dict) -> dict[str, float]:
    return {
        name: (
            float(_strategy_config(config, name).get("size_frac", 0.0))
            if _enabled(_strategy_config(config, name)) else 0.0
        )
        for name in STRATEGIES
        if _strategy_config(config, name).get("size_frac") is not None
    }


def _parse_bankroll_key(key: str) -> float | None:
    if not key.startswith("BR_"):
        return None
    raw = key[3:].lower()
    mult = 1000.0 if raw.endswith("k") else 1.0
    if raw.endswith("k"):
        raw = raw[:-1]
    try:
        return float(raw) * mult
    except ValueError:
        return None


def _stake_caps_for_bankroll(config: dict, bankroll: float) -> dict[str, float]:
    raw_caps = config.get("stake_caps_by_bankroll", {})
    parsed: list[tuple[float, dict]] = []
    for key, caps in raw_caps.items():
        br = _parse_bankroll_key(key)
        if br is None or not isinstance(caps, dict):
            continue
        parsed.append((br, caps))
    if not parsed:
        return {}
    parsed.sort(key=lambda x: x[0])
    selected_br, selected_caps = parsed[0]
    for br, caps in parsed:
        if bankroll >= br:
            selected_br, selected_caps = br, caps
        else:
            break
    del selected_br
    return {
        "NO": float(selected_caps["NO"]) if "NO" in selected_caps else None,
        "YMID": float(selected_caps.get("YMID", selected_caps.get("YES_mid"))) if ("YMID" in selected_caps or "YES_mid" in selected_caps) else None,
        "TAIL": float(selected_caps["TAIL"]) if "TAIL" in selected_caps else None,
        "YHIGH": float(selected_caps["YHIGH"]) if "YHIGH" in selected_caps else None,
    }


# --------------------------------------------------------------------------- loaders

def load_prices_by_slug_side(db_path: Path) -> dict[tuple[str, str], list[tuple[int, float]]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    for slug, side, ts, price in conn.execute(
        "SELECT market_slug, side, ts_unix, price FROM prices "
        "WHERE price IS NOT NULL ORDER BY market_slug, side, ts_unix"
    ):
        out[(slug, side)].append((ts, price))
    conn.close()
    return out


def load_metrics_by_slug(db_path: Path) -> dict[str, tuple[list[int], list[tuple[float, float, float]]]]:
    """Per-slug: (sorted ts list, list of (vol, liq, spread)) for binary search."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    raw: dict[str, list[tuple[int, float, float, float]]] = defaultdict(list)
    for slug, ts, vol, liq, spr in conn.execute(
        "SELECT market_slug, ts_unix, volume, liquidity, spread FROM metrics "
        "ORDER BY market_slug, ts_unix"
    ):
        raw[slug].append((
            ts,
            float(vol) if vol is not None else 0.0,
            float(liq) if liq is not None else DEFAULT_LIQUIDITY,
            float(spr) if spr is not None else DEFAULT_SPREAD,
        ))
    conn.close()
    out: dict[str, tuple[list[int], list[tuple[float, float, float]]]] = {}
    for slug, lst in raw.items():
        keys = [r[0] for r in lst]
        vals = [(r[1], r[2], r[3]) for r in lst]
        out[slug] = (keys, vals)
    return out


def metrics_at(metrics_idx: dict, slug: str, ts: int) -> tuple[float, float, float]:
    """Latest metrics snapshot (vol, liq, spread) at-or-before ts."""
    entry = metrics_idx.get(slug)
    if entry is None:
        return (0.0, DEFAULT_LIQUIDITY, DEFAULT_SPREAD)
    keys, vals = entry
    if not keys:
        return (0.0, DEFAULT_LIQUIDITY, DEFAULT_SPREAD)
    i = bisect.bisect_right(keys, ts) - 1
    if i < 0:
        # ts is before any snapshot; use the earliest
        return vals[0]
    return vals[i]


# --------------------------------------------------------------------------- exit modeling

def find_exit_trigger(entry_price: float, entry_ts: int, close_ts: int,
                       prices: list[tuple[int, float]],
                       tp: float | None, sl: float | None) -> tuple[float, int, str] | None:
    """First post-entry snapshot that crosses TP or SL threshold.

    Returns (trigger_mid_price, exit_ts, reason) or None if held to close.
    Caller applies walk-book on exit using metrics at exit_ts.
    """
    if tp is None and sl is None:
        return None
    for ts, p in prices:
        if ts <= entry_ts:
            continue
        if ts > close_ts:
            break
        if tp is not None and p >= entry_price + tp:
            return (p, ts, "tp")
        if sl is not None and p <= entry_price - sl:
            return (p, ts, "sl")
    return None


def exit_walk_book(stake_usd: float, mid: float, liquidity: float, spread: float) -> float:
    """Realized per-share exit price when selling stake_usd worth of shares.

    Mirrors entry walk-book but on the bid side:
      bid_top   = mid - spread/2  (cross to bid)
      no impact for first 5% of liquidity
      linear impact above that, capped at 30% of bid_top per unit-of-liquidity consumed
    """
    if mid < 1e-4:
        return 0.001
    if mid > 0.9999:
        mid = 0.9999
    half_spread = spread / 2.0
    bid_top = max(mid - half_spread, 0.001)
    if liquidity < 1e-3:
        return bid_top
    impact_per_unit = EXIT_IMPACT_FACTOR * bid_top
    ratio = stake_usd / liquidity
    if ratio <= EXIT_NO_IMPACT_ZONE:
        return bid_top
    excess = ratio - EXIT_NO_IMPACT_ZONE
    return max(bid_top - impact_per_unit * excess, 0.001)


# --------------------------------------------------------------------------- simulator

def simulate(bets, prices_by_ss, metrics_idx, df_records, cfg: LiveConfig,
             tp_sl: dict[str, tuple[float | None, float | None]],
             window_start: str, window_end: str,
             size_frac: dict[str, float] | None = None,
             max_stake: dict[str, float] | None = None,
             min_fill_ratio: dict[str, float] | None = None,
             max_l2_ask_premium: float | None = None,
             bet_log: list | None = None) -> dict:
    """Sequential bet simulator with optional per-strategy capital fraction.

    size_frac: {strat_name: fraction_of_capital} - if absent, uses cfg.max_bet_capital_frac.
    max_stake: {strat_name: dollar_cap} - caps per-bet stake at high BR. Applied
    AFTER frac sizing but BEFORE DD-halving and min_bet floor.
    bet_log: when a list is passed, every executed bet appends one honest per-bet
    record (see backtest/lib/honest_report.py::BET_COLUMNS) for calibration
    reporting and live-vs-backtest joins. Purely additive; PnL is unchanged.
    """
    size_frac = size_frac or {}
    max_stake = max_stake or {}
    min_fill_ratio = min_fill_ratio or {}
    capital = cfg.initial_bankroll_usd
    peak = capital
    pnl_list: list[float] = []
    cap_curve = [capital]
    per_strat: dict[str, dict] = {}

    for bet in bets:
        if len(bet) >= 10:
            date, i, side, fp_displayed, signal_p, liq, spread, will_win, strat, entry_ts = bet[:10]
        else:
            date, i, side, fp_displayed, signal_p, liq, spread, will_win, strat = bet[:9]
            entry_ts = int(df_records[i]["entry_ts_unix"])
        if not (window_start <= date <= window_end):
            continue
        frac = size_frac.get(strat, cfg.max_bet_capital_frac)
        target_usd = capital * frac
        cap_stake = max_stake.get(strat)
        if cap_stake is not None:
            target_usd = min(target_usd, cap_stake)
        dd = (peak - capital) / peak if peak > 1e-9 else 0.0
        if dd >= cfg.max_dd:
            target_usd *= 0.5
        if target_usd < cfg.min_bet_usd or target_usd > capital:
            continue

        min_edge = execution_min_edge_for_strategy(cfg, strat)
        rec = df_records[i]
        entry_ladder = ask_ladder_for_record(rec, side, entry_ts)
        if max_l2_ask_premium is not None and entry_ladder:
            best_ask = min(price for price, _shares in entry_ladder)
            if best_ask - fp_displayed > max_l2_ask_premium:
                continue
        result = simulate_walk_book(target_usd, fp_displayed, liq, signal_p,
                                     cfg.poly_fee_theta, min_edge,
                                     cfg.min_bet_usd, entry_spread=spread,
                                     ask_ladder=entry_ladder)
        if result is None:
            continue
        stake, entry_vwap, _ = result
        fill_ratio_floor = float(min_fill_ratio.get(strat, 0.0) or 0.0)
        if fill_ratio_floor > 0 and stake < target_usd * fill_ratio_floor:
            continue

        slug = rec["market_slug"]
        entry_ts = int(entry_ts)
        close_ts = int(rec["close_ts_unix"])
        shares = stake / entry_vwap
        entry_fee = shares * fee(cfg.poly_fee_theta, entry_vwap)

        tp, sl = tp_sl.get(strat, (None, None))
        side_key = "Yes" if side == "YES" else "No"
        prices = prices_by_ss.get((slug, side_key), ())

        trigger = find_exit_trigger(entry_vwap, entry_ts, close_ts, prices, tp, sl)
        if trigger is not None:
            trigger_mid, exit_ts, _reason = trigger
            # Real exit cost: walk the book on the way out using metrics at exit_ts
            _vol_x, liq_x, spr_x = metrics_at(metrics_idx, slug, exit_ts)
            exit_vwap = exit_walk_book(stake, trigger_mid, liq_x, spr_x)
            net = shares * (exit_vwap - entry_vwap) \
                - entry_fee - shares * fee(cfg.poly_fee_theta, exit_vwap)
        else:
            if will_win:
                net = stake * (1.0 - entry_vwap) / entry_vwap \
                    - stake * fee(cfg.poly_fee_theta, entry_vwap) / entry_vwap
            else:
                net = -stake - entry_fee

        capital += net
        peak = max(peak, capital)
        if capital < 1.0:
            break
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
                "entry_ts": int(entry_ts),
                "exit_reason": (trigger[2] if trigger is not None else "close"),
            })
        ps = per_strat.setdefault(strat, {
            "n": 0, "pnl": 0.0, "stake_total": 0.0,
            "n_tp": 0, "n_sl": 0, "n_close": 0,
            "pnl_tp": 0.0, "pnl_sl": 0.0, "pnl_close": 0.0,
            "n_tp_wouldwin": 0, "n_tp_wouldlose": 0,
            "n_sl_wouldwin": 0, "n_sl_wouldlose": 0,
            "n_close_win": 0, "n_close_lose": 0,
            "pnl_close_win": 0.0, "pnl_close_lose": 0.0,
        })
        ps["n"] += 1
        ps["pnl"] += net
        ps["stake_total"] += stake
        if trigger is not None:
            reason = trigger[2]
            ps[f"n_{reason}"] += 1
            ps[f"pnl_{reason}"] += net
            ps[f"n_{reason}_wouldwin" if will_win else f"n_{reason}_wouldlose"] += 1
        else:
            ps["n_close"] += 1
            ps["pnl_close"] += net
            if will_win:
                ps["n_close_win"] += 1
                ps["pnl_close_win"] += net
            else:
                ps["n_close_lose"] += 1
                ps["pnl_close_lose"] += net

    if not pnl_list:
        return {"n": 0, "pnl": 0.0, "max_dd_pct_peak": 0.0,
                "final_bankroll": cfg.initial_bankroll_usd, "per_strat": {}}

    cap_arr = np.array(cap_curve)
    peak_arr = np.maximum.accumulate(cap_arr)
    dd_pct = float(abs(((cap_arr - peak_arr) / np.where(peak_arr > 0, peak_arr, 1.0)).min()))
    pnl_arr = np.array(pnl_list)
    return {
        "n": len(pnl_arr),
        "pnl": float(pnl_arr.sum()),
        "max_dd_pct_peak": dd_pct,
        "final_bankroll": float(cap_arr[-1]),
        "per_strat": per_strat,
    }


# --------------------------------------------------------------------------- main

def main():
    config = json.loads(CONFIG_PATH.read_text())
    tp_sl = {}
    for strat in STRATEGIES:
        s = _strategy_config(config, strat)
        tp = s.get("tp")
        sl = s.get("sl")
        tp = float(tp) if tp is not None else None
        sl = float(sl) if sl is not None else None
        tp_sl[strat] = (tp, sl)

    df = pd.read_parquet(PARQUET_OUT)
    record_cols = ["market_slug", "entry_ts_unix", "close_ts_unix"]
    for hour in range(24):
        record_cols.extend([
            f"entry_ts_h{hour}",
            f"yes_ask_ladder_h{hour}",
            f"no_ask_ladder_h{hour}",
        ])
    df_records = df[[c for c in record_cols if c in df.columns]].to_dict("records")
    prices_by_ss = load_prices_by_slug_side(MARKET_DB)
    metrics_idx = load_metrics_by_slug(MARKET_DB)

    # Generate the bet stream once (independent of bankroll)
    cfg_template = _live_config_from_spec(config, RANKING_BR)
    kw = _candidate_kwargs(config)
    bets = candidates_3strats(df, cfg_template, **kw)
    conflict_policy, conflict_scope = _no_tail_conflict_config(config)
    bets, conflict_stats = apply_no_tail_conflict_policy(
        bets,
        df,
        policy=conflict_policy,
        scope=conflict_scope,
    )
    tail_delayed_entry_threshold = _tail_delayed_entry_threshold(config)
    bets, tail_delayed_entry_stats = apply_tail_delayed_entry(
        bets=bets,
        records=df_records,
        prices_by_ss=prices_by_ss,
        metrics_idx=metrics_idx,
        threshold=tail_delayed_entry_threshold,
    )
    bets.sort(key=lambda b: (b[0], b[9] if len(b) >= 10 else 0, b[1], b[8]))

    size_frac = _size_fracs(config)
    max_l2_ask_premium = (
        float(config["max_l2_ask_premium"])
        if config.get("max_l2_ask_premium") is not None else None
    )

    # Run for each bankroll
    results_by_br = {}
    train_bet_log: list = []
    test_bet_log: list = []
    for br in BANKROLLS:
        cfg_br = _live_config_from_spec(config, br)
        max_stake = _stake_caps_for_bankroll(config, br)
        # Capture the honest per-bet stream only at the ranking bankroll so the
        # calibration report reflects the same path ce-optimize ranks on.
        capture = (br == RANKING_BR)
        tr = simulate(bets, prices_by_ss, metrics_idx, df_records, cfg_br,
                      tp_sl, TRAIN_START, TRAIN_END,
                      size_frac=size_frac, max_stake=max_stake,
                      max_l2_ask_premium=max_l2_ask_premium,
                      bet_log=train_bet_log if capture else None)
        te = simulate(bets, prices_by_ss, metrics_idx, df_records, cfg_br,
                      tp_sl, TEST_START, TEST_END,
                      size_frac=size_frac, max_stake=max_stake,
                      max_l2_ask_premium=max_l2_ask_premium,
                      bet_log=test_bet_log if capture else None)

        def ps(window, key):
            return window["per_strat"].get(key, {
                "n": 0, "pnl": 0.0, "stake_total": 0.0,
                "n_tp": 0, "n_sl": 0, "n_close": 0,
                "pnl_tp": 0.0, "pnl_sl": 0.0, "pnl_close": 0.0,
                "n_tp_wouldwin": 0, "n_tp_wouldlose": 0,
                "n_sl_wouldwin": 0, "n_sl_wouldlose": 0,
                "n_close_win": 0, "n_close_lose": 0,
                "pnl_close_win": 0.0, "pnl_close_lose": 0.0,
            })

        def make_breakdown(window, strat):
            s = ps(window, strat)
            return {
                "n": s["n"], "pnl": round(s["pnl"], 4), "stake_total": round(s["stake_total"], 2),
                "n_tp": s["n_tp"], "pnl_tp": round(s["pnl_tp"], 4),
                "n_tp_wouldwin": s["n_tp_wouldwin"], "n_tp_wouldlose": s["n_tp_wouldlose"],
                "n_sl": s["n_sl"], "pnl_sl": round(s["pnl_sl"], 4),
                "n_sl_wouldwin": s["n_sl_wouldwin"], "n_sl_wouldlose": s["n_sl_wouldlose"],
                "n_close": s["n_close"], "pnl_close": round(s["pnl_close"], 4),
                "n_close_win": s["n_close_win"], "n_close_lose": s["n_close_lose"],
                "pnl_close_win": round(s["pnl_close_win"], 4),
                "pnl_close_lose": round(s["pnl_close_lose"], 4),
            }

        rec = {
            "bankroll": br,
            "train_pnl": round(tr["pnl"], 4),
            "test_pnl": round(te["pnl"], 4),
            "train_dd": round(tr["max_dd_pct_peak"] * 100, 4),
            "test_dd": round(te["max_dd_pct_peak"] * 100, 4),
            "train_return_pct": round(tr["pnl"] / br * 100, 2),
            "test_return_pct": round(te["pnl"] / br * 100, 2),
            "train_final": round(tr["final_bankroll"], 2),
            "test_final": round(te["final_bankroll"], 2),
            "total_n_train": tr["n"],
            "total_n_test": te["n"],
            "no_n_train": ps(tr, "NO")["n"],
            "no_pnl_train": round(ps(tr, "NO")["pnl"], 4),
            "no_n_test": ps(te, "NO")["n"],
            "no_pnl_test": round(ps(te, "NO")["pnl"], 4),
            "ymid_n_train": ps(tr, "YMID")["n"],
            "ymid_pnl_train": round(ps(tr, "YMID")["pnl"], 4),
            "ymid_n_test": ps(te, "YMID")["n"],
            "ymid_pnl_test": round(ps(te, "YMID")["pnl"], 4),
            "tail_n_train": ps(tr, "TAIL")["n"],
            "tail_pnl_train": round(ps(tr, "TAIL")["pnl"], 4),
            "tail_n_test": ps(te, "TAIL")["n"],
            "tail_pnl_test": round(ps(te, "TAIL")["pnl"], 4),
            "yhigh_n_train": ps(tr, "YHIGH")["n"],
            "yhigh_pnl_train": round(ps(tr, "YHIGH")["pnl"], 4),
            "yhigh_n_test": ps(te, "YHIGH")["n"],
            "yhigh_pnl_test": round(ps(te, "YHIGH")["pnl"], 4),
            # Per-strategy exit-cause breakdown (TRAIN + TEST × NO/YMID/TAIL/YHIGH)
            "no_train_breakdown": make_breakdown(tr, "NO"),
            "no_test_breakdown": make_breakdown(te, "NO"),
            "ymid_train_breakdown": make_breakdown(tr, "YMID"),
            "ymid_test_breakdown": make_breakdown(te, "YMID"),
            "tail_train_breakdown": make_breakdown(tr, "TAIL"),
            "tail_test_breakdown": make_breakdown(te, "TAIL"),
            "yhigh_train_breakdown": make_breakdown(tr, "YHIGH"),
            "yhigh_test_breakdown": make_breakdown(te, "YHIGH"),
        }
        results_by_br[f"BR{int(br)}"] = rec

    # ---------------------------------------------------------------- honest report
    # Attach bracket meta, then compute calibration / ROS / reliability splits by
    # strategy and by bracket unit at the ranking bankroll. Persist the per-bet
    # streams for later live-vs-backtest joins (shadow_replay.py) unless the dump
    # flag is turned off. This is a champion/final driver, so the dump defaults ON.
    hr.attach_meta(train_bet_log, df)
    hr.attach_meta(test_bet_log, df)
    for r in train_bet_log:
        r["period"] = "TRAIN"
    for r in test_bet_log:
        r["period"] = "TEST"
    primary_br = results_by_br[f"BR{int(RANKING_BR)}"]
    honest = {
        "train": hr.summarize(train_bet_log, portfolio_dd_pct=primary_br["train_dd"]),
        "test": hr.summarize(test_bet_log, portfolio_dd_pct=primary_br["test_dd"]),
    }

    dump_bets = os.environ.get("HTB_DUMP_BETS", "1").strip().lower() not in ("0", "false", "no", "")
    bets_dir = REPO_ROOT / "backtest" / "results" / "bets"
    variant = CONFIG_PATH.stem
    dumped: dict[str, str] = {}
    if dump_bets:
        if train_bet_log:
            p = hr.persist_bets(train_bet_log, bets_dir / f"{variant}_train.parquet", period="TRAIN")
            dumped["train"] = str(p)
        if test_bet_log:
            p = hr.persist_bets(test_bet_log, bets_dir / f"{variant}_test.parquet", period="TEST")
            dumped["test"] = str(p)
    honest["dumped_bet_files"] = dumped

    # Human-readable calibration block to STDERR so the stdout JSON stays a
    # single line for ce-optimize.
    print(hr.render_text(honest["train"], f"HONEST REPORT - {variant} - TRAIN ({TRAIN_START}..{TRAIN_END})"),
          file=sys.stderr)
    print(hr.render_text(honest["test"], f"HONEST REPORT - {variant} - TEST ({TEST_START}..{TEST_END})"),
          file=sys.stderr)
    if dumped:
        print(f"[honest] per-bet streams written: {dumped}", file=sys.stderr)

    # Top-level uses RANKING_BR ($1000) for ce-optimize gate evaluation
    primary = results_by_br[f"BR{int(RANKING_BR)}"]
    out = {
        "train_pnl": primary["train_pnl"],
        "test_pnl": primary["test_pnl"],
        "train_dd": primary["train_dd"],
        "test_dd": primary["test_dd"],
        "total_n_train": primary["total_n_train"],
        "total_n_test": primary["total_n_test"],
        "no_n_train": primary["no_n_train"],
        "no_pnl_train": primary["no_pnl_train"],
        "no_n_test": primary["no_n_test"],
        "no_pnl_test": primary["no_pnl_test"],
        "ymid_n_train": primary["ymid_n_train"],
        "ymid_pnl_train": primary["ymid_pnl_train"],
        "ymid_n_test": primary["ymid_n_test"],
        "ymid_pnl_test": primary["ymid_pnl_test"],
        "tail_n_train": primary["tail_n_train"],
        "tail_pnl_train": primary["tail_pnl_train"],
        "tail_n_test": primary["tail_n_test"],
        "tail_pnl_test": primary["tail_pnl_test"],
        "yhigh_n_train": primary["yhigh_n_train"],
        "yhigh_pnl_train": primary["yhigh_pnl_train"],
        "yhigh_n_test": primary["yhigh_n_test"],
        "yhigh_pnl_test": primary["yhigh_pnl_test"],
        "results_by_bankroll": results_by_br,
        "no_tail_conflict": conflict_stats,
        "tail_delayed_entry": tail_delayed_entry_stats,
        "honest_report": honest,
        "config": config,
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
