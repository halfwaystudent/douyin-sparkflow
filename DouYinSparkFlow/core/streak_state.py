from __future__ import annotations

import json
import os
import re
import unicodedata
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils.config import normalize_unique_id

try:
    from filelock import FileLock
except ImportError:  # pragma: no cover - dependency is pinned in requirements
    FileLock = None


STATE_PENDING = "pending"
STATE_IN_FLIGHT = "in_flight"
STATE_SENT_UNVERIFIED = "sent_unverified"
STATE_SEND_CONFIRMED = "send_confirmed"
STATE_STREAK_VERIFIED = "streak_verified"
STATE_FAILED_RETRYABLE = "failed_retryable"
STATE_FAILED_TERMINAL = "failed_terminal"

CONFIRMED_STATES = {STATE_SEND_CONFIRMED, STATE_STREAK_VERIFIED}
FAILED_STATES = {STATE_FAILED_RETRYABLE, STATE_FAILED_TERMINAL}

TERMINAL_FAILURE_CATEGORIES = {
    "protocol_check_message_not_pass",
    "protocol_check_message_self_visible",
    "protocol_user_blocked",
    "protocol_user_not_in_conversation",
    "protocol_check_conversation_not_pass",
}

LOGIN_FAILURE_CATEGORIES = {
    "login_required",
    "protocol_login_required",
    "account_identity_mismatch",
    "browser_login_required",
}

SENSITIVE_REPORT_KEY_PARTS = (
    "cookie",
    "token",
    "password",
    "secret",
    "subscription",
    "session",
    "authorization",
    "proxy_user",
)


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_time(value, tz=None):
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz or timezone.utc)
    return parsed if tz is None else parsed.astimezone(tz)


def _iso(value):
    return _now(value).isoformat(timespec="seconds")


def normalize_target_name(value):
    raw = unicodedata.normalize("NFKC", str(value or ""))
    for token in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        raw = raw.replace(token, "")
    raw = raw.replace("\xa0", " ")
    return " ".join(raw.split()).strip()


def account_match_tokens(account):
    tokens = set()
    username = str(account.get("username") or "").strip()
    unique_id = str(account.get("unique_id") or "").strip()
    normalized_unique_id = normalize_unique_id(unique_id)
    if username:
        tokens.add(username.casefold())
    if unique_id:
        tokens.add(unique_id.casefold())
    if normalized_unique_id:
        tokens.add(normalized_unique_id.casefold())
    return tokens


def find_matching_account(accounts, user):
    wanted = account_match_tokens(user)
    if not wanted:
        return None
    for account in accounts or []:
        if account_match_tokens(account) & wanted:
            return account
    return None


def _normalize_key(value):
    return normalize_target_name(value).casefold()


def _state_bucket(account):
    states = account.get("target_states")
    if not isinstance(states, dict):
        states = {}
        account["target_states"] = states
    return states


def _ref_bucket(account):
    refs = account.get("target_refs")
    if not isinstance(refs, dict):
        refs = {}
        account["target_refs"] = refs
    return refs


def _ref_from_record(record):
    if not isinstance(record, dict):
        return ""
    sec_uid = str(record.get("secUid") or record.get("sec_uid") or "").strip()
    if sec_uid:
        return f"sec:{sec_uid}"
    peer_user_id = str(
        record.get("peerUserId") or record.get("peer_user_id") or ""
    ).strip()
    if peer_user_id:
        return f"peer:{peer_user_id}"
    for item in record.get("stableKeys") or []:
        raw = str(item or "").strip()
        if raw.startswith(("sec:", "peer:")):
            return raw
        ref = resolve_target_ref({}, raw)
        if ref and not ref.startswith("nickname:"):
            return ref
    return ""


def resolve_target_ref(account, target_name):
    target_name = normalize_target_name(target_name)
    if not target_name:
        return ""

    refs = _ref_bucket(account)
    cached = str(refs.get(target_name) or "").strip()

    normalized = _normalize_key(target_name)
    protocol_matches = [
        dict(item)
        for item in (account.get("protocol_targets_cache") or [])
        if isinstance(item, dict)
        and _normalize_key(item.get("nickname")) == normalized
    ]
    if len(protocol_matches) == 1:
        ref = _ref_from_record(protocol_matches[0])
        if ref:
            _upgrade_state_key(account, cached, ref)
            refs[target_name] = ref
            return ref

    friend_entry = dict(
        (account.get("friend_index") or {}).get(normalized) or {}
    )
    ref = _ref_from_record(friend_entry)
    if ref:
        _upgrade_state_key(account, cached, ref)
        refs[target_name] = ref
        return ref
    if cached:
        return cached
    ref = f"nickname:{normalized}"
    refs[target_name] = ref
    return ref


def target_display_name(account, target_ref):
    for name, ref in (_ref_bucket(account) or {}).items():
        if ref == target_ref:
            return name
    state = _state_bucket(account).get(target_ref) or {}
    return str(state.get("displayName") or target_ref).strip()


def _legacy_history_entry(account, target_name, target_ref):
    history = account.get("message_history") or {}
    aliases = _legacy_aliases(account, target_name, target_ref)
    entries = [
        dict(history.get(alias) or {})
        for alias in aliases
        if alias in history
    ]
    return _latest_entry(entries, "sentAt")


def _legacy_failure_entry(account, target_name, target_ref):
    failures = account.get("failure_queue") or {}
    aliases = _legacy_aliases(account, target_name, target_ref)
    entries = [
        dict(failures.get(alias) or {})
        for alias in aliases
        if alias in failures
    ]
    return _latest_entry(entries, "lastAttemptAt")


def _legacy_aliases(account, target_name, target_ref):
    aliases = [normalize_target_name(target_name), str(target_ref or "")]
    for name, ref in (_ref_bucket(account) or {}).items():
        if str(ref or "") == str(target_ref or ""):
            aliases.append(name)
    state = _state_bucket(account).get(str(target_ref or "")) or {}
    if state.get("displayName"):
        aliases.append(str(state.get("displayName")))
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _latest_entry(entries, *time_keys):
    best = {}
    best_time = None
    for entry in entries or []:
        parsed = None
        for key in time_keys:
            parsed = _parse_time(entry.get(key))
            if parsed is not None:
                break
        if not best or (
            parsed is not None
            and (best_time is None or parsed > best_time)
        ):
            best = dict(entry)
            best_time = parsed
    return best


def _state_event_time(state):
    for key in ("confirmedAt", "verifiedAt", "sentAt"):
        parsed = _parse_time(dict(state or {}).get(key))
        if parsed is not None:
            return parsed
    return None


def _upgrade_state_key(account, old_ref, new_ref):
    old_ref = str(old_ref or "")
    new_ref = str(new_ref or "")
    if not old_ref or not new_ref or old_ref == new_ref:
        return
    states = _state_bucket(account)
    old_state = states.get(old_ref)
    if not isinstance(old_state, dict):
        return
    new_state = states.get(new_ref)
    old_time = _state_event_time(old_state)
    new_time = _state_event_time(new_state) if isinstance(new_state, dict) else None
    if not isinstance(new_state, dict) or (old_time and (not new_time or old_time > new_time)):
        states[new_ref] = old_state
    states.pop(old_ref, None)
    refs = _ref_bucket(account)
    for name, ref in list(refs.items()):
        if str(ref or "") == old_ref:
            refs[name] = new_ref


def _state_from_legacy(account, target_name, target_ref, now):
    history = _legacy_history_entry(account, target_name, target_ref)
    failure = _legacy_failure_entry(account, target_name, target_ref)
    state = {
        "targetRef": target_ref,
        "displayName": target_name,
        "status": STATE_PENDING,
        "attemptCount": 0,
        "fallbackAttempted": False,
        "updatedAt": _iso(now),
    }
    if history:
        sent_at = _parse_time(history.get("sentAt"), now.tzinfo)
        if sent_at and sent_at.date() == now.date():
            if history.get("status") == STATE_STREAK_VERIFIED:
                state["status"] = STATE_STREAK_VERIFIED
            elif (
                history.get("status") == "confirmed"
                and history.get("confirmationLevel") == "strong"
            ) or _receipt_is_strong(history.get("serverReceipt")):
                state["status"] = STATE_SEND_CONFIRMED
            else:
                state["status"] = STATE_SENT_UNVERIFIED
            state["sentAt"] = str(history.get("sentAt") or "")
            state["confirmationSource"] = str(
                history.get("confirmationSource") or ""
            )
            state["lastEvidenceDetail"] = str(
                history.get("confirmationDetail") or ""
            )
    if failure:
        last_attempt = _parse_time(failure.get("lastAttemptAt"), now.tzinfo)
        if last_attempt and last_attempt.date() == now.date():
            category = str(failure.get("category") or "")
            state["status"] = (
                STATE_FAILED_TERMINAL
                if category in TERMINAL_FAILURE_CATEGORIES
                else STATE_FAILED_RETRYABLE
            )
            state["lastErrorCategory"] = category
            state["lastErrorReason"] = str(failure.get("reason") or "")
            state["attemptCount"] = _attempt_count(failure)
    return state


def _receipt_is_strong(receipt):
    if not isinstance(receipt, dict):
        return False
    try:
        http_status = int(receipt.get("httpStatus") or 0)
    except (TypeError, ValueError):
        http_status = 0
    call = str(receipt.get("call") or "").strip()
    return (
        bool(receipt.get("ok"))
        and 200 <= http_status < 300
        and call == "message_send"
    )


def receipt_is_strong(receipt):
    return _receipt_is_strong(receipt)


def _attempt_count(entry):
    try:
        return max(0, int(dict(entry or {}).get("attemptCount") or 0))
    except (TypeError, ValueError):
        return 0


def reconcile_account(account, now=None):
    now = _now(now)
    states = _state_bucket(account)
    changed = False
    for target_name in account.get("targets") or []:
        target_name = normalize_target_name(target_name)
        if not target_name:
            continue
        target_ref = resolve_target_ref(account, target_name)
        state = states.get(target_ref)
        if not isinstance(state, dict):
            state = _state_from_legacy(account, target_name, target_ref, now)
            states[target_ref] = state
            changed = True
        if state.get("status") == STATE_IN_FLIGHT:
            lease_expires_at = _parse_time(state.get("leaseExpiresAt"), now.tzinfo)
            if lease_expires_at is None or lease_expires_at <= now:
                state.update(
                    {
                        "status": STATE_FAILED_RETRYABLE,
                        "lastErrorCategory": "in_flight_lease_expired",
                        "lastErrorReason": "previous attempt did not finish",
                        "updatedAt": _iso(now),
                    }
                )
                state.pop("runId", None)
                state.pop("leaseExpiresAt", None)
                changed = True
        if state.get("displayName") != target_name:
            state["displayName"] = target_name
            changed = True
    return changed


def target_state(account, target_name, now=None):
    now = _now(now)
    reconcile_account(account, now)
    target_ref = resolve_target_ref(account, target_name)
    state = _state_bucket(account).get(target_ref) or {}
    return deepcopy(state)


def _state_for_write(account, target_name, now):
    now = _now(now)
    reconcile_account(account, now)
    target_ref = resolve_target_ref(account, target_name)
    states = _state_bucket(account)
    state = states.get(target_ref)
    if not isinstance(state, dict):
        state = _state_from_legacy(account, target_name, target_ref, now)
        states[target_ref] = state
    state["targetRef"] = target_ref
    state["displayName"] = normalize_target_name(target_name)
    return state


def mark_in_flight(
    account,
    target_name,
    *,
    run_id,
    strategy,
    now=None,
    lease_seconds=900,
):
    now = _now(now)
    state = _state_for_write(account, target_name, now)
    event_time = _state_event_time(state)
    if (
        state.get("status") in CONFIRMED_STATES
        and event_time
        and event_time.astimezone(now.tzinfo).date() == now.date()
    ):
        return deepcopy(state)
    state.update(
        {
            "status": STATE_IN_FLIGHT,
            "strategy": str(strategy or ""),
            "runId": str(run_id or ""),
            "startedAt": _iso(now),
            "leaseExpiresAt": _iso(
                now + timedelta(seconds=max(1, int(lease_seconds)))
            ),
            "attemptCount": _attempt_count(state) + 1,
            "updatedAt": _iso(now),
        }
    )
    return deepcopy(state)


def mark_sent_unverified(
    account,
    target_name,
    *,
    run_id="",
    strategy="browser",
    now=None,
    detail="",
):
    now = _now(now)
    state = _state_for_write(account, target_name, now)
    event_time = _state_event_time(state)
    if (
        state.get("status") in CONFIRMED_STATES
        and event_time
        and event_time.astimezone(now.tzinfo).date() == now.date()
    ):
        return deepcopy(state)
    state.update(
        {
            "status": STATE_SENT_UNVERIFIED,
            "strategy": str(strategy or "browser"),
            "runId": str(run_id or state.get("runId") or ""),
            "sentAt": _iso(now),
            "confirmationSource": "browser_visible_count_increased",
            "lastEvidenceDetail": str(detail or ""),
            "needsVerification": True,
            "updatedAt": _iso(now),
        }
    )
    state.pop("leaseExpiresAt", None)
    return deepcopy(state)


def mark_send_confirmed(
    account,
    target_name,
    *,
    run_id="",
    strategy="protocol",
    now=None,
    source="protocol_send_receipt",
    detail="",
):
    now = _now(now)
    state = _state_for_write(account, target_name, now)
    state.update(
        {
            "status": STATE_SEND_CONFIRMED,
            "strategy": str(strategy or "protocol"),
            "runId": str(run_id or state.get("runId") or ""),
            "sentAt": _iso(now),
            "confirmedAt": _iso(now),
            "confirmationSource": str(source or "protocol_send_receipt"),
            "lastEvidenceDetail": str(detail or ""),
            "needsVerification": False,
            "updatedAt": _iso(now),
        }
    )
    state.pop("leaseExpiresAt", None)
    return deepcopy(state)


def mark_streak_verified(account, target_name, *, now=None, detail=""):
    now = _now(now)
    state = _state_for_write(account, target_name, now)
    state.update(
        {
            "status": STATE_STREAK_VERIFIED,
            "verifiedAt": _iso(now),
            "lastEvidenceDetail": str(detail or ""),
            "needsVerification": False,
            "updatedAt": _iso(now),
        }
    )
    return deepcopy(state)


def mark_failed(
    account,
    target_name,
    *,
    category,
    reason,
    retryable,
    now=None,
    run_id="",
    strategy="",
):
    now = _now(now)
    state = _state_for_write(account, target_name, now)
    event_time = _state_event_time(state)
    if (
        state.get("status") in CONFIRMED_STATES
        and event_time
        and event_time.astimezone(now.tzinfo).date() == now.date()
    ):
        return deepcopy(state)
    state.update(
        {
            "status": (
                STATE_FAILED_RETRYABLE if retryable else STATE_FAILED_TERMINAL
            ),
            "runId": str(run_id or state.get("runId") or ""),
            "strategy": str(strategy or state.get("strategy") or ""),
            "lastErrorCategory": str(category or ""),
            "lastErrorReason": str(reason or ""),
            "updatedAt": _iso(now),
        }
    )
    state.pop("leaseExpiresAt", None)
    return deepcopy(state)


def mark_fallback_attempted(account, target_name, *, now=None, strategy="browser"):
    now = _now(now)
    state = _state_for_write(account, target_name, now)
    state.update(
        {
            "fallbackAttempted": True,
            "fallbackStrategy": str(strategy or "browser"),
            "fallbackAt": _iso(now),
            "updatedAt": _iso(now),
        }
    )
    return deepcopy(state)


def fallback_eligible(account, target_name, now=None):
    now = _now(now)
    state = target_state(account, target_name, now)
    fallback_at = _parse_time(state.get("fallbackAt"), now.tzinfo)
    return bool(
        state.get("status") == STATE_FAILED_RETRYABLE
        and not (fallback_at and fallback_at.date() == now.date())
    )


def is_send_confirmed(account, target_name, now=None):
    now = _now(now)
    state = target_state(account, target_name, now)
    if state.get("status") not in CONFIRMED_STATES:
        return False
    event_time = _state_event_time(state)
    return bool(event_time and event_time.astimezone(now.tzinfo).date() == now.date())


def is_streak_verified(account, target_name, now=None):
    now = _now(now)
    state = target_state(account, target_name, now)
    if state.get("status") != STATE_STREAK_VERIFIED:
        return False
    event_time = _parse_time(state.get("verifiedAt") or state.get("sentAt"))
    return bool(event_time and event_time.astimezone(now.tzinfo).date() == now.date())


def is_sent_unverified(account, target_name, now=None):
    now = _now(now)
    state = target_state(account, target_name, now)
    if state.get("status") != STATE_SENT_UNVERIFIED:
        return False
    event_time = _parse_time(state.get("sentAt"))
    return bool(event_time and event_time.astimezone(now.tzinfo).date() == now.date())


def preflight_account(account, now=None, *, require_friend_index=False):
    now = _now(now)
    failure = dict(account.get("account_failure") or {})
    last_attempt = _parse_time(failure.get("lastAttemptAt"), now.tzinfo)
    category = str(failure.get("category") or "").strip()
    if last_attempt and last_attempt.date() == now.date() and category in LOGIN_FAILURE_CATEGORIES:
        return {
            "healthy": False,
            "category": category,
            "reason": str(failure.get("reason") or category),
        }
    if account.get("account_identity_mismatch") or account.get("identity_mismatch"):
        return {
            "healthy": False,
            "category": "account_identity_mismatch",
            "reason": "login identity does not match the configured account",
        }
    if account.get("login_required") or account.get("needs_relogin"):
        return {
            "healthy": False,
            "category": "login_required",
            "reason": "account requires login",
        }
    network_route = str(
        account.get("lastNetworkRoute")
        or account.get("networkRoute")
        or ""
    ).strip().lower()
    if network_route in {"unavailable", "failed"}:
        return {
            "healthy": False,
            "category": "network_unavailable",
            "reason": f"network route is {network_route}",
        }
    if not (account.get("cookies") or []):
        return {
            "healthy": False,
            "category": "missing_cookies",
            "reason": "account has no cookies",
        }
    if require_friend_index:
        meta = dict(account.get("friend_index_meta") or {})
        last_scan_at = _parse_time(meta.get("lastScanAt"), now.tzinfo)
        if (
            not account.get("friend_index")
            or not meta.get("lastScanComplete")
            or not last_scan_at
            or last_scan_at.date() != now.date()
        ):
            return {
                "healthy": False,
                "category": "friend_index_stale",
                "reason": "friend index is missing or stale",
            }
    return {"healthy": True, "category": "", "reason": ""}


def schedule_phase(now, send_window):
    window = dict(send_window or {})
    if not window.get("enabled"):
        return "regular"
    now = _now(now)
    start_hour = int(window.get("startHour") or 0)
    end_hour = int(window.get("endHour") or 23)
    interval = max(1, int(window.get("scheduleIntervalMinutes") or 20))
    start = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if end_hour == 24:
        if now.hour < start_hour:
            end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            end = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
    else:
        end = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    grace = timedelta(minutes=interval)
    if start <= now < end - grace:
        return "regular"
    if end - grace <= now <= end:
        return "close-out"
    if end < now <= end + grace:
        return "fallback"
    return "outside"


def _redact_report(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.casefold()
            if any(part in normalized for part in SENSITIVE_REPORT_KEY_PARTS):
                continue
            redacted[key_text] = _redact_report(item)
        return redacted
    if isinstance(value, list):
        return [_redact_report(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value):
    text = str(value or "")
    text = re.sub(
        r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@",
        r"\1[redacted]@",
        text,
    )
    text = re.sub(
        r"(?i)\b(token|sessionid|session|password|secret)=([^&\s]+)",
        r"\1=[redacted]",
        text,
    )
    return text


def append_run_report(record, *, path=None, now=None):
    now = _now(now)
    target = (
        Path(path)
        if path is not None
        else Path("logs") / "run_reports" / f"{now.date().isoformat()}.jsonl"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _redact_report(dict(record or {}))
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    lock = f"{target}.lock"
    if FileLock is not None:
        with FileLock(lock, timeout=30):
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line)
    else:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def update_weekly_summary(records, *, directory=None, now=None):
    now = _now(now)
    root = (
        Path(directory)
        if directory is not None
        else Path("logs") / "run_reports"
    )
    iso = now.isocalendar()
    target = root / "weekly" / f"{iso.year}-W{iso.week:02d}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = f"{target}.lock"

    def mutate():
        try:
            summary = json.loads(target.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            summary = {
                "week": f"{iso.year}-W{iso.week:02d}",
                "runs": 0,
                "statusCounts": {},
                "categoryCounts": {},
                "confirmationSourceCounts": {},
                "totalDurationMs": 0,
            }
        for record in records or []:
            record = dict(record or {})
            summary["runs"] = int(summary.get("runs") or 0) + 1
            status = str(record.get("status") or "unknown")
            category = str(record.get("category") or "none")
            source = str(record.get("confirmationSource") or "none")
            for key, value in (
                ("statusCounts", status),
                ("categoryCounts", category),
                ("confirmationSourceCounts", source),
            ):
                bucket = summary.setdefault(key, {})
                bucket[value] = int(bucket.get(value) or 0) + 1
            try:
                duration = max(0, int(record.get("durationMs") or 0))
            except (TypeError, ValueError):
                duration = 0
            summary["totalDurationMs"] = int(summary.get("totalDurationMs") or 0) + duration
        target.write_text(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass

    if FileLock is not None:
        with FileLock(lock, timeout=30):
            mutate()
    else:
        mutate()
    return target


def prune_run_reports(directory, *, detail_days=30, now=None):
    now = _now(now)
    root = Path(directory)
    if not root.exists():
        return []
    cutoff = now.date() - timedelta(days=max(0, int(detail_days)))
    removed = []
    for path in root.glob("*.jsonl"):
        try:
            report_date = datetime.fromisoformat(path.stem).date()
        except ValueError:
            continue
        if report_date < cutoff:
            path.unlink()
            removed.append(str(path))
    return removed
