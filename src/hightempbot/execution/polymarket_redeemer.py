"""Auto-redeem Polymarket positions marked redeemable by the Data API."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Iterable

from hightempbot.db.connection import log_pipeline_health, safe_float as _safe_float, utc_now_sql
from hightempbot.execution.stale_requests import recover_stale_submitting_requests
from hightempbot.execution.strategy_constants import REDEEMABLE_PAYOUT_VALUE_FRACTION
from hightempbot.persistence.ledger import record_resolution
from hightempbot.persistence.wallet_reconciliation import fetch_data_api_wallet_records
from hightempbot.polymarket.primitives import is_address, mask_address

logger = logging.getLogger(__name__)

REDEMPTION_SOURCE = "polymarket_data_api_redeemable"
ZERO_PAYOUT_SOURCE = "polymarket_data_api_zero_payout_redeemable"
MIN_REDEEMABLE_PAYOUT_FRAC = REDEEMABLE_PAYOUT_VALUE_FRACTION
MAX_ZERO_PAYOUT_FRAC = max(0.0, 1.0 - MIN_REDEEMABLE_PAYOUT_FRAC)
MAX_ZERO_PAYOUT_USD = 0.01
LEDGER_SIZE_MATCH_ABS_TOL = 1e-6
LEDGER_SIZE_MATCH_REL_TOL = 1e-6
LEDGER_SIZE_MATCH_ROUNDED_DP = 2
STALE_SUBMITTING_REDEMPTION_AFTER_S = 15 * 60


class RedemptionError(RuntimeError):
    """Raised when the auto-redeemer cannot safely process a position."""


@dataclass(frozen=True)
class RedeemablePosition:
    wallet_address: str
    token_id: str
    condition_id: str
    outcome: str
    outcome_index: int
    index_set: int
    negative_risk: bool
    size: float
    current_value_usd: float
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RedemptionScanResult:
    status: str
    scanned: int = 0
    redeemable: int = 0
    zero_payout: int = 0
    ignored_zero_payout: int = 0
    settled_rows: int = 0
    settled_losses: int = 0
    submitted: int = 0
    deferred: int = 0
    skipped_existing: int = 0
    skipped_unmatched: int = 0
    blocked_manual: int = 0
    failed: int = 0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "scanned": self.scanned,
            "redeemable": self.redeemable,
            "zeroPayout": self.zero_payout,
            "ignoredZeroPayout": self.ignored_zero_payout,
            "settledRows": self.settled_rows,
            "settledLosses": self.settled_losses,
            "submitted": self.submitted,
            "deferred": self.deferred,
            "skippedExisting": self.skipped_existing,
            "skippedUnmatched": self.skipped_unmatched,
            "blockedManual": self.blocked_manual,
            "failed": self.failed,
            "message": self.message,
        }


def ensure_redemption_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='redemption_requests'"
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "redemption_requests table is missing; run init_db "
            "(hightempbot.db.connection.init_db) to apply schema.sql"
        )
    recover_stale_submitting_redemptions(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS redemption_requests_active_unique "
        "ON redemption_requests(wallet_address, condition_id, token_id) "
        "WHERE status IN ('SUBMITTING','SUBMITTED','CONFIRMED')"
    )
    conn.commit()


def recover_stale_submitting_redemptions(
    conn: sqlite3.Connection,
    *,
    max_age_s: int = STALE_SUBMITTING_REDEMPTION_AFTER_S,
) -> int:
    """Fail old pre-relayer redemption rows so scans can retry visibly."""
    return recover_stale_submitting_requests(
        conn,
        table="redemption_requests",
        noun="redemption",
        max_age_s=max_age_s,
    )


def _raw_position_value(raw: dict[str, Any], size: float) -> float:
    current_value = _safe_float(raw.get("currentValue"))
    if current_value <= 0:
        cur_price = _safe_float(raw.get("curPrice"))
        current_value = cur_price * size if cur_price > 0 and size > 0 else 0.0
    return current_value


def _redeemable_payout_value(raw: dict[str, Any], size: float) -> float:
    """Return the positive payout value for a truly winning redeemable position."""
    current_value = _raw_position_value(raw, size)
    if current_value <= 0:
        return 0.0
    if current_value < size * MIN_REDEEMABLE_PAYOUT_FRAC:
        return 0.0
    return current_value


def _is_zero_payout_redeemable(raw: dict[str, Any], size: float) -> bool:
    """Return true when a released position carries no meaningful payout."""
    if raw.get("redeemable") is not True or size <= 0:
        return False
    current_value = _raw_position_value(raw, size)
    zero_threshold = max(MAX_ZERO_PAYOUT_USD, size * MAX_ZERO_PAYOUT_FRAC)
    return current_value <= zero_threshold


def _extract_position(
    wallet_address: str,
    raw: dict[str, Any],
    *,
    require_full_payout: bool = True,
) -> RedeemablePosition | None:
    if raw.get("redeemable") is not True:
        return None
    size = _safe_float(raw.get("size"))
    if size <= 0:
        return None

    token_id = str(raw.get("asset") or "").strip()
    condition_id = str(raw.get("conditionId") or raw.get("condition_id") or "").strip()
    outcome = str(raw.get("outcome") or "").strip().upper()
    if outcome not in {"YES", "NO"}:
        return None
    try:
        outcome_index = int(raw.get("outcomeIndex"))
    except (TypeError, ValueError):
        outcome_index = 0 if outcome == "YES" else 1
    if outcome_index not in (0, 1):
        return None
    if not token_id or not condition_id:
        return None

    negative_raw = raw.get("negativeRisk")
    if not isinstance(negative_raw, bool):
        return None
    negative_risk = negative_raw
    current_value_usd = _redeemable_payout_value(raw, size)
    if current_value_usd <= 0 and require_full_payout:
        logger.info(
            "Skipping Data API redeemable position with zero/partial payout: token=%s currentValue=%s curPrice=%s size=%s",
            token_id,
            raw.get("currentValue"),
            raw.get("curPrice"),
            size,
        )
        return None

    return RedeemablePosition(
        wallet_address=wallet_address,
        token_id=token_id,
        condition_id=condition_id,
        outcome=outcome,
        outcome_index=outcome_index,
        index_set=1 << outcome_index,
        negative_risk=negative_risk,
        size=size,
        current_value_usd=current_value_usd,
        raw=dict(raw),
    )


def _position_rows(
    conn: sqlite3.Connection,
    position: RedeemablePosition,
    *,
    outcomes: tuple[str, ...] = ("PENDING", "WIN"),
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in outcomes)
    return conn.execute(
        f"""
        SELECT id, station_id, target_date, side, outcome, bet_size, fill_price,
               fill_size, event_detail
        FROM ledger
        WHERE event_type = 'bet'
          AND token_id = ?
          AND market_id = ?
          AND UPPER(side) = ?
          AND outcome IN ({placeholders})
        ORDER BY id ASC
        """,
        (position.token_id, position.condition_id, position.outcome, *outcomes),
    ).fetchall()


def _gross_win_pnl(row: sqlite3.Row) -> float | None:
    bet_size = _safe_float(row["bet_size"])
    fill_size = _row_ledger_size(row)
    if fill_size is None or fill_size <= 0:
        return None
    return fill_size - bet_size


def _gross_loss_pnl(row: sqlite3.Row) -> float | None:
    bet_size = _safe_float(row["bet_size"])
    return -bet_size if bet_size > 0 else None


def _row_ledger_size(row: sqlite3.Row) -> float | None:
    fill_size = _safe_float(row["fill_size"])
    if fill_size > 0:
        return fill_size
    bet_size = _safe_float(row["bet_size"])
    fill_price = _safe_float(row["fill_price"])
    if fill_price > 0 and bet_size > 0:
        return bet_size / fill_price
    return None


def _size_tolerance(expected_size: float, ledger_size: float) -> float:
    return max(
        LEDGER_SIZE_MATCH_ABS_TOL,
        LEDGER_SIZE_MATCH_REL_TOL * max(abs(expected_size), abs(ledger_size), 1.0),
    )


def _rounded_size(value: float) -> Decimal:
    quant = Decimal("1").scaleb(-LEDGER_SIZE_MATCH_ROUNDED_DP)
    return Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)


def _matched_ledger_size(rows: Iterable[sqlite3.Row]) -> tuple[float | None, list[int]]:
    total = 0.0
    missing_ids: list[int] = []
    for row in rows:
        row_size = _row_ledger_size(row)
        if row_size is None:
            missing_ids.append(int(row["id"]))
            continue
        total += row_size
    if missing_ids:
        return None, missing_ids
    return total, []


def _ledger_size_matches_position(
    *,
    position: RedeemablePosition,
    rows: Iterable[sqlite3.Row],
) -> tuple[bool, float | None, float]:
    ledger_size, missing_ids = _matched_ledger_size(rows)
    if ledger_size is None:
        logger.warning(
            "Redeemable wallet position cannot be ownership-sized because ledger rows lack fill metadata: token=%s condition=%s ledger_ids=%s",
            position.token_id,
            position.condition_id,
            missing_ids,
        )
        return False, None, 0.0
    tolerance = _size_tolerance(position.size, ledger_size)
    size_matches = abs(position.size - ledger_size) <= tolerance or _rounded_size(
        position.size
    ) == _rounded_size(ledger_size)
    return size_matches, ledger_size, tolerance


def _settle_pending_rows(
    conn: sqlite3.Connection,
    *,
    position: RedeemablePosition,
    rows: Iterable[sqlite3.Row],
) -> tuple[int, int]:
    settled = 0
    blocked = 0
    for row in rows:
        if row["outcome"] != "PENDING":
            continue
        gross_pnl = _gross_win_pnl(row)
        if gross_pnl is None:
            blocked += 1
            logger.warning(
                "Redeemable ledger row %s has no fill_size/fill_price; skipping auto-redeem until reconciled",
                row["id"],
            )
            continue
        extra_detail = {
            "redeemable_detected_at": utc_now_sql(),
            "redeemable_token_id": position.token_id,
            "redeemable_condition_id": position.condition_id,
            "redeemable_outcome": position.outcome,
            "redeemable_outcome_index": position.outcome_index,
            "redeemable_size": position.size,
            "redeemable_current_value_usd": position.current_value_usd,
            "redeemable_event_slug": position.raw.get("eventSlug") or "",
            "redeemable_slug": position.raw.get("slug") or "",
            "redeemable_title": position.raw.get("title") or "",
        }
        if record_resolution(
            conn,
            int(row["id"]),
            actual_tmax=None,
            outcome="WIN",
            pnl=gross_pnl,
            resolution_source=REDEMPTION_SOURCE,
            resolution_price=1.0,
            extra_detail=extra_detail,
        ):
            settled += 1
    return settled, blocked


def _settle_zero_payout_rows(
    conn: sqlite3.Connection,
    *,
    position: RedeemablePosition,
    rows: Iterable[sqlite3.Row],
) -> tuple[int, int]:
    settled = 0
    blocked = 0
    raw_value = _raw_position_value(position.raw, position.size)
    for row in rows:
        if row["outcome"] != "PENDING":
            continue
        gross_pnl = _gross_loss_pnl(row)
        if gross_pnl is None:
            blocked += 1
            logger.warning(
                "Zero-payout redeemable ledger row %s has no bet_size; skipping loss settlement",
                row["id"],
            )
            continue
        extra_detail = {
            "zero_payout_detected_at": utc_now_sql(),
            "zero_payout_token_id": position.token_id,
            "zero_payout_condition_id": position.condition_id,
            "zero_payout_outcome": position.outcome,
            "zero_payout_outcome_index": position.outcome_index,
            "zero_payout_size": position.size,
            "zero_payout_current_value_usd": raw_value,
            "zero_payout_event_slug": position.raw.get("eventSlug") or "",
            "zero_payout_slug": position.raw.get("slug") or "",
            "zero_payout_title": position.raw.get("title") or "",
        }
        if record_resolution(
            conn,
            int(row["id"]),
            actual_tmax=None,
            outcome="LOSS",
            pnl=gross_pnl,
            resolution_source=ZERO_PAYOUT_SOURCE,
            resolution_price=0.0,
            extra_detail=extra_detail,
        ):
            settled += 1
    return settled, blocked


def _existing_nonfailed_request(
    conn: sqlite3.Connection,
    position: RedeemablePosition,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, status
        FROM redemption_requests
        WHERE lower(wallet_address) = lower(?)
          AND condition_id = ?
          AND token_id = ?
          AND status != 'FAILED'
        ORDER BY id DESC
        LIMIT 1
        """,
        (position.wallet_address, position.condition_id, position.token_id),
    ).fetchone()


def _insert_submitting_request(
    conn: sqlite3.Connection,
    *,
    position: RedeemablePosition,
    matched_ledger_ids: list[int],
) -> tuple[int | None, str | None]:
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise RedemptionError(f"could not acquire redemption write-lock: {exc}") from exc
    try:
        existing = _existing_nonfailed_request(conn, position)
        if existing is not None:
            conn.rollback()
            return None, str(existing["status"] or "UNKNOWN")
        cur = conn.execute(
            """
            INSERT INTO redemption_requests
            (wallet_address, condition_id, token_id, outcome, outcome_index,
             index_set_value, negative_risk, size, current_value_usd, status,
             matched_ledger_ids_json, position_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'SUBMITTING', ?, ?)
            """,
            (
                position.wallet_address,
                position.condition_id,
                position.token_id,
                position.outcome,
                position.outcome_index,
                position.index_set,
                1 if position.negative_risk else 0,
                position.size,
                position.current_value_usd,
                json.dumps(matched_ledger_ids),
                json.dumps(position.raw, sort_keys=True),
            ),
        )
        request_id = int(cur.lastrowid)
        conn.commit()
        return request_id, None
    except sqlite3.IntegrityError as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        existing = _existing_nonfailed_request(conn, position)
        return None, str(existing["status"] or "UNKNOWN") if existing is not None else str(exc)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def _update_request_submitted(
    conn: sqlite3.Connection,
    *,
    request_id: int,
    response: dict[str, Any],
) -> None:
    relayer_tx_id = str(response.get("transactionID") or response.get("transactionId") or "")
    tx_hash = str(response.get("transactionHash") or response.get("txHash") or "")
    conn.execute(
        """
        UPDATE redemption_requests
        SET status='SUBMITTED',
            updated_at=?,
            relayer_tx_id=?,
            tx_hash=?,
            response_json=?,
            error=NULL
        WHERE id=?
        """,
        (
            utc_now_sql(),
            relayer_tx_id,
            tx_hash,
            json.dumps(response, sort_keys=True),
            request_id,
        ),
    )
    conn.commit()


def _update_request_failed(
    conn: sqlite3.Connection,
    *,
    request_id: int,
    exc: BaseException,
) -> None:
    conn.execute(
        """
        UPDATE redemption_requests
        SET status='FAILED', updated_at=?, error=?
        WHERE id=?
        """,
        (utc_now_sql(), f"{type(exc).__name__}: {exc}", request_id),
    )
    conn.commit()


def _fetch_redeemable_positions(wallet_address: str) -> list[dict[str, Any]]:
    _, positions = fetch_data_api_wallet_records(wallet_address)
    return positions


def _secret_value(config: Any, name: str) -> str:
    value = getattr(config, name, "")
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if callable(getter) else value or "")


def _creds_ready(config: Any) -> bool:
    return bool(
        _secret_value(config, "poly_private_key")
        and _secret_value(config, "relayer_api_key")
        and getattr(config, "relayer_api_key_address", "")
        and getattr(config, "relayer_url", "")
    )


def _scan_message(result: RedemptionScanResult) -> str:
    return (
        f"scanned={result.scanned} redeemable={result.redeemable} "
        f"settled_rows={result.settled_rows} submitted={result.submitted} "
        f"deferred={result.deferred} "
        f"existing={result.skipped_existing} unmatched={result.skipped_unmatched} "
        f"blocked_manual={result.blocked_manual} "
        f"failed={result.failed} zero_payout={result.zero_payout} "
        f"settled_losses={result.settled_losses}"
    )


def _short_id(value: str, *, prefix: int = 8, suffix: int = 6) -> str:
    text = str(value or "")
    if len(text) <= prefix + suffix + 3:
        return text
    return f"{text[:prefix]}...{text[-suffix:]}"


def _usd(value: float | None) -> str:
    if value is None:
        return "$-"
    return f"${float(value):.2f}"


def _signed_usd(value: float | None) -> str:
    if value is None:
        return "$-"
    amount = float(value)
    sign = "+" if amount >= 0 else "-"
    return f"{sign}${abs(amount):.2f}"


def _ledger_rows_for_notification(
    conn: sqlite3.Connection, ledger_ids: Iterable[int],
) -> list[sqlite3.Row]:
    ids = [int(row_id) for row_id in ledger_ids]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT id, station_id, target_date, side, outcome, pnl
        FROM ledger
        WHERE id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    return [by_id[row_id] for row_id in ids if row_id in by_id]


def _submitted_notification_detail(
    conn: sqlite3.Connection,
    *,
    request_id: int,
    position: RedeemablePosition,
    matched_ledger_ids: list[int],
    response: dict[str, Any],
    newly_settled: int,
) -> str:
    tx_hash = str(response.get("transactionHash") or response.get("txHash") or "")
    relayer_tx_id = str(response.get("transactionID") or response.get("transactionId") or "")
    chain_ref = f"tx={_short_id(tx_hash)}" if tx_hash else f"relayer={_short_id(relayer_tx_id)}"
    row_bits = []
    for row in _ledger_rows_for_notification(conn, matched_ledger_ids):
        row_bits.append(
            "row={id} {station} {target} {side} {outcome} pnl={pnl}".format(
                id=row["id"],
                station=row["station_id"],
                target=row["target_date"],
                side=row["side"],
                outcome=row["outcome"],
                pnl=_signed_usd(_safe_float(row["pnl"])),
            )
        )
    rows_text = "; ".join(row_bits) if row_bits else f"rows={matched_ledger_ids}"
    return (
        f"submitted request={request_id} payout={_usd(position.current_value_usd)} "
        f"settled_now={newly_settled} token={_short_id(position.token_id)} "
        f"{chain_ref}; {rows_text}"
    )


def _settled_loss_notification_detail(
    conn: sqlite3.Connection,
    *,
    position: RedeemablePosition,
    matched_ledger_ids: list[int],
    newly_settled: int,
) -> str:
    row_bits = []
    for row in _ledger_rows_for_notification(conn, matched_ledger_ids):
        row_bits.append(
            "row={id} {station} {target} {side} {outcome} pnl={pnl}".format(
                id=row["id"],
                station=row["station_id"],
                target=row["target_date"],
                side=row["side"],
                outcome=row["outcome"],
                pnl=_signed_usd(_safe_float(row["pnl"])),
            )
        )
    rows_text = "; ".join(row_bits) if row_bits else f"rows={matched_ledger_ids}"
    return (
        f"settled zero-payout LOSS settled_now={newly_settled} "
        f"token={_short_id(position.token_id)} payout={_usd(_raw_position_value(position.raw, position.size))}; "
        f"{rows_text}"
    )


def _notification_message(
    result: RedemptionScanResult,
    *,
    submitted_details: list[str],
    loss_details: list[str],
    failed_details: list[str],
    unmatched_details: list[str],
    manual_details: list[str],
) -> str:
    lines = [_scan_message(result)]
    for label, details in (
        ("Submitted", submitted_details),
        ("Settled losses", loss_details),
        ("Failed", failed_details),
        ("Unmatched", unmatched_details),
        ("Manual", manual_details),
    ):
        if not details:
            continue
        lines.append(f"{label}:")
        lines.extend(f"- {detail}" for detail in details[:5])
        if len(details) > 5:
            lines.append(f"- ... {len(details) - 5} more")
    return "\n".join(lines)


def run_redeemable_scan(
    conn: sqlite3.Connection,
    *,
    config: Any,
    dry_run: bool | None = None,
    positions_fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
    submitter: Callable[..., dict[str, Any]] | None = None,
    notify: Callable[[str, str], None] | None = None,
) -> RedemptionScanResult:
    """Scan POLY_FUNDER Data API positions, settle wins, and redeem matches."""
    ensure_redemption_schema(conn)
    if dry_run is None:
        dry_run = bool(getattr(config, "dry_run", True))
    enabled = bool(getattr(config, "auto_redeem_enabled", True))
    wallet_address = (getattr(config, "poly_funder", "") or "").strip()

    if dry_run:
        return RedemptionScanResult(status="SKIPPED", message="dry_run")
    if not enabled:
        return RedemptionScanResult(status="SKIPPED", message="auto_redeem_disabled")
    if not is_address(wallet_address):
        result = RedemptionScanResult(status="ERROR", message="POLY_FUNDER invalid")
        log_pipeline_health(conn, "", "auto_redeem", "ERROR", result.message)
        return result
    if not _creds_ready(config):
        result = RedemptionScanResult(status="WARNING", message="relayer credentials missing")
        log_pipeline_health(conn, "", "auto_redeem", "WARNING", result.message)
        return result

    try:
        raw_positions = (positions_fetcher or _fetch_redeemable_positions)(wallet_address)
    except Exception as exc:
        result = RedemptionScanResult(
            status="ERROR",
            message=f"Data API fetch failed: {type(exc).__name__}: {exc}",
        )
        log_pipeline_health(conn, "", "auto_redeem", "ERROR", result.message[:500])
        return result

    scanned = 0
    redeemable = 0
    zero_payout = 0
    ignored_zero_payout = 0
    settled_rows = 0
    settled_losses = 0
    submitted = 0
    deferred = 0
    skipped_existing = 0
    skipped_unmatched = 0
    blocked_manual = 0
    failed = 0
    submitted_details: list[str] = []
    loss_details: list[str] = []
    failed_details: list[str] = []
    unmatched_details: list[str] = []
    manual_details: list[str] = []

    if submitter is None:
        from hightempbot.execution.polymarket_relayer import (
            submit_deposit_wallet_redeem_positions,
        )

        submitter = submit_deposit_wallet_redeem_positions

    for raw in raw_positions:
        position = _extract_position(wallet_address, raw, require_full_payout=False)
        if position is None:
            scanned += 1
            continue

        zero_payout_rows: list[sqlite3.Row] | None = None
        is_zero_payout = (
            position.current_value_usd <= 0
            and _is_zero_payout_redeemable(raw, position.size)
        )
        if is_zero_payout:
            zero_payout_rows = _position_rows(conn, position, outcomes=("PENDING", "LOSS"))
            if not any(row["outcome"] == "PENDING" for row in zero_payout_rows):
                ignored_zero_payout += 1
                if not zero_payout_rows:
                    logger.info(
                        "Ignoring zero-payout redeemable position with no matching bot ledger rows: %s %s %s",
                        mask_address(wallet_address),
                        position.condition_id,
                        position.token_id,
                    )
                continue

        scanned += 1

        if position.current_value_usd <= 0:
            if not is_zero_payout:
                logger.info(
                    "Skipping Data API redeemable position with partial payout: token=%s currentValue=%s curPrice=%s size=%s",
                    position.token_id,
                    raw.get("currentValue"),
                    raw.get("curPrice"),
                    position.size,
                )
                continue
            zero_payout += 1
            rows = zero_payout_rows or _position_rows(conn, position, outcomes=("PENDING", "LOSS"))
            if not rows:
                logger.info(
                    "Zero-payout redeemable position has no matching bot ledger rows: %s %s %s",
                    mask_address(wallet_address),
                    position.condition_id,
                    position.token_id,
                )
                continue
            size_matches, ledger_size, tolerance = _ledger_size_matches_position(
                position=position,
                rows=rows,
            )
            if ledger_size is None:
                failed += 1
                failed_details.append(
                    f"token={_short_id(position.token_id)} cannot size-match ledger rows"
                )
                continue
            if not size_matches:
                blocked_manual += 1
                manual_details.append(
                    f"zero-payout token={_short_id(position.token_id)} side={position.outcome} "
                    f"wallet_size={position.size:.4f} ledger_size={ledger_size:.4f}"
                )
                logger.warning(
                    "Blocking zero-payout loss settlement for manual handling because wallet size does not match bot ledger size: wallet=%s condition=%s token=%s outcome=%s wallet_size=%.12g ledger_size=%.12g tolerance=%.12g",
                    mask_address(wallet_address),
                    position.condition_id,
                    position.token_id,
                    position.outcome,
                    position.size,
                    ledger_size,
                    tolerance,
                )
                continue

            newly_lost, loss_blocked = _settle_zero_payout_rows(
                conn,
                position=position,
                rows=rows,
            )
            settled_rows += newly_lost
            settled_losses += newly_lost
            if loss_blocked:
                failed += loss_blocked
                failed_details.append(
                    f"token={_short_id(position.token_id)} zero-payout loss settlement blocked for {loss_blocked} row(s)"
                )
                continue
            if newly_lost:
                matched_ledger_ids = [int(row["id"]) for row in rows]
                loss_details.append(
                    _settled_loss_notification_detail(
                        conn,
                        position=position,
                        matched_ledger_ids=matched_ledger_ids,
                        newly_settled=newly_lost,
                    )
                )
            continue

        redeemable += 1
        rows = _position_rows(conn, position)
        if not rows:
            skipped_unmatched += 1
            unmatched_details.append(
                f"token={_short_id(position.token_id)} condition={_short_id(position.condition_id)} "
                f"side={position.outcome} payout={_usd(position.current_value_usd)}"
            )
            logger.warning(
                "Redeemable wallet position has no matching bot ledger rows: %s %s %s",
                mask_address(wallet_address),
                position.condition_id,
                position.token_id,
            )
            continue

        size_matches, ledger_size, tolerance = _ledger_size_matches_position(
            position=position,
            rows=rows,
        )
        if ledger_size is None:
            failed += 1
            failed_details.append(
                f"token={_short_id(position.token_id)} cannot size-match ledger rows"
            )
            continue
        if not size_matches:
            blocked_manual += 1
            manual_details.append(
                f"token={_short_id(position.token_id)} side={position.outcome} "
                f"wallet_size={position.size:.4f} ledger_size={ledger_size:.4f}"
            )
            logger.warning(
                "Blocking auto-redeem for manual handling because wallet size does not match bot ledger size: wallet=%s condition=%s token=%s outcome=%s wallet_size=%.12g ledger_size=%.12g tolerance=%.12g",
                mask_address(wallet_address),
                position.condition_id,
                position.token_id,
                position.outcome,
                position.size,
                ledger_size,
                tolerance,
            )
            continue

        newly_settled, settlement_blocked = _settle_pending_rows(
            conn,
            position=position,
            rows=rows,
        )
        settled_rows += newly_settled
        if settlement_blocked:
            failed += settlement_blocked
            failed_details.append(
                f"token={_short_id(position.token_id)} settlement blocked for {settlement_blocked} row(s)"
            )
            continue
        matched_ledger_ids = [int(row["id"]) for row in rows]

        existing = _existing_nonfailed_request(conn, position)
        if existing is not None:
            skipped_existing += 1
            logger.info(
                "Redeemable position already has redemption request status=%s: %s",
                existing["status"],
                position.condition_id,
            )
            continue
        if submitted >= 1:
            deferred += 1
            logger.info(
                "Deferring extra auto-redeem submit until next scan: %s %s",
                position.condition_id,
                position.outcome,
            )
            continue

        request_id, existing_status = _insert_submitting_request(
            conn,
            position=position,
            matched_ledger_ids=matched_ledger_ids,
        )
        if request_id is None:
            skipped_existing += 1
            logger.info(
                "Redeemable position already has redemption request status=%s: %s",
                existing_status,
                position.condition_id,
            )
            continue

        try:
            response = submitter(
                relayer_url=getattr(config, "relayer_url", ""),
                api_key=_secret_value(config, "relayer_api_key"),
                api_key_address=getattr(config, "relayer_api_key_address", ""),
                private_key=_secret_value(config, "poly_private_key"),
                deposit_wallet=wallet_address,
                condition_id=position.condition_id,
                index_sets=[position.index_set],
                negative_risk=position.negative_risk,
            )
            _update_request_submitted(conn, request_id=request_id, response=response)
            submitted += 1
            submitted_details.append(
                _submitted_notification_detail(
                    conn,
                    request_id=request_id,
                    position=position,
                    matched_ledger_ids=matched_ledger_ids,
                    response=response,
                    newly_settled=newly_settled,
                )
            )
        except Exception as exc:
            _update_request_failed(conn, request_id=request_id, exc=exc)
            failed += 1
            failed_details.append(
                f"request={request_id} token={_short_id(position.token_id)} "
                f"{type(exc).__name__}: {exc}"
            )
            logger.error(
                "Auto-redeem submit failed for %s %s",
                position.condition_id,
                position.outcome,
                exc_info=True,
            )

    status = "OK" if failed == 0 else "WARNING"
    if blocked_manual:
        status = "WARNING"
    if skipped_unmatched and submitted == 0 and settled_rows == 0 and failed == 0:
        status = "WARNING"
    result = RedemptionScanResult(
        status=status,
        scanned=scanned,
        redeemable=redeemable,
        zero_payout=zero_payout,
        ignored_zero_payout=ignored_zero_payout,
        settled_rows=settled_rows,
        settled_losses=settled_losses,
        submitted=submitted,
        deferred=deferred,
        skipped_existing=skipped_existing,
        skipped_unmatched=skipped_unmatched,
        blocked_manual=blocked_manual,
        failed=failed,
    )
    message = _scan_message(result)
    log_pipeline_health(
        conn,
        "",
        "auto_redeem",
        "OK" if result.status == "OK" else "WARNING",
        message,
    )
    if notify and (submitted or settled_losses or failed or skipped_unmatched or blocked_manual):
        notify(
            "Polymarket auto-redeem",
            _notification_message(
                result,
                submitted_details=submitted_details,
                loss_details=loss_details,
                failed_details=failed_details,
                unmatched_details=unmatched_details,
                manual_details=manual_details,
            ),
        )
    return RedemptionScanResult(
        status=result.status,
        scanned=result.scanned,
        redeemable=result.redeemable,
        zero_payout=result.zero_payout,
        ignored_zero_payout=result.ignored_zero_payout,
        settled_rows=result.settled_rows,
        settled_losses=result.settled_losses,
        submitted=result.submitted,
        deferred=result.deferred,
        skipped_existing=result.skipped_existing,
        skipped_unmatched=result.skipped_unmatched,
        blocked_manual=result.blocked_manual,
        failed=result.failed,
        message=message,
    )


def latest_redemption_summary(
    conn: sqlite3.Connection,
    *,
    wallet_address: str,
    limit: int = 500,
) -> dict[str, Any]:
    empty = {
        "recent": [],
        "pendingCount": 0,
        "submittedCount": 0,
        "failedCount": 0,
        "zeroPayoutCount": 0,
        "pendingValueUsd": 0.0,
        "submittedValueUsd": 0.0,
        "inFlightRedemptionCount": 0,
        "inFlightRedemptionValueUsd": 0.0,
    }
    try:
        ensure_redemption_schema(conn)
    except RuntimeError:
        return empty

    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT id, created_at, updated_at, condition_id, token_id, outcome,
                   size, current_value_usd, status, relayer_tx_id, tx_hash, error,
                   ROW_NUMBER() OVER (
                       PARTITION BY lower(wallet_address), condition_id, token_id
                       ORDER BY
                           CASE status
                               WHEN 'CONFIRMED' THEN 0
                               WHEN 'SUBMITTED' THEN 1
                               WHEN 'SUBMITTING' THEN 2
                               ELSE 3
                           END,
                           id DESC
                   ) AS rn
            FROM redemption_requests
            WHERE lower(wallet_address) = lower(?)
        )
        SELECT created_at, updated_at, condition_id, token_id, outcome, size,
               current_value_usd, status, relayer_tx_id, tx_hash, error
        FROM ranked
        WHERE rn = 1
        ORDER BY id DESC
        LIMIT ?
        """,
        (wallet_address, int(limit)),
    ).fetchall()
    counts = conn.execute(
        """
        WITH ranked AS (
            SELECT status, current_value_usd, size,
                   ROW_NUMBER() OVER (
                       PARTITION BY lower(wallet_address), condition_id, token_id
                       ORDER BY
                           CASE status
                               WHEN 'CONFIRMED' THEN 0
                               WHEN 'SUBMITTED' THEN 1
                               WHEN 'SUBMITTING' THEN 2
                               ELSE 3
                           END,
                           id DESC
                   ) AS rn
            FROM redemption_requests
            WHERE lower(wallet_address) = lower(?)
        )
        SELECT
          SUM(CASE WHEN status = 'SUBMITTING' THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN status = 'SUBMITTED'
                    AND COALESCE(current_value_usd, 0) > 0
              THEN 1 ELSE 0 END) AS submitted,
          SUM(CASE WHEN status = 'SUBMITTED'
                    AND COALESCE(current_value_usd, 0) <= 0
              THEN 1 ELSE 0 END) AS zero_payout,
          SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
          SUM(CASE WHEN status = 'SUBMITTING'
              THEN COALESCE(current_value_usd, 0)
              ELSE 0 END) AS pending_value,
          SUM(CASE WHEN status = 'SUBMITTED'
                    AND COALESCE(current_value_usd, 0) > 0
              THEN COALESCE(current_value_usd, 0)
              ELSE 0 END) AS submitted_value
        FROM ranked
        WHERE rn = 1
        """,
        (wallet_address,),
    ).fetchone()
    pending_count = int(counts["pending"] or 0) if counts else 0
    submitted_count = int(counts["submitted"] or 0) if counts else 0
    zero_payout_count = int(counts["zero_payout"] or 0) if counts else 0
    pending_value = _safe_float(counts["pending_value"]) if counts else 0.0
    submitted_value = _safe_float(counts["submitted_value"]) if counts else 0.0

    def _display_status(row: sqlite3.Row) -> str:
        status = str(row["status"] or "")
        if status == "SUBMITTED" and _safe_float(row["current_value_usd"]) <= 0:
            return "ZERO_PAYOUT"
        return status

    return {
        "recent": [
            {
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "conditionId": row["condition_id"],
                "tokenId": row["token_id"],
                "outcome": row["outcome"],
                "size": round(_safe_float(row["size"]), 4),
                "currentValueUsd": round(_safe_float(row["current_value_usd"]), 2),
                "status": _display_status(row),
                "rawStatus": row["status"],
                "relayerTxId": row["relayer_tx_id"] or "",
                "txHash": row["tx_hash"] or "",
                "error": row["error"] or "",
            }
            for row in rows
        ],
        "pendingCount": pending_count,
        "submittedCount": submitted_count,
        "failedCount": int(counts["failed"] or 0) if counts else 0,
        "zeroPayoutCount": zero_payout_count,
        "pendingValueUsd": round(pending_value, 2),
        "submittedValueUsd": round(submitted_value, 2),
        "inFlightRedemptionCount": pending_count + submitted_count,
        "inFlightRedemptionValueUsd": round(pending_value + submitted_value, 2),
    }
