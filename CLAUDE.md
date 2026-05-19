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
- `skills/finance/bot/` — aiogram + heartbeat middleware
- `skills/finance/monitoring/` — async alerts, heartbeat jobs, `/health`
- `skills/finance/ingestion/parsers/<bank>_parser.py` — each exports `__parser_version__`
- `scripts/backup_supabase.py` — standalone, launchd-invoked
- `migrations/` — `001_init`, `002_verify_readonly`, `003_seed`; `*.local.sql` gitignored for real secrets/PII
- `config/model_routing.yaml` — task → model + fallbacks
- `launchd/` — plists for app + backup
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
