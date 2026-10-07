"""Regression guard against piecemeal-merge schema drift."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hightempbot.db.connection import init_db


@pytest.fixture
def fresh_db(tmp_path: Path) -> sqlite3.Connection:
    return init_db(tmp_path / "schema_integration.db")


def test_pred_bucket_history_writable(fresh_db: sqlite3.Connection) -> None:
    """calibration/lut.py:509-style INSERT must succeed on a fresh DB."""
    fresh_db.execute(
        """INSERT OR REPLACE INTO pred_bucket_history
           (station_id, local_date, pred_bucket_low, pred_bucket_high, emos_p, hit)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("KDAL", "2026-04-24", 0.4, 0.5, 0.45, 1),
    )
    fresh_db.commit()
    rows = fresh_db.execute(
        "SELECT station_id, hit FROM pred_bucket_history"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "KDAL"


def test_dashboard_stations_scan_does_not_raise(fresh_db: sqlite3.Connection) -> None:
    """dashboard/app.py:387-style aggregate must execute on an empty fresh DB."""
    fresh_db.execute(
        """SELECT station_id, COUNT(*)
           FROM pred_bucket_history GROUP BY station_id"""
    ).fetchall()
    fresh_db.execute(
        "SELECT COUNT(DISTINCT local_date) FROM pred_bucket_history"
    ).fetchone()


def test_lut_bucket_stats_and_history_exist(fresh_db: sqlite3.Connection) -> None:
    """Both LCB tables must land from schema.sql alone (no migration runner)."""
    tables = {
        r[0] for r in fresh_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required = {"pred_bucket_history", "lut_bucket_stats", "calibration_params_history"}
    missing = required - tables
    assert not missing, f"schema.sql is missing LCB tables: {sorted(missing)}"


def test_book_snapshots_table_and_columns_exist(fresh_db: sqlite3.Connection) -> None:
    """book_snapshots must land from schema.sql alone with the full column set."""
    tables = {
        r[0] for r in fresh_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "book_snapshots" in tables, "schema.sql is missing book_snapshots"
    cols = {
        r[1] for r in fresh_db.execute("PRAGMA table_info(book_snapshots)").fetchall()
    }
    required = {
        "id", "snapped_at", "station_id", "target_date", "bracket_idx",
        "bracket_label", "token_id", "no_token_id", "best_ask", "best_bid",
        "yes_top_ask_price", "yes_top_ask_size", "no_top_ask_price",
        "no_top_ask_size", "volume24hr",
    }
    missing = required - cols
    assert not missing, f"book_snapshots is missing columns: {sorted(missing)}"


def test_ledger_has_calibration_forensic_columns(fresh_db: sqlite3.Connection) -> None:
    """ledger must carry the forensic columns that record_bet writes to."""
    cols = {
        r[1] for r in fresh_db.execute("PRAGMA table_info(ledger)").fetchall()
    }
    required = {
        "realized_edge", "prob_safe_floor",
        "pred_bucket_low", "pred_bucket_high", "n_bucket",
    }
    missing = required - cols
    assert not missing, f"ledger is missing forensic columns: {sorted(missing)}"


def test_lut_bucket_stats_has_no_wilson_ci_columns(fresh_db: sqlite3.Connection) -> None:
    """The retired Wilson CI columns must be absent from fresh schemas."""
    cols = {
        r[1] for r in fresh_db.execute("PRAGMA table_info(lut_bucket_stats)").fetchall()
    }
    assert "lcb" not in cols
    assert "ucb" not in cols


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Calling init_db twice on the same path must not raise (production restart case)."""
    db_path = tmp_path / "idem.db"
    conn1 = init_db(db_path)
    conn1.close()
    conn2 = init_db(db_path)
    conn2.close()


def test_pred_bucket_history_legacy_bucket_unique_schema_is_rebuilt(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_lut.db"
    raw = sqlite3.connect(db_path)
    raw.execute(
        """CREATE TABLE pred_bucket_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL,
            local_date TEXT NOT NULL,
            pred_bucket_low REAL NOT NULL,
            pred_bucket_high REAL NOT NULL,
            emos_p REAL NOT NULL,
            hit INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(station_id, local_date, pred_bucket_low)
        )"""
    )
    raw.execute(
        "INSERT INTO pred_bucket_history "
        "(station_id, local_date, pred_bucket_low, pred_bucket_high, emos_p, hit) "
        "VALUES ('KDAL', '2026-04-24', 0.0, 0.02, 0.01, 0)"
    )
    raw.commit()
    raw.close()

    conn = init_db(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pred_bucket_history)").fetchall()}
    assert {"bracket_low", "bracket_high", "bracket_key"} <= cols
    assert conn.execute("SELECT COUNT(*) FROM pred_bucket_history").fetchone()[0] == 0
    backup_exists = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name='pred_bucket_history_legacy_bucket_unique'"
    ).fetchone()[0]
    assert backup_exists == 1
    conn.close()


def test_no_orphan_bss_or_monitor_modules() -> None:
    """Phase F teardown: the deleted modules must stay deleted."""
    import importlib.util
    assert importlib.util.find_spec("hightempbot.execution.bss") is None, \
        "execution/bss.py was removed in Phase F — do not re-add without replacing the LCB gate"
    assert importlib.util.find_spec("hightempbot.execution.monitor") is None, \
        "execution/monitor.py was removed in Phase F — its legacy BSS gate does not match the LCB gate"
