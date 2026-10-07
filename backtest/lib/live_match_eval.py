"""Candidate generator and execution model matching the live bot.

Strategies NO, YMID, TAIL and YHIGH can be enabled independently. Sizing is
``capital × size_frac``, fees are ``θ·p·(1−p)``, and fills walk the real L2 ask
ladder when one is available. Inputs come from the leakage-safe decision table.
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backtest.lib.sweep_lib import PARQUET_OUT  # noqa: E402
from backtest.lib import honest_report as hr  # noqa: E402

TRAIN_START = "2026-02-04"
TRAIN_END = "2026-04-04"
TEST_START = "2026-04-05"
TEST_END = "2026-05-02"


# Mirror src/hightempbot/execution/strategy_constants.py
@dataclass
class LiveConfig:
    initial_bankroll_usd: float = 10000.0
    # Sizing (Edge-Preserving)
    max_bet_capital_frac: float = 0.01  # 1% of capital per bet (MILD baseline)
    max_dd: float = 0.40  # halve bet size when drawdown reaches 40% of peak
    min_bet_usd: float = 1.0
    # Fee
    poly_fee_theta: float = 0.05  # fee = theta * p * (1-p)
    # Universal gates
    min_fill_price: float = 0.01
    max_edge: float = 0.20
    min_bvol: float = 50.0  # 24h $ volume gate
    # NO side
    no_min_fill_price: float = 0.70
    no_min_edge: float = 0.025  # MIN_EDGE for NO
    # YES side (CONTRARIAN — negative edge band)
    yes_min_fill_price: float = 0.35
    yes_min_edge: float = -0.10
    yes_max_edge: float = -0.03
    # Exposure caps are not simulated.
    # Signal source
    signal_col: str = "p_E"
    no_execution_min_edge: float | None = None
    tail_execution_min_edge: float | None = None
    ymid_execution_min_edge: float | None = None
    yhigh_min_edge_walk: float = 0.025
    # Entry timing — None means use yes_price/no_price (latest snapshot >= 4h before close UTC).
    # If set to one of ENTRY_LOCAL_HOURS, simulates "the price you would see at H:00 local on market_date".
    entry_local_hour: int | None = None


def fee(theta: float, p: float) -> float:
    """Polymarket fee: theta * p * (1-p)."""
    return theta * p * (1.0 - p)


def _parse_ladder(raw) -> list[tuple[float, float]]:
    """Parse a serialized L2 ladder into sorted (price, shares) levels."""
    if raw is None:
        return []
    if isinstance(raw, float) and math.isnan(raw):
        return []
    if isinstance(raw, str):
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []

    levels: list[tuple[float, float]] = []
    for level in raw:
        if isinstance(level, dict):
            price = level.get("price")
            size = level.get("size")
        else:
            if len(level) < 2:
                continue
            price, size = level[0], level[1]
        try:
            p = float(price)
            s = float(size)
        except (TypeError, ValueError):
            continue
        if math.isfinite(p) and math.isfinite(s) and 0.0 < p < 1.0 and s > 0:
            levels.append((p, s))
    return sorted(levels, key=lambda x: x[0])


def _entry_ts_int(entry_ts: int | float | None) -> int | None:
    if entry_ts is None:
        return None
    try:
        return int(entry_ts)
    except (TypeError, ValueError, OverflowError):
        return None


def _matching_hour(record: dict, entry_ts_i: int) -> int | None:
    for hour in range(24):
        ts = record.get(f"entry_ts_h{hour}")
        try:
            if ts is None or math.isnan(float(ts)):
                continue
            if int(ts) == entry_ts_i:
                return hour
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _ladder_for_record(record: dict, side: str, entry_ts: int | float | None, book_side: str):
    """Return an L2 bid/ask ladder matching a generated bet's side and entry hour."""
    entry_ts_i = _entry_ts_int(entry_ts)
    if entry_ts_i is None:
        return None

    side_key = "yes" if side == "YES" else "no"
    cache = record.setdefault("_l2_ladder_cache", {})
    cache_key = (side_key, entry_ts_i, book_side)
    if cache_key in cache:
        return cache[cache_key]

    hour = _matching_hour(record, entry_ts_i)
    if hour is None:
        cache[cache_key] = None
        return None

    levels = _parse_ladder(record.get(f"{side_key}_{book_side}_ladder_h{hour}"))
    cache[cache_key] = levels or None
    return cache[cache_key]


def ask_ladder_for_record(record: dict, side: str, entry_ts: int | float | None):
    return _ladder_for_record(record, side, entry_ts, "ask")


def bid_ladder_for_record(record: dict, side: str, entry_ts: int | float | None):
    return _ladder_for_record(record, side, entry_ts, "bid")


def _ask_ladder_for_df_row(df: pd.DataFrame, row_idx: int, side: str, entry_ts: int | float | None):
    entry_ts_i = _entry_ts_int(entry_ts)
    if entry_ts_i is None:
        return None

    side_key = "yes" if side == "YES" else "no"
    row = df.iloc[row_idx]
    for hour in range(24):
        ts_col = f"entry_ts_h{hour}"
        ladder_col = f"{side_key}_ask_ladder_h{hour}"
        if ts_col not in df.columns or ladder_col not in df.columns:
            continue
        try:
            ts = row[ts_col]
            if pd.isna(ts) or int(ts) != entry_ts_i:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        levels = _parse_ladder(row[ladder_col])
        return levels or None
    return None


def _walk_real_ask_ladder(
    target_usd: float,
    ask_ladder,
    signal_p: float,
    theta: float,
    min_edge: float,
    min_bet_usd: float,
) -> tuple[float, float, float] | None:
    levels = _parse_ladder(ask_ladder)
    if not levels:
        return None

    acc_usd = 0.0
    acc_shares = 0.0
    for price, shares in levels:
        remaining = target_usd - acc_usd
        if remaining <= 0:
            break
        level_usd = price * shares
        take_usd = min(level_usd, remaining)
        take_shares = take_usd / price

        new_usd = acc_usd + take_usd
        new_shares = acc_shares + take_shares
        if new_shares <= 0:
            continue
        new_vwap = new_usd / new_shares
        new_edge = signal_p - new_vwap - theta * new_vwap * (1.0 - new_vwap)
        if not math.isfinite(new_edge) or new_edge < min_edge:
            break

        acc_usd = new_usd
        acc_shares = new_shares

    if acc_shares <= 0 or acc_usd < min_bet_usd:
        return None
    vwap = acc_usd / acc_shares
    realized_edge = signal_p - vwap - theta * vwap * (1.0 - vwap)
    if not math.isfinite(realized_edge) or realized_edge < min_edge:
        return None
    return (acc_usd, vwap, realized_edge)


def simulate_walk_book(target_usd: float, displayed_price: float, liquidity: float,
                        signal_p: float, theta: float, min_edge: float,
                        min_bet_usd: float = 1.0,
                        entry_spread: float = 0.0,
                        ask_ladder=None) -> tuple[float, float, float] | None:
    """Approximate live bot's walk_book_edge_preserving with a linear-impact model."""
    if _parse_ladder(ask_ladder):
        return _walk_real_ask_ladder(
            target_usd, ask_ladder, signal_p, theta, min_edge, min_bet_usd
        )

    if liquidity < 1e-3 or displayed_price < 1e-4 or displayed_price > 0.999:
        return None

    # Step 1: taker pays mid + half_spread (cap haircut at 50% of mid to avoid
    # nonsensical fills past 1.0; uses entry_spread which is ENTRY-time, much
    # tighter than the contaminated lifetime avg_spread)
    half_spread = entry_spread * 0.5
    taker_top = min(displayed_price + half_spread, 0.999)

    # Step 2: linear price-impact above taker_top. Consuming 100% of liquidity
    # above the no-impact threshold pushes VWAP up by 30% of (1 - taker_top).
    impact_per_unit = 0.30 * (1.0 - taker_top)
    no_impact_threshold = 0.05  # bottom 5% of liquidity fills at taker_top
    displayed_price = taker_top  # for the rest of the function

    def vwap_at(size_usd: float) -> float:
        ratio = size_usd / liquidity
        if ratio <= no_impact_threshold:
            return displayed_price
        excess = ratio - no_impact_threshold
        return min(displayed_price + impact_per_unit * excess, 0.999)

    def edge_at(size_usd: float) -> float:
        v = vwap_at(size_usd)
        return signal_p - v - theta * v * (1.0 - v)

    # Fast path: full target fills at acceptable edge
    e_full = edge_at(target_usd)
    if e_full >= min_edge:
        v = vwap_at(target_usd)
        return (target_usd, v, e_full)

    # Edge breaks at full target. Check if even min_bet_usd preserves edge.
    if edge_at(min_bet_usd) < min_edge:
        return None  # skip — can't preserve edge at any size

    # Binary search for the largest size where edge >= min_edge.
    lo, hi = min_bet_usd, target_usd
    for _ in range(20):
        mid = (lo + hi) / 2
        if edge_at(mid) >= min_edge:
            lo = mid
        else:
            hi = mid
    v = vwap_at(lo)
    return (lo, v, edge_at(lo))


def candidates_under_live(df, cfg: LiveConfig):
    """Yield (date, idx, side, p, fill_price, edge) for every row that passes live gates
    as either a NO or a YES bet."""
    md = df["market_date"].to_numpy()
    hour = cfg.entry_local_hour
    yp, np_p, vol, _liq, _spr, entry_ts, not_leakage = _entry_arrays(df, hour)
    won = df["won_yes"].to_numpy()
    p = df[cfg.signal_col].to_numpy(dtype=float)

    # NO edge: (1-p) - no_price - fee
    no_edge = (1.0 - p) - np_p - fee(cfg.poly_fee_theta, np_p)
    # YES edge: p - yes_price - fee
    yes_edge = p - yp - fee(cfg.poly_fee_theta, yp)

    # NO gates
    no_pass = (
        ~np.isnan(p) & ~np.isnan(np_p) & not_leakage &
        (np_p >= cfg.no_min_fill_price) &
        (np_p >= cfg.min_fill_price) &
        (no_edge >= cfg.no_min_edge) &
        (no_edge <= cfg.max_edge) &
        (vol >= cfg.min_bvol)
    )

    # YES gates: contrarian band + YES floor + NO not in NO-zone
    yes_pass = (
        ~np.isnan(p) & ~np.isnan(yp) & ~np.isnan(np_p) & not_leakage &
        (yp >= cfg.yes_min_fill_price) &
        (yp >= cfg.min_fill_price) &
        (yes_edge >= cfg.yes_min_edge) &
        (yes_edge <= cfg.yes_max_edge) &
        (np_p < cfg.no_min_fill_price) &  # YES inverse-fill gate
        (vol >= cfg.min_bvol)
    )

    return no_pass, yes_pass, p, yp, np_p, won, no_edge, yes_edge, md


def evaluate_live_match(df, cfg: LiveConfig, window_start: str, window_end: str):
    no_pass, yes_pass, p_arr, yp, np_p, won, no_edge, yes_edge, md = candidates_under_live(df, cfg)
    in_window = (md >= window_start) & (md <= window_end)
    entry_liq = df["entry_liquidity"].fillna(0).to_numpy(dtype=float)
    entry_spr = df["entry_spread"].fillna(0).to_numpy(dtype=float)

    # Build bet list with the per-row signal_p + entry_spread (taker pays half-spread haircut)
    bets = []  # (date, idx, side, mid_price, signal_p, liquidity, spread, will_win)
    for i in np.where(no_pass & in_window)[0]:
        bets.append((md[i], int(i), "NO", float(np_p[i]), 1.0 - float(p_arr[i]),
                     float(entry_liq[i]), float(entry_spr[i]), won[i] == 0))
    for i in np.where(yes_pass & in_window)[0]:
        bets.append((md[i], int(i), "YES", float(yp[i]), float(p_arr[i]),
                     float(entry_liq[i]), float(entry_spr[i]), won[i] == 1))
    bets.sort(key=lambda b: (b[0], b[1]))

    capital = cfg.initial_bankroll_usd
    peak = capital
    pnl_list = []
    stake_list = []
    capital_curve = [capital]
    per_side = {"NO": {"pnl": [], "stake": [], "win": [], "claimed": [], "won_close": [], "n": 0},
                "YES": {"pnl": [], "stake": [], "win": [], "claimed": [], "won_close": [], "n": 0}}
    skip_min_bet = 0
    skip_walk_book = 0

    for date, i, side, fp_displayed, signal_p, liq, spread, will_win in bets:
        # Edge-Preserving Sizing target
        target_usd = capital * cfg.max_bet_capital_frac
        dd = (peak - capital) / peak if peak > 1e-9 else 0.0
        if dd >= cfg.max_dd:
            target_usd *= 0.5
        if target_usd < cfg.min_bet_usd or target_usd > capital:
            skip_min_bet += 1
            continue

        # Walk the book (taker pays mid + half-spread, then linear impact above)
        min_edge_for_side = cfg.no_min_edge if side == "NO" else cfg.yes_min_edge
        result = simulate_walk_book(target_usd, fp_displayed, liq, signal_p,
                                     cfg.poly_fee_theta, min_edge_for_side, cfg.min_bet_usd,
                                     entry_spread=spread)
        if result is None:
            skip_walk_book += 1
            continue
        stake, fp_actual, _ = result

        # Resolution at the walked VWAP price
        if will_win:
            net = stake * (1.0 - fp_actual) / fp_actual - stake * fee(cfg.poly_fee_theta, fp_actual) / fp_actual
        else:
            net = -stake - stake * fee(cfg.poly_fee_theta, fp_actual) / fp_actual

        capital += net
        peak = max(peak, capital)
        if capital < 1.0:
            capital = max(capital, 0.0); break

        pnl_list.append(net)
        stake_list.append(stake)
        capital_curve.append(capital)
        per_side[side]["pnl"].append(net)
        per_side[side]["stake"].append(stake)
        per_side[side]["win"].append(net > 0)
        per_side[side]["claimed"].append(float(signal_p))
        per_side[side]["won_close"].append(bool(will_win))
        per_side[side]["n"] += 1

    pnl_arr = np.array(pnl_list); stake_arr = np.array(stake_list); cap_arr = np.array(capital_curve)
    if len(pnl_arr) == 0:
        return _empty(cfg)

    peak_arr = np.maximum.accumulate(cap_arr)
    dd_pct = float(abs(((cap_arr - peak_arr) / np.where(peak_arr > 0, peak_arr, 1.0)).min()))

    summary = {}
    for side in ("NO", "YES"):
        d = per_side[side]
        n = d["n"]
        if n == 0:
            summary[side] = {"n": 0, "pnl": 0, "stake": 0, "wr": 0, "ros": 0, "avg_stake": 0,
                             "wr_se": 0, "claimed_mean": 0, "realized_mean": 0, "overconf_pp": 0}
            continue
        ps = sum(d["pnl"]); st = sum(d["stake"])
        # Honest add-ons: win-rate SE + model calibration (claimed vs realized).
        wins_close = int(sum(d["won_close"]))
        claimed_mean = float(sum(d["claimed"]) / n)
        realized_mean = wins_close / n
        summary[side] = {
            "n": n, "pnl": float(ps), "stake": float(st),
            "wr": float(sum(d["win"]) / n), "ros": float(ps / st) if st > 0 else 0,
            "avg_stake": float(st / n),
            "wr_se": round(hr.proportion_se(int(sum(d["win"])), n), 4),
            "claimed_mean": round(claimed_mean, 4),
            "realized_mean": round(realized_mean, 4),
            "overconf_pp": round((claimed_mean - realized_mean) * 100.0, 2),
        }

    all_claimed = per_side["NO"]["claimed"] + per_side["YES"]["claimed"]
    all_won_close = per_side["NO"]["won_close"] + per_side["YES"]["won_close"]
    n_all = len(pnl_arr)
    claimed_mean_all = float(sum(all_claimed) / n_all) if n_all else 0.0
    realized_mean_all = float(sum(all_won_close) / n_all) if n_all else 0.0
    return {
        "n": len(pnl_arr), "pnl": float(pnl_arr.sum()),
        "stake": float(stake_arr.sum()),
        "ros": float(pnl_arr.sum() / stake_arr.sum()) if stake_arr.sum() > 0 else 0,
        "wr": float((pnl_arr > 0).mean()),
        "wr_se": round(hr.proportion_se(int((pnl_arr > 0).sum()), n_all), 4),
        "claimed_mean": round(claimed_mean_all, 4),
        "realized_mean": round(realized_mean_all, 4),
        "overconf_pp": round((claimed_mean_all - realized_mean_all) * 100.0, 2),
        "max_dd_pct_peak": dd_pct,
        "final_bankroll": float(cap_arr[-1]),
        "skip_min_bet": skip_min_bet,
        "per_side": summary,
        "n_no_eligible": int((no_pass & ((md >= window_start) & (md <= window_end))).sum()),
        "n_yes_eligible": int((yes_pass & ((md >= window_start) & (md <= window_end))).sum()),
    }


def _empty(cfg):
    return {"n": 0, "pnl": 0.0, "stake": 0.0, "ros": 0.0, "wr": 0.0,
            "wr_se": 0.0, "claimed_mean": 0.0, "realized_mean": 0.0, "overconf_pp": 0.0,
            "max_dd_pct_peak": 0.0, "final_bankroll": cfg.initial_bankroll_usd,
            "skip_min_bet": 0, "per_side": {}, "n_no_eligible": 0, "n_yes_eligible": 0}


def fmt(label, r, days):
    return (f"{label:<20} n={r['n']:>4} ({r['n']/days:>5.1f}/d) "
            f"pnl=${r['pnl']:>+8,.2f} (${r['pnl']/days:>+7.2f}/d) "
            f"stake=${r['stake']:>8,.0f} ROS={r['ros']*100:>+6.2f}% "
            f"wr={r['wr']*100:>5.1f}%+-{r.get('wr_se', 0)*100:>3.1f} "
            f"claim={r.get('claimed_mean', 0)*100:>5.1f}% overconf={r.get('overconf_pp', 0):>+5.1f}pp "
            f"dd={r['max_dd_pct_peak']*100:>5.1f}% "
            f"final=${r['final_bankroll']:>9,.2f} "
            f"NO_elig={r['n_no_eligible']:>3} YES_elig={r['n_yes_eligible']:>3}")


# =============================================================================
# 4-strategy candidate generator under live-matching exec
# =============================================================================

LUT_MIN_N_FOR_SHRINKAGE = 30


def _hours(value, default: tuple[int, ...]) -> tuple[int, ...]:
    if value is None:
        return default
    if isinstance(value, int):
        return (value,)
    return tuple(int(v) for v in value)


def execution_min_edge_for_strategy(cfg: LiveConfig, strat: str) -> float:
    if strat == "NO":
        return cfg.no_execution_min_edge if cfg.no_execution_min_edge is not None else cfg.no_min_edge
    if strat == "TAIL":
        return cfg.tail_execution_min_edge if cfg.tail_execution_min_edge is not None else 0.0
    if strat == "YMID":
        return cfg.ymid_execution_min_edge if cfg.ymid_execution_min_edge is not None else 0.0
    if strat == "YHIGH":
        return getattr(cfg, "yhigh_min_edge_walk", 0.025)
    return 0.0


def _entry_arrays(df: pd.DataFrame, hour: int | None):
    """Return price/metric arrays for one simulated entry hour."""
    if hour is None:
        yp_col, np_col, ts_col = "yes_price", "no_price", "entry_ts_unix"
        vol_col, liq_col, spr_col = "entry_volume", "entry_liquidity", "entry_spread"
    else:
        yp_col, np_col, ts_col = f"yes_price_h{hour}", f"no_price_h{hour}", f"entry_ts_h{hour}"
        vol_col, liq_col, spr_col = f"entry_volume_h{hour}", f"entry_liquidity_h{hour}", f"entry_spread_h{hour}"
        missing = [c for c in (yp_col, np_col, ts_col, vol_col, liq_col, spr_col) if c not in df.columns]
        if missing:
            raise ValueError(
                "per-hour entry metrics missing "
                f"({', '.join(missing)}); rebuild the base backtest decision table"
            )

    yp = df[yp_col].to_numpy(dtype=float)
    np_p = df[np_col].to_numpy(dtype=float)
    entry_ts = df[ts_col].to_numpy(dtype=float)
    vol = df[vol_col].fillna(0).to_numpy(dtype=float)
    liq = df[liq_col].fillna(0).to_numpy(dtype=float)
    spr = df[spr_col].fillna(0).to_numpy(dtype=float)

    if hour is None:
        not_leakage = ~np.isnan(entry_ts)
    else:
        # Cast to datetime64[s]: pandas 3 defaults to microseconds, which broke `// 10**9`.
        md_unix = pd.to_datetime(df["market_date"]).astype("datetime64[s]").astype("int64").to_numpy()
        not_leakage = ~np.isnan(entry_ts) & (entry_ts >= md_unix)

    return yp, np_p, vol, liq, spr, entry_ts, not_leakage


def _append_bets(bets, df, mask, *, md, won, side, price, signal_p, liq, spr, entry_ts, strat):
    for i in np.where(mask)[0]:
        bets.append((
            md[i],
            int(i),
            side,
            float(price[i]),
            float(signal_p[i]),
            float(liq[i]),
            float(spr[i]),
            won[i] == (1 if side == "YES" else 0),
            strat,
            int(entry_ts[i]),
        ))


NO_TAIL_CONFLICT_POLICIES = frozenset(("allow_both", "prefer_tail", "prefer_no", "drop_both"))
NO_TAIL_CONFLICT_SCOPES = frozenset(("same_bracket", "station_date"))


def _no_tail_conflict_key(df: pd.DataFrame, row_idx: int, scope: str) -> tuple:
    row = df.iloc[row_idx]
    base = (str(row["station_id"]), str(row["market_date"]))
    if scope == "station_date":
        return base
    if scope == "same_bracket":
        return (*base, int(row["bracket_index"]))
    raise ValueError(f"unknown NO/TAIL conflict scope: {scope}")


def apply_no_tail_conflict_policy(
    bets: list[tuple],
    df: pd.DataFrame,
    *,
    policy: str = "allow_both",
    scope: str = "same_bracket",
) -> tuple[list[tuple], dict[str, int | str]]:
    """Resolve generated NO/TAIL conflicts in a narrow, opt-in layer."""
    if policy not in NO_TAIL_CONFLICT_POLICIES:
        raise ValueError(
            f"unknown NO/TAIL conflict policy: {policy}; "
            f"expected one of {sorted(NO_TAIL_CONFLICT_POLICIES)}"
        )
    if scope not in NO_TAIL_CONFLICT_SCOPES:
        raise ValueError(
            f"unknown NO/TAIL conflict scope: {scope}; "
            f"expected one of {sorted(NO_TAIL_CONFLICT_SCOPES)}"
        )

    groups: dict[tuple, list[tuple[int, str]]] = {}
    for pos, bet in enumerate(bets):
        strat = str(bet[8]) if len(bet) >= 9 else ""
        if strat not in ("NO", "TAIL"):
            continue
        row_idx = int(bet[1])
        key = _no_tail_conflict_key(df, row_idx, scope)
        groups.setdefault(key, []).append((pos, strat))

    drop_positions: set[int] = set()
    stats: dict[str, int | str] = {
        "policy": policy,
        "scope": scope,
        "conflict_groups": 0,
        "conflict_no_bets": 0,
        "conflict_tail_bets": 0,
        "dropped_no_bets": 0,
        "dropped_tail_bets": 0,
        "dropped_bets": 0,
        "kept_bets": len(bets),
    }

    for entries in groups.values():
        strats = {strat for _, strat in entries}
        if not {"NO", "TAIL"}.issubset(strats):
            continue

        stats["conflict_groups"] = int(stats["conflict_groups"]) + 1
        stats["conflict_no_bets"] = int(stats["conflict_no_bets"]) + sum(
            1 for _, strat in entries if strat == "NO"
        )
        stats["conflict_tail_bets"] = int(stats["conflict_tail_bets"]) + sum(
            1 for _, strat in entries if strat == "TAIL"
        )

        if policy == "prefer_tail":
            drop_positions.update(pos for pos, strat in entries if strat == "NO")
        elif policy == "prefer_no":
            drop_positions.update(pos for pos, strat in entries if strat == "TAIL")
        elif policy == "drop_both":
            drop_positions.update(pos for pos, _strat in entries)

    if drop_positions:
        stats["dropped_no_bets"] = sum(1 for pos in drop_positions if bets[pos][8] == "NO")
        stats["dropped_tail_bets"] = sum(1 for pos in drop_positions if bets[pos][8] == "TAIL")
        stats["dropped_bets"] = len(drop_positions)
        filtered = [bet for pos, bet in enumerate(bets) if pos not in drop_positions]
        stats["kept_bets"] = len(filtered)
        return filtered, stats

    return list(bets), stats


def _consensus_block_mask(df: pd.DataFrame, hour: int, threshold: float) -> np.ndarray:
    """Per-row mask: True when any bracket in the same (station, market_date) at the
    given entry hour shows yes_price >= threshold."""
    yp_col = f"yes_price_h{hour}"
    if yp_col not in df.columns:
        raise ValueError(f"missing {yp_col}; rebuild the base backtest decision table")
    group_max = df.groupby(["station_id", "market_date"])[yp_col].transform("max")
    return (group_max >= threshold).to_numpy()


def candidates_3strats(df, cfg: LiveConfig, *, enable_no=True, enable_yes_mid=False,
                       enable_tail=False, enable_yhigh=False,
                       yes_mid_alpha=1.5, tail_alpha=2.0,
                       tail_n_required=4,
                       yes_mid_signal="p_B_50", yes_mid_fp_min=0.10, yes_mid_fp_max=0.30,
                       yes_mid_max_edge=0.30,
                       tail_vote_signals=None, tail_fp_min=0.001, tail_fp_max=0.10,
                       tail_bracket_kinds=None,
                       yhigh_signal="p_B_50", yhigh_fp_min=0.50, yhigh_fp_max=1.00,
                       yhigh_min_edge=0.025, yhigh_max_edge=0.30,
                       no_signal_for_high_bracket="p_B_50",
                       no_min_fill_price_for_high=0.50,
                       no_max_edge_for_high=0.30,
                       no_entry_hours=None, yes_mid_entry_hours=None,
                       tail_entry_hours=None, yhigh_entry_hours=None,
                       min_lut_n=LUT_MIN_N_FOR_SHRINKAGE,
                       consensus_skip_threshold: float | None = None,
                       no_consensus_skip_threshold: float | None = None,
                       tail_consensus_skip_threshold: float | None = None,
                       ymid_consensus_skip_threshold: float | None = None,
                       yhigh_consensus_skip_threshold: float | None = None):
    """Generate candidates from up to 4 strategies."""
    md = df["market_date"].to_numpy()
    won = df["won_yes"].to_numpy()
    n_ok = df["n_cum"].fillna(0).to_numpy(dtype=float) >= float(min_lut_n)
    bracket_high = df["bracket_kind"].to_numpy() == "high"
    bracket_kind_arr = df["bracket_kind"].to_numpy()
    bets = []

    no_T = no_consensus_skip_threshold if no_consensus_skip_threshold is not None else consensus_skip_threshold
    tail_T = tail_consensus_skip_threshold if tail_consensus_skip_threshold is not None else consensus_skip_threshold
    ymid_T = ymid_consensus_skip_threshold if ymid_consensus_skip_threshold is not None else consensus_skip_threshold
    yhigh_T = yhigh_consensus_skip_threshold if yhigh_consensus_skip_threshold is not None else consensus_skip_threshold

    if enable_no:
        p_strict = df[cfg.signal_col].to_numpy(dtype=float)
        p_ext = df[no_signal_for_high_bracket].to_numpy(dtype=float)
        emitted = np.zeros(len(df), dtype=bool)
        for hour in _hours(no_entry_hours, tuple(range(24)) if cfg.entry_local_hour == 0 else (cfg.entry_local_hour,)):
            _yp, np_p, vol, liq, spr, entry_ts, not_leakage = _entry_arrays(df, hour)
            strict_edge = (1.0 - p_strict) - np_p - fee(cfg.poly_fee_theta, np_p)
            strict = (
                n_ok & ~np.isnan(p_strict) & ~np.isnan(np_p) & not_leakage &
                (np_p >= cfg.no_min_fill_price) & (np_p >= cfg.min_fill_price) &
                (strict_edge >= cfg.no_min_edge) & (strict_edge <= cfg.max_edge) &
                (vol >= cfg.min_bvol)
            )

            ext_edge = (1.0 - p_ext) - np_p - fee(cfg.poly_fee_theta, np_p)
            extension = (
                n_ok & ~np.isnan(p_ext) & ~np.isnan(np_p) & not_leakage & bracket_high &
                (np_p >= no_min_fill_price_for_high) & (np_p >= cfg.min_fill_price) &
                (ext_edge >= cfg.no_min_edge) & (ext_edge <= no_max_edge_for_high) &
                (vol >= cfg.min_bvol) & ~strict
            )
            take = (strict | extension) & ~emitted
            if no_T is not None:
                take &= ~_consensus_block_mask(df, hour, no_T)
            signal_p = np.where(extension, 1.0 - p_ext, 1.0 - p_strict)
            _append_bets(
                bets, df, take, md=md, won=won, side="NO", price=np_p,
                signal_p=signal_p, liq=liq, spr=spr, entry_ts=entry_ts, strat="NO",
            )
            emitted |= take

    if enable_yes_mid:
        p = df[yes_mid_signal].to_numpy(dtype=float)
        emitted = np.zeros(len(df), dtype=bool)
        for hour in _hours(yes_mid_entry_hours, (cfg.entry_local_hour,)):
            yp, _np_p, vol, liq, spr, entry_ts, not_leakage = _entry_arrays(df, hour)
            yes_edge_pos = p - yp - fee(cfg.poly_fee_theta, yp)
            ymid_pass = (
                n_ok & ~np.isnan(p) & ~np.isnan(yp) & not_leakage &
                (yp >= yes_mid_fp_min) & (yp <= yes_mid_fp_max) & (yp >= cfg.min_fill_price) &
                (p >= yes_mid_alpha * yp) &
                (yes_edge_pos <= yes_mid_max_edge) &
                (vol >= cfg.min_bvol) & ~emitted
            )
            if ymid_T is not None:
                ymid_pass &= ~_consensus_block_mask(df, hour, ymid_T)
            _append_bets(
                bets, df, ymid_pass, md=md, won=won, side="YES", price=yp,
                signal_p=p, liq=liq, spr=spr, entry_ts=entry_ts, strat="YMID",
            )
            emitted |= ymid_pass

    if enable_tail:
        sigs = tuple(tail_vote_signals or ("p_E", "p_B_50", "p_L_loose", "p_Shrink_n10"))
        emitted = np.zeros(len(df), dtype=bool)
        for hour in _hours(tail_entry_hours, (cfg.entry_local_hour,)):
            yp, _np_p, vol, liq, spr, entry_ts, not_leakage = _entry_arrays(df, hour)
            votes = np.zeros(len(df), dtype=int)
            any_nan = np.zeros(len(df), dtype=bool)
            p_avg = np.zeros(len(df), dtype=float)
            for sig in sigs:
                ps = df[sig].to_numpy(dtype=float)
                votes += ((ps >= tail_alpha * yp) & ~np.isnan(ps)).astype(int)
                any_nan |= np.isnan(ps)
                p_avg += np.nan_to_num(ps, nan=0.0)
            p_avg /= len(sigs)
            tail_pass = (
                n_ok & (yp >= tail_fp_min) & (yp <= tail_fp_max) & (yp >= cfg.min_fill_price) &
                (votes >= tail_n_required) & ~any_nan & not_leakage &
                (vol >= cfg.min_bvol) & ~emitted
            )
            if tail_bracket_kinds is not None:
                tail_pass &= np.isin(bracket_kind_arr, tuple(tail_bracket_kinds))
            if tail_T is not None:
                tail_pass &= ~_consensus_block_mask(df, hour, tail_T)
            _append_bets(
                bets, df, tail_pass, md=md, won=won, side="YES", price=yp,
                signal_p=p_avg, liq=liq, spr=spr, entry_ts=entry_ts, strat="TAIL",
            )
            emitted |= tail_pass

    if enable_yhigh:
        p = df[yhigh_signal].to_numpy(dtype=float)
        emitted = np.zeros(len(df), dtype=bool)
        for hour in _hours(yhigh_entry_hours, (cfg.entry_local_hour,)):
            yp, _np_p, vol, liq, spr, entry_ts, not_leakage = _entry_arrays(df, hour)
            yhigh_edge_pos = p - yp - fee(cfg.poly_fee_theta, yp)
            yhigh_pass = (
                n_ok & ~np.isnan(p) & ~np.isnan(yp) & not_leakage & bracket_high &
                (yp >= yhigh_fp_min) & (yp <= yhigh_fp_max) & (yp >= cfg.min_fill_price) &
                (yhigh_edge_pos >= yhigh_min_edge) & (yhigh_edge_pos <= yhigh_max_edge) &
                (vol >= cfg.min_bvol) & ~emitted
            )
            if yhigh_T is not None:
                yhigh_pass &= ~_consensus_block_mask(df, hour, yhigh_T)
            _append_bets(
                bets, df, yhigh_pass, md=md, won=won, side="YES", price=yp,
                signal_p=p, liq=liq, spr=spr, entry_ts=entry_ts, strat="YHIGH",
            )
            emitted |= yhigh_pass

    return bets


def evaluate_3strats(df, cfg: LiveConfig, window_start, window_end, **kwargs):
    """Run a 4-strategy portfolio under live-matching execution with walk_book."""
    bets = [b for b in candidates_3strats(df, cfg, **kwargs)
            if window_start <= b[0] <= window_end]
    bets.sort(key=lambda b: (b[0], b[9] if len(b) >= 10 else 0, b[1], b[8]))

    capital = cfg.initial_bankroll_usd
    peak = capital
    pnl_list = []; stake_list = []; cap_curve = [capital]
    per_strat = {}
    skip_walk = 0

    for bet in bets:
        date, i, side, fp_displayed, signal_p, liq, spread, will_win, strat = bet[:9]
        target_usd = capital * cfg.max_bet_capital_frac
        dd = (peak - capital) / peak if peak > 1e-9 else 0.0
        if dd >= cfg.max_dd:
            target_usd *= 0.5
        if target_usd < cfg.min_bet_usd or target_usd > capital:
            continue

        min_edge = execution_min_edge_for_strategy(cfg, strat)
        entry_ladder = _ask_ladder_for_df_row(
            df, i, side, bet[9] if len(bet) >= 10 else None
        )
        result = simulate_walk_book(target_usd, fp_displayed, liq, signal_p,
                                     cfg.poly_fee_theta, min_edge, cfg.min_bet_usd,
                                     entry_spread=spread, ask_ladder=entry_ladder)
        if result is None:
            skip_walk += 1
            continue
        stake, fp_actual, _ = result

        entry_fee = stake * fee(cfg.poly_fee_theta, fp_actual) / fp_actual
        if will_win:
            net = stake * (1.0 - fp_actual) / fp_actual - entry_fee
        else:
            net = -stake - entry_fee

        capital += net
        peak = max(peak, capital)
        if capital < 1.0:
            break

        pnl_list.append(net); stake_list.append(stake); cap_curve.append(capital)
        ps = per_strat.setdefault(strat, {"n": 0, "pnl": 0, "stake": 0, "wins": 0,
                                          "claimed_sum": 0.0})
        ps["n"] += 1
        ps["pnl"] += net
        ps["stake"] += stake
        ps["wins"] += 1 if will_win else 0
        ps["claimed_sum"] += float(signal_p)

    if not pnl_list:
        return {"n": 0, "pnl": 0, "final_bankroll": cfg.initial_bankroll_usd,
                "max_dd_pct_peak": 0, "per_strat": {}}

    pnl_arr = np.array(pnl_list); cap_arr = np.array(cap_curve)
    peak_arr = np.maximum.accumulate(cap_arr)
    dd_pct = float(abs(((cap_arr - peak_arr) / np.where(peak_arr > 0, peak_arr, 1.0)).min()))

    for strat, ps in per_strat.items():
        ps["wr"] = ps["wins"] / ps["n"] if ps["n"] else 0
        ps["ros"] = ps["pnl"] / ps["stake"] if ps["stake"] > 0 else 0
        ps["avg_stake"] = ps["stake"] / ps["n"] if ps["n"] else 0
        # Honest add-ons: win-rate SE + model calibration (claimed vs realized).
        ps["wr_se"] = round(hr.proportion_se(int(ps["wins"]), int(ps["n"])), 4) if ps["n"] else 0
        claimed_mean = ps["claimed_sum"] / ps["n"] if ps["n"] else 0
        ps["claimed_mean"] = round(claimed_mean, 4)
        ps["realized_mean"] = round(ps["wr"], 4)
        ps["overconf_pp"] = round((claimed_mean - ps["wr"]) * 100.0, 2)

    return {
        "n": len(pnl_arr),
        "pnl": float(pnl_arr.sum()),
        "stake": float(sum(stake_list)),
        "wr": float((pnl_arr > 0).mean()),
        "ros": float(pnl_arr.sum() / sum(stake_list)) if stake_list else 0,
        "max_dd_pct_peak": dd_pct,
        "final_bankroll": float(cap_arr[-1]),
        "per_strat": per_strat,
    }


def main():
    df = pd.read_parquet(PARQUET_OUT)
    print(f"Loaded {len(df)} rows.\n")

    print("=" * 170)
    print("LIVE-MATCHING EVALUATOR")
    print("Execution: live-like sizing, fee = 0.05*p*(1-p), DD-halve at configured max_dd, MIN_BVOL=$50.")
    print("=" * 170)

    configs = [
        ("CURRENT LIVE BOT (exact match)", LiveConfig()),
        ("Live + lower vol gate ($100)", LiveConfig(min_bvol=100.0)),
        ("Live + no vol gate", LiveConfig(min_bvol=0.0)),
        ("Live + relax NO floor 0.55", LiveConfig(no_min_fill_price=0.55)),
        ("Live + relax YES floor 0.10", LiveConfig(yes_min_fill_price=0.10, yes_min_edge=-0.30, yes_max_edge=0.30)),
    ]
    for label, cfg in configs:
        print(f"\n{label}")
        for window_label, ws, we, days in [("TRAIN (60d)", TRAIN_START, TRAIN_END, 60),
                                              ("TEST  (28d)", TEST_START, TEST_END, 28)]:
            r = evaluate_live_match(df, cfg, ws, we)
            print("  " + fmt(window_label, r, days))

    # ===========================================================================
    # 4-strategy portfolio under live execution
    # ===========================================================================
    print("\n" + "=" * 170)
    print("4-STRATEGY PORTFOLIO  (NO + YMID + TAIL + YHIGH)  under LIVE-MATCHING execution")
    print("All under: live-like sizing, fee=theta*p*(1-p), DD-halve at configured max_dd, MIN_BVOL=$50")
    print("=" * 170)

    cfg100 = LiveConfig(min_bvol=50.0)
    portfolios = [
        ("NO only (live bot)",        dict(enable_no=True,  enable_yes_mid=False, enable_tail=False)),
        ("NO + YMID",                 dict(enable_no=True,  enable_yes_mid=True,  enable_tail=False)),
        ("NO + TAIL",                 dict(enable_no=True,  enable_yes_mid=False, enable_tail=True)),
        ("NO + YMID + TAIL",          dict(enable_no=True,  enable_yes_mid=True,  enable_tail=True)),
        ("NO + YMID + TAIL + YHIGH",  dict(enable_no=True,  enable_yes_mid=True,  enable_tail=True, enable_yhigh=True)),
        ("YMID only",                 dict(enable_no=False, enable_yes_mid=True,  enable_tail=False)),
        ("TAIL only",                 dict(enable_no=False, enable_yes_mid=False, enable_tail=True)),
        ("YHIGH only",                dict(enable_no=False, enable_yes_mid=False, enable_tail=False, enable_yhigh=True)),
    ]
    for label, kw in portfolios:
        print(f"\n{label}")
        for window_label, ws, we, days in [("TRAIN (60d)", TRAIN_START, TRAIN_END, 60),
                                              ("TEST  (28d)", TEST_START, TEST_END, 28)]:
            r = evaluate_3strats(df, cfg100, ws, we, **kw)
            print(f"  {window_label:<14} n={r['n']:>4} ({r['n']/days:>5.1f}/d) "
                  f"pnl=${r['pnl']:>+9,.2f} (${r['pnl']/days:>+7.2f}/d) "
                  f"dd={r['max_dd_pct_peak']*100:>5.1f}% final=${r['final_bankroll']:>10,.2f}")
            if r["per_strat"]:
                for s, ps in r["per_strat"].items():
                    print(f"    {s:<10} n={ps['n']:>4} pnl=${ps['pnl']:>+8,.2f} "
                          f"avg_stake=${ps['avg_stake']:>6.2f} ROS={ps['ros']*100:>+6.2f}% "
                          f"wr={ps['wr']*100:>5.1f}%+-{ps.get('wr_se', 0)*100:>3.1f} "
                          f"claim={ps.get('claimed_mean', 0)*100:>5.1f}% overconf={ps.get('overconf_pp', 0):>+5.1f}pp")


if __name__ == "__main__":
    main()
