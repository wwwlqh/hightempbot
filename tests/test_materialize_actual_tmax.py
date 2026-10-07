"""Tests for ``scheduler.jobs.materialize_actual_tmax_into_ledger``."""

from __future__ import annotations

import json

import pytest

from hightempbot.db.connection import get_connection, init_db
from hightempbot.scheduler.jobs import materialize_actual_tmax_into_ledger


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    return str(db_path)


def _insert_row(
    db_path: str, *, station_id: str, target_date: str, outcome: str,
    actual_tmax: float | None, event_type: str = "bet",
) -> int:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             fill_price, fill_size,
             outcome, event_type, event_detail, actual_tmax, pnl)
            VALUES (?, ?, 'm', 't', ?, 1, 21.0, 'NO', 0.45, 0.30, 0.15,
             5.0, 5000.0, 5.0, 0.3, 0.3, 16.67,
             ?, ?, ?, ?, ?)""",
            (
                f"{target_date}T08:00:00Z", station_id, target_date, outcome,
                event_type, json.dumps({}), actual_tmax,
                1.0 if outcome == "WIN" else -1.0,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


class TestMaterializeActualTmax:
    def test_fills_closed_row_with_null_actual(self, db):
        bet_id = _insert_row(
            db, station_id="KDAL", target_date="2026-05-15",
            outcome="CLOSED", actual_tmax=None,
        )
        conn = get_connection(db)
        try:
            n = materialize_actual_tmax_into_ledger(
                conn, "KDAL", "2026-05-15", tmax_celsius=23.5, source="wu",
            )
            assert n == 1
            row = conn.execute(
                "SELECT actual_tmax FROM ledger WHERE id=?", (bet_id,),
            ).fetchone()
            assert row["actual_tmax"] == pytest.approx(23.5)
        finally:
            conn.close()

    def test_fills_all_resolved_outcomes(self, db):
        ids = [
            _insert_row(db, station_id="KDAL", target_date="2026-05-15",
                        outcome=o, actual_tmax=None)
            for o in ("WIN", "LOSS", "PUSH", "CLOSED")
        ]
        conn = get_connection(db)
        try:
            n = materialize_actual_tmax_into_ledger(
                conn, "KDAL", "2026-05-15", tmax_celsius=23.5, source="wu",
            )
            assert n == 4
            for bet_id in ids:
                row = conn.execute(
                    "SELECT actual_tmax FROM ledger WHERE id=?", (bet_id,),
                ).fetchone()
                assert row["actual_tmax"] == pytest.approx(23.5)
        finally:
            conn.close()

    def test_does_not_clobber_existing_actual(self, db):
        """Idempotent: a row that already has actual_tmax stays untouched
        (matches the backfill CLI's safety contract)."""
        bet_id = _insert_row(
            db, station_id="KDAL", target_date="2026-05-15",
            outcome="WIN", actual_tmax=99.0,
        )
        conn = get_connection(db)
        try:
            n = materialize_actual_tmax_into_ledger(
                conn, "KDAL", "2026-05-15", tmax_celsius=23.5, source="wu",
            )
            assert n == 0
            row = conn.execute(
                "SELECT actual_tmax FROM ledger WHERE id=?", (bet_id,),
            ).fetchone()
            assert row["actual_tmax"] == 99.0
        finally:
            conn.close()

    def test_skips_pending_rows(self, db):
        bet_id = _insert_row(
            db, station_id="KDAL", target_date="2026-05-15",
            outcome="PENDING", actual_tmax=None,
        )
        conn = get_connection(db)
        try:
            n = materialize_actual_tmax_into_ledger(
                conn, "KDAL", "2026-05-15", tmax_celsius=23.5, source="wu",
            )
            assert n == 0
            row = conn.execute(
                "SELECT actual_tmax FROM ledger WHERE id=?", (bet_id,),
            ).fetchone()
            # PENDING rows aren't materialized via this path — resolution
            # writes their value at resolution time.
            assert row["actual_tmax"] is None
        finally:
            conn.close()

    def test_refuses_unsupported_source(self, db):
        """source='ncei' is outside SUPPORTED_LIVE_SOURCES."""
        bet_id = _insert_row(
            db, station_id="KDAL", target_date="2026-05-15",
            outcome="CLOSED", actual_tmax=None,
        )
        conn = get_connection(db)
        try:
            n = materialize_actual_tmax_into_ledger(
                conn, "KDAL", "2026-05-15", tmax_celsius=23.5, source="ncei",
            )
            assert n == 0
            row = conn.execute(
                "SELECT actual_tmax FROM ledger WHERE id=?", (bet_id,),
            ).fetchone()
            assert row["actual_tmax"] is None
        finally:
            conn.close()

    def test_does_not_touch_other_stations_or_dates(self, db):
        same_station = _insert_row(
            db, station_id="KDAL", target_date="2026-05-15",
            outcome="CLOSED", actual_tmax=None,
        )
        other_date = _insert_row(
            db, station_id="KDAL", target_date="2026-05-14",
            outcome="CLOSED", actual_tmax=None,
        )
        other_station = _insert_row(
            db, station_id="KSEA", target_date="2026-05-15",
            outcome="CLOSED", actual_tmax=None,
        )
        conn = get_connection(db)
        try:
            n = materialize_actual_tmax_into_ledger(
                conn, "KDAL", "2026-05-15", tmax_celsius=23.5, source="wu",
            )
            assert n == 1
            rows = {
                r["id"]: r["actual_tmax"] for r in conn.execute(
                    "SELECT id, actual_tmax FROM ledger ORDER BY id"
                ).fetchall()
            }
            assert rows[same_station] == pytest.approx(23.5)
            assert rows[other_date] is None
            assert rows[other_station] is None
        finally:
            conn.close()
