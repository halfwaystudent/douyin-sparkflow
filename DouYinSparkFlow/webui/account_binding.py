"""Coordinate login storage and ownership, and expose safe binding progress."""

import asyncio
from copy import deepcopy
from contextvars import ContextVar
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse

from core import identity
from core.streak_state import LOGIN_FAILURE_CATEGORIES
from webui import users


_BEFORE_BINDING_WRITE = ContextVar("before_binding_write", default=None)


def capture_login_before_write(account):
    callback = _BEFORE_BINDING_WRITE.get()
    if callback is not None:
        callback(deepcopy(account))


class BindingCommitError(RuntimeError):
    def __init__(self, *, uncertain=False):
        self.uncertain = uncertain
        super().__init__("账号绑定未完成，请先在账号页核对结果，再重试。" if uncertain else "账号归属未能保存，本次登录改动已撤回，请稍后重试。")


def commit_login_binding(save_callback, *, update_accounts, claim_callback=None, owner_username=""):
    """Keep ownership changes out of the account-save/claim interval.

    Storage uses two encrypted files. If the ownership write fails, restore only
    this save's unchanged fields. A newer login or background update is kept.
    Process crashes still require checking the stored account before retrying.
    """
    with users._web_users_lock():
        snapshot = {"captured": False, "account": None}
        def capture(account):
            snapshot.update(captured=True, account=account)
        token = _BEFORE_BINDING_WRITE.set(capture)
        try:
            account, action = save_callback()
        finally:
            _BEFORE_BINDING_WRITE.reset(token)
        if claim_callback is None:
            return account, action
        try:
            if owner_username and users.account_owner_username(account.get("account_ref", "")) == owner_username:
                return account, action
            claim_callback(account)
        except Exception as exc:
            if owner_username:
                try:
                    if users.account_owner_username(account.get("account_ref", "")) == owner_username:
                        return account, action
                except Exception:
                    raise BindingCommitError(uncertain=True) from exc
            if not snapshot["captured"]:
                raise BindingCommitError(uncertain=True) from exc
            original = snapshot["account"]
            if original is not None and original.get("account_ref") != account.get("account_ref"):
                raise BindingCommitError(uncertain=True) from exc
            missing = object()

            def undo(accounts):
                current = next((row for row in accounts if row.get("account_ref") == account.get("account_ref")), None)
                if current is None:
                    return True, False
                if identity.record_fingerprint(current) != identity.record_fingerprint(account):
                    return False, False
                if original is None:
                    if current != account:
                        return False, False
                    accounts.remove(current)
                else:
                    for key in set(original) | set(account):
                        old, saved = original.get(key, missing), account.get(key, missing)
                        if old == saved or current.get(key, missing) != saved:
                            continue
                        if old is missing:
                            current.pop(key, None)
                        else:
                            current[key] = deepcopy(old)
                return True, True

            try:
                restored = update_accounts(undo, force_reload=True)
            except Exception:
                raise BindingCommitError(uncertain=True) from exc
            raise BindingCommitError(uncertain=not restored) from exc
        return account, action


def binding_progress(account):
    check = dict(account.get("login_verification") or {})
    health = account.get("account_health") or {}
    state = identity.identity_state(account)
    if state == "conflict" or check.get("state") == "identity_conflict":
        check["state"] = "identity_conflict"
    elif any(account.get(field) for field in ("login_required", "needs_relogin", "patrol_login_failure")) or (health.get("healthy") is False and health.get("category") in LOGIN_FAILURE_CATEGORIES | {"expired_cookies", "missing_cookies"}):
        check.update(state="login_required", category=str(health.get("category") or "login_required"), reason=str(health.get("reason") or check.get("reason") or "账号需要重新登录"))
    elif not check.get("state"):
        check["state"] = "unverified" if account.get("pending_login_verification") or state != "verified" else "verified"
    key = quote(str(account.get("account_ref") or account.get("unique_id") or ""), safe="")
    return {
        "account_ref": str(account.get("account_ref") or ""),
        "unique_id": str(account.get("unique_id") or ""),
        "username": str(account.get("username") or ""),
        "login_generation": identity.account_login_generation(account),
        "identity_state": state,
        "verification": {field: str(check.get(field) or "")[:300] for field in ("state", "category", "reason")},
        "has_targets": bool(account.get("targets") or account.get("group_targets")),
        "next_url": "/accounts?_anchor=account-" + key,
    }


def register_routes(app, *, principal, read_accounts):
    @app.get("/accounts/binding-status")
    async def status(request: Request):
        current = principal(request)
        if not current:
            return JSONResponse({"ok": False, "error": "请先登录平台"}, status_code=401)
        accounts = await asyncio.to_thread(read_accounts, force_reload=True)
        live = principal(request)
        if not live or any(live.get(key) != current.get(key) for key in ("username", "role", "session_id")):
            return JSONResponse({"ok": False, "error": "登录状态已变化，请重新登录"}, status_code=401)
        if live.get("role") != "admin":
            refs = set(live.get("account_refs") or [])
            accounts = [row for row in accounts if row.get("account_ref") in refs]
        wanted = str(request.query_params.get("account_ref") or "")
        if wanted:
            accounts = [row for row in accounts if row.get("account_ref") == wanted]
            if not accounts:
                return JSONResponse({"ok": False, "error": "找不到当前账号"}, status_code=404)
        return JSONResponse({"ok": True, "accounts": [binding_progress(account) for account in accounts]}, headers={"Cache-Control": "no-store"})
