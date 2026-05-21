# W5.1 — Refund + Self-Transfer Detection Design

**Date:** 2026-05-21
**Author:** Rajat (brainstormed with Claude)
**Status:** Locked v1
**Predecessors:** PRD §F8, PRD §429-432, `docs/superpowers/specs/2026-04-26-v1-roadmap-r2-reprioritization.md` (W5.1)
**Implementation plan:** to be written via `superpowers:writing-plans` after this spec is approved

## 1. Constraint

W4.2 (morning brief + weekly review) and the eventual `/afford` engine both read `SUM(direction='in')` and `SUM(direction='out')` as the basis for their numbers. Today, the `transactions` table contains three classes of `direction='in'` row that those queries would conflate:

1. **Real merchant refunds** — e.g. AMEX showing `"paytm*JUBILANTFOODWORKS Noida ₹673"` undoing a recent debit on the same card. Should net against the original spend.
2. **CC bill payments from bank** — e.g. AMEX `"PAYMENT RECEIVED. THANK YOU ₹60,457"`, ICICI CC `"BBPS Payment received ₹59,327"`. These are self-transfers between the user's own accounts — NOT income, NOT a refund. A naive brief would count them as income and double-count the original spend.
3. **Fee/surcharge reversals** — small bank-side adjustments like `"Reversal of Fuel Surcharge ₹30"`, `"SGST-Rev-CI@9% ₹2.73"`. Real but tiny; don't materially distort briefs.

Schema is ready: `is_refund boolean`, `linked_txn_id uuid REFERENCES transactions`, and (post-migration 008) `is_self_transfer boolean` all live on `transactions`. They are populated by no code path today.

This spec defines a deterministic, LLM-free detection pass to populate these flags after ingestion. Out of V1 scope: surcharge reversal classification, automatic merchant categorization, refund-induced category propagation.

## 2. Decisions

| # | Decision |
|---|---|
| D1 | Detect **(a) merchant refunds** and **(b) CC-bill-payment self-transfers**. Surcharge/fee reversals are NOT detected in V1 — too small to distort briefs, scope creep otherwise. |
| D2 | **Refund matcher:** same account, exact amount, `rapidfuzz.fuzz.token_set_ratio(raw_merchant_a, raw_merchant_b) >= 80`, date window `[credit.date - 30 days, credit.date - 1 day]`. |
| D3 | **Self-transfer matcher:** per-source text pattern (config-driven) PLUS cross-account exact-amount debit within ±2 days. Pattern-without-corroboration leaves row pending (not auto-marked). |
| D4 | **Both sides of a self-transfer get `is_self_transfer=true`.** `linked_txn_id` set on the CC-side credit only, pointing at the savings-side debit. |
| D5 | **Trigger model:** inline at `pipeline.py:ingest()` end (Path 1), wrapped in try/except so detection failures don't roll back ingestion. One-time `scripts/backfill_refund_detection.py` for existing 1,227 rows. No periodic scheduler involvement. |
| D6 | **Ambiguity resolution:** when multiple candidate originals match, pick chronologically closest (smallest date delta). Tie-break for self-transfer multi-match: amount delta. |
| D7 | **Idempotency contract (A.i):** `is_refund` and `is_self_transfer` are tri-state nullable booleans. `NULL` = unprocessed; `true`/`false` = processed (leave alone on re-run). User override via future `/categorize` (W4.3) sets explicit `true`/`false` and is preserved. |
| D8 | **No LLM, no Anthropic spend.** Pure heuristic detection. Matches the V1 zero-Anthropic-spend constraint from `2026-04-30-llm-routing-anthropic-zero-spend.md`. |
| D9 | **All SELECT queries via `readonly_client()` (W3.5 psycopg3).** Avoids the supabase-py 1000-row read cap (see `feedback_supabase_pagination_cap.md`). UPDATEs through `service_client()`. |

## 3. File structure

**New artifacts:**

```
skills/finance/categorization/                NEW package
├── __init__.py
└── refund_detector.py                        PRD line 631 — single public entry detect_for_account()

config/self_transfer_patterns.yaml            NEW
                                              keyed by account_id; list of marker substrings per source

migrations/008_refund_detection.sql           NEW
                                              ADD COLUMN is_self_transfer; tri-state nullable refactor on
                                              is_refund (drop default; nullify existing checked-but-unlinked
                                              rows); two performance indexes

scripts/backfill_refund_detection.py          NEW
                                              one-time pass across all accounts; --apply / --dry-run-default

tests/test_self_transfer_patterns.py          NEW   ~6 tests (config loader + util)
tests/test_refund_detector.py                 NEW   ~17 tests (12 matcher unit + 5 integration)
tests/test_pipeline_refund_integration.py     NEW   2 tests (success path + crash-isolation guard)
```

**Modified:**

```
skills/finance/ingestion/pipeline.py          add _run_refund_detection_safe + invoke at end of ingest()
```

**Boundary decisions:**

- **New package `categorization/`** (not `agents/`, not `ingestion/`) — refund detection is a post-ingestion derived/audit pass. PRD line 631 already named this directory. Future siblings: `merchant_normalizer.py` (W5+), `category_assigner.py` (W5+).
- **One module, two matcher functions.** `refund_detector.py` exports `detect_for_account()` which internally calls `_find_refund_match()` and `_find_self_transfer_match()`. They share DB-query helpers and `linked_txn_id` write semantics. Split into separate files only if either grows beyond ~150 lines.
- **Patterns in YAML, keyed by account_id.** Same shape as `config/model_routing.yaml`. Adding a new bank source is a config-only change.

## 4. Schema (migration 008)

```sql
-- migrations/008_refund_detection.sql

-- 1. New flag mirroring is_refund's shape (nullable, no default).
ALTER TABLE transactions
  ADD COLUMN is_self_transfer boolean;   -- NULL = unchecked; false = checked/not; true = confirmed

-- 2. Shift is_refund from default-false to tri-state nullable. Currently every
--    row has is_refund=false (column default), which doesn't distinguish
--    "checked and not a refund" from "never processed." Drop the default so
--    new ingestions get NULL; nullify existing rows so the backfill processes them.
ALTER TABLE transactions ALTER COLUMN is_refund DROP DEFAULT;
UPDATE transactions
  SET is_refund = NULL
  WHERE is_refund = false AND linked_txn_id IS NULL;
-- (Rows where linked_txn_id is already set: leave is_refund alone. Zero such
--  rows today; future-proofing clause for re-runnable migrations.)

-- 3. Performance indexes for matcher queries. Trivial cost at 1,227 rows;
--    avoids seq-scans as row count grows to ~50K+.
CREATE INDEX IF NOT EXISTS idx_transactions_account_date
  ON transactions (account_id, date);   -- refund matcher: candidates in this account, date range
CREATE INDEX IF NOT EXISTS idx_transactions_amount_date
  ON transactions (amount, date);       -- self-transfer matcher: matching-amount debits across all accounts
```

**Tri-state nullable semantics:**

| `is_refund` value | Meaning | Detector behavior on re-run |
|---|---|---|
| `NULL` | Never processed | Process this row |
| `true` | Confirmed refund (with `linked_txn_id`) | Skip — leave alone |
| `false` | Confirmed not-a-refund | Skip — leave alone |

Identical semantics for `is_self_transfer`. Detector's re-run guard: `WHERE is_refund IS NULL` (or `WHERE is_self_transfer IS NULL`).

**User override path (forward-looking for W4.3 `/categorize`):**

- User overrides wrongly-linked refund → `/categorize` sets `is_refund = false`, `linked_txn_id = NULL`. Detector won't re-touch.
- User manually marks a row as refund → `/categorize` sets `is_refund = true`, `linked_txn_id = <chosen>`. Detector won't re-touch.
- User forces re-detection → set `is_refund = NULL`. Detector picks it up on next run.

**No pipeline.py insert dict change.** `_build_insert_row()` already omits `is_refund`; new rows get `NULL` automatically once the default is dropped.

## 5. Matcher algorithms

### 5.1 Public entry point

```python
# skills/finance/categorization/refund_detector.py

@dataclass(frozen=True)
class DetectionResult:
    refunds_linked: int        # count of rows newly marked is_refund=true
    self_transfers_linked: int # count of CC↔savings pairs newly linked
    rows_processed: int        # total rows whose flags transitioned NULL → not-NULL
    rows_pending: int          # text-pattern hits left at NULL awaiting cross-account match

def detect_for_account(account_id: UUID, since: date | None = None) -> DetectionResult:
    """Run refund + self-transfer detection scoped to this account's rows on or after `since`.

    Two phases:
      A. Process new direction='in' rows on `account_id` (the just-ingested account).
      B. The new direction='out' rows on `account_id` may unblock pattern-matched
         CC credits ingested earlier (the "savings caught up" case).
    """
```

### 5.2 Phase A — process new credits on `account_id`

For each `direction='in'` row where `(is_refund IS NULL OR is_self_transfer IS NULL) AND (since IS NULL OR date >= since)`:

1. **Self-transfer check first** (cheaper + more specific than fuzzy merchant):
   - Does `raw_merchant` contain any text pattern from `config/self_transfer_patterns.yaml[account_id]`?
     - **No pattern match** → skip to step 2 (refund check).
     - **Pattern matches** → look across accounts **other than `account_id`** (cross-account match is required — same-account debits don't count as self-transfers) for a `direction='out'` row with:
       - exact same `amount`
       - `date` within ±2 days
       - `is_self_transfer IS NULL` (don't relink already-processed rows)
       - `account_id != credit.account_id` (the cross-account constraint, restated as a hard filter)
       - same `user_id` (V1 no-op; forward-looking for multi-tenant)
       - **Exactly one match** → set both rows' `is_self_transfer = true`; set the CC row's `linked_txn_id = <debit.id>`. Done.
       - **Multiple matches** → pick smallest date delta; tie-break smallest amount delta (amount is exact, so this rarely fires).
       - **No match yet** → leave the CC row's `is_self_transfer = NULL` (don't mark as processed; wait for the savings statement to land). Increment `rows_pending`. **Continue to step 2** — the pattern-hit doesn't preempt the refund check. Patterns like "PAYMENT RECEIVED" don't fuzzy-match real merchant names, so this is safe.

2. **Refund check** (only reached if step 1 didn't auto-link as self-transfer):
   - Find `direction='out'` rows on the SAME `account_id` with:
     - exact same `amount`
     - `date` in the CLOSED interval `[credit.date - 30 days, credit.date - 1 day]` (refund must follow original; `-1 day` excludes same-day pairs which are typically adjustment artifacts, not refunds)
     - `rapidfuzz.fuzz.token_set_ratio(credit.raw_merchant, candidate.raw_merchant) >= 80`
     - candidate's `is_refund IS NOT true` AND `is_self_transfer IS NOT true` (cycle and cross-linking prevention; §7 below)
   - **One or more matches** → pick most recent (smallest `credit.date - candidate.date`). Set `is_refund = true`, `linked_txn_id = <candidate.id>`. Done.
   - **No matches** → set `is_refund = false` (processed, not a refund).

3. **Mark processed.** Any row that didn't enter the `rows_pending` bucket gets its non-set flag updated to `false`:
   - Rows that reached step 2 with no refund match: `is_refund = false`.
   - Rows whose step 1 had no text-pattern match: `is_self_transfer = false`. (Per 3.i locked in brainstorm — single processed-marker; pattern additions to YAML are rare enough to require explicit re-NULLing to re-evaluate.)

### 5.3 Phase B — unblock pending pattern-credits elsewhere

For each `direction='out'` row newly visible on `account_id` (since `since`):

- Look across other accounts for `is_self_transfer IS NULL` credits that:
  - have a text-pattern match per `config/self_transfer_patterns.yaml`
  - have exact same `amount` and `date` within ±2 days
- If exactly one such pending credit → link both sides (same write semantics as Phase A step 1).

Phase B is what makes the "ingest AMEX statement first, then savings" workflow correct. Without it, AMEX's `"PAYMENT RECEIVED"` credits stay `is_self_transfer=NULL` forever once savings is ingested (because the savings-side debits are `direction='out'`, never picked up by Phase A on subsequent runs).

### 5.4 Order matters

Self-transfer first, refund second. A `"PAYMENT RECEIVED. THANK YOU"` credit could in theory fuzzy-match an unrelated debit if amounts collide — `token_set_ratio` between that string and a real merchant is near zero, but defensive ordering eliminates the risk entirely.

### 5.5 DB access pattern

SELECTs through `readonly_client()` (W3.5 psycopg3) — bypasses the supabase-py 1000-row read cap. UPDATEs through `service_client()` per-row `.update().eq("id", ...).execute()` — bulk reads don't go through that client.

## 6. Trigger integration

### 6.1 Pipeline.py wire-up

```python
# skills/finance/ingestion/pipeline.py — appended to ingest()

async def ingest(parse_result, account_id, source_meta) -> dict:
    val = validate(parse_result)
    if not val.ok:
        return await _log_validation_failure(parse_result, source_meta, val)

    rows = [_build_insert_row(r, account_id, parse_result, source_meta)
            for r in parse_result.insertable_rows()]
    response = await adb(
        lambda: service_client().table("transactions")
            .upsert(rows, on_conflict="import_hash", ignore_duplicates=True)
            .execute()
    )
    rows_added = len(response.data) if response.data else 0
    log_entry = await _log_success(parse_result, source_meta, val, rows_added)

    if rows_added > 0:
        await _run_refund_detection_safe(account_id, parse_result)

    return log_entry


async def _run_refund_detection_safe(account_id: UUID, parse_result: ParseResult) -> None:
    """Inline detection wrapped to never roll back ingestion on failure.

    Detection writes AUDIT fields (is_refund, is_self_transfer, linked_txn_id),
    not primary facts. Failures here MUST NOT undo the ingestion already
    committed. Next detection run picks up unprocessed rows via the IS NULL guard.
    """
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

**Three guarantees:**

1. **Ingestion is the primary fact.** `_log_success()` writes the `ingestion_log` row BEFORE detection runs. Detection crash → statement is still ingested, Telegram summary still fires, `ingestion_log.status='success'`.
2. **`since = earliest_new_row.date - 30 days`** covers the refund matcher's window AND lets Phase B unblock pending pattern-credits whose pending state ended just outside the new batch.
3. **`rows_added > 0` short-circuit** avoids burning DB queries on duplicate re-ingestions.

### 6.2 Backfill script

```python
# scripts/backfill_refund_detection.py
"""One-time pass to populate is_refund / is_self_transfer / linked_txn_id on
existing rows. Idempotent — re-runnable; the detector's IS NULL guards mean
already-processed rows are skipped.

Usage:
    .venv/bin/python -m scripts.backfill_refund_detection           # dry-run by default
    .venv/bin/python -m scripts.backfill_refund_detection --apply   # actually writes
    .venv/bin/python -m scripts.backfill_refund_detection --apply --strict
                                                                    # raise on first error
"""

def main():
    args = parse_args()
    sb = service_client()
    accounts = sb.table("accounts").select("id,nickname").execute().data or []

    totals = {"refunds": 0, "self_transfers": 0, "processed": 0, "pending": 0}
    failures: list[tuple[UUID, str]] = []
    for acct in accounts:
        acct_id = UUID(acct["id"])
        print(f"\n=== {acct['nickname']} ({acct_id}) ===")
        try:
            if args.apply:
                r = detect_for_account(acct_id, since=None)
            else:
                r = _dry_run_count(acct_id)
        except Exception as e:  # noqa: BLE001
            failures.append((acct_id, f"{type(e).__name__}: {e}"))
            if args.strict:
                raise
            print(f"  FAILED: {type(e).__name__}: {e} (continuing — pass --strict to abort)")
            continue
        print(f"  refunds_linked={r.refunds_linked}  self_transfers_linked={r.self_transfers_linked}")
        print(f"  rows_processed={r.rows_processed}    rows_pending={r.rows_pending}")
        totals = _accumulate(totals, r)

    print(f"\nTotals: {totals}")
    if failures:
        print(f"\nFailed accounts ({len(failures)}):")
        for acct, msg in failures:
            print(f"  {acct}: {msg}")
    if not args.apply:
        print("(dry run — pass --apply to commit changes)")
```

**Three guarantees:**

1. **Default dry-run.** First-invocation safety; explicit `--apply` required to write. Mirrors `scripts/restore_drill.py` and `scripts/backup_supabase.py` patterns.
2. **Order-independent.** Per Phase A's cross-account match + Phase B's catch-up duality, correctness doesn't depend on which account is processed first.
3. **Re-runnable.** Second invocation is a no-op on already-processed rows.

**Lenient default with `--strict` opt-in.** Per-account errors don't abort the batch by default. `--strict` is for CI/dev where a clean failure signal matters.

### 6.3 Explicitly NOT building

- No APScheduler job for periodic detection (Path 1 chose ingestion-trigger only).
- No Telegram alert on detection result (would be noisy; `logger.info` is sufficient).
- No new `detection_log` table (`linked_txn_id` IS the audit trail; `/ask` can query it).

## 7. Error handling

**Outer guard:** `_run_refund_detection_safe` (§6.1) catches anything bubbling out of `detect_for_account`. Ingestion already committed; failure is logged via `logger.exception` and the next detection run re-processes via the `IS NULL` guard.

**Inner error categories:**

| Category | Example | Handling |
|---|---|---|
| Per-row data quirks | `raw_merchant IS NULL`, empty string | Skip row, log at WARNING. Don't update flags (stays NULL → retried next run). |
| Multiple-match anomaly | Two candidates tie on date AND amount | Pick lexicographically smallest UUID, log at INFO. Deterministic so re-runs converge. |
| Linked-txn cycle | Candidate has `is_refund=true` already (refund-to-refund link attempt) | Skip the link, log at WARNING. Mark candidate's `is_refund=false` so we don't retry. |
| Cross-linking attempt | Candidate has `is_self_transfer=true` (refund linking into a self-transfer pair) | Same as cycle case — skip, log, mark processed. |
| Config errors | `self_transfer_patterns.yaml` missing/malformed; empty pattern; unknown account_id | Fail loud at module import time. Better to crash detection than process every row as not-a-self-transfer. |
| DB errors during write | Service client UPDATE conflict, connection drop mid-batch | Per-row try/except around the UPDATE. Log + skip — row stays NULL, next run retries. Other rows continue. |

**Critical: Supabase 1000-row read cap (`feedback_supabase_pagination_cap.md`).** All candidate-lookup SELECTs go through `readonly_client()` (psycopg3, no cap). The supabase-py client is used ONLY for per-row UPDATEs.

**Logger contract — one log line per per-row decision:**

- `INFO` on successful link: `"refund_detector: linked credit=<id> to original=<id> (date_delta=Nd, amount=₹X)"` or `"refund_detector: linked self_transfer cc=<id> savings=<id> (date_delta=Nd, amount=₹X)"`
- `INFO` on no-match: `"refund_detector: no match for credit=<id> account=<acct>"`
- `WARNING` on skipped rows: `"refund_detector: skipped credit=<id>: <reason>"`
- `DEBUG` on candidate-search activity (grep-able when triaging)

Per-batch summary is the single `logger.info` line from §6.1. No `print()` calls inside `refund_detector.py` (CLAUDE.md: "No `print` in long-running paths").

## 8. Testing

**Three test files:**

```
tests/test_self_transfer_patterns.py      ~6 tests — config loader + util (unit-only)
tests/test_refund_detector.py              ~17 tests (12 matcher unit + 5 integration)
tests/test_pipeline_refund_integration.py  2 tests — wrapper success path + crash-isolation guard
```

### 8.1 `tests/test_self_transfer_patterns.py`

Templates from `tests/test_review_config.py`. Coverage:

- Valid YAML loads, returns `dict[UUID, list[str]]`
- Missing file raises at import (fail-loud per §7)
- Malformed YAML raises
- Empty patterns list for an account raises
- Pattern with empty string raises (would match every row)
- Account ID in YAML not in `accounts` table raises (verified once at load)
- `matches_self_transfer(raw_merchant, patterns)` case-insensitive substring; multi-pattern OR

### 8.2 `tests/test_refund_detector.py`

**Unit half — 12 tests, pure functions, no DB:**

For `_find_refund_match(credit, candidates)`:
- Single exact candidate → returns it
- Multiple candidates → smallest date delta (D6)
- Rapidfuzz score = 79 → rejected
- Rapidfuzz score = 80 → accepted
- Same-day candidate excluded
- 31-day-old candidate excluded
- `raw_merchant IS NULL` on either side → returns None
- Candidate with `is_refund=true` → excluded (cycle prevention)
- Candidate with `is_self_transfer=true` → excluded (cross-linking prevention)

For `_find_self_transfer_match(credit, recent_debits, patterns)`:
- Pattern hit + cross-account match → returns debit row
- Pattern hit + no cross-account match → returns `PENDING` sentinel
- No pattern hit → returns None
- Pattern hit, only same-account debit found → returns None (cross-account required)
- Multiple cross-account matches → smallest date delta
- Cross-account candidate with `is_self_transfer=true` → excluded

**Integration half — 5 tests, live DB, gated like `tests/test_readonly_client.py`:**

Use `_LIVE = pytest.mark.skipif(not _readonly_password_available(), ...)` and a `seeded_fresh_scenario` fixture that opens a psycopg transaction, INSERTs canned rows, runs the test, ROLLBACKs at teardown. Seeded rows never persist (decision 6.i from brainstorm).

1. Refund happy path — seed: 1 debit + 1 matching credit; assert flags + linked_txn_id.
2. Self-transfer happy path — seed: AMEX credit with "PAYMENT RECEIVED" + savings debit; assert both flagged, linked_txn_id only on CC side.
3. Phase B catch-up — seed AMEX credit (pattern, no savings); run AMEX detect; assert pending. Seed matching savings debit; run savings detect; assert both linked.
4. Idempotent re-run — run twice; second run `rows_processed=0`.
5. User override preserved — manually `is_refund=true`; run; assert untouched. Null it; re-run; assert detector now processes.

### 8.3 `tests/test_pipeline_refund_integration.py`

Two tests:

1. **Happy path:** mock `detect_for_account` returning a `DetectionResult`. Run `ingest(...)`. Assert (a) `_log_success` called BEFORE detection, (b) `logger.info` summary line emitted, (c) `ingestion_log` row has `status='success'`.
2. **Detection-crash isolation:** mock `detect_for_account` raising `RuntimeError("synthetic")`. Run `ingest(...)`. Assert (a) `_log_success` STILL called (ingestion committed), (b) `logger.exception` fires, (c) `ingest` returns success log_entry, NOT a failure entry. **Load-bearing safety contract from §6.1.**

### 8.4 Out of scope for V1 tests

- No golden-PDF fixtures (refund detection runs against `transactions`, not parsers).
- No backfill-script test beyond a smoke (mock detect_for_account; verify dry-run-default + `--apply` + `--strict` + final summary). Backfill is operational.
- No performance/load tests at 1,227 rows. Revisit at ~50K+.

**Total new tests:** ~27 (6 + 17 + 2 + 2 backfill smoke). In line with W3.5 (13) and W4.1 (80+).

**Gating strategy mirrors existing patterns:**
- Unit tests: deterministic, no env dependency, run on every CI
- Integration tests: gated on `SUPABASE_READONLY_PASSWORD`; auto-run when `.env` has it, auto-skip in CI without it
- Wrapper tests: mock-based; verify failure-isolation invariant explicitly

## 9. Out of V1 scope

- **Surcharge / fee reversal classification.** Tiny amounts (₹2-30); don't materially distort briefs. Revisit if observed to mislead `/ask` or briefs in practice.
- **Refund category propagation.** A refund row should arguably inherit the category of its original spend. Belongs to W5+ merchant normalization / categorization scope.
- **Bulk override commands.** `/categorize` (W4.3) handles per-row corrections. Bulk overrides not specified for V1.
- **Cross-currency refunds.** All current data is INR. Schema has `currency text`; matcher currently requires exact amount match — implicitly assumes same currency. Future multi-currency requires currency-aware logic.
- **Multi-tenant.** V1 is single-user. The `user_id` filter in matchers is a forward-looking no-op.

## 10. References

- PRD §F8 (income classification → variable/bonus/refund disambiguation)
- PRD lines 383-384 (schema: `is_refund`, `linked_txn_id`)
- PRD lines 429-432 (refund handling: 30-day window + fuzzy merchant)
- PRD line 631 (planned `skills/finance/categorization/refund_detector.py`)
- `docs/superpowers/specs/2026-04-26-v1-roadmap-r2-reprioritization.md` (W5.1)
- `docs/superpowers/specs/2026-04-30-llm-routing-anthropic-zero-spend.md` (zero-Anthropic-spend constraint that justifies D8)
- W3.5 `readonly_client()` (`skills/finance/lib/db.py`) — psycopg3 connection used for matcher SELECTs
- `feedback_supabase_pagination_cap.md` (the 1000-row cap that justifies §5.5 / §7's "all SELECTs via psycopg3")
- `tests/test_review_config.py` (template for `tests/test_self_transfer_patterns.py`)
- `tests/test_readonly_client.py` (template for integration-test gating + fixture pattern)
