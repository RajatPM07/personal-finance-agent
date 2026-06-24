"""Telegram document handler — receives PDF or XLSX docs, saves to inbox with
canonical filename. Folder watcher then dispatches.

Auto-renaming reduces user friction: forward an unmodified Gmail attachment;
the bot detects bank from filename or asks via inline keyboard.
"""
from __future__ import annotations

import logging
import re
import uuid
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

# Telegram callback_data limit is 64 bytes. file_id is ~100 chars so we can't
# embed it directly. Store pending docs keyed by a short token instead.
_pending_docs: dict[str, tuple[str, str]] = {}  # token → (file_id, original_filename)


def _is_rajat(message: Message) -> bool:
    # CLAUDE.md invariant #7: whitelist-only message handling.
    return str(message.chat.id) == str(settings.telegram_chat_id_rajat)


def _sanitize_stem(name: str) -> str:
    stem = Path(name).stem
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")


def _canonical_name(bank: str, original_filename: str) -> str:
    ext = Path(original_filename).suffix.lower() or ".pdf"
    return f"{bank}_{_sanitize_stem(original_filename)}{ext}"


def pop_pending_doc(token: str) -> tuple[str, str] | None:
    """Retrieve and remove a pending (file_id, original_filename) by its short token."""
    return _pending_docs.pop(token, None)


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
        # Store (file_id, filename) with a short token — callback_data is capped
        # at 64 bytes and file_id alone exceeds that limit.
        token = uuid.uuid4().hex[:12]
        _pending_docs[token] = (doc.file_id, doc.file_name or "unnamed")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="ICICI CC", callback_data=f"pickbank:icici_cc:{token}"),
            InlineKeyboardButton(text="ICICI Savings", callback_data=f"pickbank:icici_savings:{token}"),
            InlineKeyboardButton(text="AMEX CC", callback_data=f"pickbank:amex_cc:{token}"),
            InlineKeyboardButton(text="Paytm UPI", callback_data=f"pickbank:paytm_upi:{token}"),
            InlineKeyboardButton(text="Cancel", callback_data=f"pickbank:cancel:{token}"),
        ]])
        await message.answer(
            text=f"Couldn't auto-detect bank from '{doc.file_name}'. Which is this?",
            reply_markup=kb,
        )
        return

    inbox = Path(settings.finance_inbox_path)
    inbox.mkdir(parents=True, exist_ok=True)
    canonical = inbox / _canonical_name(bank, doc.file_name or "unnamed.pdf")
    # Download to a staging file first so the folder watcher only fires
    # on_moved (after the write is complete), not on_created mid-download.
    staging = inbox / f".dl_{canonical.stem}"  # no extension — watcher skips non-.pdf/.xlsx
    await bot.download(doc, destination=str(staging))
    staging.rename(canonical)
    logger.info("saved telegram doc as %s", canonical)
    await message.answer(f"Saved as {canonical.name} — processing.")
