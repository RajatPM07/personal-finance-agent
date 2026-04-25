-- migrations/002_verify_readonly.sql
-- Run this connected as finance_agent_readonly. Two classes of checks:
--   1. Mutations must FAIL with "permission denied" (negative tests).
--   2. SELECTs against seeded tables must RETURN > 0 rows (positive read test).
-- The positive read test was added 2026-04-25 after discovering Supabase
-- auto-enables RLS on tables created via the SQL editor. With RLS on and zero
-- policies, the readonly role's SELECTs silently return zero rows — no error,
-- just quiet filtering — which a mutation-only drill can't catch.
-- See tasks/lessons.md "Supabase auto-enables RLS" entry.

-- ============================================================================
-- Negative tests — every line below MUST fail with "permission denied".
-- If any succeeds, the GRANT is too broad — investigate.
-- ============================================================================
-- Expected: permission denied
INSERT INTO transactions (user_id, date, amount, direction) VALUES (gen_random_uuid(), CURRENT_DATE, 1, 'out');
-- Expected: permission denied
UPDATE transactions SET amount = 0 WHERE id IS NOT NULL;
-- Expected: permission denied
DELETE FROM transactions WHERE id IS NOT NULL;
-- Expected: permission denied
TRUNCATE transactions;

-- ============================================================================
-- Positive tests — readonly role MUST be able to SEE seeded rows.
-- If any of these return 0 against a seeded DB, RLS is filtering — re-apply
-- the DISABLE ROW LEVEL SECURITY block from 001_init.sql.
-- ============================================================================
-- Expected: > 0 against a seeded DB (will be 0 only on a fresh unseeded DB).
SELECT count(*) AS users_visible       FROM users;
SELECT count(*) AS accounts_visible    FROM accounts;
SELECT count(*) AS liabilities_visible FROM liabilities;

-- Expected: SUCCESS, returns 0 until Week 2 ingestion lands. This line proves
-- the SELECT itself works (not silently filtered) even when the table is empty.
SELECT count(*) AS transactions_visible FROM transactions;
