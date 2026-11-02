"""Shared login-desktop workspace lease and FIFO queue."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path

from utils.config import repo_root


def _default_lock_path() -> Path:
    candidates = [
        Path("/opt/douyin-sparkflow/state"),
        repo_root().parent / "state",
        repo_root() / "state",
    ]
    for candidate in candidates:
        if candidate.exists() or candidate.parent.exists():
            return candidate / "login-workspace.lock.json"
    return candidates[0] / "login-workspace.lock.json"


LOCK_PATH = _default_lock_path()
LOCK_TTL_SECONDS = 180
QUEUE_MAX_SIZE = 32
_MUTEX = threading.Lock()


def _empty_state() -> dict:
    return {"version": 2, "phase": "idle", "active": None, "queue": []}


def _read_raw() -> dict | None:
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _normalize_state(data: dict | None) -> dict:
    if not data:
        return _empty_state()
    if "active" in data or "queue" in data:
        state = _empty_state()
        state.update({key: data.get(key) for key in ("version", "phase", "active", "queue")})
        state["version"] = 2
        state["phase"] = str(state.get("phase") or ("active" if state.get("active") else "idle"))
        state["queue"] = [item for item in (state.get("queue") or []) if isinstance(item, dict)]
        return state
    # Backward compatibility with the original single-lock file.
    legacy = dict(data)
    now = time.time()
    legacy.setdefault("ticket", "legacy-" + uuid.uuid4().hex)
    legacy.setdefault("requested_at", now)
    legacy.setdefault("started_at", legacy.get("acquired_at", now))
    legacy.setdefault("last_heartbeat_at", legacy.get("acquired_at", now))
    return {"version": 2, "phase": "active", "active": legacy, "queue": []}


def _write_state(state: dict) -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".login-workspace.", suffix=".tmp", dir=str(LOCK_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, LOCK_PATH)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _delete_state() -> None:
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def _now() -> float:
    return time.time()


def _expired(active: dict | None, now: float | None = None) -> bool:
    if not active:
        return False
    now = _now() if now is None else now
    try:
        return now - float(active.get("last_heartbeat_at", active.get("acquired_at", 0))) > LOCK_TTL_SECONDS
    except (TypeError, ValueError):
        return True


def _public_item(item: dict | None) -> dict | None:
    if not item:
        return None
    return {
        "ticket": item.get("ticket", ""),
        "username": item.get("username", ""),
        "account_ref": item.get("account_ref", ""),
        "requested_at": item.get("requested_at", 0),
        "started_at": item.get("started_at", 0),
        "last_heartbeat_at": item.get("last_heartbeat_at", 0),
    }


def get_workspace_state() -> dict:
    with _MUTEX:
        return deepcopy(_normalize_state(_read_raw()))


def get_lock() -> dict | None:
    """Compatibility helper: return the current active lease only."""
    return get_workspace_state().get("active")


def owns(lock: dict | None, *, username: str, session_id: str, account_ref: str | None = None, ticket: str | None = None) -> bool:
    if not lock:
        return False
    if str(lock.get("username")) != str(username) or str(lock.get("session_id")) != str(session_id):
        return False
    if account_ref is not None and str(lock.get("account_ref", "")) != str(account_ref):
        return False
    if ticket is not None and str(lock.get("ticket", "")) != str(ticket):
        return False
    return True


def _new_item(username: str, session_id: str, account_ref: str, now: float, mode: str = "relogin") -> dict:
    return {
        "ticket": "ticket-" + uuid.uuid4().hex,
        "mode": mode if mode in {"relogin", "add"} else "relogin",
        "username": username,
        "session_id": session_id,
        "account_ref": account_ref,
        "requested_at": now,
        "started_at": now,
        "last_heartbeat_at": now,
    }


def find_request(state: dict, *, username: str, session_id: str) -> tuple[str, dict | None, int]:
    active = state.get("active")
    if active and str(active.get("username")) == str(username) and str(active.get("session_id")) == str(session_id):
        return "active", active, 0
    for index, item in enumerate(state.get("queue") or [], start=1):
        if str(item.get("username")) == str(username) and str(item.get("session_id")) == str(session_id):
            return "queued", item, index
    return "none", None, -1


def request_workspace(*, username: str, session_id: str, account_ref: str = "", mode: str = "relogin") -> dict:
    with _MUTEX:
        state = _normalize_state(_read_raw())
        now = _now()
        if state.get("phase") == "resetting":
            queue = state.setdefault("queue", [])
            kind, existing, position = find_request(state, username=username, session_id=session_id)
            if kind == "queued":
                return {"state": "queued", "position": position, "request": deepcopy(existing), "workspace": state}
            if len(queue) >= QUEUE_MAX_SIZE:
                return {"state": "full", "position": -1, "request": None, "workspace": state}
            item = _new_item(username, session_id, account_ref, now, mode=mode)
            item["started_at"] = 0
            item["last_heartbeat_at"] = 0
            queue.append(item)
            _write_state(state)
            return {"state": "queued", "position": len(queue), "request": deepcopy(item), "workspace": state}
        elif state.get("active") and not _expired(state["active"], now):
            kind, existing, position = find_request(state, username=username, session_id=session_id)
            if kind == "active":
                existing["last_heartbeat_at"] = now
                _write_state(state)
                return {"state": "active", "position": 0, "request": deepcopy(existing), "workspace": state}
            if kind == "queued":
                return {"state": "queued", "position": position, "request": deepcopy(existing), "workspace": state}
            queue = state.setdefault("queue", [])
            if len(queue) >= QUEUE_MAX_SIZE:
                return {"state": "full", "position": -1, "request": None, "workspace": state}
            item = _new_item(username, session_id, account_ref, now, mode=mode)
            item["started_at"] = 0
            item["last_heartbeat_at"] = 0
            queue.append(item)
            _write_state(state)
            return {"state": "queued", "position": len(queue), "request": deepcopy(item), "workspace": state}
        else:
            item = _new_item(username, session_id, account_ref, now, mode=mode)
            state = {"version": 2, "phase": "active", "active": item, "queue": state.get("queue", [])}
            _write_state(state)
            return {"state": "active", "position": 0, "request": deepcopy(item), "workspace": state}


def heartbeat(*, username: str, session_id: str, ticket: str = "", account_ref: str = "") -> bool:
    with _MUTEX:
        state = _normalize_state(_read_raw())
        active = state.get("active")
        if not owns(active, username=username, session_id=session_id, account_ref=account_ref or None, ticket=ticket or None):
            return False
        active["last_heartbeat_at"] = _now()
        _write_state(state)
        return True


def begin_expiration() -> dict | None:
    with _MUTEX:
        state = _normalize_state(_read_raw())
        if state.get("phase") != "active" or not _expired(state.get("active")):
            return None
        old = deepcopy(state.get("active"))
        state["phase"] = "resetting"
        state["active"] = None
        state["transition_reason"] = "heartbeat_timeout"
        state["transition_at"] = _now()
        _write_state(state)
        return old


def begin_force_reset(*, clear_queue: bool = False) -> dict | None:
    with _MUTEX:
        state = _normalize_state(_read_raw())
        active = deepcopy(state.get("active"))
        if not active and not state.get("queue"):
            return None
        state["phase"] = "resetting"
        state["active"] = None
        if clear_queue:
            state["queue"] = []
        state["transition_reason"] = "admin_reset"
        state["transition_at"] = _now()
        _write_state(state)
        return active


def begin_release(*, username: str, session_id: str, ticket: str = "", account_ref: str = "") -> dict | None:
    with _MUTEX:
        state = _normalize_state(_read_raw())
        active = state.get("active")
        if not owns(active, username=username, session_id=session_id, account_ref=account_ref or None, ticket=ticket or None):
            return None
        old = deepcopy(active)
        state["phase"] = "resetting"
        state["active"] = None
        state["transition_reason"] = "released"
        state["transition_at"] = _now()
        _write_state(state)
        return old


def finish_transition() -> dict | None:
    with _MUTEX:
        state = _normalize_state(_read_raw())
        queue = state.get("queue") or []
        if queue:
            item = queue.pop(0)
            now = _now()
            item["started_at"] = now
            item["last_heartbeat_at"] = now
            state["phase"] = "active"
            state["active"] = item
            state["queue"] = queue
            state.pop("transition_reason", None)
            state.pop("transition_at", None)
            _write_state(state)
            return deepcopy(item)
        _delete_state()
        return None


def cancel_request(*, username: str, session_id: str, ticket: str = "") -> tuple[str, dict | None]:
    with _MUTEX:
        state = _normalize_state(_read_raw())
        active = state.get("active")
        if owns(active, username=username, session_id=session_id, ticket=ticket or None):
            state["phase"] = "resetting"
            state["active"] = None
            state["transition_reason"] = "cancelled"
            state["transition_at"] = _now()
            _write_state(state)
            return "active", deepcopy(active)
        queue = state.get("queue") or []
        for index, item in enumerate(queue):
            if str(item.get("username")) == str(username) and str(item.get("session_id")) == str(session_id) and (not ticket or str(item.get("ticket")) == str(ticket)):
                removed = queue.pop(index)
                state["queue"] = queue
                if queue or state.get("active"):
                    _write_state(state)
                else:
                    _delete_state()
                return "queued", deepcopy(removed)
        return "none", None


def workspace_status(*, username: str, session_id: str) -> dict:
    state = get_workspace_state()
    kind, item, position = find_request(state, username=username, session_id=session_id)
    if kind == "active":
        remaining = max(0, int(LOCK_TTL_SECONDS - (_now() - float(item.get("last_heartbeat_at", _now())))))
        return {"state": "active", "position": 0, "ticket": item.get("ticket", ""), "remaining_seconds": remaining, "workspace": state}
    if kind == "queued":
        return {"state": "queued", "position": position, "ticket": item.get("ticket", ""), "remaining_seconds": 0, "workspace": state}
    if state.get("phase") == "resetting":
        return {"state": "resetting", "position": 0, "ticket": "", "remaining_seconds": 0, "workspace": state}
    return {"state": "closed", "position": 0, "ticket": "", "remaining_seconds": 0, "workspace": state}


# Backward-compatible single-lease helpers used by existing tests and callers.
def acquire(*, username: str, session_id: str, account_ref: str = "", force: bool = False) -> tuple[bool, dict | None]:
    with _MUTEX:
        state = _normalize_state(_read_raw())
        if force:
            state = _empty_state()
        active = state.get("active")
        if active and not owns(active, username=username, session_id=session_id, account_ref=account_ref):
            return False, active
    result = request_workspace(username=username, session_id=session_id, account_ref=account_ref)
    return result["state"] == "active", result.get("request")


def refresh(*, username: str, session_id: str, account_ref: str = "") -> bool:
    return heartbeat(username=username, session_id=session_id, account_ref=account_ref)


def release(*, username: str | None = None, session_id: str | None = None, force: bool = False) -> bool:
    with _MUTEX:
        state = _normalize_state(_read_raw())
        active = state.get("active")
        if not active:
            return False
        if not force:
            if username is not None and str(active.get("username")) != str(username):
                return False
            if session_id is not None and str(active.get("session_id")) != str(session_id):
                return False
        _delete_state()
        return True
