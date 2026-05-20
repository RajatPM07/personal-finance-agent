"""W4.1 SQL agent orchestrator.

Synchronous (psycopg + LiteLLM are both sync). The bot handler wraps the
entry point in asyncio.to_thread so the aiogram loop is never blocked.

Pipeline (full state machine — Tasks 7/8/9):
  question
    → llm("sql_agent") → SQL
    → validate_sql() → reject? short-circuit
    → readonly_client().execute(SQL) → rows
    → llm("sql_agent_judge", build_judge_prompt(...)) → JudgeVerdict
    → if verdict==ok AND confidence >= threshold: render
    → if verdict==uncertain OR low-confidence ok: escalate to strict judge
    → if verdict==wrong: retry with critique up to max_retry_rounds
    → last-resort: sql_agent_strict (Sonnet generates SQL)
    → if even strict fails: surface "rephrase?" to user
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psycopg

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

FinalOutcome = Literal["rendered", "validator_rejected", "surfaced_to_user"]


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


def _strip_codefence(text: str) -> str:
    """Strip markdown codefence from LLM-generated SQL."""
    sql = text.strip()
    if sql.startswith("```"):
        sql = sql.strip("`")
        if sql.startswith("sql"):
            sql = sql[3:]
        sql = sql.strip()
    return sql


def _exec_select(sql: str) -> list[dict]:
    conn = readonly_client()
    with conn.cursor() as cur:
        cur.execute(sql)
        if cur.description is None:
            return []
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
    sql = _strip_codefence(gen_resp.choices[0].message.content)

    # 2. Static validation.
    val = validate_sql(sql, ALLOWED_TABLES)
    if not val.ok:
        return AgentResult(
            final="validator_rejected", sql=sql, rows=None,
            judge_verdict=None, validator_result=val,
            escalated=False, retried=False,
            reason=val.reason,
        )

    # 3. Execute on readonly DB. DB exec failures (e.g. UUID-cast errors from
    #    a hallucinated `'your_user_id'` literal) are routed into the retry
    #    path as a synthesized wrong-verdict instead of crashing the agent.
    db_exec_failed = False
    rows: list[dict] = []
    verdict: JudgeVerdict
    try:
        rows = _exec_select(sql)
    except psycopg.Error as e:
        db_exec_failed = True
        verdict = JudgeVerdict(
            verdict="wrong",
            confidence=0.99,
            reason=f"DB execution failed: {e}",
        )

    if not db_exec_failed:
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

    # 6. Escalation path — Sonnet strict judge — for uncertain or low-confidence-ok.
    #    Skipped when the initial call hit a DB error (we know the SQL is broken,
    #    no benefit to running judges; go straight to retry).
    if not db_exec_failed and (verdict.verdict in ("uncertain",) or (verdict.verdict == "ok" and verdict.confidence < cfg.confidence_threshold)):
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
    for _ in range(cfg.max_retry_rounds):
        retry_prompt = (
            f"Your previous SQL:\n```sql\n{current_sql}\n```\n"
            f"was rejected because: {current_verdict.reason}\n\n"
            f"Original question: {question}\n\n"
            f"Schema:\n{schema}\n\n"
            f"Generate a corrected single PostgreSQL SELECT. "
            f"Output ONLY the SQL, no preamble, no markdown."
        )
        retry_resp = llm("sql_agent", retry_prompt)
        current_sql = _strip_codefence(retry_resp.choices[0].message.content)

        val = validate_sql(current_sql, ALLOWED_TABLES)
        if not val.ok:
            continue  # validator failure on retry; loop again with same critique

        try:
            current_rows = _exec_select(current_sql)
        except psycopg.Error as e:
            # DB exec failed on this retry too — feed the error as the next
            # critique and try again without burning a judge call.
            current_verdict = JudgeVerdict(
                verdict="wrong",
                confidence=0.99,
                reason=f"DB execution failed: {e}",
            )
            continue
        retry_judge_prompt = build_judge_prompt(
            question=question, sql=current_sql, result_preview=current_rows,
            schema_excerpt=schema,
        )
        retry_judge_resp = llm(
            "sql_agent_judge",
            retry_judge_prompt,
            response_format={"type": "json_object"},
        )
        current_verdict = parse_judge_response(retry_judge_resp.choices[0].message.content)
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
    strict_sql = _strip_codefence(strict_gen_resp.choices[0].message.content)

    val = validate_sql(strict_sql, ALLOWED_TABLES)
    if val.ok:
        try:
            strict_rows = _exec_select(strict_sql)
        except psycopg.Error as e:
            # Strict-gen SQL also blew up at the DB. Fall through to the
            # surface-to-user path with the DB error as the surfaced reason.
            current_verdict = JudgeVerdict(
                verdict="wrong",
                confidence=0.99,
                reason=f"DB execution failed: {e}",
            )
        else:
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
