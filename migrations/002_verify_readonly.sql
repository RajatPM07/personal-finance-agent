-- migrations/002_verify_readonly.sql
-- Run this connected as finance_agent_readonly. Every INSERT/UPDATE/DELETE/
-- TRUNCATE below MUST fail with "permission denied." If any succeeds, the
-- readonly role is broken.

-- Expected: permission denied
INSERT INTO transactions (user_id, date, amount, direction) VALUES (gen_random_uuid(), CURRENT_DATE, 1, 'out');
-- Expected: permission denied
UPDATE transactions SET amount = 0 WHERE id IS NOT NULL;
-- Expected: permission denied
DELETE FROM transactions WHERE id IS NOT NULL;
-- Expected: permission denied
TRUNCATE transactions;
-- Expected: SUCCESS
SELECT count(*) FROM transactions;
