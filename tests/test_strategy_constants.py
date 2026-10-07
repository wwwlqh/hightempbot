"""Tests for Unit 1: execution config, types, capital, and schema."""

import sqlite3
from pathlib import Path

import pytest

from hightempbot.execution.strategy_constants import (
    EXPECTED_MODELS,
    LUT_STALE_HOURS,
    MAX_DAILY_NOTIONAL_FRAC,
    MAX_DD,
    MAX_EDGE,
    MAX_ORDER_RETRIES,
    MIN_BET_USD,
    MIN_BVOL,
    MIN_EDGE,
    MIN_PAIRS,
    ORDER_RETRY_BACKOFF_S,
    ORDER_VERIFY_POLL_S,
    REF_START_DATE,
    REQUIRED_MEMBERS,
    RESOLUTION_PRICE_THRESHOLD,
    RESOLUTION_SCAN_START_HOUR,
    SCAN_INTERVAL_MINUTES,
    STRATEGY_CONFIGS,
    VERIFY_POLY_TIMEOUT_S,
)
from hightempbot.execution.types import BetSignal
from hightempbot.execution.capital import (
    get_capital_snapshot,
    return_transfer_notional,
)
from hightempbot.db.connection import get_connection, init_db
from hightempbot.persistence.wallet_reconciliation import build_wallet_snapshot, record_wallet_snapshot


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    conn = get_connection(str(db_path))
    yield conn
    conn.close()


class TestExecutionConfig:
    def test_constants_have_expected_values(self):
        assert MIN_BET_USD == 1.0
        # 2026-05-24: MAX_DD lowered 0.50 -> 0.40 after the ABCD
        # re-optimization through 2026-05-21.
        assert MAX_DD == 0.40
        assert MAX_DAILY_NOTIONAL_FRAC == 1.00  # No cap (raised from 0.30 in 244c719).
        assert MIN_EDGE == 0.03
        assert MAX_EDGE == 0.10
        # 2026-05-07: lowered 500 -> 50 to match backtest "optimus" gate.
        # Reverses 2026-05-06 keep-at-500 decision after dry-run divergence.
        assert MIN_BVOL == 50
        assert RESOLUTION_PRICE_THRESHOLD == 0.995
        assert REF_START_DATE == "2024-03-01"

    def test_min_edge_strictly_below_max_edge(self):
        # Inverting these collapses the bet band to zero; catch that at import time.
        assert MIN_EDGE < MAX_EDGE

    def test_lcb_live_constants(self):
        assert LUT_STALE_HOURS == 36
        assert MIN_PAIRS == 30
        assert MAX_ORDER_RETRIES == 3
        assert VERIFY_POLY_TIMEOUT_S == 15
        assert ORDER_RETRY_BACKOFF_S == 2
        assert ORDER_VERIFY_POLL_S == 3

    def test_expected_models_list(self):
        assert len(EXPECTED_MODELS) == 9
        assert "ecmwf_ifs025" in EXPECTED_MODELS
        assert "bom_access_global" not in EXPECTED_MODELS
        assert "jma_seamless" not in EXPECTED_MODELS

    def test_required_members_locked_at_nine(self):
        assert REQUIRED_MEMBERS == 9

    def test_constants_are_correct_types(self):
        assert isinstance(MIN_BET_USD, float)
        assert isinstance(MAX_DD, float)
        assert isinstance(MIN_EDGE, (int, float))
        assert isinstance(MAX_EDGE, (int, float))
        assert isinstance(EXPECTED_MODELS, list)
        assert isinstance(RESOLUTION_SCAN_START_HOUR, int)
        assert isinstance(SCAN_INTERVAL_MINUTES, int)
        assert isinstance(LUT_STALE_HOURS, int)
        assert isinstance(MAX_ORDER_RETRIES, int)


class TestBetSignal:
    def test_creates_with_defaults(self):
        sig = BetSignal(
            station_id="KDAL", target_date="2026-04-07", horizon=1,
            bracket_idx=3, threshold=68.0, bracket_label="66-68°F YES",
            side="YES", p_model=0.45, p_market=0.30, edge=0.15,
            bet_size_usd=1.0, fill_price=0.30, volume_usd=5000.0,
            market_id="m1", token_id="t1",
        )
        assert sig.passed_all_gates is False


class TestCapital:
    def test_initial_capital(self, db):
        snapshot = get_capital_snapshot(db, 100.0)
        capital, peak = snapshot.deployable_capital, snapshot.peak_realized_capital
        assert capital == 100.0
        assert peak == 100.0

    def test_capital_after_win(self, db):
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, pnl, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-04-06T08:00:00Z", "KDAL", "m", "t", "2026-04-07",
             1, 68.0, "YES", 0.45, 0.30, 0.15,
             1.0, 5000.0, 1.0, 0.30, "WIN", 2.33, "bet"),
        )
        db.commit()
        snapshot = get_capital_snapshot(db, 100.0)
        capital, peak = snapshot.deployable_capital, snapshot.peak_realized_capital
        assert capital == 102.33
        assert peak == 102.33

    def test_dry_run_excluded_from_capital(self, db):
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, pnl, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-04-06T08:00:00Z", "KDAL", "m", "t", "2026-04-07",
             1, 68.0, "YES", 0.45, 0.30, 0.15,
             1.0, 5000.0, 1.0, 0.30, "LOSS", -1.0, "dry_run"),
        )
        db.commit()
        snapshot = get_capital_snapshot(db, 100.0)
        capital, peak = snapshot.deployable_capital, snapshot.peak_realized_capital
        assert capital == 100.0  # dry-run excluded
        assert peak == 100.0

    def test_pending_exposure_reduces_deployable_but_not_realized_drawdown_base(self, db):
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, pnl, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-04-06T08:00:00Z", "KDAL", "m1", "t1", "2026-04-07",
             1, 68.0, "YES", 0.45, 0.30, 0.15,
             1.0, 5000.0, 10.0, 0.30, "WIN", 20.0, "bet"),
        )
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-04-07T08:00:00Z", "KDAL", "m2", "t2", "2026-04-08",
             1, 70.0, "YES", 0.40, 0.25, 0.10,
             5.0, 5000.0, 5.0, 0.25, "PENDING", "bet"),
        )
        db.commit()

        snapshot = get_capital_snapshot(db, 100.0)

        assert snapshot.realized_capital == 120.0
        assert snapshot.deployable_capital == 115.0
        assert snapshot.stake_basis_capital == 120.0
        assert snapshot.pending_exposure == 5.0
        assert snapshot.peak_realized_capital == 120.0

    def test_live_stake_basis_adds_open_cost_to_wallet_cash(self, db):
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-05-21 11:04:05", "KDAL", "m2", "t2", "2026-05-21",
             1, 75.0, "NO", 0.30, 0.77, 0.10,
             4.9973, 5000.0, 4.9973, 0.77,
             "ord_0", "PENDING", "bet"),
        )
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-05-21 12:04:12", "KLAX", "m1", "t1", "2026-05-21",
             1, 74.0, "NO", 0.30, 0.55, 0.10,
             2.75, 5000.0, 2.75, 0.55,
             "ord_1", "PENDING", "bet"),
        )
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-05-21 13:14:22", "KLAX", "m1", "t1", "2026-05-21",
             1, 74.0, "NO", 0.30, 0.53, 0.10,
             1.855, 5000.0, 1.855, 0.53,
             "ord_2", "PENDING", "bet"),
        )
        db.execute(
            "INSERT INTO bankroll_peak (sampled_at, wallet_balance, realized_pnl, pending_exposure) "
            "VALUES (?, ?, ?, ?)",
            ("2026-05-21 08:19:10", 106.09, 0.0, 0.0),
        )
        db.commit()

        class OrderClient:
            def check_balance(self):
                return 90.23478

        snapshot = get_capital_snapshot(
            db,
            100.0,
            order_client=OrderClient(),
            dry_run=False,
        )

        assert snapshot.realized_capital == pytest.approx(100.0)
        assert snapshot.deployable_capital == pytest.approx(90.23478)
        assert snapshot.pending_exposure == pytest.approx(9.6023)
        assert snapshot.stake_basis_capital == pytest.approx(100.0)
        assert snapshot.peak_realized_capital == pytest.approx(100.0)

    def test_live_stake_basis_prefers_data_api_position_value(self, db):
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

        class OrderClient:
            _funder = wallet

            def check_balance(self):
                return 95.0

        snapshot = get_capital_snapshot(
            db,
            100.0,
            order_client=OrderClient(),
            dry_run=False,
        )

        assert snapshot.capital_source == "polymarket_data_api"
        assert snapshot.data_api_position_value == pytest.approx(6.25)
        assert snapshot.stake_basis_capital == pytest.approx(101.25)
        assert snapshot.deployable_capital == pytest.approx(95.0)

    def test_live_stake_basis_data_api_floor_uses_realized_ledger_capital(self, db):
        wallet = "0x" + "d" * 40
        condition = "0x" + "1" * 64
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, event_type, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-05-21 12:04:12", "KLAX", "settled", "settled-token", "2026-05-21",
             1, 74.0, "NO", 0.30, 0.55, 0.10,
             2.75, 5000.0, 2.75, 0.55,
             "WIN", "bet", 20.0),
        )
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size, outcome, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-05-22 12:04:12", "KLAX", condition, "token-no", "2026-05-22",
             1, 74.0, "NO", 0.30, 0.80, 0.10,
             4.0, 5000.0, 4.0, 0.80,
             "ord_1", 0.80, 5.0, "PENDING", "bet"),
        )
        db.commit()
        record_wallet_snapshot(
            db,
            build_wallet_snapshot(
                db,
                wallet_address=wallet,
                clob_balance_usd=50.0,
                chain_balance_usd=50.0,
                open_orders=[],
                data_api_trades=[],
                data_api_positions=[{
                    "asset": "token-no",
                    "conditionId": condition,
                    "size": 5.0,
                    "avgPrice": 0.80,
                    "initialValue": 4.0,
                    "currentValue": 5.0,
                    "cashPnl": 1.0,
                    "curPrice": 1.0,
                    "outcome": "No",
                }],
            ),
        )

        class OrderClient:
            _funder = wallet

            def check_balance(self):
                return 50.0

        snapshot = get_capital_snapshot(
            db,
            100.0,
            order_client=OrderClient(),
            dry_run=False,
        )

        assert snapshot.capital_source == "polymarket_data_api"
        assert snapshot.data_api_position_value == pytest.approx(5.0)
        assert snapshot.stake_basis_capital == pytest.approx(120.0)
        assert snapshot.realized_capital == pytest.approx(120.0)
        assert snapshot.deployable_capital == pytest.approx(50.0)

    def test_live_stake_basis_excludes_mismatched_data_api_position(self, db):
        wallet = "0x" + "d" * 40
        condition = "0x" + "2" * 64
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
                    "size": 12.0,
                    "avgPrice": 0.75,
                    "initialValue": 9.0,
                    "currentValue": 10.0,
                    "cashPnl": 1.0,
                    "curPrice": 0.8333,
                    "outcome": "No",
                }],
            ),
        )

        class OrderClient:
            _funder = wallet

            def check_balance(self):
                return 95.0

        snapshot = get_capital_snapshot(
            db,
            100.0,
            order_client=OrderClient(),
            dry_run=False,
        )

        assert snapshot.capital_source == "polymarket_data_api"
        assert snapshot.data_api_position_value == 0.0
        assert snapshot.stake_basis_capital == pytest.approx(100.0)
        assert snapshot.data_api_reconciliation_warnings

    def test_live_stake_basis_does_not_fallback_to_other_wallet_snapshot(self, db):
        configured_wallet = "0x" + "d" * 40
        other_wallet = "0x" + "e" * 40
        record_wallet_snapshot(
            db,
            build_wallet_snapshot(
                db,
                wallet_address=other_wallet,
                clob_balance_usd=70.0,
                chain_balance_usd=70.0,
                open_orders=[],
                data_api_trades=[],
                data_api_positions=[{
                    "asset": "token-other",
                    "conditionId": "0x" + "9" * 64,
                    "size": 10.0,
                    "avgPrice": 0.50,
                    "initialValue": 5.0,
                    "currentValue": 10.0,
                    "cashPnl": 5.0,
                    "curPrice": 1.0,
                    "outcome": "No",
                }],
            ),
        )

        class OrderClient:
            _funder = configured_wallet

            def check_balance(self):
                return 95.0

        snapshot = get_capital_snapshot(
            db,
            100.0,
            order_client=OrderClient(),
            dry_run=False,
        )

        assert snapshot.capital_source == "ledger_fallback"
        assert snapshot.data_api_position_value == 0.0
        assert snapshot.stake_basis_capital == pytest.approx(100.0)

    def test_live_stake_basis_floors_to_resolved_ledger_capital(self, db):
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, event_type, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-05-21 12:04:12", "KLAX", "m1", "t1", "2026-05-21",
             1, 74.0, "NO", 0.30, 0.55, 0.10,
             2.75, 5000.0, 2.75, 0.55,
             "WIN", "bet", 5.22),
        )
        db.commit()

        class OrderClient:
            def check_balance(self):
                return 49.41

        snapshot = get_capital_snapshot(
            db,
            100.0,
            order_client=OrderClient(),
            dry_run=False,
        )

        assert snapshot.realized_capital == pytest.approx(105.22)
        assert snapshot.stake_basis_capital == pytest.approx(105.22)
        assert snapshot.deployable_capital == pytest.approx(49.41)
        assert snapshot.peak_realized_capital == pytest.approx(105.22)

    def test_live_return_transfer_reduces_stake_basis_without_drawdown(self, db):
        db.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             outcome, event_type, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-05-21 12:04:12", "KLAX", "m1", "t1", "2026-05-21",
             1, 74.0, "NO", 0.30, 0.55, 0.10,
             2.75, 5000.0, 2.75, 0.55,
             "WIN", "bet", 5.22),
        )
        db.execute(
            """INSERT INTO transfer_requests
            (from_wallet, to_wallet, amount_usd, status, confirmation)
            VALUES (?, ?, ?, 'SUBMITTED', ?)""",
            ("0x" + "d" * 40, "0x" + "e" * 40, 10.0, "ok"),
        )
        db.commit()

        class OrderClient:
            def check_balance(self):
                return 90.0

        snapshot = get_capital_snapshot(
            db,
            100.0,
            order_client=OrderClient(),
            dry_run=False,
        )

        assert snapshot.realized_capital == pytest.approx(95.22)
        assert snapshot.stake_basis_capital == pytest.approx(95.22)
        assert snapshot.peak_realized_capital == pytest.approx(95.22)

    def test_stale_submitting_transfer_notional_is_not_counted(self, db):
        db.execute(
            """INSERT INTO transfer_requests
            (created_at, updated_at, from_wallet, to_wallet, amount_usd, status, confirmation)
            VALUES (?, ?, ?, ?, ?, 'SUBMITTING', ?)""",
            (
                "2026-01-01 00:00:00",
                "2026-01-01 00:00:00",
                "0x" + "d" * 40,
                "0x" + "e" * 40,
                10.0,
                "stale",
            ),
        )
        db.execute(
            """INSERT INTO transfer_requests
            (from_wallet, to_wallet, amount_usd, status, confirmation)
            VALUES (?, ?, ?, 'SUBMITTED', ?)""",
            ("0x" + "d" * 40, "0x" + "e" * 40, 7.0, "submitted"),
        )
        db.commit()

        assert return_transfer_notional(db) == pytest.approx(7.0)


class TestL2ChampionParity:
    """R8: pin the live config to the L2 champion JSON (the source of truth)."""

    @staticmethod
    def _champion() -> dict:
        import json
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "backtest" / "configs" / "candidate_l2_depth.json"
        )
        if not path.exists():
            pytest.skip(f"L2 champion config not present at {path}")
        return json.loads(path.read_text())

    def test_removed_l2_premium_gate_matches_champion(self):
        champ = self._champion()
        assert champ["max_l2_ask_premium"] is None

    def test_no_sleeve_matches_champion(self):
        champ = self._champion()["NO"]
        no = STRATEGY_CONFIGS["NO"]
        assert no.enabled is bool(champ["enabled"])
        assert no.execution_min_edge == champ["execution_min_edge"]
        assert no.min_edge == champ["no_min_edge"]
        assert no.max_edge == champ["max_edge"]
        assert no.capital_frac == champ["size_frac"]            # JSON size_frac -> capital_frac
        assert sorted(no.entry_hour_set) == sorted(champ["entry_local_hours"])

    def test_tail_sleeve_matches_champion(self):
        champ = self._champion()["TAIL"]
        tail = STRATEGY_CONFIGS["TAIL"]
        assert tail.enabled is bool(champ["enabled"])           # re-enabled per operator
        assert tail.alpha_ratio == champ["alpha"]               # JSON alpha -> alpha_ratio
        assert tail.fp_max == champ["fp_max"]
        assert tail.fp_min == champ["fp_min"]
        assert tail.delayed_entry_fp_max == champ["delayed_entry_fp_max"]
        assert tail.execution_min_edge == champ["execution_min_edge"]
        assert tail.consensus_skip_threshold == champ["consensus_skip_threshold"]
        assert tail.capital_frac == champ["size_frac"]
        assert sorted(tail.entry_hour_set) == sorted(champ["entry_local_hours"])
        assert tail.tp == champ["tp"]

    def test_disabled_sleeves_match_champion(self):
        champ = self._champion()
        assert STRATEGY_CONFIGS["YMID"].enabled is bool(champ["YMID"]["enabled"])
        assert STRATEGY_CONFIGS["YHIGH"].enabled is bool(champ["YHIGH"]["enabled"])

    def test_vwap_slip_leashes_match_live_execution_policy(self):
        policy = self._champion()["live_execution_policy"]
        assert STRATEGY_CONFIGS["NO"].max_vwap_slip_from_anchor == policy["NO"]["max_vwap_slip_from_anchor"]
        assert STRATEGY_CONFIGS["TAIL"].max_vwap_slip_from_anchor == policy["TAIL"]["max_vwap_slip_from_anchor"]
        # Disabled sleeves carry no slip leash.
        assert STRATEGY_CONFIGS["YMID"].max_vwap_slip_from_anchor is None
        assert STRATEGY_CONFIGS["YHIGH"].max_vwap_slip_from_anchor is None


class TestL2ChampionConstantPins:
    """Non-skipping value pins for the ported L2 champion constants."""

    def test_no_sleeve_values(self):
        no = STRATEGY_CONFIGS["NO"]
        assert no.enabled is True
        assert no.execution_min_edge == 0.05            # L2: 0.03 -> 0.05
        assert no.min_edge == 0.05              # 2026-07-17: aligned with execution_min_edge
                                                # (0.090 -> 0.04 on 07-16, -> 0.05 on 07-17)
        assert no.max_edge == 0.15
        assert no.capital_frac == 0.07
        assert sorted(no.entry_hour_set) == [0, 1, 2, 3, 4, 5, 6]
        assert no.consensus_skip_threshold is None
        assert no.max_vwap_slip_from_anchor is None

    def test_tail_sleeve_values(self):
        tail = STRATEGY_CONFIGS["TAIL"]
        assert tail.enabled is False                    # disabled 2026-07-17 (EV<=0 forensics)
        assert tail.alpha_ratio == 4.0                  # L2: 4.5 -> 4.0
        assert tail.fp_max == 0.03                       # L2: 0.05 -> 0.03
        assert tail.fp_min == 0.001
        assert tail.delayed_entry_fp_max == 0.02
        assert tail.execution_min_edge == 0.07          # L2: 0.03 -> 0.07
        assert tail.consensus_skip_threshold == 0.40    # L2: 0.50 -> 0.40
        assert sorted(tail.entry_hour_set) == [1]       # L2: {0} -> {1}
        assert tail.capital_frac == 0.05
        assert tail.tp == 0.20
        assert tail.max_vwap_slip_from_anchor is None

    def test_module_and_disabled_sleeves(self):
        assert STRATEGY_CONFIGS["YMID"].enabled is False
        assert STRATEGY_CONFIGS["YHIGH"].enabled is False
        assert STRATEGY_CONFIGS["YMID"].max_vwap_slip_from_anchor is None
        assert STRATEGY_CONFIGS["YHIGH"].max_vwap_slip_from_anchor is None
