"""Tests for Unit 4: bet decision pipeline."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from hightempbot.db.connection import get_connection, init_db
from hightempbot.decision.strategies import (
    evaluate_station,
    rank_signals,
)
from hightempbot.execution.types import BetSignal


@dataclass
class MockStation:
    icao: str
    unit: str = "F"


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    conn = get_connection(str(db_path))
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _mock_wu_forecast():
    """Stub fetch_wu_forecast so tests never hit api.weather.com."""
    import hightempbot.ingestion.wu_forecast as wuf
    wuf.clear_cache()
    with patch("hightempbot.decision.strategies.fetch_wu_forecast", return_value=None) as m:
        yield m
    wuf.clear_cache()


def _make_market_data(n_brackets: int = 11, best_ask: float = 0.30, volume: float = 50000.0):
    """Create mock market data for n brackets with F-station bracket bounds."""
    result = {}
    base = 59
    for i in range(n_brackets):
        no_fill = 1.0 - best_ask - 0.02
        mkt = {
            "best_ask": best_ask,
            "best_bid": no_fill,
            "volume24hr": volume,
            "market_id": f"market_{i}",
            "token_id": f"token_{i}",
            "no_token_id": f"no_token_{i}",
            "_yes_book": {"asks": [{"price": best_ask, "size": 100000.0}]},
            "_no_book": {"asks": [{"price": no_fill, "size": 100000.0}]},
        }
        if i == 0:
            mkt["bracket_low"] = None
            mkt["bracket_high"] = float(base) + 0.5
            mkt["bracket_label"] = f"<{base}°F"
        elif i == n_brackets - 1:
            mkt["bracket_low"] = float(base + 1 + (i - 1) * 2) - 0.5
            mkt["bracket_high"] = None
            mkt["bracket_label"] = f"≥{base + 1 + (i - 1) * 2}°F"
        else:
            lo = base + 1 + (i - 1) * 2
            mkt["bracket_low"] = float(lo) - 0.5
            mkt["bracket_high"] = float(lo + 1) + 0.5
            mkt["bracket_label"] = f"{lo}-{lo + 1}°F"
        result[i] = mkt
    return result


def _make_model():
    """Stub CalibrationModel — the bracket probabilities come from a patched fn."""
    model = MagicMock()
    model.predict = MagicMock(return_value=0.5)
    model.is_ready = MagicMock(return_value=True)
    return model


def _seed_lut(
    conn: sqlite3.Connection,
    station_id: str,
    bucket_low: float,
    bucket_high: float,
    n: int,
    hits: int,
    refreshed_at: str | None = None,
) -> None:
    """Insert one lut_bucket_stats row directly (bypasses rebuild_lut)."""
    refreshed_at = refreshed_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    observed = hits / n if n > 0 else None
    mean_pred = (bucket_low + bucket_high) / 2
    conn.execute(
        "INSERT OR REPLACE INTO lut_bucket_stats "
        "(station_id, pred_bucket_low, pred_bucket_high, n, hits, "
        " observed, mean_pred, refreshed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (station_id, bucket_low, bucket_high, n, hits, observed, mean_pred, refreshed_at),
    )
    conn.commit()


def _seed_all_buckets(conn: sqlite3.Connection, station_id: str, n: int, observed: float) -> None:
    """Seed every notebook Cell F bucket for a station with identical (n, observed)."""
    from hightempbot.calibration.lut import BUCKETS
    for lo, hi in BUCKETS:
        _seed_lut(conn, station_id, lo, hi, n=n, hits=int(round(n * observed)))
    # Backfill `n` synthetic resolved days per bucket so cumulative lookups
    # see enough history. Each bucket gets the same `n` rows with distinct
    # local_dates — UNIQUE(station_id, local_date, pred_bucket_low) holds
    # since the `pred_bucket_low` differs per bucket.
    # All dates are anchored well before any plausible test asof_date so the
    # strict `local_date < asof_local_date` cumulative lookup includes them.
    base = datetime(2020, 1, 1)
    rows: list[tuple] = []
    for lo, hi in BUCKETS:
        for i in range(n):
            local_date = (base + timedelta(days=i)).date().isoformat()
            rows.append((
                station_id, local_date, lo, hi,
                (lo + hi) / 2,  # emos_p ~= bucket midpoint
                1 if i < int(round(n * observed)) else 0,
            ))
    conn.executemany(
        "INSERT OR IGNORE INTO pred_bucket_history "
        "(station_id, local_date, pred_bucket_low, pred_bucket_high, emos_p, hit) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


class TestStationEvaluationCoverage:
    def test_non_eleven_bracket_count_still_evaluates(self, db: sqlite3.Connection):
        """Markets with fewer than 11 brackets are no longer auto-skipped."""
        station = MockStation("KDAL", "F")
        model = _make_model()
        ensemble = np.array([20.0] * 5)
        market = _make_market_data(n_brackets=9, best_ask=0.20)

        with patch("hightempbot.decision.strategies.bracket_probabilities", return_value=[0.25] * 9) as mock_probs:
            signals = evaluate_station(
                conn=db, station=station, model=model, ensemble_members=ensemble,
                market_data=market, capital=1000.0,
                target_date="2026-04-07", horizon=1,
            )

        assert signals
        mock_probs.assert_called_once()


class TestRankSignals:
    def _signal(
        self,
        edge: float,
        kelly: float,
        station: str = "A",
        target_date: str = "2026-04-07",
    ) -> BetSignal:
        return BetSignal(
            station_id=station, target_date=target_date, horizon=1,
            bracket_idx=0, threshold=68.0, bracket_label="test",
            side="YES", p_model=0.45, p_market=0.30, edge=edge,
            bet_size_usd=kelly, fill_price=0.30, volume_usd=5000.0,
            passed_all_gates=True,
        )

    def test_sorts_by_edge_descending(self, db: sqlite3.Connection):
        sigs = [self._signal(edge=0.15, kelly=5.0), self._signal(edge=0.25, kelly=5.0)]
        ranked = rank_signals(db, sigs, capital=1000.0)
        assert ranked[0].edge == 0.25
        assert ranked[1].edge == 0.15

    @patch("hightempbot.decision.strategies.MAX_DAILY_NOTIONAL_FRAC", 0.30)
    def test_daily_notional_cap_drops_overflow(self, db: sqlite3.Connection):
        # Cap = 0.30 * 100 = $30. Two signals at $20 each — first fits, second doesn't.
        # Patches the cap to 0.30 because production config sets it to 1.00 (no cap).
        sigs = [self._signal(edge=0.25, kelly=20.0, station="A"),
                self._signal(edge=0.15, kelly=20.0, station="B")]
        ranked = rank_signals(db, sigs, capital=100.0)
        assert len(ranked) == 1
        assert ranked[0].station_id == "A"  # best-edge wins the budget
        dropped = [s for s in sigs if s.station_id == "B"][0]
        assert dropped.passed_all_gates is False
        assert dropped.gate_results.get("daily_notional") is False

    @patch("hightempbot.decision.strategies.MAX_DAILY_NOTIONAL_FRAC", 0.30)
    def test_existing_budget_usage_reduces_available(self, db: sqlite3.Connection):
        # Pre-insert a $25 bet for the same target date so only $5 remains.
        # Patches the cap to 0.30 because production config sets it to 1.00 (no cap).
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, event_type)
            VALUES (datetime('now'), 'A', 'm', 't', '2026-04-07',
             1, 68.0, 'YES', 0.45, 0.30, 0.15,
             25.0, 5000.0, 25.0, 0.30, 'PENDING', 'bet')""",
        )
        db.commit()
        sigs = [self._signal(edge=0.25, kelly=10.0)]
        ranked = rank_signals(db, sigs, capital=100.0)
        assert ranked == []  # $10 stake exceeds $5 remaining budget

    def test_existing_budget_usage_for_other_target_date_is_ignored(self, db: sqlite3.Connection):
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, event_type)
            VALUES (datetime('now'), 'A', 'm', 't', '2026-04-06',
             1, 68.0, 'YES', 0.45, 0.30, 0.15,
             30.0, 5000.0, 30.0, 0.30, 'PENDING', 'bet')""",
        )
        db.commit()
        sigs = [self._signal(edge=0.25, kelly=10.0, target_date="2026-04-07")]

        ranked = rank_signals(db, sigs, capital=100.0)

        assert len(ranked) == 1

    def test_each_target_date_gets_its_own_budget(self, db: sqlite3.Connection):
        # Cap = $30 per target date. Two $20 signals fit when targets differ.
        sigs = [
            self._signal(edge=0.25, kelly=20.0, station="A", target_date="2026-04-07"),
            self._signal(edge=0.15, kelly=20.0, station="B", target_date="2026-04-08"),
        ]

        ranked = rank_signals(db, sigs, capital=100.0)

        assert len(ranked) == 2
        assert {s.target_date for s in ranked} == {"2026-04-07", "2026-04-08"}

    def test_live_budget_ignores_dry_run_usage(self, db: sqlite3.Connection):
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, event_type)
            VALUES (datetime('now'), 'A', 'm', 't', '2026-04-07',
             1, 68.0, 'YES', 0.45, 0.30, 0.15,
             30.0, 5000.0, 30.0, 0.30, 'PENDING', 'dry_run')""",
        )
        db.commit()
        sigs = [self._signal(edge=0.25, kelly=1.0)]

        ranked = rank_signals(db, sigs, capital=100.0, dry_run=False)

        assert len(ranked) == 1

    @patch("hightempbot.decision.strategies.MAX_DAILY_NOTIONAL_FRAC", 0.30)
    def test_dry_run_budget_counts_dry_run_usage(self, db: sqlite3.Connection):
        # Patches the cap to 0.30 because production config sets it to 1.00 (no cap).
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, event_type)
            VALUES (datetime('now'), 'A', 'm', 't', '2026-04-07',
             1, 68.0, 'YES', 0.45, 0.30, 0.15,
             30.0, 5000.0, 30.0, 0.30, 'PENDING', 'dry_run')""",
        )
        db.commit()
        sigs = [self._signal(edge=0.25, kelly=1.0)]

        ranked = rank_signals(db, sigs, capital=100.0, dry_run=True)

        assert ranked == []

    def test_cancelled_bets_ignored_in_budget(self, db: sqlite3.Connection):
        # CANCELLED rows should not eat target-date budget.
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, event_type)
            VALUES (datetime('now'), 'A', 'm', 't', '2026-04-07',
             1, 68.0, 'YES', 0.45, 0.30, 0.15,
             25.0, 5000.0, 25.0, 0.30, 'CANCELLED', 'bet')""",
        )
        db.commit()
        sigs = [self._signal(edge=0.25, kelly=10.0)]
        ranked = rank_signals(db, sigs, capital=100.0)
        assert len(ranked) == 1

    def test_empty_signals(self, db: sqlite3.Connection):
        ranked = rank_signals(db, [], capital=1000.0)
        assert ranked == []


class TestWuConsensusGateModes:
    """Regression tests for SHADOW vs BLOCK gate aggregator semantics."""

    def _seed_passable_no_bet(self, db: sqlite3.Connection):
        from tests.test_decision import _seed_all_buckets, _make_model, _make_market_data
        station = MockStation("KDAL", "F")
        _seed_all_buckets(db, "KDAL", n=200, observed=0.11)
        model = _make_model()
        ensemble = np.array([20.0] * 5)
        market = _make_market_data(best_ask=0.23, volume=50000.0)
        for mkt in market.values():
            mkt["best_bid"] = 0.75
            mkt["_no_book"] = {"asks": [{"price": 0.75, "size": 100000.0}]}
        return station, model, ensemble, market

    @patch("hightempbot.decision.strategies.WU_CONSENSUS_MODE", "SHADOW")
    @patch("hightempbot.decision.strategies.fetch_wu_forecast", return_value=18.0)
    def test_shadow_does_not_block_on_disagree(self, _mock_wu, db: sqlite3.Connection):
        # WU = 18°C, all NO bets target ≥0°F brackets — WU is well inside many
        # bracket interiors so the gate's verdict for those NO bets is False
        # (disagree). In SHADOW the False verdict must NOT block the bet.
        station, model, ensemble, market = self._seed_passable_no_bet(db)
        with patch("hightempbot.decision.strategies.bracket_probabilities", return_value=[0.12] * 11):
            signals = evaluate_station(
                conn=db, station=station, model=model, ensemble_members=ensemble,
                market_data=market, capital=1000.0,
                target_date="2026-04-07", horizon=1,
            )
        passing = [s for s in signals if s.side == "NO" and s.passed_all_gates]
        assert passing, "SHADOW must not block bets when WU disagrees"
        # Verdict still recorded on the signal for telemetry.
        # (Some are True / some False depending on bracket; at least one False
        # must exist somewhere across the 11 brackets and still have its bet
        # in the passing set, proving the verdict is stored without blocking.)
        any_disagree = any(
            s.wu_consensus_verdict is False and s.passed_all_gates
            for s in signals if s.side == "NO"
        )
        assert any_disagree, "Expected at least one NO bet with WU disagree to still pass in SHADOW"
        # gate_results must NOT carry the wu_consensus_shadow key (or any
        # WU key) in SHADOW — the verdict lives on wu_consensus_verdict.
        for s in signals:
            assert "wu_consensus" not in s.gate_results
            assert "wu_consensus_shadow" not in s.gate_results

    @patch("hightempbot.decision.strategies.WU_CONSENSUS_MODE", "BLOCK")
    @patch("hightempbot.decision.strategies.fetch_wu_forecast", return_value=18.0)
    def test_block_blocks_on_disagree(self, _mock_wu, db: sqlite3.Connection):
        # In BLOCK, a WU disagree on a candidate must drive passed_all_gates
        # to False with gate_results["wu_consensus"] = False.
        station, model, ensemble, market = self._seed_passable_no_bet(db)
        with patch("hightempbot.decision.strategies.bracket_probabilities", return_value=[0.12] * 11):
            signals = evaluate_station(
                conn=db, station=station, model=model, ensemble_members=ensemble,
                market_data=market, capital=1000.0,
                target_date="2026-04-07", horizon=1,
            )
        # At least one signal must be blocked specifically by wu_consensus.
        blocked = [
            s for s in signals
            if s.side == "NO"
            and s.gate_results.get("wu_consensus") is False
            and not s.passed_all_gates
        ]
        assert blocked, "BLOCK must block at least one bet via wu_consensus=False"

    @patch("hightempbot.decision.strategies.WU_CONSENSUS_MODE", "BLOCK")
    @patch("hightempbot.decision.strategies.fetch_wu_forecast", return_value=None)
    def test_block_fails_closed_when_wu_unavailable(self, _mock_wu, db: sqlite3.Connection):
        # WU unavailable → gate returns None. In BLOCK this MUST be mapped to
        # False before being written to gate_results, otherwise the all-True
        # comparator filters None and the bet silently passes (fail-open).
        station, model, ensemble, market = self._seed_passable_no_bet(db)
        with patch("hightempbot.decision.strategies.bracket_probabilities", return_value=[0.12] * 11):
            signals = evaluate_station(
                conn=db, station=station, model=model, ensemble_members=ensemble,
                market_data=market, capital=1000.0,
                target_date="2026-04-07", horizon=1,
            )
        passing = [s for s in signals if s.side == "NO" and s.passed_all_gates]
        assert not passing, "BLOCK must fail-closed (no bets pass) when WU is unavailable"
        # Every NO signal that reached the WU gate must carry False in gate_results.
        no_signals = [s for s in signals if s.side == "NO" and "wu_consensus" in s.gate_results]
        assert no_signals
        for s in no_signals:
            assert s.gate_results["wu_consensus"] is False
            assert s.wu_consensus_verdict is None  # raw verdict still records "unavailable"
