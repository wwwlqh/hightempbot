"""Data shaping for the v2 trading-journal dashboard.

`build_htb_data(conn, ...)` returns a dict that mirrors the schema of
`docs/design/project/ui_kits/dashboard_v2/data.js::HTB_DATA`. The React UI
fetches it via `/api/v2/data` and renders without a build step.

Wiring status (2026-05-06 first pass):
- LIVE / wired to real DB: capital, totalPnl, realizedPnl, fees, resolved/wins/losses counts,
  open + resolved positions list, ddPct, equityCurve from ledger, performance by
  station, strategies (NO/YMID/TAIL/YHIGH) breakdown, ymidExits (TP fire stats),
  funnel, stations list (eligibility), uptime, lastScanAgo, mode (LIVE/DRY-RUN),
  calendar (per target_date P&L), top3 winners/losers, station sparklines.
- PLACEHOLDER (computed cheaply with simple defaults — flagged in JSON with
  `_synthetic_<key>: true`): ratios.sharpe / sortino / calmar,
  calibrationByStation per-bucket observed/predicted from the LUT bucket grid
  (computed from lut_bucket_stats — real but coarse), ensembleByStation
  per-model 30d accuracy (placeholder until per-model ledger is added).

Update incrementally: add real wirings to replace placeholders one at a time.
The React UI tolerates missing/empty arrays — pages render with the real keys
and fall back to "No data" text where the placeholder is empty.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import statistics as stats
from dataclasses import dataclass
from datetime import date as _date, datetime as _dt, timedelta, timezone

import numpy as np

from hightempbot.calibration.emos import EMOSParams, predict_emos
from hightempbot.execution.capital import (
    live_gate_capital_view,
    return_transfer_notional,
    return_transfer_status_filter,
)
from hightempbot.execution.strategy_constants import (
    LUT_MIN_N_FOR_SHRINKAGE,
    MAX_DD,
    MIN_BET_USD,
    POLY_FEE_THETA,
    SCAN_INTERVAL_MINUTES,
    STRATEGY_CONFIGS,
)
from hightempbot.persistence.ledger import decode_event_detail
from hightempbot.persistence.wallet_reconciliation import latest_wallet_snapshot

from hightempbot.stations import supports_live_resolution_source

logger = logging.getLogger(__name__)

# Halt at MAX_DD — 2026-05-20 operator switch from the prior "halve at
# MAX_DD" rule. The React component expects ``ddHaltThreshold`` in PERCENT
# (currently 40). ``reducedSizeThreshold`` is still emitted at 100 (the no-op value
# equivalent to "no reduced band") so older bundled UI builds don't fall over
# on a missing key; the field is now unused and will be removed once the
# bundled JS no longer references it. A lower value would lie to the
# operator: dashboard says "half-sized" while the bot is in fact halted.
DEFAULT_HALT_THRESHOLD_PCT = int(round(MAX_DD * 100))
DEFAULT_REDUCED_AT_PCT_OF_HALT = 100  # legacy field; halt-on-DD has no reduced band


# ICAO -> country/flag helpers — single source of truth in _geo.py so the
# prefix table doesn't drift between v2_data.py and app.py (ce-review
# maintainability + kieran-python finding #18).
from hightempbot.dashboard._geo import _icao_to_flag  # noqa: F401


def _parse_iso_utc(ts: str | None) -> _dt | None:
    if not ts:
        return None
    try:
        d = _dt.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _to_md(ts: str | None) -> str:
    """UTC ISO → 'MM-DD HH:MM' (UTC) for display."""
    d = _parse_iso_utc(ts)
    return d.strftime("%m-%d %H:%M") if d else ""


def _finite_float(value: object) -> float | None:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _configured_poly_funder() -> str:
    try:
        from hightempbot.runtime_config import get_config

        return str(getattr(get_config(), "poly_funder", "") or "").strip()
    except Exception:
        return ""


def _latest_wallet_snapshot_for_dashboard(conn: sqlite3.Connection) -> dict[str, object] | None:
    try:
        wallet_address = _configured_poly_funder()
        if wallet_address:
            return latest_wallet_snapshot(
                conn,
                wallet_address=wallet_address,
                freshness_ttl_s=300,
            )
        return latest_wallet_snapshot(conn, freshness_ttl_s=300)
    except Exception:
        logger.warning("latest wallet snapshot lookup failed for dashboard", exc_info=True)
        return None


def _api_position_by_ledger_id(snapshot: dict[str, object] | None) -> dict[int, dict[str, object]]:
    if not snapshot:
        return {}
    out: dict[int, dict[str, object]] = {}
    for record in snapshot.get("records") or []:  # type: ignore[union-attr]
        if not isinstance(record, dict) or record.get("record_type") != "position":
            continue
        raw = record.get("record") if isinstance(record.get("record"), dict) else {}
        match = raw.get("ledgerMatch") if isinstance(raw.get("ledgerMatch"), dict) else {}
        ledger_ids = match.get("ledgerIds") if isinstance(match.get("ledgerIds"), list) else []
        source_id = str(record.get("source_id") or raw.get("asset") or "")
        api_row = {
            "sourceId": source_id,
            "matchStatus": record.get("match_status") or match.get("status") or "",
            "trusted": bool(match.get("trusted")),
            "currentValueUsd": _finite_float(match.get("apiCurrentValueUsd")),
            "initialValueUsd": _finite_float(match.get("apiInitialValueUsd")),
            "cashPnlUsd": _finite_float(match.get("apiCashPnlUsd")),
            "curPrice": _finite_float(match.get("apiCurPrice")),
            "avgPrice": _finite_float(match.get("apiAvgPrice")),
            "redeemable": bool(raw.get("redeemable")),
            "reminder": str(match.get("reminder") or ""),
            "maxDriftPct": _finite_float(match.get("maxDriftPct")),
        }
        for ledger_id in ledger_ids:
            try:
                out[int(ledger_id)] = api_row
            except (TypeError, ValueError):
                continue
    return out


def _attach_api_positions(
    rows: list[dict[str, object]],
    snapshot: dict[str, object] | None,
) -> None:
    by_ledger_id = _api_position_by_ledger_id(snapshot)
    for row in rows:
        try:
            ledger_id = int(row.get("id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        api_position = by_ledger_id.get(ledger_id)
        if api_position is not None:
            row["api_position"] = api_position


def _event_detail(row: dict[str, object]) -> dict[str, object]:
    return decode_event_detail(row.get("event_detail"))


def _row_strategy(row: dict[str, object]) -> str:
    detail = _event_detail(row)
    strategy = detail.get("strategy")
    return str(strategy or "NO")


def _row_detail_value(row: dict[str, object], key: str) -> object:
    value = row.get(key)
    if value is not None:
        return value
    return _event_detail(row).get(key)


def _group_value(value: object) -> str:
    v = _finite_float(value)
    if v is None:
        return ""
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _trade_group_key(row: dict[str, object]) -> str:
    """Slot-like key for dashboard display grouping."""
    return "|".join([
        str(row.get("event_type") or ""),
        str(row.get("station_id") or ""),
        str(row.get("target_date") or ""),
        str(row.get("side") or ""),
        _row_strategy(row),
        _group_value(row.get("threshold")),
        _group_value(_row_detail_value(row, "bracket_low")),
        _group_value(_row_detail_value(row, "bracket_high")),
    ])


def _group_rows_by_trade(rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        groups.setdefault(_trade_group_key(row), []).append(row)
    return groups


def _row_dicts(rows) -> list[dict[str, object]]:
    return [dict(row) for row in rows]


def _hour_set_display(hours) -> str | None:
    if not hours:
        return None
    vals = sorted(int(h) for h in hours)
    if not vals:
        return None
    if len(vals) == 1:
        return str(vals[0])
    runs: list[tuple[int, int]] = []
    start = prev = vals[0]
    for h in vals[1:]:
        if h == prev + 1:
            prev = h
            continue
        runs.append((start, prev))
        start = prev = h
    runs.append((start, prev))
    return ",".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)


def _serialize_strategy_configs() -> dict[str, dict[str, object]]:
    """Serialize STRATEGY_CONFIGS-backed data for the Strategy dashboard tab.

    Field names mirror the live config attribute names so the wiki's
    [[Optimum Strategy]] cross-references stay valid.
    """
    payload: dict[str, dict[str, object]] = {}
    disabled: list[str] = []
    for name, cfg in STRATEGY_CONFIGS.items():
        if not getattr(cfg, "enabled", True):
            disabled.append(name)
            continue
        payload[name] = {
            "side": cfg.side,
            "signal_name": cfg.signal_name,
            "capital_frac": cfg.capital_frac,
            "fp_min": cfg.fp_min,
            "fp_max": cfg.fp_max,
            "min_edge": cfg.min_edge,
            "max_edge": cfg.max_edge,
            "alpha_ratio": cfg.alpha_ratio,
            "vote_signals": list(cfg.vote_signals) if cfg.vote_signals else None,
            "vote_n_required": cfg.vote_n_required,
            "vote_min_n": cfg.vote_min_n,
            "consensus_skip_threshold": cfg.consensus_skip_threshold,
            "delayed_entry_fp_max": cfg.delayed_entry_fp_max,
            "entry_hours": _hour_set_display(cfg.entry_hour_set),
            "min_bvol": cfg.min_bvol,
            "max_walk_price": cfg.max_walk_price,
            "execution_min_edge": cfg.execution_min_edge,
            "max_vwap_slip_from_anchor": cfg.max_vwap_slip_from_anchor,
            "tp": cfg.tp,
            "sl": cfg.sl,
            "signal_name_for_ceiling": cfg.signal_name_for_ceiling,
            "fp_min_for_ceiling": cfg.fp_min_for_ceiling,
            "max_edge_for_ceiling": cfg.max_edge_for_ceiling,
        }
    payload["__lut_min_n"] = {"value": int(LUT_MIN_N_FOR_SHRINKAGE)}
    payload["__execution_policy"] = {
        "scan_interval_minutes": int(SCAN_INTERVAL_MINUTES),
        "new_slot_tick": 0,
        "topups_every_tick": True,
        "min_bet_usd": float(MIN_BET_USD),
        "poly_fee_theta": float(POLY_FEE_THETA),
    }
    # Operator 2026-08-09: the Strategy tab shows only the live sleeve(s)
    # (FLIP under FLIP_MODE=1; NO otherwise). Disabled sleeves stay in
    # STRATEGY_CONFIGS for the TP/SL monitor and parity tests but are not
    # rendered — the key keeps its shape for the JSX/test contract.
    del disabled
    payload["__disabled"] = {"value": []}
    return payload


def _target_size_for_group(rows: list[dict[str, object]], stake_basis_capital: float | None) -> float | None:
    if stake_basis_capital is None or stake_basis_capital <= 0 or not rows:
        return None
    cfg = STRATEGY_CONFIGS.get(_row_strategy(rows[0]))
    if cfg is None:
        return None
    actual_size = sum((_finite_float(row.get("bet_size")) or 0.0) for row in rows)
    # Target is an operator display value. Round upward to cents so tiny cash
    # dust/fees do not turn a 5% BR100 target into a confusing "$4.99 target".
    target = math.ceil(float(stake_basis_capital) * float(cfg.capital_frac) * 100.0) / 100.0
    return max(actual_size, target)


def _actual_display_for_v2(row: dict[str, object], *, resolved: bool) -> str | None:
    # Prefer the Polymarket bracket-bin label (e.g. "68-69°F", "≥27°F") over
    # the raw observed temperature so the trade journal matches how the market
    # itself reported the outcome. Fall back to "{actual_display}°{unit}" only
    # when no bracket label is available (e.g. resolution driven by our own
    # WU/NOAA actuals, which clears resolution_actual_label upstream).
    label = row.get("actual_label")
    if label:
        return str(label)
    if not resolved:
        return None
    ad = row.get("actual_display")
    if ad is None:
        return ""
    au = row.get("actual_unit") or "C"
    return f"{ad}°{au}"


def _fill_levels_for_v2(row: dict[str, object]) -> list[dict[str, float]]:
    """Return per-price fill depth captured at execution time.

    `volume_cap` is intentionally not used here: that column is the broader
    market/strategy volume gate, not the amount available or filled at the
    displayed price. When older reconciliation paths only recorded the fill
    VWAP/size, synthesize a one-level VWAP ladder so known fills do not render
    as missing depth.
    """
    levels = _event_detail(row).get("fill_levels")
    out: list[dict[str, float]] = []
    if isinstance(levels, list):
        for level in levels:
            if not isinstance(level, dict):
                continue
            price = _finite_float(level.get("price"))
            usd = _finite_float(level.get("usd"))
            shares = _finite_float(level.get("shares"))
            if price is None:
                continue
            if usd is None and shares is not None:
                usd = price * shares
            if shares is None and usd is not None and price > 0:
                shares = usd / price
            if usd is None or shares is None:
                continue
            out.append({
                "price": round(price, 4),
                "usd": round(usd, 2),
                "shares": round(shares, 4),
            })
    if out:
        return out

    price = _finite_float(row.get("fill_price"))
    shares = _finite_float(row.get("fill_size"))
    usd = _finite_float(row.get("bet_size"))
    if price is None or price <= 0:
        return []
    if shares is None and usd is not None:
        shares = usd / price
    if usd is None and shares is not None:
        usd = price * shares
    if usd is None or shares is None:
        return []
    if usd > 0 and shares > 0:
        out.append({
            "price": round(price, 4),
            "usd": round(usd, 2),
            "shares": round(shares, 4),
        })
    return out


def _fill_detail_for_v2(row: dict[str, object], *, resolved: bool) -> dict[str, object]:
    fill_price = _finite_float(row.get("fill_price"))
    edge = _finite_float(row.get("edge"))
    realized_edge = _finite_float(row.get("realized_edge"))
    # Dry-run rows historically did not persist realized_edge (the ledger
    # UPDATE path skipped that column for event_type='dry_run'). The pre-trade
    # `edge` is the best available approximation — dry-run has no slippage,
    # so realized_edge ≈ edge. Without this fallback every dry-run row shows
    # an empty Realized column on the Trade Journal.
    if realized_edge is None and row.get("event_type") == "dry_run" and edge is not None:
        realized_edge = edge
    bet_size = _finite_float(row.get("bet_size")) or 0.0
    fill_size = _finite_float(row.get("fill_size"))
    price_levels = _fill_levels_for_v2(row)
    level_volume = sum(level["usd"] for level in price_levels) if price_levels else None
    limit_price = _finite_float(row.get("limit_price"))
    pnl = _finite_float(row.get("pnl")) or 0.0
    return {
        "ledgerId": row.get("id"),
        "ts": _to_md(row.get("bet_ts")),
        "rawTs": row.get("bet_ts") or "",
        "fillTs": _to_md(row.get("fill_ts")),
        "rawFillTs": row.get("fill_ts") or "",
        "fillPrice": round(fill_price, 4) if fill_price is not None else None,
        "fill": round(fill_price * 100.0, 1) if fill_price is not None else None,
        "edge": round(edge * 100.0, 1) if edge is not None else None,
        "realizedEdge": round(realized_edge * 100.0, 1) if realized_edge is not None else None,
        "size": round(bet_size, 2),
        "fillSize": round(fill_size, 4) if fill_size is not None else None,
        "levelVolume": round(level_volume, 2) if level_volume is not None else None,
        "priceLevels": price_levels,
        "limit": round(limit_price, 4) if limit_price is not None else None,
        "orderId": row.get("order_id") or "",
        "outcome": row.get("outcome") or "",
        "pnl": round(pnl, 2) if resolved else None,
    }


def _weighted_average(values: list[tuple[float, float]]) -> float | None:
    total_weight = sum(weight for _, weight in values if weight > 0)
    if total_weight <= 0:
        return None
    return sum(value * weight for value, weight in values if weight > 0) / total_weight


def _group_positions_for_v2(
    positions: list[dict],
    *,
    resolved: bool,
    stake_basis_capital: float | None = None,
    limit: int | None = None,
) -> list[dict[str, object]]:
    """Group row-exact ledger fills into slot-level dashboard rows."""
    groups = _group_rows_by_trade(positions)

    out: list[dict[str, object]] = []
    for key, rows in groups.items():
        rows = sorted(rows, key=lambda r: str(r.get("bet_ts") or ""))
        first = rows[0]
        latest = rows[-1]
        icao = first.get("station_id") or ""
        fills = [_fill_detail_for_v2(row, resolved=resolved) for row in rows]
        size = sum((_finite_float(row.get("bet_size")) or 0.0) for row in rows)
        fill_values = [
            (fill_price, weight)
            for row in rows
            if (fill_price := _finite_float(row.get("fill_price"))) is not None
            and (weight := (_finite_float(row.get("bet_size")) or 0.0)) > 0
        ]
        edge_values = []
        realized_edge_values = []
        for row in rows:
            weight = _finite_float(row.get("bet_size")) or 0.0
            if weight <= 0:
                continue
            edge_value = _finite_float(row.get("edge"))
            if edge_value is not None:
                edge_values.append((edge_value, weight))
            realized_edge = _finite_float(row.get("realized_edge"))
            if realized_edge is None and row.get("event_type") == "dry_run" and edge_value is not None:
                realized_edge = edge_value
            if realized_edge is not None:
                realized_edge_values.append((realized_edge, weight))
        avg_fill = _weighted_average(fill_values)
        avg_edge = _weighted_average(edge_values)
        avg_realized_edge = _weighted_average(realized_edge_values)
        target_size = _target_size_for_group(rows, stake_basis_capital)
        outcomes = {str(row.get("outcome") or "") for row in rows if row.get("outcome")}
        outcome = next(iter(outcomes)) if len(outcomes) == 1 else "MIXED"
        row_out = {
            "rowKey": key,
            "ts": _to_md(first.get("bet_ts")),
            "latestTs": _to_md(latest.get("bet_ts")),
            "rawLatestTs": latest.get("bet_ts") or "",
            "id": icao,
            "flag": _icao_to_flag(str(icao)),
            "city": first.get("city") or "",
            "target": first.get("target_date") or "",
            "bracket": first.get("bracket_label") or "",
            "side": first.get("side") or "",
            "strategy": _row_strategy(first),
            "fill": round(avg_fill * 100.0, 1) if avg_fill is not None else None,
            "edge": round(avg_edge * 100.0, 1) if avg_edge is not None else None,
            "realizedEdge": round(avg_realized_edge * 100.0, 1)
            if avg_realized_edge is not None
            else None,
            "size": round(size, 2),
            "targetSize": round(target_size, 2) if target_size is not None else None,
            "fillCount": len(fills),
            "fills": fills,
            "actual": _actual_display_for_v2(latest, resolved=resolved),
        }
        if not resolved:
            api_positions: dict[str, dict[str, object]] = {}
            for row in rows:
                api_position = row.get("api_position")
                if not isinstance(api_position, dict):
                    continue
                source_id = str(api_position.get("sourceId") or "")
                if source_id:
                    api_positions[source_id] = api_position
            if api_positions:
                api_current_value = sum(
                    _finite_float(pos.get("currentValueUsd")) or 0.0
                    for pos in api_positions.values()
                )
                api_initial_value = sum(
                    _finite_float(pos.get("initialValueUsd")) or 0.0
                    for pos in api_positions.values()
                )
                api_cash_pnl = sum(
                    _finite_float(pos.get("cashPnlUsd")) or 0.0
                    for pos in api_positions.values()
                )
                trusted = all(bool(pos.get("trusted")) for pos in api_positions.values())
                reminders = [
                    str(pos.get("reminder") or "")
                    for pos in api_positions.values()
                    if str(pos.get("reminder") or "")
                ]
                row_out.update({
                    "size": round(api_initial_value, 2) if api_initial_value > 0 else row_out["size"],
                    "apiCurrentValue": round(api_current_value, 2),
                    "apiCashPnl": round(api_cash_pnl, 2),
                    "apiCurPrice": next(
                        (
                            round(cur_price, 4)
                            for pos in api_positions.values()
                            if (cur_price := (_finite_float(pos.get("curPrice")) or 0.0)) > 0
                        ),
                        None,
                    ),
                    "apiConfirmed": trusted,
                    "apiStatus": "API_CONFIRMED" if trusted else "API_MISMATCH",
                    "apiReminder": reminders[0] if reminders else "",
                    "redeemable": any(bool(pos.get("redeemable")) for pos in api_positions.values()),
                })
            else:
                row_out.update({
                    "apiCurrentValue": None,
                    "apiCashPnl": None,
                    "apiCurPrice": None,
                    "apiConfirmed": False,
                    "apiStatus": "API_PENDING",
                    "apiReminder": "Waiting for Polymarket Data API confirmation",
                    "redeemable": False,
                })
        if resolved:
            pnl = sum((_finite_float(row.get("pnl")) or 0.0) for row in rows)
            row_out["outcome"] = outcome
            row_out["pnl"] = round(pnl, 2)
        out.append(row_out)

    out.sort(key=lambda row: str(row.get("rawLatestTs") or ""), reverse=True)
    return out[:limit] if limit is not None else out


def _equity_curve_from_ledger(
    conn: sqlite3.Connection,
    cutoff_utc: str,
    initial_bankroll: float,
    days: int = 30,
) -> list[dict[str, object]]:
    """Cumulative P&L from resolved bets, bucketed by UTC date. Starts at 0.

    Returns list of {x: 'MM-DD', y: cumulative P&L} for the last `days` UTC
    days that have resolved bets, prefixed with one zero-point on the day
    before the first resolution.
    """
    rows = conn.execute(
        """
        SELECT substr(bet_ts, 1, 10) AS d, COALESCE(SUM(pnl), 0) AS pnl
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN', 'LOSS', 'CLOSED', 'PUSH')
          AND bet_ts >= ?
        GROUP BY d
        ORDER BY d ASC
        """,
        (cutoff_utc,),
    ).fetchall()
    if not rows:
        return []
    cum = 0.0
    out: list[dict[str, object]] = []
    # Prefix a zero-point so the chart starts from 0.
    first_day = _date.fromisoformat(rows[0]["d"])
    prefix_day = first_day - timedelta(days=1)
    out.append({"x": prefix_day.strftime("%m-%d"), "y": 0.0})
    for r in rows:
        cum += float(r["pnl"] or 0.0)
        out.append({"x": _date.fromisoformat(r["d"]).strftime("%m-%d"), "y": round(cum, 2)})
    if len(out) > days + 1:
        out = out[-(days + 1):]
    return out


def _transfer_outflow_by_day(
    conn: sqlite3.Connection,
    cutoff_utc: str,
) -> dict[str, float]:
    status_where, status_params = return_transfer_status_filter()
    rows = conn.execute(
        f"""
        SELECT substr(created_at, 1, 10) AS d,
               COALESCE(SUM(amount_usd), 0) AS amount
        FROM transfer_requests
        WHERE ({status_where})
          AND created_at >= ?
        GROUP BY d
        ORDER BY d ASC
        """,
        (*status_params, cutoff_utc),
    ).fetchall()
    return {str(r["d"]): float(r["amount"] or 0.0) for r in rows}


def _withdrawal_events(
    conn: sqlite3.Connection,
    cutoff_utc: str,
    days: int = 30,
) -> list[dict[str, object]]:
    status_where, status_params = return_transfer_status_filter()
    rows = conn.execute(
        f"""
        SELECT created_at, amount_usd, status
        FROM transfer_requests
        WHERE ({status_where})
          AND created_at >= ?
        ORDER BY created_at ASC, id ASC
        """,
        (*status_params, cutoff_utc),
    ).fetchall()
    out = []
    for r in rows:
        day = str(r["created_at"] or "")[:10]
        try:
            label = _date.fromisoformat(day).strftime("%m-%d")
        except ValueError:
            label = day
        out.append({
            "x": label,
            "ts": r["created_at"],
            "amount": round(float(r["amount_usd"] or 0.0), 2),
            "status": r["status"],
        })
    return out[-days:]


def _account_equity_curve(
    conn: sqlite3.Connection,
    cutoff_utc: str,
    initial_bankroll: float,
    days: int = 30,
) -> list[dict[str, object]]:
    """Trading P&L minus operator withdrawals, bucketed by UTC date.

    The trading curve remains the drawdown source. This account curve makes
    withdrawals visually obvious without turning them into strategy losses.
    """
    del initial_bankroll  # curve is plotted as cumulative P&L delta, not bankroll.
    pnl_rows = conn.execute(
        """
        SELECT substr(bet_ts, 1, 10) AS d, COALESCE(SUM(pnl), 0) AS pnl
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN', 'LOSS', 'CLOSED', 'PUSH')
          AND bet_ts >= ?
        GROUP BY d
        ORDER BY d ASC
        """,
        (cutoff_utc,),
    ).fetchall()
    pnl_by_day = {str(r["d"]): float(r["pnl"] or 0.0) for r in pnl_rows}
    withdrawal_by_day = _transfer_outflow_by_day(conn, cutoff_utc)
    dates = sorted(set(pnl_by_day) | set(withdrawal_by_day))
    if not dates:
        return []

    out: list[dict[str, object]] = []
    first_day = _date.fromisoformat(dates[0])
    out.append({
        "x": (first_day - timedelta(days=1)).strftime("%m-%d"),
        "y": 0.0,
        "tradingY": 0.0,
        "withdrawn": 0.0,
        "withdrawal": 0.0,
    })

    cum_pnl = 0.0
    cum_withdrawn = 0.0
    for day in dates:
        cum_pnl += pnl_by_day.get(day, 0.0)
        withdrawal = withdrawal_by_day.get(day, 0.0)
        cum_withdrawn += withdrawal
        out.append({
            "x": _date.fromisoformat(day).strftime("%m-%d"),
            "y": round(cum_pnl - cum_withdrawn, 2),
            "tradingY": round(cum_pnl, 2),
            "withdrawn": round(cum_withdrawn, 2),
            "withdrawal": round(withdrawal, 2),
        })
    if len(out) > days + 1:
        out = out[-(days + 1):]
    return out


def _weekly_pnl(conn: sqlite3.Connection, cutoff_utc: str, n_weeks: int = 8) -> list[dict[str, object]]:
    """Last `n_weeks` ISO-week bucketed P&L from resolved bets.

    One GROUP BY rather than one query per week: every dashboard render hits
    this path, and the previous loop issued ``n_weeks`` separate aggregates.
    """
    today = _dt.now(timezone.utc).date()
    earliest_week_end = today - timedelta(days=today.weekday()) + timedelta(days=6) - timedelta(weeks=n_weeks - 1)
    earliest_week_start = earliest_week_end - timedelta(days=6)

    rows = conn.execute(
        """
        SELECT strftime('%Y-%W', substr(bet_ts, 1, 10)) AS yw,
               COALESCE(SUM(pnl), 0) AS pnl
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN', 'LOSS', 'CLOSED')
          AND substr(bet_ts, 1, 10) >= ?
          AND bet_ts >= ?
        GROUP BY yw
        """,
        (earliest_week_start.isoformat(), cutoff_utc),
    ).fetchall()

    pnl_by_yw: dict[str, float] = {r["yw"]: float(r["pnl"] or 0.0) for r in rows}

    out: list[dict[str, object]] = []
    for i in range(n_weeks - 1, -1, -1):
        week_end = today - timedelta(days=today.weekday()) + timedelta(days=6) - timedelta(weeks=i)
        # `%Y-%W` uses Sunday-as-week-start in strftime; compute the bucket
        # using one of that week's days (the Saturday end-of-week).
        yw = week_end.strftime("%Y-%W")
        label = "This" if i == 0 else f"W-{i}"
        out.append({"week": label, "pnl": round(pnl_by_yw.get(yw, 0.0), 2)})
    return out


_RESOLVED_OUTCOMES = {"WIN", "LOSS", "PUSH", "CLOSED"}
_PNL_OUTCOMES = {"WIN", "LOSS", "CLOSED"}


def _terminal_trade_groups(
    rows: list[dict[str, object]],
    *,
    outcomes: set[str],
) -> list[dict[str, object]]:
    """Collapse fill rows into terminal trade groups for count-like KPIs."""
    out: list[dict[str, object]] = []
    for group_rows in _group_rows_by_trade(rows).values():
        terminal_rows = [
            row for row in group_rows
            if str(row.get("outcome") or "") in outcomes
        ]
        if not terminal_rows:
            continue
        group_outcomes = {
            str(row.get("outcome") or "")
            for row in terminal_rows
            if str(row.get("outcome") or "") in outcomes
        }
        if len(group_outcomes) == 1:
            out.append({
                "rows": terminal_rows,
                "outcome": next(iter(group_outcomes)),
                "pnl": sum((_finite_float(row.get("pnl")) or 0.0) for row in terminal_rows),
                "latest_ts": max(str(row.get("bet_ts") or "") for row in terminal_rows),
                "target_date": terminal_rows[0].get("target_date"),
            })
        else:
            # Mixed terminal states inside one slot are rare and usually mean
            # manual repair. Count each fill so the dashboard does not hide it.
            for row in terminal_rows:
                out.append({
                    "rows": [row],
                    "outcome": str(row.get("outcome") or ""),
                    "pnl": _finite_float(row.get("pnl")) or 0.0,
                    "latest_ts": str(row.get("bet_ts") or ""),
                    "target_date": row.get("target_date"),
                })
    return out


def _trade_is_win(outcome: str, pnl: float) -> bool:
    return outcome == "WIN" or (outcome == "CLOSED" and pnl > 0)


def _trade_is_loss(outcome: str, pnl: float) -> bool:
    return outcome == "LOSS" or (outcome == "CLOSED" and pnl <= 0)


@dataclass
class _ResolvedTradeStats:
    resolved: int = 0
    wins: int = 0
    losses: int = 0
    gross_win: float = 0.0
    gross_loss: float = 0.0


def _resolved_trade_stats(rows: list[dict[str, object]]) -> _ResolvedTradeStats:
    stats_out = _ResolvedTradeStats()
    for trade in _terminal_trade_groups(rows, outcomes=_RESOLVED_OUTCOMES):
        outcome = str(trade["outcome"])
        pnl = float(trade["pnl"] or 0.0)
        stats_out.resolved += 1
        if _trade_is_win(outcome, pnl):
            stats_out.wins += 1
            stats_out.gross_win += pnl
        elif _trade_is_loss(outcome, pnl):
            stats_out.losses += 1
            stats_out.gross_loss += abs(pnl)
    return stats_out


def _pnl_distribution(conn: sqlite3.Connection, cutoff_utc: str) -> list[dict[str, object]]:
    """Histogram of per-trade P&L bucketed at $5 width, [-25, +25] range."""
    rows = _row_dicts(conn.execute(
        """
        SELECT *
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN', 'LOSS', 'CLOSED')
          AND bet_ts >= ?
        """,
        (cutoff_utc,),
    ).fetchall())
    bins = [-15, -10, -5, 0, 5, 10, 15, 20, 25]
    counts = {b: 0 for b in bins}
    for trade in _terminal_trade_groups(rows, outcomes=_PNL_OUTCOMES):
        pnl = float(trade["pnl"] or 0.0)
        # bin lower edge: floor((pnl + 17.5) / 5) * 5 - 17.5 — simpler:
        b = max(min(round(pnl / 5.0) * 5, 25), -15)
        counts[b] = counts.get(b, 0) + 1
    return [{"bin": ("+" if b > 0 else "") + str(b), "n": counts[b]} for b in bins]


def _streaks(conn: sqlite3.Connection, cutoff_utc: str, last_n: int = 20) -> dict[str, object]:
    """Current win/loss streak + longest streak from the last N resolved bets."""
    rows = _row_dicts(conn.execute(
        """
        SELECT *
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN', 'LOSS', 'CLOSED')
          AND bet_ts >= ?
        ORDER BY bet_ts DESC
        """,
        (cutoff_utc,),
    ).fetchall())
    trades = sorted(
        _terminal_trade_groups(rows, outcomes=_PNL_OUTCOMES),
        key=lambda trade: str(trade.get("latest_ts") or ""),
        reverse=True,
    )
    if not trades:
        return {"current": "—", "currentTone": "neu", "longestW": 0, "longestL": 0, "last20": []}
    seq = [
        "W" if _trade_is_win(str(trade["outcome"]), float(trade["pnl"] or 0.0)) else "L"
        for trade in trades
    ]
    current_kind = seq[0]
    current_run = 0
    for s in seq:
        if s == current_kind:
            current_run += 1
        else:
            break
    longest_w = longest_l = run = 0
    last_kind = None
    for s in reversed(seq):
        if s == last_kind:
            run += 1
        else:
            run = 1
        last_kind = s
        if s == "W":
            longest_w = max(longest_w, run)
        else:
            longest_l = max(longest_l, run)
    return {
        "current": f"{current_kind}{current_run}",
        "currentTone": "pos" if current_kind == "W" else "neg",
        "longestW": longest_w,
        "longestL": longest_l,
        "last20": list(reversed(seq[:last_n])),
    }


def _ratios(
    equity_curve: list[dict[str, object]],
    total_pnl: float,
    initial_bankroll: float,
    dd_pct: float,
    wins: int,
    losses: int,
    gross_win: float,
    gross_loss: float,
) -> dict[str, object]:
    """Compute risk ratios from the daily equity curve.

    Sharpe and Sortino use daily P&L; rough annualization is x sqrt(252).
    """
    daily = []
    if equity_curve:
        prev = equity_curve[0]["y"]
        for p in equity_curve[1:]:
            daily.append(float(p["y"]) - float(prev))
            prev = p["y"]
    sharpe = sortino = 0.0
    if daily and len(daily) > 1:
        mean = sum(daily) / len(daily)
        try:
            sd = stats.pstdev(daily)
            if sd > 0:
                sharpe = (mean / sd) * math.sqrt(252)
            downside = [x for x in daily if x < 0]
            if downside:
                ds_sd = stats.pstdev(downside)
                if ds_sd > 0:
                    sortino = (mean / ds_sd) * math.sqrt(252)
        except stats.StatisticsError:
            pass
    calmar = (total_pnl / initial_bankroll) / (dd_pct / 100) if dd_pct > 0 else 0.0
    n_resolved = wins + losses
    expectancy = (total_pnl / n_resolved) if n_resolved > 0 else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else 0.0
    return {
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "expectancy": round(expectancy, 2),
        "profitFactor": round(profit_factor, 2),
        "kellyFrac": 0.0,  # Legacy UI key; Kelly sizing is retired.
    }


@dataclass
class _WinLossAccumulator:
    """W/L/CLOSED tally used by both _strategies_breakdown and the per-station
    perf rollup (ce-code-review P2 #61 — was duplicated as two ~60-line
    dict-of-mixed-types blocks). CLOSED + positive pnl = TP-exit win;
    CLOSED + non-positive pnl = held-to-loss. MIXED (rare: trade groups with
    multiple terminal outcomes) is resolved per-row instead of group-summed.
    """

    n_resolved: int = 0
    wins: int = 0
    losses: int = 0
    gross_win: float = 0.0
    gross_loss: float = 0.0

    def add_trade(self, group_rows: list[dict[str, object]], pnl: float) -> None:
        outcomes = {str(row.get("outcome") or "") for row in group_rows if row.get("outcome")}
        outcome = next(iter(outcomes)) if len(outcomes) == 1 else "MIXED"
        if outcome == "MIXED":
            for row in group_rows:
                row_outcome = str(row.get("outcome") or "")
                row_pnl = _finite_float(row.get("pnl")) or 0.0
                if row_outcome in {"WIN", "LOSS", "CLOSED"}:
                    self.n_resolved += 1
                if row_outcome == "WIN" or (row_outcome == "CLOSED" and row_pnl > 0):
                    self.wins += 1
                    self.gross_win += row_pnl
                elif row_outcome == "LOSS" or (row_outcome == "CLOSED" and row_pnl <= 0):
                    self.losses += 1
                    self.gross_loss += abs(row_pnl)
        elif outcome in {"WIN", "LOSS", "CLOSED"}:
            self.n_resolved += 1
            if outcome == "WIN" or (outcome == "CLOSED" and pnl > 0):
                self.wins += 1
                self.gross_win += pnl
            elif outcome == "LOSS" or (outcome == "CLOSED" and pnl <= 0):
                self.losses += 1
                self.gross_loss += abs(pnl)


def _strategies_breakdown(conn: sqlite3.Connection, cutoff_utc: str) -> list[dict[str, object]]:
    """NO / YMID / TAIL / YHIGH stats from resolved event_detail.strategy rows.

    Single GROUP BY query — gross_win and gross_loss are folded into the
    outer aggregation as conditional sums, eliminating the previous N+1
    pattern that issued 12 extra ledger scans per dashboard request
    (ce-review performance perf-001 + maintainability + kieran-python #25).
    """
    # WIN/LOSS semantics: a CLOSED outcome with positive PnL is a TP exit
    # (TAIL strategy takes profit and exits before resolution). It is a real
    # win, so it counts toward `wins`/`gross_win`. Held-to-resolution losses
    # and CLOSED-with-non-positive PnL go to the loss bucket.
    rows = _row_dicts(conn.execute(
        """
        SELECT *
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN', 'LOSS', 'CLOSED')
          AND bet_ts >= ?
        """,
        (cutoff_utc,),
    ).fetchall())
    # shared W/L tally via _WinLossAccumulator. Extra
    # per-strategy fields (n_bets, gross_stake, edges, stakes, total_pnl) stay
    # inline here because the per-station rollup below doesn't need them.
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for group_rows in _group_rows_by_trade(rows).values():
        first = group_rows[0]
        key = (_row_strategy(first), str(first.get("side") or ""))
        entry = grouped.setdefault(key, {
            "strategy": key[0],
            "side": key[1],
            "n_bets": 0,
            "total_pnl": 0.0,
            "gross_stake": 0.0,
            "edges": [],
            "stakes": [],
            "wl": _WinLossAccumulator(),
        })
        entry["n_bets"] = int(entry["n_bets"]) + 1  # type: ignore[arg-type]
        stake = sum((_finite_float(row.get("bet_size")) or 0.0) for row in group_rows)
        pnl = sum((_finite_float(row.get("pnl")) or 0.0) for row in group_rows)
        edge = _weighted_average([
            (edge_value, weight)
            for row in group_rows
            if (edge_value := _finite_float(row.get("edge"))) is not None
            and (weight := (_finite_float(row.get("bet_size")) or 0.0)) > 0
        ])
        entry["gross_stake"] = float(entry["gross_stake"]) + stake  # type: ignore[arg-type]
        entry["total_pnl"] = float(entry["total_pnl"]) + pnl  # type: ignore[arg-type]
        entry["stakes"].append(stake)  # type: ignore[union-attr]
        if edge is not None:
            entry["edges"].append(edge)  # type: ignore[union-attr]
        entry["wl"].add_trade(group_rows, pnl)  # type: ignore[union-attr]

    out = []
    for r in grouped.values():
        wl: _WinLossAccumulator = r["wl"]  # type: ignore[assignment]
        n_resolved = wl.n_resolved
        wins = wl.wins
        losses = wl.losses
        total_pnl = float(r["total_pnl"] or 0.0)  # type: ignore[arg-type]
        gross_stake = float(r["gross_stake"] or 0.0)  # type: ignore[arg-type]
        gross_w = wl.gross_win
        gross_l = wl.gross_loss
        edges = r["edges"]  # type: ignore[assignment]
        stakes = r["stakes"]  # type: ignore[assignment]
        roi_pct = (total_pnl / gross_stake * 100) if gross_stake > 0 else 0.0
        win_rate = (wins / n_resolved * 100) if n_resolved > 0 else 0.0
        pf = (gross_w / gross_l) if gross_l > 0 else 0.0
        out.append({
            "name": r["strategy"],
            "side": r["side"],
            "n_bets": r["n_bets"],
            "n_resolved": n_resolved,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_edge": round((sum(edges) / len(edges)) if edges else 0.0, 4),
            "avg_stake": round((sum(stakes) / len(stakes)) if stakes else 0.0, 2),
            "roi_pct": round(roi_pct, 1),
            "profit_factor": round(pf, 2),
        })
    return sorted(out, key=lambda r: float(r["total_pnl"]), reverse=True)


def _ymid_exits(conn: sqlite3.Connection, cutoff_utc: str) -> dict[str, object]:
    """TP / SL fire counts + average P&L for the YMID strategy."""
    base = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM ledger
        WHERE event_type IN ('bet','dry_run')
          AND COALESCE(json_extract(event_detail,'$.strategy'),'NO') = 'YMID'
          AND outcome != 'CANCELLED'
          AND bet_ts >= ?
        """,
        (cutoff_utc,),
    ).fetchone()
    total_ymid = int(base["n"] or 0)

    def _by_reason(reason_prefix: str) -> tuple[int, float]:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(AVG(pnl), 0) AS avg_pnl
            FROM ledger
            WHERE event_type IN ('bet','dry_run')
              AND outcome = 'CLOSED'
              AND json_extract(event_detail,'$.close_reason') LIKE ?
              AND bet_ts >= ?
            """,
            (f"{reason_prefix}%", cutoff_utc),
        ).fetchone()
        return int(row["n"] or 0), float(row["avg_pnl"] or 0.0)

    tp_n, tp_avg = _by_reason("ymid_tp")
    sl_n, sl_avg = _by_reason("ymid_sl")
    return {
        "tp": {
            "n": tp_n,
            "fire_rate": round(tp_n / total_ymid * 100, 1) if total_ymid else 0.0,
            "avg_pnl": round(tp_avg, 2),
        },
        "sl": {
            "n": sl_n,
            "fire_rate": round(sl_n / total_ymid * 100, 1) if total_ymid else 0.0,
            "avg_pnl": round(sl_avg, 2),
        },
    }


def _calendar_by_month(conn: sqlite3.Connection, cutoff_utc: str) -> dict[str, list[dict[str, object]]]:
    """Per target_date P&L grouped by YYYY-MM key."""
    rows = _row_dicts(conn.execute(
        """
        SELECT *
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN', 'LOSS', 'CLOSED')
          AND bet_ts >= ?
          AND target_date IS NOT NULL
        ORDER BY target_date ASC
        """,
        (cutoff_utc,),
    ).fetchall())
    by_date: dict[str, dict[str, object]] = {}
    for trade in _terminal_trade_groups(rows, outcomes=_PNL_OUTCOMES):
        d = str(trade.get("target_date") or "")
        if not d:
            continue
        entry = by_date.setdefault(d, {
            "n_trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
        })
        pnl = float(trade["pnl"] or 0.0)
        outcome = str(trade["outcome"])
        entry["n_trades"] = int(entry["n_trades"]) + 1
        entry["pnl"] = float(entry["pnl"]) + pnl
        if _trade_is_win(outcome, pnl):
            entry["wins"] = int(entry["wins"]) + 1
        elif _trade_is_loss(outcome, pnl):
            entry["losses"] = int(entry["losses"]) + 1

    out: dict[str, list[dict[str, object]]] = {}
    for d, entry in sorted(by_date.items()):
        try:
            month_key = d[:7]
        except Exception:
            continue
        out.setdefault(month_key, []).append({
            "date": d,
            "n_trades": int(entry["n_trades"] or 0),
            "wins": int(entry["wins"] or 0),
            "losses": int(entry["losses"] or 0),
            "pnl": round(float(entry["pnl"] or 0.0), 2),
        })
    return out


def _station_sparks(conn: sqlite3.Connection, cutoff_utc: str, days: int = 14) -> dict[str, list[float]]:
    """14-day cumulative P&L sparkline per station."""
    rows = conn.execute(
        """
        SELECT
          station_id,
          substr(bet_ts, 1, 10) AS d,
          COALESCE(SUM(pnl), 0) AS pnl
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN', 'LOSS', 'CLOSED')
          AND bet_ts >= datetime('now', ?)
          AND bet_ts >= ?
        GROUP BY station_id, d
        ORDER BY station_id, d ASC
        """,
        (f"-{days} days", cutoff_utc),
    ).fetchall()
    by_st: dict[str, list[float]] = {}
    for r in rows:
        by_st.setdefault(r["station_id"], []).append(float(r["pnl"] or 0.0))
    out = {}
    for st, dailies in by_st.items():
        cum = 0.0
        seq = [0.0]
        for v in dailies:
            cum += v
            seq.append(round(cum, 2))
        out[st] = seq
    return out


def _open_positions_for_v2(
    open_positions: list[dict],
    *,
    stake_basis_capital: float | None = None,
) -> list[dict[str, object]]:
    """Translate enriched ledger rows into grouped v2 trade-row schema."""
    return _group_positions_for_v2(
        open_positions,
        resolved=False,
        stake_basis_capital=stake_basis_capital,
    )


def _resolved_positions_for_v2(resolved_positions: list[dict]) -> list[dict[str, object]]:
    # No row cap: the resolved-bets table is the operator's trade journal and
    # must show every resolved bet in the queried window. The earlier limit=50
    # truncated the latest 50 trade-groups, which (with ~15-20 unique groups
    # per day) made the table appear to span only the last 3 days even when
    # range="All" was selected.
    return _group_positions_for_v2(resolved_positions, resolved=True)


def _calibration_by_station(conn: sqlite3.Connection) -> dict[str, list[dict[str, object]]]:
    """Per-station LUT bucket → predicted vs observed (real, but coarse)."""
    rows = conn.execute(
        """
        SELECT station_id, pred_bucket_low, pred_bucket_high, n, observed, mean_pred
        FROM lut_bucket_stats
        WHERE n IS NOT NULL AND n > 0
        ORDER BY station_id, pred_bucket_low
        """
    ).fetchall()
    out: dict[str, list[dict[str, object]]] = {}
    for r in rows:
        st = r["station_id"]
        lo = float(r["pred_bucket_low"])
        hi = float(r["pred_bucket_high"])
        bucket = f"{int(round(lo*100))}-{int(round(hi*100))}%"
        out.setdefault(st, []).append({
            "bucket": bucket,
            "n": int(r["n"]),
            "predicted": round(float(r["mean_pred"] or (lo + hi) / 2.0), 3),
            "observed": round(float(r["observed"] or 0.0), 3),
        })
    return out


def build_htb_data(
    conn: sqlite3.Connection,
    *,
    initial_bankroll: float,
    dry_run: bool,
    boot_time: _dt,
    cutoff_utc: str,
    # Helpers from app.py — passed in to avoid circular imports.
    session_baseline_capital,
    dashboard_peak_capital,
    dashboard_realized_capital,
    sum_polymarket_fees,
    coverage_by_station,
    lut_by_station,
    load_dashboard_station_rows,
    dashboard_station_configs,
    eligibility_funnel,
    enrich_ledger_positions,
    ref_start_date: str,
    min_coverage_pct: float,
    active_target_date: str = "",
    range_days: int | None = None,
) -> dict[str, object]:
    """Build the HTB_DATA dict shape consumed by the v2 dashboard SPA.

    Helpers from app.py are passed in as keyword arguments rather than
    imported, because v2_data is imported by app.py — pulling the helpers
    in via a normal import would create a cycle. The DI shape is the cost
    of the no-cycle constraint.
    """
    from hightempbot.stations import get_all_stations

    runtime_st = get_all_stations(conn)
    dashboard_rows = load_dashboard_station_rows(conn)
    dashboard_st = dashboard_station_configs(dashboard_rows)

    baseline = session_baseline_capital(conn, initial_bankroll)

    # `range_days` (when set) tightens realized/audit rows and charts to the
    # last N days. Visible Net P&L and Capital are realized-only; Data API
    # open-position marks remain wallet/detail data and are not included in
    # the main account cards. Two values are NOT windowed and always reflect
    # the current session state:
    #
    #   * ``capital`` — the operator's current account balance. A brokerage
    #     UI doesn't change your balance when you toggle the time range;
    #     neither should this one.
    #   * ``totalPnl`` / ``realizedPnl`` — the visible Net P&L number. This
    #     intentionally excludes unrealized Data API marks.
    #   * ``peak`` / ``dd_pct`` — labeled "Max DD · current peak-to-trough"
    #     in the UI. The word ``current`` is load-bearing: this is the
    #     realized drawdown right now, not a windowed slice. Windowing would
    #     hide the very drawdown the halt gate is comparing against.
    #
    # Both are computed from the session floor regardless of the selected
    # range, so the user can compare windowed performance against their
    # actual current capital.
    if range_days is not None and range_days > 0:
        range_floor_utc = (
            (_dt.now(timezone.utc) - timedelta(days=range_days))
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        kpi_cutoff_utc = max(cutoff_utc, range_floor_utc)
        # Resolved-bets table follows the same window as KPI cards so the
        # 7d/30d buttons on Trade Journal narrow the table contents.
        resolved_cutoff_utc = range_floor_utc
    else:
        kpi_cutoff_utc = cutoff_utc
        # "All" honors the session-floor anchor (same as KPI cards) — the
        # operator-anchored cutover defines the dashboard's universe, so the
        # trade journal cannot surface pre-cutover history.
        resolved_cutoff_utc = cutoff_utc

    open_cost_basis_row = conn.execute(
        """
        SELECT COALESCE(SUM(bet_size), 0) AS x
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome = 'PENDING'
          AND station_id != 'RECOVERED'
          AND order_id IS NOT NULL
        """,
    ).fetchone()
    open_cost_basis = float(open_cost_basis_row["x"] or 0.0) if open_cost_basis_row else 0.0

    # Session-scoped (operator zero-reset 2026-08-10): the Withdrawal tile and
    # the ledger-fallback capital math count only return transfers made after
    # the session epoch, so pre-session withdrawals (e.g. the 2026-07-27
    # $84.05) no longer appear anywhere on a fresh session.
    return_transfer_outflow = return_transfer_notional(conn, since_utc=cutoff_utc)
    adjusted_baseline = max(0.0, baseline - return_transfer_outflow)
    ledger_realized_capital = max(
        0.0,
        dashboard_realized_capital(conn, initial_bankroll, cutoff_utc) - return_transfer_outflow,
    )
    ledger_peak = max(
        max(0.0, dashboard_peak_capital(conn, initial_bankroll, cutoff_utc) - return_transfer_outflow),
        adjusted_baseline,
        ledger_realized_capital,
    )
    capital = ledger_realized_capital
    peak = ledger_peak

    # Wallet-derived display. Data API values are retained for wallet detail
    # and reconciliation only; they do not override the realized P&L/Capital
    # cards in the main dashboard.
    wallet_balance: float | None = None
    wallet_peak: float | None = None
    wallet_sampled_at: str | None = None
    wallet_snapshot: dict[str, object] | None = None
    data_api_position_value = 0.0
    data_api_position_initial_value = 0.0
    data_api_position_cash_pnl = 0.0
    data_api_capital_source = "ledger_realized"
    data_api_reconciliation_warnings: list[str] = []
    configured_wallet_address = _configured_poly_funder()
    if not dry_run:
        wallet_snapshot = _latest_wallet_snapshot_for_dashboard(conn)
        if wallet_snapshot is not None:
            wallet_balance = _finite_float(wallet_snapshot.get("clobBalanceUsd"))
            wallet_sampled_at = str(wallet_snapshot.get("sampledAt") or "") or None
            data_api_position_value = _finite_float(
                wallet_snapshot.get("dataApiTrustedOpenPositionsValueUsd")
            ) or 0.0
            data_api_position_initial_value = _finite_float(
                wallet_snapshot.get("dataApiTrustedOpenPositionsInitialValueUsd")
            ) or 0.0
            data_api_position_cash_pnl = _finite_float(
                wallet_snapshot.get("dataApiTrustedOpenPositionsCashPnlUsd")
            ) or 0.0
            data_api_reconciliation_warnings = [
                str(w) for w in wallet_snapshot.get("dataApiReconciliationWarnings") or []
            ]
        if wallet_balance is None and not configured_wallet_address:
            try:
                wrow = conn.execute(
                    "SELECT wallet_balance, sampled_at FROM bankroll_peak "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if wrow is not None and wrow["wallet_balance"] is not None:
                    wallet_balance = float(wrow["wallet_balance"])
                    wallet_sampled_at = wrow["sampled_at"]
            except Exception:
                # bankroll_peak table might not exist on a freshly migrated DB
                # before the first tick. Silently fall through to ledger math.
                pass

    if wallet_balance is not None:
        sources = (
            wallet_snapshot.get("sourcesChecked")
            if isinstance(wallet_snapshot, dict) and isinstance(wallet_snapshot.get("sourcesChecked"), dict)
            else {}
        )
        fresh_api_positions = bool(
            isinstance(wallet_snapshot, dict)
            and wallet_snapshot.get("fresh")
            and sources.get("dataApiPositions")
        )
        if fresh_api_positions:
            open_cost_basis = data_api_position_initial_value
        # Capital/peak mirror the live halt gate (wallet basis floored at
        # ledger realized), not the session-ledger walk above: operator
        # deposits reach the wallet without any ledger row, so after a full
        # withdrawal + re-fund the ledger walk reports $0 capital and a fake
        # 100% drawdown while the gate itself trades normally off the wallet.
        # api_position_value stays None here even when the Data API snapshot
        # is fresh: visible Capital excludes unrealized open-position marks
        # (product decision above; pinned by
        # test_data_api_marks_do_not_change_visible_capital_or_pnl), so the
        # display uses wallet + ledger open COST, accepting a small fork from
        # the gate's mark-inclusive basis while positions are open.
        capital, peak = live_gate_capital_view(
            conn,
            initial_bankroll,
            wallet_balance=wallet_balance,
            api_position_value=None,
        )
        data_api_capital_source = "live_gate"
        wallet_peak = peak

    dd_pct = ((peak - capital) / peak * 100) if peak > 0 else 0.0
    dd_color = "green" if dd_pct < 15 else ("yellow" if dd_pct < 25 else "red")

    # Aggregate ledger totals
    # `pending_exposure` excludes station_id='RECOVERED' — recovery sentinel
    # rows persist indefinitely (operator must `patch_recovered_orphan` to
    # relink) and would otherwise inflate the dashboard pending number with
    # every crash. Capital module's `get_capital_snapshot` does the same.
    # WIN/LOSS semantics mirror _strategies_breakdown: CLOSED+positive PnL is
    # counted as a win (TAIL TP exit); CLOSED+non-positive as a loss.
    kpi_row = conn.execute(
        """
        SELECT
          COALESCE(SUM(COALESCE(pnl, 0)), 0) AS total_pnl,
          COALESCE(SUM(CASE WHEN outcome = 'PENDING' AND station_id != 'RECOVERED' THEN bet_size ELSE 0 END), 0) AS pending_exposure,
          COUNT(CASE WHEN outcome = 'PENDING' AND station_id != 'RECOVERED' THEN 1 END) AS open_positions,
          COUNT(CASE WHEN outcome IN ('WIN','LOSS','PUSH','CLOSED') THEN 1 END) AS resolved,
          SUM(CASE WHEN outcome = 'WIN' OR (outcome = 'CLOSED' AND pnl > 0) THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN outcome = 'LOSS' OR (outcome = 'CLOSED' AND COALESCE(pnl, 0) <= 0) THEN 1 ELSE 0 END) AS losses,
          COALESCE(SUM(CASE WHEN outcome = 'WIN' OR (outcome = 'CLOSED' AND pnl > 0) THEN pnl ELSE 0 END), 0) AS gross_win,
          COALESCE(SUM(CASE WHEN outcome = 'LOSS' OR (outcome = 'CLOSED' AND COALESCE(pnl, 0) <= 0) THEN ABS(pnl) ELSE 0 END), 0) AS gross_loss,
          COALESCE(AVG(CASE WHEN outcome IN ('WIN','LOSS','PUSH','CLOSED') THEN edge END), 0) AS avg_edge_resolved
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome != 'CANCELLED'
          AND bet_ts >= ?
        """,
        (kpi_cutoff_utc,),
    ).fetchone()
    fees_paid = sum_polymarket_fees(conn)
    kpi_trade_rows = _row_dicts(conn.execute(
        """
        SELECT *
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN','LOSS','PUSH','CLOSED')
          AND bet_ts >= ?
        """,
        (kpi_cutoff_utc,),
    ).fetchall())
    kpi_trade_stats = _resolved_trade_stats(kpi_trade_rows)

    wins = kpi_trade_stats.wins
    losses = kpi_trade_stats.losses
    n_resolved = wins + losses
    win_rate = (wins / n_resolved * 100) if n_resolved > 0 else 0.0
    realized_pnl = float(kpi_row["total_pnl"] or 0.0)
    account_pnl = realized_pnl
    account_pnl_pct = (account_pnl / adjusted_baseline * 100.0) if adjusted_baseline > 0 else 0.0
    total_pnl = realized_pnl
    # Average edge across resolved bets in window.
    #
    # Semantics: edge percent (×100), AVG over outcomes in
    # {WIN,LOSS,PUSH,CLOSED} — INCLUDING PUSH rows. PUSH bets have no
    # realized PnL but their pre-trade edge stays meaningful as a
    # signal-quality measure.
    #
    # Note: `strategies[*].avg_edge` (line ~322) uses a different scope:
    # raw fraction (no ×100), no outcome filter (PENDING included),
    # because it's the per-strategy signal-quality picture rather than
    # a window-realized KPI. Don't compare top-level `avgEdge` against
    # per-strategy `avg_edge` directly — the units differ by 100×.
    avg_edge_pct = float(kpi_row["avg_edge_resolved"] or 0.0) * 100.0

    # Today's bets / signals: scoped to the scanner's current UTC target_date.
    # Cumulative views above keep using `bet_ts >= cutoff` so resolved history
    # doesn't disappear at UTC midnight.
    # (Removed three station-local "today" helper calls whose return values
    # were assigned to locals but never read — ce-review maintainability #30.)
    if active_target_date:
        today_rows_raw = conn.execute(
            """
            SELECT *
            FROM ledger
            WHERE event_type IN ('bet', 'dry_run')
              AND outcome != 'CANCELLED'
              AND target_date = ?
            """,
            (active_target_date,),
        ).fetchall()
        today_rows = _row_dicts(today_rows_raw)
        cutoff_today_n = len(_group_rows_by_trade(today_rows))
        cutoff_today_volume = sum((_finite_float(row.get("bet_size")) or 0.0) for row in today_rows)
        cutoff_signals = conn.execute(
            """
            SELECT
                COUNT(*) AS n,
                SUM(CASE WHEN outcome = 'WOULD_BET' THEN 1 ELSE 0 END) AS would_bet
            FROM signals
            WHERE target_date = ?
            """,
            (active_target_date,),
        ).fetchone()
        cutoff_signals_n = int(cutoff_signals["n"] or 0) if cutoff_signals is not None else 0
        cutoff_would_bet = int(cutoff_signals["would_bet"] or 0) if cutoff_signals is not None else 0
    else:
        cutoff_today_n = 0
        cutoff_today_volume = 0.0
        cutoff_signals_n = 0
        cutoff_would_bet = 0

    # Funnel
    cov_m = coverage_by_station(conn, ref_start_date)
    lut_m = lut_by_station(conn)
    funnel = eligibility_funnel(dashboard_st, cov_m, lut_m, active_ids=set(runtime_st))

    # Stations list — coarse stage classification mirrors the live dashboard.
    stations_list: list[dict[str, object]] = []
    for icao, cfg in sorted(dashboard_st.items()):
        cov = cov_m.get(icao)
        lut = lut_m.get(icao) or {}
        # `lut_total_n` displayed in the Stations table = seeded days, not the
        # summed per-bucket triple count. Each station-day inserts ~3 bucket
        # rows into pred_bucket_history; summing `n` across buckets inflates
        # the count by ~3x and gives operators a misleading 1.5–2k number when
        # they expect ~600 days.
        lut_total_n = int(lut.get("seeded_days") or 0)
        # `bucket_rows` is the canonical key from _lut_by_station (count of
        # distinct lut_bucket_stats rows). Earlier we read `n_buckets` which
        # the producer never emits, so the column was permanently 0/8.
        lut_buckets = int(lut.get("bucket_rows") or 0)
        lut_age = lut.get("age_hours")
        # Round to integer hours so the UI shows "4h" not "4.328383939h".
        if isinstance(lut_age, (int, float)):
            lut_age = int(round(float(lut_age)))
        lut_stale = bool(lut.get("stale"))
        source_supported = supports_live_resolution_source(
            getattr(cfg, "resolution_source", None)
        )
        if not source_supported:
            stage = "source_fail"
        elif cov is None or cov < min_coverage_pct:
            stage = "coverage_fail" if cov is not None else "source_fail"
        elif lut_total_n == 0:
            stage = "no_lut"
        elif lut_stale:
            stage = "lut_stale"
        else:
            stage = "bettable"
        stations_list.append({
            "id": icao,
            "city": getattr(cfg, "city", "") or "",
            "flag": _icao_to_flag(icao),
            "stage": stage,
            "status": "LIVE" if (icao in runtime_st and not dry_run) else "DRY_RUN",
            "source": getattr(cfg, "resolution_source", "") or "",
            "coverage": round(cov, 2) if cov is not None else None,
            "last_actual": (lut.get("last_actual_date") or ""),
            "actual_fresh": bool(lut.get("actual_fresh", False)),
            "fc_fresh": True,
            "lut_total_n": lut_total_n,
            "lut_buckets": lut_buckets,
            "lut_age": lut_age,
            "lut_stale": lut_stale,
        })

    # Performance by station — cutoff-scoped so the v2 dashboard respects the
    # session boundary. The legacy `_performance_stats(conn)` (deleted
    # 2026-08-09) returned all-time stats and would have leaked pre-cutoff bets
    # into the v2 perStation/Top3 tables.
    # WIN/LOSS semantics match the rest of the dashboard: CLOSED+positive pnl
    # is a TP-exit win, CLOSED+non-positive is a loss.
    city_map = {icao: getattr(cfg, "city", "") for icao, cfg in dashboard_st.items()}
    perf_source_rows = _row_dicts(conn.execute(
        """
        SELECT *
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome != 'CANCELLED'
          AND bet_ts >= ?
        ORDER BY bet_ts ASC, id ASC
        """,
        (cutoff_utc,),
    ).fetchall())
    # shared W/L tally via _WinLossAccumulator (see
    # _strategies_breakdown for the other call site). Per-station rollup only
    # needs n_bets + total_pnl in addition to the W/L counters.
    grouped_perf: dict[str, dict[str, object]] = {}
    for group_rows in _group_rows_by_trade(perf_source_rows).values():
        first = group_rows[0]
        station_id = str(first.get("station_id") or "")
        if not station_id:
            continue
        station_perf = grouped_perf.setdefault(station_id, {
            "key": station_id,
            "n_bets": 0,
            "total_pnl": 0.0,
            "wl": _WinLossAccumulator(),
        })
        station_perf["n_bets"] = int(station_perf["n_bets"]) + 1  # type: ignore[arg-type]
        pnl = sum((_finite_float(row.get("pnl")) or 0.0) for row in group_rows)
        station_perf["total_pnl"] = float(station_perf["total_pnl"]) + pnl  # type: ignore[arg-type]
        station_perf["wl"].add_trade(group_rows, pnl)  # type: ignore[union-attr]

    # Flatten the accumulator back to the legacy dict shape the downstream
    # block reads (n_resolved / wins / losses / gross_win / gross_loss).
    flat_perf_rows: list[dict[str, object]] = []
    for sp in grouped_perf.values():
        wl: _WinLossAccumulator = sp["wl"]  # type: ignore[assignment]
        flat_perf_rows.append({
            "key": sp["key"],
            "n_bets": sp["n_bets"],
            "n_resolved": wl.n_resolved,
            "wins": wl.wins,
            "losses": wl.losses,
            "gross_win": wl.gross_win,
            "gross_loss": wl.gross_loss,
            "total_pnl": sp["total_pnl"],
        })
    perf_rows_raw = sorted(
        flat_perf_rows,
        key=lambda r: float(r["total_pnl"] or 0.0),  # type: ignore[arg-type]
        reverse=True,
    )

    # Latest EMOS (mu, sigma) per station for the per-station detail panels.
    # Compute from the most recent horizon=1 forecast_archive ensemble + the
    # current calibration_params snapshot. predict_emos uses:
    #   mu    = a + b * ens_mean
    #   sigma = sqrt(exp(c) + exp(d) * ens_var)
    emos_latest_by_station: dict[str, tuple[float, float]] = {}
    try:
        # Latest target_date per station with a horizon=1 ensemble of >=4
        # distinct centres. Filtered to source='openmeteo' (live-eval rows
        # would otherwise double-count) and deduped by (station, centre) on
        # latest ingested_at — mirrors the live evaluator in lut.py so the
        # dashboard's μ/σ matches what the bot used to score.
        ens_rows = conn.execute(
            """
            SELECT fa.station_id, fa.centre, fa.tmax_celsius
            FROM forecast_archive fa
            JOIN (
                SELECT station_id AS s, MAX(target_date) AS d
                FROM forecast_archive
                WHERE horizon = 1 AND source = 'openmeteo'
                GROUP BY station_id
            ) latest ON latest.s = fa.station_id AND latest.d = fa.target_date
            WHERE fa.horizon = 1 AND fa.source = 'openmeteo'
            ORDER BY fa.station_id, fa.centre, fa.ingested_at DESC
            """,
        ).fetchall()
        ensembles_raw: dict[str, dict[str, float]] = {}
        for ens_row in ens_rows:
            sid = ens_row["station_id"]
            centre = ens_row["centre"]
            try:
                v = float(ens_row["tmax_celsius"])
            except (TypeError, ValueError):
                continue
            per_station = ensembles_raw.setdefault(sid, {})
            if centre not in per_station:
                per_station[centre] = v
        ensembles: dict[str, list[float]] = {
            sid: list(centres.values()) for sid, centres in ensembles_raw.items()
        }
        cal_rows = conn.execute(
            "SELECT station_id, params_blob FROM calibration_params "
            "WHERE horizon = 1 AND param_type = 'emos'"
        ).fetchall()
        for cal in cal_rows:
            sid = cal["station_id"]
            members = ensembles.get(sid)
            if not members or len(members) < 4:
                continue
            # Per-station try/except: a single bad params_blob or
            # predict_emos failure must not wipe other stations' results.
            try:
                p = json.loads(cal["params_blob"])
                params = EMOSParams(
                    a=float(p["a"]), b=float(p["b"]),
                    c=float(p["c"]), d=float(p["d"]),
                    n_samples=int(p.get("n_samples", 0)),
                )
                mu, sigma = predict_emos(params, np.asarray(members, dtype=float))
                emos_latest_by_station[sid] = (mu, sigma)
            except (TypeError, ValueError, json.JSONDecodeError, KeyError):
                continue
            except Exception:
                logger.exception("EMOS predict failed for station %s", sid)
                continue
    except Exception:
        # Outer except: SQL / schema failures. Inner per-station failures
        # are already isolated above, so reaching here means the whole
        # block didn't run.
        logger.exception("EMOS dashboard computation failed")
        emos_latest_by_station = {}
    perf_rows = []
    for r in perf_rows_raw:
        d = dict(r)
        d["win_rate"] = (d["wins"] / d["n_resolved"]) if d["n_resolved"] else None
        d["pf"] = (d["gross_win"] / d["gross_loss"]) if (d["gross_loss"] or 0) > 0 else None
        perf_rows.append(d)
    performance_by_station: list[dict[str, object]] = []
    for r in perf_rows:
        if r.get("key") in (None, ""):
            continue
        win_pct = round((r["win_rate"] or 0) * 100) if r.get("win_rate") is not None else 0
        emos_pair = emos_latest_by_station.get(r["key"])
        performance_by_station.append({
            "key": r["key"],
            "city": city_map.get(r["key"], ""),
            "flag": _icao_to_flag(r["key"]),
            "n_bets": int(r["n_bets"] or 0),
            "n_resolved": int(r["n_resolved"] or 0),
            "wins": int(r["wins"] or 0),
            "losses": int(r["losses"] or 0),
            "win_rate": win_pct,
            "pf": round(float(r["pf"] or 0.0), 2),
            "total_pnl": round(float(r["total_pnl"] or 0.0), 2),
            "emos_mu": round(emos_pair[0], 2) if emos_pair else None,
            "emos_sigma": round(emos_pair[1], 2) if emos_pair else None,
        })

    sorted_perf = sorted(performance_by_station, key=lambda x: x["total_pnl"], reverse=True)
    top3_winners = [p for p in sorted_perf if p["total_pnl"] > 0][:3]
    top3_losers = [p for p in sorted(performance_by_station, key=lambda x: x["total_pnl"]) if p["total_pnl"] < 0][:3]

    # Open + resolved positions
    open_rows = conn.execute(
        """
        SELECT l.*, COALESCE(l.actual_tmax, a.tmax_celsius) AS actual_tmax
        FROM ledger l
        LEFT JOIN actuals a ON a.station_id = l.station_id AND a.local_date = l.target_date
        WHERE l.outcome = 'PENDING' AND l.event_type IN ('bet', 'dry_run')
        ORDER BY l.bet_ts DESC
        """
    ).fetchall()
    enriched_open = enrich_ledger_positions(open_rows, runtime_st)
    for p in enriched_open:
        cfg = runtime_st.get(p.get("station_id") or "")
        if cfg:
            p["city"] = cfg.city
    _attach_api_positions(enriched_open, wallet_snapshot)
    open_positions_list = _open_positions_for_v2(
        enriched_open,
        stake_basis_capital=capital,
    )
    pending_exposure = sum((_finite_float(p.get("bet_size")) or 0.0) for p in enriched_open)

    resolved_rows = conn.execute(
        """
        SELECT l.*, COALESCE(l.actual_tmax, a.tmax_celsius) AS actual_tmax
        FROM ledger l
        LEFT JOIN actuals a ON a.station_id = l.station_id AND a.local_date = l.target_date
        WHERE l.outcome IN ('WIN','LOSS','PUSH','CLOSED')
          AND l.event_type IN ('bet', 'dry_run')
          AND l.bet_ts >= ?
        ORDER BY l.bet_ts DESC
        """,
        (resolved_cutoff_utc,),
    ).fetchall()
    enriched_resolved = enrich_ledger_positions(resolved_rows, runtime_st)
    for p in enriched_resolved:
        cfg = runtime_st.get(p.get("station_id") or "")
        if cfg:
            p["city"] = cfg.city
    resolved_positions_list = _resolved_positions_for_v2(enriched_resolved)

    # Overview-tab realized/history surfaces use kpi_cutoff_utc.
    # Calendar stays on cutoff_utc — its per-day grid is its own range surface.
    equity_curve = _equity_curve_from_ledger(conn, kpi_cutoff_utc, initial_bankroll)
    account_equity_curve = _account_equity_curve(conn, kpi_cutoff_utc, initial_bankroll)
    withdrawal_events = _withdrawal_events(conn, kpi_cutoff_utc)
    weekly_pnl = _weekly_pnl(conn, kpi_cutoff_utc)
    pnl_dist = _pnl_distribution(conn, kpi_cutoff_utc)
    streaks = _streaks(conn, kpi_cutoff_utc)
    ratios = _ratios(
        account_equity_curve, total_pnl, initial_bankroll, dd_pct,
        wins, losses, kpi_trade_stats.gross_win, kpi_trade_stats.gross_loss,
    )
    strategies = _strategies_breakdown(conn, kpi_cutoff_utc)
    ymid_exits = _ymid_exits(conn, kpi_cutoff_utc)
    calendar = _calendar_by_month(conn, cutoff_utc)
    station_sparks = _station_sparks(conn, kpi_cutoff_utc)
    calibration_by_station = _calibration_by_station(conn)
    # Aggregate (cross-station) calibration as average across stations.
    overall_buckets: dict[str, dict[str, float]] = {}
    for buckets in calibration_by_station.values():
        for b in buckets:
            agg = overall_buckets.setdefault(b["bucket"], {"n": 0, "p": 0.0, "o": 0.0, "w": 0.0})
            n = float(b["n"])
            agg["n"] += n
            agg["p"] += float(b["predicted"]) * n
            agg["o"] += float(b["observed"]) * n
    overall_calibration = []
    for bucket_name, agg in overall_buckets.items():
        if agg["n"] <= 0:
            continue
        overall_calibration.append({
            "bucket": bucket_name,
            "n": int(agg["n"]),
            "predicted": round(agg["p"] / agg["n"], 3),
            "observed": round(agg["o"] / agg["n"], 3),
        })
    # Sort by bucket lower bound for chart x-axis
    def _bucket_order(b):
        try:
            return int(b["bucket"].split("-")[0])
        except Exception:
            return 0
    overall_calibration.sort(key=_bucket_order)

    # Uptime
    elapsed = _dt.now(timezone.utc) - boot_time
    days = elapsed.days
    hours = elapsed.seconds // 3600
    minutes = (elapsed.seconds % 3600) // 60
    uptime = f"up {days}d {hours:02d}:{minutes:02d}" if days else f"up {hours}h {minutes}m"

    # Last scan ago
    last_scan_row = conn.execute(
        "SELECT created_at FROM pipeline_health WHERE stage = 'scan' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if last_scan_row and last_scan_row["created_at"]:
        scan_dt = _parse_iso_utc(last_scan_row["created_at"])
        ago_s = (_dt.now(timezone.utc) - scan_dt).total_seconds() if scan_dt else None
        if ago_s is None:
            last_scan_ago = "unknown"
        elif ago_s < 60:
            last_scan_ago = f"{int(ago_s)}s ago"
        elif ago_s < 3600:
            last_scan_ago = f"{int(ago_s // 60)} min ago"
        else:
            last_scan_ago = f"{int(ago_s // 3600)}h ago"
    else:
        last_scan_ago = "no scans yet"

    # Per-station 9-model ensemble — wires real per-model latest tmax from
    # forecast_archive. Per-model 30-day accuracy is left None (pending ledger
    # tracking); the React UI renders "—" when accuracy is null.
    from hightempbot.execution.strategy_constants import EXPECTED_MODELS

    # Pick the latest forecast per (station, centre) within a 14-day window. On
    # a live bot this resolves to today's forecast for tomorrow's target_date;
    # on a snapshot DB the window keeps the lookup robust against a few days
    # of stale data.
    fc_window_start = (_dt.now(timezone.utc).date() - timedelta(days=14)).isoformat()
    fc_rows = conn.execute(
        f"""
        SELECT station_id, centre, tmax_celsius, target_date
        FROM (
          SELECT station_id, centre, tmax_celsius, target_date,
                 ROW_NUMBER() OVER (
                     PARTITION BY station_id, centre
                     ORDER BY target_date DESC, ingested_at DESC
                 ) AS rn
          FROM forecast_archive
          WHERE horizon = 1
            AND tmax_celsius IS NOT NULL
            AND target_date >= ?
            AND centre IN ({','.join('?' * len(EXPECTED_MODELS))})
        )
        WHERE rn = 1
        """,
        (fc_window_start, *EXPECTED_MODELS),
    ).fetchall()
    fc_by_station_centre: dict[tuple[str, str], float] = {}
    for r in fc_rows:
        fc_by_station_centre[(r["station_id"], r["centre"])] = float(r["tmax_celsius"])

    ensemble_by_station: dict[str, list[dict[str, object]]] = {}
    for icao in runtime_st.keys():
        ensemble_by_station[icao] = [
            {
                "name": m.split("_")[0].upper(),
                "source": m,
                "tmax": fc_by_station_centre.get((icao, m)),
                "accuracy": None,
            }
            for m in EXPECTED_MODELS
        ]

    return {
        # contract-version stamp on the v2 dashboard
        # payload. Documentation only — bump when the public field shape
        # changes (add/rename/remove top-level keys). The SPA can refuse to
        # render against an unexpected version.
        "schemaVersion": 2,
        # Top-level metrics
        "capital": round(capital, 2),
        # Wallet-derived snapshot fields (live mode only). The SPA can label
        # the capital card "Wallet: $X (CLOB pUSD)" when walletBalance is
        # present, and fall back to "Ledger" when None.
        "walletBalance": round(wallet_balance, 2) if wallet_balance is not None else None,
        "walletPeak": round(wallet_peak, 2) if wallet_peak is not None else None,
        "walletSampledAt": wallet_sampled_at,
        "openCostBasis": round(open_cost_basis, 2),
        "capitalSource": data_api_capital_source,
        "dataApiOpenPositionValue": round(data_api_position_value, 2),
        "dataApiOpenPositionInitialValue": round(data_api_position_initial_value, 2),
        "dataApiOpenPositionCashPnl": round(data_api_position_cash_pnl, 2),
        "dataApiReconciliationWarnings": data_api_reconciliation_warnings,
        "returnTransferOutflow": round(return_transfer_outflow, 2),
        "initialBankroll": round(initial_bankroll, 2),
        "accountPnl": round(account_pnl, 2),
        "accountPnlPct": round(account_pnl_pct, 1),
        "totalPnl": round(total_pnl, 2),
        "realizedPnl": round(realized_pnl, 2),
        "resolvedCount": kpi_trade_stats.resolved,
        "feesPaid": round(fees_paid, 2),
        "winRate": round(win_rate, 1),
        "wins": wins,
        "losses": losses,
        "avgEdge": round(avg_edge_pct, 1),
        # Cutoff-scoped today counters: the v2 session reset means "today" is
        # "all bets/signals since the cutoff" rather than "today in any
        # station's local calendar day." A clean session starts at zero.
        "todaySignals": cutoff_signals_n,
        "todayWouldBet": cutoff_would_bet,
        "todayBets": cutoff_today_n,
        "todayVolume": round(cutoff_today_volume, 2),
        "ddPct": round(dd_pct, 1),
        "ddFillPct": round(min(dd_pct / DEFAULT_HALT_THRESHOLD_PCT * 100, 100), 1),
        "ddColor": dd_color,
        "ddHaltThreshold": DEFAULT_HALT_THRESHOLD_PCT,
        "reducedSizeThreshold": DEFAULT_REDUCED_AT_PCT_OF_HALT,
        "reducedSizeActive": dd_pct >= DEFAULT_HALT_THRESHOLD_PCT * DEFAULT_REDUCED_AT_PCT_OF_HALT / 100,
        "openPositions": len(open_positions_list),
        "pendingExposure": round(pending_exposure, 2),
        "uptime": uptime,
        "lastScanAgo": last_scan_ago,
        "mode": "DRY-RUN" if dry_run else "LIVE",
        # Funnel
        "funnel": {
            "available_count": int(funnel.get("available_count") or 0),
            "source_eligible_count": int(funnel.get("source_eligible_count") or 0),
            "coverage_eligible_count": int(funnel.get("coverage_eligible_count") or 0),
            "active_count": int(funnel.get("active_count") or 0),
            "bettable_count": int(funnel.get("bettable_count") or 0),
        },
        # Per-station perf
        "performanceByStation": performance_by_station,
        "top3Winners": top3_winners,
        "top3Losers": top3_losers,
        # Stations + sparks
        "stations": stations_list,
        "stationSparks": station_sparks,
        # Strategies + YMID exits
        "strategies": strategies,
        "strategiesConfig": _serialize_strategy_configs(),
        "ymidExits": ymid_exits,
        # Trade journal
        "openPositionsList": open_positions_list,
        "resolvedPositionsList": resolved_positions_list,
        # Charts + ratios
        "equityCurve": equity_curve,
        "tradingEquityCurve": equity_curve,
        "accountEquityCurve": account_equity_curve,
        "withdrawalEvents": withdrawal_events,
        "weeklyPnl": weekly_pnl,
        "pnlDist": pnl_dist,
        "streaks": streaks,
        "ratios": ratios,
        # Calendar + calibration for the scanner's current UTC target date.
        "targetDate": active_target_date,
        "calendar": calendar,
        "calibration": overall_calibration,
        "calibrationByStation": calibration_by_station,
        # 9-model ensemble (placeholder — see comment above)
        "ensembleByStation": ensemble_by_station,
        "_synthetic_ensemble": True,  # frontend can show a "placeholder" badge
    }
