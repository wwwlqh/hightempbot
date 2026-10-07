"""Main entry point - boots scheduler and dashboard.

Usage: python -m hightempbot.main
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import sys
import threading
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from hightempbot.runtime_config import Config
from hightempbot.dashboard.app import app, configure as configure_dashboard
from hightempbot.db.connection import init_db
from hightempbot.scheduler.jobs import schedule_all_jobs


def _format_exception(exc: BaseException | None, limit: int = 700) -> str:
    """Return a compact exception summary for operator alerts."""
    if exc is None:
        return "unknown"
    text = f"{type(exc).__name__}: {exc}".strip()
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 14].rstrip() + " ...[truncated]"
    return text


def _send_operational_alert(
    title: str,
    message: str,
    *,
    cfg: Config | None = None,
    stage: str = "runtime",
    station_id: str = "",
) -> bool:
    """Best-effort Telegram alert for failures outside normal trading paths."""
    try:
        from hightempbot.execution.notify import send_alert

        return send_alert(
            title,
            message,
            config=cfg or Config(),
            stage=stage,
            station_id=station_id,
        )
    except Exception:
        logging.getLogger("hightempbot.main").debug(
            "Operational alert failed", exc_info=True
        )
        return False


def _install_thread_exception_alerts(cfg: Config) -> None:
    """Page on uncaught background-thread crashes."""
    if getattr(threading.excepthook, "_hightempbot_alert_hook", False):
        return

    previous_hook = threading.excepthook
    logger = logging.getLogger("hightempbot.main")

    def _alerting_excepthook(args) -> None:
        if args.exc_type is SystemExit:
            previous_hook(args)
            return

        thread_name = getattr(args.thread, "name", "unknown")
        logger.critical(
            "Background thread %s crashed",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        _send_operational_alert(
            "CRITICAL: HighTempBot thread crashed",
            (
                f"Thread: {thread_name}\n"
                f"Exception: {_format_exception(args.exc_value)}\n"
                "Action: check logs/hightempbot.log for the full traceback."
            ),
            cfg=cfg,
            stage="thread_crash",
            station_id=thread_name,
        )

        try:
            previous_hook(args)
        except Exception:
            logger.debug("Previous threading.excepthook failed", exc_info=True)

    _alerting_excepthook._hightempbot_alert_hook = True  # type: ignore[attr-defined]
    threading.excepthook = _alerting_excepthook


def _dashboard_bind_host(cfg: Config) -> str:
    """Resolve the dashboard bind host from explicit and legacy config."""
    explicit_host = (getattr(cfg, "dashboard_bind_host", "") or "").strip()
    if explicit_host:
        return explicit_host
    if cfg.dashboard_pass:
        return "0.0.0.0"
    return "127.0.0.1"


def _is_loopback_dashboard_host(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


def _validate_dashboard_live_safety(cfg: Config, *, dry_run: bool, dash_host: str) -> None:
    """Fail closed for unsafe public live dashboards."""
    if dry_run or _is_loopback_dashboard_host(dash_host):
        return
    if not cfg.dashboard_pass:
        raise RuntimeError(
            "LIVE dashboard bound to non-loopback host without DASHBOARD_PASS. "
            "Set DASHBOARD_PASS or bind DASHBOARD_BIND_HOST to 127.0.0.1."
        )
    if not getattr(cfg, "dashboard_tls_terminated", False):
        raise RuntimeError(
            "LIVE dashboard bound to non-loopback host without "
            "DASHBOARD_TLS_TERMINATED=1. Plaintext HTTP exposes the session "
            "cookie; terminate TLS in front of the dashboard or bind "
            "DASHBOARD_BIND_HOST to 127.0.0.1."
        )


def setup_logging(level: str) -> None:
    # Resolve the log path against the current working directory then ensure
    # the parent exists. Doing this here (not relying on `logs/` being a
    # relative path that exists) means launching from a different cwd — eg.
    # systemd from `/` — won't crash on FileHandler open before
    # `cfg.ensure_dirs()` has run.
    #
    # RotatingFileHandler caps disk growth at ~250 MB total (50 MB × 5
    # backups). The previous plain FileHandler grew unbounded — at 50
    # stations × 144 ticks/day the bot would fill a small VPS disk in
    # weeks (perf-006 in the 2026-05-14 review).
    from logging.handlers import RotatingFileHandler

    log_path = (Path.cwd() / "logs" / "hightempbot.log").resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(name)-30s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(
                str(log_path),
                maxBytes=50 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
        ],
    )


def _auto_retrain_missing_calibration(conn: sqlite3.Connection) -> None:
    """Train missing calibration rows and seed/rebuild LUTs immediately."""
    logger = logging.getLogger("hightempbot.main")

    try:
        missing_cal = conn.execute(
            """
            SELECT DISTINCT f.station_id
            FROM forecast_archive f
            JOIN actuals a ON f.station_id = a.station_id AND f.target_date = a.local_date
            WHERE f.station_id NOT IN (SELECT DISTINCT station_id FROM calibration_params)
            GROUP BY f.station_id
            HAVING COUNT(DISTINCT f.target_date) >= 30
            LIMIT 50
            """
        ).fetchall()
    except Exception:
        logger.warning("Auto-retrain check failed (non-fatal)", exc_info=True)
        return

    if not missing_cal:
        return

    from hightempbot.calibration.model import retrain
    from hightempbot.calibration.monthly_retrain import refresh_lut_after_retrain

    for row in missing_cal:
        station_id = row["station_id"]
        try:
            logger.info(
                "Auto-retrain: %s has aligned data but no calibration - training",
                station_id,
            )
            model = retrain(station_id, 1, conn)
            if model is not None and model.is_ready():
                refresh_lut_after_retrain(conn, station_id, "startup")
        except Exception:
            logger.warning("Auto-retrain failed for %s (non-fatal)", station_id, exc_info=True)

    logger.info("Auto-retrain complete: %d stations", len(missing_cal))


def _stop_dry_run_pending_for_live(conn: sqlite3.Connection) -> int:
    """Cancel any PENDING dry_run ledger rows when starting in live mode.

    Live restart is meant to be a clean slate: the simulated book should
    not keep eating capacity or showing up alongside real fills. Settled
    dry_run rows (WIN/LOSS/CLOSED) are kept for historical PnL.
    """
    logger = logging.getLogger("hightempbot.main")
    cursor = conn.execute(
        "UPDATE ledger SET outcome = 'CANCELLED' "
        "WHERE event_type = 'dry_run' AND outcome = 'PENDING'"
    )
    conn.commit()
    n = cursor.rowcount or 0
    if n:
        logger.info("Live-mode startup: cancelled %d pending dry_run row(s)", n)
    return n


def _sync_station_runtime_mode(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Align active station status labels with the global runtime mode."""
    logger = logging.getLogger("hightempbot.main")
    from_status = "LIVE" if dry_run else "DRY_RUN"
    to_status = "DRY_RUN" if dry_run else "LIVE"

    updates: list[tuple] = []
    rows = conn.execute(
        "SELECT icao FROM enrolled_stations WHERE status = ? ORDER BY icao",
        (from_status,),
    ).fetchall()
    for row in rows:
        if dry_run:
            updates.append((to_status, "day_1", "Dry-run started", row["icao"]))
        else:
            updates.append((to_status, "active", "Live trading enabled", row["icao"]))

    if not updates:
        return 0

    conn.executemany(
        "UPDATE enrolled_stations "
        "SET status = ?, step = ?, step_detail = ?, updated_at = datetime('now') "
        "WHERE icao = ?",
        updates,
    )
    conn.commit()
    logger.info("Runtime mode sync: moved %d station(s) from %s to %s", len(updates), from_status, to_status)
    return len(updates)


def _sync_operator_boot_mode(conn: sqlite3.Connection, dry_run: bool) -> None:
    """Persist the final effective boot mode for operator-control checks."""
    logger = logging.getLogger("hightempbot.main")
    from hightempbot.execution.operator_control import get_operator_state

    state = get_operator_state(conn, boot_dry_run=dry_run)
    logger.info(
        "Operator control state: state=%s boot_dry_run=%s",
        state.state,
        state.boot_dry_run,
    )


def main() -> None:
    from hightempbot.runtime_config import set_config
    cfg = Config()
    # Cache the boot config as the process-wide singleton so per-tick callers
    # (scheduler/betting_tick, jobs.py alerts, etc.) reuse it instead of
    # re-reading .env on every fire (ce-code-review P1 #11).
    set_config(cfg)
    cfg.ensure_dirs()
    setup_logging(cfg.log_level)

    logger = logging.getLogger("hightempbot.main")
    _install_thread_exception_alerts(cfg)
    logger.info("Starting HighTempBot")

    # Initialise database
    conn = init_db(cfg.db_path)
    logger.info("Database initialised at %s", cfg.db_path)

    try:
        from hightempbot.persistence.ledger import backfill_fee_adjusted_pnl

        n_fee_adjusted = backfill_fee_adjusted_pnl(conn)
        if n_fee_adjusted > 0:
            logger.info(
                "Fee-adjusted PnL backfill complete: %d ledger row(s) updated",
                n_fee_adjusted,
            )
    except Exception:
        logger.warning("Fee-adjusted PnL backfill failed (non-fatal)", exc_info=True)

    # Phase 2: DRY_RUN mode check
    # Use a local variable because Pydantic BaseSettings is frozen and
    # cfg.dry_run cannot be mutated after construction.
    dry_run = cfg.dry_run

    if not dry_run:
        if not cfg.poly_private_key.get_secret_value():
            logger.error(
                "LIVE TRADING requires POLY_PRIVATE_KEY - forcing DRY_RUN for this process"
            )
            dry_run = True
            from hightempbot.db.connection import log_pipeline_health
            log_pipeline_health(
                conn, None, "live_readiness", "ERROR",
                "POLY_PRIVATE_KEY missing; process forced DRY_RUN",
            )
            _send_operational_alert(
                "WARNING: HighTempBot forced DRY_RUN",
                (
                    "DRY_RUN=False was configured but POLY_PRIVATE_KEY is missing. "
                    "Dashboard and scheduler will start in dry-run mode."
                ),
                cfg=cfg,
                stage="startup_degraded",
            )
        else:
            logger.warning("LIVE TRADING ENABLED - DRY_RUN=False")
            try:
                from hightempbot.execution.live_readiness import (
                    build_live_readiness_report,
                    record_readiness_report,
                )

                readiness = build_live_readiness_report(cfg)
                record_readiness_report(conn, readiness)
                if not readiness.ok:
                    logger.error(
                        "Live readiness failed - starting in forced DRY_RUN mode"
                    )
                    if getattr(cfg, "live_readiness_required", True):
                        dry_run = True
                    _send_operational_alert(
                        "WARNING: HighTempBot forced DRY_RUN",
                        (
                            "Live readiness failed. Live trading is disabled for "
                            "this process; scheduler and dashboard still start.\n"
                            f"Failed checks: "
                            f"{', '.join(c.name for c in readiness.checks if c.status == 'ERROR')}"
                        ),
                        cfg=cfg,
                        stage="startup_degraded",
                    )
                else:
                    logger.info("Live readiness OK")
                    try:
                        from hightempbot.persistence.wallet_reconciliation import (
                            refresh_wallet_snapshot,
                        )

                        refresh_wallet_snapshot(
                            conn,
                            config=cfg,
                        )
                    except Exception:
                        logger.warning("Startup wallet snapshot write failed", exc_info=True)
            except Exception as exc:
                logger.error(
                    "Live readiness crashed - starting in forced DRY_RUN mode",
                    exc_info=True,
                )
                if getattr(cfg, "live_readiness_required", True):
                    dry_run = True
                _send_operational_alert(
                    "WARNING: HighTempBot forced DRY_RUN",
                    (
                        "Live readiness crashed. Live trading is disabled for "
                        "this process; scheduler and dashboard still start.\n"
                        f"Exception: {_format_exception(exc)}"
                    ),
                    cfg=cfg,
                    stage="startup_degraded",
                )
    else:
        logger.info("DRY-RUN mode - signals will be logged but no orders placed")

    # Phase 2: Startup reconciliation (live mode only).
    # Each underlying CLOB call is timeout-bounded inside reconcile_orders,
    # but a large orphan/missing-fill set could still block the main thread
    # for minutes. Run the reconcile pass in a worker thread with an outer
    # deadline so a brownout cannot prevent the bot from booting.
    if not dry_run:
        from concurrent.futures import (
            ThreadPoolExecutor as _ReconcileTPE,
            TimeoutError as _ReconcileTimeoutError,
        )

        from hightempbot.persistence.reconciliation import reconcile_orders

        try:
            abort_event = threading.Event()

            def _run_startup_reconcile():
                from hightempbot.db.connection import get_connection
                from hightempbot.execution.walker import OrderClient

                recon_conn = get_connection(cfg.db_path)
                try:
                    return reconcile_orders(
                        OrderClient(cfg), recon_conn, abort_event=abort_event
                    )
                finally:
                    recon_conn.close()

            _ex = _ReconcileTPE(max_workers=1, thread_name_prefix="boot-recon")
            fut = _ex.submit(_run_startup_reconcile)
            try:
                result = fut.result(timeout=180)
            except _ReconcileTimeoutError:
                logger.error(
                    "Reconciliation deadline (180s) exceeded - signaling worker to abort "
                    "and starting in forced DRY_RUN mode"
                )
                abort_event.set()
                dry_run = True
                result = None
                fut.cancel()
                _ex.shutdown(wait=False, cancel_futures=True)
                _send_operational_alert(
                    "WARNING: HighTempBot forced DRY_RUN",
                    (
                        "Startup reconciliation exceeded 180s. Live trading is "
                        "disabled for this process; scheduler and dashboard still start.\n"
                        "Action: check logs/hightempbot.log and review open CLOB orders."
                    ),
                    cfg=cfg,
                    stage="startup_degraded",
                )
            except Exception:
                _ex.shutdown(wait=True)
                raise
            else:
                _ex.shutdown(wait=True)

            if result is not None:
                if result.failed:
                    logger.error("Reconciliation failed - starting in forced DRY_RUN mode")
                    dry_run = True
                    _send_operational_alert(
                        "WARNING: HighTempBot forced DRY_RUN",
                        (
                            "Startup reconciliation returned failed=True. Live trading is "
                            "disabled for this process; scheduler and dashboard still start.\n"
                            f"matched={result.matched} cancelled={result.cancelled} "
                            f"updated={result.updated}\n"
                            "Action: check logs/hightempbot.log and reconcile orders manually."
                        ),
                        cfg=cfg,
                        stage="startup_degraded",
                    )
                else:
                    logger.info(
                        "Reconciliation complete: matched=%d, cancelled=%d, updated=%d",
                        result.matched,
                        result.cancelled,
                        result.updated,
                    )
                if getattr(result, "aborted", False):
                    logger.warning(
                        "Reconciliation completed partially before abort signal: "
                        "matched=%d cancelled=%d updated=%d — review unprocessed orphans manually",
                        result.matched,
                        result.cancelled,
                        result.updated,
                    )
        except Exception as exc:
            logger.error(
                "Reconciliation crashed - starting in forced DRY_RUN mode",
                exc_info=True,
            )
            _send_operational_alert(
                "WARNING: HighTempBot forced DRY_RUN",
                (
                    "Startup reconciliation crashed. Live trading is disabled for "
                    "this process; scheduler and dashboard still start.\n"
                    f"Exception: {_format_exception(exc)}\n"
                    "Action: check logs/hightempbot.log and reconcile orders manually."
                ),
                cfg=cfg,
                stage="startup_degraded",
            )
            dry_run = True

    # Configure dashboard after live-mode safety checks so the UI reflects
    # the actual runtime mode if startup reconciliation forces dry-run.
    _sync_operator_boot_mode(conn, dry_run)
    configure_dashboard(
        cfg.db_path,
        dry_run,
        cfg.initial_bankroll,
        dashboard_user=cfg.dashboard_user,
        dashboard_pass=cfg.dashboard_pass,
        dashboard_tls_terminated=cfg.dashboard_tls_terminated,
    )

    if not dry_run:
        _stop_dry_run_pending_for_live(conn)

    _sync_station_runtime_mode(conn, dry_run)

    dash_host = _dashboard_bind_host(cfg)
    _validate_dashboard_live_safety(cfg, dry_run=dry_run, dash_host=dash_host)

    # Load all stations from enrolled_stations DB and register in runtime maps
    from hightempbot.stations import get_all_stations, register_enrolled_station

    all_stations = get_all_stations(conn)
    if all_stations:
        logger.info("Loaded %d stations from DB", len(all_stations))
        for station in all_stations.values():
            register_enrolled_station(station)

    # Repair historical resolved rows now that actuals may have arrived after resolution.
    try:
        from hightempbot.persistence.ledger import backfill_all_resolved_actuals

        n_backfilled = backfill_all_resolved_actuals(conn)
        if n_backfilled > 0:
            logger.info(
                "Resolved-actual backfill complete: %d ledger row(s) updated",
                n_backfilled,
            )
    except Exception:
        logger.warning("Resolved-actual backfill failed (non-fatal)", exc_info=True)

    # One-shot migration: convert market_tokens rows written under the old
    # integer-label parser to the new continuous [lo, hi) ranges. Idempotent:
    # post-migration rows have fractional bounds and will not match the predicates.
    try:
        floor_fixed = conn.execute(
            """
            UPDATE market_tokens
            SET bracket_high = bracket_high + 0.5
            WHERE bracket_idx != -1
              AND bracket_low IS NULL
              AND bracket_high IS NOT NULL
              AND bracket_high = CAST(bracket_high AS INTEGER)
            """
        ).rowcount
        ceiling_fixed = conn.execute(
            """
            UPDATE market_tokens
            SET bracket_low = bracket_low - 0.5
            WHERE bracket_idx != -1
              AND bracket_high IS NULL
              AND bracket_low IS NOT NULL
              AND bracket_low = CAST(bracket_low AS INTEGER)
            """
        ).rowcount
        interior_fixed = conn.execute(
            """
            UPDATE market_tokens
            SET bracket_low = bracket_low - 0.5,
                bracket_high = bracket_high + 0.5
            WHERE bracket_idx != -1
              AND bracket_low IS NOT NULL
              AND bracket_high IS NOT NULL
              AND bracket_low = CAST(bracket_low AS INTEGER)
              AND bracket_high = CAST(bracket_high AS INTEGER)
            """
        ).rowcount
        conn.commit()
        total = floor_fixed + ceiling_fixed + interior_fixed
        if total > 0:
            logger.info(
                "Bracket-bounds migration: %d rows updated (floor=%d, ceiling=%d, interior=%d)",
                total,
                floor_fixed,
                ceiling_fixed,
                interior_fixed,
            )
    except Exception:
        logger.warning("Bracket-bounds migration failed (non-fatal)", exc_info=True)

    # Auto-retrain stations that have aligned data but no calibration params.
    # This covers first deployment and data-restoration scenarios where backfill
    # loaded actuals+forecasts but retrain was never triggered.
    _auto_retrain_missing_calibration(conn)

    # Start scheduler with enough worker threads for concurrent station scans.
    from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPool

    scheduler = BackgroundScheduler(
        executors={
            "default": APSThreadPool(max_workers=20),
            "ops": APSThreadPool(max_workers=4),
        },
    )
    schedule_all_jobs(
        scheduler,
        conn,
        Path(cfg.data_dir),
        db_path=cfg.db_path,
        initial_bankroll=cfg.initial_bankroll,
        dry_run=dry_run,
        stations=all_stations,
    )
    scheduler.start()
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    # Start dashboard in a separate thread.
    # bind-host hardening. Resolution order:
    #   1. DASHBOARD_BIND_HOST env (explicit operator choice, including 0.0.0.0).
    #   2. Legacy: DASHBOARD_PASS set => 0.0.0.0 (back-compat for current prod).
    #   3. Default: 127.0.0.1 (SSH-tunnel only).
    # In live mode, public dashboard binds fail closed unless auth and TLS
    # termination are explicitly configured. Dry-run keeps the older warning so
    # local experiments are not blocked by transport setup.
    _is_loopback = _is_loopback_dashboard_host(dash_host)
    _tls_terminated = bool(getattr(cfg, "dashboard_tls_terminated", False))
    if not _is_loopback and not _tls_terminated:
        warn_msg = (
            f"Dashboard bound to non-loopback host {dash_host} without "
            "DASHBOARD_TLS_TERMINATED=1. Plaintext HTTP exposes the session "
            "cookie to passive network observers. Put nginx/Caddy with TLS in "
            "front, restrict the port via firewall, or use an SSH tunnel."
        )
        logger.warning(warn_msg)
        _send_operational_alert(
            "WARNING: HighTempBot dashboard on plaintext HTTP",
            warn_msg,
            cfg=cfg,
            stage="startup_degraded",
        )

    def run_dashboard() -> None:
        import uvicorn

        try:
            uvicorn.run(
                app,
                host=dash_host,
                port=cfg.dashboard_port,
                log_level="warning",
            )
        except Exception:
            # Daemon thread crashes were silent: a port conflict or uvicorn
            # blow-up would leave the bot running with no dashboard and no
            # log entry. Log loudly here so the operator can see it; the
            # main thread continues so the scheduler keeps trading.
            logger.error(
                "Dashboard thread crashed (host=%s port=%d) — bot continues without UI",
                dash_host, cfg.dashboard_port, exc_info=True,
            )
            _send_operational_alert(
                "CRITICAL: HighTempBot dashboard crashed",
                (
                    f"Dashboard thread stopped on {dash_host}:{cfg.dashboard_port}; "
                    "the scheduler continues running without UI.\n"
                    "Action: check logs/hightempbot.log for the uvicorn traceback."
                ),
                cfg=cfg,
                stage="dashboard_crash",
            )

    dashboard_thread = threading.Thread(target=run_dashboard, daemon=True)
    dashboard_thread.start()
    logger.info("Dashboard running at http://%s:%d", dash_host, cfg.dashboard_port)

    # Block until interrupt
    shutdown_event = threading.Event()

    def handle_signal(sig, frame) -> None:
        del sig, frame
        logger.info("Shutdown signal received")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("HighTempBot running. Press Ctrl+C to stop.")
    shutdown_event.wait()

    # Cleanup
    scheduler.shutdown(wait=True)
    conn.close()
    logger.info("HighTempBot stopped")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if exc.code not in (0, None):
            logging.getLogger("hightempbot.main").critical(
                "HighTempBot exited with code %s", exc.code
            )
            _send_operational_alert(
                "CRITICAL: HighTempBot exited during startup",
                (
                    f"Exit code: {exc.code}\n"
                    "Action: check logs/hightempbot.log for the startup failure."
                ),
                stage="process_exit",
            )
        raise
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        logging.getLogger("hightempbot.main").critical(
            "HighTempBot process crashed", exc_info=True
        )
        _send_operational_alert(
            "CRITICAL: HighTempBot process crashed",
            (
                f"Exception: {_format_exception(exc)}\n"
                "Action: check logs/hightempbot.log for the full traceback."
            ),
            stage="process_crash",
        )
        raise
