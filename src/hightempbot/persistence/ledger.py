"""Ledger recording — INSERT bets, events, and resolutions.

Source: parent R28-R31, origin R34, R62 (signal logging)
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

from hightempbot.db.connection import utc_now_sql
from hightempbot.execution.strategy_constants import POLY_FEE_THETA
from hightempbot.persistence.actuals import actual_source_clause
from hightempbot.resolution.gamma import winning_bracket_from_gamma
from hightempbot.execution.types import BetSignal, OrderResult

# Time bound for the resolution-label backfill: ignore resolved-but-unlabeled
# rows older than this. Bounds the per-tick HTTP cost and stops re-fetching
# permanently abandoned Polymarket markets every 15 min forever. UMA usually
# finalizes within hours; anything still ambiguous after 14 days won't change.
_RESOLUTION_BACKFILL_LOOKBACK_DAYS = 14

logger = logging.getLogger(__name__)


def poly_fee_per_share(price: float, *, fee_theta: float = POLY_FEE_THETA) -> float:
    """Polymarket V2 taker fee per share at ``price``: θ × p × (1 − p).

    Peaks at p=0.50. Callers must ensure ``price`` is a finite float in
    (0, 1); use ``poly_fee_charge`` if you need defensive coercion.
    """
    return fee_theta * price * (1.0 - price)


def poly_fee_charge(price: float | None, shares: float | None) -> float:
    """Polymarket V2 taker fee in USDC for ``shares`` filled at ``price``.

    Formula (weather / economics / culture / other category):
        fee = shares × θ × p × (1 − p),  with θ = POLY_FEE_THETA = 0.05.
    Maker rebates are not modeled — taker is the conservative direction.
    Returns 0.0 on missing/invalid inputs so callers can use it unconditionally.
    """
    try:
        p = float(price)
        s = float(shares)
    except (TypeError, ValueError):
        return 0.0
    if not (p > 0 and p < 1 and s > 0):
        return 0.0
    return s * poly_fee_per_share(p)


def _bet_entry_fee(row) -> float:
    """Recompute the entry-side Polymarket fee for an existing ledger row.

    Falls back to ``fill_price``/``fill_size`` (or ``bet_size/fill_price``) when
    the historical row has no ``poly_entry_fee`` cached in event_detail.
    """
    detail = decode_event_detail(row["event_detail"]) if "event_detail" in row.keys() else {}
    cached = detail.get("poly_entry_fee")
    if cached is not None:
        try:
            return float(cached)
        except (TypeError, ValueError):
            pass
    price = row["fill_price"] if "fill_price" in row.keys() else None
    size = row["fill_size"] if "fill_size" in row.keys() else None
    if size is None:
        bet_size = row["bet_size"] if "bet_size" in row.keys() else None
        if bet_size is not None and price:
            try:
                size = float(bet_size) / float(price)
            except (TypeError, ValueError, ZeroDivisionError):
                size = None
    return poly_fee_charge(price, size)


def _close_exit_fee(row, detail: dict) -> float:
    """Return the exit-side fee for a CLOSED row, using cached detail first."""
    cached = detail.get("poly_exit_fee")
    if cached is not None:
        try:
            return float(cached)
        except (TypeError, ValueError):
            pass

    close_price = detail.get("close_price") or detail.get("close_vwap_quote")
    close_size = detail.get("close_size")
    if close_size is None and "fill_size" in row.keys():
        close_size = row["fill_size"]
    if close_size is None:
        fill_price = row["fill_price"] if "fill_price" in row.keys() else None
        bet_size = row["bet_size"] if "bet_size" in row.keys() else None
        if fill_price and bet_size:
            try:
                close_size = float(bet_size) / float(fill_price)
            except (TypeError, ValueError, ZeroDivisionError):
                close_size = None
    return poly_fee_charge(close_price, close_size)


def backfill_fee_adjusted_pnl(conn: sqlite3.Connection) -> int:
    """Convert historical settled ledger PnL from gross to net-of-fees.

    New resolution/close writes persist ``pnl_gross`` in event_detail, so this
    migration skips those rows. Historical rows without that marker are treated
    as gross PnL and adjusted exactly once.
    """
    rows = conn.execute(
        """SELECT id, outcome, pnl, bet_size, fill_price, fill_size, event_detail
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN', 'LOSS', 'PUSH', 'CLOSED')
          AND pnl IS NOT NULL"""
    ).fetchall()

    updated = 0
    adjusted_at = utc_now_sql()
    for row in rows:
        detail = decode_event_detail(row["event_detail"])
        if detail.get("pnl_gross") is not None or detail.get("fee_adjusted_at") is not None:
            continue

        try:
            pnl_gross = float(row["pnl"] or 0.0)
        except (TypeError, ValueError):
            continue

        entry_fee = _bet_entry_fee(row)
        exit_fee = _close_exit_fee(row, detail) if row["outcome"] == "CLOSED" else 0.0
        pnl_net = pnl_gross - entry_fee - exit_fee
        event_detail = _merge_event_detail(
            row["event_detail"],
            {
                "pnl_gross": pnl_gross,
                "poly_entry_fee": entry_fee,
                "poly_exit_fee": exit_fee,
                "fee_adjusted_at": adjusted_at,
            },
        )
        conn.execute(
            "UPDATE ledger SET pnl = ?, event_detail = ? WHERE id = ?",
            (pnl_net, event_detail, row["id"]),
        )
        updated += 1

    if updated:
        conn.commit()
    return updated


def decode_event_detail(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _encode_event_detail(detail: dict) -> str:
    return json.dumps(detail, sort_keys=True)


def _merge_event_detail(raw: str | None, updates: dict) -> str:
    detail = decode_event_detail(raw)
    detail.update({k: v for k, v in updates.items() if v is not None})
    return _encode_event_detail(detail)


def _coerce_positive_float(value: object) -> float | None:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _normalized_fill_levels(order_result: OrderResult | None) -> list[dict[str, float]] | None:
    if order_result is None:
        return None
    out: list[dict[str, float]] = []
    for level in order_result.fill_levels or []:
        if not isinstance(level, dict):
            continue
        price = _coerce_positive_float(level.get("price"))
        shares = _coerce_positive_float(level.get("shares"))
        usd = _coerce_positive_float(level.get("usd"))
        if price is None:
            continue
        if usd is None and shares is not None:
            usd = price * shares
        if shares is None and usd is not None:
            shares = usd / price
        if shares is None or usd is None:
            continue
        out.append({
            "price": round(price, 4),
            "shares": round(shares, 8),
            "usd": round(usd, 2),
        })
    return out or None


def _execution_detail_updates(order_result: OrderResult | None) -> dict[str, object]:
    return {"fill_levels": levels} if (levels := _normalized_fill_levels(order_result)) else {}


def _merge_execution_detail(
    conn: sqlite3.Connection,
    bet_id: int,
    order_result: OrderResult | None,
) -> None:
    updates = _execution_detail_updates(order_result)
    if not updates:
        return
    row = conn.execute("SELECT event_detail FROM ledger WHERE id = ?", (bet_id,)).fetchone()
    if row is None:
        return
    conn.execute(
        "UPDATE ledger SET event_detail = ? WHERE id = ?",
        (_merge_event_detail(row["event_detail"], updates), bet_id),
    )


def _build_bet_event_detail(signal: BetSignal) -> str | None:
    """Serialize a BetSignal's gate context + strategy metadata into event_detail JSON.

    Keys written:
      - All entries from `signal.gate_results` (except internal-prefix keys
        starting with `_`, which are pulled out for first-class fields below).
      - bracket_low / bracket_high / bracket_unit
      - wu_forecast_c / wu_consensus_verdict
      - strategy / signal_used / signal_value (4-stack router, F-009/F-011 fix)
      - tail_votes (only when strategy == "TAIL"; carries the 4-component
        breakdown stashed by `_evaluate_strategy` under gate_results["_tail_votes"]).

    Returns None when the signal has no gate context to record (preserves the
    legacy single-gate behavior where empty gate_results suppressed event_detail).
    """
    if not signal.gate_results and not signal.strategy:
        return None
    public_gates = {k: v for k, v in (signal.gate_results or {}).items() if not k.startswith("_")}
    detail: dict[str, object] = {
        **public_gates,
        "bracket_low": signal.bracket_low,
        "bracket_high": signal.bracket_high,
        "bracket_unit": signal.bracket_unit,
        "wu_forecast_c": signal.wu_forecast_c,
        "wu_consensus_verdict": signal.wu_consensus_verdict,
    }
    if signal.strategy:
        detail["strategy"] = signal.strategy
    if signal.signal_used:
        detail["signal_used"] = signal.signal_used
    if signal.signal_value is not None:
        detail["signal_value"] = signal.signal_value
    # Sticky entry_top_price: persisted on the slot's first fill so subsequent
    # top-up ticks read it back as the walker's walk_anchor_price. Re-stamped
    # on every top-up row (same value — idempotent). is-not-None so a 0.0
    # sentinel never silently drops.
    if signal.entry_top_price is not None:
        detail["entry_top_price"] = signal.entry_top_price
    # Sticky entry_fill_vwap: the slot's first row stamps its decision-time
    # book-walked VWAP; later top-ups read the EARLIEST row's value back
    # (slot_state) as the VWAP-slip anchor. Re-stamped on every row (each row's
    # own walk VWAP — only the earliest is read as the anchor). is-not-None so a
    # 0.0 sentinel never silently drops. Added 2026-05-29 for the slip guard.
    if signal.entry_fill_vwap is not None:
        detail["entry_fill_vwap"] = signal.entry_fill_vwap
    # F-009: TAIL components persisted only when the strategy fired.
    tail_votes = (signal.gate_results or {}).get("_tail_votes")
    if signal.strategy == "TAIL" and isinstance(tail_votes, dict) and tail_votes:
        detail["tail_votes"] = tail_votes
    return json.dumps(detail)


def pending_positions_by_strategy(
    conn: sqlite3.Connection,
    strategy: str,
    *,
    station_id: str | None = None,
    dry_run: bool | None = None,
) -> list[sqlite3.Row]:
    """Return PENDING ledger rows tagged with the given strategy.

    Used by the YMID TP/SL monitor to find rows that need their current
    market price checked against TP/SL thresholds. Only `outcome = 'PENDING'`
    rows are returned; CLOSED / WIN / LOSS / CANCELLED / EXPIRED are excluded.

    `dry_run` filters by the bot's current operating mode:
    * ``True``  -> only ``event_type='dry_run'`` rows
    * ``False`` -> only ``event_type='bet'`` rows
    * ``None``  -> both (default; preserves legacy callers)

    The dry_run filter prevents a critical cross-mode bug: after flipping
    ``DRY_RUN=False`` and restarting, the live TP/SL monitor would otherwise
    iterate stale dry_run PENDING rows and call ``order_client.close_position``
    on token_ids whose actual size on Polymarket is zero — most fail benignly
    but any token-id collision with a real position triggers an unintended
    sell.

    Legacy rows (placed before the per-strategy router shipped) have no
    `strategy` key in event_detail; `COALESCE(...,'NO')` defaults them to
    'NO' so they do NOT appear in YMID monitor scans.
    """
    if dry_run is True:
        event_types: tuple[str, ...] = ("dry_run",)
    elif dry_run is False:
        event_types = ("bet",)
    else:
        event_types = ("bet", "dry_run")
    placeholders = ",".join("?" for _ in event_types)
    sql = (
        f"SELECT id, station_id, market_id, token_id, target_date, "
        f"       threshold, side, fill_price, fill_size, bet_size, "
        f"       bet_ts, event_detail "
        f"FROM ledger "
        f"WHERE outcome = 'PENDING' "
        f"  AND event_type IN ({placeholders}) "
        f"  AND COALESCE(json_extract(event_detail, '$.strategy'), 'NO') = ?"
    )
    params: list[object] = [*event_types, strategy]
    if station_id is not None:
        sql += " AND station_id = ?"
        params.append(station_id)
    return list(conn.execute(sql, params).fetchall())


def _slot_predicate(
    *,
    station_id: str,
    target_date: str,
    threshold_val: float,
    side: str,
    bracket_low_val: float | None,
    strategy: str,
    ledger_event_types: tuple[str, ...],
) -> tuple[str, tuple[object, ...]]:
    """Build the WHERE clause + params identifying a slot's non-cancelled rows.

    A "slot" is the (station_id, target_date, threshold, side, bracket_low,
    strategy) tuple — exactly what the legacy ``_strategy_idempotency_count``
    keyed on. Centralizing the predicate so the consolidated ``slot_state``
    SUM and anchor reads stay aligned on which rows belong to the slot.

    Preserves one compatibility behavior from the legacy count gate:
    - Floor-bracket bets carry NULL ``bracket_low``; dispatch on
      ``bracket_low_val is None`` and use IS NULL explicitly (not a sentinel).

    Legacy NULL-strategy rows (written before the per-strategy router) match
    only when ``strategy='NO'`` via COALESCE(...,'NO'); they no longer
    over-match every strategy key.
    """
    if bracket_low_val is None:
        bracket_clause = "json_extract(event_detail, '$.bracket_low') IS NULL"
        bracket_params: tuple[object, ...] = ()
    else:
        bracket_clause = "json_extract(event_detail, '$.bracket_low') = ?"
        bracket_params = (bracket_low_val,)

    placeholders = ",".join("?" for _ in ledger_event_types)
    where = (
        f"station_id = ? AND target_date = ? AND threshold = ? AND side = ? "
        f"AND {bracket_clause} "
        f"AND COALESCE(json_extract(event_detail, '$.strategy'), 'NO') = ? "
        f"AND event_type IN ({placeholders}) "
        f"AND outcome != 'CANCELLED'"
    )
    params = (
        station_id, target_date, threshold_val, side,
        *bracket_params,
        strategy,
        *ledger_event_types,
    )
    return where, params


def slot_state(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    target_date: str,
    threshold_val: float,
    side: str,
    bracket_low_val: float | None,
    strategy: str,
    ledger_event_types: tuple[str, ...],
) -> tuple[float, float | None, float | None]:
    """Return ``(slot_filled_usd, slot_anchor_price, slot_first_fill_vwap)`` in ONE query.

    Single SELECT against the slot predicate covers the cumulative
    non-cancelled exposure (USD), the sticky walker price anchor, and the
    slot's first-fill book-walked VWAP. The decision-time hot path calls this
    on every bracket; the merge keeps it to one SQL round-trip vs the legacy
    multi-call pattern removed 2026-05-23.

    Anchor sources, both from the earliest non-cancelled row
    (``ORDER BY bet_ts ASC, id ASC``):
      - ``slot_anchor_price`` = ``event_detail.entry_top_price`` — the sticky
        price leash (YMID/YHIGH) and dedup signal.
      - ``slot_first_fill_vwap`` = ``event_detail.entry_fill_vwap`` — the
        VWAP-slip anchor (NO/TAIL top-ups). None for legacy/transition rows
        written before the 2026-05-29 slip guard; callers must treat None as
        "no slip cap" rather than re-anchoring to the live book.

    Returns ``(0.0, None, None)`` when the slot has no matching rows.

    Honors ``MAX_PENDING_AGE_MINUTES`` stale-PENDING exclusion. The anchor
    reads are NOT age-gated: the earliest row's anchors are the slot's sticky
    references regardless of its current outcome — that's the invariant the
    top-up plan locks.
    """
    from hightempbot.execution.strategy_constants import MAX_PENDING_AGE_MINUTES

    where, params = _slot_predicate(
        station_id=station_id,
        target_date=target_date,
        threshold_val=threshold_val,
        side=side,
        bracket_low_val=bracket_low_val,
        strategy=strategy,
        ledger_event_types=ledger_event_types,
    )
    age_minutes = int(max(0, MAX_PENDING_AGE_MINUTES))
    # One round-trip: aggregate SUM/COUNT alongside a correlated anchor
    # subquery scoped to the same predicate.
    sql = (
        f"SELECT "
        f"COALESCE(SUM(bet_size), 0) AS filled, "
        f"SUM(CASE WHEN outcome != 'PENDING' THEN 1 ELSE 0 END) AS n_filled, "
        f"(SELECT json_extract(event_detail, '$.entry_top_price') "
        f" FROM ledger WHERE {where} "
        f" ORDER BY bet_ts ASC, id ASC LIMIT 1) AS anchor, "
        f"(SELECT json_extract(event_detail, '$.entry_fill_vwap') "
        f" FROM ledger WHERE {where} "
        f" ORDER BY bet_ts ASC, id ASC LIMIT 1) AS first_fill_vwap "
        f"FROM ledger WHERE {where}"
    )
    # Note: the WHERE clause is reused three times in the SQL string (anchor
    # subquery, vwap subquery, main FROM — in that order) so the bound-parameter
    # tuple must be tripled to match.
    row = conn.execute(sql, (*params, *params, *params)).fetchone()
    if row is None:
        return 0.0, None, None
    n_filled = int(row["n_filled"] or 0)
    total = float(row["filled"] or 0.0)
    if n_filled > 0 and age_minutes > 0:
        stale_sql = (
            f"SELECT COALESCE(SUM(bet_size), 0) AS stale FROM ledger WHERE {where} "
            f"AND outcome = 'PENDING' "
            f"AND bet_ts < datetime('now', '-{age_minutes} minutes')"
        )
        stale_row = conn.execute(stale_sql, params).fetchone()
        stale_total = float(stale_row["stale"] or 0.0) if stale_row is not None else 0.0
        total = max(0.0, total - stale_total)

    def _coerce(raw: object) -> float | None:
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    return total, _coerce(row["anchor"]), _coerce(row["first_fill_vwap"])


def record_bet(
    conn: sqlite3.Connection,
    signal: BetSignal,
    order_result: OrderResult | None,
    dry_run: bool,
) -> int:
    """Insert a bet into the ledger table. Returns the row ID."""
    now = utc_now_sql()
    event_type = "dry_run" if dry_run else "bet"
    dry_run_fill_price = signal.fill_price if dry_run and signal.fill_price > 0 else None
    dry_run_fill_size = (
        signal.bet_size_usd / signal.fill_price
        if dry_run_fill_price and signal.bet_size_usd > 0
        else None
    )
    stored_fill_price = order_result.fill_price if order_result else dry_run_fill_price
    stored_fill_size = order_result.fill_size if order_result else dry_run_fill_size
    stored_fill_ts = order_result.fill_ts if order_result else (now if dry_run_fill_price else None)
    event_detail = _build_bet_event_detail(signal)
    execution_updates = _execution_detail_updates(order_result)
    if execution_updates:
        event_detail = _merge_event_detail(event_detail, execution_updates)

    pred_bucket_low = signal.pred_bucket[0] if signal.pred_bucket else None
    pred_bucket_high = signal.pred_bucket[1] if signal.pred_bucket else None
    cursor = conn.execute(
        """INSERT INTO ledger
        (bet_ts, station_id, market_id, token_id, target_date,
         horizon, threshold, side, p_model, p_market, edge,
         kelly_size, volume_cap, bet_size, limit_price,
         order_id, fill_price, fill_size, fill_ts,
         outcome, pnl, kelly_multiplier, event_type, event_detail,
         prob_safe_floor, pred_bucket_low, pred_bucket_high, n_bucket)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now,
            signal.station_id,
            signal.market_id,
            signal.token_id,
            signal.target_date,
            signal.horizon,
            signal.threshold,
            signal.side,
            signal.p_model,
            signal.p_market,
            signal.edge,
            signal.bet_size_usd,
            signal.volume_usd or 0.0,
            signal.bet_size_usd,  # bet_size = kelly_size after caps
            signal.limit_price if signal.limit_price > 0 else signal.fill_price,
            order_result.order_id if order_result else None,
            stored_fill_price,
            stored_fill_size,
            stored_fill_ts,
            "PENDING",  # both live and dry-run use PENDING for capital tracking
            None,  # pnl computed at resolution
            None,  # kelly_multiplier (legacy, not used in Phase 2)
            event_type,
            event_detail,
            signal.prob_safe_floor,
            pred_bucket_low,
            pred_bucket_high,
            signal.n_bucket,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def record_signal(
    conn: sqlite3.Connection,
    signal: BetSignal,
    tick_ts: str,
    outcome_label: str,
) -> None:
    """Write a signal row to the signals table for dashboard display.

    Called for every evaluated bracket — pass or fail.
    """
    gates = signal.gate_results
    conn.execute(
        """INSERT INTO signals
        (created_at, station_id, target_date, bracket_label, side,
         p_model, p_fill, edge, kelly_size_usd, volume_usd,
         gate_bss, gate_edge, gate_fill_price,
         gate_volume, gate_daily_exposure,
         gate_idempotency, passed_all_gates, outcome)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tick_ts,
            signal.station_id,
            signal.target_date,
            signal.bracket_label,
            signal.side,
            signal.p_model,
            signal.fill_price,
            signal.edge,
            signal.bet_size_usd,
            signal.volume_usd,
            _gate_to_int(_gate_value(gates, "lut", "bss")),
            _gate_to_int(_gate_value(gates, "edge_gate", "lcb_gate", "edge")),
            _gate_to_int(_gate_value(gates, "max_per_market", "fill_price")),
            _gate_to_int(_gate_value(gates, "volume")),
            _gate_to_int(_gate_value(gates, "daily_notional", "daily_exposure")),
            _gate_to_int(_gate_value(gates, "idempotency")),
            1 if signal.passed_all_gates else 0,
            outcome_label,
        ),
    )
    conn.commit()


def _gate_value(gates: dict[str, bool | str | None], *keys: str) -> bool | str | None:
    """Return the first present gate value from ``gates``."""
    for key in keys:
        if key in gates:
            return gates[key]
    return None


def _gate_to_int(val: bool | None) -> int | None:
    """Convert gate result to integer for DB storage. None = not evaluated."""
    if val is None:
        return None
    if not isinstance(val, bool):
        return None
    return 1 if val else 0


def record_resolution(
    conn: sqlite3.Connection,
    bet_id: int,
    actual_tmax: float | None,
    outcome: str,
    pnl: float,
    *,
    resolution_label: str | None = None,
    resolution_source: str | None = None,
    resolution_price: float | None = None,
    extra_detail: dict | None = None,
) -> bool:
    """Update a ledger row with resolution outcome and net-of-fees P&L.

    The caller passes the gross PnL computed from fill_price and bet_size;
    this function subtracts the Polymarket entry-side taker fee so the
    stored ``pnl`` matches what the wallet would actually realise. The fee
    amount is also persisted to ``event_detail.poly_entry_fee`` so the
    dashboard can sum it.

    Returns ``True`` iff a row was actually written. Callers must respect
    this when incrementing counters — a concurrent scheduler/operator race
    can otherwise double-count the same resolution and lie to pipeline_health
    (finding #12).
    """
    row = conn.execute(
        "SELECT outcome, bet_size, fill_price, fill_size, event_detail FROM ledger WHERE id = ?",
        (bet_id,),
    ).fetchone()
    if row is None:
        logger.warning("record_resolution: ledger row %s not found", bet_id)
        return False
    if row["outcome"] != "PENDING":
        logger.info(
            "record_resolution: row %s already terminal (%s), skipping %s",
            bet_id,
            row["outcome"],
            outcome,
        )
        return False

    entry_fee = _bet_entry_fee(row)
    pnl_net = float(pnl) - entry_fee

    detail_updates = dict(extra_detail or {})
    if resolution_label:
        detail_updates["resolution_actual_label"] = resolution_label
    if resolution_source:
        detail_updates["resolution_source"] = resolution_source
    if resolution_price is not None:
        detail_updates["resolution_price"] = float(resolution_price)
    if entry_fee > 0:
        detail_updates["poly_entry_fee"] = entry_fee
        detail_updates["pnl_gross"] = float(pnl)

    event_detail = _merge_event_detail(
        row["event_detail"],
        detail_updates,
    )
    cur = conn.execute(
        """UPDATE ledger
        SET actual_tmax = ?, outcome = ?, pnl = ?, event_detail = ?
        WHERE id = ? AND outcome = 'PENDING'""",
        (actual_tmax, outcome, pnl_net, event_detail, bet_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        logger.info(
            "record_resolution: row %s changed state before resolution write; skipped",
            bet_id,
        )
        return False
    return True


def record_position_close(
    conn: sqlite3.Connection,
    bet_id: int,
    *,
    close_price: float,
    close_size: float | None = None,
    close_order_id: str | None = None,
    close_transaction_hash: str | None = None,
    reason: str = "no_stop_close",
    extra_detail: dict | None = None,
) -> float:
    """Mark a pending position as closed and return realized close P&L."""
    row = conn.execute(
        """SELECT outcome, bet_size, fill_price, fill_size, event_detail
        FROM ledger
        WHERE id = ?""",
        (bet_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"ledger row {bet_id} not found")
    if row["outcome"] != "PENDING":
        raise ValueError(f"ledger row {bet_id} is not PENDING (outcome={row['outcome']})")

    bet_size = float(row["bet_size"] or 0.0)
    fill_price = float(row["fill_price"] or 0.0)
    held_size = close_size if close_size is not None else row["fill_size"]
    if held_size is None and fill_price > 0:
        held_size = bet_size / fill_price
    if held_size is None or float(held_size) <= 0:
        raise ValueError(f"ledger row {bet_id} has no closeable fill_size")

    held_size = float(held_size)
    close_price = float(close_price)
    pnl_gross = close_price * held_size - bet_size

    # Polymarket charges taker fees on BOTH the entry buy and the exit sell.
    # Stops are the only flow that hits both legs (winners redeem at $1
    # without a second trade), so fold both sides in here.
    entry_fee = _bet_entry_fee(row)
    exit_fee = poly_fee_charge(close_price, held_size)
    pnl_net = pnl_gross - entry_fee - exit_fee

    updates = {
        "close_reason": reason,
        "close_price": close_price,
        "close_size": held_size,
        "close_pnl": pnl_net,
        "closed_at": utc_now_sql(),
        "close_order_id": close_order_id,
        "close_transaction_hash": close_transaction_hash,
        "pnl_gross": pnl_gross,
        "poly_entry_fee": entry_fee,
        "poly_exit_fee": exit_fee,
    }
    updates.update(extra_detail or {})
    event_detail = _merge_event_detail(row["event_detail"], updates)

    cur = conn.execute(
        """UPDATE ledger
        SET outcome = 'CLOSED',
            pnl = ?,
            event_detail = ?
        WHERE id = ? AND outcome = 'PENDING'""",
        (pnl_net, event_detail, bet_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(f"ledger row {bet_id} changed state before close write")
    return pnl_net


def update_pending_bet_after_execution(
    conn: sqlite3.Connection,
    bet_id: int,
    *,
    dry_run: bool,
    order_result: OrderResult | None = None,
) -> None:
    """Finalize the submission state for a pre-inserted pending row."""
    final_stake = None
    if order_result is not None:
        if order_result.bet_size_usd is not None:
            final_stake = float(order_result.bet_size_usd)
        elif (
            order_result.fill_price is not None
            and order_result.fill_size is not None
        ):
            final_stake = float(order_result.fill_price) * float(order_result.fill_size)

    if dry_run:
        dry_tx = None
        dry_fill_price = None
        dry_fill_size = None
        dry_fill_ts = None
        dry_realized_edge = None
        if order_result is not None:
            dry_tx = order_result.transaction_hash
            dry_fill_price = order_result.fill_price
            dry_fill_size = order_result.fill_size
            dry_fill_ts = order_result.fill_ts
            dry_realized_edge = order_result.realized_edge
        # dry-run paths now also refresh bet_size from
        # the order_result so dashboards and downstream sims see the realised
        # notional, not the pre-walker target. COALESCE keeps the original
        # value when the result didn't carry one.
        conn.execute(
            """UPDATE ledger
            SET order_id = COALESCE(order_id, ?),
                fill_price = COALESCE(?, fill_price),
                fill_size = COALESCE(?, fill_size),
                fill_ts = COALESCE(?, fill_ts),
                realized_edge = COALESCE(?, realized_edge),
                transaction_hash = COALESCE(?, transaction_hash),
                bet_size = COALESCE(?, bet_size),
                verify_attempts = 0
            WHERE id = ?""",
            (
                f"DRY_RUN_{bet_id}",
                dry_fill_price,
                dry_fill_size,
                dry_fill_ts,
                dry_realized_edge,
                dry_tx,
                final_stake,
                bet_id,
            ),
        )
        _merge_execution_detail(conn, bet_id, order_result)
        conn.commit()
        return

    if order_result is not None and order_result.leave_pending:
        conn.execute(
            """UPDATE ledger
            SET order_id = COALESCE(order_id, ?),
                limit_price = COALESCE(?, limit_price),
                transaction_hash = COALESCE(?, transaction_hash),
                verify_attempts = ?,
                verification_downgraded = ?
            WHERE id = ?""",
            (
                order_result.order_id,
                order_result.limit_price,
                order_result.transaction_hash,
                order_result.verify_attempts,
                1 if order_result.verification_downgraded else 0,
                bet_id,
            ),
        )
        conn.commit()
        return

    if order_result is not None and order_result.success:
        # the success branch must NOT wipe real values
        # with None. The walker anchors fill_price/fill_size to walker output
        # before declaring success (walker.py: result.fill_price = filled_vwap
        # if result.fill_price is None), so reaching this point with None is a
        # contract violation. Refuse to write a half-complete row -- route
        # through the cancel branch so the operator can reconcile manually.
        if order_result.fill_price is None or order_result.fill_size is None:
            logger.error(
                "update_pending_bet_after_execution: success=True but "
                "fill_price=%r fill_size=%r on bet_id=%s -- treating as "
                "CANCELLED to avoid NULL-wiping the row",
                order_result.fill_price, order_result.fill_size, bet_id,
            )
            conn.execute(
                """UPDATE ledger
                SET order_id = COALESCE(order_id, ?),
                    outcome = 'CANCELLED',
                    pnl = 0.0,
                    verify_attempts = ?,
                    transaction_hash = COALESCE(?, transaction_hash)
                WHERE id = ?""",
                (
                    order_result.order_id,
                    order_result.verify_attempts,
                    order_result.transaction_hash,
                    bet_id,
                ),
            )
            conn.commit()
            return
        conn.execute(
            """UPDATE ledger
            SET order_id = ?,
                limit_price = COALESCE(?, limit_price),
                fill_price = COALESCE(?, fill_price),
                fill_size = COALESCE(?, fill_size),
                fill_ts = COALESCE(?, fill_ts),
                kelly_size = COALESCE(?, kelly_size),
                bet_size = COALESCE(?, bet_size),
                realized_edge = COALESCE(?, realized_edge),
                transaction_hash = COALESCE(?, transaction_hash),
                verify_attempts = ?,
                verification_downgraded = ?
            WHERE id = ?""",
            (
                order_result.order_id,
                order_result.limit_price,
                order_result.fill_price,
                order_result.fill_size,
                order_result.fill_ts,
                final_stake,
                final_stake,
                order_result.realized_edge,
                order_result.transaction_hash,
                order_result.verify_attempts,
                1 if order_result.verification_downgraded else 0,
                bet_id,
            ),
        )
        _merge_execution_detail(conn, bet_id, order_result)
    else:
        cancel_attempts = order_result.verify_attempts if order_result is not None else 0
        cancel_tx = order_result.transaction_hash if order_result is not None else None
        cancel_order_id = order_result.order_id if order_result is not None else None
        conn.execute(
            """UPDATE ledger
            SET order_id = COALESCE(order_id, ?),
                outcome = 'CANCELLED',
                pnl = 0.0,
                verify_attempts = ?,
                transaction_hash = COALESCE(?, transaction_hash)
            WHERE id = ?""",
            (cancel_order_id, cancel_attempts, cancel_tx, bet_id),
        )
    conn.commit()


def backfill_resolved_actual_for_station_date(
    conn: sqlite3.Connection,
    station_id: str,
    target_date: str,
) -> int:
    """Fill missing actual_tmax for resolved rows matching one station/date."""
    actuals_clause, actuals_params = actual_source_clause("a")
    result = conn.execute(
        f"""UPDATE ledger
        SET actual_tmax = (
            SELECT a.tmax_celsius
            FROM actuals a
            WHERE a.station_id = ledger.station_id
              AND a.local_date = ledger.target_date
              AND {actuals_clause}
        )
        WHERE station_id = ?
          AND target_date = ?
          AND event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN', 'LOSS')
          AND actual_tmax IS NULL
          AND EXISTS (
              SELECT 1
              FROM actuals a
              WHERE a.station_id = ledger.station_id
                AND a.local_date = ledger.target_date
                AND {actuals_clause}
          )""",
        (*actuals_params, station_id, target_date, *actuals_params),
    )
    conn.commit()
    _backfill_wu_actual_metadata(conn, station_id, target_date)
    return result.rowcount


def backfill_all_resolved_actuals(conn: sqlite3.Connection) -> int:
    """Fill missing actual_tmax for all resolved rows that already have actuals."""
    actuals_clause, actuals_params = actual_source_clause("a")
    result = conn.execute(
        f"""UPDATE ledger
        SET actual_tmax = (
            SELECT a.tmax_celsius
            FROM actuals a
            WHERE a.station_id = ledger.station_id
              AND a.local_date = ledger.target_date
              AND {actuals_clause}
        )
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN', 'LOSS')
          AND actual_tmax IS NULL
          AND EXISTS (
              SELECT 1
              FROM actuals a
              WHERE a.station_id = ledger.station_id
                AND a.local_date = ledger.target_date
                AND {actuals_clause}
          )""",
        (*actuals_params, *actuals_params),
    )
    conn.commit()
    actuals_clause, actuals_params = actual_source_clause("a")
    pairs = conn.execute(
        f"""SELECT DISTINCT l.station_id, l.target_date
        FROM ledger l
        JOIN actuals a
          ON a.station_id = l.station_id
         AND a.local_date = l.target_date
         AND {actuals_clause}
        WHERE l.event_type IN ('bet', 'dry_run')
          AND l.outcome IN ('WIN', 'LOSS', 'CLOSED')""",
        actuals_params,
    ).fetchall()
    for pair in pairs:
        _backfill_wu_actual_metadata(
            conn,
            pair["station_id"],
            pair["target_date"],
        )
    return result.rowcount


def backfill_polymarket_resolution_labels(
    conn: sqlite3.Connection,
    station_id: str | None = None,
) -> int:
    """Backfill ``event_detail.resolution_actual_label`` for resolved bets.

    Terminal-loss settlement paths leave the winning bracket label empty
    because the per-bet signal only proves the bet's own bracket lost. The
    dashboard then shows a blank actual column; this function names the
    winning bracket from Polymarket Gamma close-state once the event is
    finalised.

    Display-only — never writes ``actual_tmax`` (preserves WU/PROB_API-only
    invariant for the ``actuals`` table).
    """
    base_where: list[str] = [
        "event_type IN ('bet', 'dry_run')",
        "outcome IN ('WIN', 'LOSS')",
        "json_extract(event_detail, '$.resolution_source') LIKE 'polymarket_%'",
        "((json_extract(event_detail, '$.resolution_actual_label') IS NULL"
        " OR json_extract(event_detail, '$.resolution_actual_label') = '')"
        " OR (json_extract(event_detail, '$.resolution_source') = "
        "'polymarket_data_api_redeemable'"
        " AND json_extract(event_detail, '$.resolution_label_backfilled_at') IS NULL))",
        "target_date >= ?",
    ]
    lookback_floor = (
        date.today() - timedelta(days=_RESOLUTION_BACKFILL_LOOKBACK_DAYS)
    ).isoformat()
    params: list[str] = [lookback_floor]
    if station_id:
        base_where.append("station_id = ?")
        params.append(station_id)

    rows = conn.execute(
        "SELECT id, station_id, target_date FROM ledger WHERE "
        + " AND ".join(base_where),
        params,
    ).fetchall()
    if not rows:
        return 0

    groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[(row["station_id"], row["target_date"])].append(row)

    updated_at = utc_now_sql()
    updated_total = 0
    skipped_groups: list[tuple[str, str]] = []
    for (sid, target_date_str), group_rows in groups.items():
        try:
            target_day = date.fromisoformat(target_date_str)
        except (TypeError, ValueError):
            continue

        # winning_bracket_from_gamma applies the safety gate (all closed,
        # single winner above threshold, parseable bounds). Returns None if
        # any predicate fails — we leave the row alone and try again next
        # tick rather than persisting a placeholder label.
        winning = winning_bracket_from_gamma(sid, target_day)
        if winning is None:
            skipped_groups.append((sid, target_date_str))
            continue

        winning_low = winning.get("bracket_low")
        winning_high = winning.get("bracket_high")
        winning_label = winning.get("bracket_label")
        if not winning_label:
            # Bounds present but no label string — synthesize a safe one.
            winning_label = (
                f"[{winning_low},{winning_high}]"
                if winning_low is not None and winning_high is not None
                else f"≥{winning_low}" if winning_low is not None
                else f"<{winning_high}"
            )

        updates = {
            "resolution_actual_label": winning_label,
            "resolution_bracket_low": winning_low,
            "resolution_bracket_high": winning_high,
            "resolution_label_backfilled_at": updated_at,
        }
        # Re-read each row's event_detail inside the per-row UPDATE to shrink
        # the read-modify-write window across the Gamma HTTP call. Other
        # writers (fee/wu backfills) may have appended keys since the SELECT.
        for row in group_rows:
            current = conn.execute(
                "SELECT event_detail FROM ledger WHERE id = ?",
                (row["id"],),
            ).fetchone()
            if current is None:
                continue
            event_detail = _merge_event_detail(current["event_detail"], updates)
            conn.execute(
                "UPDATE ledger SET event_detail = ? WHERE id = ?",
                (event_detail, row["id"]),
            )
            updated_total += 1
        # Commit per (station, date) so a later-group failure preserves
        # earlier progress — backfill is idempotent on already-labeled rows.
        conn.commit()
        _log_backfill_health(
            conn, sid, "OK",
            f"Labeled {len(group_rows)} bet(s) for {target_date_str} -> {winning_label}",
        )
        logger.info(
            "Backfilled resolution label for %d bet(s): %s %s -> %s",
            len(group_rows), sid, target_date_str, winning_label,
        )

    if skipped_groups and updated_total == 0:
        # Surface persistent skip groups once per tick so dashboard sees the
        # backfill ran (not silently broken). Group count is bounded by the
        # 14-day lookback floor.
        first_skip_sid, first_skip_date = skipped_groups[0]
        _log_backfill_health(
            conn, station_id or first_skip_sid, "SKIP",
            f"{len(skipped_groups)} group(s) not yet finalised on Gamma "
            f"(first: {first_skip_sid} {first_skip_date})",
        )
    return updated_total


def _log_backfill_health(
    conn: sqlite3.Connection,
    station_id: str,
    status: str,
    message: str,
) -> None:
    """Best-effort pipeline_health row for the resolution-label backfill."""
    from hightempbot.db.connection import log_pipeline_health
    log_pipeline_health(conn, station_id, "resolution_backfill", status, message)


def _actual_display_value(conn: sqlite3.Connection, station_id: str, actual_c: float) -> tuple[float, str]:
    row = conn.execute(
        "SELECT unit FROM enrolled_stations WHERE icao = ?",
        (station_id,),
    ).fetchone()
    unit = (row["unit"] if row and row["unit"] else "C").upper()
    if unit == "F":
        from hightempbot.stations import celsius_to_fahrenheit
        return float(round(celsius_to_fahrenheit(actual_c))), "F"
    return float(round(actual_c)), "C"


def _actual_matches_market_bracket(
    actual_display: float,
    low: float | None,
    high: float | None,
) -> bool:
    """Thin shim: delegates to ``brackets.actual_in_bracket``.

    Single source of truth lives in ``execution.brackets`` so a future
    boundary-rule change happens in one place. Kept under the existing
    name so callers in this module read naturally.
    """
    from hightempbot.decision.brackets import actual_in_bracket
    return actual_in_bracket(actual_display, low, high)


def _fallback_actual_label(actual_display: float, unit: str) -> str:
    value = int(actual_display) if float(actual_display).is_integer() else actual_display
    return f"{value}\u00b0{unit}"


def _resolution_actual_label(
    conn: sqlite3.Connection,
    station_id: str,
    target_date: str,
    actual_c: float,
) -> tuple[str, float, str]:
    actual_display, unit = _actual_display_value(conn, station_id, actual_c)
    rows = conn.execute(
        """SELECT bracket_label, bracket_low, bracket_high
        FROM market_tokens
        WHERE station_id = ? AND market_date = ?
        ORDER BY bracket_idx""",
        (station_id, target_date),
    ).fetchall()
    for row in rows:
        low = row["bracket_low"]
        high = row["bracket_high"]
        if _actual_matches_market_bracket(actual_display, low, high):
            return row["bracket_label"] or _fallback_actual_label(actual_display, unit), actual_display, unit
    return _fallback_actual_label(actual_display, unit), actual_display, unit


def _wu_actual_matches_polymarket(
    detail: dict,
    actual_display: float,
    wu_actual_label: str,
) -> bool | None:
    """Return whether WU actual agrees with stored Polymarket resolution."""
    low = detail.get("resolution_bracket_low")
    high = detail.get("resolution_bracket_high")
    if low is not None or high is not None:
        return _actual_matches_market_bracket(actual_display, low, high)

    polymarket_label = detail.get("resolution_actual_label")
    if polymarket_label:
        return str(polymarket_label) == str(wu_actual_label)
    return None


def _backfill_wu_actual_metadata(
    conn: sqlite3.Connection,
    station_id: str,
    target_date: str,
) -> int:
    actuals_clause, actuals_params = actual_source_clause()
    actual = conn.execute(
        "SELECT tmax_celsius FROM actuals "
        f"WHERE station_id = ? AND local_date = ? AND {actuals_clause}",
        (station_id, target_date, *actuals_params),
    ).fetchone()
    if actual is None:
        return 0

    label, actual_display, unit = _resolution_actual_label(
        conn,
        station_id,
        target_date,
        float(actual["tmax_celsius"]),
    )
    rows = conn.execute(
        """SELECT id, event_detail
        FROM ledger
        WHERE station_id = ?
          AND target_date = ?
          AND event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN', 'LOSS', 'CLOSED')""",
        (station_id, target_date),
    ).fetchall()
    updated = 0
    for row in rows:
        detail = decode_event_detail(row["event_detail"])
        actual_matches = _wu_actual_matches_polymarket(detail, actual_display, label)
        updates = {
            "wu_actual_label": label,
            "wu_actual_source": "actuals",
            "wu_actual_display": actual_display,
            "wu_actual_unit": unit,
            "wu_actual_matches_polymarket": actual_matches,
        }
        next_detail = dict(detail)
        next_detail.update({k: v for k, v in updates.items() if v is not None})
        if next_detail == detail:
            continue
        conn.execute(
            "UPDATE ledger SET event_detail = ? WHERE id = ?",
            (_encode_event_detail(next_detail), row["id"]),
        )
        updated += 1
        if actual_matches is False:
            logger.warning(
                "WU actual mismatch %s %s bet_id=%s: Polymarket=%s, WU=%s",
                station_id,
                target_date,
                row["id"],
                detail.get("resolution_actual_label"),
                label,
            )
    if updated:
        conn.commit()
    return updated


def expire_stuck_pending(
    conn: sqlite3.Connection,
    max_age_days: int = 7,
    *,
    order_client=None,
) -> int:
    """Expire PENDING bets older than max_age_days.

    Bets stuck in PENDING state (actuals source failed, market delisted, etc.)
    permanently consume pending exposure cap. Expiring them releases the cap
    so the bot can continue placing new bets.

    ce-code-review P1 #14 + adversarial ADV-002 cascade: when an ``order_client``
    is supplied, ROWS WITH ``order_id`` ARE CLOB-VERIFIED BEFORE EXPIRY.
    If CLOB reports the order as MATCHED/FILLED, the row is left PENDING
    (the periodic reconciler will eventually pick it up via get_trades) and
    a critical pipeline_health row is written so the operator notices.
    Without this guard, a real fill that the reconciler missed for >7 days
    (extended CLOB outage, broken get_trades, etc.) would be silently
    zero-PnL'd and the wallet would over-report realized_capital.

    When ``order_client`` is None (legacy/test/dry-run path), the old
    unconditional-expire behavior is preserved.
    """
    excluded_outcomes_clause = (
        "outcome = 'PENDING' AND event_type IN ('bet', 'dry_run') "
        "AND station_id != 'RECOVERED' "
        "AND DATE(bet_ts) < DATE('now', ? || ' days')"
    )
    if order_client is None:
        # Legacy path: no CLOB visibility, expire blindly.
        result = conn.execute(
            f"UPDATE ledger SET outcome = 'EXPIRED', pnl = 0.0 "
            f"WHERE {excluded_outcomes_clause}",
            (f"-{max_age_days}",),
        )
        conn.commit()
        if result.rowcount > 0:
            logging.getLogger(__name__).warning(
                "Expired %d stuck PENDING bets older than %d days",
                result.rowcount, max_age_days,
            )
        return result.rowcount

    # Order-client-aware path: pre-check each row with an order_id against CLOB.
    rows = conn.execute(
        f"SELECT id, order_id, station_id "
        f"FROM ledger WHERE {excluded_outcomes_clause}",
        (f"-{max_age_days}",),
    ).fetchall()
    expired = 0
    matched_held = 0
    _MATCHED = {"MATCHED", "FILLED"}
    for row in rows:
        oid = row["order_id"]
        bet_id = int(row["id"])
        # Rows without order_id never reached CLOB — safe to expire blindly.
        if not oid:
            conn.execute(
                "UPDATE ledger SET outcome = 'EXPIRED', pnl = 0.0 "
                "WHERE id = ? AND outcome = 'PENDING'",
                (bet_id,),
            )
            expired += 1
            continue
        # Pre-check CLOB. Any failure is fail-closed -- leave the row PENDING
        # for the next pass rather than silently zero a real fill. Use
        # reconciliation's bounded get_order so a hung CLOB read can't stall
        # the expiry sweep (no leaky reach into execution.walker internals).
        try:
            from hightempbot.persistence.reconciliation import _bounded_get_order

            raw = _bounded_get_order(order_client, oid)
            if raw is None:
                logging.getLogger(__name__).warning(
                    "expire_stuck_pending: get_order returned unknown for %s -- leaving PENDING",
                    oid,
                )
                matched_held += 1
                from hightempbot.db.connection import log_pipeline_health
                log_pipeline_health(
                    conn, row["station_id"], "expire", "WARNING",
                    f"bet #{bet_id} order_id={oid}: CLOB get_order unknown; left PENDING",
                )
                continue
            status = (raw.get("status") if isinstance(raw, dict)
                      else getattr(raw, "status", "")) or ""
            status_up = str(status).upper()
        except Exception:
            logging.getLogger(__name__).warning(
                "expire_stuck_pending: get_order failed for %s -- leaving PENDING",
                oid, exc_info=True,
            )
            matched_held += 1
            from hightempbot.db.connection import log_pipeline_health
            log_pipeline_health(
                conn, row["station_id"], "expire", "WARNING",
                f"bet #{bet_id} order_id={oid}: CLOB get_order failed; left PENDING",
            )
            continue
        if status_up in _MATCHED:
            # Real fill -- DO NOT EXPIRE. Surface to operator.
            logging.getLogger(__name__).error(
                "expire_stuck_pending: row %s order_id=%s is MATCHED on CLOB but "
                "still PENDING in ledger -- LEFT PENDING; manual reconcile needed",
                bet_id, oid,
            )
            matched_held += 1
            from hightempbot.db.connection import log_pipeline_health
            log_pipeline_health(
                conn, row["station_id"], "expire", "ERROR",
                f"bet #{bet_id} order_id={oid}: CLOB status=MATCHED but ledger "
                f"PENDING; reconcile manually",
            )
            continue
        # Order is dead on CLOB (CANCELED/EXPIRED/REJECTED/etc) -- safe to expire.
        conn.execute(
            "UPDATE ledger SET outcome = 'EXPIRED', pnl = 0.0 "
            "WHERE id = ? AND outcome = 'PENDING'",
            (bet_id,),
        )
        expired += 1
    conn.commit()
    if expired > 0 or matched_held > 0:
        logging.getLogger(__name__).warning(
            "expire_stuck_pending: expired=%d held_matched_or_unverifiable=%d "
            "(age>%dd)", expired, matched_held, max_age_days,
        )
    return expired


def prune_market_tokens(conn: sqlite3.Connection, retention_days: int = 3) -> int:
    """Delete market_tokens rows for past dates. Returns count deleted.

    Token IDs are only useful for active/upcoming markets. Past-date entries
    are never queried again — safe to prune aggressively.
    """
    result = conn.execute(
        """DELETE FROM market_tokens
        WHERE DATE(market_date) < DATE('now', ? || ' days')
          AND NOT EXISTS (
              SELECT 1
              FROM ledger l
              WHERE l.station_id = market_tokens.station_id
                AND l.target_date = market_tokens.market_date
                AND l.outcome = 'PENDING'
                AND l.event_type IN ('bet', 'dry_run')
          )""",
        (f"-{retention_days}",),
    )
    conn.commit()
    return result.rowcount


def prune_pipeline_health(conn: sqlite3.Connection, retention_days: int = 7) -> int:
    """Delete pipeline_health rows older than retention_days. Returns count deleted.

    pipeline_health grows ~36K rows/day (42 stations x 6 stages x ~1 tick/min).
    Dashboard only uses 6h-48h windows, so 7 days is more than sufficient.
    """
    result = conn.execute(
        """DELETE FROM pipeline_health
        WHERE created_at < datetime('now', ? || ' days')""",
        (f"-{retention_days}",),
    )
    conn.commit()
    return result.rowcount


def prune_book_snapshots(conn: sqlite3.Connection, retention_days: int = 180) -> int:
    """Delete book_snapshots rows older than retention_days. Returns count deleted.

    Retention is intentionally LONG (default 180 days): book_snapshots is the
    durable history of the CLOB prices each tick acted on, kept for calibration
    refits and live/backtest parity. Do NOT tie this to the 3-7 day
    market_tokens / pipeline_health prune windows. snapped_at is written in
    SQLite canonical UTC form so this lexicographic comparison is correct.
    """
    result = conn.execute(
        """DELETE FROM book_snapshots
        WHERE snapped_at < datetime('now', ? || ' days')""",
        (f"-{retention_days}",),
    )
    conn.commit()
    return result.rowcount
