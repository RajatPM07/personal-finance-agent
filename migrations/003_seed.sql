-- migrations/003_seed.sql — TEMPLATE. Real values live in 003_seed.local.sql (gitignored).
-- Applying this file as-is is a no-op (and intentionally errors on the angle-bracket
-- literals before any partial state is committed); see 003_seed.local.sql for the
-- actual insert with real values.

INSERT INTO users (id, telegram_handle, role, display_name)
VALUES ('00000000-0000-0000-0000-000000000001', '<rajat_handle>', 'admin', 'Rajat')
ON CONFLICT (telegram_handle) DO NOTHING;

-- Accounts: one row per bank/card/UPI/broker
INSERT INTO accounts (id, user_id, type, institution, identifier, nickname)
VALUES
  ('10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'bank',        'ICICI',   '<last4>',         'ICICI Savings'),
  ('10000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', 'bank',        'HDFC',    '<last4>',         'HDFC Savings'),
  ('10000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000001', 'credit_card', 'ICICI',   '<last4>',         'ICICI CC'),
  ('10000000-0000-0000-0000-000000000004', '00000000-0000-0000-0000-000000000001', 'credit_card', 'HDFC',    '<last4>',         'HDFC CC'),
  ('10000000-0000-0000-0000-000000000005', '00000000-0000-0000-0000-000000000001', 'credit_card', 'AMEX',    '<last4>',         'AMEX CC'),
  ('10000000-0000-0000-0000-000000000006', '00000000-0000-0000-0000-000000000001', 'upi',         'Paytm',   '<upi_handle_1>',  'Paytm UPI 1'),
  ('10000000-0000-0000-0000-000000000007', '00000000-0000-0000-0000-000000000001', 'upi',         'Paytm',   '<upi_handle_2>',  'Paytm UPI 2'),
  ('10000000-0000-0000-0000-000000000008', '00000000-0000-0000-0000-000000000001', 'broker',      'Zerodha', '<client_code>',   'Zerodha'),
  ('10000000-0000-0000-0000-000000000009', '00000000-0000-0000-0000-000000000001', 'mf',          'Zfunds',  '<folio>',         'Zfunds MF')
ON CONFLICT (id) DO NOTHING;

-- Personal loan liability
INSERT INTO liabilities (id, user_id, type, original_principal, outstanding_principal, interest_rate, emi_amount, tenure_remaining_months, start_date, lender)
VALUES ('20000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'personal_loan', 0, 0, 10.3, 22000, 0, '<start_date>', '<lender>')
ON CONFLICT (id) DO NOTHING;

-- EMI commitment linked to the liability
INSERT INTO commitments (id, user_id, type, name, amount, frequency, next_due_date, account_id, liability_id)
VALUES ('30000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'emi', 'Personal loan EMI', 22000, 'monthly', '<next_due>', '10000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001')
ON CONFLICT (id) DO NOTHING;
