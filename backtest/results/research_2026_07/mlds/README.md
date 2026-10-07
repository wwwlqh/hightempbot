# Neighbor-ML dataset

Inputs for `backtest/scripts/explore_ml_neighbors.py`. 46 stations
(`stations.csv`); all temperatures are °C. The `unit` column only says how
Polymarket labels the brackets (F for the 11 US stations).

| File | Rows | Grain |
|---|---:|---|
| `stations.csv` | 46 | station: id, city, lat, lon, tz, unit, resolution_source |
| `forecasts.parquet` | 71,144 | station × target_date × centre |
| `actuals.parquet` | 85,613 | station × local_date |
| `neighbors.parquet` | — | station × point_label × date |

**forecasts**: day-ahead only (`issue_date = target_date − 1`),
2026-02-01 → 2026-07-16, every station complete. Deduped to the latest
`ingested_at` per centre. Key on `centre`, not `member`: JMA was in the
ensemble until 2026-04-15, which shifted member indices after it.

**actuals**: WU daily highs, 2021-01-01 → 2026-07-16. Minor 2026 gaps
(DNMM 184, MPMG 190 of 197 days).

**neighbors**: ERA5 daily Tmax from the Open-Meteo Archive API at the
station (`TGT`) and 8 points ~100 km away (N…NW), 2026-01-01 → 2026-07-15.
Coordinates are grid-snapped. Use values only through D−1 when predicting
day D. Gaps are in `neighbor_gaps.csv`.

Join on station plus `target_date` = `local_date` = `date` (station local).
