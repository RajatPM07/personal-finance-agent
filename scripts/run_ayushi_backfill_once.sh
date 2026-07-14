#!/bin/bash
# One-shot wrapper for the Ayushi historical backfill.
#
# Invoked by launchd/com.rajat.pfa.ayushi-backfill.plist at 04:00 IST. Runs the
# backfill with --commit (writes to the production DB; idempotent via
# import_hash), and on SUCCESS removes its own launchd job so it never fires
# again. On failure it leaves the job registered so the next 04:00 retries.
set -u

LABEL="com.rajat.pfa.ayushi-backfill"
ROOT="/Users/rajat/AntiGravity/Personal finance Agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SENTINEL="/Users/rajat/finance-logs/.ayushi_backfill_done"

teardown() {
    # Detached + delayed so bootout doesn't kill this script before it exits;
    # AbandonProcessGroup=true in the plist lets this subshell outlive us.
    ( sleep 5
      /bin/launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
      /bin/rm -f "$PLIST" ) >/dev/null 2>&1 &
}

# Already completed on a prior fire — self-heal any job that failed to
# deregister (e.g. a teardown race), then exit without re-running.
if [ -f "$SENTINEL" ]; then
    teardown
    exit 0
fi

cd "$ROOT" || exit 1
"$ROOT/.venv/bin/python" "$ROOT/scripts/backfill_ayushi.py" --commit
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
    touch "$SENTINEL"
    teardown
fi

exit "$STATUS"
