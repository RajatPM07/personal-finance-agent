"""
Supabase client module.

RULE: the Supabase Python client is SYNCHRONOUS. Any async handler that needs
to call it MUST go through `adb()`, which runs the sync call in a thread so
the event loop is never blocked. This is the single enforcement point — don't
call `.table(...).execute()` directly from an async context; wrap it.

    # Correct — from an async aiogram handler:
    rows = await adb(lambda: service_client().table("users").select("*").execute())

    # Wrong — blocks the aiogram poll loop:
    rows = service_client().table("users").select("*").execute()

The SQL-agent readonly connection (psycopg + SUPABASE_READONLY_PASSWORD) is
deliberately NOT in this module for Week 1. It lands in Week 5 when the SQL
agent is built.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from supabase import Client, create_client

from skills.finance.lib.settings import settings


@lru_cache(maxsize=1)
def service_client() -> Client:
    """Full-write Supabase client. Used by ingestion pipeline and heartbeat writer."""
    return create_client(settings.supabase_url, settings.supabase_service_key)


async def adb(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a sync Supabase call (or any sync callable) in a worker thread.

    All async handlers MUST route Supabase access through this helper so the
    aiogram event loop is never blocked by the synchronous Supabase client.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)
