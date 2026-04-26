# V1 Roadmap r2 — W3/W4/W5 Reprioritization

**Status:** Locked
**Date:** 2026-04-26
**Supersedes:** PRD §11 Week 3–5 ordering (original, written pre-Week-1)
**Triggered by:** Post-Week-2 reprioritization session — drop SMS path from V1, replace investment-API integrations with static asset rows, defer affordability engine until 60-day data threshold.

## 1. Why this reordering happened

PRD §11 packed Week 3 with seven independent ingestion paths (SMS×3, Paytm, MF CAS, Zerodha API, Payslip), Week 4 with five reasoning items including affordability, and Week 5 with categorization. Realistic V1 priorities differ:

- **SMS infrastructure (Android forwarder + Email→Supabase pipeline) is heavy V1 scope** for marginal incremental coverage given Paytm + statements already capture most spend.
- **Investment data needs to *exist* in V1** for net-worth queries, but live API integration (Kite Connect ₹2,000/mo, MF CAS recurring uploads) isn't worth building before usage patterns are known.
- **Affordability engine has a 60-day data-quality guard** (PRD §6.5). Today is 2026-04-26; ingestion goes live mid-W3. The engine cannot return confident answers until ~late June regardless of when the code ships → engineering it now risks designing against synthetic data.
- **Anthropic balance is ~₹420 / $5** (per Week-1 close memory). Payslip + Morning Brief + future LLM calls require explicit top-up; not silent burn.

This document locks the new ordering. Future revisions append a `vN → vN+1 changelog` section like Week 2's spec did.

## 2. Week 3 — Sources (locked)

5 tasks. Order matters: Paytm first builds momentum on a familiar pattern; readonly client closes the week so Week 4's SQL agent has its prerequisite ready on day 1.

| # | Task | Type | LLM cost | Est |
|---|---|---|---|---|
| W3.1 | **Paytm XLSX parser** — Telegram-drop + folder watcher + `pandas.read_excel` → ingestion pipeline | deterministic | none | ~1.5 days |
| W3.2 | **Investment static-asset seeding** — verify existing MF row (`Zfunds MF static-₹2L`); add Zerodha row to `assets` (~₹2L). Migration `004_static_assets.sql` | seed | none | ~0.5 day |
| W3.3 | **Anthropic balance top-up — gating task** | manual | — | blocks W3.4 |
| W3.4 | **Payslip parser** — Claude Sonnet structured output via Pydantic → `income_events` | LLM | Sonnet calibration | ~2 days |
| W3.5 | **Readonly DB client** — `psycopg` + `SUPABASE_READONLY_PASSWORD`; Supavisor pooler username format `<role>.<project_ref>` (per lessons.md 2026-04-25). Smoke test: positive-read assertion against seeded `accounts`. | infra | none | ~0.5 day |

**Out of W3 / explicitly deferred:**

- 3.1 SMS parser → Backlog (post-V1; reopen if Paytm + statements miss meaningful signal)
- 3.2 SMS-to-Email forwarder → cut from V1 entirely
- 3.3 SMS reconciliation → cut from V1 entirely
- 3.5 MF CAS via `casparser` + 3.6 Zerodha API/CSV → replaced by W3.2 static seed (per user call)

**Open W3 risk:** rolling-statement overlap (PRD §7.420) — same real txn appearing in two PDFs (e.g., monthly + year-end consolidated) yields different `import_hash` → two rows. **Decision: defer auto-reconciliation; handle via manual `/dedup` in W4.3.** Build the auto path only after we observe real overlap cases in production.

## 3. Week 4 — Reasoning (locked)

3 tasks. `/afford` is no longer in this list — it's data-gated backlog (§5).

| # | Task | Notes |
|---|---|---|
| W4.1 | **`sql_agent.py`** | Read-only role + SQL validator. Prerequisite is W3.5 readonly client. |
| W4.2 | **Morning brief + weekly review** | APScheduler-driven; LLM-light template + numbers. |
| W4.3 | **Commands** `/exclude` `/categorize` `/retry` `/dedup` | 4 commands, **not 5** (`/afford` deferred). `/dedup` is the manual path for the rolling-statement decision deferred from W3. |

**Out of W4:**

- 4.4 Affordability engine + 60-day guard → Backlog — Data-gated (§5)
- 4.5 Refund detection → Moved to W5.1 (first item)

## 4. Week 5 — Categorization + Refund (locked)

5 tasks, ordered by dependency:

| # | Task | Why this position |
|---|---|---|
| W5.1 | **Refund detection** (was W4.5) | Cheap, deterministic, cleans dual-entry refund noise from W4.2 briefs and future affordability calculations. Quick win to start the week. |
| W5.2 | **Hand-curated `merchants_india.json` (top 200)** | Manual data work; foundation for tiered pipeline. Can run in parallel with W5.3. |
| W5.3 | **MCC taxonomy seed** (`greggles/mcc-codes`) | One-shot static import; pairs with W5.2. |
| W5.4 | **Tiered normalization pipeline** — curated → rapidfuzz → pgvector → LLM fallback | Replaces W2's ad-hoc categorization. Depends on W5.2 + W5.3. |
| W5.5 | **DistilBERT baseline categorizer** — *optional* | Decision based on LLM-tier hit-rate observed after W5.4. Skip if LLM tier fires <10%. |

## 5. Backlog — Data-gated (new track)

These items are committed to V1 but **not assigned a week** until their data trigger fires.

| Item | Trigger | Earliest realistic activation |
|---|---|---|
| **Affordability engine + 60-day guard** (was W4.4) | ≥60 days of clean ingested history (`history >= 60d` AND `>10% uncategorized` AND `no recent failed-totals`) | 2026-06-25 → 2026-07-10 (depending on W3 first-ingest date) |
| **SMS parser** (was W3.1) | Reopened if/when Paytm + statements show consistent signal gaps; or post-V1 | Post-V1 |

These items live in this section of `tasks/todo.md` once the file is split per-week. They are **not forgotten**, just not active.

## 6. Cross-cutting decisions made this session

| Decision | Call | Rationale |
|---|---|---|
| Rolling-statement overlap reconciliation | Manual `/dedup` only (W4.3) | Avoid over-engineering; observe real cases first |
| Anthropic budget approach | **Just-in-time top-up** before W3.4 (Payslip) | Smaller spend; explicit gating task W3.3 makes it visible |
| Readonly DB client placement | **End of Week 3 (W3.5)**, not Week 5 | SQL agent (W4.1) needs it day-1; original "Week 5" placement was stale |
| Investment data approach | Static seed rows (W3.2) | Real API integration not worth recurring cost in V1 |
| `/afford` shipping | **Deferred entirely until 60-day data exists** | Synthetic-data design risks abstractions that don't survive real spend patterns |

## 7. Required CLAUDE.md / PRD updates

- **CLAUDE.md invariant 8** ("No `readonly_client()` until Week 5") — now stale. Updated in same commit as this spec to reference Week 3 closing. (See diff in commit message.)
- **PRD §11** — original ordering preserved as historical context; this doc is the living plan. PRD will be amended at V1 acceptance review, not piecemeal.

## 8. What "Locked" means here

- W3 task list is the input to the next-step `superpowers:writing-plans` invocation (Paytm XLSX is first sub-spec).
- W4/W5 entries are **commitments at task-level granularity** — implementation specs for each are written when their week begins.
- Changes to this plan land via a `vN → vN+1 changelog` section appended at top, mirroring Week 2's spec convention.

## 9. References

- PRD.md §11 (build sequence, original)
- PRD.md §6.5 (data-quality maturity guard for `/afford`)
- PRD.md §7.420 (rolling-statement overlap, `import_hash` Mode B)
- `tasks/lessons.md` 2026-04-25 (Supavisor pooler username convention — applies to W3.5)
- `docs/superpowers/specs/2026-04-26-week-2-ingestion-design.md` (Week 2 reference; W3.1 Paytm reuses its folder watcher + Telegram doc handler patterns)
- Memory: `project_pfa_status.md` (Anthropic ₹420 balance, Zerodha account exists in seed, MF static-₹2L already seeded)
