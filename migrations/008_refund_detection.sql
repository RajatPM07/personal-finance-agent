-- migrations/008_refund_detection.sql
-- W5.1 refund + self-transfer detection schema.
-- Per docs/superpowers/specs/2026-05-21-refund-detection-design.md §4.

-- 1. New flag mirroring is_refund's shape (nullable, no default).
ALTER TABLE transactions
  ADD COLUMN is_self_transfer boolean;   -- NULL = unchecked; false = checked/not; true = confirmed

-- 2. Shift is_refund from default-false to tri-state nullable. Drop the default
--    so new ingestions get NULL; nullify existing rows so the backfill processes them.
ALTER TABLE transactions ALTER COLUMN is_refund DROP DEFAULT;
UPDATE transactions
  SET is_refund = NULL
  WHERE is_refund = false AND linked_txn_id IS NULL;
-- Rows where linked_txn_id is already set: leave is_refund alone.
-- (Zero such rows today; future-proofing clause for re-runnable migrations.)

-- 3. Performance indexes for matcher queries.
CREATE INDEX IF NOT EXISTS idx_transactions_account_date
  ON transactions (account_id, date);   -- refund matcher: candidates in this account, date range
CREATE INDEX IF NOT EXISTS idx_transactions_amount_date
  ON transactions (amount, date);       -- self-transfer matcher: matching-amount debits across all accounts
