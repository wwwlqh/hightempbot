"""Walk-forward cumulative LUT lookup: strict less-than asof semantics.

Mirrors the leakage-safe semantics of `backtest/lib/sweep_lib.py::lut_lookup_for_rows`
which uses `merge_asof(direction='backward', allow_exact_matches=False)` —
same-day rows are NEVER included.
"""

from __future__ import annotations

import sqlite3

import pytest

from hightempbot.calibration.lut import (
    CumulativeStats,
    lookup_with_cumulative,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE pred_bucket_history (
            station_id TEXT NOT NULL,
            local_date TEXT NOT NULL,
            pred_bucket_low REAL NOT NULL,
            emos_p REAL,
            hit INTEGER NOT NULL
        )"""
    )
    return c


def _insert(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        "INSERT INTO pred_bucket_history "
        "(station_id, local_date, pred_bucket_low, emos_p, hit) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()


def test_strict_less_than_excludes_asof_date(conn: sqlite3.Connection) -> None:
    _insert(conn, [
        ("KJFK", "2026-04-01", 0.40, 0.45, 1),
        ("KJFK", "2026-04-02", 0.40, 0.50, 0),
        ("KJFK", "2026-04-03", 0.40, 0.55, 1),
        ("KJFK", "2026-04-04", 0.40, 0.50, 1),  # same as asof — must be excluded
    ])
    out = lookup_with_cumulative(conn, "KJFK", (0.40, 0.60), "2026-04-04")
    assert out.n_cum == 3
    assert out.hits_cum == 2
    # mean_pred is over the 3 included rows: (0.45 + 0.50 + 0.55) / 3 = 0.50
    assert out.mean_pred == pytest.approx(0.50)


def test_includes_all_history_at_later_asof(conn: sqlite3.Connection) -> None:
    _insert(conn, [
        ("KJFK", "2026-04-01", 0.40, 0.45, 1),
        ("KJFK", "2026-04-02", 0.40, 0.50, 0),
        ("KJFK", "2026-04-03", 0.40, 0.55, 1),
    ])
    out = lookup_with_cumulative(conn, "KJFK", (0.40, 0.60), "2026-04-04")
    assert out.n_cum == 3
    assert out.hits_cum == 2


def test_cold_start_returns_zeros(conn: sqlite3.Connection) -> None:
    out = lookup_with_cumulative(conn, "KJFK", (0.00, 0.02), "2026-04-04")
    assert out.n_cum == 0
    assert out.hits_cum == 0
    assert out.mean_pred is None
    assert isinstance(out, CumulativeStats)


def test_filters_by_station(conn: sqlite3.Connection) -> None:
    _insert(conn, [
        ("KJFK", "2026-04-01", 0.40, 0.45, 1),
        ("KORD", "2026-04-01", 0.40, 0.45, 1),  # other station
        ("KJFK", "2026-04-02", 0.40, 0.50, 0),
    ])
    jfk = lookup_with_cumulative(conn, "KJFK", (0.40, 0.60), "2026-04-04")
    ord_ = lookup_with_cumulative(conn, "KORD", (0.40, 0.60), "2026-04-04")
    assert jfk.n_cum == 2
    assert ord_.n_cum == 1


def test_filters_by_bucket(conn: sqlite3.Connection) -> None:
    _insert(conn, [
        ("KJFK", "2026-04-01", 0.40, 0.45, 1),
        ("KJFK", "2026-04-01", 0.60, 0.70, 0),  # different bucket
        ("KJFK", "2026-04-02", 0.40, 0.50, 0),
    ])
    bucket_40 = lookup_with_cumulative(conn, "KJFK", (0.40, 0.60), "2026-04-04")
    bucket_60 = lookup_with_cumulative(conn, "KJFK", (0.60, 1.00), "2026-04-04")
    assert bucket_40.n_cum == 2
    assert bucket_60.n_cum == 1


def test_returns_zero_when_no_rows_match(conn: sqlite3.Connection) -> None:
    _insert(conn, [
        ("KJFK", "2026-04-10", 0.40, 0.45, 1),
    ])
    out = lookup_with_cumulative(conn, "KJFK", (0.40, 0.60), "2026-04-01")
    assert out.n_cum == 0
    assert out.hits_cum == 0
