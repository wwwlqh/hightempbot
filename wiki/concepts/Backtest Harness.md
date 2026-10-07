---
title: Backtest Harness
type: concept
created: 2026-05-05
updated: 2026-05-31
tags: [concept, backtest, walk-forward, leakage, polymarket]
status: stable
---

> [!important] 2026-05-08 — pandas 3 leakage bug fixed
> The per-hour readiness gate at `backtest/lib/live_match_eval.py` (~L349) and `backtest/lib/sweep_lib.py` (~L641) collapsed under pandas 3 due to a datetime64-unit assumption, nearly doubling the bet stream and inflating BR100 test PnL from $516.55 to $1138-$1495. Fixed in commits `25cc373` + `3dc93ea` on `optimize/all-bankroll-pnl-v3`. Both pandas 2.3.3 and 3.0.2 now produce identical results. See [[Pandas 3 datetime64 Leakage Bug]] for the full root cause. Any pre-fix sanity baseline below that was measured on `.venv` pandas 3 should be re-verified.

> [!note] 2026-05-19 — backtest/ reorg
> The flat `backtest/` directory was reorganized into `lib/`, `scripts/`, `configs/`, and `data/` subfolders. The shared library files are now `backtest/lib/sweep_lib.py` and `backtest/lib/live_match_eval.py`; one-shot tooling lives under `backtest/scripts/`; configs under `backtest/configs/`; built artifacts under `backtest/data/`. `tp_sl_config.json` was renamed to `configs/candidate1.json`; that retired non-L2 config was later removed after the L2 champion became the only active config.

> [!important] 2026-05-29 / 2026-06-01 — narrowed to the L2-depth champion chain
> The harness was pruned to the L2 champion build/run chain (commit `0706ca0`); the non-L2 sweeps/result files and the whole `validation/` directory were removed. The champion is now `configs/candidate_l2_depth.json` (variant `sel_taila40_fp03_cs40`), selected on **real PMD L2 book ladders** with leakage-safe expanding ABCD validation, and ported into `src` on 2026-05-29 (commit `b07c13a`). The 2026-06-04 sizing refresh measures OOS as one continuous B-D bankroll path: fixed NO 8% / TAIL 6% wins the diagnostic grid, while live uses the 2026-06-05 operator override NO 7% / TAIL 5% with the NO/TAIL first-fill VWAP top-up slip caps removed. The folder tree, pipeline, and "Production config" sections below are updated for the L2 chain; pre-L2 numbers in the "Sanity baselines" tables are rollback/history only. See `backtest/README.md` and [[Backtest and Validation Suite]].

# Backtest Harness

The `backtest/` subdirectory is a leakage-safe walk-forward strategy explorer that lives outside the live bot. It joins live-bot DB primitives (forecasts, actuals, EMOS history, LUT history) to 90 days of Polymarket bracket prices and metrics, generates 9 candidate signals per row, and evaluates strategy configs against P&L outcomes.

> [!key-insight] Independence guarantee
> `hightempbot.*` does not import anything from `backtest/`. The arrow is one-way. This means any pipeline change inside the live bot can be replayed against the backtest harness without circular dependencies — and any backtest finding can be reproduced from the live primitives alone.

## Why a separate harness exists

The live bot's edge is asserted by [[EMOS Calibration]] + [[Walk-forward LUT]] producing a calibrated `prob_safe_floor`, gated by [[Edge-Preserving Sizing]]. Whether that edge survives realistic execution — fees, spreads, walk-book impact — is an *empirical* question. The harness answers it on real bracket history without replaying live trades.

## Inputs

Two SQLite databases:

| DB | Provided by | Tables read |
|---|---|---|
| `data/hightempbot_server_latest.db` | live bot snapshot | `actuals`, `forecast_archive`, `calibration_params_history`, `pred_bucket_history`, `enrolled_stations` |
| `backtest/data/polymarket_history.db` | `backtest/scripts/fetch_polymarket_history.py` | `markets`, `prices`, `metrics` |

Tables the harness deliberately **never** reads:

- `lut_bucket_stats` — current LUT snapshot, full history aggregated → leakage
- `calibration_params` — current EMOS snapshot → leakage

Walk-forward semantics force the harness to use only `*_history` tables joined as-of `market_date`.

## Folder structure (post-2026-05-19 reorg)

Verified against the live tree on 2026-05-29 after the L2-champion prune (commit `0706ca0`). The earlier non-L2 sweep/result files (`monthly_expectation.py`, `sweep_consensus_skip{,_2d}.py`, `lut_bucket_combinations.py`, `analyze_station_pnl_stability.py`, and their reports) and the whole `validation/` directory were removed; the harness is now the L2 build/run chain only.

```
backtest/
├── README.md
├── configs/
│   └── candidate_l2_depth.json     <- ACTIVE L2 champion (variant sel_taila40_fp03_cs40)
├── data/
│   ├── decision_table_may11plus.parquet     ← non-L2 base table
│   ├── decision_table_may11plus_l2.parquet  ← L2 decision table used by the sweep
│   ├── polymarket_history.db                ← PMD price/metadata
│   ├── polymarket_books_10m.db              ← PMD L2 book snapshots (large)
│   └── manifest.csv
│
├── lib/
│   ├── sweep_lib.py                ← shared paths/flavors/leakage helpers + PMD DB path
│   └── live_match_eval.py          ← candidate generator + real L2 ask-ladder walk (simulate_walk_book_l2)
│
├── scripts/
│   ├── fetch_polymarket_history.py ← PMD price/metrics + shared API client/rate limiter
│   ├── fetch_polymarket_books.py   ← PMD /books downloader → polymarket_books_10m.db
│   ├── build_decision_table.py     ← non-L2 base builder
│   ├── build_l2_decision_table.py  ← joins book snapshots at-or-before each entry ts
│   ├── sweep_l2_depth_features.py  ← L2 strategy feature sweep
│   ├── walkforward_l2_depth_features.py ← expanding ABCD selector (A→B, A+B→C, A+B+C→D)
│   ├── measure_tp_sl.py            ← simulator/config helpers used by the L2 sweep
│   └── lut_range_chunks.py         ← ABCD chunk + TP/SL helpers
│
└── results/
    ├── l2_depth_feature_sweep.{csv,md}       ← full L2 sweep output
    └── l2_depth_walkforward_abcd.{csv,md}    ← strict ABCD selector evidence
```

The old default `decision_table.parquet` and the non-L2 result artifacts (`robust_no_current_tail_2026-05-24.md`, consensus-skip sweeps, sizing/capacity diagnostics, dataset-validation outputs) were intentionally removed in the L2-only cleanup.

## Pipeline (L2 chain)

```
fetch_polymarket_history.py  →  data/polymarket_history.db
fetch_polymarket_books.py    →  data/polymarket_books_10m.db
                                       │
build_l2_decision_table.py  ←─ base table + book DB  (lib/sweep_lib.py for shared joins/asserts)
        ▼
data/decision_table_may11plus_l2.parquet  (~29k L2 rows)  ← the "evidence base"
        │
        ├─► lib/live_match_eval.py              — candidate generator + real L2 ask-ladder walk
        ├─► scripts/sweep_l2_depth_features.py  — L2 feature sweep
        └─► scripts/walkforward_l2_depth_features.py — strict expanding ABCD selector
```

## decision_table_may11plus_l2.parquet (build output)

See [[Decision Table]] for the base column schema. Rough shape:

- ~29,190 L2 rows = (station, market_date, bracket_index) with a non-null L2 book ladder joined at-or-before the entry timestamp (≈60.75% coverage; remaining rows fall back to the synthetic liquidity model at execution time only).
- 9 [[Signal Flavors|p_model variants]] computed per row.
- All EMOS/LUT joins use strict-asof semantics; L2 book joins are at-or-before the entry timestamp (0 future joins, 0 joins over 90 min in the 2026-05-28 validation).
- `assert_no_leakage` raises if any row violates the walk-forward constraint.

## Eval config schema

The current entry points are `backtest/scripts/sweep_l2_depth_features.py` (the L2 feature sweep) and `backtest/scripts/walkforward_l2_depth_features.py` (the strict ABCD selector), both reading `configs/candidate_l2_depth.json` via `lib/sweep_lib.py` + `scripts/measure_tp_sl.py`. The fields below document the legacy single-config schema for context; the live `StrategyConfig` dataclass is in `src/hightempbot/execution/strategy_constants.py::STRATEGY_CONFIGS` and the current backtest schema is the multi-strategy `candidate_l2_depth.json`.

Legacy single-config schema (historical):

```json
{
  "side": "YES" | "NO",
  "signal": "p_E" | "p_L_strict" | "p_L_loose" | "p_B_50" | "p_B_30" | "p_B_70"
            | "p_Shrink_n10" | "p_Shrink_n50" | "p_Ramp",
  "min_edge": -0.20..0.20,
  "max_edge": min_edge+0.02..0.40,
  "min_fill_price": 0.0..1.0,
  "max_fill_price": min_fp+0.02..1.0,
  "bracket_kind": "low" | "mid" | "high" | "all",
  "min_liquidity": >= 0,
  "min_lut_n": >= 0,
  "min_alpha_ratio": >= 0,
  "entry_local_hour": 0..23
}
```

Edge formula mirrors live execution and is still used by every current evaluator:

- **YES**: `edge = p_model − yes_price − fee(yes_price)` where `fee(p) = 0.05·p·(1−p)`
- **NO**: `edge = (1 − p_model) − no_price − fee(no_price)`

Output JSON shape: `{train_n, train_pnl, train_sharpe, train_win_rate, train_avg_edge, test_*, config}` (consistent across `measure_tp_sl.py` and the sweep scripts).

Window split: train = `2026-02-04 → 2026-04-04` (60d), test = `2026-04-05 → 2026-05-02` (28d).

## Selection rule

Rank by **train sharpe or train return** — never test. Reject any candidate where `test_pnl ≤ 0` or `test_n < 30`. This avoids the standard backtest sin where the optimizer finds a config that happens to fit the test window.

## What's NOT in the harness

- Real order-book ladder — synthetic linear-impact VWAP from per-snapshot `entry_liquidity` + `entry_spread`. The live bot's `walk_book_edge_preserving` enforces edge against the actual CLOB at execution.
- Trade tape — only mid-price snapshots at ~10-min intervals. Direction can be inferred from yp deltas but actual aggressor not visible.
- Per-level book asymmetry — your screenshot of a real ASK ladder ($46 deep) vs BID ladder ($9 thin) is collapsed to one `liquidity` scalar. Affects YMID exits most (round-trip cross of bid-ask).

## Sanity baselines and active champion

> [!key-insight] Active anchor is now the L2 champion (2026-05-29)
> The current regression anchor is the **L2-depth champion** (`configs/candidate_l2_depth.json`, variant `sel_taila40_fp03_cs40`), validated on real PMD L2 book ladders. Strict-ABCD out-of-sample (B–D): **+$251.96**, worst OOS chunk +$52.55, **3/3 positive**, max test DD 18.22%, 314 OOS bets (NO +$143.02 / TAIL +$108.94). Evidence: `backtest/results/l2_depth_walkforward_abcd.md`, recommended profile `l2_tail_book_no_no_retune` (risk-adjusted; excludes NO-side retunes that overfit chunk A). Drift from this OOS row means the parquet, PMD book backfill, base DB, or harness changed — re-run `walkforward_l2_depth_features.py`.
> The 2026-06-09 TAIL delayed-entry WFO selected `wait_le_02c`: keep the TAIL signal band at `yes_ask <= 0.03`, then wait for a later `yes_ask <= 0.02` before placing. Evidence: `backtest/results/tail_delayed_entry_wfo.md` (`+$2,537.24` B-D continuous OOS PnL, 26.18% max DD, worst chunk +$319.94, TAIL +$1,325.80 on 30 bets). Drift from this row means the price history, PMD DB, or delayed-entry harness changed — re-run `sweep_tail_delayed_entry_wfo.py`.

The non-L2 lineage below (verified on pandas 2.3.3 / 3.0.2 after the datetime64[s] fix and the 2026-05-09 live-parity fixes) is **rollback/history only** since the L2 port:

| Profile | Window | Total | A | B | C | D | Positive chunks | Max DD | Bets |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Robust NO + current TAIL (2026-05-24, superseded) | ABCD through 2026-05-21 | +$398.95 | +$63.56 | +$88.48 | +$202.24 | +$44.67 | 4/4 | 21.24% | 487 |
| Current live NO+TAIL baseline (pre-L2) | Same chunks/hours | +$394.43 | +$93.46 | -$0.55 | +$239.36 | +$62.15 | 3/4 | 32.39% | 685 |

The 2026-05-24 robust-NO profile (NO `fp_min=0.75`/`min_edge=0.090`/`max_edge=0.15`, ceiling `fp_min=0.50`/`max_edge=0.35`; TAIL `alpha=4.5`, `fp=0.001..0.05`, consensus skip 0.50) was the active profile *before* the L2 port; the L2 champion retuned TAIL to `alpha=4.0`/signal `fp_max=0.03`/hour {1}/consensus 0.40 and raised per-sleeve realized VWAP edge floors. On 2026-06-09, delayed-entry WFO added `delayed_entry_fp_max=0.02`: TAIL can signal through 3c but only sizes/places at 2c or cheaper. The selected `wait_le_02c` row is recorded at +$2,537.24 B-D continuous OOS PnL, 26.18% max DD, and +$1,325.80 TAIL PnL. Live target size is NO 7% and TAIL 5%; the fixed sizing diagnostic runs one continuous B-D path from $100, not reset-summed chunks (+$1,345.48, final $1,445.48, 26.25% max DD, 91.1% actual open exposure, 96.5% target cap used). NO/TAIL first-fill VWAP top-up slip caps are removed. The 2026-06-06 operator override retired `max_l2_ask_premium`; executable price quality is owned by the true-depth walker. The 2026-05-09 Candidate #1 remains the TAIL lineage and deepest rollback baseline (BR100 original split: train +$39.11 / DD 37.09%, test +$419.70 / DD 13.20%, n=242+243).

> [!warning] Pre-parity-fix baselines are tainted
> Earlier 4-strategy YHIGH-inclusive numbers (e.g. "+$10,877 train @ $10k BR") and the +$124.52 train / +$516.55 test pre-parity numbers were measured before the bracket-parser parity fix. Three-way diagnostic showed bracket parser drove 98% of the BR$100 train PnL shift; sigma floor 2%. Pre-fix figures are inflated and should not be used as regression anchors. The 4-strategy spec needs re-verification on the corrected harness before being trusted again - see [[Optimum Strategy]].

> [!note] 24h refetch (Phase A')
> Original fetch used `limit=50` without pagination, yielding ~8h of late-trading snapshots per market. Refetch used `limit=200` with a 24h window, growing total price rows from 2.3M to 4.15M. The 24h data is what makes `entry_local_hour` analysis meaningful.

## Production config (`backtest/configs/candidate_l2_depth.json`)

`backtest/configs/candidate_l2_depth.json` (variant `sel_taila40_fp03_cs40`) is the active champion lineage since 2026-05-29, with NO 7% / TAIL 5% sizing and disabled NO/TAIL slip caps. `configs/candidate1.json` was removed; use git history if the retired non-L2 baseline is needed for comparison.

The deployed live constants live in `src/hightempbot/execution/strategy_constants.py::STRATEGY_CONFIGS`; the JSON exists for backtest reproduction only, and `tests/test_strategy_constants.py` fails CI if the two drift. The active config is 2-strategy (NO+TAIL); YMID and YHIGH are disabled. It contains:

- Per-strategy: signal, gates, fp band, edge floors, TP/SL, `size_frac`
- `max_l2_ask_premium=null` as of 2026-06-06; ask-premium sweeps remain supported for experiments, but live parity expects the gate disabled
- `live_execution_policy` NO/TAIL execution VWAP edge floors (0.05 / 0.07); first-fill VWAP top-up slip caps are disabled (`max_vwap_slip_from_anchor=null`)
- NO `size_frac=0.0700`; TAIL `entry_local_hours=[1]`, `alpha=4.0`, signal `fp_max=0.03`, delayed-entry trigger `delayed_entry_fp_max=0.02`, station-level `consensus_skip_threshold=0.40`, `size_frac=0.0500`
- Bracket-conditional NO ceiling refinement (`p_B_50`, `fp_min=0.50`, `max_edge=0.35`)
- Sizing is `size_frac * capital` with idempotent top-up; **no static stake cap** (the cap table is a diagnostic, not the strategy). Live `MAX_DD=0.40` halts new entries.
- Shared backtest runners (`measure_tp_sl.py`, `sweep_l2_depth_features.py`, `sweep_l2_sizing_walkforward.py`, `lut_range_chunks.py`) apply `TAIL.delayed_entry_fp_max` from the JSON. The dedicated delayed-entry WFO script disables that configured pass only to construct the immediate-entry baseline and threshold variants.

See [[Optimum Strategy]] for the full active L2 champion spec.

## Related

- [[Decision Table]] — column schema and how each col is derived
- [[Signal Flavors]] — the 9 p_model definitions
- [[Optimum Strategy]] — active L2 champion spec; robust-NO and 4-strategy variants are historical reference
- [[Walk-Forward Calibration Backtest]] — the upstream BSS validator (validation/ removed; concept retained)
- [[EMOS Calibration]], [[Walk-forward LUT]] — the live calibration whose history is replayed here
