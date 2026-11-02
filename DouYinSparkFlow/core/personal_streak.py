"""Personal sending preferences and durable daily dispatch claims."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from utils.config import get_userData


STORE_KEY = "personal_streak"
UTC = timezone.utc


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


def _default():
    return {"enabled": False, "time": "18:00", "timezone": str(os.getenv("TZ") or "Asia/Shanghai"), "messages": [], "next_run_at": ""}


def preferences(principal):
    users = _users()
    with users._web_users_lock():
        people = _people()
        key = _principal_key(principal, people)
        profile = (users._load_raw().get(STORE_KEY) or {}).get(key) or _default()
        return {field: deepcopy(profile.get(field)) for field in ("enabled", "time", "timezone", "messages", "next_run_at")}


def save_preferences(principal, *, enabled, at_time, timezone_name, messages):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(at_time)):
        raise ValueError("请选择有效的续火花时间")
    try:
        ZoneInfo(str(timezone_name))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("时区无效")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 20:
        raise ValueError("请填写 1–20 条消息，每行一条")
    messages = [str(message).strip() for message in messages]
    if any(not message or len(message) > 500 or "\x00" in message for message in messages):
        raise ValueError("每条消息应为 1–500 字")
    wanted = {"enabled": bool(enabled), "time": str(at_time), "timezone": str(timezone_name), "messages": messages}
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
        if all(old.get(field) == value for field, value in wanted.items()):
            return deepcopy(old)
        wanted["revision"] = uuid.uuid4().hex
        wanted["next_run_at"] = next_run(wanted).isoformat(timespec="seconds") if enabled else ""
        profiles[key] = wanted
        users._atomic_write_json(users.USERS_FILE, raw)
        return deepcopy(wanted)


def account_groups(config, accounts, *, manual=False, profile_key="", revision="", claim=""):
    users = _users()
    with users._web_users_lock():
        people = _people()
        profiles = deepcopy(users._load_raw().get(STORE_KEY) or {})
        owners = _owners(accounts, people)
    # Preserve standalone installations and their existing sender behavior.
    if not profile_key and not profiles and len(people) == 1:
        return [(config, accounts)]
    groups = {}
    for account in accounts:
        key = owners.get(str(account.get("account_ref") or ""), "admin")
        person, profile = people.get(key), profiles.get(key)
        if not person or not person.get("enabled") or person.get("registration_pending"):
            continue
        if profile_key:
            pending = (profile or {}).get("claim") or {}
            if key != profile_key or not profile or not profile.get("enabled") or profile.get("revision") != revision or pending.get("token") != claim:
                continue
        elif not manual and (key != "admin" or profile is not None):
            # Personal accounts are never swept up by the old global cron job.
            continue
        if key != "admin" and not (profile or {}).get("messages"):
            continue
        if key not in groups:
            effective = deepcopy(config)
            if profile and profile.get("messages"):
                effective["messageTemplate"] = profile["messages"][0]
                effective.setdefault("sendStrategy", {})["messageVariants"] = list(profile["messages"])
                effective["literalMessageTemplates"] = True
                effective.setdefault("happyNewYear", {})["enabled"] = False
                effective["scheduleTimezone"] = profile["timezone"]
                hour = int(profile["time"].split(":")[0])
                effective["dailySendWindow"] = {"enabled": True, "startHour": hour, "endHour": 24, "scheduleIntervalMinutes": 1, "exactTime": profile["time"]}
            groups[key] = (effective, [])
        scoped = dict(account)
        scoped["_personal_scope"] = {"owner": key, "revision": (profile or {}).get("revision", ""), "claim": claim if profile_key else ""}
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
        profile = (users._load_raw().get(STORE_KEY) or {}).get(key) or {}
        if profile.get("revision", "") != scope.get("revision", ""):
            return False
        pending = profile.get("claim") or {}
        if scope.get("claim") and (not profile.get("enabled") or pending.get("token") != scope["claim"] or float(pending.get("expires", 0)) <= _now().timestamp()):
            return False
    return True


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
            token = uuid.uuid4().hex
            profile["claim"] = {"token": token, "expires": now.timestamp() + 1800}
            users._atomic_write_json(users.USERS_FILE, raw)
            return {"key": key, "revision": profile["revision"], "token": token}
    return None


def finish_claim(job, *, busy=False, now=None):
    now = (now or _now()).astimezone(UTC)
    users = _users()
    with users._web_users_lock():
        raw = users._load_raw()
        profile = (raw.get(STORE_KEY) or {}).get(job["key"])
        if not profile or profile.get("revision") != job["revision"] or (profile.get("claim") or {}).get("token") != job["token"]:
            return
        profile.pop("claim", None)
        profile["next_run_at"] = (now + timedelta(minutes=1) if busy else next_run(profile, now)).isoformat(timespec="seconds")
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
