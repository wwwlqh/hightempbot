# Backtest Harness

L2-focused HighTempBot strategy backtest harness. It lives outside the live bot
and is never imported by `hightempbot.*`.

The current champion is `configs/candidate_l2_depth.json`, selected with real
PMD L2 book ladders where available and leakage-safe expanding ABCD validation.

## Quick Start

```bash
# 1. Refresh PMD market price/metrics history when needed.
python backtest/scripts/fetch_polymarket_history.py --resume

# 2. Refresh PMD L2 order books.
python backtest/scripts/fetch_polymarket_books.py --start 2026-02-27 --end 2026-05-21 --missing-only

# 3. Rebuild the L2 decision table.
python backtest/scripts/build_l2_decision_table.py

# 4. Run the L2 feature sweep and strict ABCD selector.
python backtest/scripts/sweep_l2_depth_features.py
python backtest/scripts/walkforward_l2_depth_features.py
```

To sanity-check alternate configs without editing a file, set
`HTB_TP_SL_CONFIG=/path/to/spec.json`.

## Core Files

| File | Purpose |
|---|---|
| `configs/candidate_l2_depth.json` | Current L2 champion settings for forward use. |
| `lib/sweep_lib.py` | Shared decision-table paths, signal flavors, leakage helpers, and PMD DB path. |
| `lib/live_match_eval.py` | Candidate generator and execution model, including real L2 ask-ladder walking. |
| `scripts/fetch_polymarket_history.py` | PMD price/metrics downloader and shared PMD API client/rate limiter. |
| `scripts/fetch_polymarket_books.py` | PMD `/books` downloader into `data/polymarket_books_10m.db`. |
| `scripts/build_decision_table.py` | Builds leakage-safe non-L2 base tables when a fresh base is needed. |
| `scripts/build_l2_decision_table.py` | Joins PMD book snapshots at-or-before each entry timestamp. |
| `scripts/sweep_l2_depth_features.py` | Current L2 strategy feature sweep. |
| `scripts/walkforward_l2_depth_features.py` | Expanding ABCD selector: train A -> B, train A+B -> C, train A+B+C -> D. |
| `scripts/measure_tp_sl.py` | Shared simulator/config helpers used by the L2 sweep. |
| `scripts/lut_range_chunks.py` | Shared ABCD chunk and TP/SL helpers used by the L2 sweep. |

## Data Files

| File | Purpose |
|---|---|
| `data/polymarket_history.db` | PMD price and market metadata source. |
| `data/polymarket_books_10m.db` | PMD L2 book snapshots. Large and expensive to rebuild. |
| `data/decision_table_may11plus.parquet` | Base May11+ leakage-safe decision table. |
| `data/decision_table_may11plus_l2.parquet` | Current L2 decision table used by the sweep. |
| `data/manifest.csv` | PMD market manifest from the historical pull. |

The old default `data/decision_table.parquet` was intentionally removed during
the L2-only cleanup.

The old non-L2 baseline `configs/candidate1.json` was removed after the L2
champion became the only active config path; use git history if that retired
baseline is needed for comparison.

## L2 Champion Config

Champion config: `configs/candidate_l2_depth.json`.

Variant: `sel_taila40_fp03_cs40`.

> The evidence sections below are historical backtest results. They were kept on
> purpose. Live trading did not reproduce them (the backtests overfit; see
> `results/research_2026_07/README.md` for why), but they document how the
> strategy was selected.

Settings:

| Setting | Value |
|---|---|
| L2 ask premium guard | `side best ask - displayed entry price <= 0.10` |
| NO execution VWAP edge floor | `0.05` |
| TAIL execution VWAP edge floor | `0.07` |
| TAIL entry local hour | `[1]` |
| TAIL alpha | `4.0` |
| TAIL `fp_max` | `0.03` |
| TAIL delayed entry trigger | wait for YES ask `<= 0.02` after signal conditions pass |
| TAIL consensus skip | station-level `max YES ask < 0.40`; NO has no consensus skip |
| YMID/YHIGH | disabled |

Sizing/execution policy:

| Setting | Value |
|---|---|
| Static stake cap | none; caps are diagnostics, not the strategy |
| Slot target | `strategy.size_frac * live capital` |
| Idempotency | subtract already-filled slot exposure before each top-up |
| First fill | first scheduler tick of an allowed local entry hour only |
| Top-up cadence | every 10 minutes after first fill while remaining is not dust |
| NO dynamic guards | realized VWAP edge `>= 0.05`; top-ups recheck current entry gates |
| TAIL dynamic guards | realized VWAP edge `>= 0.07`; top-ups recheck current entry gates |

## TAIL Delayed Entry Evidence

Evidence is in `results/tail_delayed_entry_wfo.md`.

Protocol: normal TAIL signal conditions pass first (`yes_ask <= 0.03`,
4-of-4 vote, volume, consensus), then TAIL waits for the first later YES price
snapshot `<= 0.02` before placing. The delayed entry must still occur at least
4h before bracket close. NO is unchanged.

The selected `wait_le_02c` row was the B-D continuous OOS winner:

| Metric | Value |
|---|---:|
| B-D continuous OOS PnL | +$2,537.24 |
| Max DD | 26.18% |
| Worst chunk | +$319.94 |
| Total bets | 308 |
| NO PnL | +$1,211.43 |
| TAIL PnL | +$1,325.80 |
| TAIL entered / missed | 55 / 1 |
| Avg TAIL delay | 118.0m |

`scripts/measure_tp_sl.py`, `scripts/sweep_l2_depth_features.py`,
`scripts/sweep_l2_sizing_walkforward.py`, and `scripts/lut_range_chunks.py`
apply `TAIL.delayed_entry_fp_max` from `configs/candidate_l2_depth.json`.
The dedicated `scripts/sweep_tail_delayed_entry_wfo.py` intentionally builds
an immediate-entry baseline first, then sweeps delayed-entry thresholds.

## Strict ABCD Evidence

Leakage-safe evidence is in `results/l2_depth_walkforward_abcd.md`.

Protocol:

```text
train A     -> select variant -> test B
train A+B   -> select variant -> test C
train A+B+C -> select variant -> test D
```

Recommended selector profile: `l2_tail_book_no_no_retune`, risk-adjusted.
It excludes NO-side retunes because the unrestricted expanded grid overfit
chunk A and then failed chunk B.

| Metric | Value |
|---|---:|
| B-D out-of-sample PnL | +$251.96 |
| Worst OOS chunk | +$52.55 |
| Positive OOS chunks | 3/3 |
| Max test DD | 18.22% |
| OOS bets | 314 |
| OOS NO PnL | +$143.02 |
| OOS TAIL PnL | +$108.94 |

## L2 Sizing Evidence

The full strategy source is `configs/candidate_l2_depth.json`. The sizing,
refill/reversion, and top-up-capacity diagnostic scripts and their result files
were removed in the L2-champion cleanup; the deployment sizing policy they
informed is retained below.

Important distinction:

- `volume24hr` is an activity gate, not fillable depth.
- L2 ask ladders are the fillable depth evidence.
- PMD 10-minute snapshots do not include the market impact of our hypothetical
  order.
- Idempotency increases fill chances, but it does not create safe liquidity.

Current deployment view: **no static stake cap**. The target is
`size_frac * capital`; top-ups keep trying every 10 minutes only while dynamic
fill quality remains good.

The 2026-06-04 equity-cap sizing refresh uses current equity, not initial
bankroll, as the exposure denominator, and reports out-of-sample sizing as one
continuous bankroll path from chunk B through D. Chunk A remains training-only;
B-D does not reset to `$100` between chunks. With Asia19 excluded and
`maxDD <= 30%`, the fixed continuous B-D PnL winner was
**NO 8% / TAIL 6% / 100% cap**: +$1,496.77, final bankroll $1,596.77,
29.93% max DD, 90.0% max actual open exposure, and 97.7% max target cap used.
Live deployment is still below the 30% DD training constraint while adding NO
exposure per the 2026-06-05 operator override: **NO 7% / TAIL 5% / 100% cap**
has +$1,345.48 continuous B-D PnL, final bankroll $1,445.48, 26.25% max DD,
91.1% max actual open exposure, 96.5% max target cap used, zero days above
100%, and 3/3 positive B-D segments.

Dynamic execution gates:

| Strategy | Target | Realized edge | VWAP slip from anchor | Notes |
|---|---:|---:|---:|---|
| NO | `7% * capital` | `>= 0.05` | off | operator sizing override; can scale through idempotency when current gates and edge floor hold |
| TAIL | `5% * capital` | `>= 0.07` | off | waits for YES ask `<= 0.02` after the 3c signal band passes; top-ups remain edge-floor bounded |

The retained sizing result files are diagnostics only. They answer "where does
depth start to degrade?" but do not define live sizing. Large top-ups still
recheck the current strategy gates and realized VWAP edge floor; the old
first-fill VWAP slip leash was removed for NO/TAIL on 2026-05-31.

## Dataset Validation

The `validate_l2_dataset.py` checker and its
`results/l2_dataset_validation.{json,md}` output were removed in the
L2-champion cleanup; the figures it reported are retained below for reference.

On 2026-05-28:

| Check | Value |
|---|---:|
| L2 table rows | 29,190 |
| Entry timestamps checked | 698,518 |
| YES L2 match coverage | 60.75% |
| NO L2 match coverage | 60.75% |
| Future book timestamp joins | 0 |
| Joins over 90 minutes | 0 |
| Invalid ladder cells | 0 |

PMD currently allows only the rolling last 90 days of historical books on this
plan. Feb18-Feb26 books were no longer downloadable, and several later PMD slugs
returned persistent empty/404 responses. Non-null L2 ladders are real PMD book
JSON selected at or before the strategy entry timestamp; missing L2 rows remain
null and the simulator falls back to the synthetic liquidity model only at
execution time.

## Generated Results To Keep

| File | Purpose |
|---|---|
| `results/l2_depth_feature_sweep.{csv,md}` | Full L2 sweep output. |
| `results/l2_depth_walkforward_abcd.{csv,md}` | Strict ABCD selector evidence. |
| `results/tail_delayed_entry_wfo.{csv,md}` | TAIL delayed-entry walk-forward (+$2,537 B-D). |
| `results/l2_sizing_{sweep,walkforward}_ex_asia19_equitycap_grid.*` | Equity-cap sizing grid (NO 8% / TAIL 6% and NO 7% / TAIL 5% rows). |
| `results/bets/*.parquet` | Per-bet records for the champion and candidate runs. |

## Cleanup Review

Safe generated files that can be deleted anytime:

- `backtest/lib/__pycache__/`
- `backtest/scripts/__pycache__/`
- `backtest/data/*.db-shm`
- `backtest/data/*.db-wal`

Obsolete fixed-cap summary artifacts intentionally not kept:

- `backtest/results/l2_sizing_recommendation.md`
- `backtest/results/l2_market_capacity.{csv,md}`

Keep the remaining L2 scripts/results. They are the current champion strategy
and its strict ABCD evidence.

Historical non-L2 results and retired exploratory scripts were removed during
the L2-only cleanup. The sizing, refill/reversion, top-up-capacity, and
dataset-validation diagnostic scripts (and their result files) were removed in a
later L2-champion cleanup, narrowing the harness to the champion build/run chain.
