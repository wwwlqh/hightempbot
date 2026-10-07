"""Tests for per-tick book_snapshots persistence + retention prune (FIX 2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hightempbot.db.connection import init_db
from hightempbot.persistence.ledger import prune_book_snapshots
from hightempbot.scheduler.betting_tick import _persist_book_snapshots, _top_ask


@pytest.fixture
def db(tmp_path):
    return init_db(tmp_path / "snap.db")


def _fake_enriched_mdata():
    """Two brackets as they look right after CLOB enrichment."""
    return {
        0: {
            "token_id": "yes0",
            "no_token_id": "no0",
            "bracket_label": "60-62°F",
            "best_ask": 0.45,
            "best_bid": 0.55,
            "volume24hr": 1234.5,
            # asks are NOT pre-sorted: top-of-book is the cheapest ask.
            "_yes_book": {
                "asks": [
                    {"price": "0.48", "size": "50"},
                    {"price": "0.46", "size": "100"},
                ],
                "bids": [{"price": "0.44", "size": "80"}],
            },
            "_no_book": {
                "asks": [{"price": "0.54", "size": "60"}],
                "bids": [],
            },
        },
        1: {
            "token_id": "yes1",
            "no_token_id": "no1",
            "bracket_label": "62-64°F",
            "best_ask": 0.90,
            "best_bid": 0.10,
            "volume24hr": None,
            "_yes_book": None,        # missing book
            "_no_book": "garbage",    # malformed book
        },
    }


class TestTopAsk:
    def test_picks_cheapest_ask(self):
        book = {"asks": [{"price": "0.48", "size": "50"},
                         {"price": "0.46", "size": "100"}]}
        assert _top_ask(book) == (0.46, 100.0)

    def test_none_and_malformed_return_none_pair(self):
        assert _top_ask(None) == (None, None)
        assert _top_ask("garbage") == (None, None)
        assert _top_ask({"asks": []}) == (None, None)
        assert _top_ask({"bids": [{"price": "0.5", "size": "1"}]}) == (None, None)
        # A level missing a price is skipped, not fatal.
        assert _top_ask({"asks": [{"size": "10"}]}) == (None, None)


class TestPersistBookSnapshots:
    def test_writes_one_row_per_bracket(self, db):
        logged: list[tuple] = []

        def log_health(stage, status, msg):
            logged.append((stage, status, msg))

        n = _persist_book_snapshots(db, "KLGA", "2024-07-15", _fake_enriched_mdata(), log_health)
        assert n == 2
        assert logged == []  # no failure logged

        rows = db.execute(
            "SELECT * FROM book_snapshots ORDER BY bracket_idx"
        ).fetchall()
        assert len(rows) == 2

        r0 = rows[0]
        assert r0["station_id"] == "KLGA"
        assert r0["target_date"] == "2024-07-15"
        assert r0["bracket_idx"] == 0
        assert r0["bracket_label"] == "60-62°F"
        assert r0["token_id"] == "yes0"
        assert r0["no_token_id"] == "no0"
        assert r0["best_ask"] == 0.45
        assert r0["best_bid"] == 0.55
        assert r0["yes_top_ask_price"] == 0.46      # cheapest of the two asks
        assert r0["yes_top_ask_size"] == 100.0
        assert r0["no_top_ask_price"] == 0.54
        assert r0["no_top_ask_size"] == 60.0
        assert r0["volume24hr"] == 1234.5

    def test_malformed_or_missing_book_does_not_raise(self, db):
        n = _persist_book_snapshots(db, "KLGA", "2024-07-15", _fake_enriched_mdata(), lambda *a: None)
        assert n == 2
        r1 = db.execute(
            "SELECT * FROM book_snapshots WHERE bracket_idx = 1"
        ).fetchone()
        # None/malformed books yield NULL top-of-book, but the row still lands.
        assert r1["yes_top_ask_price"] is None
        assert r1["yes_top_ask_size"] is None
        assert r1["no_top_ask_price"] is None
        assert r1["no_top_ask_size"] is None
        assert r1["volume24hr"] is None
        assert r1["best_ask"] == 0.90

    def test_non_dict_bracket_is_skipped_not_fatal(self, db):
        mdata = {0: "not-a-dict", 1: {"token_id": "y", "no_token_id": "n"}}
        n = _persist_book_snapshots(db, "KLGA", "2024-07-15", mdata, lambda *a: None)
        assert n == 1  # only the well-formed bracket persisted

    def test_persist_failure_never_raises_and_logs_once(self, db):
        """A DB failure is swallowed and logged exactly once for the tick."""
        logged: list[tuple] = []

        def log_health(stage, status, msg):
            logged.append((stage, status, msg))

        # Drop the table to force the INSERT to raise inside the helper.
        db.execute("DROP TABLE book_snapshots")
        db.commit()

        n = _persist_book_snapshots(db, "KLGA", "2024-07-15", _fake_enriched_mdata(), log_health)
        assert n == 0
        assert len(logged) == 1
        assert logged[0][0] == "book_snapshot"


class TestPruneBookSnapshots:
    def _insert(self, db, snapped_at):
        db.execute(
            "INSERT INTO book_snapshots "
            "(snapped_at, station_id, target_date, bracket_idx) VALUES (?, ?, ?, ?)",
            (snapped_at, "KLGA", "2024-07-15", 0),
        )
        db.commit()

    def test_prune_respects_retention(self, db):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
        recent = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        self._insert(db, old)
        self._insert(db, recent)

        deleted = prune_book_snapshots(db, retention_days=180)
        assert deleted == 1

        remaining = db.execute("SELECT snapped_at FROM book_snapshots").fetchall()
        assert len(remaining) == 1
        assert remaining[0]["snapped_at"] == recent

    def test_prune_keeps_everything_inside_window(self, db):
        now = datetime.now(timezone.utc)
        self._insert(db, (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"))
        self._insert(db, (now - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S"))
        assert prune_book_snapshots(db, retention_days=180) == 0
        assert db.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0] == 2
