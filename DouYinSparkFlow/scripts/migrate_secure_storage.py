"""Run with all application services stopped and the key file configured."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filelock import FileLock
from utils.config import _save_json_file
from utils.secure_storage import read_json, storage_cipher, require_encrypted_files


FILES = ("config.json", "usersData.json", "webui_settings.json", "webui_users.json", "webui_passkeys.json", "webui_sessions.json")


def migrate(directory, *, check_only=False):
    if storage_cipher() is None:
        raise ValueError("Set SPARKFLOW_STORAGE_KEY_FILE before running migration.")
    directory = Path(directory)
    paths = [directory / name for name in FILES if (directory / name).exists()]
    paths += sorted((directory / ".data-backups").glob("*.json"))
    if check_only:
        require_encrypted_files(paths)
        return [str(path.relative_to(directory)) for path in paths]
    # Verify every existing encrypted file before changing any file.
    for path in paths:
        read_json(path, allow_plaintext=True)
    converted = []
    for path in paths:
        with FileLock(f"{path}.lock", timeout=5):
            data = read_json(path, allow_plaintext=True)
            if not check_only:
                _save_json_file(path, data, allow_plaintext_migration=True)
        converted.append(str(path.relative_to(directory)))
    return converted


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name in migrate(args.data_dir, check_only=args.check):
        print(("Checked: " if args.check else "Encrypted: ") + name)
