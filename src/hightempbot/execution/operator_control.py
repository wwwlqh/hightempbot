"""Durable operator state for live dashboard controls."""

from __future__ import annotations

import json
import sqlite3
import weakref
from dataclasses import dataclass

from hightempbot.db.connection import utc_now_sql

LIVE = "LIVE"
STOPPED_PROCESSING = "STOPPED_PROCESSING"
TRANSFER_LOCK = "TRANSFER_LOCK"
ALLOWED_STATES = {LIVE, STOPPED_PROCESSING, TRANSFER_LOCK}


class OperatorControlError(RuntimeError):
    """Raised when an operator action violates a live-money guardrail."""


@dataclass(frozen=True)
class OperatorState:
    state: str
    boot_dry_run: bool
    reason: str
    updated_by: str
    updated_at: str
    # monotonic version for optimistic concurrency
    # control on the single-row operator_control_state. Every mutation reads
    # this then writes new = current + 1; a 0-rowcount UPDATE signals that
    # another writer overtook us, and the caller must retry.
    version: int = 0

    @property
    def processing_enabled(self) -> bool:
        return self.state == LIVE

    @property
    def transfer_locked(self) -> bool:
        return self.state == TRANSFER_LOCK

    def to_public_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "bootDryRun": self.boot_dry_run,
            "reason": self.reason,
            "updatedBy": self.updated_by,
            "updatedAt": self.updated_at,
            "processingEnabled": self.processing_enabled,
            "transferLocked": self.transfer_locked,
            "version": self.version,
        }


_verified_conns: weakref.WeakSet = weakref.WeakSet()


def ensure_operator_schema(conn: sqlite3.Connection) -> None:
    """Assert operator_control tables exist; schema.sql is authoritative.

    schema.sql (applied by db.connection.init_db) is the single source of
    truth — this helper just verifies the tables exist. After a connection
    has been verified once, results are cached per-conn: schema can't change
    at runtime, and the check fires once per tick × bracket on the hot path.
    """
    if conn in _verified_conns:
        return
    required = ("operator_control_state", "operator_control_events")
    for table in required:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"operator_control table {table!r} is missing; run init_db "
                "(hightempbot.db.connection.init_db) to apply schema.sql"
            )
    try:
        _verified_conns.add(conn)
    except TypeError:
        # Some Connection subclasses (e.g. MagicMock in tests) don't
        # support weak refs; skipping the cache is safe — only impact is
        # repeated checks for that conn.
        pass


def get_operator_state(
    conn: sqlite3.Connection,
    *,
    boot_dry_run: bool | None = None,
) -> OperatorState:
    """Return the durable operator-control state.

    ce-code-review P2 #37: ``boot_dry_run`` is sticky after boot. The caller
    that knows the boot mode (main.py at process start) passes it once; all
    later callers (processing_block_reason, dashboard reads) pass None so the
    stored flag isn't repeatedly rewritten — that overwrote a future
    operator-set boot mode every tick.
    """
    ensure_operator_schema(conn)
    if boot_dry_run is not None:
        # Only update when an explicit value is provided AND it differs from
        # what's already stored. Avoids spurious commits on every status read.
        current = conn.execute(
            "SELECT boot_dry_run FROM operator_control_state WHERE id=1"
        ).fetchone()
        stored = bool(current["boot_dry_run"]) if current else None
        if stored is None or stored != bool(boot_dry_run):
            conn.execute(
                "UPDATE operator_control_state SET boot_dry_run=? WHERE id=1",
                (1 if boot_dry_run else 0,),
            )
            conn.commit()
    row = conn.execute(
        "SELECT state, boot_dry_run, reason, updated_by, updated_at, "
        "COALESCE(version, 0) AS version "
        "FROM operator_control_state WHERE id=1"
    ).fetchone()
    if row is None:
        raise OperatorControlError("operator_control_state row missing")
    return OperatorState(
        state=row["state"],
        boot_dry_run=bool(row["boot_dry_run"]),
        reason=row["reason"] or "",
        updated_by=row["updated_by"] or "",
        updated_at=row["updated_at"] or "",
        version=int(row["version"] or 0),
    )


def _write_event(
    conn: sqlite3.Connection,
    *,
    action: str,
    from_state: str,
    to_state: str,
    actor: str,
    reason: str,
    detail: dict[str, object] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO operator_control_events
        (action, from_state, to_state, actor, reason, detail)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            action,
            from_state,
            to_state,
            actor,
            reason,
            json.dumps(detail or {}, sort_keys=True),
        ),
    )


def set_operator_state(
    conn: sqlite3.Connection,
    new_state: str,
    *,
    actor: str = "dashboard",
    reason: str = "",
    boot_dry_run: bool | None = None,
    detail: dict[str, object] | None = None,
) -> OperatorState:
    """Mutate the durable operator_control state with optimistic concurrency.

    ce-code-review P3 #71: reads the current ``version`` and conditional-
    UPDATEs ``WHERE version=?``. If 0 rows are affected, another writer
    overtook us between our read and write — we raise ``OperatorControlError
    ("concurrent state change detected; retry")`` and let the caller decide
    whether to retry. The new row version is ``current + 1``.
    """
    ensure_operator_schema(conn)
    if new_state not in ALLOWED_STATES:
        raise OperatorControlError(f"unsupported operator state: {new_state}")
    current = get_operator_state(conn, boot_dry_run=boot_dry_run)
    if new_state == LIVE and current.boot_dry_run:
        raise OperatorControlError("Start Processing cannot override DRY_RUN=True boot mode")
    now = utc_now_sql()
    new_version = current.version + 1
    cur = conn.execute(
        """
        UPDATE operator_control_state
        SET state=?, reason=?, updated_by=?, updated_at=?, boot_dry_run=?, version=?
        WHERE id=1 AND COALESCE(version, 0) = ?
        """,
        (
            new_state,
            reason,
            actor,
            now,
            1 if current.boot_dry_run else 0,
            new_version,
            current.version,
        ),
    )
    if int(cur.rowcount or 0) == 0:
        try:
            conn.rollback()
        except Exception:
            pass
        raise OperatorControlError("concurrent state change detected; retry")
    _write_event(
        conn,
        action=f"set_{new_state.lower()}",
        from_state=current.state,
        to_state=new_state,
        actor=actor,
        reason=reason,
        detail=detail,
    )
    conn.commit()
    return get_operator_state(conn)


def stop_processing(
    conn: sqlite3.Connection,
    *,
    actor: str = "dashboard",
    reason: str = "",
    boot_dry_run: bool | None = None,
) -> OperatorState:
    return set_operator_state(
        conn,
        STOPPED_PROCESSING,
        actor=actor,
        reason=reason or "operator stop processing",
        boot_dry_run=boot_dry_run,
    )


def start_processing(
    conn: sqlite3.Connection,
    *,
    actor: str = "dashboard",
    reason: str = "",
    boot_dry_run: bool | None = None,
) -> OperatorState:
    return set_operator_state(
        conn,
        LIVE,
        actor=actor,
        reason=reason or "operator start processing",
        boot_dry_run=boot_dry_run,
    )


def enter_transfer_lock(
    conn: sqlite3.Connection,
    *,
    actor: str = "dashboard",
    reason: str = "",
    boot_dry_run: bool | None = None,
) -> OperatorState:
    return set_operator_state(
        conn,
        TRANSFER_LOCK,
        actor=actor,
        reason=reason or "operator transfer lock",
        boot_dry_run=boot_dry_run,
    )


def clear_transfer_lock(
    conn: sqlite3.Connection,
    *,
    actor: str = "dashboard",
    reason: str = "",
    boot_dry_run: bool | None = None,
) -> OperatorState:
    return start_processing(
        conn,
        actor=actor,
        reason=reason or "operator clear transfer lock",
        boot_dry_run=boot_dry_run,
    )


def processing_block_reason(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
) -> str | None:
    # don't rewrite boot_dry_run on every gate check —
    # the value is set once at boot from main.py / set_operator_state.
    state = get_operator_state(conn)
    if state.state == LIVE:
        return None
    if state.state == STOPPED_PROCESSING:
        return "operator Stop Processing is active"
    if state.state == TRANSFER_LOCK:
        return "operator Transfer Lock is active"
    return f"operator state {state.state} blocks processing"


def latest_operator_events(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, object]]:
    ensure_operator_schema(conn)
    rows = conn.execute(
        """
        SELECT created_at, action, from_state, to_state, actor, reason, detail
        FROM operator_control_events
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict[str, object]] = []
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (TypeError, json.JSONDecodeError):
            detail = {}
        out.append(
            {
                "createdAt": row["created_at"],
                "action": row["action"],
                "fromState": row["from_state"],
                "toState": row["to_state"],
                "actor": row["actor"],
                "reason": row["reason"],
                "detail": detail,
            }
        )
    return out


def prune_audit_tables(
    conn: sqlite3.Connection,
    *,
    retention_days: int = 30,
) -> dict[str, int]:
    """Prune time-series audit tables to ``retention_days`` of history.

    ce-code-review P2 #50: the 5 audit tables (operator_control_events,
    live_readiness_reports, transfer_requests, redemption_requests,
    wallet_reconciliation_runs, wallet_reconciliation_records) had no
    retention policy and were growing unbounded. Operator-control events drive an audit log shown only for
    recent transitions; the rest are debugging snapshots. Keep 30 days.

    wallet_reconciliation_records carry a FK to runs; runs delete first and
    records cascade-or-orphan depending on the FK setup. We delete records by
    join on the run age so behaviour is correct either way.
    """
    deleted: dict[str, int] = {}
    cutoff = f"-{int(retention_days)} days"

    def _prune(table: str, col: str, where_extra: str = "") -> None:
        try:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE {col} < datetime('now', ?){where_extra}",
                (cutoff,),
            )
            deleted[table] = int(cur.rowcount or 0)
        except Exception:
            deleted[table] = 0

    try:
        _prune("operator_control_events", "created_at")
        _prune("live_readiness_reports", "created_at")
        # Don't prune SUBMITTING transfers; only terminal (SUBMITTED / FAILED /
        # cancelled) rows can be safely dropped.
        _prune(
            "transfer_requests",
            "created_at",
            " AND status IN ('SUBMITTED','FAILED','CANCELLED')",
        )
        _prune(
            "redemption_requests",
            "created_at",
            " AND status IN ('SUBMITTED','CONFIRMED','FAILED','SKIPPED')",
        )
        # Records first (FK to runs), then runs.
        try:
            cur = conn.execute(
                "DELETE FROM wallet_reconciliation_records "
                "WHERE run_id IN ("
                "  SELECT id FROM wallet_reconciliation_runs "
                "  WHERE sampled_at < datetime('now', ?)"
                ")",
                (cutoff,),
            )
            deleted["wallet_reconciliation_records"] = int(cur.rowcount or 0)
        except Exception:
            deleted["wallet_reconciliation_records"] = 0
        _prune("wallet_reconciliation_runs", "sampled_at")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    return deleted


def public_operator_payload(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
) -> dict[str, object]:
    # Status reads are intentionally non-mutating. main.py and state mutation
    # helpers are the only callers that should write the sticky boot mode.
    state = get_operator_state(conn)
    payload = state.to_public_dict()
    payload["events"] = latest_operator_events(conn, limit=10)
    return payload
