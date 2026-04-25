"""Restore a .sql.gz Supabase backup into a scratch target for verification.

Scratch target can be (a) a throwaway Supabase project, or (b) a local Postgres db.
Usage: python scripts/restore_drill.py <backup.sql.gz> <scratch_db_url>

DISASTER RECOVERY PROCEDURE (read this before assuming the backup is enough):

    The backup files produced by `scripts/backup_supabase.py` use
    `pg_dump --no-owner --no-acl`, which deliberately STRIPS role definitions
    and grants for portability. That means:

      - `finance_agent_readonly` role is NOT in the dump.
      - GRANT ... TO finance_agent_readonly is NOT in the dump.
      - Default privileges on `public` are NOT in the dump.

    Recovery is therefore a TWO-STEP process, not just "restore the dump":

      1. Apply migrations/001_init.sql to the recovery target. This creates
         the readonly role (without password), all tables, all triggers.
      2. Apply migrations/001_init.local.sql to set the role's password.
      3. THEN run this script (or psql) to restore the data dump.

    Doing only step 3 will give you the tables and data, but the SQL agent's
    readonly path won't work because the role doesn't exist.

    This is the right pattern: schema and roles live in version-controlled
    migrations; data lives in backups. Don't try to "fix" it by re-enabling
    `--owner` and `--acl` on pg_dump — that breaks portability across projects
    and is what we explicitly opted out of.
"""
from __future__ import annotations

import gzip
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: restore_drill.py <backup.sql.gz> <scratch_db_url>",
            file=sys.stderr,
        )
        return 2
    backup_gz = Path(sys.argv[1])
    target = sys.argv[2]

    raw = backup_gz.with_suffix("")
    with gzip.open(backup_gz, "rb") as fi, open(raw, "wb") as fo:
        fo.write(fi.read())
    try:
        subprocess.run(["psql", target, "-f", str(raw)], check=True)
    finally:
        raw.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
