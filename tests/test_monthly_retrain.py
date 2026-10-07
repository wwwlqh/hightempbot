"""Tests for Unit 8: monthly_retrain._retrain_station triggers LUT refresh."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hightempbot.calibration.monthly_retrain import _retrain_station
from hightempbot.db.connection import get_connection, init_db


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    conn = get_connection(str(db_path))
    yield conn
    conn.close()


def _seed_existing_lut(conn: sqlite3.Connection, station_id: str) -> None:
    """Plant one bucket row so the post-retrain hook takes the rebuild branch."""
    conn.execute(
        "INSERT INTO lut_bucket_stats "
        "(station_id, pred_bucket_low, pred_bucket_high, n, hits, "
        " observed, mean_pred, refreshed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (station_id, 0.40, 0.60, 100, 45, 0.45, 0.50, "2026-01-01 00:00:00"),
    )
    conn.commit()


def _ready_model(n_samples: int = 60) -> MagicMock:
    model = MagicMock()
    model.is_ready.return_value = True
    model.emos_params = MagicMock(n_samples=n_samples)
    return model


class TestRetrainStationLutHook:
    def test_successful_retrain_with_existing_lut_triggers_rebuild(self, db):
        _seed_existing_lut(db, "KDAL")

        with patch(
            "hightempbot.calibration.model.retrain",
            return_value=_ready_model(n_samples=42),
        ):
            with patch(
                "hightempbot.calibration.lut.rebuild_lut",
                return_value=8,
            ) as mock_rebuild:
                with patch(
                    "hightempbot.calibration.lut.seed_lut_from_history",
                ) as mock_seed:
                    result = _retrain_station(db, "KDAL", "2026-03")

        assert result["kept"] == "ok"
        mock_rebuild.assert_called_once_with(db, "KDAL")
        mock_seed.assert_not_called()
        health = db.execute(
            "SELECT status, message FROM pipeline_health "
            "WHERE station_id = 'KDAL' AND stage = 'lut' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert health["status"] == "OK"
        assert "rebuild_lut" in health["message"]

    def test_successful_retrain_with_no_lut_seeds_from_history(self, db):
        # No pre-existing lut_bucket_stats row for KDAL.
        with patch(
            "hightempbot.calibration.model.retrain",
            return_value=_ready_model(),
        ):
            with patch(
                "hightempbot.calibration.lut.rebuild_lut",
            ) as mock_rebuild:
                with patch(
                    "hightempbot.calibration.lut.seed_lut_from_history",
                    return_value=(30, 240),
                ) as mock_seed:
                    result = _retrain_station(db, "KDAL", "2026-03")

        assert result["kept"] == "ok"
        mock_seed.assert_called_once_with(db, "KDAL")
        mock_rebuild.assert_not_called()
        health = db.execute(
            "SELECT status, message FROM pipeline_health "
            "WHERE station_id = 'KDAL' AND stage = 'lut' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert health["status"] == "OK"
        assert "seeded" in health["message"]

    def test_seed_with_empty_history_logs_skipped(self, db):
        with patch(
            "hightempbot.calibration.model.retrain",
            return_value=_ready_model(),
        ):
            with patch(
                "hightempbot.calibration.lut.seed_lut_from_history",
                return_value=(0, 0),
            ):
                _retrain_station(db, "KDAL", "2026-03")

        health = db.execute(
            "SELECT status FROM pipeline_health "
            "WHERE station_id = 'KDAL' AND stage = 'lut' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert health["status"] == "SKIPPED"

    def test_retrain_failure_does_not_touch_lut(self, db):
        _seed_existing_lut(db, "KDAL")

        with patch(
            "hightempbot.calibration.model.retrain",
            return_value=None,  # insufficient data
        ):
            with patch(
                "hightempbot.calibration.lut.rebuild_lut",
            ) as mock_rebuild:
                with patch(
                    "hightempbot.calibration.lut.seed_lut_from_history",
                ) as mock_seed:
                    result = _retrain_station(db, "KDAL", "2026-03")

        assert result["kept"] == "skip"
        mock_rebuild.assert_not_called()
        mock_seed.assert_not_called()
        # No 'lut' stage log row — only 'retrain' ERROR.
        lut_row = db.execute(
            "SELECT COUNT(*) as cnt FROM pipeline_health "
            "WHERE station_id = 'KDAL' AND stage = 'lut'"
        ).fetchone()
        assert lut_row["cnt"] == 0

    def test_lut_refresh_exception_logged_but_does_not_fail_retrain(self, db):
        _seed_existing_lut(db, "KDAL")

        with patch(
            "hightempbot.calibration.model.retrain",
            return_value=_ready_model(),
        ):
            with patch(
                "hightempbot.calibration.lut.rebuild_lut",
                side_effect=RuntimeError("disk full"),
            ):
                result = _retrain_station(db, "KDAL", "2026-03")

        # Retrain result is still "ok" — only the LUT step failed.
        assert result["kept"] == "ok"
        health = db.execute(
            "SELECT status, message FROM pipeline_health "
            "WHERE station_id = 'KDAL' AND stage = 'lut' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert health["status"] == "ERROR"
        assert "disk full" in health["message"]

    def test_unsupported_source_clears_lut_and_skips_refresh(self, db):
        _seed_existing_lut(db, "LLBG")
        db.execute(
            """INSERT INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source,
             poly_slug, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("LLBG", "Tel Aviv", 0.0, 0.0, "Asia/Jerusalem", "C", "ims", "ims", "tel-aviv", "DRY_RUN"),
        )
        db.execute(
            "INSERT INTO pred_bucket_history "
            "(station_id, local_date, pred_bucket_low, pred_bucket_high, emos_p, hit) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("LLBG", "2026-04-01", 0.40, 0.60, 0.50, 1),
        )
        db.commit()

        with patch(
            "hightempbot.calibration.model.retrain",
            return_value=_ready_model(),
        ):
            with patch(
                "hightempbot.calibration.lut.rebuild_lut",
            ) as mock_rebuild:
                with patch(
                    "hightempbot.calibration.lut.seed_lut_from_history",
                ) as mock_seed:
                    result = _retrain_station(db, "LLBG", "2026-03")

        assert result["kept"] == "ok"
        mock_rebuild.assert_not_called()
        mock_seed.assert_not_called()
        assert db.execute(
            "SELECT COUNT(*) AS n FROM lut_bucket_stats WHERE station_id = 'LLBG'"
        ).fetchone()["n"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS n FROM pred_bucket_history WHERE station_id = 'LLBG'"
        ).fetchone()["n"] == 0
        health = db.execute(
            "SELECT status, message FROM pipeline_health "
            "WHERE station_id = 'LLBG' AND stage = 'lut' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert health["status"] == "SKIPPED"
        assert "unsupported source" in health["message"]
