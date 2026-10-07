from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from hightempbot.db.connection import init_db
from hightempbot.ingestion.polymarket_prices import find_temperature_events


def test_find_temperature_events_bootstraps_station_registry(tmp_path):
    conn = init_db(tmp_path / "test.db")
    conn.execute(
        """INSERT INTO enrolled_stations
        (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source, poly_slug, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("KDAL", "Dallas", 32.85, -96.85, "America/Chicago", "F", "wu", "wu", "dallas", "DRY_RUN"),
    )
    conn.commit()

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = [{
        "title": "Highest temperature in Dallas on April 17, 2026",
        "markets": [{
            "question": "Will the highest temperature in Dallas be between 62-63°F on April 17?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomePrices": ["0.41", "0.59"],
            "volume": 1234,
            "conditionId": "market_1",
        }],
    }]

    with patch.dict("hightempbot.stations.ICAO_TO_CITY", {}, clear=True), \
         patch.dict("hightempbot.ingestion.polymarket_prices._CITY_TO_ICAO", {}, clear=True), \
         patch("requests.get", return_value=response) as mock_get:
        events = find_temperature_events(target_date=date(2026, 4, 17), conn=conn)

    assert mock_get.called
    assert len(events) == 1
    assert events[0]["station_id"] == "KDAL"
    assert events[0]["market_date"] == "2026-04-17"
    conn.close()
