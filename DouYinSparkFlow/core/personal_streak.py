"""Personal sending preferences and durable daily dispatch claims."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import os
import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.msg_builder import FESTIVAL_WINDOW_END, FESTIVAL_WINDOW_START
from utils.config import get_config, get_userData


STORE_KEY = "personal_streak"
UTC = timezone.utc
MAX_START_FAILURE_RETRIES = 3
END_TIME_RE = re.compile(r"(?:(?:[01]\d|2[0-3]):[0-5]\d|24:00)")


class PreferenceConflict(ValueError):
    pass


def _now():
    return datetime.now(UTC)


def _users():
    from webui import users
    return users


def _people():
    users = _users()
    people = {"admin": {"username": "", "enabled": True, "account_refs": []}}
    for user in users.get_web_users():
        people[users.stable_user_id(user)] = user
    return people


def _owners(accounts, people):
    owners = {}
    for key, person in people.items():
        if key == "admin":
            continue
        for ref in person.get("account_refs") or []:
            owners[ref] = key if ref not in owners else "!conflict"
    return {str(a.get("account_ref") or ""): owners.get(str(a.get("account_ref") or ""), "admin") for a in accounts}


def _legacy_global_refs(raw):
    """Read only explicit legacy account mappings from the Web-user store."""
    mappings = raw.get("legacy_global_schedule") or {}
    if not isinstance(mappings, dict):
        return {}
    result = {}
    for key, value in mappings.items():
        if not isinstance(value, dict):
            continue
        refs = value.get("account_refs")
        if not isinstance(refs, (list, tuple)):
            continue
        result[str(key)] = {str(ref) for ref in refs if str(ref)}
    return result


def _principal_key(principal, people):
    if not principal:
        raise ValueError("请先登录")
    if principal.get("role") == "admin":
        return "admin"
    wanted = str(principal.get("username") or "").casefold()
    for key, person in people.items():
        if key != "admin" and str(person.get("username") or "").casefold() == wanted and person.get("enabled") and not person.get("registration_pending"):
            return key
    raise ValueError("当前账号不可用")


def next_run(profile, now=None):
    now = (now or _now()).astimezone(UTC)
    zone = ZoneInfo(profile["timezone"])
    local = now.astimezone(zone)
    hour, minute = map(int, profile["time"].split(":"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
    if candidate.astimezone(UTC) <= now:
        candidate = candidate + timedelta(days=1)
    # Round-trip normalizes a nonexistent local time during a DST transition.
    return candidate.astimezone(UTC)


def _end_minutes(value):
    text = str(value or "24:00")
    if not END_TIME_RE.fullmatch(text):
        raise ValueError("请选择有效的结束时间")
    if text == "24:00":
        return 24 * 60
    hour, minute = map(int, text.split(":"))
    return hour * 60 + minute


def _profile_end_time(profile):
    """Return the safe all-day default for data saved before end_time existed."""
    try:
        value = str((profile or {}).get("end_time") or "24:00")
        _end_minutes(value)
        return value
    except ValueError:
        return "24:00"


def _within_end_time(profile, now, *, due=None, claim=None):
    zone = ZoneInfo(profile["timezone"])
    local = now.astimezone(zone)
    if due is not None and due.astimezone(zone).date() != local.date():
        return False
    claim_date = str((claim or {}).get("local_date") or "")
    if claim_date and claim_date != local.date().isoformat():
        return False
    return local.hour * 60 + local.minute < _end_minutes(_profile_end_time(profile))


def _default():
    return {"enabled": False, "time": "18:00", "end_time": "24:00", "timezone": str(os.getenv("TZ") or "Asia/Shanghai"), "messages": [], "next_run_at": ""}


def has_global_message_templates(config):
    """Whether the current message-builder path has a configured source.

    This deliberately does not use ``build_message_candidates``: that helper
    supplies its historical "续火花" fallback, which personal inheritance must
    never turn into a send.  Its branch order mirrors the builder instead.
    """
    if not isinstance(config, dict):
        return False
    holiday = config.get("happyNewYear") or {}
    if (holiday.get("enabled") and FESTIVAL_WINDOW_START <= date.today() <= FESTIVAL_WINDOW_END):
        return bool(str(holiday.get("messageTemplate") or "").strip())
    strategy = config.get("sendStrategy") or {}
    variants = strategy.get("messageVariants") or []
    return any(str(item).strip() for item in variants) or bool(str(config.get("messageTemplate") or "").strip())


def preferences(principal):
    users = _users()
    with users._web_users_lock():
        people = _people()
        key = _principal_key(principal, people)
        profile = (users._load_raw().get(STORE_KEY) or {}).get(key) or _default()
        result = {field: deepcopy(profile.get(field)) for field in ("enabled", "time", "timezone", "messages", "next_run_at", "last_run")}
        result["end_time"] = _profile_end_time(profile)
        result["revision"] = str(profile.get("revision") or "")
        return result


def save_preferences(principal, *, enabled, at_time, timezone_name, messages, end_time=None, expected_revision=None):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(at_time)):
        raise ValueError("请选择有效的续火花时间")
    try:
        ZoneInfo(str(timezone_name))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("时区无效")
    if not isinstance(messages, list) or len(messages) > 20:
        raise ValueError("请填写 0–20 条消息，每行一条")
    messages = [str(message).strip() for message in messages]
    if any(not message or len(message) > 500 or "\x00" in message for message in messages):
        raise ValueError("每条消息应为 1–500 字")
    users = _users()
    with users._web_users_lock():
        people = _people()
        key = _principal_key(principal, people)
        accounts = get_userData(force_reload=True)
        owners = _owners(accounts, people)
        if enabled and not any(owner == key for owner in owners.values()):
            raise ValueError("请先添加或分配自己的抖音账号，再启用自动续火花")
        raw = users._load_raw()
        profiles = raw.setdefault(STORE_KEY, {})
        old = profiles.get(key) or {}
        selected_end_time = _profile_end_time(old) if end_time in (None, "") else str(end_time)
        end_minutes = _end_minutes(selected_end_time)
        start_hour, start_minute = map(int, str(at_time).split(":"))
        if end_minutes <= start_hour * 60 + start_minute:
            raise ValueError("结束时间必须晚于开始时间；暂不支持跨日")
        wanted = {"enabled": bool(enabled), "time": str(at_time), "end_time": selected_end_time, "timezone": str(timezone_name), "messages": messages}
        if expected_revision is not None and str(expected_revision) != str(old.get("revision") or ""):
            raise PreferenceConflict("设置已在另一页更新。你的输入已保留，请先核对已保存内容。")
        if not messages and not has_global_message_templates(get_config(force_reload=True)):
            raise ValueError("个人消息留空时，请先在设置→消息中配置全局固定模板或变体")
        if all((_profile_end_time(old) if field == "end_time" else old.get(field)) == value for field, value in wanted.items()):
            # Introducing the optional end-time control must not reset a legacy
            # plan's revision, pending claim or due time on an unchanged save.
            unchanged = deepcopy(old)
            unchanged["end_time"] = _profile_end_time(old)
            return unchanged
        wanted["revision"] = uuid.uuid4().hex
        wanted["next_run_at"] = next_run(wanted).isoformat(timespec="seconds") if enabled else ""
        if old.get("last_run"):
            wanted["last_run"] = deepcopy(old["last_run"])
        profiles[key] = wanted
        users._atomic_write_json(users.USERS_FILE, raw)
        return deepcopy(wanted)


def account_groups(config, accounts, *, manual=False, profile_key="", revision="", claim=""):
    users = _users()
    with users._web_users_lock():
        people = _people()
        raw = users._load_raw()
        profiles = deepcopy(raw.get(STORE_KEY) or {})
        legacy_refs = _legacy_global_refs(raw)
        owners = _owners(accounts, people)
    # Preserve standalone installations and their existing sender behavior.
    if not profile_key and not profiles and len(people) == 1:
        return [(config, accounts)]
    groups = {}
    for account in accounts:
        ref = str(account.get("account_ref") or "")
        key = owners.get(ref, "admin")
        person, profile = people.get(key), profiles.get(key)
        legacy = (
            profile is None
            and bool(account.get("enabled", True))
            and ref in legacy_refs.get(key, set())
        )
        if not person or not person.get("enabled") or person.get("registration_pending"):
            continue
        if profile_key:
            pending = (profile or {}).get("claim") or {}
            if key != profile_key or not profile or not profile.get("enabled") or profile.get("revision") != revision or pending.get("token") != claim:
                continue
        elif not manual and (key != "admin" or profile is not None) and not legacy:
            # Personal accounts are never swept up by the old global cron job.
            continue
        # An explicit saved empty personal message list inherits global text;
        # the verified Hangzhou legacy mapping still opts in existing accounts.
        if key != "admin" and not legacy and not ((profile or {}).get("messages") or (profile or {}).get("revision")):
            continue
        if profile is not None and not profile.get("messages") and not has_global_message_templates(config):
            continue
        if key not in groups:
            effective = deepcopy(config)
            if profile and profile.get("messages"):
                effective["messageTemplate"] = profile["messages"][0]
                effective.setdefault("sendStrategy", {})["messageVariants"] = list(profile["messages"])
                effective["literalMessageTemplates"] = True
                effective.setdefault("happyNewYear", {})["enabled"] = False
            if profile:
                effective["scheduleTimezone"] = profile["timezone"]
                hour = int(profile["time"].split(":")[0])
                end_time = _profile_end_time(profile)
                effective["dailySendWindow"] = {
                    "enabled": True,
                    "startHour": hour,
                    "endHour": _end_minutes(end_time) // 60,
                    "endTime": end_time,
                    "scheduleIntervalMinutes": 1,
                    "exactTime": profile["time"],
                }
            groups[key] = (effective, [])
        scoped = dict(account)
        scoped["_personal_scope"] = {
            "owner": key,
            "revision": (profile or {}).get("revision", ""),
            "claim": claim if profile_key else "",
            # Captured from the encrypted preference profile, not account input.
            # Retry quota reads this provenance even when now is UTC.
            "schedule_timezone": (profile or {}).get("timezone", ""),
            "legacy_global_schedule": legacy,
        }
        groups[key][1].append(scoped)
    return list(groups.values())


def authorized(account):
    scope = account.get("_personal_scope")
    if not isinstance(scope, dict):
        return True
    from core.identity import account_login_generation, account_uid
    live = next((item for item in get_userData(force_reload=True)
                 if item.get("account_ref") == account.get("account_ref")), None)
    if not live or not live.get("enabled", True):
        return False
    if account_uid(live) != account_uid(account) or account_login_generation(live) != account_login_generation(account):
        return False
    users = _users()
    with users._web_users_lock():
        people = _people()
        owners = _owners([account], people)
        key = owners.get(str(account.get("account_ref") or ""))
        person = people.get(key) or {}
        if key != scope.get("owner") or not person.get("enabled") or person.get("registration_pending"):
            return False
        raw = users._load_raw()
        profile = (raw.get(STORE_KEY) or {}).get(key)
        if scope.get("legacy_global_schedule"):
            return profile is None and str(account.get("account_ref") or "") in _legacy_global_refs(raw).get(key, set())
        profile = profile or {}
        if profile.get("revision", "") != scope.get("revision", ""):
            return False
        pending = profile.get("claim") or {}
        if scope.get("claim") and (not profile.get("enabled") or pending.get("token") != scope["claim"] or float(pending.get("expires", 0)) <= _now().timestamp()):
            return False
    return True


def authorized_schedule_timezone(account):
    """Return the profile timezone only after the personal scope is live."""
    scope = account.get("_personal_scope")
    if not isinstance(scope, dict) or scope.get("legacy_global_schedule"):
        return None
    if not authorized(account):
        return None
    name = str(scope.get("schedule_timezone") or "").strip()
    if not name:
        return None
    users = _users()
    with users._web_users_lock():
        profile = ((users._load_raw().get(STORE_KEY) or {}).get(scope.get("owner")) or {})
        if str(profile.get("timezone") or "").strip() != name:
            return None
    try:
        return ZoneInfo(str(profile["timezone"]))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None


def protocol_send_deadline(account, now=None):
    """Return a claimed personal job's verified end boundary in epoch ms.

    The protocol child cannot read the encrypted web-user profile safely, so it
    receives only this checked, absolute deadline.  No deadline is supplied for
    manual or global work, preserving their existing no-claim behavior.
    """
    scope = account.get("_personal_scope") or {}
    if not isinstance(scope, dict) or not scope.get("claim"):
        return None
    now = (now or _now()).astimezone(UTC)
    # Revalidate the scope and token after any queueing before handing the
    # child a deadline.  Manual/global accounts have no claim and return above.
    if not authorized(account):
        return None
    users = _users()
    with users._web_users_lock():
        profile = ((users._load_raw().get(STORE_KEY) or {}).get(scope.get("owner")) or {})
        claim = profile.get("claim") or {}
        if (
            not profile
            or not profile.get("enabled")
            or profile.get("revision", "") != scope.get("revision", "")
            or claim.get("token") != scope.get("claim")
            or not _within_end_time(profile, now, claim=claim)
        ):
            return None
        try:
            claim_expires = float(claim["expires"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if claim_expires <= now.timestamp():
            return None
        local = now.astimezone(ZoneInfo(profile["timezone"]))
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        deadline = midnight + timedelta(minutes=_end_minutes(_profile_end_time(profile)))
        return min(
            int(deadline.astimezone(UTC).timestamp() * 1000),
            int(claim_expires * 1000),
        )


def within_send_window(account, now=None):
    """Apply the end-time gate only to claimed automatic personal jobs.

    Manual sends deliberately have no claim token and retain their existing
    bypass semantics.  The browser sender invokes this immediately before
    Enter, after any queueing or UI work that could cross the boundary.
    """
    scope = account.get("_personal_scope") or {}
    if not scope.get("claim"):
        return True
    now = (now or _now()).astimezone(UTC)
    users = _users()
    with users._web_users_lock():
        profile = ((users._load_raw().get(STORE_KEY) or {}).get(scope.get("owner")) or {})
        claim = profile.get("claim") or {}
        if not profile or claim.get("token") != scope.get("claim"):
            return False
        return _within_end_time(profile, now, claim=claim)


def claim_due(now=None):
    now = (now or _now()).astimezone(UTC)
    users = _users()
    with users._web_users_lock():
        people = _people()
        raw = users._load_raw()
        profiles = raw.get(STORE_KEY) or {}
        owners = _owners(get_userData(force_reload=True), people)
        for key, profile in sorted(profiles.items(), key=lambda item: str(item[1].get("next_run_at") or "9999")):
            person = people.get(key) or {}
            if not profile.get("enabled") or not person.get("enabled") or person.get("registration_pending") or not any(owner == key for owner in owners.values()):
                continue
            try:
                due = datetime.fromisoformat(profile["next_run_at"])
                claim = profile.get("claim") or {}
                if due > now or float(claim.get("expires", 0)) > now.timestamp():
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            if not _within_end_time(profile, now, due=due):
                profile.pop("claim", None)
                profile["next_run_at"] = next_run(profile, now).isoformat(timespec="seconds")
                users._atomic_write_json(users.USERS_FILE, raw)
                continue
            token = uuid.uuid4().hex
            profile["claim"] = {
                "token": token,
                "expires": now.timestamp() + 1800,
                "local_date": now.astimezone(ZoneInfo(profile["timezone"])).date().isoformat(),
            }
            users._atomic_write_json(users.USERS_FILE, raw)
            return {"key": key, "revision": profile["revision"], "token": token}
    return None


def finish_claim(job, *, busy=False, outcome="success", now=None):
    """Record a claimed job's outcome without ever retrying an uncertain send.

    ``failed`` is reserved for a Popen failure, before a child existed.  Only
    those failures receive a small, durable retry budget.  A child exit other
    than the documented busy code has an unknown delivery result and advances
    to the ordinary next run instead of risking duplicate sends.
    """
    now = (now or _now()).astimezone(UTC)
    users = _users()
    with users._web_users_lock():
        raw = users._load_raw()
        profile = (raw.get(STORE_KEY) or {}).get(job["key"])
        if not profile or profile.get("revision") != job["revision"] or (profile.get("claim") or {}).get("token") != job["token"]:
            return
        claim = profile.pop("claim", None) or {}
        retry_at = now + timedelta(minutes=1)
        can_retry = _within_end_time(profile, retry_at, claim=claim)
        if busy:
            profile["next_run_at"] = (retry_at if can_retry else next_run(profile, now)).isoformat(timespec="seconds")
        elif outcome == "failed":
            failures = int(profile.get("start_failure_count") or 0) + 1
            retry_scheduled = failures < MAX_START_FAILURE_RETRIES and can_retry
            profile["last_run"] = {
                "status": "failed", "at": now.isoformat(timespec="seconds"),
                "retry_scheduled": retry_scheduled, "retry_count": failures,
            }
            if retry_scheduled:
                profile["start_failure_count"] = failures
                profile["next_run_at"] = retry_at.isoformat(timespec="seconds")
            else:
                profile.pop("start_failure_count", None)
                profile["next_run_at"] = next_run(profile, now).isoformat(timespec="seconds")
        else:
            profile.pop("start_failure_count", None)
            status = "unknown" if outcome == "unknown" else "completed"
            profile["last_run"] = {"status": status, "at": now.isoformat(timespec="seconds")}
            profile["next_run_at"] = next_run(profile, now).isoformat(timespec="seconds")
        users._atomic_write_json(users.USERS_FILE, raw)


def renew_claim(job, now=None):
    now = (now or _now()).astimezone(UTC)
    users = _users()
    with users._web_users_lock():
        raw = users._load_raw()
        profile = (raw.get(STORE_KEY) or {}).get(job["key"]) or {}
        pending = profile.get("claim") or {}
        if not profile.get("enabled") or profile.get("revision") != job["revision"] or pending.get("token") != job["token"]:
            return
        if float(pending.get("expires", 0)) < now.timestamp() + 1500:
            pending["expires"] = now.timestamp() + 1800
            users._atomic_write_json(users.USERS_FILE, raw)
