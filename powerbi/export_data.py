"""Build the Power BI extracts from the bot's SQLite database.

Each query in sql/ becomes one file in data/. Small tables are written as CSV;
the forecast table (about 550k rows) is written as Parquet.

Usage:
    python powerbi/export_data.py [--db PATH]
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

POWERBI_DIR = Path(__file__).resolve().parent
SQL_DIR = POWERBI_DIR / "sql"
DATA_DIR = POWERBI_DIR / "data"
DEFAULT_DB = POWERBI_DIR.parent / "data" / "hightempbot_server_latest.db"

PARQUET_TABLES = {"fact_forecast_error"}


def export_table(conn: sqlite3.Connection, sql_file: Path) -> tuple[Path, int]:
    df = pd.read_sql_query(sql_file.read_text(encoding="utf-8"), conn)
    if sql_file.stem in PARQUET_TABLES:
        path = DATA_DIR / f"{sql_file.stem}.parquet"
        df.to_parquet(path, index=False)
    else:
        path = DATA_DIR / f"{sql_file.stem}.csv"
        df.to_csv(path, index=False)
    return path, len(df)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Power BI extracts.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database to read")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    try:
        for sql_file in sorted(SQL_DIR.glob("*.sql")):
            path, rows = export_table(conn, sql_file)
            print(f"{sql_file.stem:<22} {rows:>9,} rows  {path.relative_to(POWERBI_DIR)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
