"""Recovery helpers for stale live-money request rows."""

from __future__ import annotations

import sqlite3

from hightempbot.db.connection import utc_now_sql

_SUBMITTING_REQUEST_TABLES = frozenset({
    "transfer_requests",
    "redemption_requests",
})


def recover_stale_submitting_requests(
    conn: sqlite3.Connection,
    *,
    table: str,
    noun: str,
    max_age_s: int,
) -> int:
    """Fail old pre-relayer SUBMITTING rows for one request table."""
    if table not in _SUBMITTING_REQUEST_TABLES:
        raise ValueError(f"unsupported submitting-request table: {table}")
    age_s = max(1, int(max_age_s))
    error = (
        f"Stale SUBMITTING {noun} recovered: no relayer response was recorded "
        f"within {age_s} seconds."
    )
    cur = conn.execute(
        f"""
        UPDATE {table}
        SET status='FAILED',
            updated_at=?,
            error=COALESCE(NULLIF(error, ''), ?)
        WHERE status='SUBMITTING'
          AND (relayer_tx_id IS NULL OR relayer_tx_id = '')
          AND datetime(created_at) <= datetime('now', ?)
        """,
        (utc_now_sql(), error, f"-{age_s} seconds"),
    )
    conn.commit()
    return int(cur.rowcount or 0)
