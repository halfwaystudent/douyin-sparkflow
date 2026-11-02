from fastapi import Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from webui import registration
from webui.credentials import credential_slot, run_auth_read, run_credential_work
from webui.auth import current_principal, csrf_token, is_bootstrapped, validate_csrf
from webui.login_guard import LoginThrottle
from webui.users import UserStoreError, authenticate
from utils.secure_storage import encryption_required, storage_cipher


def register_routes(app, render, flash, throttle, secure_cookie):
    attempts = LoginThrottle()

    async def admin_form(request):
        form = await request.form()
        principal = await run_auth_read(current_principal, request)
        if not principal or principal["role"] != "admin":
            return form, None, PlainTextResponse("Forbidden", status_code=403)
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return form, None, PlainTextResponse("Invalid CSRF token", status_code=403)
        return form, principal, None

    async def registration_page(request, *, message="", username="", token="", status=200):
        def page():
            return render(request, "register.html", {
                "registration": registration.policy(), "registration_csrf": csrf_token(request),
                "registration_message": message, "registration_username": username, "invitation_token": token,
            }, status_code=status)
        return await run_auth_read(page)

    def admin_operation(request, expected, function, *args):
        live = current_principal(request)
        if not live or live["role"] != "admin" or any(live.get(key) != expected.get(key) for key in ("username", "session_id")):
            raise UserStoreError("登录状态已变化，请重新登录。")
        return function(*args)

    @app.get("/register")
    async def signup_page(request: Request):
        if await run_auth_read(current_principal, request):
            return RedirectResponse("/", status_code=303)
        return await registration_page(request)

    @app.post("/register")
    async def signup(request: Request):
        if await run_auth_read(current_principal, request):
            return RedirectResponse("/", status_code=303)
        form = await request.form()
        if not await run_auth_read(is_bootstrapped):
            return PlainTextResponse("请先由服务器管理员完成初始化。", status_code=403)
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return await registration_page(request, message="页面已更新，请重新填写注册信息。", status=403)
        client_ip = request.client.host if request.client else ""
        key = "signup:" + client_ip
        wait = attempts.retry_after(client_ip, key)
        if wait:
            return PlainTextResponse("注册尝试过于频繁，请稍后再试。", status_code=429, headers={"Retry-After": str(wait)})
        attempts.record_failure(client_ip, key)
        username = str(form.get("username", "")).strip()
        token = str(form.get("invitation_token", "")).strip()
        password = str(form.get("password", ""))
        if form.get("website"):
            return PlainTextResponse("请求无效。", status_code=400)
        if password != str(form.get("confirm_password", "")):
            return await registration_page(request, message="两次输入的密码不一致。", username=username, token=token, status=400)
        try:
            async with credential_slot(app):
                user = await run_credential_work(registration.register, username, password, token)
        except UserStoreError as exc:
            return await registration_page(request, message=str(exc), username=username, token=token, status=400)
        flash(request, "注册完成，等待管理员审核后即可登录。" if not user["enabled"] else "注册完成，请使用新账号登录。", "success")
        return RedirectResponse("/login", status_code=303)

    @app.post("/admin/registration")
    async def configure_registration(request: Request):
        form, principal, error = await admin_form(request)
        if error:
            return error
        ip = request.client.host if request.client else ""
        if throttle.retry_after(ip, principal["username"]):
            return PlainTextResponse("尝试次数过多，请稍后重试。", status_code=429)
        async with credential_slot(app):
            identity = await run_credential_work(authenticate, principal["username"], str(form.get("current_password", "")))
        live = await run_auth_read(current_principal, request)
        if not live or any(live.get(key) != principal.get(key) for key in ("username", "role", "session_id")):
            return PlainTextResponse("登录状态已变化，请重新登录。", status_code=403)
        if not identity:
            throttle.record_failure(ip, principal["username"])
            return PlainTextResponse("当前密码不正确，注册设置未修改。", status_code=403)
        throttle.record_success(ip, principal["username"])
        if str(form.get("mode", "closed")) == "public" and (
            not secure_cookie or not encryption_required() or storage_cipher() is None
        ):
            return PlainTextResponse("开放注册前，请先按共享部署说明启用 HTTPS、安全 Cookie 和加密存储。", status_code=400)
        try:
            await run_credential_work(admin_operation, request, principal, registration.configure, str(form.get("mode", "closed")), str(form.get("approval_required", "")) == "on", int(form.get("max_users", 100)))
            flash(request, "注册设置已保存。", "success")
        except (ValueError, UserStoreError) as exc:
            flash(request, str(exc), "error")
        return RedirectResponse("/settings?_anchor=registration", status_code=303)

    @app.post("/admin/invitations/create")
    async def create_invitation(request: Request):
        form, principal, error = await admin_form(request)
        if error:
            return error
        try:
            token = await run_credential_work(admin_operation, request, principal, registration.create_invitation, principal["username"], int(form.get("hours", 24)))
        except (ValueError, UserStoreError) as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/settings?_anchor=registration", status_code=303)
        scheme = "https" if secure_cookie else request.url.scheme
        url = f"{scheme}://{request.url.netloc}/register#invite={token}"
        return await run_auth_read(lambda: render(request, "invitation_created.html", {"invitation_url": url, "registration_closed": registration.policy()["mode"] == "closed"}))

    @app.post("/admin/invitations/{invitation_id}/revoke")
    async def revoke_invitation(request: Request, invitation_id: str):
        _, principal, error = await admin_form(request)
        if error:
            return error
        try:
            await run_credential_work(admin_operation, request, principal, registration.revoke_invitation, invitation_id)
        except UserStoreError as exc:
            return PlainTextResponse(str(exc), status_code=403)
        flash(request, "邀请已撤销。", "success")
        return RedirectResponse("/settings?_anchor=registration", status_code=303)
