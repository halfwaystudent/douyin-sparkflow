import json
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse
from webauthn.helpers.exceptions import WebAuthnException

from webui.auth import current_principal, issue_session, refreshed_identity, validate_csrf
from webui.credentials import run_auth_read, run_credential_work
from webui.passkeys import login_identity
from webui.login_guard import LoginThrottle
from webui.users import authenticate, find_web_user, stable_user_id, update_web_user


class _SessionChanged(ValueError):
    pass


def register_passkey_routes(app, store, throttle):
    challenge_throttle = LoginThrottle()

    def error(message, status=400):
        return JSONResponse({"error": message}, status_code=status)

    def require_live(request, expected):
        live = current_principal(request)
        if not live or any(live.get(key) != expected.get(key) for key in ("username", "role", "session_id")):
            raise _SessionChanged("登录状态已变化，请重新登录。")
        return live

    @app.post("/admin/users/{username}/revoke-passkeys")
    async def revoke_user_passkeys(request: Request, username: str):
        principal = await run_auth_read(current_principal, request)
        if not principal or principal["role"] != "admin":
            return error("Forbidden", 403)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return error("Invalid CSRF token", 403)

        def revoke():
            require_live(request, principal)
            user = find_web_user(username)
            if not store or not user:
                return False
            store.revoke_all("user:" + stable_user_id(user), authorize=lambda: require_live(request, principal))
            update_web_user(username, revoke_sessions=True)
            return True

        try:
            if not await run_credential_work(revoke):
                return error("Not found", 404)
            await run_auth_read(require_live, request, principal)
        except _SessionChanged as exc:
            return error(str(exc), 401)
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/settings?_anchor=users", status_code=303)

    @app.post("/auth/passkeys/{action}")
    async def login_passkey(request: Request, action: str):
        if not store:
            return error("未启用通行密钥。", 404)
        form = await request.form()
        username = str(form.get("username", ""))[:64] if action == "options" else request.session.get("passkey_username", "")
        client_ip = request.client.host if request.client else ""
        wait = throttle.retry_after(client_ip, username)
        if wait:
            return JSONResponse({"error": "尝试次数过多，请稍后重试。"}, status_code=429, headers={"Retry-After": str(wait)})
        binding = request.session.setdefault("passkey_binding", secrets.token_urlsafe(32))
        try:
            if action == "options":
                if challenge_throttle.retry_after(client_ip, username):
                    return error("尝试次数过多，请稍后重试。", 429)
                challenge_throttle.record_failure(client_ip, username)

                def begin():
                    identity = login_identity(username)
                    if not identity:
                        raise ValueError("无法使用通行密钥登录，请使用密码。")
                    return identity, store.begin(identity, binding)

                identity, options = await run_credential_work(begin)
                request.session["passkey_username"] = identity["username"]
                return JSONResponse(options)
            if action != "verify":
                return error("Not found", 404)
            identity = await run_credential_work(store.finish, str(form.get("challenge_id", "")), binding, json.loads(str(form.get("credential", "{}"))))
            identity = await run_auth_read(refreshed_identity, identity)
            if not identity:
                raise _SessionChanged("登录状态已变化，请重新登录。")
        except (ValueError, TypeError, KeyError, WebAuthnException):
            throttle.record_failure(client_ip, username)
            return error("通行密钥验证失败或已过期，请重试或使用密码。")
        throttle.record_success(client_ip, identity["username"])
        challenge_throttle.record_success(client_ip, identity["username"])
        issue_session(request, identity["username"], role=identity["role"], account_refs=identity["account_refs"], auth_version=identity["auth_version"])
        return JSONResponse({"ok": True, "redirect": "/"})

    @app.post("/account/passkeys/{action}")
    async def manage_passkey(request: Request, action: str):
        if not store:
            return error("未启用通行密钥。", 404)
        principal = await run_auth_read(current_principal, request)
        if not principal:
            return error("请先登录。", 401)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return error("请求校验失败，请刷新页面。", 403)
        client_ip = request.client.host if request.client else ""
        if action == "options":
            wait = throttle.retry_after(client_ip, principal["username"])
            if wait:
                return error("尝试次数过多，请稍后重试。", 429)
        if action not in {"options", "verify", "delete"}:
            return error("Not found", 404)

        def manage():
            require_live(request, principal)
            identity = login_identity(principal["username"])
            if not identity:
                raise _SessionChanged("登录已失效。")
            binding = principal["session_id"]
            if action == "options":
                if not authenticate(principal["username"], str(form.get("current_password", ""))):
                    return None
                # Password hashing may overlap a password reset or revocation.
                require_live(request, principal)
                return store.begin(identity, binding, registration=True, authorize=lambda: require_live(request, principal))
            if action == "verify":
                store.finish(str(form.get("challenge_id", "")), binding, json.loads(str(form.get("credential", "{}"))),
                             registration=True, identity=identity, label=str(form.get("label", "")), authorize=lambda: require_live(request, principal))
            else:
                store.delete(identity, str(form.get("credential_id", "")), authorize=lambda: require_live(request, principal))
            return {"ok": True}

        try:
            result = await run_credential_work(manage)
            await run_auth_read(require_live, request, principal)
            if result is None:
                throttle.record_failure(client_ip, principal["username"])
                return error("当前密码不正确。", 403)
            if action == "options":
                throttle.record_success(client_ip, principal["username"])
            return JSONResponse(result)
        except _SessionChanged as exc:
            return error(str(exc), 401)
        except (ValueError, TypeError, KeyError, WebAuthnException):
            return error("通行密钥验证失败或已过期，请重新操作。")
