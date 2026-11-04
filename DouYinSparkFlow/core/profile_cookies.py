"""Safe synchronization between stored account cookies and a browser profile.

The module never makes browser requests.  Callers hold the account/profile lock
while preparing or publishing a session.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.identity import FIELD_COOKIE_IMPORT_GENERATION, FIELD_LOGIN_GENERATION
from core.streak_state import AUTH_COOKIE_NAMES
from utils.config import get_userData, normalize_unique_id, update_user_data

IMPORT_GENERATION_FIELD = FIELD_COOKIE_IMPORT_GENERATION
_MARKER_NAME = ".sparkflow-cookie-session.json"
_MARKER_VERSION = 1


class ProfileCookieSyncError(RuntimeError):
    """Persistent-profile cookie state cannot safely be synchronized."""


@dataclass
class Session:
    """A caller-held account/profile synchronization snapshot."""

    account: dict[str, Any]
    profile_dir: Path
    cookies: list[dict[str, Any]]
    imported: bool = False


def _identity(account: dict[str, Any]) -> tuple[str, str]:
    return (
        str(account.get("account_ref") or "").strip(),
        normalize_unique_id(account.get("unique_id")),
    )


def _find_account(accounts, expected):
    expected_ref, expected_uid = _identity(expected)
    if expected_ref:
        matches = [item for item in accounts or [] if str(item.get("account_ref") or "").strip() == expected_ref]
    elif expected_uid:
        matches = [item for item in accounts or [] if normalize_unique_id(item.get("unique_id")) == expected_uid]
    else:
        return None
    if len(matches) != 1:
        return None
    candidate = matches[0]
    if expected_uid and normalize_unique_id(candidate.get("unique_id")) != expected_uid:
        return None
    return candidate


def _expiry_valid(cookie: dict[str, Any]) -> bool:
    value = cookie.get("expires")
    if value is None:
        value = cookie.get("expiry")
    if value in (None, "", -1, "-1", 0, "0"):
        return True
    try:
        return float(value) > datetime.now(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return False


def _is_douyin_domain(value: Any) -> bool:
    domain = str(value or "").strip().lower().lstrip(".")
    return domain == "douyin.com" or domain.endswith(".douyin.com")


def _is_auth_cookie(cookie: Any) -> bool:
    return (
        isinstance(cookie, dict)
        and str(cookie.get("name") or "") in AUTH_COOKIE_NAMES
        and bool(str(cookie.get("value") or ""))
        and _is_douyin_domain(cookie.get("domain"))
        and _expiry_valid(cookie)
    )


def _valid_auth_bundle(cookies: Any) -> list[dict[str, Any]]:
    values = [deepcopy(item) for item in cookies or []
              if isinstance(item, dict) and _is_douyin_domain(item.get("domain"))]
    return values if any(_is_auth_cookie(item) for item in values) else []


def _marker_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / _MARKER_NAME


def _read_marker(profile_dir: Path) -> dict[str, Any] | None:
    path = _marker_path(profile_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ProfileCookieSyncError("profile cookie state marker is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("version") != _MARKER_VERSION:
        raise ProfileCookieSyncError("profile cookie state marker is invalid")
    if not all(key in payload for key in ("account_ref", "unique_id", FIELD_LOGIN_GENERATION)):
        raise ProfileCookieSyncError("profile cookie state marker is incomplete")
    return payload


def _write_marker(profile_dir: Path, account: dict[str, Any]) -> None:
    directory = Path(profile_dir)
    payload = {
        "version": _MARKER_VERSION,
        "account_ref": _identity(account)[0],
        "unique_id": _identity(account)[1],
        FIELD_LOGIN_GENERATION: str(account.get(FIELD_LOGIN_GENERATION) or ""),
    }
    target = _marker_path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{_MARKER_NAME}.", dir=str(directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, target)
        except BaseException:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise ProfileCookieSyncError("profile cookie state marker could not be written") from exc


def _fresh_snapshot(expected: dict[str, Any]) -> dict[str, Any]:
    latest = _find_account(get_userData(force_reload=True), expected)
    if (
        latest is None
        or latest.get(FIELD_LOGIN_GENERATION) != expected.get(FIELD_LOGIN_GENERATION)
        or latest.get("cookies") != expected.get("cookies")
    ):
        raise ProfileCookieSyncError("account changed while importing cookies")
    return deepcopy(latest)


async def _replace_douyin_cookies(context, fresh: dict[str, Any], profile_dir: Path) -> Session:
    stored_cookies = _valid_auth_bundle(fresh.get("cookies"))
    if not stored_cookies:
        raise ProfileCookieSyncError("new login has no valid saved authentication cookies")
    await context.clear_cookies(domain=re.compile(r"(^|\.)douyin\.com$", re.IGNORECASE))
    _fresh_snapshot(fresh)
    await context.add_cookies(stored_cookies)
    latest = _fresh_snapshot(fresh)
    _write_marker(profile_dir, latest)
    return Session(latest, profile_dir, stored_cookies, imported=True)


async def prepare_profile_cookies(context, account, profile_config, profile_dir) -> Session:
    """Adopt a verified profile session or safely import the saved generation."""
    fresh = _find_account(get_userData(force_reload=True), account or {})
    if fresh is None:
        raise ProfileCookieSyncError("account changed or is missing")
    fresh = deepcopy(fresh)
    try:
        current = await context.cookies()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ProfileCookieSyncError("profile cookies could not be read") from exc
    fresh = _fresh_snapshot(fresh)
    marker = _read_marker(Path(profile_dir))
    stored_cookies = _valid_auth_bundle(fresh.get("cookies"))
    pending_generation = str(fresh.get(IMPORT_GENERATION_FIELD) or "")
    generation = str(fresh.get(FIELD_LOGIN_GENERATION) or "")
    if marker is not None:
        marker_ref = str(marker.get("account_ref") or "")
        marker_uid = normalize_unique_id(marker.get("unique_id"))
        if (marker_ref, marker_uid) != _identity(fresh):
            raise ProfileCookieSyncError("profile cookie state belongs to another account")
        if marker.get(FIELD_LOGIN_GENERATION) != generation:
            if not profile_config.get("syncStoredCookiesBeforeRun"):
                raise ProfileCookieSyncError("stored login generation differs from persistent profile")
            return await _replace_douyin_cookies(context, fresh, Path(profile_dir))
    if marker is None and pending_generation and pending_generation == generation:
        return await _replace_douyin_cookies(context, fresh, Path(profile_dir))
    if marker is None and _valid_auth_bundle(current):
        _write_marker(Path(profile_dir), fresh)
        return Session(fresh, Path(profile_dir), stored_cookies)
    if not _valid_auth_bundle(current) and profile_config.get("seedCookiesWhenEmpty") and stored_cookies:
        _fresh_snapshot(fresh)
        await context.add_cookies(stored_cookies)
        latest = _fresh_snapshot(fresh)
        _write_marker(Path(profile_dir), latest)
        return Session(latest, Path(profile_dir), stored_cookies, imported=True)
    return Session(fresh, Path(profile_dir), stored_cookies)


def _douyin_cookies(cookies: Any) -> list[dict[str, Any]]:
    return [deepcopy(item) for item in cookies or [] if isinstance(item, dict) and _is_douyin_domain(item.get("domain"))]


async def publish_profile_cookies(context, session) -> bool:
    """CAS-export a verified profile bundle without overwriting another writer."""
    exported = _douyin_cookies(await context.cookies())
    if not _valid_auth_bundle(exported):
        return False
    expected = deepcopy(session.account)
    expected_ref, expected_uid = _identity(expected)
    expected_generation = expected.get(FIELD_LOGIN_GENERATION)
    expected_cookies = deepcopy(expected.get("cookies") or [])

    def mutate(accounts):
        matched = _find_account(accounts, expected)
        if (
            matched is None
            or _identity(matched) != (expected_ref, expected_uid)
            or matched.get(FIELD_LOGIN_GENERATION) != expected_generation
            or matched.get("cookies") != expected_cookies
        ):
            return None, False
        matched["cookies"] = deepcopy(exported)
        return deepcopy(matched), True

    updated = update_user_data(mutate, force_reload=True)
    if updated is None:
        return False
    session.account.clear()
    session.account.update(deepcopy(updated))
    session.cookies = deepcopy(exported)
    return True
