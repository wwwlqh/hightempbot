from unittest.mock import patch

import pytest

from hightempbot.db.connection import init_db
from hightempbot.stations import (
    ICAO_TO_CITY,
    celsius_to_fahrenheit,
    fahrenheit_to_celsius,
    poly_slug_for_station_id,
)


class TestUnitConversions:
    def test_f_to_c(self):
        assert fahrenheit_to_celsius(32.0) == pytest.approx(0.0)
        assert fahrenheit_to_celsius(212.0) == pytest.approx(100.0)
        assert fahrenheit_to_celsius(95.0) == pytest.approx(35.0)

    def test_c_to_f(self):
        assert celsius_to_fahrenheit(0.0) == pytest.approx(32.0)
        assert celsius_to_fahrenheit(100.0) == pytest.approx(212.0)
        assert celsius_to_fahrenheit(35.0) == pytest.approx(95.0)

    def test_round_trip(self):
        for temp in [0, 20, 35, -10, 42.5]:
            assert fahrenheit_to_celsius(celsius_to_fahrenheit(temp)) == pytest.approx(temp)


class TestPolySlugLookup:
    def test_uses_runtime_registry(self):
        with patch.dict(ICAO_TO_CITY, {"KHOU": "houston"}, clear=True):
            assert poly_slug_for_station_id("KHOU") == "houston"

    def test_falls_back_to_enrolled_stations_and_caches(self, tmp_path):
        conn = init_db(tmp_path / "test.db")
        conn.execute(
            """INSERT INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source,
             calibration_source, poly_slug, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "SAEZ", "Buenos Aires", -34.8222, -58.5358, "America/Argentina/Buenos_Aires",
                "C", "wu", "wu", "buenos-aires", "LIVE",
            ),
        )
        conn.commit()

        with patch.dict(ICAO_TO_CITY, {}, clear=True):
            assert poly_slug_for_station_id("SAEZ", conn=conn) == "buenos-aires"
            assert ICAO_TO_CITY["SAEZ"] == "buenos-aires"

        conn.close()

    def test_derives_slug_when_db_override_is_blank(self, tmp_path):
        conn = init_db(tmp_path / "test.db")
        conn.execute(
            """INSERT INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source,
             calibration_source, poly_slug, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "KATL", "Atlanta", 33.6407, -84.4277, "America/New_York",
                "F", "wu", "wu", "", "DRY_RUN",
            ),
        )
        conn.commit()

        with patch.dict(ICAO_TO_CITY, {}, clear=True):
            assert poly_slug_for_station_id("KATL", conn=conn) == "atlanta"

        conn.close()
