"""Open-Meteo deterministic multi-model forecasts — historical + live.

Uses the Previous Runs API for historical and the Forecast API for live.
Same models in both phases (canonical list lives in `execution/strategy_constants.py:EXPECTED_MODELS`).

IMPORTANT: Historical forecasts use `temperature_2m_previous_day1` (hourly)
to get TRUE day-ahead forecasts. The `daily=temperature_2m_max` endpoint
returns same-day composite data (includes 12Z/18Z runs issued AFTER tmax
already occurred) and must NOT be used for calibration.

9 models, each gives 1 daily tmax forecast per station per day:
  - ecmwf_ifs025       (ECMWF IFS 0.25deg)
  - gfs_seamless       (NCEP GFS)
  - icon_seamless      (DWD ICON)
  - gem_seamless       (Canada GEM)
  - meteofrance_seamless (Meteo-France)
  - ukmo_seamless      (UK Met Office)
  - knmi_seamless      (KNMI Netherlands)
  - dmi_seamless       (DMI Denmark)
  - ncep_gfs013        (NCEP GFS 0.13deg)
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta

import pytz
import requests

from hightempbot.execution.strategy_constants import EXPECTED_MODELS
from hightempbot.stations import StationConfig

logger = logging.getLogger(__name__)

_PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
_HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_LIVE_FETCH_ENDPOINTS = (
    ("previous-runs", _PREVIOUS_RUNS_URL, 8),
    ("historical-forecast", _HISTORICAL_FORECAST_URL, 8),
    ("forecast", _FORECAST_URL, 30),
)

# 9 deterministic models (BoM offline since 2025-07; JMA excluded)
MODELS = list(EXPECTED_MODELS)

MODELS_CSV = ",".join(MODELS)
_RATE_DELAY = 0.3
_MIN_COMPLETE_MODELS = len(MODELS)
_EXPECTED_MODEL_SET = frozenset(EXPECTED_MODELS)
_EXPECTED_MODEL_PLACEHOLDERS = ",".join("?" for _ in MODELS)


@dataclass(frozen=True)
class BackfillResult:
    stations_attempted: int = 0
    rows_stored: int = 0
    failed_stations: tuple[tuple[str, str], ...] = ()
    failed_chunks: tuple[tuple[str, str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed_stations and not self.failed_chunks


def _requested_dates(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _live_membership_error(
    result: dict[date, dict[str, float | None]],
    requested_dates: tuple[date, ...],
) -> str | None:
    """Return a human-readable membership error, or None for an exact live ensemble."""
    for target_date in requested_dates:
        model_values = result.get(target_date)
        if model_values is None:
            return f"{target_date.isoformat()} missing target date"

        present = {model_name for model_name, value in model_values.items() if value is not None}
        if present != _EXPECTED_MODEL_SET:
            missing = sorted(_EXPECTED_MODEL_SET - present)
            extra = sorted(present - _EXPECTED_MODEL_SET)
            parts = [f"{target_date.isoformat()} has {len(present)}/{len(_EXPECTED_MODEL_SET)} models"]
            if missing:
                parts.append(f"missing={','.join(missing)}")
            if extra:
                parts.append(f"extra={','.join(extra)}")
            return "; ".join(parts)

    return None


def fetch_historical(
    station: StationConfig,
    start: date,
    end: date,
) -> dict[date, dict[str, float | None]] | None:
    """Fetch TRUE day-ahead historical tmax from all 11 models for a station.

    Uses `temperature_2m_previous_day1` (hourly) to get forecasts from
    the PREVIOUS day's model run, then computes tmax over local calendar day.

    Returns dict: {local_date: {model_name: tmax_celsius, ...}, ...}
    or None on failure.
    """
    params = {
        "latitude": station.lat,
        "longitude": station.lon,
        "hourly": "temperature_2m_previous_day1",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "models": MODELS_CSV,
        "timezone": station.timezone,
    }

    try:
        time.sleep(_RATE_DELAY)
        resp = requests.get(_PREVIOUS_RUNS_URL, params=params, timeout=120)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.warning(
            "Open-Meteo historical failed for %s %s to %s",
            station.icao, start, end, exc_info=True,
        )
        return None

    return _parse_hourly_to_daily_tmax(data, "temperature_2m_previous_day1")


# In-memory cache: {(station_icao, forecast_days): (timestamp, result)}
# Forecasts change every ~6 hours; a cached forecast up to 6 hours old is fine.
# `_forecast_cache_lock` guards mutation under concurrent scheduler threads;
# wu_forecast.py uses the same pattern for its equivalent cache.
_forecast_cache: dict[tuple[str, int], tuple[float, dict[date, dict[str, float | None]]]] = {}
_forecast_cache_lock = threading.Lock()
_CACHE_MAX_AGE_S = 6 * 3600  # 6 hours


def fetch_live(
    station: StationConfig,
    forecast_days: int = 3,
) -> dict[date, dict[str, float | None]] | None:
    """Fetch day-ahead forecast daily tmax from all configured models.

    Uses `hourly=temperature_2m_previous_day1`, so each target date's tmax is
    derived from the model run initialized the PREVIOUS day (no contamination
    from same-day model runs that have already ingested target-day
    observations). Prefer the previous-runs host for train/live symmetry. If
    that host is unreachable, try Open-Meteo's historical-forecast host and
    then the live forecast host, accepting a fallback only when it returns
    actual `previous_day1` model values.

    ``forecast_days`` = number of target days starting from today (station-local).
    Falls back to cached forecast (up to 6h old) if API fails.

    Returns dict: {target_date: {model_name: tmax_celsius, ...}, ...}
    """
    import random
    import time as _time
    from datetime import datetime as _datetime

    tz = pytz.timezone(station.timezone)
    today_local = _datetime.now(tz).date()
    start_date = today_local
    end_date = today_local + timedelta(days=max(forecast_days - 1, 0))
    requested_dates = _requested_dates(start_date, end_date)

    params = {
        "latitude": station.lat,
        "longitude": station.lon,
        "hourly": "temperature_2m_previous_day1",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "models": MODELS_CSV,
        "timezone": station.timezone,
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        for endpoint_name, endpoint_url, timeout_s in _LIVE_FETCH_ENDPOINTS:
            try:
                resp = requests.get(endpoint_url, params=params, timeout=timeout_s)
                resp.raise_for_status()
                data = resp.json()
                result = _parse_hourly_to_daily_tmax(data, "temperature_2m_previous_day1")
                membership_error = _live_membership_error(result, requested_dates)
                if membership_error is None:
                    result = {target_date: result[target_date] for target_date in requested_dates}
                    # Cache successful fetch
                    with _forecast_cache_lock:
                        _forecast_cache[(station.icao, forecast_days)] = (_time.time(), result)
                    logger.info(
                        "Open-Meteo live OK for %s via %s: %d models x %d days",
                        station.icao,
                        endpoint_name,
                        len(_EXPECTED_MODEL_SET),
                        len(requested_dates),
                    )
                    return result
                logger.warning(
                    "Open-Meteo live %s returned unsafe previous_day1 membership for %s: %s",
                    endpoint_name,
                    station.icao,
                    membership_error,
                )
                continue
            except Exception:
                logger.warning(
                    "Open-Meteo live %s attempt %d/%d failed for %s",
                    endpoint_name,
                    attempt,
                    max_attempts,
                    station.icao,
                    exc_info=attempt == max_attempts,
                )
                continue

        if attempt < max_attempts:
            delay = 2 ** attempt + random.uniform(0, 1)
            logger.warning(
                "Open-Meteo live attempt %d/%d failed for %s across all endpoints, "
                "retrying in %.1fs",
                attempt, max_attempts, station.icao, delay,
            )
            _time.sleep(delay)
        else:
            logger.warning(
                "Open-Meteo live failed for %s after %d attempts across all endpoints",
                station.icao, max_attempts,
            )

    # API failed — try cache. Snapshot under the lock so we don't iterate
    # while another thread is writing.
    with _forecast_cache_lock:
        cache_snapshot = list(_forecast_cache.items())
    cached_candidates = [
        ((icao, cached_days), cached_days, cached_ts, cached_result)
        for (icao, cached_days), (cached_ts, cached_result) in cache_snapshot
        if icao == station.icao and cached_days >= forecast_days
    ]
    cached_candidates.sort(key=lambda item: (item[1], item[2]), reverse=True)
    for cache_key, cached_days, cached_ts, cached_result in cached_candidates:
        age_s = _time.time() - cached_ts
        if age_s <= _CACHE_MAX_AGE_S:
            membership_error = _live_membership_error(cached_result, requested_dates)
            if membership_error is not None:
                logger.warning(
                    "Open-Meteo cached forecast rejected for %s (horizon=%dd): %s",
                    station.icao,
                    cached_days,
                    membership_error,
                )
                with _forecast_cache_lock:
                    current = _forecast_cache.get(cache_key)
                    if current and current[0] == cached_ts:
                        _forecast_cache.pop(cache_key, None)
                continue
            age_min = int(age_s / 60)
            logger.info(
                "Open-Meteo using cached forecast for %s (age %dm, horizon=%dd)",
                station.icao,
                age_min,
                cached_days,
            )
            return cached_result
        logger.warning(
            "Open-Meteo cache expired for %s (age %.1fh, horizon=%dd)",
            station.icao,
            age_s / 3600,
            cached_days,
        )

    return None


def _parse_hourly_to_daily_tmax(
    data: dict, var_prefix: str,
) -> dict[date, dict[str, float | None]]:
    """Parse hourly response and compute daily tmax per model.

    Groups hourly values by local calendar date (timezone already applied
    by Open-Meteo via the timezone parameter) and takes the max.
    """
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    # Find model-specific columns
    model_keys: dict[str, str] = {}
    for key in hourly.keys():
        if key.startswith(var_prefix + "_") and key != var_prefix:
            model_name = key[len(var_prefix) + 1:]
            model_keys[model_name] = key

    # If single model (no model suffix), use bare key
    if not model_keys and var_prefix in hourly:
        model_keys["default"] = var_prefix

    # Group by date and compute tmax
    # {date_str: {model: [hourly_vals]}}
    date_model_vals: dict[str, dict[str, list[float]]] = {}

    for i, time_str in enumerate(times):
        date_str = time_str[:10]  # "2025-06-15T14:00" -> "2025-06-15"
        if date_str not in date_model_vals:
            date_model_vals[date_str] = {}
        for model_name, col_key in model_keys.items():
            val = hourly[col_key][i]
            if val is not None:
                if model_name not in date_model_vals[date_str]:
                    date_model_vals[date_str][model_name] = []
                date_model_vals[date_str][model_name].append(float(val))

    # Compute tmax per date per model
    result: dict[date, dict[str, float | None]] = {}
    for date_str, model_vals in date_model_vals.items():
        d = date.fromisoformat(date_str)
        tmax_per_model: dict[str, float | None] = {}
        for model_name, vals in model_vals.items():
            tmax_per_model[model_name] = max(vals) if vals else None
        result[d] = tmax_per_model

    return result


def store_forecast_records(
    conn: sqlite3.Connection,
    station_id: str,
    forecast_data: dict[date, dict[str, float | None]],
    source: str = "openmeteo",
) -> int:
    """Store multi-model forecasts in forecast_archive table.

    Each model stored as a separate "member" with centre = model name.
    Horizon = 1 (day-ahead) only — previous_day1 data is always from
    the previous day's model run.
    """
    count = 0
    rows = []

    for target_date, model_values in forecast_data.items():
        issue_date = target_date - timedelta(days=1)
        for model_name, tmax in model_values.items():
            if tmax is None:
                continue
            # Use fixed MODELS list position for stable member_num
            # (dict iteration order is not guaranteed to be consistent)
            try:
                member_num = MODELS.index(model_name) + 1
            except ValueError:
                # Unknown model — assign a high number to avoid collisions
                member_num = 100 + hash(model_name) % 100
            rows.append((
                station_id,
                target_date.isoformat(),
                1,  # horizon = 1 (true day-ahead)
                issue_date.isoformat(),
                model_name,
                member_num,
                tmax,
                source,
            ))
            count += 1

    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO forecast_archive "
            "(station_id, target_date, horizon, issue_date, centre, member, tmax_celsius, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

    return count


def backfill_openmeteo(
    start: date,
    end: date,
    conn: sqlite3.Connection,
    stations: dict[str, StationConfig] | None = None,
    chunk_days: int = 365,
) -> BackfillResult:
    """Backfill forecasts for WU stations via Open-Meteo Previous Runs API.

    Fetches in yearly chunks per station.
    Callers must pass stations dict explicitly.
    """
    candidate_stations: dict = stations or {}

    # Only fetch forecasts for stations that already have actuals in the DB.
    # This keeps useless backfills out while still allowing any supported
    # resolution source to participate once actual data exists.
    actual_station_ids = {
        row["station_id"]
        for row in conn.execute("SELECT DISTINCT station_id FROM actuals").fetchall()
    }
    active_stations = {
        icao: st for icao, st in candidate_stations.items()
        if icao in actual_station_ids
    }

    if not active_stations:
        logger.info("Open-Meteo forecast backfill skipped: no stations with actuals in DB")
        return BackfillResult()

    total_stations = len(active_stations)
    total_rows = 0
    failed_stations: list[tuple[str, str]] = []
    failed_chunks: list[tuple[str, str, str]] = []
    for i, (icao, station) in enumerate(active_stations.items()):
        logger.info("Station %d/%d: %s (%s)", i + 1, total_stations, icao, station.city)

        # Per-station try/except: a network hiccup or sqlite contention on one
        # station must not abort the whole loop. Each failure logs at WARNING +
        # writes a per-station pipeline_health row so the dashboard surfaces it.
        try:
            chunk_start = start
            station_rows = 0
            station_failed_chunks = 0

            while chunk_start <= end:
                chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), end)

                # Check if already in DB. Unknown/legacy centres must not
                # satisfy completeness; calibration only consumes EXPECTED_MODELS.
                existing = conn.execute(
                    "SELECT COUNT(*) as cnt FROM ("
                    "SELECT target_date FROM forecast_archive "
                    "WHERE station_id = ? AND target_date >= ? AND target_date <= ? "
                    "AND source = 'openmeteo' "
                    "GROUP BY target_date "
                    "HAVING COUNT(DISTINCT CASE "
                    f"WHEN centre IN ({_EXPECTED_MODEL_PLACEHOLDERS}) THEN centre "
                    "END) = ?"
                    ")",
                    (
                        icao,
                        chunk_start.isoformat(),
                        chunk_end.isoformat(),
                        *MODELS,
                        _MIN_COMPLETE_MODELS,
                    ),
                ).fetchone()["cnt"]

                expected_days = (chunk_end - chunk_start).days + 1
                if existing >= expected_days:
                    logger.info("  %s to %s: already in DB (%d days)", chunk_start, chunk_end, existing)
                    chunk_start = chunk_end + timedelta(days=1)
                    continue

                data = fetch_historical(station, chunk_start, chunk_end)
                if data:
                    count = store_forecast_records(conn, icao, data, "openmeteo")
                    non_null_days = sum(1 for mv in data.values() if any(v is not None for v in mv.values()))
                    station_rows += count
                    total_rows += count
                    logger.info(
                        "  %s to %s: %d days with data, %d rows stored",
                        chunk_start, chunk_end, non_null_days, count,
                    )
                else:
                    logger.warning("  %s to %s: FAILED", chunk_start, chunk_end)
                    station_failed_chunks += 1
                    failed_chunks.append((
                        icao,
                        chunk_start.isoformat(),
                        chunk_end.isoformat(),
                    ))

                chunk_start = chunk_end + timedelta(days=1)

            logger.info("%s done: %d total rows", icao, station_rows)
            from hightempbot.db.connection import log_pipeline_health
            if station_failed_chunks:
                log_pipeline_health(
                    conn, icao, "forecast_backfill", "ERROR",
                    f"backfill {start.isoformat()}..{end.isoformat()} -> "
                    f"{station_rows} rows, {station_failed_chunks} failed chunk(s)",
                )
            else:
                log_pipeline_health(
                    conn, icao, "forecast_backfill", "OK",
                    f"backfill {start.isoformat()}..{end.isoformat()} -> {station_rows} rows",
                )
        except Exception as exc:
            logger.error(
                "Backfill failed for station %s; continuing with remaining stations",
                icao, exc_info=True,
            )
            failed_stations.append((icao, str(exc)[:200]))
            from hightempbot.db.connection import log_pipeline_health
            log_pipeline_health(
                conn, icao, "forecast_backfill", "ERROR",
                f"backfill {start.isoformat()}..{end.isoformat()} aborted: "
                f"{type(exc).__name__}: {str(exc)[:160]}",
            )

    if failed_stations:
        logger.warning(
            "Open-Meteo forecast backfill complete with %d failed station(s): %s",
            len(failed_stations),
            ", ".join(f"{icao}({err})" for icao, err in failed_stations[:5]),
        )
    else:
        logger.info("Open-Meteo forecast backfill complete")

    return BackfillResult(
        stations_attempted=total_stations,
        rows_stored=total_rows,
        failed_stations=tuple(failed_stations),
        failed_chunks=tuple(failed_chunks),
    )
