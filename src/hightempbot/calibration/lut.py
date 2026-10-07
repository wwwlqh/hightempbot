"""Per-station empirical hit-rate lookup table.

For each (station, predicted-probability-bucket) cell we cache the empirical
``hits / n`` rate over the historical window. The decision gate compares the
fee-adjusted edge against this point-estimate calibration.

Two windows operate at different cadences:
  * EMOS retrain — rolling 30 days of (pred, actual) pairs. Matches notebook.
  * LUT           — expanding window since `REF_START_DATE = 2024-03-01`.

Bucket grid is the notebook Cell F 8-bucket layout (locked).

Walk-forward semantics: each historical triple is labeled with the bucket
assigned by that day's EMOS params (not today's). Historical EMOS params are
memoized in `calibration_params_history` via `calibration.store.save_emos_at`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Sequence

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


# Notebook Cell F bucket grid (8 buckets, right-exclusive except final).
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

# Rolling window size for per-day walk-forward EMOS refit (matches notebook N=30).
WALK_FORWARD_WINDOW_DAYS = 30

# Minimum (pred, actual) pairs required for a walk-forward EMOS fit.
# Intentionally more permissive than execution/strategy_constants.py::MIN_PAIRS=30:
# the wiki's EMOS Calibration spec documents this asymmetry — LUT seed fits
# at >=20 pairs (best-effort cold-start) while live betting requires >=30
# pairs via CalibrationModel.is_ready().
MIN_PAIRS_FOR_FIT = 20


@dataclass(frozen=True)
class CumulativeStats:
    """Walk-forward cumulative (n, hits) for a (station, bucket) at an asof date.

    Built from `pred_bucket_history` with strict `local_date < asof_local_date`
    semantics — byte-identical to backtest/sweep_lib.py::load_walk_forward_lut +
    lut_lookup_for_rows (`merge_asof(direction='backward', allow_exact_matches=False)`).
    Used by the per-strategy router to compute Bayesian shrinkage signals
    (`p_Shrink_n10`, `p_Shrink_n50`) without leaking same-day resolutions.
    """

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
    """Compute walk-forward cumulative (n, hits, mean_pred) for a bucket.

    Strict `local_date < asof_local_date` — same-day resolutions are NEVER
    included, preserving the backtest's leakage semantics. Returns a
    CumulativeStats with `n_cum=0, hits_cum=0, mean_pred=None` when no prior
    history exists for the (station, bucket) pair (cold-start case — the
    caller's signal-flavor logic handles it via NaN propagation).

    Args:
        conn: DB connection (per-thread).
        station_id: ICAO of the station.
        bucket: (pred_bucket_low, pred_bucket_high) — only `low` is queried
            since pred_bucket_history stores the canonical `pred_bucket_low`.
        asof_local_date: ISO date string. The lookup excludes any row with
            `local_date >= asof_local_date`.

    Returns:
        Always a CumulativeStats; never None. Cold-start = (0, 0, None).
    """
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
    """Map a probability ``p ∈ [0, 1]`` to its notebook Cell F bucket.

    Right-exclusive on internal boundaries; right-inclusive on the final
    bucket so that ``p = 1.0`` lands in ``(0.60, 1.00]``.
    """
    if not (0.0 <= p <= 1.0):
        raise ValueError(f"probability out of range: {p}")

    for i, (lo, hi) in enumerate(BUCKETS):
        if i == len(BUCKETS) - 1:
            if lo <= p <= hi:
                return (lo, hi)
        else:
            if lo <= p < hi:
                return (lo, hi)
    # Unreachable due to range check above.
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
    """Recompute ``lut_bucket_stats`` rows for one station from its triples.

    Reads every row in ``pred_bucket_history`` for ``station_id``, groups by
    ``pred_bucket_low``, and writes one row per bucket back into
    ``lut_bucket_stats`` with a fresh ``refreshed_at`` timestamp. Returns the
    number of bucket rows written.

    Idempotent: buckets with no history disappear from lut_bucket_stats.
    """
    if not _supports_station_lut(conn, station_id):
        clear_station_lut(conn, station_id)
        logger.info("rebuild_lut: skipped unsupported source for %s", station_id)
        return 0

    # Aggregate in SQL: Wilson CI was retired so the only remaining work is
    # COUNT/SUM/AVG, which SQLite handles natively. Materialising the entire
    # history into Python and aggregating row-by-row was ~3x slower at 600+
    # rows per station per retrain.
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

    # Remove any lut rows for buckets that no longer have history, so a
    # cleared history really does clear the table.
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
    """Touch ``refreshed_at`` on every lut row for ``station_id``.

    Called by the nightly retrain job even when no new data has arrived so
    that the 36h stale halt doesn't fire on a station whose data is correct
    but idle (weekends, holidays, WU rate-limit delays).
    """
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
    """Load (ensemble_matrix, actuals) for the ``window_days`` before ``end_date``.

    Returns ``None`` if fewer than ``MIN_PAIRS_FOR_FIT`` aligned pairs exist.
    ``end_date`` is inclusive — the returned window is ``[end_date - window, end_date]``.
    """
    from hightempbot.execution.strategy_constants import EXPECTED_MODELS, REQUIRED_MEMBERS

    d_end = date.fromisoformat(end_date)
    d_start = d_end - timedelta(days=window_days)

    # One row per (station, target_date, horizon, centre, member) — aggregate
    # into a matrix with one column per centre.
    placeholders = ",".join("?" for _ in EXPECTED_MODELS)
    actuals_clause, actuals_params = actual_source_clause("a")
    # ORDER BY ingested_at DESC so the first row seen per (date, centre) is the
    # most recent ingestion. Same-centre rows from older backfills (different
    # member numbers, identical tmax) are skipped rather than poisoning the date.
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

    # Build per-date member list. Keep the first row per (date, centre) — the
    # latest ingestion thanks to ORDER BY ingested_at DESC.
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
    """Memoized walk-forward EMOS params valid AS OF ``asof_date``.

    First checks ``calibration_params_history`` (cached). On miss, fits EMOS
    on the 30-day window ending at ``asof_date - 1`` and stores the result.
    """
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
    """Return the 11 bracket bounds used by market_tokens for this station.

    Each entry is ``(bracket_low, bracket_high)`` in °C. The LUT is fed by
    walking these brackets against each day's EMOS to produce ``emos_p`` per
    bracket per day.
    """
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
    """Append ``(station, local_date, bucket, hit)`` triples for one resolved day.

    One triple per bracket that was active for the station — ``emos_p`` is the
    EMOS-predicted probability that actual tmax fell in the bracket under
    the walk-forward EMOS params valid AS OF ``local_date``. ``hit`` is 1
    iff the resolved ``actuals.tmax_celsius`` lies in the bracket.

    When ``commit=False`` the caller is responsible for committing — the
    seed loop uses this to batch hundreds of per-day inserts into one WAL
    sync instead of one commit per day.

    Returns the number of triples written (zero if prerequisites missing).
    """
    if not _supports_station_lut(conn, station_id):
        return 0

    if brackets is None:
        brackets = _brackets_for_station(conn, station_id)
    if not brackets:
        return 0

    # Get the resolved actual for this station-date (must exist).
    actuals_clause, actuals_params = actual_source_clause()
    row = conn.execute(
        "SELECT tmax_celsius FROM actuals "
        f"WHERE station_id = ? AND local_date = ? AND {actuals_clause}",
        (station_id, local_date, *actuals_params),
    ).fetchone()
    if row is None:
        return 0
    actual_tmax = float(row["tmax_celsius"])

    # Fetch ensemble members for this day at the requested horizon.
    from hightempbot.execution.strategy_constants import EXPECTED_MODELS, REQUIRED_MEMBERS

    placeholders = ",".join("?" for _ in EXPECTED_MODELS)
    # Dedupe by latest ingestion so re-backfilled rows (different member nums,
    # identical tmax) don't push the row count past REQUIRED_MEMBERS.
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
            # Bracket hit: threshold is continuous low-inclusive, high-exclusive.
            hit = 1 if (lo_c <= actual_tmax < hi_c) else 0

            # P(bracket) = P(tmax > lo_c) - P(tmax >= hi_c) = P(>lo_c) - P(>hi_c).
            p_above_lo = emos_probability(params, ensemble, lo_c)
            p_above_hi = emos_probability(params, ensemble, hi_c)
            emos_p = max(0.0, p_above_lo - p_above_hi)

        # Clamp to handle numerical precision at the boundaries.
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
    """Seed ``pred_bucket_history`` from all resolved history for ``station_id``.

    Walks every (station, local_date) in ``actuals`` from ``start_date`` (or
    the earliest available date) through today, refitting EMOS per day via
    ``_walk_forward_params`` and appending one triple per bracket per day.

    Finishes by calling ``rebuild_lut`` so the Wilson bounds in
    ``lut_bucket_stats`` reflect the seeded history.

    Returns ``(days_processed, triples_written)``.
    """
    if not _supports_station_lut(conn, station_id):
        clear_station_lut(conn, station_id)
        logger.info(
            "seed_lut_from_history: skipped unsupported source for %s",
            station_id,
        )
        return (0, 0)

    # Pull the dates we can resolve.
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

    # Full seed is authoritative for the selected market grid. Clear first so
    # old shifted grids do not keep contributing stale bracket-day triples.
    clear_station_lut(conn, station_id)

    days = 0
    triples = 0
    conn.execute("SAVEPOINT seed_lut_from_history")
    try:
        for d in resolved_dates:
            # Defer the per-day commit and issue one atomic release at the end.
            # With 600+ resolved dates per station this turns 600 WAL syncs
            # into one and avoids committing partial pred_bucket_history if a
            # later day fails before lut_bucket_stats is rebuilt.
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
