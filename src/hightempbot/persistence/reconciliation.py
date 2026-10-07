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
    """Delegates to ``execution.walker._bounded_client_call``.

    Both modules need the same timeout-bounded CLOB call shape; the canonical
    implementation lives in walker (it predates this one and has more callers).
    """
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
    """Run ``order_client._client.get_order`` through the executor timeout.

    Bypassing the timeout wrapper would let a hung CLOB block a scheduler
    thread indefinitely. Returns None on timeout/error so callers stay
    fail-closed.
    """
    try:
        return _bounded_call(order_client, order_client._client.get_order, order_id)
    except FuturesTimeoutError:
        logger.warning("get_order timed out for %s", order_id)
        return None
    except Exception:
        logger.debug("get_order raised for %s", order_id, exc_info=True)
        return None


def _bounded_get_trades(order_client: "OrderClient", order_id: str) -> list | None:
    """Run ``order_client._client.get_trades`` through the executor timeout.

    Returns ``None`` when the trades state is unknown because the API call
    timed out or raised. A real empty response is returned as ``[]`` so callers
    can distinguish "confirmed no fills" from "could not verify".
    """
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
    """Poll CLOB until ``order_id`` reaches a terminal state or the deadline passes.

    Returns ``(True, transaction_hash)`` only when the order is MATCHED/FILLED
    **and** the trade record carries a non-null transaction hash. Returns
    ``(False, None)`` on timeout, terminal failure, or MATCHED-without-tx_hash
    after the deadline so callers can decide whether to tolerate the downgrade.

    Each ``get_order``/``get_trades`` call is bounded by the OrderClient's
    executor timeout so a single hung CLOB call cannot exceed the overall
    deadline silently.
    """
    # Canonical tx-hash extractor lives in walker (imported lazily to match
    # this module's existing walker-import convention and avoid an import cycle).
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
    """Return ``(vwap_price, total_size, side)`` from one or more trade payloads.

    ``side`` is the unanimous YES/NO across the trade records, or ``None``
    when no trade carries a resolvable ``outcome`` / ``side`` field. When
    trades disagree (mixed YES + NO on the same order_id), the function
    returns ``None`` for ``side`` so the caller can refuse to silently
    book a phantom open bet on the majority side — a SELL-orphan recovery
    booked as a BUY would invert the realized PnL on resolution.

    Polymarket trade events typically expose ``side`` ("BUY"/"SELL") and
    ``outcome`` ("YES"/"NO"); we prefer ``outcome`` when present and fall
    back to ``side``.
    """
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
        # Prefer `outcome` (YES/NO) over `side` (BUY/SELL) — outcome maps
        # directly to the ledger `side` column, while BUY/SELL needs the
        # token-id context we don't have here. Skip anything not in the
        # canonical {YES, NO} set so a stray "MAKER" doesn't poll.
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
        # Mixed YES+NO trades on one order_id is anomalous (a single CLOB
        # order is one-sided); refuse to guess. Caller logs and routes to
        # manual reconciliation rather than booking the wrong side.
        logger.error(
            "Mixed YES/NO trade payload (votes=%s); refusing to infer side",
            {k: round(v, 4) for k, v in side_votes.items()},
        )
        inferred_side = None

    return total_notional / total_size, total_size, inferred_side


def _earliest_trade_ts(trades) -> str | None:
    """Return the earliest CLOB trade timestamp as a SQLite-text UTC stamp.

    Polymarket trades expose `match_time` (ISO) or `matchTime` (epoch
    seconds string) — try both. Returns None when no trade carries a
    parseable timestamp; caller falls back to ``utc_now_sql()``.
    """
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
    """Insert a recovery PENDING row for an orphan order with real fills.

    Returns True when a recovery row was written. The recovery row carries
    `event_detail.recovered_orphan = true` so dashboards can surface it for
    manual review of the bracket linkage.

    When ``abort_event`` is set (by an outer reconcile timeout), the function
    bails out before the ledger INSERT so the supervisor's forced DRY_RUN
    downgrade isn't undermined by a worker thread that keeps mutating the
    ledger after its caller has moved on.
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
    # Use the earliest CLOB trade timestamp as bet_ts when available so the
    # recovery row sits in equity-curve order at the actual placement time
    # rather than the recovery time. Falls back to utc_now_sql() when the
    # trade payload omits match_time / matchTime.
    recovery_bet_ts = _earliest_trade_ts(trades) or utc_now_sql()
    # Schema requires every NOT NULL column on `ledger`; the recovery row
    # carries sentinels for fields the crash erased. `bracket_label` is not
    # a `ledger` column (it lives on `signals` / `market_tokens`) — store
    # it inside `event_detail` JSON so the dashboard can still surface it.
    recovery_detail = json.dumps({
        "recovered_orphan": True,
        "bracket_label": "RECOVERED",
        "recovered_at": utc_now_sql(),
    })
    # Build the recovery row as a dict so column → value pairs stay readable
    # and adding/removing a column doesn't desync a 21-slot positional tuple.
    recovery_row: dict[str, object] = {
        "bet_ts": recovery_bet_ts,
        "station_id": "RECOVERED",
        "market_id": "RECOVERED",
        "token_id": "RECOVERED",
        "target_date": "RECOVERED",    # sentinel; manual review re-links
        "horizon": 1,
        "threshold": 0.0,
        "side": inferred_side,         # derived from CLOB trades (#10)
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
        # `with conn:` commits on clean exit and rolls back on exception so
        # the recovery INSERT is atomic with any other pending writes on this
        # connection (e.g. a half-applied UPDATE from the caller).
        # UNIQUE INDEX idx_ledger_order_id prevents duplicate recovery on
        # retry — see migrations/2026_05_13_ledger_order_id_unique.sql
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
    """Reconcile open CLOB orders against ledger.

    1. Fetch open orders from CLOB.
    2. Match against ledger by order_id.
    3. Orphaned (CLOB only): pre-check status; cancel only if not already
       MATCHED/FILLED. A MATCHED orphan signals a crash between place_order
       and the ledger UPDATE — recover into a PENDING row instead of cancelling
       a real fill. A terminally-failed orphan needs no cancel call.
    4. Missing fills (ledger only): query CLOB, update ledger.

    When ``abort_event`` is set by an outer supervisor (e.g. the 180s boot
    deadline in ``main._run_startup_reconcile``), the orphan-recovery loop
    bails out before mutating the ledger and ``result.aborted`` is set so
    callers can log a partial-reconcile follow-up.
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
        # oid → (p_market, pre_trade_edge) for realized_edge recomputation below.
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
            # Honor an outer timeout: if the supervisor has flipped the
            # abort flag, the parent has already moved on (forced DRY_RUN);
            # continuing to mutate the ledger here would leave the dry-run
            # session inheriting pending exposure for orders it didn't place.
            if abort_event is not None and abort_event.is_set():
                remaining_count = len(orphan_list) - i
                logger.warning(
                    "Reconciliation aborted by timeout signal; %d orphan(s) left unprocessed",
                    remaining_count,
                )
                result.aborted = True
                break

            # Pre-check status: MATCHED/FILLED means a real fill. FAK can also
            # finish CANCELED/EXPIRED after a partial fill, so terminal orphans
            # still get one trades lookup before we no-op/cancel.
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
                    # Terminal FAK with no positive trades is a true no-fill.
                    continue
                else:
                    # Distinguish a true unrecovered orphan from an abort-induced skip:
                    # only flag failure when the worker wasn't told to stop.
                    # The top-of-loop abort guard at line 452 covers the next
                    # iteration; this in-line check fires when the abort raced
                    # the recovery call between the top-of-loop check and now.
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

        # Honor abort signal before entering missing-fills loop: the supervisor
        # may have flipped DRY_RUN already if we spent the budget on orphans.
        if abort_event is not None and abort_event.is_set():
            result.aborted = True
            logger.warning(
                "Reconciliation aborted before missing-fills loop by timeout signal"
            )
            return result

        missing = ledger_order_ids - clob_order_ids
        for oid in missing:
            # Per-iteration abort check: long missing-fills loops can outlast
            # the boot deadline, and continuing to mutate the ledger after the
            # supervisor flipped to forced DRY_RUN leaves real-money state
            # racing the new dry-run session.
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
                    # Missing-fill branch only updates fill_price/size/realized_edge; the
                    # ledger row already carries the placement-time `side`, so the
                    # `inferred_side` from trade aggregation is ignored here.
                    fill_price, fill_size, _missing_inferred_side = aggregated
                    filled_notional = fill_price * fill_size

                    # Recompute realized_edge from the ledger's pre-trade inputs:
                    #   pre_edge = prob_safe_floor - p_market - fee(p_market)
                    #   realized_edge = pre_edge + p_market + fee(p_market) - fill_price - fee(fill_price)
                    # Net out the pre_edge / p_market terms to avoid persisting
                    # the LUT-calibrated probability separately just for reconciliation.
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

                    # Outcome filter prevents resurrecting a terminal row: a
                    # resolver tick may have already settled this order_id to
                    # WIN/LOSS/PUSH/CLOSED before reconciler caught up. The
                    # original UPDATE re-set outcome='PENDING', which would
                    # reverse the resolution and book pnl=0 on a real win.
                    # `with conn:` makes the UPDATE atomic with any other
                    # half-applied write on this connection.
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

        # Honor abort before the stranded-PENDING UPDATE: another mutation
        # path that should respect the supervisor's forced-DRY_RUN signal.
        if abort_event is not None and abort_event.is_set():
            result.aborted = True
            logger.warning(
                "Reconciliation aborted before stranded-PENDING sweep by timeout signal"
            )
            return result

        # Stranded no-order_id PENDING: crashed between record_bet insert
        # (PENDING-first ledger) and execute_or_log (which sets order_id +
        # transitions to FILLED/CANCELLED). The row never reached Polymarket.
        # 30-min threshold leaves headroom for genuinely slow place-order paths
        # without permanently consuming exposure cap.
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
