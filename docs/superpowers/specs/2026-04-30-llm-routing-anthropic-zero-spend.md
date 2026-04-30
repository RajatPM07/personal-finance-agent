# LLM Routing — Anthropic Zero-Spend Strategy + SQL-Agent Reviewer Layer

**Status:** Locked v1
**Date:** 2026-04-30
**Supersedes:**
- Roadmap r2 §2 W3.3 ("Anthropic balance top-up — manual gating task before W3.4 Payslip parser") — replaced by the routing approach in §3 of this doc; no top-up required.
- `config/model_routing.yaml` comments calling Groq Llama the "documented-degraded fallback" — Groq Llama is now the primary; Sonnet fallback stays as last-resort.

**PRD references:** §6.4 (model routing — Sonnet for stakes:high reasoning), §6.5 (data-quality maturity guard for affordability)
**Impacts (when implemented):** W3.4 Payslip parser routing entry, W4.1 SQL agent design, W4.4 Affordability reasoning routing (W4.4 itself is data-gated backlog).

## 1. Constraint

**Hard rule:** zero new spend on Anthropic. The existing ~₹420 / $5 balance is the entire LLM-paid budget for V1, indefinitely. Free-tier providers (Gemini, Groq) carry the routine load.

**Rationale:** user-stated cost ceiling. Top-up to "≥$50" originally specced in Roadmap r2 §2 W3.3 is rejected; smaller top-ups also rejected for now.

## 2. Decisions

| # | Decision |
|---|---|
| D1 | Flip `sql_agent` and `affordability_reasoning` primary from Anthropic Sonnet → Groq Llama 3.3 70B. Sonnet moves to the fallback slot (last-resort). |
| D2 | Add new `payslip_extraction` task to `model_routing.yaml`, primary = Gemini 2.5 Flash (vision + structured output, free), fallback = Groq Llama. No Anthropic involvement. |
| D3 | Build a tiered reviewer layer for `sql_agent` only (B2 from brainstorm): Gemini Flash judges every SQL; if its confidence < threshold, escalate to Anthropic Sonnet for judgment. |
| D4 | Calibrate the judge before shipping: 20 hand-written NL→SQL pairs run through the pipeline, measure judge precision/recall, ship only if ≥ 80% recall on wrong-SQL detection AND ≤ 20% false-positive rate. |
| D5 | Document this as a deliberate V1 cost-vs-quality trade against PRD §6.4 (which chose Sonnet for stakes:high). Future review will know it was intentional. |
| D6 | Set explicit revisit triggers (§8) so the routing decision doesn't silently rot. |

## 3. Updated `config/model_routing.yaml`

Two existing entries change, one new entry added. Diff:

```yaml
# CHANGED — sql_agent: primary flipped, fallback flipped
sql_agent:
  model: groq/llama-3.3-70b-versatile        # was: anthropic/claude-sonnet-4-6
  fallbacks: [anthropic/claude-sonnet-4-6]    # was: [groq/llama-3.3-70b-versatile]
  stakes: high
  # 2026-04-30: primary flipped to free-tier Groq under zero-spend constraint.
  # Sonnet fallback fires only on Groq unavailability OR when the reviewer-layer
  # judge escalates a low-confidence verdict (see §4).
  # Tracked degradation: complex multi-CTE SQL is weaker on Llama 3.3 70B than
  # Sonnet. Reviewer layer + /retry mitigate.

# CHANGED — affordability_reasoning: same flip
affordability_reasoning:
  model: groq/llama-3.3-70b-versatile        # was: anthropic/claude-sonnet-4-6
  fallbacks: [anthropic/claude-sonnet-4-6]    # was: [groq/llama-3.3-70b-versatile]
  stakes: high

# NEW — payslip_extraction (W3.4)
payslip_extraction:
  model: gemini/gemini-2.5-flash             # vision + structured output, free tier
  fallbacks: [groq/llama-3.3-70b-versatile]
  stakes: medium
  # 2026-04-30: deliberately Gemini-primary not Anthropic. Payslip is a
  # bounded-extraction task; Gemini Flash handles structured output cleanly.

# NEW — sql_agent_judge (used by reviewer layer §4)
sql_agent_judge:
  model: gemini/gemini-2.5-flash
  fallbacks: [anthropic/claude-sonnet-4-6]    # escalation to Sonnet when Gemini judge is uncertain
  stakes: medium

# NEW — sql_agent_judge_strict (escalation-only judge path; called explicitly when needed)
sql_agent_judge_strict:
  model: anthropic/claude-sonnet-4-6
  fallbacks: []                                # no fallback — if Sonnet fails, reviewer-layer surfaces "uncertain" to user
  stakes: high

# NEW — sql_agent_strict (last-resort SQL generation; called by retry path §4.3)
sql_agent_strict:
  model: anthropic/claude-sonnet-4-6
  fallbacks: []                                # if Sonnet fails here, retry path surfaces "rephrase?" to user
  stakes: high
  # Used only when Groq's first attempt + critique-retry both fail the judge.
  # Distinct from sql_agent.fallbacks (which fires on Groq unavailability) so
  # cost is observable and intentional, not implicit.
```

`sql_agent_judge` and `sql_agent_judge_strict` are split intentionally so the reviewer layer can call the strict (Anthropic) judge directly on escalation rather than relying on LiteLLM's automatic-fallback semantics. Cleaner control flow + observable cost.

## 4. SQL-Agent Reviewer Layer (B2 — tiered judge)

### 4.1 Architecture

```
NL question
    │
    ▼
[llm("sql_agent", prompt)]   ← Groq Llama 3.3 70B primary (free)
    │
    ▼
SQL output
    │
    ▼
[Static SQL validator]        ← sqlglot parse; reject non-SELECT, off-allowlist tables
    │  (defense-in-depth — readonly DB role enforces at SQL layer too)
    ▼
Run on readonly DB (psycopg + SUPABASE_READONLY_PASSWORD per W3.5)
    │
    ▼
First N=3 result rows
    │
    ▼
[llm("sql_agent_judge", judge_prompt)]   ← Gemini Flash judge (free)
    │  Returns: { confident: bool, verdict: "ok"|"wrong"|"uncertain", reason: str }
    │
    ├── confident && verdict=="ok" ────────────────► render full result to user
    │
    ├── verdict=="wrong" (any confidence) ─────────► retry path (§4.3)
    │
    └── verdict=="uncertain" OR low-confidence ok ─► escalation path (§4.2)
```

### 4.2 Escalation path — Anthropic Sonnet strict judge

Triggered when Gemini Flash returns low confidence or "uncertain":

```
Same SQL + question + result rows
    │
    ▼
[llm("sql_agent_judge_strict", judge_prompt)]   ← Anthropic Sonnet (PAID, ~$0.01)
    │
    ├── verdict == "ok" ─────────► render full result
    └── verdict == "wrong" ──────► retry path (§4.3)
```

This is the only spend path. ~10–20% of queries expected to escalate based on calibration target.

### 4.3 Retry path — when judge says SQL is wrong

```
Original NL question + Groq's SQL + judge's critique
    │
    ▼
[llm("sql_agent", retry_prompt)]   ← Groq again, with critique (free)
    │  Prompt: "Your previous SQL <X> was rejected because <judge_reason>.
    │           Generate a corrected version."
    ▼
New SQL → re-validate → re-run → re-judge (§4.1 from top)
    │
    ├── now passes ──────────► render full result
    │
    └── still wrong ─────────► last-resort: llm("sql_agent_strict")
                                — a SEPARATE task entry that maps directly to
                                Sonnet (NOT via sql_agent's automatic fallback;
                                that fallback only fires on Groq unavailability,
                                not on judge rejection). If Sonnet's SQL also
                                fails the judge, surface the question to the
                                user as "I'm not sure about this one — can you
                                rephrase?"

                                Cost: this branch is the only routine driver
                                of Anthropic spend after escalation judging.
                                Calibration target keeps this rare (< 5% of
                                queries reach this branch).
```

Rate-limit: max 2 retry rounds before surfacing to user. Prevents infinite loops on inherently-ambiguous questions.

### 4.4 Confidence thresholds

| Threshold | Default | Rationale |
|---|---|---|
| Gemini judge confidence required to skip Anthropic escalation | **0.85** | Calibrate on the 20-pair test set; tune up/down based on observed precision |
| Max retry rounds | **2** | Beyond 2, the question is the problem, not the SQL |
| Anthropic balance "low" warning | **$2.00** | Sends a Telegram alert when balance drops below; user revisits routing |

These live in `config/sql_agent_review.yaml` (new file) so tuning doesn't require a code commit.

### 4.5 Judge prompt structure (skeleton)

```
SYSTEM:
  You are a SQL reviewer. Given a natural language question, the SQL that was
  generated to answer it, and the first 3 rows of the result, decide whether
  the SQL faithfully answers the question. Be strict. If the SQL touches the
  wrong tables, applies the wrong filter, or returns the wrong aggregation
  shape, mark it WRONG. If you can't tell from the question and rows whether
  the SQL is right, mark it UNCERTAIN.

USER:
  Schema (relevant tables): {schema_excerpt}
  Question: "{nl_question}"
  Generated SQL: ```sql
  {sql}
  ```
  First 3 result rows: {result_preview}

  Output JSON: { "verdict": "ok" | "wrong" | "uncertain", "confidence": 0.0–1.0, "reason": "<one sentence>" }
```

Same prompt for Gemini and Sonnet judges. The prompt itself is part of the calibration sweep — if Gemini misjudges systematically, the prompt is the first thing to tune.

## 5. Calibration test set

Before the reviewer layer ships in W4.1, build a small dataset:

### 5.1 Layout

```
tests/sql_agent_calibration/
├── pairs.yaml                   # 20 NL→SQL pairs (gitignored — uses real schema)
└── run_calibration.py            # Runs the pipeline, scores precision/recall
```

### 5.2 Pair schema

```yaml
# tests/sql_agent_calibration/pairs.yaml (gitignored)
- id: 1
  question: "How much did I spend at restaurants last month?"
  expected_sql: |
    SELECT SUM(amount)
    FROM transactions
    WHERE direction = 'out'
      AND raw_merchant ILIKE '%swiggy%' OR raw_merchant ILIKE '%zomato%' OR ...
      AND date >= date_trunc('month', current_date) - interval '1 month'
      AND date <  date_trunc('month', current_date)
  expected_value: <decimal — fill in after manual run>
  notes: ""

- id: 2
  question: "What's my net cash flow in March 2026?"
  ...
```

### 5.3 Categories to cover

The 20 pairs should span:
- **Simple aggregates** (5): "spend on X last month", "total income in Y"
- **Time-window comparisons** (3): "spend last week vs week before"
- **Top-N / ranking** (3): "top 10 merchants by amount"
- **Multi-table joins** (3): "spend by category for credit card vs UPI"
- **Date math edge cases** (2): "this month so far", "year-to-date"
- **Aggregation variations** (2): "average daily spend", "median transaction"
- **Ambiguous / hard** (2): "am I spending more than usual?" (open-ended; expect some `uncertain` verdicts)

### 5.4 Scoring

Run each pair through the full pipeline. For each, log:

| Field | Source |
|---|---|
| `groq_sql_correct` | Manual judgment: does Groq's SQL match `expected_sql` semantically? |
| `gemini_judge_verdict` | Output of Gemini judge |
| `escalated_to_anthropic` | True iff Gemini was low-confidence/uncertain |
| `anthropic_judge_verdict` | Output of Sonnet judge (if escalated) |
| `final_outcome` | "rendered" / "retried" / "surfaced_to_user" |

Compute:

| Metric | Definition | Ship threshold |
|---|---|---|
| **Judge recall on wrong SQL** | Of pairs where Groq's SQL is actually wrong, what % did the judge (Gemini OR escalated Sonnet) catch? | ≥ 80% |
| **Judge precision on right SQL** | Of pairs where the judge said "wrong", what % were actually wrong? (1 - false-positive rate) | ≥ 80% (≤ 20% false-positive rate) |
| **Escalation rate** | What % of queries triggered Anthropic? | Target ≤ 20%, reject ≥ 40% (too costly) |

### 5.5 Calibration cost

20 pairs × (1 Groq call + 1 Gemini judge + 0.2 Anthropic escalations average) ≈ **$0.04 total**. Trivial.

### 5.6 Frequency

Calibration runs:
- **Once before W4.1 ships** (gating decision)
- **Once a quarter** thereafter, OR whenever the schema changes materially

Not on every commit (it costs real money + takes ~2 minutes wall clock for 20 LLM calls).

## 6. PRD §6.4 deviation note

PRD §6.4 explicitly chose Anthropic Sonnet as primary for `stakes:high` reasoning tasks (sql_agent, affordability_reasoning) on quality grounds. **This spec deliberately deviates** from that choice for V1 due to the zero-spend constraint.

The deviation is bounded by:
1. Reviewer layer (§4) catches the cases where Llama's quality matters most.
2. Anthropic remains in the fallback slot — quality is recoverable on the rare cases that need it.
3. Revisit triggers (§8) ensure this doesn't silently degrade.

When V2 onboarding (Ayushi) or any budget change happens, the routing flip in §3 is reversible in two yaml lines. **No code changes required to revert** — only config.

## 7. Revisit triggers

If any of these fire, reopen the routing decision:

| Trigger | Threshold | Action |
|---|---|---|
| Anthropic balance low | < $2.00 | Telegram alert; user picks: top-up, drop strict-judge escalation, or accept Gemini-only judging |
| Wrong-SQL rate (per `/retry` invocations) | > 20% over 2-week sample | Reopen routing; consider promoting Sonnet back to primary or improving prompts |
| Judge false-positive rate (user marks judged-wrong as actually right) | > 20% over 30 queries | Tune confidence threshold or judge prompt; recalibrate |
| Calibration regression | Recall drops below 70% on the test set | Block deploy until prompts retuned |

All triggers should write a `request_logs` row with reason for traceability (W4.1 implementation).

## 8. Implementation surface (what changes when)

This spec lands without code changes. It's the **decision artifact**. Implementation happens in two places:

### When W3.4 Payslip parser ships:
- Add `payslip_extraction` entry to `config/model_routing.yaml` per §3.
- No reviewer layer needed (extraction task, not reasoning task).

### When W4.1 SQL agent ships:
- Apply the §3 yaml diff (sql_agent flip + judge entries).
- Build the reviewer layer (§4) as part of `skills/finance/agents/sql_agent.py`.
- Build calibration harness `tests/sql_agent_calibration/` and run it before ship.
- Add `config/sql_agent_review.yaml` for tunable thresholds.
- Telegram low-balance alert (§7) wired into the existing `monitoring/alerts.py`.

### Standalone now:
- Update `config/model_routing.yaml` comments to mark the W4 flip as scheduled.
- Append a new entry in `tasks/lessons.md` capturing the cost-vs-quality V1 trade.
- Update Roadmap r2 status (W3.3 entry now references this spec instead of "manual top-up").

## 9. References

- `docs/superpowers/specs/2026-04-26-v1-roadmap-r2-reprioritization.md` (this spec supersedes its W3.3 entry)
- `config/model_routing.yaml` (current — to be updated when W3.4 / W4.1 ship)
- `skills/finance/lib/llm.py` (LiteLLM wrapper — no changes needed; routing change is config-only)
- PRD.md §6.4 (model routing rationale, original Sonnet-primary choice)
- PRD.md §6.5 (data-quality maturity guard — the affordability complement to this spec's reviewer layer for sql_agent)
- `tasks/lessons.md` 2026-04-21 (CLAUDE.md invariant 11 — verify before pinning model IDs; the calibration test set in §5 is the operational form of this principle for prompts)
- Brainstorm conversation 2026-04-30 (B2 ratified)
