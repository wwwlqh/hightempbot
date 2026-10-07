"""TP/SL monitor: fire/skip/idempotency/age-out/None-handling."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from hightempbot.execution.strategy_constants import (
    SCAN_INTERVAL_MINUTES,
    TP_SL_FLAG_STALE_SECONDS,
    icao_tick_offset,
)
from hightempbot.execution.tp_sl_monitor import (
    _evaluate_row,
    _flag_started_at,
    _parse_close_in_flight,
    run_tp_sl_monitor,
)


# ---------------------------------------------------------------- pure helpers

def test_evaluate_row_tp_fires() -> None:
    # entry 0.30, bid 0.46 → move=+0.16 ≥ tp=0.15
    assert _evaluate_row(fill_price=0.30, bid=0.46, tp=0.15, sl=0.10) == "tp"


def test_evaluate_row_sl_fires() -> None:
    # entry 0.30, bid 0.19 → move=-0.11 ≤ -sl=-0.10
    assert _evaluate_row(fill_price=0.30, bid=0.19, tp=0.15, sl=0.10) == "sl"


def test_evaluate_row_no_fire_within_band() -> None:
    assert _evaluate_row(fill_price=0.30, bid=0.32, tp=0.15, sl=0.10) is None
    assert _evaluate_row(fill_price=0.30, bid=0.21, tp=0.15, sl=0.10) is None


def test_parse_close_in_flight() -> None:
    ed = json.dumps({"strategy": "YMID", "close_in_flight": {"started_at": "2026-05-06 10:00:00"}})
    cif = _parse_close_in_flight(ed)
    assert cif == {"started_at": "2026-05-06 10:00:00"}
    started = _flag_started_at(cif)
    assert started == datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)


def test_parse_close_in_flight_handles_missing() -> None:
    assert _parse_close_in_flight(None) is None
    assert _parse_close_in_flight("not-json") is None
    assert _parse_close_in_flight(json.dumps({"strategy": "YMID"})) is None


# ---------------------------------------------------------------- fixtures + fakes

@dataclass
class FakeStation:
    icao: str = "KJFK"
    timezone: str = "America/New_York"


class FakePriceSource:
    """Stand-in for ClobReader/OrderClient that returns canned prices."""

    def __init__(self, books: dict[str, dict | None] = None, bids: dict[str, tuple[float, float] | None] = None) -> None:
        self.books = books or {}
        self.bids = bids or {}
        self.close_calls: list[tuple] = []
        self.close_kwargs: list[dict] = []
        self._close_result = None

    def fetch_order_book(self, token_id: str):
        return self.books.get(token_id)

    def best_bid(self, book):
        # Look up by token via book identity — book is the canned dict from `books`.
        for tok, b in self.books.items():
            if b is book:
                return self.bids.get(tok)
        return None

    def set_close_result(self, result):
        self._close_result = result

    def close_position(self, token_id, *, target_size, **kwargs):
        self.close_calls.append((token_id, target_size))
        self.close_kwargs.append(kwargs)
        return self._close_result


@pytest.fixture
def db(tmp_path: Path) -> str:
    """Build a small SQLite DB with the ledger schema and return the file path."""
    path = tmp_path / "test_monitor.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # Minimal ledger schema sufficient for the monitor's reads/writes.
    conn.execute(
        """CREATE TABLE ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bet_ts TEXT,
            station_id TEXT,
            market_id TEXT,
            token_id TEXT,
            target_date TEXT,
            horizon INTEGER,
            threshold REAL,
            side TEXT,
            p_model REAL,
            p_market REAL,
            edge REAL,
            kelly_size REAL,
            volume_cap REAL,
            bet_size REAL,
            limit_price REAL,
            order_id TEXT,
            fill_price REAL,
            fill_size REAL,
            fill_ts TEXT,
            outcome TEXT,
            pnl REAL,
            kelly_multiplier REAL,
            event_type TEXT,
            event_detail TEXT,
            actual_tmax REAL,
            prob_safe_floor REAL,
            pred_bucket_low REAL,
            pred_bucket_high REAL,
            n_bucket INTEGER
        )"""
    )
    # ce-code-review P1 #25: operator_control_state is now schema-managed; the
    # monitor reads it via processing_block_reason. Seed the LIVE singleton row
    # so the operator-control gate doesn't trip in tests that don't care about
    # operator state. pipeline_health stays an inline create here for the same
    # reason — minimal schema, no dependency on full init_db.
    conn.execute(
        """CREATE TABLE operator_control_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            state TEXT NOT NULL DEFAULT 'LIVE',
            boot_dry_run INTEGER NOT NULL DEFAULT 1,
            reason TEXT,
            updated_by TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            version INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.execute(
        "INSERT INTO operator_control_state (id, state, boot_dry_run, reason, updated_by, version) "
        "VALUES (1, 'LIVE', 0, 'test fixture', 'test', 0)"
    )
    conn.execute(
        """CREATE TABLE operator_control_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            action TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            actor TEXT,
            reason TEXT,
            detail TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return str(path)


def _insert_pending_ymid(
    db_path: str,
    *,
    station_id: str = "KJFK",
    token_id: str = "TOK_YES",
    fill_price: float = 0.30,
    fill_size: float = 100.0,
    bet_size: float = 30.0,
    target_date: str = "2026-05-10",
    event_type: str = "dry_run",
    extra_detail: dict | None = None,
) -> int:
    """Insert a PENDING YMID ledger row."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    detail = {"strategy": "YMID", "bracket_low": 20.0, "bracket_high": 25.0}
    if extra_detail:
        detail.update(extra_detail)
    cur = conn.execute(
        """INSERT INTO ledger
           (bet_ts, station_id, token_id, target_date, threshold, side,
            fill_price, fill_size, bet_size, outcome, event_type, event_detail)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "2026-05-06 10:00:00", station_id, token_id, target_date,
            22.5, "YES", fill_price, fill_size, bet_size,
            "PENDING", event_type, json.dumps(detail),
        ),
    )
    bet_id = cur.lastrowid
    conn.commit()
    conn.close()
    return bet_id


# ---------------------------------------------------------------- monitor end-to-end

def test_monitor_fires_tp_dry_run_records_close(db: str) -> None:
    bet_id = _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0)
    # Bid moves to 0.46 → +0.16 → TP fires (0.15 threshold).
    src = FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.46", "size": "1000"}], "asks": []}},
        bids={"TOK_YES": (0.46, 1000.0)},
    )
    counters = run_tp_sl_monitor(
        FakeStation(), db, reader=src, dry_run=True,
    )
    assert counters["tp_fired"] == 1
    assert counters["sl_fired"] == 0

    # Verify the row was closed in dry-run with the simulated price.
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT outcome, event_detail FROM ledger WHERE id = ?", (bet_id,)).fetchone()
    conn.close()
    assert row["outcome"] == "CLOSED"
    detail = json.loads(row["event_detail"])
    assert detail["close_reason"] == "ymid_tp_dry"
    assert detail["close_dry_run"] is True
    assert detail["close_price"] == 0.46


def test_monitor_uses_full_size_vwap_not_top_bid_to_trigger(db: str) -> None:
    bet_id = _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0)
    src = FakePriceSource(
        books={
            "TOK_YES": {
                "bids": [
                    {"price": "0.44", "size": "90"},
                    {"price": "0.46", "size": "10"},
                ],
                "asks": [],
            }
        },
        bids={"TOK_YES": (0.46, 10.0)},
    )

    counters = run_tp_sl_monitor(FakeStation(), db, reader=src, dry_run=True)

    assert counters["tp_fired"] == 0
    assert counters["skipped"] == 1
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT outcome, event_detail FROM ledger WHERE id = ?", (bet_id,)).fetchone()
    conn.close()
    assert row["outcome"] == "PENDING"
    assert "close_in_flight" not in (row["event_detail"] or "")


def test_monitor_dry_run_records_full_size_vwap_when_triggered(db: str) -> None:
    bet_id = _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0)
    src = FakePriceSource(
        books={
            "TOK_YES": {
                "bids": [
                    {"price": "0.45", "size": "50"},
                    {"price": "0.46", "size": "50"},
                ],
                "asks": [],
            }
        },
        bids={"TOK_YES": (0.46, 50.0)},
    )

    counters = run_tp_sl_monitor(FakeStation(), db, reader=src, dry_run=True)

    assert counters["tp_fired"] == 1
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT outcome, event_detail FROM ledger WHERE id = ?", (bet_id,)).fetchone()
    conn.close()
    assert row["outcome"] == "CLOSED"
    detail = json.loads(row["event_detail"])
    assert detail["close_price"] == pytest.approx(0.455)
    assert detail["trigger_move"] == pytest.approx(0.155)
    assert detail["top_bid_price"] == pytest.approx(0.46)
    assert detail["close_limit_price"] == pytest.approx(0.45)


def test_monitor_ignores_sl_when_disabled(db: str) -> None:
    _insert_pending_ymid(db, fill_price=0.30)
    src = FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.19", "size": "1000"}]}},
        bids={"TOK_YES": (0.19, 1000.0)},
    )
    counters = run_tp_sl_monitor(FakeStation(), db, reader=src, dry_run=True)
    assert counters["sl_fired"] == 0
    assert counters["skipped"] == 1


def test_monitor_no_fire_within_band(db: str) -> None:
    _insert_pending_ymid(db, fill_price=0.30)
    src = FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.32", "size": "1000"}]}},
        bids={"TOK_YES": (0.32, 1000.0)},
    )
    counters = run_tp_sl_monitor(FakeStation(), db, reader=src, dry_run=True)
    assert counters["tp_fired"] == 0
    assert counters["sl_fired"] == 0
    assert counters["skipped"] == 1


def test_monitor_operator_stop_blocks_before_book_fetch(db: str) -> None:
    from hightempbot.execution.operator_control import stop_processing

    _insert_pending_ymid(db, fill_price=0.30)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    stop_processing(conn, boot_dry_run=True)
    conn.close()
    src = FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.46", "size": "1000"}]}},
        bids={"TOK_YES": (0.46, 1000.0)},
    )

    counters = run_tp_sl_monitor(FakeStation(), db, reader=src, dry_run=True)

    assert counters["skipped"] == 1
    assert src.close_calls == []


# F-007: empty book / None book → skip, no flag set
def test_monitor_skip_on_empty_book(db: str) -> None:
    bet_id = _insert_pending_ymid(db)
    src = FakePriceSource(books={"TOK_YES": None}, bids={"TOK_YES": None})
    counters = run_tp_sl_monitor(FakeStation(), db, reader=src, dry_run=True)
    assert counters["tp_fired"] == 0
    assert counters["sl_fired"] == 0
    assert counters["skipped"] == 1
    # Row is still PENDING and has no in-flight flag.
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT outcome, event_detail FROM ledger WHERE id = ?", (bet_id,)).fetchone()
    conn.close()
    assert row["outcome"] == "PENDING"
    assert "close_in_flight" not in (row["event_detail"] or "")


def test_monitor_skip_on_none_best_bid(db: str) -> None:
    _insert_pending_ymid(db)
    src = FakePriceSource(
        books={"TOK_YES": {"bids": []}},
        bids={"TOK_YES": None},
    )
    counters = run_tp_sl_monitor(FakeStation(), db, reader=src, dry_run=True)
    assert counters["skipped"] == 1


# F-001 (a): stale flag age-out — older than TP_SL_FLAG_STALE_SECONDS gets cleared and reattempted.
def test_monitor_clears_stale_flag_and_reattempts(db: str) -> None:
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=TP_SL_FLAG_STALE_SECONDS + 60))
    bet_id = _insert_pending_ymid(
        db,
        extra_detail={"close_in_flight": {"started_at": stale_ts.strftime("%Y-%m-%d %H:%M:%S")}},
    )
    src = FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.46", "size": "1000"}]}},
        bids={"TOK_YES": (0.46, 1000.0)},
    )
    counters = run_tp_sl_monitor(FakeStation(), db, reader=src, dry_run=True)
    # Stale flag was cleared this tick; the row will reattempt next tick.
    # (Single-tick behavior: clear, then skip evaluation in same tick.)
    assert counters["stale_cleared"] == 1


# F-001 (a): fresh flag is respected — concurrent tick skips.
def test_monitor_skips_when_flag_fresh(db: str) -> None:
    fresh_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    bet_id = _insert_pending_ymid(
        db,
        extra_detail={"close_in_flight": {"started_at": fresh_ts}},
    )
    src = FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.46", "size": "1000"}]}},
        bids={"TOK_YES": (0.46, 1000.0)},
    )
    counters = run_tp_sl_monitor(FakeStation(), db, reader=src, dry_run=True)
    assert counters["tp_fired"] == 0
    assert counters["skipped"] == 1


def test_monitor_does_not_touch_non_ymid_rows(db: str) -> None:
    # Insert a PENDING NO bet at the same parameters; monitor must ignore it.
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO ledger
           (station_id, token_id, target_date, threshold, side, fill_price, fill_size,
            bet_size, outcome, event_type, event_detail)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("KJFK", "TOK_NO", "2026-05-10", 22.5, "NO", 0.85, 100.0, 85.0,
         "PENDING", "bet", json.dumps({"strategy": "NO"})),
    )
    conn.commit()
    conn.close()
    src = FakePriceSource(
        books={"TOK_NO": {"bids": [{"price": "0.95", "size": "1000"}]}},
        bids={"TOK_NO": (0.95, 1000.0)},
    )
    counters = run_tp_sl_monitor(FakeStation(), db, reader=src, dry_run=True)
    # Nothing fired because no YMID row exists.
    assert counters["tp_fired"] == 0
    assert counters["sl_fired"] == 0


def test_monitor_tail_tp_dry_run_records_close(db: str) -> None:
    """TAIL TP path: entry 0.03 → bid 0.24 → +0.21 ≥ tp=0.20 fires."""
    conn = sqlite3.connect(db)
    cur = conn.execute(
        """INSERT INTO ledger
           (bet_ts, station_id, token_id, target_date, threshold, side,
            fill_price, fill_size, bet_size, outcome, event_type, event_detail)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "2026-05-06 10:00:00", "KJFK", "TOK_YES_TAIL", "2026-05-10",
            22.5, "YES", 0.03, 100.0, 3.0,
            "PENDING", "dry_run", json.dumps({"strategy": "TAIL"}),
        ),
    )
    bet_id = cur.lastrowid
    conn.commit()
    conn.close()

    src = FakePriceSource(
        books={"TOK_YES_TAIL": {"bids": [{"price": "0.24", "size": "1000"}], "asks": []}},
        bids={"TOK_YES_TAIL": (0.24, 1000.0)},
    )
    counters = run_tp_sl_monitor(
        FakeStation(), db, strategy="TAIL", reader=src, dry_run=True,
    )
    assert counters["tp_fired"] == 1

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT outcome, event_detail FROM ledger WHERE id = ?", (bet_id,)).fetchone()
    conn.close()
    assert row["outcome"] == "CLOSED"
    detail = json.loads(row["event_detail"])
    assert detail["close_reason"] == "tail_tp_dry"
    assert detail["close_price"] == 0.24


def test_monitor_unknown_strategy_is_noop(db: str) -> None:
    """Unknown strategy key returns zeroed counters without error."""
    counters = run_tp_sl_monitor(
        FakeStation(), db, strategy="NOT_A_STRATEGY", dry_run=True,
    )
    assert counters == {
        "tp_fired": 0, "sl_fired": 0, "skipped": 0,
        "stale_cleared": 0, "errors": 0,
    }


# ---------------------------------------------------------------- live-path tests
# ce-review testing finding #15: every previous test ran in dry_run=True. The
# live close_position branch + record_position_close success/failure paths +
# orphan-close fallback were entirely uncovered.


@dataclass
class _OrderResult:
    success: bool
    fill_price: float | None = None
    fill_size: float | None = None
    order_id: str | None = None
    transaction_hash: str | None = None
    bet_size_usd: float | None = None
    error: str | None = None
    error_kind: str | None = None


def test_monitor_live_tp_records_close(db: str) -> None:
    """Live TP path: close_position succeeds + ledger records CLOSED."""
    bet_id = _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0, event_type="bet")
    src = FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.46", "size": "1000"}]}},
        bids={"TOK_YES": (0.46, 1000.0)},
    )
    src.set_close_result(_OrderResult(
        success=True, fill_price=0.46, fill_size=100.0,
        order_id="ORD-LIVE-1", transaction_hash="0xLIVE",
    ))
    counters = run_tp_sl_monitor(
        FakeStation(), db, order_client=src, dry_run=False,
    )
    assert counters["tp_fired"] == 1
    assert counters["errors"] == 0
    assert src.close_calls == [("TOK_YES", 100.0)]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT outcome, event_detail FROM ledger WHERE id = ?", (bet_id,)).fetchone()
    conn.close()
    assert row["outcome"] == "CLOSED"
    detail = json.loads(row["event_detail"])
    assert detail.get("close_reason") == "ymid_tp"
    # close_in_flight must be cleared on success (ce-review #22).
    assert "close_in_flight" not in detail


def test_monitor_live_close_passes_vwap_trigger_floor(db: str) -> None:
    _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0, event_type="bet")
    src = FakePriceSource(
        books={
            "TOK_YES": {
                "bids": [
                    {"price": "0.45", "size": "50"},
                    {"price": "0.46", "size": "50"},
                ]
            }
        },
        bids={"TOK_YES": (0.46, 50.0)},
    )
    src.set_close_result(_OrderResult(
        success=True, fill_price=0.455, fill_size=100.0,
        order_id="ORD-LIVE-VWAP", transaction_hash="0xLIVEVWAP",
    ))

    counters = run_tp_sl_monitor(
        FakeStation(), db, order_client=src, dry_run=False,
    )

    assert counters["tp_fired"] == 1
    assert src.close_calls == [("TOK_YES", 100.0)]
    assert len(src.close_kwargs) == 1
    assert src.close_kwargs[0]["min_acceptable_vwap"] == pytest.approx(0.45)
    assert src.close_kwargs[0]["max_acceptable_vwap"] is None


def test_monitor_live_stale_quote_veto_skips_without_error(db: str) -> None:
    bet_id = _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0, event_type="bet")
    src = FakePriceSource(
        books={
            "TOK_YES": {
                "bids": [
                    {"price": "0.45", "size": "50"},
                    {"price": "0.46", "size": "50"},
                ]
            }
        },
        bids={"TOK_YES": (0.46, 50.0)},
    )
    src.set_close_result(_OrderResult(
        success=False,
        error="close vwap below minimum acceptable trigger",
        error_kind="stale_quote",
    ))

    counters = run_tp_sl_monitor(
        FakeStation(), db, order_client=src, dry_run=False,
    )

    assert counters["tp_fired"] == 0
    assert counters["skipped"] == 1
    assert counters["errors"] == 0
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT outcome, event_detail FROM ledger WHERE id = ?", (bet_id,)).fetchone()
    conn.close()
    assert row["outcome"] == "PENDING"
    assert "close_in_flight" not in (row["event_detail"] or "")


def test_monitor_live_close_failure_clears_flag_for_retry(db: str) -> None:
    """Live: OrderResult(success=False) clears flag and increments errors."""
    bet_id = _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0, event_type="bet")
    src = FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.46", "size": "1000"}]}},
        bids={"TOK_YES": (0.46, 1000.0)},
    )
    src.set_close_result(_OrderResult(success=False, error="insufficient bid depth"))
    counters = run_tp_sl_monitor(
        FakeStation(), db, order_client=src, dry_run=False,
    )
    assert counters["tp_fired"] == 0
    assert counters["errors"] == 1
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT outcome, event_detail FROM ledger WHERE id = ?", (bet_id,)).fetchone()
    conn.close()
    assert row["outcome"] == "PENDING"  # not closed
    # Flag cleared so the next tick can re-attempt.
    assert "close_in_flight" not in (row["event_detail"] or "")


def test_monitor_live_close_timeout_keeps_in_flight(db: str) -> None:
    """Ambiguous submit failures keep the flag so the next tick cannot double-sell."""
    bet_id = _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0, event_type="bet")
    src = FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.46", "size": "1000"}]}},
        bids={"TOK_YES": (0.46, 1000.0)},
    )
    src.set_close_result(_OrderResult(
        success=False,
        error="post_order timed out",
        error_kind="network",
    ))

    counters = run_tp_sl_monitor(
        FakeStation(), db, order_client=src, dry_run=False,
    )

    assert counters["tp_fired"] == 0
    assert counters["errors"] == 1
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT outcome, event_detail FROM ledger WHERE id = ?", (bet_id,)).fetchone()
    conn.close()
    assert row["outcome"] == "PENDING"
    assert "close_in_flight" in (row["event_detail"] or "")


def test_monitor_live_ambiguous_success_keeps_in_flight(db: str) -> None:
    """Success without fill details or chain proof is not enough to close locally."""
    bet_id = _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0, event_type="bet")
    src = FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.46", "size": "1000"}]}},
        bids={"TOK_YES": (0.46, 1000.0)},
    )
    src.set_close_result(_OrderResult(success=True, order_id="ORD-AMBIG"))

    counters = run_tp_sl_monitor(
        FakeStation(), db, order_client=src, dry_run=False,
    )

    assert counters["tp_fired"] == 0
    assert counters["errors"] == 1
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT outcome, event_detail FROM ledger WHERE id = ?", (bet_id,)).fetchone()
    conn.close()
    assert row["outcome"] == "PENDING"
    assert "close_in_flight" in (row["event_detail"] or "")


def test_monitor_live_explicit_safe_response_can_close_without_fill_fields(db: str) -> None:
    """A chain-proven FOK close may use the trigger VWAP when fill fields are absent."""
    bet_id = _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0, event_type="bet")
    src = FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.46", "size": "1000"}]}},
        bids={"TOK_YES": (0.46, 1000.0)},
    )
    src.set_close_result(_OrderResult(
        success=True,
        order_id="ORD-SAFE",
        transaction_hash="0xSAFE",
    ))

    counters = run_tp_sl_monitor(
        FakeStation(), db, order_client=src, dry_run=False,
    )

    assert counters["tp_fired"] == 1
    assert counters["errors"] == 0
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT outcome, event_detail FROM ledger WHERE id = ?", (bet_id,)).fetchone()
    conn.close()
    assert row["outcome"] == "CLOSED"
    detail = json.loads(row["event_detail"])
    assert detail["close_price"] == pytest.approx(0.46)
    assert detail["close_size"] == pytest.approx(100.0)
    assert "close_in_flight" not in detail


def test_monitor_live_orphan_close_marker_when_ledger_write_fails(db: str) -> None:
    """Live: close_position succeeds but record_position_close raises 3x."""
    bet_id = _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0, event_type="bet")
    src = FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.46", "size": "1000"}]}},
        bids={"TOK_YES": (0.46, 1000.0)},
    )
    src.set_close_result(_OrderResult(
        success=True, fill_price=0.46, fill_size=100.0,
        order_id="ORD-ORPHAN", transaction_hash="0xOR",
    ))
    notifications: list[tuple[str, str]] = []

    def _capture(title: str, body: str) -> None:
        notifications.append((title, body))

    with patch(
        "hightempbot.execution.tp_sl_monitor.record_position_close",
        side_effect=RuntimeError("simulated DB write failure"),
    ):
        counters = run_tp_sl_monitor(
            FakeStation(), db, order_client=src, dry_run=False,
            notify=_capture,
        )
    assert counters["errors"] == 1
    assert notifications, "operator should be notified of orphan close"
    assert "orphan_closed" in notifications[0][0].lower()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT outcome, pnl, event_detail FROM ledger WHERE id = ?", (bet_id,)
    ).fetchone()
    conn.close()
    assert row["outcome"] == "ORPHAN_CLOSED"
    assert row["pnl"] == 0.0
    detail = json.loads(row["event_detail"])
    assert "orphan_close" in detail
    assert detail["orphan_close"]["order_id"] == "ORD-ORPHAN"
    assert detail["orphan_close"]["fill_price"] == 0.46
    assert detail["orphan_close"]["reason"] == "ymid_tp"


def test_monitor_skips_orphan_closed_rows_on_subsequent_tick(db: str) -> None:
    """Once a row is ORPHAN_CLOSED, the next monitor tick must NOT re-close it."""
    bet_id = _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0, event_type="bet")
    # Directly transition the row to ORPHAN_CLOSED as if a prior tick had
    # written the marker.
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE ledger SET outcome = 'ORPHAN_CLOSED', pnl = 0.0 WHERE id = ?",
        (bet_id,),
    )
    conn.commit()
    conn.close()

    src = FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.46", "size": "1000"}]}},
        bids={"TOK_YES": (0.46, 1000.0)},
    )
    src.set_close_result(_OrderResult(
        success=True, fill_price=0.46, fill_size=100.0,
        order_id="ORD-SHOULD-NOT-FIRE", transaction_hash="0xN",
    ))
    counters = run_tp_sl_monitor(
        FakeStation(), db, order_client=src, dry_run=False,
    )
    # No PENDING rows for this strategy/station -> early return, no fires.
    assert counters["tp_fired"] == 0
    assert counters["sl_fired"] == 0
    assert counters["errors"] == 0
    assert src.close_calls == [], "close_position must not be called on ORPHAN_CLOSED rows"


# ------------------------------------------------------------ Per-row TP regression
# Covers origin requirements AE4: a multi-row TAIL slot must close each row
# independently at its own ``fill_price + cfg.tp`` trigger. The monitor's
# correctness here is structural (one row per PENDING entry, each row carries
# its own fill_price), but this regression test pins the property explicitly
# so a future refactor that aggregates to a slot-level "average entry" gets
# caught.


def _insert_pending_tail(
    db_path: str,
    *,
    fill_price: float,
    fill_size: float = 100.0,
    bet_size: float | None = None,
    token_id: str = "TOK_TAIL",
    target_date: str = "2026-05-10",
    threshold: float = 22.5,
    bracket_low: float = 20.0,
    bracket_high: float = 22.0,
    event_type: str = "dry_run",
) -> int:
    """Insert one PENDING TAIL row. Mirror _insert_pending_ymid's shape."""
    if bet_size is None:
        bet_size = fill_price * fill_size
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    detail = {
        "strategy": "TAIL",
        "bracket_low": bracket_low,
        "bracket_high": bracket_high,
        "entry_top_price": fill_price,
    }
    cur = conn.execute(
        """INSERT INTO ledger
           (bet_ts, station_id, token_id, target_date, threshold, side,
            fill_price, fill_size, bet_size, outcome, event_type, event_detail)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "2026-05-10 00:00:00", "KJFK", token_id, target_date,
            threshold, "YES", fill_price, fill_size, bet_size,
            "PENDING", event_type, json.dumps(detail),
        ),
    )
    bet_id = cur.lastrowid
    conn.commit()
    conn.close()
    return bet_id


def test_multi_row_tail_slot_closes_each_row_at_own_threshold(db: str) -> None:
    """AE4: two TAIL rows on the same slot entered at fill_price=0.04 and
    fill_price=0.05 must close independently."""
    row_a = _insert_pending_tail(db, fill_price=0.04, fill_size=100.0)
    row_b = _insert_pending_tail(db, fill_price=0.05, fill_size=100.0)

    # Tick 1: bid=0.24 → row-A fires TP, row-B holds.
    src = FakePriceSource(
        books={"TOK_TAIL": {"bids": [{"price": "0.24", "size": "1000"}], "asks": []}},
        bids={"TOK_TAIL": (0.24, 1000.0)},
    )
    counters = run_tp_sl_monitor(FakeStation(), db, reader=src,
                                 strategy="TAIL", dry_run=True)
    assert counters["tp_fired"] == 1
    # Verify the ledger: row-A is CLOSED, row-B is still PENDING.
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {
        r["id"]: r for r in conn.execute(
            "SELECT id, outcome FROM ledger ORDER BY id"
        ).fetchall()
    }
    conn.close()
    assert rows[row_a]["outcome"] == "CLOSED"
    assert rows[row_b]["outcome"] == "PENDING"

    # Tick 2: bid=0.25 → row-B fires TP independently.
    src2 = FakePriceSource(
        books={"TOK_TAIL": {"bids": [{"price": "0.25", "size": "1000"}], "asks": []}},
        bids={"TOK_TAIL": (0.25, 1000.0)},
    )
    counters2 = run_tp_sl_monitor(FakeStation(), db, reader=src2,
                                  strategy="TAIL", dry_run=True)
    assert counters2["tp_fired"] == 1
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {
        r["id"]: r for r in conn.execute(
            "SELECT id, outcome FROM ledger ORDER BY id"
        ).fetchall()
    }
    conn.close()
    assert rows[row_a]["outcome"] == "CLOSED"
    assert rows[row_b]["outcome"] == "CLOSED"


def test_multi_row_tail_slot_both_rows_fire_same_tick(db: str) -> None:
    """When the bid jumps past both rows' triggers in one tick, both rows
    close in the same monitor pass with independent close records."""
    row_a = _insert_pending_tail(db, fill_price=0.04, fill_size=100.0)
    row_b = _insert_pending_tail(db, fill_price=0.05, fill_size=100.0)
    # Bid=0.26 exceeds both triggers (0.24 and 0.25).
    src = FakePriceSource(
        books={"TOK_TAIL": {"bids": [{"price": "0.26", "size": "1000"}], "asks": []}},
        bids={"TOK_TAIL": (0.26, 1000.0)},
    )
    counters = run_tp_sl_monitor(FakeStation(), db, reader=src,
                                 strategy="TAIL", dry_run=True)
    assert counters["tp_fired"] == 2
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {
        r["id"]: r for r in conn.execute(
            "SELECT id, outcome FROM ledger ORDER BY id"
        ).fetchall()
    }
    conn.close()
    assert rows[row_a]["outcome"] == "CLOSED"
    assert rows[row_b]["outcome"] == "CLOSED"


# ---------------------------------------------- Hourly-first-tick gate (2026-05-16)
#
# Mirrors the betting-side gate: NEW close decisions fire only at tick 0
# of the station's local hour. ``tick_index_for`` is offset-aware so a
# misfire that delays the cron-fire by up to one SCAN_INTERVAL_MINUTES
# still reads as tick 0. Stale-flag retries are exempt — a close that
# failed mid-attempt at tick 0 must still complete on tick 1+.

_TP_SL_GATE_OFFSET = icao_tick_offset("KJFK")  # FakeStation default


def _src_with_tp_bid() -> FakePriceSource:
    """Price source where bid 0.46 vs entry 0.30 → +0.16 ≥ YMID tp=0.15."""
    return FakePriceSource(
        books={"TOK_YES": {"bids": [{"price": "0.46", "size": "1000"}], "asks": []}},
        bids={"TOK_YES": (0.46, 1000.0)},
    )


def test_tp_sl_gate_skipped_when_local_now_minute_is_none(db: str) -> None:
    """Back-compat: omitting local_now_minute skips the gate, TP fires."""
    _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0)
    counters = run_tp_sl_monitor(
        FakeStation(), db, reader=_src_with_tp_bid(), dry_run=True,
    )
    assert counters["tp_fired"] == 1


def test_tp_sl_gate_tick_0_fires(db: str) -> None:
    """At the station's offset minute (tick 0), a fresh TP close fires."""
    _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0)
    counters = run_tp_sl_monitor(
        FakeStation(), db, reader=_src_with_tp_bid(), dry_run=True,
        local_now_minute=_TP_SL_GATE_OFFSET,
    )
    assert counters["tp_fired"] == 1


def test_tp_sl_gate_tick_0_under_one_minute_misfire(db: str) -> None:
    """Misfire-resilience: a tick scheduled at offset firing 1 min late still
    reads as tick 0 — the close fires instead of being silently deferred."""
    _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0)
    minute = (_TP_SL_GATE_OFFSET + 1) % 60
    counters = run_tp_sl_monitor(
        FakeStation(), db, reader=_src_with_tp_bid(), dry_run=True,
        local_now_minute=minute,
    )
    assert counters["tp_fired"] == 1


def test_tp_sl_gate_tick_1_skips_new_close(db: str) -> None:
    """Tick 1 (first minute of second decile) blocks a fresh new-close fire."""
    _insert_pending_ymid(db, fill_price=0.30, fill_size=100.0)
    minute = (_TP_SL_GATE_OFFSET + SCAN_INTERVAL_MINUTES) % 60
    counters = run_tp_sl_monitor(
        FakeStation(), db, reader=_src_with_tp_bid(), dry_run=True,
        local_now_minute=minute,
    )
    assert counters["tp_fired"] == 0
    assert counters["skipped"] == 1


def test_tp_sl_gate_was_retry_exempt_at_tick_1(db: str) -> None:
    """was_retry exemption: a stale-cleared close_in_flight row proceeds past the gate
    on tick 1+."""
    stale_ts = datetime.now(timezone.utc) - timedelta(seconds=TP_SL_FLAG_STALE_SECONDS + 60)
    _insert_pending_ymid(
        db, fill_price=0.30, fill_size=100.0,
        extra_detail={"close_in_flight": {"started_at": stale_ts.strftime("%Y-%m-%d %H:%M:%S")}},
    )
    minute = (_TP_SL_GATE_OFFSET + SCAN_INTERVAL_MINUTES) % 60  # tick 1
    counters = run_tp_sl_monitor(
        FakeStation(), db, reader=_src_with_tp_bid(), dry_run=True,
        local_now_minute=minute,
    )
    # Stale flag cleared this tick, was_retry=True, gate bypassed, TP fires.
    assert counters["stale_cleared"] == 1
    assert counters["tp_fired"] == 1
