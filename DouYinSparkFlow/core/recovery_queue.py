"""Fail-closed in-account metadata for automatic recovery (no I/O)."""
from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core import streak_state
from utils.config import get_config

CONFIG_KEY = "automaticRecovery"
KEY = "automatic_recovery_queue"
ACCOUNT_KEY = "automatic_recovery_account"
_ACCOUNT_CATEGORIES = {"browser_stage_timeout", "friend_list_unavailable", "network_unavailable", "browser_page_unavailable"}


def enabled(config=None):
    config = get_config(force_reload=True) if config is None else config
    return isinstance(config, dict) and isinstance(config.get(CONFIG_KEY), dict) and config[CONFIG_KEY].get("enabled") is True


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _parse(value):
    if isinstance(value, datetime):
        return value if _aware(value) else None
    if not isinstance(value, str) or not value:
        return None
    try:
        value = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if _aware(parsed) else None


def _tz():
    name = (os.getenv("SPARKFLOW_TIMEZONE") or os.getenv("TZ") or "Asia/Shanghai").strip()
    try:
        return ZoneInfo(name), name
    except Exception:
        return None, name


def _ctx(now):
    zone, name = _tz()
    if not _aware(now) or zone is None:
        return None
    local = now.astimezone(zone)
    return zone, name, local, local.date().isoformat()


def allowed_now(now, day=None, config=None):
    context = _ctx(now)
    if context is None:
        return False
    if not enabled(config):
        return True
    _, _, local, today = context
    if day is not None and str(day) != today:
        return False
    return (local.hour, local.minute) >= (10, 0) and (local.hour, local.minute) < (23, 50)


def _generation(account):
    value = account.get("login_generation", "") if isinstance(account, dict) else ""
    return value if isinstance(value, str) else None


def _ids(account, target):
    if not isinstance(account, dict) or not isinstance(target, str) or not target.strip():
        return None
    binding = streak_state.target_uid_binding(account, target)
    canonical = streak_state.resolve_target_ref(account, target)
    if not isinstance(canonical, str) or not canonical:
        return None
    refs = [canonical]
    uid = str(binding.get("uid") or "").strip() if binding else ""
    sec = str(binding.get("secUid") or binding.get("sec_uid") or "").strip() if binding else ""
    if uid:
        refs.append("peer:" + uid)
    if sec:
        refs.append("sec:" + sec)
    return canonical, list(dict.fromkeys(refs)), [ref for ref in refs if ref.startswith(("peer:", "sec:"))]


def _valid_base(row, context, generation, now):
    if not isinstance(row, dict) or not isinstance(generation, str):
        return None
    zone, name, _, today = context
    if row.get("timezone") != name or not isinstance(row.get("day"), str) or not isinstance(row.get("generation"), str):
        return None
    source = _parse(row.get("sourceAt"))
    next_at = _parse(row.get("nextAttemptAt"))
    refs = row.get("identityRefs")
    seq = row.get("sequence")
    if (source is None or next_at is None or next_at < source or source > now or
            source.astimezone(zone).date().isoformat() != row["day"] or
            not isinstance(refs, list) or not refs or any(not isinstance(x, str) or not x for x in refs) or
            len(set(refs)) != len(refs) or type(seq) is not int or seq < 0 or
            row.get("kind") not in {"refusal", "unsent"} or
            row.get("state") not in {"waiting", "claimed", "done"}):
        return None
    if row["state"] == "claimed":
        claimed = _parse(row.get("claimedAt"))
        if (claimed is None or claimed > now or claimed < next_at or seq < 1
                or not isinstance(row.get("runId"), str) or not row["runId"]):
            return None
    if row["state"] == "done":
        done = _parse(row.get("doneAt"))
        if done is None or done > now:
            return None
    return source, next_at, refs, today


def _find(account, target, now):
    context = _ctx(now)
    # Capture a prior alias before resolve_target_ref may replace it.  A name
    # whose persisted stable identity changed is a conflict, not a new target.
    prior = ""
    refs_map = account.get("target_refs") if isinstance(account, dict) else None
    if isinstance(refs_map, dict):
        wanted = streak_state.normalize_target_name(target).casefold()
        for name, ref in refs_map.items():
            if streak_state.normalize_target_name(name).casefold() == wanted:
                prior = str(ref or "")
                break
    ident = _ids(account, target)
    generation = _generation(account)
    if context is None or ident is None or generation is None:
        return "bad", None, None, context, ident, generation
    bucket = account.get(KEY)
    if bucket is None:
        return "missing", None, None, context, ident, generation
    if not isinstance(bucket, dict):
        return "bad", None, None, context, ident, generation
    canonical, refs, stable = ident
    hits = []
    for key, row in bucket.items():
        row_refs = row.get("identityRefs") if isinstance(row, dict) else []
        if str(key) in refs or (isinstance(row_refs, list) and set(row_refs).intersection(refs)):
            hits.append((key, row))
    if not hits:
        # The same visible name used to resolve to a queued stable identity.
        # Do not let a rebinding silently turn that into an apparent absence.
        if prior and any(
            isinstance(row, dict) and prior in (row.get("identityRefs") or [])
            for row in bucket.values()
        ):
            return "bad", None, None, context, ident, generation
        return "missing", None, None, context, ident, generation
    if len(hits) != 1:
        return "bad", None, None, context, ident, generation
    key, row = hits[0]
    valid = _valid_base(row, context, generation, now)
    if valid is None:
        return "bad", key, row, context, ident, generation
    source, _, old_refs, today = valid
    if row["day"] != today:
        return "stale", key, row, context, ident, generation
    if row["generation"] != generation:
        return "bad", key, row, context, ident, generation
    if stable and not set(old_refs).intersection(stable):
        return "bad", key, row, context, ident, generation
    if not stable and canonical not in old_refs:
        return "bad", key, row, context, ident, generation
    # Preserve every observed stable alias after a verified overlap.  This is
    # what lets a later secUid upgrade find a peerUid-keyed queue row.
    row["identityRefs"] = list(dict.fromkeys(old_refs + refs))
    row["canonicalRef"] = canonical
    return "current", key, row, context, ident, generation


def target_entry(account, target, now):
    if not enabled():
        return {}
    status, _, row, *_ = _find(account, target, now)
    return dict(row) if status == "current" else ({} if status in {"missing", "stale"} else None)


def _iso(value):
    return value.isoformat(timespec="seconds")


def _source(value, context, now):
    parsed = _parse(value)
    return parsed if parsed and parsed <= now and parsed.astimezone(context[0]).date().isoformat() == context[3] else None


def schedule_target(account, target, now, source_at, kind):
    if not enabled() or kind not in {"refusal", "unsent"}:
        return None
    status, key, old, context, ident, generation = _find(account, target, now)
    if status == "bad" or context is None:
        return None
    source = _source(source_at, context, now)
    if source is None:
        return None
    if status == "current":
        old_source = _parse(old["sourceAt"])
        same_second_first_refusal = (source == old_source and old['state'] == 'claimed'
                                     and old['kind'] == 'unsent' and kind == 'refusal')
        if (source < old_source or old["state"] == "done"
                or (source == old_source and not same_second_first_refusal)):
            return dict(old)
        if old["state"] == "claimed" and (kind != "refusal" or source < _parse(old["claimedAt"])):
            return None
    bucket = account.setdefault(KEY, {})
    if status == "stale":
        del bucket[key]
    canonical, refs, _ = ident
    if status == "current":
        refs = list(dict.fromkeys(old["identityRefs"] + refs))
    next_at = source if kind == "unsent" else source + timedelta(minutes=random.randint(45, 90))
    row = {"day": context[3], "timezone": context[1], "generation": generation, "sourceAt": _iso(source),
           "nextAttemptAt": _iso(next_at), "kind": kind, "state": "waiting", "sequence": old.get("sequence", 0) if status == "current" else 0,
           "identityRefs": refs, "canonicalRef": canonical}
    if status == "current" and key != canonical:
        del bucket[key]
    bucket[canonical] = row
    return dict(row)


def due(account, target, now):
    if not enabled() or not allowed_now(now):
        return False
    status, _, row, *_ = _find(account, target, now)
    return status == "current" and row["state"] == "waiting" and _parse(row["nextAttemptAt"]) <= now


def reserve(account, target, now, run_id, first=False):
    if not enabled() or not allowed_now(now) or not isinstance(run_id, str) or not run_id:
        return None
    status, key, row, context, ident, generation = _find(account, target, now)
    if first and status not in {"missing", "stale"}:
        return None
    if first and status in {"missing", "stale"}:
        canonical, refs, _ = ident
        if status == "stale":
            del account.setdefault(KEY, {})[key]
        row = {"day": context[3], "timezone": context[1], "generation": generation, "sourceAt": _iso(now), "nextAttemptAt": _iso(now), "kind": "unsent", "state": "waiting", "sequence": 0, "identityRefs": refs, "canonicalRef": canonical}
        account.setdefault(KEY, {})[canonical] = row
        key, status = canonical, "current"
    if status != "current" or row["state"] != "waiting" or (not first and _parse(row["nextAttemptAt"]) > now):
        return None
    row.update({"state": "claimed", "claimedAt": _iso(now.astimezone(timezone.utc)), "runId": run_id, "sequence": row["sequence"] + 1})
    canonical = ident[0]
    return {"canonicalRef": canonical, "runId": run_id, "sequence": row["sequence"], "day": row["day"], "generation": generation}


def reservation_matches(account, target, now, run_id, token):
    if not enabled() or not allowed_now(now) or not isinstance(token, dict) or not isinstance(run_id, str):
        return False
    status, _, row, _, ident, generation = _find(account, target, now)
    return status == "current" and row["state"] == "claimed" and row.get("runId") == run_id and all((token.get("canonicalRef") == ident[0], token.get("runId") == run_id, token.get("sequence") == row["sequence"], token.get("day") == row["day"], token.get("generation") == generation))


def finish_target(account, target, now):
    if not enabled():
        return None
    status, _, row, *_ = _find(account, target, now)
    if status != "current":
        return None
    row.update({"state": "done", "doneAt": _iso(now.astimezone(timezone.utc))})
    return dict(row)


def _account_row(account, now):
    context = _ctx(now)
    row = account.get(ACCOUNT_KEY) if isinstance(account, dict) else None
    if row is None:
        return "missing", None, context
    if context is None or not isinstance(row, dict) or row.get("timezone") != context[1] or not isinstance(row.get("day"), str) or row.get("category") not in _ACCOUNT_CATEGORIES:
        return "bad", row, context
    source, next_at = _parse(row.get("sourceAt")), _parse(row.get("nextAttemptAt"))
    if source is None or next_at is None or next_at < source or source > now or source.astimezone(context[0]).date().isoformat() != row["day"]:
        return "bad", row, context
    return ("current" if row["day"] == context[3] else "stale"), row, context


def schedule_account(account, now, source_at, category):
    if not enabled() or category not in _ACCOUNT_CATEGORIES:
        return None
    status, old, context = _account_row(account, now)
    if status == "bad" or context is None:
        return None
    source = _source(source_at, context, now)
    if source is None:
        return None
    if status == "current" and source <= _parse(old["sourceAt"]):
        return dict(old)
    row = {"day": context[3], "timezone": context[1], "sourceAt": _iso(source), "nextAttemptAt": _iso(source + timedelta(minutes=random.randint(45, 90))), "category": category}
    account[ACCOUNT_KEY] = row
    return dict(row)


def account_blocked(account, now):
    if not enabled():
        return False
    status, row, _ = _account_row(account, now)
    return status == "bad" or (status == "current" and _parse(row["nextAttemptAt"]) > now)
