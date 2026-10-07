-- One row per bet: live orders and paper (dry-run) bets.
-- Strategy and bracket bounds live in the event_detail JSON column.

SELECT
    id                                                   AS trade_id,
    CASE event_type WHEN 'bet' THEN 'Live' ELSE 'Paper' END AS mode,
    bet_ts                                               AS placed_at,
    station_id,
    target_date,
    COALESCE(json_extract(event_detail, '$.strategy'), side) AS strategy,
    side,
    json_extract(event_detail, '$.bracket_low')          AS bracket_low,
    json_extract(event_detail, '$.bracket_high')         AS bracket_high,
    json_extract(event_detail, '$.bracket_unit')         AS bracket_unit,
    ROUND(p_model, 4)                                    AS model_probability,
    ROUND(p_market, 4)                                   AS market_price,
    ROUND(edge, 4)                                       AS edge,
    ROUND(bet_size, 2)                                   AS stake,
    ROUND(fill_price, 4)                                 AS fill_price,
    actual_tmax                                          AS observed_high,
    outcome,
    ROUND(pnl, 2)                                        AS pnl
FROM ledger
WHERE event_type IN ('bet', 'dry_run')
ORDER BY trade_id;
