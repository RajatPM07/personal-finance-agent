"""W4.1 calibration triage: Groq free-tier 429s were crashing the
calibration runner mid-pair. `llm()` now wraps `litellm.completion()` in a
manual exponential-backoff retry that only catches
`litellm.exceptions.RateLimitError`. These tests pin that contract:

  1. Transient 429s retry transparently and the caller sees the eventual
     success.
  2. Persistent 429s exhaust _MAX_RETRIES and re-raise.
  3. Non-RateLimitError exceptions propagate immediately (no retry).
  4. Backoff delays follow the exponential schedule.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import litellm
import pytest


def _make_rate_limit_error(message: str = "429: too many requests") -> litellm.exceptions.RateLimitError:
    """Construct a RateLimitError per its 1.83.x signature."""
    return litellm.exceptions.RateLimitError(
        message=message,
        llm_provider="groq",
        model="groq/llama-3.3-70b-versatile",
    )


def _fake_completion_response(content: str = "ok"):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def test_transient_rate_limit_then_success_is_transparent_to_caller():
    """Two 429s then a successful response → caller sees the success."""
    from skills.finance.lib import llm as llm_mod

    call_count = {"n": 0}

    def side_effect(**kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise _make_rate_limit_error()
        return _fake_completion_response("final-content")

    with patch.object(llm_mod.litellm, "completion", side_effect=side_effect), \
         patch.object(llm_mod.time, "sleep") as m_sleep:
        resp = llm_mod.llm("sql_agent", "test prompt")

    assert resp.choices[0].message.content == "final-content"
    assert call_count["n"] == 3
    # Two retries → two sleeps (sleeps happen between attempts, not after the
    # final success).
    assert m_sleep.call_count == 2


def test_five_consecutive_rate_limits_exhausts_retries_and_raises():
    """All 5 attempts hit 429 → final attempt re-raises RateLimitError."""
    from skills.finance.lib import llm as llm_mod

    err = _make_rate_limit_error("persistent 429")

    with patch.object(llm_mod.litellm, "completion", side_effect=err) as m_call, \
         patch.object(llm_mod.time, "sleep") as m_sleep, pytest.raises(litellm.exceptions.RateLimitError):
        llm_mod.llm("sql_agent", "test prompt")

    assert m_call.call_count == llm_mod._MAX_RETRIES  # 5 attempts total
    # Sleeps fire between attempts: _MAX_RETRIES - 1 = 4 sleeps before giving up.
    assert m_sleep.call_count == llm_mod._MAX_RETRIES - 1


def test_authentication_error_propagates_without_retry():
    """Non-RateLimitError exceptions must surface immediately. The retry
    wrapper is intentionally narrow — broadening would hide auth/config bugs.
    """
    from skills.finance.lib import llm as llm_mod

    auth_err = litellm.exceptions.AuthenticationError(
        message="invalid api key",
        llm_provider="groq",
        model="groq/llama-3.3-70b-versatile",
    )

    with patch.object(llm_mod.litellm, "completion", side_effect=auth_err) as m_call, \
         patch.object(llm_mod.time, "sleep") as m_sleep, pytest.raises(litellm.exceptions.AuthenticationError):
        llm_mod.llm("sql_agent", "test prompt")

    assert m_call.call_count == 1  # zero retries — first try raises out
    assert m_sleep.call_count == 0


def test_generic_value_error_also_propagates_without_retry():
    """Belt-and-braces: any non-RateLimitError exception must not be caught."""
    from skills.finance.lib import llm as llm_mod

    with patch.object(llm_mod.litellm, "completion", side_effect=ValueError("boom")) as m_call, \
         patch.object(llm_mod.time, "sleep") as m_sleep, pytest.raises(ValueError):
        llm_mod.llm("sql_agent", "test prompt")

    assert m_call.call_count == 1
    assert m_sleep.call_count == 0


def test_backoff_delays_follow_exponential_schedule():
    """time.sleep must be called with 1, 2, 4, 8 s (for the first 4 retries
    of a 5-attempt run). The 5th attempt has no following sleep — we give up.
    """
    from skills.finance.lib import llm as llm_mod

    err = _make_rate_limit_error()

    with patch.object(llm_mod.litellm, "completion", side_effect=err), \
         patch.object(llm_mod.time, "sleep") as m_sleep, pytest.raises(litellm.exceptions.RateLimitError):
        llm_mod.llm("sql_agent", "test prompt")

    expected = [
        llm_mod._BACKOFF_BASE_SECONDS * (2 ** i)
        for i in range(llm_mod._MAX_RETRIES - 1)
    ]
    actual = [call.args[0] for call in m_sleep.call_args_list]
    assert actual == expected, (
        f"backoff schedule drift: expected {expected}, got {actual}"
    )
