"""Tests for Unit 11: Crash Recovery & Reconciliation."""

from __future__ import annotations

import sqlite3
import sys
import threading
import types
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hightempbot.db.connection import get_connection, init_db
from hightempbot.persistence.reconciliation import (
    _aggregate_trades,
    reconcile_orders,
)


@pytest.fixture(autouse=True)
def _fake_clob_types(monkeypatch):
    if "py_clob_client_v2.clob_types" in sys.modules:
        return

    root = sys.modules.get("py_clob_client_v2") or types.ModuleType("py_clob_client_v2")
    clob_types = types.ModuleType("py_clob_client_v2.clob_types")

    @dataclass(frozen=True)
    class OrderPayload:
        orderID: str

    clob_types.OrderPayload = OrderPayload
    root.clob_types = clob_types
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", root)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", clob_types)


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    conn = get_connection(str(db_path))
    yield conn
    conn.close()


def _mock_client() -> MagicMock:
    """Mock OrderClient with a real ``_with_timeout`` pass-through.

    ``walker._bounded_client_call`` always routes through ``_with_timeout``
    when present; a bare MagicMock attribute would swallow the wrapped call
    instead of invoking it.
    """
    client = MagicMock()
    client._with_timeout = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    return client


def _insert_pending_bet(db, order_id):
    db.execute(
        """INSERT INTO ledger
        (bet_ts, station_id, market_id, token_id, target_date,
         horizon, threshold, side, p_model, p_market, edge,
         kelly_size, volume_cap, bet_size, limit_price,
         order_id, outcome, event_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("2026-04-06T08:00:00Z", "KDAL", "m", "t", "2026-04-07",
         1, 68.0, "YES", 0.45, 0.30, 0.15,
         10.0, 5000.0, 10.0, 0.30,
         order_id, "PENDING", "bet"),
    )
    db.commit()


def _mark_pending_bet_filled(db, order_id, *, fill_price=0.50, fill_size=2.0):
    db.execute(
        """UPDATE ledger
        SET fill_price = ?, fill_size = ?, fill_ts = ?
        WHERE order_id = ?""",
        (fill_price, fill_size, "2026-04-06T08:01:00Z", order_id),
    )
    db.commit()


class TestReconciliation:
    def test_matched_orders_no_action(self, db):
        _insert_pending_bet(db, "ord_1")
        _insert_pending_bet(db, "ord_2")

        client = _mock_client()
        client._client.get_open_orders.return_value = [
            {"id": "ord_1"}, {"id": "ord_2"},
        ]

        result = reconcile_orders(client, db)
        assert result.matched == 2
        assert result.cancelled == 0
        assert result.updated == 0
        assert result.failed is False

    def test_orphaned_order_cancelled(self, db):
        # ord_orphan in CLOB but not in ledger
        client = _mock_client()
        client._client.get_open_orders.return_value = [{"id": "ord_orphan"}]

        result = reconcile_orders(client, db)
        assert result.cancelled == 1
        from py_clob_client_v2.clob_types import OrderPayload
        client._client.cancel_order.assert_called_once_with(OrderPayload(orderID="ord_orphan"))

    def test_missing_order_updated_from_trades(self, db):
        _insert_pending_bet(db, "ord_filled")

        client = _mock_client()
        client._client.get_open_orders.return_value = []  # order no longer in CLOB (filled)
        client._client.get_trades.return_value = [
            {"price": "0.30", "size": "100"},
        ]

        result = reconcile_orders(client, db)
        assert result.updated == 1

        row = db.execute(
            "SELECT fill_price, fill_size FROM ledger WHERE order_id = 'ord_filled'"
        ).fetchone()
        assert row["fill_price"] == 0.30
        assert row["fill_size"] == 100.0

    def test_missing_order_aggregates_multiple_trades(self, db):
        _insert_pending_bet(db, "ord_split")

        client = _mock_client()
        client._client.get_open_orders.return_value = []
        client._client.get_trades.return_value = [
            {"price": "0.30", "size": "40"},
            {"price": "0.35", "size": "60"},
        ]

        result = reconcile_orders(client, db)
        assert result.updated == 1

        row = db.execute(
            "SELECT fill_price, fill_size FROM ledger WHERE order_id = 'ord_split'"
        ).fetchone()
        assert row["fill_price"] == pytest.approx(0.33)
        assert row["fill_size"] == 100.0

    def test_missing_order_unknown_trades_stays_pending_and_fails(self, db):
        _insert_pending_bet(db, "ord_unknown")

        client = _mock_client()
        client._client.get_open_orders.return_value = []
        client._client.get_trades.return_value = None

        result = reconcile_orders(client, db)
        assert result.failed is True
        assert result.cancelled == 0

        row = db.execute(
            "SELECT outcome, pnl FROM ledger WHERE order_id = 'ord_unknown'"
        ).fetchone()
        assert row["outcome"] == "PENDING"
        assert row["pnl"] is None

    def test_missing_order_unknown_trades_with_recorded_fill_does_not_fail(self, db):
        _insert_pending_bet(db, "ord_recorded_fill_unknown")
        _mark_pending_bet_filled(db, "ord_recorded_fill_unknown")

        client = _mock_client()
        client._client.get_open_orders.return_value = []
        client._client.get_trades.return_value = None

        result = reconcile_orders(client, db)
        assert result.failed is False
        assert result.cancelled == 0

        row = db.execute(
            "SELECT outcome, fill_price, fill_size FROM ledger WHERE order_id = ?",
            ("ord_recorded_fill_unknown",),
        ).fetchone()
        assert row["outcome"] == "PENDING"
        assert row["fill_price"] == 0.50
        assert row["fill_size"] == 2.0

    def test_missing_order_confirmed_empty_trades_cancelled(self, db):
        _insert_pending_bet(db, "ord_empty")

        client = _mock_client()
        client._client.get_open_orders.return_value = []
        client._client.get_trades.return_value = []

        result = reconcile_orders(client, db)
        assert result.failed is False
        assert result.cancelled == 1

        row = db.execute(
            "SELECT outcome, pnl FROM ledger WHERE order_id = 'ord_empty'"
        ).fetchone()
        assert row["outcome"] == "CANCELLED"
        assert row["pnl"] == 0.0

    def test_missing_order_empty_trades_with_recorded_fill_not_cancelled(self, db):
        _insert_pending_bet(db, "ord_recorded_fill_empty")
        _mark_pending_bet_filled(db, "ord_recorded_fill_empty")

        client = _mock_client()
        client._client.get_open_orders.return_value = []
        client._client.get_trades.return_value = []

        result = reconcile_orders(client, db)
        assert result.failed is False
        assert result.cancelled == 0

        row = db.execute(
            "SELECT outcome, fill_price, fill_size FROM ledger WHERE order_id = ?",
            ("ord_recorded_fill_empty",),
        ).fetchone()
        assert row["outcome"] == "PENDING"
        assert row["fill_price"] == 0.50
        assert row["fill_size"] == 2.0

    def test_clob_down_returns_failed(self, db):
        client = _mock_client()
        client._client.get_open_orders.side_effect = ConnectionError("CLOB down")

        result = reconcile_orders(client, db)
        assert result.failed is True
        assert "CLOB down" in result.error

    def test_empty_clob_and_ledger(self, db):
        client = _mock_client()
        client._client.get_open_orders.return_value = []

        result = reconcile_orders(client, db)
        assert result.matched == 0
        assert result.cancelled == 0
        assert result.updated == 0
        assert result.failed is False

    def test_matched_orphan_writes_recovery_row(self, db):
        """A MATCHED CLOB order with no ledger linkage must NOT be cancelled
        (it's a real fill); it must be persisted as a PENDING recovery row
        carrying every NOT NULL ledger column and the recovered_orphan flag."""
        import json

        client = _mock_client()
        client._client.get_open_orders.return_value = [{"id": "ord_matched_orphan"}]
        # Status pre-check returns MATCHED so reconcile takes the recovery path.
        client._client.get_order.return_value = {"status": "MATCHED"}
        # Trades aggregation: 100 shares @ 0.40 → fill_price=0.40, notional=40.
        # `outcome` is required for side derivation (per the side-derivation
        # contract); a MATCHED orphan without a resolvable side is rejected.
        client._client.get_trades.return_value = [
            {"price": "0.40", "size": "100", "outcome": "YES"},
        ]

        result = reconcile_orders(client, db)

        # No cancel call — cancelling a real fill destroys committed capital.
        client._client.cancel_order.assert_not_called()
        assert result.failed is False
        assert result.updated == 1

        row = db.execute(
            "SELECT * FROM ledger WHERE order_id = 'ord_matched_orphan'"
        ).fetchone()
        assert row is not None
        # Schema-required columns are populated with non-null sentinels.
        assert row["bet_ts"] is not None
        assert row["station_id"] == "RECOVERED"
        assert row["market_id"] == "RECOVERED"
        assert row["token_id"] == "RECOVERED"
        assert row["target_date"] == "RECOVERED"
        assert row["horizon"] == 1
        # Side is now derived from the trades payload (majority-size YES/NO
        # vote), not the legacy "UNKNOWN" sentinel.
        assert row["side"] == "YES"
        assert row["limit_price"] == pytest.approx(0.40)
        # Real-fill fields carry the aggregated trade values.
        assert row["fill_price"] == pytest.approx(0.40)
        assert row["fill_size"] == pytest.approx(100.0)
        assert row["bet_size"] == pytest.approx(40.0)
        assert row["kelly_size"] == pytest.approx(40.0)
        assert row["outcome"] == "PENDING"
        assert row["event_type"] == "bet"
        # bracket_label lives in event_detail JSON (not a ledger column).
        detail = json.loads(row["event_detail"])
        assert detail["recovered_orphan"] is True
        assert detail["bracket_label"] == "RECOVERED"

    def test_terminal_fak_orphan_with_trades_writes_recovery_row(self, db):
        """A FAK order can be terminal after filling only part of its size."""
        client = _mock_client()
        client._client.get_open_orders.return_value = [{"id": "ord_fak_partial"}]
        client._client.get_order.return_value = {"status": "CANCELED"}
        client._client.get_trades.return_value = [
            {"price": "0.42", "size": "7", "outcome": "NO"},
        ]

        result = reconcile_orders(client, db)

        client._client.cancel_order.assert_not_called()
        assert result.failed is False
        assert result.updated == 1
        row = db.execute(
            "SELECT side, fill_price, fill_size, bet_size FROM ledger WHERE order_id = 'ord_fak_partial'"
        ).fetchone()
        assert row is not None
        assert row["side"] == "NO"
        assert row["fill_price"] == pytest.approx(0.42)
        assert row["fill_size"] == pytest.approx(7.0)
        assert row["bet_size"] == pytest.approx(0.42 * 7)

    def test_matched_orphan_with_no_trades_fails_without_writing_row(self, db):
        """When trades come back empty/unknown for a MATCHED orphan we cannot
        rebuild the fill — leave the order without ledger linkage and surface
        result.failed so startup forces dry-run rather than running blind."""
        client = _mock_client()
        client._client.get_open_orders.return_value = [{"id": "ord_matched_no_trades"}]
        client._client.get_order.return_value = {"status": "MATCHED"}
        client._client.get_trades.return_value = None  # unknown trades

        result = reconcile_orders(client, db)

        assert result.failed is True
        assert "ord_matched_no_trades" in (result.error or "")
        client._client.cancel_order.assert_not_called()
        # No recovery row written because we couldn't aggregate trades.
        assert db.execute(
            "SELECT COUNT(*) AS n FROM ledger WHERE order_id = 'ord_matched_no_trades'"
        ).fetchone()["n"] == 0

    def test_matched_orphan_recovery_insert_failure_surfaces_failed_no_row(self, db):
        """When the recovery INSERT itself raises sqlite3.Error the function
        must NOT swallow the failure silently: return False so reconcile_orders
        flags `result.failed` and startup downgrades to dry-run. We selectively
        raise on the INSERT statement so the SELECTs that read the ledger
        beforehand still succeed (the read path is uninvolved in the bug).

        Wraps the connection in a proxy because sqlite3.Connection.execute is
        a read-only attribute on Python 3.13+ (patch.object can't override it).
        """
        client = _mock_client()
        client._client.get_open_orders.return_value = [{"id": "ord_insert_fails"}]
        client._client.get_order.return_value = {"status": "MATCHED"}
        # Trade carries `outcome` so _aggregate_trades returns a resolvable side
        # (otherwise we'd short-circuit before reaching the INSERT branch).
        client._client.get_trades.return_value = [
            {"price": "0.40", "size": "100", "outcome": "YES"},
        ]

        class _FailingInsertConn:
            """Forward everything to ``inner`` except recovery INSERT calls,
            which raise sqlite3.OperationalError to drive the except branch."""

            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *args, **kwargs):
                if "INSERT OR IGNORE INTO ledger" in sql:
                    raise sqlite3.OperationalError("disk full")
                return self._inner.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def __enter__(self):
                # `with conn:` is used inside _recover_orphan_with_fill; the
                # context-manager protocol on sqlite3.Connection itself begins
                # a savepoint and commits/rolls back on exit. Mirror that by
                # delegating to the inner connection.
                return self._inner.__enter__()

            def __exit__(self, exc_type, exc, tb):
                return self._inner.__exit__(exc_type, exc, tb)

        wrapper = _FailingInsertConn(db)
        result = reconcile_orders(client, wrapper)

        assert result.failed is True
        assert "ord_insert_fails" in (result.error or "")
        # No row was committed because the INSERT raised.
        assert db.execute(
            "SELECT COUNT(*) AS n FROM ledger WHERE order_id = 'ord_insert_fails'"
        ).fetchone()["n"] == 0
        client._client.cancel_order.assert_not_called()


class TestAbortEvent:
    """Boot deadline propagation: abort_event must stop every mutation path."""

    def test_pre_set_abort_blocks_orphan_recovery(self, db):
        """abort_event set before reconcile_orders runs prevents orphan INSERT."""
        client = _mock_client()
        client._client.get_open_orders.return_value = [{"id": "ord_aborted"}]
        client._client.get_order.return_value = {"status": "MATCHED"}
        client._client.get_trades.return_value = [
            {"price": "0.40", "size": "100", "outcome": "YES"},
        ]

        abort = threading.Event()
        abort.set()

        result = reconcile_orders(client, db, abort_event=abort)

        # Top-of-loop abort guard should fire before the recovery path runs.
        assert result.aborted is True
        assert result.updated == 0
        # No recovery row written — the worker honored the supervisor signal.
        assert db.execute(
            "SELECT COUNT(*) AS n FROM ledger WHERE order_id = 'ord_aborted'"
        ).fetchone()["n"] == 0
        client._client.cancel_order.assert_not_called()

    def test_pre_set_abort_blocks_missing_fills_update(self, db):
        """abort_event set before reconcile_orders prevents missing-fill UPDATE."""
        _insert_pending_bet(db, "ord_filled_aborted")

        client = _mock_client()
        client._client.get_open_orders.return_value = []
        client._client.get_trades.return_value = [
            {"price": "0.30", "size": "100", "outcome": "YES"},
        ]

        abort = threading.Event()
        abort.set()

        result = reconcile_orders(client, db, abort_event=abort)

        assert result.aborted is True
        assert result.updated == 0
        # Row stays PENDING with no fill_price — the supervisor's signal
        # must override the missing-fill mutation path too.
        row = db.execute(
            "SELECT outcome, fill_price FROM ledger WHERE order_id = 'ord_filled_aborted'"
        ).fetchone()
        assert row["outcome"] == "PENDING"
        assert row["fill_price"] is None

    def test_no_abort_runs_normally(self, db):
        """Sanity: with no abort_event, the path is unchanged."""
        _insert_pending_bet(db, "ord_normal")

        client = _mock_client()
        client._client.get_open_orders.return_value = []
        client._client.get_trades.return_value = [
            {"price": "0.30", "size": "100", "outcome": "YES"},
        ]

        result = reconcile_orders(client, db)
        assert result.aborted is False
        assert result.updated == 1


class TestMissingFillOutcomeFilter:
    """Missing-fills UPDATE must not resurrect terminal rows."""

    def test_terminal_row_not_overwritten(self, db):
        """A row already settled to WIN must not be flipped back via missing-fills UPDATE."""
        _insert_pending_bet(db, "ord_already_won")
        # Settle the row before the reconciler sees it (simulating a race
        # where resolver ran while reconciler was mid-cycle).
        db.execute(
            "UPDATE ledger SET outcome = 'WIN', pnl = 5.0 WHERE order_id = 'ord_already_won'"
        )
        db.commit()

        client = _mock_client()
        client._client.get_open_orders.return_value = []  # CLOB no longer has the order
        client._client.get_trades.return_value = [
            {"price": "0.30", "size": "100", "outcome": "YES"},
        ]

        result = reconcile_orders(client, db)
        # Reconciler should skip the update (outcome filter rejects WIN);
        # row stays settled with its real PnL intact.
        row = db.execute(
            "SELECT outcome, pnl, fill_price FROM ledger WHERE order_id = 'ord_already_won'"
        ).fetchone()
        assert row["outcome"] == "WIN"
        assert row["pnl"] == 5.0
        # fill_price stays None — not overwritten by the reconciler tick.
        assert row["fill_price"] is None
        # And the result should not double-count the update.
        assert result.updated == 0


class TestAggregateTradesSideValidation:
    """_aggregate_trades must refuse to guess on mixed YES/NO trades."""

    def test_unanimous_yes(self):
        out = _aggregate_trades([
            {"price": "0.40", "size": "100", "outcome": "YES"},
            {"price": "0.42", "size": "50", "outcome": "YES"},
        ])
        assert out is not None
        _vwap, _size, side = out
        assert side == "YES"

    def test_mixed_yes_no_returns_none_side(self):
        out = _aggregate_trades([
            {"price": "0.40", "size": "100", "outcome": "YES"},
            {"price": "0.60", "size": "50", "outcome": "NO"},
        ])
        assert out is not None
        _vwap, _size, side = out
        # Mixed payload — refuse to guess. Caller logs and routes to manual.
        assert side is None

    def test_no_resolvable_side_returns_none(self):
        out = _aggregate_trades([
            {"price": "0.40", "size": "100", "outcome": "MAKER"},
        ])
        assert out is not None
        _vwap, _size, side = out
        assert side is None

    def test_malformed_trade_rows_are_skipped(self):
        out = _aggregate_trades([
            "not-a-dict",
            {"price": "oops", "size": "12", "outcome": "YES"},
            {"price": "0.41", "size": None, "outcome": "YES"},
            {"price": "0.40", "size": "10", "outcome": 123},
            {"price": "0.42", "size": "5", "outcome": "YES"},
        ])
        assert out is not None
        vwap, size, side = out
        assert vwap == pytest.approx(((0.40 * 10) + (0.42 * 5)) / 15)
        assert size == pytest.approx(15.0)
        assert side == "YES"

    def test_all_malformed_trade_rows_return_none(self):
        assert _aggregate_trades([
            "not-a-dict",
            {"price": "oops", "size": "12", "outcome": "YES"},
            {"price": "0.40", "size": "nan", "outcome": "YES"},
        ]) is None
