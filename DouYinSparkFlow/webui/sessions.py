"""Revocations remain valid until a session's absolute expiry."""

import hashlib
import time

from filelock import FileLock

from utils import config
from utils.secure_storage import StorageError, read_json


MAX_REVOCATIONS = 20000


def store_path():
    return config.app_settings_path().with_name("webui_sessions.json")


def _read(path):
    data = read_json(path) if path.exists() else {}
    if not isinstance(data, dict) or any(not isinstance(value, (int, float)) for value in data.values()):
        raise StorageError("Invalid session revocation storage.")
    return data


def is_revoked(session_id):
    return _read(store_path()).get(hashlib.sha256(session_id.encode()).hexdigest(), 0) > time.time()


def revoke(session_id, expires_at):
    if not session_id or expires_at <= time.time():
        return
    path = store_path()
    with FileLock(f"{path}.lock", timeout=5):
        data = {key: value for key, value in _read(path).items() if value > time.time()}
        digest = hashlib.sha256(session_id.encode()).hexdigest()
        if data.get(digest, 0) >= expires_at:
            return
        if digest not in data and len(data) >= MAX_REVOCATIONS:
            raise StorageError("Session revocation capacity reached. Retry after old sessions expire.")
        data[digest] = expires_at
        config._save_json_file(path, data)
