"""Async alert dispatcher for the Personal Finance Agent.

Alerts use the SECONDARY Telegram bot (separate from the main user-facing bot)
so that if the main bot itself is the failing component we still hear about it.
See PRD §18.7 — "alert channel = secondary Telegram bot, independent of the
main one."

Contract:
- `send_alert(text)` is `async` and runs on the same event loop as the rest of
  the system. APScheduler's AsyncIOScheduler awaits it directly; do not add a
  sync shim that calls `asyncio.run()` inside (that crashes when the loop is
  already running).
- `send_alert(text)` MUST NEVER raise. Failures are logged via
  `logger.exception` and swallowed — alerting is best-effort and should not
  propagate exceptions back into a scheduler job or handler that called it.
"""
from __future__ import annotations

import logging
from typing import Any, cast

from aiogram import Bot

from skills.finance.agents.review_config import load_review_config
from skills.finance.lib.db import service_client
from skills.finance.lib.settings import settings

logger = logging.getLogger(__name__)
_alert_bot = Bot(token=settings.telegram_alert_bot_token)


async def send_alert(text: str) -> None:
    """Send an alert via the secondary Telegram bot. Never raises."""
    try:
        await _alert_bot.send_message(chat_id=settings.telegram_alert_chat_id, text=f"⚠️ {text}")
    except Exception as e:
        logger.exception("alert dispatch failed: %s", e)


# --- W4.1 Anthropic balance check -----------------------------------------


def _fetch_anthropic_balance_usd() -> float:
    """Return current Anthropic balance in USD.

    Path B: derive from request_logs by subtracting cumulative spend
    from a known starting credit ($5 at the start of V1 per project memory).
    Swap to Path A (direct Anthropic API) by replacing this function body
    if/when the API path is available."""
    initial_credit_usd = 5.00  # per memory project_pfa_status.md
    # request_logs is auto-written by LiteLLM's supabase callback per W1.
    # Verified against the live table (2026-07-14): the per-call USD cost column
    # is `total_cost` (not `response_cost`), and Anthropic models are logged as
    # `claude-*` (e.g. `claude-sonnet-4-6`) — an `%anthropic%` filter matches
    # nothing and would silently report a permanent full balance.
    res = (
        service_client()
        .table("request_logs")
        .select("total_cost")
        .ilike("model", "%claude%")
        .execute()
    )
    data = cast(list[dict[str, Any]], res.data or [])
    spent = sum(
        float(r.get("total_cost") or 0.0)
        for r in data
    )
    return initial_credit_usd - spent


async def check_anthropic_balance() -> None:
    """Scheduled daily — fire a Telegram alert when balance drops below threshold."""
    cfg = load_review_config()
    try:
        balance = _fetch_anthropic_balance_usd()
    except Exception as e:  # noqa: BLE001
        logger.exception("anthropic balance check failed")
        await send_alert(
            f"Couldn't check Anthropic balance: {type(e).__name__}: {e}. "
            f"Verify the API path or request_logs availability."
        )
        return

    if balance < cfg.anthropic_balance_warning_usd:
        await send_alert(
            f"Anthropic balance ${balance:.2f} < threshold "
            f"${cfg.anthropic_balance_warning_usd:.2f}. "
            f"Decide: top-up, drop strict-judge escalation, or stay Gemini-only."
        )
    else:
        logger.info("Anthropic balance OK: $%.2f", balance)

