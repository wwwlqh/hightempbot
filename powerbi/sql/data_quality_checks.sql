-- Data quality rules. One row per rule with the number of rows it checked and
-- the number that failed.

WITH settled AS (
    SELECT *
    FROM ledger
    WHERE event_type IN ('bet', 'dry_run')
      AND outcome IN ('WIN', 'LOSS', 'CLOSED')
),

snapshot AS (
    SELECT DATE(MAX(bet_ts)) AS as_of FROM ledger
),

neighbour_days AS (
    SELECT
        tmax_celsius,
        LAG(tmax_celsius)  OVER station_days AS previous_c,
        LEAD(tmax_celsius) OVER station_days AS next_c
    FROM actuals
    WINDOW station_days AS (PARTITION BY station_id ORDER BY local_date)
),

day_ahead_forecast AS (
    SELECT DISTINCT station_id, target_date
    FROM forecast_archive
    WHERE horizon = 1
),

latest_run AS (
    SELECT MAX(id) AS run_id FROM wallet_reconciliation_runs
),

checks AS (
    SELECT 'TRD-01' AS check_id, 'Trades' AS area,
           'Settled bet has a P&L value' AS rule,
           COUNT(*) AS rows_checked, COALESCE(SUM(pnl IS NULL), 0) AS rows_failed
    FROM settled

    UNION ALL
    SELECT 'TRD-02', 'Trades', 'Won or lost bet has an observed high',
           COUNT(*), COALESCE(SUM(actual_tmax IS NULL), 0)
    FROM settled
    WHERE outcome IN ('WIN', 'LOSS')

    UNION ALL
    SELECT 'TRD-03', 'Trades', 'Fill price is between 0 and 1',
           COUNT(*), COALESCE(SUM(fill_price <= 0 OR fill_price >= 1), 0)
    FROM ledger
    WHERE fill_price IS NOT NULL

    UNION ALL
    SELECT 'TRD-04', 'Trades', 'Exchange order id is unique',
           COUNT(*), COUNT(*) - COUNT(DISTINCT order_id)
    FROM ledger
    WHERE order_id IS NOT NULL

    UNION ALL
    SELECT 'TRD-05', 'Trades', 'Bet station exists in the station list',
           COUNT(*), COALESCE(SUM(station_id NOT IN (SELECT icao FROM enrolled_stations)), 0)
    FROM ledger

    UNION ALL
    SELECT 'TRD-06', 'Trades', 'Pending bet is less than 2 days past its target date',
           COUNT(*), COALESCE(SUM(target_date < DATE(snapshot.as_of, '-2 day')), 0)
    FROM ledger, snapshot
    WHERE outcome = 'PENDING'

    UNION ALL
    SELECT 'WX-01', 'Weather', 'Observed high is between -60 and 60 °C',
           COUNT(*), COALESCE(SUM(tmax_celsius NOT BETWEEN -60 AND 60), 0)
    FROM actuals

    UNION ALL
    SELECT 'WX-02', 'Weather', 'Observed high is not a one-day spike of 15 °C or more',
           COUNT(*),
           COALESCE(SUM(
               (tmax_celsius - previous_c >= 15 AND tmax_celsius - next_c >= 15)
               OR (previous_c - tmax_celsius >= 15 AND next_c - tmax_celsius >= 15)
           ), 0)
    FROM neighbour_days
    WHERE previous_c IS NOT NULL AND next_c IS NOT NULL

    UNION ALL
    SELECT 'WX-03', 'Weather', 'Day-ahead forecast has an observation to score against',
           COUNT(*), COALESCE(SUM(a.station_id IS NULL), 0)
    FROM day_ahead_forecast AS f
    LEFT JOIN (SELECT DISTINCT station_id, local_date FROM actuals) AS a
        ON a.station_id = f.station_id AND a.local_date = f.target_date

    UNION ALL
    SELECT 'WX-04', 'Weather', 'Forecast high is between -60 and 60 °C',
           COUNT(*), COALESCE(SUM(tmax_celsius NOT BETWEEN -60 AND 60), 0)
    FROM forecast_archive

    UNION ALL
    SELECT 'REC-01', 'Reconciliation', 'Wallet trade matches a ledger bet (latest run)',
           COUNT(*), COALESCE(SUM(rec.match_status <> 'exact'), 0)
    FROM wallet_reconciliation_records AS rec
    JOIN latest_run ON latest_run.run_id = rec.run_id
    WHERE rec.record_type = 'trade'

    UNION ALL
    SELECT 'REC-02', 'Reconciliation', 'Open position matches the ledger (latest run)',
           COUNT(*),
           COALESCE(SUM(rec.match_status NOT IN ('api_position_matched', 'api_position_resolved_matched')), 0)
    FROM wallet_reconciliation_records AS rec
    JOIN latest_run ON latest_run.run_id = rec.run_id
    WHERE rec.record_type = 'position'
)

SELECT
    check_id,
    area,
    rule,
    rows_checked,
    rows_failed,
    CASE WHEN rows_failed = 0 THEN 'Pass' ELSE 'Fail' END AS status
FROM checks
ORDER BY check_id;
