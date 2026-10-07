"""``run_betting_cycle``: safety checks, then evaluate, rank, record and place bets."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from typing import TYPE_CHECKING

import numpy as np

from hightempbot.db.connection import utc_now_sql
from hightempbot.execution.capital import get_capital_snapshot, live_pending_notional
from hightempbot.execution.strategy_constants import MAX_DD
from hightempbot.decision.strategies import evaluate_station, rank_signals
from hightempbot.persistence.actuals import actual_source_clause
from hightempbot.persistence.ledger import record_bet, record_signal
from hightempbot.execution.walker import OrderClient, execute_or_log
from hightempbot.execution.types import BetSignal, CycleResult
from hightempbot.stations import StationConfig

if TYPE_CHECKING:
    from hightempbot.runtime_config import Config

logger = logging.getLogger(__name__)


# Halt alerts fire per station; send each kind at most once per window.
_HALT_ALERT_DEDUPE_SECONDS = 300.0
_last_halt_alert_ts: dict[str, float] = {}


def _should_send_halt_alert(kind: str) -> bool:
    """Return True once per `_HALT_ALERT_DEDUPE_SECONDS` per halt kind."""
    import time as _time

    now = _time.monotonic()
    last = _last_halt_alert_ts.get(kind, 0.0)
    if now - last < _HALT_ALERT_DEDUPE_SECONDS:
        return False
    _last_halt_alert_ts[kind] = now
    return True


def _refresh_wallet_after_live_order(
    conn: sqlite3.Connection,
    *,
    config: "Config | None",
    order_client: OrderClient | None,
) -> None:
    """Best-effort wallet refresh so dashboard value/PnL follows Data API."""
    if config is None or order_client is None:
        return
    try:
        from hightempbot.persistence.wallet_reconciliation import refresh_wallet_snapshot

        refresh_wallet_snapshot(
            conn,
            config=config,
            order_client_factory=lambda _cfg: order_client,
        )
    except Exception:
        logger.warning("Post-order wallet snapshot refresh failed", exc_info=True)


def run_betting_cycle(
    conn: sqlite3.Connection,
    station: StationConfig,
    ensemble_data: dict[str, float],
    market_data: dict[int, dict],
    order_client: OrderClient | None,
    initial_bankroll: float,
    dry_run: bool,
    target_date: str | None = None,
    horizon: int = 1,
    config: "Config | None" = None,
    local_now_hour: int | None = None,
    local_now_minute: int | None = None,
) -> CycleResult:
    """Run one betting cycle for a station.

    ``ensemble_data`` is {model: tmax_c}; ``market_data`` is {bracket_idx:
    {best_ask, best_bid, volume24hr, ...}}; ``order_client`` is None in dry-run.
    Order: operator → coverage → capital → wallet → drawdown → exposure →
    ensemble → calibration, then per bracket a locked re-check, PENDING
    insert and execution.
    """
    station_id = station.icao
    tick_ts = utc_now_sql()

    if target_date is None:
        from datetime import timedelta as _td
        target_date = (date.today() + _td(days=horizon)).isoformat()

    result = CycleResult(dry_run=dry_run)

    try:
        from hightempbot.execution.operator_control import processing_block_reason

        block_reason = processing_block_reason(conn, dry_run=dry_run)
    except Exception:
        block_reason = None if dry_run else "operator-control gate unavailable"
        logger.warning("Operator-control gate failed in dry_run=%s", dry_run, exc_info=True)
    if block_reason:
        logger.info("Station %s: %s", station_id, block_reason)
        from hightempbot.db.connection import log_pipeline_health
        log_pipeline_health(conn, station_id, "operator", "SKIP", block_reason[:500])
        return result

    # --- Data coverage gate (actuals since REF_START_DATE) ---
    from hightempbot.execution.strategy_constants import MIN_COVERAGE_PCT, REF_START_DATE
    actuals_clause, actuals_params = actual_source_clause()
    cov_row = conn.execute(
        "SELECT COUNT(*) as n_days FROM actuals "
        "WHERE station_id = ? AND local_date >= ? "
        f"AND {actuals_clause}",
        (station_id, REF_START_DATE, *actuals_params),
    ).fetchone()
    n_days = cov_row["n_days"] or 0
    from datetime import date as _date
    ref_span = (_date.today() - _date.fromisoformat(REF_START_DATE)).days + 1
    coverage = n_days / max(ref_span, 1)
    if coverage < MIN_COVERAGE_PCT:
        logger.info("Station %s: coverage %.1f%% < %.0f%% of ref window, skipping", station_id, coverage * 100, MIN_COVERAGE_PCT * 100)
        return result

    # --- Capital & drawdown (wallet-derived in live, ledger in dry-run) ---
    try:
        capital_state = get_capital_snapshot(
            conn, initial_bankroll,
            order_client=order_client if not dry_run else None,
            dry_run=dry_run,
        )
    except Exception:
        logger.error("Failed to compute capital for %s", station_id, exc_info=True)
        return result

    # --- Wallet halt (live): no wallet reading, no betting ---
    if not dry_run:
        if not capital_state.wallet_available:
            msg = (
                f"Halted: CLOB wallet unavailable (check_balance returned None or raised). "
                f"Live trading paused until wallet read succeeds."
            )
            logger.error("Station %s: %s", station_id, msg)
            from hightempbot.db.connection import log_pipeline_health
            log_pipeline_health(conn, station_id, "gates", "ERROR", msg[:500])
            if _should_send_halt_alert("wallet_unavailable"):
                try:
                    from hightempbot.execution.notify import send_alert
                    send_alert(
                        title="Wallet unavailable (fleet halt)",
                        message=(
                            f"First-affected station: {station_id}\n{msg}\n"
                            "Subsequent per-station alerts suppressed for "
                            f"{int(_HALT_ALERT_DEDUPE_SECONDS)}s."
                        ),
                        config=config,
                        stage="startup_degraded",
                        station_id=station_id,
                    )
                except Exception:
                    pass
            return result
        if (capital_state.wallet_balance or 0.0) < 5.0:
            msg = (
                f"Halted: CLOB wallet balance ${capital_state.wallet_balance:.2f} < $5.00. "
                f"Live trading paused until wallet topped up."
            )
            logger.error("Station %s: %s", station_id, msg)
            from hightempbot.db.connection import log_pipeline_health
            log_pipeline_health(conn, station_id, "gates", "ERROR", msg[:500])
            if _should_send_halt_alert("wallet_low_balance"):
                try:
                    from hightempbot.execution.notify import send_alert
                    send_alert(
                        title="Wallet low (fleet halt)",
                        message=(
                            f"First-affected station: {station_id}\n{msg}\n"
                            "Subsequent per-station alerts suppressed for "
                            f"{int(_HALT_ALERT_DEDUPE_SECONDS)}s."
                        ),
                        config=config,
                        stage="startup_degraded",
                        station_id=station_id,
                    )
                except Exception:
                    pass
            return result

    deployable_capital = capital_state.deployable_capital
    stake_basis_capital = capital_state.stake_basis_capital
    peak_capital = capital_state.peak_realized_capital
    realized_capital = capital_state.realized_capital
    drawdown = (peak_capital - realized_capital) / peak_capital if peak_capital > 0 else 0.0
    if drawdown >= MAX_DD and not dry_run:
        # Drawdown halt (live only, so dry-run can still be used to investigate).
        msg = (
            f"Halted: drawdown {drawdown * 100:.1f}% >= MAX_DD "
            f"({MAX_DD * 100:.0f}%); realized=${realized_capital:.2f} "
            f"peak=${peak_capital:.2f}"
        )
        logger.warning("Station %s: %s", station_id, msg)
        from hightempbot.db.connection import log_pipeline_health
        log_pipeline_health(conn, station_id, "gates", "SKIP", msg[:500])
        return result

    # --- Pending exposure cap (all event types) ---
    from hightempbot.execution.strategy_constants import MAX_PENDING_EXPOSURE_PCT
    total_pending = capital_state.pending_exposure
    exposure_cap = stake_basis_capital * MAX_PENDING_EXPOSURE_PCT
    if total_pending >= exposure_cap:
        logger.info("Station %s: total pending $%.2f >= %.0f%% of capital ($%.2f), no new positions",
                     station_id, total_pending, MAX_PENDING_EXPOSURE_PCT * 100, exposure_cap)
        return result

    # Size from cash + open cost; check affordability against deployable cash.
    effective_capital = stake_basis_capital

    # --- Build ensemble array ---
    if not ensemble_data:
        logger.warning("No ensemble data for %s", station_id)
        return result

    from hightempbot.execution.strategy_constants import EXPECTED_MODELS

    present = set(ensemble_data.keys())
    expected = set(EXPECTED_MODELS)
    if present != expected:
        logger.warning(
            "Station %s: ensemble set mismatch before scoring (present=%s, expected=%s)",
            station_id,
            sorted(present),
            sorted(expected),
        )
        return result

    ensemble_members = np.array(
        [ensemble_data[model_name] for model_name in EXPECTED_MODELS],
        dtype=float,
    )

    # --- Load calibration model ---
    try:
        from hightempbot.calibration.model import CalibrationModel
        from hightempbot.calibration.store import load_emos

        emos_params = load_emos(conn, station_id, horizon)

        model = CalibrationModel(
            station_id=station_id,
            horizon=horizon,
            emos_params=emos_params,
        )
        if not model.is_ready():
            logger.debug("Calibration not ready for %s h%d", station_id, horizon)
            return result
    except Exception:
        logger.error("Failed to load calibration model for %s h%d", station_id, horizon, exc_info=True)
        return result

    # --- Evaluate station ---
    try:
        signals = evaluate_station(
            conn=conn,
            station=station,
            model=model,
            ensemble_members=ensemble_members,
            market_data=market_data,
            capital=effective_capital,
            target_date=target_date,
            horizon=horizon,
            dry_run=dry_run,
            local_now_hour=local_now_hour,
            local_now_minute=local_now_minute,
        )
    except Exception:
        logger.error("evaluate_station failed for %s", station_id, exc_info=True)
        return result

    result.n_evaluated = len(signals)

    # --- Rank (against stake basis; deployable cash would double-count open cost) ---
    budget_capital = stake_basis_capital
    passing = rank_signals(conn, signals, budget_capital, dry_run=dry_run)
    result.n_passed_gates = len(passing)

    # Log failing signals now and passing ones after execution. The SKIP
    # reason is the first False gate, ignoring bracket_extension (not a gate).
    _NON_GATE_KEYS = frozenset({"bracket_extension"})

    passing_ids = {id(sig) for sig in passing}
    for sig in signals:
        if id(sig) in passing_ids:
            continue
        first_fail = next(
            (
                k for k, v in sig.gate_results.items()
                if v is False and k not in _NON_GATE_KEYS
            ),
            "unknown",
        )
        outcome_label = f"SKIP:{first_fail}"
        try:
            record_signal(conn, sig, tick_ts, outcome_label)
        except Exception:
            logger.error("Failed to log signal for %s", sig.bracket_label, exc_info=True)

    # Cash reserved by this tick's live orders, so the start-of-tick wallet
    # snapshot can't be overspent.
    in_tick_cash_reserved = 0.0
    placed_live_order = False

    def _skip_signal(sig: BetSignal, key: str) -> None:
        """Record a bracket as skipped for ``key``."""
        sig.passed_all_gates = False
        sig.gate_results[key] = False
        try:
            record_signal(conn, sig, tick_ts, f"SKIP:{key}")
        except Exception:
            logger.error("Failed to log %s signal", key, exc_info=True)

    for sig in passing:
        # Re-check operator control so Stop takes effect mid-tick.
        try:
            from hightempbot.execution.operator_control import processing_block_reason as _block

            mid_tick_block = _block(conn, dry_run=dry_run)
        except Exception:
            mid_tick_block = None
        if mid_tick_block:
            logger.info(
                "Station %s: operator gate engaged mid-tick (%s); skipping remaining brackets",
                station_id, mid_tick_block,
            )
            break
        row_id = None
        outcome_label = "SKIP:execution"
        try:
            # Other stations tick concurrently, so re-check exposure under a
            # write lock. If the lock can't be had after short retries, skip
            # the bracket rather than check against stale data.
            in_tx = False
            _last_lock_err: sqlite3.OperationalError | None = None
            for _attempt, _delay in enumerate((0.0, 0.05, 0.10, 0.20)):
                if _delay > 0.0:
                    import time as _t
                    _t.sleep(_delay)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    in_tx = True
                    _last_lock_err = None
                    break
                except sqlite3.OperationalError as _exc:
                    _last_lock_err = _exc
                    logger.info(
                        "Station %s: BEGIN IMMEDIATE busy on %s (attempt %d/4, last err: %s)",
                        station_id, sig.bracket_label, _attempt + 1, _exc,
                    )
            if not in_tx:
                logger.warning(
                    "Station %s: BEGIN IMMEDIATE could not be acquired for %s after retries (%s); "
                    "skipping bracket to preserve exposure-cap race fix",
                    station_id, sig.bracket_label, _last_lock_err,
                )
                _skip_signal(sig, "lock_contention")
                continue

            # --- Exposure cap re-check with fresh PENDING totals ---
            try:
                fresh_pending = live_pending_notional(conn)
            except Exception:
                fresh_pending = total_pending  # fall back to tick-start view
            projected_pending = fresh_pending + float(sig.bet_size_usd)
            if not dry_run and projected_pending > exposure_cap:
                if in_tx:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    in_tx = False
                logger.info(
                    "Station %s: exposure_cap_race blocks %s ("
                    "fresh_pending=$%.2f + bet=$%.2f > cap=$%.2f)",
                    station_id, sig.bracket_label,
                    fresh_pending, sig.bet_size_usd, exposure_cap,
                )
                _skip_signal(sig, "exposure_cap_race")
                continue

            projected_cash = in_tick_cash_reserved + float(sig.bet_size_usd)
            if not dry_run and projected_cash > deployable_capital + 1e-9:
                if in_tx:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    in_tx = False
                logger.info(
                    "Station %s: deployable_cash blocks %s (reserved=$%.2f + "
                    "bet=$%.2f > deployable=$%.2f)",
                    station_id, sig.bracket_label,
                    in_tick_cash_reserved, sig.bet_size_usd, deployable_capital,
                )
                _skip_signal(sig, "deployable_cash")
                continue

            # PENDING row first, so a crash mid-order still leaves a record.
            row_id = record_bet(conn, sig, None, dry_run=dry_run)
            in_tx = False  # record_bet committed; lock released
            order_result = execute_or_log(
                sig,
                order_client,
                dry_run=dry_run,
                row_id=row_id,
                conn=conn,
                config=config,
            )

            if dry_run:
                result.n_placed += 1
                result.total_exposure_usd += sig.bet_size_usd
                if sig.slot_filled_pre > 0:
                    result.n_topup += 1
                outcome_label = "WOULD_BET"
            elif order_result is not None and order_result.success:
                placed_size = order_result.bet_size_usd or sig.bet_size_usd
                in_tick_cash_reserved += float(placed_size)
                result.n_placed += 1
                result.total_exposure_usd += placed_size
                placed_live_order = True
                if sig.slot_filled_pre > 0:
                    result.n_topup += 1
                outcome_label = "BET"
            elif order_result is not None:
                if order_result.leave_pending:
                    in_tick_cash_reserved += float(order_result.bet_size_usd or sig.bet_size_usd)
                logger.warning("Order did not fill for %s: %s", sig.bracket_label, order_result.error)
                sig.passed_all_gates = False
                reason_key = (order_result.error or "").strip().split(":", 1)[0]
                if reason_key and reason_key.replace("_", "").isalnum():
                    sig.gate_results[reason_key] = False
                    outcome_label = f"SKIP:{reason_key}"
                else:
                    sig.gate_results["execution"] = False
                    outcome_label = "SKIP:execution"
            else:
                sig.passed_all_gates = False
                sig.gate_results["execution"] = False
                outcome_label = "SKIP:execution"

        except Exception:
            # Release the write lock if the error came before the commit.
            if in_tx:
                try:
                    conn.rollback()
                except Exception:
                    logger.error("ROLLBACK failed after broken record_bet for %s",
                                 sig.bracket_label, exc_info=True)
                in_tx = False
            if row_id is not None:
                try:
                    conn.execute(
                        """UPDATE ledger
                        SET outcome = 'CANCELLED', pnl = 0.0
                        WHERE id = ? AND outcome = 'PENDING' AND order_id IS NULL""",
                        (row_id,),
                    )
                    conn.commit()
                except Exception:
                    logger.error("Failed to cancel broken pending row for %s", sig.bracket_label, exc_info=True)
            sig.passed_all_gates = False
            sig.gate_results["execution"] = False
            outcome_label = "SKIP:execution"
            logger.error("Execution failed for %s", sig.bracket_label, exc_info=True)

        try:
            record_signal(conn, sig, tick_ts, outcome_label)
        except Exception:
            logger.error("Failed to log post-exec signal for %s", sig.bracket_label, exc_info=True)

    # Refresh the wallet once for the next tick.
    if placed_live_order:
        _refresh_wallet_after_live_order(
            conn,
            config=config,
            order_client=order_client,
        )

    logger.info(
        "Cycle %s: evaluated=%d, passed=%d, placed=%d, exposure=$%.2f %s",
        station_id, result.n_evaluated, result.n_passed_gates,
        result.n_placed, result.total_exposure_usd,
        "(DRY-RUN)" if dry_run else "(LIVE)",
    )

    return result
