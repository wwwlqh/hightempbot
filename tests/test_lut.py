"""Tests for calibration.lut — bucket_of, seed + rebuild.

Covers Unit 2 of the live deployment plan:
  * ``bucket_of`` grid + boundary semantics
  * ``rebuild_lut`` empty/full/after-append behaviors
  * ``stamp_refreshed`` touches ``refreshed_at``
  * ``append_triples_for_date`` end-to-end against fixture data
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from hightempbot.calibration.emos import EMOSParams
from hightempbot.calibration.lut import (
    BUCKETS,
    append_triples_for_date,
    bucket_of,
    rebuild_lut,
    seed_lut_from_history,
    stamp_refreshed,
)
from hightempbot.calibration.store import save_emos_at
from hightempbot.db.connection import init_db
from hightempbot.execution.strategy_constants import EXPECTED_MODELS


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    conn = init_db(str(db_path))
    yield conn
    conn.close()


def _bucket_row(conn: sqlite3.Connection, station_id: str, bucket: tuple[float, float]):
    """Read one lut_bucket_stats row directly (mirrors how live code reads it)."""
    return conn.execute(
        "SELECT station_id, pred_bucket_low, pred_bucket_high, n, hits, "
        "observed, mean_pred, refreshed_at "
        "FROM lut_bucket_stats WHERE station_id = ? AND pred_bucket_low = ?",
        (station_id, bucket[0]),
    ).fetchone()


# -----------------------------------------------------------------------------
# bucket_of
# -----------------------------------------------------------------------------

class TestBucketOf:
    def test_locked_grid_has_8_buckets(self):
        assert len(BUCKETS) == 8
        # Notebook Cell F grid — exact values locked.
        assert BUCKETS[0] == (0.00, 0.02)
        assert BUCKETS[-1] == (0.60, 1.00)

    @pytest.mark.parametrize(
        "p,expected",
        [
            (0.00, (0.00, 0.02)),
            (0.019, (0.00, 0.02)),
            (0.02, (0.02, 0.05)),
            (0.049, (0.02, 0.05)),
            (0.051, (0.05, 0.10)),
            (0.10, (0.10, 0.15)),
            (0.25, (0.25, 0.40)),
            (0.40, (0.40, 0.60)),
            (0.60, (0.60, 1.00)),
            (0.99, (0.60, 1.00)),
            (1.00, (0.60, 1.00)),  # final bucket is right-inclusive
        ],
    )
    def test_bucket_boundaries(self, p, expected):
        assert bucket_of(p) == expected

    def test_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            bucket_of(-0.01)
        with pytest.raises(ValueError):
            bucket_of(1.01)


# -----------------------------------------------------------------------------
# rebuild_lut
# -----------------------------------------------------------------------------

def _insert_triple(conn, station, local_date, lo, hi, emos_p, hit):
    conn.execute(
        "INSERT OR REPLACE INTO pred_bucket_history "
        "(station_id, local_date, pred_bucket_low, pred_bucket_high, emos_p, hit) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (station, local_date, lo, hi, emos_p, hit),
    )


class TestRebuildLut:
    def test_empty_history_writes_no_rows(self, db):
        written = rebuild_lut(db, "KDAL")
        assert written == 0
        row = db.execute(
            "SELECT COUNT(*) AS c FROM lut_bucket_stats WHERE station_id = 'KDAL'"
        ).fetchone()
        assert row["c"] == 0

    def test_single_bucket_populated(self, db):
        # 100 triples in the [0.25, 0.40) bucket, 30 hits.
        for i in range(100):
            _insert_triple(
                db, "KDAL", f"2025-01-{i+1:02d}"[:10] if i < 30 else f"2025-02-{(i-29):02d}"[:10],
                0.25, 0.40, 0.30, 1 if i < 30 else 0,
            )
        db.commit()

        written = rebuild_lut(db, "KDAL")
        assert written == 1

        stats = _bucket_row(db, "KDAL", (0.25, 0.40))
        assert stats is not None
        assert stats["n"] == 100
        assert stats["hits"] == 30
        assert stats["observed"] == pytest.approx(0.30)
        assert stats["mean_pred"] == pytest.approx(0.30)

    def test_multiple_buckets(self, db):
        # Two buckets, different hit rates.
        for i in range(50):
            _insert_triple(
                db, "KDAL", f"2025-01-{i+1:02d}"[:10],
                0.05, 0.10, 0.07, 1 if i < 4 else 0,
            )
        for i in range(30):
            _insert_triple(
                db, "KDAL", f"2025-03-{i+1:02d}"[:10],
                0.25, 0.40, 0.32, 1 if i < 10 else 0,
            )
        db.commit()

        written = rebuild_lut(db, "KDAL")
        assert written == 2

        s_low = _bucket_row(db, "KDAL", (0.05, 0.10))
        s_high = _bucket_row(db, "KDAL", (0.25, 0.40))
        assert s_low["n"] == 50 and s_low["hits"] == 4
        assert s_high["n"] == 30 and s_high["hits"] == 10

    def test_rebuild_is_idempotent(self, db):
        for i in range(20):
            _insert_triple(db, "KDAL", f"2025-01-{i+1:02d}"[:10], 0.25, 0.40, 0.31, 1 if i < 6 else 0)
        db.commit()

        first = rebuild_lut(db, "KDAL")
        second = rebuild_lut(db, "KDAL")
        assert first == second == 1

    def test_observed_stable_after_appending_more_triples(self, db):
        # Seed with 30 triples at ~30% hit rate.
        for i in range(30):
            _insert_triple(db, "KDAL", f"2025-01-{i+1:02d}"[:10], 0.25, 0.40, 0.31, 1 if i < 9 else 0)
        db.commit()
        rebuild_lut(db, "KDAL")
        s_before = _bucket_row(db, "KDAL", (0.25, 0.40))

        # Append 70 more at the same hit rate.
        base = date(2025, 2, 1)
        for i in range(70):
            d = (base + timedelta(days=i)).isoformat()
            _insert_triple(db, "KDAL", d, 0.25, 0.40, 0.31, 1 if i < 21 else 0)
        db.commit()
        rebuild_lut(db, "KDAL")
        s_after = _bucket_row(db, "KDAL", (0.25, 0.40))

        assert s_before["n"] == 30
        assert s_after["n"] == 100
        assert s_after["observed"] == pytest.approx(0.30, abs=0.01)

    def test_rebuild_isolates_by_station(self, db):
        _insert_triple(db, "KDAL", "2025-01-01", 0.25, 0.40, 0.31, 1)
        _insert_triple(db, "KLGA", "2025-01-01", 0.05, 0.10, 0.07, 0)
        db.commit()

        rebuild_lut(db, "KDAL")
        rebuild_lut(db, "KLGA")

        kdal_rows = db.execute(
            "SELECT COUNT(*) AS c FROM lut_bucket_stats WHERE station_id = 'KDAL'"
        ).fetchone()["c"]
        klga_rows = db.execute(
            "SELECT COUNT(*) AS c FROM lut_bucket_stats WHERE station_id = 'KLGA'"
        ).fetchone()["c"]
        assert kdal_rows == 1
        assert klga_rows == 1

    def test_rebuild_removes_stale_buckets_when_history_cleared(self, db):
        _insert_triple(db, "KDAL", "2025-01-01", 0.25, 0.40, 0.31, 1)
        db.commit()
        rebuild_lut(db, "KDAL")
        assert _bucket_row(db, "KDAL", (0.25, 0.40)) is not None

        db.execute("DELETE FROM pred_bucket_history WHERE station_id = 'KDAL'")
        db.commit()
        rebuild_lut(db, "KDAL")
        assert _bucket_row(db, "KDAL", (0.25, 0.40)) is None


# -----------------------------------------------------------------------------
# stamp_refreshed
# -----------------------------------------------------------------------------

class TestStampRefreshed:
    def test_updates_timestamp_for_all_bucket_rows(self, db):
        _insert_triple(db, "KDAL", "2025-01-01", 0.05, 0.10, 0.08, 0)
        _insert_triple(db, "KDAL", "2025-01-02", 0.25, 0.40, 0.31, 1)
        db.commit()
        rebuild_lut(db, "KDAL")

        before = db.execute(
            "SELECT refreshed_at FROM lut_bucket_stats WHERE station_id = 'KDAL' "
            "ORDER BY pred_bucket_low LIMIT 1"
        ).fetchone()["refreshed_at"]

        # Ensure a visible delta.
        db.execute(
            "UPDATE lut_bucket_stats SET refreshed_at = '2020-01-01 00:00:00' "
            "WHERE station_id = 'KDAL'"
        )
        db.commit()

        stamp_refreshed(db, "KDAL")

        after = db.execute(
            "SELECT refreshed_at FROM lut_bucket_stats WHERE station_id = 'KDAL' "
            "ORDER BY pred_bucket_low LIMIT 1"
        ).fetchone()["refreshed_at"]

        assert after > "2020-01-01 00:00:00"
        assert after >= before or after == before  # fresh or newer


# -----------------------------------------------------------------------------
# append_triples_for_date — end-to-end with fixture forecasts + actuals
# -----------------------------------------------------------------------------

class TestAppendTriplesForDate:
    def _seed_market_tokens(self, db, station: str, brackets_c: list[tuple[float, float]]):
        for i, (lo, hi) in enumerate(brackets_c):
            db.execute(
                "INSERT OR REPLACE INTO market_tokens "
                "(station_id, market_date, bracket_idx, token_id, no_token_id, "
                " market_id, bracket_label, bracket_low, bracket_high) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (station, "2026-04-15", i, f"yt{i}", f"nt{i}", "mid",
                 f"{lo}-{hi}C", lo, hi),
            )
        db.commit()

    def _seed_walk_forward_params(self, db, station: str, horizon: int, asof: str):
        # Easy-to-predict params: μ = 20 + 1 * ens_mean, σ ≈ 1°C.
        save_emos_at(
            db, station, horizon, asof,
            EMOSParams(a=0.0, b=1.0, c=0.0, d=-5.0, n_samples=30),
        )

    def _seed_forecast(self, db, station: str, tdate: str, horizon: int, centres: dict[str, float]):
        for centre, tmax_c in centres.items():
            db.execute(
                "INSERT OR REPLACE INTO forecast_archive "
                "(station_id, target_date, horizon, issue_date, centre, "
                " member, tmax_celsius, source) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, 'openmeteo')",
                (station, tdate, horizon, "2026-04-14", centre, tmax_c),
            )
        db.commit()

    def _seed_actual(
        self,
        db,
        station: str,
        local_date: str,
        tmax_c: float,
        source: str = "wu",
    ):
        db.execute(
            "INSERT OR REPLACE INTO actuals "
            "(station_id, local_date, tmax_celsius, source) "
            "VALUES (?, ?, ?, ?)",
            (station, local_date, tmax_c, source),
        )
        db.commit()

    def test_missing_actual_writes_nothing(self, db):
        written = append_triples_for_date(db, "KDAL", 1, "2026-04-15", brackets=[(20.0, 22.0)])
        assert written == 0

    def test_non_wu_actual_writes_nothing(self, db):
        self._seed_walk_forward_params(db, "KDAL", 1, "2026-04-15")
        self._seed_actual(db, "KDAL", "2026-04-15", 21.0, source="ncei")
        self._seed_forecast(
            db, "KDAL", "2026-04-15", 1,
            {m: 21.0 + 0.02 * i for i, m in enumerate(EXPECTED_MODELS)},
        )

        written = append_triples_for_date(
            db, "KDAL", 1, "2026-04-15", brackets=[(20.0, 22.0)]
        )

        assert written == 0
        count = db.execute(
            "SELECT COUNT(*) AS n FROM pred_bucket_history WHERE station_id = 'KDAL'"
        ).fetchone()["n"]
        assert count == 0

    def test_missing_forecast_writes_nothing(self, db):
        self._seed_actual(db, "KDAL", "2026-04-15", 21.0)
        # No forecasts
        written = append_triples_for_date(db, "KDAL", 1, "2026-04-15", brackets=[(20.0, 22.0)])
        assert written == 0

    def test_missing_emos_params_writes_nothing(self, db):
        self._seed_actual(db, "KDAL", "2026-04-15", 21.0)
        self._seed_forecast(
            db, "KDAL", "2026-04-15", 1,
            {m: 21.0 + 0.02 * i for i, m in enumerate(EXPECTED_MODELS)},
        )
        # No EMOS params memoized → and no history to derive from.
        written = append_triples_for_date(
            db, "KDAL", 1, "2026-04-15", brackets=[(20.0, 22.0)]
        )
        assert written == 0

    def test_full_pipeline_writes_one_triple_per_bracket(self, db):
        brackets_c = [(18.0, 20.0), (20.0, 22.0), (22.0, 24.0)]
        self._seed_market_tokens(db, "KDAL", brackets_c)
        self._seed_walk_forward_params(db, "KDAL", 1, "2026-04-15")
        self._seed_actual(db, "KDAL", "2026-04-15", 21.0)  # lands in [20, 22)
        self._seed_forecast(
            db, "KDAL", "2026-04-15", 1,
            {m: 21.0 + 0.02 * i for i, m in enumerate(EXPECTED_MODELS)},
        )

        written = append_triples_for_date(
            db, "KDAL", 1, "2026-04-15", brackets=brackets_c
        )
        assert written == 3

        rows = db.execute(
            "SELECT pred_bucket_low, pred_bucket_high, emos_p, hit "
            "FROM pred_bucket_history WHERE station_id = 'KDAL' "
            "ORDER BY rowid"
        ).fetchall()
        assert len(rows) == 3

        # Exactly one bracket hit (the actual landed in [20, 22)).
        hits = sum(r["hit"] for r in rows)
        assert hits == 1

        # Bucket mapping valid for all.
        for r in rows:
            assert (r["pred_bucket_low"], r["pred_bucket_high"]) in BUCKETS

    def test_same_bucket_brackets_are_not_overwritten(self, db, monkeypatch):
        brackets_c = [(20.0, 21.0), (21.0, 22.0)]
        self._seed_market_tokens(db, "KDAL", brackets_c)
        self._seed_walk_forward_params(db, "KDAL", 1, "2026-04-15")
        self._seed_actual(db, "KDAL", "2026-04-15", 20.5)
        self._seed_forecast(
            db, "KDAL", "2026-04-15", 1,
            {m: 21.0 + 0.02 * i for i, m in enumerate(EXPECTED_MODELS)},
        )
        probs = iter([0.50, 0.49, 0.49, 0.48])
        monkeypatch.setattr(
            "hightempbot.calibration.lut.emos_probability",
            lambda *_args, **_kwargs: next(probs),
        )

        written = append_triples_for_date(
            db, "KDAL", 1, "2026-04-15", brackets=brackets_c,
        )

        assert written == 2
        rows = db.execute(
            "SELECT pred_bucket_low, bracket_key FROM pred_bucket_history "
            "WHERE station_id = 'KDAL' AND local_date = '2026-04-15'"
        ).fetchall()
        assert len(rows) == 2
        assert {r["pred_bucket_low"] for r in rows} == {0.0}
        assert len({r["bracket_key"] for r in rows}) == 2

    def test_idempotent_on_rerun(self, db):
        brackets_c = [(20.0, 22.0)]
        self._seed_market_tokens(db, "KDAL", brackets_c)
        self._seed_walk_forward_params(db, "KDAL", 1, "2026-04-15")
        self._seed_actual(db, "KDAL", "2026-04-15", 21.0)
        self._seed_forecast(
            db, "KDAL", "2026-04-15", 1,
            {m: 21.0 + 0.02 * i for i, m in enumerate(EXPECTED_MODELS)},
        )

        append_triples_for_date(db, "KDAL", 1, "2026-04-15", brackets=brackets_c)
        append_triples_for_date(db, "KDAL", 1, "2026-04-15", brackets=brackets_c)

        count = db.execute(
            "SELECT COUNT(*) AS c FROM pred_bucket_history "
            "WHERE station_id = 'KDAL' AND local_date = '2026-04-15'"
        ).fetchone()["c"]
        # UNIQUE(station, date, bucket_low) → at most 1 row per bracket per day.
        assert count == 1

    def test_brackets_for_station_converts_fahrenheit_bounds_to_celsius(self, db):
        from hightempbot.calibration.lut import _brackets_for_station

        db.execute(
            "INSERT INTO market_tokens "
            "(station_id, market_date, bracket_idx, token_id, no_token_id, "
            " market_id, bracket_label, bracket_low, bracket_high) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("KDAL", "2026-04-15", 0, "yt0", "nt0", "mid", "76-77°F", 75.5, 77.5),
        )
        db.commit()

        brackets = _brackets_for_station(db, "KDAL")

        assert brackets == [(24.166667, 25.277778)]

    def test_brackets_for_station_uses_one_market_date_not_union(self, db):
        from hightempbot.calibration.lut import _brackets_for_station

        rows = [
            ("KDAL", "2026-04-14", 0, "yt0", "nt0", "m0", "20C", 19.5, 20.5),
            ("KDAL", "2026-04-14", 1, "yt1", "nt1", "m1", "21C", 20.5, 21.5),
            ("KDAL", "2026-04-15", 0, "yt2", "nt2", "m2", "25C", 24.5, 25.5),
        ]
        db.executemany(
            "INSERT INTO market_tokens "
            "(station_id, market_date, bracket_idx, token_id, no_token_id, "
            " market_id, bracket_label, bracket_low, bracket_high) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        db.commit()

        assert _brackets_for_station(db, "KDAL") == [(24.5, 25.5)]
        assert _brackets_for_station(db, "KDAL", "2026-04-14") == [
            (19.5, 20.5),
            (20.5, 21.5),
        ]


# -----------------------------------------------------------------------------
# seed_lut_from_history — smoke test (full pipeline over a tiny history)
# -----------------------------------------------------------------------------

class TestSeedLutFromHistory:
    def _seed_day(
        self,
        db,
        station: str,
        local_date: str,
        horizon: int,
        centres: dict[str, float],
        actual: float,
    ):
        for centre, tmax in centres.items():
            db.execute(
                "INSERT OR REPLACE INTO forecast_archive "
                "(station_id, target_date, horizon, issue_date, centre, member, "
                " tmax_celsius, source) VALUES (?, ?, ?, ?, ?, 0, ?, 'openmeteo')",
                (station, local_date, horizon, local_date, centre, tmax),
            )
        db.execute(
            "INSERT OR REPLACE INTO actuals "
            "(station_id, local_date, tmax_celsius, source) "
            "VALUES (?, ?, ?, 'wu')",
            (station, local_date, actual),
        )

    def test_no_history_returns_zero(self, db):
        days, triples = seed_lut_from_history(db, "KDAL")
        assert days == 0 and triples == 0

    def test_no_market_tokens_skips(self, db):
        self._seed_day(db, "KDAL", "2025-03-01", 1, {"ecmwf": 20.0, "gfs": 20.2}, 21.0)
        db.commit()
        days, triples = seed_lut_from_history(db, "KDAL")
        assert days == 0 and triples == 0

    def test_seed_memoizes_walk_forward_emos(self, db):
        # Seed 30 days of training data then call seed for the 31st day.
        for i in range(40):
            d = (date(2025, 3, 1) + timedelta(days=i)).isoformat()
            # Smooth random walk
            centres = {
                model: 18.0 + 0.1 * i + (idx * 0.01)
                for idx, model in enumerate(EXPECTED_MODELS)
            }
            self._seed_day(
                db, "KDAL", d, 1,
                centres,
                18.5 + 0.1 * i,
            )

        # Old shifted grids must not be unioned into every seed day.
        db.executemany(
            "INSERT INTO market_tokens "
            "(station_id, market_date, bracket_idx, token_id, no_token_id, "
            " market_id, bracket_label, bracket_low, bracket_high) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("KDAL", "2025-02-28", 0, "old_y0", "old_n0", "old0", "18-19C", 18.0, 19.0),
                ("KDAL", "2025-02-28", 1, "old_y1", "old_n1", "old1", "19-20C", 19.0, 20.0),
            ],
        )
        # Seed one latest-grid bracket so seed_lut_from_history has work.
        db.execute(
            "INSERT INTO market_tokens "
            "(station_id, market_date, bracket_idx, token_id, no_token_id, "
            " market_id, bracket_label, bracket_low, bracket_high) "
            "VALUES ('KDAL', '2025-03-01', 0, 'yt', 'nt', 'm', '20-22C', 20.0, 22.0)",
        )
        db.execute(
            "INSERT INTO pred_bucket_history "
            "(station_id, local_date, pred_bucket_low, pred_bucket_high, "
            " bracket_low, bracket_high, bracket_key, emos_p, hit) "
            "VALUES ('KDAL', '2020-01-01', 0.60, 1.00, 99.0, 100.0, "
            " '99.000000:100.000000', 0.70, 1)"
        )
        db.commit()

        days, triples = seed_lut_from_history(db, "KDAL", horizon=1)

        # At least some days past the 30-day warm-up should produce triples
        # once enough history exists for the walk-forward window.
        assert days > 0
        assert triples == days
        assert db.execute(
            "SELECT COUNT(*) AS n FROM pred_bucket_history "
            "WHERE station_id = 'KDAL' AND bracket_key = '99.000000:100.000000'"
        ).fetchone()["n"] == 0

        # History memoization: each seeded day should have an entry in
        # calibration_params_history.
        cached = db.execute(
            "SELECT COUNT(*) AS c FROM calibration_params_history "
            "WHERE station_id = 'KDAL'"
        ).fetchone()["c"]
        assert cached == days

        # lut_bucket_stats rebuilt after seeding.
        lut_rows = db.execute(
            "SELECT COUNT(*) AS c FROM lut_bucket_stats WHERE station_id = 'KDAL'"
        ).fetchone()["c"]
        assert lut_rows >= 1

    def test_pairs_for_fit_rejects_drifting_model_subsets(self, db):
        from hightempbot.calibration.lut import _pairs_for_fit

        for i in range(25):
            d = (date(2025, 3, 1) + timedelta(days=i)).isoformat()
            actual = 20.0 + i * 0.1
            db.execute(
                "INSERT OR REPLACE INTO actuals "
                "(station_id, local_date, tmax_celsius, source) "
                "VALUES (?, ?, ?, 'wu')",
                ("KDAL", d, actual),
            )

            missing_idx = i % len(EXPECTED_MODELS)
            for idx, model in enumerate(EXPECTED_MODELS):
                if idx == missing_idx:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO forecast_archive "
                    "(station_id, target_date, horizon, issue_date, centre, member, "
                    " tmax_celsius, source) VALUES (?, ?, ?, ?, ?, ?, ?, 'openmeteo')",
                    ("KDAL", d, 1, d, model, idx + 1, actual + idx * 0.01),
                )
        db.commit()

        pairs = _pairs_for_fit(db, "KDAL", 1, "2025-03-25", 30)
        assert pairs is None

    def test_pairs_for_fit_ignores_non_wu_actuals(self, db):
        from hightempbot.calibration.lut import _pairs_for_fit

        for i in range(25):
            d = (date(2025, 3, 1) + timedelta(days=i)).isoformat()
            actual = 20.0 + i * 0.1
            db.execute(
                "INSERT OR REPLACE INTO actuals "
                "(station_id, local_date, tmax_celsius, source) "
                "VALUES (?, ?, ?, 'ncei')",
                ("KDAL", d, actual),
            )
            for idx, model in enumerate(EXPECTED_MODELS):
                db.execute(
                    "INSERT OR REPLACE INTO forecast_archive "
                    "(station_id, target_date, horizon, issue_date, centre, member, "
                    " tmax_celsius, source) VALUES (?, ?, ?, ?, ?, ?, ?, 'openmeteo')",
                    ("KDAL", d, 1, d, model, idx + 1, actual + idx * 0.01),
                )
        db.commit()

        pairs = _pairs_for_fit(db, "KDAL", 1, "2025-03-25", 30)

        assert pairs is None
