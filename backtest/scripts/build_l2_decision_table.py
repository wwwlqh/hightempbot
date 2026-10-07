"""Enrich a decision table parquet with PMD historical L2 book ladders.

The join is leakage-safe: for each per-hour entry timestamp, it selects the
latest book snapshot at or before that timestamp, bounded by a max-lag window.

Default input/output:
    backtest/data/decision_table_may11plus.parquet
    backtest/data/decision_table_may11plus_l2.parquet
"""

from __future__ import annotations

import argparse
import bisect
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

BOOKS_DB = REPO_ROOT / "backtest" / "data" / "polymarket_books_10m.db"
BASE_PARQUET = REPO_ROOT / "backtest" / "data" / "decision_table_may11plus.parquet"
OUT_PARQUET = REPO_ROOT / "backtest" / "data" / "decision_table_may11plus_l2.parquet"

logger = logging.getLogger("build_l2_decision_table")


@dataclass
class TokenSnapshots:
    ts: list[int]
    bids: list[str]
    asks: list[str]


def _snapshots_for_slug(conn: sqlite3.Connection, slug: str) -> dict[str, TokenSnapshots]:
    by_token: dict[str, TokenSnapshots] = {
        "Yes": TokenSnapshots([], [], []),
        "No": TokenSnapshots([], [], []),
    }
    for token, ts, bids, asks in conn.execute(
        """
        SELECT token, ts_unix, bids_json, asks_json
        FROM book_snapshots
        WHERE market_slug=?
        ORDER BY token, ts_unix
        """,
        (slug,),
    ):
        bucket = by_token.get(str(token))
        if bucket is None:
            continue
        bucket.ts.append(int(ts))
        bucket.bids.append(str(bids))
        bucket.asks.append(str(asks))
    return by_token


def _select_snapshot(snaps: TokenSnapshots, entry_ts, max_lag_sec: int) -> tuple[str | None, str | None, int | None]:
    if entry_ts is None or pd.isna(entry_ts):
        return None, None, None
    try:
        entry_ts_i = int(entry_ts)
    except (TypeError, ValueError, OverflowError):
        return None, None, None
    idx = bisect.bisect_right(snaps.ts, entry_ts_i) - 1
    if idx < 0:
        return None, None, None
    snap_ts = snaps.ts[idx]
    if entry_ts_i - snap_ts > max_lag_sec:
        return None, None, None
    return snaps.bids[idx], snaps.asks[idx], snap_ts


def _drop_existing_l2_columns(df: pd.DataFrame) -> pd.DataFrame:
    prefixes = ("yes_ask_ladder_h", "no_ask_ladder_h", "yes_bid_ladder_h", "no_bid_ladder_h", "book_ts_h")
    return df.drop(columns=[c for c in df.columns if c.startswith(prefixes)], errors="ignore")


def enrich(base: Path, out: Path, books_db: Path, max_lag_minutes: int) -> pd.DataFrame:
    df = pd.read_parquet(base)
    df = _drop_existing_l2_columns(df)
    conn = sqlite3.connect(f"file:{books_db}?mode=ro", uri=True)
    max_lag_sec = int(max_lag_minutes) * 60

    stats = {
        "rows": len(df),
        "slug_with_books": 0,
        "yes_matched": 0,
        "no_matched": 0,
        "possible": 0,
    }

    new_cols: dict[str, list[object]] = {}
    for hour in range(24):
        for name in (
            f"yes_bid_ladder_h{hour}",
            f"yes_ask_ladder_h{hour}",
            f"no_bid_ladder_h{hour}",
            f"no_ask_ladder_h{hour}",
            f"book_ts_h{hour}",
        ):
            new_cols[name] = []

    for n, row in enumerate(df.itertuples(index=False), start=1):
        slug = str(getattr(row, "market_slug"))
        snapshots = _snapshots_for_slug(conn, slug)
        has_books = bool(snapshots["Yes"].ts or snapshots["No"].ts)
        if has_books:
            stats["slug_with_books"] += 1

        for hour in range(24):
            entry_ts = getattr(row, f"entry_ts_h{hour}", None)
            if entry_ts is not None and not pd.isna(entry_ts):
                stats["possible"] += 1

            yes_bids, yes_asks, yes_ts = _select_snapshot(snapshots["Yes"], entry_ts, max_lag_sec)
            no_bids, no_asks, no_ts = _select_snapshot(snapshots["No"], entry_ts, max_lag_sec)

            if yes_asks is not None:
                stats["yes_matched"] += 1
            if no_asks is not None:
                stats["no_matched"] += 1

            new_cols[f"yes_bid_ladder_h{hour}"].append(yes_bids)
            new_cols[f"yes_ask_ladder_h{hour}"].append(yes_asks)
            new_cols[f"no_bid_ladder_h{hour}"].append(no_bids)
            new_cols[f"no_ask_ladder_h{hour}"].append(no_asks)
            if yes_ts is not None and no_ts is not None:
                new_cols[f"book_ts_h{hour}"].append(min(yes_ts, no_ts))
            else:
                new_cols[f"book_ts_h{hour}"].append(yes_ts if yes_ts is not None else no_ts)

        if n % 1000 == 0:
            logger.info("joined %d/%d rows", n, len(df))

    conn.close()
    df = pd.concat([df, pd.DataFrame(new_cols)], axis=1)

    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)

    possible = max(stats["possible"], 1)
    logger.info(
        "wrote %s rows=%d cols=%d slugs_with_books=%d yes_match=%.1f%% no_match=%.1f%%",
        out,
        len(df),
        len(df.columns),
        stats["slug_with_books"],
        100.0 * stats["yes_matched"] / possible,
        100.0 * stats["no_matched"] / possible,
    )
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE_PARQUET)
    parser.add_argument("--out", type=Path, default=OUT_PARQUET)
    parser.add_argument("--books-db", type=Path, default=BOOKS_DB)
    parser.add_argument("--max-lag-minutes", type=int, default=90)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    enrich(args.base, args.out, args.books_db, args.max_lag_minutes)


if __name__ == "__main__":
    main()
