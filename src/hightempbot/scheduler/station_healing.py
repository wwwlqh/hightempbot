"""Betting-tick self-repair: fix a wrong station unit, seed a missing LUT."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date

from hightempbot.scheduler.market_data import _fetch_market_data
from hightempbot.scheduler.station_scanner import _log_pipeline_health
from hightempbot.stations import StationConfig

logger = logging.getLogger(__name__)


def _heal_station_unit_if_wrong(
    conn,
    station_id: str,
    current_unit: str,
    market_data: dict,
) -> None:
    """Update enrolled_stations.unit to match the market labels. Never raises."""
    inferred = None
    label = ""
    for mkt in market_data.values():
        label = mkt.get("bracket_label") or ""
        if "°F" in label:
            inferred = "F"
            break
        if "°C" in label:
            inferred = "C"
            break
    if inferred is None or inferred == current_unit:
        return

    try:
        conn.execute(
            "UPDATE enrolled_stations SET unit = ? WHERE icao = ? AND unit != ?",
            (inferred, station_id, inferred),
        )
        conn.commit()
        logger.warning(
            "Auto-healed station unit %s: %s -> %s (from bracket label %r)",
            station_id, current_unit, inferred, label,
        )
    except Exception:
        logger.warning(
            "Auto-heal failed for %s (tried %s -> %s)",
            station_id, current_unit, inferred, exc_info=True,
        )




def _attempt_seed_missing_lut(
    conn: sqlite3.Connection,
    station: StationConfig,
    target_date: date,
) -> bool:
    """Try to seed a missing LUT from the readiness-cycle target-date brackets."""
    from hightempbot.calibration.lut import _brackets_for_station, seed_lut_from_history

    station_id = station.icao
    parsed_brackets = 0
    market_data = _fetch_market_data(station_id, target_date, conn=conn)
    parsed_brackets += sum(
        1
        for mkt in market_data.values()
        if mkt.get("bracket_label") is not None
        or mkt.get("bracket_low") is not None
        or mkt.get("bracket_high") is not None
    )

    if parsed_brackets == 0:
        return False

    try:
        brackets = _brackets_for_station(conn, station_id, target_date.isoformat())
        if not brackets:
            return False
        days, triples = seed_lut_from_history(conn, station_id, brackets=brackets)
    except Exception:
        logger.warning("On-demand LUT seed failed for %s", station_id, exc_info=True)
        return False

    if triples <= 0:
        return False

    _log_pipeline_health(
        conn,
        station_id,
        "lut",
        "OK",
        f"Seeded missing LUT from live brackets ({days} days / {triples} triples)",
    )
    logger.info(
        "Seeded missing LUT for %s from live brackets: %d days / %d triples",
        station_id,
        days,
        triples,
    )
    return True
