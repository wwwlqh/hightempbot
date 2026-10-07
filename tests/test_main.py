from __future__ import annotations

import threading
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from hightempbot import main as main_module
from hightempbot.db.connection import init_db
from hightempbot.main import (
    _auto_retrain_missing_calibration,
    _dashboard_bind_host,
    _install_thread_exception_alerts,
    _validate_dashboard_live_safety,
    _sync_station_runtime_mode,
)


def test_auto_retrain_missing_calibration_refreshes_lut(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        for i in range(30):
            target_date = (date(2025, 3, 1) + timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO actuals "
                "(station_id, local_date, tmax_celsius, source) "
                "VALUES (?, ?, ?, 'wu')",
                ("KDAL", target_date, 20.0 + i * 0.1),
            )
            conn.execute(
                "INSERT OR REPLACE INTO forecast_archive "
                "(station_id, target_date, horizon, issue_date, centre, member, "
                " tmax_celsius, source) VALUES (?, ?, ?, ?, ?, ?, ?, 'openmeteo')",
                ("KDAL", target_date, 1, target_date, "ecmwf_ifs025", 1, 19.5 + i * 0.1),
            )
        conn.commit()

        ready_model = SimpleNamespace(is_ready=lambda: True)
        with patch("hightempbot.calibration.model.retrain", return_value=ready_model) as mock_retrain, \
             patch("hightempbot.calibration.monthly_retrain.refresh_lut_after_retrain") as mock_refresh:
            _auto_retrain_missing_calibration(conn)

        mock_retrain.assert_called_once_with("KDAL", 1, conn)
        mock_refresh.assert_called_once_with(conn, "KDAL", "startup")
    finally:
        conn.close()


def test_sync_station_runtime_mode_demotes_live_to_dry_run(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        conn.execute(
            """INSERT INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source,
             poly_slug, status, step, step_detail)
            VALUES (?, ?, 0, 0, 'UTC', 'F', 'wu', 'wu', ?, 'LIVE', 'active', 'Live trading enabled')""",
            ("KDAL", "Dallas", "dallas"),
        )
        conn.commit()

        updated = _sync_station_runtime_mode(conn, dry_run=True)
        row = conn.execute(
            "SELECT status, step, step_detail FROM enrolled_stations WHERE icao = 'KDAL'"
        ).fetchone()
        assert updated == 1
        assert row["status"] == "DRY_RUN"
        assert row["step"] == "day_1"
        assert row["step_detail"] == "Dry-run started"
    finally:
        conn.close()


def test_sync_station_runtime_mode_promotes_dry_run_to_live(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        conn.execute(
            """INSERT INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source,
             poly_slug, status, step, step_detail)
            VALUES (?, ?, 0, 0, 'UTC', 'F', 'wu', 'wu', ?, 'DRY_RUN', 'day_1', 'Dry-run started')""",
            ("KDAL", "Dallas", "dallas"),
        )
        conn.commit()

        updated = _sync_station_runtime_mode(conn, dry_run=False)
        row = conn.execute(
            "SELECT status, step, step_detail FROM enrolled_stations WHERE icao = 'KDAL'"
        ).fetchone()
        assert updated == 1
        assert row["status"] == "LIVE"
        assert row["step"] == "active"
        assert row["step_detail"] == "Live trading enabled"
    finally:
        conn.close()


def test_dashboard_bind_host_defaults_to_loopback_without_password():
    cfg = SimpleNamespace(dashboard_bind_host="", dashboard_pass="")

    assert _dashboard_bind_host(cfg) == "127.0.0.1"


def test_dashboard_bind_host_preserves_legacy_public_bind_with_password():
    cfg = SimpleNamespace(dashboard_bind_host="", dashboard_pass="secret")

    assert _dashboard_bind_host(cfg) == "0.0.0.0"


def test_live_public_dashboard_requires_password():
    cfg = SimpleNamespace(
        dashboard_pass="",
        dashboard_tls_terminated=False,
    )

    try:
        _validate_dashboard_live_safety(cfg, dry_run=False, dash_host="0.0.0.0")
    except RuntimeError as exc:
        assert "DASHBOARD_PASS" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("public live dashboard without auth was allowed")

    cfg.dashboard_pass = "secret"
    try:
        _validate_dashboard_live_safety(cfg, dry_run=False, dash_host="0.0.0.0")
    except RuntimeError as exc:
        assert "DASHBOARD_TLS_TERMINATED" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("public live dashboard without TLS was allowed")

    cfg.dashboard_tls_terminated = True
    _validate_dashboard_live_safety(cfg, dry_run=False, dash_host="0.0.0.0")
    cfg.dashboard_tls_terminated = False
    _validate_dashboard_live_safety(cfg, dry_run=False, dash_host="127.0.0.1")
    _validate_dashboard_live_safety(cfg, dry_run=True, dash_host="0.0.0.0")


def test_thread_exception_hook_sends_operational_alert(monkeypatch):
    previous_calls = []
    captured = []

    def _previous_hook(args):
        previous_calls.append(args)

    def _capture_alert(title, message, **kwargs):
        captured.append((title, message, kwargs))
        return True

    monkeypatch.setattr(threading, "excepthook", _previous_hook)
    monkeypatch.setattr(main_module, "_send_operational_alert", _capture_alert)

    cfg = SimpleNamespace()
    _install_thread_exception_alerts(cfg)

    args = SimpleNamespace(
        exc_type=RuntimeError,
        exc_value=RuntimeError("thread blew up"),
        exc_traceback=None,
        thread=SimpleNamespace(name="boot-worker"),
    )
    threading.excepthook(args)

    assert previous_calls == [args]
    assert captured == [(
        "CRITICAL: HighTempBot thread crashed",
        (
            "Thread: boot-worker\n"
            "Exception: RuntimeError: thread blew up\n"
            "Action: check logs/hightempbot.log for the full traceback."
        ),
        {
            "cfg": cfg,
            "stage": "thread_crash",
            "station_id": "boot-worker",
        },
    )]
