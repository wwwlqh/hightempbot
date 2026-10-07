-- One row per weather station the bot trades.

SELECT
    icao              AS station_id,
    city,
    lat               AS latitude,
    lon               AS longitude,
    timezone,
    unit              AS market_unit,
    resolution_source,
    status
FROM enrolled_stations
ORDER BY station_id;
