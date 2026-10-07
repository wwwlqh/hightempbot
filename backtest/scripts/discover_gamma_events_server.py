"""Server-side Gamma discovery for daily-temperature markets (Jun-Aug 2026).

Runs ON THE ORACLE SERVER (gamma-api.polymarket.com is unreachable from the
Windows dev box). Uses per-(city,date) `?slug=` lookups because
`/events?tag_slug=...&offset=` is hard-capped at offset 2100 and only reaches
Feb-Apr 2026.

Writes a slim sqlite with the same `markets` schema as
backtest/data/polymarket_history.db so it can be merged locally.

Usage (on server):
    python3.11 discover_gamma_events_server.py --start 2026-06-01 --end 2026-08-08 \
        --out /tmp/pmd_discovery.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

GAMMA = "https://gamma-api.polymarket.com"
UA = "Mozilla/5.0 (compatible; htb-backtest/1.0)"
MONTHS = ("january february march april may june july august september october "
          "november december").split()

PROD_DB = "/home/opc/hightempbot/data/hightempbot.db"

_lock = threading.Lock()


def stations() -> list[tuple[str, str]]:
    c = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT icao, COALESCE(NULLIF(poly_slug,''), LOWER(REPLACE(city,' ','-'))) "
        "FROM enrolled_stations WHERE LOWER(resolution_source)='wu' ORDER BY icao"
    ).fetchall()
    c.close()
    return rows


def init(out: str) -> sqlite3.Connection:
    conn = sqlite3.connect(out, timeout=60)
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
        CREATE TABLE IF NOT EXISTS discovery_log (
            station_id TEXT, market_date TEXT, event_slug TEXT,
            status TEXT, n_brackets INTEGER, http_code INTEGER, error TEXT,
            PRIMARY KEY (station_id, market_date)
        );
    """)
    conn.commit()
    return conn


def get_event(slug: str, retries: int = 3):
    url = f"{GAMMA}/events?slug={slug}"
    last = ""
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode()), 200, ""
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                last = f"http {e.code}"
                time.sleep(2.0 * (a + 1))
                continue
            return None, e.code, f"http {e.code}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (a + 1))
    return None, -1, last


def brackets_of(event: dict) -> list[dict]:
    out = []
    for bi, m in enumerate(event.get("markets") or []):
        slug = m.get("slug") or ""
        if not slug:
            continue
        tok = m.get("clobTokenIds")
        if isinstance(tok, str):
            try:
                tok = json.loads(tok)
            except Exception:  # noqa: BLE001
                tok = []
        tok = tok or []
        q = m.get("question") or ""
        label = q.split("be ", 1)[-1].split(" on ", 1)[0] if "be " in q else q[:40]
        out.append({
            "bracket_index": bi, "market_slug": slug, "bracket_label": label,
            "yes_token_id": tok[0] if len(tok) > 0 else None,
            "no_token_id": tok[1] if len(tok) > 1 else None,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", default="/tmp/pmd_discovery.db")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    s, e = date.fromisoformat(a.start), date.fromisoformat(a.end)
    days = [s + timedelta(days=i) for i in range((e - s).days + 1)]
    sts = stations()
    conn = init(a.out)
    done = {(r[0], r[1]) for r in conn.execute(
        "SELECT station_id, market_date FROM discovery_log WHERE status IN ('ok','empty')")}
    jobs = [(icao, cs, d) for icao, cs in sts for d in days
            if (icao, d.isoformat()) not in done]
    print(f"stations={len(sts)} days={len(days)} jobs={len(jobs)}", flush=True)

    counter = {"n": 0, "ok": 0, "empty": 0, "err": 0, "brk": 0}
    t0 = time.time()

    def work(job):
        icao, cs, d = job
        slug = f"highest-temperature-in-{cs}-on-{MONTHS[d.month-1]}-{d.day}-{d.year}"
        js, code, err = get_event(slug)
        evs = js if isinstance(js, list) else []
        ev = evs[0] if evs else None
        return icao, d.isoformat(), slug, ev, code, err

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for icao, md, slug, ev, code, err in ex.map(work, jobs):
            with _lock:
                counter["n"] += 1
                if ev is None:
                    st = "empty" if code == 200 else "error"
                    counter["empty" if code == 200 else "err"] += 1
                    conn.execute("INSERT OR REPLACE INTO discovery_log VALUES (?,?,?,?,?,?,?)",
                                 (icao, md, slug, st, 0, code, err[:200]))
                else:
                    bs = brackets_of(ev)
                    for b in bs:
                        conn.execute(
                            "INSERT OR REPLACE INTO markets(market_slug,station_id,market_date,"
                            "bracket_index,bracket_label,yes_token_id,no_token_id) VALUES (?,?,?,?,?,?,?)",
                            (b["market_slug"], icao, md, b["bracket_index"],
                             b["bracket_label"], b["yes_token_id"], b["no_token_id"]))
                    counter["ok"] += 1
                    counter["brk"] += len(bs)
                    conn.execute("INSERT OR REPLACE INTO discovery_log VALUES (?,?,?,?,?,?,?)",
                                 (icao, md, slug, "ok" if bs else "empty", len(bs), 200, ""))
                if counter["n"] % 100 == 0:
                    conn.commit()
                    r = counter["n"] / max(1e-9, time.time() - t0)
                    print(f"  {counter['n']}/{len(jobs)} ok={counter['ok']} empty={counter['empty']} "
                          f"err={counter['err']} brackets={counter['brk']} "
                          f"rate={r:.1f}/s eta={int((len(jobs)-counter['n'])/max(r,0.01))}s", flush=True)
    conn.commit()
    print("DONE", counter, flush=True)
    print("markets rows:", conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0], flush=True)


if __name__ == "__main__":
    sys.exit(main())
