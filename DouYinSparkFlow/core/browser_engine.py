"""Optional, server-owned browser-engine policy and profile binding.

The default engine is stock Chromium.  A fingerprint engine can be selected only
by an enabled server-owned policy and is bound to an account profile before the
browser starts.  This module intentionally has no Playwright dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_POLICY_ENV = "SPARKFLOW_BROWSER_ENGINE_POLICY_FILE"
_GATE_ENV = "SPARKFLOW_FINGERPRINT_BROWSER_ENABLED"
_PROFILE_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_POLICY_BYTES = 1024 * 1024
_HASH_CACHE_MAX = 16
_hash_cache: OrderedDict[str, tuple[tuple[int, int, int, int, int], str]] = OrderedDict()


class BrowserEngineError(RuntimeError):
    """The configured browser engine cannot be used safely."""


@dataclass(frozen=True, slots=True)
class BrowserEngineSelection:
    """An immutable browser launch choice resolved from trusted configuration."""

    name: str
    _executable_path: str | None = None
    _seed: int | None = None
    _sha256: str | None = None

    def launch_overrides(self) -> dict[str, object]:
        if self.name == "stock":
            return {}
        if self.name == "fingerprint" and self._executable_path and self._seed is not None:
            return {
                "executable_path": self._executable_path,
                "args": [f"--fingerprint={self._seed}"],
            }
        raise BrowserEngineError("invalid fingerprint engine selection")

    def expectation(self) -> dict[str, object]:
        if self.name == "stock":
            return {"engine": "stock"}
        if self.name == "fingerprint" and self._seed is not None and self._sha256:
            return {
                "engine": "fingerprint",
                "seed": self._seed,
                "executable_sha256": self._sha256,
            }
        raise BrowserEngineError("invalid fingerprint engine selection")


def fingerprint_browser_enabled() -> bool:
    """Read the fail-closed opt-in gate."""
    raw = os.getenv(_GATE_ENV, "").strip().lower()
    if raw in {"", "0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    raise BrowserEngineError(f"invalid {_GATE_ENV} value")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BrowserEngineError(f"duplicate policy key: {key}")
        result[key] = value
    return result


def _read_policy() -> dict[str, Any]:
    raw_path = os.getenv(_POLICY_ENV, "").strip()
    if not raw_path:
        raise BrowserEngineError(f"{_POLICY_ENV} is required when fingerprint browser is enabled")
    path = Path(raw_path)
    if not path.is_absolute():
        raise BrowserEngineError("browser engine policy path must be absolute")
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise BrowserEngineError("browser engine policy must be a regular file")
        if info.st_size > _MAX_POLICY_BYTES:
            raise BrowserEngineError("browser engine policy is too large")
        text = path.read_text(encoding="utf-8")
    except BrowserEngineError:
        raise
    except (OSError, UnicodeError) as exc:
        raise BrowserEngineError("cannot read browser engine policy") from exc
    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, TypeError, ValueError, BrowserEngineError) as exc:
        raise BrowserEngineError("invalid browser engine policy JSON") from exc
    if type(parsed) is not dict:
        raise BrowserEngineError("browser engine policy must be a JSON object")
    return parsed


def _exact_keys(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise BrowserEngineError(f"invalid {label} schema")
    return value


def _valid_seed(value: object) -> bool:
    return type(value) is int and 0 <= value <= 0xFFFFFFFF


def _validate_policy(policy: dict[str, Any]) -> tuple[str, str, dict[str, tuple[str, int | None]]]:
    root = _exact_keys(policy, {"schema", "fingerprint", "profiles"}, "browser engine policy")
    if type(root["schema"]) is not int or root["schema"] != 1:
        raise BrowserEngineError("unsupported browser engine policy schema")
    fingerprint = _exact_keys(root["fingerprint"], {"executable_path", "sha256"}, "fingerprint")
    executable_path = fingerprint["executable_path"]
    digest = fingerprint["sha256"]
    if type(executable_path) is not str or type(digest) is not str or not _SHA256.fullmatch(digest):
        raise BrowserEngineError("invalid fingerprint executable configuration")
    profiles = root["profiles"]
    if type(profiles) is not dict or len(profiles) > 10000:
        raise BrowserEngineError("invalid browser engine profiles")
    normalized: dict[str, tuple[str, int | None]] = {}
    for profile_name, profile in profiles.items():
        if type(profile_name) is not str or not _PROFILE_NAME.fullmatch(profile_name):
            raise BrowserEngineError("invalid browser engine profile key")
        if type(profile) is not dict or type(profile.get("engine")) is not str:
            raise BrowserEngineError("invalid browser engine profile")
        engine = profile["engine"]
        if engine == "stock" and set(profile) == {"engine"}:
            normalized[profile_name] = (engine, None)
        elif engine == "fingerprint" and set(profile) == {"engine", "seed"} and _valid_seed(profile["seed"]):
            normalized[profile_name] = (engine, profile["seed"])
        else:
            raise BrowserEngineError("invalid browser engine profile")
    return executable_path, digest, normalized


def _path_has_symlink_ancestor(path: Path) -> bool:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            if current.is_symlink():
                return True
        except OSError as exc:
            raise BrowserEngineError("cannot validate browser engine path") from exc
    return False


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        info = path.stat()
    except OSError as exc:
        raise BrowserEngineError("fingerprint executable is unavailable") from exc
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _hash_executable(path: Path) -> str:
    key = str(path)
    before = _file_identity(path)
    cached = _hash_cache.get(key)
    if cached is not None and cached[0] == before:
        _hash_cache.move_to_end(key)
        return cached[1]
    for _ in range(2):
        hasher = hashlib.sha256()
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(chunk)
        except OSError as exc:
            raise BrowserEngineError("cannot hash fingerprint executable") from exc
        after = _file_identity(path)
        if before == after:
            digest = hasher.hexdigest()
            _hash_cache[key] = (after, digest)
            _hash_cache.move_to_end(key)
            while len(_hash_cache) > _HASH_CACHE_MAX:
                _hash_cache.popitem(last=False)
            return digest
        before = after
    raise BrowserEngineError("fingerprint executable changed while being verified")


def _verified_executable(raw_path: str, expected_digest: str) -> str:
    path = Path(raw_path)
    if not path.is_absolute():
        raise BrowserEngineError("fingerprint executable path must be absolute")
    if _path_has_symlink_ancestor(path):
        raise BrowserEngineError("fingerprint executable path must not contain symlinks")
    try:
        info = path.stat()
    except OSError as exc:
        raise BrowserEngineError("fingerprint executable is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or not (info.st_mode & 0o111):
        raise BrowserEngineError("fingerprint executable is not executable")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise BrowserEngineError("fingerprint executable path must not contain symlinks")
    if _hash_executable(path) != expected_digest:
        raise BrowserEngineError("fingerprint executable digest does not match policy")
    return str(resolved)


def resolve_browser_engine(profile_name: str | None = None) -> BrowserEngineSelection:
    """Resolve a server-owned engine choice for one canonical account profile."""
    # Keep existing stock paths completely independent of policy and custom binaries.
    if not fingerprint_browser_enabled() or profile_name is None:
        return BrowserEngineSelection("stock")
    if type(profile_name) is not str or not _PROFILE_NAME.fullmatch(profile_name):
        raise BrowserEngineError("invalid canonical browser profile name")
    executable, digest, profiles = _validate_policy(_read_policy())
    entry = profiles.get(profile_name)
    if entry is None or entry[0] == "stock":
        return BrowserEngineSelection("stock")
    verified_path = _verified_executable(executable, digest)
    return BrowserEngineSelection("fingerprint", verified_path, entry[1], digest)


def check_engine_expectation(selection: BrowserEngineSelection, expectation: object) -> None:
    """Reject a peer whose engine choice differs from this server's choice."""
    if not isinstance(selection, BrowserEngineSelection):
        raise BrowserEngineError("invalid browser engine selection")
    if expectation is None:
        if selection.name == "stock":
            return
        raise BrowserEngineError("fingerprint engine requires an expectation")
    expected = selection.expectation()
    if type(expectation) is not dict or set(expectation) != set(expected):
        raise BrowserEngineError("invalid browser engine expectation")
    if expectation != expected:
        raise BrowserEngineError("browser engine expectation does not match selection")
    if selection.name == "fingerprint" and type(expectation.get("seed")) is not int:
        raise BrowserEngineError("invalid fingerprint engine expectation seed")


_MARKER_NAME = ".sparkflow-browser-engine.json"
_ALLOWED_FRESH_METADATA = {".sparkflow-cookie-session.json", ".sparkflow-login-pending.json"}


def _safe_profile_directory(profile_dir: object, *, create: bool) -> Path:
    if not isinstance(profile_dir, (str, os.PathLike)):
        raise BrowserEngineError("browser profile path must be a path")
    path = Path(profile_dir)
    if not path.is_absolute() or _path_has_symlink_ancestor(path):
        raise BrowserEngineError("browser profile path must be absolute without symlink ancestors")
    if not path.exists():
        if not create:
            return path
        current = Path(path.anchor)
        try:
            for component in path.parts[1:]:
                current /= component
                current.mkdir(exist_ok=True)
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise BrowserEngineError("browser profile path is unsafe")
        except BrowserEngineError:
            raise
        except OSError as exc:
            raise BrowserEngineError("cannot create browser profile directory") from exc
    try:
        info = path.lstat()
    except OSError as exc:
        raise BrowserEngineError("cannot inspect browser profile directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BrowserEngineError("browser profile path is not a directory")
    return path


def _marker_path(profile_dir: Path) -> Path:
    return profile_dir / _MARKER_NAME


def _read_marker(marker: Path) -> dict[str, object] | None:
    if os.path.lexists(marker):
        try:
            info = marker.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise BrowserEngineError("browser engine marker is unsafe")
            if info.st_size > 4096:
                raise BrowserEngineError("browser engine marker is too large")
            text = marker.read_text(encoding="utf-8")
            value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except BrowserEngineError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise BrowserEngineError("invalid browser engine marker") from exc
        if type(value) is not dict:
            raise BrowserEngineError("invalid browser engine marker")
        return value
    return None


def _validate_fingerprint_marker(marker: dict[str, object]) -> dict[str, object]:
    expected_keys = {"schema", "engine", "seed", "executable_sha256"}
    if (
        set(marker) != expected_keys
        or type(marker.get("schema")) is not int
        or marker.get("schema") != 1
        or marker.get("engine") != "fingerprint"
    ):
        raise BrowserEngineError("foreign or incompatible browser engine marker")
    seed = marker.get("seed")
    digest = marker.get("executable_sha256")
    if not _valid_seed(seed) or type(digest) is not str or not _SHA256.fullmatch(digest):
        raise BrowserEngineError("invalid browser engine marker")
    return marker


def _profile_is_fresh(profile_dir: Path) -> bool:
    try:
        entries = list(profile_dir.iterdir())
    except OSError as exc:
        raise BrowserEngineError("cannot inspect browser profile directory") from exc
    for entry in entries:
        if entry.name not in _ALLOWED_FRESH_METADATA:
            return False
        try:
            info = entry.lstat()
        except OSError as exc:
            raise BrowserEngineError("cannot inspect profile metadata") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise BrowserEngineError("unsafe profile metadata")
    return True


def _write_marker_exclusively(profile_dir: Path, marker: dict[str, object]) -> bool:
    """Atomically publish ``marker`` iff no marker exists yet."""
    payload = (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".sparkflow-browser-engine-", suffix=".tmp", dir=str(profile_dir))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary_path, _marker_path(profile_dir), follow_symlinks=False)
        except FileExistsError:
            return False
        try:
            directory_fd = os.open(profile_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
        return True
    except OSError as exc:
        raise BrowserEngineError("cannot write browser engine marker") from exc
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def prepare_profile_engine(profile_dir: object, selection: BrowserEngineSelection) -> None:
    """Bind a fresh persistent profile directory to its resolved engine.

    The caller is responsible for the account's physical-browser lock.  This
    function only validates and initializes the profile-local durable marker.
    """
    if not isinstance(selection, BrowserEngineSelection):
        raise BrowserEngineError("invalid browser engine selection")
    if selection.name == "stock":
        # Do not create ordinary stock directories.  A pre-existing marker is a
        # binding and must prevent a silent downgrade.  This intentionally uses
        # the legacy path as given: stock profiles may be relative or rooted
        # through a symlink, and this read-only check must not change that.
        if not isinstance(profile_dir, (str, os.PathLike)):
            raise BrowserEngineError("browser profile path must be a path")
        marker = _read_marker(_marker_path(Path(profile_dir)))
        if marker is None:
            return
        _validate_fingerprint_marker(marker)
        raise BrowserEngineError("fingerprint-bound browser profile cannot use stock engine")
    if selection.name != "fingerprint":
        raise BrowserEngineError("invalid browser engine selection")
    expected = selection.expectation()
    profile = _safe_profile_directory(profile_dir, create=True)
    marker_path = _marker_path(profile)
    existing = _read_marker(marker_path)
    if existing is not None:
        existing = _validate_fingerprint_marker(existing)
        if existing != {"schema": 1, **expected}:
            raise BrowserEngineError("browser profile is bound to a different fingerprint engine")
        return
    if not _profile_is_fresh(profile):
        raise BrowserEngineError("existing Chromium profile requires migration or backup before fingerprint binding")
    durable = {"schema": 1, **expected}
    if _write_marker_exclusively(profile, durable):
        return
    raced = _read_marker(marker_path)
    if raced is None or _validate_fingerprint_marker(raced) != durable:
        raise BrowserEngineError("browser profile is bound to a different fingerprint engine")
