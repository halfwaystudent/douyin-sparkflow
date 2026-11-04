"""One cooled retry for a narrowly identified browser 8610 rejection.

This is not a change to receipt acceptance or the terminal-category set.
The reservation lives outside replaceable target outcome rows.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
from core import streak_state

KEY = 'refusal_retry_reservations'
COOLDOWN = timedelta(minutes=20)


def _timezone_from_scope(account, schedule_timezone=None):
    """Choose retry-day provenance, never an account-provided timezone field."""
    if schedule_timezone is not None:
        if isinstance(schedule_timezone, str):
            try:
                return ZoneInfo(schedule_timezone.strip())
            except Exception:
                return None
        if not callable(getattr(schedule_timezone, 'utcoffset', None)):
            return None
        return schedule_timezone
    scope = account.get("_personal_scope") or {}
    if isinstance(scope, dict):
        name = str(scope.get("schedule_timezone") or "").strip()
        if name:
            try:
                return ZoneInfo(name)
            except Exception:
                return None
    try:
        return ZoneInfo(os.getenv('SPARKFLOW_TIMEZONE') or os.getenv('TZ') or 'Asia/Shanghai')
    except Exception:
        return timezone.utc


def local_now(now, schedule_timezone=None, *, account=None):
    return now.astimezone(_timezone_from_scope(account or {}, schedule_timezone))


def _reservation_timezone(value, fallback):
    name = str(value.get('timezone') or '').strip()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            return None
    return fallback


def _reservation_used_on_day(value, now, policy_timezone):
    """Keep a reservation attached to its original day policy across edits."""
    if not isinstance(value, dict):
        return True
    stored_day = value.get('day')
    if not isinstance(stored_day, str):
        return True
    reservation_timezone = _reservation_timezone(value, policy_timezone)
    if reservation_timezone is None:
        return True
    try:
        # The day, timestamp, and provenance must describe the same recorded
        # reservation.  Any disagreement is unreadable quota state, not a
        # reason to open another attempt.
        if date.fromisoformat(stored_day).isoformat() != stored_day:
            return True
        reserved_at = datetime.fromisoformat(value['at'])
        if reserved_at.tzinfo is None or reserved_at > now:
            return True
        if reserved_at.astimezone(reservation_timezone).date().isoformat() != stored_day:
            return True
    except (KeyError, ValueError, TypeError, OverflowError):
        return True
    if value.get('timezone'):
        # Keep both sides of a timezone edit active: the original policy day
        # protects the reservation while its old local day is active, and the
        # same reservation instant protects the new policy's current day.
        return (
            stored_day == now.astimezone(reservation_timezone).date().isoformat()
            or reserved_at.astimezone(policy_timezone).date() == now.astimezone(policy_timezone).date()
        )
    # Legacy rows had no timezone provenance.  Their recorded day remains
    # compatible, and a recent ISO timestamp is conservatively protected across
    # a timezone edit rather than becoming an extra retry.
    if stored_day == now.astimezone(policy_timezone).date().isoformat():
        return True
    return now.astimezone(timezone.utc) - reserved_at.astimezone(timezone.utc) < timedelta(days=1)


def _identity_refs(account, target):
    refs = {streak_state.resolve_target_ref(account, target)}
    binding = streak_state.target_uid_binding(account, target)
    if binding.get('uid'):
        refs.add('peer:' + str(binding['uid']).strip())
    if binding.get('secUid'):
        refs.add('sec:' + str(binding['secUid']).strip())
    return refs - {''}


def used_today(account, target, now, schedule_timezone=None):
    reservations = account.get(KEY) or {}
    if not isinstance(reservations, dict):
        return True  # unreadable quota must not open another attempt
    policy_timezone = _timezone_from_scope(account, schedule_timezone)
    if policy_timezone is None:
        return True
    target_refs = _identity_refs(account, target)
    for ref, value in reservations.items():
        if not isinstance(value, dict):
            return True
        aliases = value.get('identityRefs', [ref])
        if not isinstance(aliases, list) or any(not isinstance(item, str) for item in aliases):
            return True
        if target_refs.intersection([ref] + aliases) or streak_state.target_keys_match(account, ref, target):
            if _reservation_used_on_day(value, now, policy_timezone):
                return True
    return False


def _integer(value, expected):
    return type(value) is int and value == expected


def explicit_8610(account, target, now):
    state = streak_state.target_state(account, target, now)
    entry = streak_state.failure_entry(account, target, now.tzinfo)
    if state.get('status') != streak_state.STATE_FAILED_TERMINAL or state.get('lastErrorCategory') != 'send_rejected':
        return False
    if entry.get('category') != 'send_rejected' or entry.get('lastAttemptAt') != state.get('lastAttemptAt'):
        return False
    r = entry.get('serverReceipt')
    if not isinstance(r, dict):
        return False
    codes, rejection = r.get('protoCheckCodes'), r.get('rejection')
    return bool(
        r.get('call') == 'message_send' and r.get('ok') is False
        and r.get('protoParsed') is True and r.get('protoOk') is False
        and r.get('jsonOk') is not True
        and _integer(r.get('protoStatusCode'), 0)
        and _integer(r.get('protoSendStatus'), 3)
        and _integer(r.get('protoCheckCode'), 2)
        and isinstance(codes, dict) and set(codes) == {'status_code', 'raw_check_code'}
        and _integer(codes.get('status_code'), 8610) and _integer(codes.get('raw_check_code'), 2)
        and isinstance(rejection, dict) and rejection.get('key') == 'status_code'
        and _integer(rejection.get('value'), 8610)
    )


def rejection_precedes_today(account, target, now, schedule_timezone=None):
    """A valid earlier-day refusal is yesterday's outcome, not today's retry.

    Malformed, naive or future timestamps must not masquerade as a day reset.
    """
    state = streak_state.target_state(account, target, now)
    if state.get('lastErrorCategory') != 'send_rejected':
        return False
    try:
        attempted_at = datetime.fromisoformat(state['lastAttemptAt'])
        policy_timezone = _timezone_from_scope(account, schedule_timezone)
        return bool(attempted_at.tzinfo and policy_timezone and attempted_at.astimezone(policy_timezone).date() < now.astimezone(policy_timezone).date())
    except (KeyError, ValueError, TypeError):
        return False


def eligible(account, target, now, schedule_timezone=None):
    policy_timezone = _timezone_from_scope(account, schedule_timezone)
    if (policy_timezone is None
            or used_today(account, target, now, policy_timezone)
            or not explicit_8610(account, target, now)):
        return False
    entry = streak_state.failure_entry(account, target, now.tzinfo)
    try:
        at = datetime.fromisoformat(entry['lastAttemptAt'])
        if at.tzinfo is None:
            return False
        return at.astimezone(policy_timezone).date() == now.astimezone(policy_timezone).date() and now - at >= COOLDOWN
    except (KeyError, ValueError, TypeError):
        return False


def reserve(account, target, now, run_id, schedule_timezone=None):
    """Caller must persist atomically and independently read back before send."""
    policy_timezone = _timezone_from_scope(account, schedule_timezone)
    if policy_timezone is None or used_today(account, target, now, policy_timezone):
        return None
    today = now.astimezone(policy_timezone).date().isoformat()
    timezone_name = getattr(policy_timezone, 'key', None) or policy_timezone.tzname(now) or ''
    bucket = dict(account.get(KEY) or {})
    ref = streak_state.resolve_target_ref(account, target)
    bucket[ref] = {'day': today, 'timezone': timezone_name,
                   'at': now.astimezone(timezone.utc).isoformat(timespec='seconds'),
                   'runId': run_id, 'identityRefs': sorted(_identity_refs(account, target))}
    account[KEY] = bucket
    return ref
