"""Telegram document handler — receives PDF or XLSX docs, saves to inbox with
canonical filename. Folder watcher then dispatches.

Auto-renaming reduces user friction: forward an unmodified Gmail attachment;
the bot detects bank from filename or asks via inline keyboard.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from aiogram import Bot
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from skills.finance.ingestion._common import detect_bank_from_filename
from skills.finance.lib.settings import settings

logger = logging.getLogger(__name__)


def _is_rajat(message: Message) -> bool:
    # CLAUDE.md invariant #7: whitelist-only message handling.
    return str(message.chat.id) == str(settings.telegram_chat_id_rajat)


def _sanitize_stem(name: str) -> str:
    stem = Path(name).stem
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")


def _canonical_name(bank: str, original_filename: str) -> str:
    ext = Path(original_filename).suffix.lower() or ".pdf"
    return f"{bank}_{_sanitize_stem(original_filename)}{ext}"


async def handle_document(message: Message, bot: Bot) -> None:
    """Aiogram Document message handler.

    1. Whitelist check.
    2. Token-match the original filename.
    3a. Unambiguous match → save to inbox with canonical prefix; reply confirmation.
    3b. Ambiguous / no match → send inline keyboard prompting [ICICI CC] / [AMEX CC] / [Cancel].
    """
    if not _is_rajat(message):
        return

    doc = message.document
    if doc is None:
        return

    bank = detect_bank_from_filename(doc.file_name or "")

    if bank is None:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="ICICI CC", callback_data=f"pickbank:icici_cc:{doc.file_id}"),
            InlineKeyboardButton(text="AMEX CC", callback_data=f"pickbank:amex_cc:{doc.file_id}"),
            InlineKeyboardButton(text="Cancel", callback_data=f"pickbank:cancel:{doc.file_id}"),
        ]])
        await message.answer(
            text=f"Couldn't auto-detect bank from '{doc.file_name}'. Which is this?",
            reply_markup=kb,
        )
        return

    inbox = Path(settings.finance_inbox_path)
    inbox.mkdir(parents=True, exist_ok=True)
    canonical = inbox / _canonical_name(bank, doc.file_name or "unnamed.pdf")
    await bot.download(doc, destination=str(canonical))
    logger.info("saved telegram doc as %s", canonical)
    await message.answer(f"Saved as {canonical.name} — processing.")
