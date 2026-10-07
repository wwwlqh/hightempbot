"""Bet decision pipeline — gate checks, fixed sizing, candidate ranking.

Extracts and extends the logic from backtest/engine.py lines 207-328.
All signals (pass and fail) are emitted so the dashboard can show gate status.
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

# First-occurrence trackers for noisy per-tick log lines. Each slot key fires
# WARNING/INFO once per process lifetime; subsequent ticks for the same slot
# log at DEBUG so a stuck condition stops flooding the log + Telegram. Sets
# reset on process restart, which is acceptable.
#
# Slot keys carry ``target_date`` (index 1). Stale entries from prior dates are
# evicted once per UTC day to keep memory bounded — long-running bots otherwise
# accumulate one set entry per distinct slot ever seen.
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
    # NO/TAIL now use the realized VWAP edge floor as the execution boundary;
    # the legacy 5-cent leash stays for strategies without that explicit floor.
    if cfg.execution_min_edge is not None:
        return None
    return cfg.max_walk_price


def _calibrate_no_claimed(
    reliability: ReliabilityProvider | None,
    bracket_unit: str,
    claimed_raw: float,
    no_price: float,
) -> float:
    """Reliability-calibrate a NO claimed probability before the edge gate.

    Returns ``claimed_raw`` unchanged when calibration is globally disabled,
    when no provider is supplied, or when the bracket unit has no group
    (identity fallback). When a valid per-group curve is loaded, returns the
    calibrated value. The result is the single number that must flow to the
    edge computation, ``prob_safe_floor``, and the ledger (one consistent
    value everywhere the raw one used to flow).

    ``no_price`` is the market NO fill price at entry. It is required by the
    market-aware ``logit_blend`` curve (which estimates ``P(NO wins | claim,
    market price)``) and ignored by the legacy 1D isotonic curve; passing it
    always is harmless for isotonic and load-bearing for the blend.
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
    # Operator alert: import lazily + best-effort so a notify failure never
    # blocks the bet decision path. station_scanner._notify already owns its
    # own try/except, but we wrap again here in case the import itself fails.
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
    """Per-strategy target stake. The pipeline halts at MAX_DD before
    ``evaluate_station`` fires (2026-05-20 halt-on-DD switch), so this
    function is the sole sizing dial: ``cfg.capital_frac * capital``.
    """
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
    """Final consensus check: WU's fresh forecast must agree with bet side.

    Returns ``(passed, wu_forecast_c)``. ``passed=None`` means "WU unavailable"
    and is treated as fail-closed by ``passed_all_gates`` (None values are
    excluded from the all-True check, so the gate stays unset → bet skipped).
    Buffer of ``buffer_c`` °C absorbs WU reporting precision and minor
    between-cycle revision drift.
    """
    # Both-None bounds are degenerate: with lo=-inf and hi=+inf the gate
    # would trivially accept YES against any forecast and trivially reject NO
    # against any forecast. Real Polymarket brackets always have at least one
    # finite edge; both-None means upstream parsing failed. Fail-closed.
    if bracket_low is None and bracket_high is None:
        return None, None
    # Empty bracket_unit is the sentinel for "unknown / not yet inferred". Do
    # NOT guess: fail closed so a missing bracket_unit cannot silently compare
    # °F bounds against °C forecast (~30°C error).
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

    # Convert °F bounds (US stations) to °C so the comparison is uniform.
    # ``bracket_unit`` is validated to be "C"/"F" above, matching
    # ``_to_celsius``'s unit-marker contract (None bounds pass through as None).
    lo_c = _to_celsius(bracket_low, bracket_unit)
    hi_c = _to_celsius(bracket_high, bracket_unit)

    # Tail-bracket math: only the finite edge participates in the buffer.
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
    """Compute the 9 probability variants used by the per-strategy router.

    Mirrors `backtest/sweep_lib.py::add_signal_flavors` (lines 432-455) byte-for-byte.
    Returns NaN for L_strict / L_loose when n_cum < lut_min_n (cold-start guard).
    Bayesian shrinkage variants always produce a finite value as long as p_emos
    is finite — the prior weight (10 or 50) keeps the denominator non-zero.

    Inputs:
        p_emos:    raw EMOS bracket probability (the "E" in sweep_lib).
        n_cum:     walk-forward strict-less-than cumulative sample count for the bucket.
        hits_cum:  walk-forward strict-less-than cumulative hit count for the bucket.
        lut_min_n: confidence threshold for L_strict / L_loose (default 30).

    Returns dict with keys: p_E, p_L_strict, p_L_loose, p_B_50, p_B_30, p_B_70,
    p_Shrink_n10, p_Shrink_n50, p_Ramp.
    """
    E = float(p_emos)
    n = float(max(n_cum, 0))
    hits = float(max(hits_cum, 0))

    # Empirical observed rate (NaN when n=0)
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
    """Per-bracket inputs to ``_evaluate_strategy``.

    Bundles the 8 fields that describe a single Polymarket bracket so the
    evaluator's signature stays under control.
    """
    bracket_idx: int
    mkt: dict
    threshold_val: float
    bracket_low: float | None
    bracket_high: float | None
    bracket_kind: str                          # "ceiling" | "floor" | "interior"
    bracket_label: str
    bracket_unit: str


class CalibrationContext(NamedTuple):
    """Per-bracket calibration outputs consumed by ``_evaluate_strategy``.

    All four fields are produced upstream by ``bracket_probabilities`` +
    ``_compute_signal_flavors`` for the same EMOS-probability bucket; the
    evaluator reads them as a coherent unit.
    """
    p_emos: float
    pred_bucket: tuple[float, float]
    flavors: dict[str, float]
    n_cum: int


@dataclass(frozen=True)
class _BranchResult:
    """Uniform return shape for per-strategy gate evaluators.

    ``partial_gates`` holds the keys this branch decided (``fp_band``,
    ``lut_min_n``, ``edge_gate``, ``bracket_extension``, ``ratio_gate``,
    ``edge_ceiling``, ``vote_*``); the caller merges them into the full
    gate_results dict. ``tail_components`` is TAIL-only diagnostic data.
    """
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
    """Build the SKIP _BranchResult shared by NO/YMID/YHIGH cold-start arms.

    Sets ``fp_band=True``, ``lut_min_n=False``, ``edge_gate=False`` and
    ``bracket_extension=False`` so the dashboard records the rejection
    with a consistent gate footprint regardless of which strategy fired.
    """
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
    """Outcome of the shared calibrated NO gate (``_evaluate_no_gate``).

    ``edge``, ``claimed_raw``, ``claimed_calibrated``, ``signal_name`` and
    ``signal_value`` always describe the path that decided the outcome: the
    winning path on a pass (strict or ceiling extension), the strict path on
    a fail (a failed extension attempt is discarded — the strict numbers stay
    the audit values, matching the pre-extraction behavior of both consumers).
    """
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
    """The champion NO gate: strict calibrated edge band + ceiling extension.

    Single source of truth shared by the NO sleeve and the FLIP mirror sleeve
    (operator invariant 2026-08-09: FLIP fires iff NO would fire). Both
    branches MUST route their gate decision through this function — the two
    hand-copied implementations it replaced are exactly the drift risk the
    invariant forbids. ``eval_price`` is the NO book price the gate is judged
    against; the caller's own fill side may differ (FLIP fills YES) but the
    gate math must not.

    Preconditions owned by the caller: ``flavors["p_E"]`` is finite,
    ``eval_price`` is a valid in-band price, and the cold-start guard has
    already been handled.

    Strict path: fee at ``eval_price``; claimed ``P(NO wins) = 1 - p_E`` is
    reliability-calibrated per bracket-unit group BEFORE the edge/fee
    computation (2026-07-16 over-confidence fix); ``edge = calibrated -
    eval_price - fee``; pass iff ``eval_price >= cfg.fp_min`` and edge in
    ``[cfg.min_edge, cfg.max_edge]`` (a None bound skips that side).

    Ceiling extension: tried only when the strict path missed, the bracket is
    a ceiling, and all three ceiling overrides are configured. Re-derives the
    claim from ``cfg.signal_name_for_ceiling`` (NaN → no extension) and
    passes iff ``eval_price >= cfg.fp_min_for_ceiling`` and the extension
    edge is in ``[cfg.min_edge, cfg.max_edge_for_ceiling]``.
    """
    p = flavors["p_E"]

    # --- Strict NO gate (always tried first) ---
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

    # --- Ceiling-bracket extension (only if strict missed and bracket is ceiling) ---
    # Guarded happy path: the extension fires only when it is applicable, the
    # extension signal is finite, and the relaxed gate passes. Every other
    # outcome (not applicable, NaN signal, extension gate missed) falls through
    # to the single shared fail-return below with the strict-gate audit values.
    extension_applicable = (
        bracket_kind == "ceiling"
        and cfg.signal_name_for_ceiling is not None
        and cfg.fp_min_for_ceiling is not None
        and cfg.max_edge_for_ceiling is not None
    )
    if extension_applicable:
        p_ext = flavors.get(cfg.signal_name_for_ceiling, float("nan"))
        if not math.isnan(p_ext):  # extension can't fire on NaN
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

    # --- Shared fail-return (strict missed and no extension fired) ---
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
    """Evaluate NO's strict gate, falling back to the ceiling extension.

    Returns ``None`` when ``flavors["p_E"]`` is NaN — the strategy can't
    evaluate, silent skip with no signal logged (matches the pre-extraction
    behavior of the inline NO branch).

    The gate itself (fee, claimed calibration, strict band, ceiling
    extension) lives in ``_evaluate_no_gate``, shared with the FLIP mirror
    sleeve. This wrapper owns the NO-side framing: gate on the NO fill price,
    ``prob_safe_floor`` = calibrated ``P(NO wins)``, and the NO sleeve's
    walker edge floor. Both the raw and calibrated claimed values are
    recorded in ``partial_gates`` as ``claimed_raw`` / ``claimed_calibrated``
    so live monitoring can compare; the calibrated value is the one that
    flows into ``edge`` and ``prob_safe_floor``.
    """
    del n_cum, no_price  # NO gates on its own fill side; no_price is FLIP-only
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
        # Use 0.0 only when p is NaN — a real p value remains useful
        # diagnostic context for the dashboard.
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
        edge_min_for_walker=0.0,  # ratio gate already verified; walker preserves breakeven
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
    """TAIL: N-of-N vote across cfg.vote_signals with min_n guard (F-005)."""
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
    # YHIGH only fires on ceiling brackets ("X-or-higher"). Locked spec:
    # YES side, signal=p_B_50, fp [0.50, 1.00], additive edge [0.02, 0.30].
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
    """FLIP: buy YES on exactly the brackets where the champion NO gate fires.

    Operator-ordered live experiment (2026-08-09). The gate is the SAME
    ``_evaluate_no_gate`` the NO sleeve runs — strict band plus ceiling
    extension — evaluated against the live NO book price (``no_price``) using
    the NO sleeve's tuned config, so FLIP fires iff NO would have fired. The
    trade then executes on the YES side: ``fill_price`` is the YES best ask
    (cfg.side="YES"), and the walker consumes the YES ask book. FLIP's only
    extra precondition is a sane mirrored price: ``no_price`` in (0, 1).

    Economics are deliberately inverted: ``prob_safe_floor`` carries the model
    P(YES) (tiny — the mirrored gate just asserted NO is the value side), so
    the walker's edge floor can never pass. ``edge_min_for_walker`` is -1.0
    (accept any negative edge) and the ONLY fill boundary is the FLIP config's
    ``max_walk_price`` leash (YES ask + 0.05). The recorded ``edge`` is the
    mirrored NO-side edge — an audit value ranking flip conviction, NOT the
    YES trade's expected value (which is negative by model).
    """
    del cfg, n_cum  # gate params mirror the NO sleeve's config
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
        # Pre-extraction FLIP only wrote this key when the extension fired;
        # the merged gate_results still read False otherwise via the seeded
        # default in _evaluate_strategy. Preserved as-is.
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
    """Evaluate one strategy against one bracket. Returns a signal (pass or fail) or None.

    Returning None means the strategy isn't applicable to this bracket at all
    (e.g., NO strategy on the YES side, or the fp band excludes the price even
    before any gate — keeps signal-log noise low). For all gate FAILS that
    represent a real evaluation, we emit a SKIP signal so the dashboard sees it.

    Per-strategy gates (in order):
      1. Hour gate (cfg.entry_hour_set): hard skip, returns None — too cheap to log
      2. Side / fill-price band: returns None — strategy doesn't apply
      3. Volume floor (cfg.min_bvol)
      4. Strategy-specific edge gate (NO additive, YMID ratio, TAIL N-of-N vote)
      5. TAIL min-n guard (vote_min_n) — F-005 fix
      6. Optional delayed-entry price trigger (TAIL waits for <=2c)
      7. Idempotency (strategy-scoped, F-003 fix)
      8. Sizing + walk_book_edge_preserving
      9. WU consensus (handled by caller)
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

    # --- Hour gate ---
    if local_now_hour is not None and local_now_hour not in cfg.entry_hour_set:
        return None

    # --- "Market knows the answer" consensus gate (per-strategy) ---
    # When any bracket in the same (station, target_date) shows YES best_ask
    # >= cfg.consensus_skip_threshold, drop the candidate. Silent skip (no
    # signal logged). This is a station-level market consensus filter, not the
    # per-candidate LUT pred_bucket. Current L2 config enables it for TAIL only;
    # NO deliberately leaves cfg.consensus_skip_threshold=None.
    if (
        cfg.consensus_skip_threshold is not None
        and station_max_yes_ask is not None
        and station_max_yes_ask >= cfg.consensus_skip_threshold
    ):
        return None

    # --- Pick fill side per strategy ---
    if cfg.side == "NO":
        fp_raw = mkt.get("best_bid")
        token_id = mkt.get("no_token_id") or ""
    else:
        fp_raw = mkt.get("best_ask")
        token_id = mkt.get("token_id") or ""

    if fp_raw is None or fp_raw <= 0:
        return None
    fill_price = float(fp_raw)
    # Scanner-time top-of-book. The default walker anchor for first-fill rows.
    entry_top_now = fill_price

    # NO-side book price, needed by FLIP's mirrored gate regardless of which
    # side this strategy fills on. None when the NO quote is missing/zero.
    no_price_raw = mkt.get("best_bid")
    no_price_for_branch: float | None = (
        float(no_price_raw) if no_price_raw is not None and no_price_raw > 0 else None
    )

    # --- Fill price band ---
    # NO's bracket-conditional ceiling extension uses a relaxed fp floor
    # (e.g. 0.50) on ceiling brackets, so widen the gate when applicable.
    # The strict-vs-extension decision still happens inside the NO branch.
    # Ordered before the slot SQL queries so the cheap pure-Python rejects
    # don't trigger the slot reads.
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

    # MIN_FILL_PRICE global floor (defensive — strategy fp_min should already cover this)
    if fill_price < MIN_FILL_PRICE:
        return None

    # --- Slot query (cross-tick top-up support) ---
    #
    # Before the gates fire, read the slot's prior exposure and sticky anchor.
    # Three semantically distinct outcomes:
    #   - slot_filled == 0:              first-fill on this slot; anchor =
    #                                     entry_top_now (scanner-time top).
    #   - slot_filled > 0, anchor set:   top-up on a slot from a prior tick;
    #                                     anchor = the slot's first-fill
    #                                     entry_top_price. Strategies without
    #                                     execution_min_edge still use it as
    #                                     the sticky price leash.
    #   - slot_filled > 0, anchor None:  legacy slot (rows written before the
    #                                     2026-05-11 top-up rollout have no
    #                                     entry_top_price in event_detail).
    #                                     Origin Scope Boundary mandates
    #                                     forward-only behavior — skip the
    #                                     bet rather than silently re-anchor.
    #
    # The slot read here, the gates that follow, and the eventual record_bet
    # in pipeline.py are serialized per (station, target_date) by APScheduler's
    # max_instances=1 on the station scanner job. Without that invariant, the
    # exposure-summing gate would need an explicit IMMEDIATE transaction to
    # prevent concurrent ticks from additively over-staking.
    # Merged slot read: one SQL round-trip returns both cumulative exposure
    # and the sticky anchor. The earlier two-call pattern was retired
    # 2026-05-23 along with the standalone slot_filled_usd / slot_entry_top_price
    # helpers; tests that still need a single-field view wrap slot_state locally.
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

    # --- Hourly-first-tick gate (2026-05-16) ---
    # The backtest harness evaluates each entry hour as a single price
    # snapshot. The live scheduler fires `60 / SCAN_INTERVAL_MINUTES` ticks
    # per hour with a per-ICAO offset. Empirically the extra new-bet
    # attempts hurt because the second-onward tick crosses fresh spread on
    # a stale signal. Restrict OPENING a new slot to the first scheduler
    # tick of the hour; top-ups (slot_filled > 0) keep running on every
    # tick until the entry hour closes so partial fills can still reach
    # `cfg.capital_frac × capital`. ``tick_index_for`` is offset-aware so a
    # misfire that delays the cron-fire minute by up to one
    # SCAN_INTERVAL_MINUTES still reads as tick 0 (see config.py for
    # details). ``local_now_minute=None`` (tests, replay harness) skips
    # the gate so back-compat callers keep working.
    if local_now_minute is not None:
        if tick_index_for(station_id, local_now_minute) >= 1 and slot_filled <= 0:
            return None

    legacy_slot_locked = False
    if slot_filled > 0:
        if slot_anchor_db is None:
            # Legacy row from before the top-up rollout: no anchor stored,
            # so we cannot honor the sticky-cap guarantee. Skip rather than
            # silently re-anchor to the current top.
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

    # Walker anchor: first-fill uses the current scanner top; top-up reuses
    # the slot's first-fill anchor for audit/sticky slot identity. NO/TAIL no
    # longer apply a top-up slip leash; their fill boundary is the realized
    # VWAP edge floor after current entry gates pass.
    if slot_filled > 0 and slot_anchor_db is not None:
        entry_top_price = slot_anchor_db
    else:
        entry_top_price = entry_top_now

    volume = mkt.get("volume24hr")
    market_id = mkt.get("market_id", "")

    # Initialize gate_results with the predictable per-strategy key set.
    # `bracket_extension` is NO-specific but seeded here for every strategy so
    # the dict's key set is uniform across strategies — analytics queries that
    # read `gate_results.bracket_extension` without filtering on
    # `strategy='NO'` get a stable False rather than a missing key.
    gate_results: dict[str, bool | str | float | None] = {
        "strategy": strategy_name,
        "bracket_extension": False,
    }

    # --- Bracket-unit gate (2026-07-16) ---
    # Fail-closed: only bracket units in ALLOWED_BRACKET_UNITS may place live
    # bets; a missing unit ("") is also rejected. Policy history lives on the
    # constant in strategy_constants.py. Recorded on every evaluated bracket
    # so the dashboard sees why disallowed units never bet; folded into
    # `proceed_to_size` below so the walk-book is skipped when disallowed.
    unit_allowed = bracket_unit in ALLOWED_BRACKET_UNITS
    gate_results["unit_allowed"] = unit_allowed

    # --- Strategy-specific signal + edge gate ---
    edge_min_for_walker: float
    edge: float
    prob_safe_floor: float
    signal_used: str
    signal_value: float
    tail_components: dict[str, float] | None = None

    # Cold-start LUT guard (ce-review correctness/kieran-python #19+#20+#21).
    # All four strategies need a minimum-n LUT bucket history before the
    # signal flavor is trustworthy:
    #  - NO uses raw `p_E` — no shrinkage, but the LUT bucket grid still
    #    needs enough history before EMOS's bucket placement is meaningful.
    #  - YMID uses `p_Shrink_n50`, which collapses to `p_E` when n_cum=0
    #    (NaN guard never catches that).
    #  - TAIL already had this guard; we keep it but emit a SKIP signal so
    #    the dashboard records the rejection (was: silent return None).
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

    # --- Volume gate ---
    volume_pass = volume is not None and volume >= cfg.min_bvol
    gate_results["volume"] = volume_pass if edge_pass else None

    # --- Optional delayed-entry trigger ---
    #
    # TAIL keeps its normal signal band up to 3c so we can record "signal was
    # good, still waiting" telemetry. The actual entry/top-up is blocked until
    # the current YES ask reaches the WFO-selected trigger (2c).
    delayed_entry_pass = True
    if cfg.delayed_entry_fp_max is not None:
        if edge_pass and volume_pass:
            delayed_entry_pass = fill_price <= cfg.delayed_entry_fp_max
            gate_results["delayed_entry"] = delayed_entry_pass
        else:
            delayed_entry_pass = False
            gate_results["delayed_entry"] = None

    # --- Idempotency (exposure-summing across the slot's non-cancelled rows) ---
    #
    # Replaces the legacy count-based duplicate gate. The slot stays eligible
    # for additional fills while cumulative exposure < target_usd; once at or
    # above target it's permanently closed for this entry window.
    #
    # `effective_min_bet` floors the per-top-up minimum at 1% of target (with
    # MIN_BET_USD as the absolute floor) so a slot trickling toward exhaustion
    # doesn't produce dust rows. At a $5000 target, the dust floor is $50.
    # Computed unconditionally so the post-walk dust recheck (below) can
    # reuse it even when the pre-walk idempotency gate passed.
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

    # --- Sizing + walk-book ---
    bet_size_usd = 0.0
    limit_price = 0.0
    # Decision-time walked VWAP of this fill; persisted as entry_fill_vwap for
    # audit and for any future sleeve that opts into a top-up slip leash.
    entry_fill_vwap_val: float | None = None
    # Order-time slip anchor for a slip-bounded top-up (carried on the signal so
    # the order/dry-run re-walk enforces the same leash). None => no slip cap.
    slip_anchor_for_signal: float | None = None

    if proceed_to_size:
        target_usd = _compute_target_usd(cfg, capital)
        # Top-up subtracts already-filled exposure from the target so each
        # tick only attempts to deploy the unfilled remainder. First-fill
        # rows (slot_filled == 0) get the full target.
        remaining = max(0.0, target_usd - slot_filled)
        if target_usd < MIN_BET_USD:
            gate_results["insufficient_capital"] = False
            proceed_to_size = False
        elif target_usd > capital:
            # Don't bet more than total capital
            gate_results["insufficient_capital"] = False
            proceed_to_size = False
        elif remaining < MIN_BET_USD:
            # Belt-and-suspenders: the idempotency gate above already rejected
            # remaining < effective_min_bet (>= MIN_BET_USD). Refusing again
            # here against the floor ensures the walker never sees a sub-floor
            # target if some future caller skips the idempotency check.
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
                # Walk params depend on first-fill vs optional slip-bounded
                # top-up:
                #
                #  - Slip-bounded TOP-UP (slot_filled > 0, sleeve has a
                #    max_vwap_slip_from_anchor leash, AND the slot has a real
                #    first-fill VWAP anchor): walk against (first_fill_vwap +
                #    slip cap). The walker stops before any level above that
                #    bound and keeps the partial fill; the execution_min_edge
                #    floor still applies (whichever binds first). The walked
                #    VWAP is carried to order time via slip_anchor_for_signal so
                #    the FAK re-walk enforces the same leash, not just the
                #    looser edge floor.
                #
                #  - NULL-ANCHOR GUARD (slot_first_fill_vwap is None on a
                #    top-up): transition slots written before the 2026-05-29
                #    slip guard have an entry_top_price but no entry_fill_vwap.
                #    Passing the cap with a None anchor would make the walker
                #    re-anchor to the drifted LIVE top — the exact book-drift
                #    bug the sticky anchor exists to prevent. These fall through
                #    to the strategy default (NO/TAIL -> no price cap, edge-floor
                #    only), forward-only like the
                #    legacy_slot_locked path.
                #
                #  - FIRST fill (slot_filled == 0): exempt — walks freely to the
                #    edge floor and SETS the anchor (its walked VWAP becomes
                #    entry_fill_vwap, read back by later top-ups).
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
                    # Persist this fill's book-walked VWAP. On a first fill it
                    # becomes the slot's first-fill VWAP; on a top-up it is this
                    # row's own realized VWAP. Set unconditionally after a
                    # successful walk -- a later edge-ceiling/dust SKIP just
                    # records it as telemetry on the signals row (never reaches
                    # the ledger).
                    entry_fill_vwap_val = filled_vwap
                    # Strategy-specific post-walk edge ceiling.
                    #
                    # Defensive belt-and-suspenders: structurally redundant
                    # today because the walker's edge floor + the pre-walk
                    # gate ensure walked_edge ≤ pre_walk_edge ≤ ceiling
                    # (walker only consumes ascending prices → VWAP only goes
                    # up → edge only goes down). Kept against future walker
                    # changes that might break the monotonicity invariant
                    # (e.g. fee tiers, rebates, mid-walk prob_safe_floor
                    # refresh) — those would silently start producing
                    # walked_edge > ceiling without this guard.
                    #
                    # NO uses the ceiling-extension's relaxed max_edge (0.35)
                    # when the extension path fired, otherwise the strict
                    # max_edge (0.15). YMID + YHIGH share the simple
                    # cfg.max_edge gate.
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
                        # Keep signal price/edge as the entry-gate audit values.
                        # Execution VWAP/edge is persisted from OrderResult.
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
                            # Post-walk dust guard: the walker can return any
                            # amount above MIN_BET_USD, but the slot's dust
                            # floor (1% of target_usd) is the operative
                            # minimum once a top-up is in flight. A $5 fill
                            # against a $50 floor would otherwise slip
                            # through the pre-walk idempotency gate.
                            gate_results["insufficient_size"] = False
                            proceed_to_size = False
                            bet_size_usd = 0.0

    # --- Build BetSignal (pass or fail) ---
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

    # Stash TAIL components on gate_results for ledger persistence (F-009).
    # record_bet reads json_extract(event_detail, '$.tail_votes') from this dict.
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
    """Evaluate all brackets for a station against the 4-strategy stack.

    Each bracket is run through NO, YMID, TAIL, and YHIGH strategies
    independently. A single bracket can produce up to one signal per
    strategy (typically NO and either YMID/TAIL based on fp band; YHIGH
    only fires on ceiling brackets in favorite territory). Each signal
    carries its `strategy` tag for downstream ledger writes and TP/SL
    monitor scoping. NO has a ceiling-bracket fallback gate that uses
    p_B_50 with relaxed thresholds when the strict gate misses.

    Args:
        conn: DB connection for LUT lookup and idempotency check.
        station: Station config with id, unit, timezone.
        model: EMOS model for this station/horizon.
        ensemble_members: Array of forecast tmax values from all models.
        market_data: Dict of bracket_idx → {token_id, market_id, best_bid,
            best_ask, volume24hr, _yes_book, _no_book}.
        capital: Current deployable capital (after pending exposure).
        target_date: ISO date string for the bet target.
        horizon: 1, 2, or 3 (days ahead).
        dry_run: Whether to evaluate against the simulated or live ledger book.
        local_now_hour: Station-local current hour. When None, the per-strategy
            entry-hour gate is skipped entirely (back-compat for tests). The
            scanner passes the live local hour at tick time.
        local_now_minute: Station-local current minute. When None, the hourly-
            first-tick gate is skipped (back-compat for tests / replay tools).

    Returns:
        List of BetSignal with gate_results populated (both pass and fail).
    """
    station_id = station.icao
    signals: list[BetSignal] = []

    # --- Build brackets from Polymarket's actual markets ---
    # Use Polymarket's bracket bounds (not locally computed) so we only
    # bet on brackets that actually exist on the market.
    poly_brackets, poly_market_order = _extract_polymarket_brackets(market_data)

    if not poly_brackets:
        logger.info("Station %s: no parseable Polymarket brackets, skipping", station_id)
        return signals

    # Bracket unit is the Polymarket label unit (°C/°F in the market question),
    # not station.unit. Some stations display in one unit while Polymarket
    # resolves in the other (e.g. EFHK Helsinki: display °F, market °C).
    # Fail closed if no label carries a unit marker — assuming a default
    # would silently compare °F bounds against °C ensemble (~30°C error).
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

    # Compute probabilities for Polymarket's actual brackets
    probs = bracket_probabilities(model, ensemble_members, poly_brackets, bracket_unit, icao=station_id)

    # bracket_probabilities returns [] when it fails closed (NaN propagation,
    # missing corner with collapsed sum). Skip the station entirely.
    if not probs or len(probs) != len(poly_brackets):
        logger.info("Station %s: bracket_probabilities failed closed, skipping", station_id)
        return signals

    ledger_event_types = _ledger_event_types_for_mode(dry_run)
    ledger_event_placeholders = ",".join("?" for _ in ledger_event_types)

    # --- Count existing bets today for MAX_PER_MARKET ---
    existing_bets = conn.execute(
        f"""SELECT COUNT(*) as cnt FROM ledger
            WHERE station_id = ? AND target_date = ?
            AND event_type IN ({ledger_event_placeholders})
            AND outcome != 'CANCELLED'""",
        (station_id, target_date, *ledger_event_types),
    ).fetchone()["cnt"]
    remaining_slots = max(0, MAX_PER_MARKET - existing_bets)

    # --- Station-wide consensus snapshot ("market knows the answer") ---
    # The per-strategy `consensus_skip_threshold` compares against the max YES
    # best_ask seen across all brackets in this station+target_date. Compute
    # once per scanner tick — every bracket reads the same value. None means
    # no bracket has a usable best_ask in this tick (consensus check disabled).
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

    # --- Reliability-calibration provider (2026-07-16 NO over-confidence fix) ---
    # Loaded once per station tick from the reliability_curves table. Guardrails
    # (min pairs / max age) are applied at load; a missing / stale / thin curve
    # yields an identity provider (no behavioral change). Only the NO branch
    # consumes it.
    reliability = ReliabilityProvider.load(conn)

    # --- Evaluate each Polymarket bracket against all 4 strategies ---
    candidates: list[BetSignal] = []

    # Per-station cumulative-LUT cache for this scanner tick. The 11 brackets
    # frequently land in the same pred_bucket (adjacent brackets share EMOS
    # probabilities), so memoising the lookup avoids redundant SQLite reads
    # against pred_bucket_history within a single evaluate_station call.
    _lut_cum_cache: dict[tuple[float, float], CumulativeStats] = {}

    for idx, (btype, blo, bhi) in enumerate(poly_brackets):
        bi = poly_market_order[idx]
        mkt = market_data[bi]

        p_model = probs[idx]
        label = mkt.get("bracket_label") or bracket_label(station, (btype, blo, bhi))

        # Threshold stored in BetSignal: upper bound for floor/interior, lower for ceiling.
        if btype == "ceiling":
            threshold_val = float(blo) if blo is not None else 0.0
        else:
            threshold_val = float(bhi) if bhi is not None else 0.0

        # NaN-safe bucket mapping. If EMOS isn't ready, emit one diagnostic
        # signal per bracket and skip — the dashboard sees the reason.
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

        # Map Polymarket bracket type to the bracket_kind label used by
        # strategy gates (mirrors backtest sweep_lib.parse_bracket).
        if btype == "ceiling":
            bracket_kind = "ceiling"
        elif btype == "floor":
            bracket_kind = "floor"
        else:
            bracket_kind = "interior"

        bracket_ctx = BracketContext(
            bracket_idx=bi,
            mkt=mkt,
            threshold_val=threshold_val,
            bracket_low=mkt.get("bracket_low"),
            bracket_high=mkt.get("bracket_high"),
            bracket_kind=bracket_kind,
            bracket_label=label,
            bracket_unit=bracket_unit,
        )
        calibration_ctx = CalibrationContext(
            p_emos=p_model,
            pred_bucket=pred_bucket,
            flavors=flavors,
            n_cum=cum.n_cum,
        )

        # Run each of the strategies independently against this bracket.
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

    # --- WU consensus gate runs on candidates only (after strategy gates) ---
    # OFF    — skipped entirely; no WU fetch, no verdict written.
    # SHADOW — records verdict on the signal but does not flow into
    #          gate_results / passed_all_gates.
    # BLOCK  — participates in gate_results; None (WU unavailable) is
    #          fail-closed.
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

    # Re-derive candidates after WU gate (some may have been demoted).
    candidates = [s for s in candidates if s.passed_all_gates]

    # --- MAX_PER_MARKET trim ---
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
    """Rank passing signals by edge, enforce per-target-date notional cap.

    Used budget is summed from the current target-date book (excluding
    CANCELLED). Available = ``MAX_DAILY_NOTIONAL_FRAC × capital − used``.
    Candidates are placed best-edge first; each that fits decrements the
    remaining budget. Candidates whose stake exceeds the remaining budget
    are dropped (``gate_results["daily_notional"] = False``).
    """
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
