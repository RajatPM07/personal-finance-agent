"""W4.1 SQL agent orchestrator tests. LLM calls + DB calls are mocked;
this layer is logic-only. Live integration tests live in the calibration
harness, not the unit suite.

Multi-user note (Task 5): the static validator now enforces a per-user
`user_id` filter, and it runs for real in these tests (only `llm` and
`readonly_client`/`_exec_select` are mocked). So every mocked SQL that is
expected to *pass* validation carries a `WHERE user_id = '<RAJAT_USER_ID>'`
predicate. `run_sql_agent` is called with `RAJAT_USER_ID` as the caller."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from skills.finance.agents.sql_agent import AgentResult, run_sql_agent
from skills.finance.lib.users import RAJAT_USER_ID

# Reusable per-user filter — keeps the mocked SQL valid under the Task-5 guard.
WU = f"WHERE user_id = '{RAJAT_USER_ID}'"


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


# ── Task 7: Happy path ─────────────────────────────────────────────────────

def test_happy_path_renders_first_attempt():
    """Groq generates valid SELECT → validator passes → DB returns rows →
    Gemini judge ok + high confidence → AgentResult.final == 'rendered'."""
    sql = f"SELECT count(*) FROM transactions {WU}"
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        m_llm.side_effect = [
            _fake_llm_response(sql),
            _fake_llm_response(json.dumps({
                "verdict": "ok", "confidence": 0.92, "reason": "matches question",
            })),
        ]
        m_conn.return_value = _fake_readonly_conn([{"count": 1227}])

        result = run_sql_agent("How many transactions total?", RAJAT_USER_ID)

    assert isinstance(result, AgentResult)
    assert result.final == "rendered"
    assert result.sql == sql
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

        result = run_sql_agent("Add a fake row", RAJAT_USER_ID)

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
                f"SELECT count(*) FROM transactions {WU} AND raw_merchant ILIKE '%cricket%'"
            ),
            _fake_llm_response(json.dumps({
                "verdict": "ok", "confidence": 0.88,
                "reason": "no rows but SQL semantics are correct",
            })),
        ]
        m_conn.return_value = _fake_readonly_conn([])

        result = run_sql_agent("How much did I spend on cricket gear?", RAJAT_USER_ID)

    assert result.final == "rendered"
    assert result.rows == []


# ── Task 8: Escalation path ────────────────────────────────────────────────

def test_low_confidence_escalates_to_strict_judge():
    """verdict=ok but confidence < threshold → escalate to sql_agent_judge_strict.
    If strict judge says ok, render.

    The committed default `confidence_threshold` is 0.0 (post-2026-05-21
    calibration §5.0 tight-cluster decision), at which value low-confidence
    never blocks. Pass an explicit non-zero ReviewConfig so this test
    exercises the still-live code path independent of the default.
    """
    from skills.finance.agents.review_config import ReviewConfig
    cfg = ReviewConfig(
        confidence_threshold=0.85,
        max_retry_rounds=2,
        anthropic_balance_warning_usd=3.0,
    )
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        m_llm.side_effect = [
            _fake_llm_response(f"SELECT count(*) FROM transactions {WU}"),
            # Gemini: ok but low confidence (below the 0.85 threshold)
            _fake_llm_response(json.dumps({
                "verdict": "ok", "confidence": 0.5, "reason": "looks ok-ish",
            })),
            # Sonnet strict judge: ok with high confidence
            _fake_llm_response(json.dumps({
                "verdict": "ok", "confidence": 0.95, "reason": "verified",
            })),
        ]
        m_conn.return_value = _fake_readonly_conn([{"count": 1227}])

        result = run_sql_agent("How many transactions total?", RAJAT_USER_ID, cfg=cfg)

    assert result.final == "rendered"
    assert result.escalated is True
    # The third LLM call should be the strict judge
    assert m_llm.call_args_list[2][0][0] == "sql_agent_judge_strict"


def test_uncertain_verdict_escalates():
    """verdict=uncertain (any confidence) → escalate."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        m_llm.side_effect = [
            _fake_llm_response(f"SELECT count(*) FROM transactions {WU}"),
            _fake_llm_response(json.dumps({
                "verdict": "uncertain", "confidence": 0.9, "reason": "ambiguous question",
            })),
            _fake_llm_response(json.dumps({
                "verdict": "ok", "confidence": 0.95, "reason": "verified",
            })),
        ]
        m_conn.return_value = _fake_readonly_conn([{"count": 1227}])

        result = run_sql_agent("How many things?", RAJAT_USER_ID)

    assert result.final == "rendered"
    assert result.escalated is True


def test_strict_judge_says_wrong_falls_to_retry():
    """If strict judge also rejects, fall to retry path. After retries exhaust
    and strict gen also fails, we get surfaced_to_user."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        m_llm.side_effect = [
            _fake_llm_response(f"SELECT count(*) FROM transactions {WU}"),
            _fake_llm_response(json.dumps({
                "verdict": "uncertain", "confidence": 0.4, "reason": "?",
            })),
            _fake_llm_response(json.dumps({
                "verdict": "wrong", "confidence": 0.9, "reason": "wrong table",
            })),
            # retry 1: gen + judge=wrong
            _fake_llm_response(f"SELECT 1 FROM transactions {WU}"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r1"})),
            # retry 2: gen + judge=wrong
            _fake_llm_response(f"SELECT 2 FROM transactions {WU}"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r2"})),
            # strict gen + judge=wrong
            _fake_llm_response(f"SELECT 3 FROM transactions {WU}"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "still wrong"})),
        ]
        m_conn.return_value = _fake_readonly_conn([{"count": 1227}])

        result = run_sql_agent("How many transactions?", RAJAT_USER_ID)

    assert result.final == "surfaced_to_user"
    # confirm we DID escalate before falling through
    called_tasks = [c[0][0] for c in m_llm.call_args_list]
    assert "sql_agent_judge_strict" in called_tasks


# ── Task 9: Retry path + strict last-resort ─────────────────────────────────

def test_judge_wrong_triggers_groq_retry_with_critique():
    """verdict=wrong on first judge → retry sql_agent with the judge's critique
    embedded in the new prompt. Retry must include the original critique text."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent.readonly_client") as m_conn:
        m_llm.side_effect = [
            _fake_llm_response(f"SELECT date FROM transactions {WU}"),
            _fake_llm_response(json.dumps({
                "verdict": "wrong", "confidence": 0.9,
                "reason": "missing aggregation",
            })),
            _fake_llm_response(f"SELECT count(*) FROM transactions {WU}"),
            _fake_llm_response(json.dumps({
                "verdict": "ok", "confidence": 0.95, "reason": "fixed",
            })),
        ]
        m_conn.return_value = _fake_readonly_conn([{"count": 1227}])

        result = run_sql_agent("How many transactions total?", RAJAT_USER_ID)

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
            _fake_llm_response(f"SELECT 1 FROM transactions {WU}"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r1"})),
            _fake_llm_response(f"SELECT 2 FROM transactions {WU}"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r2"})),
            _fake_llm_response(f"SELECT 3 FROM transactions {WU}"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r3"})),
            _fake_llm_response(f"SELECT count(*) FROM transactions {WU}"),  # strict gen
            _fake_llm_response(json.dumps({"verdict": "ok", "confidence": 0.95, "reason": "ok"})),
        ]
        m_conn.return_value = _fake_readonly_conn([{"count": 1227}])

        result = run_sql_agent("?", RAJAT_USER_ID)

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
            _fake_llm_response(f"SELECT 1 FROM transactions {WU}"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r1"})),
            _fake_llm_response(f"SELECT 2 FROM transactions {WU}"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r2"})),
            _fake_llm_response(f"SELECT 3 FROM transactions {WU}"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r3"})),
            _fake_llm_response(f"SELECT 4 FROM transactions {WU}"),  # strict gen
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.95, "reason": "still wrong"})),
        ]
        m_conn.return_value = _fake_readonly_conn([{"x": 1}])

        result = run_sql_agent("ambiguous question", RAJAT_USER_ID)

    assert result.final == "surfaced_to_user"
    assert "rephrase" in (result.reason or "").lower()


# ── W4.1 calibration triage: DB exec failures route to retry path ──────────
#
# Pre-fix: a psycopg `InvalidTextRepresentation` (e.g. from a hallucinated
# `WHERE user_id = 'your_user_id'` literal) raised straight out of
# `_exec_select`, crashed `run_sql_agent`, and the user got an opaque
# "Something went wrong" — the retry-on-wrong-verdict path was never reached.
# Post-fix: a `psycopg.Error` is synthesised into a `verdict=wrong` and routed
# into the same retry flow the judge would have triggered.
#
# Task-5 note: the SQL literals below use the *valid* caller UUID so they pass
# the static validator and reach `_exec_select` (mocked to raise). The DB-error
# message still names the old hallucinated `'your_user_id'` literal — that's what
# would have blown up at execution before the validator existed — so the
# retry-critique assertion is unchanged.

import psycopg  # noqa: E402  — at-bottom on purpose; only the new tests use it


def _uuid_cast_error() -> psycopg.Error:
    """Realistic stand-in for the bug we saw: Groq generated SQL with a
    string literal `'your_user_id'` cast to uuid; Postgres rejects at exec."""
    return psycopg.errors.InvalidTextRepresentation(
        "invalid input syntax for type uuid: 'your_user_id'"
    )


def test_initial_db_exec_failure_routes_into_retry_path_and_succeeds():
    """Initial _exec_select raises a UUID-cast error → retry path fires with
    the DB error as the critique → retry SQL succeeds → final='rendered',
    retried=True. Critically, the agent did NOT crash."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent._exec_select") as m_exec:
        m_llm.side_effect = [
            _fake_llm_response(f"SELECT count(*) FROM transactions {WU}"),
            # No initial judge call expected — DB error short-circuits it.
            # Retry round 1: corrected SQL, then judge=ok.
            _fake_llm_response(f"SELECT count(*) FROM transactions {WU} AND direction='out'"),
            _fake_llm_response(json.dumps({
                "verdict": "ok", "confidence": 0.95, "reason": "fixed",
            })),
        ]
        m_exec.side_effect = [_uuid_cast_error(), [{"count": 1227}]]

        result = run_sql_agent("How many transactions?", RAJAT_USER_ID)

    assert result.final == "rendered"
    assert result.retried is True
    assert result.rows == [{"count": 1227}]
    # The retry prompt must contain the DB error text as critique.
    retry_gen_prompt = m_llm.call_args_list[1][0][1]
    assert "DB execution failed" in retry_gen_prompt
    assert "your_user_id" in retry_gen_prompt
    # Critical: no initial judge call (we skipped straight to retry on DB error).
    called_tasks = [c[0][0] for c in m_llm.call_args_list]
    assert called_tasks == ["sql_agent", "sql_agent", "sql_agent_judge"]


def test_db_exec_failure_persists_through_all_retries_and_strict_surfaces():
    """Every _exec_select raises (initial + 2 retries + strict-gen) →
    final='surfaced_to_user'. No crash."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent._exec_select") as m_exec:
        m_llm.side_effect = [
            _fake_llm_response(f"SELECT 1 FROM transactions {WU}"),
            # Retry 1: another broken SQL (validator passes, exec raises).
            _fake_llm_response(f"SELECT 2 FROM transactions {WU}"),
            # Retry 2: same.
            _fake_llm_response(f"SELECT 3 FROM transactions {WU}"),
            # Strict gen: also broken.
            _fake_llm_response(f"SELECT 4 FROM transactions {WU}"),
        ]
        m_exec.side_effect = [
            _uuid_cast_error(),
            _uuid_cast_error(),
            _uuid_cast_error(),
            _uuid_cast_error(),
        ]

        result = run_sql_agent("query that keeps hallucinating user_id", RAJAT_USER_ID)

    assert result.final == "surfaced_to_user"
    assert "rephrase" in (result.reason or "").lower()
    # All four _exec_select invocations fired and were handled gracefully.
    assert m_exec.call_count == 4


def test_non_psycopg_exception_from_exec_propagates_to_caller():
    """We deliberately only swallow `psycopg.Error`. A `RuntimeError` (or any
    other unexpected class) must crash out so unknown bugs surface, not get
    silently swallowed as a fake wrong-verdict."""
    with patch("skills.finance.agents.sql_agent.llm") as m_llm, \
         patch("skills.finance.agents.sql_agent._exec_select") as m_exec:
        m_llm.return_value = _fake_llm_response(f"SELECT 1 FROM transactions {WU}")
        m_exec.side_effect = RuntimeError("something else broke")

        try:
            run_sql_agent("anything", RAJAT_USER_ID)
        except RuntimeError as e:
            assert "something else broke" in str(e)
        else:
            raise AssertionError("RuntimeError should have propagated")
