#!/bin/bash
# One-shot wrapper for the Ayushi historical backfill.
#
# Invoked by launchd/com.rajat.pfa.ayushi-backfill.plist at 04:00 IST. Runs the
# backfill with --commit (writes to the production DB; idempotent via
# import_hash), then removes its own launchd job so it never fires again.
set -u

LABEL="com.rajat.pfa.ayushi-backfill"
ROOT="/Users/rajat/AntiGravity/Personal finance Agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

cd "$ROOT" || exit 1
"$ROOT/.venv/bin/python" "$ROOT/scripts/backfill_ayushi.py" --commit
STATUS=$?

# One-shot self-teardown: unload the job and delete the installed plist so it
# does not run again tomorrow. Detached + delayed so `bootout` doesn't kill
# this script before it exits.
( sleep 5; /bin/launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; /bin/rm -f "$PLIST" ) >/dev/null 2>&1 &

exit $STATUS
