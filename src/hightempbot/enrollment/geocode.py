"""City → lat/lon/timezone via the Open-Meteo geocoding API (cached in memory)."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

# In-memory cache: {city_name_lower: GeoResult}
_cache: dict[str, GeoResult] = {}


@dataclass
class GeoResult:
    """Geocoding result for a city."""
    lat: float
    lon: float
    timezone: str
    country_code: str


def geocode_city(city_name: str, max_attempts: int = 3) -> GeoResult | None:
    """Geocode a Polymarket city name, with retries. None on failure (cached
    only after all retries fail)."""
    key = city_name.lower().strip()
    if key in _cache:
        return _cache[key]

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(
                _GEOCODING_URL,
                params={"name": city_name, "count": 1, "language": "en"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            if not results:
                # HTTP 200 + empty results = city genuinely not found, don't retry
                logger.warning("Geocoding returned no results for %s", city_name)
                return None

            top = results[0]
            result = GeoResult(
                lat=top["latitude"],
                lon=top["longitude"],
                timezone=top.get("timezone", "UTC"),
                country_code=top.get("country_code", ""),
            )
            _cache[key] = result
            logger.info(
                "Geocoded %s → lat=%.4f, lon=%.4f, tz=%s, cc=%s",
                city_name, result.lat, result.lon, result.timezone, result.country_code,
            )
            return result

        except Exception:
            if attempt < max_attempts:
                delay = 2 ** attempt + random.uniform(0, 1)
                logger.warning("Geocoding %s failed (attempt %d/%d), retrying in %.1fs",
                               city_name, attempt, max_attempts, delay, exc_info=True)
                time.sleep(delay)
            else:
                logger.warning("Geocoding %s failed after %d attempts",
                               city_name, max_attempts, exc_info=True)
                return None

    return None
