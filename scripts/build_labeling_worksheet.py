"""Build a human-labeling worksheet for a user's 'Needs Review' spend rows.

For UPI person-to-person payments, the counterparty is a bare individual name
or a masked account number — no LLM or rule can categorize them correctly, so
they sit (correctly) in 'Needs Review'. This produces a worksheet of the ones
WORTH a human's time — recurring payees and material amounts — leaving the
long tail of tiny one-offs alone. READ-ONLY: writes a CSV, never the DB.

A payee is included when it RECURS (>= --min-count rows) OR is MATERIAL
(payee total >= --min-amount). Everything below both thresholds stays as
'Needs Review' — that is the honest state for unknowable micro-UPI noise.

Fill the `category` column in the CSV, then apply with
``scripts/apply_labeling_worksheet.py``.

Usage:
    .venv/bin/python -m scripts.build_labeling_worksheet \
        --user ayushi --account-like PhonePe --out tasks/phonepe_labeling.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

from scripts.backfill_categorization import _connect, category_ids
from skills.finance.lib.users import AYUSHI_USER_ID, RAJAT_USER_ID

USERS = {"rajat": RAJAT_USER_ID, "ayushi": AYUSHI_USER_ID}
_MASKED_RE = re.compile(r"[X*]{4,}|^\d{5,}$")


def _kind(merchant: str) -> str:
    """Rough hint for the labeler: a masked/numeric account (likely a transfer)
    vs a person/merchant name."""
    return "masked-acct" if _MASKED_RE.search((merchant or "").strip()) else "name"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a Needs-Review labeling worksheet.")
    ap.add_argument("--user", choices=list(USERS), default="ayushi")
    ap.add_argument("--account-like", default="PhonePe",
                    help="substring matched against account institution/nickname")
    ap.add_argument("--min-count", type=int, default=2, help="recurring threshold")
    ap.add_argument("--min-amount", type=float, default=500.0, help="material threshold (payee total)")
    ap.add_argument("--out", default="tasks/labeling_worksheet.csv")
    args = ap.parse_args()

    conn = _connect()
    uid = USERS[args.user]

    accts = conn.execute(
        """
        SELECT id FROM accounts
        WHERE user_id=%s AND (institution ILIKE %s OR nickname ILIKE %s)
        """,
        (uid, f"%{args.account_like}%", f"%{args.account_like}%"),
    ).fetchall()
    if not accts:
        print(f"no accounts matching {args.account_like!r} for {args.user}", file=sys.stderr)
        return 1
    acct_ids = [str(a[0]) for a in accts]

    rows = conn.execute(
        """
        SELECT t.raw_merchant, t.amount, t.date
        FROM transactions t JOIN categories c ON c.id=t.category_id
        WHERE t.user_id=%s AND t.account_id = ANY(%s)
          AND t.direction='out' AND c.name='Needs Review'
          AND t.raw_merchant IS NOT NULL
        """,
        (uid, acct_ids),
    ).fetchall()

    by_payee: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for m, amt, d in rows:
        by_payee[(m or "").strip()].append((float(amt or 0), str(d)))

    # (total, count, payee, kind, dates) — typed tuples so sort/sum stay clean.
    included: list[tuple[float, int, str, str, str]] = []
    tail_rows, tail_amt = 0, 0.0
    for payee, items in by_payee.items():
        total = round(sum(a for a, _ in items), 2)
        if len(items) >= args.min_count or total >= args.min_amount:
            dates = ";".join(sorted(d for _, d in items))
            included.append((total, len(items), payee, _kind(payee), dates))
        else:
            tail_rows += len(items)
            tail_amt += total

    included.sort(key=lambda r: -r[0])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["payee", "kind", "count", "total_inr", "dates", "category"])
        for total, count, payee, kind, dates in included:
            w.writerow([payee, kind, count, total, dates, ""])  # blank category for human

    allowed = sorted(category_ids(conn, uid))
    inc_rows = sum(count for _, count, *_ in included)
    inc_amt = sum(total for total, *_ in included)
    print(f"Worksheet: {len(included)} payees ({inc_rows} rows, ₹{inc_amt:,.0f}) -> {out}")
    print(f"Left as Needs Review (below thresholds): {tail_rows} rows, ₹{tail_amt:,.0f}")
    print(f"\nAllowed categories (put one in the 'category' column):\n  {', '.join(allowed)}")
    print(f"\nFill the category column, then:\n  .venv/bin/python -m scripts.apply_labeling_worksheet "
          f"--user {args.user} --account-like {args.account_like} --file {out}  # dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
