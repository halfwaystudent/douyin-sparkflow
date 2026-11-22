"""Cookie input parsing for WebUI cookie login.

Accepts the three shapes users actually have on hand:

* a browser-extension JSON array (``[{name, value, domain, ...}]``),
* a raw request-header string (``a=1; b=2``),
* a single ``name=value`` pair,

and normalizes them into the Playwright cookie dicts the account store and
:mod:`core.friends` / :mod:`core.tasks` already consume.

Cookie values are credentials: callers must never log or echo them.
"""

from __future__ import annotations

import json
import math
import re
import time

from core.streak_state import AUTH_COOKIE_NAMES

DEFAULT_COOKIE_DOMAIN = ".douyin.com"
DEFAULT_COOKIE_PATH = "/"
CREATOR_COOKIE_DOMAIN = ".creator.douyin.com"

_MAX_INPUT_LENGTH = 200_000
_MAX_COOKIES = 200

_PAIR_RE = re.compile(
    r"^\s*(?P<name>[^\s=;]+)\s*=\s*(?P<value>[^;]*)\s*$",
    re.UNICODE,
)


class CookieParseError(ValueError):
    """Raised when pasted cookie input cannot be turned into cookies."""

    def __init__(self, message, *, category="cookie_format_invalid"):
        super().__init__(message)
        self.category = category


def _strip_quotes(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def normalize_cookie(entry):
    """Normalize one cookie mapping into the stored cookie structure."""
    if not isinstance(entry, dict):
        raise CookieParseError("cookie entries must be objects with name and value")

    name = str(entry.get("name") or "").strip()
    if not name:
        raise CookieParseError("cookie entry is missing its name")
    if "value" not in entry:
        raise CookieParseError("Cookie 条目缺少 value 字段。")
    value = str(entry.get("value") or "")

    domain = str(entry.get("domain") or "").strip()
    host_only = entry.get("hostOnly")
    if not domain:
        domain = DEFAULT_COOKIE_DOMAIN
    if host_only is True:
        domain = domain.lstrip(".")
    elif not domain.startswith(".") and "." in domain:
        domain = f".{domain}"

    path = str(entry.get("path") or "").strip() or DEFAULT_COOKIE_PATH
    if not path.startswith("/"):
        path = DEFAULT_COOKIE_PATH

    cookie = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
    }

    expiry = entry.get("expires")
    if expiry is None:
        expiry = entry.get("expirationDate")
    if expiry is None:
        expiry = entry.get("expiry")
    if expiry is None:
        expiry = entry.get("expiresAt")
    try:
        expires = float(expiry) if expiry is not None else -1
    except (TypeError, ValueError):
        expires = -1
    cookie["expires"] = expires if math.isfinite(expires) and expires >= 0 else -1

    for flag in ("httpOnly", "secure"):
        if flag in entry:
            cookie[flag] = bool(entry.get(flag))
    same_site = entry.get("sameSite")
    if isinstance(same_site, str) and same_site.strip():
        # Extension exports use unspecified when no SameSite attribute exists.
        # Omitting it preserves browser defaults; this is not SameSite=None.
        if same_site.strip().casefold() == "unspecified":
            return cookie
        normalized_same_site = {
            "lax": "Lax",
            "strict": "Strict",
            "none": "None",
            "no_restriction": "None",
        }.get(same_site.strip().casefold())
        if normalized_same_site is None:
            raise CookieParseError(
                "Cookie 条目的 sameSite 格式不受支持，请重新导出 JSON。",
                category="cookie_format_invalid",
            )
        cookie["sameSite"] = normalized_same_site
    return cookie


def _parse_json_cookies(text):
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict):
        for key in ("cookies", "cookie"):
            nested = payload.get(key)
            if isinstance(nested, list):
                payload = nested
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise CookieParseError("JSON cookie input must be an array of cookie objects")
    if not payload:
        raise CookieParseError("JSON cookie input is an empty array")
    return payload


def _parse_header_cookies(text):
    stripped = text.strip()
    if stripped.lower().startswith("cookie:"):
        stripped = stripped.split(":", 1)[1]
    entries = []
    for chunk in stripped.split(";"):
        if not chunk.strip():
            continue
        match = _PAIR_RE.match(chunk)
        if not match:
            raise CookieParseError(
                "cookie string must look like name=value pairs separated by ';'"
            )
        entries.append(
            {
                "name": match.group("name").strip(),
                "value": _strip_quotes(match.group("value")),
            }
        )
    if not entries:
        raise CookieParseError("cookie string does not contain any name=value pair")
    return entries


def parse_cookie_input(raw):
    """Parse pasted cookie input into normalized cookie dicts."""
    if raw is None:
        raise CookieParseError("cookie input is empty")
    text = str(raw).strip()
    if not text:
        raise CookieParseError("cookie input is empty")
    if len(text) > _MAX_INPUT_LENGTH:
        raise CookieParseError("cookie input is too large")

    entries = None
    if text[0] in "[{":
        entries = _parse_json_cookies(text)
        if entries is None:
            raise CookieParseError("JSON 格式有误，请重新导出并粘贴完整的 Cookie-Editor JSON。")
    elif text.startswith("# Netscape HTTP Cookie File") or any(line.count("\t") >= 6 for line in text.splitlines()):
        raise CookieParseError("请在 Cookie-Editor 中选择 JSON 或 Header 导出；这里不接收 Netscape 文本。")
    if entries is None:
        if "=" not in text:
            raise CookieParseError(
                "cookie input must contain name=value pairs; paste the JSON export "
                "or the Cookie request header"
            )
        entries = _parse_header_cookies(text)

    by_key = {}
    now = time.time()
    for entry in entries:
        cookie = normalize_cookie(entry)
        key = (cookie["name"], cookie["domain"], cookie["path"])
        previous = by_key.get(key)
        if previous is None or (_expired(previous, now) and not _expired(cookie, now)):
            by_key[key] = cookie
    if not by_key:
        raise CookieParseError("cookie input does not contain any usable cookie")
    return list(by_key.values())[:_MAX_COOKIES]


def auth_cookie_names(cookies):
    """Return the authentication cookie names present in ``cookies``."""
    names = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        if name in AUTH_COOKIE_NAMES and name not in names:
            names.append(name)
    return names


def _expired(cookie, now):
    try:
        value = float(cookie.get("expires", -1))
    except (TypeError, ValueError, OverflowError):
        return False
    return 0 <= value <= now


def require_auth_cookies(cookies):
    """Require at least one present, non-empty, non-expired authentication cookie."""
    auth = [item for item in cookies or [] if isinstance(item, dict)
            and str(item.get("name") or "").strip() in AUTH_COOKIE_NAMES and item.get("value")]
    now = time.time()
    names = auth_cookie_names(item for item in auth if not _expired(item, now))
    if not names:
        if auth:
            raise CookieParseError(
                "导出的抖音登录 Cookie 已过期，请在已登录的页面重新导出。",
                category="cookie_auth_expired",
            )
        raise CookieParseError(
            "导出内容缺少有效的抖音登录 Cookie，请确认扩展在已登录的抖音页面导出。",
            category="cookie_auth_missing",
        )
    return names


def cookie_summary(cookies):
    """Describe cookies for logs and UI without exposing any cookie value."""
    return {
        "count": len(cookies or []),
        "auth_names": auth_cookie_names(cookies),
    }
