"""Helpers shared by the betting and resolution ticks: health logging,
notifications, and the daily Open-Meteo readiness probe."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

import pytz

from hightempbot.stations import StationConfig, celsius_to_fahrenheit

logger = logging.getLogger(__name__)


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


# {utc_date: "" if ready else "comma,separated,missing,models"}
_last_run_ready_cache: dict[date, str] = {}
_last_run_ready_lock = threading.Lock()
_LAST_RUN_PROBE_TTL_S = 300  # recheck interval while not ready

# meta.json lives on the underlying model; models without one (gem) are skipped.
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
    """``(ready, missing)``: ready once every model has an available run
    initialised since yesterday 00:00 UTC. Cached per UTC date."""
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
    """Today's UTC date. Deliberately not derived from forecast_archive, where
    one station's future row could shift every station's date."""
    del conn
    if now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(pytz.utc)
    return now_utc.date()



