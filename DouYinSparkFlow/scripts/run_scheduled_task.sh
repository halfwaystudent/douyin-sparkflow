#!/usr/bin/env bash

set -u

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
trigger_label="${SPARKFLOW_TRIGGER_LABEL:-scheduled task}"

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
