"""Tests for actuals ingestion routing, upsert, and retry logic."""

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from hightempbot.db.connection import init_db
from hightempbot.ingestion.actuals import (
    ActualRow,
    backfill_missing_actuals,
    fetch_actual,
    upsert_actual,
    fetch_and_store,
    supports_actual_scrape,
)
from hightempbot.stations import StationConfig


# Inline test fixtures — no dependency on hardcoded STATIONS
_KLGA = StationConfig(
    icao="KLGA", city="New York City", lat=40.7772, lon=-73.8726,
    timezone="America/New_York", unit="F", resolution_source="wu",
    poly_slug="nyc",
)
_KBKF = StationConfig(
    icao="KBKF", city="Denver", lat=39.7169, lon=-104.7519,
    timezone="America/Denver", unit="F", resolution_source="wu",
)
_RJTT = StationConfig(
    icao="RJTT", city="Tokyo", lat=35.5533, lon=139.7811,
    timezone="Asia/Tokyo", unit="C", resolution_source="wu",
)


class _FakeWUResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def db(tmp_path):
    return init_db(tmp_path / "test.db")


class TestActualRouting:
    def test_wu_station_routes_correctly(self, tmp_path):
        """KLGA (wu resolution source) calls fetch_wu_tmax."""
        with patch(
            "hightempbot.ingestion.sources.wu.fetch_wu_tmax",
            return_value=95.0,  # °F
        ):
            row = fetch_actual("KLGA", date(2024, 7, 15), tmp_path, station_override=_KLGA)
        assert row is not None
        assert row.station_id == "KLGA"
        assert row.tmax_celsius == pytest.approx(35.0)  # converted from 95°F
        assert row.source == "wu"

    def test_supports_actual_scrape_allows_wu(self):
        assert supports_actual_scrape(_KLGA) is True

    def test_unknown_station_returns_none(self, tmp_path):
        """No station_override returns None."""
        row = fetch_actual("XXXX", date(2024, 7, 15), tmp_path)
        assert row is None


class TestRetryLogic:
    def test_retries_on_failure(self, tmp_path):
        """fetch_actual retries up to 3 times before returning None."""
        call_count = 0

        def failing_wu(icao, target_date, cache_dir, unit="F", tz=None):
            nonlocal call_count
            call_count += 1
            return None

        with patch(
            "hightempbot.ingestion.sources.wu.fetch_wu_tmax",
            side_effect=failing_wu,
        ):
            row = fetch_actual("KLGA", date(2024, 7, 15), tmp_path, station_override=_KLGA)

        assert row is None
        assert call_count == 3  # MAX_RETRIES

    def test_succeeds_on_second_attempt(self, tmp_path):
        """fetch_actual returns data on second attempt."""
        attempts = []

        def intermittent_wu(icao, target_date, cache_dir, unit="F", tz=None):
            attempts.append(1)
            if len(attempts) < 2:
                return None
            return 90.0  # °F

        with patch(
            "hightempbot.ingestion.sources.wu.fetch_wu_tmax",
            side_effect=intermittent_wu,
        ):
            row = fetch_actual("KLGA", date(2024, 7, 15), tmp_path, station_override=_KLGA)

        assert row is not None
        assert row.tmax_celsius == pytest.approx(32.222, abs=0.01)
        assert len(attempts) == 2

    def test_retries_when_resolution_source_raises(self, tmp_path):
        """Source exceptions are retried instead of escaping the actuals job."""
        attempts = []

        def intermittent_wu(icao, target_date, cache_dir, unit="F", tz=None):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("corrupt cache")
            return 90.0

        with patch(
            "hightempbot.ingestion.sources.wu.fetch_wu_tmax",
            side_effect=intermittent_wu,
        ), patch("time.sleep", return_value=None) as sleep:
            row = fetch_actual("KLGA", date(2024, 7, 15), tmp_path, station_override=_KLGA)

        assert row is not None
        assert row.tmax_celsius == pytest.approx(32.222, abs=0.01)
        assert len(attempts) == 2
        sleep.assert_called_once()


class TestWUCacheHardening:
    def test_unknown_icao_country_prefix_fails_closed_without_request(self, tmp_path):
        from hightempbot.ingestion.sources.wu import fetch_wu_tmax

        with patch("hightempbot.ingestion.sources.wu.requests.get") as get:
            tmax = fetch_wu_tmax("XXYZ", date(2024, 7, 15), tmp_path, unit="C")

        assert tmax is None
        get.assert_not_called()

    def test_cache_read_errors_are_deleted_and_refetched(self, tmp_path, monkeypatch):
        """Unreadable cached JSON does not permanently block a station-day."""
        from hightempbot.ingestion.sources.wu import fetch_wu_tmax

        cache_file = tmp_path / "wu_cache" / "KLGA" / "2024-07-15_F.json"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text('{"tmax": 72.0}', encoding="utf-8")

        original_read_text = Path.read_text

        def fail_read(self, *args, **kwargs):
            if self == cache_file:
                raise OSError("locked cache")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_read)
        payload = {"observations": [{"temp": 80.0}, {"temp": 96.0}]}

        with patch("hightempbot.ingestion.sources.wu.time.sleep", return_value=None), patch(
            "hightempbot.ingestion.sources.wu._get_wu_api_key",
            return_value="test-key",
        ), patch(
            "hightempbot.ingestion.sources.wu.requests.get",
            return_value=_FakeWUResponse(payload),
        ) as get:
            tmax = fetch_wu_tmax("KLGA", date(2024, 7, 15), tmp_path, unit="F")

        assert tmax == 96.0
        assert get.call_count == 1
        cached_payload = json.loads(original_read_text(cache_file, encoding="utf-8"))
        assert cached_payload["tmax"] == 96.0

    def test_corrupt_cache_is_deleted_and_refetched(self, tmp_path):
        """Bad cached JSON does not permanently block a station-day."""
        from hightempbot.ingestion.sources.wu import fetch_wu_tmax

        cache_file = tmp_path / "wu_cache" / "KLGA" / "2024-07-15_F.json"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text("{not-json", encoding="utf-8")

        payload = {"observations": [{"temp": 80.0}, {"temp": 95.0}]}
        with patch("hightempbot.ingestion.sources.wu.time.sleep", return_value=None), patch(
            "hightempbot.ingestion.sources.wu._get_wu_api_key",
            return_value="test-key",
        ), patch(
            "hightempbot.ingestion.sources.wu.requests.get",
            return_value=_FakeWUResponse(payload),
        ) as get:
            tmax = fetch_wu_tmax("KLGA", date(2024, 7, 15), tmp_path, unit="F")

        assert tmax == 95.0
        assert get.call_count == 1
        assert json.loads(cache_file.read_text(encoding="utf-8"))["tmax"] == 95.0

    def test_cache_write_errors_are_best_effort(self, tmp_path, monkeypatch):
        """A successful WU response still returns even if cache persistence fails."""
        from hightempbot.ingestion.sources.wu import fetch_wu_tmax

        def fail_write(self, *args, **kwargs):
            raise OSError("read-only cache")

        monkeypatch.setattr(Path, "write_text", fail_write)
        payload = {"observations": [{"temp": 88.0}, {"temp": 91.0}]}

        with patch("hightempbot.ingestion.sources.wu.time.sleep", return_value=None), patch(
            "hightempbot.ingestion.sources.wu._get_wu_api_key",
            return_value="test-key",
        ), patch(
            "hightempbot.ingestion.sources.wu.requests.get",
            return_value=_FakeWUResponse(payload),
        ):
            tmax = fetch_wu_tmax("KLGA", date(2024, 7, 15), tmp_path, unit="F")

        assert tmax == 91.0


class TestUpsert:
    def test_upsert_round_trip(self, db):
        """INSERT and read back an ActualRow."""
        row = ActualRow("KLGA", date(2024, 7, 15), 35.0, "wu")
        upsert_actual(db, row)

        result = db.execute(
            "SELECT tmax_celsius, source FROM actuals "
            "WHERE station_id='KLGA' AND local_date='2024-07-15'"
        ).fetchone()
        assert result["tmax_celsius"] == 35.0
        assert result["source"] == "wu"

    def test_upsert_replaces_existing(self, db):
        """Second upsert replaces the first."""
        upsert_actual(db, ActualRow("KLGA", date(2024, 7, 15), 35.0, "wu"))
        upsert_actual(db, ActualRow("KLGA", date(2024, 7, 15), 36.0, "wu"))

        result = db.execute(
            "SELECT tmax_celsius FROM actuals "
            "WHERE station_id='KLGA' AND local_date='2024-07-15'"
        ).fetchone()
        assert result["tmax_celsius"] == 36.0

    def test_fetch_and_store_integration(self, db, tmp_path):
        """fetch_and_store writes to DB and returns the row."""
        with patch(
            "hightempbot.ingestion.sources.wu.fetch_wu_tmax",
            return_value=91.0,  # °F
        ):
            row = fetch_and_store("KLGA", date(2024, 7, 15), db, tmp_path, station_override=_KLGA)

        assert row is not None
        assert row.tmax_celsius == pytest.approx(32.778, abs=0.01)

        result = db.execute(
            "SELECT tmax_celsius FROM actuals "
            "WHERE station_id='KLGA' AND local_date='2024-07-15'"
        ).fetchone()
        assert result is not None

    def test_upsert_backfills_resolved_ledger_actual(self, db):
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, pnl, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-04-06 08:00:00", "KLGA", "m", "t", "2024-07-15",
                1, 68.0, "YES", 0.45, 0.30, 0.15,
                1.0, 5000.0, 1.0, 0.30,
                "WIN", 2.0, "dry_run",
            ),
        )
        db.commit()

        upsert_actual(db, ActualRow("KLGA", date(2024, 7, 15), 35.0, "wu"))

        result = db.execute(
            "SELECT actual_tmax FROM ledger WHERE station_id='KLGA' AND target_date='2024-07-15'"
        ).fetchone()
        assert result["actual_tmax"] == 35.0


class TestBackfillMissingActuals:
    def test_fills_only_missing_dates(self, db, tmp_path):
        """Existing rows are skipped; only gaps trigger WU fetches."""
        # Pre-seed two dates so the gap is one single date in the middle.
        upsert_actual(db, ActualRow("KLGA", date(2024, 7, 15), 30.0, "wu"))
        upsert_actual(db, ActualRow("KLGA", date(2024, 7, 17), 31.0, "wu"))

        with patch(
            "hightempbot.ingestion.sources.wu.fetch_wu_tmax",
            return_value=95.0,
        ) as wu:
            n = backfill_missing_actuals(
                db, _KLGA, tmp_path,
                start_date=date(2024, 7, 15),
                end_date=date(2024, 7, 18),  # half-open
            )

        assert n == 1
        assert wu.call_count == 1  # only the missing day was fetched
        # The new row was written with the WU-converted tmax (95°F -> 35°C).
        row = db.execute(
            "SELECT tmax_celsius FROM actuals "
            "WHERE station_id='KLGA' AND local_date='2024-07-16'"
        ).fetchone()
        assert row is not None
        assert row["tmax_celsius"] == pytest.approx(35.0)

    def test_unsupported_source_is_noop(self, db, tmp_path):
        """Stations whose source is not 'wu' return 0 without DB writes."""
        unsupported = StationConfig(
            icao="ZZZZ", city="Nowhere", lat=0.0, lon=0.0,
            timezone="UTC", unit="C", resolution_source="prob_api",
        )
        with patch("hightempbot.ingestion.sources.wu.fetch_wu_tmax") as wu:
            n = backfill_missing_actuals(
                db, unsupported, tmp_path,
                start_date=date(2024, 7, 15),
                end_date=date(2024, 7, 18),
            )

        assert n == 0
        assert wu.call_count == 0

    def test_no_gaps_returns_zero(self, db, tmp_path):
        """When all dates in the window are present, nothing is fetched."""
        for d in (date(2024, 7, 15), date(2024, 7, 16), date(2024, 7, 17)):
            upsert_actual(db, ActualRow("KLGA", d, 30.0, "wu"))

        with patch("hightempbot.ingestion.sources.wu.fetch_wu_tmax") as wu:
            n = backfill_missing_actuals(
                db, _KLGA, tmp_path,
                start_date=date(2024, 7, 15),
                end_date=date(2024, 7, 18),
            )

        assert n == 0
        assert wu.call_count == 0

    def test_end_bound_clamped_to_station_local_yesterday(self, db, tmp_path):
        """A UTC end_date past the station-local today never fetches the
        in-progress local day (calibration-poisoning guard)."""
        calls: list[date] = []

        def rec(icao, target_date, cache_dir, unit="F", tz=None):
            calls.append(target_date)
            return 90.0

        # Station-local "today" is 2024-07-17: the in-progress day. The
        # caller's UTC-derived end_date (2024-07-18) is a day ahead — the
        # guard must clamp fetches to <= 2024-07-16 (station-local yesterday).
        with patch(
            "hightempbot.ingestion.actuals._station_local_today",
            return_value=date(2024, 7, 17),
        ), patch(
            "hightempbot.ingestion.sources.wu.fetch_wu_tmax",
            side_effect=rec,
        ):
            n = backfill_missing_actuals(
                db, _KLGA, tmp_path,
                start_date=date(2024, 7, 14),
                end_date=date(2024, 7, 18),
            )

        assert date(2024, 7, 17) not in calls   # in-progress local day skipped
        assert date(2024, 7, 18) not in calls
        assert max(calls) == date(2024, 7, 16)   # last fetched = local yesterday
        assert n == 3                            # 14, 15, 16


class TestWUCacheCompleteness:
    """FIX: the WU disk cache must not serve a partial value cached during the
    station's in-progress local day, and must handle legacy payloads."""

    _NY = "America/New_York"

    def _write_cache(self, tmp_path, target_date, payload):
        cache_file = (
            tmp_path / "wu_cache" / "KLGA" / f"{target_date.isoformat()}_F.json"
        )
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(payload), encoding="utf-8")
        return cache_file

    def test_incomplete_day_cache_is_refetched(self, tmp_path):
        """fetched_at before station-local day-end + margin → partial → refetch."""
        from hightempbot.ingestion.sources.wu import fetch_wu_tmax

        target = date(2024, 7, 15)
        # NY (EDT) day 2024-07-15 ends 2024-07-16 04:00Z; +6h margin = 10:00Z.
        # This stamp (mid-day, before that) is an in-progress partial.
        cache_file = self._write_cache(
            tmp_path, target,
            {"tmax": 70.0, "fetched_at": "2024-07-15T18:00:00+00:00"},
        )
        payload = {"observations": [{"temp": 80.0}, {"temp": 96.0}]}

        with patch(
            "hightempbot.ingestion.sources.wu.time.sleep", return_value=None,
        ), patch(
            "hightempbot.ingestion.sources.wu._get_wu_api_key",
            return_value="test-key",
        ), patch(
            "hightempbot.ingestion.sources.wu.requests.get",
            return_value=_FakeWUResponse(payload),
        ) as get:
            tmax = fetch_wu_tmax("KLGA", target, tmp_path, unit="F", tz=self._NY)

        assert tmax == 96.0            # refetched final high, not the 70.0 partial
        assert get.call_count == 1
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        assert cached["tmax"] == 96.0
        assert "fetched_at" in cached  # rewritten with a fresh stamp

    def test_complete_day_cache_is_served(self, tmp_path):
        """fetched_at well after day-end + margin → served without a WU call."""
        from hightempbot.ingestion.sources.wu import fetch_wu_tmax

        target = date(2024, 7, 15)
        self._write_cache(
            tmp_path, target,
            {"tmax": 88.0, "fetched_at": "2024-07-17T12:00:00+00:00"},
        )
        with patch("hightempbot.ingestion.sources.wu.requests.get") as get:
            tmax = fetch_wu_tmax("KLGA", target, tmp_path, unit="F", tz=self._NY)

        assert tmax == 88.0
        get.assert_not_called()

    def test_legacy_cache_recent_date_is_refetched(self, tmp_path):
        """Legacy {"tmax": ...} for a recent date (<7d) is not trusted."""
        from hightempbot.ingestion.sources.wu import fetch_wu_tmax

        target = date.today() - timedelta(days=1)
        self._write_cache(tmp_path, target, {"tmax": 70.0})
        payload = {"observations": [{"temp": 91.0}]}

        with patch(
            "hightempbot.ingestion.sources.wu.time.sleep", return_value=None,
        ), patch(
            "hightempbot.ingestion.sources.wu._get_wu_api_key",
            return_value="test-key",
        ), patch(
            "hightempbot.ingestion.sources.wu.requests.get",
            return_value=_FakeWUResponse(payload),
        ) as get:
            tmax = fetch_wu_tmax("KLGA", target, tmp_path, unit="F", tz=self._NY)

        assert tmax == 91.0
        assert get.call_count == 1

    def test_legacy_cache_old_date_is_served(self, tmp_path):
        """Legacy {"tmax": ...} for an old date (>7d) is trusted (backward-compat)."""
        from hightempbot.ingestion.sources.wu import fetch_wu_tmax

        target = date.today() - timedelta(days=30)
        self._write_cache(tmp_path, target, {"tmax": 70.0})
        with patch("hightempbot.ingestion.sources.wu.requests.get") as get:
            tmax = fetch_wu_tmax("KLGA", target, tmp_path, unit="F", tz=self._NY)

        assert tmax == 70.0
        get.assert_not_called()

    def test_no_tz_preserves_legacy_serve(self, tmp_path):
        """tz-less callers keep the prior unconditional cache-serve behavior."""
        from hightempbot.ingestion.sources.wu import fetch_wu_tmax

        target = date.today() - timedelta(days=1)  # recent — would refetch WITH tz
        self._write_cache(tmp_path, target, {"tmax": 70.0})
        with patch("hightempbot.ingestion.sources.wu.requests.get") as get:
            tmax = fetch_wu_tmax("KLGA", target, tmp_path, unit="F")  # no tz

        assert tmax == 70.0
        get.assert_not_called()


class TestUnitConversion:
    def test_fahrenheit_conversion_for_us_stations(self, tmp_path):
        """US WU stations (°F) convert to Celsius."""
        with patch(
            "hightempbot.ingestion.sources.wu.fetch_wu_tmax",
            return_value=95.0,
        ):
            row = fetch_actual("KBKF", date(2024, 7, 15), tmp_path, station_override=_KBKF)

        assert row is not None
        assert row.tmax_celsius == pytest.approx(35.0)
        assert row.source == "wu"

    def test_celsius_station_no_conversion(self, tmp_path):
        """International WU stations (°C) are not converted."""
        with patch(
            "hightempbot.ingestion.sources.wu.fetch_wu_tmax",
            return_value=35.0,  # already °C
        ):
            row = fetch_actual("RJTT", date(2024, 7, 15), tmp_path, station_override=_RJTT)

        assert row is not None
        assert row.tmax_celsius == 35.0
