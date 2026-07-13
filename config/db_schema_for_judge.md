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
- **Multi-user — you MUST filter by user_id.** Every query runs on behalf of ONE user whose UUID is given to you as `:user_id` in the prompt. Add `WHERE user_id = '<that UUID>'` (using the literal UUID string provided, not a placeholder) to EVERY table you read that has a `user_id` column (`transactions`, `accounts`, `categories`, `assets`, `liabilities`). A query without the caller's `user_id` literal is rejected by the validator. Never reference any other user's UUID.
- **Merchant search MUST use `raw_merchant ILIKE '%keyword%'`** — `normalized_merchant` is NULL for all rows until Week 5 normalization runs. Never filter or group on `normalized_merchant`. Always use case-insensitive partial match on `raw_merchant` for merchant queries (e.g. `raw_merchant ILIKE '%Blinkit%'` not `normalized_merchant = 'Blinkit'`).
