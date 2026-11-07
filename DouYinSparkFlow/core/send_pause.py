"""Temporary, fail-closed pause guard for all message-send entry points."""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path

from utils.config import repo_root


_LOG = logging.getLogger(__name__)


def _pause_file(path):
    return Path(path) if path is not None else repo_root() / "logs" / "sending-paused-until.json"


def pause_status(path=None, now=None):
    """Return ``(paused, reason)`` for the temporary sending-pause file."""
    target = _pause_file(path)
    current = now or datetime.now(timezone.utc)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        until = datetime.fromisoformat(str(payload["until"]))
    except FileNotFoundError:
        return False, "pause_file_missing"
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        _LOG.warning("Sending paused because the pause state is invalid or unreadable")
        return True, "pause_file_invalid"
    if until.tzinfo is None or until.utcoffset() is None or current.tzinfo is None or current.utcoffset() is None:
        _LOG.warning("Sending paused because the pause state has no usable timezone")
        return True, "pause_file_invalid"
    if until.astimezone(timezone.utc) > current.astimezone(timezone.utc):
        _LOG.warning("Sending is temporarily paused until %s", until.isoformat())
        return True, "pause_until_active"
    return False, "pause_until_elapsed"


def should_pause_sending(path=None, now=None):
    """Whether sending must stop; absent pause state preserves prior behavior."""
    return pause_status(path=path, now=now)[0]
