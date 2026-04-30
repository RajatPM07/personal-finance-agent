# ICICI Savings Statement (PDF) Ingestion — Design Spec

**Status:** Locked v1
**Date:** 2026-04-30
**Supersedes:**
- Roadmap r2 §2 W3.4 ("Payslip parser") — replaced by this spec. ICICI Savings captures salary credits via the bank statement transaction stream; payslip component-level extraction is deferred to V2 (when tax reasoning activates).
- Routing spec `2026-04-30-llm-routing-anthropic-zero-spend.md` §3 `payslip_extraction` task entry — removed in the same commit (v2→v3 amendment of that spec).

**PRD references:** §11 Week 3 (build sequence — savings ingestion was implicit via "remaining sources"), §6.4 (model routing — deterministic parser, no LLM), §7 (schema — `transactions` table extended), §19.5 (`import_hash` Mode B).
**Lessons referenced:** ICICI CC parser pattern (W2), Paytm flag-don't-drop pattern (W3.1), Supabase insert dict typing (lessons.md 2026-04-26), `password_lookup` for ICICI CC reused.
**Brainstorm artifact:** Conversation 2026-04-30 — D1-D7 ratified by user, including resolution of the UPI dual-entry overlap with the existing Paytm ingestion.

## 1. Goal

Stand up an end-to-end ingestion path for ICICI Savings (account `…1896`) e-statement PDFs. By close of W3.4:
- Two months of historical statements (Jan + Feb 2026) populate `transactions` with non-UPI rows only — UPI rows are skipped because Paytm passbook is the system-of-record for UPI activity (D1).
- A new `txn_mode TEXT` column on `transactions` captures the payment rail (NEFT, IMPS, ATM, BIL/PAY, SAL, INT.PD, etc.) for analysis and W5 categorization.
- Validator confirms extracted deposits + withdrawals match the per-page subtotals on the PDF (within ±₹1 each), using the same flag-don't-drop pattern as the Paytm parser.
- Re-dropping the same PDF produces `skipped_duplicate` (idempotency, Mode B hash).
- Folder watcher + Telegram document handler dispatch `icici_savings_*.pdf` automatically.

## 2. Scope

**In scope:**
- New deterministic parser `parsers/icici_savings.py` using `pikepdf` (decrypt) + `camelot-py[stream]` and/or `pdfplumber` text scan (extract).
- Migration `007_txn_mode.sql` adding one nullable column to `transactions`.
- Updates to `_common.detect_bank_from_filename` to disambiguate `icici_savings` from `icici_cc`.
- Updates to `folder_watcher.EXPECTED_EXTENSION`, `ACCOUNT_IDS`, and `dispatch_to_parser` for `icici_savings`.
- Validator runs against per-page `Total:` subtotals (page-aggregate ≈ row-aggregate within ±₹1).
- Golden-fixture TDD against `tests/golden_fixtures/icici_savings_2026_01.pdf` and `_02.pdf`.
- Backfill of those 2 fixtures upon ship; user runs the rest of the 9-month historical at his own pace afterwards.
- New `is_upi_skip: bool = False` field on `ParsedRow`. Parser sets it; `ParseResult.insertable_rows()` extended to also filter on `is_upi_skip=True`.

**Explicitly deferred:**
- Payslip component-level extraction (BASIC, HRA, PF, IT etc.). V2 — depends on tax reasoning surface (PRD §6.5 affordability is satisfied by gross-from-bank-credit alone).
- HDFC Savings ingestion. Out of V1 entirely (per roadmap r2, savings was scope-cut; we're now restoring only ICICI savings because of D1's UPI consolidation logic).
- Self-transfer detection between own accounts (e.g., HDFC → ICICI). Showing as a row each side is acceptable for V1; W5 categorizer can flag `txn_mode IN ('IMPS','NEFT')` + counterparty-self-handle as "self-transfer / exclude from spend".
- Cross-source dedup beyond the UPI rule (the rolling-statement overlap deferred to W4.3 `/dedup` already covers this).
- Row-level balance-arithmetic validator (the "β" option in brainstorm). Deferred unless α reveals a class of silent bugs.

## 3. Architecture

```
~/finance-inbox/icici_savings_2026_02.pdf
        │
        ▼
folder_watcher (watchdog)
        │  detect_bank → "icici_savings"
        │  ext check → ".pdf" ✓
        │  password_lookup("icici_savings", last4="1896")
        │   └─ V1 fallback: same value as icici_cc (per credentials.yaml)
        ▼
dispatch_to_parser(icici_savings)
        │
        ▼
parsers/icici_savings.parse(file_path, password)
        │  pikepdf decrypt → temp unencrypted PDF
        │  pdfplumber extract_tables() → page-1 summary, per-page Total: rows
        │  camelot-py (stream) OR positional text scan → transaction rows
        │  classify each row: is_upi_skip = (mode=="UPI" OR particulars.startswith("UPI/"))
        │  return ParseResult{rows (all), declared_totals (page-subtotal sum), pdf_hash, version}
        ▼
ingestion/pipeline.ingest()
        │  validate(pr): sum-of-out vs total_spends, sum-of-in vs total_credits, ±₹1
        │  for r in pr.insertable_rows(): build insert dict (incl. txn_mode + category_hint=NULL)
        │  upsert(on_conflict='import_hash')
        │  log to ingestion_log
        ▼
Telegram summary message
        "📥 ICICI SAVINGS icici_savings_2026_02.xlsx ingested
         N rows (M UPI rows skipped — captured via Paytm), totals match ✓"
```

## 4. File structure (new + modified)

```
skills/finance/ingestion/
├── parsers/
│   ├── icici_cc.py            (unchanged)
│   ├── amex_cc.py             (unchanged)
│   ├── paytm_upi.py           (unchanged)
│   └── icici_savings.py        ← NEW
├── _common.py                 ← MODIFIED (Bank literal, ParsedRow.is_upi_skip,
│                                          ParsedRow.txn_mode, ParseResult.insertable_rows
│                                          extended; detect_bank_from_filename rule for savings)
├── folder_watcher.py          ← MODIFIED (ACCOUNT_IDS, EXPECTED_EXTENSION, dispatch branch)
├── pipeline.py                ← MODIFIED (txn_mode field added to insert dict)
└── statement_validator.py     (unchanged — uses existing total_spends/total_credits)

migrations/
└── 007_txn_mode.sql            ← NEW

tests/
├── test_icici_savings_parser.py    ← NEW (golden-file)
├── test_icici_savings_helpers.py   ← NEW (small unit, no fixture)
└── golden_fixtures/
    ├── icici_savings_2026_01.pdf   (already in place, gitignored, encrypted)
    └── icici_savings_2026_02.pdf   (already in place, gitignored, encrypted)
```

## 5. Source layer

### 5.1 `_common.detect_bank_from_filename` — disambiguation

The function currently returns `icici_cc` when filename has `icici` AND `cc` tokens. For savings, add a `savings` (or `sav`) token check:

```python
def detect_bank_from_filename(filename: str) -> Bank | None:
    name = filename.lower()
    tokens = set(re.split(r"[^a-z0-9]+", name))
    has_icici = "icici" in tokens
    has_amex = ("amex" in tokens) or ("american" in tokens)
    has_paytm = "paytm" in tokens
    has_savings = ("savings" in tokens) or ("sav" in tokens)
    has_cc = "cc" in tokens

    # Disambiguation: ICICI must specify CC vs SAVINGS
    if has_icici and has_cc and has_savings:    return None  # ambiguous
    if has_icici and has_cc:                    return "icici_cc"
    if has_icici and has_savings:               return "icici_savings"
    if has_icici:                               return None  # 'icici' alone — needs disambiguator

    # Other banks unchanged
    matches = sum([has_amex, has_paytm])
    if matches > 1: return None
    if has_amex:    return "amex_cc"
    if has_paytm:   return "paytm_upi"
    return None
```

Filename `icici_savings_2026_02.pdf` → `"icici_savings"`. Filename `icici_cc_2025_06.pdf` → `"icici_cc"`. Filename `icici_2026_02.pdf` (no disambiguator) → `None` → existing rejection path.

### 5.2 `folder_watcher` updates

```python
ACCOUNT_IDS["icici_savings"] = UUID("10000000-0000-0000-0000-000000000001")  # ICICI Savings 1896 per 003_seed.local.sql
EXPECTED_EXTENSION["icici_savings"] = ".pdf"
```

`dispatch_to_parser` gets a fourth branch:

```python
elif bank == "icici_savings":
    from skills.finance.ingestion.parsers.icici_savings import parse as savings_parse
    password = await asyncio.to_thread(password_lookup, "icici_savings", "1896")
    parse_result = await asyncio.to_thread(savings_parse, file_path, password)
    source = SourceMeta(source="manual_pdf", source_ref=file_path.name)
```

### 5.3 Credentials

`credentials.yaml` (gitignored) gains a new entry:

```yaml
icici_savings_1896:
  value: "<same-password-as-icici_cc-1008>"   # user copied per their note "same as ICICI CC"
  pattern: "Same convention as ICICI CC — see icici_cc_1008 entry."
```

`password_lookup("icici_savings", last4="1896")` returns this value. The function already supports the `<bank>_<last4>` pattern; no code change there.

### 5.4 Telegram document handler

No changes. Once `detect_bank_from_filename` knows `"icici_savings"`, filename-prefixed savings PDFs forwarded via Telegram route correctly through the existing handler.

## 6. Parse layer (`parsers/icici_savings.py`)

### 6.1 Public surface

```python
__parser_version__ = "icici-savings-pdf/v1"

def parse(pdf_path: Path, password: str) -> ParseResult:
    """Decrypt, extract page-1 summary anchors, extract per-page transaction
    rows, sum page subtotals, classify UPI rows for skip-at-insert."""
```

### 6.2 PDF decryption

```python
import pikepdf, tempfile
from pathlib import Path

def _decrypt_to_temp(pdf_path: Path, password: str) -> Path:
    """Returns path to a temp unencrypted copy. Caller is responsible for
    cleanup. Raises pikepdf.PasswordError if password wrong (let it propagate
    so pipeline logs status='failed' rather than total_check_failed)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    with pikepdf.open(pdf_path, password=password) as pdf:
        pdf.save(tmp_path)
    return tmp_path
```

### 6.3 Page-by-page table extraction strategy

The transaction table spans 1+ pages with the header `[DATE | MODE | PARTICULARS | DEPOSITS | WITHDRAWALS | BALANCE]` repeated per page, and a `Total:` row at the bottom of each transaction page giving page-subtotals.

**Recommended approach (try in order):**

1. **Primary: `camelot-py` with `flavor="stream"`** (already in `pyproject.toml` deps). Stream flavor is designed for whitespace-aligned tables without lattice borders, which is the layout here. Returns `pandas.DataFrame` per detected table.

2. **Secondary: `pdfplumber.extract_tables()`** to capture the per-page `Total:` row reliably (it parsed those cleanly during inspection 2026-04-30). Use this even if approach 1 captures the body — the `Total:` row is a separate small table on each page.

3. **Tertiary fallback: anchor-based text scan.** If camelot produces noise on a particular page:
   - Find the line `DATE MODE PARTICULARS DEPOSITS WITHDRAWALS BALANCE` (page header).
   - From the next line, scan for date-prefixed lines (`^\d{2}-\d{2}-\d{4}\s+`).
   - Stop when `Total:` appears OR the line `ACCOUNT TYPE ACCOUNT NUMBER MICR CODE` appears (nominee block on last txn page) OR end-of-page.

The implementation can choose at parse time based on what produces clean output for the fixture; spec is agnostic. **Acceptance criterion (§17): the validator passes on both fixtures, regardless of which extraction method ships.**

### 6.4 Multi-line PARTICULARS handling

PARTICULARS sometimes spans 2 lines (long descriptions). Approach:

```python
# pseudocode
rows = []
current_row = None
for line in extracted_lines:
    if matches_date_prefix(line):
        if current_row: rows.append(current_row)
        current_row = parse_data_row(line)
    elif current_row and not matches_section_anchor(line):
        # continuation: append to PARTICULARS
        current_row.particulars += " " + line.strip()
if current_row: rows.append(current_row)
```

### 6.5 Per-row column construction

For each non-skipped row, build a `ParsedRow`:

| Field | Source |
|---|---|
| `account_id` | `ACCOUNT_IDS["icici_savings"]` |
| `txn_date` | DATE column → `_parse_savings_date` (try `%d-%m-%Y`, `%d/%m/%Y`, `%Y-%m-%d`) |
| `amount` | `abs(DEPOSITS)` if non-zero else `abs(WITHDRAWALS)`; signed direction inferred (below) |
| `direction` | `"in"` if DEPOSITS column has a value, `"out"` if WITHDRAWALS column has a value |
| `raw_merchant` | PARTICULARS column with whitespace normalized |
| `txn_mode` | MODE column normalized (UPI / NEFT / IMPS / ATM / BIL/PAY / SAL / INT.PD / TFR / etc.) |
| `source_row_ordinal` | 1..N contiguous over `result.rows` (CLAUDE.md test invariant) |
| `is_upi_skip` | True iff `txn_mode == "UPI"` OR `raw_merchant.startswith("UPI/")` (D7) |
| `is_amex_routed` | False (Paytm-only field; default) |
| `is_self_transfer` | False (Paytm-only field; default) |
| `category_hint` | None (Paytm-only field; default — bank statement has no pre-tag) |

### 6.6 Page-subtotal extraction (validator inputs)

Each transaction page has a row at the bottom matching `['', '', 'Total:', <deposits>, <withdrawals>, <closing_balance>]` (extract via `pdfplumber.extract_tables()` reliably). Capture all of them; the statement-period totals are the sums:

```python
def _extract_page_subtotals(tmp_pdf: Path) -> dict:
    """Returns {'total_spends': sum_of_withdrawals, 'total_credits': sum_of_deposits,
                'closing_balance': last_page_balance}"""
    total_in  = Decimal("0")
    total_out = Decimal("0")
    closing   = None
    with pdfplumber.open(tmp_pdf) as pdf:
        for page in pdf.pages:
            for tbl in page.extract_tables() or []:
                # The Total: row is typically a single-row table with 'Total:'
                # in column index 2. Defensive scan:
                for row in tbl:
                    if row and any((cell or "").strip() == "Total:" for cell in row):
                        # Columns: ['', '', 'Total:', deposits, withdrawals, balance]
                        deposits   = _decimal_from_indian_str(row[3])
                        withdrawls = _decimal_from_indian_str(row[4])
                        bal        = _decimal_from_indian_str(row[5])
                        total_in  += deposits
                        total_out += withdrawls
                        closing   = bal     # last-seen wins → closing balance
    return {
        "total_spends":     total_out,
        "total_credits":    total_in,
        "closing_balance":  closing,
        "_derived_from_rows": False,
    }
```

(Reuse `_decimal_from_indian_str` from `parsers/paytm_upi.py` if ergonomic, or inline a small copy.)

### 6.7 ParseResult assembly

```python
declared_totals = _extract_page_subtotals(tmp_pdf)
rows            = _extract_transaction_rows(tmp_pdf)   # all rows incl. UPI-skip-flagged
pdf_hash        = _sha256_file(pdf_path)               # original encrypted file's hash

return ParseResult(
    rows=rows,
    declared_totals=declared_totals,
    pdf_content_hash=pdf_hash,
    parser_version=__parser_version__,
)
```

`pdf_content_hash` uses the **encrypted source file's bytes**, not the temp decrypted copy — so re-decrypting the same input yields the same hash regardless of pikepdf serialization differences. This matches the ICICI CC parser's convention.

## 7. Validate layer

### 7.1 Page-subtotal aggregation (D2 = α)

`statement_validator.validate(pr)` is unchanged. Math:

```python
extracted_in  = sum(r.amount for r in pr.rows if r.direction == "in")
extracted_out = sum(r.amount for r in pr.rows if r.direction == "out")
delta_in      = abs(declared_in  - extracted_in)
delta_out     = abs(declared_out - extracted_out)
ok            = delta_in <= ₹1 AND delta_out <= ₹1
```

**Critical: validator runs over `pr.rows` — including UPI-skip-flagged rows**, because the page subtotals on the PDF include UPI activity. The flag-don't-drop pattern (mirror Paytm) keeps validator math aligned with what the PDF declares; UPI rows are filtered later at insert via `pr.insertable_rows()`.

### 7.2 No row-level balance arithmetic in V1

Spec D2 = α (page-subtotal only). β (per-row balance chain check) is deferred unless α reveals a class of silent bugs. Rationale captured in brainstorm: β catches sign-error / row-swap class bugs but is cheaper to catch via golden-fixture parser tests than at runtime; multi-line PARTICULARS handling makes β prone to false-positive `total_check_failed` if row assembly is slightly off.

## 8. Persist layer

### 8.1 Schema migration `007_txn_mode.sql`

```sql
-- 007_txn_mode.sql
-- W3.4 ICICI Savings parser populates this column from the PDF's MODE column
-- (UPI/NEFT/IMPS/ATM/BIL/PAY/SAL/INT.PD/TFR/etc.). ICICI CC, AMEX CC, and Paytm
-- rows leave this NULL — those parsers don't have a structured payment-rail
-- field. W5 normalization can use txn_mode as a strong prior for category
-- baselining (ATM → "Cash Withdrawal", SAL → "Salary", BIL/PAY → "Bills", etc.).

ALTER TABLE transactions
  ADD COLUMN txn_mode TEXT;

COMMENT ON COLUMN transactions.txn_mode IS
  'Payment rail / mode from bank statements (UPI, NEFT, IMPS, ATM, BIL/PAY, SAL, INT.PD, TFR, etc.). Populated by ICICI Savings parser; NULL for credit-card and Paytm parsers.';
```

No backfill — existing 1,220 rows stay NULL.

### 8.2 Hash mode (D6)

Mode B per CLAUDE.md invariant 3:

```
import_hash = sha256(
    account_id ||
    txn_date ||
    amount ||
    normalized_desc ||
    pdf_content_hash ||      # sha256 of original encrypted PDF bytes
    source_row_ordinal ||
    parser_version
)
```

Re-dropping the same PDF produces identical hashes → `skipped_duplicate`. Re-issued statements with byte drift → new `pdf_content_hash` → re-ingestion under new ordinals; the rolling-statement overlap problem is the same as ICICI CC's, deferred to W4.3 `/dedup`.

### 8.3 Pipeline behavior

`pipeline.ingest()` is mostly unchanged. Three small additions:

1. `_build_insert_row` adds `"txn_mode": r.txn_mode` to the dict written to `transactions`.
2. The iteration uses `parse_result.insertable_rows()` (already done in W3.1 for Paytm) — `is_upi_skip=True` rows are filtered automatically once `insertable_rows()` is extended to also drop on `is_upi_skip`.
3. The success log reports skipped UPI count in `error_msg` for visibility (rendered by Telegram summary downstream).

### 8.4 `_common` extensions

`ParsedRow` gets two new optional fields (existing 3 + new 2 = 5):

```python
@dataclass(frozen=True)
class ParsedRow:
    txn_date: date
    amount: Decimal
    direction: Literal["in", "out"]
    raw_merchant: str
    source_row_ordinal: int
    # Paytm-only
    is_amex_routed: bool = False
    is_self_transfer: bool = False
    category_hint: str | None = None
    # ICICI Savings W3.4
    is_upi_skip: bool = False           # True → drop at insert (D1: Paytm = UPI source-of-truth)
    txn_mode: str | None = None         # UPI/NEFT/IMPS/ATM/BIL/PAY/SAL/INT.PD/TFR/etc.; NULL for non-savings parsers
```

`ParseResult.insertable_rows()` extends:

```python
def insertable_rows(self) -> list[ParsedRow]:
    return [r for r in self.rows if not r.is_amex_routed and not r.is_upi_skip]
```

`Bank` literal extends to `Literal["icici_cc", "amex_cc", "paytm_upi", "icici_savings"]`.

## 9. Telegram review flow

After ingestion, `_send_summary` in `folder_watcher.py` posts plain text (no `parse_mode`, per the bot-layer fix in commit `464f8cf`):

```
📥 ICICI SAVINGS icici_savings_2026_02.pdf ingested
N rows, ₹<deposits-total> deposits (declared ₹<page-sum>) — totals match ✓
₹<withdrawals-total> withdrawals (declared ₹<page-sum>) — totals match ✓
M UPI rows skipped at insert (Paytm passbook is UPI source-of-truth).
Closing balance: ₹<closing>
```

If totals fail, `error_msg` includes both deltas + the skipped-UPI count for debugging.

## 10. Backfill mechanics

1. Once the parser ships + tests pass + app restarted via `launchctl kickstart -k`, drop a savings PDF with the canonical filename:
   ```
   ~/finance-inbox/icici_savings_<YYYY>_<MM>.pdf
   ```
2. Folder watcher's `on_created` (or `on_moved`) handler dispatches → ~30-70 transaction rows land per statement → Telegram summary confirms.
3. Verify via Supabase: `SELECT count(*) FROM transactions WHERE account_id = '10000000-0000-0000-0000-000000000001';` → expected ~30-70 rows per statement.
4. Iterate at the user's pace — no required ordering. Idempotency means re-drops are safe.

V1 backfill = the 2 fixtures upon W3.4 close. Full 9-month backfill is the user's choice afterwards.

## 11. Error handling matrix

| Condition | Behavior | `ingestion_log.status` |
|---|---|---|
| File parses cleanly, both totals match | rows inserted (UPI dropped), summary sent | `success` |
| File re-dropped (same content) | no rows inserted | `skipped_duplicate` |
| Deposits total mismatch (>₹1) | no rows inserted, alert sent | `total_check_failed` |
| Withdrawals total mismatch (>₹1) | no rows inserted, alert sent | `total_check_failed` |
| Sheet / page-1 summary unparseable | no rows inserted, alert sent with first 10 lines preview | `failed` |
| Password wrong (pikepdf raises) | no rows inserted, alert sent | `failed` |
| MODE column not extractable for some rows | parser fills `txn_mode=None` for those rows; logs warning | (no log impact) |
| UPI row encountered | row flagged is_upi_skip, kept in result.rows for validation, dropped at insert | (counted in success message) |
| Multi-line PARTICULARS | continuation lines appended to row's `raw_merchant` | (no log impact) |

## 12. Testing strategy

Golden-file TDD per `tests/test_paytm_upi_parser.py` pattern.

`tests/test_icici_savings_parser.py` — fixture-dependent, skips when fixtures missing:

- `test_parser_version_string` — `__parser_version__ == "icici-savings-pdf/v1"`
- `test_parse_returns_nonempty_parseresult` — fixture parses, both totals populated, hash 64 chars.
- `test_savings_ordinals_contiguous_1_to_n` — CLAUDE.md test invariant.
- `test_savings_validator_passes_on_real_fixtures` — validator returns ok on both Jan + Feb fixtures.
- `test_savings_upi_rows_flagged_not_dropped` — at least one row has `is_upi_skip=True`; that row appears in `result.rows` but NOT in `result.insertable_rows()`.
- `test_savings_txn_mode_populated_for_non_upi` — for non-UPI rows, `txn_mode` is non-NULL and matches one of the expected MODE values.
- `test_savings_parsed_row_fields_well_formed` — amount > 0, direction in {in, out}, ordinal ≥ 1.
- `test_savings_re_drop_is_idempotent` — re-running parse on same file produces identical hash + identical row import_hashes.

`tests/test_icici_savings_helpers.py` — pure-fn unit tests, no fixture:

- `test_is_upi_skip_true_for_mode_upi` — `_is_upi_skip(mode="UPI", particulars="...")` returns True.
- `test_is_upi_skip_true_for_particulars_upi_prefix` — `_is_upi_skip(mode="OTHER", particulars="UPI/SOMEONE...")` returns True.
- `test_is_upi_skip_false_for_neft_neft` — neither condition met, returns False.
- `test_decimal_from_indian_str_negative_signed` — handles `-1,234.56` and `+5,000.00` returning the signed Decimal (consistent with paytm helper if reused).
- `test_savings_continuation_line_appended_to_particulars` — synthetic 2-row dataframe where row N has no date but row N-1 does → row N's content appended to row N-1's `raw_merchant`.

## 13. New dependencies

None. `pikepdf`, `pdfplumber`, `camelot-py`, and `pandas` are all already pinned in `pyproject.toml` from W1/W2.

## 14. Code changes outside `skills/finance/ingestion/`

- `migrations/007_txn_mode.sql` — new file. Applied via psql in the implementation phase (matches `005_category_hint.sql` workflow).
- `credentials.yaml` (gitignored) — user adds `icici_savings_1896` entry copying the value from `icici_cc_1008`. Done outside the implementation flow (manual user step before backfill).
- `pyproject.toml` — no change.
- `CLAUDE.md` — no change (no new invariants).

## 15. Open implementation risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| `camelot-py` produces noisy output on whitespace-aligned tables | Medium | Anchor-based text scan fallback (§6.3 tertiary). Acceptance is "validator passes" — implementation chooses best path per fixture. |
| MODE column extraction unreliable for some rows | Medium | UPI-skip rule uses OR-logic against PARTICULARS (D7); other modes default to NULL and log a warning rather than failing. |
| Multi-line PARTICULARS rows mis-counted by row-assembly | Medium | Test `test_savings_continuation_line_appended_to_particulars` covers the pattern; α validator catches aggregate mismatches if assembly is wrong. |
| ICICI re-issues a statement → byte-different PDF, fresh hash → re-ingestion | Low | Mode B hash includes ordinal + parser_version; rolling-statement overlap deferred to W4.3 `/dedup`. |
| `is_upi_skip` rule false-positive (e.g., a legitimate non-UPI row whose PARTICULARS happens to contain "UPI/") | Low | `raw_merchant.startswith("UPI/")` is restrictive enough that this is unlikely. If observed, narrow to `MODE == "UPI"` only. |
| User has UPI handles linked to *other* banks (HDFC) — those Paytm rows won't appear in ICICI savings at all | Already handled | Per Paytm summary breakdown: 522 from HDFC, 67 from ICICI — the 67 are exactly the ones we'd see in ICICI savings. The 522 stay covered only by Paytm passbook. No data loss. |

## 16. Acceptance criteria

By close of W3.4:

- [ ] `parsers/icici_savings.py` lands; all 8 fixture-dependent + 5 helper tests pass.
- [ ] `migrations/007_txn_mode.sql` applied to Supabase; `transactions` has `txn_mode` column.
- [ ] `_common.detect_bank_from_filename` recognizes `icici_savings` and disambiguates from `icici_cc`.
- [ ] `_common.ParsedRow` has the two new fields (`is_upi_skip`, `txn_mode`); `ParseResult.insertable_rows()` filters both flags.
- [ ] Folder watcher dispatch tested: dropping a savings PDF with canonical name → ~30-70 rows land in `transactions`.
- [ ] `ingestion_log` shows `status=success` with both totals matching.
- [ ] Re-dropping the same PDF produces `skipped_duplicate` (idempotency).
- [ ] Telegram summary message shows correct row count, both totals, skipped-UPI count, closing balance.
- [ ] `make lint && make typecheck && make test` all green.
- [ ] No regression in existing ICICI CC, AMEX CC, Paytm parsers (run their tests in the same suite — `_common` changes are backward-compatible because the new fields default safely).

## 17. Routing-spec amendment (concurrent v2 → v3)

This commit also amends `docs/superpowers/specs/2026-04-30-llm-routing-anthropic-zero-spend.md` to drop payslip references — see the changelog block in that spec. Net effect on routing yaml: one fewer task entry (`payslip_extraction`) on the W3.4-when-it-ships list, since W3.4 is now ICICI Savings (deterministic parser, no LLM).

## 18. References

- `docs/superpowers/specs/2026-04-26-v1-roadmap-r2-reprioritization.md` — W3.4 entry updated (Payslip → ICICI Savings) in same commit.
- `docs/superpowers/specs/2026-04-30-llm-routing-anthropic-zero-spend.md` — payslip_extraction dropped (v3 amendment in same commit).
- `docs/superpowers/specs/2026-04-29-paytm-xlsx-ingestion-design.md` — flag-don't-drop pattern reused; ParseResult.insertable_rows() pattern reused.
- `docs/superpowers/specs/2026-04-26-week-2-ingestion-design.md` — ICICI CC parser reference (pikepdf decrypt + pdfplumber pattern).
- PRD §7 (`transactions` table); PRD §11 Week 3 (savings was implicitly covered under "remaining sources").
- CLAUDE.md invariants 2 (`adb()` + `.execute()`), 3 (`import_hash`), 4 (`__parser_version__`), 9 (credentials split), 10 (golden fixtures gitignored).
- `tasks/lessons.md` 2026-04-25 (Supavisor pooler convention — applies if/when readonly client touches savings table; not in this spec's scope).
- Brainstorm conversation 2026-04-30 — D1-D7 ratification + the dual-entry insight.
