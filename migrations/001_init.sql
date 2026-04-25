-- ============================================================================
-- Personal Finance Agent — initial schema (migration 001)
-- Applies: roles (no password), extensions, tables, triggers.
--
-- TODO(V2): ENABLE ROW LEVEL SECURITY on every user-scoped table before
-- Ayushi onboarding. Per PRD §18.10, RLS is a hard prerequisite for V2.
-- Write policies in a separate migration that will be applied just before
-- Ayushi's `users` row is created.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- for gen_random_uuid()

-- Read-only role for the future SQL agent (Week 5+). Created here WITHOUT a
-- password so this migration is safe to commit. The password is set by the
-- paired migrations/001_init.local.sql file, which is gitignored.
CREATE ROLE finance_agent_readonly WITH LOGIN;
GRANT USAGE ON SCHEMA public TO finance_agent_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO finance_agent_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO finance_agent_readonly;

-- ============================================================================
-- Tables (dependency order: users → accounts → categories → transactions →
-- commitments → liabilities → assets → asset_snapshots → goals →
-- income_events → ingestion_log → agent_memory → heartbeat)
--
-- NOTE: `commitments.liability_id` references `liabilities`, and `liabilities`
-- is defined after `commitments`. We add that FK with ALTER TABLE after both
-- tables exist to avoid forward-reference issues.
-- ============================================================================

-- users -----------------------------------------------------------------------
CREATE TABLE users (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  telegram_handle text UNIQUE,
  role            text CHECK (role IN ('admin','user')),
  display_name    text,
  created_at      timestamptz DEFAULT now()
);

-- accounts --------------------------------------------------------------------
CREATE TABLE accounts (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid REFERENCES users,
  type        text CHECK (type IN ('bank','credit_card','upi','broker','mf')),
  institution text,
  identifier  text,
  nickname    text,
  active      boolean DEFAULT true,
  created_at  timestamptz DEFAULT now()
);

-- categories ------------------------------------------------------------------
CREATE TABLE categories (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid REFERENCES users,
  name          text,
  parent_id     uuid REFERENCES categories,
  mcc_code      text,  -- ISO 18245 MCC code from greggles/mcc-codes, nullable
  is_system     boolean DEFAULT false,
  created_by    text CHECK (created_by IN ('agent','user','seed')),
  first_seen_at timestamptz DEFAULT now()
);

-- transactions ----------------------------------------------------------------
CREATE TABLE transactions (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             uuid REFERENCES users,
  date                date NOT NULL,
  txn_time            timestamptz,             -- exact timestamp when source provides it (SMS, txn emails); null for most PDF rows
  amount              numeric(12,2) NOT NULL,
  currency            text DEFAULT 'INR',      -- V1 primarily INR; schema forward-compatible for foreign txns
  direction           text CHECK (direction IN ('in','out')),
  is_refund           boolean DEFAULT false,
  linked_txn_id       uuid REFERENCES transactions,  -- links refund to original transaction
  raw_merchant        text,
  normalized_merchant text,
  category_id         uuid REFERENCES categories,
  subcategory         text,
  source              text,
  source_ref          text,   -- email_id / pdf_hash / sms_id — AUDIT column only; NOT part of import_hash
  pdf_content_hash    text,   -- sha256 of source PDF bytes; null for non-PDF rows; part of Mode B import_hash
  source_row_ordinal  int,    -- within-PDF row position (1..N per parser); part of Mode B import_hash
  parser_version      text,   -- e.g. 'bout@0.4.2', 'hdfc_cc_v2_infinia@1.3.0'; part of import_hash
  import_hash         text UNIQUE,
  account_id          uuid REFERENCES accounts,
  visibility          text DEFAULT 'shared' CHECK (visibility IN ('shared','private','excluded')),
  is_deleted          boolean DEFAULT false,   -- soft delete; included in dedup checks per Firefly III pattern
  notes               text,
  ingested_at         timestamptz DEFAULT now(),
  updated_at          timestamptz DEFAULT now()
);

-- commitments -----------------------------------------------------------------
-- liability_id FK is added after `liabilities` is created (see ALTER TABLE below).
CREATE TABLE commitments (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid REFERENCES users,
  type          text CHECK (type IN ('emi','rent','subscription','insurance_premium','other')),
  name          text,
  amount        numeric(12,2),
  frequency     text CHECK (frequency IN ('monthly','quarterly','annual')),
  next_due_date date,
  account_id    uuid REFERENCES accounts,
  active        boolean DEFAULT true,
  liability_id  uuid,  -- FK added after liabilities table is created
  updated_at    timestamptz DEFAULT now()
);

-- liabilities -----------------------------------------------------------------
CREATE TABLE liabilities (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                 uuid REFERENCES users,
  type                    text CHECK (type IN ('personal_loan','home_loan','credit_card_revolving','other')),
  original_principal      numeric(14,2),
  outstanding_principal   numeric(14,2),
  interest_rate           numeric(5,2),
  emi_amount              numeric(12,2),
  tenure_remaining_months int,
  start_date              date,
  lender                  text,
  last_updated_at         timestamptz DEFAULT now(),
  updated_at              timestamptz DEFAULT now()
);

-- Now add the deferred FK from commitments.liability_id → liabilities.id
ALTER TABLE commitments
  ADD CONSTRAINT commitments_liability_id_fkey
  FOREIGN KEY (liability_id) REFERENCES liabilities;

-- assets ----------------------------------------------------------------------
CREATE TABLE assets (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid REFERENCES users,
  type           text CHECK (type IN ('stock','mf','fd','epf','ppf','savings','other')),
  institution    text,
  identifier     text,
  quantity       numeric(18,4),
  avg_cost       numeric(12,2),
  current_value  numeric(14,2),
  currency       text DEFAULT 'INR',
  last_valued_at timestamptz,
  notes          text,
  updated_at     timestamptz DEFAULT now()
);

-- asset_snapshots -------------------------------------------------------------
CREATE TABLE asset_snapshots (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id  uuid REFERENCES assets,
  valued_at timestamptz,
  value     numeric(14,2)
);

-- goals -----------------------------------------------------------------------
CREATE TABLE goals (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid REFERENCES users,
  name           text,
  target_amount  numeric(14,2),
  current_amount numeric(14,2) DEFAULT 0,  -- CACHE ONLY. Written exclusively by budget_engine.py on its weekly run (computes from tagged transactions or designated asset balance). Never written from anywhere else. Treat as stale between updates; authoritative answer is always the computation, not the column.
  target_date    date,
  priority       int,
  notes          text,
  status         text CHECK (status IN ('active','paused','achieved','abandoned')),
  created_at     timestamptz DEFAULT now(),
  updated_at     timestamptz DEFAULT now()
);

-- income_events ---------------------------------------------------------------
CREATE TABLE income_events (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid REFERENCES users,
  date          date,
  amount        numeric(12,2),
  bucket        text CHECK (bucket IN ('recurring_salary','variable_comp','other')),
  source_txn_id uuid REFERENCES transactions,
  classified_by text CHECK (classified_by IN ('agent','user','payslip'))
);

-- ingestion_log ---------------------------------------------------------------
CREATE TABLE ingestion_log (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source          text,
  source_ref      text,
  status          text CHECK (status IN ('success','skipped_duplicate','possible_duplicate','failed','needs_review','total_check_failed')),
  rows_added      int,
  declared_total  numeric(14,2),  -- from CC statement
  extracted_total numeric(14,2),  -- sum of extracted rows
  error_msg       text,
  timestamp       timestamptz DEFAULT now()
);

-- agent_memory ----------------------------------------------------------------
CREATE TABLE agent_memory (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid REFERENCES users,
  key        text,
  value      jsonb,
  updated_at timestamptz DEFAULT now(),
  UNIQUE(user_id, key)
);

-- heartbeat -------------------------------------------------------------------
CREATE TABLE heartbeat (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  component text,
  status    text CHECK (status IN ('ok','failed','late')),
  last_ping timestamptz DEFAULT now(),
  error_msg text
);

-- ============================================================================
-- updated_at triggers
-- Apply to all tables with updated_at:
--   transactions, commitments, liabilities, assets, goals, agent_memory
-- ============================================================================
CREATE OR REPLACE FUNCTION update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_updated_at BEFORE UPDATE ON transactions FOR EACH ROW EXECUTE FUNCTION update_timestamp();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON commitments  FOR EACH ROW EXECUTE FUNCTION update_timestamp();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON liabilities  FOR EACH ROW EXECUTE FUNCTION update_timestamp();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON assets       FOR EACH ROW EXECUTE FUNCTION update_timestamp();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON goals        FOR EACH ROW EXECUTE FUNCTION update_timestamp();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON agent_memory FOR EACH ROW EXECUTE FUNCTION update_timestamp();

-- ============================================================================
-- LiteLLM Supabase callback table — `public.request_logs`
--
-- Source: https://docs.litellm.ai/docs/observability/supabase_integration
-- Confirmed in tasks/preconditions-notes.md §0.4: LiteLLM SDK does NOT
-- auto-create this table; we must pre-create it. (`litellm_spend_logs` is
-- the Proxy-server schema, which we are NOT running.) DDL below is verbatim
-- from the docs.
-- ============================================================================
create table public.request_logs (
    id bigint generated by default as identity,
    created_at timestamp with time zone null default now(),
    model text null default ''::text,
    messages json null default '{}'::json,
    response json null default '{}'::json,
    end_user text null default ''::text,
    status text null default ''::text,
    error json null default '{}'::json,
    response_time real null default '0'::real,
    total_cost real null,
    additional_details json null default '{}'::json,
    litellm_call_id text unique,
    primary key (id)
) tablespace pg_default;

-- ============================================================================
-- RLS — DISABLED in V1 per PRD §18.10.
--
-- Supabase auto-enables Row Level Security on tables created via the SQL
-- editor (relrowsecurity=true). With RLS on and zero policies, a non-
-- superuser role's SELECT returns ZERO ROWS — no error, just silent filter.
-- The Supabase dashboard's "RLS: Disabled" column is misleading: it means
-- "no custom policies attached," NOT "RLS literally turned off." Two
-- different states, same display label.
--
-- For V1 (single-user), we explicitly DISABLE RLS so the finance_agent_readonly
-- role can do its job. RLS is a V2 hard prerequisite (PRD §18.10) — when V2
-- lands, a separate migration will:
--   1. ENABLE ROW LEVEL SECURITY on every user-scoped table
--   2. Add per-table policies tying visibility to user_id
-- See tasks/lessons.md "Supabase auto-enables RLS" entry for full backstory.
-- ============================================================================
ALTER TABLE users           DISABLE ROW LEVEL SECURITY;
ALTER TABLE accounts        DISABLE ROW LEVEL SECURITY;
ALTER TABLE categories      DISABLE ROW LEVEL SECURITY;
ALTER TABLE transactions    DISABLE ROW LEVEL SECURITY;
ALTER TABLE commitments     DISABLE ROW LEVEL SECURITY;
ALTER TABLE liabilities     DISABLE ROW LEVEL SECURITY;
ALTER TABLE assets          DISABLE ROW LEVEL SECURITY;
ALTER TABLE asset_snapshots DISABLE ROW LEVEL SECURITY;
ALTER TABLE goals           DISABLE ROW LEVEL SECURITY;
ALTER TABLE income_events   DISABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_log   DISABLE ROW LEVEL SECURITY;
ALTER TABLE agent_memory    DISABLE ROW LEVEL SECURITY;
ALTER TABLE heartbeat       DISABLE ROW LEVEL SECURITY;
ALTER TABLE request_logs    DISABLE ROW LEVEL SECURITY;
