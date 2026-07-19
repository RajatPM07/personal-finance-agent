# Plan — Bank as UPI source-of-truth for Ayushi's ICICI savings

## Problem
Ayushi's ICICI savings `OpTransactionHistory*.xls` (13 Jan–13 Jul 2026) is ingested with
the **D1 rule** that drops all UPI rows (`is_upi_skip=True`) because Paytm is the assumed
source of truth. Ayushi does **not** use Paytm — she pays merchants by UPI straight from
this account. Result: the large majority of her UPI rows (her real quick-commerce /
food-delivery spend: Zepto, Swiggy, Blinkit, Zomato, BigBasket) were **never ingested**;
the DB showed a tiny fraction of actual spend.
(Exact row counts / amounts / account number: local session memory — kept out of the repo
as third-party PII.)

Decision (Rajat): **Bank = source of truth.** Ingest all bank UPI rows; supersede the
PhonePe person-name rows that duplicate them.

## Steps
- [ ] 1. **Account-scope the D1 rule.** Add an `include_upi` path so the pipeline inserts
      UPI rows for *this* account only. Mechanism: config set of account_ids (or a param on
      `ingest()`), NOT a global flip — Rajat's Paytm-backed accounts must keep skipping UPI.
      Update CLAUDE.md D1 note to record the account-scoped exception.
- [ ] 2. **Re-ingest the xls** through `icici_savings_xls.parse` → `ingest()`.
      Ordinals are stable (parser numbers every row incl. UPI), so the 50 existing rows
      dedupe on `import_hash`; the UPI rows insert fresh. Verify the count delta matches
      the UPI-row count in local memory.
- [ ] 3. **Categorize the new UPI merchant rows.** Deterministic map on the `UPI/<name>/…`
      counterparty: Swiggy/Zomato → Food Delivery; Zepto/Blinkit/Instamart/BigBasket →
      Groceries; known society/staff names → existing maps; person / masked VPAs →
      Needs Review (fed to the labeling worksheet, not guessed).
- [ ] 4. **Supersede the PhonePe duplicates** (overlap count in local memory). Match bank-UPI ↔ PhonePe on
      (date, amount), 1:1 greedy (handle 1:N by count, not blind delete — two ₹200 same-day
      payments must not collapse). Delete matched PhonePe rows (bank row is superior: real
      merchant name). Dry-run diff first; log every deletion; rollback snapshot kept.
- [ ] 5. **Verify:** the quick-commerce/food merchants now attributable; no double-count vs PhonePe;
      spend totals reconcile; `make lint/typecheck/test` clean; add a regression test that
      Ayushi's savings account ingests UPI while a Paytm-backed account does not.

## Risks
- **Coincidental (date, amount) collisions** in step 4 → false dedup. Mitigate: strict 1:1
  greedy match + count reconciliation + dry-run diff before any delete.
- **D1 scope change** is an invariant edit — must be explicit + tested (steps 1, 5).
- Steps 2–4 mutate the live DB → rollback snapshot before each write phase.
