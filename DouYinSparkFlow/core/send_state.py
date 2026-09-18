from __future__ import annotations

from datetime import datetime

from core import streak_state


def parse_sent_at(raw_value, local_tz):
    if not raw_value:
        return None
    raw = str(raw_value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)


def _receipt_is_strong(receipt):
    """Delegate to the state machine so one receipt cannot get two verdicts.

    This module used to accept a missing ``call`` field while the state machine
    required ``call == "message_send"``; a receipt whose intercepted call could
    not be identified is not proof that a message was sent, so the stricter
    definition wins.
    """
    return streak_state.receipt_is_strong(receipt)


def history_entry_is_strong_confirmed_today(entry, now):
    entry = dict(entry or {})
    sent_at = parse_sent_at(entry.get("sentAt"), now.tzinfo)
    if (
        not sent_at
        or not streak_state.same_calendar_day(sent_at, now)
        or bool(entry.get("needsVerification"))
    ):
        return False
    if entry.get("status") == "confirmed" and entry.get("confirmationLevel") == "strong":
        return True
    return _receipt_is_strong(entry.get("serverReceipt"))


def target_is_strong_confirmed_today(account, target_name, now):
    if streak_state.is_send_confirmed(account, target_name, now):
        return True
    history = dict(account.get("message_history") or {})
    return history_entry_is_strong_confirmed_today(history.get(target_name), now)


def history_entry_is_today(entry, now):
    sent_at = parse_sent_at(dict(entry or {}).get("sentAt"), now.tzinfo)
    return bool(sent_at and streak_state.same_calendar_day(sent_at, now))
