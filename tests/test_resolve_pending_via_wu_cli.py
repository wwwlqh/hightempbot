"""CLI-level smoke tests for the WU-fallback operator CLI."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

import pytest

from hightempbot.cli import resolve_pending_via_wu as mod
from hightempbot.db.connection import init_db


class TestParseTargetDate:
    def test_accepts_iso_date(self):
        assert mod._parse_target_date("2026-05-17") == "2026-05-17"

    def test_rejects_non_iso_format(self):
        with pytest.raises(argparse.ArgumentTypeError):
            mod._parse_target_date("5/17/2026")

    def test_rejects_invalid_month(self):
        with pytest.raises(argparse.ArgumentTypeError):
            mod._parse_target_date("2026-13-17")

    def test_rejects_future_date(self):
        # Operator would never settle a future date — refuse rather than
        # silently exit 0 with "no PENDING rows match".
        with pytest.raises(argparse.ArgumentTypeError):
            mod._parse_target_date("2099-01-01")


class TestParseStation:
    def test_accepts_4char_icao(self):
        assert mod._parse_station("KSEA") == "KSEA"

    def test_uppercases_lowercase_input(self):
        assert mod._parse_station("ksea") == "KSEA"

    def test_rejects_short_code(self):
        with pytest.raises(argparse.ArgumentTypeError):
            mod._parse_station("XYZ")

    def test_rejects_long_code(self):
        with pytest.raises(argparse.ArgumentTypeError):
            mod._parse_station("KSEATTLE")


def _seed_manual_resolve_db(tmp_path, *, actual_source: str) -> tuple[str, str]:
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    target = (date.today() - timedelta(days=2)).isoformat()
    conn.execute(
        """INSERT INTO enrolled_stations
        (icao, city, lat, lon, timezone, unit, resolution_source,
         calibration_source, poly_slug, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("KDAL", "Dallas", 32.85, -96.85, "US/Central", "F", "wu", "wu", "dallas", "DRY_RUN"),
    )
    conn.execute(
        "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) "
        "VALUES (?, ?, ?, ?)",
        ("KDAL", target, 21.0, actual_source),
    )
    conn.execute(
        """INSERT INTO ledger
        (bet_ts, station_id, market_id, token_id, target_date,
         horizon, threshold, side, p_model, p_market, edge,
         kelly_size, volume_cap, bet_size, limit_price,
         fill_price, fill_size,
         outcome, event_type, event_detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"{target}T08:00:00Z",
            "KDAL",
            "m",
            "tok",
            target,
            1,
            70.5,
            "YES",
            0.45,
            0.30,
            0.15,
            5.0,
            5000.0,
            5.0,
            0.40,
            0.40,
            12.5,
            "PENDING",
            "dry_run",
            json.dumps({"bracket_low": 69.5, "bracket_high": 70.5}),
        ),
    )
    conn.commit()
    conn.close()
    return str(db_path), target


class TestManualResolveActualSourceFilter:
    def test_dry_run_skips_non_wu_actual_row(self, tmp_path, monkeypatch, capsys):
        db_path, target = _seed_manual_resolve_db(tmp_path, actual_source="ncei")
        monkeypatch.setattr(
            sys,
            "argv",
            ["resolve_pending_via_wu", "--db", db_path, "--target-date", target],
        )

        assert mod.main() == 0

        out = capsys.readouterr().out
        assert "no WU actual recorded" in out
        assert "WOULD-WRITE" not in out

    def test_dry_run_keeps_wu_actual_row(self, tmp_path, monkeypatch, capsys):
        db_path, target = _seed_manual_resolve_db(tmp_path, actual_source="wu")
        monkeypatch.setattr(
            sys,
            "argv",
            ["resolve_pending_via_wu", "--db", db_path, "--target-date", target],
        )

        assert mod.main() == 0

        out = capsys.readouterr().out
        assert "WOULD-WRITE" in out
        assert "WU actual recorded" not in out
