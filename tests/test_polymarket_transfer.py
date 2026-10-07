from __future__ import annotations

import pytest

from hightempbot.db.connection import init_db
from hightempbot.execution.capital import return_transfer_notional
from hightempbot.execution.operator_control import enter_transfer_lock, get_operator_state
from hightempbot.execution.polymarket_transfer import (
    TransferSafetyError,
    preview_return_transfer,
    submit_return_transfer,
)
from hightempbot.persistence.wallet_reconciliation import (
    build_wallet_snapshot,
    record_wallet_snapshot,
)


class _Secret:
    def __init__(self, value):
        self.value = value

    def get_secret_value(self):
        return self.value


class _Cfg:
    poly_funder = "0x" + "d" * 40
    poly_return_wallet = "0x" + "e" * 40
    wallet_snapshot_freshness_ttl_s = 300
    relayer_url = "https://relayer.test"
    relayer_api_key = _Secret("relayer_key")
    relayer_api_key_address = "0x" + "a" * 40
    poly_private_key = _Secret("0x" + "a" * 64)


def _fresh_wallet(conn, *, positions=None, open_orders=None):
    snapshot = build_wallet_snapshot(
        conn,
        wallet_address=_Cfg.poly_funder,
        clob_balance_usd=100.0,
        chain_balance_usd=100.0,
        data_api_trades=[],
        data_api_positions=list(positions or []),
        open_orders=list(open_orders or []),
    )
    record_wallet_snapshot(conn, snapshot)


def test_preview_refuses_when_live_pending_exists(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, event_type)
            VALUES ('2026-05-21 00:00:00', 'KDAL', 'm', 't', '2026-05-21',
             1, 1, 'YES', 0.5, 0.4, 0.1, 1, 1, 5, 0.4, 'PENDING', 'bet')"""
        )
        conn.commit()

        preview = preview_return_transfer(
            conn,
            config=_Cfg(),
            amount="10",
            dry_run=False,
        )

        assert preview.ok is False
        assert any("in-flight" in err for err in preview.errors)
    finally:
        conn.close()


def test_preview_allows_submitted_pending_and_open_positions(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, outcome, event_type)
            VALUES ('2026-05-21 00:00:00', 'KDAL', 'm', 't', '2026-05-21',
             1, 1, 'YES', 0.5, 0.4, 0.1, 1, 1, 5, 0.4,
             'ord-open', 0.5, 10.0, 'PENDING', 'bet')"""
        )
        conn.commit()
        _fresh_wallet(
            conn,
            positions=[{
                "asset": "t",
                "conditionId": "m",
                "size": "10",
                "avgPrice": "0.50",
                "initialValue": "5",
                "currentValue": "2.50",
                "curPrice": "0.25",
                "outcome": "Yes",
            }],
        )

        preview = preview_return_transfer(
            conn,
            config=_Cfg(),
            amount="10usd",
            dry_run=False,
        )

        assert preview.ok is True
        assert preview.amount_base_units == 10_000_000
        assert any("free pUSD only" in warning for warning in preview.warnings)
    finally:
        conn.close()


def test_preview_blocks_transfer_blocking_data_api_position_warning(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(
            conn,
            positions=[{
                "asset": "orphan-token",
                "conditionId": "0x" + "9" * 64,
                "size": "2",
                "avgPrice": "0.50",
                "initialValue": "1.00",
                "currentValue": "1.20",
                "curPrice": "0.60",
                "outcome": "No",
            }],
        )

        preview = preview_return_transfer(
            conn,
            config=_Cfg(),
            amount="10",
            dry_run=False,
        )

        assert preview.ok is False
        assert any("transfer-blocking Data API" in err for err in preview.errors)
        assert any("no matching local ledger row" in warning for warning in preview.warnings)
    finally:
        conn.close()


def test_preview_passes_with_fresh_wallet_and_no_exposure(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)

        preview = preview_return_transfer(
            conn,
            config=_Cfg(),
            amount="10.1234567",
            dry_run=False,
        )

        assert preview.ok is True
        assert preview.amount_base_units == 10_123_456
        assert preview.confirmation.startswith("TRANSFER 10.123456 PUSD")
    finally:
        conn.close()


def test_preview_omits_confirmation_when_return_wallet_missing(tmp_path):
    class MissingReturnWallet(_Cfg):
        poly_return_wallet = ""

    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)

        preview = preview_return_transfer(
            conn,
            config=MissingReturnWallet(),
            amount="10",
            dry_run=False,
        )

        assert preview.ok is False
        assert preview.confirmation == ""
        assert any("POLY_RETURN_WALLET is not configured" in err for err in preview.errors)
    finally:
        conn.close()


def test_preview_refuses_incomplete_wallet_snapshot(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        snapshot = build_wallet_snapshot(
            conn,
            wallet_address=_Cfg.poly_funder,
            clob_balance_usd=100.0,
            chain_balance_usd=100.0,
        )
        record_wallet_snapshot(conn, snapshot)

        preview = preview_return_transfer(
            conn,
            config=_Cfg(),
            amount="10",
            dry_run=False,
        )

        assert preview.ok is False
        assert any("incomplete" in err for err in preview.errors)
    finally:
        conn.close()


def test_submit_requires_transfer_lock(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        get_operator_state(conn, boot_dry_run=False)
        preview = preview_return_transfer(conn, config=_Cfg(), amount="10", dry_run=False)

        with pytest.raises(TransferSafetyError, match="TRANSFER_LOCK"):
            submit_return_transfer(
                conn,
                config=_Cfg(),
                amount="10",
                confirmation=preview.confirmation,
                dry_run=False,
                submitter=lambda **_kw: {"transactionID": "tx"},
            )
    finally:
        conn.close()


def test_submit_does_not_rewrite_dry_run_boot_fuse(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        enter_transfer_lock(conn, boot_dry_run=True)

        with pytest.raises(TransferSafetyError, match="DRY_RUN"):
            submit_return_transfer(
                conn,
                config=_Cfg(),
                amount="10",
                confirmation="TRANSFER 10.000000 PUSD TO " + _Cfg.poly_return_wallet,
                dry_run=False,
                submitter=lambda **_kw: {"transactionID": "tx"},
            )

        assert get_operator_state(conn).boot_dry_run is True
        row = conn.execute("SELECT COUNT(*) AS n FROM transfer_requests").fetchone()
        assert row["n"] == 0
    finally:
        conn.close()


def test_preview_blocks_same_wallet_destination(tmp_path):
    """ce-code-review P1 #12: refuse when POLY_RETURN_WALLET == POLY_FUNDER."""
    class _SameCfg(_Cfg):
        poly_return_wallet = _Cfg.poly_funder  # destination == source

    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        preview = preview_return_transfer(
            conn,
            config=_SameCfg(),
            amount="10",
            dry_run=False,
        )
        assert preview.ok is False
        assert any("cannot be the same" in err for err in preview.errors)
    finally:
        conn.close()


def test_preview_blocks_amount_above_available(tmp_path):
    """ce-code-review P1 #13: refuse when amount > available pUSD."""
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)  # 100.0 pUSD available
        preview = preview_return_transfer(
            conn,
            config=_Cfg(),
            amount="500",
            dry_run=False,
        )
        assert preview.ok is False
        assert any("exceeds available" in err for err in preview.errors)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "amount, expected_token",
    [
        ("0", "greater than 0"),
        ("-1.5", "greater than 0"),
        ("not-a-number", "decimal"),
        ("", "decimal"),
    ],
)
def test_preview_surfaces_invalid_amount(tmp_path, amount, expected_token):
    # ce-code-review P2 #41: preview branch for amount parsing errors was not
    # exercised. preview_return_transfer must catch TransferSafetyError from
    # pusd_amount_to_base_units and surface it as a preview error (not raise).
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        preview = preview_return_transfer(
            conn,
            config=_Cfg(),
            amount=amount,
            dry_run=False,
        )
        assert preview.ok is False
        assert any(expected_token in err for err in preview.errors), (
            f"expected token {expected_token!r} in errors {preview.errors!r}"
        )
        # Defensive: ensure no transfer_request row was written on a rejected preview.
        rows = conn.execute("SELECT COUNT(*) AS n FROM transfer_requests").fetchone()
        assert rows["n"] == 0
    finally:
        conn.close()


def test_submit_records_failed_status_on_relayer_exception(tmp_path):
    """ce-code-review P1 #14: relayer failure flips the row to FAILED with the error."""
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        enter_transfer_lock(conn, boot_dry_run=False)
        preview = preview_return_transfer(conn, config=_Cfg(), amount="10", dry_run=False)

        def _explode(**_kw):
            raise RuntimeError("relayer 500")

        with pytest.raises(RuntimeError, match="relayer 500"):
            submit_return_transfer(
                conn,
                config=_Cfg(),
                amount="10",
                confirmation=preview.confirmation,
                dry_run=False,
                submitter=_explode,
            )

        row = conn.execute(
            "SELECT status, error FROM transfer_requests "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["status"] == "FAILED"
        assert "relayer 500" in (row["error"] or "")
    finally:
        conn.close()


def test_pusd_amount_to_base_units_catches_systemexit():
    """ce-code-review P1 #15: invalid amount raises TransferSafetyError, not SystemExit.

    The CLI delegates to pusd_amount_to_base_units (P2 #46). The execution
    boundary must surface TransferSafetyError so the dashboard/submit code
    paths can format it as a 4xx without leaking SystemExit through FastAPI.
    """
    from hightempbot.execution.polymarket_transfer import (
        pusd_amount_to_base_units,
        TransferSafetyError,
    )
    with pytest.raises(TransferSafetyError, match="decimal"):
        pusd_amount_to_base_units("not-a-number")
    with pytest.raises(TransferSafetyError, match="greater than 0"):
        pusd_amount_to_base_units("0")
    with pytest.raises(TransferSafetyError, match="greater than 0"):
        pusd_amount_to_base_units("-5")


def test_preview_pins_destination_to_return_wallet(tmp_path):
    """ce-code-review P0 #1: operator-supplied to_wallet must equal POLY_RETURN_WALLET."""
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        with pytest.raises(TransferSafetyError, match="POLY_RETURN_WALLET"):
            preview_return_transfer(
                conn,
                config=_Cfg(),
                amount="10",
                to_wallet="0x" + "f" * 40,  # not the return wallet
                dry_run=False,
            )
    finally:
        conn.close()


def test_preview_blocks_zero_and_burn_destinations(tmp_path, monkeypatch):
    """ce-code-review P3 #68: explicit reject of 0x000…/0x000…dead."""
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        for bad in ("0x" + "0" * 40, "0x000000000000000000000000000000000000dead"):
            class _BurnCfg(_Cfg):
                poly_return_wallet = bad
            with pytest.raises(TransferSafetyError, match="zero or burn"):
                preview_return_transfer(
                    conn,
                    config=_BurnCfg(),
                    amount="10",
                    dry_run=False,
                )
    finally:
        conn.close()


def test_preview_confirmation_uses_lowercased_destination(tmp_path):
    """ce-code-review P3 #67: confirmation string anchors to lowercase address."""
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        preview = preview_return_transfer(
            conn, config=_Cfg(), amount="10", dry_run=False,
        )
        # to_wallet preserves operator casing; confirmation string is the
        # canonical lowercased form so submit's strict equality match is
        # stable across operator typing.
        assert preview.confirmation.endswith(_Cfg.poly_return_wallet.lower())
    finally:
        conn.close()


def test_submit_rejects_duplicate_in_flight(tmp_path):
    """ce-code-review P1 #11: unique partial index blocks a second SUBMITTING row."""
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        enter_transfer_lock(conn, boot_dry_run=False)
        preview = preview_return_transfer(conn, config=_Cfg(), amount="10", dry_run=False)
        # Manually create an in-flight SUBMITTING row to simulate a concurrent
        # call that already passed the BEGIN IMMEDIATE gate.
        from hightempbot.execution.polymarket_transfer import ensure_transfer_schema
        ensure_transfer_schema(conn)
        conn.execute(
            "INSERT INTO transfer_requests "
            "(from_wallet, to_wallet, amount_usd, status, confirmation) "
            "VALUES (?, ?, ?, 'SUBMITTING', ?)",
            (_Cfg.poly_funder, _Cfg.poly_return_wallet, 10.0, preview.confirmation),
        )
        conn.commit()
        with pytest.raises(TransferSafetyError, match="already in flight"):
            submit_return_transfer(
                conn,
                config=_Cfg(),
                amount="10",
                confirmation=preview.confirmation,
                dry_run=False,
                submitter=lambda **_kw: {"transactionID": "tx-dup"},
            )
    finally:
        conn.close()


def test_stale_submitting_transfer_is_recovered_on_submit_not_preview(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        enter_transfer_lock(conn, boot_dry_run=False)
        conn.execute(
            """INSERT INTO transfer_requests
            (created_at, updated_at, from_wallet, to_wallet, amount_usd, status, confirmation)
            VALUES (?, ?, ?, ?, ?, 'SUBMITTING', ?)""",
            (
                "2026-01-01 00:00:00",
                "2026-01-01 00:00:00",
                _Cfg.poly_funder,
                _Cfg.poly_return_wallet,
                10.0,
                "stale",
            ),
        )
        conn.commit()

        preview = preview_return_transfer(conn, config=_Cfg(), amount="10", dry_run=False)

        row = conn.execute("SELECT status, error FROM transfer_requests").fetchone()
        assert row["status"] == "SUBMITTING"
        assert row["error"] is None
        assert return_transfer_notional(conn) == pytest.approx(0.0)
        assert preview.ok is True

        result = submit_return_transfer(
            conn,
            config=_Cfg(),
            amount="10",
            confirmation=preview.confirmation,
            dry_run=False,
            submitter=lambda **_kw: {"transactionID": "tx-after-stale"},
        )

        assert result["status"] == "SUBMITTED"
        rows = conn.execute(
            "SELECT status, error FROM transfer_requests ORDER BY id"
        ).fetchall()
        assert [r["status"] for r in rows] == ["FAILED", "SUBMITTED"]
        assert "Stale SUBMITTING transfer" in rows[0]["error"]
    finally:
        conn.close()


def test_submit_calls_relayer_submitter_after_confirmation(tmp_path):
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        enter_transfer_lock(conn, boot_dry_run=False)
        preview = preview_return_transfer(conn, config=_Cfg(), amount="10", dry_run=False)
        calls = []

        def _submitter(**kwargs):
            calls.append(kwargs)
            return {"transactionID": "tx-1"}

        result = submit_return_transfer(
            conn,
            config=_Cfg(),
            amount="10",
            confirmation=preview.confirmation,
            dry_run=False,
            submitter=_submitter,
        )

        assert result["status"] == "SUBMITTED"
        assert result["relayerTxId"] == "tx-1"
        assert calls[0]["deposit_wallet"] == _Cfg.poly_funder
        assert calls[0]["to_address"] == _Cfg.poly_return_wallet
        assert calls[0]["amount_base_units"] == 10_000_000
    finally:
        conn.close()


def test_submit_invokes_snapshot_refresher_for_agent_callers(tmp_path):
    """ce-code-review #38/#39: non-dashboard callers can inject a refresher
    so the snapshot is refreshed before the submit-time freshness gate runs."""
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        enter_transfer_lock(conn, boot_dry_run=False)
        preview = preview_return_transfer(conn, config=_Cfg(), amount="10", dry_run=False)
        refresh_calls: list[object] = []

        def _refresher(c):
            refresh_calls.append(c)

        def _submitter(**kwargs):
            return {"transactionID": "tx-2"}

        result = submit_return_transfer(
            conn,
            config=_Cfg(),
            amount="10",
            confirmation=preview.confirmation,
            dry_run=False,
            submitter=_submitter,
            snapshot_refresher=_refresher,
        )

        assert result["status"] == "SUBMITTED"
        assert len(refresh_calls) == 1, "snapshot_refresher should be invoked exactly once"
        assert refresh_calls[0] is conn
    finally:
        conn.close()


def test_submit_tolerates_snapshot_refresher_failure(tmp_path):
    """ce-code-review #38/#39: refresher failure is non-fatal — the existing
    freshness gate inside preview is the real gatekeeper. This mirrors the
    dashboard's upstream behavior at _assert_fresh_live_action_context."""
    conn = init_db(tmp_path / "test.db")
    try:
        _fresh_wallet(conn)
        enter_transfer_lock(conn, boot_dry_run=False)
        preview = preview_return_transfer(conn, config=_Cfg(), amount="10", dry_run=False)

        def _broken_refresher(c):
            raise RuntimeError("CLOB unreachable in test env")

        def _submitter(**kwargs):
            return {"transactionID": "tx-3"}

        result = submit_return_transfer(
            conn,
            config=_Cfg(),
            amount="10",
            confirmation=preview.confirmation,
            dry_run=False,
            submitter=_submitter,
            snapshot_refresher=_broken_refresher,
        )

        assert result["status"] == "SUBMITTED"
    finally:
        conn.close()
