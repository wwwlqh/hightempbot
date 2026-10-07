"""Station config and the runtime registry, loaded from ``enrolled_stations``."""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StationConfig:
    icao: str
    city: str
    lat: float
    lon: float
    timezone: str                # IANA timezone (e.g. "America/New_York")
    unit: str                    # "F" or "C" — Polymarket resolution unit
    resolution_source: str       # e.g. "wu", "cwa", "ims"
    poly_slug: str = ""          # defaults to the slugified city
    notes: str = ""


def _city_to_slug(city: str) -> str:
    """Convert city name to Polymarket URL slug.

    'São Paulo' → 'sao-paulo', 'Los Angeles' → 'los-angeles'
    """
    nfkd = unicodedata.normalize("NFKD", city)
    ascii_text = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def get_poly_slug(station: StationConfig) -> str:
    """Get Polymarket city slug — explicit override or auto-derived from city name."""
    return station.poly_slug if station.poly_slug else _city_to_slug(station.city)


def _row_value(row, key: str, index: int):
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return row[index]


def poly_slug_for_station_id(station_id: str, conn=None) -> str | None:
    """Polymarket city slug, from the registry or (before it's loaded) the DB."""
    with _registry_lock:
        slug = ICAO_TO_CITY.get(station_id)
        station = STATIONS.get(station_id)
    if slug:
        return slug
    if station is not None:
        slug = get_poly_slug(station)
        if slug:
            with _registry_lock:
                ICAO_TO_CITY[station_id] = slug
            return slug

    if conn is None:
        return None

    try:
        row = conn.execute(
            """
            SELECT city, poly_slug
            FROM enrolled_stations
            WHERE icao = ? AND status IN ('DRY_RUN', 'LIVE')
            """,
            (station_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None

    city = str(_row_value(row, "city", 0) or "")
    explicit_slug = str(_row_value(row, "poly_slug", 1) or "")
    slug = explicit_slug or (_city_to_slug(city) if city else "")
    if not slug:
        return None

    with _registry_lock:
        ICAO_TO_CITY[station_id] = slug
    return slug


SUPPORTED_LIVE_SOURCES = frozenset({"wu"})


def supports_live_resolution_source(resolution_source: str | None) -> bool:
    """Return whether a source is supported for live scraping/betting."""
    return (resolution_source or "").lower() in SUPPORTED_LIVE_SOURCES


# Runtime registry, filled from enrolled_stations at startup.

# Guards STATIONS and ICAO_TO_CITY (enrollment writes while ticks iterate).
_registry_lock = threading.Lock()

STATIONS: dict[str, StationConfig] = {}

ICAO_TO_CITY: dict[str, str] = {}


def load_enrolled_stations(conn) -> dict[str, StationConfig]:
    """DRY_RUN and LIVE stations from the database."""
    try:
        rows = conn.execute(
            "SELECT * FROM enrolled_stations WHERE status IN ('DRY_RUN', 'LIVE')"
        ).fetchall()
    except sqlite3.OperationalError:
        logger.warning(
            "load_enrolled_stations: enrolled_stations table missing or unreadable",
            exc_info=True,
        )
        return {}

    result: dict[str, StationConfig] = {}
    for row in rows:
        result[row["icao"]] = StationConfig(
            icao=row["icao"],
            city=row["city"],
            lat=row["lat"],
            lon=row["lon"],
            timezone=row["timezone"],
            unit=row["unit"],
            resolution_source=row["resolution_source"],
            poly_slug=row["poly_slug"],
        )
    return result


def get_all_stations(conn=None) -> dict[str, StationConfig]:
    """All active stations; {} without a connection."""
    if conn is None:
        return {}
    return load_enrolled_stations(conn)


def register_enrolled_station(station: StationConfig) -> None:
    """Add a DRY_RUN/LIVE station to the runtime maps."""
    slug = get_poly_slug(station)
    with _registry_lock:
        STATIONS[station.icao] = station
        ICAO_TO_CITY[station.icao] = slug
    try:
        from hightempbot.ingestion import polymarket_prices as _poly
        _poly._CITY_TO_ICAO[slug] = station.icao
        if station.city:
            _poly._CITY_TO_ICAO[_city_to_slug(station.city)] = station.icao
    except Exception:
        logger.debug(
            "register_enrolled_station: polymarket_prices side-effect failed for %s",
            station.icao, exc_info=True,
        )


def fahrenheit_to_celsius(f: float) -> float:
    """Convert Fahrenheit to Celsius."""
    return (f - 32.0) * 5.0 / 9.0


def celsius_to_fahrenheit(c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return c * 9.0 / 5.0 + 32.0
