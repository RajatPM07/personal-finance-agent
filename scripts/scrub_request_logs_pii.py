"""One-off privacy remediation: scrub prompt/response bodies from request_logs.

Historically LiteLLM's built-in ["supabase"] success callback persisted the
full `messages` (rendered prompt) and `response` body to request_logs. For the
SQL judge those contain schema_excerpt + result_preview = real PII. The forward
fix (lib/llm.py) stops writing bodies; this script removes the ALREADY-STORED
ones by resetting the content columns to their empty default while PRESERVING
the metadata (model, total_cost, response_time, status) the balance check needs.

Default is a DRY RUN (report counts only). Pass --commit to apply.
Idempotent: re-running after a successful scrub reports 0 rows to scrub.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]

_HAS_CONTENT = (
    "messages::text <> '{}' OR response::text <> '{}' "
    "OR coalesce(end_user,'') <> '' OR error::text <> '{}'"
)
_SCRUB = (
    "UPDATE request_logs "
    "SET messages='{}'::json, response='{}'::json, end_user='', error='{}'::json "
    f"WHERE {_HAS_CONTENT}"
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrub PII bodies from request_logs.")
    ap.add_argument("--commit", action="store_true", help="apply the scrub (default: dry run)")
    args = ap.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("SUPABASE_DB_URL not set", file=sys.stderr)
        return 1

    conn = psycopg.connect(db_url, prepare_threshold=None, autocommit=True)

    def _scalar(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row else 0

    total = _scalar("SELECT count(*) FROM request_logs")
    with_content = _scalar(f"SELECT count(*) FROM request_logs WHERE {_HAS_CONTENT}")
    print(f"request_logs: {total} rows total, {with_content} hold body/PII content.")

    if not args.commit:
        print("DRY RUN — no changes. Re-run with --commit to scrub.")
        return 0

    conn.execute(_SCRUB)
    remaining = _scalar(f"SELECT count(*) FROM request_logs WHERE {_HAS_CONTENT}")
    claude_row = conn.execute(
        "SELECT count(*), round(coalesce(sum(total_cost),0)::numeric, 4) "
        "FROM request_logs WHERE model ILIKE '%claude%'"
    ).fetchone()
    claude_n, claude_cost = (claude_row[0], claude_row[1]) if claude_row else (0, 0)
    print(f"SCRUBBED. rows with content now: {remaining} (was {with_content}).")
    print(f"Cost metadata preserved: {claude_n} claude rows, total_cost sum ${claude_cost}.")
    if remaining:
        print("WARNING: some rows still hold content — investigate.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
