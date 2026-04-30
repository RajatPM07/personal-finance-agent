# ICICI Savings PDF Ingestion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest 12 months of ICICI Savings (account `…1896`) PDF e-statements into `transactions`, skipping UPI rows at insert (Paytm passbook is UPI source-of-truth) and capturing payment rail info via a new `txn_mode` column.

**Architecture:** Mirror ICICI CC parser pattern (pikepdf decrypt + pdfplumber + regex anchor scan). Add one schema migration (`txn_mode` column), extend `_common.py` with two ParsedRow fields and one literal value, wire savings into `folder_watcher.dispatch_to_parser`. Validator stays unchanged — Paytm's flag-don't-drop pattern is reused so page-subtotal aggregation closes against `result.rows` while UPI rows are filtered at the insert step.

**Tech Stack:** Python 3.11, `pikepdf`, `pdfplumber`, `pytest` + `pytest-asyncio`, Supabase Postgres.

**Spec reference:** `docs/superpowers/specs/2026-04-30-icici-savings-ingestion-design.md`

---

## File map (locked decomposition)

| Action | Path | Responsibility |
|---|---|---|
| Create | `migrations/007_txn_mode.sql` | one ALTER TABLE on `transactions` |
| Modify | `skills/finance/ingestion/_common.py` | extend `Bank` literal; add 2 optional fields to `ParsedRow`; extend `insertable_rows()` filter; **move** `_decimal_from_indian_str` from `parsers/paytm_upi.py` here as a shared helper; extend `detect_bank_from_filename` for `icici_savings` disambiguation |
| Modify | `skills/finance/ingestion/parsers/paytm_upi.py` | replace local `_decimal_from_indian_str` with import from `_common` (compatibility shim — same semantics) |
| Create | `skills/finance/ingestion/parsers/icici_savings.py` | the parser itself |
| Modify | `skills/finance/ingestion/folder_watcher.py` | add `icici_savings` to `ACCOUNT_IDS` + `EXPECTED_EXTENSION` + `dispatch_to_parser` |
| Modify | `skills/finance/ingestion/pipeline.py` | include `txn_mode` field in `_build_insert_row` |
| Create | `tests/test_icici_savings_parser.py` | golden-file tests |
| Create | `tests/test_icici_savings_helpers.py` | pure-fn unit tests |
| Modify | `tests/test_ingestion_common.py` | regression tests for filename detection + Bank literal + new ParsedRow fields |

---

## Task 1: Schema migration `007_txn_mode.sql`

**Files:**
- Create: `migrations/007_txn_mode.sql`

- [ ] **Step 1: Create the migration file**

```sql
-- 007_txn_mode.sql
-- W3.4 ICICI Savings parser populates this column from the PDF's MODE column
-- (UPI / NEFT / IMPS / ATM / BIL/PAY / SAL / INT.PD / TFR / etc.). Other
-- parsers (ICICI CC, AMEX CC, Paytm) leave NULL — they don't have a structured
-- payment-rail field. W5 normalization can use txn_mode as a strong prior
-- (ATM → "Cash Withdrawal", SAL → "Salary", BIL/PAY → "Bills", etc.).

ALTER TABLE transactions
  ADD COLUMN txn_mode TEXT;

COMMENT ON COLUMN transactions.txn_mode IS
  'Payment rail / mode from bank statements (UPI, NEFT, IMPS, ATM, BIL/PAY, SAL, INT.PD, TFR, etc.). Populated by ICICI Savings parser; NULL for credit-card and Paytm parsers.';
```

- [ ] **Step 2: Commit (migration is applied later in Task 11)**

```bash
git add migrations/007_txn_mode.sql
git commit -m "migration(007): add transactions.txn_mode for W3.4 ICICI Savings parser"
```

---

## Task 2: Extend `_common.py` — Bank literal, ParsedRow fields, insertable_rows filter, shared decimal helper

**Files:**
- Modify: `skills/finance/ingestion/_common.py`
- Modify: `skills/finance/ingestion/parsers/paytm_upi.py` (replace local `_decimal_from_indian_str` with import)
- Test: `tests/test_ingestion_common.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_ingestion_common.py`:

```python
def test_bank_literal_includes_icici_savings():
    """ICICI Savings added in W3.4 — type-level guard against typos."""
    from typing import get_args
    from skills.finance.ingestion._common import Bank
    assert "icici_savings" in get_args(Bank)
    assert "icici_cc" in get_args(Bank)
    assert "amex_cc" in get_args(Bank)
    assert "paytm_upi" in get_args(Bank)


def test_parsed_row_accepts_icici_savings_fields():
    """is_upi_skip and txn_mode default safely so other parsers' rows
    construct unchanged."""
    from datetime import date
    from decimal import Decimal
    from skills.finance.ingestion._common import ParsedRow

    # Minimal construction (existing parsers' usage) still works:
    r = ParsedRow(
        txn_date=date(2026, 4, 30),
        amount=Decimal("100"),
        direction="out",
        raw_merchant="Test",
        source_row_ordinal=1,
    )
    assert r.is_upi_skip is False
    assert r.txn_mode is None

    # Savings-style construction sets the new fields:
    r2 = ParsedRow(
        txn_date=date(2026, 4, 30),
        amount=Decimal("500"),
        direction="out",
        raw_merchant="UPI/SOMEONE/12345",
        source_row_ordinal=2,
        is_upi_skip=True,
        txn_mode="UPI",
    )
    assert r2.is_upi_skip is True
    assert r2.txn_mode == "UPI"


def test_insertable_rows_excludes_is_upi_skip():
    """ParseResult.insertable_rows() filters out is_upi_skip=True rows
    (in addition to the existing is_amex_routed filter from W3.1)."""
    from datetime import date
    from decimal import Decimal
    from skills.finance.ingestion._common import ParsedRow, ParseResult

    keep_neft = ParsedRow(
        txn_date=date(2026, 4, 30), amount=Decimal("50000"), direction="in",
        raw_merchant="NEFT-LOMBARD", source_row_ordinal=1,
        is_upi_skip=False, txn_mode="NEFT",
    )
    drop_upi = ParsedRow(
        txn_date=date(2026, 4, 30), amount=Decimal("500"), direction="out",
        raw_merchant="UPI/SOMEONE", source_row_ordinal=2,
        is_upi_skip=True, txn_mode="UPI",
    )

    pr = ParseResult(
        rows=[keep_neft, drop_upi],
        declared_totals={"total_spends": Decimal("500"), "total_credits": Decimal("50000"),
                         "closing_balance": Decimal("100000"), "_derived_from_rows": False},
        pdf_content_hash="0" * 64,
        parser_version="test/v1",
    )
    insertable = pr.insertable_rows()
    assert len(insertable) == 1
    assert insertable[0].txn_mode == "NEFT"


def test_decimal_from_indian_str_in_common():
    """Helper moved from parsers/paytm_upi.py to _common for sharing.
    Behavior must remain identical: handle '1,23,456.78' Indian-format,
    Decimal pass-through, signed strings."""
    from decimal import Decimal
    from skills.finance.ingestion._common import _decimal_from_indian_str

    assert _decimal_from_indian_str("1,23,456.78") == Decimal("123456.78")
    assert _decimal_from_indian_str(1234.56) == Decimal("1234.56") or _decimal_from_indian_str(1234.56) == Decimal("1234.56")
    assert _decimal_from_indian_str(Decimal("100")) == Decimal("100")
    assert _decimal_from_indian_str("-1,234.56") == Decimal("-1234.56")
    assert _decimal_from_indian_str("+5,000.00") == Decimal("5000.00")
```

- [ ] **Step 2: Run failing tests**

```bash
.venv/bin/python -m pytest tests/test_ingestion_common.py::test_bank_literal_includes_icici_savings tests/test_ingestion_common.py::test_parsed_row_accepts_icici_savings_fields tests/test_ingestion_common.py::test_insertable_rows_excludes_is_upi_skip tests/test_ingestion_common.py::test_decimal_from_indian_str_in_common -v
```

Expected: 4 FAILs (Bank doesn't have icici_savings; ParsedRow rejects unknown kwargs; insertable_rows doesn't filter on is_upi_skip; _decimal_from_indian_str not importable from _common).

- [ ] **Step 3: Update `Bank` literal in `_common.py`**

Find the line `Bank = Literal["icici_cc", "amex_cc", "paytm_upi"]` (currently at line 22) and change to:

```python
Bank = Literal["icici_cc", "amex_cc", "paytm_upi", "icici_savings"]
```

- [ ] **Step 4: Add new fields to `ParsedRow`**

Locate the `@dataclass(frozen=True) class ParsedRow:` block in `_common.py`. Append two new optional fields after the existing W3.1 Paytm-only fields:

```python
@dataclass(frozen=True)
class ParsedRow:
    txn_date: date
    amount: Decimal                       # always positive; sign info on `direction`
    direction: Literal["in", "out"]
    raw_merchant: str
    source_row_ordinal: int                # 1..N within the file, deterministic per parser
    # Paytm-only fields (W3.1). ICICI/AMEX rows leave these at defaults.
    is_amex_routed: bool = False           # True → row is dropped at insert (D1 dual-entry skip)
    is_self_transfer: bool = False         # True → Paytm 'Money sent to ...' row to a known own-handle (D2)
    category_hint: str | None = None       # Paytm's pre-tagged category, emoji-stripped (D4)
    # ICICI Savings fields (W3.4). Other parsers leave these at defaults.
    is_upi_skip: bool = False              # True → row is dropped at insert (D1: Paytm = UPI source-of-truth)
    txn_mode: str | None = None            # UPI / NEFT / IMPS / ATM / BIL/PAY / SAL / INT.PD / TFR / etc. NULL for non-savings parsers.
```

- [ ] **Step 5: Extend `ParseResult.insertable_rows()` filter**

Locate the `insertable_rows` method on `ParseResult` and update its return expression to filter on `is_upi_skip` too:

```python
def insertable_rows(self) -> list[ParsedRow]:
    """Rows the pipeline should persist. Excludes is_amex_routed=True rows
    (D1 in W3.1 Paytm spec) AND is_upi_skip=True rows (D1 in W3.4 savings spec).
    Self-transfers (Paytm) ARE in the insertable set (D2: ingest all)."""
    return [r for r in self.rows if not r.is_amex_routed and not r.is_upi_skip]
```

- [ ] **Step 6: Move `_decimal_from_indian_str` from paytm_upi.py to _common.py**

Open `skills/finance/ingestion/parsers/paytm_upi.py`, find the function definition (currently at line 84):

```python
def _decimal_from_indian_str(s: Any) -> Decimal:
    """Convert '1,23,456.78' or 1234.56 or Decimal(...) → Decimal('1234.56').
    Tolerates Indian-style multi-comma thousand/lakh separators."""
    if isinstance(s, Decimal):
        return s
    cleaned = str(s).replace(",", "").strip()
    return Decimal(cleaned)
```

Cut it from `paytm_upi.py`. Add it to `_common.py` (anywhere after the existing imports — group with other small helpers near the bottom of the file, before `password_lookup` for organization):

```python
def _decimal_from_indian_str(s: Any) -> Decimal:
    """Convert '1,23,456.78' or 1234.56 or Decimal(...) → Decimal('1234.56').
    Tolerates Indian-style multi-comma thousand/lakh separators and signed
    strings like '-1,234.56' or '+5,000.00'."""
    if isinstance(s, Decimal):
        return s
    cleaned = str(s).replace(",", "").strip()
    return Decimal(cleaned)
```

In `paytm_upi.py`, replace the now-removed function with an import. Find the existing imports block at the top of the file and add to it:

```python
from skills.finance.ingestion._common import (
    ParsedRow,
    ParseResult,
    _decimal_from_indian_str,   # moved here in W3.4 to share with icici_savings parser
)
```

(If `ParsedRow, ParseResult` are already imported, just add `_decimal_from_indian_str` to the existing tuple.)

- [ ] **Step 7: Run all tests; verify pass**

```bash
.venv/bin/python -m pytest tests/test_ingestion_common.py tests/test_paytm_upi_parser.py tests/test_paytm_helpers.py -v
```

Expected: all PASS — 4 new common tests, plus existing Paytm parser tests still pass (proves the helper-move was a clean refactor).

- [ ] **Step 8: Commit**

```bash
git add skills/finance/ingestion/_common.py skills/finance/ingestion/parsers/paytm_upi.py tests/test_ingestion_common.py
git commit -m "feat(_common): extend Bank+ParsedRow+insertable_rows for W3.4; promote _decimal_from_indian_str to _common"
```

---

## Task 3: Extend `detect_bank_from_filename` to disambiguate `icici_savings`

**Files:**
- Modify: `skills/finance/ingestion/_common.py:86`
- Test: `tests/test_ingestion_common.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_ingestion_common.py`:

```python
def test_detect_bank_icici_savings_canonical():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("icici_savings_2026_02.pdf") == "icici_savings"


def test_detect_bank_icici_sav_short():
    """Short 'sav' token also matches (defensive)."""
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("icici_sav_2026_02.pdf") == "icici_savings"


def test_detect_bank_icici_cc_still_works():
    """Regression: existing icici_cc detection unaffected."""
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("icici_cc_2026_04.pdf") == "icici_cc"


def test_detect_bank_icici_alone_returns_none():
    """Bare 'icici' filename without disambiguator → None.
    Existing rejection path handles it."""
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("icici_2026_02.pdf") is None


def test_detect_bank_ambiguous_icici_savings_and_cc_returns_none():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("icici_cc_savings_combined.pdf") is None
```

- [ ] **Step 2: Run failing tests**

```bash
.venv/bin/python -m pytest tests/test_ingestion_common.py::test_detect_bank_icici_savings_canonical tests/test_ingestion_common.py::test_detect_bank_icici_sav_short tests/test_ingestion_common.py::test_detect_bank_icici_alone_returns_none tests/test_ingestion_common.py::test_detect_bank_ambiguous_icici_savings_and_cc_returns_none -v
```

Expected: 4 FAILs (function returns None for icici_savings filenames; bare icici might pass-through to icici_cc by accident).

- [ ] **Step 3: Update `detect_bank_from_filename`**

In `_common.py`, replace the existing `detect_bank_from_filename` function body with:

```python
def detect_bank_from_filename(filename: str) -> Bank | None:
    """Pure function. Lowercase + word-boundary token match.

    Returns:
      'icici_cc'        if filename has 'icici' AND 'cc' tokens (no 'savings'/'sav').
      'icici_savings'   if filename has 'icici' AND ('savings' OR 'sav') tokens (no 'cc').
      'amex_cc'         if filename has 'amex' OR 'american' tokens.
      'paytm_upi'       if filename has 'paytm' token.
      None              if multiple bank-family tokens collide (ambiguous), or no
                        family matches, or 'icici' appears without a disambiguator.
    """
    name = filename.lower()
    tokens = set(re.split(r"[^a-z0-9]+", name))
    has_icici = "icici" in tokens
    has_amex = ("amex" in tokens) or ("american" in tokens)
    has_paytm = "paytm" in tokens
    has_savings = ("savings" in tokens) or ("sav" in tokens)
    has_cc = "cc" in tokens

    # ICICI must specify CC vs SAVINGS unambiguously
    icici_specifier_count = (1 if has_cc else 0) + (1 if has_savings else 0)
    if has_icici and icici_specifier_count != 1:
        # Either no specifier ('icici_2026.pdf') OR both ('icici_cc_savings.pdf')
        # Both cases: route through rejection path (None).
        # If icici + cc/savings AND amex/paytm, fall through to multi-family check below.
        if not (has_amex or has_paytm):
            return None

    # Multi-family ambiguity check (e.g., paytm + amex)
    matches = sum([has_icici, has_amex, has_paytm])
    if matches > 1:
        return None

    if has_icici and has_cc:
        return "icici_cc"
    if has_icici and has_savings:
        return "icici_savings"
    if has_amex:
        return "amex_cc"
    if has_paytm:
        return "paytm_upi"
    return None
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_ingestion_common.py -v
```

Expected: all PASS — new icici_savings tests pass, existing icici_cc / amex_cc / paytm tests still pass.

- [ ] **Step 5: Commit**

```bash
git add skills/finance/ingestion/_common.py tests/test_ingestion_common.py
git commit -m "feat(_common): detect_bank_from_filename disambiguates icici_savings vs icici_cc"
```

---

## Task 4: Parser scaffold — `__parser_version__`, ParserError, helpers, UPI-skip classifier

**Files:**
- Create: `skills/finance/ingestion/parsers/icici_savings.py`
- Create: `tests/test_icici_savings_helpers.py`

- [ ] **Step 1: Write failing helper tests**

Create `tests/test_icici_savings_helpers.py`:

```python
def test_savings_parser_version():
    from skills.finance.ingestion.parsers.icici_savings import __parser_version__
    assert __parser_version__ == "icici-savings-pdf/v1"


def test_classify_upi_skip_true_for_mode_upi():
    from skills.finance.ingestion.parsers.icici_savings import classify_upi_skip
    assert classify_upi_skip(mode="UPI", particulars="some text") is True


def test_classify_upi_skip_true_for_mode_lowercase_upi():
    """Defensive: MODE column may extract with mixed case."""
    from skills.finance.ingestion.parsers.icici_savings import classify_upi_skip
    assert classify_upi_skip(mode="upi", particulars="some text") is True


def test_classify_upi_skip_true_for_particulars_upi_prefix():
    """OR-logic: when MODE extraction is finicky, PARTICULARS prefix
    catches the same row."""
    from skills.finance.ingestion.parsers.icici_savings import classify_upi_skip
    assert classify_upi_skip(mode="OTHER", particulars="UPI/SOMEONE/12345/...") is True


def test_classify_upi_skip_false_for_neft():
    from skills.finance.ingestion.parsers.icici_savings import classify_upi_skip
    assert classify_upi_skip(mode="NEFT", particulars="NEFT-LOMBARD-PAYROLL") is False


def test_classify_upi_skip_false_for_atm():
    from skills.finance.ingestion.parsers.icici_savings import classify_upi_skip
    assert classify_upi_skip(mode="ATM", particulars="ATM/CASH WDL") is False


def test_classify_upi_skip_handles_none():
    """Pandas NaN / None values must not crash."""
    from skills.finance.ingestion.parsers.icici_savings import classify_upi_skip
    assert classify_upi_skip(mode=None, particulars=None) is False
    assert classify_upi_skip(mode="", particulars="") is False


def test_parse_savings_date_dd_mm_yyyy():
    from datetime import date
    from skills.finance.ingestion.parsers.icici_savings import _parse_savings_date
    assert _parse_savings_date("28-02-2026") == date(2026, 2, 28)


def test_parse_savings_date_other_formats():
    """Defensive: ICICI uses DD-MM-YYYY but tolerate ISO + slashed forms."""
    from datetime import date
    from skills.finance.ingestion.parsers.icici_savings import _parse_savings_date
    assert _parse_savings_date("2026-02-28") == date(2026, 2, 28)
    assert _parse_savings_date("28/02/2026") == date(2026, 2, 28)


def test_parse_savings_date_unparseable_raises():
    from skills.finance.ingestion.parsers.icici_savings import (
        ParserError,
        _parse_savings_date,
    )
    import pytest
    with pytest.raises(ParserError):
        _parse_savings_date("not a date")
```

- [ ] **Step 2: Run failing tests**

```bash
.venv/bin/python -m pytest tests/test_icici_savings_helpers.py -v
```

Expected: all FAIL with `ModuleNotFoundError: No module named 'skills.finance.ingestion.parsers.icici_savings'`.

- [ ] **Step 3: Create the parser scaffold**

Create `skills/finance/ingestion/parsers/icici_savings.py`:

```python
"""ICICI Savings PDF e-statement parser — deterministic.

ICICI sends a password-protected PDF (same password convention as ICICI CC,
per credentials.yaml entries `icici_cc_<last4>` and `icici_savings_<last4>`).
Use pikepdf to decrypt + pdfplumber for text extraction + regex anchor scan
for the transaction table.

Spec: docs/superpowers/specs/2026-04-30-icici-savings-ingestion-design.md

Three Savings-specific behaviors:
  D1: rows where MODE=='UPI' OR PARTICULARS startswith 'UPI/' are flagged
      with is_upi_skip=True. They appear in result.rows so the validator
      can compare against page subtotals (which include UPI), but the
      pipeline drops them at insert via insertable_rows() because Paytm
      passbook is the V1 source-of-truth for UPI activity.
  D3: each ParsedRow carries the MODE column value into the new
      `txn_mode` field (UPI/NEFT/IMPS/ATM/BIL/PAY/SAL/INT.PD/TFR/etc.).
  D7: classify_upi_skip uses OR-logic across MODE and PARTICULARS for
      robustness against MODE-column extraction quirks.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__parser_version__ = "icici-savings-pdf/v1"


class ParserError(Exception):
    """Raised when the savings PDF layout is unrecognized or row parsing fails."""


def classify_upi_skip(mode: Any, particulars: Any) -> bool:
    """Return True iff this row represents a UPI transaction that should be
    dropped at insert (Paytm passbook is the V1 source-of-truth for UPI).

    OR-logic across both signals — D7 in the savings spec:
      - MODE column normalized → "UPI"  (case-insensitive)
      - PARTICULARS starts with "UPI/"  (catches rows where MODE extraction is finicky)
    """
    if mode is not None:
        m = str(mode).strip().upper()
        if m == "UPI":
            return True
    if particulars is not None:
        p = str(particulars).strip()
        if p.startswith("UPI/"):
            return True
    return False


def _parse_savings_date(s: Any) -> date:
    """ICICI Savings statements use DD-MM-YYYY format. Tolerate ISO + slashed
    variants for defensiveness (some ICICI exports use DD/MM/YYYY)."""
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    s = str(s).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ParserError(f"Could not parse ICICI Savings date: {s!r}")


def _sha256_file(path: Path) -> str:
    """Hash the original (still-encrypted) PDF bytes. Re-decrypting with pikepdf
    produces a different byte stream, so we hash the input file directly to
    keep the import_hash stable across re-ingestion attempts."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_icici_savings_helpers.py -v
```

Expected: all 10 PASS (parser_version + 6 classify_upi_skip + 3 _parse_savings_date).

- [ ] **Step 5: Commit**

```bash
git add skills/finance/ingestion/parsers/icici_savings.py tests/test_icici_savings_helpers.py
git commit -m "feat(savings): parser scaffold + UPI-skip classifier + date parser"
```

---

## Task 5: Anchor-based row extractor — find transaction table, scan date-prefixed lines

**Files:**
- Modify: `skills/finance/ingestion/parsers/icici_savings.py`
- Test: `tests/test_icici_savings_helpers.py`

This task implements the core line-by-line transaction extraction. We scan all pages for the transaction table header, then collect date-prefixed lines as rows. The page-1 summary block, marketing pages, and nominee block are all naturally skipped because they don't have date-prefixed lines.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_icici_savings_helpers.py`:

```python
def test_extract_data_row_full_columns():
    """A complete data line: DATE MODE PARTICULARS DEPOSITS WITHDRAWALS BALANCE.
    All columns extracted; one of DEPOSITS/WITHDRAWALS is non-zero."""
    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    line = "28-02-2026 NEFT NEFT-LOMBARD-PAYROLL FEB 2026 1,86,062.00 0.00 1,98,407.67"
    row = _extract_data_row(line)
    assert row is not None
    assert row.txn_date.isoformat() == "2026-02-28"
    assert row.direction == "in"
    assert str(row.amount) == "186062.00"
    assert row.txn_mode == "NEFT"
    assert "LOMBARD-PAYROLL" in row.raw_merchant


def test_extract_data_row_withdrawal():
    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    line = "27-02-2026 ATM ATM-CASH WDL/ATMID/12345 0.00 5,000.00 1,93,407.67"
    row = _extract_data_row(line)
    assert row is not None
    assert row.direction == "out"
    assert str(row.amount) == "5000.00"
    assert row.txn_mode == "ATM"


def test_extract_data_row_upi_flagged_skip():
    """UPI row gets is_upi_skip=True and txn_mode='UPI'."""
    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    line = "26-02-2026 UPI UPI/SOMEONE/UPI-REF-12345/SBI 0.00 500.00 1,98,407.67"
    row = _extract_data_row(line)
    assert row is not None
    assert row.is_upi_skip is True
    assert row.txn_mode == "UPI"


def test_extract_data_row_no_date_returns_none():
    """Lines that don't start with a date (continuation lines, headers,
    page footers) → None. Caller treats those separately."""
    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    assert _extract_data_row("DATE MODE PARTICULARS DEPOSITS WITHDRAWALS BALANCE") is None
    assert _extract_data_row("Total: 46,400.00 2,43,672.08 17,715.85") is None
    assert _extract_data_row("continuation of description") is None
    assert _extract_data_row("") is None


def test_extract_data_row_zero_amount_both_columns_returns_none():
    """Defensive: if both deposits and withdrawals are 0, the row has no
    monetary content — skip (parallel to ICICI CC's `amt <= 0` filter)."""
    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    line = "28-02-2026 INT INT.PD-INFO 0.00 0.00 1,98,407.67"
    assert _extract_data_row(line) is None
```

- [ ] **Step 2: Run failing tests**

```bash
.venv/bin/python -m pytest tests/test_icici_savings_helpers.py -v 2>&1 | tail -10
```

Expected: 5 FAILs — `_extract_data_row` not defined.

- [ ] **Step 3: Implement `_extract_data_row`**

Add at the top of `icici_savings.py`, alongside the other imports:

```python
import re
from decimal import Decimal

from skills.finance.ingestion._common import (
    ParsedRow,
    _decimal_from_indian_str,
)
```

Then add this function near the other helpers:

```python
# Anchor pattern: "DD-MM-YYYY MODE PARTICULARS_TEXT NUMBER NUMBER NUMBER"
# - Date: 10 chars exactly
# - MODE: short alphanumeric token (UPI, NEFT, IMPS, ATM, BIL/PAY, SAL, INT.PD, TFR, etc.)
# - PARTICULARS: arbitrary text (greedy, until the last 3 numeric columns)
# - DEPOSITS / WITHDRAWALS / BALANCE: Indian-format numerics like "1,23,456.78"
#
# Greedy match on PARTICULARS terminated by 3 trailing numerics ensures correct
# column alignment even when PARTICULARS contains spaces, slashes, or numbers.
_ROW_RE = re.compile(
    r"^(?P<date>\d{2}-\d{2}-\d{4})\s+"
    r"(?P<mode>[A-Z][A-Z0-9./]*?)\s+"            # MODE: starts with capital letter, may contain digits/dots/slashes
    r"(?P<particulars>.+?)\s+"                    # PARTICULARS: lazy match; the 3 trailing numerics anchor the end
    r"(?P<deposits>[0-9,]+\.\d{2})\s+"
    r"(?P<withdrawals>[0-9,]+\.\d{2})\s+"
    r"(?P<balance>[0-9,]+\.\d{2})$"
)


def _extract_data_row(line: str) -> ParsedRow | None:
    """Parse a single transaction line. Returns None if the line isn't a
    transaction row (header, page footer 'Total:', continuation line, blank,
    or both monetary columns are zero).

    Caller is responsible for assembling continuation lines onto the previous
    row's `raw_merchant` (see Task 6).
    """
    if not line:
        return None
    m = _ROW_RE.match(line.strip())
    if not m:
        return None
    deposits = _decimal_from_indian_str(m.group("deposits"))
    withdrawals = _decimal_from_indian_str(m.group("withdrawals"))
    if deposits == 0 and withdrawals == 0:
        # Informational rows (e.g. interest accrual notes) — no monetary content
        return None
    if deposits > 0 and withdrawals > 0:
        # Defensive — shouldn't happen on well-formed ICICI lines
        raise ParserError(
            f"Row has both deposits and withdrawals non-zero: {line!r}. "
            f"Layout drift likely; check ROW_RE column alignment."
        )
    if deposits > 0:
        direction = "in"
        amount = deposits
    else:
        direction = "out"
        amount = withdrawals
    mode = m.group("mode")
    particulars = m.group("particulars").strip()
    return ParsedRow(
        txn_date=_parse_savings_date(m.group("date")),
        amount=amount,
        direction=direction,
        raw_merchant=particulars,
        source_row_ordinal=0,                     # caller assigns the real ordinal
        txn_mode=mode,
        is_upi_skip=classify_upi_skip(mode=mode, particulars=particulars),
    )
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_icici_savings_helpers.py -v
```

Expected: all 15 PASS (10 prior + 5 new).

- [ ] **Step 5: Commit**

```bash
git add skills/finance/ingestion/parsers/icici_savings.py tests/test_icici_savings_helpers.py
git commit -m "feat(savings): _extract_data_row anchor regex + UPI-skip flagging"
```

---

## Task 6: Multi-line PARTICULARS continuation handling

**Files:**
- Modify: `skills/finance/ingestion/parsers/icici_savings.py`
- Test: `tests/test_icici_savings_helpers.py`

ICICI Savings sometimes wraps long PARTICULARS to a second line that has no DATE prefix. The continuation must be appended to the previous row's `raw_merchant`. We implement this as `_assemble_rows(lines)` taking the full text and returning ordinal-numbered rows.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_icici_savings_helpers.py`:

```python
def test_assemble_rows_single_line_each():
    """No continuation: each row is one line, ordinals are 1..N."""
    from skills.finance.ingestion.parsers.icici_savings import _assemble_rows
    lines = [
        "28-02-2026 NEFT NEFT-LOMBARD-PAYROLL 1,86,062.00 0.00 1,98,407.67",
        "27-02-2026 ATM ATM-CASH WDL 0.00 5,000.00 1,93,407.67",
    ]
    rows = _assemble_rows(lines)
    assert len(rows) == 2
    assert rows[0].source_row_ordinal == 1
    assert rows[1].source_row_ordinal == 2
    assert rows[0].txn_mode == "NEFT"
    assert rows[1].txn_mode == "ATM"


def test_assemble_rows_continuation_appends_to_previous():
    """Continuation line (no date) appends to the previous row's raw_merchant."""
    from skills.finance.ingestion.parsers.icici_savings import _assemble_rows
    lines = [
        "28-02-2026 NEFT NEFT-LOMBARD-PAYROLL 1,86,062.00 0.00 1,98,407.67",
        "REFERENCE-NUMBER-EXTRA-INFO",                       # continuation
        "27-02-2026 ATM ATM-CASH WDL 0.00 5,000.00 1,93,407.67",
    ]
    rows = _assemble_rows(lines)
    assert len(rows) == 2
    assert "REFERENCE-NUMBER-EXTRA-INFO" in rows[0].raw_merchant
    assert rows[0].raw_merchant.startswith("NEFT-LOMBARD-PAYROLL")


def test_assemble_rows_skips_header_total_blank():
    """Non-data lines (header / Total / blank / unrelated nominee block)
    don't disturb assembly."""
    from skills.finance.ingestion.parsers.icici_savings import _assemble_rows
    lines = [
        "DATE MODE PARTICULARS DEPOSITS WITHDRAWALS BALANCE",
        "",
        "28-02-2026 NEFT NEFT-LOMBARD 1,86,062.00 0.00 1,98,407.67",
        "Total: 1,86,062.00 0.00 1,98,407.67",
        "ACCOUNT TYPE ACCOUNT NUMBER MICR CODE IFS CODE NAME OF NOMINEE*",
    ]
    rows = _assemble_rows(lines)
    assert len(rows) == 1
    assert rows[0].source_row_ordinal == 1


def test_assemble_rows_continuation_only_attaches_after_a_data_row():
    """Stray continuation lines BEFORE any data row are dropped (no anchor
    to attach to). Avoid silently losing real data without a row context."""
    from skills.finance.ingestion.parsers.icici_savings import _assemble_rows
    lines = [
        "stray text before any row",                          # dropped
        "28-02-2026 NEFT NEFT-LOMBARD 1,86,062.00 0.00 1,98,407.67",
    ]
    rows = _assemble_rows(lines)
    assert len(rows) == 1
    assert "stray" not in rows[0].raw_merchant
```

- [ ] **Step 2: Run failing tests**

```bash
.venv/bin/python -m pytest tests/test_icici_savings_helpers.py -v 2>&1 | tail -10
```

Expected: 4 FAILs — `_assemble_rows` not defined.

- [ ] **Step 3: Implement `_assemble_rows`**

Append to `icici_savings.py`:

```python
# Boundary markers — stop assembly when seeing the nominee block on the last
# transaction page (so we don't mistakenly grab nominee detail lines as
# continuations of the last txn).
_NOMINEE_HEADER_RE = re.compile(
    r"^ACCOUNT\s+TYPE\s+ACCOUNT\s+NUMBER\s+MICR\s+CODE\s+IFS\s+CODE",
    re.IGNORECASE,
)
_TOTAL_FOOTER_RE = re.compile(r"^\s*Total\s*:", re.IGNORECASE)


def _assemble_rows(lines: list[str]) -> list[ParsedRow]:
    """Walk a flat list of text lines, build ParsedRows.

    For each line, in order:
      - If it parses as a data row → finalize the previous row, start a new one.
      - If it's a known boundary (Total: row / nominee header) → finalize and stop.
      - Otherwise (continuation candidate) → append to the previous row's
        raw_merchant if a row exists; drop the line otherwise (no anchor).

    Ordinals are assigned 1..N to the finalized rows in declaration order.
    """
    rows: list[ParsedRow] = []
    current: ParsedRow | None = None

    def finalize():
        nonlocal current
        if current is not None:
            ordinal = len(rows) + 1
            rows.append(
                ParsedRow(
                    txn_date=current.txn_date,
                    amount=current.amount,
                    direction=current.direction,
                    raw_merchant=current.raw_merchant.strip(),
                    source_row_ordinal=ordinal,
                    txn_mode=current.txn_mode,
                    is_upi_skip=current.is_upi_skip,
                )
            )
            current = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        # Boundary: nominee block ends the transaction table.
        if _NOMINEE_HEADER_RE.match(line):
            finalize()
            break

        # Boundary: per-page Total: footer — finalize current row but keep scanning
        # (next page's header / data may follow).
        if _TOTAL_FOOTER_RE.match(line):
            finalize()
            continue

        # Try as data row
        candidate = _extract_data_row(line)
        if candidate is not None:
            finalize()
            current = candidate
            continue

        # Continuation: append to current row's particulars (raw_merchant)
        if current is not None and not _looks_like_header_or_chrome(line):
            # Replace the frozen ParsedRow with a new one that has the appended text.
            current = ParsedRow(
                txn_date=current.txn_date,
                amount=current.amount,
                direction=current.direction,
                raw_merchant=(current.raw_merchant + " " + line).strip(),
                source_row_ordinal=current.source_row_ordinal,
                txn_mode=current.txn_mode,
                is_upi_skip=current.is_upi_skip
                            or classify_upi_skip(mode=current.txn_mode, particulars=line),
            )

    finalize()
    return rows


def _looks_like_header_or_chrome(line: str) -> bool:
    """Return True for lines that are clearly NOT continuation content.
    Covers: column header, page-1 summary block headers, page-N marketing,
    URLs, footer page-numbers, etc.

    Conservative: when in doubt, return False (treat as continuation).
    """
    upper = line.upper()
    if upper.startswith("DATE MODE PARTICULARS"):
        return True
    if upper.startswith("STATEMENT SUMMARY"):
        return True
    if upper.startswith("ACCOUNT HOLDERS"):
        return True
    if upper.startswith("ACCOUNT TYPE"):
        return True
    if "ICICI BANK LTD" in upper:
        return True
    if upper.startswith("PAGE "):
        return True
    return False
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_icici_savings_helpers.py -v
```

Expected: all 19 PASS (15 prior + 4 new).

- [ ] **Step 5: Commit**

```bash
git add skills/finance/ingestion/parsers/icici_savings.py tests/test_icici_savings_helpers.py
git commit -m "feat(savings): _assemble_rows handles multi-line PARTICULARS + boundary markers"
```

---

## Task 7: Page-subtotal extraction — `Total:` rows for validator inputs

**Files:**
- Modify: `skills/finance/ingestion/parsers/icici_savings.py`
- Test: `tests/test_icici_savings_helpers.py`

The page-subtotal `Total:` rows give us the validator inputs. Each transaction page has one in the format `[..., 'Total:', deposits, withdrawals, balance]`. Sum across all pages → statement-period totals.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_icici_savings_helpers.py`:

```python
def test_parse_total_row_basic():
    from skills.finance.ingestion.parsers.icici_savings import _parse_total_row
    from decimal import Decimal
    res = _parse_total_row("Total: 46,400.00 2,43,672.08 17,715.85")
    assert res is not None
    deposits, withdrawals, balance = res
    assert deposits == Decimal("46400.00")
    assert withdrawals == Decimal("243672.08")
    assert balance == Decimal("17715.85")


def test_parse_total_row_with_leading_whitespace():
    from skills.finance.ingestion.parsers.icici_savings import _parse_total_row
    res = _parse_total_row("    Total:  1,45,061.00  49,348.00  1,13,428.85")
    assert res is not None


def test_parse_total_row_non_total_returns_none():
    from skills.finance.ingestion.parsers.icici_savings import _parse_total_row
    assert _parse_total_row("28-02-2026 NEFT something 100.00 0.00 200.00") is None
    assert _parse_total_row("DATE MODE PARTICULARS DEPOSITS WITHDRAWALS BALANCE") is None
    assert _parse_total_row("") is None


def test_aggregate_totals_sums_across_pages():
    """Multiple Total: rows from different pages → summed."""
    from decimal import Decimal
    from skills.finance.ingestion.parsers.icici_savings import _aggregate_totals
    lines = [
        "28-02-2026 NEFT NEFT-LOMBARD 1,86,062.00 0.00 1,98,407.67",
        "Total: 1,86,062.00 0.00 1,98,407.67",        # page 1 subtotal
        "27-02-2026 ATM ATM-CASH 0.00 5,000.00 1,93,407.67",
        "Total: 0.00 5,000.00 1,93,407.67",            # page 2 subtotal
    ]
    totals = _aggregate_totals(lines)
    assert totals["total_credits"] == Decimal("186062.00")
    assert totals["total_spends"] == Decimal("5000.00")
    # closing_balance = last-seen subtotal balance
    assert totals["closing_balance"] == Decimal("193407.67")
    assert totals["_derived_from_rows"] is False


def test_aggregate_totals_no_total_rows_raises():
    """Defensive: if NO Total: rows are found, the parser can't produce
    declared totals → fail loud rather than silently using 0.
    Different from AMEX (which derives from row sums) because savings
    statements always have explicit subtotals; their absence indicates
    a layout change worth investigating."""
    from skills.finance.ingestion.parsers.icici_savings import (
        ParserError,
        _aggregate_totals,
    )
    import pytest
    with pytest.raises(ParserError):
        _aggregate_totals([
            "28-02-2026 NEFT NEFT-LOMBARD 1,86,062.00 0.00 1,98,407.67",
        ])
```

- [ ] **Step 2: Run failing tests**

```bash
.venv/bin/python -m pytest tests/test_icici_savings_helpers.py -v 2>&1 | tail -10
```

Expected: 5 FAILs — `_parse_total_row` and `_aggregate_totals` not defined.

- [ ] **Step 3: Implement total-row helpers**

Append to `icici_savings.py`:

```python
# Per-page subtotal: "Total: <deposits> <withdrawals> <balance>"
_TOTAL_ROW_RE = re.compile(
    r"^\s*Total\s*:\s*"
    r"(?P<deposits>[0-9,]+\.\d{2})\s+"
    r"(?P<withdrawals>[0-9,]+\.\d{2})\s+"
    r"(?P<balance>[0-9,]+\.\d{2})\s*$",
    re.IGNORECASE,
)


def _parse_total_row(line: str) -> tuple[Decimal, Decimal, Decimal] | None:
    """Parse 'Total: <deposits> <withdrawals> <balance>' line. Returns
    (deposits, withdrawals, balance) or None if line doesn't match."""
    m = _TOTAL_ROW_RE.match(line)
    if not m:
        return None
    return (
        _decimal_from_indian_str(m.group("deposits")),
        _decimal_from_indian_str(m.group("withdrawals")),
        _decimal_from_indian_str(m.group("balance")),
    )


def _aggregate_totals(lines: list[str]) -> dict:
    """Scan all lines for per-page Total: rows; aggregate to statement totals.

    Returns dict matching the existing validator's expected shape:
        {'total_spends': Decimal,
         'total_credits': Decimal,
         'closing_balance': Decimal,
         '_derived_from_rows': False}

    Raises ParserError if no Total: row is found across ANY page (indicates
    layout drift; ICICI savings statements always have explicit subtotals).
    """
    total_in = Decimal("0")
    total_out = Decimal("0")
    last_balance: Decimal | None = None
    for line in lines:
        parsed = _parse_total_row(line)
        if parsed is None:
            continue
        deposits, withdrawals, balance = parsed
        total_in += deposits
        total_out += withdrawals
        last_balance = balance
    if last_balance is None:
        raise ParserError(
            "No 'Total:' subtotal rows found in any page. ICICI Savings layout "
            "drift suspected; check the PDF text extraction for changes."
        )
    return {
        "total_spends": total_out,
        "total_credits": total_in,
        "closing_balance": last_balance,
        "_derived_from_rows": False,
    }
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_icici_savings_helpers.py -v
```

Expected: all 24 PASS (19 prior + 5 new).

- [ ] **Step 5: Commit**

```bash
git add skills/finance/ingestion/parsers/icici_savings.py tests/test_icici_savings_helpers.py
git commit -m "feat(savings): _parse_total_row + _aggregate_totals for validator inputs"
```

---

## Task 8: Full `parse()` integration — decrypt + extract + assemble + aggregate

**Files:**
- Modify: `skills/finance/ingestion/parsers/icici_savings.py`
- Create: `tests/test_icici_savings_parser.py`

This stitches all the helpers together into the public `parse(pdf_path, password)` function and runs against the real fixtures.

- [ ] **Step 1: Write the parser-version + scaffolding tests (cheap, no fixture)**

Create `tests/test_icici_savings_parser.py`:

```python
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

FIXTURE_FEB = Path(__file__).parent / "golden_fixtures" / "icici_savings_2026_02.pdf"
FIXTURE_JAN = Path(__file__).parent / "golden_fixtures" / "icici_savings_2026_01.pdf"


def _password():
    """Reuse the icici_cc password (per spec note: same credential value)."""
    from skills.finance.ingestion._common import password_lookup
    return password_lookup("icici_cc")


def test_savings_parser_version_via_parse_entrypoint():
    from skills.finance.ingestion.parsers.icici_savings import __parser_version__
    assert __parser_version__ == "icici-savings-pdf/v1"


@pytest.mark.skipif(not FIXTURE_FEB.exists(), reason="ICICI Savings Feb fixture missing")
def test_parse_feb_returns_nonempty_parseresult():
    from skills.finance.ingestion.parsers.icici_savings import parse
    result = parse(FIXTURE_FEB, _password())
    assert result.parser_version == "icici-savings-pdf/v1"
    assert len(result.pdf_content_hash) == 64
    assert len(result.rows) > 30                     # Feb fixture had ~43 txns
    assert "total_spends" in result.declared_totals
    assert "total_credits" in result.declared_totals
    assert "closing_balance" in result.declared_totals


@pytest.mark.skipif(not FIXTURE_FEB.exists(), reason="ICICI Savings Feb fixture missing")
def test_savings_ordinals_contiguous_1_to_n():
    """CLAUDE.md test invariant — ordinals 1..N contiguous in result.rows."""
    from skills.finance.ingestion.parsers.icici_savings import parse
    result = parse(FIXTURE_FEB, _password())
    ordinals = [r.source_row_ordinal for r in result.rows]
    assert ordinals == list(range(1, len(result.rows) + 1))


@pytest.mark.skipif(not FIXTURE_FEB.exists(), reason="ICICI Savings Feb fixture missing")
def test_savings_validator_passes_feb():
    """Page-subtotal aggregation: extracted in/out matches declared in/out."""
    from skills.finance.ingestion.parsers.icici_savings import parse
    from skills.finance.ingestion.statement_validator import validate
    result = parse(FIXTURE_FEB, _password())
    val = validate(result)
    assert val.ok, (
        f"Feb savings validator failed: delta_in={val.delta_in}, "
        f"delta_out={val.delta_out}. Declared: in={val.declared_in} out={val.declared_out}; "
        f"Extracted: in={val.extracted_in} out={val.extracted_out}."
    )


@pytest.mark.skipif(not FIXTURE_JAN.exists(), reason="ICICI Savings Jan fixture missing")
def test_savings_validator_passes_jan():
    """Same validator check for the Jan fixture (proves format stability)."""
    from skills.finance.ingestion.parsers.icici_savings import parse
    from skills.finance.ingestion.statement_validator import validate
    result = parse(FIXTURE_JAN, _password())
    val = validate(result)
    assert val.ok, f"Jan delta_in={val.delta_in}, delta_out={val.delta_out}"


@pytest.mark.skipif(not FIXTURE_FEB.exists(), reason="ICICI Savings Feb fixture missing")
def test_savings_upi_rows_flagged_not_dropped():
    """D1: UPI rows appear in result.rows (validator-friendly) but NOT in
    result.insertable_rows() (insert-time skip)."""
    from skills.finance.ingestion.parsers.icici_savings import parse
    result = parse(FIXTURE_FEB, _password())
    upi_rows = [r for r in result.rows if r.is_upi_skip]
    assert len(upi_rows) > 0, "Expected at least some UPI rows in Feb fixture"
    insertable = result.insertable_rows()
    assert all(r not in insertable for r in upi_rows)
    assert len(insertable) == len(result.rows) - len(upi_rows)


@pytest.mark.skipif(not FIXTURE_FEB.exists(), reason="ICICI Savings Feb fixture missing")
def test_savings_txn_mode_populated_for_non_upi():
    """Every non-UPI row should have a non-NULL txn_mode (NEFT, IMPS, ATM, etc.)."""
    from skills.finance.ingestion.parsers.icici_savings import parse
    result = parse(FIXTURE_FEB, _password())
    non_upi = [r for r in result.rows if not r.is_upi_skip]
    assert len(non_upi) > 0
    for r in non_upi:
        assert r.txn_mode is not None and r.txn_mode != "", \
            f"non-UPI row at ordinal {r.source_row_ordinal} has empty txn_mode"


@pytest.mark.skipif(not FIXTURE_FEB.exists(), reason="ICICI Savings Feb fixture missing")
def test_savings_parsed_row_fields_well_formed():
    from skills.finance.ingestion.parsers.icici_savings import parse
    result = parse(FIXTURE_FEB, _password())
    for row in result.rows:
        assert row.amount > Decimal("0"), \
            f"non-positive amount at ordinal {row.source_row_ordinal}"
        assert row.direction in ("in", "out")
        assert row.raw_merchant.strip()
        assert row.source_row_ordinal >= 1
```

- [ ] **Step 2: Run the parser-version test (only one that should pass without `parse()` defined)**

```bash
.venv/bin/python -m pytest tests/test_icici_savings_parser.py::test_savings_parser_version_via_parse_entrypoint -v
```

Expected: PASS (just imports the constant).

- [ ] **Step 3: Run all the fixture-dependent tests — should fail because parse() doesn't exist yet**

```bash
.venv/bin/python -m pytest tests/test_icici_savings_parser.py -v 2>&1 | tail -15
```

Expected: 7 FAILs — `module 'icici_savings' has no attribute 'parse'`.

- [ ] **Step 4: Implement `parse()` in `icici_savings.py`**

Append to `icici_savings.py`:

```python
import tempfile

import pikepdf
import pdfplumber

from skills.finance.ingestion._common import ParseResult


def parse(pdf_path: Path, password: str) -> ParseResult:
    """Decrypt the ICICI Savings PDF and extract rows + page-subtotal totals.

    Steps:
      1. Hash the (still-encrypted) source PDF for stable import_hash.
      2. Decrypt to a temp file via pikepdf.
      3. pdfplumber-extract text from every page; flatten into a list of lines.
      4. _assemble_rows produces ParsedRows with multi-line PARTICULARS handling
         and UPI-skip flagging.
      5. _aggregate_totals scans the same lines for 'Total:' rows and sums
         them across pages → declared_totals.
    """
    pdf_path = Path(pdf_path)
    pdf_content_hash = _sha256_file(pdf_path)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        with pikepdf.open(pdf_path, password=password) as src:
            src.save(tmp.name)

        all_lines: list[str] = []
        with pdfplumber.open(tmp.name) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                all_lines.extend(text.splitlines())

    rows = _assemble_rows(all_lines)
    declared_totals = _aggregate_totals(all_lines)

    return ParseResult(
        rows=rows,
        declared_totals=declared_totals,
        pdf_content_hash=pdf_content_hash,
        parser_version=__parser_version__,
    )
```

- [ ] **Step 5: Run all parser tests against real fixtures**

```bash
.venv/bin/python -m pytest tests/test_icici_savings_parser.py -v
```

Expected: all 8 PASS. If validator deltas are non-zero, the regex `_ROW_RE` may need tuning for the actual fixture's exact formatting — common fix is allowing additional whitespace tolerance OR updating the MODE-token character class (e.g. include `:` for `INT.PD:` variants).

- [ ] **Step 6: Run the full test suite to confirm no regressions**

```bash
.venv/bin/python -m pytest tests/ 2>&1 | tail -5
```

Expected: all PASS (existing W1-W3.2 tests + new W3.4 tests).

- [ ] **Step 7: Run lint**

```bash
.venv/bin/python -m ruff check .
```

Expected: All checks passed!

- [ ] **Step 8: Commit**

```bash
git add skills/finance/ingestion/parsers/icici_savings.py tests/test_icici_savings_parser.py
git commit -m "feat(savings): parse() integration — decrypt + extract + assemble + aggregate"
```

---

## Task 9: Wire savings into `folder_watcher.py` dispatch

**Files:**
- Modify: `skills/finance/ingestion/folder_watcher.py`

- [ ] **Step 1: Update ACCOUNT_IDS and EXPECTED_EXTENSION**

In `skills/finance/ingestion/folder_watcher.py`, locate the `ACCOUNT_IDS` and `EXPECTED_EXTENSION` dicts. Replace the existing definitions with:

```python
ACCOUNT_IDS: dict[str, UUID] = {
    "icici_cc":      UUID("10000000-0000-0000-0000-000000000003"),
    "amex_cc":       UUID("10000000-0000-0000-0000-000000000005"),
    "paytm_upi":     UUID("10000000-0000-0000-0000-000000000006"),
    "icici_savings": UUID("10000000-0000-0000-0000-000000000001"),
}

EXPECTED_EXTENSION: dict[str, str] = {
    "icici_cc":      ".pdf",
    "amex_cc":       ".xlsx",
    "paytm_upi":     ".xlsx",
    "icici_savings": ".pdf",
}
```

- [ ] **Step 2: Add the savings dispatch branch**

Inside `dispatch_to_parser`, after the existing `elif bank == "paytm_upi":` block, add:

```python
    elif bank == "icici_savings":
        from skills.finance.ingestion.parsers.icici_savings import parse as savings_parse
        password = await asyncio.to_thread(password_lookup, "icici_savings", "1896")
        parse_result = await asyncio.to_thread(savings_parse, file_path, password)
        source = SourceMeta(source="manual_pdf", source_ref=file_path.name)
```

(Note: the `password_lookup` import should already exist at the top of the file from the ICICI CC dispatch branch. Confirm it does; if missing, add `from skills.finance.ingestion._common import password_lookup` to the top.)

- [ ] **Step 3: Run the full test suite**

```bash
.venv/bin/python -m pytest tests/ 2>&1 | tail -5
```

Expected: all PASS, no regression.

- [ ] **Step 4: Run lint**

```bash
.venv/bin/python -m ruff check skills/finance/ingestion/folder_watcher.py
```

Expected: All checks passed!

- [ ] **Step 5: Commit**

```bash
git add skills/finance/ingestion/folder_watcher.py
git commit -m "feat(folder_watcher): dispatch icici_savings → icici_savings.parse"
```

---

## Task 10: Pipeline — include `txn_mode` in insert dict

**Files:**
- Modify: `skills/finance/ingestion/pipeline.py`

- [ ] **Step 1: Update `_build_insert_row`**

In `pipeline.py`, locate `_build_insert_row` and update its return-dict to include `txn_mode`:

```python
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
        "category_hint": r.category_hint,    # W3.1: Paytm-only; NULL for others
        "txn_mode": r.txn_mode,              # W3.4: ICICI Savings-only; NULL for others
    }
```

The iteration already uses `parse_result.insertable_rows()` (set in W3.1). No change needed there — the W3.4 `is_upi_skip` filter is automatic via the helper extension in Task 2.

- [ ] **Step 2: Run all tests**

```bash
.venv/bin/python -m pytest tests/ 2>&1 | tail -5
```

Expected: all PASS. Existing AMEX / ICICI CC / Paytm rows write `txn_mode=None`; savings rows write the actual MODE.

- [ ] **Step 3: Commit**

```bash
git add skills/finance/ingestion/pipeline.py
git commit -m "feat(pipeline): include txn_mode in transactions insert dict"
```

---

## Task 11: Apply migration to Supabase + lint/typecheck/test green

**Files:**
- Run: `migrations/007_txn_mode.sql` against Supabase

- [ ] **Step 1: Apply the migration via psql**

```bash
cd "/Users/rajat/AntiGravity/Personal finance Agent"
DB_URL=$(grep '^SUPABASE_DB_URL=' .env | cut -d= -f2- | tr -d '"' | tr -d "'")
psql "$DB_URL" -f migrations/007_txn_mode.sql
```

Expected output: `ALTER TABLE` and `COMMENT` confirmations (matches the W3.1 migration 005 application pattern).

- [ ] **Step 2: Verify column reachable**

```bash
.venv/bin/python -c "
from skills.finance.lib.db import service_client
c = service_client()
r = c.table('transactions').select('txn_mode').limit(1).execute()
print('txn_mode column reachable:', r.data is not None)
"
```

Expected: `txn_mode column reachable: True`.

- [ ] **Step 3: Run all three project checks**

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy skills scripts app.py
.venv/bin/python -m pytest -v
```

Expected: all PASS. If mypy complains about untyped pdfplumber/pikepdf, the existing `[[tool.mypy.overrides]]` block in `pyproject.toml` already covers them — no fix needed unless a new error surfaces.

If a `dict[str, Any]` annotation is needed on a pandas-touching variable in the parser (per `tasks/lessons.md` 2026-04-26 entry), apply the cast pattern locally.

- [ ] **Step 4: Commit any cleanup if needed**

```bash
git add -A
git commit -m "chore: lint/typecheck cleanup for W3.4"
```

(Skip if nothing required fixing.)

---

## Task 12: User-action gate — add `icici_savings_1896` credential entry

**Files:**
- Modify: `credentials.yaml` (gitignored — user edits directly)

This is the only manual step. The implementation cannot execute steps 1–11 then proceed to backfill in Task 13 without the credential entry being present, because the dispatch branch in Task 9 calls `password_lookup("icici_savings", "1896")`.

- [ ] **Step 1: Confirm with user**

Pause and ask the user:

> "Task 12 is a manual gate: please add an `icici_savings_1896` entry to your gitignored `credentials.yaml`, copying the value from your existing `icici_cc_<last4>` entry (since you confirmed the password is the same). Reply 'done' when added."

- [ ] **Step 2: Verify the credential is reachable**

After user confirms, run a non-decrypting smoke check:

```bash
.venv/bin/python -c "
from skills.finance.ingestion._common import password_lookup
try:
    pw = password_lookup('icici_savings', last4='1896')
    print('icici_savings_1896 found, length:', len(pw))
except Exception as e:
    print('FAILED:', type(e).__name__, str(e))
"
```

Expected: `icici_savings_1896 found, length: <N>` for some N. If FAILED, surface the error to the user; cannot proceed to Task 13.

- [ ] **Step 3: No commit — `credentials.yaml` is gitignored**

---

## Task 13: Backfill — restart launchd app, drop a fixture into inbox, verify ingestion

**Files:**
- Move: golden fixture → `~/finance-inbox/`

- [ ] **Step 1: Restart the launchd app**

```bash
launchctl kickstart -k gui/$(id -u)/com.rajat.pfa.app
sleep 5
launchctl print gui/$(id -u)/com.rajat.pfa.app | grep -E "pid|state" | head -3
tail -20 /Users/rajat/finance-logs/app.stdout.log
```

Expected: new pid, `scheduler started; jobs=[...]`, `folder_watcher started on /Users/rajat/finance-inbox`. No tracebacks.

- [ ] **Step 2: Copy the Feb fixture into the inbox**

```bash
cp "/Users/rajat/AntiGravity/Personal finance Agent/tests/golden_fixtures/icici_savings_2026_02.pdf" \
   "/Users/rajat/finance-inbox/icici_savings_2026_02.pdf"
sleep 30
tail -30 /Users/rajat/finance-logs/app.stdout.log
```

Expected log lines:
- `INFO ingested NN rows from manual_pdf/icici_savings_2026_02.pdf (validator ok, MM total)` — NN ≤ MM (UPI rows kept in MM but skipped at insert).
- A Telegram message arrives confirming totals match.

- [ ] **Step 3: Verify DB state**

```bash
.venv/bin/python -c "
from skills.finance.lib.db import service_client
c = service_client()
log = c.table('ingestion_log').select('*').order('timestamp', desc=True).limit(2).execute().data
for r in log:
    print(f\"  {str(r.get('timestamp'))[:19]}  {r.get('source_ref','-'):40} status={r.get('status'):20} rows_added={r.get('rows_added')}\")
print()
sav_uuid = '10000000-0000-0000-0000-000000000001'
n = c.table('transactions').select('id', count='exact').eq('account_id', sav_uuid).execute()
print(f'ICICI Savings rows in transactions: {n.count}')
modes = c.table('transactions').select('txn_mode').eq('account_id', sav_uuid).execute().data
mode_counts = {}
for r in modes:
    mode_counts[r['txn_mode']] = mode_counts.get(r['txn_mode'], 0) + 1
print('  txn_mode breakdown:')
for m, n in sorted(mode_counts.items(), key=lambda x: -x[1]):
    print(f'    {m!r:12} {n}')
"
```

Expected:
- Top log row: `icici_savings_2026_02.pdf  status=success  rows_added=NN` (NN typically 30-40 for Feb).
- `ICICI Savings rows in transactions: NN`.
- `txn_mode breakdown` showing distribution across `NEFT`, `IMPS`, `ATM`, `BIL/PAY`, `SAL`, etc. **No 'UPI' in the breakdown** (UPI rows skipped at insert).

- [ ] **Step 4: Idempotency check — re-drop the file**

```bash
mv "/Users/rajat/finance-inbox/icici_savings_2026_02.pdf" /tmp/
sleep 2
mv /tmp/icici_savings_2026_02.pdf "/Users/rajat/finance-inbox/"
sleep 30
.venv/bin/python -c "
from skills.finance.lib.db import service_client
c = service_client()
log = c.table('ingestion_log').select('*').order('timestamp', desc=True).limit(2).execute().data
for r in log:
    print(f\"  {str(r.get('timestamp'))[:19]}  {r.get('source_ref','-'):40} status={r.get('status'):20} rows_added={r.get('rows_added')}\")
"
```

Expected: most-recent row has `status=skipped_duplicate, rows_added=0`.

- [ ] **Step 5: (Optional) Drop the Jan fixture too**

```bash
cp "/Users/rajat/AntiGravity/Personal finance Agent/tests/golden_fixtures/icici_savings_2026_01.pdf" \
   "/Users/rajat/finance-inbox/icici_savings_2026_01.pdf"
sleep 30
```

Confirms the parser handles both fixture months. Verify another `success` row in `ingestion_log`.

- [ ] **Step 6: Push everything**

```bash
git push origin main
```

Expected: clean push.

---

## Self-Review

After writing the complete plan, I checked it against the spec:

**1. Spec coverage:**

- §1 Goal (~711 rows ingested, both validators pass, idempotent re-drop) → covered by Task 13 verification + Task 8 fixture tests.
- §2 Scope (in/deferred) → matches the file map and task list.
- §3 Architecture diagram → realized end-to-end across Tasks 8 (parse), 9 (folder_watcher dispatch), 10 (pipeline insert).
- §4 File structure → matches the file map exactly.
- §5 Source layer (detect_bank, dispatch, ACCOUNT_IDS, EXPECTED_EXTENSION, credentials) → Tasks 3, 9, 12.
- §6 Parse layer (parser_version, decryption, table extraction, multi-line, MODE handling, UPI-skip) → Tasks 4, 5, 6, 7, 8.
- §7 Validate layer (page-subtotal aggregation, flag-don't-drop) → Task 7 (`_aggregate_totals` returns the existing-validator-friendly dict shape) + Task 8 (`test_savings_validator_passes_*`).
- §8 Persist layer (migration, hash mode, pipeline behavior, ParsedRow extensions) → Tasks 1, 2, 10, 11.
- §9 Telegram review flow → existing `_send_summary` already plain text; no parser change. Verified in Task 13 step 2.
- §10 Backfill mechanics → Task 13.
- §11 Error handling matrix → covered by the existing pipeline log paths + new `_aggregate_totals` raising on missing Total: rows + new tests for `_extract_data_row` zero-amount + ROW_RE both-non-zero.
- §12 Testing strategy (8 fixture tests + 5 helper tests) → Tasks 4, 5, 6, 7, 8 cover all listed tests.
- §15 Open implementation risks → addressed by:
  - Risk: camelot vs pdfplumber → resolved by anchor-regex approach (no camelot needed; cleaner than spec considered).
  - Risk: MODE column unreliable → OR-logic on PARTICULARS catches UPI even if MODE missed (Task 4 + 5).
  - Risk: Multi-line PARTICULARS mis-counted → Task 6 explicit handling + tests.
  - Risk: Re-issued statements → existing Mode B + W4.3 deferred.
  - Risk: is_upi_skip false-positive → Tasks 4 tests cover NEFT/ATM not flagged.
- §16 Acceptance criteria (9 items) → all map to Tasks 8, 11, 13.
- §17 Routing-spec amendment → already landed in commit `6218835`; no new task needed.

**2. Placeholder scan:** No "TBD" / "TODO" / "implement later". Every code block contains complete code. Every command is exact.

**3. Type consistency:** `is_upi_skip`, `txn_mode`, `_extract_data_row`, `_assemble_rows`, `_parse_total_row`, `_aggregate_totals`, `classify_upi_skip`, `_parse_savings_date`, `_sha256_file` — all named identically across Tasks 2, 4, 5, 6, 7, 8 (definition) and Task 9, 10 (consumption). `parse(pdf_path: Path, password: str) -> ParseResult` signature consistent.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-30-icici-savings-implementation.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Each task has clean I/O (test→implement→commit) so subagent context per task is naturally bounded.

**2. Inline Execution** — Execute tasks in this session using the executing-plans skill. Batch execution with checkpoints. Useful when later tasks may discover real-fixture surprises requiring earlier-task adjustments (we saw this with Paytm — 6 prefix variants surfaced from real data that needed mid-execution iteration).

For this plan I lean **option 2 (inline)**, same reasoning as the Paytm execution:
- Task 5/6/8 may surface real-fixture quirks (regex tuning, multi-line variants we haven't seen) requiring iteration on earlier tasks before later ones run cleanly.
- Task 12 is a user-action gate that fits the inline checkpoint pattern naturally.

**Which approach? 1 or 2?**
