"""Focused L2-depth feature tests for the current NO+TAIL champion.

This is a narrow diagnostic runner, not a broad optimizer. It reuses the
existing candidate generator and simulator, then tests features made possible by
the new per-hour ask ladders:

- real L2 entry execution
- TAIL position relative to the market-implied mode
- displayed price vs L2 best-ask dislocation
- realized fill-ratio floors
- execution_min_edge sensitivity
"""

from __future__ import annotations

import copy
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backtest.lib.live_match_eval import (  # noqa: E402
    LiveConfig,
    apply_no_tail_conflict_policy,
    ask_ladder_for_record,
    bid_ladder_for_record,
    candidates_3strats,
)
from backtest.scripts.lut_range_chunks import Chunk, date_chunks, tp_sl_from_config  # noqa: E402
from backtest.scripts.measure_tp_sl import (  # noqa: E402
    MARKET_DB,
    RANKING_BR,
    _candidate_kwargs,
    _live_config_from_spec,
    _no_tail_conflict_config,
    _size_fracs,
    _stake_caps_for_bankroll,
    _tail_delayed_entry_threshold,
    apply_tail_delayed_entry,
    load_metrics_by_slug,
    load_prices_by_slug_side,
    simulate,
)

DEFAULT_PARQUET = REPO_ROOT / "backtest" / "data" / "decision_table_may11plus_l2.parquet"
DEFAULT_CONFIG = REPO_ROOT / "backtest" / "configs" / "candidate_l2_depth.json"
OUT_CSV = REPO_ROOT / "backtest" / "results" / "l2_depth_feature_sweep.csv"
OUT_MD = REPO_ROOT / "backtest" / "results" / "l2_depth_feature_sweep.md"

BetFilter = Callable[[list[tuple], pd.DataFrame, list[dict]], list[tuple]]
CfgMutator = Callable[[LiveConfig], None]
ConfigMutator = Callable[[dict], None]


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    bet_filter: BetFilter | None = None
    cfg_mutator: CfgMutator | None = None
    config_mutator: ConfigMutator | None = None
    min_fill_ratio: dict[str, float] | None = None


def record_columns(df: pd.DataFrame) -> list[str]:
    cols = ["market_slug", "entry_ts_unix", "close_ts_unix"]
    for hour in range(24):
        cols.extend([
            f"entry_ts_h{hour}",
            f"yes_bid_ladder_h{hour}",
            f"yes_ask_ladder_h{hour}",
            f"no_bid_ladder_h{hour}",
            f"no_ask_ladder_h{hour}",
        ])
    return [c for c in cols if c in df.columns]


def market_mode_index(df: pd.DataFrame, hour: int = 0) -> dict[tuple[str, str], int]:
    price_col = f"yes_price_h{hour}"
    work = df[["station_id", "market_date", "bracket_index"]].copy()
    work["price"] = pd.to_numeric(df[price_col], errors="coerce")
    work = work.dropna(subset=["price"])
    idx = work.groupby(["station_id", "market_date"])["price"].idxmax()
    peak = work.loc[idx, ["station_id", "market_date", "bracket_index"]]
    return {
        (str(r.station_id), str(r.market_date)): int(r.bracket_index)
        for r in peak.itertuples()
    }


def tail_offset(df: pd.DataFrame, mode: dict[tuple[str, str], int], row_idx: int) -> int | None:
    row = df.iloc[row_idx]
    center = mode.get((str(row["station_id"]), str(row["market_date"])))
    if center is None:
        return None
    return int(row["bracket_index"]) - center


def filter_no_tail(bets: list[tuple], _df: pd.DataFrame, _records: list[dict]) -> list[tuple]:
    return [b for b in bets if b[8] != "TAIL"]


def chain_filters(*filters: BetFilter) -> BetFilter:
    def _f(bets: list[tuple], df: pd.DataFrame, records: list[dict]) -> list[tuple]:
        out = bets
        for filter_fn in filters:
            out = filter_fn(out, df, records)
        return out
    return _f


def filter_tail_side(*, keep_offsets: Callable[[int], bool]) -> BetFilter:
    def _f(bets: list[tuple], df: pd.DataFrame, _records: list[dict]) -> list[tuple]:
        mode = market_mode_index(df, 0)
        out = []
        for b in bets:
            if b[8] != "TAIL":
                out.append(b)
                continue
            offset = tail_offset(df, mode, int(b[1]))
            if offset is not None and keep_offsets(offset):
                out.append(b)
        return out
    return _f


def filter_tail_kind(kind: str) -> BetFilter:
    def _f(bets: list[tuple], df: pd.DataFrame, _records: list[dict]) -> list[tuple]:
        return [
            b for b in bets
            if b[8] != "TAIL" or str(df.iloc[int(b[1])]["bracket_kind"]) == kind
        ]
    return _f


def _bet_record_and_entry(bet: tuple, records: list[dict]) -> tuple[dict, str, int | float | None]:
    if len(bet) >= 10:
        _date, i, side, _fp, _signal_p, _liq, _spread, _will_win, _strat, entry_ts = bet[:10]
    else:
        _date, i, side, *_rest = bet
        entry_ts = records[int(i)].get("entry_ts_unix")
    return records[int(i)], str(side), entry_ts


def best_ask_for_bet(bet: tuple, records: list[dict]) -> float | None:
    ladder = _ladder_for_bet(bet, records)
    if not ladder:
        return None
    return min(price for price, _shares in ladder)


def _ladder_for_bet(bet: tuple, records: list[dict]):
    record, side, entry_ts = _bet_record_and_entry(bet, records)
    return ask_ladder_for_record(record, side, entry_ts)


def _bid_ladder_for_bet(bet: tuple, records: list[dict]):
    record, side, entry_ts = _bet_record_and_entry(bet, records)
    return bid_ladder_for_record(record, side, entry_ts)


def _depth_usd_within(bet: tuple, records: list[dict], max_premium: float | None = None) -> float | None:
    ladder = _ladder_for_bet(bet, records)
    if not ladder:
        return None
    displayed = float(bet[3])
    max_price = None if max_premium is None else displayed + float(max_premium)
    return sum(price * shares for price, shares in ladder if max_price is None or price <= max_price)


def _top_depth_usd(bet: tuple, records: list[dict]) -> float | None:
    ladder = _ladder_for_bet(bet, records)
    if not ladder:
        return None
    price, shares = min(ladder, key=lambda level: level[0])
    return price * shares


def _best_bid_for_bet(bet: tuple, records: list[dict]) -> float | None:
    ladder = _bid_ladder_for_bet(bet, records)
    if not ladder:
        return None
    return max(price for price, _shares in ladder)


def _bid_depth_usd_within(bet: tuple, records: list[dict], max_discount: float | None = None) -> float | None:
    ladder = _bid_ladder_for_bet(bet, records)
    if not ladder:
        return None
    displayed = float(bet[3])
    min_price = None if max_discount is None else displayed - float(max_discount)
    return sum(price * shares for price, shares in ladder if min_price is None or price >= min_price)


def _filter_on_optional_metric(
    metric: Callable[[tuple, list[dict]], float | None],
    keep: Callable[[float, tuple], bool],
    *,
    strategies: tuple[str, ...] | None = None,
) -> BetFilter:
    def _f(bets: list[tuple], _df: pd.DataFrame, records: list[dict]) -> list[tuple]:
        out = []
        for bet in bets:
            if strategies is not None and str(bet[8]) not in strategies:
                out.append(bet)
                continue
            value = metric(bet, records)
            if value is None or keep(value, bet):
                out.append(bet)
        return out
    return _f


def filter_l2_premium(max_premium: float, strategies: tuple[str, ...] | None = None) -> BetFilter:
    return _filter_on_optional_metric(
        best_ask_for_bet,
        lambda best_ask, bet: best_ask - float(bet[3]) <= max_premium,
        strategies=strategies,
    )


def filter_l2_spread(max_spread: float, strategies: tuple[str, ...] | None = None) -> BetFilter:
    def spread(bet: tuple, records: list[dict]) -> float | None:
        best_ask = best_ask_for_bet(bet, records)
        best_bid = _best_bid_for_bet(bet, records)
        if best_ask is None or best_bid is None:
            return None
        return best_ask - best_bid

    return _filter_on_optional_metric(
        spread,
        lambda value, _bet: value <= max_spread,
        strategies=strategies,
    )


def filter_l2_depth_usd(
    min_usd: float,
    *,
    max_premium: float | None = None,
    strategies: tuple[str, ...] | None = None,
) -> BetFilter:
    return _filter_on_optional_metric(
        lambda bet, records: _depth_usd_within(bet, records, max_premium=max_premium),
        lambda depth, _bet: depth >= min_usd,
        strategies=strategies,
    )


def filter_l2_bid_depth_usd(
    min_usd: float,
    *,
    max_discount: float | None = None,
    strategies: tuple[str, ...] | None = None,
) -> BetFilter:
    return _filter_on_optional_metric(
        lambda bet, records: _bid_depth_usd_within(bet, records, max_discount=max_discount),
        lambda depth, _bet: depth >= min_usd,
        strategies=strategies,
    )


def filter_top_depth_usd(
    min_usd: float,
    *,
    strategies: tuple[str, ...] | None = None,
) -> BetFilter:
    return _filter_on_optional_metric(
        _top_depth_usd,
        lambda depth, _bet: depth >= min_usd,
        strategies=strategies,
    )


def filter_require_l2(bets: list[tuple], _df: pd.DataFrame, records: list[dict]) -> list[tuple]:
    return [b for b in bets if best_ask_for_bet(b, records) is not None]


def set_exec_edges(no: float | None = None, tail: float | None = None) -> CfgMutator:
    def _m(cfg: LiveConfig) -> None:
        if no is not None:
            cfg.no_execution_min_edge = no
        if tail is not None:
            cfg.tail_execution_min_edge = tail
    return _m


def set_config_strategy_enabled(**enabled: bool) -> ConfigMutator:
    def _m(config: dict) -> None:
        for name, value in enabled.items():
            config.setdefault(name, {})["enabled"] = bool(value)
    return _m


def set_config_entry_hours(strategy: str, hours: tuple[int, ...]) -> ConfigMutator:
    def _m(config: dict) -> None:
        config.setdefault(strategy, {})["entry_local_hours"] = list(hours)
    return _m


def set_config_tail_params(
    *,
    alpha: float | None = None,
    fp_max: float | None = None,
    consensus: float | None = None,
    bracket_kinds: tuple[str, ...] | None = None,
) -> ConfigMutator:
    def _m(config: dict) -> None:
        tail = config.setdefault("TAIL", {})
        if alpha is not None:
            tail["alpha"] = float(alpha)
        if fp_max is not None:
            tail["fp_max"] = float(fp_max)
        if consensus is not None:
            tail["consensus_skip_threshold"] = float(consensus)
        if bracket_kinds is not None:
            tail["bracket_kinds"] = list(bracket_kinds)
    return _m


def set_config_no_params(
    *,
    no_min_fill_price: float | None = None,
    no_min_edge: float | None = None,
    max_edge: float | None = None,
) -> ConfigMutator:
    def _m(config: dict) -> None:
        no = config.setdefault("NO", {})
        if no_min_fill_price is not None:
            no["no_min_fill_price"] = float(no_min_fill_price)
        if no_min_edge is not None:
            no["no_min_edge"] = float(no_min_edge)
        if max_edge is not None:
            no["max_edge"] = float(max_edge)
    return _m


def set_config_no_signal(signal: str) -> ConfigMutator:
    """Swap the NO strict signal column (feeds LiveConfig.signal_col)."""
    def _m(config: dict) -> None:
        config.setdefault("NO", {})["signal"] = signal
    return _m


def set_config_tail_vote_signals(signals: tuple[str, ...]) -> ConfigMutator:
    """Replace the TAIL consensus vote signal set."""
    def _m(config: dict) -> None:
        config.setdefault("TAIL", {})["vote_signals"] = list(signals)
    return _m


def combine_config_mutators(*mutators: ConfigMutator) -> ConfigMutator:
    def _m(config: dict) -> None:
        for mutator in mutators:
            mutator(config)
    return _m


def build_bets_for_config(
    *,
    df: pd.DataFrame,
    config: dict,
    records: list[dict] | None = None,
    prices_by_ss: dict | None = None,
    metrics_idx: dict | None = None,
    apply_tail_delay: bool = True,
) -> tuple[list[tuple], dict[str, object]]:
    cfg_template = _live_config_from_spec(config, RANKING_BR)
    bets = candidates_3strats(df, cfg_template, **_candidate_kwargs(config))
    policy, scope = _no_tail_conflict_config(config)
    bets, conflict_stats = apply_no_tail_conflict_policy(bets, df, policy=policy, scope=scope)
    stats: dict[str, object] = dict(conflict_stats)
    tail_threshold = _tail_delayed_entry_threshold(config) if apply_tail_delay else None
    if apply_tail_delay:
        if tail_threshold is not None and (
            records is None or prices_by_ss is None or metrics_idx is None
        ):
            raise ValueError(
                "TAIL.delayed_entry_fp_max requires records, prices_by_ss, and metrics_idx "
                "when building configured backtest bets"
            )
        bets, tail_stats = apply_tail_delayed_entry(
            bets=bets,
            records=records or [],
            prices_by_ss=prices_by_ss or {},
            metrics_idx=metrics_idx or {},
            threshold=tail_threshold,
        )
        stats["tail_delayed_entry"] = tail_stats
        if tail_threshold is not None:
            stats["tail_delayed_entry_threshold"] = tail_threshold
    bets.sort(key=lambda b: (b[0], b[9] if len(b) >= 10 else 0, b[1], b[8]))
    return bets, stats


def score_variant(
    *,
    variant: Variant,
    base_bets: list[tuple],
    df: pd.DataFrame,
    records: list[dict],
    chunks: list[Chunk],
    config: dict,
    prices_by_ss: dict,
    metrics_idx: dict,
) -> dict:
    cfg = _live_config_from_spec(config, RANKING_BR)
    if variant.cfg_mutator is not None:
        variant.cfg_mutator(cfg)

    bets = list(base_bets)
    if variant.bet_filter is not None:
        bets = variant.bet_filter(bets, df, records)

    tp_sl = tp_sl_from_config(config)
    size_frac = _size_fracs(config)
    max_stake = _stake_caps_for_bankroll(config, RANKING_BR)
    max_l2_ask_premium = (
        float(config["max_l2_ask_premium"])
        if config.get("max_l2_ask_premium") is not None else None
    )
    row: dict[str, object] = {
        "variant": variant.name,
        "description": variant.description,
        "generated_bets": len(base_bets),
        "kept_bets": len(bets),
    }

    total_pnl = 0.0
    total_n = 0
    total_no_pnl = 0.0
    total_tail_pnl = 0.0
    worst = None
    pos = 0
    max_dd = 0.0

    for chunk in chunks:
        result = simulate(
            bets,
            prices_by_ss,
            metrics_idx,
            records,
            cfg,
            tp_sl,
            chunk.start,
            chunk.end,
            size_frac=size_frac,
            max_stake=max_stake,
            min_fill_ratio=variant.min_fill_ratio,
            max_l2_ask_premium=max_l2_ask_premium,
        )
        per = result.get("per_strat", {})
        no_pnl = float(per.get("NO", {}).get("pnl", 0.0))
        tail_pnl = float(per.get("TAIL", {}).get("pnl", 0.0))
        pnl = float(result["pnl"])
        dd = float(result["max_dd_pct_peak"]) * 100.0
        n = int(result["n"])
        row[f"{chunk.label}_n"] = n
        row[f"{chunk.label}_pnl"] = round(pnl, 4)
        row[f"{chunk.label}_dd"] = round(dd, 4)
        row[f"{chunk.label}_no_pnl"] = round(no_pnl, 4)
        row[f"{chunk.label}_tail_pnl"] = round(tail_pnl, 4)

        total_pnl += pnl
        total_n += n
        total_no_pnl += no_pnl
        total_tail_pnl += tail_pnl
        worst = pnl if worst is None else min(worst, pnl)
        pos += 1 if pnl > 0 else 0
        max_dd = max(max_dd, dd)

    row.update({
        "total_n": total_n,
        "total_pnl": round(total_pnl, 4),
        "no_pnl": round(total_no_pnl, 4),
        "tail_pnl": round(total_tail_pnl, 4),
        "positive_chunks": pos,
        "worst_chunk_pnl": round(float(worst or 0.0), 4),
        "max_chunk_dd": round(max_dd, 4),
    })
    return row


def variants() -> list[Variant]:
    base = [
        Variant("baseline_l2", "Current champion with L2 ask-ladder entry execution."),
        Variant("no_only", "Disable TAIL; NO sleeve only.", bet_filter=filter_no_tail),
        Variant("tail_right_center", "Keep TAIL only at/above the market-implied mode.", bet_filter=filter_tail_side(keep_offsets=lambda x: x >= 0)),
        Variant("tail_right_only", "Keep TAIL only hotter than the market-implied mode.", bet_filter=filter_tail_side(keep_offsets=lambda x: x > 0)),
        Variant("tail_near_mode_abs_le2", "Keep TAIL only within two brackets of the market-implied mode.", bet_filter=filter_tail_side(keep_offsets=lambda x: abs(x) <= 2)),
        Variant("tail_far_abs_ge3", "Keep TAIL only at least three brackets away from the market-implied mode.", bet_filter=filter_tail_side(keep_offsets=lambda x: abs(x) >= 3)),
        Variant("tail_high_only", "Keep only high/open-hot TAIL bets.", bet_filter=filter_tail_kind("high")),
        Variant("tail_mid_only", "Keep only interior TAIL bets.", bet_filter=filter_tail_kind("mid")),
        Variant("tail_low_only", "Keep only low/open-cold TAIL bets.", bet_filter=filter_tail_kind("low")),
        Variant("premium_le_0", "When L2 is present, require best ask <= displayed price.", bet_filter=filter_l2_premium(0.00)),
        Variant("premium_le_1c", "When L2 is present, require best ask <= displayed price + 1c.", bet_filter=filter_l2_premium(0.01)),
        Variant("premium_le_2c", "When L2 is present, require best ask <= displayed price + 2c.", bet_filter=filter_l2_premium(0.02)),
        Variant("premium_le_5c", "When L2 is present, require best ask <= displayed price + 5c.", bet_filter=filter_l2_premium(0.05)),
        Variant("require_l2", "Evaluate only bets with an L2 ladder in the parquet.", bet_filter=filter_require_l2),
        Variant("no_fill_50pct", "Require NO real fill >= 50% of target.", min_fill_ratio={"NO": 0.50}),
        Variant("no_fill_75pct", "Require NO real fill >= 75% of target.", min_fill_ratio={"NO": 0.75}),
        Variant("tail_fill_50pct", "Require TAIL real fill >= 50% of target.", min_fill_ratio={"TAIL": 0.50}),
        Variant("all_fill_50pct", "Require every real fill >= 50% of target.", min_fill_ratio={"NO": 0.50, "TAIL": 0.50}),
        Variant("exec_edge_5pp", "NO and TAIL execution_min_edge = 5pp.", cfg_mutator=set_exec_edges(no=0.05, tail=0.05)),
        Variant("exec_edge_7pp", "NO and TAIL execution_min_edge = 7pp.", cfg_mutator=set_exec_edges(no=0.07, tail=0.07)),
        Variant("exec_edge_9pp", "NO and TAIL execution_min_edge = 9pp.", cfg_mutator=set_exec_edges(no=0.09, tail=0.09)),
        Variant(
            "right_center_exec_5pp",
            "TAIL at/above mode plus 5pp execution edge.",
            bet_filter=filter_tail_side(keep_offsets=lambda x: x >= 0),
            cfg_mutator=set_exec_edges(no=0.05, tail=0.05),
        ),
        Variant(
            "no_only_exec_5pp",
            "NO only plus 5pp execution edge.",
            bet_filter=filter_no_tail,
            cfg_mutator=set_exec_edges(no=0.05),
        ),
        Variant(
            "premium5_exec5",
            "5c L2 premium guard plus 5pp execution edge.",
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.05, tail=0.05),
        ),
        Variant(
            "premium5_exec7",
            "5c L2 premium guard plus 7pp execution edge.",
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_exec9",
            "5c L2 premium guard plus 9pp execution edge.",
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.09, tail=0.09),
        ),
        Variant(
            "premium2_exec5",
            "2c L2 premium guard plus 5pp execution edge.",
            bet_filter=filter_l2_premium(0.02),
            cfg_mutator=set_exec_edges(no=0.05, tail=0.05),
        ),
        Variant(
            "tail_mid_exec5",
            "Interior TAIL only plus 5pp execution edge.",
            bet_filter=filter_tail_kind("mid"),
            cfg_mutator=set_exec_edges(no=0.05, tail=0.05),
        ),
        Variant(
            "tail_mid_exec7",
            "Interior TAIL only plus 7pp execution edge.",
            bet_filter=filter_tail_kind("mid"),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_tail_mid",
            "5c L2 premium guard plus interior TAIL only.",
            bet_filter=chain_filters(filter_l2_premium(0.05), filter_tail_kind("mid")),
        ),
        Variant(
            "premium5_tail_mid_exec5",
            "5c L2 premium guard plus interior TAIL only plus 5pp execution edge.",
            bet_filter=chain_filters(filter_l2_premium(0.05), filter_tail_kind("mid")),
            cfg_mutator=set_exec_edges(no=0.05, tail=0.05),
        ),
        Variant(
            "premium5_tail_mid_exec7",
            "5c L2 premium guard plus interior TAIL only plus 7pp execution edge.",
            bet_filter=chain_filters(filter_l2_premium(0.05), filter_tail_kind("mid")),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_tail_mid_exec9",
            "5c L2 premium guard plus interior TAIL only plus 9pp execution edge.",
            bet_filter=chain_filters(filter_l2_premium(0.05), filter_tail_kind("mid")),
            cfg_mutator=set_exec_edges(no=0.09, tail=0.09),
        ),
        Variant(
            "premium5_no_only",
            "5c L2 premium guard with NO sleeve only.",
            bet_filter=chain_filters(filter_l2_premium(0.05), filter_no_tail),
        ),
        Variant(
            "premium5_no_only_exec5",
            "5c L2 premium guard with NO only plus 5pp execution edge.",
            bet_filter=chain_filters(filter_l2_premium(0.05), filter_no_tail),
            cfg_mutator=set_exec_edges(no=0.05),
        ),
    ]

    premium_exec_grid: list[Variant] = []
    for premium in (0.03, 0.04, 0.06, 0.07, 0.10):
        for edge in (0.05, 0.07, 0.09):
            premium_exec_grid.append(Variant(
                f"premium{int(premium * 100):02d}_exec{int(edge * 100):02d}",
                f"{int(premium * 100)}c L2 premium guard plus {edge:.0%} execution edge.",
                bet_filter=filter_l2_premium(premium),
                cfg_mutator=set_exec_edges(no=edge, tail=edge),
            ))

    independent_edge_grid: list[Variant] = []
    for no_edge in (0.03, 0.05, 0.07, 0.09):
        for tail_edge in (0.03, 0.05, 0.07, 0.09, 0.11):
            if no_edge == 0.03 and tail_edge == 0.03:
                continue
            independent_edge_grid.append(Variant(
                f"premium5_noe{int(no_edge * 100):02d}_taile{int(tail_edge * 100):02d}",
                f"5c premium guard with NO execution edge {no_edge:.0%}, TAIL execution edge {tail_edge:.0%}.",
                bet_filter=filter_l2_premium(0.05),
                cfg_mutator=set_exec_edges(no=no_edge, tail=tail_edge),
            ))

    depth_grid: list[Variant] = []
    for min_usd in (1.0, 2.0, 3.0, 5.0):
        depth_grid.extend([
            Variant(
                f"depth5c_all_ge{int(min_usd)}",
                f"Require at least ${min_usd:.0f} executable L2 ask depth within displayed+5c.",
                bet_filter=filter_l2_depth_usd(min_usd, max_premium=0.05),
            ),
            Variant(
                f"top_all_ge{int(min_usd)}",
                f"Require best ask level notional >= ${min_usd:.0f}.",
                bet_filter=filter_top_depth_usd(min_usd),
            ),
            Variant(
                f"premium5_exec7_depth5c_ge{int(min_usd)}",
                f"5c premium + 7pp execution edge + ${min_usd:.0f} depth within 5c.",
                bet_filter=chain_filters(
                    filter_l2_premium(0.05),
                    filter_l2_depth_usd(min_usd, max_premium=0.05),
                ),
                cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
            ),
        ])

    strategy_specific_l2 = [
        Variant(
            "no_premium5_exec7",
            "Apply 5c premium guard only to NO, with 7pp execution edge.",
            bet_filter=filter_l2_premium(0.05, strategies=("NO",)),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "tail_premium5_exec7",
            "Apply 5c premium guard only to TAIL, with 7pp execution edge.",
            bet_filter=filter_l2_premium(0.05, strategies=("TAIL",)),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "no_premium2_tail_premium5_exec7",
            "NO premium <=2c, TAIL premium <=5c, with 7pp execution edge.",
            bet_filter=chain_filters(
                filter_l2_premium(0.02, strategies=("NO",)),
                filter_l2_premium(0.05, strategies=("TAIL",)),
            ),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "no_premium5_tail_premium2_exec7",
            "NO premium <=5c, TAIL premium <=2c, with 7pp execution edge.",
            bet_filter=chain_filters(
                filter_l2_premium(0.05, strategies=("NO",)),
                filter_l2_premium(0.02, strategies=("TAIL",)),
            ),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_all_fill75_exec7",
            "5c premium + 7pp execution edge + require 75% target fill.",
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
            min_fill_ratio={"NO": 0.75, "TAIL": 0.75},
        ),
        Variant(
            "premium5_all_fill100_exec7",
            "5c premium + 7pp execution edge + require full target fill.",
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
            min_fill_ratio={"NO": 1.0, "TAIL": 1.0},
        ),
    ]

    hour_variants = [
        Variant(
            "premium5_exec7_no_h0",
            "5c premium + 7pp execution edge, NO only at local hour 0.",
            config_mutator=set_config_entry_hours("NO", (0,)),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_exec7_no_h0_3",
            "5c premium + 7pp execution edge, NO local hours 0-3.",
            config_mutator=set_config_entry_hours("NO", (0, 1, 2, 3)),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_exec7_no_h1_6",
            "5c premium + 7pp execution edge, NO local hours 1-6.",
            config_mutator=set_config_entry_hours("NO", (1, 2, 3, 4, 5, 6)),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_exec7_tail_h1",
            "5c premium + 7pp execution edge, TAIL local hour 1.",
            config_mutator=set_config_entry_hours("TAIL", (1,)),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_exec7_tail_h2",
            "5c premium + 7pp execution edge, TAIL local hour 2.",
            config_mutator=set_config_entry_hours("TAIL", (2,)),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_exec7_tail_h3",
            "5c premium + 7pp execution edge, TAIL local hour 3.",
            config_mutator=set_config_entry_hours("TAIL", (3,)),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_exec7_tail_h4",
            "5c premium + 7pp execution edge, TAIL local hour 4.",
            config_mutator=set_config_entry_hours("TAIL", (4,)),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_exec7_tail_h5",
            "5c premium + 7pp execution edge, TAIL local hour 5.",
            config_mutator=set_config_entry_hours("TAIL", (5,)),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_exec7_tail_h6",
            "5c premium + 7pp execution edge, TAIL local hour 6.",
            config_mutator=set_config_entry_hours("TAIL", (6,)),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_exec7_tail_h0_2",
            "5c premium + 7pp execution edge, TAIL local hours 0-2.",
            config_mutator=set_config_entry_hours("TAIL", (0, 1, 2)),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
    ]

    tail_h1_local_search: list[Variant] = []
    tail_h1_config = set_config_entry_hours("TAIL", (1,))
    for premium in (0.03, 0.04, 0.05, 0.06, 0.07, 0.10):
        for no_edge in (0.05, 0.07, 0.09):
            for tail_edge in (0.05, 0.07, 0.09, 0.11):
                tail_h1_local_search.append(Variant(
                    f"tailh1_p{int(premium * 100):02d}_noe{int(no_edge * 100):02d}_taile{int(tail_edge * 100):02d}",
                    (
                        f"TAIL h1, premium <= {int(premium * 100)}c, "
                        f"NO execution edge {no_edge:.0%}, TAIL execution edge {tail_edge:.0%}."
                    ),
                    config_mutator=tail_h1_config,
                    bet_filter=filter_l2_premium(premium),
                    cfg_mutator=set_exec_edges(no=no_edge, tail=tail_edge),
                ))

    tail_h1_shape_checks = [
        Variant(
            "tailh1_p5_e7_tail_mid",
            "TAIL h1 + 5c premium + 7pp edge + interior TAIL only.",
            config_mutator=tail_h1_config,
            bet_filter=chain_filters(filter_l2_premium(0.05), filter_tail_kind("mid")),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "tailh1_p5_e7_tail_high",
            "TAIL h1 + 5c premium + 7pp edge + high/open-hot TAIL only.",
            config_mutator=tail_h1_config,
            bet_filter=chain_filters(filter_l2_premium(0.05), filter_tail_kind("high")),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "tailh1_p5_e7_tail_low",
            "TAIL h1 + 5c premium + 7pp edge + low/open-cold TAIL only.",
            config_mutator=tail_h1_config,
            bet_filter=chain_filters(filter_l2_premium(0.05), filter_tail_kind("low")),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "tailh1_p5_e7_fill100",
            "TAIL h1 + 5c premium + 7pp edge + require full target fill.",
            config_mutator=tail_h1_config,
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
            min_fill_ratio={"NO": 1.0, "TAIL": 1.0},
        ),
        Variant(
            "tailh1_p5_e7_depth5c_ge2",
            "TAIL h1 + 5c premium + 7pp edge + $2 depth within 5c.",
            config_mutator=tail_h1_config,
            bet_filter=chain_filters(
                filter_l2_premium(0.05),
                filter_l2_depth_usd(2.0, max_premium=0.05),
            ),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "tailh1_p5_e7_enable_yhigh",
            "TAIL h1 + 5c premium + 7pp edge + re-enable YHIGH.",
            config_mutator=combine_config_mutators(
                tail_h1_config,
                set_config_strategy_enabled(YHIGH=True),
            ),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
    ]

    tail_h1_refined_grid: list[Variant] = []
    for premium in (0.07, 0.08, 0.09, 0.10, 0.12, 0.15):
        for no_edge in (0.03, 0.04, 0.05, 0.06):
            for tail_edge in (0.05, 0.06, 0.07, 0.08, 0.09):
                name = (
                    f"tailh1_ref_p{int(premium * 100):02d}_"
                    f"noe{int(no_edge * 100):02d}_taile{int(tail_edge * 100):02d}"
                )
                tail_h1_refined_grid.append(Variant(
                    name,
                    (
                        f"Refined TAIL h1, premium <= {int(premium * 100)}c, "
                        f"NO execution edge {no_edge:.0%}, TAIL execution edge {tail_edge:.0%}."
                    ),
                    config_mutator=tail_h1_config,
                    bet_filter=filter_l2_premium(premium),
                    cfg_mutator=set_exec_edges(no=no_edge, tail=tail_edge),
                ))

    selected_l2_profile = combine_config_mutators(
        set_config_entry_hours("TAIL", (1,)),
    )
    selected_l2_edges = set_exec_edges(no=0.05, tail=0.07)
    selected_l2_filter = filter_l2_premium(0.10)

    selected_hour_checks: list[Variant] = []
    for label, tail_hours in (
        ("tail_h0", (0,)),
        ("tail_h1", (1,)),
        ("tail_h2", (2,)),
        ("tail_h0_1", (0, 1)),
        ("tail_h1_2", (1, 2)),
        ("tail_h0_2", (0, 1, 2)),
    ):
        selected_hour_checks.append(Variant(
            f"sel_{label}",
            f"Selected L2 profile with TAIL local hours {tail_hours}.",
            config_mutator=set_config_entry_hours("TAIL", tail_hours),
            bet_filter=selected_l2_filter,
            cfg_mutator=selected_l2_edges,
        ))
    for label, no_hours in (
        ("no_h0", (0,)),
        ("no_h0_1", (0, 1)),
        ("no_h0_2", (0, 1, 2)),
        ("no_h0_3", (0, 1, 2, 3)),
        ("no_h0_4", (0, 1, 2, 3, 4)),
        ("no_h0_6", (0, 1, 2, 3, 4, 5, 6)),
        ("no_h1_6", (1, 2, 3, 4, 5, 6)),
        ("no_h0_8", (0, 1, 2, 3, 4, 5, 6, 7, 8)),
    ):
        selected_hour_checks.append(Variant(
            f"sel_{label}",
            f"Selected L2 profile with NO local hours {no_hours}.",
            config_mutator=combine_config_mutators(
                selected_l2_profile,
                set_config_entry_hours("NO", no_hours),
            ),
            bet_filter=selected_l2_filter,
            cfg_mutator=selected_l2_edges,
        ))

    strategy_specific_premium_grid: list[Variant] = []
    for no_premium in (0.03, 0.05, 0.07, 0.10, 0.12):
        for tail_premium in (0.03, 0.05, 0.07, 0.10, 0.12):
            strategy_specific_premium_grid.append(Variant(
                f"sel_nop{int(no_premium * 100):02d}_tailp{int(tail_premium * 100):02d}",
                (
                    f"Selected L2 profile with NO premium <= {int(no_premium * 100)}c "
                    f"and TAIL premium <= {int(tail_premium * 100)}c."
                ),
                config_mutator=selected_l2_profile,
                bet_filter=chain_filters(
                    filter_l2_premium(no_premium, strategies=("NO",)),
                    filter_l2_premium(tail_premium, strategies=("TAIL",)),
                ),
                cfg_mutator=selected_l2_edges,
            ))

    selected_shape_checks = [
        Variant(
            "sel_tail_mid",
            "Selected L2 profile with interior TAIL only.",
            config_mutator=selected_l2_profile,
            bet_filter=chain_filters(selected_l2_filter, filter_tail_kind("mid")),
            cfg_mutator=selected_l2_edges,
        ),
        Variant(
            "sel_tail_high",
            "Selected L2 profile with high/open-hot TAIL only.",
            config_mutator=selected_l2_profile,
            bet_filter=chain_filters(selected_l2_filter, filter_tail_kind("high")),
            cfg_mutator=selected_l2_edges,
        ),
        Variant(
            "sel_tail_low",
            "Selected L2 profile with low/open-cold TAIL only.",
            config_mutator=selected_l2_profile,
            bet_filter=chain_filters(selected_l2_filter, filter_tail_kind("low")),
            cfg_mutator=selected_l2_edges,
        ),
        Variant(
            "sel_tail_near_mode_abs_le2",
            "Selected L2 profile with TAIL within two brackets of market-implied mode.",
            config_mutator=selected_l2_profile,
            bet_filter=chain_filters(
                selected_l2_filter,
                filter_tail_side(keep_offsets=lambda x: abs(x) <= 2),
            ),
            cfg_mutator=selected_l2_edges,
        ),
        Variant(
            "sel_tail_far_abs_ge3",
            "Selected L2 profile with TAIL at least three brackets from market-implied mode.",
            config_mutator=selected_l2_profile,
            bet_filter=chain_filters(
                selected_l2_filter,
                filter_tail_side(keep_offsets=lambda x: abs(x) >= 3),
            ),
            cfg_mutator=selected_l2_edges,
        ),
    ]

    no_param_grid: list[Variant] = []
    for fill_price in (0.70, 0.75, 0.80):
        for min_edge in (0.07, 0.09, 0.11):
            for max_edge in (0.12, 0.15, 0.20):
                no_param_grid.append(Variant(
                    (
                        f"sel_nofp{int(fill_price * 100):02d}_"
                        f"nog{int(min_edge * 100):02d}_{int(max_edge * 100):02d}"
                    ),
                    (
                        f"Selected L2 profile with NO fill >= {fill_price:.0%}, "
                        f"NO edge [{min_edge:.0%}, {max_edge:.0%}]."
                    ),
                    config_mutator=combine_config_mutators(
                        selected_l2_profile,
                        set_config_no_params(
                            no_min_fill_price=fill_price,
                            no_min_edge=min_edge,
                            max_edge=max_edge,
                        ),
                    ),
                    bet_filter=selected_l2_filter,
                    cfg_mutator=selected_l2_edges,
                ))

    tail_param_grid: list[Variant] = []
    for alpha in (4.0, 4.5, 5.0):
        for fp_max in (0.03, 0.05, 0.07):
            for consensus in (0.40, 0.50, 0.60):
                tail_param_grid.append(Variant(
                    f"sel_taila{int(alpha * 10):02d}_fp{int(fp_max * 100):02d}_cs{int(consensus * 100):02d}",
                    (
                        f"Selected L2 profile with TAIL alpha {alpha:.1f}, "
                        f"fp_max {fp_max:.0%}, consensus skip {consensus:.0%}."
                    ),
                    config_mutator=combine_config_mutators(
                        selected_l2_profile,
                        set_config_tail_params(alpha=alpha, fp_max=fp_max, consensus=consensus),
                    ),
                    bet_filter=selected_l2_filter,
                    cfg_mutator=selected_l2_edges,
                ))

    l2_bid_spread_checks = [
        Variant(
            f"sel_spread_le_{int(max_spread * 100):02d}c",
            f"Selected L2 profile requiring L2 spread <= {int(max_spread * 100)}c when bid+ask exist.",
            config_mutator=selected_l2_profile,
            bet_filter=chain_filters(selected_l2_filter, filter_l2_spread(max_spread)),
            cfg_mutator=selected_l2_edges,
        )
        for max_spread in (0.03, 0.05, 0.08, 0.10)
    ]
    l2_bid_spread_checks.extend([
        Variant(
            f"sel_bid5c_ge{int(min_usd)}",
            f"Selected L2 profile requiring bid depth >= ${min_usd:.0f} within displayed-5c.",
            config_mutator=selected_l2_profile,
            bet_filter=chain_filters(
                selected_l2_filter,
                filter_l2_bid_depth_usd(min_usd, max_discount=0.05),
            ),
            cfg_mutator=selected_l2_edges,
        )
        for min_usd in (1.0, 2.0, 5.0)
    ])
    l2_bid_spread_checks.extend([
        Variant(
            f"sel_tail_bid5c_ge{int(min_usd)}",
            f"Selected L2 profile requiring TAIL bid depth >= ${min_usd:.0f} within displayed-5c.",
            config_mutator=selected_l2_profile,
            bet_filter=chain_filters(
                selected_l2_filter,
                filter_l2_bid_depth_usd(min_usd, max_discount=0.05, strategies=("TAIL",)),
            ),
            cfg_mutator=selected_l2_edges,
        )
        for min_usd in (1.0, 2.0, 5.0)
    ])

    disabled_sleeve_checks = [
        Variant(
            "premium5_exec7_enable_ymid",
            "5c premium + 7pp NO/TAIL edge, re-enable YMID under L2 execution.",
            config_mutator=set_config_strategy_enabled(YMID=True),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_exec7_enable_yhigh",
            "5c premium + 7pp NO/TAIL edge, re-enable YHIGH under L2 execution.",
            config_mutator=set_config_strategy_enabled(YHIGH=True),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
        Variant(
            "premium5_exec7_enable_ymid_yhigh",
            "5c premium + 7pp NO/TAIL edge, re-enable YMID and YHIGH under L2 execution.",
            config_mutator=set_config_strategy_enabled(YMID=True, YHIGH=True),
            bet_filter=filter_l2_premium(0.05),
            cfg_mutator=set_exec_edges(no=0.07, tail=0.07),
        ),
    ]

    # Recency-weighted LUT flavor variants (plan 2026-05-29-002 U3). Each mirrors
    # the current champion profile (selected L2 profile + TAIL alpha 4.0 / fp_max
    # 0.03 / consensus 0.40 + NO 0.05 / TAIL 0.07 edges + 10c premium guard) and
    # only swaps the calibration signal to the recency flavor, so the walk-forward
    # compares them head-to-head with sel_taila40_fp03_cs40.
    champion_tail_params = set_config_tail_params(alpha=4.0, fp_max=0.03, consensus=0.40)
    recency_checks: list[Variant] = []
    for hl in (15, 30):
        recency_sig = f"p_Recency_h{hl}"
        recency_checks.append(Variant(
            f"rec_no_h{hl}",
            f"Champion profile with NO strict signal = {recency_sig}.",
            config_mutator=combine_config_mutators(
                selected_l2_profile, champion_tail_params,
                set_config_no_signal(recency_sig),
            ),
            bet_filter=selected_l2_filter,
            cfg_mutator=selected_l2_edges,
        ))
        recency_checks.append(Variant(
            f"rec_tail_h{hl}",
            f"Champion profile with TAIL votes swapping p_Shrink_n10 -> {recency_sig}.",
            config_mutator=combine_config_mutators(
                selected_l2_profile, champion_tail_params,
                set_config_tail_vote_signals(("p_E", "p_B_50", "p_L_loose", recency_sig)),
            ),
            bet_filter=selected_l2_filter,
            cfg_mutator=selected_l2_edges,
        ))
        recency_checks.append(Variant(
            f"rec_both_h{hl}",
            f"Champion profile with NO + TAIL both on {recency_sig}.",
            config_mutator=combine_config_mutators(
                selected_l2_profile, champion_tail_params,
                set_config_no_signal(recency_sig),
                set_config_tail_vote_signals(("p_E", "p_B_50", "p_L_loose", recency_sig)),
            ),
            bet_filter=selected_l2_filter,
            cfg_mutator=selected_l2_edges,
        ))

    return (
        base
        + premium_exec_grid
        + independent_edge_grid
        + depth_grid
        + strategy_specific_l2
        + hour_variants
        + tail_h1_local_search
        + tail_h1_shape_checks
        + tail_h1_refined_grid
        + selected_hour_checks
        + strategy_specific_premium_grid
        + selected_shape_checks
        + no_param_grid
        + tail_param_grid
        + l2_bid_spread_checks
        + disabled_sleeve_checks
        + recency_checks
    )


def write_outputs(rows: list[dict], chunks: list[Chunk]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    ranked = sorted(
        rows,
        key=lambda r: (
            int(r["positive_chunks"]),
            float(r["worst_chunk_pnl"]),
            float(r["total_pnl"]),
            -float(r["max_chunk_dd"]),
        ),
        reverse=True,
    )
    lines = [
        "# L2 Depth Feature Sweep",
        "",
        f"Decision table: `{DEFAULT_PARQUET}`",
        f"Config: `{DEFAULT_CONFIG}`",
        "",
        "## Chunks",
        "",
    ]
    for chunk in chunks:
        lines.append(f"- {chunk.label}: {chunk.start} to {chunk.end}")
    lines += [
        "",
        "## Top Variants",
        "",
        "| rank | variant | total | worst | +chunks | maxDD | n | NO | TAIL |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(ranked[:15], start=1):
        lines.append(
            f"| {rank} | {row['variant']} | ${float(row['total_pnl']):+.2f} | "
            f"${float(row['worst_chunk_pnl']):+.2f} | {row['positive_chunks']}/4 | "
            f"{float(row['max_chunk_dd']):.2f}% | {row['total_n']} | "
            f"${float(row['no_pnl']):+.2f} | ${float(row['tail_pnl']):+.2f} |"
        )
    lines += ["", "## All Variants", ""]
    lines += [
        "| variant | total | A | B | C | D | worst | +chunks | maxDD | n | description |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in ranked:
        lines.append(
            f"| {row['variant']} | ${float(row['total_pnl']):+.2f} | "
            f"${float(row['A_pnl']):+.2f} | ${float(row['B_pnl']):+.2f} | "
            f"${float(row['C_pnl']):+.2f} | ${float(row['D_pnl']):+.2f} | "
            f"${float(row['worst_chunk_pnl']):+.2f} | {row['positive_chunks']}/4 | "
            f"{float(row['max_chunk_dd']):.2f}% | {row['total_n']} | "
            f"{row['description']} |"
        )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    config = json.loads(DEFAULT_CONFIG.read_text())
    df = pd.read_parquet(DEFAULT_PARQUET)
    records = df[record_columns(df)].to_dict("records")
    prices_by_ss = load_prices_by_slug_side(MARKET_DB)
    metrics_idx = load_metrics_by_slug(MARKET_DB)

    chunks = date_chunks(df["market_date"], 4)
    base_bets, _conflict_stats = build_bets_for_config(
        df=df,
        config=config,
        records=records,
        prices_by_ss=prices_by_ss,
        metrics_idx=metrics_idx,
    )
    bet_cache = {json.dumps(config, sort_keys=True): base_bets}

    # Recency variants (plan 2026-05-29-002 U3) need the p_Recency_h{hl} columns,
    # which only exist after a decision-table rebuild on the updated sweep_lib.
    # Self-disable them when absent so the sweep stays runnable on an older parquet.
    cols = set(df.columns)
    active_variants = []
    skipped_recency: list[str] = []
    for v in variants():
        if v.name.startswith("rec_"):
            hl = 15 if v.name.endswith("_h15") else 30
            if f"p_Recency_h{hl}" not in cols:
                skipped_recency.append(v.name)
                continue
        active_variants.append(v)
    if skipped_recency:
        print(json.dumps({
            "skipped_recency_variants": skipped_recency,
            "reason": "p_Recency_* columns absent; rebuild the decision table to enable",
        }))

    rows = []
    for v in active_variants:
        variant_config = copy.deepcopy(config)
        if v.config_mutator is not None:
            v.config_mutator(variant_config)
        config_key = json.dumps(variant_config, sort_keys=True)
        variant_bets = bet_cache.get(config_key)
        if variant_bets is None:
            variant_bets, _variant_conflict_stats = build_bets_for_config(
                df=df,
                config=variant_config,
                records=records,
                prices_by_ss=prices_by_ss,
                metrics_idx=metrics_idx,
            )
            bet_cache[config_key] = variant_bets
        rows.append(score_variant(
            variant=v,
            base_bets=variant_bets,
            df=df,
            records=records,
            chunks=chunks,
            config=variant_config,
            prices_by_ss=prices_by_ss,
            metrics_idx=metrics_idx,
        ))
    write_outputs(rows, chunks)
    print(json.dumps({
        "n_variants": len(rows),
        "best": max(rows, key=lambda r: (r["positive_chunks"], r["worst_chunk_pnl"], r["total_pnl"])),
        "out_csv": str(OUT_CSV),
        "out_md": str(OUT_MD),
    }))


if __name__ == "__main__":
    main()
