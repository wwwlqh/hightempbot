"""Tests for APScheduler job setup — Phase 2 per-station scanning."""

import pytest
import time
from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.background import BackgroundScheduler
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from hightempbot.db.connection import init_db
from hightempbot.runtime_config import Config, set_config
from hightempbot.scheduler import jobs as scheduler_jobs
from hightempbot.scheduler.jobs import schedule_all_jobs
from hightempbot.stations import StationConfig


# Inline test fixtures — no dependency on hardcoded STATIONS
_TEST_STATIONS = {
    "KLGA": StationConfig(
        icao="KLGA", city="New York City", lat=40.7772, lon=-73.8726,
        timezone="America/New_York", unit="F", resolution_source="wu",
        poly_slug="nyc",
    ),
    "RJTT": StationConfig(
        icao="RJTT", city="Tokyo", lat=35.5533, lon=139.7811,
        timezone="Asia/Tokyo", unit="C", resolution_source="wu",
    ),
}

_WITH_NON_WU = {
    **_TEST_STATIONS,
    "RCTP": StationConfig(
        icao="RCTP", city="Taipei", lat=25.0777, lon=121.2328,
        timezone="Asia/Taipei", unit="C", resolution_source="cwa",
    ),
}

@pytest.fixture
def db(tmp_path):
    return init_db(tmp_path / "test.db")


def _seed_active_stations(conn, n=6):
    for idx in range(n):
        icao = f"KX{idx:02d}"
        conn.execute(
            """INSERT INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source,
             poly_slug, status)
            VALUES (?, ?, 0.0, 0.0, 'UTC', 'F', 'wu', 'wu', ?, 'LIVE')""",
            (icao, f"Test {idx}", f"test-{idx}"),
        )
    conn.commit()


def _insert_health(conn, station_id, stage, status, message="test"):
    conn.execute(
        "INSERT INTO pipeline_health (station_id, stage, status, message) "
        "VALUES (?, ?, ?, ?)",
        (station_id, stage, status, message),
    )
    conn.commit()


class TestScheduler:
    def test_creates_correct_job_count(self, db, tmp_path):
        """n midnight + n betting + n resolution + n tpsl + 8 global."""
        scheduler = BackgroundScheduler()
        schedule_all_jobs(scheduler, db, tmp_path, db_path=str(tmp_path / "test.db"), stations=_TEST_STATIONS)
        jobs = scheduler.get_jobs()
        n = len(_TEST_STATIONS)
        assert len(jobs) == n * 4 + 8

    def test_midnight_jobs_have_correct_timezone(self, db, tmp_path):
        scheduler = BackgroundScheduler()
        schedule_all_jobs(scheduler, db, tmp_path, db_path=str(tmp_path / "test.db"), stations=_TEST_STATIONS)

        tokyo_job = scheduler.get_job("midnight_RJTT")
        assert tokyo_job is not None
        assert str(tokyo_job.trigger.timezone) == "Asia/Tokyo"

        nyc_job = scheduler.get_job("midnight_KLGA")
        assert nyc_job is not None
        assert str(nyc_job.trigger.timezone) == "America/New_York"

    def test_betting_jobs_exist(self, db, tmp_path):
        scheduler = BackgroundScheduler()
        schedule_all_jobs(scheduler, db, tmp_path, db_path=str(tmp_path / "test.db"), stations=_TEST_STATIONS)

        for icao in _TEST_STATIONS:
            job = scheduler.get_job(f"betting_{icao}")
            assert job is not None, f"Missing betting job for {icao}"

    def test_resolution_jobs_exist(self, db, tmp_path):
        scheduler = BackgroundScheduler()
        schedule_all_jobs(scheduler, db, tmp_path, db_path=str(tmp_path / "test.db"), stations=_TEST_STATIONS)

        for icao in _TEST_STATIONS:
            job = scheduler.get_job(f"resolution_{icao}")
            assert job is not None, f"Missing resolution job for {icao}"

    def test_monthly_housekeeping_job_exists(self, db, tmp_path):
        scheduler = BackgroundScheduler()
        schedule_all_jobs(scheduler, db, tmp_path, db_path=str(tmp_path / "test.db"), stations=_TEST_STATIONS)

        hk_job = scheduler.get_job("monthly_housekeeping")
        assert hk_job is not None

    def test_live_housekeeping_skips_pending_expiry_when_order_client_unavailable(self, db, tmp_path):
        db_path = str(tmp_path / "test.db")
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, event_type, order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2000-01-01T00:00:00Z",
                "KDAL",
                "m",
                "tok",
                "2000-01-02",
                1,
                90.0,
                "YES",
                0.55,
                0.40,
                0.15,
                1.0,
                5000.0,
                1.0,
                0.40,
                "PENDING",
                "bet",
                "ord_1",
            ),
        )
        db.commit()
        scheduler = BackgroundScheduler()
        cfg = Config(_env_file=None, dry_run=False)
        set_config(cfg)
        try:
            schedule_all_jobs(
                scheduler,
                db,
                tmp_path,
                db_path=db_path,
                stations={},
                dry_run=False,
            )
            with patch(
                "hightempbot.execution.walker.OrderClient",
                side_effect=RuntimeError("missing key"),
            ):
                scheduler.get_job("monthly_housekeeping").func()
        finally:
            set_config(None)

        row = db.execute("SELECT outcome FROM ledger WHERE order_id = 'ord_1'").fetchone()
        assert row["outcome"] == "PENDING"
        health = db.execute(
            "SELECT status, message FROM pipeline_health "
            "WHERE stage = 'expire' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert health["status"] == "ERROR"
        assert "skipped stuck-PENDING expiry" in health["message"]

    def test_monthly_retrain_aborts_when_backfill_reports_failure(self, db, tmp_path):
        from datetime import date

        from hightempbot.calibration.monthly_retrain import run_monthly_retrain
        from hightempbot.ingestion.openmeteo_forecast import BackfillResult

        db.execute(
            """INSERT INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source,
             poly_slug, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "KDAL",
                "Dallas",
                32.85,
                -96.85,
                "US/Central",
                "F",
                "wu",
                "wu",
                "dallas",
                "LIVE",
            ),
        )
        db.commit()
        failed = BackfillResult(
            stations_attempted=1,
            failed_chunks=(("KDAL", "2026-04-01", "2026-04-30"),),
        )

        with patch(
            "hightempbot.ingestion.openmeteo_forecast.backfill_openmeteo",
            return_value=failed,
        ), patch("hightempbot.calibration.model.retrain") as mock_retrain:
            results = run_monthly_retrain(
                db,
                str(tmp_path / "test.db"),
                target_month=date(2026, 4, 1),
            )

        mock_retrain.assert_not_called()
        assert results["KDAL"]["kept"] == "skip"
        assert "forecast backfill failed" in results["KDAL"]["reason"]
        history = db.execute(
            "SELECT kept, reason FROM retrain_history WHERE station_id = 'KDAL'"
        ).fetchone()
        assert history["kept"] == "skip"
        assert "forecast backfill failed" in history["reason"]
        health = db.execute(
            "SELECT status, message FROM pipeline_health "
            "WHERE station_id = 'KDAL' AND stage = 'retrain' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert health["status"] == "ERROR"
        assert "retrain skipped" in health["message"]

    def test_betting_max_instances_1(self, db, tmp_path):
        scheduler = BackgroundScheduler()
        schedule_all_jobs(scheduler, db, tmp_path, db_path=str(tmp_path / "test.db"), stations=_TEST_STATIONS)

        job = scheduler.get_job("betting_KLGA")
        assert job.max_instances == 1

    def test_subset_of_stations(self, db, tmp_path):
        subset = {"KLGA": _TEST_STATIONS["KLGA"]}
        scheduler = BackgroundScheduler()
        schedule_all_jobs(scheduler, db, tmp_path, db_path=str(tmp_path / "test.db"), stations=subset)
        jobs = scheduler.get_jobs()
        # 1 midnight + 1 betting + 1 resolution + 1 tpsl + 8 global = 12
        assert len(jobs) == 12

    def test_replace_existing(self, db, tmp_path):
        scheduler = BackgroundScheduler()
        db_path = str(tmp_path / "test.db")
        schedule_all_jobs(scheduler, db, tmp_path, db_path=db_path, stations=_TEST_STATIONS)
        schedule_all_jobs(scheduler, db, tmp_path, db_path=db_path, stations=_TEST_STATIONS)
        jobs = scheduler.get_jobs()
        n = len(_TEST_STATIONS)
        # n*4 per-station + 8 global; no duplicates after replace_existing fires.
        assert len(jobs) == n * 4 + 8

    def test_job_error_listener_alerts_and_records_health(self, db, tmp_path):
        scheduler = BackgroundScheduler()
        db_path = str(tmp_path / "test.db")
        schedule_all_jobs(scheduler, db, tmp_path, db_path=db_path, stations=_TEST_STATIONS)

        listener = next(
            callback
            for callback, mask in scheduler._listeners
            if mask & EVENT_JOB_ERROR
        )
        captured = []

        def _capture(title, message, config=None, stage="unknown", station_id=""):
            captured.append({
                "title": title,
                "message": message,
                "stage": stage,
                "station_id": station_id,
            })
            return True

        event = SimpleNamespace(
            job_id="betting_KLGA",
            exception=RuntimeError("openmeteo timeout"),
            traceback="Traceback (most recent call last): ...",
        )

        with patch("hightempbot.execution.notify.send_alert", side_effect=_capture):
            listener(event)

        assert captured == [{
            "title": "CRITICAL: scheduled job failed",
            "message": (
                "Job: betting_KLGA\n"
                "Station: KLGA\n"
                "Exception: RuntimeError: openmeteo timeout\n"
                "Action: check logs/hightempbot.log for the full traceback."
            ),
            "stage": "scheduler_job_failed:betting_KLGA",
            "station_id": "KLGA",
        }]

        row = db.execute(
            "SELECT stage, station_id, status, message "
            "FROM pipeline_health ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert dict(row) == {
            "stage": "scheduler",
            "station_id": "KLGA",
            "status": "ERROR",
            "message": "betting_KLGA: RuntimeError: openmeteo timeout",
        }

    def test_daily_forecast_backfill_job_exists(self, db, tmp_path):
        """Daily Open-Meteo backfill at 00:30 UTC is registered with bounded grace."""
        scheduler = BackgroundScheduler()
        schedule_all_jobs(scheduler, db, tmp_path, db_path=str(tmp_path / "test.db"), stations=_TEST_STATIONS)
        job = scheduler.get_job("daily_forecast_backfill")
        assert job is not None
        assert job.max_instances == 1
        # Tightened from 12h to 1h after the 2026-05-14 review — weekly job
        # is the self-healing safety net for missed daily runs.
        assert job.misfire_grace_time == 3600

    def test_system_health_does_not_page_forecast_stall_for_gate_only_quiet_window(
        self, db, tmp_path,
    ):
        db_path = str(tmp_path / "test.db")
        _seed_active_stations(db)
        _insert_health(db, "KX00", "scan", "OK", "betting tick")
        _insert_health(
            db,
            "KX00",
            "gates",
            "SKIP",
            "Outside trading window: target 2026-05-25, local 2026-05-26 (past)",
        )

        scheduler = BackgroundScheduler()
        schedule_all_jobs(scheduler, db, tmp_path, db_path=db_path, stations={})
        captured = []

        def _capture(title, message, config=None, stage="unknown", station_id=""):
            captured.append({"title": title, "message": message, "stage": stage})
            return True

        with patch("hightempbot.execution.notify.send_alert", side_effect=_capture):
            scheduler.get_job("system_health_check").func()

        assert not [row for row in captured if row["stage"] == "forecast_stall"]

    def test_system_health_pages_forecast_stall_after_upstream_success(
        self, db, tmp_path,
    ):
        db_path = str(tmp_path / "test.db")
        _seed_active_stations(db)
        _insert_health(db, "KX00", "scan", "OK", "betting tick")
        _insert_health(db, "KX00", "market", "OK", "11 brackets")

        scheduler = BackgroundScheduler()
        schedule_all_jobs(scheduler, db, tmp_path, db_path=db_path, stations={})
        captured = []

        def _capture(title, message, config=None, stage="unknown", station_id=""):
            captured.append({"title": title, "message": message, "stage": stage})
            return True

        with patch("hightempbot.execution.notify.send_alert", side_effect=_capture):
            scheduler.get_job("system_health_check").func()

        forecast_alerts = [row for row in captured if row["stage"] == "forecast_stall"]
        assert len(forecast_alerts) == 1
        assert forecast_alerts[0]["title"] == "WARNING: 0 forecasts in 2h"
        assert "upstream_ok=1" in forecast_alerts[0]["message"]

    def test_periodic_retrain_timeout_does_not_wait_for_worker_shutdown(self, db, tmp_path):
        db_path = str(tmp_path / "test.db")
        db.execute(
            """INSERT INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source,
             poly_slug, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "KDAL",
                "Dallas",
                32.85,
                -96.85,
                "US/Central",
                "F",
                "wu",
                "wu",
                "dallas",
                "LIVE",
            ),
        )
        db.commit()

        scheduler = BackgroundScheduler()
        with patch("hightempbot.scheduler.jobs.rolling_actuals_backfill_job"):
            schedule_all_jobs(
                scheduler,
                db,
                tmp_path,
                db_path=db_path,
                stations={},
            )

        def _slow_retrain(*_args, **_kwargs):
            Event().wait(1.0)
            return None

        with patch("hightempbot.scheduler.jobs.PERIODIC_RETRAIN_TIMEOUT_S", 0.01), \
             patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0), \
             patch("hightempbot.calibration.model.retrain", side_effect=_slow_retrain):
            started = time.perf_counter()
            scheduler.get_job("periodic_retrain").func()
            elapsed = time.perf_counter() - started

        assert elapsed < 0.5
        health = db.execute(
            "SELECT status, message FROM pipeline_health "
            "WHERE station_id = 'KDAL' AND stage = 'retrain' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert health["status"] == "ERROR"
        assert "Timeout after 0.01s" in health["message"]

    def test_tpsl_jobs_exist(self, db, tmp_path):
        """Per-station YMID TP/SL monitor job is registered."""
        scheduler = BackgroundScheduler()
        schedule_all_jobs(scheduler, db, tmp_path, db_path=str(tmp_path / "test.db"), stations=_TEST_STATIONS)

        for icao in _TEST_STATIONS:
            job = scheduler.get_job(f"tpsl_{icao}")
            assert job is not None, f"Missing tpsl job for {icao}"
            assert job.max_instances == 1
            assert job.misfire_grace_time == 600

    def test_live_mode_registers_ten_minute_wallet_and_auto_redeem_jobs(self, db, tmp_path):
        scheduler = BackgroundScheduler()
        cfg = Config(
            _env_file=None,
            dry_run=False,
            auto_redeem_enabled=True,
            auto_redeem_interval_minutes=10,
            wallet_snapshot_interval_minutes=10,
        )
        set_config(cfg)
        try:
            schedule_all_jobs(
                scheduler,
                db,
                tmp_path,
                db_path=str(tmp_path / "test.db"),
                stations={},
                dry_run=False,
            )
        finally:
            set_config(None)

        job = scheduler.get_job("periodic_redeemer")
        assert job is not None
        assert job.max_instances == 1
        assert job.executor == "ops"
        assert job.coalesce is True
        assert job.misfire_grace_time == 900
        assert job.next_run_time is not None
        assert job.trigger.interval.total_seconds() == 10 * 60
        wallet_job = scheduler.get_job("periodic_wallet_snapshot")
        assert wallet_job is not None
        assert wallet_job.max_instances == 1
        assert wallet_job.executor == "ops"
        assert wallet_job.coalesce is True
        assert wallet_job.misfire_grace_time == 900
        assert wallet_job.next_run_time is not None
        assert wallet_job.trigger.interval.total_seconds() == 10 * 60

    def test_dry_run_does_not_register_auto_redeemer(self, db, tmp_path):
        scheduler = BackgroundScheduler()
        schedule_all_jobs(
            scheduler,
            db,
            tmp_path,
            db_path=str(tmp_path / "test.db"),
            stations={},
            dry_run=True,
        )

        assert scheduler.get_job("periodic_redeemer") is None
        assert scheduler.get_job("periodic_wallet_snapshot") is None

    def test_auto_redeemer_noop_does_not_write_duplicate_wallet_snapshot(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        init_db(db_path).close()
        cfg = Config(_env_file=None, dry_run=False)
        result = SimpleNamespace(
            status="OK",
            message="scanned=14 redeemable=0 submitted=0",
            submitted=0,
            settled_rows=0,
            failed=0,
            skipped_unmatched=0,
        )
        with (
            patch("hightempbot.runtime_config.get_config", return_value=cfg),
            patch("hightempbot.execution.polymarket_redeemer.run_redeemable_scan", return_value=result),
            patch("hightempbot.persistence.wallet_reconciliation.refresh_wallet_snapshot") as refresh,
        ):
            scheduler_jobs._run_auto_redeemer_job(db_path, dry_run=False)

        refresh.assert_not_called()

    def test_auto_redeemer_refreshes_wallet_snapshot_after_submit(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        init_db(db_path).close()
        cfg = Config(_env_file=None, dry_run=False)
        result = SimpleNamespace(
            status="OK",
            message="scanned=14 redeemable=1 submitted=1",
            submitted=1,
            settled_rows=1,
            failed=0,
            skipped_unmatched=0,
        )
        with (
            patch("hightempbot.runtime_config.get_config", return_value=cfg),
            patch("hightempbot.execution.polymarket_redeemer.run_redeemable_scan", return_value=result),
            patch("hightempbot.persistence.wallet_reconciliation.refresh_wallet_snapshot") as refresh,
        ):
            scheduler_jobs._run_auto_redeemer_job(db_path, dry_run=False)

        refresh.assert_called_once()

    def test_auto_redeemer_notification_uses_message_fingerprint_rate_key(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        init_db(db_path).close()
        cfg = Config(_env_file=None, dry_run=False)
        result = SimpleNamespace(
            status="OK",
            message="scanned=14 redeemable=1 submitted=1",
            submitted=1,
            settled_rows=1,
            failed=0,
            skipped_unmatched=0,
        )
        alerts = []

        def _scan(_conn, **kwargs):
            kwargs["notify"](
                "Polymarket auto-redeem",
                "submitted request=10 row=255 KLGA 2026-05-26 NO WIN",
            )
            return result

        def _capture(*args, **kwargs):
            alerts.append((args, kwargs))
            return True

        with (
            patch("hightempbot.runtime_config.get_config", return_value=cfg),
            patch("hightempbot.execution.polymarket_redeemer.run_redeemable_scan", side_effect=_scan),
            patch("hightempbot.execution.notify.send_alert", side_effect=_capture),
            patch("hightempbot.persistence.wallet_reconciliation.refresh_wallet_snapshot"),
        ):
            scheduler_jobs._run_auto_redeemer_job(db_path, dry_run=False)

        assert len(alerts) == 1
        args, kwargs = alerts[0]
        assert args[:2] == (
            "Polymarket auto-redeem",
            "submitted request=10 row=255 KLGA 2026-05-26 NO WIN",
        )
        assert kwargs["stage"] == "auto_redeem"
        assert kwargs["station_id"]

    def test_non_wu_station_gets_no_midnight_actuals_job(self, db, tmp_path):
        scheduler = BackgroundScheduler()
        schedule_all_jobs(scheduler, db, tmp_path, db_path=str(tmp_path / "test.db"), stations=_WITH_NON_WU)

        assert scheduler.get_job("midnight_RCTP") is None
        assert scheduler.get_job("betting_RCTP") is None
        assert scheduler.get_job("resolution_RCTP") is not None

    def test_enrollment_scan_schedules_jobs_for_new_station(self, db, tmp_path):
        scheduler = BackgroundScheduler()
        db_path = str(tmp_path / "test.db")
        schedule_all_jobs(scheduler, db, tmp_path, db_path=db_path, stations={})

        class _Resp:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        def _fake_get(url, params=None, timeout=None):
            if url.endswith("/markets"):
                return _Resp(200, [{
                    "question": "Will the highest temperature in Dallas be 75°F or higher on April 23?",
                }])
            if url.endswith("/events"):
                return _Resp(200, [{
                    "title": "Highest temperature in Dallas on April 23",
                    "markets": [{
                        "question": "Will the highest temperature in Dallas be 75°F or higher on April 23?",
                        "clobTokenIds": ["yes0", "no0"],
                        "outcomePrices": ["0.40", "0.60"],
                        "volume": 1000,
                        "conditionId": "m0",
                    }],
                }])
            raise AssertionError(url)

        def _fake_enroll(city_name, event, conn, enroll_db_path, data_dir="data", market_date=None, runtime_dry_run=True):
            conn.execute(
                """INSERT INTO enrolled_stations
                (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source,
                 poly_slug, status, step)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'DRY_RUN', 'day_1')""",
                ("KDAL", city_name, 32.85, -96.85, "US/Central", "F", "wu", "wu", "dallas"),
            )
            conn.commit()
            return "DRY_RUN"

        with patch.dict("hightempbot.ingestion.polymarket_prices._CITY_TO_ICAO", {}, clear=True), \
             patch("requests.get", side_effect=_fake_get), \
             patch("hightempbot.enrollment.pipeline.enroll_station", side_effect=_fake_enroll):
            scheduler.get_job("enrollment_scan").func()

        assert scheduler.get_job("midnight_KDAL") is not None
        assert scheduler.get_job("betting_KDAL") is not None
        assert scheduler.get_job("resolution_KDAL") is not None

    def test_enrollment_scan_retries_legacy_skipped_station(self, db, tmp_path):
        scheduler = BackgroundScheduler()
        db_path = str(tmp_path / "test.db")
        schedule_all_jobs(scheduler, db, tmp_path, db_path=db_path, stations={})

        db.execute(
            """INSERT INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source,
             poly_slug, status, step, skip_reason, coverage_pct)
            VALUES (?, ?, 0, 0, 'UTC', ?, ?, ?, ?, 'SKIPPED', 'legacy_bss', 'low_bss', 1.0)""",
            ("KDAL", "Dallas", "F", "wu", "wu", "dallas"),
        )
        db.commit()

        class _Resp:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        def _fake_get(url, params=None, timeout=None):
            if url.endswith("/markets"):
                return _Resp(200, [{
                    "question": "Will the highest temperature in Dallas be 75Â°F or higher on April 23?",
                }])
            if url.endswith("/events"):
                return _Resp(200, [{
                    "title": "Highest temperature in Dallas on April 23",
                    "resolutionSource": "https://www.wunderground.com/history/daily/us/tx/dallas/KDAL",
                    "markets": [{
                        "question": "Will the highest temperature in Dallas be 75Â°F or higher on April 23?",
                        "clobTokenIds": ["yes0", "no0"],
                        "outcomePrices": ["0.40", "0.60"],
                        "volume": 1000,
                        "conditionId": "m0",
                    }],
                }])
            raise AssertionError(url)

        def _fake_enroll(city_name, event, conn, enroll_db_path, data_dir="data", market_date=None, runtime_dry_run=True):
            conn.execute("UPDATE enrolled_stations SET status = 'DRY_RUN', step = 'day_1' WHERE icao = 'KDAL'")
            conn.commit()
            return "DRY_RUN"

        with patch.dict("hightempbot.ingestion.polymarket_prices._CITY_TO_ICAO", {}, clear=True), \
             patch("requests.get", side_effect=_fake_get), \
             patch("hightempbot.enrollment.pipeline.enroll_station", side_effect=_fake_enroll):
            scheduler.get_job("enrollment_scan").func()

        assert scheduler.get_job("midnight_KDAL") is not None
        assert scheduler.get_job("betting_KDAL") is not None
        assert scheduler.get_job("resolution_KDAL") is not None

    def test_enrollment_scan_reprobes_unknown_source_skipped_station(self, db, tmp_path):
        scheduler = BackgroundScheduler()
        db_path = str(tmp_path / "test.db")
        schedule_all_jobs(scheduler, db, tmp_path, db_path=db_path, stations={})

        db.execute(
            """INSERT INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source,
             poly_slug, status, step, skip_reason, coverage_pct)
            VALUES (?, ?, 0, 0, 'UTC', ?, ?, ?, ?, 'SKIPPED', 'source',
                    'unknown_source', NULL)""",
            ("UNKNOWN_dallas", "Dallas", "F", "unknown", "unknown", "dallas"),
        )
        db.commit()

        class _Resp:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        def _fake_get(url, params=None, timeout=None):
            if url.endswith("/markets"):
                return _Resp(200, [{
                    "question": "Will the highest temperature in Dallas be 75Â°F or higher on April 23?",
                }])
            if url.endswith("/events"):
                return _Resp(200, [{
                    "title": "Highest temperature in Dallas on April 23",
                    "resolutionSource": "https://www.wunderground.com/history/daily/us/tx/dallas/KDAL",
                    "markets": [{
                        "question": "Will the highest temperature in Dallas be 75Â°F or higher on April 23?",
                        "clobTokenIds": ["yes0", "no0"],
                        "outcomePrices": ["0.40", "0.60"],
                        "volume": 1000,
                        "conditionId": "m0",
                    }],
                }])
            raise AssertionError(url)

        def _fake_enroll(city_name, event, conn, enroll_db_path, data_dir="data", market_date=None, runtime_dry_run=True):
            conn.execute("DELETE FROM enrolled_stations WHERE city = ?", (city_name,))
            conn.execute(
                """INSERT INTO enrolled_stations
                (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source,
                 poly_slug, status, step)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'DRY_RUN', 'day_1')""",
                ("KDAL", city_name, 32.85, -96.85, "US/Central", "F", "wu", "wu", "dallas"),
            )
            conn.commit()
            return "DRY_RUN"

        with patch.dict("hightempbot.ingestion.polymarket_prices._CITY_TO_ICAO", {}, clear=True), \
             patch("requests.get", side_effect=_fake_get), \
             patch("hightempbot.enrollment.pipeline.enroll_station", side_effect=_fake_enroll):
            scheduler.get_job("enrollment_scan").func()

        assert scheduler.get_job("midnight_KDAL") is not None
        assert scheduler.get_job("betting_KDAL") is not None
        assert scheduler.get_job("resolution_KDAL") is not None

