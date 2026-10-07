"""Take-profit / stop-loss monitor for strategies with ``tp``/``sl`` set.

Closes a PENDING position when the full-size bid-walk VWAP has moved past the
threshold. ``event_detail.close_in_flight`` is written before selling so a
crash can't cause a double sell; flags older than TP_SL_FLAG_STALE_SECONDS are
cleared and retried. An ambiguous submit keeps the flag.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from hightempbot.execution.strategy_constants import (
    STRATEGY_CONFIGS,
    TP_SL_FLAG_STALE_SECONDS,
    tick_index_for,
)
from hightempbot.persistence.ledger import (
    decode_event_detail,
    pending_positions_by_strategy,
    record_position_close,
)
from hightempbot.execution.walker import (
    ClobReader,
    OrderClient,
    OrderResult,
    close_size_fills_target,
    quote_close_sell,
)
from hightempbot.db.connection import get_connection, utc_now_sql

logger = logging.getLogger(__name__)
_PRICE_EPSILON = 1e-9


def _parse_close_in_flight(detail_raw: str | None) -> dict | None:
    """Pull the close_in_flight marker out of raw event_detail JSON, or None."""
    cif = decode_event_detail(detail_raw).get("close_in_flight")
    if isinstance(cif, dict):
        return cif
    return None


def _flag_started_at(cif: dict) -> datetime | None:
    val = cif.get("started_at") if isinstance(cif, dict) else None
    if not val:
        return None
    try:
        return datetime.strptime(val, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _set_close_in_flight(conn: sqlite3.Connection, bet_id: int, started_at: str) -> None:
    """Set event_detail.close_in_flight."""
    conn.execute(
        "UPDATE ledger SET event_detail = json_set("
        "  COALESCE(event_detail, '{}'), "
        "  '$.close_in_flight', json_object('started_at', ?) "
        ") WHERE id = ?",
        (started_at, bet_id),
    )
    conn.commit()


def _clear_close_in_flight(conn: sqlite3.Connection, bet_id: int) -> None:
    """Remove the close_in_flight key from event_detail."""
    conn.execute(
        "UPDATE ledger SET event_detail = json_remove("
        "  COALESCE(event_detail, '{}'), '$.close_in_flight'"
        ") WHERE id = ?",
        (bet_id,),
    )
    conn.commit()


def _close_result_has_verified_full_fill(result: OrderResult, target_size: float) -> bool:
    if result.fill_price is None:
        return False
    try:
        price = float(result.fill_price)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(price)
        and price > 0
        and close_size_fills_target(result.fill_size, target_size)
    )


def _close_result_is_explicit_safe_response(
    result: OrderResult,
    target_size: float,
) -> bool:
    if not (result.order_id and result.transaction_hash):
        return False
    if result.fill_size is not None and not close_size_fills_target(
        result.fill_size,
        target_size,
    ):
        return False
    return True


def _close_result_unknown_after_submit(result: OrderResult) -> bool:
    kind = getattr(result, "error_kind", None)
    if kind in {"network", "unknown"}:
        return True
    text = str(getattr(result, "error", "") or "").lower()
    return "timeout" in text or "timed out" in text


def _evaluate_row(
    *,
    fill_price: float,
    bid: float,
    tp: float | None,
    sl: float | None,
) -> str | None:
    """'tp' if bid − fill_price ≥ tp, 'sl' if ≤ −sl, else None."""
    move = bid - fill_price
    if tp is not None and move >= tp - _PRICE_EPSILON:
        return "tp"
    if sl is not None and move <= -sl + _PRICE_EPSILON:
        return "sl"
    return None


def run_tp_sl_monitor(
    station,
    db_path: str,
    *,
    strategy: str = "YMID",
    order_client: OrderClient | None = None,
    reader: ClobReader | None = None,
    dry_run: bool = True,
    notify=None,
    local_now_minute: int | None = None,
) -> dict[str, int]:
    """One TP/SL pass over ``station``'s PENDING rows for ``strategy``.

    Live needs ``order_client``; dry-run simulates the close at the bid-walk
    VWAP using ``reader``. With ``local_now_minute``, new closes fire only on
    the hour's first tick. Returns counters: tp_fired, sl_fired, skipped,
    stale_cleared, errors.
    """
    try:
        cfg = STRATEGY_CONFIGS[strategy]
    except KeyError:
        logger.error("run_tp_sl_monitor: unknown strategy %r", strategy)
        return {"tp_fired": 0, "sl_fired": 0, "skipped": 0, "stale_cleared": 0, "errors": 0}
    if cfg.tp is None and cfg.sl is None:
        logger.warning("%s has no tp/sl configured; monitor is a no-op", strategy)
        return {"tp_fired": 0, "sl_fired": 0, "skipped": 0, "stale_cleared": 0, "errors": 0}

    reason_prefix = strategy.lower()
    counters = {"tp_fired": 0, "sl_fired": 0, "skipped": 0, "stale_cleared": 0, "errors": 0}
    station_id = station.icao
    price_source = reader or order_client
    if price_source is None and not dry_run:
        logger.error("run_tp_sl_monitor (live) requires an OrderClient")
        return counters

    with get_connection(db_path) as conn:
        try:
            from hightempbot.execution.operator_control import processing_block_reason

            block_reason = processing_block_reason(conn, dry_run=dry_run)
        except Exception:
            block_reason = None if dry_run else "operator-control gate unavailable"
            logger.warning("TP/SL operator-control gate failed for %s", station_id, exc_info=True)
        if block_reason:
            logger.info("TP/SL monitor %s/%s skipped: %s", strategy, station_id, block_reason)
            counters["skipped"] += 1
            from hightempbot.db.connection import log_pipeline_health
            log_pipeline_health(conn, station_id, "operator", "SKIP", block_reason[:500])
            return counters

        # Only rows of the current mode (never sell dry-run positions live).
        rows = pending_positions_by_strategy(
            conn,
            strategy,
            station_id=station_id,
            dry_run=dry_run,
        )
        if not rows:
            return counters

        now_utc = datetime.now(timezone.utc)
        stale_threshold = timedelta(seconds=TP_SL_FLAG_STALE_SECONDS)

        # Per-token caches so rows sharing a token fetch one book.
        _book_cache: dict[str, object] = {}
        _bid_cache: dict[str, object] = {}
        _MISSING = object()

        def _book_for(token_id: str):
            if price_source is None:
                return None
            cached = _book_cache.get(token_id, _MISSING)
            if cached is _MISSING:
                cached = price_source.fetch_order_book(token_id)
                _book_cache[token_id] = cached
            return cached

        def _bid_for(token_id: str, book):
            if price_source is None or book is None:
                return None
            cached = _bid_cache.get(token_id, _MISSING)
            if cached is _MISSING:
                cached = price_source.best_bid(book)
                _bid_cache[token_id] = cached
            return cached

        for row in rows:
            # Re-check operator control so Stop takes effect mid-pass.
            try:
                from hightempbot.execution.operator_control import (
                    processing_block_reason as _block,
                )

                mid_row_block = _block(conn, dry_run=dry_run)
            except Exception:
                mid_row_block = None
            if mid_row_block:
                logger.info(
                    "TP/SL monitor %s/%s halted mid-pass (%s); skipping remaining rows",
                    strategy, station_id, mid_row_block,
                )
                break
            bet_id = int(row["id"])
            try:
                fill_price = float(row["fill_price"] or 0.0)
                fill_size = float(row["fill_size"] or 0.0)
                token_id = row["token_id"] or ""
                if fill_price <= 0 or fill_size <= 0 or not token_id:
                    counters["skipped"] += 1
                    continue

                # Clear a stale in-flight flag and retry.
                cif = _parse_close_in_flight(row["event_detail"])
                was_retry = False
                if cif is not None:
                    started = _flag_started_at(cif)
                    age = (now_utc - started) if started else None
                    if age is None or age > stale_threshold:
                        logger.warning(
                            "Clearing stale close_in_flight on row %s (age=%s)",
                            bet_id, age,
                        )
                        _clear_close_in_flight(conn, bet_id)
                        counters["stale_cleared"] += 1
                        was_retry = True
                    else:
                        counters["skipped"] += 1
                        continue

                # New closes only on the hour's first tick; stale retries are exempt.
                if (
                    local_now_minute is not None
                    and tick_index_for(station.icao, local_now_minute) >= 1
                    and not was_retry
                ):
                    counters["skipped"] += 1
                    continue

                if price_source is None:
                    counters["skipped"] += 1
                    continue
                book = _book_for(token_id)
                if book is None:
                    logger.info("No book for row %s token %s — skipping tick", bet_id, token_id)
                    counters["skipped"] += 1
                    continue
                bid_pair = _bid_for(token_id, book)
                if bid_pair is None:
                    logger.info("Empty bids for row %s — skipping tick", bet_id)
                    counters["skipped"] += 1
                    continue
                top_bid_price = float(bid_pair[0])
                if top_bid_price <= 0:
                    counters["skipped"] += 1
                    continue

                quote = quote_close_sell(book, fill_size)
                if quote is None:
                    logger.info(
                        "Insufficient executable bid depth for row %s size %.8f — skipping tick",
                        bet_id, fill_size,
                    )
                    counters["skipped"] += 1
                    continue
                _quote_size, executable_vwap, close_limit_price = quote

                fired = _evaluate_row(
                    fill_price=fill_price, bid=executable_vwap,
                    tp=cfg.tp, sl=cfg.sl,
                )
                if fired is None:
                    counters["skipped"] += 1
                    continue

                min_acceptable_vwap = (
                    fill_price + cfg.tp if fired == "tp" and cfg.tp is not None else None
                )
                max_acceptable_vwap = (
                    fill_price - cfg.sl if fired == "sl" and cfg.sl is not None else None
                )

                # Set the in-flight flag before closing.
                started_at = utc_now_sql()
                _set_close_in_flight(conn, bet_id, started_at)

                if dry_run:
                    reason = f"{reason_prefix}_{fired}_dry"
                    try:
                        record_position_close(
                            conn,
                            bet_id,
                            close_price=executable_vwap,
                            close_size=fill_size,
                            close_order_id=None,
                            reason=reason,
                            extra_detail={
                                "close_dry_run": True,
                                "trigger_move": executable_vwap - fill_price,
                                "top_bid_price": top_bid_price,
                                "close_limit_price": close_limit_price,
                            },
                        )
                        _clear_close_in_flight(conn, bet_id)
                    except Exception:
                        logger.error("dry-run close failed for row %s", bet_id, exc_info=True)
                        _clear_close_in_flight(conn, bet_id)
                        counters["errors"] += 1
                        continue
                else:
                    if order_client is None:
                        logger.error("Live monitor missing OrderClient; clearing flag")
                        _clear_close_in_flight(conn, bet_id)
                        counters["errors"] += 1
                        continue
                    result = order_client.close_position(
                        token_id,
                        target_size=fill_size,
                        min_acceptable_vwap=min_acceptable_vwap,
                        max_acceptable_vwap=max_acceptable_vwap,
                    )
                    if not result.success:
                        # Clear the flag so the next tick retries.
                        if getattr(result, "error_kind", None) == "stale_quote":
                            logger.info(
                                "close_position quote stale for row %s: %s — clearing flag",
                                bet_id, result.error,
                            )
                            _clear_close_in_flight(conn, bet_id)
                            counters["skipped"] += 1
                            continue
                        if _close_result_unknown_after_submit(result):
                            logger.warning(
                                "close_position outcome unknown for row %s: %s -- "
                                "keeping close_in_flight for reconciliation",
                                bet_id, result.error,
                            )
                            counters["errors"] += 1
                            continue
                        logger.warning(
                            "close_position failed for row %s: %s — clearing flag for retry",
                            bet_id, result.error,
                        )
                        _clear_close_in_flight(conn, bet_id)
                        counters["errors"] += 1
                        continue
                    reason = f"{reason_prefix}_{fired}"
                    verified_full_fill = _close_result_has_verified_full_fill(result, fill_size)
                    explicit_safe = _close_result_is_explicit_safe_response(result, fill_size)
                    if not verified_full_fill and not explicit_safe:
                        logger.warning(
                            "close_position success for row %s lacked verified full-fill "
                            "details (order_id=%s); keeping close_in_flight",
                            bet_id, result.order_id,
                        )
                        counters["errors"] += 1
                        continue
                    # The sell filled. If recording it fails, retry only the
                    # ledger write (never the sell), then mark ORPHAN_CLOSED.
                    close_price = float(result.fill_price or executable_vwap)
                    close_size = float(result.fill_size or fill_size)
                    record_kwargs = {
                        "close_price": close_price,
                        "close_size": close_size,
                        "close_order_id": result.order_id,
                        "close_transaction_hash": result.transaction_hash,
                        "reason": reason,
                        "extra_detail": {
                            "trigger_move": close_price - fill_price,
                            "trigger_quote_vwap": executable_vwap,
                            "top_bid_price": top_bid_price,
                            "close_limit_price": close_limit_price,
                        },
                    }
                    record_ok = False
                    last_err: Exception | None = None
                    for attempt in range(3):
                        try:
                            record_position_close(conn, bet_id, **record_kwargs)
                            record_ok = True
                            break
                        except Exception as exc:
                            last_err = exc
                            if attempt < 2:
                                time.sleep(0.5 * (2 ** attempt))
                    if record_ok:
                        _clear_close_in_flight(conn, bet_id)
                    else:
                        logger.error(
                            "record_position_close failed for row %s after successful "
                            "close (order_id=%s, fill_price=%s) — writing orphan_close + "
                            "transitioning to ORPHAN_CLOSED",
                            bet_id, result.order_id, result.fill_price,
                            exc_info=last_err,
                        )
                        # ORPHAN_CLOSED is terminal, so nothing re-sells it.
                        # If this write also fails, an operator must reconcile.
                        orphan_marker_ok = False
                        orphan_last_err: Exception | None = None
                        for orphan_attempt, backoff_ms in enumerate((100, 250, 500)):
                            try:
                                conn.execute(
                                    "UPDATE ledger SET "
                                    "  outcome = 'ORPHAN_CLOSED', "
                                    "  pnl = 0.0, "
                                    "  event_detail = json_set("
                                    "    COALESCE(event_detail, '{}'), "
                                    "    '$.orphan_close', json_object("
                                    "      'order_id', ?, 'fill_price', ?, "
                                    "      'fill_size', ?, 'reason', ?, 'started_at', ?"
                                    "    )"
                                    "  ) "
                                    "WHERE id = ? AND outcome = 'PENDING'",
                                    (
                                        str(result.order_id) if result.order_id else "",
                                        close_price,
                                        float(result.fill_size or fill_size),
                                        reason,
                                        started_at,
                                        bet_id,
                                    ),
                                )
                                conn.commit()
                                orphan_marker_ok = True
                                break
                            except sqlite3.OperationalError as exc:
                                orphan_last_err = exc
                                logger.warning(
                                    "orphan-close UPDATE attempt %d/3 failed for row %s: %s",
                                    orphan_attempt + 1, bet_id, exc,
                                )
                                if orphan_attempt < 2:
                                    time.sleep(backoff_ms / 1000.0)
                            except Exception as exc:
                                orphan_last_err = exc
                                break
                        if not orphan_marker_ok:
                            logger.warning(
                                "orphan-close marker write FAILED after 3 attempts for "
                                "row %s (order_id=%s, fill_price=%s); manual reconcile "
                                "required — row remains PENDING",
                                bet_id, result.order_id, result.fill_price,
                                exc_info=orphan_last_err,
                            )
                        from hightempbot.db.connection import log_pipeline_health
                        log_pipeline_health(
                            conn, station_id, "order", "ERROR",
                            f"bet #{bet_id} ORPHAN_CLOSED: closed at "
                            f"{result.fill_price} (order {result.order_id}); "
                            f"manual reconcile required",
                        )
                        if notify is not None:
                            try:
                                notify(
                                    f"{strategy} ORPHAN_CLOSED (manual reconcile required)",
                                    f"{station_id} bet #{bet_id}: closed at "
                                    f"{result.fill_price} (order {result.order_id}) "
                                    f"but ledger write failed -- row marked ORPHAN_CLOSED "
                                    f"with pnl=0; reconcile the realised close PnL manually.",
                                )
                            except Exception:
                                pass
                        counters["errors"] += 1
                        continue

                counters[f"{fired}_fired"] += 1
                if notify is not None:
                    side = "TP" if fired == "tp" else "SL"
                    mode = "DRY-RUN" if dry_run else "LIVE"
                    try:
                        notify(
                            f"{strategy} {side} fired ({mode})",
                            f"{station_id} bet #{bet_id}: vwap={executable_vwap:.4f} "
                            f"vs entry={fill_price:.4f} (move={executable_vwap - fill_price:+.4f})",
                        )
                    except Exception:
                        logger.warning("notify failed for row %s", bet_id, exc_info=True)

            except Exception:
                logger.error("Unexpected error monitoring row %s", bet_id, exc_info=True)
                counters["errors"] += 1
                continue

    logger.info(
        "TP/SL monitor %s/%s: tp=%d sl=%d skipped=%d stale_cleared=%d errors=%d",
        strategy, station_id, counters["tp_fired"], counters["sl_fired"],
        counters["skipped"], counters["stale_cleared"], counters["errors"],
    )
    return counters
