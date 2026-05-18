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

`readonly_client()` (psycopg + SUPABASE_READONLY_PASSWORD via Supavisor pooler)
is the SELECT-only connection used by the W4.1 SQL agent. Role GRANTs are the
primary security boundary; transaction-level read-only mode is defense-in-depth.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import lru_cache
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
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


def _project_ref_from_supabase_url(url: str) -> str:
    """Extract the Supabase project ref (subdomain) from SUPABASE_URL.

    The pooler-side username format is `<role>.<project_ref>` — see
    tasks/lessons.md 2026-04-25 "Supavisor pooler requires `<role>.<project_ref>`".
    Single source of truth for project_ref is the SUPABASE_URL subdomain so
    that one env var rotation doesn't need a paired update elsewhere.
    """
    host = urlsplit(url).hostname
    if not host or "." not in host:
        raise ValueError(f"SUPABASE_URL has no parseable host: {url!r}")
    return host.split(".", 1)[0]


def _build_readonly_dsn(
    supabase_db_url: str, supabase_url: str, readonly_password: str
) -> str:
    """Build the readonly DSN by substituting role + password into SUPABASE_DB_URL.

    SUPABASE_DB_URL already points at the Supavisor pooler (port 6543) with the
    `<role>.<project_ref>` username convention — we keep host/port/path/query
    and swap the user (postgres → finance_agent_readonly) + password. Password
    is url-quoted in case Supabase rotated to one with reserved chars.
    """
    sp = urlsplit(supabase_db_url)
    if not sp.hostname or not sp.port:
        raise ValueError("SUPABASE_DB_URL must include host and port")
    project_ref = _project_ref_from_supabase_url(supabase_url)
    new_user = f"finance_agent_readonly.{project_ref}"
    netloc = f"{new_user}:{quote(readonly_password, safe='')}@{sp.hostname}:{sp.port}"
    return urlunsplit((sp.scheme, netloc, sp.path, sp.query, ""))


@lru_cache(maxsize=1)
def readonly_client() -> psycopg.Connection:
    """Long-lived SELECT-only psycopg3 connection through the Supavisor pooler.

    Used by the W4.1 SQL agent. Role GRANTs (finance_agent_readonly: SELECT on
    public.*, no INSERT/UPDATE/DELETE) are the primary security boundary.
    `conn.read_only = True` adds transaction-level defense for grouped queries.

    Per-connection config (set live; `-c options` does NOT survive the
    Supavisor pooler — see tasks/preconditions-notes.md W3.5):
    - autocommit=True so each .execute() is its own transaction
    - statement_timeout = 10s (PRD §11)
    - transaction-level read_only mode
    """
    dsn = _build_readonly_dsn(
        supabase_db_url=settings.supabase_db_url,
        supabase_url=settings.supabase_url,
        readonly_password=settings.supabase_readonly_password,
    )
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("SET statement_timeout = '10s'")
    conn.read_only = True
    return conn
