"""W3.5 readonly client tests.

Unit tests run unconditionally. Integration tests are gated on the readonly
password being available — they hit live Supabase, so they're skipped in
CI / on machines without credentials.

Both halves of the lessons-learned coverage are here:
- Negative: writes blocked (role-GRANT enforcement is the durable boundary).
- Positive: SELECTs against seeded tables return > 0 rows (catches the silent
  RLS-filter regression class from lessons.md 2026-04-25).
"""
from __future__ import annotations

import time

import psycopg
import pytest

from skills.finance.lib.db import (
    _build_readonly_dsn,
    _project_ref_from_supabase_url,
    readonly_client,
)


def _readonly_password_available() -> bool:
    """Pydantic-settings reads .env at Settings() init; OS env may not have it.
    Check the loaded settings, not os.getenv, so live tests run when .env has
    the password (typical local dev) without needing `set -a; source .env`."""
    try:
        from skills.finance.lib.settings import settings
        return bool(settings.supabase_readonly_password)
    except Exception:
        return False


# ---- unit tests (no DB) ----------------------------------------------------


def test_project_ref_extraction_typical():
    assert (
        _project_ref_from_supabase_url("https://wqfwazmndhvoxrkatfkv.supabase.co")
        == "wqfwazmndhvoxrkatfkv"
    )


def test_project_ref_extraction_trailing_slash():
    assert (
        _project_ref_from_supabase_url("https://abc123.supabase.co/")
        == "abc123"
    )


def test_project_ref_extraction_raises_on_malformed():
    with pytest.raises(ValueError):
        _project_ref_from_supabase_url("not-a-url")
    with pytest.raises(ValueError):
        _project_ref_from_supabase_url("https://localhost")


def test_readonly_dsn_uses_role_dot_projectref_username():
    """The Supavisor-pooler username MUST be `<role>.<project_ref>` — see
    tasks/lessons.md 2026-04-25. Hardcoding just the role fails at runtime."""
    dsn = _build_readonly_dsn(
        supabase_db_url=(
            "postgresql://postgres.wqfwazmndhvoxrkatfkv:somepw"
            "@aws-1-ap-northeast-1.pooler.supabase.com:6543/postgres?sslmode=require"
        ),
        supabase_url="https://wqfwazmndhvoxrkatfkv.supabase.co",
        readonly_password="ropw",
    )
    assert "finance_agent_readonly.wqfwazmndhvoxrkatfkv:" in dsn
    assert "postgres.wqfwazmndhvoxrkatfkv:" not in dsn  # original role swapped out
    assert "@aws-1-ap-northeast-1.pooler.supabase.com:6543" in dsn  # host/port preserved
    assert "sslmode=require" in dsn  # query string preserved


def test_readonly_dsn_url_quotes_special_chars_in_password():
    """Passwords with reserved chars (e.g. `@` `:` `/`) must be percent-encoded
    so they don't break URL parsing on the Supabase side."""
    dsn = _build_readonly_dsn(
        supabase_db_url=(
            "postgresql://postgres.abc:pw@host.example:6543/postgres?sslmode=require"
        ),
        supabase_url="https://abc.supabase.co",
        readonly_password="p@ss:w/ord",
    )
    assert "p%40ss%3Aw%2Ford" in dsn
    assert "p@ss:w/ord" not in dsn.split("@host.example", 1)[0]


# ---- integration tests (live DB) -------------------------------------------

_LIVE = pytest.mark.skipif(
    not _readonly_password_available(),
    reason="SUPABASE_READONLY_PASSWORD not in settings (.env missing or empty)",
)


@pytest.fixture
def fresh_readonly_conn():
    """A non-cached connection so tests don't pollute each other's session state."""
    from skills.finance.lib.db import _build_readonly_dsn
    from skills.finance.lib.settings import settings

    dsn = _build_readonly_dsn(
        supabase_db_url=settings.supabase_db_url,
        supabase_url=settings.supabase_url,
        readonly_password=settings.supabase_readonly_password,
    )
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("SET statement_timeout = '10s'")
    conn.read_only = True
    yield conn
    conn.close()


@_LIVE
def test_live_connects_as_readonly_role(fresh_readonly_conn):
    """current_user must be finance_agent_readonly, not the substituted-out postgres."""
    with fresh_readonly_conn.cursor() as cur:
        cur.execute("SELECT current_user")
        assert cur.fetchone()[0] == "finance_agent_readonly"


@_LIVE
def test_live_statement_timeout_session_setting_is_10s(fresh_readonly_conn):
    """`SET statement_timeout` must stick — Supavisor strips `-c options` from
    connect (verified in preconditions); SET post-connect is the durable path."""
    with fresh_readonly_conn.cursor() as cur:
        cur.execute("SELECT current_setting('statement_timeout')")
        assert cur.fetchone()[0] == "10s"


@_LIVE
def test_live_positive_read_seeded_accounts(fresh_readonly_conn):
    """RLS-filter regression test: seeded `accounts` must return > 0 to readonly.
    A return of 0 here means RLS is on with no policies — see
    tasks/lessons.md 2026-04-25 'Supabase auto-enables RLS'."""
    with fresh_readonly_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM accounts")
        assert cur.fetchone()[0] > 0


@_LIVE
@pytest.mark.parametrize("write_sql", [
    "INSERT INTO transactions (user_id, date, amount, direction) "
    "VALUES (gen_random_uuid(), CURRENT_DATE, 1, 'out')",
    "UPDATE transactions SET amount = 0 WHERE id IS NOT NULL",
    "DELETE FROM transactions WHERE id IS NOT NULL",
])
def test_live_writes_blocked(fresh_readonly_conn, write_sql):
    """Role GRANTs are the durable security boundary. Either error class proves
    the write was blocked — exact class depends on whether txn-level read_only
    fires first or role-level GRANT fires first."""
    with fresh_readonly_conn.cursor() as cur, pytest.raises(
        (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)
    ):
        cur.execute(write_sql)


@_LIVE
def test_live_statement_timeout_actually_cancels(fresh_readonly_conn):
    """pg_sleep(11s) MUST be cancelled by the 10s timeout. If this hangs the
    full 11s, the SET didn't apply on this connection."""
    start = time.monotonic()
    with fresh_readonly_conn.cursor() as cur, pytest.raises(psycopg.errors.QueryCanceled):
        cur.execute("SELECT pg_sleep(11)")
    elapsed = time.monotonic() - start
    assert elapsed < 11.0, f"timeout did not fire — query took {elapsed:.1f}s"


@_LIVE
def test_live_readonly_client_factory_works():
    """The lru_cache'd factory returns a usable connection. We don't assert
    identity-across-calls strictly because pytest can interleave with other
    test files that may also import & touch the cache."""
    conn = readonly_client()
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
