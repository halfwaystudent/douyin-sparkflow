import errno
import json
import hashlib
import logging
import os
import re
import shlex
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from core.send_state import history_entry_is_strong_confirmed_today, parse_sent_at
from utils.config import get_app_settings, get_config, get_userData, repo_root, save_config

logger = logging.getLogger(__name__)

TASK_ALREADY_RUNNING = -2

TASK_SCHEDULE_MARKERS = (
    "docker compose run --rm task",
    "docker compose run --rm douyin",
    "main.py --doTask",
    "run_scheduled_task.sh",
)
HOST_CRONTAB_PATH = Path("/host-spool-cron/root")
WINDOWED_SCHEDULE_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})/(\d+)m$", re.IGNORECASE)

CONFIRMATION_LABELS = {
    "cdp_message_send_receipt": "服务端回执",
    "browser_visible_count_increased": "页面回显",
    "legacy_sentAt_only": "旧记录待核验",
    "manual_reset": "人工标记待核验",
}

FAILURE_CATEGORY_LABELS = {
    "send_unconfirmed": "待核验",
    "login_required": "登录失效",
    "friend_not_found": "未找到好友",
    "friend_list_unavailable": "好友列表不可用",
    "timeout": "执行超时",
    "navigation": "页面访问失败",
    "selector": "页面结构变化",
    "browser_crash": "浏览器异常",
    "protocol_user_blocked": "对方限制私信",
    "protocol_user_not_in_conversation": "不在会话中",
}


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


def _pid_is_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if getattr(exc, "winerror", None) == 87 or exc.errno == errno.ESRCH:
            return False
        if exc.errno in (errno.EPERM, errno.EACCES):
            return True
        raise
    return True


def _parse_lock_pid(raw):
    try:
        return int(str(raw or "").strip().splitlines()[0])
    except (IndexError, TypeError, ValueError):
        return None


def task_run_lock_status():
    lock_path = repo_root() / "logs" / "task.run.lock"
    if not lock_path.exists():
        return {
            "running": False,
            "path": str(lock_path),
            "pid": None,
            "ageSeconds": 0,
            "stale": False,
            "staleReason": "",
            "staleRemoved": False,
        }

    raw = lock_path.read_text(encoding="utf-8", errors="ignore")
    pid = _parse_lock_pid(raw)
    try:
        age_seconds = max(0, int(datetime.now(timezone.utc).timestamp() - lock_path.stat().st_mtime))
    except OSError:
        return {
            "running": False,
            "path": str(lock_path),
            "pid": pid,
            "ageSeconds": 0,
            "stale": True,
            "staleReason": "lock_stat_failed",
            "staleRemoved": False,
        }

    if pid is not None and not _pid_is_alive(pid):
        return {
            "running": False,
            "path": str(lock_path),
            "pid": pid,
            "ageSeconds": age_seconds,
            "stale": True,
            "staleReason": "owner_pid_missing",
            "staleRemoved": False,
        }

    if pid is None and age_seconds > 7200:
        return {
            "running": False,
            "path": str(lock_path),
            "pid": None,
            "ageSeconds": age_seconds,
            "stale": True,
            "staleReason": "unreadable_lock",
            "staleRemoved": False,
        }

    return {
        "running": True,
        "path": str(lock_path),
        "pid": pid,
        "ageSeconds": age_seconds,
        "stale": False,
        "staleReason": "",
        "staleRemoved": False,
    }


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


def build_scheduled_task_command(extra_env=None, trigger_label="scheduled send"):
    if running_in_container():
        task_env = dict(extra_env or {})
        task_env["SPARKFLOW_TRIGGER_LABEL"] = trigger_label
        return _with_env_prefix("bash /app/scripts/run_scheduled_task.sh", task_env)
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
        # Docker and cron are optional integration points when the UI is run
        # directly on a developer workstation (especially on Windows). A
        # status probe must not turn their absence into a warning on every
        # dashboard refresh.
        logger.debug("Optional command not found: %s", args[0] if args else args)
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
    cwd_path = Path(cwd) if cwd else compose_root()
    child_env = os.environ.copy()
    if env:
        child_env.update(env)

    with log_path.open("ab") as handle:
        started_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        env_keys = ",".join(sorted((env or {}).keys())) or "none"
        handle.write(
            (
                f"[WEB_TRIGGER] {started_at} start cwd={cwd_path} "
                f"env_keys={env_keys} command={shlex.join([str(part) for part in args])}\n"
            ).encode("utf-8", errors="replace")
        )
        handle.flush()
        process = subprocess.Popen(
            args,
            cwd=str(cwd_path),
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=child_env,
        )
        handle.write(f"[WEB_TRIGGER] {started_at} pid={process.pid}\n".encode("utf-8", errors="replace"))
        handle.flush()
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


def run_task_now(*, unsent_only=False, failed_only=False, force_all=False, account_refs=None):
    try:
        lock_status = task_run_lock_status()
        if lock_status.get("running"):
            logger.info(
                "Refusing to start manual task because task lock is active pid=%s age=%ss",
                lock_status.get("pid"),
                lock_status.get("ageSeconds"),
            )
            return TASK_ALREADY_RUNNING

        log_file = Path(get_app_settings().get("ops_log_file") or "/var/log/douyin-sparkflow.log")
        command, cwd = build_task_run_spec()
        run_env = {
            "SPARKFLOW_MANUAL_RUN": "1",
            "PYTHONUNBUFFERED": "1",
        }
        if account_refs is not None:
            run_env["SPARKFLOW_ACCOUNT_REFS"] = ",".join(sorted({str(ref).strip() for ref in account_refs if str(ref).strip()}))
        if force_all:
            run_env["SPARKFLOW_MANUAL_FORCE_ALL"] = "1"
        elif failed_only:
            run_env["SPARKFLOW_MANUAL_FAILED_ONLY"] = "1"
        elif unsent_only:
            run_env["SPARKFLOW_MANUAL_UNSENT_ONLY"] = "1"
        logger.info(
            "Starting background task command=%s cwd=%s env=%s log=%s",
            command,
            cwd,
            {key: run_env[key] for key in sorted(run_env)},
            log_file,
        )
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


def run_failed_retry_now(*, account_refs=None):
    return run_task_now(failed_only=True, account_refs=account_refs)


def run_unsent_retry_now(*, account_refs=None):
    return run_task_now(unsent_only=True, account_refs=account_refs)


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
        if running_in_container():
            return run_command(["docker", "restart", "mihomo"], timeout=120)
        return run_command(compose_command("restart", "proxy"), timeout=120)
    except Exception as exc:
        logger.error("restart_proxy failed: %s", exc)
        return _empty_result(stderr=str(exc))


def read_log_tail(lines=200):
    log_path = Path(get_app_settings().get("ops_log_file") or "/var/log/douyin-sparkflow.log")
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
    except FileNotFoundError:
        # Native Windows installs do not provide ``crontab``. The caller can
        # treat an unavailable scheduler as an empty schedule and still serve
        # the rest of the dashboard.
        logger.debug("Optional command not found: crontab")
        return ""
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
    updated = []

    for raw_line in crontab_text.splitlines():
        line = raw_line.rstrip("\n")
        if any(marker in line for marker in TASK_SCHEDULE_MARKERS):
            continue
        updated.append(line)

    if schedule["mode"] == "window":
        updated.append(
            f"*/{schedule['scheduleIntervalMinutes']} {schedule['startHour']}-{schedule['endHour'] - 1} * * * "
            f"{scheduled_command} >> /var/log/douyin-sparkflow.log 2>&1"
        )
        updated.append(
            f"0 {schedule['endHour']} * * * "
            f"{scheduled_command} >> /var/log/douyin-sparkflow.log 2>&1"
        )
        updated.append(
            f"{schedule['scheduleIntervalMinutes']} {schedule['endHour']} * * * "
            f"{fallback_command} >> /var/log/douyin-sparkflow.log 2>&1"
        )
    else:
        updated.append(
            f"{schedule['minute']} {schedule['hour']} * * * "
            f"{scheduled_command} >> /var/log/douyin-sparkflow.log 2>&1"
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
    config = get_config(force_reload=True)
    window = dict(config.get("dailySendWindow") or {})
    if not window.get("enabled"):
        return subprocess.CompletedProcess(
            args=["sync-daily-schedule"],
            returncode=0,
            stdout="schedule disabled; existing crontab left unchanged",
            stderr="",
        )

    try:
        time_string = _format_window_schedule(window)
        current = read_crontab()
        updated = replace_douyin_cron_schedule(current, time_string)
        if updated == current:
            return subprocess.CompletedProcess(
                args=["sync-daily-schedule"], returncode=0, stdout="already synchronized", stderr=""
            )
        if running_in_container() and HOST_CRONTAB_PATH.parent.exists():
            HOST_CRONTAB_PATH.write_text(updated, encoding="utf-8")
            return subprocess.CompletedProcess(
                args=["sync-daily-schedule"], returncode=0, stdout="host spool updated", stderr=""
            )
        return subprocess.run(
            ["crontab", "-"],
            input=updated,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
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


def _next_window_trigger(now, window):
    interval = max(1, int(window["scheduleIntervalMinutes"]))
    candidates = []
    for hour in range(int(window["startHour"]), int(window["endHour"])):
        for minute in range(0, 60, interval):
            candidates.append(now.replace(hour=hour, minute=minute, second=0, microsecond=0))
    end_hour = int(window["endHour"])
    candidates.append(now.replace(hour=end_hour, minute=0, second=0, microsecond=0))
    if interval < 60:
        candidates.append(now.replace(hour=end_hour, minute=interval, second=0, microsecond=0))
    for candidate in sorted(set(candidates)):
        if candidate > now:
            return candidate
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(
        hour=int(window["startHour"]),
        minute=0,
        second=0,
        microsecond=0,
    )


def get_schedule_snapshot(now=None):
    now = now or datetime.now(_schedule_timezone())
    window = _normalize_send_window()
    label = current_daily_schedule()
    if window.get("enabled"):
        next_trigger = _next_window_trigger(now, window)
    else:
        try:
            hour, minute = [int(part) for part in label.split(":", 1)]
            next_trigger = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_trigger <= now:
                next_trigger += timedelta(days=1)
        except (TypeError, ValueError):
            next_trigger = None
    return {
        "label": label,
        "nextTriggerAt": next_trigger.isoformat(timespec="seconds") if next_trigger else "",
        "nextTriggerDisplay": next_trigger.strftime("%m-%d %H:%M") if next_trigger else "",
    }


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
    return parse_sent_at(raw_value, local_tz)


def _account_identity(user):
    return str(user.get("unique_id") or user.get("username") or "unknown").strip()


def _coerce_attempt_count(entry):
    try:
        return int(dict(entry or {}).get("attemptCount") or 0)
    except (TypeError, ValueError):
        return 0


def _account_failure_pause_after_attempts():
    raw_value = str(os.getenv("SPARKFLOW_ACCOUNT_FAILURE_PAUSE_AFTER_ATTEMPTS") or "2").strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        return 2


def _account_failure_entry_today(account, now):
    entry = dict(account.get("account_failure") or {})
    last_attempt_at = _parse_sent_at(entry.get("lastAttemptAt"), now.tzinfo)
    if last_attempt_at and last_attempt_at.date() == now.date():
        entry["lastAttemptAt"] = last_attempt_at.isoformat(timespec="seconds")
        first_attempt_at = _parse_sent_at(entry.get("firstAttemptAt"), now.tzinfo)
        if first_attempt_at:
            entry["firstAttemptAt"] = first_attempt_at.isoformat(timespec="seconds")
        entry["attemptCount"] = _coerce_attempt_count(entry)
        entry["affectedTargets"] = list(entry.get("affectedTargets") or [])
        return entry
    return {}


def _normalize_friend_index_key(value):
    raw = unicodedata.normalize("NFKC", str(value or ""))
    for token in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        raw = raw.replace(token, "")
    raw = raw.replace("\xa0", " ")
    return " ".join(raw.split()).strip()


def _friend_index_status(account, target_name):
    friend_index = dict(account.get("friend_index") or {})
    entry = dict(friend_index.get(_normalize_friend_index_key(target_name)) or {})
    return {
        "seen": bool(entry),
        "visibleName": str(entry.get("visibleName") or ""),
        "stableKeys": list(entry.get("stableKeys") or []),
        "lastSeenAt": str(entry.get("lastSeenAt") or ""),
    }


def _account_blocked_target_status(item, account_failure):
    blocked_item = dict(item)
    affected_targets = set(account_failure.get("affectedTargets") or [])
    blocked_item.update(
        {
            "status": "account_blocked",
            "category": str(account_failure.get("category") or ""),
            "reason": str(account_failure.get("reason") or ""),
            "attemptCount": _coerce_attempt_count(account_failure),
            "lastAttemptAt": str(account_failure.get("lastAttemptAt") or ""),
            "accountFailureAffected": blocked_item.get("target") in affected_targets,
        }
    )
    return blocked_item


def _scheduled_send_time(user, target_name, send_window, now):
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


def _base_target_status(account, target_name, now):
    return {
        "target": target_name,
        "status": "",
        "message": "",
        "sentAt": "",
        "lastAttemptAt": "",
        "category": "",
        "reason": "",
        "attemptCount": 0,
        "scheduledAt": "",
        "friendIndex": _friend_index_status(account, target_name),
        "confirmationLevel": "",
        "confirmationSource": "",
        "confirmationDetail": "",
        "needsVerification": False,
        "legacyUnverified": False,
        "displaySentAt": "",
        "displayLastAttemptAt": "",
        "displayScheduledAt": "",
        "confirmationLabel": "",
        "categoryLabel": "",
    }


def _history_entry_is_strong_confirmed(history_entry, sent_at, now):
    return history_entry_is_strong_confirmed_today(history_entry, now)


def _format_short_time(raw_value, now):
    parsed = _parse_sent_at(raw_value, now.tzinfo)
    if not parsed:
        return ""
    if parsed.date() == now.date():
        return parsed.strftime("%H:%M:%S")
    return parsed.strftime("%m-%d %H:%M")


def _finalize_target_status(item, now):
    item = dict(item)
    item["displaySentAt"] = _format_short_time(item.get("sentAt"), now)
    item["displayLastAttemptAt"] = _format_short_time(item.get("lastAttemptAt"), now)
    item["displayScheduledAt"] = _format_short_time(item.get("scheduledAt"), now)
    source = str(item.get("confirmationSource") or "")
    category = str(item.get("category") or "")
    item["confirmationLabel"] = CONFIRMATION_LABELS.get(source, source or "-")
    item["categoryLabel"] = FAILURE_CATEGORY_LABELS.get(category, category or "-")
    return item


def _build_target_status(account, target_name, now, send_window):
    history = dict(account.get("message_history") or {})
    failure_queue = dict(account.get("failure_queue") or {})
    item = _base_target_status(account, target_name, now)

    history_entry = dict(history.get(target_name) or {})
    sent_at = _parse_sent_at(history_entry.get("sentAt"), now.tzinfo)
    if _history_entry_is_strong_confirmed(history_entry, sent_at, now):
        item.update(
            {
                "status": "sent",
                "message": str(history_entry.get("message") or ""),
                "sentAt": sent_at.isoformat(timespec="seconds"),
                "confirmationLevel": str(history_entry.get("confirmationLevel") or "strong"),
                "confirmationSource": str(history_entry.get("confirmationSource") or "browser_visible_count_increased"),
                "confirmationDetail": str(history_entry.get("confirmationDetail") or ""),
            }
        )
        return _finalize_target_status(item, now)

    failure_entry = dict(failure_queue.get(target_name) or {})
    last_attempt_at = _parse_sent_at(failure_entry.get("lastAttemptAt"), now.tzinfo)
    failure_is_today = bool(last_attempt_at and last_attempt_at.date() == now.date())

    if sent_at and sent_at.date() == now.date():
        confirmation_level = str(history_entry.get("confirmationLevel") or "legacy")
        confirmation_source = str(history_entry.get("confirmationSource") or "legacy_sentAt_only")
        confirmation_detail = str(history_entry.get("confirmationDetail") or "")
        legacy_unverified = not history_entry.get("confirmationLevel")
        if legacy_unverified:
            confirmation_detail = confirmation_detail or "旧格式发送账本缺少强确认字段，已降级为待核验。"
        item.update(
            {
                "status": "unconfirmed",
                "message": str(history_entry.get("message") or failure_entry.get("message") or ""),
                "sentAt": sent_at.isoformat(timespec="seconds"),
                "lastAttemptAt": last_attempt_at.isoformat(timespec="seconds") if failure_is_today else "",
                "category": str(failure_entry.get("category") or "send_unconfirmed"),
                "reason": str(failure_entry.get("reason") or confirmation_detail or "发送记录缺少强确认，需要核验。"),
                "attemptCount": int(failure_entry.get("attemptCount") or 0),
                "confirmationLevel": confirmation_level,
                "confirmationSource": confirmation_source,
                "confirmationDetail": confirmation_detail,
                "needsVerification": True,
                "legacyUnverified": legacy_unverified,
            }
        )
        return _finalize_target_status(item, now)

    if failure_is_today:
        category = str(failure_entry.get("category") or "")
        status = "unconfirmed" if category == "send_unconfirmed" else "failed"
        item.update(
            {
                "status": status,
                "message": str(failure_entry.get("message") or ""),
                "lastAttemptAt": last_attempt_at.isoformat(timespec="seconds"),
                "category": category,
                "reason": str(failure_entry.get("reason") or ""),
                "attemptCount": int(failure_entry.get("attemptCount") or 0),
                "confirmationLevel": str(failure_entry.get("confirmationLevel") or ("weak" if status == "unconfirmed" else "")),
                "confirmationSource": str(failure_entry.get("confirmationSource") or ""),
                "confirmationDetail": str(failure_entry.get("reason") or ""),
                "needsVerification": status == "unconfirmed",
            }
        )
        return _finalize_target_status(item, now)

    scheduled_at = None
    if send_window.get("enabled"):
        scheduled_at = _scheduled_send_time(account, target_name, send_window, now)
        if scheduled_at > now:
            item.update(
                {
                    "status": "pending",
                    "scheduledAt": scheduled_at.isoformat(timespec="seconds"),
                }
            )
            return _finalize_target_status(item, now)

    item.update(
        {
            "status": "unprocessed",
            "scheduledAt": scheduled_at.isoformat(timespec="seconds") if scheduled_at else "",
        }
    )
    return _finalize_target_status(item, now)


def _orphan_records(account, configured_targets):
    configured_target_set = {str(target) for target in configured_targets}
    history = dict(account.get("message_history") or {})
    failure_queue = dict(account.get("failure_queue") or {})
    orphan_history = sorted(str(target) for target in history if str(target) not in configured_target_set)
    orphan_failure = sorted(str(target) for target in failure_queue if str(target) not in configured_target_set)
    return orphan_history, orphan_failure


def get_send_console_snapshot(account_refs=None):
    allowed_refs = None if account_refs is None else {str(ref).strip() for ref in account_refs}
    accounts = [
        account
        for account in get_userData(force_reload=True)
        if account.get("enabled", True) and (allowed_refs is None or account.get("account_ref") in allowed_refs)
    ]
    send_window = _normalize_send_window()
    now = datetime.now(_schedule_timezone())

    summary = {
        "enabled_accounts": len(accounts),
        "total_targets": 0,
        "today_sent_targets": 0,
        "today_confirmed_targets": 0,
        "today_unconfirmed_targets": 0,
        "today_legacy_unverified_targets": 0,
        "today_failed_targets": 0,
        "today_pending_targets": 0,
        "today_unprocessed_targets": 0,
        "today_account_blocked_targets": 0,
        "today_attention_targets": 0,
        "today_remaining_targets": 0,
        "today_account_failures": 0,
        "today_account_paused": 0,
        "today_warning_count": 0,
        "orphan_history_records": 0,
        "orphan_failure_records": 0,
        "last_confirmed_at": "",
        "last_confirmed_display": "",
        "all_confirmed": False,
    }
    account_rows = []
    account_failure_pause_after = _account_failure_pause_after_attempts()

    for account in accounts:
        configured_targets = list(account.get("targets") or [])
        statuses = [_build_target_status(account, target_name, now, send_window) for target_name in configured_targets]
        confirmed_targets = [item for item in statuses if item["status"] == "sent"]
        sent_targets = confirmed_targets
        unconfirmed_targets = [item for item in statuses if item["status"] == "unconfirmed"]
        failed_targets = [item for item in statuses if item["status"] == "failed"]
        account_failure = _account_failure_entry_today(account, now)
        account_paused = bool(account_failure and _coerce_attempt_count(account_failure) >= account_failure_pause_after)
        account_blocked_targets = []
        if account_paused:
            account_blocked_targets = [
                _finalize_target_status(_account_blocked_target_status(item, account_failure), now)
                for item in statuses
                if item["status"] in {"pending", "unprocessed"}
            ]
            pending_targets = []
            unprocessed_targets = []
        else:
            pending_targets = [item for item in statuses if item["status"] == "pending"]
            unprocessed_targets = [item for item in statuses if item["status"] == "unprocessed"]
        friend_index_meta = dict(account.get("friend_index_meta") or {})
        friend_index_last_scan_at = _parse_sent_at(friend_index_meta.get("lastScanAt"), now.tzinfo)
        if friend_index_last_scan_at:
            friend_index_meta["lastScanAt"] = friend_index_last_scan_at.isoformat(timespec="seconds")
        friend_index_meta["missingTargets"] = list(friend_index_meta.get("missingTargets") or [])
        friend_index_meta["lastScanComplete"] = bool(friend_index_meta.get("lastScanComplete"))
        try:
            friend_index_meta["scannedCount"] = int(friend_index_meta.get("scannedCount") or 0)
        except (TypeError, ValueError):
            friend_index_meta["scannedCount"] = 0

        orphan_history, orphan_failure = _orphan_records(account, configured_targets)
        warnings = []
        if not configured_targets:
            warnings.append({"category": "no_targets", "message": "该启用账号没有配置目标，不能代表全部续上。"})
        if orphan_history:
            warnings.append({"category": "orphan_history", "message": f"有 {len(orphan_history)} 条发送账本不在当前目标列表中。"})
        if orphan_failure:
            warnings.append({"category": "orphan_failure", "message": f"有 {len(orphan_failure)} 条失败队列记录不在当前目标列表中。"})
        legacy_unverified_targets = [item for item in unconfirmed_targets if item.get("legacyUnverified")]
        attention_count = len(unconfirmed_targets) + len(failed_targets) + len(account_blocked_targets)
        pending_count = len(pending_targets) + len(unprocessed_targets)
        confirmed_times = [
            _parse_sent_at(item.get("sentAt"), now.tzinfo)
            for item in confirmed_targets
            if item.get("sentAt")
        ]
        confirmed_times = [item for item in confirmed_times if item]
        last_confirmed_at = max(confirmed_times).isoformat(timespec="seconds") if confirmed_times else ""
        if account_paused:
            account_state = "paused"
        elif attention_count:
            account_state = "attention"
        elif warnings:
            account_state = "warning"
        elif pending_count:
            account_state = "pending"
        else:
            account_state = "healthy"

        summary["total_targets"] += len(configured_targets)
        summary["today_sent_targets"] += len(sent_targets)
        summary["today_confirmed_targets"] += len(confirmed_targets)
        summary["today_unconfirmed_targets"] += len(unconfirmed_targets)
        summary["today_legacy_unverified_targets"] += len(legacy_unverified_targets)
        summary["today_failed_targets"] += len(failed_targets)
        summary["today_pending_targets"] += len(pending_targets)
        summary["today_unprocessed_targets"] += len(unprocessed_targets)
        summary["today_account_blocked_targets"] += len(account_blocked_targets)
        summary["today_attention_targets"] += attention_count
        summary["today_remaining_targets"] += (
            len(unconfirmed_targets)
            + len(failed_targets)
            + len(pending_targets)
            + len(unprocessed_targets)
            + len(account_blocked_targets)
        )
        summary["today_warning_count"] += len(warnings)
        summary["orphan_history_records"] += len(orphan_history)
        summary["orphan_failure_records"] += len(orphan_failure)
        if account_failure:
            summary["today_account_failures"] += 1
        if account_paused:
            summary["today_account_paused"] += 1
        if last_confirmed_at and (
            not summary["last_confirmed_at"] or last_confirmed_at > summary["last_confirmed_at"]
        ):
            summary["last_confirmed_at"] = last_confirmed_at

        account_rows.append(
            {
                "account_ref": str(account.get("account_ref") or ""),
                "unique_id": str(account.get("unique_id") or ""),
                "username": account.get("username") or "",
                "total_targets": len(configured_targets),
                "sent_targets": sent_targets,
                "confirmed_targets": confirmed_targets,
                "unconfirmed_targets": unconfirmed_targets,
                "legacy_unverified_targets": legacy_unverified_targets,
                "failed_targets": failed_targets,
                "pending_targets": pending_targets,
                "unprocessed_targets": unprocessed_targets,
                "account_blocked_targets": account_blocked_targets,
                "last_failure_reason": failed_targets[0]["reason"] if failed_targets else "",
                "last_unconfirmed_reason": unconfirmed_targets[0]["reason"] if unconfirmed_targets else "",
                "failure_queue": dict(account.get("failure_queue") or {}),
                "account_failure": account_failure,
                "account_paused": account_paused,
                "account_failure_pause_after": account_failure_pause_after,
                "state": account_state,
                "attention_count": attention_count,
                "pending_count": pending_count,
                "last_confirmed_at": last_confirmed_at,
                "last_confirmed_display": _format_short_time(last_confirmed_at, now),
                "friend_index_meta": friend_index_meta,
                "friend_index_count": len(dict(account.get("friend_index") or {})),
                "warnings": warnings,
                "orphan_history_records": orphan_history,
                "orphan_failure_records": orphan_failure,
            }
        )

    state_rank = {"paused": 0, "attention": 1, "warning": 2, "pending": 3, "healthy": 4}
    account_rows.sort(key=lambda row: (state_rank.get(row.get("state"), 9), str(row.get("username") or "")))

    summary["all_confirmed"] = bool(
        summary["total_targets"] > 0
        and summary["today_confirmed_targets"] == summary["total_targets"]
        and summary["today_remaining_targets"] == 0
        and summary["today_warning_count"] == 0
        and summary["orphan_history_records"] == 0
        and summary["orphan_failure_records"] == 0
    )
    summary["last_confirmed_display"] = _format_short_time(summary["last_confirmed_at"], now)

    return {
        "now": now.isoformat(timespec="seconds"),
        "nowDisplay": now.strftime("%m-%d %H:%M"),
        "summary": summary,
        "accounts": account_rows,
    }


def get_overview_snapshot(account_refs=None):
    send_console = get_send_console_snapshot(account_refs=account_refs)
    summary = dict(send_console["summary"])
    accounts = []
    for row in send_console["accounts"]:
        accounts.append(
            {
                "uniqueId": row["unique_id"],
                "displayName": row["username"],
                "state": row["state"],
                "total": row["total_targets"],
                "confirmed": len(row["confirmed_targets"]),
                "attention": row["attention_count"],
                "pending": row["pending_count"],
                "lastConfirmedAt": row["last_confirmed_at"],
            }
        )
    return {
        "now": send_console["now"],
        "schedule": get_schedule_snapshot(),
        "task": task_run_lock_status(),
        "summary": {
            "enabledAccounts": summary["enabled_accounts"],
            "total": summary["total_targets"],
            "confirmed": summary["today_confirmed_targets"],
            "unconfirmed": summary["today_unconfirmed_targets"],
            "failed": summary["today_failed_targets"],
            "blocked": summary["today_account_blocked_targets"],
            "attention": summary["today_attention_targets"],
            "pending": summary["today_pending_targets"],
            "unprocessed": summary["today_unprocessed_targets"],
            "remaining": summary["today_remaining_targets"],
            "warnings": summary["today_warning_count"],
            "lastConfirmedAt": summary["last_confirmed_at"],
            "allConfirmed": summary["all_confirmed"],
        },
        "accounts": accounts,
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


def get_ops_snapshot(account_refs=None):
    """Collect operational metrics for the dashboard.

    Every external call is individually guarded so the dashboard always
    renders, even when Docker or crontab are not available.
    """
    send_console = get_send_console_snapshot(account_refs=account_refs)
    return {
        "compose_root": str(compose_root()),
        "compose_file": str(compose_file_path() or ""),
        "containers": get_container_status(),
        "task_containers": get_task_container_rows(),
        "send_console": send_console,
        "task_lock": task_run_lock_status(),
        "daily_schedule": current_daily_schedule(),
        "schedule": get_schedule_snapshot(),
        "crontab": read_crontab(),
        "log_tail": read_log_tail(120),
        "image_present": _check_image_present(),
    }
