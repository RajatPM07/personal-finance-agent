"""Quick spend summary from Supabase. Run from the repo root:

    python scripts/spend_summary.py
    python scripts/spend_summary.py --months 3
    python scripts/spend_summary.py --month 2026-05

Uses SUPABASE_DB_URL from .env (the Supavisor pooler, read-only is fine).
"""
from __future__ import annotations

import argparse
import os
from datetime import date, datetime
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

RUPEE = "₹"


def fmt(amount: float) -> str:
    if amount >= 1_00_000:
        return f"{RUPEE}{amount/1_00_000:.2f}L"
    if amount >= 1_000:
        return f"{RUPEE}{amount/1_000:.1f}k"
    return f"{RUPEE}{amount:.0f}"


def run(months: int, single_month: str | None) -> None:
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        raise SystemExit("SUPABASE_DB_URL not set — check .env")

    conn = psycopg.connect(db_url, prepare_threshold=None)

    if single_month:
        # e.g. "2026-05"
        try:
            start = datetime.strptime(single_month, "%Y-%m").date().replace(day=1)
        except ValueError:
            raise SystemExit("--month must be YYYY-MM, e.g. 2026-05")
        import calendar
        _, last_day = calendar.monthrange(start.year, start.month)
        end = start.replace(day=last_day)
        date_clause = f"date BETWEEN '{start}' AND '{end}'"
        period_label = start.strftime("%B %Y")
    else:
        date_clause = f"date >= (CURRENT_DATE - INTERVAL '{months} months')"
        period_label = f"last {months} months"

    # ── 1. Grand total ────────────────────────────────────────────────────────
    row = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(amount) FILTER (WHERE direction = 'out' AND NOT is_refund), 0) AS total_out,
            COALESCE(SUM(amount) FILTER (WHERE direction = 'in'), 0)                    AS total_in,
            COUNT(*) FILTER (WHERE direction = 'out' AND NOT is_refund)                 AS txn_count
        FROM transactions
        WHERE {date_clause}
          AND is_deleted = false
        """
    ).fetchone()
    total_out, total_in, txn_count = row  # type: ignore[misc]

    print(f"\n{'='*54}")
    print(f"  SPEND SUMMARY — {period_label}")
    print(f"{'='*54}")
    print(f"  Total spend   : {fmt(float(total_out))}  ({txn_count} transactions)")
    print(f"  Total inflows : {fmt(float(total_in))}")
    print(f"  Net           : {fmt(float(total_in) - float(total_out))}")

    # ── 2. By category ────────────────────────────────────────────────────────
    rows = conn.execute(
        f"""
        SELECT
            COALESCE(c.name, 'Uncategorised') AS category,
            SUM(t.amount)                      AS total,
            COUNT(*)                           AS cnt
        FROM transactions t
        LEFT JOIN categories c ON c.id = t.category_id
        WHERE {date_clause}
          AND t.direction = 'out'
          AND NOT t.is_refund
          AND t.is_deleted = false
        GROUP BY c.name
        ORDER BY total DESC
        """
    ).fetchall()

    print(f"\n  {'CATEGORY':<28} {'AMOUNT':>9}  {'TXNs':>5}")
    print(f"  {'-'*28} {'-'*9}  {'-'*5}")
    for cat, total, cnt in rows:
        bar = "█" * min(int(float(total) / float(total_out) * 20), 20) if total_out else ""
        print(f"  {cat:<28} {fmt(float(total)):>9}  {cnt:>5}  {bar}")

    # ── 3. By month (only when showing multiple months) ───────────────────────
    if not single_month and months > 1:
        rows_m = conn.execute(
            f"""
            SELECT
                TO_CHAR(date, 'Mon YYYY') AS month,
                DATE_TRUNC('month', date) AS month_start,
                SUM(amount)              AS total,
                COUNT(*)                 AS cnt
            FROM transactions
            WHERE {date_clause}
              AND direction = 'out'
              AND NOT is_refund
              AND is_deleted = false
            GROUP BY month, month_start
            ORDER BY month_start DESC
            """
        ).fetchall()

        print(f"\n  {'MONTH':<12} {'AMOUNT':>9}  {'TXNs':>5}")
        print(f"  {'-'*12} {'-'*9}  {'-'*5}")
        for month, _, total, cnt in rows_m:
            print(f"  {month:<12} {fmt(float(total)):>9}  {cnt:>5}")

    # ── 4. By account ─────────────────────────────────────────────────────────
    rows_a = conn.execute(
        f"""
        SELECT
            COALESCE(a.nickname, a.institution || ' ' || a.identifier) AS account,
            a.type,
            SUM(t.amount)                                               AS total,
            COUNT(*)                                                    AS cnt
        FROM transactions t
        LEFT JOIN accounts a ON a.id = t.account_id
        WHERE {date_clause}
          AND t.direction = 'out'
          AND NOT t.is_refund
          AND t.is_deleted = false
        GROUP BY a.nickname, a.institution, a.identifier, a.type
        ORDER BY total DESC
        """
    ).fetchall()

    print(f"\n  {'ACCOUNT':<32} {'TYPE':<8} {'AMOUNT':>9}  {'TXNs':>5}")
    print(f"  {'-'*32} {'-'*8} {'-'*9}  {'-'*5}")
    for acct, atype, total, cnt in rows_a:
        print(f"  {str(acct):<32} {str(atype or ''):<8} {fmt(float(total)):>9}  {cnt:>5}")

    # ── 5. Top 10 merchants ───────────────────────────────────────────────────
    rows_t = conn.execute(
        f"""
        SELECT
            raw_merchant,
            SUM(amount)  AS total,
            COUNT(*)     AS cnt
        FROM transactions
        WHERE {date_clause}
          AND direction = 'out'
          AND NOT is_refund
          AND is_deleted = false
        GROUP BY raw_merchant
        ORDER BY total DESC
        LIMIT 10
        """
    ).fetchall()

    print(f"\n  TOP 10 MERCHANTS")
    print(f"  {'-'*44} {'-'*9}  {'-'*5}")
    for merchant, total, cnt in rows_t:
        print(f"  {str(merchant)[:44]:<44} {fmt(float(total)):>9}  {cnt:>5}")

    print(f"\n{'='*54}\n")
    conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Personal finance spend summary")
    p.add_argument("--months", type=int, default=6, help="Rolling window in months (default 6)")
    p.add_argument("--month", type=str, default=None, help="Single month, e.g. 2026-05")
    args = p.parse_args()
    run(months=args.months, single_month=args.month)


if __name__ == "__main__":
    main()
