"""Safe return-transfer workflow for pUSD held by the bot deposit wallet."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Callable

from hightempbot.polymarket.primitives import PUSD_DECIMALS, is_address, mask_address
from hightempbot.db.connection import utc_now_sql
from hightempbot.execution.operator_control import TRANSFER_LOCK, get_operator_state
from hightempbot.execution.stale_requests import recover_stale_submitting_requests
from hightempbot.persistence.wallet_reconciliation import (
    latest_wallet_snapshot,
    transfer_blocking_reconciliation_warnings,
)


class TransferSafetyError(RuntimeError):
    """Raised when a transfer preview or submit violates a safety rule."""


STALE_SUBMITTING_TRANSFER_AFTER_S = 15 * 60


def recover_stale_submitting_transfers(
    conn: sqlite3.Connection,
    *,
    max_age_s: int = STALE_SUBMITTING_TRANSFER_AFTER_S,
) -> int:
    """Fail old pre-relayer SUBMITTING rows so they do not wedge capital forever."""
    return recover_stale_submitting_requests(
        conn,
        table="transfer_requests",
        noun="transfer",
        max_age_s=max_age_s,
    )


@dataclass(frozen=True)
class TransferPreview:
    ok: bool
    amount_usd: float
    amount_base_units: int
    from_wallet: str
    to_wallet: str
    confirmation: str
    errors: list[str]
    warnings: list[str]
    available_usd: float | None
    snapshot_fresh: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "amountUsd": self.amount_usd,
            "amountBaseUnits": self.amount_base_units,
            "fromWallet": self.from_wallet,
            "toWallet": self.to_wallet,
            "confirmation": self.confirmation,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "availableUsd": self.available_usd,
            "snapshotFresh": self.snapshot_fresh,
        }


def ensure_transfer_schema(conn: sqlite3.Connection) -> None:
    """Check transfer_requests exists and add its one-in-flight index on old DBs."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='transfer_requests'"
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "transfer_requests table is missing; run init_db "
            "(hightempbot.db.connection.init_db) to apply schema.sql"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS transfer_requests_single_submit "
        "ON transfer_requests(from_wallet) WHERE status='SUBMITTING'"
    )
    conn.commit()


def pusd_amount_to_base_units(amount: str | float | Decimal) -> int:
    if isinstance(amount, str):
        cleaned = amount.strip().replace(",", "")
        if cleaned.startswith("$"):
            cleaned = cleaned[1:].strip()
        lower = cleaned.lower()
        for suffix in ("pusd", "usd"):
            if lower.endswith(suffix):
                cleaned = cleaned[: -len(suffix)].strip()
                break
        amount = cleaned
    try:
        decimal_amount = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise TransferSafetyError("amount must be a decimal pUSD value") from exc
    if decimal_amount <= 0:
        raise TransferSafetyError("amount must be greater than 0")
    base_units = (decimal_amount * (Decimal(10) ** PUSD_DECIMALS)).quantize(
        Decimal("1"),
        rounding=ROUND_DOWN,
    )
    if base_units <= 0:
        raise TransferSafetyError("amount is below one pUSD base unit")
    return int(base_units)


def _live_local_pending_notional(conn: sqlite3.Connection) -> float:
    from hightempbot.execution.capital import live_local_pending_notional

    return live_local_pending_notional(conn)


SUBMIT_FRESHNESS_TTL_S = 30
"""Tight freshness window for the snapshot read inside submit_return_transfer.

ce-code-review #38/#39: preview reads can tolerate the operator's configured
WALLET_SNAPSHOT_FRESHNESS_TTL_S (default 900s) because the preview is an
advisory readout. Submit cannot — between the operator's preview click and the
submit click, the wallet could have absorbed a fill or another transfer.
Enforcing a 30s window at submit closes that TOCTOU even for callers that
bypass the dashboard's upstream refresh.
"""


def preview_return_transfer(
    conn: sqlite3.Connection,
    *,
    config,
    amount: str | float | Decimal,
    to_wallet: str | None = None,
    dry_run: bool,
    freshness_ttl_s_override: int | None = None,
) -> TransferPreview:
    ensure_transfer_schema(conn)
    errors: list[str] = []
    warnings: list[str] = []
    from_wallet = (getattr(config, "poly_funder", "") or "").strip()
    return_wallet = (getattr(config, "poly_return_wallet", "") or "").strip()
    destination = (to_wallet or return_wallet).strip()

    # Only POLY_RETURN_WALLET is allowed as a destination.
    if not return_wallet:
        errors.append("POLY_RETURN_WALLET is not configured; transfer disabled")
    elif destination.lower() != return_wallet.lower():
        raise TransferSafetyError(
            "destination must equal POLY_RETURN_WALLET"
        )
    # block zero/burn destinations explicitly.
    _ZERO = "0x" + "0" * 40
    _BURN = "0x000000000000000000000000000000000000dead"
    if destination.lower() in {_ZERO, _BURN}:
        raise TransferSafetyError("destination cannot be the zero or burn address")

    try:
        amount_base_units = pusd_amount_to_base_units(amount)
        amount_usd = amount_base_units / (10 ** PUSD_DECIMALS)
    except TransferSafetyError as exc:
        amount_base_units = 0
        amount_usd = 0.0
        errors.append(str(exc))

    if dry_run:
        errors.append("process is booted DRY_RUN; transfer submit is disabled")
    if not is_address(from_wallet):
        errors.append("POLY_FUNDER must be a valid deposit-wallet address")
    if not is_address(destination):
        errors.append("POLY_RETURN_WALLET or requested destination must be a valid address")
    if from_wallet and destination and from_wallet.lower() == destination.lower():
        errors.append("destination cannot be the same as POLY_FUNDER")

    local_pending = _live_local_pending_notional(conn)
    if local_pending > 0:
        errors.append(
            f"local in-flight order exposure exists (${local_pending:.2f}); wait for submit/cancel before transfer"
        )

    if freshness_ttl_s_override is not None:
        ttl_s = int(freshness_ttl_s_override)
    else:
        ttl_s = int(getattr(config, "wallet_snapshot_freshness_ttl_s", 900) or 900)
    snapshot = (
        latest_wallet_snapshot(conn, wallet_address=from_wallet, freshness_ttl_s=ttl_s)
        if is_address(from_wallet)
        else None
    )
    snapshot_fresh = bool(snapshot and snapshot.get("fresh"))
    if not snapshot_fresh:
        errors.append("wallet snapshot is missing or stale")

    available = None
    if snapshot:
        if snapshot.get("complete") is not True:
            errors.append("wallet snapshot is incomplete; refresh wallet reconciliation before transfer")
        available = snapshot.get("clobBalanceUsd")
        if available is None:
            available = snapshot.get("chainBalanceUsd")
        try:
            available = float(available) if available is not None else None
        except (TypeError, ValueError):
            available = None
        snapshot_warnings = [str(w) for w in (snapshot.get("warnings") or [])]
        warnings.extend(snapshot_warnings)
        if snapshot_warnings:
            errors.append("wallet snapshot has unresolved warning(s); refresh or resolve before transfer")
        blocking_reconciliation_warnings = transfer_blocking_reconciliation_warnings(snapshot)
        warnings.extend(blocking_reconciliation_warnings)
        if blocking_reconciliation_warnings:
            errors.append(
                "wallet snapshot has transfer-blocking Data API reconciliation warning(s); "
                "resolve before transfer"
            )
        if int(snapshot.get("openOrdersCount") or 0) > 0:
            errors.append("open CLOB orders exist; cancel or wait before transfer")
        open_positions = int(snapshot.get("openPositionsCount") or 0)
        if open_positions > 0:
            warnings.append(
                f"{open_positions} unresolved wallet position(s) remain; transfer moves free pUSD only"
            )
    if available is None:
        errors.append("wallet snapshot has no available pUSD balance")
    elif amount_usd > available:
        errors.append(f"amount ${amount_usd:.2f} exceeds available pUSD ${available:.2f}")

    # Lowercase address in the confirmation; blank if the destination is invalid.
    confirmation = (
        f"TRANSFER {amount_usd:.6f} PUSD TO {destination.lower()}"
        if amount_base_units > 0 and is_address(destination)
        else ""
    )
    return TransferPreview(
        ok=not errors,
        amount_usd=amount_usd,
        amount_base_units=amount_base_units,
        from_wallet=from_wallet,
        to_wallet=destination,
        confirmation=confirmation,
        errors=errors,
        warnings=warnings,
        available_usd=available,
        snapshot_fresh=snapshot_fresh,
    )


def submit_return_transfer(
    conn: sqlite3.Connection,
    *,
    config,
    amount: str | float | Decimal,
    confirmation: str,
    actor: str = "dashboard",
    to_wallet: str | None = None,
    dry_run: bool,
    submitter: Callable[..., dict[str, Any]] | None = None,
    snapshot_refresher: Callable[[sqlite3.Connection], None] | None = None,
) -> dict[str, Any]:
    ensure_transfer_schema(conn)
    recover_stale_submitting_transfers(conn)
    state = get_operator_state(conn)
    if state.boot_dry_run:
        raise TransferSafetyError("Transfer Submit cannot override DRY_RUN=True boot mode")
    if state.state != TRANSFER_LOCK:
        raise TransferSafetyError("Transfer Submit requires active TRANSFER_LOCK")

    # Optional refresh for non-dashboard callers; the freshness check still applies.
    if snapshot_refresher is not None:
        try:
            snapshot_refresher(conn)
        except Exception:
            pass

    preview = preview_return_transfer(
        conn,
        config=config,
        amount=amount,
        to_wallet=to_wallet,
        dry_run=dry_run,
        freshness_ttl_s_override=SUBMIT_FRESHNESS_TTL_S,
    )
    if not preview.ok:
        raise TransferSafetyError("; ".join(preview.errors))
    if confirmation != preview.confirmation:
        raise TransferSafetyError("confirmation text does not match preview")

    # The unique SUBMITTING index turns a concurrent second submit into an error.
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise TransferSafetyError(
            f"could not acquire transfer write-lock: {exc}"
        ) from exc
    try:
        cur = conn.execute(
            """
            INSERT INTO transfer_requests
            (actor, from_wallet, to_wallet, amount_usd, status, confirmation,
             preview_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor,
                preview.from_wallet,
                preview.to_wallet,
                preview.amount_usd,
                "SUBMITTING",
                confirmation,
                json.dumps(preview.to_dict(), sort_keys=True),
            ),
        )
        request_id = int(cur.lastrowid)
        conn.commit()
    except sqlite3.IntegrityError as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        raise TransferSafetyError("transfer already in flight") from exc

    try:
        if submitter is None:
            from hightempbot.execution.polymarket_relayer import (
                submit_deposit_wallet_pusd_transfer,
            )

            submitter = submit_deposit_wallet_pusd_transfer
        response = submitter(
            relayer_url=getattr(config, "relayer_url", ""),
            api_key=config.relayer_api_key.get_secret_value(),
            api_key_address=getattr(config, "relayer_api_key_address", ""),
            private_key=config.poly_private_key.get_secret_value(),
            deposit_wallet=preview.from_wallet,
            to_address=preview.to_wallet,
            amount_base_units=preview.amount_base_units,
        )
        relayer_tx_id = str(
            response.get("transactionID") or response.get("transactionId") or ""
        )
        conn.execute(
            """
            UPDATE transfer_requests
            SET status='SUBMITTED', updated_at=?, relayer_tx_id=?, error=NULL
            WHERE id=?
            """,
            (utc_now_sql(), relayer_tx_id, request_id),
        )
        conn.commit()
        return {
            "requestId": request_id,
            "status": "SUBMITTED",
            "relayerTxId": relayer_tx_id,
            "fromWallet": mask_address(preview.from_wallet),
            "toWallet": mask_address(preview.to_wallet),
            "amountUsd": preview.amount_usd,
            "response": response,
        }
    except Exception as exc:
        conn.execute(
            """
            UPDATE transfer_requests
            SET status='FAILED', updated_at=?, error=?
            WHERE id=?
            """,
            (utc_now_sql(), f"{type(exc).__name__}: {exc}", request_id),
        )
        conn.commit()
        raise
