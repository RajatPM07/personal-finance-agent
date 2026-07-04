"""Tests for send_morning_brief_job orchestration."""
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import skills.finance.nudging.morning_brief as mb

IST = ZoneInfo("Asia/Kolkata")


def _data(new_txns=()):
    return mb.BriefData(
        now_ist=datetime(2026, 7, 4, 9, 0, tzinfo=IST),
        new_txns=list(new_txns),
        mtd_total=Decimal("38400"),
        monthly_avg=Decimal("260000"),
        months_of_history=6,
        top_category=None,
        top_category_mtd=Decimal("0"),
        top_category_avg=Decimal("0"),
    )


@pytest.mark.asyncio
async def test_happy_path_sends_and_advances_watermark():
    bot = AsyncMock()
    wm = datetime(2026, 7, 3, 3, 30, tzinfo=UTC)
    with (
        patch.object(mb, "get_watermark", AsyncMock(return_value=wm)),
        patch.object(mb, "fetch_brief_data", MagicMock(return_value=_data())) as fetch,
        patch.object(mb, "set_watermark", AsyncMock()) as set_wm,
        patch.object(mb, "send_alert", AsyncMock()) as alert,
    ):
        await mb.send_morning_brief_job(bot)
    fetch.assert_called_once_with(wm)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args is not None
    text = bot.send_message.await_args.args[1] if len(bot.send_message.await_args.args) > 1 \
        else bot.send_message.await_args.kwargs["text"]
    assert text.startswith("₹ Brief")
    set_wm.assert_awaited_once()
    # watermark advanced to a time >= the old one
    assert set_wm.await_args is not None
    assert set_wm.await_args.args[0] >= wm
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_alerts_and_does_not_advance_watermark():
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("telegram down")
    with (
        patch.object(mb, "get_watermark", AsyncMock(return_value=datetime.now(tz=UTC))),
        patch.object(mb, "fetch_brief_data", MagicMock(return_value=_data())),
        patch.object(mb, "set_watermark", AsyncMock()) as set_wm,
        patch.object(mb, "send_alert", AsyncMock()) as alert,
    ):
        await mb.send_morning_brief_job(bot)  # must NOT raise
    set_wm.assert_not_awaited()
    alert.assert_awaited_once()
    assert alert.await_args is not None
    assert "Morning brief failed" in alert.await_args.args[0]


@pytest.mark.asyncio
async def test_fetch_failure_alerts():
    bot = AsyncMock()
    with (
        patch.object(mb, "get_watermark", AsyncMock(return_value=datetime.now(tz=UTC))),
        patch.object(mb, "fetch_brief_data", MagicMock(side_effect=RuntimeError("db down"))),
        patch.object(mb, "set_watermark", AsyncMock()) as set_wm,
        patch.object(mb, "send_alert", AsyncMock()) as alert,
    ):
        await mb.send_morning_brief_job(bot)
    bot.send_message.assert_not_awaited()
    set_wm.assert_not_awaited()
    alert.assert_awaited_once()


def test_app_registers_morning_brief_job():
    """_build_scheduler must register the morning_brief cron job at 09:00 IST."""
    from app import _build_scheduler
    sched = _build_scheduler()
    job = sched.get_job("morning_brief")
    assert job is not None
    trigger = str(job.trigger)
    assert "hour='9'" in trigger and "minute='0'" in trigger
