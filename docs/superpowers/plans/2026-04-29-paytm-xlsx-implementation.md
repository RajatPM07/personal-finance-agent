# Paytm UPI XLSX Ingestion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest a 12-month Paytm UPI XLSX statement (~711 rows after 2 AMEX-routed rows skipped at insert) end-to-end through the existing folder-watcher → parser → validator → pipeline → Telegram-summary path, populating a new `category_hint` column from Paytm's pre-tagged categories.

**Architecture:** Mirror the AMEX XLSX parser pattern. Add one new parser file, three small additions to `_common.py` (Bank literal, ParsedRow optional fields, ParseResult.insertable_rows method), one schema migration, and minor wires in `folder_watcher.py` and `pipeline.py`. Validator stays unchanged — Paytm parser pre-adjusts `declared_totals` at parse time so existing math closes.

**Tech Stack:** Python 3.11, `pandas` + `openpyxl` (already pinned), `pytest` + `pytest-asyncio`, Supabase Postgres.

**Spec reference:** `docs/superpowers/specs/2026-04-29-paytm-xlsx-ingestion-design.md`

---

## File map (locked decomposition)

| Action | Path | Responsibility |
|---|---|---|
| Create | `migrations/005_category_hint.sql` | one-column ALTER TABLE on `transactions` |
| Modify | `skills/finance/ingestion/_common.py` | extend `Bank` literal, add 3 optional fields to `ParsedRow`, add `insertable_rows()` method on `ParseResult`, add `"paytm"` token detection to `detect_bank_from_filename` |
| Create | `skills/finance/ingestion/parsers/paytm_upi.py` | the parser itself |
| Modify | `skills/finance/ingestion/folder_watcher.py` | add `paytm_upi` to `ACCOUNT_IDS` + `EXPECTED_EXTENSION` + `dispatch_to_parser` |
| Modify | `skills/finance/ingestion/pipeline.py` | iterate `insertable_rows()` instead of `rows`; include `category_hint` in insert dict |
| Create | `tests/test_paytm_upi_parser.py` | golden-file tests against `tests/golden_fixtures/paytm_upi_apr25_mar26.xlsx` |
| Create | `tests/test_paytm_self_transfer.py` | pure-fn unit tests for the self-transfer classifier |
| Modify | `tests/test_ingestion_common.py` | regression test that existing ICICI/AMEX detection still works after Paytm token added |

---

## Task 1: Schema migration `005_category_hint.sql`

**Files:**
- Create: `migrations/005_category_hint.sql`

- [ ] **Step 1: Create the migration file**

```sql
-- 005_category_hint.sql
-- W3.1 Paytm parser populates this column from the Tags column in Paytm's XLSX.
-- ICICI/AMEX rows leave this NULL. W5 normalization layer treats this as a
-- strong prior (not the final answer) — see roadmap r2 §4.

ALTER TABLE transactions
  ADD COLUMN category_hint TEXT;

COMMENT ON COLUMN transactions.category_hint IS
  'External pre-categorization (e.g. Paytm Tags). W5 normalization treats as strong prior, not final answer.';
```

- [ ] **Step 2: Commit (the migration is run later in Task 13 once the code that writes to it is ready)**

```bash
git add migrations/005_category_hint.sql
git commit -m "migration(005): add transactions.category_hint for W3.1 Paytm parser"
```

---

## Task 2: Extend `_common.py` — Bank literal, ParsedRow fields, ParseResult method

**Files:**
- Modify: `skills/finance/ingestion/_common.py:22` (Bank literal)
- Modify: `skills/finance/ingestion/_common.py:34-41` (ParsedRow fields)
- Modify: `skills/finance/ingestion/_common.py:43-54` (ParseResult method)
- Test: `tests/test_ingestion_common.py` (regression + new tests)

- [ ] **Step 1: Write failing tests first**

Append to `tests/test_ingestion_common.py`:

```python
def test_bank_literal_includes_paytm_upi():
    """Paytm UPI added in W3.1 — type-level guard against typos."""
    from typing import get_args
    from skills.finance.ingestion._common import Bank
    assert "paytm_upi" in get_args(Bank)
    assert "icici_cc" in get_args(Bank)
    assert "amex_cc" in get_args(Bank)


def test_parsed_row_accepts_optional_paytm_fields():
    """New optional fields default safely so ICICI/AMEX construction is unchanged."""
    from datetime import date
    from decimal import Decimal
    from skills.finance.ingestion._common import ParsedRow

    # Minimal construction (existing parsers' usage) still works:
    r = ParsedRow(
        txn_date=date(2026, 4, 29),
        amount=Decimal("100"),
        direction="out",
        raw_merchant="Test",
        source_row_ordinal=1,
    )
    assert r.is_amex_routed is False
    assert r.is_self_transfer is False
    assert r.category_hint is None

    # Paytm-style construction sets the new fields:
    r2 = ParsedRow(
        txn_date=date(2026, 4, 29),
        amount=Decimal("500"),
        direction="out",
        raw_merchant="Some Merchant",
        source_row_ordinal=2,
        is_amex_routed=True,
        is_self_transfer=False,
        category_hint="Food",
    )
    assert r2.is_amex_routed is True
    assert r2.category_hint == "Food"


def test_parse_result_insertable_rows_excludes_amex_routed():
    from datetime import date
    from decimal import Decimal
    from skills.finance.ingestion._common import ParsedRow, ParseResult

    keep = ParsedRow(
        txn_date=date(2026, 4, 29), amount=Decimal("100"), direction="out",
        raw_merchant="Keep", source_row_ordinal=1,
    )
    drop = ParsedRow(
        txn_date=date(2026, 4, 29), amount=Decimal("200"), direction="out",
        raw_merchant="Drop", source_row_ordinal=2, is_amex_routed=True,
    )

    pr = ParseResult(
        rows=[keep, drop],
        declared_totals={"total_spends": Decimal("100"), "total_credits": Decimal("0"),
                         "closing_balance": None, "_derived_from_rows": False},
        pdf_content_hash="0" * 64,
        parser_version="test/v1",
    )
    insertable = pr.insertable_rows()
    assert len(insertable) == 1
    assert insertable[0].raw_merchant == "Keep"
```

- [ ] **Step 2: Run the failing tests**

```bash
.venv/bin/python -m pytest tests/test_ingestion_common.py::test_bank_literal_includes_paytm_upi tests/test_ingestion_common.py::test_parsed_row_accepts_optional_paytm_fields tests/test_ingestion_common.py::test_parse_result_insertable_rows_excludes_amex_routed -v
```

Expected: 3 FAILs (Bank doesn't have paytm_upi; ParsedRow rejects unknown kwargs; ParseResult has no insertable_rows method).

- [ ] **Step 3: Update `Bank` literal in `_common.py:22`**

Change:
```python
Bank = Literal["icici_cc", "amex_cc"]
```
To:
```python
Bank = Literal["icici_cc", "amex_cc", "paytm_upi"]
```

- [ ] **Step 4: Add three optional fields to `ParsedRow` (`_common.py:34-41`)**

Replace the existing `ParsedRow` definition with:

```python
@dataclass(frozen=True)
class ParsedRow:
    txn_date: date
    amount: Decimal                       # always positive; sign info on `direction`
    direction: Literal["in", "out"]
    raw_merchant: str
    source_row_ordinal: int                # 1..N within the file, deterministic per parser
    # Paytm-only fields (W3.1). ICICI/AMEX rows leave these at defaults.
    is_amex_routed: bool = False           # True → row is dropped at insert (D1)
    is_self_transfer: bool = False         # True → row is excluded from validator paid-sum logic (D2)
    category_hint: str | None = None       # Paytm's pre-tagged category, emoji-stripped (D4)
```

- [ ] **Step 5: Add `insertable_rows()` method to `ParseResult`**

Replace the existing `ParseResult` definition with:

```python
@dataclass(frozen=True)
class ParseResult:
    rows: list[ParsedRow]
    declared_totals: dict                  # {'total_spends': Decimal, 'total_credits': Decimal,
                                           #  'closing_balance': Decimal | None,
                                           #  '_derived_from_rows': bool}
    pdf_content_hash: str                  # sha256 of source FILE bytes (PDF or XLSX)
    parser_version: str

    def insertable_rows(self) -> list[ParsedRow]:
        """Rows the pipeline should persist. Excludes is_amex_routed=True rows.
        Self-transfers ARE in the insertable set (D2: ingest all transactions
        even when excluded from the published-summary count).
        """
        return [r for r in self.rows if not r.is_amex_routed]
```

- [ ] **Step 6: Run tests; verify they pass**

```bash
.venv/bin/python -m pytest tests/test_ingestion_common.py -v
```

Expected: PASS for the 3 new tests + all existing pass (regression).

- [ ] **Step 7: Commit**

```bash
git add skills/finance/ingestion/_common.py tests/test_ingestion_common.py
git commit -m "feat(_common): extend Bank literal, ParsedRow optional fields, ParseResult.insertable_rows for W3.1"
```

---

## Task 3: Extend `detect_bank_from_filename` to recognize Paytm

**Files:**
- Modify: `skills/finance/ingestion/_common.py:74-96` (`detect_bank_from_filename`)
- Test: `tests/test_ingestion_common.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_ingestion_common.py`:

```python
def test_detect_bank_paytm_canonical():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("paytm_upi_apr25_mar26.xlsx") == "paytm_upi"


def test_detect_bank_paytm_loose():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("Paytm_UPI_Statement_2026.xlsx") == "paytm_upi"
    assert detect_bank_from_filename("my_paytm_export.xlsx") == "paytm_upi"


def test_detect_bank_paytm_does_not_break_existing():
    """Regression: ICICI + AMEX detection unchanged."""
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("icici_cc_2026_04.pdf") == "icici_cc"
    assert detect_bank_from_filename("amex_cc_2026_04.xlsx") == "amex_cc"


def test_detect_bank_ambiguous_paytm_plus_icici_returns_none():
    """If a filename mentions both Paytm and ICICI it's ambiguous — let the
    user disambiguate via the Telegram inline keyboard."""
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("paytm_icici_combined.xlsx") is None
```

- [ ] **Step 2: Run failing tests**

```bash
.venv/bin/python -m pytest tests/test_ingestion_common.py::test_detect_bank_paytm_canonical tests/test_ingestion_common.py::test_detect_bank_paytm_loose tests/test_ingestion_common.py::test_detect_bank_paytm_does_not_break_existing tests/test_ingestion_common.py::test_detect_bank_ambiguous_paytm_plus_icici_returns_none -v
```

Expected: 4 FAILs (function returns None for paytm; ambiguous case maybe wrong).

- [ ] **Step 3: Update `detect_bank_from_filename` in `_common.py`**

Replace the existing function body with:

```python
def detect_bank_from_filename(filename: str) -> Bank | None:
    """Pure function. Lowercase + word-boundary token match.

    Tokenizes the filename on non-alphanumerics so 'cc' must appear as a
    standalone token (NOT as a substring of words like 'account' which
    contains 'cc' as a bigram).

    Returns:
      'icici_cc'  if filename has 'icici' AND 'cc' tokens.
      'amex_cc'   if filename has 'amex' OR 'american' tokens.
      'paytm_upi' if filename has 'paytm' token.
      None        if multiple bank tokens collide (ambiguous), or none match.
    """
    name = filename.lower()
    tokens = set(re.split(r"[^a-z0-9]+", name))
    has_icici = "icici" in tokens
    has_amex = ("amex" in tokens) or ("american" in tokens)
    has_paytm = "paytm" in tokens

    matches = sum([has_icici, has_amex, has_paytm])
    if matches > 1:
        return None  # ambiguous

    if has_icici and ("cc" in tokens):
        return "icici_cc"
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

Expected: all PASS (new + existing regression).

- [ ] **Step 5: Commit**

```bash
git add skills/finance/ingestion/_common.py tests/test_ingestion_common.py
git commit -m "feat(_common): detect_bank_from_filename recognizes 'paytm' token"
```

---

## Task 4: Self-transfer classifier — pure-fn unit

**Files:**
- Create: `skills/finance/ingestion/parsers/paytm_upi.py` (initial scaffold + classifier)
- Create: `tests/test_paytm_self_transfer.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_paytm_self_transfer.py`:

```python
def test_money_sent_to_with_own_handle_is_self_transfer():
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    assert classify_self_transfer(
        transaction_details="Money sent to Rajat Sharma",
        other_transaction_details="UPI Ref ABC / 7358467199@ptsbi",
        own_handles=own,
    ) is True


def test_money_sent_to_other_handle_is_not_self_transfer():
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    assert classify_self_transfer(
        transaction_details="Money sent to Md Mehboob",
        other_transaction_details="9123456789@upi",
        own_handles=own,
    ) is False


def test_paid_to_prefix_is_never_self_transfer():
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    # Even if the row has the own-handle in the UPI ID column (theoretical),
    # 'Paid to ...' is a merchant payment, not a self-transfer.
    assert classify_self_transfer(
        transaction_details="Paid to Munni Sharma",
        other_transaction_details="7358467199@ptsbi",
        own_handles=own,
    ) is False


def test_received_from_is_never_self_transfer():
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    assert classify_self_transfer(
        transaction_details="Received from Aayushi Shukla",
        other_transaction_details="7358467199@ptsbi",
        own_handles=own,
    ) is False


def test_empty_own_handles_returns_false():
    """If we have no own-handles configured, no row is a self-transfer.
    (This is the V1 fallback when accounts table has no UPI-typed rows yet.)"""
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    assert classify_self_transfer(
        transaction_details="Money sent to Anyone",
        other_transaction_details="some@upi",
        own_handles=[],
    ) is False


def test_none_inputs_handled():
    """NaN / None values from pandas show up as None in some rows; classifier
    must tolerate them without crashing."""
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    assert classify_self_transfer(
        transaction_details=None,
        other_transaction_details=None,
        own_handles=own,
    ) is False
```

- [ ] **Step 2: Run failing tests**

```bash
.venv/bin/python -m pytest tests/test_paytm_self_transfer.py -v
```

Expected: 6 FAILs (module doesn't exist).

- [ ] **Step 3: Create the parser file with the classifier**

Create `skills/finance/ingestion/parsers/paytm_upi.py`:

```python
"""Paytm UPI XLSX statement parser — deterministic.

Paytm exports an unencrypted .xlsx with two sheets:
  - 'Summary': declared totals + per-source-account breakdown
  - 'Passbook Payment History': transaction rows

Spec: docs/superpowers/specs/2026-04-29-paytm-xlsx-ingestion-design.md

Three Paytm-specific behaviors:
  D1: rows where Your Account = "American Express Credit Card" are flagged
      with is_amex_routed=True. They appear in result.rows (so the validator
      can verify we extracted everything Paytm reports), but pipeline drops
      them at insert via result.insertable_rows().
  D2: 'Money sent to ...' rows whose Other Transaction Details column
      contains a known own-handle are flagged is_self_transfer=True. They
      ARE inserted (audit trail) but the parser pre-adjusts declared_totals
      so the existing validator math closes (Summary excludes self-transfers
      from declared paid; we add their total back in).
  D4: Paytm's Tags column is captured into ParsedRow.category_hint with
      leading emoji stripped. NULL when the Tags column is empty.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__parser_version__ = "paytm-upi-xlsx/v1"


class ParserError(Exception):
    """Raised when the Paytm XLSX layout is unrecognized or row parsing fails."""


def classify_self_transfer(
    transaction_details: Any,
    other_transaction_details: Any,
    own_handles: list[str],
) -> bool:
    """Return True iff this row represents money sent from the user to themselves.

    Self-transfer = "Money sent to <person>" prefix AND the other-details column
    contains one of the user's own UPI handles. Paytm's Summary footnote says
    "Self transfer payments are not included" in declared paid total — so
    classifier results feed into the parser's declared_totals adjustment.
    """
    if not own_handles:
        return False
    if transaction_details is None or other_transaction_details is None:
        return False
    td = str(transaction_details)
    if not td.startswith("Money sent to "):
        return False
    other = str(other_transaction_details)
    return any(h in other for h in own_handles)
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_paytm_self_transfer.py -v
```

Expected: 6 PASSes.

- [ ] **Step 5: Commit**

```bash
git add skills/finance/ingestion/parsers/paytm_upi.py tests/test_paytm_self_transfer.py
git commit -m "feat(paytm): self-transfer classifier (pure fn) + parser scaffold"
```

---

## Task 5: Summary sheet reader — declared totals via label-scan

**Files:**
- Modify: `skills/finance/ingestion/parsers/paytm_upi.py` (add `_read_summary_totals`)
- Test: append to `tests/test_paytm_self_transfer.py` (rename to `tests/test_paytm_helpers.py` for clarity)

- [ ] **Step 1: Rename and extend the helper test file**

```bash
git mv tests/test_paytm_self_transfer.py tests/test_paytm_helpers.py
```

- [ ] **Step 2: Write failing tests**

Append to `tests/test_paytm_helpers.py`:

```python
import pandas as pd
from decimal import Decimal


def _build_synthetic_summary() -> pd.DataFrame:
    """Mirror the structure observed in the real Paytm Summary sheet
    (verified during 2026-04-26 inspection): declared totals at rows 9-12,
    addressable by label-scan rather than fixed-row indexing."""
    rows = [[None] * 5 for _ in range(15)]
    rows[8] = ["Paytm Statement for :", "Apr 2025 - Mar 2026", None, None, None]
    rows[9] = ["Money Paid (Amount in Rs.)", "1,23,456.78", None, None, None]
    rows[10] = ["Money Paid (No. of Payments)", 698, None, None, None]
    rows[11] = ["Money Received (Amount in Rs.)", "5,000.00", None, None, None]
    rows[12] = ["Money Received (No. of Payments)", 8, None, None, None]
    return pd.DataFrame(rows)


def test_read_summary_totals_extracts_paid_amount():
    from skills.finance.ingestion.parsers.paytm_upi import _read_summary_totals
    totals = _read_summary_totals(_build_synthetic_summary())
    assert totals["paid_amount"] == Decimal("123456.78")
    assert totals["paid_count"] == 698
    assert totals["recv_amount"] == Decimal("5000.00")
    assert totals["recv_count"] == 8


def test_read_summary_totals_handles_string_with_indian_separators():
    """Indian number format uses comma at thousand AND lakh boundaries
    (e.g. '1,23,456.78'). Parser must strip commas before Decimal."""
    from skills.finance.ingestion.parsers.paytm_upi import _read_summary_totals
    df = _build_synthetic_summary()
    df.iat[9, 1] = "12,34,567.89"
    totals = _read_summary_totals(df)
    assert totals["paid_amount"] == Decimal("1234567.89")


def test_read_summary_totals_raises_when_labels_missing():
    """If Paytm changes the Summary layout, fail loud rather than silently
    pass with zero totals."""
    from skills.finance.ingestion.parsers.paytm_upi import (
        ParserError, _read_summary_totals,
    )
    df = pd.DataFrame([["random", "data"], ["here", "no labels"]])
    import pytest
    with pytest.raises(ParserError) as exc_info:
        _read_summary_totals(df)
    assert "Money Paid" in str(exc_info.value) or "label" in str(exc_info.value).lower()
```

- [ ] **Step 3: Run failing tests**

```bash
.venv/bin/python -m pytest tests/test_paytm_helpers.py::test_read_summary_totals_extracts_paid_amount tests/test_paytm_helpers.py::test_read_summary_totals_handles_string_with_indian_separators tests/test_paytm_helpers.py::test_read_summary_totals_raises_when_labels_missing -v
```

Expected: 3 FAILs (function doesn't exist).

- [ ] **Step 4: Implement `_read_summary_totals`**

Append to `skills/finance/ingestion/parsers/paytm_upi.py`:

```python
from decimal import Decimal


def _decimal_from_indian_str(s: Any) -> Decimal:
    """Convert '1,23,456.78' or 1234.56 or Decimal(...) → Decimal('1234.56').
    Tolerates Indian-style multi-comma thousand/lakh separators."""
    if isinstance(s, Decimal):
        return s
    cleaned = str(s).replace(",", "").strip()
    return Decimal(cleaned)


_SUMMARY_LABELS = {
    "paid_amount": "Money Paid (Amount in Rs.)",
    "paid_count": "Money Paid (No. of Payments)",
    "recv_amount": "Money Received (Amount in Rs.)",
    "recv_count": "Money Received (No. of Payments)",
}


def _read_summary_totals(summary_df: "pd.DataFrame") -> dict:
    """Scan the Summary sheet for the four declared-total labels (label-scan
    rather than fixed-row indexing in case Paytm shifts the layout). Returns:
        {paid_amount: Decimal, paid_count: int,
         recv_amount: Decimal, recv_count: int}
    Raises ParserError if any of the four labels is not found.
    """
    found: dict = {}
    for i in range(len(summary_df)):
        label = summary_df.iat[i, 0]
        if label is None:
            continue
        label_str = str(label).strip()
        for key, target in _SUMMARY_LABELS.items():
            if key in found:
                continue
            if label_str == target:
                value = summary_df.iat[i, 1]
                if key.endswith("_amount"):
                    found[key] = _decimal_from_indian_str(value)
                else:  # _count
                    found[key] = int(value)
                break
    missing = [k for k in _SUMMARY_LABELS if k not in found]
    if missing:
        raise ParserError(
            f"Paytm Summary sheet missing expected labels: {missing}. "
            f"Looked for: {list(_SUMMARY_LABELS.values())}. "
            f"First 15 rows of column A: "
            f"{[summary_df.iat[i, 0] for i in range(min(15, len(summary_df)))]}"
        )
    return found
```

Also add the `import pandas as pd` at the top of `paytm_upi.py`:

```python
import pandas as pd
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/test_paytm_helpers.py -v
```

Expected: all PASS (the original 6 self-transfer tests + 3 new summary tests).

- [ ] **Step 6: Commit**

```bash
git add skills/finance/ingestion/parsers/paytm_upi.py tests/test_paytm_helpers.py
git commit -m "feat(paytm): _read_summary_totals via label-scan; tolerates Indian-format numbers"
```

---

## Task 6: Direction inference + row construction helper

**Files:**
- Modify: `skills/finance/ingestion/parsers/paytm_upi.py`
- Test: `tests/test_paytm_helpers.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_paytm_helpers.py`:

```python
def test_infer_direction_paid_to():
    from skills.finance.ingestion.parsers.paytm_upi import _infer_direction
    assert _infer_direction("Paid to NETC FASTag Recharge") == "out"


def test_infer_direction_money_sent_to():
    from skills.finance.ingestion.parsers.paytm_upi import _infer_direction
    assert _infer_direction("Money sent to Md Mehboob") == "out"


def test_infer_direction_received_from():
    from skills.finance.ingestion.parsers.paytm_upi import _infer_direction
    assert _infer_direction("Received from Aayushi Shukla") == "in"


def test_infer_direction_unknown_prefix_raises():
    from skills.finance.ingestion.parsers.paytm_upi import (
        ParserError, _infer_direction,
    )
    import pytest
    with pytest.raises(ParserError) as exc_info:
        _infer_direction("Refund processed for order 123")
    assert "unknown" in str(exc_info.value).lower() or "prefix" in str(exc_info.value).lower()


def test_strip_paytm_tag_emoji():
    """Paytm tags like '#🥘 Food' should produce 'Food' as category_hint."""
    from skills.finance.ingestion.parsers.paytm_upi import _strip_tag
    assert _strip_tag("#🥘 Food") == "Food"
    assert _strip_tag("#🛒 Groceries") == "Groceries"
    assert _strip_tag("#⛽️ Fuel") == "Fuel"
    assert _strip_tag("#💵 Money Transfer") == "Money Transfer"
    assert _strip_tag("#🔄 Miscellaneous") == "Miscellaneous"


def test_strip_paytm_tag_blank_or_none_returns_none():
    from skills.finance.ingestion.parsers.paytm_upi import _strip_tag
    assert _strip_tag(None) is None
    assert _strip_tag("") is None
    assert _strip_tag("   ") is None
```

- [ ] **Step 2: Run failing tests**

```bash
.venv/bin/python -m pytest tests/test_paytm_helpers.py::test_infer_direction_paid_to tests/test_paytm_helpers.py::test_infer_direction_money_sent_to tests/test_paytm_helpers.py::test_infer_direction_received_from tests/test_paytm_helpers.py::test_infer_direction_unknown_prefix_raises tests/test_paytm_helpers.py::test_strip_paytm_tag_emoji tests/test_paytm_helpers.py::test_strip_paytm_tag_blank_or_none_returns_none -v
```

Expected: 6 FAILs.

- [ ] **Step 3: Implement `_infer_direction` and `_strip_tag`**

Append to `skills/finance/ingestion/parsers/paytm_upi.py`:

```python
import re


_DIRECTION_PREFIXES: dict[str, str] = {
    "Paid to ": "out",
    "Money sent to ": "out",
    "Received from ": "in",
}


def _infer_direction(transaction_details: str) -> str:
    """Map the Transaction Details column prefix to direction. Raises if the
    prefix is unknown — Paytm's known prefix list is small and stable; an
    unknown prefix is a parser-update signal."""
    for prefix, direction in _DIRECTION_PREFIXES.items():
        if transaction_details.startswith(prefix):
            return direction
    raise ParserError(
        f"Unknown Paytm Transaction Details prefix: {transaction_details!r}. "
        f"Known prefixes: {list(_DIRECTION_PREFIXES.keys())}. "
        f"If Paytm added a new pattern, extend _DIRECTION_PREFIXES."
    )


# Match leading '#' + any emoji/symbol char(s) + optional whitespace.
# We strip the '#<emoji> ' prefix and keep the human-readable label.
_TAG_PREFIX_RE = re.compile(r"^#\S+\s*")


def _strip_tag(tag_value: Any) -> str | None:
    """Convert Paytm's '#🥘 Food' → 'Food'. Returns None for blank/None input."""
    if tag_value is None:
        return None
    s = str(tag_value).strip()
    if not s:
        return None
    stripped = _TAG_PREFIX_RE.sub("", s).strip()
    return stripped or None
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_paytm_helpers.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/finance/ingestion/parsers/paytm_upi.py tests/test_paytm_helpers.py
git commit -m "feat(paytm): _infer_direction + _strip_tag helpers"
```

---

## Task 7: AMEX-routed flag classifier

**Files:**
- Modify: `skills/finance/ingestion/parsers/paytm_upi.py`
- Test: `tests/test_paytm_helpers.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_paytm_helpers.py`:

```python
def test_is_amex_routed_true_for_amex():
    from skills.finance.ingestion.parsers.paytm_upi import _is_amex_routed
    assert _is_amex_routed("American Express Credit Card") is True


def test_is_amex_routed_false_for_other_accounts():
    from skills.finance.ingestion.parsers.paytm_upi import _is_amex_routed
    assert _is_amex_routed("HDFC Bank") is False
    assert _is_amex_routed("ICICI Bank") is False
    assert _is_amex_routed("UPI Linked Bank") is False
    assert _is_amex_routed("Other UPI Apps") is False


def test_is_amex_routed_handles_none():
    from skills.finance.ingestion.parsers.paytm_upi import _is_amex_routed
    assert _is_amex_routed(None) is False
    assert _is_amex_routed("") is False
```

- [ ] **Step 2: Run failing tests**

```bash
.venv/bin/python -m pytest tests/test_paytm_helpers.py::test_is_amex_routed_true_for_amex tests/test_paytm_helpers.py::test_is_amex_routed_false_for_other_accounts tests/test_paytm_helpers.py::test_is_amex_routed_handles_none -v
```

Expected: 3 FAILs.

- [ ] **Step 3: Implement `_is_amex_routed`**

Append to `skills/finance/ingestion/parsers/paytm_upi.py`:

```python
def _is_amex_routed(your_account: Any) -> bool:
    """A Paytm row's `Your Account` column tells which underlying source funded
    the payment. When the source is the user's AMEX CC, the same spend ALSO
    appears in the AMEX statement (via the generic 'Paytm' merchant). To avoid
    double-counting, the pipeline drops these rows at insert. Spec D1.
    """
    if your_account is None:
        return False
    return "American Express" in str(your_account)
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_paytm_helpers.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/finance/ingestion/parsers/paytm_upi.py tests/test_paytm_helpers.py
git commit -m "feat(paytm): _is_amex_routed classifier (D1 dual-entry skip)"
```

---

## Task 8: Own-handles loader from `accounts` table

**Files:**
- Modify: `skills/finance/ingestion/parsers/paytm_upi.py`
- Test: `tests/test_paytm_helpers.py`

- [ ] **Step 1: Write failing test (no live DB; mock the client)**

Append to `tests/test_paytm_helpers.py`:

```python
def test_load_own_upi_handles_filters_to_upi_type(monkeypatch):
    """Own UPI handles are loaded from accounts where type='upi'.
    The function must use the existing service_client + adb-equivalent
    sync path so it works inside Paytm parser's pandas thread."""
    from skills.finance.ingestion.parsers import paytm_upi

    # Fake supabase response: 2 UPI rows + 1 bank row should yield 2 handles.
    def fake_service_client():
        class _Resp:
            data = [
                {"identifier": "7358467199@ptsbi", "type": "upi"},
                {"identifier": "secondhandle@upi",  "type": "upi"},
            ]

        class _Builder:
            def select(self, *a, **kw): return self
            def eq(self, *a, **kw): return self
            def execute(self): return _Resp()

        class _Client:
            def table(self, name): return _Builder()

        return _Client()

    monkeypatch.setattr(paytm_upi, "service_client", fake_service_client)
    handles = paytm_upi._load_own_upi_handles()
    assert handles == ["7358467199@ptsbi", "secondhandle@upi"]


def test_load_own_upi_handles_empty_returns_empty_list(monkeypatch):
    from skills.finance.ingestion.parsers import paytm_upi

    def fake_service_client():
        class _Resp:
            data = []

        class _Builder:
            def select(self, *a, **kw): return self
            def eq(self, *a, **kw): return self
            def execute(self): return _Resp()

        class _Client:
            def table(self, name): return _Builder()

        return _Client()

    monkeypatch.setattr(paytm_upi, "service_client", fake_service_client)
    assert paytm_upi._load_own_upi_handles() == []
```

- [ ] **Step 2: Run failing tests**

```bash
.venv/bin/python -m pytest tests/test_paytm_helpers.py::test_load_own_upi_handles_filters_to_upi_type tests/test_paytm_helpers.py::test_load_own_upi_handles_empty_returns_empty_list -v
```

Expected: 2 FAILs (function not defined).

- [ ] **Step 3: Implement `_load_own_upi_handles`**

Add at the top of `skills/finance/ingestion/parsers/paytm_upi.py`:

```python
from skills.finance.lib.db import service_client
```

Append to the file:

```python
def _load_own_upi_handles() -> list[str]:
    """Read all UPI-typed account identifiers from the `accounts` table.

    Used by the parser to classify 'Money sent to ...' rows as self-transfers
    when the destination matches one of the user's own handles. Called inside
    parse() which already runs on a worker thread (parser dispatch in
    folder_watcher uses asyncio.to_thread), so calling the sync supabase
    client directly is safe — no `adb()` wrap needed here."""
    resp = (
        service_client()
        .table("accounts")
        .select("identifier,type")
        .eq("type", "upi")
        .execute()
    )
    return [r["identifier"] for r in (resp.data or []) if r.get("identifier")]
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_paytm_helpers.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/finance/ingestion/parsers/paytm_upi.py tests/test_paytm_helpers.py
git commit -m "feat(paytm): _load_own_upi_handles from accounts table (type='upi')"
```

---

## Task 9: `parse(file_path)` integration — declared-totals adjustment + row construction

**Files:**
- Modify: `skills/finance/ingestion/parsers/paytm_upi.py`
- Create: `tests/test_paytm_upi_parser.py`

- [ ] **Step 1: Write the parser-version + scaffolding tests (cheap, no fixture)**

Create `tests/test_paytm_upi_parser.py`:

```python
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "golden_fixtures" / "paytm_upi_apr25_mar26.xlsx"


def test_paytm_parser_version():
    from skills.finance.ingestion.parsers.paytm_upi import __parser_version__
    assert __parser_version__ == "paytm-upi-xlsx/v1"


@pytest.mark.skipif(not FIXTURE.exists(), reason="Paytm golden fixture not present")
def test_parse_returns_nonempty_parseresult(monkeypatch):
    """Smoke: real fixture parses, ParseResult has expected shape."""
    from skills.finance.ingestion.parsers import paytm_upi
    # Stub out the DB lookup so this test doesn't touch Supabase.
    monkeypatch.setattr(paytm_upi, "_load_own_upi_handles",
                        lambda: ["7358467199@ptsbi"])

    result = paytm_upi.parse(FIXTURE)
    assert result.parser_version == "paytm-upi-xlsx/v1"
    assert len(result.pdf_content_hash) == 64
    assert len(result.rows) > 700      # expect ~713
    assert "total_spends" in result.declared_totals
    assert "total_credits" in result.declared_totals


@pytest.mark.skipif(not FIXTURE.exists(), reason="Paytm golden fixture not present")
def test_paytm_amex_routed_rows_flagged_not_dropped(monkeypatch):
    """D1: AMEX-routed rows are in result.rows but NOT in insertable_rows."""
    from skills.finance.ingestion.parsers import paytm_upi
    monkeypatch.setattr(paytm_upi, "_load_own_upi_handles",
                        lambda: ["7358467199@ptsbi"])

    result = paytm_upi.parse(FIXTURE)
    amex_rows = [r for r in result.rows if r.is_amex_routed]
    assert len(amex_rows) == 2, "Summary said 2 AMEX-routed; parser must agree"
    assert all(r not in result.insertable_rows() for r in amex_rows)
    assert len(result.insertable_rows()) == len(result.rows) - 2


@pytest.mark.skipif(not FIXTURE.exists(), reason="Paytm golden fixture not present")
def test_paytm_ordinals_contiguous_1_to_n(monkeypatch):
    """CLAUDE.md test invariant: ordinals 1..N within result.rows."""
    from skills.finance.ingestion.parsers import paytm_upi
    monkeypatch.setattr(paytm_upi, "_load_own_upi_handles",
                        lambda: ["7358467199@ptsbi"])

    result = paytm_upi.parse(FIXTURE)
    ordinals = [r.source_row_ordinal for r in result.rows]
    assert ordinals == list(range(1, len(result.rows) + 1))


@pytest.mark.skipif(not FIXTURE.exists(), reason="Paytm golden fixture not present")
def test_paytm_validator_passes_on_real_fixture(monkeypatch):
    """The whole point of declared-totals adjustment: validator passes."""
    from skills.finance.ingestion.parsers import paytm_upi
    from skills.finance.ingestion.statement_validator import validate
    monkeypatch.setattr(paytm_upi, "_load_own_upi_handles",
                        lambda: ["7358467199@ptsbi"])

    result = paytm_upi.parse(FIXTURE)
    val = validate(result)
    assert val.ok, (
        f"Paytm validator failed: delta_in={val.delta_in}, "
        f"delta_out={val.delta_out}. If delta_out roughly equals the "
        f"self-transfer total, the own-handles list may be incomplete."
    )


@pytest.mark.skipif(not FIXTURE.exists(), reason="Paytm golden fixture not present")
def test_paytm_category_hint_populated(monkeypatch):
    """At least some rows should have category_hint set (Paytm tags most rows)."""
    from skills.finance.ingestion.parsers import paytm_upi
    monkeypatch.setattr(paytm_upi, "_load_own_upi_handles",
                        lambda: ["7358467199@ptsbi"])

    result = paytm_upi.parse(FIXTURE)
    hinted = [r for r in result.rows if r.category_hint is not None]
    assert len(hinted) > 500, "Expected most rows to be tagged by Paytm"


@pytest.mark.skipif(not FIXTURE.exists(), reason="Paytm golden fixture not present")
def test_paytm_parsed_row_fields_well_formed(monkeypatch):
    from skills.finance.ingestion.parsers import paytm_upi
    monkeypatch.setattr(paytm_upi, "_load_own_upi_handles",
                        lambda: ["7358467199@ptsbi"])

    result = paytm_upi.parse(FIXTURE)
    for row in result.rows:
        assert row.amount > Decimal("0"), \
            f"non-positive amount at ordinal {row.source_row_ordinal}"
        assert row.direction in ("in", "out")
        assert row.raw_merchant.strip(), "empty merchant"
        assert row.source_row_ordinal >= 1
```

- [ ] **Step 2: Run the parser-version test (the only one that should pass without parse() implemented)**

```bash
.venv/bin/python -m pytest tests/test_paytm_upi_parser.py::test_paytm_parser_version -v
```

Expected: PASS (just imports the constant).

- [ ] **Step 3: Run the fixture-dependent tests**

```bash
.venv/bin/python -m pytest tests/test_paytm_upi_parser.py -v
```

Expected: 6 FAILs ("module 'paytm_upi' has no attribute 'parse'" or AttributeError for missing parse).

- [ ] **Step 4: Implement `parse()` in `paytm_upi.py`**

Add at the top of `paytm_upi.py`:

```python
import hashlib
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

from skills.finance.ingestion._common import ParsedRow, ParseResult
```

Append to the file:

```python
def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_paytm_date(s: Any) -> date:
    """Paytm dates may arrive as datetime (Excel date cell) or as 'DD/MM/YYYY'
    string. Try datetime first; fall back to DD/MM/YYYY (Indian format)."""
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
    raise ParserError(f"Could not parse Paytm date: {s!r}")


def _strip_prefix(transaction_details: str) -> str:
    """Strip the 'Paid to ' / 'Money sent to ' / 'Received from ' prefix
    to get just the merchant/person name for `raw_merchant`."""
    for prefix in _DIRECTION_PREFIXES:
        if transaction_details.startswith(prefix):
            return transaction_details[len(prefix):].strip()
    return transaction_details


def parse(file_path: Path) -> ParseResult:
    """Parse a Paytm UPI XLSX export.

    Behavior:
      - Reads 'Summary' sheet for declared paid + received totals.
      - Reads 'Passbook Payment History' sheet for transaction rows.
      - Flags AMEX-routed rows (D1) and self-transfer rows (D2).
      - Adjusts declared_totals so the existing single-tolerance validator
        passes against `result.rows` (which includes self-transfers but
        NOT AMEX-routed) — see spec §7.1.
      - Populates category_hint from Tags column with leading emoji stripped (D4).
    """
    file_path = Path(file_path)
    pdf_content_hash = _sha256_file(file_path)

    # 1) Declared totals from Summary sheet
    summary_df = pd.read_excel(file_path, sheet_name="Summary",
                               header=None, engine="openpyxl")
    summary = _read_summary_totals(summary_df)

    # 2) Transaction rows from Passbook
    passbook = pd.read_excel(file_path, sheet_name="Passbook Payment History",
                             engine="openpyxl")
    # Expected columns (verified during 2026-04-26 inspection):
    #   Date | Time | Transaction Details | Other Transaction Details |
    #   Your Account | Amount | UPI Ref No. | Order ID | Remarks | Tags | Comment
    required = {"Date", "Transaction Details", "Your Account", "Amount", "Tags"}
    missing = required - set(passbook.columns)
    if missing:
        raise ParserError(
            f"Paytm Passbook sheet missing required columns: {missing}. "
            f"Got columns: {list(passbook.columns)}"
        )

    own_handles = _load_own_upi_handles()

    rows: list[ParsedRow] = []
    self_transfer_total = Decimal("0")
    amex_routed_total = Decimal("0")
    ordinal = 1
    for _, raw in passbook.iterrows():
        td = raw["Transaction Details"]
        if td is None or (isinstance(td, float) and pd.isna(td)):
            continue
        td = str(td)
        try:
            direction = _infer_direction(td)
        except ParserError:
            logger.warning("skipping unrecognized Paytm row: %r", td)
            continue

        amt_raw = raw["Amount"]
        if amt_raw is None or (isinstance(amt_raw, float) and pd.isna(amt_raw)):
            continue
        amount = abs(_decimal_from_indian_str(amt_raw))

        is_amex = _is_amex_routed(raw["Your Account"])
        is_self = classify_self_transfer(
            transaction_details=td,
            other_transaction_details=raw.get("Other Transaction Details"),
            own_handles=own_handles,
        )
        if is_amex:
            amex_routed_total += amount
        if is_self and direction == "out":
            self_transfer_total += amount

        rows.append(ParsedRow(
            txn_date=_parse_paytm_date(raw["Date"]),
            amount=amount,
            direction=direction,
            raw_merchant=_strip_prefix(td),
            source_row_ordinal=ordinal,
            is_amex_routed=is_amex,
            is_self_transfer=is_self,
            category_hint=_strip_tag(raw.get("Tags")),
        ))
        ordinal += 1

    # 3) Adjust declared totals so existing validator math closes.
    #    Summary's paid total = ordinary_paid + amex_routed_paid (excludes
    #    self-transfers). Result.rows includes ordinary + self-transfer rows
    #    BUT NOT AMEX (those stay in result.rows for traceability AND for
    #    validator's extracted_out — wait, no, AMEX-routed are still in
    #    result.rows because we kept them above. Let me re-check.)
    #
    #    Result.rows after the loop above contains: ordinary + amex + self.
    #    Validator computes extracted_out over all of result.rows where
    #    direction == 'out'.
    #    extracted_out = ordinary_out + amex_out + self_out
    #    declared_published = paid_summary + self_transfer_total
    #                       = (ordinary_out + amex_out) + self_transfer_total
    #    These match.
    #    For received: no AMEX-received case observed; self-transfers don't
    #    contribute to received either (they're "out"). So declared_credits
    #    = recv_summary unchanged.
    declared_total_spends = summary["paid_amount"] + self_transfer_total
    declared_total_credits = summary["recv_amount"]

    return ParseResult(
        rows=rows,
        declared_totals={
            "total_spends": declared_total_spends,
            "total_credits": declared_total_credits,
            "closing_balance": None,
            "_derived_from_rows": False,
        },
        pdf_content_hash=pdf_content_hash,
        parser_version=__parser_version__,
    )
```

- [ ] **Step 5: Run all Paytm tests**

```bash
.venv/bin/python -m pytest tests/test_paytm_helpers.py tests/test_paytm_upi_parser.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add skills/finance/ingestion/parsers/paytm_upi.py tests/test_paytm_upi_parser.py
git commit -m "feat(paytm): parse() integration — Summary + Passbook + declared-totals adjustment"
```

---

## Task 10: Wire Paytm into `folder_watcher.py`

**Files:**
- Modify: `skills/finance/ingestion/folder_watcher.py:33-41` (ACCOUNT_IDS, EXPECTED_EXTENSION)
- Modify: `skills/finance/ingestion/folder_watcher.py:50-59` (`dispatch_to_parser`)

- [ ] **Step 1: Update ACCOUNT_IDS and EXPECTED_EXTENSION**

In `skills/finance/ingestion/folder_watcher.py`, replace the existing maps:

```python
ACCOUNT_IDS: dict[str, UUID] = {
    "icici_cc": UUID("10000000-0000-0000-0000-000000000003"),
    "amex_cc":  UUID("10000000-0000-0000-0000-000000000005"),
    "paytm_upi": UUID("10000000-0000-0000-0000-000000000006"),
}

EXPECTED_EXTENSION: dict[str, str] = {
    "icici_cc":  ".pdf",
    "amex_cc":   ".xlsx",
    "paytm_upi": ".xlsx",
}
```

- [ ] **Step 2: Add the Paytm dispatch branch in `dispatch_to_parser`**

Inside `dispatch_to_parser`, after the existing `elif bank == "amex_cc":` block, add:

```python
    elif bank == "paytm_upi":
        from skills.finance.ingestion.parsers.paytm_upi import parse as paytm_parse
        parse_result = await asyncio.to_thread(paytm_parse, file_path)
        source = SourceMeta(source="manual_xlsx", source_ref=file_path.name)
```

- [ ] **Step 3: Run the full parser test suite (regression for ICICI/AMEX, integration for Paytm)**

```bash
.venv/bin/python -m pytest tests/ -v
```

Expected: all PASS, no regressions.

- [ ] **Step 4: Commit**

```bash
git add skills/finance/ingestion/folder_watcher.py
git commit -m "feat(folder_watcher): dispatch paytm_upi → paytm_upi.parse"
```

---

## Task 11: Pipeline — use `insertable_rows()` and persist `category_hint`

**Files:**
- Modify: `skills/finance/ingestion/pipeline.py:30-62` (`_build_insert_row` + iteration)

- [ ] **Step 1: Update `_build_insert_row` to include `category_hint`**

In `pipeline.py`, replace the return-dict in `_build_insert_row` with:

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
        "category_hint": r.category_hint,    # W3.1: Paytm-only today; NULL elsewhere
    }
```

- [ ] **Step 2: Update the row iteration in `ingest()` to use `insertable_rows()`**

In `pipeline.ingest()`, change:

```python
    rows = [
        _build_insert_row(r, account_id, parse_result, source_meta)
        for r in parse_result.rows
    ]
```

To:

```python
    rows = [
        _build_insert_row(r, account_id, parse_result, source_meta)
        for r in parse_result.insertable_rows()
    ]
```

- [ ] **Step 3: Run all tests**

```bash
.venv/bin/python -m pytest tests/ -v
```

Expected: all PASS. Existing AMEX/ICICI tests still pass because their `insertable_rows()` returns the same as `rows` (none have `is_amex_routed=True`).

- [ ] **Step 4: Commit**

```bash
git add skills/finance/ingestion/pipeline.py
git commit -m "feat(pipeline): use insertable_rows() and persist category_hint"
```

---

## Task 12: Apply migration to Supabase + lint/typecheck/test green

**Files:**
- Run: `migrations/005_category_hint.sql` against Supabase

- [ ] **Step 1: Apply the migration**

Use the Supabase SQL editor (paste contents of `migrations/005_category_hint.sql`) OR psql with service_role.

Verify via the Python helper:

```bash
.venv/bin/python -c "
from skills.finance.lib.db import service_client
c = service_client()
# Check the column exists by attempting a select on it.
r = c.table('transactions').select('category_hint').limit(1).execute()
print('category_hint column reachable:', r.data is not None)
"
```

Expected: `category_hint column reachable: True`.

- [ ] **Step 2: Run all three project checks**

```bash
.venv/bin/ruff check .
.venv/bin/python -m mypy skills scripts app.py
.venv/bin/python -m pytest -v
```

Expected: all PASS. If lint/typecheck flags anything, fix it inline (the Paytm parser is the only new code; mypy may demand explicit `: Any` annotations on pandas-touching variables — apply the lessons.md `dict[str, Any]` annotation pattern).

- [ ] **Step 3: Commit any lint/typecheck fixes**

```bash
git add -A
git commit -m "chore: lint+typecheck cleanup for W3.1"
```

(Skip if nothing needed fixing.)

---

## Task 13: Backfill — restart app, rename `.rejected`, verify ingestion

**Files:**
- Move: `~/finance-inbox/paytm_upi_apr25_mar26.xlsx.rejected` → `~/finance-inbox/paytm_upi_apr25_mar26.xlsx`

- [ ] **Step 1: Restart the launchd app to pick up new code**

```bash
launchctl kickstart -k gui/$(id -u)/com.rajat.pfa.app
sleep 5
launchctl print gui/$(id -u)/com.rajat.pfa.app | grep -E "pid|state" | head -3
tail -20 /Users/rajat/finance-logs/app.stdout.log
```

Expected: new pid, `scheduler started; jobs=[...]`, `folder_watcher started on /Users/rajat/finance-inbox`. No tracebacks.

- [ ] **Step 2: Rename the file (drop the `.rejected` suffix)**

```bash
mv "/Users/rajat/finance-inbox/paytm_upi_apr25_mar26.xlsx.rejected" \
   "/Users/rajat/finance-inbox/paytm_upi_apr25_mar26.xlsx"
```

The watcher's `on_moved` event picks this up.

- [ ] **Step 3: Watch the log for ingestion activity**

```bash
tail -f /Users/rajat/finance-logs/app.stdout.log &
sleep 30
# Then Ctrl+C the tail
```

Expected log lines:
- `INFO ingested NNN rows from manual_xlsx/paytm_upi_apr25_mar26.xlsx (validator ok, NNN total)` — NNN should be ~711.
- A Telegram message arrives on `@Sharma_finance_bot` saying the totals match.

- [ ] **Step 4: Verify ingestion outcome via DB**

```bash
.venv/bin/python -c "
from skills.finance.lib.db import service_client
c = service_client()
log = c.table('ingestion_log').select('*').order('timestamp', desc=True).limit(3).execute().data
for r in log:
    print(f\"  {str(r.get('timestamp'))[:19]}  {r.get('source_ref','-'):40} status={r.get('status'):20} rows={r.get('rows_added')}\")
print()
paytm_uuid = '10000000-0000-0000-0000-000000000006'
n = c.table('transactions').select('id', count='exact').eq('account_id', paytm_uuid).execute()
print(f'Paytm UPI rows in transactions: {n.count}')
# Verify category_hint populated for some rows
hint = c.table('transactions').select('id', count='exact').eq('account_id', paytm_uuid).not_.is_('category_hint', 'null').execute()
print(f'  of which category_hint populated: {hint.count}')
"
```

Expected:
- Top log row: `paytm_upi_apr25_mar26.xlsx status=success rows=~711`.
- `Paytm UPI rows in transactions: ~711`.
- `category_hint populated: > 500`.

- [ ] **Step 5: Idempotency check — re-drop the file**

Move it out and back:

```bash
mv "/Users/rajat/finance-inbox/paytm_upi_apr25_mar26.xlsx" /tmp/
sleep 2
mv "/tmp/paytm_upi_apr25_mar26.xlsx" "/Users/rajat/finance-inbox/"
sleep 30
```

Expected: a second `ingestion_log` entry with `status=skipped_duplicate`, `rows_added=0`.

- [ ] **Step 6: Push everything**

```bash
git push origin main
```

Expected: clean push, no conflicts.

---

## Self-Review

After writing the plan, I checked it against the spec:

**1. Spec coverage:**
- Spec §3 architecture → covered by Tasks 9–11 wiring.
- Spec §4 file structure → matches the file map exactly.
- Spec §5 source layer → Tasks 3 (detect_bank), 10 (folder_watcher).
- Spec §6 parse layer → Tasks 4–9 (every helper has its own task, then integration in 9).
- Spec §7 validate layer → handled implicitly via Task 9's declared-totals adjustment + Task 12's `validate(result)` call in `pipeline.ingest()` (unchanged).
- Spec §8 persist layer → Tasks 1 (migration file), 11 (pipeline), 12 (apply).
- Spec §9 Telegram review flow → no parser change; existing `_send_summary` already plain-text. Verified in Task 13 step 3.
- Spec §10 backfill → Task 13.
- Spec §11 error handling → behavior follows from existing pipeline flow + parser raising `ParserError`. No dedicated task; tests in 5/9 cover.
- Spec §12 testing → Tasks 4–9 each contain TDD tests.
- Spec §15 risks: mitigated by the soft-fallback comment in Task 9 declared-totals math + ParserError messages including label preview.
- Spec §16 acceptance criteria: all 9 items map to Task 13's verification steps.

**2. Placeholder scan:** no "TBD", "TODO", "implement later". All steps have concrete code or commands. The Paytm UUID is hardcoded in Task 10. The own-handle is hardcoded in Task 4 tests but loaded dynamically in production code (Task 8).

**3. Type consistency:** `ParsedRow` field names — `is_amex_routed`, `is_self_transfer`, `category_hint` — used identically in Task 2 (definition), Task 9 (construction), Task 11 (pipeline read), and tests. `_load_own_upi_handles()` named identically across Tasks 8 and 9. `parse()` signature `(file_path: Path) -> ParseResult` consistent across Task 9 definition and Task 10 dispatch caller.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-29-paytm-xlsx-implementation.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Good when tasks are independent enough that fresh context per task helps.

**2. Inline Execution** — Execute tasks in this session using the executing-plans skill. Batch execution with checkpoints for review. Good when later tasks need context from earlier ones (which they do here — declared-totals adjustment in Task 9 is intricate).

**Which approach?**
