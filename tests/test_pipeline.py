"""Tests for Unit 6: Pipeline Orchestrator."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hightempbot.db.connection import get_connection, init_db
from hightempbot.execution.pipeline import run_betting_cycle
from hightempbot.execution.types import BetSignal, OrderResult
from hightempbot.persistence.wallet_reconciliation import build_wallet_snapshot, record_wallet_snapshot

# Patch targets for calibration store
_PATCH_EMOS = "hightempbot.calibration.store.load_emos"
_PATCH_CAL_MODEL = "hightempbot.calibration.model.CalibrationModel"


@dataclass
class MockStation:
    station_id: str
    unit: str = "F"
    timezone: str = "US/Central"
    lat: float = 32.85
    lon: float = -96.85
    city: str = "Dallas"
    icao: str = "KDAL"


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    conn = get_connection(str(db_path))
    # ce-code-review P3 #70: schema.sql now seeds STOPPED_PROCESSING (safe halt
    # on fresh installs). Tests that exercise the betting pipeline need LIVE.
    from tests.conftest import seed_operator_live
    seed_operator_live(conn)
    # Seed through today so the fixed-reference coverage gate does not drift
    # as real calendar time advances.
    from datetime import date, timedelta
    from hightempbot.execution.strategy_constants import REF_START_DATE

    base = date.fromisoformat(REF_START_DATE)
    n_days = (date.today() - base).days + 1
    conn.executemany(
        "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
        [("KDAL", (base + timedelta(days=i)).isoformat(), 20.0 + (i % 10), "wu") for i in range(n_days)],
    )
    conn.commit()
    yield conn
    conn.close()


def _make_ensemble():
    from hightempbot.execution.strategy_constants import EXPECTED_MODELS
    return {m: 20.0 + 0.1 * i for i, m in enumerate(EXPECTED_MODELS)}


def _make_market_data(best_ask=0.30, volume=5000.0):
    """Create mock market data with F-station bracket bounds (2°F wide).

    Bracket labels carry the °F unit token because evaluate_station infers
    bracket_unit from the labels and fails closed if no token is present.
    """
    result = {}
    base = 59
    for i in range(11):
        mkt = {
            "best_ask": best_ask,
            "best_bid": 1.0 - best_ask - 0.02,
            "volume24hr": volume,
            "market_id": f"m_{i}",
            "token_id": f"t_{i}",
            "no_token_id": f"no_t_{i}",
        }
        if i == 0:
            mkt["bracket_low"] = None
            mkt["bracket_high"] = float(base)
            mkt["bracket_label"] = f"<{base}°F"
        elif i == 10:
            mkt["bracket_low"] = float(base + 1 + (i - 1) * 2)
            mkt["bracket_high"] = None
            mkt["bracket_label"] = f"≥{base + 1 + (i - 1) * 2}°F"
        else:
            lo = base + 1 + (i - 1) * 2
            mkt["bracket_low"] = float(lo)
            mkt["bracket_high"] = float(lo + 1)
            mkt["bracket_label"] = f"{lo}-{lo + 1}°F"
        result[i] = mkt
    return result


def _make_signal(**kwargs) -> BetSignal:
    defaults = dict(
        station_id="KDAL",
        target_date="2026-04-07",
        horizon=1,
        bracket_idx=3,
        threshold=68.0,
        bracket_label="66-68F YES",
        side="YES",
        p_model=0.45,
        p_market=0.30,
        edge=0.15,
        bet_size_usd=30.0,
        fill_price=0.30,
        volume_usd=5000.0,
        market_id="m1",
        token_id="t1",
        passed_all_gates=True,
        gate_results={
            "lut": True,
            "edge_gate": True,
            "max_per_market": True,
            "volume": True,
            "daily_notional": True,
            "idempotency": True,
        },
    )
    defaults.update(kwargs)
    return BetSignal(**defaults)


def _patch_calibration(mock_model):
    """Helper to patch calibration loading with a given mock model."""
    return [
        patch(_PATCH_EMOS, return_value=MagicMock()),
        patch(_PATCH_CAL_MODEL, return_value=mock_model),
    ]


class TestRunBettingCycle:
    def test_dry_run_produces_signals_no_orders(self, db):
        station = MockStation("KDAL")

        mock_model = MagicMock()
        mock_model.is_ready.return_value = True
        # Threshold-aware mock so per-bracket p_raw values vary realistically
        # across the 11 brackets, instead of collapsing every interior bracket
        # to P=0 (which would be filtered by the LUT-bucket gate at extremes
        # and dropped at the edge gate everywhere else).
        import math
        def _predict_cdf(_ensemble, threshold_c):
            return max(0.01, min(0.99, 1.0 / (1.0 + math.exp((threshold_c - 22.0) * 0.5))))
        mock_model.predict.side_effect = _predict_cdf

        patches = _patch_calibration(mock_model)
        for p in patches:
            p.start()
        try:
            result = run_betting_cycle(
                conn=db, station=station,
                ensemble_data=_make_ensemble(),
                market_data=_make_market_data(),
                order_client=None,
                initial_bankroll=1000.0,
                dry_run=True,
                target_date="2026-04-07",
            )
        finally:
            for p in patches:
                p.stop()

        assert result.n_evaluated > 0
        assert result.dry_run is True

        sig_count = db.execute("SELECT COUNT(*) as cnt FROM signals").fetchone()["cnt"]
        assert sig_count > 0

    def test_non_wu_actuals_do_not_satisfy_coverage_gate(self, db):
        station = MockStation("KDAL")
        db.execute("UPDATE actuals SET source = 'ncei' WHERE station_id = 'KDAL'")
        db.commit()

        mock_model = MagicMock()
        mock_model.is_ready.return_value = True
        patches = _patch_calibration(mock_model)
        for p in patches:
            p.start()
        try:
            with patch("hightempbot.execution.pipeline.evaluate_station") as mock_eval:
                result = run_betting_cycle(
                    conn=db,
                    station=station,
                    ensemble_data=_make_ensemble(),
                    market_data=_make_market_data(),
                    order_client=None,
                    initial_bankroll=1000.0,
                    dry_run=True,
                    target_date="2026-04-07",
                )
        finally:
            for p in patches:
                p.stop()

        assert result.n_evaluated == 0
        assert result.n_placed == 0
        mock_eval.assert_not_called()

    def test_local_now_minute_threads_through_to_hourly_gate(self, db):
        """Plumbing check: run_betting_cycle's local_now_minute reaches the
        per-strategy hourly-first-tick gate in _evaluate_strategy. The empty
        ledger means every bracket has slot_filled=0, so passing a tick-1
        minute should cause every strategy on every bracket to return None,
        emptying the signals list. The same setup without local_now_minute
        produces signals (see test_dry_run_produces_signals_no_orders)."""
        from hightempbot.execution.strategy_constants import (
            SCAN_INTERVAL_MINUTES, icao_tick_offset,
        )

        station = MockStation("KDAL")  # icao="KDAL"
        offset = icao_tick_offset(station.icao)
        tick_1_minute = (offset + SCAN_INTERVAL_MINUTES) % 60

        mock_model = MagicMock()
        mock_model.is_ready.return_value = True
        import math
        def _predict_cdf(_ensemble, threshold_c):
            return max(0.01, min(0.99, 1.0 / (1.0 + math.exp((threshold_c - 22.0) * 0.5))))
        mock_model.predict.side_effect = _predict_cdf

        patches = _patch_calibration(mock_model)
        for p in patches:
            p.start()
        try:
            result = run_betting_cycle(
                conn=db, station=station,
                ensemble_data=_make_ensemble(),
                market_data=_make_market_data(),
                order_client=None,
                initial_bankroll=1000.0,
                dry_run=True,
                target_date="2026-04-07",
                local_now_minute=tick_1_minute,
            )
        finally:
            for p in patches:
                p.stop()

        assert result.n_evaluated == 0
        assert result.n_placed == 0

    def test_drawdown_hard_stop_live_mode(self, db, monkeypatch):
        """At drawdown >= MAX_DD (default 0.40) the LIVE pipeline halts before
        any evaluation. Operator decision 2026-05-20 — replaces the prior
        "halve at MAX_DD" semantic. Existing PENDING positions still resolve;
        only new placement is suspended. (Epoch backdated so the seeded loss
        is in-session — the 2026-08-10 zero-reset gate must still halt on
        session losses.)
        """
        monkeypatch.setattr(
            "hightempbot.execution.strategy_constants.DASHBOARD_SESSION_START_UTC",
            "2026-01-01 00:00:00",
        )
        station = MockStation("KDAL")

        # Live drawdown is based on realized ledger PnL, not wallet cash that
        # is temporarily locked in open positions. Seed a $600 realized loss
        # so current basis is $400 versus a $1000 peak.
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, pnl, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-04-01 08:00:00", "KDAL", "m-loss", "t-loss", "2026-04-01",
             1, 70.0, "NO", 0.40, 0.30, 0.10,
             600.0, 5000.0, 600.0, 0.30, "LOSS", -600.0, "bet"),
        )
        db.commit()

        mock_client = MagicMock()
        mock_client.check_balance.return_value = 400.0
        with patch("hightempbot.execution.pipeline.evaluate_station") as mock_eval:
            result = run_betting_cycle(
                conn=db, station=station,
                ensemble_data=_make_ensemble(),
                market_data=_make_market_data(),
                order_client=mock_client,
                initial_bankroll=1000.0,
                dry_run=False,
                target_date="2026-04-07",
            )

        # Halt fires before evaluation — strong evidence the gate is
        # the drawdown one, not a downstream calibration/coverage skip.
        mock_eval.assert_not_called()
        assert result.n_evaluated == 0
        assert result.n_placed == 0

        # Halt is announced via pipeline_health so the dashboard can surface it.
        row = db.execute(
            "SELECT status, message FROM pipeline_health "
            "WHERE station_id = ? AND stage = 'gates' "
            "ORDER BY id DESC LIMIT 1",
            ("KDAL",),
        ).fetchone()
        assert row["status"] == "SKIP"
        assert "Halted" in row["message"]
        assert ">= MAX_DD" in row["message"]

    def test_drawdown_hard_stop_skipped_in_dry_run(self, db):
        """Dry-run mode bypasses the MAX_DD halt so operators can flip to
        dry_run to investigate behavior after a live drawdown (finding #16).
        Capital snapshots already filter on event_type='bet', so dry_run
        state can't fire the halt against itself.
        """
        station = MockStation("KDAL")
        # Same 60% drawdown as the live-mode test.
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, pnl, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-04-01T08:00:00Z", "KDAL", "m", "t", "2026-04-02",
             1, 68.0, "YES", 0.45, 0.30, 0.15,
             100.0, 5000.0, 100.0, 0.30,
             "LOSS", -600.0, "bet"),
        )
        db.commit()

        mock_model = MagicMock()
        mock_model.is_ready.return_value = True
        patches = _patch_calibration(mock_model)
        for p in patches:
            p.start()
        try:
            with patch(
                "hightempbot.execution.pipeline.evaluate_station", return_value=[]
            ) as mock_eval:
                run_betting_cycle(
                    conn=db, station=station,
                    ensemble_data=_make_ensemble(),
                    market_data=_make_market_data(),
                    order_client=None,
                    initial_bankroll=1000.0,
                    dry_run=True,
                    target_date="2026-04-07",
                )
        finally:
            for p in patches:
                p.stop()

        # Halt is suppressed → evaluation proceeds.
        halt_rows = db.execute(
            "SELECT message FROM pipeline_health "
            "WHERE station_id = ? AND stage = 'gates' "
            "AND message LIKE '%Halted%'",
            ("KDAL",),
        ).fetchall()
        assert len(halt_rows) == 0
        assert mock_eval.called

    def test_drawdown_at_exact_threshold_halts(self, db):
        """Boundary case: drawdown == MAX_DD must halt (>= comparison, not >).
        Without this test a future refactor that flips >= to > would slip
        through (finding #22).
        """
        station = MockStation("KDAL")
        # Exact 40% drawdown: $1000 initial - $400 loss = $600 realized -> 40%.
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, pnl, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-04-01T08:00:00Z", "KDAL", "m", "t", "2026-04-02",
             1, 68.0, "YES", 0.45, 0.30, 0.15,
             100.0, 5000.0, 100.0, 0.30,
             "LOSS", -400.0, "bet"),
        )
        db.commit()
        mock_client = MagicMock()
        mock_client.check_balance.return_value = 1000.0
        with patch("hightempbot.execution.pipeline.evaluate_station") as mock_eval:
            run_betting_cycle(
                conn=db, station=station,
                ensemble_data=_make_ensemble(),
                market_data=_make_market_data(),
                order_client=mock_client,
                initial_bankroll=1000.0,
                dry_run=False,
                target_date="2026-04-07",
            )
        mock_eval.assert_not_called()

    def test_drawdown_recovers_resumes_betting(self, db):
        """Recovery transition: after a WIN brings realized capital back above
        the (1 - MAX_DD) × peak threshold, the next tick must NOT halt
        (finding #18). Guards against any change that would make the halt
        sticky via persistent state.
        """
        station = MockStation("KDAL")
        # Step 1: realized LOSS = -600 → 60% drawdown → halt would fire.
        # Step 2: realized WIN = +400 → net realized = -200 → 20% drawdown
        # -> well below MAX_DD=0.40, halt must NOT fire.
        db.executemany(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, pnl, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("2026-04-01T08:00:00Z", "KDAL", "m1", "t1", "2026-04-02",
                 1, 68.0, "YES", 0.45, 0.30, 0.15,
                 100.0, 5000.0, 100.0, 0.30,
                 "LOSS", -600.0, "bet"),
                ("2026-04-03T08:00:00Z", "KDAL", "m2", "t2", "2026-04-04",
                 1, 68.0, "YES", 0.45, 0.30, 0.15,
                 100.0, 5000.0, 100.0, 0.30,
                 "WIN", 400.0, "bet"),
            ],
        )
        db.commit()
        mock_client = MagicMock()
        mock_client.check_balance.return_value = 1000.0
        mock_model = MagicMock()
        mock_model.is_ready.return_value = True
        patches = _patch_calibration(mock_model)
        for p in patches:
            p.start()
        try:
            with patch(
                "hightempbot.execution.pipeline.evaluate_station", return_value=[]
            ) as mock_eval:
                run_betting_cycle(
                    conn=db, station=station,
                    ensemble_data=_make_ensemble(),
                    market_data=_make_market_data(),
                    order_client=mock_client,
                    initial_bankroll=1000.0,
                    dry_run=False,
                    target_date="2026-04-07",
                )
        finally:
            for p in patches:
                p.stop()
        # Recovered — halt must not fire, evaluate_station IS reached.
        assert mock_eval.called

    def test_drawdown_below_threshold_does_not_halt(self, db):
        """Drawdown < MAX_DD lets the pipeline reach evaluate_station.

        Without this companion test the halt assertion above could pass
        trivially against any change that breaks evaluate_station entirely.
        """
        station = MockStation("KDAL")
        # 20% DD ($1000 - $200 = $800 realized) — well below MAX_DD=0.40.
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, pnl, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-04-01T08:00:00Z", "KDAL", "m", "t", "2026-04-02",
             1, 68.0, "YES", 0.45, 0.30, 0.15,
             100.0, 5000.0, 100.0, 0.30,
             "LOSS", -200.0, "bet"),
        )
        db.commit()

        mock_model = MagicMock()
        mock_model.is_ready.return_value = True
        patches = _patch_calibration(mock_model)
        for p in patches:
            p.start()
        try:
            with patch(
                "hightempbot.execution.pipeline.evaluate_station", return_value=[]
            ) as mock_eval:
                run_betting_cycle(
                    conn=db, station=station,
                    ensemble_data=_make_ensemble(),
                    market_data=_make_market_data(),
                    order_client=None,
                    initial_bankroll=1000.0,
                    dry_run=True,
                    target_date="2026-04-07",
                )
        finally:
            for p in patches:
                p.stop()

        mock_eval.assert_called_once()

    def test_empty_ensemble_returns_empty(self, db):
        station = MockStation("KDAL")

        result = run_betting_cycle(
            conn=db, station=station,
            ensemble_data={},
            market_data=_make_market_data(),
            order_client=None,
            initial_bankroll=1000.0,
            dry_run=True,
            target_date="2026-04-07",
        )
        assert result.n_evaluated == 0

    def test_operator_stop_blocks_before_evaluation(self, db):
        from hightempbot.execution.operator_control import stop_processing

        station = MockStation("KDAL")
        stop_processing(db, boot_dry_run=True)

        with patch("hightempbot.execution.pipeline.evaluate_station") as mock_eval:
            result = run_betting_cycle(
                conn=db,
                station=station,
                ensemble_data=_make_ensemble(),
                market_data=_make_market_data(),
                order_client=None,
                initial_bankroll=1000.0,
                dry_run=True,
                target_date="2026-04-07",
            )

        mock_eval.assert_not_called()
        assert result.n_evaluated == 0
        row = db.execute(
            "SELECT stage, status, message FROM pipeline_health ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["stage"] == "operator"
        assert row["status"] == "SKIP"
        assert "Stop Processing" in row["message"]

    def test_live_halt_when_wallet_unavailable(self, db):
        """ce-code-review P1 #22: live cycle halts when wallet read fails."""
        station = MockStation("KDAL")
        # check_balance returns None → wallet_available=False
        mock_client = MagicMock()
        mock_client.check_balance.return_value = None

        with patch("hightempbot.execution.pipeline.evaluate_station") as mock_eval:
            result = run_betting_cycle(
                conn=db,
                station=station,
                ensemble_data=_make_ensemble(),
                market_data=_make_market_data(),
                order_client=mock_client,
                initial_bankroll=1000.0,
                dry_run=False,
                target_date="2026-04-07",
            )

        mock_eval.assert_not_called()
        assert result.n_evaluated == 0
        row = db.execute(
            "SELECT stage, status, message FROM pipeline_health "
            "WHERE stage='gates' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["status"] == "ERROR"
        assert "wallet unavailable" in row["message"].lower()

    def test_live_halt_when_wallet_balance_below_floor(self, db):
        """ce-code-review P1 #23: live cycle halts when wallet < $5.00."""
        station = MockStation("KDAL")
        mock_client = MagicMock()
        mock_client.check_balance.return_value = 4.5  # below $5 floor

        with patch("hightempbot.execution.pipeline.evaluate_station") as mock_eval:
            result = run_betting_cycle(
                conn=db,
                station=station,
                ensemble_data=_make_ensemble(),
                market_data=_make_market_data(),
                order_client=mock_client,
                initial_bankroll=1000.0,
                dry_run=False,
                target_date="2026-04-07",
            )

        mock_eval.assert_not_called()
        assert result.n_evaluated == 0
        row = db.execute(
            "SELECT stage, status, message FROM pipeline_health "
            "WHERE stage='gates' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["status"] == "ERROR"
        assert "< $5" in row["message"]

    @patch("hightempbot.execution.pipeline.rank_signals", return_value=[])
    @patch("hightempbot.execution.pipeline.evaluate_station", return_value=[])
    def test_sizing_uses_stake_basis_not_deployable_capital(
        self,
        mock_evaluate,
        mock_rank,
        db,
    ):
        station = MockStation("KDAL")
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-04-07T00:00:00Z", "KDAL", "m", "t", "2026-04-07",
             1, 68.0, "YES", 0.45, 0.30, 0.15,
             40.0, 5000.0, 40.0, 0.30, "PENDING", "bet"),
        )
        db.commit()
        mock_model = MagicMock()
        mock_model.is_ready.return_value = True

        patches = _patch_calibration(mock_model)
        for p in patches:
            p.start()
        try:
            run_betting_cycle(
                conn=db, station=station,
                ensemble_data=_make_ensemble(),
                market_data=_make_market_data(),
                order_client=None,
                initial_bankroll=100.0,
                dry_run=True,
                target_date="2026-04-07",
            )
        finally:
            for p in patches:
                p.stop()

        assert mock_evaluate.call_args.kwargs["capital"] == pytest.approx(100.0)
        assert mock_rank.call_args.args[2] == pytest.approx(100.0)

    @patch("hightempbot.execution.pipeline.rank_signals", return_value=[])
    @patch("hightempbot.execution.pipeline.evaluate_station", return_value=[])
    def test_live_sizing_basis_adds_open_cost_to_wallet_cash(
        self,
        mock_evaluate,
        mock_rank,
        db,
    ):
        station = MockStation("KLAX")
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-05-21 12:04:12", "KLAX", "m", "t", "2026-05-21",
             1, 74.0, "NO", 0.30, 0.55, 0.10,
             2.75, 5000.0, 2.75, 0.55,
             "ord_1", "PENDING", "bet"),
        )
        db.execute(
            "INSERT INTO bankroll_peak (sampled_at, wallet_balance, realized_pnl, pending_exposure) "
            "VALUES (?, ?, ?, ?)",
            ("2026-05-21 08:00:00", 300.0, 0.0, 0.0),
        )
        db.commit()
        mock_model = MagicMock()
        mock_model.is_ready.return_value = True
        order_client = MagicMock()
        order_client.check_balance.return_value = 97.25

        patches = _patch_calibration(mock_model)
        for p in patches:
            p.start()
        try:
            run_betting_cycle(
                conn=db, station=station,
                ensemble_data=_make_ensemble(),
                market_data=_make_market_data(),
                order_client=order_client,
                initial_bankroll=100.0,
                dry_run=False,
                target_date="2026-05-21",
            )
        finally:
            for p in patches:
                p.stop()

        assert mock_evaluate.call_args.kwargs["capital"] == pytest.approx(100.0)
        assert mock_rank.call_args.args[2] == pytest.approx(100.0)

    @patch("hightempbot.execution.pipeline.rank_signals", return_value=[])
    @patch("hightempbot.execution.pipeline.evaluate_station", return_value=[])
    def test_live_sizing_basis_uses_data_api_position_value(
        self,
        mock_evaluate,
        mock_rank,
        db,
    ):
        station = MockStation("KLAX")
        wallet = "0x" + "d" * 40
        condition = "0x" + "1" * 64
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-05-22 12:04:12", "KLAX", condition, "token-no", "2026-05-22",
             1, 74.0, "NO", 0.30, 0.80, 0.10,
             5.0, 5000.0, 5.0, 0.80,
             "ord_1", 0.80, 6.25, "PENDING", "bet"),
        )
        db.commit()
        record_wallet_snapshot(
            db,
            build_wallet_snapshot(
                db,
                wallet_address=wallet,
                clob_balance_usd=95.0,
                chain_balance_usd=95.0,
                open_orders=[],
                data_api_trades=[],
                data_api_positions=[{
                    "asset": "token-no",
                    "conditionId": condition,
                    "size": 6.25,
                    "avgPrice": 0.80,
                    "initialValue": 5.0,
                    "currentValue": 6.25,
                    "cashPnl": 1.25,
                    "curPrice": 1.0,
                    "outcome": "No",
                }],
            ),
        )
        mock_model = MagicMock()
        mock_model.is_ready.return_value = True
        order_client = MagicMock()
        order_client._funder = wallet
        order_client.check_balance.return_value = 95.0

        patches = _patch_calibration(mock_model)
        for p in patches:
            p.start()
        try:
            run_betting_cycle(
                conn=db, station=station,
                ensemble_data=_make_ensemble(),
                market_data=_make_market_data(),
                order_client=order_client,
                initial_bankroll=100.0,
                dry_run=False,
                target_date="2026-05-22",
            )
        finally:
            for p in patches:
                p.stop()

        assert mock_evaluate.call_args.kwargs["capital"] == pytest.approx(101.25)
        assert mock_rank.call_args.args[2] == pytest.approx(101.25)

    def test_calibration_not_ready_skips(self, db):
        station = MockStation("KDAL")
        mock_model = MagicMock()
        mock_model.is_ready.return_value = False

        patches = _patch_calibration(mock_model)
        for p in patches:
            p.start()
        try:
            result = run_betting_cycle(
                conn=db, station=station,
                ensemble_data=_make_ensemble(),
                market_data=_make_market_data(),
                order_client=None,
                initial_bankroll=1000.0,
                dry_run=True,
                target_date="2026-04-07",
            )
        finally:
            for p in patches:
                p.stop()

        assert result.n_evaluated == 0

    def test_exception_in_evaluate_handled(self, db):
        station = MockStation("KDAL")

        mock_model = MagicMock()
        mock_model.is_ready.return_value = True
        mock_model.predict.side_effect = RuntimeError("calibration crash")

        patches = _patch_calibration(mock_model)
        for p in patches:
            p.start()
        try:
            result = run_betting_cycle(
                conn=db, station=station,
                ensemble_data=_make_ensemble(),
                market_data=_make_market_data(),
                order_client=None,
                initial_bankroll=1000.0,
                dry_run=True,
                target_date="2026-04-07",
            )
        finally:
            for p in patches:
                p.stop()

        assert result.n_evaluated == 0

    def test_execution_exception_does_not_cancel_stamped_order(self, db):
        station = MockStation("KDAL")
        signal = _make_signal()
        order_client = MagicMock()
        order_client.check_balance.return_value = 1000.0

        mock_model = MagicMock()
        mock_model.is_ready.return_value = True

        def stamp_order_then_crash(sig, order_client, dry_run, row_id, conn, config):
            conn.execute(
                """UPDATE ledger
                SET order_id = ?, fill_price = ?, fill_size = ?
                WHERE id = ?""",
                ("ord_live", 0.30, signal.bet_size_usd / signal.fill_price, row_id),
            )
            conn.commit()
            raise RuntimeError("post-submit crash")

        patches = _patch_calibration(mock_model)
        for p in patches:
            p.start()
        try:
            with (
                patch("hightempbot.execution.pipeline.evaluate_station", return_value=[signal]),
                patch("hightempbot.execution.pipeline.rank_signals", return_value=[signal]),
                patch("hightempbot.execution.pipeline.execute_or_log", side_effect=stamp_order_then_crash),
            ):
                result = run_betting_cycle(
                    conn=db,
                    station=station,
                    ensemble_data=_make_ensemble(),
                    market_data=_make_market_data(),
                    order_client=order_client,
                    initial_bankroll=1000.0,
                    dry_run=False,
                    target_date="2026-04-07",
                )
        finally:
            for p in patches:
                p.stop()

        row = db.execute(
            "SELECT outcome, order_id, pnl FROM ledger WHERE event_type = 'bet'"
        ).fetchone()
        assert result.n_placed == 0
        assert row["outcome"] == "PENDING"
        assert row["order_id"] == "ord_live"
        assert row["pnl"] is None

    def test_same_tick_exposure_does_not_double_count_committed_pending(self, db):
        station = MockStation("KDAL")
        signals = [
            _make_signal(
                bracket_idx=3,
                bracket_label="66-68F YES",
                market_id="m1",
                token_id="t1",
                bet_size_usd=50.0,
            ),
            _make_signal(
                bracket_idx=4,
                bracket_label="68-70F YES",
                market_id="m2",
                token_id="t2",
                bet_size_usd=50.0,
            ),
        ]
        order_client = MagicMock()
        order_client.check_balance.return_value = 100.0

        mock_model = MagicMock()
        mock_model.is_ready.return_value = True

        def fake_execute(sig, order_client, dry_run, row_id, conn, config):
            return OrderResult(
                order_id=f"ord_{row_id}",
                success=True,
                bet_size_usd=sig.bet_size_usd,
                fill_price=sig.fill_price,
                fill_size=sig.bet_size_usd / sig.fill_price,
            )

        patches = _patch_calibration(mock_model)
        for p in patches:
            p.start()
        try:
            with (
                patch("hightempbot.execution.pipeline.evaluate_station", return_value=signals),
                patch("hightempbot.execution.pipeline.rank_signals", return_value=signals),
                patch("hightempbot.execution.pipeline.execute_or_log", side_effect=fake_execute),
            ):
                result = run_betting_cycle(
                    conn=db,
                    station=station,
                    ensemble_data=_make_ensemble(),
                    market_data=_make_market_data(),
                    order_client=order_client,
                    initial_bankroll=100.0,
                    dry_run=False,
                    target_date="2026-04-07",
                )
        finally:
            for p in patches:
                p.stop()

        assert result.n_placed == 2
        assert result.total_exposure_usd == pytest.approx(100.0)
        rows = db.execute(
            "SELECT outcome, bet_size FROM ledger WHERE event_type = 'bet' ORDER BY id"
        ).fetchall()
        assert [row["bet_size"] for row in rows] == [50.0, 50.0]
