-- 2026-05-14 — dashboard performance: cover (event_type, outcome, bet_ts) for
-- the window-scoped aggregates in build_htb_data. Idempotent.
-- BEFORE APPLYING: nothing — index build is non-destructive on SQLite, but
-- can be slow on a large ledger (rebuild scans every row once).
CREATE INDEX IF NOT EXISTS idx_ledger_event_outcome_bet_ts
    ON ledger(event_type, outcome, bet_ts);
