from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hightempbot.calibration.emos import EMOSParams
from hightempbot.calibration.store import save_emos
from hightempbot.db.connection import get_connection, init_db
from hightempbot.enrollment.geocode import GeoResult
from hightempbot.enrollment.pipeline import enroll_station


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def _event() -> dict:
    return {
        "title": "Highest temperature in Dallas on April 23",
        "description": "Dallas resolution uses Fahrenheit.",
        "resolutionSource": "https://www.wunderground.com/history/daily/us/tx/dallas/KDAL",
        "markets": [
            {
                "question": "Will the highest temperature in Dallas be 72°F or below on April 23?",
                "clobTokenIds": ["yes0", "no0"],
                "outcomePrices": ["0.30", "0.70"],
                "volume": 1000,
                "conditionId": "m0",
            },
            {
                "question": "Will the highest temperature in Dallas be between 73-74°F on April 23?",
                "clobTokenIds": ["yes1", "no1"],
                "outcomePrices": ["0.40", "0.60"],
                "volume": 1000,
                "conditionId": "m1",
            },
            {
                "question": "Will the highest temperature in Dallas be 75°F or higher on April 23?",
                "clobTokenIds": ["yes2", "no2"],
                "outcomePrices": ["0.30", "0.70"],
                "volume": 1000,
                "conditionId": "m2",
            },
        ],
    }


def test_enrollment_seeds_event_brackets_before_lut_seed(db_path: str):
    conn = get_connection(db_path)

    def _fake_retrain(station_id: str, horizon: int, retrain_conn):
        save_emos(
            retrain_conn,
            station_id,
            horizon,
            EMOSParams(a=0.0, b=1.0, c=0.0, d=0.0, n_samples=60),
        )
        return SimpleNamespace(is_ready=lambda: True, emos_params=SimpleNamespace(n_samples=60))

    def _fake_seed(seed_conn, station_id: str):
        row = seed_conn.execute(
            "SELECT COUNT(*) AS c FROM market_tokens WHERE station_id = ?",
            (station_id,),
        ).fetchone()
        return (3, 9) if row["c"] else (0, 0)

    try:
        with patch(
            "hightempbot.enrollment.pipeline.geocode_city",
            return_value=GeoResult(lat=32.85, lon=-96.85, timezone="US/Central", country_code="US"),
        ), patch(
            "hightempbot.enrollment.pipeline._backfill_actuals",
            return_value=(1000, False),
        ), patch(
            "hightempbot.ingestion.openmeteo_forecast.backfill_openmeteo",
            return_value=None,
        ), patch(
            "hightempbot.calibration.model.retrain",
            side_effect=_fake_retrain,
        ), patch(
            "hightempbot.calibration.lut.seed_lut_from_history",
            side_effect=_fake_seed,
        ), patch(
            "hightempbot.enrollment.pipeline.register_enrolled_station",
            return_value=None,
        ):
            status = enroll_station(
                "Dallas",
                _event(),
                conn,
                db_path,
                market_date=date(2026, 4, 23),
            )

        assert status == "DRY_RUN"
        rows = conn.execute(
            "SELECT bracket_idx, bracket_label FROM market_tokens "
            "WHERE station_id = ? AND market_date = ? ORDER BY bracket_idx",
            ("KDAL", "2026-04-23"),
        ).fetchall()
        assert len(rows) == 3
        assert rows[1]["bracket_label"]
    finally:
        conn.close()


def test_enrollment_restarts_retryable_skipped_station(db_path: str):
    conn = get_connection(db_path)
    conn.execute(
        """INSERT INTO enrolled_stations
        (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source,
         poly_slug, status, step, step_detail, skip_reason, coverage_pct)
        VALUES (?, ?, 0, 0, 'UTC', ?, ?, ?, ?, 'SKIPPED', 'legacy_bss',
                'BSS=0.78 < 0.86', 'low_bss', 1.0)""",
        ("KDAL", "Dallas", "F", "wu", "wu", "dallas"),
    )
    conn.commit()

    def _fake_retrain(station_id: str, horizon: int, retrain_conn):
        save_emos(
            retrain_conn,
            station_id,
            horizon,
            EMOSParams(a=0.0, b=1.0, c=0.0, d=0.0, n_samples=60),
        )
        return SimpleNamespace(is_ready=lambda: True, emos_params=SimpleNamespace(n_samples=60))

    try:
        with patch(
            "hightempbot.enrollment.pipeline.geocode_city",
            return_value=GeoResult(lat=32.85, lon=-96.85, timezone="US/Central", country_code="US"),
        ), patch(
            "hightempbot.enrollment.pipeline._backfill_actuals",
            return_value=(1000, False),
        ), patch(
            "hightempbot.ingestion.openmeteo_forecast.backfill_openmeteo",
            return_value=None,
        ), patch(
            "hightempbot.calibration.model.retrain",
            side_effect=_fake_retrain,
        ), patch(
            "hightempbot.calibration.lut.seed_lut_from_history",
            return_value=(3, 9),
        ), patch(
            "hightempbot.enrollment.pipeline.register_enrolled_station",
            return_value=None,
        ):
            status = enroll_station(
                "Dallas",
                _event(),
                conn,
                db_path,
                market_date=date(2026, 4, 23),
            )

        assert status == "DRY_RUN"
        row = conn.execute(
            "SELECT status, step, skip_reason FROM enrolled_stations WHERE icao = ?",
            ("KDAL",),
        ).fetchone()
        assert row["status"] == "DRY_RUN"
        assert row["step"] == "day_1"
        assert row["skip_reason"] is None
    finally:
        conn.close()


def test_enrollment_uses_live_status_when_runtime_dry_run_is_false(db_path: str):
    conn = get_connection(db_path)

    def _fake_retrain(station_id: str, horizon: int, retrain_conn):
        save_emos(
            retrain_conn,
            station_id,
            horizon,
            EMOSParams(a=0.0, b=1.0, c=0.0, d=0.0, n_samples=60),
        )
        return SimpleNamespace(is_ready=lambda: True, emos_params=SimpleNamespace(n_samples=60))

    try:
        with patch(
            "hightempbot.enrollment.pipeline.geocode_city",
            return_value=GeoResult(lat=32.85, lon=-96.85, timezone="US/Central", country_code="US"),
        ), patch(
            "hightempbot.enrollment.pipeline._backfill_actuals",
            return_value=(1000, False),
        ), patch(
            "hightempbot.ingestion.openmeteo_forecast.backfill_openmeteo",
            return_value=None,
        ), patch(
            "hightempbot.calibration.model.retrain",
            side_effect=_fake_retrain,
        ), patch(
            "hightempbot.calibration.lut.seed_lut_from_history",
            return_value=(3, 9),
        ), patch(
            "hightempbot.enrollment.pipeline.register_enrolled_station",
            return_value=None,
        ):
            status = enroll_station(
                "Dallas",
                _event(),
                conn,
                db_path,
                market_date=date(2026, 4, 23),
                runtime_dry_run=False,
            )

        assert status == "LIVE"
        row = conn.execute(
            "SELECT status, step, step_detail FROM enrolled_stations WHERE icao = ?",
            ("KDAL",),
        ).fetchone()
        assert row["status"] == "LIVE"
        assert row["step"] == "active"
        assert row["step_detail"] == "Live trading enabled"
    finally:
        conn.close()
