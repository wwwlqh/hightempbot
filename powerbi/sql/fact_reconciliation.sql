-- One row per wallet record checked against the ledger, per reconciliation run.
-- The bot compares its own ledger with what the exchange API reports.

SELECT
    run.id                   AS run_id,
    run.sampled_at           AS run_at,
    rec.record_type,
    rec.match_status,
    CASE
        WHEN rec.match_status IN ('exact', 'api_position_matched', 'api_position_resolved_matched')
            THEN 'Matched'
        WHEN rec.match_status = 'wallet_position'
            THEN 'Not checked'  -- written by an early version that did not match positions
        ELSE 'Exception'
    END                      AS result,
    rec.matched_ledger_id    AS trade_id,
    ROUND(rec.amount_usd, 2) AS amount_usd
FROM wallet_reconciliation_records AS rec
JOIN wallet_reconciliation_runs    AS run ON run.id = rec.run_id
ORDER BY run.id, rec.id;
