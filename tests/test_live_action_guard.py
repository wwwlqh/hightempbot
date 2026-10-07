from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from hightempbot.db.connection import init_db, utc_now_sql
from hightempbot.execution.live_action_guard import (
    LiveActionSafetyError,
    assert_fresh_live_action_context,
)
from hightempbot.execution.live_readiness import ReadinessReport
from hightempbot.persistence.wallet_reconciliation import (
    build_wallet_snapshot,
    record_wallet_snapshot,
)


def _cfg(wallet: str) -> SimpleNamespace:
    return SimpleNamespace(
        dry_run=False,
        poly_funder=wallet,
        operator_action_freshness_ttl_s=300,
        wallet_snapshot_freshness_ttl_s=300,
        live_onchain_verify_enabled=False,
    )


def _ok_readiness(wallet: str) -> ReadinessReport:
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    return ReadinessReport(
        status="OK",
        mode="LIVE",
        generated_at=utc_now_sql(),
        expires_at=expires.strftime("%Y-%m-%d %H:%M:%S"),
        signature_type=3,
        funder=wallet,
    )


def _record_wallet(conn, wallet: str, *, open_orders: list[dict] | None = None):
    snapshot = build_wallet_snapshot(
        conn,
        wallet_address=wallet,
        clob_balance_usd=100.0,
        chain_balance_usd=100.0,
        data_api_trades=[],
        data_api_positions=[],
        open_orders=list(open_orders or []),
        chain_balance_required=False,
    )
    record_wallet_snapshot(conn, snapshot)
    return snapshot


def test_live_action_guard_refreshes_and_accepts_fresh_context(tmp_path, monkeypatch):
    wallet = "0x" + "d" * 40
    conn = init_db(tmp_path / "guard-ok.db")
    calls: list[str] = []

    def _fake_readiness(_config):
        calls.append("readiness")
        return _ok_readiness(wallet)

    def _fake_wallet_refresh(c, *, config):
        calls.append("wallet")
        return _record_wallet(c, config.poly_funder)

    monkeypatch.setattr(
        "hightempbot.execution.live_readiness.build_live_readiness_report",
        _fake_readiness,
    )
    monkeypatch.setattr(
        "hightempbot.persistence.wallet_reconciliation.refresh_wallet_snapshot",
        _fake_wallet_refresh,
    )
    try:
        assert_fresh_live_action_context(
            conn,
            config=_cfg(wallet),
            dry_run=False,
        )

        assert calls == ["readiness", "wallet"]
    finally:
        conn.close()


def test_live_action_guard_refuses_transfer_when_wallet_not_transfer_eligible(
    tmp_path,
    monkeypatch,
):
    wallet = "0x" + "d" * 40
    conn = init_db(tmp_path / "guard-open-orders.db")

    def _fake_readiness(_config):
        return _ok_readiness(wallet)

    def _fake_wallet_refresh(c, *, config):
        return _record_wallet(c, config.poly_funder, open_orders=[{"id": "order-1"}])

    monkeypatch.setattr(
        "hightempbot.execution.live_readiness.build_live_readiness_report",
        _fake_readiness,
    )
    monkeypatch.setattr(
        "hightempbot.persistence.wallet_reconciliation.refresh_wallet_snapshot",
        _fake_wallet_refresh,
    )
    try:
        with pytest.raises(LiveActionSafetyError, match="Open CLOB orders"):
            assert_fresh_live_action_context(
                conn,
                config=_cfg(wallet),
                dry_run=False,
                require_no_exposure=True,
            )
    finally:
        conn.close()


def test_live_action_guard_refuses_dry_run_without_refreshing(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "guard-dry-run.db")
    refreshed = False

    def _fake_wallet_refresh(_conn, *, config):
        nonlocal refreshed
        refreshed = True

    monkeypatch.setattr(
        "hightempbot.persistence.wallet_reconciliation.refresh_wallet_snapshot",
        _fake_wallet_refresh,
    )
    try:
        with pytest.raises(LiveActionSafetyError, match="DRY_RUN"):
            assert_fresh_live_action_context(
                conn,
                config=_cfg("0x" + "d" * 40),
                dry_run=True,
            )
        assert refreshed is False
    finally:
        conn.close()
