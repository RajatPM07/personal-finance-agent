"""Personal Finance Agent — top-level orchestrator.

Runs three concurrent subsystems in one Python process:
1. aiogram bot polling      (skills.finance.bot.main)
2. APScheduler AsyncIO jobs (heartbeat + external watchdog — daily backup is a
   SEPARATE launchd job, not wired here)
3. FastAPI /health endpoint (skills.finance.monitoring.health)

Graceful shutdown:
- SIGTERM / SIGINT triggers a clean shutdown: cancels the scheduler, closes
  the aiogram session, stops uvicorn. launchctl stop invokes SIGTERM.
- The launchd backup job runs in its own process, independent of this app,
  so shutdown here does not interrupt a backup in flight.

Notes on aiogram 3.27.0:
- `Dispatcher.start_polling(..., handle_signals=True)` (the default) installs
  its own SIGTERM/SIGINT handlers via `loop.add_signal_handler`, which would
  overwrite the orchestrator's `stop_event.set` handlers. We pass
  `handle_signals=False` so this module owns signal routing.
- `close_bot_session=False` because `_run_bot` closes the session itself
  exactly once after `stop_polling` returns.
"""
from __future__ import annotations

import asyncio
import logging
import signal

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from skills.finance.bot.main import bot, dp
from skills.finance.lib.logging_setup import configure_logging
from skills.finance.monitoring.health import app as fastapi_app
from skills.finance.monitoring.heartbeat import (
    check_stale_components_job,
    ping_external_watchdog_job,
)

logger = logging.getLogger("pfa.app")


def _build_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="Asia/Kolkata")
    sched.add_job(
        ping_external_watchdog_job,
        IntervalTrigger(minutes=15),
        id="external_watchdog",
    )
    sched.add_job(
        check_stale_components_job,
        IntervalTrigger(minutes=15),
        id="stale_check",
    )
    # Note: NO "telegram_bot" heartbeat writer here — the bot writes its own
    # heartbeat via middleware (skills/finance/bot/main.py). This means the
    # heartbeat reflects bot health, not scheduler health.
    # Note: daily pg_dump backup is NOT scheduled here. It runs as a separate
    # launchd job (com.rajat.pfa.backup.plist) so it survives app restarts and
    # cannot block the async event loop.
    return sched


async def _run_http(stop_event: asyncio.Event) -> None:
    config = uvicorn.Config(
        fastapi_app,
        host="127.0.0.1",
        port=8765,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await stop_event.wait()
    server.should_exit = True
    await task


async def _run_bot(stop_event: asyncio.Event) -> None:
    polling = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False, close_bot_session=False)
    )
    await stop_event.wait()
    await dp.stop_polling()
    await polling
    await bot.session.close()


async def main() -> None:
    configure_logging()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    sched = _build_scheduler()
    sched.start()
    logger.info("scheduler started; jobs=%s", [j.id for j in sched.get_jobs()])

    try:
        await asyncio.gather(_run_bot(stop_event), _run_http(stop_event))
    finally:
        logger.info("shutting down: cancelling scheduler")
        sched.shutdown(wait=False)
        logger.info("shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
