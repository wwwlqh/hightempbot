"""Pipeline orchestrator — wires forecast → decision → execution → ledger.

This module contains `run_betting_cycle()` which is called by the per-station
scanner (Unit 9) for each 15-min tick within the betting window.
"""

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


# --- Halt-alert dedupe (ce-code-review P3 #66) ---
# Wallet-unavailable and low-balance halts run per-station, so a 30-station
# fleet would page Telegram ~30× per tick when wallet reads fail. Dedupe by
# halt kind via monotonic wall-clock timestamps; halt behavior itself (the
# `return result`) is unchanged — this only suppresses the redundant alerts.
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
    """Run a single betting cycle for one station.

    Args:
        conn: DB connection (per-thread, WAL mode).
        station: Station config.
        ensemble_data: {model_name: tmax_celsius} from fetch_live().
        market_data: {bracket_idx: {best_ask, best_bid, volume24hr, ...}}.
        order_client: CLOB client (None in dry-run).
        initial_bankroll: Starting capital from config.
        dry_run: If True, log signals but don't place orders.
        target_date: ISO date for the bet target. The scanner passes the UTC
            readiness-cycle N+1 market date after ensemble readiness.
        horizon: Days ahead (1, 2, or 3).

    Returns:
        CycleResult with counts.
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

    # --- Data coverage gate ---
    # Coverage measured against a fixed reference window (matching notebook methodology).
    # Stations that started late or have large gaps are excluded.
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

    # --- Wallet halt (live only) -- ce-code-review P1 #10 ---
    # In live mode the wallet IS the source of truth. If we can't read it,
    # we must NOT silently fall back to ledger capital (operator could have
    # withdrawn collateral, account could be paused, etc.). Halt and alert.
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
        # Halt new entries when realized capital has fallen MAX_DD off peak.
        # Live-only — dry_run mode skips the halt so operators can flip to
        # dry_run to investigate behavior after a live drawdown without the
        # halt silencing signal flow. In live mode peak comes from the
        # bankroll_peak high-water table (wallet readings) so the halt fires
        # on real-money drawdown, not ledger PnL accounting.
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

    # Sizing basis: live wallet cash already excludes CLOB-filled open
    # positions, but those positions are unrealized and should not shrink the
    # strategy's target stake. Use cash + open cost basis for target sizing;
    # keep deployable_capital for the final cash-affordability check.
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

    # --- Rank passing signals (before logging, so dashboard reflects final state) ---
    # Target-date notional is scoped to stake basis. Passing deployable cash
    # would subtract existing same-date open cost twice (`wallet_balance` first,
    # then `used` inside rank_signals) and shrink the book as positions remain
    # unrealized.
    budget_capital = stake_basis_capital
    passing = rank_signals(conn, signals, budget_capital, dry_run=dry_run)
    result.n_passed_gates = len(passing)

    # --- Log non-passing signals immediately; passing signals are logged
    # only after execution so BET/WOULD_BET reflects the final outcome. ---
    # `bracket_extension` is seeded False on every strategy (decision.py
    # initialises gate_results so analytics queries see a stable key) and is
    # only flipped True on NO when the ceiling-extension path fires. It is
    # NOT a real per-strategy gate, so excluding it from the "first False"
    # walk is required — otherwise every non-extension skip across NO and
    # all of TAIL/YMID/YHIGH gets mislabeled `SKIP:bracket_extension`,
    # masking the actual failing gate.
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

    # In-tick cash tracker (ce-code-review P0 #3 / ADV-004 race fix).
    # MAX_PENDING_EXPOSURE_PCT was checked once at the top of the tick against
    # the snapshot's pending_exposure. As record_bet commits happen inside
    # this loop, concurrent station ticks read STALE pending values and could
    # collectively over-deploy. The DB-backed exposure check uses a fresh SUM
    # under BEGIN IMMEDIATE, so it already includes this tick's committed
    # PENDING rows. Track only cash reserved by successful/uncertain live
    # submissions so the stale wallet snapshot cannot be overspent.
    in_tick_cash_reserved = 0.0
    placed_live_order = False

    def _skip_signal(sig: BetSignal, key: str) -> None:
        """Mark a bracket skipped for ``key`` and log the SKIP signal.

        Shared tail of the three copy-paste skip blocks (lock_contention,
        exposure_cap_race, deployable_cash). Callers own their preceding
        ``conn.rollback()`` and log line; this only records the SKIP.
        """
        sig.passed_all_gates = False
        sig.gate_results[key] = False
        try:
            record_signal(conn, sig, tick_ts, f"SKIP:{key}")
        except Exception:
            logger.error("Failed to log %s signal", key, exc_info=True)

    for sig in passing:
        # re-check operator-control gate per bracket.
        # Stop Processing or TRANSFER_LOCK pressed mid-tick must halt the
        # remaining brackets in the loop, not just the next tick.
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
            # Per-bracket BEGIN IMMEDIATE around record_bet + transactional
            # cap re-check. APScheduler max_instances=1 serializes ticks
            # PER STATION, but cross-station ticks fire concurrently. Without
            # a fresh exposure SUM under the write-lock, sibling stations
            # all read the same stale `total_pending` from the tick-start
            # snapshot and collectively over-deploy past the cap.
            # bounded retry instead of silent fallthrough.
            # If we can't acquire the write-lock, the exposure-cap re-check
            # below sees stale data and the race fix is silently disabled.
            # Retry with 50/100/200ms backoff (≤ 350ms total). If still busy,
            # SKIP this bracket as `lock_contention` so we never proceed
            # against the cap without holding the lock. A sibling station's
            # bracket gets the next attempt; nothing is silently dropped.
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

            # --- Transactional exposure cap re-check (P0 #3) ---
            # Re-SELECT live pending now that we hold BEGIN IMMEDIATE -- this
            # sees any commits from this tick and sibling stations that landed
            # after the tick-start snapshot. Add only this bet's notional; this
            # tick's previous PENDING rows are already in fresh_pending.
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

            # Insert the PENDING ledger row first so the bot always has a
            # record even if it crashes mid-verify; execute_or_log then
            # owns the PENDING → FILLED / CANCELLED transition.
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
                # Dry-run: execute_or_log stamps DRY_RUN_<uuid> on the row
                # and returns None. Count it as placed for dashboard metrics.
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
                # Surface the richer reason (e.g., "insufficient_depth") when the
                # order path knows why it failed; fall back to generic "execution".
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
            # Release the BEGIN IMMEDIATE lock if it's still held — the
            # exception fired before record_bet's conn.commit() ran, so
            # the transaction is open and would otherwise stall the next
            # bracket's BEGIN IMMEDIATE on a busy lock.
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

    # Single post-loop wallet refresh. The refreshed snapshot is only consumed
    # by the NEXT tick's get_capital_snapshot, never within this loop, so one
    # refresh after all orders is equivalent to the prior per-order refresh
    # (last write wins) with fewer CLOB round-trips.
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
