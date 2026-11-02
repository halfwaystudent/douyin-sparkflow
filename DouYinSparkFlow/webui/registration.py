"""Registration policy and invitations share the user-store transaction."""

import hashlib
import secrets
import time
import uuid
from copy import deepcopy

from webui import users
from webui.auth import hash_password
from webui.login_guard import password_problem


MODES = {"closed", "invite", "public"}
DEFAULT_POLICY = {"mode": "closed", "approval_required": True, "max_users": 100}
MAX_INVITATIONS = 200


def policy():
    data = dict(DEFAULT_POLICY, **users._load_raw().get("registration", {}))
    if data["mode"] not in MODES:
        data["mode"] = "closed"
    return data


def configure(mode, approval_required, max_users):
    if mode not in MODES or not 1 <= max_users <= 1000:
        raise users.UserStoreError("注册设置无效，用户上限须在 1 到 1000 之间。")
    with users._web_users_lock():
        data = users._load_raw()
        data["registration"] = {"mode": mode, "approval_required": bool(approval_required), "max_users": max_users}
        users._atomic_write_json(users.USERS_FILE, data)


def invitations():
    return [dict(id=digest[:16], **value) for digest, value in users._load_raw().get("invitations", {}).items()
            if value["expires_at"] > time.time()]


def create_invitation(created_by, hours=24):
    if not 1 <= hours <= 168:
        raise users.UserStoreError("邀请有效期须在 1 到 168 小时之间。")
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    with users._web_users_lock():
        data = users._load_raw()
        active = {key: value for key, value in data.get("invitations", {}).items() if value["expires_at"] > time.time()}
        if len(active) >= MAX_INVITATIONS:
            raise users.UserStoreError("未使用的邀请过多，请先撤销不用的邀请。")
        active[digest] = {"created_by": created_by, "expires_at": time.time() + hours * 3600}
        data["invitations"] = active
        users._atomic_write_json(users.USERS_FILE, data)
    return token


def revoke_invitation(invitation_id):
    with users._web_users_lock():
        data = users._load_raw()
        data["invitations"] = {key: value for key, value in data.get("invitations", {}).items() if key[:16] != invitation_id}
        users._atomic_write_json(users.USERS_FILE, data)


def register(username, password, invitation_token=""):
    username = str(username).strip()
    if not users.USERNAME_RE.fullmatch(username) or users._reserved_username(username):
        raise users.UserStoreError("用户名不可用。请使用 1–64 位字母、数字、下划线、点或短横线。")
    problem = password_problem(password, username)
    if problem:
        raise users.UserStoreError(problem)
    with users._web_users_lock():
        raw = users._load_raw()
        settings = dict(DEFAULT_POLICY, **raw.get("registration", {}))
        if settings["mode"] not in {"invite", "public"}:
            raise users.UserStoreError("管理员已关闭注册。")
        existing = users.get_web_users()
        if len(existing) >= settings["max_users"]:
            raise users.UserStoreError("暂时无法创建新用户，请联系管理员。")
        if any(users.normalize_username(item["username"]) == users.normalize_username(username) for item in existing):
            raise users.UserStoreError("用户名不可用，请换一个。")
        digest = hashlib.sha256(str(invitation_token).strip().encode()).hexdigest()
        invitation = raw.get("invitations", {}).get(digest)
        if settings["mode"] == "invite" or invitation_token:
            if not invitation or invitation["expires_at"] <= time.time():
                raise users.UserStoreError("邀请无效或已过期，请联系管理员获取新的邀请。")
        epoch = int(raw.get("auth_epoch", 0)) + 1
        item = {"username": username, "user_id": uuid.uuid4().hex, "role": "user",
                "password_hash": hash_password(password), "enabled": not settings["approval_required"],
                "registration_pending": settings["approval_required"], "session_version": epoch, "account_refs": []}
        # Only publish the user and consume the invitation in one atomic write.
        raw["users"] = existing + [item]
        raw["auth_epoch"] = epoch
        if invitation:
            raw["invitations"].pop(digest)
        users._atomic_write_json(users.USERS_FILE, raw)
        return deepcopy(item)
