"""Tests for Unit 5: Ledger Recording."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hightempbot.db.connection import get_connection, init_db
from hightempbot.persistence.ledger import (
    backfill_all_resolved_actuals,
    backfill_fee_adjusted_pnl,
    backfill_polymarket_resolution_labels,
    backfill_resolved_actual_for_station_date,
    expire_stuck_pending,
    poly_fee_charge,
    record_bet,
    record_position_close,
    record_resolution,
    record_signal,
    update_pending_bet_after_execution,
)
from hightempbot.execution.types import BetSignal, OrderResult


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    conn = get_connection(str(db_path))
    yield conn
    conn.close()


def _make_signal(**kwargs) -> BetSignal:
    defaults = dict(
        station_id="KDAL", target_date="2026-04-07", horizon=1,
        bracket_idx=3, threshold=68.0, bracket_label="66-68°F YES",
        side="YES", p_model=0.45, p_market=0.30, edge=0.15,
        bet_size_usd=30.0, fill_price=0.30, volume_usd=5000.0,
        market_id="m1", token_id="t1", passed_all_gates=True,
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


class TestRecordBet:
    def test_live_bet_inserts_all_columns(self, db: sqlite3.Connection):
        signal = _make_signal()
        order_result = OrderResult(
            order_id="ord_123", fill_price=0.30, fill_size=100.0, success=True,
        )
        record_bet(db, signal, order_result, dry_run=False)

        row = db.execute("SELECT * FROM ledger WHERE order_id = 'ord_123'").fetchone()
        assert row is not None
        assert row["station_id"] == "KDAL"
        assert row["side"] == "YES"
        assert row["event_type"] == "bet"
        assert row["outcome"] == "PENDING"
        assert row["order_id"] == "ord_123"

    def test_record_bet_persists_fill_levels_in_event_detail(self, db: sqlite3.Connection):
        signal = _make_signal()
        order_result = OrderResult(
            order_id="ord_levels",
            fill_price=0.125,
            fill_size=120.0,
            success=True,
            fill_levels=[
                {"price": 0.12, "usd": 10.0, "shares": 83.33333333},
                {"price": 0.13, "usd": 5.0, "shares": 38.46153846},
            ],
        )
        record_bet(db, signal, order_result, dry_run=False)

        row = db.execute("SELECT event_detail FROM ledger WHERE order_id = 'ord_levels'").fetchone()
        detail = json.loads(row["event_detail"])
        assert detail["fill_levels"] == [
            {"price": 0.12, "shares": 83.33333333, "usd": 10.0},
            {"price": 0.13, "shares": 38.46153846, "usd": 5.0},
        ]

    def test_update_pending_persists_fill_levels_in_event_detail(self, db: sqlite3.Connection):
        signal = _make_signal()
        row_id = record_bet(db, signal, None, dry_run=True)
        update_pending_bet_after_execution(
            db,
            row_id,
            dry_run=True,
            order_result=OrderResult(
                fill_price=0.125,
                fill_size=120.0,
                fill_ts="2026-05-08 00:05:10",
                transaction_hash="DRY_RUN_test",
                fill_levels=[
                    {"price": 0.12, "usd": 10.0, "shares": 83.33333333},
                    {"price": 0.13, "usd": 5.0, "shares": 38.46153846},
                ],
            ),
        )

        row = db.execute("SELECT event_detail FROM ledger WHERE id = ?", (row_id,)).fetchone()
        detail = json.loads(row["event_detail"])
        assert detail["fill_levels"] == [
            {"price": 0.12, "shares": 83.33333333, "usd": 10.0},
            {"price": 0.13, "shares": 38.46153846, "usd": 5.0},
        ]

    def test_limit_price_uses_walked_limit_not_fill_price(self, db: sqlite3.Connection):
        signal = _make_signal(fill_price=0.30, limit_price=0.34)
        order_result = OrderResult(
            order_id="ord_limit", fill_price=0.31, fill_size=96.8, limit_price=0.34, success=True,
        )
        record_bet(db, signal, order_result, dry_run=False)

        row = db.execute("SELECT limit_price, fill_price FROM ledger WHERE order_id = 'ord_limit'").fetchone()
        assert row["limit_price"] == 0.34
        assert row["fill_price"] == 0.31

    def test_dry_run_sets_event_type(self, db: sqlite3.Connection):
        signal = _make_signal()
        record_bet(db, signal, None, dry_run=True)

        row = db.execute("SELECT * FROM ledger LIMIT 1").fetchone()
        assert row["event_type"] == "dry_run"
        assert row["outcome"] == "PENDING"  # dry-run also uses PENDING for capital tracking
        assert row["order_id"] is None
        assert row["fill_price"] == signal.fill_price
        assert row["fill_size"] == pytest.approx(signal.bet_size_usd / signal.fill_price)
        assert row["fill_ts"] is not None

    def test_entry_top_price_persists_through_event_detail(self, db: sqlite3.Connection):
        """entry_top_price must round-trip through event_detail JSON.

        - A positive value persists.
        - A 0.0 value also persists (is-not-None check, not truthiness — a
          0.0 sentinel must never silently drop).
        - A None value must NOT write the key (legacy compatibility).
        """
        # Positive value.
        sig_pos = _make_signal(entry_top_price=0.7)
        rid_pos = record_bet(db, sig_pos, None, dry_run=True)
        row = db.execute(
            "SELECT json_extract(event_detail, '$.entry_top_price') AS v FROM ledger WHERE id = ?",
            (rid_pos,),
        ).fetchone()
        assert row["v"] == 0.7

        # 0.0 sentinel — must persist, not drop.
        sig_zero = _make_signal(entry_top_price=0.0)
        rid_zero = record_bet(db, sig_zero, None, dry_run=True)
        row = db.execute(
            "SELECT json_extract(event_detail, '$.entry_top_price') AS v FROM ledger WHERE id = ?",
            (rid_zero,),
        ).fetchone()
        assert row["v"] == 0.0

        # None — key absent from event_detail.
        sig_none = _make_signal(entry_top_price=None)
        rid_none = record_bet(db, sig_none, None, dry_run=True)
        row = db.execute(
            "SELECT event_detail FROM ledger WHERE id = ?", (rid_none,),
        ).fetchone()
        detail = json.loads(row["event_detail"]) if row["event_detail"] else {}
        assert "entry_top_price" not in detail


class TestExpireStuckPending:
    @staticmethod
    def _seed_old_pending_with_order(
        db: sqlite3.Connection,
        *,
        order_id: str = "ord_old",
    ) -> int:
        from datetime import datetime, timedelta, timezone

        row_id = record_bet(
            db,
            _make_signal(),
            OrderResult(
                order_id=order_id,
                fill_price=0.30,
                fill_size=100.0,
                success=True,
            ),
            dry_run=False,
        )
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        db.execute("UPDATE ledger SET bet_ts = ? WHERE id = ?", (old_ts, row_id))
        db.commit()
        return row_id

    def test_unknown_clob_order_state_leaves_pending(
        self,
        db: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ):
        row_id = self._seed_old_pending_with_order(db)
        monkeypatch.setattr(
            "hightempbot.persistence.reconciliation._bounded_get_order",
            lambda _order_client, _order_id: None,
        )

        expired = expire_stuck_pending(
            db,
            max_age_days=7,
            order_client=object(),
        )

        row = db.execute(
            "SELECT outcome, pnl FROM ledger WHERE id = ?",
            (row_id,),
        ).fetchone()
        health = db.execute(
            "SELECT status, message FROM pipeline_health "
            "WHERE stage = 'expire' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert expired == 0
        assert row["outcome"] == "PENDING"
        assert row["pnl"] is None
        assert health["status"] == "WARNING"
        assert "get_order unknown" in health["message"]

    def test_terminal_clob_order_state_expires(
        self,
        db: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ):
        row_id = self._seed_old_pending_with_order(db)
        monkeypatch.setattr(
            "hightempbot.persistence.reconciliation._bounded_get_order",
            lambda _order_client, _order_id: {"status": "CANCELLED"},
        )

        expired = expire_stuck_pending(
            db,
            max_age_days=7,
            order_client=object(),
        )

        row = db.execute(
            "SELECT outcome, pnl FROM ledger WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert expired == 1
        assert row["outcome"] == "EXPIRED"
        assert row["pnl"] == 0.0


class TestRecordSignal:
    def test_signal_inserted(self, db: sqlite3.Connection):
        signal = _make_signal()
        record_signal(db, signal, "2026-04-06T08:00:00Z", "BET")

        row = db.execute("SELECT * FROM signals LIMIT 1").fetchone()
        assert row is not None
        assert row["station_id"] == "KDAL"
        assert row["outcome"] == "BET"
        assert row["gate_bss"] == 1
        assert row["gate_edge"] == 1
        assert row["gate_fill_price"] == 1
        assert row["gate_daily_exposure"] == 1
        assert row["gate_idempotency"] == 1

    def test_skip_signal_with_gate_name(self, db: sqlite3.Connection):
        signal = _make_signal(
            passed_all_gates=False,
            gate_results={"lut": True, "edge_gate": False, "max_per_market": None},
        )
        record_signal(db, signal, "2026-04-06T08:00:00Z", "SKIP:edge")

        row = db.execute("SELECT * FROM signals LIMIT 1").fetchone()
        assert row["outcome"] == "SKIP:edge"
        assert row["gate_edge"] == 0
        assert row["gate_fill_price"] is None  # not evaluated

    def test_legacy_gate_names_still_populate_signal_columns(self, db: sqlite3.Connection):
        signal = _make_signal(
            gate_results={
                "bss": True,
                "edge": False,
                "fill_price": True,
                "daily_exposure": False,
            },
        )
        record_signal(db, signal, "2026-04-06T08:00:00Z", "SKIP:edge")

        row = db.execute("SELECT * FROM signals LIMIT 1").fetchone()
        assert row["gate_bss"] == 1
        assert row["gate_edge"] == 0
        assert row["gate_fill_price"] == 1
        assert row["gate_daily_exposure"] == 0

    def test_null_volume_accepted(self, db: sqlite3.Connection):
        signal = _make_signal(volume_usd=None)
        record_signal(db, signal, "2026-04-06T08:00:00Z", "SKIP:volume")

        row = db.execute("SELECT * FROM signals LIMIT 1").fetchone()
        assert row["volume_usd"] is None


class TestRecordResolution:
    def test_updates_outcome_and_pnl(self, db: sqlite3.Connection):
        signal = _make_signal()
        order_result = OrderResult(
            order_id="ord_res",
            fill_price=0.30,
            fill_size=signal.bet_size_usd / 0.30,
            success=True,
        )
        record_bet(db, signal, order_result, dry_run=False)

        row = db.execute("SELECT id FROM ledger LIMIT 1").fetchone()
        record_resolution(db, row["id"], actual_tmax=69.0, outcome="WIN", pnl=20.0)

        updated = db.execute("SELECT * FROM ledger WHERE id = ?", (row["id"],)).fetchone()
        expected_fee = poly_fee_charge(0.30, signal.bet_size_usd / 0.30)
        assert updated["outcome"] == "WIN"
        assert updated["pnl"] == pytest.approx(20.0 - expected_fee)
        assert updated["actual_tmax"] == 69.0
        detail = json.loads(updated["event_detail"])
        assert detail["poly_entry_fee"] == pytest.approx(expected_fee)
        assert detail["pnl_gross"] == pytest.approx(20.0)

    def test_does_not_overwrite_closed_position(self, db: sqlite3.Connection):
        signal = _make_signal()
        row_id = record_bet(db, signal, None, dry_run=True)
        close_size = signal.bet_size_usd / signal.fill_price
        record_position_close(
            db,
            row_id,
            close_price=0.45,
            close_size=close_size,
            reason="ymid_tp_dry",
        )
        before = db.execute(
            "SELECT outcome, pnl, event_detail FROM ledger WHERE id = ?",
            (row_id,),
        ).fetchone()

        record_resolution(db, row_id, actual_tmax=69.0, outcome="LOSS", pnl=-999.0)

        after = db.execute(
            "SELECT outcome, pnl, actual_tmax, event_detail FROM ledger WHERE id = ?",
            (row_id,),
        ).fetchone()
        detail = json.loads(after["event_detail"])
        assert after["outcome"] == "CLOSED"
        assert after["pnl"] == pytest.approx(before["pnl"])
        assert after["actual_tmax"] is None
        assert after["event_detail"] == before["event_detail"]
        assert detail["close_reason"] == "ymid_tp_dry"
        assert "resolution_source" not in detail

    def test_position_close_refuses_resolved_position(self, db: sqlite3.Connection):
        signal = _make_signal()
        row_id = record_bet(db, signal, None, dry_run=True)
        record_resolution(db, row_id, actual_tmax=69.0, outcome="WIN", pnl=20.0)
        before = db.execute(
            "SELECT outcome, pnl, event_detail FROM ledger WHERE id = ?",
            (row_id,),
        ).fetchone()

        with pytest.raises(ValueError, match="not PENDING"):
            record_position_close(
                db,
                row_id,
                close_price=0.45,
                close_size=signal.bet_size_usd / signal.fill_price,
            )

        after = db.execute(
            "SELECT outcome, pnl, event_detail FROM ledger WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert after["outcome"] == before["outcome"]
        assert after["pnl"] == pytest.approx(before["pnl"])
        assert after["event_detail"] == before["event_detail"]


class TestFeeAdjustedPnlBackfill:
    def test_historical_win_row_becomes_net_and_stamped(self, db: sqlite3.Connection):
        signal = _make_signal()
        order_result = OrderResult(
            order_id="hist_win",
            fill_price=0.30,
            fill_size=signal.bet_size_usd / 0.30,
            success=True,
        )
        row_id = record_bet(db, signal, order_result, dry_run=False)
        db.execute("UPDATE ledger SET outcome = 'WIN', pnl = 20.0 WHERE id = ?", (row_id,))
        db.commit()

        updated = backfill_fee_adjusted_pnl(db)

        row = db.execute("SELECT pnl, event_detail FROM ledger WHERE id = ?", (row_id,)).fetchone()
        detail = json.loads(row["event_detail"])
        expected_entry_fee = poly_fee_charge(0.30, signal.bet_size_usd / 0.30)
        assert updated == 1
        assert row["pnl"] == pytest.approx(20.0 - expected_entry_fee)
        assert detail["pnl_gross"] == pytest.approx(20.0)
        assert detail["poly_entry_fee"] == pytest.approx(expected_entry_fee)
        assert detail["poly_exit_fee"] == pytest.approx(0.0)
        assert detail["fee_adjusted_at"]

    def test_historical_closed_row_subtracts_entry_and_exit_fees(self, db: sqlite3.Connection):
        signal = _make_signal(side="NO", fill_price=0.80, bet_size_usd=10.0)
        held_size = 10.0 / 0.80
        order_result = OrderResult(
            order_id="hist_close",
            fill_price=0.80,
            fill_size=held_size,
            success=True,
        )
        row_id = record_bet(db, signal, order_result, dry_run=False)
        db.execute(
            "UPDATE ledger SET outcome = 'CLOSED', pnl = ?, event_detail = ? WHERE id = ?",
            (
                -5.0,
                json.dumps({"close_price": 0.40, "close_size": held_size}),
                row_id,
            ),
        )
        db.commit()

        updated = backfill_fee_adjusted_pnl(db)

        row = db.execute("SELECT pnl, event_detail FROM ledger WHERE id = ?", (row_id,)).fetchone()
        detail = json.loads(row["event_detail"])
        expected_entry_fee = poly_fee_charge(0.80, held_size)
        expected_exit_fee = poly_fee_charge(0.40, held_size)
        assert updated == 1
        assert row["pnl"] == pytest.approx(-5.0 - expected_entry_fee - expected_exit_fee)
        assert detail["pnl_gross"] == pytest.approx(-5.0)
        assert detail["poly_entry_fee"] == pytest.approx(expected_entry_fee)
        assert detail["poly_exit_fee"] == pytest.approx(expected_exit_fee)

    def test_fee_backfill_is_idempotent(self, db: sqlite3.Connection):
        signal = _make_signal()
        order_result = OrderResult(
            order_id="hist_once",
            fill_price=0.30,
            fill_size=signal.bet_size_usd / 0.30,
            success=True,
        )
        row_id = record_bet(db, signal, order_result, dry_run=False)
        db.execute("UPDATE ledger SET outcome = 'WIN', pnl = 20.0 WHERE id = ?", (row_id,))
        db.commit()

        first = backfill_fee_adjusted_pnl(db)
        pnl_after_first = db.execute("SELECT pnl FROM ledger WHERE id = ?", (row_id,)).fetchone()["pnl"]
        second = backfill_fee_adjusted_pnl(db)
        pnl_after_second = db.execute("SELECT pnl FROM ledger WHERE id = ?", (row_id,)).fetchone()["pnl"]

        assert first == 1
        assert second == 0
        assert pnl_after_second == pytest.approx(pnl_after_first)

    def test_fee_backfill_skips_already_adjusted_rows(self, db: sqlite3.Connection):
        signal = _make_signal()
        order_result = OrderResult(order_id="already_net", fill_price=0.30, fill_size=100.0, success=True)
        row_id = record_bet(db, signal, order_result, dry_run=False)
        db.execute(
            "UPDATE ledger SET outcome = 'WIN', pnl = ?, event_detail = ? WHERE id = ?",
            (
                19.0,
                json.dumps({"pnl_gross": 20.0, "poly_entry_fee": 1.0}),
                row_id,
            ),
        )
        db.commit()

        updated = backfill_fee_adjusted_pnl(db)

        row = db.execute("SELECT pnl, event_detail FROM ledger WHERE id = ?", (row_id,)).fetchone()
        detail = json.loads(row["event_detail"])
        assert updated == 0
        assert row["pnl"] == pytest.approx(19.0)
        assert detail["pnl_gross"] == pytest.approx(20.0)


class TestResolvedActualBackfill:
    def test_backfill_for_station_date_updates_matching_resolved_rows(self, db: sqlite3.Connection):
        signal = _make_signal(target_date="2026-04-07")
        record_bet(db, signal, None, dry_run=True)
        row_id = db.execute("SELECT id FROM ledger LIMIT 1").fetchone()["id"]
        record_resolution(db, row_id, actual_tmax=None, outcome="WIN", pnl=20.0)
        db.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
            ("KDAL", "2026-04-07", 21.5, "wu"),
        )
        db.commit()

        updated = backfill_resolved_actual_for_station_date(db, "KDAL", "2026-04-07")

        row = db.execute("SELECT actual_tmax FROM ledger WHERE id = ?", (row_id,)).fetchone()
        assert updated == 1
        assert row["actual_tmax"] == 21.5

    def test_backfill_for_station_date_skips_non_wu_actual(self, db: sqlite3.Connection):
        signal = _make_signal(target_date="2026-04-07")
        record_bet(db, signal, None, dry_run=True)
        row_id = db.execute("SELECT id FROM ledger LIMIT 1").fetchone()["id"]
        record_resolution(db, row_id, actual_tmax=None, outcome="WIN", pnl=20.0)
        db.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
            ("KDAL", "2026-04-07", 21.5, "ncei"),
        )
        db.commit()

        updated = backfill_resolved_actual_for_station_date(db, "KDAL", "2026-04-07")

        row = db.execute(
            "SELECT actual_tmax, event_detail FROM ledger WHERE id = ?",
            (row_id,),
        ).fetchone()
        detail = json.loads(row["event_detail"])
        assert updated == 0
        assert row["actual_tmax"] is None
        assert "wu_actual_label" not in detail

    def test_backfill_for_station_date_adds_wu_actual_metadata(self, db: sqlite3.Connection):
        signal = _make_signal(target_date="2026-04-07")
        record_bet(db, signal, None, dry_run=True)
        row_id = db.execute("SELECT id FROM ledger LIMIT 1").fetchone()["id"]
        record_resolution(db, row_id, actual_tmax=None, outcome="WIN", pnl=20.0)
        db.execute(
            """INSERT INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source, poly_slug)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("KDAL", "Dallas", 32.85, -96.85, "US/Central", "F", "wu", "wu", "dallas"),
        )
        db.execute(
            """INSERT INTO market_tokens
            (station_id, market_date, bracket_idx, token_id, no_token_id, market_id,
             bracket_label, bracket_low, bracket_high)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("KDAL", "2026-04-07", 0, "yes", "no", "m", "70-71\u00b0F", 69.5, 71.5),
        )
        db.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
            ("KDAL", "2026-04-07", 21.5, "wu"),
        )
        db.commit()

        updated = backfill_resolved_actual_for_station_date(db, "KDAL", "2026-04-07")

        row = db.execute("SELECT actual_tmax, event_detail FROM ledger WHERE id = ?", (row_id,)).fetchone()
        detail = json.loads(row["event_detail"])
        assert updated == 1
        assert row["actual_tmax"] == 21.5
        assert "resolution_actual_label" not in detail
        assert detail["wu_actual_label"] == "70-71\u00b0F"
        assert detail["wu_actual_source"] == "actuals"
        assert detail["wu_actual_display"] == 71.0
        assert detail["wu_actual_unit"] == "F"

    def test_backfill_keeps_polymarket_label_and_flags_wu_mismatch(self, db: sqlite3.Connection):
        signal = _make_signal(target_date="2026-04-07")
        record_bet(db, signal, None, dry_run=True)
        row_id = db.execute("SELECT id FROM ledger LIMIT 1").fetchone()["id"]
        record_resolution(
            db,
            row_id,
            actual_tmax=None,
            outcome="WIN",
            pnl=20.0,
            resolution_label="68-69\u00b0F",
            resolution_source="polymarket_winner",
            extra_detail={
                "resolution_bracket_low": 67.5,
                "resolution_bracket_high": 69.5,
            },
        )
        before = db.execute("SELECT outcome, pnl FROM ledger WHERE id = ?", (row_id,)).fetchone()
        db.execute(
            """INSERT INTO enrolled_stations
            (icao, city, lat, lon, timezone, unit, resolution_source, calibration_source, poly_slug)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("KDAL", "Dallas", 32.85, -96.85, "US/Central", "F", "wu", "wu", "dallas"),
        )
        db.execute(
            """INSERT INTO market_tokens
            (station_id, market_date, bracket_idx, token_id, no_token_id, market_id,
             bracket_label, bracket_low, bracket_high)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("KDAL", "2026-04-07", 0, "yes", "no", "m", "70-71\u00b0F", 69.5, 71.5),
        )
        db.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
            ("KDAL", "2026-04-07", 21.5, "wu"),
        )
        db.commit()

        updated = backfill_resolved_actual_for_station_date(db, "KDAL", "2026-04-07")

        row = db.execute(
            "SELECT outcome, pnl, actual_tmax, event_detail FROM ledger WHERE id = ?",
            (row_id,),
        ).fetchone()
        detail = json.loads(row["event_detail"])
        assert updated == 1
        assert row["outcome"] == before["outcome"]
        assert row["pnl"] == before["pnl"]
        assert row["actual_tmax"] == 21.5
        assert detail["resolution_actual_label"] == "68-69\u00b0F"
        assert detail["resolution_source"] == "polymarket_winner"
        assert detail["wu_actual_label"] == "70-71\u00b0F"
        assert detail["wu_actual_matches_polymarket"] is False

    def test_backfill_all_updates_only_missing_actuals(self, db: sqlite3.Connection):
        signal_a = _make_signal(target_date="2026-04-07")
        signal_b = _make_signal(target_date="2026-04-08", station_id="KLGA", market_id="m2", token_id="t2")
        record_bet(db, signal_a, None, dry_run=True)
        record_bet(db, signal_b, None, dry_run=True)
        rows = db.execute("SELECT id, station_id, target_date FROM ledger ORDER BY id").fetchall()
        record_resolution(db, rows[0]["id"], actual_tmax=None, outcome="WIN", pnl=10.0)
        record_resolution(db, rows[1]["id"], actual_tmax=18.0, outcome="LOSS", pnl=-5.0)
        db.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
            ("KDAL", "2026-04-07", 22.0, "wu"),
        )
        db.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
            ("KLGA", "2026-04-08", 19.0, "wu"),
        )
        db.commit()

        updated = backfill_all_resolved_actuals(db)
        again = backfill_all_resolved_actuals(db)

        results = db.execute("SELECT id, actual_tmax FROM ledger ORDER BY id").fetchall()
        assert updated == 1
        assert again == 0
        assert results[0]["actual_tmax"] == 22.0
        assert results[1]["actual_tmax"] == 18.0

    def test_backfill_all_skips_non_wu_actuals(self, db: sqlite3.Connection):
        signal = _make_signal(target_date="2026-04-07")
        record_bet(db, signal, None, dry_run=True)
        row_id = db.execute("SELECT id FROM ledger LIMIT 1").fetchone()["id"]
        record_resolution(db, row_id, actual_tmax=None, outcome="WIN", pnl=10.0)
        db.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) VALUES (?, ?, ?, ?)",
            ("KDAL", "2026-04-07", 22.0, "ncei"),
        )
        db.commit()

        updated = backfill_all_resolved_actuals(db)

        row = db.execute(
            "SELECT actual_tmax, event_detail FROM ledger WHERE id = ?",
            (row_id,),
        ).fetchone()
        detail = json.loads(row["event_detail"])
        assert updated == 0
        assert row["actual_tmax"] is None
        assert "wu_actual_label" not in detail


class TestPolymarketResolutionLabelBackfill:
    """Backfill of ``event_detail.resolution_actual_label`` for resolved bets."""

    @staticmethod
    def _recent_date(days_ago: int = 1) -> str:
        from datetime import date, timedelta
        return (date.today() - timedelta(days=days_ago)).isoformat()

    @staticmethod
    def _winning_bracket(
        *,
        bracket_low: float | None = 70.0,
        bracket_high: float | None = 71.0,
        bracket_label: str | None = "70-71°F",
        yes_price: float = 0.995,
    ) -> dict | None:
        if bracket_low is None and bracket_high is None:
            return None
        return {
            "yes_price": yes_price,
            "closed": True,
            "token_id": "t_win",
            "bracket_low": bracket_low,
            "bracket_high": bracket_high,
            "bracket_label": bracket_label,
        }

    def _patch_gamma(
        self,
        monkeypatch: pytest.MonkeyPatch,
        return_value: dict | None,
        *,
        call_counter: dict | None = None,
    ) -> None:
        from hightempbot.resolution import gamma
        # Patch in the ledger module's namespace because ledger imported
        # the symbol at module load time.
        from hightempbot.persistence import ledger as ledger_mod

        def _stub(sid, td):
            if call_counter is not None:
                call_counter["n"] = call_counter.get("n", 0) + 1
            return return_value

        monkeypatch.setattr(gamma, "winning_bracket_from_gamma", _stub)
        monkeypatch.setattr(ledger_mod, "winning_bracket_from_gamma", _stub)

    def _seed_resolved_unlabeled(
        self,
        db: sqlite3.Connection,
        *,
        station_id: str = "KDAL",
        target_date: str | None = None,
        resolution_source: str = "polymarket_terminal_yes",
        outcome: str = "WIN",
        side: str = "NO",
    ) -> int:
        td = target_date or self._recent_date()
        signal = _make_signal(station_id=station_id, target_date=td, side=side)
        row_id = record_bet(db, signal, None, dry_run=True)
        record_resolution(
            db, row_id, actual_tmax=None, outcome=outcome, pnl=10.0,
            resolution_source=resolution_source,
        )
        return row_id

    def test_backfills_label_when_polymarket_event_finalised(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    ):
        row_id = self._seed_resolved_unlabeled(db)
        self._patch_gamma(monkeypatch, self._winning_bracket())

        updated = backfill_polymarket_resolution_labels(db)

        assert updated == 1
        detail = json.loads(
            db.execute("SELECT event_detail FROM ledger WHERE id = ?", (row_id,)).fetchone()["event_detail"]
        )
        assert detail["resolution_actual_label"] == "70-71°F"
        assert detail["resolution_bracket_low"] == 70.0
        assert detail["resolution_bracket_high"] == 71.0
        assert "resolution_label_backfilled_at" in detail

    def test_replaces_old_data_api_title_label_when_event_finalised(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    ):
        signal = _make_signal(target_date=self._recent_date())
        row_id = record_bet(db, signal, None, dry_run=True)
        record_resolution(
            db,
            row_id,
            actual_tmax=None,
            outcome="WIN",
            pnl=10.0,
            resolution_label="Will the highest temperature in Dallas be 75F or below?",
            resolution_source="polymarket_data_api_redeemable",
        )
        self._patch_gamma(monkeypatch, self._winning_bracket(bracket_label="70-71F"))

        updated = backfill_polymarket_resolution_labels(db)

        assert updated == 1
        detail = json.loads(
            db.execute("SELECT event_detail FROM ledger WHERE id = ?", (row_id,)).fetchone()["event_detail"]
        )
        assert detail["resolution_actual_label"] == "70-71F"
        assert detail["resolution_source"] == "polymarket_data_api_redeemable"
        assert "resolution_label_backfilled_at" in detail

    def test_skips_already_labeled_rows(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    ):
        signal = _make_signal(target_date=self._recent_date())
        row_id = record_bet(db, signal, None, dry_run=True)
        record_resolution(
            db, row_id, actual_tmax=None, outcome="WIN", pnl=10.0,
            resolution_label="68-69°F", resolution_source="polymarket_winner",
        )
        calls: dict = {}
        self._patch_gamma(monkeypatch, self._winning_bracket(), call_counter=calls)

        updated = backfill_polymarket_resolution_labels(db)

        assert updated == 0
        assert calls.get("n", 0) == 0

    def test_skips_pending_rows(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    ):
        signal = _make_signal(target_date=self._recent_date())
        record_bet(db, signal, None, dry_run=True)
        self._patch_gamma(monkeypatch, self._winning_bracket())

        updated = backfill_polymarket_resolution_labels(db)

        assert updated == 0

    def test_skips_rows_outside_lookback_window(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    ):
        # Row dated > 14 days ago should be excluded by the lookback floor.
        from datetime import date, timedelta
        old_date = (date.today() - timedelta(days=30)).isoformat()
        self._seed_resolved_unlabeled(db, target_date=old_date)
        calls: dict = {}
        self._patch_gamma(monkeypatch, self._winning_bracket(), call_counter=calls)

        updated = backfill_polymarket_resolution_labels(db)

        assert updated == 0
        # Crucially, no Gamma call fired — the SELECT pruned the row entirely.
        assert calls.get("n", 0) == 0

    def test_skips_when_gamma_returns_none(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    ):
        self._seed_resolved_unlabeled(db)
        self._patch_gamma(monkeypatch, None)

        updated = backfill_polymarket_resolution_labels(db)
        assert updated == 0

    def test_skips_when_winning_bracket_has_no_bounds(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    ):
        # Defensive: even if upstream returns a "winner" with None bounds
        # (shouldn't happen because winning_bracket_from_gamma now guards it),
        # the backfill should not write a placeholder label.
        row_id = self._seed_resolved_unlabeled(db)
        self._patch_gamma(monkeypatch, self._winning_bracket(
            bracket_low=None, bracket_high=None, bracket_label=None,
        ))

        updated = backfill_polymarket_resolution_labels(db)

        assert updated == 0
        detail = json.loads(
            db.execute("SELECT event_detail FROM ledger WHERE id = ?", (row_id,)).fetchone()["event_detail"]
        )
        assert "resolution_actual_label" not in detail

    def test_groups_by_station_date_single_gamma_call(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    ):
        td = self._recent_date()
        self._seed_resolved_unlabeled(db, target_date=td, side="NO")
        signal2 = _make_signal(target_date=td, side="YES", token_id="t_other")
        row2 = record_bet(db, signal2, None, dry_run=True)
        record_resolution(
            db, row2, actual_tmax=None, outcome="LOSS", pnl=-10.0,
            resolution_source="polymarket_terminal_token",
        )
        calls: dict = {}
        self._patch_gamma(monkeypatch, self._winning_bracket(), call_counter=calls)

        updated = backfill_polymarket_resolution_labels(db)

        assert updated == 2
        assert calls["n"] == 1

    def test_filters_by_station_id(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    ):
        kdal_id = self._seed_resolved_unlabeled(db, station_id="KDAL")
        klga_id = self._seed_resolved_unlabeled(
            db, station_id="KLGA", target_date=self._recent_date(2),
        )
        self._patch_gamma(monkeypatch, self._winning_bracket())

        updated = backfill_polymarket_resolution_labels(db, station_id="KDAL")

        assert updated == 1
        kdal_detail = json.loads(
            db.execute("SELECT event_detail FROM ledger WHERE id = ?", (kdal_id,)).fetchone()["event_detail"]
        )
        klga_detail = json.loads(
            db.execute("SELECT event_detail FROM ledger WHERE id = ?", (klga_id,)).fetchone()["event_detail"]
        )
        assert kdal_detail.get("resolution_actual_label") == "70-71°F"
        assert klga_detail.get("resolution_actual_label") in (None, "")

    def test_idempotent_second_run_returns_zero(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    ):
        self._seed_resolved_unlabeled(db)
        self._patch_gamma(monkeypatch, self._winning_bracket())

        first = backfill_polymarket_resolution_labels(db)
        second = backfill_polymarket_resolution_labels(db)

        assert first == 1
        assert second == 0

    def test_does_not_touch_actual_tmax(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    ):
        row_id = self._seed_resolved_unlabeled(db)
        self._patch_gamma(monkeypatch, self._winning_bracket())

        backfill_polymarket_resolution_labels(db)

        actual = db.execute(
            "SELECT actual_tmax FROM ledger WHERE id = ?", (row_id,),
        ).fetchone()["actual_tmax"]
        assert actual is None

    def test_preserves_outcome_and_pnl(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    ):
        # Display-only invariant: backfill must not change settlement state.
        row_id = self._seed_resolved_unlabeled(db, outcome="WIN")
        before = db.execute(
            "SELECT outcome, pnl, event_detail FROM ledger WHERE id = ?",
            (row_id,),
        ).fetchone()
        before_source = json.loads(before["event_detail"])["resolution_source"]
        self._patch_gamma(monkeypatch, self._winning_bracket())

        backfill_polymarket_resolution_labels(db)

        after = db.execute(
            "SELECT outcome, pnl, event_detail FROM ledger WHERE id = ?", (row_id,),
        ).fetchone()
        after_detail = json.loads(after["event_detail"])
        assert after["outcome"] == before["outcome"]
        assert after["pnl"] == before["pnl"]
        assert after_detail["resolution_source"] == before_source

    def test_logs_pipeline_health_on_success(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    ):
        self._seed_resolved_unlabeled(db)
        self._patch_gamma(monkeypatch, self._winning_bracket())

        backfill_polymarket_resolution_labels(db)

        health = db.execute(
            "SELECT status, message FROM pipeline_health"
            " WHERE stage = 'resolution_backfill' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert health is not None
        assert health["status"] == "OK"
        assert "70-71°F" in health["message"]
