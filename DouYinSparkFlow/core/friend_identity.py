"""Friend uid collection, target binding and rename following.

The creator chat page loads the friend list and, on its own, POSTs
``/aweme/v1/creator/im/user_detail/``. The JSON response carries every
friend's uid (``user_id``), current ``nickname`` and ``SecretUseId``. This
module only *listens* to that response: it never builds, sends or replays a
request, and it never calls a Douyin protocol API.

Data written on an account (all inside ``update_user_data`` mutators):

``friend_uids``
    ``{uid: {"nickname", "secUid", "lastSeenAt", "previousNickname"?}}`` - the
    latest uid <-> nickname mapping seen on any friend-list load.
``target_uid_bindings``
    ``{target name: {"uid", "secUid", "boundAt", "source", ...}}`` - targets
    bound to a friend uid. Only a Web re-selection rebinds a bound target.
``friend_uid_meta``
    Diagnostics of the last reconcile (uid count, trust, unbound reasons).
``identity_events``
    ``[{"key", "date", "category", "targetName", "oldName", "newName",
    "uidSuffix", "reason", "degraded", "recordedAt"}]`` - one event per
    account / target identity / category / schedule day, consumed by the
    notification package.

Friend names and scan completeness stay owned by the page DOM; uid data only
adds stable keys and never adds or removes friend entries.
"""

import asyncio
import hashlib
import json
import logging
from copy import deepcopy
from datetime import timedelta
from urllib.parse import urlparse

from core import streak_state
from utils.config import update_user_data
from utils.logger import setup_logger


logger = setup_logger(level=logging.DEBUG)

USER_DETAIL_PATH = "/aweme/v1/creator/im/user_detail/"

FRIEND_UIDS_KEY = "friend_uids"
BINDINGS_KEY = streak_state.TARGET_UID_BINDINGS_KEY
META_KEY = "friend_uid_meta"
EVENTS_KEY = "identity_events"
IDENTITY_EVENT_RETENTION_DAYS = 14

STATUS_FOUND = "found"
STATUS_RENAMED = "renamed"
STATUS_MISSING = "missing"
STATUS_UNBOUND = "unbound"
# Not enough evidence to conclude anything (incomplete scan, follow limit...).
STATUS_UNCHECKED = "unchecked"

EVENT_RENAMED = "renamed"
EVENT_MISSING = "missing"
EVENT_AMBIGUOUS = "rename_ambiguous"

# Account fields this module may change; copied back onto in-memory users.
IDENTITY_SYNC_KEYS = (
    "target_refs",
    "target_states",
    "message_history",
    "failure_queue",
    "account_failure",
    "friend_index",
    "friends_cache",
    FRIEND_UIDS_KEY,
    BINDINGS_KEY,
    META_KEY,
    EVENTS_KEY,
)


# --------------------------------------------------------------------------
# helpers


def mask_uid(value):
    """Log-safe form of a uid: only the last four characters survive."""
    text = str(value or "").strip()
    if not text:
        return ""
    return f"***{text[-4:]}"


def uid_suffix(value):
    text = str(value or "").strip()
    return text[-4:] if text else ""


def _name(value):
    return streak_state.normalize_target_name(value)


def _key(value):
    return _name(value).casefold()


def _iso(now):
    return streak_state._iso(now)


def _schedule_date(now):
    return now.astimezone(streak_state._state_schedule_timezone()).date().isoformat()


def uid_stable_keys(uid, sec_uid=""):
    keys = []
    if sec_uid:
        keys.append(f"sec:{sec_uid}")
    if uid:
        keys.append(f"peer:{uid}")
    return keys


def uid_target_ref(uid, sec_uid=""):
    """The ledger ref of a uid, following streak_state's sec > peer ranking."""
    refs = streak_state._refs_from_record({"secUid": sec_uid, "peerUserId": uid})
    return refs[0] if refs else ""


# --------------------------------------------------------------------------
# parsing


def _coerce_uid(value):
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return ""
    # Creator responses also use opaque identifiers; they are not necessarily
    # numeric platform IDs. Preserve their case and never interpret them as URLs.
    if not 1 <= len(value) <= 256 or not value.isascii():
        return ""
    return value if all(ch.isalnum() or ch in "_-" for ch in value) else ""


def parse_user_detail_payload(payload):
    """Parse a ``user_detail`` response body.

    Returns ``(entries, error)``. ``entries`` is ``[{"uid", "nickname",
    "secUid"}]`` deduplicated by uid (a later item wins). Items with a missing
    or mistyped uid / nickname are skipped. When the body as a whole cannot be
    used, ``entries`` is empty and ``error`` names the reason.
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            return [], "undecodable_body"
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return [], "invalid_json"
    if not isinstance(payload, dict):
        return [], "unexpected_payload_type"
    status_code = payload.get("status_code", 0)
    if status_code not in (0, "0", None):
        return [], f"status_code={status_code}"
    user_list = payload.get("user_list")
    if not isinstance(user_list, list):
        return [], "missing_user_list"
    merged = {}
    for item in user_list:
        if not isinstance(item, dict):
            continue
        uid = _coerce_uid(item.get("user_id"))
        user = item.get("user")
        if not uid or not isinstance(user, dict):
            continue
        nickname = user.get("nickname")
        if not isinstance(nickname, str) or not _name(nickname):
            continue
        sec_uid = user.get("SecretUseId")
        if not isinstance(sec_uid, str):
            sec_uid = ""
        merged.pop(uid, None)
        merged[uid] = {
            "uid": uid,
            "nickname": _name(nickname),
            "secUid": sec_uid.strip(),
        }
    if user_list and not merged:
        return [], "no_usable_user_entries"
    return list(merged.values()), ""


def merge_uid_entries(*batches):
    """Merge entry lists by uid; later batches override the nickname."""
    merged = {}
    for batch in batches:
        for entry in batch or []:
            if not isinstance(entry, dict):
                continue
            uid = _coerce_uid(entry.get("uid"))
            nickname = _name(entry.get("nickname"))
            if not uid or not nickname:
                continue
            previous = merged.pop(uid, {})
            merged[uid] = {
                "uid": uid,
                "nickname": nickname,
                "secUid": str(entry.get("secUid") or previous.get("secUid") or "").strip(),
            }
    return list(merged.values())


# --------------------------------------------------------------------------
# passive collector


class UserDetailCollector:
    """Passively read the page's own ``user_detail`` responses.

    Usage::

        collector = UserDetailCollector(account_name).attach(page)
        ...  # the page loads the friend list by itself
        await collector.drain()
        entries = collector.entries()  # [{"uid", "nickname", "secUid"}]
        collector.detach()

    ``version`` increases each time a response adds data, so a scan loop can
    react to new batches without waiting.
    """

    def __init__(self, account_name=""):
        self.account_name = str(account_name or "")
        self._entries = {}
        self._tasks = set()
        self._page = None
        self.responses_seen = 0
        self.responses_parsed = 0
        self.errors = []
        self.version = 0

    @staticmethod
    def matches(url):
        try:
            parsed = urlparse(str(url or ""))
        except ValueError:
            return False
        return parsed.path.rstrip("/") == USER_DETAIL_PATH.rstrip("/")

    def attach(self, page):
        if self._page is not None or page is None:
            return self
        try:
            page.on("response", self._on_response)
        except Exception as exc:  # noqa: BLE001 - collection must never break a read
            self._record_error(f"attach_failed:{type(exc).__name__}")
            return self
        self._page = page
        return self

    def detach(self):
        page, self._page = self._page, None
        if page is None:
            return
        for method_name in ("remove_listener", "off"):
            method = getattr(page, method_name, None)
            if not callable(method):
                continue
            try:
                method("response", self._on_response)
                return
            except Exception:  # noqa: BLE001
                continue

    def _on_response(self, response):
        try:
            url = response.url
        except Exception:  # noqa: BLE001
            return
        if not self.matches(url):
            return
        self.responses_seen += 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._record_error("no_event_loop")
            return
        task = loop.create_task(self._read(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read(self, response):
        try:
            payload = await response.json()
        except Exception:  # noqa: BLE001 - fall back to the raw text
            try:
                payload = await response.text()
            except Exception as exc:  # noqa: BLE001
                self._record_error(f"unreadable_body:{type(exc).__name__}")
                return
        self.ingest(payload)

    def ingest(self, payload):
        entries, error = parse_user_detail_payload(payload)
        if error:
            self._record_error(error)
            return []
        self.responses_parsed += 1
        if entries:
            for entry in entries:
                previous = self._entries.pop(entry["uid"], {})
                merged = dict(entry)
                if not merged.get("secUid") and previous.get("secUid"):
                    merged["secUid"] = previous["secUid"]
                self._entries[entry["uid"]] = merged
            self.version += 1
        return entries

    def _record_error(self, error):
        self.errors.append(str(error))
        logger.warning(
            "Friend uid collection for %s could not use a user_detail response: %s",
            self.account_name or "unknown",
            error,
        )

    async def drain(self, timeout=5.0):
        pending = [task for task in self._tasks if not task.done()]
        if not pending:
            return
        try:
            await asyncio.wait(pending, timeout=timeout)
        except Exception:  # noqa: BLE001
            logger.debug("Draining user_detail readers failed", exc_info=True)

    def entries(self):
        return [dict(entry) for entry in self._entries.values()]

    def summary(self):
        return {
            "responses": self.responses_seen,
            "parsed": self.responses_parsed,
            "uidCount": len(self._entries),
            "errors": list(self.errors[-5:]),
        }


# --------------------------------------------------------------------------
# account data


def _dict_field(account, key):
    value = account.get(key)
    if not isinstance(value, dict):
        value = {}
        account[key] = value
    return value


def merge_friend_uids(account, entries, now):
    """Store the latest uid <-> nickname mapping; never drops known uids."""
    if not entries:
        return False
    store = _dict_field(account, FRIEND_UIDS_KEY)
    seen_at = _iso(now)
    for entry in entries:
        uid = entry["uid"]
        record = dict(store.get(uid) or {})
        previous = _name(record.get("nickname"))
        if previous and previous != entry["nickname"]:
            record["previousNickname"] = previous
        record["nickname"] = entry["nickname"]
        if entry.get("secUid"):
            record["secUid"] = entry["secUid"]
        record["lastSeenAt"] = seen_at
        store[uid] = record
    return True


def _strip_uid_keys(record):
    keys = [
        key
        for key in record.get("stableKeys") or []
        if not str(key or "").startswith(("peer:", "sec:"))
    ]
    changed = keys != list(record.get("stableKeys") or [])
    record["stableKeys"] = keys
    for field in ("peerUserId", "secUid"):
        if field in record:
            record.pop(field, None)
            changed = True
    return changed


def apply_uid_keys_to_friend_index(account, entries):
    """Attach ``peer:``/``sec:`` stable keys to existing friend-index entries.

    Entries are only updated, never added or removed. A name owned by several
    uids loses its uid keys (it cannot identify one friend); keys of a uid now
    shown under another nickname are removed from the stale name.
    """
    friend_index = account.get("friend_index")
    if not entries or not isinstance(friend_index, dict):
        return False
    by_name = {}
    for entry in entries:
        by_name.setdefault(_key(entry["nickname"]), []).append(entry)
    current_name_of_uid = {entry["uid"]: _key(entry["nickname"]) for entry in entries}
    changed = False
    for index_name, record in list(friend_index.items()):
        if not isinstance(record, dict):
            continue
        record_key = _key(index_name) or _key(record.get("visibleName"))
        owners = by_name.get(record_key) or []
        if len(owners) == 1:
            owner = owners[0]
            wanted = uid_stable_keys(owner["uid"], owner.get("secUid"))
            others = [
                key
                for key in record.get("stableKeys") or []
                if not str(key or "").startswith(("peer:", "sec:"))
            ]
            new_keys = wanted + others
            if (
                list(record.get("stableKeys") or []) != new_keys
                or record.get("peerUserId") != owner["uid"]
                or record.get("uidAmbiguous")
            ):
                record["stableKeys"] = new_keys
                record["peerUserId"] = owner["uid"]
                if owner.get("secUid"):
                    record["secUid"] = owner["secUid"]
                else:
                    record.pop("secUid", None)
                record.pop("uidAmbiguous", None)
                changed = True
            continue
        if len(owners) > 1:
            changed = _strip_uid_keys(record) or changed
            if not record.get("uidAmbiguous"):
                record["uidAmbiguous"] = True
                changed = True
            continue
        # No uid for this name in the batch: keep what was saved, unless the
        # saved uid is now shown under another nickname (it was renamed).
        record_refs = set(streak_state._refs_from_record(record))
        stale = any(
            f"peer:{uid}" in record_refs and name_key != record_key
            for uid, name_key in current_name_of_uid.items()
        )
        if stale:
            changed = _strip_uid_keys(record) or changed
    return changed


def target_binding(account, target_name):
    return streak_state.target_uid_binding(account, target_name)


def _set_binding(account, target_name, uid, sec_uid, *, source, now, extra=None):
    bindings = _dict_field(account, BINDINGS_KEY)
    wanted = _key(target_name)
    for existing in list(bindings):
        if _key(existing) == wanted:
            bindings.pop(existing, None)
    record = {
        "uid": uid,
        "secUid": sec_uid or "",
        "boundAt": _iso(now),
        "source": source,
    }
    if extra:
        record.update(extra)
    bindings[_name(target_name)] = record
    return record


def _pop_binding(account, target_name):
    bindings = account.get(BINDINGS_KEY)
    if not isinstance(bindings, dict):
        return None
    wanted = _key(target_name)
    for existing in list(bindings):
        if _key(existing) == wanted:
            return bindings.pop(existing)
    return None


def bind_target(account, target_name, uid, sec_uid="", *, source, now=None):
    """Bind ``target_name`` to a friend uid and move its ledger to that identity."""
    now = streak_state._now(now)
    uid = _coerce_uid(uid)
    if not uid or not _name(target_name):
        return None
    if not sec_uid:
        sec_uid = str(((account.get(FRIEND_UIDS_KEY) or {}).get(uid) or {}).get("secUid") or "")
    record = _set_binding(account, target_name, uid, sec_uid, source=source, now=now)
    streak_state.resolve_target_ref(account, _name(target_name))
    logger.info(
        "Bound target %s of %s to friend uid %s source=%s",
        _name(target_name),
        account.get("username", "unknown"),
        mask_uid(uid),
        source,
    )
    return record


def uids_for_nickname(account, nickname):
    wanted = _key(nickname)
    matches = []
    for uid, record in (account.get(FRIEND_UIDS_KEY) or {}).items():
        if isinstance(record, dict) and _key(record.get("nickname")) == wanted:
            matches.append((str(uid), str(record.get("secUid") or "")))
    return matches


def _target_names(account):
    names = []
    for target in account.get("targets") or []:
        name = _name(_target_display(target))
        if name:
            names.append(name)
    return names


def _target_display(target):
    if isinstance(target, dict):
        for key in ("name", "displayName", "nickname", "username"):
            value = str(target.get(key) or "").strip()
            if value:
                return value
        return ""
    return str(target or "")


def apply_web_target_selection(account, previous_targets, now=None):
    """Bind targets the operator selected in the Web form.

    A target that is newly selected (or not bound yet) binds to the uid its
    nickname uniquely maps to in ``friend_uids``. Targets that stayed selected
    keep their binding; this is the only path that may replace a binding.
    Bindings of removed targets are dropped (the ledgers are kept).
    Returns ``{"bound": [...], "unbound": {name: reason}}``.
    """
    now = streak_state._now(now)
    current = _target_names(account)
    current_keys = {_key(name) for name in current}
    previous_keys = {_key(_target_display(name)) for name in previous_targets or []}
    bindings = account.get(BINDINGS_KEY)
    if isinstance(bindings, dict):
        for name in list(bindings):
            if _key(name) not in current_keys:
                bindings.pop(name, None)
    bound, unbound = [], {}
    for name in current:
        newly_selected = _key(name) not in previous_keys
        if not newly_selected and target_binding(account, name):
            continue
        matches = uids_for_nickname(account, name)
        if len(matches) == 1:
            uid, sec_uid = matches[0]
            bind_target(account, name, uid, sec_uid, source="web", now=now)
            bound.append(name)
        elif len(matches) > 1:
            unbound[name] = "ambiguous_nickname"
        else:
            unbound[name] = "no_uid_for_nickname"
    if unbound or bound:
        meta = _dict_field(account, META_KEY)
        unbound_meta = dict(meta.get("unbound") or {})
        for name in bound:
            unbound_meta.pop(name, None)
        unbound_meta.update(unbound)
        meta["unbound"] = {
            name: reason
            for name, reason in unbound_meta.items()
            if _key(name) in current_keys
        }
    return {"bound": bound, "unbound": unbound}


def _uid_token(uid):
    """Opaque per-uid identity used to dedupe events without storing the uid."""
    return hashlib.sha256(f"sparkflow-uid:{uid}".encode("utf-8")).hexdigest()[:16]


def uid_token(uid, prefix="peer"):
    """Stable log/report-safe reference that cannot disclose a full uid."""
    if not str(uid or "").strip():
        return ""
    return f"{prefix}:h-{_uid_token(str(uid).strip())[:10]}"


def redact_target_ref(ref):
    """Replace a peer/sec uid ref with a stable short hash token."""
    value = str(ref or "")
    if value.startswith("peer:"):
        return uid_token(value[5:], "peer")
    if value.startswith("sec:"):
        return uid_token(value[4:], "sec")
    return value


def _event_identity(uid, target_name):
    return f"uid:{_uid_token(uid)}" if uid else f"name:{_key(target_name)}"


def record_identity_event(
    account,
    *,
    category,
    target_name,
    now,
    uid="",
    old_name="",
    new_name="",
    reason="",
    degraded=False,
):
    """Persist one identity event per identity/category/schedule day.

    The identity is the bound uid (stored only as an opaque token plus its last
    four digits) or, for unbound targets, the nickname. Returns the new event,
    or ``None`` when the same event was already recorded today.
    """
    events = account.get(EVENTS_KEY)
    if not isinstance(events, list):
        events = []
    local_today = now.astimezone(streak_state._state_schedule_timezone()).date()
    today = local_today.isoformat()
    cutoff = (local_today - timedelta(days=IDENTITY_EVENT_RETENTION_DAYS)).isoformat()
    events = [
        event
        for event in events
        if isinstance(event, dict) and str(event.get("date") or "") >= cutoff
    ]
    identity = _event_identity(uid, old_name or target_name)
    key = f"{today}|{category}|{identity}"
    account[EVENTS_KEY] = events
    if any(event.get("key") == key for event in events):
        return None
    event = {
        "key": key,
        "date": today,
        "category": category,
        "identity": identity,
        "targetName": _name(target_name),
        "oldName": _name(old_name),
        "newName": _name(new_name),
        "uidSuffix": uid_suffix(uid),
        "reason": str(reason or ""),
        "degraded": bool(degraded),
        "recordedAt": _iso(now),
    }
    events.append(event)
    return dict(event)


def _follows_today(account, uid, now):
    today = _schedule_date(now)
    identity = _event_identity(uid, "")
    return sum(
        1
        for event in account.get(EVENTS_KEY) or []
        if isinstance(event, dict)
        and event.get("date") == today
        and event.get("category") == EVENT_RENAMED
        and event.get("identity") == identity
    )


def _replace_target_name(account, old_name, new_name):
    old_key = _key(old_name)
    targets = account.get("targets")
    if not isinstance(targets, list):
        return
    for index, target in enumerate(targets):
        if isinstance(target, dict):
            for field in ("name", "displayName", "nickname", "username"):
                if _key(target.get(field)) == old_key:
                    target[field] = new_name
                    break
        elif _key(target) == old_key:
            targets[index] = new_name


def rename_target(account, old_name, new_name, *, now=None):
    """Rename a bound target and carry its whole ledger to the new name.

    The ledger already belongs to the bound stable ref; this also moves the
    name-keyed history / failure queue rows, the account failure list, the
    binding and the friend cache so nothing restarts as a new unsent target.
    Returns ``""`` on success or a reason string when the rename was refused.
    """
    now = streak_state._now(now)
    old_name = _name(old_name)
    new_name = _name(new_name)
    if not old_name or not new_name or old_name == new_name:
        return "nothing_to_rename"
    if _key(old_name) != _key(new_name) and any(
        _key(name) == _key(new_name) for name in _target_names(account)
    ):
        return "new_name_already_a_target"
    binding = target_binding(account, old_name)
    if not binding:
        return "target_not_bound"
    ref = streak_state.resolve_target_ref(account, old_name)

    _replace_target_name(account, old_name, new_name)
    _pop_binding(account, old_name)
    _set_binding(
        account,
        new_name,
        binding["uid"],
        binding.get("secUid") or "",
        source=str(binding.get("source") or "auto"),
        now=now,
        extra={
            "boundAt": binding.get("boundAt") or _iso(now),
            "previousName": old_name,
            "renamedAt": _iso(now),
        },
    )

    for bucket_name, time_key in (
        ("message_history", "sentAt"),
        ("failure_queue", "lastAttemptAt"),
    ):
        bucket = account.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        for key in [key for key in bucket if _key(key) == _key(old_name)]:
            streak_state._move_ref_record(account, bucket_name, key, new_name, time_key)

    account_failure = account.get("account_failure")
    if isinstance(account_failure, dict):
        affected = list(account_failure.get("affectedTargets") or [])
        if any(_key(item) == _key(old_name) for item in affected):
            account_failure["affectedTargets"] = [
                new_name if _key(item) == _key(old_name) else item
                for item in affected
            ]

    refs = streak_state._ref_bucket(account)
    items = [(name, value) for name, value in refs.items() if name != new_name]
    refs.clear()
    refs[new_name] = ref
    for name, value in items:
        refs[name] = value
    if old_name not in refs:
        refs[old_name] = ref

    state = streak_state._state_bucket(account).get(ref)
    if isinstance(state, dict):
        state["displayName"] = new_name

    cache = account.get("friends_cache")
    if isinstance(cache, list) and not any(_key(item) == _key(new_name) for item in cache):
        account["friends_cache"] = [
            new_name if _key(item) == _key(old_name) else item for item in cache
        ]

    streak_state.resolve_target_ref(account, new_name)
    return ""


def _uid_data_trust(entries, scan_complete, scanned_keys):
    if not entries:
        return False, "no_uid_data"
    if not scan_complete:
        return False, "scan_incomplete"
    known = {_key(entry["nickname"]) for entry in entries}
    unmatched = [key for key in scanned_keys if key not in known]
    if unmatched:
        return False, f"uid_data_mismatch:{len(unmatched)}_names_without_uid"
    return True, ""


def reconcile_targets(
    account,
    uid_entries,
    *,
    scan_complete,
    scanned_names,
    now=None,
    allow_partial_rename=False,
    max_follow_per_day=None,
):
    """Compare the account's targets with one friend-list load and act on it.

    Mutates ``account`` in place (call it inside an ``update_user_data``
    mutator, or use :func:`persist_identity_scan`). Steps:

    1. merge ``uid_entries`` into ``friend_uids`` and the friend index keys;
    2. auto-bind unbound targets whose nickname maps to exactly one uid;
    3. classify every target: ``found`` / ``renamed`` / ``missing`` /
       ``unbound`` / ``unchecked`` and follow renames of bound targets;
    4. record ``renamed`` / ``missing`` identity events (deduped per day).

    ``missing`` is only concluded when ``scan_complete`` is true. A bound
    target is judged by uid only when the uid data is trusted (complete scan and
    every scanned name has a uid); otherwise the check degrades to nicknames.
    Renames need a complete scan unless ``allow_partial_rename`` (the send
    scan, which must select the friend before the list end is reached).
    Bound targets are never rebound here.
    """
    now = streak_state._now(now)
    entries = merge_uid_entries(uid_entries)
    by_uid = {entry["uid"]: entry for entry in entries}
    by_name = {}
    for entry in entries:
        by_name.setdefault(_key(entry["nickname"]), []).append(entry)
    scanned_keys = {_key(name) for name in scanned_names or [] if _key(name)}
    trusted, trust_reason = _uid_data_trust(entries, scan_complete, scanned_keys)

    changed = False
    if entries:
        changed = merge_friend_uids(account, entries, now) or changed
        # Refresh/patrol partial reads preserve the last complete index. The
        # sender's explicit same-run rename mode still needs its live index keys.
        if scan_complete or allow_partial_rename:
            changed = apply_uid_keys_to_friend_index(account, entries) or changed

    results, renames, new_events = [], [], []
    unbound_reasons = {}

    def add_event(**kwargs):
        event = record_identity_event(account, now=now, **kwargs)
        if event:
            new_events.append(event)
        return event

    for name in list(_target_names(account)):
        key = _key(name)
        binding = target_binding(account, name)
        current_owners = by_name.get(key) or []
        historical_owners = uids_for_nickname(account, name)
        auto_binding_ambiguous = bool(
            binding
            and binding.get("source") == "auto"
            and trusted
            and (len(current_owners) > 1 or len(historical_owners) > 1)
        )
        if auto_binding_ambiguous:
            # Preserve the uid ledger, but withdraw the unsafe nickname binding.
            _pop_binding(account, name)
            streak_state._ref_bucket(account).pop(name, None)
            binding = {}
            changed = True
        result = {
            "target": name,
            "status": STATUS_UNCHECKED,
            "reason": "",
            "bound": bool(binding),
            "uidSuffix": uid_suffix((binding or {}).get("uid")),
            "degraded": False,
            "autoBound": False,
        }
        if not binding:
            owners = by_name.get(key) or []
            if (
                not auto_binding_ambiguous
                and len(owners) == 1
                and trusted
                and len(current_owners) == 1
                and len(historical_owners) == 1
                and key in scanned_keys
                and key in scanned_keys
            ):
                owner = owners[0]
                bind_target(account, name, owner["uid"], owner.get("secUid"), source="auto", now=now)
                changed = True
                binding = target_binding(account, name)
                result.update(
                    bound=True,
                    autoBound=True,
                    status=STATUS_FOUND,
                    uidSuffix=uid_suffix(owner["uid"]),
                )
                results.append(result)
                continue
            if (
                auto_binding_ambiguous
                or len(owners) > 1
                or len(current_owners) > 1
                or len(historical_owners) > 1
            ):
                reason = "ambiguous_nickname"
            elif not trusted:
                reason = trust_reason
            elif not entries:
                reason = "no_uid_data"
            else:
                reason = "nickname_not_in_uid_data"
            if scan_complete and key not in scanned_keys and not owners:
                result.update(status=STATUS_MISSING, reason="nickname_not_in_friend_list")
                add_event(
                    category=EVENT_MISSING,
                    target_name=name,
                    reason="nickname_not_in_friend_list",
                )
            elif scan_complete or key in scanned_keys or owners:
                result.update(status=STATUS_UNBOUND, reason=reason)
                unbound_reasons[name] = reason
            else:
                result.update(status=STATUS_UNCHECKED, reason="scan_incomplete")
            results.append(result)
            continue

        uid = str(binding.get("uid") or "")
        entry = by_uid.get(uid)
        if entry:
            if entry.get("secUid") and entry["secUid"] != binding.get("secUid"):
                _set_binding(
                    account,
                    name,
                    uid,
                    entry["secUid"],
                    source=str(binding.get("source") or "auto"),
                    now=now,
                    extra={
                        key_: value
                        for key_, value in binding.items()
                        if key_ not in {"uid", "secUid", "source"}
                    },
                )
                streak_state.resolve_target_ref(account, name)
                changed = True
            if _key(entry["nickname"]) == key:
                result["status"] = STATUS_FOUND
            elif not (scan_complete or allow_partial_rename):
                result.update(status=STATUS_UNCHECKED, reason="rename_deferred_scan_incomplete")
            elif len(uids_for_nickname(account, entry["nickname"])) > 1:
                reason = "rename_ambiguous_nickname"
                result.update(status=STATUS_UNCHECKED, reason=reason)
                add_event(
                    category=EVENT_AMBIGUOUS, target_name=name, uid=uid,
                    old_name=name, new_name=entry["nickname"], reason=reason,
                )
            elif key in scanned_keys:
                reason = "rename_old_name_still_visible"
                result.update(status=STATUS_UNCHECKED, reason=reason)
                add_event(
                    category=EVENT_AMBIGUOUS, target_name=name, uid=uid,
                    old_name=name, new_name=entry["nickname"], reason=reason,
                )
            elif (
                max_follow_per_day is not None
                and _follows_today(account, uid, now) >= max_follow_per_day
            ):
                result.update(status=STATUS_UNCHECKED, reason="rename_follow_limit_reached")
            else:
                new_name = entry["nickname"]
                refused = rename_target(account, name, new_name, now=now)
                if refused:
                    result.update(status=STATUS_UNCHECKED, reason=f"rename_refused:{refused}")
                    logger.warning(
                        "Not following rename of target %s of %s uid=%s: %s",
                        name,
                        account.get("username", "unknown"),
                        mask_uid(uid),
                        refused,
                    )
                else:
                    changed = True
                    renames.append(
                        {"oldName": name, "newName": new_name, "uidSuffix": uid_suffix(uid)}
                    )
                    result.update(
                        target=new_name,
                        status=STATUS_RENAMED,
                        oldName=name,
                        newName=new_name,
                    )
                    add_event(
                        category=EVENT_RENAMED,
                        target_name=new_name,
                        uid=uid,
                        old_name=name,
                        new_name=new_name,
                    )
                    logger.info(
                        "Target of %s renamed by friend uid %s: %s -> %s",
                        account.get("username", "unknown"),
                        mask_uid(uid),
                        name,
                        new_name,
                    )
            results.append(result)
            continue

        if not scan_complete:
            if key in scanned_keys:
                result["status"] = STATUS_FOUND
                result["degraded"] = True
                result["reason"] = "matched_by_nickname"
            else:
                result.update(status=STATUS_UNCHECKED, reason="scan_incomplete")
        elif trusted:
            result.update(status=STATUS_MISSING, reason="uid_not_in_friend_list")
            add_event(
                category=EVENT_MISSING,
                target_name=name,
                uid=uid,
                reason="uid_not_in_friend_list",
            )
        else:
            result["degraded"] = True
            if key in scanned_keys:
                result.update(status=STATUS_FOUND, reason=f"degraded:{trust_reason}")
            else:
                result.update(status=STATUS_MISSING, reason=f"degraded:{trust_reason}")
                add_event(
                    category=EVENT_MISSING,
                    target_name=name,
                    uid=uid,
                    reason=f"degraded:{trust_reason}",
                    degraded=True,
                )
        results.append(result)

    if new_events:
        changed = True
    meta = _dict_field(account, META_KEY)
    previous_meta = deepcopy(meta)
    if entries:
        meta["lastCollectedAt"] = _iso(now)
        meta["lastUidCount"] = len(entries)
    meta["lastReconcileAt"] = _iso(now)
    meta["lastScanComplete"] = bool(scan_complete)
    meta["uidDataTrusted"] = bool(trusted)
    meta["trustReason"] = trust_reason
    current_keys = {_key(name) for name in _target_names(account)}
    unbound_meta = {
        name: reason
        for name, reason in dict(meta.get("unbound") or {}).items()
        if _key(name) in current_keys and not target_binding(account, name)
    }
    unbound_meta.update(unbound_reasons)
    meta["unbound"] = unbound_meta
    if previous_meta.get("unbound") != meta.get("unbound"):
        changed = True
    if entries or scan_complete:
        changed = True

    return {
        "changed": bool(changed),
        "uidCount": len(entries),
        "scanComplete": bool(scan_complete),
        "uidDataTrusted": bool(trusted),
        "trustReason": trust_reason,
        "targets": results,
        "renamed": renames,
        "events": new_events,
        "targetsAfter": list(_target_names(account)),
    }


def _match_account(accounts, user):
    ref = str(user.get("account_ref") or "").strip()
    if ref:
        for account in accounts:
            if str(account.get("account_ref") or "").strip() == ref:
                return account
    unique_id = normalize_unique_id(user.get("unique_id"))
    if unique_id:
        for account in accounts:
            if normalize_unique_id(account.get("unique_id")) == unique_id:
                return account
    username = str(user.get("username") or "").strip()
    if username:
        for account in accounts:
            if str(account.get("username") or "").strip() == username:
                return account
    return None


def sync_user_identity_fields(user, account):
    """Copy identity-related fields of a stored account onto an in-memory user."""
    for key in IDENTITY_SYNC_KEYS:
        if key in account:
            user[key] = deepcopy(account[key])
        else:
            user.pop(key, None)


def persist_identity_scan(
    user,
    uid_entries,
    *,
    scan_complete,
    scanned_names,
    now=None,
    allow_partial_rename=False,
    max_follow_per_day=None,
    sync_user=True,
):
    """Run :func:`reconcile_targets` on the stored account under the data lock.

    Returns the reconcile result (``None`` when the account is gone). When
    ``sync_user`` is true the identity fields of ``user`` are refreshed from
    the stored account; ``user["targets"]`` is left to the caller because a
    send run keeps only the due subset there.
    """
    now = streak_state._now(now)

    def mutate(accounts):
        matched = _match_account(accounts, user)
        if matched is None:
            return None, False
        result = reconcile_targets(
            matched,
            uid_entries,
            scan_complete=scan_complete,
            scanned_names=scanned_names,
            now=now,
            allow_partial_rename=allow_partial_rename,
            max_follow_per_day=max_follow_per_day,
        )
        snapshot = {key: deepcopy(matched[key]) for key in IDENTITY_SYNC_KEYS if key in matched}
        return (result, snapshot), result["changed"]

    saved = update_user_data(mutate, force_reload=True)
    if saved is None:
        logger.warning(
            "Could not find account to persist friend uid data for user=%s",
            user.get("username", "unknown"),
        )
        return None
    result, snapshot = saved
    if sync_user:
        sync_user_identity_fields(user, snapshot)
    summary = {}
    for item in result["targets"]:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    logger.info(
        "Friend uid reconcile for %s uids=%s complete=%s trusted=%s reason=%s "
        "renamed=%s outcomes=%s",
        user.get("username", "unknown"),
        result["uidCount"],
        result["scanComplete"],
        result["uidDataTrusted"],
        result["trustReason"] or "-",
        [f"{item['oldName']}->{item['newName']}" for item in result["renamed"]],
        summary,
    )
    return result
