"""Daily Supabase backup. Invoked by launchd/com.rajat.pfa.backup.plist.

Runs as an independent process — does not rely on app.py being up.

Per CLAUDE.md invariant #6, this script must NOT be registered on
APScheduler — pg_dump would block the event loop. It is launched
exclusively by the launchd plist at launchd/com.rajat.pfa.backup.plist.

Intentionally avoids importing skills.finance.lib.* (which pulls supabase,
pydantic, aiogram, etc.) so the script stays minimal and fast-failing.
"""
from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Absolute path to homebrew libpq pg_dump. Safer than relying on PATH under
# launchd's restricted environment (PATH there typically only contains
# /usr/bin:/bin:/usr/sbin:/sbin and would not pick up the force-linked
# homebrew pg_dump otherwise).
PG_DUMP = "/opt/homebrew/opt/libpq/bin/pg_dump"


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(repo_root / ".env")

    db_url = os.environ.get("SUPABASE_DB_URL")
    backup_path = Path(
        os.environ.get(
            "FINANCE_BACKUP_PATH",
            str(Path.home() / "finance-backups"),
        )
    )
    if not db_url:
        print("SUPABASE_DB_URL not set", file=sys.stderr)
        return 2
    backup_path.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw = backup_path / f"supabase_{stamp}.sql"
    gz = raw.with_suffix(".sql.gz")

    subprocess.run(
        [PG_DUMP, "--no-owner", "--no-acl", "-f", str(raw), db_url],
        check=True,
    )
    with open(raw, "rb") as fi, gzip.open(gz, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    raw.unlink()
    print(f"backup written: {gz}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
