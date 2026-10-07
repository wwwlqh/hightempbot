"""Calibration parameter persistence — read/write EMOS params to SQLite."""

from __future__ import annotations

import json
import sqlite3

from hightempbot.calibration.emos import EMOSParams


def save_emos(
    conn: sqlite3.Connection,
    station_id: str,
    horizon: int,
    params: EMOSParams,
) -> None:
    """Persist EMOS params as JSON to calibration_params table."""
    blob = json.dumps({
        "a": params.a,
        "b": params.b,
        "c": params.c,
        "d": params.d,
        "n_samples": params.n_samples,
    }).encode("utf-8")
    # Use threshold_bucket=0.0 (not NULL) so UNIQUE constraint works for REPLACE.
    conn.execute(
        "INSERT OR REPLACE INTO calibration_params "
        "(station_id, horizon, threshold_bucket, param_type, params_blob, n_samples) "
        "VALUES (?, ?, 0.0, 'emos', ?, ?)",
        (station_id, horizon, blob, params.n_samples),
    )
    conn.commit()


def load_emos(
    conn: sqlite3.Connection,
    station_id: str,
    horizon: int,
) -> EMOSParams | None:
    """Load EMOS params from DB (JSON deserialization)."""
    row = conn.execute(
        "SELECT params_blob FROM calibration_params "
        "WHERE station_id = ? AND horizon = ? AND param_type = 'emos' "
        "ORDER BY trained_at DESC LIMIT 1",
        (station_id, horizon),
    ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["params_blob"])
        return EMOSParams(
            a=data["a"], b=data["b"], c=data["c"], d=data["d"],
            n_samples=data["n_samples"],
        )
    except (json.JSONDecodeError, KeyError):
        return None


def save_emos_at(
    conn: sqlite3.Connection,
    station_id: str,
    horizon: int,
    asof_date: str,
    params: EMOSParams,
    *,
    commit: bool = True,
) -> None:
    """Persist historical walk-forward EMOS params for (station, horizon, asof_date).

    Used by the LUT seeder to memoize the cost of per-day EMOS refits across
    ~2 years of history. `asof_date` is an ISO YYYY-MM-DD string representing
    the day these params were valid for (fit on the 30-day window ending at
    asof_date - 1).
    """
    blob = json.dumps({
        "a": params.a,
        "b": params.b,
        "c": params.c,
        "d": params.d,
        "n_samples": params.n_samples,
    }).encode("utf-8")
    conn.execute(
        "INSERT OR REPLACE INTO calibration_params_history "
        "(station_id, horizon, asof_date, params_blob, n_samples) "
        "VALUES (?, ?, ?, ?, ?)",
        (station_id, horizon, asof_date, blob, params.n_samples),
    )
    if commit:
        conn.commit()


def load_emos_at(
    conn: sqlite3.Connection,
    station_id: str,
    horizon: int,
    asof_date: str,
) -> EMOSParams | None:
    """Load memoized walk-forward EMOS params for (station, horizon, asof_date).

    Returns None if no row exists — callers are expected to fit-on-demand
    and persist via `save_emos_at` when that happens.
    """
    row = conn.execute(
        "SELECT params_blob FROM calibration_params_history "
        "WHERE station_id = ? AND horizon = ? AND asof_date = ?",
        (station_id, horizon, asof_date),
    ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["params_blob"])
        return EMOSParams(
            a=data["a"], b=data["b"], c=data["c"], d=data["d"],
            n_samples=data["n_samples"],
        )
    except (json.JSONDecodeError, KeyError):
        return None
