import asyncio
from unittest.mock import AsyncMock, MagicMock, mock_open, patch


def test_heartbeat_middleware_calls_execute():
    """Regression test for Fix 1: supabase-py v2's .insert() returns a builder.
    Without a terminating .execute(), the row is never written.
    """
    from skills.finance.bot.main import BotHeartbeatMiddleware

    execute_mock = MagicMock(return_value=MagicMock(data=[]))
    insert_mock = MagicMock(return_value=MagicMock(execute=execute_mock))
    table_mock = MagicMock(return_value=MagicMock(insert=insert_mock))
    fake_client = MagicMock(table=table_mock)

    async def fake_handler(event, data):
        return "handler_result"

    middleware = BotHeartbeatMiddleware()

    with patch("skills.finance.bot.main.service_client", return_value=fake_client):
        result = asyncio.run(middleware(fake_handler, MagicMock(), {}))

    assert result == "handler_result", "middleware must return handler result unchanged"
    table_mock.assert_called_once_with("heartbeat")
    insert_mock.assert_called_once()
    # The critical assertion — if this fails, the heartbeat is silently no-op'ing
    execute_mock.assert_called_once()


def test_model_list_command_returns_yaml():
    from skills.finance.bot.main import model_list_handler
    fake_message = MagicMock(chat=MagicMock(id=42), text="/model list")
    fake_message.answer = AsyncMock()

    fake_yaml = "pdf_extraction:\n  model: gemini/gemini-2.5-flash\n"
    # Auth now flows through _authorized_user_id (→ lib.users.settings), a
    # different import than bot.main.settings, so patch the gate seam directly.
    with patch("skills.finance.bot.main._authorized_user_id",
               return_value="00000000-0000-0000-0000-000000000001"), \
         patch("builtins.open", mock_open(read_data=fake_yaml)):
        asyncio.run(model_list_handler(fake_message))

    fake_message.answer.assert_called_once()
    args, kwargs = fake_message.answer.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "pdf_extraction" in text
    assert "gemini/gemini-2.5-flash" in text


def test_model_list_rejects_non_rajat():
    from skills.finance.bot.main import model_list_handler
    fake_message = MagicMock(chat=MagicMock(id=999), text="/model list")
    fake_message.answer = AsyncMock()
    with patch("skills.finance.bot.main.settings",
               MagicMock(telegram_chat_id_rajat="42")):
        asyncio.run(model_list_handler(fake_message))
    fake_message.answer.assert_not_called()
