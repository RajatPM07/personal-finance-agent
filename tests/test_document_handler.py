from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def test_unambiguous_icici_pdf_saves_with_canonical_prefix(tmp_path, monkeypatch):
    from skills.finance.bot.document_handler import handle_document

    inbox = tmp_path / "finance-inbox"
    inbox.mkdir()
    monkeypatch.setattr(
        "skills.finance.bot.document_handler.settings",
        MagicMock(finance_inbox_path=str(inbox), telegram_chat_id_rajat="42"),
    )

    fake_doc = MagicMock(file_name="Statement_April_2026_ICICI_CC.pdf", file_id="fake_id")
    fake_message = MagicMock(chat=MagicMock(id=42), document=fake_doc)
    fake_message.answer = AsyncMock()

    async def _fake_download(_doc, destination):
        Path(destination).write_bytes(b"pdf-bytes")

    fake_bot = MagicMock()
    fake_bot.download = AsyncMock(side_effect=_fake_download)

    asyncio.run(handle_document(fake_message, bot=fake_bot))

    fake_bot.download.assert_called_once()
    # The handler downloads to a hidden `.dl_` staging file, then renames it to
    # the canonical name — assert on the final saved file, not the staging dest.
    saved = [p for p in inbox.iterdir() if not p.name.startswith(".dl_")]
    assert len(saved) == 1
    assert saved[0].name.startswith("icici_cc_")
    assert saved[0].suffix == ".pdf"
    fake_message.answer.assert_called_once()


def test_unambiguous_amex_xlsx_saves_with_canonical_prefix(tmp_path, monkeypatch):
    from skills.finance.bot.document_handler import handle_document

    inbox = tmp_path / "finance-inbox"
    inbox.mkdir()
    monkeypatch.setattr(
        "skills.finance.bot.document_handler.settings",
        MagicMock(finance_inbox_path=str(inbox), telegram_chat_id_rajat="42"),
    )

    fake_doc = MagicMock(file_name="AMEX_April_2026.xlsx", file_id="fake_id")
    fake_message = MagicMock(chat=MagicMock(id=42), document=fake_doc)
    fake_message.answer = AsyncMock()

    async def _fake_download(_doc, destination):
        Path(destination).write_bytes(b"xlsx-bytes")

    fake_bot = MagicMock()
    fake_bot.download = AsyncMock(side_effect=_fake_download)

    asyncio.run(handle_document(fake_message, bot=fake_bot))

    fake_bot.download.assert_called_once()
    saved = [p for p in inbox.iterdir() if not p.name.startswith(".dl_")]
    assert len(saved) == 1
    assert saved[0].name.startswith("amex_cc_")
    assert saved[0].suffix == ".xlsx"


def test_ambiguous_filename_sends_inline_keyboard(tmp_path, monkeypatch):
    from skills.finance.bot.document_handler import handle_document

    inbox = tmp_path / "finance-inbox"
    inbox.mkdir()
    monkeypatch.setattr(
        "skills.finance.bot.document_handler.settings",
        MagicMock(finance_inbox_path=str(inbox), telegram_chat_id_rajat="42"),
    )

    fake_doc = MagicMock(file_name="Statement_April.pdf", file_id="fake_id")
    fake_message = MagicMock(chat=MagicMock(id=42), document=fake_doc)
    fake_message.answer = AsyncMock()

    fake_bot = MagicMock()
    fake_bot.download = AsyncMock()

    asyncio.run(handle_document(fake_message, bot=fake_bot))

    fake_bot.download.assert_not_called()
    fake_message.answer.assert_called_once()
    assert "reply_markup" in fake_message.answer.call_args.kwargs


def test_non_whitelisted_user_silently_ignored(monkeypatch):
    from skills.finance.bot.document_handler import handle_document

    monkeypatch.setattr(
        "skills.finance.bot.document_handler.settings",
        MagicMock(finance_inbox_path="/tmp", telegram_chat_id_rajat="42"),
    )

    fake_doc = MagicMock(file_name="anything.pdf", file_id="fake_id")
    fake_message = MagicMock(chat=MagicMock(id=999), document=fake_doc)
    fake_message.answer = AsyncMock()

    fake_bot = MagicMock()
    fake_bot.download = AsyncMock()

    asyncio.run(handle_document(fake_message, bot=fake_bot))

    fake_message.answer.assert_not_called()
    fake_bot.download.assert_not_called()
