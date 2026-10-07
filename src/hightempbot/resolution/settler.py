"""Per-station resolution tick: settle PENDING bets from Polymarket's close state.

Runs every scan interval, 24/7. Same-local-day dates wait until
RESOLUTION_SCAN_START_HOUR.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import date, datetime
from typing import Any

import pytz

from hightempbot.db.connection import get_connection
from hightempbot.persistence.actuals import actual_source_clause
from hightempbot.persistence.ledger import decode_event_detail
from hightempbot.resolution.gamma import (
    fetch_gamma_resolution_markets,
    winning_bracket_from_gamma,
)
from hightempbot.scheduler.market_data import _fetch_market_data
from hightempbot.scheduler.station_scanner import (
    _actual_matches_bracket,
    _log_pipeline_health,
    _notify,
    _station_actual_display,
)
from hightempbot.stations import (
    StationConfig,
    celsius_to_fahrenheit,
    get_all_stations,
)

logger = logging.getLogger(__name__)


def _bet_pnl(fill_price: float, bet_size: float, won: bool) -> float:
    return ((1.0 / fill_price) - 1.0) * float(bet_size) if won else -float(bet_size)


def run_resolution_tick(
    station: StationConfig,
    db_path: str,
) -> None:
    """Settle this station's PENDING bets whose Polymarket event has closed.

    Only Gamma close-state settles automatically; the CLOB threshold scan is
    behind EARLY_RESOLUTION_ENABLED (off) and the WU fallback is manual only.
    """
    station_id = station.icao
    tz = pytz.timezone(station.timezone)
    local_now = datetime.now(tz)

    conn = get_connection(db_path)
    try:
        from hightempbot.db.connection import log_pipeline_health
        log_pipeline_health(
            conn, station_id, "scan", "OK",
            f"resolution tick {local_now.strftime('%H:%M')} local",
        )

        pending = conn.execute(
            """SELECT id, station_id, target_date, threshold, side, token_id, bet_size,
                      fill_price, fill_size, limit_price, event_type, event_detail
            FROM ledger
            WHERE station_id = ? AND outcome = 'PENDING'
            AND event_type IN ('bet', 'dry_run')""",
            (station_id,),
        ).fetchall()

        if pending:
            from collections import defaultdict
            by_date: dict[str, list] = defaultdict(list)
            for row in pending:
                by_date[row["target_date"]].append(row)

            for target_date, bets in by_date.items():
                from hightempbot.execution.strategy_constants import RESOLUTION_SCAN_START_HOUR
                local_today = local_now.date().isoformat()
                if target_date > local_today:
                    continue
                if (
                    target_date == local_today
                    and local_now.hour < RESOLUTION_SCAN_START_HOUR
                ):
                    continue
                try:
                    _resolve_station_date(conn, station_id, target_date, bets)
                except Exception:
                    logger.error(
                        "Resolution failed for %s %s",
                        station_id, target_date, exc_info=True,
                    )

        try:
            from hightempbot.persistence.ledger import (
                backfill_polymarket_resolution_labels,
            )
            backfill_polymarket_resolution_labels(conn, station_id=station_id)
        except Exception:
            logger.error(
                "Polymarket resolution-label backfill failed for %s",
                station_id, exc_info=True,
            )
    except Exception:
        logger.error("Resolution tick failed for %s", station_id, exc_info=True)
    finally:
        conn.close()


def _is_intish(val: float | None) -> bool:
    """Return True when ``val`` is non-None and within 1e-6 of an integer."""
    return val is not None and abs(val - round(val)) < 1e-6


def _is_legacy_integer_bracket(lo: float | None, hi: float | None) -> bool:
    """Old rows stored integer labels: ``lo == hi`` (1°C) or ``hi - lo == 1`` (2°F)."""
    return (
        _is_intish(lo)
        and _is_intish(hi)
        and ((lo == hi) or (abs(hi - lo - 1.0) < 1e-6))
    )


def _bet_matches_winner(
    bet_low: float | None,
    bet_high: float | None,
    winning_low: float | None,
    winning_high: float | None,
    *,
    station_id: str = "",
    bet_id: int | None = None,
) -> bool:
    """Match a bet's stored bounds to the winning bracket, including legacy integer bounds."""
    if bet_low == winning_low and bet_high == winning_high:
        return True

    if (
        bet_low is None and winning_low is None
        and bet_high is not None and winning_high is not None
        and _is_intish(bet_high)
        and abs((bet_high + 0.5) - winning_high) < 1e-6
    ):
        logger.info(
            "legacy-format compat: matched bet_id=%s (%s) floor bounds=(%s,%s) to winner=(%s,%s)",
            bet_id, station_id, bet_low, bet_high, winning_low, winning_high,
        )
        return True

    if (
        bet_high is None and winning_high is None
        and bet_low is not None and winning_low is not None
        and _is_intish(bet_low)
        and abs((bet_low - 0.5) - winning_low) < 1e-6
    ):
        logger.info(
            "legacy-format compat: matched bet_id=%s (%s) ceiling bounds=(%s,%s) to winner=(%s,%s)",
            bet_id, station_id, bet_low, bet_high, winning_low, winning_high,
        )
        return True

    if bet_low is None or bet_high is None:
        return False
    if winning_low is None or winning_high is None:
        return False

    is_legacy = _is_legacy_integer_bracket(bet_low, bet_high)
    if is_legacy and winning_low <= bet_low < winning_high:
        logger.info(
            "legacy-format compat: matched bet_id=%s (%s) bounds=(%s,%s) to winner=(%s,%s)",
            bet_id, station_id, bet_low, bet_high, winning_low, winning_high,
        )
        return True
    return False


def _bet_matches_market(
    bet_low: float | None,
    bet_high: float | None,
    market: dict,
    bet_token: str,
    *,
    station_id: str = "",
    bet_id: int | None = None,
    bet_label: str | None = None,
) -> bool:
    """Whether a ledger bet belongs to a market's bracket.

    Missing bounds are recovered from the label; token matching is the last
    resort (and warns, since a relist changes the token).
    """
    market_low = market.get("bracket_low")
    market_high = market.get("bracket_high")
    if bet_low is not None or bet_high is not None:
        return _bet_matches_winner(
            bet_low,
            bet_high,
            market_low,
            market_high,
            station_id=station_id,
            bet_id=bet_id,
        )

    if bet_label:
        # Imported here to avoid an import cycle.
        from hightempbot.resolution.gamma import parse_bracket_bounds
        parsed = parse_bracket_bounds(bet_label)
        if parsed is not None:
            recovered_low, recovered_high, _ = parsed
            return _bet_matches_winner(
                recovered_low,
                recovered_high,
                market_low,
                market_high,
                station_id=station_id,
                bet_id=bet_id,
            )

    token_match = bet_token in {market.get("token_id", ""), market.get("no_token_id", "")}
    if token_match:
        logger.warning(
            "bet_id=%s (%s) matched market by bet_token only "
            "(no bracket bounds, label=%r) — Polymarket relist could resolve "
            "this against the wrong bracket; manual review recommended",
            bet_id, station_id, bet_label,
        )
    return token_match


def _resolve_bet_from_terminal_yes_price(
    side: str,
    yes_price: float,
) -> bool:
    """Win/loss from a terminal YES price (≥0.995 won, ≤0.005 lost); NO is the inverse."""
    side_up = side.upper()
    if side_up == "YES":
        return yes_price >= 0.5
    return yes_price < 0.5


def _outcome_from_resolved_bracket(side: str, bet_matches_resolved_bracket: bool) -> str:
    """Return ledger outcome when Polymarket identifies the winning bracket."""
    side_up = side.upper()
    won = bet_matches_resolved_bracket if side_up == "YES" else not bet_matches_resolved_bracket
    return "WIN" if won else "LOSS"


def _bracket_detail(market: dict, *, prefix: str) -> dict:
    """Persist bracket bounds keyed by ``prefix`` (e.g. ``resolution`` / ``closed``)."""
    return {
        f"{prefix}_bracket_low": market.get("bracket_low"),
        f"{prefix}_bracket_high": market.get("bracket_high"),
    }


def _resolved_bracket_detail(market: dict) -> dict:
    return _bracket_detail(market, prefix="resolution")


def _effective_fill_price(bet_row) -> float:
    """fill_price, else limit_price, else 0.0 (non-finite or ≤0 counts as missing)."""
    for key in ("fill_price", "limit_price"):
        try:
            raw = bet_row[key]
        except (IndexError, KeyError):
            raw = None
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return 0.0


def _apply_null_fill_push(
    conn: sqlite3.Connection,
    bet_id: int,
    station_id: str,
    context_label: str,
    *,
    won: bool,
) -> tuple[str, float, dict]:
    """Book a bet with no fill price as PUSH (pnl 0), recording the real result.

    Returns ``(outcome, pnl, extra_detail)`` where extra_detail carries
    ``null_fill_price_push``, ``bracket_resolution`` and, for losses,
    ``unrecorded_loss``. Logs CRITICAL and writes a pipeline_health error.
    """
    bracket_resolution = "WIN" if won else "LOSS"
    logger.critical(
        "bet_id=%s (%s) %s: NULL/zero/NaN fill_price; bracket resolved %s but "
        "pnl is unquantifiable — booking PUSH (pnl=0). %s",
        bet_id, station_id, context_label, bracket_resolution,
        "Unrecorded LOSS — real position lost without recorded entry price." if not won
            else "Bet would have won but payout is unknown.",
    )
    try:
        _log_pipeline_health(
            conn,
            station_id,
            f"resolution_{context_label}",
            "ERROR",
            f"bet_id={bet_id} NULL fill_price -> PUSH (bracket={bracket_resolution})",
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to record pipeline_health for NULL-fill PUSH bet_id=%s", bet_id
        )
    extra: dict[str, Any] = {
        "null_fill_price_push": True,
        "bracket_resolution": bracket_resolution,
    }
    if not won:
        extra["unrecorded_loss"] = True
    return "PUSH", 0.0, extra


def _terminal_token_result(
    reader,
    token_id: str,
    *,
    win_threshold: float,
    loss_threshold: float,
) -> tuple[bool, float] | None:
    """Return (won, price) when a bought outcome token is terminal."""
    if not token_id:
        return None
    book = reader.fetch_order_book(token_id)
    if not book:
        return None
    bid = reader.best_bid(book)
    if bid and bid[0] >= win_threshold:
        return True, float(bid[0])
    # A loss needs an executable ask at the floor; a missing ask proves nothing.
    ask = reader.best_ask(book)
    if ask is not None and ask[0] <= loss_threshold:
        return False, float(ask[0])
    return None


def _resolve_station_date(
    conn: sqlite3.Connection,
    station_id: str,
    target_date: str,
    bets: list,
) -> None:
    """Resolve one (station, target_date) group of PENDING bets.

    With EARLY_RESOLUTION_ENABLED, CLOB terminal prices are tried first.
    Otherwise only ``_resolve_via_gamma_close`` settles; if Gamma isn't
    closed yet the bets stay PENDING.
    """
    from hightempbot.execution.strategy_constants import (
        EARLY_RESOLUTION_ENABLED,
        RESOLUTION_LOSS_PRICE_THRESHOLD,
        RESOLUTION_PRICE_THRESHOLD,
    )
    from hightempbot.execution.walker import ClobReader
    from hightempbot.persistence.ledger import record_resolution
    station_cfg = get_all_stations(conn).get(station_id)
    target_day = date.fromisoformat(target_date)

    # Only read actuals from supported sources.
    actuals_clause, actuals_params = actual_source_clause()
    actual_row = conn.execute(
        f"SELECT tmax_celsius FROM actuals "
        f"WHERE station_id = ? AND local_date = ? "
        f"AND {actuals_clause}",
        (station_id, target_date, *actuals_params),
    ).fetchone()
    actual_tmax = actual_row["tmax_celsius"] if actual_row else None
    actual_display = (
        _station_actual_display(actual_tmax, station_cfg)
        if actual_tmax is not None else None
    )

    winning_bracket = None
    terminal_markets: list[dict] = []
    reader = None
    market_data: dict | None = None
    if EARLY_RESOLUTION_ENABLED:
        market_data = _fetch_market_data(station_id, target_day, conn=conn)
        if market_data:
            try:
                reader = ClobReader()
            except Exception as exc:
                msg = f"CLOB resolution unavailable for {target_date}: {exc}"
                _log_pipeline_health(conn, station_id, "resolution", "ERROR", msg)
                logger.warning("Resolution %s %s: %s", station_id, target_date, msg)
                return
            for _bi, mkt in market_data.items():
                yes_token = mkt.get("token_id", "")
                if not yes_token:
                    continue
                book = reader.fetch_order_book(yes_token)
                if not book:
                    continue
                bid = reader.best_bid(book)
                if bid and bid[0] >= RESOLUTION_PRICE_THRESHOLD:
                    mkt["_resolved_price"] = bid[0]
                    mkt["_terminal_yes_price"] = bid[0]
                    terminal_markets.append(mkt)
                    winning_bracket = mkt
                    break
                ask = reader.best_ask(book)
                if ask is not None and ask[0] <= RESOLUTION_LOSS_PRICE_THRESHOLD:
                    mkt["_resolved_price"] = float(ask[0])
                    mkt["_terminal_yes_price"] = float(ask[0])
                    terminal_markets.append(mkt)
        else:
            _log_pipeline_health(conn, station_id, "resolution", "WARNING", f"No Polymarket market data for {target_date}")
            logger.debug("Resolution %s %s: no market data from Polymarket", station_id, target_date)

    if winning_bracket is not None:
        winning_low = winning_bracket.get("bracket_low")
        winning_high = winning_bracket.get("bracket_high")
        winning_token = winning_bracket.get("token_id", "")
        winning_label = winning_bracket.get("bracket_label", f"[{winning_low},{winning_high}]")
        resolved_price = winning_bracket.get("_resolved_price", 0.0)

        logger.info(
            "Resolution %s %s: winning bracket %s (price=%.3f)",
            station_id, target_date, winning_label, resolved_price,
        )

        for bet_row in bets:
            side = bet_row["side"]
            fill_price = _effective_fill_price(bet_row)
            bet_size = bet_row["bet_size"]
            bet_token = bet_row["token_id"]

            detail = decode_event_detail(bet_row["event_detail"])

            bet_low = detail.get("bracket_low")
            bet_high = detail.get("bracket_high")
            bet_label = bet_row["bracket_label"] if "bracket_label" in bet_row.keys() else detail.get("bracket_label")

            if bet_low is not None or bet_high is not None:
                bet_is_winner = _bet_matches_winner(
                    bet_low, bet_high, winning_low, winning_high,
                    station_id=station_id, bet_id=bet_row["id"],
                )
            elif bet_label:
                # Recover bounds from the label so a relisted market still matches.
                from hightempbot.resolution.gamma import parse_bracket_bounds
                parsed = parse_bracket_bounds(bet_label)
                if parsed is not None:
                    rec_low, rec_high, _ = parsed
                    bet_is_winner = _bet_matches_winner(
                        rec_low, rec_high, winning_low, winning_high,
                        station_id=station_id, bet_id=bet_row["id"],
                    )
                else:
                    bet_is_winner = (bet_token == winning_token)
                    if bet_is_winner:
                        logger.warning(
                            "bet_id=%s (%s) winning match by bet_token only "
                            "(label=%r failed to parse) — relist could mis-resolve",
                            bet_row["id"], station_id, bet_label,
                        )
            else:
                bet_is_winner = (bet_token == winning_token)
                if bet_is_winner:
                    logger.warning(
                        "bet_id=%s (%s) winning match by bet_token only "
                        "(no bracket bounds, no label) — relist could mis-resolve",
                        bet_row["id"], station_id,
                    )

            outcome = _outcome_from_resolved_bracket(side, bet_is_winner)
            won = outcome == "WIN"
            extra_detail = _resolved_bracket_detail(winning_bracket)
            if not (fill_price > 0):
                outcome, pnl, push_extra = _apply_null_fill_push(
                    conn, bet_row["id"], station_id,
                    context_label="winning_bracket",
                    won=won,
                )
                extra_detail = {**extra_detail, **push_extra}
            else:
                pnl = _bet_pnl(fill_price, bet_size, won)

            if not record_resolution(
                conn,
                bet_row["id"],
                actual_tmax,
                outcome,
                pnl,
                resolution_label=winning_label,
                resolution_source="polymarket_winner",
                resolution_price=resolved_price,
                extra_detail=extra_detail,
            ):
                continue
            logger.info(
                "Resolved %s bet_id=%d: %s (Polymarket bracket %s), pnl=$%.2f",
                station_id, bet_row["id"], outcome, winning_label, pnl,
            )
            _notify(
                f"Resolution: {station_id} {outcome}",
                f"bet_id={bet_row['id']}, bracket={winning_label}, pnl=${pnl:+.2f}",
                stage="resolution",
                station_id=station_id,
            )

        if actual_display is not None:
            try:
                actual_agrees = _actual_matches_bracket(actual_display, winning_low, winning_high)
                if not actual_agrees:
                    logger.warning(
                        "R29 MISMATCH %s %s: Polymarket resolved bracket %s but actual=%.1f%s (display=%s). "
                        "Polymarket resolution takes precedence.",
                        station_id, target_date, winning_label, actual_tmax, "°C", actual_display,
                    )
            except Exception:
                pass
        _log_pipeline_health(
            conn,
            station_id,
            "resolution",
            "OK",
            f"Resolved {target_date} from Polymarket at {resolved_price:.3f}",
        )
        return

    if terminal_markets:
        resolved_count = 0
        for bet_row in bets:
            side = bet_row["side"]
            fill_price = _effective_fill_price(bet_row)
            bet_size = bet_row["bet_size"]
            bet_token = bet_row["token_id"]

            detail = decode_event_detail(bet_row["event_detail"])

            bet_low = detail.get("bracket_low")
            bet_high = detail.get("bracket_high")
            bet_label = bet_row["bracket_label"] if "bracket_label" in bet_row.keys() else detail.get("bracket_label")
            terminal_market = next(
                (
                    m
                    for m in terminal_markets
                    if _bet_matches_market(
                        bet_low,
                        bet_high,
                        m,
                        bet_token,
                        station_id=station_id,
                        bet_id=bet_row["id"],
                        bet_label=bet_label,
                    )
                ),
                None,
            )
            if terminal_market is None:
                continue

            resolved_price = terminal_market.get("_terminal_yes_price")
            if resolved_price is None:
                continue

            won = _resolve_bet_from_terminal_yes_price(side, float(resolved_price))
            outcome = "WIN" if won else "LOSS"
            label = terminal_market.get(
                "bracket_label",
                f"[{terminal_market.get('bracket_low')},{terminal_market.get('bracket_high')}]",
            )
            extra_detail = {
                "terminal_bracket_label": label,
                **(
                    _resolved_bracket_detail(terminal_market)
                    if float(resolved_price) >= 0.5
                    else {}
                ),
            }
            if not (fill_price > 0):
                outcome, pnl, push_extra = _apply_null_fill_push(
                    conn, bet_row["id"], station_id,
                    context_label="terminal_yes",
                    won=won,
                )
                extra_detail = {**extra_detail, **push_extra}
            else:
                pnl = _bet_pnl(fill_price, bet_size, won)

            if not record_resolution(
                conn,
                bet_row["id"],
                actual_tmax,
                outcome,
                pnl,
                resolution_label=label if float(resolved_price) >= 0.5 else None,
                resolution_source="polymarket_terminal_yes",
                resolution_price=float(resolved_price),
                extra_detail=extra_detail,
            ):
                continue
            resolved_count += 1
            logger.info(
                "Resolved %s bet_id=%d: %s (Polymarket bracket %s terminal YES=%.3f), pnl=$%.2f",
                station_id,
                bet_row["id"],
                outcome,
                label,
                float(resolved_price),
                pnl,
            )
            _notify(
                f"Resolution: {station_id} {outcome}",
                f"bet_id={bet_row['id']}, bracket={label}, pnl=${pnl:+.2f}",
                stage="resolution",
                station_id=station_id,
            )

        if resolved_count > 0:
            _log_pipeline_health(
                conn,
                station_id,
                "resolution",
                "OK",
                f"Resolved {resolved_count} bet(s) for {target_date} from terminal Polymarket prices",
            )
            return

    if market_data and reader is not None:
        resolved_count = 0
        for bet_row in bets:
            side = bet_row["side"]
            fill_price = _effective_fill_price(bet_row)
            bet_size = bet_row["bet_size"]
            bet_token = bet_row["token_id"]

            terminal = _terminal_token_result(
                reader,
                bet_token,
                win_threshold=RESOLUTION_PRICE_THRESHOLD,
                loss_threshold=RESOLUTION_LOSS_PRICE_THRESHOLD,
            )
            if terminal is None:
                continue
            won, terminal_price = terminal

            detail = decode_event_detail(bet_row["event_detail"])
            bet_low = detail.get("bracket_low")
            bet_high = detail.get("bracket_high")
            bet_label = bet_row["bracket_label"] if "bracket_label" in bet_row.keys() else detail.get("bracket_label")
            terminal_market = next(
                (
                    m
                    for m in market_data.values()
                    if _bet_matches_market(
                        bet_low,
                        bet_high,
                        m,
                        bet_token,
                        station_id=station_id,
                        bet_id=bet_row["id"],
                        bet_label=bet_label,
                    )
                ),
                None,
            )
            terminal_label = None
            terminal_detail: dict = {}
            if terminal_market is not None:
                terminal_label = terminal_market.get(
                    "bracket_label",
                    f"[{terminal_market.get('bracket_low')},{terminal_market.get('bracket_high')}]",
                )
                terminal_detail["terminal_bracket_label"] = terminal_label
                if (side.upper() == "YES" and won) or (side.upper() == "NO" and not won):
                    terminal_detail.update(_resolved_bracket_detail(terminal_market))
                else:
                    terminal_label = None

            if not (fill_price > 0):
                outcome, pnl, push_extra = _apply_null_fill_push(
                    conn, bet_row["id"], station_id,
                    context_label="terminal_token",
                    won=won,
                )
                terminal_detail = {**terminal_detail, **push_extra}
            else:
                outcome = "WIN" if won else "LOSS"
                pnl = _bet_pnl(fill_price, bet_size, won)

            if not record_resolution(
                conn,
                bet_row["id"],
                actual_tmax,
                outcome,
                pnl,
                resolution_label=terminal_label,
                resolution_source="polymarket_terminal_token",
                resolution_price=terminal_price,
                extra_detail=terminal_detail,
            ):
                continue
            resolved_count += 1
            logger.info(
                "Resolved %s bet_id=%d: %s (bet token %s terminal=%.3f), pnl=$%.2f",
                station_id,
                bet_row["id"],
                outcome,
                bet_token,
                terminal_price,
                pnl,
            )
            _notify(
                f"Resolution: {station_id} {outcome}",
                f"bet_id={bet_row['id']}, token={bet_token}, pnl=${pnl:+.2f}",
                stage="resolution",
                station_id=station_id,
            )

        if resolved_count > 0:
            _log_pipeline_health(
                conn,
                station_id,
                "resolution",
                "OK",
                f"Resolved/closed {resolved_count} bet(s) for {target_date} from terminal bet-token prices",
            )
            return

    # --- Gamma close state: every bracket closed and exactly one winner ---
    gamma_resolved = _resolve_via_gamma_close(
        conn, station_id, target_date, bets, actual_tmax,
    )
    if gamma_resolved > 0:
        _log_pipeline_health(
            conn, station_id, "resolution", "OK",
            f"Resolved {gamma_resolved} bet(s) for {target_date} from Polymarket Gamma close-state",
        )
        return

    # Not per bracket: one bracket can close before the event is final.
    # The WU fallback is manual only.

    msg = (
        f"No Polymarket terminal price at >= {RESOLUTION_PRICE_THRESHOLD:.3f} "
        f"or <= {RESOLUTION_LOSS_PRICE_THRESHOLD:.3f} for {target_date}"
    )
    _log_pipeline_health(conn, station_id, "resolution", "WARNING", msg)
    logger.debug("Resolution %s %s: %s", station_id, target_date, msg)
    return

def _resolve_via_gamma_close(
    conn: sqlite3.Connection,
    station_id: str,
    target_date: str,
    bets: list[sqlite3.Row],
    actual_tmax: float | None,
) -> int:
    """Settle bets once ``winning_bracket_from_gamma`` finds the final winner. Returns count."""
    from hightempbot.persistence.ledger import record_resolution

    winning = winning_bracket_from_gamma(
        station_id, date.fromisoformat(target_date), conn=conn
    )
    if winning is None:
        return 0

    winning_low = winning.get("bracket_low")
    winning_high = winning.get("bracket_high")
    winning_token = winning.get("token_id", "")
    winning_label = winning.get("bracket_label") or f"[{winning_low},{winning_high}]"
    resolved_price = float(winning.get("yes_price", 0.0))

    logger.info(
        "Resolution %s %s via Gamma closed: winning bracket %s (yes=%.3f)",
        station_id, target_date, winning_label, resolved_price,
    )

    resolved_count = 0
    for bet_row in bets:
        side = bet_row["side"]
        fill_price = _effective_fill_price(bet_row)
        bet_size = bet_row["bet_size"]
        bet_token = bet_row["token_id"]

        detail = decode_event_detail(bet_row["event_detail"])
        bet_low = detail.get("bracket_low")
        bet_high = detail.get("bracket_high")

        if bet_low is not None or bet_high is not None:
            bet_is_winner = _bet_matches_winner(
                bet_low, bet_high, winning_low, winning_high,
                station_id=station_id, bet_id=bet_row["id"],
            )
        else:
            bet_is_winner = (bet_token == winning_token)

        outcome = _outcome_from_resolved_bracket(side, bet_is_winner)
        won = outcome == "WIN"
        extra_detail = _resolved_bracket_detail(winning)
        # A PUSH row shouldn't display the winning label.
        push_resolution_label: str | None = winning_label
        if not (fill_price > 0):
            outcome, pnl, push_extra = _apply_null_fill_push(
                conn, bet_row["id"], station_id,
                context_label="gamma_close_event",
                won=won,
            )
            extra_detail = {**extra_detail, **push_extra}
            push_resolution_label = None
        else:
            pnl = _bet_pnl(fill_price, bet_size, won)

        if not record_resolution(
            conn, bet_row["id"], actual_tmax, outcome, pnl,
            resolution_label=push_resolution_label,
            resolution_source="polymarket_gamma_closed",
            resolution_price=resolved_price,
            extra_detail=extra_detail,
        ):
            continue
        resolved_count += 1
        logger.info(
            "Resolved %s bet_id=%d via Gamma close: %s pnl=$%.2f",
            station_id, bet_row["id"], outcome, pnl,
        )

    return resolved_count


def _resolve_via_wu_actual_fallback(
    conn: sqlite3.Connection,
    station_id: str,
    target_date: str,
    bets: list[sqlite3.Row],
    actual_tmax: float,
    station_cfg: StationConfig,
    *,
    days_past: int,
) -> int:
    """Settle bets from WU actuals once Polymarket has archived the event.

    Manual only (CLI and dashboard admin endpoint). Skips any bet without
    stored bounds or whose station unit is unknown. Writes
    ``resolution_source='wu_actual_fallback'``.
    """
    from hightempbot.decision.brackets import actual_in_bracket
    from hightempbot.persistence.ledger import record_resolution

    if not station_cfg.unit:
        logger.warning(
            "WU fallback: station %s has empty unit — refusing to resolve %d bet(s) for %s",
            station_id, len(bets), target_date,
        )
        return 0

    # Don't round: the bounds already encode Polymarket's rounding, and
    # banker's rounding would flip .5 cases.
    actual_display = (
        celsius_to_fahrenheit(actual_tmax) if station_cfg.unit.upper() == "F" else actual_tmax
    )

    resolved_count = 0
    for bet_row in bets:
        try:
            if _resolve_wu_fallback_bet(
                conn, bet_row, actual_tmax, actual_display,
                station_id, station_cfg, days_past, actual_in_bracket,
                record_resolution,
            ):
                resolved_count += 1
        except Exception:
            # One bad row must not block the rest.
            logger.exception(
                "WU fallback: bet_id=%s (%s, %s) raised; skipping (other bets continue)",
                bet_row["id"], station_id, target_date,
            )

    return resolved_count


def wu_fallback_actual_display(
    actual_tmax: float, station_cfg: StationConfig,
) -> float:
    """Convert °C WU actual to the station's display unit without rounding."""
    return (
        celsius_to_fahrenheit(actual_tmax) if station_cfg.unit.upper() == "F" else actual_tmax
    )


def preview_wu_fallback_outcome(
    bet_row: sqlite3.Row,
    actual_tmax: float,
    station_cfg: StationConfig,
    days_past: int,
) -> dict:
    """Read-only preview of what the WU fallback would write for one bet
    (outcome WIN/LOSS/PUSH/SKIP, pnl_gross, reason, ...)."""
    from hightempbot.decision.brackets import actual_in_bracket

    if not station_cfg.unit:
        return {
            "bet_id": int(bet_row["id"]),
            "outcome": "SKIP",
            "reason": "station_cfg.unit is empty",
            "days_past": days_past,
        }

    detail = decode_event_detail(bet_row["event_detail"])

    bet_low = detail.get("bracket_low")
    bet_high = detail.get("bracket_high")
    side = (bet_row["side"] or "").upper()

    if bet_low is None and bet_high is None:
        return {
            "bet_id": int(bet_row["id"]),
            "side": side,
            "outcome": "SKIP",
            "reason": "no bracket bounds (token-only matching unsafe under relisting)",
            "days_past": days_past,
        }

    actual_display = wu_fallback_actual_display(actual_tmax, station_cfg)
    cont_low, cont_high = _continuous_bracket_bounds(bet_low, bet_high)
    bracket_won = actual_in_bracket(actual_display, cont_low, cont_high)
    won = (not bracket_won) if side == "NO" else bracket_won

    fill_price = _effective_fill_price(bet_row)
    bet_size = float(bet_row["bet_size"])

    if not (fill_price > 0):
        outcome = "PUSH"
        pnl_gross = 0.0
        reason = "null_fill_price → PUSH downgrade"
    else:
        outcome = "WIN" if won else "LOSS"
        pnl_gross = _bet_pnl(fill_price, bet_size, won)
        reason = "WU actuals fallback"

    return {
        "bet_id": int(bet_row["id"]),
        "side": side,
        "bracket_low": bet_low,
        "bracket_high": bet_high,
        "actual_display": actual_display,
        "actual_unit": station_cfg.unit,
        "outcome": outcome,
        "pnl_gross": float(pnl_gross),
        "reason": reason,
        "days_past": days_past,
    }


def _continuous_bracket_bounds(
    bet_low: float | None, bet_high: float | None,
) -> tuple[float | None, float | None]:
    """Convert legacy integer bounds to ``[lo-0.5, hi+0.5)``; others unchanged."""
    if bet_low is None or bet_high is None:
        return bet_low, bet_high

    if _is_legacy_integer_bracket(bet_low, bet_high):
        return bet_low - 0.5, bet_high + 0.5

    return bet_low, bet_high


def _resolve_wu_fallback_bet(
    conn: sqlite3.Connection,
    bet_row: sqlite3.Row,
    actual_tmax: float,
    actual_display: float,
    station_id: str,
    station_cfg: StationConfig,
    days_past: int,
    actual_in_bracket_fn,
    record_resolution_fn,
) -> bool:
    """Resolve one bet against the WU actual. Returns True iff a write was made."""
    side = bet_row["side"]
    fill_price = _effective_fill_price(bet_row)
    bet_size = bet_row["bet_size"]

    detail = decode_event_detail(bet_row["event_detail"])

    bet_low = detail.get("bracket_low")
    bet_high = detail.get("bracket_high")
    bet_label = (
        bet_row["bracket_label"]
        if "bracket_label" in bet_row.keys()
        else detail.get("bracket_label")
    )

    if bet_low is None and bet_high is None:
        logger.warning(
            "WU fallback: bet_id=%s (%s) has no bracket bounds (label=%r) — "
            "refusing to resolve via fallback (token-only matching unsafe under relisting)",
            bet_row["id"], station_id, bet_label,
        )
        return False

    cont_low, cont_high = _continuous_bracket_bounds(bet_low, bet_high)
    if (cont_low, cont_high) != (bet_low, bet_high):
        logger.info(
            "WU fallback: legacy-format compat for bet_id=%s (%s) bounds=(%s,%s) "
            "→ continuous (%s,%s)",
            bet_row["id"], station_id, bet_low, bet_high, cont_low, cont_high,
        )

    bracket_won = actual_in_bracket_fn(actual_display, cont_low, cont_high)
    won = (not bracket_won) if side.upper() == "NO" else bracket_won
    outcome = "WIN" if won else "LOSS"

    bracket_label = bet_label or f"[{bet_low},{bet_high})"
    extra_detail = {
        "fallback_reason": f"polymarket_unavailable_{days_past}d",
        "actual_display": actual_display,
        "actual_unit": station_cfg.unit,
        "wu_fallback_bracket_label": bracket_label,
    }
    resolution_label = bracket_label if bracket_won else None

    if not (fill_price > 0):
        outcome, pnl, push_extra = _apply_null_fill_push(
            conn, bet_row["id"], station_id,
            context_label="wu_actual_fallback",
            won=won,
        )
        extra_detail = {**extra_detail, **push_extra}
        resolution_label = None
    else:
        pnl = _bet_pnl(fill_price, bet_size, won)

    wrote = record_resolution_fn(
        conn, bet_row["id"], actual_tmax, outcome, pnl,
        resolution_label=resolution_label,
        resolution_source="wu_actual_fallback",
        resolution_price=None,
        extra_detail=extra_detail,
    )
    if wrote:
        logger.info(
            "Resolved %s bet_id=%d via WU fallback: %s bracket=[%s,%s) "
            "actual=%.2f°%s pnl=$%.2f (Polymarket unavailable %dd)",
            station_id, bet_row["id"], outcome, bet_low, bet_high,
            actual_display, station_cfg.unit, pnl, days_past,
        )
    return bool(wrote)


