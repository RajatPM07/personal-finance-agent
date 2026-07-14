# Ayushi Onboarding — Second User + Statement Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Onboard Ayushi as a proper second user with application-level (not RLS) data separation, and ingest her full statement history (2 ICICI credit cards, PhonePe UPI, ICICI savings).

**Architecture:** Ayushi becomes a real `users` row; every one of her accounts carries `accounts.user_id = <ayushi>`. Separation is enforced in the application layer, not Postgres RLS (rejected due to Supavisor pooler connection-reuse leakage risk). The Telegram bot maps each sender's chat-id → their DB `user_id`; the SQL agent receives that `user_id` and every generated query is filtered by it, with a deterministic validator guard rejecting any query missing the caller's UUID. Her historical statements are ingested via a one-off backfill script with an explicit file→(account, user) map, because filename-based routing cannot infer owner or card.

**Tech Stack:** Python 3.11+, aiogram, Supabase (psycopg3 + sync client via `adb()`), pdfplumber + pikepdf (PDF), pandas (XLS), sqlglot (SQL validation), LiteLLM, pytest golden-file TDD.

## Global Constraints

_Every task implicitly inherits these. Values copied verbatim from `CLAUDE.md`._

- **Async→sync bridging goes through `adb()` only.** Never call `.execute()` directly in async handlers.
- **Every `adb()` chain must end at `.execute()`.**
- **`import_hash` = Option Y** — `sha256(account || date || amount || normalized_desc || pdf_content_hash || source_row_ordinal || parser_version)`. Never add `source_ref`. `import_hash` includes `account_id`, so Ayushi's rows never collide with Rajat's as long as her `account_id`s differ.
- **Every parser exports `__parser_version__`.** Bumping forces fresh ingest + reconciliation — treat deliberately.
- **Telegram bot is whitelist-only** — an authorization check at the top of every handler; non-match → silent return.
- **`readonly_client()`** (psycopg3 + Supavisor pooler) is the SQL-agent DB path. Role GRANTs are the security boundary; `conn.read_only` + `statement_timeout` are defense-in-depth.
- **Secrets split:** `.env` for flat env vars; `credentials.yaml` *only* for keyed PDF passwords.
- **Golden fixtures contain real PII** — `tests/golden_fixtures/*` is gitignored. Tests skip when the fixture is missing; never commit real statements.
- **Assert ordinals contiguous `1..N` in every parser test.**
- **Judge-prompt content carries `credentials.yaml`-level sensitivity.** Do not extend `lib/llm.py` `success_callback` to log `messages=`/prompts. Regression-tested by `tests/test_llm_logging_invariant.py`.
- **No plaintext secrets committed;** `*.local.sql` gitignored; pre-commit hook blocks. Commit with `PATH="$(pwd)/.venv/bin:$PATH" git commit ...` (never `--no-verify`).
- Run `make test`, `make lint`, `make typecheck` before claiming any task done.

---

## Decisions & Corrections (read before starting)

These change the task list as originally scoped. Confirm before Task 1.

1. **ICICI CC parser needs NO calibration — verify only.** Rajat's *own* ICICI CC statements (`tests/golden_fixtures/icici_sample.pdf`) have the identical line format as Ayushi's: `DATE <11-13 digit ref#> <merchant> <reward-pts> <amount> [CR]`. The current `ROW_RE` already folds ref#+points into `raw_merchant` for both. Her statements parse today with zero changes. Stripping ref#/points into structured fields would change *Rajat's* `raw_merchant` → `import_hash` → force full re-ingest of his ICICI CC history (invariants #4/#5). That is a separate, optional, deliberate `parser_version` bump — **out of scope here.** Task 7 is verify-only.

2. **Ingestion attribution plumbing is required (was not in the original spec).** `pipeline.py` hardcodes `RAJAT_USER_ID`; `folder_watcher.ACCOUNT_IDS` maps `bank`→`account_id` with no owner. Ayushi's files also can't be routed by filename (`Monthlystatement_….pdf` carries no `icici`/`cc` token, can't distinguish her two cards, and can't tell owner). So: thread `user_id` through the pipeline (Task 4) and ingest her history via an explicit backfill script (Task 9). Live folder-watcher auto-routing of *future* Ayushi statements is deferred (noted in Task 9).

3. **SQL separation gets a deterministic guard, not LLM-trust-only.** Per the spec we instruct the LLM to filter by `user_id`, AND (Task 5) the static validator rejects any query whose text does not contain the caller's UUID literal / contains a *different* user's UUID. Pure prompt-instruction is a silent cross-user leak risk; this is the "concrete validator at the point of failure" pattern.

4. **Annual statement (`Annualstatement_FY2025-26.pdf`) is skipped** — redundant with the monthlies, different multi-card summary layout.

5. **PhonePe is her UPI source of truth** (she has no Paytm). Password `8989050872` (her phone number) → `credentials.yaml`. Two funding sub-accounts (`XXXX15`, `XX5263`) collapse to one `upi` account in V1. The ₹70,000 Jan 02 transfer to "Bank Account XXXX4891" is almost certainly rent — this ingestion closes the Jan–May rent data gap.

---

## Ayushi identity constants (used across tasks)

Fixed UUIDs chosen for this onboarding (namespace `20000000…` for Ayushi accounts to avoid collision with Rajat's `10000000…` seed):

```
AYUSHI_USER_ID          = 00000000-0000-0000-0000-000000000002
Ayushi ICICI Amazon Pay CC (4315…6007)  account_id = 20000000-0000-0000-0000-000000000001
Ayushi ICICI CC #2        (4035…4009)   account_id = 20000000-0000-0000-0000-000000000002
Ayushi PhonePe UPI                       account_id = 20000000-0000-0000-0000-000000000003
Ayushi ICICI Savings                     account_id = 20000000-0000-0000-0000-000000000004
```

(`RAJAT_USER_ID = 00000000-0000-0000-0000-000000000001`, already in `_common.py`.)

---

## File Structure

**Create:**
- `migrations/009_ayushi_onboarding.local.sql` — Ayushi `users` row + 4 `accounts` rows (gitignored; real identifiers = PII, mirrors `003_seed.local.sql`).
- `skills/finance/lib/users.py` — user identity constants + `user_id_for_chat(chat_id) -> str | None`. Single source of truth for chat-id→user_id mapping.
- `skills/finance/ingestion/parsers/phonepe_upi.py` — new PhonePe PDF parser, `__parser_version__ = "phonepe-upi/v1"`.
- `scripts/backfill_ayushi.py` — one-off, launched manually; explicit file→(parser, account_id, user_id) map; idempotent via `import_hash`.
- `tests/test_phonepe_upi_parser.py` — golden-file TDD for PhonePe.
- `tests/test_users_mapping.py` — chat-id→user_id mapping.
- `tests/test_icici_cc_ayushi.py` — verify existing parser handles her CC fixture (Task 7).
- `tests/test_icici_savings_xls_ayushi.py` — verify existing xls parser on her fixture (Task 8).

**Modify:**
- `skills/finance/lib/settings.py` — add `telegram_chat_id_ayushi: str`.
- `.env` / `.env.example` — add `TELEGRAM_CHAT_ID_AYUSHI=`.
- `skills/finance/bot/main.py` — replace `_is_rajat` with env-driven `_authorized_user_id(message)`; thread `user_id` into `/ask`.
- `skills/finance/agents/sql_agent.py` — `run_sql_agent(question, user_id, cfg=None)`; inject UUID into prompts.
- `skills/finance/agents/sql_validator.py` — add `require_user_id` guard.
- `config/db_schema_for_judge.md` — replace the single-user "DO NOT filter by user_id" note with a "MUST filter by the given user_id UUID" note.
- `skills/finance/ingestion/pipeline.py` — `ingest(..., user_id=RAJAT_USER_ID)`, `_build_insert_row(..., user_id)`.
- `skills/finance/ingestion/_common.py` — extend `Bank` literal with `"phonepe_upi"`; `detect_bank_from_filename` recognizes `phonepe`.
- `skills/finance/ingestion/folder_watcher.py` — add PhonePe to `ACCOUNT_IDS`/`EXPECTED_EXTENSION`/`dispatch_to_parser`.
- `credentials.yaml` — add `phonepe_upi_XXXX15` entry (password `8989050872`).

---

### Task 1: Seed Ayushi user + accounts

**Files:**
- Create: `migrations/009_ayushi_onboarding.local.sql`

**Interfaces:**
- Produces: `users` row `00000000-0000-0000-0000-000000000002`; four `accounts` rows under the `20000000…` namespace (see constants above).

- [ ] **Step 1:** Write `migrations/009_ayushi_onboarding.local.sql`:

```sql
-- 009_ayushi_onboarding.local.sql  (GITIGNORED — real identifiers = PII)
-- Ayushi as a proper second user. Application-level separation only (no RLS).
INSERT INTO users (id, telegram_handle, role, display_name) VALUES
  ('00000000-0000-0000-0000-000000000002', NULL, 'user', 'Ayushi')
ON CONFLICT (id) DO NOTHING;

INSERT INTO accounts (id, user_id, type, institution, identifier, nickname) VALUES
  ('20000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002', 'credit_card', 'ICICI', '6007', 'Ayushi ICICI Amazon Pay CC'),
  ('20000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000002', 'credit_card', 'ICICI', '4009', 'Ayushi ICICI CC 2'),
  ('20000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000002', 'upi',         'PhonePe', 'XXXX15', 'Ayushi PhonePe UPI'),
  ('20000000-0000-0000-0000-000000000004', '00000000-0000-0000-0000-000000000002', 'bank',        'ICICI', 'sav', 'Ayushi ICICI Savings')
ON CONFLICT (id) DO NOTHING;
```

- [ ] **Step 2:** Confirm `009_…local.sql` is gitignored: `git check-ignore migrations/009_ayushi_onboarding.local.sql` → prints the path (ignored). If not, add `migrations/*.local.sql` is already in `.gitignore` — verify.

- [ ] **Step 3:** Apply against the DB (same path as prior `*.local.sql` migrations — psycopg with `prepare_threshold=None`). Then verify:

```bash
python -c "import psycopg,os; c=psycopg.connect(os.environ['SUPABASE_DB_URL'],prepare_threshold=None); \
print(c.execute(\"select display_name from users where id='00000000-0000-0000-0000-000000000002'\").fetchone()); \
print(c.execute(\"select count(*) from accounts where user_id='00000000-0000-0000-0000-000000000002'\").fetchone())"
```
Expected: `('Ayushi',)` then `(4,)`.

- [ ] **Step 4:** Commit (the `.local.sql` is gitignored, so this commit is a no-op for that file — commit only if any tracked file changed, e.g. a `.gitignore` tweak). Otherwise skip.

---

### Task 2: User identity module + chat→user mapping

**Files:**
- Create: `skills/finance/lib/users.py`
- Create: `tests/test_users_mapping.py`
- Modify: `skills/finance/lib/settings.py`
- Modify: `.env`, `.env.example`

**Interfaces:**
- Produces: `AYUSHI_USER_ID: str`, `RAJAT_USER_ID: str`, `user_id_for_chat(chat_id: str | int) -> str | None`.
- Consumes: `settings.telegram_chat_id_rajat`, `settings.telegram_chat_id_ayushi`.

- [ ] **Step 1: Add the settings field.** In `skills/finance/lib/settings.py`, after `telegram_chat_id_rajat: str` add:

```python
    telegram_chat_id_ayushi: str = ""   # empty until Ayushi's chat id is known
```

- [ ] **Step 2:** Add `TELEGRAM_CHAT_ID_AYUSHI=` to `.env` (real value) and `.env.example` (blank).

- [ ] **Step 3: Write the failing test** `tests/test_users_mapping.py`:

```python
from skills.finance.lib import users


def test_rajat_and_ayushi_ids_distinct():
    assert users.RAJAT_USER_ID != users.AYUSHI_USER_ID


def test_maps_rajat_chat_to_rajat(monkeypatch):
    monkeypatch.setattr(users.settings, "telegram_chat_id_rajat", "111")
    monkeypatch.setattr(users.settings, "telegram_chat_id_ayushi", "222")
    assert users.user_id_for_chat("111") == users.RAJAT_USER_ID
    assert users.user_id_for_chat(111) == users.RAJAT_USER_ID   # int accepted


def test_maps_ayushi_chat_to_ayushi(monkeypatch):
    monkeypatch.setattr(users.settings, "telegram_chat_id_rajat", "111")
    monkeypatch.setattr(users.settings, "telegram_chat_id_ayushi", "222")
    assert users.user_id_for_chat("222") == users.AYUSHI_USER_ID


def test_unknown_chat_returns_none(monkeypatch):
    monkeypatch.setattr(users.settings, "telegram_chat_id_rajat", "111")
    monkeypatch.setattr(users.settings, "telegram_chat_id_ayushi", "222")
    assert users.user_id_for_chat("999") is None


def test_empty_ayushi_id_never_matches(monkeypatch):
    # Guard: an unset TELEGRAM_CHAT_ID_AYUSHI ("") must not authorize chat "".
    monkeypatch.setattr(users.settings, "telegram_chat_id_rajat", "111")
    monkeypatch.setattr(users.settings, "telegram_chat_id_ayushi", "")
    assert users.user_id_for_chat("") is None
```

- [ ] **Step 4: Run — verify fail:** `pytest tests/test_users_mapping.py -v` → FAIL (module missing).

- [ ] **Step 5: Implement** `skills/finance/lib/users.py`:

```python
"""User identity + Telegram-chat → DB user_id mapping.

Application-level user separation (no Postgres RLS). Single source of truth
for who is who. The chat→user map is derived from settings, so rotating a
chat id is a config change, not a code change.
"""
from __future__ import annotations

from skills.finance.lib.settings import settings

RAJAT_USER_ID: str = "00000000-0000-0000-0000-000000000001"
AYUSHI_USER_ID: str = "00000000-0000-0000-0000-000000000002"


def user_id_for_chat(chat_id: str | int) -> str | None:
    """Return the DB user_id for a Telegram chat id, or None if not whitelisted.

    An empty configured chat id ("") never matches, so an un-provisioned
    Ayushi id cannot accidentally authorize an empty/unknown sender.
    """
    cid = str(chat_id)
    rajat = str(settings.telegram_chat_id_rajat)
    ayushi = str(settings.telegram_chat_id_ayushi)
    if rajat and cid == rajat:
        return RAJAT_USER_ID
    if ayushi and cid == ayushi:
        return AYUSHI_USER_ID
    return None
```

- [ ] **Step 6: Run — verify pass:** `pytest tests/test_users_mapping.py -v` → PASS.

- [ ] **Step 7: Commit.** `feat(users): chat-id → user_id mapping for multi-user separation`

---

### Task 3: Bot whitelist from env + thread user into `/ask`

**Files:**
- Modify: `skills/finance/bot/main.py`

**Interfaces:**
- Consumes: `users.user_id_for_chat`, `run_sql_agent(question, user_id)` (Task 5 signature).

- [ ] **Step 1:** In `skills/finance/bot/main.py`, import and replace `_is_rajat`:

```python
from skills.finance.lib.users import user_id_for_chat

def _authorized_user_id(message: Message) -> str | None:
    """Return the sender's DB user_id, or None if not whitelisted."""
    return user_id_for_chat(message.chat.id)
```

- [ ] **Step 2:** Update every handler that used `_is_rajat`. Pattern — replace:

```python
    if not _is_rajat(message):
        return
```
with:
```python
    uid = _authorized_user_id(message)
    if uid is None:
        return
```

Apply in `ping`, `model_list_handler`, `cmd_ask`. For `_document_handler`, add the same guard at the top (ingestion is currently open via `handle_document`; gate it so only whitelisted users can upload — keep behavior identical for Rajat).

- [ ] **Step 3:** Thread `uid` into the SQL agent call in `cmd_ask`. Change `run_sql_agent_async`:

```python
async def run_sql_agent_async(question: str, user_id: str) -> AgentResult:
    return await asyncio.to_thread(run_sql_agent, question, user_id)
```
and in `cmd_ask`:
```python
    result = await run_sql_agent_async(question, uid)
```

- [ ] **Step 4: Manual verification (no live Telegram in tests).** Add a lightweight import/smoke check:

```bash
python -c "import skills.finance.bot.main as m; print('ok', hasattr(m,'_authorized_user_id'))"
```
Expected: `ok True`. Full behavioral check happens in the end-to-end verification (post-Task 5) by simulating a `Message` with each chat id.

- [ ] **Step 5:** `make lint typecheck` → clean. Commit. `feat(bot): env-driven whitelist + per-user /ask routing`

---

### Task 4: Thread `user_id` through the ingestion pipeline

**Files:**
- Modify: `skills/finance/ingestion/pipeline.py`
- Test: `tests/test_pipeline_user_id.py` (create)

**Interfaces:**
- Produces: `ingest(parse_result, account_id, source_meta, user_id=RAJAT_USER_ID)`, `_build_insert_row(r, account_id, pr, source, user_id)`.
- Back-compat: default `user_id=RAJAT_USER_ID` keeps `folder_watcher` (Rajat's 4 banks) unchanged.

- [ ] **Step 1: Write the failing test** `tests/test_pipeline_user_id.py` — assert `_build_insert_row` stamps the passed `user_id`:

```python
from datetime import date
from decimal import Decimal
from uuid import UUID

from skills.finance.ingestion._common import ParsedRow, ParseResult, SourceMeta
from skills.finance.ingestion.pipeline import _build_insert_row


def _row():
    return ParsedRow(txn_date=date(2026, 1, 2), amount=Decimal("100.00"),
                     direction="out", raw_merchant="TEST", source_row_ordinal=1)


def _pr():
    return ParseResult(rows=[_row()], declared_totals={"total_spends": Decimal("100"),
                       "total_credits": Decimal("0"), "closing_balance": None,
                       "_derived_from_rows": True}, pdf_content_hash="a"*64,
                       parser_version="test/v1")


def test_build_insert_row_defaults_to_rajat():
    d = _build_insert_row(_row(), UUID(int=1), _pr(), SourceMeta("manual_pdf", "f.pdf"))
    assert d["user_id"] == "00000000-0000-0000-0000-000000000001"


def test_build_insert_row_honors_explicit_user():
    ayushi = "00000000-0000-0000-0000-000000000002"
    d = _build_insert_row(_row(), UUID(int=1), _pr(), SourceMeta("manual_pdf", "f.pdf"), user_id=ayushi)
    assert d["user_id"] == ayushi
```

- [ ] **Step 2: Run — verify fail:** `pytest tests/test_pipeline_user_id.py -v` → FAIL (`_build_insert_row` takes no `user_id`).

- [ ] **Step 3: Implement.** In `pipeline.py`:
  - `_build_insert_row(r, account_id, pr, source, user_id: str = RAJAT_USER_ID)` and set `"user_id": user_id`.
  - `ingest(parse_result, account_id, source_meta, user_id: str = RAJAT_USER_ID)`; pass `user_id` into the `_build_insert_row(...)` call inside the list comprehension.

- [ ] **Step 4: Run — verify pass:** `pytest tests/test_pipeline_user_id.py -v` → PASS. Also run the existing ingestion tests to confirm no regression: `pytest tests/ -k "pipeline or ingest" -v`.

- [ ] **Step 5: Commit.** `feat(ingest): thread user_id through pipeline (defaults to Rajat)`

---

### Task 5: SQL agent user scoping + validator guard

**Files:**
- Modify: `skills/finance/agents/sql_agent.py`
- Modify: `skills/finance/agents/sql_validator.py`
- Modify: `config/db_schema_for_judge.md`
- Test: `tests/test_sql_validator_user_scope.py` (create), extend `tests/test_sql_agent*.py` if present.

**Interfaces:**
- Produces: `run_sql_agent(question: str, user_id: str, cfg: ReviewConfig | None = None) -> AgentResult`.
- Produces: `validate_sql(sql, allowed_tables, require_user_id: str | None = None) -> ValidationResult`.

- [ ] **Step 1: Write the failing validator test** `tests/test_sql_validator_user_scope.py`:

```python
from skills.finance.agents.sql_validator import validate_sql

TABLES = {"transactions", "accounts", "categories"}
UID = "00000000-0000-0000-0000-000000000002"


def test_rejects_query_missing_user_id():
    sql = "SELECT sum(amount) FROM transactions WHERE direction='out'"
    r = validate_sql(sql, TABLES, require_user_id=UID)
    assert not r.ok and "user_id" in r.reason.lower()


def test_accepts_query_with_correct_user_id():
    sql = f"SELECT sum(amount) FROM transactions WHERE user_id = '{UID}' AND direction='out'"
    r = validate_sql(sql, TABLES, require_user_id=UID)
    assert r.ok


def test_rejects_query_with_foreign_user_id():
    other = "00000000-0000-0000-0000-000000000001"
    sql = f"SELECT sum(amount) FROM transactions WHERE user_id = '{other}'"
    r = validate_sql(sql, TABLES, require_user_id=UID)
    assert not r.ok


def test_require_user_id_none_preserves_legacy_behavior():
    sql = "SELECT sum(amount) FROM transactions"
    assert validate_sql(sql, TABLES, require_user_id=None).ok
```

- [ ] **Step 2: Run — verify fail:** `pytest tests/test_sql_validator_user_scope.py -v` → FAIL (arg unknown / no enforcement).

- [ ] **Step 3: Implement the guard** in `sql_validator.py`. Add param and, before the final success return, when `require_user_id` is set: collect all string literals in the parsed statement (`for lit in statement.find_all(exp.Literal)`); require the caller UUID present and reject any *other* 36-char UUID-shaped literal appearing in a `user_id` comparison:

```python
import re as _re

_UUID_RE = _re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

def validate_sql(sql, allowed_tables, require_user_id=None):
    ...  # existing parsing/validation unchanged
    if require_user_id is not None:
        eqs = list(statement.find_all(exp.EQ))
        user_id_values = []
        for eq in eqs:
            cols = [c.name for c in eq.find_all(exp.Column)]
            if "user_id" in cols:
                for lit in eq.find_all(exp.Literal):
                    if lit.is_string:
                        user_id_values.append(lit.this)
        if require_user_id not in user_id_values:
            return ValidationResult(ok=False,
                reason="query must filter every table by user_id = the caller's UUID")
        foreign = [v for v in user_id_values if _UUID_RE.match(v) and v != require_user_id]
        if foreign:
            return ValidationResult(ok=False,
                reason=f"query references a foreign user_id: {foreign[0]}")
    return ValidationResult(ok=True, statement_type="select")
```

_Note for implementer: this guards the common single-table and joined cases. A query that reads `transactions` but filters only via a subquery on `accounts.user_id` will be rejected (no `transactions.user_id` predicate) — acceptable; the LLM is instructed to filter each table directly. Document this in the schema note (Step 5)._

- [ ] **Step 4: Run — verify pass:** `pytest tests/test_sql_validator_user_scope.py -v` → PASS.

- [ ] **Step 5: Update `config/db_schema_for_judge.md`.** Replace the "Single-user V1 — DO NOT include a `user_id` filter" bullet (line ~75) with:

```markdown
- **Multi-user — you MUST filter by user_id.** Every query runs on behalf of ONE user whose UUID is given to you as `:user_id` in the prompt. Add `WHERE user_id = '<that UUID>'` (using the literal UUID string provided, not a placeholder) to EVERY table you read that has a `user_id` column (`transactions`, `accounts`, `categories`, `assets`, `liabilities`). A query without the caller's `user_id` literal is rejected by the validator. Never reference any other user's UUID.
```

- [ ] **Step 6: Update `run_sql_agent`** in `sql_agent.py`:
  - Signature: `def run_sql_agent(question: str, user_id: str, cfg: ReviewConfig | None = None) -> AgentResult:`
  - In the generation prompt (and every retry/strict prompt), inject: `f"You are answering for user_id = '{user_id}'. Every table you read that has a user_id column MUST be filtered with WHERE user_id = '{user_id}'.\n\n"`
  - Pass `require_user_id=user_id` into every `validate_sql(...)` call (initial, retry loop, strict).

- [ ] **Step 7: Run the full SQL-agent test suite** (mock LLM): `pytest tests/ -k "sql_agent or validator" -v`. Fix any call sites that pass positional `cfg`. Expected: PASS.

- [ ] **Step 8: Commit.** `feat(sql-agent): per-user scoping + validator user_id guard`

---

### Task 6: PhonePe UPI parser

**Files:**
- Create: `skills/finance/ingestion/parsers/phonepe_upi.py`
- Create: `tests/test_phonepe_upi_parser.py`
- Modify: `skills/finance/ingestion/_common.py` (`Bank` literal + `detect_bank_from_filename`)
- Modify: `skills/finance/ingestion/folder_watcher.py` (dispatch + maps)
- Modify: `credentials.yaml`
- Fixture: `tests/golden_fixtures/phonepe_sample.pdf` (copy of her real statement; gitignored)

**Interfaces:**
- Produces: `parse(pdf_path: Path, password: str) -> ParseResult`, `__parser_version__ = "phonepe-upi/v1"`.

**Format (verified):** 4-line records. Line 1: `<Mon DD, YYYY> (Paid to|Received from|Paid -) <details> (Debit|Credit) INR <amount>`. Line 2: `<HH:MM AM/PM> Transaction ID : T… [<amount if it overflowed line 1>]`. Line 3: `UTR No : …`. Line 4: `Debited from <acct>` / `Credited to <acct>`. Debit→`out`, Credit→`in`. ~18/353 records carry the amount on line 2 (large transfers, e.g. ₹70,000 rent).

- [ ] **Step 1:** Copy her statement to the fixture path (gitignored) and add the password to `credentials.yaml`:

```bash
cp "Aayushi Statement/PhonePe_Transaction_Statement.pdf" tests/golden_fixtures/phonepe_sample.pdf
```
`credentials.yaml` add:
```yaml
phonepe_upi_XXXX15:
  pattern: phonepe
  value: "8989050872"
```

- [ ] **Step 2: Write the failing test** `tests/test_phonepe_upi_parser.py`:

```python
import os
from decimal import Decimal
from pathlib import Path

import pytest

from skills.finance.ingestion.parsers.phonepe_upi import parse, __parser_version__

FIXTURE = Path(__file__).parent / "golden_fixtures" / "phonepe_sample.pdf"
PASSWORD = "8989050872"

pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="PhonePe golden fixture not present")


def test_version():
    assert __parser_version__ == "phonepe-upi/v1"


def test_parses_rows():
    r = parse(FIXTURE, PASSWORD)
    assert len(r.rows) > 300              # ~353 detail lines in the sample
    assert len(r.pdf_content_hash) == 64


def test_directions_and_amounts_wellformed():
    r = parse(FIXTURE, PASSWORD)
    for row in r.rows:
        assert row.amount > Decimal("0")   # no blank/zero amounts survive
        assert row.direction in ("in", "out")
        assert row.raw_merchant.strip()


def test_ordinals_contiguous():
    r = parse(FIXTURE, PASSWORD)
    assert [x.source_row_ordinal for x in r.rows] == list(range(1, len(r.rows) + 1))


def test_large_overflow_amount_captured():
    # The Jan 02 ₹70,000 transfer has its amount on the Transaction-ID line.
    r = parse(FIXTURE, PASSWORD)
    assert any(row.amount == Decimal("70000.00") for row in r.rows)


def test_credit_row_direction_in():
    r = parse(FIXTURE, PASSWORD)
    assert any(row.direction == "in" for row in r.rows)   # "Received from …" rows
```

- [ ] **Step 3: Run — verify fail:** `pytest tests/test_phonepe_upi_parser.py -v` → FAIL (module missing).

- [ ] **Step 4: Implement** `skills/finance/ingestion/parsers/phonepe_upi.py`:

```python
"""PhonePe UPI transaction-statement PDF parser — deterministic.

PhonePe exports a password-protected PDF (password = the account phone number).
Records span 4 text lines:

    <Mon DD, YYYY> (Paid to|Received from|Paid -) <details> (Debit|Credit) INR <amount>
    <HH:MM AM/PM> Transaction ID : T........  [<amount, when it overflowed line 1>]
    UTR No : ............
    Debited from <acct> | Credited to <acct>

For most rows the amount is on line 1. For large transfers pdfplumber pushes the
amount onto line 2 (after the Transaction ID) — we recover it there. Debit→out,
Credit→in. PhonePe is Ayushi's UPI source of truth (she has no Paytm), so no
UPI-skip rule applies here.
"""
from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pikepdf

from skills.finance.ingestion._common import ParsedRow, ParseResult

logger = logging.getLogger(__name__)

__parser_version__ = "phonepe-upi/v1"

# Line 1: date + "Paid to X"/"Received from Y"/"Paid - Z" + Debit/Credit + INR + optional amount
_DETAIL_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2}) (?P<day>\d{2}), (?P<year>\d{4})\s+"
    r"(?P<details>.+?)\s+"
    r"(?P<type>Debit|Credit)\s+INR\s*(?P<amount>[\d,]+\.\d{2})?\s*$"
)
# Line 2: time + txn id, optionally trailing overflow amount
_TXN_RE = re.compile(
    r"^\d{2}:\d{2}\s+[AP]M\s+Transaction ID\s*:\s*(?P<txnid>\S+)"
    r"(?:\s+(?P<amount>[\d,]+\.\d{2}))?\s*$"
)

_MONTHS = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], start=1)}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _amount(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def parse(pdf_path: Path, password: str) -> ParseResult:
    pdf_path = Path(pdf_path)
    file_hash = _sha256_file(pdf_path)

    lines: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        with pikepdf.open(pdf_path, password=password) as src:
            src.save(tmp.name)
        with pdfplumber.open(tmp.name) as pdf:
            for page in pdf.pages:
                lines.extend((page.extract_text() or "").splitlines())

    rows: list[ParsedRow] = []
    total_out = Decimal(0)
    total_in = Decimal(0)
    ordinal = 0
    i = 0
    n = len(lines)
    while i < n:
        m = _DETAIL_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        amount_str = m.group("amount")
        # Recover overflow amount from the Transaction-ID line if line 1 had none.
        if amount_str is None and i + 1 < n:
            tm = _TXN_RE.match(lines[i + 1].strip())
            if tm and tm.group("amount"):
                amount_str = tm.group("amount")
        if amount_str is None:
            logger.warning("phonepe: record with no recoverable amount at line %d: %r", i, lines[i][:80])
            i += 1
            continue
        amount = _amount(amount_str)
        direction = "out" if m.group("type") == "Debit" else "in"
        txn_date = date(int(m.group("year")), _MONTHS[m.group("mon")], int(m.group("day")))

        ordinal += 1
        rows.append(ParsedRow(
            txn_date=txn_date,
            amount=amount,
            direction=direction,  # type: ignore[arg-type]
            raw_merchant=m.group("details").strip(),
            source_row_ordinal=ordinal,
        ))
        if direction == "out":
            total_out += amount
        else:
            total_in += amount
        i += 1

    logger.info("phonepe: parsed %d rows from %s", len(rows), pdf_path.name)
    declared_totals = {
        "total_spends": total_out,
        "total_credits": total_in,
        "closing_balance": None,
        "_derived_from_rows": True,
    }
    return ParseResult(
        rows=rows,
        declared_totals=declared_totals,
        pdf_content_hash=file_hash,
        parser_version=__parser_version__,
    )
```

- [ ] **Step 5: Run — verify pass:** `pytest tests/test_phonepe_upi_parser.py -v` → PASS. If `test_parses_rows` count is off, inspect which `_DETAIL_RE` lines fail to match (print non-matching `(Paid|Received)` lines) and adjust — do NOT loosen the amount requirement.

- [ ] **Step 6: Wire detection.** In `_common.py`: extend `Bank = Literal["icici_cc", "amex_cc", "paytm_upi", "icici_savings", "phonepe_upi"]`; in `detect_bank_from_filename`, add `has_phonepe = "phonepe" in tokens`, include it in the multi-family ambiguity `sum([...])`, and `if has_phonepe: return "phonepe_upi"`.

- [ ] **Step 7: Wire the watcher.** In `folder_watcher.py`: add `"phonepe_upi"` to `EXPECTED_EXTENSION` (`{".pdf"}`); add a `dispatch_to_parser` branch calling the PhonePe parser with `password_lookup("phonepe_upi", "XXXX15")` and `SourceMeta("manual_pdf", ...)`. **Account/owner mapping:** see Task 9 note — for live routing, `ACCOUNT_IDS["phonepe_upi"]` = Ayushi's PhonePe account and the ingest call must pass `user_id=AYUSHI_USER_ID`. Add a parallel `ACCOUNT_OWNERS: dict[str, str]` map (bank→user_id, default Rajat) and pass `user_id=ACCOUNT_OWNERS.get(bank, RAJAT_USER_ID)` into `ingest(...)`.

- [ ] **Step 8:** Add a detection unit test to `tests/` (or extend the existing `detect_bank_from_filename` test) asserting `detect_bank_from_filename("PhonePe_Transaction_Statement.pdf") == "phonepe_upi"`. Run it → PASS.

- [ ] **Step 9:** `make test lint typecheck`. Commit. `feat(ingest): PhonePe UPI parser + detection/routing`

---

### Task 7: Verify existing ICICI CC parser on Ayushi's statements (NO parser change)

**Files:**
- Create: `tests/test_icici_cc_ayushi.py`
- Fixtures: `tests/golden_fixtures/icici_cc_ayushi_amazonpay.pdf`, `tests/golden_fixtures/icici_cc_ayushi_card2.pdf` (gitignored copies)

**Interfaces:** Consumes existing `icici_cc.parse(path, password)`. No production change.

- [ ] **Step 1:** Copy two representative statements (one per card) to gitignored fixtures. **Her monthlies are NOT password-protected** (confirmed with Rajat + verified: `pikepdf` reports `encrypted=False`), so call `parse(path, password="")` — no `credentials.yaml` entry needed for the CCs.

- [ ] **Step 2: Write the test** `tests/test_icici_cc_ayushi.py` — assert the *existing* parser produces well-formed rows, contiguous ordinals, and non-empty merchants on her statements (mirrors `test_icici_cc_parser.py`, different fixture + password). Include an assertion that a known credit row (`INFINITY PAYMENT RECEIVED` / `BBPS Payment received`) is `direction == "in"`.

- [ ] **Step 3: Run:** `pytest tests/test_icici_cc_ayushi.py -v` → PASS (skips if fixture/password absent). If it fails, the discrepancy is real new information — STOP and report; do not silently mutate `icici_cc.py` (invariant #4/#5).

- [ ] **Step 4:** Commit the test. `test(icici-cc): verify existing parser handles Ayushi's statements`

---

### Task 8: Verify existing ICICI savings XLS parser on Ayushi's export

**Files:**
- Create: `tests/test_icici_savings_xls_ayushi.py`
- Fixture: `tests/golden_fixtures/icici_savings_xls_ayushi.xls` (gitignored copy of `OpTransactionHistory13-07-2026.xls`)

- [ ] **Step 1:** Copy her `.xls` to the fixture path.

- [ ] **Step 2: Write the test** — call `icici_savings_xls.parse(fixture)`; assert `len(rows) > 0`, ordinals contiguous, amounts positive, directions valid, and that at least one row has `is_upi_skip` set (her savings will contain UPI rows). Note: PhonePe is her UPI source of truth, so UPI rows here are correctly skipped at insert.

- [ ] **Step 3: Run:** `pytest tests/test_icici_savings_xls_ayushi.py -v` → PASS or a concrete failure to report.

- [ ] **Step 4:** Commit. `test(icici-savings-xls): verify parser on Ayushi's export`

---

### Task 9: Historical backfill script

**Files:**
- Create: `scripts/backfill_ayushi.py`

**Interfaces:** Consumes `phonepe_upi.parse`, `icici_cc.parse`, `icici_savings_xls.parse`, `pipeline.ingest(..., user_id=AYUSHI_USER_ID)`. Idempotent via `import_hash` upsert.

**Why a script (not the folder-watcher):** her filenames (`Monthlystatement_….pdf`) carry no bank token, can't distinguish her two cards, and can't encode owner. An explicit map is correct and auditable. Live auto-routing of *future* Ayushi statements is deferred — when she gets bot upload access, owner comes from `user_id_for_chat(sender)`, and card disambiguation is a separate follow-up.

- [ ] **Step 1:** Write `scripts/backfill_ayushi.py` with an explicit manifest — each entry `(file_path, parser, password, account_id)` — iterating the `Aayushi Statement/` directory:
  - 7× ICICI Amazon Pay CC monthlies (`Monthlystatement_13 *`) → `icici_cc.parse` → account `20000000-…-0001`
  - 3× ICICI CC #2 monthlies (`Monthlystatement_17 *`) → `icici_cc.parse` → account `20000000-…-0002`
  - 1× PhonePe → `phonepe_upi.parse` (password `8989050872`) → account `20000000-…-0003`
  - 1× ICICI savings `.xls` → `icici_savings_xls.parse` → account `20000000-…-0004`
  - Skip `Annualstatement_FY2025-26.pdf`.
  - For each: build `ParseResult`, call `await ingest(pr, account_id, SourceMeta("manual_pdf"/"manual_xlsx", filename), user_id=AYUSHI_USER_ID)`. Print the returned `ingestion_log` status + `rows_added` per file.
  - Use the `adb()`/async pattern already used by the pipeline; run under `asyncio.run(main())`.

- [ ] **Step 2: Dry-run guard.** Add a `--commit` flag; without it, parse + report row counts per file but do NOT ingest. This lets us eyeball extracted totals before writing.

- [ ] **Step 3: Run dry:** `python scripts/backfill_ayushi.py`. Verify each file parses with a sane row count and total. Cross-check the PhonePe ₹70,000 rent row appears.

- [ ] **Step 4: Run for real:** `python scripts/backfill_ayushi.py --commit`. Capture per-file `rows_added`.

- [ ] **Step 5: Verify in DB:**

```bash
python -c "import psycopg,os; c=psycopg.connect(os.environ['SUPABASE_DB_URL'],prepare_threshold=None); \
print(c.execute(\"select a.nickname, count(*) from transactions t join accounts a on a.id=t.account_id where t.user_id='00000000-0000-0000-0000-000000000002' group by a.nickname order by 1\").fetchall())"
```
Expected: one row per Ayushi account with non-zero counts.

- [ ] **Step 6: Idempotency check.** Re-run `--commit`; every file reports `rows_added=0` / `skipped_duplicate`. Confirms `import_hash` dedup holds for her accounts.

- [ ] **Step 7: Commit** the script. `feat(scripts): Ayushi historical statement backfill`

---

## Cross-user reconciliation (post-backfill, verification — not a code task)

After Task 9, spot-check the cross-person transfers the PhonePe statement exposes (e.g. "Received from RAJAT SHARMA", "Paid to RAJAT SHARMA", the ₹70,000 rent). The existing refund/self-transfer detector runs per-account within `ingest`; cross-*user* linking is **not** in scope here. Note any duplicate-looking household transfers for a future dedup pass; do not hand-link now.

---

## Self-Review (completed against the spec)

- **User onboarding & whitelisting:** Task 1 (users row), Task 2 (settings field + mapping helper), Task 3 (env-driven whitelist + chat→user). ✓
- **SQL agent separation:** Task 5 — `run_sql_agent(question, user_id)`, schema-doc rewrite, validator guard, `/ask` threads UUID (Task 3 Step 3). ✓
- **PhonePe parser:** Task 6, password → `credentials.yaml`. ✓
- **ICICI CC calibration:** re-scoped to verify-only (Task 7) with justification — the format is identical to Rajat's; calibration would force re-ingest of his data. ✓ (flagged for confirmation)
- **ICICI savings xls verify:** Task 8. ✓
- **Added (attribution gap):** Task 4 (pipeline `user_id`), Task 9 (backfill). ✓
- **Type consistency:** `run_sql_agent(question, user_id, cfg)`, `validate_sql(sql, allowed_tables, require_user_id)`, `ingest(pr, account_id, source_meta, user_id)`, `_build_insert_row(r, account_id, pr, source, user_id)`, `user_id_for_chat(chat_id)`, `parse(pdf_path, password)` — consistent across tasks.
- **Deferred (explicitly noted):** live folder-watcher owner/card auto-routing for future Ayushi statements; cross-user transfer dedup; optional ICICI CC merchant-field cleanup (ref#/points stripping) as a future `parser_version` bump.
