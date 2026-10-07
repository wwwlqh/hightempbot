---
title: Optimum Strategy
type: concept
created: 2026-05-06
updated: 2026-08-09
tags: [concept, backtest, strategy, optimization, no, tail, flip, calibration, l2-depth, sizing, tp-sl, candidate-l2-depth]
status: developing
---

# Optimum Strategy

> [!key-insight] Updated 2026-08-09 — calibrated NO gate + FLIP experiment; TAIL off
> Three changes since the 2026-05-29 L2 note below (which remains the base spec):
>
> 1. **Calibrated NO gate (2026-07-16/17).** The NO claim is reliability-calibrated before the edge math (`RELIABILITY_CALIBRATION_ENABLED=True`; `logit_blend` curve per bracket-unit group NO_C/NO_F from `reliability_curves`, fitted via `cli/fit_reliability.py`; identity when no curve — do NOT deploy without fitted curves). `no_min_edge` moved 0.090 → 0.04 (2026-07-16 walk-forward calibrated-gate sweep, OOS n=849) → **0.05** (2026-07-17, aligned to the 0.05 walker floor after real-fill scoring). `max_edge=0.15` unchanged. See [[2026-07-16 Calibrated NO Gate]].
> 2. **TAIL disabled 2026-07-17.** EV ≤ 0 at honest fills in every tested slice — the delayed-entry dump-to-2c trigger IS the adverse selection, and the L2 backtest's TAIL profit was a fill artifact. YMID/YHIGH stay disabled.
> 3. **FLIP sleeve live 2026-08-09 (operator order).** Buys YES on exactly the brackets where the NO gate fires; `FLIP_MODE=1` in the server `.env` swaps NO off / FLIP on at boot. Deployed despite the measured −EV verdict (flip of the real June+July 2026 live bets at real YES asks = −$89.36; model EV −73% of stake). Rollback = `FLIP_MODE=0` + restart. See [[2026-08-09 FLIP Sleeve]].
>
> `MAX_DD=0.40` full halt unchanged. Runtime truth: `src/hightempbot/execution/strategy_constants.py::STRATEGY_CONFIGS`. The L2 sections below stay as the base spec + history; where they say "TAIL re-enabled" or `no_min_edge: 0.090`, this note supersedes them.

> [!key-insight] 2026-05-29 — the L2-depth champion is the active production strategy
> Selected on **real PMD L2 order-book ladders** (not a synthetic linear-impact approximation) with leakage-safe expanding ABCD validation, then ported into `src` wholesale (commit `b07c13a`). Champion config: `backtest/configs/candidate_l2_depth.json`, variant `sel_taila40_fp03_cs40`. This supersedes the 2026-05-24 robust-NO + current-TAIL profile (now rollback/history below).
>
> **Strict-ABCD out-of-sample (B–D), risk-adjusted profile `l2_tail_book_no_no_retune`:**
>
> | Metric | Value |
> |---|---:|
> | B–D OOS PnL | +$251.96 |
> | Worst OOS chunk | +$52.55 |
> | Positive OOS chunks | 3/3 |
> | Max test DD | 18.22% |
> | OOS bets | 314 (NO +$143.02 / TAIL +$108.94) |
>
> **What changed vs robust-NO:** NO execution VWAP edge floor 0.03 → **0.05**; TAIL `alpha` 4.5 → **4.0**, `fp_max` 0.05 → **0.03**, entry hour {0} → **{1}**, consensus skip 0.50 → **0.40**, execution VWAP floor 0.03 → **0.07**, and **TAIL re-enabled** (overrides the 5cd8d83 pause; 0.40 consensus cuts TAIL flow ~in half). The L2 port originally added `MAX_L2_ASK_PREMIUM=0.10`, but the gate was retired on 2026-06-06; executable depth is now controlled by the realized VWAP edge floors. The 2026-06-04 sizing refresh measures OOS as one continuous B-D bankroll path: fixed NO 8% / TAIL 6% wins the diagnostic grid; live now uses the 2026-06-05 operator override **NO 7% / TAIL 5%** with NO/TAIL first-fill VWAP top-up slip caps removed. Top-ups rely on current gates plus the realized VWAP edge floors.
>
> **Source of truth:** `backtest/configs/candidate_l2_depth.json` (the JSON is canonical; `tests/test_strategy_constants.py` fails CI on drift). Evidence: `backtest/results/l2_depth_walkforward_abcd.md` and `backtest/results/tail_delayed_entry_wfo.md`. Runtime reads `src/hightempbot/execution/strategy_constants.py::STRATEGY_CONFIGS`.

## Active spec (L2-depth base + 2026-07/08 overrides)

```yaml
max_l2_ask_premium: null          # retired 2026-06-06; true depth owns fill quality
reliability_calibration: enabled  # 2026-07-16: logit_blend per NO_C/NO_F from
                                  # reliability_curves; identity without a fitted
                                  # curve (raw gate over-fires — fit before deploy)

NO:                               # champion sleeve; boot-disabled while FLIP_MODE=1
  signal: p_E
  signal_for_high_bracket: p_B_50
  no_min_fill_price: 0.75
  no_min_fill_price_for_high: 0.50
  no_min_edge: 0.050              # 2026-07-17 (0.090 → 0.04 on 07-16, → 0.05 on 07-17);
                                  # edge computed on the CALIBRATED claim
  max_edge: 0.150
  max_edge_for_high: 0.35
  entry_local_hours: [0, 1, 2, 3, 4, 5, 6]
  size_frac: 0.0700
  execution_min_edge: 0.05        # realized VWAP floor (was 0.03)
  max_vwap_slip_from_anchor: null # top-up slip leash removed 2026-05-31
  tp: null
  sl: null

FLIP:                             # operator experiment, live 2026-08-09 via FLIP_MODE=1
  side: YES                       # fires where the NO gate fires, buys YES instead
  signal: p_E_flip
  min_edge: -1.0                  # intentional: accept negative edge; the ONLY fill
                                  # leash is max_walk_price = scanner-time ask + 0.05
  fp_band: [0.01, 0.50]
  entry_local_hours: [0, 1, 2, 3, 4, 5, 6]
  size_frac: 0.0700               # same sizing dial as NO
  tp: null                        # holds to resolution
  sl: null

TAIL:
  enabled: false                  # DISABLED 2026-07-17: EV<=0 at honest fills; the
                                  # dump-to-2c trigger IS the adverse selection; the
                                  # L2 backtest's TAIL profit was a fill artifact
  vote_signals: [p_E, p_B_50, p_L_loose, p_Shrink_n10]
  alpha: 4.0                      # was 4.5
  n_required: 4
  fp_min: 0.001
  fp_max: 0.03                    # signal band (was 0.05)
  delayed_entry_fp_max: 0.02      # 2026-06-09 WFO: signal stays 0.03; place only when yes_ask <= 0.02
  entry_local_hours: [1]          # was [0]
  size_frac: 0.0500               # current equity-cap deployment 2026-06-05
  execution_min_edge: 0.07        # realized VWAP floor (was 0.03)
  max_vwap_slip_from_anchor: null # top-up slip leash removed 2026-05-31
  tp: 0.20
  sl: null
  consensus_skip_threshold: 0.40  # was 0.50

YMID, YHIGH:
  enabled: false

min_bvol: 50
max_dd_halve_threshold: 0.40     # backtest halves; live halts new entries
poly_fee_theta: 0.05
min_bet_usd: 1.00
# sizing = size_frac * capital with idempotent 10-min top-up; NO static stake cap
```

Delayed-entry evidence: `wait_le_02c` is the selected 2026-06-09 TAIL WFO row.
It keeps the TAIL signal band at `yes_ask <= 0.03`, waits for the first later
`yes_ask <= 0.02` snapshot before placing, and produced +$2,537.24 B-D
continuous OOS PnL, 26.18% max DD, worst chunk +$319.94, and +$1,325.80 TAIL
PnL on 30 TAIL bets. Shared backtest runners apply
`TAIL.delayed_entry_fp_max`; the dedicated delayed-entry WFO script only
disables it to build the immediate-entry baseline and threshold variants.

## Why these knobs (2026-05-29 L2 champion)

- **Real L2 ladders, not a guess.** The selector walks the actual PMD ask ladder per entry; fill rules were *found* by the sweep (`simulate_walk_book_l2`), not hard-coded. The live fill boundary is now the per-sleeve realized VWAP edge floor; the earlier `MAX_L2_ASK_PREMIUM` pre-walk dislocation guard was removed on 2026-06-06.
- **TAIL retune (`alpha` 4.5→4.0, `fp_max` 0.05→0.03, hour {0}→{1}, consensus 0.50→0.40).** The L2 sweep favored a slightly looser vote ratio, a cheaper fp ceiling, the hour-1 entry, and the PnL-optimal consensus skip. Net effect: ~half the TAIL bet flow vs 0.50, but cleaner OOS chunks.
- **Execution VWAP floors raised (NO 0.05, TAIL 0.07).** Fill deeper by VWAP while keeping the sleeve's realized edge above the floor. Since 2026-05-31, NO/TAIL no longer have a first-fill VWAP slip leash; current gates plus the realized edge floor are the live fill boundary.
- **NO 7% / TAIL 5% current equity-cap sizing.** The 2026-06-04 sizing refresh uses current equity as the exposure denominator and reports sizing OOS as one continuous B-D bankroll path from $100, with chunk A training-only. Fixed continuous B-D winner was NO 8% / TAIL 6% / cap 100% (+$1,496.77, final $1,596.77, 29.93% max DD, 90.0% actual open exposure, 97.7% target cap used); live uses the 2026-06-05 operator override NO 7% / TAIL 5% / cap 100% because it stays under the 30% DD training constraint while adding NO exposure (+$1,345.48, final $1,445.48, 26.25% max DD, 91.1% actual open exposure, 96.5% target cap used, 3/3 positive B-D segments). There is still no static stake cap; idempotent 10-minute top-up subtracts already-filled slot exposure.
- **`MAX_DD=0.40`**: live halts new entries at the threshold; the backtest harness halves target size there.
- **YMID/YHIGH remain disabled.**

## Historical notes

The sections below preserve the superseded 2026-05-24 robust-NO refresh plus earlier parity, validation, and tuning records. They are useful rollback context, but they are no longer the active production spec.

> [!note] 2026-05-24 — Robust NO + current TAIL (superseded by the L2 champion)
> The pre-L2 active profile. ABCD BR100, identical chunks/hours: robust NO + current TAIL **+$398.95** (A +$63.56 / B +$88.48 / C +$202.24 / D +$44.67, 4/4 positive, DD 21.24%, n=487) vs the then-current live NO+TAIL +$394.43 (3/4, DD 32.39%, n=685). Knobs: NO `fp_min` 0.70→0.75, `max_edge` 0.25→0.15, ceiling `max_edge_for_high` 0.30→0.35; TAIL unchanged (`alpha=4.5`, `fp=0.001..0.05`, consensus 0.50); YMID/YHIGH disabled; `MAX_DD=0.40`. Evidence file `robust_no_current_tail_2026-05-24.md` was removed in the L2-only cleanup.
> [!warning] 2026-05-09 — post-parity-fix harness: champion still wins test, but train PnL collapsed
> Two long-standing P0 parity bugs between backtest and live were fixed on 2026-05-09 (see [[Parity Report: src vs backtest (2026-05-09)]]):
> 1. **EMOS sigma floor:** backtest used `0.5°C`, live uses `0.1°C` (5× difference, since file creation).
> 2. **Bracket-bound parsing:** backtest used "extends-to-next-bracket-edge" (+0.5°F bias at every edge), live uses ROUND-rule midpoints.
>
> Three-way diagnostic finding: **bracket parser was 98% of the impact, sigma floor was 2%.** Sigma floor essentially does not bind on this dataset; the bracket-bound shift was the real driver of inflated train PnL.
>
> The 2-strategy production champion (NO + TAIL) on `optimize/all-bankroll-pnl-v3` still wins on test under the post-fix harness, but train numbers are very different:
>
> | Window | Pre-fix harness | Bracket-only fix | Both fixed (live parity) |
> |---|---|---|---|
> | BR100 train PnL | +$124.52 | +$0.51 | **+$3.46** |
> | BR100 test PnL | +$516.55 | +$613.01 | **+$624.11** |
> | TAIL train @ BR100 | +$69 | −$26 | **−$26** |
> | NO train @ BR100 | +$55 | +$26 | **+$29** |
>
> **Implications:**
> - Champion rankings are still directionally correct (NO + TAIL is the right pick), but absolute train PnL was inflated.
> - The earlier "test PnL is 2-4× train PnL" intuition is dead. The new train-to-test ratio at BR100 is roughly 200× — train is anaemic under correct semantics.
> - **TAIL's train season is now negative.** The previously profitable +$69 train was a bracket-parser-bias artifact (same 91 bets, different resolutions because bounds shifted by 0.5°F).
> - The train +$3 / test +$624 asymmetry is **not** a bug — it shows up identically with the bracket-only fix. Likely causes: test-aware selection bias from ce-optimize, regime shift between train (Feb-Apr) and test (Apr-May), and train season being structurally harder.
> - The 4-strategy YHIGH-inclusive spec below ALSO carries pre-parity-fix biases. Every per-BR number on this page is from the buggy harness. Treat as historical reference. Re-tune YHIGH/YMID on the post-fix harness before re-enabling.
>
> At that point, the active 2-strategy spec lived in `backtest/configs/candidate1.json`. That file is now a removed historical non-L2 baseline; the active source of truth is `backtest/configs/candidate_l2_depth.json`. Whether YMID/YHIGH should be re-enabled is an open question requiring fresh sweeps on the corrected harness.

> [!warning] 2026-05-09 — Three-chunk validation flagged the strategy as SUSPECT
> Frozen-param 3-chunk validation (`backtest/three_chunk_validation.py`):
>
> | Chunk | Days | Bets | PnL | $/bet |
> |---|---|---|---|---|
> | Chunk 1 (Feb 18 – Mar 12) | 23 | 186 | +$24.44 | +$0.13 |
> | Chunk 2 (Mar 13 – Apr 4) | 23 | 133 | **−$16.86** | −$0.13 |
> | Chunk 3 (Apr 5 – May 2) | 28 | 292 | +$624.11 | +$2.14 |
>
> **Chunk 3 is 99% of total PnL** ($624 of $632). The other two chunks roughly cancel. Per-bet edge swung 16× across adjacent 23-day periods. TAIL was negative in 2 of 3 chunks (−$15, −$9) before the +$265 chunk-3 outlier.
>
> **Realistic per-day edge expectation under normal regimes: ~+$0.15/day at BR100** (chunks 1+2 combined: +$7.58 over 46 days). The +$22/day from chunk 3 is regime-specific. Do not budget against it.
>
> **TAIL needs re-tuning or should be disabled.** Chunks 1+2 show TAIL as net negative; chunk 3 alone is a 78-bet outlier. The current `α=3.5 / 4-of-4 / fp 0.001-0.05` parameters are not stable.

> [!info] 2026-05-09 — Multi-split validation softens the SUSPECT verdict to MODERATE
> Multi train/test split validation (`backtest/multi_split_validation.py`) shows a more nuanced picture than the 3-chunk independent test:
>
> | Split | Train PnL ($/bet) | Test PnL ($/bet) | Verdict |
> |---|---|---|---|
> | A: 30+15 (Feb 18–Mar 19 / Mar 20–Apr 3) | −$4.77 (−$0.02) | +$22.22 (+$0.25) | TEST-ONLY |
> | B: 45+25 (Feb 18–Apr 3 / Apr 4–Apr 28)  | +$16.40 (+$0.05) | +$402.83 (+$1.77) | PASS |
> | C: 60+15 (Feb 18–Apr 18 / Apr 19–May 3) | +$230.93 (+$0.55) | +$200.76 (+$1.07) | PASS |
>
> **Why multi-split looks "better" than the original 60/30 split — boundary placement, not strategy improvement:**
>
> The original `measure_tp_sl.py` puts the train/test boundary at `2026-04-04`, which falls exactly on the regime transition (early breakeven → late favorable). That gives the extreme +$3 train / +$624 test split. Splits B/C slide the boundary 14–28 days INTO the favorable regime, moving some favorable-regime PnL from test to train. But the per-day rate inside the favorable period is unchanged (~$13–$22/day BR100 across all measurements).
>
> Both views measure the same regime-conditional reality. The 3-chunk SUSPECT verdict and the multi-split MODERATE verdict are not disagreeing — they're showing the same data through different boundary choices.
>
> **Deployment guidance:**
> - Realistic per-day expectation in "good regime" (Apr 5+): $13–$22/day at BR100, supported across all measurements.
> - Realistic per-day expectation in "early regime" (Feb–early April): breakeven.
> - **Re-optimization against multi-split on the same 73 days will not break this regime-conditioning** — it will just chase the smeared average. The honest answer is to deploy dry-run for 30+ days of new data and observe which regime persists. If live tracks $13+/day → favorable regime is normal. If live drops to <$1/day → the favorable regime was transient, TAIL needs disabling.

Canonical 4-strategy spec (pre-2026-05-08, leaky-harness era — kept for reference):

1. **YHIGH added** as 4th strategy (YHIGH Strategy) — high-tail favorite sniper
2. **NO `min_edge` bumped 0.02 → 0.025** for cleaner ROS + bracket-conditional looser gate on `bracket_kind=high`
3. **Sizing rebalanced**: NO 1.0% → **1.5%**, TAIL 0.5% → **0.25%** (was over-sized; half-Kelly fired too often)
4. **YMID SL dropped** — TP=0.15 contributes 80% of exit value; SL alone added only +$216 te while doubling spread-cost exposure
5. **Per-BR hardcoded stake-cap ladder** instead of universal caps

Historical source of truth: `backtest/configs/candidate1.json` was set to the parity-fixed 2-strategy champion (NO+TAIL) before later L2 replacement and removal; use git history for the deleted file. The 4-strategy spec described below is pre-parity-fix historical reference, not active.

> [!key-insight] One-line result
> **NO `p_E` + YMID `p_Shrink_n50` (TP=0.15) + TAIL α=2.0 + YHIGH `p_B_50`** with MILD-plus sizing (1.5%/1%/0.25%/1%), per-BR stake caps, halve-Kelly at 50% DD. Adding YHIGH is **strictly Pareto-positive** — more PnL AND lower DD at every bankroll.

## Strategy stack (4 strategies)

| | NO | YMID | TAIL | **YHIGH** |
|---|---|---|---|---|
| **Side** | NO | YES | YES | **YES** |
| **Bracket** | any (high gets looser gate) | any (mid by price band) | any (cheap by price band) | **`high` only** |
| **Signal** | `p_E` (`p_B_50` for high) | `p_Shrink_n50` | vote{p_E, p_B_50, p_L_loose, p_Shrink_n10}, α=2.0, n=4 | **`p_B_50`** |
| **Fill price band** | `np ∈ [0.70, 1.00]` (`np ∈ [0.50, 1.00]` for high) | `yp ∈ [0.10, 0.50]` | `yp ∈ [0.001, 0.10]` | **`yp ∈ [0.50, 1.00]`** |
| **Edge gate** | `(1−p)−np−fee ∈ [0.025, 0.20]` (0.30 cap for high) | `p ≥ 1.3·yp`, edge ≤ 0.30 | 4/4 vote at α=2.0 | **`p−yp−fee ∈ [0.025, 0.30]`** |
| **Sizing** | **1.5%** of capital | **1.0%** of capital | **0.25%** of capital | **1.0%** of capital |
| **TP** | None | **+0.15** | None | **None** |
| **SL** | None | **None** (dropped 2026-05-07) | None | **None** |
| **Stake cap** | per-BR ladder | per-BR ladder | per-BR ladder | per-BR ladder |
| **Win rate** | ~92% | ~25% | ~7% | **~93%** |
| **Bets / 88d** | 2,689 | 977 | 735 | **18** |
| **Entry hour** | h=0 (multi-hour optional) | h=0 only | h=0 only | h=0 only |

## Sizing rule

```
target_usd = capital × strategy_size_frac
target_usd = min(target_usd, stake_caps_by_bankroll[BR_bracket][strategy])

if drawdown_from_peak >= 0.50:                 # backtest: halve target_usd
    target_usd *= 0.5                           # LIVE (since 2026-05-20): pipeline halts entirely;
                                                # this halve path is dead in production.
                                                # See [[Edge-Preserving Sizing#Drawdown Tracking]].

target_usd = max(target_usd, MIN_BET_USD = 1.00)
skip if target_usd > capital
```

Walk-book then trims further if the realized VWAP would break the strategy's edge floor (NO: 0.025, YHIGH: 0.025, YMID/TAIL: 0).

## Stake caps by bankroll (hardcoded, simple)

```yaml
BR_1k:    NO=$50,  YMID=$10,  TAIL=$3,  YHIGH=$15
BR_5k:    NO=$150, YMID=$25,  TAIL=$7,  YHIGH=$50
BR_10k:   NO=$300, YMID=$50,  TAIL=$10, YHIGH=$100   # recommended start
BR_50k:   NO=$750, YMID=$50,  TAIL=$10, YHIGH=$200
BR_100k:  NO=$750, YMID=$100, TAIL=$25, YHIGH=$200
```

Re-tune when bankroll crosses 2× a row. Beyond $100k: parallel-deploy multiple accounts rather than scaling caps further (TAIL liquidity ceiling).

> [!note] Why hardcoded caps not liquidity-aware
> Tested `min(capital × frac, entry_liquidity × 10%)` (liquidity-aware sizing) — generalizes principled across BRs but adds runtime complexity for marginal benefit at the operational $1k–$50k range. Hardcoded $-caps are simpler, more debuggable, and match the same average stakes that the liquidity-aware rule would produce. (Liquidity-aware sizing was an experimental sweep that did not graduate to its own wiki page.)

## NO bracket-conditional refinement (NEW)

The base NO gate is `np ≥ 0.70 AND edge ∈ [0.025, 0.20]`. On `bracket_kind=high` brackets, the same gate is loosened:

```python
# Base NO gate (any bracket)
if np_p >= 0.70 and 0.025 <= edge <= 0.20:
    take_no_bet()

# Bracket-conditional extension (only on bracket_kind=high)
elif bracket_kind == "high" and np_p >= 0.50 and 0.025 <= edge <= 0.30:
    take_no_bet()
```

This captures **40 train + 34 test** new bets that base NO rejects (np ∈ [0.50, 0.70] on high tails, or edge ∈ [0.20, 0.30] on high tails). Estimated marginal: **+$70 train / +$35 test PnL @ $10k BR**.

Why high-tail brackets specifically tolerate looser gates: fatter pricing dispersion than mid brackets due to fat-tail forecast uncertainty. Mid brackets converge tightly to climatology — same loosening would let in noise.

## Full historical config (formerly `backtest/configs/candidate1.json`)

```yaml
NO:
  signal: p_E
  signal_for_high_bracket: p_B_50
  no_min_fill_price: 0.70
  no_min_fill_price_for_high: 0.50
  no_min_edge: 0.025
  max_edge: 0.20
  max_edge_for_high: 0.30
  size_frac: 0.0150
  tp: null
  sl: null

YES_mid:
  signal: p_Shrink_n50
  alpha: 1.3
  fp_min: 0.10
  fp_max: 0.50
  size_frac: 0.0100
  tp: 0.15
  sl: null                   # dropped 2026-05-07

TAIL:
  vote_signals: [p_E, p_B_50, p_L_loose, p_Shrink_n10]
  alpha: 2.0
  n_required: 4
  fp_min: 0.001
  fp_max: 0.10
  size_frac: 0.0025          # halved from 0.005 (was over-sized)
  tp: null
  sl: null

YHIGH:                       # NEW 2026-05-07
  signal: p_B_50
  bracket_kind: high
  fp_min: 0.50
  fp_max: 1.00
  min_edge: 0.025
  max_edge: 0.30
  size_frac: 0.0100
  tp: null
  sl: null

stake_caps_by_bankroll:
  BR_1k:    {NO:50,  YMID:10,  TAIL:3,  YHIGH:15}
  BR_5k:    {NO:150, YMID:25,  TAIL:7,  YHIGH:50}
  BR_10k:   {NO:300, YMID:50,  TAIL:10, YHIGH:100}
  BR_50k:   {NO:750, YMID:50,  TAIL:10, YHIGH:200}
  BR_100k:  {NO:750, YMID:100, TAIL:25, YHIGH:200}

min_bvol: 50
max_dd_halve_threshold: 0.50
poly_fee_theta: 0.05
min_bet_usd: 1.00
entry_local_hour: 0
```

## Bankroll deployment table (4-strategy)

| BR | NO/YMID/TAIL/YHIGH stake | TR PnL / DD | TE PnL / DD / Ret% | Verdict |
|---|---|---|---|---|
| $1k    | $15 / $10 / $3 / $15  | +$1,539 / 41.6% | +$5,586 / 18.0% / +559% | high variance |
| $5k    | $75 / $50 / $13 / $50 | +$5,000 / ~30%  | +$11,500 / ~14% / +230% | borderline |
| **$10k**   | **$300/$50/$10/$100**  | **+$10,877 / 25.2%** | **+$20,240 / 9.8% / +202%** | **recommended start** ⭐ |
| $50k   | $750/$50/$10/$200 | +$14,749 / 16.5% | +$29,896 / 6.4% / +60% | sweet spot |
| **$100k**  | **$750/$100/$25/$200** | **+$15,232 / 9.8%** | **+$35,519 / 3.9% / +36%** | **mature** ⭐ |

PnL plateaus at ~$15k train / $35k test once all caps bind. Higher BR → lower percent return on capital but cleaner DD.

## Per-strategy contribution @ $10k BR

```
NO    TR n=1196 pnl=$+4446  stake=$127960  ROS=+3.5%   ← bread-and-butter (60% of PnL)
YMID  TR n= 408 pnl=$+2598  stake=$ 20321  ROS=+12.8%  ← specialist (35% of PnL on 21% of bets)
TAIL  TR n= 296 pnl=$ +123  stake=$  2960  ROS=+4.2%   ← lottery (small TR, big TE upside)
YHIGH TR n=  10 pnl=$ +250  stake=$   942  ROS=+26.5%  ← sniper (highest ROS, lowest n)
```

## YMID TP-only update (2026-05-07)

Contribution analysis showed:

| Config | YMID TR PnL | YMID TE PnL | Δ vs no-exits |
|---|---|---|---|
| No exits (baseline) | +$1,244 | +$2,444 | — |
| **TP=0.15 only** | **+$2,462** | **+$5,350** | **+$1,218 / +$2,906** |
| SL=0.10 only | +$1,540 | +$2,693 | +$296 / +$249 |
| TP=0.15 + SL=0.10 | +$2,598 | +$5,566 | +$1,354 / +$3,122 |

**TP carries 80% of the exit benefit; SL adds only +$216 te above TP-alone.** Combined with the bid-ask asymmetry concern (YMID exit crosses thin bid-side ladder), SL was dropped. Net: simpler config, less spread-cost exposure, ~5% PnL trade-off acceptable.

## Why each strategy works

**NO `p_E`** — raw EMOS for mid brackets, `p_B_50` for high brackets. Lower-variance Gaussian for saturated favorites. The bracket-conditional loosening on high tails captures the 5-7% of NO opportunities the strict gate misses.

**YMID `p_Shrink_n50` + TP=0.15** — Bayesian shrinkage with strong EMOS prior. TP captures bets whose price has moved favorably (73% of TP-firers also resolve YES at close anyway, but TP locks in $0.13/share with no fee tail risk). SL was dropped — too marginal to justify the round-trip spread cost.

**TAIL α=2.0 (no exits)** — 4-vote unanimous on cheap tails. 7% WR with 10×–1000× payoffs; positive EV (+12%/bet) but extreme variance. Sized at 0.25% (was 0.5%) — empirical sweep showed 0.5% over-extended into half-Kelly territory during normal TAIL losing streaks, suppressing the rare big winners.

**YHIGH `p_B_50` (no exits)** — see YHIGH Strategy. ~93% long-run WR puts it in the saturated-favorites regime where exits hurt (same as NO).

## Pre-deployment checklist

> [!info] This checklist is for the 4-strategy pre-parity spec (historical reference)
> The active strategy (as of 2026-08-09) is the **L2-depth base + calibrated NO gate (2026-07-16/17) + FLIP experiment (2026-08-09)**; verify against `STRATEGY_CONFIGS` at HEAD: `NO fp_min=0.75`, `NO min_edge=0.05` (was 0.090; edge computed on the reliability-calibrated claim), `NO max_edge=0.15`, `NO max_edge_for_ceiling=0.35`, `NO execution_min_edge=0.05`, `NO capital_frac=0.070`, `NO max_vwap_slip_from_anchor=None`; `RELIABILITY_CALIBRATION_ENABLED=True` with an ACTIVE `logit_blend` curve per NO_C/NO_F in `reliability_curves` (identity without one — do not deploy uncurved); `ALLOWED_BRACKET_UNITS={F,C}`; `TAIL enabled=False` (disabled 2026-07-17; its tuned params — `alpha_ratio=4.0`, `fp_max=0.03`, `delayed_entry_fp_max=0.02`, `entry_hour_set={1}`, `execution_min_edge=0.07`, `capital_frac=0.050`, `consensus_skip_threshold=0.40` — stay in the registry for rollback); YMID/YHIGH `enabled=False`; `FLIP` boot-enabled only via `FLIP_MODE=1` (which disables NO): `capital_frac=0.070`, `min_edge=-1.0` (intentional), `max_walk_price=0.05` as the sole fill leash, holds to resolution; `MAX_DD=0.40` (full halt); `MAX_L2_ASK_PREMIUM` is no longer live. `backtest/configs/candidate_l2_depth.json` remains the L2-base source of truth and `tests/test_strategy_constants.py` enforces parity on the ported values.

- [ ] `LUT_BUCKETS` in `backtest/lib/sweep_lib.py` matches `src/hightempbot/calibration/lut.py`
- [ ] `backtest/data/decision_table.parquet` rebuilt under 24h refetch data (4.15M rows)
- [ ] `MIN_BET_USD = 1.00` in `src/hightempbot/execution/strategy_constants.py`
- [ ] Per-strategy sizing implemented: NO 1.5%, YMID 1.0%, TAIL 0.25%, **YHIGH 1.0%**
- [ ] **NO `min_edge = 0.025`** (was 0.02)
- [ ] **NO bracket-conditional looser gate on `bracket_kind=high`** (np≥0.50, max_edge=0.30, signal=p_B_50)
- [ ] **YHIGH wired**: bracket_kind=high gate, p_B_50 signal, fp [0.50,1.00], edge [0.025,0.30]
- [ ] **YMID TP=0.15 only** (SL removed)
- [x] Superseded: live pipeline **halts** at 50% DD since 2026-05-20; this historical 4-strategy checklist's halve wording is no longer production behavior.
- [ ] Per-strategy `max_stake_usd` per BR-row (see ladder above)
- [ ] `entry_local_hour` logic per strategy (NO multi-hour, YMID/TAIL/YHIGH h=0)
- [ ] Idempotency gate at `decision/strategies.py` ~L488-500 left ON

## Operational rules

| rule | reason |
|---|---|
| Start at $10k+ BR if available | DD comfortably under gate; sweet spot returns |
| At $1k BR, expect 40%+ DD bursts | MILD-plus sizing on small capital is high-variance |
| Halt at 50% DD is automated (since 2026-05-20) | pipeline returns empty CycleResult at the threshold; existing PENDINGs still resolve. See [[Edge-Preserving Sizing#Drawdown Tracking]]. |
| Don't exceed $100k without parallel deployment | TAIL liquidity ceiling |
| Re-tune YMID/TAIL/YHIGH caps quarterly | market liquidity drifts; cap should track typical depth |
| YHIGH and NO-on-high cold start ~2 weeks | wait for LUT high-bucket coverage |
| Watch forecast pipeline gaps | calibration_params drift breaks NO/YHIGH most |

## Related

- [[2026-07-16 Calibrated NO Gate]] — reliability-calibrated NO edge gate; TAIL disabled 2026-07-17
- [[2026-08-09 FLIP Sleeve]] — operator-ordered YES-mirror experiment (live)
- [[2026-05-24 Robust NO + Current TAIL]] — superseded strategy profile (pre-L2 champion)
- [[Backtest Harness]] — the harness that produced this
- YHIGH Strategy — the 4th strategy added 2026-05-07
- Live-Match Evaluator — execution model used for sizing/walk-book/TP-SL
- [[Signal Flavors]] — the 9 p_model variants
- [[Decision Table]] — the 20k-row evidence base
- [[Edge-Preserving Sizing]] — sizing math
