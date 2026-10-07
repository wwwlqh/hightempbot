"""Per-station hit rate of each EMOS probability bucket (the LUT).

Each resolved day adds one (bucket, hit) triple per bracket to
``pred_bucket_history``, bucketed with that day's walk-forward EMOS params
(memoized in ``calibration_params_history``). EMOS refits use a rolling 30
days; the LUT is an expanding window from REF_START_DATE.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

import numpy as np

from hightempbot.calibration.emos import EMOSParams, emos_probability, fit_emos
from hightempbot.calibration.store import load_emos_at, save_emos_at
from hightempbot.db.connection import utc_now_sql
from hightempbot.persistence.actuals import actual_source_clause

logger = logging.getLogger(__name__)


def _supports_station_lut(conn: sqlite3.Connection, station_id: str) -> bool:
    """Return whether this station should maintain live LUT state."""
    from hightempbot.stations import supports_live_resolution_source

    try:
        row = conn.execute(
            "SELECT resolution_source FROM enrolled_stations WHERE icao = ? LIMIT 1",
            (station_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None

    if row is None:
        return True
    return supports_live_resolution_source(row["resolution_source"])


def clear_station_lut(conn: sqlite3.Connection, station_id: str) -> tuple[int, int]:
    """Delete persisted LUT state for a station."""
    hist_deleted = conn.execute(
        "DELETE FROM pred_bucket_history WHERE station_id = ?",
        (station_id,),
    ).rowcount
    lut_deleted = conn.execute(
        "DELETE FROM lut_bucket_stats WHERE station_id = ?",
        (station_id,),
    ).rowcount
    conn.commit()
    return hist_deleted, lut_deleted


# 8 buckets, right-exclusive except the last.
BUCKETS: tuple[tuple[float, float], ...] = (
    (0.00, 0.02),
    (0.02, 0.05),
    (0.05, 0.10),
    (0.10, 0.15),
    (0.15, 0.25),
    (0.25, 0.40),
    (0.40, 0.60),
    (0.60, 1.00),
)

WALK_FORWARD_WINDOW_DAYS = 30

# Seeding fits with ≥20 pairs; live betting still needs MIN_PAIRS (30).
MIN_PAIRS_FOR_FIT = 20


@dataclass(frozen=True)
class CumulativeStats:
    """Cumulative (n, hits, mean_pred) for a bucket from days strictly before asof."""

    station_id: str
    pred_bucket_low: float
    n_cum: int
    hits_cum: int
    mean_pred: float | None  # cumulative mean of `pred_p` for the bucket up to asof


def lookup_with_cumulative(
    conn: sqlite3.Connection,
    station_id: str,
    bucket: tuple[float, float],
    asof_local_date: str,
) -> CumulativeStats:
    """Bucket stats from rows with ``local_date < asof_local_date`` (no same-day
    leakage; matches the backtest's merge_asof). Cold start is (0, 0, None)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "       COALESCE(SUM(hit), 0) AS hits, "
        "       AVG(emos_p) AS mean_pred "
        "FROM pred_bucket_history "
        "WHERE station_id = ? "
        "  AND pred_bucket_low = ? "
        "  AND local_date < ?",
        (station_id, bucket[0], asof_local_date),
    ).fetchone()
    n_cum = int(row["n"] or 0) if row is not None else 0
    hits_cum = int(row["hits"] or 0) if row is not None else 0
    mean_pred = float(row["mean_pred"]) if (row is not None and row["mean_pred"] is not None) else None
    return CumulativeStats(
        station_id=station_id,
        pred_bucket_low=bucket[0],
        n_cum=n_cum,
        hits_cum=hits_cum,
        mean_pred=mean_pred,
    )


def bucket_of(p: float) -> tuple[float, float]:
    """Bucket for ``p`` in [0, 1]; p = 1.0 falls in the last bucket."""
    if not (0.0 <= p <= 1.0):
        raise ValueError(f"probability out of range: {p}")

    for i, (lo, hi) in enumerate(BUCKETS):
        if i == len(BUCKETS) - 1:
            if lo <= p <= hi:
                return (lo, hi)
        else:
            if lo <= p < hi:
                return (lo, hi)
    raise ValueError(f"probability {p} did not map to any bucket")


def _bracket_key(lo_c: float | None, hi_c: float | None) -> str:
    """Stable key for one active bracket in canonical Celsius bounds."""
    lo = "-inf" if lo_c is None else f"{float(lo_c):.6f}"
    hi = "inf" if hi_c is None else f"{float(hi_c):.6f}"
    return f"{lo}:{hi}"


def rebuild_lut(
    conn: sqlite3.Connection,
    station_id: str,
    *,
    commit: bool = True,
) -> int:
    """Rebuild a station's ``lut_bucket_stats`` from its history. Returns rows written."""
    if not _supports_station_lut(conn, station_id):
        clear_station_lut(conn, station_id)
        logger.info("rebuild_lut: skipped unsupported source for %s", station_id)
        return 0

    agg_rows = conn.execute(
        """
        SELECT pred_bucket_low,
               pred_bucket_high,
               COUNT(*)     AS n,
               SUM(hit)     AS hits,
               AVG(emos_p)  AS mean_pred
        FROM pred_bucket_history
        WHERE station_id = ?
        GROUP BY pred_bucket_low, pred_bucket_high
        """,
        (station_id,),
    ).fetchall()

    conn.execute(
        "DELETE FROM lut_bucket_stats WHERE station_id = ?",
        (station_id,),
    )

    now_sql = utc_now_sql()
    insert_payload = []
    for r in agg_rows:
        n = int(r["n"] or 0)
        hits = int(r["hits"] or 0)
        observed = hits / n if n > 0 else None
        mean_pred = float(r["mean_pred"]) if r["mean_pred"] is not None else None
        insert_payload.append(
            (
                station_id,
                r["pred_bucket_low"],
                r["pred_bucket_high"],
                n,
                hits,
                observed,
                mean_pred,
                now_sql,
            )
        )

    if insert_payload:
        conn.executemany(
            "INSERT INTO lut_bucket_stats "
            "(station_id, pred_bucket_low, pred_bucket_high, n, hits, "
            " observed, mean_pred, refreshed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            insert_payload,
        )

    if commit:
        conn.commit()
    return len(insert_payload)


def stamp_refreshed(conn: sqlite3.Connection, station_id: str) -> None:
    """Touch ``refreshed_at`` so an idle but healthy station doesn't look stale."""
    now_sql = utc_now_sql()
    conn.execute(
        "UPDATE lut_bucket_stats SET refreshed_at = ? WHERE station_id = ?",
        (now_sql, station_id),
    )
    conn.commit()


def _pairs_for_fit(
    conn: sqlite3.Connection,
    station_id: str,
    horizon: int,
    end_date: str,
    window_days: int,
) -> tuple[np.ndarray, np.ndarray] | None:
    """(ensemble_matrix, actuals) for ``[end_date - window_days, end_date]``;
    None with fewer than MIN_PAIRS_FOR_FIT pairs."""
    from hightempbot.execution.strategy_constants import EXPECTED_MODELS, REQUIRED_MEMBERS

    d_end = date.fromisoformat(end_date)
    d_start = d_end - timedelta(days=window_days)

    placeholders = ",".join("?" for _ in EXPECTED_MODELS)
    actuals_clause, actuals_params = actual_source_clause("a")
    # Newest ingestion first; keep only the first row per (date, centre).
    rows = conn.execute(
        f"SELECT f.target_date AS target_date, f.centre AS centre, "
        f"f.tmax_celsius AS pred, a.tmax_celsius AS actual "
        f"FROM forecast_archive f "
        f"JOIN actuals a "
        f"  ON a.station_id = f.station_id AND a.local_date = f.target_date "
        f"WHERE f.station_id = ? AND f.horizon = ? "
        f"  AND f.target_date >= ? AND f.target_date <= ? "
        f"  AND f.source = 'openmeteo' "
        f"  AND f.centre IN ({placeholders}) "
        f"  AND {actuals_clause} "
        f"ORDER BY f.target_date, f.centre, f.ingested_at DESC",
        (
            station_id, horizon, d_start.isoformat(), d_end.isoformat(),
            *EXPECTED_MODELS, *actuals_params,
        ),
    ).fetchall()

    if not rows:
        return None

    by_date: dict[str, dict[str, float]] = {}
    actuals_by_date: dict[str, float] = {}
    for row in rows:
        tdate = row["target_date"]
        members = by_date.setdefault(tdate, {})
        if row["centre"] in members:
            continue
        members[row["centre"]] = float(row["pred"])
        actuals_by_date[tdate] = float(row["actual"])

    ensembles: list[list[float]] = []
    actuals: list[float] = []
    expected_centres = tuple(EXPECTED_MODELS)
    expected_set = set(expected_centres)
    for tdate, members in by_date.items():
        if len(members) != REQUIRED_MEMBERS:
            continue
        if set(members) != expected_set:
            continue
        ensembles.append([members[c] for c in expected_centres])
        actuals.append(actuals_by_date[tdate])

    if len(actuals) < MIN_PAIRS_FOR_FIT:
        return None

    return np.asarray(ensembles, dtype=float), np.asarray(actuals, dtype=float)


def _walk_forward_params(
    conn: sqlite3.Connection,
    station_id: str,
    horizon: int,
    asof_date: str,
    *,
    commit: bool = True,
) -> EMOSParams | None:
    """EMOS params as of ``asof_date`` (fit on the 30 days before it), memoized."""
    cached = load_emos_at(conn, station_id, horizon, asof_date)
    if cached is not None:
        return cached

    # Fit on the window ending the day BEFORE asof_date (no look-ahead).
    d = date.fromisoformat(asof_date)
    prior_end = (d - timedelta(days=1)).isoformat()
    pairs = _pairs_for_fit(
        conn, station_id, horizon, prior_end, WALK_FORWARD_WINDOW_DAYS
    )
    if pairs is None:
        return None

    ensembles, actuals = pairs
    params = fit_emos(ensembles, actuals)
    if params is None:
        return None

    save_emos_at(conn, station_id, horizon, asof_date, params, commit=commit)
    return params


def _brackets_for_station(
    conn: sqlite3.Connection,
    station_id: str,
    market_date: str | None = None,
) -> list[tuple[float | None, float | None]]:
    """The station's market bracket bounds, in °C."""
    from hightempbot.stations import fahrenheit_to_celsius

    if market_date is None:
        latest = conn.execute(
            "SELECT MAX(market_date) AS d FROM market_tokens "
            "WHERE station_id = ? AND bracket_idx != -1 "
            "  AND (bracket_low IS NOT NULL OR bracket_high IS NOT NULL)",
            (station_id,),
        ).fetchone()
        market_date = latest["d"] if latest and latest["d"] else None
    if market_date is None:
        return []

    rows = conn.execute(
        "SELECT bracket_low, bracket_high, bracket_label FROM market_tokens "
        "WHERE station_id = ? AND market_date = ? AND bracket_idx != -1 "
        "  AND (bracket_low IS NOT NULL OR bracket_high IS NOT NULL) "
        "ORDER BY COALESCE(bracket_low, -999999.0), COALESCE(bracket_high, 999999.0)",
        (station_id, market_date),
    ).fetchall()
    brackets: list[tuple[float | None, float | None]] = []
    seen: set[tuple[float | None, float | None]] = set()
    for row in rows:
        lo = float(row["bracket_low"]) if row["bracket_low"] is not None else None
        hi = float(row["bracket_high"]) if row["bracket_high"] is not None else None
        label = (
            str(row["bracket_label"] or "")
            .replace("Â", "")
            .replace("Ã‚", "")
            .strip()
            .upper()
        )
        if "°F" in label or label.endswith("F"):
            if lo is not None:
                lo = fahrenheit_to_celsius(lo)
            if hi is not None:
                hi = fahrenheit_to_celsius(hi)

        # Round after unit conversion so logically identical brackets de-dup.
        pair = (
            round(lo, 6) if lo is not None else None,
            round(hi, 6) if hi is not None else None,
        )
        if pair in seen:
            continue
        seen.add(pair)
        brackets.append(pair)
    return brackets


def append_triples_for_date(
    conn: sqlite3.Connection,
    station_id: str,
    horizon: int,
    local_date: str,
    brackets: Sequence[tuple[float | None, float | None]] | None = None,
    *,
    commit: bool = True,
) -> int:
    """Add one (bucket, hit) triple per bracket for a resolved day, using that
    day's walk-forward EMOS. Returns triples written (0 if data is missing)."""
    if not _supports_station_lut(conn, station_id):
        return 0

    if brackets is None:
        brackets = _brackets_for_station(conn, station_id)
    if not brackets:
        return 0

    actuals_clause, actuals_params = actual_source_clause()
    row = conn.execute(
        "SELECT tmax_celsius FROM actuals "
        f"WHERE station_id = ? AND local_date = ? AND {actuals_clause}",
        (station_id, local_date, *actuals_params),
    ).fetchone()
    if row is None:
        return 0
    actual_tmax = float(row["tmax_celsius"])

    from hightempbot.execution.strategy_constants import EXPECTED_MODELS, REQUIRED_MEMBERS

    placeholders = ",".join("?" for _ in EXPECTED_MODELS)
    # Dedupe re-backfilled rows by latest ingestion.
    member_rows = conn.execute(
        f"SELECT centre, tmax_celsius FROM forecast_archive "
        f"WHERE station_id = ? AND target_date = ? AND horizon = ? "
        f"AND source = 'openmeteo' "
        f"AND centre IN ({placeholders}) "
        f"ORDER BY centre, ingested_at DESC",
        (station_id, local_date, horizon, *EXPECTED_MODELS),
    ).fetchall()
    members: dict[str, float] = {}
    for row in member_rows:
        if row["centre"] in members:
            continue
        members[row["centre"]] = float(row["tmax_celsius"])
    if len(members) != REQUIRED_MEMBERS or set(members) != set(EXPECTED_MODELS):
        return 0
    ensemble = np.asarray([members[centre] for centre in EXPECTED_MODELS], dtype=float)

    params = _walk_forward_params(
        conn, station_id, horizon, local_date, commit=commit,
    )
    if params is None:
        return 0

    written = 0
    for lo_c, hi_c in brackets:
        if lo_c is None and hi_c is None:
            continue

        if lo_c is None and hi_c is not None:
            hit = 1 if actual_tmax < hi_c else 0
            emos_p = 1.0 - emos_probability(params, ensemble, hi_c)
        elif hi_c is None and lo_c is not None:
            hit = 1 if actual_tmax >= lo_c else 0
            emos_p = emos_probability(params, ensemble, lo_c)
        else:
            assert lo_c is not None and hi_c is not None
            hit = 1 if (lo_c <= actual_tmax < hi_c) else 0

            p_above_lo = emos_probability(params, ensemble, lo_c)
            p_above_hi = emos_probability(params, ensemble, hi_c)
            emos_p = max(0.0, p_above_lo - p_above_hi)

        emos_p = min(1.0, max(0.0, emos_p))
        lo_b, hi_b = bucket_of(emos_p)

        conn.execute(
            "INSERT OR REPLACE INTO pred_bucket_history "
            "(station_id, local_date, pred_bucket_low, pred_bucket_high, "
            " bracket_low, bracket_high, bracket_key, emos_p, hit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                station_id,
                local_date,
                lo_b,
                hi_b,
                lo_c,
                hi_c,
                _bracket_key(lo_c, hi_c),
                emos_p,
                hit,
            ),
        )
        written += 1

    if commit:
        conn.commit()
    return written


def seed_lut_from_history(
    conn: sqlite3.Connection,
    station_id: str,
    horizon: int = 1,
    start_date: str | None = None,
    brackets: Sequence[tuple[float | None, float | None]] | None = None,
) -> tuple[int, int]:
    """Rebuild a station's LUT history from all resolved days since ``start_date``.
    Returns ``(days_processed, triples_written)``."""
    if not _supports_station_lut(conn, station_id):
        clear_station_lut(conn, station_id)
        logger.info(
            "seed_lut_from_history: skipped unsupported source for %s",
            station_id,
        )
        return (0, 0)

    if start_date is None:
        actuals_clause, actuals_params = actual_source_clause()
        row = conn.execute(
            "SELECT MIN(local_date) AS d FROM actuals "
            f"WHERE station_id = ? AND {actuals_clause}",
            (station_id, *actuals_params),
        ).fetchone()
        start_date = row["d"] if row and row["d"] else None
    if start_date is None:
        logger.info("seed_lut_from_history: no actuals for %s", station_id)
        return (0, 0)

    actuals_clause, actuals_params = actual_source_clause()
    resolved_dates = [
        r["local_date"]
        for r in conn.execute(
            "SELECT local_date FROM actuals "
            f"WHERE station_id = ? AND local_date >= ? AND {actuals_clause} "
            "ORDER BY local_date",
            (station_id, start_date, *actuals_params),
        ).fetchall()
    ]
    if not resolved_dates:
        return (0, 0)

    if brackets is None:
        brackets = _brackets_for_station(conn, station_id)
    if not brackets:
        logger.info(
            "seed_lut_from_history: no market_tokens brackets for %s — "
            "seed skipped; will populate on first market scan",
            station_id,
        )
        return (0, 0)

    # Clear first so triples from an old bracket grid don't linger.
    clear_station_lut(conn, station_id)

    days = 0
    triples = 0
    conn.execute("SAVEPOINT seed_lut_from_history")
    try:
        for d in resolved_dates:
            # One commit at the end.
            written = append_triples_for_date(
                conn, station_id, horizon, d, brackets=brackets, commit=False,
            )
            if written:
                days += 1
                triples += written

        rebuild_lut(conn, station_id, commit=False)
        conn.execute("RELEASE SAVEPOINT seed_lut_from_history")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT seed_lut_from_history")
        conn.execute("RELEASE SAVEPOINT seed_lut_from_history")
        raise

    logger.info(
        "seed_lut_from_history(%s): %d days, %d triples",
        station_id,
        days,
        triples,
    )
    return (days, triples)


__all__ = (
    "BUCKETS",
    "CumulativeStats",
    "bucket_of",
    "lookup_with_cumulative",
    "rebuild_lut",
    "stamp_refreshed",
    "append_triples_for_date",
    "seed_lut_from_history",
)
