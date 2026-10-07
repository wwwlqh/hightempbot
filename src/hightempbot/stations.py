"""Station configuration and registry — all stations loaded from enrolled_stations DB.

Auto-enrollment pipeline discovers markets on Polymarket, parses resolution
source + ICAO from WU URLs, geocodes lat/lon/timezone, and populates the DB.
At startup, main.py calls get_all_stations(conn) and register_enrolled_station()
to build the runtime lookup maps.
"""

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
    poly_slug: str = ""          # Polymarket city slug override (auto-derived from city if empty)
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
    """Return the Polymarket city slug for a station.

    Long-running bot processes populate ``ICAO_TO_CITY`` at startup, but CLI
    helpers and one-off worker processes can call market-resolution code before
    that registry has been bootstrapped. Fall back to ``enrolled_stations`` so
    Gamma lookups don't silently build ``None`` slugs on cold start.
    """
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


# ---------------------------------------------------------------------------
# Runtime station registry — populated from enrolled_stations DB at startup
# ---------------------------------------------------------------------------

# `_registry_lock` guards STATIONS and ICAO_TO_CITY against concurrent
# mutation by the enrollment scan job (jobs.py) and reads by scheduler
# threads (station_scanner.py). Without it the scanner can iterate the
# dict while enrollment is rebinding entries — CPython's GIL makes single
# dict ops atomic, but iteration is not.
_registry_lock = threading.Lock()

STATIONS: dict[str, StationConfig] = {}

# Auto-derived ICAO → Polymarket slug mapping. Initially empty; populated
# by `register_enrolled_station` at startup and by the auto-enrollment
# scan thereafter. The original module-level comprehension over STATIONS
# evaluated to {} every time anyway (STATIONS was empty at import) — the
# explicit empty dict here makes that contract obvious.
ICAO_TO_CITY: dict[str, str] = {}


def load_enrolled_stations(conn) -> dict[str, StationConfig]:
    """Load auto-enrolled stations from the database.

    Returns stations with status IN ('DRY_RUN', 'LIVE') as StationConfig objects.
    """
    try:
        rows = conn.execute(
            "SELECT * FROM enrolled_stations WHERE status IN ('DRY_RUN', 'LIVE')"
        ).fetchall()
    except sqlite3.OperationalError:
        # Most common cause: schema migration hasn't run yet on this DB.
        # Returning {} lets boot continue; warn so the operator can see it.
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
    """Get all stations from the enrolled_stations DB table.

    If conn is None, returns empty dict (no stations available without DB).
    """
    if conn is None:
        return {}
    return load_enrolled_stations(conn)


def register_enrolled_station(station: StationConfig) -> None:
    """Register a newly enrolled station in the runtime lookup maps.

    Updates ICAO_TO_CITY so discovery and market fetch can find the station.
    Called after a station transitions to DRY_RUN or LIVE. Mutations are
    serialized via `_registry_lock` so concurrent enrollment scans cannot
    interleave with reader iteration.
    """
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
