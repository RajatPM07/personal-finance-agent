"""W4.1 §4.5 sensitivity invariant: request logging persists ONLY metadata
(model, cost, latency, status, task, call id) to request_logs — never the
rendered prompt `messages` or the `response` body.

History: this was previously enforced by grepping lib/llm.py for the line
`litellm.success_callback = ["supabase"]`. That was false assurance — the
built-in "supabase" callback it blessed *does* persist full messages/response,
so the grep passed green while real PII (schema_excerpt + result_preview) was
written to Supabase. Logging is now done explicitly in llm() (LiteLLM's
callback lists are kept empty), and this test verifies BEHAVIOR: the row we
write carries only non-content metadata.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import litellm


def test_no_builtin_body_logging_callback_active():
    """The built-in `"supabase"` callback (which logs messages+response) must
    not be registered on either LiteLLM callback list."""
    import skills.finance.lib.llm  # noqa: F401  (registers the empty lists on import)

    assert "supabase" not in (litellm.success_callback or [])
    assert "supabase" not in (litellm.callbacks or [])


def test_metadata_row_has_no_message_or_response_bodies():
    """Drive the explicit logger with a response object carrying content and
    assert the row written to request_logs has ONLY metadata keys."""
    from skills.finance.lib import llm

    captured: dict = {}
    builder = MagicMock()
    builder.insert.side_effect = lambda payload: (captured.__setitem__("payload", payload) or builder)
    client = MagicMock()
    client.table.return_value = builder

    # A ModelResponse-like object with content that must NOT be persisted.
    resp = MagicMock()
    resp.model = "claude-sonnet-4-6"
    resp.id = "call-abc"

    with patch("skills.finance.lib.db.service_client", return_value=client), \
         patch.object(litellm, "completion_cost", return_value=0.0123):
        llm._log_request_metadata("sql_agent_judge", "fallback-model", resp, latency_s=1.5)

    payload = captured["payload"]
    client.table.assert_called_once_with("request_logs")
    assert "messages" not in payload
    assert "response" not in payload
    assert set(payload.keys()) <= {
        "model", "total_cost", "response_time", "status", "litellm_call_id",
        "additional_details",
    }
    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["total_cost"] == 0.0123
    assert payload["response_time"] == 1.5
    assert payload["litellm_call_id"] == "call-abc"
    assert payload["additional_details"] == {"task": "sql_agent_judge"}


def test_logging_failure_never_raises():
    """A DB failure while logging must be swallowed — logging can't break the
    LLM call."""
    from skills.finance.lib import llm

    resp = MagicMock()
    resp.model = "m"
    resp.id = "x"
    with patch("skills.finance.lib.db.service_client", side_effect=RuntimeError("db down")), \
         patch.object(litellm, "completion_cost", return_value=0.0):
        llm._log_request_metadata("some_task", "m", resp, latency_s=0.1)  # must not raise
