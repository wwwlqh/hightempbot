"""Shared dataclasses for the execution pipeline.

These types are used across decision, order, ledger, and pipeline modules
to avoid cross-module import coupling.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BetSignal:
    """A single bracket evaluation result (pass or fail)."""

    station_id: str
    target_date: str          # ISO date (station local time)
    horizon: int              # 1, 2, or 3
    bracket_idx: int
    threshold: float          # upper bracket boundary
    bracket_label: str        # e.g. "66-68°F YES"
    side: str                 # "YES" or "NO"
    p_model: float
    p_market: float           # fill price (best ask for YES, best bid for NO)
    edge: float
    # Pre-walk stake target in USD. Named ``bet_size_usd`` to reflect the
    # 2026-04-24 retirement of Kelly sizing — the field used to be
    # ``kelly_size_usd`` and the legacy SQL columns (``ledger.kelly_size``
    # and ``signals.kelly_size_usd``) keep that name on production SQLite
    # 3.34.1 which can't cleanly RENAME COLUMN. The value is now
    # ``capital * cfg.capital_frac`` after stake-cap and walker trim.
    bet_size_usd: float
    fill_price: float
    volume_usd: float | None
    market_id: str = ""
    token_id: str = ""
    limit_price: float = 0.0   # highest ask level to fill across (for walk-the-book orders)
    bracket_low: float | None = None   # Polymarket bracket lower bound (for resolution matching)
    bracket_high: float | None = None  # Polymarket bracket upper bound (for resolution matching)
    # "F" or "C" — native unit of bracket_low/high. Empty string is the
    # sentinel for "unknown / not yet inferred"; the WU consensus gate treats
    # it as fail-closed rather than guessing °C and producing a 30°C error on
    # US °F brackets.
    bracket_unit: str = ""
    # Side-aware LUT-calibrated point probability used as the safe-floor for
    # the edge calculation. YES: bucket observed rate. NO: 1 - bucket observed rate.
    prob_safe_floor: float | None = None
    pred_bucket: tuple[float, float] | None = None
    n_bucket: int = 0
    gate_results: dict[str, bool | None] = field(default_factory=dict)
    passed_all_gates: bool = False
    wu_forecast_c: float | None = None  # WU live forecast tmax in °C at gate time (telemetry)
    # Raw WU consensus verdict regardless of mode: True = WU agrees with bet
    # side, False = disagrees, None = WU unavailable. SHADOW writes this for
    # telemetry without affecting gate_results / passed_all_gates; BLOCK writes
    # this AND a bool into gate_results["wu_consensus"] (None mapped to False
    # so unavailable is fail-closed).
    wu_consensus_verdict: bool | None = None

    # --- 3-strategy stack (NO / YMID / TAIL) — added 2026-05-06 ---
    # `strategy` is the canonical key written to event_detail.strategy and used
    # by tp_sl_monitor + idempotency. Empty string is the legacy/uninitialized
    # sentinel; all new bets emitted by the per-strategy router populate it.
    strategy: str = ""
    # Name of the signal column actually used for the edge calc, e.g. "p_E",
    # "p_Shrink_n50", "tail_vote_avg". Persisted to event_detail.signal_used
    # so dashboards and post-hoc analyses can filter by signal.
    signal_used: str = ""
    # The chosen signal's numeric value at decision time. Persisted for parity
    # checks against the backtest decision table. For TAIL this is the
    # average of the 4 voting signals.
    signal_value: float | None = None

    # Pre-walk top-of-book price recorded at scanner time. Used by the order
    # retry/dry-run paths to anchor the ``max_walk_price`` cap to the
    # scanner-time top rather than re-anchoring to the live top on every
    # retry (which would let the cap drift with the book and silently violate
    # the operator's intended slippage leash). None means "no anchor
    # recorded" — retry walker falls back to anchoring to the live top. A 0.0
    # value will fail the walker rather than fall back (walker rejects
    # non-positive anchors).
    #
    # Cross-tick top-up (2026-05-11): on first-fill rows this is the
    # scanner-time top; on top-up rows it is the slot's first-fill anchor,
    # read back from event_detail.entry_top_price of the earliest non-cancelled
    # row on the slot. Persisted into event_detail on every record_bet.
    entry_top_price: float | None = None

    # Cumulative non-cancelled exposure on the slot at decision time (before
    # this signal is recorded). 0.0 on first-fill, > 0 on top-up. The
    # pipeline reads this when deciding whether to bump CycleResult.n_topup.
    slot_filled_pre: float = 0.0

    # Decision-time book-walked VWAP of THIS fill (the realized per-share price
    # the edge-preserving walker produced at scanner time). Distinct from
    # ``fill_price`` (the /price scanner quote in dry-run, the matched avg in
    # live) — neither equals the book walk. Persisted to
    # ``event_detail.entry_fill_vwap`` on every record_bet; ``slot_state`` reads
    # the EARLIEST non-cancelled row's value back as the slip anchor for later
    # top-ups. None on SKIP signals (no walk) and legacy rows. Added 2026-05-29
    # for the VWAP-slip-from-anchor guard (L2 champion port).
    entry_fill_vwap: float | None = None

    # Order-time slip anchor: the slot's first-fill VWAP, set ONLY on a
    # slip-bounded top-up (slot_filled > 0 AND cfg.max_vwap_slip_from_anchor is
    # set AND a real first-fill VWAP exists on the slot). Carries the
    # decision-time slip leash into the order/dry-run re-walk so the cap is
    # enforced at execution time too, not just at decision-time sizing. None =>
    # no slip cap at order time (first fills, transition slots with no
    # first-fill VWAP, and sleeves with max_vwap_slip_from_anchor=None). In-tick
    # only — NOT persisted to the ledger (the slot's first row owns the anchor).
    slip_anchor_vwap: float | None = None


@dataclass
class OrderResult:
    """Result of a CLOB order placement attempt."""

    order_id: str | None = None
    limit_price: float | None = None
    fill_price: float | None = None
    fill_size: float | None = None
    fill_ts: str | None = None
    error: str | None = None
    # Coarse classification of `error` for retry policy. ce-code-review P1 #13.
    # Values:
    #   "auth"               -- signature / API-key rejection; do not retry, halt
    #   "insufficient_funds" -- wallet collateral below order notional; halt
    #   "market_closed"      -- bracket no longer tradable; abort this signal
    #   "network"            -- transient HTTP / timeout / 5xx; retry safe
    #   "stale_quote"        -- fresh quote no longer satisfies caller's guard
    #   "unknown"            -- uncategorized exception; retry (current behavior)
    # None means error itself is None (success or pre-error state).
    error_kind: str | None = None
    success: bool = False
    # Realized fill size in USD (renamed from ``kelly_size_usd``; see BetSignal).
    bet_size_usd: float | None = None
    # Post-fill edge at the realized VWAP (prob_safe_floor - fill_price - fee).
    # None on dry_run / pre-fill; populated by the edge-preserving walker and
    # persisted to ledger.realized_edge by update_pending_bet_after_execution.
    realized_edge: float | None = None
    # 2-step verification forensics (ported from LCB Units 6/7):
    #   transaction_hash: on-chain proof on live fills; "DRY_RUN_<uuid>" on dry-run.
    #   verify_attempts: number of place_order attempts (1..MAX_ORDER_RETRIES).
    #   verification_downgraded: True when MATCHED was returned on every
    #     attempt but no tx hash was ever attached — the bet is credited as
    #     FILLED but flagged for operator review.
    transaction_hash: str | None = None
    verify_attempts: int = 0
    verification_downgraded: bool = False
    # True when a live order was submitted but could not be safely cancelled
    # after verification failed. The ledger row must stay PENDING with its
    # order_id so startup reconciliation can track/cancel it.
    leave_pending: bool = False
    # Per-price fill ladder for dashboard forensics. Each entry is
    # {"price": ask/trade price, "shares": token shares, "usd": notional}.
    fill_levels: list[dict[str, float]] = field(default_factory=list)


@dataclass
class CycleResult:
    """Summary of a single betting tick."""

    n_evaluated: int = 0
    n_passed_gates: int = 0
    n_placed: int = 0
    total_exposure_usd: float = 0.0
    dry_run: bool = True
    # Count of placements in this tick that were top-ups (slot_state filled > 0
    # at placement time, i.e. not the first fill on that slot). Surfaced in
    # the scanner's aggregate Telegram alert body so operators can see when
    # the bot is adding to an existing slot vs opening a new one.
    n_topup: int = 0
