---
title: Weather Underground
type: entity
entity_type: data_provider
aliases: [WU]
created: 2026-05-03
updated: 2026-05-03
tags: [entity, weather, actuals]
status: stable
---

# Weather Underground

Source of **station actuals** (observed daily-high temperatures) used to resolve markets and to retrain the per-station [[EMOS Calibration|EMOS]] / [[Walk-forward LUT]].

## Integration constraints

- **Sole accepted actuals source.** Never fall back to `polyhightemp` or any non-WU stream for `true_prob` / actuals.
- **URL format:** must be `ICAO:9:COUNTRY` — verify the country prefix when enrolling new stations.
- **Concurrency envelope:** **10 workers × 0.5s** is the proven safe rate. Don't push beyond this without empirical justification.

## See also

[[HighTempBot Project]] · [[Open-Meteo]] · [[EMOS Calibration]]
