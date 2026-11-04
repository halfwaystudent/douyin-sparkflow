#!/usr/bin/env python3
"""Create noVNC's optional version metadata from the installed Debian package."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def installed_novnc_version() -> str | None:
    """Return the version owned by dpkg, without inventing an upstream version."""
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", "novnc"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as error:
        print(f"Could not read installed noVNC version: {error}", file=sys.stderr)
        return None

    version = result.stdout.strip()
    if result.returncode != 0 or not version:
        print("Could not read installed noVNC version from dpkg-query", file=sys.stderr)
        return None
    return version


def ensure_metadata(web_root: Path) -> bool:
    """Write package.json only for a usable noVNC web root that lacks it."""
    metadata_path = web_root / "package.json"
    if metadata_path.exists():
        return True
    if not web_root.is_dir() or not (web_root / "vnc.html").is_file():
        return True

    version = installed_novnc_version()
    if version is None:
        return False

    descriptor, temporary_name = tempfile.mkstemp(
        dir=web_root, prefix=".package.json.", suffix=".tmp", text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": version}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, metadata_path)
    except OSError as error:
        temporary_path.unlink(missing_ok=True)
        print(f"Could not write noVNC metadata: {error}", file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--web-root", type=Path, default=Path("/usr/share/novnc"))
    args = parser.parse_args()
    return 0 if ensure_metadata(args.web_root) else 1


if __name__ == "__main__":
    raise SystemExit(main())
