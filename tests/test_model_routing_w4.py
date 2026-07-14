"""W4.1 added three new task entries to config/model_routing.yaml + flipped
two existing ones. Lock the shape with explicit tests so a yaml typo can't
break the SQL agent silently.

Zero-spend routing (2026-07): no paid Anthropic anywhere — Gemini + Groq only.
The reviewer layer still escalates across *different* models (gemini judge ->
groq strict judge) so escalation adds a genuine second opinion.
"""
from __future__ import annotations

from skills.finance.lib.llm import ROUTING


def test_sql_agent_primary_is_groq():
    assert ROUTING["sql_agent"]["model"] == "groq/llama-3.3-70b-versatile"


def test_sql_agent_fallback_is_gemini():
    assert ROUTING["sql_agent"]["fallbacks"] == ["gemini/gemini-2.5-flash"]


def test_affordability_primary_is_groq():
    assert ROUTING["affordability_reasoning"]["model"] == "groq/llama-3.3-70b-versatile"


def test_affordability_fallback_is_gemini():
    assert ROUTING["affordability_reasoning"]["fallbacks"] == ["gemini/gemini-2.5-flash"]


def test_pdf_extraction_has_no_fallback():
    cfg = ROUTING["pdf_extraction"]
    assert cfg["model"] == "gemini/gemini-2.5-flash"
    assert cfg["fallbacks"] == []


def test_sql_agent_judge_uses_gemini_with_groq_escalation():
    cfg = ROUTING["sql_agent_judge"]
    assert cfg["model"] == "gemini/gemini-2.5-flash"
    assert cfg["fallbacks"] == ["groq/llama-3.3-70b-versatile"]


def test_sql_agent_judge_strict_uses_groq_no_fallback():
    cfg = ROUTING["sql_agent_judge_strict"]
    # Groq, NOT gemini — the primary judge is gemini, so escalating to a
    # different model is what makes the strict pass a real second opinion.
    assert cfg["model"] == "groq/llama-3.3-70b-versatile"
    assert cfg["fallbacks"] == []


def test_sql_agent_strict_uses_gemini_no_fallback():
    cfg = ROUTING["sql_agent_strict"]
    assert cfg["model"] == "gemini/gemini-2.5-flash"
    assert cfg["fallbacks"] == []


def test_no_route_uses_paid_anthropic():
    """Guard the zero-spend constraint: no task's model or fallback may point at
    a paid Anthropic model."""
    for task, cfg in ROUTING.items():
        assert "anthropic" not in cfg.get("model", ""), f"{task} model uses anthropic"
        for fb in cfg.get("fallbacks", []) or []:
            assert "anthropic" not in fb, f"{task} fallback uses anthropic: {fb}"
