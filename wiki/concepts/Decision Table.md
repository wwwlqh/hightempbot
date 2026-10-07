---
title: Decision Table
type: concept
created: 2026-05-05
updated: 2026-05-19
tags: [concept, backtest, parquet, walk-forward]
status: stable
---

# Decision Table

`backtest/data/decision_table.parquet` — the evidence base for the [[Backtest Harness]]. Built once by `backtest/scripts/build_decision_table.py` (~20s, mtime-cached), reread by every downstream script. (Paths updated 2026-05-19 reorg.)

## Identity

| col | type | source |
|---|---|---|
| `market_slug` | str | `markets.market_slug` |
| `station_id` | str | ICAO |
| `market_date` | str (ISO date) | bracket resolution date |
| `bracket_index` | int | 0..10 |
| `bracket_label` | str | e.g. `"between 75-77°F"`, `"32°F or below"`, `"94°F or higher"` |
| `bracket_kind` | str | `"low"` (X or below), `"mid"` (interior), `"high"` (X or higher) |
| `lo_c`, `hi_c` | float | bracket bounds in Celsius (±inf for tails) |
| `yes_token_id`, `no_token_id` | str | CLOB token IDs |

## Pricing (entry-time)

| col | desc |
|---|---|
| `yes_price`, `no_price` | latest Polymarket snapshot ≥ 4h before close UTC |
| `entry_ts_unix`, `close_ts_unix` | snapshot ts + bracket close (`market_date 00:00:00 UTC + 86400`) |
| `avg_volume`, `avg_liquidity`, `avg_spread` | lifetime averages from `metrics` (kept for back-compat) |
| `entry_volume`, `entry_liquidity`, `entry_spread` | **per-snapshot at entry-time** — what the bot would actually see |
| `entry_metrics_ts` | timestamp of the metrics snapshot used |

The `entry_*` metrics are critical for walk-book realism. Lifetime averages over-state liquidity (markets thicken near close) and produce optimistic VWAP estimates.

## Per-local-hour entry prices (time-of-day study)

For each `H ∈ (0, 6, 8, 10, 12, 14, 16, 18, 20)`:

- `yes_price_h{H}`, `no_price_h{H}` — latest snapshot whose ts ≤ `market_date H:00 local`
- `entry_ts_h{H}` — its unix timestamp

Used by `eval_strategy.py`'s `entry_local_hour` knob. Limitation: the original PMD fetch used `limit=50` (not paginated), so most markets only carry the last ~8h of snapshots before close, and most of these 9 cutoffs collapse to the same row. Re-fetch with `limit=200` + `next_cursor` pagination unlocks earlier hours.

## Walk-forward calibration (as-of joins)

These columns prove walk-forward integrity:

| col | semantics |
|---|---|
| `a`, `b`, `c`, `d` | EMOS params from `calibration_params_history`, joined `asof_date ≤ market_date` |
| `asof_date` | which `calibration_params_history` row was chosen |
| `n_cum`, `hits_cum` | cumulative LUT bucket counters from `pred_bucket_history`, joined `local_date < market_date` strictly |
| `local_date` | latest LUT row's date (NaT = cold-start, no LUT yet) |
| `pred_bucket_low` | which of 8 LUT buckets `p_raw` falls into |

The two joins use `pd.merge_asof`:

- EMOS: `direction='backward', allow_exact_matches=True` → `asof ≤ md`
- LUT: `direction='backward', allow_exact_matches=False` → `local < md` strict

Cold-start rows (LUT never bucketed for this `(station, pred_bucket_low)` before `market_date`) are allowed; their `n_cum` and `hits_cum` fall back to 0 and `p_L_strict` becomes NaN.

## Predicted probability (raw EMOS)

| col | formula |
|---|---|
| `p_raw` | `P(temp ∈ [lo_c, hi_c)) = Φ((hi_c−μ)/σ) − Φ((lo_c−μ)/σ)` |

`μ = a + b·ē`, `σ² = max(exp(c) + exp(d)·var(e), 0.5²)` — see [[EMOS Calibration]]. Tail brackets use `±∞` for the open side.

## Outcome

| col | desc |
|---|---|
| `actual_high_c` | resolved high temperature (°C, from WU `actuals`) |
| `won_yes` | `1` if `lo_c ≤ actual_high_c < hi_c`, else `0` |

The right-open interval matches Polymarket's bracket semantics — exactly one of the 11 brackets resolves YES.

## Signal flavors

9 `p_model` columns added by `add_signal_flavors`:

`p_E`, `p_L_strict`, `p_L_loose`, `p_B_50`, `p_B_30`, `p_B_70`, `p_Shrink_n10`, `p_Shrink_n50`, `p_Ramp`.

Each is a different way of mixing raw EMOS (`p_raw`) with empirical LUT hit rate (`hits_cum/n_cum`). See [[Signal Flavors]] for the formulas and what each is testing.

## Build invariants

`backtest/scripts/build_decision_table.py` filters out:

- Rows outside `[2026-02-04, 2026-05-04]`
- `(station, market_date)` with no ensemble or no actual
- Markets with no entry price 4h before close
- Bracket labels that fail to parse
- Cold-start stations with no `calibration_params_history` rows yet (`a is NaN`)

Then the build calls `assert_no_leakage` which raises if any row has:

1. `asof_date > market_date`
2. `local_date ≥ market_date` (and `local_date` is not NaT)
3. `entry_ts_unix + 4h > close_ts_unix`

The parquet only writes after the asserts pass.

## Mild biases acknowledged (not blocking)

- `forecast_archive` has no `ingested_at` column. We use whatever the bot last UPSERTed for that `target_date`. Since horizon=1 forecasts are made BEFORE `target_date`, this is safe — we just may use a slightly later run of the day's forecast than we would have at hypothetical bet time.

## Related

- [[Backtest Harness]] — top-level entry point
- [[Signal Flavors]] — column-by-column derivation of the 9 p_model variants
- [[EMOS Calibration]] — where `a, b, c, d` come from
- [[Walk-forward LUT]] — where `n_cum, hits_cum` come from
