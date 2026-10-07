"""Per-station betting tick — extracted from station_scanner.py (U9).

Runs every 15 min once the latest complete previous-day ensemble is ready.
Pulls market+CLOB data, fetches the ensemble forecast, and hands off to
`execution.pipeline.run_betting_cycle` to evaluate strategies and place
orders.

Imports (post-U11):
- Shared helpers `_target_date_for_ready_cycle`, `_last_run_ensemble_ready`,
  `_notify` from `station_scanner.py`.
- Data helpers `_fetch_market_data`, `_fetch_ensemble` from `market_data.py`.
- Healing helpers `_attempt_seed_missing_lut`, `_heal_station_unit_if_wrong`
  from `station_healing.py`.

Preserve the `betting_tick.<symbol>` module-level bindings — the test suite
patches against these names rather than reaching through to the origin module.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime

import pytz

from hightempbot.db.connection import get_connection, utc_now_sql
from hightempbot.execution.strategy_constants import MAX_PER_MARKET
from hightempbot.persistence.actuals import actual_source_clause
from hightempbot.scheduler.market_data import _fetch_ensemble, _fetch_market_data
from hightempbot.scheduler.station_healing import (
    _attempt_seed_missing_lut,
    _heal_station_unit_if_wrong,
)
from hightempbot.scheduler.station_scanner import (
    _last_run_ensemble_ready,
    _notify,
    _target_date_for_ready_cycle,
)
from hightempbot.stations import (
    StationConfig,
    supports_live_resolution_source,
)

logger = logging.getLogger(__name__)


def _top_ask(book) -> tuple[float | None, float | None]:
    """Return the best (cheapest) ask ``(price, size)`` from a CLOB book.

    Parses defensively: the attached ``_yes_book`` / ``_no_book`` dicts may be
    None, empty, or malformed. Returns ``(None, None)`` on anything that isn't
    a well-formed ask ladder. Never raises.
    """
    try:
        if not isinstance(book, dict):
            return (None, None)
        asks = book.get("asks")
        if not asks:
            return (None, None)
        best_price: float | None = None
        best_size: float | None = None
        for level in asks:
            if isinstance(level, dict):
                price = level.get("price")
                size = level.get("size")
            else:
                price = getattr(level, "price", None)
                size = getattr(level, "size", None)
            if price is None:
                continue
            price_f = float(price)
            if best_price is None or price_f < best_price:
                best_price = price_f
                best_size = float(size) if size is not None else None
        return (best_price, best_size)
    except (TypeError, ValueError):
        return (None, None)


def _persist_book_snapshots(conn, station_id, target_date_iso, mdata, log_health) -> int:
    """Persist one top-of-book snapshot row per bracket for this tick.

    Best-effort forensic record of the exact CLOB state the tick acted on
    (needed for calibration refits + live/backtest parity). NEVER raises: a
    snapshot failure must not break the betting tick. On failure it logs a
    single pipeline_health row for the whole tick. Returns rows written.
    """
    try:
        snapped_at = utc_now_sql()
        rows: list[tuple] = []
        for bi, mkt in mdata.items():
            if not isinstance(mkt, dict):
                continue
            yes_ask_p, yes_ask_s = _top_ask(mkt.get("_yes_book"))
            no_ask_p, no_ask_s = _top_ask(mkt.get("_no_book"))
            rows.append((
                snapped_at,
                station_id,
                target_date_iso,
                int(bi),
                mkt.get("bracket_label"),
                mkt.get("token_id"),
                mkt.get("no_token_id"),
                mkt.get("best_ask"),
                mkt.get("best_bid"),
                yes_ask_p,
                yes_ask_s,
                no_ask_p,
                no_ask_s,
                mkt.get("volume24hr"),
            ))
        if not rows:
            return 0
        conn.executemany(
            "INSERT INTO book_snapshots "
            "(snapped_at, station_id, target_date, bracket_idx, bracket_label, "
            "token_id, no_token_id, best_ask, best_bid, "
            "yes_top_ask_price, yes_top_ask_size, no_top_ask_price, no_top_ask_size, "
            "volume24hr) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return len(rows)
    except Exception:
        logger.warning(
            "Book snapshot persist failed for %s %s",
            station_id, target_date_iso, exc_info=True,
        )
        try:
            log_health("book_snapshot", "WARNING", "snapshot persist failed")
        except Exception:
            pass
        return 0


def run_betting_tick(
    station: StationConfig,
    db_path: str,
    initial_bankroll: float,
    dry_run: bool,
    data_dir: str = "data",
) -> None:
    """Execute one 15-min betting tick for a station.

    Targets the current UTC market date after the UTC readiness gate confirms
    every expected Open-Meteo model has published its latest usable run.
    The exact model set is checked again before scoring.

    Betting window: every 15 min until the station-local cutoff hour on the
    market's target local date.

    Opens its own DB connection (WAL mode, thread-safe).
    """
    station_id = station.icao
    tz = pytz.timezone(station.timezone)
    local_now = datetime.now(tz)

    # Stagger API calls: random 0-30s delay to avoid OM "too many concurrent requests"
    time.sleep(random.uniform(0, 30))

    logger.info("Betting tick %s: %s local, fetching data...", station_id, local_now.strftime("%H:%M"))

    conn = get_connection(db_path)
    try:
        from hightempbot.db.connection import log_pipeline_health

        def _log_health(stage, status, msg):
            """Log to pipeline_health for dashboard visibility."""
            log_pipeline_health(conn, station_id, stage, status, msg)

        # Log scan so dashboard Last Scan stays fresh
        _log_health("scan", "OK", f"betting tick {local_now.strftime('%H:%M')} local")

        try:
            from hightempbot.execution.operator_control import processing_block_reason

            block_reason = processing_block_reason(conn, dry_run=dry_run)
        except Exception:
            block_reason = None if dry_run else "operator-control gate unavailable"
            logger.warning("Operator-control gate failed for %s", station_id, exc_info=True)
        if block_reason:
            _log_health("operator", "SKIP", block_reason)
            logger.info("Station %s: %s", station_id, block_reason)
            return

        now_utc = datetime.now(pytz.utc)
        target_date = _target_date_for_ready_cycle(now_utc, conn=conn)
        if target_date is None:
            _log_health(
                "gates",
                "SKIP",
                "No ensemble released yet; skipping until forecast_archive populated",
            )
            logger.debug("Station %s: no ensemble released, skipping", station_id)
            return
        target_date_iso = target_date.isoformat()
        local_date = local_now.date()
        # Trading window per station = local calendar day of target_date
        # (00:00 to 24:00). Strict equality closes both ends — before the
        # window opens (local_date < target_date) AND after it closes
        # (local_date > target_date). This replaces the old one-sided gate
        # which left east-of-UTC stations bettable for the WRONG target_date.
        if local_date != target_date:
            relation = "before" if local_date < target_date else "past"
            _log_health(
                "gates",
                "SKIP",
                (
                    f"Outside trading window: target {target_date_iso}, "
                    f"local {local_date.isoformat()} ({relation})"
                ),
            )
            logger.debug(
                "Station %s: target %s vs local %s (%s window), skipping",
                station_id,
                target_date_iso,
                local_date.isoformat(),
                relation,
            )
            return

        from hightempbot.execution.strategy_constants import BETTING_LOCAL_CUTOFF_HOUR, STRATEGY_CONFIGS
        # BETTING_LOCAL_CUTOFF_HOUR == 0 means "no cutoff" — WU consensus gate
        # provides per-bet freshness safety, so the coarse time cutoff is opt-in.
        if (
            BETTING_LOCAL_CUTOFF_HOUR > 0
            and local_date == target_date
            and local_now.hour >= BETTING_LOCAL_CUTOFF_HOUR
        ):
            msg = (
                f"Betting cutoff reached: {local_now.strftime('%H:%M')} local "
                f"for target {target_date_iso}; no new same-day bets after "
                f"{BETTING_LOCAL_CUTOFF_HOUR:02d}:00 local"
            )
            _log_health("gates", "SKIP", msg)
            logger.debug("Station %s: %s", station_id, msg)
            return

        active_entry_hours = set().union(
            *(
                cfg.entry_hour_set
                for cfg in STRATEGY_CONFIGS.values()
                if cfg.enabled
            )
        )
        if active_entry_hours and local_now.hour not in active_entry_hours:
            msg = (
                f"No enabled strategy entry window at {local_now.strftime('%H:%M')} local "
                f"for target {target_date_iso}; skipping market/CLOB fetch"
            )
            _log_health("gates", "SKIP", msg)
            logger.debug("Station %s: %s", station_id, msg)
            return

        # --- Latest complete N -> N+1 readiness gate ---
        # Starting at 00Z on UTC date N+1, the bot polls Open-Meteo metadata
        # until every model's latest usable UTC-day-N run is available.
        # Once ready, the ensemble is fetched once for the N+1 target date
        # and locked for the rest of the UTC cycle. Before
        # readiness, the tick skips (no ensemble fetch, no market eval, no
        # bet) and logs which model is still missing.
        # Market price polling (15-min cadence) resumes after readiness;
        # the locked ensemble is reused all day until the next UTC midnight
        # resets the cycle.
        ready, missing = _last_run_ensemble_ready(now_utc)
        if not ready:
            _log_health(
                "gates",
                "SKIP",
                f"Ensemble not yet recorded ({now_utc.strftime('%H:%M')} UTC); waiting on {missing}",
            )
            logger.debug(
                "Station %s: skipping tick — latest N missing for: %s",
                station_id, missing,
            )
            return

        # --- Source gate ---
        if not supports_live_resolution_source(station.resolution_source):
            _log_health(
                "gates",
                "SKIP",
                f"Source {station.resolution_source} unsupported for live betting",
            )
            logger.debug("Station %s: source %s unsupported for betting", station_id, station.resolution_source)
            return

        # --- Early gates: skip before any API calls ---
        # 0. Coverage gate (cheap DB query — skip stations below MIN_COVERAGE_PCT coverage)
        from hightempbot.execution.strategy_constants import (
            LUT_STALE_HOURS, MIN_COVERAGE_PCT, REF_START_DATE,
        )
        from datetime import (
            date as _date_cls,
            datetime as _datetime_cls,
            timezone as _tz_cls,
        )
        ref_start = _date_cls.fromisoformat(REF_START_DATE)
        expected_days = (local_now.date() - ref_start).days + 1
        actuals_clause, actuals_params = actual_source_clause()
        if expected_days > 0:
            actuals_count = conn.execute(
                "SELECT COUNT(*) as n FROM actuals "
                "WHERE station_id = ? AND local_date >= ? "
                f"AND {actuals_clause}",
                (station_id, REF_START_DATE, *actuals_params),
            ).fetchone()["n"]
            coverage = actuals_count / expected_days
            if coverage < MIN_COVERAGE_PCT:
                _log_health(
                    "gates",
                    "SKIP",
                    f"Coverage {coverage:.1%} below {MIN_COVERAGE_PCT:.0%}",
                )
                logger.debug("Station %s: coverage %.1f%% < %.0f%%, skipping tick",
                             station_id, coverage * 100, MIN_COVERAGE_PCT * 100)
                return

        # 1. Stale-LUT gate — fires when lut_bucket_stats is empty or too
        # old. Gate cascade is coverage → stale_lut → max_per_market; the
        # old BSS and DAILY_MAX_BETS gates were retired in the Phase F
        # teardown (2026-04-22).
        lut_row = conn.execute(
            "SELECT MAX(refreshed_at) AS last FROM lut_bucket_stats WHERE station_id = ?",
            (station_id,),
        ).fetchone()
        last_refreshed = lut_row["last"] if lut_row else None
        if last_refreshed is None:
            if _attempt_seed_missing_lut(conn, station, target_date):
                lut_row = conn.execute(
                    "SELECT MAX(refreshed_at) AS last FROM lut_bucket_stats WHERE station_id = ?",
                    (station_id,),
                ).fetchone()
                last_refreshed = lut_row["last"] if lut_row else None
            if last_refreshed is None:
                _log_health("gates", "SKIP", f"LUT not seeded for {station_id}")
                logger.debug("Station %s: LUT not seeded, skipping tick", station_id)
                return
        try:
            refreshed_dt = _datetime_cls.strptime(
                last_refreshed, "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=_tz_cls.utc)
            age_hours = (
                _datetime_cls.now(_tz_cls.utc) - refreshed_dt
            ).total_seconds() / 3600.0
        except Exception:
            age_hours = float("inf")
        if age_hours > LUT_STALE_HOURS:
            _log_health(
                "gates",
                "SKIP",
                f"LUT stale ({age_hours:.1f}h > {LUT_STALE_HOURS}h)",
            )
            logger.debug(
                "Station %s: LUT stale %.1fh > %dh, skipping tick",
                station_id, age_hours, LUT_STALE_HOURS,
            )
            return

        # --- Actuals freshness gate ---
        # `is_ready()` only checks `n_samples >= MIN_PAIRS`, which a
        # bulk-loaded historical import satisfies even when the live
        # scrape hasn't run in months. Stations with an unsupported
        # `resolution_source` (e.g., ncei/ims/cwa today) would otherwise
        # silently bet on calibration that has never been refreshed
        # against recent data. Fail closed when the freshest actual lags
        # the station's local "today" by more than ACTUALS_STALE_DAYS.
        # Runs after the LUT stale gate (cheaper query first) and before
        # any market/CLOB/ensemble fetch so blocked stations don't burn
        # rate-limit budget.
        from hightempbot.execution.strategy_constants import ACTUALS_STALE_DAYS
        latest_actual_row = conn.execute(
            "SELECT MAX(local_date) AS latest FROM actuals "
            "WHERE station_id = ? "
            f"AND {actuals_clause}",
            (station_id, *actuals_params),
        ).fetchone()
        latest_actual = latest_actual_row["latest"] if latest_actual_row else None
        if latest_actual is None:
            _log_health("gates", "SKIP", "No actuals on file")
            logger.info("Station %s: no actuals on file, skipping tick", station_id)
            return
        try:
            latest_actual_date = _date_cls.fromisoformat(latest_actual)
            actuals_age_days = (local_now.date() - latest_actual_date).days
        except Exception:
            actuals_age_days = 999
        if actuals_age_days > ACTUALS_STALE_DAYS:
            _log_health(
                "gates",
                "SKIP",
                (
                    f"Actuals stale ({actuals_age_days}d > "
                    f"{ACTUALS_STALE_DAYS}d, latest={latest_actual})"
                ),
            )
            logger.info(
                "Station %s: actuals stale %dd > %dd (latest=%s), skipping tick",
                station_id, actuals_age_days, ACTUALS_STALE_DAYS, latest_actual,
            )
            return

        # --- Market-already-resolved gate ---
        # Once the resolution tick records an event-level Polymarket Gamma
        # close for this (station, target_date), there is nothing left to do
        # this cycle: the winning bracket is pinned, no fresh bets can settle
        # before UTC midnight rotates target_date. Skip before any Gamma /
        # CLOB / ensemble fetch so a resolved market stops burning the
        # 15-min scrape budget for the rest of the local day.
        #
        # Intentional exclusions:
        # - outcome='PUSH' — accounting downgrade (NULL fill_price), the
        #   market itself may still be trading other brackets. (#1)
        # - polymarket_gamma_closed_bracket — one bracket closing (typically
        #   a loser when intraday actual passes it) does NOT pin the winner;
        #   the remaining brackets are still actively trading. (#2)
        # - wu_actual_fallback — Polymarket may still resolve later, and the
        #   bot should resume betting next day if the row is rolled back. (#23)
        # Mode-aware event_type filter mirrors line 507's MAX_PER_MARKET gate
        # so a stale dry_run row from yesterday's staging test doesn't block
        # today's live bets (and vice versa). (#11)
        gate_event_types = ("bet",) if not dry_run else ("dry_run",)
        gate_placeholders = ",".join("?" * len(gate_event_types))
        resolved_row = conn.execute(
            f"""SELECT 1 FROM ledger
            WHERE station_id = ?
            AND target_date = ?
            AND outcome IN ('WIN', 'LOSS')
            AND event_type IN ({gate_placeholders})
            AND json_extract(event_detail, '$.resolution_source') IN (
                'polymarket_gamma_closed'
            )
            LIMIT 1""",
            (station_id, target_date_iso, *gate_event_types),
        ).fetchone()
        if resolved_row is not None:
            _log_health(
                "gates",
                "SKIP",
                f"Market for {target_date_iso} already resolved on Polymarket",
            )
            logger.debug(
                "Station %s: market %s already resolved on Polymarket, skipping tick",
                station_id, target_date_iso,
            )
            return

        # --- Fetch market + CLOB for the readiness-cycle target date ---
        from hightempbot.execution.walker import ClobReader
        market_data = None
        candidate_dates = [target_date]

        for try_date in candidate_dates:
            try_date_iso = try_date.isoformat()

            mdata = _fetch_market_data(station_id, try_date, conn=conn)
            if not mdata:
                _log_health("market", "ERROR", f"No Polymarket data for {try_date_iso}")
                logger.info("Station %s: no market data for %s", station_id, try_date_iso)
                continue

            _log_health("market", "OK", f"{len(mdata)} brackets ({try_date_iso})")

            # Refresh volume24hr from Gamma API (DB cache doesn't store volume)
            from hightempbot.scheduler.market_data import refresh_market_volume
            refresh_market_volume(station_id, try_date, mdata, conn=conn)

            # CLOB enrichment — check for $0.99 walls.
            # Run all per-bracket /price + /book fetches concurrently against
            # the shared CLOB executor. Sequential calls would hold the
            # APScheduler thread for up to ~660s in worst case (22 calls x
            # 30s timeout) and miss misfire_grace_time on a slow CLOB.
            try:
                reader = ClobReader()
                clob_ok = 0
                wall_count = 0
                missing_books = 0

                tokens_to_fetch: list[tuple[int, str, str]] = []  # (bi, role, token)
                for bi, mkt in mdata.items():
                    yes_token = mkt.get("token_id", "")
                    no_token = mkt.get("no_token_id", "")
                    if yes_token:
                        tokens_to_fetch.append((bi, "yes", yes_token))
                    if no_token:
                        tokens_to_fetch.append((bi, "no", no_token))

                # Stage the enrichment so we never submit /price and /book
                # for every token at once. Each ClobReader call is already
                # timeout-bounded by the shared CLOB executor; this small
                # outer pool only gives modest parallelism without flooding
                # the shared executor with abandoned work during API slowness.
                price_results: dict[tuple[int, str], float | None] = {}
                book_results: dict[tuple[int, str], dict | None] = {}
                if tokens_to_fetch:
                    from concurrent.futures import (
                        ThreadPoolExecutor as _TPE,
                        as_completed as _as_completed,
                    )

                    def _price_call(item: tuple[int, str, str]):
                        bi, role, token = item
                        try:
                            return bi, role, reader.fetch_price(token, "buy")
                        except Exception:
                            logger.debug("CLOB price enrichment failed for %s", token, exc_info=True)
                            return bi, role, None

                    def _book_call(item: tuple[int, str, str]):
                        bi, role, token = item
                        try:
                            return bi, role, reader.fetch_order_book(token)
                        except Exception:
                            logger.debug("CLOB book enrichment failed for %s", token, exc_info=True)
                            return bi, role, None

                    _enrich_workers = min(len(tokens_to_fetch), 6)
                    with _TPE(max_workers=_enrich_workers, thread_name_prefix="enrich") as _ex:
                        price_futs = [_ex.submit(_price_call, item) for item in tokens_to_fetch]
                        for fut in _as_completed(price_futs):
                            bi, role, price = fut.result()
                            price_results[(bi, role)] = price

                    with _TPE(max_workers=_enrich_workers, thread_name_prefix="enrich") as _ex:
                        book_futs = [_ex.submit(_book_call, item) for item in tokens_to_fetch]
                        for fut in _as_completed(book_futs):
                            bi, role, book = fut.result()
                            book_results[(bi, role)] = book

                for bi, mkt in mdata.items():
                    yes_token = mkt.get("token_id", "")
                    no_token = mkt.get("no_token_id", "")
                    if yes_token:
                        yes_price = price_results.get((bi, "yes"))
                        yes_book = book_results.get((bi, "yes"))
                        if yes_price is not None:
                            mkt["best_ask"] = yes_price
                            clob_ok += 1
                            if yes_book:
                                mkt["_yes_book"] = yes_book
                            else:
                                missing_books += 1
                            if yes_price >= 0.90:
                                wall_count += 1
                        else:
                            if yes_book:
                                ask = reader.best_ask(yes_book)
                                if ask:
                                    mkt["best_ask"] = ask[0]
                                    mkt["_yes_book"] = yes_book
                                    if ask[0] >= 0.90:
                                        wall_count += 1
                                else:
                                    wall_count += 1
                                clob_ok += 1
                            else:
                                missing_books += 1
                    if no_token:
                        no_price = price_results.get((bi, "no"))
                        no_book = book_results.get((bi, "no"))
                        if no_price is not None:
                            mkt["best_bid"] = no_price
                            if no_book:
                                mkt["_no_book"] = no_book
                            else:
                                missing_books += 1
                        else:
                            if no_book:
                                ask = reader.best_ask(no_book)
                                if ask:
                                    mkt["best_bid"] = ask[0]
                                    mkt["_no_book"] = no_book
                            else:
                                missing_books += 1

                # --- Persist per-bracket top-of-book snapshot (best-effort) ---
                # First durable record of the exact CLOB prices this tick acts
                # on; needed for calibration refits + live/backtest parity.
                # Self-contained try/except inside — a snapshot failure never
                # breaks the enrichment path or the betting tick.
                _persist_book_snapshots(
                    conn, station_id, try_date_iso, mdata, _log_health,
                )

                # Count NO-side valid prices. NO favorites are valid even above
                # 0.90, because the 4-strategy spec explicitly allows them.
                no_valid = sum(1 for mkt in mdata.values()
                               if mkt.get("best_bid") is not None and mkt["best_bid"] > 0)

                if clob_ok == 0 and no_valid == 0:
                    if missing_books:
                        _log_health(
                            "clob",
                            "ERROR",
                            f"No CLOB prices for {try_date_iso} ({missing_books} tokens missing)",
                        )
                        logger.warning(
                            "Station %s: no CLOB prices for %s (%d tokens missing)",
                            station_id,
                            try_date_iso,
                            missing_books,
                        )
                    else:
                        _log_health("clob", "ERROR", f"No CLOB prices for {try_date_iso}")
                        logger.warning("Station %s: CLOB returned 0 prices for %s", station_id, try_date_iso)
                    continue
                elif wall_count == clob_ok and no_valid == 0:
                    _log_health("clob", "WARNING",
                                f"All {clob_ok} brackets show walls for {try_date_iso}")
                    logger.warning("Station %s: all %d brackets walls (0 NO valid) for %s, skipping target date",
                                   station_id, clob_ok, try_date_iso)
                    continue
                else:
                    detail = f"{clob_ok}/{len(mdata)} enriched ({wall_count} walls) for {try_date_iso}"
                    status = "OK"
                    if missing_books:
                        status = "WARNING"
                        detail += f"; {missing_books} tokens missing order books"
                    _log_health("clob", status, detail)
            except Exception as exc:
                _log_health("clob", "ERROR", f"CLOB enrichment failed for {try_date_iso}: {exc}")
                logger.warning("Station %s: CLOB enrichment failed for %s", station_id, try_date_iso, exc_info=True)
                continue

            event_types = ("bet", "dry_run") if dry_run else ("bet",)
            event_placeholders = ",".join("?" for _ in event_types)
            existing = conn.execute(
                f"""SELECT COUNT(*) as cnt FROM ledger
                WHERE station_id = ? AND target_date = ?
                AND event_type IN ({event_placeholders})
                AND outcome != 'CANCELLED'""",
                (station_id, try_date_iso, *event_types),
            ).fetchone()["cnt"]
            if existing >= MAX_PER_MARKET:
                _log_health(
                    "gates",
                    "SKIP",
                    f"MAX_PER_MARKET reached ({existing}/{MAX_PER_MARKET}) for {try_date_iso}",
                )
                logger.debug(
                    "Station %s: MAX_PER_MARKET reached (%d) for %s",
                    station_id, existing, try_date_iso,
                )
                continue

            # Found a live market
            market_data = mdata
            target_date = try_date
            target_date_iso = try_date_iso
            _heal_station_unit_if_wrong(conn, station_id, station.unit, market_data)
            break

        if market_data is None:
            _log_health("market", "WARNING", f"No live market for {target_date_iso}")
            logger.info("Station %s: no live market for %s", station_id, target_date_iso)
            return

        # --- Fetch forecast for the selected target date ---
        ensemble_data = _fetch_ensemble(station, target_date)
        if not ensemble_data:
            _log_health("forecast", "ERROR", "Open-Meteo returned no data")
            logger.debug("Station %s: no forecast data available", station_id)
            return

        _log_health("forecast", "OK", f"{len(ensemble_data)} models")

        # --- Ensemble membership lock: require the exact calibrated model set ---
        from hightempbot.execution.strategy_constants import EXPECTED_MODELS, REQUIRED_MEMBERS
        present = set(ensemble_data.keys())
        expected = set(EXPECTED_MODELS)
        member_count = len(present)
        if member_count != REQUIRED_MEMBERS or present != expected:
            _notify(
                title=f"Ensemble deviation: {member_count} members (expected {REQUIRED_MEMBERS})",
                message=(
                    f"Station: {station_id}\n"
                    f"Target date: {target_date_iso}\n"
                    f"Present ({member_count}): {sorted(present)}\n"
                    f"Missing vs expected: {sorted(expected - present)}\n"
                    f"Extra vs expected: {sorted(present - expected)}"
                ),
                stage="ensemble_count",
                station_id=station_id,
            )
            logger.warning(
                "Station %s ensemble set mismatch (present=%s, expected=%s); skipping bet decision",
                station_id,
                sorted(present),
                sorted(expected),
            )
            return

        # --- Build order client (live only) ---
        order_client = None
        cfg = None
        if not dry_run:
            try:
                from hightempbot.runtime_config import get_config
                from hightempbot.execution.walker import OrderClient
                # Process-wide singleton: avoids re-reading .env every tick
                # (ce-code-review P1 #11). Credential rotation requires
                # restart_bot.sh per the documented workflow.
                cfg = get_config()
                order_client = OrderClient(cfg)
            except Exception:
                logger.error("Failed to create OrderClient for %s", station_id, exc_info=True)
                # Surface this in pipeline_health so silent boot-time
                # credential breakage is operator-visible (ADV-009).
                _log_health(
                    "order", "ERROR",
                    "OrderClient construction failed -- check POLY_* credentials in .env",
                )
                return

        # --- Run pipeline ---
        from hightempbot.execution.pipeline import run_betting_cycle
        result = run_betting_cycle(
            conn=conn,
            station=station,
            ensemble_data=ensemble_data,
            market_data=market_data,
            order_client=order_client,
            initial_bankroll=initial_bankroll,
            dry_run=dry_run,
            target_date=target_date_iso,
            horizon=1,
            config=cfg,
            local_now_hour=local_now.hour,
            local_now_minute=local_now.minute,
        )

        # --- Log pipeline results for dashboard Pipeline tab ---
        if result:
            n_evaluated = result.n_evaluated
            n_placed = result.n_placed
            n_skipped = n_evaluated - n_placed

            if n_placed > 0:
                _log_health("gates", "OK", f"{n_placed} bets, {n_skipped} skipped")
                _log_health("order", "OK", f"{n_placed} orders placed")
                mode = "DRY-RUN" if dry_run else "LIVE"
                topup_note = f" ({result.n_topup} top-up)" if result.n_topup > 0 else ""
                # Split the rate-limit bucket between first-fill and top-up
                # alerts so a top-up on a freshly opened slot is not silently
                # swallowed by the 15-min window of the opening alert.
                bet_stage = "bet_placed_topup" if result.n_topup > 0 else "bet_placed_first"
                _notify(
                    f"{mode} Bet: {station_id}",
                    f"{n_placed} bet(s) placed for {target_date_iso}{topup_note}, "
                    f"exposure ${result.total_exposure_usd:.2f}",
                    stage=bet_stage, station_id=station_id,
                )
            elif n_evaluated > 0:
                _log_health("gates", "SKIP", f"0/{n_evaluated} passed gates")
            else:
                _log_health("gates", "OK", "No signals evaluated")
    except Exception:
        logger.error("Betting tick failed for %s", station_id, exc_info=True)
    finally:
        conn.close()
