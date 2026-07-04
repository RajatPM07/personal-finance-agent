# Daily Adaptive Morning Brief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily 09:00 IST Telegram message that shows new transactions since the last brief when there are any, otherwise month-to-date spend pacing vs the trailing-6-month average — never a useless "no data" ping.

**Architecture:** New `skills/finance/nudging/morning_brief.py` module with three layers: pure formatting/computation functions (unit-tested, no I/O), a sync data-fetch layer (raw SQL via the existing `readonly_client()` psycopg connection), and an async APScheduler job wired into `app.py`'s existing `_build_scheduler()`. Last-brief watermark lives in the `agent_memory` table (key `morning_brief_last_run`). Failures alert via the existing `send_alert()` secondary bot — no heartbeat-table writes (the 30-min stale threshold in `check_stale_components_job` would false-positive on a daily component).

**Tech Stack:** Python 3.11, APScheduler CronTrigger, aiogram Bot (existing instance), psycopg3 via `readonly_client()`, supabase-py via `adb()`/`service_client()` for the watermark, pytest.

## Global Constraints

- **Invariant #1 (CLAUDE.md):** async code never calls the sync Supabase client directly — always `await adb(lambda: ...)`. Sync psycopg work runs via `asyncio.to_thread`.
- **Invariant #2 (CLAUDE.md):** every Supabase builder chain ends at `.execute()`.
- No `print` in long-running paths; use module `logging`.
- No raw card/account numbers in INFO-level logs. Do NOT log the rendered brief text at INFO (it contains merchant names + amounts); log only counts and totals at INFO.
- Pacing queries exclude categories `('Self Transfer', 'Wallet Load', 'Loan Repayment')` — Self Transfer/Wallet Load are money movement not spend; Loan Repayment excluded because the May 2026 ₹8,02,873 prepayment would poison every average.
- All spend queries filter `direction = 'out'` AND `is_deleted = false`.
- Amounts render as `₹{n:,.0f}` (western grouping, whole rupees) — matches `_format_value` in `bot/main.py`.
- "Today" and month boundaries computed in `Asia/Kolkata` (`zoneinfo.ZoneInfo`), not UTC.
- Work happens on branch `feat/daily-brief` (in-place branch, NOT a git worktree — the repo's `.venv` hard-codes this path; see tasks/lessons.md 2026-04-26).
- Before claiming any task done: `make test && make lint && make typecheck` all pass.
- `RAJAT_USER_ID = "00000000-0000-0000-0000-000000000001"` (from `skills/finance/ingestion/_common.py`).

---

### Task 1: Pure computation + formatting layer

**Files:**
- Create: `skills/finance/nudging/__init__.py` (empty)
- Create: `skills/finance/nudging/morning_brief.py`
- Test: `tests/test_morning_brief_format.py`

**Interfaces:**
- Consumes: nothing (pure functions only in this task).
- Produces: `NewTxn`, `BriefData` dataclasses; `format_brief(data: BriefData) -> str`; `compute_pacing_pct(mtd_total, monthly_avg, day, days_in_month) -> float | None`; `top_mover(mtd_by_cat, avg_by_cat, day, days_in_month) -> tuple[str, Decimal, Decimal] | None`; `_inr(n) -> str`. Task 2 fills `BriefData`; Task 3 calls `format_brief`.

- [ ] **Step 1: Create branch**

```bash
git checkout -b feat/daily-brief
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_morning_brief_format.py`:

```python
"""Unit tests for the pure formatting/computation layer of the morning brief."""
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from skills.finance.nudging.morning_brief import (
    BriefData,
    NewTxn,
    _inr,
    compute_pacing_pct,
    format_brief,
    top_mover,
)

IST = ZoneInfo("Asia/Kolkata")
JUL_4 = datetime(2026, 7, 4, 9, 0, tzinfo=IST)


def _base_data(**overrides) -> BriefData:
    defaults = dict(
        now_ist=JUL_4,
        new_txns=[],
        mtd_total=Decimal("38400"),
        monthly_avg=Decimal("260000"),
        months_of_history=6,
        top_category="Dining Out",
        top_category_mtd=Decimal("9200"),
        top_category_avg=Decimal("22575"),
    )
    defaults.update(overrides)
    return BriefData(**defaults)


class TestInr:
    def test_whole_rupees_grouped(self):
        assert _inr(Decimal("38400")) == "₹38,400"

    def test_rounds_paise(self):
        assert _inr(Decimal("640.75")) == "₹641"

    def test_zero(self):
        assert _inr(Decimal("0")) == "₹0"


class TestComputePacingPct:
    def test_over_pace(self):
        # expected at day 4/31 of a 260k avg month = 33,548; mtd 38,400 → +14%
        pct = compute_pacing_pct(Decimal("38400"), Decimal("260000"), 4, 31)
        assert pct is not None
        assert round(pct) == 14

    def test_under_pace_negative(self):
        pct = compute_pacing_pct(Decimal("20000"), Decimal("260000"), 4, 31)
        assert pct is not None
        assert pct < 0

    def test_no_baseline_returns_none(self):
        assert compute_pacing_pct(Decimal("38400"), Decimal("0"), 4, 31) is None


class TestTopMover:
    def test_picks_largest_positive_delta_vs_prorated_avg(self):
        mtd = {"Dining Out": Decimal("9200"), "Groceries": Decimal("1000")}
        avg = {"Dining Out": Decimal("22575"), "Groceries": Decimal("7942")}
        # day 4/31 prorated: Dining 2913 → delta +6287; Groceries 1025 → delta -25
        result = top_mover(mtd, avg, 4, 31)
        assert result is not None
        name, cat_mtd, cat_avg = result
        assert name == "Dining Out"
        assert cat_mtd == Decimal("9200")
        assert cat_avg == Decimal("22575")

    def test_no_positive_delta_returns_none(self):
        mtd = {"Groceries": Decimal("100")}
        avg = {"Groceries": Decimal("7942")}
        assert top_mover(mtd, avg, 4, 31) is None

    def test_category_without_baseline_ignored(self):
        mtd = {"Brand New Cat": Decimal("99999")}
        avg = {}
        assert top_mover(mtd, avg, 4, 31) is None

    def test_empty_inputs(self):
        assert top_mover({}, {}, 4, 31) is None


class TestFormatBriefNewTxns:
    def test_new_txns_variant(self):
        data = _base_data(new_txns=[
            NewTxn(merchant="Amazon", amount=Decimal("3180"), category="Shopping"),
            NewTxn(merchant="Uber", amount=Decimal("1000"), category="Transport"),
            NewTxn(merchant="Swiggy", amount=Decimal("640"), category="Food Delivery"),
        ])
        text = format_brief(data)
        assert text.startswith("₹ Brief · Jul 4")
        assert "3 new txns: ₹4,820 out" in text
        assert "• Amazon ₹3,180 · Shopping" in text
        assert "• Swiggy ₹640 · Food Delivery" in text
        assert "July so far: ₹38,400" in text
        # pacing line present with a signed percentage
        assert "vs avg pace" in text

    def test_caps_at_five_txns_with_more_line(self):
        txns = [
            NewTxn(merchant=f"M{i}", amount=Decimal(1000 - i), category="Shopping")
            for i in range(7)
        ]
        text = format_brief(_base_data(new_txns=txns))
        assert "7 new txns" in text
        assert text.count("•") == 5
        assert "+2 more" in text

    def test_null_category_renders_needs_review(self):
        data = _base_data(new_txns=[
            NewTxn(merchant="Mystery Shop", amount=Decimal("500"), category=None),
        ])
        assert "• Mystery Shop ₹500 · Needs Review" in format_brief(data)


class TestFormatBriefPacing:
    def test_pacing_variant_when_no_new_txns(self):
        text = format_brief(_base_data())
        assert text.startswith("₹ Brief · Jul 4")
        assert "July so far: ₹38,400" in text
        assert "vs 6-mo avg" in text
        assert "Top mover: Dining Out ₹9,200 (avg ₹22,575)" in text
        assert "new txns" not in text

    def test_no_baseline_omits_pacing_line(self):
        text = format_brief(_base_data(monthly_avg=Decimal("0"), months_of_history=0,
                                       top_category=None,
                                       top_category_mtd=Decimal("0"),
                                       top_category_avg=Decimal("0")))
        assert "July so far: ₹38,400" in text
        assert "no baseline yet" in text

    def test_no_top_mover_omits_line(self):
        text = format_brief(_base_data(top_category=None,
                                       top_category_mtd=Decimal("0"),
                                       top_category_avg=Decimal("0")))
        assert "Top mover" not in text
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_morning_brief_format.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'skills.finance.nudging'`

- [ ] **Step 4: Implement the module**

Create `skills/finance/nudging/__init__.py` (empty file).

Create `skills/finance/nudging/morning_brief.py`:

```python
"""Daily adaptive morning brief — pure computation + formatting layer.

Design (ADHD-first, per 2026-07-04 plan):
- Max ~7 short lines. One anchor message at 09:00 IST daily.
- Adaptive: if new transactions were ingested since the last brief, show them;
  otherwise show month-to-date pacing vs the trailing-6-month average.
- Never sends a useless "no data" message.

This file's pure functions take plain data and return strings/values — no I/O.
Data fetch (Task 2) and the scheduler job (Task 3) live in this module too,
layered below.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

MAX_TXN_LINES = 5


@dataclass(frozen=True)
class NewTxn:
    merchant: str
    amount: Decimal
    category: str | None  # None → not yet categorized


@dataclass(frozen=True)
class BriefData:
    now_ist: datetime
    new_txns: list[NewTxn]          # outflow txns ingested since last brief, amount desc
    mtd_total: Decimal              # month-to-date outflow (excl. transfer categories)
    monthly_avg: Decimal            # mean monthly outflow over prior full months; 0 if none
    months_of_history: int          # how many full months back the avg covers
    top_category: str | None        # biggest overspend vs pro-rated category avg
    top_category_mtd: Decimal
    top_category_avg: Decimal       # that category's full-month average


def _inr(n: Decimal | int | float) -> str:
    return f"₹{round(float(n)):,}"


def compute_pacing_pct(
    mtd_total: Decimal, monthly_avg: Decimal, day: int, days_in_month: int,
) -> float | None:
    """Percent over/under the pro-rated monthly average. None when no baseline."""
    if monthly_avg <= 0:
        return None
    expected = float(monthly_avg) * day / days_in_month
    if expected <= 0:
        return None
    return (float(mtd_total) - expected) / expected * 100.0


def top_mover(
    mtd_by_cat: dict[str, Decimal],
    avg_by_cat: dict[str, Decimal],
    day: int,
    days_in_month: int,
) -> tuple[str, Decimal, Decimal] | None:
    """Category with the largest positive delta between MTD spend and its
    pro-rated monthly average. Categories without a baseline are skipped —
    a brand-new category has no "normal" to compare against."""
    best: tuple[str, Decimal, Decimal] | None = None
    best_delta = 0.0
    for cat, mtd in mtd_by_cat.items():
        avg = avg_by_cat.get(cat)
        if avg is None or avg <= 0:
            continue
        prorated = float(avg) * day / days_in_month
        delta = float(mtd) - prorated
        if delta > best_delta:
            best_delta = delta
            best = (cat, mtd, avg)
    return best


def format_brief(data: BriefData) -> str:
    day = data.now_ist.day
    days_in_month = calendar.monthrange(data.now_ist.year, data.now_ist.month)[1]
    month_name = data.now_ist.strftime("%B")
    header = f"₹ Brief · {data.now_ist.strftime('%b')} {day}"
    pct = compute_pacing_pct(data.mtd_total, data.monthly_avg, day, days_in_month)

    lines = [header, ""]
    if data.new_txns:
        total_out = sum(t.amount for t in data.new_txns)
        lines.append(f"{len(data.new_txns)} new txns: {_inr(total_out)} out")
        for t in data.new_txns[:MAX_TXN_LINES]:
            cat = t.category or "Needs Review"
            lines.append(f"• {t.merchant} {_inr(t.amount)} · {cat}")
        overflow = len(data.new_txns) - MAX_TXN_LINES
        if overflow > 0:
            lines.append(f"  +{overflow} more")
        lines.append("")
        if pct is None:
            lines.append(f"{month_name} so far: {_inr(data.mtd_total)}")
        else:
            lines.append(
                f"{month_name} so far: {_inr(data.mtd_total)} ({pct:+.0f}% vs avg pace)"
            )
    else:
        lines.append(f"{month_name} so far: {_inr(data.mtd_total)}")
        if pct is None:
            lines.append("Pacing: no baseline yet")
        else:
            lines.append(f"Pacing {pct:+.0f}% vs 6-mo avg")
        if data.top_category is not None:
            lines.append(
                f"Top mover: {data.top_category} {_inr(data.top_category_mtd)} "
                f"(avg {_inr(data.top_category_avg)})"
            )
    return "\n".join(lines)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_morning_brief_format.py -v`
Expected: all PASS

- [ ] **Step 6: Lint + typecheck**

Run: `make lint && make typecheck`
Expected: clean (fix any findings in the new files only)

- [ ] **Step 7: Commit**

```bash
git add skills/finance/nudging/ tests/test_morning_brief_format.py
git commit -m "feat(nudging): morning brief pure formatting + pacing layer (Task 1)"
```

---

### Task 2: Data-fetch layer

**Files:**
- Modify: `skills/finance/nudging/morning_brief.py` (append fetch layer)
- Test: `tests/test_morning_brief_fetch.py`

**Interfaces:**
- Consumes: `readonly_client()` from `skills.finance.lib.db` (long-lived autocommit psycopg3 connection); `service_client()` + `adb()` from same module; `BriefData`/`NewTxn` from Task 1; `RAJAT_USER_ID` from `skills.finance.ingestion._common`.
- Produces: `fetch_brief_data(watermark_utc: datetime) -> BriefData` (SYNC — caller wraps in `asyncio.to_thread`); `async get_watermark() -> datetime` ; `async set_watermark(ts_utc: datetime) -> None`. Task 3 consumes all three.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_morning_brief_fetch.py`:

```python
"""Tests for the morning-brief data-fetch layer. DB access fully mocked."""
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

import skills.finance.nudging.morning_brief as mb


@pytest.fixture
def fake_conn(monkeypatch):
    """Patches readonly_client() with a MagicMock whose cursor returns
    scripted results per query, in call order."""
    conn = MagicMock()
    monkeypatch.setattr(mb, "readonly_client", lambda: conn)
    return conn


def _script_queries(conn, results: list[list[tuple]]):
    """Each execute() call pops the next result set."""
    cursors = []
    for rows in results:
        cur = MagicMock()
        cur.fetchall.return_value = rows
        cur.fetchone.return_value = rows[0] if rows else None
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cursors.append(cur)
    conn.cursor.side_effect = cursors


WATERMARK = datetime(2026, 7, 3, 3, 30, tzinfo=UTC)


class TestFetchBriefData:
    def test_assembles_brief_data(self, fake_conn):
        _script_queries(fake_conn, [
            # 1. new txns since watermark (merchant, amount, category)
            [("Amazon", Decimal("3180"), "Shopping"),
             ("Swiggy", Decimal("640"), "Food Delivery")],
            # 2. MTD total
            [(Decimal("38400"),)],
            # 3. monthly totals for prior full months (month, total)
            [(datetime(2026, 1, 1), Decimal("250000")),
             (datetime(2026, 2, 1), Decimal("270000"))],
            # 4. MTD by category
            [("Dining Out", Decimal("9200"))],
            # 5. category monthly avg over prior full months
            [("Dining Out", Decimal("22575"))],
        ])
        data = mb.fetch_brief_data(WATERMARK)
        assert [t.merchant for t in data.new_txns] == ["Amazon", "Swiggy"]
        assert data.mtd_total == Decimal("38400")
        assert data.monthly_avg == Decimal("260000")
        assert data.months_of_history == 2
        assert data.top_category == "Dining Out"

    def test_empty_db_yields_zeroes(self, fake_conn):
        _script_queries(fake_conn, [[], [(None,)], [], [], []])
        data = mb.fetch_brief_data(WATERMARK)
        assert data.new_txns == []
        assert data.mtd_total == Decimal("0")
        assert data.monthly_avg == Decimal("0")
        assert data.months_of_history == 0
        assert data.top_category is None

    def test_new_txn_null_category_preserved(self, fake_conn):
        _script_queries(fake_conn, [
            [("Mystery", Decimal("500"), None)],
            [(Decimal("500"),)], [], [], [],
        ])
        data = mb.fetch_brief_data(WATERMARK)
        assert data.new_txns[0].category is None


class TestWatermark:
    @pytest.mark.asyncio
    async def test_get_watermark_returns_stored_ts(self, monkeypatch):
        stored = {"ts": "2026-07-03T03:30:00+00:00"}
        resp = MagicMock()
        resp.data = [{"value": stored}]
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = resp
        monkeypatch.setattr(mb, "service_client", lambda: client)
        ts = await mb.get_watermark()
        assert ts == datetime(2026, 7, 3, 3, 30, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_get_watermark_missing_defaults_24h(self, monkeypatch):
        resp = MagicMock()
        resp.data = []
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = resp
        monkeypatch.setattr(mb, "service_client", lambda: client)
        before = datetime.now(tz=UTC)
        ts = await mb.get_watermark()
        assert (before - ts).total_seconds() == pytest.approx(24 * 3600, abs=120)

    @pytest.mark.asyncio
    async def test_set_watermark_upserts_and_executes(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(mb, "service_client", lambda: client)
        ts = datetime(2026, 7, 4, 3, 30, tzinfo=UTC)
        await mb.set_watermark(ts)
        upsert_call = client.table.return_value.upsert
        assert upsert_call.called
        payload = upsert_call.call_args.args[0]
        assert payload["key"] == "morning_brief_last_run"
        assert payload["value"] == {"ts": "2026-07-04T03:30:00+00:00"}
        # invariant #2: chain terminated with .execute()
        assert upsert_call.return_value.execute.called
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_morning_brief_fetch.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'fetch_brief_data'`

(If `pytest.mark.asyncio` errors: `pytest-asyncio` is already a dev dep used by existing async tests — check `pyproject.toml` before adding anything.)

- [ ] **Step 3: Implement the fetch layer**

Append to `skills/finance/nudging/morning_brief.py`:

```python
# --- Data-fetch layer (sync — callers wrap in asyncio.to_thread) ------------

import logging
from datetime import UTC, timedelta
from zoneinfo import ZoneInfo

from skills.finance.ingestion._common import RAJAT_USER_ID
from skills.finance.lib.db import adb, readonly_client, service_client

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
WATERMARK_KEY = "morning_brief_last_run"

# Money movement, not spend. Loan Repayment excluded because the May 2026
# ₹8L prepayment would poison every monthly average.
_EXCLUDED_CATEGORIES = ("Self Transfer", "Wallet Load", "Loan Repayment")

_NEW_TXNS_SQL = """
    SELECT t.raw_merchant, t.amount, c.name
    FROM transactions t
    LEFT JOIN categories c ON c.id = t.category_id
    WHERE t.direction = 'out' AND t.is_deleted = false
      AND t.ingested_at > %(watermark)s
    ORDER BY t.amount DESC
"""

_MTD_TOTAL_SQL = """
    SELECT COALESCE(SUM(t.amount), 0)
    FROM transactions t
    LEFT JOIN categories c ON c.id = t.category_id
    WHERE t.direction = 'out' AND t.is_deleted = false
      AND t.date >= %(month_start)s AND t.date <= %(today)s
      AND (c.name IS NULL OR c.name NOT IN %(excluded)s)
"""

_MONTHLY_TOTALS_SQL = """
    SELECT date_trunc('month', t.date) AS mth, SUM(t.amount) AS total
    FROM transactions t
    LEFT JOIN categories c ON c.id = t.category_id
    WHERE t.direction = 'out' AND t.is_deleted = false
      AND t.date >= %(history_start)s AND t.date < %(month_start)s
      AND (c.name IS NULL OR c.name NOT IN %(excluded)s)
    GROUP BY 1
"""

_MTD_BY_CAT_SQL = """
    SELECT c.name, SUM(t.amount)
    FROM transactions t
    JOIN categories c ON c.id = t.category_id
    WHERE t.direction = 'out' AND t.is_deleted = false
      AND t.date >= %(month_start)s AND t.date <= %(today)s
      AND c.name NOT IN %(excluded)s
    GROUP BY 1
"""

_CAT_MONTHLY_AVG_SQL = """
    SELECT name, AVG(total) FROM (
        SELECT c.name AS name, date_trunc('month', t.date) AS mth,
               SUM(t.amount) AS total
        FROM transactions t
        JOIN categories c ON c.id = t.category_id
        WHERE t.direction = 'out' AND t.is_deleted = false
          AND t.date >= %(history_start)s AND t.date < %(month_start)s
          AND c.name NOT IN %(excluded)s
        GROUP BY 1, 2
    ) m GROUP BY name
"""


def _rows(conn, sql: str, params: dict) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_brief_data(watermark_utc: datetime) -> BriefData:
    """Assemble BriefData with 5 read-only queries. SYNC — wrap in to_thread."""
    now_ist = datetime.now(tz=IST)
    today = now_ist.date()
    month_start = today.replace(day=1)
    # 6 full calendar months of history before the current month
    history_start = (month_start - timedelta(days=1)).replace(day=1)
    for _ in range(5):
        history_start = (history_start - timedelta(days=1)).replace(day=1)

    conn = readonly_client()
    params = {
        "watermark": watermark_utc,
        "month_start": month_start,
        "today": today,
        "history_start": history_start,
        "excluded": _EXCLUDED_CATEGORIES,
    }

    txn_rows = _rows(conn, _NEW_TXNS_SQL, params)
    new_txns = [
        NewTxn(merchant=m or "Unknown", amount=Decimal(a), category=c)
        for (m, a, c) in txn_rows
    ]

    mtd_row = _rows(conn, _MTD_TOTAL_SQL, params)
    mtd_total = Decimal(mtd_row[0][0]) if mtd_row and mtd_row[0][0] is not None else Decimal(0)

    monthly_rows = _rows(conn, _MONTHLY_TOTALS_SQL, params)
    months = len(monthly_rows)
    monthly_avg = (
        sum((Decimal(t) for (_, t) in monthly_rows), Decimal(0)) / months
        if months else Decimal(0)
    )

    mtd_by_cat = {name: Decimal(total) for (name, total) in _rows(conn, _MTD_BY_CAT_SQL, params)}
    avg_by_cat = {name: Decimal(avg) for (name, avg) in _rows(conn, _CAT_MONTHLY_AVG_SQL, params)}

    day = now_ist.day
    dim = calendar.monthrange(now_ist.year, now_ist.month)[1]
    mover = top_mover(mtd_by_cat, avg_by_cat, day, dim)

    return BriefData(
        now_ist=now_ist,
        new_txns=new_txns,
        mtd_total=mtd_total,
        monthly_avg=monthly_avg,
        months_of_history=months,
        top_category=mover[0] if mover else None,
        top_category_mtd=mover[1] if mover else Decimal(0),
        top_category_avg=mover[2] if mover else Decimal(0),
    )


# --- Watermark (agent_memory) ------------------------------------------------


async def get_watermark() -> datetime:
    """Last successful brief send (UTC). Missing → 24h ago (first run)."""
    resp = await adb(
        lambda: service_client()
        .table("agent_memory")
        .select("value")
        .eq("user_id", RAJAT_USER_ID)
        .eq("key", WATERMARK_KEY)
        .execute()
    )
    if resp.data:
        return datetime.fromisoformat(resp.data[0]["value"]["ts"])
    return datetime.now(tz=UTC) - timedelta(hours=24)


async def set_watermark(ts_utc: datetime) -> None:
    payload = {
        "user_id": RAJAT_USER_ID,
        "key": WATERMARK_KEY,
        "value": {"ts": ts_utc.isoformat()},
    }
    await adb(
        lambda: service_client()
        .table("agent_memory")
        .upsert(payload, on_conflict="user_id,key")
        .execute()
    )
```

Note: `%(excluded)s` with a Python tuple renders as a SQL composite via psycopg3, valid inside `IN`. The MagicMock tests don't exercise SQL rendering; live verification happens in Task 3 Step 6.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_morning_brief_fetch.py tests/test_morning_brief_format.py -v`
Expected: all PASS

- [ ] **Step 5: Lint + typecheck**

Run: `make lint && make typecheck`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add skills/finance/nudging/morning_brief.py tests/test_morning_brief_fetch.py
git commit -m "feat(nudging): morning brief data-fetch layer + watermark (Task 2)"
```

---

### Task 3: Scheduler job + app wiring + live smoke

**Files:**
- Modify: `skills/finance/nudging/morning_brief.py` (append job)
- Modify: `app.py` (wire cron job in `_build_scheduler`; note `_build_scheduler()` currently takes no args but already receives `bot` implicitly via module import — pass `bot` through `args=[bot]` exactly like the existing `bot_self_ping` job)
- Modify: `CLAUDE.md` (directory map + known-tasks line)
- Test: `tests/test_morning_brief_job.py`

**Interfaces:**
- Consumes: `format_brief`, `fetch_brief_data`, `get_watermark`, `set_watermark` (Tasks 1–2); `send_alert` from `skills.finance.monitoring.alerts` (signature: `async def send_alert(text: str) -> None`); aiogram `Bot` instance from `skills.finance.bot.main`; `settings.telegram_chat_id_rajat`.
- Produces: `async def send_morning_brief_job(bot: Bot) -> None` — registered in `app.py` with `CronTrigger(hour=9, minute=0, timezone="Asia/Kolkata")`, job id `morning_brief`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_morning_brief_job.py`:

```python
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
    text = bot.send_message.await_args.args[1] if len(bot.send_message.await_args.args) > 1 \
        else bot.send_message.await_args.kwargs["text"]
    assert text.startswith("₹ Brief")
    set_wm.assert_awaited_once()
    # watermark advanced to a time >= the old one
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_morning_brief_job.py -v`
Expected: FAIL — no attribute `send_morning_brief_job`

- [ ] **Step 3: Implement the job**

Append to `skills/finance/nudging/morning_brief.py`:

```python
# --- Scheduler job ------------------------------------------------------------

import asyncio
from typing import TYPE_CHECKING

from skills.finance.lib.settings import settings
from skills.finance.monitoring.alerts import send_alert

if TYPE_CHECKING:
    from aiogram import Bot


async def send_morning_brief_job(bot: Bot) -> None:
    """09:00 IST daily. Failures alert via the secondary bot and never raise
    (an unhandled exception here would silently kill future runs' logging).

    Deliberately NO heartbeat-table write: check_stale_components_job flags
    anything >30 min old, which a daily component would trip perpetually.
    Failure alerting is direct via send_alert instead.
    """
    try:
        watermark = await get_watermark()
        generated_at = datetime.now(tz=UTC)
        data = await asyncio.to_thread(fetch_brief_data, watermark)
        text = format_brief(data)
        await bot.send_message(settings.telegram_chat_id_rajat, text)
        await set_watermark(generated_at)
        logger.info(
            "morning brief sent: new_txns=%d mtd_total=%s",
            len(data.new_txns), data.mtd_total,
        )
    except Exception as e:  # noqa: BLE001 — job must never raise into APScheduler
        logger.exception("morning brief failed")
        try:
            await send_alert(f"Morning brief failed: {type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            logger.exception("morning brief failure alert also failed")
```

- [ ] **Step 4: Wire into app.py**

In `app.py`, inside `_build_scheduler()` after the `anthropic_balance_check` block, add:

```python
    # Daily adaptive morning brief at 09:00 IST (plan 2026-07-04-daily-morning-brief).
    from skills.finance.nudging.morning_brief import send_morning_brief_job
    sched.add_job(
        send_morning_brief_job,
        CronTrigger(hour=9, minute=0, timezone="Asia/Kolkata"),
        id="morning_brief",
        args=[bot],
        replace_existing=True,
    )
    logger.info("scheduled daily morning brief at 09:00 Asia/Kolkata")
```

(`CronTrigger` is already imported locally in that function for the balance check — move `from apscheduler.triggers.cron import CronTrigger` to the top of the function if needed so both jobs share it. `bot` is already imported at module top.)

- [ ] **Step 5: Run the full suite + lint + typecheck**

Run: `.venv/bin/python -m pytest tests/ -q && make lint && make typecheck`
Expected: all pass, no regressions

- [ ] **Step 6: Live smoke test (one-shot, real DB + real Telegram)**

```bash
.venv/bin/python - <<'EOF'
import asyncio
from aiogram import Bot
from skills.finance.lib.settings import settings
from skills.finance.nudging.morning_brief import send_morning_brief_job

async def main():
    bot = Bot(token=settings.telegram_bot_token)
    await send_morning_brief_job(bot)
    await bot.session.close()

asyncio.run(main())
EOF
```

Expected: a real brief lands in Rajat's Telegram; console shows `morning brief sent` log line. If SQL fails against the live DB (e.g. the `IN %(excluded)s` composite), fix the query and re-run tests + smoke. Record the received message text in the task report.

- [ ] **Step 7: Update CLAUDE.md**

In `CLAUDE.md` directory map, add after the `monitoring/` line:

```
- `skills/finance/nudging/morning_brief.py` — daily 09:00 IST adaptive brief (new txns else MTD pacing); watermark in agent_memory
```

- [ ] **Step 8: Commit**

```bash
git add skills/finance/nudging/morning_brief.py app.py CLAUDE.md tests/test_morning_brief_job.py
git commit -m "feat(nudging): 09:00 IST morning brief job wired into scheduler (Task 3)"
```

---

## Self-Review Notes

- Spec coverage: adaptive content ✅ (format_brief branches), 09:00 IST ✅ (CronTrigger), watermark ✅ (agent_memory), never-useless-message ✅ (pacing fallback), failure alerting ✅ (send_alert path), feature branch ✅ (Task 1 Step 1).
- Type consistency: `fetch_brief_data(watermark_utc: datetime) -> BriefData` consumed identically in Task 3 tests and job; `top_mover` returns `tuple[str, Decimal, Decimal] | None` in both definition and use.
- Known risk flagged for live smoke: psycopg3 tuple adaptation for `IN %(excluded)s` — verified pattern, but Task 3 Step 6 is the real gate.
