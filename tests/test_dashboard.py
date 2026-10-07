"""Smoke tests for the v2 trading-journal dashboard.

The legacy operator's-terminal dashboard (live.html + /partials/* HTMX) was
deleted in the 2026-05-06 v2 cutover. The 45 tests that targeted those routes
were removed alongside their behaviour. The v2 dashboard is a React+Babel SPA
fed by /api/v2/data; we test the JSON shape directly instead of rendered HTML.

Coverage:
- /, /v2, /api/v2/data, /health respond 200 or expected redirect
- /api/v2/data returns the documented HTB_DATA shape with the right top-level
  keys, including /strategies (NO/YMID/TAIL) and /halt-related fields
- Auth still works on the v2 routes (cookie required when password set)
- The 3-strategy halt thresholds match the strategy spec
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest


def _utc_today() -> date:
    """Mirror dashboard `_active_target_date()` — UTC date, not local.

    SGT (UTC+8) tests using `date.today()` diverge from production for
    ~8h/day, which surfaced as `test_grouped_trade_counts_and_target_size`
    failing during early SGT mornings (local 2026-05-22 vs UTC 2026-05-21).
    Pinning every test-seeded `target_date` to UTC eliminates the
    time-of-day flake.
    """
    return datetime.now(timezone.utc).date()
from fastapi.testclient import TestClient

from hightempbot.dashboard.app import (
    _clean_display_text,
    _compute_health_status,
    _enrich_ledger_positions,
    app,
    configure,
)
from hightempbot.dashboard.v2_data import (
    _open_positions_for_v2,
    _resolved_positions_for_v2,
)
from hightempbot.db.connection import get_connection, init_db
from hightempbot.execution.strategy_constants import (
    MIN_BET_USD,
    POLY_FEE_THETA,
    REF_START_DATE,
    SCAN_INTERVAL_MINUTES,
    STRATEGY_CONFIGS,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    from hightempbot.runtime_config import Config, set_config

    set_config(Config(_env_file=None, dry_run=True, poly_funder=""))
    monkeypatch.setattr(
        "hightempbot.dashboard.app.DASHBOARD_SESSION_START_UTC",
        "2026-05-08 00:00:00",
    )
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    configure(str(db_path), dry_run=True, initial_bankroll=1000.0)
    return db_path


@pytest.fixture
def client(db):
    return TestClient(app)


def _insert_enrolled_station(
    db_path,
    icao: str,
    city: str,
    *,
    timezone: str = "UTC",
    unit: str = "C",
    resolution_source: str = "wu",
    status: str = "LIVE",
):
    conn = get_connection(str(db_path))
    try:
        conn.execute(
            """INSERT INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source,
             poly_slug, status, skip_reason)
            VALUES (?, ?, 0.0, 0.0, ?, ?, ?, ?, ?, ?, NULL)""",
            (icao, city, timezone, unit, resolution_source, resolution_source,
             city.lower().replace(" ", "-"), status),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_pipeline_health(db_path, station_id: str, stage: str, status: str, message: str):
    conn = get_connection(str(db_path))
    try:
        conn.execute(
            "INSERT INTO pipeline_health (station_id, stage, status, message) "
            "VALUES (?, ?, ?, ?)",
            (station_id, stage, status, message),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_ledger_row(
    db_path,
    outcome: str,
    *,
    station_id: str = "KDAL",
    pnl: float | None = 0.0,
    side: str = "NO",
    bet_size: float = 10.0,
    bet_ts: str = "2026-05-08 12:00:00",
    target_date: str = "2026-05-08",
    event_type: str = "bet",
    strategy: str | None = None,
):
    """Insert a minimal ledger row for testing v2_data aggregations.

    ``bet_ts`` and ``target_date`` default to "today" (2026-05-08) so the
    dashboard's ``target_date = active_target_date`` filter (today KPIs) and
    its ``bet_ts >= session_floor`` filter (cumulative views) both include
    the row by default. Tests that need pre-cutoff or off-target rows can
    override either kwarg explicitly.
    """
    import json
    detail = {"bracket_low": 17.0, "bracket_high": 21.0}
    if strategy:
        detail["strategy"] = strategy
    conn = get_connection(str(db_path))
    try:
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, event_type, pnl, event_detail)
            VALUES (?, ?, 'm', 't', ?,
             1, 21.0, ?, 0.45, 0.30, 0.15,
             10.0, 5000.0, ?, 0.30, ?, ?, ?, ?)""",
            (
                bet_ts,
                station_id,
                target_date,
                side,
                bet_size,
                outcome,
                event_type,
                pnl,
                json.dumps(detail),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_full_actual_coverage(
    db_path,
    station_id: str,
    *,
    source: str = "wu",
):
    conn = get_connection(str(db_path))
    try:
        d = date.fromisoformat(REF_START_DATE)
        today = date.today()
        rows = []
        while d <= today:
            rows.append((station_id, d.isoformat(), 20.0, source))
            d += timedelta(days=1)
        conn.executemany(
            """INSERT OR REPLACE INTO actuals
            (station_id, local_date, tmax_celsius, source)
            VALUES (?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------- root + redirects

class TestRoot:
    def test_root_redirects_to_v2(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/v2"

    def test_root_follows_to_v2(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_health_is_public(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_unauth_strips_metrics(self, client):
        """ce-code-review P2 #35: unauth /health must not leak pipeline metrics."""
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        # Metrics live behind the auth-gated admin endpoint now.
        for forbidden in (
            "last_scan",
            "forecasts_2h",
            "forecast_attempts_2h",
            "forecast_upstream_ok_2h",
            "errors_15m",
        ):
            assert forbidden not in body, f"unauth /health leaked {forbidden}"

    def test_health_stays_ok_when_forecast_quiet_window_has_only_gate_skips(self, db):
        _insert_pipeline_health(db, "KX00", "scan", "OK", "betting tick")
        _insert_pipeline_health(
            db,
            "KX00",
            "gates",
            "SKIP",
            "Outside trading window: target 2026-05-25, local 2026-05-26 (past)",
        )

        health = _compute_health_status()

        assert health["status"] == "ok"
        assert health["forecasts_2h"] == 0
        assert health["forecast_attempts_2h"] == 0
        assert health["forecast_upstream_ok_2h"] == 0

    def test_health_down_when_forecast_missing_after_upstream_success(self, db):
        _insert_pipeline_health(db, "KX00", "scan", "OK", "betting tick")
        _insert_pipeline_health(db, "KX00", "market", "OK", "11 brackets")

        health = _compute_health_status()

        assert health["status"] == "down"
        assert health["forecasts_2h"] == 0
        assert health["forecast_upstream_ok_2h"] == 1


class TestLoginRateLimit:
    """ce-code-review P1 #28: /login is rate-limited after 5 failures in 60s.

    These tests exercise the rate-limit helpers directly because the FastAPI
    /login endpoint relies on `python-multipart` for form parsing, which isn't
    in the project's runtime deps. Direct testing of the in-process counter
    is sufficient: the endpoint is a thin wrapper that calls the same helpers.
    """

    def test_failed_attempts_lock_after_threshold(self):
        from hightempbot.dashboard.app import (
            _login_rate_limited,
            _record_login_failure,
            _reset_login_rate_limit_state,
            _LOGIN_MAX_ATTEMPTS,
        )

        _reset_login_rate_limit_state()
        try:
            ip = "10.0.0.1"
            # Under the threshold, the IP is not locked out.
            for _ in range(_LOGIN_MAX_ATTEMPTS - 1):
                assert _login_rate_limited(ip) is False
                _record_login_failure(ip)
            # The Nth failure trips the lock.
            _record_login_failure(ip)
            assert _login_rate_limited(ip) is True
        finally:
            _reset_login_rate_limit_state()

    def test_distinct_ips_have_distinct_buckets(self):
        from hightempbot.dashboard.app import (
            _login_rate_limited,
            _record_login_failure,
            _reset_login_rate_limit_state,
            _LOGIN_MAX_ATTEMPTS,
        )

        _reset_login_rate_limit_state()
        try:
            # Saturate one IP.
            for _ in range(_LOGIN_MAX_ATTEMPTS):
                _record_login_failure("10.0.0.2")
            assert _login_rate_limited("10.0.0.2") is True
            # Different IP starts fresh.
            assert _login_rate_limited("10.0.0.3") is False
        finally:
            _reset_login_rate_limit_state()

    def test_clear_login_attempts_resets_bucket(self):
        from hightempbot.dashboard.app import (
            _clear_login_attempts,
            _login_rate_limited,
            _record_login_failure,
            _reset_login_rate_limit_state,
            _LOGIN_MAX_ATTEMPTS,
        )

        _reset_login_rate_limit_state()
        try:
            ip = "10.0.0.4"
            for _ in range(_LOGIN_MAX_ATTEMPTS):
                _record_login_failure(ip)
            assert _login_rate_limited(ip) is True
            _clear_login_attempts(ip)
            assert _login_rate_limited(ip) is False
        finally:
            _reset_login_rate_limit_state()

    def test_window_expiry_resets_attempt_counter(self):
        from hightempbot.dashboard.app import (
            _login_rate_limited,
            _record_login_failure,
            _reset_login_rate_limit_state,
            _LOGIN_MAX_ATTEMPTS,
            _LOGIN_WINDOW_SECONDS,
        )

        _reset_login_rate_limit_state()
        try:
            ip = "10.0.0.5"
            for _ in range(_LOGIN_MAX_ATTEMPTS - 1):
                _record_login_failure(ip, now=0.0)
            assert _login_rate_limited(ip, now=0.0) is False
            # Failures roll out past the rolling window — a new failure starts
            # a fresh bucket rather than tripping the lock.
            _record_login_failure(ip, now=_LOGIN_WINDOW_SECONDS + 1.0)
            assert _login_rate_limited(ip, now=_LOGIN_WINDOW_SECONDS + 1.0) is False
        finally:
            _reset_login_rate_limit_state()


# ------------------------------------------------------------------ /v2 SPA

class TestV2Spa:
    def test_v2_serves_html(self, client):
        resp = client.get("/v2")
        assert resp.status_code == 200
        assert "HighTempBot" in resp.text or "Trading Journal" in resp.text

    def test_v2_loads_static_styles(self, client):
        resp = client.get("/static/v2/styles.css")
        assert resp.status_code == 200
        assert "--accent" in resp.text or "kpi" in resp.text

    def test_trade_journal_badge_uses_current_target_trade_count(self, client):
        resp = client.get("/static/v2/Shell.jsx")
        assert resp.status_code == 200
        assert "count: Number(d.todayBets || 0)" in resp.text
        assert "openPositionsList.length + d.resolvedPositionsList.length" not in resp.text

    def test_calendar_tints_day_and_week_result_cells(self, client):
        calendar = client.get("/static/v2/Calendar.jsx")
        styles = client.get("/static/v2/styles.css")
        assert calendar.status_code == 200
        assert styles.status_code == 200
        assert '"cal2-week-total " + pnlTone' in calendar.text
        assert "cal2-wl" in calendar.text
        assert ".cal2-cell.has.pos" in styles.text
        assert ".cal2-cell.has.neg" in styles.text
        assert ".cal2-week-total.pos" in styles.text
        assert ".cal2-week-total.neg" in styles.text
        assert "rgba(31,138,91,0.18)" in styles.text
        assert "rgba(193,53,42,0.17)" in styles.text
        assert "inset 4px 0 0 rgba(31,138,91,0.78)" in styles.text

    def test_overview_net_pnl_uses_realized_basis(self, client):
        resp = client.get("/static/v2/Overview.jsx")
        assert resp.status_code == 200
        assert "const realizedPnl = Number(d.realizedPnl ?? d.totalPnl ?? 0)" in resp.text
        assert "value={formatMoney(realizedPnl, true)}" in resp.text
        assert 'sub="realized capital"' in resp.text
        assert "dataApiOpenPositionCashPnl" not in resp.text

    def test_performance_net_pnl_uses_realized_basis(self, client):
        resp = client.get("/static/v2/Pages2.jsx")
        assert resp.status_code == 200
        assert "const netPnl = Number(d.realizedPnl ?? d.totalPnl ?? 0)" in resp.text
        assert 'sub={`${d.resolvedCount} resolved`}' in resp.text

    def test_trade_journal_net_pnl_is_not_window_labeled(self, client):
        resp = client.get("/static/v2/Pages3.jsx")
        assert resp.status_code == 200
        assert 'label="Net P&L (window)"' not in resp.text
        assert "const netPnlW = Number(d.realizedPnl ?? d.totalPnl ?? 0)" in resp.text

    def test_strategy_tab_omits_removed_l2_premium_gate(self, client):
        resp = client.get("/static/v2/Strategy.jsx")
        assert resp.status_code == 200
        assert '"ask - displayed"' not in resp.text
        assert "L2 ask premium" not in resp.text
        assert "ask - mid" not in resp.text


# ------------------------------------------------------- /api/v2/data shape

class TestV2DataEndpoint:
    def test_returns_json_with_top_level_keys(self, client):
        resp = client.get("/api/v2/data")
        assert resp.status_code == 200
        d = resp.json()
        # Critical keys consumed by the React UI:
        for key in (
            "capital", "totalPnl", "realizedPnl", "winRate", "wins", "losses",
            "accountPnl", "accountPnlPct",
            "ddPct", "ddHaltThreshold", "reducedSizeThreshold",
            "openPositions", "pendingExposure",
            "openPositionsList", "resolvedPositionsList",
            "stations", "performanceByStation", "strategies",
            "ymidExits", "equityCurve", "tradingEquityCurve",
            "accountEquityCurve", "withdrawalEvents", "weeklyPnl", "pnlDist",
            "streaks", "ratios", "calibration", "calibrationByStation",
            "calendar", "stationSparks", "ensembleByStation",
            "funnel", "uptime", "lastScanAgo", "mode",
            "operator", "wallet", "readiness", "liveActionsEnabled",
        ):
            assert key in d, f"missing key: {key}"

    def test_dry_run_mode(self, client):
        d = client.get("/api/v2/data").json()
        assert d["mode"] == "DRY-RUN"
        assert d["operator"]["bootDryRun"] is True
        assert d["wallet"]["source"] == "POLY_FUNDER"
        assert d["liveActionsEnabled"] is False

    def test_grouped_trade_counts_and_target_size(self, db, client):
        configure(str(db), dry_run=True, initial_bankroll=100.0)
        target_date = _utc_today().isoformat()
        conn = get_connection(str(db))
        try:
            rows = [
                (
                    "2026-05-21 11:04:05", "KDAL", "m-kdal", "tok-kdal",
                    75.0, 4.9973, 0.77, 6.49, "ord-kdal",
                    {"strategy": "NO", "bracket_low": None, "bracket_high": 75.5, "bracket_unit": "F"},
                ),
                (
                    "2026-05-21 12:04:12", "KLAX", "m-klax", "tok-klax",
                    74.0, 2.75, 0.55, 5.0, "ord-klax-1",
                    {"strategy": "NO", "bracket_low": 73.5, "bracket_high": None, "bracket_unit": "F"},
                ),
                (
                    "2026-05-21 13:14:22", "KLAX", "m-klax", "tok-klax",
                    74.0, 1.855, 0.53, 3.5, "ord-klax-2",
                    {"strategy": "NO", "bracket_low": 73.5, "bracket_high": None, "bracket_unit": "F"},
                ),
            ]
            for bet_ts, station_id, market_id, token_id, threshold, bet_size, fill_price, fill_size, order_id, detail in rows:
                conn.execute(
                    """INSERT INTO ledger
                    (bet_ts, fill_ts, station_id, market_id, token_id, target_date,
                     horizon, threshold, side, p_model, p_market, edge,
                     kelly_size, volume_cap, bet_size, limit_price,
                     order_id, fill_price, fill_size, outcome, event_type, event_detail)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        bet_ts, bet_ts, station_id, market_id, token_id, target_date,
                        1, threshold, "NO", 0.30, fill_price, 0.10,
                        bet_size, 5000.0, bet_size, fill_price,
                        order_id, fill_price, fill_size, "PENDING", "bet", json.dumps(detail),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        d = client.get("/api/v2/data").json()

        assert d["openPositions"] == 2
        assert d["todayBets"] == 2
        assert d["todayVolume"] == pytest.approx(9.60)
        assert d["pendingExposure"] == pytest.approx(9.60)
        assert d["openCostBasis"] == pytest.approx(9.60)

        klax = next(row for row in d["openPositionsList"] if row["id"] == "KLAX")
        assert klax["fillCount"] == 2
        assert klax["size"] == pytest.approx(4.61)
        assert klax["targetSize"] == pytest.approx(7.01)

        station = next(row for row in d["performanceByStation"] if row["key"] == "KLAX")
        assert station["n_bets"] == 1

        conn = get_connection(str(db))
        try:
            conn.execute(
                "INSERT INTO bankroll_peak (sampled_at, wallet_balance, realized_pnl, pending_exposure) "
                "VALUES (?, ?, ?, ?)",
                ("2026-05-21 08:19:10", 106.09, 0.0, 0.0),
            )
            conn.execute(
                "INSERT INTO bankroll_peak (sampled_at, wallet_balance, realized_pnl, pending_exposure) "
                "VALUES (?, ?, ?, ?)",
                ("2026-05-21 14:14:21", 90.23478, 0.0, 9.6023),
            )
            conn.commit()
        finally:
            conn.close()

        configure(str(db), dry_run=False, initial_bankroll=100.0)
        d_live = client.get("/api/v2/data").json()
        assert d_live["capital"] == pytest.approx(100.0)
        assert d_live["walletBalance"] == pytest.approx(90.23)
        assert d_live["walletPeak"] == pytest.approx(100.0)
        assert d_live["openCostBasis"] == pytest.approx(9.60)
        assert d_live["ddPct"] == pytest.approx(0.0)

    def test_resolved_kpis_count_grouped_trades_not_fills(self, db, client):
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_enrolled_station(db, "KLAX", "Los Angeles")
        # KLAX was filled in two ledger rows, but it is one strategy slot.
        _insert_ledger_row(
            db, "WIN", station_id="KLAX", pnl=2.19,
            bet_ts="2026-05-21 12:04:12", target_date="2026-05-21",
            strategy="NO",
        )
        _insert_ledger_row(
            db, "WIN", station_id="KLAX", pnl=1.60,
            bet_ts="2026-05-21 13:14:22", target_date="2026-05-21",
            strategy="NO",
        )
        _insert_ledger_row(
            db, "WIN", station_id="KDAL", pnl=1.44,
            bet_ts="2026-05-21 11:04:05", target_date="2026-05-21",
            strategy="NO",
        )

        d = client.get("/api/v2/data").json()

        assert len(d["resolvedPositionsList"]) == 2
        assert d["resolvedCount"] == 2
        assert d["wins"] == 2
        assert d["losses"] == 0
        assert d["streaks"]["current"] == "W2"
        assert sum(bucket["n"] for bucket in d["pnlDist"]) == 2
        day = d["calendar"]["2026-05"][0]
        assert day["n_trades"] == 2
        assert day["wins"] == 2

    def test_resolved_kpis_keep_distinct_event_detail_brackets(self, db, client):
        _insert_enrolled_station(db, "KDAL", "Dallas")
        conn = get_connection(str(db))
        try:
            for detail in (
                {"strategy": "NO", "bracket_low": 70.5, "bracket_high": 72.5},
                {"strategy": "NO", "bracket_low": 72.5, "bracket_high": 74.5},
            ):
                conn.execute(
                    """INSERT INTO ledger
                    (bet_ts, station_id, market_id, token_id, target_date,
                     horizon, threshold, side, p_model, p_market, edge,
                     kelly_size, volume_cap, bet_size, limit_price,
                     outcome, event_type, pnl, event_detail)
                    VALUES (?, 'KDAL', 'm', 't', '2026-05-21',
                     1, 73.0, 'NO', 0.45, 0.30, 0.15,
                     10.0, 5000.0, 10.0, 0.30, 'WIN', 'bet', 1.00, ?)""",
                    ("2026-05-21 12:00:00", json.dumps(detail)),
                )
            conn.commit()
        finally:
            conn.close()

        d = client.get("/api/v2/data").json()

        assert d["resolvedCount"] == 2
        assert d["wins"] == 2
        assert len(d["resolvedPositionsList"]) == 2

    def test_live_capital_uses_ledger_basis_when_resolved_cash_lags(self, db, client):
        configure(str(db), dry_run=False, initial_bankroll=100.0)
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_enrolled_station(db, "KLAX", "Los Angeles")
        _insert_ledger_row(
            db, "WIN", station_id="KLAX", pnl=2.19,
            bet_ts="2026-05-21 12:04:12", target_date="2026-05-21",
            strategy="NO",
        )
        _insert_ledger_row(
            db, "WIN", station_id="KLAX", pnl=1.60,
            bet_ts="2026-05-21 13:14:22", target_date="2026-05-21",
            strategy="NO",
        )
        _insert_ledger_row(
            db, "WIN", station_id="KDAL", pnl=1.44,
            bet_ts="2026-05-21 11:04:05", target_date="2026-05-21",
            strategy="NO",
        )
        conn = get_connection(str(db))
        try:
            conn.execute(
                """INSERT INTO ledger
                (bet_ts, fill_ts, station_id, market_id, token_id, target_date,
                 horizon, threshold, side, p_model, p_market, edge,
                 kelly_size, volume_cap, bet_size, limit_price,
                 order_id, fill_price, fill_size, outcome, event_type, event_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "2026-05-22 00:14:00", "2026-05-22 00:14:30",
                    "KDAL", "m-open", "tok-open", _utc_today().isoformat(),
                    1, 75.5, "NO", 0.30, 0.77, 0.10,
                    4.0, 5000.0, 4.0, 0.77,
                    "ord-open", 0.77, 5.1948, "PENDING", "bet",
                    json.dumps({"strategy": "NO", "bracket_low": None, "bracket_high": 75.5}),
                ),
            )
            conn.execute(
                "INSERT INTO bankroll_peak (sampled_at, wallet_balance, realized_pnl, pending_exposure) "
                "VALUES (?, ?, ?, ?)",
                ("2026-05-22 00:15:00", 49.41, 5.23, 4.0),
            )
            conn.commit()
        finally:
            conn.close()

        d = client.get("/api/v2/data").json()

        assert d["walletBalance"] == pytest.approx(49.41)
        assert d["openCostBasis"] == pytest.approx(4.0)
        assert d["capital"] == pytest.approx(105.23)
        assert d["accountPnl"] == pytest.approx(5.23)
        assert d["accountPnlPct"] == pytest.approx(5.2)
        assert d["totalPnl"] == pytest.approx(5.23)
        assert d["realizedPnl"] == pytest.approx(5.23)
        assert d["ddPct"] == pytest.approx(0.0)
        assert d["openPositionsList"][0]["targetSize"] == pytest.approx(7.37)

    def test_data_api_marks_do_not_change_visible_capital_or_pnl(self, db, client):
        configure(str(db), dry_run=False, initial_bankroll=100.0)
        snapshot = {
            "clobBalanceUsd": 48.0,
            "dataApiTrustedOpenPositionsValueUsd": 63.92,
            "dataApiTrustedOpenPositionsInitialValueUsd": 59.35,
            "dataApiTrustedOpenPositionsCashPnlUsd": 4.56,
            "sourcesChecked": {"dataApiPositions": True},
            "dataApiReconciliationWarnings": [],
        }
        conn = get_connection(str(db))
        try:
            conn.execute(
                """INSERT INTO wallet_reconciliation_runs
                (sampled_at, wallet_address, source_status, clob_balance_usd,
                 open_positions_count, snapshot_json)
                VALUES (datetime('now'), ?, 'live_refresh', ?, ?, ?)""",
                ("0x" + "d" * 40, 48.0, 14, json.dumps(snapshot)),
            )
            conn.commit()
        finally:
            conn.close()

        d = client.get("/api/v2/data").json()

        # Live mode with a wallet reading present now reports the halt-gate
        # mirror ("live_gate"), but the invariant this test pins is unchanged:
        # unrealized Data-API marks (63.92 here) must not move visible
        # Capital — wallet 48 + open cost 0 floors at ledger realized 100.
        assert d["capitalSource"] == "live_gate"
        assert d["capital"] == pytest.approx(100.0)
        assert d["accountPnl"] == pytest.approx(0.0)
        assert d["accountPnlPct"] == pytest.approx(0.0)
        assert d["totalPnl"] == pytest.approx(0.0)
        assert d["realizedPnl"] == pytest.approx(0.0)
        assert d["dataApiOpenPositionCashPnl"] == pytest.approx(4.56)

    def test_live_capital_subtracts_return_transfer_outflow(self, db, client):
        from hightempbot.persistence.wallet_reconciliation import (
            build_wallet_snapshot,
            record_wallet_snapshot,
        )
        from hightempbot.runtime_config import Config, set_config

        configure(str(db), dry_run=False, initial_bankroll=100.0)
        wallet = "0x" + "d" * 40
        set_config(Config(_env_file=None, dry_run=False, poly_funder=wallet))
        try:
            conn = get_connection(str(db))
            try:
                conn.execute(
                    """INSERT INTO transfer_requests
                    (created_at, updated_at, from_wallet, to_wallet, amount_usd, status, confirmation)
                    VALUES (?, ?, ?, ?, ?, 'SUBMITTED', ?)""",
                    (
                        "2026-05-22 00:20:00",
                        "2026-05-22 00:20:00",
                        wallet,
                        "0x" + "e" * 40,
                        10.0,
                        "ok",
                    ),
                )
                conn.execute(
                    """INSERT INTO transfer_requests
                    (created_at, updated_at, from_wallet, to_wallet, amount_usd, status, confirmation)
                    VALUES (?, ?, ?, ?, ?, 'SUBMITTING', ?)""",
                    (
                        "2026-01-01 00:00:00",
                        "2026-01-01 00:00:00",
                        wallet,
                        "0x" + "e" * 40,
                        99.0,
                        "stale",
                    ),
                )
                record_wallet_snapshot(
                    conn,
                    build_wallet_snapshot(
                        conn,
                        wallet_address=wallet,
                        clob_balance_usd=90.0,
                        chain_balance_usd=90.0,
                        data_api_trades=[],
                        data_api_positions=[],
                        open_orders=[],
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            d = client.get("/api/v2/data").json()

            assert d["walletBalance"] == pytest.approx(90.0)
            assert d["returnTransferOutflow"] == pytest.approx(10.0)
            assert d["capital"] == pytest.approx(90.0)
            assert d["accountPnl"] == pytest.approx(0.0)
            assert d["accountPnlPct"] == pytest.approx(0.0)
            assert d["totalPnl"] == pytest.approx(0.0)
            assert d["ddPct"] == pytest.approx(0.0)
            assert d["accountEquityCurve"][-1]["y"] == pytest.approx(-10.0)
            assert d["accountEquityCurve"][-1]["withdrawal"] == pytest.approx(10.0)
            assert d["tradingEquityCurve"] == []
            assert d["withdrawalEvents"][0]["amount"] == pytest.approx(10.0)
        finally:
            set_config(None)

    def test_operator_stop_endpoint_sets_processing_state(self, client):
        resp = client.post(
            "/api/v2/admin/operator/stop",
            json={"reason": "test stop"},
        )

        assert resp.status_code == 200
        assert resp.json()["operator"]["state"] == "STOPPED_PROCESSING"
        d = client.get("/api/v2/data").json()
        assert d["operator"]["processingEnabled"] is False

    def test_operator_start_refuses_dry_run_boot(self, client):
        client.post("/api/v2/admin/operator/stop", json={"reason": "test stop"})

        resp = client.post(
            "/api/v2/admin/operator/start",
            json={"reason": "test start"},
        )

        assert resp.status_code == 409
        assert "DRY_RUN" in resp.json()["detail"]

    def test_transfer_preview_is_available_but_refuses_dry_run(self, client):
        resp = client.post(
            "/api/v2/admin/operator/transfer/preview",
            json={"amount": "10"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert any("DRY_RUN" in err for err in body["preview"]["errors"])

    def test_unsupported_source_is_not_reported_as_missing_lut(self, db, client):
        _insert_enrolled_station(
            db,
            "UTST",
            "Unsupported Test",
            resolution_source="ncei",
            status="DRY_RUN",
        )
        _insert_full_actual_coverage(db, "UTST", source="ncei")

        d = client.get("/api/v2/data").json()

        station = next(s for s in d["stations"] if s["id"] == "UTST")
        assert station["stage"] == "source_fail"
        assert station["lut_total_n"] == 0

    def test_live_mode(self, tmp_path):
        db_path = tmp_path / "live.db"
        init_db(str(db_path))
        configure(str(db_path), dry_run=False, initial_bankroll=1000.0)
        d = TestClient(app).get("/api/v2/data").json()
        assert d["mode"] == "LIVE"

    def test_operator_start_refreshes_stale_readiness_on_demand(self, tmp_path, monkeypatch):
        from datetime import datetime, timedelta, timezone

        from hightempbot.db.connection import utc_now_sql
        from hightempbot.execution.live_readiness import ReadinessReport
        from hightempbot.persistence.wallet_reconciliation import (
            build_wallet_snapshot,
            record_wallet_snapshot,
        )
        from hightempbot.runtime_config import Config, set_config

        wallet = "0x" + "d" * 40
        db_path = tmp_path / "live-action.db"
        init_db(str(db_path))
        configure(str(db_path), dry_run=False, initial_bankroll=1000.0)
        cfg = Config(
            _env_file=None,
            dry_run=False,
            poly_funder=wallet,
            operator_action_freshness_ttl_s=300,
            wallet_snapshot_freshness_ttl_s=300,
        )
        set_config(cfg)

        def _fake_readiness(_cfg):
            expires = datetime.now(timezone.utc) + timedelta(minutes=5)
            return ReadinessReport(
                status="OK",
                mode="LIVE",
                generated_at=utc_now_sql(),
                expires_at=expires.strftime("%Y-%m-%d %H:%M:%S"),
                signature_type=3,
                funder=wallet,
            )

        def _fake_wallet_refresh(conn, *, config):
            snapshot = build_wallet_snapshot(
                conn,
                wallet_address=config.poly_funder,
                clob_balance_usd=100.0,
                chain_balance_usd=100.0,
                data_api_trades=[],
                data_api_positions=[],
                open_orders=[],
                chain_balance_required=False,
            )
            record_wallet_snapshot(conn, snapshot)
            return snapshot

        monkeypatch.setattr("hightempbot.execution.live_readiness.build_live_readiness_report", _fake_readiness)
        monkeypatch.setattr("hightempbot.persistence.wallet_reconciliation.refresh_wallet_snapshot", _fake_wallet_refresh)
        try:
            client = TestClient(app)
            client.post("/api/v2/admin/operator/stop", json={"reason": "test"})

            resp = client.post("/api/v2/admin/operator/start", json={"reason": "test"})

            assert resp.status_code == 200
            assert resp.json()["operator"]["state"] == "LIVE"
        finally:
            set_config(None)

    def test_transfer_submit_endpoint_auth_and_safety(self, tmp_path, monkeypatch):
        """ce-code-review P2 #45: e2e coverage for POST /transfer/submit.

        Covers:
        - dry-run boot rejects with 409 (live actions disabled)
        - live boot with TransferSafetyError surfaces as 4xx, not 500
        - successful submission returns ok+transfer envelope
        """
        from datetime import datetime, timedelta, timezone

        from hightempbot.db.connection import utc_now_sql
        from hightempbot.execution.live_readiness import ReadinessReport
        from hightempbot.execution.operator_control import enter_transfer_lock
        from hightempbot.persistence.wallet_reconciliation import (
            build_wallet_snapshot,
            record_wallet_snapshot,
        )
        from hightempbot.runtime_config import Config, set_config

        wallet = "0x" + "d" * 40
        dest = "0x" + "e" * 40

        # Path 1: dry-run boot refuses the submit at the freshness gate.
        db_dry = tmp_path / "submit-dry.db"
        init_db(str(db_dry))
        configure(str(db_dry), dry_run=True, initial_bankroll=1000.0)
        try:
            resp_dry = TestClient(app).post(
                "/api/v2/admin/operator/transfer/submit",
                json={"amount": "10", "confirmation": "x"},
            )
            assert resp_dry.status_code == 409
            assert "DRY_RUN" in resp_dry.json()["detail"]
        finally:
            set_config(None)

        # Path 2: live boot with TransferSafetyError (no transfer lock) returns
        # 409. Sets up a fresh readiness + wallet snapshot so the freshness
        # gate passes, then expects the lock check inside submit_return_transfer
        # to raise.
        db_live = tmp_path / "submit-live.db"
        init_db(str(db_live))
        configure(str(db_live), dry_run=False, initial_bankroll=1000.0)
        cfg = Config(
            _env_file=None,
            dry_run=False,
            poly_funder=wallet,
            poly_return_wallet=dest,
        )
        set_config(cfg)

        def _fake_readiness(_cfg):
            expires = datetime.now(timezone.utc) + timedelta(minutes=5)
            return ReadinessReport(
                status="OK",
                mode="LIVE",
                generated_at=utc_now_sql(),
                expires_at=expires.strftime("%Y-%m-%d %H:%M:%S"),
                signature_type=3,
                funder=wallet,
            )

        def _fake_wallet_refresh(conn, *, config):
            snapshot = build_wallet_snapshot(
                conn,
                wallet_address=config.poly_funder,
                clob_balance_usd=100.0,
                chain_balance_usd=100.0,
                data_api_trades=[],
                data_api_positions=[],
                open_orders=[],
            )
            record_wallet_snapshot(conn, snapshot)
            return snapshot

        monkeypatch.setattr(
            "hightempbot.execution.live_readiness.build_live_readiness_report",
            _fake_readiness,
        )
        monkeypatch.setattr(
            "hightempbot.persistence.wallet_reconciliation.refresh_wallet_snapshot",
            _fake_wallet_refresh,
        )
        try:
            client = TestClient(app)
            # No transfer lock acquired -> submit must refuse.
            resp_locked = client.post(
                "/api/v2/admin/operator/transfer/submit",
                json={"amount": "10", "confirmation": "x", "to_wallet": dest},
            )
            # 409 from TransferSafetyError (no lock or confirmation mismatch).
            assert resp_locked.status_code in (409, 422), resp_locked.text

            with get_connection(str(db_live)) as conn:
                enter_transfer_lock(
                    conn,
                    actor="test",
                    reason="submit success path",
                    boot_dry_run=False,
                )

            def _fake_relayer(**kwargs):
                assert kwargs["deposit_wallet"] == wallet
                assert kwargs["to_address"] == dest
                assert kwargs["amount_base_units"] == 10_000_000
                return {"transactionID": "relay-123"}

            monkeypatch.setattr(
                "hightempbot.execution.polymarket_relayer.submit_deposit_wallet_pusd_transfer",
                _fake_relayer,
            )
            confirmation = f"TRANSFER 10.000000 PUSD TO {dest.lower()}"
            resp_ok = client.post(
                "/api/v2/admin/operator/transfer/submit",
                json={
                    "amount": "10",
                    "confirmation": confirmation,
                    "to_wallet": dest,
                },
                headers={"X-Operator-Actor": "operator:test"},
            )
            assert resp_ok.status_code == 200, resp_ok.text
            body = resp_ok.json()
            assert body["ok"] is True
            assert body["transfer"]["status"] == "SUBMITTED"
            assert body["transfer"]["relayerTxId"] == "relay-123"

            with get_connection(str(db_live)) as conn:
                row = conn.execute(
                    "SELECT actor, status, relayer_tx_id FROM transfer_requests"
                ).fetchone()
            assert row["actor"] == "operator:test"
            assert row["status"] == "SUBMITTED"
            assert row["relayer_tx_id"] == "relay-123"
        finally:
            set_config(None)

    def test_transfer_lock_refuses_incomplete_refreshed_wallet_snapshot(self, tmp_path, monkeypatch):
        from datetime import datetime, timedelta, timezone

        from hightempbot.db.connection import utc_now_sql
        from hightempbot.execution.live_readiness import ReadinessReport
        from hightempbot.persistence.wallet_reconciliation import (
            build_wallet_snapshot,
            record_wallet_snapshot,
        )
        from hightempbot.runtime_config import Config, set_config

        wallet = "0x" + "d" * 40
        db_path = tmp_path / "transfer-lock.db"
        init_db(str(db_path))
        configure(str(db_path), dry_run=False, initial_bankroll=1000.0)
        cfg = Config(_env_file=None, dry_run=False, poly_funder=wallet)
        set_config(cfg)

        def _fake_readiness(_cfg):
            expires = datetime.now(timezone.utc) + timedelta(minutes=5)
            return ReadinessReport(
                status="OK",
                mode="LIVE",
                generated_at=utc_now_sql(),
                expires_at=expires.strftime("%Y-%m-%d %H:%M:%S"),
                signature_type=3,
                funder=wallet,
            )

        def _fake_incomplete_wallet(conn, *, config):
            snapshot = build_wallet_snapshot(
                conn,
                wallet_address=config.poly_funder,
                clob_balance_usd=100.0,
                chain_balance_usd=100.0,
            )
            record_wallet_snapshot(conn, snapshot)
            return snapshot

        monkeypatch.setattr("hightempbot.execution.live_readiness.build_live_readiness_report", _fake_readiness)
        monkeypatch.setattr("hightempbot.persistence.wallet_reconciliation.refresh_wallet_snapshot", _fake_incomplete_wallet)
        try:
            resp = TestClient(app).post(
                "/api/v2/admin/operator/transfer/lock",
                json={"reason": "test"},
            )

            assert resp.status_code == 409
            assert "incomplete" in resp.json()["detail"]
        finally:
            set_config(None)

    def test_transfer_lock_allows_open_positions_with_free_cash(self, tmp_path, monkeypatch):
        from datetime import datetime, timedelta, timezone

        from hightempbot.db.connection import utc_now_sql
        from hightempbot.execution.live_readiness import ReadinessReport
        from hightempbot.persistence.wallet_reconciliation import (
            build_wallet_snapshot,
            record_wallet_snapshot,
        )
        from hightempbot.runtime_config import Config, set_config

        wallet = "0x" + "d" * 40
        db_path = tmp_path / "transfer-lock-open-position.db"
        init_db(str(db_path))
        configure(str(db_path), dry_run=False, initial_bankroll=1000.0)
        cfg = Config(
            _env_file=None,
            dry_run=False,
            poly_funder=wallet,
            poly_return_wallet="0x" + "e" * 40,
        )
        set_config(cfg)
        condition = "0x" + "8" * 64
        conn = get_connection(str(db_path))
        try:
            conn.execute(
                """INSERT INTO ledger
                (bet_ts, station_id, market_id, token_id, target_date,
                 horizon, threshold, side, p_model, p_market, edge,
                 kelly_size, volume_cap, bet_size, limit_price,
                 order_id, fill_price, fill_size, outcome, event_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "2026-05-21 00:00:00",
                    "KDAL",
                    condition,
                    "tok-open",
                    "2026-05-21",
                    1,
                    75.0,
                    "YES",
                    0.5,
                    0.5,
                    0.1,
                    2.5,
                    5000.0,
                    2.5,
                    0.5,
                    "ord-open",
                    0.5,
                    5.0,
                    "PENDING",
                    "bet",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        def _fake_readiness(_cfg):
            expires = datetime.now(timezone.utc) + timedelta(minutes=5)
            return ReadinessReport(
                status="OK",
                mode="LIVE",
                generated_at=utc_now_sql(),
                expires_at=expires.strftime("%Y-%m-%d %H:%M:%S"),
                signature_type=3,
                funder=wallet,
            )

        def _fake_wallet_with_position(conn, *, config):
            snapshot = build_wallet_snapshot(
                conn,
                wallet_address=config.poly_funder,
                clob_balance_usd=100.0,
                chain_balance_usd=100.0,
                data_api_trades=[],
                data_api_positions=[{
                    "asset": "tok-open",
                    "conditionId": condition,
                    "size": "5",
                    "avgPrice": "0.50",
                    "initialValue": "2.50",
                    "currentValue": "2.50",
                    "curPrice": "0.50",
                    "outcome": "Yes",
                }],
                open_orders=[],
            )
            record_wallet_snapshot(conn, snapshot)
            return snapshot

        monkeypatch.setattr("hightempbot.execution.live_readiness.build_live_readiness_report", _fake_readiness)
        monkeypatch.setattr("hightempbot.persistence.wallet_reconciliation.refresh_wallet_snapshot", _fake_wallet_with_position)
        try:
            resp = TestClient(app).post(
                "/api/v2/admin/operator/transfer/lock",
                json={"reason": "test"},
            )

            assert resp.status_code == 200
            assert resp.json()["operator"]["state"] == "TRANSFER_LOCK"
        finally:
            set_config(None)

    def test_halt_thresholds_match_optimum_strategy(self, client):
        """Halt at MAX_DD=0.40; dashboard halt banner must fire at the
        same DD as the bot's pipeline halt gate (execution.pipeline:
        ``drawdown >= MAX_DD``). 2026-05-20: switched from halve-at-MAX_DD
        to halt-at-MAX_DD; no reduced band anymore.
        """
        d = client.get("/api/v2/data").json()
        assert d["ddHaltThreshold"] == 40  # MAX_DD = 0.40 -> 40%
        # reducedSizeThreshold lingers in v2_data for back-compat with the
        # bundled JS that still reads the field. Halt-on-DD has no reduced
        # band — value of 100 means the legacy "reduced" band never fires.
        assert d["reducedSizeThreshold"] == 100

    def test_strategy_config_payload_reflects_execution_policy(self, client):
        d = client.get("/api/v2/data").json()
        cfg = d["strategiesConfig"]

        assert cfg["NO"]["capital_frac"] == STRATEGY_CONFIGS["NO"].capital_frac
        assert cfg["NO"]["execution_min_edge"] == STRATEGY_CONFIGS["NO"].execution_min_edge
        assert cfg["NO"]["max_vwap_slip_from_anchor"] is None
        # TAIL disabled 2026-07-17 (EV<=0 slice forensics): disabled strategies
        # are excluded from the per-strategy payload. Operator 2026-08-09:
        # they are no longer listed either — the tab shows only live sleeves.
        assert cfg["__disabled"]["value"] == []
        assert "TAIL" not in {k for k in cfg if not k.startswith("__")}
        assert cfg["NO"]["consensus_skip_threshold"] is None
        assert cfg["NO"]["delayed_entry_fp_max"] is None
        assert "__max_l2_ask_premium" not in cfg
        assert cfg["__execution_policy"] == {
            "scan_interval_minutes": SCAN_INTERVAL_MINUTES,
            "new_slot_tick": 0,
            "topups_every_tick": True,
            "min_bet_usd": float(MIN_BET_USD),
            "poly_fee_theta": float(POLY_FEE_THETA),
        }
        # The disabled sleeves stay in STRATEGY_CONFIGS (TP/SL monitor +
        # parity tests) but are never serialized for display.
        assert all(
            name not in cfg
            for name, strategy in STRATEGY_CONFIGS.items()
            if not strategy.enabled
        )

    def test_strategy_breakdown_aggregates_by_strategy(self, db, client):
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(db, "WIN", pnl=5.0, strategy="NO")
        _insert_ledger_row(db, "WIN", pnl=3.0, strategy="YMID")
        _insert_ledger_row(db, "LOSS", pnl=-2.0, strategy="TAIL")
        d = client.get("/api/v2/data").json()
        names = {s["name"] for s in d["strategies"]}
        assert {"NO", "YMID", "TAIL"} <= names

    def test_strategy_breakdown_uses_resolved_trades_only(self, db, client):
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(
            db,
            "WIN",
            pnl=5.0,
            bet_size=10.0,
            strategy="ZTEST",
            side="YES",
        )
        _insert_ledger_row(
            db,
            "PENDING",
            pnl=None,
            bet_size=90.0,
            strategy="ZTEST",
            side="YES",
            target_date="2026-05-09",
        )

        d = client.get("/api/v2/data").json()

        rows = [s for s in d["strategies"] if s["name"] == "ZTEST" and s["side"] == "YES"]
        assert len(rows) == 1
        row = rows[0]
        assert row["n_bets"] == 1
        assert row["n_resolved"] == 1
        assert row["wins"] == 1
        assert row["total_pnl"] == 5.0
        assert row["avg_stake"] == 10.0
        assert row["roi_pct"] == 50.0

    def test_legacy_no_strategy_treated_as_NO(self, db, client):
        """A pre-router row with no event_detail.strategy must be aggregated as NO."""
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(db, "WIN", pnl=4.0)  # no strategy field
        d = client.get("/api/v2/data").json()
        no_rows = [s for s in d["strategies"] if s["name"] == "NO"]
        assert no_rows
        assert sum(s["wins"] for s in no_rows) >= 1

    def test_open_positions_pendingexposure_excludes_resolved(self, db, client):
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(db, "PENDING", pnl=None)
        _insert_ledger_row(db, "WIN", pnl=5.0)
        d = client.get("/api/v2/data").json()
        assert d["openPositions"] >= 1
        # pendingExposure rounded; just check non-negative
        assert d["pendingExposure"] >= 0

    def test_calibration_buckets_have_predicted_observed(self, client):
        d = client.get("/api/v2/data").json()
        for bucket in d["calibration"]:
            assert "predicted" in bucket
            assert "observed" in bucket
            assert "n" in bucket

    def test_funnel_shape(self, client):
        d = client.get("/api/v2/data").json()
        f = d["funnel"]
        assert all(k in f for k in (
            "available_count", "source_eligible_count",
            "coverage_eligible_count", "active_count", "bettable_count",
        ))
        # available >= source_eligible >= coverage_eligible >= bettable
        assert f["available_count"] >= f["source_eligible_count"] >= f["bettable_count"]

    def test_ensemble_models_match_expected_models(self, db, client):
        """ensembleByStation must enumerate the 9 EXPECTED_MODELS for each runtime station."""
        from hightempbot.execution.strategy_constants import EXPECTED_MODELS
        _insert_enrolled_station(db, "KDAL", "Dallas")
        d = client.get("/api/v2/data").json()
        # Stations may be empty if get_all_stations doesn't pick up the test row
        # without a calibration_params entry — skip if empty.
        if not d["ensembleByStation"]:
            pytest.skip("no runtime stations to assert ensemble shape")
        for icao, models in d["ensembleByStation"].items():
            assert len(models) == len(EXPECTED_MODELS)
            sources = {m["source"] for m in models}
            assert sources == set(EXPECTED_MODELS), f"{icao}: {sources}"


def _insert_return_transfer(db_path, amount_usd: float, *, status: str = "SUBMITTED", wallet: str = "0x" + "d" * 40, created_at: str = "2026-06-13 00:00:00"):
    """Insert a capital-affecting return transfer (operator withdrawal)."""
    conn = get_connection(str(db_path))
    try:
        conn.execute(
            """INSERT INTO transfer_requests
            (created_at, updated_at, from_wallet, to_wallet, amount_usd, status, confirmation)
            VALUES (?, ?, ?, ?, ?, ?, 'ok')""",
            (created_at, created_at, wallet, "0x" + "e" * 40, amount_usd, status),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_withdrawal_redeposit_scenario(db_path):
    """Production scenario 2026-06-13: IB=100, all-time realized PnL +54.54
    (running-PnL peak +125.31), then a full $154.54 return transfer.

    Two resolved rows walk the cumulative PnL up to +125.31 then down to
    +54.54, so ``_ledger_peak`` sees a raw high-water of 225.31 (IB + peak).
    A single SUBMITTED return transfer of 154.54 (the full withdrawal)
    drives ``initial_bankroll + SUM(pnl) - transfers`` to exactly 0.
    """
    _insert_ledger_row(
        db_path, "WIN", station_id="KDAL", pnl=125.31,
        bet_ts="2026-06-01 12:00:00", target_date="2026-06-01", strategy="NO",
    )
    _insert_ledger_row(
        db_path, "LOSS", station_id="KDAL", pnl=-70.77,
        bet_ts="2026-06-02 12:00:00", target_date="2026-06-02", strategy="NO",
    )
    _insert_return_transfer(db_path, 154.54)


class TestLiveGateCapitalView:
    """Pure-math tests for ``live_gate_capital_view`` — the read-only mirror of
    the live halt gate the dashboard now displays after a withdrawal + re-fund.
    """

    def test_production_wallet_refunded_reads_wallet_not_zero(self, db):
        """Re-deposited $99.88 reaches the wallet with no ledger row. The
        gate view must report (99.88, 99.88) so the dashboard agrees with the
        pipeline gate (which trades normally), not the fake $0/100%-DD the
        ledger walk produces.
        """
        from hightempbot.execution.capital import live_gate_capital_view

        _seed_withdrawal_redeposit_scenario(db)
        conn = get_connection(str(db))
        try:
            capital, peak = live_gate_capital_view(
                conn, 100.0, wallet_balance=99.88,
            )
        finally:
            conn.close()

        assert capital == pytest.approx(99.88)
        assert peak == pytest.approx(99.88)

    def test_production_wallet_drained_floors_capital_but_keeps_ledger_peak(self, db, monkeypatch):
        """Wallet fully withdrawn (~$0): capital floors to 0, but the peak is
        the transfer-adjusted ledger high-water (225.31 - 154.54 = 70.77).

        Epoch is backdated so the whole scenario is in-session: with all
        history inside the session, ``_session_ledger_peak``'s reconstruction
        reproduces the legacy transfer-adjusted numbers exactly (seed
        collapses to IB=100), so this protection is unchanged by the
        2026-08-10 zero-reset.
        """
        from hightempbot.execution.capital import live_gate_capital_view

        monkeypatch.setattr(
            "hightempbot.execution.strategy_constants.DASHBOARD_SESSION_START_UTC",
            "2026-01-01 00:00:00",
        )
        _seed_withdrawal_redeposit_scenario(db)
        conn = get_connection(str(db))
        try:
            capital, peak = live_gate_capital_view(
                conn, 100.0, wallet_balance=0.0,
            )
        finally:
            conn.close()

        assert capital == pytest.approx(0.0)
        assert peak == pytest.approx(70.77)

    def test_api_position_value_adds_to_stake_basis(self, db, monkeypatch):
        """The ``api_position_value`` branch: capital = wallet + position value
        (floored at ledger realized). With the whole withdrawal history
        pre-epoch (2026-08-10 zero-reset), the session peak floors at the
        current basis: (60, 60) with marks, (40, 40) without.
        """
        from hightempbot.execution.capital import live_gate_capital_view

        monkeypatch.setattr(
            "hightempbot.execution.strategy_constants.DASHBOARD_SESSION_START_UTC",
            "2026-08-10 00:00:00",
        )
        _seed_withdrawal_redeposit_scenario(db)
        conn = get_connection(str(db))
        try:
            with_api = live_gate_capital_view(
                conn, 100.0, wallet_balance=40.0, api_position_value=20.0,
            )
            without_api = live_gate_capital_view(
                conn, 100.0, wallet_balance=40.0,
            )
        finally:
            conn.close()

        assert with_api == (pytest.approx(60.0), pytest.approx(60.0))
        # Same wallet, no position value -> position value is what moved it.
        assert without_api == (pytest.approx(40.0), pytest.approx(40.0))

    def test_floors_capital_at_ledger_realized_when_wallet_lags(self, db, monkeypatch):
        """Resolved-but-not-yet-redeemed proceeds: no withdrawal, ledger
        realized = 100 + 54.54 = 154.54. A wallet reading of $50 (cash lags)
        must NOT show as drawdown — capital floors up to 154.54, peak is the
        raw ledger high-water 225.31. (Epoch backdated: in-session history
        reproduces the legacy walk exactly.)
        """
        from hightempbot.execution.capital import live_gate_capital_view

        monkeypatch.setattr(
            "hightempbot.execution.strategy_constants.DASHBOARD_SESSION_START_UTC",
            "2026-01-01 00:00:00",
        )
        _insert_ledger_row(
            db, "WIN", station_id="KDAL", pnl=125.31,
            bet_ts="2026-06-01 12:00:00", target_date="2026-06-01", strategy="NO",
        )
        _insert_ledger_row(
            db, "LOSS", station_id="KDAL", pnl=-70.77,
            bet_ts="2026-06-02 12:00:00", target_date="2026-06-02", strategy="NO",
        )
        conn = get_connection(str(db))
        try:
            capital, peak = live_gate_capital_view(
                conn, 100.0, wallet_balance=50.0,
            )
        finally:
            conn.close()

        assert capital == pytest.approx(154.54)
        assert peak == pytest.approx(225.31)


class TestSessionZeroReset:
    """Operator zero-reset 2026-08-10: the gate re-bases at the session epoch
    (``capital._session_ledger_peak``). Pre-epoch history — the $188.95
    all-time peak and the stuck-SUBMITTED $84.05 withdrawal that had the live
    gate silently halted at ~48% DD — must have zero influence, while
    in-session losses must still ratchet DD toward the MAX_DD halt.
    """

    EPOCH = "2026-08-10 00:00:00"

    def _pin_epoch(self, monkeypatch):
        monkeypatch.setattr(
            "hightempbot.execution.strategy_constants.DASHBOARD_SESSION_START_UTC",
            self.EPOCH,
        )

    def _seed_pre_epoch_history(self, db):
        # Mirrors production: peak +88.95 above IB, net +2.36, $84.05 stuck
        # SUBMITTED withdrawal — the state that produced the fake ~48% DD.
        _insert_ledger_row(
            db, "WIN", station_id="KDAL", pnl=88.95,
            bet_ts="2026-06-01 12:00:00", target_date="2026-06-01", strategy="NO",
        )
        _insert_ledger_row(
            db, "LOSS", station_id="KDAL", pnl=-86.59,
            bet_ts="2026-07-01 12:00:00", target_date="2026-07-01", strategy="NO",
        )
        _insert_return_transfer(db, 84.05, created_at="2026-07-27 08:50:49")

    def test_fresh_session_reads_zero_drawdown(self, db, monkeypatch):
        """Day one of the reset: peak == basis, DD == 0, gate open."""
        from hightempbot.execution.capital import live_gate_capital_view

        self._pin_epoch(monkeypatch)
        self._seed_pre_epoch_history(db)
        conn = get_connection(str(db))
        try:
            capital, peak = live_gate_capital_view(
                conn, 100.0, wallet_balance=54.67,
            )
        finally:
            conn.close()

        assert capital == pytest.approx(54.67)
        assert peak == pytest.approx(54.67)

    def test_session_losses_still_trip_the_halt(self, db, monkeypatch):
        """A real in-session loss ratchets DD: losing $25 of the $54.67
        session bankroll reads ~45.7% >= MAX_DD 0.40. The reset re-bases the
        gate; it must not weaken it.
        """
        from hightempbot.execution.capital import live_gate_capital_view

        self._pin_epoch(monkeypatch)
        self._seed_pre_epoch_history(db)
        _insert_ledger_row(
            db, "LOSS", station_id="KDAL", pnl=-25.0,
            bet_ts="2026-08-11 12:00:00", target_date="2026-08-11", strategy="FLIP",
        )
        conn = get_connection(str(db))
        try:
            capital, peak = live_gate_capital_view(
                conn, 100.0, wallet_balance=29.67,
            )
        finally:
            conn.close()

        assert capital == pytest.approx(29.67)
        assert peak == pytest.approx(54.67)
        assert 1.0 - capital / peak >= 0.40

    def test_session_withdrawal_is_not_a_loss(self, db, monkeypatch):
        """An in-session recorded withdrawal shrinks the peak with the
        basis: DD stays 0, no fake halt."""
        from hightempbot.execution.capital import live_gate_capital_view

        self._pin_epoch(monkeypatch)
        self._seed_pre_epoch_history(db)
        _insert_return_transfer(db, 20.0, created_at="2026-08-11 09:00:00")
        conn = get_connection(str(db))
        try:
            capital, peak = live_gate_capital_view(
                conn, 100.0, wallet_balance=34.67,
            )
        finally:
            conn.close()

        assert capital == pytest.approx(34.67)
        assert peak == pytest.approx(34.67)

    def test_session_profit_ratchets_the_peak(self, db, monkeypatch):
        """Win +10 then lose -30: the peak holds at the session high-water
        (54.67 + 10), so DD reflects the fall from the session peak."""
        from hightempbot.execution.capital import live_gate_capital_view

        self._pin_epoch(monkeypatch)
        self._seed_pre_epoch_history(db)
        _insert_ledger_row(
            db, "WIN", station_id="KDAL", pnl=10.0,
            bet_ts="2026-08-11 12:00:00", target_date="2026-08-11", strategy="FLIP",
        )
        _insert_ledger_row(
            db, "LOSS", station_id="KDAL", pnl=-30.0,
            bet_ts="2026-08-12 12:00:00", target_date="2026-08-12", strategy="FLIP",
        )
        conn = get_connection(str(db))
        try:
            capital, peak = live_gate_capital_view(
                conn, 100.0, wallet_balance=34.67,
            )
        finally:
            conn.close()

        assert capital == pytest.approx(34.67)
        assert peak == pytest.approx(64.67)


class TestDashboardLiveGateOverride:
    """``build_htb_data`` live-mode override (via /api/v2/data): capital/peak
    now mirror the halt gate; dry-run stays on the ledger walk.
    """

    def test_live_dashboard_agrees_with_gate_after_redeposit(self, db, client):
        from hightempbot.persistence.wallet_reconciliation import (
            build_wallet_snapshot,
            record_wallet_snapshot,
        )
        from hightempbot.runtime_config import Config, set_config

        configure(str(db), dry_run=False, initial_bankroll=100.0)
        wallet = "0x" + "d" * 40
        set_config(Config(_env_file=None, dry_run=False, poly_funder=wallet))
        try:
            _seed_withdrawal_redeposit_scenario(db)
            # The full withdrawal is FROM this wallet so it counts as outflow.
            conn = get_connection(str(db))
            try:
                conn.execute(
                    "UPDATE transfer_requests SET from_wallet = ?", (wallet,)
                )
                record_wallet_snapshot(
                    conn,
                    build_wallet_snapshot(
                        conn,
                        wallet_address=wallet,
                        clob_balance_usd=99.88,
                        chain_balance_usd=99.88,
                        data_api_trades=[],
                        data_api_positions=[],
                        open_orders=[],
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            d = client.get("/api/v2/data").json()

            # Old ledger walk would report capital $0.00 / ddPct 100 here.
            assert d["capitalSource"] == "live_gate"
            assert d["walletBalance"] == pytest.approx(99.88)
            assert d["capital"] == pytest.approx(99.88)
            assert d["walletPeak"] == pytest.approx(99.88)
            assert d["ddPct"] == pytest.approx(0.0)
            assert d["returnTransferOutflow"] == pytest.approx(154.54)
        finally:
            set_config(None)

    def test_live_dashboard_drained_wallet_keeps_ledger_peak(self, db, client, monkeypatch):
        from hightempbot.persistence.wallet_reconciliation import (
            build_wallet_snapshot,
            record_wallet_snapshot,
        )
        from hightempbot.runtime_config import Config, set_config

        monkeypatch.setattr(
            "hightempbot.execution.strategy_constants.DASHBOARD_SESSION_START_UTC",
            "2026-01-01 00:00:00",
        )
        configure(str(db), dry_run=False, initial_bankroll=100.0)
        wallet = "0x" + "d" * 40
        set_config(Config(_env_file=None, dry_run=False, poly_funder=wallet))
        try:
            _seed_withdrawal_redeposit_scenario(db)
            conn = get_connection(str(db))
            try:
                conn.execute(
                    "UPDATE transfer_requests SET from_wallet = ?", (wallet,)
                )
                record_wallet_snapshot(
                    conn,
                    build_wallet_snapshot(
                        conn,
                        wallet_address=wallet,
                        clob_balance_usd=0.0,
                        chain_balance_usd=0.0,
                        data_api_trades=[],
                        data_api_positions=[],
                        open_orders=[],
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            d = client.get("/api/v2/data").json()

            assert d["capitalSource"] == "live_gate"
            assert d["capital"] == pytest.approx(0.0)
            assert d["walletPeak"] == pytest.approx(70.77)
            # dd = (70.77 - 0) / 70.77 * 100 = 100
            assert d["ddPct"] == pytest.approx(100.0)
        finally:
            set_config(None)

    def test_dry_run_capital_unchanged_by_gate_override(self, db, client):
        """Dry-run has no wallet snapshot, so capital/peak stay on the ledger
        walk and ``capitalSource`` is the ledger source, never ``live_gate``.
        """
        configure(str(db), dry_run=True, initial_bankroll=100.0)
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(
            db, "WIN", station_id="KDAL", pnl=5.0,
            bet_ts="2026-05-08 12:00:00", target_date="2026-05-08", strategy="NO",
        )

        d = client.get("/api/v2/data").json()

        assert d["mode"] == "DRY-RUN"
        assert d["capitalSource"] == "ledger_realized"
        assert d["capital"] == pytest.approx(105.0)
        assert d["ddPct"] == pytest.approx(0.0)


class TestV2TradeGrouping:
    @staticmethod
    def _row(**overrides):
        import json

        row = {
            "id": 1,
            "bet_ts": "2026-05-08 00:05:00",
            "fill_ts": "2026-05-08 00:05:10",
            "station_id": "KDAL",
            "city": "Dallas",
            "target_date": "2026-05-09",
            "threshold": 71.0,
            "bracket_low": 70.5,
            "bracket_high": 72.5,
            "bracket_label": "71-72F",
            "side": "NO",
            "edge": 0.10,
            "realized_edge": 0.12,
            "bet_size": 40.0,
            "fill_price": 0.05,
            "fill_size": 800.0,
            "volume_cap": 500.0,
            "limit_price": 0.06,
            "order_id": "ord_1",
            "outcome": "PENDING",
            "event_type": "dry_run",
            "event_detail": json.dumps({"strategy": "NO"}),
            "actual_label": None,
        }
        row.update(overrides)
        return row

    def test_open_rows_group_by_slot_with_weighted_fill_and_entry_edge(self):
        import json

        rows = [
            self._row(
                id=1,
                bet_size=40.0,
                fill_price=0.05,
                realized_edge=0.12,
                volume_cap=500.0,
                event_detail=json.dumps({
                    "strategy": "NO",
                    "fill_levels": [{"price": 0.05, "usd": 40.0, "shares": 800.0}],
                }),
            ),
            self._row(
                id=2,
                bet_ts="2026-05-08 00:10:00",
                fill_ts="2026-05-08 00:10:10",
                bet_size=60.0,
                fill_price=0.07,
                edge=0.16,
                realized_edge=0.22,
                volume_cap=900.0,
                order_id="ord_2",
                event_detail=json.dumps({
                    "strategy": "NO",
                    "fill_levels": [
                        {"price": 0.06, "usd": 25.0, "shares": 416.6666667},
                        {"price": 0.08, "usd": 35.0, "shares": 437.5},
                    ],
                }),
            ),
        ]

        grouped = _open_positions_for_v2(rows)

        assert len(grouped) == 1
        row = grouped[0]
        assert row["size"] == 100.0
        assert row["fill"] == pytest.approx(6.2)
        assert row["edge"] == pytest.approx(13.6)
        assert row["realizedEdge"] == pytest.approx(18.0)
        assert row["fillCount"] == 2
        assert [f["fillPrice"] for f in row["fills"]] == [0.05, 0.07]
        assert [f["levelVolume"] for f in row["fills"]] == [40.0, 60.0]
        assert row["fills"][1]["priceLevels"] == [
            {"price": 0.06, "usd": 25.0, "shares": 416.6667},
            {"price": 0.08, "usd": 35.0, "shares": 437.5},
        ]

    def test_fill_detail_synthesizes_vwap_level_when_ladder_missing(self):
        row = _open_positions_for_v2([
            self._row(id=1, volume_cap=900.0, event_detail='{"strategy":"NO"}'),
        ])[0]

        assert row["fills"][0]["levelVolume"] == 40.0
        assert row["fills"][0]["priceLevels"] == [
            {"price": 0.05, "usd": 40.0, "shares": 800.0}
        ]

    def test_fill_detail_still_ignores_market_volume_without_fill(self):
        row = _open_positions_for_v2([
            self._row(
                id=1,
                fill_price=None,
                fill_size=None,
                volume_cap=900.0,
                event_detail='{"strategy":"NO"}',
            ),
        ])[0]

        assert row["fills"][0]["levelVolume"] is None
        assert row["fills"][0]["priceLevels"] == []

    def test_grouping_keeps_live_and_dry_run_rows_separate(self):
        rows = [
            self._row(id=1, event_type="dry_run"),
            self._row(id=2, event_type="bet"),
        ]

        grouped = _open_positions_for_v2(rows)

        assert len(grouped) == 2

    def test_resolved_rows_sum_pnl_and_keep_fill_details(self):
        rows = [
            self._row(
                id=1,
                outcome="WIN",
                pnl=3.0,
                bet_size=25.0,
                fill_price=0.40,
                realized_edge=None,
                edge=0.10,
            ),
            self._row(
                id=2,
                bet_ts="2026-05-08 00:10:00",
                outcome="WIN",
                pnl=7.0,
                bet_size=75.0,
                fill_price=0.60,
                realized_edge=None,
                edge=0.20,
                order_id="ord_2",
            ),
        ]

        grouped = _resolved_positions_for_v2(rows)

        assert len(grouped) == 1
        row = grouped[0]
        assert row["pnl"] == 10.0
        assert row["outcome"] == "WIN"
        assert row["size"] == 100.0
        assert row["fill"] == pytest.approx(55.0)
        assert row["edge"] == pytest.approx(17.5)
        assert [f["pnl"] for f in row["fills"]] == [3.0, 7.0]


# ------------------------------------------------ helper fns kept from legacy

class TestCleanDisplayText:
    """_clean_display_text repairs UTF-8-as-cp1252 mojibake. Used by
    _enrich_ledger_positions which v2_data calls. Keep it tested."""

    def test_passthrough_clean_ascii(self):
        assert _clean_display_text("66-68") == "66-68"

    def test_repairs_degree_sign(self):
        # UTF-8 bytes for '°' decoded as cp1252 → 'Â°'
        assert _clean_display_text("66-68Â°F") == "66-68°F"

    def test_handles_none(self):
        assert _clean_display_text(None) == ""


class TestEnrichLedgerPositionsRecoveredOrphan:
    """RECOVERED orphan rows have no bracket bounds, so _enrich_ledger_positions
    must surface ``event_detail.bracket_label`` instead of falling back to
    the meaningless ``threshold`` string (e.g. ``"0.0°"``) for the display label.
    """

    @staticmethod
    def _row_with_recovered_orphan_detail(tmp_path) -> sqlite3.Row:
        """Insert a synthetic RECOVERED orphan row and return it as an sqlite3.Row.

        Going through INSERT + SELECT keeps the Row produced here identical
        in shape to what the dashboard's actual SQL would yield.
        """
        import json
        db_path = tmp_path / "recovered.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))
        try:
            conn.execute(
                """INSERT INTO ledger
                (bet_ts, station_id, market_id, token_id, target_date,
                 horizon, threshold, side, p_model, p_market, edge,
                 kelly_size, volume_cap, bet_size, limit_price,
                 fill_price, fill_size,
                 outcome, event_type, event_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("2026-05-13T08:00:00Z", "RECOVERED", "RECOVERED", "RECOVERED",
                 "RECOVERED", 1, 0.0, "YES", 0.0, 0.40, 0.0,
                 40.0, 0.0, 40.0, 0.40,
                 0.40, 100.0,
                 "PENDING", "bet",
                 json.dumps({
                     "recovered_orphan": True,
                     "bracket_label": "RECOVERED",
                 })),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM ledger WHERE id = 1").fetchone()
        finally:
            conn.close()
        return row

    def test_recovered_orphan_uses_event_detail_bracket_label(self, tmp_path):
        """Bracket bounds absent + event_detail.bracket_label='RECOVERED' must
        surface 'RECOVERED' on the dashboard row, not the threshold fallback
        ('0.0°') that would otherwise lie about a recovered fill's bracket."""
        row = self._row_with_recovered_orphan_detail(tmp_path)
        enriched = _enrich_ledger_positions([row], all_st={})
        assert len(enriched) == 1
        item = enriched[0]
        assert item["bracket_label"] == "RECOVERED"
        # The fallback used to surface as '0.0°' — guard against regression.
        assert item["bracket_label"] != "0.0°"
        # bracket_low/high are absent (recovered orphan has no bracket linkage).
        assert item["bracket_low"] is None
        assert item["bracket_high"] is None


class TestEnrichLedgerPositionsActualLabel:
    def test_data_api_redeemable_title_is_not_actual_label(self):
        row = {
            "id": 1,
            "station_id": "OPKC",
            "threshold": 34.0,
            "outcome": "WIN",
            "actual_tmax": None,
            "event_detail": json.dumps({
                "bracket_low": 33.5,
                "bracket_high": 34.5,
                "resolution_source": "polymarket_data_api_redeemable",
                "resolution_actual_label": (
                    "Will the highest temperature in Karachi be 34C on May 22?"
                ),
            }),
        }

        enriched = _enrich_ledger_positions([row], all_st={})

        assert enriched[0]["actual_label"] == ""
        assert _resolved_positions_for_v2(enriched)[0]["actual"] == ""

    def test_data_api_redeemable_uses_gamma_backfilled_label(self):
        row = {
            "id": 1,
            "station_id": "OPKC",
            "threshold": 34.0,
            "outcome": "WIN",
            "actual_tmax": None,
            "event_detail": json.dumps({
                "bracket_low": 33.5,
                "bracket_high": 34.5,
                "resolution_source": "polymarket_data_api_redeemable",
                "resolution_actual_label": "34C",
                "resolution_bracket_low": 33.5,
                "resolution_bracket_high": 34.5,
                "resolution_label_backfilled_at": "2026-05-22 10:00:00",
            }),
        }

        enriched = _enrich_ledger_positions([row], all_st={})

        assert enriched[0]["actual_label"] == "34C"
        assert _resolved_positions_for_v2(enriched)[0]["actual"] == "34C"


class TestOpenPositionApiValues:
    def test_open_position_uses_api_value_and_pnl_when_confirmed(self):
        rows = [{
            "id": 1,
            "bet_ts": "2026-05-22 08:00:00",
            "fill_ts": "2026-05-22 08:01:00",
            "station_id": "KDAL",
            "city": "Dallas",
            "target_date": "2026-05-22",
            "bracket_label": "75F or below",
            "side": "NO",
            "strategy": "NO",
            "threshold": 75.0,
            "fill_price": 0.80,
            "fill_size": 6.25,
            "edge": 0.10,
            "realized_edge": 0.09,
            "bet_size": 5.0,
            "limit_price": 0.80,
            "order_id": "ord_1",
            "outcome": "PENDING",
            "event_type": "bet",
            "api_position": {
                "sourceId": "token-no",
                "trusted": True,
                "currentValueUsd": 6.25,
                "initialValueUsd": 5.0,
                "cashPnlUsd": 1.25,
                "curPrice": 1.0,
                "reminder": "",
            },
        }]

        row = _open_positions_for_v2(rows, stake_basis_capital=101.25)[0]

        assert row["size"] == 5.0
        assert row["apiCurrentValue"] == 6.25
        assert row["apiCashPnl"] == 1.25
        assert row["apiConfirmed"] is True
        assert row["apiStatus"] == "API_CONFIRMED"

    def test_open_position_marks_unconfirmed_when_api_missing(self):
        rows = [{
            "id": 1,
            "bet_ts": "2026-05-22 08:00:00",
            "station_id": "KDAL",
            "city": "Dallas",
            "target_date": "2026-05-22",
            "bracket_label": "75F or below",
            "side": "NO",
            "strategy": "NO",
            "threshold": 75.0,
            "fill_price": 0.80,
            "fill_size": 6.25,
            "edge": 0.10,
            "bet_size": 5.0,
            "limit_price": 0.80,
            "order_id": "ord_1",
            "outcome": "PENDING",
            "event_type": "bet",
        }]

        row = _open_positions_for_v2(rows, stake_basis_capital=100.0)[0]

        assert row["apiCurrentValue"] is None
        assert row["apiCashPnl"] is None
        assert row["apiConfirmed"] is False
        assert row["apiStatus"] == "API_PENDING"
        assert "Waiting for Polymarket Data API" in row["apiReminder"]


# ---------------------------- legacy endpoints redirect to /v2 (no 404)

class TestLegacyRoutesRedirect:
    @pytest.mark.parametrize("url", [
        "/partials/summary",
        "/partials/stations",
        "/partials/trading",
        "/partials/overview/performance",
        "/partials/station/KDAL",
        "/partials/station/KDAL/buckets",
        "/legacy",
    ])
    def test_legacy_redirects_to_v2(self, client, url):
        resp = client.get(url, follow_redirects=False)
        assert resp.status_code == 308, f"{url} should redirect to /v2"
        assert resp.headers["location"] == "/v2"

    def test_legacy_pnl_data_returns_410_gone(self, client):
        """Old /api/pnl-data shape was {labels, pnl, bets}; /api/v2/data is a
        full payload. A 308 to v2 would silently deliver the wrong shape;
        a 410 with `successor` in the body is the honest contract.
        """
        resp = client.get("/api/pnl-data", follow_redirects=False)
        assert resp.status_code == 410
        body = resp.json()
        assert body["successor"] == "/api/v2/data"

    def test_legacy_partials_follow_redirect_lands_on_v2(self, client):
        resp = client.get("/partials/summary")  # follow_redirects=True default
        assert resp.status_code == 200
        assert "HighTempBot" in resp.text or "Trading Journal" in resp.text


# ------------------------ Per-station WIN/LOSS SQL (2026-05-16 ce-review)
#
# After commit 88aadb7 (and the 2026-05-16 review pass), the per-station
# perf SQL counts CLOSED+positive-pnl as a WIN and CLOSED+non-positive
# (including NULL via COALESCE) as a LOSS. Pin the boundaries so the SQL
# can't regress silently — the v2_data per-station query is the source of
# truth for the Stations table on the dashboard.

class TestPerStationWinLossSql:
    def _station_perf(self, client, icao: str) -> dict | None:
        d = client.get("/api/v2/data").json()
        for row in d.get("performanceByStation", []):
            if row.get("key") == icao:
                return row
        return None

    def test_closed_positive_pnl_counts_as_win(self, db, client):
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(db, "CLOSED", pnl=5.0)
        row = self._station_perf(client, "KDAL")
        assert row is not None
        assert row["wins"] == 1
        assert row["losses"] == 0

    def test_closed_zero_pnl_counts_as_loss(self, db, client):
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(db, "CLOSED", pnl=0.0)
        row = self._station_perf(client, "KDAL")
        assert row is not None
        assert row["wins"] == 0
        assert row["losses"] == 1

    def test_closed_negative_pnl_counts_as_loss(self, db, client):
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(db, "CLOSED", pnl=-3.0)
        row = self._station_perf(client, "KDAL")
        assert row is not None
        assert row["wins"] == 0
        assert row["losses"] == 1

    def test_closed_null_pnl_counts_as_loss(self, db, client):
        """Boundary: CLOSED with pnl IS NULL. The win arm (`pnl > 0`) is
        False on NULL; the loss arm rescues via COALESCE(pnl, 0) <= 0."""
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(db, "CLOSED", pnl=None)
        row = self._station_perf(client, "KDAL")
        assert row is not None
        assert row["wins"] == 0
        assert row["losses"] == 1

    def test_legacy_win_outcome_still_counts(self, db, client):
        """Pre-CLOSED rows with outcome='WIN' must still count alongside
        the new CLOSED+positive path."""
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(db, "WIN", pnl=4.0)
        _insert_ledger_row(db, "CLOSED", pnl=2.0)
        row = self._station_perf(client, "KDAL")
        assert row is not None
        assert row["wins"] == 2
        assert row["losses"] == 0

    def test_cancelled_excluded_from_both(self, db, client):
        """CANCELLED rows must NOT count as either wins or losses (memory note
        feedback_cancelled_display)."""
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(db, "CANCELLED", pnl=-10.0)
        _insert_ledger_row(db, "WIN", pnl=3.0)
        row = self._station_perf(client, "KDAL")
        assert row is not None
        assert row["wins"] == 1
        assert row["losses"] == 0


# --------------------------- Per-station EMOS μ/σ (2026-05-16 ce-review)
#
# build_htb_data emits emos_mu / emos_sigma per station in
# performanceByStation. The compute is wrapped in a try/except that
# silently degrades to None on failure; that error path is exactly the
# kind that masked the row-shadowing bug fixed in df45573. Pin the happy
# path so a regression that silently empties the EMOS panel surfaces.

class TestPerStationEmos:
    def _seed_horizon_1_ensemble(
        self,
        db_path,
        *,
        station_id: str = "KDAL",
        target_date: str = "2026-05-08",
        centres: tuple[str, ...] = ("ecmwf_ifs025", "gfs_seamless",
                                    "icon_seamless", "metno_seamless"),
        tmax_values: tuple[float, ...] = (21.0, 22.0, 23.0, 24.0),
        source: str = "openmeteo",
    ) -> None:
        conn = get_connection(str(db_path))
        try:
            for centre, tmax in zip(centres, tmax_values):
                conn.execute(
                    """INSERT INTO forecast_archive
                       (station_id, target_date, horizon, issue_date, centre,
                        member, tmax_celsius, source)
                       VALUES (?, ?, 1, '2026-05-07', ?, 0, ?, ?)""",
                    (station_id, target_date, centre, tmax, source),
                )
            conn.commit()
        finally:
            conn.close()

    def _seed_emos_params(
        self,
        db_path,
        *,
        station_id: str = "KDAL",
        a: float = 0.0, b: float = 1.0, c: float = -2.0, d: float = -2.0,
    ) -> None:
        import json
        params = {"a": a, "b": b, "c": c, "d": d, "n_samples": 100}
        conn = get_connection(str(db_path))
        try:
            conn.execute(
                """INSERT INTO calibration_params
                   (station_id, horizon, threshold_bucket, param_type,
                    params_blob, n_samples)
                   VALUES (?, 1, NULL, 'emos', ?, ?)""",
                (station_id, json.dumps(params), 100),
            )
            conn.commit()
        finally:
            conn.close()

    def _station_perf(self, client, icao: str) -> dict | None:
        d = client.get("/api/v2/data").json()
        for row in d.get("performanceByStation", []):
            if row.get("key") == icao:
                return row
        return None

    def test_happy_path_populates_emos_fields(self, db, client):
        """4 openmeteo centres + valid EMOS params → emos_mu/sigma are populated."""
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(db, "WIN", pnl=5.0)  # gives the station a perf row
        self._seed_horizon_1_ensemble(db)
        self._seed_emos_params(db)
        row = self._station_perf(client, "KDAL")
        assert row is not None
        # mu = a + b * mean(21,22,23,24) = 0 + 1 * 22.5 = 22.5
        assert row["emos_mu"] is not None
        assert abs(row["emos_mu"] - 22.5) < 0.5
        # sigma = sqrt(exp(c) + exp(d) * var) > 0
        assert row["emos_sigma"] is not None
        assert row["emos_sigma"] > 0

    def test_emos_skipped_when_below_member_threshold(self, db, client):
        """<4 members → emos_mu/sigma remain None even with valid params."""
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(db, "WIN", pnl=5.0)
        self._seed_horizon_1_ensemble(
            db,
            centres=("ecmwf_ifs025", "gfs_seamless"),  # only 2 centres
            tmax_values=(21.0, 22.0),
        )
        self._seed_emos_params(db)
        row = self._station_perf(client, "KDAL")
        assert row is not None
        assert row["emos_mu"] is None
        assert row["emos_sigma"] is None

    def test_emos_source_filter_ignores_live_rows(self, db, client):
        """Only source='openmeteo' rows feed EMOS — 'live' rows from the
        evaluator must not double-count even if they cohabit a target_date."""
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(db, "WIN", pnl=5.0)
        # 4 openmeteo + 4 live for the same target_date — only the openmeteo
        # values (mean 22.5) should drive mu.
        self._seed_horizon_1_ensemble(db, source="openmeteo",
                                       tmax_values=(21.0, 22.0, 23.0, 24.0))
        self._seed_horizon_1_ensemble(
            db, source="live",
            centres=("ecmwf_ifs025", "gfs_seamless",
                     "icon_seamless", "metno_seamless"),
            tmax_values=(40.0, 41.0, 42.0, 43.0),  # would skew mu to ~32.5 if mixed
        )
        self._seed_emos_params(db)
        row = self._station_perf(client, "KDAL")
        assert row is not None
        assert row["emos_mu"] is not None
        # mu must reflect openmeteo (22.5), not the mixed mean (32.5)
        assert abs(row["emos_mu"] - 22.5) < 1.0

    def test_emos_picks_latest_target_date_per_station(self, db, client):
        """When a station has multiple horizon=1 target_dates, the query
        picks MAX(target_date) — newer forecasts override older ones."""
        _insert_enrolled_station(db, "KDAL", "Dallas")
        _insert_ledger_row(db, "WIN", pnl=5.0)
        # Older target_date with skewed values that should NOT win.
        self._seed_horizon_1_ensemble(
            db, target_date="2026-05-01",
            tmax_values=(10.0, 10.0, 10.0, 10.0),
        )
        # Newer target_date with the "real" values — should be selected.
        self._seed_horizon_1_ensemble(
            db, target_date="2026-05-08",
            tmax_values=(21.0, 22.0, 23.0, 24.0),
        )
        self._seed_emos_params(db)
        row = self._station_perf(client, "KDAL")
        assert row is not None
        # mu should reflect the newer target_date (mean 22.5), not the older (10).
        assert row["emos_mu"] is not None
        assert abs(row["emos_mu"] - 22.5) < 1.0


class TestCapitalRangeDecoupled:
    """Capital and Max DD are NOT scoped to the selected range.

    When the operator selects 7d/30d, realized Net P&L narrows but Capital
    must keep reflecting the current account balance (initial bankroll + all
    session P&L). API open marks stay out of both visible Capital and Net P&L.
    """

    def test_capital_includes_pre_window_session_pnl(self, db, client):
        """An older in-session bet (>7d ago but after session floor) must
        still be reflected in Capital when the user selects 7d range. The
        bet's PnL is excluded from realized Net P&L but included in Capital
        (current balance).
        """
        today = _utc_today()
        older_day = today - timedelta(days=9)
        recent_day = today - timedelta(days=1)

        # Older bet: after session floor but >7d before today, so outside
        # the 7d window while still inside the session.
        _insert_ledger_row(
            db, "WIN", pnl=10.0,
            bet_ts=f"{older_day.isoformat()} 12:00:00", target_date=older_day.isoformat(),
        )
        # Recent bet: in-window.
        _insert_ledger_row(
            db, "LOSS", pnl=-2.0,
            bet_ts=f"{recent_day.isoformat()} 12:00:00", target_date=recent_day.isoformat(),
        )

        d_all = client.get("/api/v2/data").json()
        d_7d = client.get("/api/v2/data?range=7d").json()

        # Realized Net P&L narrows to in-window only (-$2.00 vs
        # -$2.00 + $10.00 = $8).
        assert abs(d_all["totalPnl"] - 8.0) < 0.01
        assert abs(d_7d["totalPnl"] - (-2.0)) < 0.01
        assert abs(d_all["realizedPnl"] - 8.0) < 0.01
        assert abs(d_7d["realizedPnl"] - (-2.0)) < 0.01

        # Capital is the SAME in both views — current account balance, not
        # windowed. The previous behavior anchored it at $100 in the 7d view
        # which hid the older WIN.
        assert abs(d_7d["capital"] - d_all["capital"]) < 0.01
        assert abs(d_all["capital"] - (1000.0 + 8.0)) < 0.01

    def test_max_dd_includes_pre_window_drawdown(self, db, client):
        """Max DD reflects the realized peak-to-trough across the full
        session, not just the selected window. The UI labels this KPI
        ``current peak-to-trough`` — windowing it would hide the very
        drawdown the halt gate is comparing against.
        """
        # Big WIN early in session → sets peak well above baseline.
        _insert_ledger_row(
            db, "WIN", pnl=200.0,
            bet_ts="2026-05-09 12:00:00", target_date="2026-05-09",
        )
        # LOSS later that takes capital back down → 200/1200 = 16.7% DD off
        # the post-WIN peak.
        _insert_ledger_row(
            db, "LOSS", pnl=-200.0,
            bet_ts="2026-05-19 12:00:00", target_date="2026-05-19",
        )

        d_7d = client.get("/api/v2/data?range=7d").json()
        # The peak was set BEFORE the 7d window opened, but the dashboard
        # must still report the realized DD — not a windowed slice.
        assert d_7d["ddPct"] > 10.0


class TestAdminResolvePending:
    """POST /api/v2/admin/resolve-pending — HTTP parity for the WU-fallback
    operator workflow (finding #24). Mirrors the CLI script's semantics:
    dry-run by default, refuses pre-threshold dates without ``force``, source-
    filtered actuals reads, audit reason on commit."""

    @staticmethod
    def _seed(db_path: str, *, station_id: str, target_date: str,
              actual_tmax_c: float, side: str = "NO", fill_price: float | None = 0.30,
              limit_price: float | None = None,
              bracket_low: float | None = 17.0,
              bracket_high: float | None = 21.0,
              actual_source: str = "wu", unit: str = "C") -> int:
        """Seed an enrolled station + WU actual + one PENDING bet. Returns the
        bet's row id so callers can read it back after commit."""
        import json
        if limit_price is None:
            limit_price = fill_price if fill_price is not None else 0.30
        fill_size = 5.0 / (fill_price if fill_price is not None else limit_price)
        conn = get_connection(str(db_path))
        try:
            conn.execute(
                """INSERT INTO enrolled_stations
                (icao, city, lat, lon, timezone, unit, resolution_source,
                 calibration_source, poly_slug, status, skip_reason)
                VALUES (?, ?, 0.0, 0.0, 'UTC', ?, 'wu', 'wu', ?, 'LIVE', NULL)""",
                (station_id, "TestCity", unit, station_id.lower()),
            )
            conn.execute(
                "INSERT OR REPLACE INTO actuals "
                "(station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
                (station_id, target_date, actual_tmax_c, actual_source),
            )
            cur = conn.execute(
                """INSERT INTO ledger
                (bet_ts, station_id, market_id, token_id, target_date,
                 horizon, threshold, side, p_model, p_market, edge,
                 kelly_size, volume_cap, bet_size, limit_price,
                 fill_price, fill_size,
                 outcome, event_type, event_detail)
                VALUES (?, ?, 'm', 'tok', ?, 1, 21.0, ?, 0.45, 0.30, 0.15,
                 5.0, 5000.0, 5.0, ?, ?, ?, 'PENDING', 'bet', ?)""",
                (
                    f"{target_date}T08:00:00Z", station_id, target_date, side,
                    limit_price, fill_price, fill_size,
                    json.dumps({"bracket_low": bracket_low,
                                "bracket_high": bracket_high}),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def test_rejects_non_iso_target_date(self, client):
        resp = client.post(
            "/api/v2/admin/resolve-pending",
            json={"target_date": "5/17/2026"},
        )
        assert resp.status_code == 400
        assert "ISO" in resp.json()["detail"]

    def test_rejects_future_target_date(self, client):
        resp = client.post(
            "/api/v2/admin/resolve-pending",
            json={"target_date": "2099-01-01"},
        )
        assert resp.status_code == 400
        assert "future" in resp.json()["detail"]

    def test_rejects_bad_icao(self, client):
        resp = client.post(
            "/api/v2/admin/resolve-pending",
            json={"target_date": "2026-05-10", "station": "XYZ"},
        )
        assert resp.status_code == 400
        assert "ICAO" in resp.json()["detail"]

    def test_rejects_non_object_body(self, client):
        resp = client.post(
            "/api/v2/admin/resolve-pending",
            json="not-an-object",
        )
        assert resp.status_code == 400

    def test_dry_run_returns_preview_without_writing(self, db, client):
        from datetime import date, timedelta
        target = (date.today() - timedelta(days=2)).isoformat()
        bet_id = self._seed(
            db, station_id="KDAL", target_date=target,
            actual_tmax_c=15.0, side="NO",
            bracket_low=63.5, bracket_high=None, unit="F",
        )

        resp = client.post(
            "/api/v2/admin/resolve-pending",
            json={"target_date": target, "station": "KDAL"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "dry_run"
        assert body["total_resolved"] == 0
        # Bet row stays PENDING — dry-run wrote nothing.
        conn = get_connection(str(db))
        try:
            row = conn.execute(
                "SELECT outcome FROM ledger WHERE id=?", (bet_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["outcome"] == "PENDING"
        # Preview entries match the production outcome shape.
        bets = body["groups"][0]["bets"]
        assert len(bets) == 1
        # 15°C → 59°F < 63.5 floor → NO wins.
        assert bets[0]["outcome"] == "WIN"

    def test_commit_resolves_when_past_threshold(self, db, client):
        from datetime import date, timedelta
        target = (date.today() - timedelta(days=2)).isoformat()
        bet_id = self._seed(
            db, station_id="KDAL", target_date=target,
            actual_tmax_c=15.0, side="NO",
            bracket_low=63.5, bracket_high=None, unit="F",
        )

        resp = client.post(
            "/api/v2/admin/resolve-pending",
            json={"target_date": target, "station": "KDAL", "commit": True,
                  "reason": "Polymarket archived"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "commit"
        assert body["total_resolved"] == 1
        # Bet flipped to WIN with the WU fallback source AND the audit reason
        # stamped into event_detail.
        import json as _json
        conn = get_connection(str(db))
        try:
            row = conn.execute(
                "SELECT outcome, event_detail FROM ledger WHERE id=?", (bet_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["outcome"] == "WIN"
        detail = _json.loads(row["event_detail"])
        assert detail["resolution_source"] == "wu_actual_fallback"
        assert detail["manual_resolution_reason"] == "Polymarket archived"
        assert detail["manual_resolution_actor"] == "dashboard-admin"

    def test_commit_uses_limit_price_and_stamps_only_requested_rows(self, db, client):
        from datetime import date, timedelta
        import json as _json

        target = (date.today() - timedelta(days=2)).isoformat()
        bet_id = self._seed(
            db, station_id="KDAL", target_date=target,
            actual_tmax_c=15.0, side="NO",
            fill_price=None, limit_price=0.30,
            bracket_low=63.5, bracket_high=None, unit="F",
        )

        conn = get_connection(str(db))
        try:
            cur = conn.execute(
                """INSERT INTO ledger
                (bet_ts, station_id, market_id, token_id, target_date,
                 horizon, threshold, side, p_model, p_market, edge,
                 kelly_size, volume_cap, bet_size, limit_price,
                 fill_price, fill_size,
                 outcome, event_type, pnl, event_detail)
                VALUES (?, 'KDAL', 'old-m', 'old-tok', ?, 1, 63.5, 'NO',
                 0.45, 0.30, 0.15, 5.0, 5000.0, 5.0, 0.30, 0.30, 16.67,
                 'WIN', 'bet', 1.0, ?)""",
                (
                    f"{target}T07:00:00Z",
                    target,
                    _json.dumps({
                        "bracket_low": 63.5,
                        "bracket_high": None,
                        "resolution_source": "wu_actual_fallback",
                        "manual_resolution_reason": "old reason",
                        "manual_resolution_actor": "old actor",
                    }),
                ),
            )
            old_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()

        resp = client.post(
            "/api/v2/admin/resolve-pending",
            json={
                "target_date": target,
                "station": "KDAL",
                "commit": True,
                "reason": "fresh operator request",
            },
            headers={"X-Operator-Actor": "agent-d"},
        )
        assert resp.status_code == 200
        assert resp.json()["total_resolved"] == 1

        conn = get_connection(str(db))
        try:
            rows = conn.execute(
                "SELECT id, outcome, pnl, event_detail FROM ledger "
                "WHERE id IN (?, ?) ORDER BY id",
                (bet_id, old_id),
            ).fetchall()
        finally:
            conn.close()
        by_id = {int(r["id"]): r for r in rows}

        patched = by_id[bet_id]
        assert patched["outcome"] == "WIN"
        assert patched["pnl"] > 0
        patched_detail = _json.loads(patched["event_detail"])
        assert patched_detail["manual_resolution_reason"] == "fresh operator request"
        assert patched_detail["manual_resolution_actor"] == "agent-d"

        old_detail = _json.loads(by_id[old_id]["event_detail"])
        assert old_detail["manual_resolution_reason"] == "old reason"
        assert old_detail["manual_resolution_actor"] == "old actor"

    def test_commit_refused_before_threshold_without_force(self, db, client):
        from datetime import date
        # target = today → days_past=0 < POLYMARKET_FALLBACK_DAYS=1.
        target = date.today().isoformat()
        bet_id = self._seed(
            db, station_id="KDAL", target_date=target,
            actual_tmax_c=15.0, unit="F",
        )

        resp = client.post(
            "/api/v2/admin/resolve-pending",
            json={"target_date": target, "station": "KDAL", "commit": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_resolved"] == 0
        assert "refused" in body["groups"][0]
        # Bet still PENDING.
        conn = get_connection(str(db))
        try:
            row = conn.execute(
                "SELECT outcome FROM ledger WHERE id=?", (bet_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["outcome"] == "PENDING"

    def test_commit_with_force_overrides_threshold(self, db, client):
        from datetime import date
        target = date.today().isoformat()
        bet_id = self._seed(
            db, station_id="KDAL", target_date=target,
            actual_tmax_c=15.0, side="NO",
            bracket_low=63.5, bracket_high=None, unit="F",
        )

        resp = client.post(
            "/api/v2/admin/resolve-pending",
            json={"target_date": target, "station": "KDAL",
                  "commit": True, "force": True},
        )
        assert resp.status_code == 200
        assert resp.json()["total_resolved"] == 1
        conn = get_connection(str(db))
        try:
            row = conn.execute(
                "SELECT outcome FROM ledger WHERE id=?", (bet_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["outcome"] == "WIN"

    def test_actuals_source_filter_rejects_non_wu(self, db, client):
        """Defense-in-depth (finding #25): a non-WU actuals row must NOT
        unblock the fallback even though the row exists for the (station,
        date).
        """
        from datetime import date, timedelta
        target = (date.today() - timedelta(days=2)).isoformat()
        bet_id = self._seed(
            db, station_id="KDAL", target_date=target,
            actual_tmax_c=15.0, unit="F",
            actual_source="ncei",  # NOT in SUPPORTED_LIVE_SOURCES
        )

        resp = client.post(
            "/api/v2/admin/resolve-pending",
            json={"target_date": target, "station": "KDAL", "commit": True},
        )
        body = resp.json()
        # No WU row visible to the source-filtered read → refused.
        assert body["total_resolved"] == 0
        assert "no WU actual" in body["groups"][0]["refused"]
        conn = get_connection(str(db))
        try:
            row = conn.execute(
                "SELECT outcome FROM ledger WHERE id=?", (bet_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["outcome"] == "PENDING"

    def test_empty_target_returns_empty_groups(self, db, client):
        from datetime import date, timedelta
        target = (date.today() - timedelta(days=2)).isoformat()
        resp = client.post(
            "/api/v2/admin/resolve-pending",
            json={"target_date": target},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "dry_run"
        assert body["groups"] == []
        assert body["total_resolved"] == 0
