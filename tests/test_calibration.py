"""Tests for the calibration pipeline (EMOS only)."""

from datetime import date, timedelta
from unittest.mock import patch

import numpy as np
import pytest

from hightempbot.db.connection import init_db
from hightempbot.calibration.emos import EMOSParams, fit_emos, predict_emos, emos_probability
from hightempbot.calibration.model import CalibrationModel, CalibrationNotReadyError
from hightempbot.calibration.monthly_retrain import _retrain_station
from hightempbot.calibration.store import save_emos, load_emos
from hightempbot.execution.strategy_constants import EXPECTED_MODELS


@pytest.fixture
def db(tmp_path):
    return init_db(tmp_path / "test.db")


def _synthetic_data(n_days=60, n_members=82, seed=42):
    """Generate synthetic (ensemble, actual) pairs for testing."""
    rng = np.random.RandomState(seed)
    true_temps = 30.0 + 5.0 * rng.randn(n_days)
    ensemble = true_temps[:, None] + 2.0 * rng.randn(n_days, n_members)
    actuals = true_temps + 1.0 * rng.randn(n_days)
    return ensemble, actuals


class TestEMOS:
    def test_fit_converges(self):
        ensemble, actuals = _synthetic_data()
        params = fit_emos(ensemble, actuals)
        assert params is not None
        assert params.n_samples == 60
        assert 0.5 < params.b < 1.5

    def test_predict_returns_reasonable_values(self):
        ensemble, actuals = _synthetic_data()
        params = fit_emos(ensemble, actuals)
        mu, sigma = predict_emos(params, ensemble[0])
        assert 15.0 < mu < 45.0
        assert sigma > 0.1

    def test_probability_between_0_and_1(self):
        ensemble, actuals = _synthetic_data()
        params = fit_emos(ensemble, actuals)
        for threshold in [20.0, 30.0, 40.0]:
            p = emos_probability(params, ensemble[0], threshold)
            assert 0.0 <= p <= 1.0

    def test_probability_decreases_with_threshold(self):
        ensemble, actuals = _synthetic_data()
        params = fit_emos(ensemble, actuals)
        members = ensemble[0]
        p20 = emos_probability(params, members, 20.0)
        p30 = emos_probability(params, members, 30.0)
        p40 = emos_probability(params, members, 40.0)
        assert p20 >= p30 >= p40

    def test_too_few_samples_returns_none(self):
        ensemble = np.random.randn(5, 82)
        actuals = np.random.randn(5)
        assert fit_emos(ensemble, actuals) is None


class TestCalibrationModel:
    def test_not_ready_without_params(self):
        model = CalibrationModel("KLGA", 1)
        assert not model.is_ready()

    def test_not_ready_with_few_samples(self):
        params = EMOSParams(a=0, b=1, c=0, d=0, n_samples=10)
        model = CalibrationModel("KLGA", 1, emos_params=params)
        assert not model.is_ready()

    def test_ready_with_enough_samples(self):
        params = EMOSParams(a=0, b=1, c=0, d=0, n_samples=30)
        model = CalibrationModel("KLGA", 1, emos_params=params)
        assert model.is_ready()

    def test_predict_raises_when_not_ready(self):
        model = CalibrationModel("KLGA", 1)
        with pytest.raises(CalibrationNotReadyError):
            model.predict(np.array([30.0] * 82), 35.0)

    def test_predict_returns_probability(self):
        ensemble, actuals = _synthetic_data()
        params = fit_emos(ensemble, actuals)
        model = CalibrationModel("KLGA", 1, emos_params=params)
        p = model.predict(ensemble[0], 30.0)
        assert 0.0 <= p <= 1.0

    def test_retrain_rejects_duplicate_source_partial_ensemble(self, db):
        """A duplicate centre from another source must not replace a missing model."""
        from hightempbot.calibration.model import retrain, ROLLING_WINDOW_DAYS

        # Anchor to today so the fixture's 30 days fall inside the rolling
        # training window (retrain filters to local_date >= today - WINDOW).
        start = date.today() - timedelta(days=ROLLING_WINDOW_DAYS - 1)
        partial_models = EXPECTED_MODELS[:-1]
        duplicate_centre = EXPECTED_MODELS[0]
        for day in range(30):
            target = (start + timedelta(days=day)).isoformat()
            db.execute(
                "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) "
                "VALUES (?, ?, ?, 'wu')",
                ("KDAL", target, 30.0 + day * 0.1),
            )
            for member, centre in enumerate(partial_models, start=1):
                db.execute(
                    """INSERT INTO forecast_archive
                    (station_id, target_date, horizon, issue_date, centre, member,
                     tmax_celsius, source)
                    VALUES (?, ?, 1, ?, ?, ?, ?, 'openmeteo')""",
                    ("KDAL", target, target, centre, member, 29.0 + member),
                )
            db.execute(
                """INSERT INTO forecast_archive
                (station_id, target_date, horizon, issue_date, centre, member,
                 tmax_celsius, source)
                VALUES (?, ?, 1, ?, ?, 999, ?, 'live')""",
                ("KDAL", target, target, duplicate_centre, 99.0),
            )
        db.commit()

        with patch(
            "hightempbot.calibration.model.fit_emos",
            return_value=EMOSParams(a=0, b=1, c=0, d=1, n_samples=30),
        ) as mock_fit:
            model = retrain("KDAL", 1, db)

        assert model is None
        mock_fit.assert_not_called()

    def test_retrain_dedupes_duplicate_openmeteo_centres(self, db):
        """Re-backfilled openmeteo rows must not make a complete day look partial."""
        from hightempbot.calibration.model import retrain, ROLLING_WINDOW_DAYS

        # Anchor to today so the fixture's 30 days fall inside the rolling
        # training window.
        start = date.today() - timedelta(days=ROLLING_WINDOW_DAYS - 1)
        duplicate_centre = EXPECTED_MODELS[0]
        for day in range(30):
            target = (start + timedelta(days=day)).isoformat()
            db.execute(
                "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) "
                "VALUES (?, ?, ?, 'wu')",
                ("KDAL", target, 30.0 + day * 0.1),
            )
            for member, centre in enumerate(EXPECTED_MODELS, start=1):
                db.execute(
                    """INSERT INTO forecast_archive
                    (station_id, target_date, horizon, issue_date, centre, member,
                     tmax_celsius, source, ingested_at)
                    VALUES (?, ?, 1, ?, ?, ?, ?, 'openmeteo', ?)""",
                    ("KDAL", target, target, centre, member, 29.0 + member, "2026-01-01 00:00:00"),
                )
            db.execute(
                """INSERT INTO forecast_archive
                (station_id, target_date, horizon, issue_date, centre, member,
                 tmax_celsius, source, ingested_at)
                VALUES (?, ?, 1, ?, ?, 999, ?, 'openmeteo', ?)""",
                ("KDAL", target, target, duplicate_centre, 99.0, "2026-01-02 00:00:00"),
            )
        db.commit()

        with patch(
            "hightempbot.calibration.model.fit_emos",
            return_value=EMOSParams(a=0, b=1, c=0, d=1, n_samples=30),
        ) as mock_fit:
            model = retrain("KDAL", 1, db)

        assert model is not None
        mock_fit.assert_called_once()

    def test_retrain_excludes_data_outside_rolling_window(self, db):
        """Days older than ROLLING_WINDOW_DAYS must be silently excluded."""
        from hightempbot.calibration.model import retrain, ROLLING_WINDOW_DAYS

        fresh_start = date.today() - timedelta(days=ROLLING_WINDOW_DAYS - 1)
        stale_start = fresh_start - timedelta(days=ROLLING_WINDOW_DAYS + 1)

        # Stale block: 30 days, complete ensemble, but BEFORE the cutoff.
        for day in range(30):
            target = (stale_start + timedelta(days=day)).isoformat()
            db.execute(
                "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) "
                "VALUES (?, ?, ?, 'wu')",
                ("KDAL", target, 25.0 + day * 0.1),
            )
            for member, centre in enumerate(EXPECTED_MODELS, start=1):
                db.execute(
                    """INSERT INTO forecast_archive
                    (station_id, target_date, horizon, issue_date, centre, member,
                     tmax_celsius, source)
                    VALUES (?, ?, 1, ?, ?, ?, ?, 'openmeteo')""",
                    ("KDAL", target, target, centre, member, 24.0 + member),
                )

        # Fresh block: 30 days, complete ensemble, INSIDE the cutoff.
        for day in range(30):
            target = (fresh_start + timedelta(days=day)).isoformat()
            db.execute(
                "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) "
                "VALUES (?, ?, ?, 'wu')",
                ("KDAL", target, 30.0 + day * 0.1),
            )
            for member, centre in enumerate(EXPECTED_MODELS, start=1):
                db.execute(
                    """INSERT INTO forecast_archive
                    (station_id, target_date, horizon, issue_date, centre, member,
                     tmax_celsius, source)
                    VALUES (?, ?, 1, ?, ?, ?, ?, 'openmeteo')""",
                    ("KDAL", target, target, centre, member, 29.0 + member),
                )
        db.commit()

        captured: dict[str, int] = {}

        def _capture_n_samples(ensemble_matrix, actuals_array):
            captured["n"] = int(actuals_array.shape[0])
            return EMOSParams(a=0, b=1, c=0, d=1, n_samples=captured["n"])

        with patch(
            "hightempbot.calibration.model.fit_emos",
            side_effect=_capture_n_samples,
        ):
            model = retrain("KDAL", 1, db)

        assert model is not None
        # Fresh window has exactly 30 pairs; stale 30 must be excluded.
        assert captured.get("n") == 30

    def test_retrain_ignores_non_wu_actuals(self, db):
        """Non-WU actual rows must not train EMOS even with complete forecasts."""
        from hightempbot.calibration.model import retrain, ROLLING_WINDOW_DAYS

        start = date.today() - timedelta(days=ROLLING_WINDOW_DAYS - 1)
        for day in range(30):
            target = (start + timedelta(days=day)).isoformat()
            db.execute(
                "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) "
                "VALUES (?, ?, ?, 'ncei')",
                ("KDAL", target, 30.0 + day * 0.1),
            )
            for member, centre in enumerate(EXPECTED_MODELS, start=1):
                db.execute(
                    """INSERT INTO forecast_archive
                    (station_id, target_date, horizon, issue_date, centre, member,
                     tmax_celsius, source)
                    VALUES (?, ?, 1, ?, ?, ?, ?, 'openmeteo')""",
                    ("KDAL", target, target, centre, member, 29.0 + member),
                )
        db.commit()

        with patch("hightempbot.calibration.model.fit_emos") as mock_fit:
            model = retrain("KDAL", 1, db)

        assert model is None
        mock_fit.assert_not_called()


class TestCalibrationStore:
    def test_emos_round_trip(self, db):
        params = EMOSParams(a=1.5, b=0.95, c=-0.3, d=0.1, n_samples=60)
        save_emos(db, "KLGA", 1, params)

        loaded = load_emos(db, "KLGA", 1)
        assert loaded is not None
        assert loaded.a == pytest.approx(1.5)
        assert loaded.b == pytest.approx(0.95)
        assert loaded.n_samples == 60

    def test_load_nonexistent_returns_none(self, db):
        assert load_emos(db, "XXXX", 1) is None


class TestMonthlyRetrain:
    def test_retrain_station_handles_retrain_commit(self, db):
        ready_model = CalibrationModel(
            "KLGA",
            1,
            EMOSParams(a=0.0, b=1.0, c=0.0, d=0.0, n_samples=60),
        )

        def _fake_retrain(station_id, horizon, conn):
            conn.execute(
                """INSERT INTO calibration_params
                (station_id, horizon, threshold_bucket, param_type, params_blob, n_samples)
                VALUES (?, ?, 0.0, 'emos', ?, ?)""",
                (station_id, horizon, b"{}", ready_model.emos_params.n_samples),
            )
            conn.commit()
            return ready_model

        with patch("hightempbot.calibration.model.retrain", side_effect=_fake_retrain):
            result = _retrain_station(db, "KLGA", "2026-03")

        assert result["kept"] == "ok"
        history = db.execute(
            "SELECT kept FROM retrain_history WHERE station_id = ? AND retrain_month = ?",
            ("KLGA", "2026-03"),
        ).fetchone()
        assert history["kept"] == "ok"
