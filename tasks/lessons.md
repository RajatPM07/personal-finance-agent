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
