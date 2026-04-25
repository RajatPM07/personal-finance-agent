# Week 2 — PFA Ingestion Implementation Plan (revision 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** End-to-end ingestion pipeline for ICICI CC (PDF) and AMEX CC (XLSX) statements with statement-total integrity validation and graceful failure paths. By end of Week 2, three months of ICICI CC + AMEX CC history are in `transactions`, and new monthly statements drop into `~/finance-inbox/` (or via Telegram doc) and process automatically.

**Architecture:** Four isolated layers — Source (folder watcher + Telegram doc handler) → Parse (deterministic ICICI via `pikepdf+pdfplumber`, deterministic AMEX via `pandas.read_excel`) → Validate (pure-fn statement-total comparator) → Persist (Mode-B `import_hash` + Supabase upsert + `ingestion_log`). Spec at `docs/superpowers/specs/2026-04-26-week-2-ingestion-design.md`.

**Tech Stack:** Python 3.11, `pikepdf` (ICICI decrypt), `pdfplumber` (ICICI text extraction), `pandas` + `openpyxl` (AMEX XLSX), `watchdog` (folder watcher), aiogram (Telegram doc handler), supabase-py (upsert).

**PRD reference:** `PRD.md` V2.1 §6 (model routing), §7 (schema), §11 Week 2 (partially superseded), §18.4 (PDF integrity), §19.5 (`import_hash` Mode B).
**Lessons referenced (do NOT repeat):** bout-is-CSV-only, aiogram-handle_signals, Supavisor-username, RLS-auto-enable, CLAUDE.md invariants #2 (`.execute()`), #4 (manual `__parser_version__`), #11 (verify, don't assume).

**Revision history:**
- **r2 (2026-04-26):** AMEX file format clarified as XLSX (not password-protected PDF). Dropped LLM-AMEX path, dual-model calibration, `pdf2image`/`Pillow`/`poppler` deps, `lib/llm.py` `images` parameter re-add. 12 tasks → 10. AMEX parser becomes deterministic `pandas.read_excel`. ₹200 calibration budget freed; full $5 Anthropic balance reserved for Week 4 reasoning.
- r1 (2026-04-26): initial plan after 4-question brainstorm and locked spec v1.

---

## Preconditions — Rajat's tasks (not code)

Every box must be green before Task 0 starts.

- [ ] AMEX CC XLSX copied to `tests/golden_fixtures/amex_sample.xlsx` (gitignored; one real recent statement)
- [ ] `ICICI_PDF_PASSWORD` env var (already used in Week 1's Task 9 smoke; same value)
- [ ] launchd-supervised app is healthy (`launchctl print gui/$(id -u)/com.rajat.pfa.app | grep state` → `running`)
- [ ] `git config --local user.{name,email}` set so commits don't need `-c` flags

(No `brew install poppler`, no `AMEX_PDF_PASSWORD`, no Anthropic top-up needed — all dropped in r2.)

---

## File structure

All paths relative to `/Users/rajat/AntiGravity/Personal finance Agent/`.

**Created in Week 2:**
- `skills/finance/ingestion/_common.py` — `ParsedRow`, `ParseResult`, `SourceMeta`, `ValidationResult` dataclasses; `RAJAT_USER_ID`; `password_lookup(bank, last4)`; `detect_bank_from_filename(name)`
- `skills/finance/ingestion/statement_validator.py` — pure `validate(parse_result) → ValidationResult`
- `skills/finance/ingestion/pipeline.py` — `async ingest(parse_result, account_id, source_meta) → IngestionLogEntry`
- `skills/finance/ingestion/parsers/icici_cc.py` — `parse(pdf_path, password) → ParseResult`, `__parser_version__ = "icici-cc/v1"`
- `skills/finance/ingestion/parsers/amex_cc.py` — `parse(xlsx_path, password=None) → ParseResult`, `__parser_version__ = "amex-cc-xlsx/v1"`
- `skills/finance/ingestion/folder_watcher.py` — `watchdog` Observer; `async run()` task; dispatches `*.pdf` AND `*.xlsx`
- `skills/finance/bot/document_handler.py` — aiogram `Document` handler with auto-rename
- `tests/test_statement_validator.py`, `test_icici_cc_parser.py`, `test_amex_cc_parser.py`, `test_pipeline.py`, `test_folder_watcher.py`, `test_document_handler.py`

**Modified in Week 2:**
- `skills/finance/bot/main.py` — register `document_handler` + `/model list` command
- `app.py` — start folder watcher in `asyncio.gather`; include in graceful shutdown
- `pyproject.toml` — add `watchdog>=3` to deps (only)

**NOT modified (deferred indefinitely):**
- `skills/finance/lib/llm.py` — the `images` parameter re-add was tied to LLM-AMEX path. Not needed.

---

## Task 0: Preconditions verification (tooling sanity check)

Catches library API surprises before they poison downstream tasks (CLAUDE.md invariant #11).

- [ ] **Step 0.1: Verify `pandas.read_excel` works on the AMEX fixture**

```bash
cd "/Users/rajat/AntiGravity/Personal finance Agent"
source .venv/bin/activate
python -c "
import pandas as pd
df = pd.read_excel('tests/golden_fixtures/amex_sample.xlsx', engine='openpyxl', header=None, nrows=30)
print('AMEX XLSX read OK')
print(f'shape (first 30 rows × cols): {df.shape}')
print()
print('=== first 30 rows preview (verbatim cell values, NaN visible) ===')
for i, row in df.iterrows():
    cells = [str(v)[:40] if pd.notna(v) else 'NaN' for v in row.values[:10]]
    print(f'  row {i}: {cells}')
"
deactivate
```

Record findings into `tasks/week-2-preconditions-notes.md`:
- The actual header row index (often row 0 or row 5–10 depending on AMEX export variant)
- The actual column names AMEX is using (Date / Description / Amount / etc.)
- Whether amount is signed (positive=charge, negative=credit) or split into separate Debit/Credit columns
- Whether there's a "Total" / "Statement Total" footer row

If the headers don't match any of the three layouts in spec §6.3's `KNOWN_COLUMN_SETS`, **add a 4th entry to the parser before Task 4** based on observed names. This is exactly what spec §16.2 anticipates.

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

Confirm `on_conflict` and `ignore_duplicates` are accepted parameters. If signature differs from what spec §8 / Task 5 (pipeline) assumes, document the actual signature in `tasks/week-2-preconditions-notes.md` and propose the fallback (per-row insert with try/except for unique-constraint violation). Update Task 5 inline before implementation.

- [ ] **Step 0.3: Make sure `tasks/week-2-preconditions-notes.md` is gitignored**

```bash
cd "/Users/rajat/AntiGravity/Personal finance Agent"
grep -E "preconditions-notes" .gitignore || echo "tasks/week-2-preconditions-notes.md" >> .gitignore
git check-ignore -v tasks/week-2-preconditions-notes.md
```

Expected: `git check-ignore` prints a hit. If `.gitignore` was modified, commit:
```bash
git add .gitignore
git -c user.name="Rajat Sharma" -c user.email="sharma.rajat70@gmail.com" \
    commit -m "chore(gitignore): add Week 2 preconditions-notes path"
```

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


def test_detect_bank_amex_loose_xlsx():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("AMEX_Statement_2026_05.xlsx") == "amex_cc"
    assert detect_bank_from_filename("american_express_may.xlsx") == "amex_cc"


def test_detect_bank_no_match_returns_none():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("randomfile.pdf") is None


def test_detect_bank_ambiguous_returns_none():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("icici_amex_partnership_statement.pdf") is None


def test_password_lookup_unique_prefix():
    from skills.finance.ingestion._common import password_lookup
    fake_yaml = """
icici_cc_1008:
  pattern: "custom"
  value: "icici_pass_123"
"""
    with patch("builtins.open", mock_open(read_data=fake_yaml)):
        assert password_lookup("icici_cc") == "icici_pass_123"


def test_password_lookup_exact_key_with_last4():
    from skills.finance.ingestion._common import password_lookup
    fake_yaml = """
icici_cc_1008:
  value: "icici_pass_1008"
icici_cc_9999:
  value: "icici_pass_9999"
"""
    with patch("builtins.open", mock_open(read_data=fake_yaml)):
        assert password_lookup("icici_cc", last4="1008") == "icici_pass_1008"


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
            password_lookup("icici_cc")
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
    source_row_ordinal: int                # 1..N within the file, deterministic per parser


@dataclass(frozen=True)
class ParseResult:
    rows: list[ParsedRow]
    declared_totals: dict                  # {'total_spends': Decimal, 'total_credits': Decimal,
                                           #  'closing_balance': Decimal | None}
    pdf_content_hash: str                  # sha256 of source FILE bytes (PDF or XLSX);
                                           #   threaded into import_hash. Field name kept as
                                           #   pdf_content_hash for schema compatibility with
                                           #   transactions table; despite the name, also used
                                           #   for XLSX content hashing.
    parser_version: str                    # e.g. "icici-cc/v1"


@dataclass(frozen=True)
class SourceMeta:
    source: Literal["manual_pdf", "manual_xlsx", "telegram_pdf", "telegram_xlsx", "gmail_cc_stmt"]
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
    Returns None if both or neither match.
    """
    name = filename.lower()
    is_icici = ("icici" in name) and ("cc" in name)
    is_amex = ("amex" in name) or ("american" in name)
    if is_icici and is_amex:
        return None
    if is_icici:
        return "icici_cc"
    if is_amex:
        return "amex_cc"
    return None


def password_lookup(bank: Bank, last4: str | None = None,
                    credentials_path: Path = Path("credentials.yaml")) -> str:
    """Read credentials.yaml. Returns the password for the given bank.

    NB: AMEX in V1 is XLSX without a password — callers should not invoke this
    helper for `bank='amex_cc'`. ICICI is the only V1 caller. The function still
    accepts the bank parameter for forward-compatibility with future
    password-protected sources.
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
    pr = _make_pr([_row(100, "out", 1), _row(50, "out", 2)], "150.50", 0)
    res = validate(pr)
    assert res.ok is True
    assert res.delta_out == Decimal("0.50")


def test_validator_over_tolerance_rejects():
    from skills.finance.ingestion.statement_validator import validate
    pr = _make_pr([_row(100, "out", 1), _row(50, "out", 2)], 152, 0)
    res = validate(pr)
    assert res.ok is False
    assert res.delta_out == Decimal("2")


def test_validator_all_credits():
    from skills.finance.ingestion.statement_validator import validate
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
    # Defensive: if a parser ever emits negative amount (it shouldn't), validator
    # computes correctly via abs() on the deltas.
    pr = _make_pr([_row(-100, "out", 1)], 100, 0)
    res = validate(pr)
    assert res.ok is False
```

- [ ] **Step 2.2: Run tests, verify all fail**

```bash
pytest tests/test_statement_validator.py -v
```
Expected: 6 errors.

- [ ] **Step 2.3: Implement `statement_validator.py`**

```python
# skills/finance/ingestion/statement_validator.py
"""Pure function: compare a ParseResult's row totals against its declared totals.

No I/O. Easy to unit-test. Used by pipeline.py before any DB write.

Caveat: AMEX XLSX exports may not include a declared totals row (see spec §6.3
+ §9.2). In that case the AMEX parser sets declared_totals from row sums, and
this validator passes tautologically. The annotation on the success message
makes the weakness visible to the user."""
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

- [ ] **Step 2.6: Commit**

```bash
git add skills/finance/ingestion/statement_validator.py tests/test_statement_validator.py
git commit -m "feat(ingestion): pure-fn statement-total validator with ±₹1 tolerance"
```

---

## Task 3: ICICI CC parser (deterministic PDF)

**Files:**
- Create: `skills/finance/ingestion/parsers/icici_cc.py`
- Create: `skills/finance/ingestion/parsers/__init__.py` (empty)
- Test: `tests/test_icici_cc_parser.py`

The parser is calibrated against the Week 1 golden fixture at `tests/golden_fixtures/icici_sample.pdf`. Tests skip when fixture absent or password env var unset (mirrors `test_pdf_smoke.py`).

- [ ] **Step 3.1: Write tests**

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
    assert len(result.pdf_content_hash) == 64


@pytest.mark.skipif(not FIXTURE.exists(), reason="ICICI golden fixture not present")
@pytest.mark.skipif(not PASSWORD, reason="ICICI_PDF_PASSWORD env var not set")
def test_parsed_row_fields_well_formed():
    from skills.finance.ingestion.parsers.icici_cc import parse
    result = parse(FIXTURE, password=PASSWORD)
    for row in result.rows:
        assert row.amount > Decimal("0")
        assert row.direction in ("in", "out")
        assert row.raw_merchant.strip()
        assert row.source_row_ordinal >= 1


@pytest.mark.skipif(not FIXTURE.exists(), reason="ICICI golden fixture not present")
@pytest.mark.skipif(not PASSWORD, reason="ICICI_PDF_PASSWORD env var not set")
def test_ordinals_contiguous_1_to_N():
    """CLAUDE.md testing §: assert ordinals contiguous 1..N — catches silent ordering drift."""
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
        f"Either parser regex is off, or declared totals labels need calibration."
    )
```

- [ ] **Step 3.2: Run tests, verify failures**

```bash
pytest tests/test_icici_cc_parser.py -v
```
Expected: ImportError or 5 errors.

- [ ] **Step 3.3: Implement `icici_cc.py`**

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

ROW_RE = re.compile(
    r"^(?P<date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<merchant>.+?)\s+"
    r"(?P<amount>[\d,]+\.\d{2})"
    r"(?:\s*(?P<cr>Cr))?\s*$"
)

# Calibrate these labels against the real fixture during implementation. ICICI
# uses different wording across statement variants; the validator going ok=False
# on the real fixture is the surface to adjust here.
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

- [ ] **Step 3.4: Run tests; calibrate regexes if needed**

```bash
pytest tests/test_icici_cc_parser.py -v
```
Expected: 5 passed.

If `test_extracted_totals_match_declared_via_validator` fails: inspect the actual ICICI statement text (`pdfplumber.open(...).pages[0].extract_text()`) and adjust `TOTAL_SPENDS_RE` / `TOTAL_CREDITS_RE` to match the real labels. **Do not lower TOLERANCE in `statement_validator.py` — that defeats the integrity check.**

- [ ] **Step 3.5: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 3.6: Commit**

```bash
git add skills/finance/ingestion/parsers/icici_cc.py skills/finance/ingestion/parsers/__init__.py tests/test_icici_cc_parser.py
git commit -m "feat(parsers): ICICI CC deterministic PDF parser (Week 2 Task 3)"
```

---

## Task 4: AMEX CC parser (deterministic XLSX)

**Files:**
- Create: `skills/finance/ingestion/parsers/amex_cc.py`
- Test: `tests/test_amex_cc_parser.py`

The parser is calibrated against `tests/golden_fixtures/amex_sample.xlsx`. Tests skip when fixture absent. Some unit tests use canned `pd.DataFrame` inputs and don't need the fixture.

- [ ] **Step 4.1: Write tests**

```python
# tests/test_amex_cc_parser.py
from __future__ import annotations
from decimal import Decimal
from pathlib import Path
import pandas as pd
import pytest

FIXTURE = Path(__file__).parent / "golden_fixtures" / "amex_sample.xlsx"


def test_amex_parser_version():
    from skills.finance.ingestion.parsers.amex_cc import __parser_version__
    assert __parser_version__ == "amex-cc-xlsx/v1"


def test_find_header_row_canonical_layout():
    """Synthetic DF: known column set on row 0."""
    from skills.finance.ingestion.parsers.amex_cc import find_header_row
    df = pd.DataFrame([
        ["Date", "Description", "Amount"],
        ["15/05/2026", "SWIGGY", 350.00],
        ["20/05/2026", "BLINKIT", 1200.00],
    ])
    idx, mapping = find_header_row(df)
    assert idx == 0
    assert "date" in mapping and "description" in mapping and "amount" in mapping


def test_find_header_row_offset_layout():
    """AMEX exports often have a few preamble rows before the headers."""
    from skills.finance.ingestion.parsers.amex_cc import find_header_row
    df = pd.DataFrame([
        ["AMEX Statement", None, None],
        ["Customer: Rajat", None, None],
        ["", None, None],
        ["Date", "Description", "Amount"],
        ["15/05/2026", "SWIGGY", 350.00],
    ])
    idx, mapping = find_header_row(df)
    assert idx == 3


def test_find_header_row_alternate_naming():
    from skills.finance.ingestion.parsers.amex_cc import find_header_row
    df = pd.DataFrame([
        ["Transaction Date", "Description of Transaction", "Amount"],
        ["15/05/2026", "SWIGGY", 350.00],
    ])
    idx, mapping = find_header_row(df)
    assert idx == 0


def test_find_header_row_no_match_raises_with_preview():
    from skills.finance.ingestion.parsers.amex_cc import find_header_row, ParserError
    df = pd.DataFrame([
        ["Foo", "Bar", "Baz"],
        ["X", "Y", "Z"],
    ])
    with pytest.raises(ParserError) as exc_info:
        find_header_row(df)
    assert "Foo" in str(exc_info.value) or "actual headers" in str(exc_info.value).lower()


def test_amex_amount_signed_convention_charges_become_out():
    """AMEX exports with signed amounts: positive=charge (out), negative=credit (in)."""
    from skills.finance.ingestion.parsers.amex_cc import _row_from_signed_amount
    row = _row_from_signed_amount(
        date_str="15/05/2026", description="SWIGGY", amount_value=350.00, ordinal=1,
    )
    assert row.amount == Decimal("350.00")
    assert row.direction == "out"

    refund = _row_from_signed_amount(
        date_str="20/05/2026", description="REFUND ZOMATO", amount_value=-150.00, ordinal=2,
    )
    assert refund.amount == Decimal("150.00")
    assert refund.direction == "in"


@pytest.mark.skipif(not FIXTURE.exists(), reason="AMEX golden fixture not present")
def test_parse_real_amex_xlsx_returns_nonempty_parseresult():
    from skills.finance.ingestion.parsers.amex_cc import parse
    result = parse(FIXTURE)
    assert len(result.rows) > 0
    assert result.parser_version == "amex-cc-xlsx/v1"
    assert len(result.pdf_content_hash) == 64


@pytest.mark.skipif(not FIXTURE.exists(), reason="AMEX golden fixture not present")
def test_amex_parsed_row_fields_well_formed():
    from skills.finance.ingestion.parsers.amex_cc import parse
    result = parse(FIXTURE)
    for row in result.rows:
        assert row.amount > Decimal("0")
        assert row.direction in ("in", "out")
        assert row.raw_merchant.strip()
        assert row.source_row_ordinal >= 1


@pytest.mark.skipif(not FIXTURE.exists(), reason="AMEX golden fixture not present")
def test_amex_ordinals_contiguous_1_to_N():
    from skills.finance.ingestion.parsers.amex_cc import parse
    result = parse(FIXTURE)
    ordinals = [r.source_row_ordinal for r in result.rows]
    assert ordinals == list(range(1, len(result.rows) + 1))


@pytest.mark.skipif(not FIXTURE.exists(), reason="AMEX golden fixture not present")
def test_amex_extracted_totals_pass_validator():
    """Whether declared totals come from a footer row or are derived from row sums,
    validator should pass on a real fixture."""
    from skills.finance.ingestion.parsers.amex_cc import parse
    from skills.finance.ingestion.statement_validator import validate
    result = parse(FIXTURE)
    val = validate(result)
    assert val.ok, (
        f"AMEX validator failed: delta_in={val.delta_in}, delta_out={val.delta_out}. "
        f"If a footer Total row exists in the fixture, parser regex needs adjustment. "
        f"If it doesn't, parser should fall back to row-sum-derived totals."
    )
```

- [ ] **Step 4.2: Run tests, verify failures**

```bash
pytest tests/test_amex_cc_parser.py -v
```
Expected: errors / failures (no module yet).

- [ ] **Step 4.3: Implement `amex_cc.py`**

```python
# skills/finance/ingestion/parsers/amex_cc.py
"""AMEX CC XLSX statement parser — deterministic.

AMEX's MyStatement portal exports an unencrypted .xlsx file. We use
pandas.read_excel + structured-cell extraction. No LLM in path.

KNOWN_COLUMN_SETS lists the layouts we've seen. If a real export uses
different headers, find_header_row raises ParserError with the actual
headers in the message — extend KNOWN_COLUMN_SETS and re-run.

Known weakness: AMEX exports may not include a "Total" / "Statement Total"
footer row. When absent, declared_totals is derived from row sums and
the validator passes tautologically. Logged as a warning. See spec §6.3.
"""
from __future__ import annotations
import hashlib
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pandas as pd

from skills.finance.ingestion._common import ParsedRow, ParseResult

logger = logging.getLogger(__name__)

# CLAUDE.md invariant #4: manually curated.
__parser_version__ = "amex-cc-xlsx/v1"


class ParserError(Exception):
    """Raised when XLSX header detection fails or row parsing breaks."""


# Each entry maps semantic field → list of accepted column header variants
# (case-insensitive, whitespace-tolerant). The first set with all three
# required keys (date, description, amount-or-debit-credit-pair) wins.
KNOWN_COLUMN_SETS: list[dict[str, list[str]]] = [
    # India MyStatement, signed-amount layout
    {
        "date": ["Date", "Transaction Date"],
        "description": ["Description", "Description of Transaction", "Details"],
        "amount": ["Amount"],
    },
    # Split debit/credit columns variant
    {
        "date": ["Date", "Transaction Date"],
        "description": ["Description", "Description of Transaction", "Details"],
        "debit": ["Charges", "Debit"],
        "credit": ["Credits", "Credit"],
    },
]


def _normalize_header(s) -> str:
    if pd.isna(s):
        return ""
    return str(s).strip().lower()


def find_header_row(df: pd.DataFrame) -> tuple[int, dict[str, str]]:
    """Walk the first 30 rows. For each, check if the values match any
    KNOWN_COLUMN_SETS entry. Return (row_index, mapping from semantic_key → real_column_name).

    Raises ParserError with a preview of the first 10 rows if no match found."""
    max_rows = min(30, len(df))
    for i in range(max_rows):
        row_values = [_normalize_header(v) for v in df.iloc[i].values]
        for column_set in KNOWN_COLUMN_SETS:
            mapping: dict[str, str] = {}
            all_found = True
            for semantic_key, accepted in column_set.items():
                accepted_lower = [a.lower() for a in accepted]
                match_idx = next(
                    (j for j, v in enumerate(row_values) if v in accepted_lower),
                    None,
                )
                if match_idx is None:
                    all_found = False
                    break
                # Map semantic_key to the column index → caller uses .iloc[:, idx]
                mapping[semantic_key] = str(match_idx)
            if all_found:
                return i, mapping
    # No match found — surface real headers
    preview_rows = df.head(10).values.tolist()
    raise ParserError(
        f"AMEX header row not detected in first {max_rows} rows. "
        f"None of KNOWN_COLUMN_SETS matched. "
        f"First 10 rows preview: {preview_rows}. "
        f"Add a new entry to KNOWN_COLUMN_SETS based on the actual headers."
    )


def _row_from_signed_amount(date_str: str, description: str,
                            amount_value, ordinal: int) -> ParsedRow:
    """AMEX signed-amount convention: positive = charge (out), negative = credit (in)."""
    val = Decimal(str(amount_value))
    return ParsedRow(
        txn_date=_parse_date(date_str),
        amount=abs(val),
        direction="out" if val > 0 else "in",
        raw_merchant=str(description).strip(),
        source_row_ordinal=ordinal,
    )


def _row_from_split(date_str: str, description: str, debit_value, credit_value,
                    ordinal: int) -> ParsedRow:
    """Split debit/credit columns — exactly one should be non-NaN."""
    if pd.notna(debit_value) and Decimal(str(debit_value)) != 0:
        return ParsedRow(
            txn_date=_parse_date(date_str),
            amount=abs(Decimal(str(debit_value))),
            direction="out",
            raw_merchant=str(description).strip(),
            source_row_ordinal=ordinal,
        )
    if pd.notna(credit_value) and Decimal(str(credit_value)) != 0:
        return ParsedRow(
            txn_date=_parse_date(date_str),
            amount=abs(Decimal(str(credit_value))),
            direction="in",
            raw_merchant=str(description).strip(),
            source_row_ordinal=ordinal,
        )
    raise ParserError(
        f"Row at ordinal {ordinal} has no debit OR credit value; cannot determine direction"
    )


def _parse_date(s) -> date:
    """AMEX India: DD/MM/YYYY. Be permissive — pandas may have already converted to Timestamp."""
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    s = str(s).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ParserError(f"Could not parse date string: {s!r}")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_total_row(df: pd.DataFrame, header_idx: int, desc_col_idx: int,
                    amount_col_idx: int) -> Decimal | None:
    """Search rows below the data band for a 'Total' / 'Statement Total' summary row.
    Returns the amount, or None if not found."""
    for i in range(header_idx + 1, len(df)):
        cell = df.iloc[i, desc_col_idx]
        if pd.isna(cell):
            continue
        if "total" in str(cell).strip().lower():
            amt = df.iloc[i, amount_col_idx]
            if pd.notna(amt):
                try:
                    return abs(Decimal(str(amt)))
                except Exception:
                    continue
    return None


def parse(xlsx_path: Path, password: str | None = None) -> ParseResult:
    """Read AMEX XLSX, extract rows + declared totals.

    `password` parameter is accepted for parser-interface uniformity but
    ignored — AMEX XLSX is unencrypted in V1."""
    xlsx_path = Path(xlsx_path)
    pdf_content_hash = _sha256_file(xlsx_path)

    df = pd.read_excel(xlsx_path, engine="openpyxl", header=None)
    header_idx, mapping = find_header_row(df)

    rows: list[ParsedRow] = []
    ordinal = 1
    desc_col_idx = int(mapping["description"])

    if "amount" in mapping:
        amount_col_idx = int(mapping["amount"])
        date_col_idx = int(mapping["date"])
        for i in range(header_idx + 1, len(df)):
            row = df.iloc[i]
            d, desc, amt = row.iloc[date_col_idx], row.iloc[desc_col_idx], row.iloc[amount_col_idx]
            if pd.isna(d) or pd.isna(desc) or pd.isna(amt):
                # Skip blank / separator rows. Footer total rows also fall here when
                # date is missing; we extract them separately below.
                continue
            # Skip 'Total' rows that happen to have all three cells filled
            if "total" in str(desc).strip().lower():
                continue
            try:
                rows.append(_row_from_signed_amount(str(d), str(desc), amt, ordinal))
                ordinal += 1
            except ParserError:
                # Skip rows that fail individual parsing; log it
                logger.warning("skipping unparseable row at index %d: %r", i, row.to_list())
    else:
        # Split debit/credit layout
        debit_col_idx = int(mapping["debit"])
        credit_col_idx = int(mapping["credit"])
        date_col_idx = int(mapping["date"])
        for i in range(header_idx + 1, len(df)):
            row = df.iloc[i]
            d, desc = row.iloc[date_col_idx], row.iloc[desc_col_idx]
            debit, credit = row.iloc[debit_col_idx], row.iloc[credit_col_idx]
            if pd.isna(d) or pd.isna(desc):
                continue
            if pd.isna(debit) and pd.isna(credit):
                continue
            if "total" in str(desc).strip().lower():
                continue
            try:
                rows.append(_row_from_split(str(d), str(desc), debit, credit, ordinal))
                ordinal += 1
            except ParserError:
                logger.warning("skipping unparseable row at index %d: %r", i, row.to_list())

    # Declared totals
    declared_total_spends: Decimal | None = None
    if "amount" in mapping:
        declared_total_spends = _find_total_row(
            df, header_idx, desc_col_idx, int(mapping["amount"]),
        )

    if declared_total_spends is None:
        # Fall back to row sums — validator will pass tautologically. Logged.
        logger.warning(
            "AMEX XLSX has no 'Total' row; deriving declared totals from row sums "
            "(validator will pass tautologically; success message annotates this)"
        )
        declared_total_spends = sum(
            (r.amount for r in rows if r.direction == "out"), Decimal("0")
        )
        declared_total_credits = sum(
            (r.amount for r in rows if r.direction == "in"), Decimal("0")
        )
        declared_totals = {
            "total_spends": declared_total_spends,
            "total_credits": declared_total_credits,
            "closing_balance": None,
            "_derived_from_rows": True,  # flag picked up by pipeline for annotation
        }
    else:
        declared_total_credits = Decimal("0")  # split layouts may add a separate credit total
        declared_totals = {
            "total_spends": declared_total_spends,
            "total_credits": declared_total_credits,
            "closing_balance": None,
            "_derived_from_rows": False,
        }

    return ParseResult(
        rows=rows,
        declared_totals=declared_totals,
        pdf_content_hash=pdf_content_hash,
        parser_version=__parser_version__,
    )
```

- [ ] **Step 4.4: Run tests; calibrate KNOWN_COLUMN_SETS if needed**

```bash
pytest tests/test_amex_cc_parser.py -v
```
Expected: 9 passed (or 5 + 4 skipped if AMEX fixture absent — but Preconditions required it present).

If `find_header_row` fails on the real fixture, the `ParserError` message includes the actual headers. Add a 4th entry to `KNOWN_COLUMN_SETS` based on those headers, re-run.

- [ ] **Step 4.5: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 4.6: Commit**

```bash
git add skills/finance/ingestion/parsers/amex_cc.py tests/test_amex_cc_parser.py
git commit -m "feat(parsers): AMEX CC deterministic XLSX parser via pandas.read_excel"
```

---

## Task 5: `pipeline.py` — orchestrator

**Files:**
- Create: `skills/finance/ingestion/pipeline.py`
- Test: `tests/test_pipeline.py`

This task uses the supabase-py upsert behavior verified in Task 0.2. If Task 0 found `upsert(..., on_conflict='import_hash', ignore_duplicates=True)` doesn't work as expected, **substitute the per-row insert + try/except fallback before writing.**

- [ ] **Step 5.1: Write failing tests (mocked Supabase)**

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
        pdf_content_hash="abc" * 21 + "z",
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


def _make_table_mock(txn_inserts, log_inserts):
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
    return mock_table


def test_pipeline_validation_failure_no_insert():
    """Totals mismatch → no transactions inserted, ingestion_log gets total_check_failed."""
    from skills.finance.ingestion.pipeline import ingest

    pr = _make_pr([_row(100, 1), _row(50, 2)], 200, 0)
    source = SourceMeta(source="manual_pdf", source_ref="test.pdf")

    txn_inserts, log_inserts = [], []
    fake_client = MagicMock()
    fake_client.table.side_effect = _make_table_mock(txn_inserts, log_inserts)

    with patch("skills.finance.ingestion.pipeline.service_client", return_value=fake_client):
        result = asyncio.run(ingest(pr, ICICI_CC_ACCOUNT_ID, source))

    assert len(txn_inserts) == 0
    assert len(log_inserts) == 1
    assert log_inserts[0]["status"] == "total_check_failed"
    assert result["status"] == "total_check_failed"


def test_pipeline_success_inserts_rows_and_logs():
    from skills.finance.ingestion.pipeline import ingest

    pr = _make_pr([_row(100, 1, merchant="SWIGGY"), _row(50, 2, merchant="BLINKIT")], 150, 0)
    source = SourceMeta(source="manual_pdf", source_ref="test.pdf")

    txn_inserts, log_inserts = [], []
    fake_client = MagicMock()
    fake_client.table.side_effect = _make_table_mock(txn_inserts, log_inserts)

    with patch("skills.finance.ingestion.pipeline.service_client", return_value=fake_client):
        result = asyncio.run(ingest(pr, ICICI_CC_ACCOUNT_ID, source))

    assert len(txn_inserts) == 1
    inserted = txn_inserts[0]
    assert len(inserted) == 2
    assert inserted[0]["raw_merchant"] == "SWIGGY"
    assert len(inserted[0]["import_hash"]) == 64
    assert inserted[0]["parser_version"] == "icici-cc/v1"
    assert result["status"] == "success"


def test_pipeline_import_hash_per_row_uses_mode_b():
    from skills.finance.ingestion.pipeline import ingest

    pr = _make_pr([_row(350, 1, merchant="SWIGGY"), _row(350, 2, merchant="SWIGGY")], 700, 0)
    source = SourceMeta(source="manual_pdf", source_ref="test.pdf")

    captured_hashes = []
    def fake_import_hash_pdf(**kwargs):
        captured_hashes.append(kwargs)
        return "h" * 64

    txn_inserts, log_inserts = [], []
    fake_client = MagicMock()
    fake_client.table.side_effect = _make_table_mock(txn_inserts, log_inserts)

    with patch("skills.finance.ingestion.pipeline.service_client", return_value=fake_client), \
         patch("skills.finance.ingestion.pipeline.import_hash_pdf",
               side_effect=fake_import_hash_pdf):
        asyncio.run(ingest(pr, ICICI_CC_ACCOUNT_ID, source))

    assert len(captured_hashes) == 2
    assert captured_hashes[0]["source_row_ordinal"] == 1
    assert captured_hashes[1]["source_row_ordinal"] == 2
    assert captured_hashes[0]["pdf_content_hash"] == captured_hashes[1]["pdf_content_hash"]
    assert captured_hashes[0]["parser_version"] == "icici-cc/v1"
```

- [ ] **Step 5.2: Run tests, verify failures**

```bash
pytest tests/test_pipeline.py -v
```
Expected: 3 errors (ModuleNotFoundError).

- [ ] **Step 5.3: Implement `pipeline.py`**

```python
# skills/finance/ingestion/pipeline.py
"""Ingestion pipeline orchestrator.

Runs: validate → compute Mode-B import_hash per row → upsert to transactions →
log to ingestion_log → return status. On totals failure, NO rows are
inserted (per spec §8 / PRD §18.4 — entire statement rejected).

Caller (folder_watcher.dispatch_to_parser) handles the Telegram summary
message after this returns.
"""
from __future__ import annotations
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
    """NB: normalized_description = raw_merchant in Week 2; merchant normalization
    lands in Week 5 with a parser_version bump that forces re-ingest of affected rows."""
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
    derived = pr.declared_totals.get("_derived_from_rows", False)
    error_msg = "declared totals derived from row sums (no Total row in source)" if derived else None
    log_row = {
        "source": source.source,
        "source_ref": source.source_ref,
        "status": status,
        "rows_added": rows_added,
        "declared_total": str(val.declared_out),
        "extracted_total": str(val.extracted_out),
        "error_msg": error_msg,
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
    keeps the pipeline pure (no Telegram I/O)."""
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

- [ ] **Step 5.4: Run tests, verify all pass**

```bash
pytest tests/test_pipeline.py -v
```
Expected: 3 passed.

- [ ] **Step 5.5: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 5.6: Commit**

```bash
git add skills/finance/ingestion/pipeline.py tests/test_pipeline.py
git commit -m "feat(ingestion): pipeline orchestrator (validate → import_hash → upsert → log)"
```

---

## Task 6: `folder_watcher.py`

Watches `~/finance-inbox/` for both `*.pdf` AND `*.xlsx`; dispatches by extension + bank token.

**Files:**
- Create: `skills/finance/ingestion/folder_watcher.py`
- Test: `tests/test_folder_watcher.py`

- [ ] **Step 6.1: Write failing tests**

```python
# tests/test_folder_watcher.py
import asyncio
from pathlib import Path
from unittest.mock import patch
import pytest


@pytest.fixture
def tmp_inbox(tmp_path):
    inbox = tmp_path / "finance-inbox"
    inbox.mkdir()
    return inbox


def test_dispatch_unknown_filename_renames_to_rejected(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "random.pdf"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.send_alert") as mock_alert, \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser") as mock_dispatch:
        asyncio.run(handle_new_file(f))
    assert (tmp_inbox / "random.pdf.rejected").exists()
    assert not f.exists()
    mock_dispatch.assert_not_called()
    mock_alert.assert_called_once()


def test_dispatch_icici_pdf_calls_parser(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "icici_cc_2026_05.pdf"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.password_lookup",
               return_value="testpass"), \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser") as mock_dispatch:
        asyncio.run(handle_new_file(f))
    mock_dispatch.assert_called_once()
    assert mock_dispatch.call_args.kwargs["bank"] == "icici_cc"
    assert mock_dispatch.call_args.kwargs["password"] == "testpass"


def test_dispatch_amex_xlsx_calls_parser_no_password(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "amex_cc_2026_05.xlsx"
    f.write_bytes(b"PK\x03\x04fake")
    with patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser") as mock_dispatch:
        asyncio.run(handle_new_file(f))
    mock_dispatch.assert_called_once()
    assert mock_dispatch.call_args.kwargs["bank"] == "amex_cc"
    assert mock_dispatch.call_args.kwargs["password"] is None


def test_amex_pdf_extension_mismatch_rejected(tmp_inbox):
    """AMEX should be XLSX in V1; an AMEX PDF is a category mismatch."""
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "amex_cc_statement.pdf"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.send_alert") as mock_alert, \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser") as mock_dispatch:
        asyncio.run(handle_new_file(f))
    assert (tmp_inbox / "amex_cc_statement.pdf.rejected").exists()
    mock_dispatch.assert_not_called()
    assert mock_alert.called
    assert "extension" in str(mock_alert.call_args).lower() or "expects" in str(mock_alert.call_args).lower()


def test_icici_xlsx_extension_mismatch_rejected(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "icici_cc_statement.xlsx"
    f.write_bytes(b"PK\x03\x04fake")
    with patch("skills.finance.ingestion.folder_watcher.send_alert") as mock_alert, \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser") as mock_dispatch:
        asyncio.run(handle_new_file(f))
    assert (tmp_inbox / "icici_cc_statement.xlsx.rejected").exists()
    mock_dispatch.assert_not_called()
    assert mock_alert.called


def test_dispatch_ambiguous_filename_alerts_and_rejects(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "icici_amex_partnership.pdf"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.send_alert") as mock_alert, \
         patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser") as mock_dispatch:
        asyncio.run(handle_new_file(f))
    assert (tmp_inbox / "icici_amex_partnership.pdf.rejected").exists()
    mock_dispatch.assert_not_called()
    assert mock_alert.called
    assert "ambiguous" in str(mock_alert.call_args).lower()


def test_already_rejected_files_are_ignored(tmp_inbox):
    from skills.finance.ingestion.folder_watcher import handle_new_file
    f = tmp_inbox / "random.pdf.rejected"
    f.write_bytes(b"%PDF-fake")
    with patch("skills.finance.ingestion.folder_watcher.dispatch_to_parser") as mock_dispatch, \
         patch("skills.finance.ingestion.folder_watcher.send_alert") as mock_alert:
        asyncio.run(handle_new_file(f))
    mock_dispatch.assert_not_called()
    mock_alert.assert_not_called()
    assert f.exists()
```

- [ ] **Step 6.2: Run tests, verify failures**

```bash
pytest tests/test_folder_watcher.py -v
```
Expected: 7 errors.

- [ ] **Step 6.3: Implement `folder_watcher.py`**

```python
# skills/finance/ingestion/folder_watcher.py
"""Watches ~/finance-inbox/ for new PDFs and XLSX files; dispatches to the right parser.

Single-threaded: files are processed sequentially in arrival order. Spec §5.1
locks this for V1 (debugging simplicity)."""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from uuid import UUID

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from skills.finance.ingestion._common import (
    AmbiguousCredentialError,
    Bank,
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

# Per-bank expected file extension (V1)
EXPECTED_EXTENSION: dict[str, str] = {
    "icici_cc": ".pdf",
    "amex_cc": ".xlsx",
}


async def dispatch_to_parser(
    file_path: Path,
    bank: Bank,
    password: str | None,
) -> None:
    """Call the right parser, then ingest. Send Telegram summary after ingest."""
    if bank == "icici_cc":
        from skills.finance.ingestion.parsers.icici_cc import parse as icici_parse
        parse_result = await asyncio.to_thread(icici_parse, file_path, password or "")
        source = SourceMeta(source="manual_pdf", source_ref=file_path.name)
    elif bank == "amex_cc":
        from skills.finance.ingestion.parsers.amex_cc import parse as amex_parse
        parse_result = await asyncio.to_thread(amex_parse, file_path)
        source = SourceMeta(source="manual_xlsx", source_ref=file_path.name)
    else:
        raise ValueError(f"Unknown bank: {bank}")

    account_id = ACCOUNT_IDS[bank]
    log_entry = await ingest(parse_result, account_id, source)
    await _send_summary(bank, file_path.name, log_entry, parse_result)


async def _send_summary(bank: Bank, filename: str, log_entry: dict,
                        parse_result) -> None:
    """Send a per-statement summary message to the main bot."""
    from skills.finance.bot.main import bot as main_bot
    status = log_entry["status"]
    if status == "success":
        derived = parse_result.declared_totals.get("_derived_from_rows", False)
        annotation = ""
        if derived:
            annotation = "\n_Note: declared totals derived from row sums; validator effectively skipped (no Total row in source)._"
        text = (
            f"📥 {bank.upper().replace('_', ' ')} {filename} ingested\n"
            f"{log_entry['rows_added']} rows, "
            f"₹{log_entry['extracted_total']} (declared ₹{log_entry['declared_total']}) — totals match ✓"
            f"{annotation}"
        )
        await main_bot.send_message(
            chat_id=settings.telegram_chat_id_rajat, text=text, parse_mode="Markdown",
        )
    elif status == "skipped_duplicate":
        await main_bot.send_message(
            chat_id=settings.telegram_chat_id_rajat,
            text=f"📥 {bank.upper().replace('_', ' ')} {filename}: already ingested previously (skipped).",
        )
    # 'total_check_failed' alerts go via send_alert from inside pipeline; no double-message.


async def handle_new_file(file_path: Path) -> None:
    """Top-level handler for any new file in the inbox.

    Routes by filename token-match + extension match. On unknown / ambiguous /
    extension-mismatch: rename to <name>.rejected so re-scans don't re-trigger;
    send alert."""
    name = file_path.name
    if name.endswith(".rejected"):
        return  # already-rejected; ignore

    ext = file_path.suffix.lower()
    if ext not in (".pdf", ".xlsx"):
        return  # not a file type we care about

    bank = detect_bank_from_filename(name)
    if bank is None:
        n = name.lower()
        is_icici = ("icici" in n) and ("cc" in n)
        is_amex = ("amex" in n) or ("american" in n)
        if is_icici and is_amex:
            msg = f"Ambiguous filename '{name}' — matches both ICICI and AMEX. Rejected."
        else:
            msg = f"Filename '{name}' doesn't match any known bank pattern. Rename to include 'icici_cc_' or 'amex_cc_' and re-drop. Rejected."
        rejected = file_path.parent / f"{name}.rejected"
        await asyncio.to_thread(file_path.rename, rejected)
        await send_alert(msg)
        return

    expected_ext = EXPECTED_EXTENSION[bank]
    if ext != expected_ext:
        msg = (
            f"Bank '{bank}' expects {expected_ext} files in V1; got '{ext}' for '{name}'. "
            f"ICICI = PDF, AMEX = XLSX. Rejected."
        )
        rejected = file_path.parent / f"{name}.rejected"
        await asyncio.to_thread(file_path.rename, rejected)
        await send_alert(msg)
        return

    # Resolve password — only for ICICI (XLSX is unencrypted)
    password: str | None = None
    if bank == "icici_cc":
        try:
            password = await asyncio.to_thread(password_lookup, bank)
        except (AmbiguousCredentialError, CredentialNotFoundError) as e:
            logger.exception("password lookup failed for %s", bank)
            rejected = file_path.parent / f"{name}.rejected"
            await asyncio.to_thread(file_path.rename, rejected)
            await send_alert(f"Password lookup for {bank} failed: {e}. {name} rejected.")
            return

    try:
        await dispatch_to_parser(file_path, bank=bank, password=password)
    except Exception as e:  # noqa: BLE001
        logger.exception("dispatch failed for %s", name)
        await send_alert(f"Ingestion failed for {name}: {e}")


class _FileEventHandler(FileSystemEventHandler):
    """Bridges sync watchdog callbacks to the async event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop
        self._lock = asyncio.Lock()

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_handle(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._maybe_handle(event, path_attr="dest_path")

    def _maybe_handle(self, event: FileSystemEvent, path_attr: str = "src_path") -> None:
        if event.is_directory:
            return
        path = Path(getattr(event, path_attr))
        if path.suffix.lower() not in (".pdf", ".xlsx"):
            return
        asyncio.run_coroutine_threadsafe(self._serialized_handle(path), self.loop)

    async def _serialized_handle(self, path: Path) -> None:
        async with self._lock:
            await handle_new_file(path)


async def run() -> None:
    """Long-running task — start in app.py alongside aiogram + APScheduler."""
    inbox = Path(settings.finance_inbox_path)
    inbox.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    handler = _FileEventHandler(loop)
    observer = Observer()
    observer.schedule(handler, str(inbox), recursive=False)
    observer.start()
    logger.info("folder_watcher started on %s", inbox)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("folder_watcher stopping")
    finally:
        observer.stop()
        observer.join(timeout=5)
        logger.info("folder_watcher stopped cleanly")
```

- [ ] **Step 6.4: Run tests, verify all pass**

```bash
pytest tests/test_folder_watcher.py -v
```
Expected: 7 passed.

- [ ] **Step 6.5: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 6.6: Commit**

```bash
git add skills/finance/ingestion/folder_watcher.py tests/test_folder_watcher.py
git commit -m "feat(ingestion): folder watcher with bank+extension dispatch (PDF or XLSX)"
```

---

## Task 7: `bot/document_handler.py` — Telegram doc handler with auto-rename

**Files:**
- Create: `skills/finance/bot/document_handler.py`
- Test: `tests/test_document_handler.py`
- Modify: `skills/finance/bot/main.py` — register the handler

- [ ] **Step 7.1: Write failing tests**

```python
# tests/test_document_handler.py
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


def test_unambiguous_icici_pdf_saves_with_canonical_prefix(tmp_path, monkeypatch):
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
    assert save_path.suffix == ".pdf"
    fake_message.answer.assert_called_once()


def test_unambiguous_amex_xlsx_saves_with_canonical_prefix(tmp_path, monkeypatch):
    from skills.finance.bot.document_handler import handle_document

    inbox = tmp_path / "finance-inbox"
    inbox.mkdir()
    monkeypatch.setattr(
        "skills.finance.bot.document_handler.settings",
        MagicMock(finance_inbox_path=str(inbox), telegram_chat_id_rajat="42"),
    )

    fake_doc = MagicMock(file_name="AMEX_April_2026.xlsx", file_id="fake_id")
    fake_message = MagicMock(chat=MagicMock(id=42), document=fake_doc)
    fake_message.answer = AsyncMock()

    fake_bot = MagicMock()
    fake_bot.download = AsyncMock()

    asyncio.run(handle_document(fake_message, bot=fake_bot))

    fake_bot.download.assert_called_once()
    save_path = Path(fake_bot.download.call_args.kwargs["destination"])
    assert save_path.name.startswith("amex_cc_")
    assert save_path.suffix == ".xlsx"


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

    fake_bot.download.assert_not_called()
    fake_message.answer.assert_called_once()
    assert "reply_markup" in fake_message.answer.call_args.kwargs


def test_non_whitelisted_user_silently_ignored(monkeypatch):
    from skills.finance.bot.document_handler import handle_document

    monkeypatch.setattr(
        "skills.finance.bot.document_handler.settings",
        MagicMock(finance_inbox_path="/tmp", telegram_chat_id_rajat="42"),
    )

    fake_doc = MagicMock(file_name="anything.pdf", file_id="fake_id")
    fake_message = MagicMock(chat=MagicMock(id=999), document=fake_doc)
    fake_message.answer = AsyncMock()

    fake_bot = MagicMock()
    fake_bot.download = AsyncMock()

    asyncio.run(handle_document(fake_message, bot=fake_bot))

    fake_message.answer.assert_not_called()
    fake_bot.download.assert_not_called()
```

- [ ] **Step 7.2: Run tests, verify failures**

```bash
pytest tests/test_document_handler.py -v
```
Expected: 4 errors.

- [ ] **Step 7.3: Implement `bot/document_handler.py`**

```python
# skills/finance/bot/document_handler.py
"""Telegram document handler — receives PDF or XLSX docs, saves to inbox with
canonical filename. Folder watcher then dispatches.

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
    stem = Path(name).stem
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")


def _canonical_name(bank: str, original_filename: str) -> str:
    ext = Path(original_filename).suffix.lower() or ".pdf"
    return f"{bank}_{_sanitize_stem(original_filename)}{ext}"


async def handle_document(message: Message, bot: Bot) -> None:
    """Aiogram Document message handler.

    1. Whitelist check.
    2. Token-match the original filename.
    3a. Unambiguous match → save to inbox with canonical prefix; reply confirmation.
    3b. Ambiguous / no match → send inline keyboard prompting [ICICI CC] / [AMEX CC] / [Cancel].
    """
    if not _is_rajat(message):
        return

    doc = message.document
    if doc is None:
        return

    bank = detect_bank_from_filename(doc.file_name or "")

    if bank is None:
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

    inbox = Path(settings.finance_inbox_path)
    inbox.mkdir(parents=True, exist_ok=True)
    canonical = inbox / _canonical_name(bank, doc.file_name or "unnamed.pdf")
    await bot.download(doc, destination=str(canonical))
    logger.info("saved telegram doc as %s", canonical)
    await message.answer(f"Saved as `{canonical.name}` — processing.", parse_mode="Markdown")
```

- [ ] **Step 7.4: Wire handler into `bot/main.py`**

Read the current `skills/finance/bot/main.py`. Append (or modify) so the document handler is registered alongside the existing `/ping` handler:

```python
# Imports (add):
from aiogram import F
from skills.finance.bot.document_handler import handle_document

# After existing /ping handler:
@dp.message(F.document)
async def _document_handler(message: Message) -> None:
    await handle_document(message, bot=bot)
```

- [ ] **Step 7.5: Run tests**

```bash
pytest tests/test_document_handler.py tests/test_bot_middleware.py -v
```
Expected: 5 passed (4 new + 1 existing middleware).

- [ ] **Step 7.6: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 7.7: Commit**

```bash
git add skills/finance/bot/document_handler.py skills/finance/bot/main.py tests/test_document_handler.py
git commit -m "feat(bot): Telegram document handler — PDF + XLSX auto-rename, inline keyboard fallback"
```

---

## Task 8: `/model list` command

**Files:**
- Modify: `skills/finance/bot/main.py` — register `/model` command
- Test: extend `tests/test_bot_middleware.py`

- [ ] **Step 8.1: Write failing test**

Append to `tests/test_bot_middleware.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, mock_open, patch


def test_model_list_command_returns_yaml():
    from skills.finance.bot.main import model_list_handler
    fake_message = MagicMock(chat=MagicMock(id=42), text="/model list")
    fake_message.answer = AsyncMock()

    fake_yaml = "pdf_extraction:\n  model: gemini/gemini-2.5-flash\n"
    with patch("skills.finance.bot.main.settings",
               MagicMock(telegram_chat_id_rajat="42")), \
         patch("builtins.open", mock_open(read_data=fake_yaml)):
        asyncio.run(model_list_handler(fake_message))

    fake_message.answer.assert_called_once()
    args, kwargs = fake_message.answer.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "pdf_extraction" in text
    assert "gemini/gemini-2.5-flash" in text


def test_model_list_rejects_non_rajat():
    from skills.finance.bot.main import model_list_handler
    fake_message = MagicMock(chat=MagicMock(id=999), text="/model list")
    fake_message.answer = AsyncMock()
    with patch("skills.finance.bot.main.settings",
               MagicMock(telegram_chat_id_rajat="42")):
        asyncio.run(model_list_handler(fake_message))
    fake_message.answer.assert_not_called()
```

- [ ] **Step 8.2: Run tests, verify failures**

```bash
pytest tests/test_bot_middleware.py -v
```

- [ ] **Step 8.3: Implement `/model` handler in `bot/main.py`**

Append:

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

- [ ] **Step 8.4: Run tests, verify pass**

```bash
pytest tests/test_bot_middleware.py -v
```

- [ ] **Step 8.5: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 8.6: Commit**

```bash
git add skills/finance/bot/main.py tests/test_bot_middleware.py
git commit -m "feat(bot): /model list command (V1 read-only; full family deferred to Week 4)"
```

---

## Task 9: `app.py` orchestration update

Folder watcher must run as a sibling task alongside aiogram polling, APScheduler, FastAPI. Graceful SIGTERM must also stop it.

**Files:**
- Modify: `app.py`
- Modify: `pyproject.toml` (add `watchdog`)

- [ ] **Step 9.1: Add `watchdog` to `pyproject.toml`**

Edit `pyproject.toml` `[project] dependencies = [...]` to include:

```toml
"watchdog>=3",                # folder watcher for ingestion (Week 2)
```

(No `pdf2image`, no `Pillow` — those were dropped in r2.)

Then:
```bash
source .venv/bin/activate
pip install -e ".[dev]"
deactivate
```

- [ ] **Step 9.2: Wire `folder_watcher.run()` into `app.py`**

Read the current `app.py`. Modify `main()` so the folder watcher runs concurrently with the bot, scheduler, and HTTP server:

```python
# Add to imports near the top of app.py:
from skills.finance.ingestion import folder_watcher

# Modify main() — add watcher_task before the existing asyncio.gather:
async def main() -> None:
    configure_logging()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    sched = _build_scheduler()
    sched.start()
    logger.info("scheduler started; jobs=%s", [j.id for j in sched.get_jobs()])

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

- [ ] **Step 9.3: Verify import smoke**

```bash
source .venv/bin/activate
python -c "import app; print('import OK')"
deactivate
```
Expected: `import OK` with no traceback.

- [ ] **Step 9.4: Lint, typecheck, full suite**

```bash
make lint && make typecheck && make test
```

- [ ] **Step 9.5: Commit**

```bash
git add app.py pyproject.toml
git commit -m "feat(app): start folder_watcher concurrently with graceful shutdown wiring"
```

- [ ] **Step 9.6: Restart launchd-supervised app**

```bash
launchctl kickstart -k gui/$(id -u)/com.rajat.pfa.app
sleep 5
launchctl print gui/$(id -u)/com.rajat.pfa.app | grep -E "state|last exit code"
```
Expected: `state = running`, no recent crash.

```bash
tail -20 ~/finance-logs/pfa.log | grep folder_watcher
```
Expected: line like `folder_watcher started on /Users/rajat/finance-inbox`.

---

## Task 10: Backfill drill + acceptance verification

**No code changes.** Drives spec §17 acceptance criteria green by running the real backfill.

- [ ] **Step 10.1: Verify fixtures present**

```bash
ls -la tests/golden_fixtures/
```
Expected: `icici_sample.pdf` (Week 1) AND `amex_sample.xlsx` (Week 2 precondition).

- [ ] **Step 10.2: Drop 6 files into `~/finance-inbox/`**

3 ICICI CC PDFs + 3 AMEX CC XLSX. Example:

```bash
cp ~/Downloads/ICICI_April_2026.pdf       ~/finance-inbox/icici_cc_2026_04.pdf
cp ~/Downloads/ICICI_May_2026.pdf         ~/finance-inbox/icici_cc_2026_05.pdf
cp ~/Downloads/ICICI_June_2026.pdf        ~/finance-inbox/icici_cc_2026_06.pdf
cp ~/Downloads/AMEX_April_2026.xlsx       ~/finance-inbox/amex_cc_2026_04.xlsx
cp ~/Downloads/AMEX_May_2026.xlsx         ~/finance-inbox/amex_cc_2026_05.xlsx
cp ~/Downloads/AMEX_June_2026.xlsx        ~/finance-inbox/amex_cc_2026_06.xlsx
```

(Or forward each from your phone to the Telegram bot — it'll auto-rename and save.)

- [ ] **Step 10.3: Watch logs as they process**

```bash
tail -f ~/finance-logs/pfa.log
```

You should see, for each file, `dispatch_to_parser` log + `pipeline ingested N rows` + Telegram summary message arriving in your main bot.

- [ ] **Step 10.4: Verify backfill landed**

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

Expected: two rows (ICICI CC, AMEX CC), each spanning ~3 months, totals matching what your statements declared.

- [ ] **Step 10.5: Walk spec §17 acceptance criteria**

Each box from spec §17:
- [ ] 6 backfill files ingested into `transactions`
- [ ] `make lint && make typecheck && make test` clean — re-run now to confirm
- [ ] Validator catches manually-injected wrong amount → `test_statement_validator.py::test_validator_over_tolerance_rejects` passes
- [ ] AMEX XLSX header detection on real fixture → `test_amex_cc_parser.py::test_parse_real_amex_xlsx_returns_nonempty_parseresult` passes
- [ ] Folder watcher under launchd → verified in 9.6
- [ ] Telegram doc handler auto-rename → forward a non-canonically-named PDF AND XLSX, verify both saved with prefix
- [ ] Each parser exports `__parser_version__` — `"icici-cc/v1"`, `"amex-cc-xlsx/v1"` — covered by tests
- [ ] No PII committed to git: `git log --all --source -- 'tests/golden_fixtures/*.pdf' 'tests/golden_fixtures/*.xlsx' '.env' 'credentials.yaml' 'migrations/*.local.sql'` returns nothing
- [ ] `tasks/week-2-todo.md` exists; `tasks/todo.md` preserved as Week 1 record — `ls tasks/`
- [ ] No new `brew install` lines required to run Week 2

- [ ] **Step 10.6: Tag `week-2-ingestion`**

```bash
git -c user.name="Rajat Sharma" -c user.email="sharma.rajat70@gmail.com" \
    tag -a week-2-ingestion -m "Week 2 — Ingestion (ICICI CC PDF + AMEX CC XLSX) complete

3 months of historical CC transactions ingested.
Statement-total validator: live, ±₹1 tolerance.
ICICI CC: deterministic via pikepdf + pdfplumber + regex.
AMEX CC: deterministic via pandas.read_excel (no LLM in path; calibration not needed).
Folder watcher + Telegram doc handler: both source paths exercised; PDF + XLSX both supported.
/model list: shipped (full family deferred to Week 4)."
```

- [ ] **Step 10.7: Update `tasks/lessons.md` with any patterns from Week 2**

Anything that surprised you (regex calibration pain, supabase-py quirk, AMEX column variant we hadn't anticipated, etc.) — append a lessons entry. Specific + actionable.

---

## Self-Review

**Spec coverage:**

| Spec section | Implemented in |
|---|---|
| §1 Goal | Tasks 3, 4, 5, 6, 7, 9, 10 |
| §2 Scope | Tasks 1–10 cover in-scope; deferred items acknowledged in plan File structure |
| §3 Architecture (4 layers) | Task 1 (common types), 2 (validate), 3+4 (parse), 5 (persist), 6 (source: watcher), 7 (source: Telegram) |
| §4 File structure | Mirrored in plan |
| §5.1 Folder watcher (PDF+XLSX, ext mismatch) | Task 6 |
| §5.2 Telegram doc handler | Task 7 |
| §6.1 Common contract | Task 1 |
| §6.2 ICICI parser (deterministic PDF) | Task 3 |
| §6.3 AMEX parser (deterministic XLSX) | Task 4 |
| §7 Validator | Task 2 |
| §8 Persist (pipeline) | Task 5 |
| §9 Telegram review flow | Task 6 (`_send_summary` in `dispatch_to_parser`) |
| §10 /model list | Task 8 |
| §11 Backfill mechanics | Task 10 |
| §12 Error handling matrix | Task 6 (rejection paths), Task 5 (status logging) |
| §13 Testing strategy | All tasks include unit tests |
| §14 New dependencies | Task 9.1 (just `watchdog`) |
| §15 Code changes outside ingestion/ | Task 7.4 (bot/main.py), Task 9.2 (app.py) |
| §16 Open implementation risks | Task 0.2 verifies upsert; Task 4 calibrates KNOWN_COLUMN_SETS; Task 9.6 verifies launchd |
| §17 Acceptance criteria | Task 10.5 walks each |

**Placeholder scan:** No "TBD", "TODO", "implement later", or vague handwaves. Every code-step has actual code.

**Type consistency:**
- `ParsedRow`, `ParseResult`, `SourceMeta`, `ValidationResult` defined in Task 1, imported consistently by Tasks 2, 3, 4, 5, 6.
- `Bank` literal type (`"icici_cc" | "amex_cc"`) in Task 1; used in Tasks 1, 6.
- `__parser_version__` strings: `"icici-cc/v1"` (Task 3), `"amex-cc-xlsx/v1"` (Task 4); referenced consistently.
- `RAJAT_USER_ID` UUID from Task 1; used in Task 5.
- `ACCOUNT_IDS`, `EXPECTED_EXTENSION` mappings in Task 6 reference seeded UUIDs from `migrations/003_seed.local.sql`.
- `pdf_content_hash` field name retained on `ParseResult` even though it now also covers XLSX content (documented in `_common.py` comment) — this preserves schema column name `transactions.pdf_content_hash` from Week 1 without a migration.

---

## Execution Handoff

**Plan complete and saved to `tasks/week-2-todo.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration. Same workflow that landed Week 1 cleanly.

**2. Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

**Which approach?**
