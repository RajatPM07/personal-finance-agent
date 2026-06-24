# PM Learning — Personal Finance Agent

**Project:** Personal Finance Agent (PFA)
**Stack:** Python + Supabase + Telegram, single-user, 24/7 on a spare Mac
**Status as of this doc:** Week 1 (foundation) + Week 2 (ingestion: ICICI CC PDF + AMEX CC XLSX) shipped. Week 3 scope open.

This doc is a study artifact — the PM-side handoff for the project. Three layers: snapshot, engineer-grade handoff, then the PM lens against each handoff section.

---

## Layer 1 — Snapshot

One Python process (`app.py`) runs four concurrent things on a spare Mac:

1. **Aiogram bot** polling Telegram (whitelisted to one chat ID).
2. **APScheduler** running two 15-min jobs: stale-heartbeat check + external healthchecks.io ping.
3. **FastAPI `/health`** on `127.0.0.1:8765`.
4. **Folder watcher** on `~/finance-inbox/` — picks up PDFs/XLSX, dispatches to a parser, ingests into Supabase.

Outside this process: a **launchd-supervised backup job** (`pg_dump` daily at 3 AM), kept separate so it can't block the async loop.

Storage: Supabase Postgres. Writes go through the `service_role` key. Readonly path (psycopg + `finance_agent_readonly`) lands end of Week 3 to unblock the Week 4 SQL agent (originally Week 5; reprioritized).

LLMs: routed through one function `llm(task, prompt)` reading `config/model_routing.yaml` and dispatched via LiteLLM. Today: Gemini Flash for extraction, Groq Llama for categorization, Claude Sonnet 4.6 for high-stakes tasks (SQL agent, affordability reasoning).

Parsers shipped: ICICI CC (PDF) and AMEX CC (XLSX). Both export `__parser_version__` so a parser bump forces a clean re-ingest path via the `import_hash` chain.

---

## Layer 2 — The mental model

Three concepts unlock everything else.

### (a) Idempotency via `import_hash`

The same statement could land twice (re-drop a PDF, ICICI re-emails it, drag it from Downloads twice). Every transaction row has a SHA-256 fingerprint. The DB has a unique constraint on it. Re-ingesting is a no-op. Two modes:

- **Mode A** (time-bearing — SMS, txn emails): hash includes the timestamp.
- **Mode B** (PDF — what's live today): hash includes `pdf_content_hash` + `source_row_ordinal` + `parser_version` because PDFs lack timestamps and can have two identical-looking rows on the same date.

The hash deliberately **excludes `source_ref`** so the same txn seen via SMS *and* email dedups to one row. This is the single most important design decision in the system; almost every "wait, what about…" question maps back to it.

### (b) Async/sync bridging via `adb()`

Aiogram is async. Supabase's Python client is sync. If you call Supabase directly from a handler, you block the event loop and the bot freezes. So every Supabase call goes:

```python
await adb(lambda: client.table(...).insert(...).execute())
```

Two repeat bugs sit on top of this: forgetting `.execute()` (silent no-op), and calling Supabase outside `adb()`. Both are CLAUDE.md invariants.

### (c) Failure detection > failure prevention

You can't prevent ICICI changing PDF formats, Anthropic running out of credit, the Mac falling asleep, or Supabase RLS silently filtering reads. So the system is designed to *notice and shout*: per-component `heartbeat` table → 30-min stale check → external healthchecks.io watchdog → secondary-bot alert. Plus statement-level totals validator that rejects the whole statement if row sums don't match the declared total. Every silent-failure mode hit becomes a regression test.

---

## Layer 3 — Engineer-grade handoff doc

### 1. What this thing does

Single-user personal finance agent for an Indian household. User drops a credit-card statement into a folder (or forwards on Telegram), system parses, dedups, writes clean rows into Supabase, replies on Telegram with a summary. Later phases add SMS/email ingestion, an LLM SQL agent, affordability reasoning, morning brief. Today only credit-card statement ingestion ships — but the foundation is built for all of it.

Runs 24/7 on a spare Mac. Not a cloud service. That deployment fact drives most decisions.

### 2. How it's deployed

One Python 3.11+ process started by **launchd** (`KeepAlive=true`). If it crashes, launchd restarts it. No Docker, no Kubernetes, no cloud function. Mac never sleeps (energy settings in the precondition checklist).

A **second** launchd job runs `pg_dump` nightly at 3 AM. Deliberately not in APScheduler — `pg_dump` is sync and slow; would block the loop for minutes.

**Healthchecks.io** expects a ping every 15 minutes. If the watchdog stops pinging, healthchecks.io alerts via a *secondary* Telegram bot. Two-bot design: main bot for daily interaction, secondary bot for system alerts. Independent so a bug in the main bot can still scream for help.

### 3. Repo map (key files)

- `app.py` — entry point: signal handlers + `asyncio.gather`.
- `PRD.md` — source of truth for product scope. Reference §-numbers.
- `CLAUDE.md` — invariants. Read before changing any.
- `config/model_routing.yaml` — task → primary model + fallbacks.
- `credentials.yaml` — gitignored. PDF passwords keyed by `<bank>_<last4>`.
- `migrations/001_init.sql` — schema. RLS explicitly DISABLED.
- `migrations/*.local.sql` — gitignored. Real secrets/PII.
- `skills/finance/lib/db.py` — `service_client()` + `adb()` async wrapper.
- `skills/finance/lib/llm.py` — `llm(task, prompt)`, single LiteLLM entry.
- `skills/finance/lib/hashing.py` — Mode A + Mode B `import_hash`.
- `skills/finance/bot/main.py` — Bot, Dispatcher, heartbeat middleware, `/ping`, `/model list`.
- `skills/finance/bot/document_handler.py` — Telegram doc → inbox with canonical name.
- `skills/finance/ingestion/_common.py` — dataclasses, bank detection, credentials lookup.
- `skills/finance/ingestion/statement_validator.py` — pure: declared totals vs row sums.
- `skills/finance/ingestion/pipeline.py` — validate → upsert → log.
- `skills/finance/ingestion/folder_watcher.py` — watchdog on inbox.
- `skills/finance/ingestion/parsers/icici_cc.py`, `amex_cc.py` — per-bank parsers.
- `skills/finance/monitoring/{alerts,heartbeat,health}.py` — alerts, watchdog jobs, `/health`.
- `tasks/todo.md` — current week's plan.
- `tasks/lessons.md` — institutional memory; read at session start.
- `tasks/preconditions-notes.md` — gitignored; verified library API surfaces.

### 4. End-to-end happy path: the journey of a PDF

1. User drops `icici_cc_apr_2026.pdf` into `~/finance-inbox/`.
2. **folder_watcher** — watchdog Observer fires `on_created`, posts to the asyncio loop.
3. **handle_new_file** — token-match filename → `"icici_cc"`; check extension (`.pdf` expected); look up password from `credentials.yaml` (single match → password; multi-match without `last4` → error).
4. **dispatch_to_parser**.
5. **parsers/icici_cc.py — `parse(path, password)`**:
   - sha256 of file bytes → `pdf_content_hash`.
   - `pikepdf.open(password=...)` decrypts to a temp PDF.
   - `pdfplumber` extracts text page by page.
   - Regex over each line → `ParsedRow`s with `source_row_ordinal` 1..N.
   - Regex for declared totals labels; if missing, derive from row sums and flag `_derived_from_rows=True`.
   - Returns `ParseResult(rows, declared_totals, pdf_content_hash, parser_version="icici-cc/v1")`.
6. **pipeline.ingest**:
   - `validate(pr)` — declared vs extracted in/out, ₹1 tolerance. Fail → write `total_check_failed` to `ingestion_log`, **zero** rows inserted, return.
   - For each row, compute `import_hash_pdf(...)`.
   - `adb(lambda: client.table("transactions").upsert(rows, on_conflict="import_hash", ignore_duplicates=True).execute())`.
   - `rows_added = len(response.data)` — 0 means everything was a dup → `status='skipped_duplicate'`.
   - One row to `ingestion_log` with status + counts.
7. `_send_summary()` — main bot replies on Telegram: `📥 ICICI CC apr_2026.pdf ingested — 47 rows, ₹84,231 (declared ₹84,231) — totals match ✓`.

Telegram document upload uses the same path: `bot/document_handler.py` saves to the inbox with a canonical filename and folder watcher takes over from step 2.

### 5. The data model

13 tables + `request_logs` (LiteLLM cost callback). Tables you'll touch in Week 2-3:

- **`transactions`** — heart of the system. `import_hash UNIQUE`. `source_ref` is *audit-only* (NOT in hash). `pdf_content_hash`, `source_row_ordinal`, `parser_version` participate in Mode B hash.
- **`accounts`** — `type IN ('bank','credit_card','upi','broker','mf')`. UUIDs seeded; watcher hardcodes them in `ACCOUNT_IDS`.
- **`ingestion_log`** — every parse attempt logs here. Status: `success | skipped_duplicate | possible_duplicate | failed | needs_review | total_check_failed`. Audit trail.
- **`heartbeat`** — append-only ping log. 30-min stale check looks at the most recent row per component.
- **`request_logs`** — LiteLLM callback target. Per-call cost, latency, model, prompt.

RLS explicitly disabled on every table — V1 is single-user. V2 multi-user turns it back on with policies in a new migration.

### 6. The four abstractions

1. **`import_hash` — idempotent dedup with two modes.** See Layer 2 (a).
2. **`adb()` — only async-safe path to Supabase.** See Layer 2 (b).
3. **`.execute()` terminator.** Supabase builder is lazy; `.insert(payload)` alone is a no-op. CLAUDE.md invariant #2; regression test in `tests/test_bot_middleware.py`.
4. **Failure-loud, not failure-quiet.** Every layer notices and shouts. Validator rejects whole statement on totals mismatch (₹1 tolerance). Heartbeat + watchdog. Honest LLM fallbacks (`groq/llama-3.3-70b-versatile` for SQL agent when Anthropic is down — *materially worse*, surfaced honestly).

### 7. Conventions you'll get yelled at for breaking (CLAUDE.md invariants)

1. Async → sync Supabase only via `adb()`.
2. Every Supabase chain ends at `.execute()`.
3. `import_hash` recipe is locked. Never add `source_ref`.
4. Every parser exports `__parser_version__`.
5. Versioned filenames when source format changes (`icici_cc_v1.py` → `icici_cc_v2.py`).
6. Backups never on APScheduler — separate launchd job.
7. Every Telegram handler starts with `_is_rajat(message)`.
8. **`readonly_client()` lands end of Week 3** (was Week 5; reprioritized to unblock Week 4 SQL agent).
9. Secrets split: `.env` for flat env vars; `credentials.yaml` only for keyed PDF passwords.
10. Real-PII fixtures gitignored.
11. Never pin a library API from memory — verify with `help()` / `pip index versions` / `inspect.getsource()` and write to `tasks/preconditions-notes.md`.

Plus: no AGPL code copied; no `print()` in long-running paths; no raw card numbers / OTPs in prompts or logs; no LiteLLM calls bypassing `llm()`.

### 8. Operational story

- Logs: `logging_setup.configure_logging()` (rotating file + stdout).
- Process status: `launchctl print gui/$(id -u)/com.rajat.pfa.app`.
- Health: `curl http://127.0.0.1:8765/health`.
- Heartbeat freshness: `select component, max(last_ping) from heartbeat group by 1;`.
- Recent ingests: `select * from ingestion_log order by timestamp desc limit 10;`.
- Cost audit: `select model, sum(total_cost) from request_logs where created_at > now() - interval '7 days' group by 1;`.
- Backups: `~/Backups/pfa/*.sql.gz`. Restore drill: `scripts/restore_drill.py`.

### 9. Local dev workflow

- `python app.py` — foreground dev run.
- `make test && make lint && make typecheck` — definition of done.
- `pytest tests/test_<parser>.py -v` — parser smoke.
- LLM calls mocked by default. Live smoke in gitignored `scripts/smoke_*.py`, deleted after.
- **Venv gotcha:** if you ever `mv` the repo, recreate venv (`python -m venv .venv --clear`) — `bin/activate` hardcodes the original path while `bin/python` resolves relative; they diverge silently.

### 10. Status

- **Done (Weeks 1–2):** foundation + ICICI CC PDF + AMEX CC XLSX ingestion, folder watcher, Telegram doc upload, validator, Mode B hash, end-to-end Telegram summary, heartbeats, watchdog, backups.
- **Up next (Week 3):** Gmail or SMS ingestion + readonly DB role for the SQL agent.
- **Intentionally absent (do not build):** merchant normalization (Week 5 with parser_version bump), RLS policies (V2), `casparser`/`camelot` deps (added when imported), AGPL-licensed components.

---

## Layer 4 — The PM lens (what to know, ask, watch)

Mapped one-to-one against the handoff sections above.

### 1. What this thing does — PM lens

- The "single-user" assumption is **load-bearing**. It's why no auth, no RLS, no tenancy. Adding a second user (e.g. spouse) is a V2-grade migration: RLS turn-on, per-user policies, onboarding flow, whitelist becomes a list, account UUIDs out of `_common.py`. Cost it accordingly.
- V1 scope = "I drop a statement, you tell me what's in it." Anything beyond (alerts, summaries, planning) is a *separate* roadmap line. Don't quietly fold them in.
- This is honestly a personal tool, not a product. That's a feature. Don't "productize" prematurely.

### 2. How it's deployed — PM lens

- Deployment shape *is* your reliability story. SLA is "as reliable as a home computer on home Wi-Fi." Set expectations.
- Recovery time if Mac dies = however long it takes to set up a new Mac. No DR plan beyond backups. Decide if that's acceptable.
- Two-bot design (main + alerts) is a deliberate UX call. Don't merge to "simplify."
- External healthchecks.io is the watcher of the watcher. Free tier is fine for one user; multi-tenant means a paid line item.
- Backups as a separate launchd job → policy: backup integrity is decoupled from app uptime. Worth knowing when explaining the system.

### 3. Repo map — PM lens

- `PRD.md` is the spec. "What should this do?" → "see §X," not "we discussed in Slack."
- `tasks/lessons.md` is institutional memory. Read at session start. Protects from re-litigating mistakes.
- `tasks/todo.md` is the current week's plan. If stale, intent has drifted. Treat like a Jira board.
- `*.local.sql` and `credentials.yaml` are real PII / secrets. Real values in `*.local.sql`; committed file is a template.
- Three planning surfaces (PRD, todo, lessons) and you own all three. PM-grade load — but no engineer can blame ambiguity.

### 4. End-to-end happy path — PM lens

- The 7-step path *is* your acceptance criteria for V1 ingestion. Memorize it.
- User-visible failure points:
  1. Filename mismatch → `.rejected` + alert. UX trade-off: traded auto-detection for explicit rejection.
  2. Wrong extension → rejected. ICICI=PDF, AMEX=XLSX hardcoded. New banks = new code.
  3. Password lookup fails → rejected with reason. User edits `credentials.yaml`.
  4. Validator fails (totals mismatch) → entire statement rejected, zero rows. Half-ingested data is worse than none.
  5. Duplicate → silent success message. Good UX, but user never sees "tried 47, all dups."
- The Telegram summary text is the *entire* user-facing surface for ingestion. Treat the format as a product spec — every change is a UX change.
- The `_derived_from_rows` annotation is a UX honesty bet — "validator effectively skipped" is surfaced, not swallowed. Defend this principle.

### 5. The data model — PM lens

- `transactions` is the contract with every downstream feature (SQL agent, affordability, morning brief, weekly review). Schema changes are expensive — every reader has to know.
- `source_ref` is audit-only. Don't let any future feature "use `source_ref` to filter dupes" — it'll break the design.
- `ingestion_log` is your support tool. "I dropped the file but nothing happened" → `select * from ingestion_log order by timestamp desc limit 5;` is your first query.
- `request_logs` is your LLM cost dashboard in raw form. Run a weekly query to catch a runaway prompt before the cap fires.
- `heartbeat` tells you what subsystems are alive. Future feature stops working → "is this component pinging?" not "is the app up?"
- Schema has `assets`, `liabilities`, `goals`, `commitments`, `income_events` already — empty by design. When you finally implement, you populate; you don't redesign.

### 6. The four abstractions — PM lens

- **`import_hash`** is the product promise: "Drop the same statement twice. Forward from Gmail and the bank. Re-process after a parser change. We won't double-count." If broken, data corrupts silently — user doesn't notice until totals are wrong. Test like your life depends on it.
- **`adb()`** is invisible to users. PM-relevant only on failure: "the bot froze for 30 seconds" → first hypothesis is "someone bypassed `adb()`."
- **`.execute()`** is invisible bug class. Pattern recognition: bug whose symptom is "appeared to succeed but nothing was written" → "did we forget `.execute()`?"
- **Failure-loud** is *the* product principle. Every feature you scope must answer: what's the silent failure mode, and where does it scream? If the answer is "we'll find out from logs," scope an alert *into* the feature.

### 7. Conventions — PM lens

Most are engineering hygiene; a few are product policies in disguise:

- **Versioned filenames when format changes** has a user-visible side effect: re-ingest after a parser bump produces *new hashes*, so user sees "47 rows added" for data they thought was already loaded. Plan migration messaging.
- **Whitelist-only handlers** blocks every V2 sharing scenario. The first day you say "share read access with my CA" is when you confront this.
- **`readonly_client()` lands end of Week 3** (reprioritized from Week 5). YAGNI was loud about not building it; the reprioritization is now explicit because Week 4's SQL agent depends on it.
- **Real-PII fixtures gitignored** is privacy posture. A PR with real data is *an incident*, not a code-review nit.
- **No AGPL** is legal posture. "Just copy from Firefly III" → see `LICENSE_AUDIT.md`. AGPL contamination is irreversible.
- **Never pin from memory** is research discipline. When an engineer says "I think `bout` does PDFs" or "Sonnet 4.7 is out," reflex is "verify and write to `preconditions-notes.md`," not "great, let's go."

### 8. Operational story — PM lens

- You ARE the on-call. Healthchecks.io pages you; no one else is looking. Be debug-ready from a phone in 2 minutes.
- Define SLOs explicitly (in your head): "ingestion < 60s," "alert-to-fix < 1 day," "data loss tolerance = 0 rows." Without numbers, you'll silently degrade.
- Backup verification is the test you skip and regret. `restore_drill.py` exists. Run monthly. A backup never restored is Schrödinger's backup.
- Cost ceilings (₹1,500 soft / ₹3,000 hard) are *product constraints*. Shape feature scope. Past the cap → kill or raise *deliberately*, not by accident from a chatty prompt.
- The Groq-Llama fallback path is a degraded-mode product decision. Materially worse SQL on complex queries. Don't pretend it's equivalent — surface the degradation.

### 9. Local dev workflow — PM lens

- Mostly engineer-facing. The venv-divergence lesson is a process risk in disguise — breaks invisibly during machine migrations / contractor onboarding. Costs ~15 minutes; budget it.
- `make test lint typecheck` before declaring anything done. When you hand a task, "passes all three" is your definition of done — not "it works on my machine."

### 10. Status — PM lens

This is the most PM-shaped section.

- What's done is V1 ingestion for two banks only. Everything else (Gmail, SMS, SQL agent, morning brief, affordability) is roadmap, not "almost there."
- Week 3 scope is your decision. Architecture is ready for Gmail or SMS. Pick on user-value-per-engineering-hour. Gmail dedup against later-arriving PDFs = riskier integration; SMS via Tasker = faster to value but Android-coupled. Frame the trade-off explicitly.
- "Intentionally absent" is a feature. Every parked item has a reason. When the urge to build appears, check the deferred list first.
- Schema is full-PRD-ready, most tables empty. Front-loaded design cost; populate-don't-redesign when you implement.

### 11. Onboarding cost — PM lens

- New engineer productive in ~1 day if they follow §11 of the handoff. Useful number for "contractor for Week 4?" decisions.
- Expensive part of onboarding is *invariants*, not code. Code is re-readable; invariants are tribal knowledge. CLAUDE.md + lessons.md are your transferable institutional memory. Keep them current.

---

## Two cross-cutting habits worth building

1. **Read `tasks/lessons.md` weekly.** Only place where "what we learned the hard way" is durable. Same lesson appearing twice = process bug. Fix the process, not the symptom.
2. **Three questions for every proposed feature:** *(a) what's the silent failure mode? (b) where does it scream when it fails? (c) what's the cost ceiling?* These catch 80% of trouble before code gets written. PM version of failure-loud.

---

## How to use this doc

- Pick a section that feels thin and study it.
- Try answering the EM-style questions out loud without re-reading. Where you stumble is the gap.
- Update this doc when invariants change (e.g. invariant #8 moved from Week 5 → end of Week 3 — that's the kind of change that should propagate).
