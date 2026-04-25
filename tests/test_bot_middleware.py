import asyncio
from unittest.mock import MagicMock, patch


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
