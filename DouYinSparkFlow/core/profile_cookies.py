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

from core import identity as account_identity
from core.identity import FIELD_COOKIE_IMPORT_GENERATION, FIELD_LOGIN_GENERATION
from core.login_profile import AccountProfileError, read_account_profile_pending, clear_account_profile_pending
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


def _safe_marker_path(profile_dir: Path, *, create_directory: bool) -> Path:
    """Return a marker path without following profile or marker symlinks."""
    directory = Path(profile_dir).absolute()
    chain = list(reversed(directory.parents)) + [directory]
    try:
        for component in chain:
            if component.is_symlink():
                raise ProfileCookieSyncError("profile cookie state marker is unsafe")
            if create_directory:
                component.mkdir(exist_ok=True)
            elif not component.exists():
                # A legacy caller can inspect a not-yet-created profile; there
                # cannot be a marker beneath it, and writes will create it.
                return directory / _MARKER_NAME
            if component.is_symlink() or not component.is_dir():
                raise ProfileCookieSyncError("profile cookie state marker is unsafe")
    except OSError as exc:
        raise ProfileCookieSyncError("profile cookie state marker could not be written") from exc
    path = directory / _MARKER_NAME
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ProfileCookieSyncError("profile cookie state marker is unsafe")
    return path


def _read_marker_snapshot(profile_dir: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    path = _safe_marker_path(profile_dir, create_directory=False)
    if not path.exists():
        return None, None
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ProfileCookieSyncError("profile cookie state marker is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("version") != _MARKER_VERSION:
        raise ProfileCookieSyncError("profile cookie state marker is invalid")
    if not all(key in payload for key in ("account_ref", "unique_id", FIELD_LOGIN_GENERATION)):
        raise ProfileCookieSyncError("profile cookie state marker is incomplete")
    return payload, raw


def _read_marker(profile_dir: Path) -> dict[str, Any] | None:
    return _read_marker_snapshot(profile_dir)[0]


def _write_marker(profile_dir: Path, account: dict[str, Any]) -> None:
    payload = {
        "version": _MARKER_VERSION,
        "account_ref": _identity(account)[0],
        "unique_id": _identity(account)[1],
        FIELD_LOGIN_GENERATION: str(account.get(FIELD_LOGIN_GENERATION) or ""),
    }
    target = _safe_marker_path(profile_dir, create_directory=True)
    try:
        fd, name = tempfile.mkstemp(prefix=f".{_MARKER_NAME}.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            if target.is_symlink():
                raise ProfileCookieSyncError("profile cookie state marker is unsafe")
            os.replace(name, target)
        except BaseException:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
    except ProfileCookieSyncError:
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


def _orphan_recovery_trusted_uid(marker, fresh, profile_config) -> str:
    """Return the trusted platform UID for the narrow repair case, if eligible."""
    marker_ref = str(marker.get("account_ref") or "")
    marker_uid = normalize_unique_id(marker.get("unique_id"))
    marker_generation = marker.get(FIELD_LOGIN_GENERATION)
    fresh_ref, fresh_uid = _identity(fresh)
    generation = str(fresh.get(FIELD_LOGIN_GENERATION) or "")
    import_generation = str(fresh.get(IMPORT_GENERATION_FIELD) or "")
    trusted_uid = account_identity.account_uid(fresh)
    if (
        not marker_ref
        or not marker_uid
        or not fresh_ref
        or not fresh_uid
        or marker_ref == fresh_ref
        or marker_uid != fresh_uid
        or not isinstance(marker_generation, str)
        or not marker_generation
        or not generation
        or marker_generation == generation
        or profile_config.get("syncStoredCookiesBeforeRun") is not True
        or not import_generation
        or import_generation != generation
        or not trusted_uid
        or trusted_uid != fresh_uid
    ):
        return ""
    return trusted_uid


def _orphan_recovery_snapshot_from_accounts(accounts, expected, marker, marker_bytes, profile_dir, trusted_uid):
    """Validate the complete repair fence while the data-store lock is held."""
    expected_ref, expected_uid = _identity(expected)
    expected_generation = expected.get(FIELD_LOGIN_GENERATION)
    expected_cookies = deepcopy(expected.get("cookies"))
    marker_ref = str(marker.get("account_ref") or "")
    try:
        pending = read_account_profile_pending(profile_dir)
    except AccountProfileError as exc:
        raise ProfileCookieSyncError(str(exc)) from exc
    current = _find_account(accounts, expected)
    same_platform_uid = [row for row in accounts or [] if account_identity.account_uid(row) == trusted_uid]
    same_profile_uid = [row for row in accounts or [] if _identity(row)[1] == expected_uid]
    live_marker, live_bytes = _read_marker_snapshot(profile_dir)
    if (
        pending is not None
        or current is None
        or _identity(current) != (expected_ref, expected_uid)
        or current.get(FIELD_LOGIN_GENERATION) != expected_generation
        or current.get("cookies") != expected_cookies
        or current.get(IMPORT_GENERATION_FIELD) != expected_generation
        or len(same_profile_uid) != 1
        or any(str(row.get("account_ref") or "").strip() == marker_ref for row in accounts or [])
        or len(same_platform_uid) != 1
        or _identity(same_platform_uid[0]) != (expected_ref, expected_uid)
        or live_marker != marker
        or live_bytes != marker_bytes
    ):
        raise ProfileCookieSyncError("orphaned profile cookie state changed during recovery")
    return deepcopy(current)


def _locked_orphan_recovery_snapshot(expected, marker, marker_bytes, profile_dir, trusted_uid):
    """Read the exact repair state under the account data-store lock.

    The callback deliberately returns ``changed=False``: it is a CAS fence, not
    an account migration.  The caller already owns the physical browser lock.
    """
    def check(accounts):
        return _orphan_recovery_snapshot_from_accounts(
            accounts, expected, marker, marker_bytes, profile_dir, trusted_uid
        ), False

    return update_user_data(check, force_reload=True)


async def _recover_orphan_same_uid_marker(context, fresh, profile_config, profile_dir, marker, marker_bytes) -> Session:
    """Repair only a deleted/re-added, proven same-account marker conflict."""
    trusted_uid = _orphan_recovery_trusted_uid(marker, fresh, profile_config)
    if not trusted_uid:
        raise ProfileCookieSyncError("profile cookie state belongs to another account")
    # Reject non-orphans or ambiguous trusted identities before credential use.
    _locked_orphan_recovery_snapshot(fresh, marker, marker_bytes, profile_dir, trusted_uid)
    try:
        proof = await asyncio.to_thread(account_identity.fetch_trusted_identity, fresh.get("cookies") or [])
        actual_uid = str((proof or {}).get("uid") or "")
    except Exception as exc:
        raise ProfileCookieSyncError("saved login identity could not be verified for marker recovery") from exc
    if actual_uid != trusted_uid:
        raise ProfileCookieSyncError("saved login identity does not match marker recovery account")

    # The credential proof can take time; repeat the data and byte-for-byte
    # marker CAS before touching the browser profile.
    _locked_orphan_recovery_snapshot(fresh, marker, marker_bytes, profile_dir, trusted_uid)
    stored_cookies = _valid_auth_bundle(fresh.get("cookies"))
    if not stored_cookies:
        raise ProfileCookieSyncError("new login has no valid saved authentication cookies")
    await context.clear_cookies(domain=re.compile(r"(^|\.)douyin\.com$", re.IGNORECASE))
    _locked_orphan_recovery_snapshot(fresh, marker, marker_bytes, profile_dir, trusted_uid)
    await context.add_cookies(stored_cookies)

    # Commit the new marker only while holding the same store lock and only if
    # the conflict marker is byte-for-byte the one we authenticated above.
    def commit(accounts):
        current = _locked_orphan_recovery_snapshot_callback(
            accounts, fresh, marker, marker_bytes, profile_dir, trusted_uid
        )
        _write_marker(profile_dir, current)
        return deepcopy(current), False

    latest = update_user_data(commit, force_reload=True)
    return Session(latest, profile_dir, stored_cookies, imported=True)


def _locked_orphan_recovery_snapshot_callback(accounts, expected, marker, marker_bytes, profile_dir, trusted_uid):
    """Callback form shared by the recovery CAS read and marker commit."""
    return _orphan_recovery_snapshot_from_accounts(
        accounts, expected, marker, marker_bytes, profile_dir, trusted_uid
    )


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
    marker, marker_bytes = _read_marker_snapshot(Path(profile_dir))
    stored_cookies = _valid_auth_bundle(fresh.get("cookies"))
    pending_generation = str(fresh.get(IMPORT_GENERATION_FIELD) or "")
    generation = str(fresh.get(FIELD_LOGIN_GENERATION) or "")
    try:
        login_pending = read_account_profile_pending(profile_dir)
    except AccountProfileError as exc:
        raise ProfileCookieSyncError(str(exc)) from exc
    if login_pending is not None:
        if (login_pending["account_ref"], login_pending["unique_id"]) != _identity(fresh):
            raise ProfileCookieSyncError("pending login belongs to another account")
        if (not generation or generation == login_pending["login_generation"]
                or not profile_config.get("syncStoredCookiesBeforeRun")):
            raise ProfileCookieSyncError("重新登录尚未保存，pending profile cannot be used for sending")
        # Never adopt cookies left by a cancelled or rejected relogin. Only
        # trusted account persistence can supply a newer generation to import.
        session = await _replace_douyin_cookies(context, fresh, Path(profile_dir))
        _fresh_snapshot(fresh)
        try:
            clear_account_profile_pending(profile_dir, login_pending)
        except (AccountProfileError, OSError) as exc:
            raise ProfileCookieSyncError("pending login fence could not be cleared") from exc
        return session
    if marker is not None:
        marker_ref = str(marker.get("account_ref") or "")
        marker_uid = normalize_unique_id(marker.get("unique_id"))
        if (marker_ref, marker_uid) != _identity(fresh):
            return await _recover_orphan_same_uid_marker(
                context, fresh, profile_config, Path(profile_dir), marker, marker_bytes
            )
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
    try:
        if read_account_profile_pending(session.profile_dir) is not None:
            return False
    except AccountProfileError:
        return False
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
