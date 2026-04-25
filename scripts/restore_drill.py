"""Restore a .sql.gz Supabase backup into a scratch target for verification.

Scratch target can be (a) a throwaway Supabase project, or (b) a local Postgres db.
Usage: python scripts/restore_drill.py <backup.sql.gz> <scratch_db_url>
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
