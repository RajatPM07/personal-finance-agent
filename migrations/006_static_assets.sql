-- 006_static_assets.sql
-- W3.2: add Zerodha as a static-valued holding alongside the existing
-- Zfunds MF static row (already in `assets`, seeded via 003_seed.local.sql).
-- Per the locked V1 roadmap r2:
--   docs/superpowers/specs/2026-04-26-v1-roadmap-r2-reprioritization.md §2 W3.2
-- We replace live Kite Connect / MF CAS integrations with single static-value
-- rows so net-worth queries (Week 4) have something to read without recurring
-- API costs. Update current_value manually if holdings change materially.
--
-- Note: identifier='static' (not 'CHM486') intentionally — keeps this file
-- PII-free so it can be committed. The Zerodha account number CHM486 lives
-- in the gitignored 003_seed.local.sql in the `accounts` table; this `assets`
-- row is just the valuation snapshot.

INSERT INTO assets (id, user_id, type, institution, identifier, current_value, last_valued_at, notes)
VALUES (
  '40000000-0000-0000-0000-000000000002',
  '00000000-0000-0000-0000-000000000001',
  'stock',
  'Zerodha',
  'static',
  200000.00,
  now(),
  'Static holding ~₹2L. No further contributions. Update current_value manually if changes.'
)
ON CONFLICT (id) DO NOTHING;
