"""Scrape current (and optionally hourly) bracket prices into polymarket_prices.

    python -m hightempbot.ingestion.polymarket_prices          # scrape today
    python -m hightempbot.ingestion.polymarket_prices --history # scrape with hourly history
"""

import argparse
import json
import logging
import sqlite3
import time
from datetime import date

import requests

from hightempbot.db.connection import utc_now_sql
from hightempbot.stations import ICAO_TO_CITY

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Normalized city name → ICAO, filled by register_enrolled_station().
_CITY_TO_ICAO: dict[str, str] = {}


def gamma_event_slug(city_slug: str, target_date: date) -> str:
    """Polymarket Gamma event slug for a city + target date."""
    month = target_date.strftime("%B").lower()
    return f"highest-temperature-in-{city_slug}-on-{month}-{target_date.day}-{target_date.year}"


def ensure_table(conn):
    """Create polymarket_prices table if not exists."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS polymarket_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL,
            market_date TEXT NOT NULL,
            bracket_index INTEGER NOT NULL,
            bracket_label TEXT NOT NULL,
            token_id TEXT NOT NULL,
            price REAL,
            volume REAL,
            scraped_at TEXT NOT NULL,
            UNIQUE(station_id, market_date, bracket_index, scraped_at)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS polymarket_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL,
            market_date TEXT NOT NULL,
            bracket_index INTEGER NOT NULL,
            token_id TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            price REAL NOT NULL,
            UNIQUE(station_id, market_date, bracket_index, timestamp)
        )
    """)
    conn.commit()


def _bootstrap_station_registry(conn) -> None:
    """Load enrolled stations into the in-memory slug registry."""
    from hightempbot.stations import get_all_stations, register_enrolled_station

    for station in get_all_stations(conn).values():
        register_enrolled_station(station)


def find_temperature_events(target_date=None, conn=None):
    """Find all active temperature market events from Gamma API."""
    if target_date is None:
        target_date = date.today()

    if not ICAO_TO_CITY and conn is not None:
        _bootstrap_station_registry(conn)

    events = []
    for icao, city_slug in ICAO_TO_CITY.items():
        # Fetch the requested market date only.
        for d in [target_date]:
            slug = gamma_event_slug(city_slug, d)

            try:
                resp = requests.get(
                    f"{GAMMA_API}/events",
                    params={"slug": slug},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data:
                        event = data[0] if isinstance(data, list) else data
                        # Safety: reject if not a "highest temperature" market
                        event_title = event.get("title", "")
                        if "highest temperature" not in event_title.lower():
                            logger.warning("Rejecting non-highest-temp event for %s: %s", icao, event_title[:80])
                            continue
                        markets = event.get("markets", [])
                        if markets:
                            events.append({
                                "station_id": icao,
                                "market_date": d.isoformat(),
                                "event_title": event.get("title", ""),
                                "markets": markets,
                            })
                            logger.info(f"  {icao}: {len(markets)} brackets")
                time.sleep(0.1)  # rate limit
            except Exception as e:
                logger.warning(f"  {icao} {d}: {e}")

    return events


def scrape_current_prices(conn, events):
    """Store current bracket prices for all events."""
    now = utc_now_sql()
    count = 0

    for event in events:
        for bi, market in enumerate(event["markets"]):
            question = market.get("question", "")
            token_ids = market.get("clobTokenIds", [])
            prices = market.get("outcomePrices", [])
            # These may be JSON strings, not lists
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
            if isinstance(prices, str):
                prices = json.loads(prices)

            if not token_ids or not prices:
                continue

            # YES token is first, NO token is second
            yes_token = token_ids[0]
            yes_price = float(prices[0]) if prices else None
            volume = float(market.get("volume", 0) or 0)

            # Extract bracket label from question
            label = question.split("be ")[-1].split(" on ")[0] if "be " in question else question[:30]

            try:
                conn.execute(
                    """INSERT OR REPLACE INTO polymarket_prices
                       (station_id, market_date, bracket_index, bracket_label, token_id, price, volume, scraped_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event["station_id"], event["market_date"], bi, label, yes_token, yes_price, volume, now),
                )
                count += 1
            except sqlite3.IntegrityError:
                pass

    conn.commit()
    return count


def scrape_price_history(conn, events):
    """Fetch hourly price history for each bracket."""
    count = 0

    for event in events:
        for bi, market in enumerate(event["markets"]):
            token_ids = market.get("clobTokenIds", [])
            if not token_ids:
                continue

            yes_token = token_ids[0]

            try:
                resp = requests.get(
                    f"{CLOB_API}/prices-history",
                    params={"market": yes_token, "interval": "max", "fidelity": 60},
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    history = data.get("history", [])
                    for point in history:
                        ts = int(point.get("t", 0))
                        price = float(point.get("p", 0))
                        try:
                            conn.execute(
                                """INSERT OR IGNORE INTO polymarket_price_history
                                   (station_id, market_date, bracket_index, token_id, timestamp, price)
                                   VALUES (?, ?, ?, ?, ?, ?)""",
                                (event["station_id"], event["market_date"], bi, yes_token, ts, price),
                            )
                            count += 1
                        except sqlite3.IntegrityError:
                            pass

                time.sleep(0.2)  # rate limit
            except Exception as e:
                logger.warning(f"  History {event['station_id']} bracket {bi}: {e}")

    conn.commit()
    return count


def main():
    parser = argparse.ArgumentParser(description="Scrape Polymarket temperature bracket prices")
    parser.add_argument("--history", action="store_true", help="Also fetch hourly price history")
    parser.add_argument("--db", default="data/hightempbot.db", help="Database path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from hightempbot.db.connection import get_connection
    conn = get_connection(args.db)
    ensure_table(conn)
    _bootstrap_station_registry(conn)

    logger.info("Finding active temperature markets...")
    events = find_temperature_events(conn=conn)
    logger.info(f"Found {len(events)} station markets with {sum(len(e['markets']) for e in events)} brackets")

    if not events:
        logger.warning("No active markets found. Markets may not exist for today.")
        conn.close()
        return

    logger.info("Scraping current prices...")
    n_prices = scrape_current_prices(conn, events)
    logger.info(f"Stored {n_prices} bracket prices")

    if args.history:
        logger.info("Scraping price history (hourly)...")
        n_history = scrape_price_history(conn, events)
        logger.info(f"Stored {n_history} history points")

    conn.close()
    logger.info("Done.")


if __name__ == "__main__":
    main()
