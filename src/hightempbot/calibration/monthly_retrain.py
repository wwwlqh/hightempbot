"""Monthly job: backfill last month's forecasts, retrain EMOS per station,
refresh the LUT, and log to retrain_history."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

logger = logging.getLogger(__name__)


def run_monthly_retrain(
    conn: sqlite3.Connection,
    db_path: str,
    target_month: date | None = None,
) -> dict[str, dict]:
    """Backfill ``target_month`` (default: last month) and retrain every station.
    Aborts entirely if the backfill fails. Returns {station_id: result}."""
    from hightempbot.db.connection import get_connection
    from hightempbot.ingestion.openmeteo_forecast import backfill_openmeteo
    from hightempbot.stations import get_all_stations

    if target_month is None:
        today = date.today()
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - timedelta(days=1)
        target_month = last_month_end.replace(day=1)

    month_end = (target_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    month_label = target_month.strftime("%Y-%m")

    logger.info("Monthly retrain: backfilling %s historical forecasts", month_label)

    all_stations = get_all_stations(conn)

    # --- Step 1: Backfill historical forecasts for the target month ---
    backfill_error: str | None = None
    fc_conn = get_connection(db_path)
    try:
        backfill_result = backfill_openmeteo(
            target_month, month_end, fc_conn, stations=all_stations,
        )
        if getattr(backfill_result, "ok", True) is False:
            failed_stations = len(getattr(backfill_result, "failed_stations", ()) or ())
            failed_chunks = len(getattr(backfill_result, "failed_chunks", ()) or ())
            backfill_error = (
                f"forecast backfill failed "
                f"({failed_stations} station(s), {failed_chunks} chunk(s))"
            )
    except Exception as exc:
        backfill_error = f"forecast backfill raised {type(exc).__name__}: {str(exc)[:120]}"
        logger.error("Monthly retrain: forecast backfill failed for %s", month_label, exc_info=True)
    finally:
        fc_conn.close()

    if backfill_error:
        results = {}
        logger.error(
            "Monthly retrain %s aborted: %s",
            month_label,
            backfill_error,
        )
        for icao in sorted(all_stations.keys()):
            result = {
                "station_id": icao,
                "month": month_label,
                "kept": "skip",
                "reason": backfill_error,
            }
            _log_health(
                conn,
                icao,
                "retrain",
                "ERROR",
                f"{month_label}: retrain skipped; {backfill_error}",
            )
            _save_history(conn, result)
            results[icao] = result
        return results

    # --- Step 2: Per-station retrain ---
    results = {}
    for icao in sorted(all_stations.keys()):
        result = _retrain_station(conn, icao, month_label)
        results[icao] = result

    n_ok = sum(1 for r in results.values() if r["kept"] == "ok")
    n_skip = sum(1 for r in results.values() if r["kept"] == "skip")
    logger.info(
        "Monthly retrain %s complete: %d retrained, %d skipped",
        month_label, n_ok, n_skip,
    )

    return results


def _retrain_station(
    conn: sqlite3.Connection,
    station_id: str,
    month_label: str,
) -> dict:
    """Retrain one station and record the outcome (no savepoint: retrain commits)."""
    from hightempbot.calibration.model import retrain

    result = {
        "station_id": station_id,
        "month": month_label,
        "kept": "skip",
        "reason": "",
    }

    try:
        model = retrain(station_id, 1, conn)
        if model is None or not model.is_ready():
            result["kept"] = "skip"
            result["reason"] = "retrain returned None (insufficient data)"
            _log_health(conn, station_id, "retrain", "ERROR",
                        f"{month_label}: retrain failed (insufficient data)")
        else:
            n = model.emos_params.n_samples if model.emos_params else 0
            result["kept"] = "ok"
            result["reason"] = f"Retrained with {n} pairs"
            _log_health(conn, station_id, "retrain", "OK",
                        f"{month_label}: retrained n={n}")
            logger.info("Monthly retrain %s %s: ok n=%d", month_label, station_id, n)
            try:
                refresh_lut_after_retrain(conn, station_id, month_label)
            except Exception as exc:
                _log_health(conn, station_id, "lut", "ERROR",
                            f"{month_label}: lut refresh failed - {str(exc)[:150]}")
                logger.warning("Monthly retrain %s %s: LUT refresh error",
                               month_label, station_id, exc_info=True)
    except Exception as e:
        result["kept"] = "skip"
        result["reason"] = f"Error: {str(e)[:150]}"
        _log_health(conn, station_id, "retrain", "ERROR",
                    f"{month_label}: error {str(e)[:100]}")
        logger.error("Monthly retrain %s %s: error", month_label, station_id, exc_info=True)

    _save_history(conn, result)
    return result


def refresh_lut_after_retrain(
    conn: sqlite3.Connection,
    station_id: str,
    month_label: str,
) -> None:
    """After a retrain: rebuild the LUT if it exists, otherwise seed it."""
    from hightempbot.calibration.lut import (
        clear_station_lut,
        rebuild_lut,
        seed_lut_from_history,
        _supports_station_lut,
    )

    if not _supports_station_lut(conn, station_id):
        hist_deleted, lut_deleted = clear_station_lut(conn, station_id)
        _log_health(
            conn,
            station_id,
            "lut",
            "SKIPPED",
            f"{month_label}: unsupported source; cleared {hist_deleted} history rows and {lut_deleted} LUT rows",
        )
        logger.info(
            "Monthly retrain %s %s: skipped LUT for unsupported source",
            month_label,
            station_id,
        )
        return

    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM lut_bucket_stats WHERE station_id = ?",
        (station_id,),
    ).fetchone()["n"]

    if existing > 0:
        written = rebuild_lut(conn, station_id)
        _log_health(conn, station_id, "lut", "OK",
                    f"{month_label}: rebuild_lut wrote {written} bucket rows")
        logger.info("Monthly retrain %s %s: LUT rebuild wrote %d rows",
                    month_label, station_id, written)
        return

    days, triples = seed_lut_from_history(conn, station_id)
    if triples == 0:
        _log_health(conn, station_id, "lut", "SKIPPED",
                    f"{month_label}: no triples yet (empty actuals or no brackets)")
        logger.info("Monthly retrain %s %s: seed_lut_from_history deferred (no triples)",
                    month_label, station_id)
    else:
        _log_health(conn, station_id, "lut", "OK",
                    f"{month_label}: seeded LUT from {days} days / {triples} triples")
        logger.info("Monthly retrain %s %s: seeded LUT from %d days / %d triples",
                    month_label, station_id, days, triples)


def _save_history(conn: sqlite3.Connection, result: dict) -> None:
    """Insert or update retrain_history row."""
    try:
        conn.execute(
            """INSERT OR REPLACE INTO retrain_history
            (station_id, horizon, retrain_month, kept, reason)
            VALUES (?, ?, ?, ?, ?)""",
            (
                result["station_id"], 1,
                result["month"],
                result["kept"], result.get("reason", ""),
            ),
        )
        conn.commit()
    except Exception:
        logger.warning("Failed to save retrain_history for %s", result["station_id"], exc_info=True)


def _log_health(conn: sqlite3.Connection, station_id: str, stage: str, status: str, msg: str) -> None:
    """Log to pipeline_health for dashboard visibility."""
    from hightempbot.db.connection import log_pipeline_health
    log_pipeline_health(conn, station_id, stage, status, msg)
