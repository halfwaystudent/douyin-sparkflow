from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from urllib.parse import parse_qs, urlparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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

AUTH_COOKIE_NAMES = {
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "uid_tt",
    "uid_tt_ss",
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


def _refs_from_record(record):
    if not isinstance(record, dict):
        return []
    refs = []

    def add(ref):
        ref = str(ref or "").strip()
        if ref and ref not in refs:
            refs.append(ref)

    sec_uid = str(record.get("secUid") or record.get("sec_uid") or "").strip()
    if sec_uid:
        add(f"sec:{sec_uid}")
    peer_user_id = str(
        record.get("peerUserId") or record.get("peer_user_id") or ""
    ).strip()
    if peer_user_id:
        add(f"peer:{peer_user_id}")
    for item in record.get("stableKeys") or []:
        raw = str(item or "").strip()
        if raw.startswith(("sec:", "peer:")):
            add(raw)
        if raw.startswith("data-sec-uid:"):
            value = raw.split(":", 1)[1].strip()
            if value:
                add(f"sec:{value}")
        if raw.startswith("data-user-id:"):
            value = raw.split(":", 1)[1].strip()
            if value:
                add(f"peer:{value}")
        if raw.startswith("data-id:"):
            value = raw.split(":", 1)[1].strip()
            if value:
                add(f"peer:{value}")
        if raw.startswith("data-conversation-id:"):
            value = raw.split(":", 1)[1].strip()
            if value:
                add(f"conversation:{value}")
        if raw.startswith("href:"):
            href = raw.split(":", 1)[1].strip()
            query = parse_qs(urlparse(href).query)
            for key in ("sec_uid", "secUid", "peerUserId", "user_id"):
                value = str((query.get(key) or [""])[0]).strip()
                if value:
                    prefix = "sec" if key.lower() == "sec_uid" else "peer"
                    add(f"{prefix}:{value}")
        ref = resolve_target_ref({}, raw)
        if ref and not ref.startswith("nickname:"):
            add(ref)
    return sorted(refs, key=_target_ref_rank, reverse=True)


def _ref_from_record(record):
    refs = _refs_from_record(record)
    return refs[0] if refs else ""


def _upgrade_related_refs(account, new_ref, candidate_refs):
    new_rank = _target_ref_rank(new_ref)
    if new_rank <= 0:
        return
    candidates = {
        str(ref or "")
        for ref in candidate_refs or []
        if str(ref or "")
    }
    known_refs = set(candidates)
    refs = account.get("target_refs")
    if isinstance(refs, dict):
        known_refs.update(
            str(ref or "")
            for ref in refs.values()
            if str(ref or "")
        )
    states = account.get("target_states")
    if isinstance(states, dict):
        known_refs.update(
            str(ref or "")
            for ref in states.keys()
            if str(ref or "")
        )
    for bucket_name in ("message_history", "failure_queue"):
        bucket = account.get(bucket_name)
        if isinstance(bucket, dict):
            known_refs.update(
                str(ref or "")
                for ref in bucket.keys()
                if str(ref or "")
            )
    associated_refs = set(candidates)
    nickname_aliases = {}
    history_bucket = account.get("message_history")
    failure_bucket = account.get("failure_queue")

    def associate_alias(alias_name, metadata_ref):
        alias_name = normalize_target_name(alias_name)
        if not alias_name:
            return
        if isinstance(refs, dict):
            mapped_ref = str(refs.get(alias_name) or "")
            if (
                mapped_ref
                and mapped_ref not in candidates
                and not mapped_ref.startswith("nickname:")
            ):
                return
        nickname_ref = f"nickname:{_normalize_key(alias_name)}"
        matching_names = [alias_name]
        bucket_alias_exists = False
        for bucket in (history_bucket, failure_bucket):
            if not isinstance(bucket, dict):
                continue
            for key in bucket:
                if _normalize_key(key) == _normalize_key(alias_name):
                    bucket_alias_exists = True
                    matching_names.append(str(key))
        nickname_aliases.setdefault(nickname_ref, []).extend(matching_names)
        if (
            metadata_ref in known_refs
            or bucket_alias_exists
        ):
            associated_refs.add(nickname_ref)

    if isinstance(refs, dict):
        for name, ref in refs.items():
            if str(ref or "") not in candidates:
                continue
            associate_alias(name, f"nickname:{_normalize_key(name)}")
    for record in account.get("protocol_targets_cache") or []:
        record_refs = set(_refs_from_record(record))
        if not record_refs.intersection(candidates):
            continue
        alias_name = normalize_target_name(
            (record or {}).get("nickname") or ""
        )
        associate_alias(
            alias_name,
            f"nickname:{_normalize_key(alias_name)}",
        )
    for index_name, record in (account.get("friend_index") or {}).items():
        record_refs = set(_refs_from_record(record))
        if not record_refs.intersection(candidates):
            continue
        for alias_name in (
            str(index_name or ""),
            str((record or {}).get("visibleName") or ""),
        ):
            associate_alias(
                alias_name,
                f"nickname:{_normalize_key(alias_name)}",
            )
    old_refs = sorted(
        (
            ref
            for ref in known_refs | associated_refs
            if ref in associated_refs and 0 <= _target_ref_rank(ref) < new_rank
        ),
        key=_target_ref_rank,
    )
    for old_ref in old_refs:
        _upgrade_state_key(account, old_ref, new_ref)
        for alias_name in nickname_aliases.get(old_ref, []):
            _move_ref_record(
                account,
                "message_history",
                alias_name,
                new_ref,
                "sentAt",
            )
            _move_ref_record(
                account,
                "failure_queue",
                alias_name,
                new_ref,
                "lastAttemptAt",
            )
            if isinstance(refs, dict):
                refs[alias_name] = new_ref


def target_ref_for_stored_key(account, key):
    raw = str(key or "").strip()
    if not raw:
        return ""
    if _target_ref_rank(raw) >= 0:
        best_ref = raw
        best_rank = _target_ref_rank(raw)
        for record in account.get("protocol_targets_cache") or []:
            record_refs = _refs_from_record(record)
            if raw not in record_refs:
                continue
            for record_ref in record_refs:
                if _target_ref_rank(record_ref) > best_rank:
                    best_ref = record_ref
                    best_rank = _target_ref_rank(record_ref)
        for record in (account.get("friend_index") or {}).values():
            record_refs = _refs_from_record(record)
            if raw not in record_refs:
                continue
            for record_ref in record_refs:
                if _target_ref_rank(record_ref) > best_rank:
                    best_ref = record_ref
                    best_rank = _target_ref_rank(record_ref)
        return best_ref
    return resolve_target_ref(account, raw)


def target_keys_match(account, left, right):
    left_name = normalize_target_name(left)
    right_name = normalize_target_name(right)
    if not left_name or not right_name:
        return False
    if left_name == right_name:
        return True
    left_ref = target_ref_for_stored_key(account, left_name)
    right_ref = target_ref_for_stored_key(account, right_name)
    return bool(left_ref and right_ref and left_ref == right_ref)


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
        candidate_refs = _refs_from_record(protocol_matches[0])
        ref = candidate_refs[0] if candidate_refs else ""
        if ref:
            _upgrade_related_refs(account, ref, candidate_refs)
            if cached in candidate_refs or cached.startswith("nickname:"):
                _upgrade_state_key(account, cached, ref, target_name)
            elif cached:
                _detach_legacy_name(account, target_name, cached)
            refs[target_name] = ref
            return ref

    friend_entry = {}
    for index_name, record in (account.get("friend_index") or {}).items():
        if not isinstance(record, dict):
            continue
        record_names = (
            str(index_name or ""),
            str(record.get("visibleName") or ""),
            str(record.get("nickname") or ""),
        )
        if any(_normalize_key(name) == normalized for name in record_names):
            friend_entry = dict(record)
            break
    candidate_refs = _refs_from_record(friend_entry)
    ref = candidate_refs[0] if candidate_refs else ""
    if ref:
        _upgrade_related_refs(account, ref, candidate_refs)
        if cached in candidate_refs or cached.startswith("nickname:"):
            _upgrade_state_key(account, cached, ref, target_name)
        elif cached:
            _detach_legacy_name(account, target_name, cached)
        refs[target_name] = ref
        return ref

    stable_cache_entries = [
        item
        for item in (account.get("protocol_targets_cache") or [])
        if isinstance(item, dict) and _refs_from_record(item)
    ]
    if len(stable_cache_entries) == 1:
        candidate = stable_cache_entries[0]
        candidate_refs = _refs_from_record(candidate)
        ref = candidate_refs[0] if candidate_refs else ""
        stale_ledger_names = {
            _normalize_key(name)
            for name in set(account.get("message_history") or {})
            | set(account.get("failure_queue") or {})
        }
        if ref and _normalize_key(candidate.get("nickname")) in stale_ledger_names:
            _upgrade_related_refs(account, ref, candidate_refs)
            candidate["nickname"] = target_name
            if cached and cached != ref:
                _upgrade_state_key(account, cached, ref, target_name)
            refs[target_name] = ref
            return ref

    if cached:
        _upgrade_state_key(account, cached, cached, target_name)
        return cached
    ref = f"nickname:{normalized}"
    refs[target_name] = ref
    return ref


def history_entry(account, target_name, tz=None):
    target_ref = resolve_target_ref(account, target_name)
    return _legacy_history_entry(account, target_name, target_ref, tz)


def failure_entry(account, target_name, tz=None):
    target_ref = resolve_target_ref(account, target_name)
    return _legacy_failure_entry(account, target_name, target_ref, tz)


def target_display_name(account, target_ref):
    for name, ref in (_ref_bucket(account) or {}).items():
        if ref == target_ref:
            return name
    state = _state_bucket(account).get(target_ref) or {}
    return str(state.get("displayName") or target_ref).strip()


def _legacy_history_entry(account, target_name, target_ref, tz=None):
    history = account.get("message_history") or {}
    aliases = _legacy_aliases(account, target_name, target_ref)
    entries = [
        dict(history.get(alias) or {})
        for alias in aliases
        if alias in history
    ]
    return _preferred_history_entry(entries, tz)


def _preferred_history_entry(entries, tz=None):
    latest = _latest_entry(entries, "sentAt", tz=tz)
    if not latest:
        return {}
    latest_time = _parse_time(latest.get("sentAt"), tz)
    same_day_entries = [
        entry
        for entry in entries
        if (
            latest_time is None
            or _same_calendar_day(
                _parse_time(entry.get("sentAt"), tz),
                latest_time,
            )
        )
    ]
    manual_resets = [
        entry
        for entry in same_day_entries
        if entry.get("resetAt")
        and entry.get("needsVerification")
        and str(entry.get("status") or "") in {"unconfirmed", STATE_SENT_UNVERIFIED}
    ]
    if manual_resets:
        manual_reset = max(
            manual_resets,
            key=lambda entry: _parse_time(
                entry.get("resetAt") or entry.get("sentAt"),
                tz,
            )
            or datetime.min.replace(tzinfo=timezone.utc),
        )
        evidence = max(
            same_day_entries or entries,
            key=lambda entry: (
                _history_evidence_rank(entry),
                _parse_time(entry.get("sentAt"), tz)
                or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
        manual_time = _parse_time(
            manual_reset.get("resetAt") or manual_reset.get("sentAt"),
            tz,
        )
        evidence_time = _parse_time(evidence.get("sentAt"), tz)
        if manual_time and (
            evidence_time is None or manual_time >= evidence_time
        ):
            return manual_reset
    return max(
        same_day_entries or entries,
        key=lambda entry: (
            _history_evidence_rank(entry),
            _parse_time(entry.get("sentAt"), tz) or datetime.min.replace(
                tzinfo=timezone.utc
            ),
        ),
    )


def _history_evidence_rank(entry):
    entry = dict(entry or {})
    status = str(entry.get("status") or "")
    level = str(entry.get("confirmationLevel") or "")
    if status == STATE_STREAK_VERIFIED:
        return 3
    if (status == "confirmed" and level == "strong") or _receipt_is_strong(
        entry.get("serverReceipt")
    ):
        return 2
    if status in {STATE_SENT_UNVERIFIED, "unconfirmed"} or level:
        return 1
    return 0


def _legacy_failure_entry(account, target_name, target_ref, tz=None):
    failures = account.get("failure_queue") or {}
    aliases = _legacy_aliases(account, target_name, target_ref)
    entries = [
        dict(failures.get(alias) or {})
        for alias in aliases
        if alias in failures
    ]
    return _latest_entry(entries, "lastAttemptAt", tz=tz)


def _legacy_aliases(account, target_name, target_ref):
    target_ref = str(target_ref or "")
    aliases = [normalize_target_name(target_name), target_ref]
    for name, ref in (_ref_bucket(account) or {}).items():
        if str(ref or "") == target_ref:
            aliases.append(name)
    for name, record in (account.get("friend_index") or {}).items():
        if _ref_from_record(record) != target_ref:
            continue
        aliases.append(str(name or ""))
        if isinstance(record, dict):
            aliases.append(str(record.get("visibleName") or ""))
    for record in account.get("protocol_targets_cache") or []:
        if _ref_from_record(record) != target_ref:
            continue
        aliases.append(str((record or {}).get("nickname") or ""))
    state = _state_bucket(account).get(target_ref) or {}
    if state.get("displayName"):
        aliases.append(str(state.get("displayName")))
    normalized_aliases = {_normalize_key(alias) for alias in aliases}
    for bucket_name in ("message_history", "failure_queue"):
        bucket = account.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        aliases.extend(
            str(key)
            for key in bucket
            if _normalize_key(key) in normalized_aliases
        )
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _latest_entry(entries, *time_keys, tz=None):
    best = {}
    best_time = None
    for entry in entries or []:
        parsed = None
        for key in time_keys:
            parsed = _parse_time(entry.get(key), tz)
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
    timestamps = []
    for key in ("confirmedAt", "verifiedAt", "sentAt"):
        parsed = _parse_time(dict(state or {}).get(key))
        if parsed is not None:
            timestamps.append(parsed)
    return max(timestamps) if timestamps else None


def _state_schedule_timezone():
    timezone_name = (
        str(os.getenv("SPARKFLOW_TIMEZONE") or "").strip()
        or str(os.getenv("TZ") or "").strip()
        or "Asia/Shanghai"
    )
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


def _same_calendar_day(left, right):
    if not left or not right:
        return False
    schedule_tz = _state_schedule_timezone()
    return (
        left.astimezone(schedule_tz).date()
        == right.astimezone(schedule_tz).date()
    )


def _is_schedule_today(value, now):
    return bool(value and _same_calendar_day(value, now))


def _target_ref_rank(ref):
    value = str(ref or "")
    if value.startswith("sec:"):
        return 3
    if value.startswith("peer:"):
        return 2
    if value.startswith("conversation:"):
        return 1
    if value.startswith("nickname:"):
        return 0
    return -1


def _is_stable_target_ref(ref):
    return _target_ref_rank(ref) > 0


def _detach_legacy_name(account, target_name, old_ref):
    target_name = normalize_target_name(target_name)
    if not target_name or not old_ref:
        return
    for bucket_name in ("message_history", "failure_queue"):
        bucket = account.get(bucket_name)
        if not isinstance(bucket, dict) or target_name not in bucket:
            continue
        entry = bucket.pop(target_name)
        bucket.setdefault(old_ref, entry)


def _move_ref_record(account, bucket_name, old_ref, new_ref, time_key):
    bucket = account.get(bucket_name)
    if not isinstance(bucket, dict) or old_ref == new_ref:
        return
    old_record = bucket.get(old_ref)
    if not isinstance(old_record, dict):
        return
    new_record = bucket.get(new_ref)
    if isinstance(new_record, dict):
        if bucket_name == "message_history":
            latest = _preferred_history_entry(
                [old_record, new_record],
            )
        else:
            latest = _latest_entry(
                [old_record, new_record],
                time_key,
            )
        bucket[new_ref] = latest or dict(new_record)
    else:
        bucket[new_ref] = old_record
    bucket.pop(old_ref, None)


def _prefer_state(left, right):
    rank = {
        STATE_PENDING: 0,
        STATE_FAILED_RETRYABLE: 1,
        STATE_FAILED_TERMINAL: 1,
        STATE_IN_FLIGHT: 2,
        STATE_SENT_UNVERIFIED: 2,
        STATE_SEND_CONFIRMED: 3,
        STATE_STREAK_VERIFIED: 4,
    }
    left_status = str(left.get("status") or STATE_PENDING)
    right_status = str(right.get("status") or STATE_PENDING)
    left_time = _state_observation_time(left)
    right_time = _state_observation_time(right)
    left_confirmed = left_status in CONFIRMED_STATES
    right_confirmed = right_status in CONFIRMED_STATES
    left_manual_reset = (
        left_status == STATE_FAILED_RETRYABLE
        and str(left.get("lastErrorCategory") or "") == "send_unconfirmed"
    )
    right_manual_reset = (
        right_status == STATE_FAILED_RETRYABLE
        and str(right.get("lastErrorCategory") or "") == "send_unconfirmed"
    )
    same_day = _same_calendar_day(left_time, right_time)

    if left_manual_reset != right_manual_reset:
        manual_time = left_time if left_manual_reset else right_time
        other_time = right_time if left_manual_reset else left_time
        if manual_time and other_time:
            if manual_time != other_time:
                return (
                    left_manual_reset
                    if manual_time > other_time
                    else not left_manual_reset
                )
            return left_manual_reset
        elif manual_time:
            return left_manual_reset
        elif other_time:
            return not left_manual_reset
        else:
            return left_manual_reset

    if left_status == STATE_SENT_UNVERIFIED and right_status in FAILED_STATES:
        return bool(
            left_time and (right_time is None or left_time > right_time)
        )
    if right_status == STATE_SENT_UNVERIFIED and left_status in FAILED_STATES:
        return False

    if left_status in FAILED_STATES and right_status == STATE_IN_FLIGHT:
        return bool(
            left_time and (right_time is None or left_time >= right_time)
        )
    if right_status in FAILED_STATES and left_status == STATE_IN_FLIGHT:
        return False

    if left_confirmed != right_confirmed:
        if left_confirmed:
            if left_time and right_time and not _same_calendar_day(
                left_time, right_time
            ):
                return left_time > right_time
            return True
        if left_time and right_time and not _same_calendar_day(
            left_time, right_time
        ):
            return left_time > right_time
        return False

    if same_day and rank.get(left_status, 0) != rank.get(right_status, 0):
        return rank.get(left_status, 0) > rank.get(right_status, 0)
    if left_time and right_time and left_time != right_time:
        return left_time > right_time
    return rank.get(left_status, 0) >= rank.get(right_status, 0)


def _upgrade_state_key(account, old_ref, new_ref, target_name=""):
    old_ref = str(old_ref or "")
    new_ref = str(new_ref or "")
    if not old_ref or not new_ref:
        return
    states = _state_bucket(account)
    if old_ref == new_ref:
        state = states.get(new_ref)
        if isinstance(state, dict) and state.get("targetRef") != new_ref:
            state["targetRef"] = new_ref
        return
    if _target_ref_rank(new_ref) <= _target_ref_rank(old_ref):
        _detach_legacy_name(account, target_name, old_ref)
        return
    old_state = states.get(old_ref)
    if isinstance(old_state, dict):
        new_state = states.get(new_ref)
        if not isinstance(new_state, dict):
            old_state["targetRef"] = new_ref
            states[new_ref] = old_state
        else:
            base, other = (
                (new_state, old_state)
                if _prefer_state(new_state, old_state)
                else (old_state, new_state)
            )
            merged = deepcopy(base)
            merged["targetRef"] = new_ref
            merged["attemptCount"] = max(
                _attempt_count(base),
                _attempt_count(other),
            )
            merged["fallbackAttempted"] = bool(
                base.get("fallbackAttempted")
                or other.get("fallbackAttempted")
            )
            fallback_candidates = []
            for state in (base, other):
                fallback_at = _parse_time(state.get("fallbackAt"))
                if fallback_at:
                    fallback_candidates.append((fallback_at, state))
            if fallback_candidates:
                _, latest_fallback = max(
                    fallback_candidates,
                    key=lambda item: item[0],
                )
                merged["fallbackAt"] = latest_fallback.get("fallbackAt")
                merged["fallbackStrategy"] = (
                    latest_fallback.get("fallbackStrategy")
                    or base.get("fallbackStrategy")
                    or other.get("fallbackStrategy")
                    or "browser"
                )
            states[new_ref] = merged
        states.pop(old_ref, None)
    _move_ref_record(account, "message_history", old_ref, new_ref, "sentAt")
    _move_ref_record(account, "failure_queue", old_ref, new_ref, "lastAttemptAt")
    refs = _ref_bucket(account)
    for name, ref in list(refs.items()):
        if str(ref or "") == old_ref:
            refs[name] = new_ref


def _state_from_legacy(account, target_name, target_ref, now):
    history = _legacy_history_entry(account, target_name, target_ref, now.tzinfo)
    failure = _legacy_failure_entry(account, target_name, target_ref, now.tzinfo)
    history_time = _parse_time(history.get("sentAt"), now.tzinfo)
    failure_time = _parse_time(failure.get("lastAttemptAt"), now.tzinfo)
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
        if _is_schedule_today(sent_at, now):
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
    failure_time_is_today = bool(
        failure and _is_schedule_today(failure_time, now)
    )
    if failure_time_is_today:
        state["attemptCount"] = max(
            _attempt_count(state),
            _attempt_count(failure),
        )
    if failure:
        failure_is_newer = bool(
            failure_time
            and (history_time is None or failure_time >= history_time)
        )
        if (
            failure_time
            and _is_schedule_today(failure_time, now)
            and failure_is_newer
            and state.get("status") not in CONFIRMED_STATES
        ):
            category = str(failure.get("category") or "")
            state["status"] = (
                STATE_FAILED_TERMINAL
                if category in TERMINAL_FAILURE_CATEGORIES
                else STATE_FAILED_RETRYABLE
            )
            state["lastErrorCategory"] = category
            state["lastErrorReason"] = str(failure.get("reason") or "")
            state["lastAttemptAt"] = str(failure.get("lastAttemptAt") or "")
            state["attemptCount"] = _attempt_count(failure)
    state["_legacyEvidence"] = bool(
        state.get("status") != STATE_PENDING
        or state.get("lastErrorCategory")
    )
    return state


def _state_observation_time(state):
    state = dict(state or {})
    if (
        str(state.get("status") or "") == STATE_FAILED_RETRYABLE
        and str(state.get("lastErrorCategory") or "") == "send_unconfirmed"
    ):
        reset_time = _parse_time(
            state.get("resetAt")
            or state.get("lastAttemptAt")
            or state.get("updatedAt")
        )
        if reset_time is not None:
            return reset_time
    timestamps = []
    for key in (
        "confirmedAt",
        "verifiedAt",
        "sentAt",
        "lastAttemptAt",
        "startedAt",
    ):
        parsed = _parse_time(state.get(key))
        if parsed is not None:
            timestamps.append(parsed)
    return max(timestamps) if timestamps else None


def _should_merge_legacy_state(existing, legacy):
    existing_status = str(existing.get("status") or STATE_PENDING)
    legacy_status = str(legacy.get("status") or STATE_PENDING)
    rank = {
        STATE_PENDING: 0,
        STATE_FAILED_RETRYABLE: 1,
        STATE_FAILED_TERMINAL: 1,
        STATE_IN_FLIGHT: 2,
        STATE_SENT_UNVERIFIED: 2,
        STATE_SEND_CONFIRMED: 3,
        STATE_STREAK_VERIFIED: 4,
    }
    existing_time = _state_observation_time(existing)
    legacy_time = _state_observation_time(legacy)
    same_day = _same_calendar_day(existing_time, legacy_time)
    existing_manual_reset = (
        existing_status == STATE_FAILED_RETRYABLE
        and str(existing.get("lastErrorCategory") or "") == "send_unconfirmed"
    )
    legacy_manual_reset = (
        legacy_status == STATE_FAILED_RETRYABLE
        and str(legacy.get("lastErrorCategory") or "") == "send_unconfirmed"
    )
    if existing_manual_reset:
        if legacy_time and existing_time and legacy_time > existing_time:
            return True
        return False
    if legacy_manual_reset:
        if existing_time and legacy_time and existing_time > legacy_time:
            return False
        return True
    if (
        existing_status in CONFIRMED_STATES
        and legacy_status not in CONFIRMED_STATES
    ):
        return False
    if (
        legacy_status in CONFIRMED_STATES
        and existing_status not in CONFIRMED_STATES
    ):
        if same_day:
            return True
        if existing_time and legacy_time and existing_time != legacy_time:
            return legacy_time > existing_time
        return True
    if (
        existing_status in {STATE_SENT_UNVERIFIED, STATE_IN_FLIGHT}
        and legacy_status in FAILED_STATES
        and same_day
    ):
        return False
    if (
        existing_status in CONFIRMED_STATES
        and legacy_status in CONFIRMED_STATES
        and same_day
        and rank.get(legacy_status, 0) != rank.get(existing_status, 0)
    ):
        return rank.get(legacy_status, 0) > rank.get(existing_status, 0)
    if existing_time and legacy_time and existing_time != legacy_time:
        return legacy_time > existing_time
    if existing_time and not legacy_time:
        return False
    if legacy_time and not existing_time:
        return True
    if (
        existing_status in CONFIRMED_STATES
        and legacy_status in CONFIRMED_STATES
        and rank.get(legacy_status, 0) != rank.get(existing_status, 0)
    ):
        return rank.get(legacy_status, 0) > rank.get(existing_status, 0)
    if existing_status == STATE_PENDING:
        return rank.get(legacy_status, 0) > rank.get(existing_status, 0)
    return False


def _merge_legacy_state(existing, legacy, now):
    if not _should_merge_legacy_state(existing, legacy):
        return False
    for key in (
        "status",
        "sentAt",
        "confirmedAt",
        "verifiedAt",
        "confirmationSource",
        "lastEvidenceDetail",
        "needsVerification",
        "lastErrorCategory",
        "lastErrorReason",
        "lastAttemptAt",
        "displayName",
        "resetAt",
        "resetReason",
        "previousStatus",
    ):
        if key in legacy:
            existing[key] = legacy[key]
    existing["attemptCount"] = max(
        _attempt_count(existing),
        _attempt_count(legacy),
    )
    if existing.get("status") != STATE_STREAK_VERIFIED:
        existing.pop("verifiedAt", None)
    if existing.get("status") not in CONFIRMED_STATES:
        existing.pop("confirmedAt", None)
    if existing.get("status") in CONFIRMED_STATES:
        existing.pop("lastErrorCategory", None)
        existing.pop("lastErrorReason", None)
        existing.pop("lastAttemptAt", None)
    existing["updatedAt"] = _iso(now)
    return True


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
        and receipt.get("jsonOk") is True
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
        legacy_state = _state_from_legacy(
            account,
            target_name,
            target_ref,
            now,
        )
        legacy_evidence = bool(legacy_state.pop("_legacyEvidence", False))
        if not isinstance(state, dict):
            state = legacy_state
            states[target_ref] = state
            changed = True
        elif legacy_evidence and _merge_legacy_state(state, legacy_state, now):
            changed = True
        if state.get("status") == STATE_IN_FLIGHT:
            lease_expires_at = _parse_time(state.get("leaseExpiresAt"), now.tzinfo)
            if lease_expires_at is None or lease_expires_at <= now:
                fallback_at = _parse_time(
                    state.get("fallbackAt"),
                    now.tzinfo,
                )
                started_at = _parse_time(
                    state.get("startedAt"),
                    now.tzinfo,
                )
                state.update(
                    {
                        "status": STATE_FAILED_RETRYABLE,
                        "lastErrorCategory": "in_flight_lease_expired",
                        "lastErrorReason": "previous attempt did not finish",
                        "lastAttemptAt": _iso(now),
                        "updatedAt": _iso(now),
                    }
                )
                state.pop("runId", None)
                state.pop("leaseExpiresAt", None)
                if fallback_at and (
                    started_at is None or fallback_at >= started_at
                ):
                    state.pop("fallbackAttempted", None)
                    state.pop("fallbackAt", None)
                    state.pop("fallbackStrategy", None)
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


def _store_preferred_state(account, target_name, now, candidate):
    target_ref = resolve_target_ref(account, target_name)
    states = _state_bucket(account)
    existing = states.get(target_ref)
    if not isinstance(existing, dict) or _prefer_state(candidate, existing):
        candidate["targetRef"] = target_ref
        candidate["displayName"] = normalize_target_name(target_name)
        states[target_ref] = candidate
        return deepcopy(candidate)
    return deepcopy(existing)


def claim_in_flight(
    account,
    target_name,
    *,
    run_id,
    strategy,
    now=None,
    lease_seconds=900,
    allow_retry=False,
):
    now = _now(now)
    state = _state_for_write(account, target_name, now)
    event_time = _state_event_time(state)
    lease_expires_at = _parse_time(state.get("leaseExpiresAt"), now.tzinfo)
    fallback_at = _parse_time(state.get("fallbackAt"), now.tzinfo)
    if (
        not allow_retry
        and state.get("fallbackAttempted")
        and _is_schedule_today(
        fallback_at,
        now,
        )
    ):
        return deepcopy(state), False
    if (
        state.get("status") == STATE_IN_FLIGHT
        and lease_expires_at
        and lease_expires_at > now
    ):
        return deepcopy(state), False
    if (
        state.get("status") in CONFIRMED_STATES
        and event_time
        and _is_schedule_today(event_time, now)
    ):
        return deepcopy(state), False
    if (
        not allow_retry
        and
        state.get("status") == STATE_SENT_UNVERIFIED
        and event_time
        and _is_schedule_today(event_time, now)
    ):
        return deepcopy(state), False
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
    return deepcopy(state), True


def mark_in_flight(
    account,
    target_name,
    *,
    run_id,
    strategy,
    now=None,
    lease_seconds=900,
    allow_retry=False,
):
    state, _ = claim_in_flight(
        account,
        target_name,
        run_id=run_id,
        strategy=strategy,
        now=now,
        lease_seconds=lease_seconds,
        allow_retry=allow_retry,
    )
    return state


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
        and _is_schedule_today(event_time, now)
    ):
        return deepcopy(state)
    candidate = deepcopy(state)
    candidate.update(
        {
            "status": STATE_SENT_UNVERIFIED,
            "strategy": str(strategy or "browser"),
            "runId": str(run_id or candidate.get("runId") or ""),
            "sentAt": _iso(now),
            "confirmationSource": "browser_visible_count_increased",
            "lastEvidenceDetail": str(detail or ""),
            "needsVerification": True,
            "updatedAt": _iso(now),
        }
    )
    candidate.pop("leaseExpiresAt", None)
    return _store_preferred_state(account, target_name, now, candidate)


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
    event_time = _state_event_time(state)
    if (
        state.get("status") == STATE_STREAK_VERIFIED
        and event_time
        and _is_schedule_today(event_time, now)
    ):
        return deepcopy(state)
    candidate = deepcopy(state)
    candidate.update(
        {
            "status": STATE_SEND_CONFIRMED,
            "strategy": str(strategy or "protocol"),
            "runId": str(run_id or candidate.get("runId") or ""),
            "sentAt": _iso(now),
            "confirmedAt": _iso(now),
            "confirmationSource": str(source or "protocol_send_receipt"),
            "lastEvidenceDetail": str(detail or ""),
            "needsVerification": False,
            "updatedAt": _iso(now),
        }
    )
    candidate.pop("leaseExpiresAt", None)
    for key in (
        "lastErrorCategory",
        "lastErrorReason",
        "lastAttemptAt",
        "resetAt",
        "resetReason",
        "previousStatus",
    ):
        candidate.pop(key, None)
    return _store_preferred_state(account, target_name, now, candidate)


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
        and _is_schedule_today(event_time, now)
    ):
        return deepcopy(state)
    if (
        state.get("status") == STATE_SENT_UNVERIFIED
        and event_time
        and _is_schedule_today(event_time, now)
    ):
        return deepcopy(state)
    candidate = deepcopy(state)
    candidate.update(
        {
            "status": (
                STATE_FAILED_RETRYABLE if retryable else STATE_FAILED_TERMINAL
            ),
            "runId": str(run_id or candidate.get("runId") or ""),
            "strategy": str(strategy or candidate.get("strategy") or ""),
            "lastErrorCategory": str(category or ""),
            "lastErrorReason": str(reason or ""),
            "lastAttemptAt": _iso(now),
            "updatedAt": _iso(now),
        }
    )
    candidate.pop("leaseExpiresAt", None)
    return _store_preferred_state(account, target_name, now, candidate)


def mark_unconfirmed(
    account,
    target_name,
    *,
    reason,
    source="manual_reset",
    now=None,
    detail="",
):
    now = _now(now)
    state = _state_for_write(account, target_name, now)
    candidate = deepcopy(state)
    candidate.update(
        {
            "status": STATE_FAILED_RETRYABLE,
            "lastErrorCategory": "send_unconfirmed",
            "lastErrorReason": str(reason or ""),
            "lastAttemptAt": _iso(now),
            "resetAt": _iso(now),
            "confirmationSource": str(source or "manual_reset"),
            "lastEvidenceDetail": str(detail or reason or ""),
            "needsVerification": True,
            "updatedAt": _iso(now),
        }
    )
    for key in ("confirmedAt", "verifiedAt", "leaseExpiresAt", "serverReceipt"):
        candidate.pop(key, None)
    return _store_preferred_state(account, target_name, now, candidate)


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
        and not _is_schedule_today(fallback_at, now)
    )


def attempt_in_progress(account, target_name, now=None):
    now = _now(now)
    state = target_state(account, target_name, now)
    if state.get("status") != STATE_IN_FLIGHT:
        return False
    lease_expires_at = _parse_time(state.get("leaseExpiresAt"), now.tzinfo)
    return bool(lease_expires_at and lease_expires_at > now)


def fallback_attempted_today(account, target_name, now=None):
    now = _now(now)
    state = target_state(account, target_name, now)
    fallback_at = _parse_time(state.get("fallbackAt"), now.tzinfo)
    return _is_schedule_today(fallback_at, now)


def is_send_confirmed(account, target_name, now=None):
    now = _now(now)
    state = target_state(account, target_name, now)
    if state.get("status") not in CONFIRMED_STATES:
        return False
    event_time = _state_event_time(state)
    return _is_schedule_today(event_time, now)


def is_streak_verified(account, target_name, now=None):
    now = _now(now)
    state = target_state(account, target_name, now)
    if state.get("status") != STATE_STREAK_VERIFIED:
        return False
    event_time = _parse_time(state.get("verifiedAt") or state.get("sentAt"))
    return _is_schedule_today(event_time, now)


def is_sent_unverified(account, target_name, now=None):
    now = _now(now)
    state = target_state(account, target_name, now)
    if state.get("status") != STATE_SENT_UNVERIFIED:
        return False
    event_time = _parse_time(state.get("sentAt"))
    return _is_schedule_today(event_time, now)


def preflight_account(account, now=None):
    now = _now(now)
    failure = dict(account.get("account_failure") or {})
    last_attempt = _parse_time(failure.get("lastAttemptAt"), now.tzinfo)
    category = str(failure.get("category") or "").strip()
    if (
        _is_schedule_today(last_attempt, now)
        and category in LOGIN_FAILURE_CATEGORIES
    ):
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
    expiries = []
    auth_cookie_seen = False
    auth_cookie_expired = False
    for cookie in account.get("cookies") or []:
        cookie_name = (
            str((cookie or {}).get("name") or "").strip()
            if isinstance(cookie, dict)
            else ""
        )
        if cookie_name in AUTH_COOKIE_NAMES:
            auth_cookie_seen = True
        raw_expiry = (
            (cookie or {}).get("expiry")
            if isinstance(cookie, dict)
            else None
        )
        if raw_expiry is None and isinstance(cookie, dict):
            raw_expiry = cookie.get("expires")
        try:
            if raw_expiry is not None:
                expiry = float(raw_expiry)
                if expiry > 0:
                    expiries.append(expiry)
                    if (
                        cookie_name in AUTH_COOKIE_NAMES
                        and expiry <= now.timestamp()
                    ):
                        auth_cookie_expired = True
        except (TypeError, ValueError):
            continue
    if not auth_cookie_seen:
        return {
            "healthy": False,
            "category": "missing_cookies",
            "reason": "account is missing authentication cookies",
        }
    if auth_cookie_expired:
        return {
            "healthy": False,
            "category": "expired_cookies",
            "reason": "authentication cookie has expired",
        }
    if expiries and all(expiry <= now.timestamp() for expiry in expiries):
        return {
            "healthy": False,
            "category": "expired_cookies",
            "reason": "all cookie expiries are in the past",
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
    if end_hour == 24:
        fallback_start = end - timedelta(minutes=1)
        if start <= now < end - grace:
            return "regular"
        if end - grace <= now < fallback_start:
            return "close-out"
        if fallback_start <= now <= end + grace:
            return "fallback"
        return "outside"
    if start <= now < end - grace:
        return "regular"
    if end - grace <= now < end:
        return "close-out"
    if end <= now <= end + grace:
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
        r"""(?ims)\\?["']?authorization\\?["']?\s*[:=].*$""",
        "authorization=[redacted]",
        text,
    )
    sensitive_keys = "|".join(
        re.escape(part)
        for part in sorted(
            SENSITIVE_REPORT_KEY_PARTS,
            key=len,
            reverse=True,
        )
    )
    text = re.sub(
        rf"""(?ims)\\?["']?([A-Za-z0-9_.-]*(?:{sensitive_keys})"""
        r"""[A-Za-z0-9_.-]*)\\?["']?\s*[:=].*$""",
        r"\1=[redacted]",
        text,
    )
    return text


def _atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


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

    def mutate():
        try:
            existing = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""
        _atomic_write_text(target, existing + line)

    if FileLock is not None:
        with FileLock(lock, timeout=30):
            mutate()
    else:
        mutate()
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
        _atomic_write_text(
            target,
            json.dumps(
                summary,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )

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
