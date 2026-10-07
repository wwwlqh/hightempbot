"""Calibration model — EMOS (Ensemble Model Output Statistics).

Exposes:
- CalibrationModel.predict(threshold) → P(tmax > threshold)
- CalibrationModel.is_ready() → has enough training data
- retrain(station, horizon, conn) → re-fits from DB data
"""

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

# Minimum (forecast, actual) pairs before a station/horizon is considered calibrated.
# 2026-05-14: restored to 30 to match the polybot wiki canonical spec
# (EMOS Calibration: `is_ready()` requires n_samples >= 30, stricter than the
# LUT's 20). Prior 30 -> 20 lowering was reverted; live stations whose 30-day
# rolling window yields < 30 pairs will stall on `not is_ready()` and skip
# the betting pipeline until forecast-archive backfill catches them up.
MIN_PAIRS = 30

# Rolling training window. The EMOS retrain pulls (forecast, actual) pairs
# whose target_date falls in the last ROLLING_WINDOW_DAYS days only. This
# matches the backtest's training shape and keeps the fit adapting to recent
# regime/seasonal changes instead of being dominated by the full history.
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
        """Return P(tmax > threshold).

        Raises CalibrationNotReadyError if model is not ready.
        """
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
    """Re-fit EMOS model from DB data.

    Loads all (forecast, actual) pairs for this station/horizon
    and fits EMOS parameters.
    """
    # Rolling-window cutoff. We restrict both actuals and forecast_dates to
    # the last ROLLING_WINDOW_DAYS days so the EMOS fit reflects recent
    # regime/seasonal conditions and stays symmetric with backtest training
    # shape. The cutoff is computed once at retrain time (today - N).
    cutoff_date = (date.today() - timedelta(days=ROLLING_WINDOW_DAYS)).isoformat()

    # Load actuals (rolling window)
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

    # Build aligned (ensemble_matrix, actuals) arrays
    actuals_dict = {row["local_date"]: row["tmax_celsius"] for row in actuals_rows}

    from hightempbot.execution.strategy_constants import EXPECTED_MODELS, REQUIRED_MEMBERS

    expected_centres = tuple(EXPECTED_MODELS)
    expected_set = set(expected_centres)

    # Single SELECT for all ensemble members in the rolling window. Replaces
    # the previous N+1 pattern (1 DISTINCT-dates query + 1 per-date SELECT)
    # — at ROLLING_WINDOW_DAYS=30 the periodic retrain across 50 stations
    # collapses from ~1,550 round-trips to ~50. Index
    # idx_forecast_station_date(station_id, target_date, horizon) covers
    # the predicate.
    placeholders = ",".join("?" for _ in EXPECTED_MODELS)
    member_rows = conn.execute(
        f"SELECT target_date, centre, tmax_celsius FROM forecast_archive "
        f"WHERE station_id = ? AND horizon = ? AND target_date >= ? "
        f"AND source = 'openmeteo' "
        f"AND centre IN ({placeholders}) "
        f"ORDER BY target_date, centre, ingested_at DESC",
        (station_id, horizon, cutoff_date, *EXPECTED_MODELS),
    ).fetchall()

    # Group by (target_date, centre) and keep the latest ingested row for
    # each pair (the ORDER BY ingested_at DESC means the first hit per
    # centre per date is the freshest, so subsequent hits are skipped).
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

    # Filter to days with consistent member count.
    # Use the most common count (9 if BoM is dead, 10 if all models alive).
    # Training and live must use the same ensemble size.
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

    # Fit EMOS
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

    # Save to DB
    save_emos(conn, station_id, horizon, emos_params)

    return model
