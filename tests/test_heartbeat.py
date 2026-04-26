from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_stale_components_flagged_when_exceeds_threshold():
    from skills.finance.monitoring.heartbeat import find_stale_components

    now = datetime(2026, 4, 21, 14, 0, tzinfo=UTC)
    latest = {
        "gmail_scanner": {"component": "gmail_scanner", "last_ping": (now - timedelta(minutes=10)).isoformat()},
        "morning_brief": {"component": "morning_brief", "last_ping": (now - timedelta(minutes=45)).isoformat()},
        "telegram_bot": {"component": "telegram_bot", "last_ping": (now - timedelta(minutes=5)).isoformat()},
    }
    stale = find_stale_components(latest.values(), now=now, threshold_minutes=30)
    assert stale == ["morning_brief"]


@pytest.mark.asyncio
async def test_self_ping_writes_ok_when_get_me_succeeds():
    from skills.finance.monitoring import heartbeat as hb

    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=MagicMock(id=123))
    captured: dict = {}

    def fake_client():
        client = MagicMock()
        # capture the payload that lands at .insert(...)
        client.table.return_value.insert = lambda payload: (
            captured.setdefault("payload", payload),
            MagicMock(execute=lambda: None),
        )[1]
        return client

    with patch.object(hb, "service_client", fake_client):
        await hb.self_ping_telegram_bot_job(bot)

    bot.get_me.assert_awaited_once()
    assert captured["payload"]["component"] == "telegram_bot"
    assert captured["payload"]["status"] == "ok"
    assert captured["payload"]["error_msg"] is None


@pytest.mark.asyncio
async def test_self_ping_writes_failed_when_get_me_raises():
    from skills.finance.monitoring import heartbeat as hb

    bot = MagicMock()
    bot.get_me = AsyncMock(side_effect=RuntimeError("network down"))
    captured: dict = {}

    def fake_client():
        client = MagicMock()
        client.table.return_value.insert = lambda payload: (
            captured.setdefault("payload", payload),
            MagicMock(execute=lambda: None),
        )[1]
        return client

    with patch.object(hb, "service_client", fake_client):
        await hb.self_ping_telegram_bot_job(bot)

    assert captured["payload"]["status"] == "failed"
    assert "network down" in captured["payload"]["error_msg"]
