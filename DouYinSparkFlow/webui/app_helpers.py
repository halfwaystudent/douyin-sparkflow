from datetime import datetime, timedelta, timezone

from core import streak_state
from core.send_state import history_entry_is_strong_confirmed_today, parse_sent_at
from utils.config import normalize_unique_id


def find_account(accounts, unique_id):
    normalized = normalize_unique_id(unique_id)
    for account in accounts:
        if normalize_unique_id(account.get("unique_id")) == normalized:
            return account
    return None


def is_account_enabled(account):
    return bool(account.get("enabled", True))


def coerce_int(value, default, minimum=0):
    try:
        return max(minimum, int(str(value).strip()))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _schedule_timezone():
    return timezone(timedelta(hours=8), name="Asia/Shanghai")


def _parse_sent_at(raw_value):
    return parse_sent_at(raw_value, _schedule_timezone())


def _history_entry_strong_confirmed_today(entry):
    return history_entry_is_strong_confirmed_today(
        entry,
        datetime.now(_schedule_timezone()),
    )


def _target_sent_today(account, target_name):
    if streak_state.is_send_confirmed(account, target_name):
        return True
    entry = streak_state.history_entry(
        account,
        target_name,
        _schedule_timezone(),
    )
    return _history_entry_strong_confirmed_today(entry)


def _target_unconfirmed_today(account, target_name):
    now = datetime.now(_schedule_timezone())
    entry = streak_state.history_entry(account, target_name, now.tzinfo)
    sent_at = _parse_sent_at(entry.get("sentAt"))
    if (
        sent_at
        and sent_at.date() == now.date()
        and not _history_entry_strong_confirmed_today(entry)
        and not streak_state.is_send_confirmed(account, target_name, now)
    ):
        return True
    failure_entry = streak_state.failure_entry(account, target_name, now.tzinfo)
    last_attempt_at = _parse_sent_at(failure_entry.get("lastAttemptAt"))
    return bool(
        last_attempt_at
        and last_attempt_at.date() == now.date()
        and str(failure_entry.get("category") or "") == "send_unconfirmed"
    )


def mark_target_unconfirmed(
    account,
    target_name,
    *,
    reason="manual_reset_possible_false_positive",
    force=False,
    now=None,
):
    now_dt = (now or datetime.now(_schedule_timezone())).replace(
        microsecond=0,
    )
    now = now_dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    history = dict(account.get("message_history") or {})
    existing = streak_state.history_entry(account, target_name, now_dt.tzinfo)
    sent_at = _parse_sent_at(existing.get("sentAt"))
    today = now_dt.date()
    if existing and sent_at and sent_at.date() != today and not force:
        return False

    previous_status = existing.get("status") or ("legacy_sentAt_only" if existing else "missing_history")
    message = str(existing.get("message") or "")
    history_entry = {
        **existing,
        "message": message,
        "sentAt": existing.get("sentAt") or now,
        "status": "unconfirmed",
        "confirmationLevel": existing.get("confirmationLevel") or "legacy",
        "confirmationSource": existing.get("confirmationSource") or "manual_reset",
        "confirmationDetail": existing.get("confirmationDetail") or "已手动标记为待核验/待补发。",
        "needsVerification": True,
        "resetAt": now,
        "resetReason": reason,
        "previousStatus": previous_status,
    }

    queue = dict(account.get("failure_queue") or {})
    existing_failure = streak_state.failure_entry(
        account,
        target_name,
        now_dt.tzinfo,
    )
    queue_entry = {
        "category": "send_unconfirmed",
        "reason": reason,
        "message": message,
        "firstAttemptAt": existing_failure.get("firstAttemptAt") or now,
        "lastAttemptAt": now,
        "attemptCount": int(existing_failure.get("attemptCount") or 0) + 1,
        "lastRunMode": "manual_reset",
        "confirmationLevel": history_entry.get("confirmationLevel"),
        "confirmationSource": history_entry.get("confirmationSource"),
    }
    state = streak_state.mark_unconfirmed(
        account,
        target_name,
        reason=reason,
        now=now_dt,
        detail=history_entry.get("confirmationDetail") or "",
    )
    if (
        str(state.get("status") or "")
        != streak_state.STATE_FAILED_RETRYABLE
        or str(state.get("lastErrorCategory") or "") != "send_unconfirmed"
        or streak_state._parse_time(state.get("resetAt")) != now_dt
    ):
        return False
    history[target_name] = history_entry
    queue[target_name] = queue_entry
    account["message_history"] = history
    account["failure_queue"] = queue
    return True


def manual_retry_config(config):
    """Config for a single-target manual retry: one task, no anti-burst delay."""
    retry_config = dict(config)
    retry_config["taskCount"] = 1
    strategy = dict(retry_config.get("sendStrategy") or {})
    strategy["accountStartDelaySecondsMin"] = 0
    strategy["accountStartDelaySecondsMax"] = 0
    retry_config["sendStrategy"] = strategy
    return retry_config
