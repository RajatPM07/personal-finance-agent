from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject

from skills.finance.bot.document_handler import handle_document
from skills.finance.lib.db import adb, service_client
from skills.finance.lib.settings import settings

ROUTING_YAML_PATH = Path("config/model_routing.yaml")

logger = logging.getLogger(__name__)

bot = Bot(token=settings.telegram_bot_token)
dp = Dispatcher()


class BotHeartbeatMiddleware(BaseMiddleware):
    """Every successfully processed message bumps the telegram_bot heartbeat row.

    This means the heartbeat reflects actual bot health, not scheduler health.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        result = await handler(event, data)
        try:
            payload = {
                "component": "telegram_bot",
                "status": "ok",
                "last_ping": datetime.now(tz=UTC).isoformat(),
            }
            # IMPORTANT (CLAUDE.md invariant #2): chain must end at .execute().
            # A bare .insert(payload) returns a builder and silently no-ops.
            await adb(lambda: service_client().table("heartbeat").insert(payload).execute())
        except Exception as e:
            logger.warning("bot heartbeat write failed: %s", e)
        return result


dp.message.middleware(BotHeartbeatMiddleware())


def _is_rajat(message: Message) -> bool:
    return str(message.chat.id) == str(settings.telegram_chat_id_rajat)


@dp.message(Command("ping"))
async def ping(message: Message) -> None:
    if not _is_rajat(message):
        return
    await message.answer("pong")


@dp.message(F.document)
async def _document_handler(message: Message) -> None:
    await handle_document(message, bot=bot)


@dp.message(Command("model"))
async def model_list_handler(message: Message) -> None:
    """V1 minimal /model command — only `/model list` (read-only).
    Full /model family (switch, --confirm, A/B mode) deferred to Week 4."""
    if not _is_rajat(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    subcommand = parts[1].strip().lower() if len(parts) > 1 else "list"

    if subcommand != "list":
        await message.answer(
            f"`/model {subcommand}` is not yet supported. Only `/model list` is available in Week 2. "
            "Full command family lands in Week 4.",
            parse_mode="Markdown",
        )
        return

    with open(ROUTING_YAML_PATH) as f:
        yaml_text = f.read()
    await message.answer(f"```yaml\n{yaml_text}\n```", parse_mode="Markdown")
