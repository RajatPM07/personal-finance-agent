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
