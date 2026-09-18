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
import re

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
        raise CookieParseError(f"cookie {name!r} is missing its value")
    value = str(entry.get("value") or "")

    domain = str(entry.get("domain") or "").strip()
    if not domain:
        domain = DEFAULT_COOKIE_DOMAIN
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
    cookie["expires"] = expires if expires > 0 else -1

    for flag in ("httpOnly", "secure"):
        if flag in entry:
            cookie[flag] = bool(entry.get(flag))
    same_site = entry.get("sameSite")
    if isinstance(same_site, str) and same_site.strip():
        cookie["sameSite"] = same_site.strip().capitalize()
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
        if "=" not in text:
            raise CookieParseError(
                "cookie input must contain name=value pairs; paste the JSON export "
                "or the Cookie request header"
            )
        entries = _parse_header_cookies(text)

    cookies = []
    seen = set()
    for entry in entries:
        cookie = normalize_cookie(entry)
        key = (cookie["name"], cookie["domain"], cookie["path"])
        if key in seen:
            continue
        seen.add(key)
        cookies.append(cookie)
    if not cookies:
        raise CookieParseError("cookie input does not contain any usable cookie")
    if len(cookies) > _MAX_COOKIES:
        cookies = cookies[:_MAX_COOKIES]
    return cookies


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


def require_auth_cookies(cookies):
    """Raise when the pasted cookies carry no known authentication cookie."""
    names = auth_cookie_names(cookies)
    if not names:
        raise CookieParseError(
            "cookie input has no Douyin authentication cookie "
            f"(expected one of: {', '.join(sorted(AUTH_COOKIE_NAMES))})",
            category="cookie_auth_missing",
        )
    return names


def cookie_summary(cookies):
    """Describe cookies for logs and UI without exposing any cookie value."""
    return {
        "count": len(cookies or []),
        "auth_names": auth_cookie_names(cookies),
    }
