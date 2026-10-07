from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from hightempbot.db.connection import init_db
from hightempbot.resolution.gamma import fetch_gamma_resolution_markets
from hightempbot.stations import ICAO_TO_CITY


def test_fetch_gamma_resolution_markets_uses_enrolled_station_slug_on_cold_start(tmp_path):
    conn = init_db(tmp_path / "test.db")
    conn.execute(
        """INSERT INTO enrolled_stations
        (icao, city, lat, lon, timezone, unit, resolution_source,
         calibration_source, poly_slug, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "KHOU", "Houston", 29.6454, -95.2789, "America/Chicago",
            "F", "wu", "wu", "houston", "LIVE",
        ),
    )
    conn.commit()

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = [{
        "title": "Highest temperature in Houston on May 22, 2026",
        "markets": [{
            "question": "Will the highest temperature in Houston be 90°F or higher on May 22?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomePrices": ["0.9995", "0.0005"],
            "closed": True,
        }],
    }]

    with patch.dict(ICAO_TO_CITY, {}, clear=True), \
         patch("requests.get", return_value=response) as mock_get:
        markets = fetch_gamma_resolution_markets("KHOU", date(2026, 5, 22), conn=conn)

    assert mock_get.call_args.kwargs["params"]["slug"] == (
        "highest-temperature-in-houston-on-may-22-2026"
    )
    assert markets is not None
    assert markets[0]["token_id"] == "yes_tok"
    assert markets[0]["bracket_low"] == 89.5
    assert markets[0]["bracket_high"] is None
    conn.close()


def test_fetch_gamma_resolution_markets_skips_malformed_market(tmp_path):
    conn = init_db(tmp_path / "test.db")
    conn.execute(
        """INSERT INTO enrolled_stations
        (icao, city, lat, lon, timezone, unit, resolution_source,
         calibration_source, poly_slug, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "KHOU", "Houston", 29.6454, -95.2789, "America/Chicago",
            "F", "wu", "wu", "houston", "LIVE",
        ),
    )
    conn.commit()

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = [{
        "title": "Highest temperature in Houston on May 22, 2026",
        "markets": [
            {
                "question": "Will the highest temperature in Houston be 88°F on May 22?",
                "clobTokenIds": "[not-json",
                "outcomePrices": ["bad"],
                "closed": True,
            },
            {
                "question": "Will the highest temperature in Houston be 90°F or higher on May 22?",
                "clobTokenIds": ["yes_tok", "no_tok"],
                "outcomePrices": ["0.9995", "0.0005"],
                "closed": True,
            },
        ],
    }]

    with patch("requests.get", return_value=response):
        markets = fetch_gamma_resolution_markets("KHOU", date(2026, 5, 22), conn=conn)

    assert markets is not None
    assert list(markets) == [1]
    assert markets[1]["token_id"] == "yes_tok"
    assert markets[1]["bracket_low"] == 89.5
    conn.close()
