"""Download Polymarket market metadata (Gamma) and 5-minute prices/metrics
(polymarketdata.co) into ``backtest/data/polymarket_history.db``.

    python backtest/scripts/fetch_polymarket_history.py --probe       # check key on one market
    python backtest/scripts/fetch_polymarket_history.py [--days 30 | --start D --end D] [--resume]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

logger = logging.getLogger("polymarket_history")

GAMMA_API = "https://gamma-api.polymarket.com"
PMD_API = "https://api.polymarketdata.co/v1"
RESOLUTION = "10m"  # API supports: 1m, 10m, 1h, 6h, 1d
PMD_RPM_LIMIT = 480  # 500 RPM plan with 4% safety margin


class RateLimiter:
    """Thread-safe sliding-window rate limiter: at most `rpm` calls per 60s."""

    def __init__(self, rpm: int):
        self.rpm = rpm
        self.window = 60.0
        self._lock = threading.Lock()
        self._calls: deque[float] = deque()

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.window:
                    self._calls.popleft()
                if len(self._calls) < self.rpm:
                    self._calls.append(now)
                    return
                wait = self.window - (now - self._calls[0]) + 0.005
            time.sleep(max(wait, 0.01))

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE_DB = REPO_ROOT / "data" / "hightempbot_server_latest.db"
OUT_DB = REPO_ROOT / "backtest" / "data" / "polymarket_history.db"
OUT_MANIFEST = REPO_ROOT / "backtest" / "data" / "manifest.csv"


# --------------------------------------------------------------------------- env

def load_env() -> str:
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.split("#", 1)[0].strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(), v)
    key = os.environ.get("POLYMARKETDATA_API_KEY", "").strip()
    if not key or key == "your_polymarketdata_api_key_here":
        sys.exit("ERROR: set POLYMARKETDATA_API_KEY in .env (see .env.example)")
    return key


# --------------------------------------------------------------------------- stations

def _city_to_slug(city: str) -> str:
    nfkd = unicodedata.normalize("NFKD", city)
    ascii_text = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def _station_slug_aliases(city: str, poly_slug: str | None) -> tuple[str, ...]:
    """Return Polymarket slug aliases in live-discovery order."""
    aliases: list[str] = []
    for raw in (poly_slug, city):
        slug = _city_to_slug(raw or "")
        if slug and slug not in aliases:
            aliases.append(slug)
    return tuple(aliases)


def load_wu_stations() -> list[tuple[str, tuple[str, ...]]]:
    if not SOURCE_DB.exists():
        sys.exit(f"ERROR: source DB missing: {SOURCE_DB}")
    c = sqlite3.connect(SOURCE_DB)
    rows = c.execute(
        "SELECT icao, city, COALESCE(poly_slug, '') FROM enrolled_stations "
        "WHERE LOWER(resolution_source)='wu' ORDER BY icao"
    ).fetchall()
    c.close()
    return [(icao, _station_slug_aliases(city, poly_slug)) for icao, city, poly_slug in rows]


def _build_city_slug_index(stations: list[tuple[str, tuple[str, ...]]]) -> dict[str, str]:
    city_to_icao: dict[str, str] = {}
    for icao, slug_aliases in stations:
        for city_slug in slug_aliases:
            existing = city_to_icao.get(city_slug)
            if existing and existing != icao:
                logger.warning(
                    "duplicate Polymarket slug alias %s for %s and %s; keeping %s",
                    city_slug, existing, icao, existing,
                )
                continue
            city_to_icao[city_slug] = icao
    return city_to_icao


# --------------------------------------------------------------------------- output schema

def init_db() -> sqlite3.Connection:
    OUT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(OUT_DB, timeout=30.0)
    # WAL allows concurrent readers (snapshot queries) without blocking the writer.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS markets (
            market_slug    TEXT PRIMARY KEY,
            station_id     TEXT NOT NULL,
            market_date    TEXT NOT NULL,
            bracket_index  INTEGER NOT NULL,
            bracket_label  TEXT,
            yes_token_id   TEXT,
            no_token_id    TEXT,
            pmd_market_id  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_markets_station_date
            ON markets(station_id, market_date);

        CREATE TABLE IF NOT EXISTS prices (
            market_slug TEXT NOT NULL,
            side        TEXT NOT NULL,
            ts_unix     INTEGER NOT NULL,
            price       REAL,
            PRIMARY KEY (market_slug, side, ts_unix)
        );

        CREATE TABLE IF NOT EXISTS metrics (
            market_slug TEXT NOT NULL,
            ts_unix     INTEGER NOT NULL,
            volume      REAL,
            liquidity   REAL,
            spread      REAL,
            PRIMARY KEY (market_slug, ts_unix)
        );

        CREATE TABLE IF NOT EXISTS fetch_log (
            station_id  TEXT NOT NULL,
            market_date TEXT NOT NULL,
            market_slug TEXT NOT NULL DEFAULT '',
            endpoint    TEXT NOT NULL,
            status      TEXT NOT NULL,
            n_rows      INTEGER,
            http_code   INTEGER,
            error       TEXT,
            fetched_at  TEXT NOT NULL,
            PRIMARY KEY (station_id, market_date, market_slug, endpoint)
        );
    """)
    conn.commit()
    return conn


# --------------------------------------------------------------------------- gamma

def gamma_bulk_temperature_events(session: requests.Session, page_size: int = 100) -> list[dict]:
    """Paginate Gamma /events to get ALL highest-temperature events."""
    by_slug: dict[str, dict] = {}
    for archived in ("true", "false"):
        offset = 0
        while True:
            resp = session.get(
                f"{GAMMA_API}/events",
                params={
                    "tag_slug": "daily-temperature",
                    "limit": page_size,
                    "offset": offset,
                    "closed": "true",
                    "archived": archived,
                },
                timeout=30,
            )
            resp.raise_for_status()
            page = resp.json() or []
            if not isinstance(page, list) or not page:
                break
            # Filter to highest-temperature events only (tag may contain related events)
            kept = [e for e in page if (e.get("slug") or "").startswith("highest-temperature-in-")]
            for event in kept:
                slug = event.get("slug") or ""
                if slug:
                    by_slug.setdefault(slug, event)
            logger.info(
                "gamma bulk: archived=%s offset=%d page=%d kept=%d cumulative=%d",
                archived, offset, len(page), len(kept), len(by_slug),
            )
            if len(page) < page_size:
                break
            offset += page_size
            time.sleep(0.1)
    return list(by_slug.values())


_SLUG_DATE_RE = re.compile(
    r"^highest-temperature-in-(?P<city>[a-z0-9-]+?)-on-"
    r"(?P<month>january|february|march|april|may|june|july|august|september|october|november|december)-"
    r"(?P<day>\d{1,2})-(?P<year>\d{4})$"
)
_MONTHS = {m: i+1 for i, m in enumerate(
    "january february march april may june july august september october november december".split()
)}


def parse_event_slug(slug: str) -> tuple[str, date] | None:
    """Extract (city_slug, market_date) from event slug. None if it doesn't match."""
    m = _SLUG_DATE_RE.match(slug or "")
    if not m:
        return None
    try:
        d = date(int(m.group("year")), _MONTHS[m.group("month")], int(m.group("day")))
    except (ValueError, KeyError):
        return None
    return m.group("city"), d


def parse_brackets(event: dict) -> list[dict]:
    out = []
    for bi, m in enumerate(event.get("markets") or []):
        slug = m.get("slug") or ""
        if not slug:
            continue
        token_ids = m.get("clobTokenIds")
        if isinstance(token_ids, str):
            try: token_ids = json.loads(token_ids)
            except Exception: token_ids = []
        token_ids = token_ids or []
        question = m.get("question") or ""
        label = question.split("be ", 1)[-1].split(" on ", 1)[0] if "be " in question else question[:40]
        out.append({
            "bracket_index": bi,
            "market_slug": slug,
            "bracket_label": label,
            "yes_token_id": token_ids[0] if len(token_ids) > 0 else None,
            "no_token_id":  token_ids[1] if len(token_ids) > 1 else None,
        })
    return out


# --------------------------------------------------------------------------- polymarketdata.co

def pmd_get(session: requests.Session, api_key: str, path: str,
            params: dict, limiter: RateLimiter,
            retries: int = 3) -> tuple[dict | list | None, int, str]:
    """Returns (json, http_code, error). Rate-limited via shared limiter."""
    url = f"{PMD_API}{path}"
    headers = {"X-API-Key": api_key, "Accept": "application/json"}
    last_err = ""
    for attempt in range(retries):
        limiter.acquire()
        try:
            r = session.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                return r.json(), 200, ""
            except json.JSONDecodeError as e:
                return None, 200, f"json decode: {e}"
        if r.status_code == 429:
            # honor Retry-After if present, else exponential backoff
            ra = r.headers.get("Retry-After")
            try:
                wait = float(ra) if ra else 5.0 * (attempt + 1)
            except ValueError:
                wait = 5.0 * (attempt + 1)
            last_err = f"http 429 (retry-after={ra})"
            time.sleep(wait)
            continue
        if r.status_code in (500, 502, 503, 504):
            last_err = f"http {r.status_code}: {r.text[:200]}"
            time.sleep(2.0 * (attempt + 1))
            continue
        return None, r.status_code, r.text[:300]
    return None, -1, last_err or "retries exhausted"


def _parse_ts(ts) -> int | None:
    if isinstance(ts, str):
        try:
            if "T" in ts and not ts.endswith("Z") and "+" not in ts and "-" not in ts[10:]:
                ts = ts + "+00:00"  # naive ISO -> assume UTC
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except Exception:
            return None
    if isinstance(ts, (int, float)):
        n = int(ts)
        return n // 1000 if n > 10**12 else n
    return None


def _extract_price_rows(payload) -> list[tuple[str, int, float | None]]:
    """From /markets/{slug}/prices response, yield (side, ts_unix, price)."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    out: list[tuple[str, int, float | None]] = []
    if isinstance(data, dict):
        for side in ("Yes", "No"):
            for r in (data.get(side) or []):
                if not isinstance(r, dict):
                    continue
                ts = _parse_ts(r.get("t") or r.get("ts") or r.get("timestamp"))
                if ts is None:
                    continue
                p = r.get("p") if r.get("p") is not None else r.get("price")
                try:
                    p = float(p) if p is not None else None
                except (TypeError, ValueError):
                    p = None
                out.append((side, ts, p))
    return out


def _extract_metric_rows(payload) -> list[dict]:
    """From /markets/{slug}/metrics response, yield raw dicts (already flat)."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("data", "results", "metrics", "items"):
            v = payload.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


# --------------------------------------------------------------------------- worker

def already_done(conn: sqlite3.Connection, station_id: str, market_date: str,
                 market_slug: str | None, endpoint: str) -> bool:
    row = conn.execute(
        "SELECT status FROM fetch_log WHERE station_id=? AND market_date=? "
        "AND market_slug=? AND endpoint=?",
        (station_id, market_date, market_slug or "", endpoint),
    ).fetchone()
    return row is not None and row[0] in ("ok", "empty")


def log_fetch(conn: sqlite3.Connection, station_id: str, market_date: str,
              market_slug: str | None, endpoint: str, status: str,
              n_rows: int | None = None, http_code: int | None = None,
              error: str = ""):
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log "
        "(station_id, market_date, market_slug, endpoint, status, n_rows, http_code, error, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (station_id, market_date, market_slug or "", endpoint, status, n_rows, http_code, error,
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


PAGE_LIMIT = 200          # PMD hard maximum ("limit too large. Requested: 1000, Maximum: 200")
MAX_PAGES = 40            # safety stop; 40*200 = 8000 rows/market
WINDOW_DAYS = 7           # lead days before market_date; overridden by --window-days


def pmd_get_paged(session, api_key, path, params, limiter):
    """Follow PMD `metadata.next_cursor` pagination."""
    pages, code, err = [], 0, ""
    p = dict(params)
    p["limit"] = PAGE_LIMIT
    for _ in range(MAX_PAGES):
        js, code, err = pmd_get(session, api_key, path, p, limiter)
        if code != 200 or js is None:
            if pages:
                # keep the pages we already have; flag the truncation in the error text
                return pages, 200, f"partial: page {len(pages)+1} failed http {code}: {err[:120]}"
            return pages, code, err
        pages.append(js)
        cur = (js.get("metadata") or {}).get("next_cursor") if isinstance(js, dict) else None
        if not cur:
            break
        p["cursor"] = cur
    return pages, code, err


def fetch_market_history(api_key: str, market_slug: str, market_date: date,
                         pmd_session: requests.Session,
                         limiter: RateLimiter) -> dict:
    """Fetch prices + metrics for a single market. Returns dict with rows + status."""
    # Window: market_date 00:00Z minus WINDOW_DAYS through market_date 23:59Z (clamped to now)
    # Markets typically open ~7 days before resolution; clamping prevents 400s on open markets.
    start = datetime(market_date.year, market_date.month, market_date.day, tzinfo=timezone.utc) - timedelta(days=WINDOW_DAYS)
    end = datetime(market_date.year, market_date.month, market_date.day, 23, 59, 59, tzinfo=timezone.utc)
    now_utc = datetime.now(timezone.utc) - timedelta(seconds=60)
    if end > now_utc:
        end = now_utc
    if start >= end:
        return {"prices": [], "prices_err": "window_empty", "prices_code": 200, "pmd_market_id": None,
                "metrics": [], "metrics_err": "window_empty", "metrics_code": 200}
    params = {
        "start_ts": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_ts":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolution": RESOLUTION,
    }
    out: dict = {"prices": [], "prices_err": "", "prices_code": 0, "pmd_market_id": None,
                 "metrics": [], "metrics_err": "", "metrics_code": 0}

    ppages, pc, pe = pmd_get_paged(pmd_session, api_key, f"/markets/{market_slug}/prices", params, limiter)
    out["prices_code"], out["prices_err"] = pc, pe
    rows: list = []
    for pj in ppages:
        rows.extend(_extract_price_rows(pj))
        if isinstance(pj, dict) and pj.get("market_id"):
            out["pmd_market_id"] = pj.get("market_id")
    out["prices"] = rows

    mpages, mc, me = pmd_get_paged(pmd_session, api_key, f"/markets/{market_slug}/metrics", params, limiter)
    out["metrics_code"], out["metrics_err"] = mc, me
    mrows: list = []
    for mj in mpages:
        mrows.extend(_extract_metric_rows(mj))
    out["metrics"] = mrows
    return out


# --------------------------------------------------------------------------- probe

def probe(api_key: str):
    stations = load_wu_stations()
    if not stations:
        sys.exit("no WU stations")
    icao, slug_aliases = stations[0]
    sess = requests.Session()
    # Pull recent temperature events in bulk, pick one matching this station nearest to today
    all_events = gamma_bulk_temperature_events(sess)
    candidates = []
    for ev in all_events:
        parsed = parse_event_slug(ev.get("slug") or "")
        if not parsed:
            continue
        cs, dd = parsed
        if cs in slug_aliases:
            candidates.append((dd, ev))
    if not candidates:
        sys.exit(f"no temperature event found for {icao} ({', '.join(slug_aliases)}) in bulk listing")
    today = date.today()
    candidates.sort(key=lambda x: abs((x[0] - today).days))
    d, event = candidates[0]
    print(f"PROBE: station={icao} slug_aliases={','.join(slug_aliases)} market_date={d}")
    brackets = parse_brackets(event)
    print(f"event title: {event.get('title')!r}")
    print(f"brackets: {len(brackets)}")
    if not brackets:
        sys.exit("event had no brackets")

    print("\n--- Gamma brackets ---")
    for b in brackets:
        print(f"  [{b['bracket_index']:2d}] {b['bracket_label']:20s} yes={b.get('yes_price')} slug={b['market_slug']}")

    limiter = RateLimiter(PMD_RPM_LIMIT)
    by_active = sorted(brackets, key=lambda b: abs((b.get("yes_price") or 0) - 0.5))
    print("\n--- /prices probe across top-3 active brackets ---")
    for target in by_active[:3]:
        print(f"\n>> {target['market_slug']}  label={target['bracket_label']!r}  yes={target.get('yes_price')}")
        # Direct call so we can dump raw response
        start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - timedelta(days=7)
        end = datetime.now(timezone.utc) - timedelta(seconds=60)
        params = {"start_ts": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "end_ts":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "resolution": RESOLUTION}
        pj, pc, pe = pmd_get(sess, api_key, f"/markets/{target['market_slug']}/prices", params, limiter)
        print(f"   /prices  http={pc} type={type(pj).__name__} err={pe[:200]!r}")
        if pj is not None:
            preview = json.dumps(pj, indent=2)[:600]
            print(f"   raw: {preview}")


# --------------------------------------------------------------------------- main loop

def run(start_date: date, end_date: date, api_key: str, resume: bool, max_workers: int,
        skip_discovery: bool = False):
    stations = load_wu_stations()
    logger.info("WU stations: %d", len(stations))
    days = (end_date - start_date).days + 1
    logger.info("date range: %s -> %s (%d days)", start_date, end_date, days)

    conn = init_db()

    if skip_discovery:
        # Gamma is unreachable from the dev box; discovery runs server-side
        # (discover_gamma_events_server.py) and is merged into `markets` first.
        rows = conn.execute(
            "SELECT station_id, market_date, market_slug, bracket_index, bracket_label,"
            " yes_token_id, no_token_id FROM markets "
            "WHERE market_date BETWEEN ? AND ? ORDER BY station_id, market_date, bracket_index",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
        grouped: dict[tuple[str, str], list[dict]] = {}
        for icao, md, slug, bi, lbl, yt, nt in rows:
            grouped.setdefault((icao, md), []).append(
                {"market_slug": slug, "bracket_index": bi, "bracket_label": lbl,
                 "yes_token_id": yt, "no_token_id": nt})
        discovered = [(icao, date.fromisoformat(md), {}, bs)
                      for (icao, md), bs in grouped.items()]
        logger.info("skip-discovery: %d (station,date) pairs / %d markets from local `markets`",
                    len(discovered), sum(len(b) for *_, b in discovered))
        return _fetch_phase(conn, discovered, api_key, resume, max_workers)

    gamma_session = requests.Session()

    # Step 1: bulk-discover ALL temperature events with one paginated Gamma call,
    # then filter locally to (WU station, date in window).
    city_to_icao = _build_city_slug_index(stations)
    all_events = gamma_bulk_temperature_events(gamma_session)
    logger.info("Gamma bulk: fetched %d temperature events total", len(all_events))

    discovered: list[tuple[str, date, dict, list[dict]]] = []
    n_kept = n_skip_resume = 0
    for event in all_events:
        parsed = parse_event_slug(event.get("slug") or "")
        if not parsed:
            continue
        city_slug, d = parsed
        if d < start_date or d > end_date:
            continue
        icao = city_to_icao.get(city_slug)
        if not icao:
            continue
        md = d.isoformat()
        if resume and already_done(conn, icao, md, None, "event"):
            rows = conn.execute(
                "SELECT market_slug, bracket_index, bracket_label, yes_token_id, no_token_id "
                "FROM markets WHERE station_id=? AND market_date=?",
                (icao, md),
            ).fetchall()
            if rows:
                brackets = [{"market_slug": r[0], "bracket_index": r[1], "bracket_label": r[2],
                             "yes_token_id": r[3], "no_token_id": r[4]} for r in rows]
                discovered.append((icao, d, {}, brackets))
                n_skip_resume += 1
            continue

        brackets = parse_brackets(event)
        if not brackets:
            log_fetch(conn, icao, md, None, "event", "empty", 0, 200)
            continue
        for b in brackets:
            conn.execute(
                "INSERT OR REPLACE INTO markets "
                "(market_slug, station_id, market_date, bracket_index, bracket_label, "
                " yes_token_id, no_token_id) VALUES (?,?,?,?,?,?,?)",
                (b["market_slug"], icao, md, b["bracket_index"], b["bracket_label"],
                 b["yes_token_id"], b["no_token_id"]),
            )
        log_fetch(conn, icao, md, None, "event", "ok", len(brackets), 200)
        discovered.append((icao, d, event, brackets))
        n_kept += 1
    conn.commit()
    logger.info("Discovery: %d events matched WU stations + window  (resume-skipped=%d)",
                n_kept, n_skip_resume)

    n_markets = sum(len(b) for *_, b in discovered)
    logger.info("Discovered %d markets across %d (station,date) pairs", n_markets, len(discovered))
    if not n_markets:
        return
    return _fetch_phase(conn, discovered, api_key, resume, max_workers)


def _fetch_phase(conn, discovered, api_key: str, resume: bool, max_workers: int):
    # Step 2: fetch prices + metrics in parallel (shared 480 RPM limiter)
    limiter = RateLimiter(PMD_RPM_LIMIT)

    def task(icao: str, d: date, b: dict):
        sess = requests.Session()
        return icao, d, b, fetch_market_history(api_key, b["market_slug"], d, sess, limiter)

    pending = []
    for icao, d, _ev, brackets in discovered:
        md = d.isoformat()
        for b in brackets:
            slug = b["market_slug"]
            if resume and already_done(conn, icao, md, slug, "prices") and already_done(conn, icao, md, slug, "metrics"):
                continue
            pending.append((icao, d, b))
    logger.info("Fetching history for %d markets (%d workers, resolution=%s)",
                len(pending), max_workers, RESOLUTION)

    n_done = 0
    n_ok_prices = 0
    n_ok_metrics = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(task, icao, d, b) for icao, d, b in pending]
        for fut in as_completed(futs):
            icao, d, b, res = fut.result()
            md = d.isoformat()
            slug = b["market_slug"]

            # backfill PolymarketData market_id (audit later — drop if always present-and-derivable)
            if res.get("pmd_market_id"):
                conn.execute("UPDATE markets SET pmd_market_id=? WHERE market_slug=?",
                             (res["pmd_market_id"], slug))

            # prices: per-side time series
            if res["prices_code"] == 200:
                inserted = 0
                for side, ts, price in res["prices"]:
                    conn.execute(
                        "INSERT OR REPLACE INTO prices(market_slug, side, ts_unix, price) VALUES (?,?,?,?)",
                        (slug, side, ts, price),
                    )
                    inserted += 1
                log_fetch(conn, icao, md, slug, "prices",
                          "ok" if inserted else "empty", inserted, 200)
                if inserted: n_ok_prices += 1
            else:
                log_fetch(conn, icao, md, slug, "prices", "error",
                          0, res["prices_code"], res["prices_err"])

            # metrics: flat {t, volume, liquidity, spread}
            if res["metrics_code"] == 200:
                inserted = 0
                for row in res["metrics"]:
                    ts = _parse_ts(row.get("t") or row.get("ts") or row.get("timestamp"))
                    if ts is None:
                        continue
                    vol = row.get("volume")
                    liq = row.get("liquidity")
                    spr = row.get("spread")
                    conn.execute(
                        "INSERT OR REPLACE INTO metrics(market_slug, ts_unix, volume, liquidity, spread) "
                        "VALUES (?,?,?,?,?)",
                        (slug, ts,
                         float(vol) if vol is not None else None,
                         float(liq) if liq is not None else None,
                         float(spr) if spr is not None else None),
                    )
                    inserted += 1
                log_fetch(conn, icao, md, slug, "metrics",
                          "ok" if inserted else "empty", inserted, 200)
                if inserted: n_ok_metrics += 1
            else:
                log_fetch(conn, icao, md, slug, "metrics", "error",
                          0, res["metrics_code"], res["metrics_err"])

            n_done += 1
            if n_done % 50 == 0:
                conn.commit()
                rate = n_done / max(1, time.time() - t0)
                eta = (len(pending) - n_done) / max(rate, 0.01)
                logger.info("  progress %d/%d  prices_ok=%d metrics_ok=%d  rate=%.1f/s  eta=%ds",
                            n_done, len(pending), n_ok_prices, n_ok_metrics, rate, int(eta))
    conn.commit()

    write_manifest(conn)
    logger.info("DONE  markets=%d  prices_ok=%d  metrics_ok=%d  output=%s",
                n_done, n_ok_prices, n_ok_metrics, OUT_DB)


def write_manifest(conn: sqlite3.Connection):
    rows = conn.execute("""
        SELECT m.station_id, m.market_date, m.bracket_index, m.bracket_label, m.market_slug,
               (SELECT COUNT(*) FROM prices  p WHERE p.market_slug=m.market_slug AND p.side='Yes') AS n_yes,
               (SELECT COUNT(*) FROM prices  p WHERE p.market_slug=m.market_slug AND p.side='No')  AS n_no,
               (SELECT COUNT(*) FROM metrics x WHERE x.market_slug=m.market_slug)                  AS n_metrics
        FROM markets m
        ORDER BY m.station_id, m.market_date, m.bracket_index
    """).fetchall()
    OUT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with OUT_MANIFEST.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["station_id", "market_date", "bracket_index", "bracket_label",
                    "market_slug", "n_yes_prices", "n_no_prices", "n_metrics"])
        w.writerows(rows)
    logger.info("manifest -> %s (%d rows)", OUT_MANIFEST, len(rows))


# --------------------------------------------------------------------------- cli

def main():
    global WINDOW_DAYS
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=90, help="lookback window in days (default 90)")
    p.add_argument("--start", type=str, help="YYYY-MM-DD override")
    p.add_argument("--end", type=str, help="YYYY-MM-DD override (default: yesterday)")
    p.add_argument("--workers", type=int, default=16,
                   help="concurrent polymarketdata.co requests (rate-limited to 480 RPM globally)")
    p.add_argument("--resume", action="store_true", help="skip rows already in fetch_log with status=ok|empty")
    p.add_argument("--probe", action="store_true", help="fetch one recent market and print response shape")
    p.add_argument("--skip-discovery", action="store_true",
                   help="use `markets` rows already in the DB (Gamma unreachable from dev box)")
    p.add_argument("--window-days", type=int, default=WINDOW_DAYS,
                   help="lead days of history before market_date (default 7; 0 = market-day only)")
    args = p.parse_args()
    WINDOW_DAYS = args.window_days

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    api_key = load_env()

    if args.probe:
        probe(api_key)
        return

    end_date = date.fromisoformat(args.end) if args.end else (date.today() - timedelta(days=1))
    start_date = date.fromisoformat(args.start) if args.start else (end_date - timedelta(days=args.days - 1))
    if start_date > end_date:
        sys.exit("start > end")
    run(start_date, end_date, api_key, args.resume, args.workers, args.skip_discovery)


if __name__ == "__main__":
    main()
