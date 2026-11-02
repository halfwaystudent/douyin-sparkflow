"""Authenticated encryption for application JSON files."""

import json
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class StorageError(RuntimeError):
    pass


def storage_cipher():
    key_path = os.getenv("SPARKFLOW_STORAGE_KEY_FILE", "").strip()
    if not key_path:
        return None
    path = Path(key_path)
    try:
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise StorageError("Storage key must be readable only by its owner (chmod 600).")
        keys = [Fernet(line.strip().encode("ascii")) for line in path.read_text().splitlines() if line.strip()]
        if not keys:
            raise ValueError
        return MultiFernet(keys)
    except StorageError:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        raise StorageError("Storage key is missing or invalid.") from exc


def encrypted_payload(data):
    return isinstance(data, dict) and data.get("sparkflowEncrypted") == 1


def encryption_required():
    return os.getenv("SPARKFLOW_STORAGE_REQUIRE_ENCRYPTED", "").lower() in {"1", "true", "yes", "on"}


def read_json(path, *, allow_plaintext=False):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise StorageError("Unable to read JSON storage.") from exc
    if not encrypted_payload(data):
        if isinstance(data, dict) and "sparkflowEncrypted" in data:
            raise StorageError("Unsupported encrypted storage format.")
        if encryption_required() and not allow_plaintext:
            raise StorageError("Plaintext storage is disabled. Run the offline migration first.")
        return data
    cipher = storage_cipher()
    if cipher is None:
        raise StorageError("An encryption key is required to read this file.")
    try:
        return json.loads(cipher.decrypt(data["ciphertext"].encode("ascii")))
    except (InvalidToken, KeyError, TypeError, ValueError, UnicodeError, AttributeError) as exc:
        raise StorageError("Unable to decrypt storage. Check the key and restore a verified backup if needed.") from exc


def serialize_json(data, path, *, allow_plaintext_migration=False):
    cipher = storage_cipher()
    if encryption_required() and cipher is None:
        raise StorageError("Protected storage requires its encryption key.")
    if Path(path).exists():
        text = Path(path).read_text(encoding="utf-8")
        try:
            existing = json.loads(text) if text.strip() else None
        except ValueError as exc:
            raise StorageError("Refusing to overwrite invalid JSON storage.") from exc
        if encrypted_payload(existing):
            read_json(path)
        elif existing is not None and encryption_required() and not allow_plaintext_migration:
            raise StorageError("Refusing to replace plaintext data in protected mode; run migration.")
    if cipher:
        token = cipher.encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8")).decode("ascii")
        data = {"sparkflowEncrypted": 1, "ciphertext": token}
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def require_encrypted_files(paths):
    if storage_cipher() is None:
        raise StorageError("Shared mode requires SPARKFLOW_STORAGE_KEY_FILE.")
    for path in paths:
        path = Path(path)
        if path.exists():
            try:
                encrypted = encrypted_payload(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, OSError) as exc:
                raise StorageError("Unable to inspect application storage.") from exc
            if not encrypted:
                raise StorageError("Migrate existing JSON data before starting shared mode.")
            read_json(path)
