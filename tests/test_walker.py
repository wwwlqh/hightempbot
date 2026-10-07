"""Tests for Unit 4: Order Placement + Dry-Run."""

from __future__ import annotations

import logging
import math
import sys
import types
from unittest.mock import MagicMock

import pytest
import requests

from hightempbot.execution.walker import (
    ClobReader,
    OrderClient,
    _max_walk_for_signal,
    _walk_anchor_for_signal,
    execute_or_log,
    quantize_market_buy_size,
    walk_book_edge_preserving,
)
from hightempbot.execution.types import BetSignal


def _make_signal(**kwargs) -> BetSignal:
    defaults = dict(
        station_id="KDAL", target_date="2026-04-07", horizon=1,
        bracket_idx=3, threshold=68.0, bracket_label="66-68°F YES",
        side="YES", p_model=0.45, p_market=0.30, edge=0.15,
        bet_size_usd=30.0, fill_price=0.30, volume_usd=5000.0,
        market_id="m1", token_id="t1", passed_all_gates=True,
    )
    defaults.update(kwargs)
    return BetSignal(**defaults)


def _install_fake_clob_modules(monkeypatch):
    root = types.ModuleType("py_clob_client_v2")
    order_builder = types.ModuleType("py_clob_client_v2.order_builder")
    constants = types.ModuleType("py_clob_client_v2.order_builder.constants")
    constants.BUY = "BUY"
    constants.SELL = "SELL"
    clob_types = types.ModuleType("py_clob_client_v2.clob_types")

    class FakeOrderArgs:
        def __init__(self, price, size, side, token_id):
            self.price = price
            self.size = size
            self.side = side
            self.token_id = token_id

    class FakeOrderType:
        GTC = "GTC"
        FOK = "FOK"
        FAK = "FAK"

    class FakeAssetType:
        COLLATERAL = "COLLATERAL"
        CONDITIONAL = "CONDITIONAL"

    class FakeBalanceAllowanceParams:
        def __init__(self, asset_type=None, token_id=None, signature_type=-1):
            self.asset_type = asset_type
            self.token_id = token_id
            self.signature_type = signature_type

    clob_types.OrderArgs = FakeOrderArgs
    clob_types.OrderType = FakeOrderType
    clob_types.AssetType = FakeAssetType
    clob_types.BalanceAllowanceParams = FakeBalanceAllowanceParams
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", root)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.order_builder", order_builder)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.order_builder.constants", constants)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", clob_types)
    return FakeOrderType


def _requests_http_error(status_code: int, message: str = "CLOB error"):
    response = requests.Response()
    response.status_code = status_code
    response._content = message.encode("utf-8")
    return requests.exceptions.HTTPError(message, response=response)


def _mock_clob_response(status_code: int, *, data: dict | None = None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = data or {}
    if status_code >= 400:
        response.raise_for_status.side_effect = _requests_http_error(
            status_code, "CLOB server error"
        )
    return response


class TestExecuteOrLog:
    def test_dry_run_returns_none(self):
        signal = _make_signal()
        result = execute_or_log(signal, order_client=None, dry_run=True)
        assert result is None

    def test_dry_run_does_not_place_order(self):
        signal = _make_signal()
        client = MagicMock()
        result = execute_or_log(signal, order_client=client, dry_run=True)
        assert result is None
        client.place_order.assert_not_called()

    def test_live_no_client_returns_error(self):
        signal = _make_signal()
        result = execute_or_log(signal, order_client=None, dry_run=False)
        assert result is not None
        assert result.success is False
        assert "pre-inserted pending ledger row" in result.error

    def test_live_without_pending_row_does_not_place_order(self):
        signal = _make_signal()
        client = MagicMock()

        result = execute_or_log(signal, order_client=client, dry_run=False)

        assert result.success is False
        assert "pre-inserted pending ledger row" in result.error
        client.fetch_order_book.assert_not_called()
        client.place_order.assert_not_called()


class TestBookOrdering:
    def test_best_ask_uses_lowest_price_even_when_book_is_descending(self):
        reader = ClobReader.__new__(ClobReader)
        book = {
            "asks": [
                {"price": "0.99", "size": "5000"},
                {"price": "0.62", "size": "36"},
                {"price": "0.58", "size": "53.48"},
                {"price": "0.55", "size": "76.72"},
            ],
            "bids": [],
        }

        assert reader.best_ask(book) == (0.55, 76.72)

    def test_best_bid_uses_highest_price_even_when_book_is_unsorted(self):
        reader = ClobReader.__new__(ClobReader)
        book = {
            "asks": [],
            "bids": [
                {"price": "0.45", "size": "35"},
                {"price": "0.01", "size": "5883.29"},
                {"price": "0.53", "size": "11"},
                {"price": "0.50", "size": "60"},
            ],
        }

        assert reader.best_bid(book) == (0.53, 11.0)


class TestClobReaderHttpErrors:
    def test_fetch_order_book_returns_none_for_404_without_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _mock_clob_response(404))
        reader = ClobReader.__new__(ClobReader)
        reader._timeout = 30

        with caplog.at_level(logging.WARNING, logger="hightempbot.execution.walker"):
            assert reader.fetch_order_book("missing-token") is None

        assert caplog.records == []

    @pytest.mark.parametrize("status_code", [400, 500])
    def test_fetch_order_book_logs_warning_for_non_404_http_errors(
        self, monkeypatch, caplog, status_code
    ):
        monkeypatch.setattr(
            requests, "get", lambda *args, **kwargs: _mock_clob_response(status_code)
        )
        reader = ClobReader.__new__(ClobReader)
        reader._timeout = 30

        with caplog.at_level(logging.WARNING, logger="hightempbot.execution.walker"):
            assert reader.fetch_order_book("error-token") is None

        assert any(
            record.levelno == logging.WARNING and "Failed to fetch CLOB /book" in record.message
            for record in caplog.records
        )

    def test_fetch_price_returns_none_for_404_without_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _mock_clob_response(404))
        reader = ClobReader.__new__(ClobReader)
        reader._timeout = 30

        with caplog.at_level(logging.WARNING, logger="hightempbot.execution.walker"):
            assert reader.fetch_price("missing-token", side="buy") is None

        assert caplog.records == []

    @pytest.mark.parametrize("status_code", [400, 500])
    def test_fetch_price_logs_warning_for_non_404_http_errors(
        self, monkeypatch, caplog, status_code
    ):
        monkeypatch.setattr(
            requests, "get", lambda *args, **kwargs: _mock_clob_response(status_code)
        )
        reader = ClobReader.__new__(ClobReader)
        reader._timeout = 30

        with caplog.at_level(logging.WARNING, logger="hightempbot.execution.walker"):
            assert reader.fetch_price("error-token", side="buy") is None

        assert any(
            record.levelno == logging.WARNING and "Failed to fetch CLOB /price" in record.message
            for record in caplog.records
        )


class TestClosePosition:
    def test_place_order_rejects_malformed_poly_1271_order_before_posting(self, monkeypatch):
        _install_fake_clob_modules(monkeypatch)
        client = OrderClient.__new__(OrderClient)
        client._signature_type = 3
        client._funder = "0xfb636718271f63e168db16804b4e23cea7d05fa3"
        client._client = MagicMock()
        client._client.create_order.return_value = types.SimpleNamespace(
            maker="0xfb636718271f63e168db16804b4e23cea7d05fa3",
            signer="0x1111111111111111111111111111111111111111",
            signatureType=3,
            signature="0x" + "a" * 130,
        )
        client._with_timeout = lambda fn, *args: fn(*args)

        result = client.place_order(_make_signal(limit_price=0.30))

        assert result.success is False
        assert result.error_kind == "auth"
        assert "signer" in result.error
        client._client.post_order.assert_not_called()

    def test_place_order_allows_wrapped_poly_1271_order(self, monkeypatch):
        order_type = _install_fake_clob_modules(monkeypatch)
        client = OrderClient.__new__(OrderClient)
        client._signature_type = 3
        client._funder = "0xfb636718271f63e168db16804b4e23cea7d05fa3"
        client._client = MagicMock()
        client._client.create_order.return_value = types.SimpleNamespace(
            maker="0xfb636718271f63e168db16804b4e23cea7d05fa3",
            signer="0xfb636718271f63e168db16804b4e23cea7d05fa3",
            signatureType=3,
            signature="0x" + "a" * 260,
        )
        client._client.post_order.return_value = {"orderID": "ord_poly_1271"}
        client._with_timeout = lambda fn, *args: fn(*args)

        result = client.place_order(_make_signal(limit_price=0.30))

        assert result.success is True
        assert result.order_id == "ord_poly_1271"
        post_args = client._client.post_order.call_args.args
        assert post_args[1] == order_type.FAK

    def test_quantize_market_buy_size_survives_py_clob_float_floor(self):
        quantized = quantize_market_buy_size(
            4.034288279999999,
            0.05,
            min_bet_usd=1.0,
        )

        assert quantized is not None
        size, spend = quantized
        client_size = math.floor(size * 100) / 100
        assert client_size == pytest.approx(80.60)
        assert client_size * 0.05 == pytest.approx(4.03)
        assert spend == pytest.approx(4.03)

    def test_place_order_rejects_signed_buy_with_subcent_maker_amount(self, monkeypatch):
        _install_fake_clob_modules(monkeypatch)
        client = OrderClient.__new__(OrderClient)
        client._signature_type = 3
        client._funder = "0xfb636718271f63e168db16804b4e23cea7d05fa3"
        client._client = MagicMock()
        client._client.create_order.return_value = types.SimpleNamespace(
            maker="0xfb636718271f63e168db16804b4e23cea7d05fa3",
            signer="0xfb636718271f63e168db16804b4e23cea7d05fa3",
            signatureType=3,
            signature="0x" + "a" * 260,
            makerAmount="4029500",
            takerAmount="80590000",
        )
        client._with_timeout = lambda fn, *args: fn(*args)

        result = client.place_order(_make_signal(
            limit_price=0.05,
            fill_price=0.05,
            bet_size_usd=4.034288279999999,
        ))

        assert result.success is False
        assert result.error_kind == "invalid_amounts"
        assert "maker amount" in result.error
        client._client.post_order.assert_not_called()

    @pytest.mark.parametrize(
        ("price", "target_usd", "expected_size", "expected_spend"),
        [
            (0.86, 8.6119, 10.0, 8.60),
            (0.87, 8.6119, 9.0, 7.83),
            (0.84, 8.5764, 10.0, 8.40),
        ],
    )
    def test_place_order_quantizes_market_buy_amounts_for_clob_precision(
        self, monkeypatch, price, target_usd, expected_size, expected_spend,
    ):
        _install_fake_clob_modules(monkeypatch)
        client = OrderClient.__new__(OrderClient)
        client._signature_type = 3
        client._funder = "0xfb636718271f63e168db16804b4e23cea7d05fa3"
        client._client = MagicMock()
        client._client.create_order.return_value = types.SimpleNamespace(
            maker="0xfb636718271f63e168db16804b4e23cea7d05fa3",
            signer="0xfb636718271f63e168db16804b4e23cea7d05fa3",
            signatureType=3,
            signature="0x" + "a" * 260,
        )
        client._client.post_order.return_value = {"orderID": "ord_quantized"}
        client._with_timeout = lambda fn, *args: fn(*args)

        result = client.place_order(_make_signal(
            limit_price=price,
            fill_price=price,
            bet_size_usd=target_usd,
        ))

        assert result.success is True
        assert result.order_id == "ord_quantized"
        assert result.fill_size == pytest.approx(expected_size)
        assert result.bet_size_usd == pytest.approx(expected_spend)
        assert result.bet_size_usd <= target_usd
        created_order = client._client.create_order.call_args.args[0]
        assert created_order.size == pytest.approx(expected_size)
        assert created_order.price * created_order.size == pytest.approx(expected_spend)
        assert round((created_order.price * created_order.size) * 100) == pytest.approx(
            expected_spend * 100
        )

    def test_place_order_skips_when_precision_quantization_drops_below_minimum(self, monkeypatch):
        _install_fake_clob_modules(monkeypatch)
        client = OrderClient.__new__(OrderClient)
        client._client = MagicMock()
        client._with_timeout = lambda fn, *args: fn(*args)

        result = client.place_order(_make_signal(
            limit_price=0.99,
            fill_price=0.99,
            bet_size_usd=1.0,
        ))

        assert result.success is False
        assert result.error_kind == "invalid_amounts"
        assert "below CLOB precision/minimum" in result.error
        client._client.create_order.assert_not_called()
        client._client.post_order.assert_not_called()

    def test_close_position_uses_full_size_fok_sell(self, monkeypatch):
        order_type = _install_fake_clob_modules(monkeypatch)
        client = OrderClient.__new__(OrderClient)
        client.fetch_order_book = MagicMock(return_value={
            "bids": [
                {"price": "0.35", "size": "5"},
                {"price": "0.40", "size": "8"},
            ]
        })
        client._client = MagicMock()
        client._client.update_balance_allowance.return_value = {"ok": True}
        client._client.get_balance_allowance.return_value = {
            "balance": "10000000",
            "allowances": {"0xspender": "10000000"},
        }
        client._client.create_order.side_effect = lambda args: args
        client._client.post_order.return_value = {"orderID": "close_1", "success": True}
        client._with_timeout = lambda fn, *args: fn(*args)

        result = client.close_position("tok", target_size=10.0)

        assert result.success is True
        allowance_params = client._client.update_balance_allowance.call_args.args[0]
        assert allowance_params.asset_type == "CONDITIONAL"
        assert allowance_params.token_id == "tok"
        created_order = client._client.create_order.call_args.args[0]
        assert created_order.size == pytest.approx(10.0)
        assert created_order.price == pytest.approx(0.35)
        post_args = client._client.post_order.call_args.args
        assert post_args[1] == order_type.FOK
        assert result.fill_size == pytest.approx(10.0)
        assert result.fill_price == pytest.approx(0.39)

    def test_close_position_rejects_vwap_below_min_before_posting(self, monkeypatch):
        _install_fake_clob_modules(monkeypatch)
        client = OrderClient.__new__(OrderClient)
        client.fetch_order_book = MagicMock(return_value={
            "bids": [{"price": "0.44", "size": "10"}]
        })
        client._client = MagicMock()
        client._with_timeout = lambda fn, *args: fn(*args)

        result = client.close_position(
            "tok",
            target_size=10.0,
            min_acceptable_vwap=0.45,
        )

        assert result.success is False
        assert "below minimum acceptable" in result.error
        assert result.error_kind == "stale_quote"
        client._client.update_balance_allowance.assert_not_called()
        client._client.create_order.assert_not_called()
        client._client.post_order.assert_not_called()

    def test_close_position_rejects_vwap_above_max_before_posting(self, monkeypatch):
        _install_fake_clob_modules(monkeypatch)
        client = OrderClient.__new__(OrderClient)
        client.fetch_order_book = MagicMock(return_value={
            "bids": [{"price": "0.25", "size": "10"}]
        })
        client._client = MagicMock()
        client._with_timeout = lambda fn, *args: fn(*args)

        result = client.close_position(
            "tok",
            target_size=10.0,
            max_acceptable_vwap=0.20,
        )

        assert result.success is False
        assert "above maximum acceptable" in result.error
        assert result.error_kind == "stale_quote"
        client._client.update_balance_allowance.assert_not_called()
        client._client.create_order.assert_not_called()
        client._client.post_order.assert_not_called()

    def test_close_position_rejects_malformed_poly_1271_order_before_posting(self, monkeypatch):
        _install_fake_clob_modules(monkeypatch)
        client = OrderClient.__new__(OrderClient)
        client._signature_type = 3
        client._funder = "0xfb636718271f63e168db16804b4e23cea7d05fa3"
        client.fetch_order_book = MagicMock(return_value={
            "bids": [{"price": "0.40", "size": "20"}]
        })
        client._client = MagicMock()
        client._client.update_balance_allowance.return_value = {"ok": True}
        client._client.get_balance_allowance.return_value = {
            "balance": "10000000",
            "allowances": {"0xspender": "10000000"},
        }
        client._client.create_order.return_value = types.SimpleNamespace(
            maker="0xfb636718271f63e168db16804b4e23cea7d05fa3",
            signer="0x1111111111111111111111111111111111111111",
            signatureType=3,
            signature="0x" + "a" * 130,
        )
        client._with_timeout = lambda fn, *args: fn(*args)

        result = client.close_position("tok", target_size=10.0)

        assert result.success is False
        assert result.error_kind == "auth"
        assert "signer" in result.error
        client._client.post_order.assert_not_called()

    def test_close_position_stops_when_conditional_allowance_refresh_fails(self, monkeypatch):
        _install_fake_clob_modules(monkeypatch)
        client = OrderClient.__new__(OrderClient)
        client._timeout = 15
        client.fetch_order_book = MagicMock(return_value={
            "bids": [{"price": "0.40", "size": "20"}]
        })
        client._client = MagicMock()
        client._client.update_balance_allowance.side_effect = RuntimeError("allowance down")
        client._with_timeout = lambda fn, *args: fn(*args)

        result = client.close_position("tok", target_size=10.0)

        assert result.success is False
        assert result.error_kind == "auth"
        assert "conditional allowance refresh failed" in result.error
        client._client.create_order.assert_not_called()
        client._client.post_order.assert_not_called()

    def test_close_position_stops_when_conditional_allowance_remains_zero(self, monkeypatch):
        _install_fake_clob_modules(monkeypatch)
        client = OrderClient.__new__(OrderClient)
        client._timeout = 15
        client.fetch_order_book = MagicMock(return_value={
            "bids": [{"price": "0.40", "size": "20"}]
        })
        client._client = MagicMock()
        client._client.update_balance_allowance.return_value = ""
        client._client.get_balance_allowance.return_value = {
            "balance": "10000000",
            "allowances": {
                "0xE111180000d2663C0091e4f400237545B87B996B": "10000000",
                "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296": "0",
            },
        }
        client._with_timeout = lambda fn, *args: fn(*args)

        result = client.close_position("tok", target_size=10.0)

        assert result.success is False
        assert result.error_kind == "auth"
        assert "conditional token allowance below close size" in result.error
        assert "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296" in result.error
        client._client.create_order.assert_not_called()
        client._client.post_order.assert_not_called()

    def test_close_position_rejects_partial_book_depth(self, monkeypatch):
        _install_fake_clob_modules(monkeypatch)
        client = OrderClient.__new__(OrderClient)
        client.fetch_order_book = MagicMock(return_value={
            "bids": [{"price": "0.40", "size": "5"}]
        })
        client._client = MagicMock()

        result = client.close_position("tok", target_size=10.0)

        assert result.success is False
        assert "insufficient bid depth" in result.error
        client._client.create_order.assert_not_called()


class TestWalkBookEdgePreserving:
    """Invariants for the edge-preserving walker (U4)."""

    FEE = 0.05          # POLY_FEE_THETA
    MIN_EDGE = 0.05     # matches config.MIN_EDGE

    def test_fills_full_target_when_book_is_deep_and_flat(self):
        book = {"asks": [{"price": "0.20", "size": "100000"}]}
        # prob_safe_floor chosen so edge at 0.20 = 0.30 - 0.20 - 0.008 = 0.092
        result = walk_book_edge_preserving(
            book, 10.0,
            prob_safe_floor=0.30, fee_theta=self.FEE, min_edge=self.MIN_EDGE,
            min_bet_usd=1.0,
        )
        assert result is not None
        filled_usd, filled_shares, vwap, limit_price, realized_edge = result
        assert filled_usd == pytest.approx(10.0)
        assert vwap == pytest.approx(0.20)
        assert limit_price == pytest.approx(0.20)
        assert realized_edge >= self.MIN_EDGE
        assert realized_edge == pytest.approx(0.092, abs=1e-4)

    def test_can_return_per_price_fill_levels(self):
        book = {"asks": [
            {"price": "0.12", "size": "83.3333333333"},
            {"price": "0.13", "size": "1000"},
        ]}
        result = walk_book_edge_preserving(
            book, 15.0,
            prob_safe_floor=0.30, fee_theta=self.FEE, min_edge=self.MIN_EDGE,
            min_bet_usd=1.0, return_levels=True,
        )

        assert result is not None
        filled_usd, _, vwap, limit_price, _, levels = result
        assert filled_usd == pytest.approx(15.0)
        assert vwap == pytest.approx(15.0 / (83.3333333333 + 5.0 / 0.13))
        assert limit_price == pytest.approx(0.13)
        assert levels[0]["price"] == pytest.approx(0.12)
        assert levels[0]["shares"] == pytest.approx(83.3333333333)
        assert levels[0]["usd"] == pytest.approx(10.0)
        assert levels[1]["price"] == pytest.approx(0.13)
        assert levels[1]["shares"] == pytest.approx(5.0 / 0.13)
        assert levels[1]["usd"] == pytest.approx(5.0)

    def test_stops_before_level_that_would_break_edge_floor(self):
        # Level 1: 0.20 → edge ≈ 0.092 (pass)
        # Level 2: 0.40 cheap side will pull VWAP up.
        # With target_usd=10 the walker fills $1 at L1 and then L2 would
        # drop VWAP edge below 0.05 — walker must stop before L2.
        book = {"asks": [
            {"price": "0.20", "size": "5"},       # 5 * 0.20 = $1 depth at L1
            {"price": "0.40", "size": "100"},      # deep but breaks edge
        ]}
        result = walk_book_edge_preserving(
            book, 10.0,
            prob_safe_floor=0.30, fee_theta=self.FEE, min_edge=self.MIN_EDGE,
            min_bet_usd=1.0,
        )
        assert result is not None
        filled_usd, _, vwap, limit_price, realized_edge = result
        assert filled_usd == pytest.approx(1.0)
        assert vwap == pytest.approx(0.20)
        assert limit_price == pytest.approx(0.20)
        assert realized_edge >= self.MIN_EDGE

    def test_skips_when_no_fill_reaches_min_bet_usd(self):
        # L1 $0.50 depth would pass edge floor, but then adding L2 drops VWAP edge
        # below min_edge; L1 alone is below min_bet_usd=1.0 → return None.
        book = {"asks": [
            {"price": "0.20", "size": "2.5"},   # $0.50 depth
            {"price": "0.40", "size": "100"},   # breaks edge
        ]}
        result = walk_book_edge_preserving(
            book, 10.0,
            prob_safe_floor=0.30, fee_theta=self.FEE, min_edge=self.MIN_EDGE,
            min_bet_usd=1.0,
        )
        assert result is None

    def test_returns_none_on_empty_asks(self):
        assert walk_book_edge_preserving(
            {"asks": []}, 10.0,
            prob_safe_floor=0.30, fee_theta=self.FEE, min_edge=self.MIN_EDGE,
            min_bet_usd=1.0,
        ) is None

    def test_realized_edge_ge_min_edge_always(self):
        # Property-ish: across several book shapes, realized_edge must never fall
        # below min_edge when result is not None.
        shapes = [
            {"asks": [{"price": "0.10", "size": "200"}]},
            {"asks": [{"price": "0.10", "size": "10"}, {"price": "0.12", "size": "50"}]},
            {"asks": [{"price": "0.18", "size": "80"}, {"price": "0.22", "size": "100"}]},
        ]
        for book in shapes:
            result = walk_book_edge_preserving(
                book, 8.0,
                prob_safe_floor=0.30, fee_theta=self.FEE, min_edge=self.MIN_EDGE,
                min_bet_usd=1.0,
            )
            if result is not None:
                _, _, _, _, realized_edge = result
                assert realized_edge >= self.MIN_EDGE - 1e-9

    def test_caps_at_target_usd_when_book_has_more_depth(self):
        book = {"asks": [{"price": "0.20", "size": "100000"}]}
        result = walk_book_edge_preserving(
            book, 5.0,
            prob_safe_floor=0.30, fee_theta=self.FEE, min_edge=self.MIN_EDGE,
            min_bet_usd=1.0,
        )
        assert result is not None
        filled_usd, _, _, _, _ = result
        assert filled_usd == pytest.approx(5.0)

    def test_walker_stops_at_price_walk_cap(self):
        # 2026-05-07 cap: walker must stop before consuming any ask priced
        # strictly above top_ask + max_walk_price (0.05 here). The 0.26
        # level is at top + 0.06 → must NOT be consumed, even though edge
        # at 0.26 would still be > 0 with a generous prob_safe_floor.
        # Top=0.20, second=0.24 (under cap), third=0.26 (above cap).
        # Edge floor disabled (min_edge very low) so the cap is the only
        # break condition this test exercises.
        book = {"asks": [
            {"price": "0.20", "size": "2000"},   # $400 fillable @ 0.20
            {"price": "0.24", "size": "5000"},   # $1200 fillable @ 0.24 (cap=0.25)
            {"price": "0.26", "size": "9999"},   # 0.26 > 0.25 → must stop
        ]}
        result = walk_book_edge_preserving(
            book, 10000.0,
            prob_safe_floor=0.95, fee_theta=self.FEE, min_edge=-1.0,
            min_bet_usd=1.0, max_walk_price=0.05,
        )
        assert result is not None
        filled_usd, _, vwap, limit_price, _ = result
        # Walker fills L1 ($400) + L2 ($1200) = $1600, never crosses to L2.6.
        assert filled_usd == pytest.approx(1600.0)
        assert 0.20 <= vwap <= 0.24
        assert limit_price == pytest.approx(0.24)

    def test_walker_consumes_level_exactly_at_cap(self):
        # Cap is strict-greater (price > top + max_walk_price), so a level
        # priced exactly at top + 0.05 = 0.25 IS consumable.
        book = {"asks": [
            {"price": "0.20", "size": "100"},   # $20 @ 0.20
            {"price": "0.25", "size": "100"},   # $25 @ 0.25 = at cap, must consume
        ]}
        result = walk_book_edge_preserving(
            book, 100.0,
            prob_safe_floor=0.95, fee_theta=self.FEE, min_edge=-1.0,
            min_bet_usd=1.0, max_walk_price=0.05,
        )
        assert result is not None
        filled_usd, _, _, limit_price, _ = result
        # Both levels consumed.
        assert filled_usd == pytest.approx(45.0)
        assert limit_price == pytest.approx(0.25)

    def test_walker_max_walk_price_none_is_byte_compatible(self):
        # Regression guard: when max_walk_price is omitted (the back-compat
        # default), behavior is identical to the legacy walker — no cap,
        # only the edge floor governs.
        book = {"asks": [
            {"price": "0.20", "size": "100000"},
        ]}
        result_no_cap = walk_book_edge_preserving(
            book, 10.0,
            prob_safe_floor=0.30, fee_theta=self.FEE, min_edge=self.MIN_EDGE,
            min_bet_usd=1.0,
        )
        result_cap_none = walk_book_edge_preserving(
            book, 10.0,
            prob_safe_floor=0.30, fee_theta=self.FEE, min_edge=self.MIN_EDGE,
            min_bet_usd=1.0, max_walk_price=None,
        )
        assert result_no_cap == result_cap_none

    def test_walker_max_walk_price_zero_fills_only_top_level(self):
        # max_walk_price=0.0 → cap = top_ask + 0.0 = top_ask. Strict-greater
        # comparison rejects any level priced strictly above top_ask. Only
        # the cheapest ask is consumable.
        book = {"asks": [
            {"price": "0.20", "size": "50"},   # $10 @ 0.20 — consumable
            {"price": "0.21", "size": "10000"},  # 0.21 > 0.20 → blocked
        ]}
        result = walk_book_edge_preserving(
            book, 10000.0,
            prob_safe_floor=0.95, fee_theta=self.FEE, min_edge=-1.0,
            min_bet_usd=1.0, max_walk_price=0.0,
        )
        assert result is not None
        filled_usd, _, _, limit_price, _ = result
        assert filled_usd == pytest.approx(10.0)
        assert limit_price == pytest.approx(0.20)

    def test_walker_negative_max_walk_price_returns_none(self):
        # Negative cap is a config typo; walker rejects rather than walking
        # uncapped. Logs a warning (not asserted here, but covered by the
        # log capture in production telemetry).
        book = {"asks": [{"price": "0.20", "size": "10000"}]}
        result = walk_book_edge_preserving(
            book, 10.0,
            prob_safe_floor=0.30, fee_theta=self.FEE, min_edge=self.MIN_EDGE,
            min_bet_usd=1.0, max_walk_price=-0.05,
        )
        assert result is None

    def test_walker_non_finite_max_walk_price_returns_none(self):
        # Inf/NaN cap is also rejected — defensive validation prevents
        # `inf - x` math from producing a silently-disabled cap.
        book = {"asks": [{"price": "0.20", "size": "10000"}]}
        for bad in (float("inf"), float("nan")):
            assert walk_book_edge_preserving(
                book, 10.0,
                prob_safe_floor=0.30, fee_theta=self.FEE, min_edge=self.MIN_EDGE,
                min_bet_usd=1.0, max_walk_price=bad,
            ) is None

    def test_walker_skips_non_finite_top_to_anchor_cap(self):
        # P2 #4: if asks[0] (after sort) has a non-finite price, the cap
        # must NOT silently disable. Walker should scan for the first finite
        # ask and anchor the cap there. Below: a poisoned NaN level would
        # sort to position 0 in some Python versions; even so, the second
        # level (finite) should be chosen as the cap anchor and the loop
        # respects that cap.
        book = {"asks": [
            {"price": "nan", "size": "10000"},  # poisoned — must be skipped
            {"price": "0.20", "size": "50"},     # first finite → anchor at 0.20
            {"price": "0.30", "size": "10000"},  # 0.30 > 0.20 + 0.05 → blocked
        ]}
        result = walk_book_edge_preserving(
            book, 10000.0,
            prob_safe_floor=0.95, fee_theta=self.FEE, min_edge=-1.0,
            min_bet_usd=1.0, max_walk_price=0.05,
        )
        assert result is not None
        filled_usd, _, _, limit_price, _ = result
        # Only the 0.20 level fills ($10); 0.30 level blocked by the cap.
        assert filled_usd == pytest.approx(10.0)
        assert limit_price == pytest.approx(0.20)

    def test_walker_walk_anchor_price_sticks_across_book_drift(self):
        # P1 #2: retry callers pass walk_anchor_price=signal.entry_top_price
        # so the cap is sticky to the scanner-time top. Simulate book drift:
        # original top was 0.55 (cap=0.60), now top has moved to 0.62. With
        # walk_anchor_price=0.55, the cap is 0.60 — second-level 0.61 is
        # blocked. Without the anchor, fresh-top 0.62 would set cap=0.67
        # and let 0.61 fill.
        book = {"asks": [
            {"price": "0.62", "size": "100"},  # fresh top
            {"price": "0.61", "size": "100"},  # within fresh-top cap (0.67) but above anchored cap (0.60)
        ]}
        # Sticky anchor — should reject the 0.62 level because cap = 0.60.
        # _sorted_asks puts 0.61 first; 0.62 second. 0.61 > 0.60 → break
        # before consuming any level → returns None.
        result_anchored = walk_book_edge_preserving(
            book, 100.0,
            prob_safe_floor=0.95, fee_theta=self.FEE, min_edge=-1.0,
            min_bet_usd=1.0, max_walk_price=0.05, walk_anchor_price=0.55,
        )
        assert result_anchored is None

        # No anchor (default) — fresh top 0.61 sets cap=0.66; consumes both.
        result_fresh = walk_book_edge_preserving(
            book, 100.0,
            prob_safe_floor=0.95, fee_theta=self.FEE, min_edge=-1.0,
            min_bet_usd=1.0, max_walk_price=0.05,
        )
        assert result_fresh is not None


# ---------------- Order-time top-up walk policy

def test_max_walk_for_signal_no_topup_uses_edge_floor_without_slip_leash():
    """NO/TAIL no longer carry a top-up slip leash; execution_min_edge bounds
    the order-time walk."""
    sig = _make_signal(strategy="NO", side="NO", slip_anchor_vwap=0.75)
    assert _max_walk_for_signal(sig) is None


def test_max_walk_for_signal_tail_topup_uses_edge_floor_without_slip_leash():
    sig = _make_signal(strategy="TAIL", side="YES", slip_anchor_vwap=0.02)
    assert _max_walk_for_signal(sig) is None


def test_max_walk_for_signal_no_first_fill_uses_edge_floor_no_leash():
    """NO first fill (no slip anchor) returns None — the VWAP edge floor is the
    fill boundary, no fixed price leash."""
    sig = _make_signal(strategy="NO", side="NO")
    assert _max_walk_for_signal(sig) is None


def test_max_walk_for_signal_legacy_leash_for_non_floor_sleeve():
    """A sleeve without execution_min_edge and no slip anchor keeps its legacy
    max_walk_price leash (YMID = 0.05)."""
    sig = _make_signal(strategy="YMID", side="YES")
    assert _max_walk_for_signal(sig) == pytest.approx(0.05)


def test_walk_anchor_for_signal_prefers_slip_anchor():
    sig = _make_signal(strategy="NO", side="NO", slip_anchor_vwap=0.73, entry_top_price=0.80)
    assert _walk_anchor_for_signal(sig) == pytest.approx(0.73)


def test_walk_anchor_for_signal_defaults_to_entry_top_price():
    sig = _make_signal(strategy="NO", side="NO", entry_top_price=0.80)
    assert _walk_anchor_for_signal(sig) == pytest.approx(0.80)
