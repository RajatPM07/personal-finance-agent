"""One-off historical backfill of Ayushi's statements into `transactions`.

Default is a DRY RUN (parse + report, no DB writes). Pass ``--commit`` to
ingest. Idempotent via the ``import_hash`` upsert, so re-running is safe.

Why a script and not the folder-watcher: her filenames
(``Monthlystatement_*.pdf``) carry no bank token, can't distinguish her two
ICICI cards, and can't encode owner. This explicit manifest is the auditable
source of truth. Every row is written under Ayushi's ``user_id`` and her
per-account ``account_id`` (seeded by ``009_ayushi_onboarding.local.sql``).

Invoked unattended by ``launchd/com.rajat.pfa.ayushi-backfill.plist`` at 04:00
IST for the first real run; on ``--commit`` it sends a Telegram summary.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
# settings' env_file is relative; load .env by absolute path so the script is
# CWD-independent under launchd (imports below instantiate Settings at import).
load_dotenv(REPO_ROOT / ".env")

from skills.finance.ingestion._common import (  # noqa: E402
    ParseResult,
    SourceMeta,
    password_lookup,
)
from skills.finance.ingestion.parsers import (  # noqa: E402
    icici_cc,
    icici_savings_xls,
    phonepe_upi,
)
from skills.finance.ingestion.pipeline import ingest  # noqa: E402
from skills.finance.lib.users import AYUSHI_USER_ID  # noqa: E402

STMT_DIR = REPO_ROOT / "Aayushi Statement"
CREDENTIALS_PATH = REPO_ROOT / "credentials.yaml"

# Account UUIDs seeded in 009_ayushi_onboarding.local.sql.
ACC_AMAZONPAY = UUID("20000000-0000-0000-0000-000000000001")
ACC_CARD2 = UUID("20000000-0000-0000-0000-000000000002")
ACC_PHONEPE = UUID("20000000-0000-0000-0000-000000000003")
ACC_SAVINGS = UUID("20000000-0000-0000-0000-000000000004")

NICK = {
    ACC_AMAZONPAY: "ICICI Amazon Pay CC",
    ACC_CARD2: "ICICI CC 2",
    ACC_PHONEPE: "PhonePe UPI",
    ACC_SAVINGS: "ICICI Savings",
}


@dataclass
class Job:
    path: Path
    parse: Callable[[], ParseResult]
    account_id: UUID
    source: SourceMeta


def build_jobs() -> list[Job]:
    """Explicit file → parser → account manifest. Skips the annual statement."""
    jobs: list[Job] = []
    for p in sorted(STMT_DIR.glob("Monthlystatement_13 *.pdf")):
        jobs.append(Job(p, partial(icici_cc.parse, p, ""), ACC_AMAZONPAY,
                        SourceMeta("manual_pdf", p.name)))
    for p in sorted(STMT_DIR.glob("Monthlystatement_17 *.pdf")):
        jobs.append(Job(p, partial(icici_cc.parse, p, ""), ACC_CARD2,
                        SourceMeta("manual_pdf", p.name)))
    phonepe = STMT_DIR / "PhonePe_Transaction_Statement.pdf"
    if phonepe.exists():
        pw = password_lookup("phonepe_upi", "XXXX15", credentials_path=CREDENTIALS_PATH)
        jobs.append(Job(phonepe, partial(phonepe_upi.parse, phonepe, pw), ACC_PHONEPE,
                        SourceMeta("manual_pdf", phonepe.name)))
    xls = STMT_DIR / "OpTransactionHistory13-07-2026.xls"
    if xls.exists():
        jobs.append(Job(xls, partial(icici_savings_xls.parse, xls), ACC_SAVINGS,
                        SourceMeta("manual_xlsx", xls.name)))
    return jobs


async def run(commit: bool) -> int:
    jobs = build_jobs()
    mode = "COMMIT" if commit else "DRY RUN"
    print(f"=== Ayushi backfill — {mode} — {len(jobs)} files ===\n")

    header = f"{'file':<46}{'parser':<20}{'account':<22}{'ins':>5}{'out ₹':>15}{'in ₹':>15}"
    print(header)
    print("-" * len(header))

    per_account_added: dict[UUID, int] = {}
    failures: list[str] = []
    total_insertable = 0
    phonepe_70k_seen = False

    for job in jobs:
        try:
            pr = job.parse()
            ins_rows = pr.insertable_rows()
            out_tot = sum((r.amount for r in ins_rows if r.direction == "out"), Decimal(0))
            in_tot = sum((r.amount for r in ins_rows if r.direction == "in"), Decimal(0))
            total_insertable += len(ins_rows)
            if job.account_id == ACC_PHONEPE:
                phonepe_70k_seen = any(r.amount == Decimal("70000.00") for r in ins_rows)
            print(f"{job.path.name[:46]:<46}{pr.parser_version:<20}"
                  f"{NICK[job.account_id]:<22}{len(ins_rows):>5}"
                  f"{out_tot:>15,.2f}{in_tot:>15,.2f}")
            if commit:
                log = await ingest(pr, job.account_id, job.source, user_id=AYUSHI_USER_ID)
                added = log.get("rows_added", 0) or 0
                per_account_added[job.account_id] = per_account_added.get(job.account_id, 0) + added
                print(f"    -> {log.get('status')}: {added} rows added")
        except Exception as e:  # noqa: BLE001 — one bad file must not abort the batch
            failures.append(f"{job.path.name}: {type(e).__name__}: {e}")
            print(f"    !! FAILED: {type(e).__name__}: {e}")

    print(f"\nTotal insertable rows across files: {total_insertable}")
    print(f"PhonePe ₹70,000 rent row present: {phonepe_70k_seen}")
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print("  -", f)

    if commit:
        added_total = sum(per_account_added.values())
        lines = ["📥 Ayushi historical backfill complete.",
                 f"Total rows added: {added_total}"]
        for acc, n in per_account_added.items():
            lines.append(f"  {NICK[acc]}: +{n}")
        if failures:
            lines.append(f"⚠️ {len(failures)} file(s) failed: " + "; ".join(failures))
        try:
            from skills.finance.monitoring.alerts import send_alert
            await send_alert("\n".join(lines))
        except Exception as e:  # noqa: BLE001 — Telegram failure must not fail the backfill
            print(f"(telegram summary failed: {type(e).__name__}: {e})")

    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill Ayushi's statement history.")
    ap.add_argument("--commit", action="store_true",
                    help="actually write to the DB (default: dry run, no writes)")
    args = ap.parse_args()
    return asyncio.run(run(args.commit))


if __name__ == "__main__":
    sys.exit(main())
