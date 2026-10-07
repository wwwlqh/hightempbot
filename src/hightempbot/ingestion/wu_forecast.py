"""WU live 5-day forecast (°C), used only by the WU consensus gate (currently off)."""

from __future__ import annotations

import logging
import threading
import time
from datetime import date

import requests

from hightempbot.ingestion.sources.wu import (
    _get_country_code,
    _get_wu_api_key,
    _get_wu_icao,
)

logger = logging.getLogger(__name__)

_WU_BASE = "https://api.weather.com/v1/location"
_FORECAST_PATH = "forecast/daily/5day.json"

_MIN_DELAY_S = 0.5

# {(icao, target_date): (tmax_c or None, expires_at)}, 15-minute TTL.
_CACHE_TTL_S = 15 * 60
_cache: dict[tuple[str, str], tuple[float | None, float]] = {}
_cache_lock = threading.Lock()


def fetch_wu_forecast(icao: str, target_date: date) -> float | None:
    """WU's forecast high for ``target_date`` in °C; None if unavailable (fail closed)."""
    target_iso = target_date.isoformat()
    key = (icao, target_iso)
    now = time.time()

    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and entry[1] > now:
            return entry[0]

    # Metric units; apiKey in params so it isn't logged with the URL.
    wu_icao = _get_wu_icao(icao)
    country = _get_country_code(icao)
    url = f"{_WU_BASE}/{wu_icao}:9:{country}/{_FORECAST_PATH}"
    params = {
        "apiKey": _get_wu_api_key(),
        "units": "m",
        "language": "en-US",
    }

    try:
        time.sleep(_MIN_DELAY_S)
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        # No exc_info: the traceback would include the key.
        logger.warning("WU forecast API failed for %s %s", icao, target_iso)
        _store(key, None, now)
        return None

    tmax_c = _extract_tmax_for_date(payload, target_iso)
    _store(key, tmax_c, now)
    return tmax_c


def _store(key: tuple[str, str], value: float | None, now: float) -> None:
    with _cache_lock:
        _cache[key] = (value, now + _CACHE_TTL_S)


def _extract_tmax_for_date(payload: dict, target_iso: str) -> float | None:
    """``max_temp`` for ``target_iso``, else the day daypart's temp. Never the
    night temp: that's the overnight low."""
    forecasts = payload.get("forecasts") if isinstance(payload, dict) else None
    if not isinstance(forecasts, list):
        logger.warning("WU forecast: unexpected payload shape (no forecasts list)")
        return None

    for day in forecasts:
        if not isinstance(day, dict):
            continue
        local = (day.get("fcst_valid_local") or day.get("validTimeLocal") or "")
        if not isinstance(local, str) or not local.startswith(target_iso):
            continue
        max_temp = day.get("max_temp")
        if max_temp is None:
            day_dp = day.get("day")
            if isinstance(day_dp, dict) and day_dp.get("temp") is not None:
                max_temp = day_dp.get("temp")
        if max_temp is None:
            continue
        try:
            return float(max_temp)
        except (TypeError, ValueError):
            return None

    logger.info("WU forecast: no entry for target %s in payload", target_iso)
    return None


def clear_cache() -> None:
    """Test hook — wipe the in-memory cache."""
    with _cache_lock:
        _cache.clear()
