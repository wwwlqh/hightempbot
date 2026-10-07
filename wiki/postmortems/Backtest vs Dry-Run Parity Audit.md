---
type: postmortem
title: "Backtest vs Dry-Run Parity Audit"
created: 2026-05-12
updated: 2026-05-12
incident_date: 2026-05-12
status: partially-resolved
severity: high
tags:
  - postmortem
  - backtest
  - calibration
  - parity
  - dry-run
related:
  - "Forecast Model"
  - "[[Walk-forward LUT]]"
  - "[[Backtest Harness]]"
  - "Live-Match Evaluator"
  - "[[EMOS Calibration]]"
  - "Strategy Config Registry"
  - "[[Optimum Strategy]]"
  - "[[Pandas 3 datetime64 Leakage Bug]]"
---

# Backtest vs Dry-Run Parity Audit

Investigation triggered 2026-05-12 after dry-run NO performance diverged from backtest expectation: live ran **-$14.07 across 4 days** while backtest's prior 88-day sim showed **+$420 test PnL on 186 NO bets** (~+$5/day average). Per-day comparison for 5/9 (only date with both backtest and live data) showed backtest expected **+$0.61** but live realized **-$9.41**. Of 70 backtest 4-day windows, **0 hit -$14**.

The gap was real, not sample noise. Audit identified **four** structural differences between what the backtest measures and what the live bot does. **Gap #1 (calibration training window) was the dominant cause.** Fixed and deployed 2026-05-12 06:11 UTC. Gaps #2-#4 remain outstanding pending validation of #1's effect.

## The four parity gaps (ranked by likely PnL impact)

### #1 Calibration training window — DOMINANT, FIXED 2026-05-12

| Side | Window | Sample size | σ behavior |
|---|---|---|---|
| Live (`model.py:retrain` pre-fix) | All historical pairs | ~600 | Stable ~0.5-1°C |
| Backtest (`lut.py:_walk_forward_params`) | 30-day rolling | ~30 | **Collapses to floor** (`c=-10` or `d=-10`) on many stations |

**Evidence of σ collapse**: backfilling `calibration_params_history` for 5/3-5/12 with the walk-forward fitter produced sigma-floor saturation on 7 of 12 high-volume stations:

| Station | Walk-forward (5/9 asof) | Live full-history |
|---|---|---|
| LEMD | c=**-10.000**, σ ≈ 0.007°C | c=-0.952, σ ≈ 0.62°C |
| EHAM | c=**-10.000** | c=-1.032 |
| KHOU | c=**-10.000** | c=-0.665 |
| KATL | c=**-10.000** | c=-0.444 |
| OEJN | d=**-10.000** | d=-2.031 |
| KMIA | c=**-10.000** | c=-1.765 |
| OPKC | c=**-10.000** | c=-0.440 |
| LIMC, EFHK, CYYZ, EGLC, KORD | normal | normal |

When σ collapses, the EMOS Gaussian becomes near-delta. Predicted p_E ≈ 0 for any non-modal bracket. The bot then bets NO at very low p_E (large positive edge), wins easily on most days, and loses catastrophically when actual lands in a non-modal bracket (the bullseye-loss pattern observed in live).

**Per-bracket evidence**: for the same (station, date, bracket), backtest's recomputed p_E differed by 2-6× from what live recorded:

| Live bet (5/9) | Live p_E | Backtest p_E | Live fp | BT fp_h0 |
|---|---|---|---|---|
| OPKC 35°C NO | 0.060 | **0.388** | 0.840 | 0.700 |
| KMIA 89°F NO | 0.029 | **0.183** | 0.890 | 0.670 |
| OEJN 37°C NO | 0.148 | **0.304** | 0.750 | 0.827 |
| EHAM 18°C NO | 0.160 | **0.336** | 0.740 | 0.945 |
| EFHK 15°C NO | 0.071 | **0.167** | 0.800 | 0.650 |

For the same day (5/9), backtest fired on 11 brackets across 10 stations vs live's 14 brackets across 12 stations — **only 3 stations overlapped**. The two systems were making essentially independent decisions.

**Fix shipped 2026-05-12 06:11 UTC**:
- `model.py:retrain` modified to use 30-day rolling window via `WHERE local_date > date('now', '-30 days')`.
- `MIN_PAIRS` lowered 30 → 20 in `model.py` to match backtest's `MIN_PAIRS_FOR_FIT` (`sweep_lib.py`). With a 30-day window, actuals gaps reduce effective pair count to ~25 typically; MIN_PAIRS=30 would have gated every retrain off.
- All 50 stations retrained on the new window; calibration_params now reflects 30-day fits. 7+ stations now show σ floor saturation matching backtest.

> [!note] 2026-05-14 follow-up — MIN_PAIRS restored to 30
> The 30→20 lowering above was reverted on 2026-05-14 during the src⇔backtest parity audit. `MIN_PAIRS=30` is the wiki-canonical readiness gate per [[EMOS Calibration]] and the asymmetry vs `lut.py::MIN_PAIRS_FOR_FIT=20` is intentional. Stations whose 30-day window yields <30 pairs will stall on `not is_ready()` and skip betting silently — monitor pipeline_health for `n_samples` warnings; backfill forecast_archive if widespread.

### #2 Tick discretization — NOT FIXED

| Side | Discretization | Chances per slot |
|---|---|---|
| Live | Every 10 min × 7 entry hours | **42** |
| Backtest | One snapshot per hour (downsampled from 10-min raw data) | **7** |

`live_match_eval.py:411` already scans multiple hours via `for hour in _hours(...)` with `emitted` mask for first-pass-wins per slot. So the gap isn't "1 vs 42" — it's "7 hourly snapshots vs 42 ten-minute ticks." Live still has **6× more intra-hour chances** to catch a price wobble into the gate band.

`backtest/polymarket_history.db` `prices` table actually stores 10-min snapshots (verified — fetcher uses `RESOLUTION = "10m"`). The hourly downsampling happens in `sweep_lib.py:load_entry_prices_by_local_hour:329-432`, which picks the latest snapshot whose ts ≤ `H:00:00` local for each hour H. The intermediate 5 snapshots per hour are discarded.

**Selection bias**: marginal-pass bets that fire because a single 10-min tick wobbled into the band have lower true win rate than bets that pass at multiple ticks. Live grabs both classes; backtest's hourly snapshot only sees the latter.

**Solution menu**:

| Solution | Effort | Effect | Risk |
|---|---|---|---|
| A. Throttle live betting tick to once per hour (`scheduler/jobs.py` change `minute=0,10,20,30,40,50` → `minute=0`, matches backtest's hourly snapshots) | small | Eliminates the 6× intra-hour selection bias | Low — loses retry chances if a tick fails (network glitch, etc.) |
| B. Backtest scans 10-min granularity (rebuild decision_table with `no_price_h0_m0`, `h0_m10`, … 42 cols per slot) | large | Backtest matches live exactly; re-tune Candidate #1 against realistic 42-tick selection bias | None (backtest only) — heavy data fetch + parquet rewrite, parquet grows ~6× to ~30 MB |
| C. Tighten `min_edge` to absorb noise (0.090 → 0.12) | small | Rejects marginal-band bets entirely | Medium — kills bet rate ~30%; throws out genuine high-edge bets that just have small wobble below 0.12 |
| D. Sustained-edge gate (require edge > floor for 30 consecutive minutes before firing) | medium | Best of both worlds — keeps real signal, rejects noise | Medium — adds 30-min latency, requires tuning N |

### #3 Fill price source — NOT FIXED

| Side | Source | What it is |
|---|---|---|
| Live | CLOB `/price` mark (preferred) → `/book` `best_ask(no_book)` fallback | Mark price from orderbook midpoint, or actual ask price |
| Backtest | Gamma `outcomePrices[1]` snapshot at hour boundary | Last-trade price |

Code refs: live in `scheduler/station_scanner.py:516-558`; backtest in `sweep_lib.py:load_entry_prices_by_local_hour`.

These can differ by 1-15¢ on thin books. Even with identical EMOS p_E, the gate's edge calculation `edge = (1 - p_E) - fp - fee(fp)` produces different decisions when fp differs.

**Likely fix**: shift backtest to use CLOB-style snapshot if available, or shift live to record fill_price provenance for diagnostic comparison. No clean immediate solution — this gap is operational rather than algorithmic.

### #4 Ensemble snapshot timing — NOT FIXED (probably small effect)

| Side | Source | When written |
|---|---|---|
| Live | `_fetch_ensemble` → Open-Meteo Previous Runs API at scanner-tick time, cached 22h in-process | Never persisted to DB |
| Backtest | `forecast_archive` table | Written by Sunday `weekly_forecast_backfill` job (post-hoc), enrollment, and monthly_retrain |

Same upstream API (Open-Meteo Previous Runs is reproducible), so for a given target_date the values *should* match. They diverge when:
- A model was temporarily unavailable at scanner-tick time (live skips that target_date) but is back by the Sunday backfill (backtest sees a complete ensemble).
- Open-Meteo silently revises an archived run (rare).

**Likely fix**: add a write to `forecast_archive` from `_fetch_ensemble` after each scanner-tick fetch. Then backtest reads what live actually saw. ~1-day implementation; no production risk.

## Other things checked, found in parity (no fix needed)

| Component | Status |
|---|---|
| LUT bucket grid (`BUCKETS` vs `LUT_BUCKETS`) | byte-identical |
| Signal flavors formula (`_compute_signal_flavors` vs `add_signal_flavors`) | byte-identical |
| Bracket parser semantics (`parse_bracket_bounds` vs `parse_bracket`) | byte-identical (since 2026-05-09 fix) |
| Outcome rule (`_bet_matches_winner` vs `won_yes`) | byte-identical (`actual >= lo AND actual < hi`) |
| Fee formula (`POLY_FEE_THETA × p × (1-p)`) | byte-identical |
| Resolution semantics | byte-identical |

The wiki's `lut_bucket_grid_mismatch` memory note is stale and should be retired.

## What was deployed 2026-05-12

| Time UTC | Change |
|---|---|
| 05:38 | `model.py` retrain switched to 30-day window + MIN_PAIRS lowered 30→20. All 50 DRY_RUN stations retrained successfully (46 ok, 4 failed for too-few-actuals on newly enrolled). |
| 06:11 | Full `feat/cross-tick-topup` branch deployed: 10 files (`execution/{config, decision, ledger, pipeline, polymarket_resolution, reconciliation, tp_sl_monitor, types}.py` + `scheduler/{jobs, station_scanner}.py`). Cross-tick top-up code, MAX_PENDING_EXPOSURE_PCT 0.70→1.00, MIN_BVOL 500→50, WU_CONSENSUS_MODE SHADOW→OFF, Candidate #1 strategy params all activated. |

Backup: `~/hightempbot/backups/src_pre_topup_20260512_140534.tgz`. Bot remains in `DRY_RUN=True`.

## Predicted convergence

If gap #1 was the dominant cause, live's per-day NO PnL should converge toward backtest's 5/2-5/9 average of **+$11.84/day** within 1-2 weeks of the deploy. Day-to-day swings will be larger (typical: -$8 to +$25 range) because the σ-collapsed calibration produces asymmetric outcomes (many small wins, occasional catastrophic losses).

## What to watch (next 1-2 weeks)

| Metric | Backtest target (5/2-5/9 average) | Action if missed |
|---|---|---|
| NO bet rate | ~24/day (was ~13/day pre-fix) | Investigate ensemble fetch coverage if much lower |
| NO win rate | 89-90% | If <80% on ≥150 bets, gap #2-#4 still material |
| Net PnL | +$5-12/day average | Same |
| σ-collapsed station bets win rate | 90%+ when modal forecast is correct | Observed lower → gap #2 (tick selection bias) is residual |

If after **150 NO bets** (~3-4 weeks) the gap is still >$3/day, escalate to gap #2 fix (Solution A: throttle live to hourly). If gap is <$1/day, declare parity fixed and move on.

## Rollback

```bash
ssh opc@<server-ip> "cd ~/hightempbot && tar xzf backups/src_pre_topup_20260512_140534.tgz && bash restart_bot.sh"
```

Restores all 10 cross-tick top-up files AND model.py to pre-2026-05-12 state.

## See also

Forecast Model · [[Walk-forward LUT]] · [[EMOS Calibration]] · [[Backtest Harness]] · Live-Match Evaluator · Cross-Tick Top-Up · Strategy Config Registry · [[Optimum Strategy]] · [[Pandas 3 datetime64 Leakage Bug]] · [[Edge-Preserving Sizing]]
