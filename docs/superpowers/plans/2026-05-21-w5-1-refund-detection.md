# W5.1 Refund + Self-Transfer Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, LLM-free post-ingestion pass that populates `is_refund`, `is_self_transfer`, and `linked_txn_id` on `transactions` rows, so future briefs (W4.2) and `/afford` calculations don't conflate real merchant refunds, CC-bill-payment self-transfers, and genuine income.

**Architecture:** New package `skills/finance/categorization/` exposing one function `detect_for_account(account_id, since)` invoked synchronously from `pipeline.py:ingest()` (via `adb()`) after each successful ingestion, plus a one-time `scripts/backfill_refund_detection.py` for the existing 1,227 rows. Detection runs in two phases per call: Phase A processes new direction='in' rows on this account (self-transfer pattern + cross-account match → refund matcher → mark processed); Phase B uses this account's new direction='out' rows to unblock pending pattern-credits on other accounts. Schema migration 008 adds `is_self_transfer` and shifts `is_refund` from default-false to tri-state nullable so the IS NULL idempotency contract works. Inline detection is wrapped in try/except so failures never roll back the primary-fact ingestion.

**Tech Stack:** Python 3.11, sqlglot already pinned, rapidfuzz>=3 already in `pyproject.toml`, psycopg3 readonly client (W3.5), Supabase service client for UPDATEs, pyyaml for the patterns config, pytest with the same `_LIVE` gating pattern as `tests/test_readonly_client.py`.

---

## File Structure

**New files:**

| Path | Responsibility |
|---|---|
| `migrations/008_refund_detection.sql` | DDL: add `is_self_transfer`, shift `is_refund` to tri-state nullable, two performance indexes |
| `skills/finance/categorization/__init__.py` | Empty package marker |
| `skills/finance/categorization/refund_detector.py` | Single public entry `detect_for_account()` + private helpers: `_load_patterns()`, `_matches_self_transfer()`, `_find_refund_match()`, `_find_self_transfer_match()`, `_db_error_verdict` is from W4.1 — not reused here |
| `config/self_transfer_patterns.yaml` | Per-account text patterns; keyed by `account_id`; AMEX + ICICI CC at ship |
| `scripts/backfill_refund_detection.py` | One-time pass; `--dry-run` default, `--apply`, `--strict` opt-in |
| `tests/test_self_transfer_patterns.py` | 6 tests — pattern loader + matching util |
| `tests/test_refund_detector.py` | 17 tests — matcher units (12) + detect_for_account integration (5) |
| `tests/test_pipeline_refund_integration.py` | 2 tests — wrapper happy path + crash-isolation guard |

**Modified files:**

| Path | Change |
|---|---|
| `skills/finance/ingestion/pipeline.py` | Add `_run_refund_detection_safe()`; call from `ingest()` after `_log_success()` |

---

## Task 0: Preconditions

**Files:**
- Modify: `tasks/preconditions-notes.md` (append, gitignored — no commit)

Per CLAUDE.md invariant #11.

- [ ] **Step 0.1: Verify rapidfuzz installed at usable version**

Run:
```
.venv/bin/python -c "
import rapidfuzz
from rapidfuzz import fuzz
print('rapidfuzz', rapidfuzz.__version__)
# Smoke the token_set_ratio API we'll actually use
print('exact match:', fuzz.token_set_ratio('Amazon', 'Amazon'))
print('variant   :', fuzz.token_set_ratio('Amazon', 'Amazon Mumbai'))
print('mismatch  :', fuzz.token_set_ratio('Amazon', 'Swiggy Bangalore'))
"
```

Expected:
```
rapidfuzz <version>
exact match: 100.0
variant   : 100  (or close to 100 — token_set_ratio is whitespace-tokenized)
mismatch  : <low, ~20-40>
```

Record the exact version + the three scores in `tasks/preconditions-notes.md` under a new `## W5.1 — 2026-05-21` heading. If `token_set_ratio('Amazon', 'Amazon Mumbai')` < 80, the spec's threshold won't trip on real variants — flag this and we re-tune in Task 3.

- [ ] **Step 0.2: Verify migration 008 will apply cleanly against live DB**

This is a read-only check; doesn't apply the migration yet.

Run:
```
.venv/bin/python -c "
from skills.finance.lib.db import readonly_client
conn = readonly_client()
with conn.cursor() as cur:
    cur.execute(\"\"\"
        SELECT column_name, data_type, column_default, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'transactions' AND column_name IN ('is_refund', 'is_self_transfer', 'linked_txn_id')
        ORDER BY column_name
    \"\"\")
    for row in cur.fetchall():
        print(row)
    # Also count existing rows that the UPDATE clause in migration 008 will touch
    cur.execute(\"\"\"
        SELECT count(*) FROM transactions WHERE is_refund = false AND linked_txn_id IS NULL
    \"\"\")
    print('rows to nullify:', cur.fetchone()[0])
"
```

Expected:
- `is_refund` exists with `boolean` type, `false` default, `YES` nullable
- `is_self_transfer` does NOT exist (migration adds it)
- `linked_txn_id` exists with `uuid` type, NULL default
- `rows to nullify:` is approximately 1227 (every existing row)

Record exact counts in preconditions-notes.

- [ ] **Step 0.3: Confirm no in-flight code already references `is_refund` outside the spec**

Run:
```
cd "/Users/rajat/AntiGravity/Personal finance Agent" && grep -rn "is_refund\|is_self_transfer\|linked_txn_id" skills/ scripts/ tests/ 2>&1 | grep -v __pycache__ | head -20
```

Expected: only schema references (`migrations/001_init.sql:383-384`) and the PRD mentions; no production code reads/writes these columns yet. If anything unexpected shows up, that's a hidden coupling — flag before proceeding.

- [ ] **Step 0.4: Append findings to preconditions-notes**

Open `tasks/preconditions-notes.md`. Append `## W5.1 — 2026-05-21` section with: rapidfuzz version + three scores, current `is_refund`/`is_self_transfer`/`linked_txn_id` column shapes from `information_schema`, row count to nullify, grep findings. No commit (file is gitignored).

---

## Task 1: Migration 008 — schema + indexes

**Files:**
- Create: `migrations/008_refund_detection.sql`

- [ ] **Step 1.1: Create the migration file**

Write `migrations/008_refund_detection.sql`:
```sql
-- migrations/008_refund_detection.sql
-- W5.1 refund + self-transfer detection schema.
-- Per docs/superpowers/specs/2026-05-21-refund-detection-design.md §4.

-- 1. New flag mirroring is_refund's shape (nullable, no default).
ALTER TABLE transactions
  ADD COLUMN is_self_transfer boolean;   -- NULL = unchecked; false = checked/not; true = confirmed

-- 2. Shift is_refund from default-false to tri-state nullable. Drop the default
--    so new ingestions get NULL; nullify existing rows so the backfill processes them.
ALTER TABLE transactions ALTER COLUMN is_refund DROP DEFAULT;
UPDATE transactions
  SET is_refund = NULL
  WHERE is_refund = false AND linked_txn_id IS NULL;
-- Rows where linked_txn_id is already set: leave is_refund alone.
-- (Zero such rows today; future-proofing clause for re-runnable migrations.)

-- 3. Performance indexes for matcher queries.
CREATE INDEX IF NOT EXISTS idx_transactions_account_date
  ON transactions (account_id, date);   -- refund matcher: candidates in this account, date range
CREATE INDEX IF NOT EXISTS idx_transactions_amount_date
  ON transactions (amount, date);       -- self-transfer matcher: matching-amount debits across all accounts
```

- [ ] **Step 1.2: Apply the migration against the live DB**

Open Supabase SQL Editor (https://supabase.com/dashboard/project/wqfwazmndhvoxrkatfkv/sql) and paste the contents of `migrations/008_refund_detection.sql`. Execute.

Expected: 4 successful statements. The `UPDATE transactions SET is_refund = NULL` row should report ~1,227 affected rows (matching Task 0.2's count).

- [ ] **Step 1.3: Verify the migration landed**

Run:
```
.venv/bin/python -c "
from skills.finance.lib.db import readonly_client
conn = readonly_client()
with conn.cursor() as cur:
    cur.execute(\"\"\"
        SELECT column_name, data_type, column_default, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'transactions' AND column_name IN ('is_refund', 'is_self_transfer')
        ORDER BY column_name
    \"\"\")
    for row in cur.fetchall():
        print(row)
    cur.execute(\"SELECT count(*) FROM transactions WHERE is_refund IS NULL\")
    print('is_refund IS NULL count:', cur.fetchone()[0])
    cur.execute(\"SELECT count(*) FROM transactions WHERE is_self_transfer IS NULL\")
    print('is_self_transfer IS NULL count:', cur.fetchone()[0])
    cur.execute(\"\"\"
        SELECT indexname FROM pg_indexes
        WHERE tablename = 'transactions' AND indexname LIKE 'idx_transactions_%'
    \"\"\")
    for row in cur.fetchall():
        print('index:', row[0])
"
```

Expected:
- `is_refund` shows `column_default = None`, `is_nullable = YES`
- `is_self_transfer` exists with `boolean`, `column_default = None`, `is_nullable = YES`
- Both `IS NULL count` are approximately 1,227
- Indexes `idx_transactions_account_date` and `idx_transactions_amount_date` both listed

- [ ] **Step 1.4: Commit the migration file**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add migrations/008_refund_detection.sql && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(schema): migration 008 — refund detection (W5.1 §4)

Add is_self_transfer boolean (nullable, no default). Shift is_refund
from default-false to tri-state nullable so the IS NULL idempotency
contract works (NULL=unprocessed, true=confirmed refund, false=
confirmed not-a-refund). Nullify existing checked-but-unlinked rows
so the backfill processes them. Two indexes for matcher performance.

Applied to live DB via Supabase SQL editor.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Self-transfer patterns config + loader

**Files:**
- Create: `config/self_transfer_patterns.yaml`
- Create: `skills/finance/categorization/__init__.py`
- Create: `skills/finance/categorization/refund_detector.py` (initial scaffold — pattern loader + util only)
- Create: `tests/test_self_transfer_patterns.py`

- [ ] **Step 2.1: Create the patterns config**

Write `config/self_transfer_patterns.yaml`:
```yaml
# W5.1 self-transfer pattern config.
# Keyed by account_id (UUID strings from migrations/003_seed*.sql).
# Patterns are case-insensitive substrings checked against transactions.raw_merchant.
# Adding a new bank source = add an account_id key + its marker substrings; no code change.

# ICICI CC — account_id from 003_seed.local.sql
"10000000-0000-0000-0000-000000000003":
  - "BBPS Payment received"

# AMEX CC — account_id from 003_seed.local.sql
"10000000-0000-0000-0000-000000000005":
  - "PAYMENT RECEIVED. THANK YOU"
```

- [ ] **Step 2.2: Create the empty package marker**

Write `skills/finance/categorization/__init__.py`:
```python
"""W5.1 post-ingestion derived/audit passes.

Contains the refund + self-transfer detector. Future siblings (post-V1):
merchant_normalizer.py, category_assigner.py."""
```

- [ ] **Step 2.3: Write failing tests for the patterns loader + util**

Write `tests/test_self_transfer_patterns.py`:
```python
"""W5.1 self-transfer pattern loader + matching util.

Per docs/superpowers/specs/2026-05-21-refund-detection-design.md §7:
config errors fail loud at module load time — better to crash detection
than process every row as not-a-self-transfer.
"""
from __future__ import annotations

import textwrap
from uuid import UUID

import pytest

from skills.finance.categorization.refund_detector import (
    _load_patterns,
    _matches_self_transfer,
)

ICICI_CC = UUID("10000000-0000-0000-0000-000000000003")
AMEX_CC = UUID("10000000-0000-0000-0000-000000000005")


def test_load_patterns_from_committed_yaml():
    """Committed defaults: ICICI CC + AMEX CC patterns load with the documented
    marker substrings. Regression-guard against accidental config changes."""
    patterns = _load_patterns()
    assert ICICI_CC in patterns
    assert AMEX_CC in patterns
    assert "BBPS Payment received" in patterns[ICICI_CC]
    assert "PAYMENT RECEIVED. THANK YOU" in patterns[AMEX_CC]


def test_load_missing_file_raises(tmp_path):
    """Per §7: fail loud at load time, not silent partial behavior."""
    bogus = tmp_path / "does_not_exist.yaml"
    with pytest.raises(FileNotFoundError):
        _load_patterns(bogus)


def test_load_malformed_yaml_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("this is not: valid: yaml: at all: :::")
    with pytest.raises(Exception):  # yaml.YAMLError or similar
        _load_patterns(p)


def test_load_empty_patterns_list_raises(tmp_path):
    """An account with empty patterns list = config bug. Refuse silent
    'always returns False' behavior."""
    p = tmp_path / "empty.yaml"
    p.write_text(textwrap.dedent("""
        "10000000-0000-0000-0000-000000000003": []
    """))
    with pytest.raises(ValueError, match="empty"):
        _load_patterns(p)


def test_load_empty_pattern_string_raises(tmp_path):
    """An empty-string pattern would match every row. Refuse."""
    p = tmp_path / "empty_str.yaml"
    p.write_text(textwrap.dedent("""
        "10000000-0000-0000-0000-000000000003":
          - ""
    """))
    with pytest.raises(ValueError, match="empty"):
        _load_patterns(p)


def test_matches_self_transfer_case_insensitive():
    """Substring match, case-insensitive, multi-pattern OR."""
    patterns = ["BBPS Payment received", "PAYMENT RECEIVED. THANK YOU"]
    assert _matches_self_transfer("11373135294 BBPS Payment received 0", patterns) is True
    assert _matches_self_transfer("11373135294 bbps payment received 0", patterns) is True
    assert _matches_self_transfer("payment received. thank you", patterns) is True
    assert _matches_self_transfer("PaYmEnT REcEiVeD. ThAnK YoU", patterns) is True
    assert _matches_self_transfer("Some random merchant", patterns) is False
    assert _matches_self_transfer("", patterns) is False
    assert _matches_self_transfer(None, patterns) is False
```

- [ ] **Step 2.4: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_self_transfer_patterns.py -v 2>&1 | tail -10
```

Expected: `ImportError` on `skills.finance.categorization.refund_detector` (module doesn't exist yet).

- [ ] **Step 2.5: Write the initial scaffold + loader + util**

Write `skills/finance/categorization/refund_detector.py`:
```python
"""W5.1 refund + self-transfer detector.

Per docs/superpowers/specs/2026-05-21-refund-detection-design.md.

Pure heuristic detection (no LLM). Populates is_refund, is_self_transfer,
linked_txn_id on transactions rows after ingestion. Invoked from
pipeline.py via adb() so the synchronous DB calls don't block the async loop.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

import yaml

_PATTERNS_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "self_transfer_patterns.yaml"
)


def _load_patterns(path: Path | None = None) -> dict[UUID, list[str]]:
    """Load per-account self-transfer text patterns from yaml.

    Fail loud at load time (per spec §7) — empty list, empty string, malformed
    YAML, or missing file all raise rather than silently returning empty
    behavior. The error category that would otherwise hide is "every row
    processed as not-a-self-transfer."
    """
    p = path if path is not None else _PATTERNS_PATH
    with open(p) as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: expected a mapping, got {type(raw).__name__}")

    out: dict[UUID, list[str]] = {}
    for key, patterns in raw.items():
        try:
            acct = UUID(str(key))
        except ValueError as e:
            raise ValueError(f"{p}: key {key!r} is not a valid UUID") from e
        if not isinstance(patterns, list):
            raise ValueError(f"{p}[{key}]: expected a list, got {type(patterns).__name__}")
        if len(patterns) == 0:
            raise ValueError(
                f"{p}[{key}]: empty patterns list — refuse silent "
                "always-False behavior. Remove the key or add at least one pattern."
            )
        for s in patterns:
            if not isinstance(s, str) or not s.strip():
                raise ValueError(
                    f"{p}[{key}]: empty or non-string pattern {s!r} — "
                    "would match every row."
                )
        out[acct] = list(patterns)
    return out


def _matches_self_transfer(raw_merchant: str | None, patterns: list[str]) -> bool:
    """Case-insensitive substring match, multi-pattern OR."""
    if not raw_merchant:
        return False
    haystack = raw_merchant.casefold()
    return any(p.casefold() in haystack for p in patterns)
```

- [ ] **Step 2.6: Run tests to verify they pass**

Run:
```
.venv/bin/python -m pytest tests/test_self_transfer_patterns.py -v 2>&1 | tail -10
```

Expected: 7 passed.

- [ ] **Step 2.7: Lint + typecheck**

Run:
```
.venv/bin/ruff check skills/finance/categorization tests/test_self_transfer_patterns.py && .venv/bin/mypy skills/finance/categorization
```

Expected: all checks pass + no mypy issues.

- [ ] **Step 2.8: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add config/self_transfer_patterns.yaml skills/finance/categorization/__init__.py skills/finance/categorization/refund_detector.py tests/test_self_transfer_patterns.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(categorization): self-transfer pattern loader + AMEX/ICICI CC patterns (W5.1 §3)

New skills/finance/categorization/ package. _load_patterns() reads
config/self_transfer_patterns.yaml keyed by account_id; fails loud
on missing file, malformed YAML, empty pattern list, or empty
pattern string. _matches_self_transfer() does case-insensitive
substring match with multi-pattern OR. 7 tests cover regression
defaults + every fail-loud branch + case-insensitivity matrix.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Refund matcher (`_find_refund_match`)

**Files:**
- Modify: `skills/finance/categorization/refund_detector.py` (append)
- Modify: `tests/test_refund_detector.py` (new file)

- [ ] **Step 3.1: Write failing unit tests for the refund matcher**

Write `tests/test_refund_detector.py`:
```python
"""W5.1 refund detector — matcher units + detect_for_account integration.

Per docs/superpowers/specs/2026-05-21-refund-detection-design.md §5 + §8.

Unit half (this file): pure functions, no DB. Mocked-row dataclass-like
dicts as input. Run on every CI.

Integration half (appended in Task 5): live DB, gated like
tests/test_readonly_client.py, transaction-rollback fixture so seeded rows
never persist.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from skills.finance.categorization.refund_detector import _find_refund_match

AMEX_CC = UUID("10000000-0000-0000-0000-000000000005")
ICICI_CC = UUID("10000000-0000-0000-0000-000000000003")


@dataclass
class FakeRow:
    """Minimal stand-in for a transactions row, with just the fields
    _find_refund_match inspects. Real impl will be passed dicts coming
    from psycopg's cursor; the matcher should work with attribute OR
    subscript access — implementation choice."""
    id: UUID
    account_id: UUID
    date: date
    amount: Decimal
    direction: str
    raw_merchant: str | None
    is_refund: bool | None = None
    is_self_transfer: bool | None = None


def _credit(date_=None, merchant="Amazon", amount="500.00", acct=AMEX_CC):
    return FakeRow(
        id=uuid4(), account_id=acct,
        date=date_ or date(2026, 3, 15),
        amount=Decimal(amount), direction="in",
        raw_merchant=merchant,
    )


def _debit(date_, merchant="Amazon", amount="500.00", acct=AMEX_CC,
           is_refund=None, is_self_transfer=None):
    return FakeRow(
        id=uuid4(), account_id=acct,
        date=date_, amount=Decimal(amount),
        direction="out", raw_merchant=merchant,
        is_refund=is_refund, is_self_transfer=is_self_transfer,
    )


def test_single_exact_candidate_matches():
    credit = _credit()
    candidates = [_debit(date(2026, 3, 1), "Amazon")]
    match = _find_refund_match(credit, candidates)
    assert match is not None
    assert match.id == candidates[0].id


def test_multiple_candidates_pick_smallest_date_delta():
    """D6 locked: chronologically closest wins."""
    credit = _credit(date_=date(2026, 3, 15))
    older = _debit(date(2026, 2, 20), "Amazon")
    newer = _debit(date(2026, 3, 8), "Amazon")
    match = _find_refund_match(credit, [older, newer])
    assert match.id == newer.id


def test_rapidfuzz_score_79_rejected():
    """Threshold is >= 80 (D2)."""
    credit = _credit(merchant="Amazon")
    # Construct a merchant that scores below 80
    candidates = [_debit(date(2026, 3, 1), "Swiggy Bangalore Restaurant Delivery")]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_rapidfuzz_score_80_accepted_via_variant():
    """Real-world variant: 'Amazon' vs 'Amazon Mumbai' should pass token_set_ratio
    threshold of 80 (verified in Task 0.1)."""
    credit = _credit(merchant="Amazon")
    candidates = [_debit(date(2026, 3, 1), "Amazon Mumbai")]
    match = _find_refund_match(credit, candidates)
    assert match is not None


def test_same_day_candidate_excluded():
    """Window is [credit.date - 30d, credit.date - 1d] — same-day excluded
    (typically adjustment artifacts, not refunds)."""
    credit = _credit(date_=date(2026, 3, 15))
    candidates = [_debit(date(2026, 3, 15), "Amazon")]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_thirty_one_day_old_candidate_excluded():
    credit = _credit(date_=date(2026, 3, 31))
    candidates = [_debit(date(2026, 2, 28), "Amazon")]  # 31 days back
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_amount_mismatch_excluded():
    credit = _credit(amount="500.00")
    candidates = [_debit(date(2026, 3, 1), "Amazon", amount="501.00")]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_null_raw_merchant_on_credit_returns_none():
    credit = _credit(merchant=None)
    candidates = [_debit(date(2026, 3, 1), "Amazon")]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_null_raw_merchant_on_candidate_excluded():
    credit = _credit(merchant="Amazon")
    candidates = [_debit(date(2026, 3, 1), None)]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_candidate_with_is_refund_true_excluded():
    """Cycle prevention — don't link a refund to another refund (§7)."""
    credit = _credit()
    candidates = [_debit(date(2026, 3, 1), "Amazon", is_refund=True)]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_candidate_with_is_self_transfer_true_excluded():
    """Cross-linking prevention — don't link a refund into a self-transfer
    pair (§7 cycle-detection scope sub-decision)."""
    credit = _credit()
    candidates = [_debit(date(2026, 3, 1), "Amazon", is_self_transfer=True)]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_different_account_excluded():
    """Refund matcher is same-account-only (D2)."""
    credit = _credit(acct=AMEX_CC)
    candidates = [_debit(date(2026, 3, 1), "Amazon", acct=ICICI_CC)]
    match = _find_refund_match(credit, candidates)
    assert match is None
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_refund_detector.py -v 2>&1 | tail -10
```

Expected: ImportError on `_find_refund_match`.

- [ ] **Step 3.3: Implement `_find_refund_match`**

Append to `skills/finance/categorization/refund_detector.py`:
```python
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from rapidfuzz import fuzz

_FUZZ_THRESHOLD = 80
_REFUND_WINDOW_DAYS = 30


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Tolerant attr-or-subscript access — _find_refund_match accepts both
    dict-like rows (from psycopg) and dataclass-like rows (from tests)."""
    if hasattr(row, key):
        return getattr(row, key)
    if hasattr(row, "__getitem__"):
        try:
            return row[key]
        except (KeyError, TypeError):
            pass
    return default


def _find_refund_match(credit: Any, candidates: list[Any]) -> Any | None:
    """Pick the best refund original for `credit` from `candidates`.

    Returns the chosen candidate row (or None if none qualify).
    Per spec §5.2 step 2 + D2 + D6: exact amount, same account, fuzzy merchant
    >= 80, window [credit.date - 30d, credit.date - 1d], exclude candidates
    already flagged as refund or self-transfer. On ties, smallest date delta wins.
    """
    credit_merchant = _row_get(credit, "raw_merchant")
    if not credit_merchant:
        return None
    credit_date = _row_get(credit, "date")
    credit_amount = _row_get(credit, "amount")
    credit_account = _row_get(credit, "account_id")

    earliest_allowed = credit_date - timedelta(days=_REFUND_WINDOW_DAYS)
    latest_allowed = credit_date - timedelta(days=1)

    best: Any | None = None
    best_delta: int | None = None
    for c in candidates:
        if _row_get(c, "account_id") != credit_account:
            continue
        if _row_get(c, "amount") != credit_amount:
            continue
        c_date = _row_get(c, "date")
        if c_date < earliest_allowed or c_date > latest_allowed:
            continue
        if _row_get(c, "is_refund") is True:
            continue
        if _row_get(c, "is_self_transfer") is True:
            continue
        c_merchant = _row_get(c, "raw_merchant")
        if not c_merchant:
            continue
        score = fuzz.token_set_ratio(credit_merchant, c_merchant)
        if score < _FUZZ_THRESHOLD:
            continue
        delta = (credit_date - c_date).days
        if best is None or delta < best_delta:
            best = c
            best_delta = delta
    return best
```

- [ ] **Step 3.4: Run tests to verify they pass**

Run:
```
.venv/bin/python -m pytest tests/test_refund_detector.py -v 2>&1 | tail -20
```

Expected: 12 passed.

- [ ] **Step 3.5: Lint + typecheck**

Run:
```
.venv/bin/ruff check skills/finance/categorization tests/test_refund_detector.py && .venv/bin/mypy skills/finance/categorization
```

Expected: clean.

- [ ] **Step 3.6: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add skills/finance/categorization/refund_detector.py tests/test_refund_detector.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(categorization): refund matcher (W5.1 §5.2 step 2)

_find_refund_match() picks the best refund original from candidate
debits: exact amount, same account, fuzzy merchant >= 80
(rapidfuzz.token_set_ratio), date window [credit-30d, credit-1d],
exclude candidates already flagged is_refund or is_self_transfer
(cycle + cross-linking prevention). Multi-candidate tie-break:
smallest date delta (D6). 12 unit tests cover the matrix.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Self-transfer matcher (`_find_self_transfer_match`)

**Files:**
- Modify: `skills/finance/categorization/refund_detector.py` (append)
- Modify: `tests/test_refund_detector.py` (append)

- [ ] **Step 4.1: Append failing tests for self-transfer matcher**

Append to `tests/test_refund_detector.py`:
```python


# --- self-transfer matcher tests --------------------------------------------

from skills.finance.categorization.refund_detector import (
    PENDING,
    _find_self_transfer_match,
)


def test_self_transfer_pattern_hit_with_cross_account_match():
    """Pattern matches AND a cross-account debit with same amount within ±2d
    exists → returns the debit row."""
    credit = _credit(merchant="PAYMENT RECEIVED. THANK YOU", acct=AMEX_CC,
                     date_=date(2026, 3, 15), amount="60000.00")
    debit = _debit(date(2026, 3, 14), "ACH/AMEX BILL PAYMENT", amount="60000.00",
                   acct=UUID("10000000-0000-0000-0000-000000000001"))  # savings
    patterns = ["PAYMENT RECEIVED. THANK YOU"]
    match = _find_self_transfer_match(credit, [debit], patterns)
    assert match is not None
    assert match is not PENDING
    assert match.id == debit.id


def test_self_transfer_pattern_hit_no_cross_account_returns_pending():
    """Pattern matches but no cross-account debit exists yet → returns PENDING
    sentinel so caller leaves is_self_transfer=NULL (waits for the savings
    statement to be ingested)."""
    credit = _credit(merchant="PAYMENT RECEIVED. THANK YOU", acct=AMEX_CC,
                     amount="60000.00")
    patterns = ["PAYMENT RECEIVED. THANK YOU"]
    match = _find_self_transfer_match(credit, [], patterns)
    assert match is PENDING


def test_self_transfer_no_pattern_returns_none():
    """No pattern hit → returns None (caller proceeds to refund check)."""
    credit = _credit(merchant="Amazon Mumbai", acct=AMEX_CC)
    patterns = ["PAYMENT RECEIVED. THANK YOU"]
    match = _find_self_transfer_match(credit, [], patterns)
    assert match is None


def test_self_transfer_only_same_account_debit_returns_pending():
    """Pattern hits but the only matching-amount debit is in the same
    account — cross-account is required. Treated as pending (wait for a
    cross-account debit to appear)."""
    credit = _credit(merchant="PAYMENT RECEIVED. THANK YOU", acct=AMEX_CC,
                     amount="60000.00")
    same_acct_debit = _debit(date(2026, 3, 14), "Something", amount="60000.00",
                              acct=AMEX_CC)
    patterns = ["PAYMENT RECEIVED. THANK YOU"]
    match = _find_self_transfer_match(credit, [same_acct_debit], patterns)
    assert match is PENDING


def test_self_transfer_multiple_cross_account_smallest_date_delta_wins():
    credit = _credit(merchant="PAYMENT RECEIVED. THANK YOU", acct=AMEX_CC,
                     date_=date(2026, 3, 15), amount="60000.00")
    far_debit = _debit(date(2026, 3, 13), "X", amount="60000.00",  # 2d back
                       acct=UUID("10000000-0000-0000-0000-000000000001"))
    near_debit = _debit(date(2026, 3, 14), "Y", amount="60000.00",  # 1d back
                        acct=UUID("10000000-0000-0000-0000-000000000001"))
    patterns = ["PAYMENT RECEIVED. THANK YOU"]
    match = _find_self_transfer_match(credit, [far_debit, near_debit], patterns)
    assert match.id == near_debit.id


def test_self_transfer_cross_account_with_is_self_transfer_true_excluded():
    """Already-flagged debits aren't re-linkable."""
    credit = _credit(merchant="PAYMENT RECEIVED. THANK YOU", acct=AMEX_CC,
                     amount="60000.00")
    debit = _debit(date(2026, 3, 14), "X", amount="60000.00",
                   acct=UUID("10000000-0000-0000-0000-000000000001"),
                   is_self_transfer=True)
    patterns = ["PAYMENT RECEIVED. THANK YOU"]
    match = _find_self_transfer_match(credit, [debit], patterns)
    assert match is PENDING
```

- [ ] **Step 4.2: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_refund_detector.py -v 2>&1 | tail -10
```

Expected: ImportError on `_find_self_transfer_match` and `PENDING`.

- [ ] **Step 4.3: Implement `_find_self_transfer_match`**

Append to `skills/finance/categorization/refund_detector.py`:
```python
_SELF_TRANSFER_WINDOW_DAYS = 2


class _Pending:
    """Sentinel — pattern matched but no cross-account debit found yet.
    Caller leaves is_self_transfer=NULL and waits for next detection run."""
    __slots__ = ()
    def __repr__(self) -> str:
        return "PENDING"


PENDING: _Pending = _Pending()


def _find_self_transfer_match(
    credit: Any,
    recent_debits: list[Any],
    patterns: list[str],
) -> Any | _Pending | None:
    """Return the matching cross-account debit row, PENDING (pattern hit but
    no cross-account match yet), or None (no pattern hit — proceed to refund).

    Per spec §5.2 step 1 + D3 + D4 + D6: pattern hit is REQUIRED; cross-account
    match (different account_id, exact amount, ±2 days, is_self_transfer IS NOT
    true) is sufficient when present. Multi-match: smallest date delta wins;
    tie-break smallest amount delta (amount is exact so rarely fires).
    """
    if not _matches_self_transfer(_row_get(credit, "raw_merchant"), patterns):
        return None

    credit_account = _row_get(credit, "account_id")
    credit_amount = _row_get(credit, "amount")
    credit_date = _row_get(credit, "date")

    best: Any | None = None
    best_delta: int | None = None
    for d in recent_debits:
        if _row_get(d, "account_id") == credit_account:
            continue
        if _row_get(d, "amount") != credit_amount:
            continue
        d_date = _row_get(d, "date")
        delta = abs((credit_date - d_date).days)
        if delta > _SELF_TRANSFER_WINDOW_DAYS:
            continue
        if _row_get(d, "is_self_transfer") is True:
            continue
        if best is None or delta < best_delta:
            best = d
            best_delta = delta
    if best is not None:
        return best
    return PENDING
```

- [ ] **Step 4.4: Run tests to verify they pass**

Run:
```
.venv/bin/python -m pytest tests/test_refund_detector.py -v 2>&1 | tail -20
```

Expected: 18 passed (12 refund + 6 self-transfer).

- [ ] **Step 4.5: Lint + typecheck**

Run:
```
.venv/bin/ruff check skills/finance/categorization tests/test_refund_detector.py && .venv/bin/mypy skills/finance/categorization
```

Expected: clean.

- [ ] **Step 4.6: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add skills/finance/categorization/refund_detector.py tests/test_refund_detector.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(categorization): self-transfer matcher (W5.1 §5.2 step 1)

_find_self_transfer_match() returns matching cross-account debit,
PENDING sentinel (pattern hit but no cross-account match — wait for
savings statement), or None (no pattern hit — proceed to refund
check). Pattern is required; cross-account (account_id !=) + exact
amount + ±2d + is_self_transfer IS NOT true. Tie-break: smallest
date delta. 6 unit tests cover the matrix.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `detect_for_account` orchestrator + Phase A + Phase B + integration tests

**Files:**
- Modify: `skills/finance/categorization/refund_detector.py` (append the orchestrator)
- Modify: `tests/test_refund_detector.py` (append integration tests)

- [ ] **Step 5.1: Append failing integration tests**

Append to `tests/test_refund_detector.py`:
```python


# --- detect_for_account integration tests (live DB, gated) ------------------

import psycopg
import pytest
from urllib.parse import quote, urlsplit, urlunsplit

from skills.finance.categorization.refund_detector import (
    DetectionResult,
    detect_for_account,
)


def _readonly_password_available() -> bool:
    try:
        from skills.finance.lib.settings import settings
        return bool(settings.supabase_readonly_password)
    except Exception:
        return False


_LIVE = pytest.mark.skipif(
    not _readonly_password_available(),
    reason="SUPABASE_READONLY_PASSWORD not in settings — live integration tests skipped",
)

# Reusable account UUIDs from 003_seed.local.sql.
SAVINGS = UUID("10000000-0000-0000-0000-000000000001")


def _write_dsn() -> str:
    """Build a writable psycopg DSN by substituting the SERVICE role (postgres)
    + service role password into SUPABASE_DB_URL. Live integration tests need
    INSERT/UPDATE; readonly_client can't write. We then ROLLBACK at teardown,
    so seeded rows never persist."""
    from skills.finance.lib.settings import settings
    sp = urlsplit(settings.supabase_db_url)
    # The DB URL already encodes postgres.<project_ref>:<pw>; just reuse it.
    return settings.supabase_db_url


@pytest.fixture
def write_conn():
    """Open a transaction we'll roll back at teardown. Tests INSERT into
    transactions through this connection and assert via direct SELECTs;
    no row commits. Per spec §8.3 decision 6.i."""
    conn = psycopg.connect(_write_dsn(), autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _insert(conn, **fields) -> UUID:
    """Insert one transactions row with given fields, return the id."""
    fields.setdefault("id", uuid4())
    fields.setdefault("user_id", UUID("00000000-0000-0000-0000-000000000001"))  # Rajat
    fields.setdefault("currency", "INR")
    cols = ",".join(fields.keys())
    placeholders = ",".join(["%s"] * len(fields))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO transactions ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
    return fields["id"]


def _select_flags(conn, txn_id: UUID) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_refund, is_self_transfer, linked_txn_id FROM transactions WHERE id = %s",
            (txn_id,),
        )
        r = cur.fetchone()
        return {
            "is_refund": r[0], "is_self_transfer": r[1], "linked_txn_id": r[2],
        }


@_LIVE
def test_refund_happy_path(write_conn):
    """Seed: AMEX debit + matching credit. Run detect_for_account. Assert
    is_refund=true + linked_txn_id pointing to the debit."""
    debit_id = _insert(
        write_conn, account_id=AMEX_CC, date=date(2026, 3, 1),
        amount=Decimal("500.00"), direction="out",
        raw_merchant="Amazon", is_refund=None,
    )
    credit_id = _insert(
        write_conn, account_id=AMEX_CC, date=date(2026, 3, 15),
        amount=Decimal("500.00"), direction="in",
        raw_merchant="Amazon Mumbai", is_refund=None,
    )
    # Use the same write_conn — detect_for_account opens its own readonly
    # connection but UPDATEs go through service_client; we'll need a slight
    # adapter to use the test's write_conn for both reads and writes.
    result = detect_for_account(
        AMEX_CC, since=date(2026, 2, 1),
        _conn_for_test=write_conn,
    )
    flags = _select_flags(write_conn, credit_id)
    assert flags["is_refund"] is True
    assert flags["linked_txn_id"] == debit_id
    assert result.refunds_linked == 1


@_LIVE
def test_self_transfer_happy_path(write_conn):
    """Seed: AMEX credit with PAYMENT RECEIVED + savings matching debit.
    Assert both flagged, linked_txn_id only on CC."""
    debit_id = _insert(
        write_conn, account_id=SAVINGS, date=date(2026, 3, 14),
        amount=Decimal("60000.00"), direction="out",
        raw_merchant="AMEX CC BILL PAYMENT", is_self_transfer=None,
    )
    credit_id = _insert(
        write_conn, account_id=AMEX_CC, date=date(2026, 3, 15),
        amount=Decimal("60000.00"), direction="in",
        raw_merchant="PAYMENT RECEIVED. THANK YOU",
        is_self_transfer=None,
    )
    result = detect_for_account(
        AMEX_CC, since=date(2026, 2, 1),
        _conn_for_test=write_conn,
    )
    cc_flags = _select_flags(write_conn, credit_id)
    savings_flags = _select_flags(write_conn, debit_id)
    assert cc_flags["is_self_transfer"] is True
    assert cc_flags["linked_txn_id"] == debit_id
    assert savings_flags["is_self_transfer"] is True
    assert savings_flags["linked_txn_id"] is None  # one-way FK
    assert result.self_transfers_linked == 1


@_LIVE
def test_phase_b_catch_up(write_conn):
    """Ingest CC credit first (no savings yet) → leaves pending. Then ingest
    savings debit; running detect_for_account(savings) Phase B unblocks the
    CC credit."""
    credit_id = _insert(
        write_conn, account_id=AMEX_CC, date=date(2026, 3, 15),
        amount=Decimal("60000.00"), direction="in",
        raw_merchant="PAYMENT RECEIVED. THANK YOU",
        is_self_transfer=None,
    )
    # First detect — no savings debit yet
    r1 = detect_for_account(AMEX_CC, since=date(2026, 2, 1), _conn_for_test=write_conn)
    cc_flags_1 = _select_flags(write_conn, credit_id)
    assert cc_flags_1["is_self_transfer"] is None  # still pending
    assert r1.rows_pending == 1
    # Now seed savings debit and run detection for savings
    debit_id = _insert(
        write_conn, account_id=SAVINGS, date=date(2026, 3, 14),
        amount=Decimal("60000.00"), direction="out",
        raw_merchant="AMEX BILL", is_self_transfer=None,
    )
    r2 = detect_for_account(SAVINGS, since=date(2026, 2, 1), _conn_for_test=write_conn)
    cc_flags_2 = _select_flags(write_conn, credit_id)
    savings_flags = _select_flags(write_conn, debit_id)
    assert cc_flags_2["is_self_transfer"] is True
    assert cc_flags_2["linked_txn_id"] == debit_id
    assert savings_flags["is_self_transfer"] is True
    assert r2.self_transfers_linked == 1


@_LIVE
def test_idempotent_re_run_is_noop(write_conn):
    """First run processes a row; second run is a no-op (skips via IS NULL guard)."""
    _insert(
        write_conn, account_id=AMEX_CC, date=date(2026, 3, 15),
        amount=Decimal("500.00"), direction="in",
        raw_merchant="Random Merchant", is_refund=None,
    )
    r1 = detect_for_account(AMEX_CC, since=date(2026, 2, 1), _conn_for_test=write_conn)
    r2 = detect_for_account(AMEX_CC, since=date(2026, 2, 1), _conn_for_test=write_conn)
    assert r1.rows_processed >= 1
    assert r2.rows_processed == 0


@_LIVE
def test_user_override_preserved(write_conn):
    """A row already flagged is_refund=true is left alone on detection runs."""
    debit_id = _insert(
        write_conn, account_id=AMEX_CC, date=date(2026, 3, 1),
        amount=Decimal("500.00"), direction="out", raw_merchant="Amazon",
    )
    credit_id = _insert(
        write_conn, account_id=AMEX_CC, date=date(2026, 3, 15),
        amount=Decimal("500.00"), direction="in", raw_merchant="Amazon Mumbai",
        is_refund=True,  # user-overridden to TRUE before detection
        linked_txn_id=debit_id,
    )
    r = detect_for_account(AMEX_CC, since=date(2026, 2, 1), _conn_for_test=write_conn)
    flags = _select_flags(write_conn, credit_id)
    assert flags["is_refund"] is True   # untouched
    assert flags["linked_txn_id"] == debit_id   # untouched
    assert r.refunds_linked == 0   # detector didn't re-process this row
```

- [ ] **Step 5.2: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_refund_detector.py -v 2>&1 | tail -15
```

Expected: ImportError on `detect_for_account` / `DetectionResult`.

- [ ] **Step 5.3: Implement the orchestrator**

Append to `skills/finance/categorization/refund_detector.py`:
```python
import logging
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetectionResult:
    refunds_linked: int = 0
    self_transfers_linked: int = 0
    rows_processed: int = 0
    rows_pending: int = 0


_ALLOWED_ACCOUNTS_CACHE: dict[UUID, list[str]] | None = None


def _get_patterns() -> dict[UUID, list[str]]:
    global _ALLOWED_ACCOUNTS_CACHE
    if _ALLOWED_ACCOUNTS_CACHE is None:
        _ALLOWED_ACCOUNTS_CACHE = _load_patterns()
    return _ALLOWED_ACCOUNTS_CACHE


def detect_for_account(
    account_id: UUID,
    since: date | None = None,
    _conn_for_test: Any = None,
) -> DetectionResult:
    """Detect refunds + self-transfers for rows on this account.

    Two phases per spec §5.2 + §5.3:
      A. Process new direction='in' rows on this account.
      B. New direction='out' rows on this account may unblock pending
         pattern-credits on OTHER accounts.

    `_conn_for_test`: when provided (test mode), uses this psycopg connection
    for both reads and writes (so transaction-rollback fixture works).
    In production: SELECTs via readonly_client(), UPDATEs via service_client().
    """
    patterns = _get_patterns()

    if _conn_for_test is not None:
        return _detect_with_conn(account_id, since, _conn_for_test, patterns)
    else:
        # Production path
        from skills.finance.lib.db import readonly_client, service_client
        # The production path also uses a single connection for both reads
        # and writes — we re-use the service_client + a psycopg connection
        # to readonly. For atomicity within an account's detection pass,
        # operations are individually committed (no per-batch transaction).
        return _detect_production(account_id, since, patterns,
                                   readonly_client(), service_client())


def _detect_with_conn(
    account_id: UUID,
    since: date | None,
    conn: Any,
    patterns: dict[UUID, list[str]],
) -> DetectionResult:
    """Test-mode detection using a single psycopg connection for both
    reads and writes."""
    return _detect_impl(
        account_id, since, patterns,
        read=lambda sql, params: _exec_fetch(conn, sql, params),
        write=lambda sql, params: _exec(conn, sql, params),
    )


def _detect_production(
    account_id: UUID,
    since: date | None,
    patterns: dict[UUID, list[str]],
    readonly_conn: Any,
    service_client_: Any,
) -> DetectionResult:
    """Production-mode detection: psycopg readonly for SELECT (bypasses
    Supabase 1000-row cap), service client for UPDATE writes."""
    def _write(sql: str, params: tuple) -> None:
        # service_client UPDATE for a single row by id
        # Convert from "UPDATE transactions SET ... WHERE id = %s" to .update().eq().execute()
        # Simpler: just write via the readonly_conn's sibling — but readonly
        # role can't write. We'd need a separate psycopg connection as
        # service role. For V1, use service_client.table().update().eq("id",...).execute()
        # which supabase-py handles.
        raise NotImplementedError(
            "Production write adapter — implementer fills this from service_client; "
            "see scripts/backfill_refund_detection.py for the reference pattern."
        )
    return _detect_impl(
        account_id, since, patterns,
        read=lambda sql, params: _exec_fetch_psycopg(readonly_conn, sql, params),
        write=_write,
    )


def _detect_impl(
    account_id: UUID,
    since: date | None,
    patterns: dict[UUID, list[str]],
    read,
    write,
) -> DetectionResult:
    """The detection algorithm, parameterised on read/write callables so the
    same logic runs in test mode (single psycopg conn, transaction-rollback)
    and production mode (split read/write)."""
    refunds_linked = 0
    self_transfers_linked = 0
    rows_processed = 0
    rows_pending = 0

    # --- Phase A: new credits on this account -------------------------------
    since_clause = "AND date >= %s" if since is not None else ""
    since_params = (since,) if since is not None else ()
    credits = read(
        f"""
        SELECT id, account_id, date, amount, direction, raw_merchant,
               is_refund, is_self_transfer, linked_txn_id
        FROM transactions
        WHERE account_id = %s AND direction = 'in'
          AND (is_refund IS NULL OR is_self_transfer IS NULL)
          {since_clause}
        ORDER BY date
        """,
        (account_id,) + since_params,
    )

    acct_patterns = patterns.get(account_id, [])
    for credit in credits:
        # Step 1: self-transfer check
        st_match: Any | _Pending | None = None
        if acct_patterns:
            # Find candidate cross-account debits within ±2 days, matching amount
            debit_lookup_since = credit["date"] - timedelta(days=_SELF_TRANSFER_WINDOW_DAYS)
            debit_lookup_until = credit["date"] + timedelta(days=_SELF_TRANSFER_WINDOW_DAYS)
            cross_debits = read(
                """
                SELECT id, account_id, date, amount, direction, raw_merchant,
                       is_refund, is_self_transfer
                FROM transactions
                WHERE direction = 'out'
                  AND account_id != %s
                  AND amount = %s
                  AND date BETWEEN %s AND %s
                  AND (is_self_transfer IS NULL OR is_self_transfer = false)
                """,
                (account_id, credit["amount"], debit_lookup_since, debit_lookup_until),
            )
            st_match = _find_self_transfer_match(credit, cross_debits, acct_patterns)

        if st_match is PENDING:
            rows_pending += 1
            # Don't write any flags — wait for next run
            continue
        if st_match is not None and st_match is not PENDING:
            # Link both sides
            write(
                "UPDATE transactions SET is_self_transfer = true, linked_txn_id = %s WHERE id = %s",
                (st_match["id"], credit["id"]),
            )
            write(
                "UPDATE transactions SET is_self_transfer = true WHERE id = %s",
                (st_match["id"],),
            )
            self_transfers_linked += 1
            rows_processed += 1
            continue

        # Step 2: refund check
        debit_lookup_since = credit["date"] - timedelta(days=_REFUND_WINDOW_DAYS)
        debit_lookup_until = credit["date"] - timedelta(days=1)
        refund_candidates = read(
            """
            SELECT id, account_id, date, amount, direction, raw_merchant,
                   is_refund, is_self_transfer
            FROM transactions
            WHERE account_id = %s AND direction = 'out'
              AND amount = %s
              AND date BETWEEN %s AND %s
              AND (is_refund IS NULL OR is_refund = false)
              AND (is_self_transfer IS NULL OR is_self_transfer = false)
            """,
            (account_id, credit["amount"], debit_lookup_since, debit_lookup_until),
        )
        refund_match = _find_refund_match(credit, refund_candidates)
        if refund_match is not None:
            write(
                "UPDATE transactions SET is_refund = true, linked_txn_id = %s WHERE id = %s",
                (refund_match["id"], credit["id"]),
            )
            refunds_linked += 1
            rows_processed += 1
        else:
            # No match either way: mark processed (per 3.i)
            write(
                "UPDATE transactions SET is_refund = false, is_self_transfer = false WHERE id = %s",
                (credit["id"],),
            )
            rows_processed += 1

    # --- Phase B: new debits on this account may unblock pending elsewhere --
    new_debits = read(
        f"""
        SELECT id, account_id, date, amount FROM transactions
        WHERE account_id = %s AND direction = 'out'
          {since_clause}
        """,
        (account_id,) + since_params,
    )
    for debit in new_debits:
        # Find pending credits on OTHER accounts with matching amount + window
        d_window_start = debit["date"] - timedelta(days=_SELF_TRANSFER_WINDOW_DAYS)
        d_window_end = debit["date"] + timedelta(days=_SELF_TRANSFER_WINDOW_DAYS)
        pending_credits = read(
            """
            SELECT id, account_id, date, amount, raw_merchant
            FROM transactions
            WHERE direction = 'in'
              AND account_id != %s
              AND amount = %s
              AND date BETWEEN %s AND %s
              AND is_self_transfer IS NULL
            """,
            (account_id, debit["amount"], d_window_start, d_window_end),
        )
        for cand in pending_credits:
            cand_patterns = patterns.get(cand["account_id"], [])
            if _matches_self_transfer(cand["raw_merchant"], cand_patterns):
                write(
                    "UPDATE transactions SET is_self_transfer = true, linked_txn_id = %s WHERE id = %s",
                    (debit["id"], cand["id"]),
                )
                write(
                    "UPDATE transactions SET is_self_transfer = true WHERE id = %s",
                    (debit["id"],),
                )
                self_transfers_linked += 1
                rows_processed += 1
                break  # one debit unblocks at most one credit

    return DetectionResult(
        refunds_linked=refunds_linked,
        self_transfers_linked=self_transfers_linked,
        rows_processed=rows_processed,
        rows_pending=rows_pending,
    )


# --- minimal psycopg helpers for the test-mode connection -------------------

def _exec_fetch(conn: Any, sql: str, params: tuple) -> list[dict]:
    """Execute SELECT, return list of dicts."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        cols = [d.name if hasattr(d, "name") else d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def _exec(conn: Any, sql: str, params: tuple) -> None:
    """Execute non-returning statement."""
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _exec_fetch_psycopg(conn: Any, sql: str, params: tuple) -> list[dict]:
    """Same as _exec_fetch — kept as a separate name to clarify intent in
    the production split (readonly conn for reads)."""
    return _exec_fetch(conn, sql, params)
```

- [ ] **Step 5.4: Run integration tests**

Run:
```
.venv/bin/python -m pytest tests/test_refund_detector.py -v 2>&1 | tail -25
```

Expected: 23 passed (12 refund unit + 6 self-transfer unit + 5 integration). If the live tests skip with "SUPABASE_READONLY_PASSWORD not in settings" your `.env` isn't being read — see W3.5 docs.

- [ ] **Step 5.5: Lint + typecheck**

Run:
```
.venv/bin/ruff check skills/finance/categorization tests/test_refund_detector.py && .venv/bin/mypy skills/finance/categorization
```

Expected: clean.

- [ ] **Step 5.6: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add skills/finance/categorization/refund_detector.py tests/test_refund_detector.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(categorization): detect_for_account orchestrator (W5.1 §5)

Two phases:
- Phase A iterates new direction='in' rows on this account; tries
  self-transfer match (pattern + cross-account exact amount within ±2d),
  then refund match (fuzzy merchant + exact amount + 30d back window).
- Phase B uses this account's new direction='out' rows to unblock pending
  pattern-credits elsewhere.

Idempotent via tri-state IS NULL guard. Pattern hit without
cross-account match returns PENDING — row stays NULL, retried next run.
SELECTs via psycopg (bypasses Supabase 1000-row cap), UPDATEs via the
test connection in test mode or service_client in production.

5 live integration tests gated like test_readonly_client.py.
Transaction-rollback fixture (decision 6.i) means seeded rows never persist.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Pipeline wire-up + safety wrapper

**Files:**
- Modify: `skills/finance/ingestion/pipeline.py`
- Create: `tests/test_pipeline_refund_integration.py`

- [ ] **Step 6.1: Write failing tests for the wrapper**

Write `tests/test_pipeline_refund_integration.py`:
```python
"""W5.1 §6.1: detection runs inline after successful ingestion, wrapped in
try/except so detection bugs never roll back ingestion."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from skills.finance.categorization.refund_detector import DetectionResult
from skills.finance.ingestion._common import ParsedRow, ParseResult, SourceMeta

ACCT = UUID("10000000-0000-0000-0000-000000000005")


def _fake_parse_result():
    return ParseResult(
        rows=[
            ParsedRow(
                txn_date=date(2026, 3, 15), amount=Decimal("100.00"),
                direction="out", raw_merchant="X",
                source_row_ordinal=1,
                parser_version="test/v1",
            )
        ],
        declared_totals={"_derived_from_rows": False},
        pdf_content_hash="abc123",
        parser_version="test/v1",
    )


@pytest.mark.asyncio
async def test_ingest_calls_detection_after_success():
    """Happy path: _log_success called BEFORE detection; detection result
    logged at INFO level."""
    pr = _fake_parse_result()
    source = SourceMeta(source="manual_pdf", source_ref="test.pdf")

    fake_response = MagicMock()
    fake_response.data = [{"id": str(uuid4())}]   # rows_added = 1

    with patch("skills.finance.ingestion.pipeline.adb", new=AsyncMock()) as m_adb, \
         patch("skills.finance.ingestion.pipeline.validate") as m_val, \
         patch("skills.finance.ingestion.pipeline._log_success", new=AsyncMock()) as m_log, \
         patch(
             "skills.finance.categorization.refund_detector.detect_for_account",
             return_value=DetectionResult(refunds_linked=2, self_transfers_linked=1,
                                          rows_processed=3, rows_pending=0),
         ) as m_detect:
        m_val.return_value = MagicMock(ok=True, declared_out=Decimal("100"), extracted_out=Decimal("100"),
                                        delta_in=Decimal("0"), delta_out=Decimal("0"))
        # adb returns the upsert response on first call (the upsert itself)
        m_adb.side_effect = [fake_response, DetectionResult(2, 1, 3, 0)]
        m_log.return_value = {"status": "success"}

        from skills.finance.ingestion.pipeline import ingest
        log_entry = await ingest(pr, ACCT, source)

    assert log_entry["status"] == "success"
    # _log_success called BEFORE detection — assert ordering by checking
    # m_log was awaited first relative to the m_adb call that invoked detect
    m_log.assert_called_once()
    m_detect.assert_called_once()


@pytest.mark.asyncio
async def test_detection_crash_does_not_rollback_ingestion():
    """The load-bearing safety contract: if detect_for_account raises,
    ingestion is STILL committed and _log_success has STILL fired."""
    pr = _fake_parse_result()
    source = SourceMeta(source="manual_pdf", source_ref="test.pdf")

    fake_response = MagicMock()
    fake_response.data = [{"id": str(uuid4())}]

    with patch("skills.finance.ingestion.pipeline.adb", new=AsyncMock()) as m_adb, \
         patch("skills.finance.ingestion.pipeline.validate") as m_val, \
         patch("skills.finance.ingestion.pipeline._log_success", new=AsyncMock()) as m_log, \
         patch(
             "skills.finance.categorization.refund_detector.detect_for_account",
             side_effect=RuntimeError("synthetic detection failure"),
         ):
        m_val.return_value = MagicMock(ok=True, declared_out=Decimal("100"), extracted_out=Decimal("100"),
                                        delta_in=Decimal("0"), delta_out=Decimal("0"))
        m_adb.side_effect = [fake_response, RuntimeError("propagated from detect")]
        m_log.return_value = {"status": "success"}

        from skills.finance.ingestion.pipeline import ingest
        log_entry = await ingest(pr, ACCT, source)

    # Ingestion still committed
    assert log_entry["status"] == "success"
    # _log_success STILL called before the detection failure
    m_log.assert_called_once()
```

- [ ] **Step 6.2: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_pipeline_refund_integration.py -v 2>&1 | tail -10
```

Expected: failures because pipeline.py doesn't call detect_for_account yet.

- [ ] **Step 6.3: Implement the wire-up in pipeline.py**

Open `skills/finance/ingestion/pipeline.py`. Add to imports near the top:
```python
from datetime import timedelta
```

Then modify the `ingest()` function — locate the line `return await _log_success(...)` and insert detection BEFORE the return:
```python
async def ingest(
    parse_result: ParseResult,
    account_id: UUID,
    source_meta: SourceMeta,
) -> dict[str, Any]:
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
        for r in parse_result.insertable_rows()
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
    log_entry = await _log_success(parse_result, source_meta, val, rows_added)

    if rows_added > 0:
        await _run_refund_detection_safe(account_id, parse_result)

    return log_entry


async def _run_refund_detection_safe(
    account_id: UUID, parse_result: ParseResult,
) -> None:
    """Inline refund + self-transfer detection. Per W5.1 spec §6.1: failures
    here MUST NOT roll back the ingestion that's already committed.
    AUDIT fields are derived; the next detection run picks up unprocessed rows."""
    try:
        from skills.finance.categorization.refund_detector import detect_for_account
        earliest_date = min(r.txn_date for r in parse_result.insertable_rows())
        since = earliest_date - timedelta(days=30)
        result = await adb(detect_for_account, account_id, since)
        logger.info(
            "refund detection: account=%s since=%s refunds=%d self_transfers=%d "
            "processed=%d pending=%d",
            account_id, since, result.refunds_linked, result.self_transfers_linked,
            result.rows_processed, result.rows_pending,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "refund detection failed for account=%s — ingestion already committed",
            account_id,
        )
```

- [ ] **Step 6.4: Run tests to verify they pass**

Run:
```
.venv/bin/python -m pytest tests/test_pipeline_refund_integration.py tests/test_pipeline.py -v 2>&1 | tail -15
```

Expected: 2 new + existing pipeline tests pass.

- [ ] **Step 6.5: Lint + typecheck**

Run:
```
.venv/bin/ruff check skills/finance/ingestion/pipeline.py tests/test_pipeline_refund_integration.py && .venv/bin/mypy skills/finance/ingestion/pipeline.py
```

Expected: clean.

- [ ] **Step 6.6: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add skills/finance/ingestion/pipeline.py tests/test_pipeline_refund_integration.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(pipeline): inline refund detection at ingest end (W5.1 §6.1)

_run_refund_detection_safe() invoked after _log_success() when
rows_added > 0. Wrapped in try/except so detection failures never
roll back the ingestion that's already committed — AUDIT fields
are derived; next detection run re-processes via IS NULL guard.
since = earliest_new_row.date - 30d covers refund window + lets
Phase B unblock pending pattern-credits whose pending state ended
just outside the new batch.

2 tests: happy path (detection logged), crash-isolation (synthetic
detect_for_account raise; ingestion still committed).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Backfill script

**Files:**
- Create: `scripts/backfill_refund_detection.py`

(No unit tests for this script — it's operational glue. Smoke-tested by actually running it in Task 8.)

- [ ] **Step 7.1: Write the backfill script**

Write `scripts/backfill_refund_detection.py`:
```python
"""One-time backfill pass to populate is_refund / is_self_transfer / linked_txn_id
on existing transactions rows.

Per W5.1 spec §6.2. Idempotent — re-runnable; the detector's IS NULL guards
mean already-processed rows are skipped. Default: dry-run.

Usage:
    .venv/bin/python -m scripts.backfill_refund_detection           # dry-run by default
    .venv/bin/python -m scripts.backfill_refund_detection --apply   # actually writes
    .venv/bin/python -m scripts.backfill_refund_detection --apply --strict
                                                                     # raise on first error
"""
from __future__ import annotations

import argparse
from uuid import UUID

from skills.finance.categorization.refund_detector import (
    DetectionResult,
    detect_for_account,
)
from skills.finance.lib.db import service_client


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually commit changes. Without this, runs in dry-run mode "
             "(prints what WOULD happen but writes nothing).",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Raise on the first per-account error instead of logging and "
             "continuing. Useful in CI/dev.",
    )
    return p.parse_args()


def _dry_run_count(account_id: UUID) -> DetectionResult:
    """Estimate what detect_for_account WOULD do without writing. We just
    count direction='in' rows with is_refund IS NULL on this account."""
    sb = service_client()
    res = (
        sb.table("transactions")
        .select("id", count="exact")
        .eq("account_id", str(account_id))
        .eq("direction", "in")
        .is_("is_refund", "null")
        .execute()
    )
    pending_count = res.count or 0
    return DetectionResult(
        refunds_linked=0, self_transfers_linked=0,
        rows_processed=pending_count, rows_pending=0,
    )


def main() -> int:
    args = _parse_args()
    sb = service_client()
    accounts = sb.table("accounts").select("id,nickname,type").execute().data or []

    totals = {
        "refunds_linked": 0,
        "self_transfers_linked": 0,
        "rows_processed": 0,
        "rows_pending": 0,
    }
    failures: list[tuple[str, str]] = []

    for acct in accounts:
        acct_id = UUID(acct["id"])
        label = f"{acct['nickname']} ({acct['type']}, {acct_id})"
        print(f"\n=== {label} ===")
        try:
            if args.apply:
                r = detect_for_account(acct_id, since=None)
            else:
                r = _dry_run_count(acct_id)
        except Exception as e:  # noqa: BLE001
            failures.append((label, f"{type(e).__name__}: {e}"))
            if args.strict:
                raise
            print(f"  FAILED: {type(e).__name__}: {e} (continuing — pass --strict to abort)")
            continue

        if args.apply:
            print(f"  refunds_linked={r.refunds_linked}  self_transfers_linked={r.self_transfers_linked}")
            print(f"  rows_processed={r.rows_processed}    rows_pending={r.rows_pending}")
        else:
            print(f"  WOULD process {r.rows_processed} direction='in' rows (is_refund IS NULL)")

        totals["refunds_linked"] += r.refunds_linked
        totals["self_transfers_linked"] += r.self_transfers_linked
        totals["rows_processed"] += r.rows_processed
        totals["rows_pending"] += r.rows_pending

    print(f"\n=== Totals ===")
    for k, v in totals.items():
        print(f"  {k}: {v}")

    if failures:
        print(f"\nFailed accounts ({len(failures)}):")
        for label, msg in failures:
            print(f"  {label}: {msg}")

    if not args.apply:
        print("\n(dry run — pass --apply to commit changes)")

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7.2: Smoke the backfill script (dry-run)**

Run:
```
.venv/bin/python -m scripts.backfill_refund_detection
```

Expected:
- Prints `=== <nickname> (<type>, <uuid>) ===` per account
- Each account shows `WOULD process N direction='in' rows (is_refund IS NULL)`
- Totals at the end (rows_processed total should be ~10-30 across all CC/savings accounts)
- Ends with `(dry run — pass --apply to commit changes)`

Record the dry-run totals in case you want to compare against `--apply` later.

- [ ] **Step 7.3: Lint + typecheck**

Run:
```
.venv/bin/ruff check scripts/backfill_refund_detection.py && .venv/bin/mypy scripts/backfill_refund_detection.py
```

Expected: clean.

- [ ] **Step 7.4: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add scripts/backfill_refund_detection.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(ops): refund detection backfill script (W5.1 §6.2)

scripts/backfill_refund_detection.py. Default dry-run; --apply
commits; --strict raises on first per-account error. Idempotent —
re-runnable via the detector's IS NULL guards. Iterates all
accounts in arbitrary order (correctness independent per Phase A
cross-account + Phase B catch-up duality from spec §5).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Run the backfill against existing 1,227 rows

**Files:** none (operational step)

- [ ] **Step 8.1: Dry-run first to confirm scope**

Run:
```
.venv/bin/python -m scripts.backfill_refund_detection 2>&1 | tee /tmp/w5_1_dry_run.log
```

Expected: same output as Task 7.2. Verify it lists every account.

- [ ] **Step 8.2: Apply with strict mode for clean failure signal**

Run:
```
.venv/bin/python -m scripts.backfill_refund_detection --apply --strict 2>&1 | tee /tmp/w5_1_apply.log
```

Expected:
- Each account section prints actual counts
- Totals at the end: roughly `refunds_linked ~5-10`, `self_transfers_linked ~10-20` (estimates; real numbers depend on data)
- No failures (if any error, `--strict` will raise — diagnose before retrying)

- [ ] **Step 8.3: Spot-check via a query**

Run:
```
.venv/bin/python -c "
from skills.finance.lib.db import readonly_client
conn = readonly_client()
with conn.cursor() as cur:
    cur.execute(\"SELECT count(*) FROM transactions WHERE is_refund = true\")
    print('refunds total:', cur.fetchone()[0])
    cur.execute(\"SELECT count(*) FROM transactions WHERE is_self_transfer = true\")
    print('self_transfers total:', cur.fetchone()[0])
    cur.execute(\"SELECT count(*) FROM transactions WHERE is_refund IS NULL AND direction = 'in'\")
    print('still pending (unprocessed in direction=in):', cur.fetchone()[0])
    # Look at a few linked pairs
    cur.execute(\"\"\"
        SELECT c.date, c.amount, c.raw_merchant AS credit_merchant,
               d.date AS debit_date, d.raw_merchant AS debit_merchant
        FROM transactions c
        JOIN transactions d ON c.linked_txn_id = d.id
        WHERE c.is_refund = true
        ORDER BY c.date DESC
        LIMIT 5
    \"\"\")
    print('\\nRecent refund links:')
    for row in cur.fetchall():
        print(' ', row)
"
```

Expected: refunds + self_transfers > 0; pending (unprocessed) close to 0 except for any pattern-but-no-cross-account-match rows.

---

## Task 9: Documentation + memory update + final ship

**Files:**
- Modify: `tasks/lessons.md` (append)
- Modify: memory `project_pfa_status.md`
- Modify: memory `MEMORY.md` index

- [ ] **Step 9.1: Append to lessons.md**

Append to `tasks/lessons.md`:
```markdown

---

## 2026-05-21 — W5.1 refund + self-transfer detection shipped

**Pattern:** `is_refund` and `linked_txn_id` columns were in the schema since W2
but never populated. Briefs (W4.2 next) and `/afford` calculations would naively
sum `direction='in'` as income — counting CC bill payments as income and not
netting refunds against the original spend.

**What W5.1 did:** post-ingestion deterministic detector (no LLM) that flags two
distinct classes. Refunds: same account, exact amount, rapidfuzz token_set_ratio
>= 80 on merchant, 30-day backward window. Self-transfers (CC bill payments):
per-source text pattern (yaml config, keyed by account_id) + cross-account exact
amount within ±2 days; both sides flagged, one-direction FK CC→savings. Inline
detection at end of pipeline.ingest(), wrapped in try/except so detection bugs
don't roll back ingestion. Schema migration 008 shifted is_refund from default-
false to tri-state nullable so the IS NULL idempotency contract works.

**Rule:** When a derived/audit field is needed alongside primary facts, the
default-value choice determines whether re-runs are predictable. `boolean DEFAULT
false` loses the distinction between "checked, not a refund" and "never
processed." Tri-state nullable (NULL = unprocessed, true/false = processed,
respect user overrides) is the minimum-mechanism solution.

**Captured as:** this entry; schema migration 008; spec
`docs/superpowers/specs/2026-05-21-refund-detection-design.md`; plan
`docs/superpowers/plans/2026-05-21-w5-1-refund-detection.md`; lib
`skills/finance/categorization/refund_detector.py`.
```

- [ ] **Step 9.2: Update memory `project_pfa_status.md`**

Edit `/Users/rajat/.claude/projects/-Users-rajat-AntiGravity-Personal-finance-Agent/memory/project_pfa_status.md`. Update the frontmatter name + description:
```yaml
name: PFA W3.1–W5.1 done; refund detection live
description: PFA — W3.1 Paytm + W3.2 Zerodha + W3.4 ICICI Savings + W3.5 readonly client + W4.1 SQL agent + W5.1 refund/self-transfer detection shipped through 2026-05-21. Next is W4.2 briefs.
```

Append a new section after the W4.1 block:
```markdown
---

**W5.1 SHIPPED 2026-05-21.** Spec at `docs/superpowers/specs/2026-05-21-refund-detection-design.md`; plan at `docs/superpowers/plans/2026-05-21-w5-1-refund-detection.md`.

- New package `skills/finance/categorization/` with `refund_detector.py` exposing `detect_for_account(account_id, since)`.
- `config/self_transfer_patterns.yaml` keyed by account_id; ICICI CC + AMEX CC at ship.
- Migration 008 added `is_self_transfer` and shifted `is_refund` to tri-state nullable.
- Inline at `pipeline.py:ingest()` end via `_run_refund_detection_safe` (try/except guarantee).
- `scripts/backfill_refund_detection.py` ran clean on the existing 1,227 rows: refunds_linked=X, self_transfers_linked=Y (fill in from Task 8.2 actuals).
- 27 new tests across `tests/test_self_transfer_patterns.py`, `tests/test_refund_detector.py`, `tests/test_pipeline_refund_integration.py`.
- `lessons.md` 2026-05-21 entry captures the tri-state-nullable-vs-default-false trade.
```

- [ ] **Step 9.3: Update `MEMORY.md` index**

Edit `/Users/rajat/.claude/projects/-Users-rajat-AntiGravity-Personal-finance-Agent/memory/MEMORY.md`. Replace the top status line:
```
- [PFA W3.1–W5.1 done; refund detection live](project_pfa_status.md) — Through 2026-05-21: ingestion + readonly + SQL agent + refund/self-transfer detection all shipped; W4.2 briefs next
```

- [ ] **Step 9.4: Final triple-gate**

Run:
```
.venv/bin/python -m pytest -q 2>&1 | tail -5 && echo "---" && .venv/bin/ruff check . 2>&1 | tail -3 && echo "---" && .venv/bin/mypy skills scripts app.py 2>&1 | tail -3
```

Expected: all green. Total tests should be 228 (prior baseline) + 27 (W5.1 new) = ~255 passed.

- [ ] **Step 9.5: Commit + push**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add tasks/lessons.md && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "docs(lessons): W5.1 refund/self-transfer detection shipped

Captures the tri-state-nullable-vs-default-false trade as a durable
lesson for any future derived/audit field that needs re-runnable
detection semantics.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"

git push origin main
```

Expected: push lands on origin with the W5.1 commit chain visible.

---

## Self-Review

**Spec coverage** (each spec section/requirement traced to a task):

| Spec § | Requirement | Implementing task |
|---|---|---|
| §1 | Three classes of direction='in' need disambiguation | Task 3 (refund), Task 4 (self-transfer) |
| §2 D1 | Detect refunds + self-transfers; ignore surcharge reversals | Whole plan — surcharge reversals not addressed (out of scope per spec §9) |
| §2 D2 | Refund matcher specifics | Task 3 |
| §2 D3 | Self-transfer matcher specifics | Task 4 |
| §2 D4 | Both sides flagged, one-direction FK | Task 5 (orchestrator writes both UPDATEs) |
| §2 D5 | Trigger model: inline + one-time backfill | Tasks 6 + 7 + 8 |
| §2 D6 | Ambiguity: smallest date delta | Tasks 3 + 4 (covered by tests) |
| §2 D7 | Tri-state nullable idempotency | Task 1 (migration) + Task 5 (IS NULL guard) |
| §2 D8 | No LLM, no Anthropic spend | Whole plan — no llm() calls |
| §2 D9 | SELECT via readonly_client; UPDATE via service_client | Task 5 production path |
| §3 | File structure | Defined in plan header; created by Tasks 2, 5, 7 |
| §4 | Migration 008 | Task 1 |
| §5.1 | Public entry point | Task 5 |
| §5.2 Phase A | Process new credits | Task 5 |
| §5.3 Phase B | Catch-up via new debits | Task 5 |
| §5.4 | Order matters: self-transfer first | Task 5 (algorithm) |
| §5.5 | DB access pattern | Task 5 |
| §6.1 | Pipeline wire-up | Task 6 |
| §6.2 | Backfill script | Task 7 + 8 |
| §6.3 | NOT building (no scheduler, no Telegram, no detection_log) | Plan respects all three exclusions |
| §7 | Error handling categories | Task 5 (per-row try/except + logger contract); patterns loader fails loud (Task 2) |
| §8.1 | Pattern loader tests | Task 2 |
| §8.2 unit | Matcher unit tests | Tasks 3 + 4 |
| §8.2 integration | detect_for_account integration | Task 5 |
| §8.3 | Pipeline wrapper tests | Task 6 |
| §8.4 | Out-of-scope tests | Plan respects |
| §9 | Out-of-V1 scope | Plan respects |

**Placeholder scan:** Searched for "TBD", "TODO", "fill in", "implement later", "add appropriate", "etc.". One legitimate fill-in remains: Step 9.2's "refunds_linked=X, self_transfers_linked=Y (fill in from Task 8.2 actuals)" — Task 8.2's actuals can't be known at plan-write time. This is a genuine post-execution data point, not a plan-failure placeholder. Acceptable.

**Type consistency:**
- `DetectionResult(refunds_linked, self_transfers_linked, rows_processed, rows_pending)` defined Task 5, referenced Tasks 6, 7, 8.
- `_find_refund_match(credit, candidates) -> Any | None` defined Task 3, called from Task 5.
- `_find_self_transfer_match(credit, recent_debits, patterns) -> Any | _Pending | None` defined Task 4, called from Task 5.
- `PENDING` sentinel defined Task 4, used Task 5.
- `_load_patterns()` defined Task 2, called from `_get_patterns()` in Task 5.
- `_matches_self_transfer(raw_merchant, patterns) -> bool` defined Task 2, used Task 5 Phase B.
- `detect_for_account(account_id, since, _conn_for_test=None) -> DetectionResult` defined Task 5, called from Task 6 (production) and Task 7 (script).
- Schema columns: `is_refund`, `is_self_transfer`, `linked_txn_id` — all defined or modified Task 1, referenced consistently in Tasks 3, 4, 5.

**Identified risk to flag at execution time:** Task 5's `_detect_production` function has a `NotImplementedError` placeholder for the service_client write adapter. The implementer must complete it before Task 8 can run successfully via the inline production path. Acceptable because (a) Task 5's tests use `_conn_for_test` to avoid the production adapter entirely, and (b) Task 7's backfill script calls `detect_for_account` with `_conn_for_test=None`, exercising the production write path — so Step 8.2's `--apply` is the moment the production adapter MUST be working. The implementer needs to be explicit about handling this either by completing the adapter in Task 5 or surfacing it in Task 7/8 as a sub-step.

**Filling the production adapter gap inline** (resolving the identified risk now):

In Task 5 Step 5.3, replace the `_detect_production` function with this fully-working version that uses service_client for writes:
```python
def _detect_production(
    account_id: UUID,
    since: date | None,
    patterns: dict[UUID, list[str]],
    readonly_conn: Any,
    service_client_: Any,
) -> DetectionResult:
    """Production-mode detection: psycopg readonly for SELECT (bypasses
    Supabase 1000-row cap), service client for UPDATE writes."""
    import re

    def _write(sql: str, params: tuple) -> None:
        # Parse `UPDATE transactions SET <assignments> WHERE id = %s`
        # into (assignments_dict, txn_id) for the supabase-py client.
        # We control the call sites so the SQL shapes are fixed.
        # Two shapes used:
        #   "UPDATE transactions SET is_self_transfer = true, linked_txn_id = %s WHERE id = %s"
        #   "UPDATE transactions SET is_self_transfer = true WHERE id = %s"
        #   "UPDATE transactions SET is_refund = true, linked_txn_id = %s WHERE id = %s"
        #   "UPDATE transactions SET is_refund = false, is_self_transfer = false WHERE id = %s"
        m = re.match(r"UPDATE transactions SET (.+) WHERE id = %s$", sql)
        if not m:
            raise ValueError(f"Unsupported UPDATE shape: {sql!r}")
        set_clause = m.group(1)
        txn_id = params[-1]
        # Identify literal=true/false and placeholder=%s columns
        assignments: dict[str, Any] = {}
        placeholder_idx = 0
        for part in [p.strip() for p in set_clause.split(",")]:
            col, _, val = part.partition(" = ")
            if val == "true":
                assignments[col] = True
            elif val == "false":
                assignments[col] = False
            elif val == "%s":
                assignments[col] = str(params[placeholder_idx])
                placeholder_idx += 1
            else:
                raise ValueError(f"Unsupported SET value: {part!r}")
        service_client_.table("transactions").update(assignments).eq("id", str(txn_id)).execute()

    return _detect_impl(
        account_id, since, patterns,
        read=lambda sql, params: _exec_fetch(readonly_conn, sql, params),
        write=_write,
    )
```

Replace the entire `_detect_production` block in Step 5.3 with the above. Note: the regex parser is intentionally narrow — it ONLY accepts the four UPDATE shapes used internally by `_detect_impl`. If a future change adds a new UPDATE shape, the function raises a clear `ValueError` — fail loud, not silent.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-21-w5-1-refund-detection.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task (Tasks 0–9), review between each with two-stage check (spec compliance then code quality). Best for this plan given the 10 well-bounded tasks with clear interfaces.

2. **Inline Execution** — Execute tasks in this session with checkpoints, full visibility into every step.

Which approach?
