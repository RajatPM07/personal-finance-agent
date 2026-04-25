# Lessons — Personal Finance Agent

Append any correction received during execution here. Read before starting sessions.
Format: date → pattern → root cause → rule.

---

## 2026-04-21 — r2 plan review caught four bugs after sign-off

**Pattern:** Week 1 plan r2 was "signed off" but a focused second pass found four bugs that would have bitten during execution: (a) heartbeat middleware missing `.execute()` terminator, (b) readonly role password committed to git, (c) seed PII committed to git, (d) `python-dotenv` missing from deps despite backup script using it.

**Root cause:** Plan review was scoped to architecture and omissions, not to line-level bug-hunting inside the code blocks. Each bug was local to one step, hidden by surrounding correct code.

**Rule:** Plan review is not just "does this cover the spec"; also check every code block for silent-failure modes (un-terminated builders, committed secrets, unstated imports). Especially for Supabase client calls — `.insert(payload)` without `.execute()` is a known repeat offender and now has a dedicated regression test in `tests/test_bot_middleware.py`.

**Captured as:** `CLAUDE.md` invariant #2; regression test at `tests/test_bot_middleware.py`; plan revisions r2 → r3 in `tasks/todo.md`.

---

## 2026-04-21 — bout API discrepancy & minor hallucination on model ID

**Pattern:** The plan incorrectly assumed `bout` supported PDF extraction based purely on PyPI descriptions and incorrectly assumed the Anthropic Sonnet model was `claude-sonnet-4-7`.

**Root cause:** Trusting a "remembered or surfaced fact" without explicit verification during the research/planning phase. 

**Rule:** Never pin a library version, model ID, or API surface from memory or from a summary document. Run `help()`, `pip index versions`, or a one-line smoke call and trust verified output, not recollection.  Write the output to `tasks/preconditions-notes.md`.

**Captured as:** `CLAUDE.md` invariant #11; rewrites applied to `todo.md` and `github_research_results.md` during M1-M10.

---

## 2026-04-21 — Week 1 `pyproject.toml` shipped Week 2+ deps with pin conflicts

**Pattern:** The r3 `pyproject.toml` included `casparser>=0.8` (used only in Week 3 MF CAS ingestion) and `camelot-py[cv]>=0.11` (used only in Week 2+ table extraction). `casparser` hard-pins `pdfminer-six==20240706`, which is incompatible with every `pdfplumber>=0.11` release; resolver gave `ResolutionImpossible`. `camelot-py` had its `[cv]` extra removed in 1.0 (opencv is now a core dep), so the `[cv]` modifier would fail on current stable.

**Root cause:** Copying "the full stack" into Week 1 deps up front without asking whether each library is actually imported during Week 1. YAGNI violation with a compounding cost — premature dep = premature pin conflict.

**Rule:** `pyproject.toml` deps are scoped to the week that imports them. A library used first in Week 3 goes in during Week 3, not "as part of setup." Apply this retroactively to every weekly plan: audit the dep list against the weekly import surface before committing.

**Captured as:** `tasks/todo.md` Task 1.5 `pyproject.toml` block updated to drop `casparser`, update `camelot-py`; lessons entry above.

---

## 2026-04-25 — Week 1 wrap-up: subagent-driven-development worked, two pattern catches worth preserving

**Pattern:** Across Tasks 0–11, the subagent-driven workflow caught two non-obvious bugs that would have shipped silently if a single human-coded the whole stack:

1. **`bout` library doesn't parse PDFs** (it's a CSV→QIF converter). Caught at Task 0 verification before any wrapper code was written. Preconditions notes captured the actual API surface; Task 9 was rewritten to use `pikepdf` + `pdfplumber` directly. Without Task 0, this would have surfaced mid-Week-2 and broken the ICICI ingestion path.
2. **aiogram 3.27's `start_polling()` defaults to `handle_signals=True`**, which clobbers user-defined `loop.add_signal_handler(SIGTERM, ...)` calls. Caught at Task 7 by `inspect.getsource(Dispatcher.start_polling)` instead of trusting docs/memory. Without this catch, graceful shutdown would have appeared to work in tests but actually no-op'd in production — heartbeat rows wouldn't flush, bot session wouldn't close cleanly.

**Root cause (both):** Library API assumptions from research summaries or memory, not from running `help()` / reading source. CLAUDE.md invariant #11 ("Never pin a library version, model ID, or API surface from memory") is the rule that catches both — Task 0 is its enforcement mechanism for new dependencies, and `inspect.getsource` is its enforcement mechanism for kwarg defaults inside hot paths.

**Rule:** Before wrapping any third-party library entry point that has non-obvious side effects (signal handlers, threading, cleanup), read the actual source. `inspect.getsource(<callable>)` is one line; the cost of NOT running it is debugging a silent production failure six weeks later.

**Captured as:** Task 0 preconditions verification step (now mandatory for any new library); Task 7's explicit `handle_signals=False, close_bot_session=False` parameters in `app.py` with an inline comment explaining the override.

---

## 2026-04-25 — Supabase Supavisor pooler requires `<role>.<project_ref>` username

**Pattern:** Connecting to Supabase Postgres via the Supavisor pooler (port 6543, the recommended path) with a custom Postgres role like `finance_agent_readonly` fails with `FATAL: (ENOIDENTIFIER) no tenant identifier provided (external_id or sni_hostname required)` if you use just the role name.

**Root cause:** Supavisor is a multi-tenant connection pooler. It needs to know which Supabase project the connection is for. The convention is to encode the project ref as a username suffix: `finance_agent_readonly.<project_ref>` instead of `finance_agent_readonly`. The project ref is the subdomain of `SUPABASE_URL` (e.g., `wqfwazmndhvoxrkatfkv` for `https://wqfwazmndhvoxrkatfkv.supabase.co`).

**Rule:** Any psql or psycopg connection going through the Supavisor pooler with a non-default role must format the user as `<role>.<project_ref>`. Direct port-5432 connections do not need this (different connection path), but Supabase recommends the pooler for everything except short-lived migrations.

**How to apply:**
- The Week 5 SQL agent (psycopg + `SUPABASE_READONLY_PASSWORD`) MUST format the connection user as `finance_agent_readonly.<project_ref>`. Hardcoding just `finance_agent_readonly` will fail at runtime, not at startup — make this a connection-time test.
- The `migrations/002_verify_readonly.sql` runner already knows this; if you copy that pattern for any future role-based connection, preserve it.
- For any helper that constructs a connection string from `.env`, derive `<project_ref>` from `SUPABASE_URL`'s subdomain rather than asking the user for a separate var.

**Captured as:** This lessons entry; the pattern was discovered while running the Week 1 readonly verification drill on 2026-04-25.

---

## 2026-04-25 — Supabase auto-enables RLS on tables created via the SQL editor (and the Table Editor's "RLS: Disabled" column lies)

**Pattern:** Tables created via the Supabase SQL editor have `pg_class.relrowsecurity = true` by default. With RLS on and **zero policies**, any non-superuser role's SELECT returns zero rows — no error, just silent filtering. The Supabase dashboard's Table Editor shows "RLS: Disabled" in its column even when RLS is actually enabled with zero policies. Two different states, same display label.

**Why service_role still saw the data:** Postgres superuser/`bypassrls` roles bypass RLS by default. The `service_role` Supabase issues to its REST API has bypassrls; the `finance_agent_readonly` role we created does not. Result: ingestion writes (via service_role) succeed; readonly reads (via finance_agent_readonly) silently return nothing. Worst-of-both: data is in the table, but the SQL agent will say "you have no transactions."

**Why our Week 1 readonly drill missed it:** `migrations/002_verify_readonly.sql` only tested the negative case ("can readonly mutate?" → no, correctly). The single positive line `SELECT count(*) FROM transactions` ran against an unseeded `transactions` table (Week 2 ingestion hadn't started), so `count = 0` looked like the right answer. Negative-only tests can't surface this class of bug.

**Discovery path:** Caught while doing a sanity-check `SELECT type, institution, nickname FROM accounts` as readonly before Week 2 brainstorming — got 0 rows even though service_role REST showed 7 accounts. `has_table_privilege(... 'SELECT')` returned `t` for every table, so the GRANT was correct; `pg_class.relrowsecurity` was `t` everywhere despite `pg_policies` being empty. That triangulated to RLS auto-enable.

**Rule:**
1. For V1 single-user, `001_init.sql` MUST explicitly `DISABLE ROW LEVEL SECURITY` on every table after `CREATE TABLE`. Don't rely on the dashboard's column or assume RLS is off.
2. Every readonly verification drill MUST include positive-read assertions on seeded tables (e.g., `SELECT count(*) FROM users` expected > 0). Negative-only tests miss the silent-filter case entirely.
3. Don't trust the Supabase dashboard's "RLS: Disabled" column at face value. Verify with `SELECT relrowsecurity FROM pg_class WHERE relnamespace = 'public'::regnamespace;` if there's any doubt.

**Captured as:** 14× `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` appended to `migrations/001_init.sql`; positive-read assertions added to `migrations/002_verify_readonly.sql`; this entry.
