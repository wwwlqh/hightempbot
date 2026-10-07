"""Capital computation: wallet-derived in live, ledger-derived in dry-run.

LIVE MODE (operator decision 2026-05-20):
    realized_capital = max(wallet_balance + CLOB-submitted open cost,
                           initial_bankroll + realized PnL - return transfers)
    peak_realized_capital = ledger realized high-water, never below current basis
    deployable_capital = wallet_balance - sum(local-only PENDING)
    stake_basis_capital = realized_capital
    pending_exposure = sum(ledger PENDING bet_size) -- audit visibility

    Every successful wallet read appends to bankroll_peak for wallet audit
    history, but live drawdown peak comes from realized ledger PnL so old
    wallet samples or open positions cannot create fake drawdown.

DRY-RUN MODE:
    realized_capital = initial_bankroll + SUM(live_pnl)
    peak_realized_capital = MAX over running ledger PnL
    Unchanged from the original ledger-based model. Dry-run does not touch
    the wallet so dry-run drawdown can't influence live drawdown.

The wallet view IS net of CLOB-locked open orders (Polymarket V2 locks
collateral synchronously on place_order). The bot's own ledger PENDING
rows MAY or MAY NOT be on CLOB yet (record_bet commits before
execute_or_log calls place_order); deployable subtracts only the
``not-yet-on-CLOB`` set (order_id IS NULL) so PENDING-on-CLOB exposure
is not double-counted.
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
    """Capital view used by the betting pipeline + dashboard.

    ``wallet_available`` is True iff ``wallet_balance`` was successfully read
    from CLOB this snapshot. False means the read failed (live) or the snapshot
    is dry-run synthetic (no read attempted). Callers in live mode MUST treat
    ``wallet_available is False`` as a HALT condition rather than silently
    falling back to ledger capital (ce-code-review P1 #10).

    ce-code-review P1 #30: ``kw_only=True`` so the field order is not a public
    contract. Future additions can land in any position without silently
    shifting positional callers onto wrong field assignments.
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
    """SUM(bet_size) of live PENDING rows (excluding RECOVERED sentinel).

    ce-code-review P2 #47: canonical public version of the PENDING-exposure
    query. Previously duplicated in polymarket_transfer, pipeline (in-tick
    re-check), and v2_data — those should call this instead so changes to
    the exclusion set (RECOVERED, etc.) land in one place.
    """
    row = conn.execute(
        """SELECT COALESCE(SUM(bet_size), 0) AS x
        FROM ledger
        WHERE event_type = 'bet' AND outcome = 'PENDING'
          AND station_id != 'RECOVERED'"""
    ).fetchone()
    return float(row["x"] or 0.0)


def _ledger_local_pending_exposure(conn: sqlite3.Connection) -> float:
    """SUM(bet_size) of live PENDING rows that have NOT been submitted to CLOB.

    These are rows where record_bet committed but execute_or_log hasn't yet
    received an order_id from place_order. The wallet has NOT been debited
    for them yet, so they need to be subtracted from wallet_balance to get
    the true deployable capital. PENDING rows WITH order_id are already
    reflected in the wallet (CLOB locked the collateral).
    """
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
    """Return pUSD moved out through the dashboard transfer workflow.

    These transfers are operator capital withdrawals, not trading losses. They
    reduce the bankroll basis used for future stake sizing and the drawdown
    peak, so withdrawing $10 does not look like a strategy drawdown.
    """
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
    """SUM(bet_size) of live PENDING rows already submitted to CLOB.

    CLOB debits available pUSD when an order fills, so live wallet balance is
    cash after open-position cost. Adding this cost back gives the stake basis
    for sizing top-ups without counting unrealized PnL.
    """
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
    """Return live bankroll basis without charging open fills as drawdown.

    Wallet cash plus open cost basis is the normal live cash view. Floor that
    at ledger realized capital after return transfers so resolved-but-not-yet-
    redeemed proceeds do not show as drawdown or shrink the next 5%-of-capital
    target. Deployable cash is still wallet-based, so the placement path cannot
    spend unavailable cash.
    """
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
    """Return ``(capital, peak)`` exactly as the live drawdown gate computes them.

    Pure read-only mirror of :func:`get_capital_snapshot`'s live path, fed a
    stored wallet reading instead of a fresh CLOB call, for display surfaces
    (dashboard) that must agree with the halt gate. Keep in lockstep with the
    live branch of ``get_capital_snapshot`` — a fork between the two puts a
    drawdown number on the dashboard that the gate is not actually acting on.

    Why the ledger-only math is wrong for display after a re-deposit: an
    operator deposit reaches the wallet but has no ledger row, so
    ``initial_bankroll + SUM(pnl) - transfers`` reports $0 capital and a fake
    100% drawdown after a full withdrawal + re-fund, while the gate itself
    (wallet-floored) trades normally.
    """
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
    """Delete bankroll_peak rows older than ``retention_days``.

    ce-code-review P2 #31: the table accrues a row per live capital snapshot
    (~every tick). Without pruning it grows unbounded; only the recent window
    drives the wallet-derived peak. Keep enough history to detect multi-day
    drawdown patterns, drop the rest.
    """
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
    """Append the current wallet reading to bankroll_peak.

    Cheap insert; the index on sampled_at supports the MAX(wallet_balance)
    read pattern used by the dashboard and the peak query below. Failures
    here are non-fatal -- we log and continue so a transient DB lock never
    blocks a live tick.
    """
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
    """Session-anchored drawdown peak (operator fresh-start reset 2026-08-10).

    The halt gate protects the CURRENT session's bankroll, not all-time
    history: DD reads 0 at the session epoch, session losses ratchet it toward
    MAX_DD, recorded in-session withdrawals shrink the peak instead of looking
    like losses, and pre-epoch history (old peaks, old withdrawals — e.g. the
    2026-07-27 $84.05 return transfer) has no influence. Before this change
    the all-time walk had the live gate stuck at ~48% DD, silently halting the
    FLIP experiment from its first day.

    Mechanics: walk realized PnL over bets PLACED (``bet_ts``) at/after the
    epoch, reconstruct the session-start basis as
        ``seed = stake_basis - session_pnl + session_return_transfers``
    then ``peak = seed + max(0, cummax(session walk)) - session_returns``,
    floored at the current basis. A pre-epoch bet resolving post-epoch moves
    basis and seed equally (treated as an external flow, not session PnL).
    Unrecorded wallet flows (UI deposits/withdrawals) shift the seed instead
    of faking a drawdown; every trading loss is ledger-recorded, so real
    session losses can never hide from the gate.
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
    """Return the bankroll state.

    In live mode (``dry_run=False`` AND ``order_client`` provided):
      * deployable = wallet_balance - local-only PENDING (rows not yet on CLOB)
      * stake/realized basis = wallet_balance + submitted open cost, floored
        at ledger realized capital so resolved proceeds do not count as DD
      * peak = session-anchored realized high-water (``_session_ledger_peak``,
        epoch = DASHBOARD_SESSION_START_UTC), never below the current basis

    If the wallet read FAILS in live mode, return ``wallet_available=False``
    and zero-valued financials. The caller MUST treat this as a HALT (ce-code-
    review P1 #10) -- silent fallback to ledger capital is unsafe because the
    ledger doesn't know about external wallet movement (deposits, withdrawals,
    settled positions outside the bot's bookkeeping).

    In dry-run mode (or when no order_client is supplied), keep the legacy
    ledger-derived behavior so backtest replay and tests stay deterministic.
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
