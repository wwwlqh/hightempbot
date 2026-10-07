from __future__ import annotations

import weakref
import sqlite3
from types import SimpleNamespace

import pytest

from hightempbot.db.connection import init_db
from hightempbot.execution.operator_control import (
    LIVE,
    STOPPED_PROCESSING,
    TRANSFER_LOCK,
    OperatorControlError,
    clear_transfer_lock,
    enter_transfer_lock,
    get_operator_state,
    processing_block_reason,
    start_processing,
    stop_processing,
)


def test_init_db_connection_supports_operator_schema_cache(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        from hightempbot.execution import operator_control as oc

        oc.ensure_operator_schema(conn)

        assert weakref.ref(conn)() is conn
        assert conn in oc._verified_conns
    finally:
        conn.close()


def test_init_db_migrates_legacy_operator_state_before_schema_seed(tmp_path):
    db_path = tmp_path / "legacy.db"
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            """
            CREATE TABLE operator_control_state (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                state           TEXT NOT NULL DEFAULT 'STOPPED_PROCESSING',
                boot_dry_run    INTEGER NOT NULL DEFAULT 1,
                reason          TEXT,
                updated_by      TEXT,
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        raw.execute(
            """
            INSERT INTO operator_control_state
            (id, state, boot_dry_run, reason, updated_by)
            VALUES (1, 'LIVE', 0, 'legacy row', 'test')
            """
        )
        raw.commit()
    finally:
        raw.close()

    conn = init_db(db_path)
    try:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(operator_control_state)").fetchall()
        }
        row = conn.execute(
            "SELECT state, boot_dry_run, version FROM operator_control_state WHERE id=1"
        ).fetchone()

        assert "version" in cols
        assert row["state"] == LIVE
        assert row["boot_dry_run"] == 0
        assert row["version"] == 0
    finally:
        conn.close()


def test_main_syncs_effective_boot_mode_before_dashboard_reads(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        from hightempbot.main import _sync_operator_boot_mode

        assert get_operator_state(conn).boot_dry_run is True

        _sync_operator_boot_mode(conn, dry_run=False)

        state = get_operator_state(conn)
        assert state.boot_dry_run is False
        assert state.state == STOPPED_PROCESSING
    finally:
        conn.close()


def test_live_public_dashboard_requires_tls():
    from hightempbot.main import _validate_dashboard_live_safety

    cfg = SimpleNamespace(
        dashboard_pass="secret",
        dashboard_tls_terminated=False,
    )

    with pytest.raises(RuntimeError, match="DASHBOARD_TLS_TERMINATED"):
        _validate_dashboard_live_safety(cfg, dry_run=False, dash_host="0.0.0.0")


def test_live_public_dashboard_allows_declared_tls():
    from hightempbot.main import _validate_dashboard_live_safety

    cfg = SimpleNamespace(
        dashboard_pass="secret",
        dashboard_tls_terminated=True,
    )

    _validate_dashboard_live_safety(cfg, dry_run=False, dash_host="0.0.0.0")


def test_public_payload_read_does_not_rewrite_boot_mode(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        from hightempbot.execution.operator_control import public_operator_payload
        from hightempbot.main import _sync_operator_boot_mode

        _sync_operator_boot_mode(conn, dry_run=False)

        payload = public_operator_payload(conn, dry_run=True)

        assert payload["bootDryRun"] is False
        assert get_operator_state(conn).boot_dry_run is False
    finally:
        conn.close()


def test_stop_processing_blocks_pipeline_work(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        state = stop_processing(conn, actor="test", boot_dry_run=False)

        assert state.state == STOPPED_PROCESSING
        assert processing_block_reason(conn, dry_run=False) == "operator Stop Processing is active"
    finally:
        conn.close()


def test_start_processing_cannot_override_dry_run_boot(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        stop_processing(conn, boot_dry_run=True)

        with pytest.raises(OperatorControlError, match="DRY_RUN"):
            start_processing(conn, boot_dry_run=True)
    finally:
        conn.close()


def test_start_processing_resumes_live_boot(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        stop_processing(conn, boot_dry_run=False)
        state = start_processing(conn, boot_dry_run=False)

        assert state.state == LIVE
        assert processing_block_reason(conn, dry_run=False) is None
    finally:
        conn.close()


def test_transfer_lock_blocks_processing(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        state = enter_transfer_lock(conn, boot_dry_run=False)

        assert state.state == TRANSFER_LOCK
        assert processing_block_reason(conn, dry_run=False) == "operator Transfer Lock is active"
        events = conn.execute("SELECT COUNT(*) AS n FROM operator_control_events").fetchone()
        assert events["n"] == 1
    finally:
        conn.close()


def test_clear_transfer_lock_resumes_processing(tmp_path):
    # Clear_transfer_lock was untested. Mirror the
    # enter_transfer_lock test by establishing the lock and then releasing it.
    conn = init_db(tmp_path / "test.db")
    try:
        enter_transfer_lock(conn, boot_dry_run=False)
        state = clear_transfer_lock(conn, boot_dry_run=False)

        assert state.state == LIVE
        assert processing_block_reason(conn, dry_run=False) is None
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM operator_control_events"
        ).fetchone()
        # One for enter_transfer_lock, one for clear (delegates to start_processing).
        assert events["n"] == 2
    finally:
        conn.close()


def test_clear_transfer_lock_cannot_override_dry_run_boot(tmp_path):
    # Companion test: clear_transfer_lock delegates to start_processing, which
    # refuses to resume live trading when the process booted DRY_RUN.
    conn = init_db(tmp_path / "test.db")
    try:
        enter_transfer_lock(conn, boot_dry_run=True)

        with pytest.raises(OperatorControlError, match="DRY_RUN"):
            clear_transfer_lock(conn, boot_dry_run=True)
    finally:
        conn.close()


def test_concurrent_update_raises_optimistic_conflict(tmp_path):
    """Monotonic version on operator_control_state."""
    from hightempbot.execution.operator_control import set_operator_state, LIVE

    conn = init_db(tmp_path / "test.db")
    try:
        # Start from a known live state.
        stop_processing(conn, boot_dry_run=False)
        start_processing(conn, boot_dry_run=False)

        # Read current state (and its version), then have another writer
        # bump version out-of-band before we issue our own UPDATE. We bypass
        # the helper by hitting the table directly to force a stale read.
        current = get_operator_state(conn)
        # Out-of-band concurrent writer: advance version by 1.
        conn.execute(
            "UPDATE operator_control_state SET version = version + 1 WHERE id = 1"
        )
        conn.commit()

        # Patch get_operator_state to return the stale version snapshot so
        # set_operator_state writes WHERE version=<stale> and fails.
        import hightempbot.execution.operator_control as oc

        original = oc.get_operator_state

        def fake_get_operator_state(_conn, *, boot_dry_run=None):  # noqa: ARG001
            return current  # the stale snapshot

        oc.get_operator_state = fake_get_operator_state
        try:
            with pytest.raises(OperatorControlError, match="concurrent"):
                set_operator_state(conn, LIVE, actor="test", boot_dry_run=False)
        finally:
            oc.get_operator_state = original
    finally:
        conn.close()
