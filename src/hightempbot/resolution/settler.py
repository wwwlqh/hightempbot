"""Per-station resolution tick + helpers — extracted from station_scanner.py (U10),
moved into the resolution/ package in U14.

Settles open positions from finalized Polymarket Gamma close-state. Runs
every 10 min (`SCAN_INTERVAL_MINUTES` cron slots), 24/7, for
market-day-or-older pending positions; gated against
`RESOLUTION_SCAN_START_HOUR` for same-local-day positions so resolution
waits until the bracket can plausibly call.

Imports (post-U11/U14):
- Shared helpers `_notify`, `_log_pipeline_health`, `_station_actual_display`,
  `_actual_matches_bracket` from `scheduler/station_scanner.py`.
- `_fetch_market_data` from `scheduler/market_data.py`.
- Gamma helpers `fetch_gamma_resolution_markets`, `parse_bracket_bounds`,
  `winning_bracket_from_gamma` from `resolution/gamma.py`.

station_scanner does NOT back-import from here — external callers (jobs.py,
tests) reach `run_resolution_tick` / `_resolve_station_date` directly via
this module.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytz

from hightempbot.db.connection import get_connection
from hightempbot.decision.brackets import actual_in_bracket
from hightempbot.persistence.actuals import actual_source_clause
from hightempbot.persistence.ledger import decode_event_detail
from hightempbot.resolution.gamma import (
    fetch_gamma_resolution_markets,
    parse_bracket_bounds,
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
    supports_live_resolution_source,
)

logger = logging.getLogger(__name__)


def _bet_pnl(fill_price: float, bet_size: float, won: bool) -> float:
    return ((1.0 / fill_price) - 1.0) * float(bet_size) if won else -float(bet_size)


def run_resolution_tick(
    station: StationConfig,
    db_path: str,
) -> None:
    """Check if any open positions for this station have resolved.

    Polymarket is the single source of truth for WIN/LOSS. In production the
    tick's only automatic settlement path is Gamma close-state: a market
    settles once Polymarket reports every bracket ``closed`` with final
    ``outcomePrices`` (``resolution_source='polymarket_gamma_closed'``, see
    ``_resolve_via_gamma_close``). The legacy CLOB threshold scan (best bid
    >= 0.995 win / best ask <= 0.005 loss) sits behind
    ``EARLY_RESOLUTION_ENABLED``, hardcoded False since 2026-05-22 — a
    deliberate flag-not-delete decision so tests can patch it back on to
    exercise the early-settlement paths. Rows with no Gamma close yet stay
    PENDING; the WU-actuals fallback is operator-initiated only
    (CLI/dashboard) and is never called from this tick.
    """
    station_id = station.icao
    tz = pytz.timezone(station.timezone)
    local_now = datetime.now(tz)

    conn = get_connection(db_path)
    try:
        # Log every scan so dashboard Last Scan stays fresh
        from hightempbot.db.connection import log_pipeline_health
        log_pipeline_health(
            conn, station_id, "scan", "OK",
            f"resolution tick {local_now.strftime('%H:%M')} local",
        )

        # Find open positions for this station (both live and dry-run)
        pending = conn.execute(
            """SELECT id, station_id, target_date, threshold, side, token_id, bet_size,
                      fill_price, fill_size, limit_price, event_type, event_detail
            FROM ledger
            WHERE station_id = ? AND outcome = 'PENDING'
            AND event_type IN ('bet', 'dry_run')""",
            (station_id,),
        ).fetchall()

        if pending:
            # Group by target_date — fetch market data once per event, not per bet
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
    """Return True for the legacy integer-label bracket heuristic.

    Legacy rows wrote raw integer labels: a 1°C bracket as ``lo == hi`` and a
    2°F bracket as ``hi - lo == 1``; both bounds must be integer-ish. Shared by
    ``_bet_matches_winner`` and ``_continuous_bracket_bounds`` so the two stay
    in lockstep — preserves the exact epsilon and None handling.
    """
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
    """Match a bet's stored bracket bounds against the winning bracket.

    Handles post-fix bets (exact equality on continuous [lo, hi)) and legacy
    rows written under the old integer-label semantics (lo == hi for 1°C
    brackets, or hi - lo == 1 for 2°F brackets). Legacy compat is removed
    once `legacy-format compat` stops firing in production logs.
    """
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
    """Return whether a ledger bet belongs to a Polymarket bracket row.

    When ``bet_low``/``bet_high`` are both NULL (legacy rows) we first try
    to recover the bounds by parsing ``bet_label``. Only when no bounds can
    be recovered do we fall through to ``bet_token``-equality matching, and
    we warn about it because Polymarket relisting (new conditionId) yields a
    different token for the same bracket label and a token-only match would
    silently strand or mis-resolve the row.
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
        # Lazy import: resolution.gamma imports stations, so bare-import
        # at module top would create an import cycle.
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
    """Return whether the bet won from a terminal YES-token price.

    A bracket YES token at 0.995 means the bracket won; at 0.005 means the
    bracket lost. NO bets are the opposite side of the same bracket.
    """
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


def _closed_bracket_detail(market: dict) -> dict:
    return _bracket_detail(market, prefix="closed")


def _effective_fill_price(bet_row) -> float:
    """``fill_price`` with limit_price fallback and NULL/zero coercion to 0.0.

    Centralises the `bet_row["fill_price"] or bet_row["limit_price"] or 0.0`
    expression so the 5 resolution sites (winning bracket, terminal yes,
    terminal token, gamma close event-level, gamma close per-bracket) cannot
    drift. Treats any non-positive / non-finite value as 0.0 — the caller
    routes that through `_apply_null_fill_push`.

    Python truthiness skips 0.0 (falsy) but NOT NaN (truthy), so the naive
    `a or b or 0.0` pattern would let a NaN fill_price through silently.
    Coerce explicitly via math.isfinite + > 0 to honor the docstring.
    """
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
    """Return ``(outcome, pnl, extra_detail_addition)`` for a NULL/zero ``fill_price`` bet.

    The bet is always booked as PUSH with pnl=0 — without ``fill_price`` we cannot
    quantify either a win payout or a loss — but the bracket's actual resolution
    (``won``) is recorded in ``event_detail`` so dashboards / audit scripts can
    surface "PUSHed for accounting, but the bracket truly resolved LOSS" rows.

    Side-effects: emits CRITICAL log and writes a ``pipeline_health`` ERROR row
    (stage = ``resolution_<context_label>``) so operators can SQL-query for the
    condition. The returned dict carries:

    - ``null_fill_price_push = True``: marks the downgrade in ``event_detail``.
    - ``bracket_resolution``: "WIN" or "LOSS" — the bracket's real outcome.
    - ``unrecorded_loss = True``: set when ``won is False`` so an operator can
      `WHERE event_detail LIKE '%"unrecorded_loss": true%'` to enumerate
      capital that was lost without entry-price audit trail.
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
    # Loss requires an executable ask at the floor — a missing ask with a
    # low bid is an information void, not proof the token is dead.
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

    Production path: ``_resolve_via_gamma_close`` — settle only when
    Polymarket's Gamma API reports the whole event closed with final
    ``outcomePrices`` (``resolution_source='polymarket_gamma_closed'``). When
    Gamma has no close-state yet, the group stays PENDING and a WARNING is
    logged; nothing else fires automatically from this function.

    The CLOB threshold blocks above the Gamma call (winning bracket at bid
    >= 0.995, per-bracket terminal YES price, terminal bet-token price — the
    ``polymarket_winner`` / ``polymarket_terminal_yes`` /
    ``polymarket_terminal_token`` sources) run only when
    ``EARLY_RESOLUTION_ENABLED`` is True. That flag has been hardcoded False
    since 2026-05-22 (deliberate flag-not-delete: tests patch it to exercise
    the legacy early-settlement scan). ``_resolve_via_wu_actual_fallback`` is
    NOT called from here — WU settlement is operator-initiated only
    (CLI/dashboard).
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

    # Defense-in-depth: the writer-side gate (stations.SUPPORTED_LIVE_SOURCES)
    # already restricts which sources can land in `actuals` for live-betting
    # stations, but the resolution path mints `wu_actual_fallback` rows that
    # claim WU provenance — refuse any actuals row whose source isn't in the
    # supported set so a future ingestion change can't silently corrupt the
    # fallback's audit trail (finding #25).
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
                # Loss confirmation needs an executable ask at the floor; a low
                # bid alone with no asks is a quote void, not a dead bracket.
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
                # Recover bounds from the stored label so a Polymarket relist
                # (new conditionId, same label) still resolves correctly.
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
                # No price ever recorded (NULL/zero/NaN); treat as PUSH so the
                # row doesn't silently book as a -bet_size loss on a winning bet.
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

    # --- Gamma close-state fallback ---
    # CLOB threshold detection misses bets when a market closes with an empty
    # order book. Polymarket's Gamma API exposes the authoritative resolution
    # via `closed: True` + `outcomePrices` once the market is finalized; settle
    # on that when every bracket in the event is closed and exactly one shows
    # a definitive YES price (>= 0.99).
    gamma_resolved = _resolve_via_gamma_close(
        conn, station_id, target_date, bets, actual_tmax,
    )
    if gamma_resolved > 0:
        _log_pipeline_health(
            conn, station_id, "resolution", "OK",
            f"Resolved {gamma_resolved} bet(s) for {target_date} from Polymarket Gamma close-state",
        )
        return

    # Per-bracket Gamma close-state is intentionally not used for production
    # settlement. A single bracket can show closed=True/outcomePrices while
    # the wallet position is still open and the full event is not final, so
    # those rows must remain PENDING until the event-level all-closed path
    # above can pin the final Polymarket result.

    # Automatic WU fallback is intentionally disabled in production. Operators
    # want ledger rows to remain PENDING until Polymarket finalizes the event.
    # The WU helper remains available only through explicit CLI/dashboard
    # manual workflows for archived events.
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
    """Settle bets using Polymarket's authoritative ``closed`` + ``outcomePrices``.

    Delegates the gating (all closed, single winner, parseable bounds) to
    ``winning_bracket_from_gamma``. Returns the count of bets settled.
    """
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
        # NULL/zero fill_price means we never recorded what the bet filled
        # at — applying `-bet_size` would book a fabricated loss even when
        # `won is True`. Mirror the per-bracket variant: downgrade to PUSH
        # so dashboard/capital accounting stays honest, and log CRITICAL so
        # the row gets manual attention.
        extra_detail = _resolved_bracket_detail(winning)
        # Default to the winning bracket label, but null it out for the PUSH
        # downgrade so the dashboard doesn't show "won at X" for a row whose
        # outcome is actually PUSH (event_detail.bracket_resolution still
        # carries the canonical "what would have happened").
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


def _resolve_via_gamma_close_per_bracket(
    conn: sqlite3.Connection,
    station_id: str,
    target_date: str,
    bets: list[sqlite3.Row],
    actual_tmax: float | None,
) -> int:
    """Legacy helper: settle each bet against its own bracket's Gamma close-state.

    Status: zero in-repo callers (not wired into ``_resolve_station_date``,
    any CLI, or any test). Retained deliberately as the reference
    implementation for the historical
    ``resolution_source='polymarket_gamma_closed_bracket'`` ledger rows it
    once wrote, so their settlement semantics stay auditable. Production
    rejected this path: a single bracket can show ``closed``/``outcomePrices``
    while the event is not final, so per-bracket settlement could book results
    before the winning bracket is pinned (see the comment in
    ``_resolve_station_date``).

    What it does: ``_resolve_via_gamma_close`` requires every bracket in the
    event to be closed before firing, so it cannot settle individually closed
    losing brackets while the precise winning bracket is still being pinned
    (the typical pattern when intraday actuals shoot past several lower
    brackets hours before the daily high lands). This per-bracket variant
    settles each bet against its own bracket's authoritative ``closed`` +
    ``outcomePrices`` regardless of what other brackets are doing.

    Skips bets whose bracket is still open or whose outcomePrices haven't
    landed at a corner.
    """
    from hightempbot.execution.strategy_constants import RESOLUTION_PRICE_THRESHOLD
    from hightempbot.persistence.ledger import record_resolution

    markets = fetch_gamma_resolution_markets(
        station_id, date.fromisoformat(target_date), conn=conn
    )
    if not markets:
        return 0

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

        bet_market = next(
            (
                m for m in markets.values()
                if _bet_matches_market(
                    bet_low, bet_high, m, bet_token,
                    station_id=station_id, bet_id=bet_row["id"],
                    bet_label=bet_label,
                )
            ),
            None,
        )
        if bet_market is None or not bet_market.get("closed"):
            continue

        yes_price = float(bet_market.get("yes_price", 0.0))
        no_price = float(bet_market.get("no_price", 0.0))

        if yes_price >= RESOLUTION_PRICE_THRESHOLD:
            bracket_won = True
            resolved_price = yes_price
        elif no_price >= RESOLUTION_PRICE_THRESHOLD:
            bracket_won = False
            resolved_price = no_price
        else:
            # Closed but outcomePrices haven't landed at a corner yet.
            continue

        outcome = _outcome_from_resolved_bracket(side, bracket_won)
        won = outcome == "WIN"
        bracket_label = bet_market.get("bracket_label") or (
            f"[{bet_market.get('bracket_low')},{bet_market.get('bracket_high')}]"
        )
        extra_detail = {
            "closed_bracket_label": bracket_label,
            **_closed_bracket_detail(bet_market),
        }
        resolution_label = bracket_label if bracket_won else None
        if bracket_won:
            extra_detail.update(_resolved_bracket_detail(bet_market))

        if not (fill_price > 0):
            outcome, pnl, push_extra = _apply_null_fill_push(
                conn, bet_row["id"], station_id,
                context_label="gamma_close_per_bracket",
                won=won,
            )
            extra_detail = {**extra_detail, **push_extra}
            # PUSH downgrade — null out the winning-bracket label so the
            # dashboard doesn't show "won at X" for a PUSH row.
            resolution_label = None
        else:
            pnl = _bet_pnl(fill_price, bet_size, won)

        if not record_resolution(
            conn, bet_row["id"], actual_tmax, outcome, pnl,
            resolution_label=resolution_label,
            resolution_source="polymarket_gamma_closed_bracket",
            resolution_price=resolved_price,
            extra_detail=extra_detail,
        ):
            continue
        resolved_count += 1
        logger.info(
            "Resolved %s bet_id=%d via Gamma per-bracket close: "
            "%s bracket=%s yes=%.3f no=%.3f pnl=$%.2f",
            station_id, bet_row["id"], outcome, bracket_label,
            yes_price, no_price, pnl,
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
    """Settle bets using local WU actuals when Polymarket never will.

    NOT called by the automatic resolution tick: ``_resolve_station_date``
    deliberately leaves rows PENDING until Polymarket finalizes the event.
    The only callers are the operator surfaces — the
    ``hightempbot.cli.resolve_pending_via_wu`` CLI and the dashboard's
    ``POST /api/v2/admin/resolve-pending`` endpoint — both of which enforce
    the ``POLYMARKET_FALLBACK_DAYS`` floor (``--force`` to override).
    Purpose: Polymarket Gamma archives daily-temperature events some time
    after close — the slug + conditionId lookups both go empty (confirmed
    2026-05-20 against 5/17 + 5/18 events) — and without this manual escape
    hatch the affected PENDINGs would linger indefinitely.

    Safety constraints (any failure skips the row, never guesses):

    - Bet MUST carry explicit ``bracket_low`` or ``bracket_high`` in
      event_detail. Token-only matching is refused: Polymarket relisting can
      rebind a token to a different bracket, and falling back without bounds
      would risk silently resolving against the wrong slice.
    - ``station_cfg.unit`` must be set (`"F"` or `"C"`). The display-unit
      conversion is the only thing keeping a US-Fahrenheit station from being
      compared against °C bracket bounds; an unknown unit is treated as
      unresolvable. (`bracket_unit` empty-string semantics — see AGENTS.md
      "Things that have burned us".)
    - `actual_tmax` is in °C (the WU convention). Rounded to the station's
      display unit so the bracket compare matches Polymarket's resolution
      semantics (`[lo, hi)` round-rule).

    Records `resolution_source='wu_actual_fallback'` with
    `event_detail.fallback_reason='polymarket_unavailable_<days>d'` so the row
    is auditably distinct from the 5 polymarket_* sources and
    `backfill_polymarket_resolution_labels` leaves it alone.
    """
    from hightempbot.decision.brackets import actual_in_bracket
    from hightempbot.persistence.ledger import record_resolution

    if not station_cfg.unit:
        logger.warning(
            "WU fallback: station %s has empty unit — refusing to resolve %d bet(s) for %s",
            station_id, len(bets), target_date,
        )
        return 0

    # Compare the raw display value against the bracket — do NOT round to the
    # nearest integer first. Python's banker's rounding (`round(68.5) == 68`)
    # combined with continuous half-open bracket bounds (`[67.5, 68.5)` for
    # "be 68°F", `[68.5, 69.5)` for "be 69°F") would silently flip the .5
    # boundary case. Polymarket resolves against the raw NWS observation; the
    # bracket parser already encodes the round-rule into the bounds (label X
    # ↔ actual in `[X-0.5, X+0.5)`). See `resolution/gamma.py::parse_bracket_bounds`.
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
            # Per-bet failure must not abort remaining bets in the group: a
            # corrupted event_detail or transient DB error on one row should
            # not strand every later bet on the same (station, target_date).
            # Next tick will retry the unresolved row.
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
    """Predict what `_resolve_via_wu_actual_fallback` would write for one bet.

    Pure read-only — no DB writes. Shared between the operator script's
    dry-run preview and the dashboard's mutation endpoint so the two can't
    drift from production (finding #4 + #24).

    Returns a dict with at minimum:
      ``bet_id``, ``side``, ``bracket_low``, ``bracket_high``,
      ``actual_display``, ``actual_unit``, ``outcome``
      (``WIN``/``LOSS``/``PUSH``/``SKIP``), ``pnl_gross``, ``reason``,
      ``days_past``.
    """
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
    """Convert legacy integer bracket bounds to the continuous half-open form.

    Post-fix brackets are stored as continuous bounds (label X ↔ `[X-0.5, X+0.5)`).
    Legacy rows wrote raw integer labels:
    - 1°C bracket label `70` → ``lo == hi == 70`` → continuous ``[69.5, 70.5)``
    - 2°F bracket label `70-71` → ``lo=70, hi=71`` → continuous ``[69.5, 71.5)``
    Detected by integer-ish bounds with ``lo == hi`` or ``hi - lo == 1``. Returns
    the bounds unchanged for post-fix continuous rows.

    Mirrors the legacy heuristics in ``_bet_matches_winner`` so the WU fallback
    matches the rest of the resolver. See feedback memory
    ``bracket_parser_dominates_parity`` (2026-05-09).
    """
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


