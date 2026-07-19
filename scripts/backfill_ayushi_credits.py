"""One-time backfill: categorize a user's uncategorized ICICI CREDIT rows.

The general spend backfill (``scripts/backfill_categorization.py``) only touches
``direction='out'`` rows, so every inbound credit (salary, refunds, FD/PPF/sweep
maturities, CC bill-payments received) stays uncategorized. This script owns the
``direction='in'`` path for a user's ICICI accounts.

Default is a DRY RUN — prints every proposed change and writes NOTHING. Pass
``--apply`` to write.

Three phases (all idempotent — each only touches rows still needing work):

  A. Self-transfer flip. CC bill-payment credits (``BBPS Payment received`` /
     ``… PAYMENT RECEIVED …``) that the refund detector left ``is_self_transfer
     IS NULL`` because no funding debit exists in the ingested data (permanent
     data gap). On a credit-card account those memos are unambiguously a bill
     payment INTO the card — definitionally a self-transfer. Flip them TRUE
     (``linked_txn_id`` stays NULL — there is no counterpart to link). Markers
     mirror ``config/self_transfer_patterns.yaml`` for these accounts.

  B. Credit categorization for the remaining uncategorized in-rows:
       1. deterministic IN_OVERRIDES (salary/interest → Income; sweep/FD/PPF/
          broker → Self Transfer; inbound P2P → Personal Transfer);
       2. merchant refunds → reuse the *original spend category* (the category
          the same normalized merchant got on its debit rows) — per the user's
          decision, a Myntra refund is labelled whatever Myntra spend was;
       3. anything left (a merchant seen only as a credit) → the LLM;
       4. final fallback → 'Needs Review'.

  C. Assign 'Self Transfer' category to every ``is_self_transfer IS TRUE`` credit
     still lacking a category (including the rows flipped in phase A).

NOTE: reporting (``morning_brief.py``) aggregates ``direction='out'`` only, so
none of these credit categories affect any spend/pacing metric — they make the
ledger read truthfully and clear the "uncategorized" backlog.

Usage:
    .venv/bin/python -m scripts.backfill_ayushi_credits            # dry-run
    .venv/bin/python -m scripts.backfill_ayushi_credits --apply
"""
from __future__ import annotations

import argparse
import sys

import psycopg

from scripts.backfill_categorization import (
    SELF_TRANSFER_CATEGORY,
    _connect,
    category_ids,
)
from skills.finance.categorization.categorizer import (
    FALLBACK_CATEGORY,
    categorize_merchants,
)
from skills.finance.categorization.normalize import normalize_merchant
from skills.finance.lib.users import AYUSHI_USER_ID

INCOME_CATEGORY = "Income"
PERSONAL_TRANSFER_CATEGORY = "Personal Transfer"

# Credit-card bill-payment markers (mirror of config/self_transfer_patterns.yaml
# for Ayushi's two CC accounts). Case-insensitive substring on raw_merchant.
CC_BILLPAY_MARKERS = ["BBPS Payment received", "PAYMENT RECEIVED"]

# Deterministic category rules for CREDIT rows, checked in order. Money-movement
# and income resolve here BEFORE any merchant/LLM guess. Same spirit as
# KNOWN_OVERRIDES in the spend backfill; the interest/dividend needles are
# holding-specific (like CLAUDE.md's "Known merchant mappings") and will need
# extending if the portfolio changes.
IN_OVERRIDES: list[tuple[str, str]] = [
    ("SALARY", INCOME_CATEGORY),              # monthly salary credit
    ("Int.Pd", INCOME_CATEGORY),              # savings interest paid
    ("IRFC", INCOME_CATEGORY),                # IRFC bond interest
    ("CENTRALBK", INCOME_CATEGORY),           # Central Bank bond interest
    ("CENTRAL BK", INCOME_CATEGORY),
    ("CIPLALIMITED", INCOME_CATEGORY),        # Cipla dividend
    ("Rev Sweep", SELF_TRANSFER_CATEGORY),    # ICICI auto-sweep FD reversal
    ("Closure Proceeds", SELF_TRANSFER_CATEGORY),
    ("SWEEP CLOSURE", SELF_TRANSFER_CATEGORY),
    ("FD clos", SELF_TRANSFER_CATEGORY),      # FD closure proceeds
    ("Trf to PPF", SELF_TRANSFER_CATEGORY),
    ("ZERODHA", SELF_TRANSFER_CATEGORY),      # broker redemption back to bank
    ("RenName", PERSONAL_TRANSFER_CATEGORY),  # inbound P2P transfer
    ("Redeem token", FALLBACK_CATEGORY),      # DTK reward — ambiguous, hold
]


def in_override(merchant: str) -> str | None:
    ml = merchant.lower()
    for needle, cat in IN_OVERRIDES:
        if needle.lower() in ml:
            return cat
    return None


def _is_cc_billpay(merchant: str | None) -> bool:
    if not merchant:
        return False
    ml = merchant.lower()
    return any(mk.lower() in ml for mk in CC_BILLPAY_MARKERS)


def icici_accounts(conn: psycopg.Connection, user_id: str) -> tuple[list[str], list[str]]:
    """Return (all_icici_account_ids, icici_credit_card_account_ids) for user."""
    rows = conn.execute(
        "SELECT id, type FROM accounts WHERE user_id=%s AND institution='ICICI'",
        (user_id,),
    ).fetchall()
    all_ids = [str(r[0]) for r in rows]
    cc_ids = [str(r[0]) for r in rows if r[1] == "credit_card"]
    return all_ids, cc_ids


def ensure_income_category(conn: psycopg.Connection, user_id: str) -> bool:
    """Create the 'Income' category for the user if absent. Returns True if it
    inserted a row. 'Income' is NOT in the reference (Rajat) taxonomy, so it is
    created directly here rather than cloned. created_by='seed' (CHECK enum)."""
    cur = conn.execute(
        """
        INSERT INTO categories (user_id, name, is_system, created_by)
        SELECT %s, %s, false, 'seed'
        WHERE NOT EXISTS (
            SELECT 1 FROM categories WHERE user_id=%s AND name=%s
        )
        """,
        (user_id, INCOME_CATEGORY, user_id, INCOME_CATEGORY),
    )
    return cur.rowcount > 0


def spend_category_by_norm(conn: psycopg.Connection, user_id: str) -> dict[str, str]:
    """Map normalized_merchant -> category name from the user's already
    categorized SPEND (out) rows. This is the 'original spend category' a refund
    inherits."""
    rows = conn.execute(
        """
        SELECT t.raw_merchant, c.name
        FROM transactions t JOIN categories c ON c.id = t.category_id
        WHERE t.user_id=%s AND t.direction='out' AND t.category_id IS NOT NULL
          AND t.raw_merchant IS NOT NULL
        """,
        (user_id,),
    ).fetchall()
    out: dict[str, str] = {}
    for raw, cat in rows:
        out[normalize_merchant(raw)] = cat
    return out


def uncategorized_credits(
    conn: psycopg.Connection, user_id: str, account_ids: list[str]
) -> list[tuple[str, int, bool]]:
    """(raw_merchant, count, is_self_transfer_is_true) for uncategorized in-rows."""
    rows = conn.execute(
        """
        SELECT raw_merchant, count(*), bool_or(is_self_transfer IS TRUE)
        FROM transactions
        WHERE user_id=%s AND account_id = ANY(%s) AND direction='in'
          AND category_id IS NULL AND raw_merchant IS NOT NULL
        GROUP BY raw_merchant
        ORDER BY count(*) DESC
        """,
        (user_id, account_ids),
    ).fetchall()
    return [(r[0], r[1], bool(r[2])) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="Categorize a user's ICICI credit rows.")
    ap.add_argument("--user", default=AYUSHI_USER_ID)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--batch-size", type=int, default=12)
    ap.add_argument("--pause", type=float, default=4.0)
    args = ap.parse_args()

    conn = _connect()
    all_ids, cc_ids = icici_accounts(conn, args.user)
    if not all_ids:
        print(f"user {args.user} has no ICICI accounts", file=sys.stderr)
        return 1

    # --- Phase A: self-transfer flip for CC bill-pay credits (marker-sufficient).
    flip_rows = conn.execute(
        """
        SELECT id, raw_merchant, amount FROM transactions
        WHERE user_id=%s AND account_id = ANY(%s) AND direction='in'
          AND is_self_transfer IS NULL
          AND (raw_merchant ILIKE '%%BBPS Payment received%%'
               OR raw_merchant ILIKE '%%PAYMENT RECEIVED%%')
        ORDER BY amount DESC
        """,
        (args.user, cc_ids),
    ).fetchall()
    print(f"=== Phase A: CC bill-pay credits to flip is_self_transfer -> TRUE: {len(flip_rows)} ===")
    for _id, m, amt in flip_rows:
        print(f"    ₹{float(amt or 0):>9,.0f}  {(m or '')[:50]}")

    # --- Phase B: categorize remaining uncategorized credits.
    credits = uncategorized_credits(conn, args.user, all_ids)
    spend_map = spend_category_by_norm(conn, args.user)

    # Rows that phase A will flip are self-transfers -> handled in phase C, skip here.
    plan: dict[str, str] = {}          # raw_merchant -> category
    residual_for_llm: list[str] = []
    for raw, _n, _st in credits:
        if _is_cc_billpay(raw):
            continue  # becomes Self Transfer via phase A + C
        ov = in_override(raw)
        if ov is not None:
            plan[raw] = ov
            continue
        cat = spend_map.get(normalize_merchant(raw))  # refund -> original spend cat
        if cat is not None:
            plan[raw] = cat
            continue
        residual_for_llm.append(raw)

    if residual_for_llm:
        allowed = sorted(category_ids(conn, args.user).keys())
        allowed = [c for c in allowed if c != SELF_TRANSFER_CATEGORY]  # detector owns money-movement
        norm_residual = {normalize_merchant(r): r for r in residual_for_llm}
        print(f"\n{len(norm_residual)} credit merchant(s) unseen in spend -> LLM:")
        llm_map = categorize_merchants(
            list(norm_residual), allowed, batch_size=args.batch_size, pause_s=args.pause
        )
        for norm, raw in norm_residual.items():
            plan[raw] = llm_map.get(norm, FALLBACK_CATEGORY)

    # Dry-run summary grouped by category.
    by_cat: dict[str, list[str]] = {}
    for raw, cat in plan.items():
        by_cat.setdefault(cat, []).append(raw)
    print("\n=== Phase B: proposed credit categorization ===")
    for cat in sorted(by_cat):
        print(f"\n  [{cat}]  {len(by_cat[cat])} merchant(s)")
        for raw in sorted(by_cat[cat]):
            print(f"      {raw[:56]}")

    st_pending = conn.execute(
        """
        SELECT count(*) FROM transactions
        WHERE user_id=%s AND account_id = ANY(%s) AND direction='in'
          AND category_id IS NULL AND is_self_transfer IS TRUE
        """,
        (args.user, all_ids),
    ).fetchone()
    already_st = int(st_pending[0]) if st_pending else 0
    print(f"\n=== Phase C: existing is_self_transfer credits -> '{SELF_TRANSFER_CATEGORY}': "
          f"{already_st} (+{len(flip_rows)} from phase A) ===")

    if not args.apply:
        print("\n(DRY RUN — nothing written. Re-run with --apply.)")
        return 0

    # --- APPLY ---
    created = ensure_income_category(conn, args.user)
    print(f"\n'{INCOME_CATEGORY}' category {'created' if created else 'already existed'}.")
    name_to_id = category_ids(conn, args.user)

    missing = {c for c in plan.values() if c not in name_to_id}
    for required in (SELF_TRANSFER_CATEGORY, INCOME_CATEGORY):
        if required not in name_to_id:
            missing.add(required)
    if missing:
        print(f"ERROR: categories missing: {missing}", file=sys.stderr)
        return 1

    # Phase A write.
    flip_ids = [r[0] for r in flip_rows]
    if flip_ids:
        conn.execute(
            "UPDATE transactions SET is_self_transfer=TRUE "
            "WHERE id = ANY(%s)",
            (flip_ids,),
        )

    # Phase B write.
    written = 0
    for raw, cat in plan.items():
        cur = conn.execute(
            """
            UPDATE transactions SET category_id=%s
            WHERE user_id=%s AND account_id = ANY(%s) AND raw_merchant=%s
              AND direction='in' AND category_id IS NULL
              AND is_self_transfer IS NOT TRUE
            """,
            (name_to_id[cat], args.user, all_ids, raw),
        )
        written += cur.rowcount

    # Phase C write: all self-transfer credits (incl. phase-A flips) -> Self Transfer.
    st_cur = conn.execute(
        """
        UPDATE transactions SET category_id=%s
        WHERE user_id=%s AND account_id = ANY(%s) AND direction='in'
          AND category_id IS NULL AND is_self_transfer IS TRUE
        """,
        (name_to_id[SELF_TRANSFER_CATEGORY], args.user, all_ids),
    )
    print(f"Flipped {len(flip_ids)} to self-transfer; wrote {written} merchant-credit rows "
          f"+ {st_cur.rowcount} self-transfer rows.")

    rem = conn.execute(
        """
        SELECT count(*) FROM transactions
        WHERE user_id=%s AND account_id = ANY(%s) AND direction='in'
          AND category_id IS NULL
        """,
        (args.user, all_ids),
    ).fetchone()
    print(f"Remaining uncategorized ICICI credits: {rem[0] if rem else '?'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
