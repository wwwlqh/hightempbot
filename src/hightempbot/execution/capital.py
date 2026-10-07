"""Capital and drawdown basis.

Live:
    realized = max(wallet + open cost on CLOB, bankroll + realized PnL − withdrawals)
    peak     = session realized-ledger high-water, never below realized
    deployable = wallet − PENDING rows not yet sent to CLOB
Dry-run: everything comes from the ledger.

The peak uses the ledger, not wallet samples, so open positions and stale
reads can't fake a drawdown. The wallet already excludes collateral locked
by CLOB orders, so only rows without an order_id are subtracted.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hightempbot.execution.walker import OrderClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class CapitalSnapshot:
    """Capital view for the pipeline and dashboard.

    In live mode ``wallet_available=False`` (wallet read failed) must halt
    betting rather than fall back to ledger capital.
    """

    realized_pnl: float
    pending_exposure: float
    realized_capital: float
    deployable_capital: float
    stake_basis_capital: float
    peak_realized_capital: float
    wallet_balance: float | None = None
    wallet_available: bool = False
    capital_source: str = "ledger"
    data_api_position_value: float = 0.0
    data_api_reconciliation_warnings: tuple[str, ...] = ()


def live_pending_notional(conn: sqlite3.Connection) -> float:
    """SUM(bet_size) of live PENDING rows, excluding RECOVERED sentinels."""
    row = conn.execute(
        """SELECT COALESCE(SUM(bet_size), 0) AS x
        FROM ledger
        WHERE event_type = 'bet' AND outcome = 'PENDING'
          AND station_id != 'RECOVERED'"""
    ).fetchone()
    return float(row["x"] or 0.0)


def _ledger_local_pending_exposure(conn: sqlite3.Connection) -> float:
    """SUM(bet_size) of live PENDING rows not yet sent to CLOB (no order_id),
    which the wallet hasn't been debited for."""
    row = conn.execute(
        """SELECT COALESCE(SUM(bet_size), 0) AS x
        FROM ledger
        WHERE event_type = 'bet' AND outcome = 'PENDING'
          AND station_id != 'RECOVERED'
          AND order_id IS NULL"""
    ).fetchone()
    return float(row["x"] or 0.0)


def live_local_pending_notional(conn: sqlite3.Connection) -> float:
    """SUM(bet_size) of live PENDING rows not yet submitted to CLOB."""
    return _ledger_local_pending_exposure(conn)


RETURN_TRANSFER_CAPITAL_STATUSES = ("SUBMITTING", "SUBMITTED")
SUBMITTING_TRANSFER_CAPITAL_TTL_S = 15 * 60


def return_transfer_status_filter(
    statuses: tuple[str, ...] = RETURN_TRANSFER_CAPITAL_STATUSES,
) -> tuple[str, tuple[object, ...]]:
    """SQL filter for capital-affecting return-transfer statuses."""
    status_values = tuple(str(status).upper() for status in statuses)
    clauses: list[str] = []
    params: list[object] = []
    non_submitting = [status for status in status_values if status != "SUBMITTING"]
    if non_submitting:
        placeholders = ",".join("?" for _ in non_submitting)
        clauses.append(f"status IN ({placeholders})")
        params.extend(non_submitting)
    if "SUBMITTING" in status_values:
        clauses.append("(status = 'SUBMITTING' AND created_at >= datetime('now', ?))")
        params.append(f"-{SUBMITTING_TRANSFER_CAPITAL_TTL_S} seconds")
    return " OR ".join(clauses) if clauses else "0", tuple(params)


def return_transfer_notional(
    conn: sqlite3.Connection,
    *,
    since_utc: str | None = None,
    statuses: tuple[str, ...] = RETURN_TRANSFER_CAPITAL_STATUSES,
) -> float:
    """pUSD withdrawn via the dashboard; lowers basis and peak, not counted as loss."""
    if not statuses:
        return 0.0
    where, params = return_transfer_status_filter(statuses)
    params = list(params)
    if since_utc:
        where = f"({where}) AND created_at > ?"
        params.append(since_utc)
    try:
        row = conn.execute(
            f"SELECT COALESCE(SUM(amount_usd), 0) AS x FROM transfer_requests WHERE {where}",
            params,
        ).fetchone()
    except sqlite3.Error:
        return 0.0
    return float(row["x"] or 0.0)


def _ledger_open_cost_basis(conn: sqlite3.Connection) -> float:
    """SUM(bet_size) of live PENDING rows already on CLOB (open cost basis)."""
    row = conn.execute(
        """SELECT COALESCE(SUM(bet_size), 0) AS x
        FROM ledger
        WHERE event_type = 'bet' AND outcome = 'PENDING'
          AND station_id != 'RECOVERED'
          AND order_id IS NOT NULL"""
    ).fetchone()
    return float(row["x"] or 0.0)


def _ledger_realized_pnl(conn: sqlite3.Connection) -> float:
    """SUM(pnl) over live terminal rows."""
    row = conn.execute(
        """SELECT COALESCE(SUM(COALESCE(pnl, 0)), 0) AS total_pnl
        FROM ledger
        WHERE event_type = 'bet'
        AND outcome NOT IN ('PENDING', 'CANCELLED')"""
    ).fetchone()
    return float(row["total_pnl"] or 0.0)


def live_capital_basis(
    wallet_balance: float,
    open_cost_basis: float,
    ledger_realized_capital: float,
) -> float:
    """Wallet + open cost, floored at ledger realized capital (so unredeemed
    winnings don't look like drawdown)."""
    wallet_cost_basis = float(wallet_balance) + float(open_cost_basis)
    ledger_basis = max(0.0, float(ledger_realized_capital))
    return max(wallet_cost_basis, ledger_basis)


def live_gate_capital_view(
    conn: sqlite3.Connection,
    initial_bankroll: float,
    *,
    wallet_balance: float,
    api_position_value: float | None = None,
) -> tuple[float, float]:
    """``(capital, peak)`` as the live halt gate computes them, from a stored
    wallet reading. Keep in sync with ``get_capital_snapshot``."""
    realized_pnl = _ledger_realized_pnl(conn)
    return_transfers = return_transfer_notional(conn)
    ledger_realized = max(
        0.0, float(initial_bankroll) + realized_pnl - return_transfers
    )
    if api_position_value is not None:
        stake_basis = max(
            max(0.0, float(wallet_balance) + float(api_position_value)),
            ledger_realized,
        )
    else:
        stake_basis = live_capital_basis(
            float(wallet_balance),
            _ledger_open_cost_basis(conn),
            ledger_realized,
        )
    return stake_basis, _session_ledger_peak(conn, stake_basis)


def _safe_snapshot_float(snapshot: dict, key: str) -> float:
    try:
        return float(snapshot.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _latest_trusted_api_position_value(
    conn: sqlite3.Connection,
    *,
    wallet_address: str = "",
) -> tuple[float | None, tuple[str, ...]]:
    """Return trusted Data API open-position value when the latest snapshot is fresh."""
    try:
        from hightempbot.persistence.wallet_reconciliation import latest_wallet_snapshot
    except Exception:
        return None, ()
    try:
        snapshot = latest_wallet_snapshot(
            conn,
            wallet_address=wallet_address,
            freshness_ttl_s=300,
        )
    except Exception:
        return None, ()
    if not snapshot:
        return None, ()
    sources = snapshot.get("sourcesChecked") if isinstance(snapshot.get("sourcesChecked"), dict) else {}
    if not snapshot.get("fresh") or not sources.get("dataApiPositions"):
        return None, tuple(str(w) for w in snapshot.get("dataApiReconciliationWarnings") or ())
    return (
        _safe_snapshot_float(snapshot, "dataApiTrustedOpenPositionsValueUsd"),
        tuple(str(w) for w in snapshot.get("dataApiReconciliationWarnings") or ()),
    )


def prune_bankroll_peak(
    conn: sqlite3.Connection,
    *,
    retention_days: int = 30,
) -> int:
    """Delete bankroll_peak rows older than ``retention_days``."""
    try:
        cur = conn.execute(
            "DELETE FROM bankroll_peak "
            "WHERE sampled_at < datetime('now', ?)",
            (f"-{int(retention_days)} days",),
        )
        deleted = int(cur.rowcount or 0)
        conn.commit()
        return deleted
    except Exception:
        logger.warning("Failed to prune bankroll_peak", exc_info=True)
        return 0


def _persist_wallet_peak(
    conn: sqlite3.Connection,
    wallet_balance: float,
    realized_pnl: float,
    pending_exposure: float,
) -> None:
    """Record a wallet reading in bankroll_peak. Failures are logged, not raised."""
    try:
        conn.execute(
            "INSERT INTO bankroll_peak (wallet_balance, realized_pnl, pending_exposure) "
            "VALUES (?, ?, ?)",
            (float(wallet_balance), float(realized_pnl), float(pending_exposure)),
        )
        conn.commit()
    except Exception:
        logger.warning("Failed to persist bankroll_peak row", exc_info=True)


def _ledger_peak(conn: sqlite3.Connection, initial_bankroll: float) -> float:
    """Peak realized capital from the live ledger PnL window (dry-run path)."""
    row = conn.execute(
        """
        SELECT MAX(running_capital) AS peak
        FROM (
            SELECT
                ? + SUM(COALESCE(pnl, 0)) OVER (ORDER BY bet_ts, id) AS running_capital
            FROM ledger
            WHERE event_type = 'bet'
            AND outcome NOT IN ('PENDING', 'CANCELLED')
        )
        """,
        (initial_bankroll,),
    ).fetchone()
    peak = float(row["peak"]) if row["peak"] is not None else float(initial_bankroll)
    return max(peak, float(initial_bankroll))


def _session_ledger_peak(
    conn: sqlite3.Connection,
    stake_basis: float,
    *,
    session_start_utc: str | None = None,
) -> float:
    """Drawdown peak for the current session (since DASHBOARD_SESSION_START_UTC).

    ``seed = stake_basis − session_pnl + session_withdrawals``;
    ``peak = seed + max(0, cummax(session pnl walk)) − session_withdrawals``,
    floored at the current basis. Unrecorded wallet flows move the seed, so
    they never look like drawdown; trading losses always do.
    """
    if session_start_utc is None:
        from hightempbot.execution import strategy_constants

        session_start_utc = strategy_constants.DASHBOARD_SESSION_START_UTC
    rows = conn.execute(
        """
        SELECT COALESCE(pnl, 0) AS pnl
        FROM ledger
        WHERE event_type = 'bet'
          AND outcome NOT IN ('PENDING', 'CANCELLED')
          AND bet_ts >= ?
        ORDER BY bet_ts, id
        """,
        (session_start_utc,),
    ).fetchall()
    cum = 0.0
    cummax = 0.0
    for row in rows:
        cum += float(row["pnl"] or 0.0)
        cummax = max(cummax, cum)
    session_returns = return_transfer_notional(conn, since_utc=session_start_utc)
    seed = float(stake_basis) - cum + session_returns
    peak = seed + max(0.0, cummax) - session_returns
    return max(peak, float(stake_basis), 0.0)


def get_capital_snapshot(
    conn: sqlite3.Connection,
    initial_bankroll: float,
    *,
    order_client: "OrderClient | None" = None,
    dry_run: bool = True,
) -> CapitalSnapshot:
    """Current capital snapshot (see module docstring).

    Live needs ``order_client``; a failed wallet read returns zeros with
    ``wallet_available=False``, which callers must treat as a halt. Without
    a client (dry-run), everything comes from the ledger.
    """
    realized_pnl = _ledger_realized_pnl(conn)
    pending_exposure = live_pending_notional(conn)

    if dry_run or order_client is None:
        realized_capital = float(initial_bankroll) + realized_pnl
        deployable_capital = realized_capital - pending_exposure
        peak = _ledger_peak(conn, initial_bankroll)
        return CapitalSnapshot(
            realized_pnl=realized_pnl,
            pending_exposure=pending_exposure,
            realized_capital=realized_capital,
            deployable_capital=deployable_capital,
            stake_basis_capital=realized_capital,
            peak_realized_capital=peak,
            wallet_balance=None,
            wallet_available=False,
        )

    # Live mode: try the wallet.
    try:
        wallet_balance = order_client.check_balance()
    except Exception:
        logger.error("check_balance raised in capital snapshot", exc_info=True)
        wallet_balance = None

    if wallet_balance is None:
        return CapitalSnapshot(
            realized_pnl=realized_pnl,
            pending_exposure=pending_exposure,
            realized_capital=0.0,
            deployable_capital=0.0,
            stake_basis_capital=0.0,
            peak_realized_capital=0.0,
            wallet_balance=None,
            wallet_available=False,
        )

    wallet_balance = float(wallet_balance)
    local_pending = _ledger_local_pending_exposure(conn)
    open_cost_basis = _ledger_open_cost_basis(conn)
    return_transfers = return_transfer_notional(conn)
    ledger_realized_capital = max(0.0, float(initial_bankroll) + realized_pnl - return_transfers)
    deployable = max(0.0, wallet_balance - local_pending)
    wallet_address = str(getattr(order_client, "_funder", "") or "")
    api_position_value, api_warnings = _latest_trusted_api_position_value(
        conn,
        wallet_address=wallet_address,
    )
    if api_position_value is not None:
        wallet_api_basis = max(0.0, wallet_balance + api_position_value)
        stake_basis = max(wallet_api_basis, ledger_realized_capital)
        capital_source = "polymarket_data_api"
    else:
        stake_basis = live_capital_basis(
            wallet_balance,
            open_cost_basis,
            ledger_realized_capital,
        )
        capital_source = "ledger_fallback"
    _persist_wallet_peak(conn, wallet_balance, realized_pnl, pending_exposure)
    peak = _session_ledger_peak(conn, stake_basis)
    return CapitalSnapshot(
        realized_pnl=realized_pnl,
        pending_exposure=pending_exposure,
        realized_capital=stake_basis,
        deployable_capital=deployable,
        stake_basis_capital=stake_basis,
        peak_realized_capital=peak,
        wallet_balance=wallet_balance,
        wallet_available=True,
        capital_source=capital_source,
        data_api_position_value=api_position_value or 0.0,
        data_api_reconciliation_warnings=api_warnings,
    )
