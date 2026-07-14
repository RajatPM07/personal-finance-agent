from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, TelegramObject

from skills.finance.agents.sql_agent import AgentResult, run_sql_agent
from skills.finance.bot.document_handler import handle_document, pop_pending_doc
from skills.finance.lib.db import adb, service_client
from skills.finance.lib.settings import settings
from skills.finance.lib.users import user_id_for_chat

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


def _authorized_user_id(message: Message) -> str | None:
    """Return the sender's DB user_id, or None if not whitelisted."""
    return user_id_for_chat(message.chat.id)


@dp.message(Command("ping"))
async def ping(message: Message) -> None:
    uid = _authorized_user_id(message)
    if uid is None:
        return
    await message.answer("pong")


@dp.message(F.document)
async def _document_handler(message: Message) -> None:
    uid = _authorized_user_id(message)
    if uid is None:
        return
    await handle_document(message, bot=bot)


@dp.callback_query(F.data.startswith("pickbank:"))
async def _pickbank_callback(callback: CallbackQuery) -> None:
    """Handles inline keyboard bank selection for unrecognised filenames."""
    # Whitelist gate (CLAUDE.md invariant #7): this handler downloads a file
    # into the ingestion inbox, so it must reject non-whitelisted senders.
    if callback.from_user is None or user_id_for_chat(callback.from_user.id) is None:
        return
    await callback.answer()  # dismiss the loading spinner
    data = (callback.data or "").split(":", 2)
    if len(data) != 3:
        return
    _, bank, token = data

    if bank == "cancel":
        if callback.message:
            await callback.message.edit_text("Cancelled.")
        return

    pending = pop_pending_doc(token)
    if pending is None:
        if callback.message:
            await callback.message.edit_text("Session expired — please resend the file.")
        return

    file_id, original_filename = pending

    from pathlib import Path

    from skills.finance.bot.document_handler import _canonical_name
    from skills.finance.lib.settings import settings as s

    inbox = Path(s.finance_inbox_path)
    inbox.mkdir(parents=True, exist_ok=True)
    canonical = inbox / _canonical_name(bank, original_filename)
    staging = inbox / f".dl_{canonical.stem}"  # no extension — watcher skips non-.pdf/.xlsx
    await bot.download(file_id, destination=str(staging))
    staging.rename(canonical)
    logger.info("pickbank: saved %s as %s", bank, canonical)
    if callback.message:
        await callback.message.edit_text(f"Saved as {canonical.name} — processing.")


@dp.message(Command("model"))
async def model_list_handler(message: Message) -> None:
    """V1 minimal /model command — only `/model list` (read-only).
    Full /model family (switch, --confirm, A/B mode) deferred to Week 4."""
    uid = _authorized_user_id(message)
    if uid is None:
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


async def run_sql_agent_async(question: str, user_id: str) -> AgentResult:
    """Thread-pool wrapper. run_sql_agent is sync (psycopg + LiteLLM are sync);
    we hop to a worker thread so the aiogram loop is never blocked."""
    return await asyncio.to_thread(run_sql_agent, question, user_id)


def _format_value(v: object) -> str:
    """Format a single cell value — pretty-print numbers with ₹ where appropriate."""
    from decimal import Decimal
    if isinstance(v, int | float | Decimal):
        n = float(v)
        if n == int(n):
            return f"₹{int(n):,}"
        return f"₹{n:,.2f}"
    return str(v) if v is not None else "—"


def _try_fast_format(result: AgentResult) -> str | None:
    """Return a formatted string without an LLM call for simple result shapes.

    Returns None when the result is complex enough to need the narrator.
    Handles:
      - 1 row, 1 column  → "₹X"  (single aggregation)
      - 1 row, 2-4 cols  → "key: value" list  (e.g. date + amount)
      - 2-8 rows, ≤3 cols → simple table  (top-N rankings)
    """
    rows = result.rows or []
    if not rows:
        return "No transactions found for that query."

    cols = list(rows[0].keys())

    if len(rows) == 1:
        if len(cols) == 1:
            return _format_value(rows[0][cols[0]])
        if len(cols) <= 4:
            return "\n".join(f"{k}: {_format_value(v)}" for k, v in rows[0].items())

    if 2 <= len(rows) <= 8 and len(cols) <= 3:
        lines = []
        for i, row in enumerate(rows, 1):
            parts = " | ".join(_format_value(v) for v in row.values())
            lines.append(f"{i}. {parts}")
        return "\n".join(lines)

    return None  # complex — let the narrator handle it


def _narrate_result(question: str, result: AgentResult) -> str:
    """Return a conversational reply. Uses a fast local formatter for simple
    results; falls back to an LLM narrator only for complex multi-row output."""
    fast = _try_fast_format(result)
    if fast is not None:
        return fast

    import json

    from skills.finance.lib.llm import llm as _llm
    rows_preview = json.dumps((result.rows or [])[:20], default=str)
    prompt = (
        f"The user asked: {question}\n\n"
        f"The SQL query returned these rows:\n{rows_preview}\n\n"
        "Write a concise, conversational reply (2-4 sentences max) that directly answers "
        "the question using the data. Use ₹ for amounts. No SQL, no technical details, "
        "no markdown headers. Just a natural response as if you're a personal finance assistant."
    )
    try:
        resp = _llm("sql_agent_narrator", prompt)
        return resp.choices[0].message.content.strip()
    except Exception:
        rows = result.rows or []
        lines = [", ".join(f"{k}: {_format_value(v)}" for k, v in r.items()) for r in rows[:10]]
        return "\n".join(lines) or "No data found."


def _render_surface(result: AgentResult) -> str:
    return result.reason or "I couldn't answer — please rephrase."


def _render_rejected(result: AgentResult) -> str:
    return f"That question generated SQL I can't run safely ({result.reason}). Try rephrasing."


@dp.message(Command("ask"))
async def cmd_ask(message: Message) -> None:
    uid = _authorized_user_id(message)
    if uid is None:
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
        result = await run_sql_agent_async(question, uid)
    except Exception as e:  # noqa: BLE001
        logger.exception("/ask agent crashed for question: %s", question)
        await message.answer(f"Something went wrong: {type(e).__name__}. Try again or rephrase.")
        return

    if result.final == "rendered":
        narrative = await asyncio.to_thread(_narrate_result, question, result)
        await message.answer(narrative)
    elif result.final == "surfaced_to_user":
        await message.answer(_render_surface(result))
    elif result.final == "validator_rejected":
        await message.answer(_render_rejected(result))
    else:
        await message.answer(f"Unhandled outcome: {result.final}")

