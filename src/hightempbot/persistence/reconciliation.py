"""Crash recovery: reconcile CLOB orders against ledger on startup."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hightempbot.db.connection import utc_now_sql
from hightempbot.execution.strategy_constants import VERIFY_POLY_TIMEOUT_S
from hightempbot.persistence.ledger import decode_event_detail, poly_fee_per_share

if TYPE_CHECKING:
    from hightempbot.execution.walker import OrderClient

logger = logging.getLogger(__name__)

_MATCHED_STATUSES = {"MATCHED", "FILLED"}
_TERMINAL_FAILURE_STATUSES = {"CANCELED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED"}


def _recovered_orphan_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    detail = decode_event_detail(row["event_detail"])
    return {
        "ledgerId": int(row["id"]),
        "betTs": row["bet_ts"],
        "stationId": row["station_id"],
        "targetDate": row["target_date"],
        "marketId": row["market_id"],
        "tokenId": row["token_id"],
        "side": row["side"],
        "orderId": row["order_id"],
        "fillPrice": row["fill_price"],
        "fillSize": row["fill_size"],
        "betSize": row["bet_size"],
        "limitPrice": row["limit_price"],
        "threshold": row["threshold"],
        "outcome": row["outcome"],
        "bracketLow": detail.get("bracket_low"),
        "bracketHigh": detail.get("bracket_high"),
        "bracketLabel": detail.get("bracket_label"),
        "patchedAt": detail.get("recovered_orphan_patch", {}).get("patched_at")
        if isinstance(detail.get("recovered_orphan_patch"), dict)
        else None,
        "eventDetail": detail,
    }


def list_recovered_orphans(
    conn: sqlite3.Connection,
    *,
    pending_only: bool = True,
) -> list[dict[str, Any]]:
    """Return recovered-orphan ledger rows for manual operator review."""
    sql = (
        "SELECT id, bet_ts, station_id, target_date, market_id, token_id, "
        "side, order_id, fill_price, fill_size, bet_size, limit_price, "
        "threshold, outcome, event_detail "
        "FROM ledger "
        "WHERE json_extract(event_detail, '$.recovered_orphan') = 1"
    )
    if pending_only:
        sql += " AND outcome = 'PENDING'"
    sql += " ORDER BY id"
    return [_recovered_orphan_row_to_dict(row) for row in conn.execute(sql).fetchall()]


def patch_recovered_orphan(
    conn: sqlite3.Connection,
    *,
    ledger_id: int,
    station_id: str,
    market_id: str,
    token_id: str,
    target_date: str,
    threshold: float,
    bracket_low: float | None,
    bracket_high: float | None,
    side: str | None = None,
    bracket_label: str | None = None,
    actor: str = "cli:orphan-patch",
    reason: str = "",
) -> dict[str, Any]:
    """Relink a recovered orphan to its station/date/token/bracket metadata."""
    if bracket_low is None and bracket_high is None:
        raise ValueError("at least one bracket bound is required")

    row = conn.execute(
        "SELECT id, outcome, event_detail FROM ledger "
        "WHERE id = ? AND json_extract(event_detail, '$.recovered_orphan') = 1",
        (ledger_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"recovered orphan ledger row {ledger_id} was not found")
    if row["outcome"] != "PENDING":
        raise ValueError(f"recovered orphan ledger row {ledger_id} is not PENDING")

    detail = decode_event_detail(row["event_detail"])
    detail["recovered_orphan"] = True
    detail["bracket_low"] = bracket_low
    detail["bracket_high"] = bracket_high
    if bracket_label:
        detail["bracket_label"] = bracket_label
    if side:
        detail["recovered_side"] = side
    detail["recovered_orphan_patch"] = {
        "actor": actor,
        "reason": reason,
        "patched_at": utc_now_sql(),
    }

    update_cols = [
        "station_id = ?",
        "market_id = ?",
        "token_id = ?",
        "target_date = ?",
        "threshold = ?",
        "event_detail = ?",
    ]
    params: list[Any] = [
        station_id,
        market_id,
        token_id,
        target_date,
        float(threshold),
        json.dumps(detail, sort_keys=True),
    ]
    if side:
        update_cols.append("side = ?")
        params.append(side)
    params.append(ledger_id)

    with conn:
        cur = conn.execute(
            f"UPDATE ledger SET {', '.join(update_cols)} "
            "WHERE id = ? "
            "AND outcome = 'PENDING' "
            "AND json_extract(event_detail, '$.recovered_orphan') = 1",
            tuple(params),
        )
    if cur.rowcount != 1:
        raise ValueError(f"recovered orphan ledger row {ledger_id} was not patched")

    patched = conn.execute(
        "SELECT id, bet_ts, station_id, target_date, market_id, token_id, "
        "side, order_id, fill_price, fill_size, bet_size, limit_price, "
        "threshold, outcome, event_detail "
        "FROM ledger WHERE id = ?",
        (ledger_id,),
    ).fetchone()
    return _recovered_orphan_row_to_dict(patched)


def _bounded_call(order_client: "OrderClient", fn, *args, **kwargs):
    """``walker._bounded_client_call``, imported lazily to avoid a cycle."""
    from hightempbot.execution.walker import _bounded_client_call
    return _bounded_client_call(order_client, fn, *args, **kwargs)


@dataclass
class ReconciliationResult:
    """Summary of startup reconciliation."""

    matched: int = 0
    cancelled: int = 0
    updated: int = 0
    failed: bool = False
    error: str | None = None
    aborted: bool = False


def _bounded_get_order(order_client: "OrderClient", order_id: str) -> dict | None:
    """Timeout-bounded ``get_order``; None on timeout or error."""
    try:
        return _bounded_call(order_client, order_client._client.get_order, order_id)
    except FuturesTimeoutError:
        logger.warning("get_order timed out for %s", order_id)
        return None
    except Exception:
        logger.debug("get_order raised for %s", order_id, exc_info=True)
        return None


def _bounded_get_trades(order_client: "OrderClient", order_id: str) -> list | None:
    """Timeout-bounded ``get_trades``: ``[]`` means no fills, None means unknown."""
    try:
        from hightempbot.execution.walker import _get_trades_for_order

        return _get_trades_for_order(order_client, order_id)
    except FuturesTimeoutError:
        logger.warning("get_trades timed out for %s", order_id)
        return None
    except Exception:
        logger.debug("get_trades raised for %s", order_id, exc_info=True)
        return None


def verify_order_matched(
    order_client: "OrderClient",
    order_id: str,
    poll_interval_s: float = 1.0,
    timeout_s: float = float(VERIFY_POLY_TIMEOUT_S),
) -> tuple[bool, str | None]:
    """Poll until the order is terminal or the deadline passes.

    ``(True, tx_hash)`` only for MATCHED/FILLED with a transaction hash;
    otherwise ``(False, None)``.
    """
    from hightempbot.execution.walker import _tx_hash_from_trades

    deadline = time.monotonic() + max(0.0, timeout_s)

    while True:
        order = _bounded_get_order(order_client, order_id)

        if order:
            status_raw = order.get("status") if isinstance(order, dict) else getattr(order, "status", None)
            status = str(status_raw or "").upper()
            if status in _MATCHED_STATUSES:
                trades = _bounded_get_trades(order_client, order_id)
                tx_hash = _tx_hash_from_trades(trades)
                if tx_hash is not None:
                    return (True, tx_hash)
            if status in _TERMINAL_FAILURE_STATUSES:
                return (False, None)

        if time.monotonic() >= deadline:
            return (False, None)

        time.sleep(poll_interval_s)


def _aggregate_trades(trades) -> tuple[float, float, str | None] | None:
    """``(vwap, total_size, side)`` from trades. ``side`` is the unanimous
    YES/NO outcome, or None if unknown or mixed (caller must not guess)."""
    if not trades:
        return None

    from hightempbot.execution.walker import _trade_list

    trade_list = _trade_list(trades)
    total_size = 0.0
    total_notional = 0.0
    side_votes: dict[str, float] = {}

    for trade in trade_list:
        if not isinstance(trade, dict):
            continue
        try:
            size = float(trade.get("size", 0) or 0)
            price = float(trade.get("price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(size) or not math.isfinite(price):
            continue
        if size <= 0 or price <= 0:
            continue
        total_size += size
        total_notional += price * size
        outcome_raw = str(trade.get("outcome") or "").upper()
        side_raw = str(trade.get("side") or "").upper()
        candidate = outcome_raw if outcome_raw in {"YES", "NO"} else (
            side_raw if side_raw in {"YES", "NO"} else ""
        )
        if candidate:
            side_votes[candidate] = side_votes.get(candidate, 0.0) + size

    if total_size <= 0:
        return None

    inferred_side: str | None = None
    if len(side_votes) == 1:
        inferred_side = next(iter(side_votes))
    elif len(side_votes) > 1:
        logger.error(
            "Mixed YES/NO trade payload (votes=%s); refusing to infer side",
            {k: round(v, 4) for k, v in side_votes.items()},
        )
        inferred_side = None

    return total_notional / total_size, total_size, inferred_side


def _earliest_trade_ts(trades) -> str | None:
    """Earliest ``match_time``/``matchTime`` as SQLite UTC text, or None."""
    if not trades:
        return None
    candidates: list[float] = []
    from hightempbot.execution.walker import _trade_list
    trade_list = _trade_list(trades)
    from datetime import datetime as _dt2, timezone as _tz2
    for trade in trade_list:
        if not isinstance(trade, dict):
            continue
        raw = (
            trade.get("match_time")
            or trade.get("matchTime")
            or trade.get("matched_at")
        )
        if raw is None:
            continue
        try:
            if isinstance(raw, (int, float)):
                ts_seconds = float(raw)
            else:
                raw_str = str(raw).strip()
                if raw_str.isdigit() or (
                    raw_str.startswith("-") and raw_str[1:].isdigit()
                ):
                    ts_seconds = float(raw_str)
                else:
                    parsed = _dt2.fromisoformat(raw_str.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=_tz2.utc)
                    ts_seconds = parsed.timestamp()
        except (TypeError, ValueError):
            continue
        if math.isfinite(ts_seconds) and ts_seconds > 0:
            candidates.append(ts_seconds)
    if not candidates:
        return None
    earliest = min(candidates)
    return _dt2.fromtimestamp(earliest, tz=_tz2.utc).strftime("%Y-%m-%d %H:%M:%S")


def _recover_orphan_with_fill(
    order_client: "OrderClient",
    conn: sqlite3.Connection,
    order_id: str,
    *,
    abort_event: threading.Event | None = None,
    require_fill: bool = True,
) -> bool:
    """Insert a PENDING ``RECOVERED`` row for an orphan order that filled.

    Returns True if written. Does nothing once ``abort_event`` is set.
    """
    trades = _bounded_get_trades(order_client, order_id)
    if trades is None:
        if require_fill:
            logger.error(
                "Orphan order %s may be filled but trades lookup is unknown; "
                "leaving live without ledger linkage for manual reconciliation",
                order_id,
            )
        else:
            logger.debug("Terminal orphan order %s trades lookup unknown", order_id)
        return False
    aggregated = _aggregate_trades(trades) if trades else None
    if aggregated is None:
        if require_fill:
            logger.error(
                "Orphan order %s had no positive/unparseable trades; "
                "leaving live without ledger linkage for manual reconciliation",
                order_id,
            )
        else:
            logger.info("Terminal orphan order %s had no positive trades; treating as no-fill", order_id)
        return False
    fill_price, fill_size, inferred_side = aggregated
    if inferred_side is None:
        logger.error(
            "Orphan order %s has fills but trades carry no resolvable side; "
            "leaving live without ledger linkage for manual reconciliation",
            order_id,
        )
        return False
    filled_notional = fill_price * fill_size
    recovery_bet_ts = _earliest_trade_ts(trades) or utc_now_sql()
    # Sentinels fill the NOT NULL columns the crash lost.
    recovery_detail = json.dumps({
        "recovered_orphan": True,
        "bracket_label": "RECOVERED",
        "recovered_at": utc_now_sql(),
    })
    recovery_row: dict[str, object] = {
        "bet_ts": recovery_bet_ts,
        "station_id": "RECOVERED",
        "market_id": "RECOVERED",
        "token_id": "RECOVERED",
        "target_date": "RECOVERED",    # sentinel; manual review re-links
        "horizon": 1,
        "threshold": 0.0,
        "side": inferred_side,         # derived from CLOB trades
        "p_model": 0.0,
        "p_market": fill_price,        # fill price is the only price we know
        "edge": 0.0,
        "kelly_size": filled_notional,
        "volume_cap": 0.0,
        "bet_size": filled_notional,
        "limit_price": fill_price,     # order matched at fill_price
        "order_id": order_id,
        "fill_price": fill_price,
        "fill_size": fill_size,
        "outcome": "PENDING",
        "event_type": "bet",
        "event_detail": recovery_detail,
    }
    columns = ", ".join(recovery_row.keys())
    placeholders = ", ".join(["?"] * len(recovery_row))
    if abort_event is not None and abort_event.is_set():
        logger.warning(
            "Orphan %s recovery aborted before INSERT by timeout signal", order_id
        )
        return False
    try:
        # The unique order_id index makes retries no-ops.
        with conn:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO ledger ({columns}) VALUES ({placeholders})",
                tuple(recovery_row.values()),
            )
        if cur.rowcount == 0:
            logger.info(
                "Orphan order %s already has a ledger row (UNIQUE index hit); "
                "skipping duplicate recovery",
                order_id,
            )
            return False
        logger.warning(
            "Recovered orphan filled order %s as PENDING (fill_price=%.3f size=%.1f); "
            "manual bracket linkage required",
            order_id, fill_price, fill_size,
        )
        return True
    except sqlite3.Error:
        logger.error("Failed to insert recovery row for orphan %s", order_id, exc_info=True)
        return False


def reconcile_orders(
    order_client: "OrderClient",
    conn: sqlite3.Connection,
    *,
    abort_event: threading.Event | None = None,
) -> ReconciliationResult:
    """Bring the ledger in line with CLOB.

    - CLOB orders missing from the ledger: recover them if they filled,
      otherwise cancel.
    - Ledger PENDING rows: read fills from CLOB.
    - PENDING rows with no order_id older than 30 min: cancel.

    Stops writing once ``abort_event`` is set (``result.aborted``).
    """
    result = ReconciliationResult()

    try:
        try:
            open_orders = _bounded_call(order_client, order_client._client.get_open_orders)
            if not isinstance(open_orders, list):
                open_orders = []
        except FuturesTimeoutError as e:
            logger.error("get_open_orders timed out at startup")
            result.failed = True
            result.error = f"get_open_orders timeout: {e}"
            return result
        except Exception as e:
            logger.error("Failed to fetch open orders from CLOB: %s", e)
            result.failed = True
            result.error = str(e)
            return result

        clob_order_ids = {
            oid for o in open_orders if o
            for oid in [o.get("id") or o.get("order_id")]
            if oid
        }

        ledger_rows = conn.execute(
            """SELECT id, order_id, p_market, edge, fill_price, fill_size, fill_ts
            FROM ledger
            WHERE outcome = 'PENDING' AND order_id IS NOT NULL
            AND event_type = 'bet'"""
        ).fetchall()
        ledger_order_ids = {r["order_id"] for r in ledger_rows}
        ledger_reconcile_inputs: dict[str, tuple[float | None, float | None]] = {
            r["order_id"]: (r["p_market"], r["edge"]) for r in ledger_rows
        }
        ledger_recorded_fills: dict[str, bool] = {}
        for r in ledger_rows:
            try:
                fill_price = float(r["fill_price"] or 0.0)
                fill_size = float(r["fill_size"] or 0.0)
            except (TypeError, ValueError):
                fill_price = 0.0
                fill_size = 0.0
            ledger_recorded_fills[r["order_id"]] = (
                math.isfinite(fill_price)
                and math.isfinite(fill_size)
                and fill_price > 0.0
                and fill_size > 0.0
            )

        orphaned = clob_order_ids - ledger_order_ids
        orphan_list = list(orphaned)
        for i, oid in enumerate(orphan_list):
            if abort_event is not None and abort_event.is_set():
                remaining_count = len(orphan_list) - i
                logger.warning(
                    "Reconciliation aborted by timeout signal; %d orphan(s) left unprocessed",
                    remaining_count,
                )
                result.aborted = True
                break

            # A FAK can end CANCELED after a partial fill, so check trades too.
            raw = _bounded_get_order(order_client, oid)
            status_up = ""
            if raw is not None:
                status_raw = raw.get("status") if isinstance(raw, dict) else getattr(raw, "status", None)
                status_up = str(status_raw or "").upper()

            if status_up in _MATCHED_STATUSES or status_up in _TERMINAL_FAILURE_STATUSES:
                if _recover_orphan_with_fill(
                    order_client,
                    conn,
                    oid,
                    abort_event=abort_event,
                    require_fill=status_up in _MATCHED_STATUSES,
                ):
                    result.updated += 1
                    continue
                if status_up in _TERMINAL_FAILURE_STATUSES:
                    continue
                else:
                    # Not a failure if we were told to abort.
                    if abort_event is not None and abort_event.is_set():
                        result.aborted = True
                        break
                    result.failed = True
                    result.error = f"unrecovered matched orphan {oid}"
                continue
            try:
                from py_clob_client_v2.clob_types import OrderPayload
                _bounded_call(
                    order_client, order_client._client.cancel_order, OrderPayload(orderID=oid)
                )
                result.cancelled += 1
                logger.info("Cancelled orphaned order: %s (status=%s)", oid, status_up or "UNKNOWN")
            except FuturesTimeoutError:
                logger.warning("Cancel timed out for orphan %s", oid)
            except Exception:
                logger.warning("Failed to cancel orphaned order %s", oid, exc_info=True)

        matched = clob_order_ids & ledger_order_ids
        result.matched = len(matched)

        if abort_event is not None and abort_event.is_set():
            result.aborted = True
            logger.warning(
                "Reconciliation aborted before missing-fills loop by timeout signal"
            )
            return result

        missing = ledger_order_ids - clob_order_ids
        for oid in missing:
            if abort_event is not None and abort_event.is_set():
                result.aborted = True
                logger.warning(
                    "Missing-fills loop aborted by timeout signal at order %s",
                    oid,
                )
                break
            try:
                trades = _bounded_get_trades(order_client, oid)
                if trades is None:
                    if ledger_recorded_fills.get(oid):
                        logger.warning(
                            "Trades unavailable for missing ledger order %s, "
                            "but ledger already records a positive fill; "
                            "leaving row PENDING without failing reconciliation",
                            oid,
                        )
                        continue
                    result.failed = True
                    result.error = f"get_trades unavailable for {oid}"
                    logger.error(
                        "Could not verify trades for missing ledger order %s; "
                        "leaving row PENDING and failing reconciliation",
                        oid,
                    )
                    continue
                if trades:
                    aggregated = _aggregate_trades(trades)
                    if aggregated is None:
                        logger.warning("Trades for order %s had no positive size/price payloads", oid)
                        continue
                    fill_price, fill_size, _missing_inferred_side = aggregated
                    filled_notional = fill_price * fill_size

                    # realized = pre_edge + p_market + fee(p_market) - fill - fee(fill)
                    p_market, pre_edge = ledger_reconcile_inputs.get(oid, (None, None))
                    realized_edge: float | None = None
                    if (
                        p_market is not None
                        and pre_edge is not None
                        and math.isfinite(p_market)
                        and math.isfinite(pre_edge)
                        and math.isfinite(fill_price)
                    ):
                        old_fee = poly_fee_per_share(p_market)
                        new_fee = poly_fee_per_share(fill_price)
                        realized_edge = pre_edge + p_market + old_fee - fill_price - new_fee

                    # Only touch rows still PENDING; never un-settle a row.
                    with conn:
                        cur = conn.execute(
                            """UPDATE ledger
                            SET fill_price = ?,
                                fill_size = ?,
                                kelly_size = ?,
                                bet_size = ?,
                                realized_edge = COALESCE(?, realized_edge)
                            WHERE order_id = ?
                              AND outcome NOT IN
                                  ('WIN', 'LOSS', 'PUSH', 'CLOSED',
                                   'CANCELLED', 'EXPIRED')""",
                            (fill_price, fill_size, filled_notional,
                             filled_notional, realized_edge, oid),
                        )
                    if cur.rowcount == 0:
                        logger.info(
                            "Skipping missing-fill update for %s: row already terminal "
                            "(WIN/LOSS/PUSH/CLOSED/CANCELLED/EXPIRED)",
                            oid,
                        )
                    else:
                        result.updated += 1
                        logger.info(
                            "Updated fill for order %s: price=%.3f size=%.1f",
                            oid, fill_price, fill_size,
                        )
                else:
                    if ledger_recorded_fills.get(oid):
                        logger.warning(
                            "CLOB returned no trades for missing ledger order %s, "
                            "but ledger already records a positive fill; "
                            "leaving row PENDING without cancelling",
                            oid,
                        )
                        continue
                    with conn:
                        conn.execute(
                            "UPDATE ledger SET outcome = 'CANCELLED', pnl = 0.0 "
                            "WHERE order_id = ? AND outcome = 'PENDING'",
                            (oid,),
                        )
                    result.cancelled += 1
                    logger.info("Cancelled unfilled expired order: %s", oid)
            except Exception:
                logger.warning("Failed to reconcile order %s", oid, exc_info=True)

        if abort_event is not None and abort_event.is_set():
            result.aborted = True
            logger.warning(
                "Reconciliation aborted before stranded-PENDING sweep by timeout signal"
            )
            return result

        # PENDING rows with no order_id never reached CLOB (crash before placing).
        stranded_rows = conn.execute(
            """SELECT id, station_id, target_date, threshold
            FROM ledger
            WHERE outcome = 'PENDING' AND order_id IS NULL AND event_type = 'bet'
            AND bet_ts < datetime('now', '-30 minutes')"""
        ).fetchall()
        for stranded in stranded_rows:
            logger.warning(
                "Cancelling stranded PENDING row id=%s station=%s date=%s threshold=%s "
                "(no order_id, >30min old — crashed between record_bet and execute_or_log)",
                stranded["id"], stranded["station_id"], stranded["target_date"],
                stranded["threshold"],
            )
        if stranded_rows:
            with conn:
                conn.execute(
                    """UPDATE ledger SET outcome = 'CANCELLED', pnl = 0.0
                    WHERE outcome = 'PENDING' AND order_id IS NULL AND event_type = 'bet'
                    AND bet_ts < datetime('now', '-30 minutes')"""
                )
            result.cancelled += len(stranded_rows)

    except Exception as e:
        logger.error("Reconciliation failed: %s", e, exc_info=True)
        result.failed = True
        result.error = str(e)

    logger.info(
        "Reconciliation: matched=%d, cancelled=%d, updated=%d, failed=%s",
        result.matched, result.cancelled, result.updated, result.failed,
    )
    return result
