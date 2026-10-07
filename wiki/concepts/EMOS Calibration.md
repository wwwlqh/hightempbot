---
title: EMOS Calibration
type: concept
aliases: [EMOS, Ensemble Model Output Statistics]
created: 2026-05-03
updated: 2026-05-03
tags: [concept, calibration, statistics, forecasting]
status: stable
---

# EMOS Calibration

**Ensemble Model Output Statistics** — fits a Gaussian over the predictand (here, `tmax`) by regressing on raw ensemble members. Used in [[HighTempBot Project|HighTempBot]] to convert raw [[Open-Meteo]] ensemble forecasts into a calibrated probability distribution.

## Parameters (`EMOSParams`)

```python
@dataclass
class EMOSParams:
    a: float       # Mean intercept
    b: float       # Mean slope (on ensemble mean)
    c: float       # Log-space variance intercept  ← exp() enforces σ² > 0
    d: float       # Log-space variance slope
    n_samples: int # Training pairs used
```

- **Mean**: `μ = a + b × ensemble_mean`
- **Variance**: `σ² = exp(c) + exp(d) × ensemble_var`
- **σ floor**: `_SIGMA_FLOOR = 0.1°C` — prevents numerical underflow
- **`ddof=0`** for ensemble variance (population variance — justified by Gneiting 2005: members represent the full forecast distribution, not a sample from an unknown population)

## Fitting (`fit_emos`)

- Minimizes mean **CRPS** (Continuous Ranked Probability Score) via L-BFGS-B optimizer (max 500 iterations).
- Bounds: `a, b` unconstrained; `c, d` in `[−10, 10]` (prevents `exp(10) ≈ 22000` overflow).
- Initial guess: `[a=0, b=1, c=0, d=0]` (identity + moderate spread).
- Returns `None` if < 10 samples (insufficient data).
- Non-convergence: returns partial result (usually usable), logs error.
- Sigma stuck at floor: logs warning.

## Readiness Threshold

`CalibrationModel.is_ready()` requires `n_samples ≥ 30` (stricter than LUT's 20) — conservative gate for live betting decisions.

## In HighTempBot

- Module: [emos.py](../../src/hightempbot/calibration/emos.py) + [model.py](../../src/hightempbot/calibration/model.py). See Forecast Model for the `CalibrationModel` wrapper and Calibration Store for persistence.
- `fit_emos` at [emos.py:44-86](../../src/hightempbot/calibration/emos.py#L44-L86); `predict_emos` at [emos.py:89-111](../../src/hightempbot/calibration/emos.py#L89-L111); `emos_probability` at [emos.py:114-127](../../src/hightempbot/calibration/emos.py#L114-L127).
- **Per-station fit.** Each station has its own EMOS coefficients.
- **Output:** a Gaussian over `tmax` per (station, horizon).
- **Strict input requirement.** Fits assume the exact `EXPECTED_MODELS` membership; partial ensembles silently corrupt the fit. See Ensemble Lock.

## Pipeline position

```
Open-Meteo ensemble  →  EMOS (Gaussian)  →  Walk-forward LUT (empirical hit-rate)  →  LCB / UCB
```

Downstream, the [[Walk-forward LUT]] buckets historical (forecast, observed) pairs to produce empirical hit rates per bracket and horizon — the EMOS output is the **forecast** input there.

## Retraining

- Per-station, on resolution events, NOT on a global UTC calendar gate.
- Module: [monthly_retrain.py](../../src/hightempbot/calibration/monthly_retrain.py). See Monthly Retrain.

## See also

Forecast Model · Calibration Store · [[Walk-forward LUT]] · Ensemble Lock · [[Open-Meteo]] · [[Edge-Preserving Sizing]] · [[2026-04-27 EMOS-Only Reverted]]
