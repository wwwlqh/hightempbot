"""Report duplicate ``order_id`` rows in the ledger. ``init_db`` won't start
while duplicates exist.

    python scripts/check_ledger_order_id_duplicates.py /path/to/hightempbot.db

Exit 0: none. Exit 1: duplicates found.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def main(db_path: Path) -> int:
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT order_id, COUNT(*) AS n, GROUP_CONCAT(id, ',') AS bet_ids
            FROM ledger
            WHERE order_id IS NOT NULL
            GROUP BY order_id
            HAVING COUNT(*) > 1
            ORDER BY n DESC, order_id
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("OK: no duplicate order_id rows. Migration is safe to apply.")
        return 0

    print(f"FOUND {len(rows)} duplicate order_id group(s):")
    for row in rows:
        print(f"  order_id={row['order_id']!r}  count={row['n']}  bet_ids={row['bet_ids']}")
    print()
    print("Reconcile these rows before running the migration. For each group, decide which row to KEEP")
    print("(typically the row with the real bracket_label / station_id, not 'RECOVERED' sentinels) and")
    print("DELETE the others. Example:")
    print("  DELETE FROM ledger WHERE id IN (<duplicate_ids>);")
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/check_ledger_order_id_duplicates.py /path/to/hightempbot.db", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
