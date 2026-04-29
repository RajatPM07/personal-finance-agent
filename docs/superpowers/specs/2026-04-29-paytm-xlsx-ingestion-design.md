# Paytm UPI XLSX Ingestion — Design Spec

**Status:** Locked v1
**Date:** 2026-04-29
**Roadmap reference:** `docs/superpowers/specs/2026-04-26-v1-roadmap-r2-reprioritization.md` §2 (W3.1 — first task of Week 3)
**PRD references:** §11 Week 3, §6 model routing, §7 schema, §19.5 `import_hash`
**Lessons referenced:** AMEX-XLSX parser pattern (Week 2), validator pure-fn convention, golden-file TDD
**Brainstorm artifact:** Conversation 2026-04-26 — file inspected at-rest, 5 design decisions D1–D5 ratified by user 2026-04-29.

## 1. Goal

Stand up an end-to-end ingestion path for Paytm UPI annual statements (XLSX). By the end of W3.1:
- A single year (Apr 2025 → Mar 2026) of Paytm UPI transactions populates `transactions` — **~711 rows** (713 in the Passbook minus 2 AMEX-routed rows skipped at insert; the 7 self-transfers ARE inserted, just excluded from validator's paid-sum).
- Folder watcher and Telegram document handler dispatch `paytm_upi_*.xlsx` files automatically, identical UX to ICICI / AMEX.
- A new `category_hint` column on `transactions` captures Paytm's pre-categorization (free training data for Week 5).
- Re-dropping the same file produces `skipped_duplicate` (idempotency, mirrors W2 invariant).
- Validator confirms extracted totals match Summary's declared paid + received within ±₹1 — both directions.

## 2. Scope

**In scope:**
- New deterministic parser `parsers/paytm_upi.py` using `pandas.read_excel`.
- Migration `005_category_hint.sql` adding one nullable column to `transactions`.
- Updates to `_common.detect_bank_from_filename` to recognize "paytm".
- Updates to `folder_watcher.EXPECTED_EXTENSION`, `ACCOUNT_IDS`, and `dispatch_to_parser` for `paytm_upi`.
- Validator called twice per file (paid + received separately).
- Golden-fixture TDD against `tests/golden_fixtures/paytm_upi_apr25_mar26.xlsx`.
- 1-year backfill of the existing fixture file (rename `~/finance-inbox/paytm_upi_apr25_mar26.xlsx.rejected` → `paytm_upi_apr25_mar26.xlsx` once the parser ships).

**Explicitly deferred:**
- Cross-source enrichment for AMEX-routed Paytm rows (D1 backlog item — post-V1).
- SMS-based reconciliation against Paytm txns (cut from V1 in roadmap r2).
- Multi-handle self-transfer detection (V1 uses the single known handle from `accounts`).
- Splitting Paytm transactions by underlying source-account into separate `transactions` rows per bank (V1: all Paytm rows owned by the Paytm UPI account; the source-bank info is preserved in `notes` field).

## 3. Architecture

```
~/finance-inbox/paytm_upi_apr25_mar26.xlsx
        │
        ▼
folder_watcher (watchdog)
        │  detect_bank → "paytm_upi"
        │  ext check → ".xlsx" ✓
        ▼
dispatch_to_parser(paytm_upi)
        │
        ▼
parsers/paytm_upi.parse(file_path)  ──── reads Summary sheet for totals
        │                                reads Passbook sheet for rows
        │                                returns ParseResult{rows, declared_totals}
        ▼
ingestion/pipeline.ingest()
        │  validate(paid_extracted, paid_declared, ±₹1)   ─── twice
        │  validate(recv_extracted, recv_declared, ±₹1)
        │  for each row: compute import_hash (Mode B, file_content_hash + ordinal)
        │  upsert; collect (inserted, skipped) counts
        │  log to ingestion_log
        ▼
Telegram summary message
```

The orchestration layer is unchanged from Week 2 — Paytm slots in as a third bank with the same contract as ICICI / AMEX.

## 4. File structure (new + modified)

```
skills/finance/ingestion/
├── parsers/
│   ├── icici_cc.py            (unchanged)
│   ├── amex_cc.py             (unchanged)
│   └── paytm_upi.py           ← NEW
├── _common.py                 ← MODIFIED (detect_bank patterns)
├── folder_watcher.py          ← MODIFIED (ACCOUNT_IDS, EXPECTED_EXTENSION, dispatch)
├── pipeline.py                (unchanged)
└── statement_validator.py     (unchanged)

migrations/
└── 005_category_hint.sql       ← NEW

tests/
├── test_paytm_upi_parser.py    ← NEW (golden-file)
├── test_paytm_self_transfer.py ← NEW (small unit, no fixture)
└── golden_fixtures/
    └── paytm_upi_apr25_mar26.xlsx  (already in place, gitignored)
```

## 5. Source layer

### 5.1 `_common.detect_bank_from_filename`

Add a token match for `paytm`. The regex must respect existing patterns and avoid false positives (e.g. matching the literal string "paytm" inside another bank's filename — exceedingly unlikely in practice).

```python
# pseudocode addition to existing detect_bank_from_filename
PAYTM_TOKENS = ("paytm",)
# unchanged: ICICI_TOKENS, AMEX_TOKENS

# detection rule: lowercase + token boundary check
if any(t in name_lc for t in PAYTM_TOKENS):
    return "paytm_upi"
```

### 5.2 `folder_watcher` updates

```python
ACCOUNT_IDS["paytm_upi"] = UUID("…")    # look up from migrations/003_seed.local.sql
                                          # (the row where institution = 'Paytm' and type = 'upi')
EXPECTED_EXTENSION["paytm_upi"] = ".xlsx"
```

`dispatch_to_parser` gets a third branch:

```python
elif bank == "paytm_upi":
    from skills.finance.ingestion.parsers.paytm_upi import parse as paytm_parse
    parse_result = await asyncio.to_thread(paytm_parse, file_path)
    source = SourceMeta(source="manual_xlsx", source_ref=file_path.name)
```

No password lookup needed — Paytm UPI exports are not encrypted.

### 5.3 Telegram document handler

No changes required. The existing handler uses `detect_bank_from_filename`; once that knows "paytm", filename-prefixed Paytm uploads via Telegram will route correctly. The unrelated `BUTTON_DATA_INVALID` bug in the disambiguation path remains a separate fix (tracked outside this spec).

## 6. Parse layer (`parsers/paytm_upi.py`)

### 6.1 Public surface

```python
__parser_version__ = "1.0"   # threaded into import_hash per CLAUDE.md invariant 4

def parse(file_path: Path) -> ParseResult:
    """Read Summary + Passbook sheets, return ParseResult.

    Skips rows where Your Account = "American Express Credit Card" (D1).
    Populates row.category_hint from the Tags column when present (D4).
    Validator-friendly declared_totals dict has paid + received keys (D3).
    """
```

### 6.2 Reading the file

Two sheets, both read with `pandas.read_excel`:

| Sheet | Purpose | Header position |
|---|---|---|
| `Summary` | declared totals, per-source breakdown | no fixed header — extract by row scan |
| `Passbook Payment History` | transaction rows | row 0 is header |

Summary parsing:

```python
sumdf = pd.read_excel(path, sheet_name="Summary", header=None)
# rows 9..12 hold the totals (verified from inspection 2026-04-26):
#   row 9:  "Money Paid (Amount in Rs.)"      → paid_declared
#   row 10: "Money Paid (No. of Payments)"    → paid_count_declared
#   row 11: "Money Received (Amount in Rs.)"  → recv_declared
#   row 12: "Money Received (No. of Payments)" → recv_count_declared
# Defensive: scan rows for label match instead of trusting fixed indices,
# in case Paytm shifts the layout in a later export format.
```

### 6.3 Direction inference

The `Transaction Details` column starts with one of three patterns:

| Prefix | Direction | Sign |
|---|---|---|
| `Paid to ` | outgoing (paid) | debit (positive amount, negative signed) |
| `Money sent to ` | outgoing (paid OR self-transfer) | debit |
| `Received from ` | incoming (received) | credit |

Direction is inferred from the prefix. Self-transfer detection (D2):

```python
# A "Money sent to ..." row is treated as self-transfer iff
# Other Transaction Details column contains a known own-handle.
# V1: own-handles = [{accounts.account_number for paytm UPI accounts}]
# (currently a single handle per project memory).
SELF_HANDLES = load_self_handles_from_accounts_table()
def is_self_transfer(row) -> bool:
    if not row["Transaction Details"].startswith("Money sent to "):
        return False
    other = str(row.get("Other Transaction Details") or "")
    return any(h in other for h in SELF_HANDLES)
```

Self-transfers are ingested (we want the audit trail) but excluded from the validator's paid-sum.

### 6.4 Row construction

For each non-skipped, non-AMEX-routed row, build a `ParseRow`:

| Field | Source |
|---|---|
| `account_id` | `ACCOUNT_IDS["paytm_upi"]` (constant) |
| `txn_date` | parse Date column → `date` |
| `txn_time` | parse Time column → `time` (kept in `notes` for now; schema doesn't have a separate time column) |
| `amount` | parse Amount column; sign by direction (paid = negative, received = positive — match existing convention) |
| `description` | `Transaction Details` (e.g. "Paid to Merchant A") |
| `merchant_raw` | extract everything after the prefix ("Merchant A") |
| `notes` | concat `Time`, `Your Account`, `UPI Ref No.` fields for downstream visibility |
| `category_hint` | `Tags` column with leading emoji + `#` stripped (e.g. `#🥘 Food` → `"Food"`) |
| `source_row_ordinal` | 1..N contiguous over the kept rows (per CLAUDE.md test invariant) |

### 6.5 Flagging (NOT skipping at parse layer)

The 2 AMEX-routed rows are part of Paytm's declared paid count of 698 — Summary includes them. If the parser drops them, the validator's extracted_paid will be short by exactly those 2 rows' amounts and the strict ±₹1 check will fail.

**Correct contract: keep all 713 rows in the parser output, flagged. The insert layer applies the skip.**

```python
@dataclass
class PaytmRow:
    # ...standard fields...
    direction: Literal["paid", "received"]
    is_self_transfer: bool
    is_amex_routed: bool        # D1: dropped at insert, kept in validator
    category_hint: str | None    # D4: Paytm tag, emoji-stripped

# ParseResult exposes:
class ParseResult:
    rows: list[PaytmRow]                  # all 713
    declared_totals: dict                  # from Summary (includes AMEX-routed in paid)
    metadata: dict                         # {"skipped_amex_routed": 2}

    @property
    def insertable_rows(self) -> list[PaytmRow]:
        return [r for r in self.rows if not r.is_amex_routed]
```

The skipped-count is computed once (on access of `metadata["skipped_amex_routed"]`) and threaded into the Telegram summary.

## 7. Validate layer

### 7.1 Two-direction validation

The existing `statement_validator.validate(extracted, declared, tolerance)` is parser-agnostic and called twice. Both sums run over **all 713 parsed rows** (including AMEX-routed) because Paytm's Summary counts them in declared paid:

```python
# Note: AMEX-routed rows ARE in this sum (matches Summary semantic).
# Note: self-transfers are NOT in paid sum (matches Summary footnote
#       "self transfer payments are not included").
paid_extracted = sum(abs(r.amount) for r in result.rows
                     if r.direction == "paid" and not r.is_self_transfer)
recv_extracted = sum(abs(r.amount) for r in result.rows
                     if r.direction == "received")

paid_ok = validate(paid_extracted, declared_paid, tolerance=Decimal("1"))
recv_ok = validate(recv_extracted, declared_recv, tolerance=Decimal("1"))
overall = paid_ok and recv_ok
```

The AMEX-routed skip only kicks in at the insert step (`pipeline.upsert(result.insertable_rows)`), keeping the validator semantically aligned with Paytm's published numbers.

If either direction fails, `pipeline` logs `status='total_check_failed'` with both deltas in `error_msg` so debugging doesn't require re-running.

### 7.2 Self-transfer tolerance fallback

If V1 self-handles list is incomplete (we miss some self-transfers), the paid-sum will overshoot the declared. Mitigation:
- First pass: ±₹1 strict tolerance.
- If it fails AND the delta is positive (extracted > declared): re-run with ±₹100 tolerance and log `status='total_check_failed'` with `error_msg` flagging "likely self-transfer mismatch — review own-handles list".
- This is a soft-failure path, not a permission to silently degrade.

## 8. Persist layer

### 8.1 Schema migration `005_category_hint.sql`

```sql
-- 005_category_hint.sql
-- W3.1 Paytm parser populates this from the Tags column. NULL for ICICI/AMEX rows.
-- W5 normalization layer treats this as a strong prior, NOT the final category.

ALTER TABLE transactions
  ADD COLUMN category_hint TEXT;

COMMENT ON COLUMN transactions.category_hint IS
  'External pre-categorization (e.g. Paytm Tags). W5 normalization treats as strong prior, not final answer.';
```

No backfill: existing 509 rows stay NULL.

### 8.2 Hash mode (D5)

Mode B per CLAUDE.md invariant 3:

```
import_hash = sha256(
    account_id ||
    txn_date ||
    amount ||
    normalized_desc ||
    file_content_hash ||      # sha256 of the raw XLSX bytes
    source_row_ordinal ||
    parser_version
)
```

`file_content_hash` is the existing `pdf_content_hash` field reused for any binary file — invariant unchanged. Re-dropping the same XLSX produces identical hashes → `skipped_duplicate`. A re-export of the same period from Paytm with byte drift would produce a new `file_content_hash`; in V1 this would re-ingest the rows with new ordinals → false dedup miss. Mitigation deferred to W4 `/dedup` (manual reconciliation).

### 8.3 Pipeline behavior

`pipeline.ingest()` is mostly unchanged. Two small changes:

1. Validator is called twice (paid + received) — see §7.1.
2. The insert step uses `result.insertable_rows` instead of `result.rows`, so AMEX-routed rows (which counted in validation) are dropped before hash computation and persistence.

Hash computation, `upsert(on_conflict='import_hash')`, and `ingestion_log` writes are unchanged.

## 9. Telegram review flow

After ingestion, the existing `_send_summary` in `folder_watcher.py` posts:

```
📥 PAYTM UPI paytm_upi_apr25_mar26.xlsx ingested
711 rows, ₹<paid_total> paid (declared ₹<paid_declared>) — totals match ✓
₹<recv_total> received (declared ₹<recv_declared>) — totals match ✓
2 AMEX-routed rows skipped at insert (dedup with AMEX statement).
```

Plain text, no `parse_mode` (consistent with the bot-layer fix in commit `464f8cf`).

## 10. Backfill mechanics

Single file, single year:

1. After `parsers/paytm_upi.py` is merged + tests pass + app restarted via `launchctl kickstart -k`, rename:
   ```
   mv ~/finance-inbox/paytm_upi_apr25_mar26.xlsx.rejected \
      ~/finance-inbox/paytm_upi_apr25_mar26.xlsx
   ```
2. The folder watcher's `on_moved` handler picks up the rename → dispatches → ~711 rows land in `transactions` → Telegram summary confirms.
3. Verify in Supabase: `SELECT count(*) FROM transactions WHERE account_id = '<paytm_uuid>';` → expect ~711.

If totals fail validation, the file is logged as `total_check_failed` and rows are NOT inserted (consistent with W2 pipeline). Re-drop after fix.

## 11. Error handling matrix

| Condition | Behavior | `ingestion_log.status` |
|---|---|---|
| File parses cleanly, both totals match | rows inserted, summary sent | `success` |
| File re-dropped (same content) | no rows inserted | `skipped_duplicate` |
| Paid total mismatch (>₹1 strict, >₹100 fallback) | no rows inserted, alert sent | `total_check_failed` |
| Received total mismatch | no rows inserted, alert sent | `total_check_failed` |
| Sheet structure unexpected (Summary missing rows 9-12 labels) | no rows inserted, alert sent | `failed` |
| AMEX-routed row encountered | row skipped silently, counted in metadata | (not its own row in log; reported in success row) |
| Tags column blank | `category_hint = NULL` for that row | (no log impact) |
| Unknown row prefix (not "Paid to" / "Money sent to" / "Received from") | row dropped, counted in metadata, alert sent | `needs_review` if any unknown rows present |

## 12. Testing strategy

Golden-file TDD per `tests/test_amex_cc_parser.py` pattern.

`tests/test_paytm_upi_parser.py` — fixture-dependent, skips when `tests/golden_fixtures/paytm_upi_apr25_mar26.xlsx` missing:

- `test_parser_version_string` — `__parser_version__ == "1.0"`.
- `test_parse_returns_nonempty_parseresult` — rows count > 0, both declared totals populated.
- `test_paytm_amex_routed_rows_flagged_not_dropped` — `result.rows` has all 713; `result.insertable_rows` has 711; `metadata["skipped_amex_routed"] == 2`.
- `test_paytm_amex_routed_rows_in_validator_sum` — paid_extracted (computed from `result.rows`) includes the 2 AMEX-routed amounts; matches declared 698 paid count semantic.
- `test_paytm_ordinals_contiguous_1_to_n` — ordinal field 1..N with no gaps (CLAUDE.md test invariant).
- `test_paytm_extracted_paid_matches_declared` — abs(extracted − declared) ≤ ₹1.
- `test_paytm_extracted_received_matches_declared` — abs(extracted − declared) ≤ ₹1.
- `test_paytm_category_hint_populated` — for any row whose Tags column was non-empty, `category_hint` is non-NULL and emoji-stripped.
- `test_paytm_self_transfer_excluded_from_paid_sum` — sum of paid rows minus self-transfers equals validator's input.

`tests/test_paytm_self_transfer.py` — pure-fn unit, no fixture:
- known own-handle in `Other Transaction Details` + "Money sent to" prefix → flagged.
- non-own-handle → not flagged.
- "Paid to" prefix → not flagged regardless of handle.

## 13. New dependencies

None. `pandas` and `openpyxl` are already in `pyproject.toml` from Week 2.

## 14. Code changes outside `skills/finance/ingestion/`

- `migrations/005_category_hint.sql` — new file.
- `migrations/003_seed.local.sql` — no change (Paytm UPI account already seeded per project memory).
- `pyproject.toml` — no change.
- `CLAUDE.md` — no change (Paytm doesn't introduce new invariants).

## 15. Open implementation risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Self-handle list incomplete → paid-sum overshoots declared | Medium (only 1 known handle today) | Soft fallback to ±₹100 tolerance with explicit log entry |
| Paytm changes Summary sheet layout in a future export | Low | Label-scan instead of fixed-row indexing in §6.2 |
| Annual re-export drifts file bytes → re-ingestion of same period | Medium | Documented; deferred to `/dedup` in W4 |
| `Tags` column emoji handling on Postgres | Low | Postgres `text` is UTF-8 by default; strip emoji before insert as belt-and-braces |
| `Order ID`, `Remarks`, `Comment` columns sparse — most rows have NULL | Already observed | Concatenate non-NULL values into `notes`; tolerate NULL |

## 16. Acceptance criteria

By close of W3.1:

- [ ] `parsers/paytm_upi.py` lands; all 8 fixture-dependent tests + 3 self-transfer unit tests pass.
- [ ] `migrations/005_category_hint.sql` applied to Supabase; `transactions` table has the column.
- [ ] `_common.detect_bank_from_filename` recognizes "paytm" → "paytm_upi".
- [ ] Folder watcher dispatch tested with the renamed file → ~711 rows land in `transactions` (Paytm UPI account); 2 AMEX-routed rows reported as skipped in metadata.
- [ ] `ingestion_log` shows `status='success'` for the Paytm file with both paid and received totals matching.
- [ ] Re-dropping the same file produces `skipped_duplicate` (idempotency).
- [ ] Telegram summary shows correct row count + skipped-AMEX count + both totals.
- [ ] `make lint && make typecheck && make test` all green.
- [ ] No regression in existing AMEX/ICICI parsers (run their tests in the same suite).

## 17. References

- `docs/superpowers/specs/2026-04-26-week-2-ingestion-design.md` (Week 2 — pattern reused for parser shape, validator contract, Telegram summary)
- `docs/superpowers/specs/2026-04-26-v1-roadmap-r2-reprioritization.md` (W3.1 placement)
- PRD §7.420 (rolling-statement overlap; deferred to `/dedup`)
- PRD §19.5 (`import_hash` Mode B — Option Y)
- CLAUDE.md invariants 2 (`adb()` + `.execute()`), 3 (`import_hash`), 4 (`__parser_version__`), 10 (golden fixtures gitignored)
- `tasks/lessons.md` 2026-04-21 (Supabase insert dict typing — `dict[str, Any]` annotation needed for `category_hint` literal builds)
- Brainstorm conversation 2026-04-26 → 2026-04-29 (file inspection + D1–D5 ratification)
