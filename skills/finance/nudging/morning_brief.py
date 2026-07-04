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
