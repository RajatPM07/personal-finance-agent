from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject

from skills.finance.agents.sql_agent import AgentResult, run_sql_agent
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
            f"/model {subcommand} is not yet supported. Only /model list is available in Week 2. "
            "Full command family lands in Week 4."
        )
        return

    with open(ROUTING_YAML_PATH) as f:
        yaml_text = f.read()
    await message.answer(yaml_text)


# --- W4.1 /ask handler ------------------------------------------------------


async def run_sql_agent_async(question: str) -> AgentResult:
    """Thread-pool wrapper. run_sql_agent is sync (psycopg + LiteLLM are sync);
    we hop to a worker thread so the aiogram loop is never blocked."""
    return await asyncio.to_thread(run_sql_agent, question)


def _render_rendered(result: AgentResult) -> str:
    n = len(result.rows or [])
    head = f"Answer ({n} row{'s' if n != 1 else ''}):"
    preview_lines = []
    for r in (result.rows or [])[:10]:
        preview_lines.append("  " + ", ".join(f"{k}={v}" for k, v in r.items()))
    sql_block = f"```sql\n{result.sql}\n```"
    tags = []
    if result.escalated:
        tags.append("escalated to Sonnet")
    if result.retried:
        tags.append("retried")
    tag_line = f"({', '.join(tags)})\n" if tags else ""
    return f"{tag_line}{head}\n" + "\n".join(preview_lines) + f"\n\n{sql_block}"


def _render_surface(result: AgentResult) -> str:
    return result.reason or "I couldn't answer — please rephrase."


def _render_rejected(result: AgentResult) -> str:
    return f"That question generated SQL I can't run safely ({result.reason}). Try rephrasing."


@dp.message(Command("ask"))
async def cmd_ask(message: Message) -> None:
    if not _is_rajat(message):
        return
    text = (message.text or "").strip()
    # /ask without a question
    if text == "/ask" or (text.startswith("/ask ") and not text[5:].strip()):
        await message.answer(
            "Usage: /ask <question about your finances>\n"
            "Example: /ask How much did I spend on food last month?"
        )
        return

    question = text[len("/ask "):].strip()
    try:
        result = await run_sql_agent_async(question)
    except Exception as e:  # noqa: BLE001
        logger.exception("/ask agent crashed for question: %s", question)
        await message.answer(f"Something went wrong: {type(e).__name__}. Try again or rephrase.")
        return

    if result.final == "rendered":
        await message.answer(_render_rendered(result))
    elif result.final == "surfaced_to_user":
        await message.answer(_render_surface(result))
    elif result.final == "validator_rejected":
        await message.answer(_render_rejected(result))
    else:
        await message.answer(f"Unhandled outcome: {result.final}")

