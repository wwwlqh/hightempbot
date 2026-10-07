"""APScheduler job definitions for per-station scans and monthly jobs.

Per-station jobs: midnight actuals, betting scan, and resolution scan.
Monthly jobs: DB housekeeping, historical retrain, and cleanup.
"""

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
    """Return the per-cycle minute offsets derived from ``SCAN_INTERVAL_MINUTES``.

    ``SCAN_INTERVAL_MINUTES`` is the canonical scanner cadence. With the
    default (10 min) this returns ``[0, 10, 20, 30, 40, 50]`` (or
    ``[5, 15, ...]`` when ``extra_offset`` is 5). Lifting the slot list out
    of jobs.py's hardcoded literals lets a future operator change cadence
    via the config knob without editing scheduler internals.
    """
    interval = max(1, int(SCAN_INTERVAL_MINUTES))
    return [(m + extra_offset) % 60 for m in range(0, 60, interval)]


def _run_forecast_backfill(
    db_path: str, start: date, end: date, label: str,
    *, max_workers: int = 1,
) -> None:
    """Shared backfill driver for the weekly + daily Open-Meteo cron jobs.

    Opens its own DB connection (per worker when ``max_workers > 1``).
    With ``max_workers > 1`` the active station map is sharded across
    worker threads, each holding its own connection — SQLite WAL handles
    concurrent writes safely, and Open-Meteo's per-IP rate limit is
    respected because each worker still observes the
    ``ingestion.openmeteo_forecast._RATE_DELAY`` between its own requests.
    """
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

    # Shard the station map deterministically so each worker owns a
    # disjoint slice; one DB connection per worker keeps SQLite WAL
    # contention bounded.
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
    """Retroactively write ``actual_tmax`` onto any resolved ledger row for the
    given (station, target_date) that's still NULL.

    Closes the asymmetry between the resolution-time write path (settler.py,
    fires for WIN/LOSS/PUSH at resolution time) and CLOSED rows from TAIL
    TP-exit, which close intraday before the daily high is observable. The
    dashboard's query-time COALESCE already covers this at display time, but
    materializing the value into the row keeps the ledger auditable for
    downstream consumers that don't COALESCE.

    Gated on the source being a supported live source (mirrors the defense
    in ``resolution/settler.py`` finding #25). Returns the number of rows
    updated (0 if no eligible rows).
    """
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
    """Scrape yesterday's actual for a station, retrain calibration if new data.

    Each job opens its own DB connection (thread-safe with WAL mode).
    """
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
            # Incremental LUT update: append the newly-resolved day's triple
            # using walk-forward EMOS params valid AS OF that date, then
            # re-aggregate Wilson bounds. Skipped silently if prerequisites
            # (brackets / params) are missing — monthly_retrain's seed path
            # will catch up later.
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
    """Daily self-heal: backfill missing actuals + force-retrain weak models.

    Per-station midnight scrapes can fail silently (WU rate-limit, network,
    bot down). Without recovery the actuals table accumulates holes that
    silently disqualify EMOS retrain once the rolling window slides over
    them — ``n_samples < MIN_PAIRS`` makes ``CalibrationModel.is_ready()``
    return False and the betting pipeline exits before evaluating any
    bracket, producing the "No signals evaluated" pattern in pipeline_health
    with zero new bets.

    This job runs once per UTC day plus once at every bot startup:
      1. For every enrolled station, fetch any missing actuals in the
         last 35 days (rolling window + buffer for late resolutions).
      2. Force-retrain stations whose latest stored EMOS ``n_samples`` is
         below MIN_PAIRS, or which just received new actuals from (1).

    Logs to pipeline_health stage='actuals_backfill' so the dashboard
    reflects gap-filling activity.
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

        # Force-retrain stations that are either (a) below the MIN_PAIRS
        # readiness floor in their latest stored params, or (b) freshly
        # backfilled. Use a correlated subquery to pick the row with the
        # latest trained_at per station rather than a GROUP BY (which
        # collapses n_samples ambiguously).
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
    """Wrapper around tp_sl_monitor.run_tp_sl_monitor for APScheduler.

    Builds the live OrderClient (or its read-only ClobReader subclass) per the
    dry_run flag, then invokes the monitor for every strategy that has a
    non-None tp/sl (currently YMID and TAIL). Exceptions are logged but never
    re-raised so a single bad tick doesn't kill the scheduler.

    The schedule-time ``dry_run`` argument is the boot mode shared by the
    betting pipeline. Keep it boot-scoped: a mid-process ``.env`` edit must
    not make TP/SL switch modes independently of the scanner/reconciliation
    safety path.
    """
    try:
        from hightempbot.execution.strategy_constants import STRATEGY_CONFIGS
        from hightempbot.execution.tp_sl_monitor import run_tp_sl_monitor

        # Include disabled strategies too: a strategy can be turned off but
        # still hold open PENDING rows from before the disable; those need
        # the monitor to honor their tp/sl until they close or resolve.
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

        # Wire push notifications so TP/SL fires + orphan-close events
        # surface to the operator instead of going silent (ce-review
        # reliability rel-003). _notify takes more positional args than
        # tp_sl_monitor's `notify(title, body)` contract — wrap it.
        from hightempbot.scheduler.station_scanner import _notify

        def _tp_sl_notify(title: str, body: str) -> None:
            _notify(title, body, stage="tp_sl", station_id=station.icao)

        # Station-local minute for the hourly-first-tick gate. Matches the
        # betting scanner's behavior; mirrors the gate in decision.py.
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
    """Register all cron + interval jobs on the scheduler.

    Per-station:
    - midnight actuals scrape (00:05 local)
    - betting scan (every 15 min, self-skips after station-local cutoff)
    - resolution scan (every 15 min, 24/7)

    Global:
    - monthly DB housekeeping (1st of month, 02:00 UTC)
    - monthly historical retrain (1st of month, 03:00 UTC)
    - periodic retrain (daily 04:00 UTC, if >30 days stale)
    - enrollment scan (every 6 hours)
    """
    if stations is None:
        logger.warning("schedule_all_jobs called without stations - no per-station jobs")
        stations = {}

    from hightempbot.ingestion.actuals import supports_actual_scrape
    from hightempbot.scheduler.betting_tick import run_betting_tick
    from hightempbot.resolution.settler import run_resolution_tick

    # Register error listener so job failures are logged prominently
    if not getattr(scheduler, "_hightempbot_error_listener_registered", False):
        def _job_error_listener(event):
            _alert_scheduler_failure(event, db_path)

        scheduler.add_listener(_job_error_listener, EVENT_JOB_ERROR)
        setattr(scheduler, "_hightempbot_error_listener_registered", True)

    # Remove any existing jobs from previous schedule_all_jobs() call
    for job in scheduler.get_jobs():
        job.remove()

    def _schedule_station_jobs(station: StationConfig) -> None:
        icao = station.icao

        # Validate station.timezone up front. Without this, a malformed
        # timezone string raises pytz.UnknownTimeZoneError deep inside
        # CronTrigger / datetime.now(pytz.timezone(...)) at job-run time,
        # caught by a generic try/except that silently disables the
        # station's TP/SL monitor (ce-review reliability finding 2026-05-16).
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

        # Per-station minute offset to spread the :00 thunder-herd across
        # the worker pool. With 50+ stations all firing at minute 0, the
        # 20-thread executor backlogs and Open-Meteo + CLOB hit
        # rate-limits (per memory `feedback_wu_concurrency_limit`: >10
        # concurrent caused SSL EOF). A deterministic ICAO-derived offset
        # (mod SCAN_INTERVAL_MINUTES) gives every station the same cadence
        # but on a different minute slot within the cycle. The helper is
        # shared with the betting/TP-SL gates so the gate can recover the
        # same tick index the scheduler uses.
        offset = icao_tick_offset(icao)

        def _slots(*minutes: int) -> str:
            # Backwards-compat wrapper: callers still pass explicit slot
            # literals so cadence shifts are obvious in the diff. Slots
            # are computed centrally via _scan_minute_slots so the cadence
            # constant stays the single source of truth.
            del minutes  # unused — slots come from SCAN_INTERVAL_MINUTES
            return ",".join(
                str((m + offset) % 60) for m in _scan_minute_slots()
            )

        def _slots_offset(extra: int) -> str:
            """Same cadence as ``_slots`` but shifted by ``extra`` minutes
            (used by TP/SL monitor to land between the betting/resolution
            ticks)."""
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
                CronTrigger(minute=_slots(0, 10, 20, 30, 40, 50), timezone=station.timezone),
                args=[station, db_path, initial_bankroll, dry_run],
                id=f"betting_{icao}",
                replace_existing=True,
                # 900s (1.5x cadence) leaves headroom for slow scans without
                # silently dropping the next slot (per ce-review rel-001).
                misfire_grace_time=900,
                name=f"Betting scan: {icao} ({station.city})",
                max_instances=1,
            )

        scheduler.add_job(
            run_resolution_tick,
            CronTrigger(minute=_slots(0, 10, 20, 30, 40, 50), timezone=station.timezone),
            args=[station, db_path],
            id=f"resolution_{icao}",
            replace_existing=True,
            misfire_grace_time=900,
            name=f"Resolution scan: {icao} ({station.city})",
            max_instances=1,
        )

        # TP/SL monitor: 10-min cadence offset 5 min from betting tick to
        # avoid colliding with the betting/resolution slots, then further
        # offset by the per-station value. Stale-flag age-out (TP_SL_FLAG_
        # STALE_SECONDS=600) plus 600s grace = up to 20 min lag tolerance.
        # The job loops over every strategy with non-None tp/sl (currently
        # YMID and TAIL) — see _run_tp_sl_monitor_job.
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


    # --- Per-station midnight actuals / betting / resolution jobs ---
    # Jobs stay scheduled around the clock; betting ticks self-skip after the
    # station-local cutoff so scan health remains fresh.
    for station in stations.values():
        _schedule_station_jobs(station)

    # --- Monthly DB housekeeping (1st of month, 02:00 UTC) ---
    # Was "monthly_bss" before Phase F (2026-04-22). The BSS recalc step was
    # removed along with bss.py; housekeeping (expire stuck pendings, prune
    # pipeline_health / market_tokens) still needs a monthly cadence.
    def _monthly_housekeeping_job():
        from hightempbot.persistence.ledger import (
            expire_stuck_pending, prune_book_snapshots,
            prune_market_tokens, prune_pipeline_health,
        )
        hk_conn = get_connection(db_path)
        # In live mode, hand expire_stuck_pending an OrderClient so it can
        # CLOB-verify each row with an order_id before zeroing PnL. If the
        # client cannot be built, skip expiry entirely; blind live expiry can
        # zero real fills during credential/RPC outages.
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
            # book_snapshots keeps LONG history (180d) for calibration refits +
            # backtest parity — pruned here only to bound unbounded growth.
            n_snapshots = prune_book_snapshots(hk_conn)
            # ce-code-review P2 #31/#50: prune wallet/bankroll/operator audit
            # tables on the same monthly schedule. 30-day window preserves
            # multi-week drawdown history without unbounded growth.
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

    # --- Monthly historical retrain (1st of month) ---
    # Backfills last month's true day-ahead forecasts from Open-Meteo Previous Runs API,
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
        misfire_grace_time=86400,  # 24h grace - can run late
        name="Monthly historical retrain",
        max_instances=1,
    )

    # --- Weekly forecast backfill (every Sunday 05:00 UTC) ---
    # Keeps forecast_archive fresh for calibration/BSS between monthly retrains.
    def _weekly_forecast_backfill_job():
        # Sequential — weekly backfill spans 7 days × N stations and is
        # already long-running; rate-limit safety beats wall-clock here.
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

    # --- Daily forecast backfill (every day 00:30 UTC) ---
    # The weekly Sunday backfill alone leaves forecast_archive up to 6 days
    # stale during the week, which gates calibration n_samples below MIN_PAIRS
    # for stations that just entered the 9-member Open-Meteo regime. Run the
    # same backfill daily, scoped to "yesterday only", so the archive tracks
    # the live ingest cadence. 00:30 UTC gives Open-Meteo's `previous_runs`
    # endpoint time to publish the previous calendar day. The weekly job is
    # left in place as a self-healing safety net (it backfills the full last
    # 7 days, so it patches any single failed daily run).
    def _daily_forecast_backfill_job():
        # Daily window is 1 day per station, so parallelism (max_workers=4)
        # cuts wall-clock at ~50 stations from ~75s to ~20s without
        # exceeding Open-Meteo's per-IP rate ceiling — each worker still
        # honors the 0.3s ingestion._RATE_DELAY between its own calls.
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
        # 1h grace (was 12h): a bot restart at 11:59 UTC re-triggered the
        # 00:30 cron under the larger window, which surprised operators
        # and double-ran backfill. Weekly Sunday job is the self-healing
        # safety net for any single missed daily run.
        misfire_grace_time=3600,
        name="Daily forecast backfill",
        max_instances=1,
    )

    # --- Daily rolling actuals backfill (every day 00:15 UTC) ---
    # Self-heals gaps in the 35-day rolling window per station so the EMOS
    # retrain never silently falls below MIN_PAIRS just because a single
    # midnight scrape failed weeks ago. Also force-retrains any station
    # whose latest stored params are below the readiness floor. Scheduled
    # 15 min before _daily_forecast_backfill_job so freshly filled actuals
    # join the same UTC cycle's forecast backfill.
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

    # --- Periodic retrain (daily check, retrain if >30 days stale) ---
    # If retrain fails (no new data), retries next day up to 3 consecutive attempts.
    def _periodic_retrain_job():
        """Retrain stations whose calibration is >30 days old.

        Logs to pipeline_health stage='retrain' for dashboard visibility.
        Retry logic: if retrain returns None (insufficient data), increment
        a failure counter via pipeline_health. After 3 consecutive failures,
        stop retrying until the next 30-day cycle.
        """
        from hightempbot.calibration.model import retrain as do_retrain
        from hightempbot.stations import get_all_stations

        # Hoist the per-station retrain closure outside the inner loop so
        # Python doesn't rebuild a fresh closure for every iteration.
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

            # Batch the three per-station gate queries into three group-by
            # aggregates so the loop is O(1) per station instead of 3 × O(N).
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

            # Daemon-thread timeout avoids holding the APS worker if a model
            # fit ignores the timeout and keeps running in the background.
            for icao in sorted(all_st.keys()):
                # Decouple "LUT data freshness" from "table-update freshness":
                # stamp refreshed_at on every station the job touches so idle
                # weekends/holidays don't spuriously fire the stale-LUT gate.
                # Only no-ops for stations that have no lut_bucket_stats rows yet.
                try:
                    stamp_refreshed(rt_conn, icao)
                except Exception:
                    logger.warning("stamp_refreshed failed for %s", icao, exc_info=True)
                # Coverage gate: skip retrain for stations below MIN_COVERAGE_PCT coverage
                if expected_days > 0:
                    actuals_n = actuals_count.get(icao, 0)
                    if actuals_n / expected_days < MIN_COVERAGE_PCT:
                        continue

                # Last trained_at for h=1
                last_trained = last_trained_by.get(icao)

                # Determine days since last retrain
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

                # Attempt retrain with a timeout per station. The worker
                # opens its own DB connection to avoid cross-thread sqlite use.
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

    # --- Auto-enrollment scan (every 6 hours) ---
    def _enrollment_scan_job():
        """Check for new Polymarket temperature cities and trigger enrollment.

        Lightweight scan: fetches top 100 markets by volume from Gamma,
        filters for temperature markets, extracts city names, compares
        against enrolled_stations DB to find new cities.
        """
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

    # --- Periodic reconciler (live only) ---
    # PENDING-first ledger writes mean a successful CLOB place_order can lag
    # the ledger UPDATE; the reconciler watches for that gap and reconciles
    # CLOB state back into the ledger. In live mode we run it on a 5-min
    # cadence (RECONCILE_INTERVAL_MINUTES) alongside the existing tp_sl /
    # betting cycles so PENDING-only slot lockup self-clears.
    #
    # the stranded-PENDING sweep inside
    # `reconcile_orders` (persistence/reconciliation.py) caps the
    # crash-between-PENDING-write-and-place_order window at this interval.
    # Combined with the startup reconcile invocation in main.py, the window
    # is bounded by max(boot_recon_runtime, RECONCILE_INTERVAL_MINUTES).
    #
    # max_instances=1 guards against the rare slow CLOB pass taking longer
    # than the interval — the next firing skips rather than queues so we
    # don't double-reconcile concurrently.
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

    # --- System health check (every 15 min, offset from betting ticks) ---
    def _system_health_check():
        """Detect systemic failures and send Telegram alert.

        Checks:
        1. All stations erroring in the last 15 min (import crash, API down, etc.)
        2. Zero forecasts in 2 hours despite active stations (silent failure)
        3. Persistent calibration unhealthy: ≥ CALIBRATION_HEALTH_ALERT_THRESHOLD
           active stations with n_samples < MIN_PAIRS OR actuals stale
           > ACTUALS_STALE_DAYS (auto-heal pipeline degraded).
        """
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

            # 2. Zero forecasts in 2 hours after forecast-eligible work
            # reached market/CLOB or forecast stages. Quiet windows with only
            # gate skips are expected when the UTC target date has not rolled
            # for east-of-UTC stations, or no enabled strategy entry hour is
            # active.
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

            # 3. Persistent calibration unhealthy
            # Today's "no bets for 21h" outage (44 stations stuck at n=29
            # after the 2026-04-26 actuals gap entered the rolling window)
            # was invisible to checks 1 and 2 — scans were "OK", forecasts
            # were "OK". The only symptom was n_samples falling below
            # MIN_PAIRS in calibration_params. Page on either a low
            # n_samples OR stale actuals (>ACTUALS_STALE_DAYS) so the
            # auto-heal pipeline can't silently break again.
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

    # Boot-time rolling actuals self-heal. Restarts should not have to wait
    # until 00:15 UTC for gap recovery — kick the same job on a daemon
    # thread so the betting pipeline can become healthy mid-day after a
    # deploy. Errors are swallowed by the job itself; the daemon thread
    # never blocks scheduler startup.
    threading.Thread(
        target=rolling_actuals_backfill_job,
        args=(db_path, data_dir),
        name="rolling_actuals_backfill_boot",
        daemon=True,
    ).start()
