#!/usr/bin/env bash

set -u

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
trigger_label="${SPARKFLOW_TRIGGER_LABEL:-scheduled task}"

# Temporary A/B switch: a marker file disables UID response reading for every
# scheduled run. Kept here because the web app regenerates the cron file from
# config on startup, which would drop an env prefix added to cron.
if [ -f "$app_root/logs/uid-collection-off.flag" ]; then
    export SPARKFLOW_UID_COLLECTION=off
fi

# Friend-chat identity guard: off unless the marker exists, because it fails
# closed and an unverified live page shape would block every friend send.
if [ -f "$app_root/logs/friend-guard-on.flag" ]; then
    export SPARKFLOW_FRIEND_GUARD=on
fi

echo "[AUTO_TRIGGER] $(date -Iseconds) ${trigger_label} start"

cd "$app_root"
cd_rc=$?
if [ "$cd_rc" -ne 0 ]; then
    echo "[AUTO_TRIGGER] $(date -Iseconds) ${trigger_label} failed to enter app directory rc=${cd_rc}"
    exit "$cd_rc"
fi

python main.py --doTask
task_rc=$?
echo "[AUTO_TRIGGER] $(date -Iseconds) ${trigger_label} exit rc=${task_rc}"
exit "$task_rc"
