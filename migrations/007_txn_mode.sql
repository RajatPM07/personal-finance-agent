-- 007_txn_mode.sql
-- W3.4 ICICI Savings parser populates this column from the PDF's MODE column
-- (UPI / NEFT / IMPS / ATM / BIL/PAY / SAL / INT.PD / TFR / etc.). Other
-- parsers (ICICI CC, AMEX CC, Paytm) leave NULL — they don't have a structured
-- payment-rail field. W5 normalization can use txn_mode as a strong prior
-- (ATM → "Cash Withdrawal", SAL → "Salary", BIL/PAY → "Bills", etc.).

ALTER TABLE transactions
  ADD COLUMN txn_mode TEXT;

COMMENT ON COLUMN transactions.txn_mode IS
  'Payment rail / mode from bank statements (UPI, NEFT, IMPS, ATM, BIL/PAY, SAL, INT.PD, TFR, etc.). Populated by ICICI Savings parser; NULL for credit-card and Paytm parsers.';
