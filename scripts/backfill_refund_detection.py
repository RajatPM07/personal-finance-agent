"""One-time backfill pass to populate is_refund / is_self_transfer / linked_txn_id
on existing transactions rows.

Per W5.1 spec §6.2. Idempotent — re-runnable; the detector's IS NULL guards
mean already-processed rows are skipped. Default: dry-run.

Usage:
    .venv/bin/python -m scripts.backfill_refund_detection           # dry-run by default
    .venv/bin/python -m scripts.backfill_refund_detection --apply   # actually writes
    .venv/bin/python -m scripts.backfill_refund_detection --apply --strict
                                                                     # raise on first error
"""
from __future__ import annotations

import argparse
from typing import Any, cast
from uuid import UUID

from postgrest.types import CountMethod

from skills.finance.categorization.refund_detector import (
    DetectionResult,
    detect_for_account,
)
from skills.finance.lib.db import service_client


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually commit changes. Without this, runs in dry-run mode "
             "(prints what WOULD happen but writes nothing).",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Raise on the first per-account error instead of logging and "
             "continuing. Useful in CI/dev.",
    )
    return p.parse_args()


def _dry_run_count(account_id: UUID) -> DetectionResult:
    """Estimate what detect_for_account WOULD do without writing. We just
    count direction='in' rows with is_refund IS NULL on this account."""
    sb = service_client()
    res = (
        sb.table("transactions")
        .select("id", count=CountMethod.exact)
        .eq("account_id", str(account_id))
        .eq("direction", "in")
        .is_("is_refund", "null")
        .execute()
    )
    pending_count = res.count or 0
    return DetectionResult(
        refunds_linked=0,
        self_transfers_linked=0,
        rows_processed=pending_count,
        rows_pending=0,
    )


def main() -> int:
    args = _parse_args()
    sb = service_client()
    # supabase-py's .data is typed as a JSON union; we know the shape from the
    # SELECT projection. Cast to a concrete list-of-dicts so mypy can index it.
    accounts = cast(
        "list[dict[str, Any]]",
        sb.table("accounts").select("id,nickname,type").execute().data or [],
    )

    if not accounts:
        print("No accounts found.")
        return 0

    totals = {
        "refunds_linked": 0,
        "self_transfers_linked": 0,
        "rows_processed": 0,
        "rows_pending": 0,
    }
    failures: list[tuple[str, str]] = []

    for acct in accounts:
        acct_id = UUID(acct["id"])
        label = f"{acct['nickname']} ({acct['type']}, {acct_id})"
        print(f"\n=== {label} ===")
        try:
            r = (
                detect_for_account(acct_id, since=None)
                if args.apply
                else _dry_run_count(acct_id)
            )
        except Exception as e:  # noqa: BLE001
            failures.append((label, f"{type(e).__name__}: {e}"))
            if args.strict:
                raise
            print(f"  FAILED: {type(e).__name__}: {e} (continuing — pass --strict to abort)")
            continue

        if args.apply:
            print(f"  refunds_linked={r.refunds_linked}  self_transfers_linked={r.self_transfers_linked}")
            print(f"  rows_processed={r.rows_processed}    rows_pending={r.rows_pending}")
        else:
            print(f"  WOULD process {r.rows_processed} direction='in' rows (is_refund IS NULL)")

        totals["refunds_linked"] += r.refunds_linked
        totals["self_transfers_linked"] += r.self_transfers_linked
        totals["rows_processed"] += r.rows_processed
        totals["rows_pending"] += r.rows_pending

    print("\n=== Totals ===")
    for k, v in totals.items():
        print(f"  {k}: {v}")

    if failures:
        print(f"\nFailed accounts ({len(failures)}):")
        for label, msg in failures:
            print(f"  {label}: {msg}")

    if not args.apply:
        print("\n(dry run — pass --apply to commit changes)")

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
