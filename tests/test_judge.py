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
