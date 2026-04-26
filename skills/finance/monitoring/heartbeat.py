"""Internal heartbeat checks + external watchdog ping.

Both scheduler-facing jobs are `async def` so APScheduler's AsyncIOScheduler
awaits them on the main event loop. Do NOT call `asyncio.run()` inside these
functions — the loop is already running, and that crashes.

All Supabase access goes through `adb(lambda: ....execute())`:
- invariant #1: sync Supabase client must run in a worker thread.
- invariant #2: every Supabase builder chain terminates at `.execute()`.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import requests

from skills.finance.lib.db import adb, service_client
from skills.finance.lib.settings import settings
from skills.finance.monitoring.alerts import send_alert

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)
HEARTBEAT_STALE_MINUTES = 30


def find_stale_components(
    latest_rows: Iterable[dict],
    *,
    now: datetime,
    threshold_minutes: int = HEARTBEAT_STALE_MINUTES,
) -> list[str]:
    """Return component names whose `last_ping` is older than `threshold_minutes`.

    Pure function, fully unit-testable — no DB or network access.
    """
    cutoff = now - timedelta(minutes=threshold_minutes)
    stale: list[str] = []
    for r in latest_rows:
        last = datetime.fromisoformat(r["last_ping"])
        if last < cutoff:
            stale.append(r["component"])
    return stale


async def check_stale_components_job() -> None:
    """Async APScheduler job. Alerts on any component whose most-recent heartbeat is stale."""
    rows = await adb(
        lambda: service_client()
        .table("heartbeat")
        .select("component,last_ping")
        .order("last_ping", desc=True)
        .limit(200)
        .execute()
    )
    latest: dict[str, dict] = {}
    for r in rows.data:
        latest.setdefault(r["component"], r)
    stale = find_stale_components(latest.values(), now=datetime.now(tz=UTC))
    if stale:
        await send_alert(f"Stale heartbeat for: {', '.join(stale)}")


async def self_ping_telegram_bot_job(bot: Bot) -> None:
    """Verify Telegram API connectivity and refresh telegram_bot heartbeat.

    The per-message middleware in bot/main.py only fires on user activity, so
    long quiet periods would age the latest heartbeat past HEARTBEAT_STALE_MINUTES
    and trigger false-positive stale alerts. This job calls `bot.get_me()` (a
    cheap GET against api.telegram.org) every 10 minutes; success means the
    bot's connection is genuinely alive and we record `status='ok'`. Failure
    records `status='failed'` so the stale-checker still alerts on a real
    outage rather than being silenced by a stale-but-recent 'ok' row.
    """
    status = "ok"
    error_msg: str | None = None
    try:
        await bot.get_me()
    except Exception as e:  # noqa: BLE001
        status = "failed"
        error_msg = str(e)[:500]
        logger.warning("telegram bot self-ping failed: %s", e)

    payload = {
        "component": "telegram_bot",
        "status": status,
        "last_ping": datetime.now(tz=UTC).isoformat(),
        "error_msg": error_msg,
    }
    try:
        await adb(
            lambda: service_client().table("heartbeat").insert(payload).execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("self-ping heartbeat write failed: %s", e)


async def ping_external_watchdog_job() -> None:
    """Async APScheduler job. If this stops firing, healthchecks.io alerts us.

    `requests` is sync, so wrap in `asyncio.to_thread` to avoid blocking the loop.
    On failure: log only — there is no upstream alerting from a failure to
    reach healthchecks.io itself.
    """
    try:
        await asyncio.to_thread(
            lambda: requests.get(settings.healthcheck_url, timeout=10).raise_for_status()
        )
    except Exception as e:
        logger.warning("external watchdog ping failed: %s", e)
