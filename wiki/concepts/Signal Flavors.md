---
title: Signal Flavors
type: concept
aliases: [Signal Flavors (live), _compute_signal_flavors, p_E, p_B_50, p_Shrink_n50, tail_vote_avg]
created: 2026-05-05
updated: 2026-06-09
tags: [concept, signals, lut, emos, shrinkage, backtest, runtime]
status: stable
---

# Signal Flavors

The 9 probability variants computed per `(station, target_date, bracket)` row. Each strategy selects one via `signal:` in its config. Live and backtest implementations must remain in parity; drift invalidates sweep results.

For terms (`p_E`, `p_L_loose`, etc.) see [[glossary#signals-the-9-flavors]]. For where each strategy consumes them, see the [Per-strategy consumption](#per-strategy-consumption-current-live) table below.

## Inputs

- `E = p_emos` - raw EMOS Gaussian `P(temp in bracket)` for this row.
- `n = n_cum` - walk-forward count of past predictions in this `(station, pred_bucket_low)`.
- `hits = hits_cum` - walk-forward count of those predictions that hit.
- `L_obs = hits / max(n, 1)`; NaN when `n == 0`.
- `confident = (n >= LUT_MIN_N_FOR_SHRINKAGE)`; currently `30`.

`n_cum` and `hits_cum` come from [[Walk-forward LUT|`lookup_with_cumulative`]] with strict `local_date < target_date`; no same-day leakage.

## The 9 Flavors

| Key | Formula | Notes |
|---|---|---|
| `p_E` | `E` | Pure EMOS Gaussian |
| `p_L_strict` | `L_obs if confident else NaN` | Strict LUT, skip-bet on cold start |
| `p_L_loose` | `L_obs if confident else E` | LUT with EMOS fallback |
| `p_B_50` | `0.5*E + 0.5*p_L_loose` | Equal blend |
| `p_B_30` | `0.3*E + 0.7*p_L_loose` | LUT-heavy blend |
| `p_B_70` | `0.7*E + 0.3*p_L_loose` | EMOS-heavy blend |
| `p_Shrink_n10` | `(hits + 10*E) / (n + 10)` | Bayesian shrinkage, EMOS prior weight 10 |
| `p_Shrink_n50` | `(hits + 50*E) / (n + 50)` | Bayesian shrinkage, EMOS prior weight 50 |
| `p_Ramp` | `lambda*L_safe + (1-lambda)*E`, `lambda = min(n/50, 1)` | Linear ramp; `L_safe = E if L_obs is NaN else L_obs` |

## NaN-Safe Semantics

- `L_obs` is NaN when `n = 0`, which propagates through `p_L_strict` below `lut_min_n`.
- `p_Shrink_*` is always finite because the prior weight prevents a zero denominator.
- `p_B_*` blends use `p_L_loose`, which falls back to `E` on cold start.
- `p_Ramp` uses `L_safe` to avoid NaN propagation.

## Per-Strategy Consumption Current Live

| Strategy | Signal | Edge gate |
|---|---|---|
| **NO** | `p_E` (strict) -> `p_B_50` (ceiling extension) | additive `(1-p) - np - fee`; strict `fp >= 0.75, edge <= 0.15`; ceiling `fp >= 0.50, edge <= 0.35` |
| **TAIL** | `tail_vote_avg` (4-of-4 from `p_E, p_B_50, p_L_loose, p_Shrink_n10`) | each voter `p_i >= 4.0 * yp`; signal fp `0.001..0.03`; delayed entry waits for `yes_ask <= 0.02`; consensus skip 0.40 |
| **YMID** | `p_Shrink_n50` | ratio: `p >= 1.3 * yp` AND additive <= 0.30 *(currently disabled)* |
| **YHIGH** | `p_B_50` (ceiling brackets only) | additive `[0.025, 0.30]` *(currently disabled)* |

See [[Optimum Strategy]] for the deployed config. YMID and YHIGH are listed for completeness; they are disabled in the active spec.

## Cold-Start Guard

When `n_cum < LUT_MIN_N_FOR_SHRINKAGE` (30), every strategy emits a SKIP `BetSignal` with `gate_results["lut_min_n"] = False` rather than silently returning None, so the dashboard records the rejection.

## Discrete Bucket Low Coupling

All LUT-derived signals depend on `pred_bucket_low`, the 8-bucket of `p_raw`:

```text
(0.00, 0.02) (0.02, 0.05) (0.05, 0.10) (0.10, 0.15)
(0.15, 0.25) (0.25, 0.40) (0.40, 0.60) (0.60, 1.00)
```

Two predictions with `p_raw = 0.61` and `p_raw = 0.99` share the same LUT bucket and therefore the same `n_cum, hits_cum`. The wide top bucket flattens distinctions in confident regimes.

## Why Each Flavor Exists

- `p_L_strict` vs `p_L_loose` - tests whether skipping low-coverage rows beats EMOS fallback.
- `p_B_*` blends - cheap mixtures without changing threshold logic.
- `p_Shrink_*` - Bayesian shrinkage that pulls `L_obs` toward EMOS when `n` is small.
- `p_Ramp` - similar trajectory but linear; hits pure LUT at `n = 50`.

## Where

- Live: `src/hightempbot/decision/strategies.py::_compute_signal_flavors`.
- Backtest: `backtest/lib/sweep_lib.py::add_signal_flavors`.

## Related

- [[glossary#signals-the-9-flavors]]
- [[Decision Table]]
- [[EMOS Calibration]]
- [[Walk-forward LUT]]
- [[Optimum Strategy]]
- [[Backtest Harness]]
