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

from aiogram import Bot

from skills.finance.lib.settings import settings

logger = logging.getLogger(__name__)
_alert_bot = Bot(token=settings.telegram_alert_bot_token)


async def send_alert(text: str) -> None:
    """Send an alert via the secondary Telegram bot. Never raises."""
    try:
        await _alert_bot.send_message(chat_id=settings.telegram_alert_chat_id, text=f"⚠️ {text}")
    except Exception as e:
        logger.exception("alert dispatch failed: %s", e)
