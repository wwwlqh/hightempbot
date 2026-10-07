"""Enroll a newly discovered city: parse source → geocode → backfill actuals →
coverage gate → backfill forecasts → fit EMOS → seed LUT → register.

Progress is written to ``enrolled_stations`` at each step.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from hightempbot.db.connection import utc_now_sql
from hightempbot.enrollment.geocode import geocode_city
from hightempbot.enrollment.parser import parse_resolution_source
from hightempbot.execution.strategy_constants import MIN_COVERAGE_PCT, REF_START_DATE
from hightempbot.stations import (
    StationConfig,
    _city_to_slug,
    register_enrolled_station,
    supports_live_resolution_source,
)

logger = logging.getLogger(__name__)


_ALLOWED_UPDATE_FIELDS = frozenset({
    "skip_reason", "actuals_count", "forecast_count", "coverage_pct",
    "bss", "dry_run_start",
})

_RETRYABLE_SKIP_REASONS = frozenset({
    "low_bss",
    "train_failed",
    "lut_seed_failed",
    "insufficient_lut_data",
})


def should_probe_skipped_station(row: sqlite3.Row | dict) -> bool:
    """Return whether a skipped row needs a fresh event parse to decide retry."""
    status = str(row["status"] or "").upper()
    if status != "SKIPPED":
        return False
    reason = str(row["skip_reason"] or "").strip().lower()
    return reason in {"unknown_source", "unsupported_source"}


def should_retry_skipped_station(
    row: sqlite3.Row | dict,
    candidate_resolution_source: str | None = None,
) -> bool:
    """Return whether a skipped enrollment row should restart from step 1."""
    status = str(row["status"] or "").upper()
    if status != "SKIPPED":
        return False

    reason = str(row["skip_reason"] or "").strip().lower()
    if reason in _RETRYABLE_SKIP_REASONS:
        return True

    if reason == "low_coverage":
        coverage = row["coverage_pct"]
        return coverage is not None and float(coverage) >= MIN_COVERAGE_PCT

    if reason in {"unknown_source", "unsupported_source"}:
        return supports_live_resolution_source(candidate_resolution_source)

    return False


def _update_status(
    conn: sqlite3.Connection, icao: str,
    status: str, step: str, detail: str = "",
    **extra_fields,
) -> None:
    """Update enrolled_stations status, step, and detail."""
    sets = ["status = ?", "step = ?", "step_detail = ?", "updated_at = datetime('now')"]
    vals: list = [status, step, detail]
    for k, v in extra_fields.items():
        if k not in _ALLOWED_UPDATE_FIELDS:
            raise ValueError(f"Disallowed column in _update_status: {k}")
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(icao)
    conn.execute(
        f"UPDATE enrolled_stations SET {', '.join(sets)} WHERE icao = ?",
        vals,
    )
    conn.commit()


def _log_health(conn: sqlite3.Connection, station_id: str, stage: str, status: str, msg: str) -> None:
    """Log to pipeline_health for dashboard visibility."""
    from hightempbot.db.connection import log_pipeline_health
    log_pipeline_health(conn, station_id, stage, status, msg)


def _seed_market_tokens_from_event(
    conn: sqlite3.Connection,
    station_id: str,
    market_date: date | None,
    event: dict,
) -> int:
    """Cache bracket metadata from the just-fetched event before LUT seeding."""
    if market_date is None:
        return 0

    markets = event.get("markets", []) or []
    if not markets:
        return 0

    try:
        from hightempbot.scheduler.market_data import _parse_raw_markets

        parsed = _parse_raw_markets(markets)
    except Exception:
        logger.warning(
            "Failed to parse event markets for %s during enrollment",
            station_id,
            exc_info=True,
        )
        return 0

    if not parsed:
        return 0

    rows_to_insert = []
    for bi, mkt in parsed.items():
        rows_to_insert.append((
            station_id,
            market_date.isoformat(),
            bi,
            mkt.get("token_id", ""),
            mkt.get("no_token_id", ""),
            mkt.get("market_id", ""),
            mkt.get("bracket_label"),
            mkt.get("bracket_low"),
            mkt.get("bracket_high"),
        ))

    try:
        conn.executemany(
            "INSERT OR REPLACE INTO market_tokens "
            "(station_id, market_date, bracket_idx, token_id, no_token_id, "
            "market_id, bracket_label, bracket_low, bracket_high) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows_to_insert,
        )
        conn.commit()
    except Exception:
        logger.warning(
            "Failed to seed market_tokens from enrollment event for %s",
            station_id,
            exc_info=True,
        )
        return 0

    return len(rows_to_insert)


def enroll_station(
    city_name: str,
    event: dict,
    conn: sqlite3.Connection,
    db_path: str,
    data_dir: str = "data",
    market_date: date | None = None,
    runtime_dry_run: bool = True,
) -> str:
    """Enroll ``city``. Returns 'DRY_RUN'/'LIVE', 'CONFIGURING' (retry later) or
    'SKIPPED'. Never raises."""
    icao = ""
    try:
        # ── Step 1: Parse resolution source ──────────────────────────────
        _log_health(conn, city_name, "enrollment", "OK", f"Starting enrollment for {city_name}")

        candidate = parse_resolution_source(event)
        if candidate is None:
            _insert_skipped(conn, city_name, "unknown_source", f"Cannot determine resolution source for {city_name}")
            _log_health(conn, city_name, "enrollment", "ERROR", "Unknown resolution source")
            return "SKIPPED"

        icao = candidate.icao

        existing = conn.execute(
            "SELECT icao, city, status, updated_at, skip_reason, coverage_pct, resolution_source "
            "FROM enrolled_stations WHERE icao = ? OR city = ? "
            "ORDER BY CASE WHEN icao = ? THEN 0 ELSE 1 END LIMIT 1",
            (icao, candidate.city, icao),
        ).fetchone()
        if existing:
            status = existing["status"]
            # Restart if stuck in an intermediate step for over 2 hours.
            if status in ("CONFIGURING", "BACKFILLING", "TRAINING"):
                updated = existing["updated_at"] or ""
                if updated:
                    from datetime import datetime as _dt
                    try:
                        age_h = (datetime.now(timezone.utc) - _dt.fromisoformat(updated).replace(tzinfo=timezone.utc)).total_seconds() / 3600
                        if age_h > 2:
                            logger.warning("Station %s stuck in %s for %.1fh, restarting enrollment", icao, status, age_h)
                            conn.execute("DELETE FROM enrolled_stations WHERE icao = ?", (icao,))
                            conn.commit()
                        else:
                            logger.info("Station %s in progress (status=%s, %.0fm ago), skipping", icao, status, age_h * 60)
                            return status
                    except Exception:
                        pass
                else:
                    logger.info("Station %s already enrolled (status=%s), skipping", icao, status)
                    return status
            elif should_retry_skipped_station(existing, candidate.resolution_source):
                existing_icao = existing["icao"]
                logger.info(
                    "Station %s (%s) marked retryable from prior skip (%s), restarting enrollment",
                    existing_icao,
                    candidate.city,
                    existing["skip_reason"],
                )
                conn.execute("DELETE FROM enrolled_stations WHERE icao = ?", (existing_icao,))
                conn.commit()
            else:
                logger.info("Station %s already enrolled (status=%s), skipping", icao, status)
                return status

        # ── Step 2: Geocode ──────────────────────────────────────────────
        # OR IGNORE: another enrollment run may own the row.
        poly_slug = _city_to_slug(candidate.city)
        conn.execute(
            """INSERT OR IGNORE INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source, poly_slug, status, step)
            VALUES (?, ?, 0, 0, 'UTC', ?, ?, ?, ?, 'CONFIGURING', 'geocoding')""",
            (icao, candidate.city, candidate.unit, candidate.resolution_source,
             candidate.resolution_source, poly_slug),
        )
        conn.commit()
        if conn.execute("SELECT changes()").fetchone()[0] == 0:
            logger.info("Station %s already being enrolled by another run, skipping", icao)
            return conn.execute("SELECT status FROM enrolled_stations WHERE icao = ?", (icao,)).fetchone()["status"]

        geo = geocode_city(candidate.city)
        if geo is None:
            _update_status(conn, icao, "SKIPPED", "geocode_failed",
                           f"Geocoding returned no results for {candidate.city}",
                           skip_reason="geocode_failed")
            _log_health(conn, icao, "enrollment", "ERROR", "Geocoding failed")
            return "SKIPPED"

        conn.execute(
            "UPDATE enrolled_stations SET lat = ?, lon = ?, timezone = ? WHERE icao = ?",
            (geo.lat, geo.lon, geo.timezone, icao),
        )
        conn.commit()
        _update_status(conn, icao, "CONFIGURING", "creating_config",
                        f"lat={geo.lat:.2f}, lon={geo.lon:.2f}, tz={geo.timezone}")

        station = StationConfig(
            icao=icao,
            city=candidate.city,
            lat=geo.lat,
            lon=geo.lon,
            timezone=geo.timezone,
            unit=candidate.unit,
            resolution_source=candidate.resolution_source,
            poly_slug=poly_slug,
        )

        # ── Step 3: Backfill actuals ─────────────────────────────────────
        _update_status(conn, icao, "BACKFILLING", "actuals", "Starting actuals backfill")
        _log_health(conn, icao, "enrollment", "OK", "Backfilling actuals")

        ref_start = date.fromisoformat(REF_START_DATE)
        today = date.today()
        actuals_count, backfill_aborted = _backfill_actuals(conn, station, ref_start, today, Path(data_dir), icao)

        if backfill_aborted:
            _update_status(conn, icao, "SKIPPED", "consecutive_failures",
                            f"Backfill aborted after 30 consecutive failures ({actuals_count} days fetched)",
                            skip_reason="consecutive_failures", actuals_count=actuals_count)
            _log_health(conn, icao, "enrollment", "ERROR",
                        f"Backfill aborted — 30 consecutive failures (check ICAO country code)")
            return "SKIPPED"

        _update_status(conn, icao, "BACKFILLING", "actuals_done",
                        f"{actuals_count} days fetched", actuals_count=actuals_count)

        # ── Step 4: Coverage gate ────────────────────────────────────────
        _update_status(conn, icao, "BACKFILLING", "coverage_check", "Checking coverage")

        expected_days = (today - ref_start).days + 1
        coverage = actuals_count / expected_days if expected_days > 0 else 0

        if coverage < MIN_COVERAGE_PCT:
            _update_status(conn, icao, "SKIPPED", "low_coverage",
                            f"coverage={coverage:.1%} < {MIN_COVERAGE_PCT:.0%} ({actuals_count}/{expected_days} days)",
                            skip_reason="low_coverage", coverage_pct=coverage)
            _log_health(conn, icao, "enrollment", "ERROR",
                        f"Coverage {coverage:.1%} below {MIN_COVERAGE_PCT:.0%}")
            return "SKIPPED"

        _update_status(conn, icao, "BACKFILLING", "coverage_check",
                        f"coverage={coverage:.1%} OK", coverage_pct=coverage)

        # ── Step 5: Backfill forecasts ───────────────────────────────────
        _update_status(conn, icao, "BACKFILLING", "forecasts", "Starting forecast backfill")
        _log_health(conn, icao, "enrollment", "OK", "Backfilling forecasts")

        from hightempbot.db.connection import get_connection
        from hightempbot.ingestion.openmeteo_forecast import backfill_openmeteo

        fc_conn = get_connection(db_path)
        try:
            backfill_openmeteo(ref_start, today, fc_conn, stations={icao: station})
        finally:
            fc_conn.close()

        fc_count = conn.execute(
            "SELECT COUNT(DISTINCT target_date) FROM forecast_archive WHERE station_id = ?",
            (icao,),
        ).fetchone()[0]
        _update_status(conn, icao, "BACKFILLING", "forecasts_done",
                        f"{fc_count} forecast days", forecast_count=fc_count)

        # ── Step 6: Train calibration ────────────────────────────────────
        _log_health(conn, icao, "enrollment", "OK", "Training calibration")

        from hightempbot.calibration.model import retrain

        for h in (1, 2, 3):
            _update_status(conn, icao, "TRAINING", f"emos_h{h}", f"Training horizon {h}")
            retrain(icao, h, conn)

        verify_row = conn.execute(
            "SELECT n_samples FROM calibration_params "
            "WHERE station_id = ? AND horizon = 1 AND param_type = 'emos' "
            "ORDER BY trained_at DESC LIMIT 1",
            (icao,),
        ).fetchone()
        if verify_row is None:
            logger.error("Retrain produced no EMOS params for %s h=1", icao)
            _update_status(conn, icao, "SKIPPED", "train_failed",
                           "Retrain completed but no EMOS params produced",
                           skip_reason="train_failed")
            _log_health(conn, icao, "enrollment", "ERROR", "No EMOS params after retrain")
            return "SKIPPED"

        # ── Step 7: Seed LUT from history ────────────────────────────────
        # Zero triples means no usable history yet.
        _update_status(conn, icao, "TRAINING", "lut_seed", "Seeding LUT from history")
        seeded_brackets = _seed_market_tokens_from_event(conn, icao, market_date, event)
        if seeded_brackets > 0:
            _update_status(
                conn,
                icao,
                "TRAINING",
                "lut_seed",
                f"Seeded {seeded_brackets} market brackets from live event",
            )
        from hightempbot.calibration.lut import seed_lut_from_history
        try:
            days, triples = seed_lut_from_history(conn, icao)
        except Exception as exc:
            _update_status(conn, icao, "SKIPPED", "lut_seed_failed",
                           f"seed_lut_from_history raised: {str(exc)[:120]}",
                           skip_reason="lut_seed_failed")
            _log_health(conn, icao, "enrollment", "ERROR",
                        f"LUT seed error: {str(exc)[:120]}")
            return "SKIPPED"

        if triples == 0:
            if seeded_brackets == 0 and market_date is not None:
                _update_status(
                    conn,
                    icao,
                    "CONFIGURING",
                    "awaiting_market_tokens",
                    "Waiting for parseable market brackets before LUT seed; enrollment will retry from scratch",
                )
                _log_health(
                    conn,
                    icao,
                    "enrollment",
                    "WARNING",
                    "No parseable event brackets yet; leaving enrollment in retryable state",
                )
                return "CONFIGURING"
            _update_status(conn, icao, "SKIPPED", "insufficient_lut_data",
                           "seed_lut_from_history produced 0 triples",
                           skip_reason="insufficient_lut_data")
            _log_health(conn, icao, "enrollment", "ERROR",
                        "No LUT triples — actuals / brackets / EMOS missing")
            return "SKIPPED"

        _update_status(conn, icao, "TRAINING", "lut_seed",
                       f"LUT seeded: {days}d / {triples} triples")

        # ── Step 8: Transition to active runtime mode ───────────────────
        now_iso = utc_now_sql()
        final_status = "DRY_RUN" if runtime_dry_run else "LIVE"
        final_step = "day_1" if runtime_dry_run else "active"
        final_detail = "Dry-run started" if runtime_dry_run else "Live trading enabled"
        _update_status(conn, icao, final_status, final_step, final_detail, dry_run_start=now_iso)
        _log_health(
            conn,
            icao,
            "enrollment",
            "OK",
            f"Enrolled as {final_status} (LUT {days}d/{triples}t, coverage={coverage:.1%})",
        )

        register_enrolled_station(station)

        logger.info(
            "Station %s (%s) enrolled as %s: LUT %d days / %d triples, coverage=%.1f%%, %d actuals, %d forecast days",
            icao, candidate.city, final_status, days, triples, coverage * 100, actuals_count, fc_count,
        )
        return final_status

    except Exception as e:
        logger.error("Enrollment failed for %s (%s): %s", city_name, icao, e, exc_info=True)
        if icao:
            try:
                _update_status(conn, icao, "SKIPPED", "error", str(e)[:200],
                                skip_reason=f"error: {str(e)[:100]}")
            except Exception:
                pass
        _log_health(conn, icao or city_name, "enrollment", "ERROR", str(e)[:200])
        return "SKIPPED"


def _backfill_actuals(
    conn: sqlite3.Connection,
    station: StationConfig,
    start: date,
    end: date,
    data_dir: Path,
    icao: str,
) -> tuple[int, bool]:
    """Fetch actuals day by day. Skips ahead a year (up to 3 times) while there's
    no data yet; aborts after 30 straight failures. Returns ``(count, aborted)``."""
    from hightempbot.ingestion.actuals import fetch_actual, upsert_actual

    count = 0
    consecutive_failures = 0
    max_consecutive_failures = 30
    max_skips = 3
    skips_used = 0
    total_days = (end - start).days + 1
    current = start
    aborted = False

    while current <= end:
        row = fetch_actual(icao, current, data_dir, station_override=station)
        if row is not None:
            upsert_actual(conn, row)
            count += 1
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                if count == 0 and skips_used < max_skips:
                    skip_to = current + timedelta(days=365)
                    logger.info(
                        "Backfill %s: %d consecutive failures with 0 data — skipping forward to %s (skip %d/%d)",
                        icao, max_consecutive_failures, skip_to, skips_used + 1, max_skips,
                    )
                    _update_status(conn, icao, "BACKFILLING", "actuals",
                                   f"Skipping forward to {skip_to} (no data before this)")
                    current = skip_to
                    consecutive_failures = 0
                    skips_used += 1
                    continue
                else:
                    logger.error(
                        "Backfill %s: %d consecutive failures — aborting (%d fetched, %d skips used)",
                        icao, max_consecutive_failures, count, skips_used,
                    )
                    aborted = True
                    break

        if count > 0 and count % 100 == 0:
            _update_status(conn, icao, "BACKFILLING", "actuals",
                            f"{count}/{total_days} days ({count/total_days:.0%})",
                            actuals_count=count)

        current += timedelta(days=1)

    return count, aborted


def _insert_skipped(
    conn: sqlite3.Connection,
    city_name: str,
    reason: str,
    detail: str,
) -> None:
    """Insert a SKIPPED record for a city that can't be enrolled."""
    try:
        conn.execute(
            """INSERT OR IGNORE INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source,
             poly_slug, status, step, step_detail, skip_reason)
            VALUES (?, ?, 0, 0, 'UTC', 'C', 'unknown', 'unknown', ?, 'SKIPPED', ?, ?, ?)""",
            (f"UNKNOWN_{_city_to_slug(city_name)}", city_name,
             _city_to_slug(city_name), reason, detail, reason),
        )
        conn.commit()
    except Exception:
        pass
