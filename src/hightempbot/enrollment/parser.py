"""Resolution source, station id and unit from a Polymarket event: a WU URL,
else HKO for Hong Kong, else None."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


_HKO_CITY_TO_STATION_ID = {
    "hong kong": "VHHH",
}


@dataclass
class EnrollmentCandidate:
    """Parsed result from a Polymarket temperature event."""

    city: str
    icao: str
    resolution_source: str   # e.g. "wu", "hko"
    unit: str                # "F" or "C"


def parse_resolution_source(event: dict) -> EnrollmentCandidate | None:
    """Parse a Polymarket event to extract station info for enrollment."""
    res_url = event.get("resolutionSource", "") or ""
    description = event.get("description", "") or ""
    title = event.get("title", "") or ""

    if "highest temperature" not in title.lower():
        logger.warning("Rejecting non-highest-temp event: %s", title[:80])
        return None

    city = _parse_city_from_title(title)
    if not city:
        markets = event.get("markets", [])
        if markets:
            question = markets[0].get("question", "")
            match = re.search(r"in (.+?) be ", question)
            if match:
                city = match.group(1).strip()
    if not city:
        logger.warning("Cannot parse city name from event: %s", title[:80])
        return None

    unit = _parse_unit(description, event)

    if "wunderground.com" in res_url:
        icao = _parse_wu_icao(res_url)
        if icao:
            if icao.startswith(("K", "C")) or icao[:2] in ("MM", "MP"):
                unit = "F"
            return EnrollmentCandidate(
                city=city,
                icao=icao,
                resolution_source="wu",
                unit=unit,
            )

    if _looks_like_hko_source(res_url, description):
        station_id = _parse_hko_station_id(city)
        if station_id:
            return EnrollmentCandidate(
                city=city,
                icao=station_id,
                resolution_source="hko",
                unit=unit,
            )

    logger.warning(
        "source: UNKNOWN - skipping auto-enrollment for %s (resolutionSource=%s)",
        city,
        res_url[:80] if res_url else "N/A",
    )
    return None


def _parse_city_from_title(title: str) -> str | None:
    """Extract city name from event title."""
    match = re.search(r"temperature in (.+?) on ", title, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _parse_unit(description: str, event: dict) -> str:
    """Determine F or C from market questions, description, or fallback."""
    for market in event.get("markets", []):
        question = market.get("question", "") or ""
        if "°F" in question or "Â°F" in question:
            return "F"
        if "°C" in question or "Â°C" in question:
            return "C"

    text = description or ""
    lower = text.lower()
    if "fahrenheit" in lower or "°F" in text or "Â°F" in text:
        return "F"
    if "celsius" in lower or "°C" in text or "Â°C" in text:
        return "C"
    return "C"


def _parse_wu_icao(url: str) -> str | None:
    """Extract ICAO from WU URL."""
    parts = url.rstrip("/").split("/")
    if len(parts) < 2:
        return None
    icao = parts[-1].upper()
    if re.match(r"^[A-Z0-9]{3,4}$", icao):
        return icao
    return None


def _looks_like_hko_source(resolution_source: str, description: str) -> bool:
    """Return whether the market resolves from Hong Kong Observatory data."""
    haystack = f"{resolution_source}\n{description}".lower()
    return "hong kong observatory" in haystack or "weather.gov.hk" in haystack


def _parse_hko_station_id(city: str) -> str | None:
    """Map an HKO city market to the runtime station id used by the bot."""
    return _HKO_CITY_TO_STATION_ID.get(city.strip().lower())
