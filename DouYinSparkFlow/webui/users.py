"""Persistent Web UI users and stable account ownership helpers."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from copy import deepcopy
from pathlib import Path

from filelock import FileLock

from utils.config import (
    get_app_settings,
    get_userData,
    normalize_unique_id,
    save_userData,
    update_user_data,
    users_data_path,
)
from webui.auth import hash_password, verify_password


USERS_FILE = Path(__file__).resolve().parents[1] / "webui_users.json"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class UserStoreError(ValueError):
    """Raised when a Web UI user operation is invalid."""


def _web_users_lock():
    return FileLock(f"{USERS_FILE}.lock", timeout=30)


def normalize_username(username: str) -> str:
    return str(username or "").strip().casefold()


def _coerce_session_version(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _load_raw() -> dict:
    if not USERS_FILE.exists():
        return {"users": [], "auth_epoch": 0}
    text = USERS_FILE.read_text(encoding="utf-8")
    if not text.strip():
        return {"users": [], "auth_epoch": 0}
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("users", []), list):
        raise UserStoreError("webui_users.json must contain a users list")
    data["auth_epoch"] = _coerce_session_version(data.get("auth_epoch"))
    return data


def get_web_users(force_reload: bool = False) -> list[dict]:
    del force_reload
    users = []
    for raw in _load_raw().get("users", []):
        if not isinstance(raw, dict):
            continue
        username = str(raw.get("username", "")).strip()
        if not username:
            continue
        users.append(
            {
                "username": username,
                "role": "user",
                "password_hash": str(raw.get("password_hash", "")),
                "enabled": bool(raw.get("enabled", True)),
                "session_version": _coerce_session_version(
                    raw.get("session_version")
                ),
                "account_refs": list(dict.fromkeys(
                    str(ref).strip() for ref in (raw.get("account_refs") or []) if str(ref).strip()
                )),
            }
        )
    return users


def _save_web_users_locked(users: list[dict], *, auth_epoch=None) -> list[dict]:
    normalized = []
    seen = set()
    assigned = set()
    for raw in users:
        username = str(raw.get("username", "")).strip()
        key = normalize_username(username)
        if not username or key == normalize_username("admin") or key in seen:
            raise UserStoreError("invalid or duplicate Web user")
        if not USERNAME_RE.fullmatch(username):
            raise UserStoreError("Web username may contain only letters, digits, _, ., and -")
        password_hash = str(raw.get("password_hash", ""))
        if not password_hash:
            raise UserStoreError(f"password hash missing for {username}")
        refs = list(dict.fromkeys(
            str(ref).strip() for ref in (raw.get("account_refs") or []) if str(ref).strip()
        ))
        if assigned.intersection(refs):
            raise UserStoreError("one or more accounts are already assigned")
        assigned.update(refs)
        seen.add(key)
        normalized.append(
            {
                "username": username,
                "role": "user",
                "password_hash": password_hash,
                "enabled": bool(raw.get("enabled", True)),
                "session_version": _coerce_session_version(
                    raw.get("session_version")
                ),
                "account_refs": refs,
            }
        )
    if auth_epoch is None:
        auth_epoch = int(_load_raw().get("auth_epoch") or 0)
    _atomic_write_json(
        USERS_FILE,
        {
            "auth_epoch": max(0, int(auth_epoch)),
            "users": normalized,
        },
    )
    try:
        os.chmod(USERS_FILE, 0o600)
    except OSError:
        pass
    return deepcopy(normalized)


def save_web_users(users: list[dict]) -> list[dict]:
    with _web_users_lock():
        auth_epoch = _coerce_session_version(
            _load_raw().get("auth_epoch")
        ) + 1
        normalized = []
        for user in users:
            item = dict(user)
            item["session_version"] = auth_epoch
            normalized.append(item)
        return _save_web_users_locked(
            normalized,
            auth_epoch=auth_epoch,
        )


def find_web_user(username: str) -> dict | None:
    key = normalize_username(username)
    return next((user for user in get_web_users() if normalize_username(user["username"]) == key), None)


def authenticate(username: str, password: str) -> dict | None:
    settings = get_app_settings(force_reload=True)
    admin_username = str(settings.get("admin_username", "admin")).strip() or "admin"
    if normalize_username(username) == normalize_username(admin_username):
        if verify_password(password, settings.get("admin_password_hash", "")):
            return {
                "username": admin_username,
                "role": "admin",
                "account_refs": [],
                "enabled": True,
                "auth_version": _coerce_session_version(
                    settings.get("admin_session_version")
                ),
            }
        return None

    user = find_web_user(username)
    if not user or not user.get("enabled") or not verify_password(password, user.get("password_hash", "")):
        return None
    return {
        "username": user["username"],
        "role": "user",
        "account_refs": list(user.get("account_refs", [])),
        "enabled": True,
        "auth_version": _coerce_session_version(user.get("session_version")),
    }


def ensure_account_refs(accounts: list[dict] | None = None) -> tuple[list[dict], bool]:
    if accounts is None:
        def mutate(current_accounts):
            changed = False
            for account in current_accounts:
                if not str(account.get("account_ref", "")).strip():
                    account["account_ref"] = f"acc-{uuid.uuid4().hex}"
                    changed = True
            return current_accounts, changed

        return update_user_data(mutate, force_reload=True, return_changed=True)

    provided = deepcopy(accounts)
    with FileLock(f"{users_data_path()}.lock", timeout=30):
        current_accounts = deepcopy(get_userData(force_reload=True))
        by_unique_id = {
            normalize_unique_id(account.get("unique_id")): account
            for account in provided
            if normalize_unique_id(account.get("unique_id"))
        }
        by_username = {
            str(account.get("username") or "").strip(): account
            for account in provided
            if str(account.get("username") or "").strip()
        }
        changed = False
        for account in current_accounts:
            if str(account.get("account_ref", "")).strip():
                continue
            source = by_unique_id.get(
                normalize_unique_id(account.get("unique_id"))
            ) or by_username.get(str(account.get("username") or "").strip())
            source_ref = str((source or {}).get("account_ref") or "").strip()
            account["account_ref"] = source_ref or f"acc-{uuid.uuid4().hex}"
            changed = True
        if changed:
            save_userData(current_accounts)
        return current_accounts, changed


def account_by_ref(accounts: list[dict], account_ref: str) -> dict | None:
    target = str(account_ref or "").strip()
    return next((account for account in accounts if str(account.get("account_ref", "")).strip() == target), None)


def account_by_unique_id(accounts: list[dict], unique_id: str) -> dict | None:
    target = normalize_unique_id(unique_id)
    return next((account for account in accounts if normalize_unique_id(account.get("unique_id")) == target), None)


def get_visible_accounts(principal: dict | None, accounts: list[dict] | None = None) -> list[dict]:
    accounts, _ = ensure_account_refs(accounts)
    if principal and principal.get("role") == "admin":
        return accounts
    allowed = set(principal.get("account_refs", [])) if principal else set()
    return [account for account in accounts if account.get("account_ref") in allowed]


def can_access_account(principal: dict | None, account: dict | None) -> bool:
    if not principal or not account:
        return False
    return principal.get("role") == "admin" or account.get("account_ref") in set(principal.get("account_refs", []))


def all_assigned_refs(exclude_username: str | None = None) -> set[str]:
    excluded = normalize_username(exclude_username) if exclude_username else None
    refs = set()
    for user in get_web_users():
        if excluded and normalize_username(user["username"]) == excluded:
            continue
        refs.update(user.get("account_refs", []))
    return refs


def _validate_refs(refs: list[str] | None, accounts: list[dict] | None = None) -> list[str]:
    if accounts is None:
        accounts = get_userData(force_reload=True)
    accounts, _ = ensure_account_refs(accounts)
    valid = {str(account.get("account_ref")) for account in accounts}
    result = list(dict.fromkeys(str(ref).strip() for ref in (refs or []) if str(ref).strip()))
    unknown = [ref for ref in result if ref not in valid]
    if unknown:
        raise UserStoreError("one or more account assignments are invalid")
    return result


def create_web_user(username: str, password: str, *, enabled: bool = True, account_refs: list[str] | None = None) -> dict:
    refs = _validate_refs(account_refs)
    with _web_users_lock():
        auth_epoch = _coerce_session_version(
            _load_raw().get("auth_epoch")
        ) + 1
        username = str(username or "").strip()
        if not USERNAME_RE.fullmatch(username) or normalize_username(username) == normalize_username("admin"):
            raise UserStoreError("invalid Web username")
        if not password:
            raise UserStoreError("password is required")
        if find_web_user(username):
            raise UserStoreError("Web username already exists")
        if all_assigned_refs().intersection(refs):
            raise UserStoreError("one or more accounts are already assigned")
        item = {
            "username": username,
            "role": "user",
            "password_hash": hash_password(password),
            "enabled": bool(enabled),
            "session_version": auth_epoch,
            "account_refs": refs,
        }
        _save_web_users_locked(
            get_web_users() + [item],
            auth_epoch=auth_epoch,
        )
        return deepcopy(item)


def update_web_user(
    username: str,
    *,
    new_username: str | None = None,
    password: str | None = None,
    enabled: bool | None = None,
    account_refs: list[str] | None = None,
) -> dict:
    refs = _validate_refs(account_refs) if account_refs is not None else None
    with _web_users_lock():
        auth_epoch = _coerce_session_version(
            _load_raw().get("auth_epoch")
        )
        users = get_web_users()
        target = next((user for user in users if normalize_username(user["username"]) == normalize_username(username)), None)
        if target is None:
            raise UserStoreError("Web user not found")
        original_username = target["username"]
        revoke_session = False
        if new_username is not None:
            new_username = str(new_username).strip()
            if not USERNAME_RE.fullmatch(new_username) or normalize_username(new_username) == normalize_username("admin"):
                raise UserStoreError("invalid Web username")
            if normalize_username(new_username) != normalize_username(target["username"]) and find_web_user(new_username):
                raise UserStoreError("Web username already exists")
            revoke_session = (
                normalize_username(new_username)
                != normalize_username(target["username"])
            )
            target["username"] = new_username
        if password:
            target["password_hash"] = hash_password(password)
            revoke_session = True
        if enabled is not None:
            revoke_session = True
            target["enabled"] = bool(enabled)
        if account_refs is not None:
            if all_assigned_refs(original_username).intersection(refs):
                raise UserStoreError("one or more accounts are already assigned")
            target["account_refs"] = refs
        if revoke_session:
            auth_epoch = max(
                auth_epoch,
                _coerce_session_version(target.get("session_version")),
            ) + 1
            target["session_version"] = auth_epoch
        else:
            auth_epoch = max(
                auth_epoch,
                _coerce_session_version(target.get("session_version")),
            )
        _save_web_users_locked(users, auth_epoch=auth_epoch)
        return deepcopy(target)


def remove_account_refs_from_users(account_refs: list[str] | set[str]) -> int:
    refs = {str(ref).strip() for ref in account_refs if str(ref).strip()}
    if not refs:
        return 0
    with _web_users_lock():
        users = get_web_users()
        changed = 0
        for user in users:
            original = list(user.get("account_refs", []))
            filtered = [ref for ref in original if ref not in refs]
            if filtered != original:
                user["account_refs"] = filtered
                changed += 1
        if changed:
            _save_web_users_locked(users)
        return changed


def delete_web_user(username: str) -> bool:
    with _web_users_lock():
        auth_epoch = _coerce_session_version(
            _load_raw().get("auth_epoch")
        ) + 1
        key = normalize_username(username)
        users = get_web_users()
        remaining = [user for user in users if normalize_username(user["username"]) != key]
        if len(remaining) == len(users):
            return False
        _save_web_users_locked(remaining, auth_epoch=auth_epoch)
        return True
