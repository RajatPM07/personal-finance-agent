# Week 2 — PFA Ingestion Implementation Plan (revision 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** End-to-end ingestion pipeline for ICICI CC and AMEX CC PDF statements with statement-total integrity validation, AMEX dual-model calibration, and graceful failure paths. By end of Week 2, three months of ICICI CC + AMEX CC history are in `transactions`, and new monthly statements drop into `~/finance-inbox/` (or via Telegram doc) and process automatically.

**Architecture:** Four isolated layers — Source (folder watcher + Telegram doc handler) → Parse (deterministic ICICI, LLM AMEX) → Validate (pure-fn statement-total comparator) → Persist (Mode-B `import_hash` + Supabase upsert + `ingestion_log`). Spec at `docs/superpowers/specs/2026-04-26-week-2-ingestion-design.md`.

**Tech Stack:** Python 3.11, `pikepdf` (decrypt), `pdfplumber` (ICICI text extraction), `pdf2image` + `Pillow` + `poppler` (AMEX rasterization), LiteLLM `pdf_extraction` task (Gemini Flash primary, Claude Haiku for AMEX calibration), `watchdog` (folder watcher), aiogram (Telegram doc handler), pydantic v2 (LLM schema enforcement), supabase-py (upsert).

**PRD reference:** `PRD.md` V2.1 §6 (model routing), §7 (schema), §11 Week 2 (partially superseded), §18.4 (PDF integrity), §19.5 (`import_hash` Mode B).
**Lessons referenced (do NOT repeat):** bout-is-CSV-only (parser must be built from scratch), aiogram-handle_signals (already fixed in app.py), Supavisor-username (Week 5 SQL agent), RLS-auto-enable (already disabled), CLAUDE.md invariants #2 (`.execute()` terminator), #4 (manual `__parser_version__`), #11 (verify, don't assume).

**Revision history:**
- **r1 (2026-04-26):** initial plan after 4-question brainstorm and locked spec.

---

## Preconditions — Rajat's tasks (not code)

Every box must be green before Task 0 starts.

- [ ] `brew install poppler` (required by `pdf2image`; ARM Homebrew bottle is fine)
- [ ] AMEX CC PDF copied to `tests/golden_fixtures/amex_sample.pdf` (gitignored; one real recent statement)
- [ ] `AMEX_PDF_PASSWORD` env var available for the implementation session: `export AMEX_PDF_PASSWORD=<your statement password>`
- [ ] `ICICI_PDF_PASSWORD` env var (already used in Week 1's Task 9 smoke; same value)
- [ ] Anthropic balance shows ≥ ₹420 (current $5; calibration expected to use ≤ ₹15 of it)
- [ ] launchd-supervised app is healthy (`launchctl print gui/$(id -u)/com.rajat.pfa.app | grep state` → `running`)
- [ ] `git config --local user.{name,email}` set so commits don't need `-c` flags

---

## File structure

All paths relative to `/Users/rajat/AntiGravity/Personal finance Agent/`.

**Created in Week 2:**
- `skills/finance/ingestion/_common.py` — `ParsedRow`, `ParseResult`, `SourceMeta`, `ValidationResult` dataclasses; `RAJAT_USER_ID`; `password_lookup(bank, last4)`; `detect_bank_from_filename(name)`
- `skills/finance/ingestion/statement_validator.py` — pure `validate(parse_result) → ValidationResult`
- `skills/finance/ingestion/pipeline.py` — `async ingest(parse_result, account_id, source_meta) → IngestionLogEntry`
- `skills/finance/ingestion/parsers/icici_cc.py` — `parse(pdf_path, password) → ParseResult`, `__parser_version__ = "icici-cc/v1"`
- `skills/finance/ingestion/parsers/amex_cc.py` — `parse(pdf_path, password) → ParseResult`, `__parser_version__ = "amex-cc-llm/v1"`
- `skills/finance/ingestion/calibration.py` — `async calibrate_amex(pdf_path, password) → list[ParsedRow]` (dual-model diff + Telegram adjudication)
- `skills/finance/ingestion/folder_watcher.py` — `watchdog` Observer; `async run()` task
- `skills/finance/bot/document_handler.py` — aiogram `Document` handler with auto-rename
- `tests/test_statement_validator.py`, `test_icici_cc_parser.py`, `test_amex_cc_parser.py`, `test_pipeline.py`, `test_calibration.py`, `test_folder_watcher.py`, `test_document_handler.py`

**Modified in Week 2:**
- `skills/finance/lib/llm.py` — re-add `images: list[str] | None = None` parameter
- `skills/finance/bot/main.py` — register `document_handler` + `/model list` command
- `app.py` — start folder watcher in `asyncio.gather` alongside bot/scheduler/HTTP; include in graceful shutdown
- `pyproject.toml` — add `watchdog>=3`, `pdf2image>=1.17` to deps

**Deferred (out of scope per spec §2):**
Gmail integration (Week 3), bank-savings parsers (Week 3), Paytm/MF/Zerodha/payslip (Week 3), merchant normalization (Week 5), categorization (Week 4), refund detection (Week 4), full `/model` family (Week 4), SQL agent + affordability (Week 4).

---

## Task 0: Preconditions verification (tooling sanity check)

Catches library API surprises before they poison downstream tasks (CLAUDE.md invariant #11).

- [ ] **Step 0.1: Verify `pdf2image` + `poppler` work on this Mac**

```bash
cd "/Users/rajat/AntiGravity/Personal finance Agent"
source .venv/bin/activate
pip install pdf2image
python -c "
from pdf2image import convert_from_path
import tempfile
import pikepdf
import os
pwd = os.environ.get('ICICI_PDF_PASSWORD', '')
with pikepdf.open('tests/golden_fixtures/icici_sample.pdf', password=pwd) as pdf:
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as t:
        pdf.save(t.name)
        pages = convert_from_path(t.name)
        print(f'pdf2image OK: {len(pages)} pages, first page size = {pages[0].size}')
        os.unlink(t.name)
"
deactivate
```

Record findings in `tasks/week-2-preconditions-notes.md`:
- Number of pages in the ICICI fixture
- Page size in pixels
- Any errors / warnings about poppler

If `pdf2image` raises `PDFInfoNotInstalledError`, `brew install poppler` was missed — STOP, report BLOCKED.

- [ ] **Step 0.2: Verify `supabase-py` upsert with `on_conflict` + `ignore_duplicates`**

```bash
cd "/Users/rajat/AntiGravity/Personal finance Agent"
source .venv/bin/activate
python -c "
from skills.finance.lib.db import service_client
import inspect
c = service_client()
sig = inspect.signature(c.table('transactions').upsert)
print(f'upsert signature: {sig}')
print(f'param names: {list(sig.parameters.keys())}')
"
deactivate
```

Confirm `on_conflict` and `ignore_duplicates` are accepted parameters. If signature is materially different from what spec §8 assumed, document the actual signature in `tasks/week-2-preconditions-notes.md` and propose the fallback (per-row insert with try/except for unique-constraint violation). Update Task 6 inline with the correct approach before implementation begins.

- [ ] **Step 0.3: Verify pikepdf can decrypt the AMEX fixture**

```bash
cd "/Users/rajat/AntiGravity/Personal finance Agent"
source .venv/bin/activate
python -c "
import pikepdf, os
pwd = os.environ.get('AMEX_PDF_PASSWORD', '')
if not pwd:
    print('AMEX_PDF_PASSWORD env var not set — STOP')
    raise SystemExit(2)
with pikepdf.open('tests/golden_fixtures/amex_sample.pdf', password=pwd) as pdf:
    print(f'AMEX decrypt OK: {len(pdf.pages)} pages')
"
deactivate
```

If pikepdf raises `PasswordError`, the credential is wrong — verify with Rajat before proceeding.

- [ ] **Step 0.4: Save preconditions notes (gitignored)**

```bash
cd "/Users/rajat/AntiGravity/Personal finance Agent"
# tasks/week-2-preconditions-notes.md — SAME pattern as Week 1's preconditions-notes.md
# Already gitignored via *.preconditions-notes.md? No — re-check .gitignore
grep -E "preconditions-notes" .gitignore || echo "tasks/week-2-preconditions-notes.md" >> .gitignore
```

Confirm the file appears in `.gitignore`. If `.gitignore` rule for Week 1's `tasks/preconditions-notes.md` was specific (not a glob), add the Week 2 path explicitly.

- [ ] **Step 0.5: Commit `.gitignore` adjustment if needed**

If `.gitignore` was modified in Step 0.4, commit:
```bash
git add .gitignore
git commit -m "chore(gitignore): add Week 2 preconditions-notes path"
```

If unchanged, skip.

---

## Task 1: `_common.py` — shared dataclasses + helpers

**Files:**
- Create: `skills/finance/ingestion/_common.py`
- Test: `tests/test_ingestion_common.py`

- [ ] **Step 1.1: Write failing tests for `password_lookup` and `detect_bank_from_filename`**

```python
# tests/test_ingestion_common.py
import pytest
from unittest.mock import mock_open, patch

def test_detect_bank_icici_canonical():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("icici_cc_2026_05.pdf") == "icici_cc"

def test_detect_bank_icici_loose():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("Statement_April_2026_ICICI_CC.pdf") == "icici_cc"

def test_detect_bank_amex_loose():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("AMEX_Statement_2026_05.pdf") == "amex_cc"
    assert detect_bank_from_filename("american_express_may.pdf") == "amex_cc"

def test_detect_bank_no_match_returns_none():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("randomfile.pdf") is None

def test_detect_bank_ambiguous_returns_none():
    from skills.finance.ingestion._common import detect_bank_from_filename
    # When BOTH icici and amex tokens appear, watcher must reject as ambiguous
    assert detect_bank_from_filename("icici_amex_partnership_statement.pdf") is None

def test_password_lookup_unique_prefix():
    from skills.finance.ingestion._common import password_lookup
    fake_yaml = """
icici_cc_1008:
  pattern: "custom"
  value: "icici_pass_123"
amex_cc_4003:
  pattern: "NAME_DDMM"
  value: "amex_pass_456"
"""
    with patch("builtins.open", mock_open(read_data=fake_yaml)):
        assert password_lookup("icici_cc") == "icici_pass_123"
        assert password_lookup("amex_cc") == "amex_pass_456"

def test_password_lookup_exact_key_with_last4():
    from skills.finance.ingestion._common import password_lookup
    fake_yaml = """
icici_cc_1008:
  pattern: "custom"
  value: "icici_pass_1008"
icici_cc_9999:
  pattern: "custom"
  value: "icici_pass_9999"
"""
    with patch("builtins.open", mock_open(read_data=fake_yaml)):
        assert password_lookup("icici_cc", last4="1008") == "icici_pass_1008"
        assert password_lookup("icici_cc", last4="9999") == "icici_pass_9999"

def test_password_lookup_ambiguous_without_last4_raises():
    from skills.finance.ingestion._common import password_lookup, AmbiguousCredentialError
    fake_yaml = """
icici_cc_1008:
  value: "p1"
icici_cc_9999:
  value: "p2"
"""
    with patch("builtins.open", mock_open(read_data=fake_yaml)):
        with pytest.raises(AmbiguousCredentialError):
            password_lookup("icici_cc")  # multiple matches, no last4 disambiguator
```

- [ ] **Step 1.2: Run tests, verify all fail**

```bash
cd "/Users/rajat/AntiGravity/Personal finance Agent"
source .venv/bin/activate
pytest tests/test_ingestion_common.py -v
```
Expected: 8 errors (ModuleNotFoundError).

- [ ] **Step 1.3: Implement `_common.py`**

```python
# skills/finance/ingestion/_common.py
"""Shared dataclasses, constants, and pure helpers for the ingestion pipeline.

No I/O outside `password_lookup` (which reads credentials.yaml). The dataclasses
live here so parsers, validator, pipeline, watcher, and doc handler all import
from a single source of truth.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal
import yaml

# V1 single-user. Hardcoded UUID matching the row seeded by 003_seed.local.sql.
# V2 onboarding will move this to settings.py when Ayushi is added.
RAJAT_USER_ID: str = "00000000-0000-0000-0000-000000000001"

Bank = Literal["icici_cc", "amex_cc"]


class AmbiguousCredentialError(Exception):
    """Raised when password_lookup matches multiple credentials.yaml keys
    without a last4 disambiguator. Caller must supply last4."""


class CredentialNotFoundError(Exception):
    """Raised when password_lookup finds no matching key in credentials.yaml."""


@dataclass(frozen=True)
class ParsedRow:
    txn_date: date
    amount: Decimal                       # always positive; sign info on `direction`
    direction: Literal["in", "out"]
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
    source: Literal["manual_pdf", "telegram_pdf", "gmail_cc_stmt"]
    source_ref: str                        # filename / message_id / email_id


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


def detect_bank_from_filename(filename: str) -> Bank | None:
    """Pure function. Lowercase + token match.

    Returns 'icici_cc' if filename contains 'icici' AND 'cc'.
    Returns 'amex_cc' if filename contains 'amex' OR 'american'.
    Returns None if both or neither match (ambiguous and unknown collapse to None;
    caller distinguishes by re-checking).
    """
    name = filename.lower()
    is_icici = ("icici" in name) and ("cc" in name)
    is_amex = ("amex" in name) or ("american" in name)
    if is_icici and is_amex:
        return None  # ambiguous — caller must reject
    if is_icici:
        return "icici_cc"
    if is_amex:
        return "amex_cc"
    return None


def password_lookup(bank: Bank, last4: str | None = None,
                    credentials_path: Path = Path("credentials.yaml")) -> str:
    """Read credentials.yaml. Returns the password for the given bank.

    If last4 is provided, looks up the exact key '<bank>_<last4>'.
    If last4 is None, finds the unique key starting with '<bank>_'. Raises
    AmbiguousCredentialError if multiple match (caller must disambiguate).
    """
    with open(credentials_path) as f:
        creds: dict = yaml.safe_load(f) or {}

    if last4:
        key = f"{bank}_{last4}"
        if key not in creds:
            raise CredentialNotFoundError(
                f"No credential entry for '{key}' in {credentials_path}"
            )
        return creds[key]["value"]

    matching = [k for k in creds if k.startswith(f"{bank}_")]
    if not matching:
        raise CredentialNotFoundError(
            f"No credential entries with prefix '{bank}_' in {credentials_path}"
        )
    if len(matching) > 1:
        raise AmbiguousCredentialError(
            f"Multiple credential entries match '{bank}_*': {matching}. "
            f"Pass last4 to disambiguate."
        )
    return creds[matching[0]]["value"]
```

- [ ] **Step 1.4: Run tests, verify all pass**

```bash
pytest tests/test_ingestion_common.py -v
```
Expected: 8 passed.

- [ ] **Step 1.5: Lint, typecheck**

```bash
make lint && make typecheck
```
Both must be clean.

- [ ] **Step 1.6: Commit**

```bash
git add skills/finance/ingestion/_common.py tests/test_ingestion_common.py
git commit -m "feat(ingestion): shared dataclasses + bank/password helpers (Week 2 Task 1)"
```

---

## Task 2: `statement_validator.py` — pure-fn totals comparator

**Files:**
- Create: `skills/finance/ingestion/statement_validator.py`
- Test: `tests/test_statement_validator.py`

- [ ] **Step 2.1: Write failing tests**

```python
# tests/test_statement_validator.py
from datetime import date
from decimal import Decimal
import pytest
from skills.finance.ingestion._common import ParsedRow, ParseResult


def _make_pr(rows, total_spends, total_credits):
    return ParseResult(
        rows=rows,
        declared_totals={
            "total_spends": Decimal(str(total_spends)),
            "total_credits": Decimal(str(total_credits)),
            "closing_balance": None,
        },
        pdf_content_hash="abc",
        parser_version="test/v1",
    )


def _row(amount, direction, ordinal=1):
    return ParsedRow(
        txn_date=date(2026, 5, 15),
        amount=Decimal(str(amount)),
        direction=direction,
        raw_merchant="test",
        source_row_ordinal=ordinal,
    )


def test_validator_exact_match():
    from skills.finance.ingestion.statement_validator import validate
    pr = _make_pr([_row(100, "out", 1), _row(50, "out", 2), _row(30, "in", 3)], 150, 30)
    res = validate(pr)
    assert res.ok is True
    assert res.delta_out == Decimal("0")
    assert res.delta_in == Decimal("0")


def test_validator_within_tolerance_rs1():
    from skills.finance.ingestion.statement_validator import validate
    # Extracted ₹150 vs declared ₹150.50 — 50p delta, within ₹1 tolerance
    pr = _make_pr([_row(100, "out", 1), _row(50, "out", 2)], "150.50", 0)
    res = validate(pr)
    assert res.ok is True
    assert res.delta_out == Decimal("0.50")


def test_validator_over_tolerance_rejects():
    from skills.finance.ingestion.statement_validator import validate
    # Extracted ₹150 vs declared ₹152 — ₹2 delta, OVER tolerance
    pr = _make_pr([_row(100, "out", 1), _row(50, "out", 2)], 152, 0)
    res = validate(pr)
    assert res.ok is False
    assert res.delta_out == Decimal("2")


def test_validator_all_credits():
    from skills.finance.ingestion.statement_validator import validate
    # Refund-only statement
    pr = _make_pr([_row(200, "in", 1), _row(50, "in", 2)], 0, 250)
    res = validate(pr)
    assert res.ok is True


def test_validator_zero_rows_zero_totals():
    from skills.finance.ingestion.statement_validator import validate
    pr = _make_pr([], 0, 0)
    res = validate(pr)
    assert res.ok is True
    assert res.rows_count == 0


def test_validator_signed_amount_negative_rejected():
    from skills.finance.ingestion.statement_validator import validate
    # Per spec §6.1, amount must be positive; sign info lives on direction.
    # If a parser ever emits negative amount, validator should still compute correctly.
    # (Defensive — abs the deltas.)
    pr = _make_pr([_row(-100, "out", 1)], 100, 0)  # parser bug emits -100
    res = validate(pr)
    # extracted_out = -100 vs declared_out = 100 → delta = 200 → reject
    assert res.ok is False
```

- [ ] **Step 2.2: Run tests, verify all fail**

```bash
pytest tests/test_statement_validator.py -v
```
Expected: 6 errors (ModuleNotFoundError).

- [ ] **Step 2.3: Implement `statement_validator.py`**

```python
# skills/finance/ingestion/statement_validator.py
"""Pure function: compare a ParseResult's row totals against its declared totals.

No I/O. Easy to unit-test. Used by pipeline.py before any DB write."""
from __future__ import annotations
from decimal import Decimal
from skills.finance.ingestion._common import ParseResult, ValidationResult

TOLERANCE: Decimal = Decimal("1.00")


def validate(pr: ParseResult) -> ValidationResult:
    declared_in = pr.declared_totals["total_credits"]
    declared_out = pr.declared_totals["total_spends"]

    extracted_in = sum(
        (r.amount for r in pr.rows if r.direction == "in"), Decimal("0")
    )
    extracted_out = sum(
        (r.amount for r in pr.rows if r.direction == "out"), Decimal("0")
    )

    delta_in = abs(declared_in - extracted_in)
    delta_out = abs(declared_out - extracted_out)

    return ValidationResult(
        ok=(delta_in <= TOLERANCE and delta_out <= TOLERANCE),
        delta_in=delta_in,
        delta_out=delta_out,
        rows_count=len(pr.rows),
        declared_in=declared_in,
        declared_out=declared_out,
        extracted_in=extracted_in,
        extracted_out=extracted_out,
    )
```

- [ ] **Step 2.4: Run tests, verify all pass**

```bash
pytest tests/test_statement_validator.py -v
```
Expected: 6 passed.

- [ ] **Step 2.5: Lint, typecheck, full test suite**

```bash
make lint && make typecheck && make test
```
Expected: clean across the board, total test count = 13 + 8 + 6 = 27 passed (+ 1 skipped from PDF smoke).

- [ ] **Step 2.6: Commit**

```bash
git add skills/finance/ingestion/statement_validator.py tests/test_statement_validator.py
git commit -m "feat(ingestion): pure-fn statement-total validator with ±₹1 tolerance"
```

---

## Task 3: Re-add `images` parameter to `lib/llm.py`

The parameter was removed in Week 1 as YAGNI. Now AMEX parser needs it.

**Files:**
- Modify: `skills/finance/lib/llm.py`
- Modify (extend): `tests/test_llm.py`

- [ ] **Step 3.1: Write failing test for image parameter handling**

Add to `tests/test_llm.py`:

```python
def test_llm_passes_images_to_litellm():
    """Re-added in Week 2 for AMEX page-image extraction."""
    from skills.finance.lib.llm import llm
    from unittest.mock import patch, MagicMock

    fake_b64 = "data:image/png;base64,AAAA"  # placeholder
    with patch("skills.finance.lib.llm.litellm.completion") as mock_completion:
        mock_completion.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="ok"))]
        )
        llm("pdf_extraction", prompt="extract", images=[fake_b64])
        _, kwargs = mock_completion.call_args
        # The `images` are merged into the user message content as multipart
        user_msg = next(m for m in kwargs["messages"] if m["role"] == "user")
        # Content should be a list of dicts when images are present
        assert isinstance(user_msg["content"], list)
        assert any(
            part.get("type") == "image_url" and part["image_url"]["url"] == fake_b64
            for part in user_msg["content"]
        )
```

- [ ] **Step 3.2: Run test, verify it fails**

```bash
pytest tests/test_llm.py::test_llm_passes_images_to_litellm -v
```
Expected: TypeError or assertion failure (images param doesn't exist or isn't passed through).

- [ ] **Step 3.3: Update `lib/llm.py`**

Read the current `lib/llm.py` to find the `llm()` function. Replace its signature and body:

```python
def llm(task: str, prompt: str, system: str | None = None,
        images: list[str] | None = None):
    """Single entry point for all LLM calls. Routes by task name via model_routing.yaml.

    images: optional list of image references (base64 data URLs or http URLs).
    When provided, the user message becomes multipart with one image_url part
    per image plus the text prompt. Used by AMEX page-image extraction (Week 2);
    re-added after Week 1 had removed it as YAGNI.
    """
    if task not in ROUTING:
        raise KeyError(f"Unknown task '{task}'. Known: {list(ROUTING.keys())}")
    cfg = ROUTING[task]

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})

    if images:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for img in images:
            content.append({"type": "image_url", "image_url": {"url": img}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": prompt})

    return litellm.completion(
        model=cfg["model"],
        messages=messages,
        fallbacks=cfg.get("fallbacks", []),
        metadata={"task": task},
    )
```

- [ ] **Step 3.4: Run all `lib/llm.py` tests**

```bash
pytest tests/test_llm.py -v
```
Expected: 3 passed (the 2 existing tests + 1 new image test).

- [ ] **Step 3.5: Lint, typecheck**

```bash
make lint && make typecheck
```

- [ ] **Step 3.6: Commit**

```bash
git add skills/finance/lib/llm.py tests/test_llm.py
git commit -m "feat(llm): re-add images parameter for multimodal extraction (Week 2 prep)"
```

---

## Task 4: ICICI CC parser

**Files:**
- Create: `skills/finance/ingestion/parsers/icici_cc.py`
- Test: `tests/test_icici_cc_parser.py`

The parser is calibrated against the real golden fixture. Tests skip when fixture absent or password env var unset (mirrors Week 1's `test_pdf_smoke.py` pattern).

- [ ] **Step 4.1: Write tests (golden fixture + structure assertions)**

```python
# tests/test_icici_cc_parser.py
from __future__ import annotations
import os
from decimal import Decimal
from pathlib import Path
import pytest

FIXTURE = Path(__file__).parent / "golden_fixtures" / "icici_sample.pdf"
PASSWORD = os.environ.get("ICICI_PDF_PASSWORD", "")


def test_parser_version_string():
    from skills.finance.ingestion.parsers.icici_cc import __parser_version__
    # CLAUDE.md invariant #4 — manually curated; bumping is deliberate.
    assert __parser_version__ == "icici-cc/v1"


@pytest.mark.skipif(not FIXTURE.exists(), reason="ICICI golden fixture not present")
@pytest.mark.skipif(not PASSWORD, reason="ICICI_PDF_PASSWORD env var not set")
def test_parse_returns_nonempty_parseresult():
    from skills.finance.ingestion.parsers.icici_cc import parse
    result = parse(FIXTURE, password=PASSWORD)
    assert len(result.rows) > 0
    assert result.declared_totals["total_spends"] >= Decimal("0")
    assert result.declared_totals["total_credits"] >= Decimal("0")
    assert result.parser_version == "icici-cc/v1"
    assert len(result.pdf_content_hash) == 64  # sha256 hex


@pytest.mark.skipif(not FIXTURE.exists(), reason="ICICI golden fixture not present")
@pytest.mark.skipif(not PASSWORD, reason="ICICI_PDF_PASSWORD env var not set")
def test_parsed_row_fields_well_formed():
    from skills.finance.ingestion.parsers.icici_cc import parse
    result = parse(FIXTURE, password=PASSWORD)
    for row in result.rows:
        assert row.amount > Decimal("0"), "amount must be positive (sign on direction)"
        assert row.direction in ("in", "out")
        assert row.raw_merchant.strip(), "raw_merchant must not be blank"
        assert row.source_row_ordinal >= 1


@pytest.mark.skipif(not FIXTURE.exists(), reason="ICICI golden fixture not present")
@pytest.mark.skipif(not PASSWORD, reason="ICICI_PDF_PASSWORD env var not set")
def test_ordinals_contiguous_1_to_N():
    """CLAUDE.md testing §: assert ordinals contiguous 1..N — catches silent ordering drift
    that would corrupt import_hash on re-parse."""
    from skills.finance.ingestion.parsers.icici_cc import parse
    result = parse(FIXTURE, password=PASSWORD)
    ordinals = [r.source_row_ordinal for r in result.rows]
    assert ordinals == list(range(1, len(result.rows) + 1))


@pytest.mark.skipif(not FIXTURE.exists(), reason="ICICI golden fixture not present")
@pytest.mark.skipif(not PASSWORD, reason="ICICI_PDF_PASSWORD env var not set")
def test_extracted_totals_match_declared_via_validator():
    """End-to-end: parser output → validator → ok=True for a real statement."""
    from skills.finance.ingestion.parsers.icici_cc import parse
    from skills.finance.ingestion.statement_validator import validate
    result = parse(FIXTURE, password=PASSWORD)
    val = validate(result)
    assert val.ok, (
        f"Validator failed on real ICICI fixture — "
        f"delta_in={val.delta_in}, delta_out={val.delta_out}. "
        f"Either parser is wrong, or declared totals on the statement need re-checking."
    )
```

- [ ] **Step 4.2: Run tests, verify the version assertion fails (rest are skipped because parser doesn't exist)**

```bash
pytest tests/test_icici_cc_parser.py -v
```
Expected: ImportError (no parser module yet).

- [ ] **Step 4.3: Implement `icici_cc.py` — first draft, calibrate against fixture**

```python
# skills/finance/ingestion/parsers/icici_cc.py
"""ICICI CC PDF statement parser — deterministic.

Uses pikepdf for password decryption + pdfplumber for text extraction +
regex parsing of the transaction-table region. Calibrated against the real
ICICI CC statement format. Re-calibration required if ICICI changes the
layout — bump __parser_version__ when that happens.
"""
from __future__ import annotations
import hashlib
import re
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pikepdf

from skills.finance.ingestion._common import ParsedRow, ParseResult

# CLAUDE.md invariant #4: manually curated. Bump only when the parser's
# extracted output shape would change for the same input.
__parser_version__ = "icici-cc/v1"

# Matches lines like:  15/05/2026  SWIGGY INSTAMART HSR LAYO...  3,400.00 Cr
# - date in DD/MM/YYYY
# - merchant text (greedy, then non-greedy back to amount)
# - amount with optional thousand separators
# - optional 'Cr' suffix → credit; absence → debit
ROW_RE = re.compile(
    r"^(?P<date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<merchant>.+?)\s+"
    r"(?P<amount>[\d,]+\.\d{2})"
    r"(?:\s*(?P<cr>Cr))?\s*$"
)

# Declared totals labels — calibrate against the actual ICICI statement.
# These are the labels that appear in the summary block of the real statement;
# adjust during calibration if your statement uses different wording.
TOTAL_SPENDS_RE = re.compile(r"Total\s+(?:Purchases|Spends|Debits).*?([\d,]+\.\d{2})", re.IGNORECASE)
TOTAL_CREDITS_RE = re.compile(r"Total\s+(?:Credits|Payments).*?([\d,]+\.\d{2})", re.IGNORECASE)
CLOSING_BAL_RE = re.compile(r"(?:Closing|Total\s+Amount\s+Due).*?([\d,]+\.\d{2})", re.IGNORECASE)


def _parse_amount(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%d/%m/%Y").date()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse(pdf_path: Path, password: str) -> ParseResult:
    """Decrypt the ICICI CC statement and extract rows + declared totals."""
    pdf_path = Path(pdf_path)
    pdf_content_hash = _sha256_file(pdf_path)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        with pikepdf.open(pdf_path, password=password) as src:
            src.save(tmp.name)

        rows: list[ParsedRow] = []
        all_text_parts: list[str] = []
        ordinal = 1
        with pdfplumber.open(tmp.name) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                all_text_parts.append(text)
                for line in text.splitlines():
                    m = ROW_RE.match(line.strip())
                    if not m:
                        continue
                    rows.append(
                        ParsedRow(
                            txn_date=_parse_date(m.group("date")),
                            amount=_parse_amount(m.group("amount")),
                            direction="in" if m.group("cr") else "out",
                            raw_merchant=m.group("merchant").strip(),
                            source_row_ordinal=ordinal,
                        )
                    )
                    ordinal += 1

        full_text = "\n".join(all_text_parts)

    declared_totals: dict = {
        "total_spends": Decimal("0"),
        "total_credits": Decimal("0"),
        "closing_balance": None,
    }

    spends_m = TOTAL_SPENDS_RE.search(full_text)
    if spends_m:
        declared_totals["total_spends"] = _parse_amount(spends_m.group(1))

    credits_m = TOTAL_CREDITS_RE.search(full_text)
    if credits_m:
        declared_totals["total_credits"] = _parse_amount(credits_m.group(1))

    cb_m = CLOSING_BAL_RE.search(full_text)
    if cb_m:
        declared_totals["closing_balance"] = _parse_amount(cb_m.group(1))

    return ParseResult(
        rows=rows,
        declared_totals=declared_totals,
        pdf_content_hash=pdf_content_hash,
        parser_version=__parser_version__,
    )
```

- [ ] **Step 4.4: Run tests against the real fixture**

```bash
pytest tests/test_icici_cc_parser.py -v
```
Expected: 5 passed.

If `test_extracted_totals_match_declared_via_validator` fails, the regex calibration is off. Inspect the failure's `delta_in`/`delta_out`, look at the actual statement layout, and tighten the row regex or the totals labels until validation passes. **Do not lower TOLERANCE in `statement_validator.py` to make tests pass — that defeats the integrity check.**

If `test_parse_returns_nonempty_parseresult` returns 0 rows, the row regex isn't matching. Print a few lines from the PDF to see what format they actually have, and adjust `ROW_RE`.

- [ ] **Step 4.5: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 4.6: Commit**

```bash
git add skills/finance/ingestion/parsers/icici_cc.py tests/test_icici_cc_parser.py
git commit -m "feat(parsers): ICICI CC deterministic parser (Week 2 Task 4)"
```

---

## Task 5: AMEX CC parser (LLM)

**Files:**
- Create: `skills/finance/ingestion/parsers/amex_cc.py`
- Test: `tests/test_amex_cc_parser.py`

- [ ] **Step 5.1: Write failing tests (mocked LLM)**

```python
# tests/test_amex_cc_parser.py
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


def test_amex_parser_version():
    from skills.finance.ingestion.parsers.amex_cc import __parser_version__
    assert __parser_version__ == "amex-cc-llm/v1"


def test_amex_parse_normal_response():
    """Mocked llm() returns canned JSON; parser should produce well-formed rows."""
    from skills.finance.ingestion.parsers.amex_cc import parse

    canned_response_json = json.dumps({
        "rows": [
            {"txn_date": "2026-05-15", "amount": "3400.00", "direction": "out",
             "raw_merchant": "SWIGGY"},
            {"txn_date": "2026-05-20", "amount": "1200.00", "direction": "out",
             "raw_merchant": "BLINKIT"},
            {"txn_date": "2026-05-22", "amount": "500.00", "direction": "in",
             "raw_merchant": "REFUND ZOMATO"},
        ],
        "total_spends": "4600.00",
        "total_credits": "500.00",
        "closing_balance": "4100.00",
    })

    fake_llm_resp = MagicMock(
        choices=[MagicMock(message=MagicMock(content=canned_response_json))]
    )

    with patch("skills.finance.ingestion.parsers.amex_cc.llm",
               return_value=fake_llm_resp), \
         patch("skills.finance.ingestion.parsers.amex_cc.pikepdf.open"), \
         patch("skills.finance.ingestion.parsers.amex_cc.convert_from_path",
               return_value=[MagicMock(), MagicMock()]), \
         patch("skills.finance.ingestion.parsers.amex_cc._image_to_b64",
               return_value="data:image/png;base64,xxx"), \
         patch("skills.finance.ingestion.parsers.amex_cc._sha256_file",
               return_value="a" * 64):
        result = parse(Path("/fake/path.pdf"), password="x")

    assert len(result.rows) == 3
    assert result.rows[0].txn_date == date(2026, 5, 15)
    assert result.rows[0].amount == Decimal("3400.00")
    assert result.rows[0].direction == "out"
    assert result.rows[0].raw_merchant == "SWIGGY"
    assert result.rows[2].direction == "in"
    assert result.declared_totals["total_spends"] == Decimal("4600.00")
    assert result.declared_totals["total_credits"] == Decimal("500.00")
    assert result.parser_version == "amex-cc-llm/v1"


def test_amex_parse_invalid_json_raises():
    """If llm returns malformed JSON, parser raises ParserError."""
    from skills.finance.ingestion.parsers.amex_cc import parse, ParserError

    fake_llm_resp = MagicMock(
        choices=[MagicMock(message=MagicMock(content="this is not json"))]
    )

    with patch("skills.finance.ingestion.parsers.amex_cc.llm",
               return_value=fake_llm_resp), \
         patch("skills.finance.ingestion.parsers.amex_cc.pikepdf.open"), \
         patch("skills.finance.ingestion.parsers.amex_cc.convert_from_path",
               return_value=[MagicMock()]), \
         patch("skills.finance.ingestion.parsers.amex_cc._image_to_b64",
               return_value="data:image/png;base64,xxx"), \
         patch("skills.finance.ingestion.parsers.amex_cc._sha256_file",
               return_value="b" * 64):
        with pytest.raises(ParserError):
            parse(Path("/fake/path.pdf"), password="x")


def test_amex_parse_schema_violation_raises():
    """If llm returns JSON missing required fields, parser raises ParserError."""
    from skills.finance.ingestion.parsers.amex_cc import parse, ParserError

    bad_json = json.dumps({
        "rows": [{"txn_date": "2026-05-15", "amount": "100.00"}],  # missing direction, raw_merchant
        "total_spends": "100.00",
        "total_credits": "0.00",
    })

    fake_llm_resp = MagicMock(
        choices=[MagicMock(message=MagicMock(content=bad_json))]
    )

    with patch("skills.finance.ingestion.parsers.amex_cc.llm",
               return_value=fake_llm_resp), \
         patch("skills.finance.ingestion.parsers.amex_cc.pikepdf.open"), \
         patch("skills.finance.ingestion.parsers.amex_cc.convert_from_path",
               return_value=[MagicMock()]), \
         patch("skills.finance.ingestion.parsers.amex_cc._image_to_b64",
               return_value="data:image/png;base64,xxx"), \
         patch("skills.finance.ingestion.parsers.amex_cc._sha256_file",
               return_value="c" * 64):
        with pytest.raises(ParserError):
            parse(Path("/fake/path.pdf"), password="x")
```

- [ ] **Step 5.2: Run tests, verify they fail (no module yet)**

```bash
pytest tests/test_amex_cc_parser.py -v
```
Expected: 4 errors (ModuleNotFoundError).

- [ ] **Step 5.3: Implement `amex_cc.py`**

```python
# skills/finance/ingestion/parsers/amex_cc.py
"""AMEX CC PDF statement parser — LLM via Gemini Flash vision.

AMEX has no good OSS deterministic parser (per github_research_results.md §1).
We rasterize each page to PNG, send via the `pdf_extraction` LiteLLM task with
a Pydantic-enforced schema, parse the structured response.

Total cost per statement: ~$0.005-0.01 on Gemini Flash free tier.
"""
from __future__ import annotations
import base64
import hashlib
import io
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pikepdf
from pdf2image import convert_from_path
from pydantic import BaseModel, ValidationError

from skills.finance.ingestion._common import ParsedRow, ParseResult
from skills.finance.lib.llm import llm

__parser_version__ = "amex-cc-llm/v1"


class ParserError(Exception):
    """Raised when the LLM response can't be parsed into the expected schema."""


class _LLMRow(BaseModel):
    txn_date: date
    amount: Decimal
    direction: Literal["in", "out"]
    raw_merchant: str


class _LLMStatement(BaseModel):
    rows: list[_LLMRow]
    total_spends: Decimal
    total_credits: Decimal
    closing_balance: Decimal | None = None


_SYSTEM_PROMPT = """You are a precise PDF extractor for AMEX credit card statements. \
Extract every transaction row and the declared statement totals. Output strict JSON \
matching the schema. Do not include any prose, markdown, or commentary — JSON only."""

_USER_PROMPT = """Extract all transactions from these statement page images. For each row:
- txn_date: ISO format YYYY-MM-DD
- amount: positive decimal string (no currency symbol, no thousand separators)
- direction: "out" for charges/debits, "in" for refunds/credits/payments received
- raw_merchant: the merchant or description text as it appears on the statement

Also extract the declared totals from the summary section:
- total_spends: sum of all 'out' direction
- total_credits: sum of all 'in' direction
- closing_balance: the closing/total-amount-due if present, else null

Output strict JSON matching this schema:
{
  "rows": [{"txn_date": "...", "amount": "...", "direction": "...", "raw_merchant": "..."}, ...],
  "total_spends": "...",
  "total_credits": "...",
  "closing_balance": "..." | null
}"""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _image_to_b64(img) -> str:
    """PIL Image → base64 data URL (PNG)."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def parse(pdf_path: Path, password: str) -> ParseResult:
    """Decrypt the AMEX CC statement, rasterize, send to Gemini Flash, parse."""
    pdf_path = Path(pdf_path)
    pdf_content_hash = _sha256_file(pdf_path)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        with pikepdf.open(pdf_path, password=password) as src:
            src.save(tmp.name)

        page_images = convert_from_path(tmp.name)
        b64_images = [_image_to_b64(img) for img in page_images]

    resp = llm(
        "pdf_extraction",
        system=_SYSTEM_PROMPT,
        prompt=_USER_PROMPT,
        images=b64_images,
    )

    raw_content = resp.choices[0].message.content
    try:
        parsed = _LLMStatement.model_validate_json(raw_content)
    except ValidationError as e:
        raise ParserError(f"LLM response failed schema validation: {e}") from e
    except Exception as e:
        raise ParserError(f"LLM response not valid JSON: {e}") from e

    rows: list[ParsedRow] = [
        ParsedRow(
            txn_date=r.txn_date,
            amount=r.amount,
            direction=r.direction,
            raw_merchant=r.raw_merchant.strip(),
            source_row_ordinal=i,
        )
        for i, r in enumerate(parsed.rows, start=1)
    ]

    return ParseResult(
        rows=rows,
        declared_totals={
            "total_spends": parsed.total_spends,
            "total_credits": parsed.total_credits,
            "closing_balance": parsed.closing_balance,
        },
        pdf_content_hash=pdf_content_hash,
        parser_version=__parser_version__,
    )
```

- [ ] **Step 5.4: Run tests, verify all pass**

```bash
pytest tests/test_amex_cc_parser.py -v
```
Expected: 4 passed.

- [ ] **Step 5.5: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 5.6: Commit**

```bash
git add skills/finance/ingestion/parsers/amex_cc.py tests/test_amex_cc_parser.py
git commit -m "feat(parsers): AMEX CC LLM-based parser via Gemini Flash + Pydantic schema"
```

---

## Task 6: `pipeline.py` — orchestrator

**Files:**
- Create: `skills/finance/ingestion/pipeline.py`
- Test: `tests/test_pipeline.py`

This task uses the supabase-py upsert behavior verified in Task 0.2. If Task 0 found `upsert(..., on_conflict='import_hash', ignore_duplicates=True)` doesn't work as expected, **substitute the per-row insert + try/except fallback in Step 6.3 before writing.**

- [ ] **Step 6.1: Write failing tests (mocked Supabase)**

```python
# tests/test_pipeline.py
import asyncio
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import UUID

from skills.finance.ingestion._common import ParsedRow, ParseResult, SourceMeta


ICICI_CC_ACCOUNT_ID = UUID("10000000-0000-0000-0000-000000000003")


def _make_pr(rows, total_spends, total_credits, parser_version="icici-cc/v1"):
    return ParseResult(
        rows=rows,
        declared_totals={
            "total_spends": Decimal(str(total_spends)),
            "total_credits": Decimal(str(total_credits)),
            "closing_balance": None,
        },
        pdf_content_hash="abc" * 21 + "z",  # 64 chars
        parser_version=parser_version,
    )


def _row(amount, ordinal, direction="out", merchant="test"):
    return ParsedRow(
        txn_date=date(2026, 5, 15),
        amount=Decimal(str(amount)),
        direction=direction,
        raw_merchant=merchant,
        source_row_ordinal=ordinal,
    )


def test_pipeline_validation_failure_no_insert():
    """Totals mismatch → no transactions inserted, ingestion_log gets total_check_failed."""
    from skills.finance.ingestion.pipeline import ingest

    # Extracted ₹150 vs declared ₹200 → off by ₹50, fails validation
    pr = _make_pr([_row(100, 1), _row(50, 2)], 200, 0)
    source = SourceMeta(source="manual_pdf", source_ref="test.pdf")

    txn_inserts = []
    log_inserts = []

    def mock_insert(self, table_name):
        builder = MagicMock()
        if table_name == "transactions":
            builder.upsert = lambda rows, **kw: _record(txn_inserts, rows, builder)
            builder.insert = lambda rows: _record(txn_inserts, rows, builder)
        elif table_name == "ingestion_log":
            builder.insert = lambda row: _record(log_inserts, row, builder)
        return builder

    def _record(target, payload, builder):
        target.append(payload)
        builder.execute = lambda: MagicMock(data=payload if isinstance(payload, list) else [payload])
        return builder

    fake_client = MagicMock()
    fake_client.table.side_effect = lambda t: mock_insert(fake_client, t)

    with patch("skills.finance.ingestion.pipeline.service_client", return_value=fake_client):
        result = asyncio.run(ingest(pr, ICICI_CC_ACCOUNT_ID, source))

    # transactions.upsert / .insert should NOT have been called
    assert len(txn_inserts) == 0, f"Expected no transactions insert, got {txn_inserts}"
    # ingestion_log should have one entry with status='total_check_failed'
    assert len(log_inserts) == 1
    assert log_inserts[0]["status"] == "total_check_failed"
    assert result["status"] == "total_check_failed"


def test_pipeline_success_inserts_rows_and_logs():
    """Validation passes → all rows inserted, ingestion_log gets status='success'."""
    from skills.finance.ingestion.pipeline import ingest

    pr = _make_pr([_row(100, 1, merchant="SWIGGY"), _row(50, 2, merchant="BLINKIT")], 150, 0)
    source = SourceMeta(source="manual_pdf", source_ref="test.pdf")

    txn_inserts = []
    log_inserts = []

    def mock_table(name):
        builder = MagicMock()
        if name == "transactions":
            def _upsert(rows, **kw):
                txn_inserts.append(rows)
                builder.execute = lambda: MagicMock(data=rows)
                return builder
            builder.upsert = _upsert
        elif name == "ingestion_log":
            def _insert(row):
                log_inserts.append(row)
                builder.execute = lambda: MagicMock(data=[row])
                return builder
            builder.insert = _insert
        return builder

    fake_client = MagicMock()
    fake_client.table.side_effect = mock_table

    with patch("skills.finance.ingestion.pipeline.service_client", return_value=fake_client):
        result = asyncio.run(ingest(pr, ICICI_CC_ACCOUNT_ID, source))

    assert len(txn_inserts) == 1
    inserted = txn_inserts[0]
    assert len(inserted) == 2
    assert inserted[0]["raw_merchant"] == "SWIGGY"
    # import_hash is computed; sanity-check it's a 64-char hex string
    assert len(inserted[0]["import_hash"]) == 64
    # parser_version threaded in
    assert inserted[0]["parser_version"] == "icici-cc/v1"
    assert result["status"] == "success"


def test_pipeline_import_hash_per_row_uses_mode_b():
    """Verify pipeline calls import_hash_pdf with the right arguments."""
    from skills.finance.ingestion.pipeline import ingest

    pr = _make_pr([_row(350, 1, merchant="SWIGGY"), _row(350, 2, merchant="SWIGGY")], 700, 0)
    source = SourceMeta(source="manual_pdf", source_ref="test.pdf")

    captured_hashes = []

    def fake_import_hash_pdf(**kwargs):
        captured_hashes.append(kwargs)
        return "h" * 64  # placeholder

    txn_inserts = []
    log_inserts = []

    def mock_table(name):
        builder = MagicMock()
        if name == "transactions":
            def _upsert(rows, **kw):
                txn_inserts.append(rows)
                builder.execute = lambda: MagicMock(data=rows)
                return builder
            builder.upsert = _upsert
        elif name == "ingestion_log":
            def _insert(row):
                log_inserts.append(row)
                builder.execute = lambda: MagicMock(data=[row])
                return builder
            builder.insert = _insert
        return builder

    fake_client = MagicMock()
    fake_client.table.side_effect = mock_table

    with patch("skills.finance.ingestion.pipeline.service_client", return_value=fake_client), \
         patch("skills.finance.ingestion.pipeline.import_hash_pdf",
               side_effect=fake_import_hash_pdf):
        asyncio.run(ingest(pr, ICICI_CC_ACCOUNT_ID, source))

    # Two rows = two hash calls. The Swiggy-Swiggy intra-PDF case must be disambiguated
    # by source_row_ordinal in the hash inputs.
    assert len(captured_hashes) == 2
    assert captured_hashes[0]["source_row_ordinal"] == 1
    assert captured_hashes[1]["source_row_ordinal"] == 2
    assert captured_hashes[0]["pdf_content_hash"] == captured_hashes[1]["pdf_content_hash"]
    assert captured_hashes[0]["parser_version"] == "icici-cc/v1"
```

- [ ] **Step 6.2: Run tests, verify failures**

```bash
pytest tests/test_pipeline.py -v
```
Expected: 3 errors (ModuleNotFoundError).

- [ ] **Step 6.3: Implement `pipeline.py`**

```python
# skills/finance/ingestion/pipeline.py
"""Ingestion pipeline orchestrator.

Runs: validate → compute Mode-B import_hash per row → upsert to transactions →
log to ingestion_log → send Telegram summary. On totals failure, NO rows are
inserted (per spec §8 / PRD §18.4 — entire statement rejected).
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any
from uuid import UUID

from skills.finance.ingestion._common import (
    ParseResult,
    ParsedRow,
    RAJAT_USER_ID,
    SourceMeta,
    ValidationResult,
)
from skills.finance.ingestion.statement_validator import validate
from skills.finance.lib.db import adb, service_client
from skills.finance.lib.hashing import import_hash_pdf

logger = logging.getLogger(__name__)


def _build_insert_row(
    r: ParsedRow,
    account_id: UUID,
    pr: ParseResult,
    source: SourceMeta,
) -> dict[str, Any]:
    """Build a single transactions row dict for Supabase insert.

    NB: normalized_description = raw_merchant in Week 2; merchant normalization
    lands in Week 5 with a parser_version bump that forces re-ingest of the
    affected rows."""
    h = import_hash_pdf(
        account_id=str(account_id),
        txn_date=r.txn_date,
        amount=r.amount,
        normalized_description=r.raw_merchant,
        pdf_content_hash=pr.pdf_content_hash,
        source_row_ordinal=r.source_row_ordinal,
        parser_version=pr.parser_version,
    )
    return {
        "user_id": RAJAT_USER_ID,
        "account_id": str(account_id),
        "date": r.txn_date.isoformat(),
        "amount": str(r.amount),
        "currency": "INR",
        "direction": r.direction,
        "raw_merchant": r.raw_merchant,
        "source": source.source,
        "source_ref": source.source_ref,
        "pdf_content_hash": pr.pdf_content_hash,
        "source_row_ordinal": r.source_row_ordinal,
        "parser_version": pr.parser_version,
        "import_hash": h,
    }


async def _log_validation_failure(
    pr: ParseResult,
    source: SourceMeta,
    val: ValidationResult,
) -> dict[str, Any]:
    log_row = {
        "source": source.source,
        "source_ref": source.source_ref,
        "status": "total_check_failed",
        "rows_added": 0,
        "declared_total": str(val.declared_out),
        "extracted_total": str(val.extracted_out),
        "error_msg": (
            f"Totals mismatch: delta_in={val.delta_in}, delta_out={val.delta_out} "
            f"(tolerance ₹1). Statement NOT ingested."
        ),
    }
    await adb(
        lambda: service_client().table("ingestion_log").insert(log_row).execute()
    )
    return log_row


async def _log_success(
    pr: ParseResult,
    source: SourceMeta,
    val: ValidationResult,
    rows_added: int,
) -> dict[str, Any]:
    status = "success" if rows_added > 0 else "skipped_duplicate"
    log_row = {
        "source": source.source,
        "source_ref": source.source_ref,
        "status": status,
        "rows_added": rows_added,
        "declared_total": str(val.declared_out),
        "extracted_total": str(val.extracted_out),
        "error_msg": None,
    }
    await adb(
        lambda: service_client().table("ingestion_log").insert(log_row).execute()
    )
    return log_row


async def ingest(
    parse_result: ParseResult,
    account_id: UUID,
    source_meta: SourceMeta,
) -> dict[str, Any]:
    """Orchestrate validate → upsert → log. Returns the ingestion_log row.

    Caller is responsible for sending the Telegram summary message; this fn
    keeps the pipeline pure (no Telegram I/O). pipeline.ingest is the unit
    that gets unit-tested with mocked service_client."""
    val = validate(parse_result)
    if not val.ok:
        logger.warning(
            "validation failed for %s/%s: delta_in=%s delta_out=%s",
            source_meta.source, source_meta.source_ref,
            val.delta_in, val.delta_out,
        )
        return await _log_validation_failure(parse_result, source_meta, val)

    rows = [
        _build_insert_row(r, account_id, parse_result, source_meta)
        for r in parse_result.rows
    ]

    response = await adb(
        lambda: service_client()
            .table("transactions")
            .upsert(rows, on_conflict="import_hash", ignore_duplicates=True)
            .execute()
    )
    rows_added = len(response.data) if response.data else 0
    logger.info(
        "ingested %d rows from %s/%s (validator ok, %d total)",
        rows_added, source_meta.source, source_meta.source_ref, len(rows),
    )
    return await _log_success(parse_result, source_meta, val, rows_added)
```

- [ ] **Step 6.4: Run tests, verify all pass**

```bash
pytest tests/test_pipeline.py -v
```
Expected: 3 passed.

- [ ] **Step 6.5: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 6.6: Commit**

```bash
git add skills/finance/ingestion/pipeline.py tests/test_pipeline.py
git commit -m "feat(ingestion): pipeline orchestrator (validate → import_hash → upsert → log)"
```

---

## Task 7: `calibration.py` — AMEX dual-model diff

**Files:**
- Create: `skills/finance/ingestion/calibration.py`
- Test: `tests/test_calibration.py`

- [ ] **Step 7.1: Write failing tests**

```python
# tests/test_calibration.py
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

from skills.finance.ingestion._common import ParsedRow, ParseResult


def _row(amount, ordinal, direction="out", merchant="test", txn_date=None):
    return ParsedRow(
        txn_date=txn_date or date(2026, 5, 15),
        amount=Decimal(str(amount)),
        direction=direction,
        raw_merchant=merchant,
        source_row_ordinal=ordinal,
    )


def _make_pr(rows, parser_version="amex-cc-llm/v1"):
    return ParseResult(
        rows=rows, declared_totals={"total_spends": Decimal("0"), "total_credits": Decimal("0"),
                                    "closing_balance": None},
        pdf_content_hash="x" * 64, parser_version=parser_version,
    )


def test_diff_perfect_agreement_no_disagreements():
    from skills.finance.ingestion.calibration import diff_extractions
    a = _make_pr([_row(100, 1, merchant="SWIGGY"), _row(50, 2, merchant="BLINKIT")])
    b = _make_pr([_row(100, 1, merchant="SWIGGY"), _row(50, 2, merchant="BLINKIT")])
    disagreements = diff_extractions(a, b)
    assert disagreements == []


def test_diff_merchant_differs_one_disagreement():
    from skills.finance.ingestion.calibration import diff_extractions
    a = _make_pr([_row(100, 1, merchant="SWIGGY"), _row(50, 2, merchant="BLINKIT")])
    b = _make_pr([_row(100, 1, merchant="ZOMATO"), _row(50, 2, merchant="BLINKIT")])
    disagreements = diff_extractions(a, b)
    assert len(disagreements) == 1
    assert disagreements[0].kind == "merchant_differs"
    assert disagreements[0].a_row.raw_merchant == "SWIGGY"
    assert disagreements[0].b_row.raw_merchant == "ZOMATO"


def test_diff_one_side_missing_row():
    from skills.finance.ingestion.calibration import diff_extractions
    a = _make_pr([_row(100, 1, merchant="SWIGGY"), _row(50, 2, merchant="BLINKIT")])
    b = _make_pr([_row(100, 1, merchant="SWIGGY")])  # missing the second row
    disagreements = diff_extractions(a, b)
    assert len(disagreements) == 1
    assert disagreements[0].kind == "missing_in_b"


def test_diff_multiple_disagreements():
    from skills.finance.ingestion.calibration import diff_extractions
    a = _make_pr([_row(100, 1, merchant="A"), _row(50, 2, merchant="B"), _row(30, 3, merchant="C")])
    b = _make_pr([_row(100, 1, merchant="A"), _row(50, 2, merchant="X")])  # different B, missing C
    disagreements = diff_extractions(a, b)
    assert len(disagreements) == 2
    kinds = sorted(d.kind for d in disagreements)
    assert kinds == ["merchant_differs", "missing_in_b"]


def test_cost_cap_check_under_budget():
    from skills.finance.ingestion.calibration import calibration_cost_so_far_inr
    fake_client = MagicMock()
    fake_client.table.return_value.select.return_value.gte.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"total_cost": 0.05}, {"total_cost": 0.03}]  # USD; cumulative 0.08
    )
    inr = calibration_cost_so_far_inr(client=fake_client, since_iso="2026-04-26T00:00:00Z")
    # 0.08 USD × ~₹84/USD ≈ ₹6.72
    assert Decimal("6") < inr < Decimal("8")


def test_cost_cap_check_at_budget():
    from skills.finance.ingestion.calibration import is_calibration_over_budget
    assert is_calibration_over_budget(spent_inr=Decimal("199")) is False
    assert is_calibration_over_budget(spent_inr=Decimal("200")) is True
    assert is_calibration_over_budget(spent_inr=Decimal("250")) is True
```

- [ ] **Step 7.2: Run tests, verify failures**

```bash
pytest tests/test_calibration.py -v
```
Expected: 6 errors.

- [ ] **Step 7.3: Implement `calibration.py`**

```python
# skills/finance/ingestion/calibration.py
"""AMEX dual-model calibration — Month 1 only.

Runs both Gemini Flash + Claude Haiku on the same AMEX statement, diffs the
row sets, surfaces disagreements via Telegram for adjudication. Hard cost cap:
₹200 across all AMEX calibration runs (spec §9.2).
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import litellm

from skills.finance.ingestion._common import ParsedRow, ParseResult
from skills.finance.lib.db import service_client

logger = logging.getLogger(__name__)

# ₹200 cap from PRD §18.4 + spec §9.2. Brainstorm Q4 locked this.
CALIBRATION_BUDGET_INR: Decimal = Decimal("200")

# USD → INR conversion for budget-comparison only. Approximate; exact rate
# doesn't matter — we just want to detect "approaching cap" and bail.
USD_TO_INR_APPROX: Decimal = Decimal("84")


@dataclass(frozen=True)
class Disagreement:
    """A single point of disagreement between two parser extractions."""
    kind: Literal["merchant_differs", "missing_in_a", "missing_in_b"]
    a_row: ParsedRow | None
    b_row: ParsedRow | None
    join_key: tuple                       # (txn_date, amount, direction) for context


def _row_key(r: ParsedRow) -> tuple:
    return (r.txn_date, r.amount, r.direction)


def diff_extractions(a: ParseResult, b: ParseResult) -> list[Disagreement]:
    """Compare two ParseResults row-by-row.

    Strategy: inner-join on (txn_date, amount, direction). Rows present in both
    but with different `raw_merchant` are 'merchant_differs'. Rows present in
    only one are 'missing_in_a' or 'missing_in_b'.
    """
    a_by_key: dict[tuple, ParsedRow] = {_row_key(r): r for r in a.rows}
    b_by_key: dict[tuple, ParsedRow] = {_row_key(r): r for r in b.rows}

    disagreements: list[Disagreement] = []

    for key, a_row in a_by_key.items():
        if key not in b_by_key:
            disagreements.append(Disagreement(
                kind="missing_in_b", a_row=a_row, b_row=None, join_key=key
            ))
        else:
            b_row = b_by_key[key]
            if a_row.raw_merchant.strip().lower() != b_row.raw_merchant.strip().lower():
                disagreements.append(Disagreement(
                    kind="merchant_differs", a_row=a_row, b_row=b_row, join_key=key
                ))

    for key, b_row in b_by_key.items():
        if key not in a_by_key:
            disagreements.append(Disagreement(
                kind="missing_in_a", a_row=None, b_row=b_row, join_key=key
            ))

    return disagreements


def calibration_cost_so_far_inr(
    client=None,
    since_iso: str = "2026-04-26T00:00:00Z",
) -> Decimal:
    """Sum total_cost from request_logs where metadata->>'task' = 'pdf_extraction'
    and created_at >= since_iso. Returns INR (approximate, USD→INR×84)."""
    c = client or service_client()
    rows = (
        c.table("request_logs")
        .select("total_cost")
        .gte("created_at", since_iso)
        .eq("metadata->>task", "pdf_extraction")  # NB: PostgREST JSON op
        .execute()
    ).data or []
    total_usd = sum(
        (Decimal(str(r.get("total_cost") or 0)) for r in rows),
        Decimal("0"),
    )
    return (total_usd * USD_TO_INR_APPROX).quantize(Decimal("0.01"))


def is_calibration_over_budget(spent_inr: Decimal) -> bool:
    return spent_inr >= CALIBRATION_BUDGET_INR


async def calibrate_amex(
    pdf_path,
    password: str,
    bot=None,                   # aiogram Bot for surfacing disagreements; None in tests
    chat_id: str | None = None,
) -> ParseResult:
    """Run both Gemini Flash + Claude Haiku on the AMEX PDF; diff; surface
    disagreements via Telegram for user adjudication; return the chosen rows
    as a ParseResult.

    Cost-cap guarded: if request_logs shows > ₹200 spent on pdf_extraction
    since calibration-start, abort calibration and return the Gemini-Flash-only
    extraction (logged + alerted)."""
    spent = calibration_cost_so_far_inr()
    if is_calibration_over_budget(spent):
        logger.warning(
            "calibration cost cap (₹%s) reached (spent ₹%s); falling back to Gemini-only",
            CALIBRATION_BUDGET_INR, spent,
        )
        # Fall back to single-model extraction via the standard amex_cc parser.
        from skills.finance.ingestion.parsers.amex_cc import parse as amex_parse
        return amex_parse(pdf_path, password=password)

    # Run both extractors. The standard parser uses gemini-flash via routing config;
    # for the second pass, override the model to Claude Haiku.
    from skills.finance.ingestion.parsers.amex_cc import parse as amex_parse_default
    a = amex_parse_default(pdf_path, password=password)

    # For Claude Haiku, temporarily swap the routing — minimal approach for V1:
    # call litellm.completion directly with the same prompts but a different model.
    # (A cleaner approach is to add a `task` parameter override to llm(); deferred
    # to keep the surface area small. Document this trade-off.)
    b = await _amex_parse_via_haiku(pdf_path, password=password)

    disagreements = diff_extractions(a, b)
    if not disagreements:
        # Both extractors agree on every row — return either.
        return a

    if bot is None or chat_id is None:
        # Test mode: no Telegram. Return `a` (Gemini) by default and log.
        logger.info("calibrate_amex: %d disagreements, no bot — returning Gemini set", len(disagreements))
        return a

    # Production: surface each disagreement, await user response, build the chosen row set.
    # Implementation deferred to Step 7.4 (the human-in-the-loop flow needs aiogram
    # Update objects to receive replies; we'll wire it in step-by-step).
    raise NotImplementedError(
        "Telegram-driven adjudication is wired in Step 7.4 of Task 7. "
        "Calling this in production before that step lands is a programmer error."
    )


async def _amex_parse_via_haiku(pdf_path, password: str) -> ParseResult:
    """Helper: run the same AMEX extraction prompt but with Claude Haiku as the model.

    This is the SECOND extraction in the dual-model calibration. Implementation
    duplicates parsers/amex_cc.parse but uses litellm.completion directly with
    model='anthropic/claude-haiku-4-5-20251001' rather than going through llm().
    Kept as a private helper here (not in parsers/) because it's calibration-only
    and should never be the default path."""
    # ... implementation duplicates parsers/amex_cc.parse but with the Claude Haiku
    # model. To keep the plan tight, we implement this in Step 7.4 alongside the
    # Telegram adjudication flow.
    raise NotImplementedError("Implemented in Step 7.4")
```

- [ ] **Step 7.4: Implement the Haiku helper + Telegram adjudication**

Replace the two `NotImplementedError` placeholders with real implementations:

```python
# Append/replace inside calibration.py

import base64
import io
import json
import tempfile

import litellm
import pikepdf
from pdf2image import convert_from_path
from pydantic import ValidationError

from skills.finance.ingestion.parsers.amex_cc import (
    _LLMStatement,
    _SYSTEM_PROMPT,
    _USER_PROMPT,
    _image_to_b64,
    _sha256_file,
    ParserError,
    __parser_version__ as _AMEX_VERSION,
)


async def _amex_parse_via_haiku(pdf_path, password: str) -> ParseResult:
    """Same prompts/schema as parsers/amex_cc.parse, but model = Claude Haiku."""
    from pathlib import Path
    pdf_path = Path(pdf_path)
    pdf_content_hash = _sha256_file(pdf_path)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        with pikepdf.open(pdf_path, password=password) as src:
            src.save(tmp.name)
        page_images = convert_from_path(tmp.name)
        b64_images = [_image_to_b64(img) for img in page_images]

    content: list[dict] = [{"type": "text", "text": _USER_PROMPT}]
    for img in b64_images:
        content.append({"type": "image_url", "image_url": {"url": img}})

    resp = litellm.completion(
        model="anthropic/claude-haiku-4-5-20251001",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        metadata={"task": "pdf_extraction", "calibration": "haiku"},
    )

    raw = resp.choices[0].message.content
    try:
        parsed = _LLMStatement.model_validate_json(raw)
    except ValidationError as e:
        raise ParserError(f"Haiku response failed schema validation: {e}") from e

    rows = [
        ParsedRow(
            txn_date=r.txn_date,
            amount=r.amount,
            direction=r.direction,
            raw_merchant=r.raw_merchant.strip(),
            source_row_ordinal=i,
        )
        for i, r in enumerate(parsed.rows, start=1)
    ]

    return ParseResult(
        rows=rows,
        declared_totals={
            "total_spends": parsed.total_spends,
            "total_credits": parsed.total_credits,
            "closing_balance": parsed.closing_balance,
        },
        pdf_content_hash=pdf_content_hash,
        parser_version=f"{_AMEX_VERSION}-calibration-haiku",  # marker; not used as primary
    )
```

For the Telegram adjudication flow inside `calibrate_amex`, this requires bidirectional bot communication (asking user, awaiting reply). For Week 2, implement a **simpler MVP**:

```python
# Replace the NotImplementedError block in calibrate_amex with:

# Production path: surface a single combined message, log all disagreements,
# default to Gemini's choices, but write to ingestion_log status='needs_review'
# with the disagreements as JSON in error_msg. User reviews offline + corrects
# specific rows post-ingest using Week 4's /categorize command.
#
# Reasoning: full inline-adjudication-via-Telegram requires per-disagreement
# state management (FSM), which is its own subsystem. For Month 1's 2-3 AMEX
# statements, the disagreement count is expected to be 0-3 per statement. Logging
# them and letting Rajat fix them via /categorize (Week 4) is acceptable friction.
# If volume turns out higher than expected, escalate.

if bot is not None and chat_id is not None:
    summary = (
        f"🧪 AMEX CC calibration — {len(disagreements)} disagreement(s)\n\n"
    )
    for d in disagreements[:10]:  # cap at 10 lines per message; rare to exceed
        if d.kind == "merchant_differs":
            summary += (
                f"• Row {d.a_row.source_row_ordinal} merchant differs:\n"
                f"  Gemini → {d.a_row.raw_merchant}\n"
                f"  Haiku  → {d.b_row.raw_merchant}\n"
                f"  Date {d.a_row.txn_date}, ₹{d.a_row.amount}\n\n"
            )
        elif d.kind == "missing_in_b":
            summary += (
                f"• Row {d.a_row.source_row_ordinal} only in Gemini: "
                f"{d.a_row.raw_merchant} ₹{d.a_row.amount}\n\n"
            )
        elif d.kind == "missing_in_a":
            summary += (
                f"• Row only in Haiku: "
                f"{d.b_row.raw_merchant} ₹{d.b_row.amount}\n\n"
            )
    summary += (
        "Defaulting to Gemini extraction. Use /categorize <txn_id> after ingest "
        "(Week 4) to correct individual rows if needed."
    )
    await bot.send_message(chat_id=chat_id, text=summary)

logger.info(
    "calibrate_amex: %d disagreements logged; Gemini set used for ingest",
    len(disagreements),
)
return a
```

- [ ] **Step 7.5: Run tests, verify all pass**

```bash
pytest tests/test_calibration.py -v
```
Expected: 6 passed.

- [ ] **Step 7.6: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 7.7: Commit**

```bash
git add skills/finance/ingestion/calibration.py tests/test_calibration.py
git commit -m "feat(ingestion): AMEX dual-model calibration with ₹200 cap and disagreement diff"
```

---

## Task 8: `folder_watcher.py`

**Files:**
- Create: `skills/finance/ingestion/folder_watcher.py`
- Test: `tests/test_folder_watcher.py`

- [ ] **Step 8.1: Write failing tests (mocked watchdog + filesystem)**

```python
# tests/test_folder_watcher.py
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


@pytest.fixture
def tmp_inbox(tmp_path):
    inbox = tmp_path / "finance-inbox"
    inbox.mkdir()
    return inbox


def test_dispatch_unknown_filename_renames_to_rejected(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_pdf
    f = tmp_inbox / "random.pdf"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.send_alert") as mock_alert, \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser") as mock_dispatch:
        asyncio.run(handle_new_pdf(f))
    assert (tmp_inbox / "random.pdf.rejected.pdf").exists()
    assert not f.exists()
    mock_dispatch.assert_not_called()
    mock_alert.assert_called_once()


def test_dispatch_icici_filename_calls_parser(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_pdf
    f = tmp_inbox / "icici_cc_2026_05.pdf"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.password_lookup",
               return_value="testpass"), \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser") as mock_dispatch:
        asyncio.run(handle_new_pdf(f))
    mock_dispatch.assert_called_once()
    call_args = mock_dispatch.call_args
    assert call_args.kwargs["bank"] == "icici_cc"
    assert call_args.kwargs["password"] == "testpass"


def test_dispatch_amex_filename_calls_parser(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_pdf
    f = tmp_inbox / "amex_cc_2026_05.pdf"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.password_lookup",
               return_value="testpass"), \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser") as mock_dispatch:
        asyncio.run(handle_new_pdf(f))
    mock_dispatch.assert_called_once()
    assert mock_dispatch.call_args.kwargs["bank"] == "amex_cc"


def test_dispatch_ambiguous_filename_alerts_and_rejects(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_pdf
    f = tmp_inbox / "icici_amex_partnership.pdf"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.send_alert") as mock_alert, \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser") as mock_dispatch:
        asyncio.run(handle_new_pdf(f))
    assert (tmp_inbox / "icici_amex_partnership.pdf.rejected.pdf").exists()
    mock_dispatch.assert_not_called()
    assert mock_alert.called
    # Alert message should mention "ambiguous"
    assert "ambiguous" in str(mock_alert.call_args).lower()


def test_already_rejected_files_are_ignored(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_pdf
    f = tmp_inbox / "random.pdf.rejected.pdf"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser") as mock_dispatch, \
         patch("skills.finance.ingestion.folder_watcher.send_alert") as mock_alert:
        asyncio.run(handle_new_pdf(f))
    mock_dispatch.assert_not_called()
    mock_alert.assert_not_called()
    # File should remain in place
    assert f.exists()
```

- [ ] **Step 8.2: Run tests, verify failures**

```bash
pytest tests/test_folder_watcher.py -v
```
Expected: 5 errors.

- [ ] **Step 8.3: Implement `folder_watcher.py`**

```python
# skills/finance/ingestion/folder_watcher.py
"""Watches ~/finance-inbox/ for new PDFs and dispatches to the right parser.

Single-threaded: files are processed sequentially in arrival order. Spec §5.1
locks this for V1 (debugging simplicity). Async file ops via asyncio.to_thread."""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from uuid import UUID

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from skills.finance.ingestion._common import (
    AmbiguousCredentialError,
    CredentialNotFoundError,
    SourceMeta,
    detect_bank_from_filename,
    password_lookup,
)
from skills.finance.ingestion.pipeline import ingest
from skills.finance.lib.settings import settings
from skills.finance.monitoring.alerts import send_alert

logger = logging.getLogger(__name__)

# Account UUIDs from 003_seed.local.sql
ACCOUNT_IDS: dict[str, UUID] = {
    "icici_cc": UUID("10000000-0000-0000-0000-000000000003"),
    "amex_cc": UUID("10000000-0000-0000-0000-000000000005"),
}


async def dispatch_to_parser(
    pdf_path: Path,
    bank: str,
    password: str,
) -> None:
    """Call the right parser, then ingest. Calibration is wired in for AMEX
    Month 1; for now, dispatch to the standard parser."""
    if bank == "icici_cc":
        from skills.finance.ingestion.parsers.icici_cc import parse as icici_parse
        parse_result = await asyncio.to_thread(icici_parse, pdf_path, password)
    elif bank == "amex_cc":
        # During Month 1 calibration window, route via calibration.calibrate_amex.
        # For now (Week 2 plan), wire the standard parser; calibration wiring
        # is folded into Task 7's run-time invocation by the orchestrator.
        from skills.finance.ingestion.parsers.amex_cc import parse as amex_parse
        parse_result = await asyncio.to_thread(amex_parse, pdf_path, password)
    else:
        raise ValueError(f"Unknown bank: {bank}")

    source_meta = SourceMeta(source="manual_pdf", source_ref=pdf_path.name)
    account_id = ACCOUNT_IDS[bank]
    await ingest(parse_result, account_id, source_meta)


async def handle_new_pdf(pdf_path: Path) -> None:
    """Top-level handler for any new PDF. Routes by filename token match.

    On unknown / ambiguous filename: rename to <name>.rejected.pdf so re-scans
    don't re-trigger; send alert."""
    name = pdf_path.name
    # Already-rejected files: ignore
    if name.endswith(".rejected.pdf"):
        logger.debug("ignoring already-rejected file: %s", name)
        return

    bank = detect_bank_from_filename(name)
    if bank is None:
        # Either no token match or ambiguous (both icici and amex matched).
        # detect_bank_from_filename returns None for both; we re-check to write
        # a clearer alert message.
        n = name.lower()
        is_icici = ("icici" in n) and ("cc" in n)
        is_amex = ("amex" in n) or ("american" in n)
        if is_icici and is_amex:
            msg = f"Ambiguous filename '{name}' — matches both ICICI and AMEX patterns. Rejected."
        else:
            msg = f"Filename '{name}' doesn't match any known bank pattern. Rename to include 'icici_cc_' or 'amex_cc_' and re-drop. Rejected."
        rejected = pdf_path.with_suffix(".pdf.rejected.pdf")
        # If the file is already named foo.pdf, this becomes foo.pdf.rejected.pdf
        # via with_suffix replacing .pdf with .pdf.rejected.pdf would actually
        # overwrite — use the rename approach explicitly:
        rejected = pdf_path.parent / f"{name}.rejected.pdf"
        await asyncio.to_thread(pdf_path.rename, rejected)
        await send_alert(msg)
        return

    try:
        password = await asyncio.to_thread(password_lookup, bank)
    except (AmbiguousCredentialError, CredentialNotFoundError) as e:
        logger.exception("password lookup failed for %s", bank)
        rejected = pdf_path.parent / f"{name}.rejected.pdf"
        await asyncio.to_thread(pdf_path.rename, rejected)
        await send_alert(f"Password lookup for {bank} failed: {e}. {name} rejected.")
        return

    try:
        await dispatch_to_parser(pdf_path, bank=bank, password=password)
    except Exception as e:  # noqa: BLE001
        logger.exception("dispatch failed for %s", name)
        await send_alert(f"Ingestion failed for {name}: {e}")


class _PdfEventHandler(FileSystemEventHandler):
    """Bridges sync watchdog callbacks to the async event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop
        self._processing_lock = asyncio.Lock()

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_handle(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Use destination path on move events
        self._maybe_handle(event, path_attr="dest_path")

    def _maybe_handle(self, event: FileSystemEvent, path_attr: str = "src_path") -> None:
        if event.is_directory:
            return
        path = Path(getattr(event, path_attr))
        if path.suffix.lower() != ".pdf":
            return
        # Hop back to the asyncio loop and serialize processing via lock.
        asyncio.run_coroutine_threadsafe(self._serialized_handle(path), self.loop)

    async def _serialized_handle(self, path: Path) -> None:
        async with self._processing_lock:
            await handle_new_pdf(path)


async def run() -> None:
    """Long-running task — start in app.py alongside aiogram + APScheduler.

    Returns when cancelled (via SIGTERM through the orchestrator's stop_event)."""
    inbox = Path(settings.finance_inbox_path)
    inbox.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    handler = _PdfEventHandler(loop)
    observer = Observer()
    observer.schedule(handler, str(inbox), recursive=False)
    observer.start()
    logger.info("folder_watcher started on %s", inbox)

    try:
        # Keep the task alive; the observer runs its own thread.
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("folder_watcher stopping")
    finally:
        observer.stop()
        observer.join(timeout=5)
        logger.info("folder_watcher stopped cleanly")
```

- [ ] **Step 8.4: Run tests, verify all pass**

```bash
pytest tests/test_folder_watcher.py -v
```
Expected: 5 passed.

- [ ] **Step 8.5: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 8.6: Commit**

```bash
git add skills/finance/ingestion/folder_watcher.py tests/test_folder_watcher.py
git commit -m "feat(ingestion): folder watcher with loose-token bank dispatch"
```

---

## Task 9: `bot/document_handler.py` — Telegram doc handler with auto-rename

**Files:**
- Create: `skills/finance/bot/document_handler.py`
- Test: `tests/test_document_handler.py`
- Modify: `skills/finance/bot/main.py` — register the handler

- [ ] **Step 9.1: Write failing tests**

```python
# tests/test_document_handler.py
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


def test_unambiguous_icici_filename_saves_with_canonical_prefix(tmp_path, monkeypatch):
    from skills.finance.bot.document_handler import handle_document

    inbox = tmp_path / "finance-inbox"
    inbox.mkdir()
    monkeypatch.setattr(
        "skills.finance.bot.document_handler.settings",
        MagicMock(finance_inbox_path=str(inbox), telegram_chat_id_rajat="42"),
    )

    fake_doc = MagicMock(file_name="Statement_April_2026_ICICI.pdf", file_id="fake_id")
    fake_message = MagicMock(chat=MagicMock(id=42), document=fake_doc)
    fake_message.answer = AsyncMock()

    fake_bot = MagicMock()
    fake_bot.download = AsyncMock()

    asyncio.run(handle_document(fake_message, bot=fake_bot))

    fake_bot.download.assert_called_once()
    save_path = Path(fake_bot.download.call_args.kwargs["destination"])
    assert save_path.parent == inbox
    assert save_path.name.startswith("icici_cc_")
    fake_message.answer.assert_called_once()
    assert "icici_cc_" in fake_message.answer.call_args.args[0].lower()


def test_ambiguous_filename_sends_inline_keyboard(tmp_path, monkeypatch):
    from skills.finance.bot.document_handler import handle_document

    inbox = tmp_path / "finance-inbox"
    inbox.mkdir()
    monkeypatch.setattr(
        "skills.finance.bot.document_handler.settings",
        MagicMock(finance_inbox_path=str(inbox), telegram_chat_id_rajat="42"),
    )

    fake_doc = MagicMock(file_name="Statement_April.pdf", file_id="fake_id")
    fake_message = MagicMock(chat=MagicMock(id=42), document=fake_doc)
    fake_message.answer = AsyncMock()

    fake_bot = MagicMock()
    fake_bot.download = AsyncMock()

    asyncio.run(handle_document(fake_message, bot=fake_bot))

    # Should NOT have downloaded yet (waits for user keyboard pick)
    fake_bot.download.assert_not_called()
    fake_message.answer.assert_called_once()
    # The reply should include a reply_markup (inline keyboard)
    call_kwargs = fake_message.answer.call_args.kwargs
    assert "reply_markup" in call_kwargs


def test_non_whitelisted_user_silently_ignored():
    from skills.finance.bot.document_handler import handle_document

    fake_doc = MagicMock(file_name="anything.pdf", file_id="fake_id")
    fake_message = MagicMock(chat=MagicMock(id=999), document=fake_doc)  # WRONG chat
    fake_message.answer = AsyncMock()

    fake_bot = MagicMock()
    fake_bot.download = AsyncMock()

    with patch("skills.finance.bot.document_handler.settings",
               MagicMock(finance_inbox_path="/tmp", telegram_chat_id_rajat="42")):
        asyncio.run(handle_document(fake_message, bot=fake_bot))

    fake_message.answer.assert_not_called()
    fake_bot.download.assert_not_called()
```

- [ ] **Step 9.2: Run tests, verify failures**

```bash
pytest tests/test_document_handler.py -v
```
Expected: 3 errors.

- [ ] **Step 9.3: Implement `bot/document_handler.py`**

```python
# skills/finance/bot/document_handler.py
"""Telegram document handler — receives PDF docs, saves to inbox with canonical
filename. Folder watcher then dispatches.

Auto-renaming reduces user friction: forward an unmodified Gmail attachment;
the bot detects bank from filename or asks via inline keyboard."""
from __future__ import annotations
import logging
import re
from pathlib import Path

from aiogram import Bot
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from skills.finance.ingestion._common import detect_bank_from_filename
from skills.finance.lib.settings import settings

logger = logging.getLogger(__name__)


def _is_rajat(message: Message) -> bool:
    """CLAUDE.md invariant #7: whitelist-only message handling."""
    return str(message.chat.id) == str(settings.telegram_chat_id_rajat)


def _sanitize_stem(name: str) -> str:
    """Strip extension; replace whitespace and odd chars with `_` for filesystem safety."""
    stem = Path(name).stem
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")


def _canonical_name(bank: str, original_filename: str) -> str:
    return f"{bank}_{_sanitize_stem(original_filename)}.pdf"


async def handle_document(message: Message, bot: Bot) -> None:
    """Aiogram Document message handler.

    1. Whitelist check.
    2. Token-match the original filename.
    3a. Unambiguous match → save to inbox with canonical prefix; reply confirmation.
    3b. Ambiguous / no match → send inline keyboard prompting [ICICI CC] / [AMEX CC] / [Cancel].
    """
    if not _is_rajat(message):
        return  # silent

    doc = message.document
    if doc is None:
        return

    bank = detect_bank_from_filename(doc.file_name or "")

    if bank is None:
        # Ask via inline keyboard. Encode the file_id in callback_data so the
        # callback handler can download once user picks.
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="ICICI CC", callback_data=f"pickbank:icici_cc:{doc.file_id}"),
            InlineKeyboardButton(text="AMEX CC", callback_data=f"pickbank:amex_cc:{doc.file_id}"),
            InlineKeyboardButton(text="Cancel", callback_data=f"pickbank:cancel:{doc.file_id}"),
        ]])
        await message.answer(
            text=f"Couldn't auto-detect bank from '{doc.file_name}'. Which is this?",
            reply_markup=kb,
        )
        return

    # Unambiguous match: save and let the watcher take over.
    inbox = Path(settings.finance_inbox_path)
    inbox.mkdir(parents=True, exist_ok=True)
    canonical = inbox / _canonical_name(bank, doc.file_name or "unnamed.pdf")
    await bot.download(doc, destination=str(canonical))
    logger.info("saved telegram doc as %s", canonical)
    await message.answer(f"Saved as `{canonical.name}` — processing.", parse_mode="Markdown")
```

- [ ] **Step 9.4: Wire handler into `bot/main.py`**

Read the current `bot/main.py` and append (or modify) so the document handler is registered:

```python
# Add to skills/finance/bot/main.py:
from aiogram import F
from aiogram.types import Document
from skills.finance.bot.document_handler import handle_document

@dp.message(F.document)
async def _document_handler(message: Message) -> None:
    await handle_document(message, bot=bot)
```

(Note: place the import + registration after the existing `dp` and `bot` definitions, alongside the `/ping` handler. Don't break the existing middleware registration.)

- [ ] **Step 9.5: Run all bot tests**

```bash
pytest tests/test_document_handler.py tests/test_bot_middleware.py -v
```
Expected: 4 passed (3 new + 1 existing middleware).

- [ ] **Step 9.6: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 9.7: Commit**

```bash
git add skills/finance/bot/document_handler.py skills/finance/bot/main.py tests/test_document_handler.py
git commit -m "feat(bot): Telegram document handler with auto-rename + inline keyboard fallback"
```

---

## Task 10: `/model list` command

**Files:**
- Modify: `skills/finance/bot/main.py` — register `/model` command
- Test: extend `tests/test_bot_middleware.py` (or new `test_bot_commands.py`)

- [ ] **Step 10.1: Write failing test**

```python
# Append to tests/test_bot_middleware.py OR new file tests/test_bot_commands.py

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def test_model_list_command_returns_yaml():
    from skills.finance.bot.main import model_list_handler
    fake_message = MagicMock(chat=MagicMock(id=42))
    fake_message.answer = AsyncMock()

    fake_yaml = "pdf_extraction:\n  model: gemini/gemini-2.5-flash\n"
    with patch("skills.finance.bot.main.settings",
               MagicMock(telegram_chat_id_rajat="42")), \
         patch("builtins.open", MagicMock(return_value=MagicMock(__enter__=lambda s: MagicMock(read=lambda: fake_yaml), __exit__=lambda *a: None))):
        asyncio.run(model_list_handler(fake_message))

    fake_message.answer.assert_called_once()
    args, kwargs = fake_message.answer.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "pdf_extraction" in text
    assert "gemini/gemini-2.5-flash" in text


def test_model_list_rejects_non_rajat():
    from skills.finance.bot.main import model_list_handler
    fake_message = MagicMock(chat=MagicMock(id=999))
    fake_message.answer = AsyncMock()
    with patch("skills.finance.bot.main.settings",
               MagicMock(telegram_chat_id_rajat="42")):
        asyncio.run(model_list_handler(fake_message))
    fake_message.answer.assert_not_called()
```

- [ ] **Step 10.2: Run tests, verify failures**

```bash
pytest tests/test_bot_middleware.py -v
```
Expected: tests for `model_list_handler` fail (NotFound).

- [ ] **Step 10.3: Implement `/model` handler in `bot/main.py`**

Append to `skills/finance/bot/main.py`:

```python
from pathlib import Path
from aiogram.filters import Command

ROUTING_YAML_PATH = Path("config/model_routing.yaml")


@dp.message(Command("model"))
async def model_list_handler(message: Message) -> None:
    """V1 minimal /model command — only `/model list` (read-only).
    Full /model family (switch, --confirm, A/B mode) deferred to Week 4."""
    if not _is_rajat(message):
        return

    # Parse subcommand. For Week 2, only 'list' is supported.
    # If the message is just '/model' with no arg, treat as 'list'.
    parts = (message.text or "").split(maxsplit=1)
    subcommand = parts[1].strip().lower() if len(parts) > 1 else "list"

    if subcommand != "list":
        await message.answer(
            f"`/model {subcommand}` is not yet supported. Only `/model list` is available in Week 2. "
            "Full command family lands in Week 4.",
            parse_mode="Markdown",
        )
        return

    with open(ROUTING_YAML_PATH) as f:
        yaml_text = f.read()
    await message.answer(f"```yaml\n{yaml_text}\n```", parse_mode="Markdown")
```

- [ ] **Step 10.4: Run tests, verify pass**

```bash
pytest tests/test_bot_middleware.py -v
```

- [ ] **Step 10.5: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 10.6: Commit**

```bash
git add skills/finance/bot/main.py tests/test_bot_middleware.py
git commit -m "feat(bot): /model list command (V1 read-only; full family deferred to Week 4)"
```

---

## Task 11: `app.py` orchestration update

Folder watcher must run as a sibling task alongside aiogram polling, APScheduler, and FastAPI. Graceful SIGTERM must also stop it.

**Files:**
- Modify: `app.py`
- Modify: `pyproject.toml` (add `watchdog`, `pdf2image`)

- [ ] **Step 11.1: Add new dependencies to `pyproject.toml`**

Edit `pyproject.toml` `[project] dependencies = [...]` to include:

```toml
"watchdog>=3",                # folder watcher for ingestion (Week 2)
"pdf2image>=1.17",            # AMEX page rasterization (requires `brew install poppler`)
```

Then install:

```bash
source .venv/bin/activate
pip install -e ".[dev]"
deactivate
```

- [ ] **Step 11.2: Wire `folder_watcher.run()` into `app.py`**

Read the current `app.py`. Modify `main()` so the folder watcher runs concurrently with the bot, scheduler, and HTTP server. Concretely, add to the `asyncio.gather(...)` line:

```python
# In skills/finance/ingestion/__init__.py — make sure run() is exported
from skills.finance.ingestion import folder_watcher

# In app.py main(), update the gather:
async def main() -> None:
    configure_logging()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    sched = _build_scheduler()
    sched.start()
    logger.info("scheduler started; jobs=%s", [j.id for j in sched.get_jobs()])

    # New: folder watcher task. Cancelled on stop_event.
    watcher_task = asyncio.create_task(folder_watcher.run())

    try:
        await asyncio.gather(
            _run_bot(stop_event),
            _run_http(stop_event),
            _wait_then_cancel(stop_event, watcher_task),
        )
    finally:
        logger.info("shutting down: cancelling scheduler")
        sched.shutdown(wait=False)
        logger.info("shutdown complete")


async def _wait_then_cancel(stop_event: asyncio.Event, task: asyncio.Task) -> None:
    """Cancel `task` when stop_event is set."""
    await stop_event.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
```

- [ ] **Step 11.3: Verify import smoke**

```bash
source .venv/bin/activate
python -c "import app; print('import OK')"
deactivate
```
Expected: `import OK` with no traceback.

- [ ] **Step 11.4: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```
Expected: clean across the board.

- [ ] **Step 11.5: Commit**

```bash
git add app.py pyproject.toml
git commit -m "feat(app): start folder_watcher as concurrent task with graceful shutdown"
```

- [ ] **Step 11.6: Restart launchd-supervised app**

```bash
launchctl kickstart -k gui/$(id -u)/com.rajat.pfa.app
sleep 5
launchctl print gui/$(id -u)/com.rajat.pfa.app | grep -E "state|last exit code"
```
Expected: `state = running`, no recent crash.

Verify the watcher started by tailing the log:

```bash
tail -20 ~/finance-logs/pfa.log | grep folder_watcher
```
Expected: a line like `folder_watcher started on /Users/rajat/finance-inbox`.

---

## Task 12: Backfill drill + acceptance verification

**No code changes.** This task drives the spec §17 acceptance criteria to green by running the real backfill end-to-end with your 6 real PDFs.

- [ ] **Step 12.1: Verify `tests/golden_fixtures/amex_sample.pdf` is in place**

```bash
ls -la tests/golden_fixtures/
```
Expected: both `icici_sample.pdf` and `amex_sample.pdf` present (gitignored).

If `amex_sample.pdf` is missing — drop one real recent AMEX statement there before continuing.

- [ ] **Step 12.2: Drop 6 PDFs into `~/finance-inbox/`**

You provide: 3 months × 2 cards = 6 password-protected PDFs.

```bash
# Example — adjust paths to your actual files
cp ~/Downloads/icici_cc_2026_03.pdf ~/finance-inbox/
cp ~/Downloads/icici_cc_2026_04.pdf ~/finance-inbox/
cp ~/Downloads/icici_cc_2026_05.pdf ~/finance-inbox/
cp ~/Downloads/amex_cc_2026_03.pdf ~/finance-inbox/
cp ~/Downloads/amex_cc_2026_04.pdf ~/finance-inbox/
cp ~/Downloads/amex_cc_2026_05.pdf ~/finance-inbox/
```

- [ ] **Step 12.3: Watch logs as they process**

```bash
tail -f ~/finance-logs/pfa.log
```

You should see, one by one for each PDF:
- `folder_watcher` log: `dispatch_to_parser` for the bank
- For ICICI: `pipeline ingested N rows` log
- For AMEX (first 2-3): possible calibration disagreements logged
- Telegram message via main bot: "📥 ICICI CC March 2026 ingested..." etc.

If any PDF fails (totals mismatch, parser hard fail), check `ingestion_log` via Supabase SQL editor:
```sql
SELECT * FROM ingestion_log ORDER BY timestamp DESC LIMIT 10;
```

- [ ] **Step 12.4: Verify the backfill landed**

In Supabase SQL editor (as service role):

```sql
SELECT
  a.nickname,
  count(*) AS row_count,
  min(t.date) AS earliest,
  max(t.date) AS latest,
  sum(CASE WHEN t.direction='out' THEN t.amount ELSE 0 END) AS total_out,
  sum(CASE WHEN t.direction='in'  THEN t.amount ELSE 0 END) AS total_in
FROM transactions t
JOIN accounts a ON a.id = t.account_id
WHERE a.type='credit_card'
GROUP BY a.nickname
ORDER BY a.nickname;
```

Expected: two rows (ICICI CC, AMEX CC), each with rows spanning ~3 months, totals matching what your statements declared.

- [ ] **Step 12.5: Verify acceptance criteria from spec §17**

Walk through each:
- [ ] 6 backfill PDFs ingested into `transactions`
- [ ] `make lint && make typecheck && make test` clean — run again now to confirm
- [ ] Validator catches a manually-injected wrong amount — covered by `test_statement_validator.py::test_validator_over_tolerance_rejects`
- [ ] AMEX calibration adjudication path exercised — covered by `test_calibration.py::test_diff_merchant_differs_one_disagreement` + real disagreement log if any
- [ ] Folder watcher running under launchd — verified in 11.6
- [ ] Telegram doc handler auto-renames non-canonical filename — try forwarding a non-canonically-named PDF to your bot and verify
- [ ] Each parser exports `__parser_version__` — covered by `test_icici_cc_parser.py::test_parser_version_string`, `test_amex_cc_parser.py::test_amex_parser_version`
- [ ] `lib/llm.py` re-adds `images` parameter — covered by `test_llm.py::test_llm_passes_images_to_litellm`
- [ ] `request_logs` shows AMEX calibration calls totaling ≤ ₹200 — verify with:
  ```sql
  SELECT count(*) AS calls, sum(total_cost) AS total_usd
  FROM request_logs
  WHERE metadata->>'task' = 'pdf_extraction'
    AND created_at >= '2026-04-26';
  ```
  Multiply USD by ~84 — should be ≤ ₹200.
- [ ] No PII committed to git: `git log --all --source -- 'tests/golden_fixtures/*.pdf' '.env' 'credentials.yaml' 'migrations/*.local.sql'` returns nothing
- [ ] `tasks/week-2-todo.md` exists; `tasks/todo.md` preserved as Week 1 historical record — `ls tasks/`

- [ ] **Step 12.6: Tag week-2-ingestion**

```bash
git -c user.name="Rajat Sharma" -c user.email="sharma.rajat70@gmail.com" \
    tag -a week-2-ingestion -m "Week 2 — Ingestion (ICICI CC + AMEX CC) complete

3 months of historical CC transactions ingested.
Statement-total validator: live, ±₹1 tolerance.
AMEX dual-model calibration: ran on first 2-3 statements, ₹X total cost.
Folder watcher + Telegram doc handler: both source paths exercised.
/model list: shipped (full family deferred to Week 4)."

# (Optional) Push when gh authenticated:
# git push origin main week-2-ingestion
```

- [ ] **Step 12.7: Update `tasks/lessons.md` with any patterns from Week 2**

If anything surprised you during implementation (parser regex calibration pain, supabase-py quirk, AMEX format quirk, etc.), append a lesson entry. Keep it specific and actionable for future weeks.

---

## Self-Review

**Spec coverage:**

| Spec section | Implemented in |
|---|---|
| §1 Goal | Tasks 4, 5, 6, 7, 8, 9, 11, 12 |
| §2 Scope | Tasks 1-12 (in scope); deferred items confirmed in plan File structure section |
| §3 Architecture (4 layers) | Task 1 (common types), 2 (validate), 4-5 (parse), 6 (persist), 8 (source: watcher), 9 (source: Telegram) |
| §4 File structure | Mirrored in plan's File structure |
| §5.1 Folder watcher | Task 8 |
| §5.2 Telegram doc handler | Task 9 |
| §6.1 Common contract | Task 1 |
| §6.2 ICICI parser (deterministic) | Task 4 |
| §6.3 AMEX parser (LLM) | Task 5 |
| §7 Validator | Task 2 |
| §8 Persist (pipeline) | Task 6 |
| §9 Telegram review flow | Task 7 (calibration message), Task 6 (success/fail summary via pipeline + bot — via `_log_*` returning to caller in Task 8 + Task 9) — see note below |
| §10 /model list | Task 10 |
| §11 Backfill mechanics | Task 12 |
| §12 Error handling matrix | Task 8 + Task 9 (rejection paths), Task 6 (status logging) |
| §13 Testing strategy | All tasks include unit tests |
| §14 New dependencies | Task 11.1 |
| §15 Code changes outside ingestion/ | Task 3 (llm.py), Task 9.4 (bot/main.py), Task 11.2 (app.py) |
| §16 Open implementation risks | Task 0.2 verifies upsert; Task 4.4 calibrates regex; Task 11.6 verifies launchd |
| §17 Acceptance criteria | Task 12.5 walks each |

**Gap noted:** Spec §9.1 mentions a Telegram summary message after each successful ingest (e.g., "📥 ICICI CC May 2026 ingested..."). The current pipeline.py (Task 6) only writes to `ingestion_log` and returns the row; it doesn't send a Telegram message. The Telegram summary message-per-statement is implicitly emitted by `dispatch_to_parser` in `folder_watcher.py` (Task 8) calling `pipeline.ingest`, then formatting the result and calling `bot.send_message`. **Add this as a follow-up to Task 8** — `dispatch_to_parser` should send a summary message to the main bot after `ingest()` returns. Updated note inline below.

**Placeholder scan:** Searched for "TBD", "TODO", "implement later", vague handwaves. None found in step content. The two `NotImplementedError` markers in Step 7.3 are placeholders that Step 7.4 explicitly fills in (TDD-style), not plan failures.

**Type consistency:**
- `ParsedRow`, `ParseResult`, `SourceMeta`, `ValidationResult` defined in Task 1; consistently imported by Tasks 2, 4, 5, 6, 7, 8.
- `Bank` literal type (`"icici_cc" | "amex_cc"`) defined in Task 1; used in Task 1 helpers, Task 8, Task 9.
- `__parser_version__` strings: `"icici-cc/v1"` (Task 4) and `"amex-cc-llm/v1"` (Task 5); referenced consistently in pipeline (Task 6) and tests (4.1, 5.1).
- `RAJAT_USER_ID` defined as a hardcoded UUID in Task 1; used in pipeline (Task 6).
- `ACCOUNT_IDS` mapping in Task 8 references the seeded account UUIDs from `migrations/003_seed.local.sql`.

## Inline addendum to Task 8 (catching the Spec §9.1 gap)

Add to Task 8's `dispatch_to_parser` after the `ingest(...)` call:

```python
# At the end of dispatch_to_parser, after ingest completes:
log_entry = await ingest(parse_result, account_id, source_meta)

# Send Telegram summary via main bot
from skills.finance.bot.main import bot as main_bot
status = log_entry["status"]
if status == "success":
    summary = (
        f"📥 {bank.upper()} ingested\n"
        f"{log_entry['rows_added']} rows, "
        f"₹{log_entry['extracted_total']} (declared ₹{log_entry['declared_total']}) "
        f"— totals match ✓"
    )
    await main_bot.send_message(chat_id=settings.telegram_chat_id_rajat, text=summary)
elif status == "skipped_duplicate":
    summary = f"📥 {bank.upper()} {pdf_path.name}: already ingested previously (skipped)."
    await main_bot.send_message(chat_id=settings.telegram_chat_id_rajat, text=summary)
# 'total_check_failed' alerts go via send_alert from inside pipeline; no double-message here.
```

Update Task 8.3 implementation to include this summary-send step before committing.

---

## Execution Handoff

**Plan complete and saved to `tasks/week-2-todo.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Same workflow that landed Week 1 cleanly.

**2. Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints for review.

**Which approach?**
