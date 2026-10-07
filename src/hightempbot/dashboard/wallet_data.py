"""Dashboard payload adapters for live operator controls."""

from __future__ import annotations

import sqlite3
from typing import Any

from hightempbot.execution.live_readiness import (
    latest_readiness_report,
    readiness_report_is_fresh,
)
from hightempbot.execution.operator_control import public_operator_payload
from hightempbot.persistence.wallet_reconciliation import wallet_dashboard_payload
from hightempbot.polymarket.primitives import is_address
from hightempbot.runtime_config import get_config


def build_operator_wallet_payload(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    cfg = get_config()
    readiness = latest_readiness_report(conn) or {
        "status": "SKIPPED" if dry_run else "UNKNOWN",
        "mode": "DRY-RUN" if dry_run else "LIVE",
        "checks": [],
    }
    freshness_ttl_s = int(getattr(cfg, "operator_action_freshness_ttl_s", 300) or 300)
    readiness_fresh = readiness_report_is_fresh(
        readiness,
        freshness_ttl_s=freshness_ttl_s,
    )
    readiness["fresh"] = readiness_fresh
    wallet = wallet_dashboard_payload(conn, config=cfg, dry_run=dry_run)
    return_wallet = (getattr(cfg, "poly_return_wallet", "") or "").strip()
    wallet["returnWallet"] = return_wallet
    wallet["returnWalletConfigured"] = is_address(return_wallet)
    # drop the raw reconciliation records[] from the
    # response — UI never reads it, and a populated array inflates the
    # /api/v2/data envelope by 10-100KB per request.
    # drop the inner snapshot.walletAddress alias —
    # the UI addresses wallet.primaryWallet at the top level; carrying both
    # names was a duplicated-naming foot-gun.
    _snap = wallet.get("snapshot")
    if isinstance(_snap, dict):
        _snap.pop("records", None)
        _snap.pop("walletAddress", None)
    operator = public_operator_payload(conn, dry_run=dry_run)
    live_actions_enabled = (
        not dry_run
        and readiness.get("status") == "OK"
        and readiness_fresh
        and wallet.get("actionsEnabled") is True
        and not operator.get("bootDryRun")
    )
    return {
        "operator": operator,
        "wallet": wallet,
        "readiness": readiness,
        "liveActionsEnabled": live_actions_enabled,
    }
