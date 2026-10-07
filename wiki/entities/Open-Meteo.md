---
title: Open-Meteo
type: entity
entity_type: data_provider
created: 2026-05-03
updated: 2026-05-03
tags: [entity, weather, forecast, ensemble]
status: stable
---

# Open-Meteo

Free weather forecast API. Used by [[HighTempBot Project|HighTempBot]] as the **multi-model ensemble forecast source** for daily-tmax predictions.

## Integration

- Module: `src/hightempbot/ingestion/openmeteo_forecast.py`.
- Pulls a fixed `EXPECTED_MODELS` set (9 members in current production).
- Strict membership requirement — see Ensemble Lock. Partial ensembles abort the station tick.
- The latest complete UTC N ensemble is consumed at 00Z on N+1 to bet the N+1 market date — see Target-date Budget.

## See also

[[HighTempBot Project]] · Ensemble Lock · [[EMOS Calibration]] · [[Weather Underground]]
