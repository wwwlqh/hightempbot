---
title: Backtest and Validation Suite
type: source
source_type: project_subdirectory
source_paths:
  - C:\Users\leowq\OneDrive\Desktop\hightempbot\backtest
ingested: 2026-05-05
updated: 2026-05-31
created: 2026-05-05
tags: [source, backtest, l2-depth, calibration, walk-forward, leakage-audit]
status: developing
---

# Backtest and Validation Suite

The `backtest/` subdirectory exists **outside** the live bot and is never imported by `hightempbot.*`. It answers one question:

| Folder | Question |
|---|---|
| `backtest/` | What is the *most profitable* strategy config on ~90 days of real Polymarket bracket data, executed against **real L2 order-book ladders** where available? |

> [!important] 2026-05-29 / 2026-05-31 — narrowed to the L2-depth champion chain
> The harness was pruned to the **L2 champion** build/run chain (commit `0706ca0` "Prune backtest folder to the L2 champion chain"); its champion, `configs/candidate_l2_depth.json` (variant `sel_taila40_fp03_cs40`), was ported wholesale into `src` on 2026-05-29 (commit `b07c13a` — see [[HighTempBot src module]] and [[Optimum Strategy]]). The 2026-06-04 sizing refresh measures OOS as one continuous B-D bankroll path: fixed NO 8% / TAIL 6% wins the diagnostic grid, while live uses the 2026-06-05 operator override NO 7% / TAIL 5% with the NO/TAIL first-fill VWAP top-up slip caps removed. The earlier non-L2 pipeline (`build_decision_table.py` → `decision_table.parquet`, `eval_strategy.py`, `optimize_under_realism.py`, `export_results.py`, the consensus-skip and monthly-expectation sweeps) and the separate **`validation/` directory** (`backfill.py` + `precompute.py` + `walkforward_backtest.ipynb`) were removed in the L2-only cleanup. This page now documents the current L2 chain; the removed-pipeline notes below are retained as historical context only. `backtest/README.md` is the authoritative current spec.

## backtest/ — the L2 champion chain

A leakage-safe walk-forward simulator that joins live-bot DB primitives to ~90 days of Polymarket price/metric history **and real PMD L2 book ladders**, generates candidate signals per row, and walks the actual ask ladder at execution. See [[Backtest Harness]].

**Pipeline (current):**

1. `backtest/scripts/fetch_polymarket_history.py` — pulls Polymarket bracket prices + metrics from `polymarketdata.co` into `backtest/data/polymarket_history.db` (Gamma API for slugs, PMD for 10-min OHLC). Also the shared PMD API client / rate limiter.
2. `backtest/scripts/fetch_polymarket_books.py` — downloads PMD `/books` L2 snapshots into `backtest/data/polymarket_books_10m.db` (large and expensive to rebuild; PMD only retains the rolling last ~90 days).
3. `backtest/scripts/build_l2_decision_table.py` — joins PMD book snapshots at-or-before each entry timestamp onto the base table, producing `backtest/data/decision_table_may11plus_l2.parquet` (~29k L2 rows). `build_decision_table.py` still builds the leakage-safe **non-L2** base (`decision_table_may11plus.parquet`) when a fresh base is needed.
4. `backtest/lib/live_match_eval.py` — candidate generator + execution model, including **real L2 ask-ladder walking** (`simulate_walk_book_l2`, which replaced the old linear-impact approximation — the optimizer FINDS fill rules from raw depth rather than hard-coding 5%/VWAP/walk-cap).
5. `backtest/scripts/sweep_l2_depth_features.py` — the L2 strategy feature sweep (output `results/l2_depth_feature_sweep.{csv,md}`).
6. `backtest/scripts/walkforward_l2_depth_features.py` — expanding **ABCD** selector: train A → test B, train A+B → test C, train A+B+C → test D (output `results/l2_depth_walkforward_abcd.{csv,md}`).

Shared helpers: `backtest/lib/sweep_lib.py` (decision-table paths, signal flavors, leakage helpers, PMD DB path), `backtest/scripts/measure_tp_sl.py` + `scripts/lut_range_chunks.py` (simulator/config + ABCD chunk/TP-SL helpers used by the sweep). Config: `configs/candidate_l2_depth.json` (champion). The retired non-L2 `configs/candidate1.json` baseline was removed; use git history if needed for comparison.

**Signal generation:** Every row carries 9 [[Signal Flavors]] computed walk-forward — `p_E` (raw EMOS), `p_L_strict`, `p_L_loose`, three blends (`p_B_50/30/70`), two shrinkage priors (`p_Shrink_n10/n50`), and a ramp (`p_Ramp`).

**Leakage audit (locked by `assert_no_leakage`):**

- EMOS `asof_date ≤ market_date` (and the fitter's window ends at `asof_date − 1`)
- LUT `local_date < market_date` (strict)
- Entry timestamp ≥ 4h before bracket close UTC
- L2 book joins are **at-or-before** the entry timestamp only (the 2026-05-28 dataset validation found 0 future-book joins and 0 joins over 90 minutes across 698,518 checked entry timestamps; ~60.75% L2 coverage)

The build refuses to write the parquet if any row violates.

## Current L2 champion

Champion: `configs/candidate_l2_depth.json`, variant `sel_taila40_fp03_cs40`. Strict-ABCD evidence (`results/l2_depth_walkforward_abcd.md`), recommended profile `l2_tail_book_no_no_retune` (risk-adjusted; excludes NO-side retunes that overfit chunk A):

| Metric | Value |
|---|---:|
| B–D out-of-sample PnL | +$251.96 |
| Worst OOS chunk | +$52.55 |
| Positive OOS chunks | 3/3 |
| Max test DD | 18.22% |
| OOS bets | 314 (NO +$143.02 / TAIL +$108.94) |

Settings ported to live: NO execution VWAP edge floor `0.05`, TAIL `0.07`; TAIL entry local hour `[1]`, `alpha=4.0`, signal `fp_max=0.03`, delayed-entry trigger `delayed_entry_fp_max=0.02`, station-level consensus skip `0.40`; YMID/YHIGH disabled. The 2026-06-09 delayed-entry WFO selected `wait_le_02c`: TAIL can signal through 3c, then waits for the first later `yes_ask <= 0.02` snapshot before placing (`+$2,537.24` B-D continuous OOS PnL, 26.18% max DD, worst chunk +$319.94, TAIL +$1,325.80 on 30 bets). The original L2 ask-premium guard (`side best ask - displayed entry price <= 0.10`) was removed on 2026-06-06 after a grid showed it did not improve the current profile; true depth now owns fill quality. Sizing is `size_frac * capital` with idempotent 10-min top-up, `NO.size_frac=0.0700`, and `TAIL.size_frac=0.0500`. The sizing sweep uses current equity as denominator and reports OOS sizing as one continuous B-D path from $100. Pure fixed B-D winner is NO 8% / TAIL 6% / cap 100% (+$1,496.77, 29.93% max DD); the current NO 7% / TAIL 5% / cap 100% fixed diagnostic row is +$1,345.48, final $1,445.48, 26.25% max DD, 91.1% actual open exposure, 96.5% target cap used, and 3/3 positive B-D segments. NO/TAIL first-fill VWAP top-up slip caps are disabled; **no static stake cap**. Shared backtest runners apply `TAIL.delayed_entry_fp_max` from the JSON; the delayed-entry WFO script disables it only to build immediate-entry and threshold-comparison variants. See [[Optimum Strategy]].

## Why this is split out from the live bot

Strict separation: `backtest/` may import from `hightempbot.*`, but never the reverse. This keeps live execution off the parquet codepath and ensures any backtest finding can be reproduced from the live primitives without a circular dependency.

Important distinctions baked into the L2 model:

- `volume24hr` is an activity gate, not fillable depth.
- L2 ask ladders are the fillable-depth evidence; PMD 10-minute snapshots do **not** include the market impact of our hypothetical order.
- Idempotent over-time top-up increases fill chances but does not create safe liquidity. Active NO/TAIL top-ups recheck current gates and realized VWAP edge floors; monitor fills that drift far from the first fill because the explicit first-fill VWAP leash is now disabled.
- Any winning config must beat its in-sample baseline AND survive the test window; never rank on test.

## Historical context (removed pipeline — retained for archaeology)

These sections describe code/data that no longer exists on disk. They predate the L2 chain.

### validation/ — calibration validation + data backfill (removed)

`validation/` held three files: `backfill.py` (filled `actuals` (WU) + `forecast_archive` (Open-Meteo) since 2021-01-01; WU sequential per station, parallelism across stations bounded at **10 workers × 0.5s** — the proven safe envelope, 27 workers caused SSL EOF), and `precompute.py` + `walkforward_backtest.ipynb` (the [[Walk-Forward Calibration Backtest]] — walk-forward EMOS fit on every prior day, scored against actuals, per-station BSS by month). Still-true invariants from that work:

- **Locked ensemble size:** `MIN_MEMBERS = REQUIRED_MEMBERS = 9`. BoM offline since 2025-07; validator and live must use the same set or the spread coefficient breaks.
- **Data restriction:** 9-member era only (`2024-03-01+`). Mixing 3- and 9-member regimes is a category error.
- **Per-month BSS:** qualification gates on `MIN(bss)` across all months, not the current month.

### Phase A–G optimization (2026-05-06, non-L2)

The pre-L2 optimization established: the **LUT bucket grid** in `sweep_lib.py::LUT_BUCKETS` matches live `calibration/lut.py::BUCKETS` (Phase A); a **24h refetch** (`limit=200`, full-day window) grew price rows 2.3M → 4.15M and made `entry_local_hour` analysis meaningful (Phase A'); **`entry_local_hour=0`** dominated for the non-L2 strategy (Phase D); **concurrent-exposure modeling** showed Kelly fails under the ~24h pending horizon and fixed-fraction sizing is the optimum (Phase F); per-strategy sizing tuned TAIL down (Phase G). **Forecast freshness verified not a leak:** Open-Meteo's `previous-runs-api` returns immutable historical runs; `temperature_2m_previous_day1` is the day-N−1 run regardless of query time (0 disagreements among 3,780 duplicate tuples). The non-L2 "final optimum" (NO `p_E` + YMID `p_Shrink_n50` + TAIL α=2.0) and the later 2026-05-24 robust-NO refresh are both **superseded** by the L2 champion — see [[Optimum Strategy]].

## Burned-by list (apply when running these)

- PMD only retains the rolling last ~90 days of `/books`; older slugs return empty/404. Rebuild `polymarket_books_10m.db` with `--missing-only` and accept null L2 rows (the simulator falls back to the synthetic model only at execution time for those).
- Re-run `build_l2_decision_table.py` whenever the base table or book DB is refreshed.
- L2 fill rules are **swept, not hard-coded** — `simulate_walk_book_l2` exposes raw depth and lets the optimizer find 5%/VWAP/walk-cap-equivalent behavior. Don't reintroduce hard-coded fill approximations.
- Validator/live must use the **same** `EXPECTED_MODELS` set; partial ensemble dates are filtered. Drift breaks the EMOS spread coefficient.
- `backtest/` must NOT be imported from `hightempbot.*`. The dependency arrow is one-way.

## Related

[[Backtest Harness]] · [[Decision Table]] · [[Signal Flavors]] · [[Walk-Forward Calibration Backtest]] · [[Optimum Strategy]] · [[EMOS Calibration]] · [[Walk-forward LUT]] · [[Edge-Preserving Sizing]] · [[HighTempBot src module]]
