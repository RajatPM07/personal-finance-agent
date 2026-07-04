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

import asyncio
import calendar
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from skills.finance.ingestion._common import RAJAT_USER_ID
from skills.finance.lib.db import adb, readonly_client, service_client
from skills.finance.lib.settings import settings
from skills.finance.monitoring.alerts import send_alert

if TYPE_CHECKING:
    from aiogram import Bot

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
        if pct is None or data.mtd_total <= 0:
            lines.append(f"{month_name} so far: {_inr(data.mtd_total)}")
        else:
            lines.append(
                f"{month_name} so far: {_inr(data.mtd_total)} ({pct:+.0f}% vs avg pace)"
            )
    else:
        if data.mtd_total <= 0:
            lines.append(f"{month_name} so far: ₹0 — nothing recorded yet")
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


# --- Data-fetch layer (sync — callers wrap in asyncio.to_thread) ------------

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
WATERMARK_KEY = "morning_brief_last_run"

# Money movement, not spend. Loan Repayment excluded because the May 2026
# ₹8L prepayment would poison every monthly average.
# List (not tuple): psycopg3 adapts Python lists as PostgreSQL text[] arrays,
# enabling the != ALL(%(excluded)s::text[]) pattern below.
_EXCLUDED_CATEGORIES = ["Self Transfer", "Wallet Load", "Loan Repayment"]

_NEW_TXNS_SQL = """
    SELECT t.raw_merchant, t.amount, c.name
    FROM transactions t
    LEFT JOIN categories c ON c.id = t.category_id
    WHERE t.direction = 'out' AND t.is_deleted = false
      AND t.ingested_at > %(watermark)s
      AND t.is_self_transfer IS NOT TRUE
      AND (c.name IS NULL OR c.name != ALL(%(excluded)s::text[]))
    ORDER BY t.amount DESC
"""

_MTD_TOTAL_SQL = """
    SELECT COALESCE(SUM(t.amount), 0)
    FROM transactions t
    LEFT JOIN categories c ON c.id = t.category_id
    WHERE t.direction = 'out' AND t.is_deleted = false
      AND t.date >= %(month_start)s AND t.date <= %(today)s
      AND (c.name IS NULL OR c.name != ALL(%(excluded)s::text[]))
"""

_MONTHLY_TOTALS_SQL = """
    SELECT date_trunc('month', t.date) AS mth, SUM(t.amount) AS total
    FROM transactions t
    LEFT JOIN categories c ON c.id = t.category_id
    WHERE t.direction = 'out' AND t.is_deleted = false
      AND t.date >= %(history_start)s AND t.date < %(month_start)s
      AND (c.name IS NULL OR c.name != ALL(%(excluded)s::text[]))
    GROUP BY 1
"""

_MTD_BY_CAT_SQL = """
    SELECT c.name, SUM(t.amount)
    FROM transactions t
    JOIN categories c ON c.id = t.category_id
    WHERE t.direction = 'out' AND t.is_deleted = false
      AND t.date >= %(month_start)s AND t.date <= %(today)s
      AND c.name != ALL(%(excluded)s::text[])
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
          AND c.name != ALL(%(excluded)s::text[])
        GROUP BY 1, 2
    ) m GROUP BY name
"""


def _rows(conn: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
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
    params: dict[str, Any] = {
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
    payload: dict[str, Any] = {
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


# --- Scheduler job ------------------------------------------------------------


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
