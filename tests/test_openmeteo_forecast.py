from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from hightempbot.db.connection import get_connection, init_db
from hightempbot.ingestion import openmeteo_forecast as om
from hightempbot.ingestion.openmeteo_forecast import (
    MODELS,
    BackfillResult,
    backfill_openmeteo,
    fetch_live,
)
from hightempbot.stations import StationConfig


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    conn = get_connection(str(db_path))
    yield conn
    conn.close()


def _local_today(station: StationConfig) -> date:
    return datetime.now(om.pytz.timezone(station.timezone)).date()


def _previous_day1_payload(
    start_date: date,
    forecast_days: int,
    *,
    models: list[str] | tuple[str, ...] = MODELS,
    high: float | None = 34.0,
) -> dict:
    hourly: dict[str, list] = {"time": []}
    for model in models:
        hourly[f"temperature_2m_previous_day1_{model}"] = []

    for day_offset in range(forecast_days):
        target_date = start_date + timedelta(days=day_offset)
        hourly["time"].extend([
            f"{target_date.isoformat()}T00:00",
            f"{target_date.isoformat()}T12:00",
        ])
        for model_idx, model in enumerate(models):
            key = f"temperature_2m_previous_day1_{model}"
            if high is None:
                hourly[key].extend([None, None])
            else:
                tmax = high + day_offset + (model_idx / 10.0)
                hourly[key].extend([tmax - 4.0, tmax])

    return {"hourly": hourly}


def _full_result(start_date: date, forecast_days: int, value: float = 33.0) -> dict[date, dict[str, float]]:
    return {
        start_date + timedelta(days=day_offset): {model: value for model in MODELS}
        for day_offset in range(forecast_days)
    }


def test_backfill_retries_dates_with_incomplete_model_coverage(db: sqlite3.Connection):
    station = StationConfig(
        icao="KDAL",
        city="Dallas",
        lat=32.85,
        lon=-96.85,
        timezone="US/Central",
        unit="F",
        resolution_source="wu",
        poly_slug="dallas",
    )
    db.execute(
        "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
        ("KDAL", "2026-04-07", 20.0, "test"),
    )
    for idx, model_name in enumerate(MODELS[:-2]):
        db.execute(
            """INSERT INTO forecast_archive
            (station_id, target_date, horizon, issue_date, centre, member, tmax_celsius, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("KDAL", "2026-04-07", 1, "2026-04-06", model_name, idx + 1, 21.0 + idx, "openmeteo"),
        )
    db.commit()

    returned = {date(2026, 4, 7): {model: 25.0 for model in MODELS}}
    with patch("hightempbot.ingestion.openmeteo_forecast.fetch_historical", return_value=returned) as mock_fetch:
        backfill_openmeteo(date(2026, 4, 7), date(2026, 4, 7), db, stations={"KDAL": station}, chunk_days=1)

    assert mock_fetch.called
    stored = db.execute(
        "SELECT COUNT(DISTINCT centre) AS cnt FROM forecast_archive WHERE station_id = ? AND target_date = ?",
        ("KDAL", "2026-04-07"),
    ).fetchone()
    assert stored["cnt"] == len(MODELS)


def test_backfill_refetches_same_count_wrong_model_set(db: sqlite3.Connection):
    station = StationConfig(
        icao="KDAL",
        city="Dallas",
        lat=32.85,
        lon=-96.85,
        timezone="US/Central",
        unit="F",
        resolution_source="wu",
        poly_slug="dallas",
    )
    target_date = date(2026, 4, 7)
    db.execute(
        "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
        ("KDAL", target_date.isoformat(), 20.0, "test"),
    )
    for idx, model_name in enumerate([*MODELS[:-1], "legacy_unknown_model"]):
        db.execute(
            """INSERT INTO forecast_archive
            (station_id, target_date, horizon, issue_date, centre, member, tmax_celsius, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("KDAL", target_date.isoformat(), 1, "2026-04-06", model_name, idx + 1, 21.0 + idx, "openmeteo"),
        )
    db.commit()

    returned = {target_date: {model: 25.0 for model in MODELS}}
    with patch("hightempbot.ingestion.openmeteo_forecast.fetch_historical", return_value=returned) as mock_fetch:
        backfill_openmeteo(target_date, target_date, db, stations={"KDAL": station}, chunk_days=1)

    assert mock_fetch.called
    stored_centres = {
        row["centre"]
        for row in db.execute(
            "SELECT DISTINCT centre FROM forecast_archive WHERE station_id = ? AND target_date = ?",
            ("KDAL", target_date.isoformat()),
        ).fetchall()
    }
    assert set(MODELS).issubset(stored_centres)


def test_backfill_reports_failed_fetch_chunk(db: sqlite3.Connection):
    station = StationConfig(
        icao="KDAL",
        city="Dallas",
        lat=32.85,
        lon=-96.85,
        timezone="US/Central",
        unit="F",
        resolution_source="wu",
        poly_slug="dallas",
    )
    target_date = date(2026, 4, 7)
    db.execute(
        "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
        ("KDAL", target_date.isoformat(), 20.0, "wu"),
    )
    db.commit()

    with patch("hightempbot.ingestion.openmeteo_forecast.fetch_historical", return_value=None):
        result = backfill_openmeteo(
            target_date,
            target_date,
            db,
            stations={"KDAL": station},
            chunk_days=1,
        )

    assert isinstance(result, BackfillResult)
    assert result.ok is False
    assert result.failed_chunks == (("KDAL", target_date.isoformat(), target_date.isoformat()),)
    health = db.execute(
        "SELECT status, message FROM pipeline_health "
        "WHERE station_id = 'KDAL' AND stage = 'forecast_backfill' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert health["status"] == "ERROR"
    assert "failed chunk" in health["message"]


def test_dead_temperature_2m_max_parser_is_removed():
    assert not hasattr(om, "_parse_daily_tmax")


def test_fetch_live_does_not_reuse_too_short_cache_horizon():
    station = StationConfig(
        icao="KDAL",
        city="Dallas",
        lat=32.85,
        lon=-96.85,
        timezone="US/Central",
        unit="F",
        resolution_source="wu",
        poly_slug="dallas",
    )
    om._forecast_cache.clear()

    today = _local_today(station)
    short_payload = _previous_day1_payload(today, 2)

    with patch("requests.get", return_value=_Resp(short_payload)):
        short_result = fetch_live(station, forecast_days=2)
    assert short_result is not None

    with patch("requests.get", side_effect=RuntimeError("network down")), \
         patch("time.sleep", return_value=None):
        long_result = fetch_live(station, forecast_days=3)

    assert long_result is None


def test_fetch_live_falls_back_to_historical_forecast_when_previous_runs_is_down():
    station = StationConfig(
        icao="OPKC",
        city="Karachi",
        lat=24.9065,
        lon=67.1608,
        timezone="Asia/Karachi",
        unit="C",
        resolution_source="wu",
        poly_slug="karachi",
    )
    om._forecast_cache.clear()

    today = _local_today(station)
    payload = _previous_day1_payload(today, 2)
    called_urls = []

    def _fake_get(url, **_kwargs):
        called_urls.append(url)
        if url == om._PREVIOUS_RUNS_URL:
            raise RuntimeError("network unreachable")
        return _Resp(payload)

    with patch("requests.get", side_effect=_fake_get), \
         patch("time.sleep", return_value=None):
        result = fetch_live(station, forecast_days=2)

    assert called_urls == [om._PREVIOUS_RUNS_URL, om._HISTORICAL_FORECAST_URL]
    assert result is not None
    assert result[today]["ecmwf_ifs025"] == pytest.approx(34.0)


def test_fetch_live_rejects_partial_membership_and_tries_next_endpoint():
    station = StationConfig(
        icao="OPKC",
        city="Karachi",
        lat=24.9065,
        lon=67.1608,
        timezone="Asia/Karachi",
        unit="C",
        resolution_source="wu",
        poly_slug="karachi",
    )
    om._forecast_cache.clear()

    today = _local_today(station)
    partial_payload = _previous_day1_payload(today, 2, models=MODELS[:-1])
    full_payload = _previous_day1_payload(today, 2, high=40.0)
    called_urls = []

    def _fake_get(url, **_kwargs):
        called_urls.append(url)
        if url == om._PREVIOUS_RUNS_URL:
            return _Resp(partial_payload)
        return _Resp(full_payload)

    with patch("requests.get", side_effect=_fake_get), \
         patch("time.sleep", return_value=None):
        result = fetch_live(station, forecast_days=2)

    assert called_urls == [om._PREVIOUS_RUNS_URL, om._HISTORICAL_FORECAST_URL]
    assert result is not None
    assert set(result[today]) == set(MODELS)
    cached = om._forecast_cache[(station.icao, 2)][1]
    assert set(cached[today]) == set(MODELS)


def test_fetch_live_never_caches_partial_membership():
    station = StationConfig(
        icao="OPKC",
        city="Karachi",
        lat=24.9065,
        lon=67.1608,
        timezone="Asia/Karachi",
        unit="C",
        resolution_source="wu",
        poly_slug="karachi",
    )
    om._forecast_cache.clear()

    partial_payload = _previous_day1_payload(_local_today(station), 2, models=MODELS[:-1])

    with patch("requests.get", return_value=_Resp(partial_payload)), \
         patch("time.sleep", return_value=None):
        result = fetch_live(station, forecast_days=2)

    assert result is None
    assert (station.icao, 2) not in om._forecast_cache


def test_fetch_live_does_not_cache_empty_previous_day1_fallback():
    station = StationConfig(
        icao="OPKC",
        city="Karachi",
        lat=24.9065,
        lon=67.1608,
        timezone="Asia/Karachi",
        unit="C",
        resolution_source="wu",
        poly_slug="karachi",
    )
    today = _local_today(station)
    cached = _full_result(today, 2)
    om._forecast_cache.clear()
    om._forecast_cache[(station.icao, 2)] = (om.time.time(), cached)

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return _previous_day1_payload(today, 2, high=None)

    with patch("requests.get", return_value=_Resp()), \
         patch("time.sleep", return_value=None):
        result = fetch_live(station, forecast_days=2)

    assert result == cached
