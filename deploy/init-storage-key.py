"""Create a storage key without replacing an existing key."""

import base64
import os
from pathlib import Path
import sys


def create_key(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(base64.urlsafe_b64encode(os.urandom(32)) + b"\n")
        output.flush()
        os.fsync(output.fileno())


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("state/keys/storage.key")
    try:
        create_key(target)
    except FileExistsError:
        raise SystemExit("Key already exists; it was not replaced.")
    print(f"Storage key created: {target}. Keep a separate, protected backup.")
