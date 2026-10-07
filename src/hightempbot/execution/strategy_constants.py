"""Global trading constants for Phase 2 live trading.

Tuned via the backtest pipeline (`backtest/lib/sweep_lib.py` +
`backtest/lib/live_match_eval.py`); current strategy is the **L2-depth
champion** in `backtest/configs/candidate_l2_depth.json` (variant
`sel_taila40_fp03_cs40`), ported into src on 2026-05-29. The JSON is the
source of truth — `tests/test_strategy_constants.py` carries a parity test
that fails CI if any ported value drifts from it. The prior non-L2 baseline
was removed after the real-ask-ladder L2 sweep became the only active config.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Sizing ---
# Kelly removed 2026-04-24: bet target is now cfg.capital_frac * capital,
# filled edge-preservingly across the book (see walk_book_edge_preserving).
# 2026-05-06: MAX_DD raised 0.30 -> 0.50 (halve-at-50% rule from MILD optimum).
# 2026-05-24: MAX_DD lowered 0.50 -> 0.40 after ABCD re-optimization through
#              2026-05-21; halt earlier rather than opening new positions
#              through a deeper realized drawdown.
# 2026-05-06: MAX_PENDING_EXPOSURE_PCT raised 0.70 -> 1.00 (allow full-capital
#             concurrent exposure; needed for MILD sizing on busy days).
MIN_BET_USD = 1.0
# Drawdown halt threshold. When realized capital has fallen this fraction
# below peak realized capital, the betting pipeline returns early and places
# no new bets for that tick. Existing PENDING positions still resolve
# normally; only new placement is suspended until capital recovers above the
# threshold. Operator decision 2026-05-20 — replaces the prior "halve target
# at MAX_DD" behavior; the legacy `dd_reduced` flag was removed 2026-05-23.
# Halt-on-DD is enforced in `execution.pipeline.run_betting_cycle`.
MAX_DD = 0.40
MAX_DAILY_NOTIONAL_FRAC = 1.00
MAX_PENDING_EXPOSURE_PCT = 1.00    # was 0.70 — allow concurrent open exposure up to 100% of capital

# --- Polymarket fee ---
POLY_FEE_THETA = 0.05

# --- Bet decision gates ---
MIN_EDGE = 0.03
MAX_EDGE = 0.10                  # reject bets claiming edge above this (model-error flag)
MIN_FILL_PRICE = 0.01

# --- Reliability calibration (2026-07-16 NO over-confidence fix) ---
# When True, the live NO gate replaces its raw claimed probability
# (P(NO wins) = 1 - p_E) with a reliability-calibrated value BEFORE the
# edge/fee computation. The curve is fit per bracket-unit group ('NO_C' /
# 'NO_F') and loaded from the reliability_curves table; when no valid curve is
# loaded the layer is identity (no behavioral change). Live forensics
# (2026-07-16): overall claimed 0.945 vs realized 0.881 (+6.4pp over-confident),
# concentrated in °C (+10.4pp, ROS -2.31%). See calibration/reliability.py.
RELIABILITY_CALIBRATION_ENABLED = True

# --- Bracket-unit gate (2026-07-16) ---
# Bracket units allowed to place live bets. Enforced in strategy evaluation,
# fails closed when bracket_unit is missing (gate_results["unit_allowed"]).
# History: °C was quarantined 2026-07-16 after live forensics (°C ROS -2.31%,
# +10.4pp over-confidence). Re-enabled same day per the walk-forward
# calibrated-gate sweep (backtest/scripts/sweep_calibrated_gate.py): behind
# the logit-blend reliability curve, °C is the well-calibrated unit
# (rel-gap +1.5..2.4pp OOS, positive ROS in windows C/D), while the °F blend
# is guard-refused OOS (price_weight >= 0.90 -> °F runs raw/identity).
# PRECONDITION for live °C: a healthy NO_C logit_blend curve must be fitted
# and committed to reliability_curves (cli/fit_reliability.py); with no curve
# the calibration layer is identity and °C reverts to the raw gate that lost
# money live. Drop "C" again if the dry-run shadow-replay shows °C rel-gap
# breaching +-3pp or negative °C ROS.
ALLOWED_BRACKET_UNITS = frozenset({"F", "C"})

MIN_BVOL = 50                    # 2026-05-07: lowered 500 -> 50 to match
                                 # backtest "optimus" gate. STRATEGY_CONFIGS[*]
                                 # tracks this global.
MAX_PER_MARKET = 999
MIN_COVERAGE_PCT = 0.95
REF_START_DATE = "2024-03-01"

# --- LUT calibration ---
LUT_STALE_HOURS = 36
# Wiki-canonical readiness threshold for CalibrationModel.is_ready().
# Mirror of calibration/model.py::MIN_PAIRS; keep the two in sync.
MIN_PAIRS = 30

# Actuals-staleness gate: a station fails the scanner gate if its
# freshest actual is more than this many days old. `is_ready()` only
# checks `n_samples >= MIN_PAIRS`, which the historical bulk import
# satisfies even when no live scrape has run in months — so without
# this gate a station with a broken `resolution_source` would bet on
# calibration that hasn't seen a fresh pair in a year. 35d is the
# rolling-window (30d) + a 5d buffer for weekend/holiday delays.
ACTUALS_STALE_DAYS = 35

# Persistent-calibration-unhealthy alarm threshold. The 15-min
# system_health_check pages via Telegram when at least this many
# active stations have `n_samples < MIN_PAIRS` OR actuals older
# than ACTUALS_STALE_DAYS. Default 5 is conservative — the 4
# currently-unsupported non-WU stations (LLBG, LTFM, RCTP, UUWW)
# alone won't trigger it, but a regression in the auto-heal pipeline
# (e.g., today's 2026-04-26 gap that left 44 stations stuck) will.
CALIBRATION_HEALTH_ALERT_THRESHOLD = 5

# --- Order placement & verification ---
MAX_ORDER_RETRIES = 3
VERIFY_POLY_TIMEOUT_S = 15
ORDER_RETRY_BACKOFF_S = 2
ORDER_VERIFY_POLL_S = 3

# --- Scanning schedule ---
# Betting/resolution jobs wake every 10 min via CronTrigger in scheduler/jobs.py
# (slots 0,10,20,30,40,50 + ICAO offset). Gating is also by ensemble
# completeness (REQUIRED_MEMBERS == 9 from EXPECTED_MODELS).
#
# BETTING_LOCAL_CUTOFF_HOUR: station-local hour after which no new bets fire on
# the market target date. 0 disables the cutoff entirely.
#
# Operator decision (2026-05-04): user accepts that during the SHADOW soak the
# WU gate is informational only, and that disabling the cutoff means evening
# bets fire against stale 22h-cached ensembles with no freshness check. The
# tradeoff is intentional — observed evening windows generate a meaningful
# fraction of total candidates and the user wants them captured. Re-enable
# (set to 14) only if SHADOW telemetry shows systematic late-day model drift.
BETTING_LOCAL_CUTOFF_HOUR = 0
RESOLUTION_SCAN_START_HOUR = 18
SCAN_INTERVAL_MINUTES = 10


def icao_tick_offset(icao: str) -> int:
    """Per-station tick offset (minutes within the hour) used by the scheduler cron.

    Mirrors ``scheduler/jobs.py`` so the gate can recover the same tick index
    the scheduler used to fire the job.
    """
    return sum(ord(c) for c in icao) % max(1, int(SCAN_INTERVAL_MINUTES))


def tick_index_for(icao: str, local_now_minute: int) -> int:
    """Return the offset-aware tick index for ``icao`` at ``local_now_minute``.

    Tick 0 is the first scheduler tick of the local hour for this station;
    tick 1 the second; and so on. Using ``(minute - offset) mod 60`` rather
    than the bare ``minute // SCAN_INTERVAL_MINUTES`` makes the gate robust
    to APScheduler misfire delays up to one ``SCAN_INTERVAL_MINUTES``: a
    tick scheduled at minute :09 that executes at :10 still reports as
    tick 0.
    """
    offset = icao_tick_offset(icao)
    return ((local_now_minute - offset) % 60) // max(1, int(SCAN_INTERVAL_MINUTES))


# --- WU forecast consensus gate ---
# Modes:
#   "OFF"    — gate is skipped entirely; no WU fetch, no verdict written.
#   "SHADOW" — runs fetch + records ``wu_consensus_verdict`` for telemetry
#              but does NOT participate in ``passed_all_gates``.
#   "BLOCK"  — full filter; fail-closed when WU disagrees or is unavailable.
#
# 2026-05-11: switched to OFF per operator request. Previously SHADOW; that
# still fired a WU API call per candidate to populate the telemetry column,
# even though the verdict had no effect on betting. OFF avoids the fetch
# entirely.
#
# 2026-05-10: previously env-overridable via WU_CONSENSUS_MODE. Production
# was running BLOCK by mistake, which silently killed every TAIL bet (TAIL
# bets deep tails the deterministic WU forecast naturally disagrees with)
# and produced a train/live mismatch — the backtest harness has zero WU
# consensus logic at all, so any non-OFF mode is by definition off-spec
# vs the strategy's tuned numbers (alpha_ratio, capital_frac, etc).
#
# Hardcoded here rather than read from .env so it can't be silently
# re-enabled by editing a server config file. If you ever need to
# experiment with SHADOW/BLOCK again, re-introduce the env knob deliberately
# alongside a corresponding backtest gate.
WU_CONSENSUS_MODE = "OFF"

# Buffer (°C) absorbed at each bracket edge before the gate votes ACCEPT.
# 0.0 = literal "WU forecast falls in the bracket" check (operator-chosen
# 2026-05-04). YES needs WU exactly inside the half-open bracket; NO needs
# WU strictly outside. Larger buffers add safety margin against WU's
# reporting precision but kill more bets — at 1.0°C all interior YES
# candidates are mathematically impossible on 1°C-wide brackets. Raise this
# only if WU's reporting noise is observed to flip verdicts at boundaries.
WU_CONSENSUS_BUFFER_C = 0.0

# SESSION EPOCH. Operator-chosen resets: 2026-05-21 (live-trading start),
# 2026-08-09 (FLIP experiment deploy), then 2026-08-10 (operator-ordered full
# zero reset — "start tomorrow": every dashboard card reads zero and the
# drawdown halt gate re-bases here; see capital._session_ledger_peak). Bets
# from before this UTC stamp do not appear in any dashboard card AND no longer
# influence the gate's peak — pre-epoch history had the gate stuck at ~48% DD
# (> MAX_DD 0.40), silently blocking the FLIP experiment. The underlying
# ledger rows are NOT deleted — LUT/reliability calibration and settlement
# audit still read them.
DASHBOARD_SESSION_START_UTC = "2026-08-10 00:00:00"

# --- Expected ensemble models (BoM retired, JMA not promoted) ---
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

# Early settlement via CLOB threshold scan (0.995 win / 0.005 loss). Disabled
# 2026-05-22 when live trading started: the user prefers waiting for Polymarket
# to mark the event `closed=true` (settled via outcomePrices in
# `_resolve_via_gamma_close`) rather than settling early off CLOB best-bid/ask.
# Tests for the threshold path can patch this back to True to exercise the
# legacy code; production runs with it disabled so PENDING bets only resolve
# when Polymarket has finalized the market.
EARLY_RESOLUTION_ENABLED = False

# Safety floor (calendar days past `target_date`) for the MANUAL WU-actuals
# resolution path. Polymarket Gamma archives daily temperature events some
# time after close; once archived, every polymarket_* resolution path returns
# empty and PENDING bets would linger. The automatic resolution tick does NOT
# fall back to WU actuals — it intentionally leaves such rows PENDING until
# Polymarket finalizes the event (see resolution/settler.py
# `_resolve_station_date`). Settling via local WU actuals is manual-only:
# cli/resolve_pending_via_wu.py or the dashboard resolve-pending action, both
# of which refuse to commit a date fewer than this many days past target
# (guard against settling a still-live market) unless the operator passes
# --force / force=true.
# Value chosen 2026-05-20 against the 5/17 + 5/18 archive-window incident:
# 1 day = "yesterday or older" → the manual path unlocks the morning after
# target in most timezones, giving the polymarket_* paths their best shot
# first. Raising it widens the refusal window; --force still overrides.
POLYMARKET_FALLBACK_DAYS = 1

# --- Per-strategy 4-stack (NO / YMID / TAIL / YHIGH) ---
# Optimum Strategy spec from polybot wiki, locked 2026-05-06; YHIGH added
# 2026-05-07 as the 4th strategy from backtest/results/per_strategy_detail.md.
# Each bracket on every betting tick is evaluated against all four strategies
# independently; a bracket can produce up to one signal per strategy. See
# decision.py for the router.

STRATEGY_NAMES: tuple[str, ...] = ("NO", "YMID", "TAIL", "YHIGH", "FLIP")

# Mirrors backtest/sweep_lib.add_signal_flavors `lut_min_n` default. Buckets
# with fewer cumulative samples produce NaN for `p_L_strict` and degrade
# `p_L_loose` to raw EMOS. Used by both the signal-flavor helper and the
# TAIL vote_min_n guard (see _evaluate_strategy in decision.py).
LUT_MIN_N_FOR_SHRINKAGE: int = 30

# YMID's hard ceiling on additive edge after fees: reject any candidate where
# `p_Shrink_n50 - yes_price - fee > 0.30`. Mirrors live_match_eval.candidates_3strats.
YMID_MAX_EDGE: float = 0.30


@dataclass(frozen=True)
class StrategyConfig:
    """One strategy in the registry. Fields are union-typed across NO/YMID/TAIL/YHIGH.

    Each strategy reads only the fields it needs:
      - NO    uses additive edge gate (`min_edge`, `max_edge`); ratio/vote fields = None.
              On ceiling brackets, the relaxed signal/fp/edge overrides
              (`signal_name_for_ceiling`, `fp_min_for_ceiling`,
              `max_edge_for_ceiling`) form a fallback gate that fires only
              when the strict gate misses.
      - YMID  uses ratio gate (`alpha_ratio`) + max-edge ceiling (`max_edge`); vote fields = None.
      - TAIL  uses N-of-N vote (`vote_signals`, `vote_n_required`, `vote_min_n`); ratio = `alpha_ratio`.
      - YHIGH uses additive edge gate like NO but on YES side; restricted to
              ceiling brackets (handled in decision dispatch).

    `tp` and `sl` are the post-entry exit thresholds in price-units (e.g. 0.15
    means "exit when YES price has moved +0.15 in our favor"). YMID and TAIL
    are TP-only (YMID at +0.15, TAIL at +0.20); NO/YHIGH hold positions to
    resolution. The TP/SL monitor walks every strategy with a non-None tp/sl.

    `entry_hour_set` is the set of station-local hours during which the
    strategy is allowed to fire. YMID and YHIGH are locked to local hour 0 by
    the validated 2026-05-07 spec; the L2 champion moves TAIL to local hour 1
    (`candidate_l2_depth.json.TAIL.entry_local_hours = [1]`); NO remains
    multi-hour [0..6].

    `max_walk_price` caps how far into the ask book the order walker is
    allowed to consume for strategies without `execution_min_edge`. NO/TAIL
    use a realized VWAP edge floor instead. Dollar exposure scales linearly
    with bankroll via `capital_frac` — there is no static per-bet cap.

    `max_vwap_slip_from_anchor` is an optional per-sleeve VWAP-slip leash: on a
    TOP-UP, the book walk stops eating depth once the next level would push the
    realized VWAP past the slot's first-fill VWAP + this cap. First fills are
    exempt — they set the anchor. NOTE: this is a deploy-policy directive, NOT
    a sim-validated number. NO/TAIL set it to None as of 2026-05-31 so top-ups
    are edge-floor-only after rechecking the current entry gates.
    """

    side: str                                   # "YES" or "NO"
    signal_name: str                            # e.g. "p_E", "p_Shrink_n50", "tail_vote_avg"
    fp_min: float                               # inclusive
    fp_max: float                               # inclusive
    capital_frac: float                         # 0.01 = 1% of capital
    max_walk_price: float                       # walker stops past top_ask + this
    entry_hour_set: frozenset[int]              # station-local hours allowed
    min_bvol: float                             # 24h volume floor (USD)
    execution_min_edge: float | None = None     # realized VWAP floor; None uses strategy default
    enabled: bool = True                        # disabled strategies stay documented but never fire
    # Per-sleeve VWAP-slip leash from the L2 champion's live_execution_policy.
    # Top-up-only fill bound anchored to the slot's first-fill VWAP; None = no
    # slip cap (edge-floor-only). Deploy-policy directive, not sim-validated.
    max_vwap_slip_from_anchor: float | None = None

    # NO additive edge band — None for YMID/TAIL
    min_edge: float | None = None
    max_edge: float | None = None

    # YMID/TAIL ratio gate — None for NO
    alpha_ratio: float | None = None

    # TAIL vote — None for NO/YMID
    vote_signals: tuple[str, ...] | None = None
    vote_n_required: int | None = None
    vote_min_n: int | None = None               # min n_cum so vote signals don't collapse to p_E

    # YMID-only TP/SL — None for NO/TAIL (held to resolution)
    tp: float | None = None
    sl: float | None = None

    # NO-only ceiling-bracket relaxation (None on YMID/TAIL/YHIGH). See
    # class docstring + per_strategy_detail.md "Bracket-conditional refinement".
    signal_name_for_ceiling: str | None = None
    fp_min_for_ceiling: float | None = None
    max_edge_for_ceiling: float | None = None

    # "Market knows the answer" consensus gate: skip every signal from this
    # strategy when ANY bracket in the same (station, target_date) shows YES
    # best_ask >= consensus_skip_threshold at the scanner tick. None disables
    # the gate. Set per-strategy because NO and TAIL react oppositely:
    #   - TAIL benefits (deep tails are noise once a favorite emerges)
    #   - NO loses (gating NO drops profitable bets)
    # 2026-05-17 consensus-skip sweep picked TAIL=0.50 (preserves dry-run flow)
    # vs 0.40 (PnL-optimal). The L2 champion (2026-05-29) adopts 0.40 wholesale
    # — see STRATEGY_CONFIGS["TAIL"].consensus_skip_threshold.
    consensus_skip_threshold: float | None = None

    # Optional delayed-entry trigger. When set, the strategy can evaluate its
    # normal signal/edge gates across the broader fp band, but it cannot size
    # or place until the current fill price is at or below this value.
    delayed_entry_fp_max: float | None = None


STRATEGY_CONFIGS: dict[str, StrategyConfig] = {
    "NO": StrategyConfig(
        side="NO",
        signal_name="p_E",
        fp_min=0.75,
        fp_max=1.00,
        min_edge=0.05,                          # 2026-07-17: raised 0.04 -> 0.05 to align with
                                                # execution_min_edge (real-fill scoring found the
                                                # 0.04 gate fires bets the 0.05 walker floor can
                                                # never fill; see score_deployed_realfill.py).
                                                # History: 0.090 -> 0.04 on 2026-07-16 from the
                                                # mid-fill calibrated-gate sweep (OOS n=849, ROS
                                                # +6.16%); real-fill scoring then showed the
                                                # verifiable-depth subset is adversely selected
                                                # (n=27, ~-6% ROS, loses at mid too) -- the
                                                # marginal 5-9pp band is UNPROVEN at real depth.
                                                # Live telemetry (book_snapshots) must validate
                                                # per-band realized edge; raise back toward 0.09
                                                # if the 5-9pp band shows negative realized edge.
                                                # Requires the reliability blend curve fitted
                                                # (else raw edge over-fires -- do NOT deploy
                                                # without curves).
        max_edge=0.15,                          # tightened from 0.25 to avoid noisy overfit
        capital_frac=0.07,                      # 2026-06-05 operator sizing override:
                                                # NO 7%, TAIL 5%, 100% current-equity exposure cap.
                                                # Leaves room for slow same-day resolutions.
                                                # See backtest/configs/candidate_l2_depth.json.
                                                # Sole sizing dial as of 2026-05-11 (cap table removed):
                                                # cross-tick top-up handles thin-book deployment.
        max_walk_price=0.05,                    # walker stops past top_ask + 0.05
        execution_min_edge=0.05,                # L2 champion 2026-05-29: 0.03 -> 0.05; fill deeper by
                                                # VWAP while keeping >=5pp realized edge
                                                # (candidate_l2_depth.json NO.execution_min_edge)
        entry_hour_set=frozenset(range(7)),     # optimized local hours [0..6]
        min_bvol=50.0,                          # 2026-05-07: lowered 500 -> 50 to match backtest optimus
        max_vwap_slip_from_anchor=None,         # 2026-05-31: remove top-up slip leash;
                                                # rely on current gates + >=5pp VWAP edge
        tp=None,                                # NO holds to resolution
        sl=None,
        # Ceiling-bracket relaxation: when strict NO misses on a "X-or-higher"
        # bracket, retry with p_B_50 + relaxed fp/edge bounds.
        signal_name_for_ceiling="p_B_50",
        fp_min_for_ceiling=0.50,
        max_edge_for_ceiling=0.35,
    ),
    "YMID": StrategyConfig(
        side="YES",
        signal_name="p_Shrink_n50",
        enabled=False,                          # disabled in optimized NO+TAIL production profile
        # fp_max upper bound is exclusive of the TAIL fp range (TAIL ends at
        # 0.10 inclusive, so YMID starts strictly above 0.10).
        fp_min=0.10001,
        fp_max=0.50,
        alpha_ratio=1.3,                        # p_Shrink_n50 ≥ 1.3 × yes_price
        max_edge=YMID_MAX_EDGE,                 # 0.30
        capital_frac=0.010,                     # 1.0% MILD
        max_walk_price=0.05,
        entry_hour_set=frozenset({0}),
        min_bvol=50.0,
        tp=0.15,                                # exit when yes_price moves +0.15
        sl=None,                                # TP-only; SL dropped in locked spec
    ),
    "TAIL": StrategyConfig(
        side="YES",
        enabled=False,                          # DISABLED 2026-07-17 (operator + slice forensics):
                                                # TAIL is EV<=0 at honest fills in every tested
                                                # slice; the delayed-entry dump-to-2c trigger IS
                                                # the adverse selection (dumped tails win 2.9%,
                                                # firm-priced tails the rule refuses win 40%), and
                                                # ~0.6% of entries had $10 of real ask at the
                                                # print (real fills ~4.2c). See
                                                # backtest/results/research_2026_07/README.md and
                                                # backtest/scripts/explore_tail_slices.py.
                                                # (History: re-enabled 2026-05-29 with the L2
                                                # champion; that backtest's TAIL profit was a
                                                # fill artifact.)
        signal_name="tail_vote_avg",            # average of the 4 voting signals
        fp_min=0.001,
        fp_max=0.03,                            # L2 champion 2026-05-29: 0.05 -> 0.03
        delayed_entry_fp_max=0.02,              # 2026-06-09 WFO: after TAIL conditions pass,
                                                # wait for YES ask <= 2c before entry.
        alpha_ratio=4.0,                        # L2 champion 2026-05-29: 4.5 -> 4.0; each voter
                                                # p_i >= 4.0 * yes_price (candidate_l2_depth TAIL.alpha)
        vote_signals=("p_E", "p_B_50", "p_L_loose", "p_Shrink_n10"),
        vote_n_required=4,                      # 4-of-4 unanimous
        vote_min_n=LUT_MIN_N_FOR_SHRINKAGE,     # F-005: skip TAIL when n_cum < 30 (votes collapse)
        capital_frac=0.050,                     # 2026-06-05 operator sizing override:
                                                # deployment override NO 7% / TAIL 5%;
                                                # sole sizing dial (no static cap), top-up to target
        max_walk_price=0.05,
        execution_min_edge=0.07,                # L2 champion 2026-05-29: 0.03 -> 0.07; fill deeper by
                                                # VWAP while keeping >=7pp realized edge
        entry_hour_set=frozenset({1}),          # L2 champion 2026-05-29: hour {0} -> {1}
        min_bvol=50.0,
        max_vwap_slip_from_anchor=None,         # 2026-05-31: remove top-up slip leash;
                                                # rely on current gates + >=7pp VWAP edge
        # TAIL exits at +0.20 if the lottery hits a winner before resolution.
        # Mirrors backtest measure_tp_sl simulation. Closes are recorded with
        # reason='tail_tp' (live) or 'tail_tp_dry' (dry-run) by the TP/SL monitor.
        tp=0.20,
        sl=None,
        # L2 champion 2026-05-29: consensus_skip_threshold 0.50 -> 0.40
        # (candidate_l2_depth TAIL.consensus_skip_threshold). TAIL skips every
        # signal when any bracket in the same (station, target_date) shows YES
        # best_ask >= 0.40. NOTE: 0.40 cuts TAIL bet flow ~in half vs the prior
        # operator-chosen 0.50 (≈116 -> 49 bets over the 2026-05-17 eval window);
        # accepted as part of wholesale champion adoption.
        consensus_skip_threshold=0.40,
    ),
    "FLIP": StrategyConfig(
        # Mirror-of-champion YES experiment (operator-ordered 2026-08-09).
        # Fires on exactly the brackets where the NO gate fires (the gate is
        # re-evaluated inside _evaluate_flip_branch against the live NO price
        # using STRATEGY_CONFIGS["NO"]'s tuned band), then buys the YES token
        # instead of NO. Enabled ONLY via FLIP_MODE=1 in .env (see
        # runtime_config.Config.flip_mode + _apply_flip_mode below), which
        # simultaneously disables NO so the two sleeves can never take
        # opposite sides of the same bracket in one tick.
        #
        # Execution economics are inverted vs every other sleeve: the model
        # P(YES) is tiny by construction (the NO gate just said so), so the
        # walker's edge floor can never pass. min_edge=-1.0 therefore flows
        # through _min_edge_for_signal as the order-time walker floor
        # (accept any negative edge) and the ONLY fill boundary is the
        # max_walk_price leash: ask book consumed no deeper than
        # scanner-time YES ask + 0.05. Do not "fix" min_edge back to a
        # positive number — that silently turns FLIP into a no-op sleeve
        # that gates every fill.
        #
        # WARNING (measured, not hypothetical): flip of the real June+July
        # 2026 live bets at real recorded YES asks = -$89.36; EV under the
        # live model = -73% of stake. See session research 2026-08-09.
        side="YES",
        signal_name="p_E_flip",
        enabled=False,                          # boot-toggled by _apply_flip_mode (FLIP_MODE=1)
        fp_min=MIN_FILL_PRICE,                  # YES ask band; mirror of NO fp 0.75-1.00 is
        fp_max=0.50,                            # roughly ask 0.0-0.25, widened to 0.50 to
                                                # admit the NO ceiling-extension mirror (fp>=0.50)
        min_edge=-1.0,                          # order-time walker floor: accept negative edge
        max_edge=None,
        capital_frac=0.07,                      # same sizing dial as the NO champion sleeve
        max_walk_price=0.05,                    # sole fill leash: YES ask + 5c
        execution_min_edge=None,                # None => max_walk_price leash active
        entry_hour_set=frozenset(range(7)),     # same local hours as NO [0..6]
        min_bvol=50.0,
        tp=None,                                # hold to resolution, like the tested flip
        sl=None,
    ),
    "YHIGH": StrategyConfig(
        # High-tail favorite sniper. YES on bracket_kind=ceiling (X-or-higher)
        # markets where YES has moved into favorite territory. Locked spec
        # from backtest/results/per_strategy_detail.md (2026-05-07).
        side="YES",
        signal_name="p_B_50",
        enabled=False,                          # disabled in optimized NO+TAIL production profile
        fp_min=0.50,
        fp_max=1.00,
        min_edge=0.025,                         # additive, post-fee
        max_edge=0.30,
        capital_frac=0.010,                     # 1.0% MILD
        max_walk_price=0.05,
        entry_hour_set=frozenset({0}),
        min_bvol=50.0,
        tp=None,                                # YHIGH holds to resolution
        sl=None,
    ),
}

def _apply_flip_mode() -> None:
    """Boot-time sleeve swap for the operator's FLIP experiment.

    Reads ``flip_mode`` from the runtime Config (FLIP_MODE in .env). When on:
    NO.enabled -> False, FLIP.enabled -> True. Applied once at import so the
    whole process (scanner, walker's per-signal config lookups, TP/SL, tests
    that construct signals) sees one consistent registry; changing the flag
    requires restart_bot.sh. Fail-closed: any error reading config leaves the
    registry in its committed default (NO on, FLIP off).
    """
    try:
        from hightempbot.runtime_config import get_config
        flip_on = bool(get_config().flip_mode)
    except Exception:
        flip_on = False
    if not flip_on:
        return
    from dataclasses import replace as _dc_replace
    STRATEGY_CONFIGS["NO"] = _dc_replace(STRATEGY_CONFIGS["NO"], enabled=False)
    STRATEGY_CONFIGS["FLIP"] = _dc_replace(STRATEGY_CONFIGS["FLIP"], enabled=True)


_apply_flip_mode()


# YMID TP/SL flag age-out (F-001 from doc-review): if event_detail.close_in_flight
# has been set for longer than this, treat as stale, clear it, and reattempt the
# close on the next monitor tick. Guards against process crashes between flag-set
# and close_position invocation.
TP_SL_FLAG_STALE_SECONDS: int = 600  # 10 minutes

# Reconciler cadence: how often the in-process scheduler runs `reconcile_orders`
# to keep PENDING rows in sync with CLOB. 5 min default matches the existing
# TP/SL monitor cadence and is well below the MAX_PENDING_AGE_MINUTES gate
# below so a non-running reconciler doesn't silently drop PENDING from the
# slot-exposure SUM.
RECONCILE_INTERVAL_MINUTES: int = 5

# Stale-PENDING threshold for `slot_state` exposure: rows still PENDING after this
# many minutes are excluded from the slot's filled-exposure SUM **only if**
# the slot has at least one FILLED (non-PENDING, non-CANCELLED) row alongside
# them. The combined predicate prevents a stuck PENDING row from permanently
# locking a slot while still preserving exposure-counting when PENDING is the
# only state on the slot (so the slot stays locked rather than letting a
# top-up double-stake against an in-flight order).
#
# Default 240 min (4 hours) is intentionally large: realized fills become
# FILLED within seconds of the reconciler running, so 4h captures genuine
# reconciler-lag scenarios without dropping benign submitted-order rows.
# Operators tune lower if the reconciler is known fast.
MAX_PENDING_AGE_MINUTES: int = 240
