"""W4.1 SQL agent orchestrator tests. LLM calls + DB calls are mocked;
this layer is logic-only. Live integration tests live in the calibration
harness, not the unit suite."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

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


# ── Task 7: Happy path ─────────────────────────────────────────────────────

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


# ── Task 8: Escalation path ────────────────────────────────────────────────

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
    """If strict judge also rejects, fall to retry path. After retries exhaust
    and strict gen also fails, we get surfaced_to_user."""
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
            # retry 1: gen + judge=wrong
            _fake_llm_response("SELECT 1 FROM transactions"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r1"})),
            # retry 2: gen + judge=wrong
            _fake_llm_response("SELECT 2 FROM transactions"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r2"})),
            # strict gen + judge=wrong
            _fake_llm_response("SELECT 3 FROM transactions"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "still wrong"})),
        ]
        m_conn.return_value = _fake_readonly_conn([{"count": 1227}])

        result = run_sql_agent("How many transactions?")

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
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r1"})),
            _fake_llm_response("SELECT 2 FROM transactions"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r2"})),
            _fake_llm_response("SELECT 3 FROM transactions"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r3"})),
            _fake_llm_response("SELECT count(*) FROM transactions"),  # strict gen
            _fake_llm_response(json.dumps({"verdict": "ok", "confidence": 0.95, "reason": "ok"})),
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
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r1"})),
            _fake_llm_response("SELECT 2 FROM transactions"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r2"})),
            _fake_llm_response("SELECT 3 FROM transactions"),
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.9, "reason": "r3"})),
            _fake_llm_response("SELECT 4 FROM transactions"),  # strict gen
            _fake_llm_response(json.dumps({"verdict": "wrong", "confidence": 0.95, "reason": "still wrong"})),
        ]
        m_conn.return_value = _fake_readonly_conn([{"x": 1}])

        result = run_sql_agent("ambiguous question")

    assert result.final == "surfaced_to_user"
    assert "rephrase" in (result.reason or "").lower()
