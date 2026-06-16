import json
import logging
import os
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.error
import urllib.request

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

logger = logging.getLogger(__name__)

from core.friends import fetch_account_friends
from core.tasks import run_browser_tasks
from utils.config import (
    get_app_settings,
    get_config,
    get_userData,
    normalize_unique_id,
    save_app_settings,
    save_config,
    save_userData,
    upsert_user_account,
)
from webui.auth import (
    bootstrap_admin_password,
    clear_session,
    csrf_token,
    current_user,
    is_bootstrapped,
    is_https_request,
    issue_session,
    update_admin_password,
    validate_csrf,
    verify_password,
)
from webui.ops import (
    get_ops_snapshot,
    read_log_tail,
    refresh_proxy,
    restart_proxy,
    run_task_now,
    run_unsent_retry_now,
    sync_daily_schedule_from_config,
    update_daily_schedule,
)


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DEBUG_ARTIFACTS_DIR = BASE_DIR.parent / "logs" / "debug_artifacts"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _dedupe_targets(values):
    seen = set()
    result = []
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _split_target_entries(values):
    expanded = []
    for value in values:
        raw = str(value).replace(",", "\n")
        expanded.extend(raw.splitlines())
    return _dedupe_targets(expanded)


def extract_targets_from_form(form):
    if hasattr(form, "getlist"):
        checkbox_targets = _split_target_entries(form.getlist("targets"))
        if checkbox_targets:
            return checkbox_targets
    raw_targets = str(form.get("targets", ""))
    return _split_target_entries([raw_targets])


def find_account(accounts, unique_id):
    normalized = normalize_unique_id(unique_id)
    for account in accounts:
        if normalize_unique_id(account.get("unique_id")) == normalized:
            return account
    return None


def is_account_enabled(account):
    return bool(account.get("enabled", True))


def coerce_int(value, default, minimum=0):
    try:
        return max(minimum, int(str(value).strip()))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _schedule_timezone():
    return timezone(timedelta(hours=8), name="Asia/Shanghai")


def _parse_sent_at(raw_value):
    if not raw_value:
        return None
    raw = str(raw_value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_schedule_timezone())
    return parsed.astimezone(_schedule_timezone())


def _target_sent_today(account, target_name):
    entry = dict(account.get("message_history") or {}).get(target_name) or {}
    sent_at = _parse_sent_at(entry.get("sentAt"))
    return bool(sent_at and sent_at.date() == datetime.now(_schedule_timezone()).date())


def login_desktop_api_url():
    settings = get_app_settings(force_reload=True)
    return str(os.getenv("SPARKFLOW_LOGIN_DESKTOP_API_URL") or settings.get("login_desktop_api_url") or "http://127.0.0.1:18090").rstrip("/")


def login_desktop_public_url(request: Request) -> str:
    host = request.url.hostname or "127.0.0.1"
    scheme = request.url.scheme or "http"
    port = str(os.getenv("LOGIN_DESKTOP_PUBLIC_PORT") or "8788").strip() or "8788"
    return f"{scheme}://{host}:{port}/vnc.html?autoconnect=1&resize=scale&view_only=0"


def call_login_desktop(path: str, *, method: str = "GET", payload: dict | None = None, timeout: int = 20) -> dict:
    url = f"{login_desktop_api_url()}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"login-desktop API error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"login-desktop unavailable: {exc.reason}") from exc


def save_exported_login_result(login_result: dict, *, relogin_unique_id: str = "", display_name: str = "") -> tuple[dict, str]:
    unique_id = normalize_unique_id(login_result.get("unique_id"))
    username = str(display_name or login_result.get("username") or "").strip()
    cookies = list(login_result.get("cookies") or [])
    if not unique_id or not username or not cookies:
        raise RuntimeError("Exported login result is incomplete")

    accounts = get_userData(force_reload=True)

    if relogin_unique_id:
        target = find_account(accounts, relogin_unique_id)
        if not target:
            raise RuntimeError("Target account not found for relogin")
        target["unique_id"] = unique_id
        target["username"] = username
        target["cookies"] = cookies
        target.setdefault("enabled", True)
        save_userData(accounts)
        return target, "updated"

    existing = find_account(accounts, unique_id)
    if existing:
        existing["username"] = username
        existing["cookies"] = cookies
        existing.setdefault("enabled", True)
        save_userData(accounts)
        return existing, "updated"

    account = upsert_user_account(unique_id, username, cookies, [])
    return account, "created"


def create_app():
    settings = get_app_settings()

    @asynccontextmanager
    async def lifespan(_app):
        """Synchronize configured startup state during the Web UI lifespan."""
        result = sync_daily_schedule_from_config()
        if getattr(result, "returncode", 1) != 0:
            logger.warning("schedule startup sync failed: %s", getattr(result, "stderr", ""))
        yield

    app = FastAPI(title="DouYin Spark Flow Admin", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings["session_secret"],
        max_age=settings["session_max_age_seconds"],
        same_site="lax",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    DEBUG_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/debug-artifacts", StaticFiles(directory=str(DEBUG_ARTIFACTS_DIR)), name="debug-artifacts")

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        tb_text = "".join(tb)
        logger.error("Unhandled exception on %s %s:\n%s", request.method, request.url.path, tb_text)
        return PlainTextResponse(f"Internal Server Error\n\n{tb_text}", status_code=500)

    def render_template(request, template_name, context=None, status_code=200):
        base_context = context or {}
        base_context.update(
            {
                "request": request,
                "current_user": current_user(request),
                "csrf_token": csrf_token(request) if current_user(request) else "",
                "is_https": is_https_request(request),
                "app_settings": get_app_settings(force_reload=True),
                "login_desktop_public_url": login_desktop_public_url(request),
            }
        )
        return templates.TemplateResponse(request, template_name, base_context, status_code=status_code)

    def redirect(path="/", status_code=303):
        return RedirectResponse(url=path, status_code=status_code)

    def require_user(request):
        if not current_user(request):
            return redirect("/login")
        return None

    def console_context(request):
        return {
            "flash": pop_flash(request),
            "accounts": get_userData(force_reload=True),
            "runtime_config": get_config(force_reload=True),
            "ops": get_ops_snapshot(),
        }

    def flash(request, message, level="info"):
        request.session["flash"] = {"message": message, "level": level}

    def pop_flash(request):
        return request.session.pop("flash", None)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if current_user(request):
            return redirect("/")
        return render_template(
            request,
            "login.html",
            {
                "flash": pop_flash(request),
                "bootstrapped": is_bootstrapped(),
            },
        )

    @app.post("/bootstrap")
    async def bootstrap(request: Request):
        if is_bootstrapped():
            flash(request, "管理员账号已初始化，请直接登录。", "warning")
            return redirect("/login")

        form = await request.form()
        username = str(form.get("username", "admin")).strip() or "admin"
        password = str(form.get("password", ""))
        confirm = str(form.get("confirm_password", ""))
        if not password or password != confirm:
            flash(request, "初始化失败，请输入一致的管理员密码。", "error")
            return redirect("/login")

        bootstrap_admin_password(password, username=username)
        flash(request, "管理员账号已创建，请登录控制台。", "success")
        return redirect("/login")

    @app.post("/login")
    async def login_action(request: Request):
        if not is_bootstrapped():
            flash(request, "请先创建管理员密码。", "warning")
            return redirect("/login")

        form = await request.form()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        settings = get_app_settings(force_reload=True)
        if username != settings["admin_username"] or not verify_password(password, settings["admin_password_hash"]):
            flash(request, "用户名或密码不正确。", "error")
            return redirect("/login")

        issue_session(request, username)
        flash(request, "已登录控制台。", "success")
        return redirect("/")

    @app.post("/logout")
    async def logout_action(request: Request):
        clear_session(request)
        return redirect("/login")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        return render_template(
            request,
            "dashboard.html",
            console_context(request),
        )

    @app.get("/login-workspace", response_class=HTMLResponse)
    async def login_workspace_page(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        return render_template(
            request,
            "login_workspace.html",
            console_context(request),
        )

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        return render_template(
            request,
            "accounts.html",
            console_context(request),
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        return render_template(
            request,
            "settings.html",
            console_context(request),
        )

    @app.get("/ops/send-console", response_class=HTMLResponse)
    async def send_console_page(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        return render_template(
            request,
            "send_console.html",
            {
                "flash": pop_flash(request),
                "ops": get_ops_snapshot(),
            },
        )

    @app.post("/accounts/{unique_id}/update")
    async def update_account(request: Request, unique_id: str):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        username = str(form.get("username", "")).strip()
        targets = extract_targets_from_form(form)

        accounts = get_userData(force_reload=True)
        account = find_account(accounts, unique_id)
        if account:
            account["username"] = username or account.get("username", "")
            account["targets"] = targets
            account["enabled"] = str(form.get("enabled", "")) == "on"
            save_userData(accounts)
            flash(request, f"已更新账号 {account['username']}。", "success")
        else:
            flash(request, "未找到账号。", "error")

        return redirect("/accounts")

    @app.post("/accounts/{unique_id}/toggle-enabled")
    async def toggle_account_enabled(request: Request, unique_id: str):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        accounts = get_userData(force_reload=True)
        account = find_account(accounts, unique_id)
        if not account:
            flash(request, "未找到账号。", "error")
            return redirect("/accounts")

        account["enabled"] = not is_account_enabled(account)
        save_userData(accounts)
        flash(
            request,
            f"{account.get('username', 'Account')} 已{'启用' if account['enabled'] else '停用'}自动续火花。",
            "success",
        )
        return redirect("/accounts")

    @app.post("/accounts/{unique_id}/friends/refresh")
    async def refresh_account_friend_list(request: Request, unique_id: str):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"error": "Invalid CSRF token"}, status_code=403)

        accounts = get_userData(force_reload=True)
        account = find_account(accounts, unique_id)
        if not account:
            return JSONResponse({"error": "Account not found."}, status_code=404)

        try:
            friends = await fetch_account_friends(account)
            account["friends_cache"] = friends
            account["friends_cache_updated_at"] = datetime.now().isoformat(timespec="seconds")
            save_userData(accounts)
            return JSONResponse(
                {
                    "friends": friends,
                    "updated_at": account["friends_cache_updated_at"],
                    "message": f"已刷新 {len(friends)} 个好友",
                }
            )
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/accounts/{unique_id}/delete")
    async def delete_account(request: Request, unique_id: str):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        accounts = get_userData(force_reload=True)
        updated_accounts = [item for item in accounts if normalize_unique_id(item.get("unique_id")) != normalize_unique_id(unique_id)]
        if len(updated_accounts) != len(accounts):
            save_userData(updated_accounts)
            flash(request, "账号已删除。", "success")
        else:
            flash(request, "未找到账号。", "error")
        return redirect("/accounts")

    @app.post("/accounts/{unique_id}/retry-target")
    async def retry_account_target(request: Request, unique_id: str):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        target_name = str(form.get("target", "")).strip()
        if not target_name:
            flash(request, "请选择需要重试的目标。", "error")
            return redirect("/ops/send-console")

        accounts = get_userData(force_reload=True)
        account = find_account(accounts, unique_id)
        if not account:
            flash(request, "未找到账号。", "error")
            return redirect("/ops/send-console")

        account_copy = dict(account)
        account_copy["targets"] = [target_name]
        config = get_config(force_reload=True)
        config["taskCount"] = 1

        try:
            await run_browser_tasks(config, [account_copy])
        except Exception as exc:
            flash(request, f"{account.get('username', '账号')} / {target_name} 重试失败：{exc}", "error")
            return redirect("/ops/send-console")

        updated_account = find_account(get_userData(force_reload=True), unique_id) or {}
        if _target_sent_today(updated_account, target_name):
            flash(request, f"{account.get('username', '账号')} / {target_name} 已重试成功。", "success")
        else:
            failure_entry = dict(updated_account.get("failure_queue") or {}).get(target_name) or {}
            reason = str(failure_entry.get("reason") or "重试未确认发送成功。")
            flash(request, f"{account.get('username', '账号')} / {target_name} 重试未成功：{reason}", "error")
        return redirect("/ops/send-console")

    @app.post("/config")
    async def save_runtime_config(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        config = get_config(force_reload=True)
        if "messageTemplate" in form:
            config["messageTemplate"] = str(form.get("messageTemplate", config.get("messageTemplate", "")))
        if "multiTask" in form:
            config["multiTask"] = str(form.get("multiTask", "")) == "on"
        if "taskCount" in form:
            config["taskCount"] = coerce_int(form.get("taskCount", config.get("taskCount", 1)), config.get("taskCount", 1), 1)
        if "hitokotoTypes" in form:
            raw_types = str(form.get("hitokotoTypes", ""))
            config["hitokotoTypes"] = [item.strip() for item in raw_types.replace(",", "\n").splitlines() if item.strip()]

        send_strategy = config.get("sendStrategy", {}) or {}
        if "shuffleTargets" in form:
            send_strategy["shuffleTargets"] = str(form.get("shuffleTargets", "")) == "on"
        if "accountStartDelaySecondsMin" in form:
            send_strategy["accountStartDelaySecondsMin"] = coerce_int(
                form.get("accountStartDelaySecondsMin", send_strategy.get("accountStartDelaySecondsMin", 0)),
                send_strategy.get("accountStartDelaySecondsMin", 0),
                0,
            )
        if "accountStartDelaySecondsMax" in form:
            send_strategy["accountStartDelaySecondsMax"] = coerce_int(
                form.get("accountStartDelaySecondsMax", send_strategy.get("accountStartDelaySecondsMax", 0)),
                send_strategy.get("accountStartDelaySecondsMax", 0),
                send_strategy.get("accountStartDelaySecondsMin", 0),
            )
        if "messageIntervalSecondsMin" in form:
            send_strategy["messageIntervalSecondsMin"] = coerce_int(
                form.get("messageIntervalSecondsMin", send_strategy.get("messageIntervalSecondsMin", 0)),
                send_strategy.get("messageIntervalSecondsMin", 0),
                0,
            )
        if "messageIntervalSecondsMax" in form:
            send_strategy["messageIntervalSecondsMax"] = coerce_int(
                form.get("messageIntervalSecondsMax", send_strategy.get("messageIntervalSecondsMax", 0)),
                send_strategy.get("messageIntervalSecondsMax", 0),
                send_strategy.get("messageIntervalSecondsMin", 0),
            )
        if "messageVariants" in form:
            raw_variants = str(form.get("messageVariants", ""))
            send_strategy["messageVariants"] = [
                item.strip() for item in raw_variants.replace("\r", "\n").split("\n") if item.strip()
            ]
        config["sendStrategy"] = send_strategy

        happy_new_year = config.get("happyNewYear", {})
        if "happyNewYearEnabled" in form:
            happy_new_year["enabled"] = str(form.get("happyNewYearEnabled", "")) == "on"
        if "happyNewYearTemplate" in form:
            happy_new_year["messageTemplate"] = str(form.get("happyNewYearTemplate", happy_new_year.get("messageTemplate", "")))
        config["happyNewYear"] = happy_new_year
        save_config(config)

        flash(request, "运行配置已保存。", "success")
        return redirect("/settings")

    @app.post("/settings")
    async def save_panel_settings(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        settings = get_app_settings(force_reload=True)
        settings["server_host"] = str(form.get("server_host", "")).strip()
        settings["server_username"] = str(form.get("server_username", "")).strip()
        settings["server_password"] = str(form.get("server_password", "")).strip()
        settings["compose_root"] = str(form.get("compose_root", settings.get("compose_root", ""))).strip()
        settings["ops_log_file"] = str(form.get("ops_log_file", settings.get("ops_log_file", ""))).strip()
        settings["proxy_refresh_script"] = str(form.get("proxy_refresh_script", settings.get("proxy_refresh_script", ""))).strip()
        settings["local_login_helper_url"] = str(
            form.get("local_login_helper_url", settings.get("local_login_helper_url", "http://127.0.0.1:18765"))
        ).strip()
        settings["login_desktop_api_url"] = str(
            form.get("login_desktop_api_url", settings.get("login_desktop_api_url", "http://127.0.0.1:18090"))
        ).strip()
        settings["ui_port"] = int(form.get("ui_port", settings.get("ui_port", 8787)))
        save_app_settings(settings)

        new_password = str(form.get("new_password", ""))
        confirm_password = str(form.get("confirm_password", ""))
        if new_password:
            if new_password != confirm_password:
                flash(request, "管理员密码未更新：两次输入不一致。", "error")
                return redirect("/settings")
            update_admin_password(new_password)

        flash(request, "面板与服务设置已保存。", "success")
        return redirect("/settings")

    @app.post("/ops/run-now")
    async def run_now(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        pid = run_task_now()
        if pid == -1:
            flash(request, "补发全部对象启动失败，请查看服务日志。", "error")
        else:
            flash(request, f"已启动补发全部对象任务（pid {pid}）。", "success")
        return redirect("/ops/send-console")

    @app.post("/ops/run-unsent")
    async def run_unsent(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        pid = run_unsent_retry_now()
        if pid == -1:
            flash(request, "补发未成功目标启动失败，请查看服务日志。", "error")
        else:
            flash(request, f"已启动补发未成功目标任务（pid {pid}）。", "success")
        return redirect("/ops/send-console")

    @app.post("/ops/proxy/refresh")
    async def proxy_refresh(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        refresh_proxy()
        flash(request, "代理订阅已刷新。", "success")
        return redirect("/settings")

    @app.post("/ops/proxy/restart")
    async def proxy_restart(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        restart_proxy()
        flash(request, "代理容器已重启。", "success")
        return redirect("/settings")

    @app.post("/ops/schedule")
    async def save_schedule(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        time_string = str(form.get("daily_schedule", "")).strip()
        result = update_daily_schedule(time_string)
        if getattr(result, "returncode", 1) == 0:
            flash(request, f"发送窗口已更新为 {time_string}。", "success")
        else:
            flash(request, f"发送窗口更新失败：{getattr(result, 'stderr', '')}", "error")
        return redirect("/settings")

    @app.get("/ops/logs", response_class=HTMLResponse)
    async def logs_page(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect
        return render_template(
            request,
            "logs.html",
            {
                "flash": pop_flash(request),
                "log_tail": read_log_tail(400),
            },
        )

    @app.get("/login-desktop/status")
    async def login_desktop_status(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        try:
            payload = call_login_desktop("/status")
            payload["public_url"] = login_desktop_public_url(request)
            return JSONResponse(payload)
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc), "public_url": login_desktop_public_url(request)}, status_code=503)

    @app.post("/login-desktop/open")
    async def login_desktop_open(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)
        try:
            call_login_desktop("/open-login", method="POST", payload={})
            return JSONResponse({"ok": True, "public_url": login_desktop_public_url(request)})
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)

    @app.post("/login-desktop/reset")
    async def login_desktop_reset(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)
        try:
            payload = call_login_desktop("/reset", method="POST", payload={}, timeout=120)
            return JSONResponse({"ok": True, "result": payload})
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)

    @app.post("/login-desktop/save")
    async def login_desktop_save(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)

        relogin_unique_id = str(form.get("relogin_unique_id", "")).strip()
        display_name = str(form.get("display_name", "")).strip()
        try:
            payload = call_login_desktop("/export", method="POST", payload={}, timeout=30)
            if not payload.get("ok"):
                raise RuntimeError("login-desktop export did not return ok")
            account, action = save_exported_login_result(
                payload.get("result", {}),
                relogin_unique_id=relogin_unique_id,
                display_name=display_name,
            )
            return JSONResponse({
                "ok": True,
                "action": action,
                "account": {
                    "unique_id": account.get("unique_id"),
                    "username": account.get("username"),
                    "enabled": account.get("enabled", True),
                },
            })
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    return app


app = create_app()


def run_web_app(host=None, port=None):
    settings = get_app_settings(force_reload=True)
    uvicorn.run(
        "webui.app:app",
        host=host or settings["ui_host"],
        port=port or settings["ui_port"],
        reload=False,
    )
