"""Dataclasses shared by the decision, execution and ledger modules."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BetSignal:
    """A single bracket evaluation result (pass or fail)."""

    station_id: str
    target_date: str          # ISO date, station local
    horizon: int
    bracket_idx: int
    threshold: float          # upper bracket boundary
    bracket_label: str        # e.g. "66-68°F YES"
    side: str                 # "YES" or "NO"
    p_model: float
    p_market: float           # best ask for YES, best bid for NO
    edge: float
    bet_size_usd: float       # pre-walk stake target (stored in legacy `kelly_size` columns)
    fill_price: float
    volume_usd: float | None
    market_id: str = ""
    token_id: str = ""
    limit_price: float = 0.0  # highest ask level the walker may fill
    bracket_low: float | None = None
    bracket_high: float | None = None
    bracket_unit: str = ""    # "F" / "C"; "" = unknown, fail closed
    prob_safe_floor: float | None = None   # side-aware probability used for edge
    pred_bucket: tuple[float, float] | None = None
    n_bucket: int = 0
    gate_results: dict[str, bool | None] = field(default_factory=dict)
    passed_all_gates: bool = False
    wu_forecast_c: float | None = None
    wu_consensus_verdict: bool | None = None  # None = WU unavailable

    strategy: str = ""        # registry key; written to event_detail.strategy
    signal_used: str = ""     # e.g. "p_E", "tail_vote_avg"
    signal_value: float | None = None

    # Scanner-time top of book, or the slot's first-fill anchor on a top-up.
    # Retries anchor max_walk_price here so the leash doesn't drift with the book.
    entry_top_price: float | None = None
    slot_filled_pre: float = 0.0              # slot exposure before this signal; > 0 = top-up
    entry_fill_vwap: float | None = None      # decision-time walked VWAP of this fill
    slip_anchor_vwap: float | None = None     # first-fill VWAP for slip-capped top-ups (not persisted)


@dataclass
class OrderResult:
    """Result of a CLOB order placement attempt."""

    order_id: str | None = None
    limit_price: float | None = None
    fill_price: float | None = None
    fill_size: float | None = None
    fill_ts: str | None = None
    error: str | None = None
    # "auth" | "insufficient_funds" | "market_closed" | "network" | "stale_quote" | "unknown"
    error_kind: str | None = None
    success: bool = False
    bet_size_usd: float | None = None
    realized_edge: float | None = None        # prob_safe_floor - vwap - fee
    transaction_hash: str | None = None       # "DRY_RUN_<uuid>" on dry-run
    verify_attempts: int = 0
    verification_downgraded: bool = False     # MATCHED but never got a tx hash
    # Submitted but could not be cancelled: keep the row PENDING for reconciliation.
    leave_pending: bool = False
    fill_levels: list[dict[str, float]] = field(default_factory=list)  # {"price", "shares", "usd"}


@dataclass
class CycleResult:
    """Summary of a single betting tick."""

    n_evaluated: int = 0
    n_passed_gates: int = 0
    n_placed: int = 0
    total_exposure_usd: float = 0.0
    dry_run: bool = True
    n_topup: int = 0
