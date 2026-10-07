from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class HightempConnection(sqlite3.Connection):
    """SQLite connection subclass that supports weakref-based caches."""


def utc_now_sql() -> str:
    """Return current UTC time in SQLite-compatible text format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def safe_float(value: object) -> float:
    """Coerce ``value`` to float, returning 0.0 for None / non-numeric input."""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def parse_utc_timestamp(value: object) -> datetime | None:
    """UTC datetime from epoch seconds, ISO 8601 or SQLite text (naive = UTC); None if unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value)
    for candidate in (text, text.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def log_pipeline_health(
    conn: sqlite3.Connection,
    station_id: str | None,
    stage: str,
    status: str,
    message: str,
) -> None:
    """Insert a pipeline_health row; never raises."""
    try:
        conn.execute(
            "INSERT INTO pipeline_health (stage, station_id, status, message) "
            "VALUES (?, ?, ?, ?)",
            (stage, station_id, status, message),
        )
        conn.commit()
    except Exception:
        pass


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Return a SQLite connection with WAL mode and optimised pragmas."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        factory=HightempConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait up to 5s for a lock instead of failing immediately.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _migrate_pred_bucket_history(conn: sqlite3.Connection) -> None:
    """Re-key pred_bucket_history per bracket (the old key lost same-bucket
    brackets); keep the old table as a backup and force a LUT reseed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pred_bucket_history)").fetchall()}
    if "bracket_key" in cols:
        return

    conn.execute("DROP INDEX IF EXISTS idx_pred_bucket_station_date")
    conn.execute("DROP INDEX IF EXISTS idx_pred_bucket_cell")
    conn.execute("DROP TABLE IF EXISTS pred_bucket_history_legacy_bucket_unique")
    conn.execute("ALTER TABLE pred_bucket_history RENAME TO pred_bucket_history_legacy_bucket_unique")
    conn.execute(
        """
        CREATE TABLE pred_bucket_history (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id        TEXT NOT NULL,
            local_date        TEXT NOT NULL,
            pred_bucket_low   REAL NOT NULL,
            pred_bucket_high  REAL NOT NULL,
            bracket_low       REAL,
            bracket_high      REAL,
            bracket_key       TEXT,
            emos_p            REAL NOT NULL,
            hit               INTEGER NOT NULL,
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(station_id, local_date, bracket_key)
        )
        """
    )
    conn.execute("DELETE FROM lut_bucket_stats")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pred_bucket_station_date "
        "ON pred_bucket_history(station_id, local_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pred_bucket_cell "
        "ON pred_bucket_history(station_id, pred_bucket_low)"
    )


def _migrate_before_schema_seed(conn: sqlite3.Connection) -> None:
    """Add columns that schema.sql's seed INSERTs need on old tables."""
    try:
        op_cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(operator_control_state)").fetchall()
        }
        if op_cols and "version" not in op_cols:
            conn.execute(
                "ALTER TABLE operator_control_state "
                "ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
            )
            conn.commit()
    except sqlite3.Error:
        pass


def _migrate_db(conn: sqlite3.Connection) -> None:
    """Migrations schema.sql can't express: drops, added columns, indexes."""
    # DROP COLUMN needs SQLite ≥ 3.35; older versions keep the harmless columns.
    import sqlite3 as _sqlite3
    major, minor, _ = (int(x) for x in _sqlite3.sqlite_version.split("."))
    can_drop = (major > 3) or (major == 3 and minor >= 35)

    if can_drop:
        # --- Drop removed columns from signals ---
        cols = {r[1] for r in conn.execute("PRAGMA table_info(signals)").fetchall()}
        for col in ("gate_entropy", "gate_circuit_breaker"):
            if col in cols:
                conn.execute(f"ALTER TABLE signals DROP COLUMN {col}")

        # --- Drop promoted_at from enrolled_stations ---
        cols = {r[1] for r in conn.execute("PRAGMA table_info(enrolled_stations)").fetchall()}
        if "promoted_at" in cols:
            conn.execute("ALTER TABLE enrolled_stations DROP COLUMN promoted_at")

        # --- Drop removed columns from retrain_history ---
        cols = {r[1] for r in conn.execute("PRAGMA table_info(retrain_history)").fetchall()}
        for col in ("forecast_days_added", "bss_before", "bss_after",
                     "n_pairs_before", "n_pairs_after"):
            if col in cols:
                conn.execute(f"ALTER TABLE retrain_history DROP COLUMN {col}")

    # --- Drop orphaned tables ---
    conn.execute("DROP TABLE IF EXISTS optimal_params")

    _migrate_pred_bucket_history(conn)

    # --- Add realized_edge + LUT-calibration forensic columns to ledger ---
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ledger)").fetchall()}
    if "realized_edge" not in cols:
        conn.execute("ALTER TABLE ledger ADD COLUMN realized_edge REAL")
    for col, decl in (
        ("prob_safe_floor", "REAL"),
        ("pred_bucket_low", "REAL"),
        ("pred_bucket_high", "REAL"),
        ("n_bucket", "INTEGER"),
        ("transaction_hash", "TEXT"),
        ("verify_attempts", "INTEGER DEFAULT 0"),
        ("verification_downgraded", "INTEGER DEFAULT 0"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE ledger ADD COLUMN {col} {decl}")

    # --- Drop retired Wilson CI columns (lcb/ucb) on ledger and lut_bucket_stats ---
    if can_drop:
        for col in ("lcb", "ucb"):
            if col in cols:
                conn.execute(f"ALTER TABLE ledger DROP COLUMN {col}")
        lut_cols = {r[1] for r in conn.execute("PRAGMA table_info(lut_bucket_stats)").fetchall()}
        for col in ("lcb", "ucb"):
            if col in lut_cols:
                conn.execute(f"ALTER TABLE lut_bucket_stats DROP COLUMN {col}")

    # --- Indexes not created by schema.sql ---
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pipeline_health_stage_station_created "
        "ON pipeline_health(stage, station_id, created_at DESC, id DESC)"
    )
    # For lookup_with_cumulative. Lives here because _migrate_pred_bucket_history
    # rebuilds the table.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pred_bucket_cell_date "
        "ON pred_bucket_history(station_id, pred_bucket_low, local_date)"
    )

    conn.commit()


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create/migrate the schema and return a connection.

    Refuses to start if duplicate ledger order_ids block the unique index,
    pointing at scripts/check_ledger_order_id_duplicates.py.
    """
    conn = get_connection(db_path)
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    _migrate_before_schema_seed(conn)
    try:
        conn.executescript(schema_sql)
    except sqlite3.IntegrityError as exc:
        duplicates: list[sqlite3.Row] = []
        try:
            duplicates = conn.execute(
                "SELECT order_id, COUNT(*) AS n FROM ledger "
                "WHERE order_id IS NOT NULL "
                "GROUP BY order_id HAVING n > 1 "
                "ORDER BY n DESC, order_id LIMIT 10"
            ).fetchall()
        except sqlite3.Error:
            pass
        sample = (
            "; ".join(f"order_id={r['order_id']} (n={r['n']})" for r in duplicates)
            or "ledger query unavailable — inspect the failing statement above"
        )
        raise RuntimeError(
            "init_db: schema.sql failed during executescript "
            f"({exc!s}). Likely culprit: the partial UNIQUE INDEX on "
            "ledger.order_id (idx_ledger_order_id) cannot build because "
            "duplicate non-NULL order_id rows exist. Resolve and retry: "
            "`python scripts/check_ledger_order_id_duplicates.py <db_path>` "
            "(see CLAUDE.md Deploy Commands). Top offenders: " + sample
        ) from exc
    _migrate_db(conn)
    return conn
