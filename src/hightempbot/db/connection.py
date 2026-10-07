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
    """Parse a UTC timestamp from SQLite text, ISO string, or epoch number.

    Accepts: None, int/float (Unix seconds), and str values in either ISO
    8601 (``YYYY-MM-DDTHH:MM:SS[+ZZ:ZZ]``) or SQLite canonical
    (``YYYY-MM-DD HH:MM:SS``) form. ``Z`` suffix is normalized. Naive
    datetimes are assumed UTC. Returns ``None`` on unparseable input.
    """
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
    """Best-effort pipeline_health insert. Swallows DB errors.

    Single shared writer for the table — every per-stage status row across
    the bot funnels through here. Best-effort by contract: callers must not
    fail because a health write failed.
    """
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
    # 5s busy_timeout converts immediate SQLITE_BUSY into bounded waits
    # under concurrent-writer contention (42 stations x 4 job kinds share
    # the DB). Without this the default is 0 -- the first contended write
    # raises OperationalError instead of waiting, and pipeline.py:302's
    # BEGIN IMMEDIATE silently falls through to autocommit, weakening the
    # slot-serialization invariant. CLI scripts already set this; this
    # propagates the pattern to the long-running bot. ce-code-review P1 #4.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _migrate_pred_bucket_history(conn: sqlite3.Connection) -> None:
    """Move bucket-key-unique LUT history to bracket-key-unique storage.

    The old schema used UNIQUE(station_id, local_date, pred_bucket_low), which
    collapsed multiple same-day brackets whenever their EMOS probabilities
    landed in the same bucket. Those lost rows cannot be recovered from the
    table, so preserve the old table as a backup and force LUT reseeding.
    """
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
    """Apply compatibility fixes required before schema.sql seed statements.

    ``CREATE TABLE IF NOT EXISTS`` is harmless for legacy tables, but seed
    INSERTs that name newly-added columns are not. Keep this tiny and only for
    old-table/new-seed ordering hazards; the full migration pass still runs
    after schema.sql.
    """
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
    """Apply schema migrations for existing production DBs.

    CREATE TABLE IF NOT EXISTS handles new tables automatically.
    This function handles column removals, index additions, and table drops
    that CREATE TABLE cannot express.
    """
    # SQLite >= 3.35.0 supports ALTER TABLE DROP COLUMN.
    # For older versions, silently skip — extra columns are harmless.
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

    # --- Rebuild bss_scores if it still has the legacy `month` column ---
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bss_scores)").fetchall()}
    if "month" in cols:
        conn.execute("DROP TABLE bss_scores")
        conn.execute("""
            CREATE TABLE bss_scores (
                station_id  TEXT NOT NULL PRIMARY KEY,
                bss         REAL,
                n_pairs     INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

    # --- Add missing indexes ---
    # Only indexes schema.sql does NOT already create belong here. The
    # operator_control_state `version` column is handled earlier by
    # _migrate_before_schema_seed (it must run before the schema seed
    # INSERTs name the column).
    # pipeline_health currently has no in-code reader (the v1 dashboard helper
    # that scanned it was deleted 2026-08-09). The (stage, station_id,
    # created_at DESC) index is retained for ad-hoc ops queries against the
    # table, which otherwise full-scan as it grows between monthly prunes.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pipeline_health_stage_station_created "
        "ON pipeline_health(stage, station_id, created_at DESC, id DESC)"
    )
    # `lookup_with_cumulative` filters on (station_id, pred_bucket_low, local_date < ?)
    # 11x per betting tick. The existing idx_pred_bucket_cell omits local_date,
    # forcing a post-scan over hundreds of historical rows per call.
    # NOTE: this must stay here rather than move to schema.sql —
    # _migrate_pred_bucket_history above renames the old table (taking any
    # schema.sql-created index with it) and rebuilds pred_bucket_history.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pred_bucket_cell_date "
        "ON pred_bucket_history(station_id, pred_bucket_low, local_date)"
    )

    conn.commit()


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create all tables from schema.sql and return the connection.

    Wraps ``executescript`` so a failure on the partial UNIQUE INDEX over
    ``ledger.order_id`` (added 2026-05-13) surfaces the duplicate rows the
    operator needs to resolve, instead of dying with a bare IntegrityError
    and a multi-line stack trace from inside sqlite3. Boot-blocking is the
    correct response to duplicates — silently skipping the index would let
    orphan-recovery write duplicates the next time a crash hits — but the
    failure must be actionable.
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
