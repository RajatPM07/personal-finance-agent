# Week 2 — Ingestion (ICICI CC + AMEX CC) — Design Spec

**Status:** Locked, awaiting user sign-off before implementation plan
**Date:** 2026-04-26
**PRD references:** §11 Week 2, §6 model routing, §7 schema, §18.4 PDF integrity, §19.5 `import_hash` Mode B
**Lessons referenced:** bout-is-CSV-only, casparser-pin-conflict, aiogram-handle_signals, Supavisor-username, RLS-auto-enable

---

## 1. Goal

Stand up an end-to-end ingestion pipeline for ICICI CC and AMEX CC PDF statements. By end of Week 2:
- Three months of historical ICICI CC and AMEX CC transactions populate `transactions`
- Statement-total validator catches silent corruption (extracted-sum vs declared-total mismatch)
- AMEX is calibrated against a second LLM (Claude Haiku) for the first 2–3 statements, surfacing model disagreements for human adjudication
- New monthly statements drop into `~/finance-inbox/` (or via Telegram doc) and are processed automatically with a summary notification

## 2. Scope

**In scope:**
- ICICI CC parser (deterministic via `pikepdf` + `pdfplumber` + regex)
- AMEX CC parser (LLM via `llm("pdf_extraction")` with Pydantic-enforced output)
- `statement_validator.py` — pure function, parser-agnostic
- `pipeline.py` — orchestrator: parse → validate → dedup → insert
- `calibration.py` — AMEX dual-model diff (Month 1 only, ₹200 cap)
- `folder_watcher.py` — watches `~/finance-inbox/`, dispatches by loose filename token match
- Telegram doc handler in `bot/document_handler.py` — auto-renames on save
- `/model list` command (read-only, the only `/model` subcommand in V1 Week 2)
- 3-month backfill (6 PDFs total)

**Explicitly deferred to Week 3+:**
- Gmail integration (F1) → Week 3
- ICICI Sav / HDFC Sav / Paytm / MF CAS / Zerodha / payslip parsers → Week 3
- SMS-to-email forwarder + parser (F3) → Week 3
- Merchant normalization (curated → rapidfuzz → pgvector → LLM) → Week 5
- Categorization (DistilBERT + LLM + memory) → Week 4
- Refund detection (`is_refund`, `linked_txn_id` populated) → Week 4
- `/exclude`, `/categorize`, `/dedup`, `/retry` commands → Week 4
- Full `/model` family (`/model <task> <model>`, `--confirm`, A/B mode) → Week 3 or 4
- SQL agent + affordability engine → Week 4

## 3. Architecture

```
[Source]                   [Parse]                  [Validate]               [Persist]
folder watcher ─┐
                ├─→ parsers/{bank}.py  ─→  statement_validator  ─→  pipeline.ingest
Telegram doc ───┘    ├─ icici_cc: deterministic        ↓                       ↓
                     └─ amex_cc:  LLM (Gemini)      needs_review            transactions
                                                     + alert
```

Four layers, each isolated and independently testable.

**Source layer** — two equivalent entry points; both write the PDF to `~/finance-inbox/` and let `folder_watcher` do dispatch. The Telegram doc handler is thin — it saves the doc with a canonical filename (auto-rename if needed), then the watcher picks it up. No duplicated routing logic between source paths.

**Parse layer** — per-bank modules under `skills/finance/ingestion/parsers/`. Each exports a manually-curated `__parser_version__` (per CLAUDE.md invariant #4) and exposes `parse(pdf_path: Path, password: str) -> ParseResult`.

**Validate layer** — `statement_validator.py` is a pure function: `validate(parse_result) -> ValidationResult`. Compares extracted sums against declared totals with ±₹1 tolerance. Centralized, parser-agnostic, no I/O.

**Persist layer** — `pipeline.py` orchestrates: validate → compute Mode-B `import_hash` per row → bulk upsert → log to `ingestion_log` → summary message via main bot.

## 4. File structure

```
skills/finance/ingestion/
├── __init__.py
├── _common.py             # ParsedRow, ParseResult, SourceMeta, ValidationResult dataclasses;
│                          #   password_lookup(bank, last4) → str via credentials.yaml
├── folder_watcher.py      # watchdog Observer; dispatches by loose filename token match
├── statement_validator.py # pure function — validate(ParseResult) → ValidationResult
├── pipeline.py            # async ingest orchestrator
├── calibration.py         # AMEX dual-model diff (Month 1 only)
└── parsers/
    ├── __init__.py
    ├── icici_cc.py        # deterministic; __parser_version__ = "icici-cc/v1"
    └── amex_cc.py         # LLM;          __parser_version__ = "amex-cc-llm/v1"

skills/finance/bot/
├── main.py                # existing — register doc handler + /model list
└── document_handler.py    # NEW — saves Telegram doc to inbox with auto-rename

tests/
├── test_statement_validator.py    # pure-fn unit tests, ~6 cases
├── test_icici_cc_parser.py        # golden fixture, gated on ICICI_PDF_PASSWORD
├── test_amex_cc_parser.py         # mocked llm() call
├── test_pipeline.py               # mocked service_client + adb()
├── test_calibration.py            # mocked dual-model + canned disagreement scenarios
├── test_folder_watcher.py         # mocked watchdog events
├── test_document_handler.py       # mocked aiogram Document message
└── golden_fixtures/
    ├── icici_sample.pdf            # exists from Week 1
    └── amex_sample.pdf             # user provides before Week 2 dispatch
```

New top-level files: none. New `app.py` modifications: start `folder_watcher.observe()` alongside aiogram + APScheduler + FastAPI.

## 5. Source layer

### 5.1 Folder watcher (`folder_watcher.py`)

Runs a `watchdog.observers.Observer` watching `~/finance-inbox/`. On `on_created` / `on_moved` for any `*.pdf`:

1. Lowercase the filename for matching.
2. Token-match (Pattern B):
   - Contains `'icici'` AND `'cc'` → `bank = 'icici_cc'`
   - Contains `'amex'` OR `'american'` → `bank = 'amex_cc'`
   - Both ICICI and AMEX tokens present → ambiguous; alert via main bot, log `ambiguous_filename`, rename file to `<name>.rejected.pdf` so it isn't reprocessed.
   - No match → alert: "Filename `{name}` doesn't match any known bank pattern. Rename to include `icici_cc_` or `amex_cc_` and re-drop." Rename to `.rejected.pdf`.
3. Resolve password via `_common.password_lookup(bank, last4=None)`. With one card per bank today, the helper prefix-matches `bank_*` keys in `credentials.yaml` and uses the unique entry. If multiple match (future state), the filename must include `<last4>` and the helper does an exact lookup.
4. Dispatch to parser: `parsers.<bank>.parse(pdf_path, password)`.
5. Hand the result to `pipeline.ingest(...)`.

The watcher runs as a separate `asyncio` task started by `app.py`. **Single-threaded**: files processed sequentially in arrival order. No concurrency in V1 (debugging simplicity).

### 5.2 Telegram document handler (`bot/document_handler.py`)

Registers an aiogram `Message` handler filtered on `Document` content. Whitelist check via existing `_is_rajat(message)` (CLAUDE.md invariant #7) FIRST.

On document receipt:

1. Inspect `message.document.file_name`. Lowercase, run the SAME token-match logic as the folder watcher (DRY: extract to `_common.detect_bank_from_filename(name) -> Optional[Bank]`).
2. **Unambiguous match** (e.g., `icici_cc_2026_05.pdf` or `Statement_April_2026_ICICI.pdf`): save the file to `~/finance-inbox/` with canonical prefix — `icici_cc_<sanitized-original-stem>.pdf`. Reply: "Saved as `icici_cc_…pdf` — processing." Folder watcher takes over.
3. **No match or ambiguous**: send an inline keyboard message: "Couldn't auto-detect bank from `{file_name}`. Which is this?  [ICICI CC]  [AMEX CC]  [Cancel]". On user pick: save with canonical prefix `<bank>_<sanitized-original-stem>.pdf`. On Cancel: reply "OK, ignored — re-send when ready."

Auto-rename means the user can forward an unmodified Gmail attachment without prep.

## 6. Parse layer

### 6.1 Common contract (`_common.py`)

```python
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

@dataclass(frozen=True)
class ParsedRow:
    txn_date: date
    amount: Decimal                       # always positive; sign info lives on `direction`
    direction: Literal['in', 'out']
    raw_merchant: str
    source_row_ordinal: int                # 1..N within the PDF, deterministic per parser

@dataclass(frozen=True)
class ParseResult:
    rows: list[ParsedRow]
    declared_totals: dict                  # {'total_spends': Decimal, 'total_credits': Decimal,
                                           #  'closing_balance': Decimal | None}
    pdf_content_hash: str                  # sha256 of source PDF bytes; threaded into import_hash
    parser_version: str                    # e.g. "icici-cc/v1"

@dataclass(frozen=True)
class SourceMeta:
    source: Literal['manual_pdf', 'telegram_pdf', 'gmail_cc_stmt']
    source_ref: str                        # filename / message_id / email_id

def password_lookup(bank: str, last4: str | None = None) -> str:
    """Read credentials.yaml. If last4 is given, exact key. Else prefix-match a unique entry.
    Raises AmbiguousCredentialError if multiple match without last4."""
    ...

def detect_bank_from_filename(filename: str) -> Optional[Literal['icici_cc', 'amex_cc']]:
    """Pure fn — lowercase + token match. Used by both folder_watcher and document_handler."""
    ...
```

`RAJAT_USER_ID` is declared in `_common.py` as a module-level constant — hardcoded UUID `00000000-0000-0000-0000-000000000001` matching the seeded user. Single source of truth across the ingestion pipeline. (V2 onboarding for Ayushi will move this to a `settings.py` field; not needed for V1 single-user.)

### 6.2 ICICI CC parser (`parsers/icici_cc.py`, deterministic)

- `pikepdf.open(pdf_path, password=password)` → save decrypted copy to `tempfile.NamedTemporaryFile(suffix='.pdf', delete=True)`.
- `pdfplumber.open(tmp.name)` extracts text per page.
- Regex over the transaction-table region. Each row matches:
  ```
  ^(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<merchant>.+?)\s+(?P<amount>[\d,]+\.\d{2})\s*(?P<cr>Cr)?$
  ```
  Date in DD/MM/YYYY; merchant in the middle; amount with optional `Cr` suffix. `Cr` present → `direction='in'`; absent → `direction='out'`.
- Declared totals (`total_spends`, `total_credits`, `closing_balance`) extracted from the summary block (typically labeled near top or bottom of statement). Exact regex calibrated against the Week 1 golden fixture during implementation.
- `__parser_version__ = "icici-cc/v1"` — bump if output shape changes.

### 6.3 AMEX CC parser (`parsers/amex_cc.py`, LLM)

- `pikepdf.open(...)` to decrypt → temp file.
- `pdf2image.convert_from_path(tmp.name)` rasterizes each page to PIL Image (requires `poppler` system package, installed via `brew install poppler`). Encode each page as base64-PNG.
- For the whole statement (or per page if multi-page), call:
  ```python
  resp = llm(
      "pdf_extraction",
      system="You are a precise PDF extractor for AMEX credit card statements...",
      prompt="Extract every transaction row plus the declared totals. Output strict JSON matching the schema...",
      images=[b64_png_page_1, b64_png_page_2, ...],
  )
  ```
- The response is parsed via:
  ```python
  class LLMRow(BaseModel):
      txn_date: date
      amount: Decimal
      direction: Literal['in', 'out']
      raw_merchant: str

  class LLMStatement(BaseModel):
      rows: list[LLMRow]
      total_spends: Decimal
      total_credits: Decimal
      closing_balance: Decimal | None = None

  parsed = LLMStatement.model_validate_json(resp.choices[0].message.content)
  ```
- Schema violation → raise `ParserError`; pipeline logs `status='failed'`.
- `source_row_ordinal` assigned 1..N in LLM output order.
- `__parser_version__ = "amex-cc-llm/v1"` — bump if prompt or schema changes meaningfully.

**Required change to `lib/llm.py`:** re-add `images: list[str] | None = None` parameter (removed in Week 1 as YAGNI; now actually needed). Existing `test_llm.py` MUST continue to pass — verify no regression.

## 7. Validate layer

```python
# skills/finance/ingestion/statement_validator.py
@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    delta_in: Decimal
    delta_out: Decimal
    rows_count: int
    declared_in: Decimal
    declared_out: Decimal
    extracted_in: Decimal
    extracted_out: Decimal

TOLERANCE = Decimal('1.00')

def validate(pr: ParseResult) -> ValidationResult:
    declared_in = pr.declared_totals['total_credits']
    declared_out = pr.declared_totals['total_spends']
    extracted_in = sum((r.amount for r in pr.rows if r.direction == 'in'), Decimal('0'))
    extracted_out = sum((r.amount for r in pr.rows if r.direction == 'out'), Decimal('0'))
    delta_in = abs(declared_in - extracted_in)
    delta_out = abs(declared_out - extracted_out)
    return ValidationResult(
        ok=(delta_in <= TOLERANCE and delta_out <= TOLERANCE),
        delta_in=delta_in, delta_out=delta_out,
        rows_count=len(pr.rows),
        declared_in=declared_in, declared_out=declared_out,
        extracted_in=extracted_in, extracted_out=extracted_out,
    )
```

Pure function, no I/O. Unit tests: exact match, off-by-₹1 (within tolerance), off-by-₹2 (rejected), missing declared totals, all-out (no credits), all-in (refund-only statement edge case).

## 8. Persist layer

```python
# skills/finance/ingestion/pipeline.py
async def ingest(parse_result: ParseResult, account_id: UUID, source_meta: SourceMeta) -> IngestionLogEntry:
    """Orchestrator. Returns the ingestion_log row for status reporting."""
    val = validate(parse_result)
    if not val.ok:
        return await _log_validation_failure(parse_result, source_meta, val)

    rows = [_build_insert_row(r, account_id, parse_result, source_meta) for r in parse_result.rows]

    response = await adb(
        lambda: service_client()
            .table('transactions')
            .upsert(rows, on_conflict='import_hash', ignore_duplicates=True)
            .execute()
    )
    rows_added = len(response.data) if response.data else 0
    return await _log_success(parse_result, source_meta, val, rows_added)
```

`_build_insert_row` constructs the dict for Supabase, computing `import_hash` via `lib.hashing.import_hash_pdf(...)` using:
- `account_id`, `txn_date`, `amount`, `raw_merchant` (used as `normalized_description` for Week 2 — see note below)
- `pdf_content_hash` (from `parse_result`, sha256 of PDF bytes)
- `source_row_ordinal` (from row)
- `parser_version` (from `parse_result`)

**Note on `normalized_description`:** Week 5 introduces merchant normalization. Until then, `raw_merchant` IS the normalized string (no transformation). This means: when Week 5 lands and changes how merchants are normalized, `import_hash` values will change for the same logical row. That's a feature, not a bug — per CLAUDE.md invariant #4, parser/normalization bumps force fresh ingest. Week 5's plan will include a reconciliation step.

**Open implementation risk (16.1):** supabase-py's `upsert(..., on_conflict='import_hash', ignore_duplicates=True)` semantics need verification during Week 2 implementation. If the parameter doesn't behave as expected, fall back to per-row `insert(...)` wrapped in try/except for unique-constraint violation. CLAUDE.md invariant #11: verify, don't assume.

## 9. Telegram review flow

### 9.1 ICICI CC, all months

After successful ingest, **main bot** sends:
> 📥 ICICI CC May 2026 ingested
> 45 rows, ₹1,24,380 spend (₹0 credits), totals match declared ✓
> Backfill progress: 1/3 statements

After totals failure, **alert bot** sends:
> ⚠️ ICICI CC May 2026 — totals mismatch
> Extracted: ₹1,24,380 spend / ₹0 credits
> Declared:  ₹1,26,000 spend / ₹0 credits
> Delta:     ₹1,620 (over tolerance ₹1)
> NOT ingested. `ingestion_log.id=<uuid>` in `needs_review`.
> (Manual resolution until `/retry <uuid>` lands in Week 4.)

### 9.2 AMEX CC, Month 1 calibration window

`calibration.py` runs both Gemini Flash + Claude Haiku for the first 2–3 AMEX statements. After both extractions:

1. Both extractions independently go through `statement_validator`. If either fails, calibration treats it as a disagreement.
2. Inner-join the row sets on `(txn_date, amount, direction)`. Rows present in only one set OR with differing `raw_merchant` are flagged as disagreements.
3. Each disagreement is serialized to a Telegram message via main bot:

> 🧪 AMEX CC May 2026 — calibration mode
> 23 rows total. Gemini and Haiku agree on 22.
> 1 disagreement at row 12:
> • Gemini → `2026-05-15  ₹3,400.00  ZOMATO`
> • Haiku  → `2026-05-15  ₹3,400.00  SWIGGY`
> Reply [G] / [H] / [skip]

User adjudicates each disagreement. After all resolved, the chosen row set is ingested via the standard pipeline.

**Cost cap enforcement:** before each `llm("pdf_extraction", ...)` call inside calibration, sum `total_cost` from `request_logs` where `metadata->>'task' = 'pdf_extraction'` for AMEX rows. If the running total approaches ₹200, calibration aborts with notification: "Calibration budget reached; remaining AMEX statements will use Gemini-only path." This adds ~50ms latency per call — acceptable for Month-1 volume.

**Expected actual spend:** 3 statements × ~3 pages × 2 models ≈ 18 LLM calls × $0.01 ≈ ₹15 worst case. Well within ₹200.

### 9.3 AMEX CC, post-calibration (statement #4 onward)

Single-model (Gemini Flash). Same flow as ICICI CC.

## 10. /model command (V1 minimal)

`bot/main.py` registers a `/model` handler. Only one subcommand in Week 2:

- `/model list` — replies with the current `config/model_routing.yaml` as a code-block message.

The full PRD §6.2 spec (`/model <task> <model_name>`, `--confirm` for stakes:high, A/B mode) is deferred until Week 4 when reasoning queries are live and there's a real reason to swap.

## 11. Backfill mechanics

User drops 6 PDFs at once into `~/finance-inbox/` (3 months × 2 cards). Watcher processes them sequentially. Per PDF: parse → validate → review (if AMEX in calibration window) → ingest. Failures don't halt the queue; next PDF is attempted.

Final summary message after all 6:
> Backfill complete
> ✓ ICICI CC: 3/3 statements ingested, 142 rows
> ✓ AMEX CC:  3/3 statements ingested, 81 rows (calibration adjudicated 4 disagreements)
> 0 statements in needs_review

## 12. Error handling matrix

| Failure | `ingestion_log.status` | Alert | Partial insert |
|---|---|---|---|
| Filename matches no bank | `failed` | yes (main bot) | none — file renamed `<name>.rejected.pdf` |
| Filename matches both banks | `ambiguous_filename` | yes (main bot, prompts user) | none |
| Wrong PDF password | `failed` (error_msg includes credential key tried) | yes (alert bot) | none |
| Parser hard fail (regex no match / LLM JSON parse error) | `failed` | yes (alert bot) | none |
| Totals mismatch | `total_check_failed` | yes (alert bot) | **none — entire statement rejected** |
| All rows already exist | `skipped_duplicate` | no | none — idempotent re-drop |
| Some rows already exist | `success` with `rows_added < total` | no | inserts only new |
| LLM cost cap hit during calibration | `success` with note | yes (main bot, "calibration aborted") | yes — Gemini-only path used |

## 13. Testing strategy

Every test runs as part of `make test`. No live LLM tests committed.

- `test_statement_validator.py` — 6 cases (exact / within-tolerance / over-tolerance / missing declared / all-out / all-in / refund-only)
- `test_icici_cc_parser.py` — golden fixture, gated on `ICICI_PDF_PASSWORD` env var (skips when unset, like Week 1's `test_pdf_smoke.py`)
- `test_amex_cc_parser.py` — mocked `llm()` returning canned Pydantic-validated JSON; verifies schema enforcement raises on malformed responses
- `test_pipeline.py` — mocked `service_client` and `adb()` (MagicMock pattern from `test_bot_middleware.py`); verifies validation path, hash computation, log status
- `test_calibration.py` — mocked dual-model returning canned scenarios (perfect agreement / single disagreement / multiple disagreements / one-side-missing-row / cost-cap-hit)
- `test_folder_watcher.py` — mocked `watchdog` events; verifies token-match dispatch, ambiguous-name handling, `.rejected.pdf` renaming
- `test_document_handler.py` — mocked aiogram Message + Document; verifies auto-rename and inline-keyboard prompt for ambiguous names

`tests/golden_fixtures/amex_sample.pdf` is gitignored (PII), user provides locally before Week 2 dispatch.

## 14. New dependencies

In `pyproject.toml`:
- `watchdog>=3` — folder watcher
- `pdf2image>=1.17` — AMEX page rasterization (pulls `Pillow` transitively, no separate declaration needed)

System deps:
- `brew install poppler` — required by `pdf2image` (similar to ghostscript+tcl-tk for camelot)

## 15. Code changes outside `skills/finance/ingestion/`

- `skills/finance/lib/llm.py` — re-add `images: list[str] | None = None` parameter to `llm()`. Existing `test_llm.py` MUST still pass (no regression).
- `app.py` — start `folder_watcher.observe()` in a task alongside aiogram + APScheduler + FastAPI. Wire it into the graceful-shutdown handler so a SIGTERM also stops the watcher.
- `skills/finance/bot/main.py` — register `document_handler` and the `/model list` handler.

## 16. Open implementation risks

1. **supabase-py upsert with `ignore_duplicates`** — semantics not yet confirmed. If `upsert(..., on_conflict='import_hash', ignore_duplicates=True)` doesn't actually use ON CONFLICT DO NOTHING, fall back to per-row insert with try/except for unique-constraint violation. Verify during implementation.
2. **AMEX statement format variability** — first 2–3 statements during calibration are the only window to catch format drift. If the LLM's parsing breaks on a partner-card variant, calibration adjudication is the safety net.
3. **`pdf2image` + `poppler` on macOS Apple Silicon** — `brew install poppler` works in standard ARM Homebrew. If `pdf2image.convert_from_path` errors on user's specific Python build, fall back to `pdfplumber.Page.to_image()` (slower, no system dep).
4. **`request_logs` cost-cap query latency** — the SQL aggregation before each calibration LLM call adds ~50ms. Acceptable for Month-1 (low volume); not for ongoing. Document this so Week 3+ doesn't naively reuse the pattern.

## 17. Acceptance criteria

By end of Week 2, all of these must pass:

- [ ] 6 backfill PDFs ingested into `transactions` (3 months × 2 cards, ~200–300 rows expected)
- [ ] `make lint && make typecheck && make test` clean at every commit
- [ ] Statement-total validator catches a manually-injected wrong-amount in ≥1 row of a test fixture (regression coverage in `test_statement_validator.py`)
- [ ] AMEX calibration adjudication path exercised at least once (real disagreement OR mocked test)
- [ ] Folder watcher running under launchd's app supervisor; survives `kill -9` (KeepAlive=true validates)
- [ ] Telegram doc handler auto-renames a non-canonical filename ("Statement_April.pdf") on save
- [ ] Each parser exports `__parser_version__` and the test suite asserts the exact strings (`"icici-cc/v1"`, `"amex-cc-llm/v1"`)
- [ ] `lib/llm.py` re-adds `images` parameter and existing `test_llm.py` still passes
- [ ] `request_logs` shows AMEX calibration calls totaling ≤ ₹200
- [ ] No PII committed to git (verified via pre-commit hook + manual `git log -p` review)
- [ ] `tasks/week-2-todo.md` exists and is the live plan; `tasks/todo.md` preserved as Week 1 historical record

## 18. References

- PRD §11 Week 2 (original plan, partially superseded by Week 1 lessons)
- PRD §6 model routing + §6.5 affordability data-quality guard
- PRD §7 transactions schema (V2.1)
- PRD §18.4 PDF integrity (statement-total validator + dual-model calibration)
- PRD §19.5 `import_hash` Mode B with `parser_version`
- `tasks/lessons.md` — bout, sonnet 4-7, casparser, aiogram `handle_signals`, Supavisor, RLS auto-enable
- `tasks/preconditions-notes.md` §0.1 (bout API), §0.2 (LiteLLM 1.83.10), §0.3 (model IDs), §0.4 (`request_logs` schema)
