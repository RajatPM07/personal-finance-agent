"""W4.1 §4.5 sensitivity invariant: lib/llm.py's success_callback writes
ONLY metadata (token counts, model, latency, cost) to request_logs.
It does NOT persist the rendered prompt body. Extending this without an
explicit privacy review = leaking PII (schema_excerpt + result_preview).

This test guards the invariant by inspecting llm.py's source for the
callback registration pattern. It is brittle by design — any change to
how callbacks are registered should force a deliberate read of this test."""
from __future__ import annotations

from pathlib import Path


def test_llm_module_does_not_register_body_logging_callback():
    """llm.py registers `litellm.success_callback = ["supabase"]` ONLY.
    Adding a custom callable that captures `messages=` or `prompt` content
    would defeat this invariant — fail loudly if the surface changes."""
    src = Path("skills/finance/lib/llm.py").read_text()

    # The exact line we expect — if anyone re-registers callbacks differently,
    # this assertion forces a review.
    assert 'litellm.success_callback = ["supabase"]' in src, (
        "lib/llm.py changed how it registers LiteLLM callbacks. "
        "Per CLAUDE.md invariant on prompt sensitivity, any new callback "
        "MUST NOT capture `messages=` or full prompt bodies."
    )

    # And no signs of body-capturing helpers
    forbidden = ["full_prompt", "messages_to_db", "log_prompt_body", "capture_prompt"]
    for token in forbidden:
        assert token not in src, (
            f"Forbidden token {token!r} appeared in lib/llm.py — "
            f"this implies prompt-body logging, which violates spec §4.5."
        )
