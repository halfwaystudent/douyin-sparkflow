"""Derive setup progress from the current account and personal preferences."""

from core.identity import account_uid, identity_state
from core.streak_state import LOGIN_FAILURE_CATEGORIES


def setup_state(accounts, preferences, selected_ref=""):
    account = next((item for item in accounts if item.get("account_ref") == selected_ref), None)
    selection_missing = bool(selected_ref and account is None)
    if not selected_ref:
        account = next((item for item in accounts if account_uid(item) and item.get("cookies")), None)
        account = account or next((item for item in accounts if item.get("cookies")), None)
        account = account or next(iter(accounts), None)
    check = (account or {}).get("login_verification") or {}
    health = (account or {}).get("account_health") or {}
    verification = str(check.get("state") or "")
    conflict = bool(account and (identity_state(account) == "conflict" or verification == "identity_conflict"))
    login_failed = bool(account and (account.get("login_required") or account.get("needs_relogin") or verification == "login_required"))
    if account and account.get("patrol_login_failure"):
        login_failed = True
    if health.get("healthy") is False and health.get("category") in LOGIN_FAILURE_CATEGORIES | {"account_identity_mismatch", "identity_conflict", "expired_cookies", "missing_cookies"}:
        login_failed = True
    bound = bool(account and account_uid(account) and account.get("cookies"))
    ready = bound and not conflict and not login_failed
    pending = bool(ready and (verification in {"verifying", "unverified"} or account.get("pending_login_verification")))
    has_targets = bool(ready and (account.get("targets") or account.get("group_targets")))
    has_messages = bool(preferences.get("messages"))
    can_finish = bool(ready and has_targets and has_messages)
    if not account:
        reason = "之前选择的账号已移除或权限已变化，请重新选择自己的账号。" if selection_missing else "先添加自己的抖音账号。"
    elif conflict:
        reason = "保存的登录身份与绑定账号不一致，请重新登录原账号。"
    elif login_failed:
        reason = "这个账号需要重新登录，请先更新登录态。"
    elif not bound:
        reason = "这个账号还没有完成可信身份绑定，请先重新登录并核对身份。"
    elif not has_targets:
        reason = "先选择并保存发送目标。"
    elif not has_messages:
        reason = "先保存自己的发送内容、时间和时区。"
    else:
        reason = ""
    return {
        "setup_account": account, "ready": ready, "has_targets": has_targets,
        "has_messages": has_messages, "can_finish": can_finish,
        "login_pending": pending, "login_verification_state": verification,
        "login_reason": str(check.get("reason") or ""), "setup_blocked_reason": reason,
        "automatic_enabled": bool(preferences.get("enabled")),
        "suggested_step": 1 if not ready else (2 if not has_targets else (3 if not has_messages else 4)),
    }


def requested_step(value, state):
    try:
        step = max(1, min(4, int(value)))
    except (ValueError, TypeError):
        step = state["suggested_step"]
    if step > 1 and not state["ready"]:
        return 1
    if step > 2 and not state["has_targets"]:
        return 2
    if step > 3 and not state["has_messages"]:
        return 3
    return step
