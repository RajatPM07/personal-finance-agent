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

        class FakeMessage:
            content = '{"ok": true}'
        class FakeChoice:
            message = FakeMessage()
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
