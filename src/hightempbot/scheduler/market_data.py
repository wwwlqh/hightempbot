"""Market + ensemble data fetchers — extracted from station_scanner.py (U11).

Two responsibilities collected here:

* Open-Meteo ensemble cache and fetch (`_fetch_ensemble` + module-level
  cache state) — locked once readiness fires for the UTC day.
* Polymarket market_tokens DB cache and Gamma slug fallback
  (`_fetch_market_data` + supporting helpers `_parse_raw_markets`,
  `_filter_tradable_markets`, `_store_negative_sentinel`) — both
  positive and negative TTL caching for the bracket inventory.

Imports `parse_bracket_bounds` directly from `resolution.gamma` (post-U14
package extraction). No back-import from station_scanner so this module can
be loaded first without circular import.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

import pytz

from hightempbot.db.connection import parse_utc_timestamp
from hightempbot.resolution.gamma import parse_bracket_bounds
from hightempbot.stations import StationConfig

logger = logging.getLogger(__name__)


_NEGATIVE_MARKET_CACHE_TTL = 6 * 3600
_POSITIVE_MARKET_CACHE_TTL = 12 * 3600   # refetch Gamma after 12h to catch relisting
_MARKET_VOLUME_CACHE_TTL = 2 * 3600
_GAMMA_VOLUME_REFRESH_TIMEOUTS = (10, 3)


# Open-Meteo ensemble cache: {(station_id, target_date): (timestamp, data)}.
# Locked for most of the UTC day after the readiness gate confirms all models.
_ensemble_cache: dict[tuple[str, date], tuple[float, dict[str, float]]] = {}
_ensemble_negative_cache: dict[tuple[str, date], float] = {}
_ensemble_lock = threading.Lock()
_ensemble_inflight_locks: dict[tuple[str, date], threading.Lock] = {}
_ENSEMBLE_NEGATIVE_TTL = 120
_market_volume_cache: dict[str, tuple[float, float]] = {}
_market_volume_lock = threading.Lock()
_ENSEMBLE_TTL = 22 * 3600  # 22 hours — lock ensemble once readiness fires for the UTC day


def _prune_market_volume_cache_locked(now: float) -> None:
    for mid, (ts, _volume) in list(_market_volume_cache.items()):
        if (now - ts) >= _MARKET_VOLUME_CACHE_TTL:
            _market_volume_cache.pop(mid, None)


def _cache_market_volumes(volumes: dict[str, float | None], now: float | None = None) -> None:
    if not volumes:
        return
    now = time.time() if now is None else now
    with _market_volume_lock:
        _prune_market_volume_cache_locked(now)
        for mid, volume in volumes.items():
            if not mid or volume is None:
                continue
            _market_volume_cache[mid] = (now, float(volume))


def _apply_cached_market_volumes(mdata: dict[int, dict], now: float | None = None) -> int:
    """Patch missing ``volume24hr`` values from short-lived conditionId cache."""
    if not mdata:
        return 0
    now = time.time() if now is None else now
    patched = 0
    with _market_volume_lock:
        _prune_market_volume_cache_locked(now)
        for mkt in mdata.values():
            if mkt.get("volume24hr") is not None:
                continue
            mid = mkt.get("market_id", "")
            cached = _market_volume_cache.get(mid)
            if cached is None:
                continue
            mkt["volume24hr"] = cached[1]
            patched += 1
    return patched


def _fresh_cached_ensemble(cache_key: tuple[str, date], now: float) -> dict[str, float] | None:
    cached = _ensemble_cache.get(cache_key)
    if cached and (now - cached[0]) < _ENSEMBLE_TTL:
        return cached[1]
    return None


def _has_fresh_negative_ensemble(cache_key: tuple[str, date], now: float) -> bool:
    ts = _ensemble_negative_cache.get(cache_key)
    return ts is not None and (now - ts) < _ENSEMBLE_NEGATIVE_TTL


def _prune_ensemble_state(now: float) -> None:
    """Drop expired cache state and idle singleflight locks."""
    for key, (ts, _data) in list(_ensemble_cache.items()):
        if (now - ts) >= _ENSEMBLE_TTL:
            _ensemble_cache.pop(key, None)
    for key, ts in list(_ensemble_negative_cache.items()):
        if (now - ts) >= _ENSEMBLE_NEGATIVE_TTL:
            _ensemble_negative_cache.pop(key, None)

    live_keys = set(_ensemble_cache) | set(_ensemble_negative_cache)
    for key, lock in list(_ensemble_inflight_locks.items()):
        if key not in live_keys and not lock.locked():
            _ensemble_inflight_locks.pop(key, None)


def _store_negative_ensemble(cache_key: tuple[str, date]) -> None:
    with _ensemble_lock:
        now = time.time()
        _prune_ensemble_state(now)
        _ensemble_negative_cache[cache_key] = now


def _inflight_lock_for(cache_key: tuple[str, date]) -> threading.Lock:
    with _ensemble_lock:
        _prune_ensemble_state(time.time())
        lock = _ensemble_inflight_locks.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _ensemble_inflight_locks[cache_key] = lock
        return lock


def _fetch_ensemble(station: StationConfig, target_date: date | None = None) -> dict[str, float]:
    """Fetch latest forecast data from Open-Meteo for this station.

    The target-date cache keeps all ticks on the same UTC readiness cycle
    using the same ensemble.
    """
    station_id = station.icao
    now = time.time()
    station_today = datetime.now(pytz.timezone(station.timezone)).date()
    fetch_target = target_date or station_today
    cache_key = (station_id, fetch_target)

    with _ensemble_lock:
        _prune_ensemble_state(now)
        cached = _fresh_cached_ensemble(cache_key, now)
        if cached is not None:
            return cached
        if _has_fresh_negative_ensemble(cache_key, now):
            return {}

    # Singleflight per station/date without holding the global cache lock
    # across Open-Meteo network I/O.
    with _inflight_lock_for(cache_key):
        with _ensemble_lock:
            now = time.time()
            _prune_ensemble_state(now)
            cached = _fresh_cached_ensemble(cache_key, now)
            if cached is not None:
                return cached
            if _has_fresh_negative_ensemble(cache_key, now):
                return {}

        try:
            from hightempbot.ingestion.openmeteo_forecast import fetch_live
            forecast_days = max(2, (fetch_target - station_today).days + 1)
            result = fetch_live(station, forecast_days=forecast_days)
            if result is None:
                _store_negative_ensemble(cache_key)
                return {}

            if fetch_target not in result:
                _store_negative_ensemble(cache_key)
                return {}

            model_data = result[fetch_target]
            ensemble = {}
            for model_name, tmax in model_data.items():
                if tmax is not None:
                    ensemble[model_name] = tmax

            with _ensemble_lock:
                _ensemble_negative_cache.pop(cache_key, None)
                _ensemble_cache[cache_key] = (time.time(), ensemble)
            return ensemble
        except Exception:
            logger.warning("Failed to fetch ensemble for %s", station_id, exc_info=True)
            _store_negative_ensemble(cache_key)
            return {}


def _parse_raw_markets(markets: list[dict]) -> dict[int, dict]:
    """Convert raw Gamma API market dicts to structured bracket data.

    Shared parser used by both discovery path and per-station slug fallback.
    """
    import json

    by_index: dict[int, dict] = {}

    for bi, market in enumerate(markets):
        token_ids = market.get("clobTokenIds", [])
        prices = market.get("outcomePrices", [])
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
        if isinstance(prices, str):
            prices = json.loads(prices)

        if not token_ids or not prices:
            continue

        yes_token = token_ids[0]
        no_token = token_ids[1] if len(token_ids) > 1 else ""
        yes_price = float(prices[0]) if prices else 0.0
        # Use actual NO price from Gamma (prices[1]), not synthetic 1-YES
        no_price = float(prices[1]) if len(prices) > 1 else (1.0 - yes_price if yes_price > 0 else 0.0)
        volume = float(market.get("volume", 0) or 0)
        market_id = market.get("conditionId", "")

        mkt_data = {
            "best_ask": yes_price,
            "best_bid": no_price,  # actual NO price from Gamma API
            "volume24hr": volume,
            "market_id": market_id,
            "token_id": yes_token,
            "no_token_id": no_token,
        }

        question = market.get("question", "")
        bounds = parse_bracket_bounds(question)
        if bounds:
            mkt_data["bracket_low"] = bounds[0]
            mkt_data["bracket_high"] = bounds[1]
            mkt_data["bracket_label"] = bounds[2]

        by_index[bi] = mkt_data

    return by_index


def _filter_tradable_markets(markets: dict[int, dict]) -> dict[int, dict]:
    """Keep only brackets with at least one live CLOB order book.

    Gamma/web discovery can return token ids for brackets that no longer have
    a CLOB book. A market page may still exist visually, but those dead tokens
    are not tradable by the bot. We therefore validate tradability before
    caching a newly discovered market.
    """
    if not markets:
        return {}

    from hightempbot.execution.walker import ClobReader

    reader = ClobReader()
    filtered: dict[int, dict] = {}

    tokens: list[tuple[int, str]] = []
    for bi, mkt in markets.items():
        yes_token = mkt.get("token_id", "")
        no_token = mkt.get("no_token_id", "")
        if yes_token:
            tokens.append((bi, yes_token))
        if no_token:
            tokens.append((bi, no_token))

    if not tokens:
        return {}

    def _fetch_book(token: str) -> dict | None:
        return reader.fetch_order_book(token)

    live_indices: set[int] = set()
    max_workers = min(6, len(tokens))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="market-book") as executor:
        future_to_index = {
            executor.submit(_fetch_book, token): bi
            for bi, token in tokens
        }
        for future in as_completed(future_to_index):
            bi = future_to_index[future]
            try:
                if future.result():
                    live_indices.add(bi)
            except Exception:
                logger.debug(
                    "Failed to validate CLOB book while filtering market %s",
                    bi,
                    exc_info=True,
                )

    for bi, mkt in markets.items():
        if bi in live_indices:
            filtered[bi] = mkt

    return filtered


def _store_negative_sentinel(conn, station_id: str, date_iso: str) -> None:
    """Store a sentinel row in market_tokens to cache 'no market exists'."""
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO market_tokens "
            "(station_id, market_date, bracket_idx, token_id, no_token_id, "
            "market_id, bracket_label, bracket_low, bracket_high) "
            "VALUES (?, ?, -1, '', '', '', NULL, NULL, NULL)",
            (station_id, date_iso),
        )
        conn.commit()
        logger.debug("Stored negative cache sentinel for %s %s", station_id, date_iso)
    except Exception:
        logger.debug("Failed to store negative sentinel for %s %s", station_id, date_iso, exc_info=True)


def _fetch_market_data(station_id: str, target_date: date | None = None,
                       conn=None) -> dict[int, dict]:
    """Fetch market token metadata from DB cache or Gamma API (lazy cache).

    Strategy:
      1. Check market_tokens DB table (cached from prior tick)
      2. Cache miss → Gamma slug lookup, store in DB, return

    Prices (best_ask, best_bid) are set to 0 from cache — CLOB enrichment
    in run_betting_tick() overwrites them with live orderbook prices.

    Returns {bracket_idx: {best_ask, best_bid, volume24hr, market_id, token_id, ...}}.
    """
    if target_date is None:
        target_date = date.today()

    date_iso = target_date.isoformat()

    # --- Check DB cache ---
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT * FROM market_tokens WHERE station_id = ? AND market_date = ? ORDER BY bracket_idx",
                (station_id, date_iso),
            ).fetchall()
            if rows:
                # Check for negative-result sentinel (bracket_idx = -1)
                if len(rows) == 1 and rows[0]["bracket_idx"] == -1:
                    fetched_at = parse_utc_timestamp(rows[0]["fetched_at"])
                    negative_age = None if fetched_at is None else (datetime.now(timezone.utc) - fetched_at).total_seconds()
                    if fetched_at is None or negative_age >= _NEGATIVE_MARKET_CACHE_TTL:
                        conn.execute(
                            "DELETE FROM market_tokens WHERE station_id = ? AND market_date = ? AND bracket_idx = -1",
                            (station_id, date_iso),
                        )
                        conn.commit()
                        logger.debug(
                            "Expired negative cache for %s %s; retrying Gamma lookup",
                            station_id,
                            date_iso,
                        )
                        rows = []
                    else:
                        logger.debug("Negative cache hit for %s %s (no market exists)", station_id, date_iso)
                        return {}
                if not rows:
                    pass
                else:
                    # Positive-cache TTL: rows older than _POSITIVE_MARKET_CACHE_TTL
                    # for a future/current target_date are assumed stale (Polymarket
                    # may have relisted brackets). Delete and fall through to Gamma.
                    fetched_at = parse_utc_timestamp(rows[0]["fetched_at"])
                    age = None if fetched_at is None else (datetime.now(timezone.utc) - fetched_at).total_seconds()
                    if target_date >= date.today() and (
                        fetched_at is None or age >= _POSITIVE_MARKET_CACHE_TTL
                    ):
                        conn.execute(
                            "DELETE FROM market_tokens WHERE station_id = ? AND market_date = ?",
                            (station_id, date_iso),
                        )
                        conn.commit()
                        logger.info(
                            "Expired positive cache for %s %s (age %.1fh); retrying Gamma",
                            station_id, date_iso, (age or 0) / 3600.0,
                        )
                        rows = []
                    else:
                        result: dict[int, dict] = {}
                        for row in rows:
                            if row["bracket_idx"] == -1:
                                continue
                            result[row["bracket_idx"]] = {
                                "best_ask": 0.0,       # CLOB enrichment will overwrite
                                "best_bid": 0.0,
                                # None = needs refresh from Gamma; fresh fetches
                                # below set this from _parse_raw_markets so callers
                                # can skip refresh_market_volume on cache miss.
                                "volume24hr": None,
                                "market_id": row["market_id"],
                                "token_id": row["token_id"],
                                "no_token_id": row["no_token_id"],
                                "bracket_label": row["bracket_label"],
                                "bracket_low": row["bracket_low"],
                                "bracket_high": row["bracket_high"],
                            }
                        has_parseable_bounds = any(
                            mkt.get("bracket_label") is not None
                            or mkt.get("bracket_low") is not None
                            or mkt.get("bracket_high") is not None
                            for mkt in result.values()
                        )
                        if result and not has_parseable_bounds:
                            conn.execute(
                                "DELETE FROM market_tokens WHERE station_id = ? AND market_date = ?",
                                (station_id, date_iso),
                            )
                            conn.commit()
                            logger.info(
                                "Discarded unparseable cached market tokens for %s %s; retrying Gamma",
                                station_id, date_iso,
                            )
                        elif result:
                            logger.debug("Loaded %d cached brackets for %s %s", len(result), station_id, date_iso)
                            return result
        except Exception:
            pass  # table may not exist yet on first run

    # --- Cache miss: Gamma slug lookup ---
    try:
        from hightempbot.ingestion.polymarket_prices import GAMMA_API, gamma_event_slug
        from hightempbot.stations import poly_slug_for_station_id
        import requests

        city_slug = poly_slug_for_station_id(station_id, conn=conn)
        if city_slug is None:
            logger.debug("No Polymarket city slug for %s", station_id)
            return {}

        slug = gamma_event_slug(city_slug, target_date)

        resp = requests.get(
            f"{GAMMA_API}/events",
            params={"slug": slug},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Gamma API %d for %s", resp.status_code, station_id)
            return {}

        data = resp.json()
        if not data:
            _store_negative_sentinel(conn, station_id, date_iso)
            return {}

        event = data[0] if isinstance(data, list) else data

        # Safety: reject if not a "highest temperature" market
        event_title = event.get("title", "")
        if "highest temperature" not in event_title.lower():
            logger.warning("Rejecting non-highest-temp event for %s: %s", station_id, event_title[:80])
            _store_negative_sentinel(conn, station_id, date_iso)
            return {}

        markets = event.get("markets", [])
        if not markets:
            _store_negative_sentinel(conn, station_id, date_iso)
            return {}

        result = _parse_raw_markets(markets)
        if not result:
            return {}

        result = _filter_tradable_markets(result)
        if not result:
            logger.info("No tradable CLOB brackets for %s %s", station_id, date_iso)
            _store_negative_sentinel(conn, station_id, date_iso)
            return {}
        _cache_market_volumes({
            mkt.get("market_id", ""): mkt.get("volume24hr")
            for mkt in result.values()
        })

        # --- Store in DB cache ---
        if conn is not None:
            try:
                rows_to_insert = []
                for bi, mkt in result.items():
                    rows_to_insert.append((
                        station_id, date_iso, bi,
                        mkt.get("token_id", ""),
                        mkt.get("no_token_id", ""),
                        mkt.get("market_id", ""),
                        mkt.get("bracket_label"),
                        mkt.get("bracket_low"),
                        mkt.get("bracket_high"),
                    ))
                conn.executemany(
                    "INSERT OR REPLACE INTO market_tokens "
                    "(station_id, market_date, bracket_idx, token_id, no_token_id, "
                    "market_id, bracket_label, bracket_low, bracket_high) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows_to_insert,
                )
                conn.commit()
            except Exception:
                logger.warning("Failed to cache market tokens for %s %s", station_id, date_iso, exc_info=True)

        logger.info("Fetched %d brackets for %s %s (Gamma → cached)", len(result), station_id, date_iso)
        return result

    except Exception:
        logger.warning("Market data fetch failed for %s", station_id, exc_info=True)
        return {}


def refresh_market_volume(
    station_id: str,
    target_date: date,
    mdata: dict[int, dict],
    *,
    conn=None,
) -> None:
    """Refresh ``volume24hr`` on each bracket in ``mdata`` from Gamma.

    The market_tokens DB cache stores token IDs and bracket bounds but not
    volume — volume is per-tick state, not schema. This helper hits Gamma's
    ``/events?slug=...`` once per (station, date) and patches each cached
    bracket dict in-place by matching ``conditionId``. Best-effort: any
    failure leaves ``mdata`` unchanged.

    No-op when every bracket already carries a populated ``volume24hr``
    (cache-miss path: _fetch_market_data already parsed volume from the
    Gamma response, so re-fetching is wasted work).
    """
    if mdata and all(mkt.get("volume24hr") is not None for mkt in mdata.values()):
        _cache_market_volumes({
            mkt.get("market_id", ""): mkt.get("volume24hr")
            for mkt in mdata.values()
        })
        return

    from hightempbot.ingestion.polymarket_prices import GAMMA_API, gamma_event_slug
    from hightempbot.stations import poly_slug_for_station_id
    import requests

    try:
        city_slug = poly_slug_for_station_id(station_id, conn=conn)
        if not city_slug:
            return
        slug = gamma_event_slug(city_slug, target_date)

        resp = None
        last_exc: Exception | None = None
        for attempt, timeout_s in enumerate(_GAMMA_VOLUME_REFRESH_TIMEOUTS, start=1):
            try:
                resp = requests.get(
                    f"{GAMMA_API}/events",
                    params={"slug": slug},
                    timeout=timeout_s,
                )
                break
            except Exception as exc:
                last_exc = exc
                if attempt < len(_GAMMA_VOLUME_REFRESH_TIMEOUTS):
                    logger.debug(
                        "Gamma volume attempt %d/%d failed for %s %s",
                        attempt, len(_GAMMA_VOLUME_REFRESH_TIMEOUTS),
                        station_id, target_date,
                        exc_info=True,
                    )

        if resp is None:
            patched = _apply_cached_market_volumes(mdata)
            if patched:
                logger.warning(
                    "Gamma volume refresh failed for %s %s (%s); used cached volume for %d brackets",
                    station_id, target_date, last_exc, patched,
                )
            else:
                logger.warning(
                    "Failed to refresh volume for %s %s (%s)",
                    station_id, target_date, last_exc,
                )
            return

        if resp.status_code != 200:
            patched = _apply_cached_market_volumes(mdata)
            if patched:
                logger.warning(
                    "Gamma volume %d for %s %s; used cached volume for %d brackets",
                    resp.status_code, station_id, target_date, patched,
                )
                return
            logger.warning(
                "Gamma volume %d for %s %s", resp.status_code, station_id, target_date,
            )
            return
        data = resp.json()
        if not data:
            patched = _apply_cached_market_volumes(mdata)
            if patched:
                logger.warning(
                    "Gamma volume empty for %s %s; used cached volume for %d brackets",
                    station_id, target_date, patched,
                )
            return
        event = data[0] if isinstance(data, list) else data
        # Match by conditionId (stable) instead of enumerate index.
        vol_by_mid: dict[str, float] = {}
        for vm in event.get("markets", []):
            mid = vm.get("conditionId", "")
            if mid:
                vol_by_mid[mid] = float(vm.get("volume", 0) or 0)
        _cache_market_volumes(vol_by_mid)
        for _bi, mkt in mdata.items():
            mid = mkt.get("market_id", "")
            if mid in vol_by_mid:
                mkt["volume24hr"] = vol_by_mid[mid]
    except Exception:
        patched = _apply_cached_market_volumes(mdata)
        if patched:
            logger.warning(
                "Failed to refresh volume for %s %s; used cached volume for %d brackets",
                station_id, target_date, patched,
                exc_info=True,
            )
        else:
            logger.warning(
                "Failed to refresh volume for %s %s", station_id, target_date, exc_info=True,
            )

