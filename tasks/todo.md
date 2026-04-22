# PFA Week 1 — Foundation Implementation Plan (revision 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the Personal Finance Agent's runtime foundation so Week 2 ingestion can begin against a verified base.

**Architecture:** Single long-running Python 3.11+ process on the spare Mac — aiogram bot + APScheduler + FastAPI health endpoint orchestrated from one `app.py`. All business logic under `skills/finance/`. Writes use the Supabase service_role; a future SQL agent (Week 5) will add a separate readonly psycopg path. Backup runs as a **separate launchd job** (not inside the async app) so it survives app restarts and can't block the event loop. An external healthchecks.io watchdog complements an internal per-component `heartbeat` table.

**Tech Stack:** Python 3.11, aiogram (MIT), APScheduler (MIT, AsyncIOScheduler), FastAPI (MIT), LiteLLM (MIT, version pinned at preconditions-check time), Supabase sync client wrapped via `asyncio.to_thread` in async contexts, pikepdf (decrypt) + pdfplumber (extraction) for PDF smoke in Week 1; row-level ICICI parser deferred to Week 2. pytest.

**PRD reference:** `PRD.md` V2.1 §7 (schema + dedup strategy, incl. §19.5 post-lock amendment on Mode B hash), §11 Week 1, §17 entry checklist, §19 decisions log.

**Revision history:**
- **r3 (2026-04-21):** second-pass review caught four bugs that had slipped through r2. Applied inline:
  1. **Fix 1 — Task 5.1 heartbeat middleware never wrote.** `adb(table.insert, payload)` passes an unbound method; `.insert(payload)` returns a builder that must be terminated with `.execute()`. Rewrote to `adb(lambda: ...insert(payload).execute())` and added a middleware unit test that asserts `.execute()` is invoked. Matches CLAUDE.md invariant #2.
  2. **Fix 2 — readonly password leaked into git.** Split `001_init.sql` into the committed version (no password, just `CREATE ROLE finance_agent_readonly WITH LOGIN;` + grants) and a gitignored `001_init.local.sql` containing the `ALTER ROLE ... WITH PASSWORD '...'` statement applied manually. Added `*.local.sql` to `.gitignore`.
  3. **Fix 3 — seed committed real PII.** Split `003_seed.sql` into a committed template (keeps `<angle_bracket>` literals) and a gitignored `003_seed.local.sql` with real account identifiers. The template stays reproducible; real values never hit git.
  4. **Fix 4 — `python-dotenv` missing from deps.** Added to `pyproject.toml` in Task 1.5 so `scripts/backup_supabase.py` imports cleanly.
- r2 (2026-04-21): incorporated 20-point plan review. Added Task 0 preconditions verification. Split hashing/DB into 4a/4b. Moved backup to launchd (out of APScheduler). Folded graceful shutdown into Task 7. Moved launchd app-supervisor plist to just before exit verification. Rewrote `import_hash` for Option Y + `parser_version`. Dropped `readonly_client()` from Week 1. Added `adb()` async wrapper for sync Supabase calls.
- r1 (2026-04-21): initial plan.

---

## Preconditions — §17 entry checklist + system deps (Rajat's tasks, not code)

Every box below must be green before Task 0 starts. None are code the engineer writes.

- [ ] Python 3.11+ installed (`python3 --version`)
- [ ] **macOS system deps:** `brew install ghostscript tcl-tk` (required by `camelot-py[cv]` at import time)
- [ ] Supabase project URL + `service_role` key + `anon` key copied to scratch pad
- [ ] **Supabase Postgres connection string** obtained from dashboard (Project Settings → Database → Connection string). Use the **Transaction pooler (Supavisor)** URL, not direct port 5432. Copy the entire libpq string including `?sslmode=require`.
- [ ] Anthropic API key issued with monthly spend cap (₹1,500 soft / ₹3,000 hard); **plus a separate ₹1,000 Week 2 budget line for Month 1 dual-model calibration**
- [ ] Gemini API key from https://aistudio.google.com/
- [ ] Groq API key from https://console.groq.com/
- [ ] Telegram **main** bot created via @BotFather; token + chat ID saved
- [ ] Telegram **secondary alert bot** created via @BotFather (independent of main); token + chat ID saved
- [ ] healthchecks.io account created; check "PFA Mac heartbeat" (period 15 min, grace 15 min); Telegram integration wired to secondary bot; unique ping URL copied
- [ ] Gmail API enabled in Google Cloud Console; OAuth2 client credentials downloaded as `gmail_oauth.json`
- [ ] External drive mounted or location identified for weekly backup copy
- [ ] macOS System Settings: prevent sleep on power adapter; disable automatic updates; "Start automatically after power failure"

---

## File structure

Paths relative to repo root (`~/projects/personal-finance-agent/`).

**Created in Week 1:**
- `.gitignore`, `README.md`, `LICENSE_AUDIT.md`, `.env.example`, `credentials.example.yaml`
- `pyproject.toml` — deps, pinned LiteLLM, dev deps, `[tool.ruff]`, `[tool.mypy]`
- `config/model_routing.yaml`
- `migrations/001_init.sql` — `CREATE EXTENSION pgcrypto` + tables + triggers + `TODO(V2)` RLS comment
- `migrations/002_verify_readonly.sql` — readonly role non-mutation assertions
- `migrations/003_seed.sql` — `users`, `accounts`, `liabilities`, `commitments` seed with `ON CONFLICT DO NOTHING`
- `app.py` — orchestrator; aiogram polling + APScheduler + FastAPI; SIGTERM graceful shutdown
- `skills/finance/__init__.py`
- `skills/finance/lib/__init__.py`
- `skills/finance/lib/settings.py` — pydantic-settings `.env` loader
- `skills/finance/lib/db.py` — sync Supabase client + `adb()` async wrapper (no readonly_client in Week 1)
- `skills/finance/lib/llm.py` — thin LiteLLM wrapper (no `images`, no `reload_routing` yet)
- `skills/finance/lib/hashing.py` — two-mode `import_hash` with `parser_version`
- `skills/finance/lib/logging_setup.py` — RotatingFileHandler + stdout
- `skills/finance/monitoring/__init__.py`
- `skills/finance/monitoring/alerts.py` — async-native Telegram alerts via secondary bot
- `skills/finance/monitoring/heartbeat.py` — internal heartbeat + external watchdog ping + stale-check (all async)
- `skills/finance/monitoring/health.py` — FastAPI `/health`
- `skills/finance/bot/__init__.py`
- `skills/finance/bot/main.py` — aiogram `Bot`, `Dispatcher`, middleware that bumps `telegram_bot` heartbeat on every processed message; `/ping`
- `skills/finance/ingestion/__init__.py`
- `skills/finance/ingestion/parsers/__init__.py`
- (Week 2) `skills/finance/ingestion/parsers/icici_parser.py` — full row-level PDF→rows parser; NOT created in Week 1. Task 9 only does a decrypt+extract smoke in `tests/test_pdf_smoke.py`.
- `scripts/backup_supabase.py` — standalone; invoked by launchd, not APScheduler
- `scripts/restore_drill.py` — one-shot scratch-restore verification
- `launchd/com.rajat.pfa.app.plist` — supervisor for `app.py` (KeepAlive=true)
- `launchd/com.rajat.pfa.backup.plist` — daily 3am `scripts/backup_supabase.py`
- `tests/__init__.py`
- `tests/test_llm.py`, `tests/test_db.py`, `tests/test_hashing.py`, `tests/test_heartbeat.py`, `tests/test_icici_parser.py`

**Placeholder directories** (empty `__init__.py` only, populated in later weeks):
`skills/finance/{normalization,categorization,reasoning,nudging,privacy,reconciliation,commands}/`, `tests/golden_fixtures/`.

---

## Task 0: Preconditions verification (tooling sanity check)

Purpose: catch API guesses before they poison downstream tasks.

- [ ] **Step 0.1: Check current `bout` version and API surface**

```bash
python3.11 -m venv /tmp/bout_probe && source /tmp/bout_probe/bin/activate
pip install bout
python -c "import bout; print('version:', getattr(bout, '__version__', 'unknown')); print('attrs:', [a for a in dir(bout) if not a.startswith('_')])"
python -c "import bout; help(bout.parse)" 2>&1 | head -80
deactivate && rm -rf /tmp/bout_probe
```

Record findings into `tasks/preconditions-notes.md`:
- Exact version installed
- Public attrs on `bout`
- Parameter name for PDF password (`password`? `passwd`? `pwd`? positional?)
- Return shape — is it `bout.parse(...).transactions` or `.entries` or a plain list or something else?
- Field names on each transaction (is it `.is_debit` or `.type` or `.direction`?)

If the API doesn't match what Task 9.1 assumes, **update Task 9.1 inline** before Task 1 begins — do not leave it to discover mid-implementation.

- [ ] **Step 0.2: Check current LiteLLM stable version**

```bash
pip index versions litellm 2>&1 | head -5
```

Pick the highest stable version (no `-rc`, `-dev`). Update `pyproject.toml` dep pin in Task 1.5 accordingly (`litellm==<major>.<minor>.*`). Do NOT blindly pin to `1.60.*` — verify.

- [ ] **Step 0.3: Smoke-test the chosen LiteLLM version recognizes the model IDs we use**

```bash
pip install "litellm==<version>"
python -c "
import litellm
for m in ['gemini/gemini-2.5-flash', 'anthropic/claude-sonnet-4-6', 'groq/llama-3.3-70b-versatile']:
    try:
        print(m, '->', litellm.get_llm_provider(m))
    except Exception as e:
        print(m, 'FAIL:', e)
"
```

All three must resolve cleanly. If any fails, either bump LiteLLM or adjust the model ID in `config/model_routing.yaml`. Record which you chose and why in `tasks/preconditions-notes.md`.

- [ ] **Step 0.4: Verify LiteLLM + Supabase callback table provenance**

LiteLLM's `success_callback = ["supabase"]` writes to a table commonly named `request_logs`. **It is not guaranteed to auto-create the table.** Check:

```
https://docs.litellm.ai/docs/observability/supabase_integration
```

If LiteLLM requires the table to exist, copy the `CREATE TABLE` into `migrations/001_init.sql` in Task 2. If LiteLLM auto-creates on first call, note that in `preconditions-notes.md` and verify in Step 10.3.

- [ ] **Step 0.5: Commit preconditions notes**

```bash
cd "/Users/rajat/AntiGravity/Personal finance Agent"
git add tasks/preconditions-notes.md
git commit -m "docs: preconditions verification notes (bout/litellm/spend-logs)"
```
(If this directory isn't yet a git repo, skip the commit — Task 1 creates the actual repo.)

---

## Task 1: Repo bootstrap + Python project init

**Files:**
- Create: `~/projects/personal-finance-agent/.gitignore`, `README.md`, `LICENSE_AUDIT.md`, `pyproject.toml`, `.env.example`, `credentials.example.yaml`, `Makefile`

- [ ] **Step 1.1: Create repo dir and initialize git**

```bash
mkdir -p ~/projects/personal-finance-agent
cd ~/projects/personal-finance-agent
git init
git branch -M main
```

- [ ] **Step 1.2: Write `.gitignore`** (use PRD §13 verbatim, plus these additions — one per line):
  - `launchd/*.local.plist`
  - `~/finance-logs/`
  - `tasks/preconditions-notes.md`
  - `*.local.sql` (Fix 2/3: gitignores `migrations/001_init.local.sql` for the readonly role password and `migrations/003_seed.local.sql` for PII-bearing seed values)

- [ ] **Step 1.3: Write `README.md`** (PRD §13 version)

- [ ] **Step 1.4: Write `LICENSE_AUDIT.md`** (PRD §13 version)

- [ ] **Step 1.5: Write `pyproject.toml`** with the LiteLLM version chosen in Task 0.2

```toml
[project]
name = "personal-finance-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "aiogram>=3.4,<4",
    "apscheduler>=3.10,<4",
    "fastapi>=0.110,<1",
    "uvicorn[standard]>=0.29,<1",
    "litellm==1.83.*",
    "pikepdf>=8",
    "pdfplumber>=0.11",
    "camelot-py>=1.0",                # `[cv]` extra removed in camelot-py 1.0; opencv-python-headless is now a core dep. Verified 2026-04-21 during Task 1 dispatch.
    "rapidfuzz>=3",
    "supabase>=2.5,<3",
    # `bout` intentionally removed — preconditions notes §0.1 found it does NOT parse PDFs
    # (it's a CSV→QIF converter). Week 1 uses pikepdf + pdfplumber directly for the ICICI
    # smoke. A real row-level parser lands in Week 2 and will be added to deps then.
    #
    # `casparser` intentionally removed from Week 1 — it hard-pins pdfminer-six==20240706
    # (verified 2026-04-21 via Task 1 resolver failure) which conflicts with pdfplumber>=0.11.
    # casparser is only needed by Week 3 MF CAS ingestion; added back then, likely with a
    # pdfplumber version constraint adjustment at that point. Log the conflict in lessons.md.
    "mftool>=2.10",
    "pandas>=2.2",
    "openpyxl>=3.1",
    "requests>=2.31",
    "pydantic>=2",
    "pydantic-settings>=2",
    "pyyaml>=6",
    "python-dotenv>=1",               # used by scripts/backup_supabase.py to load .env under launchd
    "google-auth>=2.28",
    "google-api-python-client>=2.120",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "pytest-mock>=3.12",
    "ruff>=0.4",
    "mypy>=1.10",
    "pre-commit>=3.7",
]

[tool.ruff]
line-length = 120
target-version = "py311"
[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP", "N", "SIM"]
ignore = ["E501"]  # line-length enforced separately

[tool.mypy]
python_version = "3.11"
strict_optional = true
warn_unused_ignores = true
warn_redundant_casts = true
check_untyped_defs = true
# Third-party libs without stubs
[[tool.mypy.overrides]]
module = ["bout.*", "casparser.*", "mftool.*", "camelot.*", "pdfplumber.*", "pikepdf.*"]
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 1.6: Write `.env.example`** (PRD §12 version, PLUS one addition):

Add line:
```
SUPABASE_DB_URL=            # full libpq connection string from Supabase dashboard (Supavisor pooler, sslmode=require)
```

- [ ] **Step 1.7: Write `credentials.example.yaml`** (PRD §12 version)

Also add at top of file:
```yaml
# V1: values stored in this file (gitignored).
# V2 upgrade path: values move into macOS Keychain, YAML keeps only
# `password_keyring_ref` + `password_hint` fields and can then be committed
# as documentation. Not implemented in V1.
```

- [ ] **Step 1.8: Write minimal `Makefile`**

```makefile
.PHONY: lint typecheck test

lint:
	ruff check .

typecheck:
	mypy skills scripts app.py

test:
	pytest -v
```

- [ ] **Step 1.9: Write `.pre-commit-config.yaml`**

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.4.10
    hooks:
      - id: ruff
  - repo: local
    hooks:
      - id: no-committed-secrets
        name: No committed secrets
        entry: bash -c 'git diff --cached --name-only | grep -Ev "\.example\." | xargs -I{} grep -lE "(ANTHROPIC|GEMINI|GROQ|SUPABASE_SERVICE_KEY|TELEGRAM_BOT_TOKEN)=[A-Za-z0-9]" {} 2>/dev/null && exit 1 || exit 0'
        language: system
        pass_filenames: false
```

- [ ] **Step 1.10: Create venv and install deps**

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```
Expected: resolver clean; `pre-commit install` writes `.git/hooks/pre-commit`.

- [ ] **Step 1.11: Create directory scaffolding**

```bash
mkdir -p skills/finance/{ingestion/parsers,normalization,categorization,reasoning,nudging,monitoring,privacy,reconciliation,commands,lib,bot}
mkdir -p config credentials migrations scripts launchd tests/golden_fixtures ~/finance-logs ~/finance-backups
touch skills/finance/__init__.py skills/finance/lib/__init__.py skills/finance/monitoring/__init__.py skills/finance/bot/__init__.py skills/finance/ingestion/__init__.py skills/finance/ingestion/parsers/__init__.py tests/__init__.py
touch credentials/.gitkeep
```

- [ ] **Step 1.12: Copy PRD, plan, and project CLAUDE.md into the repo**

```bash
cp "/Users/rajat/AntiGravity/Personal finance Agent/PRD.md" ./PRD.md
mkdir -p tasks
cp "/Users/rajat/AntiGravity/Personal finance Agent/tasks/todo.md" ./tasks/todo.md
cp "/Users/rajat/AntiGravity/Personal finance Agent/CLAUDE.md.draft" ./CLAUDE.md
# Also seed an empty lessons.md so the lessons loop has a target from day 1
touch tasks/lessons.md
```

Verify `CLAUDE.md` loaded into the new repo — Claude Code sessions started from `~/projects/personal-finance-agent/` will pick it up automatically. If later edits to invariants are needed, edit `CLAUDE.md` in the repo (authoritative), not the draft in the planning dir.

- [ ] **Step 1.13: First commit**

```bash
git add .gitignore README.md LICENSE_AUDIT.md CLAUDE.md PRD.md pyproject.toml Makefile .pre-commit-config.yaml .env.example credentials.example.yaml tasks/todo.md tasks/lessons.md
git commit -m "chore: scaffold repo with PRD V2.1, CLAUDE.md invariants, pinned deps, lint/typecheck config"
```

- [ ] **Step 1.14: Populate `.env` and `credentials.yaml` with real values**

```bash
cp .env.example .env
cp credentials.example.yaml credentials.yaml
# Edit both with real values. Both are gitignored.
```

Verify: `git status` shows neither file staged.

- [ ] **Step 1.15: Push private repo**

```bash
gh repo create personal-finance-agent --private --source=. --remote=origin --push
```

---

## Task 2: Supabase schema migration + readonly role + seed

**Files:**
- Create: `migrations/001_init.sql` (committed), `migrations/001_init.local.sql` (gitignored — readonly role password)
- Create: `migrations/002_verify_readonly.sql` (committed)
- Create: `migrations/003_seed.sql` (committed template), `migrations/003_seed.local.sql` (gitignored — real account identifiers)

- [ ] **Step 2.1: Write `migrations/001_init.sql`** (committed — NO PASSWORD)

Structure, top to bottom:

```sql
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

-- [… all CREATE TABLE blocks from PRD §7, in dependency order:
--    users → accounts → categories → transactions → commitments →
--    liabilities → assets → asset_snapshots → goals → income_events →
--    ingestion_log → agent_memory → heartbeat
-- ]

-- Critical transactions columns per PRD §7 + §19.5:
--   txn_time, currency, is_refund, linked_txn_id,
--   pdf_content_hash, source_row_ordinal, parser_version,
--   import_hash (UNIQUE), is_deleted, updated_at

-- Critical ingestion_log CHECK:
--   status IN ('success','skipped_duplicate','possible_duplicate',
--              'failed','needs_review','total_check_failed')

-- [… updated_at trigger function + CREATE TRIGGER on
--    transactions, commitments, liabilities, assets, goals, agent_memory
-- ]
```

If Task 0.4 determined LiteLLM does NOT auto-create `request_logs`, append its CREATE TABLE at the bottom. If it does, skip.

- [ ] **Step 2.1b: Write `migrations/001_init.local.sql`** (gitignored — Fix 2)

```sql
-- migrations/001_init.local.sql — GITIGNORED.
-- Sets the password for finance_agent_readonly. Applied MANUALLY in the
-- Supabase SQL editor right after 001_init.sql. Never committed.
ALTER ROLE finance_agent_readonly WITH PASSWORD '<STRONG_PASSWORD>';
```

Generate a strong password (e.g. `openssl rand -base64 32`), paste it in place of `<STRONG_PASSWORD>`, and ALSO store it in `.env` as `SUPABASE_READONLY_PASSWORD`. Verify the file is gitignored before proceeding:

```bash
git check-ignore -v migrations/001_init.local.sql
```
Expected: prints the matching `.gitignore` line (confirms it's ignored).

- [ ] **Step 2.2: Apply migrations to Supabase (in order)**

Supabase dashboard → SQL Editor → run each in sequence:
1. `migrations/001_init.sql` — schema + role without password
2. `migrations/001_init.local.sql` — sets the readonly role's password (do NOT paste its contents into a shared doc; copy direct from your gitignored file)

Verify: all tables visible under Database → Tables; all triggers under Database → Triggers; `pgcrypto` under Database → Extensions; `finance_agent_readonly` role visible under Database → Roles with login enabled.

- [ ] **Step 2.3: Write `migrations/002_verify_readonly.sql`**

```sql
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
```

- [ ] **Step 2.4: Verify readonly role manually**

Use psql (or Supabase SQL editor "connect as role" if available). Connect as `finance_agent_readonly` with the password from `.env`. Paste `002_verify_readonly.sql`. Confirm: four permission-denied errors + one successful count.

- [ ] **Step 2.5: Write `migrations/003_seed.sql`** (committed — TEMPLATE ONLY, `<angle_bracket>` literals stay in)

This is the reproducible bootstrap template. Real values never go in here. Keeps `ON CONFLICT DO NOTHING` so the restore drill and future env rebuilds work idempotently.

```sql
-- migrations/003_seed.sql — TEMPLATE. Real values live in 003_seed.local.sql (gitignored).
-- Applying this file as-is is a no-op; see 003_seed.local.sql for the actual insert.

INSERT INTO users (id, telegram_handle, role, display_name)
VALUES ('00000000-0000-0000-0000-000000000001', '<rajat_handle>', 'admin', 'Rajat')
ON CONFLICT (telegram_handle) DO NOTHING;

-- Accounts: one row per bank/card/UPI/broker
INSERT INTO accounts (id, user_id, type, institution, identifier, nickname)
VALUES
  ('10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'bank', 'ICICI', '<last4>', 'ICICI Savings'),
  ('10000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', 'bank', 'HDFC',  '<last4>', 'HDFC Savings'),
  ('10000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000001', 'credit_card', 'ICICI', '<last4>', 'ICICI CC'),
  ('10000000-0000-0000-0000-000000000004', '00000000-0000-0000-0000-000000000001', 'credit_card', 'HDFC',  '<last4>', 'HDFC CC'),
  ('10000000-0000-0000-0000-000000000005', '00000000-0000-0000-0000-000000000001', 'credit_card', 'AMEX',  '<last4>', 'AMEX CC'),
  ('10000000-0000-0000-0000-000000000006', '00000000-0000-0000-0000-000000000001', 'upi',    'Paytm',    '<upi_handle_1>', 'Paytm UPI 1'),
  ('10000000-0000-0000-0000-000000000007', '00000000-0000-0000-0000-000000000001', 'upi',    'Paytm',    '<upi_handle_2>', 'Paytm UPI 2'),
  ('10000000-0000-0000-0000-000000000008', '00000000-0000-0000-0000-000000000001', 'broker', 'Zerodha',  '<client_code>', 'Zerodha'),
  ('10000000-0000-0000-0000-000000000009', '00000000-0000-0000-0000-000000000001', 'mf',     'Zfunds',   '<folio>', 'Zfunds MF')
ON CONFLICT (id) DO NOTHING;

-- Personal loan liability
INSERT INTO liabilities (id, user_id, type, original_principal, outstanding_principal, interest_rate, emi_amount, tenure_remaining_months, start_date, lender)
VALUES ('20000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'personal_loan', 0, 0, 10.3, 22000, 0, '<start_date>', '<lender>')
ON CONFLICT (id) DO NOTHING;

-- EMI commitment linked to the liability
INSERT INTO commitments (id, user_id, type, name, amount, frequency, next_due_date, account_id, liability_id)
VALUES ('30000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'emi', 'Personal loan EMI', 22000, 'monthly', '<next_due>', '10000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001')
ON CONFLICT (id) DO NOTHING;
```

Note: the `<angle_bracket>` literals make the template SQL-invalid on purpose — running this file as-is fails early with a clear error, which is safer than accidentally seeding placeholder values into the DB.

- [ ] **Step 2.5b: Write `migrations/003_seed.local.sql`** (gitignored — Fix 3 — REAL VALUES)

Copy `003_seed.sql` to `003_seed.local.sql` and replace every `<angle_bracket>` with Rajat's real values (Telegram handle, account last-4 digits, UPI handles, client codes, folio numbers, loan principals, start/due dates, lender name). Verify it's gitignored:

```bash
git check-ignore -v migrations/003_seed.local.sql
```
Expected: prints the `*.local.sql` line from `.gitignore`.

- [ ] **Step 2.6: Apply `003_seed.local.sql`**

Supabase SQL Editor → paste contents of `003_seed.local.sql` → Run.
Verify: `SELECT count(*) FROM users;` → 1. `SELECT count(*) FROM accounts;` → 9 (or however many accounts you seeded).

- [ ] **Step 2.7: Commit (committed files only — `.local.sql` files must stay out)**

```bash
git add migrations/001_init.sql migrations/002_verify_readonly.sql migrations/003_seed.sql
git status  # confirm NO *.local.sql files staged
git commit -m "feat(db): schema migration + readonly verification + seed template"
```

If `git status` shows any `*.local.sql` as staged or untracked-and-about-to-be-added, stop and re-check `.gitignore`.

---

## Task 3: LiteLLM routing wrapper

**Files:**
- Create: `config/model_routing.yaml`
- Create: `skills/finance/lib/settings.py`
- Create: `skills/finance/lib/llm.py`
- Create: `skills/finance/lib/logging_setup.py`
- Test: `tests/test_llm.py`

- [ ] **Step 3.1: Write `config/model_routing.yaml`**

Copy the YAML block from PRD §6.1 verbatim (8 task entries). **Remove the `# requires --confirm flag to switch` comment** — that references `/model` which is Week 2 scope; don't signpost it here.

- [ ] **Step 3.2: Write `skills/finance/lib/settings.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str
    gemini_api_key: str
    groq_api_key: str
    supabase_url: str
    supabase_service_key: str
    supabase_anon_key: str
    supabase_readonly_password: str
    supabase_db_url: str
    telegram_bot_token: str
    telegram_chat_id_rajat: str
    telegram_alert_bot_token: str
    telegram_alert_chat_id: str
    healthcheck_url: str
    finance_inbox_path: str = "/Users/rajat/finance-inbox"
    finance_backup_path: str = "/Users/rajat/finance-backups"
    finance_log_path: str = "/Users/rajat/finance-logs"
    timezone: str = "Asia/Kolkata"

settings = Settings()
```

- [ ] **Step 3.3: Write `skills/finance/lib/logging_setup.py`**

```python
from __future__ import annotations
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from skills.finance.lib.settings import settings

_LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"

def configure_logging(level: int = logging.INFO) -> None:
    log_dir = Path(settings.finance_log_path)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    # Clear any pre-configured handlers (e.g. from libraries)
    root.handlers.clear()

    file_handler = RotatingFileHandler(
        log_dir / "pfa.log", maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(file_handler)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(stdout_handler)
```

- [ ] **Step 3.4: Write failing test `tests/test_llm.py`**

```python
from unittest.mock import patch, MagicMock

def test_llm_routes_pdf_extraction_to_gemini_flash():
    from skills.finance.lib.llm import llm
    with patch("skills.finance.lib.llm.litellm.completion") as mock_completion:
        mock_completion.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="ok"))])
        llm("pdf_extraction", prompt="hello")
        _, kwargs = mock_completion.call_args
        assert kwargs["model"] == "gemini/gemini-2.5-flash"
        assert "anthropic/claude-sonnet-4-6" in kwargs["fallbacks"]
        assert kwargs["metadata"]["task"] == "pdf_extraction"

def test_llm_unknown_task_raises():
    from skills.finance.lib.llm import llm
    import pytest
    with pytest.raises(KeyError):
        llm("no_such_task", prompt="x")
```

- [ ] **Step 3.5: Run test, verify it fails**

```bash
pytest tests/test_llm.py -v
```
Expected: ModuleNotFoundError.

- [ ] **Step 3.6: Write `skills/finance/lib/llm.py`**

No `images` param (Week 2 will add when the PDF vision pipeline actually needs it). No `reload_routing()` (Week 2's `/model` command will add it).

```python
from __future__ import annotations
import os
from pathlib import Path
import litellm
import yaml
from skills.finance.lib.settings import settings

os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)
os.environ.setdefault("GEMINI_API_KEY", settings.gemini_api_key)
os.environ.setdefault("GROQ_API_KEY", settings.groq_api_key)

litellm.success_callback = ["supabase"]
os.environ.setdefault("SUPABASE_URL", settings.supabase_url)
os.environ.setdefault("SUPABASE_KEY", settings.supabase_service_key)

_ROUTING_PATH = Path(__file__).resolve().parents[3] / "config" / "model_routing.yaml"

def _load_routing() -> dict:
    with open(_ROUTING_PATH) as f:
        return yaml.safe_load(f)

ROUTING = _load_routing()

def llm(task: str, prompt: str, system: str | None = None):
    """Single entry point for all LLM calls. Routes by task name via model_routing.yaml."""
    if task not in ROUTING:
        raise KeyError(f"Unknown task '{task}'. Known: {list(ROUTING.keys())}")
    cfg = ROUTING[task]
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return litellm.completion(
        model=cfg["model"],
        messages=messages,
        fallbacks=cfg.get("fallbacks", []),
        metadata={"task": task},
    )
```

- [ ] **Step 3.7: Run test, verify pass**

```bash
pytest tests/test_llm.py -v
```
Expected: 2 passed.

- [ ] **Step 3.8: Live 3-provider smoke test**

Create `scripts/smoke_llm.py`:

```python
from skills.finance.lib.llm import llm
for task in ["pdf_extraction", "merchant_categorization", "sql_agent"]:
    resp = llm(task, prompt="Reply with exactly the word: OK")
    print(task, "->", resp.choices[0].message.content.strip())
```

Run: `python scripts/smoke_llm.py`.
Expected: each task prints "OK" (or very close).

Then verify the LiteLLM → Supabase cost logging wrote rows. In Supabase SQL editor:
```sql
SELECT count(*) FROM request_logs;
```
Expected: > 0. If the table doesn't exist, return to Task 0.4 — LiteLLM isn't auto-creating it on your version; apply the CREATE TABLE migration and re-run.

- [ ] **Step 3.9: Verify fallback (manual)**

Temporarily edit `config/model_routing.yaml`: set `pdf_extraction.model: gemini/nonexistent-xyz`. Re-run smoke. Expected: still prints "OK" (from the Claude Sonnet fallback). Revert the edit.

Delete the smoke script: `rm scripts/smoke_llm.py`.

- [ ] **Step 3.10: Commit**

```bash
git add config/model_routing.yaml skills/finance/lib/settings.py skills/finance/lib/llm.py skills/finance/lib/logging_setup.py tests/test_llm.py
git commit -m "feat(llm): LiteLLM routing wrapper, pydantic settings, rotating log handler"
```

---

## Task 4a: Hashing library (pure logic, no I/O)

**Files:**
- Create: `skills/finance/lib/hashing.py`
- Test: `tests/test_hashing.py`

- [ ] **Step 4a.1: Write failing test `tests/test_hashing.py`**

```python
from datetime import datetime, date, timezone
from skills.finance.lib.hashing import import_hash_time_bearing, import_hash_pdf

PV = "test_parser@1.0.0"

def test_time_bearing_hash_differs_when_time_differs():
    t1 = datetime(2026, 4, 21, 13, 5, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 21, 21, 10, tzinfo=timezone.utc)
    h1 = import_hash_time_bearing("acct-1", t1, 350.00, "swiggy", PV)
    h2 = import_hash_time_bearing("acct-1", t2, 350.00, "swiggy", PV)
    assert h1 != h2

def test_time_bearing_hash_stable_across_identical_inputs():
    t = datetime(2026, 4, 21, 13, 5, tzinfo=timezone.utc)
    assert import_hash_time_bearing("acct-1", t, 350.00, "swiggy", PV) == \
           import_hash_time_bearing("acct-1", t, 350.00, "swiggy", PV)

def test_time_bearing_hash_rejects_naive_datetime():
    import pytest
    with pytest.raises(ValueError):
        import_hash_time_bearing("acct-1", datetime(2026, 4, 21, 13, 5), 350.00, "swiggy", PV)

def test_pdf_hash_disambiguates_by_row_ordinal():
    """Two ₹350 Swiggy orders in the same PDF on the same day — ordinal must save us."""
    base = dict(account_id="acct-1", txn_date=date(2026, 4, 21), amount=350.00,
                normalized_description="swiggy", pdf_content_hash="pdf-abc", parser_version=PV)
    h_row7 = import_hash_pdf(source_row_ordinal=7, **base)
    h_row19 = import_hash_pdf(source_row_ordinal=19, **base)
    assert h_row7 != h_row19

def test_pdf_hash_stable_when_same_pdf_reparsed():
    kwargs = dict(account_id="acct-1", txn_date=date(2026, 4, 21), amount=350.00,
                  normalized_description="swiggy", pdf_content_hash="pdf-abc",
                  source_row_ordinal=7, parser_version=PV)
    assert import_hash_pdf(**kwargs) == import_hash_pdf(**kwargs)

def test_pdf_hash_changes_on_parser_version_bump():
    base = dict(account_id="acct-1", txn_date=date(2026, 4, 21), amount=350.00,
                normalized_description="swiggy", pdf_content_hash="pdf-abc",
                source_row_ordinal=7)
    h_v1 = import_hash_pdf(parser_version="bout@0.4.2", **base)
    h_v2 = import_hash_pdf(parser_version="bout@0.5.0", **base)
    assert h_v1 != h_v2  # parser bump must force fresh ingest

def test_pdf_hash_differs_across_different_pdfs():
    base = dict(account_id="acct-1", txn_date=date(2026, 4, 21), amount=350.00,
                normalized_description="swiggy", source_row_ordinal=7, parser_version=PV)
    h_a = import_hash_pdf(pdf_content_hash="pdf-aaa", **base)
    h_b = import_hash_pdf(pdf_content_hash="pdf-bbb", **base)
    assert h_a != h_b  # documents rolling-overlap behavior; resolved by fuzzy pass + /dedup
```

- [ ] **Step 4a.2: Run test, verify it fails**

```bash
pytest tests/test_hashing.py -v
```
Expected: import error.

- [ ] **Step 4a.3: Write `skills/finance/lib/hashing.py`**

```python
from __future__ import annotations
import hashlib
from datetime import date, datetime
from decimal import Decimal

def _normalize_amount(amount: float | Decimal) -> str:
    return f"{Decimal(str(amount)).quantize(Decimal('0.01'))}"

def _normalize_desc(s: str) -> str:
    return s.strip().lower()

def import_hash_time_bearing(
    account_id: str,
    txn_time: datetime,
    amount: float | Decimal,
    normalized_description: str,
    parser_version: str,
) -> str:
    """Mode A — sources that provide exact timestamps (SMS, Gmail txn emails).

    source_ref is intentionally NOT included so the same transaction observed via
    multiple time-bearing channels dedups to a single row.
    """
    if txn_time.tzinfo is None:
        raise ValueError("txn_time must be timezone-aware")
    parts = [account_id, txn_time.isoformat(), _normalize_amount(amount),
             _normalize_desc(normalized_description), parser_version]
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()

def import_hash_pdf(
    account_id: str,
    txn_date: date,
    amount: float | Decimal,
    normalized_description: str,
    pdf_content_hash: str,
    source_row_ordinal: int,
    parser_version: str,
) -> str:
    """Mode B — PDF-derived rows (CC statements, MF CAS, bank PDFs).

    - source_row_ordinal disambiguates intra-PDF same-day same-amount rows.
    - pdf_content_hash scopes uniqueness to the specific source document.
    - parser_version makes parser upgrades observable — re-parse with a bumped
      version produces fresh hashes rather than silently merging.
    - Rolling-statement overlap (same real txn in two different PDFs) is a known
      tradeoff handled by the secondary fuzzy pass (see PRD §7).
    """
    parts = [account_id, txn_date.isoformat(), _normalize_amount(amount),
             _normalize_desc(normalized_description), pdf_content_hash,
             str(source_row_ordinal), parser_version]
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()
```

- [ ] **Step 4a.4: Run test, verify pass**

```bash
pytest tests/test_hashing.py -v
```
Expected: 7 passed.

- [ ] **Step 4a.5: Commit**

```bash
git add skills/finance/lib/hashing.py tests/test_hashing.py
git commit -m "feat(lib): two-mode import_hash with parser_version (Option Y per PRD §19.5)"
```

---

## Task 4b: DB client + `adb()` async wrapper

**Files:**
- Create: `skills/finance/lib/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 4b.1: Write failing test `tests/test_db.py`**

```python
import asyncio
from unittest.mock import MagicMock

def test_service_client_exposes_handle():
    from skills.finance.lib.db import service_client
    assert service_client() is not None

def test_adb_runs_sync_fn_in_thread():
    """adb() must be the single enforcement point for async-to-sync bridging."""
    from skills.finance.lib.db import adb
    called_with = {}
    def sync_fn(x, y=None):
        called_with["x"] = x
        called_with["y"] = y
        return "result"
    out = asyncio.run(adb(sync_fn, 42, y="hi"))
    assert out == "result"
    assert called_with == {"x": 42, "y": "hi"}
```

Note: the `readonly_client` function and its "distinct clients" test have been intentionally dropped from Week 1 per plan review — the real readonly path (psycopg + SUPABASE_READONLY_PASSWORD) lands in Week 5 when the SQL agent is built.

- [ ] **Step 4b.2: Run test, verify it fails**

```bash
pytest tests/test_db.py -v
```
Expected: import error.

- [ ] **Step 4b.3: Write `skills/finance/lib/db.py`**

```python
"""
Supabase client module.

RULE: the Supabase Python client is SYNCHRONOUS. Any async handler that needs
to call it MUST go through `adb()`, which runs the sync call in a thread so
the event loop is never blocked. This is the single enforcement point — don't
call `.table(...).execute()` directly from an async context; wrap it.

    # Correct — from an async aiogram handler:
    rows = await adb(service_client().table("users").select("*").execute)

    # Wrong — blocks the aiogram poll loop:
    rows = service_client().table("users").select("*").execute()

The SQL-agent readonly connection (psycopg + SUPABASE_READONLY_PASSWORD) is
deliberately NOT in this module for Week 1. It lands in Week 5 when the SQL
agent is built, with the real enforcement boundary it represents.
"""
from __future__ import annotations
import asyncio
from functools import lru_cache
from typing import Any, Callable
from supabase import Client, create_client
from skills.finance.lib.settings import settings

@lru_cache(maxsize=1)
def service_client() -> Client:
    """Full-write Supabase client. Used by ingestion pipeline and heartbeat writer."""
    return create_client(settings.supabase_url, settings.supabase_service_key)

async def adb(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a sync Supabase call (or any sync callable) in a worker thread.

    All async handlers MUST route Supabase access through this helper.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)
```

- [ ] **Step 4b.4: Run test, verify pass**

```bash
pytest tests/test_db.py -v
```
Expected: 2 passed.

- [ ] **Step 4b.5: Live read smoke test (manual)**

```python
# python REPL
from skills.finance.lib.db import service_client
c = service_client()
print(c.table("users").select("*").execute().data)  # expect the seeded Rajat row
```

- [ ] **Step 4b.6: Commit**

```bash
git add skills/finance/lib/db.py tests/test_db.py
git commit -m "feat(lib): Supabase service client + adb() async wrapper (single choke point)"
```

---

## Task 5: Telegram bot skeleton with heartbeat middleware

**Files:**
- Create: `skills/finance/bot/main.py`
- Test: `tests/test_bot_middleware.py` (Fix 1: regression test for `.execute()` termination)

- [ ] **Step 5.1: Write `skills/finance/bot/main.py`**

Heartbeat bumped from inside aiogram via middleware — any successfully processed message writes the `telegram_bot` heartbeat. This fixes the "scheduler heartbeat renamed" bug from plan review.

**Fix 1 critical detail:** the heartbeat write MUST be a `lambda` that terminates the builder chain with `.execute()`. supabase-py v2's `.insert(payload)` returns a builder; without `.execute()` the write silently never happens. Matches CLAUDE.md invariant #2.

```python
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject
from skills.finance.lib.db import service_client, adb
from skills.finance.lib.settings import settings

logger = logging.getLogger(__name__)

bot = Bot(token=settings.telegram_bot_token)
dp = Dispatcher()

class BotHeartbeatMiddleware(BaseMiddleware):
    """Every successfully processed message bumps the telegram_bot heartbeat row.
    This means the heartbeat reflects actual bot health, not scheduler health."""
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        result = await handler(event, data)
        try:
            payload = {
                "component": "telegram_bot",
                "status": "ok",
                "last_ping": datetime.now(tz=timezone.utc).isoformat(),
            }
            # IMPORTANT (CLAUDE.md invariant #2): chain must end at .execute().
            # A bare .insert(payload) returns a builder and silently no-ops.
            await adb(lambda: service_client().table("heartbeat").insert(payload).execute())
        except Exception as e:
            logger.warning("bot heartbeat write failed: %s", e)
        return result

dp.message.middleware(BotHeartbeatMiddleware())

def _is_rajat(message: Message) -> bool:
    return str(message.chat.id) == str(settings.telegram_chat_id_rajat)

@dp.message(Command("ping"))
async def ping(message: Message) -> None:
    if not _is_rajat(message):
        return
    await message.answer("pong")
```

Note: no `if __name__ == "__main__":` block. The bot is started from `app.py` in Task 7 as one of the concurrent subsystems.

- [ ] **Step 5.1b: Write regression test `tests/test_bot_middleware.py`** (Fix 1)

This test exists specifically to prevent the "forgotten `.execute()`" regression. If a future edit drops `.execute()` from the middleware, this test fails loudly.

```python
import asyncio
from unittest.mock import MagicMock, patch

def test_heartbeat_middleware_calls_execute():
    """Regression test for Fix 1: supabase-py v2's .insert() returns a builder.
    Without a terminating .execute(), the row is never written.
    """
    from skills.finance.bot.main import BotHeartbeatMiddleware

    execute_mock = MagicMock(return_value=MagicMock(data=[]))
    insert_mock = MagicMock(return_value=MagicMock(execute=execute_mock))
    table_mock = MagicMock(return_value=MagicMock(insert=insert_mock))
    fake_client = MagicMock(table=table_mock)

    async def fake_handler(event, data):
        return "handler_result"

    middleware = BotHeartbeatMiddleware()

    with patch("skills.finance.bot.main.service_client", return_value=fake_client):
        result = asyncio.run(middleware(fake_handler, MagicMock(), {}))

    assert result == "handler_result", "middleware must return handler result unchanged"
    table_mock.assert_called_once_with("heartbeat")
    insert_mock.assert_called_once()
    # The critical assertion — if this fails, the heartbeat is silently no-op'ing
    execute_mock.assert_called_once()
```

- [ ] **Step 5.2: Minimal standalone run check** (pre-orchestrator)

Create a temporary `run_bot.py` at repo root (will be deleted):

```python
import asyncio
from skills.finance.bot.main import bot, dp
from skills.finance.lib.logging_setup import configure_logging

async def main():
    configure_logging()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
```

Run: `python run_bot.py`. Send `/ping` on Telegram → expect `pong`. Also check Supabase → `heartbeat` → a row with `component='telegram_bot'` appeared right after the `/ping`.

Stop with Ctrl-C. Delete the file: `rm run_bot.py`.

- [ ] **Step 5.3: Run regression test**

```bash
pytest tests/test_bot_middleware.py -v
```
Expected: 1 passed. This test guarantees future edits can't silently drop `.execute()`.

- [ ] **Step 5.4: Commit**

```bash
git add skills/finance/bot/main.py tests/test_bot_middleware.py
git commit -m "feat(bot): aiogram bot with heartbeat middleware + /ping + .execute() regression test"
```

---

## Task 6: Monitoring — alerts, internal heartbeat, `/health` (all async)

**Files:**
- Create: `skills/finance/monitoring/alerts.py`
- Create: `skills/finance/monitoring/heartbeat.py`
- Create: `skills/finance/monitoring/health.py`
- Test: `tests/test_heartbeat.py`

- [ ] **Step 6.1: Write `skills/finance/monitoring/alerts.py`** (async-only — no sync shim)

```python
from __future__ import annotations
import logging
from aiogram import Bot
from skills.finance.lib.settings import settings

logger = logging.getLogger(__name__)
_alert_bot = Bot(token=settings.telegram_alert_bot_token)

async def send_alert(text: str) -> None:
    """Send an alert via the secondary Telegram bot. Never raises."""
    try:
        await _alert_bot.send_message(chat_id=settings.telegram_alert_chat_id, text=f"⚠️ {text}")
    except Exception as e:
        logger.exception("alert dispatch failed: %s", e)
```

- [ ] **Step 6.2: Write failing test `tests/test_heartbeat.py`**

```python
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

def test_stale_components_flagged_when_exceeds_threshold():
    from skills.finance.monitoring.heartbeat import find_stale_components
    now = datetime(2026, 4, 21, 14, 0, tzinfo=timezone.utc)
    latest = {
        "gmail_scanner": {"component": "gmail_scanner", "last_ping": (now - timedelta(minutes=10)).isoformat()},
        "morning_brief": {"component": "morning_brief", "last_ping": (now - timedelta(minutes=45)).isoformat()},
        "telegram_bot":  {"component": "telegram_bot",  "last_ping": (now - timedelta(minutes=5)).isoformat()},
    }
    stale = find_stale_components(latest.values(), now=now, threshold_minutes=30)
    assert stale == ["morning_brief"]
```

- [ ] **Step 6.3: Run test, verify it fails**

```bash
pytest tests/test_heartbeat.py -v
```
Expected: import error.

- [ ] **Step 6.4: Write `skills/finance/monitoring/heartbeat.py`**

All scheduler-facing jobs are `async def` so they run on the AsyncIOScheduler's main loop. No `asyncio.run()` inside them. This fixes the "asyncio.run inside running loop" bug from plan review.

```python
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Iterable
import logging
import requests
from skills.finance.lib.db import service_client, adb
from skills.finance.lib.settings import settings
from skills.finance.monitoring.alerts import send_alert

logger = logging.getLogger(__name__)
HEARTBEAT_STALE_MINUTES = 30

def find_stale_components(
    latest_rows: Iterable[dict],
    *,
    now: datetime,
    threshold_minutes: int = HEARTBEAT_STALE_MINUTES,
) -> list[str]:
    cutoff = now - timedelta(minutes=threshold_minutes)
    stale: list[str] = []
    for r in latest_rows:
        last = datetime.fromisoformat(r["last_ping"])
        if last < cutoff:
            stale.append(r["component"])
    return stale

async def check_stale_components_job() -> None:
    """Async APScheduler job. Alerts on any component whose most-recent heartbeat is stale."""
    rows = await adb(
        service_client().table("heartbeat").select("component,last_ping").order("last_ping", desc=True).limit(200).execute
    )
    latest: dict[str, dict] = {}
    for r in rows.data:
        latest.setdefault(r["component"], r)
    stale = find_stale_components(latest.values(), now=datetime.now(tz=timezone.utc))
    if stale:
        await send_alert(f"Stale heartbeat for: {', '.join(stale)}")

async def ping_external_watchdog_job() -> None:
    """Async APScheduler job. If this stops firing, healthchecks.io alerts us.

    Network call is sync (requests); wrapped in to_thread to avoid blocking."""
    import asyncio
    try:
        await asyncio.to_thread(lambda: requests.get(settings.healthcheck_url, timeout=10).raise_for_status())
    except Exception as e:
        # Logging-only: if we can't reach healthchecks.io, there's no upstream to alert.
        logger.warning("external watchdog ping failed: %s", e)
```

- [ ] **Step 6.5: Run test, verify pass**

```bash
pytest tests/test_heartbeat.py -v
```
Expected: 1 passed.

- [ ] **Step 6.6: Write `skills/finance/monitoring/health.py`**

```python
from __future__ import annotations
from fastapi import FastAPI
from skills.finance.lib.db import service_client, adb

app = FastAPI()

@app.get("/health")
async def health() -> dict:
    try:
        await adb(service_client().table("users").select("id").limit(1).execute)
        return {"status": "ok", "db": "reachable"}
    except Exception as e:
        return {"status": "degraded", "db": f"error: {e}"}
```

- [ ] **Step 6.7: Commit**

```bash
git add skills/finance/monitoring/ tests/test_heartbeat.py
git commit -m "feat(monitoring): async alerts, async heartbeat jobs, /health endpoint"
```

---

## Task 7: App orchestrator (bot + scheduler + FastAPI, with graceful shutdown)

**Files:**
- Create: `app.py`

Graceful SIGTERM/SIGINT handler is included here (not a separate task — it's 15 lines).

- [ ] **Step 7.1: Write `app.py`**

```python
"""Personal Finance Agent — top-level orchestrator.

Runs three concurrent subsystems in one Python process:
1. aiogram bot polling      (skills.finance.bot.main)
2. APScheduler AsyncIO jobs (heartbeat + external watchdog — daily backup is a SEPARATE launchd job, not wired here)
3. FastAPI /health endpoint (skills.finance.monitoring.health)

Graceful shutdown:
- SIGTERM / SIGINT triggers a clean shutdown: cancels the scheduler, closes
  the aiogram session, stops uvicorn. launchctl stop invokes SIGTERM.
- The launchd backup job runs in its own process, independent of this app,
  so shutdown here does not interrupt a backup in flight.
"""
from __future__ import annotations
import asyncio
import logging
import signal
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import uvicorn
from skills.finance.bot.main import bot, dp
from skills.finance.lib.logging_setup import configure_logging
from skills.finance.monitoring.health import app as fastapi_app
from skills.finance.monitoring.heartbeat import check_stale_components_job, ping_external_watchdog_job

logger = logging.getLogger("pfa.app")

def _build_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="Asia/Kolkata")
    sched.add_job(ping_external_watchdog_job, IntervalTrigger(minutes=15), id="external_watchdog")
    sched.add_job(check_stale_components_job,  IntervalTrigger(minutes=15), id="stale_check")
    # Note: we do NOT register a "telegram_bot" heartbeat writer on the scheduler.
    # The bot itself writes its heartbeat via middleware on every processed message
    # (see skills/finance/bot/main.py). This means the heartbeat reflects bot health,
    # not scheduler health.
    # Note: daily pg_dump backup is NOT scheduled here. It runs as a separate
    # launchd job (com.rajat.pfa.backup.plist) so it survives app restarts and
    # cannot block the async event loop.
    return sched

async def _run_http(stop_event: asyncio.Event) -> None:
    config = uvicorn.Config(fastapi_app, host="127.0.0.1", port=8765, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await stop_event.wait()
    server.should_exit = True
    await task

async def _run_bot(stop_event: asyncio.Event) -> None:
    polling = asyncio.create_task(dp.start_polling(bot))
    await stop_event.wait()
    await dp.stop_polling()
    await polling
    await bot.session.close()

async def main() -> None:
    configure_logging()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    sched = _build_scheduler()
    sched.start()
    logger.info("scheduler started; jobs=%s", [j.id for j in sched.get_jobs()])

    try:
        await asyncio.gather(_run_bot(stop_event), _run_http(stop_event))
    finally:
        logger.info("shutting down: cancelling scheduler")
        sched.shutdown(wait=False)
        logger.info("shutdown complete")

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 7.2: Run the app end-to-end**

```bash
python app.py
```
Expected: uvicorn log "running on http://127.0.0.1:8765", scheduler logs listing both job IDs, aiogram polling startup. No tracebacks.

- [ ] **Step 7.3: Manual verification round**

Second terminal:
```bash
curl http://127.0.0.1:8765/health
```
Expected: `{"status":"ok","db":"reachable"}`.

Telegram (main bot): send `/ping` → expect `pong`. Supabase → `heartbeat` table → a row with `component='telegram_bot'` within a second.

- [ ] **Step 7.4: Test graceful shutdown**

Send SIGTERM from another terminal:
```bash
kill -TERM $(pgrep -f "python app.py")
```
Expected log sequence: "shutting down: cancelling scheduler" → "shutdown complete". Exit code 0. No stray asyncio "task was destroyed" warnings.

- [ ] **Step 7.5: Test external heartbeat alert path**

Restart `python app.py`. In `app.py`, temporarily comment out the `external_watchdog` `add_job` line. Restart. Wait 30 min (15 min period + 15 min grace). Expected: healthchecks.io fires Telegram alert via secondary bot. Uncomment; restart; confirm next ping resumes.

- [ ] **Step 7.6: Test internal stale-component alert**

With the app running: in Supabase SQL editor, `INSERT INTO heartbeat (component, status, last_ping) VALUES ('gmail_scanner', 'ok', now() - interval '45 minutes');`. Within 15 minutes, `check_stale_components_job` should fire and secondary bot receives: `⚠️ Stale heartbeat for: gmail_scanner`.

- [ ] **Step 7.7: Commit**

```bash
git add app.py
git commit -m "feat(app): orchestrator with graceful SIGTERM shutdown; heartbeat via bot middleware"
```

---

## Task 8: Backup — standalone script invoked by launchd (NOT APScheduler)

**Files:**
- Create: `scripts/backup_supabase.py`
- Create: `scripts/restore_drill.py`
- Create: `launchd/com.rajat.pfa.backup.plist`

Backup lives outside the async app so (a) it can't block the event loop, (b) it keeps running if `app.py` is down, (c) it doesn't need to be considered in graceful-shutdown logic.

- [ ] **Step 8.1: Write `scripts/backup_supabase.py`**

Uses `SUPABASE_DB_URL` (the full libpq string from the Supavisor pooler, fetched in Preconditions). No string assembly.

```python
"""Daily Supabase backup. Invoked by launchd/com.rajat.pfa.backup.plist.

Runs as an independent process — does not rely on app.py being up."""
from __future__ import annotations
import gzip
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Avoid importing skills.finance.lib.settings here — that pulls in supabase,
# pydantic, etc. This script should be minimal and fast-failing.
import os
from dotenv import load_dotenv  # pip install python-dotenv — add to pyproject if not present

def main() -> int:
    # Explicitly load .env from repo root so launchd runs can find it.
    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(repo_root / ".env")

    db_url = os.environ.get("SUPABASE_DB_URL")
    backup_path = Path(os.environ.get("FINANCE_BACKUP_PATH", Path.home() / "finance-backups"))
    if not db_url:
        print("SUPABASE_DB_URL not set", file=sys.stderr)
        return 2
    backup_path.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw = backup_path / f"supabase_{stamp}.sql"
    gz = raw.with_suffix(".sql.gz")

    subprocess.run(["pg_dump", "--no-owner", "--no-acl", "-f", str(raw), db_url], check=True)
    with open(raw, "rb") as fi, gzip.open(gz, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    raw.unlink()
    print(f"backup written: {gz}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

Add `python-dotenv` to `pyproject.toml` deps if not already present.

- [ ] **Step 8.2: Run manual backup; verify**

```bash
python scripts/backup_supabase.py
ls -la ~/finance-backups/
```
Expected: one file, non-zero size.

- [ ] **Step 8.3: Write `scripts/restore_drill.py`**

```python
"""Restore a .sql.gz Supabase backup into a scratch target for verification.

Scratch target can be (a) a throwaway Supabase project, or (b) a local Postgres db.
Usage: python scripts/restore_drill.py <backup.sql.gz> <scratch_db_url>
"""
from __future__ import annotations
import gzip
import subprocess
import sys
from pathlib import Path

def main() -> int:
    if len(sys.argv) != 3:
        print("usage: restore_drill.py <backup.sql.gz> <scratch_db_url>", file=sys.stderr)
        return 2
    backup_gz = Path(sys.argv[1])
    target = sys.argv[2]

    raw = backup_gz.with_suffix("")
    with gzip.open(backup_gz, "rb") as fi, open(raw, "wb") as fo:
        fo.write(fi.read())
    try:
        subprocess.run(["psql", target, "-f", str(raw)], check=True)
    finally:
        raw.unlink(missing_ok=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 8.4: Restore drill (one-time verification)**

```bash
createdb pfa_restore_drill
python scripts/restore_drill.py ~/finance-backups/supabase_<stamp>.sql.gz "postgresql://localhost/pfa_restore_drill"
psql pfa_restore_drill -c "\dt"
psql pfa_restore_drill -c "SELECT count(*) FROM users;"
dropdb pfa_restore_drill
```
Expected: all tables present; users count matches seed.

- [ ] **Step 8.5: Write `launchd/com.rajat.pfa.backup.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.rajat.pfa.backup</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/rajat/projects/personal-finance-agent/.venv/bin/python</string>
        <string>/Users/rajat/projects/personal-finance-agent/scripts/backup_supabase.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/rajat/projects/personal-finance-agent</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>3</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/rajat/finance-logs/backup.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/rajat/finance-logs/backup.stderr.log</string>
</dict>
</plist>
```

- [ ] **Step 8.6: Install and enable the launchd backup job**

```bash
cp launchd/com.rajat.pfa.backup.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rajat.pfa.backup.plist
launchctl enable gui/$(id -u)/com.rajat.pfa.backup
```

Force a run now to verify wiring:
```bash
launchctl kickstart gui/$(id -u)/com.rajat.pfa.backup
```
Expected: fresh file in `~/finance-backups/`, log at `~/finance-logs/backup.stdout.log` shows "backup written: ...".

- [ ] **Step 8.7: Commit**

```bash
git add scripts/ launchd/com.rajat.pfa.backup.plist pyproject.toml
git commit -m "feat(backup): standalone pg_dump + restore drill + launchd schedule (out of app.py)"
```

---

## Task 9: ICICI statement smoke test (PDF Decryption + Text Extraction)

**Files:**
- Create: `tests/test_pdf_smoke.py`

**IMPORTANT:** We determined `bout` does not support ICICI PDFs. A full row-level parser is pushed to Week 2. For Week 1, we just need to prove we can decrypt and extract text from an ICICI PDF.

- [ ] **Step 9.1: Write `tests/test_pdf_smoke.py`**

```python
import pytest
from pathlib import Path
import pikepdf
import pdfplumber
import os

FIXTURE = Path(__file__).parent / "golden_fixtures" / "icici_sample.pdf"

@pytest.mark.skipif(not FIXTURE.exists(), reason="real ICICI fixture not present")
def test_pdf_decrypt_and_extract():
    # Replace with Rajat's actual password if needed
    password = ""

    # Decrypt
    try:
        pdf_file = pikepdf.open(FIXTURE, password=password)
        pdf_file.save("/tmp/unlocked.pdf")
    except Exception as e:
        pytest.fail(f"Could not decrypt PDF: {e}")

    # Extract text
    with pdfplumber.open("/tmp/unlocked.pdf") as pl_pdf:
        pages = pl_pdf.pages
        assert len(pages) > 0, "PDF has no pages"
        text = pages[0].extract_text()
        assert text, "Could not extract text from the first page"
        # Smoke check for typical ICICI PDF strings
        assert "Date" in text or "Amount" in text or "ICICI" in text or "INR" in text, "Text extraction looks like garbage"
```

- [ ] **Step 9.2: Drop one real ICICI statement into `tests/golden_fixtures/icici_sample.pdf`**

Rajat copies a recent ICICI statement. The file is gitignored (`*.pdf` in `.gitignore`).

- [ ] **Step 9.3: Run test**

```bash
pytest tests/test_pdf_smoke.py -v
```
Expected: The test passes, proving we can decrypt and read text.

- [ ] **Step 9.4: Commit**

```bash
git add tests/test_pdf_smoke.py
git commit -m "test(ingestion): smoke test PDF decryption and text extraction (Week 1)"
```

---

## Task 10: launchd supervisor plist for `app.py`

Set up last, per plan review — you don't want launchd auto-restarting the app between every iteration during Tasks 1–9. Run `python app.py` by hand during build; switch to launchd supervision as the last step.

**Files:**
- Create: `launchd/com.rajat.pfa.app.plist`

- [ ] **Step 10.1: Write `launchd/com.rajat.pfa.app.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.rajat.pfa.app</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/rajat/projects/personal-finance-agent/.venv/bin/python</string>
        <string>/Users/rajat/projects/personal-finance-agent/app.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/rajat/projects/personal-finance-agent</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>/Users/rajat/finance-logs/app.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/rajat/finance-logs/app.stderr.log</string>
</dict>
</plist>
```

- [ ] **Step 10.2: Install and enable**

```bash
# First stop any hand-started app.py process
pgrep -f "python app.py" | xargs -r kill

cp launchd/com.rajat.pfa.app.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rajat.pfa.app.plist
launchctl enable gui/$(id -u)/com.rajat.pfa.app
```

- [ ] **Step 10.3: Verify supervisor restarts app on crash**

```bash
# Confirm running
launchctl print gui/$(id -u)/com.rajat.pfa.app | grep state

# Kill the process; launchd should restart it within ThrottleInterval (10s)
pgrep -f "python /Users/rajat/projects/personal-finance-agent/app.py" | xargs kill
sleep 15
pgrep -f "python /Users/rajat/projects/personal-finance-agent/app.py"  # should print a new PID
```
Expected: a new PID printed after the sleep. If empty, check `~/finance-logs/app.stderr.log` for startup errors.

- [ ] **Step 10.4: Commit**

```bash
git add launchd/com.rajat.pfa.app.plist
git commit -m "feat(ops): launchd supervisor for app.py with KeepAlive + restart throttle"
```

---

## Task 11: Week 1 exit verification

No new code. Confirms every Week 1 deliverable from PRD §11 + §16 success criteria is green before Week 2 begins.

- [ ] **Step 11.1: Full test suite clean**

```bash
make lint && make typecheck && make test
```
All pass.

- [ ] **Step 11.2: `app.py` runs cleanly under launchd**

```bash
launchctl print gui/$(id -u)/com.rajat.pfa.app | grep -E "(state|last exit code)"
```
Expected: `state = running`, last exit code 0 (or no last exit code if it hasn't crashed).

`curl http://127.0.0.1:8765/health` → `{"status":"ok","db":"reachable"}`.
Telegram `/ping` → `pong`.

- [ ] **Step 11.3: Confirm each Week 1 success criterion**

Walk PRD §16 and check under Week 1 scope:
- [ ] Supabase schema matches §7 (every column present, including `parser_version`, `pdf_content_hash`, `source_row_ordinal`)
- [ ] `finance_agent_readonly` verified non-mutating (Task 2.4 passed)
- [ ] LiteLLM: all 3 providers called successfully + fallback verified (Task 3.8/3.9)
- [ ] LiteLLM → Supabase cost logging active. Use a soft check that gives a clear error if the table wasn't auto-created:
  ```sql
  SELECT CASE WHEN to_regclass('public.request_logs') IS NULL
              THEN 'request_logs missing — revisit Task 0.4 + 2.1'
              ELSE 'rows: ' || (SELECT count(*)::text FROM request_logs)
         END;
  ```
  Expected: `rows: N` with N > 0.
- [ ] External healthchecks.io alert fired on manual cron-pause (Task 7.5)
- [ ] Internal stale-component alert fired on manual stale-row insert (Task 7.6)
- [ ] launchd backup ran once; restore drill completed against scratch DB; scratch deleted (Task 8)
- [ ] launchd app supervisor restarts `app.py` on kill (Task 10.3)
- [ ] ICICI parse smoke returned plausible rows on a real statement (Task 9.4)
- [ ] No credentials committed: `git log --all --full-history -- .env credentials.yaml` returns nothing

- [ ] **Step 11.4: Tag the Week 1 completion**

```bash
git tag week-1-foundation
git push origin main week-1-foundation
```

- [ ] **Step 11.5: Update `tasks/lessons.md`**

Append any corrections received during Week 1 work — particularly `bout` API surprises, LiteLLM model-ID adjustments, or anything the plan had wrong. This seeds the lessons loop from project `CLAUDE.md` so Week 2's plan doesn't repeat today's mistakes.

---

## Self-Review

**Spec coverage:** Every Week 1 deliverable from PRD §11 is covered — Python project init (1), schema + readonly role + seed (2), LiteLLM routing + smoke + Supabase cost log (3), hashing (4a), DB + `adb()` (4b), bot skeleton with heartbeat middleware (5), alerts + heartbeat jobs + `/health` (6), orchestrator with graceful shutdown (7), standalone launchd backup + restore drill (8), ICICI parse smoke (9), launchd app supervisor (10), exit verification (11). All §16 success criteria for Week 1 are asserted in Task 11.3.

**Placeholder scan:** The seed SQL in Task 2.5 leaves `<angle_bracket>` values for Rajat's personal financial inventory (account last-4, loan principals, etc.) — those are personal values that don't belong in the plan. `__parser_version__` uses `getattr(bout, '__version__', 'unknown')`; test asserts it's not "unknown" so a missing attribute fails loudly. `<VERSION_FROM_TASK_0.X>` in `pyproject.toml` is filled in Task 1.5 after Task 0.2 picks a version. All other steps have complete, runnable content.

**Type consistency:** `ParsedRow` in Task 9 uses `txn_date` and `source_row_ordinal`, matching the `import_hash_pdf(txn_date=..., source_row_ordinal=...)` kwargs in Task 4a. Settings attributes (`supabase_db_url`, `healthcheck_url`, `telegram_alert_bot_token`, `finance_log_path`) all appear in the Settings class in Task 3.2. `ROUTING` dict keys in Task 3's test (`pdf_extraction`) match `config/model_routing.yaml`. `adb()` signature matches its usage in `bot/main.py`, `heartbeat.py`, and `health.py`. `__parser_version__` export pattern (Task 9.1) matches its consumption by `import_hash_pdf` in Task 4a (as a string parameter).

**r1 → r2 delta (ensuring nothing got lost in translation, per user's second-pass request):**

| r1 issue | r2 fix | Where |
|---|---|---|
| 🔴 `asyncio.run()` inside AsyncIOScheduler | All scheduler jobs are `async def`; `send_alert_sync` removed | Task 6.1, 6.4 |
| 🔴 `readonly_client()` was a lie | Dropped from Week 1 entirely; moved to Week 5 with psycopg | Task 4b |
| 🔴 Backup script shipped broken | Preconditions fetch `SUPABASE_DB_URL` first; script uses it directly | Task 0 + 8.1 |
| 🔴 bout API unverified | Task 0.1 runs `help(bout.parse)` before Task 9 | Task 0.1, 9.1 |
| 🟠 Sync Supabase blocks loop | `adb()` wrapper is the single choke point | Task 4b.3 |
| 🟠 `import_hash` Mode B flaw | Option Y + `parser_version`; PRD §7 + §19.5 updated | Task 4a |
| 🟠 "Bot heartbeat" was scheduler heartbeat | Middleware in bot writes the heartbeat on every message | Task 5.1 |
| 🟠 LiteLLM `images` param premature | Removed from Week 1 `llm()` signature | Task 3.6 |
| 🟠 LiteLLM pin unverified | `pip index versions litellm` in Task 0.2 before pinning | Task 0.2, 1.5 |
| 🟠 `pg_dump` blocks the loop | Backup moved to standalone launchd job, not APScheduler | Task 8 |
| 🟡 No process supervisor | `launchd/com.rajat.pfa.app.plist` | Task 10 |
| 🟡 No graceful shutdown | SIGTERM/SIGINT handler in `app.py:main()` | Task 7.1 |
| 🟡 No ruff/mypy config | `[tool.ruff]`, `[tool.mypy]`, `Makefile`, pre-commit | Task 1.5, 1.8, 1.9 |
| 🟡 No logging strategy | `logging_setup.py` with RotatingFileHandler | Task 3.3 |
| 🟡 Seed not in migrations | `migrations/003_seed.sql` with `ON CONFLICT DO NOTHING` | Task 2.5 |
| 🟡 Camelot system deps | `brew install ghostscript tcl-tk` in preconditions | Preconditions |
| 🟡 pgcrypto not explicit | `CREATE EXTENSION IF NOT EXISTS pgcrypto` in 001_init.sql | Task 2.1 |
| 🟡 Dead code (`images`, `reload_routing()`) | Both removed from Week 1 `llm.py` | Task 3.6 |
| 🟡 Task 4 test cleanup | `test_service_and_readonly_are_distinct_clients` dropped | Task 4b.1 |
| 🟡 `request_logs` provenance | Verified in Task 0.4; add CREATE TABLE if not auto-created | Task 0.4, 2.1 |
| 🟡 Parser version registry | `__parser_version__` exported per parser module | Task 9.1 |
| 🟡 Shutdown + backup interaction | Comment in `app.py` explaining the launchd separation | Task 7.1 |
| 🟡 bout password kwarg unverified | `help(bout.parse)` in Task 0.1 | Task 0.1 |
| RLS defer | `-- TODO(V2): ENABLE RLS` comment at top of 001_init.sql | Task 2.1 |
| `/model` ref in YAML | `--confirm` comment stripped from `config/model_routing.yaml` | Task 3.1 |
| Keychain for credentials | Documented at top of `credentials.example.yaml` as V2 upgrade path; V1 keeps gitignored YAML | Task 1.7 |

---

## Execution Handoff

Plan saved to `tasks/todo.md` (per project `CLAUDE.md` convention).

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration. Best for this plan given 11 tasks with clear interfaces.
2. **Inline Execution** — execute tasks in this session with checkpoints. Slower but fully visible.

**Which approach?**
