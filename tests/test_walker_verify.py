"""Retry loop + 2-step verification tests for execute_or_log."""
from __future__ import annotations

import sqlite3
import json
import sys
import types
from pathlib import Path
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from hightempbot.db.connection import init_db
from hightempbot.execution import strategy_constants as cfg
from hightempbot.persistence.ledger import record_bet
from hightempbot.execution.walker import (
    _bounded_client_call,
    _classify_clob_error,
    _get_trades_for_order,
    execute_or_log,
)
from hightempbot.execution.types import BetSignal, OrderResult


@pytest.fixture(autouse=True)
def _test_runtime(monkeypatch):
    """Collapse retry/verify waits so tests finish in <1s."""
    monkeypatch.setattr(cfg, "ORDER_VERIFY_POLL_S", 0.0)
    monkeypatch.setattr(cfg, "ORDER_RETRY_BACKOFF_S", 0.0)
    monkeypatch.setattr(cfg, "VERIFY_POLY_TIMEOUT_S", 0.0)
    if "py_clob_client_v2.clob_types" not in sys.modules:
        root = types.ModuleType("py_clob_client_v2")
        clob_types = types.ModuleType("py_clob_client_v2.clob_types")

        @dataclass
        class OrderPayload:
            orderID: str

        clob_types.OrderPayload = OrderPayload
        monkeypatch.setitem(sys.modules, "py_clob_client_v2", root)
        monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", clob_types)


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return init_db(tmp_path / "order_verify.db")


def _sig() -> BetSignal:
    return BetSignal(
        station_id="KDAL", target_date="2026-04-25", horizon=1,
        bracket_idx=0, threshold=95.0, bracket_label="93-95F YES", side="YES",
        p_model=0.60, p_market=0.40, edge=0.15, bet_size_usd=10.0,
        fill_price=0.40, volume_usd=1000.0,
        market_id="m1", token_id="t1", limit_price=0.40,
        prob_safe_floor=0.55, pred_bucket=(0.5, 0.6), n_bucket=100,
    )


def _make_client(fill_price: float = 0.40, size: float = 50.0) -> MagicMock:
    c = MagicMock()
    # Real pass-through: _bounded_client_call always routes through
    # _with_timeout when present; a MagicMock attribute would swallow the
    # wrapped call instead of invoking it.
    c._with_timeout = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    c.fetch_order_book.return_value = {"asks": [{"price": fill_price, "size": size}]}
    c.place_order.return_value = OrderResult(
        order_id="ord_X", fill_price=fill_price, fill_size=size / 2,
        fill_ts="2026-04-25T00:00Z", success=True, limit_price=fill_price,
    )
    c._client.get_open_orders.return_value = []
    return c


class TestVerifyRetryLoop:
    def test_bounded_client_call_passes_keyword_args_through_wrapper(self):
        class WrappedClient:
            def _with_timeout(self, fn, *args, **kwargs):
                return fn(*args, **kwargs)

        def _fetch_trade(*, order_id):
            return {"orderID": order_id}

        assert _bounded_client_call(
            WrappedClient(), _fetch_trade, order_id="ord_X"
        ) == {"orderID": "ord_X"}

    def test_get_trades_for_order_filters_legacy_trade_params_response(self):
        class InnerClient:
            def get_trades(self, params=None):
                return [
                    {"taker_order_id": "ord_X", "price": "0.42", "size": "7"},
                    {"taker_order_id": "ord_other", "price": "0.41", "size": "9"},
                ]

        class WrappedClient:
            _client = InnerClient()

            def _with_timeout(self, fn, *args, **kwargs):
                return fn(*args, **kwargs)

        assert _get_trades_for_order(WrappedClient(), "ord_X") == [
            {"taker_order_id": "ord_X", "price": "0.42", "size": "7"},
        ]

    def test_first_attempt_success_sets_tx_hash(self, db, monkeypatch):
        monkeypatch.setattr(cfg, "VERIFY_POLY_TIMEOUT_S", 1.0)
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {"status": "MATCHED"}
        client._client.get_trades.return_value = [
            {"price": "0.40", "size": "25", "transactionHash": "0xhappy"}
        ]

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and res.success
        assert res.transaction_hash == "0xhappy"
        assert res.verify_attempts == 1
        assert not res.verification_downgraded
        row = db.execute(
            "SELECT outcome, transaction_hash, verify_attempts FROM ledger WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["transaction_hash"] == "0xhappy"
        assert row["verify_attempts"] == 1

    def test_success_records_matched_fill_when_order_exposes_it(self, db, monkeypatch):
        monkeypatch.setattr(cfg, "VERIFY_POLY_TIMEOUT_S", 1.0)
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client(fill_price=0.40, size=50.0)
        client._client.get_order.return_value = {
            "status": "MATCHED",
            "avg_price": "0.37",
            "size_matched": "27",
        }
        client._client.get_trades.return_value = [{"transactionHash": "0xfill"}]

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and res.success
        assert res.fill_price == pytest.approx(0.37)
        assert res.fill_size == pytest.approx(27.0)
        row = db.execute(
            "SELECT fill_price, fill_size, bet_size FROM ledger WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["fill_price"] == pytest.approx(0.37)
        assert row["fill_size"] == pytest.approx(27.0)
        assert row["bet_size"] == pytest.approx(0.37 * 27.0)

    def test_dry_run_stamps_dry_run_uuid_and_skips_verify(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=True)
        client = _make_client()
        res = execute_or_log(sig, client, dry_run=True, row_id=row_id, conn=db, config=None)

        assert res is not None
        assert res.transaction_hash and res.transaction_hash.startswith("DRY_RUN_")
        assert res.verify_attempts == 0
        # get_order must not have been called — verify loop skipped.
        client._client.get_order.assert_not_called()
        row = db.execute(
            "SELECT transaction_hash, verify_attempts FROM ledger WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["transaction_hash"].startswith("DRY_RUN_")
        assert row["verify_attempts"] == 0

    def test_exhaustion_cancels_and_writes_pipeline_health(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {"status": "PLACED"}

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and not res.success
        assert res.verify_attempts == cfg.MAX_ORDER_RETRIES
        row = db.execute(
            "SELECT outcome, order_id, verify_attempts FROM ledger WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["outcome"] == "CANCELLED"
        assert row["order_id"] == "ord_X"
        assert row["verify_attempts"] == cfg.MAX_ORDER_RETRIES
        from py_clob_client_v2.clob_types import OrderPayload
        client._client.cancel_order.assert_called_once_with(OrderPayload(orderID="ord_X"))
        errs = db.execute(
            "SELECT COUNT(*) FROM pipeline_health WHERE stage='order' AND status='ERROR'"
        ).fetchone()[0]
        assert errs >= 1

    def test_cancel_failure_leaves_row_pending_with_order_id(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {"status": "PLACED"}
        client._client.cancel_order.side_effect = RuntimeError("cancel api down")

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and not res.success
        assert res.leave_pending is True
        row = db.execute(
            "SELECT outcome, order_id, verify_attempts FROM ledger WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["outcome"] == "PENDING"
        assert row["order_id"] == "ord_X"
        assert row["verify_attempts"] == cfg.MAX_ORDER_RETRIES

    def test_cancel_not_canceled_response_leaves_row_pending(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {"status": "PLACED"}
        client._client.cancel_order.return_value = {
            "canceled": [],
            "not_canceled": {"ord_X": "order already matched"},
        }

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and not res.success
        assert res.leave_pending is True
        row = db.execute(
            "SELECT outcome, order_id, verify_attempts FROM ledger WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["outcome"] == "PENDING"
        assert row["order_id"] == "ord_X"
        assert row["verify_attempts"] == cfg.MAX_ORDER_RETRIES

    def test_matched_without_tx_flips_verification_downgraded(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {"status": "MATCHED"}
        client._client.get_trades.return_value = [{"price": "0.40", "size": "25"}]

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and res.success
        assert res.verification_downgraded is True
        assert res.transaction_hash is None
        assert res.verify_attempts == 1
        assert res.realized_edge == pytest.approx(0.138)
        row = db.execute(
            "SELECT verification_downgraded, transaction_hash, realized_edge FROM ledger WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["verification_downgraded"] == 1
        assert row["transaction_hash"] is None
        assert row["realized_edge"] == pytest.approx(0.138)
        assert client.place_order.call_count == 1

    def test_verified_fak_with_unknown_fill_leaves_pending(self, db, monkeypatch):
        monkeypatch.setattr(cfg, "VERIFY_POLY_TIMEOUT_S", 1.0)
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {"status": "MATCHED"}
        client._client.get_trades.return_value = [{"transactionHash": "0xunknown"}]

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and not res.success
        assert res.leave_pending is True
        assert res.transaction_hash == "0xunknown"
        row = db.execute(
            "SELECT outcome, order_id, transaction_hash, fill_price, fill_size FROM ledger WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["outcome"] == "PENDING"
        assert row["order_id"] == "ord_X"
        assert row["transaction_hash"] == "0xunknown"
        assert row["fill_price"] is None
        assert row["fill_size"] is None

    def test_fak_terminal_partial_fill_records_actual_notional(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {
            "status": "CANCELED",
            "avg_price": "0.42",
            "size_matched": "7",
        }
        client._client.get_trades.return_value = [
            {"price": "0.42", "size": "7", "transactionHash": "0xfak"}
        ]

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and res.success
        assert res.transaction_hash == "0xfak"
        assert res.bet_size_usd == pytest.approx(0.42 * 7)
        row = db.execute(
            "SELECT outcome, fill_price, fill_size, bet_size, transaction_hash, event_detail "
            "FROM ledger WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["outcome"] == "PENDING"
        assert row["fill_price"] == pytest.approx(0.42)
        assert row["fill_size"] == pytest.approx(7.0)
        assert row["bet_size"] == pytest.approx(0.42 * 7)
        assert row["transaction_hash"] == "0xfak"
        detail = json.loads(row["event_detail"])
        assert detail["fill_levels"] == [
            {"price": 0.42, "shares": 7.0, "usd": 2.94},
        ]

    def test_fak_terminal_partial_fill_records_synthetic_level_without_trades(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {
            "status": "CANCELED",
            "avg_price": "0.42",
            "size_matched": "7",
        }
        client._client.get_trades.return_value = []

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and res.success
        assert res.verification_downgraded is True
        row = db.execute(
            "SELECT fill_price, fill_size, event_detail FROM ledger WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["fill_price"] == pytest.approx(0.42)
        assert row["fill_size"] == pytest.approx(7.0)
        detail = json.loads(row["event_detail"])
        assert detail["fill_levels"] == [
            {"price": 0.42, "shares": 7.0, "usd": 2.94},
        ]

    def test_fak_terminal_zero_fill_cancels_without_retry_alert(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {"status": "CANCELED"}
        client._client.get_trades.return_value = []

        captured: list[tuple[str, str, dict]] = []

        def _capture(title, message, **kw):
            captured.append((title, message, kw))
            return True

        with patch("hightempbot.execution.notify.send_alert", side_effect=_capture):
            mock_config = MagicMock()
            res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=mock_config)

        assert res is not None and not res.success
        assert res.verify_attempts == 1
        row = db.execute(
            "SELECT outcome, pnl, verify_attempts FROM ledger WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["outcome"] == "CANCELLED"
        assert row["pnl"] == 0.0
        assert row["verify_attempts"] == 1
        assert captured == []

    def test_fak_terminal_unknown_trades_stays_pending(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {"status": "CANCELED"}
        client._client.get_trades.return_value = None

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and not res.success
        assert res.leave_pending is True
        assert res.verify_attempts == cfg.MAX_ORDER_RETRIES
        client._client.cancel_order.assert_not_called()
        row = db.execute(
            "SELECT outcome, order_id, fill_price, fill_size, verify_attempts FROM ledger WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["outcome"] == "PENDING"
        assert row["order_id"] == "ord_X"
        assert row["fill_price"] is None
        assert row["fill_size"] is None
        assert row["verify_attempts"] == cfg.MAX_ORDER_RETRIES

    def test_matched_without_tx_logs_warning_without_immediate_alert(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {"status": "MATCHED"}
        client._client.get_trades.return_value = []

        captured: list[tuple[str, str, dict]] = []

        def _capture(title, message, **kw):
            captured.append((title, message, kw))
            return True

        with patch("hightempbot.execution.notify.send_alert", side_effect=_capture):
            mock_config = MagicMock()
            res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=mock_config)

        assert res is not None and not res.success
        assert res.leave_pending is True
        assert res.verification_downgraded is True
        assert captured == []

        row = db.execute(
            "SELECT status, message FROM pipeline_health WHERE stage='order' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["status"] == "WARNING"
        assert "fill_unknown" in row["message"]

    def test_retry_reuses_open_order_without_resubmitting(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {"status": "MATCHED"}
        trades_queue = [
            [],
            [],
            [{"price": "0.40", "size": "25", "transactionHash": "0xlate"}],
            [{"price": "0.40", "size": "25", "transactionHash": "0xlate"}],
        ]
        client._client.get_trades.side_effect = (
            lambda order_id=None: trades_queue.pop(0) if trades_queue else []
        )
        client._client.get_open_orders.return_value = [
            {"asset_id": "t1", "side": "YES", "id": "ord_reuse"},
        ]

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and res.success
        assert res.transaction_hash == "0xlate"
        assert res.verify_attempts == 2
        assert client.place_order.call_count == 1, (
            "retry must reuse the open order, not resubmit"
        )

    def test_timeout_retry_reuses_buy_open_order_without_resubmitting(self, db):
        """Attempt 1 submits ord_X but verify times out."""
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.side_effect = [
            {"status": "PLACED"},  # attempt 1 verify timeout
            {"status": "PLACED"},  # attempt 1 status peek
            {"status": "MATCHED"},  # attempt 2 verifies reused order
        ]
        trades_queue = [
            [],
            [{"price": "0.40", "size": "25", "transactionHash": "0xreuse"}],
            [{"price": "0.40", "size": "25", "transactionHash": "0xreuse"}],
        ]
        client._client.get_trades.side_effect = (
            lambda order_id=None: trades_queue.pop(0) if trades_queue else []
        )
        # Bot placed ord_X on attempt 1 (per _make_client default). The CLOB
        # still shows it open at retry — bot scope allows adoption.
        client._client.get_open_orders.return_value = [
            {"asset_id": "t1", "side": "BUY", "id": "ord_X"},
        ]

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and res.success
        assert res.order_id == "ord_X"
        assert res.transaction_hash == "0xreuse"
        assert res.verify_attempts == 2
        assert client.place_order.call_count == 1

    def test_retry_refuses_to_adopt_foreign_open_order(self, db):
        """An open order this bot didn't place (e.g. a manual one) is never adopted."""
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        # Attempt 1: verify times out without MATCHED. Attempt 2 onward: an
        # operator-placed order on the same token+side appears in open_orders.
        # Bot must NOT adopt it; instead it should keep retrying its own ord_X.
        client._client.get_order.return_value = {"status": "PLACED"}
        client._client.get_open_orders.return_value = [
            {"asset_id": "t1", "side": "BUY", "id": "ord_foreign"},
        ]
        # place_order is stubbed via _make_client to return ord_X. On attempts
        # 2/3 the retry idempotency check rejects ord_foreign — the bot keeps
        # its own order_id stamped on the ledger row.
        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and not res.success
        row = db.execute(
            "SELECT order_id FROM ledger WHERE id=?", (row_id,),
        ).fetchone()
        assert row["order_id"] == "ord_X"

    def test_place_order_raising_counts_as_failed_attempt(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client.place_order.side_effect = RuntimeError("CLOB down")

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and not res.success
        assert res.verify_attempts == cfg.MAX_ORDER_RETRIES
        row = db.execute(
            "SELECT outcome, verify_attempts FROM ledger WHERE id=?", (row_id,),
        ).fetchone()
        assert row["outcome"] == "CANCELLED"
        assert row["verify_attempts"] == cfg.MAX_ORDER_RETRIES

    def test_deposit_wallet_reject_is_terminal_auth(self):
        msg = (
            "PolyApiException[status_code=400, error_message={'error': "
            "'maker address not allowed, please use the deposit wallet flow'}]"
        )

        assert _classify_clob_error(msg) == "auth"

    def test_terminal_auth_error_stops_after_one_submit(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client.place_order.return_value = OrderResult(
            error=(
                "PolyApiException[status_code=400, error_message={'error': "
                "'maker address not allowed, please use the deposit wallet flow'}]"
            ),
            error_kind="auth",
        )

        res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=None)

        assert res is not None and not res.success
        assert res.error_kind == "auth"
        assert res.verify_attempts == 1
        assert res.error.startswith("auth:")
        assert client.place_order.call_count == 1
        row = db.execute(
            "SELECT outcome, verify_attempts FROM ledger WHERE id=?", (row_id,),
        ).fetchone()
        assert row["outcome"] == "CANCELLED"
        assert row["verify_attempts"] == 1


class TestAlertPayloadSafety:
    def test_alert_payload_does_not_leak_probs_edge_kelly_or_order_id(self, db):
        """Cancellation alert must not contain probability internals, edge,
        kelly, bet size, VWAP, order_id, or credential substrings."""
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {"status": "PLACED"}

        captured: list[tuple[str, str]] = []

        def _capture(title, message, **kw):
            captured.append((title, message))
            return True

        with patch("hightempbot.execution.notify.send_alert", side_effect=_capture):
            mock_config = MagicMock()
            execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=mock_config)

        assert captured, "expected send_alert to fire on exhaustion"
        title, message = captured[0]
        forbidden = ["lcb", "ucb", "edge", "kelly", "vwap", "p_model", "p_market",
                     "private_key", "api_secret", "passphrase", "ord_X"]
        combined = (title + " " + message).lower()
        for substr in forbidden:
            assert substr not in combined, f"alert payload leaked '{substr}': {combined!r}"

    def test_alert_payload_formats_mojibake_bracket_ascii(self, db):
        sig = _sig()
        sig.bracket_label = "\u00e2\u2030\u00a592\u00c2\u00b0F NO [NO]"
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {"status": "PLACED"}

        captured: list[tuple[str, str]] = []

        def _capture(title, message, **kw):
            captured.append((title, message))
            return True

        with patch("hightempbot.execution.notify.send_alert", side_effect=_capture):
            mock_config = MagicMock()
            execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=mock_config)

        assert captured
        _, message = captured[0]
        assert "bracket=>=92F NO [NO]" in message
        assert "\u00c2" not in message
        assert "\u00e2" not in message
        assert "\u00b0" not in message

    def test_send_alert_raising_does_not_block_cancellation(self, db):
        sig = _sig()
        row_id = record_bet(db, sig, None, dry_run=False)
        client = _make_client()
        client._client.get_order.return_value = {"status": "PLACED"}

        with patch(
            "hightempbot.execution.notify.send_alert",
            side_effect=RuntimeError("telegram down"),
        ):
            mock_config = MagicMock()
            res = execute_or_log(sig, client, dry_run=False, row_id=row_id, conn=db, config=mock_config)

        assert res is not None and not res.success
        row = db.execute(
            "SELECT outcome FROM ledger WHERE id=?", (row_id,),
        ).fetchone()
        assert row["outcome"] == "CANCELLED"
