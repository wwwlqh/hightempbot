"""Trading constants and the strategy registry.

Values come from `backtest/configs/candidate_l2_depth.json`;
`tests/test_strategy_constants.py` fails if they drift from it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# --- Sizing and risk ---
MIN_BET_USD = 1.0
# Drawdown halt: no new bets while realized capital is this far below its peak
# (enforced in execution.pipeline.run_betting_cycle). Open bets still resolve.
MAX_DD = 0.40
MAX_DAILY_NOTIONAL_FRAC = 1.00
MAX_PENDING_EXPOSURE_PCT = 1.00

POLY_FEE_THETA = 0.05

# --- Gates ---
MIN_EDGE = 0.03                  # fallback for strategies without their own edge
MAX_EDGE = 0.10
MIN_FILL_PRICE = 0.01

# Replace the NO gate's raw probability with a reliability-calibrated one
# (calibration/reliability.py). Without a fitted curve this is identity, and
# the raw gate over-fires — fit curves before going live.
RELIABILITY_CALIBRATION_ENABLED = True

# Bracket units allowed to bet. Missing unit fails closed.
ALLOWED_BRACKET_UNITS = frozenset({"F", "C"})

MIN_BVOL = 50
MAX_PER_MARKET = 999
MIN_COVERAGE_PCT = 0.95
REF_START_DATE = "2024-03-01"

# --- Calibration health ---
LUT_STALE_HOURS = 36
MIN_PAIRS = 30                   # keep in sync with calibration/model.py::MIN_PAIRS
# Skip a station whose freshest actual is older than this (30d window + 5d slack).
ACTUALS_STALE_DAYS = 35
# Page when at least this many stations have unhealthy calibration.
CALIBRATION_HEALTH_ALERT_THRESHOLD = 5

# --- Order placement ---
MAX_ORDER_RETRIES = 3
VERIFY_POLY_TIMEOUT_S = 15
ORDER_RETRY_BACKOFF_S = 2
ORDER_VERIFY_POLL_S = 3

# --- Schedule ---
BETTING_LOCAL_CUTOFF_HOUR = 0    # 0 disables the late-day cutoff
RESOLUTION_SCAN_START_HOUR = 18
SCAN_INTERVAL_MINUTES = 10


def icao_tick_offset(icao: str) -> int:
    """Minute offset within each scan interval for this station's cron jobs."""
    return sum(ord(c) for c in icao) % max(1, int(SCAN_INTERVAL_MINUTES))


def tick_index_for(icao: str, local_now_minute: int) -> int:
    """Index of the station's tick within the hour (0 = first).

    Offset-aware so a tick delayed by up to one interval keeps its index.
    """
    offset = icao_tick_offset(icao)
    return ((local_now_minute - offset) % 60) // max(1, int(SCAN_INTERVAL_MINUTES))


# --- WU forecast consensus gate ---
# "OFF" | "SHADOW" (record verdict only) | "BLOCK". Hardcoded OFF on purpose:
# the backtest has no WU gate, and an env override once ran BLOCK by mistake.
WU_CONSENSUS_MODE = "OFF"
WU_CONSENSUS_BUFFER_C = 0.0

# Dashboard cards and the drawdown peak only count bets after this time.
# Older ledger rows are kept for calibration and audit.
DASHBOARD_SESSION_START_UTC = "2026-08-10 00:00:00"

EXPECTED_MODELS = [
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
    "gem_seamless",
    "meteofrance_seamless",
    "ukmo_seamless",
    "knmi_seamless",
    "dmi_seamless",
    "ncep_gfs013",
]
REQUIRED_MEMBERS = 9

# --- Resolution ---
RESOLUTION_PRICE_THRESHOLD = 0.995
RESOLUTION_LOSS_PRICE_THRESHOLD = 0.005
REDEEMABLE_PAYOUT_VALUE_FRACTION = RESOLUTION_PRICE_THRESHOLD
# Settle early off CLOB prices. Off: wait for Polymarket to close the event.
EARLY_RESOLUTION_ENABLED = False
# Manual WU-actuals settlement refuses dates fewer than this many days past
# target unless forced.
POLYMARKET_FALLBACK_DAYS = 1

# --- Strategies ---
STRATEGY_NAMES: tuple[str, ...] = ("NO", "YMID", "TAIL", "YHIGH", "FLIP")

# Below this many LUT samples, p_L_strict is NaN and p_L_loose falls back to raw EMOS.
LUT_MIN_N_FOR_SHRINKAGE: int = 30
YMID_MAX_EDGE: float = 0.30


@dataclass(frozen=True)
class StrategyConfig:
    """One strategy. Each strategy reads only the fields its gate uses:

    - NO: additive edge band (`min_edge`, `max_edge`), with a relaxed
      `*_for_ceiling` fallback on "X or higher" brackets.
    - YMID: ratio gate (`alpha_ratio`) plus `max_edge`.
    - TAIL: N-of-N vote over `vote_signals`, each ≥ `alpha_ratio` × price.
    - YHIGH: additive YES edge band on ceiling brackets.
    - FLIP: buys YES wherever the NO gate fires.
    """

    side: str                                   # "YES" or "NO"
    signal_name: str
    fp_min: float                               # fill-price band, inclusive
    fp_max: float
    capital_frac: float                         # target stake = capital × this
    max_walk_price: float                       # walker stops past top ask + this
    entry_hour_set: frozenset[int]              # station-local hours allowed
    min_bvol: float                             # 24h volume floor (USD)
    execution_min_edge: float | None = None     # realized VWAP edge floor
    enabled: bool = True
    max_vwap_slip_from_anchor: float | None = None  # top-up VWAP cap vs first fill

    min_edge: float | None = None
    max_edge: float | None = None
    alpha_ratio: float | None = None

    vote_signals: tuple[str, ...] | None = None
    vote_n_required: int | None = None
    vote_min_n: int | None = None

    tp: float | None = None                     # exit after this price move
    sl: float | None = None

    signal_name_for_ceiling: str | None = None
    fp_min_for_ceiling: float | None = None
    max_edge_for_ceiling: float | None = None

    # Skip the strategy when any bracket of the same market has YES ask ≥ this.
    consensus_skip_threshold: float | None = None
    # Wait to place until the fill price is ≤ this.
    delayed_entry_fp_max: float | None = None


STRATEGY_CONFIGS: dict[str, StrategyConfig] = {
    "NO": StrategyConfig(
        side="NO",
        signal_name="p_E",
        fp_min=0.75,
        fp_max=1.00,
        min_edge=0.05,                          # equals the walker floor; lower fires unfillable bets
        max_edge=0.15,
        capital_frac=0.07,
        max_walk_price=0.05,
        execution_min_edge=0.05,
        entry_hour_set=frozenset(range(7)),
        min_bvol=50.0,
        signal_name_for_ceiling="p_B_50",
        fp_min_for_ceiling=0.50,
        max_edge_for_ceiling=0.35,
    ),
    "YMID": StrategyConfig(
        side="YES",
        signal_name="p_Shrink_n50",
        enabled=False,
        fp_min=0.10001,                         # starts just above TAIL's range
        fp_max=0.50,
        alpha_ratio=1.3,
        max_edge=YMID_MAX_EDGE,
        capital_frac=0.010,
        max_walk_price=0.05,
        entry_hour_set=frozenset({0}),
        min_bvol=50.0,
        tp=0.15,
    ),
    "TAIL": StrategyConfig(
        side="YES",
        enabled=False,                          # EV ≤ 0 at real fills (research_2026_07)
        signal_name="tail_vote_avg",
        fp_min=0.001,
        fp_max=0.03,
        delayed_entry_fp_max=0.02,
        alpha_ratio=4.0,
        vote_signals=("p_E", "p_B_50", "p_L_loose", "p_Shrink_n10"),
        vote_n_required=4,
        vote_min_n=LUT_MIN_N_FOR_SHRINKAGE,
        capital_frac=0.050,
        max_walk_price=0.05,
        execution_min_edge=0.07,
        entry_hour_set=frozenset({1}),
        min_bvol=50.0,
        tp=0.20,
        consensus_skip_threshold=0.40,
    ),
    "FLIP": StrategyConfig(
        # Operator experiment: buy YES where the NO gate fires. Measured −EV.
        # Model P(YES) is tiny here, so min_edge=-1.0 is intentional; the only
        # fill bound is max_walk_price. Enabled via FLIP_MODE=1, which also
        # disables NO.
        side="YES",
        signal_name="p_E_flip",
        enabled=False,
        fp_min=MIN_FILL_PRICE,
        fp_max=0.50,
        min_edge=-1.0,
        capital_frac=0.07,
        max_walk_price=0.05,
        entry_hour_set=frozenset(range(7)),
        min_bvol=50.0,
    ),
    "YHIGH": StrategyConfig(
        side="YES",
        signal_name="p_B_50",
        enabled=False,
        fp_min=0.50,
        fp_max=1.00,
        min_edge=0.025,
        max_edge=0.30,
        capital_frac=0.010,
        max_walk_price=0.05,
        entry_hour_set=frozenset({0}),
        min_bvol=50.0,
    ),
}


def _apply_flip_mode() -> None:
    """With FLIP_MODE=1, disable NO and enable FLIP. Defaults on any error."""
    try:
        from hightempbot.runtime_config import get_config
        flip_on = bool(get_config().flip_mode)
    except Exception:
        flip_on = False
    if flip_on:
        STRATEGY_CONFIGS["NO"] = replace(STRATEGY_CONFIGS["NO"], enabled=False)
        STRATEGY_CONFIGS["FLIP"] = replace(STRATEGY_CONFIGS["FLIP"], enabled=True)


_apply_flip_mode()


# Clear a TP/SL close_in_flight flag older than this and retry the close.
TP_SL_FLAG_STALE_SECONDS: int = 600
RECONCILE_INTERVAL_MINUTES: int = 5
# In slot_state, PENDING rows older than this stop counting toward exposure,
# but only when the slot also has a filled row.
MAX_PENDING_AGE_MINUTES: int = 240
