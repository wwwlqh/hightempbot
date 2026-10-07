"""Shared guards for live-money operator actions."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hightempbot.runtime_config import Config

logger = logging.getLogger(__name__)


class LiveActionSafetyError(RuntimeError):
    """Raised when a live-money action lacks fresh readiness context."""


def assert_fresh_live_action_context(
    conn: sqlite3.Connection,
    *,
    config: Config,
    dry_run: bool,
    require_no_exposure: bool = False,
    freshness_ttl_s_override: int | None = None,
    dry_run_message: str = "Process booted DRY_RUN=True; live-money actions are disabled.",
) -> None:
    """Raise unless readiness is fresh and OK and the wallet snapshot is
    complete. May refresh both; never moves money itself."""
    if dry_run:
        raise LiveActionSafetyError(dry_run_message)

    from hightempbot.execution.live_readiness import (
        build_live_readiness_report,
        latest_readiness_report,
        readiness_report_is_fresh,
        record_readiness_report,
    )
    from hightempbot.persistence.wallet_reconciliation import (
        refresh_wallet_snapshot,
        wallet_dashboard_payload,
    )

    if freshness_ttl_s_override is not None:
        freshness_ttl_s = int(freshness_ttl_s_override)
    else:
        freshness_ttl_s = int(getattr(config, "operator_action_freshness_ttl_s", 300) or 300)

    readiness = latest_readiness_report(conn)
    if (
        not readiness
        or readiness.get("status") != "OK"
        or not readiness_report_is_fresh(readiness, freshness_ttl_s=freshness_ttl_s)
    ):
        report = build_live_readiness_report(config)
        record_readiness_report(conn, report)
        readiness = latest_readiness_report(conn)

    try:
        refresh_wallet_snapshot(conn, config=config)
    except Exception:
        logger.warning("wallet snapshot refresh failed before live action", exc_info=True)

    readiness = dict(readiness or {})
    readiness["fresh"] = readiness_report_is_fresh(
        readiness,
        freshness_ttl_s=freshness_ttl_s,
    )
    wallet = wallet_dashboard_payload(
        conn,
        config=config,
        dry_run=dry_run,
    )

    if readiness.get("status") != "OK":
        raise LiveActionSafetyError(
            "Live readiness is missing or failing; refresh diagnostics before this action."
        )
    if readiness.get("fresh") is not True:
        raise LiveActionSafetyError(
            "Live readiness is stale; refresh diagnostics before this action."
        )
    if not wallet.get("fresh"):
        raise LiveActionSafetyError(
            "Wallet snapshot is stale; refresh reconciliation before this action."
        )
    if wallet.get("actionsEnabled") is not True:
        raise LiveActionSafetyError(
            "Wallet snapshot is degraded or incomplete; live action refused."
        )
    if require_no_exposure and wallet.get("transferEligible") is not True:
        reason = str(wallet.get("transferBlockedReason") or "")
        raise LiveActionSafetyError(
            reason or "Wallet has open orders or an in-flight local order; transfer action refused."
        )

    return None
