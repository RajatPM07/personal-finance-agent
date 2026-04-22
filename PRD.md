# Personal Finance Agent — Product Requirements Document (V2.1)

**Owner:** Rajat Sharma
**Status:** V2.1 scope locked — ready to build
**Last updated:** April 21, 2026
**Runtime target:** Python (aiogram + APScheduler + FastAPI) on spare Mac, Telegram interface
**Changelog:**
- **V2.1** — Restored six V1 §18 commitments dropped during the V2 rewrite: external healthchecks.io heartbeat, `txn_time` + PDF-content-hash in `import_hash`, affordability data-quality maturity guard (§6.5), dual-model Month 1 calibration recommitted in Week 2a with ₹1,000 cap, `goals.current_amount` source-of-truth locked, `/dedup` command restored. Minor: nudge matrix wording tightened, LiteLLM version pin added to Week 1, `dedup.py` added to skill structure. See §19 for full decisions log.
- V2 — architecture rewrite (OpenClaw → Python-native stack), LiteLLM model routing, OSS library integrations, stress test fixes

---

## 1. Problem statement

Rajat is an overspender who has repeatedly failed at manual expense logging. Financial data is scattered across multiple rails (2 banks, 3 credit cards, Paytm UPI, Zerodha investments, one active loan), and no single existing tool (Walnut, Money View, INDmoney, Fi) gives the combination of:

1. Automatic ingestion across all Indian financial rails
2. Personalized categorization that learns his spending patterns
3. Conversational reasoning for forward-looking questions ("can I afford Thailand?")
4. A family-scoped extension (wife Ayushi onboarded in V2)
5. Privacy controls both users can trust

The goal is to build a personal finance agent that runs 24/7, ingests automatically with minimal manual effort, categorizes intelligently, and surfaces insights — observing quietly by default, intervening (flagging, not blocking) when overspending trends emerge.

---

## 2. Goals & non-goals

### V1 goals
- Automatic ingestion from Gmail (CC statements, transaction emails, investment statements) and manual drops (Paytm XLSX via Telegram or local folder)
- Intelligent categorization with a per-user learned taxonomy, seeded from MCC codes and a hand-curated Indian merchant list
- Structured financial reasoning over SQL-backed data (not RAG)
- Morning brief + weekly review on Telegram
- "Can I afford X" affordability reasoning using income, outflow, commitments, liabilities, and assets
- Privacy/visibility layer baked into schema from day one
- Single-user (Rajat) operation with architecture ready for second-user (Ayushi) in V2
- Monitoring and alerting so failures are never silent

### V1 non-goals (explicit deferrals)
- Tax reasoning (though `Form16x` library is available when V2 activates this)
- Goal-based savings automation (goals can be tracked, not auto-funded)
- Forecasting beyond 3 months
- Ayushi onboarding (V2, targeting <1 hour when activated)
- Real-time transaction blocking (agent never blocks; only flags)
- Mobile app (Telegram is the interface)

### V2 (deferred)
- Ayushi onboarding
- Joint household view
- Tax reasoning from payslip data + Form 16 (`Form16x` library, MIT)
- Goal-based savings automation
- Nudge-level UX tuning (medium/loud modes)

---

## 3. Users & personas

### V1: Rajat (admin + user)
- Senior PM, Mumbai
- Banks: ICICI (primary), HDFC (secondary)
- UPI: 2 accounts via Paytm app
- Credit cards: 3 total (2 primary, 1 minor)
- Investments: Zerodha (stocks) + Zfunds (1 MF, ~₹2L current value)
- Liabilities: 1 personal loan (~₹8L outstanding @ ~10.3%, EMI ~₹22K/mo)
- Income: ICICI Lombard salary into ICICI bank (fixed + bonus + variable components)
- ADHD, prefers low-friction interfaces and configurable nudge intensity

### V2: Ayushi (user)
- Holds ICICI account (primary salary credit)
- May hold a secondary account (TBD)
- Same banks likely as Rajat — parser reuse, no new parsers needed day one

---

## 4. Scope — V1 feature set

| # | Feature | Description | OSS leverage |
|---|---|---|---|
| F1 | Automatic Gmail ingestion | Scans Gmail every 2 hrs for bank/CC/investment emails, extracts transactions | Gmail API (OAuth2) |
| F2 | Password-protected PDF handling | Per-sender credential lookup, decrypt, parse | `pikepdf` (standardized across all parsers) |
| F3 | SMS-to-Email bridge ingestion | Android app forwards ICICI/HDFC SMS to Gmail; agent parses | Port of `saurabhgupta050890/transaction-sms-parser` (MIT, TS→Py) |
| F4 | Telegram document drop | User sends Paytm XLSX or other files as Telegram document; agent ingests | `aiogram` file handler |
| F5 | Local folder drop fallback | `/Users/rajat/finance-inbox/` file watcher | `watchdog` (MIT) |
| F6 | Merchant normalization | Tiered: curated top-200 Indian merchants → `rapidfuzz` fuzzy match → pgvector embeddings → LLM fallback | `rapidfuzz` (MIT), `greggles/mcc-codes` (Unlicense) |
| F7 | Learned categorization | Baseline from DistilBERT classifier; user corrects; agent remembers | `mitulshah/global-financial-transaction-classifier` (HF, MIT) |
| F8 | Income classification | Auto-classifies recurring salary; asks when a credit doesn't match (variable/bonus/refund) | — |
| F9 | Payslip ingestion | Manual upload; Claude structured output extracts fixed/variable/tax components | Claude structured output with Pydantic schema |
| F10 | Investment snapshots | Weekly MF CAS + Zerodha statement ingestion; mark-to-market with live NAV | `casparser` (MIT), `mftool` (MIT), `pykiteconnect` (MIT) |
| F11 | Structured reasoning (text-to-SQL) | Natural language Q → SQL → answer (read-only enforced) | LiteLLM for model routing |
| F12 | Affordability engine | "Can I afford X" with runway, options (asset redemption, spend compression, variable-pay wait) | — |
| F13 | Morning brief | 9 AM Telegram ping: yesterday's totals, balance, investment value (Mondays) | `APScheduler` |
| F14 | Weekly review | Sunday 7 PM: week's categorized spend, unknowns needing classification | `APScheduler` |
| F15 | Privacy/visibility layer | Per-transaction `shared`/`private`/`excluded` flags | — |
| F16 | Soft-exclude for reasoning | `"ignore this for reasoning"` — row stays in DB, skipped in totals | — |
| F17 | Duplicate-safe ingestion | `import_hash` dedup — overlapping statement uploads are idempotent. Includes soft-deleted rows in dedup check. Fuzzy second-pass for near-duplicates. | Pattern from `bankstatementparser` (Apache-2) + Firefly III docs |
| F18 | Admin memory wipe | Admin-only command to clear agent memory (granular: `/wipe_memory all|taxonomy|prefs`) | — |
| F19 | Configurable nudge dial | Quiet (V1 default) / Medium / Loud, toggleable via `/nudge` command | — |
| F20 | Audit trail | Every ingestion logged in `ingestion_log` for debugging | — |
| F21 | Statement-total validator | Cross-checks extracted rows against declared totals on CC statements; rejects on mismatch | — |
| F22 | External heartbeat monitoring | 15-min heartbeat ping to healthchecks.io; alerts if Mac/agent goes down entirely | `healthchecks.io` |
| F23 | SMS reconciliation | Cross-references CC transactions >₹5K against SMS alerts for silent corruption detection | — |
| F24 | Interactive dedup resolution | `/dedup` command for resolving transactions flagged as possible duplicates | — |

### Nudge behavior matrix

| Level | Real-time alerts | Morning brief | Weekly review | Budget warnings | Affordability prompts |
|-------|-----------------|---------------|---------------|-----------------|----------------------|
| Quiet (V1 default) | No | Yes | Yes | No | On-demand only |
| Medium | Outflow transactions > ₹5K | Yes | Yes | Weekly | On-demand |
| Loud | All outflow transactions | Yes | Yes | Daily | Proactive |

---

## 5. Architecture overview

### 5.1 Stack summary

| Layer | Choice | Rationale |
|---|---|---|
| Runtime | Python 3.11+ on spare Mac | Every useful library in the ecosystem is Python. Single-language stack = simpler debugging, deployment, and maintenance. |
| Chat interface | Telegram bot via `aiogram` (MIT, 5.6k stars, async-first) | Free, stable, file attachments supported, works cross-platform. MIT-licensed (unlike `python-telegram-bot` which is GPL). |
| Scheduling | `APScheduler` | Cron-style scheduling for morning briefs, weekly reviews, Gmail scans, heartbeat. |
| HTTP API | FastAPI (for health checks + future webhook endpoints) | Lightweight, async, well-documented. Powers `/health` endpoint and optional admin API. |
| Data store | Supabase (existing Singapore instance) | Already set up; pgvector available for merchant embeddings. |
| Business logic | Python modules (`/skills/finance/`) | Portable, testable, framework-agnostic. |
| LLM routing | LiteLLM (MIT, 44k stars) | Unified API across 100+ providers; built-in fallback, retry, rate-limit handling, Supabase cost logging. Replaces custom router. |
| Embeddings | Gemini `embedding-001` (free tier, via LiteLLM) | Used for merchant normalization long-tail only. |
| PDF decrypt | `pikepdf` | Standardized across all parsers. Deterministic, no external CLI dependency. |
| PDF extraction | `pdfplumber` + `camelot-py[cv]` | Lighter than tabula (no JVM). Used for table extraction from bank/CC PDFs. |
| Gmail access | Gmail API (OAuth2, `google-auth` + `google-api-python-client`) | Direct OAuth2 — no MCP dependency, token refresh handled explicitly. |

### 5.2 Data flow

```
Email arrives → Gmail API scan (cron 2h)
  → Route by sender (CC/bank/investment)
  → Decrypt PDF using credentials.yaml (pikepdf)
  → Extract rows via litellm.completion("gemini/gemini-2.5-flash", ...)
  → Extract declared totals (total spends, total credits, closing balance)
  → Validate: sum(extracted_rows) == declared_totals ± ₹1
     → If mismatch: reject entire statement → ingestion_log.status='needs_review' → Telegram alert
     → If match: proceed
  → For each row:
      → compute import_hash (see §7 dedup strategy — includes txn_time where available + pdf_content_hash for PDF rows)
      → skip if exists in DB (including soft-deleted rows)
      → fuzzy second-pass: date ±2 days + exact amount + Jaro-Winkler >0.9 → flag as possible duplicate
      → normalize merchant (curated list → rapidfuzz → pgvector → LLM fallback)
      → categorize via litellm.completion("groq/llama-3.3-70b-versatile", ...)
      → detect refund (direction='in' + merchant matches recent 'out' transaction)
      → insert into transactions
  → Label email as "finance-ingested"
  → Log outcome in ingestion_log

Telegram document drop → aiogram file handler
  → Save to /finance-inbox/
  → Route: XLSX → pandas; PDF → pikepdf + pdfplumber; image → OCR (future)
  → Same pipeline (hash → dedup → validate → normalize → categorize → insert)
  → Telegram confirmation message
```

### 5.3 Architecture principles

1. **Single language, single runtime.** Python everywhere. No TypeScript/Node dependency.
2. **Framework-agnostic business logic.** All finance logic lives in `/skills/finance/`. The Telegram bot, scheduler, and API are thin orchestration layers.
3. **If aiogram or APScheduler needs replacing,** the business logic doesn't change. Swap the bot framework, point at the same modules.
4. **LiteLLM abstracts the LLM layer.** Switching providers is a config change, not a code change.

---

## 6. Model routing architecture (via LiteLLM)

The LLM layer is abstracted behind LiteLLM so that models can be switched per-task without touching business logic. Rationale:

1. **Cost control.** Free tiers (Gemini, Groq) handle 90% of calls. Claude is used only where reasoning quality materially affects outcomes.
2. **Future-proofing.** New models drop every few months. Switching is a one-line config change, not a refactor.
3. **Graceful degradation.** If a primary model fails or hits rate limits, LiteLLM's `fallbacks` parameter handles retry automatically.
4. **Built-in cost tracking.** LiteLLM's Supabase callback logs every call's cost to a `litellm_spend_logs` table — no custom instrumentation needed.

### 6.1 Two-layer design

**Layer 1 — `config/model_routing.yaml`** (the only file that changes when switching models):

```yaml
pdf_extraction:
  model: gemini/gemini-2.5-flash
  fallbacks: [anthropic/claude-sonnet-4-6]
  stakes: high          # requires --confirm flag to switch

merchant_categorization:
  model: groq/llama-3.3-70b-versatile
  fallbacks: [gemini/gemini-2.5-flash]
  stakes: low

income_classification:
  model: gemini/gemini-2.5-flash
  fallbacks: []         # asks user if primary fails
  stakes: medium

sql_agent:
  model: anthropic/claude-sonnet-4-6
  fallbacks: [gemini/gemini-2.5-pro]
  stakes: high

affordability_reasoning:
  model: anthropic/claude-sonnet-4-6
  fallbacks: [gemini/gemini-2.5-pro]
  stakes: high

morning_brief:
  model: gemini/gemini-2.5-flash
  fallbacks: [groq/llama-3.3-70b-versatile]
  stakes: low

weekly_review:
  model: gemini/gemini-2.5-flash
  fallbacks: [groq/llama-3.3-70b-versatile]
  stakes: low

embeddings:
  model: gemini/text-embedding-004
  fallbacks: []
  stakes: low
```

**Layer 2 — `lib/llm.py`** (the single entry point the rest of the codebase uses):

```python
import litellm
import yaml

# Enable Supabase cost logging
litellm.success_callback = ["supabase"]

def load_routing():
    with open("config/model_routing.yaml") as f:
        return yaml.safe_load(f)

ROUTING = load_routing()

def llm(task: str, prompt: str, system: str = None, images: list = None):
    """Single entry point for all LLM calls. Routes by task name."""
    config = ROUTING[task]
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    
    content = [{"type": "text", "text": prompt}]
    if images:
        for img in images:
            content.append({"type": "image_url", "image_url": {"url": img}})
    messages.append({"role": "user", "content": content if images else prompt})
    
    return litellm.completion(
        model=config["model"],
        messages=messages,
        fallbacks=config.get("fallbacks", []),
        metadata={"task": task},
    )

def reload_routing():
    """Hot-reload routing config after /model command changes."""
    global ROUTING
    ROUTING = load_routing()
```

Every call elsewhere in the codebase looks like `llm("merchant_categorization", prompt=...)`. No business logic imports provider SDKs directly.

### 6.2 Switching models at runtime

Three modes, increasing in speed and decreasing in safety:

**Mode A — Edit YAML via natural language on Telegram:**

> *"Switch the categorizer from Groq to Gemini Flash."*

Agent edits `config/model_routing.yaml`, commits to Git (audit trail), calls `reload_routing()`. Reversible by asking to revert.

**Mode B — Structured Telegram commands:**

```
/model list                                    # show current routing
/model <task> <model_name>                     # switch primary
/model <task> default                          # revert to config default
/model <task> <model_name> --confirm           # required for stakes: high tasks
```

**Mode C — A/B test mode (V1.5 / V2):**

> *"A/B test merchant_categorization: Groq vs Gemini Flash for 7 days."*

Agent sets both models, runs both on every call via LiteLLM, logs accuracy when user corrects miscategorizations, reports winner at end of period.

### 6.3 Task-to-model defaults (initial)

| Task | Primary | Fallback | Monthly cost estimate |
|---|---|---|---|
| PDF extraction | Gemini 2.5 Flash | Claude Sonnet 4.7 | ~₹0 (free tier) |
| Merchant categorization | Groq Llama 3.3 70B | Gemini 2.5 Flash | ~₹0 (free tier) |
| Income classification | Gemini 2.5 Flash | (asks user) | ~₹0 (free tier) |
| SQL agent | Claude Sonnet 4.7 | Gemini 2.5 Pro | ~₹200–800 |
| Affordability reasoning | Claude Sonnet 4.7 | Gemini 2.5 Pro | ~₹100–400 |
| Morning brief / weekly review | Gemini 2.5 Flash | Groq Llama | ~₹0 (free tier) |
| Embeddings | Gemini `text-embedding-004` | — | ~₹0 (free tier) |

**Expected total: ₹300–1,200/month (~$4–$15)** for the full system. If Claude spend becomes a concern, switch `sql_agent` and `affordability_reasoning` to Gemini 2.5 Pro free tier → ₹0 total with some reasoning-quality tradeoff.

### 6.4 Stakes-based safety gate

Tasks marked `stakes: high` in config (PDF extraction, SQL agent, affordability reasoning) require explicit `--confirm` flag when switched via `/model` command. This prevents casual routing of high-stakes tasks to weaker models. Rationale:

- **PDF extraction:** A misread amount (e.g. ₹22,000 → ₹2,200) poisons the DB silently and propagates to every future reasoning call.
- **SQL agent:** Wrong SQL returns wrong answers, which the agent then presents confidently.
- **Affordability reasoning:** Wrong "can I afford X" advice can drive real overspending — the exact problem this system exists to solve.

### 6.5 Affordability Data-Quality Maturity Guard
The affordability engine has a hard guard: it will refuse to run affordability reasoning queries until:
1. **Quantity:** There is at least 60 days of continuous data.
2. **Quality:** < 10% of transactions are uncategorized.
3. **Integrity:** No statements in the last 90 days have bypassed or failed the statement-total validator (F21). A statement flagged by the total check and manually patched is considered lower confidence and blocks affordability recommendations.

### 6.6 Ollama / local model support

LiteLLM natively supports Ollama (`ollama/llama3.1`, etc.). Adding local models is a single YAML change. Rationale for deferral:

- Requires 32GB+ unified memory for models strong enough to be useful.
- Latency trades off against cost savings.
- The LiteLLM entry in the routing config means switching to fully-local is a config change, not a code change.

This is a V2+ exploration, not V1.

---

## 7. Data model (Supabase PostgreSQL)

Every row has `user_id` and `created_at`. Every query filters on `user_id` first.

### Database roles

```sql
-- Read-only role for the SQL agent (CRITICAL: prevents LLM-generated mutations)
CREATE ROLE finance_agent_readonly WITH LOGIN PASSWORD '<strong-password>';
GRANT USAGE ON SCHEMA public TO finance_agent_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO finance_agent_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO finance_agent_readonly;

-- Service role used by ingestion pipeline (full write access, via Supabase service key)
-- Already exists as the Supabase service_role
```

The SQL agent (§11, `sql_agent.py`) connects via `finance_agent_readonly`. An additional SQL validator layer rejects any query containing `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, or `TRUNCATE`. Queries default to `LIMIT 1000` with a 10-second timeout.

### `users`
```sql
id            uuid PRIMARY KEY DEFAULT gen_random_uuid()
telegram_handle text UNIQUE
role          text CHECK (role IN ('admin','user'))
display_name  text
created_at    timestamptz DEFAULT now()
```
Seed: one row for Rajat, `role='admin'`.

### `accounts`
```sql
id            uuid PRIMARY KEY DEFAULT gen_random_uuid()
user_id       uuid REFERENCES users
type          text CHECK (type IN ('bank','credit_card','upi','broker','mf'))
institution   text  -- 'ICICI', 'HDFC', 'Paytm', 'Zerodha', 'Zfunds'
identifier    text  -- last 4 digits / UPI handle / client code
nickname      text
active        boolean DEFAULT true
created_at    timestamptz DEFAULT now()
```

### `transactions`
```sql
id                  uuid PRIMARY KEY DEFAULT gen_random_uuid()
user_id             uuid REFERENCES users
date                date NOT NULL
txn_time            timestamptz             -- exact timestamp when source provides it (SMS, txn emails); null for most PDF rows
amount              numeric(12,2) NOT NULL
currency            text DEFAULT 'INR'      -- V1 primarily INR; schema forward-compatible for foreign txns
direction           text CHECK (direction IN ('in','out'))
is_refund           boolean DEFAULT false
linked_txn_id       uuid REFERENCES transactions  -- links refund to original transaction
raw_merchant        text
normalized_merchant text
category_id         uuid REFERENCES categories
subcategory         text
source              text  -- 'gmail_cc_stmt', 'gmail_txn_email', 'sms_forwarded', 'paytm_xlsx', 'zerodha_api', 'mf_cas', 'manual'
source_ref          text  -- email_id / pdf_hash / sms_id — AUDIT column only; NOT part of import_hash
pdf_content_hash    text  -- sha256 of source PDF bytes; null for non-PDF rows; part of Mode B import_hash
source_row_ordinal  int   -- within-PDF row position (1..N per parser); part of Mode B import_hash
parser_version      text  -- e.g. 'bout@0.4.2', 'hdfc_cc_v2_infinia@1.3.0'; part of import_hash
import_hash         text UNIQUE  -- see §7 dedup strategy below for two-mode formula
account_id          uuid REFERENCES accounts
visibility          text DEFAULT 'shared' CHECK (visibility IN ('shared','private','excluded'))
is_deleted          boolean DEFAULT false  -- soft delete; included in dedup checks per Firefly III pattern
notes               text
ingested_at         timestamptz DEFAULT now()
updated_at          timestamptz DEFAULT now()
```

**Dedup strategy — two-mode `import_hash`:**

The hash formula adapts to the source because different sources provide different disambiguating signals. Without this, two real same-day same-amount transactions (two ₹350 Swiggy orders on the same card) would silently collapse into one row.

Every parser module exports a `__parser_version__` string (e.g., `"bout@0.4.2"`, `"hdfc_cc_v2_infinia@1.3.0"`). The ingestion pipeline threads this into the hash so that parser upgrades produce a fresh hash — intentionally a "new" row — followed by secondary reconciliation rather than silent merge.

- **Mode A — time-bearing sources** (SMS, Gmail txn emails, webhook events):
  `import_hash = sha256(account_id || txn_time || amount || normalized_description || parser_version)`
  — `txn_time` (second precision) is the primary disambiguator.
  — `source_ref` is intentionally **not** in the hash so the same txn observed via both an SMS and a Gmail alert dedups correctly across channels.

- **Mode B — PDF-derived rows** (CC statements, MF CAS, bank PDFs — no reliable per-row time):
  `import_hash = sha256(account_id || date || amount || normalized_description || pdf_content_hash || source_row_ordinal || parser_version)`
  — `source_row_ordinal` (1..N within the PDF, per parser) disambiguates intra-PDF same-day same-amount rows (the two-Swiggy case).
  — `pdf_content_hash` scopes uniqueness to the specific source document; re-parsing the identical PDF with the same parser is idempotent.
  — `source_ref` is intentionally **not** in the hash so the same PDF delivered via Gmail attachment and Telegram upload dedups correctly across channels.

**Known tradeoff — rolling-statement overlap:** The same real transaction appearing in two distinct PDFs (e.g., a month-end statement and a year-end consolidated) produces different `pdf_content_hash` → different `import_hash` → two rows inserted. Mitigated by the secondary fuzzy pass (below). **Week 3 decision:** whether to add cross-PDF reconciliation or keep manual resolution via `/dedup`.

**Future upgrade path — Option X (post-V1):** once per-row transaction reference numbers (e.g., HDFC CC "Ref No.") can be reliably extracted into a dedicated `txn_reference` column, Mode B will be amended so that `txn_reference`, when non-null, **overrides** `source_row_ordinal` in the hash. This auto-resolves rolling overlap. Treated as an override rather than an additive field so fallback behavior stays explicit. Deferred because extractor reliability isn't proven yet.

Ingestion pattern:
- `INSERT ... ON CONFLICT (import_hash) DO NOTHING`
- Soft-deleted rows (`is_deleted=true`) remain in the dedup check to prevent zombie re-imports
- Second-pass fuzzy warning: date ±2 days, exact amount, payee Jaro-Winkler >0.9 → flag in `ingestion_log` as `status='possible_duplicate'`. User resolves via `/dedup` command (§10).

**Refund handling:**
- `is_refund=true` + `linked_txn_id` pointing to the original transaction
- Categorizer detects refunds: `direction='in'` + merchant fuzzy-matches a recent `direction='out'` transaction within 30 days
- Affordability engine nets refunds against original spend

### `categories`
```sql
id             uuid PRIMARY KEY DEFAULT gen_random_uuid()
user_id        uuid REFERENCES users
name           text
parent_id      uuid REFERENCES categories
mcc_code       text  -- ISO 18245 MCC code from greggles/mcc-codes, nullable
is_system      boolean DEFAULT false
created_by     text CHECK (created_by IN ('agent','user','seed'))
first_seen_at  timestamptz DEFAULT now()
```
Seed: populated from `greggles/mcc-codes` (Unlicense) taxonomy with `created_by='seed'`.

### `commitments`
```sql
id              uuid PRIMARY KEY DEFAULT gen_random_uuid()
user_id         uuid REFERENCES users
type            text CHECK (type IN ('emi','rent','subscription','insurance_premium','other'))
name            text
amount          numeric(12,2)
frequency       text CHECK (frequency IN ('monthly','quarterly','annual'))
next_due_date   date
account_id      uuid REFERENCES accounts
active          boolean DEFAULT true
liability_id    uuid REFERENCES liabilities  -- for EMIs
updated_at      timestamptz DEFAULT now()
```

### `liabilities`
```sql
id                     uuid PRIMARY KEY DEFAULT gen_random_uuid()
user_id                uuid REFERENCES users
type                   text CHECK (type IN ('personal_loan','home_loan','credit_card_revolving','other'))
original_principal     numeric(14,2)
outstanding_principal  numeric(14,2)
interest_rate          numeric(5,2)
emi_amount             numeric(12,2)
tenure_remaining_months int
start_date             date
lender                 text
last_updated_at        timestamptz DEFAULT now()
updated_at             timestamptz DEFAULT now()
```
Seed: Rajat's personal loan — exact numbers from statement.

### `assets`
```sql
id               uuid PRIMARY KEY DEFAULT gen_random_uuid()
user_id          uuid REFERENCES users
type             text CHECK (type IN ('stock','mf','fd','epf','ppf','savings','other'))
institution      text
identifier       text  -- ISIN, folio, FD number
quantity         numeric(18,4)
avg_cost         numeric(12,2)
current_value    numeric(14,2)
currency         text DEFAULT 'INR'
last_valued_at   timestamptz
notes            text
updated_at       timestamptz DEFAULT now()
```

### `asset_snapshots`
```sql
id             uuid PRIMARY KEY DEFAULT gen_random_uuid()
asset_id       uuid REFERENCES assets
valued_at      timestamptz
value          numeric(14,2)
```
Weekly snapshot for week-over-week tracking.

### `goals`
```sql
id             uuid PRIMARY KEY DEFAULT gen_random_uuid()
user_id        uuid REFERENCES users
name           text
target_amount  numeric(14,2)
current_amount numeric(14,2) DEFAULT 0  -- CACHE ONLY. Written exclusively by budget_engine.py on its weekly run (computes from tagged transactions or designated asset balance). Never written from anywhere else. Treat as stale between updates; authoritative answer is always the computation, not the column.
target_date    date
priority       int
notes          text
status         text CHECK (status IN ('active','paused','achieved','abandoned'))
created_at     timestamptz DEFAULT now()
updated_at     timestamptz DEFAULT now()
```

### `income_events`
```sql
id              uuid PRIMARY KEY DEFAULT gen_random_uuid()
user_id         uuid REFERENCES users
date            date
amount          numeric(12,2)
bucket          text CHECK (bucket IN ('recurring_salary','variable_comp','other'))
source_txn_id   uuid REFERENCES transactions
classified_by   text CHECK (classified_by IN ('agent','user','payslip'))
```

**Affordability income policy:** The affordability engine uses only `recurring_salary` for baseline runway calculation. `variable_comp` is excluded unless the user explicitly opts it in for a specific affordability query (e.g., "Can I afford X if I include my bonus?").

### `ingestion_log`
```sql
id           uuid PRIMARY KEY DEFAULT gen_random_uuid()
source       text
source_ref   text
status       text CHECK (status IN ('success','skipped_duplicate','possible_duplicate','failed','needs_review','total_check_failed'))
rows_added   int
declared_total numeric(14,2)  -- from CC statement
extracted_total numeric(14,2) -- sum of extracted rows
error_msg    text
timestamp    timestamptz DEFAULT now()
```

### `agent_memory`
```sql
id          uuid PRIMARY KEY DEFAULT gen_random_uuid()
user_id     uuid REFERENCES users
key         text
value       jsonb
updated_at  timestamptz DEFAULT now()
UNIQUE(user_id, key)
```
For finance-specific persistent memory (taxonomy preferences, nudge level, classifications, etc.).

### `heartbeat`
```sql
id          uuid PRIMARY KEY DEFAULT gen_random_uuid()
component   text  -- 'gmail_scanner', 'morning_brief', 'weekly_review', 'telegram_bot'
status      text CHECK (status IN ('ok','failed','late'))
last_ping   timestamptz DEFAULT now()
error_msg   text
```
Internal heartbeat table. Complemented by an external 15-minute ping to **healthchecks.io** (or similar external watchdog) to catch complete system failures (e.g., Mac loses power, network drops).

### `updated_at` triggers
```sql
-- Apply to all tables with updated_at
CREATE OR REPLACE FUNCTION update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_updated_at BEFORE UPDATE ON transactions FOR EACH ROW EXECUTE FUNCTION update_timestamp();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON commitments FOR EACH ROW EXECUTE FUNCTION update_timestamp();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON liabilities FOR EACH ROW EXECUTE FUNCTION update_timestamp();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON assets FOR EACH ROW EXECUTE FUNCTION update_timestamp();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON goals FOR EACH ROW EXECUTE FUNCTION update_timestamp();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON agent_memory FOR EACH ROW EXECUTE FUNCTION update_timestamp();
```

---

## 8. Skill structure (`/skills/finance/`)

```
/skills/finance/
├── ingestion/
│   ├── gmail_scanner.py        # cron every 2h via APScheduler
│   ├── pdf_parser.py           # CC statements — pikepdf decrypt + pdfplumber/camelot extract
│   ├── parsers/
│   │   ├── icici_parser.py     # Uses `bout` library (MIT) — pip install bout
│   │   ├── hdfc_cc_v1.py       # Fork of xaneem/hdfc-credit-card-statement-parser (MIT)
│   │   ├── hdfc_cc_v2.py       # Post-Sept 2025 Infinia format (ref: joeirimpan/hdfc-cc-parser-rs)
│   │   └── generic_parser.py   # LLM-based fallback for unknown formats
│   ├── statement_validator.py  # Cross-checks extracted totals vs declared totals
│   ├── credentials.yaml        # GITIGNORED — PDF passwords per bank
│   ├── paytm_parser.py         # Paytm XLSX export → pandas (not PDF)
│   ├── mf_parser.py            # Uses `casparser` (MIT) + `mftool` (MIT) for NAV
│   ├── zerodha_parser.py       # Kite Connect API or Console CSV tradebook
│   ├── payslip_parser.py       # Claude structured output with Pydantic schema
│   └── sms_parser.py           # Ported from saurabhgupta050890/transaction-sms-parser (MIT)
├── normalization/
│   ├── merchant_normalizer.py  # Tiered: curated → rapidfuzz → pgvector → LLM
│   ├── merchants_india.json    # Hand-curated top-200 Indian merchants
│   └── mcc_taxonomy.json       # From greggles/mcc-codes (Unlicense)
├── categorization/
│   ├── categorizer.py          # DistilBERT baseline + LLM + memory
│   └── taxonomy_seed.json      # MCC-codes-derived seed with Indian additions
├── reasoning/
│   ├── sql_agent.py            # text-to-SQL via read-only DB role + SQL validator
│   ├── sql_validator.py        # Rejects INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE, enforces LIMIT + timeout
│   ├── affordability.py        # "Can I afford X" — uses only recurring_salary for baseline
│   └── budget_engine.py        # Activates month 2
├── nudging/
│   ├── morning_brief.py        # 9 AM daily via APScheduler
│   ├── weekly_review.py        # Sunday 7 PM via APScheduler
│   └── nudge_policy.py         # Quiet/Medium/Loud per behavior matrix (§4)
├── monitoring/
│   ├── heartbeat.py            # 15-min heartbeat to heartbeat table + stale-check
│   ├── alerts.py               # Telegram alerts for failures, missed briefs, ingestion errors
│   └── health.py               # FastAPI /health endpoint
├── privacy/
│   ├── visibility_filter.py    # Applied to every query
│   └── exclusion_rules.py      # "ignore this" logic
├── reconciliation/
│   ├── sms_reconciler.py       # Cross-references CC txns >₹5K against SMS alerts
│   └── refund_detector.py      # Matches incoming credits to recent merchant debits
├── commands/
│   ├── exclude.py
│   ├── categorize.py
│   ├── dedup.py                # /dedup confirm|merge|delete — resolve possible_duplicate pairs
│   ├── goal.py
│   ├── nudge.py
│   ├── model.py                # /model list|switch|--confirm
│   ├── status.py               # Last ingestion, failures, health, disk usage
│   ├── retry.py                # /retry <source_ref> — re-attempt failed ingestion
│   └── wipe_memory.py          # admin-only, granular: all|taxonomy|prefs
├── lib/
│   ├── db.py                   # Supabase client — two connections: service_role (writes) + readonly (SQL agent)
│   ├── llm.py                  # LiteLLM wrapper — routes by task via model_routing.yaml
│   └── hashing.py              # import_hash computation
└── tests/
    ├── golden_fixtures/         # First correctly-parsed statement per bank (Month 2+)
    ├── test_parsers.py
    ├── test_dedup.py
    ├── test_sql_validator.py
    └── test_categorizer.py
```

Note: `config/model_routing.yaml` lives at the repo root (not inside `skills/finance/`) so it's editable without touching code. See §6 for full details.

---

## 9. Behavioral rules (agent system prompt core)

1. **Observe before judging.** Month 1 is silent ingestion — no budget nudges until month 2 co-creation. Classification questions (income type, unknown merchants) are allowed during Month 1; budget opinions are not.
2. **Ask when uncertain, never invent.** New merchants, ambiguous categories, unrecognized credits → short Telegram question.
3. **Privacy filtering is SQL-enforced, not prompt-enforced.** Visibility filter lives in `db.py`. Prompts cannot be manipulated into leaking.
4. **Proactive behavior respects the nudge dial.** V1 default = Quiet (morning brief + weekly review + user-initiated only). See §4 nudge behavior matrix.
5. **Affordability reasoning is structured AND data-quality gated.** Uses SQL against `income_events`, `commitments`, `liabilities`, `assets` — not free-form LLM guessing. Baseline uses only `recurring_salary`; variable comp excluded unless user opts in per query. The engine refuses a confident numeric answer when the data-quality guard (§6.5) fails: <60 days of history, >10% of last-30-day outflow uncategorized, or any statement in last 90 days failed totals check. Force-override via `/afford --force` returns a caveated answer.
6. **Income classification: salary auto, variable asks.** Recurring salary pattern auto-classified; variable/bonus/other requires user confirmation.
7. **Failures are never silent.** Failed ingestions trigger immediate Telegram alerts. `needs_review` items are batched into the weekly review. Morning brief no-show triggers a backup alert by 9:15 AM.
8. **Statement integrity before data.** CC statement extraction is rejected if extracted totals don't match declared totals (±₹1). No partial ingestion of unvalidated data.

---

## 10. Interaction surface

### Automatic (agent-initiated)
- **Daily morning brief (9 AM):** yesterday's spend, balance, flagged items, Monday = + investment snapshot
- **Weekly review (Sunday 7 PM):** totals, category breakdown, unknowns needing classification
- **Income credit detection:** Telegram question when credit doesn't match known pattern
- **Ingestion failure alerts:** Immediate Telegram message on failed parse, wrong password, total-check failure
- **Heartbeat failure:** Telegram alert if any component goes stale (>30 min without heartbeat)

### On-demand (user-initiated)
Natural language queries (handled by `sql_agent.py`):
- "How much did I spend on Blinkit last month?"
- "What's my net worth right now?"
- "Can I afford a Thailand trip that costs ₹80K in June?"
- "Show transactions above ₹5K in the last 30 days."
- "Can I afford X if I include my bonus?"

Commands:
- `/exclude <txn_id>` — soft exclude from reasoning
- `/undo_exclude <txn_id>`
- `/categorize <txn_id> <category>` — correct miscategorization
- `/dedup confirm <pair_id>` — both rows are real; clear the `possible_duplicate` flag
- `/dedup merge <pair_id> <keep_id>` — one is a duplicate; delete the other, keep `keep_id`
- `/goal <name> <amount> <target_date>`
- `/nudge <quiet|medium|loud>`
- `/afford <query> [--force]` — affordability query; `--force` bypasses the data-quality maturity guard (§6.5) and returns a caveated answer
- `/status` — last ingestion, failures, health, disk usage
- `/retry <source_ref>` — re-attempt a failed ingestion
- `/model list` — show current routing
- `/model <task> <model_name> [--confirm]` — switch model for a task
- `/wipe_memory <all|taxonomy|prefs>` — admin only

---

## 11. Build sequence

### Week 1 — Foundation + first data flowing
- Python project initialized: `pyproject.toml`, virtualenv, linting
- `pip install aiogram apscheduler fastapi uvicorn pikepdf pdfplumber camelot-py[cv] rapidfuzz supabase bout casparser mftool` — plus `litellm` **pinned to current stable** (e.g., `litellm==1.60.*`); record exact version in `pyproject.toml`
- Supabase schema applied (all tables, roles, triggers from §7)
- `finance_agent_readonly` role created and tested (verify it cannot execute `INSERT`/`UPDATE`/`DELETE`)
- `users`, `accounts`, `liabilities`, `commitments` seeded
- LiteLLM routing wired: `config/model_routing.yaml` + `lib/llm.py`
- Smoke-test: call each provider once through `llm(task, ...)` and confirm fallback works
- LiteLLM → Supabase cost logging callback configured
- Telegram bot skeleton via aiogram: "Hello world" round-trip
- APScheduler scaffolding: placeholder cron jobs
- FastAPI `/health` endpoint
- **External heartbeat wired** — healthchecks.io endpoint created; APScheduler job pings `HEALTHCHECK_URL` every 15 min; verify alert fires to secondary Telegram bot by manually stopping the job
- **Internal heartbeat** — 15-min writes to `heartbeat` table + component stale-check (complements external ping; catches per-component failures the external ping can't see)
- **Automated backup:** daily `pg_dump` via APScheduler to local `~/finance-backups/` + monthly copy to external drive; restore procedure verified end-to-end once before Week 1 closes
- First real parsing: `bout` for ICICI (test with one real statement)

### Week 2 — Ingestion (hardest week, de-risked by OSS)
- **Week 2a:**
  - ICICI CC/savings via `bout` — validate against real statements
  - HDFC CC parser (fork `xaneem`) — validate against real statements
  - Statement-total validator (`statement_validator.py`) — hard requirement before any ingestion runs in production
  - `credentials.yaml` populated
  - Backfill: 1 month of ICICI + HDFC CC statements (not 3 — validate pipeline first)
  - Telegram row-review flow: summary ("45 rows, ₹1,24,380 total, matches declared total: ✓") → tap-to-approve or tap-to-review
  - **Dual-model Month 1 calibration (committed):** run Gemini Flash AND Claude Haiku on the same PDF for the first 2–3 statements per bank; diff surfaced in Telegram for row-level adjudication. Establishes per-bank ground truth and catches merchant/date misreads that the totals validator can't catch (totals pass but a ₹3,400 ZOMATO row was really SWIGGY). **Hard cost cap: ₹1,000 total across all three CC formats combined**, tracked live in `ingestion_log`; auto-halt on approach. Stops after Month 1 unless a bank changes statement format.
- **Week 2b:**
  - AMEX CC parser (using `generic_parser.py` with LLM-based extraction since no robust OSS regex parser exists)
  - `/model` command family wired (list, switch, --confirm for high-stakes)
  - Backfill remaining 2 months once pipeline is trusted

### Week 3 — Remaining sources
- SMS parser: Python port of `saurabhgupta050890/transaction-sms-parser` (MIT)
- SMS-to-Email forwarder (Android app) configured
- SMS reconciliation live: cross-reference CC transactions >₹5K against SMS alerts
- Paytm XLSX ingestion: Telegram-drop + local folder watcher, `pandas.read_excel()` → pipeline
- MF CAS ingestion: `casparser` + `mftool` NAV enrichment → `assets` + `asset_snapshots`
- Zerodha: Kite Connect API or Console CSV tradebook → `assets`
- Payslip parser: Claude structured output with Pydantic schema → `income_events`

### Week 4 — Reasoning layer
- `sql_agent.py` live (via read-only role + SQL validator)
- Morning brief + weekly review scheduled via APScheduler
- `/exclude`, `/categorize`, `/retry`, `/dedup`, `/afford` commands
- **Affordability engine V1 with data-quality maturity guard (§6.5)** — refuses confident numeric answer when history <60d, >10% outflow uncategorized, or any recent statement failed totals check; `/afford --force` returns caveated answer
- Refund detection live

### Week 5 — Merchant normalization + categorization maturation
- Hand-curated `merchants_india.json` (top 200)
- MCC taxonomy seed from `greggles/mcc-codes`
- Tiered normalization pipeline: curated → rapidfuzz → pgvector → LLM fallback
- DistilBERT baseline categorizer integrated (optional — depends on accuracy vs LLM)

### Week 6 — Budget engine + goals
- Budget engine activation (start of month 2)
- Goal tracking (`/goal` command + `current_amount` tracking)
- Taxonomy refinement from Month 1 corrections

### Week 7 — Polish + monitoring hardening
- `/status` command (ingestion health, disk usage, cost summary)
- Golden-fixture regression tests (first correctly-parsed statement per bank)
- Confidence-per-field from extractor as tertiary signal
- Admin commands: `/wipe_memory all|taxonomy|prefs`
- End-to-end test: full cycle from email → ingestion → brief → query

### Week 8 — Stabilization
- Bug fixes from daily usage
- Performance tuning (query optimization, batch ingestion)
- Documentation: README, operational runbook
- Define retention/archival policy for data >12 months

### Week 9+ — V2 kickoff (Ayushi)
- Add user row, Telegram handle
- Add her ICICI account(s)
- Define RLS policies (Postgres Row Level Security) — **hard prerequisite for V2**
- Define privacy response style when Ayushi queries Rajat's private data
- Test privacy enforcement with real second user

---

## 12. Credentials manifest

**⚠️ None of these values are stored in this PRD, the repo, or any chat. They are populated locally on your machine only.**

All credentials live in two gitignored files:
- `credentials.yaml` — bank/PDF passwords
- `.env` — API keys and tokens

### Credentials required

| # | Credential | Where to obtain | Storage location | Notes |
|---|---|---|---|---|
| C1 | Anthropic API key | https://console.anthropic.com/ → API Keys | `.env` as `ANTHROPIC_API_KEY` | Used only for `sql_agent` and `affordability_reasoning` per §6. Budget ~₹300–1,200/mo |
| C2 | Gemini API key | https://aistudio.google.com/ → Get API Key | `.env` as `GEMINI_API_KEY` | Primary for PDF extraction, briefs, embeddings. Free tier covers expected load. Note: free-tier prompts may be used to improve Google models |
| C3 | Groq API key | https://console.groq.com/ → API Keys | `.env` as `GROQ_API_KEY` | Used for `merchant_categorization`. Free tier sufficient |
| C4 | Supabase project URL | Supabase dashboard → Project Settings → API | `.env` as `SUPABASE_URL` | Use existing Singapore instance |
| C5 | Supabase service role key | Supabase dashboard → Project Settings → API → `service_role` (secret) | `.env` as `SUPABASE_SERVICE_KEY` | NEVER commit — bypasses RLS. Used by ingestion pipeline only. |
| C6 | Supabase anon key | Same page | `.env` as `SUPABASE_ANON_KEY` | Lower-privilege alternative |
| C7 | Supabase readonly password | Created manually (see §7 Database roles) | `.env` as `SUPABASE_READONLY_PASSWORD` | Used by SQL agent. SELECT-only grants. |
| C8 | Telegram bot token | Telegram → `@BotFather` → `/newbot` → follow prompts | `.env` as `TELEGRAM_BOT_TOKEN` | Save the token immediately; BotFather shows once |
| C9 | Telegram chat IDs | Message the bot, then `https://api.telegram.org/bot<TOKEN>/getUpdates` | `.env` as `TELEGRAM_CHAT_ID_RAJAT` (and `_AYUSHI` in V2) | Your numeric chat ID with the bot |
| C10 | Gmail OAuth2 credentials | Google Cloud Console → Enable Gmail API → Create OAuth2 credentials | `credentials/gmail_oauth.json` (GITIGNORED) + `credentials/gmail_token.json` (GITIGNORED) | OAuth2 flow; token auto-refreshes. Set up explicit token expiry monitoring. |
| C11 | HDFC credit card PDF password | Your HDFC NetBanking / card account | `credentials.yaml` under `hdfc_cc_<last4>` | Usually `NAME+DDMM` format |
| C12 | ICICI credit card PDF password | ICICI card account | `credentials.yaml` under `icici_cc_<last4>` | Pattern varies |
| C13 | AMEX credit card PDF password | AMEX card account | `credentials.yaml` under `amex_cc_<last4>` | Usually first 4 letters of name + DDMM |
| C14 | Zerodha: Kite Connect API key + secret | Kite Connect developer portal | `.env` as `KITE_API_KEY` + `KITE_API_SECRET` | If using API; otherwise use Console CSV export manually |
| C15 | Zfunds/MF CAS password | CAMS/KFintech MF CAS download portal | `credentials.yaml` under `mf_cas_statement` | Usually PAN or email |
| C16 | SMS forwarding app | Android app like "SMS Forwarder" or "SMS to Gmail" — one-time setup | Phone only | Forwards ICICI/HDFC transaction SMS to your Gmail |
| C17 | External Heartbeat Ping URL | healthchecks.io | `.env` as `HEALTHCHECK_URL` | Pinged every 15 minutes by APScheduler |

### `credentials.yaml` template (copy this into your repo as `credentials.example.yaml`, actual file is gitignored)

```yaml
# credentials.yaml — GITIGNORED — real values never committed
# Each key is looked up by sender/card identifier during PDF decryption.

hdfc_cc_XXXX:
  pattern: "NAME_DDMM"
  value: "<your password here>"

icici_cc_XXXX:
  pattern: "custom"
  value: "<your password here>"

amex_cc_XXXX:
  pattern: "NAME_DDMM"
  value: "<your password here>"

paytm_statement:
  value: null   # Paytm uses XLSX export — no password needed

mf_cas_statement:
  pattern: "PAN_or_email"
  value: "<PAN in uppercase or registered email>"

zerodha_statement:
  pattern: "PAN"
  value: "<PAN in uppercase>"
```

### `.env` template (copy as `.env.example`, actual file gitignored)

```
# LLM providers — per §6 model routing (all routed via LiteLLM)
ANTHROPIC_API_KEY=          # for sql_agent, affordability_reasoning
GEMINI_API_KEY=             # primary for PDF extraction, briefs, embeddings
GROQ_API_KEY=               # for merchant_categorization
# OLLAMA_BASE_URL=http://localhost:11434   # V2+ if running local models

# Supabase
SUPABASE_URL=
SUPABASE_SERVICE_KEY=       # ingestion pipeline (full write)
SUPABASE_ANON_KEY=
SUPABASE_READONLY_PASSWORD= # SQL agent (SELECT only)

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID_RAJAT=
# TELEGRAM_CHAT_ID_AYUSHI=   # V2

# Zerodha (optional — if using Kite Connect API)
# KITE_API_KEY=
# KITE_API_SECRET=

# Runtime
FINANCE_INBOX_PATH=/Users/rajat/finance-inbox
FINANCE_BACKUP_PATH=/Users/rajat/finance-backups
TIMEZONE=Asia/Kolkata
```

### Security rules (non-negotiable)
1. `credentials.yaml`, `.env`, and `credentials/` directory are in `.gitignore` before first commit.
2. Only the `.example` versions are committed.
3. No real values in this PRD, in the repo, in chat logs, or in any shared artifact.
4. Repo is private at creation. Public fork only from a sanitized branch with all bank names and personal data stripped.
5. Supabase `service_role` key treated like a root password — bypasses Row Level Security. Used only by ingestion pipeline, never by SQL agent.
6. SQL agent connects via `finance_agent_readonly` role — SELECT only, no mutation possible.
7. Gmail OAuth token expiry must be monitored. Alert if token refresh fails.

---

## 13. GitHub repo setup

**Repo is created locally and pushed to GitHub by Rajat, not by the agent.** Commands below run in your terminal on the Mac.

### Repo strategy
- **`main` branch:** private-safe, real config, actual usage
- **`public-template` branch (later):** sanitized version — no bank names, no personal data, generic examples — this is the portfolio-piece fork

### Step-by-step (run in terminal)

```bash
# 1. Create local directory
mkdir -p ~/projects/personal-finance-agent
cd ~/projects/personal-finance-agent

# 2. Initialize git
git init
git branch -M main

# 3. Create essential files
cat > .gitignore <<'EOF'
# Secrets — NEVER commit
.env
.env.*
!.env.example
credentials.yaml
!credentials.example.yaml
credentials/
!credentials/.gitkeep
*.key
*.pem

# Python
__pycache__/
*.py[cod]
*.egg-info/
.venv/
venv/
.pytest_cache/
.mypy_cache/

# macOS
.DS_Store

# Editors
.vscode/
.idea/

# Finance data
finance-inbox/
finance-backups/
*.pdf
*.csv
*.xlsx

# Logs
*.log
logs/
EOF

cat > README.md <<'EOF'
# Personal Finance Agent

A 24/7 conversational personal finance agent built in Python.
Ingests across Indian banking rails (ICICI, HDFC, Paytm, Zerodha),
categorizes intelligently, reasons about affordability and goals.
Interface: Telegram. Stack: aiogram + LiteLLM + Supabase.

Private repo — do not fork without sanitization.

See `PRD.md` for full architecture.
EOF

# 4. Copy the PRD file into the repo root
#    mv ~/Downloads/PRD.md ./PRD.md

# 5. Create directory scaffolding
mkdir -p skills/finance/{ingestion/parsers,normalization,categorization,reasoning,nudging,monitoring,privacy,reconciliation,commands,lib,tests/golden_fixtures}
mkdir -p config
mkdir -p credentials
mkdir -p migrations
mkdir -p finance-inbox/processed
touch skills/finance/__init__.py
touch credentials/.gitkeep

# 6. License audit file
cat > LICENSE_AUDIT.md <<'EOF'
# License Audit

## Direct dependencies (MIT / Apache-2 / Unlicense — safe)
- aiogram: MIT
- litellm: MIT
- bout: MIT
- casparser: MIT (default PDFMiner backend only — do NOT use [fast] extra, it pulls AGPL PyMuPDF)
- mftool: MIT
- pykiteconnect: MIT
- pikepdf: MPL-2.0
- pdfplumber: MIT
- camelot-py: MIT
- rapidfuzz: MIT
- greggles/mcc-codes: Unlicense
- xaneem/hdfc-credit-card-statement-parser: MIT (forked)
- saurabhgupta050890/transaction-sms-parser: MIT (ported TS→Py)
- bankstatementparser dedup pattern: Apache-2

## Reference only (AGPL/GPL — DO NOT import code)
- sarim2000/pennywiseai-tracker: AGPL-3 (SMS patterns — 44 banks)
- ananthakumaran/paisa: AGPL-3
- ritesh-kanwar/Cashiro: GPL-3 (UPI parsing patterns)
- maybe-finance/maybe: AGPL-3, archived
- firefly-iii/firefly-iii: AGPL-3 (dedup pattern replicated, not imported)
- bahuma20/firefly-iii-ai-categorize: AGPL-3
- HarrisonTotty/tcat: GPL-3 (YAML-regex taxonomy pattern)
- python-telegram-bot: LGPL-3 / GPL-3
EOF

# 7. First commit — only safe files
git add .gitignore README.md PRD.md LICENSE_AUDIT.md
git commit -m "Initial scaffold: PRD V2, gitignore, license audit"

# 8. Create GitHub repo (via gh CLI)
gh auth login   # one-time, skip if already logged in
gh repo create personal-finance-agent --private --source=. --remote=origin --push

# 9. Create .env and credentials templates
# (see §12 for template contents — copy from PRD)
cp .env.example .env
cp credentials.example.yaml credentials.yaml
# edit both to fill in real values — DO NOT git add them
```

### Repo URL

*To be populated by Rajat after running step 8 above. Example:*
`https://github.com/<your-username>/personal-finance-agent`

---

## 14. Risks & mitigations

| # | Risk | Mitigation |
|---|---|---|
| R1 | PDF parser failures across 3 CC formats | OSS parsers for ICICI (`bout`) and HDFC (`xaneem` fork) de-risk 2 of 3. Statement-total validator catches silent corruption. Human-in-the-loop review in Month 1. Budget 1.5x time for Week 2. |
| R2 | HDFC CC format change (last changed Sept 2025) | Version-tagged parsers (`hdfc_cc_v1.py`, `hdfc_cc_v2.py`). Golden-fixture regression tests from Month 2. |
| R3 | Credential leak | Gitignored files; security rules in §12; repo private; no real values in any shared artifact; `LICENSE_AUDIT.md` maintained |
| R4 | Paytm ingestion gaps | Using XLSX export (more reliable than PDF). Duplicate-safe ingestion means over-uploading is free; worst case is lag, not loss |
| R5 | ADHD-driven review abandonment | Weekly review is zero-effort Telegram tap flow; if skipped, categorization degrades gracefully, no hard failures |
| R6 | Wrong income classification (bonus counted as recurring) | Agent always asks on non-matching credits; payslip upload makes it deterministic. Affordability engine uses only `recurring_salary` for baseline. |
| R7 | Privacy leak between users (V2) | Read-only SQL role for agent queries; Postgres RLS policies defined before V2 onboarding; visibility filter in `db.py` as defense-in-depth |
| R8 | Supabase service key exposure | Only in `.env`, never in commits, never in chat. SQL agent uses `readonly` role, not service key. |
| R9 | Model routing misconfiguration (weak model on high-stakes task) | `stakes: high` flag in routing config; `/model` command requires `--confirm` to switch high-stakes tasks; all switches commit to Git for audit |
| R10 | Free-tier rate limit hit mid-day | LiteLLM handles fallback automatically per `fallbacks` config; 429s retried against fallback model |
| R11 | Mac sleeps / misses cron runs | `pmset` tweaks, caffeinate, prevent-sleep-on-power-adapter; ingestion pipeline is catch-up safe via `import_hash` dedup; heartbeat monitoring alerts on missed runs |
| R12 | Free-tier data usage (Google may train on free-tier prompts) | PDF extraction (contains financial data) routes to Gemini free tier — accept this tradeoff for V1. Affordability reasoning and SQL agent route to paid Claude (no training). Revisit if routing high-sensitivity tasks to free tier. |
| R13 | Gmail OAuth token expiry | Monitor token refresh in heartbeat. Alert immediately if Gmail scan returns auth error. Token refresh handled by `google-auth` library. |
| R14 | LiteLLM breaking changes | Pin LiteLLM version in `pyproject.toml`. Test before upgrading. Fallback: unwrap to direct SDK calls (the `llm.py` wrapper makes this a localized change). |
| R15 | `bout` / `casparser` library abandonment | Both are small, well-scoped libraries. If abandoned, fork and maintain — the code is simple enough to own. |
| R16 | LLM-generated SQL mutation | Read-only DB role + SQL validator layer (whitelist SELECT only). Defense in depth: two independent safeguards. |
| R17 | Silent corruption in CC extraction | Statement-total validator (primary defense), SMS reconciliation (secondary), golden-fixture regression (tertiary, Month 2+). |
| R18 | Supabase data loss | Daily `pg_dump` to local `~/finance-backups/`. Monthly copy to external drive. Defined as Week 1 deliverable. |
| R19 | macOS auto-update reboots | Disable auto-restart for updates. Heartbeat monitoring catches if the system goes down after a forced restart. |

---

## 15. Open items / deferred decisions

1. **Privacy response style** when Ayushi asks about Rajat's private data (A/B/C options) — decide before V2.
2. **Admin emergency override** — decide with Ayushi before V2 onboarding.
3. **Forecasting >3 months** — V2+.
4. **Tax reasoning from payslips + Form 16** — V2+. `Form16x` (MIT) library available.
5. **Goal-based auto-savings** — V2+.
6. **A/B test mode for model routing** — V1.5+; LiteLLM makes this trivial when ready.
7. **Fully local mode via Ollama** — V2+; depends on spare Mac RAM (need 32GB+ for meaningful models). LiteLLM supports Ollama natively.
8. **Free-tier data policy acceptance** — Gemini free-tier prompts may be used to improve Google models. Accepted for PDF extraction and merchant categorization (V1 tradeoff). Revisit if routing affordability reasoning to free tier.
9. **Data retention policy** — define at month 6. Consider monthly rollup summaries for data >12 months old.
10. **RLS policy definition** — design Postgres Row Level Security policies before V2. Hard prerequisite for Ayushi onboarding.

---

## 16. Success criteria for V1

- [ ] 90%+ of transactions auto-ingested without manual intervention (30-day measurement)
- [ ] Categorization accuracy >85% after month 1 correction cycles
- [ ] Morning brief delivered daily for 30 consecutive days
- [ ] "Can I afford X" reasoning produces a numerically correct, structurally complete answer
- [ ] Zero credential leaks to the repo (enforced by gitignore + pre-commit check)
- [ ] Rajat uses the agent weekly without nagging reminders
- [ ] Onboarding Ayushi takes ≤1 hour when V2 kicks off
- [ ] Model routing works end-to-end: primary model switch + fallback on failure verified via `/model` commands
- [ ] Monthly LLM spend stays under ₹1,500 (soft target), under ₹3,000 (hard ceiling)
- [ ] Statement-total validator catches ≥90% of extraction errors (measured against manual spot-checks)
- [ ] **External healthchecks.io heartbeat** fires alert within 30 min of Mac going down (verified by test-shutdown once before Week 2)
- [ ] **Internal heartbeat** fires Telegram alert within 30 minutes of any per-component failure
- [ ] **Dual-model Month 1 calibration completed** for all three CC formats within ₹1,000 total spend; per-bank ground-truth documented
- [ ] **Affordability engine refuses or caveats** every query run while the maturity guard is tripped (tested end-to-end before Week 5 closes)
- [ ] Daily backup runs for 30 consecutive days without failure; one restore drill completed
- [ ] SQL agent never executes a mutation query (verified by audit log + readonly role enforcement)
- [ ] `/dedup` resolves every `possible_duplicate` pair logged in Month 1 (zero left unresolved)

---

## 17. Build-mode entry checklist

Before starting Week 1 build work:
- [ ] This PRD (V2.1, including §19 decisions log) reviewed and committed to the repo
- [ ] GitHub repo created (private) and `gh repo view` works
- [ ] `LICENSE_AUDIT.md` committed
- [ ] `.env.example` and `credentials.example.yaml` committed; real versions gitignored
- [ ] Python 3.11+ available; virtualenv created
- [ ] Core dependencies installable: `pip install aiogram apscheduler fastapi uvicorn pikepdf pdfplumber supabase bout casparser mftool rapidfuzz` + `litellm` pinned to a specific stable version recorded in `pyproject.toml`
- [ ] Telegram bot created via BotFather; token stored in `.env`
- [ ] **Secondary Telegram bot created** for heartbeat/failure alerts (independent from main bot); token stored in `.env`
- [ ] Supabase project confirmed accessible; service key in `.env`
- [ ] `finance_agent_readonly` role created in Supabase; password in `.env`; **verified role cannot execute `INSERT`/`UPDATE`/`DELETE`**
- [ ] Anthropic API key in `.env` with monthly budget cap set (recommend ₹1,500 soft / ₹3,000 hard); **separate ₹1,000 one-time budget for Month 1 dual-model calibration (Week 2a)**
- [ ] Gemini API key obtained from Google AI Studio; stored in `.env`
- [ ] Groq API key obtained from console.groq.com; stored in `.env`
- [ ] Gmail API enabled in Google Cloud Console; OAuth2 credentials downloaded to `credentials/gmail_oauth.json`
- [ ] **healthchecks.io account created** (or equivalent external watchdog); check endpoint URL in `.env` as `HEALTHCHECK_URL`; alert channel verified (manually pause the cron → confirm alert fires)
- [ ] `config/model_routing.yaml` scaffolded with defaults from §6
- [ ] Spare Mac ops configured:
    - [ ] Prevent sleep on power adapter (System Settings → Battery)
    - [ ] Disable automatic macOS updates (System Settings → Software Update → Automatic Updates → off)
    - [ ] Python process added as Login Item (or launchd plist created)
    - [ ] "Start up automatically after power failure" enabled
    - [ ] `pmset` tweaks applied (disable Power Nap, auto-sleep, standby)
    - [ ] Monthly scheduled restart cron (first Sunday, 3 AM)
- [ ] Mac RAM noted — if ≥32GB, Ollama is an option for V2; if <32GB, document the constraint
- [ ] `~/finance-backups/` directory created; external drive location identified for monthly copy

When every box is checked, Rajat says "go" and we start Week 1 build.

---

## 18. OSS dependency manifest

Quick reference for all external libraries and their role in the system.

| Library | PyPI | License | Role | Section |
|---------|------|---------|------|---------|
| `aiogram` | `pip install aiogram` | MIT | Telegram bot framework | §5.1 |
| `APScheduler` | `pip install apscheduler` | MIT | Cron scheduling | §5.1 |
| `FastAPI` | `pip install fastapi uvicorn` | MIT | Health endpoint + admin API | §5.1 |
| `LiteLLM` | `pip install litellm` | MIT | Multi-model LLM routing | §6 |
| `bout` | `pip install bout` | MIT | ICICI bank/CC statement parser | §8, F1/F2 |
| `casparser` | `pip install casparser` (NOT `[fast]`) | MIT | MF CAS statement parser | §8, F10 |
| `mftool` | `pip install mftool` | MIT | AMFI NAV lookups | §8, F10 |
| `pykiteconnect` | `pip install kiteconnect` | MIT | Zerodha API access | §8, F10 |
| `pikepdf` | `pip install pikepdf` | MPL-2.0 | PDF password decryption | §8, F2 |
| `pdfplumber` | `pip install pdfplumber` | MIT | PDF table extraction | §8, F1/F2 |
| `camelot-py` | `pip install camelot-py[cv]` | MIT | PDF table extraction (complex layouts) | §8 |
| `rapidfuzz` | `pip install rapidfuzz` | MIT | Fuzzy string matching for merchants | §8, F6 |
| `supabase` | `pip install supabase` | MIT | Database client | §7 |
| `watchdog` | `pip install watchdog` | Apache-2 | Local folder file watcher | §8, F5 |
| `google-auth` | `pip install google-auth google-api-python-client` | Apache-2 | Gmail API OAuth2 | §5.1, F1 |

### Forked/ported (not pip-installable)
| Source | License | Role |
|--------|---------|------|
| `xaneem/hdfc-credit-card-statement-parser` | MIT | HDFC CC parser (forked + extended) |
| `joeirimpan/hdfc-cc-parser-rs` | MIT | HDFC Infinia format reference (Rust → Python rewrite) |
| `saurabhgupta050890/transaction-sms-parser` | MIT | Indian bank SMS parser (TypeScript → Python port) |
| `greggles/mcc-codes` | Unlicense | MCC taxonomy seed data |
| `mitulshah/global-financial-transaction-classifier` | MIT | DistilBERT baseline categorizer (HuggingFace) |
| `mitulshah/transaction-categorization` | MIT | 4.5M training samples (HuggingFace) |
| `bankstatementparser` dedup pattern | Apache-2 | Dedup hash strategy reference |

---

## 19. V2 → V2.1 Decisions Log

V2 was produced by a research-driven rewrite of V1 (OpenClaw → Python-native, custom router → LiteLLM, OSS leverage for parsers). Lead-reviewer diff of V2 vs. the V1 §18 locked commitments found six items that had dropped or weakened silently during the rewrite, plus several minor regressions. This log records the disposition of each in V2.1.

> **Rule of precedence:** where earlier sections of this PRD conflict with V2.1 decisions, the body of this PRD (which has been updated inline) is authoritative. This log is the audit trail, not the source of truth.

### 19.1 The six restored commitments

| # | V1 §18 commitment | V2 state | V2.1 fix |
|---|---|---|---|
| R1 | **External heartbeat** — 15-min ping to healthchecks.io (or equivalent); alerts via secondary Telegram bot when ping goes silent. Catches complete-Mac-down scenarios that an internal watchdog cannot. | V2 described `heartbeat` as "External watchdog or APScheduler self-check" — ambiguous, Week 1 only scaffolded the internal self-check. | External healthchecks.io ping is now mandatory (F22, §7 `heartbeat` table note, Week 1, §12 C17 `HEALTHCHECK_URL`, §17 checklist explicit setup + test-shutdown drill). Internal + external coexist; roles distinguished in §5 and Week 1. |
| R2 | **`txn_hash` two-mode formula** — time-bearing sources use `txn_time` in the hash; PDF-derived sources use `pdf_content_hash` to disambiguate same-day same-amount rows. Prevents silent collapse of e.g. two ₹350 Swiggy orders on the same card same day. | V2 removed `txn_time` column entirely, unified hash was `sha256(account_id|date|amount|normalized_description|source_ref)`. Two identical-looking rows in the same PDF would collide and one would be silently dropped. | `txn_time` column restored (nullable), `pdf_content_hash` column added, `source_row_ordinal` stored for display but not hashed. Dedup strategy in §7 rewritten as explicit two-mode formula. §5.2 data flow updated to reference §7 rather than inline a wrong formula. |
| R3 | **Affordability data-quality maturity guard** — engine refuses confident numeric answer when <60 days history, >10% recent outflow uncategorized, or any last-90-day statement failed totals check. `/afford --force` allows caveated override. Prevents confidently-wrong answers on Month 1 junk data. | V2 omitted the guard entirely. §9 Rule 5 described affordability as "structured" but had no maturity gate. | Guard formalized as §6.5 with three numeric thresholds. §9 Rule 5 rewritten to reference §6.5 and the `/afford --force` override. Week 4 deliverable updated. Success criteria (§16) include an end-to-end test that the guard refuses or caveats while tripped. |
| R4 | **Dual-model Month 1 calibration** — first 2–3 statements per bank extracted via both Gemini Flash and Claude Haiku, diff reviewed manually. Catches merchant/date/category misreads that the totals validator cannot (e.g., right amount attached to wrong merchant). Hard cost cap ₹1,000 total. | V2 demoted to §15 open item 10: "Decide at Week 2 start." Calibration is time-limited (only meaningful during the backfill window) — "decide later" effectively killed it. | Recommitted as a Week 2a deliverable with explicit ₹1,000 cap, live tracking in `ingestion_log`, auto-halt near the ceiling. Removed from §15 open items. §17 checklist has a separate line-item budget for this spend. |
| R5 | **`goals.current_amount` — computed, not stored** — V1 §18.8 explicitly rejected a stored column because stored computed values go stale. | V2 reintroduced the column with comment "manually updated or computed from tagged transactions." Two implicit sources of truth = drift bug. | Kept the column for query convenience but locked it as a **cache written exclusively by `budget_engine.py` on its weekly run**. Schema comment is now explicit: "Never written from anywhere else. Treat as stale between updates; authoritative answer is always the computation, not the column." Equivalent to removing it for correctness purposes while preserving V2's performance intent. |
| R6 | **`/dedup` command** — `confirm` and `merge` subcommands to resolve `possible_duplicate` pairs. Without this, flagged pairs can only be handled by hand-editing Supabase. | V2 kept the `possible_duplicate` flag in `ingestion_log` but removed the resolution command from §10. No surfaced resolution path. | `/dedup confirm <pair_id>` and `/dedup merge <pair_id> <keep_id>` added to §10. `commands/dedup.py` added to §8 skill structure. Success criteria: zero unresolved `possible_duplicate` rows at end of Month 1. |

### 19.2 Minor items also fixed in V2.1

- **Nudge matrix wording:** "Transactions > ₹5K" → "Outflow transactions > ₹5K" (and Loud's "All transactions" → "All outflow transactions"). A large salary credit should not trigger a spend alert.
- **LiteLLM version pin:** Week 1 and §17 now require a specific pinned version in `pyproject.toml`, not just "install latest." R14 already flagged the need but no version was locked.
- **Currency `CHECK` constraint dropped:** V2 hardcoded 7 currencies in a SQL check, which would silently reject an 8th (JPY, CHF, etc.) in a future Zerodha foreign-stock trade. Replaced with an unconstrained `DEFAULT 'INR'`. V1 primarily INR; schema now forward-compatible without migration.
- **`commands/dedup.py`** added to §8 skill tree.
- **`/afford --force`** added to §10 commands.
- **Secondary Telegram bot** for alert channel now explicit in §17 (the heartbeat/failure alerts come from an independent bot — if the main bot is the one that's broken, you still hear about it).

### 19.3 V2 decisions V2.1 preserves (i.e. did NOT revert)

All of V2's research-driven architectural wins are retained unchanged:

- Python-native stack (aiogram + APScheduler + FastAPI) — V1's OpenClaw path was infeasible per the GitHub research (OpenClaw is TypeScript; "PythonClaw" was vaporware).
- LiteLLM replacing V1's three-layer custom router — 44k-star MIT library with native fallback, retry, and Supabase cost logging.
- OSS leverage (`bout` for ICICI, `xaneem` fork for HDFC, `casparser` + `mftool` for MF/NAV, `saurabhgupta050890` SMS parser port, `greggles/mcc-codes` taxonomy seed, `mitulshah` DistilBERT categorizer baseline).
- Paytm XLSX export over PDF parsing; Kite Connect API over Zerodha PDFs.
- `LICENSE_AUDIT.md` as a first-class repo artifact enforcing the AGPL/GPL reference-only discipline.
- Internal `heartbeat` table with per-component tracking (now complementary to external ping, not a replacement for it).
- `ingestion_log.declared_total` / `extracted_total` columns for integrity audit.

### 19.4 Sign-off

V2.1 scope is **locked** as of this revision. All six V1 §18 commitments are restored or strengthened; V2's research-driven architectural improvements are preserved. Build begins at Week 1 once §17 Build-mode entry checklist is fully green.

*— Lead Reviewer sign-off: R1–R6 resolved per §19.1; minor regressions cleaned up per §19.2; V2 architectural wins preserved per §19.3. Proceed to Week 1.*

### 19.5 Post-lock amendment — Mode B hash formula (2026-04-21)

During Week 1 plan review (`tasks/todo.md`), a deeper analysis surfaced two flaws in the V2.1-locked Mode B hash formula:
1. Including `source_ref` in the hash broke cross-channel dedup — the same PDF delivered via Gmail attachment vs Telegram upload would produce different hashes despite identical content.
2. Excluding `source_row_ordinal` from the hash failed to disambiguate intra-PDF same-day same-amount rows — the exact two-Swiggy failure the mode split was designed to prevent.

Dedup strategy rewritten above:
- `source_ref` moved to audit-only (removed from hash)
- `source_row_ordinal` added to Mode B hash
- `parser_version` added to both modes as a deliberate-upgrade signal; new column on `transactions`
- Each parser module exports `__parser_version__`; ingestion threads it into the hash
- `pdf_content_hash` retained in Mode B hash (source-document identity)
- Rolling-statement overlap explicitly acknowledged as a known tradeoff with secondary-fuzzy-pass mitigation; deeper reconciliation deferred to a Week 3 decision
- Future Option X (txn_reference override) documented as a post-V1 upgrade path

Schema impact: `parser_version TEXT` column added to `transactions`; `source_row_ordinal` comment updated to reflect that it is now hashed; `source_ref` comment updated to "audit-only."

---

*End of V2.1 PRD.*
