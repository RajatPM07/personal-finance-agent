from __future__ import annotations

import os
from pathlib import Path

import litellm
import yaml

from skills.finance.lib.settings import settings

os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)
os.environ.setdefault("GEMINI_API_KEY", settings.gemini_api_key)
os.environ.setdefault("GROQ_API_KEY", settings.groq_api_key)

litellm.success_callback = ["supabase"]
os.environ.setdefault("SUPABASE_URL", settings.supabase_url)
os.environ.setdefault("SUPABASE_KEY", settings.supabase_service_key)

_ROUTING_PATH = Path(__file__).resolve().parents[3] / "config" / "model_routing.yaml"


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
    return litellm.completion(**kwargs)

