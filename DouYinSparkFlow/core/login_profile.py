"""Fail-closed account profile selection for the login desktop service."""

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock, Timeout as FileLockTimeout


DEFAULT_ACCOUNT_PROFILE_ROOT = Path("/opt/douyin-sparkflow/state/browser-profiles")
DEFAULT_ACCOUNT_LOCK_ROOT = Path("/app/logs/browser-account-locks")
_UNIQUE_ID = re.compile(r"[A-Za-z0-9._-]{1,76}\Z")


class AccountProfileError(ValueError):
    """The server did not receive a safe, complete account selector."""


class AccountProfileBusy(RuntimeError):
    """The sender or another login browser owns this account profile."""


@dataclass(frozen=True)
class AccountProfile:
    unique_id: str
    account_ref: str
    login_generation: str
    profile_name: str
    profile_dir: Path
    guard_path: Path

    @property
    def selector(self):
        return (self.unique_id, self.account_ref, self.login_generation)


def _configured_root(variable, default):
    value = str(os.getenv(variable) or default).strip()
    if not value:
        raise AccountProfileError(f"{variable} is empty")
    return Path(value).expanduser()


def _safe_unique_id(value):
    if not isinstance(value, str):
        raise AccountProfileError("account unique_id is required")
    unique_id = value.strip()
    digits = "".join(ch for ch in unique_id if ch.isdigit())
    if not _UNIQUE_ID.fullmatch(unique_id) or (digits and unique_id != digits):
        raise AccountProfileError("account unique_id is invalid or not normalized")
    return unique_id


def resolve_account_profile(selector):
    """Resolve an account selector without accepting a caller-controlled path."""
    if not isinstance(selector, dict) or set(selector) - {
        "unique_id", "account_ref", "login_generation"
    }:
        raise AccountProfileError("account profile selector is invalid")
    unique_id = _safe_unique_id(selector.get("unique_id"))
    account_ref = selector.get("account_ref")
    generation = selector.get("login_generation")
    if not isinstance(account_ref, str) or not account_ref.strip():
        raise AccountProfileError("account_ref is required")
    if not isinstance(generation, str):
        raise AccountProfileError("login_generation is invalid")
    profile_root = _configured_root("LOGIN_ACCOUNT_PROFILE_ROOT", DEFAULT_ACCOUNT_PROFILE_ROOT)
    lock_root = _configured_root("LOGIN_ACCOUNT_LOCK_ROOT", DEFAULT_ACCOUNT_LOCK_ROOT)
    # These match core.browser.account_profile_name and browser_account_lock_name
    # for the validated unique-id-only account shape.
    return AccountProfile(
        unique_id=unique_id,
        account_ref=account_ref.strip(),
        login_generation=generation,
        profile_name=f"uid-{unique_id}",
        profile_dir=profile_root / f"uid-{unique_id}",
        guard_path=lock_root / f"{unique_id.replace('.', '_')}.lock.guard",
    )


def _mkdir_without_symlink(path):
    # Inspect ancestors before mkdir: parents=True would follow a link and
    # create credential-bearing directories outside the intended mount.
    path = Path(path).absolute()
    chain = list(reversed(path.parents)) + [path]
    for component in chain:
        if component.is_symlink():
            raise AccountProfileError(f"unsafe account profile path: {component}")
    for component in chain:
        if component.is_symlink():
            raise AccountProfileError(f"unsafe account profile path: {component}")
        component.mkdir(exist_ok=True)
        if component.is_symlink() or not component.is_dir():
            raise AccountProfileError(f"unsafe account profile path: {component}")


def _verify_existing_cookie_marker(profile):
    marker_path = profile.profile_dir / ".sparkflow-cookie-session.json"
    if marker_path.is_symlink():
        raise AccountProfileError("account profile marker is unsafe")
    if not marker_path.exists():
        # Legacy profiles remain untouched; authorization comes from the
        # server-bound selector, not an invented client-side migration.
        return
    if marker_path.is_symlink() or not marker_path.is_file():
        raise AccountProfileError("account profile marker is unsafe")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountProfileError("account profile marker is unreadable") from exc
    if not isinstance(marker, dict):
        raise AccountProfileError("account profile marker is invalid")
    if (
        str(marker.get("account_ref") or "") != profile.account_ref
        or str(marker.get("unique_id") or "") != profile.unique_id
    ):
        raise AccountProfileError("account profile marker belongs to another account")


def ensure_account_profile_directory(profile):
    """Create the exact account directory while refusing symlink substitution."""
    if not isinstance(profile, AccountProfile):
        raise AccountProfileError("account profile is invalid")
    _mkdir_without_symlink(profile.profile_dir.parent)
    if profile.profile_dir.exists() or profile.profile_dir.is_symlink():
        if profile.profile_dir.is_symlink() or not profile.profile_dir.is_dir():
            raise AccountProfileError("unsafe account profile path")
    else:
        profile.profile_dir.mkdir(mode=0o700)
    if profile.profile_dir.is_symlink() or not profile.profile_dir.is_dir():
        raise AccountProfileError("unsafe account profile path")
    _verify_existing_cookie_marker(profile)
    return profile.profile_dir


def acquire_account_profile_guard(profile):
    """Take the sender-compatible guard without touching its metadata lock file."""
    if not isinstance(profile, AccountProfile):
        raise AccountProfileError("account profile is invalid")
    _mkdir_without_symlink(profile.guard_path.parent)
    # FileLock uses this exact sidecar convention.  Do not unlink, overwrite, or
    # recreate it: a sender can already own it.
    guard = FileLock(str(profile.guard_path), timeout=0)
    try:
        guard.acquire()
    except FileLockTimeout as exc:
        raise AccountProfileBusy("account browser profile is busy") from exc
    return guard


_PENDING_NAME = ".sparkflow-login-pending.json"


def read_account_profile_pending(profile_dir):
    """Read a durable unsaved-login fence; malformed state never permits send."""
    path = Path(profile_dir) / _PENDING_NAME
    if path.is_symlink():
        raise AccountProfileError("pending login marker is unsafe")
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise AccountProfileError("pending login marker is unreadable") from exc
    if (not isinstance(payload, dict) or payload.get("version") != 1
            or not all(isinstance(payload.get(key), str) for key in
                       ("account_ref", "unique_id", "login_generation"))
            or not payload["account_ref"] or not payload["unique_id"]):
        raise AccountProfileError("pending login marker is invalid")
    return payload


def mark_account_profile_pending(profile):
    """Call under the physical account guard BEFORE opening its login browser."""
    previous = read_account_profile_pending(profile.profile_dir)
    payload = {"version": 1, "account_ref": profile.account_ref,
               "unique_id": profile.unique_id,
               "login_generation": profile.login_generation}
    if previous is not None:
        if (previous["account_ref"], previous["unique_id"]) != (profile.account_ref, profile.unique_id):
            raise AccountProfileError("pending login belongs to another account")
        if previous == payload:
            return
    path = profile.profile_dir / _PENDING_NAME
    fd, temporary = tempfile.mkstemp(prefix=".login-pending-", dir=str(profile.profile_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def clear_account_profile_pending(profile_dir, expected):
    """Only a verified new-generation import may remove its matching fence."""
    if read_account_profile_pending(profile_dir) != expected:
        raise AccountProfileError("pending login changed during cookie import")
    (Path(profile_dir) / _PENDING_NAME).unlink()
