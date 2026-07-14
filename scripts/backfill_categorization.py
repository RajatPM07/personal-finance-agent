"""One-time backfill: LLM-categorize a user's uncategorized transactions.

Default is a DRY RUN — prints the distinct merchant → category mapping the LLM
proposes plus row counts, and writes NOTHING. Pass --apply to (a) seed the
target user's category taxonomy by cloning the reference user's category names
and (b) write ``category_id`` onto the matching transactions.

Design:
  * Distinct ``raw_merchant`` strings are categorized once each (batched ~20 per
    LLM call by the categorizer), then mapped back to every matching row.
  * Money-movement rows (``is_self_transfer IS TRUE``) skip the LLM entirely and
    are hard-mapped to the 'Self Transfer' category — cheaper and avoids tagging
    CC bill-pay as spend.
  * Idempotent: only touches rows with ``category_id IS NULL``; re-runs are safe
    and never re-touch already-categorized rows (e.g. the reference user's).

Usage:
    .venv/bin/python -m scripts.backfill_categorization              # dry-run (Aayushi)
    .venv/bin/python -m scripts.backfill_categorization --apply
    .venv/bin/python -m scripts.backfill_categorization --user <uuid> --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from skills.finance.categorization.categorizer import (
    FALLBACK_CATEGORY,
    categorize_merchants,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

RAJAT_USER_ID = "00000000-0000-0000-0000-000000000001"  # reference taxonomy
AYUSHI_USER_ID = "00000000-0000-0000-0000-000000000002"
SELF_TRANSFER_CATEGORY = "Self Transfer"

# Deterministic overrides for internal-transfer / bare-account strings the LLM
# cannot identify and tends to mis-guess (rent to a bare account number lands in
# 'Bank Charges'; PPF/FD contributions land in 'EMI'). Substring match on
# raw_merchant, checked BEFORE the LLM — these merchants skip the model entirely.
# Same spirit as the "Known merchant mappings" in CLAUDE.md. Every value here
# MUST be a real category name in the taxonomy.
KNOWN_OVERRIDES: list[tuple[str, str]] = [
    ("4891", "Rent"),                 # rent to Sunil Bhatkar (a/c …4891), ₹70k/mo
    ("Trf to PPF", SELF_TRANSFER_CATEGORY),  # PPF contribution — own investment
    ("TRF TO FD", SELF_TRANSFER_CATEGORY),   # FD creation — own investment
]


def override_category(merchant: str) -> str | None:
    """Return the deterministic category for `merchant` if any override
    substring matches (case-insensitive), else None."""
    ml = merchant.lower()
    for needle, cat in KNOWN_OVERRIDES:
        if needle.lower() in ml:
            return cat
    return None


def _connect() -> psycopg.Connection:
    load_dotenv(REPO_ROOT / ".env")
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("SUPABASE_DB_URL not set", file=sys.stderr)
        raise SystemExit(1)
    return psycopg.connect(db_url, prepare_threshold=None, autocommit=True)


def reference_category_names(conn: psycopg.Connection, ref_user: str) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM categories WHERE user_id=%s ORDER BY name", (ref_user,)
    ).fetchall()
    return [r[0] for r in rows]


def ensure_categories(conn: psycopg.Connection, user_id: str, ref_user: str) -> int:
    """Clone any of the reference user's category names the target user lacks.
    Flat clone (name + is_system); returns the number of rows inserted."""
    cur = conn.execute(
        """
        INSERT INTO categories (user_id, name, is_system, created_by)
        SELECT %s, r.name, r.is_system, %s
        FROM categories r
        WHERE r.user_id = %s
          AND NOT EXISTS (
            SELECT 1 FROM categories t
            WHERE t.user_id = %s AND t.name = r.name
          )
        """,
        (user_id, user_id, ref_user, user_id),
    )
    return cur.rowcount


def category_ids(conn: psycopg.Connection, user_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT name, id FROM categories WHERE user_id=%s", (user_id,)
    ).fetchall()
    return {r[0]: str(r[1]) for r in rows}


def distinct_uncategorized_merchants(
    conn: psycopg.Connection, user_id: str
) -> list[tuple[str, int]]:
    """Distinct raw_merchant (+ row count) for uncategorized SPEND rows — the
    set the LLM must categorize. Scoped to direction='out' (income/refund
    credits are not spend and have no spend category) and to non-self-transfer
    rows (those are hard-mapped to 'Self Transfer' without an LLM call)."""
    rows = conn.execute(
        """
        SELECT raw_merchant, count(*)
        FROM transactions
        WHERE user_id=%s AND category_id IS NULL
          AND direction='out'
          AND is_self_transfer IS NOT TRUE
          AND raw_merchant IS NOT NULL
        GROUP BY raw_merchant
        ORDER BY count(*) DESC
        """,
        (user_id,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def self_transfer_pending(conn: psycopg.Connection, user_id: str) -> int:
    row = conn.execute(
        """
        SELECT count(*) FROM transactions
        WHERE user_id=%s AND category_id IS NULL AND is_self_transfer IS TRUE
        """,
        (user_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM-categorize a user's transactions.")
    ap.add_argument("--user", default=AYUSHI_USER_ID, help="target user_id UUID")
    ap.add_argument("--ref", default=RAJAT_USER_ID, help="reference taxonomy user_id")
    ap.add_argument("--apply", action="store_true", help="seed categories + write (default: dry run)")
    ap.add_argument("--batch-size", type=int, default=12, help="merchants per LLM call")
    ap.add_argument("--pause", type=float, default=4.0,
                    help="seconds between LLM batches (free-tier TPM pacing)")
    args = ap.parse_args()

    conn = _connect()

    allowed = reference_category_names(conn, args.ref)
    if not allowed:
        print(f"reference user {args.ref} has no categories to clone from", file=sys.stderr)
        return 1
    for required in (FALLBACK_CATEGORY, SELF_TRANSFER_CATEGORY):
        if required not in allowed:
            print(f"reference taxonomy missing required category {required!r}", file=sys.stderr)
            return 1

    # 'Self Transfer' is NOT offered to the LLM: money-movement classification is
    # the detector's job (is_self_transfer). Letting the model pick it invites
    # tagging rent/salary as self-transfer. Only hard-mapped rows get it.
    allowed_for_llm = [c for c in allowed if c != SELF_TRANSFER_CATEGORY]

    merchants = distinct_uncategorized_merchants(conn, args.user)
    st_pending = self_transfer_pending(conn, args.user)
    merchant_names = [m for m, _ in merchants]
    row_count = {m: n for m, n in merchants}

    print(f"target user: {args.user}")
    print(f"uncategorized SPEND (out) rows across {len(merchant_names)} distinct merchants")
    print(f"self-transfer rows to hard-map to '{SELF_TRANSFER_CATEGORY}': {st_pending}")

    if not merchant_names and st_pending == 0:
        print("Nothing to categorize.")
        return 0

    # Deterministic overrides first; only the remainder goes to the LLM.
    overridden: dict[str, str] = {}
    for m in merchant_names:
        cat = override_category(m)
        if cat is not None:
            overridden[m] = cat
    to_llm = [m for m in merchant_names if m not in overridden]
    if overridden:
        print(f"{len(overridden)} merchant(s) resolved by deterministic override (skip LLM).")

    n_batches = (len(to_llm) + args.batch_size - 1) // max(args.batch_size, 1)
    print(f"\nCalling LLM ({n_batches} batch(es), {args.pause}s pacing)...")
    mapping = categorize_merchants(
        to_llm, allowed_for_llm, batch_size=args.batch_size, pause_s=args.pause
    )
    mapping.update(overridden)  # overrides win

    # Summarise by category for a readable dry-run.
    by_cat: dict[str, list[tuple[str, int]]] = {}
    for m in merchant_names:
        cat = mapping[m]
        by_cat.setdefault(cat, []).append((m, row_count[m]))

    print("\n=== Proposed mapping (merchant → category), grouped ===")
    for cat in sorted(by_cat, key=lambda c: -sum(n for _, n in by_cat[c])):
        rows = sorted(by_cat[cat], key=lambda x: -x[1])
        total_rows = sum(n for _, n in rows)
        print(f"\n  [{cat}]  {len(rows)} merchants, {total_rows} rows")
        for m, n in rows[:12]:
            print(f"      {n:>3}x  {m[:52]}")
        if len(rows) > 12:
            print(f"      … +{len(rows) - 12} more merchants")

    fallback_n = sum(n for _, n in by_cat.get(FALLBACK_CATEGORY, []))
    print(f"\nSummary: {len(merchant_names)} merchants mapped, "
          f"{fallback_n} rows -> {FALLBACK_CATEGORY}, {st_pending} rows -> {SELF_TRANSFER_CATEGORY}")

    if not args.apply:
        print("\n(DRY RUN — nothing written. Re-run with --apply to seed categories and write.)")
        return 0

    # --- APPLY ---
    inserted = ensure_categories(conn, args.user, args.ref)
    print(f"\nSeeded {inserted} category rows for target user (idempotent).")
    name_to_id = category_ids(conn, args.user)

    missing = {c for c in mapping.values() if c not in name_to_id}
    if SELF_TRANSFER_CATEGORY not in name_to_id:
        missing.add(SELF_TRANSFER_CATEGORY)
    if missing:
        print(f"ERROR: categories missing after seed: {missing}", file=sys.stderr)
        return 1

    written = 0
    for merchant, cat in mapping.items():
        cur = conn.execute(
            """
            UPDATE transactions SET category_id=%s
            WHERE user_id=%s AND raw_merchant=%s AND direction='out'
              AND category_id IS NULL AND is_self_transfer IS NOT TRUE
            """,
            (name_to_id[cat], args.user, merchant),
        )
        written += cur.rowcount

    st_cur = conn.execute(
        """
        UPDATE transactions SET category_id=%s
        WHERE user_id=%s AND category_id IS NULL AND is_self_transfer IS TRUE
        """,
        (name_to_id[SELF_TRANSFER_CATEGORY], args.user),
    )
    print(f"Wrote category_id to {written} merchant rows + {st_cur.rowcount} self-transfer rows.")

    rem_row = conn.execute(
        "SELECT count(*) FROM transactions WHERE user_id=%s AND category_id IS NULL",
        (args.user,),
    ).fetchone()
    print(f"Remaining uncategorized for user: {rem_row[0] if rem_row else '?'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
