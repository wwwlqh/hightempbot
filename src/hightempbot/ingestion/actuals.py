"""Actuals ingestion router for live station-day scrapes."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from hightempbot.stations import (
    StationConfig,
    fahrenheit_to_celsius,
    supports_live_resolution_source,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


@dataclass
class ActualRow:
    station_id: str
    local_date: date
    tmax_celsius: float
    source: str


def supports_actual_scrape(station: StationConfig) -> bool:
    """Return whether the station should be scraped for live actuals."""
    return supports_live_resolution_source(station.resolution_source)


def _station_local_today(station: StationConfig) -> date:
    """The station's current local date."""
    import pytz

    return datetime.now(pytz.timezone(station.timezone)).date()


def _fetch_wu(station: StationConfig, target_date: date, data_dir: Path) -> float | None:
    """Fetch from Weather Underground."""
    from hightempbot.ingestion.sources.wu import fetch_wu_tmax

    # Pass tz so the cache rejects values saved before the day ended.
    tmax_raw = fetch_wu_tmax(
        station.icao, target_date, data_dir,
        unit=station.unit, tz=station.timezone,
    )
    if tmax_raw is None:
        return None
    if station.unit == "F":
        return fahrenheit_to_celsius(tmax_raw)
    return tmax_raw


def _fetch_from_resolution_source(
    station: StationConfig, target_date: date, data_dir: Path
) -> ActualRow | None:
    """Fetch from the station's configured resolution source."""
    src = station.resolution_source
    icao = station.icao
    tmax: float | None = None

    if src == "wu":
        tmax = _fetch_wu(station, target_date, data_dir)
    else:
        logger.info("Skipping actual scrape for %s: unsupported source %s", icao, src)
        return None

    if tmax is None:
        return None

    return ActualRow(
        station_id=icao,
        local_date=target_date,
        tmax_celsius=tmax,
        source=src,
    )


def fetch_actual(
    station_id: str,
    target_date: date,
    data_dir: Path,
    station_override: StationConfig | None = None,
) -> ActualRow | None:
    """Fetch tmax for a single station-day with retry logic."""
    station = station_override
    if station is None:
        logger.error("No station config for %s; caller must pass station_override", station_id)
        return None
    if not supports_actual_scrape(station):
        logger.info(
            "Skipping actual scrape for %s %s: resolution_source=%s is unsupported",
            station_id,
            target_date,
            station.resolution_source,
        )
        return None

    import random
    import time

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            row = _fetch_from_resolution_source(station, target_date, data_dir)
        except Exception as exc:
            logger.warning(
                "Actual scrape attempt %d/%d raised for %s %s (%s)",
                attempt,
                MAX_RETRIES,
                station_id,
                target_date,
                type(exc).__name__,
                exc_info=True,
            )
            row = None
        if row is not None:
            return row
        if attempt < MAX_RETRIES:
            backoff = 2 ** attempt + random.uniform(0, 1)
            logger.debug(
                "Attempt %d/%d failed for %s %s, retrying in %.1fs",
                attempt,
                MAX_RETRIES,
                station_id,
                target_date,
                backoff,
            )
            time.sleep(backoff)

    logger.warning(
        "%s %s - all %d attempts failed, marking missing",
        station_id,
        target_date,
        MAX_RETRIES,
    )
    return None


def upsert_actual(conn: sqlite3.Connection, row: ActualRow) -> None:
    """Write an ActualRow to the actuals table."""
    from hightempbot.persistence.ledger import backfill_resolved_actual_for_station_date

    conn.execute(
        "INSERT OR REPLACE INTO actuals "
        "(station_id, local_date, tmax_celsius, source) "
        "VALUES (?, ?, ?, ?)",
        (row.station_id, row.local_date.isoformat(), row.tmax_celsius, row.source),
    )
    conn.commit()
    n_backfilled = backfill_resolved_actual_for_station_date(
        conn,
        row.station_id,
        row.local_date.isoformat(),
    )
    if n_backfilled > 0:
        logger.info(
            "Backfilled actual_tmax for %d resolved bet(s): %s %s",
            n_backfilled,
            row.station_id,
            row.local_date,
        )


def fetch_and_store(
    station_id: str,
    target_date: date,
    conn: sqlite3.Connection,
    data_dir: Path,
    station_override: StationConfig | None = None,
) -> ActualRow | None:
    """Fetch and persist an actual."""
    row = fetch_actual(station_id, target_date, data_dir, station_override=station_override)
    if row is not None:
        upsert_actual(conn, row)
    return row


def backfill_missing_actuals(
    conn: sqlite3.Connection,
    station: StationConfig,
    data_dir: Path,
    start_date: date,
    end_date: date,
) -> int:
    """Fetch missing actuals for ``[start_date, end_date)``, never past the
    station's local yesterday (today's high isn't final). Returns rows stored."""
    if not supports_actual_scrape(station):
        return 0

    from datetime import timedelta as _td

    station_id = station.icao
    # Never iterate past the station's in-progress local day.
    effective_end = min(end_date, _station_local_today(station))
    rows = conn.execute(
        "SELECT local_date FROM actuals WHERE station_id = ? "
        "AND local_date >= ? AND local_date < ?",
        (station_id, start_date.isoformat(), effective_end.isoformat()),
    ).fetchall()
    existing = {r["local_date"] for r in rows}

    d = start_date
    n_added = 0
    while d < effective_end:
        if d.isoformat() not in existing:
            try:
                row = fetch_and_store(
                    station_id, d, conn, data_dir, station_override=station,
                )
                if row is not None:
                    n_added += 1
                    logger.info(
                        "Backfilled missing actual: %s %s -> %.1f°C from %s",
                        station_id, d, row.tmax_celsius, row.source,
                    )
            except Exception:
                logger.warning(
                    "Backfill fetch failed for %s %s",
                    station_id, d, exc_info=True,
                )
        d += _td(days=1)
    return n_added
