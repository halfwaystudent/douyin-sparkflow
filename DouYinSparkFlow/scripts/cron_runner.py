import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


LEGACY_DOCKER_COMMAND_MARKERS = (
    "docker ps --format",
    "docker exec",
)


def migrate_legacy_line(line):
    """Replace old Docker-in-Docker task commands with a local task runner."""
    if not line.strip() or line.lstrip().startswith("#"):
        return line
    if not all(marker in line for marker in LEGACY_DOCKER_COMMAND_MARKERS):
        return line

    parts = line.split(maxsplit=5)
    if len(parts) < 6:
        return line

    schedule = " ".join(parts[:5])
    command = parts[5]
    redirect_match = re.search(r"(\s+>>\s+\S+(?:\s+2>&1)?)\s*$", command)
    redirect = redirect_match.group(1) if redirect_match else ""

    if "SPARKFLOW_MANUAL_UNSENT_ONLY=1" in command:
        env_prefix = (
            "SPARKFLOW_TRIGGER_LABEL='unsent fallback' "
            "SPARKFLOW_MANUAL_RUN=1 "
            "SPARKFLOW_MANUAL_UNSENT_ONLY=1 "
            "PYTHONUNBUFFERED=1"
        )
    else:
        env_prefix = "SPARKFLOW_TRIGGER_LABEL='scheduled send'"

    return f"{schedule} env {env_prefix} bash /app/scripts/run_scheduled_task.sh{redirect}"


def migrate_legacy_crontab(path):
    """Rewrite persisted legacy task lines once, preserving other cron entries."""
    if not path.exists():
        return False

    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = raw.splitlines()
    migrated_lines = [migrate_legacy_line(line) for line in lines]
    if migrated_lines == lines:
        return False

    updated = "\n".join(migrated_lines)
    if updated:
        updated += "\n"
    temporary = path.with_name(f".{path.name}.migration.tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(path)
    return True


def expand_field(field, minimum, maximum, current):
    values = set()
    for part in str(field).split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            step = max(1, int(raw_step))
        if part == "*":
            start, end = minimum, maximum
        elif "-" in part:
            raw_start, raw_end = part.split("-", 1)
            start, end = int(raw_start), int(raw_end)
        else:
            start = end = int(part)
        values.update(range(max(minimum, start), min(maximum, end) + 1, step))
    return current in values


def cron_dow(now):
    return (now.weekday() + 1) % 7


def field_matches(field, minimum, maximum, current, *, allow_sunday_alias=False):
    if allow_sunday_alias and current == 0:
        return expand_field(field, minimum, maximum, 0) or expand_field(field, minimum, maximum, 7)
    return expand_field(field, minimum, maximum, current)


def line_should_run(line, now):
    parts = line.split(maxsplit=5)
    if len(parts) < 6:
        return False, ""
    minute, hour, day, month, weekday, command = parts
    try:
        matched = (
            field_matches(minute, 0, 59, now.minute)
            and field_matches(hour, 0, 23, now.hour)
            and field_matches(day, 1, 31, now.day)
            and field_matches(month, 1, 12, now.month)
            and field_matches(weekday, 0, 7, cron_dow(now), allow_sunday_alias=True)
        )
    except ValueError:
        return False, ""
    return matched, command


def read_crontab(path):
    if not path.exists():
        return []
    lines = []
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" in line.split(maxsplit=1)[0]:
            continue
        lines.append(line)
    return lines


def run_loop(crontab_path):
    crontab_path.parent.mkdir(parents=True, exist_ok=True)
    if migrate_legacy_crontab(crontab_path):
        print(f"[cron_runner] migrated legacy task commands in {crontab_path}", flush=True)
    last_minute_key = None
    print(f"[cron_runner] watching {crontab_path}", flush=True)
    while True:
        now = datetime.now()
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        if minute_key != last_minute_key:
            last_minute_key = minute_key
            for line in read_crontab(crontab_path):
                matched, command = line_should_run(line, now)
                if matched and command:
                    print(f"[cron_runner] {now.isoformat(timespec='seconds')} run: {command}", flush=True)
                    subprocess.Popen(["/bin/bash", "-lc", command])
        time.sleep(15)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/host-spool-cron/root")
    run_loop(target)
