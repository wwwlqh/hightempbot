"""Wallet-first reconciliation snapshots for the live dashboard."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

from hightempbot.db.connection import (
    parse_utc_timestamp as _parse_ts,
    safe_float as _safe_float,
    utc_now_sql,
)
from hightempbot.execution.live_readiness import redact_operator_text
from hightempbot.execution.strategy_constants import REDEEMABLE_PAYOUT_VALUE_FRACTION
from hightempbot.persistence.ledger import decode_event_detail, poly_fee_charge
from hightempbot.polymarket.primitives import is_address, mask_address

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from hightempbot.runtime_config import Config

DATA_API_BASE = "https://data-api.polymarket.com"
WALLET_TRADE_PRICE_TOLERANCE_ABS = 0.01
WALLET_TRADE_PRICE_TOLERANCE_FRAC = 0.03
WALLET_TRADE_AMOUNT_TOLERANCE_ABS = 0.10
WALLET_TRADE_AMOUNT_TOLERANCE_FRAC = 0.03
WALLET_POSITION_RECONCILE_TOLERANCE_FRAC = 0.05
WALLET_POSITION_AMOUNT_TOLERANCE_ABS = 0.10


@dataclass(frozen=True)
class WalletRecord:
    source_layer: str
    record_type: str
    wallet_address: str
    source_id: str = ""
    order_id: str = ""
    tx_hash: str = ""
    token_id: str = ""
    amount_usd: float | None = None
    status: str = ""
    matched_ledger_id: int | None = None
    match_status: str = "unmatched"
    record: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WalletSnapshot:
    wallet_address: str
    sampled_at: str
    source_status: str
    clob_balance_usd: float | None = None
    chain_balance_usd: float | None = None
    open_orders_count: int = 0
    open_positions_count: int = 0
    # Data API count drives the dashboard; chain counts can include released zero-payout shares.
    data_api_open_positions_count: int = 0
    chain_open_positions_count: int | None = None
    data_api_open_positions_value_usd: float = 0.0
    data_api_open_positions_initial_value_usd: float = 0.0
    data_api_open_positions_cash_pnl_usd: float = 0.0
    data_api_trusted_open_positions_value_usd: float = 0.0
    data_api_trusted_open_positions_initial_value_usd: float = 0.0
    data_api_trusted_open_positions_cash_pnl_usd: float = 0.0
    data_api_redeemable_value_usd: float = 0.0
    data_api_matched_positions_count: int = 0
    data_api_mismatched_positions_count: int = 0
    data_api_unmatched_positions_count: int = 0
    data_api_reconciliation_warnings: list[str] = field(default_factory=list)
    data_api_transfer_blocking_warnings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    records: list[WalletRecord] = field(default_factory=list)
    sources_checked: dict[str, bool] = field(default_factory=dict)
    complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "walletAddress": self.wallet_address,
            "walletLabel": mask_address(self.wallet_address),
            "sampledAt": self.sampled_at,
            "sourceStatus": self.source_status,
            "clobBalanceUsd": self.clob_balance_usd,
            "chainBalanceUsd": self.chain_balance_usd,
            "openOrdersCount": self.open_orders_count,
            "openPositionsCount": self.open_positions_count,
            "dataApiOpenPositionsCount": self.data_api_open_positions_count,
            "chainOpenPositionsCount": self.chain_open_positions_count,
            "dataApiOpenPositionsValueUsd": round(self.data_api_open_positions_value_usd, 6),
            "dataApiOpenPositionsInitialValueUsd": round(
                self.data_api_open_positions_initial_value_usd, 6
            ),
            "dataApiOpenPositionsCashPnlUsd": round(self.data_api_open_positions_cash_pnl_usd, 6),
            "dataApiTrustedOpenPositionsValueUsd": round(
                self.data_api_trusted_open_positions_value_usd, 6
            ),
            "dataApiTrustedOpenPositionsInitialValueUsd": round(
                self.data_api_trusted_open_positions_initial_value_usd, 6
            ),
            "dataApiTrustedOpenPositionsCashPnlUsd": round(
                self.data_api_trusted_open_positions_cash_pnl_usd, 6
            ),
            "dataApiRedeemableValueUsd": round(self.data_api_redeemable_value_usd, 6),
            "dataApiMatchedPositionsCount": self.data_api_matched_positions_count,
            "dataApiMismatchedPositionsCount": self.data_api_mismatched_positions_count,
            "dataApiUnmatchedPositionsCount": self.data_api_unmatched_positions_count,
            "dataApiReconciliationWarnings": self.data_api_reconciliation_warnings,
            "dataApiTransferBlockingWarnings": self.data_api_transfer_blocking_warnings,
            "warnings": self.warnings,
            "records": [asdict(record) for record in self.records],
            "sourcesChecked": self.sources_checked,
            "complete": self.complete,
        }


def ensure_wallet_schema(conn: sqlite3.Connection) -> None:
    """Check the wallet tables exist (created by schema.sql)."""
    required = ("wallet_reconciliation_runs", "wallet_reconciliation_records")
    for table in required:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"wallet reconciliation table {table!r} is missing; run init_db "
                "(hightempbot.db.connection.init_db) to apply schema.sql"
            )


def fetch_data_api_wallet_records(
    wallet_address: str,
    *,
    limit: int = 500,
    timeout_s: float = 15.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch public Polymarket Data API trades and positions for a wallet."""
    if not is_address(wallet_address):
        raise ValueError("wallet_address must be a 0x-prefixed 40-byte address")
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError("requests is not installed") from exc

    trades_resp = requests.get(
        f"{DATA_API_BASE}/trades",
        params={"user": wallet_address, "limit": limit},
        timeout=timeout_s,
    )
    trades_resp.raise_for_status()
    positions_resp = requests.get(
        f"{DATA_API_BASE}/positions",
        params={"user": wallet_address, "limit": min(limit, 500), "sizeThreshold": 0},
        timeout=timeout_s,
    )
    positions_resp.raise_for_status()
    trades = trades_resp.json()
    positions = positions_resp.json()
    return (
        trades if isinstance(trades, list) else [],
        positions if isinstance(positions, list) else [],
    )


def _ledger_index(
    conn: sqlite3.Connection,
    *,
    need_orders: bool = True,
) -> tuple[dict[str, int], dict[str, int], list[sqlite3.Row]]:
    """Ledger lookup indexes; ``need_orders=False`` skips the order_id index."""
    order_to_id: dict[str, int] = {}
    tx_to_id: dict[str, int] = {}
    fill_rows: list[sqlite3.Row] = []
    try:
        rows = conn.execute(
            """
            SELECT id, order_id, transaction_hash, token_id, side,
                   fill_price, fill_size, fill_ts, bet_size
            FROM ledger
            WHERE event_type = 'bet'
            """
        ).fetchall()
    except sqlite3.Error:
        return order_to_id, tx_to_id, fill_rows
    for row in rows:
        if need_orders and row["order_id"]:
            order_to_id[str(row["order_id"])] = int(row["id"])
        if row["transaction_hash"]:
            tx_to_id[str(row["transaction_hash"]).lower()] = int(row["id"])
        if _safe_float(row["fill_price"]) > 0 and _safe_float(row["fill_size"]) > 0:
            fill_rows.append(row)
    return order_to_id, tx_to_id, fill_rows


def _ledger_fill_match(trade: dict[str, Any], fill_rows: Iterable[sqlite3.Row]) -> int | None:
    """Match Data API trades that omit order_id to recorded ledger fills."""
    token_id = str(trade.get("asset") or "")
    if not token_id:
        return None
    price = _safe_float(trade.get("price"))
    size = _safe_float(trade.get("size"))
    if price <= 0 or size <= 0:
        return None
    amount = price * size
    outcome = str(trade.get("outcome") or "").upper()
    trade_ts = _parse_ts(trade.get("timestamp") or trade.get("match_time") or trade.get("matchTime"))

    best: tuple[float, int] | None = None
    for row in fill_rows:
        if str(row["token_id"] or "") != token_id:
            continue
        ledger_side = str(row["side"] or "").upper()
        if outcome in {"YES", "NO"} and ledger_side and ledger_side != outcome:
            continue
        ledger_price = _safe_float(row["fill_price"])
        ledger_size = _safe_float(row["fill_size"])
        ledger_amount = _safe_float(row["bet_size"]) or ledger_price * ledger_size
        # Allow small price/amount drift: the Data API is more precise than the ledger.
        price_tolerance = max(
            WALLET_TRADE_PRICE_TOLERANCE_ABS,
            ledger_price * WALLET_TRADE_PRICE_TOLERANCE_FRAC,
        )
        if abs(price - ledger_price) > price_tolerance:
            continue
        if abs(size - ledger_size) > max(0.02, ledger_size * 0.005):
            continue
        amount_tolerance = max(
            WALLET_TRADE_AMOUNT_TOLERANCE_ABS,
            ledger_amount * WALLET_TRADE_AMOUNT_TOLERANCE_FRAC,
            ledger_size * price_tolerance,
        )
        if abs(amount - ledger_amount) > amount_tolerance:
            continue
        ledger_ts = _parse_ts(row["fill_ts"])
        time_score = 0.0
        if trade_ts is not None and ledger_ts is not None:
            time_score = abs((trade_ts - ledger_ts).total_seconds())
            if time_score > 10 * 60:
                continue
        score = time_score + abs(amount - ledger_amount)
        if best is None or score < best[0]:
            best = (score, int(row["id"]))
    return best[1] if best is not None else None


def _merge_json_detail(raw: str | None, updates: dict[str, Any]) -> str:
    detail = decode_event_detail(raw)
    detail.update(updates)
    return json.dumps(detail, sort_keys=True)


def _data_api_trade_side(trade: dict[str, Any]) -> str:
    outcome = str(trade.get("outcome") or "").upper()
    return outcome if outcome in {"YES", "NO"} else ""


def _data_api_order_surrogate(trade: dict[str, Any]) -> str:
    tx_hash = str(trade.get("transactionHash") or trade.get("transaction_hash") or "").lower()
    if tx_hash:
        return f"DATAAPI_{tx_hash}"
    token_id = str(trade.get("asset") or "")[-16:]
    timestamp = str(trade.get("timestamp") or trade.get("match_time") or "")
    return f"DATAAPI_{token_id}_{timestamp}"


def _data_api_position_side(position: dict[str, Any]) -> str:
    outcome = str(position.get("outcome") or "").upper()
    return outcome if outcome in {"YES", "NO"} else ""


def _data_api_position_condition_id(position: dict[str, Any]) -> str:
    return str(position.get("conditionId") or position.get("condition_id") or "").strip()


def _data_api_position_wallet_matches(position: dict[str, Any], wallet_address: str) -> bool:
    if not wallet_address:
        return True
    position_wallet = str(
        position.get("proxyWallet")
        or position.get("proxy_wallet")
        or position.get("wallet")
        or ""
    ).strip()
    return not position_wallet or position_wallet.lower() == wallet_address.lower()


def _position_initial_value(position: dict[str, Any]) -> float:
    initial_value = _safe_float(position.get("initialValue"))
    if initial_value > 0:
        return initial_value
    avg_price = _safe_float(position.get("avgPrice"))
    size = _safe_float(position.get("size"))
    return avg_price * size if avg_price > 0 and size > 0 else 0.0


def _position_current_value(position: dict[str, Any]) -> float:
    current_value = _safe_float(position.get("currentValue"))
    if current_value > 0:
        return current_value
    cur_price = _safe_float(position.get("curPrice"))
    size = _safe_float(position.get("size"))
    return cur_price * size if cur_price > 0 and size > 0 else 0.0


def _data_api_position_counts_as_open(record: WalletRecord) -> bool:
    if record.record_type != "position":
        return False
    position = record.record
    if position.get("redeemable") is True:
        return False
    if record.match_status == "api_position_resolved_matched":
        return False
    return True


def _relative_drift(a: float, b: float) -> float:
    denom = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / denom


def _ledger_position_groups(
    conn: sqlite3.Connection,
    *,
    outcomes: Iterable[str] = ("PENDING",),
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Aggregate ledger rows by wallet-position identity."""
    outcome_values = tuple(
        str(outcome or "").upper()
        for outcome in outcomes
        if str(outcome or "").strip()
    )
    if not outcome_values:
        return {}
    placeholders = ", ".join("?" for _ in outcome_values)
    try:
        rows = conn.execute(
            f"""
            SELECT id, market_id, token_id, side, bet_size, fill_price,
                   fill_size, order_id, outcome
            FROM ledger
            WHERE event_type = 'bet'
              AND UPPER(outcome) IN ({placeholders})
              AND station_id != 'RECOVERED'
            """,
            outcome_values,
        ).fetchall()
    except sqlite3.Error:
        return {}

    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        token_id = str(row["token_id"] or "")
        condition_id = str(row["market_id"] or "")
        side = str(row["side"] or "").upper()
        if not token_id or not condition_id or side not in {"YES", "NO"}:
            continue
        key = (token_id, condition_id, side)
        bet_size = _safe_float(row["bet_size"])
        fill_price = _safe_float(row["fill_price"])
        fill_size = _safe_float(row["fill_size"])
        if fill_size <= 0 and fill_price > 0 and bet_size > 0:
            fill_size = bet_size / fill_price
        group = groups.setdefault(
            key,
            {
                "ledgerIds": [],
                "ledgerSize": 0.0,
                "ledgerNotional": 0.0,
                "ledgerVwap": 0.0,
                "submittedRows": 0,
                "unconfirmedRows": 0,
                "outcomes": set(),
            },
        )
        group["ledgerIds"].append(int(row["id"]))
        group["ledgerSize"] += fill_size
        group["ledgerNotional"] += bet_size
        group["outcomes"].add(str(row["outcome"] or "").upper())
        if row["order_id"]:
            group["submittedRows"] += 1
        else:
            group["unconfirmedRows"] += 1

    for group in groups.values():
        ledger_size = float(group["ledgerSize"] or 0.0)
        ledger_notional = float(group["ledgerNotional"] or 0.0)
        group["ledgerVwap"] = ledger_notional / ledger_size if ledger_size > 0 else 0.0
    return groups


def _position_ledger_match(
    position: dict[str, Any],
    groups: dict[tuple[str, str, str], dict[str, Any]],
    resolved_groups: dict[tuple[str, str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    token_id = str(position.get("asset") or "")
    condition_id = _data_api_position_condition_id(position)
    side = _data_api_position_side(position)
    api_size = _safe_float(position.get("size"))
    api_initial = _position_initial_value(position)
    api_avg_price = _safe_float(position.get("avgPrice"))
    if api_avg_price <= 0 and api_size > 0 and api_initial > 0:
        api_avg_price = api_initial / api_size

    base = {
        "tokenId": token_id,
        "conditionId": condition_id,
        "side": side,
        "apiSize": api_size,
        "apiInitialValueUsd": api_initial,
        "apiCurrentValueUsd": _position_current_value(position),
        "apiCashPnlUsd": _safe_float(position.get("cashPnl")),
        "apiCurPrice": _safe_float(position.get("curPrice")),
        "apiAvgPrice": api_avg_price,
        "ledgerIds": [],
        "ledgerSize": 0.0,
        "ledgerNotionalUsd": 0.0,
        "ledgerVwap": 0.0,
        "maxDriftPct": None,
        "status": "api_position_unmatched",
        "trusted": False,
        "reminder": "API position has no matching local ledger row",
    }
    if not token_id or not condition_id or side not in {"YES", "NO"} or api_size <= 0:
        base["reminder"] = "API position is missing token, condition, side, or size"
        return base

    key = (token_id, condition_id, side)
    group = groups.get(key)
    resolved_match = False
    if group is None and resolved_groups is not None:
        group = resolved_groups.get(key)
        resolved_match = group is not None
    if group is None:
        return base

    ledger_size = float(group["ledgerSize"] or 0.0)
    ledger_notional = float(group["ledgerNotional"] or 0.0)
    ledger_vwap = float(group["ledgerVwap"] or 0.0)
    drifts = []
    if ledger_size > 0:
        drifts.append(_relative_drift(api_size, ledger_size))
    if ledger_notional > 0 and api_initial > 0:
        drifts.append(_relative_drift(api_initial, ledger_notional))
    if ledger_vwap > 0 and api_avg_price > 0:
        drifts.append(_relative_drift(api_avg_price, ledger_vwap))
    max_drift = max(drifts) if drifts else 1.0
    base.update(
        {
            "ledgerIds": list(group["ledgerIds"]),
            "ledgerSize": ledger_size,
            "ledgerNotionalUsd": ledger_notional,
            "ledgerVwap": ledger_vwap,
            "ledgerOutcomes": sorted(str(o) for o in group.get("outcomes", set()) if o),
            "maxDriftPct": max_drift * 100.0,
        }
    )
    amount_tolerance = max(
        WALLET_POSITION_AMOUNT_TOLERANCE_ABS,
        ledger_notional * WALLET_POSITION_RECONCILE_TOLERANCE_FRAC,
    )
    amount_ok = (
        api_initial <= 0
        or ledger_notional <= 0
        or abs(api_initial - ledger_notional) <= amount_tolerance
    )
    if max_drift <= WALLET_POSITION_RECONCILE_TOLERANCE_FRAC and amount_ok:
        base["status"] = (
            "api_position_resolved_matched" if resolved_match else "api_position_matched"
        )
        base["trusted"] = not resolved_match
        base["reminder"] = ""
    else:
        base["status"] = "api_position_mismatch"
        base["reminder"] = "API/ledger value mismatch"
    return base


def _backfill_no_order_pending_from_data_api(
    conn: sqlite3.Connection,
    trades: Iterable[dict[str, Any]],
) -> int:
    """Fill in PENDING rows left without order/fill data by a restart, matching
    Data API trades on token, side, notional and time."""
    updated = 0
    used_ids: set[int] = set()
    for trade in trades:
        if str(trade.get("side") or "").upper() != "BUY":
            continue
        token_id = str(trade.get("asset") or "")
        side = _data_api_trade_side(trade)
        price = _safe_float(trade.get("price"))
        size = _safe_float(trade.get("size"))
        if not token_id or not side or price <= 0 or size <= 0:
            continue
        amount = price * size
        trade_ts = _parse_ts(
            trade.get("timestamp") or trade.get("match_time") or trade.get("matchTime")
        )
        rows = conn.execute(
            """
            SELECT id, bet_ts, bet_size, limit_price, p_market, edge, event_detail
            FROM ledger
            WHERE event_type = 'bet'
              AND outcome = 'PENDING'
              AND order_id IS NULL
              AND fill_price IS NULL
              AND fill_size IS NULL
              AND token_id = ?
              AND UPPER(side) = ?
            """,
            (token_id, side),
        ).fetchall()
        best: tuple[float, sqlite3.Row] | None = None
        for row in rows:
            row_id = int(row["id"])
            if row_id in used_ids:
                continue
            ledger_amount = _safe_float(row["bet_size"])
            price_anchor = _safe_float(row["limit_price"]) or _safe_float(row["p_market"])
            price_tolerance = max(
                WALLET_TRADE_PRICE_TOLERANCE_ABS,
                price_anchor * WALLET_TRADE_PRICE_TOLERANCE_FRAC,
            )
            if price_anchor > 0 and abs(price - price_anchor) > price_tolerance:
                continue
            amount_tolerance = max(
                WALLET_TRADE_AMOUNT_TOLERANCE_ABS,
                ledger_amount * WALLET_TRADE_AMOUNT_TOLERANCE_FRAC,
                size * price_tolerance,
            )
            if abs(amount - ledger_amount) > amount_tolerance:
                continue
            bet_ts = _parse_ts(row["bet_ts"])
            time_score = 0.0
            if trade_ts is not None and bet_ts is not None:
                time_score = abs((trade_ts - bet_ts).total_seconds())
                if time_score > 30 * 60:
                    continue
            score = time_score + abs(amount - ledger_amount)
            if best is None or score < best[0]:
                best = (score, row)
        if best is None:
            continue
        row = best[1]
        row_id = int(row["id"])
        tx_hash = str(trade.get("transactionHash") or trade.get("transaction_hash") or "")
        fill_ts = (
            trade_ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            if trade_ts is not None
            else utc_now_sql()
        )
        detail = _merge_json_detail(
            row["event_detail"],
            {
                "wallet_fill_backfill": True,
                "wallet_fill_backfilled_at": utc_now_sql(),
                "wallet_fill_source": "polymarket_data_api",
                "wallet_fill_reason": "pending_first_interrupted_after_submit",
            },
        )
        try:
            conn.execute(
                """
                UPDATE ledger
                SET order_id = ?,
                    fill_price = ?,
                    fill_size = ?,
                    fill_ts = ?,
                    bet_size = ?,
                    kelly_size = ?,
                    realized_edge = COALESCE(realized_edge, edge),
                    transaction_hash = COALESCE(?, transaction_hash),
                    event_detail = ?
                WHERE id = ?
                  AND outcome = 'PENDING'
                  AND order_id IS NULL
                  AND fill_price IS NULL
                  AND fill_size IS NULL
                """,
                (
                    _data_api_order_surrogate(trade),
                    price,
                    size,
                    fill_ts,
                    amount,
                    amount,
                    tx_hash or None,
                    detail,
                    row_id,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            logger.warning("wallet fill backfill skipped duplicate trade for ledger id=%s", row_id)
            continue
        updated += 1
        used_ids.add(row_id)
    return updated


def _recomputed_terminal_pnl(
    row: sqlite3.Row,
    detail: dict[str, Any],
    *,
    amount: float,
    price: float,
    size: float,
) -> tuple[float, dict[str, Any]] | None:
    outcome = str(row["outcome"] or "").upper()
    if outcome == "PENDING":
        return None
    entry_fee = poly_fee_charge(price, size)
    exit_fee = 0.0
    if outcome == "WIN":
        resolution_price = _safe_float(detail.get("resolution_price")) or 1.0
        pnl_gross = size * resolution_price - amount
    elif outcome == "LOSS":
        pnl_gross = -amount
    elif outcome == "PUSH":
        pnl_gross = 0.0
    elif outcome == "CLOSED":
        close_price = _safe_float(detail.get("close_price")) or _safe_float(
            detail.get("close_vwap_quote")
        )
        close_size = _safe_float(detail.get("close_size")) or size
        if close_price <= 0 or close_size <= 0:
            return None
        pnl_gross = close_price * close_size - amount
        exit_fee = poly_fee_charge(close_price, close_size)
    else:
        return None
    updates = {
        "pnl_gross": pnl_gross,
        "poly_entry_fee": entry_fee,
        "poly_exit_fee": exit_fee,
        "wallet_fill_reconcile_recomputed_pnl": True,
    }
    return pnl_gross - entry_fee - exit_fee, updates


def _reconcile_open_rows_from_data_api_positions(
    conn: sqlite3.Connection,
    positions: Iterable[dict[str, Any]],
    *,
    wallet_address: str = "",
) -> int:
    """Copy the Data API cost basis into open ledger rows when token/side match
    exactly and the amounts agree within 5%."""
    updated = 0
    seen_keys: set[tuple[str, str, str]] = set()
    for position in positions:
        if not _data_api_position_wallet_matches(position, wallet_address):
            continue
        token_id = str(position.get("asset") or "")
        condition_id = _data_api_position_condition_id(position)
        side = _data_api_position_side(position)
        api_size = _safe_float(position.get("size"))
        api_initial = _position_initial_value(position)
        if not token_id or not condition_id or side not in {"YES", "NO"}:
            continue
        if api_size <= 0 or api_initial <= 0:
            continue
        key = (token_id, condition_id, side)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        rows = conn.execute(
            """
            SELECT id, bet_size, kelly_size, fill_price, fill_size, event_detail
            FROM ledger
            WHERE event_type = 'bet'
              AND outcome = 'PENDING'
              AND station_id != 'RECOVERED'
              AND token_id = ?
              AND market_id = ?
              AND UPPER(side) = ?
            ORDER BY id
            """,
            (token_id, condition_id, side),
        ).fetchall()
        if not rows:
            continue

        row_infos: list[dict[str, Any]] = []
        ledger_size = 0.0
        ledger_notional = 0.0
        for row in rows:
            bet_size = _safe_float(row["bet_size"])
            fill_price = _safe_float(row["fill_price"])
            fill_size = _safe_float(row["fill_size"])
            if fill_size <= 0 and fill_price > 0 and bet_size > 0:
                fill_size = bet_size / fill_price
            if fill_size <= 0:
                row_infos = []
                break
            ledger_size += fill_size
            ledger_notional += bet_size
            row_infos.append(
                {
                    "row": row,
                    "bet_size": bet_size,
                    "fill_price": fill_price,
                    "fill_size": fill_size,
                }
            )
        if not row_infos or ledger_size <= 0 or ledger_notional <= 0:
            continue

        api_avg_price = _safe_float(position.get("avgPrice"))
        if api_avg_price <= 0:
            api_avg_price = api_initial / api_size
        ledger_vwap = ledger_notional / ledger_size
        drifts = [
            _relative_drift(api_size, ledger_size),
            _relative_drift(api_initial, ledger_notional),
        ]
        if api_avg_price > 0 and ledger_vwap > 0:
            drifts.append(_relative_drift(api_avg_price, ledger_vwap))
        if max(drifts) > WALLET_POSITION_RECONCILE_TOLERANCE_FRAC:
            continue
        if abs(api_initial - ledger_notional) <= 0.005:
            continue

        reconciled_at = utc_now_sql()
        changed = 0
        for info in row_infos:
            row = info["row"]
            fill_size = float(info["fill_size"])
            share = fill_size / ledger_size
            next_amount = api_initial * share
            next_price = next_amount / fill_size
            if abs(_safe_float(row["bet_size"]) - next_amount) <= 0.005:
                continue
            detail = _merge_json_detail(
                row["event_detail"],
                {
                    "wallet_position_reconciled": True,
                    "wallet_position_reconciled_at": reconciled_at,
                    "wallet_position_reconcile_source": "polymarket_data_api_position",
                    "wallet_position_reconcile_previous_fill_price": info["fill_price"],
                    "wallet_position_reconcile_previous_bet_size": info["bet_size"],
                    "wallet_position_reconcile_previous_kelly_size": _safe_float(
                        row["kelly_size"]
                    ),
                    "wallet_position_reconcile_api_initial_value": api_initial,
                    "wallet_position_reconcile_api_current_value": _position_current_value(
                        position
                    ),
                    "wallet_position_reconcile_api_cash_pnl": _safe_float(
                        position.get("cashPnl")
                    ),
                    "wallet_position_reconcile_api_avg_price": api_avg_price,
                    "wallet_position_reconcile_api_size": api_size,
                },
            )
            cur = conn.execute(
                """
                UPDATE ledger
                SET fill_price = ?,
                    bet_size = ?,
                    kelly_size = ?,
                    event_detail = ?
                WHERE id = ?
                  AND outcome = 'PENDING'
                """,
                (next_price, next_amount, next_amount, detail, int(row["id"])),
            )
            changed += cur.rowcount
        if changed:
            conn.commit()
            updated += changed
    return updated


def _backfill_missing_tx_hash_from_data_api(
    conn: sqlite3.Connection,
    trades: Iterable[dict[str, Any]],
) -> int:
    """Attach Data API tx hashes to already-filled ledger rows when safe."""
    _, tx_to_id, fill_rows = _ledger_index(conn, need_orders=False)
    updated = 0
    used_ids: set[int] = set()
    for trade in trades:
        tx_hash = str(trade.get("transactionHash") or trade.get("transaction_hash") or "")
        if not tx_hash or tx_hash.lower() in tx_to_id:
            continue
        row_id = _ledger_fill_match(trade, fill_rows)
        if row_id is None or row_id in used_ids:
            continue
        row = conn.execute(
            "SELECT transaction_hash, event_detail FROM ledger WHERE id = ?",
            (row_id,),
        ).fetchone()
        if row is None or row["transaction_hash"]:
            continue
        detail = _merge_json_detail(
            row["event_detail"],
            {
                "wallet_tx_hash_backfill": True,
                "wallet_tx_hash_backfilled_at": utc_now_sql(),
                "wallet_tx_hash_source": "polymarket_data_api",
            },
        )
        cur = conn.execute(
            """
            UPDATE ledger
            SET transaction_hash = ?,
                event_detail = ?
            WHERE id = ?
              AND transaction_hash IS NULL
            """,
            (tx_hash, detail, row_id),
        )
        conn.commit()
        if cur.rowcount:
            updated += 1
            used_ids.add(row_id)
            tx_to_id[tx_hash.lower()] = row_id
    return updated


def _reconcile_filled_rows_from_data_api_trades(
    conn: sqlite3.Connection,
    trades: Iterable[dict[str, Any]],
) -> int:
    """Replace a filled row's limit-price cost with the Data API trade VWAP,
    when the trade matches by tx hash or strict token/side/size/time."""
    _, tx_to_id, fill_rows = _ledger_index(conn, need_orders=False)
    updated = 0
    used_ids: set[int] = set()
    for trade in trades:
        if str(trade.get("side") or "").upper() != "BUY":
            continue
        token_id = str(trade.get("asset") or "")
        side = _data_api_trade_side(trade)
        price = _safe_float(trade.get("price"))
        size = _safe_float(trade.get("size"))
        if not token_id or side not in {"YES", "NO"} or price <= 0 or size <= 0:
            continue
        amount = price * size
        tx_hash = str(trade.get("transactionHash") or trade.get("transaction_hash") or "")
        row_id = tx_to_id.get(tx_hash.lower()) if tx_hash else None
        if row_id is None:
            row_id = _ledger_fill_match(trade, fill_rows)
        if row_id is None or row_id in used_ids:
            continue
        row = conn.execute(
            """
            SELECT id, token_id, side, bet_size, kelly_size, fill_price, fill_size,
                   outcome, pnl, event_detail
            FROM ledger
            WHERE id = ?
              AND event_type = 'bet'
              AND outcome NOT IN ('CANCELLED', 'EXPIRED')
            """,
            (row_id,),
        ).fetchone()
        if row is None:
            continue
        if str(row["token_id"] or "") != token_id:
            continue
        if str(row["side"] or "").upper() != side:
            continue
        ledger_size = _safe_float(row["fill_size"])
        if ledger_size <= 0 or abs(ledger_size - size) > max(0.02, ledger_size * 0.005):
            continue
        ledger_price = _safe_float(row["fill_price"])
        ledger_amount = _safe_float(row["bet_size"])
        raw_detail = row["event_detail"]
        detail_obj = decode_event_detail(raw_detail)
        recomputed = _recomputed_terminal_pnl(
            row,
            detail_obj,
            amount=amount,
            price=price,
            size=size,
        )
        aligned = (
            abs(ledger_price - price) <= 1e-9
            and abs(ledger_amount - amount) <= 0.005
        )
        if (
            aligned
            and (
                recomputed is None
                or (
                    abs(_safe_float(row["pnl"]) - recomputed[0]) <= 0.005
                    and detail_obj.get("wallet_fill_reconcile_recomputed_pnl") is True
                )
            )
        ):
            continue
        detail_updates: dict[str, Any] = {
            "wallet_fill_reconciled": True,
            "wallet_fill_reconciled_at": utc_now_sql(),
            "wallet_fill_reconcile_source": "polymarket_data_api_trade",
            "wallet_fill_reconcile_previous_fill_price": ledger_price,
            "wallet_fill_reconcile_previous_bet_size": ledger_amount,
            "wallet_fill_reconcile_previous_kelly_size": _safe_float(row["kelly_size"]),
            "wallet_fill_reconcile_previous_pnl": _safe_float(row["pnl"]),
            "wallet_fill_reconcile_price": price,
            "wallet_fill_reconcile_size": size,
            "wallet_fill_reconcile_amount": amount,
            "wallet_fill_reconcile_tx_hash": tx_hash,
        }
        next_pnl: float | None = None
        if recomputed is not None:
            next_pnl, pnl_updates = recomputed
            detail_updates.update(pnl_updates)
        detail = _merge_json_detail(raw_detail, detail_updates)
        if next_pnl is None:
            conn.execute(
                """
                UPDATE ledger
                SET fill_price = ?,
                    bet_size = ?,
                    kelly_size = ?,
                    event_detail = ?
                WHERE id = ?
                  AND outcome NOT IN ('CANCELLED', 'EXPIRED')
                """,
                (price, amount, amount, detail, row_id),
            )
        else:
            conn.execute(
                """
                UPDATE ledger
                SET fill_price = ?,
                    bet_size = ?,
                    kelly_size = ?,
                    pnl = ?,
                    event_detail = ?
                WHERE id = ?
                  AND outcome NOT IN ('CANCELLED', 'EXPIRED')
                """,
                (price, amount, amount, next_pnl, detail, row_id),
            )
        conn.commit()
        updated += 1
        used_ids.add(row_id)
    return updated


def _records_from_data_api(
    conn: sqlite3.Connection,
    wallet_address: str,
    *,
    trades: Iterable[dict[str, Any]] = (),
    positions: Iterable[dict[str, Any]] = (),
) -> list[WalletRecord]:
    _, tx_to_id, fill_rows = _ledger_index(conn, need_orders=False)
    position_groups = _ledger_position_groups(conn)
    resolved_position_groups = _ledger_position_groups(
        conn,
        outcomes=("WIN", "LOSS", "PUSH", "CLOSED"),
    )
    records: list[WalletRecord] = []

    for trade in trades:
        tx_hash = str(trade.get("transactionHash") or "")
        ledger_id = tx_to_id.get(tx_hash.lower()) if tx_hash else None
        if ledger_id is None:
            ledger_id = _ledger_fill_match(trade, fill_rows)
        records.append(
            WalletRecord(
                source_layer="data_api",
                record_type="trade",
                wallet_address=wallet_address,
                source_id=tx_hash or str(trade.get("asset") or ""),
                tx_hash=tx_hash,
                token_id=str(trade.get("asset") or ""),
                amount_usd=_safe_float(trade.get("size")) * _safe_float(trade.get("price")),
                status=str(trade.get("side") or ""),
                matched_ledger_id=ledger_id,
                match_status="exact" if ledger_id is not None else "orphan_wallet_trade",
                record=dict(trade),
            )
        )

    for position in positions:
        size = _safe_float(position.get("size"))
        if size <= 0:
            continue
        if not _data_api_position_wallet_matches(position, wallet_address):
            continue
        match = _position_ledger_match(
            position,
            position_groups,
            resolved_groups=resolved_position_groups,
        )
        position_record = dict(position)
        position_record["ledgerMatch"] = match
        matched_ids = match.get("ledgerIds") if isinstance(match.get("ledgerIds"), list) else []
        matched_ledger_id = int(matched_ids[0]) if matched_ids else None
        records.append(
            WalletRecord(
                source_layer="data_api",
                record_type="position",
                wallet_address=wallet_address,
                source_id=str(position.get("asset") or ""),
                token_id=str(position.get("asset") or ""),
                amount_usd=match.get("apiCurrentValueUsd") or _position_current_value(position),
                status="open",
                matched_ledger_id=matched_ledger_id,
                match_status=str(match.get("status") or "api_position_unmatched"),
                record=position_record,
            )
        )
    return records


def build_wallet_snapshot(
    conn: sqlite3.Connection,
    *,
    wallet_address: str,
    clob_balance_usd: float | None = None,
    chain_balance_usd: float | None = None,
    data_api_trades: Iterable[dict[str, Any]] | None = None,
    data_api_positions: Iterable[dict[str, Any]] | None = None,
    open_orders: Iterable[dict[str, Any]] | None = None,
    source_status: str = "read_only",
    tolerance_usd: float = 0.25,
    chain_balance_required: bool = False,
    extra_warnings: Iterable[str] = (),
    chain_open_positions_count: int | None = None,
) -> WalletSnapshot:
    ensure_wallet_schema(conn)
    warnings: list[str] = []
    trades_list = list(data_api_trades) if data_api_trades is not None else []
    positions_list = list(data_api_positions) if data_api_positions is not None else []
    open_orders_list = list(open_orders) if open_orders is not None else []
    sources_checked = {
        "clobBalance": clob_balance_usd is not None,
        "chainBalance": chain_balance_usd is not None,
        "clobOpenOrders": open_orders is not None,
        "dataApiTrades": data_api_trades is not None,
        "dataApiPositions": data_api_positions is not None,
        "chainOpenPositions": chain_open_positions_count is not None,
    }
    records = _records_from_data_api(
        conn,
        wallet_address,
        trades=trades_list,
        positions=positions_list,
    )
    open_orders_count = len(open_orders_list)
    data_api_open_positions_count = 0
    data_api_open_positions_value_usd = 0.0
    data_api_open_positions_initial_value_usd = 0.0
    data_api_open_positions_cash_pnl_usd = 0.0
    data_api_trusted_open_positions_value_usd = 0.0
    data_api_trusted_open_positions_initial_value_usd = 0.0
    data_api_trusted_open_positions_cash_pnl_usd = 0.0
    data_api_redeemable_value_usd = 0.0
    data_api_matched_positions_count = 0
    data_api_mismatched_positions_count = 0
    data_api_unmatched_positions_count = 0
    data_api_reconciliation_warnings: list[str] = []
    data_api_transfer_blocking_warnings: list[str] = []
    for record in records:
        if record.record_type != "position":
            continue
        raw = record.record
        match = raw.get("ledgerMatch") if isinstance(raw.get("ledgerMatch"), dict) else {}
        current_value = _safe_float(match.get("apiCurrentValueUsd")) or _position_current_value(raw)
        initial_value = _safe_float(match.get("apiInitialValueUsd")) or _position_initial_value(raw)
        cash_pnl = _safe_float(match.get("apiCashPnlUsd"))
        counts_as_open = _data_api_position_counts_as_open(record)
        if counts_as_open:
            data_api_open_positions_count += 1
            data_api_open_positions_value_usd += current_value
            data_api_open_positions_initial_value_usd += initial_value
            data_api_open_positions_cash_pnl_usd += cash_pnl
        size = _safe_float(raw.get("size"))
        if (
            raw.get("redeemable") is True
            and size > 0
            and current_value >= size * REDEEMABLE_PAYOUT_VALUE_FRACTION
        ):
            data_api_redeemable_value_usd += current_value
        if record.match_status == "api_position_matched":
            data_api_matched_positions_count += 1
            data_api_trusted_open_positions_value_usd += current_value
            data_api_trusted_open_positions_initial_value_usd += initial_value
            data_api_trusted_open_positions_cash_pnl_usd += cash_pnl
        elif record.match_status == "api_position_resolved_matched":
            data_api_matched_positions_count += 1
        elif record.match_status == "api_position_mismatch":
            data_api_mismatched_positions_count += 1
            drift = match.get("maxDriftPct")
            drift_text = f" ({float(drift):.1f}% drift)" if isinstance(drift, (int, float)) else ""
            message = (
                f"API/ledger value mismatch for token {record.token_id}{drift_text}; "
                "dashboard uses API value and excludes it from trusted capital."
            )
            data_api_reconciliation_warnings.append(message)
            if counts_as_open:
                data_api_transfer_blocking_warnings.append(message)
        elif record.match_status == "api_position_unmatched":
            data_api_unmatched_positions_count += 1
            message = f"Data API position for token {record.token_id} has no matching local ledger row."
            data_api_reconciliation_warnings.append(message)
            if counts_as_open:
                data_api_transfer_blocking_warnings.append(message)
    if data_api_positions is not None:
        open_positions_count = data_api_open_positions_count
        if (
            chain_open_positions_count is not None
            and int(chain_open_positions_count) != data_api_open_positions_count
        ):
            data_api_reconciliation_warnings.append(
                "Data API released/open state drives the dashboard count "
                f"(api={data_api_open_positions_count}, chain={int(chain_open_positions_count)})."
            )
    elif chain_open_positions_count is None:
        # Chain position counts aren't wired; use the Data API count.
        open_positions_count = data_api_open_positions_count
    else:
        open_positions_count = int(chain_open_positions_count)

    if clob_balance_usd is None:
        warnings.append("CLOB pUSD balance was not checked; live actions disabled until refreshed.")

    if clob_balance_usd is not None and chain_balance_usd is not None:
        diff = abs(float(clob_balance_usd) - float(chain_balance_usd))
        if diff > tolerance_usd:
            warnings.append(
                f"CLOB balance and on-chain pUSD differ by ${diff:.2f}; live actions disabled until refreshed."
            )
    elif chain_balance_required:
        warnings.append("On-chain pUSD balance was not checked; live actions disabled until refreshed.")

    if open_orders is None:
        warnings.append("CLOB open orders were not checked; live actions disabled until refreshed.")
    if data_api_positions is None:
        warnings.append("Data API positions were not checked; live actions disabled until refreshed.")

    orphan_trades = [r for r in records if r.match_status == "orphan_wallet_trade"]
    if orphan_trades:
        warnings.append(f"{len(orphan_trades)} wallet trade(s) have no matching local ledger row.")
    warnings.extend(str(w) for w in extra_warnings if str(w))
    # Redact secrets from warning text.
    warnings = [redact_operator_text(w) for w in warnings]
    data_api_reconciliation_warnings = [
        redact_operator_text(w) for w in data_api_reconciliation_warnings
    ]
    data_api_transfer_blocking_warnings = [
        redact_operator_text(w) for w in data_api_transfer_blocking_warnings
    ]
    complete = (
        sources_checked["clobBalance"]
        and sources_checked["clobOpenOrders"]
        and sources_checked["dataApiPositions"]
        and (sources_checked["chainBalance"] or not chain_balance_required)
    )

    return WalletSnapshot(
        wallet_address=wallet_address,
        sampled_at=utc_now_sql(),
        source_status=source_status,
        clob_balance_usd=clob_balance_usd,
        chain_balance_usd=chain_balance_usd,
        open_orders_count=open_orders_count,
        open_positions_count=open_positions_count,
        data_api_open_positions_count=data_api_open_positions_count,
        chain_open_positions_count=chain_open_positions_count,
        data_api_open_positions_value_usd=data_api_open_positions_value_usd,
        data_api_open_positions_initial_value_usd=data_api_open_positions_initial_value_usd,
        data_api_open_positions_cash_pnl_usd=data_api_open_positions_cash_pnl_usd,
        data_api_trusted_open_positions_value_usd=data_api_trusted_open_positions_value_usd,
        data_api_trusted_open_positions_initial_value_usd=data_api_trusted_open_positions_initial_value_usd,
        data_api_trusted_open_positions_cash_pnl_usd=data_api_trusted_open_positions_cash_pnl_usd,
        data_api_redeemable_value_usd=data_api_redeemable_value_usd,
        data_api_matched_positions_count=data_api_matched_positions_count,
        data_api_mismatched_positions_count=data_api_mismatched_positions_count,
        data_api_unmatched_positions_count=data_api_unmatched_positions_count,
        data_api_reconciliation_warnings=data_api_reconciliation_warnings,
        data_api_transfer_blocking_warnings=data_api_transfer_blocking_warnings,
        warnings=warnings,
        records=records,
        sources_checked=sources_checked,
        complete=complete,
    )


def _snapshot_state_key(raw: object) -> str | None:
    """Stored snapshot without ``sampledAt``, for comparison; None if unreadable."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return json.dumps(
            {k: v for k, v in parsed.items() if k != "sampledAt"},
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return None


def record_wallet_snapshot(conn: sqlite3.Connection, snapshot: WalletSnapshot) -> int:
    """Save a wallet snapshot. If the state is unchanged from the newest row,
    update that row's timestamp instead of inserting. Returns the row id."""
    ensure_wallet_schema(conn)
    payload = snapshot.to_dict()
    state_key = json.dumps(
        {k: v for k, v in payload.items() if k != "sampledAt"},
        sort_keys=True,
    )
    snapshot_json = json.dumps(payload, sort_keys=True)
    warnings_json = json.dumps(snapshot.warnings)

    # Lock for the read-modify-write unless the caller already has a transaction.
    own_txn = not conn.in_transaction
    try:
        if own_txn:
            conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            """
            SELECT id, snapshot_json
            FROM wallet_reconciliation_runs
            WHERE lower(wallet_address) = lower(?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (snapshot.wallet_address,),
        ).fetchone()
        if previous is not None and _snapshot_state_key(previous["snapshot_json"]) == state_key:
            run_id = int(previous["id"])
            conn.execute(
                """
                UPDATE wallet_reconciliation_runs
                SET sampled_at = ?,
                    source_status = ?,
                    clob_balance_usd = ?,
                    chain_balance_usd = ?,
                    open_orders_count = ?,
                    open_positions_count = ?,
                    warnings_json = ?,
                    snapshot_json = ?
                WHERE id = ?
                """,
                (
                    snapshot.sampled_at,
                    snapshot.source_status,
                    snapshot.clob_balance_usd,
                    snapshot.chain_balance_usd,
                    snapshot.open_orders_count,
                    snapshot.open_positions_count,
                    warnings_json,
                    snapshot_json,
                    run_id,
                ),
            )
            conn.commit()
            return run_id

        cur = conn.execute(
            """
            INSERT INTO wallet_reconciliation_runs
            (sampled_at, wallet_address, source_status, clob_balance_usd,
             chain_balance_usd, open_orders_count, open_positions_count,
             warnings_json, snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.sampled_at,
                snapshot.wallet_address,
                snapshot.source_status,
                snapshot.clob_balance_usd,
                snapshot.chain_balance_usd,
                snapshot.open_orders_count,
                snapshot.open_positions_count,
                warnings_json,
                snapshot_json,
            ),
        )
        run_id = int(cur.lastrowid)
        # Per-record rows are no longer written (nothing read them).
        conn.commit()
        return run_id
    except Exception:
        if own_txn and conn.in_transaction:
            conn.rollback()
        raise


def _open_orders_for_client(order_client: object) -> list[dict[str, Any]]:
    raw_client = getattr(order_client, "_client", None)
    get_open_orders = getattr(raw_client, "get_open_orders", None)
    if not callable(get_open_orders):
        raise RuntimeError("OrderClient has no get_open_orders method")
    with_timeout = getattr(order_client, "_with_timeout", None)
    orders = with_timeout(get_open_orders) if callable(with_timeout) else get_open_orders()
    return orders if isinstance(orders, list) else []


def _sync_clob_collateral_balance(order_client: object) -> bool:
    sync_method = getattr(order_client, "sync_collateral_balance", None)
    if callable(sync_method):
        return bool(sync_method())
    raw_client = getattr(order_client, "_client", None)
    update_balance_allowance = getattr(raw_client, "update_balance_allowance", None)
    if not callable(update_balance_allowance):
        return False
    try:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
    except ModuleNotFoundError:
        return False
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    with_timeout = getattr(order_client, "_with_timeout", None)
    if callable(with_timeout):
        with_timeout(update_balance_allowance, params)
    else:
        update_balance_allowance(params)
    return True


def refresh_wallet_snapshot(
    conn: sqlite3.Connection,
    *,
    config: "Config",
    order_client_factory=None,
    data_api_fetcher=None,
    chain_balance_reader=None,
) -> WalletSnapshot:
    """Fetch and save a wallet snapshot (read-only externally). It is complete
    only if CLOB balance, open orders, Data API positions (and on-chain balance,
    when enabled) were all checked."""
    wallet_address = (getattr(config, "poly_funder", "") or "").strip()
    if not is_address(wallet_address):
        snapshot = build_wallet_snapshot(
            conn,
            wallet_address=wallet_address,
            source_status="refresh_degraded",
            extra_warnings=["POLY_FUNDER is not a valid 0x wallet address."],
            chain_balance_required=bool(getattr(config, "live_onchain_verify_enabled", True)),
        )
        record_wallet_snapshot(conn, snapshot)
        return snapshot

    warnings: list[str] = []
    clob_balance_usd: float | None = None
    chain_balance_usd: float | None = None
    open_orders: list[dict[str, Any]] | None = None
    data_api_trades: list[dict[str, Any]] | None = None
    data_api_positions: list[dict[str, Any]] | None = None
    order_client: object | None = None

    try:
        if order_client_factory is None:
            from hightempbot.execution.walker import OrderClient

            order_client_factory = OrderClient
        order_client = order_client_factory(config)
        balance_method = getattr(order_client, "check_balance", None)
        if callable(balance_method):
            balance = balance_method()
            clob_balance_usd = float(balance) if balance is not None else None
        open_orders = _open_orders_for_client(order_client)
    except Exception as exc:
        warnings.append(f"CLOB wallet refresh failed: {type(exc).__name__}: {exc}")

    try:
        fetcher = data_api_fetcher or fetch_data_api_wallet_records
        data_api_trades, data_api_positions = fetcher(wallet_address)
    except Exception as exc:
        warnings.append(f"Polymarket Data API wallet refresh failed: {type(exc).__name__}: {exc}")
    else:
        try:
            recovered = _backfill_no_order_pending_from_data_api(conn, data_api_trades)
            if recovered:
                logger.warning("Backfilled %d no-order PENDING row(s) from wallet trades", recovered)
            tx_recovered = _backfill_missing_tx_hash_from_data_api(conn, data_api_trades)
            if tx_recovered:
                logger.info("Backfilled %d ledger tx hash(es) from wallet trades", tx_recovered)
            reconciled = _reconcile_filled_rows_from_data_api_trades(conn, data_api_trades)
            if reconciled:
                logger.info("Reconciled %d ledger fill(s) from wallet trades", reconciled)
            position_reconciled = _reconcile_open_rows_from_data_api_positions(
                conn,
                data_api_positions,
                wallet_address=wallet_address,
            )
            if position_reconciled:
                logger.info(
                    "Reconciled %d ledger fill(s) from wallet positions",
                    position_reconciled,
                )
        except Exception as exc:
            warnings.append(f"Wallet fill backfill failed: {type(exc).__name__}: {exc}")

    chain_required = bool(getattr(config, "live_onchain_verify_enabled", True))
    if chain_required:
        try:
            if chain_balance_reader is None:
                from hightempbot.execution.live_readiness import read_erc20_balance
                from hightempbot.polymarket.primitives import PUSD_ADDRESS

                chain_balance_reader = read_erc20_balance
                token_address = PUSD_ADDRESS
            else:
                from hightempbot.polymarket.primitives import PUSD_ADDRESS as token_address
            chain_balance_usd = float(
                chain_balance_reader(
                    rpc_url=getattr(config, "polygon_rpc_url", ""),
                    token_address=token_address,
                    wallet_address=wallet_address,
                    decimals=6,
                )
            )
        except Exception as exc:
            warnings.append(f"Polygon pUSD balance refresh failed: {type(exc).__name__}: {exc}")

    tolerance_usd = float(getattr(config, "live_balance_tolerance_usd", 0.25) or 0.25)
    if (
        order_client is not None
        and clob_balance_usd is not None
        and chain_balance_usd is not None
        and abs(float(clob_balance_usd) - float(chain_balance_usd)) > tolerance_usd
    ):
        try:
            if _sync_clob_collateral_balance(order_client):
                balance_method = getattr(order_client, "check_balance", None)
                if callable(balance_method):
                    refreshed_balance = balance_method()
                    if refreshed_balance is not None:
                        clob_balance_usd = float(refreshed_balance)
        except Exception as exc:
            warnings.append(f"CLOB collateral balance resync failed: {type(exc).__name__}: {exc}")

    snapshot = build_wallet_snapshot(
        conn,
        wallet_address=wallet_address,
        clob_balance_usd=clob_balance_usd,
        chain_balance_usd=chain_balance_usd,
        data_api_trades=data_api_trades,
        data_api_positions=data_api_positions,
        open_orders=open_orders,
        source_status="live_refresh" if not warnings else "refresh_degraded",
        tolerance_usd=tolerance_usd,
        chain_balance_required=chain_required,
        extra_warnings=warnings,
    )
    record_wallet_snapshot(conn, snapshot)
    return snapshot


def latest_wallet_snapshot(
    conn: sqlite3.Connection,
    *,
    wallet_address: str = "",
    freshness_ttl_s: int = 300,
) -> dict[str, Any] | None:
    # Readers return None on a DB without the tables.
    try:
        ensure_wallet_schema(conn)
    except RuntimeError:
        return None
    if wallet_address:
        row = conn.execute(
            """
            SELECT sampled_at, snapshot_json
            FROM wallet_reconciliation_runs
            WHERE lower(wallet_address) = lower(?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (wallet_address,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT sampled_at, snapshot_json
            FROM wallet_reconciliation_runs
            ORDER BY sampled_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    try:
        snapshot = json.loads(row["snapshot_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    sampled_dt = _parse_ts(row["sampled_at"])
    age_s = None
    fresh = False
    if sampled_dt is not None:
        age_s = (datetime.now(timezone.utc) - sampled_dt).total_seconds()
        fresh = age_s <= freshness_ttl_s
    snapshot["fresh"] = fresh
    snapshot["ageSeconds"] = age_s
    return snapshot


def transfer_blocking_reconciliation_warnings(
    snapshot: dict[str, Any] | None,
) -> list[str]:
    """Return Data API reconciliation warnings that block pUSD transfers."""
    if not snapshot:
        return []
    explicit = [str(w) for w in snapshot.get("dataApiTransferBlockingWarnings") or [] if str(w)]
    if explicit:
        return explicit
    open_positions = int(_safe_float(snapshot.get("openPositionsCount")))
    mismatched = int(_safe_float(snapshot.get("dataApiMismatchedPositionsCount")))
    unmatched = int(_safe_float(snapshot.get("dataApiUnmatchedPositionsCount")))
    if open_positions > 0 and (mismatched > 0 or unmatched > 0):
        fallback = [str(w) for w in snapshot.get("dataApiReconciliationWarnings") or [] if str(w)]
        return fallback or ["Data API has unmatched or mismatched open wallet positions."]
    return []


def wallet_dashboard_payload(
    conn: sqlite3.Connection,
    *,
    config: "Config",
    dry_run: bool,
) -> dict[str, Any]:
    wallet_address = (getattr(config, "poly_funder", "") or "").strip()
    ttl_s = int(getattr(config, "wallet_snapshot_freshness_ttl_s", 900) or 900)
    if not wallet_address:
        wallet_address = ""
    snapshot = latest_wallet_snapshot(
        conn,
        wallet_address=wallet_address,
        freshness_ttl_s=ttl_s,
    ) if wallet_address else None

    if snapshot is None:
        missing_warning = (
            "No wallet reconciliation snapshot exists for configured POLY_FUNDER."
            if wallet_address
            else "POLY_FUNDER is not configured; live wallet actions disabled."
        )
        snapshot = {
            "walletAddress": wallet_address,
            "walletLabel": mask_address(wallet_address),
            "sampledAt": None,
            "sourceStatus": "no_wallet_snapshot",
            "clobBalanceUsd": None,
            "chainBalanceUsd": None,
            "openOrdersCount": 0,
            "openPositionsCount": 0,
            "warnings": (
                [missing_warning]
                if not dry_run
                else []
            ),
            "records": [],
            "sourcesChecked": {
                "clobBalance": False,
                "chainBalance": False,
                "clobOpenOrders": False,
                "dataApiTrades": False,
                "dataApiPositions": False,
            },
            "complete": False,
            "fresh": bool(dry_run),
            "ageSeconds": None,
        }

    warnings = list(snapshot.get("warnings") or [])
    transfer_blocking_warnings = transfer_blocking_reconciliation_warnings(snapshot)
    redeemable_count = 0
    redeemable_value = _safe_float(snapshot.get("dataApiRedeemableValueUsd"))
    for record in snapshot.get("records") or []:
        if not isinstance(record, dict) or record.get("record_type") != "position":
            continue
        raw = record.get("record") if isinstance(record.get("record"), dict) else {}
        if raw.get("redeemable") is not True:
            continue
        size = _safe_float(raw.get("size"))
        if size <= 0:
            continue
        current_value = _position_current_value(raw)
        if current_value < size * REDEEMABLE_PAYOUT_VALUE_FRACTION:
            continue
        redeemable_count += 1
        if redeemable_value <= 0:
            redeemable_value += current_value
    try:
        from hightempbot.execution.polymarket_redeemer import latest_redemption_summary

        redemption_summary = latest_redemption_summary(conn, wallet_address=wallet_address)
    except Exception:
        redemption_summary = {
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
    actions_enabled = bool(dry_run) or (
        bool(snapshot.get("fresh"))
        and bool(snapshot.get("complete"))
        and not warnings
    )
    try:
        from hightempbot.execution.capital import live_local_pending_notional

        local_pending_usd = live_local_pending_notional(conn)
    except Exception:
        local_pending_usd = 0.0
    open_orders_count = int(snapshot.get("openOrdersCount") or 0)
    transfer_blocked_reason = ""
    if not actions_enabled:
        transfer_blocked_reason = "Wallet snapshot is stale, degraded, or incomplete."
    elif dry_run:
        transfer_blocked_reason = "Process is booted DRY_RUN."
    elif transfer_blocking_warnings:
        transfer_blocked_reason = transfer_blocking_warnings[0]
    elif open_orders_count > 0:
        transfer_blocked_reason = "Open CLOB orders exist; cancel or wait before transfer."
    elif local_pending_usd > 0:
        transfer_blocked_reason = "A local order is still being submitted; wait for submit/cancel before transfer."
    transfer_eligible = actions_enabled and not dry_run and not transfer_blocked_reason
    return {
        "primaryWallet": wallet_address,
        "primaryWalletLabel": mask_address(wallet_address),
        "source": "POLY_FUNDER",
        "snapshot": snapshot,
        "fresh": bool(snapshot.get("fresh")),
        "warnings": warnings,
        "reconciliationWarnings": list(snapshot.get("dataApiReconciliationWarnings") or []),
        "transferBlockingWarnings": transfer_blocking_warnings,
        "actionsEnabled": actions_enabled,
        "transferEligible": transfer_eligible,
        "transferBlockedReason": transfer_blocked_reason,
        "localPendingUsd": local_pending_usd,
        "readOnlyReason": "" if actions_enabled else transfer_blocked_reason,
        "autoRedeem": {
            "enabled": bool(getattr(config, "auto_redeem_enabled", True)) and not dry_run,
            "intervalMinutes": int(getattr(config, "auto_redeem_interval_minutes", 10) or 10),
            "redeemablePositionsCount": redeemable_count,
            "redeemableValueUsd": round(redeemable_value, 2),
            **redemption_summary,
        },
    }
