---
title: Walk-Forward Calibration Backtest
type: concept
created: 2026-05-05
updated: 2026-05-05
tags: [concept, validation, calibration, bss, walk-forward, emos]
status: stable
---

# Walk-Forward Calibration Backtest

The `validation/` folder evaluates how well [[EMOS Calibration]] predicts `P(tmax > threshold)` — the core skill needed to bet profitably. Unlike the [[Backtest Harness]] (which scores P&L), this scores **forecasting skill** via Brier Skill Score (BSS).

> [!key-insight] Why BSS, not P&L
> P&L conflates skill, sizing, and execution. BSS isolates calibration: did the model's claimed probabilities actually match the empirical hit rate? A station with BSS < 0 is structurally unprofitable regardless of strategy; a station with BSS > 0.9 may be profitable depending on market mispricing. The validator answers the upstream question.

## Files

| File | Purpose |
|---|---|
| `precompute.py` | Walk-forward EMOS over all stations + retraining cadence; saves `bracket_data.pkl` |
| `walkforward_backtest.ipynb` | Interactive notebook: BSS curves, station qualification, monthly seasonal skill |
| `bracket_data.pkl` | Output of precompute (~loads in <1s vs ~minutes raw) |

## Methodology

For each `(station, date)`:

1. **Walk-forward train**: take all dates **before** `date`, fit EMOS on those (real 9-member ensemble, real actuals).
2. **Predict on `date`**: use the day's ensemble + fitted EMOS to compute `P(tmax > threshold)` for each `threshold ∈ np.arange(min_actual, max_actual+1, 0.5)`.
3. **Score**: pair `(p, y)` where `y = 1[actual > threshold]`. Brier score `BS = mean((p − y)²)`. BSS `= 1 − BS_model / BS_clim` where `BS_clim` uses `clim = mean(y)`.
4. **Retrain cadence**: `RETRAIN_EVERY = 30` days. Mirrors the live Monthly Retrain cadence.
5. **Min train days**: `MIN_TRAIN_DAYS = 365` — need a full season before the first prediction to capture all weather regimes.

## Locked ensemble — the BoM constraint

```python
MIN_MEMBERS = REQUIRED_MEMBERS  # = 9
```

[[Open-Meteo]] previously offered a 10-member ensemble including BoM (Australian Bureau of Meteorology). BoM has been **offline since 2025-07**, so the live bot's Ensemble Lock is set to the post-BoM 9-model set.

The validator must use the **same** locked set. Two reasons:

1. **EMOS spread coefficient**: `σ² = exp(c) + exp(d)·var(ensemble)`. The `d` coefficient is calibrated against the *exact* sample-variance distribution of the ensemble. A 10-member sample variance and 9-member sample variance are different statistics; mixing them breaks calibration.
2. **Live/test parity**: any BSS finding is meaningless if the validator's ensemble doesn't match what the live bot will see.

`precompute.py` filters to dates with **exactly** `MIN_MEMBERS` forecasts (`len(date_members[d]) == MIN_MEMBERS`). Partial-ensemble dates are dropped, mirroring the live tick's "abort on incomplete ensemble" behavior.

## Date restriction — 9-member era only

The notebook caps the evaluation window at **2024-03-01+**. Pre-2024-03 data is the 3-model era; mixing regimes contaminates the EMOS spread fit (see above). This restriction is non-negotiable — moving the start date earlier silently breaks calibration.

## BSS table (per-station, per-month)

`compute_bss_table(results, as_of_date)` produces `{icao: {month_int: bss_float}}` using only predictions before `as_of_date`. Three reasons it's per-month:

1. **Seasonal skill variation**: a model that predicts well in winter may collapse in summer convective regimes. Annual averages hide this.
2. **Live `bss.py` parity**: production `bss.py` does monthly_recalc; the validator matches.
3. **Qualification gate**: stations are qualified by `MIN(bss across all months) ≥ threshold`, not the current month — see `feedback_qualified_min_bss` in MEMORY.md. A station that clears summer but fails winter is rejected.

The table is recomputed before each `RETRAIN_EVERY = 30` day cutoff so the notebook can plot a rolling skill timeline per station.

## Output schema (`bracket_data.pkl`)

```python
{
  'bracket_eval': {
    icao: [
      {'date': '2025-04-15',
       'raw_probs': [...11 bracket probs (sum=1)...],
       'actual_idx': 5}  # which of 11 brackets actually resolved YES
      ...
    ]
  },
  'refresh_bss': {
    '2025-02-01': {icao: {1: 0.91, 2: 0.88, ...}},
    '2025-03-03': {icao: {1: 0.92, ...}},
    ...
  },
  'splits': [('2025-02-01', '2025-08-31'), ('2025-09-01', '2026-03-31')],
}
```

The `bracket_eval` half feeds downstream notebooks that score bracket-level P(win) calibration. `refresh_bss` feeds station qualification analysis.

## Bracket conversion

`bracket_probs_from_thresholds` converts the threshold-exceedance probabilities into 11 bracket probabilities matching Polymarket's bracket structure:

- `bracket_kind ∈ {floor, interior×9, ceiling}` (11 total)
- US stations (`K*`, `C*`, `MM*`, `MP*`) use °F, 2°F-wide interior brackets
- Non-US stations use °C, 1°C-wide interior brackets
- Brackets shift daily based on `round(ens_med)`

The `−0.5` shift on `lo` and `hi` accounts for `round()` semantics: `round(x) ∈ [b, b+1)` ↔ `x ∈ [b−0.5, b+0.5)`.

Probabilities are clipped to `≥ 0` and renormalized to sum to 1 (since the brackets are mutually exclusive and exhaustive).

## What is NOT validated here

- LUT calibration — that's intrinsically rolling; tested implicitly when the live bot's `pred_bucket_history` accumulates.
- Execution — the live-match evaluator owns this; see Live-Match Evaluator.
- Bracket parsing — exercised in `backtest/lib/sweep_lib.py::parse_bracket`.

## Related

- [[EMOS Calibration]] — the model under test
- Ensemble Lock — the locked ensemble both validator and live bot use
- Monthly Retrain — production retraining cadence (matched here)
- Backfill Pipeline — produces the data the validator reads
- [[Backtest Harness]] — downstream P&L exploration that depends on this validation
