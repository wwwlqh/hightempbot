"""Download historical PMD order books into backtest/data/polymarket_books_10m.db.

This complements fetch_polymarket_history.py. It reads market slugs from
backtest/data/polymarket_history.db, calls polymarketdata.co /books, and stores
per-token bid/ask ladders in a separate SQLite DB because the payload is large.

Usage:
    python backtest/scripts/fetch_polymarket_books.py --start 2026-02-27 --end 2026-05-21 --missing-only
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backtest.scripts.fetch_polymarket_history import (  # noqa: E402
    PMD_RPM_LIMIT,
    RateLimiter,
    load_env,
    pmd_get,
    _parse_ts,
)
from backtest.lib.sweep_lib import MARKET_DB  # noqa: E402

BOOKS_DB = REPO_ROOT / "backtest" / "data" / "polymarket_books_10m.db"
RESOLUTION = "10m"

logger = logging.getLogger("polymarket_books")


def init_db() -> sqlite3.Connection:
    BOOKS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(BOOKS_DB, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS book_snapshots (
            market_slug TEXT NOT NULL,
            token       TEXT NOT NULL,
            ts_unix     INTEGER NOT NULL,
            bids_json   TEXT NOT NULL,
            asks_json   TEXT NOT NULL,
            PRIMARY KEY (market_slug, token, ts_unix)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS books_fetch_log (
            market_slug TEXT PRIMARY KEY,
            market_date TEXT,
            status      TEXT NOT NULL,
            n_snaps     INTEGER,
            n_pages     INTEGER,
            http_code   INTEGER,
            error       TEXT,
            fetched_at  TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


def load_markets(start_date: date, end_date: date, missing_only: bool) -> list[tuple[str, str]]:
    src = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True)
    src.execute(f"ATTACH DATABASE '{BOOKS_DB}' AS books")
    where = ["m.market_date BETWEEN ? AND ?"]
    params: list[str] = [start_date.isoformat(), end_date.isoformat()]
    if missing_only:
        where.append("(f.market_slug IS NULL OR f.status NOT IN ('ok', 'empty'))")
    rows = src.execute(
        f"""
        SELECT m.market_slug, m.market_date
        FROM markets m
        LEFT JOIN books.books_fetch_log f ON f.market_slug = m.market_slug
        WHERE {' AND '.join(where)}
        ORDER BY m.market_date, m.market_slug
        """,
        params,
    ).fetchall()
    src.close()
    return [(str(slug), str(mdate)) for slug, mdate in rows]


def _book_rows(payload) -> list[tuple[str, int, str, str]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return []

    rows: list[tuple[str, int, str, str]] = []
    for token in ("Yes", "No"):
        snapshots = data.get(token) or []
        if not isinstance(snapshots, list):
            continue
        for snap in snapshots:
            if not isinstance(snap, dict):
                continue
            ts = _parse_ts(snap.get("t") or snap.get("ts") or snap.get("timestamp"))
            if ts is None:
                continue
            bids = snap.get("bids") or []
            asks = snap.get("asks") or []
            rows.append((
                token,
                ts,
                json.dumps(bids, separators=(",", ":")),
                json.dumps(asks, separators=(",", ":")),
            ))
    return rows


def fetch_books(
    api_key: str,
    market_slug: str,
    market_date: str,
    session: requests.Session,
    limiter: RateLimiter,
    lookback_days: int,
) -> tuple[int, int, str, int]:
    md = date.fromisoformat(market_date)
    start = datetime(md.year, md.month, md.day, tzinfo=timezone.utc) - timedelta(days=lookback_days)
    end = datetime(md.year, md.month, md.day, 23, 59, 59, tzinfo=timezone.utc)
    now_utc = datetime.now(timezone.utc) - timedelta(seconds=60)
    if end > now_utc:
        end = now_utc
    if start >= end:
        return 0, 200, "window_empty", 0
    params = {
        "start_ts": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_ts": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolution": RESOLUTION,
    }
    payload, code, err = pmd_get(session, api_key, f"/markets/{market_slug}/books", params, limiter)
    if code != 200:
        return 0, code, err, 0
    return len(_book_rows(payload)), code, "", 1


def run(
    start_date: date,
    end_date: date,
    missing_only: bool,
    workers: int,
    limit: int | None,
    lookback_days: int,
    plan_days: int | None,
) -> None:
    api_key = load_env()
    conn = init_db()
    markets = load_markets(start_date, end_date, missing_only)
    if limit is not None:
        markets = markets[:limit]
    logger.info(
        "fetching books for %d markets (%s -> %s, missing_only=%s)",
        len(markets),
        start_date,
        end_date,
        missing_only,
    )
    if not markets:
        return

    limiter = RateLimiter(PMD_RPM_LIMIT)

    def task(slug: str, mdate: str):
        sess = requests.Session()
        md = date.fromisoformat(mdate)
        start = datetime(md.year, md.month, md.day, tzinfo=timezone.utc) - timedelta(days=lookback_days)
        end = datetime(md.year, md.month, md.day, 23, 59, 59, tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc) - timedelta(seconds=60)
        if plan_days is not None:
            plan_floor = now_utc - timedelta(days=plan_days) + timedelta(minutes=5)
            if start < plan_floor:
                start = plan_floor
        if end > now_utc:
            end = now_utc
        if start >= end:
            return slug, mdate, [], 200, "window_empty", 0
        params = {
            "start_ts": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_ts": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "resolution": RESOLUTION,
        }
        payload, code, err = pmd_get(sess, api_key, f"/markets/{slug}/books", params, limiter)
        return slug, mdate, _book_rows(payload), code, err, 1

    t0 = time.time()
    done = ok = empty = errors = snaps = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(task, slug, mdate) for slug, mdate in markets]
        for fut in as_completed(futs):
            slug, mdate, rows, code, err, pages = fut.result()
            status = "error"
            if code == 200:
                status = "ok" if rows else "empty"
            if status == "ok":
                ok += 1
                snaps += len(rows)
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO book_snapshots
                    (market_slug, token, ts_unix, bids_json, asks_json)
                    VALUES (?,?,?,?,?)
                    """,
                    [(slug, token, ts, bids, asks) for token, ts, bids, asks in rows],
                )
            elif status == "empty":
                empty += 1
            else:
                errors += 1
            conn.execute(
                """
                INSERT OR REPLACE INTO books_fetch_log
                (market_slug, market_date, status, n_snaps, n_pages, http_code, error, fetched_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    slug,
                    mdate,
                    status,
                    len(rows),
                    pages,
                    code,
                    err[:300] if err else "",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            done += 1
            if done % 25 == 0:
                conn.commit()
                rate = done / max(1.0, time.time() - t0)
                eta = (len(markets) - done) / max(rate, 0.01)
                logger.info(
                    "progress %d/%d ok=%d empty=%d errors=%d snaps=%d rate=%.2f/s eta=%ds",
                    done,
                    len(markets),
                    ok,
                    empty,
                    errors,
                    snaps,
                    rate,
                    int(eta),
                )
    conn.commit()
    logger.info("DONE ok=%d empty=%d errors=%d snapshots=%d db=%s", ok, empty, errors, snaps, BOOKS_DB)
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--missing-only", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=0,
        help="days before market_date 00:00Z to request; default 0 for entry-day L2 backtests",
    )
    parser.add_argument(
        "--plan-days",
        type=int,
        default=90,
        help="clamp start_ts to current PMD rolling history window; use 0 to disable",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run(
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
        bool(args.missing_only),
        int(args.workers),
        args.limit,
        int(args.lookback_days),
        int(args.plan_days) if int(args.plan_days) > 0 else None,
    )


if __name__ == "__main__":
    main()
