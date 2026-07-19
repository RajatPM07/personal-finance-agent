"""Read-only categorization + dedup quality audit.

Run on the Mac (needs DB network access):

    python scripts/audit_categorization.py            # both users
    python scripts/audit_categorization.py --user rajat

Prints, per user:
  1. Categorization coverage — categorized / Needs Review / NULL, by account
  2. Top uncategorized merchants (normalized) by row count + amount
  3. Suspected duplicate rows — same (account, date, amount, description)
     appearing >1 with DIFFERENT import_hash. This is the AMEX re-download
     signature (xlsx bytes differ per download -> pdf_content_hash differs ->
     every row's import_hash differs -> UNIQUE(import_hash) never fires).
  4. Refund/self-transfer pending rows (is_refund IS NULL)

Read-only: opens the connection with autocommit and issues SELECTs only.
Exit code 1 if suspected duplicates found (so a scheduled run can alert).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from skills.finance.categorization.normalize import normalize_merchant  # noqa: E402
from skills.finance.lib.users import AYUSHI_USER_ID, RAJAT_USER_ID  # noqa: E402

USERS = {"rajat": RAJAT_USER_ID, "ayushi": AYUSHI_USER_ID}


def _conn() -> psycopg.Connection:
    load_dotenv(REPO_ROOT / ".env")
    dsn = os.environ["SUPABASE_DB_URL"]
    # prepare_threshold=None: avoids "_pg3_0 already exists" via Supavisor
    # (see CLAUDE.md "psycopg3 in scripts").
    return psycopg.connect(dsn, prepare_threshold=None, autocommit=True)


def audit_user(conn: psycopg.Connection, label: str, user_id: str) -> bool:
    """Print the audit for one user. Returns True if suspected dups found."""
    print(f"\n{'=' * 60}\nUSER: {label}\n{'=' * 60}")

    # 1. Coverage by account
    rows = conn.execute(
        """
        SELECT coalesce(a.nickname, a.institution || ' ' || coalesce(a.identifier, '')) AS acct,
               count(*) FILTER (WHERE t.direction='out')                    AS spend_rows,
               count(*) FILTER (WHERE t.direction='out'
                                AND t.category_id IS NOT NULL)              AS categorized,
               count(*) FILTER (WHERE t.direction='out'
                                AND c.name = 'Needs Review')                AS needs_review,
               count(*) FILTER (WHERE t.direction='out'
                                AND t.category_id IS NULL
                                AND t.is_self_transfer IS NOT TRUE)         AS uncategorized
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.user_id = %s
        GROUP BY acct ORDER BY acct
        """,
        (user_id,),
    ).fetchall()
    print("\n-- Categorization coverage (spend rows) --")
    print(f"{'account':<32}{'spend':>7}{'categd':>8}{'review':>8}{'uncat':>7}")
    for name, spend, cat, review, uncat in rows:
        pct = f"{100 * cat / spend:.0f}%" if spend else "—"
        print(f"{name:<32}{spend:>7}{cat:>7} ({pct}){review:>7}{uncat:>7}")

    # 2. Top uncategorized merchants (normalized)
    raw = conn.execute(
        """
        SELECT raw_merchant, count(*), sum(amount)
        FROM transactions
        WHERE user_id = %s AND direction='out' AND category_id IS NULL
          AND is_self_transfer IS NOT TRUE AND raw_merchant IS NOT NULL
        GROUP BY raw_merchant
        """,
        (user_id,),
    ).fetchall()
    agg: dict[str, tuple[int, float]] = {}
    for m, n, amt in raw:
        k = normalize_merchant(m)
        c, a = agg.get(k, (0, 0.0))
        agg[k] = (c + n, a + float(amt or 0))
    top = sorted(agg.items(), key=lambda kv: -kv[1][1])[:15]
    print("\n-- Top uncategorized merchants (normalized) --")
    for m, (n, amt) in top:
        print(f"  {n:>4}x  ₹{amt:>12,.0f}  {m[:60]}")
    if not top:
        print("  (none — fully categorized)")

    # 3. Suspected duplicates (different hash, same content)
    dups = conn.execute(
        """
        SELECT coalesce(a.nickname, a.institution || ' ' || coalesce(a.identifier, '')) AS acct,
               t.date, t.amount, t.raw_merchant,
               count(*) AS copies, count(DISTINCT t.import_hash) AS hashes
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.user_id = %s
        GROUP BY acct, t.date, t.amount, t.raw_merchant,
                 t.source_row_ordinal
        HAVING count(*) > 1 AND count(DISTINCT t.import_hash) > 1
        ORDER BY t.date DESC
        """,
        (user_id,),
    ).fetchall()
    print("\n-- Suspected duplicate rows (same content, different import_hash) --")
    for acct, date, amount, merch, copies, hashes in dups[:25]:
        print(f"  {date} ₹{float(amount):>10,.2f} x{copies} ({hashes} hashes) "
              f"[{acct}] {str(merch)[:45]}")
    if not dups:
        print("  (none)")
    else:
        print(f"  TOTAL: {len(dups)} duplicated (date, amount, merchant) groups")

    # 4. Refund/self-transfer pending
    pending_row = conn.execute(
        "SELECT count(*) FROM transactions "
        "WHERE user_id = %s AND is_refund IS NULL AND direction='in'",
        (user_id,),
    ).fetchone()
    pending = pending_row[0] if pending_row else 0
    print(f"\n-- Refund/self-transfer detection pending (is_refund IS NULL, "
          f"direction='in'): {pending}")

    return bool(dups)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", choices=[*USERS, "all"], default="all")
    args = ap.parse_args()
    targets = USERS if args.user == "all" else {args.user: USERS[args.user]}

    with _conn() as conn:
        found_dups = False
        for label, uid in targets.items():
            found_dups |= audit_user(conn, label, uid)

    if found_dups:
        print("\n⚠️  Suspected duplicates found — likely the AMEX xlsx re-download "
              "hash gap (see tasks/lessons.md 2026-05-22 side finding).")
        return 1
    print("\nAll clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
