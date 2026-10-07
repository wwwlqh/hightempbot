"""Per-station scheduler — shared utilities for the betting + resolution ticks.

After the Phase 6 split (U9-U11) and U14 (resolution package extraction), the
actual tick functions live in `betting_tick.py` and `resolution/settler.py`,
plus the data + healing helpers in `market_data.py` and `station_healing.py`.
This module retains only the shared helpers used across those modules:
pipeline_health logging, notification dispatch (`_notify` is re-exported and
imported by sibling scheduler modules + by decision/jobs callers), bracket-
actual matching, and the daily Open-Meteo readiness probe.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

import pytz

from hightempbot.decision.brackets import actual_in_bracket
from hightempbot.resolution.gamma import parse_bracket_bounds
from hightempbot.stations import StationConfig, celsius_to_fahrenheit

# Re-export under the leading-underscore name for the existing test suite,
# which patches/imports `_parse_bracket_bounds` from this module. New
# production code should reference the public name.
_parse_bracket_bounds = parse_bracket_bounds

logger = logging.getLogger(__name__)


# Lazily loaded config for notifications (avoids re-parsing .env on every tick)
_notify_config = None


def _get_notify_config():
    """Load Config once for notification credentials."""
    global _notify_config
    if _notify_config is None:
        try:
            from hightempbot.runtime_config import Config
            _notify_config = Config()
        except Exception:
            pass
    return _notify_config


def _notify(title: str, message: str, stage: str = "unknown", station_id: str = "") -> None:
    """Send a push notification (best-effort, never raises)."""
    try:
        from hightempbot.execution.notify import send_alert
        send_alert(title, message, config=_get_notify_config(), stage=stage, station_id=station_id)
    except Exception:
        pass


def _log_pipeline_health(
    conn: sqlite3.Connection,
    station_id: str,
    stage: str,
    status: str,
    message: str,
) -> None:
    """Best-effort pipeline_health insert (delegates to db.connection)."""
    from hightempbot.db.connection import log_pipeline_health
    log_pipeline_health(conn, station_id, stage, status, message)


def _station_actual_display(actual_tmax: float, station_cfg: StationConfig | None) -> float:
    """Convert local actuals to the display-unit value used for bracket matching."""
    if getattr(station_cfg, "unit", "C") == "F":
        actual_display = celsius_to_fahrenheit(actual_tmax)
    else:
        actual_display = actual_tmax

    return float(round(actual_display))


def _actual_matches_bracket(
    actual_display: float,
    bracket_low: float | None,
    bracket_high: float | None,
) -> bool:
    """Thin shim: delegates to ``brackets.actual_in_bracket``.

    Kept as a name local to this module because the existing tests reference
    it directly. New code should import ``actual_in_bracket`` instead.
    """
    return actual_in_bracket(actual_display, bracket_low, bracket_high)


# Cache the "all 9 models' last-day-N runs are ingested" decision per UTC date.
# Probing scans Open-Meteo metadata starting 00 UTC; once readiness confirms,
# result is cached for the rest of the UTC day so subsequent ticks bypass
# probing instantly. Cache key resets at midnight UTC.
_last_run_ready_cache: dict[date, str] = {}  # {utc_date: "" if ready, else "csv,of,missing,models"}
_last_run_ready_lock = threading.Lock()
_LAST_RUN_PROBE_TTL_S = 300  # 5 minutes — recheck while still waiting

# Open-Meteo `*_seamless` aliases don't expose meta.json — probe the underlying
# physical model. Models without metadata endpoint (e.g. gem) skip the check.
_META_PROBE: dict[str, str | None] = {
    "ecmwf_ifs025": "ecmwf_ifs025",
    "ncep_gfs013": "ncep_gfs013",
    "gfs_seamless": "ncep_gfs013",
    "icon_seamless": "dwd_icon",
    "gem_seamless": None,
    "meteofrance_seamless": "meteofrance_arpege_world025",
    "ukmo_seamless": "ukmo_global_deterministic_10km",
    "knmi_seamless": "knmi_harmonie_arome_europe",
    "dmi_seamless": "dmi_harmonie_arome_europe",
}
_last_run_probe_at: dict[date, float] = {}


def _last_run_ensemble_ready(now_utc: datetime) -> tuple[bool, str]:
    """Probe Open-Meteo metadata for the 9 models; return (ready, missing).

    Ready means: every model's most recent ``last_run_initialisation_time`` is
    on or after yesterday's 00:00 UTC and ``last_run_availability_time`` is in
    the past. Models publish on different cycles, so there is no universal 18Z
    run to wait for.
    Cached per UTC date — once ready, no further probes for the day.
    """
    import requests as _req

    today_utc = now_utc.date()
    target_init_min = (
        datetime(today_utc.year, today_utc.month, today_utc.day, tzinfo=pytz.utc)
        - timedelta(days=1)
    )

    with _last_run_ready_lock:
        cached = _last_run_ready_cache.get(today_utc)
        if cached == "":
            return True, ""
        last_probe = _last_run_probe_at.get(today_utc, 0.0)
        if cached is not None and (time.time() - last_probe) < _LAST_RUN_PROBE_TTL_S:
            return False, cached

    from concurrent.futures import ThreadPoolExecutor
    from hightempbot.ingestion.openmeteo_forecast import MODELS
    now_ts = now_utc.timestamp()
    target_init_ts = target_init_min.timestamp()
    probeable = [m for m in MODELS if _META_PROBE.get(m) is not None]

    def _probe(model: str) -> str | None:
        """Return the model name if missing/stale; None if ready."""
        url = f"https://api.open-meteo.com/data/{_META_PROBE[model]}/static/meta.json"
        try:
            r = _req.get(url, timeout=10)
            r.raise_for_status()
            d = r.json()
            init_ts = d.get("last_run_initialisation_time")
            avail_ts = d.get("last_run_availability_time")
            if init_ts is None or avail_ts is None or init_ts < target_init_ts or avail_ts > now_ts:
                return model
        except Exception:
            return model
        return None

    missing: list[str] = []
    if probeable:
        with ThreadPoolExecutor(max_workers=len(probeable)) as pool:
            for result in pool.map(_probe, probeable):
                if result is not None:
                    missing.append(result)

    missing_csv = ",".join(missing)
    with _last_run_ready_lock:
        _last_run_ready_cache[today_utc] = missing_csv
        _last_run_probe_at[today_utc] = time.time()
    return (not missing), missing_csv


def _target_date_for_ready_cycle(
    now_utc: datetime,
    conn=None,
) -> date | None:
    """Return the market target date for the current UTC readiness cycle.

    Starting at 00Z on UTC date N+1, the scanner waits for the latest complete
    UTC-day-N ensemble and trades the N+1 local-date market. Do not infer this
    from ``forecast_archive``: a backfilled/future row for another station can
    otherwise advance every station to the wrong market date before the exact
    per-station ensemble lock runs.
    """
    del conn
    if now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(pytz.utc)
    return now_utc.date()



