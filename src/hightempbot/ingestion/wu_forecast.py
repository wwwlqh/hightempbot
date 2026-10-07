"""Weather Underground live forecast ingester.

Used by the WU consensus gate (`execution.decision._gate_wu_consensus`) to
verify that a stale-ensemble bet candidate still agrees with WU's freshest
forecast before the bot places it.

This is **forecast-only and live-only**. Historical archives of past WU
forecast issuances are not available — the forecast is overwritten as time
progresses. For past observed tmax (resolution), see
`ingestion.sources.wu.fetch_wu_tmax`.

Same IBM Weather Company v1 endpoint family + auth as the actuals scraper;
hits the `/forecast/daily/5day.json` route instead of `/observations/historical`.
Returns the daily max for the requested target_date, normalized to °C.
"""

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

# Match WU concurrency budget: at most one in-flight per worker, 0.5s spacing
# (same as wu.py: 27 workers @ 0.3s caused SSL EOF, 10 @ 0.5s is stable).
_MIN_DELAY_S = 0.5

# 15-min in-memory TTL cache. Aligned to bot scan cadence: multiple candidates
# evaluated in the same scan tick share one scrape; stale entries trigger a
# re-scrape on the next tick. Keyed on (icao, target_date_iso). Value is
# (cached_tmax_c_or_None, expires_at_unix_ts).
_CACHE_TTL_S = 15 * 60
_cache: dict[tuple[str, str], tuple[float | None, float]] = {}
_cache_lock = threading.Lock()


def fetch_wu_forecast(icao: str, target_date: date) -> float | None:
    """Fetch WU's live forecasted daily max for ``target_date`` in °C.

    Returns ``None`` when:
      * the API call fails (network, 4xx, 5xx),
      * the response does not contain ``target_date``,
      * the response shape is unrecognized.

    Callers should treat ``None`` as "WU unavailable, fail closed" — the
    consensus gate distinguishes this from "WU disagrees".
    """
    target_iso = target_date.isoformat()
    key = (icao, target_iso)
    now = time.time()

    # Cache check (TTL).
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and entry[1] > now:
            return entry[0]

    # Always request metric units so the response is in °C; no F→C math
    # downstream. Pass apiKey via the params argument so it doesn't end up
    # in URLs that exception traces or urllib3 debug logs would record.
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
        # Log only the icao/date and a sanitised type — do NOT pass exc_info,
        # which would re-include the URL+token in the traceback.
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
    """Pick the daily max for ``target_iso`` out of WU's v1 forecast payload.

    The v1 response carries a ``forecasts`` list with per-day entries that
    include ``max_temp`` and a local-time validity stamp. WU sometimes returns
    ``max_temp`` as ``None`` for the current local day after the daytime
    portion has passed (overnight forecasts only have a ``night`` daypart);
    in that case, fall back ONLY to the ``day`` daypart's ``temp`` (which is
    the daytime high). NEVER fall back to ``night.temp`` — that field is the
    overnight low and would silently substitute a value ~10°C below the true
    daily max, flipping NO-side gate verdicts onto the wrong side.
    """
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
            # Daytime daypart only — never night, which is the overnight low.
            day_dp = day.get("day")
            if isinstance(day_dp, dict) and day_dp.get("temp") is not None:
                max_temp = day_dp.get("temp")
        if max_temp is None:
            continue
        try:
            return float(max_temp)  # already °C since units=m
        except (TypeError, ValueError):
            return None

    logger.info("WU forecast: no entry for target %s in payload", target_iso)
    return None


def clear_cache() -> None:
    """Test hook — wipe the in-memory cache."""
    with _cache_lock:
        _cache.clear()
