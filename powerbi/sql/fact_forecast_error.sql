-- One row per station, day and weather model: the day-ahead forecast of the
-- daily high against the observed high. Ensemble members are averaged.
-- Observations that fail rule WX-01 (outside -60..60 °C) are left out so they
-- don't distort the error measures.

WITH model_forecast AS (
    SELECT
        station_id,
        target_date,
        centre            AS model,
        AVG(tmax_celsius) AS forecast_high_c,
        COUNT(*)          AS ensemble_members
    FROM forecast_archive
    WHERE horizon = 1
      AND centre <> 'live_deterministic'
    GROUP BY station_id, target_date, centre
)

SELECT
    f.station_id,
    f.target_date,
    f.model,
    f.ensemble_members,
    ROUND(f.forecast_high_c, 2)                  AS forecast_high_c,
    ROUND(a.tmax_celsius, 2)                     AS observed_high_c,
    ROUND(f.forecast_high_c - a.tmax_celsius, 2) AS error_c
FROM model_forecast AS f
JOIN actuals AS a
    ON  a.station_id = f.station_id
    AND a.local_date = f.target_date
WHERE a.tmax_celsius BETWEEN -60 AND 60;
