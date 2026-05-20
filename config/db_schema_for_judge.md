# Database schema (relevant tables for SQL judge)

Source of truth: `migrations/001_init.sql` + `005_category_hint.sql` + `006_static_assets.sql` + `007_txn_mode.sql`.
This excerpt is the judge's view of the world — keep it accurate but minimal.

## transactions
- `id uuid` — primary key
- `user_id uuid` — FK users
- `date date` — transaction date (Asia/Kolkata)
- `txn_time timestamptz` — exact timestamp when source provides it; NULL for most PDF rows
- `amount numeric(12,2)` — always positive; direction column carries the sign
- `currency text` — defaults to 'INR'
- `direction text` — 'in' or 'out'
- `is_refund boolean` — defaults to false
- `linked_txn_id uuid` — FK transactions (refund links to original)
- `raw_merchant text` — text as it appears on the source
- `normalized_merchant text` — populated post-W5; NULL until then
- `category_id uuid` — FK categories
- `subcategory text`
- `source text` — channel ('manual_pdf', 'manual_xlsx', etc.)
- `source_ref text` — filename / email-id / sms-id; AUDIT only
- `parser_version text` — e.g. 'icici-savings-pdf/v2'; part of import_hash
- `category_hint text` — Paytm-only today; NULL elsewhere
- `txn_mode text` — ICICI-Savings-only today; NULL elsewhere
- `account_id uuid` — FK accounts
- `is_deleted boolean` — soft delete; INCLUDE this in WHERE when filtering "active"
- `ingested_at timestamptz`

## accounts
- `id uuid`
- `user_id uuid` — FK users
- `type text` — 'savings' / 'cc' / 'upi' / 'mf' / 'stock' etc.
- `institution text` — bank/provider name
- `nickname text` — human-readable label
- `identifier text` — last-4 / handle / 'static' for assets-only rows
- `is_active boolean`

## categories
- `id uuid`
- `user_id uuid`
- `name text` — e.g. 'Food', 'Transport'
- `parent_id uuid` — FK categories (for sub-categories)

## assets
- `id uuid`
- `user_id uuid`
- `account_id uuid` — FK accounts
- `type text` — 'mf' / 'stock' / 'cash' etc.
- `current_value numeric(14,2)` — updated manually
- `identifier text` — 'static' for V1 manually-maintained rows

## liabilities
- `id uuid`
- `user_id uuid`
- `principal numeric(14,2)`
- `rate_pct numeric(5,2)`
- `name text`

## ingestion_log
- `id uuid`
- `source text` — 'manual_pdf' / 'manual_xlsx'
- `source_ref text` — filename
- `status text` — 'success' / 'skipped_duplicate' / 'total_check_failed' / etc.
- `rows_added int`
- `declared_total numeric(14,2)`
- `extracted_total numeric(14,2)`
- `timestamp timestamptz`

## Notes for the judge

- **Date column is `date`, not `txn_date`.** Past parsers used `txn_date` internally before the insert dict mapped to `date`.
- **`amount` is unsigned**; use `direction = 'out'` for spend, `direction = 'in'` for income.
- **Soft deletes:** unless the question explicitly asks for "deleted" rows, filter by `is_deleted = false` or omit the filter (default behavior depends on the question intent — flag with `verdict=uncertain` if ambiguous).
- **Asia/Kolkata timezone:** `date` is calendar-date in Asia/Kolkata. `txn_time` is timestamptz; cast to date in Asia/Kolkata before grouping if mixing.
- **Single-user V1 — DO NOT include a `user_id` filter in your WHERE clauses.** The database has exactly one user and you do not have access to the actual `user_id` UUID. Adding `WHERE user_id = '...'` will produce hallucinated UUIDs and execution errors. Omit it entirely.
