from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import litellm
import yaml

from skills.finance.lib.settings import settings

logger = logging.getLogger(__name__)

os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)
os.environ.setdefault("GEMINI_API_KEY", settings.gemini_api_key)
os.environ.setdefault("GROQ_API_KEY", settings.groq_api_key)

litellm.success_callback = ["supabase"]
os.environ.setdefault("SUPABASE_URL", settings.supabase_url)
os.environ.setdefault("SUPABASE_KEY", settings.supabase_service_key)

_ROUTING_PATH = Path(__file__).resolve().parents[3] / "config" / "model_routing.yaml"

# Manual exponential-backoff retry policy for transient rate-limit errors.
# Worst case: 1 + 2 + 4 + 8 + 16 = 31s of cumulative sleep before giving up.
# Rationale (W4.1 calibration triage): Groq free-tier RPM caps caused 429s
# during the 20-pair calibration run. LiteLLM's `fallbacks` did NOT fire on
# RateLimitError in our setup, and `num_retries` is not in the documented
# litellm.completion() signature for 1.83.x. So we wrap manually.
# Low-level resilience constants — not yaml-tunable on purpose.
_MAX_RETRIES = 5
_BACKOFF_BASE_SECONDS = 1.0


def _load_routing() -> dict:
    with open(_ROUTING_PATH) as f:
        return yaml.safe_load(f)


ROUTING = _load_routing()


def llm(
    task: str,
    prompt: str,
    system: str | None = None,
    response_format: dict | None = None,
):
    """Single entry point for all LLM calls. Routes by task name via model_routing.yaml.

    `response_format`: optional structured-output spec passed through to LiteLLM.
    Used by W4.1's reviewer-layer judges to request JSON output. Provider support
    varies (Gemini honors via response_mime_type; Anthropic via tools; Groq via
    prompt-only). LiteLLM normalises across providers.

    Retries on `litellm.exceptions.RateLimitError` with exponential backoff
    (see _MAX_RETRIES / _BACKOFF_BASE_SECONDS). All other exceptions propagate
    immediately — we never broaden the except.
    """
    if task not in ROUTING:
        raise KeyError(f"Unknown task '{task}'. Known: {list(ROUTING.keys())}")
    cfg = ROUTING[task]
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = {
        "model": cfg["model"],
        "messages": messages,
        "fallbacks": cfg.get("fallbacks", []),
        "metadata": {"task": task},
    }
    if response_format is not None:
        kwargs["response_format"] = response_format

    last_err: litellm.exceptions.RateLimitError | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            return litellm.completion(**kwargs)
        except litellm.exceptions.RateLimitError as e:
            last_err = e
            # If this was the final attempt, fall through to raise.
            if attempt == _MAX_RETRIES - 1:
                logger.error(
                    "llm rate-limit: task=%s attempt=%d/%d giving up",
                    task, attempt + 1, _MAX_RETRIES,
                )
                break
            delay = _BACKOFF_BASE_SECONDS * (2 ** attempt)
            logger.warning(
                "llm rate-limit: task=%s attempt=%d/%d sleeping=%.1fs",
                task, attempt + 1, _MAX_RETRIES, delay,
            )
            time.sleep(delay)
    # Exhausted retries — re-raise the last RateLimitError to the caller.
    assert last_err is not None  # for type-checkers; loop guarantees this
    raise last_err

