-- 2026-05-13 — orphan-recovery: prevent duplicate ledger rows for the same Polymarket fill.
-- Idempotent. If the live DB has pre-existing duplicate order_ids, this will fail and require
-- manual reconciliation before re-running.
-- BEFORE APPLYING: run `python scripts/check_ledger_order_id_duplicates.py <db_path>` and
-- resolve any duplicates it reports. The CREATE UNIQUE INDEX below will fail otherwise.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_order_id
    ON ledger(order_id) WHERE order_id IS NOT NULL;
