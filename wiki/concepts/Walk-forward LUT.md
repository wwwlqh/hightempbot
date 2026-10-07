---
title: Walk-forward LUT
type: concept
aliases: [LUT, Walk-forward Bucket Lookup Table]
created: 2026-05-03
updated: 2026-05-03
tags: [concept, calibration, backtest, accuracy]
status: stable
---

# Walk-forward LUT

Walk-forward **bucket lookup table** mapping `(forecast_bucket, horizon) → empirical hit rate`. Sits downstream of [[EMOS Calibration]] in [[HighTempBot Project|HighTempBot]] and produces the `prob_safe_floor` used by the decision gate.

## Mechanics

- Module: [lut.py](../../src/hightempbot/calibration/lut.py). Live consumption: see [[Signal Flavors (live)]].
- Buckets historical `(forecast, observed)` pairs into brackets per market and per horizon.
- Walk-forward: at each retrain step the LUT is recomputed using only data available at that point in time (no leakage).
- **Open-ended brackets supported** — the lowest and highest brackets may have `None` for low/high. `_brackets_for_station` keeps them; never filter them out.
- **Polymarket bracket format:** 11 brackets per market, 2°F (US) or 1°C (non-US), shifts daily. The LUT must align to the market's bracket scheme.

## 8-Bucket Layout (Locked)

```python
BUCKETS = (
    (0.00, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.15),
    (0.15, 0.25), (0.25, 0.40), (0.40, 0.60), (0.60, 1.00),
)
```

Locked to notebook Cell F — **do not change**. The final bucket is **right-inclusive** (p=1.0 maps to `(0.60, 1.00]`).

## Key Functions

- `bucket_of(p)` ([lut.py:164-181](../../src/hightempbot/calibration/lut.py#L164-L181)) — maps probability to its bucket. Raises `ValueError` if `p` out of `[0, 1]`.
- `lookup_with_cumulative(...)` (lut.py) — the live read path. (The old `lookup`/`BucketStats` pair was removed 2026-08-09; consumers read `lut_bucket_stats` via SQL or the cumulative helper.)
- `lookup_with_cumulative(conn, station, bucket, asof_local_date)` ([lut.py:117-161](../../src/hightempbot/calibration/lut.py#L117-L161)) — strict `local_date < asof` walk-forward `(n_cum, hits_cum, mean_pred)`. Used by [[Signal Flavors (live)|`_compute_signal_flavors`]].
- `append_triples_for_date(...)` ([lut.py:472-578](../../src/hightempbot/calibration/lut.py#L472-L578)) — appends triples for one resolved day. Walk-forward EMOS via `_walk_forward_params`; dedupes ensemble members by latest `ingested_at`.
- `rebuild_lut(conn, station_id)` ([lut.py:218-290](../../src/hightempbot/calibration/lut.py#L218-L290)) — re-aggregates all triples into bucket stats. Cheap, idempotent.
- `seed_lut_from_history(conn, station_id)` ([lut.py:581-661](../../src/hightempbot/calibration/lut.py#L581-L661)) — cold-start: fits EMOS per-day for all historical actuals. Expensive but memoized via `calibration_params_history` (Calibration Store).
- `stamp_refreshed(conn, station_id)` ([lut.py:293-305](../../src/hightempbot/calibration/lut.py#L293-L305)) — touches `refreshed_at` without new data (prevents false stale alerts on idle stations).
- `_supports_station_lut(conn, station_id)` ([lut.py:34-48](../../src/hightempbot/calibration/lut.py#L34-L48)) — gate so HKO-resolved stations don't accumulate live LUT state. `clear_station_lut` zeroes both `pred_bucket_history` and `lut_bucket_stats` for unsupported sources.

## Walk-forward Memoization

Per-day EMOS params are cached in `calibration_params_history` (via `store.save_emos_at`). On a re-seed, the LUT fits EMOS on demand for each historical day (30-day rolling window ending `day − 1`) and persists to cache — subsequent seeds skip refitting.

## Staleness

LUT is considered stale if `refreshed_at > LUT_STALE_HOURS (36h)` ago. Stale LUT causes the betting gate to skip. `stamp_refreshed` decouples "data freshness" from "table-update freshness" so idle stations don't trigger stale alerts.

## Why a LUT and not a parametric model

Once EMOS gives a calibrated Gaussian, the residual question is *how often does the model's claimed probability actually correspond to the observed hit rate?* The LUT answers that empirically. The decision gate uses `prob_safe_floor = observed_rate` (raw bucket hit rate) to compute fee-adjusted edge.

## Authority

The LUT **owns accuracy filtering**. Because the LUT enforces the entry edge, the order layer no longer needs a `MAX_FILL` price cap; the edge-preserving walker walks asks unbounded (the realized-VWAP edge floor in `walk_book_edge_preserving` is the only stop). See YES+NO Trading.

> [!note] Bucket grid alignment (Phase A, 2026-05-05)
> A prior contradiction between live `BUCKETS` and backtest `sweep_lib.py::LUT_BUCKETS` was resolved by copying the live grid into the backtest harness. Backtest and live now use identical boundaries. See `hot.md` and [[Signal Flavors]] for the resolution.

## See also

[[EMOS Calibration]] · [[Edge-Preserving Sizing]] · YES+NO Trading · [[Polymarket]] · [[Backtest Harness]] · [[Signal Flavors]] · [[2026-04-27 EMOS-Only Reverted]]
