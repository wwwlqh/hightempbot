"""Tests for Unit 9: Per-Station Scanning Scheduler."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from unittest.mock import MagicMock, patch

import pytest

from hightempbot.db.connection import get_connection, init_db
from hightempbot.scheduler.betting_tick import run_betting_tick
from hightempbot.scheduler.market_data import _fetch_ensemble, _fetch_market_data
from hightempbot.resolution.settler import (
    _resolve_via_wu_actual_fallback,
    _resolve_station_date,
    run_resolution_tick,
)
from hightempbot.scheduler.station_scanner import (
    _last_run_ensemble_ready,
    _target_date_for_ready_cycle,
)


@dataclass
class MockStation:
    station_id: str
    unit: str = "F"
    timezone: str = "US/Central"
    lat: float = 32.85
    lon: float = -96.85
    city: str = "Dallas"
    icao: str = "KDAL"
    resolution_source: str = "wu"


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "test.db")
    conn = init_db(path)
    # ce-code-review P3 #70: schema.sql now seeds STOPPED_PROCESSING (safe halt
    # on fresh installs). Station-scanner tests exercise the betting tick under
    # an enabled operator state, so seed LIVE here.
    from tests.conftest import seed_operator_live
    seed_operator_live(conn)
    conn.close()
    return path


def _seed_fresh_lut(conn: sqlite3.Connection, station_id: str) -> None:
    """Insert a minimal lut_bucket_stats row with refreshed_at=now.

    Required by tests that want to exercise gates AFTER the stale-LUT gate.
    """
    from datetime import datetime as _dt, timezone as _tz
    now_sql = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT OR REPLACE INTO lut_bucket_stats "
        "(station_id, pred_bucket_low, pred_bucket_high, n, hits, "
        " observed, mean_pred, refreshed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (station_id, 0.40, 0.60, 200, 90, 0.45, 0.50, now_sql),
    )
    conn.commit()


def _seed_actual(conn: sqlite3.Connection, station_id: str, local_date: str = "2026-04-06") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO actuals (station_id, local_date, tmax_celsius, source) "
        "VALUES (?, ?, ?, ?)",
        (station_id, local_date, 20.0, "wu"),
    )
    conn.commit()


def _insert_pending_bet(
    conn, station_id="KDAL", target_date="2026-04-07",
    side="YES", token_id="yes_tok_1", fill_price=0.30, bet_size=10.0,
    bracket_low=61.5, bracket_high=63.5, event_type="bet",
):
    """Insert a PENDING bet into the ledger with bracket bounds in event_detail.

    Stores fill_price + fill_size so fee accounting (which reads those
    columns) sees the same shape production-filled rows have.
    """
    import json
    event_detail = json.dumps({
        "bracket_low": bracket_low,
        "bracket_high": bracket_high,
    })
    fill_size = bet_size / fill_price if fill_price else None
    conn.execute(
        """INSERT INTO ledger
        (bet_ts, station_id, market_id, token_id, target_date,
         horizon, threshold, side, p_model, p_market, edge,
         kelly_size, volume_cap, bet_size, limit_price,
         fill_price, fill_size,
         outcome, event_type, event_detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("2026-04-06T08:00:00Z", station_id, "m", token_id, target_date,
         1, bracket_high or bracket_low or 0.0, side, 0.45, 0.30, 0.15,
         bet_size, 5000.0, bet_size, fill_price,
         fill_price, fill_size,
         "PENDING", event_type, event_detail),
    )
    conn.commit()


def _make_resolved_market_data(winning_idx=3):
    """Create market data where bracket `winning_idx` has resolved (price ~1.0).

    Bounds use continuous [lo, hi) semantics produced by parse_bracket_bounds:
      - floor "<60°F" → (None, 59.5)
      - interior "60-61°F" → (59.5, 61.5)  (2°F-wide)
      - ceiling "≥78°F" → (77.5, None)

    Returns (market_data, winning_bracket_bounds).
    """
    base = 59
    result = {}
    for i in range(11):
        mkt = {
            "best_ask": 0.01,  # most brackets have near-zero price
            "volume24hr": 5000.0,
            "market_id": f"m_{i}",
            "token_id": f"yes_tok_{i}",
            "no_token_id": f"no_tok_{i}",
        }
        if i == 0:
            mkt["bracket_low"] = None
            mkt["bracket_high"] = base + 0.5  # "<60°F" → actual < 59.5... wait: base=59, so "<59°F" bracket has hi=59.5
        elif i == 10:
            mkt["bracket_low"] = (base + 1 + (i - 1) * 2) - 0.5
            mkt["bracket_high"] = None
        else:
            lo = base + 1 + (i - 1) * 2
            mkt["bracket_low"] = float(lo) - 0.5
            mkt["bracket_high"] = float(lo + 1) + 0.5

        if i == winning_idx:
            mkt["best_ask"] = 0.99  # triggers CLOB verification
        result[i] = mkt

    winning = result[winning_idx]
    return result, (winning["bracket_low"], winning["bracket_high"])


def _mock_clob_resolved(winning_token="yes_tok_3"):
    """Create a mock ClobReader that returns price=0.998 only for the winning token."""
    mock_reader = MagicMock()

    def _fetch_book(token_id):
        if token_id == winning_token:
            return {"bids": [{"price": "0.998", "size": "100"}], "asks": []}
        return {"bids": [{"price": "0.01", "size": "100"}], "asks": []}

    mock_reader.fetch_order_book = _fetch_book
    mock_reader.best_bid = lambda book: (float(book["bids"][0]["price"]), 100.0) if book.get("bids") else None
    mock_reader.best_ask = lambda book: None
    return mock_reader


def _mock_clob_below_threshold(price="0.994"):
    """Create a mock ClobReader whose bids never reach the resolution threshold."""
    mock_reader = MagicMock()
    mock_reader.fetch_order_book.return_value = {"bids": [{"price": price, "size": "100"}], "asks": []}
    mock_reader.best_bid = lambda book: (float(book["bids"][0]["price"]), 100.0) if book.get("bids") else None
    mock_reader.best_ask = lambda book: None
    return mock_reader


def _mock_clob_token_bid(token_id, price, size=100):
    """Create a mock ClobReader with a specific bid on one bought token."""
    mock_reader = MagicMock()

    def _fetch_book(book_token):
        bid_price = str(price) if book_token == token_id else "0.20"
        bid_size = str(size) if book_token == token_id else "100"
        return {"bids": [{"price": bid_price, "size": bid_size}], "asks": []}

    def _fetch_price(price_token, side="buy"):
        if price_token == token_id and side == "sell":
            return float(price)
        return None

    mock_reader.fetch_order_book = _fetch_book
    mock_reader.fetch_price = _fetch_price
    mock_reader.best_bid = lambda book: (float(book["bids"][0]["price"]), 100.0) if book.get("bids") else None
    mock_reader.best_ask = lambda book: None
    return mock_reader


def _mock_clob_terminal_low(losing_token="yes_tok_5", price="0.005"):
    """Create a mock ClobReader that returns a near-zero YES ask for one bracket."""
    mock_reader = MagicMock()

    def _fetch_book(token_id):
        if token_id == losing_token:
            return {
                "bids": [{"price": "0.001", "size": "100"}],
                "asks": [{"price": price, "size": "100"}],
            }
        return {
            "bids": [{"price": "0.20", "size": "100"}],
            "asks": [{"price": "0.25", "size": "100"}],
        }

    mock_reader.fetch_order_book = _fetch_book
    mock_reader.best_bid = lambda book: (float(book["bids"][0]["price"]), 100.0) if book.get("bids") else None
    mock_reader.best_ask = lambda book: (float(book["asks"][0]["price"]), 100.0) if book.get("asks") else None
    return mock_reader


class TestTargetDateForReadyCycle:
    def test_uses_utc_cycle_date(self):
        import pytz

        now_utc = pytz.utc.localize(datetime(2026, 5, 1, 4, 0))

        assert _target_date_for_ready_cycle(now_utc) == date(2026, 5, 1)


class TestLastRunEnsembleReady:
    def setup_method(self):
        from hightempbot.scheduler import station_scanner

        station_scanner._last_run_ready_cache.clear()
        station_scanner._last_run_probe_at.clear()

    @staticmethod
    def _ts(dt: datetime) -> int:
        import pytz

        return int(pytz.utc.localize(dt).timestamp())

    def test_accepts_day_n_12z_models(self):
        import pytz

        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "last_run_initialisation_time": self._ts(datetime(2026, 5, 2, 12, 0)),
            "last_run_availability_time": self._ts(datetime(2026, 5, 2, 20, 30)),
        }

        with patch("hightempbot.ingestion.openmeteo_forecast.MODELS", ["ecmwf_ifs025", "ukmo_seamless"]), \
             patch("requests.get", return_value=response):
            ready, missing = _last_run_ensemble_ready(
                pytz.utc.localize(datetime(2026, 5, 3, 1, 30))
            )

        assert ready is True
        assert missing == ""

    def test_rejects_runs_before_day_n(self):
        import pytz

        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "last_run_initialisation_time": self._ts(datetime(2026, 5, 1, 12, 0)),
            "last_run_availability_time": self._ts(datetime(2026, 5, 1, 20, 30)),
        }

        with patch("hightempbot.ingestion.openmeteo_forecast.MODELS", ["ecmwf_ifs025"]), \
             patch("requests.get", return_value=response):
            ready, missing = _last_run_ensemble_ready(
                pytz.utc.localize(datetime(2026, 5, 3, 1, 30))
            )

        assert ready is False
        assert missing == "ecmwf_ifs025"


class TestBettingTick:
    @pytest.fixture(autouse=True)
    def _bypass_last_run_gate(self):
        """Mock the readiness probe so unit tests don't hit Open-Meteo metadata."""
        with patch(
            "hightempbot.scheduler.betting_tick._last_run_ensemble_ready",
            return_value=(True, ""),
        ):
            yield

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.scheduler.betting_tick._fetch_ensemble")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    def test_ensemble_readiness_gate_prevents_market_work(
        self,
        mock_market,
        mock_ens,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        db_path,
    ):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        with patch(
            "hightempbot.scheduler.betting_tick._last_run_ensemble_ready",
            return_value=(False, "gfs_seamless"),
        ):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)

        mock_market.assert_not_called()
        mock_ens.assert_not_called()

    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.scheduler.betting_tick._fetch_ensemble", return_value={})
    @patch("hightempbot.scheduler.betting_tick._attempt_seed_missing_lut")
    def test_no_lut_attempts_self_heal_before_skipping(self, mock_seed_lut, mock_ens, mock_dt, db_path):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mock_seed_lut.return_value = False

        conn = get_connection(db_path)
        try:
            current = date(2024, 3, 1)
            while current <= date(2026, 4, 7):
                conn.execute(
                    "INSERT OR REPLACE INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, 'wu')",
                    ("KDAL", current.isoformat(), 20.0),
                )
                current += timedelta(days=1)
            conn.commit()
        finally:
            conn.close()

        run_betting_tick(station, db_path, 1000.0, dry_run=True)
        mock_seed_lut.assert_called_once()

    # Note: production has BETTING_LOCAL_CUTOFF_HOUR = 0 (disabled) — the WU
    # consensus gate provides freshness safety per-bet. This test pins the
    # cutoff to 14:00 so the cutoff *logic itself* remains exercised.
    @patch("hightempbot.execution.strategy_constants.BETTING_LOCAL_CUTOFF_HOUR", 14)
    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.scheduler.betting_tick._fetch_ensemble")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    @pytest.mark.parametrize("local_time", [datetime(2026, 4, 7, 14, 0), datetime(2026, 4, 7, 15, 15)])
    def test_local_cutoff_skips_new_bets_and_logs(
        self,
        mock_market,
        mock_ens,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        local_time,
        db_path,
    ):
        station = MockStation("KDAL")
        mock_dt.now.return_value = local_time
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        run_betting_tick(station, db_path, 1000.0, dry_run=True)
        mock_market.assert_not_called()
        mock_ens.assert_not_called()

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status, message FROM pipeline_health WHERE station_id = ? AND stage = 'gates' ORDER BY id DESC LIMIT 1",
            ("KDAL",),
        ).fetchone()
        conn.close()
        assert row["status"] == "SKIP"
        assert "Betting cutoff reached" in row["message"]
        assert "after 14:00 local" in row["message"]

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data", return_value={})
    def test_before_local_cutoff_allows_market_work(
        self,
        mock_market,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        db_path,
    ):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 45)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        conn = get_connection(db_path)
        try:
            _seed_fresh_lut(conn, "KDAL")
            _seed_actual(conn, "KDAL")
        finally:
            conn.close()

        with patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)

        mock_market.assert_called_once()

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.scheduler.betting_tick._fetch_ensemble")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    def test_outside_strategy_entry_hours_skips_market_work(
        self,
        mock_market,
        mock_ens,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        db_path,
    ):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 8, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        run_betting_tick(station, db_path, 1000.0, dry_run=True)

        mock_market.assert_not_called()
        mock_ens.assert_not_called()
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status, message FROM pipeline_health WHERE station_id = ? AND stage = 'gates' ORDER BY id DESC LIMIT 1",
            ("KDAL",),
        ).fetchone()
        conn.close()
        assert row["status"] == "SKIP"
        assert "No enabled strategy entry window" in row["message"]

    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.scheduler.betting_tick._fetch_ensemble", return_value={})
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data", return_value={0: {"best_ask": 0.3}})
    def test_no_ensemble_skips(self, mock_market, mock_ens, mock_dt, db_path):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        run_betting_tick(station, db_path, 1000.0, dry_run=True)
        # Market found, but no ensemble → graceful skip, no bet placed
        conn = get_connection(db_path)
        bets = conn.execute("SELECT COUNT(*) as cnt FROM ledger").fetchone()["cnt"]
        conn.close()
        assert bets == 0

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.execution.pipeline.run_betting_cycle")
    @patch("hightempbot.scheduler.betting_tick._fetch_ensemble")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    @patch("hightempbot.execution.walker.ClobReader")
    def test_max_per_market_skips(
        self,
        mock_reader_cls,
        mock_market,
        mock_ens,
        mock_cycle,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        db_path,
    ):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        mock_market.return_value = {
            0: {
                "token_id": "yes_today",
                "no_token_id": "no_today",
                "best_ask": 0.30,
                "market_id": "m_today",
                "volume24hr": 5000.0,
            },
        }
        mock_reader = MagicMock()
        mock_reader.fetch_price.side_effect = lambda token_id, side="buy": {
            "yes_today": 0.35,
            "no_today": 0.55,
        }.get(token_id)
        mock_reader.fetch_order_book.return_value = None
        mock_reader.best_ask.return_value = None
        mock_reader_cls.return_value = mock_reader

        conn = get_connection(db_path)
        _seed_fresh_lut(conn, "KDAL")
        # Seed MAX bets for the readiness-cycle target date.
        for i in range(3):
            conn.execute(
                """INSERT INTO ledger
                (bet_ts, station_id, market_id, token_id, target_date,
                 horizon, threshold, side, p_model, p_market, edge,
                 kelly_size, volume_cap, bet_size, limit_price,
                 outcome, event_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"2026-04-07T0{i}:00:00Z", "KDAL", "m", "t", "2026-04-07",
                 1, 68.0, "YES", 0.45, 0.30, 0.15,
                  10.0, 5000.0, 10.0, 0.30, "PENDING", "bet"),
            )
        conn.commit()
        conn.close()

        with patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0), \
             patch("hightempbot.scheduler.betting_tick.MAX_PER_MARKET", 3):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)

        mock_ens.assert_not_called()
        mock_cycle.assert_not_called()

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.execution.pipeline.run_betting_cycle")
    @patch("hightempbot.scheduler.betting_tick._fetch_ensemble")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    @patch("hightempbot.execution.walker.ClobReader")
    def test_skips_when_market_already_resolved_on_polymarket(
        self,
        mock_reader_cls,
        mock_market,
        mock_ens,
        mock_cycle,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        db_path,
    ):
        """An event-level Gamma-resolved target skips before Gamma/CLOB.

        Once the resolution tick stamps a ledger row with
        ``resolution_source='polymarket_gamma_closed'`` and a terminal outcome,
        the bracket winner is pinned. Further betting ticks for the same
        target_date must stop scraping Gamma / CLOB / Open-Meteo for the rest
        of the local day so the scrape budget isn't burned on a settled market.
        """
        import json

        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        del mock_reader_cls  # the gate fires before CLOB enrichment

        conn = get_connection(db_path)
        try:
            _seed_fresh_lut(conn, "KDAL")
            _seed_actual(conn, "KDAL")
            conn.execute(
                """INSERT INTO ledger
                (bet_ts, station_id, market_id, token_id, target_date,
                 horizon, threshold, side, p_model, p_market, edge,
                 kelly_size, volume_cap, bet_size, limit_price,
                 fill_price, fill_size,
                 outcome, event_type, event_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "2026-04-07T01:00:00Z", "KDAL", "m", "yes_tok_3", "2026-04-07",
                    1, 65.0, "YES", 0.45, 0.30, 0.15,
                    10.0, 5000.0, 10.0, 0.30,
                    0.30, 33.33,
                    "WIN", "dry_run",
                    json.dumps({
                        "resolution_source": "polymarket_gamma_closed",
                        "resolution_price": 0.998,
                    }),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        with patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)

        mock_market.assert_not_called()
        mock_ens.assert_not_called()
        mock_cycle.assert_not_called()

        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT status, message FROM pipeline_health "
                "WHERE station_id = ? AND stage = 'gates' "
                "ORDER BY id DESC LIMIT 1",
                ("KDAL",),
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "SKIP"
        assert "already resolved on Polymarket" in row["message"]

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.execution.pipeline.run_betting_cycle")
    @patch("hightempbot.scheduler.betting_tick._fetch_ensemble")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    @patch("hightempbot.execution.walker.ClobReader")
    def test_gate_ignores_push_outcome(
        self,
        mock_reader_cls,
        mock_market,
        mock_ens,
        mock_cycle,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        db_path,
    ):
        """PUSH is an accounting downgrade (NULL fill_price), not a market-
        wide resolution — the gate must NOT skip when one bracket carries a
        PUSH outcome (finding #1). Without this guard, a single PUSH on any
        bracket would silence the station for the rest of the day even while
        other brackets are still trading.
        """
        import json
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        del mock_reader_cls

        conn = get_connection(db_path)
        try:
            _seed_fresh_lut(conn, "KDAL")
            _seed_actual(conn, "KDAL")
            conn.execute(
                """INSERT INTO ledger
                (bet_ts, station_id, market_id, token_id, target_date,
                 horizon, threshold, side, p_model, p_market, edge,
                 kelly_size, volume_cap, bet_size, limit_price,
                 fill_price, fill_size,
                 outcome, event_type, event_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "2026-04-07T01:00:00Z", "KDAL", "m", "tok", "2026-04-07",
                    1, 65.0, "YES", 0.45, 0.30, 0.15,
                    10.0, 5000.0, 10.0, 0.0, 0.0, None,
                    "PUSH", "dry_run",
                    json.dumps({
                        "resolution_source": "polymarket_winner",
                        "null_fill_price_push": True,
                    }),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Force the gate to fire by simulating "no ensemble" — mock_market
        # would still be called if the gate let us past. We just need
        # mock_market to be reachable; assert it WAS called means the gate
        # did NOT skip on the PUSH row.
        mock_market.return_value = None
        with patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)
        # Gate did NOT short-circuit on PUSH — market fetch was attempted.
        mock_market.assert_called()

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.execution.pipeline.run_betting_cycle")
    @patch("hightempbot.scheduler.betting_tick._fetch_ensemble")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    @patch("hightempbot.execution.walker.ClobReader")
    def test_gate_ignores_per_bracket_gamma_close(
        self,
        mock_reader_cls,
        mock_market,
        mock_ens,
        mock_cycle,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        db_path,
    ):
        """``polymarket_gamma_closed_bracket`` represents one bracket closing
        (typically a loser when intraday actual passes it). The winning
        bracket is NOT pinned — other brackets are still actively trading.
        The gate must NOT skip on a per-bracket close (finding #2).
        """
        import json
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        del mock_reader_cls

        conn = get_connection(db_path)
        try:
            _seed_fresh_lut(conn, "KDAL")
            _seed_actual(conn, "KDAL")
            conn.execute(
                """INSERT INTO ledger
                (bet_ts, station_id, market_id, token_id, target_date,
                 horizon, threshold, side, p_model, p_market, edge,
                 kelly_size, volume_cap, bet_size, limit_price,
                 fill_price, fill_size,
                 outcome, event_type, event_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "2026-04-07T01:00:00Z", "KDAL", "m", "tok", "2026-04-07",
                    1, 65.0, "YES", 0.45, 0.30, 0.15,
                    10.0, 5000.0, 10.0, 0.30, 0.30, 33.33,
                    "LOSS", "dry_run",
                    json.dumps({
                        "resolution_source": "polymarket_gamma_closed_bracket",
                    }),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        mock_market.return_value = None
        with patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)
        mock_market.assert_called()

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.execution.pipeline.run_betting_cycle")
    @patch("hightempbot.scheduler.betting_tick._fetch_ensemble")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    @patch("hightempbot.execution.walker.ClobReader")
    def test_gate_ignores_wu_actual_fallback(
        self,
        mock_reader_cls,
        mock_market,
        mock_ens,
        mock_cycle,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        db_path,
    ):
        """``wu_actual_fallback`` is a local-only resolution that Polymarket
        might still override later. The gate must NOT treat it as a final
        market resolution (finding #23) — locks the intent that the SQL
        polymarket-only filter is deliberate.
        """
        import json
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        del mock_reader_cls

        conn = get_connection(db_path)
        try:
            _seed_fresh_lut(conn, "KDAL")
            _seed_actual(conn, "KDAL")
            conn.execute(
                """INSERT INTO ledger
                (bet_ts, station_id, market_id, token_id, target_date,
                 horizon, threshold, side, p_model, p_market, edge,
                 kelly_size, volume_cap, bet_size, limit_price,
                 fill_price, fill_size,
                 outcome, event_type, event_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "2026-04-07T01:00:00Z", "KDAL", "m", "tok", "2026-04-07",
                    1, 65.0, "YES", 0.45, 0.30, 0.15,
                    10.0, 5000.0, 10.0, 0.30, 0.30, 33.33,
                    "WIN", "dry_run",
                    json.dumps({
                        "resolution_source": "wu_actual_fallback",
                    }),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        mock_market.return_value = None
        with patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)
        mock_market.assert_called()

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.execution.pipeline.run_betting_cycle")
    @patch("hightempbot.scheduler.betting_tick._fetch_ensemble")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    @patch("hightempbot.execution.walker.ClobReader")
    def test_gate_dry_run_does_not_match_live_resolved_row(
        self,
        mock_reader_cls,
        mock_market,
        mock_ens,
        mock_cycle,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        db_path,
    ):
        """The gate is mode-aware: a stale ``dry_run`` row from yesterday's
        staging test must not block today's LIVE bets, and vice-versa (#11).
        Here a LIVE resolved row exists; the gate fires for live mode but
        NOT for dry_run mode.
        """
        import json
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        del mock_reader_cls

        conn = get_connection(db_path)
        try:
            _seed_fresh_lut(conn, "KDAL")
            _seed_actual(conn, "KDAL")
            # LIVE 'bet' row resolved on Polymarket.
            conn.execute(
                """INSERT INTO ledger
                (bet_ts, station_id, market_id, token_id, target_date,
                 horizon, threshold, side, p_model, p_market, edge,
                 kelly_size, volume_cap, bet_size, limit_price,
                 fill_price, fill_size,
                 outcome, event_type, event_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "2026-04-07T01:00:00Z", "KDAL", "m", "tok", "2026-04-07",
                    1, 65.0, "YES", 0.45, 0.30, 0.15,
                    10.0, 5000.0, 10.0, 0.30, 0.30, 33.33,
                    "WIN", "bet",
                    json.dumps({
                        "resolution_source": "polymarket_winner",
                    }),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Dry-run mode: the live row should NOT block the dry_run tick.
        mock_market.return_value = None
        with patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)
        mock_market.assert_called()

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.execution.pipeline.run_betting_cycle")
    @patch(
        "hightempbot.scheduler.betting_tick._fetch_ensemble",
    )
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    @patch("hightempbot.execution.walker.ClobReader")
    def test_skips_when_local_date_before_target_day(
        self,
        mock_reader_cls,
        mock_market,
        mock_ens,
        mock_cycle,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        db_path,
    ):
        """Trading window is full local calendar day of target_date.

        Was: bot traded the UTC cycle target (e.g. May 1) even when the
        station's local clock was still on Apr 30. New spec (operator
        2026-05-07): trade only when ``local_date == target_date``. Stations
        west of UTC have to wait until local midnight.
        """
        import pytz
        station = MockStation("KDAL")
        # Dallas Apr 30 23:00 (UTC May 1 04:00). target = May 1 (UTC date).
        utc_now = pytz.utc.localize(datetime(2026, 5, 1, 4, 0))
        mock_dt.now.side_effect = lambda tz=None: (
            utc_now.astimezone(tz) if tz is not None else utc_now.replace(tzinfo=None)
        )
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        del mock_reader_cls, mock_ens, mock_cycle  # unused under skip path

        conn = get_connection(db_path)
        _seed_fresh_lut(conn, "KDAL")
        conn.commit()
        conn.close()

        with patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0), \
             patch("hightempbot.scheduler.betting_tick.MAX_PER_MARKET", 3):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)

        # Strict gate: local Apr 30 != target May 1 → skip before market fetch.
        assert mock_market.call_count == 0

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.scheduler.betting_tick._fetch_ensemble")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    def test_local_date_after_cycle_target_skips_without_n_plus_2_fallback(
        self,
        mock_market,
        mock_ens,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        db_path,
    ):
        import pytz

        station = MockStation(
            "NZWN",
            icao="NZWN",
            timezone="Pacific/Auckland",
            unit="C",
        )
        utc_now = pytz.utc.localize(datetime(2026, 5, 1, 14, 0))
        mock_dt.now.side_effect = lambda tz=None: (
            utc_now.astimezone(tz) if tz is not None else utc_now.replace(tzinfo=None)
        )
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        run_betting_tick(station, db_path, 1000.0, dry_run=True)

        mock_market.assert_not_called()
        mock_ens.assert_not_called()
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT status, message FROM pipeline_health "
                "WHERE station_id = ? AND stage = 'gates' ORDER BY id DESC LIMIT 1",
                ("NZWN",),
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "SKIP"
        # Message format updated when the trading-window gate became
        # bidirectional (was "already past locally", now "Outside trading
        # window: ... (past)"). Either form is acceptable as long as the
        # SKIP captures the past-window case.
        assert "Outside trading window" in row["message"]
        assert "(past)" in row["message"]

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    def test_coverage_gate_skip_is_logged_for_dashboard(self, mock_dt, _mock_rand, _mock_sleep, db_path):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        run_betting_tick(station, db_path, 1000.0, dry_run=True)

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status, message FROM pipeline_health WHERE station_id = ? AND stage = 'gates' ORDER BY id DESC LIMIT 1",
            ("KDAL",),
        ).fetchone()
        conn.close()

        assert row["status"] == "SKIP"
        assert "Coverage" in row["message"]

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.scheduler.betting_tick._attempt_seed_missing_lut")
    def test_coverage_gate_ignores_non_wu_actuals(
        self, mock_seed_lut, mock_dt, _mock_rand, _mock_sleep, db_path,
    ):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        conn = get_connection(db_path)
        try:
            current = date(2024, 3, 1)
            while current <= date(2026, 4, 7):
                conn.execute(
                    "INSERT OR REPLACE INTO actuals "
                    "(station_id, local_date, tmax_celsius, source) "
                    "VALUES (?, ?, ?, ?)",
                    ("KDAL", current.isoformat(), 20.0, "ncei"),
                )
                current += timedelta(days=1)
            conn.commit()
        finally:
            conn.close()

        run_betting_tick(station, db_path, 1000.0, dry_run=True)

        mock_seed_lut.assert_not_called()
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status, message FROM pipeline_health "
            "WHERE station_id = ? AND stage = 'gates' ORDER BY id DESC LIMIT 1",
            ("KDAL",),
        ).fetchone()
        conn.close()
        assert row["status"] == "SKIP"
        assert "Coverage" in row["message"]

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    def test_freshness_gate_ignores_non_wu_actuals(
        self, mock_market, mock_dt, _mock_rand, _mock_sleep, db_path,
    ):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        conn = get_connection(db_path)
        try:
            _seed_fresh_lut(conn, "KDAL")
            conn.execute(
                "INSERT INTO actuals "
                "(station_id, local_date, tmax_celsius, source) "
                "VALUES (?, ?, ?, ?)",
                ("KDAL", "2026-04-07", 20.0, "ncei"),
            )
            conn.commit()
        finally:
            conn.close()

        with patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)

        mock_market.assert_not_called()
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status, message FROM pipeline_health "
            "WHERE station_id = ? AND stage = 'gates' ORDER BY id DESC LIMIT 1",
            ("KDAL",),
        ).fetchone()
        conn.close()
        assert row["status"] == "SKIP"
        assert row["message"] == "No actuals on file"

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.scheduler.station_healing._fetch_market_data")
    def test_stale_lut_gate_fires_when_lut_missing(
        self, mock_market, mock_dt, _mock_rand, _mock_sleep, db_path,
    ):
        """Cold-start: no lut_bucket_stats row → SKIP before market fetch.

        Patches `station_healing._fetch_market_data` (not the betting_tick
        binding) because the single call we expect comes from
        `_attempt_seed_missing_lut`, which lives in `station_healing.py`
        post-U11 and reads its module-local binding.
        """
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        # Satisfy coverage gate so stale-LUT is the one that fires.
        conn = get_connection(db_path)
        conn.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
            ("KDAL", "2026-04-07", 20.0, "wu"),
        )
        conn.commit()
        conn.close()

        with patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)

        assert mock_market.call_count == 1  # target-date self-heal attempt before skip
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status, message FROM pipeline_health "
            "WHERE station_id = ? AND stage = 'gates' ORDER BY id DESC LIMIT 1",
            ("KDAL",),
        ).fetchone()
        conn.close()
        assert row["status"] == "SKIP"
        assert "not seeded" in row["message"]

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    def test_stale_lut_gate_fires_when_refreshed_too_old(
        self, mock_market, mock_dt, _mock_rand, _mock_sleep, db_path,
    ):
        """refreshed_at older than LUT_STALE_HOURS → SKIP with age in message."""
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        conn = get_connection(db_path)
        conn.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
            ("KDAL", "2026-04-07", 20.0, "wu"),
        )
        # LUT row with refreshed_at = 40h ago (>36h threshold).
        stale = (_dt.now(_tz.utc) - _td(hours=40)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO lut_bucket_stats "
            "(station_id, pred_bucket_low, pred_bucket_high, n, hits, "
            " observed, mean_pred, refreshed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("KDAL", 0.40, 0.60, 100, 45, 0.45, 0.50, stale),
        )
        conn.commit()
        conn.close()

        with patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)

        mock_market.assert_not_called()
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status, message FROM pipeline_health "
            "WHERE station_id = ? AND stage = 'gates' ORDER BY id DESC LIMIT 1",
            ("KDAL",),
        ).fetchone()
        conn.close()
        assert row["status"] == "SKIP"
        assert "stale" in row["message"]

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.scheduler.betting_tick._fetch_ensemble", return_value={"gfs_seamless": 25.0})
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data")
    @patch("hightempbot.execution.walker.ClobReader")
    def test_missing_orderbooks_are_logged_for_dashboard(
        self,
        mock_reader_cls,
        mock_market,
        _mock_ens,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        db_path,
    ):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mock_market.return_value = {
            0: {"token_id": "yes_tok", "no_token_id": "no_tok", "best_ask": 0.3},
        }

        conn = get_connection(db_path)
        conn.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
            ("KDAL", "2026-04-07", 20.0, "wu"),
        )
        _seed_fresh_lut(conn, "KDAL")
        conn.commit()
        conn.close()

        mock_reader = MagicMock()
        mock_reader.fetch_order_book.return_value = None
        mock_reader.best_ask.return_value = None
        mock_reader.fetch_price.return_value = None
        mock_reader_cls.return_value = mock_reader

        with patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status, message FROM pipeline_health WHERE station_id = ? AND stage = 'clob' ORDER BY id DESC LIMIT 1",
            ("KDAL",),
        ).fetchone()
        conn.close()

        assert row["status"] == "ERROR"
        assert "tokens missing" in row["message"]

    @patch("hightempbot.scheduler.betting_tick.time.sleep", return_value=None)
    @patch("hightempbot.scheduler.betting_tick.random.uniform", return_value=0.0)
    @patch("hightempbot.scheduler.betting_tick.datetime")
    @patch("hightempbot.scheduler.betting_tick._fetch_market_data", return_value={})
    def test_no_live_market_is_logged_for_dashboard(
        self,
        mock_market,
        mock_dt,
        _mock_rand,
        _mock_sleep,
        db_path,
    ):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        conn = get_connection(db_path)
        conn.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
            ("KDAL", "2026-04-07", 20.0, "wu"),
        )
        _seed_fresh_lut(conn, "KDAL")
        conn.commit()
        conn.close()

        with patch("hightempbot.execution.strategy_constants.MIN_COVERAGE_PCT", 0.0):
            run_betting_tick(station, db_path, 1000.0, dry_run=True)

        assert mock_market.call_count == 1
        assert mock_market.call_args.args[1].isoformat() == "2026-04-07"

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status, message FROM pipeline_health WHERE station_id = ? AND stage = 'market' ORDER BY id DESC LIMIT 1",
            ("KDAL",),
        ).fetchone()
        conn.close()

        assert row["status"] == "WARNING"
        assert row["message"] == "No live market for 2026-04-07"


class TestFetchEnsemble:
    def _clear_ensemble_state(self) -> None:
        from hightempbot.scheduler import market_data

        market_data._ensemble_cache.clear()
        market_data._ensemble_negative_cache.clear()
        market_data._ensemble_inflight_locks.clear()

    @patch("hightempbot.ingestion.openmeteo_forecast.fetch_live")
    @patch("hightempbot.scheduler.market_data.datetime")
    def test_fetches_enough_forecast_days_for_target_date(self, mock_dt, mock_fetch_live):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        self._clear_ensemble_state()
        mock_fetch_live.return_value = {
            datetime(2026, 4, 8).date(): {"gfs_seamless": 25.0},
        }

        result = _fetch_ensemble(station, target_date=datetime(2026, 4, 8).date())

        assert result == {"gfs_seamless": 25.0}
        assert mock_fetch_live.call_args.kwargs["forecast_days"] >= 2

    @patch("hightempbot.ingestion.openmeteo_forecast.fetch_live")
    @patch("hightempbot.scheduler.market_data.datetime")
    def test_cache_is_scoped_by_target_date(self, mock_dt, mock_fetch_live):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        self._clear_ensemble_state()
        mock_fetch_live.side_effect = [
            {datetime(2026, 4, 7).date(): {"gfs_seamless": 24.0}},
            {datetime(2026, 4, 8).date(): {"gfs_seamless": 27.0}},
        ]

        today = _fetch_ensemble(station, target_date=datetime(2026, 4, 7).date())
        tomorrow = _fetch_ensemble(station, target_date=datetime(2026, 4, 8).date())

        assert today == {"gfs_seamless": 24.0}
        assert tomorrow == {"gfs_seamless": 27.0}
        assert mock_fetch_live.call_count == 2

    @patch("hightempbot.ingestion.openmeteo_forecast.fetch_live")
    @patch("hightempbot.scheduler.market_data.datetime")
    def test_failed_fetch_uses_short_negative_cache(self, mock_dt, mock_fetch_live):
        station = MockStation("KDAL")
        target_date = datetime(2026, 4, 7).date()
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        self._clear_ensemble_state()
        mock_fetch_live.return_value = None

        first = _fetch_ensemble(station, target_date=target_date)
        second = _fetch_ensemble(station, target_date=target_date)

        assert first == {}
        assert second == {}
        assert mock_fetch_live.call_count == 1

    @patch("hightempbot.ingestion.openmeteo_forecast.fetch_live")
    @patch("hightempbot.scheduler.market_data.datetime")
    def test_expired_negative_cache_retries_fetch(self, mock_dt, mock_fetch_live):
        station = MockStation("KDAL")
        target_date = datetime(2026, 4, 7).date()
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        self._clear_ensemble_state()
        mock_fetch_live.side_effect = [
            None,
            {target_date: {"gfs_seamless": 25.0}},
        ]

        first = _fetch_ensemble(station, target_date=target_date)
        from hightempbot.scheduler import market_data

        key = (station.icao, target_date)
        market_data._ensemble_negative_cache[key] -= market_data._ENSEMBLE_NEGATIVE_TTL + 1
        second = _fetch_ensemble(station, target_date=target_date)

        assert first == {}
        assert second == {"gfs_seamless": 25.0}
        assert mock_fetch_live.call_count == 2

    @patch("hightempbot.ingestion.openmeteo_forecast.fetch_live")
    @patch("hightempbot.scheduler.market_data.datetime")
    def test_same_station_target_date_uses_singleflight_fetch(self, mock_dt, mock_fetch_live):
        station = MockStation("KDAL")
        target_date = datetime(2026, 4, 7).date()
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        self._clear_ensemble_state()

        fetch_started = Event()
        release_fetch = Event()
        errors: list[BaseException] = []
        results: list[dict[str, float]] = []

        def _fake_fetch_live(_station, *, forecast_days):
            fetch_started.set()
            assert release_fetch.wait(2)
            return {target_date: {"gfs_seamless": 25.0}}

        def _call_fetch():
            try:
                results.append(_fetch_ensemble(station, target_date=target_date))
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        mock_fetch_live.side_effect = _fake_fetch_live
        first = Thread(target=_call_fetch)
        second = Thread(target=_call_fetch)

        first.start()
        assert fetch_started.wait(1)
        second.start()
        release_fetch.set()
        first.join(2)
        second.join(2)

        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []
        assert results == [{"gfs_seamless": 25.0}, {"gfs_seamless": 25.0}]
        assert mock_fetch_live.call_count == 1

    @patch("hightempbot.ingestion.openmeteo_forecast.fetch_live")
    @patch("hightempbot.scheduler.market_data.datetime")
    def test_other_station_fetch_does_not_wait_on_inflight_fetch(self, mock_dt, mock_fetch_live):
        target_date = datetime(2026, 4, 7).date()
        blocked_station = MockStation("KDAL")
        other_station = MockStation("KXYZ", icao="KXYZ")
        mock_dt.now.return_value = datetime(2026, 4, 7, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        self._clear_ensemble_state()

        fetch_started = Event()
        release_fetch = Event()
        first_errors: list[BaseException] = []
        second_errors: list[BaseException] = []
        first_result: dict[str, float] = {}
        second_result: dict[str, float] = {}

        def _fake_fetch_live(station, *, forecast_days):
            if station.icao == "KDAL":
                fetch_started.set()
                assert release_fetch.wait(2)
                return {target_date: {"gfs_seamless": 25.0}}
            return {target_date: {"gfs_seamless": 29.0}}

        def _call_blocked():
            nonlocal first_result
            try:
                first_result = _fetch_ensemble(blocked_station, target_date=target_date)
            except BaseException as exc:  # pragma: no cover - asserted below
                first_errors.append(exc)

        def _call_other():
            nonlocal second_result
            try:
                second_result = _fetch_ensemble(other_station, target_date=target_date)
            except BaseException as exc:  # pragma: no cover - asserted below
                second_errors.append(exc)

        mock_fetch_live.side_effect = _fake_fetch_live
        first = Thread(target=_call_blocked)
        second = Thread(target=_call_other)

        first.start()
        assert fetch_started.wait(1)
        second.start()
        second.join(0.5)
        try:
            assert not second.is_alive()
            assert second_errors == []
            assert second_result == {"gfs_seamless": 29.0}
        finally:
            release_fetch.set()
            first.join(2)
            second.join(2)

        assert not first.is_alive()
        assert first_errors == []
        assert first_result == {"gfs_seamless": 25.0}
        assert mock_fetch_live.call_count == 2


class TestResolutionTick:
    @patch("hightempbot.resolution.settler._resolve_station_date")
    @patch("hightempbot.resolution.settler.datetime")
    def test_same_day_market_resolves_after_evening_window(self, mock_dt, mock_resolve, db_path):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 19, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        conn = get_connection(db_path)
        _insert_pending_bet(conn, target_date="2026-04-07")
        conn.close()

        run_resolution_tick(station, db_path)

        mock_resolve.assert_called_once()
        assert mock_resolve.call_args.args[2] == "2026-04-07"

    @patch("hightempbot.resolution.settler._resolve_station_date")
    @patch("hightempbot.resolution.settler.datetime")
    def test_same_day_market_waits_until_evening_window(self, mock_dt, mock_resolve, db_path):
        station = MockStation("KDAL")
        mock_dt.now.return_value = datetime(2026, 4, 7, 10, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        conn = get_connection(db_path)
        _insert_pending_bet(conn, target_date="2026-04-07")
        conn.close()

        run_resolution_tick(station, db_path)

        mock_resolve.assert_not_called()


class TestResolutionPolymarket:
    """Tests for Polymarket price-based resolution (R26-R29).

    These cover the CLOB threshold scan disabled in production via
    EARLY_RESOLUTION_ENABLED=False. Patched True here so the legacy path
    is still exercised and remains documented behavior.
    """

    @pytest.fixture(autouse=True)
    def _enable_early_resolution(self):
        with patch("hightempbot.execution.strategy_constants.EARLY_RESOLUTION_ENABLED", True):
            yield

    def test_yes_bet_on_winning_bracket_is_win(self, db_path):
        """YES bet on the bracket that resolved → WIN."""
        conn = get_connection(db_path)
        market_data, (win_low, win_high) = _make_resolved_market_data(winning_idx=3)

        _insert_pending_bet(conn, bracket_low=win_low, bracket_high=win_high,
                            side="YES", token_id="yes_tok_3")

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_resolved()):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome, pnl FROM ledger WHERE id = 1").fetchone()
        assert row["outcome"] == "WIN"
        assert row["pnl"] > 0
        conn.close()

    def test_yes_bet_on_losing_bracket_is_loss(self, db_path):
        """YES bet on a bracket that did NOT resolve → LOSS."""
        conn = get_connection(db_path)
        market_data, (win_low, win_high) = _make_resolved_market_data(winning_idx=3)

        # Bet on bracket 5 (different from winning bracket 3)
        _insert_pending_bet(conn, bracket_low=70.0, bracket_high=71.0,
                            side="YES", token_id="yes_tok_5")

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_resolved()):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome, pnl FROM ledger WHERE id = 1").fetchone()
        from hightempbot.persistence.ledger import poly_fee_charge
        # Loss settles at -bet_size minus the entry-side Polymarket fee.
        expected_fee = poly_fee_charge(0.30, 10.0 / 0.30)
        assert row["outcome"] == "LOSS"
        assert row["pnl"] == pytest.approx(-10.0 - expected_fee)
        conn.close()

    def test_no_bet_on_winning_bracket_is_loss(self, db_path):
        """NO bet on the bracket that resolved → LOSS (the bracket won, so NO loses)."""
        conn = get_connection(db_path)
        market_data, (win_low, win_high) = _make_resolved_market_data(winning_idx=3)

        _insert_pending_bet(conn, bracket_low=win_low, bracket_high=win_high,
                            side="NO", token_id="no_tok_3")

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_resolved()):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome, pnl FROM ledger WHERE id = 1").fetchone()
        from hightempbot.persistence.ledger import poly_fee_charge
        # Loss settles at -bet_size minus the entry-side Polymarket fee.
        expected_fee = poly_fee_charge(0.30, 10.0 / 0.30)
        assert row["outcome"] == "LOSS"
        assert row["pnl"] == pytest.approx(-10.0 - expected_fee)
        conn.close()

    def test_no_bet_on_losing_bracket_is_win(self, db_path):
        """NO bet on a bracket that did NOT resolve → WIN (the bracket lost, so NO wins)."""
        conn = get_connection(db_path)
        market_data, (win_low, win_high) = _make_resolved_market_data(winning_idx=3)

        # NO bet on bracket 5 (not the winner)
        _insert_pending_bet(conn, bracket_low=70.0, bracket_high=71.0,
                            side="NO", token_id="no_tok_5", fill_price=0.25)

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_resolved()):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome, pnl FROM ledger WHERE id = 1").fetchone()
        assert row["outcome"] == "WIN"
        assert row["pnl"] > 0
        conn.close()

    def test_no_win_uses_winning_bracket_as_actual_label(self, db_path):
        """For NO wins, the actual label is the resolved market bracket, not the bet bracket."""
        import json

        conn = get_connection(db_path)
        market_data, _ = _make_resolved_market_data(winning_idx=3)
        losing = market_data[5]

        _insert_pending_bet(
            conn,
            bracket_low=losing["bracket_low"],
            bracket_high=losing["bracket_high"],
            side="NO",
            token_id="no_tok_5",
            fill_price=0.25,
        )

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_resolved()):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome, event_detail FROM ledger WHERE id = 1").fetchone()
        detail = json.loads(row["event_detail"])
        winning = market_data[3]
        winning_label = f"[{winning['bracket_low']},{winning['bracket_high']}]"
        assert row["outcome"] == "WIN"
        assert detail["resolution_actual_label"] == winning_label
        assert detail["resolution_actual_label"] != losing.get("bracket_label")
        conn.close()

    def test_no_bet_resolves_win_when_yes_token_is_005(self, db_path):
        """NO on a bracket settles as WIN when that bracket's YES token is 0.005."""
        import json

        conn = get_connection(db_path)
        market_data, _ = _make_resolved_market_data(winning_idx=3)
        losing = market_data[5]

        _insert_pending_bet(
            conn,
            bracket_low=losing["bracket_low"],
            bracket_high=losing["bracket_high"],
            side="NO",
            token_id="no_tok_5",
            fill_price=0.25,
        )

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_terminal_low("yes_tok_5")):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome, pnl, event_detail FROM ledger WHERE id = 1").fetchone()
        detail = json.loads(row["event_detail"])
        assert row["outcome"] == "WIN"
        assert row["pnl"] > 0
        assert detail["terminal_bracket_label"] == f"[{losing['bracket_low']},{losing['bracket_high']}]"
        assert "resolution_actual_label" not in detail
        conn.close()

    def test_yes_bet_resolves_loss_when_yes_token_is_005(self, db_path):
        """YES on a bracket settles as LOSS when that bracket's YES token is 0.005."""
        conn = get_connection(db_path)
        market_data, _ = _make_resolved_market_data(winning_idx=3)
        losing = market_data[5]

        _insert_pending_bet(
            conn,
            bracket_low=losing["bracket_low"],
            bracket_high=losing["bracket_high"],
            side="YES",
            token_id="yes_tok_5",
            fill_price=0.25,
        )

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_terminal_low("yes_tok_5")):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome, pnl FROM ledger WHERE id = 1").fetchone()
        from hightempbot.persistence.ledger import poly_fee_charge
        # Loss settles at -bet_size minus the entry-side Polymarket fee.
        expected_fee = poly_fee_charge(0.25, 10.0 / 0.25)
        assert row["outcome"] == "LOSS"
        assert row["pnl"] == pytest.approx(-10.0 - expected_fee)
        conn.close()

    def test_no_bet_resolves_loss_when_no_token_is_005(self, db_path):
        """A bought NO token at 0.005 settles as LOSS."""
        conn = get_connection(db_path)
        market_data, _ = _make_resolved_market_data(winning_idx=3)
        losing = market_data[5]

        _insert_pending_bet(
            conn,
            bracket_low=losing["bracket_low"],
            bracket_high=losing["bracket_high"],
            side="NO",
            token_id="no_tok_5",
            fill_price=0.25,
        )

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_terminal_low("no_tok_5")):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome, pnl FROM ledger WHERE id = 1").fetchone()
        from hightempbot.persistence.ledger import poly_fee_charge
        # Loss settles at -bet_size minus the entry-side Polymarket fee.
        expected_fee = poly_fee_charge(0.25, 10.0 / 0.25)
        assert row["outcome"] == "LOSS"
        assert row["pnl"] == pytest.approx(-10.0 - expected_fee)
        conn.close()

    def test_no_bet_terminal_high_wins(self, db_path):
        """A NO token at 0.995 settles as WIN via the terminal-token path."""
        import json

        conn = get_connection(db_path)
        market_data, _ = _make_resolved_market_data(winning_idx=3)
        losing = market_data[5]

        _insert_pending_bet(
            conn,
            bracket_low=losing["bracket_low"],
            bracket_high=losing["bracket_high"],
            side="NO",
            token_id="no_tok_5",
            fill_price=0.80,
            bet_size=10.0,
            event_type="dry_run",
        )

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_token_bid("no_tok_5", 0.995)):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome, pnl, event_detail FROM ledger WHERE id = 1").fetchone()
        detail = json.loads(row["event_detail"])
        assert row["outcome"] == "WIN"
        assert row["pnl"] > 0
        assert detail["terminal_bracket_label"] == f"[{losing['bracket_low']},{losing['bracket_high']}]"
        assert "resolution_actual_label" not in detail
        conn.close()

    def test_no_resolution_when_prices_below_threshold(self, db_path):
        """All bracket prices < 0.995 → bets stay PENDING."""
        conn = get_connection(db_path)
        market_data, _ = _make_resolved_market_data(winning_idx=3)

        _insert_pending_bet(conn)

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_below_threshold()), \
             patch("hightempbot.resolution.settler.winning_bracket_from_gamma", return_value=None):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome FROM ledger WHERE id = 1").fetchone()
        assert row["outcome"] == "PENDING"
        conn.close()

    def test_market_data_fetch_failure_keeps_pending(self, db_path):
        """If _fetch_market_data returns empty, bets stay PENDING."""
        conn = get_connection(db_path)
        _insert_pending_bet(conn)

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value={}), \
             patch("hightempbot.resolution.settler.winning_bracket_from_gamma", return_value=None):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome FROM ledger WHERE id = 1").fetchone()
        assert row["outcome"] == "PENDING"
        conn.close()

    def test_stale_pending_stays_pending_when_polymarket_dies(self, db_path):
        """Old contract: a stale PENDING stayed PENDING when polymarket_* paths
        couldn't resolve, even with a WU actual on file.

        New contract (2026-05-20): once target_date is at least
        POLYMARKET_FALLBACK_DAYS past and polymarket_* paths return nothing,
        the resolution tick falls back to the local WU actual. This unblocks
        bets stranded by Polymarket archiving closed daily events (see the
        TestResolutionWuFallback class for the full happy-path coverage).

        Test scenario: target is a sliding 2 days past today (so days_past >=
        POLYMARKET_FALLBACK_DAYS regardless of when tests run; finding #17),
        NO bet on bracket [70.0, 71.0) (legacy 2°F integer-label form). WU
        actual = 17°C → 62.6°F display, which lands OUTSIDE [69.5, 71.5)
        (legacy bounds converted to continuous form via
        `_continuous_bracket_bounds`). NO bet on a bracket that didn't win →
        NO wins.
        """
        from datetime import date as _date, timedelta as _td
        target_date_iso = (_date.today() - _td(days=2)).isoformat()
        conn = get_connection(db_path)
        _insert_pending_bet(
            conn, bracket_low=70.0, bracket_high=71.0,
            side="NO", fill_price=0.30, target_date=target_date_iso,
        )
        conn.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
            ("KDAL", target_date_iso, 17.0, "wu"),
        )
        conn.commit()

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value={}), \
             patch("hightempbot.resolution.settler.winning_bracket_from_gamma",
                   return_value=None), \
             patch("hightempbot.resolution.settler.get_all_stations",
                   return_value={"KDAL": MockStation("KDAL")}):
            _resolve_station_date(conn, "KDAL", target_date_iso, bets)

        import json
        row = conn.execute(
            "SELECT outcome, actual_tmax, event_detail FROM ledger WHERE id = 1"
        ).fetchone()
        detail = json.loads(row["event_detail"])
        assert row["outcome"] == "PENDING"
        assert "resolution_source" not in detail
        # actual_tmax IS now populated by the fallback path (in °C).
        assert row["actual_tmax"] is None
        conn.close()

    def test_stale_pending_stays_pending_when_no_actual_on_file(self, db_path):
        """Negative case: WU fallback refuses to fire without an actual.

        Preserves the original "PENDING stays PENDING" contract for stations
        that lack a usable actual — e.g., resolution_source=ncei stations
        where the midnight scrape never wrote a row.
        """
        conn = get_connection(db_path)
        _insert_pending_bet(conn, bracket_low=62.0, bracket_high=63.0, side="YES", fill_price=0.30)
        # No actuals row inserted — fallback gate refuses.

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value={}), \
             patch("hightempbot.resolution.settler.winning_bracket_from_gamma",
                   return_value=None), \
             patch("hightempbot.resolution.settler.get_all_stations",
                   return_value={"KDAL": MockStation("KDAL")}):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome, actual_tmax FROM ledger WHERE id = 1").fetchone()
        assert row["outcome"] == "PENDING"
        assert row["actual_tmax"] is None
        conn.close()

    def test_actual_tmax_populated_when_available(self, db_path):
        """actual_tmax is stored for logging even though it doesn't determine outcome."""
        conn = get_connection(db_path)
        market_data, (win_low, win_high) = _make_resolved_market_data(winning_idx=3)

        _insert_pending_bet(conn, bracket_low=win_low, bracket_high=win_high,
                            side="YES", token_id="yes_tok_3")

        # Insert actuals (for logging only, not for resolution)
        conn.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
            ("KDAL", "2026-04-07", 21.0, "wu"),
        )
        conn.commit()

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_resolved()):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome, actual_tmax FROM ledger WHERE id = 1").fetchone()
        assert row["outcome"] == "WIN"
        assert row["actual_tmax"] == 21.0  # populated for logging
        conn.close()

    def test_missing_clob_client_keeps_pending(self, db_path):
        conn = get_connection(db_path)
        market_data, _ = _make_resolved_market_data(winning_idx=3)
        _insert_pending_bet(conn)

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", side_effect=RuntimeError("py_clob_client_v2 is not installed")):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome FROM ledger WHERE id = 1").fetchone()
        assert row["outcome"] == "PENDING"
        conn.close()

    def test_polymarket_resolution_without_actual_does_not_invent_actual_tmax(self, db_path):
        conn = get_connection(db_path)
        market_data, (win_low, win_high) = _make_resolved_market_data(winning_idx=3)

        _insert_pending_bet(conn, bracket_low=win_low, bracket_high=win_high,
                            side="YES", token_id="yes_tok_3")

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_resolved()):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome, actual_tmax FROM ledger WHERE id = 1").fetchone()
        assert row["outcome"] == "WIN"
        assert row["actual_tmax"] is None
        conn.close()

    def test_multiple_bets_resolved_in_one_pass(self, db_path):
        """Multiple PENDING bets for same station-date resolve together."""
        conn = get_connection(db_path)
        market_data, (win_low, win_high) = _make_resolved_market_data(winning_idx=3)

        # YES on winner → WIN
        _insert_pending_bet(conn, bracket_low=win_low, bracket_high=win_high,
                            side="YES", token_id="yes_tok_3")
        # YES on loser → LOSS
        _insert_pending_bet(conn, bracket_low=70.0, bracket_high=71.0,
                            side="YES", token_id="yes_tok_5")

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()
        assert len(bets) == 2

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_resolved()):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        rows = conn.execute("SELECT outcome FROM ledger ORDER BY id").fetchall()
        assert rows[0]["outcome"] == "WIN"
        assert rows[1]["outcome"] == "LOSS"
        conn.close()

    @staticmethod
    def _insert_pending_bet_no_fill(
        conn,
        *,
        side: str = "YES",
        token_id: str = "yes_tok_3",
        bracket_low: float = 61.5,
        bracket_high: float = 63.5,
        bet_size: float = 10.0,
    ) -> None:
        """Insert a PENDING bet with both fill_price and limit_price=0.

        Mirrors the realistic crash state the null-fill PUSH path guards
        against: bet recorded with no executable price provenance, so the
        pnl ternary would fabricate a loss without the PUSH downgrade.
        """
        import json
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             fill_price, fill_size,
             outcome, event_type, event_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-04-06T08:00:00Z", "KDAL", "m", token_id, "2026-04-07",
             1, bracket_high, side, 0.45, 0.30, 0.15,
             bet_size, 5000.0, bet_size, 0.0,
             None, None,
             "PENDING", "bet",
             json.dumps({"bracket_low": bracket_low, "bracket_high": bracket_high})),
        )
        conn.commit()

    def test_winning_bracket_with_null_fill_price_settles_push_not_fake_pnl(
        self, db_path,
    ):
        """YES bet on the winning bracket with NULL fill_price downgrades to
        PUSH. Without an executable entry price the pnl ternary's WIN payout
        is unknowable, so the row books pnl=0 and records the bracket's true
        resolution via `bracket_resolution = "WIN"` in event_detail."""
        import json

        conn = get_connection(db_path)
        market_data, (win_low, win_high) = _make_resolved_market_data(winning_idx=3)
        self._insert_pending_bet_no_fill(
            conn,
            side="YES",
            token_id="yes_tok_3",
            bracket_low=win_low,
            bracket_high=win_high,
        )

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data",
                   return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader",
                   return_value=_mock_clob_resolved()):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute(
            "SELECT outcome, pnl, event_detail FROM ledger WHERE id = 1"
        ).fetchone()
        detail = json.loads(row["event_detail"])
        assert row["outcome"] == "PUSH"
        assert row["pnl"] == 0.0
        assert detail.get("null_fill_price_push") is True
        # The bracket truly won — record that even though we PUSHed for accounting.
        assert detail.get("bracket_resolution") == "WIN"
        # WIN case should NOT carry the unrecorded_loss flag.
        assert "unrecorded_loss" not in detail
        conn.close()

    def test_terminal_yes_with_null_fill_price_settles_push_records_loss(
        self, db_path,
    ):
        """A bet on the terminal-yes path with NULL fill_price downgrades to
        PUSH. When the bracket truly LOST, `unrecorded_loss = True` is set so
        operators can SQL-enumerate capital lost without entry-price audit
        trail. The terminal_yes path fires when no bracket reaches the win
        threshold but at least one has an ask <= the loss-price floor."""
        import json

        conn = get_connection(db_path)
        market_data, _ = _make_resolved_market_data(winning_idx=3)
        losing = market_data[5]
        # YES on the losing bracket — that bracket's yes_token shows 0.005 ask,
        # so the bracket lost ⇒ YES bet LOSES. With NULL fill_price the loss
        # is unquantifiable so the row books PUSH and flags unrecorded_loss.
        self._insert_pending_bet_no_fill(
            conn,
            side="YES",
            token_id="yes_tok_5",
            bracket_low=losing["bracket_low"],
            bracket_high=losing["bracket_high"],
        )

        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data",
                   return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader",
                   return_value=_mock_clob_terminal_low("yes_tok_5")):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute(
            "SELECT outcome, pnl, event_detail FROM ledger WHERE id = 1"
        ).fetchone()
        detail = json.loads(row["event_detail"])
        assert row["outcome"] == "PUSH"
        assert row["pnl"] == 0.0
        assert detail.get("null_fill_price_push") is True
        # The bracket lost — operator-visible flag for capital reconciliation.
        assert detail.get("bracket_resolution") == "LOSS"
        assert detail.get("unrecorded_loss") is True
        # terminal-yes context still records its bracket label.
        assert detail.get("terminal_bracket_label") == (
            f"[{losing['bracket_low']},{losing['bracket_high']}]"
        )
        conn.close()


class TestResolutionGammaPerBracket:
    """Per-bracket Gamma close-state must not settle production rows.

    A single bracket can show closed=True/outcomePrices while the wallet
    position is still open and the full event has not resolved. Production
    settlement now waits for event-level Gamma close-state instead.
    """

    @staticmethod
    def _assert_pending_without_resolution(conn, row_id: int = 1) -> dict:
        import json

        row = conn.execute(
            "SELECT outcome, pnl, event_detail FROM ledger WHERE id = ?",
            (row_id,),
        ).fetchone()
        detail = json.loads(row["event_detail"] or "{}")
        assert row["outcome"] == "PENDING"
        assert row["pnl"] is None
        assert "closed_bracket_label" not in detail
        assert "resolution_actual_label" not in detail
        assert "resolution_bracket_low" not in detail
        assert "resolution_bracket_high" not in detail
        assert "null_fill_price_push" not in detail
        assert "bracket_resolution" not in detail
        return detail

    @staticmethod
    def _gamma_partially_closed(
        *,
        closed_loser_low: float = 59.5,
        closed_loser_high: float = 60.5,
        closed_loser_label: str = "60Â°F",
    ) -> dict[int, dict]:
        """Mimic Gamma response with bracket-1 closed-NO and brackets 2-3 open."""
        return {
            0: {
                "yes_price": 0.0,
                "no_price": 1.0,
                "closed": True,
                "token_id": "yes_tok_0",
                "no_token_id": "no_tok_0",
                "bracket_low": None,
                "bracket_high": closed_loser_low,
                "bracket_label": "<60Â°F",
            },
            1: {
                "yes_price": 0.0,
                "no_price": 1.0,
                "closed": True,
                "token_id": "yes_tok_1",
                "no_token_id": "no_tok_1",
                "bracket_low": closed_loser_low,
                "bracket_high": closed_loser_high,
                "bracket_label": closed_loser_label,
            },
            2: {
                "yes_price": 0.45,
                "no_price": 0.55,
                "closed": False,
                "token_id": "yes_tok_2",
                "no_token_id": "no_tok_2",
                "bracket_low": closed_loser_high,
                "bracket_high": closed_loser_high + 1.0,
                "bracket_label": "61Â°F",
            },
            3: {
                "yes_price": 0.55,
                "no_price": 0.45,
                "closed": False,
                "token_id": "yes_tok_3",
                "no_token_id": "no_tok_3",
                "bracket_low": closed_loser_high + 1.0,
                "bracket_high": closed_loser_high + 2.0,
                "bracket_label": "62Â°F",
            },
        }

    def test_no_bet_on_closed_losing_bracket_stays_pending(self, db_path):
        """NO on a bracket that closed NO waits for full-event resolution."""

        conn = get_connection(db_path)
        gamma_markets = self._gamma_partially_closed()
        # Bet on bracket-1 (closed, no_price=1.0) → NO bet wins.
        loser = gamma_markets[1]
        _insert_pending_bet(
            conn,
            bracket_low=loser["bracket_low"],
            bracket_high=loser["bracket_high"],
            side="NO",
            token_id="no_tok_1",
            fill_price=0.80,
            event_type="dry_run",
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data",
                   return_value={i: dict(m) for i, m in gamma_markets.items()}), \
             patch("hightempbot.execution.walker.ClobReader",
                   return_value=_mock_clob_below_threshold()), \
             patch("hightempbot.resolution.gamma.fetch_gamma_resolution_markets",
                   return_value=gamma_markets):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        del loser
        self._assert_pending_without_resolution(conn)
        conn.close()

    def test_yes_bet_on_closed_losing_bracket_stays_pending(self, db_path):
        """YES on a bracket that closed NO waits for full-event resolution."""

        conn = get_connection(db_path)
        gamma_markets = self._gamma_partially_closed()
        loser = gamma_markets[1]
        _insert_pending_bet(
            conn,
            bracket_low=loser["bracket_low"],
            bracket_high=loser["bracket_high"],
            side="YES",
            token_id="yes_tok_1",
            fill_price=0.20,
            event_type="dry_run",
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data",
                   return_value={i: dict(m) for i, m in gamma_markets.items()}), \
             patch("hightempbot.execution.walker.ClobReader",
                   return_value=_mock_clob_below_threshold()), \
             patch("hightempbot.resolution.gamma.fetch_gamma_resolution_markets",
                   return_value=gamma_markets):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        del loser
        self._assert_pending_without_resolution(conn)
        conn.close()

    def test_yes_bet_on_closed_winning_bracket_stays_pending(self, db_path):
        """YES on a bracket that closed YES still waits for full-event close."""

        conn = get_connection(db_path)
        gamma_markets = self._gamma_partially_closed()
        winner = gamma_markets[3]
        winner["yes_price"] = 1.0
        winner["no_price"] = 0.0
        winner["closed"] = True
        _insert_pending_bet(
            conn,
            bracket_low=winner["bracket_low"],
            bracket_high=winner["bracket_high"],
            side="YES",
            token_id="yes_tok_3",
            fill_price=0.20,
            event_type="dry_run",
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data",
                   return_value={i: dict(m) for i, m in gamma_markets.items()}), \
             patch("hightempbot.execution.walker.ClobReader",
                   return_value=_mock_clob_below_threshold()), \
             patch("hightempbot.resolution.gamma.fetch_gamma_resolution_markets",
                   return_value=gamma_markets):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        del winner
        self._assert_pending_without_resolution(conn)
        conn.close()

    def test_open_bracket_stays_pending(self, db_path):
        """Bet on a bracket whose Gamma row is closed=False stays PENDING."""
        conn = get_connection(db_path)
        gamma_markets = self._gamma_partially_closed()
        open_bracket = gamma_markets[2]  # closed=False
        _insert_pending_bet(
            conn,
            bracket_low=open_bracket["bracket_low"],
            bracket_high=open_bracket["bracket_high"],
            side="NO",
            token_id="no_tok_2",
            fill_price=0.55,
            event_type="dry_run",
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data",
                   return_value={i: dict(m) for i, m in gamma_markets.items()}), \
             patch("hightempbot.execution.walker.ClobReader",
                   return_value=_mock_clob_below_threshold()), \
             patch("hightempbot.resolution.gamma.fetch_gamma_resolution_markets",
                   return_value=gamma_markets):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome FROM ledger WHERE id = 1").fetchone()
        assert row["outcome"] == "PENDING"
        conn.close()

    def test_closed_bracket_with_ambiguous_prices_stays_pending(self, db_path):
        """closed=True but outcomePrices not at a corner → don't settle yet."""
        conn = get_connection(db_path)
        gamma_markets = self._gamma_partially_closed()
        # Mutate bracket-1 to closed but with ambiguous outcomePrices.
        gamma_markets[1]["yes_price"] = 0.5
        gamma_markets[1]["no_price"] = 0.5
        loser = gamma_markets[1]
        _insert_pending_bet(
            conn,
            bracket_low=loser["bracket_low"],
            bracket_high=loser["bracket_high"],
            side="NO",
            token_id="no_tok_1",
            fill_price=0.80,
            event_type="dry_run",
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data",
                   return_value={i: dict(m) for i, m in gamma_markets.items()}), \
             patch("hightempbot.execution.walker.ClobReader",
                   return_value=_mock_clob_below_threshold()), \
             patch("hightempbot.resolution.settler.winning_bracket_from_gamma",
                   return_value=None):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome FROM ledger WHERE id = 1").fetchone()
        assert row["outcome"] == "PENDING"
        conn.close()

    @staticmethod
    def _insert_pending_bet_no_fill(
        conn,
        *,
        side: str,
        token_id: str,
        bracket_low: float,
        bracket_high: float,
        bet_size: float = 10.0,
    ) -> None:
        """Insert a PENDING bet with both fill_price and limit_price=0.

        Mirrors the crash state the per-bracket Gamma null-fill PUSH path
        guards against: a bet recorded with no executable entry price so
        the pnl ternary would book a fabricated -bet_size loss.
        """
        import json
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             fill_price, fill_size,
             outcome, event_type, event_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-04-06T08:00:00Z", "KDAL", "m", token_id, "2026-04-07",
             1, bracket_high, side, 0.45, 0.30, 0.15,
             bet_size, 5000.0, bet_size, 0.0,
             None, None,
             "PENDING", "bet",
             json.dumps({"bracket_low": bracket_low, "bracket_high": bracket_high})),
        )
        conn.commit()

    def test_no_bet_on_closed_losing_bracket_with_null_fill_stays_pending(
        self, db_path,
    ):
        """NO on a closed-NO bracket stays PENDING until event-level close."""

        conn = get_connection(db_path)
        gamma_markets = self._gamma_partially_closed()
        # Bracket-1 is closed with no_price=1.0 ⇒ NO bet wins.
        loser = gamma_markets[1]
        self._insert_pending_bet_no_fill(
            conn,
            side="NO",
            token_id="no_tok_1",
            bracket_low=loser["bracket_low"],
            bracket_high=loser["bracket_high"],
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data",
                   return_value={i: dict(m) for i, m in gamma_markets.items()}), \
             patch("hightempbot.execution.walker.ClobReader",
                   return_value=_mock_clob_below_threshold()), \
             patch("hightempbot.resolution.settler.winning_bracket_from_gamma",
                   return_value=None):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        del loser
        self._assert_pending_without_resolution(conn)
        conn.close()

    def test_yes_bet_on_closed_losing_bracket_with_null_fill_stays_pending(
        self, db_path,
    ):
        """YES on a closed-NO bracket stays PENDING until event-level close."""

        conn = get_connection(db_path)
        gamma_markets = self._gamma_partially_closed()
        loser = gamma_markets[1]
        self._insert_pending_bet_no_fill(
            conn,
            side="YES",
            token_id="yes_tok_1",
            bracket_low=loser["bracket_low"],
            bracket_high=loser["bracket_high"],
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data",
                   return_value={i: dict(m) for i, m in gamma_markets.items()}), \
             patch("hightempbot.execution.walker.ClobReader",
                   return_value=_mock_clob_below_threshold()), \
             patch("hightempbot.resolution.settler.winning_bracket_from_gamma",
                   return_value=None):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        del loser
        self._assert_pending_without_resolution(conn)
        conn.close()


class TestResolutionGammaCloseEventLevel:
    """Event-level Gamma close-state path (`_resolve_via_gamma_close`).

    Fires when every bracket on the event is closed and exactly one has
    ``yes_price >= RESOLUTION_PRICE_THRESHOLD``. The historical bug: when
    ``fill_price`` and ``limit_price`` are both NULL/0 the pnl ternary
    falls through to ``-bet_size`` even on a WIN, fabricating a loss.
    """

    @staticmethod
    def _gamma_all_closed_one_winner(winning_idx: int = 2) -> dict[int, dict]:
        """Mimic Gamma response with every bracket closed and one winner."""
        markets = {}
        for i in range(4):
            markets[i] = {
                "yes_price": 0.0,
                "no_price": 1.0,
                "closed": True,
                "token_id": f"yes_tok_{i}",
                "no_token_id": f"no_tok_{i}",
                "bracket_low": 59.5 + i,
                "bracket_high": 60.5 + i,
                "bracket_label": f"{60 + i}°F",
            }
        markets[winning_idx]["yes_price"] = 1.0
        markets[winning_idx]["no_price"] = 0.0
        return markets

    @staticmethod
    def _insert_pending_bet_no_fill(
        conn,
        *,
        side: str = "NO",
        token_id: str = "no_tok_2",
        bracket_low: float = 61.5,
        bracket_high: float = 62.5,
        bet_size: float = 10.0,
    ) -> None:
        """Insert a PENDING bet with both fill_price and limit_price=0.

        Mirrors the realistic crash state the bug fires on: bet was placed
        but never filled, and limit_price was never recorded (or was zeroed
        by a buggy path), so `fill_price or limit_price or 0.0` returns 0.0.
        """
        import json

        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             fill_price, fill_size,
             outcome, event_type, event_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-04-06T08:00:00Z", "KDAL", "m", token_id, "2026-04-07",
             1, bracket_high, side, 0.45, 0.30, 0.15,
             bet_size, 5000.0, bet_size, 0.0,
             None, None,
             "PENDING", "bet",
             json.dumps({"bracket_low": bracket_low, "bracket_high": bracket_high})),
        )
        conn.commit()

    def test_no_bet_winning_bracket_with_zero_fill_settles_push_not_fake_loss(
        self, db_path,
    ):
        """A WIN with NULL fill_price must downgrade to PUSH (pnl=0), not
        get booked as -bet_size loss. The pre-fix bug fired here because
        the ternary required `won AND fill_price > 0` and otherwise fell
        through to the loss branch."""
        conn = get_connection(db_path)
        gamma_markets = self._gamma_all_closed_one_winner(winning_idx=2)
        # Bet on bracket-2 (the winner). NO bet on the winning bracket loses
        # under normal rules — but here we're testing the WIN case with
        # zero fill_price. Use bracket-3 (a loser) and side=NO so NO wins.
        winner_idx = 2
        loser_idx = 3
        loser = gamma_markets[loser_idx]
        self._insert_pending_bet_no_fill(
            conn,
            side="NO",
            token_id=f"no_tok_{loser_idx}",
            bracket_low=loser["bracket_low"],
            bracket_high=loser["bracket_high"],
            bet_size=10.0,
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()
        del winner_idx  # unused; kept above for narrative

        with patch("hightempbot.resolution.settler._fetch_market_data",
                   return_value={i: dict(m) for i, m in gamma_markets.items()}), \
             patch("hightempbot.execution.walker.ClobReader",
                   return_value=_mock_clob_below_threshold()), \
             patch("hightempbot.resolution.gamma.fetch_gamma_resolution_markets",
                   return_value=gamma_markets):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute(
            "SELECT outcome, pnl FROM ledger WHERE id = 1"
        ).fetchone()
        assert row["outcome"] == "PUSH"
        assert row["pnl"] == 0.0
        conn.close()

    def test_yes_bet_losing_bracket_with_zero_fill_also_settles_push(
        self, db_path,
    ):
        """LOSS with NULL fill_price also downgrades to PUSH — the rule is
        about provenance, not about which side won. Without provenance we
        cannot honestly compute pnl on either side."""
        conn = get_connection(db_path)
        gamma_markets = self._gamma_all_closed_one_winner(winning_idx=2)
        loser_idx = 3
        loser = gamma_markets[loser_idx]
        self._insert_pending_bet_no_fill(
            conn,
            side="YES",
            token_id=f"yes_tok_{loser_idx}",
            bracket_low=loser["bracket_low"],
            bracket_high=loser["bracket_high"],
            bet_size=10.0,
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data",
                   return_value={i: dict(m) for i, m in gamma_markets.items()}), \
             patch("hightempbot.execution.walker.ClobReader",
                   return_value=_mock_clob_below_threshold()), \
             patch("hightempbot.resolution.gamma.fetch_gamma_resolution_markets",
                   return_value=gamma_markets):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute(
            "SELECT outcome, pnl FROM ledger WHERE id = 1"
        ).fetchone()
        assert row["outcome"] == "PUSH"
        assert row["pnl"] == 0.0
        conn.close()


class TestResolutionWuFallback:
    """WU actuals fallback path — fires when every polymarket_* path returns
    0 resolutions AND target_date is at least POLYMARKET_FALLBACK_DAYS past.

    Context (2026-05-20): Polymarket Gamma archives daily-temperature events
    some time after close (`/events?slug=` and `/markets?condition_ids=` both
    go empty). Without this fallback, PENDINGs linger forever after the
    archive — confirmed against 5/17 + 5/18 events.
    """

    @staticmethod
    def _seed_actual(conn, station_id: str, local_date: str, tmax_c: float) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO actuals "
            "(station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, 'wu')",
            (station_id, local_date, tmax_c),
        )
        conn.commit()

    @staticmethod
    def _seed_station(conn, icao: str, unit: str = "C") -> None:
        """Populate enrolled_stations so get_all_stations returns this row."""
        conn.execute(
            """INSERT OR REPLACE INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source,
             calibration_source, poly_slug, status, coverage_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (icao, "TestCity", 0.0, 0.0, "UTC", unit, "wu",
             "wu", "test-city", "DRY_RUN", 0.95),
        )
        conn.commit()

    def _insert_pending_no_bet(
        self,
        conn,
        *,
        bet_id_hint: int = 1,
        station_id: str = "KDAL",
        target_date: str = "2026-05-17",
        bracket_low: float | None = 63.5,
        bracket_high: float | None = None,
        side: str = "NO",
        fill_price: float = 0.604,
        bet_size: float = 5.0,
    ) -> int:
        """Insert a PENDING bet whose bracket bounds are stored in event_detail."""
        import json
        cur = conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             fill_price, fill_size,
             outcome, event_type, event_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"{target_date}T08:00:00Z", station_id, "m", "tok",
                target_date,
                1, bracket_high if bracket_high is not None else (bracket_low or 0.0),
                side, 0.45, 0.30, 0.15,
                bet_size, 5000.0, bet_size, fill_price,
                fill_price, bet_size / fill_price,
                "PENDING", "dry_run",
                json.dumps({"bracket_low": bracket_low, "bracket_high": bracket_high,
                            "bracket_label": f"[{bracket_low},{bracket_high})"}),
            ),
        )
        conn.commit()
        del bet_id_hint
        return int(cur.lastrowid)

    def _manual_wu_resolve(
        self,
        conn,
        station_id: str,
        target_date: str,
        bets: list[sqlite3.Row],
        *,
        unit: str = "F",
        days_past: int = 2,
    ) -> int:
        actual = conn.execute(
            "SELECT tmax_celsius FROM actuals WHERE station_id=? AND local_date=?",
            (station_id, target_date),
        ).fetchone()
        assert actual is not None
        return _resolve_via_wu_actual_fallback(
            conn,
            station_id,
            target_date,
            bets,
            actual["tmax_celsius"],
            MockStation(station_id, unit=unit, timezone="UTC", icao=station_id),
            days_past=days_past,
        )

    def test_resolves_winning_no_bet_via_wu_actuals(self, db_path):
        """A NO bet on a ceiling bracket whose actual fell short wins via WU
        fallback when every polymarket_* path is empty."""
        import json
        from datetime import date, timedelta

        conn = get_connection(db_path)
        self._seed_station(conn, "KDAL", unit="F")
        # Target 2 days ago — well past POLYMARKET_FALLBACK_DAYS (1) so the
        # fallback gate fires. Use a sliding date so the test stays valid
        # regardless of when it runs.
        target = (date.today() - timedelta(days=2)).isoformat()
        # WU actual = 15°C → 59°F display. Bracket "≥64°F" → bet_low=63.5.
        self._seed_actual(conn, "KDAL", target, tmax_c=15.0)
        bet_id = self._insert_pending_no_bet(
            conn, station_id="KDAL", target_date=target,
            bracket_low=63.5, bracket_high=None,
            side="NO", fill_price=0.604, bet_size=5.0,
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome='PENDING'").fetchall()

        # Every polymarket_* path returns empty → fallback fires.
        self._manual_wu_resolve(conn, "KDAL", target, bets, unit="F")

        row = conn.execute(
            "SELECT outcome, pnl, event_detail FROM ledger WHERE id=?", (bet_id,),
        ).fetchone()
        detail = json.loads(row["event_detail"])
        assert row["outcome"] == "WIN"
        assert detail["resolution_source"] == "wu_actual_fallback"
        assert detail["fallback_reason"].startswith("polymarket_unavailable_")
        assert detail["actual_unit"] == "F"
        # 59°F < 63.5 → bracket lost → NO won. Pnl gross = (1/0.604 − 1) × 5
        # ≈ +3.28, minus the fee from record_resolution. Verify the sign,
        # don't pin the exact penny.
        assert row["pnl"] > 3.0
        conn.close()

    def test_does_not_fire_before_fallback_days_threshold(self, db_path):
        """target_date = today → days_past=0 → fallback dormant even though
        polymarket_* paths are empty. The bet stays PENDING for the next tick."""
        from datetime import date
        from unittest.mock import patch

        conn = get_connection(db_path)
        self._seed_station(conn, "KDAL", unit="F")
        today = date.today().isoformat()
        self._seed_actual(conn, "KDAL", today, tmax_c=15.0)
        bet_id = self._insert_pending_no_bet(
            conn, station_id="KDAL", target_date=today,
            bracket_low=63.5, bracket_high=None,
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome='PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data",
                   return_value={}), \
             patch("hightempbot.resolution.settler.winning_bracket_from_gamma",
                   return_value=None):
            _resolve_station_date(conn, "KDAL", today, bets)

        row = conn.execute(
            "SELECT outcome FROM ledger WHERE id=?", (bet_id,),
        ).fetchone()
        # Stays PENDING because days_past=0 < POLYMARKET_FALLBACK_DAYS=1.
        assert row["outcome"] == "PENDING"
        conn.close()

    def test_refuses_when_bracket_bounds_missing(self, db_path):
        """Legacy rows with no bracket_low/high MUST NOT be resolved via
        fallback — token-only matching is unsafe under Polymarket relisting
        and the safety contract refuses to guess."""
        import json
        from datetime import date, timedelta

        conn = get_connection(db_path)
        self._seed_station(conn, "KDAL", unit="F")
        target = (date.today() - timedelta(days=2)).isoformat()
        self._seed_actual(conn, "KDAL", target, tmax_c=15.0)
        # Insert PENDING bet with event_detail that has NO bracket bounds.
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             fill_price, fill_size,
             outcome, event_type, event_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"{target}T08:00:00Z", "KDAL", "m", "tok", target,
                1, 63.0, "NO", 0.45, 0.30, 0.15,
                5.0, 5000.0, 5.0, 0.6,
                0.6, 8.33,
                "PENDING", "dry_run",
                json.dumps({"strategy": "NO"}),  # no bracket_low/high
            ),
        )
        conn.commit()
        bets = conn.execute("SELECT * FROM ledger WHERE outcome='PENDING'").fetchall()

        self._manual_wu_resolve(conn, "KDAL", target, bets, unit="F")

        row = conn.execute("SELECT outcome FROM ledger WHERE id=1").fetchone()
        # Refuse to resolve via fallback → stays PENDING for manual handling.
        assert row["outcome"] == "PENDING"
        conn.close()

    def test_resolves_yes_bet_via_wu_actuals(self, db_path):
        """YES-side WU fallback coverage. Without this test the
        ``side.upper() == 'NO'`` branch in the WU fallback is the only path
        exercised (finding #6).
        """
        import json
        from datetime import date, timedelta

        conn = get_connection(db_path)
        self._seed_station(conn, "KDAL", unit="F")
        target = (date.today() - timedelta(days=2)).isoformat()
        # WU actual = 21°C → 69.8°F display. Bracket [69.5, 70.5) → YES wins.
        self._seed_actual(conn, "KDAL", target, tmax_c=21.0)
        bet_id = self._insert_pending_no_bet(
            conn, station_id="KDAL", target_date=target,
            bracket_low=69.5, bracket_high=70.5,
            side="YES", fill_price=0.40, bet_size=5.0,
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome='PENDING'").fetchall()
        self._manual_wu_resolve(conn, "KDAL", target, bets, unit="F")
        row = conn.execute(
            "SELECT outcome, pnl, event_detail FROM ledger WHERE id=?", (bet_id,),
        ).fetchone()
        detail = json.loads(row["event_detail"])
        assert row["outcome"] == "WIN"
        assert detail["resolution_source"] == "wu_actual_fallback"
        assert row["pnl"] > 5.0  # (1/0.40 - 1) * 5 = 7.5 gross, less fee.
        conn.close()

    def test_resolves_celsius_station_via_wu_actuals(self, db_path):
        """°C station WU fallback — the ``unit.upper() == 'F'`` else-branch
        was previously untested (finding #6). Bracket bounds and actual stay
        in °C; no °F conversion fires.
        """
        import json
        from datetime import date, timedelta

        conn = get_connection(db_path)
        self._seed_station(conn, "EFHK", unit="C")
        target = (date.today() - timedelta(days=2)).isoformat()
        # Actual 21.2°C, bracket [20.5, 21.5) → YES wins.
        self._seed_actual(conn, "EFHK", target, tmax_c=21.2)
        bet_id = self._insert_pending_no_bet(
            conn, station_id="EFHK", target_date=target,
            bracket_low=20.5, bracket_high=21.5,
            side="YES", fill_price=0.30, bet_size=5.0,
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome='PENDING'").fetchall()
        self._manual_wu_resolve(conn, "EFHK", target, bets, unit="C")
        row = conn.execute(
            "SELECT outcome, event_detail FROM ledger WHERE id=?", (bet_id,),
        ).fetchone()
        detail = json.loads(row["event_detail"])
        assert row["outcome"] == "WIN"
        assert detail["actual_unit"] == "C"
        # actual_display stored in °C (no conversion applied).
        assert abs(float(detail["actual_display"]) - 21.2) < 1e-6
        conn.close()

    def test_null_fill_price_downgrades_to_push(self, db_path):
        """NULL fill_price in the WU fallback must downgrade to PUSH (the
        sixth `_apply_null_fill_push` call site was previously uncovered;
        finding #6).
        """
        import json
        from datetime import date, timedelta

        conn = get_connection(db_path)
        self._seed_station(conn, "KDAL", unit="F")
        target = (date.today() - timedelta(days=2)).isoformat()
        self._seed_actual(conn, "KDAL", target, tmax_c=21.0)
        # NULL fill_price — must downgrade to PUSH even though the bracket
        # would have won.
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             fill_price, fill_size,
             outcome, event_type, event_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"{target}T08:00:00Z", "KDAL", "m", "tok", target,
                1, 70.0, "YES", 0.45, 0.30, 0.15,
                5.0, 5000.0, 5.0, 0.0,
                None, None,
                "PENDING", "bet",
                json.dumps({"bracket_low": 69.5, "bracket_high": 70.5}),
            ),
        )
        conn.commit()
        bets = conn.execute("SELECT * FROM ledger WHERE outcome='PENDING'").fetchall()
        self._manual_wu_resolve(conn, "KDAL", target, bets, unit="F")
        row = conn.execute(
            "SELECT outcome, pnl, event_detail FROM ledger WHERE id=1"
        ).fetchone()
        detail = json.loads(row["event_detail"])
        assert row["outcome"] == "PUSH"
        # PUSH ⇒ no resolution_actual_label even though the bracket WOULD have
        # won — keeps the dashboard honest.
        assert detail.get("resolution_actual_label") is None
        assert detail.get("null_fill_price_push") is True
        conn.close()

    def test_refuses_empty_unit_station(self, db_path):
        """``station_cfg.unit == ''`` is unresolvable — fail closed.

        Empty-string unit semantics: refuses to convert °C → display unit so
        no bracket comparison runs. Mirrors the dry-run preview guard in
        `hightempbot.cli.resolve_pending_via_wu` (finding #14).
        """
        from datetime import date, timedelta

        conn = get_connection(db_path)
        # Empty unit — station enrolled but unit not yet known.
        self._seed_station(conn, "KDAL", unit="")
        target = (date.today() - timedelta(days=2)).isoformat()
        self._seed_actual(conn, "KDAL", target, tmax_c=15.0)
        self._insert_pending_no_bet(
            conn, station_id="KDAL", target_date=target,
            bracket_low=63.5, bracket_high=None,
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome='PENDING'").fetchall()
        self._manual_wu_resolve(conn, "KDAL", target, bets, unit="")
        row = conn.execute("SELECT outcome FROM ledger WHERE id=1").fetchone()
        # Refused → stays PENDING.
        assert row["outcome"] == "PENDING"
        conn.close()

    def test_bankers_rounding_boundary_uses_raw_value(self, db_path):
        """68.5°F boundary case — the prior implementation used
        ``round(actual_display)`` which under banker's rounding gives
        ``round(68.5) == 68``, putting the actual in ``[67.5, 68.5)`` (the
        "68°F" bracket) instead of ``[68.5, 69.5)`` (the "69°F" bracket where
        Polymarket would place it; finding #8). The fix is to compare the raw
        display value against the bracket bounds.

        Scenario: actual = 20.2778°C → 68.5°F exactly. YES bet on the "69°F"
        bracket [68.5, 69.5). With raw comparison 68.5 ∈ [68.5, 69.5) → YES
        wins. With the old banker's-rounded comparison this would have lost.
        """
        import json
        from datetime import date, timedelta

        conn = get_connection(db_path)
        self._seed_station(conn, "KDAL", unit="F")
        target = (date.today() - timedelta(days=2)).isoformat()
        # 20.2778°C → 68.5°F exact. Use 20.27778 to avoid FP noise.
        self._seed_actual(conn, "KDAL", target, tmax_c=20.27778)
        bet_id = self._insert_pending_no_bet(
            conn, station_id="KDAL", target_date=target,
            bracket_low=68.5, bracket_high=69.5,
            side="YES", fill_price=0.40, bet_size=5.0,
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome='PENDING'").fetchall()
        self._manual_wu_resolve(conn, "KDAL", target, bets, unit="F")
        row = conn.execute(
            "SELECT outcome, event_detail FROM ledger WHERE id=?", (bet_id,),
        ).fetchone()
        detail = json.loads(row["event_detail"])
        assert row["outcome"] == "WIN"
        # actual_display stored at sub-degree precision (no rounding).
        assert abs(float(detail["actual_display"]) - 68.5) < 1e-3
        conn.close()

    def test_legacy_bracket_bounds_match_continuous_form(self, db_path):
        """Legacy 2°F integer-label rows (lo=70, hi=71) must resolve via the
        same continuous bounds Polymarket uses ([69.5, 71.5)). Without the
        legacy-format heuristic the WU fallback would mis-resolve these rows
        (finding #7).
        """
        from datetime import date, timedelta

        conn = get_connection(db_path)
        self._seed_station(conn, "KDAL", unit="F")
        target = (date.today() - timedelta(days=2)).isoformat()
        # Actual 21°C → 69.8°F. Legacy bracket "70-71°F" (integer label)
        # stored as (lo=70, hi=71). Polymarket resolves YES iff actual rounds
        # to 70 or 71 → actual in [69.5, 71.5). 69.8 ∈ [69.5, 71.5) → YES
        # wins.
        self._seed_actual(conn, "KDAL", target, tmax_c=21.0)
        bet_id = self._insert_pending_no_bet(
            conn, station_id="KDAL", target_date=target,
            bracket_low=70.0, bracket_high=71.0,
            side="YES", fill_price=0.40, bet_size=5.0,
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome='PENDING'").fetchall()
        self._manual_wu_resolve(conn, "KDAL", target, bets, unit="F")
        row = conn.execute(
            "SELECT outcome FROM ledger WHERE id=?", (bet_id,),
        ).fetchone()
        assert row["outcome"] == "WIN"
        conn.close()

    def test_per_bet_exception_does_not_block_peers(self, db_path):
        """One corrupted bet must not abort resolution for remaining bets in
        the same (station, target_date) group (finding #9).
        """
        from datetime import date, timedelta

        conn = get_connection(db_path)
        self._seed_station(conn, "KDAL", unit="F")
        target = (date.today() - timedelta(days=2)).isoformat()
        self._seed_actual(conn, "KDAL", target, tmax_c=15.0)
        # Bet 1: corrupted event_detail (invalid JSON-like marker that will
        # parse into a dict with None bounds → refuses-bounds branch).
        # Bet 2: clean NO bet — must still resolve despite the earlier
        # bet's refusal-or-exception.
        conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             fill_price, fill_size,
             outcome, event_type, event_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"{target}T08:00:00Z", "KDAL", "m", "tok_a", target,
                1, 63.0, "NO", 0.45, 0.30, 0.15,
                5.0, 5000.0, 5.0, 0.6, 0.6, 8.33,
                "PENDING", "bet",
                "not-valid-json-and-no-bracket-bounds",
            ),
        )
        clean_id = self._insert_pending_no_bet(
            conn, station_id="KDAL", target_date=target,
            bracket_low=63.5, bracket_high=None,
            side="NO", fill_price=0.604, bet_size=5.0,
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome='PENDING'").fetchall()
        self._manual_wu_resolve(conn, "KDAL", target, bets, unit="F")
        # The clean bet resolved despite the corrupted peer.
        clean_outcome = conn.execute(
            "SELECT outcome FROM ledger WHERE id=?", (clean_id,),
        ).fetchone()
        assert clean_outcome["outcome"] == "WIN"
        # Corrupted bet stays PENDING (refused, not committed wrong).
        first = conn.execute("SELECT outcome FROM ledger WHERE id=1").fetchone()
        assert first["outcome"] == "PENDING"
        conn.close()


class TestMarketDiscoveryCache:
    def test_stale_negative_sentinel_refetches_gamma(self, db_path):
        market_date = "2999-04-17"
        conn = get_connection(db_path)
        conn.execute(
            """INSERT INTO market_tokens
            (station_id, market_date, bracket_idx, token_id, no_token_id, market_id, fetched_at)
            VALUES (?, ?, -1, '', '', '', ?)""",
            ("KDAL", market_date, "2000-01-01 00:00:00"),
        )
        conn.commit()

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [{
            "title": "Highest temperature in Dallas on April 17, 2999",
            "markets": [{
                "question": "Will the highest temperature in Dallas be between 62-63°F on April 17?",
                "clobTokenIds": ["yes_tok", "no_tok"],
                "outcomePrices": ["0.41", "0.59"],
                "volume": 1234,
                "conditionId": "market_1",
            }],
        }]

        mock_reader = MagicMock()
        mock_reader.fetch_order_book.return_value = {"asks": [{"price": "0.41", "size": "100"}], "bids": []}

        with patch.dict("hightempbot.stations.ICAO_TO_CITY", {"KDAL": "dallas"}, clear=False), \
             patch("requests.get", return_value=response) as mock_get, \
             patch("hightempbot.execution.walker.ClobReader", return_value=mock_reader):
            result = _fetch_market_data("KDAL", target_date=datetime(2999, 4, 17).date(), conn=conn)

        assert mock_get.called
        assert 0 in result
        assert result[0]["token_id"] == "yes_tok"
        rows = conn.execute(
            "SELECT bracket_idx, token_id FROM market_tokens WHERE station_id = ? AND market_date = ? ORDER BY bracket_idx",
            ("KDAL", market_date),
        ).fetchall()
        assert [row["bracket_idx"] for row in rows] == [0]
        assert rows[0]["token_id"] == "yes_tok"
        conn.close()

    def test_past_date_stale_negative_sentinel_refetches_gamma(self, db_path):
        market_date = (datetime.now() - timedelta(days=2)).date()
        conn = get_connection(db_path)
        conn.execute(
            """INSERT INTO market_tokens
            (station_id, market_date, bracket_idx, token_id, no_token_id, market_id, fetched_at)
            VALUES (?, ?, -1, '', '', '', ?)""",
            ("KDAL", market_date.isoformat(), "2000-01-01 00:00:00"),
        )
        conn.commit()

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [{
            "title": f"Highest temperature in Dallas on {market_date.strftime('%B')} {market_date.day}, {market_date.year}",
            "markets": [{
                "question": (
                    f"Will the highest temperature in Dallas be between 62-63°F "
                    f"on {market_date.strftime('%B')} {market_date.day}?"
                ),
                "clobTokenIds": ["yes_tok", "no_tok"],
                "outcomePrices": ["0.41", "0.59"],
                "volume": 1234,
                "conditionId": "market_1",
            }],
        }]

        mock_reader = MagicMock()
        mock_reader.fetch_order_book.return_value = {"asks": [{"price": "0.41", "size": "100"}], "bids": []}

        with patch.dict("hightempbot.stations.ICAO_TO_CITY", {"KDAL": "dallas"}, clear=False), \
             patch("requests.get", return_value=response) as mock_get, \
             patch("hightempbot.execution.walker.ClobReader", return_value=mock_reader):
            result = _fetch_market_data("KDAL", target_date=market_date, conn=conn)

        assert mock_get.called
        assert 0 in result
        assert result[0]["token_id"] == "yes_tok"
        rows = conn.execute(
            "SELECT bracket_idx, token_id FROM market_tokens WHERE station_id = ? AND market_date = ? ORDER BY bracket_idx",
            ("KDAL", market_date.isoformat()),
        ).fetchall()
        assert [row["bracket_idx"] for row in rows] == [0]
        assert rows[0]["token_id"] == "yes_tok"
        conn.close()

    def test_discovery_filters_dead_clob_tokens_before_caching(self, db_path):
        market_date = "2999-04-18"
        conn = get_connection(db_path)

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [{
            "title": "Highest temperature in Dallas on April 18, 2999",
            "markets": [
                {
                    "question": "Will the highest temperature in Dallas be between 62-63°F on April 18?",
                    "clobTokenIds": ["yes_dead", "no_dead"],
                    "outcomePrices": ["0.41", "0.59"],
                    "volume": 1234,
                    "conditionId": "market_dead",
                },
                {
                    "question": "Will the highest temperature in Dallas be between 64-65°F on April 18?",
                    "clobTokenIds": ["yes_live", "no_live"],
                    "outcomePrices": ["0.35", "0.65"],
                    "volume": 4321,
                    "conditionId": "market_live",
                },
            ],
        }]

        mock_reader = MagicMock()

        def _fetch_book(token_id):
            if token_id in {"yes_live", "no_live"}:
                return {"asks": [{"price": "0.35", "size": "100"}], "bids": []}
            return None

        mock_reader.fetch_order_book.side_effect = _fetch_book

        with patch.dict("hightempbot.stations.ICAO_TO_CITY", {"KDAL": "dallas"}, clear=False), \
             patch("requests.get", return_value=response), \
             patch("hightempbot.execution.walker.ClobReader", return_value=mock_reader):
            result = _fetch_market_data("KDAL", target_date=datetime(2999, 4, 18).date(), conn=conn)

        assert list(result.keys()) == [1]
        assert result[1]["token_id"] == "yes_live"
        rows = conn.execute(
            "SELECT bracket_idx, token_id FROM market_tokens WHERE station_id = ? AND market_date = ? ORDER BY bracket_idx",
            ("KDAL", market_date),
        ).fetchall()
        assert [(row["bracket_idx"], row["token_id"]) for row in rows] == [(1, "yes_live")]
        conn.close()

    def test_unparseable_positive_cache_refetches_gamma(self, db_path):
        market_date = "2999-04-19"
        conn = get_connection(db_path)
        conn.execute(
            """INSERT INTO market_tokens
            (station_id, market_date, bracket_idx, token_id, no_token_id, market_id, bracket_label, bracket_low, bracket_high, fetched_at)
            VALUES (?, ?, 0, 'bad_yes', 'bad_no', 'bad_market', NULL, NULL, NULL, datetime('now'))""",
            ("KDAL", market_date),
        )
        conn.commit()

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [{
            "title": "Highest temperature in Dallas on April 19, 2999",
            "markets": [{
                "question": "Will the highest temperature in Dallas be between 62-63°F on April 19?",
                "clobTokenIds": ["yes_tok", "no_tok"],
                "outcomePrices": ["0.41", "0.59"],
                "volume": 1234,
                "conditionId": "market_1",
            }],
        }]

        mock_reader = MagicMock()
        mock_reader.fetch_order_book.return_value = {"asks": [{"price": "0.41", "size": "100"}], "bids": []}

        with patch.dict("hightempbot.stations.ICAO_TO_CITY", {"KDAL": "dallas"}, clear=False), \
             patch("requests.get", return_value=response) as mock_get, \
             patch("hightempbot.execution.walker.ClobReader", return_value=mock_reader):
            result = _fetch_market_data("KDAL", target_date=datetime(2999, 4, 19).date(), conn=conn)

        assert mock_get.called
        assert 0 in result
        assert result[0]["bracket_low"] == pytest.approx(61.5)
        rows = conn.execute(
            "SELECT token_id, bracket_low, bracket_high FROM market_tokens WHERE station_id = ? AND market_date = ?",
            ("KDAL", market_date),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["token_id"] == "yes_tok"
        assert rows[0]["bracket_low"] == pytest.approx(61.5)
        assert rows[0]["bracket_high"] == pytest.approx(63.5)
        conn.close()


class TestLegacyLedgerResolution:
    """Legacy single-value bet bracket-bound tests; require the CLOB threshold
    scan (disabled in production via EARLY_RESOLUTION_ENABLED=False)."""

    @pytest.fixture(autouse=True)
    def _enable_early_resolution(self):
        with patch("hightempbot.execution.strategy_constants.EARLY_RESOLUTION_ENABLED", True):
            yield

    def test_legacy_single_value_bet_matches_new_winner(self, db_path, caplog):
        conn = get_connection(db_path)
        market_data, _ = _make_resolved_market_data(winning_idx=3)
        # Winner post-fix: (63.5, 65.5). Legacy bet stored with bracket_low==bracket_high==64
        # (old integer-label single-value semantics) should still resolve as WIN.
        for bi, mkt in market_data.items():
            mkt["bracket_low"] = None if mkt["bracket_low"] is None else mkt["bracket_low"]
        _insert_pending_bet(
            conn, bracket_low=64.0, bracket_high=64.0,
            side="YES", token_id="yes_tok_3",
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        import logging
        with caplog.at_level(logging.INFO, logger="hightempbot.resolution.settler"), \
             patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_resolved()):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome FROM ledger WHERE id = 1").fetchone()
        assert row["outcome"] == "WIN"
        assert any("legacy-format compat" in r.message for r in caplog.records)
        conn.close()

    def test_legacy_floor_bet_matches_new_floor_winner(self, db_path):
        conn = get_connection(db_path)
        market_data, _ = _make_resolved_market_data(winning_idx=0)
        _insert_pending_bet(
            conn,
            bracket_low=None,
            bracket_high=59.0,
            side="YES",
            token_id="yes_tok_0",
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_resolved(winning_token="yes_tok_0")):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome FROM ledger WHERE id = 1").fetchone()
        assert row["outcome"] == "WIN"
        conn.close()

    def test_legacy_ceiling_bet_matches_new_ceiling_winner(self, db_path):
        conn = get_connection(db_path)
        market_data, _ = _make_resolved_market_data(winning_idx=10)
        _insert_pending_bet(
            conn,
            bracket_low=78.0,
            bracket_high=None,
            side="YES",
            token_id="yes_tok_10",
        )
        bets = conn.execute("SELECT * FROM ledger WHERE outcome = 'PENDING'").fetchall()

        with patch("hightempbot.resolution.settler._fetch_market_data", return_value=market_data), \
             patch("hightempbot.execution.walker.ClobReader", return_value=_mock_clob_resolved(winning_token="yes_tok_10")):
            _resolve_station_date(conn, "KDAL", "2026-04-07", bets)

        row = conn.execute("SELECT outcome FROM ledger WHERE id = 1").fetchone()
        assert row["outcome"] == "WIN"
        conn.close()


def _insert_enrolled_station(conn, icao: str, unit: str = "F") -> None:
    conn.execute(
        """INSERT INTO enrolled_stations
        (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source, poly_slug)
        VALUES (?, ?, 0.0, 0.0, 'UTC', ?, 'wu', 'legacy', ?)""",
        (icao, icao.lower(), unit, icao.lower()),
    )
    conn.commit()


class TestAutoHealStationUnit:
    def test_updates_db_when_unit_mismatches(self, db_path, caplog):
        from hightempbot.scheduler.station_healing import _heal_station_unit_if_wrong
        import logging
        conn = get_connection(db_path)
        _insert_enrolled_station(conn, "EFHK", unit="F")
        market_data = {
            0: {"bracket_label": "<12°C"},
            1: {"bracket_label": "13°C"},
        }
        with caplog.at_level(logging.WARNING, logger="hightempbot.scheduler.station_healing"):
            _heal_station_unit_if_wrong(conn, "EFHK", "F", market_data)
        row = conn.execute("SELECT unit FROM enrolled_stations WHERE icao = ?", ("EFHK",)).fetchone()
        assert row["unit"] == "C"
        assert any("Auto-healed station unit EFHK" in r.message for r in caplog.records)
        conn.close()

    def test_is_noop_when_unit_matches(self, db_path):
        from hightempbot.scheduler.station_healing import _heal_station_unit_if_wrong
        conn = get_connection(db_path)
        _insert_enrolled_station(conn, "EGLC", unit="C")
        market_data = {0: {"bracket_label": "14°C"}}
        _heal_station_unit_if_wrong(conn, "EGLC", "C", market_data)
        row = conn.execute("SELECT unit FROM enrolled_stations WHERE icao = ?", ("EGLC",)).fetchone()
        assert row["unit"] == "C"
        conn.close()

    def test_skips_when_no_bracket_label(self, db_path):
        from hightempbot.scheduler.station_healing import _heal_station_unit_if_wrong
        conn = get_connection(db_path)
        _insert_enrolled_station(conn, "KDAL", unit="F")
        market_data = {0: {"bracket_label": ""}, 1: {"bracket_label": None}}
        _heal_station_unit_if_wrong(conn, "KDAL", "F", market_data)
        row = conn.execute("SELECT unit FROM enrolled_stations WHERE icao = ?", ("KDAL",)).fetchone()
        assert row["unit"] == "F"
        conn.close()


class TestPositiveCacheTTL:
    def _insert_cached_row(self, conn, station_id, market_date, fetched_at, bi=0,
                          label="60-61°F", token="y", no_token="n", market="m"):
        conn.execute(
            """INSERT INTO market_tokens
            (station_id, market_date, bracket_idx, token_id, no_token_id, market_id,
             bracket_label, bracket_low, bracket_high, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (station_id, market_date, bi, token, no_token, market, label, 59.5, 61.5, fetched_at),
        )
        conn.commit()

    def test_stale_positive_cache_triggers_refetch(self, db_path):
        from hightempbot.scheduler.market_data import _fetch_market_data
        from unittest.mock import patch, MagicMock
        conn = get_connection(db_path)
        market_date = (datetime.now() + timedelta(days=1)).date()
        stale_fetched = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        self._insert_cached_row(conn, "KDAL", market_date.isoformat(), stale_fetched)

        response = MagicMock()
        response.status_code = 404
        with patch.dict("hightempbot.stations.ICAO_TO_CITY", {"KDAL": "dallas"}, clear=False), \
             patch("requests.get", return_value=response) as mget:
            _fetch_market_data("KDAL", target_date=market_date, conn=conn)

        remaining = conn.execute(
            "SELECT COUNT(*) as c FROM market_tokens WHERE station_id = ? AND market_date = ? AND bracket_idx != -1",
            ("KDAL", market_date.isoformat()),
        ).fetchone()["c"]
        assert remaining == 0  # stale row deleted
        assert mget.called  # Gamma refetch attempted
        conn.close()

    def test_fresh_positive_cache_returns_without_refetch(self, db_path):
        from hightempbot.scheduler.market_data import _fetch_market_data
        from unittest.mock import patch
        conn = get_connection(db_path)
        market_date = (datetime.now() + timedelta(days=1)).date()
        fresh_fetched = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._insert_cached_row(conn, "KDAL", market_date.isoformat(), fresh_fetched,
                                 bi=0, label="60-61°F", token="y0", no_token="n0", market="m0")
        with patch("requests.get") as mget:
            result = _fetch_market_data("KDAL", target_date=market_date, conn=conn)
        assert 0 in result
        assert result[0]["bracket_label"] == "60-61°F"
        assert not mget.called  # no Gamma fetch needed
        conn.close()
