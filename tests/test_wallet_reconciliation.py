from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from hightempbot.dashboard.v2_data import _latest_wallet_snapshot_for_dashboard
from hightempbot.db.connection import init_db
from hightempbot.dashboard.wallet_data import build_operator_wallet_payload
from hightempbot.execution.live_readiness import ReadinessReport, record_readiness_report
from hightempbot.execution.strategy_constants import POLY_FEE_THETA
from hightempbot.persistence.wallet_reconciliation import (
    build_wallet_snapshot,
    latest_wallet_snapshot,
    refresh_wallet_snapshot,
    record_wallet_snapshot,
    wallet_dashboard_payload,
)
from hightempbot.runtime_config import Config, set_config


class _Cfg:
    poly_funder = "0x" + "d" * 40
    wallet_snapshot_freshness_ttl_s = 300
    live_onchain_verify_enabled = True
    live_balance_tolerance_usd = 0.25
    polygon_rpc_url = "https://rpc.test"


def test_records_wallet_snapshot_and_marks_it_fresh(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        snapshot = build_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            clob_balance_usd=100.0,
            chain_balance_usd=100.01,
            data_api_trades=[{
                "transactionHash": "0xabc",
                "asset": "tok",
                "size": "10",
                "price": "0.50",
                "side": "BUY",
            }],
            data_api_positions=[],
        )
        run_id = record_wallet_snapshot(conn, snapshot)

        assert run_id == 1
        latest = latest_wallet_snapshot(conn, wallet_address=_Cfg.poly_funder)
        assert latest is not None
        assert latest["fresh"] is True
        assert latest["clobBalanceUsd"] == 100.0
        assert latest["records"][0]["match_status"] == "orphan_wallet_trade"

        # ce-code-review P2 #44: an orphan_wallet_trade is a warning that must
        # propagate into the wallet payload as actionsEnabled=False. Without
        # this assertion an upstream regression could let the dashboard light
        # up the "Submit Transfer" / "Start Processing" buttons over a
        # wallet+ledger drift.
        cfg = Config(_env_file=None, dry_run=False, poly_funder=_Cfg.poly_funder)
        set_config(cfg)
        payload = wallet_dashboard_payload(conn, config=cfg, dry_run=False)
        assert payload["actionsEnabled"] is False
        assert payload["transferEligible"] is False
        assert any("wallet trade" in w for w in payload["warnings"])
    finally:
        conn.close()


def test_wallet_payload_does_not_fallback_to_unfiltered_bankroll_peak(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        conn.execute(
            "INSERT INTO bankroll_peak (sampled_at, wallet_balance, realized_pnl, pending_exposure) "
            "VALUES (?, ?, ?, ?)",
            ("2026-05-21 08:19:10", 77.0, 0.0, 0.0),
        )
        conn.commit()

        payload = wallet_dashboard_payload(conn, config=_Cfg(), dry_run=False)

        assert payload["snapshot"]["clobBalanceUsd"] is None
        assert payload["snapshot"]["sourcesChecked"]["clobBalance"] is False
        assert payload["actionsEnabled"] is False
        assert any("POLY_FUNDER" in warning for warning in payload["warnings"])
    finally:
        conn.close()


def test_v2_wallet_snapshot_uses_configured_funder_only(tmp_path):
    conn = init_db(tmp_path / "test.db")
    other_wallet = "0x" + "e" * 40
    cfg = Config(_env_file=None, dry_run=False, poly_funder=_Cfg.poly_funder)
    set_config(cfg)
    try:
        record_wallet_snapshot(
            conn,
            build_wallet_snapshot(
                conn,
                wallet_address=other_wallet,
                clob_balance_usd=77.0,
                chain_balance_usd=77.0,
                data_api_trades=[],
                data_api_positions=[],
                open_orders=[],
            ),
        )

        assert _latest_wallet_snapshot_for_dashboard(conn) is None
    finally:
        set_config(None)
        conn.close()


def test_data_api_trade_matches_ledger_fill_without_order_id_or_tx_hash(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, fill_ts, outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-05-21 13:14:00",
                "KLAX",
                "market",
                "token-no",
                "2026-05-21",
                1,
                74.0,
                "NO",
                0.30,
                0.53,
                0.10,
                1.855,
                5000.0,
                1.855,
                0.53,
                "0xorder",
                0.53,
                3.5,
                "2026-05-21 13:15:16",
                "PENDING",
                "bet",
            ),
        )
        conn.commit()

        snapshot = build_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            clob_balance_usd=90.0,
            chain_balance_usd=90.0,
            data_api_trades=[{
                "transactionHash": "0xdataapi",
                "asset": "token-no",
                "size": "3.5",
                "price": "0.53",
                "side": "BUY",
                "outcome": "No",
                "timestamp": 1779369316,
            }],
            data_api_positions=[],
        )

        assert snapshot.records[0].match_status == "exact"
        assert snapshot.records[0].matched_ledger_id is not None
        assert "wallet trade" not in " ".join(snapshot.warnings)
    finally:
        conn.close()


def test_data_api_trade_matches_rounded_ledger_fill_price(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, fill_ts, outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-05-22 04:52:00",
                "KATL",
                "market",
                "token-tail",
                "2026-05-22",
                1,
                90.0,
                "YES",
                0.12,
                0.06,
                0.06,
                1.7982,
                5000.0,
                1.7982,
                0.06,
                "0xorder",
                0.06,
                29.97,
                "2026-05-22 04:53:10",
                "PENDING",
                "bet",
            ),
        )
        conn.commit()

        snapshot = build_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            clob_balance_usd=49.0,
            chain_balance_usd=49.0,
            data_api_trades=[{
                "transactionHash": "0xdataapi-rounded",
                "asset": "token-tail",
                "size": "29.97",
                "price": "0.05760960960960961",
                "side": "BUY",
                "outcome": "Yes",
                "timestamp": "2026-05-22 04:53:10",
            }],
            data_api_positions=[],
        )

        assert snapshot.records[0].match_status == "exact"
        assert snapshot.records[0].matched_ledger_id is not None
        assert "wallet trade" not in " ".join(snapshot.warnings)
    finally:
        conn.close()


def test_data_api_trade_matches_low_price_high_share_fill(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, fill_ts, outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-05-22 05:01:33",
                "KHOU",
                "market",
                "token-khou-tail",
                "2026-05-22",
                1,
                90.0,
                "YES",
                0.08,
                0.029,
                0.05,
                2.10424,
                5000.0,
                2.10424,
                0.029,
                "0xorder",
                0.029,
                72.56,
                "2026-05-22 05:02:27",
                "PENDING",
                "bet",
            ),
        )
        conn.commit()

        snapshot = build_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            clob_balance_usd=36.0,
            chain_balance_usd=36.0,
            data_api_trades=[{
                "transactionHash": "0xdataapi-khou",
                "asset": "token-khou-tail",
                "size": "72.56",
                "price": "0.027015435501653803",
                "side": "BUY",
                "outcome": "Yes",
                "timestamp": "2026-05-22 05:01:37",
            }],
            data_api_positions=[],
        )

        assert snapshot.records[0].match_status == "exact"
        assert snapshot.records[0].matched_ledger_id is not None
        assert "wallet trade" not in " ".join(snapshot.warnings)
    finally:
        conn.close()


def test_data_api_trade_matches_vwap_drift_and_backfills_tx_hash(tmp_path):
    conn = init_db(tmp_path / "test.db")

    class _OrderClient:
        def __init__(self, _cfg):
            self._client = self

        def check_balance(self):
            return 37.39

        def get_open_orders(self):
            return []

    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, fill_ts, outcome, event_type,
             event_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-05-22 09:01:21",
                "KHOU",
                "market",
                "token-khou-no",
                "2026-05-22",
                1,
                90.0,
                "NO",
                0.12,
                0.889,
                0.145,
                5.25399,
                5000.0,
                5.25399,
                0.889,
                "0xorder",
                0.889,
                5.91,
                "2026-05-22 09:02:15",
                "PENDING",
                "bet",
                "{}",
            ),
        )
        conn.commit()

        snapshot = refresh_wallet_snapshot(
            conn,
            config=_Cfg(),
            order_client_factory=_OrderClient,
            data_api_fetcher=lambda _wallet: ([{
                "transactionHash": "0xb453cb5e65a3e7b058304a9c457626bdcec3ccde919fd556ec329bd53c2d7f3e",
                "asset": "token-khou-no",
                "size": "5.91",
                "price": "0.8654670050761422",
                "side": "BUY",
                "outcome": "No",
                "timestamp": 1779440486,
            }], []),
            chain_balance_reader=lambda **_kwargs: 37.39,
        )

        row = conn.execute("SELECT transaction_hash, event_detail FROM ledger WHERE token_id='token-khou-no'").fetchone()
        assert row["transaction_hash"].startswith("0xb453")
        assert snapshot.records[0].match_status == "exact"
        assert snapshot.records[0].matched_ledger_id is not None
        assert "wallet trade" not in " ".join(snapshot.warnings)
    finally:
        conn.close()


def test_refresh_wallet_snapshot_does_not_alert_stale_missing_tx_hash(tmp_path):
    conn = init_db(tmp_path / "test.db")

    class _NotifyCfg(_Cfg):
        notify_telegram_token = "token"
        notify_telegram_chat_id = "chat"

    class _OrderClient:
        def __init__(self, _cfg):
            self._client = self

        def check_balance(self):
            return 100.0

        def get_open_orders(self):
            return []

    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=31)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, fill_ts, outcome, event_type,
             verification_downgraded, verify_attempts, event_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                old_ts,
                "KAUS",
                "market",
                "token-austin-no",
                "2026-05-25",
                1,
                91.5,
                "NO",
                0.08,
                0.86,
                0.09,
                8.57,
                5000.0,
                8.57,
                0.86,
                "0xorder",
                0.86,
                9.96,
                old_ts,
                "PENDING",
                "bet",
                1,
                3,
                json.dumps({"bracket_low": 91.5, "bracket_high": None, "bracket_unit": "F"}),
            ),
        )
        conn.commit()

        captured: list[tuple[str, str, dict]] = []

        def _capture(title, message, **kw):
            captured.append((title, message, kw))
            return True

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("hightempbot.execution.notify.send_alert", _capture)
            refresh_wallet_snapshot(
                conn,
                config=_NotifyCfg(),
                order_client_factory=_OrderClient,
                data_api_fetcher=lambda _wallet: ([], []),
                chain_balance_reader=lambda **_kwargs: 100.0,
            )
            refresh_wallet_snapshot(
                conn,
                config=_NotifyCfg(),
                order_client_factory=_OrderClient,
                data_api_fetcher=lambda _wallet: ([], []),
                chain_balance_reader=lambda **_kwargs: 100.0,
            )

        assert captured == []

        row = conn.execute(
            "SELECT event_detail FROM ledger WHERE token_id='token-austin-no'"
        ).fetchone()
        detail = json.loads(row["event_detail"])
        assert "tx_hash_missing_alert_delay_minutes" not in detail
        assert "tx_hash_missing_alert_sent_at" not in detail
    finally:
        conn.close()


def test_refresh_wallet_snapshot_reconciles_fill_vwap_from_data_api_trade(tmp_path):
    conn = init_db(tmp_path / "test.db")
    condition = "0x" + "3" * 64
    tx_hash = "0x84cbc3bd0e9252678304d25919ccd5a69ffedc2c19e648738f6e837afceaacbf"

    class _OrderClient:
        def __init__(self, _cfg):
            self._client = self

        def check_balance(self):
            return 36.0

        def get_open_orders(self):
            return []

    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, fill_ts, outcome, event_type,
             transaction_hash, event_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-05-22 05:01:33",
                "KHOU",
                condition,
                "token-houston-yes",
                "2026-05-22",
                1,
                90.0,
                "YES",
                0.08,
                0.029,
                0.05,
                2.10424,
                5000.0,
                2.10424,
                0.029,
                "0xorder",
                0.029,
                72.56,
                "2026-05-22 05:02:27",
                "PENDING",
                "bet",
                tx_hash,
                "{}",
            ),
        )
        conn.commit()

        snapshot = refresh_wallet_snapshot(
            conn,
            config=_Cfg(),
            order_client_factory=_OrderClient,
            data_api_fetcher=lambda _wallet: ([{
                "transactionHash": tx_hash,
                "asset": "token-houston-yes",
                "size": "72.56",
                "price": "0.027015435501653803",
                "side": "BUY",
                "outcome": "Yes",
                "timestamp": "2026-05-22 05:01:37",
            }], [{
                "asset": "token-houston-yes",
                "conditionId": condition,
                "size": 72.56,
                "avgPrice": 0.027,
                "initialValue": 1.9602,
                "currentValue": 10.7388,
                "cashPnl": 8.7785,
                "curPrice": 0.148,
                "outcome": "Yes",
            }]),
            chain_balance_reader=lambda **_kwargs: 36.0,
        )

        row = conn.execute(
            "SELECT bet_size, kelly_size, fill_price, event_detail FROM ledger "
            "WHERE token_id='token-houston-yes'"
        ).fetchone()
        assert row["fill_price"] == pytest.approx(0.027015435501653803)
        assert row["bet_size"] == pytest.approx(0.027015435501653803 * 72.56)
        assert row["kelly_size"] == pytest.approx(row["bet_size"])
        detail = json.loads(row["event_detail"])
        assert detail["wallet_fill_reconciled"] is True
        assert detail["wallet_fill_reconcile_previous_fill_price"] == pytest.approx(0.029)
        assert snapshot.data_api_matched_positions_count == 1
        assert snapshot.data_api_mismatched_positions_count == 0
        assert snapshot.data_api_trusted_open_positions_value_usd == pytest.approx(10.7388)
    finally:
        conn.close()


def test_refresh_wallet_snapshot_recomputes_terminal_win_from_data_api_trade(tmp_path):
    conn = init_db(tmp_path / "test.db")
    condition = "0x" + "4" * 64
    tx_hash = "0x84cbc3bd0e9252678304d25919ccd5a69ffedc2c19e648738f6e837afceaacbf"
    api_price = 0.027015435501653803
    size = 72.56

    class _OrderClient:
        def __init__(self, _cfg):
            self._client = self

        def check_balance(self):
            return 106.0

        def get_open_orders(self):
            return []

    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, fill_ts, outcome, pnl,
             event_type, transaction_hash, event_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-05-22 05:01:33",
                "KHOU",
                condition,
                "token-terminal-yes",
                "2026-05-22",
                1,
                90.0,
                "YES",
                0.08,
                0.029,
                0.05,
                2.10424,
                5000.0,
                2.10424,
                0.029,
                "0xorder",
                0.029,
                size,
                "2026-05-22 05:02:27",
                "WIN",
                70.0,
                "bet",
                tx_hash,
                json.dumps({"resolution_price": 1.0}),
            ),
        )
        conn.commit()

        refresh_wallet_snapshot(
            conn,
            config=_Cfg(),
            order_client_factory=_OrderClient,
            data_api_fetcher=lambda _wallet: ([{
                "transactionHash": tx_hash,
                "asset": "token-terminal-yes",
                "size": str(size),
                "price": str(api_price),
                "side": "BUY",
                "outcome": "Yes",
                "timestamp": "2026-05-22 05:01:37",
            }], []),
            chain_balance_reader=lambda **_kwargs: 106.0,
        )

        row = conn.execute(
            "SELECT bet_size, kelly_size, fill_price, pnl, event_detail FROM ledger "
            "WHERE token_id='token-terminal-yes'"
        ).fetchone()
        amount = api_price * size
        fee = size * POLY_FEE_THETA * api_price * (1.0 - api_price)
        assert row["fill_price"] == pytest.approx(api_price)
        assert row["bet_size"] == pytest.approx(amount)
        assert row["kelly_size"] == pytest.approx(amount)
        assert row["pnl"] == pytest.approx(size - amount - fee)
        detail = json.loads(row["event_detail"])
        assert detail["wallet_fill_reconcile_previous_pnl"] == pytest.approx(70.0)
        assert detail["wallet_fill_reconcile_recomputed_pnl"] is True
        assert detail["pnl_gross"] == pytest.approx(size - amount)
        assert detail["poly_entry_fee"] == pytest.approx(fee)
    finally:
        conn.close()


def test_refresh_wallet_snapshot_reconciles_open_cost_from_data_api_position(tmp_path):
    conn = init_db(tmp_path / "test.db")
    condition = "0x" + "5" * 64

    class _OrderClient:
        def __init__(self, _cfg):
            self._client = self

        def check_balance(self):
            return 95.0

        def get_open_orders(self):
            return []

    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, fill_ts, outcome, event_type,
             event_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-05-22 08:00:00",
                "KDAL",
                condition,
                "token-api-position",
                "2026-05-22",
                1,
                75.0,
                "NO",
                0.30,
                0.80,
                0.10,
                5.05,
                5000.0,
                5.05,
                0.80,
                "ord_1",
                0.808,
                6.25,
                "2026-05-22 08:01:00",
                "PENDING",
                "bet",
                "{}",
            ),
        )
        conn.commit()

        snapshot = refresh_wallet_snapshot(
            conn,
            config=_Cfg(),
            order_client_factory=_OrderClient,
            data_api_fetcher=lambda wallet: ([], [{
                "proxyWallet": wallet,
                "asset": "token-api-position",
                "conditionId": condition,
                "size": 6.25,
                "avgPrice": 0.80,
                "initialValue": 5.0,
                "currentValue": 6.25,
                "cashPnl": 1.25,
                "curPrice": 1.0,
                "outcome": "No",
            }]),
            chain_balance_reader=lambda **_kwargs: 95.0,
        )

        row = conn.execute(
            "SELECT bet_size, kelly_size, fill_price, event_detail FROM ledger "
            "WHERE token_id='token-api-position'"
        ).fetchone()
        assert row["bet_size"] == pytest.approx(5.0)
        assert row["kelly_size"] == pytest.approx(5.0)
        assert row["fill_price"] == pytest.approx(0.80)
        detail = json.loads(row["event_detail"])
        assert detail["wallet_position_reconciled"] is True
        assert detail["wallet_position_reconcile_previous_bet_size"] == pytest.approx(5.05)
        assert snapshot.data_api_matched_positions_count == 1
        assert snapshot.data_api_mismatched_positions_count == 0
        assert snapshot.data_api_trusted_open_positions_initial_value_usd == pytest.approx(5.0)
        assert snapshot.data_api_reconciliation_warnings == []
    finally:
        conn.close()


def test_refresh_wallet_snapshot_backfills_no_order_pending_from_wallet_trade(tmp_path):
    conn = init_db(tmp_path / "test.db")

    class _OrderClient:
        def __init__(self, _cfg):
            self._client = self

        def check_balance(self):
            return 31.61

        def get_open_orders(self):
            return []

    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, fill_ts, outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-05-22 07:17:17",
                "KSFO",
                "market",
                "token-ksfo-no",
                "2026-05-22",
                1,
                65.5,
                "NO",
                0.016,
                0.85,
                0.126,
                2.5003615525,
                1023.0,
                2.5003615525,
                0.85,
                None,
                None,
                None,
                None,
                "PENDING",
                "bet",
            ),
        )
        conn.commit()

        snapshot = refresh_wallet_snapshot(
            conn,
            config=_Cfg(),
            order_client_factory=_OrderClient,
            data_api_fetcher=lambda _wallet: ([{
                "transactionHash": "0x1273ea28094ee8f97d77ebe3f2a32837e3189fc275233ec101290dcfc6cc4567",
                "asset": "token-ksfo-no",
                "size": "2.94",
                "price": "0.85",
                "side": "BUY",
                "outcome": "No",
                "timestamp": "2026-05-22 07:17:22",
            }], []),
            chain_balance_reader=lambda **_kwargs: 31.61,
        )

        row = conn.execute("SELECT * FROM ledger WHERE token_id = 'token-ksfo-no'").fetchone()
        assert row["order_id"].startswith("DATAAPI_")
        assert row["fill_price"] == 0.85
        assert row["fill_size"] == 2.94
        assert row["bet_size"] == 0.85 * 2.94
        assert row["transaction_hash"].startswith("0x1273")
        assert snapshot.records[0].match_status == "exact"
        assert "wallet trade" not in " ".join(snapshot.warnings)
    finally:
        conn.close()


def test_balance_disagreement_sets_dashboard_warning(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        snapshot = build_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            clob_balance_usd=100.0,
            chain_balance_usd=90.0,
            tolerance_usd=0.25,
        )
        record_wallet_snapshot(conn, snapshot)

        payload = wallet_dashboard_payload(conn, config=_Cfg(), dry_run=False)
        assert payload["fresh"] is True
        assert payload["actionsEnabled"] is False
        assert payload["warnings"]
    finally:
        conn.close()


def test_wallet_payload_includes_redeemable_and_redemption_summary(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        snapshot = build_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            clob_balance_usd=100.0,
            chain_balance_usd=100.0,
            open_orders=[],
            data_api_trades=[],
            data_api_positions=[{
                "asset": "token-no",
                "conditionId": "0x" + "1" * 64,
                "size": 6.49,
                "currentValue": 6.49,
                "redeemable": True,
                "outcome": "No",
                "outcomeIndex": 1,
            }],
        )
        record_wallet_snapshot(conn, snapshot)
        conn.execute(
            """INSERT INTO redemption_requests
            (wallet_address, condition_id, token_id, outcome, outcome_index,
             index_set_value, negative_risk, size, current_value_usd, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _Cfg.poly_funder,
                "0x" + "1" * 64,
                "token-no",
                "NO",
                1,
                2,
                1,
                6.49,
                6.49,
                "SUBMITTED",
            ),
        )
        conn.commit()

        payload = wallet_dashboard_payload(conn, config=_Cfg(), dry_run=False)

        auto = payload["autoRedeem"]
        assert auto["enabled"] is True
        assert auto["intervalMinutes"] == 10
        assert snapshot.open_positions_count == 0
        assert snapshot.data_api_open_positions_count == 0
        assert auto["redeemablePositionsCount"] == 1
        assert auto["redeemableValueUsd"] == 6.49
        assert auto["submittedCount"] == 1
        assert auto["submittedValueUsd"] == 6.49
        assert auto["inFlightRedemptionCount"] == 1
        assert auto["inFlightRedemptionValueUsd"] == 6.49
        assert auto["recent"][0]["status"] == "SUBMITTED"
    finally:
        conn.close()


def test_zero_value_redeemable_positions_do_not_inflate_wallet_payload(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        snapshot = build_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            clob_balance_usd=100.0,
            chain_balance_usd=100.0,
            open_orders=[],
            data_api_trades=[],
            data_api_positions=[{
                "asset": "token-yes",
                "conditionId": "0x" + "1" * 64,
                "size": 29.97,
                "currentValue": 0,
                "curPrice": 0,
                "redeemable": True,
                "outcome": "Yes",
                "outcomeIndex": 0,
            }, {
                "asset": "token-dust",
                "conditionId": "0x" + "2" * 64,
                "size": 6.11,
                "currentValue": 0.003,
                "curPrice": 0.0005,
                "redeemable": True,
                "outcome": "No",
                "outcomeIndex": 1,
            }],
        )
        record_wallet_snapshot(conn, snapshot)

        payload = wallet_dashboard_payload(conn, config=_Cfg(), dry_run=False)

        assert snapshot.data_api_redeemable_value_usd == 0.0
        assert snapshot.open_positions_count == 0
        assert snapshot.data_api_open_positions_count == 0
        assert payload["snapshot"]["openPositionsCount"] == 0
        assert payload["autoRedeem"]["redeemablePositionsCount"] == 0
        assert payload["autoRedeem"]["redeemableValueUsd"] == 0.0
    finally:
        conn.close()


def test_data_api_position_match_drives_trusted_wallet_value(tmp_path):
    conn = init_db(tmp_path / "test.db")
    condition = "0x" + "1" * 64
    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, fill_ts, outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-05-22 08:00:00",
                "KDAL",
                condition,
                "token-no",
                "2026-05-22",
                1,
                75.0,
                "NO",
                0.30,
                0.80,
                0.10,
                5.0,
                5000.0,
                5.0,
                0.80,
                "ord_1",
                0.80,
                6.25,
                "2026-05-22 08:01:00",
                "PENDING",
                "bet",
            ),
        )
        conn.commit()

        snapshot = build_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            clob_balance_usd=95.0,
            chain_balance_usd=95.0,
            open_orders=[],
            data_api_trades=[],
            data_api_positions=[{
                "asset": "token-no",
                "conditionId": condition,
                "size": 6.25,
                "avgPrice": 0.80,
                "initialValue": 5.0,
                "currentValue": 6.25,
                "cashPnl": 1.25,
                "curPrice": 1.0,
                "outcome": "No",
            }],
        )

        assert snapshot.data_api_trusted_open_positions_value_usd == pytest.approx(6.25)
        assert snapshot.data_api_trusted_open_positions_cash_pnl_usd == pytest.approx(1.25)
        assert snapshot.data_api_matched_positions_count == 1
        assert snapshot.data_api_reconciliation_warnings == []
        position = next(r for r in snapshot.records if r.record_type == "position")
        assert position.match_status == "api_position_matched"
        assert position.record["ledgerMatch"]["trusted"] is True
    finally:
        conn.close()


def test_zero_value_resolved_position_matches_ledger_without_warning(tmp_path):
    conn = init_db(tmp_path / "test.db")
    condition = "0x" + "1" * 64
    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, fill_ts, outcome, pnl, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-05-22 08:00:00",
                "EFHK",
                condition,
                "token-no",
                "2026-05-22",
                1,
                17.0,
                "NO",
                0.30,
                0.80,
                0.10,
                5.0,
                5000.0,
                4.7385,
                0.81,
                "ord_1",
                0.81,
                5.85,
                "2026-05-22 08:01:00",
                "LOSS",
                -4.78351575,
                "bet",
            ),
        )
        conn.commit()

        snapshot = build_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            clob_balance_usd=95.0,
            chain_balance_usd=95.0,
            open_orders=[],
            data_api_trades=[],
            data_api_positions=[{
                "asset": "token-no",
                "conditionId": condition,
                "size": 5.85,
                "avgPrice": 0.81,
                "initialValue": 4.7385,
                "currentValue": 0,
                "cashPnl": -4.7385,
                "curPrice": 0,
                "redeemable": True,
                "outcome": "No",
            }],
        )

        assert snapshot.data_api_matched_positions_count == 1
        assert snapshot.data_api_unmatched_positions_count == 0
        assert snapshot.data_api_reconciliation_warnings == []
        assert snapshot.data_api_trusted_open_positions_initial_value_usd == 0.0
        assert snapshot.open_positions_count == 0
        assert snapshot.data_api_open_positions_count == 0
        position = next(r for r in snapshot.records if r.record_type == "position")
        assert position.matched_ledger_id is not None
        assert position.match_status == "api_position_resolved_matched"
        assert position.record["ledgerMatch"]["trusted"] is False
        assert position.record["ledgerMatch"]["reminder"] == ""
    finally:
        conn.close()


def test_data_api_released_position_drives_open_count_over_chain_tokens(tmp_path):
    conn = init_db(tmp_path / "test.db")
    condition = "0x" + "1" * 64
    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, fill_ts, outcome, pnl, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-05-22 08:00:00",
                "EFHK",
                condition,
                "token-no",
                "2026-05-22",
                1,
                17.0,
                "NO",
                0.30,
                0.80,
                0.10,
                5.0,
                5000.0,
                4.7385,
                0.81,
                "ord_1",
                0.81,
                5.85,
                "2026-05-22 08:01:00",
                "LOSS",
                -4.78351575,
                "bet",
            ),
        )
        conn.commit()

        snapshot = build_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            clob_balance_usd=95.0,
            chain_balance_usd=95.0,
            open_orders=[],
            data_api_trades=[],
            data_api_positions=[{
                "asset": "token-no",
                "conditionId": condition,
                "size": 5.85,
                "avgPrice": 0.81,
                "initialValue": 4.7385,
                "currentValue": 0,
                "cashPnl": -4.7385,
                "curPrice": 0,
                "redeemable": True,
                "outcome": "No",
            }],
            chain_open_positions_count=1,
        )

        assert snapshot.chain_open_positions_count == 1
        assert snapshot.data_api_open_positions_count == 0
        assert snapshot.open_positions_count == 0
        assert snapshot.warnings == []
        assert any(
            "Data API released/open state drives the dashboard count" in warning
            for warning in snapshot.data_api_reconciliation_warnings
        )
    finally:
        conn.close()


def test_data_api_position_mismatch_is_reminder_not_trusted_capital(tmp_path):
    conn = init_db(tmp_path / "test.db")
    condition = "0x" + "2" * 64
    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, fill_ts, outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-05-22 08:00:00",
                "KDAL",
                condition,
                "token-no",
                "2026-05-22",
                1,
                75.0,
                "NO",
                0.30,
                0.80,
                0.10,
                5.0,
                5000.0,
                5.0,
                0.80,
                "ord_1",
                0.80,
                6.25,
                "2026-05-22 08:01:00",
                "PENDING",
                "bet",
            ),
        )
        conn.commit()

        snapshot = build_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            clob_balance_usd=95.0,
            chain_balance_usd=95.0,
            open_orders=[],
            data_api_trades=[],
            data_api_positions=[{
                "asset": "token-no",
                "conditionId": condition,
                "size": 12.0,
                "avgPrice": 0.75,
                "initialValue": 9.0,
                "currentValue": 10.0,
                "cashPnl": 1.0,
                "curPrice": 0.8333,
                "outcome": "No",
            }],
        )

        assert snapshot.data_api_trusted_open_positions_value_usd == 0.0
        assert snapshot.data_api_mismatched_positions_count == 1
        assert snapshot.data_api_reconciliation_warnings
        position = next(r for r in snapshot.records if r.record_type == "position")
        assert position.match_status == "api_position_mismatch"
        assert position.record["ledgerMatch"]["reminder"] == "API/ledger value mismatch"
    finally:
        conn.close()


def test_incomplete_wallet_snapshot_disables_live_actions(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        record_wallet_snapshot(
            conn,
            build_wallet_snapshot(
                conn,
                wallet_address=_Cfg.poly_funder,
                clob_balance_usd=100.0,
                chain_balance_usd=100.0,
            ),
        )

        payload = wallet_dashboard_payload(conn, config=_Cfg(), dry_run=False)

        assert payload["snapshot"]["complete"] is False
        assert payload["actionsEnabled"] is False
        assert any("open orders" in warning for warning in payload["warnings"])
    finally:
        conn.close()


def test_refresh_wallet_snapshot_marks_complete_only_after_live_sources_checked(tmp_path):
    conn = init_db(tmp_path / "test.db")

    class _OrderClient:
        def __init__(self, _cfg):
            self._client = self

        def check_balance(self):
            return 100.0

        def get_open_orders(self):
            return []

    try:
        snapshot = refresh_wallet_snapshot(
            conn,
            config=_Cfg(),
            order_client_factory=_OrderClient,
            data_api_fetcher=lambda _wallet: ([], []),
            chain_balance_reader=lambda **_kwargs: 100.0,
        )

        assert snapshot.complete is True
        assert snapshot.warnings == []
        payload = wallet_dashboard_payload(conn, config=_Cfg(), dry_run=False)
        assert payload["actionsEnabled"] is True
    finally:
        conn.close()


def test_refresh_wallet_snapshot_resyncs_clob_balance_when_chain_is_newer(tmp_path):
    conn = init_db(tmp_path / "test.db")
    client_holder = {}

    class _OrderClient:
        def __init__(self, _cfg):
            self._client = self
            self.reads = 0
            self.synced = False

        def check_balance(self):
            self.reads += 1
            return 100.0 if not self.synced else 105.93

        def sync_collateral_balance(self):
            self.synced = True
            return True

        def get_open_orders(self):
            return []

    def _factory(cfg):
        client = _OrderClient(cfg)
        client_holder["client"] = client
        return client

    try:
        snapshot = refresh_wallet_snapshot(
            conn,
            config=_Cfg(),
            order_client_factory=_factory,
            data_api_fetcher=lambda _wallet: ([], []),
            chain_balance_reader=lambda **_kwargs: 105.93,
        )

        assert client_holder["client"].synced is True
        assert client_holder["client"].reads == 2
        assert snapshot.clob_balance_usd == pytest.approx(105.93)
        assert snapshot.chain_balance_usd == pytest.approx(105.93)
        assert snapshot.complete is True
        assert snapshot.warnings == []
    finally:
        conn.close()


def test_open_wallet_position_allows_transfer_of_free_cash(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        condition = "0x" + "8" * 64
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-05-21 00:00:00",
                "KDAL",
                condition,
                "token",
                "2026-05-21",
                1,
                75.0,
                "YES",
                0.5,
                0.5,
                0.1,
                0.5,
                5000.0,
                0.5,
                0.5,
                "ord-open",
                0.5,
                1.0,
                "PENDING",
                "bet",
            ),
        )
        conn.commit()
        record_wallet_snapshot(
            conn,
            build_wallet_snapshot(
                conn,
                wallet_address=_Cfg.poly_funder,
                clob_balance_usd=100.0,
                chain_balance_usd=100.0,
                data_api_trades=[],
                data_api_positions=[{
                    "asset": "token",
                    "conditionId": condition,
                    "size": "1",
                    "avgPrice": "0.50",
                    "initialValue": "0.50",
                    "currentValue": "0.50",
                    "curPrice": "0.50",
                    "outcome": "Yes",
                }],
                open_orders=[],
                chain_balance_required=True,
            ),
        )

        payload = wallet_dashboard_payload(conn, config=_Cfg(), dry_run=False)

        assert payload["actionsEnabled"] is True
        assert payload["transferEligible"] is True
        assert payload["transferBlockedReason"] == ""
        assert payload["snapshot"]["openPositionsCount"] == 1
    finally:
        conn.close()


def test_unmatched_open_wallet_position_blocks_transfer_but_not_start_actions(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        record_wallet_snapshot(
            conn,
            build_wallet_snapshot(
                conn,
                wallet_address=_Cfg.poly_funder,
                clob_balance_usd=100.0,
                chain_balance_usd=100.0,
                data_api_trades=[],
                data_api_positions=[{
                    "asset": "orphan-token",
                    "conditionId": "0x" + "9" * 64,
                    "size": "2",
                    "avgPrice": "0.50",
                    "initialValue": "1.00",
                    "currentValue": "1.20",
                    "curPrice": "0.60",
                    "outcome": "No",
                }],
                open_orders=[],
                chain_balance_required=True,
            ),
        )

        payload = wallet_dashboard_payload(conn, config=_Cfg(), dry_run=False)

        assert payload["actionsEnabled"] is True
        assert payload["transferEligible"] is False
        assert payload["transferBlockingWarnings"]
        assert "no matching local ledger row" in payload["transferBlockedReason"]
    finally:
        conn.close()


def test_live_actions_require_fresh_readiness_report(tmp_path):
    conn = init_db(tmp_path / "test.db")
    cfg = Config(
        _env_file=None,
        dry_run=False,
        poly_funder=_Cfg.poly_funder,
        wallet_snapshot_freshness_ttl_s=300,
        operator_action_freshness_ttl_s=300,
    )
    set_config(cfg)
    try:
        record_wallet_snapshot(
            conn,
            build_wallet_snapshot(
                conn,
                wallet_address=_Cfg.poly_funder,
                clob_balance_usd=100.0,
                chain_balance_usd=100.0,
            ),
        )
        record_readiness_report(
            conn,
            ReadinessReport(
                status="OK",
                mode="LIVE",
                generated_at="2026-05-21 00:00:00",
                expires_at="2026-05-21 00:01:00",
                signature_type=3,
                funder=_Cfg.poly_funder,
            ),
        )

        payload = build_operator_wallet_payload(conn, dry_run=False)

        assert payload["readiness"]["status"] == "OK"
        assert payload["readiness"]["fresh"] is False
        assert payload["liveActionsEnabled"] is False
    finally:
        set_config(None)
        conn.close()


# --- state-dedup persistence (operator directive 2026-08-09) -----------------
# The audit trail must stop growing: consecutive refreshes differ only in
# sampledAt, so an unchanged state refreshes the newest row in place and the
# write-only records table is no longer written at all.


def _one_record_snapshot(conn, *, clob_balance_usd=100.0, chain_balance_usd=100.0):
    return build_wallet_snapshot(
        conn,
        wallet_address=_Cfg.poly_funder,
        clob_balance_usd=clob_balance_usd,
        chain_balance_usd=chain_balance_usd,
        open_orders=[],
        data_api_trades=[{
            "transactionHash": "0xabc",
            "asset": "tok",
            "size": "10",
            "price": "0.50",
            "side": "BUY",
        }],
        data_api_positions=[],
    )


def _run_rows(conn):
    return conn.execute(
        "SELECT id, sampled_at, snapshot_json FROM wallet_reconciliation_runs ORDER BY id"
    ).fetchall()


def _record_row_count(conn):
    return int(
        conn.execute("SELECT COUNT(*) FROM wallet_reconciliation_records").fetchone()[0]
    )


def test_unchanged_state_refreshes_single_run_row_and_writes_no_records(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        snapshot = _one_record_snapshot(conn)
        later_ts = (
            datetime.now(timezone.utc) + timedelta(seconds=600)
        ).strftime("%Y-%m-%d %H:%M:%S")

        first_id = record_wallet_snapshot(conn, snapshot)
        second_id = record_wallet_snapshot(conn, replace(snapshot, sampled_at=later_ts))

        assert second_id == first_id
        rows = _run_rows(conn)
        assert len(rows) == 1
        assert rows[0]["sampled_at"] == later_ts
        assert rows[0]["sampled_at"] > snapshot.sampled_at
        # snapshot_json carries the FULL new payload, so the inner sampledAt the
        # Operator panel renders tracks the column.
        assert json.loads(rows[0]["snapshot_json"])["sampledAt"] == later_ts
        # Write-only audit records are no longer persisted at all.
        assert _record_row_count(conn) == 0
    finally:
        conn.close()


def test_changed_balance_inserts_a_second_run_row(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        first_id = record_wallet_snapshot(conn, _one_record_snapshot(conn))
        second_id = record_wallet_snapshot(
            conn,
            _one_record_snapshot(conn, clob_balance_usd=90.0, chain_balance_usd=90.0),
        )

        assert second_id != first_id
        rows = _run_rows(conn)
        assert len(rows) == 2
        assert json.loads(rows[0]["snapshot_json"])["clobBalanceUsd"] == 100.0
        assert json.loads(rows[1]["snapshot_json"])["clobBalanceUsd"] == 90.0
        assert _record_row_count(conn) == 0
    finally:
        conn.close()


def test_changed_record_match_status_inserts_a_second_run_row(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        snapshot = _one_record_snapshot(conn)
        assert snapshot.records[0].match_status == "orphan_wallet_trade"

        first_id = record_wallet_snapshot(conn, snapshot)
        matched = replace(
            snapshot,
            sampled_at=(
                datetime.now(timezone.utc) + timedelta(seconds=600)
            ).strftime("%Y-%m-%d %H:%M:%S"),
            records=[replace(snapshot.records[0], match_status="exact", matched_ledger_id=7)],
        )
        second_id = record_wallet_snapshot(conn, matched)

        assert second_id != first_id
        rows = _run_rows(conn)
        assert len(rows) == 2
        assert json.loads(rows[0]["snapshot_json"])["records"][0]["match_status"] == (
            "orphan_wallet_trade"
        )
        assert json.loads(rows[1]["snapshot_json"])["records"][0]["match_status"] == "exact"
    finally:
        conn.close()


def test_latest_wallet_snapshot_is_fresh_after_in_place_refresh(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        now = datetime.now(timezone.utc)
        stale_ts = (now - timedelta(seconds=3600)).strftime("%Y-%m-%d %H:%M:%S")
        fresh_ts = now.strftime("%Y-%m-%d %H:%M:%S")
        snapshot = _one_record_snapshot(conn)

        run_id = record_wallet_snapshot(conn, replace(snapshot, sampled_at=stale_ts))
        stale = latest_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            freshness_ttl_s=300,
        )
        assert stale["fresh"] is False
        assert stale["sampledAt"] == stale_ts

        refreshed_id = record_wallet_snapshot(conn, replace(snapshot, sampled_at=fresh_ts))
        assert refreshed_id == run_id

        latest = latest_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            freshness_ttl_s=300,
        )
        assert latest["fresh"] is True
        assert latest["sampledAt"] == fresh_ts
        assert latest["ageSeconds"] < 300
        assert len(_run_rows(conn)) == 1
    finally:
        conn.close()


@pytest.mark.parametrize("corrupt_json", ["{not json", "[]", "null", ""])
def test_corrupt_previous_snapshot_json_falls_through_to_insert(tmp_path, corrupt_json):
    conn = init_db(tmp_path / "test.db")
    try:
        snapshot = _one_record_snapshot(conn)
        first_id = record_wallet_snapshot(conn, snapshot)
        conn.execute(
            "UPDATE wallet_reconciliation_runs SET snapshot_json = ? WHERE id = ?",
            (corrupt_json, first_id),
        )
        conn.commit()

        later_ts = (
            datetime.now(timezone.utc) + timedelta(seconds=600)
        ).strftime("%Y-%m-%d %H:%M:%S")
        second_id = record_wallet_snapshot(conn, replace(snapshot, sampled_at=later_ts))

        assert second_id != first_id
        rows = _run_rows(conn)
        assert len(rows) == 2
        assert rows[0]["snapshot_json"] == corrupt_json
        assert json.loads(rows[1]["snapshot_json"])["sampledAt"] == later_ts

        latest = latest_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            freshness_ttl_s=300,
        )
        assert latest["sampledAt"] == later_ts
    finally:
        conn.close()
