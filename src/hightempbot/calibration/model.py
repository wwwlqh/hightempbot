"""EMOS calibration model per station/horizon and its retrain from the DB."""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta

import numpy as np

from hightempbot.calibration.emos import EMOSParams, fit_emos, emos_probability
from hightempbot.calibration.store import save_emos
from hightempbot.persistence.actuals import actual_source_clause

logger = logging.getLogger(__name__)

# Pairs needed before a model is ready to bet with.
MIN_PAIRS = 30

# Train on the last N days only, as the backtest does.
ROLLING_WINDOW_DAYS = 30


class CalibrationNotReadyError(Exception):
    pass


class CalibrationModel:
    """Per (station, horizon) calibration model."""

    def __init__(
        self,
        station_id: str,
        horizon: int,
        emos_params: EMOSParams | None = None,
    ):
        self.station_id = station_id
        self.horizon = horizon
        self.emos_params = emos_params

    def is_ready(self) -> bool:
        return self.emos_params is not None and self.emos_params.n_samples >= MIN_PAIRS

    def predict(self, ensemble_members: np.ndarray, threshold: float) -> float:
        """P(tmax > threshold); raises CalibrationNotReadyError if not ready."""
        if not self.is_ready():
            raise CalibrationNotReadyError(
                f"{self.station_id} h={self.horizon}: not calibrated "
                f"({self.emos_params.n_samples if self.emos_params else 0} pairs)"
            )

        return emos_probability(self.emos_params, ensemble_members, threshold)


def retrain(
    station_id: str,
    horizon: int,
    conn: sqlite3.Connection,
) -> CalibrationModel | None:
    """Fit EMOS on the last ROLLING_WINDOW_DAYS of (forecast, actual) pairs and save it."""
    cutoff_date = (date.today() - timedelta(days=ROLLING_WINDOW_DAYS)).isoformat()

    actuals_clause, actuals_params = actual_source_clause()
    actuals_rows = conn.execute(
        "SELECT local_date, tmax_celsius FROM actuals "
        "WHERE station_id = ? AND local_date >= ? "
        f"AND {actuals_clause} "
        "ORDER BY local_date",
        (station_id, cutoff_date, *actuals_params),
    ).fetchall()

    if len(actuals_rows) < MIN_PAIRS:
        logger.info(
            "%s h=%d: only %d actuals in last %dd, need %d",
            station_id, horizon, len(actuals_rows), ROLLING_WINDOW_DAYS, MIN_PAIRS,
        )
        return None

    actuals_dict = {row["local_date"]: row["tmax_celsius"] for row in actuals_rows}

    from hightempbot.execution.strategy_constants import EXPECTED_MODELS, REQUIRED_MEMBERS

    expected_centres = tuple(EXPECTED_MODELS)
    expected_set = set(expected_centres)

    placeholders = ",".join("?" for _ in EXPECTED_MODELS)
    member_rows = conn.execute(
        f"SELECT target_date, centre, tmax_celsius FROM forecast_archive "
        f"WHERE station_id = ? AND horizon = ? AND target_date >= ? "
        f"AND source = 'openmeteo' "
        f"AND centre IN ({placeholders}) "
        f"ORDER BY target_date, centre, ingested_at DESC",
        (station_id, horizon, cutoff_date, *EXPECTED_MODELS),
    ).fetchall()

    # Keep the newest row per (date, centre).
    by_date: dict[str, dict[str, float]] = defaultdict(dict)
    for row in member_rows:
        target_date = row["target_date"]
        centre = row["centre"]
        if centre in by_date[target_date]:
            continue
        by_date[target_date][centre] = float(row["tmax_celsius"])

    aligned_ensembles: list[np.ndarray] = []
    aligned_actuals: list[float] = []

    for target_date in sorted(by_date.keys()):
        if target_date not in actuals_dict:
            continue
        by_centre = by_date[target_date]
        if len(by_centre) != REQUIRED_MEMBERS or set(by_centre) != expected_set:
            continue
        member_values = np.array([by_centre[c] for c in expected_centres])
        aligned_ensembles.append(member_values)
        aligned_actuals.append(actuals_dict[target_date])

    if len(aligned_ensembles) < MIN_PAIRS:
        logger.info(
            "%s h=%d: only %d aligned pairs, need %d",
            station_id, horizon, len(aligned_ensembles), MIN_PAIRS,
        )
        return None

    # Keep only days with the most common member count.
    size_counts = Counter(len(e) for e in aligned_ensembles)
    if not size_counts:
        logger.info("%s h=%d: no aligned ensembles", station_id, horizon)
        return None
    target_size = size_counts.most_common(1)[0][0]

    filtered_ensembles = []
    filtered_actuals = []
    for e, a in zip(aligned_ensembles, aligned_actuals):
        if len(e) == target_size:
            filtered_ensembles.append(e)
            filtered_actuals.append(a)
        else:
            logger.debug(
                "%s h=%d: dropping day with %d members (need %d)",
                station_id, horizon, len(e), target_size,
            )

    if len(filtered_ensembles) < MIN_PAIRS:
        logger.info(
            "%s h=%d: only %d days with %d members (need %d days)",
            station_id, horizon, len(filtered_ensembles), target_size, MIN_PAIRS,
        )
        return None

    ensemble_matrix = np.array(filtered_ensembles)
    actuals_array = np.array(filtered_actuals)

    emos_params = fit_emos(ensemble_matrix, actuals_array)
    if emos_params is None:
        return None

    model = CalibrationModel(
        station_id=station_id,
        horizon=horizon,
        emos_params=emos_params,
    )

    logger.info(
        "%s h=%d: retrained — %d pairs",
        station_id, horizon, len(actuals_array),
    )

    save_emos(conn, station_id, horizon, emos_params)

    return model
