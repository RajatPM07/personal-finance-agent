# W4.1 SQL Agent + Reviewer Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the natural-language → SQL Telegram interface for personal finance Q&A under a hard zero-Anthropic-spend constraint. Free-tier Groq Llama 3.3 70B generates SQL; static `sqlglot` validator gates non-SELECT or off-allowlist statements; the SQL runs on the W3.5 readonly Supabase connection; Gemini 2.5 Flash judges the result; uncertain or wrong verdicts escalate to a paid Sonnet judge or trigger a critique-driven retry; a 20-pair calibration harness gates ship.

**Architecture:** Synchronous orchestrator in `skills/finance/agents/sql_agent.py` invoked from an aiogram `/ask` handler via `asyncio.to_thread`. Pipeline: `llm("sql_agent") → sql_validator.validate_sql() → readonly_client().execute() → llm("sql_agent_judge") → optional llm("sql_agent_judge_strict") → optional retry with llm("sql_agent") again → optional last-resort llm("sql_agent_strict") → surface to user or render`. All thresholds in `config/sql_agent_review.yaml`. Role GRANTs on `finance_agent_readonly` (W3.5) are the durable security boundary; the static validator is defense-in-depth at the SQL layer.

**Tech Stack:** Python 3.11, sqlglot (BSD, postgres dialect) for SQL parsing & validation, LiteLLM via existing `lib/llm.py` wrapper extended with `response_format`, psycopg3 readonly connection via `lib/db.readonly_client()` (shipped W3.5), aiogram for the `/ask` command surface, pyyaml for tunable config, APScheduler for the daily Anthropic balance check. PRD §6.4 deviation is intentional (per `docs/superpowers/specs/2026-04-30-llm-routing-anthropic-zero-spend.md`).

---

## File Structure

**New files:**

| Path | Responsibility |
|---|---|
| `skills/finance/agents/__init__.py` | Empty package marker |
| `skills/finance/agents/sql_validator.py` | Pure function `validate_sql(sql, allowed_tables) -> ValidationResult`. sqlglot-based; rejects non-SELECT, multi-statement, off-allowlist tables. |
| `skills/finance/agents/judge.py` | `build_judge_prompt(question, sql, result_preview, schema_excerpt) -> str` + `parse_judge_response(raw) -> JudgeVerdict`. Pure functions. |
| `skills/finance/agents/sql_agent.py` | `run_sql_agent(question) -> AgentResult` orchestrator. Sync. Threads through validate → run → judge → escalate/retry. |
| `skills/finance/agents/review_config.py` | Loads `config/sql_agent_review.yaml`; validates required keys. |
| `config/sql_agent_review.yaml` | Tunable thresholds: `confidence_threshold`, `max_retry_rounds`, `anthropic_balance_warning_usd`. |
| `config/db_schema_for_judge.md` | Curated schema excerpt fed into the judge prompt. Static; updated only when schema changes. |
| `tests/sql_agent_calibration/__init__.py` | Empty package marker |
| `tests/sql_agent_calibration/pairs.example.yaml` | 3 committed sample pairs (no PII) demonstrating the schema. |
| `tests/sql_agent_calibration/run_calibration.py` | Loads `pairs.yaml`, runs each through `run_sql_agent`, scores recall/precision/escalation rate, plots confidence distribution. |
| `tests/test_sql_validator.py` | Unit tests for the validator. |
| `tests/test_judge.py` | Unit tests for prompt building + response parsing. |
| `tests/test_sql_agent.py` | Orchestrator tests with mocked LLM calls. |
| `tests/test_llm_response_format.py` | Test that `llm()`'s new `response_format` kwarg passes through to LiteLLM. |
| `tests/test_anthropic_balance_alert.py` | Test the balance-check + alert path with a mocked Anthropic API. |

**Modified files:**

| Path | Change |
|---|---|
| `config/model_routing.yaml` | Apply §3 diff from spec: flip `sql_agent` + `affordability_reasoning` primaries to Groq, demote Sonnet to fallback; add `sql_agent_judge`, `sql_agent_judge_strict`, `sql_agent_strict` entries. Strip the scheduled-change comment block (no longer scheduled — implemented). |
| `skills/finance/lib/llm.py` | Add optional `response_format: dict \| None = None` kwarg; pass through to `litellm.completion`. Keep the prompt-body-NOT-logged invariant. |
| `skills/finance/bot/main.py` | Add `/ask <question>` handler. `_is_rajat(message)` gate per CLAUDE.md invariant #7. Calls `run_sql_agent` via `asyncio.to_thread`. Renders four result classes: success / escalated / retried / surfaced_to_user. |
| `skills/finance/monitoring/alerts.py` | Add `check_anthropic_balance() -> None` async fn. Reads from Anthropic billing API; alerts on `< balance_warning_usd`. |
| `app.py` | Register `check_anthropic_balance` as a daily APScheduler job at 09:00 Asia/Kolkata. |
| `pyproject.toml` | Add `sqlglot>=22,<27` to dependencies. |
| `.gitignore` | Add `tests/sql_agent_calibration/pairs.yaml` (real questions = real PII). |
| `tasks/lessons.md` | Append the cost-vs-quality V1 trade entry per spec §D5. |
| `CLAUDE.md` | Add invariant: judge prompt content (schema_excerpt + result_preview) carries the same sensitivity as `credentials.yaml`; do NOT extend `llm()`'s callback to log prompt bodies. |
| `tasks/preconditions-notes.md` | Append W4.1 preconditions findings (Task 0). |

**Verified (no changes expected):**

| Path | Check |
|---|---|
| `skills/finance/lib/db.py` | `readonly_client()` from W3.5 is the SQL runner. |
| `skills/finance/lib/llm.py` | `llm()` already routes by task name via yaml lookup; new task entries will resolve automatically once added. |

---

## Task 0: Preconditions

**Files:**
- Modify: `tasks/preconditions-notes.md` (append)

Per CLAUDE.md invariant #11 — verify before pinning library APIs.

- [ ] **Step 0.1: Verify Anthropic Sonnet model ID is current**

Run:
```
.venv/bin/python -c "from litellm import get_model_info; print(get_model_info('anthropic/claude-sonnet-4-6'))"
```

Expected: dict containing `max_tokens`, `input_cost_per_token`, etc. If raises `BadRequestError: model not found`, run `.venv/bin/pip index versions litellm` and check anthropic provider mapping in `litellm.model_cost`. Record exact model ID + LiteLLM version in `tasks/preconditions-notes.md` under a new `## W4.1 — 2026-05-19` heading.

- [ ] **Step 0.2: Verify Groq Llama 3.3 70B reachability**

Run:
```
.venv/bin/python -c "
from skills.finance.lib.llm import llm
r = llm('sql_agent', 'Return the literal string OK and nothing else.')
print(r.choices[0].message.content[:50])
"
```

Expected: prints `OK` or similar — confirms the existing `sql_agent` route (still Sonnet primary today) can fall back to Groq if Sonnet fails. **NOTE:** the current routing has Sonnet PRIMARY — this call may cost ~$0.001. If Anthropic balance is depleted it falls to Groq. Either way the response should be `OK`. If error: triage in preconditions-notes.

- [ ] **Step 0.3: Verify Gemini 2.5 Flash structured-JSON support via LiteLLM**

Run:
```
.venv/bin/python -c "
import litellm
r = litellm.completion(
    model='gemini/gemini-2.5-flash',
    messages=[{'role':'user','content':'Return {\"verdict\":\"ok\",\"confidence\":0.9,\"reason\":\"test\"}'}],
    response_format={'type':'json_object'},
)
print(r.choices[0].message.content[:200])
"
```

Expected: a JSON string parseable by `json.loads`. If `response_format` is not honored, fall back to prompt-engineered JSON with a `json.loads(...)` retry path — record the decision in preconditions-notes. **This output decides whether Task 4 uses `response_format` or prompt-only JSON discipline.**

- [ ] **Step 0.4: Check sqlglot latest version + verify SELECT detection**

Run:
```
.venv/bin/pip index versions sqlglot 2>&1 | head -3 && echo "---" && .venv/bin/pip install 'sqlglot>=22,<27' 2>&1 | tail -3 && echo "---" && .venv/bin/python -c "
import sqlglot
from sqlglot import exp
sql_ok = 'SELECT count(*) FROM transactions'
sql_bad = 'INSERT INTO transactions (date) VALUES (CURRENT_DATE)'
sql_multi = 'SELECT 1; DELETE FROM users'
for label, sql in [('ok',sql_ok),('bad',sql_bad),('multi',sql_multi)]:
    stmts = sqlglot.parse(sql, dialect='postgres')
    keys = [s.key for s in stmts if s is not None]
    print(f'{label}: n={len(stmts)} keys={keys}')
"
```

Expected:
```
ok: n=1 keys=['select']
bad: n=1 keys=['insert']
multi: n=2 keys=['select', 'delete']
```

Record observed output in preconditions-notes. If sqlglot's API differs (e.g. `.key` not present), document the actual attribute used for statement-type detection.

- [ ] **Step 0.5: Verify Anthropic balance API**

Run:
```
.venv/bin/python -c "
import os, httpx
from skills.finance.lib.settings import settings
# Anthropic exposes /v1/organizations/{org_id}/usage or similar; the exact
# endpoint changes — this step finds the right path, doesn't lock it.
r = httpx.get(
    'https://api.anthropic.com/v1/organizations/me/usage',
    headers={'x-api-key': settings.anthropic_api_key, 'anthropic-version':'2023-06-01'},
    timeout=5,
)
print(r.status_code, r.text[:300])
"
```

Expected: 200 OK with a usage/balance payload OR 404/405 with a clear path hint. If endpoint is wrong, search the actual API docs via the response error or fall back to **deriving balance from `request_logs` total cost subtracted from a known starting credit** ($5 per memory). Record the chosen approach in preconditions-notes. **This decides whether Task 12's `check_anthropic_balance` polls the API or computes from local logs.**

- [ ] **Step 0.6: Append findings to preconditions-notes**

Open `tasks/preconditions-notes.md`. Append a new `## W4.1 readonly client — 2026-05-19` section (mirroring the W3.5 entry) with: exact Sonnet model ID + LiteLLM version, Gemini JSON-mode behavior, sqlglot output for the three test SQLs, Anthropic balance API decision. **No commit yet — preconditions-notes is gitignored.**

---

## Task 1: SQL static validator (sqlglot)

**Files:**
- Create: `skills/finance/agents/__init__.py`
- Create: `skills/finance/agents/sql_validator.py`
- Create: `tests/test_sql_validator.py`
- Modify: `pyproject.toml` (add sqlglot)

- [ ] **Step 1.1: Create empty package**

Write `skills/finance/agents/__init__.py`:
```python
"""W4.1 SQL agent + reviewer-layer package. See
docs/superpowers/specs/2026-04-30-llm-routing-anthropic-zero-spend.md
for the architecture."""
```

- [ ] **Step 1.2: Pin sqlglot in pyproject.toml**

Edit `pyproject.toml` — under `dependencies`, after the `psycopg[binary]` line, add:
```toml
    "sqlglot>=22,<27",  # W4.1 SQL static validator — parse postgres dialect, classify statement type, enforce table allowlist
```

Then:
```
.venv/bin/pip install -e ".[dev]" 2>&1 | tail -3
```

Expected: `Successfully installed ... sqlglot-XX.X.X`.

- [ ] **Step 1.3: Write failing tests for validator**

Write `tests/test_sql_validator.py`:
```python
"""W4.1 SQL static validator — rejects anything that isn't a single SELECT
against the allowlisted tables. Defense-in-depth on top of the role GRANTs
enforced by the readonly Postgres user (W3.5)."""
from __future__ import annotations

import pytest

from skills.finance.agents.sql_validator import ValidationResult, validate_sql

ALLOWED = {
    "transactions", "accounts", "categories", "assets", "liabilities",
    "users", "ingestion_log", "commitments", "income_events",
}


def test_simple_select_allowed():
    r = validate_sql("SELECT count(*) FROM transactions", ALLOWED)
    assert r.ok is True
    assert r.statement_type == "select"


def test_select_with_join_allowed():
    r = validate_sql(
        "SELECT t.amount, c.name FROM transactions t "
        "JOIN categories c ON t.category_id = c.id",
        ALLOWED,
    )
    assert r.ok is True


def test_insert_rejected():
    r = validate_sql(
        "INSERT INTO transactions (user_id, date, amount, direction) "
        "VALUES (gen_random_uuid(), CURRENT_DATE, 1, 'out')",
        ALLOWED,
    )
    assert r.ok is False
    assert "select" in (r.reason or "").lower() or "insert" in (r.reason or "").lower()


@pytest.mark.parametrize("sql", [
    "UPDATE transactions SET amount = 0",
    "DELETE FROM transactions WHERE id IS NOT NULL",
    "DROP TABLE transactions",
    "ALTER TABLE transactions ADD COLUMN x int",
    "TRUNCATE transactions",
    "CREATE TABLE x (id int)",
    "GRANT SELECT ON transactions TO public",
])
def test_non_select_rejected(sql):
    r = validate_sql(sql, ALLOWED)
    assert r.ok is False


def test_multi_statement_rejected():
    r = validate_sql("SELECT 1; DELETE FROM users", ALLOWED)
    assert r.ok is False
    assert "single" in (r.reason or "").lower() or "multi" in (r.reason or "").lower()


def test_off_allowlist_table_rejected():
    r = validate_sql("SELECT * FROM pg_catalog.pg_user", ALLOWED)
    assert r.ok is False
    assert "allow" in (r.reason or "").lower() or "pg_user" in (r.reason or "").lower()


def test_unrecognised_table_rejected():
    r = validate_sql("SELECT * FROM secret_table", ALLOWED)
    assert r.ok is False


def test_malformed_sql_rejected():
    r = validate_sql("SELEKT * FROM transactions", ALLOWED)
    assert r.ok is False
    assert "parse" in (r.reason or "").lower() or "syntax" in (r.reason or "").lower()


def test_empty_string_rejected():
    r = validate_sql("", ALLOWED)
    assert r.ok is False


def test_whitespace_only_rejected():
    r = validate_sql("   \n  ", ALLOWED)
    assert r.ok is False


def test_validation_result_is_dataclass():
    r = validate_sql("SELECT 1 FROM transactions", ALLOWED)
    assert isinstance(r, ValidationResult)
    assert isinstance(r.ok, bool)
    assert r.reason is None or isinstance(r.reason, str)
```

- [ ] **Step 1.4: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_sql_validator.py -v 2>&1 | tail -10
```

Expected: ImportError / ModuleNotFoundError on `skills.finance.agents.sql_validator`.

- [ ] **Step 1.5: Implement validator**

Write `skills/finance/agents/sql_validator.py`:
```python
"""Static SQL validator for the W4.1 SQL agent.

Defense-in-depth on top of the readonly role's GRANT enforcement (W3.5):
- single statement only
- top-level statement key must be `select` (sqlglot's classification)
- every referenced table must be in the caller-provided allowlist

This catches a generated SQL going off the rails (e.g. Groq hallucinating a
DELETE) before it touches the wire — both as a UX win (fail fast with a clear
reason) and as a redundancy. Role GRANTs are the durable boundary.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str | None = None
    statement_type: str | None = None


def validate_sql(sql: str, allowed_tables: set[str]) -> ValidationResult:
    """Validate a single SELECT against the allowed-tables set.

    Returns ValidationResult(ok=True, statement_type='select') on success,
    or ValidationResult(ok=False, reason=<human-readable why>) on rejection.
    """
    if not sql or not sql.strip():
        return ValidationResult(ok=False, reason="empty SQL")

    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except ParseError as e:
        return ValidationResult(ok=False, reason=f"parse error: {e}")

    non_null = [s for s in statements if s is not None]
    if len(non_null) == 0:
        return ValidationResult(ok=False, reason="no parseable statement")
    if len(non_null) > 1:
        return ValidationResult(
            ok=False,
            reason="only a single statement is allowed (multi-statement input rejected)",
        )

    stmt = non_null[0]
    if stmt.key != "select":
        return ValidationResult(
            ok=False,
            reason=f"only SELECT is allowed; got {stmt.key.upper()}",
            statement_type=stmt.key,
        )

    for table in stmt.find_all(exp.Table):
        # tables in postgres can be schema-qualified; reject anything not in our flat allowlist
        name = table.name
        if name not in allowed_tables:
            qualified = (
                f"{table.db}.{name}" if table.db else name
            )
            return ValidationResult(
                ok=False,
                reason=f"table {qualified!r} is not on the allowlist {sorted(allowed_tables)}",
                statement_type="select",
            )

    return ValidationResult(ok=True, statement_type="select")
```

- [ ] **Step 1.6: Run tests to verify they pass**

Run:
```
.venv/bin/python -m pytest tests/test_sql_validator.py -v 2>&1 | tail -25
```

Expected: 11 passed.

- [ ] **Step 1.7: Lint + typecheck**

Run:
```
.venv/bin/ruff check skills/finance/agents tests/test_sql_validator.py && .venv/bin/mypy skills/finance/agents
```

Expected: `All checks passed!` for ruff; `Success: no issues found` for mypy.

- [ ] **Step 1.8: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add skills/finance/agents/__init__.py skills/finance/agents/sql_validator.py tests/test_sql_validator.py pyproject.toml && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(agents): static SQL validator via sqlglot (W4.1 §4.1)

Defense-in-depth atop the W3.5 readonly role GRANTs: single SELECT
only, top-level statement must classify as 'select', all referenced
tables must be on a caller-provided allowlist. 11 tests cover the
positive case, every non-SELECT class, multi-statement, off-allowlist,
and malformed input.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Yaml routing flip + new judge entries

**Files:**
- Modify: `config/model_routing.yaml`
- Create: `tests/test_model_routing_w4.py`

- [ ] **Step 2.1: Apply spec §3 diff to model_routing.yaml**

Open `config/model_routing.yaml`. Replace the scheduled-change comment block (lines starting `# 2026-04-30 SCHEDULED CHANGE`) with a one-line confirmation that the flip landed. Then update the `sql_agent` and `affordability_reasoning` blocks and append the three new entries.

Replace lines 20–54 (the entire scheduled-change comment block) with:

```
# ───────────────────────────────────────────────────────────────────────────
# 2026-05-19 — Routing flip LANDED with W4.1 (see git log for plan + commits).
# Per docs/superpowers/specs/2026-04-30-llm-routing-anthropic-zero-spend.md.
# Anthropic remains in fallback / escalation slot only.
# ───────────────────────────────────────────────────────────────────────────
```

Replace the `sql_agent:` block (lines 71-74):
```yaml
sql_agent:
  model: groq/llama-3.3-70b-versatile           # was: anthropic/claude-sonnet-4-6 — flipped 2026-05-19 under zero-spend constraint
  fallbacks: [anthropic/claude-sonnet-4-6]      # was: [groq/...]
  stakes: high
  # Reviewer layer (sql_agent_judge + sql_agent_judge_strict + sql_agent_strict)
  # catches the quality cases where Llama's degraded multi-CTE handling matters.
  # Sonnet fallback fires only on Groq unavailability — the reviewer-layer
  # judge escalation is the cost-observable spend path, separate from this.
```

Replace the `affordability_reasoning:` block (lines 76-79):
```yaml
affordability_reasoning:
  model: groq/llama-3.3-70b-versatile           # was: anthropic/claude-sonnet-4-6 — same 2026-05-19 flip
  fallbacks: [anthropic/claude-sonnet-4-6]
  stakes: high
  # NO reviewer layer in V1 — gated by PRD §6.5 data-quality guard until ≥60d
  # of clean ingested history (~late June 2026). Revisit reviewer design when
  # sql_agent operational data exists. See spec §8.
```

Append at the bottom of the file (after `embeddings:`):
```yaml

# W4.1 reviewer-layer entries (added 2026-05-19):

sql_agent_judge:
  model: gemini/gemini-2.5-flash
  fallbacks: [anthropic/claude-sonnet-4-6]
  stakes: medium
  # Primary judge in the reviewer layer. Returns JSON {verdict, confidence, reason}.

sql_agent_judge_strict:
  model: anthropic/claude-sonnet-4-6
  fallbacks: []                                 # if Sonnet fails here, reviewer surfaces "uncertain" to user
  stakes: high
  # Escalation judge — called explicitly when Gemini returns uncertain / low-confidence.

sql_agent_strict:
  model: anthropic/claude-sonnet-4-6
  fallbacks: []                                 # last resort — surfaces "rephrase?" to user on failure
  stakes: high
  # Last-resort SQL generation, called only after Groq retry-with-critique fails the judge.
  # Distinct from sql_agent.fallbacks so cost is observable + intentional, not implicit.
```

- [ ] **Step 2.2: Write failing test for new routing entries**

Write `tests/test_model_routing_w4.py`:
```python
"""W4.1 added three new task entries to config/model_routing.yaml + flipped
two existing ones. Lock the shape with explicit tests so a yaml typo can't
break the SQL agent silently."""
from __future__ import annotations

from skills.finance.lib.llm import ROUTING


def test_sql_agent_primary_is_groq():
    assert ROUTING["sql_agent"]["model"] == "groq/llama-3.3-70b-versatile"


def test_sql_agent_fallback_is_anthropic_sonnet():
    assert ROUTING["sql_agent"]["fallbacks"] == ["anthropic/claude-sonnet-4-6"]


def test_affordability_primary_is_groq():
    assert ROUTING["affordability_reasoning"]["model"] == "groq/llama-3.3-70b-versatile"


def test_sql_agent_judge_uses_gemini_with_sonnet_escalation():
    cfg = ROUTING["sql_agent_judge"]
    assert cfg["model"] == "gemini/gemini-2.5-flash"
    assert cfg["fallbacks"] == ["anthropic/claude-sonnet-4-6"]


def test_sql_agent_judge_strict_uses_sonnet_no_fallback():
    cfg = ROUTING["sql_agent_judge_strict"]
    assert cfg["model"] == "anthropic/claude-sonnet-4-6"
    assert cfg["fallbacks"] == []


def test_sql_agent_strict_uses_sonnet_no_fallback():
    cfg = ROUTING["sql_agent_strict"]
    assert cfg["model"] == "anthropic/claude-sonnet-4-6"
    assert cfg["fallbacks"] == []
```

- [ ] **Step 2.3: Run tests**

Run:
```
.venv/bin/python -m pytest tests/test_model_routing_w4.py -v 2>&1 | tail -15
```

Expected: 6 passed.

- [ ] **Step 2.4: Lint**

Run:
```
.venv/bin/ruff check tests/test_model_routing_w4.py
```

Expected: `All checks passed!`.

- [ ] **Step 2.5: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add config/model_routing.yaml tests/test_model_routing_w4.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(routing): W4.1 yaml flip + reviewer-layer task entries

Flip sql_agent + affordability_reasoning primaries from
anthropic/claude-sonnet-4-6 → groq/llama-3.3-70b-versatile; demote
Sonnet to fallback. Add sql_agent_judge (Gemini, primary judge),
sql_agent_judge_strict (Sonnet, escalation), sql_agent_strict
(Sonnet, last-resort generation). 6 tests lock the shape so a yaml
typo can't silently break the SQL agent. Per spec
2026-04-30-llm-routing-anthropic-zero-spend.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Tunable threshold config + loader

**Files:**
- Create: `config/sql_agent_review.yaml`
- Create: `skills/finance/agents/review_config.py`
- Create: `tests/test_review_config.py`

- [ ] **Step 3.1: Create the config file**

Write `config/sql_agent_review.yaml`:
```yaml
# W4.1 SQL-agent reviewer layer tunables.
# Per docs/superpowers/specs/2026-04-30-llm-routing-anthropic-zero-spend.md §4.4.
# These live in yaml (not code) so threshold tuning during calibration does NOT
# require a commit; rerun calibration after editing.

# Gate on `verdict == "ok"`. When confidence < this, treat as "uncertain"
# and escalate to the strict judge. Spec §5.0 distribution plot MAY force
# a different value — if Gemini's confidence clusters narrowly, drop to 0.0.
confidence_threshold: 0.85

# Maximum retry rounds when the judge returns verdict=wrong. Beyond this,
# the question itself is the problem — surface "rephrase?" to the user.
max_retry_rounds: 2

# Telegram alert fires when Anthropic balance drops below this USD figure.
# At expected burn (~$0.001/query), the $5 starting credit lasts 1–2 years;
# $3 trigger gives months of lead time before the routing flip becomes urgent.
anthropic_balance_warning_usd: 3.0
```

- [ ] **Step 3.2: Write failing test for loader**

Write `tests/test_review_config.py`:
```python
"""W4.1 sql_agent reviewer-layer threshold loader. Yaml-driven so tuning
during calibration does not require a code commit."""
from __future__ import annotations

import textwrap

import pytest

from skills.finance.agents.review_config import ReviewConfig, load_review_config


def test_load_defaults_from_committed_yaml():
    cfg = load_review_config()
    assert isinstance(cfg, ReviewConfig)
    assert cfg.confidence_threshold == 0.85
    assert cfg.max_retry_rounds == 2
    assert cfg.anthropic_balance_warning_usd == 3.0


def test_load_from_custom_path(tmp_path):
    p = tmp_path / "custom.yaml"
    p.write_text(textwrap.dedent("""
        confidence_threshold: 0.5
        max_retry_rounds: 1
        anthropic_balance_warning_usd: 1.0
    """))
    cfg = load_review_config(p)
    assert cfg.confidence_threshold == 0.5
    assert cfg.max_retry_rounds == 1


def test_unknown_key_raises(tmp_path):
    """Extra keys = silent config drift. Refuse rather than ignore."""
    p = tmp_path / "bad.yaml"
    p.write_text(textwrap.dedent("""
        confidence_threshold: 0.85
        max_retry_rounds: 2
        anthropic_balance_warning_usd: 3.0
        mystery_setting: true
    """))
    with pytest.raises(ValueError, match="unknown.*mystery_setting"):
        load_review_config(p)


def test_missing_required_key_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("confidence_threshold: 0.85\n")
    with pytest.raises(ValueError, match="missing.*max_retry_rounds"):
        load_review_config(p)


def test_confidence_threshold_out_of_range_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(textwrap.dedent("""
        confidence_threshold: 1.5
        max_retry_rounds: 2
        anthropic_balance_warning_usd: 3.0
    """))
    with pytest.raises(ValueError, match="confidence_threshold"):
        load_review_config(p)
```

- [ ] **Step 3.3: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_review_config.py -v 2>&1 | tail -10
```

Expected: ImportError on `skills.finance.agents.review_config`.

- [ ] **Step 3.4: Implement loader**

Write `skills/finance/agents/review_config.py`:
```python
"""Tunable threshold loader for the W4.1 reviewer layer.

Yaml lives at config/sql_agent_review.yaml so calibration-driven
threshold updates do not require a code commit. Unknown keys raise
to catch silent config drift; missing required keys raise so partial
edits during tuning don't fall back to incoherent defaults.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "sql_agent_review.yaml"

_REQUIRED_KEYS = {"confidence_threshold", "max_retry_rounds", "anthropic_balance_warning_usd"}


@dataclass(frozen=True)
class ReviewConfig:
    confidence_threshold: float
    max_retry_rounds: int
    anthropic_balance_warning_usd: float


def load_review_config(path: Path | None = None) -> ReviewConfig:
    p = path if path is not None else _DEFAULT_PATH
    with open(p) as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{p}: expected a mapping, got {type(data).__name__}")

    missing = _REQUIRED_KEYS - data.keys()
    if missing:
        raise ValueError(f"{p}: missing required keys: {sorted(missing)}")

    extra = data.keys() - _REQUIRED_KEYS
    if extra:
        raise ValueError(f"{p}: unknown keys (refuse silent drift): {sorted(extra)}")

    ct = float(data["confidence_threshold"])
    if not 0.0 <= ct <= 1.0:
        raise ValueError(f"confidence_threshold must be in [0.0, 1.0]; got {ct}")

    mr = int(data["max_retry_rounds"])
    if mr < 0:
        raise ValueError(f"max_retry_rounds must be >= 0; got {mr}")

    bw = float(data["anthropic_balance_warning_usd"])
    if bw < 0:
        raise ValueError(f"anthropic_balance_warning_usd must be >= 0; got {bw}")

    return ReviewConfig(
        confidence_threshold=ct,
        max_retry_rounds=mr,
        anthropic_balance_warning_usd=bw,
    )
```

- [ ] **Step 3.5: Run tests to verify they pass**

Run:
```
.venv/bin/python -m pytest tests/test_review_config.py -v 2>&1 | tail -10
```

Expected: 5 passed.

- [ ] **Step 3.6: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add config/sql_agent_review.yaml skills/finance/agents/review_config.py tests/test_review_config.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(agents): tunable reviewer-layer config loader (W4.1 §4.4)

Thresholds in yaml so calibration-driven tuning does not require a
commit. Loader refuses unknown or missing keys (silent drift is the
failure mode this catches). 5 tests cover defaults, custom path,
unknown key, missing key, out-of-range value.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Extend `llm()` with `response_format` passthrough

**Files:**
- Modify: `skills/finance/lib/llm.py`
- Create: `tests/test_llm_response_format.py`

Rationale: The judge prompts must produce JSON. Either via `response_format={"type":"json_object"}` (if Task 0.3 confirmed LiteLLM honors it for Gemini) or via prompt-engineered JSON + robust parsing. Either way `llm()` needs to forward `response_format` to LiteLLM so the judge can request structured output. Path chosen via Task 0.3 output.

- [ ] **Step 4.1: Write failing test for response_format passthrough**

Write `tests/test_llm_response_format.py`:
```python
"""W4.1: judge prompts request JSON output via `response_format`. `llm()`
must forward the kwarg through to LiteLLM unchanged, while preserving the
existing routing + fallback behavior."""
from __future__ import annotations

from unittest.mock import patch


def test_response_format_forwarded_to_litellm():
    from skills.finance.lib import llm as llm_mod

    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)

        class FakeChoice:
            class message:
                content = '{"ok": true}'
        class FakeResp:
            choices = [FakeChoice()]
        return FakeResp()

    with patch.object(llm_mod.litellm, "completion", side_effect=fake_completion):
        llm_mod.llm(
            "sql_agent_judge",
            "test prompt",
            response_format={"type": "json_object"},
        )

    assert captured["response_format"] == {"type": "json_object"}
    assert captured["model"] == "gemini/gemini-2.5-flash"


def test_response_format_omitted_when_not_passed():
    from skills.finance.lib import llm as llm_mod

    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        class FakeResp:
            choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]
        return FakeResp()

    with patch.object(llm_mod.litellm, "completion", side_effect=fake_completion):
        llm_mod.llm("sql_agent", "test prompt")

    # response_format key should not be passed when caller doesn't supply one
    assert "response_format" not in captured
```

- [ ] **Step 4.2: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_llm_response_format.py -v 2>&1 | tail -10
```

Expected: TypeError on unexpected `response_format` kwarg (or AssertionError if response_format isn't propagated).

- [ ] **Step 4.3: Extend `llm()` signature**

Edit `skills/finance/lib/llm.py`. Replace the `llm()` function (lines 30-44) with:
```python
def llm(
    task: str,
    prompt: str,
    system: str | None = None,
    response_format: dict | None = None,
):
    """Single entry point for all LLM calls. Routes by task name via model_routing.yaml.

    `response_format`: optional structured-output spec passed through to LiteLLM.
    Used by W4.1's reviewer-layer judges to request JSON output. Provider support
    varies (Gemini honors via response_mime_type; Anthropic via tools; Groq via
    prompt-only). LiteLLM normalises across providers.
    """
    if task not in ROUTING:
        raise KeyError(f"Unknown task '{task}'. Known: {list(ROUTING.keys())}")
    cfg = ROUTING[task]
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = {
        "model": cfg["model"],
        "messages": messages,
        "fallbacks": cfg.get("fallbacks", []),
        "metadata": {"task": task},
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    return litellm.completion(**kwargs)
```

- [ ] **Step 4.4: Run tests to verify they pass**

Run:
```
.venv/bin/python -m pytest tests/test_llm_response_format.py tests/test_llm.py -v 2>&1 | tail -15
```

Expected: 2 + existing llm tests pass.

- [ ] **Step 4.5: Lint + typecheck**

Run:
```
.venv/bin/ruff check skills/finance/lib/llm.py tests/test_llm_response_format.py && .venv/bin/mypy skills/finance/lib/llm.py
```

Expected: clean.

- [ ] **Step 4.6: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add skills/finance/lib/llm.py tests/test_llm_response_format.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(llm): optional response_format passthrough for structured JSON

W4.1 judge prompts request JSON output. llm() now forwards the
response_format kwarg to LiteLLM unchanged. Backward-compatible —
absent kwarg behaves exactly as before (2 tests lock this).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Curated schema excerpt for judge

**Files:**
- Create: `config/db_schema_for_judge.md`

Static document fed into the judge prompt. Updated only when schema changes (not on every commit). Lists only the tables that are on the validator allowlist + a brief description of each column.

- [ ] **Step 5.1: Write the schema excerpt**

Write `config/db_schema_for_judge.md`:
```markdown
# Database schema (relevant tables for SQL judge)

Source of truth: `migrations/001_init.sql` + `005_category_hint.sql` + `006_static_assets.sql` + `007_txn_mode.sql`.
This excerpt is the judge's view of the world — keep it accurate but minimal.

## transactions
- `id uuid` — primary key
- `user_id uuid` — FK users
- `date date` — transaction date (Asia/Kolkata)
- `txn_time timestamptz` — exact timestamp when source provides it; NULL for most PDF rows
- `amount numeric(12,2)` — always positive; direction column carries the sign
- `currency text` — defaults to 'INR'
- `direction text` — 'in' or 'out'
- `is_refund boolean` — defaults to false
- `linked_txn_id uuid` — FK transactions (refund links to original)
- `raw_merchant text` — text as it appears on the source
- `normalized_merchant text` — populated post-W5; NULL until then
- `category_id uuid` — FK categories
- `subcategory text`
- `source text` — channel ('manual_pdf', 'manual_xlsx', etc.)
- `source_ref text` — filename / email-id / sms-id; AUDIT only
- `parser_version text` — e.g. 'icici-savings-pdf/v2'; part of import_hash
- `category_hint text` — Paytm-only today; NULL elsewhere
- `txn_mode text` — ICICI-Savings-only today; NULL elsewhere
- `account_id uuid` — FK accounts
- `is_deleted boolean` — soft delete; INCLUDE this in WHERE when filtering "active"
- `ingested_at timestamptz`

## accounts
- `id uuid`
- `user_id uuid` — FK users
- `type text` — 'savings' / 'cc' / 'upi' / 'mf' / 'stock' etc.
- `institution text` — bank/provider name
- `nickname text` — human-readable label
- `identifier text` — last-4 / handle / 'static' for assets-only rows
- `is_active boolean`

## categories
- `id uuid`
- `user_id uuid`
- `name text` — e.g. 'Food', 'Transport'
- `parent_id uuid` — FK categories (for sub-categories)

## assets
- `id uuid`
- `user_id uuid`
- `account_id uuid` — FK accounts
- `type text` — 'mf' / 'stock' / 'cash' etc.
- `current_value numeric(14,2)` — updated manually
- `identifier text` — 'static' for V1 manually-maintained rows

## liabilities
- `id uuid`
- `user_id uuid`
- `principal numeric(14,2)`
- `rate_pct numeric(5,2)`
- `name text`

## ingestion_log
- `id uuid`
- `source text` — 'manual_pdf' / 'manual_xlsx'
- `source_ref text` — filename
- `status text` — 'success' / 'skipped_duplicate' / 'total_check_failed' / etc.
- `rows_added int`
- `declared_total numeric(14,2)`
- `extracted_total numeric(14,2)`
- `timestamp timestamptz`

## Notes for the judge

- **Date column is `date`, not `txn_date`.** Past parsers used `txn_date` internally before the insert dict mapped to `date`.
- **`amount` is unsigned**; use `direction = 'out'` for spend, `direction = 'in'` for income.
- **Soft deletes:** unless the question explicitly asks for "deleted" rows, filter by `is_deleted = false` or omit the filter (default behavior depends on the question intent — flag with `verdict=uncertain` if ambiguous).
- **Asia/Kolkata timezone:** `date` is calendar-date in Asia/Kolkata. `txn_time` is timestamptz; cast to date in Asia/Kolkata before grouping if mixing.
- **The single user is Rajat**; `user_id` filtering is a no-op in V1 (single-user), but well-written SQL still includes the filter.
```

- [ ] **Step 5.2: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add config/db_schema_for_judge.md && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "docs(config): curated schema excerpt for W4.1 judge prompts

The judge prompt template includes a schema excerpt so the judge can
reason about whether the SQL hit the right tables/columns. This file
is the canonical source — update only when schema changes (treat as
high-leverage, low-frequency).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Judge module — prompt builder + response parser

**Files:**
- Create: `skills/finance/agents/judge.py`
- Create: `tests/test_judge.py`

- [ ] **Step 6.1: Write failing tests for judge module**

Write `tests/test_judge.py`:
```python
"""W4.1 judge module — pure functions for prompt building and response parsing.
LLM call itself is in sql_agent.py; this layer is purely synchronous + testable
without network."""
from __future__ import annotations

import json

import pytest

from skills.finance.agents.judge import (
    JudgeVerdict,
    build_judge_prompt,
    parse_judge_response,
)


def test_build_prompt_includes_all_inputs():
    p = build_judge_prompt(
        question="How much did I spend on food last month?",
        sql="SELECT SUM(amount) FROM transactions WHERE category_id = '...'",
        result_preview=[{"sum": 5432.10}],
        schema_excerpt="(stub schema)",
    )
    assert "food last month" in p
    assert "SELECT SUM(amount)" in p
    assert "5432.10" in p or "5432.1" in p
    assert "stub schema" in p
    assert "JSON" in p or "json" in p  # output instruction mentions JSON


def test_build_prompt_truncates_long_result_preview():
    """50 rows in result_preview shouldn't blow up the prompt. Cap at first 3
    per spec §4.1 (the architecture diagram explicitly says 'First N=3')."""
    rows = [{"x": i} for i in range(50)]
    p = build_judge_prompt(
        question="?",
        sql="SELECT 1",
        result_preview=rows,
        schema_excerpt="",
    )
    assert '"x": 0' in p
    assert '"x": 2' in p
    assert '"x": 3' not in p  # row 4 (index 3) NOT in the prompt
    assert '"x": 49' not in p


def test_parse_response_ok():
    raw = json.dumps({"verdict": "ok", "confidence": 0.9, "reason": "matches"})
    v = parse_judge_response(raw)
    assert v.verdict == "ok"
    assert v.confidence == 0.9
    assert v.reason == "matches"


def test_parse_response_wrong():
    raw = json.dumps({"verdict": "wrong", "confidence": 0.7, "reason": "uses wrong filter"})
    v = parse_judge_response(raw)
    assert v.verdict == "wrong"


def test_parse_response_uncertain():
    raw = json.dumps({"verdict": "uncertain", "confidence": 0.4, "reason": "can't tell"})
    v = parse_judge_response(raw)
    assert v.verdict == "uncertain"


def test_parse_handles_markdown_codefence():
    """Some providers wrap JSON in ```json ... ``` even when asked for raw JSON."""
    raw = '```json\n{"verdict":"ok","confidence":0.9,"reason":"ok"}\n```'
    v = parse_judge_response(raw)
    assert v.verdict == "ok"


def test_parse_invalid_verdict_raises():
    raw = json.dumps({"verdict": "maybe", "confidence": 0.5, "reason": "?"})
    with pytest.raises(ValueError, match="verdict"):
        parse_judge_response(raw)


def test_parse_confidence_out_of_range_raises():
    raw = json.dumps({"verdict": "ok", "confidence": 1.5, "reason": "?"})
    with pytest.raises(ValueError, match="confidence"):
        parse_judge_response(raw)


def test_parse_missing_field_raises():
    raw = json.dumps({"verdict": "ok"})
    with pytest.raises(ValueError):
        parse_judge_response(raw)


def test_parse_malformed_json_raises():
    with pytest.raises(ValueError, match="JSON"):
        parse_judge_response("not json at all")


def test_judge_verdict_is_dataclass():
    raw = json.dumps({"verdict": "ok", "confidence": 0.9, "reason": "x"})
    v = parse_judge_response(raw)
    assert isinstance(v, JudgeVerdict)
```

- [ ] **Step 6.2: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_judge.py -v 2>&1 | tail -10
```

Expected: ImportError on `skills.finance.agents.judge`.

- [ ] **Step 6.3: Implement judge module**

Write `skills/finance/agents/judge.py`:
```python
"""Judge prompt builder + response parser.

Pure functions only — the LLM call itself lives in sql_agent.py. Splitting
the prompt construction and parsing out keeps them unit-testable without
network, and lets the calibration harness reuse the exact same prompt that
ships in production.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

Verdict = Literal["ok", "wrong", "uncertain"]


@dataclass(frozen=True)
class JudgeVerdict:
    verdict: Verdict
    confidence: float
    reason: str


_PROMPT_TEMPLATE = """\
You are a SQL reviewer. Given a natural language question, the SQL that was
generated to answer it, and the first 3 rows of the result, decide whether
the SQL faithfully answers the question. Be strict.

- If the SQL touches the wrong tables, applies the wrong filter, returns the
  wrong aggregation shape, or omits a required clause, mark it WRONG.
- If you cannot tell from the question and the result rows whether the SQL is
  right, mark it UNCERTAIN.
- Otherwise mark it OK.

Schema (relevant tables):
{schema_excerpt}

Question: "{question}"

Generated SQL:
```sql
{sql}
```

First 3 result rows (JSON):
{result_preview_json}

Respond with a single JSON object and nothing else:
{{"verdict": "ok" | "wrong" | "uncertain", "confidence": 0.0-1.0, "reason": "<one sentence>"}}
"""


def build_judge_prompt(
    question: str,
    sql: str,
    result_preview: list[dict[str, Any]],
    schema_excerpt: str,
) -> str:
    """Render the judge prompt. Result preview capped at first 3 rows per
    spec §4.1 ('First N=3'). Excess rows are silently dropped — the cap is
    intentional; the judge doesn't need full data to assess correctness."""
    capped = result_preview[:3]
    return _PROMPT_TEMPLATE.format(
        question=question,
        sql=sql,
        result_preview_json=json.dumps(capped, default=str, indent=2),
        schema_excerpt=schema_excerpt,
    )


_CODEFENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_judge_response(raw: str) -> JudgeVerdict:
    """Parse the judge's JSON response. Tolerates markdown codefence wrappers
    (some providers add ```json ... ``` even when asked for raw JSON)."""
    stripped = raw.strip()
    m = _CODEFENCE_RE.match(stripped)
    if m:
        stripped = m.group(1).strip()

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError(f"judge response is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"judge response must be a JSON object, got {type(data).__name__}")

    for required in ("verdict", "confidence", "reason"):
        if required not in data:
            raise ValueError(f"judge response missing required field: {required!r}")

    verdict = data["verdict"]
    if verdict not in ("ok", "wrong", "uncertain"):
        raise ValueError(f"verdict must be one of ok/wrong/uncertain; got {verdict!r}")

    try:
        confidence = float(data["confidence"])
    except (TypeError, ValueError) as e:
        raise ValueError(f"confidence must be a number; got {data['confidence']!r}") from e
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be in [0.0, 1.0]; got {confidence}")

    reason = str(data["reason"])

    return JudgeVerdict(verdict=verdict, confidence=confidence, reason=reason)
```

- [ ] **Step 6.4: Run tests to verify they pass**

Run:
```
.venv/bin/python -m pytest tests/test_judge.py -v 2>&1 | tail -20
```

Expected: 11 passed.

- [ ] **Step 6.5: Lint + typecheck**

Run:
```
.venv/bin/ruff check skills/finance/agents/judge.py tests/test_judge.py && .venv/bin/mypy skills/finance/agents
```

Expected: clean.

- [ ] **Step 6.6: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add skills/finance/agents/judge.py tests/test_judge.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(agents): judge prompt builder + response parser (W4.1 §4.5)

Pure functions: build_judge_prompt() renders the canonical template
with question + SQL + result preview (capped at first 3 rows per spec)
+ schema excerpt; parse_judge_response() handles JSON with codefence
tolerance and validates verdict/confidence ranges. 11 tests cover
positive cases, every verdict, codefence wrapping, malformed inputs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: SQL agent orchestrator — happy path

**Files:**
- Create: `skills/finance/agents/sql_agent.py` (initial happy path only)
- Create: `tests/test_sql_agent.py`

Happy path = Groq generates → validator passes → readonly run → Gemini judge returns verdict=ok above threshold → render. Escalation and retry land in Tasks 8 and 9 respectively.

- [ ] **Step 7.1: Write failing test for happy-path orchestration**

Write `tests/test_sql_agent.py`:
```python
"""W4.1 SQL agent orchestrator tests. LLM calls + DB calls are mocked;
this layer is logic-only. Live integration tests live in the calibration
harness, not the unit suite."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from skills.finance.agents.sql_agent import AgentResult, run_sql_agent


def _fake_llm_response(content: str):
    """Mimics a litellm completion response object."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _fake_readonly_conn(rows: list[dict] | None = None):
    """Mimics a psycopg connection.execute() returning a cursor."""
    rows = rows or []
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = [tuple(r.values()) for r in rows]
    cur.description = [(k,) for k in (rows[0].keys() if rows else [])]
    conn.cursor.return_value = cur
    return conn


def test_happy_path_renders_first_attempt():
    """Groq generates valid SELECT → validator passes → DB returns rows →
    Gemini judge ok + high confidence → AgentResult.final == 'rendered'."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        m_llm.side_effect = [
            _fake_llm_response("SELECT count(*) FROM transactions"),
            _fake_llm_response(json.dumps({
                "verdict": "ok", "confidence": 0.92, "reason": "matches question",
            })),
        ]
        m_conn.return_value = _fake_readonly_conn([{"count": 1227}])

        result = run_sql_agent("How many transactions total?")

    assert isinstance(result, AgentResult)
    assert result.final == "rendered"
    assert result.sql == "SELECT count(*) FROM transactions"
    assert result.rows == [{"count": 1227}]
    assert result.judge_verdict.verdict == "ok"
    assert result.escalated is False
    assert result.retried is False


def test_validator_reject_short_circuits_before_db_call():
    """If Groq generates an INSERT, validator rejects it BEFORE the DB call.
    Agent should never connect to the readonly DB in this path."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        m_llm.return_value = _fake_llm_response(
            "INSERT INTO transactions (date) VALUES (CURRENT_DATE)"
        )

        result = run_sql_agent("Add a fake row")

    assert result.final == "validator_rejected"
    assert result.judge_verdict is None
    m_conn.assert_not_called()  # critical — never touched DB


def test_genuine_empty_result_passes_judge():
    """An empty result set from a valid SELECT is NOT an error. Judge should
    return verdict=ok if the SQL was right; agent renders empty cleanly."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        m_llm.side_effect = [
            _fake_llm_response(
                "SELECT count(*) FROM transactions WHERE raw_merchant ILIKE '%cricket%'"
            ),
            _fake_llm_response(json.dumps({
                "verdict": "ok", "confidence": 0.88,
                "reason": "no rows but SQL semantics are correct",
            })),
        ]
        m_conn.return_value = _fake_readonly_conn([])

        result = run_sql_agent("How much did I spend on cricket gear?")

    assert result.final == "rendered"
    assert result.rows == []
```

- [ ] **Step 7.2: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_sql_agent.py -v 2>&1 | tail -10
```

Expected: ImportError on `skills.finance.agents.sql_agent`.

- [ ] **Step 7.3: Implement orchestrator (happy path only)**

Write `skills/finance/agents/sql_agent.py`:
```python
"""W4.1 SQL agent orchestrator.

Synchronous (psycopg + LiteLLM are both sync). The bot handler wraps the
entry point in asyncio.to_thread so the aiogram loop is never blocked.

Pipeline (this task implements the happy path only):
  question
    → llm("sql_agent") → SQL
    → validate_sql() → reject? short-circuit
    → readonly_client().execute(SQL) → rows
    → llm("sql_agent_judge", build_judge_prompt(...)) → JudgeVerdict
    → if verdict==ok AND confidence >= threshold: render
    → else: (escalation/retry — Tasks 8/9)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from skills.finance.agents.judge import (
    JudgeVerdict,
    build_judge_prompt,
    parse_judge_response,
)
from skills.finance.agents.review_config import ReviewConfig, load_review_config
from skills.finance.agents.sql_validator import ValidationResult, validate_sql
from skills.finance.lib.db import readonly_client
from skills.finance.lib.llm import llm

_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "config" / "db_schema_for_judge.md"

ALLOWED_TABLES = {
    "transactions", "accounts", "categories", "assets", "liabilities",
    "users", "ingestion_log", "commitments", "income_events",
}

FinalOutcome = Literal["rendered", "validator_rejected"]


@dataclass(frozen=True)
class AgentResult:
    final: FinalOutcome
    sql: str
    rows: list[dict] | None
    judge_verdict: JudgeVerdict | None
    validator_result: ValidationResult
    escalated: bool
    retried: bool
    reason: str | None  # surfaced reason on rejection / surface-to-user paths


def _load_schema_excerpt() -> str:
    with open(_SCHEMA_PATH) as f:
        return f.read()


def _exec_select(sql: str) -> list[dict]:
    conn = readonly_client()
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] if isinstance(d, tuple) else d.name for d in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def run_sql_agent(question: str, cfg: ReviewConfig | None = None) -> AgentResult:
    cfg = cfg or load_review_config()
    schema = _load_schema_excerpt()

    # 1. Generate SQL via Groq Llama 3.3 70B (sql_agent route).
    gen_resp = llm(
        "sql_agent",
        f"Generate a single PostgreSQL SELECT statement that answers this question:\n\n{question}\n\n"
        f"Schema:\n{schema}\n\n"
        "Output ONLY the SQL, no preamble, no markdown.",
    )
    sql = gen_resp.choices[0].message.content.strip()
    # Strip markdown codefence if present
    if sql.startswith("```"):
        sql = sql.strip("`")
        if sql.startswith("sql"):
            sql = sql[3:]
        sql = sql.strip()

    # 2. Static validation.
    val = validate_sql(sql, ALLOWED_TABLES)
    if not val.ok:
        return AgentResult(
            final="validator_rejected", sql=sql, rows=None,
            judge_verdict=None, validator_result=val,
            escalated=False, retried=False,
            reason=val.reason,
        )

    # 3. Execute on readonly DB.
    rows = _exec_select(sql)

    # 4. Judge.
    judge_prompt = build_judge_prompt(
        question=question, sql=sql, result_preview=rows,
        schema_excerpt=schema,
    )
    judge_resp = llm(
        "sql_agent_judge",
        judge_prompt,
        response_format={"type": "json_object"},
    )
    verdict = parse_judge_response(judge_resp.choices[0].message.content)

    # 5. Happy path: render only if verdict=ok AND confidence high enough.
    if verdict.verdict == "ok" and verdict.confidence >= cfg.confidence_threshold:
        return AgentResult(
            final="rendered", sql=sql, rows=rows,
            judge_verdict=verdict, validator_result=val,
            escalated=False, retried=False, reason=None,
        )

    # Escalation / retry paths land in Tasks 8/9. For this task, surface a
    # placeholder that lets the happy-path tests pass.
    raise NotImplementedError(
        f"Non-happy-path verdict={verdict.verdict}, confidence={verdict.confidence}. "
        f"Escalation+retry implemented in Tasks 8/9."
    )
```

- [ ] **Step 7.4: Run tests to verify they pass**

Run:
```
.venv/bin/python -m pytest tests/test_sql_agent.py -v 2>&1 | tail -15
```

Expected: 3 passed.

- [ ] **Step 7.5: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add skills/finance/agents/sql_agent.py tests/test_sql_agent.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(agents): SQL agent happy-path orchestrator (W4.1 §4.1)

Pipeline: llm(sql_agent) → validate_sql → readonly_client().execute
→ llm(sql_agent_judge) with response_format=json → render iff
verdict=ok AND confidence >= threshold. Validator short-circuit
prevents DB calls on rejected SQL (asserted by test). Escalation
+ retry paths NotImplementedError placeholder — Tasks 8/9.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Escalation path — Anthropic Sonnet strict judge

**Files:**
- Modify: `skills/finance/agents/sql_agent.py`
- Modify: `tests/test_sql_agent.py` (add escalation tests)

- [ ] **Step 8.1: Write failing tests for escalation**

Append to `tests/test_sql_agent.py`:
```python


def test_low_confidence_escalates_to_strict_judge():
    """verdict=ok but confidence < threshold → escalate to sql_agent_judge_strict.
    If strict judge says ok, render."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        m_llm.side_effect = [
            _fake_llm_response("SELECT count(*) FROM transactions"),
            # Gemini: ok but low confidence
            _fake_llm_response(json.dumps({
                "verdict": "ok", "confidence": 0.5, "reason": "looks ok-ish",
            })),
            # Sonnet strict judge: ok with high confidence
            _fake_llm_response(json.dumps({
                "verdict": "ok", "confidence": 0.95, "reason": "verified",
            })),
        ]
        m_conn.return_value = _fake_readonly_conn([{"count": 1227}])

        result = run_sql_agent("How many transactions total?")

    assert result.final == "rendered"
    assert result.escalated is True
    # The third LLM call should be the strict judge
    assert m_llm.call_args_list[2][0][0] == "sql_agent_judge_strict"


def test_uncertain_verdict_escalates():
    """verdict=uncertain (any confidence) → escalate."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        m_llm.side_effect = [
            _fake_llm_response("SELECT count(*) FROM transactions"),
            _fake_llm_response(json.dumps({
                "verdict": "uncertain", "confidence": 0.9, "reason": "ambiguous question",
            })),
            _fake_llm_response(json.dumps({
                "verdict": "ok", "confidence": 0.95, "reason": "verified",
            })),
        ]
        m_conn.return_value = _fake_readonly_conn([{"count": 1227}])

        result = run_sql_agent("How many things?")

    assert result.final == "rendered"
    assert result.escalated is True


def test_strict_judge_says_wrong_falls_to_retry():
    """If strict judge also rejects, fall to retry path (Task 9 — for now,
    NotImplementedError is acceptable; this test asserts the strict judge
    WAS called)."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        m_llm.side_effect = [
            _fake_llm_response("SELECT count(*) FROM transactions"),
            _fake_llm_response(json.dumps({
                "verdict": "uncertain", "confidence": 0.4, "reason": "?",
            })),
            _fake_llm_response(json.dumps({
                "verdict": "wrong", "confidence": 0.9, "reason": "wrong table",
            })),
        ]
        m_conn.return_value = _fake_readonly_conn([{"count": 1227}])

        with pytest.raises(NotImplementedError, match="retry"):
            run_sql_agent("How many transactions?")
        # confirm we DID escalate before falling through
        called_tasks = [c[0][0] for c in m_llm.call_args_list]
        assert "sql_agent_judge_strict" in called_tasks
```

- [ ] **Step 8.2: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_sql_agent.py -v 2>&1 | tail -20
```

Expected: the new tests fail with NotImplementedError on "Escalation+retry implemented in Tasks 8/9."

- [ ] **Step 8.3: Implement escalation path**

Edit `skills/finance/agents/sql_agent.py`. Replace the final block (from `# 5. Happy path...` through the `NotImplementedError(...)`) with:
```python
    # 5. Happy path: render only if verdict=ok AND confidence high enough.
    if verdict.verdict == "ok" and verdict.confidence >= cfg.confidence_threshold:
        return AgentResult(
            final="rendered", sql=sql, rows=rows,
            judge_verdict=verdict, validator_result=val,
            escalated=False, retried=False, reason=None,
        )

    # 6. Escalation path — Sonnet strict judge.
    strict_resp = llm(
        "sql_agent_judge_strict",
        judge_prompt,
        response_format={"type": "json_object"},
    )
    strict_verdict = parse_judge_response(strict_resp.choices[0].message.content)

    if strict_verdict.verdict == "ok":
        return AgentResult(
            final="rendered", sql=sql, rows=rows,
            judge_verdict=strict_verdict, validator_result=val,
            escalated=True, retried=False, reason=None,
        )

    # Strict judge said wrong (or uncertain — treat as wrong on escalation):
    # fall to retry path (Task 9).
    raise NotImplementedError(
        f"Strict judge returned verdict={strict_verdict.verdict}. "
        f"Retry path lands in Task 9."
    )
```

Also update `FinalOutcome` literal at the top of file (no change needed yet — both rendered and validator_rejected are still the only successful outcomes; retry/surface land in Task 9).

- [ ] **Step 8.4: Run tests to verify they pass**

Run:
```
.venv/bin/python -m pytest tests/test_sql_agent.py -v 2>&1 | tail -20
```

Expected: 6 passed (3 happy-path + 3 escalation).

- [ ] **Step 8.5: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add skills/finance/agents/sql_agent.py tests/test_sql_agent.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(agents): SQL agent escalation path to Sonnet strict judge (W4.1 §4.2)

When Gemini returns verdict=uncertain OR (verdict=ok AND confidence<threshold),
escalate to llm(sql_agent_judge_strict). If strict judge agrees ok, render.
If strict judge says wrong/uncertain, fall to retry path (Task 9, currently
NotImplementedError). escalated=True flag surfaced on AgentResult.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Retry path + sql_agent_strict last-resort

**Files:**
- Modify: `skills/finance/agents/sql_agent.py`
- Modify: `tests/test_sql_agent.py`

- [ ] **Step 9.1: Write failing tests for retry path**

Append to `tests/test_sql_agent.py`:
```python


def test_judge_wrong_triggers_groq_retry_with_critique():
    """verdict=wrong on first judge → retry sql_agent with the judge's critique
    embedded in the new prompt. Retry must include the original critique text."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        m_llm.side_effect = [
            _fake_llm_response("SELECT date FROM transactions"),
            _fake_llm_response(json.dumps({
                "verdict": "wrong", "confidence": 0.9,
                "reason": "missing aggregation",
            })),
            _fake_llm_response("SELECT count(*) FROM transactions"),
            _fake_llm_response(json.dumps({
                "verdict": "ok", "confidence": 0.95, "reason": "fixed",
            })),
        ]
        m_conn.return_value = _fake_readonly_conn([{"count": 1227}])

        result = run_sql_agent("How many transactions total?")

    assert result.final == "rendered"
    assert result.retried is True
    # The retry prompt must include the critique
    retry_call_prompt = m_llm.call_args_list[2][0][1]
    assert "missing aggregation" in retry_call_prompt


def test_max_retries_exhausted_falls_to_sql_agent_strict():
    """After max_retry_rounds (default 2), invoke sql_agent_strict
    (Sonnet last-resort SQL generation)."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        # gen, judge=wrong, retry1 gen, judge=wrong, retry2 gen, judge=wrong,
        # then strict gen, then strict judge=ok
        m_llm.side_effect = [
            _fake_llm_response("SELECT 1 FROM transactions"),
            _fake_llm_response(json.dumps({"verdict":"wrong","confidence":0.9,"reason":"r1"})),
            _fake_llm_response("SELECT 2 FROM transactions"),
            _fake_llm_response(json.dumps({"verdict":"wrong","confidence":0.9,"reason":"r2"})),
            _fake_llm_response("SELECT 3 FROM transactions"),
            _fake_llm_response(json.dumps({"verdict":"wrong","confidence":0.9,"reason":"r3"})),
            _fake_llm_response("SELECT count(*) FROM transactions"),  # strict gen
            _fake_llm_response(json.dumps({"verdict":"ok","confidence":0.95,"reason":"ok"})),
        ]
        m_conn.return_value = _fake_readonly_conn([{"count": 1227}])

        result = run_sql_agent("?")

    assert result.final == "rendered"
    assert result.retried is True
    called_tasks = [c[0][0] for c in m_llm.call_args_list]
    assert "sql_agent_strict" in called_tasks


def test_strict_generation_also_fails_surfaces_to_user():
    """If sql_agent_strict's SQL ALSO fails the judge, surface "rephrase?"
    to the user via AgentResult.final = 'surfaced_to_user'."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        # 2 retries all wrong, then strict also wrong
        m_llm.side_effect = [
            _fake_llm_response("SELECT 1 FROM transactions"),
            _fake_llm_response(json.dumps({"verdict":"wrong","confidence":0.9,"reason":"r1"})),
            _fake_llm_response("SELECT 2 FROM transactions"),
            _fake_llm_response(json.dumps({"verdict":"wrong","confidence":0.9,"reason":"r2"})),
            _fake_llm_response("SELECT 3 FROM transactions"),
            _fake_llm_response(json.dumps({"verdict":"wrong","confidence":0.9,"reason":"r3"})),
            _fake_llm_response("SELECT 4 FROM transactions"),  # strict gen
            _fake_llm_response(json.dumps({"verdict":"wrong","confidence":0.95,"reason":"still wrong"})),
        ]
        m_conn.return_value = _fake_readonly_conn([{"x": 1}])

        result = run_sql_agent("ambiguous question")

    assert result.final == "surfaced_to_user"
    assert "rephrase" in (result.reason or "").lower()
```

- [ ] **Step 9.2: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_sql_agent.py -v 2>&1 | tail -15
```

Expected: 3 new tests fail (NotImplementedError or AttributeError).

- [ ] **Step 9.3: Implement retry path + strict generation**

Edit `skills/finance/agents/sql_agent.py`. Update `FinalOutcome` literal:
```python
FinalOutcome = Literal["rendered", "validator_rejected", "surfaced_to_user"]
```

Then replace the last block (escalation path through `NotImplementedError`) with:
```python
    # 5. Happy path: render only if verdict=ok AND confidence high enough.
    if verdict.verdict == "ok" and verdict.confidence >= cfg.confidence_threshold:
        return AgentResult(
            final="rendered", sql=sql, rows=rows,
            judge_verdict=verdict, validator_result=val,
            escalated=False, retried=False, reason=None,
        )

    # 6. Escalation path — Sonnet strict judge — for uncertain or low-confidence-ok.
    if verdict.verdict in ("uncertain",) or (verdict.verdict == "ok" and verdict.confidence < cfg.confidence_threshold):
        strict_resp = llm(
            "sql_agent_judge_strict",
            judge_prompt,
            response_format={"type": "json_object"},
        )
        strict_verdict = parse_judge_response(strict_resp.choices[0].message.content)
        if strict_verdict.verdict == "ok":
            return AgentResult(
                final="rendered", sql=sql, rows=rows,
                judge_verdict=strict_verdict, validator_result=val,
                escalated=True, retried=False, reason=None,
            )
        # Strict judge says wrong: fall to retry path with strict's critique.
        verdict = strict_verdict

    # 7. Retry path — verdict=wrong.
    current_sql = sql
    current_rows = rows
    current_verdict = verdict
    retried = False
    for _ in range(cfg.max_retry_rounds):
        retried = True
        retry_prompt = (
            f"Your previous SQL:\n```sql\n{current_sql}\n```\n"
            f"was rejected because: {current_verdict.reason}\n\n"
            f"Original question: {question}\n\n"
            f"Schema:\n{schema}\n\n"
            f"Generate a corrected single PostgreSQL SELECT. "
            f"Output ONLY the SQL, no preamble, no markdown."
        )
        retry_resp = llm("sql_agent", retry_prompt)
        current_sql = retry_resp.choices[0].message.content.strip()
        if current_sql.startswith("```"):
            current_sql = current_sql.strip("`")
            if current_sql.startswith("sql"):
                current_sql = current_sql[3:]
            current_sql = current_sql.strip()

        val = validate_sql(current_sql, ALLOWED_TABLES)
        if not val.ok:
            continue  # validator failure on retry; loop again with same critique

        current_rows = _exec_select(current_sql)
        retry_judge_prompt = build_judge_prompt(
            question=question, sql=current_sql, result_preview=current_rows,
            schema_excerpt=schema,
        )
        retry_resp = llm(
            "sql_agent_judge",
            retry_judge_prompt,
            response_format={"type": "json_object"},
        )
        current_verdict = parse_judge_response(retry_resp.choices[0].message.content)
        if current_verdict.verdict == "ok" and current_verdict.confidence >= cfg.confidence_threshold:
            return AgentResult(
                final="rendered", sql=current_sql, rows=current_rows,
                judge_verdict=current_verdict, validator_result=val,
                escalated=False, retried=True, reason=None,
            )

    # 8. Last-resort: sql_agent_strict (Sonnet generates the SQL).
    strict_gen_resp = llm(
        "sql_agent_strict",
        f"Generate a single PostgreSQL SELECT that answers this question:\n\n{question}\n\n"
        f"Previous attempts failed because: {current_verdict.reason}\n\n"
        f"Schema:\n{schema}\n\n"
        "Output ONLY the SQL, no preamble, no markdown.",
    )
    strict_sql = strict_gen_resp.choices[0].message.content.strip()
    if strict_sql.startswith("```"):
        strict_sql = strict_sql.strip("`")
        if strict_sql.startswith("sql"):
            strict_sql = strict_sql[3:]
        strict_sql = strict_sql.strip()

    val = validate_sql(strict_sql, ALLOWED_TABLES)
    if val.ok:
        strict_rows = _exec_select(strict_sql)
        strict_judge_resp = llm(
            "sql_agent_judge",
            build_judge_prompt(
                question=question, sql=strict_sql, result_preview=strict_rows,
                schema_excerpt=schema,
            ),
            response_format={"type": "json_object"},
        )
        strict_judge_verdict = parse_judge_response(strict_judge_resp.choices[0].message.content)
        if strict_judge_verdict.verdict == "ok":
            return AgentResult(
                final="rendered", sql=strict_sql, rows=strict_rows,
                judge_verdict=strict_judge_verdict, validator_result=val,
                escalated=True, retried=True, reason=None,
            )

    # 9. Surface to user — even the strict generator couldn't satisfy the judge.
    return AgentResult(
        final="surfaced_to_user", sql=current_sql, rows=current_rows,
        judge_verdict=current_verdict, validator_result=val,
        escalated=True, retried=True,
        reason=(
            "I'm not sure how to answer this one — can you rephrase the question, "
            "or split it into smaller parts?"
        ),
    )
```

- [ ] **Step 9.4: Run tests to verify they pass**

Run:
```
.venv/bin/python -m pytest tests/test_sql_agent.py -v 2>&1 | tail -20
```

Expected: 9 passed (3 happy-path + 3 escalation + 3 retry).

- [ ] **Step 9.5: Lint + typecheck**

Run:
```
.venv/bin/ruff check skills/finance/agents tests/test_sql_agent.py && .venv/bin/mypy skills/finance/agents
```

Expected: clean.

- [ ] **Step 9.6: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add skills/finance/agents/sql_agent.py tests/test_sql_agent.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(agents): retry path + sql_agent_strict last-resort (W4.1 §4.3)

Closes the reviewer-layer state machine. On verdict=wrong, retry with
the judge's critique embedded in the prompt; up to max_retry_rounds
(default 2). Beyond that, last-resort llm(sql_agent_strict) — Sonnet
generates the SQL directly. If even strict generation fails the judge,
AgentResult.final='surfaced_to_user' with a rephrase prompt.

9 tests now cover the full state machine: happy path, validator
rejection, escalation, retry-then-render, max-retries-then-strict,
strict-also-fails-then-surfaces.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Sensitive-prompt-content logging invariant

**Files:**
- Create: `tests/test_llm_logging_invariant.py`
- Modify: `CLAUDE.md` (add invariant)

Per spec §4.5: the rendered judge prompt contains real schema names + the first 3 result rows (real PII). LiteLLM's `success_callback = ["supabase"]` writes to `request_logs`. The W4.1 invariant is that **only call metadata** is logged, never prompt bodies.

- [ ] **Step 10.1: Write the invariant test**

Write `tests/test_llm_logging_invariant.py`:
```python
"""W4.1 §4.5 sensitivity invariant: lib/llm.py's success_callback writes
ONLY metadata (token counts, model, latency, cost) to request_logs.
It does NOT persist the rendered prompt body. Extending this without an
explicit privacy review = leaking PII (schema_excerpt + result_preview).

This test guards the invariant by inspecting llm.py's source for the
callback registration pattern. It is brittle by design — any change to
how callbacks are registered should force a deliberate read of this test."""
from __future__ import annotations

from pathlib import Path


def test_llm_module_does_not_register_body_logging_callback():
    """llm.py registers `litellm.success_callback = ["supabase"]` ONLY.
    Adding a custom callable that captures `messages=` or `prompt` content
    would defeat this invariant — fail loudly if the surface changes."""
    src = Path("skills/finance/lib/llm.py").read_text()

    # The exact line we expect — if anyone re-registers callbacks differently,
    # this assertion forces a review.
    assert 'litellm.success_callback = ["supabase"]' in src, (
        "lib/llm.py changed how it registers LiteLLM callbacks. "
        "Per CLAUDE.md invariant on prompt sensitivity, any new callback "
        "MUST NOT capture `messages=` or full prompt bodies."
    )

    # And no signs of body-capturing helpers
    forbidden = ["full_prompt", "messages_to_db", "log_prompt_body", "capture_prompt"]
    for token in forbidden:
        assert token not in src, (
            f"Forbidden token {token!r} appeared in lib/llm.py — "
            f"this implies prompt-body logging, which violates spec §4.5."
        )
```

- [ ] **Step 10.2: Run test to verify it passes**

Run:
```
.venv/bin/python -m pytest tests/test_llm_logging_invariant.py -v 2>&1 | tail -5
```

Expected: 1 passed (this is a guard, not a TDD red→green; current state is already correct).

- [ ] **Step 10.3: Add the invariant to CLAUDE.md**

Edit `CLAUDE.md`. After invariant #11 (the verify-before-pinning one), insert:
```
12. **Judge-prompt content (schema_excerpt + result_preview) carries `credentials.yaml`-level sensitivity.** LiteLLM's `success_callback` in `lib/llm.py` logs metadata only — never extend it to capture `messages=` or rendered prompts without an explicit privacy review. Real schema names + the first 3 result rows = real PII. Regression-tested by `tests/test_llm_logging_invariant.py`. Per spec §4.5.
```

- [ ] **Step 10.4: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add tests/test_llm_logging_invariant.py CLAUDE.md && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(llm): guard the prompt-body-not-logged invariant (W4.1 §4.5)

The judge prompt embeds real schema names + first 3 result rows
(real PII). LiteLLM's success_callback writes metadata only;
extending it to capture prompt bodies would leak PII. Regression
test inspects lib/llm.py source for the safe pattern; CLAUDE.md
gains invariant #12.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Bot wiring — `/ask` command

**Files:**
- Modify: `skills/finance/bot/main.py`
- Create: `tests/test_bot_ask_handler.py`

- [ ] **Step 11.1: Find the current bot/main.py structure**

Run:
```
grep -n "@router\|def _is_rajat\|message_handler\|cmd" skills/finance/bot/main.py 2>&1 | head -20
```

Capture the exact handler-registration pattern + the `_is_rajat` signature. This determines how `/ask` slots in.

- [ ] **Step 11.2: Write failing test for the handler**

Write `tests/test_bot_ask_handler.py`. The exact import path depends on what Step 11.1 reveals — adjust the import to match:
```python
"""W4.1 /ask command handler — wires the SQL agent to the aiogram bot.

We mock run_sql_agent and the message-send call so the test is offline.
The whitelist gate (_is_rajat) is reused from the existing pattern."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skills.finance.agents.sql_agent import AgentResult
from skills.finance.agents.sql_validator import ValidationResult
from skills.finance.agents.judge import JudgeVerdict


@pytest.mark.asyncio
async def test_ask_handler_renders_success():
    """Successful agent run → bot replies with the SQL + first rows."""
    from skills.finance.bot.main import cmd_ask  # may not exist yet

    msg = MagicMock()
    msg.from_user.id = 12345
    msg.text = "/ask How many transactions total?"
    msg.answer = AsyncMock()

    fake_result = AgentResult(
        final="rendered",
        sql="SELECT count(*) FROM transactions",
        rows=[{"count": 1227}],
        judge_verdict=JudgeVerdict(verdict="ok", confidence=0.95, reason="ok"),
        validator_result=ValidationResult(ok=True, statement_type="select"),
        escalated=False,
        retried=False,
        reason=None,
    )

    with patch("skills.finance.bot.main._is_rajat", return_value=True), \
         patch("skills.finance.bot.main.run_sql_agent_async", return_value=fake_result):
        await cmd_ask(msg)

    msg.answer.assert_called_once()
    sent_text = msg.answer.call_args[0][0]
    assert "1227" in sent_text
    assert "SELECT count(*)" in sent_text


@pytest.mark.asyncio
async def test_ask_handler_renders_surface_to_user():
    """When agent surfaces, bot relays the rephrase message — no SQL shown."""
    from skills.finance.bot.main import cmd_ask

    msg = MagicMock()
    msg.from_user.id = 12345
    msg.text = "/ask ambiguous question"
    msg.answer = AsyncMock()

    fake_result = AgentResult(
        final="surfaced_to_user",
        sql="SELECT 4 FROM transactions",
        rows=[],
        judge_verdict=JudgeVerdict(verdict="wrong", confidence=0.95, reason="still wrong"),
        validator_result=ValidationResult(ok=True, statement_type="select"),
        escalated=True,
        retried=True,
        reason="I'm not sure how to answer — rephrase?",
    )

    with patch("skills.finance.bot.main._is_rajat", return_value=True), \
         patch("skills.finance.bot.main.run_sql_agent_async", return_value=fake_result):
        await cmd_ask(msg)

    sent_text = msg.answer.call_args[0][0]
    assert "rephrase" in sent_text.lower()
    assert "SELECT 4" not in sent_text  # don't expose the failed SQL


@pytest.mark.asyncio
async def test_ask_handler_whitelist_silently_rejects_non_rajat():
    """Per CLAUDE.md invariant #7, non-whitelisted users get silent return."""
    from skills.finance.bot.main import cmd_ask

    msg = MagicMock()
    msg.from_user.id = 99999
    msg.text = "/ask anything"
    msg.answer = AsyncMock()

    with patch("skills.finance.bot.main._is_rajat", return_value=False):
        await cmd_ask(msg)

    msg.answer.assert_not_called()


@pytest.mark.asyncio
async def test_ask_handler_empty_question_replies_with_usage():
    """`/ask` with no question text → terse usage hint, no agent call."""
    from skills.finance.bot.main import cmd_ask

    msg = MagicMock()
    msg.from_user.id = 12345
    msg.text = "/ask"
    msg.answer = AsyncMock()

    with patch("skills.finance.bot.main._is_rajat", return_value=True), \
         patch("skills.finance.bot.main.run_sql_agent_async") as m_agent:
        await cmd_ask(msg)

    m_agent.assert_not_called()
    msg.answer.assert_called_once()
    assert "/ask" in msg.answer.call_args[0][0]
```

- [ ] **Step 11.3: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_bot_ask_handler.py -v 2>&1 | tail -10
```

Expected: ImportError on `cmd_ask` or `run_sql_agent_async`.

- [ ] **Step 11.4: Add handler to bot/main.py**

Open `skills/finance/bot/main.py`. After the existing handlers (use Step 11.1's reading to find the right insertion point), add:
```python

# --- W4.1 /ask handler ------------------------------------------------------

import asyncio
from skills.finance.agents.sql_agent import AgentResult, run_sql_agent


async def run_sql_agent_async(question: str) -> AgentResult:
    """Thread-pool wrapper. run_sql_agent is sync (psycopg + LiteLLM are sync);
    we hop to a worker thread so the aiogram loop is never blocked."""
    return await asyncio.to_thread(run_sql_agent, question)


def _render_rendered(result: AgentResult) -> str:
    n = len(result.rows or [])
    head = f"Answer ({n} row{'s' if n != 1 else ''}):"
    preview_lines = []
    for r in (result.rows or [])[:10]:
        preview_lines.append("  " + ", ".join(f"{k}={v}" for k, v in r.items()))
    sql_block = f"```sql\n{result.sql}\n```"
    tags = []
    if result.escalated:
        tags.append("escalated to Sonnet")
    if result.retried:
        tags.append("retried")
    tag_line = f"({', '.join(tags)})\n" if tags else ""
    return f"{tag_line}{head}\n" + "\n".join(preview_lines) + f"\n\n{sql_block}"


def _render_surface(result: AgentResult) -> str:
    return result.reason or "I couldn't answer — please rephrase."


def _render_rejected(result: AgentResult) -> str:
    return f"That question generated SQL I can't run safely ({result.reason}). Try rephrasing."


@router.message(Command("ask"))
async def cmd_ask(message: Message) -> None:
    if not _is_rajat(message):
        return
    text = (message.text or "").strip()
    # /ask without a question
    if text == "/ask" or text.startswith("/ask ") and not text[5:].strip():
        await message.answer(
            "Usage: /ask <question about your finances>\n"
            "Example: /ask How much did I spend on food last month?"
        )
        return

    question = text[len("/ask "):].strip()
    try:
        result = await run_sql_agent_async(question)
    except Exception as e:  # noqa: BLE001
        logger.exception("/ask agent crashed for question: %s", question)
        await message.answer(f"Something went wrong: {type(e).__name__}. Try again or rephrase.")
        return

    if result.final == "rendered":
        await message.answer(_render_rendered(result))
    elif result.final == "surfaced_to_user":
        await message.answer(_render_surface(result))
    elif result.final == "validator_rejected":
        await message.answer(_render_rejected(result))
    else:
        await message.answer(f"Unhandled outcome: {result.final}")
```

Add the necessary imports at the top of the file: `from aiogram.filters import Command`, `from aiogram.types import Message` (whichever isn't already there — Step 11.1's grep determines this).

- [ ] **Step 11.5: Run tests to verify they pass**

Run:
```
.venv/bin/python -m pytest tests/test_bot_ask_handler.py -v 2>&1 | tail -10
```

Expected: 4 passed.

- [ ] **Step 11.6: Lint + typecheck**

Run:
```
.venv/bin/ruff check skills/finance/bot/main.py tests/test_bot_ask_handler.py && .venv/bin/mypy skills/finance/bot/main.py
```

Expected: clean. If lint complains about unused imports or import order, fix inline.

- [ ] **Step 11.7: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add skills/finance/bot/main.py tests/test_bot_ask_handler.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(bot): /ask command surface for W4.1 SQL agent

Whitelist-gated (_is_rajat per CLAUDE.md invariant #7) /ask handler
wraps run_sql_agent in asyncio.to_thread, renders four outcomes:
rendered (with optional escalated/retried tags), surfaced_to_user
(rephrase), validator_rejected (don't expose SQL), and a generic
crash fallback. 4 tests cover happy, surface, whitelist-reject,
empty-question.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Low-balance Anthropic alert

**Files:**
- Modify: `skills/finance/monitoring/alerts.py`
- Modify: `app.py` (scheduler registration)
- Create: `tests/test_anthropic_balance_alert.py`

Approach depends on Task 0.5 outcome. Two implementation paths — pick at Task 0.5; this plan covers Path A (Anthropic API) AND Path B (compute from `request_logs`); use whichever Task 0.5 selected. **Both paths share the same test surface** — only the internal source of the balance number differs.

- [ ] **Step 12.1: Write failing test**

Write `tests/test_anthropic_balance_alert.py`:
```python
"""W4.1 §4.4 + §7: scheduled job that alerts on Anthropic balance < $3.
Path A (API) or Path B (logs-derived) — both exposed through the same
async function `check_anthropic_balance()`."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_alert_fires_below_threshold():
    """When balance < anthropic_balance_warning_usd, send_alert is called."""
    from skills.finance.monitoring import alerts

    with patch.object(alerts, "_fetch_anthropic_balance_usd", return_value=2.50) as m_fetch, \
         patch.object(alerts, "send_alert", new=AsyncMock()) as m_send:
        await alerts.check_anthropic_balance()

    m_fetch.assert_called_once()
    m_send.assert_called_once()
    sent_text = m_send.call_args[0][0]
    assert "2.5" in sent_text or "2.50" in sent_text
    assert "anthropic" in sent_text.lower() or "balance" in sent_text.lower()


@pytest.mark.asyncio
async def test_no_alert_above_threshold():
    from skills.finance.monitoring import alerts

    with patch.object(alerts, "_fetch_anthropic_balance_usd", return_value=4.50), \
         patch.object(alerts, "send_alert", new=AsyncMock()) as m_send:
        await alerts.check_anthropic_balance()

    m_send.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_failure_alerts_once_then_continues():
    """If the fetch fails (API down, auth issue), alert that we couldn't
    check — don't crash the scheduler."""
    from skills.finance.monitoring import alerts

    with patch.object(alerts, "_fetch_anthropic_balance_usd", side_effect=RuntimeError("boom")), \
         patch.object(alerts, "send_alert", new=AsyncMock()) as m_send:
        await alerts.check_anthropic_balance()

    m_send.assert_called_once()
    assert "couldn't" in m_send.call_args[0][0].lower() or "failed" in m_send.call_args[0][0].lower()
```

- [ ] **Step 12.2: Run tests to verify they fail**

Run:
```
.venv/bin/python -m pytest tests/test_anthropic_balance_alert.py -v 2>&1 | tail -10
```

Expected: AttributeError on `check_anthropic_balance` or `_fetch_anthropic_balance_usd`.

- [ ] **Step 12.3: Implement the balance check + alert**

Edit `skills/finance/monitoring/alerts.py`. Append:
```python


# --- W4.1 Anthropic balance check -----------------------------------------

from skills.finance.agents.review_config import load_review_config


def _fetch_anthropic_balance_usd() -> float:
    """Return current Anthropic balance in USD.

    Path A (preferred, per Task 0.5 if endpoint exists): hit Anthropic billing API.
    Path B (fallback): derive from request_logs by subtracting cumulative spend
    from a known starting credit ($5 at the start of V1 per project memory).

    Implementation here uses Path B by default since it requires no new auth scope.
    Swap to Path A by replacing this function body if the API path is available."""
    from decimal import Decimal
    from skills.finance.lib.db import adb, service_client
    import asyncio

    INITIAL_CREDIT_USD = 5.00  # per memory project_pfa_status.md
    # request_logs is auto-written by LiteLLM's supabase callback per W1.
    # Column is `response_cost` (USD) per the LiteLLM-supabase integration shape.

    def _query():
        return (
            service_client()
            .table("request_logs")
            .select("response_cost")
            .ilike("model", "%anthropic%")
            .execute()
        )

    # sync call OK here — this fn is called from inside an async fn but via thread
    res = _query()
    spent = sum(
        float(r.get("response_cost") or 0.0)
        for r in (res.data or [])
    )
    return INITIAL_CREDIT_USD - spent


async def check_anthropic_balance() -> None:
    """Scheduled daily — fire a Telegram alert when balance drops below threshold."""
    cfg = load_review_config()
    try:
        balance = _fetch_anthropic_balance_usd()
    except Exception as e:  # noqa: BLE001
        logger.exception("anthropic balance check failed")
        await send_alert(
            f"Couldn't check Anthropic balance: {type(e).__name__}: {e}. "
            f"Verify the API path or request_logs availability."
        )
        return

    if balance < cfg.anthropic_balance_warning_usd:
        await send_alert(
            f"Anthropic balance ${balance:.2f} < threshold "
            f"${cfg.anthropic_balance_warning_usd:.2f}. "
            f"Decide: top-up, drop strict-judge escalation, or stay Gemini-only."
        )
    else:
        logger.info("Anthropic balance OK: $%.2f", balance)
```

Note: the imports at the top of `alerts.py` need `import logging; logger = logging.getLogger(__name__)` if not already there — verify in Step 11.1.

- [ ] **Step 12.4: Run tests to verify they pass**

Run:
```
.venv/bin/python -m pytest tests/test_anthropic_balance_alert.py -v 2>&1 | tail -10
```

Expected: 3 passed.

- [ ] **Step 12.5: Wire into the scheduler**

Edit `app.py`. Find the APScheduler job-registration block (probably near `scheduler.add_job(...)` for the heartbeat). Add:
```python
    from skills.finance.monitoring.alerts import check_anthropic_balance
    scheduler.add_job(
        check_anthropic_balance,
        trigger="cron", hour=9, minute=0,
        timezone=settings.timezone,
        id="anthropic_balance_check",
        replace_existing=True,
    )
    logger.info("scheduled daily anthropic balance check at 09:00 %s", settings.timezone)
```

- [ ] **Step 12.6: Lint + typecheck**

Run:
```
.venv/bin/ruff check skills/finance/monitoring/alerts.py app.py tests/test_anthropic_balance_alert.py && .venv/bin/mypy skills/finance/monitoring app.py
```

Expected: clean.

- [ ] **Step 12.7: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add skills/finance/monitoring/alerts.py app.py tests/test_anthropic_balance_alert.py && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(monitoring): daily Anthropic balance alert (W4.1 §7)

Scheduled daily at 09:00 Asia/Kolkata. Compares balance against the
anthropic_balance_warning_usd threshold (default \$3) from
sql_agent_review.yaml. Default implementation derives balance from
cumulative request_logs spend subtracted from a known \$5 starting
credit (Path B — no new API auth needed). Path A (direct API) is a
drop-in replacement of _fetch_anthropic_balance_usd. 3 tests cover
below-threshold, above-threshold, fetch-failure.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: Calibration harness

**Files:**
- Create: `tests/sql_agent_calibration/__init__.py`
- Create: `tests/sql_agent_calibration/pairs.example.yaml`
- Create: `tests/sql_agent_calibration/run_calibration.py`
- Modify: `.gitignore`

- [ ] **Step 13.1: Add real pairs.yaml to .gitignore**

Edit `.gitignore`. Add:
```
# W4.1 — real NL→SQL calibration pairs are real questions about real data.
# pairs.example.yaml (with synthetic examples) IS committed; pairs.yaml is not.
tests/sql_agent_calibration/pairs.yaml
tests/sql_agent_calibration/results-*.json
```

- [ ] **Step 13.2: Create package marker + example pairs**

Write `tests/sql_agent_calibration/__init__.py`:
```python
"""W4.1 SQL-agent calibration harness. Real pairs.yaml is gitignored
(contains real questions = real PII); pairs.example.yaml ships."""
```

Write `tests/sql_agent_calibration/pairs.example.yaml`:
```yaml
# Example pairs file demonstrating the schema. Real pairs live in
# pairs.yaml (gitignored). Copy this file, edit, run run_calibration.py.
#
# Categories to cover (per spec §5.3):
#   - Simple aggregates × 4
#   - Time-window comparisons × 3
#   - Top-N / ranking × 3
#   - Multi-table joins × 3
#   - Date math edge cases × 2
#   - Aggregation variations × 2
#   - Genuinely unanswerable × 1
#   - Negative / edge cases × 2
# Total: 20 pairs.

- id: 1
  category: simple_aggregate
  question: "How many transactions are in the database total?"
  expected_sql: |
    SELECT count(*) FROM transactions
  expected_value: null   # fill in after manual run
  notes: "Trivial sanity check — should always pass the judge."

- id: 2
  category: top_n
  question: "Top 5 merchants by total spend in March 2026."
  expected_sql: |
    SELECT raw_merchant, sum(amount) AS total
    FROM transactions
    WHERE direction = 'out'
      AND date >= '2026-03-01' AND date < '2026-04-01'
    GROUP BY raw_merchant
    ORDER BY total DESC
    LIMIT 5
  expected_value: null
  notes: ""

- id: 3
  category: unanswerable
  question: "Am I spending more than usual lately?"
  expected_sql: null
  expected_value: null
  notes: |
    Genuinely ambiguous — "usual" is undefined without a baseline period
    spec. Expected judge verdict: uncertain. Validates the rephrase path.
```

- [ ] **Step 13.3: Implement the calibration runner**

Write `tests/sql_agent_calibration/run_calibration.py`:
```python
"""W4.1 calibration harness.

Loads pairs.yaml (or a path passed via --pairs), runs each through
run_sql_agent, scores recall / precision / escalation rate, and emits
a confidence-distribution histogram (Step 5.0 gate).

Usage:
    .venv/bin/python -m tests.sql_agent_calibration.run_calibration \
        --pairs tests/sql_agent_calibration/pairs.yaml \
        --out tests/sql_agent_calibration/results-$(date +%Y%m%d).json
"""
from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from skills.finance.agents.sql_agent import AgentResult, run_sql_agent


@dataclass
class PairOutcome:
    id: int
    category: str
    question: str
    actual_sql: str
    expected_sql: str | None
    final: str
    gemini_verdict: str | None
    gemini_confidence: float | None
    escalated: bool
    retried: bool
    anthropic_verdict: str | None  # if escalated
    rows_returned: int


def _run_one(pair: dict) -> PairOutcome:
    result: AgentResult = run_sql_agent(pair["question"])
    return PairOutcome(
        id=pair["id"],
        category=pair.get("category", "uncategorized"),
        question=pair["question"],
        actual_sql=result.sql,
        expected_sql=pair.get("expected_sql"),
        final=result.final,
        gemini_verdict=result.judge_verdict.verdict if result.judge_verdict else None,
        gemini_confidence=result.judge_verdict.confidence if result.judge_verdict else None,
        escalated=result.escalated,
        retried=result.retried,
        anthropic_verdict=None,  # populated below if escalated — see note
        rows_returned=len(result.rows or []),
    )


def _bucket_confidence(values: list[float]) -> str:
    """Plain-text histogram for the confidence distribution (Step 5.0)."""
    if not values:
        return "(no confidence values)"
    bins = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    counts = [0] * (len(bins) - 1)
    for v in values:
        for i in range(len(bins) - 1):
            if bins[i] <= v < bins[i + 1]:
                counts[i] += 1
                break
        else:
            counts[-1] += 1  # value of exactly 1.0
    out_lines = []
    max_count = max(counts) if counts else 1
    for i, c in enumerate(counts):
        bar = "█" * (40 * c // max(max_count, 1))
        out_lines.append(f"  [{bins[i]:.1f}-{bins[i+1]:.1f}) {c:>3} {bar}")
    return "\n".join(out_lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pairs",
        default="tests/sql_agent_calibration/pairs.yaml",
        help="Path to pairs.yaml (gitignored). Falls back to pairs.example.yaml.",
    )
    parser.add_argument(
        "--out",
        default=f"tests/sql_agent_calibration/results-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json",
    )
    args = parser.parse_args()

    pairs_path = Path(args.pairs)
    if not pairs_path.exists():
        pairs_path = Path("tests/sql_agent_calibration/pairs.example.yaml")
        print(f"!! {args.pairs} not found; using {pairs_path}")

    with open(pairs_path) as f:
        pairs = yaml.safe_load(f) or []

    outcomes: list[PairOutcome] = []
    for pair in pairs:
        print(f"\n--- pair {pair['id']}: {pair['question'][:60]} ---")
        try:
            outcome = _run_one(pair)
        except Exception as e:  # noqa: BLE001
            print(f"   CRASHED: {type(e).__name__}: {e}")
            continue
        outcomes.append(outcome)
        print(
            f"   final={outcome.final} judge={outcome.gemini_verdict} "
            f"conf={outcome.gemini_confidence} escalated={outcome.escalated} "
            f"retried={outcome.retried} rows={outcome.rows_returned}"
        )

    # Step 5.0: distribution plot
    confidences = [o.gemini_confidence for o in outcomes if o.gemini_confidence is not None]
    print("\n=== Step 5.0 — Gemini confidence distribution ===")
    print(_bucket_confidence(confidences))
    if confidences:
        print(
            f"  mean={statistics.mean(confidences):.3f}  "
            f"stdev={statistics.stdev(confidences) if len(confidences) > 1 else 0:.3f}  "
            f"min={min(confidences):.3f}  max={max(confidences):.3f}"
        )

    # Metrics
    n_total = len(outcomes)
    n_escalated = sum(1 for o in outcomes if o.escalated)
    escalation_rate = n_escalated / n_total if n_total else 0.0
    n_surfaced = sum(1 for o in outcomes if o.final == "surfaced_to_user")
    n_rendered = sum(1 for o in outcomes if o.final == "rendered")

    print("\n=== Metrics ===")
    print(f"  total pairs:     {n_total}")
    print(f"  rendered:        {n_rendered}")
    print(f"  surfaced:        {n_surfaced}")
    print(f"  escalation rate: {escalation_rate:.1%}  (target ≤ 20%, reject ≥ 40%)")
    print(
        "  judge recall on wrong SQL: REQUIRES MANUAL groq_sql_correct labelling "
        "in pairs.yaml. Spec §5.4."
    )

    print("\n=== Ship gate ===")
    if escalation_rate >= 0.40:
        print(f"  BLOCKED — escalation rate {escalation_rate:.1%} >= 40%")
    elif escalation_rate > 0.20:
        print(f"  WARN — escalation rate {escalation_rate:.1%} above target 20%")
    else:
        print(f"  Escalation rate OK ({escalation_rate:.1%})")

    out_path = Path(args.out)
    out_path.write_text(json.dumps([asdict(o) for o in outcomes], indent=2, default=str))
    print(f"\nResults written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 13.4: Smoke the runner against pairs.example.yaml**

Run:
```
.venv/bin/python -m tests.sql_agent_calibration.run_calibration --pairs tests/sql_agent_calibration/pairs.example.yaml 2>&1 | tail -40
```

Expected: runs 3 example pairs, prints judge verdicts, prints distribution histogram and metrics. Will spend ~$0.005 on Anthropic (the unanswerable pair likely escalates). **If this crashes or hangs, triage before proceeding — calibration is the ship gate.**

- [ ] **Step 13.5: Commit**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add tests/sql_agent_calibration/__init__.py tests/sql_agent_calibration/pairs.example.yaml tests/sql_agent_calibration/run_calibration.py .gitignore && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "feat(calibration): W4.1 SQL-agent calibration harness (§5)

run_calibration.py loads pairs.yaml (gitignored — real PII) or
falls back to pairs.example.yaml, runs each through run_sql_agent,
emits Gemini-confidence distribution histogram (Step 5.0 gate),
escalation/render/surface counts, JSON dump. Manual groq_sql_correct
labels in pairs.yaml drive recall/precision (Rajat fills these in).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: Documentation + memory + final triple-gate

**Files:**
- Modify: `tasks/lessons.md`
- Modify: `tasks/preconditions-notes.md` (W4.1 section, already started in Task 0)
- Modify: memory `project_pfa_status.md` (after pairs.yaml run gates the ship in Task 15)

This task lands the documentation pieces. The memory update + push happen in Task 15 after the calibration gate passes.

- [ ] **Step 14.1: Append lesson to tasks/lessons.md**

Append to `tasks/lessons.md`:
```markdown

## 2026-05-19 — Routing decision: zero-Anthropic-spend with reviewer layer (W4.1)

**Pattern:** PRD §6.4 chose Anthropic Sonnet as primary for `stakes:high` reasoning. V1 budget reality is hard zero-Anthropic-spend on top of the existing ~$5 balance. The naive moves are (a) silently flip the yaml to Gemini/Groq and pretend the PRD didn't say what it said, or (b) leave the yaml as-is and let calls fail loudly when the balance runs out.

**What W4.1 did instead:** flipped sql_agent + affordability_reasoning primaries to free-tier Groq Llama 3.3 70B with Sonnet as fallback, AND built a tiered reviewer layer (Gemini Flash judge → optional Sonnet escalation → critique-driven retry → optional Sonnet last-resort SQL gen). The reviewer layer is the quality buy-back; the calibration harness (§5) is the ship gate.

**Rule:** When PRD calls for high-quality primary and reality calls for zero-spend, accept the cost-vs-quality trade *deliberately and visibly*. Document the deviation (this lesson + spec §6), build the mitigation (reviewer layer), gate the mitigation (calibration), and set explicit revisit triggers (§7) so the decision doesn't silently rot. Don't silently substitute weaker primaries; don't leave broken routing as a future surprise.

**Captured as:** spec `2026-04-30-llm-routing-anthropic-zero-spend.md`; this entry; CLAUDE.md invariants #8 (W3.5 readonly client, the SQL-agent data path) and #12 (prompt-body sensitivity); `config/sql_agent_review.yaml` (tunable thresholds); calibration harness in `tests/sql_agent_calibration/`.
```

- [ ] **Step 14.2: Run final triple-gate**

Run:
```
.venv/bin/python -m pytest -q 2>&1 | tail -8 && echo "---" && .venv/bin/ruff check . 2>&1 | tail -3 && echo "---" && .venv/bin/mypy skills scripts app.py 2>&1 | tail -3
```

Expected: all green. If any failure, triage before Task 15.

- [ ] **Step 14.3: Commit docs**

Run:
```
PATH="$(pwd)/.venv/bin:$PATH" git add tasks/lessons.md && PATH="$(pwd)/.venv/bin:$PATH" git commit -m "docs(lessons): W4.1 routing-deviation lesson — cost-vs-quality trade

Captures the V1 zero-Anthropic-spend constraint, the reviewer-layer
mitigation, the calibration-as-ship-gate discipline, and the revisit
triggers. Pairs with CLAUDE.md invariants #8 and #12, the routing
spec, and the calibration harness.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 15: Calibration RUN + ship gate (USER ACTION)

**Files:** `tests/sql_agent_calibration/pairs.yaml` (Rajat authors; gitignored).

**This task is Rajat's, not the implementing agent's.** The agent stops at Task 14 and hands the calibration off. The plan continues here for completeness — once these checkpoints pass, the agent resumes for Task 16 ship.

- [ ] **Step 15.1: Rajat writes 20 pairs in `tests/sql_agent_calibration/pairs.yaml`** spanning the §5.3 categories. Reference: `pairs.example.yaml` for schema; `config/db_schema_for_judge.md` for table reference.

- [ ] **Step 15.2: First calibration run**

Run:
```
.venv/bin/python -m tests.sql_agent_calibration.run_calibration --pairs tests/sql_agent_calibration/pairs.yaml
```

- [ ] **Step 15.3: Step 5.0 gate — review confidence distribution histogram**

Compare against the spec §5.0 table:
- **Wide spread (0.3–1.0):** keep confidence_threshold = 0.85
- **Bimodal (~0.5 + ~0.95):** set threshold at the trough (e.g. 0.75)
- **Tight cluster (0.9–0.99):** drop threshold to 0.0 — verdict-categorical carries all the load
- **Inverted (low conf on easy):** re-tune judge prompt, repeat Step 5.0

Edit `config/sql_agent_review.yaml` if threshold change is needed. Re-run calibration.

- [ ] **Step 15.4: Manual labelling — fill `expected_value` + groq_sql_correct flag in pairs.yaml**

For each pair: run the actual_sql against the readonly DB manually, decide if Groq's output semantically matches expected_sql, set the boolean in pairs.yaml.

- [ ] **Step 15.5: Score the metrics gate (§5.4)**

| Metric | Ship threshold |
|---|---|
| Judge recall on wrong SQL | ≥ 80% |
| Judge precision on right SQL (1 - false-positive rate) | ≥ 80% |
| Escalation rate | ≤ 20% (warn 20-40%, reject ≥ 40%) |

If metrics pass → proceed to Task 16. If FAIL → see §5.4 statistical caveat (within ±10% of threshold = expand pair set to 50; further → tune judge prompt + retest).

---

## Task 16: Ship — push, update memory, declare W4.1 done

**Files:**
- Modify: memory `project_pfa_status.md`
- Modify: memory `MEMORY.md` index

After Task 15 gate passes, the agent resumes.

- [ ] **Step 16.1: Push all commits to origin**

Run:
```
git push origin main
```

- [ ] **Step 16.2: Update memory project_pfa_status.md**

Edit `/Users/rajat/.claude/projects/-Users-rajat-AntiGravity-Personal-finance-Agent/memory/project_pfa_status.md`. Flip name + description to mark W4.1 done; update the body block to reflect: yaml flipped, reviewer layer shipped, calibration metrics achieved, /ask command live, balance alert wired.

- [ ] **Step 16.3: Update MEMORY.md index**

Update the corresponding line to reflect "W3.1–W4.1 done".

- [ ] **Step 16.4: Final verification**

Run:
```
.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/mypy skills scripts app.py
```

Expected: all green.

W4.1 complete.

---

## Self-Review

**Spec coverage:** Walked each section of `2026-04-30-llm-routing-anthropic-zero-spend.md`:

| Spec section | Task that covers it |
|---|---|
| §1 Constraint (zero spend) | Tasks 2 (yaml flip), 12 (balance alert), 14 (lesson) |
| §2 Decisions D1–D6 | D1 → Task 2; D2 dropped (per v3 amendment, ICICI Savings supersedes); D3 → Tasks 6-9; D4 → Tasks 13, 15; D5 → Task 14; D6 → Task 12 |
| §3 Yaml diff | Task 2 |
| §4.1 Architecture (full state machine) | Tasks 7 (happy), 8 (escalation), 9 (retry + strict) |
| §4.2 Escalation path | Task 8 |
| §4.3 Retry path + strict | Task 9 |
| §4.4 Decision rule + thresholds | Tasks 3 (config loader), 9 (state machine), 15 (tune) |
| §4.5 Judge prompt + sensitivity | Tasks 5 (schema excerpt), 6 (prompt builder), 10 (logging invariant) |
| §5.0 Distribution plot gate | Task 13.3 (histogram in runner) + 15.3 |
| §5.1–5.6 Calibration set | Tasks 13 (harness), 15 (run + gate) |
| §6 PRD deviation note | Task 14 (lesson) |
| §7 Revisit triggers | Task 12 (balance alert is the active one) |
| §8 Implementation surface | Whole plan |

**Placeholder scan:** Searched for "TBD", "TODO", "fill in", "appropriate", "etc." — none in active steps. The example pairs YAML deliberately uses `null` for `expected_value` (a real placeholder for Rajat to fill in pair-by-pair, not a plan-failure placeholder). The "Step 11.1: Find the current bot/main.py structure" is exploration, not unspec'd — it tells the agent what to grep and what to do with the output.

**Type consistency:** 
- `AgentResult` dataclass (Task 7) used consistently in Tasks 8, 9, 11 (bot), 13 (calibration runner).
- `ValidationResult` (Task 1) used in `sql_agent.py` (Task 7) and `AgentResult` (Task 7+).
- `JudgeVerdict` (Task 6) used in `sql_agent.py` and `AgentResult`.
- `ReviewConfig` (Task 3) used in `sql_agent.py` (Task 7) and `alerts.py` (Task 12).
- `FinalOutcome` literal: `"rendered"` and `"validator_rejected"` introduced Task 7, expanded with `"surfaced_to_user"` in Task 9. Consistent across renderers in Task 11.
- `run_sql_agent` is sync (Task 7); `run_sql_agent_async` wraps it in `asyncio.to_thread` (Task 11). Bot tests mock `run_sql_agent_async` directly.
- `llm()` extended with `response_format` (Task 4) — used by Tasks 7, 8, 9 (judges always pass `{"type":"json_object"}`).

**Identified risk to flag at execution time:** Task 11 (bot handler) assumes the existing bot/main.py uses `aiogram.filters.Command` + `aiogram.Router`. Step 11.1's grep verifies before adding. If the project's pattern is different (e.g. older aiogram dispatcher style), Step 11.4's code template needs minor adjustment. Not a plan failure — the explicit verify-first step handles it.

---

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-05-19-w4-1-sql-agent-reviewer.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task (Tasks 0–14), review between each, fast iteration. Best for this plan given 14 implementing tasks with clear interfaces and TDD discipline. Tasks 15 (calibration RUN) is yours; Task 16 (ship) is the agent's after your gate.

2. **Inline Execution** — Execute tasks in this session with checkpoints after each. Slower but full visibility into every step's output.

Which approach?
