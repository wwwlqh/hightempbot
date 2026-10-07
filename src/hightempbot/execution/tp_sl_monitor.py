"""Take-profit / stop-loss monitor (per strategy).

Runs as a per-station scheduled job (5-min cadence, offset 3 min from the
betting tick — see scheduler/jobs.py). For each PENDING row of the requested
strategy, fetches the current YES bid book and fires a close when the full-size
executable bid-walk VWAP crosses the strategy's configured TP / SL threshold.

Strategies the monitor handles are exactly the ones whose StrategyConfig has a
non-None ``tp`` or ``sl``:
  * YMID  — TP=0.15
  * TAIL  — TP=0.20

NO and YHIGH set both to None and hold positions to resolution; the monitor
never touches them.

Crash-safety: writes ``event_detail.close_in_flight`` BEFORE invoking
``OrderClient.close_position`` so a crash between the order and the local
ledger flip doesn't double-sell on retry. The flag carries a 10-min age-out
(``TP_SL_FLAG_STALE_SECONDS``) — stale flags are cleared and the row reattempts
on the next tick. Retry-safe non-success results clear the flag in the same
call; ambiguous submit outcomes keep the flag so the next tick does not
double-sell while the close state is unknown.
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
        # utc_now_sql returns "YYYY-MM-DD HH:MM:SS" (UTC, no tz suffix).
        return datetime.strptime(val, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _set_close_in_flight(conn: sqlite3.Connection, bet_id: int, started_at: str) -> None:
    """Atomically merge close_in_flight into event_detail.

    json_patch on SQLite (3.34+) merges keys; we use json_set as a simpler form
    that works across the live server's 3.34.1 build.
    """
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
    """Return 'tp' if TP fires, 'sl' if SL fires, else None.

    Move is computed in absolute price units (YES side):
      move = bid - fill_price
    TP fires when configured and move >= tp (favorable). SL fires when
    configured and move <= -sl (adverse).
    """
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
    """Run one TP/SL monitor pass for ``station`` against one strategy.

    Args:
        station: StationConfig (uses .icao only).
        db_path: Per-thread DB path; opens its own connection.
        strategy: Strategy key in STRATEGY_CONFIGS (e.g. "YMID", "TAIL"). The
            monitor reads only PENDING rows tagged with this strategy and
            evaluates them against the strategy's tp/sl. Defaults to "YMID"
            for backward compatibility with legacy callers.
        order_client: Live OrderClient. Required when dry_run=False.
        reader: ClobReader for price reads. When None and dry_run=True, a
            no-op pass runs (no monitor action). Production callers always
            provide either reader or order_client (which subclasses ClobReader).
        dry_run: When True, simulates the close at the full-size executable
            bid-walk VWAP instead of submitting a sell order. Records CLOSED with the
            simulated fill price + ``reason='<strategy>_tp_dry'`` etc.
        notify: Optional callable(title, body) for push notifications.
        local_now_minute: Station-local current minute. When set, the
            hourly-first-tick gate is enforced: NEW closes fire only at
            tick 0 (minutes 0-9 of the station local hour). A stale-cleared
            close_in_flight retry is allowed at any tick. When None, the
            gate is skipped (back-compat for tests).

    Returns: dict of counters: {"tp_fired": N, "sl_fired": M, "skipped": K,
    "stale_cleared": J, "errors": E}.
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

        # Mode-scope the scan: in live mode skip stale dry_run rows so the
        # monitor never calls close_position on a token whose actual size on
        # Polymarket is zero. In dry_run mode skip live rows for symmetry —
        # a paper-trading session shouldn't touch real positions even if the
        # ledger somehow contains them.
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

        # Group pending rows by token_id so each unique CLOB book is fetched
        # once per tick instead of once per row. Multi-row TAIL/YMID slots
        # (cross-tick top-up) commonly share a token_id; dedup here avoids
        # the 3-7x book-fetch fan-out per slot.
        # Books are cached in this dict: token_id → book (or None on miss).
        # ``best_bid_cache`` follows the same shape for the bid extraction.
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
            # re-check operator-control gate per row.
            # TRANSFER_LOCK or Stop pressed mid-monitor must halt remaining
            # closes, not just the next tick.
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

                # F-001: stale-flag age-out. If a previous tick set the flag
                # but never reached close+record, clear and reattempt.
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
                        # Flag is fresh — another tick is mid-flight. Skip.
                        counters["skipped"] += 1
                        continue

                # --- Hourly-first-tick gate (2026-05-16) ---
                # Mirror the betting-side gate (decision.py): NEW close
                # decisions fire only at tick 0 of the station's local hour.
                # Later ticks skip new TP/SL fires so we don't chase
                # intra-hour bid spikes. Stale-flag retries are exempt — a
                # close that failed mid-attempt at tick 0 must still be
                # allowed to complete on tick 1+ regardless of the gate.
                # ``tick_index_for`` is offset-aware (see config.py).
                if (
                    local_now_minute is not None
                    and tick_index_for(station.icao, local_now_minute) >= 1
                    and not was_retry
                ):
                    counters["skipped"] += 1
                    continue

                # Fetch current bid book (F-007: None-safe). Book, top bid,
                # and executable close quote are cached/derived per token_id
                # within this tick so multi-row slots share one CLOB call.
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

                # F-001: set in-flight flag BEFORE the close call, with
                # a started_at timestamp for the age-out path.
                started_at = utc_now_sql()
                _set_close_in_flight(conn, bet_id, started_at)

                if dry_run:
                    # Simulated close: record CLOSED at executable full-size VWAP.
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
                        # Strip the in-flight flag from event_detail on
                        # success so CLOSED rows don't carry stale state
                        # forever (ce-review correctness #22 + adversarial
                        # ADV-006). _merge_event_detail filters out None
                        # values, so an explicit json_remove is required.
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
                        # F-001 (b): clear flag on non-success so next tick retries.
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
                    # Orphan-close recovery (ce-review reliability rel-005 +
                    # kieran-python #7): the exchange has already filled the
                    # close. If record_position_close raises, the ledger row
                    # is stranded as PENDING. Retry the LEDGER WRITE only
                    # (with bounded backoff) — never re-submit the close,
                    # that would attempt to sell a position we no longer
                    # hold and loop forever. If all retries fail, write an
                    # orphan_close marker so an operator can reconcile from
                    # the order_id + fill_price, and notify.
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
                        # Same flag-clearing rationale as the dry-run path.
                        _clear_close_in_flight(conn, bet_id)
                    else:
                        logger.error(
                            "record_position_close failed for row %s after successful "
                            "close (order_id=%s, fill_price=%s) — writing orphan_close + "
                            "transitioning to ORPHAN_CLOSED",
                            bet_id, result.order_id, result.fill_price,
                            exc_info=last_err,
                        )
                        # transition the row to the
                        # ORPHAN_CLOSED terminal state. Without this the row
                        # stays PENDING and the next monitor tick re-fires
                        # close_position against a wallet that holds zero
                        # shares (rel-005 cascade). Both TP/SL monitor
                        # (pending_positions_by_strategy filters on outcome=
                        # 'PENDING') and resolution settler ignore non-
                        # PENDING rows, so ORPHAN_CLOSED is fully inert.
                        # previously a single-shot UPDATE
                        # that silently dropped the orphan marker on a transient
                        # OperationalError (busy lock, IO timeout). Bounded retry
                        # at 100/250/500ms backoff; if all 3 fail the row stays
                        # PENDING and an operator must reconcile manually using
                        # the order_id + fill_price logged at WARNING.
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
                                # Non-transient (programmer error, schema): no retry.
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
                        # pipeline_health row so the dashboard surfaces this
                        # even when the Telegram path is broken.
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
