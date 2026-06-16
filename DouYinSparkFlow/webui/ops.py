import json
import hashlib
import logging
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from utils.config import get_app_settings, get_config, get_userData, normalize_unique_id, repo_root, save_config

logger = logging.getLogger(__name__)

TASK_SCHEDULE_MARKERS = (
    "docker compose run --rm task",
    "docker compose run --rm douyin",
    "main.py --doTask",
)
HOST_CRONTAB_PATH = Path("/host-spool-cron/root")
WINDOWED_SCHEDULE_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})/(\d+)m$", re.IGNORECASE)


def running_in_container():
    return Path("/.dockerenv").exists()


def compose_root():
    settings = get_app_settings()
    raw = settings.get("compose_root") or ""
    if raw:
        p = Path(raw)
        if (p / "docker-compose.yml").exists():
            return p
    # Docker-out-of-Docker: the compose file lives on the host at
    # /opt/douyin-sparkflow but is not always bind-mounted into /app.
    for candidate in [
        Path("/opt/douyin-sparkflow"),
        repo_root().parent,
        repo_root(),
    ]:
        if (candidate / "docker-compose.yml").exists():
            return candidate
    # Fallback
    return Path(raw) if raw else repo_root()


def compose_file_path():
    path = compose_root() / "docker-compose.yml"
    return path if path.exists() else None


def compose_command(*args):
    compose_file = compose_file_path()
    base = ["docker", "compose"]
    if compose_file:
        base.extend(["-f", str(compose_file)])
    base.extend(args)
    return base


def build_task_run_spec():
    if running_in_container():
        return [sys.executable, "main.py", "--doTask"], repo_root()
    if compose_file_path():
        return compose_command("run", "--rm", "task"), compose_root()
    return [sys.executable, "main.py", "--doTask"], repo_root()


def _env_shell_prefix(extra_env=None):
    parts = []
    for key, value in (extra_env or {}).items():
        parts.append(f"{key}={shlex.quote(str(value))}")
    return " ".join(parts)


def _with_env_prefix(command, extra_env=None):
    env_prefix = _env_shell_prefix(extra_env)
    return f"env {env_prefix} {command}" if env_prefix else command


def _compose_env_args(extra_env=None):
    parts = []
    for key, value in (extra_env or {}).items():
        parts.extend(["-e", f"{key}={value}"])
    return " ".join(shlex.quote(part) for part in parts)


def _ops_log_file():
    return str(get_app_settings().get("ops_log_file") or "/app/logs/douyin-sparkflow.log")


def build_scheduled_task_command(extra_env=None, trigger_label="scheduled send"):
    if running_in_container():
        task_command = _with_env_prefix("python main.py --doTask", extra_env)
        repo_root_quoted = shlex.quote(str(repo_root()))
        script = (
            "timestamp=$(date -Iseconds); "
            f"echo \"[AUTO_TRIGGER] $timestamp {trigger_label} start\"; "
            f"cd {repo_root_quoted} && {task_command}"
        )
        return f"/bin/bash -lc {shlex.quote(script)}"
    if compose_file_path():
        compose_root_quoted = shlex.quote(str(compose_root()))
        compose_env_args = _compose_env_args(extra_env)
        compose_env_suffix = f" {compose_env_args}" if compose_env_args else ""
        return (
            "/bin/bash -lc "
            f"'echo \"[AUTO_TRIGGER] $(date -Iseconds) compose {trigger_label} start\"; "
            f"cd {compose_root_quoted} && /usr/bin/docker compose run --rm{compose_env_suffix} task'"
        )
    repo_root_quoted = shlex.quote(str(repo_root()))
    python_quoted = shlex.quote(sys.executable)
    task_command = _with_env_prefix(f"{python_quoted} main.py --doTask", extra_env)
    return (
        "/bin/bash -lc "
        f"'echo \"[AUTO_TRIGGER] $(date -Iseconds) local {trigger_label} start\"; "
        f"cd {repo_root_quoted} && {task_command}'"
    )


def build_unsent_fallback_task_command():
    return build_scheduled_task_command(
        {
            "SPARKFLOW_MANUAL_RUN": "1",
            "SPARKFLOW_MANUAL_UNSENT_ONLY": "1",
            "PYTHONUNBUFFERED": "1",
        },
        trigger_label="unsent fallback",
    )


def run_command(args, cwd=None, timeout=120, check=False):
    """Run a command and return the CompletedProcess.

    ``check`` defaults to False so callers can inspect the result without
    crashing when the command is unavailable (e.g. docker not installed).
    """
    try:
        return subprocess.run(
            args,
            cwd=str(cwd or compose_root()),
            check=check,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        logger.warning("Command not found: %s", args[0] if args else args)
        return _empty_result()
    except subprocess.TimeoutExpired:
        logger.warning("Command timed out: %s", args)
        return _empty_result()
    except subprocess.CalledProcessError as exc:
        logger.warning("Command failed (rc=%s): %s", exc.returncode, args)
        return _empty_result(stderr=exc.stderr or "")


def _empty_result(stdout="", stderr=""):
    """Return a fake CompletedProcess for graceful degradation."""
    return subprocess.CompletedProcess(args=[], returncode=1, stdout=stdout, stderr=stderr)


def run_background_command(args, log_path, cwd=None, env=None):
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab")
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    process = subprocess.Popen(
        args,
        cwd=str(Path(cwd) if cwd else compose_root()),
        stdout=handle,
        stderr=subprocess.STDOUT,
        env=child_env,
    )
    handle.close()
    return process.pid


def get_container_status():
    try:
        result = run_command(
            [
                "docker",
                "ps",
                "-a",
                "--format",
                "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.State}}\t{{.RunningFor}}\t{{.Labels}}",
            ],
            timeout=15,
        )
        rows = []
        for raw_line in (result.stdout or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split("\t", 5)
            while len(parts) < 6:
                parts.append("")
            name, image, status, state, running_for, labels = parts
            rows.append(
                {
                    "Names": name,
                    "Image": image,
                    "Status": status,
                    "State": state,
                    "RunningFor": running_for,
                    "Labels": labels,
                }
            )
        return rows
    except Exception as exc:
        logger.warning("get_container_status failed: %s", exc)
        return []


class contextlib_suppress_json:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is json.JSONDecodeError


def get_task_container_rows():
    try:
        rows = get_container_status()
        interesting_names = {"douyin-web-hostfix", "douyin-web", "douyin-task"}
        return [row for row in rows if row.get("Names") in interesting_names]
    except Exception as exc:
        logger.warning("get_task_container_rows failed: %s", exc)
        return []


def run_task_now(*, unsent_only=False):
    try:
        log_file = Path(_ops_log_file())
        command, cwd = build_task_run_spec()
        run_env = {
            "SPARKFLOW_MANUAL_RUN": "1",
            "PYTHONUNBUFFERED": "1",
        }
        if unsent_only:
            run_env["SPARKFLOW_MANUAL_UNSENT_ONLY"] = "1"
        return run_background_command(
            command,
            log_file,
            cwd=cwd,
            env=run_env,
        )
    except Exception as exc:
        import traceback
        Path("task_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        logger.error("run_task_now failed: %s", exc)
        return -1


def run_unsent_retry_now():
    return run_task_now(unsent_only=True)


def refresh_proxy():
    try:
        script = Path(get_app_settings().get("proxy_refresh_script") or "")
        if script.exists():
            return run_command(["bash", str(script)], timeout=120)
        return run_command(compose_command("restart", "proxy"), timeout=120)
    except Exception as exc:
        logger.error("refresh_proxy failed: %s", exc)
        return _empty_result(stderr=str(exc))


def restart_proxy():
    try:
        return run_command(compose_command("restart", "proxy"), timeout=120)
    except Exception as exc:
        logger.error("restart_proxy failed: %s", exc)
        return _empty_result(stderr=str(exc))


def read_log_tail(lines=200):
    log_path = Path(_ops_log_file())
    if not log_path.exists():
        return ""
    content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def read_crontab():
    if running_in_container() and HOST_CRONTAB_PATH.exists():
        return HOST_CRONTAB_PATH.read_text(encoding="utf-8", errors="replace")
    try:
        result = subprocess.run(["crontab", "-l"], text=True, capture_output=True, timeout=10)
        if result.returncode != 0:
            return ""
        return result.stdout
    except Exception as exc:
        logger.warning("read_crontab failed: %s", exc)
        return ""


def _format_window_schedule(window_config):
    return (
        f"{int(window_config['startHour']):02d}:00-"
        f"{int(window_config['endHour']):02d}:00/"
        f"{int(window_config['scheduleIntervalMinutes'])}m"
    )


def parse_schedule_string(time_string):
    raw = str(time_string or "").strip()
    match = WINDOWED_SCHEDULE_RE.fullmatch(raw)
    if match:
        start_hour, start_minute, end_hour, end_minute, interval = [int(part) for part in match.groups()]
        if start_minute != 0 or end_minute != 0:
            raise ValueError("Window schedule must use whole hours, e.g. 10:00-18:00/10m")
        if start_hour not in range(24) or end_hour not in range(24) or end_hour <= start_hour:
            raise ValueError("Window schedule is out of range")
        if interval not in range(1, 60):
            raise ValueError("Window schedule interval must be between 1 and 59 minutes")
        return {
            "mode": "window",
            "startHour": start_hour,
            "endHour": end_hour,
            "scheduleIntervalMinutes": interval,
        }

    if not re.fullmatch(r"\d{2}:\d{2}", raw):
        raise ValueError("Time must use HH:MM or HH:00-HH:00/10m format")
    hour, minute = [int(part) for part in raw.split(":", 1)]
    if hour not in range(24) or minute not in range(60):
        raise ValueError("Time is out of range")
    return {"mode": "fixed", "hour": hour, "minute": minute}


def validate_time_string(time_string):
    parsed = parse_schedule_string(time_string)
    if parsed["mode"] != "fixed":
        raise ValueError("Time must use HH:MM format")
    return parsed["hour"], parsed["minute"]


def replace_douyin_cron_schedule(crontab_text, time_string):
    schedule = parse_schedule_string(time_string)
    scheduled_command = build_scheduled_task_command()
    fallback_command = build_unsent_fallback_task_command()
    log_redirect = f" >> {shlex.quote(_ops_log_file())} 2>&1"
    updated = []

    for raw_line in crontab_text.splitlines():
        line = raw_line.rstrip("\n")
        if any(marker in line for marker in TASK_SCHEDULE_MARKERS):
            continue
        updated.append(line)

    if schedule["mode"] == "window":
        updated.append(
            f"*/{schedule['scheduleIntervalMinutes']} {schedule['startHour']}-{schedule['endHour'] - 1} * * * "
            f"{scheduled_command}{log_redirect}"
        )
        updated.append(
            f"0 {schedule['endHour']} * * * "
            f"{scheduled_command}{log_redirect}"
        )
        updated.append(
            f"{schedule['scheduleIntervalMinutes']} {schedule['endHour']} * * * "
            f"{fallback_command}{log_redirect}"
        )
    else:
        updated.append(
            f"{schedule['minute']} {schedule['hour']} * * * "
            f"{scheduled_command}{log_redirect}"
        )

    normalized = "\n".join(line for line in updated if line.strip())
    if normalized:
        normalized += "\n"
    return normalized


def persist_schedule_config(time_string):
    parsed = parse_schedule_string(time_string)
    config = get_config(force_reload=True)
    window = dict(config.get("dailySendWindow") or {})
    if parsed["mode"] == "window":
        window.update(
            {
                "enabled": True,
                "startHour": parsed["startHour"],
                "endHour": parsed["endHour"],
                "scheduleIntervalMinutes": parsed["scheduleIntervalMinutes"],
            }
        )
    else:
        window.update({"enabled": False})
    config["dailySendWindow"] = window
    save_config(config)


def update_daily_schedule(time_string):
    persist_schedule_config(time_string)
    current = read_crontab()
    updated = replace_douyin_cron_schedule(current, time_string)
    if running_in_container() and HOST_CRONTAB_PATH.parent.exists():
        try:
            HOST_CRONTAB_PATH.write_text(updated, encoding="utf-8")
            return subprocess.CompletedProcess(args=["write-host-crontab"], returncode=0, stdout="", stderr="")
        except Exception as exc:
            logger.error("update_daily_schedule failed: %s", exc)
            return _empty_result(stderr=str(exc))
    try:
        process = subprocess.run(["crontab", "-"], input=updated, text=True, capture_output=True, check=True, timeout=10)
        return process
    except Exception as exc:
        logger.error("update_daily_schedule failed: %s", exc)
        return _empty_result(stderr=str(exc))


def sync_daily_schedule_from_config():
    """Ensure the enabled send window config is materialized into the mounted cron file."""
    window = dict(get_config(force_reload=True).get("dailySendWindow") or {})
    if not window.get("enabled"):
        return subprocess.CompletedProcess(args=["sync-daily-schedule"], returncode=0, stdout="disabled", stderr="")
    if not running_in_container() or not HOST_CRONTAB_PATH.parent.exists():
        return subprocess.CompletedProcess(args=["sync-daily-schedule"], returncode=0, stdout="skipped", stderr="")

    try:
        time_string = _format_window_schedule(window)
        current = HOST_CRONTAB_PATH.read_text(encoding="utf-8", errors="replace") if HOST_CRONTAB_PATH.exists() else ""
        updated = replace_douyin_cron_schedule(current, time_string)
        if current == updated:
            return subprocess.CompletedProcess(args=["sync-daily-schedule"], returncode=0, stdout="unchanged", stderr="")
        HOST_CRONTAB_PATH.write_text(updated, encoding="utf-8")
        return subprocess.CompletedProcess(args=["sync-daily-schedule"], returncode=0, stdout="synced", stderr="")
    except Exception as exc:
        logger.error("sync_daily_schedule_from_config failed: %s", exc)
        return _empty_result(stderr=str(exc))


def current_daily_schedule():
    config = get_config(force_reload=True)
    window = dict(config.get("dailySendWindow") or {})
    if window.get("enabled"):
        try:
            return _format_window_schedule(window)
        except Exception:
            logger.warning("current_daily_schedule found invalid dailySendWindow=%s", window)

    for line in read_crontab().splitlines():
        if any(marker in line for marker in TASK_SCHEDULE_MARKERS):
            parts = line.split(maxsplit=5)
            if len(parts) >= 2:
                if parts[0].isdigit() and parts[1].isdigit():
                    minute = int(parts[0])
                    hour = int(parts[1])
                    return f"{hour:02d}:{minute:02d}"
                return f"{parts[1]}:{parts[0]}"
    return ""


def _schedule_timezone():
    timezone_name = (
        str(os.getenv("SPARKFLOW_TIMEZONE") or "").strip()
        or str(os.getenv("TZ") or "").strip()
        or "Asia/Shanghai"
    )
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        if timezone_name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        return datetime.now().astimezone().tzinfo


def _normalize_send_window():
    raw = dict(get_config(force_reload=True).get("dailySendWindow") or {})
    return {
        "enabled": bool(raw.get("enabled", False)),
        "startHour": int(raw.get("startHour", 10)),
        "endHour": int(raw.get("endHour", 18)),
        "scheduleIntervalMinutes": max(1, int(raw.get("scheduleIntervalMinutes", 10))),
    }


def _parse_sent_at(raw_value, local_tz):
    if not raw_value:
        return None
    raw = str(raw_value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)


def _account_identity(user):
    return str(user.get("unique_id") or user.get("username") or "unknown").strip()


def _scheduled_send_time(user, target_name, send_window, now):
    """Return the deterministic planned send time for a target on the current day."""
    window_minutes = max(1, (send_window["endHour"] - send_window["startHour"]) * 60)
    start_of_window = now.replace(
        hour=send_window["startHour"],
        minute=0,
        second=0,
        microsecond=0,
    )
    seed = f"{now.date().isoformat()}|{_account_identity(user)}|{target_name}"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    offset_minutes = int.from_bytes(digest[:8], "big") % window_minutes
    return start_of_window + timedelta(minutes=offset_minutes)


def _window_fallback_deadline(send_window, now):
    """Return the last automatic retry check time for the current send window."""
    return now.replace(
        hour=send_window["endHour"],
        minute=0,
        second=0,
        microsecond=0,
    ) + timedelta(minutes=send_window["scheduleIntervalMinutes"])


def _window_has_finished_for_today(send_window, now):
    """Return True when both the configured window and fallback check have passed."""
    return bool(send_window.get("enabled")) and now > _window_fallback_deadline(send_window, now)


def _build_target_status(account, target_name, now, send_window):
    history = dict(account.get("message_history") or {})
    failure_queue = dict(account.get("failure_queue") or {})

    history_entry = history.get(target_name) or {}
    sent_at = _parse_sent_at(history_entry.get("sentAt"), now.tzinfo)
    if sent_at and sent_at.date() == now.date():
        return {
            "target": target_name,
            "status": "sent",
            "message": str(history_entry.get("message") or ""),
            "sentAt": sent_at.isoformat(timespec="seconds"),
            "lastAttemptAt": "",
            "category": "",
            "reason": "",
            "attemptCount": 0,
            "scheduledAt": "",
            "scheduleNote": "",
        }

    failure_entry = failure_queue.get(target_name) or {}
    last_attempt_at = _parse_sent_at(failure_entry.get("lastAttemptAt"), now.tzinfo)
    if last_attempt_at and last_attempt_at.date() == now.date():
        return {
            "target": target_name,
            "status": "failed",
            "message": str(failure_entry.get("message") or ""),
            "sentAt": "",
            "lastAttemptAt": last_attempt_at.isoformat(timespec="seconds"),
            "category": str(failure_entry.get("category") or ""),
            "reason": str(failure_entry.get("reason") or ""),
            "attemptCount": int(failure_entry.get("attemptCount") or 0),
            "scheduledAt": "",
            "scheduleNote": "",
        }

    scheduled_at = None
    schedule_note = ""
    if send_window.get("enabled"):
        scheduled_at = _scheduled_send_time(account, target_name, send_window, now)
        if scheduled_at > now:
            return {
                "target": target_name,
                "status": "pending",
                "message": "",
                "sentAt": "",
                "lastAttemptAt": "",
                "category": "",
                "reason": "",
                "attemptCount": 0,
                "scheduledAt": scheduled_at.isoformat(timespec="seconds"),
                "scheduleNote": "",
            }
        if _window_has_finished_for_today(send_window, now):
            schedule_note = "今天窗口已结束，明天自动发送或请手动补发"

    return {
        "target": target_name,
        "status": "unprocessed",
        "message": "",
        "sentAt": "",
        "lastAttemptAt": "",
        "category": "",
        "reason": "",
        "attemptCount": 0,
        "scheduledAt": scheduled_at.isoformat(timespec="seconds") if scheduled_at else "",
        "scheduleNote": schedule_note,
    }


def get_send_console_snapshot():
    accounts = [account for account in get_userData(force_reload=True) if account.get("enabled", True)]
    send_window = _normalize_send_window()
    now = datetime.now(_schedule_timezone())

    summary = {
        "enabled_accounts": len(accounts),
        "today_sent_targets": 0,
        "today_failed_targets": 0,
        "today_pending_targets": 0,
        "today_unprocessed_targets": 0,
    }
    account_rows = []

    for account in accounts:
        statuses = [_build_target_status(account, target_name, now, send_window) for target_name in account.get("targets") or []]
        sent_targets = [item for item in statuses if item["status"] == "sent"]
        failed_targets = [item for item in statuses if item["status"] == "failed"]
        pending_targets = [item for item in statuses if item["status"] == "pending"]
        unprocessed_targets = [item for item in statuses if item["status"] == "unprocessed"]

        summary["today_sent_targets"] += len(sent_targets)
        summary["today_failed_targets"] += len(failed_targets)
        summary["today_pending_targets"] += len(pending_targets)
        summary["today_unprocessed_targets"] += len(unprocessed_targets)

        account_rows.append(
            {
                "unique_id": str(account.get("unique_id") or ""),
                "username": account.get("username") or "",
                "sent_targets": sent_targets,
                "failed_targets": failed_targets,
                "pending_targets": pending_targets,
                "unprocessed_targets": unprocessed_targets,
                "last_failure_reason": failed_targets[0]["reason"] if failed_targets else "",
                "failure_queue": dict(account.get("failure_queue") or {}),
            }
        )

    return {
        "now": now.isoformat(timespec="seconds"),
        "summary": summary,
        "accounts": account_rows,
    }


def _check_image_present():
    """Return True if the douyin-sparkflow:local image exists."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "douyin-sparkflow:local"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def get_ops_snapshot():
    """Collect operational metrics for the dashboard.

    Every external call is individually guarded so the dashboard always
    renders, even when Docker or crontab are not available.
    """
    return {
        "compose_root": str(compose_root()),
        "compose_file": str(compose_file_path() or ""),
        "containers": get_container_status(),
        "task_containers": get_task_container_rows(),
        "send_console": get_send_console_snapshot(),
        "daily_schedule": current_daily_schedule(),
        "crontab": read_crontab(),
        "log_tail": read_log_tail(120),
        "image_present": _check_image_present(),
    }
