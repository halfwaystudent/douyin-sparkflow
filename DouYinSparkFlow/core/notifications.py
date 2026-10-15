"""Run-result notifications: immediate failure notices and the daily summary.

Decisions come from the same per-target state the run report uses. The dedupe
keys and summary dates live in logs/notify_state.json so a restart on the same
local day does not repeat a notice.

"Final point" of a day is the end-of-window fallback run (SPARKFLOW_FALLBACK_PHASE=1,
the last scheduled run of the window), or the single scheduled run when the
window is disabled. Only at that point are unconfirmed targets counted as failed.
"""
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import FileLock

from core import send_state, streak_state
from utils import notify
from utils.logger import setup_logger


logger = setup_logger(level=logging.DEBUG)

NOTIFY_STATE_PATH = Path("logs") / "notify_state.json"
STATE_KEEP_DAYS = 7
SUMMARY_LINE_LIMIT = 30
RUN_FAILED_CATEGORY = "run_failed"
UNFINISHED_CATEGORY = "unfinished"
SKIPPED_STATUSES = {"skipped_task_in_progress", "skipped_no_enabled_accounts"}

CATEGORY_LABELS = {
    "login_required": "需要重新登录",
    "protocol_login_required": "需要重新登录",
    "browser_login_required": "需要重新登录",
    "expired_cookies": "Cookie 已失效，需要重新登录",
    "missing_cookies": "缺少登录信息，需要重新登录",
    "cookie_format_invalid": "登录信息格式异常",
    "cookie_auth_missing": "登录信息不完整",
    "account_identity_mismatch": "登录账号与配置不一致，需人工核对",
    "identity_confirmation_required": "需要人工确认账号身份",
    "identity_confirmation_invalid": "账号身份确认失败",
    "login_verification_pending": "登录状态待验证",
    "send_rejected": "消息被平台拒绝",
    "friend_not_found": "好友列表中找不到该目标",
    "protocol_user_blocked": "对方已拒收或拉黑",
    "protocol_user_not_in_conversation": "未找到与对方的会话",
    "protocol_check_message_not_pass": "发送结果校验未通过",
    "protocol_check_message_self_visible": "发送结果校验未通过",
    "protocol_check_conversation_not_pass": "会话校验未通过",
    "send_unconfirmed": "发送结果待人工核验",
    "network_unavailable": "网络不可用",
    "douyin_unreachable": "抖音页面无法访问",
    "service_unavailable": "登录服务不可用",
    "no_targets": "没有可发送的目标",
    RUN_FAILED_CATEGORY: "整次运行异常退出",
    UNFINISHED_CATEGORY: "当天未发送成功",
}


def category_label(category):
    key = str(category or "").strip()
    return CATEGORY_LABELS.get(key, "发送失败")


def _as_local_date(value, tz):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz).date()


def _account_name(account):
    return str(account.get("username") or "").strip() or "未命名账号"


def _account_key(account):
    return str(account.get("account_ref") or account.get("username") or "unknown")


def _preflight_category(account, today, tz):
    health = dict(account.get("account_health") or {})
    if health.get("healthy") is not False:
        return ""
    if _as_local_date(health.get("checkedAt"), tz) != today:
        return ""
    return str(health.get("category") or "account_preflight")


def _target_failure_category(state, today, tz, *, final):
    """Failure category for a target attempted today, or "" if it is not failed yet.

    Mid-day, a failed_retryable target only counts once its fallback ran today
    or it needs manual verification (fallback still pending is not reported).
    At the final point every failed_retryable target attempted today counts,
    because browser-mode accounts have no fallback path.
    """
    if _as_local_date(state.get("lastAttemptAt"), tz) != today:
        return ""
    status = state.get("status")
    category = str(state.get("lastErrorCategory") or "send_failed")
    if status == streak_state.STATE_FAILED_TERMINAL:
        return category
    if status == streak_state.STATE_FAILED_RETRYABLE:
        if final:
            return category
        fallback_today = _as_local_date(state.get("fallbackAt"), tz) == today
        if fallback_today or state.get("needsVerification"):
            return category
    return ""


def _is_confirmed_today(account, target_name, now):
    # Sent-but-unverified counts as success for notifications (brief Q4 / A9).
    return bool(
        streak_state.is_send_confirmed(account, target_name, now)
        or streak_state.is_sent_unverified(account, target_name, now)
        or send_state.target_is_strong_confirmed_today(account, target_name, now)
    )


def _target_rows(users, now, today, tz, *, final):
    """One row per enabled target: confirmed flag, account, display name, category.

    At the final point every unconfirmed target gets a category (UNFINISHED when
    nothing failed explicitly), so success + failure always equals the total.
    """
    rows = []
    for account in users:
        account_key = _account_key(account)
        name = _account_name(account)
        preflight = _preflight_category(account, today, tz)
        for target in account.get("targets") or []:
            target_name = streak_state.normalize_target_name(target)
            if not target_name:
                continue
            if _is_confirmed_today(account, target_name, now):
                rows.append(
                    {"confirmed": True, "account_key": account_key, "account": name,
                     "target_key": target_name, "display": target_name, "category": "", "source": ""}
                )
                continue
            state = streak_state.target_state(account, target_name, now)
            display = str(state.get("displayName") or target_name)
            category = _target_failure_category(state, today, tz, final=final)
            source = "target" if category else ""
            if not category and preflight:
                category, source = preflight, "preflight"
            if not category and final:
                category, source = UNFINISHED_CATEGORY, "target"
            rows.append(
                {"confirmed": False, "account_key": account_key, "account": name,
                 "target_key": target_name, "display": display, "category": category, "source": source}
            )
    return rows


def _failure_entries(users, rows, today, tz):
    entries = []
    for account in users:
        preflight = _preflight_category(account, today, tz)
        if preflight:
            entries.append(
                {
                    "key": f"{today.isoformat()}|{_account_key(account)}|-|{preflight}",
                    "account": _account_name(account),
                    "target": "",
                    "category": preflight,
                }
            )
    for row in rows:
        if row["confirmed"] or not row["category"] or row["source"] != "target":
            continue
        entries.append(
            {
                "key": f"{today.isoformat()}|{row['account_key']}|{row['target_key']}|{row['category']}",
                "account": row["account"],
                "target": row["display"],
                "category": row["category"],
            }
        )
    return entries


def _failure_line(entry):
    if entry["category"] == RUN_FAILED_CATEGORY:
        return "- 整次运行异常退出，请查看运行日志"
    label = category_label(entry["category"])
    if entry["target"]:
        return f"- 账号「{entry['account']}」 目标「{entry['target']}」：{label}"
    return f"- 账号「{entry['account']}」：预检失败，{label}"


def _failure_message(entries, now_local):
    lines = [_failure_line(entry) for entry in entries]
    text = f"时间：{now_local.strftime('%m-%d %H:%M')}\n" + "\n".join(lines)
    return notify.make_message(
        "发送失败提醒",
        text,
        event="failure",
        date=now_local.date().isoformat(),
    )


def _summary_message(rows, reason, today):
    ok_count = sum(1 for row in rows if row["confirmed"])
    failed = [row for row in rows if not row["confirmed"]]
    lines = [
        f"日期：{today.isoformat()}（{reason}）",
        f"启用目标：{len(rows)} 个",
        f"成功目标：{ok_count} 个",
        f"失败目标：{len(failed)} 个（未确认的目标计入失败）",
    ]
    if failed:
        lines.append("失败明细：")
        for row in failed[:SUMMARY_LINE_LIMIT]:
            if row["source"] == "preflight":
                lines.append(f"- 账号「{row['account']}」（全部目标）：{category_label(row['category'])}")
            else:
                lines.append(f"- 账号「{row['account']}」 目标「{row['display']}」：{category_label(row['category'])}")
        if len(failed) > SUMMARY_LINE_LIMIT:
            lines.append(f"……另有 {len(failed) - SUMMARY_LINE_LIMIT} 项未列出")
    return notify.make_message(
        "每日汇总",
        "\n".join(lines),
        event="daily_summary",
        date=today.isoformat(),
    )


def _load_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        logger.warning("notify state unreadable, starting fresh: %s", path)
        data = {}
    if not isinstance(data, dict):
        data = {}
    dedupe = data.get("dedupe") if isinstance(data.get("dedupe"), dict) else {}
    summaries = data.get("summaries") if isinstance(data.get("summaries"), dict) else {}
    return {"dedupe": dict(dedupe), "summaries": dict(summaries)}


def _prune_state(state, today):
    cutoff = (today - timedelta(days=STATE_KEEP_DAYS)).isoformat()
    state["dedupe"] = {
        key: value
        for key, value in state["dedupe"].items()
        if str(key).split("|", 1)[0] >= cutoff
    }
    state["summaries"] = {
        day: value for day, value in state["summaries"].items() if str(day) >= cutoff
    }


def _save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def notify_run_finished(
    run_id,
    run_status,
    config,
    users,
    *,
    schedule_tz,
    send_window,
    manual_run=False,
    fallback_run=False,
    now=None,
    state_path=None,
):
    """Post-run hook. Swallows every error: notifications never change run results.

    ``send_window`` must be the output of tasks._normalize_send_window (invalid
    windows are already disabled there).
    """
    try:
        return _notify_run_finished(
            run_status,
            config,
            users,
            schedule_tz=schedule_tz,
            send_window=dict(send_window or {}),
            manual_run=manual_run,
            fallback_run=fallback_run,
            now=now,
            state_path=state_path,
        )
    except Exception as exc:
        logger.warning(
            "run notification skipped run=%s: %s",
            run_id,
            notify.redact_text(f"{type(exc).__name__}: {exc}"),
        )
        return None


def _notify_run_finished(
    run_status, config, users, *, schedule_tz, send_window, manual_run, fallback_run, now, state_path
):
    if str(run_status or "") in SKIPPED_STATUSES:
        return None
    channels_config = dict((config or {}).get("notifications") or {})
    if not notify.enabled_channel_names(channels_config):
        return None
    enabled_users = [dict(user) for user in users or [] if user.get("enabled", True)]
    if not enabled_users:
        return None

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local_now = now.astimezone(schedule_tz)
    today = local_now.date()
    path = Path(state_path) if state_path is not None else NOTIFY_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    now_iso = now.isoformat(timespec="seconds")

    window_enabled = bool(send_window.get("enabled"))
    # The fallback run is the end-of-window point; with the window disabled the
    # single scheduled run is. Manual runs that are not the fallback never reach it.
    final = bool(fallback_run or (not manual_run and not window_enabled))

    failure_message = None
    summary_message = None
    with FileLock(f"{path}.lock", timeout=30):
        state = _load_state(path)
        _prune_state(state, today)
        rows = _target_rows(enabled_users, now, today, schedule_tz, final=final)
        entries = _failure_entries(enabled_users, rows, today, schedule_tz)
        if str(run_status or "") == "failed":
            entries.append(
                {
                    "key": f"{today.isoformat()}|-|-|{RUN_FAILED_CATEGORY}",
                    "account": "",
                    "target": "",
                    "category": RUN_FAILED_CATEGORY,
                }
            )
        new_entries = []
        for entry in entries:
            if entry["key"] not in state["dedupe"]:
                state["dedupe"][entry["key"]] = now_iso
                new_entries.append(entry)
        if new_entries:
            failure_message = _failure_message(new_entries, local_now)

        today_key = today.isoformat()
        if rows and today_key not in state["summaries"]:
            all_confirmed = all(row["confirmed"] for row in rows)
            # All-confirmed may fire on scheduled runs and the fallback run, never on
            # a plain manual run. The final point always sends.
            confirmed_trigger = all_confirmed and (not manual_run or fallback_run)
            if final or confirmed_trigger:
                if all_confirmed:
                    reason = "全部目标已确认成功"
                elif window_enabled:
                    reason = "发送窗口末次兜底运行结束，未确认的目标计为失败"
                else:
                    reason = "定时运行结束，未确认的目标计为失败"
                summary_message = _summary_message(rows, reason, today)
                state["summaries"][today_key] = now_iso

        if failure_message or summary_message:
            _save_state(path, state)

    # Keys are reserved before sending: a failed send is not retried (no retry queue).
    if failure_message:
        notify.send_message(failure_message, channels_config)
    if summary_message:
        notify.send_message(summary_message, channels_config)
    # Renames and missing targets the send scan recorded today (A7/A8). They
    # share their keys with the patrol, so each is pushed once per day.
    identity_sent = _push_identity_entries(
        _identity_event_entries(enabled_users, today),
        channels_config,
        path=path,
        today=today,
        now_iso=now_iso,
        local_now=local_now,
        title="目标变化提醒",
        event="identity",
    )
    return {
        "failure": bool(failure_message),
        "summary": bool(summary_message),
        "final": final,
        "identity": identity_sent,
    }


# --------------------------------------------------------------------------
# account patrol and target identity notices

IDENTITY_EVENT_RENAMED = "renamed"
IDENTITY_EVENT_MISSING = "missing"
IDENTITY_EVENT_AMBIGUOUS = "rename_ambiguous"
PATROL_LOGIN_CATEGORIES = frozenset(
    {"login_required", "account_identity_mismatch", "missing_cookies"}
)


def login_failure_key(today, account_key, category):
    """Shared with the preflight failure key, so one login problem is one push."""
    return f"{today.isoformat()}|{account_key}|-|{category}"


def _identity_event_entries(users, today, *, account_keys=None):
    """Today's rename/missing events, keyed by account, category and identity.

    The event identity is the opaque uid token (or the nickname for an unbound
    target) recorded by core.friend_identity; no full uid ever reaches a key or
    a line.
    """
    today_text = today.isoformat()
    entries = []
    for account in users or []:
        if not account.get("enabled", True):
            continue
        account_key = _account_key(account)
        if account_keys is not None and account_key not in account_keys:
            continue
        for event in account.get("identity_events") or []:
            if not isinstance(event, dict) or str(event.get("date") or "") != today_text:
                continue
            category = str(event.get("category") or "")
            if category not in (
                IDENTITY_EVENT_RENAMED,
                IDENTITY_EVENT_MISSING,
                IDENTITY_EVENT_AMBIGUOUS,
            ):
                continue
            identity = str(event.get("identity") or event.get("targetName") or "")
            entries.append(
                {
                    "key": f"{today_text}|identity|{account_key}|{category}|{identity}",
                    "account": _account_name(account),
                    "category": category,
                    "target": str(event.get("targetName") or ""),
                    "oldName": str(event.get("oldName") or ""),
                    "newName": str(event.get("newName") or ""),
                    "uidSuffix": str(event.get("uidSuffix") or "")[-4:],
                }
            )
    return entries


def _identity_line(entry):
    category = entry["category"]
    if category == "login":
        return f"- 账号「{entry['account']}」：登录态失效，{category_label(entry['loginCategory'])}"
    suffix = f"（尾号 {entry['uidSuffix']}）" if entry.get("uidSuffix") else ""
    if category == IDENTITY_EVENT_RENAMED:
        old_name = entry.get("oldName") or entry.get("target")
        new_name = entry.get("newName") or entry.get("target")
        return f"- 账号「{entry['account']}」 目标已改名：「{old_name}」→「{new_name}」{suffix}，已自动跟随"
    if category == IDENTITY_EVENT_AMBIGUOUS:
        old_name = entry.get("oldName") or entry.get("target")
        new_name = entry.get("newName") or "新昵称"
        return f"- 账号「{entry['account']}」 目标「{old_name}」的新昵称「{new_name}」对应多个好友 uid，未自动跟随，请在 Web 核对"
    target = entry.get("target") or entry.get("oldName")
    return f"- 账号「{entry['account']}」 目标「{target}」{suffix}不在好友列表中（目标缺失）"


def _push_identity_entries(entries, channels_config, *, path, today, now_iso, local_now, title, event):
    """Reserve unseen keys in the shared notify state, then send one message."""
    if not entries or not notify.enabled_channel_names(channels_config):
        return False
    new_entries = []
    with FileLock(f"{path}.lock", timeout=30):
        state = _load_state(path)
        _prune_state(state, today)
        for entry in entries:
            if entry["key"] in state["dedupe"]:
                continue
            state["dedupe"][entry["key"]] = now_iso
            new_entries.append(entry)
        if new_entries:
            _save_state(path, state)
    if not new_entries:
        return False
    text = f"时间：{local_now.strftime('%m-%d %H:%M')}\n" + "\n".join(
        _identity_line(entry) for entry in new_entries
    )
    message = notify.make_message(title, text, event=event, date=today.isoformat())
    # Keys are reserved before sending: a failed push is only logged by
    # utils.notify and never retried or raised into the caller.
    notify.send_message(message, channels_config)
    return True


def notify_patrol_finished(results, config, users, *, schedule_tz, now=None, state_path=None):
    """Push what the account patrol found. Never raises; never sends a summary.

    Only problems are pushed: a login that is no longer usable, a missing
    target and an automatically followed rename. One account/target/category
    is pushed at most once per local day, shared with the run notices.
    """
    try:
        channels_config = dict((config or {}).get("notifications") or {})
        if not notify.enabled_channel_names(channels_config):
            return False
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        local_now = now.astimezone(schedule_tz)
        today = local_now.date()
        path = Path(state_path) if state_path is not None else NOTIFY_STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)

        by_ref = {}
        for account in users or []:
            by_ref[str(account.get("account_ref") or "")] = account
        entries = []
        account_keys = set()
        for result in results or []:
            account = by_ref.get(str(result.get("accountRef") or "")) or {
                "account_ref": result.get("accountRef"),
                "username": result.get("account"),
            }
            account_key = _account_key(account)
            account_keys.add(account_key)
            login = dict(result.get("login") or {})
            category = str(login.get("category") or "")
            if login.get("ok") is False and category in PATROL_LOGIN_CATEGORIES:
                entries.append(
                    {
                        "key": login_failure_key(today, account_key, category),
                        "account": _account_name(account),
                        "category": "login",
                        "loginCategory": category,
                    }
                )
        entries.extend(_identity_event_entries(users, today, account_keys=account_keys))
        return _push_identity_entries(
            entries,
            channels_config,
            path=path,
            today=today,
            now_iso=now.isoformat(timespec="seconds"),
            local_now=local_now,
            title="账号巡查提醒",
            event="patrol",
        )
    except Exception as exc:  # noqa: BLE001 - a push never changes the patrol
        logger.warning(
            "patrol notification skipped: %s",
            notify.redact_text(f"{type(exc).__name__}: {exc}"),
        )
        return False
