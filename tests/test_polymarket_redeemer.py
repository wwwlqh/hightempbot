from __future__ import annotations

import json

import pytest

from hightempbot.db.connection import init_db
from hightempbot.execution.polymarket_redeemer import (
    latest_redemption_summary,
    run_redeemable_scan,
)
from hightempbot.persistence.ledger import poly_fee_charge
from hightempbot.runtime_config import Config


WALLET = "0x" + "d" * 40
CONDITION = "0x" + "1" * 64
CONDITION_2 = "0x" + "2" * 64


def _cfg(**overrides):
    base = dict(
        _env_file=None,
        dry_run=False,
        poly_funder=WALLET,
        poly_private_key="0x" + "b" * 64,
        relayer_api_key="relayer-key",
        relayer_api_key_address="0x" + "a" * 40,
        relayer_url="https://relayer.test",
    )
    base.update(overrides)
    return Config(**base)


def _insert_bet(
    conn,
    *,
    token_id="token-no",
    side="NO",
    bet_size=4.0,
    fill_price=0.4,
    fill_size=10.0,
    outcome="PENDING",
):
    cur = conn.execute(
        """INSERT INTO ledger
        (bet_ts, station_id, market_id, token_id, target_date,
         horizon, threshold, side, p_model, p_market, edge,
         kelly_size, volume_cap, bet_size, limit_price,
         order_id, fill_price, fill_size, fill_ts, outcome, event_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "2026-05-22 05:00:00",
            "KDAL",
            CONDITION,
            token_id,
            "2026-05-21",
            1,
            75.0,
            side,
            0.70,
            fill_price if fill_price is not None else 0.4,
            0.10,
            bet_size,
            5000.0,
            bet_size,
            fill_price if fill_price is not None else 0.4,
            "order-" + token_id + "-" + str(fill_size),
            fill_price,
            fill_size,
            "2026-05-22 05:01:00",
            outcome,
            "bet",
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _position(**overrides):
    base = {
        "asset": "token-no",
        "conditionId": CONDITION,
        "size": 10.0,
        "currentValue": 10.0,
        "redeemable": True,
        "outcome": "No",
        "outcomeIndex": 1,
        "negativeRisk": True,
        "title": "Will the highest temperature in Dallas be 75F or below on May 21?",
        "slug": "highest-temperature-in-dallas-on-may-21-2026-75forbelow",
        "eventSlug": "highest-temperature-in-dallas-on-may-21-2026",
    }
    base.update(overrides)
    return base


def test_redeemable_no_settles_pending_and_submits_once(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        bet_id = _insert_bet(conn)
        calls = []

        def _submitter(**kwargs):
            calls.append(kwargs)
            return {"transactionID": "relay-1"}

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position()],
            submitter=_submitter,
        )

        assert result.status == "OK"
        assert result.settled_rows == 1
        assert result.submitted == 1
        assert calls[0]["index_sets"] == [2]
        assert calls[0]["negative_risk"] is True

        row = conn.execute("SELECT outcome, pnl, event_detail FROM ledger WHERE id=?", (bet_id,)).fetchone()
        assert row["outcome"] == "WIN"
        assert row["pnl"] == pytest.approx(10.0 - 4.0 - poly_fee_charge(0.4, 10.0))
        detail = json.loads(row["event_detail"])
        assert detail["resolution_source"] == "polymarket_data_api_redeemable"
        assert detail["resolution_price"] == 1.0
        assert "resolution_actual_label" not in detail
        assert detail["redeemable_title"].startswith("Will the highest temperature")

        req = conn.execute("SELECT status, index_set_value, relayer_tx_id FROM redemption_requests").fetchone()
        assert dict(req) == {
            "status": "SUBMITTED",
            "index_set_value": 2,
            "relayer_tx_id": "relay-1",
        }
    finally:
        conn.close()


def test_redeemable_notification_names_submitted_row(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        bet_id = _insert_bet(conn)
        alerts = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position()],
            submitter=lambda **_kwargs: {
                "transactionID": "relay-1",
                "transactionHash": "0x" + "c" * 64,
            },
            notify=lambda title, body: alerts.append((title, body)),
        )

        assert result.submitted == 1
        assert len(alerts) == 1
        title, body = alerts[0]
        assert title == "Polymarket auto-redeem"
        assert "scanned=1 redeemable=1 settled_rows=1 submitted=1" in body
        assert "Submitted:" in body
        assert "request=1" in body
        assert "payout=$10.00" in body
        assert "tx=0xcccccc...cccccc" in body
        assert f"row={bet_id} KDAL 2026-05-21 NO WIN" in body
        assert "pnl=+$" in body
    finally:
        conn.close()


def test_redeemable_yes_uses_index_set_one(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _insert_bet(conn, token_id="token-yes", side="YES", bet_size=2.5, fill_price=0.25, fill_size=10)
        calls = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position(
                asset="token-yes",
                outcome="Yes",
                outcomeIndex=0,
            )],
            submitter=lambda **kwargs: calls.append(kwargs) or {"transactionID": "relay-yes"},
        )

        assert result.submitted == 1
        assert calls[0]["index_sets"] == [1]
    finally:
        conn.close()


def test_zero_value_redeemable_position_settles_loss_without_burn(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        bet_id = _insert_bet(conn, token_id="token-yes", side="YES", bet_size=2.5, fill_price=0.25, fill_size=10)
        calls = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position(
                asset="token-yes",
                outcome="Yes",
                outcomeIndex=0,
                currentValue=0,
                curPrice=0,
            )],
            submitter=lambda **kwargs: calls.append(kwargs) or {"transactionID": "unexpected"},
        )

        assert result.redeemable == 0
        assert result.zero_payout == 1
        assert result.settled_rows == 1
        assert result.settled_losses == 1
        assert result.submitted == 0
        assert calls == []
        row = conn.execute(
            "SELECT outcome, pnl, event_detail FROM ledger WHERE id=?", (bet_id,),
        ).fetchone()
        assert row["outcome"] == "LOSS"
        assert row["pnl"] == pytest.approx(-2.5 - poly_fee_charge(0.25, 10.0))
        detail = json.loads(row["event_detail"])
        assert detail["resolution_source"] == "polymarket_data_api_zero_payout_redeemable"
        assert detail["resolution_price"] == 0.0
        assert detail["zero_payout_current_value_usd"] == 0.0
        assert conn.execute("SELECT COUNT(*) AS n FROM redemption_requests").fetchone()["n"] == 0
    finally:
        conn.close()


def test_low_value_redeemable_position_settles_loss_without_burn(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        bet_id = _insert_bet(conn)
        calls = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position(
                size=10,
                currentValue=0.005,
                curPrice=0.0005,
            )],
            submitter=lambda **kwargs: calls.append(kwargs) or {"transactionID": "unexpected"},
        )

        assert result.redeemable == 0
        assert result.zero_payout == 1
        assert result.settled_rows == 1
        assert result.settled_losses == 1
        assert result.submitted == 0
        assert calls == []
        row = conn.execute("SELECT outcome FROM ledger WHERE id=?", (bet_id,)).fetchone()
        assert row["outcome"] == "LOSS"
    finally:
        conn.close()


def test_zero_payout_loss_settlement_notifies_operator(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        bet_id = _insert_bet(conn, token_id="token-yes", side="YES", bet_size=2.5, fill_price=0.25, fill_size=10)
        alerts = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position(
                asset="token-yes",
                outcome="Yes",
                outcomeIndex=0,
                currentValue=0,
                curPrice=0,
            )],
            submitter=lambda **_kwargs: {"transactionID": "unexpected"},
            notify=lambda title, body: alerts.append((title, body)),
        )

        assert result.submitted == 0
        assert result.settled_losses == 1
        assert len(alerts) == 1
        title, body = alerts[0]
        assert title == "Polymarket auto-redeem"
        assert "zero_payout=1" in body
        assert "settled_losses=1" in body
        assert "Settled losses:" in body
        assert f"row={bet_id} KDAL 2026-05-21 YES LOSS" in body
        assert "Submitted:" not in body
    finally:
        conn.close()


def test_already_settled_zero_payout_loss_is_ignored_in_scan_count(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _insert_bet(
            conn,
            token_id="token-loss",
            side="YES",
            bet_size=2.5,
            fill_price=0.25,
            fill_size=10,
            outcome="LOSS",
        )
        calls = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [
                _position(asset="token-open", redeemable=False),
                _position(
                    asset="token-loss",
                    outcome="Yes",
                    outcomeIndex=0,
                    currentValue=0,
                    curPrice=0,
                ),
            ],
            submitter=lambda **kwargs: calls.append(kwargs) or {"transactionID": "unexpected"},
        )

        assert result.scanned == 1
        assert result.zero_payout == 0
        assert result.ignored_zero_payout == 1
        assert result.settled_losses == 0
        assert result.submitted == 0
        assert calls == []
        health = conn.execute(
            "SELECT message FROM pipeline_health WHERE stage='auto_redeem' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert "scanned=1 " in health["message"]
        assert "zero_payout=0" in health["message"]
    finally:
        conn.close()


def test_multiple_pending_rows_for_same_token_are_settled_but_redeemed_once(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        first = _insert_bet(conn, token_id="token-no", bet_size=3.2, fill_price=0.4, fill_size=8)
        second = _insert_bet(conn, token_id="token-no", bet_size=0.8, fill_price=0.4, fill_size=2)
        calls = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position(size=10, currentValue=10)],
            submitter=lambda **kwargs: calls.append(kwargs) or {"transactionID": "relay-merged"},
        )

        assert result.settled_rows == 2
        assert result.submitted == 1
        outcomes = conn.execute(
            "SELECT id, outcome FROM ledger WHERE id IN (?, ?) ORDER BY id",
            (first, second),
        ).fetchall()
        assert [row["outcome"] for row in outcomes] == ["WIN", "WIN"]
        assert len(calls) == 1
    finally:
        conn.close()


def test_size_mismatch_blocks_manual_handling_without_settlement_or_submit(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        bet_id = _insert_bet(conn, token_id="token-no", bet_size=4.0, fill_price=0.4, fill_size=10.0)
        calls = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position(size=12.0, currentValue=12.0)],
            submitter=lambda **kwargs: calls.append(kwargs) or {"transactionID": "unexpected"},
        )

        assert result.status == "WARNING"
        assert result.redeemable == 1
        assert result.blocked_manual == 1
        assert result.settled_rows == 0
        assert result.submitted == 0
        assert result.failed == 0
        assert "blocked_manual=1" in result.message
        assert calls == []

        row = conn.execute("SELECT outcome, pnl FROM ledger WHERE id=?", (bet_id,)).fetchone()
        assert row["outcome"] == "PENDING"
        assert row["pnl"] is None
        assert conn.execute("SELECT COUNT(*) AS n FROM redemption_requests").fetchone()["n"] == 0
        health = conn.execute(
            "SELECT status, message FROM pipeline_health WHERE stage='auto_redeem' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert health["status"] == "WARNING"
        assert "blocked_manual=1" in health["message"]
    finally:
        conn.close()


def test_size_match_allows_tiny_data_api_rounding_difference(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _insert_bet(conn, token_id="token-no", bet_size=4.0, fill_price=0.4, fill_size=10.0)
        calls = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position(size=10.000001, currentValue=10.000001)],
            submitter=lambda **kwargs: calls.append(kwargs) or {"transactionID": "relay-rounding"},
        )

        assert result.status == "OK"
        assert result.blocked_manual == 0
        assert result.settled_rows == 1
        assert result.submitted == 1
        assert len(calls) == 1
    finally:
        conn.close()


def test_size_match_allows_data_api_two_decimal_rounding_difference(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _insert_bet(
            conn,
            token_id="token-no",
            bet_size=9.009998999999999,
            fill_price=0.8458785618673396,
            fill_size=10.651646,
        )
        calls = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position(size=10.6516, currentValue=10.6516)],
            submitter=lambda **kwargs: calls.append(kwargs) or {"transactionID": "relay-rounded"},
        )

        assert result.status == "OK"
        assert result.blocked_manual == 0
        assert result.settled_rows == 1
        assert result.submitted == 1
        assert len(calls) == 1
    finally:
        conn.close()


def test_non_redeemable_and_unmatched_positions_do_not_submit(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _insert_bet(conn)
        calls = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [
                _position(redeemable=False),
                _position(asset="unknown-token"),
            ],
            submitter=lambda **kwargs: calls.append(kwargs) or {"transactionID": "unexpected"},
        )

        assert result.redeemable == 1
        assert result.skipped_unmatched == 1
        assert result.submitted == 0
        assert calls == []
        assert conn.execute("SELECT COUNT(*) AS n FROM redemption_requests").fetchone()["n"] == 0
    finally:
        conn.close()


def test_pending_without_fill_metadata_is_not_redeemed(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        bet_id = _insert_bet(conn, fill_price=None, fill_size=None)
        calls = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position()],
            submitter=lambda **kwargs: calls.append(kwargs) or {"transactionID": "unexpected"},
        )

        assert result.failed == 1
        assert result.submitted == 0
        assert calls == []
        row = conn.execute("SELECT outcome FROM ledger WHERE id=?", (bet_id,)).fetchone()
        assert row["outcome"] == "PENDING"
    finally:
        conn.close()


def test_repeated_scan_does_not_double_submit(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _insert_bet(conn)
        calls = []
        submitter = lambda **kwargs: calls.append(kwargs) or {"transactionID": f"relay-{len(calls)}"}

        first = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position()],
            submitter=submitter,
        )
        second = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position()],
            submitter=submitter,
        )

        assert first.submitted == 1
        assert second.submitted == 0
        assert second.skipped_existing == 1
        assert len(calls) == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM redemption_requests").fetchone()["n"] == 1
    finally:
        conn.close()


def test_scan_defers_extra_submits_to_avoid_wallet_busy(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _insert_bet(conn, token_id="token-no-1")
        _insert_bet(conn, token_id="token-no-2")
        conn.execute(
            "UPDATE ledger SET market_id=? WHERE token_id=?",
            (CONDITION_2, "token-no-2"),
        )
        conn.commit()
        calls = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [
                _position(asset="token-no-1"),
                _position(asset="token-no-2", conditionId=CONDITION_2),
            ],
            submitter=lambda **kwargs: calls.append(kwargs) or {"transactionID": "relay-1"},
        )

        assert result.settled_rows == 2
        assert result.submitted == 1
        assert result.deferred == 1
        assert result.failed == 0
        assert len(calls) == 1
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM redemption_requests"
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_failed_redemption_can_retry(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _insert_bet(conn)

        def _fail(**_kwargs):
            raise RuntimeError("relayer down")

        failed = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position()],
            submitter=_fail,
        )
        retried = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position()],
            submitter=lambda **_kwargs: {"transactionID": "relay-retry"},
        )

        assert failed.failed == 1
        assert retried.submitted == 1
        rows = conn.execute(
            "SELECT status FROM redemption_requests ORDER BY id"
        ).fetchall()
        assert [row["status"] for row in rows] == ["FAILED", "SUBMITTED"]
    finally:
        conn.close()


def test_stale_submitting_redemption_is_recovered_and_retried(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _insert_bet(conn)
        conn.execute(
            """INSERT INTO redemption_requests
            (created_at, updated_at, wallet_address, condition_id, token_id,
             outcome, outcome_index, index_set_value, negative_risk, size,
             current_value_usd, status, matched_ledger_ids_json)
            VALUES (?, ?, ?, ?, ?, 'NO', 1, 2, 1, 10.0, 10.0, 'SUBMITTING', ?)""",
            (
                "2026-01-01 00:00:00",
                "2026-01-01 00:00:00",
                WALLET,
                CONDITION,
                "token-no",
                json.dumps([1]),
            ),
        )
        conn.commit()
        calls = []

        result = run_redeemable_scan(
            conn,
            config=_cfg(),
            positions_fetcher=lambda _wallet: [_position()],
            submitter=lambda **kwargs: calls.append(kwargs) or {"transactionID": "relay-retry"},
        )

        assert result.submitted == 1
        assert len(calls) == 1
        rows = conn.execute(
            "SELECT status, error FROM redemption_requests ORDER BY id"
        ).fetchall()
        assert [row["status"] for row in rows] == ["FAILED", "SUBMITTED"]
        assert "Stale SUBMITTING redemption" in rows[0]["error"]
    finally:
        conn.close()


def test_redemption_summary_collapses_retry_failures(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        attempts = [
            ("token-a", CONDITION, "FAILED"),
            ("token-b", CONDITION_2, "FAILED"),
            ("token-a", CONDITION, "SUBMITTED"),
            ("token-b", CONDITION_2, "SUBMITTED"),
            ("token-c", "0x" + "3" * 64, "SUBMITTED"),
        ]
        for token_id, condition_id, status in attempts:
            conn.execute(
                """INSERT INTO redemption_requests
                (wallet_address, condition_id, token_id, outcome, outcome_index,
                 index_set_value, negative_risk, size, current_value_usd, status)
                VALUES (?, ?, ?, 'NO', 1, 2, 1, 1.0, 1.0, ?)""",
                (WALLET, condition_id, token_id, status),
            )
        conn.commit()

        summary = latest_redemption_summary(conn, wallet_address=WALLET, limit=5)

        assert summary["submittedCount"] == 3
        assert summary["failedCount"] == 0
        assert summary["zeroPayoutCount"] == 0
        assert summary["submittedValueUsd"] == pytest.approx(3.0)
        assert summary["inFlightRedemptionCount"] == 3
        assert summary["inFlightRedemptionValueUsd"] == pytest.approx(3.0)
        assert [row["tokenId"] for row in summary["recent"]] == [
            "token-c",
            "token-b",
            "token-a",
        ]
        assert {row["status"] for row in summary["recent"]} == {"SUBMITTED"}
    finally:
        conn.close()


def test_redemption_summary_does_not_fallback_to_size_for_zero_value(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        conn.execute(
            """INSERT INTO redemption_requests
            (wallet_address, condition_id, token_id, outcome, outcome_index,
             index_set_value, negative_risk, size, current_value_usd, status)
            VALUES (?, ?, ?, 'YES', 0, 1, 1, 29.97, 0.0, 'SUBMITTED')""",
            (WALLET, CONDITION, "token-zero"),
        )
        conn.commit()

        summary = latest_redemption_summary(conn, wallet_address=WALLET)

        assert summary["submittedCount"] == 0
        assert summary["zeroPayoutCount"] == 1
        assert summary["submittedValueUsd"] == 0.0
        assert summary["inFlightRedemptionCount"] == 0
        assert summary["inFlightRedemptionValueUsd"] == 0.0
        assert summary["recent"][0]["currentValueUsd"] == 0.0
        assert summary["recent"][0]["status"] == "ZERO_PAYOUT"
        assert summary["recent"][0]["rawStatus"] == "SUBMITTED"
    finally:
        conn.close()
