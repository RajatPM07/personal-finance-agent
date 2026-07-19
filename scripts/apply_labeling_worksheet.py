"""Apply a filled labeling worksheet: write category_id onto 'Needs Review' rows.

Reads the CSV produced by ``scripts/build_labeling_worksheet.py`` after a human
has filled the ``category`` column. Each labelled payee's category is written to
that payee's rows (exact ``raw_merchant`` match, still 'Needs Review',
direction='out') on the matching account(s).

Safe by construction:
  * DRY RUN by default — prints the plan, writes nothing. --apply to write.
  * Blank ``category`` rows are skipped (payees the human chose not to label
    stay 'Needs Review').
  * Every category is validated against the user's taxonomy BEFORE any write;
    an unknown category aborts with a clear error (no partial write).
  * Idempotent: only touches rows still categorized 'Needs Review'.

Usage:
    .venv/bin/python -m scripts.apply_labeling_worksheet \
        --user ayushi --account-like PhonePe --file tasks/phonepe_labeling.csv
    ... --apply
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from scripts.backfill_categorization import _connect, category_ids
from skills.finance.lib.users import AYUSHI_USER_ID, RAJAT_USER_ID

USERS = {"rajat": RAJAT_USER_ID, "ayushi": AYUSHI_USER_ID}


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply a filled labeling worksheet.")
    ap.add_argument("--user", choices=list(USERS), default="ayushi")
    ap.add_argument("--account-like", default="PhonePe")
    ap.add_argument("--file", required=True, help="filled worksheet CSV")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1

    conn = _connect()
    uid = USERS[args.user]
    name_to_id = category_ids(conn, uid)

    accts = conn.execute(
        """
        SELECT id FROM accounts
        WHERE user_id=%s AND (institution ILIKE %s OR nickname ILIKE %s)
        """,
        (uid, f"%{args.account_like}%", f"%{args.account_like}%"),
    ).fetchall()
    acct_ids = [str(a[0]) for a in accts]

    labelled: list[tuple[str, str]] = []   # (payee, category)
    skipped_blank = 0
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            cat = (row.get("category") or "").strip()
            payee = (row.get("payee") or "").strip()
            if not cat:
                skipped_blank += 1
                continue
            labelled.append((payee, cat))

    # Validate all categories up front — abort before any write on a typo.
    unknown = sorted({c for _, c in labelled if c not in name_to_id})
    if unknown:
        print(f"ERROR: worksheet uses categories not in {args.user}'s taxonomy: {unknown}",
              file=sys.stderr)
        print(f"Allowed: {', '.join(sorted(name_to_id))}", file=sys.stderr)
        return 1

    print(f"{len(labelled)} payees labelled, {skipped_blank} left blank (stay Needs Review).")
    for payee, cat in labelled:
        print(f"  [{cat:<18}] {payee[:45]}")

    if not args.apply:
        print("\n(DRY RUN — nothing written. Re-run with --apply.)")
        return 0

    nr_id = name_to_id["Needs Review"]
    written = 0
    for payee, cat in labelled:
        cur = conn.execute(
            """
            UPDATE transactions SET category_id=%s
            WHERE user_id=%s AND account_id = ANY(%s) AND raw_merchant=%s
              AND direction='out' AND category_id=%s
            """,
            (name_to_id[cat], uid, acct_ids, payee, nr_id),
        )
        written += cur.rowcount
    print(f"\nWrote category_id to {written} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
