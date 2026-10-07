"""FastAPI dashboard: serves the v2 SPA, its data payload and operator controls."""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from hightempbot.db.connection import get_connection
from hightempbot.persistence.ledger import decode_event_detail, poly_fee_charge
from hightempbot.stations import (
    StationConfig,
    celsius_to_fahrenheit,
    fahrenheit_to_celsius,
    supports_live_resolution_source,
)
from hightempbot.execution.strategy_constants import (
    DASHBOARD_SESSION_START_UTC,
    MAX_DD,
)

_STATIC_DIR = Path(__file__).parent / "static"

from datetime import datetime as _dt, timezone as _tz, timedelta as _td


def _filter_v2_payload(
    payload: dict,
    range_: str | None,
    month: str | None,
    station: str | None,
) -> dict:
    """Apply the ``range`` (last N days), ``month`` (YYYY-MM calendar) and
    ``station`` (ICAO) filters to a built payload. Unknown values are ignored."""
    if not isinstance(payload, dict):
        return payload
    range_days_map = {"7d": 7, "30d": 30, "90d": 90}
    if range_ in range_days_map:
        n = range_days_map[range_]
        eq = payload.get("equityCurve") or []
        if isinstance(eq, list):
            payload["equityCurve"] = eq[-n - 1:]  # +1 for the prefix-zero anchor
        wk = payload.get("weeklyPnl") or []
        if isinstance(wk, list):
            payload["weeklyPnl"] = wk[-max(1, n // 7):]
        cal = payload.get("calendar") or {}
        if isinstance(cal, dict):
            cutoff_iso = (_dt.now(_tz.utc).date() - _td(days=n)).isoformat()
            payload["calendar"] = {
                m: [d for d in entries if d.get("date", "") >= cutoff_iso]
                for m, entries in cal.items()
            }
    if month and isinstance(payload.get("calendar"), dict):
        payload["calendar"] = {month: payload["calendar"].get(month, [])}
    if station:
        sid = station.upper()
        for key in ("openPositionsList", "resolvedPositionsList", "performanceByStation"):
            lst = payload.get(key) or []
            if isinstance(lst, list):
                payload[key] = [
                    row for row in lst
                    if (row.get("id") or row.get("key") or row.get("station_id") or "").upper() == sid
                ]
        sparks = payload.get("stationSparks") or {}
        if isinstance(sparks, dict) and sid in sparks:
            payload["stationSparks"] = {sid: sparks[sid]}
        elif isinstance(sparks, dict):
            payload["stationSparks"] = {}
        ens = payload.get("ensembleByStation") or {}
        if isinstance(ens, dict) and sid in ens:
            payload["ensembleByStation"] = {sid: ens[sid]}
        elif isinstance(ens, dict):
            payload["ensembleByStation"] = {}
    return payload


def _session_floor() -> str:
    """``DASHBOARD_SESSION_START_UTC``: the floor for all session history."""
    return DASHBOARD_SESSION_START_UTC


def _clean_display_text(value: object | None) -> str:
    """Repair common mojibake before rendering dashboard text."""
    if value is None:
        return ""

    text = str(value)
    for codec in ("cp1252", "latin-1"):
        if not any(ch in text for ch in ("\u00c2", "\u00c3", "\u00e2", "\u00ce")):
            break
        try:
            repaired = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired:
            text = repaired

    replacements = {
        "\u00c2\u00b0F": "\u00b0F",
        "\u00c2\u00b0C": "\u00b0C",
        "\u00c2\u00b0": "\u00b0",
        "\u00c2": "",
        "\u00e2\u2030\u00a5": "\u2265",
        "\u00e2\u2030\u00a4": "\u2264",
        "\u00e2\u20ac\u201d": "\u2014",
        "\u00e2\u20ac\u201c": "\u2013",
        "\u00e2\u20ac\u00ba": "\u203a",
        "\u00e2\u20ac\u00b9": "\u2039",
        "\u00e2\u20ac\u00a2": "\u2022",
        "\u00e2\u2013\u00b6": "\u25b6",
        "\u00e2\u2013\u00bc": "\u25bc",
        "\u00c3\u2014": "\u00d7",
        "\u00ce\u00b8": "\u03b8",
        "\u00e2\u2020\u2019": "\u2192",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text

def _session_baseline_capital(conn, initial_bankroll: float) -> float:
    """The session starts from the initial bankroll."""
    del conn
    return float(initial_bankroll)

def _sum_polymarket_fees(conn) -> float:
    """Modeled taker fees (θ·p·(1−p) per share) on session bets, from fill price/size."""

    rows = conn.execute(
        """SELECT outcome, fill_price, fill_size, bet_size, event_detail
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND outcome IN ('WIN', 'LOSS', 'PUSH', 'CLOSED')
          AND bet_ts >= ?""",
        (_session_floor(),),
    ).fetchall()
    total = 0.0
    for row in rows:
        try:
            detail = decode_event_detail(row["event_detail"])
            cached_entry = detail.get("poly_entry_fee")
            cached_exit = detail.get("poly_exit_fee")
            if cached_entry is not None or cached_exit is not None:
                total += float(cached_entry or 0.0) + float(cached_exit or 0.0)
                continue

            fill_price = float(row["fill_price"]) if row["fill_price"] is not None else 0.0
            fill_size = row["fill_size"]
            bet_size = float(row["bet_size"] or 0.0)
            if fill_size is None and fill_price > 0 and bet_size > 0:
                fill_size = bet_size / fill_price
            if fill_size is None or fill_price <= 0 or fill_price >= 1:
                continue
            total += poly_fee_charge(fill_price, fill_size)
            if row["outcome"] == "CLOSED" and row["event_detail"]:
                close_price = detail.get("close_price")
                close_size = detail.get("close_size") or fill_size
                total += poly_fee_charge(close_price, close_size)
        except (TypeError, ValueError):
            continue
    return total

def _dashboard_peak_capital(
    conn, initial_bankroll: float, cutoff_utc: str | None = None
) -> float:
    """Peak running ledger capital over bets since ``cutoff_utc`` (default: session start)."""
    baseline = _session_baseline_capital(conn, initial_bankroll)
    floor = cutoff_utc if cutoff_utc is not None else _session_floor()
    peak_row = conn.execute(
        """
        SELECT MAX(running_capital) AS peak
        FROM (
            SELECT
                ? + SUM(COALESCE(pnl, 0)) OVER (ORDER BY bet_ts, id) AS running_capital
            FROM ledger
            WHERE event_type IN ('bet', 'dry_run')
              AND outcome NOT IN ('PENDING', 'CANCELLED')
              AND bet_ts >= ?
        )
        """,
        (baseline, floor),
    ).fetchone()
    peak = float(peak_row["peak"]) if peak_row and peak_row["peak"] is not None else baseline
    return max(peak, baseline)

def _dashboard_realized_capital(
    conn, initial_bankroll: float, cutoff_utc: str | None = None
) -> float:
    """Baseline plus realized ledger P&L since ``cutoff_utc`` (default: session start)."""
    baseline = _session_baseline_capital(conn, initial_bankroll)
    floor = cutoff_utc if cutoff_utc is not None else _session_floor()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(COALESCE(pnl, 0)), 0) AS total_pnl
        FROM ledger
        WHERE event_type IN ('bet', 'dry_run')
          AND bet_ts >= ?
        """,
        (floor,),
    ).fetchone()
    return baseline + float(row["total_pnl"] or 0.0)

def _coverage_by_station(conn, ref_start_date: str) -> dict[str, float]:
    """Return actuals coverage ratio per station since the reference start date."""
    import datetime as _dtmod

    ref_start = _dtmod.date.fromisoformat(ref_start_date)
    expected_days = (_dtmod.date.today() - ref_start).days + 1
    if expected_days <= 0:
        return {}

    coverage: dict[str, float] = {}
    for row in conn.execute(
        "SELECT station_id, COUNT(*) as n FROM actuals WHERE local_date >= ? GROUP BY station_id",
        (ref_start_date,),
    ).fetchall():
        coverage[row["station_id"]] = row["n"] / expected_days
    return coverage

def _lut_by_station(conn) -> dict[str, dict[str, object]]:
    """Per-station LUT summary for the Stations table: refresh time/age/staleness,
    seeded days, weighted bucket means, bucket count and the latest actual."""
    from hightempbot.execution.strategy_constants import LUT_STALE_HOURS
    import datetime as _dtmod

    rows = conn.execute(
        "SELECT station_id, pred_bucket_low, pred_bucket_high, n, hits, "
        " observed, mean_pred, refreshed_at "
        "FROM lut_bucket_stats"
    ).fetchall()

    by_station: dict[str, list] = {}
    for row in rows:
        by_station.setdefault(row["station_id"], []).append(dict(row))

    seeded_days_by_station = {
        row["station_id"]: row["days"]
        for row in conn.execute(
            "SELECT station_id, COUNT(DISTINCT local_date) AS days "
            "FROM pred_bucket_history GROUP BY station_id"
        ).fetchall()
    }

    # An actual is fresh if it is for yesterday or today (UTC).
    last_actual_by_station: dict[str, str] = {
        row["station_id"]: (row["d"] or "")
        for row in conn.execute(
            "SELECT station_id, MAX(local_date) AS d FROM actuals "
            "GROUP BY station_id"
        ).fetchall()
    }

    now = _dtmod.datetime.now(_tz_utc.utc)
    today_utc_iso = now.date().isoformat()
    yday_utc_iso = (now.date() - _dtmod.timedelta(days=1)).isoformat()
    result: dict[str, dict[str, object]] = {}
    for icao, bucket_rows in by_station.items():
        latest_refreshed = max((b["refreshed_at"] for b in bucket_rows), default=None)
        age_hours: float | None = None
        if latest_refreshed:
            try:
                refreshed_dt = _dtmod.datetime.strptime(
                    latest_refreshed, "%Y-%m-%d %H:%M:%S",
                ).replace(tzinfo=_tz_utc.utc)
                age_hours = (now - refreshed_dt).total_seconds() / 3600.0
            except Exception:
                age_hours = None
        total_n = sum(b["n"] for b in bucket_rows) or 0
        if total_n > 0:
            mean_observed_vals = [(b["observed"], b["n"]) for b in bucket_rows if b["observed"] is not None]
            obs_n = sum(n for _, n in mean_observed_vals)
            mean_observed = (
                sum(o * n for o, n in mean_observed_vals) / obs_n if obs_n > 0 else None
            )
            mean_pred_vals = [b["mean_pred"] for b in bucket_rows if b["mean_pred"] is not None]
            mean_pred = sum(mean_pred_vals) / len(mean_pred_vals) if mean_pred_vals else None
        else:
            mean_observed = None
            mean_pred = None

        last_actual_iso = last_actual_by_station.get(icao, "")
        result[icao] = {
            "refreshed_at": latest_refreshed,
            "age_hours": age_hours,
            "stale": (age_hours is not None and age_hours > LUT_STALE_HOURS),
            "bucket_rows": len(bucket_rows),
            "seeded_days": seeded_days_by_station.get(icao, 0),
            "total_n": total_n,
            "mean_pred": mean_pred,
            "mean_observed": mean_observed,
            "last_actual_date": last_actual_iso,
            "actual_fresh": last_actual_iso in (today_utc_iso, yday_utc_iso),
        }
    # Stations with actuals but no LUT rows yet.
    for icao, last_iso in last_actual_by_station.items():
        if icao not in result:
            result[icao] = {
                "refreshed_at": None,
                "age_hours": None,
                "stale": False,
                "bucket_rows": 0,
                "seeded_days": seeded_days_by_station.get(icao, 0),
                "total_n": 0,
                "mean_pred": None,
                "mean_observed": None,
                "last_actual_date": last_iso,
                "actual_fresh": last_iso in (today_utc_iso, yday_utc_iso),
            }
    return result

_ACTIVE_RUNTIME_STATUSES = frozenset({"DRY_RUN", "LIVE"})

def _normalize_skip_reason(
    skip_reason: str | None,
    resolution_source: str | None,
) -> str | None:
    """Map legacy skip reasons to operator-facing labels."""
    reason = (skip_reason or "").strip().lower()
    if not reason:
        return None
    if reason == "low_bss":
        if supports_live_resolution_source(resolution_source):
            return "legacy pre-LUT filter"
        return "unsupported source"
    if reason == "unknown_source":
        return "unsupported source"
    if reason == "low_coverage":
        return "low coverage"
    if reason == "insufficient_lut_data":
        return "waiting for LUT inputs"
    if reason == "train_failed":
        return "training failed"
    if reason == "lut_seed_failed":
        return "LUT seed failed"
    if reason == "geocode_failed":
        return "geocode failed"
    if reason == "consecutive_failures":
        return "consecutive actuals failures"
    return reason.replace("_", " ")

def _load_dashboard_station_rows(conn) -> dict[str, dict[str, object]]:
    """Return all enrolled stations for dashboard display."""
    try:
        rows = conn.execute(
            "SELECT * FROM enrolled_stations ORDER BY city, icao"
        ).fetchall()
    except Exception:
        rows = []

    if rows:
        out: dict[str, dict[str, object]] = {}
        for row in rows:
            item = dict(row)
            item["status"] = (item.get("status") or "DISCOVERED").upper()
            item["runtime_active"] = item["status"] in _ACTIVE_RUNTIME_STATUSES
            item["status_note"] = _normalize_skip_reason(
                item.get("skip_reason"),
                item.get("resolution_source"),
            )
            out[item["icao"]] = item
        return out

    from hightempbot.stations import get_all_stations

    fallback = get_all_stations(conn)
    return {
        icao: {
            "icao": icao,
            "city": cfg.city,
            "lat": getattr(cfg, "lat", 0.0),
            "lon": getattr(cfg, "lon", 0.0),
            "timezone": getattr(cfg, "timezone", "UTC"),
            "unit": getattr(cfg, "unit", "C"),
            "resolution_source": getattr(cfg, "resolution_source", ""),
            "poly_slug": getattr(cfg, "poly_slug", ""),
            "status": "LIVE",
            "runtime_active": True,
            "status_note": None,
        }
        for icao, cfg in fallback.items()
    }

def _dashboard_station_configs(
    station_rows: dict[str, dict[str, object]],
) -> dict[str, StationConfig]:
    """Build StationConfig objects for all dashboard-visible stations."""
    configs: dict[str, StationConfig] = {}
    for icao, row in station_rows.items():
        try:
            configs[icao] = StationConfig(
                icao=icao,
                city=str(row.get("city") or icao),
                lat=float(row.get("lat") or 0.0),
                lon=float(row.get("lon") or 0.0),
                timezone=str(row.get("timezone") or "UTC"),
                unit=str(row.get("unit") or "C"),
                resolution_source=str(row.get("resolution_source") or ""),
                poly_slug=str(row.get("poly_slug") or ""),
            )
        except (TypeError, ValueError):
            continue
    return configs

def _lut_is_bettable(lut_info: dict[str, object] | None) -> bool:
    """Return whether a station's LUT exists and is fresh enough to trade."""
    if not lut_info:
        return False
    if lut_info.get("refreshed_at") is None:
        return False
    return not bool(lut_info.get("stale"))

def _eligibility_funnel(
    all_st: dict[str, StationConfig],
    coverage_map: dict[str, float],
    lut_map: dict[str, dict[str, object]] | None = None,
    active_ids: set[str] | None = None,
) -> dict[str, object]:
    """Return enrolled -> source -> coverage -> active -> bettable counts."""
    from hightempbot.execution.strategy_constants import MIN_COVERAGE_PCT

    available_ids = list(all_st.keys())
    active_ids_set = set(active_ids) if active_ids is not None else set(available_ids)
    source_eligible_ids = [
        icao for icao, cfg in all_st.items()
        if supports_live_resolution_source(getattr(cfg, "resolution_source", None))
    ]
    coverage_eligible_ids = [
        icao for icao in source_eligible_ids
        if coverage_map.get(icao, 0.0) >= MIN_COVERAGE_PCT
    ]
    source_active_ids = [
        icao for icao in coverage_eligible_ids
        if icao in active_ids_set
    ]
    bettable_ids = [
        icao for icao in source_active_ids
        if _lut_is_bettable((lut_map or {}).get(icao))
    ]
    return {
        "available_ids": available_ids,
        "source_eligible_ids": source_eligible_ids,
        "coverage_eligible_ids": coverage_eligible_ids,
        "bss_eligible_ids": coverage_eligible_ids,  # back-compat alias
        "available_count": len(available_ids),
        "source_eligible_count": len(source_eligible_ids),
        "coverage_eligible_count": len(coverage_eligible_ids),
        "bss_eligible_count": len(coverage_eligible_ids),  # back-compat alias
        "active_ids": sorted(active_ids_set),
        "runtime_active_count": len(active_ids_set),
        "funnel_active_ids": source_active_ids,
        "active_count": len(source_active_ids),
        "bettable_ids": bettable_ids,
        "bettable_count": len(bettable_ids),
    }

def _enrich_ledger_positions(
    rows: list[sqlite3.Row],
    all_st: dict[str, StationConfig],
) -> list[dict[str, object]]:
    """Add bracket bounds, display labels, and display-unit actuals to ledger rows."""

    def _is_half_step(val: float | None) -> bool:
        return val is not None and abs((val % 1.0) - 0.5) < 1e-6

    def _display_bucket_label(
        blo: float | None,
        bhi: float | None,
        unit_sym: str,
    ) -> str | None:
        def _display_low(val: float) -> int:
            return int(round(val + 0.5)) if _is_half_step(val) else int(round(val))

        def _display_high(val: float) -> int:
            return int(round(val - 0.5)) if _is_half_step(val) else int(round(val))

        if blo is None and bhi is not None:
            return f"<{_display_high(bhi)}{unit_sym}"
        if bhi is None and blo is not None:
            return f"\u2265{_display_low(blo)}{unit_sym}"
        if blo is not None and bhi is not None:
            lo_label = _display_low(blo)
            hi_label = _display_high(bhi)
            if lo_label == hi_label:
                return f"{lo_label}{unit_sym}"
            return f"{lo_label}-{hi_label}{unit_sym}"
        return None

    result: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        cfg = all_st.get(item.get("station_id", ""))
        is_resolved = item.get("outcome") not in (None, "PENDING")
        label = None
        detail = decode_event_detail(item.get("event_detail"))
        if detail:
            blo = detail.get("bracket_low")
            bhi = detail.get("bracket_high")
            unit_sym = "°F" if cfg and cfg.unit == "F" else "°C"
            label = _display_bucket_label(blo, bhi, unit_sym)
            item["bracket_low"] = blo
            item["bracket_high"] = bhi

        # Rows without bounds (e.g. RECOVERED) use the stored label.
        detail_bracket_label = detail.get("bracket_label") if isinstance(detail, dict) else None
        if not label and detail_bracket_label:
            display_label = detail_bracket_label
        else:
            display_label = label or f"{item.get('threshold', '')}°"
        item["bracket_label"] = _clean_display_text(display_label)
        # "RECOVERED" orphan rows need operator action; "normal" rows are real bets.
        if (
            isinstance(detail, dict)
            and detail.get("recovered_orphan")
        ) or (item.get("station_id") == "RECOVERED"):
            item["bracket_kind"] = "RECOVERED"
        else:
            item["bracket_kind"] = "normal"
        resolution_source = detail.get("resolution_source")
        resolution_label = detail.get("resolution_actual_label")
        if resolution_source == "polymarket_data_api_redeemable" and not (
            detail.get("resolution_label_backfilled_at")
            or detail.get("resolution_bracket_low") is not None
            or detail.get("resolution_bracket_high") is not None
        ):
            resolution_label = None
        if (
            detail.get("resolution_actual_source") == "actuals"
            or resolution_source == "actuals"
        ):
            resolution_label = None
        item["actual_label"] = _clean_display_text(resolution_label)
        item.setdefault("bracket_low", None)
        item.setdefault("bracket_high", None)

        if item.get("actual_tmax") is not None and cfg and cfg.unit == "F":
            item["actual_display"] = round(celsius_to_fahrenheit(item["actual_tmax"]))
            item["actual_unit"] = "F"
        elif item.get("actual_tmax") is not None:
            item["actual_display"] = item.get("actual_tmax")
            item["actual_unit"] = "C"
        else:
            item["actual_display"] = None
            item["actual_unit"] = "C"

        item["actual_from_bracket"] = False
        if item.get("actual_tmax") is not None:
            # Compare against the winning bracket, not the bet's own.
            res_low = detail.get("resolution_bracket_low")
            res_high = detail.get("resolution_bracket_high")
            if is_resolved and (res_low is not None or res_high is not None):
                blo_d = res_low
                bhi_d = res_high
            else:
                blo_d = item.get("bracket_low")
                bhi_d = item.get("bracket_high")
            if blo_d is not None and bhi_d is not None:
                mid_d = (blo_d + bhi_d) / 2.0
            elif blo_d is not None:
                mid_d = blo_d
            elif bhi_d is not None:
                mid_d = bhi_d
            else:
                mid_d = None
            if mid_d is not None:
                mid_c = fahrenheit_to_celsius(mid_d) if cfg and cfg.unit == "F" else mid_d
                if abs(item["actual_tmax"] - mid_c) < 0.01:
                    item["actual_from_bracket"] = True

        result.append(item)

    return result

app = FastAPI(title="HighTempBot Dashboard", docs_url=None, redoc_url=None)
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

logger = logging.getLogger(__name__)

# Module globals set at startup via configure()
_db_path: str = "data/hightempbot.db"
_dry_run: bool = True
_initial_bankroll: float = 100.0
_dashboard_user: str = ""
_dashboard_pass: str = ""
_dashboard_tls_terminated: bool = False
_auth_token: str = ""  # generated at startup

# /login rate limit: 5 failures in 60s locks the client IP out for 5 minutes.
# Keyed on request.client.host; X-Forwarded-For is ignored so it can't be spoofed.
import threading as _threading

_LOGIN_WINDOW_SECONDS: float = 60.0
_LOGIN_MAX_ATTEMPTS: int = 5
_LOGIN_LOCKOUT_SECONDS: float = 300.0
_LOGIN_MAX_TRACKED_IPS: int = 4096
_login_attempts_lock = _threading.Lock()
_login_attempts: dict[str, dict[str, float]] = {}


def _login_rate_limited(client_ip: str, *, now: float | None = None) -> bool:
    """True if ``client_ip`` is locked out of /login. Also trims expired entries."""
    import time as _time

    ts = now if now is not None else _time.monotonic()
    with _login_attempts_lock:
        entry = _login_attempts.get(client_ip)
        if entry is None:
            return False
        if entry.get("locked_until", 0.0) > ts:
            return True
        if (ts - entry.get("first_ts", 0.0)) > _LOGIN_WINDOW_SECONDS and entry.get("locked_until", 0.0) <= ts:
            _login_attempts.pop(client_ip, None)
        return False


def _record_login_failure(client_ip: str, *, now: float | None = None) -> None:
    """Bump the failed-attempt counter for `client_ip`; lock at the threshold."""
    import time as _time

    ts = now if now is not None else _time.monotonic()
    with _login_attempts_lock:
        if len(_login_attempts) >= _LOGIN_MAX_TRACKED_IPS:
            for ip, ent in list(_login_attempts.items()):
                if ent.get("locked_until", 0.0) <= ts and (ts - ent.get("first_ts", 0.0)) > _LOGIN_WINDOW_SECONDS:
                    _login_attempts.pop(ip, None)
        entry = _login_attempts.get(client_ip)
        if entry is None or (ts - entry.get("first_ts", 0.0)) > _LOGIN_WINDOW_SECONDS:
            _login_attempts[client_ip] = {"first_ts": ts, "count": 1.0, "locked_until": 0.0}
            return
        entry["count"] = entry.get("count", 0.0) + 1.0
        if entry["count"] >= _LOGIN_MAX_ATTEMPTS:
            entry["locked_until"] = ts + _LOGIN_LOCKOUT_SECONDS


def _clear_login_attempts(client_ip: str) -> None:
    """Drop the failed-attempt bucket for `client_ip` on a successful login."""
    with _login_attempts_lock:
        _login_attempts.pop(client_ip, None)


def _reset_login_rate_limit_state() -> None:
    """Test hook: clear all tracked IPs. Not used in production."""
    with _login_attempts_lock:
        _login_attempts.clear()

def configure(
    db_path: str,
    dry_run: bool = True,
    initial_bankroll: float = 100.0,
    dashboard_user: str = "",
    dashboard_pass: str = "",
    dashboard_tls_terminated: bool = False,
) -> None:
    """Set dashboard configuration at startup."""
    global _db_path, _dry_run, _initial_bankroll, _dashboard_user, _dashboard_pass, _dashboard_tls_terminated, _auth_token
    _db_path = db_path
    _dry_run = dry_run
    _initial_bankroll = initial_bankroll
    _dashboard_user = dashboard_user
    _dashboard_pass = dashboard_pass
    _dashboard_tls_terminated = bool(dashboard_tls_terminated)
    if dashboard_pass:
        _auth_token = secrets.token_hex(32)

def _check_auth(request: Request):
    """Cookie auth when DASHBOARD_PASS is set; otherwise open."""
    if not _dashboard_pass:
        return
    token = request.cookies.get("htb_session")
    if token and secrets.compare_digest(token, _auth_token):
        return
    # XHR gets 401; page loads redirect to /login.
    if request.headers.get("HX-Request"):
        raise HTTPException(status_code=401, headers={"HX-Redirect": "/login"})
    raise HTTPException(status_code=307, headers={"Location": "/login"})

def _compute_health_status() -> dict[str, object]:
    """Health status with metrics (shared by /health and the admin endpoint)."""
    import sqlite3 as _sql
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    conn = _sql.connect(_db_path)
    conn.row_factory = _sql.Row
    try:
        last_scan = conn.execute(
            "SELECT MAX(created_at) as t FROM pipeline_health WHERE stage = 'scan' AND status = 'OK'"
        ).fetchone()["t"]
        cutoff = (_dt.now(_tz.utc) - _td(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
        scan_ok = last_scan is not None and last_scan >= cutoff

        from hightempbot.persistence.pipeline_health import (
            forecast_activity_counts,
            forecast_stall_detected,
        )

        forecast_counts = forecast_activity_counts(conn)
        fc_count = forecast_counts["forecast_ok"]

        err_count = conn.execute(
            "SELECT COUNT(*) as n FROM pipeline_health "
            "WHERE status = 'ERROR' AND created_at >= datetime('now', '-15 minutes')"
        ).fetchone()["n"]

        if not scan_ok or forecast_stall_detected(forecast_counts):
            status = "down"
        elif err_count > 5:
            status = "degraded"
        else:
            status = "ok"

        return {
            "status": status,
            "last_scan": last_scan,
            "forecasts_2h": fc_count,
            "forecast_attempts_2h": forecast_counts["forecast_attempts"],
            "forecast_upstream_ok_2h": forecast_counts["forecast_upstream_ok"],
            "errors_15m": err_count,
        }
    except Exception as e:
        return {"status": "down", "error": str(e)}
    finally:
        conn.close()


@app.get("/health")
async def health_check():
    """Unauthenticated liveness probe; metrics are only on the admin endpoint."""
    return {"status": _compute_health_status().get("status", "down")}


@app.get("/api/v2/admin/health", dependencies=[Depends(_check_auth)])
async def admin_health_check():
    """Auth-gated detailed health for the dashboard."""
    return _compute_health_status()

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if not _dashboard_pass:
        return RedirectResponse("/")
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Login — HighTempBot</title>
<link rel="stylesheet" href="/static/v2/styles.css">
<style>
.login-box {{ max-width:320px; margin:120px auto; padding:32px; background:var(--card-raised); border-radius:var(--radius); border:1px solid var(--border); }}
.login-box h2 {{ margin:0 0 20px; font-size:1.3em; }}
.login-box input {{ width:100%; padding:8px 12px; margin:6px 0 14px; border:1px solid var(--border); border-radius:6px; font-size:0.95em; background:var(--bg); color:var(--fg1); box-sizing:border-box; }}
.login-box button {{ width:100%; padding:10px; background:var(--fg1); color:var(--bg-surface); border:none; border-radius:6px; font-size:0.95em; font-weight:600; cursor:pointer; }}
.login-err {{ color:var(--red); font-size:0.85em; margin-bottom:10px; }}
</style></head><body>
<div class="login-box">
<h2>HighTempBot</h2>
{"<div class='login-err'>Wrong username or password</div>" if error else ""}
<form method="POST" action="/login">
<input name="username" placeholder="Username" autocomplete="username" required>
<input name="password" type="password" placeholder="Password" autocomplete="current-password" required>
<button type="submit">Login</button>
</form></div></body></html>""")

@app.post("/login")
async def login_submit(request: Request):
    client_ip = (request.client.host if request.client else "") or "unknown"
    if _login_rate_limited(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts; try again in a few minutes.",
            headers={"Retry-After": str(int(_LOGIN_LOCKOUT_SECONDS))},
        )

    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    if secrets.compare_digest(username, _dashboard_user) and secrets.compare_digest(password, _dashboard_pass):
        _clear_login_attempts(client_ip)
        response = RedirectResponse("/", status_code=303)
        # Secure only under HTTPS; plain-HTTP deploys need a tunnel or VPN.
        response.set_cookie(
            "htb_session",
            _auth_token,
            httponly=True,
            max_age=86400 * 7,
            samesite="strict",
            secure=request.url.scheme == "https" or _dashboard_tls_terminated,
        )
        return response
    _record_login_failure(client_ip)
    return RedirectResponse("/login?error=1", status_code=303)

def _conn() -> sqlite3.Connection:
    """Get a DB connection with WAL mode. Caller must close."""
    return get_connection(_db_path)

def _active_target_date(cfg, conn=None) -> str:
    """Return the same current-UTC market date used by the scanner."""
    del cfg
    del conn
    return _dt.now(_tz.utc).date().isoformat()

# Boot time for uptime display
from datetime import timezone as _tz_utc
_boot_time = _dt.now(_tz_utc.utc)


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(_check_auth)])
async def index() -> RedirectResponse:
    """Root → v2 trading-journal dashboard."""
    return RedirectResponse(url="/v2", status_code=302)


# --- v2 SPA at /v2, fed by /api/v2/data ---

@app.get("/v2", response_class=HTMLResponse, dependencies=[Depends(_check_auth)])
async def v2_index():
    """Serve the SPA shell with ``?v=<mtime>`` cache-busting on its assets."""
    from fastapi.responses import Response as _Resp

    v2_dir = _STATIC_DIR / "v2"
    try:
        latest_mtime = int(max(
            (p.stat().st_mtime for p in v2_dir.glob("*.jsx")),
            default=0,
        ))
        css_mtime = int((v2_dir / "styles.css").stat().st_mtime)
        latest_mtime = max(latest_mtime, css_mtime)
    except OSError:
        latest_mtime = 0

    html = (v2_dir / "index.html").read_text(encoding="utf-8")
    # Inject ?v=<mtime> on every same-origin .jsx and styles.css reference.
    html = html.replace('"/static/v2/styles.css"', f'"/static/v2/styles.css?v={latest_mtime}"')
    for jsx in ("Charts.jsx", "Shell.jsx", "Overview.jsx", "Pages2.jsx", "Pages3.jsx", "Calendar.jsx", "Operator.jsx", "Strategy.jsx"):
        html = html.replace(f'"/static/v2/{jsx}"', f'"/static/v2/{jsx}?v={latest_mtime}"')
    return _Resp(content=html, media_type="text/html", headers={"Cache-Control": "no-store"})


def _degraded_v2_payload(exc: Exception) -> dict:
    """Minimal dashboard envelope used when a sub-aggregation fails."""
    ratios = {
        "profitFactor": 0.0,
        "sharpe": 0.0,
        "sortino": 0.0,
        "calmar": 0.0,
        "expectancy": 0.0,
        "kellyFrac": 0.0,
    }
    return {
        "error": "Dashboard data unavailable",
        "errorDetail": str(exc),
        "capital": _initial_bankroll,
        "initialBankroll": _initial_bankroll,
        "accountPnl": 0.0,
        "accountPnlPct": 0.0,
        "totalPnl": 0.0,
        "realizedPnl": 0.0,
        "resolvedCount": 0,
        "feesPaid": 0.0,
        "winRate": 0.0,
        "wins": 0,
        "losses": 0,
        "ddPct": 0.0,
        "ddHaltThreshold": int(round(MAX_DD * 100)),
        "reducedSizeThreshold": 100,
        "todayBets": 0,
        "todayVolume": 0.0,
        "todaySignals": 0,
        "wouldBetSignals": 0,
        "pendingExposure": 0.0,
        "openPositions": 0,
        "openPositionsList": [],
        "resolvedPositionsList": [],
        "stations": [],
        "funnel": {
            "available_count": 0,
            "source_eligible_count": 0,
            "coverage_eligible_count": 0,
            "active_count": 0,
            "bettable_count": 0,
        },
        "performanceByStation": [],
        "stationSparks": {},
        "ensembleByStation": {},
        "calibration": [],
        "calibrationByStation": {},
        "calendar": {},
        "equityCurve": [],
        "tradingEquityCurve": [],
        "accountEquityCurve": [],
        "withdrawalEvents": [],
        "returnTransferOutflow": 0.0,
        "weeklyPnl": [],
        "pnlDist": [],
        "streaks": {"current": "-", "currentTone": "neu", "longestW": 0, "longestL": 0, "last20": []},
        "ratios": ratios,
        "strategies": [],
        "ymidExits": {
            "tp": {"fire_rate": 0.0, "n": 0, "avg_pnl": 0.0},
            "sl": {"fire_rate": 0.0, "n": 0, "avg_pnl": 0.0},
        },
        "targetDate": _active_target_date(None, None),
        "lastScanAgo": "-",
        "uptime": "-",
        "mode": "DRY-RUN" if _dry_run else "LIVE",
        # Fill every key with a sentinel so the UI needn't null-check.
        "operator": {
            "state": None,
            "bootDryRun": _dry_run,
            "reason": "",
            "updatedBy": "",
            "updatedAt": "",
            "processingEnabled": False,
            "transferLocked": False,
            "events": [],
        },
        "wallet": {
            "primaryWallet": "",
            "primaryWalletLabel": "",
            "source": "POLY_FUNDER",
            "snapshot": None,
            "fresh": _dry_run,
            "warnings": ["Dashboard data degraded."],
            "actionsEnabled": False,
            "transferEligible": False,
            "returnWallet": "",
            "returnWalletConfigured": False,
            "readOnlyReason": "Dashboard data degraded.",
        },
        "readiness": {
            "status": "SKIPPED" if _dry_run else "UNKNOWN",
            "mode": "DRY-RUN" if _dry_run else "LIVE",
            "checks": [],
        },
        "liveActionsEnabled": False,
    }


_RANGE_TO_DAYS = {"7d": 7, "30d": 30, "90d": 90}


def _build_v2_payload(
    conn: sqlite3.Connection, *, range_days: int | None = None,
) -> dict:
    """Build HTB_DATA with the same degraded fallback for JSON and data.js."""
    from hightempbot.dashboard import v2_data as _v2
    from hightempbot.execution.strategy_constants import MIN_COVERAGE_PCT, REF_START_DATE

    try:
        payload = _v2.build_htb_data(
            conn,
            initial_bankroll=_initial_bankroll,
            dry_run=_dry_run,
            boot_time=_boot_time,
            cutoff_utc=_session_floor(),
            session_baseline_capital=_session_baseline_capital,
            dashboard_peak_capital=_dashboard_peak_capital,
            dashboard_realized_capital=_dashboard_realized_capital,
            sum_polymarket_fees=_sum_polymarket_fees,
            coverage_by_station=_coverage_by_station,
            lut_by_station=_lut_by_station,
            load_dashboard_station_rows=_load_dashboard_station_rows,
            dashboard_station_configs=_dashboard_station_configs,
            eligibility_funnel=_eligibility_funnel,
            enrich_ledger_positions=_enrich_ledger_positions,
            ref_start_date=REF_START_DATE,
            min_coverage_pct=MIN_COVERAGE_PCT,
            active_target_date=_active_target_date(None, conn),
            range_days=range_days,
        )
        try:
            from hightempbot.dashboard.wallet_data import build_operator_wallet_payload

            payload.update(build_operator_wallet_payload(conn, dry_run=_dry_run))
        except Exception:
            logger.warning("operator/wallet dashboard payload failed", exc_info=True)
            degraded = _degraded_v2_payload(RuntimeError("operator payload unavailable"))
            for key in ("operator", "wallet", "readiness", "liveActionsEnabled"):
                payload[key] = degraded[key]
        return payload
    except Exception as exc:
        logger.exception("build_htb_data failed; returning degraded payload")
        return _degraded_v2_payload(exc)


@app.get("/api/v2/data.js", dependencies=[Depends(_check_auth)])
def v2_data_js():
    """Payload as ``window.HTB_DATA = {...}``, loaded before the Babel-compiled JSX."""
    import json as _json

    from fastapi.responses import Response

    conn = _conn()
    try:
        payload = _build_v2_payload(conn)
    finally:
        conn.close()
    body = "window.HTB_DATA = " + _json.dumps(payload, default=str) + ";\n"
    return Response(content=body, media_type="application/javascript", headers={"Cache-Control": "no-store"})


@app.get("/api/v2/data", dependencies=[Depends(_check_auth)])
def v2_data(
    range: str | None = None,
    month: str | None = None,
    station: str | None = None,
) -> JSONResponse:
    """HTB_DATA for the SPA. Filters: ``range`` (7d/30d/90d/all, also scopes
    KPIs), ``month`` (YYYY-MM calendar), ``station`` (ICAO). On failure returns
    a degraded payload with an ``error`` field instead of a 500."""
    conn = _conn()
    try:
        range_days = _RANGE_TO_DAYS.get(range or "")
        payload = _build_v2_payload(conn, range_days=range_days)
        payload = _filter_v2_payload(payload, range, month, station)
        return JSONResponse(payload)
    finally:
        conn.close()


# --- Operator controls ------------------------------------------------------

async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return body


def _assert_fresh_live_action_context(
    conn: sqlite3.Connection,
    *,
    require_no_exposure: bool = False,
    freshness_ttl_s_override: int | None = None,
) -> None:
    from hightempbot.execution.live_action_guard import (
        LiveActionSafetyError,
        assert_fresh_live_action_context,
    )
    from hightempbot.runtime_config import get_config

    try:
        assert_fresh_live_action_context(
            conn,
            config=get_config(),
            dry_run=_dry_run,
            require_no_exposure=require_no_exposure,
            freshness_ttl_s_override=freshness_ttl_s_override,
            dry_run_message="Process booted DRY_RUN=True; dashboard cannot enable live-money actions.",
        )
    except LiveActionSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _admin_actor(request: Request) -> str:
    """Audit actor from the X-Operator-Actor header (default 'dashboard-admin'),
    clipped to 64 chars without control characters."""
    raw = (request.headers.get("X-Operator-Actor") or "").strip()
    if not raw:
        return "dashboard-admin"
    cleaned = "".join(ch for ch in raw if ch.isprintable())[:64]
    return cleaned or "dashboard-admin"


@app.get("/api/v2/admin/operator/status", dependencies=[Depends(_check_auth)])
async def admin_operator_status() -> JSONResponse:
    from hightempbot.dashboard.wallet_data import build_operator_wallet_payload

    conn = _conn()
    try:
        return JSONResponse(build_operator_wallet_payload(conn, dry_run=_dry_run))
    finally:
        conn.close()


@app.post("/api/v2/admin/operator/stop", dependencies=[Depends(_check_auth)])
async def admin_operator_stop(request: Request) -> JSONResponse:
    from hightempbot.execution.operator_control import stop_processing

    body = await _json_body(request)
    conn = _conn()
    try:
        state = stop_processing(
            conn,
            actor=_admin_actor(request),
            reason=str(body.get("reason") or "")[:500],
            boot_dry_run=_dry_run,
        )
        return JSONResponse({"ok": True, "operator": state.to_public_dict()})
    finally:
        conn.close()


@app.post("/api/v2/admin/operator/start", dependencies=[Depends(_check_auth)])
async def admin_operator_start(request: Request) -> JSONResponse:
    from hightempbot.execution.operator_control import OperatorControlError, start_processing

    body = await _json_body(request)
    conn = _conn()
    try:
        _assert_fresh_live_action_context(conn)
        try:
            state = start_processing(
                conn,
                actor=_admin_actor(request),
                reason=str(body.get("reason") or "")[:500],
                boot_dry_run=_dry_run,
            )
        except OperatorControlError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "operator": state.to_public_dict()})
    finally:
        conn.close()


@app.post("/api/v2/admin/operator/transfer/preview", dependencies=[Depends(_check_auth)])
async def admin_transfer_preview(request: Request) -> JSONResponse:
    """Read-only transfer preview. A blocked transfer is still a 200 with
    ``{"ok": false, "preview": {...}}`` so the UI can show why."""
    from hightempbot.execution.polymarket_transfer import preview_return_transfer
    from hightempbot.persistence.wallet_reconciliation import refresh_wallet_snapshot
    from hightempbot.runtime_config import get_config

    body = await _json_body(request)
    conn = _conn()
    try:
        cfg = get_config()
        if not _dry_run:
            try:
                refresh_wallet_snapshot(conn, config=cfg)
            except Exception:
                logger.warning("wallet snapshot refresh failed before transfer preview", exc_info=True)
        preview = preview_return_transfer(
            conn,
            config=cfg,
            amount=body.get("amount", ""),
            to_wallet=body.get("to_wallet"),
            dry_run=_dry_run,
        )
        return JSONResponse({"ok": preview.ok, "preview": preview.to_dict()})
    finally:
        conn.close()


@app.post("/api/v2/admin/operator/transfer/lock", dependencies=[Depends(_check_auth)])
async def admin_transfer_lock(request: Request) -> JSONResponse:
    from hightempbot.execution.operator_control import OperatorControlError, enter_transfer_lock

    body = await _json_body(request)
    conn = _conn()
    try:
        _assert_fresh_live_action_context(conn, require_no_exposure=True)
        try:
            state = enter_transfer_lock(
                conn,
                actor=_admin_actor(request),
                reason=str(body.get("reason") or "transfer requested")[:500],
                boot_dry_run=_dry_run,
            )
        except OperatorControlError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "operator": state.to_public_dict()})
    finally:
        conn.close()


@app.post("/api/v2/admin/operator/transfer/submit", dependencies=[Depends(_check_auth)])
async def admin_transfer_submit(request: Request) -> JSONResponse:
    from hightempbot.execution.polymarket_transfer import (
        SUBMIT_FRESHNESS_TTL_S,
        TransferSafetyError,
        submit_return_transfer,
    )
    from hightempbot.runtime_config import get_config

    body = await _json_body(request)
    conn = _conn()
    try:
        _assert_fresh_live_action_context(
            conn,
            require_no_exposure=True,
            freshness_ttl_s_override=SUBMIT_FRESHNESS_TTL_S,
        )
        try:
            result = submit_return_transfer(
                conn,
                config=get_config(),
                amount=body.get("amount", ""),
                to_wallet=body.get("to_wallet"),
                confirmation=str(body.get("confirmation") or ""),
                actor=_admin_actor(request),
                dry_run=_dry_run,
            )
        except TransferSafetyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "transfer": result})
    finally:
        conn.close()


# --- Admin: HTTP twin of cli/resolve_pending_via_wu (same helpers) ---


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ICAO_RE = re.compile(r"^[A-Z][A-Z0-9]{3}$")


def _parse_admin_target_date(value: str) -> str:
    """Reject non-ISO and future dates so a typo can't silently 0-result."""
    from datetime import date as _date
    if not _ISO_DATE_RE.match(value or ""):
        raise HTTPException(
            status_code=400,
            detail=f"target_date must be ISO YYYY-MM-DD, got {value!r}",
        )
    try:
        parsed = _date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if parsed > _date.today():
        raise HTTPException(
            status_code=400,
            detail=f"target_date {value} is in the future; refusing to settle",
        )
    return value


def _parse_admin_station(value: str | None) -> str | None:
    """Accept 4-char ICAO (uppercased) or None."""
    if value is None or value == "":
        return None
    upper = value.upper()
    if not _ICAO_RE.match(upper):
        raise HTTPException(
            status_code=400,
            detail=f"station must be a 4-char ICAO, got {value!r}",
        )
    return upper


@app.post("/api/v2/admin/resolve-pending", dependencies=[Depends(_check_auth)])
async def admin_resolve_pending(request: Request) -> JSONResponse:
    """Settle PENDING bets from WU actuals.

    JSON body: ``target_date`` (required), ``station``, ``commit`` (false =
    preview), ``force`` (skip the POLYMARKET_FALLBACK_DAYS floor), ``reason``.
    """
    from datetime import date as _date, datetime as _dtm
    import pytz as _pytz
    from hightempbot.execution.strategy_constants import POLYMARKET_FALLBACK_DAYS
    from hightempbot.resolution.settler import (
        _resolve_via_wu_actual_fallback,
        preview_wu_fallback_outcome,
    )
    from hightempbot.persistence.actuals import actual_source_clause
    from hightempbot.stations import (
        get_all_stations,
    )

    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    target_date = _parse_admin_target_date(body.get("target_date") or "")
    station_filter = _parse_admin_station(body.get("station"))
    commit = bool(body.get("commit", False))
    force = bool(body.get("force", False))
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise HTTPException(status_code=400, detail="reason must be a string")
    if reason is not None and len(reason) > 500:
        raise HTTPException(status_code=400, detail="reason must be <= 500 chars")

    conn = _conn()
    try:
        stations = get_all_stations(conn)
        actor = _admin_actor(request)

        sql = (
            "SELECT id, station_id, target_date, side, token_id, bet_size, "
            "fill_price, fill_size, limit_price, event_detail "
            "FROM ledger "
            "WHERE outcome='PENDING' "
            "AND event_type IN ('bet','dry_run') "
            "AND target_date = ?"
        )
        params: list[str] = [target_date]
        if station_filter:
            sql += " AND station_id = ?"
            params.append(station_filter)
        sql += " ORDER BY station_id, id"

        rows = conn.execute(sql, params).fetchall()
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r["station_id"], []).append(r)

        result_groups = []
        total_resolved = 0
        today = _date.today()
        for sid, grp in groups.items():
            station_cfg = stations.get(sid)
            if station_cfg is None:
                result_groups.append({
                    "station_id": sid,
                    "target_date": target_date,
                    "refused": "station not enrolled",
                    "bets": [],
                })
                continue

            actuals_clause, actuals_params = actual_source_clause()
            actual_row = conn.execute(
                f"SELECT tmax_celsius FROM actuals "
                f"WHERE station_id=? AND local_date=? "
                f"AND {actuals_clause}",
                (sid, target_date, *actuals_params),
            ).fetchone()
            if actual_row is None:
                result_groups.append({
                    "station_id": sid,
                    "target_date": target_date,
                    "refused": "no WU actual on file for this (station, date)",
                    "bets": [],
                })
                continue
            actual_tmax = float(actual_row["tmax_celsius"])

            try:
                _tz = _pytz.timezone(getattr(station_cfg, "timezone", "UTC"))
                today_local = _dtm.now(_tz).date()
            except Exception:
                today_local = today
            days_past = (today_local - _date.fromisoformat(target_date)).days

            if commit and days_past < POLYMARKET_FALLBACK_DAYS and not force:
                result_groups.append({
                    "station_id": sid,
                    "target_date": target_date,
                    "days_past": days_past,
                    "refused": (
                        f"days_past={days_past} < "
                        f"POLYMARKET_FALLBACK_DAYS={POLYMARKET_FALLBACK_DAYS}; "
                        f"pass force=true to override"
                    ),
                    "bets": [],
                })
                continue

            previews = [
                preview_wu_fallback_outcome(r, actual_tmax, station_cfg, days_past)
                for r in grp
            ]

            group_result = {
                "station_id": sid,
                "target_date": target_date,
                "days_past": days_past,
                "actual_tmax_c": actual_tmax,
                "bets": previews,
            }

            if commit:
                resolved = _resolve_via_wu_actual_fallback(
                    conn, sid, target_date, grp, actual_tmax, station_cfg,
                    days_past=days_past,
                )
                if resolved > 0:
                    try:
                        resolved_ids = [int(r["id"]) for r in grp]
                        placeholders = ",".join("?" * len(resolved_ids))
                        conn.execute(
                            "UPDATE ledger SET event_detail = json_set("
                            "COALESCE(event_detail, '{}'), "
                            "'$.manual_resolution_reason', ?, "
                            "'$.manual_resolution_actor', ?"
                            ") "
                            f"WHERE id IN ({placeholders}) "
                            "AND json_extract(event_detail, '$.resolution_source')"
                            "='wu_actual_fallback'",
                            (reason or "", actor, *resolved_ids),
                        )
                        conn.commit()
                    except sqlite3.OperationalError:
                        logging.getLogger(__name__).warning(
                            "admin_resolve_pending: failed to stamp manual_resolution_reason",
                            exc_info=True,
                        )
                group_result["resolved_count"] = resolved
                total_resolved += resolved

            result_groups.append(group_result)

        return JSONResponse({
            "mode": "commit" if commit else "dry_run",
            "target_date": target_date,
            "station_filter": station_filter,
            "force": force,
            "total_resolved": total_resolved,
            "groups": result_groups,
        })
    finally:
        conn.close()
