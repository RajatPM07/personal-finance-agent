# CLAUDE.md — Personal Finance Agent

Personal finance agent for Indian banking (ICICI + AMEX primary, more later).
Python + Supabase + Telegram. Single-user, 24/7 on spare Mac.
See `PRD.md` for scope; `tasks/todo.md` for current plan; `tasks/lessons.md` for patterns.

## Stack
- Python 3.11+, single `app.py` orchestrating aiogram bot + AsyncIOScheduler + FastAPI (port 8765)
- Supabase (Postgres + pgvector); sync client wrapped via `adb()` in async contexts
- LiteLLM → Gemini Flash (extraction), Groq Llama (categorization), Claude Sonnet (reasoning)
- PDF: pikepdf (decrypt), pdfplumber (text), camelot-py[cv] (tables), `bout` (ICICI)
- Ops: launchd for process + backup supervision (NOT APScheduler)

## Commands
- `make test` / `make lint` / `make typecheck` — always run all three before claiming done
- `python app.py` — foreground dev run
- `launchctl print gui/$(id -u)/com.rajat.pfa.app` — prod status
- `pytest tests/test_<parser>.py -v` — parser smoke; fixtures in `tests/golden_fixtures/` (gitignored)

## Directory map
- `app.py` — entry point, signal handlers, subsystem wiring
- `skills/finance/lib/{settings,db,llm,hashing,logging_setup}.py` — core helpers
- `skills/finance/bot/` — aiogram + heartbeat middleware + /ask command (W4.1)
- `skills/finance/agents/` — W4.1 SQL agent: `sql_agent.py` (orchestrator), `judge.py` (prompt builder/parser), `sql_validator.py` (sqlglot static validation), `review_config.py` (tunable thresholds)
- `skills/finance/monitoring/` — async alerts (+ Anthropic balance check), heartbeat jobs, `/health`
- `skills/finance/nudging/morning_brief.py` — daily 09:00 IST adaptive brief (new txns else MTD pacing); watermark in agent_memory
- `skills/finance/ingestion/parsers/<bank>_parser.py` — each exports `__parser_version__`
- `scripts/backup_supabase.py` — standalone, launchd-invoked
- `migrations/` — `001_init`, `002_verify_readonly`, `003_seed`; `*.local.sql` gitignored for real secrets/PII
- `config/model_routing.yaml` — task → model + fallbacks
- `config/sql_agent_review.yaml` — W4.1 reviewer-layer tunables (confidence threshold, retry limit, balance warning)
- `config/db_schema_for_judge.md` — curated schema excerpt for W4.1 judge prompts
- `launchd/` — plists for app + backup
- `tests/sql_agent_calibration/` — W4.1 calibration harness (pairs.yaml gitignored)
- `tasks/preconditions-notes.md` — gitignored; Task 0 findings (bout API, LiteLLM version, table provenance). Check here before guessing library APIs.

## Invariants — don't break without explicit discussion

1. **Async→sync bridging goes through `adb()`** only (see `lib/db.py` docstring). Direct `.execute()` in async handlers blocks the loop.
2. **Every `adb()` chain must end at `.execute()`** — `.insert(payload)` without `.execute()` silently builds a query and discards it. We've hit this bug twice.
3. **`import_hash` = Option Y** — `sha256(account || date || amount || normalized_desc || pdf_content_hash || source_row_ordinal || parser_version)` for PDF rows; Mode A omits `ordinal`+`pdf`. Never add `source_ref` to the hash (breaks cross-channel dedup).
4. **Every parser exports `__parser_version__`** — ingestion threads it into the hash. Bumping forces fresh ingest + reconciliation; treat bumps deliberately.
5. **Versioned parser filenames when format changes** — `hdfc_cc_v1.py`, `hdfc_cc_v2_infinia.py`. HDFC CC format changed Sept 2025. Don't mutate old parsers in place.
6. **Backups run under launchd, not APScheduler.** Never register `backup_supabase.py` on the scheduler — blocks the event loop.
7. **Telegram bot is whitelist-only.** `_is_rajat(message)` at the top of every handler; non-match → silent return.
8. **`readonly_client()` shipped W3.5** (`skills/finance/lib/db.py`, psycopg3 + Supavisor pooler, `SUPABASE_READONLY_PASSWORD`). Available for W4.1 SQL agent. Role GRANTs are the primary security boundary; `conn.read_only` and `statement_timeout='10s'` are defense-in-depth. Note: `-c options` does NOT survive the Supavisor pooler — SET post-connect is the durable path (see `tasks/preconditions-notes.md` W3.5).
9. **Secrets split:** `.env` for flat env vars; `credentials.yaml` *only* for keyed PDF passwords.
10. **Parser golden fixtures contain real PII** — `tests/golden_fixtures/*.pdf` is gitignored. Tests skip when missing; never commit real statements.
11. **Never pin a library version, model ID, or library API surface from memory or from a summary document.** Run `help()`, `pip index versions`, or a one-line smoke call and write the output to `tasks/preconditions-notes.md`. Trust verified output, not recollection. (Lesson cost: `bout` was cited as a PDF parser when it's CSV-only; `claude-sonnet-4-7` was cited when current Sonnet is 4.6. Both would have shipped without Task 0 verification.)
12. **Judge-prompt content (schema_excerpt + result_preview) carries `credentials.yaml`-level sensitivity.** LiteLLM's `success_callback` in `lib/llm.py` logs metadata only — never extend it to capture `messages=` or rendered prompts without an explicit privacy review. Real schema names + the first 3 result rows = real PII. Regression-tested by `tests/test_llm_logging_invariant.py`. Per spec §4.5.

## Blocked patterns

- No AGPL code copied from PennyWise, Cashiro, Paisa, Firefly III, Maybe, Sure. Reference-only, clean-room reimplement. See `LICENSE_AUDIT.md`.
- No `[fast]` extra on `casparser` (pulls AGPL PyMuPDF). Default PDFMiner backend.
- No `print` in long-running paths; use `skills.finance.lib.logging_setup`.
- No raw card/account numbers or OTPs in prompts or INFO-level logs. Redact first.
- No LiteLLM calls bypassing `llm()` in `lib/llm.py` — single routing point; cost callback only fires through it.
- No plaintext secrets committed; `*.local.sql` gitignored; pre-commit hook blocks.

## LLM routing

All calls: `llm(task, prompt, system=None)` in `lib/llm.py`. Task → model in `config/model_routing.yaml`. Known tasks (Week 1): `pdf_extraction`, `merchant_categorization`, `sql_agent`. Cost ceilings per preconditions (Anthropic ₹1,500 soft / ₹3,000 hard). On overage: fail loud, don't silently degrade.

## Testing

- Parsers: **golden-file TDD** (real PDF → expected rows). Skip when fixture missing.
- Assert ordinals contiguous `1..N` in every parser test — catches silent ordering drift that corrupts `import_hash`.
- LLM calls mocked by default; live smoke behind gitignored `scripts/smoke_*.py`, deleted after.
- Every hash function needs tests for (a) stability, (b) difference-on-each-input, (c) malformed-input rejection (e.g. naive datetime).

## When things go wrong

- Parser crash on real statement → log to `ingestion_log` with `status='failed'` + raw PDF hash; don't crash the bot.
- LLM returns malformed JSON → retry once with `response_format`, then `status='needs_review'`.
- Any correction from me → append to `tasks/lessons.md`. Read that file before starting sessions.

## Data state (as of 2026-06-24)

### Ingested accounts
| Account | Type | Coverage |
|---------|------|----------|
| ICICI Savings (772201501896) | Savings | Apr 2025 – Jun 2026 |
| ICICI CC (XXXX 1008) | Credit Card | Dec 2025 – Jun 2026 |
| AMEX CC | Credit Card | Jan 2026 – Jun 2026 |
| Paytm UPI | UPI | Jan 2026 – Jun 2026 |

### Pending ingestion
- **HDFC CC (XXXX 0950)** — ₹61,600 paid to it Jan–Jun 2026, statements not yet provided
- **OneCard** — exists (payments visible in ICICI Savings), no statements
- **Ayushi's accounts** — wife's GPay/WhatsApp payments not in system (rent Jan–May, food maid salary)

### ICICI Savings parser — two formats
The parser (`skills/finance/ingestion/parsers/icici_savings.py`) handles two layouts auto-detected by header:
- **Monthly statement** (password-protected PDF) → `icici-savings-pdf/v2`
- **OpTransactionHistory** (not password-protected, "Statement of Transactions" header, numbered rows `{seq} DD.MM.YYYY amount balance`) → `icici-savings-opt/v1`

OPT format UPI transactions are skipped (D1 rule — Paytm is source of truth). Large non-Paytm UPI transfers (e.g. rent to Sunil Bhatkar) must be manually inserted.

### psycopg3 in scripts
One-off scripts must use `psycopg.connect(DB_URL, prepare_threshold=None)` to avoid "prepared statement _pg3_0 already exists" errors on repeated short-lived connections.

### Categories (20+, all user-created)
Household Staff, Loan Repayment, Travel, Shopping, Dining Out, Education, Rent, Food Delivery, Healthcare, Groceries, Insurance, EMI, Car & Transport, Entertainment, Transport, Subscriptions, Micro UPI, Bank Charges, Wallet Load, Personal Care, Home & Garden, Utilities, Fuel, Alcohol, Courier, Recreation, Government Services, Miscellaneous, Needs Review, Self Transfer, Other

### Known merchant mappings
- `Reliance Smart / RelianceRetailLim` → Groceries / Bhopal Parents Visit
- `Rajat Sharma VRN 34161fa820328aa2d44096e0` → Fuel / FASTag Recharge (Airtel)
- `LPMUMXX43422 RAJAT SHARMA` → Loan Repayment (one-time prepayment, not recurring)
- `ATD/Auto Debit CC0xx1340` → Self Transfer / CC Bill Auto-Payment
- `Dreamplug Technologies` → Wallet Load / CC to CRED Wallet
- `PAYTM*ONE97COMMUNICATIO` → Wallet Load / CC to Paytm Wallet
- `Munni Shukhram Chaurasiya`, `Harveersingh Rawat`, `Leslie Maxie Fernandes` → Micro UPI (society tips)
- `Orbgen Technologies` → Entertainment / Cinema Food
- `Hexpress Healthcare` → Healthcare / Mounjaro Injection
- `Herbalife` → Healthcare / Mom Supplements
- `Aachho Jaipur` → Shopping (lifestyle store)
- `Datamatic 8th Floor` → Dining Out / Office Canteen
- `Callmate India` → Shopping / Home Accessories
- Airtel (all variants) → Utilities / Family Recharge

### Rent
₹70,000/month to Sunil Bhatkar (UPI: sunilbhatkar21@saraswat). Pay via Paytm going forward (was GPay — not tracked). Only June 2026 in DB from Rajat's side; Jan–May paid by Ayushi.
