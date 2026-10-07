"""APScheduler jobs: per-station ticks plus global backfill, retrain and housekeeping."""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytz
import requests
from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from hightempbot.db.connection import get_connection
from hightempbot.execution.strategy_constants import SCAN_INTERVAL_MINUTES, icao_tick_offset
from hightempbot.stations import StationConfig

logger = logging.getLogger(__name__)

PERIODIC_RETRAIN_TIMEOUT_S = 120.0
_CALL_TIMEOUT = object()


def _run_callable_with_timeout(fn, timeout_s: float):
    """Run ``fn`` on a daemon thread and return ``_CALL_TIMEOUT`` on timeout."""
    from queue import Queue

    result_q = Queue(maxsize=1)

    def _target() -> None:
        try:
            result_q.put((True, fn()))
        except BaseException as exc:
            result_q.put((False, exc))

    worker = threading.Thread(
        target=_target,
        name="periodic_retrain_worker",
        daemon=True,
    )
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return _CALL_TIMEOUT

    ok, value = result_q.get_nowait()
    if ok:
        return value
    raise value


def _format_exception(exc: BaseException | None, limit: int = 500) -> str:
    if exc is None:
        return "unknown"
    text = f"{type(exc).__name__}: {exc}".strip()
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 14].rstrip() + " ...[truncated]"
    return text


def _station_from_job_id(job_id: str) -> str:
    for prefix in ("betting_", "resolution_", "midnight_", "tpsl_"):
        if job_id.startswith(prefix):
            return job_id.removeprefix(prefix)
    return ""


def _log_scheduler_failure(db_path: str, station_id: str, message: str) -> None:
    try:
        from hightempbot.db.connection import log_pipeline_health
        health_conn = get_connection(db_path)
        try:
            log_pipeline_health(health_conn, station_id, "scheduler", "ERROR", message[:500])
        finally:
            health_conn.close()
    except Exception:
        logger.debug("Failed to write scheduler pipeline_health row", exc_info=True)


def _alert_scheduler_failure(event, db_path: str) -> None:
    job_id = str(getattr(event, "job_id", "unknown"))
    exc = getattr(event, "exception", None)
    tb = getattr(event, "traceback", None)
    summary = _format_exception(exc)
    station_id = _station_from_job_id(job_id)

    if tb:
        logger.error("Scheduled job %s FAILED: %s\n%s", job_id, summary, tb)
    else:
        logger.error("Scheduled job %s FAILED: %s", job_id, summary)

    _log_scheduler_failure(
        db_path,
        station_id,
        f"{job_id}: {summary}",
    )

    try:
        from hightempbot.runtime_config import get_config
        from hightempbot.execution.notify import send_alert

        send_alert(
            "CRITICAL: scheduled job failed",
            (
                f"Job: {job_id}\n"
                f"Station: {station_id or 'global'}\n"
                f"Exception: {summary}\n"
                "Action: check logs/hightempbot.log for the full traceback."
            ),
            config=get_config(),
            stage=f"scheduler_job_failed:{job_id}",
            station_id=station_id,
        )
    except Exception:
        logger.debug("Failed to send scheduler failure alert", exc_info=True)


def _scan_minute_slots(extra_offset: int = 0) -> list[int]:
    """Minutes of the hour to scan at, e.g. [0, 10, ..., 50] (+ ``extra_offset``)."""
    interval = max(1, int(SCAN_INTERVAL_MINUTES))
    return [(m + extra_offset) % 60 for m in range(0, 60, interval)]


def _run_forecast_backfill(
    db_path: str, start: date, end: date, label: str,
    *, max_workers: int = 1,
) -> None:
    """Open-Meteo backfill for the daily and weekly jobs, optionally sharded
    across workers (one DB connection each; each respects the rate delay)."""
    from hightempbot.ingestion.openmeteo_forecast import backfill_openmeteo
    from hightempbot.stations import get_all_stations

    list_conn = get_connection(db_path)
    try:
        all_stations = get_all_stations(list_conn)
    finally:
        list_conn.close()

    if not all_stations:
        logger.warning("%s backfill: no stations found, skipping", label)
        return

    logger.info(
        "%s backfill: %s to %s for %d stations (workers=%d)",
        label, start, end, len(all_stations), max_workers,
    )

    if max_workers <= 1:
        bf_conn = get_connection(db_path)
        try:
            backfill_openmeteo(start, end, bf_conn, stations=all_stations)
        except Exception:
            logger.error("%s backfill failed", label, exc_info=True)
        finally:
            bf_conn.close()
        logger.info("%s backfill complete", label)
        return

    icaos = sorted(all_stations.keys())
    shards: list[dict[str, StationConfig]] = [
        {} for _ in range(max_workers)
    ]
    for i, icao in enumerate(icaos):
        shards[i % max_workers][icao] = all_stations[icao]

    def _run_shard(shard: dict[str, StationConfig]) -> None:
        if not shard:
            return
        shard_conn = get_connection(db_path)
        try:
            backfill_openmeteo(start, end, shard_conn, stations=shard)
        finally:
            shard_conn.close()

    from concurrent.futures import ThreadPoolExecutor as _BfTPE
    with _BfTPE(
        max_workers=max_workers, thread_name_prefix=f"{label}-bf",
    ) as ex:
        futures = [ex.submit(_run_shard, s) for s in shards]
        for fut in futures:
            try:
                fut.result()
            except Exception:
                logger.error(
                    "%s backfill: shard worker raised", label, exc_info=True,
                )
    logger.info("%s backfill complete", label)


def materialize_actual_tmax_into_ledger(
    conn,
    station_id: str,
    target_date: str,
    tmax_celsius: float,
    source: str,
) -> int:
    """Fill NULL ``actual_tmax`` on resolved rows for this station/date (e.g.
    TP exits that closed before the high was known). Returns rows updated."""
    from hightempbot.stations import SUPPORTED_LIVE_SOURCES

    if source not in SUPPORTED_LIVE_SOURCES:
        return 0

    try:
        cur = conn.execute(
            "UPDATE ledger SET actual_tmax = ? "
            "WHERE station_id = ? AND target_date = ? "
            "AND outcome IN ('WIN','LOSS','PUSH','CLOSED') "
            "AND event_type IN ('bet','dry_run') "
            "AND actual_tmax IS NULL",
            (float(tmax_celsius), station_id, target_date),
        )
        if cur.rowcount > 0:
            conn.commit()
            logger.info(
                "Materialized actual_tmax on %d resolved ledger row(s) for %s %s",
                cur.rowcount, station_id, target_date,
            )
        return cur.rowcount
    except Exception:
        logger.warning(
            "Failed to materialize actual_tmax for %s %s",
            station_id, target_date, exc_info=True,
        )
        return 0


def scrape_actuals_job(station_id: str, db_path: str, data_dir: Path) -> None:
    """Scrape yesterday's actual for a station and retrain if it's new."""
    from hightempbot.ingestion.actuals import fetch_and_store, supports_actual_scrape
    from hightempbot.stations import get_all_stations

    _conn_tmp = get_connection(db_path)
    try:
        all_st = get_all_stations(_conn_tmp)
    finally:
        _conn_tmp.close()
    station = all_st.get(station_id)
    if station is None:
        logger.error("scrape_actuals_job: station %s not found in registry", station_id)
        return
    if not supports_actual_scrape(station):
        logger.info(
            "Skipping midnight actuals job for %s: resolution_source=%s is unsupported",
            station_id,
            station.resolution_source,
        )
        return
    tz = pytz.timezone(station.timezone)
    local_now = datetime.now(tz)
    yesterday = (local_now - timedelta(days=1)).date()

    conn = get_connection(db_path)
    try:
        logger.info("Scraping actuals for %s %s", station_id, yesterday)
        row = fetch_and_store(station_id, yesterday, conn, data_dir, station_override=station)

        if row is not None:
            logger.info("Got actual for %s %s: %.1f°C from %s", station_id, yesterday, row.tmax_celsius, row.source)
            materialize_actual_tmax_into_ledger(
                conn, station_id, yesterday.isoformat(),
                row.tmax_celsius, row.source,
            )
            from hightempbot.calibration.model import retrain
            retrain(station_id, 1, conn)
            # Add the new day to the LUT; the monthly retrain catches up if this can't.
            try:
                from hightempbot.calibration.lut import (
                    append_triples_for_date, rebuild_lut,
                )
                written = append_triples_for_date(
                    conn, station_id, horizon=1, local_date=yesterday.isoformat(),
                )
                if written > 0:
                    rebuild_lut(conn, station_id)
                    logger.info(
                        "Post-actual LUT update %s %s: appended %d triples, rebuilt",
                        station_id, yesterday, written,
                    )
                else:
                    logger.debug(
                        "Post-actual LUT update %s %s: no triples written (missing prerequisites)",
                        station_id, yesterday,
                    )
            except Exception:
                logger.warning(
                    "Post-actual LUT update failed for %s %s", station_id, yesterday,
                    exc_info=True,
                )
        else:
            logger.warning("No actual available for %s %s", station_id, yesterday)

        # Prune old signals for this station (keep 3 days for dashboard)
        n_pruned = conn.execute(
            "DELETE FROM signals WHERE station_id = ? AND created_at < datetime('now', '-3 days')",
            (station_id,),
        ).rowcount
        if n_pruned > 0:
            conn.commit()
            logger.debug("Pruned %d old signals for %s", n_pruned, station_id)
    finally:
        conn.close()



def rolling_actuals_backfill_job(db_path: str, data_dir: Path) -> None:
    """Daily (and at boot): refetch missing actuals from the last 35 days and
    retrain stations that got new data or have fewer than MIN_PAIRS samples.

    Missed midnight scrapes otherwise leave holes that drop stations below
    MIN_PAIRS and silently stop betting.
    """
    from hightempbot.calibration.model import MIN_PAIRS
    from hightempbot.calibration.model import retrain as do_retrain
    from hightempbot.ingestion.actuals import backfill_missing_actuals
    from hightempbot.stations import get_all_stations

    bf_conn = get_connection(db_path)
    try:
        all_st = get_all_stations(bf_conn)
        if not all_st:
            return

        today = date.today()
        start = today - timedelta(days=35)

        per_station: dict[str, int] = {}
        for icao in sorted(all_st.keys()):
            try:
                added = backfill_missing_actuals(
                    bf_conn, all_st[icao], data_dir, start, today,
                )
            except Exception:
                logger.error(
                    "Rolling actuals backfill failed for %s",
                    icao, exc_info=True,
                )
                continue
            if added > 0:
                per_station[icao] = added

        if per_station:
            total = sum(per_station.values())
            logger.info(
                "Rolling actuals backfill: filled %d gaps across %d stations: %s",
                total, len(per_station), per_station,
            )
            from hightempbot.db.connection import log_pipeline_health
            log_pipeline_health(
                bf_conn, "", "actuals_backfill", "OK",
                f"filled {total} gaps across {len(per_station)} stations",
            )
        else:
            logger.info("Rolling actuals backfill: no gaps found")

        latest_n: dict[str, int] = {}
        for r in bf_conn.execute(
            "SELECT cp.station_id, cp.n_samples FROM calibration_params cp "
            "WHERE cp.horizon = 1 AND cp.param_type = 'emos' "
            "AND cp.trained_at = ("
            "  SELECT MAX(trained_at) FROM calibration_params "
            "  WHERE station_id = cp.station_id "
            "  AND horizon = 1 AND param_type = 'emos'"
            ")"
        ):
            latest_n[r["station_id"]] = int(r["n_samples"] or 0)

        targets: set[str] = set(per_station.keys())
        for icao, n in latest_n.items():
            if n < MIN_PAIRS:
                targets.add(icao)

        if not targets:
            return

        logger.info(
            "Rolling actuals backfill: retraining %d station(s): %s",
            len(targets), sorted(targets),
        )
        for icao in sorted(targets):
            if icao not in all_st:
                continue
            rt_conn = get_connection(db_path)
            try:
                model = do_retrain(icao, 1, rt_conn)
            except Exception:
                logger.error(
                    "Post-backfill retrain failed for %s",
                    icao, exc_info=True,
                )
                rt_conn.close()
                continue
            rt_conn.close()
            if model is not None and model.is_ready():
                logger.info(
                    "Post-backfill retrain %s: ready (n=%d)",
                    icao, model.emos_params.n_samples,
                )
            else:
                logger.info(
                    "Post-backfill retrain %s: still not ready",
                    icao,
                )
    finally:
        bf_conn.close()


def _run_tp_sl_monitor_job(station: StationConfig, db_path: str, dry_run: bool) -> None:
    """Run the TP/SL monitor for every strategy with a tp/sl. Never raises.
    ``dry_run`` is the boot mode, shared with the betting pipeline."""
    try:
        from hightempbot.execution.strategy_constants import STRATEGY_CONFIGS
        from hightempbot.execution.tp_sl_monitor import run_tp_sl_monitor

        # Include disabled strategies: they may still hold open positions.
        eligible = [
            name for name, cfg in STRATEGY_CONFIGS.items()
            if cfg.tp is not None or cfg.sl is not None
        ]
        if not eligible:
            return

        order_client = None
        reader = None
        if dry_run:
            try:
                from hightempbot.execution.walker import ClobReader
                reader = ClobReader()
            except Exception:
                logger.warning(
                    "TP/SL dry-run: ClobReader unavailable for %s — monitor is no-op",
                    station.icao, exc_info=True,
                )
                reader = None
        else:
            try:
                from hightempbot.runtime_config import get_config
                from hightempbot.execution.walker import OrderClient
                order_client = OrderClient(get_config())
            except Exception:
                logger.error(
                    "TP/SL live: OrderClient init failed for %s — monitor skipped",
                    station.icao, exc_info=True,
                )
                return

        from hightempbot.scheduler.station_scanner import _notify

        def _tp_sl_notify(title: str, body: str) -> None:
            _notify(title, body, stage="tp_sl", station_id=station.icao)

        local_now_minute = datetime.now(pytz.timezone(station.timezone)).minute
        for strat_name in eligible:
            run_tp_sl_monitor(
                station=station,
                db_path=db_path,
                strategy=strat_name,
                order_client=order_client,
                reader=reader,
                dry_run=dry_run,
                notify=_tp_sl_notify,
                local_now_minute=local_now_minute,
            )
    except Exception:
        logger.error("TP/SL monitor tick crashed for %s", station.icao, exc_info=True)


def _run_auto_redeemer_job(db_path: str, dry_run: bool) -> None:
    """Live-only sweep for Data API redeemable wallet positions."""
    if dry_run:
        return
    try:
        import hashlib

        from hightempbot.runtime_config import get_config
        from hightempbot.execution.notify import send_alert
        from hightempbot.execution.polymarket_redeemer import run_redeemable_scan
        from hightempbot.persistence.wallet_reconciliation import refresh_wallet_snapshot

        cfg = get_config()
        redeem_conn = get_connection(db_path)
        try:
            def _notify_auto_redeem(title: str, body: str) -> bool:
                digest = hashlib.sha1(body.encode("utf-8", errors="replace")).hexdigest()[:12]
                return send_alert(
                    title,
                    body,
                    config=cfg,
                    stage="auto_redeem",
                    station_id=digest,
                )

            result = run_redeemable_scan(
                redeem_conn,
                config=cfg,
                dry_run=dry_run,
                notify=_notify_auto_redeem,
            )
            logger.info("Auto-redeem scan: %s", result.message or result.status)
            changed = (
                result.submitted > 0
                or result.settled_rows > 0
                or result.failed > 0
                or result.skipped_unmatched > 0
            )
            if changed:
                try:
                    refresh_wallet_snapshot(redeem_conn, config=cfg)
                except Exception:
                    logger.warning(
                        "Auto-redeem post-scan wallet snapshot refresh failed",
                        exc_info=True,
                    )
        finally:
            redeem_conn.close()
    except Exception:
        logger.error("Auto-redeemer tick failed", exc_info=True)


def _run_wallet_snapshot_job(db_path: str, dry_run: bool) -> None:
    """Live-only wallet snapshot refresh so Operator stays inside its TTL."""
    if dry_run:
        return
    try:
        from hightempbot.runtime_config import get_config
        from hightempbot.persistence.wallet_reconciliation import refresh_wallet_snapshot

        cfg = get_config()
        conn = get_connection(db_path)
        try:
            snapshot = refresh_wallet_snapshot(conn, config=cfg)
            logger.info(
                "Wallet snapshot refresh: status=%s matched=%d mismatched=%d warnings=%d",
                snapshot.source_status,
                snapshot.data_api_matched_positions_count,
                snapshot.data_api_mismatched_positions_count,
                len(snapshot.warnings),
            )
        finally:
            conn.close()
    except Exception:
        logger.error("Wallet snapshot refresh tick failed", exc_info=True)


def schedule_all_jobs(
    scheduler: BackgroundScheduler,
    conn: sqlite3.Connection,
    data_dir: Path,
    db_path: str = "data/hightempbot.db",
    initial_bankroll: float = 100.0,
    dry_run: bool = True,
    stations: dict[str, StationConfig] | None = None,
) -> None:
    """Register all jobs.

    Per station: actuals scrape (00:05 local), betting and resolution ticks
    (every SCAN_INTERVAL_MINUTES), TP/SL monitor (offset +5).
    Global: forecast/actuals backfills, retrains, housekeeping, enrollment
    scan, health check, and live-only reconcile/wallet/redeem jobs.
    """
    if stations is None:
        logger.warning("schedule_all_jobs called without stations - no per-station jobs")
        stations = {}

    from hightempbot.ingestion.actuals import supports_actual_scrape
    from hightempbot.scheduler.betting_tick import run_betting_tick
    from hightempbot.resolution.settler import run_resolution_tick

    if not getattr(scheduler, "_hightempbot_error_listener_registered", False):
        def _job_error_listener(event):
            _alert_scheduler_failure(event, db_path)

        scheduler.add_listener(_job_error_listener, EVENT_JOB_ERROR)
        setattr(scheduler, "_hightempbot_error_listener_registered", True)

    for job in scheduler.get_jobs():
        job.remove()

    def _schedule_station_jobs(station: StationConfig) -> None:
        icao = station.icao

        # Fail fast on a bad timezone instead of silently at job run time.
        try:
            pytz.timezone(station.timezone)
        except pytz.UnknownTimeZoneError:
            logger.error(
                "Skipping all jobs for %s: invalid timezone %r — fix stations config and restart",
                icao,
                station.timezone,
            )
            return

        if not supports_actual_scrape(station):
            logger.info(
                "Skipping scheduled midnight actuals for %s: resolution_source=%s is unsupported",
                icao,
                station.resolution_source,
            )
        else:
            scheduler.add_job(
                scrape_actuals_job,
                CronTrigger(hour=0, minute=5, timezone=station.timezone),
                args=[icao, db_path, data_dir],
                id=f"midnight_{icao}",
                replace_existing=True,
                misfire_grace_time=3600,
                name=f"Midnight actuals: {icao} ({station.city})",
                max_instances=1,
            )

        # Spread stations across minutes so they don't all hit the APIs at :00.
        offset = icao_tick_offset(icao)

        def _slots() -> str:
            return ",".join(
                str((m + offset) % 60) for m in _scan_minute_slots()
            )

        def _slots_offset(extra: int) -> str:
            return ",".join(
                str((m + offset) % 60)
                for m in _scan_minute_slots(extra_offset=extra)
            )

        if not supports_actual_scrape(station):
            logger.info(
                "Skipping betting scan for %s: resolution_source=%s is unsupported",
                icao,
                station.resolution_source,
            )
        else:
            scheduler.add_job(
                run_betting_tick,
                CronTrigger(minute=_slots(), timezone=station.timezone),
                args=[station, db_path, initial_bankroll, dry_run],
                id=f"betting_{icao}",
                replace_existing=True,
                misfire_grace_time=900,
                name=f"Betting scan: {icao} ({station.city})",
                max_instances=1,
            )

        scheduler.add_job(
            run_resolution_tick,
            CronTrigger(minute=_slots(), timezone=station.timezone),
            args=[station, db_path],
            id=f"resolution_{icao}",
            replace_existing=True,
            misfire_grace_time=900,
            name=f"Resolution scan: {icao} ({station.city})",
            max_instances=1,
        )

        # TP/SL monitor runs 5 minutes after the betting tick.
        scheduler.add_job(
            _run_tp_sl_monitor_job,
            CronTrigger(
                minute=_slots_offset(max(1, int(SCAN_INTERVAL_MINUTES) // 2)),
                timezone=station.timezone,
            ),
            args=[station, db_path, dry_run],
            id=f"tpsl_{icao}",
            replace_existing=True,
            misfire_grace_time=600,
            name=f"TP/SL monitor: {icao} ({station.city})",
            max_instances=1,
        )


    for station in stations.values():
        _schedule_station_jobs(station)

    # --- Monthly housekeeping (1st, 02:00 UTC): expire stuck PENDING, prune tables ---
    def _monthly_housekeeping_job():
        from hightempbot.persistence.ledger import (
            expire_stuck_pending, prune_book_snapshots,
            prune_market_tokens, prune_pipeline_health,
        )
        hk_conn = get_connection(db_path)
        # Live expiry must CLOB-verify each order; skip it if no client can be built.
        hk_client = None
        can_expire_pending = True
        if not dry_run:
            try:
                from hightempbot.runtime_config import get_config
                from hightempbot.execution.walker import OrderClient
                hk_client = OrderClient(get_config())
            except Exception:
                can_expire_pending = False
                logger.error(
                    "Failed to build OrderClient for housekeeping CLOB verify; "
                    "skipping stuck-PENDING expiry",
                    exc_info=True,
                )
                try:
                    from hightempbot.db.connection import log_pipeline_health
                    log_pipeline_health(
                        hk_conn,
                        "",
                        "expire",
                        "ERROR",
                        "Live housekeeping skipped stuck-PENDING expiry: OrderClient unavailable",
                    )
                except Exception:
                    logger.debug("Failed to log housekeeping expiry skip", exc_info=True)
        try:
            n_expired = (
                expire_stuck_pending(hk_conn, order_client=hk_client)
                if can_expire_pending else 0
            )
            n_health = prune_pipeline_health(hk_conn)
            n_tokens = prune_market_tokens(hk_conn)
            n_snapshots = prune_book_snapshots(hk_conn)
            try:
                from hightempbot.execution.capital import prune_bankroll_peak
                from hightempbot.execution.operator_control import prune_audit_tables

                n_peak = prune_bankroll_peak(hk_conn)
                audit_counts = prune_audit_tables(hk_conn)
                audit_total = sum(audit_counts.values())
            except Exception:
                n_peak = 0
                audit_total = 0
                logger.warning("Monthly audit-table prune failed", exc_info=True)
            if (
                n_expired > 0 or n_health > 0 or n_tokens > 0
                or n_snapshots > 0 or n_peak > 0 or audit_total > 0
            ):
                logger.info(
                    "Monthly cleanup: expired %d stuck bets, pruned %d health, "
                    "%d market_tokens, %d book_snapshots, %d bankroll_peak, "
                    "%d audit rows",
                    n_expired, n_health, n_tokens, n_snapshots, n_peak, audit_total,
                )
        finally:
            hk_conn.close()

    scheduler.add_job(
        _monthly_housekeeping_job,
        CronTrigger(day=1, hour=2, minute=0, timezone="UTC"),
        id="monthly_housekeeping",
        replace_existing=True,
        misfire_grace_time=7200,
        name="Monthly DB housekeeping",
    )

    # --- Monthly historical retrain (1st, 03:00 UTC) ---
    def _monthly_historical_retrain_job():
        from hightempbot.calibration.monthly_retrain import run_monthly_retrain
        rt_conn = get_connection(db_path)
        try:
            run_monthly_retrain(rt_conn, db_path)
        finally:
            rt_conn.close()

    scheduler.add_job(
        _monthly_historical_retrain_job,
        CronTrigger(day=1, hour=3, minute=0, timezone="UTC"),
        id="monthly_historical_retrain",
        replace_existing=True,
        misfire_grace_time=86400,
        name="Monthly historical retrain",
        max_instances=1,
    )

    # --- Weekly forecast backfill (Sunday 05:00 UTC); patches missed daily runs ---
    def _weekly_forecast_backfill_job():
        week_end = date.today() - timedelta(days=1)
        week_start = week_end - timedelta(days=6)
        _run_forecast_backfill(db_path, week_start, week_end, "Weekly forecast")

    scheduler.add_job(
        _weekly_forecast_backfill_job,
        CronTrigger(day_of_week="sun", hour=5, minute=0, timezone="UTC"),
        id="weekly_forecast_backfill",
        replace_existing=True,
        misfire_grace_time=86400,
        name="Weekly forecast backfill",
        max_instances=1,
    )

    # --- Daily forecast backfill (00:30 UTC), yesterday only ---
    def _daily_forecast_backfill_job():
        yesterday = date.today() - timedelta(days=1)
        _run_forecast_backfill(
            db_path, yesterday, yesterday, "Daily forecast",
            max_workers=4,
        )

    scheduler.add_job(
        _daily_forecast_backfill_job,
        CronTrigger(hour=0, minute=30, timezone="UTC"),
        id="daily_forecast_backfill",
        replace_existing=True,
        misfire_grace_time=3600,
        name="Daily forecast backfill",
        max_instances=1,
    )

    # --- Daily rolling actuals backfill (00:15 UTC) ---
    scheduler.add_job(
        rolling_actuals_backfill_job,
        CronTrigger(hour=0, minute=15, timezone="UTC"),
        args=[db_path, data_dir],
        id="rolling_actuals_backfill",
        replace_existing=True,
        misfire_grace_time=7200,
        name="Rolling actuals backfill (35d gap fill + retrain)",
        max_instances=1,
    )

    # --- Periodic retrain (daily; stations older than 30 days, up to 3 failed tries) ---
    def _periodic_retrain_job():
        """Retrain stations whose calibration is over 30 days old."""
        from hightempbot.calibration.model import retrain as do_retrain
        from hightempbot.stations import get_all_stations

        def _retrain_in_thread(sid: str):
            _conn = get_connection(db_path)
            try:
                return do_retrain(sid, 1, _conn)
            finally:
                _conn.close()

        rt_conn = get_connection(db_path)
        try:
            from hightempbot.calibration.lut import stamp_refreshed
            from hightempbot.execution.strategy_constants import MIN_COVERAGE_PCT, REF_START_DATE
            ref_start_date = date.fromisoformat(REF_START_DATE)
            expected_days = (date.today() - ref_start_date).days + 1

            all_st = get_all_stations(rt_conn)

            actuals_count: dict[str, int] = {
                r["station_id"]: int(r["n"] or 0)
                for r in rt_conn.execute(
                    "SELECT station_id, COUNT(*) AS n FROM actuals "
                    "WHERE local_date >= ? GROUP BY station_id",
                    (REF_START_DATE,),
                ).fetchall()
            }
            last_trained_by: dict[str, str | None] = {
                r["station_id"]: r["last_t"]
                for r in rt_conn.execute(
                    "SELECT station_id, MAX(trained_at) AS last_t "
                    "FROM calibration_params WHERE horizon = 1 "
                    "GROUP BY station_id"
                ).fetchall()
            }
            recent_fail_count: dict[str, int] = {
                r["station_id"]: int(r["c"] or 0)
                for r in rt_conn.execute(
                    "SELECT station_id, COUNT(*) AS c FROM pipeline_health "
                    "WHERE stage = 'retrain' AND status = 'ERROR' "
                    "AND created_at >= datetime('now', '-3 days') "
                    "GROUP BY station_id"
                ).fetchall()
            }

            for icao in sorted(all_st.keys()):
                # Touch refreshed_at so quiet periods don't trip the stale-LUT gate.
                try:
                    stamp_refreshed(rt_conn, icao)
                except Exception:
                    logger.warning("stamp_refreshed failed for %s", icao, exc_info=True)
                if expected_days > 0:
                    actuals_n = actuals_count.get(icao, 0)
                    if actuals_n / expected_days < MIN_COVERAGE_PCT:
                        continue

                last_trained = last_trained_by.get(icao)

                if last_trained:
                    from datetime import datetime as _dt3, timezone as _tz3
                    try:
                        parsed = _dt3.fromisoformat(last_trained)
                        aware = parsed.replace(tzinfo=_tz3.utc) if parsed.tzinfo is None else parsed.astimezone(_tz3.utc)
                        age_days = (datetime.now(_tz3.utc) - aware).days
                    except Exception:
                        age_days = 999
                else:
                    age_days = 999  # never trained

                if age_days < 30:
                    continue  # fresh enough

                recent_fails = recent_fail_count.get(icao, 0)

                if recent_fails >= 3:
                    continue  # exhausted retries, wait for next 30-day cycle

                from hightempbot.db.connection import log_pipeline_health
                try:
                    model = _run_callable_with_timeout(
                        lambda sid=icao: _retrain_in_thread(sid),
                        PERIODIC_RETRAIN_TIMEOUT_S,
                    )
                    if model is _CALL_TIMEOUT:
                        log_pipeline_health(
                            rt_conn, icao, "retrain", "ERROR",
                            f"Timeout after {PERIODIC_RETRAIN_TIMEOUT_S:g}s "
                            f"(attempt {recent_fails + 1}/3)",
                        )
                        logger.warning(
                            "Periodic retrain %s: timeout %.1fs",
                            icao,
                            PERIODIC_RETRAIN_TIMEOUT_S,
                        )
                        continue
                    if model is not None and model.is_ready():
                        log_pipeline_health(
                            rt_conn, icao, "retrain", "OK",
                            f"Retrained (was {age_days}d stale)",
                        )
                        logger.info("Periodic retrain %s: OK (was %dd stale)", icao, age_days)
                    else:
                        log_pipeline_health(
                            rt_conn, icao, "retrain", "ERROR",
                            f"Retrain returned None (attempt {recent_fails + 1}/3, {age_days}d stale)",
                        )
                        logger.warning("Periodic retrain %s: failed attempt %d/3 (%dd stale)", icao, recent_fails + 1, age_days)
                except Exception as e:
                    log_pipeline_health(
                        rt_conn, icao, "retrain", "ERROR",
                        f"Exception: {str(e)[:100]} (attempt {recent_fails + 1}/3)",
                    )
                    logger.error("Periodic retrain %s: error attempt %d/3", icao, recent_fails + 1, exc_info=True)
        finally:
            rt_conn.close()

    scheduler.add_job(
        _periodic_retrain_job,
        CronTrigger(hour=4, minute=0, timezone="UTC"),
        id="periodic_retrain",
        replace_existing=True,
        misfire_grace_time=7200,
        name=f"Periodic retrain (30-day check, {PERIODIC_RETRAIN_TIMEOUT_S:g}s timeout/station)",
        max_instances=1,
    )

    # --- Enrollment scan (00:30/06:30/12:30/18:30 UTC) ---
    def _enrollment_scan_job():
        """Enroll new cities found in the top 100 Gamma markets by volume."""
        from hightempbot.ingestion.polymarket_prices import GAMMA_API, _CITY_TO_ICAO, gamma_event_slug
        from hightempbot.stations import _city_to_slug

        # --- Scan Gamma for temperature markets (top 100 by volume) ---
        all_cities: set[str] = set()
        for offset in range(0, 100, 100):
            try:
                resp = requests.get(f"{GAMMA_API}/markets", params={
                    "closed": "false", "limit": 100, "offset": offset,
                    "order": "volume", "ascending": "false",
                }, timeout=20)
                if resp.status_code != 200 or not resp.json():
                    break
                for m in resp.json():
                    q = m.get("question", "")
                    if "highest temperature" not in q.lower():
                        continue
                    city_match = re.search(r"in (.+?) be ", q)
                    if city_match:
                        all_cities.add(city_match.group(1).strip())
                time.sleep(0.1)
            except Exception:
                break

        if not all_cities:
            return

        # --- Find cities not yet enrolled ---
        enroll_conn = get_connection(db_path)
        try:
            from hightempbot.enrollment.pipeline import (
                should_probe_skipped_station,
                should_retry_skipped_station,
            )

            existing_rows = enroll_conn.execute(
                "SELECT city, status, skip_reason, coverage_pct, resolution_source "
                "FROM enrolled_stations"
            ).fetchall()
            existing = {row["city"] for row in existing_rows}
            retryable_cities = {
                row["city"]
                for row in existing_rows
                if should_retry_skipped_station(row)
            }
            probe_cities = {
                row["city"]
                for row in existing_rows
                if should_probe_skipped_station(row)
            }
            # Also exclude cities already mapped to an ICAO
            known_slugs = set(_CITY_TO_ICAO.keys())
            new_cities = {
                c for c in all_cities
                if (c not in existing or c in retryable_cities or c in probe_cities)
                and _city_to_slug(c) not in known_slugs
            }

            if not new_cities:
                return

            if len(new_cities) > 5:
                logger.warning(
                    "Enrollment: %d new cities detected at once — possible API format change: %s",
                    len(new_cities), sorted(new_cities),
                )

            logger.info("Enrollment: %d new cities to process: %s", len(new_cities), sorted(new_cities))

            from hightempbot.enrollment.pipeline import enroll_station

            for city_name in sorted(new_cities):
                slug_city = _city_to_slug(city_name)
                event = None

                for days_ahead in (1, 2, 3):
                    target = date.today() + timedelta(days=days_ahead)
                    slug = gamma_event_slug(slug_city, target)

                    try:
                        resp = requests.get(
                            f"{GAMMA_API}/events",
                            params={"slug": slug},
                            timeout=15,
                        )
                        if resp.status_code == 200 and resp.json():
                            event_data = resp.json()
                            event = event_data[0] if isinstance(event_data, list) else event_data
                            break
                    except Exception:
                        continue

                if event is None:
                    logger.warning("Cannot fetch event for %s (tried +1/+2/+3 days)", city_name)
                    continue

                try:
                    status = enroll_station(
                        city_name,
                        event,
                        enroll_conn,
                        db_path,
                        market_date=target,
                        runtime_dry_run=dry_run,
                    )
                    logger.info("Enrollment result for %s: %s", city_name, status)
                    if status in ("DRY_RUN", "LIVE"):
                        from hightempbot.stations import get_all_stations

                        station_row = enroll_conn.execute(
                            "SELECT icao FROM enrolled_stations "
                            "WHERE city = ? AND status IN ('DRY_RUN', 'LIVE') "
                            "ORDER BY rowid DESC LIMIT 1",
                            (city_name,),
                        ).fetchone()
                        if station_row is not None:
                            enrolled_station = get_all_stations(enroll_conn).get(station_row["icao"])
                            if enrolled_station is not None:
                                _schedule_station_jobs(enrolled_station)
                except Exception:
                    logger.error("Enrollment failed for %s", city_name, exc_info=True)

        finally:
            enroll_conn.close()

    scheduler.add_job(
        _enrollment_scan_job,
        CronTrigger(hour="0,6,12,18", minute=30, timezone="UTC"),
        id="enrollment_scan",
        replace_existing=True,
        misfire_grace_time=7200,
        name="Auto-enrollment scan",
        max_instances=1,
    )

    # --- Periodic reconciler (live only): sync CLOB state into the ledger ---
    if not dry_run:
        from hightempbot.execution.strategy_constants import RECONCILE_INTERVAL_MINUTES
        from hightempbot.runtime_config import get_config

        def _periodic_reconciler_job():
            try:
                from hightempbot.execution.walker import OrderClient
                from hightempbot.persistence.reconciliation import reconcile_orders

                recon_conn = get_connection(db_path)
                try:
                    return reconcile_orders(OrderClient(get_config()), recon_conn)
                finally:
                    recon_conn.close()
            except Exception:
                logger.error("Periodic reconciler tick failed", exc_info=True)
                return None

        scheduler.add_job(
            _periodic_reconciler_job,
            IntervalTrigger(minutes=max(1, RECONCILE_INTERVAL_MINUTES)),
            id="periodic_reconciler",
            replace_existing=True,
            misfire_grace_time=300,
            name=f"Periodic reconciler (every {RECONCILE_INTERVAL_MINUTES} min)",
            max_instances=1,
        )

        try:
            cfg = get_config()
            auto_redeem_enabled = bool(getattr(cfg, "auto_redeem_enabled", True))
            auto_redeem_interval = max(1, int(getattr(cfg, "auto_redeem_interval_minutes", 10) or 10))
            wallet_snapshot_interval = max(
                1,
                int(getattr(cfg, "wallet_snapshot_interval_minutes", 10) or 10),
            )
        except Exception:
            auto_redeem_enabled = True
            auto_redeem_interval = 10
            wallet_snapshot_interval = 10
        scheduler.add_job(
            _run_wallet_snapshot_job,
            IntervalTrigger(minutes=wallet_snapshot_interval),
            args=[db_path, dry_run],
            id="periodic_wallet_snapshot",
            replace_existing=True,
            next_run_time=datetime.now(pytz.utc),
            misfire_grace_time=900,
            name=f"Periodic wallet snapshot (every {wallet_snapshot_interval} min)",
            max_instances=1,
            executor="ops",
            coalesce=True,
        )
        if auto_redeem_enabled:
            scheduler.add_job(
                _run_auto_redeemer_job,
                IntervalTrigger(minutes=auto_redeem_interval),
                args=[db_path, dry_run],
                id="periodic_redeemer",
                replace_existing=True,
                next_run_time=datetime.now(pytz.utc),
                misfire_grace_time=900,
                name=f"Periodic auto-redeemer (every {auto_redeem_interval} min)",
                max_instances=1,
                executor="ops",
                coalesce=True,
            )

    # --- System health check (every 15 min) ---
    def _system_health_check():
        """Alert when every station errored in 15 min, no forecasts were stored
        in 2h, or too many stations have unhealthy calibration."""
        hc_conn = get_connection(db_path)
        try:
            # 1. All ticks failing in last 15 min
            errors_15m = hc_conn.execute(
                "SELECT COUNT(DISTINCT station_id) FROM pipeline_health "
                "WHERE status = 'ERROR' AND created_at >= datetime('now', '-15 minutes')"
            ).fetchone()[0]

            scans_15m = hc_conn.execute(
                "SELECT COUNT(DISTINCT station_id) FROM pipeline_health "
                "WHERE stage = 'scan' AND status = 'OK' "
                "AND created_at >= datetime('now', '-15 minutes')"
            ).fetchone()[0]

            if scans_15m > 3 and errors_15m >= scans_15m:
                try:
                    from hightempbot.runtime_config import get_config
                    from hightempbot.execution.notify import send_alert
                    send_alert(
                        f"CRITICAL: All {errors_15m} station ticks failing",
                        "Check logs: grep ERROR hightempbot.log | tail -20",
                        config=get_config(),
                        stage="system_health",
                    )
                except Exception:
                    logger.error("Failed to send system_health alert", exc_info=True)
                logger.critical("SYSTEM HEALTH: all %d scanned stations errored in last 15m", errors_15m)

            # 2. No forecasts in 2 hours, counting only ticks that got past the gates
            from hightempbot.persistence.pipeline_health import (
                forecast_activity_counts,
                forecast_stall_detected,
            )

            forecast_counts = forecast_activity_counts(hc_conn)
            forecasts_2h = forecast_counts["forecast_ok"]

            active = hc_conn.execute(
                "SELECT COUNT(*) FROM enrolled_stations WHERE status IN ('DRY_RUN', 'LIVE')"
            ).fetchone()[0]

            if active > 5 and forecast_stall_detected(forecast_counts):
                try:
                    from hightempbot.runtime_config import get_config
                    from hightempbot.execution.notify import send_alert
                    send_alert(
                        "WARNING: 0 forecasts in 2h",
                        (
                            f"{active} stations enrolled; "
                            f"forecast_attempts={forecast_counts['forecast_attempts']} "
                            f"upstream_ok={forecast_counts['forecast_upstream_ok']} "
                            "but no forecast OK in pipeline_health"
                        ),
                        config=get_config(),
                        stage="forecast_stall",
                    )
                except Exception:
                    logger.error("Failed to send forecast_stall alert", exc_info=True)
                logger.warning(
                    "SYSTEM HEALTH: 0 forecasts in 2h with %d active stations "
                    "(forecast_attempts=%d upstream_ok=%d)",
                    active,
                    forecast_counts["forecast_attempts"],
                    forecast_counts["forecast_upstream_ok"],
                )

            # 3. Too few samples or stale actuals (invisible to checks 1 and 2)
            try:
                from hightempbot.calibration.model import MIN_PAIRS
                from hightempbot.execution.strategy_constants import (
                    ACTUALS_STALE_DAYS,
                    CALIBRATION_HEALTH_ALERT_THRESHOLD,
                )

                latest_n: dict[str, int] = {
                    r["station_id"]: int(r["n_samples"] or 0)
                    for r in hc_conn.execute(
                        "SELECT cp.station_id, cp.n_samples FROM calibration_params cp "
                        "WHERE cp.horizon = 1 AND cp.param_type = 'emos' "
                        "AND cp.trained_at = ("
                        "  SELECT MAX(trained_at) FROM calibration_params "
                        "  WHERE station_id = cp.station_id "
                        "  AND horizon = 1 AND param_type = 'emos'"
                        ")"
                    )
                }
                latest_actual_by: dict[str, str | None] = {
                    r["station_id"]: r["latest"]
                    for r in hc_conn.execute(
                        "SELECT station_id, MAX(local_date) AS latest "
                        "FROM actuals GROUP BY station_id"
                    )
                }
                active_icaos = [
                    r["icao"] for r in hc_conn.execute(
                        "SELECT icao FROM enrolled_stations "
                        "WHERE status IN ('DRY_RUN', 'LIVE')"
                    )
                ]

                today_d = date.today()
                unhealthy: list[str] = []
                for icao in active_icaos:
                    n = latest_n.get(icao, 0)
                    latest = latest_actual_by.get(icao)
                    reasons: list[str] = []
                    if n < MIN_PAIRS:
                        reasons.append(f"n={n}")
                    if latest is None:
                        reasons.append("no actuals")
                    else:
                        try:
                            age = (today_d - date.fromisoformat(latest)).days
                        except Exception:
                            age = 999
                        if age > ACTUALS_STALE_DAYS:
                            reasons.append(f"actuals {age}d stale")
                    if reasons:
                        unhealthy.append(f"{icao} ({', '.join(reasons)})")

                if len(unhealthy) >= CALIBRATION_HEALTH_ALERT_THRESHOLD:
                    try:
                        from hightempbot.runtime_config import get_config
                        from hightempbot.execution.notify import send_alert
                        body = (
                            f"{len(unhealthy)} active stations have "
                            f"n<{MIN_PAIRS} or actuals >{ACTUALS_STALE_DAYS}d stale.\n"
                            "Auto-heal pipeline may be degraded.\n"
                            + "\n".join(unhealthy[:10])
                            + (
                                f"\n... +{len(unhealthy) - 10} more"
                                if len(unhealthy) > 10 else ""
                            )
                        )
                        send_alert(
                            f"WARNING: {len(unhealthy)} stations unhealthy",
                            body,
                            config=get_config(),
                            stage="calibration_unhealthy",
                        )
                    except Exception:
                        logger.error(
                            "Failed to send calibration_unhealthy alert",
                            exc_info=True,
                        )
                    logger.warning(
                        "SYSTEM HEALTH: %d unhealthy stations: %s",
                        len(unhealthy), unhealthy[:10],
                    )
            except Exception:
                logger.error(
                    "Calibration health probe failed", exc_info=True,
                )
        except Exception:
            logger.error("System health check failed", exc_info=True)
        finally:
            hc_conn.close()

    scheduler.add_job(
        _system_health_check,
        CronTrigger(minute="5,20,35,50", timezone="UTC"),
        id="system_health_check",
        replace_existing=True,
        misfire_grace_time=300,
        name="System health check",
        max_instances=1,
    )

    n_stations = len(stations)
    logger.info(
        "Scheduled %d midnight + %d betting + %d resolution + 1 BSS + 1 hist_retrain + 1 daily_retrain + 1 weekly_backfill + 1 enrollment + 1 health = %d total",
        n_stations, n_stations, n_stations, n_stations * 3 + 6,
    )

    # Also run the actuals backfill once at boot, in the background.
    threading.Thread(
        target=rolling_actuals_backfill_job,
        args=(db_path, data_dir),
        name="rolling_actuals_backfill_boot",
        daemon=True,
    ).start()
