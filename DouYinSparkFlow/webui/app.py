import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl, urlencode
from contextlib import asynccontextmanager

import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from core import identity as account_identity
from core import streak_state
from core.cookies import (
    CookieParseError,
    cookie_summary,
    parse_cookie_input,
    require_auth_cookies,
)
from core.groups import fetch_account_groups
from core.friends import (
    CATEGORY_EMPTY_RESULT,
    CATEGORY_INCOMPLETE_SCAN,
    CATEGORY_LABELS,
    CATEGORY_LOGIN_REQUIRED,
    CATEGORY_NETWORK_UNAVAILABLE,
    CATEGORY_STRUCTURE_CHANGED,
    FriendRefreshError,
    fetch_account_friends,
    verify_account_session,
)
from core import friend_identity
from core import patrol as account_patrol
from core.send_state import history_entry_is_strong_confirmed_today, parse_sent_at
from core.protocol_dispatch import run_protocol_tasks
from core.tasks import (
    _normalize_target_name,
    _append_streak_run_report,
    parse_chat_target,
    target_match_key,
    record_friend_scan,
    run_browser_tasks,
    task_run_lock,
)
from utils.config import (
    get_app_settings,
    get_config,
    get_userData,
    normalize_unique_id,
    save_app_settings,
    update_config,
    merge_user_account_into,
    update_user_data,
)
from utils import notify
from utils.logger import setup_logger
from webui.auth import (
    bootstrap_admin_password,
    clear_session,
    csrf_token,
    current_user,
    current_principal,
    is_bootstrapped,
    is_https_request,
    issue_session,
    update_admin_password,
    validate_csrf,
)
from webui.users import (
    UserStoreError,
    account_by_ref,
    account_by_unique_id,
    account_owner_username,
    can_access_account,
    create_web_user,
    delete_web_user,
    ensure_account_refs,
    get_visible_accounts,
    get_web_users,
    remove_account_refs_from_users,
    update_web_user,
)
from webui.login_guard import LoginThrottle, password_problem
from webui.login_lock import (
    begin_expiration as begin_login_expiration,
    begin_force_reset as begin_login_force_reset,
    begin_release as begin_login_release,
    cancel_request as cancel_login_request,
    finish_transition as finish_login_transition,
    get_lock as get_login_lock,
    get_workspace_state,
    heartbeat as heartbeat_login,
    owns as owns_login_lock,
    request_workspace,
    workspace_status,
)
from webui.ops import (
    TASK_ALREADY_RUNNING,
    get_overview_snapshot,
    get_ops_snapshot,
    cache_age_label,
    duplicate_display_names,
    identity_view,
    log_file_path,
    read_log_tail,
    summarize_log_tail,
    refresh_proxy,
    restart_proxy,
    run_failed_retry_now,
    run_task_now,
    run_unsent_retry_now,
    task_run_lock_status,
    preview_daily_schedule,
    schedule_window_state,
    sync_daily_schedule_from_config,
    update_daily_schedule,
)

# Route web-side warnings into the same logs/app.log the task runner writes, at
# the level it already uses, so a failed refresh is no longer silent.
logger = setup_logger(level=logging.DEBUG)


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DEBUG_ARTIFACTS_DIR = BASE_DIR.parent / "logs" / "debug_artifacts"
# Seconds one /login-desktop/qr request waits upstream for a QR code; the upstream
# adds at most ~3s on top, well inside the 20s proxy timeout.
LOGIN_QR_WAIT_SECONDS = 10
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["cache_age_label"] = cache_age_label
templates.env.globals["duplicate_display_names"] = duplicate_display_names
templates.env.globals["schedule_window_state"] = schedule_window_state
templates.env.globals["identity_view"] = identity_view
templates.env.globals["patrol_view"] = account_patrol.patrol_view


def _dedupe_targets(values):
    seen = set()
    result = []
    for value in values:
        normalized = str(value).strip()
        dedupe_key = (
            target_match_key(normalized)
            if normalized.startswith(("群聊：", "群聊:"))
            else ("friend", normalized)
        )
        if not normalized or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
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


def extract_group_targets_from_form(form):
    """Return manual group targets in the canonical, separate namespace."""
    values = form.getlist("group_targets") if hasattr(form, "getlist") else [form.get("group_targets", "")]
    normalized = []
    for value in _split_target_entries(values):
        name = value.removeprefix("群聊：").removeprefix("群聊:").strip()
        if name:
            normalized.append(f"群聊：{name}")
    return _dedupe_targets(normalized)


from webui.app_helpers import (
    _history_entry_strong_confirmed_today,
    _parse_sent_at,
    _schedule_timezone,
    _target_sent_today,
    _target_unconfirmed_today,
    coerce_int,
    find_account,
    is_account_enabled,
    manual_retry_config,
    mark_target_unconfirmed,
)  # noqa: F401


def manual_retry_channel(config):
    """Sender a manual retry must use, mirroring ``_split_sender_modes``.

    A retry that always drove the browser would contradict a protocol-primary
    deployment and record evidence the operator cannot trust, so the channel is
    derived from the same switch the scheduled run uses.
    """
    return "protocol" if config.get("useProtocolSender", True) else "browser"


def login_desktop_api_url():
    settings = get_app_settings(force_reload=True)
    configured = os.getenv("SPARKFLOW_LOGIN_DESKTOP_API_URL") or settings.get("login_desktop_api_url")
    return str(configured or "http://127.0.0.1:18090").rstrip("/")


def login_desktop_display_mode() -> str:
    settings = get_app_settings(force_reload=True)
    configured = os.getenv("SPARKFLOW_LOGIN_DESKTOP_MODE") or settings.get("login_desktop_mode")
    mode = str(configured or ("native" if os.name == "nt" else "novnc")).strip().lower()
    if mode == "native" and os.name != "nt":
        return "novnc"
    return mode if mode in {"native", "novnc"} else "novnc"


def login_desktop_public_url(request: Request) -> str:
    settings = get_app_settings(force_reload=True)
    configured_url = str(
        os.getenv("SPARKFLOW_LOGIN_DESKTOP_PUBLIC_URL")
        or settings.get("login_desktop_public_url")
        or ""
    ).strip()
    if configured_url:
        return configured_url
    if login_desktop_display_mode() == "native":
        return ""

    return (
        "/login-desktop/proxy/vnc.html"
        "?autoconnect=1&resize=scale&view_only=0"
        "&path=login-desktop/proxy/websockify"
    )


def login_desktop_novnc_http_url() -> str:
    return str(os.getenv("SPARKFLOW_LOGIN_DESKTOP_NOVNC_URL") or "http://login-desktop:6080").rstrip("/")


def login_desktop_novnc_ws_url() -> str:
    return str(os.getenv("SPARKFLOW_LOGIN_DESKTOP_NOVNC_WS_URL") or "ws://login-desktop:6080/websockify")


def login_desktop_api_headers():
    token = str(os.getenv("SPARKFLOW_LOGIN_DESKTOP_API_TOKEN") or "").strip()
    if not token:
        token_file = str(
            os.getenv("SPARKFLOW_LOGIN_DESKTOP_API_TOKEN_FILE") or ""
        ).strip()
        if token_file:
            try:
                token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def fetch_login_desktop_asset(asset_path: str, query: str = ""):
    safe_path = quote(str(asset_path or "vnc.html").lstrip("/"), safe="/._-")
    url = f"{login_desktop_novnc_http_url()}/{safe_path}"
    if query:
        url = f"{url}?{query}"
    upstream_request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(upstream_request, timeout=20) as upstream:
            headers = {
                key: value
                for key, value in upstream.headers.items()
                if key.lower() in {"content-type", "content-encoding", "cache-control", "etag", "last-modified"}
            }
            return upstream.status, headers, upstream.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"login-desktop noVNC proxy failed: {exc}") from exc


def call_login_desktop_json(path: str, *, method: str = "GET", payload: dict | None = None, timeout: int = 20):
    """Call the login-desktop API and keep the upstream status and body."""
    url = f"{login_desktop_api_url()}{path}"
    data = None
    headers = login_desktop_api_headers()
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"login-desktop unavailable: {reason}") from exc
    try:
        parsed = json.loads(body) if body.strip() else {}
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return status, parsed


def call_login_desktop(path: str, *, method: str = "GET", payload: dict | None = None, timeout: int = 20) -> dict:
    url = f"{login_desktop_api_url()}{path}"
    data = None
    headers = login_desktop_api_headers()
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
        message = body
        try:
            payload = json.loads(body)
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(detail, dict):
                message = str(detail.get("message") or detail.get("code") or body)
            elif detail:
                message = str(detail)
        except (TypeError, ValueError):
            pass
        raise RuntimeError(f"login-desktop API error {exc.code}: {message}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"login-desktop unavailable: {reason}") from exc


async def _run_websocket_relays(*coroutines):
    tasks = {asyncio.create_task(coroutine) for coroutine in coroutines}
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, (ConnectionClosed, WebSocketDisconnect, asyncio.CancelledError)):
                continue
            if isinstance(result, BaseException):
                raise result
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


_FRIEND_REFRESH_TIMEOUT_SECONDS = 300
# Background login verification (D12): poll for a free browser, give up
# waiting after two hours, bound one check, and wait a little after a restart.
LOGIN_VERIFICATION_IDLE_POLL_SECONDS = 15
LOGIN_VERIFICATION_MAX_WAIT_SECONDS = 2 * 3600
LOGIN_VERIFICATION_TIMEOUT_SECONDS = 300
LOGIN_VERIFICATION_RESTART_DELAY_SECONDS = 30
_friend_refresh_active = set()
_group_refresh_active = set()
# Accounts with a Web-triggered patrol in flight (one uvicorn process).
_patrol_active = set()
_background_tasks = set()


def _start_background(coro):
    """Run a Web-triggered job in the background of the serving event loop."""
    task = asyncio.create_task(coro)
    # Keep a reference so the loop does not drop the task before it finishes.
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def friend_refresh_conflict(unique_id):
    """Return a conflict reason when this account already has a refresh running."""
    if normalize_unique_id(unique_id) in _friend_refresh_active:
        return "该账号已有好友刷新正在进行，请稍后重试"
    if normalize_unique_id(unique_id) in _patrol_active:
        return "该账号正在巡查，请稍后重试"
    if normalize_unique_id(unique_id) in _group_refresh_active:
        return "该账号正在刷新群列表，请稍后重试"
    active = get_login_lock()
    if active:
        return "登录工作区正在被使用，请等扫码登录结束后再刷新好友"
    return ""


def _incomplete_scan_message(reason):
    """Explain a read that stopped before the end of the friend list."""
    detail = f"（{reason}）" if reason else ""
    return f"本次读取不完整，已保留上一次的好友数据；请稍后重试。{detail}"


def _incomplete_scan_fields(reason):
    """Shared result fields for any read that stopped before the list ended.

    Both the synchronous route and the background job must classify a stalled
    scan and a read that ran past its budget the same way.
    """
    return {
        "error": _incomplete_scan_message(reason),
        "category": CATEGORY_INCOMPLETE_SCAN,
        "categoryLabel": CATEGORY_LABELS[CATEGORY_INCOMPLETE_SCAN],
        "retryable": True,
    }


# A save never removes or merges other account rows (issue #41 A3/A4): only an
# explicit user delete or merge may do that. Health clearing is shared with the
# CLI save through core.identity so both paths lift exactly the same flags.
HEALTH_CLEARING_KEYS = account_identity.LOGIN_HEALTH_FLAG_KEYS
LOGIN_RELATED_FAILURE_CATEGORIES = account_identity.LOGIN_RELATED_FAILURE_CATEGORIES


def _clear_login_health(account):
    account_identity.clear_login_health(account)


def write_login_verification(
    account_ref,
    token,
    outcome,
    *,
    category="",
    reason="",
    expected_uid="",
    actual_uid="",
    generation="",
):
    """Record a background login check on the account it was started for.

    outcome is "verified", "login_required", "identity_conflict" or
    "unverified". The token (and, for a new save, the login generation) ties the
    result to the save that queued it, so a stale check never touches a newer
    login. Records written before generations existed only carry the token and
    are still accepted on the token alone.
    """
    def mutate(accounts):
        target = account_by_ref(accounts, account_ref)
        current = dict((target or {}).get("login_verification") or {})
        if not target or not token or current.get("token") != token:
            # Deleted meanwhile, or a newer login replaced this one.
            return None, False
        if generation:
            # A CLI save (or any newer login) that kept the same token but moved
            # to a new generation must not be judged by this stale result.
            if account_identity.account_login_generation(target) != generation:
                return None, False
        finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if outcome == "verified":
            # Only lift this check's own marker. The save already cleared the old
            # health flags; anything written since (an identity mismatch, a real
            # send failure today) is newer and must stay.
            target.pop("login_verification", None)
        elif outcome == "identity_conflict":
            if expected_uid and account_identity.account_uid(target) != expected_uid:
                # The binding changed since this check started; not ours to judge.
                return None, False
            target["identity_mismatch"] = {
                "kind": "platform_uid",
                "expected": expected_uid,
                "actual": actual_uid,
                "detectedAt": finished_at,
                "reason": "saved login belongs to a different platform UID",
            }
            target["login_verification"] = {
                "state": "identity_conflict",
                "token": token,
                "finishedAt": finished_at,
                "category": account_identity.CATEGORY_IDENTITY_CONFLICT,
                "reason": reason,
            }
        elif outcome == "login_required":
            target["login_required"] = True
            health = dict(target.get("account_health") or {})
            health.update(
                {
                    "healthy": False,
                    "category": category or CATEGORY_LOGIN_REQUIRED,
                    "reason": reason or "登录态已失效，需要重新登录",
                }
            )
            target["account_health"] = health
            target["login_verification"] = {
                "state": "login_required",
                "token": token,
                "finishedAt": finished_at,
                "reason": reason,
            }
        else:
            # Not confirmed either way (network, page or identity endpoint):
            # shown on the account, but sending is not paused for it.
            target["login_verification"] = {
                "state": "unverified",
                "token": token,
                "finishedAt": finished_at,
                "category": category,
                "reason": reason,
            }
        return dict(target), True

    return update_user_data(mutate, force_reload=True)



async def check_saved_login_identity(account):
    """After a usable login, check it still belongs to the record's bound UID.

    Returns ``(outcome, category, reason, expected_uid, actual_uid)``. A usable
    login is not proof of the right account; an unreadable identity is shown
    but does not pause sending; a legacy record (no bound UID) is not judged.
    """
    expected_uid = account_identity.account_uid(account)
    if not expected_uid:
        return "verified", "", "", "", ""
    try:
        trusted = await asyncio.to_thread(
            account_identity.fetch_trusted_identity,
            list(account.get("cookies") or []),
        )
    except account_identity.IdentityUnavailable as exc:
        return "unverified", account_identity.CATEGORY_IDENTITY_UNVERIFIED, str(exc), expected_uid, ""
    if trusted["uid"] != expected_uid:
        return (
            "identity_conflict",
            account_identity.CATEGORY_IDENTITY_CONFLICT,
            "保存的登录属于另一个平台 UID",
            expected_uid,
            trusted["uid"],
        )
    return "verified", "", "", expected_uid, trusted["uid"]

def find_same_name_account(accounts, username):
    """Accounts sharing a nickname, used to ask before adding a duplicate.

    Douyin nicknames are not unique, so this only narrows the candidates down;
    the operator still decides whether to update the existing row or create a
    separate account.
    """
    wanted = str(username or "").strip()
    if not wanted:
        return []
    return [
        item
        for item in accounts or []
        if str((item or {}).get("username") or "").strip() == wanted
    ]


def unique_id_owned_by_other(accounts, unique_id, current):
    """True when another Web user owns any row for this stored key.

    Duplicate rows for one ``unique_id`` are legacy data. The save path updates
    one row and then dedupes the rest, so the permission check has to look at
    the whole set: claiming an unassigned duplicate while another row for the
    same Douyin account belongs to someone else would strip their ownership.
    """
    normalized = normalize_unique_id(unique_id)
    for row in accounts or []:
        if normalize_unique_id(row.get("unique_id")) != normalized:
            continue
        if can_access_account(current, row):
            continue
        if account_owner_username(row.get("account_ref", "")):
            return True
    return False


def preferred_account_for_unique_id(accounts, unique_id, current):
    """Pick the row a save should update for one stored key.

    Duplicate rows for a single ``unique_id`` are legacy data. Prefer a row the
    caller may access so a save updates the user's own row instead of an
    unassigned duplicate that happens to sort first and would otherwise be
    deduped into the user's row.
    """
    normalized = normalize_unique_id(unique_id)
    fallback = None
    for row in accounts or []:
        if normalize_unique_id(row.get("unique_id")) != normalized:
            continue
        if can_access_account(current, row):
            return row
        if fallback is None:
            fallback = row
    return fallback


def rows_for_identity(accounts, uid, display_id=""):
    """Rows a login with this trusted UID may belong to.

    Rows bound to the UID are the answer when they exist. Otherwise legacy
    rows (no bound UID) whose stored key equals the UID or the ID the page
    displayed are candidates; they still need proof or a confirmation before
    the login is written to them.
    """
    bound = account_identity.accounts_bound_to(accounts, uid)
    if bound:
        return bound, "bound"
    # Old saves stored the digits-only form of whatever the page showed, so a
    # handle with letters ("abc123") lives under "123"; match both forms.
    shown = str(display_id or "").strip()
    keys = {str(uid or "").strip(), shown, normalize_unique_id(shown)} - {""}
    legacy = []
    for row in accounts or []:
        if account_identity.account_uid(row):
            continue
        stored = str(row.get("unique_id") or "").strip()
        if stored in keys or (stored and normalize_unique_id(stored) == uid and stored.isdigit()):
            legacy.append(row)
    return legacy, "legacy"


def identity_owned_by_other(accounts, uid, current, display_id=""):
    """True when a row this login maps to belongs to another Web user."""
    rows, _kind = rows_for_identity(accounts, uid, display_id)
    for row in rows:
        if can_access_account(current, row):
            continue
        if account_owner_username(row.get("account_ref", "")):
            return True
    return False


def may_claim_account(current, account):
    """The caller's own row, or an unassigned row a proven login may adopt."""
    if can_access_account(current, account):
        return True
    return not account_owner_username(account.get("account_ref", ""))


def preferred_row(rows, current):
    fallback = None
    for row in rows or []:
        if can_access_account(current, row):
            return row
        if fallback is None:
            fallback = row
    return fallback


class IdentityConflict(RuntimeError):
    """The login's trusted UID differs from the account it would update."""

    def __init__(self, message, *, account_ref="", expected_uid="", actual_uid=""):
        super().__init__(message)
        self.account_ref = account_ref
        self.expected_uid = expected_uid
        self.actual_uid = actual_uid


class NeedsIdentityConfirmation(RuntimeError):
    """A legacy record cannot be checked automatically; ask the operator once."""

    def __init__(self, message, *, account, fingerprint, uid, candidate_fingerprint=""):
        super().__init__(message)
        self.account_ref = str(account.get("account_ref", ""))
        self.username = str(account.get("username", ""))
        self.history_id = str(account.get("unique_id", ""))
        self.fingerprint = fingerprint
        self.uid = uid
        # Digest of the whole login candidate (UID + display/nickname + every
        # cookie with its metadata). Server-side only; never sent to the client.
        self.candidate_fingerprint = candidate_fingerprint


class LoginSavePermissionDenied(RuntimeError):
    """The live session or account assignment changed before saving."""


# Raised by the shared identity helpers too, so there is exactly one class.
AccountChangedConcurrently = account_identity.AccountChangedConcurrently


IDENTITY_SAVE_ERRORS = (
    IdentityConflict,
    NeedsIdentityConfirmation,
    AccountChangedConcurrently,
    account_identity.IdentityUnavailable,
)
IDENTITY_CONFIRMATION_TTL_SECONDS = 600


def identity_category_label(category):
    if category == account_identity.CATEGORY_IDENTITY_UNVERIFIED:
        return account_identity.IDENTITY_STATE_LABELS["unverified"]
    return CATEGORY_LABELS.get(category, CATEGORY_LABELS[CATEGORY_STRUCTURE_CHANGED])


# One-time migration confirmations live only in this process: they carry the
# record fingerprint and the candidate UID, never cookies or tokens, and the
# client only ever sees an opaque random key.
_identity_confirmations: dict[str, dict] = {}


def _issue_identity_confirmation(current, exc, route):
    now = time.monotonic()
    for key in [key for key, item in _identity_confirmations.items() if item["expires"] <= now]:
        _identity_confirmations.pop(key, None)
    token = secrets.token_urlsafe(24)
    _identity_confirmations[token] = {
        "username": str(current.get("username", "")),
        "session_id": str(current.get("session_id", "")),
        "route": route,
        "account_ref": exc.account_ref,
        "fingerprint": exc.fingerprint,
        "uid": exc.uid,
        "candidate_fingerprint": exc.candidate_fingerprint,
        "expires": now + IDENTITY_CONFIRMATION_TTL_SECONDS,
    }
    return token


def _take_identity_confirmation(token, current, route):
    """Consume a confirmation once; returns (record, problem)."""
    record = _identity_confirmations.pop(str(token or ""), None)
    if not record:
        return None, "确认已失效或已使用，请重新保存登录"
    if record["expires"] <= time.monotonic():
        return None, "确认已过期，请重新保存登录"
    if (
        record["username"] != str(current.get("username", ""))
        or record["session_id"] != str(current.get("session_id", ""))
        or record["route"] != route
    ):
        return None, "确认不属于当前会话，请重新保存登录"
    return record, ""


def _identity_error_response(exc, current, route, *, display_id=""):
    if isinstance(exc, NeedsIdentityConfirmation):
        token = _issue_identity_confirmation(current, exc, route)
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
                "category": "identity_confirmation_required",
                "identityState": "pending_confirmation",
                "identityLabel": account_identity.IDENTITY_STATE_LABELS["pending_confirmation"],
                "identity_confirmation": {
                    "token": token,
                    "account_ref": exc.account_ref,
                    "username": exc.username,
                    "history_id": exc.history_id,
                    "history_id_type": "类型未知（可能是抖音号，也可能是平台 UID）",
                    "platform_uid": exc.uid,
                    "display_id": display_id,
                    "expires_in": IDENTITY_CONFIRMATION_TTL_SECONDS,
                },
            },
            status_code=409,
        )
    if isinstance(exc, IdentityConflict):
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
                "category": account_identity.CATEGORY_IDENTITY_CONFLICT,
                "identityState": "conflict",
                "identityLabel": account_identity.IDENTITY_STATE_LABELS["conflict"],
                "hint": account_identity.IDENTITY_STATE_HINTS["conflict"],
                "retryable": False,
            },
            status_code=409,
        )
    if isinstance(exc, AccountChangedConcurrently):
        return JSONResponse(
            {"ok": False, "error": str(exc), "category": "account_changed", "retryable": True},
            status_code=409,
        )
    return JSONResponse(
        {
            "ok": False,
            "error": str(exc),
            "category": account_identity.CATEGORY_IDENTITY_UNVERIFIED,
            "categoryLabel": account_identity.IDENTITY_STATE_LABELS["unverified"],
            "identityState": "unverified",
            "retryable": True,
        },
        status_code=503,
    )


def legacy_identity_check(account) -> dict:
    """Read the UID a legacy record's own saved cookies log in as.

    Runs outside the data lock (it is a network call); the fingerprint lets the
    save refuse to use the answer if the record changed meanwhile.
    """
    check = {
        "account_ref": str(account.get("account_ref", "")),
        "fingerprint": account_identity.record_fingerprint(account),
        "uid": "",
        "reason": "",
    }
    if account_identity.account_uid(account):
        return check
    try:
        check["uid"] = account_identity.fetch_trusted_identity(list(account.get("cookies") or []))["uid"]
    except account_identity.IdentityUnavailable as exc:
        check["reason"] = str(exc)
    return check


def save_exported_login_result(
    login_result: dict,
    *,
    relogin_unique_id: str = "",
    relogin_account_ref: str = "",
    display_name: str = "",
    is_healthy: bool | None = True,
    verification_reason: str = "",
    verification_category: str = "",
    verification_token: str = "",
    identity: dict | None = None,
    legacy_check: dict | None = None,
    confirmation: dict | None = None,
    auto_matched: bool = False,
    expected_records: dict | None = None,
    principal_resolver=None,
    expected_principal: dict | None = None,
) -> tuple[dict, str]:
    """Write a login to the account its trusted platform UID belongs to.

    The match is decided before anything is written: the same bound UID
    updates the row in place (keeping its key, targets, history, owner and
    profile); a different bound UID is refused; a legacy row is bound only on
    proof from its own saved cookies or a confirmed one-time migration. Any
    refusal raises before the data file is written.
    """
    identity = dict(identity or login_result.get("identity") or {})
    uid = str(identity.get("uid") or "")
    if not account_identity.is_valid_uid(uid):
        raise account_identity.IdentityUnavailable("暂时无法核实账号身份，未保存本次登录，请稍后重试")
    username = str(display_name or login_result.get("username") or "").strip()
    cookies = list(login_result.get("cookies") or [])
    display_id = account_identity.display_id_from_text(
        login_result.get("display_id") or ""
    )
    if not cookies:
        raise RuntimeError("Exported login result is incomplete")
    if not username:
        username = display_id or uid
    # The whole candidate this save belongs to: the UID plus every parameter
    # that would change what gets written (nickname, displayed ID and the full
    # cookie list). A one-time confirmation is bound to this digest, so a
    # different candidate cannot reuse an older confirmation. Server-side only.
    candidate = account_identity.candidate_fingerprint(
        uid=uid,
        username=str(login_result.get("username") or ""),
        display_id=display_id,
        display_name=str(display_name or ""),
        cookies=cookies,
    )
    # Every saved login gets a new generation; a background check started for an
    # older generation must not touch this one (issue #41 A7).
    login_generation = account_identity.new_login_generation()

    def verify_snapshot(target, accounts):
        """Refuse when the chosen record changed since the pre-check snapshot.

        The snapshot is read before any network identity check; verifying inside
        the data lock keeps the compare-then-write atomic (issue #41 A8).
        """
        authorize_save(target, accounts)
        if expected_records is None:
            return
        ref = str((target or {}).get("account_ref") or "")
        if ref not in expected_records:
            raise AccountChangedConcurrently("目标账号在核实期间被新建或已删除，请重新保存")
        if account_identity.record_fingerprint(target) != expected_records[ref]:
            raise AccountChangedConcurrently("账号记录在核实期间被修改，请重新保存")

    def apply_health(account):
        """Record what is known about this login's usability on the account."""
        if is_healthy is None:
            # Saved before the login state was re-checked in a second browser:
            # the background verifier decides. Until then the account is not
            # paused, and the token lets a newer login supersede the check.
            _clear_login_health(account)
            account["login_verification"] = {
                "state": "verifying",
                "token": verification_token,
                "generation": login_generation,
                "startedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            return
        account.pop("login_verification", None)
        if is_healthy:
            _clear_login_health(account)
            return
        # A verification that failed for a page or transport reason must not
        # masquerade as a logged-out account: only a real login failure sets
        # login_required, and the recorded category is the one the verifier found.
        # The identity itself is proven, so old identity markers go either way.
        account_identity.clear_identity_markers(account)
        category = str(verification_category or CATEGORY_LOGIN_REQUIRED)
        account["pending_login_verification"] = True
        if category in streak_state.LOGIN_FAILURE_CATEGORIES:
            account["login_required"] = True
        else:
            account.pop("login_required", None)
        existing_health = dict(account.get("account_health") or {})
        existing_health.update(
            {
                "healthy": False,
                "category": category,
                "reason": verification_reason or "login state could not be verified after login",
            }
        )
        account["account_health"] = existing_health

    def resolve_legacy(target, accounts):
        """Return the binding method for an unbound target, or raise."""
        others = [
            row
            for row in account_identity.accounts_bound_to(accounts, uid)
            if row is not target
        ]
        if others:
            raise IdentityConflict(
                "这个平台 UID 已绑定到另一条账号记录，不能再写入这条记录",
                account_ref=str(target.get("account_ref", "")),
                actual_uid=uid,
            )
        fingerprint = account_identity.record_fingerprint(target)
        ref = str(target.get("account_ref", ""))
        for evidence in (legacy_check, confirmation):
            if evidence and evidence.get("account_ref") == ref and evidence.get("fingerprint") != fingerprint:
                raise AccountChangedConcurrently("账号记录在核实期间被修改，请重新保存")
        if legacy_check and legacy_check.get("account_ref") == ref and legacy_check.get("uid"):
            if legacy_check["uid"] != uid:
                raise IdentityConflict(
                    "本次登录的平台 UID 与这条账号原来的登录不一致，未覆盖原账号",
                    account_ref=ref,
                    expected_uid=legacy_check["uid"],
                    actual_uid=uid,
                )
            return account_identity.BINDING_LEGACY_COOKIE, ""
        if confirmation:
            if confirmation.get("account_ref") != ref or confirmation.get("uid") != uid:
                raise AccountChangedConcurrently("确认已失效：本次登录或目标账号已变化，请重新保存")
            # The confirmation is bound to the exact candidate that was shown
            # (UID, nickname/display and every cookie); a different candidate
            # that happens to yield the same UID must not reuse it (issue #41 A5).
            if str(confirmation.get("candidate_fingerprint") or "") != candidate:
                raise AccountChangedConcurrently("本次登录的账号凭据与发起确认时不同，请重新保存")
            return account_identity.BINDING_CONFIRMED, str(confirmation.get("confirmed_by") or "")
        raise NeedsIdentityConfirmation(
            "这条旧账号记录无法自动核实身份，请确认本次登录的是原账号",
            account=target,
            fingerprint=fingerprint,
            uid=uid,
            candidate_fingerprint=candidate,
        )

    def authorize_save(target, accounts):
        # Resolve again inside the account-data lock, not from the principal
        # captured before the network await. Check live ownership even when a
        # caller still has an old account_refs list.
        if principal_resolver is None:
            return
        live = principal_resolver()
        previous = expected_principal or {}
        if not live or any(
            live.get(key) != previous.get(key)
            for key in ("username", "role", "session_id")
        ):
            raise LoginSavePermissionDenied("登录会话在核实期间已变化，请重新登录")
        if live.get("role") == "admin":
            return
        candidates = account_identity.accounts_bound_to(accounts, uid)
        if target is not None:
            candidates = candidates + [target]
        for row in candidates:
            owner = account_owner_username(row.get("account_ref", ""))
            if owner and owner != live.get("username"):
                raise LoginSavePermissionDenied("账号归属在核实期间已变化，无权保存该账号")

    def update_row(target, accounts, method, confirmed_by=""):
        authorize_save(target, accounts)
        target["username"] = username
        target["cookies"] = cookies
        if display_id:
            target[account_identity.FIELD_DISPLAY_ID] = display_id
        target.setdefault("enabled", True)
        target[account_identity.FIELD_LOGIN_GENERATION] = login_generation
        if method:
            account_identity.bind_identity(target, identity, method, confirmed_by=confirmed_by)
        # The identity is proven (same UID, own-cookie match or confirmation),
        # so markers from the old mixed-ID comparison no longer apply.
        account_identity.clear_identity_markers(target)
        apply_health(target)
        # The save never removes, merges or re-keys any other row: only an
        # explicit user delete or merge does that (issue #41 A3/A4).
        return dict(target)

    def create_row(accounts):
        account_identity.check_expected_records(accounts, expected_records)
        authorize_save(None, accounts)
        for row in accounts:
            # A new record may not reuse a stored key or a platform UID that
            # another record already occupies, even one bound to a different
            # UID: that would be a cross-identity key collision (issue #41 A4).
            if str(row.get("unique_id") or "").strip() == uid:
                raise IdentityConflict(
                    "已有一条以该 ID 保存的旧记录，请从那条账号重新登录以完成核实",
                    actual_uid=uid,
                )
            if account_identity.account_uid(row) == uid:
                raise IdentityConflict(
                    "这个平台 UID 已绑定到另一条账号记录，请先在账号管理里处理",
                    actual_uid=uid,
                )
        account = {
            "account_ref": f"acc-{uuid.uuid4().hex}",
            "unique_id": uid,
            "username": username,
            "cookies": cookies,
            "targets": [],
            "enabled": True,
        }
        if display_id:
            account[account_identity.FIELD_DISPLAY_ID] = display_id
        account_identity.bind_identity(account, identity, account_identity.BINDING_NEW)
        account[account_identity.FIELD_LOGIN_GENERATION] = login_generation
        apply_health(account)
        accounts.append(account)
        return dict(account)

    def mutate(accounts):
        for item in accounts:
            if not str(item.get("account_ref", "")).strip():
                item["account_ref"] = f"acc-{uuid.uuid4().hex}"

        if relogin_account_ref or relogin_unique_id:
            target = (
                account_by_ref(accounts, relogin_account_ref)
                if relogin_account_ref
                else find_account(accounts, relogin_unique_id)
            )
            if not target:
                if expected_records is not None:
                    raise AccountChangedConcurrently("目标账号在核实期间被删除，请重新保存")
                raise RuntimeError("Target account not found for relogin")
            # The operator-chosen record must be the one the pre-check saw.
            verify_snapshot(target, accounts)
            bound_uid = account_identity.account_uid(target)
            if bound_uid:
                if bound_uid != uid:
                    raise IdentityConflict(
                        "本次登录的是另一个抖音账号（平台 UID 不同），未覆盖原账号",
                        account_ref=str(target.get("account_ref", "")),
                        expected_uid=bound_uid,
                        actual_uid=uid,
                    )
                return update_row(target, accounts, ""), "updated", True
            try:
                method, confirmed_by = resolve_legacy(target, accounts)
            except IdentityConflict:
                if not auto_matched or not legacy_check or not legacy_check.get("uid"):
                    raise
                # The row was only guessed from a matching ID, and its own
                # cookies prove it is someone else: this is a new account.
                return create_row(accounts), "created", True
            return update_row(target, accounts, method, confirmed_by), "updated", True

        bound = account_identity.accounts_bound_to(accounts, uid)
        if len(bound) > 1:
            raise IdentityConflict("有多条账号记录绑定了同一个平台 UID，请先在账号管理里处理", actual_uid=uid)
        if bound:
            # An auto-matched record is subject to the same snapshot constraint.
            verify_snapshot(bound[0], accounts)
            return update_row(bound[0], accounts, ""), "updated", True
        return create_row(accounts), "created", True

    account, action, _ = update_user_data(mutate, force_reload=True)
    return account, action


async def verify_login_result(login_result, *, relogin_account_ref="", relogin_unique_id=""):
    """Verify a pasted login result: usable login state, then trusted identity.

    Returns ``(verified, reason, identity, category)`` and never raises for a
    plain login-state failure. ``identity`` carries the trusted UID under
    ``identity["trusted"]``; the page-read ID is kept only as ``display_id``.
    """
    cookies = list(login_result.get("cookies") or [])
    identity = {
        "unique_id": "",
        "display_id": "",
        "username": str(login_result.get("username") or "").strip(),
        "trusted": {},
    }
    try:
        require_auth_cookies(cookies)
    except CookieParseError as exc:
        return False, str(exc), identity, getattr(exc, "category", "cookie_format_invalid")

    account = {
        "cookies": cookies,
        "unique_id": "",
        "username": identity["username"],
        "account_ref": relogin_account_ref,
    }
    try:
        result = await verify_account_session(account, auth_only=True)
    except FriendRefreshError as exc:
        return False, str(exc), identity, exc.category or CATEGORY_LOGIN_REQUIRED
    except Exception as exc:  # noqa: BLE001 - report any verification failure
        logger.warning("Login verification failed unexpectedly: %s", exc)
        return False, f"登录态验证失败：{exc}", identity, CATEGORY_LOGIN_REQUIRED

    resolved = dict(result.get("identity") or {})
    identity["display_id"] = account_identity.display_id_from_text(
        resolved.get("display_id") or resolved.get("unique_id") or ""
    )
    identity["username"] = str(resolved.get("username") or identity["username"]).strip()
    try:
        trusted = await asyncio.to_thread(account_identity.fetch_trusted_identity, cookies)
    except account_identity.IdentityUnavailable as exc:
        # The login works but who it belongs to is unknown: retry later, do not
        # create or overwrite an account and do not call it a logout.
        return False, str(exc), identity, account_identity.CATEGORY_IDENTITY_UNVERIFIED
    identity["trusted"] = trusted
    identity["unique_id"] = trusted["uid"]
    return True, "", identity, ""


def public_app_settings():
    settings = get_app_settings(force_reload=True)
    allowed_keys = (
        "compose_root",
        "ui_host",
        "ui_port",
        "ops_log_file",
        "proxy_refresh_script",
        "login_desktop_api_url",
        "login_desktop_public_url",
        "login_desktop_public_scheme",
        "login_desktop_public_port",
    )
    return {key: settings.get(key) for key in allowed_keys}


def apply_runtime_config_form(config, form):
    if "messageTemplate" in form:
        config["messageTemplate"] = str(
            form.get("messageTemplate", config.get("messageTemplate", ""))
        )
    if "multiTask" in form:
        config["multiTask"] = str(form.get("multiTask", "")) == "on"
    if "taskCount" in form:
        config["taskCount"] = coerce_int(
            form.get("taskCount", config.get("taskCount", 1)),
            config.get("taskCount", 1),
            1,
        )
    if "hitokotoTypes" in form:
        raw_types = str(form.get("hitokotoTypes", ""))
        config["hitokotoTypes"] = [
            item.strip()
            for item in raw_types.replace(",", "\n").splitlines()
            if item.strip()
        ]

    send_strategy = config.get("sendStrategy", {}) or {}
    if "shuffleTargets" in form:
        send_strategy["shuffleTargets"] = str(form.get("shuffleTargets", "")) == "on"
    if "accountStartDelaySecondsMin" in form:
        send_strategy["accountStartDelaySecondsMin"] = coerce_int(
            form.get(
                "accountStartDelaySecondsMin",
                send_strategy.get("accountStartDelaySecondsMin", 0),
            ),
            send_strategy.get("accountStartDelaySecondsMin", 0),
            0,
        )
    if "accountStartDelaySecondsMax" in form:
        send_strategy["accountStartDelaySecondsMax"] = coerce_int(
            form.get(
                "accountStartDelaySecondsMax",
                send_strategy.get("accountStartDelaySecondsMax", 0),
            ),
            send_strategy.get("accountStartDelaySecondsMax", 0),
            send_strategy.get("accountStartDelaySecondsMin", 0),
        )
    if "messageIntervalSecondsMin" in form:
        send_strategy["messageIntervalSecondsMin"] = coerce_int(
            form.get(
                "messageIntervalSecondsMin",
                send_strategy.get("messageIntervalSecondsMin", 0),
            ),
            send_strategy.get("messageIntervalSecondsMin", 0),
            0,
        )
    if "messageIntervalSecondsMax" in form:
        send_strategy["messageIntervalSecondsMax"] = coerce_int(
            form.get(
                "messageIntervalSecondsMax",
                send_strategy.get("messageIntervalSecondsMax", 0),
            ),
            send_strategy.get("messageIntervalSecondsMax", 0),
            send_strategy.get("messageIntervalSecondsMin", 0),
        )
    if "messageVariants" in form:
        raw_variants = str(form.get("messageVariants", ""))
        send_strategy["messageVariants"] = [
            item.strip()
            for item in raw_variants.replace("\r", "\n").split("\n")
            if item.strip()
        ]
    config["sendStrategy"] = send_strategy

    happy_new_year = config.get("happyNewYear", {})
    if "happyNewYearEnabled" in form:
        happy_new_year["enabled"] = str(form.get("happyNewYearEnabled", "")) == "on"
    if "happyNewYearTemplate" in form:
        happy_new_year["messageTemplate"] = str(
            form.get(
                "happyNewYearTemplate",
                happy_new_year.get("messageTemplate", ""),
            )
        )
    config["happyNewYear"] = happy_new_year
    return config


def create_app():
    settings = get_app_settings()

    @asynccontextmanager
    async def lifespan(_app):
        # Add stable ownership identifiers without changing existing account data.
        ensure_account_refs()
        result = sync_daily_schedule_from_config()
        if result.returncode != 0:
            logger.warning("Failed to synchronize the configured daily schedule: %s", result.stderr)
        watchdog = asyncio.create_task(login_workspace_watchdog())
        try:
            _requeue_pending_login_verifications()
        except Exception:
            logger.warning("Could not requeue pending login verifications", exc_info=True)
        try:
            yield
        finally:
            watchdog.cancel()
            pending = [watchdog, *login_verification_tasks]
            for task in pending[1:]:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    secure_cookie = str(os.getenv("SPARKFLOW_SESSION_COOKIE_SECURE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    app = FastAPI(title="DouYin Spark Flow Admin", lifespan=lifespan)
    # Per app instance so each test client starts clean; production runs one.
    login_throttle = LoginThrottle()
    app.state.login_throttle = login_throttle
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings["session_secret"],
        max_age=settings["session_max_age_seconds"],
        same_site="lax",
        https_only=secure_cookie,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    DEBUG_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return PlainTextResponse(
            "Internal Server Error",
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )

    def render_template(request, template_name, context=None, status_code=200):
        base_context = dict(context or {})
        base_context.update(
            {
                "request": request,
                "current_user": current_user(request),
                "csrf_token": csrf_token(request) if current_user(request) else "",
                "is_https": is_https_request(request),
                "principal": current_principal(request),
                "is_admin": bool(current_principal(request) and current_principal(request).get("role") == "admin"),
                "app_settings": public_app_settings(),
                "login_desktop_public_url": login_desktop_public_url(request),
                "login_desktop_display_mode": login_desktop_display_mode(),
            }
        )
        return templates.TemplateResponse(
            request,
            template_name,
            base_context,
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    def redirect(path="/", status_code=303):
        # Some mobile clients encode a Location fragment into the request path.
        # Keep in-page positioning in a query parameter instead of a fragment.
        parts = urlsplit(str(path))
        if parts.fragment and not parts.scheme and not parts.netloc and parts.path.startswith("/"):
            query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "_anchor"]
            query.append(("_anchor", parts.fragment))
            path = urlunsplit(("", "", parts.path, urlencode(query), ""))
        return RedirectResponse(url=path, status_code=status_code)

    @app.get("/settings#notify", include_in_schema=False)
    async def legacy_notify_fragment(request: Request):
        if not principal(request):
            return redirect("/login")
        return redirect("/settings?_anchor=notify")

    @app.get("/ops/send-console#account-{unique_id}", include_in_schema=False)
    async def legacy_send_console_fragment(request: Request, unique_id: str):
        # Some phone browsers send the dashboard's account link with "#" encoded.
        if not (unique_id.isascii() and unique_id.isdigit() and len(unique_id) <= 32):
            return Response(status_code=404)
        if not principal(request):
            return redirect("/login")
        return redirect(f"/ops/send-console?_anchor=account-{unique_id}")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(
            content=(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                '<path fill="#f97316" d="M18 2 6 18h8l-2 12 14-18h-9z"/></svg>'
            ),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/accounts#account-{unique_id}", include_in_schema=False)
    async def legacy_account_fragment(request: Request, unique_id: str):
        if not (unique_id.isascii() and unique_id.isdigit() and len(unique_id) <= 32):
            return Response(status_code=404)
        if not principal(request):
            return redirect("/login")
        return redirect(f"/accounts?_anchor=account-{unique_id}")

    def principal(request):
        return current_principal(request)

    def require_user(request):
        if not principal(request):
            return redirect("/login")
        return None

    def require_admin(request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect
        if principal(request).get("role") != "admin":
            return PlainTextResponse("Forbidden", status_code=403)
        return None

    def account_for_request(request, unique_id):
        accounts, _ = ensure_account_refs(get_userData(force_reload=True))
        account = account_by_unique_id(accounts, unique_id)
        if not account:
            return accounts, None, PlainTextResponse("Account not found", status_code=404)
        if not can_access_account(principal(request), account):
            return accounts, None, PlainTextResponse("Forbidden", status_code=403)
        return accounts, account, None

    def mutate_account_for_request(request, unique_id, mutator):
        current = principal(request)

        def mutate(accounts):
            account = account_by_unique_id(accounts, unique_id)
            if not account:
                return (None, None, 404), False
            if not can_access_account(current, account):
                return (None, None, 403), False
            return (account, mutator(account, accounts), None), True

        account, result, error = update_user_data(mutate, force_reload=True)
        return account, result, error

    def principal_account_refs(request):
        current = principal(request)
        if not current:
            return set()
        if current.get("role") == "admin":
            return None
        return list(current.get("account_refs", []))

    def scoped_ops_snapshot(request):
        refs = principal_account_refs(request)
        snapshot = get_ops_snapshot(account_refs=refs)
        if refs is not None:
            # Do not place host/container state or global log tails into a
            # normal user's rendered context.
            snapshot["containers"] = []
            snapshot["task_containers"] = []
            snapshot["crontab"] = ""
            snapshot["log_tail"] = []
            snapshot["compose_root"] = ""
            snapshot["compose_file"] = ""
            snapshot["image_present"] = False
        return snapshot

    def scoped_overview_snapshot(request):
        return get_overview_snapshot(account_refs=principal_account_refs(request))

    def flash(request, message, level="info"):
        request.session["flash"] = {"message": message, "level": level}

    def pop_flash(request):
        return request.session.pop("flash", None)

    @app.get("/debug-artifacts/{artifact_path:path}")
    async def debug_artifact(request: Request, artifact_path: str):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return maybe_redirect
        root = DEBUG_ARTIFACTS_DIR.resolve()
        candidate = (root / artifact_path).resolve()
        if root not in candidate.parents or not candidate.is_file():
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(candidate, headers={"Cache-Control": "no-store"})

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if principal(request):
            return redirect("/")
        if current_user(request):
            clear_session(request)
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
            flash(request, "Admin login is already configured.", "warning")
            return redirect("/login")

        form = await request.form()
        username = str(form.get("username", "admin")).strip() or "admin"
        password = str(form.get("password", ""))
        confirm = str(form.get("confirm_password", ""))
        if not password or password != confirm:
            flash(request, "Password setup failed. Please enter matching passwords.", "error")
            return redirect("/login")
        problem = password_problem(password, username)
        if problem:
            flash(request, problem, "error")
            return redirect("/login")

        bootstrap_admin_password(password, username=username)
        flash(request, "Admin credentials created. Please log in.", "success")
        return redirect("/login")

    @app.post("/login")
    async def login_action(request: Request):
        if not is_bootstrapped():
            flash(request, "Create the admin password first.", "warning")
            return redirect("/login")

        form = await request.form()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        from webui.users import authenticate

        client_ip = request.client.host if request.client else ""
        wait = login_throttle.retry_after(client_ip, username)
        if wait:
            minutes = max(1, (wait + 59) // 60)
            logger.warning("Web login locked for ip=%s user=%s retry_after=%ss", client_ip, username[:40], wait)
            flash(request, f"登录失败次数过多，请 {minutes} 分钟后再试。", "error")
            return redirect("/login")
        identity = authenticate(username, password)
        if not identity:
            login_throttle.record_failure(client_ip, username)
            logger.warning("Web login failed for ip=%s user=%s", client_ip, username[:40])
            flash(request, "Invalid username or password.", "error")
            return redirect("/login")
        login_throttle.record_success(client_ip, username)

        issue_session(
            request,
            identity["username"],
            role=identity["role"],
            account_refs=identity.get("account_refs", []),
            auth_version=identity.get("auth_version", 0),
        )
        flash(request, "Signed in successfully.", "success")
        return redirect("/")

    @app.post("/logout")
    async def logout_action(request: Request):
        clear_session(request)
        return redirect("/login")

    @app.get("/api/ops/overview")
    async def ops_overview(request: Request):
        if not principal(request):
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            scoped_overview_snapshot(request),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/account/password")
    async def change_own_password(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)
        current = principal(request)
        if current.get("role") == "admin":
            flash(request, "请在系统设置中修改管理员密码。", "info")
            return redirect("/settings#system")
        password = str(form.get("new_password", ""))
        confirm = str(form.get("confirm_password", ""))
        if not password or password != confirm:
            flash(request, "两次密码输入不一致。", "error")
            return redirect("/settings#password")
        problem = password_problem(password, current["username"])
        if problem:
            flash(request, problem, "error")
            return redirect("/settings#password")
        try:
            update_web_user(current["username"], password=password)
            flash(request, "密码已修改，请重新登录。", "success")
            clear_session(request)
            return redirect("/login")
        except UserStoreError as exc:
            flash(request, str(exc), "error")
            return redirect("/settings#password")

    @app.post("/admin/users/create")
    async def create_admin_user(request: Request):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return maybe_redirect
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)
        refs = [value for value in form.getlist("account_refs")] if hasattr(form, "getlist") else []
        problem = password_problem(str(form.get("password", "")), str(form.get("username", "")))
        if problem:
            flash(request, problem, "error")
            return redirect("/settings#users")
        try:
            create_web_user(
                str(form.get("username", "")),
                str(form.get("password", "")),
                enabled=str(form.get("enabled", "")) == "on",
                account_refs=refs,
            )
            flash(request, "普通用户已创建。", "success")
        except UserStoreError as exc:
            flash(request, str(exc), "error")
        return redirect("/settings#users")

    @app.post("/admin/users/{username}/update")
    async def update_admin_user(request: Request, username: str):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return maybe_redirect
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)
        refs = [value for value in form.getlist("account_refs")] if hasattr(form, "getlist") else []
        new_password = str(form.get("password", ""))
        if new_password:
            problem = password_problem(new_password, str(form.get("new_username", "")).strip() or username)
            if problem:
                flash(request, problem, "error")
                return redirect("/settings#users")
        try:
            update_web_user(
                username,
                new_username=str(form.get("new_username", "")).strip() or None,
                password=str(form.get("password", "")) or None,
                enabled=str(form.get("enabled", "")) == "on",
                account_refs=refs,
            )
            flash(request, "普通用户已更新。", "success")
        except UserStoreError as exc:
            flash(request, str(exc), "error")
        return redirect("/settings#users")

    @app.post("/admin/users/{username}/delete")
    async def delete_admin_user(request: Request, username: str):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return maybe_redirect
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)
        try:
            if delete_web_user(username):
                flash(request, "普通用户已删除，抖音账号数据未删除。", "success")
            else:
                flash(request, "普通用户不存在。", "error")
        except UserStoreError as exc:
            flash(request, str(exc), "error")
        return redirect("/settings#users")

    def console_context(request, *, with_admin_data=False):
        current = principal(request)
        is_admin_user = current.get("role") == "admin"
        context = {
            "flash": pop_flash(request),
            "accounts": get_visible_accounts(current, get_userData(force_reload=True)),
            "ops": scoped_ops_snapshot(request),
            "principal": current,
            "is_admin": is_admin_user,
            "runtime_config": {},
            "web_users": [],
            "all_accounts": [],
        }
        if with_admin_data and is_admin_user:
            context["runtime_config"] = get_config(force_reload=True)
            context["web_users"] = get_web_users()
            context["all_accounts"] = get_userData(force_reload=True)
        return context

    def filter_send_console(ops, query_params):
        """Return a display-only narrowed copy of the send-detail snapshot."""
        allowed_statuses = {"all", "confirmed", "pending", "attention"}
        status = str(query_params.get("status", "all")).strip()
        if status not in allowed_statuses:
            status = "all"
        query = str(query_params.get("q", "")).strip()[:120]
        account = str(query_params.get("account", "")).strip()

        filtered_ops = dict(ops)
        console = dict(ops.get("send_console") or {})
        source_accounts = list(console.get("accounts") or [])
        account_ids = {str(item.get("unique_id") or "") for item in source_accounts}
        if account not in account_ids:
            account = ""
        if account:
            source_accounts = [item for item in source_accounts if str(item.get("unique_id") or "") == account]

        status_fields = {
            "confirmed": {"confirmed_targets"},
            "pending": {"unconfirmed_targets"},
            "attention": {"failed_targets", "account_blocked_targets"},
        }
        visible_fields = status_fields.get(status)
        filtered_accounts = []
        for source in source_accounts:
            row = dict(source)
            for field in (
                "confirmed_targets", "page_echo_targets", "receipt_only_targets", "unconfirmed_targets",
                "failed_targets", "account_blocked_targets", "pending_targets", "unprocessed_targets",
            ):
                items = list(source.get(field) or [])
                if visible_fields is not None and field not in visible_fields:
                    items = []
                if query:
                    needle = query.casefold()
                    items = [item for item in items if needle in str(item.get("target") or "").casefold()]
                row[field] = items
            row["page_echo_count"] = len(row["page_echo_targets"])
            row["receipt_only_count"] = len(row["receipt_only_targets"])
            filtered_accounts.append(row)
        console["accounts"] = filtered_accounts
        filtered_ops["send_console"] = console
        return filtered_ops, {
            "status": status,
            "account": account,
            "query": query,
            "accounts": list((ops.get("send_console") or {}).get("accounts") or []),
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect
        return render_template(request, "dashboard.html", console_context(request))

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect
        return render_template(request, "accounts.html", console_context(request))

    @app.get("/login-workspace", response_class=HTMLResponse)
    async def login_workspace_page(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect
        context = console_context(request)
        # "Re-login" on the accounts page lands here with the account preselected;
        # only accounts the current user can see are matched.
        relogin = str(request.query_params.get("relogin", "")).strip()
        context["relogin_account"] = next(
            (
                account
                for account in context["accounts"]
                if relogin and str(account.get("unique_id") or "") == relogin
            ),
            None,
        )
        return render_template(request, "login_workspace.html", context)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect
        context = console_context(request, with_admin_data=True)
        if principal(request).get("role") == "admin":
            context["notify"] = notify.notifications_view(
                get_config(force_reload=True).get("notifications")
            )
        return render_template(
            request,
            "settings.html",
            context,
        )

    @app.get("/ops/send-console", response_class=HTMLResponse)
    async def send_console_page(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        ops, send_filters = filter_send_console(scoped_ops_snapshot(request), request.query_params)
        return render_template(
            request,
            "send_console.html",
            {
                "flash": pop_flash(request),
                "ops": ops,
                "send_filters": send_filters,
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
        targets = _dedupe_targets(
            [*extract_targets_from_form(form), *extract_group_targets_from_form(form)]
        )

        def mutate(account, accounts):
            del accounts
            previous_targets = list(account.get("targets") or [])
            account["username"] = username or account.get("username", "")
            picked_ids = sorted({str(value) for value in form.getlist('group_bind')})
            cache = {
                str(item.get('conversationId')): str(item.get('name') or '').strip()
                for item in account.get('group_cache', [])
                if isinstance(item, dict) and str(item.get('conversationId') or '').strip()
            }
            previous_bindings = {
                str(target): str(conversation_id)
                for target, conversation_id in dict(account.get('group_target_bindings') or {}).items()
                if str(target).strip() and str(conversation_id).strip()
            }
            old_target_by_id = {}
            for target, conversation_id in previous_bindings.items():
                old_target_by_id.setdefault(conversation_id, target)
            bindings = {}
            bound_targets = []
            # Bound targets are daily-send ledger keys.  Reusing them by stable
            # conversation id prevents a refresh/name change from making the
            # sender treat the same group as a new target today.
            for conversation_id in picked_ids:
                target = old_target_by_id.get(conversation_id)
                if target:
                    bindings[target] = conversation_id
                    bound_targets.append(target)
            existing_targets = set(previous_bindings)
            existing_visible_names = {
                target.removeprefix('群聊：').removeprefix('群聊:')
                for target in previous_bindings
            }
            cached_name_counts = {}
            for name in cache.values():
                if name:
                    cached_name_counts[name] = cached_name_counts.get(name, 0) + 1
            for conversation_id in picked_ids:
                if conversation_id in old_target_by_id:
                    continue
                name = cache.get(conversation_id)
                if not name:
                    continue
                collides = cached_name_counts.get(name, 0) > 1 or name in existing_visible_names
                target = f'群聊：{name}{f"#{conversation_id[-4:]}" if collides else ""}'
                if target in existing_targets or target in bindings:
                    target = f'{target}#{conversation_id[-4:]}'
                bindings[target] = conversation_id
                bound_targets.append(target)
            bound_target_set = set(bound_targets)
            manual_groups = [
                item for item in extract_group_targets_from_form(form)
                if item not in bound_target_set
            ]
            account["targets"] = _dedupe_targets([*extract_targets_from_form(form), *manual_groups, *bound_targets])
            account['group_target_bindings'] = bindings
            account["enabled"] = str(form.get("enabled", "")) == "on"
            # Selecting a friend as a target binds it to that friend's uid; a
            # target that stays selected keeps its existing binding.
            friend_identity.apply_web_target_selection(account, previous_targets)
            return None

        account, _, access_error = mutate_account_for_request(
            request,
            unique_id,
            mutate,
        )
        if access_error:
            return PlainTextResponse(
                "Forbidden" if access_error == 403 else "Account not found",
                status_code=access_error,
            )
        if account:
            flash(request, f"Updated account {account['username']}.", "success")
        else:
            flash(request, "Account not found.", "error")

        return redirect(f"/accounts#account-{quote(str(unique_id), safe='')}")

    @app.post("/accounts/{unique_id}/toggle-enabled")
    async def toggle_account_enabled(request: Request, unique_id: str):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        def mutate(account, accounts):
            del accounts
            account["enabled"] = not is_account_enabled(account)
            return None

        account, _, access_error = mutate_account_for_request(
            request,
            unique_id,
            mutate,
        )
        if access_error:
            return PlainTextResponse(
                "Forbidden" if access_error == 403 else "Account not found",
                status_code=access_error,
            )
        flash(
            request,
            f"{account.get('username', 'Account')} 已{'启用' if account['enabled'] else '停用'}自动续火花。",
            "success",
        )
        return redirect(f"/accounts#account-{quote(str(unique_id), safe='')}")

    # Async friend refresh. The web app is a single uvicorn process, so a plain
    # dict is enough: one entry per account, overwritten by the next refresh.
    friend_refresh_jobs: dict[str, dict] = {}

    def _write_friend_cache(normalized_id, friends, updated_at):
        def mutate(accounts):
            target = account_by_unique_id(accounts, normalized_id)
            if not target:
                return None, False
            target["friends_cache"] = list(friends)
            target["friends_cache_updated_at"] = updated_at
            return None, True

        return update_user_data(mutate, force_reload=True)

    def _record_friend_identity(account, friends):
        """Save the uids a complete refresh read and reconcile the targets."""
        try:
            result = friend_identity.persist_identity_scan(
                account,
                getattr(friends, "uid_entries", None) or [],
                scan_complete=True,
                scanned_names=list(friends),
                sync_user=False,
            )
        except Exception:
            logger.warning(
                "Friend uid update after refresh failed for %s",
                account.get("username", "unknown"),
                exc_info=True,
            )
            return {}
        if not result:
            return {}
        return {
            "uidCount": result["uidCount"],
            "uidDataTrusted": result["uidDataTrusted"],
            "renamed": [
                {"oldName": item["oldName"], "newName": item["newName"]}
                for item in result["renamed"]
            ],
            "missing": [
                item["target"]
                for item in result["targets"]
                if item["status"] == friend_identity.STATUS_MISSING
            ],
        }

    def _finish_friend_job(normalized_id, **fields):
        job = friend_refresh_jobs.get(normalized_id)
        if job is not None:
            job.update(fields)
            job["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        _friend_refresh_active.discard(normalized_id)

    async def _run_friend_refresh_job(normalized_id, account, username):
        def on_progress(collected):
            job = friend_refresh_jobs.get(normalized_id)
            if job is not None:
                job["stage"] = "collecting"
                job["collected"] = int(collected)

        try:
            friends = await asyncio.wait_for(
                fetch_account_friends(account, on_progress=on_progress),
                timeout=_FRIEND_REFRESH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Async friend refresh timed out for %s after %ss",
                account.get("username", normalized_id),
                _FRIEND_REFRESH_TIMEOUT_SECONDS,
            )
            # Exceeding the read budget is an incomplete read, not a transport
            # failure, so the console reports it the same way as a stalled scan.
            _finish_friend_job(
                normalized_id,
                state="failed",
                stage="incomplete",
                **_incomplete_scan_fields(
                    f"读取超时，超过 {_FRIEND_REFRESH_TIMEOUT_SECONDS} 秒"
                ),
                scanComplete=False,
            )
            return
        except FriendRefreshError as exc:
            category = exc.category or CATEGORY_STRUCTURE_CHANGED
            logger.warning(
                "Async friend refresh failed for %s category=%s: %s",
                account.get("username", normalized_id),
                category,
                exc,
            )
            _finish_friend_job(
                normalized_id,
                state="failed",
                stage="failed",
                error=str(exc),
                category=category,
                categoryLabel=CATEGORY_LABELS.get(
                    category, CATEGORY_LABELS[CATEGORY_STRUCTURE_CHANGED]
                ),
                retryable=category != CATEGORY_LOGIN_REQUIRED,
            )
            return
        except Exception as exc:
            logger.warning(
                "Async friend refresh errored for %s: %s",
                account.get("username", normalized_id),
                exc,
                exc_info=True,
            )
            _finish_friend_job(
                normalized_id,
                state="failed",
                stage="failed",
                error=str(exc),
                category=CATEGORY_STRUCTURE_CHANGED,
                categoryLabel=CATEGORY_LABELS[CATEGORY_STRUCTURE_CHANGED],
                retryable=True,
            )
            return

        previous_cache = list(account.get("friends_cache") or [])
        scan_complete = bool(getattr(friends, "complete", False))
        scan_reason = str(getattr(friends, "reason", "") or "")
        if not scan_complete:
            # A read that stopped early is not proof the list shrank, so it must
            # never replace the last good cache or rebuild the send index.
            logger.warning(
                "Async friend refresh incomplete for %s: count=%s reason=%s",
                account.get("username", normalized_id),
                len(friends),
                scan_reason or "-",
            )
            _finish_friend_job(
                normalized_id,
                state="failed",
                stage="incomplete",
                **_incomplete_scan_fields(scan_reason),
                scanComplete=False,
            )
            return
        if not friends:
            logger.warning(
                "Async friend refresh returned no names for %s (complete=%s); keeping %s cached friends",
                account.get("username", normalized_id),
                scan_complete,
                len(previous_cache),
            )
            _finish_friend_job(
                normalized_id,
                state="failed",
                stage="empty",
                error="本次没有读到任何好友，已保留上一次的好友数据；请稍后重试，或先确认该账号登录态。",
                category=CATEGORY_EMPTY_RESULT,
                categoryLabel=CATEGORY_LABELS[CATEGORY_EMPTY_RESULT],
                retryable=True,
            )
            return

        updated_at = datetime.now().isoformat(timespec="seconds")
        _write_friend_cache(normalized_id, friends, updated_at)

        index_updated = False
        if scan_complete and friends:
            try:
                record_friend_scan(
                    account,
                    friends,
                    scan_complete=True,
                    targets=account.get("targets") or [],
                )
                index_updated = True
            except Exception:
                logger.warning(
                    "Friend index update after async refresh failed for %s",
                    account.get("username", normalized_id),
                    exc_info=True,
                )
        identity = _record_friend_identity(account, friends) if index_updated else {}

        _finish_friend_job(
            normalized_id,
            state="done",
            stage="done",
            friends=list(friends),
            updatedAt=updated_at,
            scanComplete=scan_complete,
            indexUpdated=index_updated,
            identity=identity,
            message=f"已刷新 {len(friends)} 个好友"
            + ("，并已重建发送索引" if index_updated else ""),
        )

    def _start_friend_refresh_if_free(normalized_id, account, username):
        """Start the background friend refresh when nothing blocks it.

        Returns what the caller should report: started, running (already
        refreshing), busy (a send run holds the lock, because the send flow
        drives its own browser per account) or skipped.
        """
        if not normalized_id:
            return "skipped"
        if task_run_lock_status().get("running"):
            return "busy"
        if normalized_id in _friend_refresh_active or friend_refresh_conflict(normalized_id):
            return "running"
        _friend_refresh_active.add(normalized_id)
        friend_refresh_jobs[normalized_id] = {
            "state": "running",
            "stage": "starting",
            "collected": 0,
            "startedAt": datetime.now().isoformat(timespec="seconds"),
            "previousUpdatedAt": account.get("friends_cache_updated_at", ""),
        }
        asyncio.create_task(
            _run_friend_refresh_job(normalized_id, dict(account), str(username or ""))
        )
        return "started"

    # A saved login is verified here, one account at a time: each check drives
    # a browser, and the two-core host shares it with the login browser and the
    # send runs. The check waits until neither of those is running.
    login_verification_lock = asyncio.Lock()
    login_verification_tasks = set()

    async def _wait_until_browser_is_free(deadline):
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            if not task_run_lock_status().get("running") and not get_login_lock():
                return True
            await asyncio.sleep(LOGIN_VERIFICATION_IDLE_POLL_SECONDS)
        return False

    async def _verify_saved_login(account_ref, token, generation="", *, delay=0):
        loop = asyncio.get_running_loop()
        if delay:
            await asyncio.sleep(delay)
        async with login_verification_lock:
            # Counted from this account's turn, not from when it was queued.
            deadline = loop.time() + LOGIN_VERIFICATION_MAX_WAIT_SECONDS
            if not await _wait_until_browser_is_free(deadline):
                await asyncio.to_thread(
                    write_login_verification,
                    account_ref,
                    token,
                    "unverified",
                    category="busy",
                    reason="长时间没有空闲的浏览器可用于验证",
                    generation=generation,
                )
                return
            account = account_by_ref(get_userData(force_reload=True), account_ref)
            if not account or (account.get("login_verification") or {}).get("token") != token:
                return
            if generation and account_identity.account_login_generation(account) != generation:
                # A newer login superseded this check while it waited (A7).
                return
            # Only a page that shows the login is gone pauses the account; a
            # timeout, a browser error or an unreadable identity is "unverified"
            # and leaves sending alone (D12/Q4).
            reason, category = "", ""
            expected_uid = actual_uid = ""
            try:
                await asyncio.wait_for(
                    verify_account_session(account, auth_only=True),
                    timeout=LOGIN_VERIFICATION_TIMEOUT_SECONDS,
                )
                outcome = "verified"
            except FriendRefreshError as exc:
                category = exc.category or CATEGORY_STRUCTURE_CHANGED
                reason = str(exc)
                outcome = (
                    "login_required"
                    if category in streak_state.LOGIN_FAILURE_CATEGORIES
                    else "unverified"
                )
            except asyncio.TimeoutError:
                category, reason, outcome = CATEGORY_NETWORK_UNAVAILABLE, "验证登录态超时", "unverified"
            except Exception as exc:  # noqa: BLE001 - any other failure is not a verdict
                logger.warning("Background login verification errored: %s", exc)
                category, reason, outcome = "verification_error", f"验证时出错：{exc}", "unverified"
            if outcome == "verified":
                outcome, category, reason, expected_uid, actual_uid = await check_saved_login_identity(account)
            logger.info(
                "Background login verification: ref=%s outcome=%s category=%s",
                account_ref,
                outcome,
                category or "",
            )
            saved = await asyncio.to_thread(
                write_login_verification,
                account_ref,
                token,
                outcome,
                category=category or "",
                reason=reason or "",
                expected_uid=expected_uid,
                actual_uid=actual_uid,
                generation=generation,
            )
            if outcome != "verified" or not saved:
                return
            # The save used to start this refresh while the saver still held the
            # login workspace, so it never ran; here the browser is free.
            normalized_id = normalize_unique_id(saved.get("unique_id"))
            if task_run_lock_status().get("running") or get_login_lock():
                # The browser got busy during the check; the next manual or
                # post-send refresh will pick the friend list up.
                return
            if normalized_id and normalized_id not in _friend_refresh_active:
                _friend_refresh_active.add(normalized_id)
                friend_refresh_jobs[normalized_id] = {
                    "state": "running",
                    "stage": "starting",
                    "collected": 0,
                    "startedAt": datetime.now().isoformat(timespec="seconds"),
                    "previousUpdatedAt": saved.get("friends_cache_updated_at", ""),
                }
                await _run_friend_refresh_job(normalized_id, saved, "")

    def _queue_login_verification(account_ref, token, generation="", *, delay=0):
        task = asyncio.create_task(
            _verify_saved_login(account_ref, token, generation, delay=delay)
        )
        login_verification_tasks.add(task)

        def finished(done):
            login_verification_tasks.discard(done)
            if not done.cancelled() and done.exception():
                logger.error("Background login verification failed", exc_info=done.exception())

        task.add_done_callback(finished)

    def _requeue_pending_login_verifications():
        """After a restart, verify again whatever was still being verified."""
        for account in get_userData(force_reload=True):
            check = dict(account.get("login_verification") or {})
            ref = str(account.get("account_ref") or "")
            if check.get("state") == "verifying" and check.get("token") and ref:
                _queue_login_verification(
                    ref,
                    check["token"],
                    str(check.get("generation") or ""),
                    delay=LOGIN_VERIFICATION_RESTART_DELAY_SECONDS,
                )

    @app.post("/accounts/{unique_id}/friends/refresh/async")
    async def refresh_account_friend_list_async(request: Request, unique_id: str):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"error": "Invalid CSRF token"}, status_code=403)

        _, account, access_error = account_for_request(request, unique_id)
        if access_error:
            return JSONResponse(
                {"error": "Forbidden" if access_error.status_code == 403 else "Account not found."},
                status_code=access_error.status_code,
            )

        normalized_id = normalize_unique_id(unique_id)
        if task_run_lock_status().get("running"):
            # A send run drives its own browser per account; starting a refresh
            # at the same time would fight over the same profile.
            return JSONResponse(
                {
                    "error": "发送任务正在运行，为避免与发送流程同时驱动同一账号的浏览器，请稍后再刷新。",
                    "category": "busy",
                    "retryable": True,
                },
                status_code=409,
                headers={"Retry-After": "30"},
            )
        conflict = friend_refresh_conflict(normalized_id)
        if conflict:
            return JSONResponse(
                {"error": conflict, "category": "busy", "retryable": True},
                status_code=429,
                headers={"Retry-After": "5"},
            )

        _friend_refresh_active.add(normalized_id)
        friend_refresh_jobs[normalized_id] = {
            "state": "running",
            "stage": "starting",
            "collected": 0,
            "startedAt": datetime.now().isoformat(timespec="seconds"),
            "previousUpdatedAt": account.get("friends_cache_updated_at", ""),
        }
        asyncio.create_task(
            _run_friend_refresh_job(normalized_id, dict(account), principal(request).get("username", ""))
        )
        return JSONResponse(
            {"ok": True, "state": "running", "job": dict(friend_refresh_jobs[normalized_id])},
            status_code=202,
        )

    @app.get("/accounts/{unique_id}/friends/refresh/status")
    async def refresh_account_friend_list_status(request: Request, unique_id: str):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        _, account, access_error = account_for_request(request, unique_id)
        if access_error:
            return JSONResponse(
                {"error": "Forbidden" if access_error.status_code == 403 else "Account not found."},
                status_code=access_error.status_code,
            )

        normalized_id = normalize_unique_id(unique_id)
        job = friend_refresh_jobs.get(normalized_id)
        if job is None:
            return JSONResponse(
                {
                    "state": "idle",
                    "stage": "idle",
                    "previousUpdatedAt": account.get("friends_cache_updated_at", ""),
                }
            )
        return JSONResponse(dict(job))

    group_refresh_jobs: dict[str, dict] = {}

    async def _run_group_refresh_job(normalized_id, account):
        try:
            groups = await asyncio.wait_for(fetch_account_groups(account), timeout=_FRIEND_REFRESH_TIMEOUT_SECONDS)
            if not groups:
                raise RuntimeError('未读到群列表')
            updated_at = datetime.now().isoformat(timespec="seconds")
            def mutate(accounts):
                target = account_by_unique_id(accounts, normalized_id)
                if not target: return None, False
                target['group_cache'] = list(groups); target['group_cache_updated_at'] = updated_at
                names = {item['conversationId']: item['name'] for item in groups}
                notices = dict(target.get('group_target_notices') or {})
                statuses = []
                for target_name, cid in dict(target.get('group_target_bindings') or {}).items():
                    name = names.get(str(cid))
                    if name is None: statuses.append({'target':target_name,'status':'missing'}); notices[target_name]={'kind':'missing','at':updated_at}
                    else:
                        _, configured_name = parse_chat_target(target_name)
                        suffix = f'#{str(cid)[-4:]}'
                        if configured_name.endswith(suffix):
                            configured_name = configured_name[:-len(suffix)]
                        if _normalize_target_name(name) != _normalize_target_name(configured_name):
                            statuses.append({'target':target_name,'status':'renamed','name':name}); notices[target_name]={'kind':'renamed','currentName':name,'at':updated_at}
                        else:
                            statuses.append({'target':target_name,'status':'ok'}); notices.pop(target_name, None)
                target['group_target_notices'] = notices
                return statuses, True
            statuses = await asyncio.to_thread(update_user_data, mutate, force_reload=True)
            group_refresh_jobs[normalized_id].update(state='done', stage='done', groups=groups, updatedAt=updated_at, targets=statuses or [], message=f'已刷新 {len(groups)} 个群聊')
        except Exception as exc:
            group_refresh_jobs[normalized_id].update(state='failed', stage='failed', error=str(exc))
        finally: _group_refresh_active.discard(normalized_id)

    @app.post('/accounts/{unique_id}/groups/refresh/async')
    async def refresh_groups_async(request: Request, unique_id: str):
        if require_user(request): return JSONResponse({'error':'Unauthorized'}, status_code=401)
        form = await request.form()
        if not validate_csrf(request, str(form.get('csrf_token',''))): return JSONResponse({'error':'Invalid CSRF token'}, status_code=403)
        _, account, access_error = account_for_request(request, unique_id)
        if access_error: return JSONResponse({'error':'Forbidden'}, status_code=access_error.status_code)
        normalized_id = normalize_unique_id(unique_id)
        conflict = '该账号正在发送任务中，请稍后重试' if task_run_lock_status().get('running') else ''
        if not conflict and normalized_id in _group_refresh_active:
            conflict = '该账号已有群列表刷新正在进行，请稍后重试'
        if not conflict:
            # Same shared check friend refresh and patrol use (friend refresh,
            # patrol, login workspace): all of them drive this account's browser.
            conflict = friend_refresh_conflict(normalized_id)
        if conflict:
            return JSONResponse({'error': conflict, 'category': 'busy'}, status_code=409)
        _group_refresh_active.add(normalized_id); group_refresh_jobs[normalized_id]={'state':'running','stage':'starting','previousUpdatedAt':account.get('group_cache_updated_at','')}
        _start_background(_run_group_refresh_job(normalized_id, dict(account)))
        return JSONResponse({'ok':True,'state':'running'}, status_code=202)

    @app.get('/accounts/{unique_id}/groups/refresh/status')
    async def refresh_groups_status(request: Request, unique_id: str):
        if require_user(request): return JSONResponse({'error':'Unauthorized'}, status_code=401)
        _, account, access_error = account_for_request(request, unique_id)
        if access_error: return JSONResponse({'error':'Forbidden'}, status_code=access_error.status_code)
        return JSONResponse(dict(group_refresh_jobs.get(normalize_unique_id(unique_id), {'state':'idle','previousUpdatedAt':account.get('group_cache_updated_at','')})))

    # "Immediate patrol" for one account. Same permission and CSRF checks as
    # the friend refresh; the patrol itself runs in the background and shares
    # the per-account browser lock with the sender and the refresher.
    patrol_jobs: dict[str, dict] = {}

    async def _run_patrol_job(normalized_id, account_ref):
        try:
            outcome = await account_patrol.run_patrol(
                [account_ref],
                trigger="manual",
            )
            results = list((outcome or {}).get("accounts") or [])
            result = results[0] if results else {}
            job = patrol_jobs.get(normalized_id)
            if job is not None:
                job.update(
                    {
                        "state": "done" if result else "failed",
                        "conclusion": str(result.get("conclusion") or ""),
                        "conclusionLabel": account_patrol.CONCLUSION_LABELS.get(
                            str(result.get("conclusion") or ""), ""
                        ),
                        "reason": str(result.get("reason") or ("" if result else "账号未启用或已不存在")),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - reported on the job only
            logger.warning("Manual patrol failed for %s: %s", normalized_id, type(exc).__name__, exc_info=True)
            job = patrol_jobs.get(normalized_id)
            if job is not None:
                job.update({"state": "failed", "reason": f"巡查出错：{type(exc).__name__}"})
        finally:
            job = patrol_jobs.get(normalized_id)
            if job is not None:
                job["finishedAt"] = datetime.now().isoformat(timespec="seconds")
            _patrol_active.discard(normalized_id)

    @app.post("/accounts/{unique_id}/patrol")
    async def patrol_account_now(request: Request, unique_id: str):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"error": "Invalid CSRF token"}, status_code=403)

        _, account, access_error = account_for_request(request, unique_id)
        if access_error:
            return JSONResponse(
                {"error": "Forbidden" if access_error.status_code == 403 else "Account not found."},
                status_code=access_error.status_code,
            )
        if not account.get("enabled", True):
            return JSONResponse(
                {"error": "账号已停用，巡查只检查启用的账号。", "category": "disabled"},
                status_code=409,
            )

        normalized_id = normalize_unique_id(unique_id)
        if task_run_lock_status().get("running"):
            # A send run drives its own browser per account; same rule as the
            # friend refresh.
            return JSONResponse(
                {
                    "error": "发送任务正在运行，为避免与发送流程同时驱动同一账号的浏览器，请稍后再巡查。",
                    "category": "busy",
                    "retryable": True,
                },
                status_code=409,
                headers={"Retry-After": "30"},
            )
        if normalized_id in _patrol_active:
            return JSONResponse(
                {"error": "该账号已有巡查正在进行，请稍后再试", "category": "busy", "retryable": True},
                status_code=429,
                headers={"Retry-After": "10"},
            )
        conflict = friend_refresh_conflict(normalized_id)
        if conflict:
            return JSONResponse(
                {"error": conflict, "category": "busy", "retryable": True},
                status_code=429,
                headers={"Retry-After": "5"},
            )

        _patrol_active.add(normalized_id)
        patrol_jobs[normalized_id] = {
            "state": "running",
            "startedAt": datetime.now().isoformat(timespec="seconds"),
        }
        _start_background(
            _run_patrol_job(normalized_id, str(account.get("account_ref") or normalized_id))
        )
        return JSONResponse(
            {"ok": True, "state": "running", "job": dict(patrol_jobs[normalized_id])},
            status_code=202,
        )

    @app.get("/accounts/{unique_id}/patrol/status")
    async def patrol_account_status(request: Request, unique_id: str):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        _, account, access_error = account_for_request(request, unique_id)
        if access_error:
            return JSONResponse(
                {"error": "Forbidden" if access_error.status_code == 403 else "Account not found."},
                status_code=access_error.status_code,
            )
        normalized_id = normalize_unique_id(unique_id)
        job = dict(patrol_jobs.get(normalized_id) or {"state": "idle"})
        job["latest"] = account_patrol.patrol_view(account)
        return JSONResponse(job)

    @app.post("/accounts/{unique_id}/friends/refresh")
    async def refresh_account_friend_list(request: Request, unique_id: str):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"error": "Invalid CSRF token"}, status_code=403)

        _, account, access_error = account_for_request(request, unique_id)
        if access_error:
            return JSONResponse({"error": "Forbidden" if access_error.status_code == 403 else "Account not found."}, status_code=access_error.status_code)

        normalized_id = normalize_unique_id(unique_id)
        if task_run_lock_status().get("running"):
            return JSONResponse(
                {
                    "error": "发送任务正在运行，为避免与发送流程同时驱动同一账号的浏览器，请稍后再刷新。",
                    "category": "busy",
                    "retryable": True,
                },
                status_code=409,
                headers={"Retry-After": "30"},
            )
        conflict = friend_refresh_conflict(normalized_id)
        if conflict:
            # 429 keeps the caller's account data untouched and tells the caller
            # to retry later; no second browser is started.
            return JSONResponse(
                {"error": conflict, "category": "busy", "retryable": True},
                status_code=429,
                headers={"Retry-After": "5"},
            )

        _friend_refresh_active.add(normalized_id)
        try:
            friends = await asyncio.wait_for(
                fetch_account_friends(account),
                timeout=_FRIEND_REFRESH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Friend refresh timed out for %s after %ss",
                account.get("username", normalized_id),
                _FRIEND_REFRESH_TIMEOUT_SECONDS,
            )
            return JSONResponse(
                {
                    # Exceeding the read budget is an incomplete read, not a
                    # transport failure, so the console reports it as such.
                    **_incomplete_scan_fields(
                        f"读取超时，超过 {_FRIEND_REFRESH_TIMEOUT_SECONDS} 秒"
                    ),
                    "scan_complete": False,
                    "previousUpdatedAt": account.get("friends_cache_updated_at", ""),
                },
                status_code=504,
            )
        except FriendRefreshError as exc:
            category = exc.category or CATEGORY_STRUCTURE_CHANGED
            logger.warning(
                "Friend refresh failed for %s category=%s: %s",
                account.get("username", normalized_id),
                category,
                exc,
            )
            status_code = 401 if category == CATEGORY_LOGIN_REQUIRED else 502
            return JSONResponse(
                {
                    "error": str(exc),
                    "category": category,
                    "categoryLabel": CATEGORY_LABELS.get(category, CATEGORY_LABELS[CATEGORY_STRUCTURE_CHANGED]),
                    "retryable": category != CATEGORY_LOGIN_REQUIRED,
                    "previousUpdatedAt": account.get("friends_cache_updated_at", ""),
                },
                status_code=status_code,
            )
        except RuntimeError as exc:
            return JSONResponse(
                {
                    "error": str(exc),
                    "category": CATEGORY_STRUCTURE_CHANGED,
                    "categoryLabel": CATEGORY_LABELS[CATEGORY_STRUCTURE_CHANGED],
                    "retryable": True,
                    "previousUpdatedAt": account.get("friends_cache_updated_at", ""),
                },
                status_code=502,
            )
        finally:
            _friend_refresh_active.discard(normalized_id)

        previous_updated_at = account.get("friends_cache_updated_at", "")
        previous_cache = list(account.get("friends_cache") or [])
        scan_complete = bool(getattr(friends, "complete", False))
        scan_reason = str(getattr(friends, "reason", "") or "")
        if not scan_complete:
            # A read that stopped early is not proof the list shrank, so it must
            # never replace the last good cache or rebuild the send index.
            logger.warning(
                "Friend refresh incomplete for %s: count=%s reason=%s",
                account.get("username", normalized_id),
                len(friends),
                scan_reason or "-",
            )
            return JSONResponse(
                {
                    **_incomplete_scan_fields(scan_reason),
                    "scan_complete": False,
                    "previousUpdatedAt": previous_updated_at,
                },
                status_code=502,
            )
        if not friends:
            # An empty read is not proof that the friend list is empty, so keep
            # whatever we had and let the operator retry instead of wiping the
            # friend picker with a single flaky read.
            logger.warning(
                "Friend refresh returned no names for %s (complete=%s); keeping %s cached friends",
                account.get("username", normalized_id),
                scan_complete,
                len(previous_cache),
            )
            return JSONResponse(
                {
                    "error": "本次没有读到任何好友，已保留上一次的好友数据；请稍后重试，或先确认该账号登录态。",
                    "category": CATEGORY_EMPTY_RESULT,
                    "categoryLabel": CATEGORY_LABELS[CATEGORY_EMPTY_RESULT],
                    "retryable": True,
                    "previousUpdatedAt": previous_updated_at,
                },
                status_code=502,
            )

        updated_at = datetime.now().isoformat(timespec="seconds")

        def mutate(updated_account, accounts):
            del accounts
            updated_account["friends_cache"] = friends
            updated_account["friends_cache_updated_at"] = updated_at
            return None

        _, _, access_error = mutate_account_for_request(
            request,
            unique_id,
            mutate,
        )
        if access_error:
            return JSONResponse(
                {"error": "Forbidden" if access_error == 403 else "Account not found."},
                status_code=access_error,
            )

        index_updated = False
        if scan_complete and friends:
            try:
                record_friend_scan(
                    account,
                    friends,
                    scan_complete=True,
                    targets=account.get("targets") or [],
                )
                index_updated = True
            except Exception:
                logger.warning(
                    "Friend index update after refresh failed for %s",
                    account.get("username", normalized_id),
                    exc_info=True,
                )
        identity = _record_friend_identity(account, friends) if index_updated else {}

        logger.info(
            "Friend refresh stored for %s: count=%s complete=%s index_updated=%s",
            account.get("username", normalized_id),
            len(friends),
            scan_complete,
            index_updated,
        )
        return JSONResponse(
            {
                "friends": friends,
                "updated_at": updated_at,
                "previous_updated_at": previous_updated_at,
                "scan_complete": scan_complete,
                "index_updated": index_updated,
                "identity": identity,
                "message": f"已刷新 {len(friends)} 个好友"
                + ("，并已重建发送索引" if index_updated else ""),
            }
        )

    @app.post("/accounts/{unique_id}/merge-into")
    async def merge_duplicate_account(request: Request, unique_id: str):
        """Move the targets and ledgers of a duplicate row into the row to keep."""
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            flash(request, "合并失败：页面已过期，请刷新后重试。", "error")
            return redirect("/accounts")

        _, source, access_error = account_for_request(request, unique_id)
        if access_error:
            flash(request, "合并失败：找不到这条账号或没有权限。", "error")
            return redirect("/accounts")

        target_ref = str(form.get("target_account_ref", "")).strip()
        accounts, _ = ensure_account_refs(get_userData(force_reload=True))
        target = account_by_ref(accounts, target_ref)
        if not target or not can_access_account(principal(request), target):
            flash(request, "合并失败：要保留的账号不存在或无权访问。", "error")
            return redirect("/accounts")

        source_name = str(source.get("username") or "").strip()
        target_name = str(target.get("username") or "").strip()
        if not source_name or source_name != target_name:
            # Same nickname is the whole point: it is what makes two rows look
            # like one account, and merging different people would be data loss.
            flash(request, "合并失败：只能合并昵称相同的账号，请确认这两条确实是同一个账号。", "error")
            return redirect("/accounts")
        if normalize_unique_id(source.get("unique_id")) == normalize_unique_id(target.get("unique_id")):
            flash(request, "合并失败：两条记录指向同一个账号，无需合并。", "error")
            return redirect("/accounts")

        merged, changed = merge_user_account_into(
            source.get("unique_id"), target.get("unique_id")
        )
        if not changed:
            flash(request, "合并失败：账号可能已被其他操作改动，请刷新后重试。", "error")
            return redirect("/accounts")
        logger.info(
            "Merged duplicate account: kept_uid=%s removed_uid=%s targets=%s",
            normalize_unique_id(target.get("unique_id")),
            normalize_unique_id(source.get("unique_id")),
            len(merged.get("targets") or []) if merged else 0,
        )
        flash(
            request,
            f"已合并重复账号：目标已并入保留账号，现有 {len((merged or {}).get('targets') or [])} 个目标。",
            "success",
        )
        return redirect("/accounts")

    @app.post("/accounts/{unique_id}/delete")
    async def delete_account(request: Request, unique_id: str):
        # Any user may delete an account they own; mutate_account_for_request
        # rejects the rest with 403. Admin still deletes anything.
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        current = principal(request)

        def mutate(account, accounts):
            del account
            normalized = normalize_unique_id(unique_id)
            removed_refs = set()
            remaining = []
            for item in accounts:
                # Duplicate rows for one unique_id are legacy data; only remove
                # the ones this user may actually access so a delete can never
                # take another user's row with it.
                if (
                    normalize_unique_id(item.get("unique_id")) == normalized
                    and can_access_account(current, item)
                ):
                    ref = str(item.get("account_ref", "")).strip()
                    if ref:
                        removed_refs.add(ref)
                    continue
                remaining.append(item)
            accounts[:] = remaining
            if removed_refs:
                # Drop the dangling ownership so no user keeps a ref pointing at
                # a row that no longer exists.
                remove_account_refs_from_users(removed_refs)
            return None

        account, _, access_error = mutate_account_for_request(
            request,
            unique_id,
            mutate,
        )
        if access_error:
            return PlainTextResponse(
                "Forbidden" if access_error == 403 else "Account not found",
                status_code=access_error,
            )
        if account:
            flash(request, "Account deleted.", "success")
        else:
            flash(request, "Account not found.", "error")
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
            flash(request, "Target is required for retry.", "error")
            return redirect("/ops/send-console")

        accounts, account, access_error = account_for_request(request, unique_id)
        if access_error:
            return access_error

        lock_status = task_run_lock_status()
        if lock_status.get("running"):
            flash(request, "已有发送任务正在运行，本次单目标重试没有启动。请等当前任务结束后再试。", "warning")
            return redirect("/ops/send-console")

        account_copy = dict(account)
        account_copy["targets"] = [target_name]
        config = manual_retry_config(get_config(force_reload=True))
        channel = manual_retry_channel(config)
        run_id = uuid.uuid4().hex
        started_at = datetime.now(timezone.utc)
        run_status = "completed"

        try:
            try:
                with task_run_lock():
                    # The retry has to use the same channel as the scheduled run;
                    # a browser-only retry would contradict a protocol-primary
                    # deployment and record evidence the operator cannot trust.
                    if channel == "protocol":
                        await run_protocol_tasks(
                            config,
                            [account_copy],
                            None,
                            run_id=run_id,
                            allow_retry=True,
                        )
                    else:
                        await run_browser_tasks(
                            config,
                            [account_copy],
                            run_id=run_id,
                            allow_retry=True,
                        )
            except Exception as exc:
                run_status = "failed"
                flash(request, f"Retry failed for {account.get('username', 'Account')} / {target_name}: {exc}", "error")
                return redirect("/ops/send-console")
        finally:
            try:
                _append_streak_run_report(
                    run_id,
                    started_at,
                    config,
                    get_userData(force_reload=True),
                    run_status=run_status,
                )
            except Exception:
                logger.exception(
                    "Failed to append manual streak retry report for %s / %s",
                    account.get("username", "Account"),
                    target_name,
                )

        updated_account = find_account(get_userData(force_reload=True), unique_id) or {}
        if _target_sent_today(updated_account, target_name):
            flash(request, f"已重试 {account.get('username', 'Account')} / {target_name}，并获得强证据确认。", "success")
        elif _target_unconfirmed_today(updated_account, target_name):
            failure_entry = dict(updated_account.get("failure_queue") or {}).get(target_name) or {}
            reason = str(failure_entry.get("reason") or "Retry ran but did not get strong confirmation.")
            flash(request, f"已执行 {account.get('username', 'Account')} / {target_name}，但未强确认，已进入待核验/待补发：{reason}", "warning")
        else:
            account_failure = dict(updated_account.get("account_failure") or {})
            affected_targets = list(account_failure.get("affectedTargets") or [])
            failure_entry = dict(updated_account.get("failure_queue") or {}).get(target_name) or {}
            if any(
                streak_state.target_keys_match(
                    updated_account,
                    target_name,
                    affected_target,
                )
                for affected_target in affected_targets
            ):
                reason = str(account_failure.get("reason") or "Account-level browser failure.")
            else:
                reason = str(failure_entry.get("reason") or "Retry did not confirm a successful send.")
            flash(request, f"Retry did not succeed for {account.get('username', 'Account')} / {target_name}: {reason}", "error")
        return redirect("/ops/send-console")

    @app.post("/accounts/{unique_id}/mark-target-unconfirmed")
    async def mark_account_target_unconfirmed(request: Request, unique_id: str):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        target_name = str(form.get("target", "")).strip()
        if not target_name:
            flash(request, "Target is required.", "error")
            return redirect("/ops/send-console")

        def mutate(account, accounts):
            del accounts
            # The button is only offered for confirmed records, and it must always
            # take effect: without force, a record from an earlier day was silently
            # refused, so the click looked successful but changed nothing.
            return mark_target_unconfirmed(account, target_name, force=True)

        account, changed, access_error = mutate_account_for_request(
            request,
            unique_id,
            mutate,
        )
        if access_error:
            return PlainTextResponse(
                "Forbidden" if access_error == 403 else "Account not found",
                status_code=access_error,
            )

        if changed:
            flash(request, f"已将 {account.get('username', 'Account')} / {target_name} 标记为待核验/待补发。", "warning")
        else:
            flash(request, f"{target_name} 没有可重置或不是今日记录，未自动重置。", "info")
        return redirect("/ops/send-console")

    @app.post("/config")
    async def save_runtime_config(request: Request):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        def mutate(config):
            apply_runtime_config_form(config, form)
            return None, True

        update_config(mutate, force_reload=True)

        flash(request, "Runtime config saved.", "success")
        return redirect("/settings#messages")

    @app.post("/settings")
    async def save_panel_settings(request: Request):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        settings = get_app_settings(force_reload=True)
        settings["compose_root"] = str(form.get("compose_root", settings.get("compose_root", ""))).strip()
        settings["ops_log_file"] = str(form.get("ops_log_file", settings.get("ops_log_file", ""))).strip()
        settings["proxy_refresh_script"] = str(form.get("proxy_refresh_script", settings.get("proxy_refresh_script", ""))).strip()
        settings["login_desktop_api_url"] = str(
            form.get("login_desktop_api_url", settings.get("login_desktop_api_url", "http://127.0.0.1:18090"))
        ).strip()
        network_mode = str(form.get("douyin_network_mode", settings.get("douyin_network_mode", "direct"))).strip().lower()
        settings["douyin_network_mode"] = network_mode if network_mode in {"direct", "mihomo"} else "direct"
        settings["douyin_proxy_url"] = str(form.get("douyin_proxy_url", settings.get("douyin_proxy_url", "http://proxy:7890"))).strip()
        settings["ui_port"] = int(form.get("ui_port", settings.get("ui_port", 8787)))
        save_app_settings(settings)

        new_password = str(form.get("new_password", ""))
        confirm_password = str(form.get("confirm_password", ""))
        if new_password:
            if new_password != confirm_password:
                flash(request, "Admin password was not updated because the confirmation did not match.", "error")
                return redirect("/settings#system")
            problem = password_problem(new_password, settings.get("admin_username", "admin"))
            if problem:
                flash(request, f"管理员密码未修改：{problem}", "error")
                return redirect("/settings#system")
            update_admin_password(new_password)

        flash(request, "Panel settings saved.", "success")
        return redirect("/settings#system")

    @app.post("/settings/notifications")
    async def save_notifications(request: Request):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        def mutate(config):
            normalized, errors = notify.apply_form(config.get("notifications"), form)
            if errors:
                return errors, False
            config["notifications"] = normalized
            return errors, True

        errors = update_config(mutate, force_reload=True)
        if errors:
            message = "推送设置未保存：" + "；".join(errors)
            flash(request, message, "error")
        else:
            message = "推送设置已保存。"
            flash(request, message, "success")
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            request.session.pop("flash", None)
            return JSONResponse({"message": message, "level": "error" if errors else "success"})
        return redirect("/settings#notify")

    @app.post("/settings/notifications/test")
    async def test_notification_channel(request: Request):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        channel = str(form.get("channel", "")).strip()
        if channel not in notify.CHANNEL_NAMES:
            flash(request, "未知推送渠道，测试没有发送。", "error")
            return redirect("/settings#notify")

        # Test uses the saved config, so unsaved edits are not sent.
        saved = get_config(force_reload=True).get("notifications")
        ok, message = await asyncio.to_thread(notify.send_test, channel, saved)
        level = "success" if ok else "error"
        flash(request, message, level)
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            request.session.pop("flash", None)
            return JSONResponse({"message": message, "level": level})
        return redirect("/settings#notify")

    @app.post("/ops/run-now")
    async def run_now(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        refs = principal_account_refs(request)
        pid = run_task_now(force_all=refs is None, account_refs=refs)
        if pid == TASK_ALREADY_RUNNING:
            flash(request, "已有发送任务正在运行，本次补发全部对象没有启动。请等当前任务结束后再试。", "warning")
        elif pid == -1:
            flash(request, "Failed to start the full resend run. Check server logs for details.", "error")
        else:
            flash(request, f"已启动补发全部对象后台任务（pid {pid}）。这只表示任务已启动，实际成功数请刷新发送控制台查看。", "info")
        return redirect("/ops/send-console")

    @app.post("/ops/run-failed")
    async def run_failed_retry(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        refs = principal_account_refs(request)
        pid = run_failed_retry_now(account_refs=refs)
        if pid == TASK_ALREADY_RUNNING:
            flash(request, "已有发送任务正在运行，本次补发未成功目标没有启动。请等当前任务结束后再试。", "warning")
        elif pid == -1:
            flash(request, "Failed to start the failed-target retry run. Check server logs for details.", "error")
        else:
            flash(request, f"已启动补发未成功目标后台任务（pid {pid}）。这只表示任务已启动，实际成功数请刷新发送控制台查看。", "info")
        return redirect("/ops/send-console")

    @app.post("/ops/run-unsent")
    async def run_unsent_retry(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        refs = principal_account_refs(request)
        pid = run_unsent_retry_now(account_refs=refs)
        if pid == TASK_ALREADY_RUNNING:
            flash(request, "A send task is already running; unsent retry was not started.", "warning")
        elif pid == -1:
            flash(request, "Failed to start the unsent-target retry run. Check server logs for details.", "error")
        else:
            flash(request, f"Started unsent-target retry background task (pid {pid}). Refresh the send console for results.", "info")
        return redirect("/ops/send-console")

    @app.post("/ops/proxy/refresh")
    async def proxy_refresh(request: Request):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        refresh_proxy()
        flash(request, "Proxy subscription refreshed.", "success")
        return redirect("/settings#ops")

    @app.post("/ops/proxy/restart")
    async def proxy_restart(request: Request):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        restart_proxy()
        flash(request, "Proxy container restarted.", "success")
        return redirect("/settings#ops")

    @app.post("/ops/schedule")
    async def save_schedule(request: Request):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return maybe_redirect

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return Response("Invalid CSRF token", status_code=403)

        time_string = str(form.get("daily_schedule", "")).strip()
        window_state = schedule_window_state()
        if window_state.get("inside"):
            # Freeze the window while today's window is running: changing it
            # mid-window would silently retime the rest of the day.
            flash(
                request,
                f"现在是发送窗口 {window_state.get('label')} 内，窗口配置在窗口结束（{int(window_state.get('endHour') or 0):02d}:00）之前不可修改。",
                "error",
            )
            return redirect("/settings#schedule")
        result = update_daily_schedule(time_string)
        if getattr(result, "returncode", 1) == 0:
            flash(request, f"Updated the daily schedule to {time_string}.", "success")
        else:
            flash(request, f"Failed to update the daily schedule to {time_string}: {getattr(result, 'stderr', '')}", "error")
        return redirect("/settings#schedule")

    @app.post("/ops/schedule/preview")
    async def preview_schedule(request: Request):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)

        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)

        time_string = str(form.get("daily_schedule", "")).strip()
        try:
            preview = preview_daily_schedule(time_string)
        except Exception as exc:
            logger.warning("Schedule preview failed", exc_info=True)
            return JSONResponse(
                {"ok": False, "error": f"预览失败：{exc}"},
                status_code=500,
            )
        return JSONResponse(preview)

    @app.get("/ops/logs", response_class=HTMLResponse)
    async def logs_page(request: Request):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return maybe_redirect
        return render_template(
            request,
            "logs.html",
            {
                "flash": pop_flash(request),
                "log_tail": read_log_tail(400),
                "log_summary": summarize_log_tail(400),
            },
        )

    @app.get("/ops/logs/download")
    async def logs_download(request: Request):
        maybe_redirect = require_admin(request)
        if maybe_redirect:
            return maybe_redirect
        path = log_file_path()
        if not path.is_file():
            return PlainTextResponse(
                "日志文件不存在或尚未产生（定时触发写入任务日志后会生成）。",
                status_code=404,
            )
        return FileResponse(
            str(path),
            media_type="text/plain; charset=utf-8",
            filename=path.name,
            headers={"Cache-Control": "no-store"},
        )

    login_transition_lock = asyncio.Lock()

    def _workspace_payload(request):
        current = principal(request)
        state = get_workspace_state()
        mine = workspace_status(
            username=current.get("username", "") if current else "",
            session_id=current.get("session_id", "") if current else "",
        )
        active = state.get("active") or {}
        is_owner = bool(current and owns_login_lock(
            active,
            username=current.get("username", ""),
            session_id=current.get("session_id", ""),
        ))
        return {
            "state": mine.get("state", "closed"),
            "position": mine.get("position", 0),
            "ticket": mine.get("ticket", ""),
            "remaining_seconds": mine.get("remaining_seconds", 0),
            "queue_length": len(state.get("queue") or []),
            "active": is_owner,
            "active_username": active.get("username", "") if current and current.get("role") == "admin" else (current.get("username", "") if is_owner and current else ""),
        }

    async def _reset_and_promote(*, force=False, clear_queue=False):
        """Reset the shared browser profile, then activate the next queue item."""
        async with login_transition_lock:
            if force:
                transition = begin_login_force_reset(clear_queue=clear_queue)
            else:
                transition = begin_login_expiration()
            state_after = get_workspace_state()
            needs_reset = bool(transition or state_after.get("phase") == "resetting")
            if not needs_reset:
                return True, None
            try:
                try:
                    await asyncio.to_thread(call_login_desktop, "/close", method="POST", payload={}, timeout=60)
                except RuntimeError:
                    # Older login-desktop images do not have /close; reset is
                    # still safe because it clears the temporary login profile.
                    await asyncio.to_thread(call_login_desktop, "/reset", method="POST", payload={}, timeout=120)
            except RuntimeError as exc:
                logger.error("Failed to reset login workspace: %s", exc)
                return False, None
            promoted = finish_login_transition()
            if promoted:
                try:
                    await asyncio.to_thread(call_login_desktop, "/open-login", method="POST", payload={}, timeout=90)
                except RuntimeError as exc:
                    logger.error("Failed to open login workspace for queued user: %s", exc)
                    return False, promoted
            return True, promoted

    async def _expire_login_workspace():
        return await _reset_and_promote()

    release_tasks = set()

    def _reset_and_promote_later():
        """Release the workspace without making the leaving operator wait.

        Resetting the login browser and opening it for the next queued person
        takes seconds to tens of seconds; the save or close that triggered it
        has already finished and must answer now. The watchdog retries a reset
        that fails here.
        """
        task = asyncio.create_task(_reset_and_promote())
        release_tasks.add(task)

        def finished(done):
            release_tasks.discard(done)
            if not done.cancelled() and done.exception():
                logger.error("Background login workspace release failed", exc_info=done.exception())

        task.add_done_callback(finished)

    async def login_workspace_watchdog():
        """Reap abandoned leases even when no browser request arrives."""
        while True:
            await asyncio.sleep(5)
            try:
                await _expire_login_workspace()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("login workspace watchdog failed")

    def login_lock_owner(request):
        current = principal(request)
        active = get_login_lock()
        if not current or not active:
            return current, active, False
        return current, active, owns_login_lock(
            active,
            username=current["username"],
            session_id=current.get("session_id", ""),
        )

    def login_lock_required(request, *, api=False):
        current, active, allowed = login_lock_owner(request)
        if allowed:
            return None
        if api:
            return JSONResponse({"ok": False, "error": "登录工作区当前未由本会话占用", "workspace": _workspace_payload(request)}, status_code=423)
        return HTMLResponse(
            """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>等待登录工作区</title>
            <body style='font-family:sans-serif;padding:32px'><h2>登录工作区尚未分配</h2>
            <p>请返回账号管理，点击对应抖音账号的“重新登录”。如果前面有其他用户，页面会自动排队等待。</p></body></html>""",
            status_code=423,
        )

    @app.get("/login-desktop/proxy")
    async def login_desktop_proxy_root(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect
        lock_error = login_lock_required(request)
        if lock_error:
            return lock_error
        return RedirectResponse(login_desktop_public_url(request), status_code=307)

    @app.get("/login-desktop/proxy/{asset_path:path}")
    async def login_desktop_proxy_asset(request: Request, asset_path: str):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect
        lock_error = login_lock_required(request)
        if lock_error:
            return lock_error
        try:
            status, headers, content = await asyncio.to_thread(
                fetch_login_desktop_asset,
                asset_path,
                request.url.query,
            )
            return Response(content=content, status_code=status, headers=headers)
        except RuntimeError as exc:
            return PlainTextResponse(str(exc), status_code=502)

    @app.websocket("/login-desktop/proxy/websockify")
    async def login_desktop_proxy_websocket(websocket: WebSocket):
        current = current_principal(websocket)
        active = get_login_lock()
        if not current:
            await websocket.close(code=4401)
            return
        if not owns_login_lock(
            active,
            username=current.get("username", ""),
            session_id=current.get("session_id", ""),
        ):
            await websocket.close(code=4423)
            return

        requested_protocols = [
            item.strip()
            for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if item.strip()
        ]
        accepted = False
        try:
            async with websockets.connect(
                login_desktop_novnc_ws_url(),
                subprotocols=requested_protocols or None,
                open_timeout=10,
                close_timeout=5,
            ) as upstream:
                await websocket.accept(subprotocol=upstream.subprotocol)
                accepted = True

                async def client_to_upstream():
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        if message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send(message["text"])

                async def upstream_to_client():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                await _run_websocket_relays(client_to_upstream(), upstream_to_client())
        except (ConnectionClosed, WebSocketDisconnect):
            pass
        except Exception as exc:
            logger.warning("login desktop WebSocket proxy failed: %s", exc)
            if not accepted:
                await websocket.close(code=1011)

    @app.get("/login-desktop/qr")
    async def login_desktop_qr(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return maybe_redirect
        lock_error = login_lock_required(request)
        if lock_error:
            return lock_error
        # The login service holds the request until a QR code shows up (bounded),
        # so the page does not have to poll in a tight loop.
        url = f"{login_desktop_api_url()}/qr?wait={LOGIN_QR_WAIT_SECONDS}"
        try:
            upstream_request = urllib.request.Request(
                url,
                method="GET",
                headers=login_desktop_api_headers(),
            )
            def read_qr_response():
                upstream = urllib.request.urlopen(upstream_request, timeout=20)
                try:
                    raw_headers = getattr(upstream, "headers", {})
                    try:
                        headers = dict(raw_headers)
                    except (TypeError, ValueError):
                        headers = {}
                    return getattr(upstream, "status", 200), headers, upstream.read()
                finally:
                    close = getattr(upstream, "close", None)
                    if close:
                        close()
            upstream_status, upstream_headers, content = await asyncio.to_thread(read_qr_response)
            upstream_content_type = _header_value(upstream_headers, "Content-Type")
            if upstream_status == 202 or "application/json" in upstream_content_type:
                # JSON bodies carry a machine-readable QR state (not-generated,
                # page busy, existing session). Forwarding them as image/png
                # would show the browser a broken image instead of the reason.
                retry_after = _header_value(upstream_headers, "Retry-After") or "2"
                return _qr_state_response(
                    content,
                    state="starting",
                    status_code=upstream_status,
                    retry_after=retry_after,
                )
            if upstream_status in {409, 503}:
                # Upstream already classified the reason; forward it instead of
                # collapsing every failure into "service unavailable".
                return _qr_state_response(content, state="retrying", status_code=upstream_status)
            return Response(content=content, media_type="image/png", headers={"Cache-Control": "no-store, max-age=0"})
        except urllib.error.HTTPError as exc:
            if exc.code in {202, 409, 503}:
                return _qr_state_response(
                    _safe_json_bytes(exc),
                    state="retrying",
                    status_code=exc.code,
                    retry_after=_header_value(exc.headers, "Retry-After") or "2",
                )
            # login-desktop answered but could not produce a page: that is the
            # upstream (Douyin / network) side, not the login service itself.
            return JSONResponse(
                {
                    "ok": False,
                    "category": "douyin_unreachable",
                    "message": "无法访问抖音（网络或代理异常），请检查服务器网络后点击“刷新二维码”重试。",
                },
                status_code=502,
                headers={"Cache-Control": "no-store"},
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
                # login-desktop is reachable but its single page lock is held
                # (status checks, page load); that is "busy", worth retrying,
                # not "the service is down".
                return JSONResponse(
                    {
                        "ok": False,
                        "state": "busy",
                        "category": "page_busy",
                        "message": "登录页正在加载或处理其他请求",
                        "retry_after": 3,
                    },
                    status_code=503,
                    headers={"Cache-Control": "no-store", "Retry-After": "3"},
                )
            return JSONResponse(
                {
                    "ok": False,
                    "category": "service_unavailable",
                    "message": "登录桌面服务暂时不可用，请稍后点击“刷新二维码”重试。",
                },
                status_code=502,
                headers={"Cache-Control": "no-store"},
            )

    def _header_value(headers, name):
        """Read a response header case-insensitively (HTTP header names are)."""
        if not headers:
            return ""
        target = str(name).lower()
        try:
            for key, value in headers.items():
                if str(key).lower() == target:
                    return str(value)
        except (AttributeError, TypeError):
            return ""
        try:
            return str(headers.get(name) or "")
        except (AttributeError, TypeError):
            return ""

    def _safe_json_bytes(exc):
        try:
            return exc.read()
        except Exception:
            return b"{}"

    def _qr_state_response(content, *, state, status_code, retry_after=None):
        payload = {}
        try:
            decoded = json.loads(content.decode("utf-8"))
            if isinstance(decoded, dict):
                payload = decoded
        except (ValueError, UnicodeDecodeError, AttributeError):
            payload = {}
        payload.setdefault("ok", False)
        payload.setdefault("state", state)
        # Upstream already classifies which QR states are retryable; an expired
        # QR code is not, because only an explicit refresh regenerates it.
        retryable = bool(payload.get("retryable", status_code in {202, 503}))
        payload["retryable"] = retryable
        delay = int(retry_after or payload.get("retry_after") or 2)
        payload["retry_after"] = delay
        return JSONResponse(
            payload,
            status_code=status_code,
            headers={"Retry-After": str(delay), "Cache-Control": "no-store"},
        )

    @app.post("/login-desktop/qr/refresh")
    async def login_desktop_qr_refresh(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        lock_error = login_lock_required(request, api=True)
        if lock_error:
            return lock_error
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)
        heartbeat_login(
            username=principal(request)["username"],
            session_id=principal(request).get("session_id", ""),
            ticket=str(form.get("ticket", "")),
        )
        try:
            status, payload = await asyncio.to_thread(
                call_login_desktop_json,
                "/refresh-qr",
                method="POST",
                payload={},
                timeout=90,
            )
        except RuntimeError as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "state": "qr_service_unavailable",
                    "error": str(exc),
                    "message": "登录桌面服务暂时不可用，请稍后重试",
                    "retryable": True,
                    "retry_after": 3,
                },
                status_code=503,
                headers={"Retry-After": "3", "Cache-Control": "no-store"},
            )
        if status >= 400 or not payload.get("ok", False):
            # 202-qr_not_ready and any ok:false payload are real failures; the UI
            # must not report a successful refresh when no QR code was produced.
            message = str(payload.get("message") or payload.get("error") or "二维码尚未生成，请稍后重试")
            delay = int(payload.get("retry_after") or 2)
            return JSONResponse(
                {
                    **payload,
                    "ok": False,
                    "state": payload.get("state") or payload.get("code") or "qr_not_ready",
                    "error": message,
                    "message": message,
                    "retryable": bool(payload.get("retryable", status == 503)),
                    "retry_after": delay,
                },
                status_code=status if status >= 400 else 202,
                headers={"Retry-After": str(delay), "Cache-Control": "no-store"},
            )
        return JSONResponse({"ok": True, "result": payload, "workspace": _workspace_payload(request)})

    @app.post("/login-desktop/focus")
    async def login_desktop_focus(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        lock_error = login_lock_required(request, api=True)
        if lock_error:
            return lock_error
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)
        heartbeat_login(
            username=principal(request)["username"],
            session_id=principal(request).get("session_id", ""),
            ticket=str(form.get("ticket", "")),
        )
        try:
            payload = call_login_desktop("/focus", method="POST", payload={}, timeout=20)
            return JSONResponse({"ok": True, "result": payload, "workspace": _workspace_payload(request)})
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)

    @app.get("/login-desktop/status")
    async def login_desktop_status(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        current = principal(request)
        if current.get("role") != "admin":
            lock_error = login_lock_required(request, api=True)
            if lock_error:
                return lock_error
        await _expire_login_workspace()
        try:
            payload = call_login_desktop("/status")
            payload["public_url"] = login_desktop_public_url(request)
            payload["workspace"] = _workspace_payload(request)
            return JSONResponse(payload)
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc), "public_url": login_desktop_public_url(request), "workspace": _workspace_payload(request)}, status_code=503)

    @app.get("/login-desktop/workspace-status")
    async def login_desktop_workspace_status(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        await _expire_login_workspace()
        return JSONResponse({"ok": True, "workspace": _workspace_payload(request)}, headers={"Cache-Control": "no-store"})

    @app.post("/login-desktop/heartbeat")
    async def login_desktop_heartbeat(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)
        await _expire_login_workspace()
        current = principal(request)
        ok = heartbeat_login(
            username=current["username"],
            session_id=current.get("session_id", ""),
            ticket=str(form.get("ticket", "")),
        )
        if not ok:
            return JSONResponse({"ok": False, "error": "登录工作区已释放，请重新申请"}, status_code=423)
        return JSONResponse({"ok": True, "workspace": _workspace_payload(request)})

    @app.post("/login-desktop/open")
    async def login_desktop_open(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)
        await _expire_login_workspace()
        current = principal(request)
        relogin_unique_id = str(form.get("relogin_unique_id", "")).strip()
        requested_mode = str(form.get("mode", "")).strip().lower()
        mode = requested_mode if requested_mode in {"add", "relogin"} else ("relogin" if relogin_unique_id else "add")
        account_ref = ""
        if relogin_unique_id:
            _, account, access_error = account_for_request(request, relogin_unique_id)
            if access_error:
                return JSONResponse({"ok": False, "error": "无权操作该账号"}, status_code=access_error.status_code)
            account_ref = account.get("account_ref", "")
            mode = "relogin"
        elif mode != "add":
            return JSONResponse({"ok": False, "error": "重新登录已有账号时必须选择账号"}, status_code=400)

        result = request_workspace(
            username=current["username"],
            session_id=current.get("session_id", ""),
            account_ref=account_ref,
            mode=mode,
        )
        if result["state"] == "full":
            return JSONResponse({"ok": False, "error": "登录排队人数已满，请稍后重试"}, status_code=429)
        if result["state"] == "queued":
            return JSONResponse({"ok": True, "state": "queued", "workspace": _workspace_payload(request)}, status_code=202)
        try:
            # Off the event loop: opening takes seconds and must not stall other requests.
            await asyncio.to_thread(call_login_desktop, "/open-login", method="POST", payload={}, timeout=90)
            return JSONResponse({"ok": True, "state": "active", "public_url": login_desktop_public_url(request), "workspace": _workspace_payload(request)})
        except RuntimeError as exc:
            begin_login_release(username=current["username"], session_id=current.get("session_id", ""), ticket=result["request"].get("ticket", ""), account_ref=account_ref)
            await _reset_and_promote()
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)

    @app.post("/login-desktop/close")
    async def login_desktop_close(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)
        current = principal(request)
        if current.get("role") == "admin":
            await _reset_and_promote(force=True)
            return JSONResponse({"ok": True, "workspace": _workspace_payload(request)})
        active = get_login_lock()
        if owns_login_lock(active, username=current["username"], session_id=current.get("session_id", "")):
            begin_login_release(
                username=current["username"],
                session_id=current.get("session_id", ""),
                ticket=active.get("ticket", ""),
                account_ref=active.get("account_ref", ""),
            )
            _reset_and_promote_later()
        else:
            cancel_login_request(username=current["username"], session_id=current.get("session_id", ""))
        return JSONResponse({"ok": True, "workspace": _workspace_payload(request)})

    @app.post("/login-desktop/release")
    async def login_desktop_release(request: Request):
        """Owner-only, non-forcing release used when the page goes away.

        Unlike /close this must never force-reset the shared workspace: an admin
        tab closing is not an admin asking to reset everyone's session.
        """
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)
        current = principal(request)
        active = get_login_lock()
        if not owns_login_lock(
            active,
            username=current.get("username", ""),
            session_id=current.get("session_id", ""),
        ):
            return JSONResponse({"ok": True, "released": False})
        ticket = str(form.get("ticket", "")).strip()
        if ticket and str(active.get("ticket", "")) != ticket:
            return JSONResponse({"ok": True, "released": False})
        begin_login_release(
            username=current["username"],
            session_id=current.get("session_id", ""),
            ticket=active.get("ticket", ""),
            account_ref=active.get("account_ref", ""),
        )
        await _reset_and_promote()
        return JSONResponse({"ok": True, "released": True})

    @app.post("/login-desktop/reset")
    async def login_desktop_reset(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)
        current = principal(request)
        if current.get("role") == "admin":
            await _reset_and_promote(force=True, clear_queue=str(form.get("clear_queue", "")) == "1")
            return JSONResponse({"ok": True, "workspace": _workspace_payload(request)})
        active = get_login_lock()
        if not owns_login_lock(active, username=current["username"], session_id=current.get("session_id", "")):
            kind, _ = cancel_login_request(username=current["username"], session_id=current.get("session_id", ""))
            return JSONResponse({"ok": kind == "queued", "workspace": _workspace_payload(request)})
        begin_login_release(username=current["username"], session_id=current.get("session_id", ""), ticket=active.get("ticket", ""), account_ref=active.get("account_ref", ""))
        await _reset_and_promote()
        return JSONResponse({"ok": True, "workspace": _workspace_payload(request)})

    @app.post("/login-desktop/clear-state")
    async def login_desktop_clear_state(request: Request):
        """Drop the shared persistent-profile login state (used after cookie login)."""
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)
        # Clearing the shared profile while another session owns (or is about to
        # take) the login workspace would fight that session for the same browser.
        current = principal(request)
        if current.get("role") != "admin":
            active = get_login_lock()
            if not active or not owns_login_lock(
                active,
                username=current["username"],
                session_id=current.get("session_id", ""),
            ):
                return JSONResponse(
                    {"ok": False, "error": "登录工作区当前未由本会话占用"},
                    status_code=423,
                )
        try:
            payload = call_login_desktop("/clear-login-state", method="POST", payload={}, timeout=60)
            return JSONResponse({"ok": True, "result": payload})
        except RuntimeError as exc:
            logger.warning("Clearing the shared login state failed: %s", exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)

    @app.post("/accounts/cookies")
    async def login_with_cookies(request: Request):
        """Sign in by pasting an existing Cookie instead of scanning a QR code."""
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)

        current = principal(request)
        raw_cookies = str(form.get("cookie_input", ""))
        display_name = str(form.get("display_name", "")).strip()
        relogin_unique_id = str(form.get("relogin_unique_id", "")).strip()
        relogin_account_ref = ""

        if relogin_unique_id:
            _, account, access_error = account_for_request(request, relogin_unique_id)
            if access_error:
                return JSONResponse(
                    {"ok": False, "error": "无权操作该账号"},
                    status_code=access_error.status_code,
                )
            relogin_account_ref = str(account.get("account_ref", ""))
            relogin_unique_id = str(account.get("unique_id", ""))
        # The operator-chosen target (from the form), kept apart from a target a
        # one-time confirmation would introduce: a confirmation may never switch
        # the explicit target to another account (issue #41 A5).
        explicit_relogin_ref = relogin_account_ref

        try:
            cookies = parse_cookie_input(raw_cookies)
            require_auth_cookies(cookies)
        except CookieParseError as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "error": str(exc),
                    "category": getattr(exc, "category", "cookie_format_invalid"),
                    "retryable": False,
                },
                status_code=400,
            )

        # Snapshot the store before the network identity check; the save refuses
        # to write when the chosen record changed meanwhile (issue #41 A8).
        expected_records = {
            str(row.get("account_ref") or ""): account_identity.record_fingerprint(row)
            for row in get_userData(force_reload=True)
            if str(row.get("account_ref") or "").strip()
        }

        verified, verification_reason, identity, verification_category = await verify_login_result(
            {"cookies": cookies},
            relogin_account_ref=relogin_account_ref,
        )
        if not verified:
            # Reject without touching any account: the pasted state was not
            # usable, or whose account it is could not be confirmed yet.
            category = verification_category or CATEGORY_LOGIN_REQUIRED
            logger.warning(
                "Cookie login rejected for user=%s category=%s reason=%s",
                current.get("username", ""),
                category,
                verification_reason,
            )
            return JSONResponse(
                {
                    "ok": False,
                    "error": verification_reason or "Cookie 验证失败",
                    "category": category,
                    "categoryLabel": identity_category_label(category),
                    # A transport, page or identity-endpoint problem is worth
                    # retrying; a rejected Cookie is not.
                    "retryable": category != CATEGORY_LOGIN_REQUIRED,
                },
                status_code=400,
            )

        trusted = dict(identity.get("trusted") or {})
        uid = trusted.get("uid", "")
        display_id = identity.get("display_id", "")
        confirmation = None
        confirm_token = str(form.get("confirm_identity", "")).strip()
        if confirm_token:
            record, problem = _take_identity_confirmation(confirm_token, current, "cookies")
            if not record:
                return JSONResponse({"ok": False, "error": problem, "category": "identity_confirmation_invalid"}, status_code=409)
            confirmed_account = account_by_ref(get_userData(force_reload=True), record["account_ref"])
            if not confirmed_account or not may_claim_account(current, confirmed_account):
                return JSONResponse({"ok": False, "error": "无权操作该账号"}, status_code=403)
            if explicit_relogin_ref and explicit_relogin_ref != record["account_ref"]:
                # The confirmation is for a different account than the one the
                # operator explicitly chose; never repoint the target silently.
                return JSONResponse(
                    {"ok": False, "error": "确认的账号与本次重新登录的账号不一致"},
                    status_code=409,
                )
            relogin_account_ref = record["account_ref"]
            confirmation = {
                "account_ref": record["account_ref"],
                "fingerprint": record["fingerprint"],
                "uid": record["uid"],
                "candidate_fingerprint": record.get("candidate_fingerprint", ""),
                "confirmed_by": current.get("username", ""),
            }

        auto_matched = False
        if not relogin_account_ref and uid:
            saved_accounts = get_userData(force_reload=True)
            if identity_owned_by_other(saved_accounts, uid, current, display_id):
                return JSONResponse(
                    {"ok": False, "error": "这个抖音账号已经绑定给其他用户，不能覆盖"},
                    status_code=403,
                )
            rows, kind = rows_for_identity(saved_accounts, uid, display_id)
            existing = preferred_row(rows, current)
            if existing:
                if not can_access_account(current, existing):
                    # No other row for this Douyin account is owned, so the user
                    # who just proved control may claim this one in place.
                    logger.info(
                        "Cookie login adopts an unassigned account: platform_uid=%s account_ref=%s user=%s",
                        uid,
                        existing.get("account_ref", ""),
                        current.get("username", ""),
                    )
                relogin_account_ref = str(existing.get("account_ref", ""))
                auto_matched = kind == "legacy"

        legacy_check = None
        if relogin_account_ref:
            saved_accounts = get_userData(force_reload=True)
            target_row = account_by_ref(saved_accounts, relogin_account_ref)
            if target_row and unique_id_owned_by_other(saved_accounts, target_row.get("unique_id"), current):
                # Another user's row shares this record's key; the save's dedupe
                # must never take it (or its ownership) away.
                return JSONResponse(
                    {"ok": False, "error": "这个抖音账号已经绑定给其他用户，不能覆盖"},
                    status_code=403,
                )
            if target_row and not confirmation and not account_identity.account_uid(target_row):
                legacy_check = await asyncio.to_thread(legacy_identity_check, target_row)

        login_result = {
            "username": identity.get("username", ""),
            "display_id": display_id,
            "cookies": cookies,
            "cookie_source": "pasted",
        }
        try:
            account, action = save_exported_login_result(
                login_result,
                relogin_account_ref=relogin_account_ref,
                display_name=display_name,
                is_healthy=True,
                identity=trusted,
                legacy_check=legacy_check,
                confirmation=confirmation,
                auto_matched=auto_matched,
                expected_records=expected_records,
                principal_resolver=lambda: principal(request),
                expected_principal=current,
            )
        except LoginSavePermissionDenied as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        except IDENTITY_SAVE_ERRORS as exc:
            return _identity_error_response(exc, current, "cookies", display_id=display_id)
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        if current.get("role") == "user":
            update_web_user(current["username"], append_account_ref=account.get("account_ref", ""))

        try:
            call_login_desktop("/clear-login-state", method="POST", payload={}, timeout=60)
        except RuntimeError as exc:
            logger.warning("Cookie login saved but clearing the shared login state failed: %s", exc)

        summary = cookie_summary(cookies)
        logger.info(
            "Cookie login saved by user=%s action=%s cookies=%s auth=%s",
            current.get("username", ""),
            action,
            summary["count"],
            ",".join(summary["auth_names"]),
        )
        return JSONResponse(
            {
                "ok": True,
                "action": action,
                "message": f"Cookie 登录成功，已{'更新' if action == 'updated' else '添加'}账号 {account.get('username', '')}（{account_identity.IDENTITY_STATE_LABELS['verified']}）",
                "identityState": "verified",
                "account": {
                    "account_ref": account.get("account_ref"),
                    "unique_id": account.get("unique_id"),
                    "username": account.get("username"),
                    "platform_uid": account.get(account_identity.FIELD_UID, ""),
                    "display_id": account.get(account_identity.FIELD_DISPLAY_ID, ""),
                    "enabled": account.get("enabled", True),
                    "healthy": True,
                },
            }
        )

    @app.post("/login-desktop/save")
    async def login_desktop_save(request: Request):
        maybe_redirect = require_user(request)
        if maybe_redirect:
            return JSONResponse({"redirect": "/login"}, status_code=401)
        form = await request.form()
        if not validate_csrf(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"ok": False, "error": "Invalid CSRF token"}, status_code=403)

        current = principal(request)
        active = get_login_lock()
        if not owns_login_lock(active, username=current["username"], session_id=current.get("session_id", "")):
            return JSONResponse({"ok": False, "error": "登录工作区已释放，请重新申请"}, status_code=423)
        relogin_unique_id = str(form.get("relogin_unique_id", "")).strip()
        display_name = str(form.get("display_name", "")).strip()
        operation = str(active.get("mode", "relogin"))
        relogin_account_ref = str(active.get("account_ref", ""))
        if relogin_account_ref:
            account = account_by_ref(get_userData(force_reload=True), relogin_account_ref)
            if not account or not can_access_account(current, account):
                return JSONResponse({"ok": False, "error": "无权操作该账号"}, status_code=403)
            relogin_unique_id = account.get("unique_id", "")
            operation = "relogin"
        elif operation != "add" and current.get("role") != "admin":
            return JSONResponse({"ok": False, "error": "普通用户必须选择自己的抖音账号"}, status_code=400)
        # Snapshot the store before the export and the identity check: the save
        # refuses to write when the chosen record changed meanwhile (A8).
        expected_records = {
            str(row.get("account_ref") or ""): account_identity.record_fingerprint(row)
            for row in get_userData(force_reload=True)
            if str(row.get("account_ref") or "").strip()
        }
        try:
            # Off the event loop: exporting reads the live login page and must not
            # freeze every other request (noVNC, heartbeats) while it runs.
            payload = await asyncio.to_thread(call_login_desktop, "/export", method="POST", payload={}, timeout=30)
            if not payload.get("ok"):
                raise RuntimeError("login-desktop export did not return ok")
            exported = payload.get("result", {}) or {}
            display_id = account_identity.display_id_from_text(
                exported.get("display_id") or ""
            )
            # The page-read ID may be the public handle or the platform UID;
            # only the authenticated self-identity response decides who this is.
            try:
                trusted = await asyncio.to_thread(
                    account_identity.fetch_trusted_identity,
                    list(exported.get("cookies") or []),
                )
            except account_identity.IdentityUnavailable as exc:
                # Keep the workspace: the operator can retry the save.
                return _identity_error_response(exc, current, "login-desktop", display_id=display_id)
            uid = trusted["uid"]
            rows, match_kind = rows_for_identity(
                get_userData(force_reload=True), uid, display_id
            )
            existing = preferred_row(rows, current)
            confirmation = None
            confirm_adopts = False
            confirm_token = str(form.get("confirm_identity", "")).strip()
            if confirm_token:
                record, problem = _take_identity_confirmation(confirm_token, current, "login-desktop")
                if not record:
                    return JSONResponse({"ok": False, "error": problem, "category": "identity_confirmation_invalid"}, status_code=409)
                candidate = account_by_ref(get_userData(force_reload=True), record["account_ref"])
                if not candidate or not may_claim_account(current, candidate):
                    return JSONResponse({"ok": False, "error": "无权操作该账号"}, status_code=403)
                confirm_adopts = not can_access_account(current, candidate)
                if relogin_account_ref and relogin_account_ref != record["account_ref"]:
                    return JSONResponse({"ok": False, "error": "确认的账号与本次重新登录的账号不一致"}, status_code=409)
                existing = candidate
                relogin_account_ref = record["account_ref"]
                relogin_unique_id = candidate.get("unique_id", "")
                operation = "relogin"
                confirmation = {
                    "account_ref": record["account_ref"],
                    "fingerprint": record["fingerprint"],
                    "uid": record["uid"],
                    "candidate_fingerprint": record.get("candidate_fingerprint", ""),
                    "confirmed_by": current.get("username", ""),
                }
            merge_with = str(form.get("merge_with", "")).strip()
            if merge_with and not confirmation:
                # The operator confirmed that a same-name account is the one they
                # are re-logging into, so update it instead of adding a duplicate.
                candidate = account_by_ref(get_userData(force_reload=True), merge_with)
                if candidate and can_access_account(current, candidate):
                    existing = candidate
                    relogin_account_ref = candidate.get("account_ref", "")
                    relogin_unique_id = candidate.get("unique_id", "")
                    operation = "relogin"
            # Read once, before the guard below: the audit line has to stay
            # reachable, and inside the guard `allow_duplicate == "1"` is always
            # false, which made the first version of this log dead code.
            allow_duplicate_requested = (
                str(form.get("allow_duplicate", "")).strip() == "1"
            )
            if allow_duplicate_requested and not existing:
                logger.info(
                    "Login save creates a separate account past a same-name match: platform_uid=%s",
                    uid,
                )
            # A chosen re-login target means the operator already picked which
            # account this login belongs to; it is still only written when the
            # identity check agrees.
            relogin_target_given = bool(
                str(form.get("relogin_unique_id", "")).strip() or merge_with or confirmation
            )
            if (
                not existing
                and not allow_duplicate_requested
                and not relogin_target_given
            ):
                # No account matched this login, so creating a row is the only
                # remaining outcome. Ask first if the nickname already exists:
                # the operator may be re-logging into a legacy record.
                # Fall back to the displayed ID when the export carries no
                # nickname, otherwise an empty name silently skips the check.
                exported_name = str(
                    exported.get("username") or display_id or ""
                ).strip()
                same_name = [
                    item
                    for item in find_same_name_account(
                        get_userData(force_reload=True), exported_name
                    )
                    if can_access_account(current, item)
                ]
                if same_name:
                    duplicate = same_name[0]
                    logger.info(
                        "Login save paused for confirmation: platform_uid=%s candidates=%s",
                        uid,
                        len(same_name),
                    )
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "已有同名账号，请确认是更新它还是新建",
                            "duplicate_candidate": {
                                "account_ref": duplicate.get("account_ref", ""),
                                "username": duplicate.get("username", ""),
                                "unique_id": duplicate.get("unique_id", ""),
                            },
                            # More than one candidate means the console cannot
                            # guess which row the operator means; the frontend
                            # refuses instead of merging into the wrong account.
                            "duplicate_candidates": [
                                {
                                    "account_ref": item.get("account_ref", ""),
                                    "username": item.get("username", ""),
                                    "unique_id": item.get("unique_id", ""),
                                }
                                for item in same_name
                            ],
                        },
                        status_code=409,
                    )
            # Confirming an unassigned legacy row claims it, like adopting it.
            adopt_unassigned = confirm_adopts
            if not relogin_account_ref and identity_owned_by_other(
                get_userData(force_reload=True), uid, current, display_id
            ):
                raise RuntimeError("这个抖音账号已经绑定给其他用户，不能覆盖")
            if existing and str(existing.get("account_ref", "")) != relogin_account_ref and not can_access_account(current, existing):
                # No other row for this Douyin account is owned, so the operator
                # who just proved control may claim this unassigned row in place.
                adopt_unassigned = True
                logger.info(
                    "Login save adopts an unassigned account: platform_uid=%s account_ref=%s user=%s",
                    uid,
                    existing.get("account_ref", ""),
                    current.get("username", ""),
                )
            if operation == "add" and current.get("role") == "user" and existing:
                relogin_account_ref = existing.get("account_ref", "")
                relogin_unique_id = existing.get("unique_id", "")
                operation = "relogin"
            # A row found only by a matching ID (not chosen by the operator) is
            # written only once a legacy row is proven or confirmed; if its own
            # cookies prove it is someone else, the login becomes a new account.
            auto_matched = bool(existing) and not relogin_target_given and match_kind == "legacy"
            if existing and not relogin_account_ref:
                relogin_account_ref = existing.get("account_ref", "")
            legacy_check = None
            if relogin_account_ref:
                saved_accounts = get_userData(force_reload=True)
                target_row = account_by_ref(saved_accounts, relogin_account_ref)
                if target_row and unique_id_owned_by_other(saved_accounts, target_row.get("unique_id"), current):
                    raise RuntimeError("这个抖音账号已经绑定给其他用户，不能覆盖")
                if target_row and not confirmation and not account_identity.account_uid(target_row):
                    legacy_check = await asyncio.to_thread(legacy_identity_check, target_row)
            # Only the instant checks run here. Re-reading the login state in a
            # second browser took minutes on this host, so it runs after the
            # save, in the background (D12); the account shows "verifying".
            verification_reason, verification_category = "", ""
            try:
                require_auth_cookies(list(exported.get("cookies") or []))
                verified = None
            except CookieParseError as exc:
                verified = False
                verification_reason = str(exc)
                verification_category = getattr(exc, "category", "cookie_format_invalid")
            verification_token = uuid.uuid4().hex if verified is None else ""
            try:
                account, action = save_exported_login_result(
                    exported,
                    relogin_unique_id=relogin_unique_id,
                    relogin_account_ref=relogin_account_ref,
                    display_name=display_name,
                    is_healthy=verified,
                    verification_reason=verification_reason,
                    verification_category=verification_category,
                    verification_token=verification_token,
                    identity=trusted,
                    legacy_check=legacy_check,
                    confirmation=confirmation,
                    auto_matched=auto_matched,
                    expected_records=expected_records,
                    principal_resolver=lambda: principal(request),
                    expected_principal=current,
                )
            except LoginSavePermissionDenied as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
            except IDENTITY_SAVE_ERRORS as exc:
                # Nothing was written and the workspace is kept for a retry.
                return _identity_error_response(exc, current, "login-desktop", display_id=display_id)
            normalized_saved = normalize_unique_id(account.get("unique_id"))
            friend_refresh_state = "queued" if verified is None else "skipped"
            # Without this trail a wrong match is invisible: a duplicate account
            # only shows up later as two same-name rows in the list.
            logger.info(
                "Login save: platform_uid=%s name_len=%s requested_uid=%s operation=%s action=%s verified=%s matched_before=%s",
                uid,
                len(str(exported.get("username") or "")),
                normalize_unique_id(relogin_unique_id) or "",
                operation,
                action,
                verified,
                bool(existing),
            )
            if (operation == "add" or adopt_unassigned) and current.get("role") == "user":
                update_web_user(current["username"], append_account_ref=account.get("account_ref", ""))
            begin_login_release(username=current["username"], session_id=current.get("session_id", ""), ticket=active.get("ticket", ""), account_ref=active.get("account_ref", ""))
            # The account is saved; do not hold the answer until the login
            # browser is reset and reopened for whoever is queued next.
            _reset_and_promote_later()
            if verification_token:
                _queue_login_verification(
                    account.get("account_ref", ""),
                    verification_token,
                    account_identity.account_login_generation(account),
                )
            return JSONResponse({
                "ok": True,
                "action": action,
                "verified": verified,
                "verification": "pending" if verified is None else ("passed" if verified else "failed"),
                "verification_error": verification_reason,
                "verification_category": verification_category,
                "friend_refresh": friend_refresh_state,
                "identityState": "verified",
                "identityLabel": account_identity.IDENTITY_STATE_LABELS["verified"],
                "account": {
                    "account_ref": account.get("account_ref"),
                    "unique_id": account.get("unique_id"),
                    "username": account.get("username"),
                    "platform_uid": account.get(account_identity.FIELD_UID, ""),
                    "display_id": account.get(account_identity.FIELD_DISPLAY_ID, ""),
                    "enabled": account.get("enabled", True),
                    "healthy": bool(verified),
                },
                "workspace": _workspace_payload(request),
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
