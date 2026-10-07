"""Weather Underground actuals scraper.

Used for resolution tracking (Polymarket settlement) on WU-resolved stations.
Also serves as a fallback calibration source when GSOD is unavailable.

Primary approach: undocumented api.weather.com JSON endpoint.
Fallback: Selenium with undetected-chromedriver (not implemented yet).
"""

from __future__ import annotations

import functools
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _get_wu_api_key() -> str:
    """Return the WU API key from Config(), cached for the process lifetime.

    Default is the legacy wunderground.com bundle public key (see Config).
    An empty string means the operator explicitly disabled WU -- warn so
    they see why every call 401s.
    """
    from hightempbot.runtime_config import get_config
    key = get_config().wu_api_key
    if not key:
        logger.warning("WU_API_KEY explicitly empty — Weather Underground calls will fail")
    return key
_WU_BASE = "https://api.weather.com/v1/location"

# Rate-limit: 0.5s per worker (27 workers at 0.3s caused SSL EOF under sustained load;
# 10 workers at 0.5s = ~8 req/s sustained, safe for multi-hour runs)
_MIN_DELAY_S = 0.5

# WU historical observations for a day can post with a lag past local midnight.
# A cached tmax is only trusted as the day's FINAL high once its fetch happened
# at least this many hours after the station-local END of the target day.
# Earlier fetches may be a partial max(in-progress observations) that would
# poison calibration if served as the final actual.
_CACHE_COMPLETE_MARGIN_HOURS = 6.0
# Legacy cache payloads ({"tmax": ...} with no ``fetched_at`` stamp) are trusted
# only once the target day is at least this old — by then any same-day partial
# has long since been superseded by a complete scrape.
_LEGACY_CACHE_MAX_AGE_DAYS = 7


def _cache_path(icao: str, d: date, cache_dir: Path, unit: str = "F") -> Path:
    return cache_dir / "wu_cache" / icao / f"{d.isoformat()}_{unit}.json"


def _parse_iso_utc(raw: object) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware UTC datetime; None on failure."""
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _station_local_day_end_utc(target_date: date, tz_name: str) -> datetime | None:
    """UTC instant at which the station-local ``target_date`` ends.

    That is the next local midnight, converted to UTC. Returns None when the
    timezone name is unknown so callers can fall back to the age heuristic.
    """
    try:
        import pytz

        tzinfo = pytz.timezone(tz_name)
    except Exception:
        return None
    next_day = target_date + timedelta(days=1)
    local_next_midnight = tzinfo.localize(
        datetime(next_day.year, next_day.month, next_day.day)
    )
    return local_next_midnight.astimezone(timezone.utc)


def _cache_complete_enough(
    data: dict, target_date: date, tz_name: str, now_utc: datetime,
) -> bool:
    """Whether a cached payload can be trusted as a COMPLETE-day final value.

    Guards against serving a partial daily max cached during the station's
    in-progress local day.
    """
    fetched_at_raw = data.get("fetched_at")
    if fetched_at_raw is None:
        # Legacy payload without a fetch stamp: trust only once the target day
        # is old enough that any same-day partial has been overwritten.
        return (now_utc.date() - target_date).days >= _LEGACY_CACHE_MAX_AGE_DAYS
    fetched_at = _parse_iso_utc(fetched_at_raw)
    if fetched_at is None:
        return False
    day_end_utc = _station_local_day_end_utc(target_date, tz_name)
    if day_end_utc is None:
        # Unknown timezone — fall back to the legacy age heuristic on the day.
        return (now_utc.date() - target_date).days >= _LEGACY_CACHE_MAX_AGE_DAYS
    return fetched_at >= day_end_utc + timedelta(hours=_CACHE_COMPLETE_MARGIN_HOURS)


def _delete_bad_cache(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "WU cache: failed to delete bad cache file %s",
            path,
            exc_info=True,
        )


def _read_cached_tmax(
    path: Path,
    icao: str,
    target_date: date,
    tz_name: str | None = None,
    now_utc: datetime | None = None,
) -> float | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        logger.warning(
            "WU cache: failed to read cache for %s %s; refetching",
            icao,
            target_date,
            exc_info=True,
        )
        _delete_bad_cache(path)
        return None
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning(
            "WU cache: corrupt JSON for %s %s; deleting and refetching",
            icao,
            target_date,
            exc_info=True,
        )
        _delete_bad_cache(path)
        return None

    if not isinstance(data, dict):
        logger.warning(
            "WU cache: invalid payload for %s %s; deleting and refetching",
            icao,
            target_date,
        )
        _delete_bad_cache(path)
        return None

    tmax = data.get("tmax")
    if not isinstance(tmax, (int, float)) or isinstance(tmax, bool):
        logger.warning(
            "WU cache: missing/invalid tmax for %s %s; deleting and refetching",
            icao,
            target_date,
        )
        _delete_bad_cache(path)
        return None

    # Completeness gate — only when the caller supplies the station timezone
    # (the live actuals path does; legacy tz-less callers keep prior behavior).
    # Refuse to serve a value cached before the station-local target day was
    # complete: it may be a partial daily max that would poison calibration.
    # Do NOT delete — return None so the caller refetches and overwrites the
    # entry with a stamped final value.
    if tz_name is not None:
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        if not _cache_complete_enough(data, target_date, tz_name, now_utc):
            logger.debug(
                "WU cache: entry for %s %s not yet complete-day; refetching",
                icao,
                target_date,
            )
            return None

    return float(tmax)



# ICAO prefix → ISO 3166 country code for WU API URL
# Must cover all prefixes that auto-enrollment can discover.
_ICAO_COUNTRY: dict[str, str] = {
    "K": "US",     # US stations (KLGA, KLAX, etc.)
    "C": "CA",     # Canada (CYYZ)
    "SB": "BR",    # Brazil (SBGR)
    "SA": "AR",    # Argentina (SAEZ Buenos Aires)
    "MM": "MX",    # Mexico (MMMX)
    "MP": "PA",    # Panama (MPTO Panama City)
    "EG": "GB",    # UK (EGLC)
    "EH": "NL",    # Netherlands (EHAM Amsterdam)
    "EF": "FI",    # Finland (EFHK Helsinki)
    "ED": "DE",    # Germany (EDDM Munich)
    "EP": "PL",    # Poland (EPWA Warsaw)
    "LF": "FR",    # France (LFPG)
    "LE": "ES",    # Spain (LEMD Madrid)
    "LI": "IT",    # Italy (LIMC Milan)
    "RJ": "JP",    # Japan (RJTT)
    "RK": "KR",    # South Korea (RKSI, RKPK Busan)
    "Z": "CN",     # China — all Z-prefix ICAOs (ZBAA, ZSPD, ZGSZ, ZUCK, ZHHH, ZUUU)
    "WS": "SG",    # Singapore (WSSS)
    "WI": "ID",    # Indonesia (WIII Jakarta)
    "WM": "MY",    # Malaysia (WMKK Kuala Lumpur)
    "NZ": "NZ",    # New Zealand (NZWN)
    "VI": "IN",    # India (VILK)
    "LT": "TR",    # Turkey (LTAC)
    "LL": "IL",    # Israel (LLBG Tel Aviv)
    "UU": "RU",    # Russia (UUWW Moscow)
    "RC": "TW",    # Taiwan (RCTP Taipei)
    "VH": "HK",    # Hong Kong (VHHH)
    "RP": "PH",    # Philippines (RPLL Manila)
    "OP": "PK",    # Pakistan (OPKC Karachi)
    "OE": "SA",    # Saudi Arabia (OEJN Jeddah)
    "DN": "NG",    # Nigeria (DNMM Lagos)
    "FA": "ZA",    # South Africa (FACT Cape Town)
}


def _get_wu_icao(icao: str) -> str:
    """Return the WU ICAO code (identity — no overrides currently needed)."""
    return icao


def _get_country_code(icao: str) -> str:
    """Map ICAO code to ISO country code for WU API."""
    wu_icao = _get_wu_icao(icao)
    if wu_icao[:2] in _ICAO_COUNTRY:
        return _ICAO_COUNTRY[wu_icao[:2]]
    if wu_icao[:1] in _ICAO_COUNTRY:
        return _ICAO_COUNTRY[wu_icao[:1]]
    logger.warning(
        "No ICAO country mapping for %s; add prefix %r to _ICAO_COUNTRY",
        icao,
        wu_icao[:2],
    )
    return ""


def fetch_wu_tmax(
    icao: str,
    target_date: date,
    cache_dir: Path,
    unit: str = "F",
    tz: str | None = None,
) -> float | None:
    """Fetch the WU historical high for a single station-day.

    Returns tmax in the station's native unit (°F for US, °C for intl).
    Uses valid cached data when available; invalid cache files are deleted and
    refetched.

    When ``tz`` (the station's IANA timezone) is supplied, a cached value is
    only served if it was fetched after the station-local target day was
    complete — see ``_cache_complete_enough``. tz-less callers keep the prior
    unconditional cache-serve behavior.
    """
    cached = _cache_path(icao, target_date, cache_dir, unit)
    if cached.exists():
        tmax_cached = _read_cached_tmax(cached, icao, target_date, tz_name=tz)
        if tmax_cached is not None:
            return tmax_cached

    # Try the undocumented JSON endpoint. Pass apiKey via the params argument
    # so it is not concatenated into the URL string — exception traces and
    # urllib3 debug logs would otherwise leak the key.
    units_param = "e" if unit == "F" else "m"  # e=imperial, m=metric
    date_str = target_date.strftime("%Y%m%d")
    wu_icao = _get_wu_icao(icao)
    country = _get_country_code(icao)
    if not country:
        logger.warning("WU API skipped for %s %s: unknown ICAO country prefix", icao, target_date)
        return None
    url = f"{_WU_BASE}/{wu_icao}:9:{country}/observations/historical.json"
    params = {
        "apiKey": _get_wu_api_key(),
        "units": units_param,
        "startDate": date_str,
    }

    try:
        time.sleep(_MIN_DELAY_S)
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
    except requests.exceptions.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning(
            "WU API failed for %s %s (%s status=%s)",
            icao, target_date, type(exc).__name__, status,
        )
        return None
    except Exception as exc:
        logger.warning(
            "WU API failed for %s %s (%s)",
            icao, target_date, type(exc).__name__,
        )
        return None

    # Extract the day's high from hourly observations
    # WU API returns individual obs with "temp" field, not a daily summary
    observations = payload.get("observations", [])
    if not observations:
        logger.warning("WU: no observations for %s %s", icao, target_date)
        return None

    temps = [
        obs.get("temp")
        for obs in observations
        if obs.get("temp") is not None
    ]
    if not temps:
        return None

    tmax = max(temps)

    # Cache the result, stamped with the UTC fetch time so a later read can
    # verify the value was captured after the station-local day completed.
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(
            json.dumps({
                "tmax": tmax,
                "icao": icao,
                "date": str(target_date),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }),
            encoding="utf-8",
        )
    except OSError:
        logger.warning(
            "WU cache: failed to write cache for %s %s; continuing without cache",
            icao,
            target_date,
            exc_info=True,
        )
    return tmax
