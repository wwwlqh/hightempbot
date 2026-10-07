"""Per-bracket strategy gates, sizing and ranking.

Every evaluated bracket emits a signal (pass or fail) so the dashboard can
show why it did or didn't bet.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, NamedTuple

import numpy as np

from hightempbot.calibration.lut import (
    CumulativeStats,
    bucket_of,
    lookup_with_cumulative,
)
from hightempbot.decision.brackets import (
    _to_celsius,
    bracket_label,
    bracket_probabilities,
)
from hightempbot.execution.strategy_constants import (
    ALLOWED_BRACKET_UNITS,
    LUT_MIN_N_FOR_SHRINKAGE,
    MAX_DAILY_NOTIONAL_FRAC,
    MAX_PER_MARKET,
    MIN_BET_USD,
    MIN_FILL_PRICE,
    POLY_FEE_THETA,
    RELIABILITY_CALIBRATION_ENABLED,
    STRATEGY_CONFIGS,
    tick_index_for,
    STRATEGY_NAMES,
    StrategyConfig,
    WU_CONSENSUS_BUFFER_C,
    WU_CONSENSUS_MODE,
)
from hightempbot.calibration.reliability import (
    ReliabilityProvider,
    group_for_unit,
)
from hightempbot.persistence.ledger import poly_fee_per_share as _poly_fee_per_share, slot_state
from hightempbot.ingestion.wu_forecast import fetch_wu_forecast
from hightempbot.execution.walker import (
    quantize_market_buy_size as _quantize_market_buy_size,
    walk_book_edge_preserving as _walk_book_edge_preserving,
)
from hightempbot.execution.types import BetSignal

if TYPE_CHECKING:
    from hightempbot.calibration.model import CalibrationModel
    from hightempbot.stations import StationConfig

logger = logging.getLogger(__name__)

# Slots already warned about, so repeated ticks log at DEBUG. Keys carry
# target_date at index 1; old dates are evicted once per UTC day.
_warned_legacy_slots: set[tuple[object, ...]] = set()
_warned_dust_slots: set[tuple[object, ...]] = set()
_warned_slots_evicted_on: str | None = None


def _evict_stale_warned_slots() -> None:
    """Drop entries whose target_date is older than today's UTC date. Once per day."""
    global _warned_slots_evicted_on
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _warned_slots_evicted_on == today:
        return
    for cache in (_warned_legacy_slots, _warned_dust_slots):
        cache.difference_update({k for k in cache if str(k[1]) < today})
    _warned_slots_evicted_on = today


def _execution_min_edge(cfg: StrategyConfig, fallback: float) -> float:
    return cfg.execution_min_edge if cfg.execution_min_edge is not None else fallback


def _execution_max_walk_price(cfg: StrategyConfig) -> float | None:
    # Strategies with a VWAP edge floor don't need the price leash.
    if cfg.execution_min_edge is not None:
        return None
    return cfg.max_walk_price


def _calibrate_no_claimed(
    reliability: ReliabilityProvider | None,
    bracket_unit: str,
    claimed_raw: float,
    no_price: float,
) -> float:
    """Reliability-calibrate P(NO wins); identity when no curve applies.

    ``no_price`` feeds the market-aware logit_blend curve (isotonic ignores it).
    """
    if not RELIABILITY_CALIBRATION_ENABLED or reliability is None:
        return claimed_raw
    group = group_for_unit(bracket_unit, side="NO")
    if group is None:
        return claimed_raw
    return reliability.apply(group, claimed_raw, no_price)


def _maybe_warn_legacy_slot_locked(
    *,
    station_id: str,
    target_date: str,
    threshold_val: float,
    side: str,
    bracket_low: float | None,
    strategy: str,
    slot_filled: float,
) -> None:
    """First fire WARNING + operator notify; later fires DEBUG (dedup)."""
    _evict_stale_warned_slots()
    slot_key = (station_id, target_date, threshold_val, side, bracket_low, strategy)
    if slot_key in _warned_legacy_slots:
        logger.debug(
            "legacy_slot_locked (dedup) station=%s date=%s threshold=%s side=%s "
            "bracket_low=%s strategy=%s filled=$%.2f",
            station_id, target_date, threshold_val, side,
            bracket_low, strategy, slot_filled,
        )
        return
    _warned_legacy_slots.add(slot_key)
    logger.warning(
        "legacy_slot_locked station=%s date=%s threshold=%s side=%s "
        "bracket_low=%s strategy=%s filled=$%.2f — skipping top-up "
        "(no entry_top_price in earliest row's event_detail)",
        station_id, target_date, threshold_val, side,
        bracket_low, strategy, slot_filled,
    )
    try:
        from hightempbot.scheduler.station_scanner import _notify
        _notify(
            title=f"Legacy slot locked: {station_id}",
            message=(
                f"station={station_id} date={target_date} threshold={threshold_val} "
                f"side={side} bracket_low={bracket_low} strategy={strategy} "
                f"filled=${slot_filled:.2f}"
            ),
            stage="legacy_slot_locked",
            station_id=station_id,
        )
    except Exception:
        logger.debug("notify failed", exc_info=True)


def _maybe_log_dust_skip(
    *,
    station_id: str,
    target_date: str,
    threshold_val: float,
    side: str,
    strategy: str,
    remaining: float,
    effective_min_bet: float,
    slot_filled: float,
) -> None:
    """First fire INFO; later fires DEBUG (dedup, per-process)."""
    _evict_stale_warned_slots()
    slot_key = (station_id, target_date, threshold_val, side, strategy)
    if slot_key in _warned_dust_slots:
        logger.debug(
            "dust_skip (dedup) station=%s date=%s threshold=%s side=%s strategy=%s "
            "remaining=$%.2f floor=$%.2f filled=$%.2f",
            station_id, target_date, threshold_val, side, strategy,
            remaining, effective_min_bet, slot_filled,
        )
        return
    _warned_dust_slots.add(slot_key)
    logger.info(
        "dust_skip station=%s date=%s threshold=%s side=%s strategy=%s "
        "remaining=$%.2f floor=$%.2f filled=$%.2f",
        station_id, target_date, threshold_val, side, strategy,
        remaining, effective_min_bet, slot_filled,
    )


def _ledger_event_types_for_mode(dry_run: bool) -> tuple[str, ...]:
    """Return ledger event types that should consume capacity in this mode."""
    return ("bet", "dry_run") if dry_run else ("bet",)


def _compute_target_usd(cfg: StrategyConfig, capital: float) -> float:
    return cfg.capital_frac * capital


def _gate_wu_consensus(
    bracket_low: float | None,
    bracket_high: float | None,
    bracket_unit: str,
    side: str,
    station_id: str,
    target_date: str,
    *,
    buffer_c: float = WU_CONSENSUS_BUFFER_C,
) -> tuple[bool | None, float | None]:
    """Check that WU's forecast agrees with the bet side.

    Returns ``(passed, wu_forecast_c)``; ``passed=None`` means WU was
    unavailable or the bracket is malformed (fail closed).
    """
    if bracket_low is None and bracket_high is None:
        return None, None
    if bracket_unit not in ("C", "F"):
        return None, None

    from datetime import date as _date_cls
    try:
        target_d = _date_cls.fromisoformat(target_date)
    except (TypeError, ValueError):
        return None, None

    wu_max_c = fetch_wu_forecast(station_id, target_d)
    if wu_max_c is None:
        return None, None

    lo_c = _to_celsius(bracket_low, bracket_unit)
    hi_c = _to_celsius(bracket_high, bracket_unit)

    lo_eff = (lo_c + buffer_c) if lo_c is not None else float("-inf")
    hi_eff = (hi_c - buffer_c) if hi_c is not None else float("inf")
    clearly_in = lo_eff <= wu_max_c < hi_eff
    out_low = lo_c is not None and wu_max_c < lo_c - buffer_c
    out_high = hi_c is not None and wu_max_c >= hi_c + buffer_c
    clearly_out = out_low or out_high

    if side == "YES":
        return clearly_in, wu_max_c
    if side == "NO":
        return clearly_out, wu_max_c
    return False, wu_max_c


def _compute_signal_flavors(
    p_emos: float,
    n_cum: int,
    hits_cum: int,
    lut_min_n: int = LUT_MIN_N_FOR_SHRINKAGE,
) -> dict[str, float]:
    """The 9 probability variants used by the strategies.

    Mirrors `backtest/lib/sweep_lib.py::add_signal_flavors`. Below ``lut_min_n``
    samples, p_L_strict is NaN and p_L_loose falls back to raw EMOS; the
    shrinkage variants stay finite thanks to their prior weight.
    """
    E = float(p_emos)
    n = float(max(n_cum, 0))
    hits = float(max(hits_cum, 0))

    L_obs: float = (hits / n) if n > 0 else float("nan")
    confident = n >= lut_min_n

    p_L_strict = L_obs if confident else float("nan")
    p_L_loose = L_obs if confident else E
    p_B_50 = 0.5 * E + 0.5 * p_L_loose
    p_B_30 = 0.3 * E + 0.7 * p_L_loose
    p_B_70 = 0.7 * E + 0.3 * p_L_loose
    p_Shrink_n10 = (hits + 10.0 * E) / (n + 10.0)
    p_Shrink_n50 = (hits + 50.0 * E) / (n + 50.0)

    # Ramp: lambda * L_safe + (1-lambda) * E where lambda = min(n/50, 1)
    lam = min(n / 50.0, 1.0)
    L_safe = E if math.isnan(L_obs) else L_obs
    p_Ramp = lam * L_safe + (1.0 - lam) * E

    return {
        "p_E": E,
        "p_L_strict": p_L_strict,
        "p_L_loose": p_L_loose,
        "p_B_50": p_B_50,
        "p_B_30": p_B_30,
        "p_B_70": p_B_70,
        "p_Shrink_n10": p_Shrink_n10,
        "p_Shrink_n50": p_Shrink_n50,
        "p_Ramp": p_Ramp,
    }


class BracketContext(NamedTuple):
    """Per-bracket inputs to ``_evaluate_strategy``."""
    bracket_idx: int
    mkt: dict
    threshold_val: float
    bracket_low: float | None
    bracket_high: float | None
    bracket_kind: str                          # "ceiling" | "floor" | "interior"
    bracket_label: str
    bracket_unit: str


class CalibrationContext(NamedTuple):
    """Per-bracket calibration outputs consumed by ``_evaluate_strategy``."""
    p_emos: float
    pred_bucket: tuple[float, float]
    flavors: dict[str, float]
    n_cum: int


@dataclass(frozen=True)
class _BranchResult:
    """Result of a per-strategy gate; ``partial_gates`` merges into gate_results."""
    edge_pass: bool
    edge: float
    prob_safe_floor: float
    signal_used: str
    signal_value: float
    edge_min_for_walker: float
    partial_gates: dict[str, bool | str | float | None]
    tail_components: dict[str, float] | None = None


def _cold_start_skip(
    *,
    signal_used: str,
    signal_value: float,
    prob_safe_floor: float,
    edge_min_for_walker: float,
    extra_partial: dict[str, bool | str | None] | None = None,
) -> _BranchResult:
    """SKIP result for a strategy whose LUT bucket has too few samples."""
    partial: dict[str, bool | str | None] = {
        "fp_band": True,
        "lut_min_n": False,
        "edge_gate": False,
        "bracket_extension": False,
    }
    if extra_partial:
        partial.update(extra_partial)
    return _BranchResult(
        edge_pass=False,
        edge=0.0,
        prob_safe_floor=prob_safe_floor,
        signal_used=signal_used,
        signal_value=signal_value,
        edge_min_for_walker=edge_min_for_walker,
        partial_gates=partial,
    )


class _NoGateDecision(NamedTuple):
    """Outcome of ``_evaluate_no_gate``: the winning path's values on a pass,
    the strict path's values on a fail."""
    edge_pass: bool
    edge: float
    claimed_raw: float
    claimed_calibrated: float
    signal_name: str
    signal_value: float
    extension_fired: bool


def _evaluate_no_gate(
    cfg: StrategyConfig,
    *,
    eval_price: float,
    flavors: dict[str, float],
    bracket_kind: str,
    reliability: ReliabilityProvider | None,
    bracket_unit: str,
) -> _NoGateDecision:
    """The NO gate, shared by NO and FLIP so FLIP fires exactly when NO would.

    Strict: ``edge = calibrated(1 - p_E) - eval_price - fee`` must be in
    ``[min_edge, max_edge]`` with ``eval_price >= fp_min``. If that misses on a
    ceiling bracket, retry with ``signal_name_for_ceiling`` and the relaxed
    ceiling bounds. Caller guarantees finite p_E and handles cold start.
    """
    p = flavors["p_E"]

    fee = _poly_fee_per_share(eval_price)
    claimed_raw = 1.0 - p
    claimed_calibrated = _calibrate_no_claimed(
        reliability, bracket_unit, claimed_raw, eval_price
    )
    edge = claimed_calibrated - eval_price - fee
    strict_pass = (
        eval_price >= cfg.fp_min
        and (cfg.min_edge is None or edge >= cfg.min_edge)
        and (cfg.max_edge is None or edge <= cfg.max_edge)
    )
    if strict_pass:
        return _NoGateDecision(
            edge_pass=True, edge=edge,
            claimed_raw=claimed_raw, claimed_calibrated=claimed_calibrated,
            signal_name="p_E", signal_value=p, extension_fired=False,
        )

    extension_applicable = (
        bracket_kind == "ceiling"
        and cfg.signal_name_for_ceiling is not None
        and cfg.fp_min_for_ceiling is not None
        and cfg.max_edge_for_ceiling is not None
    )
    if extension_applicable:
        p_ext = flavors.get(cfg.signal_name_for_ceiling, float("nan"))
        if not math.isnan(p_ext):
            ext_claimed_raw = 1.0 - p_ext
            ext_claimed_calibrated = _calibrate_no_claimed(
                reliability, bracket_unit, ext_claimed_raw, eval_price
            )
            edge_ext = ext_claimed_calibrated - eval_price - fee
            ext_pass = (
                eval_price >= cfg.fp_min_for_ceiling
                and (cfg.min_edge is None or edge_ext >= cfg.min_edge)
                and edge_ext <= cfg.max_edge_for_ceiling
            )
            if ext_pass:
                return _NoGateDecision(
                    edge_pass=True, edge=edge_ext,
                    claimed_raw=ext_claimed_raw,
                    claimed_calibrated=ext_claimed_calibrated,
                    signal_name=cfg.signal_name_for_ceiling,
                    signal_value=p_ext, extension_fired=True,
                )

    return _NoGateDecision(
        edge_pass=False, edge=edge,
        claimed_raw=claimed_raw, claimed_calibrated=claimed_calibrated,
        signal_name="p_E", signal_value=p, extension_fired=False,
    )


def _evaluate_no_branch(
    cfg: StrategyConfig,
    *,
    fill_price: float,
    flavors: dict[str, float],
    bracket_kind: str,
    cold_start: bool,
    n_cum: int,
    reliability: ReliabilityProvider | None = None,
    bracket_unit: str = "",
    no_price: float | None = None,
) -> _BranchResult | None:
    """NO: buy the NO token when ``_evaluate_no_gate`` passes. None if p_E is NaN."""
    del n_cum, no_price
    p = flavors["p_E"]
    if math.isnan(p):
        return None

    edge_min_for_walker = _execution_min_edge(
        cfg,
        cfg.min_edge if cfg.min_edge is not None else 0.0,
    )

    if cold_start:
        return _cold_start_skip(
            signal_used="p_E", signal_value=p,
            prob_safe_floor=1.0 - p,
            edge_min_for_walker=edge_min_for_walker,
        )

    decision = _evaluate_no_gate(
        cfg,
        eval_price=fill_price,
        flavors=flavors,
        bracket_kind=bracket_kind,
        reliability=reliability,
        bracket_unit=bracket_unit,
    )
    partial: dict[str, bool | str | float | None] = {
        "fp_band": True,
        "lut_min_n": True,
        "claimed_raw": decision.claimed_raw,
        "claimed_calibrated": decision.claimed_calibrated,
        "edge_gate": decision.edge_pass,
        "bracket_extension": decision.extension_fired,
    }
    return _BranchResult(
        edge_pass=decision.edge_pass, edge=decision.edge,
        prob_safe_floor=decision.claimed_calibrated,
        signal_used=decision.signal_name, signal_value=decision.signal_value,
        edge_min_for_walker=edge_min_for_walker, partial_gates=partial,
    )


def _evaluate_ymid_branch(
    cfg: StrategyConfig,
    *,
    fill_price: float,
    flavors: dict[str, float],
    bracket_kind: str,
    cold_start: bool,
    n_cum: int,
    reliability: ReliabilityProvider | None = None,
    bracket_unit: str = "",
    no_price: float | None = None,
) -> _BranchResult | None:
    """YMID: ratio gate p_Shrink_n50 >= alpha * fp, plus max-edge ceiling."""
    del bracket_kind, n_cum, reliability, bracket_unit, no_price
    p = flavors["p_Shrink_n50"]

    if math.isnan(p) or cold_start:
        p_safe = 0.0 if math.isnan(p) else p
        return _cold_start_skip(
            signal_used="p_Shrink_n50", signal_value=p_safe,
            prob_safe_floor=p_safe, edge_min_for_walker=0.0,
        )

    partial: dict[str, bool | str | None] = {"lut_min_n": True}
    ratio_pass = p >= (cfg.alpha_ratio or 0.0) * fill_price
    fee = _poly_fee_per_share(fill_price)
    edge = p - fill_price - fee
    max_edge_pass = (cfg.max_edge is None) or (edge <= cfg.max_edge)
    partial["fp_band"] = True
    partial["ratio_gate"] = ratio_pass
    partial["edge_ceiling"] = max_edge_pass
    edge_pass = ratio_pass and max_edge_pass
    partial["edge_gate"] = edge_pass
    return _BranchResult(
        edge_pass=edge_pass, edge=edge, prob_safe_floor=p,
        signal_used="p_Shrink_n50", signal_value=p,
        edge_min_for_walker=0.0,
        partial_gates=partial,
    )


def _evaluate_tail_branch(
    cfg: StrategyConfig,
    *,
    fill_price: float,
    flavors: dict[str, float],
    bracket_kind: str,
    cold_start: bool,
    n_cum: int,
    reliability: ReliabilityProvider | None = None,
    bracket_unit: str = "",
    no_price: float | None = None,
) -> _BranchResult | None:
    """TAIL: every signal in cfg.vote_signals must be ≥ alpha_ratio × price."""
    del bracket_kind, cold_start, reliability, bracket_unit, no_price
    partial: dict[str, bool | str | float | None] = {}

    min_n_required = cfg.vote_min_n or 0
    if n_cum < min_n_required:
        partial["fp_band"] = True
        partial["vote_min_n"] = False
        partial["edge_gate"] = False
        return _BranchResult(
            edge_pass=False, edge=0.0, prob_safe_floor=0.0,
            signal_used="tail_vote_avg", signal_value=0.0,
            edge_min_for_walker=_execution_min_edge(cfg, 0.0),
            partial_gates=partial,
        )

    partial["vote_min_n"] = True

    votes_pass = 0
    votes_total = 0
    component_values: dict[str, float] = {}
    for sig_name in (cfg.vote_signals or ()):
        v = flavors.get(sig_name)
        if v is None or math.isnan(v):
            continue
        votes_total += 1
        if v >= (cfg.alpha_ratio or 0.0) * fill_price:
            votes_pass += 1
        component_values[sig_name] = float(v)

    n_required = cfg.vote_n_required or 0
    edge_pass = votes_pass >= n_required and votes_total >= n_required
    partial["fp_band"] = True
    partial["vote_pass"] = votes_pass
    partial["vote_required"] = n_required
    partial["edge_gate"] = edge_pass

    if component_values:
        tail_avg = sum(component_values.values()) / len(component_values)
    else:
        tail_avg = float("nan")
    if math.isnan(tail_avg):
        return None

    fee = _poly_fee_per_share(fill_price)
    return _BranchResult(
        edge_pass=edge_pass, edge=tail_avg - fill_price - fee,
        prob_safe_floor=tail_avg,
        signal_used="tail_vote_avg", signal_value=tail_avg,
        edge_min_for_walker=_execution_min_edge(cfg, 0.0),
        partial_gates=partial, tail_components=component_values,
    )


def _evaluate_yhigh_branch(
    cfg: StrategyConfig,
    *,
    fill_price: float,
    flavors: dict[str, float],
    bracket_kind: str,
    cold_start: bool,
    n_cum: int,
    reliability: ReliabilityProvider | None = None,
    bracket_unit: str = "",
    no_price: float | None = None,
) -> _BranchResult | None:
    """YHIGH: ceiling-bracket-only YES bet, signal=p_B_50, additive edge band."""
    del n_cum, reliability, bracket_unit, no_price
    if bracket_kind != "ceiling":
        return None
    p = flavors["p_B_50"]
    if math.isnan(p):
        return None

    edge_min_for_walker = cfg.min_edge if cfg.min_edge is not None else 0.0

    if cold_start:
        return _cold_start_skip(
            signal_used="p_B_50", signal_value=p,
            prob_safe_floor=p, edge_min_for_walker=edge_min_for_walker,
        )

    partial: dict[str, bool | str | None] = {"fp_band": True, "lut_min_n": True}
    fee = _poly_fee_per_share(fill_price)
    edge = p - fill_price - fee
    edge_pass = (cfg.min_edge is None or edge >= cfg.min_edge) and (
        cfg.max_edge is None or edge <= cfg.max_edge
    )
    partial["edge_gate"] = edge_pass
    return _BranchResult(
        edge_pass=edge_pass, edge=edge, prob_safe_floor=p,
        signal_used="p_B_50", signal_value=p,
        edge_min_for_walker=edge_min_for_walker, partial_gates=partial,
    )


def _evaluate_flip_branch(
    cfg: StrategyConfig,
    *,
    fill_price: float,
    flavors: dict[str, float],
    bracket_kind: str,
    cold_start: bool,
    n_cum: int,
    reliability: ReliabilityProvider | None = None,
    bracket_unit: str = "",
    no_price: float | None = None,
) -> _BranchResult | None:
    """FLIP: buy YES wherever the NO gate (with NO's config and price) passes.

    The walker floor is -1.0, so the only fill bound is max_walk_price. The
    recorded edge is the NO-side edge, not the YES trade's (negative) EV.
    """
    del cfg, n_cum
    no_cfg = STRATEGY_CONFIGS["NO"]
    p = flavors["p_E"]
    if math.isnan(p):
        return None
    if no_price is None or not (0.0 < no_price < 1.0):
        return None

    if cold_start:
        return _cold_start_skip(
            signal_used="p_E_flip", signal_value=p,
            prob_safe_floor=p,
            edge_min_for_walker=-1.0,
        )

    decision = _evaluate_no_gate(
        no_cfg,
        eval_price=no_price,
        flavors=flavors,
        bracket_kind=bracket_kind,
        reliability=reliability,
        bracket_unit=bracket_unit,
    )
    partial: dict[str, bool | str | float | None] = {
        "fp_band": True,
        "lut_min_n": True,
        "claimed_raw": decision.claimed_raw,
        "claimed_calibrated": decision.claimed_calibrated,
    }
    if decision.extension_fired:
        partial["bracket_extension"] = True
    partial["edge_gate"] = decision.edge_pass
    return _BranchResult(
        edge_pass=decision.edge_pass, edge=decision.edge,
        prob_safe_floor=1.0 - decision.claimed_calibrated,
        signal_used=f"{decision.signal_name}_flip",
        signal_value=decision.signal_value,
        edge_min_for_walker=-1.0, partial_gates=partial,
    )


_StrategyBranchFn = Callable[..., "_BranchResult | None"]
_STRATEGY_BRANCHES: dict[str, _StrategyBranchFn] = {
    "NO": _evaluate_no_branch,
    "YMID": _evaluate_ymid_branch,
    "TAIL": _evaluate_tail_branch,
    "YHIGH": _evaluate_yhigh_branch,
    "FLIP": _evaluate_flip_branch,
}


def _evaluate_strategy(
    cfg: StrategyConfig,
    strategy_name: str,
    *,
    station_id: str,
    target_date: str,
    horizon: int,
    bracket: BracketContext,
    calibration: CalibrationContext,
    capital: float,
    local_now_hour: int | None,
    conn: sqlite3.Connection,
    ledger_event_types: tuple[str, ...],
    local_now_minute: int | None = None,
    station_max_yes_ask: float | None = None,
    reliability: ReliabilityProvider | None = None,
) -> BetSignal | None:
    """Evaluate one strategy on one bracket.

    Returns None when the strategy doesn't apply (wrong hour, price outside
    its band); otherwise a signal, passing or not. Gate order: hour →
    consensus skip → price band → first tick of hour → strategy edge gate →
    volume → delayed entry → slot idempotency → sizing and book walk.
    """
    bi = bracket.bracket_idx
    mkt = bracket.mkt
    threshold_val = bracket.threshold_val
    bracket_low = bracket.bracket_low
    bracket_high = bracket.bracket_high
    bracket_kind = bracket.bracket_kind
    bracket_label_str = bracket.bracket_label
    bracket_unit = bracket.bracket_unit

    p_emos = calibration.p_emos
    pred_bucket = calibration.pred_bucket
    flavors = calibration.flavors
    n_cum = calibration.n_cum

    if local_now_hour is not None and local_now_hour not in cfg.entry_hour_set:
        return None

    # Skip when the market already has a strong favorite.
    if (
        cfg.consensus_skip_threshold is not None
        and station_max_yes_ask is not None
        and station_max_yes_ask >= cfg.consensus_skip_threshold
    ):
        return None

    if cfg.side == "NO":
        fp_raw = mkt.get("best_bid")
        token_id = mkt.get("no_token_id") or ""
    else:
        fp_raw = mkt.get("best_ask")
        token_id = mkt.get("token_id") or ""

    if fp_raw is None or fp_raw <= 0:
        return None
    fill_price = float(fp_raw)
    entry_top_now = fill_price

    # FLIP gates on the NO price even though it fills YES.
    no_price_raw = mkt.get("best_bid")
    no_price_for_branch: float | None = (
        float(no_price_raw) if no_price_raw is not None and no_price_raw > 0 else None
    )

    # NO's ceiling extension allows a lower fill price on ceiling brackets.
    effective_fp_min = cfg.fp_min
    if (
        strategy_name == "NO"
        and bracket_kind == "ceiling"
        and cfg.fp_min_for_ceiling is not None
        and cfg.fp_min_for_ceiling < effective_fp_min
    ):
        effective_fp_min = cfg.fp_min_for_ceiling
    if not (effective_fp_min <= fill_price <= cfg.fp_max):
        return None

    if fill_price < MIN_FILL_PRICE:
        return None

    # Prior exposure on this slot plus its first-fill anchor. slot_filled == 0
    # is a first fill; > 0 is a top-up. A top-up with no stored anchor is a
    # pre-top-up legacy slot and is skipped. Safe without a lock because
    # APScheduler runs each station's tick with max_instances=1.
    slot_filled, slot_anchor_db, slot_first_fill_vwap = slot_state(
        conn,
        station_id=station_id,
        target_date=target_date,
        threshold_val=threshold_val,
        side=cfg.side,
        bracket_low_val=bracket_low,
        strategy=strategy_name,
        ledger_event_types=ledger_event_types,
    )

    # New slots open only on the hour's first tick (matching the backtest's
    # one snapshot per hour); top-ups run every tick.
    if local_now_minute is not None:
        if tick_index_for(station_id, local_now_minute) >= 1 and slot_filled <= 0:
            return None

    legacy_slot_locked = False
    if slot_filled > 0:
        if slot_anchor_db is None:
            legacy_slot_locked = True
            _maybe_warn_legacy_slot_locked(
                station_id=station_id,
                target_date=target_date,
                threshold_val=threshold_val,
                side=cfg.side,
                bracket_low=bracket_low,
                strategy=strategy_name,
                slot_filled=slot_filled,
            )

    if slot_filled > 0 and slot_anchor_db is not None:
        entry_top_price = slot_anchor_db
    else:
        entry_top_price = entry_top_now

    volume = mkt.get("volume24hr")
    market_id = mkt.get("market_id", "")

    gate_results: dict[str, bool | str | float | None] = {
        "strategy": strategy_name,
        "bracket_extension": False,
    }

    unit_allowed = bracket_unit in ALLOWED_BRACKET_UNITS
    gate_results["unit_allowed"] = unit_allowed

    # Shrinkage signals stay finite with no history, so gate on sample count.
    cold_start = n_cum < LUT_MIN_N_FOR_SHRINKAGE

    branch_fn = _STRATEGY_BRANCHES.get(strategy_name)
    if branch_fn is None:
        return None
    branch_result = branch_fn(
        cfg,
        fill_price=fill_price,
        flavors=flavors,
        bracket_kind=bracket_kind,
        cold_start=cold_start,
        n_cum=n_cum,
        reliability=reliability,
        bracket_unit=bracket_unit,
        no_price=no_price_for_branch,
    )
    if branch_result is None:
        return None
    edge_pass = branch_result.edge_pass
    edge = branch_result.edge
    prob_safe_floor = branch_result.prob_safe_floor
    signal_used = branch_result.signal_used
    signal_value = branch_result.signal_value
    edge_min_for_walker = branch_result.edge_min_for_walker
    tail_components = branch_result.tail_components
    gate_results.update(branch_result.partial_gates)

    volume_pass = volume is not None and volume >= cfg.min_bvol
    gate_results["volume"] = volume_pass if edge_pass else None

    delayed_entry_pass = True
    if cfg.delayed_entry_fp_max is not None:
        if edge_pass and volume_pass:
            delayed_entry_pass = fill_price <= cfg.delayed_entry_fp_max
            gate_results["delayed_entry"] = delayed_entry_pass
        else:
            delayed_entry_pass = False
            gate_results["delayed_entry"] = None

    # The slot takes more fills until it reaches its target. Top-ups below 1%
    # of target (or MIN_BET_USD) are dust and skipped.
    target_usd_for_floor = _compute_target_usd(cfg, capital)
    effective_min_bet = max(MIN_BET_USD, 0.01 * target_usd_for_floor)
    if edge_pass and volume_pass and delayed_entry_pass and not legacy_slot_locked:
        remaining_for_idem = max(0.0, target_usd_for_floor - slot_filled)
        gate_results["idempotency"] = remaining_for_idem >= effective_min_bet
        if (
            gate_results["idempotency"] is False
            and slot_filled > 0
            and 0 < remaining_for_idem < effective_min_bet
        ):
            _maybe_log_dust_skip(
                station_id=station_id,
                target_date=target_date,
                threshold_val=threshold_val,
                side=cfg.side,
                strategy=strategy_name,
                remaining=remaining_for_idem,
                effective_min_bet=effective_min_bet,
                slot_filled=slot_filled,
            )
    elif legacy_slot_locked:
        gate_results["idempotency"] = False
        gate_results["legacy_slot_locked"] = True
    else:
        gate_results["idempotency"] = None

    proceed_to_size = (
        edge_pass
        and volume_pass
        and delayed_entry_pass
        and unit_allowed
        and gate_results.get("idempotency") is True
    )

    bet_size_usd = 0.0
    limit_price = 0.0
    entry_fill_vwap_val: float | None = None
    slip_anchor_for_signal: float | None = None

    if proceed_to_size:
        target_usd = _compute_target_usd(cfg, capital)
        remaining = max(0.0, target_usd - slot_filled)
        if target_usd < MIN_BET_USD:
            gate_results["insufficient_capital"] = False
            proceed_to_size = False
        elif target_usd > capital:
            gate_results["insufficient_capital"] = False
            proceed_to_size = False
        elif remaining < MIN_BET_USD:
            gate_results["insufficient_capital"] = False
            proceed_to_size = False
        else:
            book_key = "_yes_book" if cfg.side == "YES" else "_no_book"
            book = mkt.get(book_key)
            if not book:
                gate_results["insufficient_depth"] = False
                proceed_to_size = False
                logger.info(
                    "walk_book missing for %s/%s %s side=%s, skipping bet",
                    station_id, strategy_name, bracket_label_str, cfg.side,
                )
            else:
                # A slip-capped top-up walks against first-fill VWAP + cap and
                # carries that anchor to order time. Everything else uses the
                # strategy's default leash.
                if (
                    cfg.max_vwap_slip_from_anchor is not None
                    and slot_filled > 0
                    and slot_first_fill_vwap is not None
                ):
                    walk_max_price = cfg.max_vwap_slip_from_anchor
                    walk_anchor = slot_first_fill_vwap
                    slip_anchor_for_signal = slot_first_fill_vwap
                else:
                    walk_max_price = _execution_max_walk_price(cfg)
                    walk_anchor = entry_top_price
                walked = _walk_book_edge_preserving(
                    book,
                    remaining,
                    prob_safe_floor=prob_safe_floor,
                    fee_theta=POLY_FEE_THETA,
                    min_edge=edge_min_for_walker,
                    min_bet_usd=MIN_BET_USD,
                    max_walk_price=walk_max_price,
                    walk_anchor_price=walk_anchor,
                )
                if walked is None:
                    gate_results["insufficient_depth"] = False
                    proceed_to_size = False
                    logger.info(
                        "insufficient depth for %s/%s %s (target $%.2f, slot_filled $%.2f)",
                        station_id, strategy_name, bracket_label_str,
                        remaining, slot_filled,
                    )
                else:
                    filled_usd, filled_shares, filled_vwap, walked_limit, walked_edge = walked
                    entry_fill_vwap_val = filled_vwap
                    # Re-check the edge ceiling on the walked edge (defensive:
                    # walking only lowers edge today).
                    if strategy_name == "NO":
                        no_ceiling = (
                            cfg.max_edge_for_ceiling
                            if gate_results.get("bracket_extension") is True
                            and cfg.max_edge_for_ceiling is not None
                            else cfg.max_edge
                        )
                        if no_ceiling is not None and walked_edge > no_ceiling:
                            gate_results["edge_ceiling"] = False
                            proceed_to_size = False
                    elif strategy_name in ("YMID", "YHIGH"):
                        if cfg.max_edge is not None and walked_edge > cfg.max_edge:
                            gate_results["edge_ceiling"] = False
                            proceed_to_size = False
                    if proceed_to_size:
                        bet_size_usd = filled_usd
                        limit_price = walked_limit
                        if bet_size_usd < MIN_BET_USD:
                            gate_results["insufficient_depth"] = False
                            proceed_to_size = False
                            bet_size_usd = 0.0
                        elif _quantize_market_buy_size(
                            bet_size_usd,
                            limit_price,
                            min_bet_usd=MIN_BET_USD,
                        ) is None:
                            gate_results["insufficient_size"] = False
                            proceed_to_size = False
                            bet_size_usd = 0.0
                        elif bet_size_usd < effective_min_bet:
                            gate_results["insufficient_size"] = False
                            proceed_to_size = False
                            bet_size_usd = 0.0

    passed = proceed_to_size

    sig = BetSignal(
        station_id=station_id,
        target_date=target_date,
        horizon=horizon,
        bracket_idx=bi,
        threshold=threshold_val,
        bracket_label=f"{bracket_label_str} {cfg.side} [{strategy_name}]",
        side=cfg.side,
        p_model=signal_value,
        p_market=fill_price,
        edge=edge,
        bet_size_usd=bet_size_usd,
        fill_price=fill_price,
        volume_usd=volume,
        market_id=market_id,
        token_id=token_id,
        limit_price=limit_price,
        bracket_low=bracket_low,
        bracket_high=bracket_high,
        bracket_unit=bracket_unit,
        prob_safe_floor=prob_safe_floor,
        pred_bucket=pred_bucket,
        n_bucket=n_cum,
        gate_results=gate_results,
        passed_all_gates=passed,
        strategy=strategy_name,
        signal_used=signal_used,
        signal_value=signal_value,
        entry_top_price=entry_top_price,
        slot_filled_pre=slot_filled,
        entry_fill_vwap=entry_fill_vwap_val,
        slip_anchor_vwap=slip_anchor_for_signal,
    )

    # record_bet persists these as event_detail.tail_votes.
    if tail_components is not None:
        sig.gate_results["_tail_votes"] = tail_components

    return sig


def _extract_polymarket_brackets(
    market_data: dict,
) -> tuple[list[tuple[str, float | None, float | None]], list[int]]:
    """Return parseable Polymarket bracket bounds plus their market keys."""
    poly_brackets: list[tuple[str, float | None, float | None]] = []
    poly_market_order: list[int] = []

    for bi, mkt in sorted(market_data.items()):
        blo = mkt.get("bracket_low")
        bhi = mkt.get("bracket_high")
        if blo is None and bhi is not None:
            poly_brackets.append(("floor", None, bhi))
        elif bhi is None and blo is not None:
            poly_brackets.append(("ceiling", blo, None))
        elif blo is not None and bhi is not None:
            poly_brackets.append(("interior", blo, bhi))
        else:
            continue
        poly_market_order.append(bi)

    return poly_brackets, poly_market_order


def evaluate_station(
    conn: sqlite3.Connection,
    station: StationConfig,
    model: CalibrationModel,
    ensemble_members: np.ndarray,
    market_data: dict,
    capital: float,
    target_date: str,
    horizon: int,
    dry_run: bool = False,
    local_now_hour: int | None = None,
    local_now_minute: int | None = None,
) -> list[BetSignal]:
    """Run every enabled strategy over every Polymarket bracket of a station.

    ``market_data`` maps bracket_idx to {token_id, market_id, best_bid,
    best_ask, volume24hr, _yes_book, _no_book}. ``local_now_hour`` /
    ``local_now_minute`` of None skip the hour gates (tests). Returns all
    signals, passing and failing.
    """
    station_id = station.icao
    signals: list[BetSignal] = []

    poly_brackets, poly_market_order = _extract_polymarket_brackets(market_data)

    if not poly_brackets:
        logger.info("Station %s: no parseable Polymarket brackets, skipping", station_id)
        return signals

    # Take the unit from the market labels, not station.unit (they can
    # differ). No unit marker means fail closed.
    bracket_unit = ""
    for bi in poly_market_order:
        lbl = market_data.get(bi, {}).get("bracket_label") or ""
        if "°F" in lbl:
            bracket_unit = "F"
            break
        if "°C" in lbl:
            bracket_unit = "C"
            break

    if bracket_unit not in ("C", "F"):
        logger.warning(
            "Station %s: no parseable °C/°F unit in any Polymarket bracket label; "
            "failing closed to avoid wrong-unit comparison",
            station_id,
        )
        return signals

    probs = bracket_probabilities(model, ensemble_members, poly_brackets, bracket_unit, icao=station_id)
    if not probs or len(probs) != len(poly_brackets):
        logger.info("Station %s: bracket_probabilities failed closed, skipping", station_id)
        return signals

    ledger_event_types = _ledger_event_types_for_mode(dry_run)
    ledger_event_placeholders = ",".join("?" for _ in ledger_event_types)

    existing_bets = conn.execute(
        f"""SELECT COUNT(*) as cnt FROM ledger
            WHERE station_id = ? AND target_date = ?
            AND event_type IN ({ledger_event_placeholders})
            AND outcome != 'CANCELLED'""",
        (station_id, target_date, *ledger_event_types),
    ).fetchone()["cnt"]
    remaining_slots = max(0, MAX_PER_MARKET - existing_bets)

    # Highest YES ask across the market, for consensus_skip_threshold.
    station_max_yes_ask: float | None = None
    for bi in poly_market_order:
        ask = market_data.get(bi, {}).get("best_ask")
        if ask is None:
            continue
        ask_f = float(ask)
        if not (ask_f == ask_f):  # NaN guard
            continue
        if station_max_yes_ask is None or ask_f > station_max_yes_ask:
            station_max_yes_ask = ask_f

    # Identity when no fresh, well-sampled curve exists.
    reliability = ReliabilityProvider.load(conn)

    candidates: list[BetSignal] = []
    _lut_cum_cache: dict[tuple[float, float], CumulativeStats] = {}

    for idx, (btype, blo, bhi) in enumerate(poly_brackets):
        bi = poly_market_order[idx]
        mkt = market_data[bi]

        p_model = probs[idx]
        label = mkt.get("bracket_label") or bracket_label(station, (btype, blo, bhi))

        # Upper bound for floor/interior brackets, lower bound for ceiling.
        if btype == "ceiling":
            threshold_val = float(blo) if blo is not None else 0.0
        else:
            threshold_val = float(bhi) if bhi is not None else 0.0

        # EMOS not ready: emit a diagnostic signal and skip.
        if not (p_model == p_model and 0.0 <= p_model <= 1.0):
            sig = BetSignal(
                station_id=station_id,
                target_date=target_date,
                horizon=horizon,
                bracket_idx=bi,
                threshold=threshold_val,
                bracket_label=f"{label} [no-emos]",
                side="",
                p_model=0.0,
                p_market=0.0,
                edge=0.0,
                bet_size_usd=0.0,
                fill_price=0.0,
                volume_usd=mkt.get("volume24hr"),
                market_id=mkt.get("market_id", ""),
                token_id="",
                limit_price=0.0,
                bracket_low=mkt.get("bracket_low"),
                bracket_high=mkt.get("bracket_high"),
                bracket_unit=bracket_unit,
                prob_safe_floor=None,
                pred_bucket=None,
                n_bucket=0,
                gate_results={"lut": False, "reason": "emos_not_ready"},
                passed_all_gates=False,
            )
            signals.append(sig)
            continue

        pred_bucket = bucket_of(p_model)
        cum = _lut_cum_cache.get(pred_bucket)
        if cum is None:
            cum = lookup_with_cumulative(
                conn, station_id, pred_bucket, target_date,
            )
            _lut_cum_cache[pred_bucket] = cum
        flavors = _compute_signal_flavors(
            p_model, cum.n_cum, cum.hits_cum, LUT_MIN_N_FOR_SHRINKAGE,
        )

        bracket_ctx = BracketContext(
            bracket_idx=bi,
            mkt=mkt,
            threshold_val=threshold_val,
            bracket_low=mkt.get("bracket_low"),
            bracket_high=mkt.get("bracket_high"),
            bracket_kind=btype,
            bracket_label=label,
            bracket_unit=bracket_unit,
        )
        calibration_ctx = CalibrationContext(
            p_emos=p_model,
            pred_bucket=pred_bucket,
            flavors=flavors,
            n_cum=cum.n_cum,
        )

        for strategy_name in STRATEGY_NAMES:
            cfg = STRATEGY_CONFIGS[strategy_name]
            if not cfg.enabled:
                continue
            sig = _evaluate_strategy(
                cfg,
                strategy_name,
                station_id=station_id,
                target_date=target_date,
                horizon=horizon,
                bracket=bracket_ctx,
                calibration=calibration_ctx,
                capital=capital,
                local_now_hour=local_now_hour,
                local_now_minute=local_now_minute,
                conn=conn,
                ledger_event_types=ledger_event_types,
                station_max_yes_ask=station_max_yes_ask,
                reliability=reliability,
            )
            if sig is None:
                continue
            signals.append(sig)
            if sig.passed_all_gates:
                candidates.append(sig)

    if WU_CONSENSUS_MODE in ("SHADOW", "BLOCK"):
        for sig in candidates:
            wu_passed, wu_value = _gate_wu_consensus(
                sig.bracket_low, sig.bracket_high, sig.bracket_unit,
                sig.side, sig.station_id, sig.target_date,
            )
            sig.wu_forecast_c = wu_value
            sig.wu_consensus_verdict = wu_passed
            if WU_CONSENSUS_MODE == "BLOCK":
                sig.gate_results["wu_consensus"] = wu_passed if wu_passed is True else False
                if sig.gate_results["wu_consensus"] is False:
                    sig.passed_all_gates = False

    candidates = [s for s in candidates if s.passed_all_gates]
    candidates.sort(key=lambda s: s.edge, reverse=True)
    top_candidates: set[int] = set()
    for c in candidates[:remaining_slots]:
        top_candidates.add(id(c))

    for sig in signals:
        if sig.passed_all_gates and id(sig) not in top_candidates:
            sig.passed_all_gates = False
            sig.gate_results["max_per_market"] = False

    return signals


def rank_signals(
    conn: sqlite3.Connection,
    signals: list[BetSignal],
    capital: float,
    dry_run: bool = False,
) -> list[BetSignal]:
    """Keep passing signals best-edge first while they fit the per-target-date
    budget ``MAX_DAILY_NOTIONAL_FRAC × capital − already staked``."""
    passing = [s for s in signals if s.passed_all_gates]
    passing.sort(key=lambda s: s.edge, reverse=True)

    ledger_event_types = _ledger_event_types_for_mode(dry_run)
    ledger_event_placeholders = ",".join("?" for _ in ledger_event_types)
    available_by_target_date: dict[str, float] = {}

    def _available_for(target_date: str) -> float:
        if target_date not in available_by_target_date:
            row = conn.execute(
                f"""SELECT COALESCE(SUM(bet_size), 0) AS used FROM ledger
                    WHERE target_date = ?
                      AND event_type IN ({ledger_event_placeholders})
                      AND outcome != 'CANCELLED'""",
                (target_date, *ledger_event_types),
            ).fetchone()
            used = float(row["used"] or 0.0)
            available_by_target_date[target_date] = max(
                0.0,
                MAX_DAILY_NOTIONAL_FRAC * capital - used,
            )
        return available_by_target_date[target_date]

    result: list[BetSignal] = []
    for sig in passing:
        available = _available_for(sig.target_date)
        if sig.bet_size_usd <= available + 1e-9:
            result.append(sig)
            available_by_target_date[sig.target_date] = available - sig.bet_size_usd
        else:
            sig.passed_all_gates = False
            sig.gate_results["daily_notional"] = False

    return result
