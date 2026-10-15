"""Trusted Douyin account identity (issue #41).

The creator home page shows either the public Douyin handle or the numeric
platform UID in the same place, and the old code stored whichever it read as
``unique_id``. Comparing those two forms flagged the same person as a different
account on every other re-login. This module keeps three things apart:

* the platform UID, read from an authenticated response of the creator IM
  self-identity endpoint; the only value used to decide "same account";
* the text the page displayed (handle or UID, type unknown), kept for display;
* the stored ``unique_id`` key, which only addresses an existing record.

Nothing here logs or returns cookie values or the IM token.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

IDENTITY_SOURCE = "creator_im_user_token"
IDENTITY_VERSION = 1
USER_TOKEN_URL = "https://creator.douyin.com/aweme/v1/creator/im/user_token/"
CREATOR_CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
FETCH_TIMEOUT_SECONDS = 15
MAX_UID_DIGITS = 32

CATEGORY_IDENTITY_UNVERIFIED = "identity_unverified"
CATEGORY_IDENTITY_CONFLICT = "account_identity_mismatch"

# Account fields owned by this module.
FIELD_UID = "platform_uid"
FIELD_BINDING = "identity_binding"
FIELD_DISPLAY_ID = "display_id"

BINDING_NEW = "new_account"
BINDING_SAME_UID = "same_uid"
BINDING_LEGACY_COOKIE = "legacy_cookie_match"
BINDING_CONFIRMED = "confirmed_migration"

IDENTITY_MARKER_KEYS = ("identity_mismatch", "account_identity_mismatch")

# Account field holding the login generation a background check belongs to.
FIELD_LOGIN_GENERATION = "login_generation"

# Flags a proven login of the same account resolves. Kept here (not in
# core.streak_state, which this module must not import) so the Web save and the
# CLI save clear exactly the same state.
LOGIN_HEALTH_FLAG_KEYS = (
    "account_health",
    "account_identity_mismatch",
    "identity_mismatch",
    "login_required",
    "needs_relogin",
    "pending_login_verification",
    # Written by the pre-window account patrol (core.patrol) when the page
    # proved the login unusable; a proven new login resolves it.
    "patrol_login_failure",
)

# Categories describing a login or identity problem (not a send problem). These
# are literal values so clearing health never imports core.streak_state.
LOGIN_RELATED_FAILURE_CATEGORIES = frozenset(
    {
        "login_required",
        "protocol_login_required",
        "account_identity_mismatch",
        "browser_login_required",
        "login_verification_pending",
        "expired_cookies",
        "missing_cookies",
        CATEGORY_IDENTITY_UNVERIFIED,
    }
)


def clear_login_health(account) -> None:
    """Remove the login/identity state a proven same-UID login resolves.

    Shared by the Web save and the CLI save (issue #41 A6). ``account_health``
    is only dropped when its category is a login/identity one, so a send failure
    or a rate-limit health record written after the login survives; a
    non-login ``account_failure`` record is left in place.
    """
    for key in LOGIN_HEALTH_FLAG_KEYS:
        if key != "account_health":
            account.pop(key, None)
    health = dict(account.get("account_health") or {})
    if str(health.get("category") or "") in LOGIN_RELATED_FAILURE_CATEGORIES:
        account.pop("account_health", None)
    failure = dict(account.get("account_failure") or {})
    if str(failure.get("category") or "") in LOGIN_RELATED_FAILURE_CATEGORIES:
        account.pop("account_failure", None)


def new_login_generation() -> str:
    """A fresh login generation id for one saved login."""
    return uuid.uuid4().hex


def account_login_generation(account) -> str:
    return str((account or {}).get(FIELD_LOGIN_GENERATION) or "")


class AccountChangedConcurrently(RuntimeError):
    """The record changed between the identity check and the write."""


def check_expected_records(accounts, expected_records) -> None:
    """Raise AccountChangedConcurrently when the store no longer matches a snapshot.

    ``expected_records`` maps ``account_ref`` to the fingerprint read before a
    network identity check. Called inside the data lock, before any write, so a
    record deleted or modified meanwhile aborts the whole save instead of
    half-writing. ``None`` means the caller did not take a snapshot (direct
    helper use) and skips the check.
    """
    if expected_records is None:
        return
    by_ref = {}
    for account in accounts or []:
        by_ref.setdefault(str(account.get("account_ref") or ""), account)
    for ref, fingerprint in expected_records.items():
        account = by_ref.get(str(ref))
        if account is None:
            raise AccountChangedConcurrently("账号记录在核实期间被删除，请重新保存")
        if record_fingerprint(account) != fingerprint:
            raise AccountChangedConcurrently("账号记录在核实期间被修改，请重新保存")


def _canonical_cookies(cookies) -> list:
    """Stable full-metadata JSON for each cookie dict (no field dropped)."""
    return sorted(
        json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        for item in cookies or []
        if isinstance(item, dict)
    )


def candidate_fingerprint(*, uid, username="", display_id="", display_name="", cookies=None) -> str:
    """Server-side digest of the full login candidate a confirmation is bound to.

    Covers the platform UID, the nickname/display parameters and every cookie
    dict with all of its metadata. The digest stays on the server; it is never
    sent to the client and never stands in for the UID as proof.
    """
    material = json.dumps(
        {
            "uid": str(uid or ""),
            "username": str(username or ""),
            "display_id": str(display_id or ""),
            "display_name": str(display_name or ""),
            "cookies": _canonical_cookies(cookies),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class IdentityUnavailable(RuntimeError):
    """The trusted UID could not be read; retry later, the login is not judged."""

    category = CATEGORY_IDENTITY_UNVERIFIED


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_valid_uid(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, str):
        return False
    return value.isascii() and value.isdigit() and 0 < len(value) <= MAX_UID_DIGITS and value != "0" * len(value)


def parse_user_token_response(text) -> str:
    """Return the exact UID string from a user_token response body.

    Integers are parsed as strings so a 16+ digit UID never passes through a
    float. Anything other than a successful status with a plain decimal UID is
    rejected.
    """
    try:
        data = json.loads(text or "", parse_int=str, parse_float=lambda raw: None)
    except ValueError as exc:
        raise IdentityUnavailable("身份接口返回的不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise IdentityUnavailable("身份接口返回格式无法识别")
    if str(data.get("status_code")) != "0":
        raise IdentityUnavailable("身份接口未返回成功状态")
    uid = data.get("user_id")
    if isinstance(uid, str):
        uid = uid.strip()
    if not is_valid_uid(uid):
        raise IdentityUnavailable("身份接口没有返回合法的账号 UID")
    return uid


def _cookie_header(cookies):
    parts = []
    for item in cookies or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or item.get("value") is None:
            continue
        parts.append(f"{name}={item.get('value')}")
    return "; ".join(parts)


def _cookie_value(cookies, *names):
    for wanted in names:
        for item in cookies or []:
            if isinstance(item, dict) and item.get("name") == wanted and item.get("value"):
                return str(item.get("value"))
    return ""


def _build_request(cookies):
    user_agent = (os.getenv("SPARKFLOW_PROTOCOL_USER_AGENT") or DEFAULT_USER_AGENT).strip()
    params = urllib.parse.urlencode(
        {
            "aid": "2906",
            "app_name": "aweme_creator_platform",
            "device_platform": "web",
            "referer": "",
            "user_agent": user_agent,
            "cookie_enabled": "true",
            "screen_width": "1280",
            "screen_height": "720",
            "browser_language": "en-US@posix",
            "browser_platform": "Linux x86_64",
            "browser_name": "Mozilla",
            "browser_version": user_agent,
            "browser_online": "true",
            "timezone_name": "Asia/Shanghai",
        }
    )
    headers = {
        "User-Agent": user_agent,
        "Referer": CREATOR_CHAT_URL,
        "Origin": "https://creator.douyin.com",
        "Accept": "application/json, text/javascript",
        "Cookie": _cookie_header(cookies),
        "x-tt-passport-csrf-token": _cookie_value(
            cookies, "passport_csrf_token", "passport_csrf_token_default"
        ),
    }
    return urllib.request.Request(f"{USER_TOKEN_URL}?{params}", headers=headers, method="GET")


def _default_routes():
    """(label, proxy-or-None) pairs in the same order the browser uses."""
    try:
        from core.browser import _douyin_browser_proxy, douyin_network_modes

        routes = []
        for mode in douyin_network_modes():
            proxy = _douyin_browser_proxy(network_mode=mode)
            if mode != "direct" and not proxy:
                continue
            routes.append((mode, proxy))
        return routes or [("direct", None)]
    except Exception:  # noqa: BLE001 - settings unreadable: try direct only
        return [("direct", None)]


class _NoIdentityRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # This request carries credentials: never forward them to a redirect.
        return None


def _open_once(request, proxy, timeout):
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler, _NoIdentityRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), response.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), ""


def fetch_trusted_identity(cookies, *, routes=None, opener=None, timeout=FETCH_TIMEOUT_SECONDS) -> dict:
    """Read the platform UID the given cookies are logged in as.

    Returns ``{"uid", "source", "version", "checkedAt"}``. Raises
    :class:`IdentityUnavailable` when no route yields a valid UID; the message
    never contains cookie values or response bodies.
    """
    if not _cookie_header(cookies):
        raise IdentityUnavailable("没有可用于核实身份的 Cookie")
    request = _build_request(cookies)
    open_once = opener or _open_once
    last_reason = "身份接口不可用"
    for label, proxy in routes if routes is not None else _default_routes():
        try:
            status, body = open_once(request, proxy, timeout)
        except Exception as exc:  # noqa: BLE001 - network failure on this route
            last_reason = f"身份接口连接失败（{label}）"
            logger.info("Trusted identity route=%s failed: %s", label, exc.__class__.__name__)
            continue
        if status >= 400:
            last_reason = f"身份接口返回 HTTP {status}"
            continue
        try:
            uid = parse_user_token_response(body)
        except IdentityUnavailable as exc:
            last_reason = str(exc)
            continue
        return {
            "uid": uid,
            "source": IDENTITY_SOURCE,
            "version": IDENTITY_VERSION,
            "checkedAt": _now_iso(),
        }
    raise IdentityUnavailable(f"暂时无法核实账号身份：{last_reason}")


def account_uid(account) -> str:
    """The trusted UID bound to a stored account, or "" for a legacy record."""
    account = account or {}
    uid = account.get(FIELD_UID)
    binding = account.get(FIELD_BINDING) or {}
    if is_valid_uid(uid) and binding.get("source") == IDENTITY_SOURCE:
        return uid
    return ""


def accounts_bound_to(accounts, uid):
    return [row for row in accounts or [] if uid and account_uid(row) == uid]


def bind_identity(account, identity, method, *, confirmed_by=""):
    account[FIELD_UID] = identity["uid"]
    binding = {
        "source": identity.get("source", IDENTITY_SOURCE),
        "version": int(identity.get("version", IDENTITY_VERSION)),
        "method": method,
        "boundAt": _now_iso(),
    }
    if confirmed_by:
        binding["confirmedBy"] = confirmed_by
    account[FIELD_BINDING] = binding


def clear_identity_markers(account):
    for key in IDENTITY_MARKER_KEYS:
        account.pop(key, None)


def record_fingerprint(account) -> str:
    """Hash of the fields that decide which account a record is.

    Server-side only. Covers the stored cookies so a concurrent re-login of the
    same record invalidates a pending comparison, but ignores health, friend
    caches and other fields background jobs touch.
    """
    account = account or {}
    material = json.dumps(
        {
            "ref": str(account.get("account_ref") or ""),
            "key": str(account.get("unique_id") or ""),
            "uid": str(account.get(FIELD_UID) or ""),
            "binding": account.get(FIELD_BINDING) or {},
            "cookies": _canonical_cookies(account.get("cookies")),
            "generation": account_login_generation(account),
            "username": str(account.get("username") or ""),
            "targets": [str(target) for target in account.get("targets") or []],
            "enabled": bool(account.get("enabled", True)),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def display_id_from_text(text) -> str:
    """The ID text the page showed, kept as-is (letters included) for display."""
    value = str(text or "").strip()
    for prefix in ("抖音号：", "抖音号:", "抖音号"):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    return value[:64]


def identity_state(account) -> str:
    """One of verified / conflict / unverified / legacy for the console."""
    account = account or {}
    if any(account.get(key) for key in IDENTITY_MARKER_KEYS):
        return "conflict"
    check = account.get("login_verification") or {}
    if check.get("category") == CATEGORY_IDENTITY_UNVERIFIED:
        return "unverified"
    if account_uid(account):
        return "verified"
    return "legacy"


IDENTITY_STATE_LABELS = {
    "verified": "身份已核实",
    "conflict": "身份冲突",
    "unverified": "身份暂时无法核实",
    "legacy": "身份待核实",
    "pending_confirmation": "待一次确认",
}

IDENTITY_STATE_HINTS = {
    "verified": "已按平台 UID 核实，重新登录同一账号不会被误判。",
    "conflict": "本次登录的平台 UID 与这条账号不一致：请用原账号重新扫码，或把新账号另行添加。",
    "unverified": "登录态可用，但暂时取不到平台 UID，稍后会再核实；定时发送不受影响。",
    "legacy": "旧记录尚未绑定平台 UID，下次重新登录时会自动核实。",
    "pending_confirmation": "旧记录无法自动核实，需要你确认本次登录的是原账号。",
}
