-- 005_category_hint.sql
-- W3.1 Paytm parser populates this column from the Tags column in Paytm's XLSX.
-- ICICI/AMEX rows leave this NULL. W5 normalization layer treats this as a
-- strong prior (not the final answer) — see roadmap r2 §4.

ALTER TABLE transactions
  ADD COLUMN category_hint TEXT;

COMMENT ON COLUMN transactions.category_hint IS
  'External pre-categorization (e.g. Paytm Tags). W5 normalization treats as strong prior, not final answer.';
