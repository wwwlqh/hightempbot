"""Shared pipeline_health query helpers."""

from __future__ import annotations

import sqlite3


def forecast_activity_counts(
    conn: sqlite3.Connection,
    *,
    lookback_hours: int = 2,
) -> dict[str, int]:
    """Return recent forecast-stage and upstream activity counts."""
    lookback = max(1, int(lookback_hours))
    params = (f"-{lookback} hours",)
    forecast_ok = int(conn.execute(
        "SELECT COUNT(*) FROM pipeline_health "
        "WHERE stage = 'forecast' AND status = 'OK' "
        "AND created_at >= datetime('now', ?)",
        params,
    ).fetchone()[0] or 0)
    forecast_attempts = int(conn.execute(
        "SELECT COUNT(*) FROM pipeline_health "
        "WHERE stage = 'forecast' "
        "AND created_at >= datetime('now', ?)",
        params,
    ).fetchone()[0] or 0)
    upstream_ok = int(conn.execute(
        "SELECT COUNT(*) FROM pipeline_health "
        "WHERE stage IN ('market', 'clob') AND status = 'OK' "
        "AND created_at >= datetime('now', ?)",
        params,
    ).fetchone()[0] or 0)
    return {
        "forecast_ok": forecast_ok,
        "forecast_attempts": forecast_attempts,
        "forecast_upstream_ok": upstream_ok,
    }


def forecast_stall_detected(counts: dict[str, int]) -> bool:
    """True when forecast should have succeeded but did not."""
    return (
        int(counts.get("forecast_ok", 0)) == 0
        and (
            int(counts.get("forecast_attempts", 0)) > 0
            or int(counts.get("forecast_upstream_ok", 0)) > 0
        )
    )
