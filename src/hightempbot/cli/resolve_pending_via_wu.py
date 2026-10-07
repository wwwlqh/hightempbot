"""Settle PENDING bets from WU actuals when Polymarket archived the event
without resolving it. Same helper as the dashboard's resolve-pending action.

Dry-run by default. Dates fewer than POLYMARKET_FALLBACK_DAYS old are refused
unless ``--force``.

    python -m hightempbot.cli.resolve_pending_via_wu --target-date 2026-05-17 [--station KSEA] [--commit]
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from datetime import date


ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ICAO_RE = re.compile(r"^[A-Z][A-Z0-9]{3}$")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _parse_target_date(value: str) -> str:
    """Argparse type: accept ISO date string only (YYYY-MM-DD)."""
    if not ISO_DATE_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"--target-date must be ISO YYYY-MM-DD, got {value!r}"
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if parsed > date.today():
        raise argparse.ArgumentTypeError(
            f"--target-date {value} is in the future; refusing to settle"
        )
    return value


def _parse_station(value: str) -> str:
    """Argparse type: accept 4-char ICAO only (uppercased)."""
    upper = value.upper()
    if not ICAO_RE.match(upper):
        raise argparse.ArgumentTypeError(
            f"--station must be a 4-char ICAO (e.g., KSEA), got {value!r}"
        )
    return upper


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--db",
        default="/home/opc/hightempbot/data/hightempbot.db",
        help="Path to hightempbot.db (default: production path on Oracle Cloud).",
    )
    parser.add_argument(
        "--target-date", action="append", required=True, dest="target_dates",
        type=_parse_target_date,
        help="ISO target_date to settle (repeat for multiple). Required.",
    )
    parser.add_argument(
        "--station", action="append", dest="stations",
        type=_parse_station,
        help="Limit to specific ICAO(s). Omit to settle every station with a "
             "PENDING bet on any given target_date.",
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually call record_resolution. Default is dry-run.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Allow --commit to settle dates with days_past < POLYMARKET_FALLBACK_DAYS. "
             "Required when the auto-path has not yet given up on Polymarket — "
             "use only when the operator has confirmed Polymarket will never resolve.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    from hightempbot.execution.strategy_constants import POLYMARKET_FALLBACK_DAYS
    from hightempbot.resolution.settler import (
        _resolve_via_wu_actual_fallback,
        preview_wu_fallback_outcome,
    )
    from hightempbot.persistence.actuals import actual_source_clause
    from hightempbot.stations import (
        get_all_stations,
        register_enrolled_station,
    )

    from hightempbot.db.connection import get_connection
    conn = get_connection(args.db)
    try:
        stations = get_all_stations(conn)
        for s in stations.values():
            register_enrolled_station(s)

        target_dates = sorted(set(args.target_dates))
        station_filter = set(args.stations or [])

        sql = (
            "SELECT id, station_id, target_date, side, token_id, bet_size, "
            "fill_price, event_detail "
            "FROM ledger "
            "WHERE outcome='PENDING' "
            "AND event_type IN ('bet','dry_run') "
            f"AND target_date IN ({','.join('?' * len(target_dates))})"
        )
        params: list[str] = list(target_dates)
        if station_filter:
            sql += f" AND station_id IN ({','.join('?' * len(station_filter))})"
            params.extend(sorted(station_filter))
        sql += " ORDER BY target_date, station_id, id"

        rows = conn.execute(sql, params).fetchall()
        if not rows:
            print("No PENDING rows match. Nothing to do.")
            print("(If you expected matches, check --station spelling and ")
            print(" that --target-date has PENDING rows in the ledger.)")
            return 0

        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for r in rows:
            groups.setdefault((r["station_id"], r["target_date"]), []).append(r)

        print(f"=== {'COMMIT' if args.commit else 'DRY-RUN'} ===")
        print(f"Found {len(rows)} PENDING row(s) across {len(groups)} (station, target_date) group(s).")
        print()

        today = date.today()
        total_resolved = 0
        for (sid, td), grp in groups.items():
            station_cfg = stations.get(sid)
            if station_cfg is None:
                print(f"  SKIP {sid} {td}: station not enrolled (no StationConfig)")
                continue
            # Unknown unit: unresolvable (same as the real resolver).
            if not station_cfg.unit:
                print(
                    f"  SKIP {sid} {td}: station_cfg.unit is empty — "
                    f"refusing to resolve {len(grp)} bet(s) (production "
                    f"would refuse too)"
                )
                continue
            actuals_clause, actuals_params = actual_source_clause()
            actual_row = conn.execute(
                "SELECT tmax_celsius FROM actuals "
                "WHERE station_id=? AND local_date=? "
                f"AND {actuals_clause}",
                (sid, td, *actuals_params),
            ).fetchone()
            if actual_row is None:
                print(f"  SKIP {sid} {td}: no WU actual recorded ({len(grp)} bet(s) untouched)")
                continue
            actual_tmax = float(actual_row["tmax_celsius"])
            try:
                days_past = (today - date.fromisoformat(td)).days
            except (TypeError, ValueError):
                days_past = -1

            if args.commit and days_past < POLYMARKET_FALLBACK_DAYS and not args.force:
                print(
                    f"  REFUSE {sid} {td} (days_past={days_past} < "
                    f"POLYMARKET_FALLBACK_DAYS={POLYMARKET_FALLBACK_DAYS}): "
                    f"market may still resolve on Polymarket. Pass --force "
                    f"to override (use only when the operator has confirmed "
                    f"Polymarket will never resolve)."
                )
                continue

            if not args.commit:
                _print_dry_run_preview(
                    grp, sid, td, actual_tmax, station_cfg,
                    days_past, preview_wu_fallback_outcome,
                )
                continue

            resolved = _resolve_via_wu_actual_fallback(
                conn, sid, td, grp, actual_tmax, station_cfg,
                days_past=days_past,
            )
            total_resolved += resolved
            print(f"  {sid} {td} (days_past={days_past}): resolved {resolved}/{len(grp)} bet(s)")

        print()
        if args.commit:
            print(f"Total resolved: {total_resolved}")
        else:
            print("DRY-RUN — no writes. Re-run with --commit to write.")
        return 0
    finally:
        conn.close()


def _print_dry_run_preview(
    grp: list[sqlite3.Row],
    sid: str,
    td: str,
    actual_tmax: float,
    station_cfg,
    days_past: int,
    preview_fn,
) -> None:
    """Same preview as the dashboard endpoint."""
    for r in grp:
        p = preview_fn(r, actual_tmax, station_cfg, days_past)
        if p["outcome"] == "SKIP":
            print(
                f"  WOULD-SKIP id={p['bet_id']:5d} {sid} {td}: {p['reason']}"
            )
            continue
        push_note = (
            " [NULL-fill → PUSH]" if p["outcome"] == "PUSH" else ""
        )
        print(
            f"  WOULD-WRITE id={p['bet_id']:5d} {td} {sid:6s} "
            f"{p['side']:3s} bracket=[{p['bracket_low']},{p['bracket_high']}) "
            f"unit={p['actual_unit']} actual={actual_tmax:.1f}°C → "
            f"{p['actual_display']:.2f}°{p['actual_unit']}   "
            f"bet={p['outcome']} pnl_gross≈${p['pnl_gross']:+.2f} "
            f"(days_past={days_past}){push_note}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
