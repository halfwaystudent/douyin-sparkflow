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

from filelock import FileLock, Timeout as FileLockTimeout

from core import streak_state
from core.send_state import history_entry_is_strong_confirmed_today, parse_sent_at
from core.tasks import (
    TEMPORARY_ACCOUNT_FAILURE_CATEGORIES,
    _temporary_account_failure_cooldown_minutes,
)
from utils.config import (
    get_app_settings,
    get_config,
    get_userData,
    repo_root,
    update_config,
)

logger = logging.getLogger(__name__)

TASK_ALREADY_RUNNING = -2

TASK_SCHEDULE_MARKERS = (
    "docker compose run --rm task",
    "docker compose run --rm douyin",
    "main.py --doTask",
    "run_scheduled_task.sh",
)
HOST_CRONTAB_PATH = Path("/host-spool-cron/root")
# The scheduler container mounts ./DouYinSparkFlow/logs at /app/logs, so trigger
# output written here stays readable from both the scheduler and the web
# container (the panel). /var/log is not mounted and silently diverges.
CRON_LOG_PATH = "/app/logs/douyin-sparkflow.log"
WINDOWED_SCHEDULE_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})/(\d+)m$", re.IGNORECASE)

CONFIRMATION_LABELS = {
    "cdp_message_send_receipt": "服务端回执",
    "browser_visible_count_increased": "页面回显",
    "protocol_send_receipt": "协议发送回执",
    "streak_verified": "续火花已核验",
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
        if exc.errno in (errno.EPERM, errno.EACCES):
            return True
        if getattr(exc, "winerror", None) is not None:
            # Windows reports an invalid or already-exited pid with a variety of
            # winerror values (87 ERROR_INVALID_PARAMETER, 11 ERROR_BAD_FORMAT,
            # 1168 ERROR_NOT_FOUND, ...). None of them mean the process is alive.
            return False
        if exc.errno == errno.ESRCH:
            return False
        raise
    return True


def _parse_lock_pid(raw):
    text = str(raw or "").strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and payload.get("pid") is not None:
        try:
            return int(payload["pid"])
        except (TypeError, ValueError):
            return None
    try:
        return int(text.splitlines()[0])
    except (IndexError, TypeError, ValueError):
        return None


def task_run_lock_status():
    lock_path = repo_root() / "logs" / "task.run.lock"
    guard = FileLock(f"{lock_path}.guard", timeout=0)
    try:
        guard.acquire()
    except FileLockTimeout:
        locked = True
    else:
        locked = False
        guard.release()

    if not lock_path.exists():
        return {
            "running": locked,
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
        age_seconds = max(
            0,
            int(datetime.now(timezone.utc).timestamp() - lock_path.stat().st_mtime),
        )
    except OSError:
        return {
            "running": locked,
            "path": str(lock_path),
            "pid": pid,
            "ageSeconds": 0,
            "stale": not locked,
            "staleReason": "lock_stat_failed" if not locked else "",
            "staleRemoved": False,
        }

    if locked:
        return {
            "running": True,
            "path": str(lock_path),
            "pid": pid,
            "ageSeconds": age_seconds,
            "stale": False,
            "staleReason": "",
            "staleRemoved": False,
        }

    stale_reason = (
        "owner_pid_missing"
        if pid is not None and not _pid_is_alive(pid)
        else "file_lock_released"
    )
    return {
        "running": False,
        "path": str(lock_path),
        "pid": pid,
        "ageSeconds": age_seconds,
        "stale": True,
        "staleReason": stale_reason,
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
            "SPARKFLOW_FALLBACK_PHASE": "1",
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
        if (
            start_hour not in range(24)
            or end_hour not in range(1, 25)
            or end_hour <= start_hour
        ):
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


def strip_douyin_schedule_lines(crontab_text):
    """Return the crontab text without any douyin task line."""
    kept = [
        raw_line.rstrip("\n")
        for raw_line in str(crontab_text or "").splitlines()
        if not any(marker in raw_line for marker in TASK_SCHEDULE_MARKERS)
    ]
    normalized = "\n".join(line for line in kept if line.strip())
    if normalized:
        normalized += "\n"
    return normalized


def douyin_schedule_lines(crontab_text=None):
    text = read_crontab() if crontab_text is None else crontab_text
    return [
        line
        for line in str(text or "").splitlines()
        if any(marker in line for marker in TASK_SCHEDULE_MARKERS)
    ]


def schedule_line_kind(line):
    """Classify a douyin task line as a windowed schedule or a single fixed run."""
    fields = str(line or "").split()
    if len(fields) < 5:
        return "unknown"
    minute, hour = fields[0], fields[1]
    if "/" in minute or "-" in hour or "," in hour:
        return "window"
    return "fixed"


def get_schedule_alignment():
    """Compare the configured send window with the task lines that are live.

    ``dailySendWindow.enabled`` is false both for "no automatic sending" and for
    the single fixed-time mode, whose time is carried by the task line itself
    (see ``current_daily_schedule``). So the drift worth reporting is a spool
    that still holds window-style task lines while no window is configured.
    """
    window = dict(get_config(force_reload=True).get("dailySendWindow") or {})
    enabled = bool(window.get("enabled"))
    lines = douyin_schedule_lines()
    kinds = sorted({schedule_line_kind(line) for line in lines})
    if enabled:
        aligned = bool(lines) and kinds == ["window"]
        detail = (
            "配置与实际任务行一致"
            if aligned
            else ("配置已启用发送窗口，但 spool 中没有窗口式任务行" if not lines else "配置已启用发送窗口，但 spool 中的任务行形态不一致")
        )
    else:
        aligned = not lines or kinds == ["fixed"]
        if not lines:
            detail = "未配置发送窗口，spool 中也没有发送任务行"
        elif kinds == ["fixed"]:
            detail = "按单次固定时间调度（该模式的时间保存在任务行内）"
        else:
            detail = "配置未启用发送窗口，但 spool 中仍存在窗口式任务行"
    return {
        "windowEnabled": enabled,
        "configLabel": _format_window_schedule(window) if enabled else "",
        "fixedLabel": current_daily_schedule() if not enabled else "",
        "lines": lines,
        "kinds": kinds,
        "aligned": aligned,
        "detail": detail,
    }


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
        end_hour = schedule["endHour"]
        if end_hour == 24:
            if schedule["startHour"] < 23:
                updated.append(
                    f"*/{schedule['scheduleIntervalMinutes']} "
                    f"{schedule['startHour']}-22 * * * "
                    f"{scheduled_command} >> {CRON_LOG_PATH} 2>&1"
                )
            updated.append(
                f"0-58/{schedule['scheduleIntervalMinutes']} 23 * * * "
                f"{scheduled_command} >> {CRON_LOG_PATH} 2>&1"
            )
            updated.append(
                "59 23 * * * "
                f"{fallback_command} >> {CRON_LOG_PATH} 2>&1"
            )
        else:
            updated.append(
                f"*/{schedule['scheduleIntervalMinutes']} {schedule['startHour']}-{end_hour - 1} * * * "
                f"{scheduled_command} >> {CRON_LOG_PATH} 2>&1"
            )
            updated.append(
                f"0 {end_hour} * * * "
                f"{fallback_command} >> {CRON_LOG_PATH} 2>&1"
            )
    else:
        updated.append(
            f"{schedule['minute']} {schedule['hour']} * * * "
            f"{scheduled_command} >> {CRON_LOG_PATH} 2>&1"
        )

    normalized = "\n".join(line for line in updated if line.strip())
    if normalized:
        normalized += "\n"
    return normalized


def persist_schedule_config(time_string):
    parsed = parse_schedule_string(time_string)

    def mutate(config):
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
        return None, True

    update_config(mutate, force_reload=True)


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
        lines = douyin_schedule_lines()
        kinds = {schedule_line_kind(line) for line in lines}
        if not lines or kinds == {"fixed"}:
            # No window configured, and either nothing is scheduled or a single
            # fixed run carries its own time in the task line: leave it alone.
            return subprocess.CompletedProcess(
                args=["sync-daily-schedule"],
                returncode=0,
                stdout="schedule disabled; existing crontab left unchanged",
                stderr="",
            )
        # Window-style task lines survive while no window is configured: that
        # combination keeps sending on an old window, so reconcile to "off".
        try:
            cleaned = strip_douyin_schedule_lines(read_crontab())
            if running_in_container() and HOST_CRONTAB_PATH.parent.exists():
                HOST_CRONTAB_PATH.write_text(cleaned, encoding="utf-8")
                detail = "host spool"
            else:
                subprocess.run(
                    ["crontab", "-"],
                    input=cleaned,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                detail = "crontab"
            logger.warning(
                "No send window is configured but %s stale window task line(s) were live; removed them from the %s",
                len(lines),
                detail,
            )
            return subprocess.CompletedProcess(
                args=["sync-daily-schedule"],
                returncode=0,
                stdout=f"schedule disabled; removed {len(lines)} stale window task line(s) from the {detail}",
                stderr="",
            )
        except Exception as exc:
            logger.error("sync_daily_schedule_from_config failed: %s", exc)
            return _empty_result(stderr=str(exc))

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
    end_hour = int(window["endHour"])
    for hour in range(int(window["startHour"]), end_hour):
        for minute in range(0, 60, interval):
            if end_hour == 24 and hour == 23 and minute == 59:
                continue
            candidates.append(now.replace(hour=hour, minute=minute, second=0, microsecond=0))
    fallback_at = now.replace(
        hour=23 if end_hour == 24 else end_hour,
        minute=59 if end_hour == 24 else 0,
        second=0,
        microsecond=0,
    )
    candidates.append(fallback_at)
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


def _account_failure_pause_active(account_failure, now, pause_after):
    if _coerce_attempt_count(account_failure) < pause_after:
        return False
    category = str(account_failure.get("category") or "")
    if category not in TEMPORARY_ACCOUNT_FAILURE_CATEGORIES:
        return True
    last_attempt_at = _parse_sent_at(
        account_failure.get("lastAttemptAt"),
        now.tzinfo,
    )
    if not last_attempt_at:
        return True
    elapsed_seconds = (now - last_attempt_at).total_seconds()
    return elapsed_seconds < _temporary_account_failure_cooldown_minutes() * 60


def _account_failure_entry_today(account, now):
    entry = dict(account.get("account_failure") or {})
    last_attempt_at = _parse_sent_at(entry.get("lastAttemptAt"), now.tzinfo)
    category = str(entry.get("category") or "")
    cooldown_active = bool(
        last_attempt_at
        and category in TEMPORARY_ACCOUNT_FAILURE_CATEGORIES
        and now
        < last_attempt_at
        + timedelta(minutes=_temporary_account_failure_cooldown_minutes())
    )
    if last_attempt_at and (
        last_attempt_at.date() == now.date() or cooldown_active
    ):
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
    return " ".join(raw.split()).strip().casefold()


def _friend_index_status(account, target_name):
    friend_index = dict(account.get("friend_index") or {})
    normalized_target = _normalize_friend_index_key(target_name)
    entry = {}
    for key, record in friend_index.items():
        if not isinstance(record, dict):
            continue
        normalized_names = {
            _normalize_friend_index_key(key),
            _normalize_friend_index_key(record.get("visibleName")),
        }
        if normalized_target in normalized_names:
            entry = dict(record)
            break
    return {
        "seen": bool(entry),
        "visibleName": str(entry.get("visibleName") or ""),
        "stableKeys": list(entry.get("stableKeys") or []),
        "lastSeenAt": str(entry.get("lastSeenAt") or ""),
    }


def _account_blocked_target_status(account, item, account_failure):
    blocked_item = dict(item)
    target_name = str(blocked_item.get("target") or "")
    account_failure_affected = any(
        streak_state.target_keys_match(account, target_name, affected_target)
        for affected_target in (account_failure.get("affectedTargets") or [])
    )
    blocked_item.update(
        {
            "status": "account_blocked",
            "category": str(account_failure.get("category") or ""),
            "reason": str(account_failure.get("reason") or ""),
            "attemptCount": _coerce_attempt_count(account_failure),
            "lastAttemptAt": str(account_failure.get("lastAttemptAt") or ""),
            "accountFailureAffected": account_failure_affected,
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
        "targetRef": streak_state.resolve_target_ref(account, target_name),
        "sendState": streak_state.target_state(account, target_name, now).get("status") or "",
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
    item = _base_target_status(account, target_name, now)
    state = streak_state.target_state(account, target_name, now)

    history_entry = streak_state.history_entry(account, target_name, now.tzinfo)
    sent_at = _parse_sent_at(history_entry.get("sentAt"), now.tzinfo)
    if streak_state.is_streak_verified(account, target_name, now):
        item.update(
            {
                "status": "sent",
                "message": str(history_entry.get("message") or ""),
                "sentAt": sent_at.isoformat(timespec="seconds") if sent_at else "",
                "confirmationLevel": "verified",
                "confirmationSource": "streak_verified",
                "confirmationDetail": str(state.get("lastEvidenceDetail") or ""),
                "needsVerification": False,
            }
        )
        return _finalize_target_status(item, now)
    if (
        str(state.get("status") or "")
        == streak_state.STATE_FAILED_RETRYABLE
        and str(state.get("lastErrorCategory") or "") == "send_unconfirmed"
    ):
        last_attempt_at = _parse_sent_at(
            state.get("lastAttemptAt"),
            now.tzinfo,
        )
        item.update(
            {
                "status": "unconfirmed",
                "message": str(
                    history_entry.get("message")
                    or state.get("lastEvidenceDetail")
                    or ""
                ),
                "sentAt": (
                    sent_at.isoformat(timespec="seconds") if sent_at else ""
                ),
                "lastAttemptAt": (
                    last_attempt_at.isoformat(timespec="seconds")
                    if last_attempt_at
                    else ""
                ),
                "category": "send_unconfirmed",
                "reason": str(
                    state.get("lastErrorReason")
                    or "manual reset requires verification"
                ),
                "attemptCount": int(state.get("attemptCount") or 0),
                "confirmationLevel": "weak",
                "confirmationSource": str(
                    state.get("confirmationSource") or "manual_reset"
                ),
                "confirmationDetail": str(
                    state.get("lastEvidenceDetail") or ""
                ),
                "needsVerification": True,
            }
        )
        return _finalize_target_status(item, now)
    if streak_state.is_send_confirmed(account, target_name, now):
        state_sent_at = _parse_sent_at(state.get("sentAt"), now.tzinfo)
        item.update(
            {
                "status": "sent",
                "message": str(history_entry.get("message") or ""),
                "sentAt": (
                    state_sent_at.isoformat(timespec="seconds")
                    if state_sent_at
                    else (sent_at.isoformat(timespec="seconds") if sent_at else "")
                ),
                "confirmationLevel": "strong",
                "confirmationSource": str(
                    state.get("confirmationSource") or "protocol_send_receipt"
                ),
                "confirmationDetail": str(state.get("lastEvidenceDetail") or ""),
                "needsVerification": False,
            }
        )
        return _finalize_target_status(item, now)
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

    failure_entry = streak_state.failure_entry(account, target_name, now.tzinfo)
    last_attempt_at = _parse_sent_at(failure_entry.get("lastAttemptAt"), now.tzinfo)
    failure_is_today = bool(last_attempt_at and last_attempt_at.date() == now.date())

    if sent_at and sent_at.date() == now.date():
        confirmation_level = str(history_entry.get("confirmationLevel") or "legacy")
        confirmation_source = str(history_entry.get("confirmationSource") or "legacy_sentAt_only")
        confirmation_detail = str(history_entry.get("confirmationDetail") or "")
        legacy_unverified = not history_entry.get("confirmationLevel")
        if legacy_unverified:
            confirmation_detail = confirmation_detail or "旧格式发送账本缺少强确认字段，已降级为待核验。"
        # Only a real weak-evidence send counts as "sent with page echo"; a record
        # that carries nothing but a timestamp (or was manually reset) still needs
        # verification.
        page_echo_evidence = bool(
            not legacy_unverified
            and confirmation_level == "weak"
            and confirmation_source
            not in ("", "legacy_sentAt_only", "manual_reset")
        )
        item.update(
            {
                "status": "sent_page_echo" if page_echo_evidence else "unconfirmed",
                "message": str(history_entry.get("message") or failure_entry.get("message") or ""),
                "sentAt": sent_at.isoformat(timespec="seconds"),
                "lastAttemptAt": last_attempt_at.isoformat(timespec="seconds") if failure_is_today else "",
                "category": str(failure_entry.get("category") or "send_unconfirmed"),
                "reason": str(
                    failure_entry.get("reason")
                    or confirmation_detail
                    or (
                        "已发出，但只取得页面回显这一弱证据，未取得服务端强确认。"
                        if page_echo_evidence
                        else "发送记录缺少强确认，需要核验。"
                    )
                ),
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
    configured_targets = [str(target) for target in configured_targets]
    configured_target_set = set(configured_targets)
    configured_target_refs = {
        streak_state.target_ref_for_stored_key(account, target)
        for target in configured_targets
        if streak_state.target_ref_for_stored_key(account, target)
    }

    def is_configured(key):
        if str(key) in configured_target_set:
            return True
        return (
            streak_state.target_ref_for_stored_key(account, key)
            in configured_target_refs
        )

    history = dict(account.get("message_history") or {})
    failure_queue = dict(account.get("failure_queue") or {})
    orphan_history = sorted(
        str(target)
        for target in history
        if not is_configured(target)
    )
    orphan_failure = sorted(
        str(target)
        for target in failure_queue
        if not is_configured(target)
    )
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
        "today_page_echo_targets": 0,
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
        # A target that was sent and only produced the weak page-echo evidence is
        # finished for today (the engine will not resend it), so it belongs to
        # the "sent" side of the board, not to "needs attention".
        page_echo_targets = [item for item in statuses if item["status"] == "sent_page_echo"]
        sent_targets = confirmed_targets + page_echo_targets
        unconfirmed_targets = [item for item in statuses if item["status"] == "unconfirmed"]
        failed_targets = [item for item in statuses if item["status"] == "failed"]
        account_health = dict(account.get("account_health") or {})
        preflight_failed = account_health.get("healthy") is False
        account_failure = _account_failure_entry_today(account, now)
        failure_paused = _account_failure_pause_active(
            account_failure,
            now,
            account_failure_pause_after,
        )
        account_paused = bool(failure_paused or preflight_failed)
        pause_entry = (
            account_failure
            if failure_paused
            else {
                "category": str(account_health.get("category") or "account_preflight"),
                "reason": str(
                    account_health.get("reason")
                    or "account health preflight failed"
                ),
                "attemptCount": 0,
                "lastAttemptAt": str(account_health.get("checkedAt") or ""),
                "affectedTargets": [],
            }
        )
        account_blocked_targets = []
        if account_paused:
            account_blocked_targets = [
                _finalize_target_status(
                    _account_blocked_target_status(account, item, pause_entry),
                    now,
                )
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
        if preflight_failed:
            warnings.append(
                {
                    "category": "account_preflight",
                    "message": (
                        "账号预检失败，已暂停："
                        f"{pause_entry.get('reason') or pause_entry.get('category')}"
                    ),
                }
            )
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
        summary["today_page_echo_targets"] += len(page_echo_targets)
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
                "page_echo_targets": page_echo_targets,
                "page_echo_count": len(page_echo_targets),
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
                "account_health": account_health,
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
                "pageEcho": row["page_echo_count"],
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
            "pageEcho": summary["today_page_echo_targets"],
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


def recent_trigger_lines(limit=5):
    """Most recent scheduling-trigger log lines.

    Trigger output now lands in the mounted log file, so the panel can show
    whether the configured window actually fired instead of forcing the
    operator onto the host shell.
    """
    try:
        tail = read_log_tail(400) or []
    except Exception:
        logger.warning("recent_trigger_lines could not read the log tail", exc_info=True)
        return []
    markers = ("AUTO_TRIGGER", "run_scheduled_task", "scheduled send", "unsent fallback")
    hits = [line for line in tail if any(marker in line for marker in markers)]
    return hits[-max(1, int(limit)) :]


def _guarded_schedule_alignment():
    try:
        return get_schedule_alignment()
    except Exception:
        logger.warning("get_schedule_alignment failed", exc_info=True)
        return {
            "windowEnabled": False,
            "configLabel": "",
            "fixedLabel": "",
            "lines": [],
            "kinds": [],
            "aligned": False,
            "detail": "无法读取调度状态",
        }


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
        "schedule_alignment": _guarded_schedule_alignment(),
        "recent_triggers": recent_trigger_lines(),
        "schedule": get_schedule_snapshot(),
        "crontab": read_crontab(),
        "log_tail": read_log_tail(120),
        "image_present": _check_image_present(),
    }
