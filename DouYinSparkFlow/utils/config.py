import json
import logging
import os
import secrets
import sys
import tempfile
import time
import uuid
from copy import deepcopy
from enum import Enum
from pathlib import Path

from filelock import FileLock

from utils.logger import setup_logger


logger = setup_logger(level=logging.DEBUG)

DEBUG = False
CONFIGFILE = "config.json"
USERDATAFILE = "usersData.json"
APPSETTINGSFILE = "webui_settings.json"

DEFAULT_CONFIG = {
    "multiTask": True,
    "taskCount": 1,
    "proxyAddress": "",
    "messageTemplate": "🤩今日火花+1\r\n",
    "saveDebugArtifacts": False,
    "useProtocolSender": False,
    "protocolDryRun": False,
    "browserSenderAccounts": [],
    "sendStrategy": {
        "shuffleTargets": True,
        "accountStartDelaySecondsMin": 15,
        "accountStartDelaySecondsMax": 60,
        "messageIntervalSecondsMin": 25,
        "messageIntervalSecondsMax": 70,
        "messageVariants": [
            "🤩今日火花+1",
            "今天来补个火花",
            "给你续一下今天的火花",
            "路过给你加个小火花"
        ]
    },
    "dailySendWindow": {
        "enabled": True,
        "startHour": 10,
        "endHour": 18,
        "scheduleIntervalMinutes": 20
    },
    "hitokotoTypes": [
        "文学",
        "影视",
        "诗词",
        "哲学"
    ],
    "happyNewYear": {
        "enabled": False,
        "messageTemplate": "\r\n"
    },
    "friendListScan": {
        "maxScanSeconds": 300,
        "idleScanSeconds": 120,
        "scrollStepPx": 400,
        "scrollDelaySeconds": 0.8
    },
    "persistentBrowserProfiles": {
        "enabled": True,
        "root": "/opt/douyin-sparkflow/state/browser-profiles",
        "seedCookiesWhenEmpty": True,
        "syncStoredCookiesBeforeRun": True,
        "refreshStoredCookiesAfterLogin": True
    }
}

LEGACY_OPS_LOG_FILE = "/var/log/douyin-sparkflow.log"

DEFAULT_APP_SETTINGS = {
    "admin_username": "admin",
    "admin_password_hash": "",
    "session_secret": "",
    "session_max_age_seconds": 8 * 60 * 60,
    "compose_root": "",
    "ui_host": "0.0.0.0",
    "ui_port": 8787,
    "login_poll_interval_seconds": 1,
    "ops_log_file": "/app/logs/douyin-sparkflow.log",
    "proxy_refresh_script": "/opt/douyin-sparkflow/refresh_proxy.sh",
    "local_login_helper_url": "http://127.0.0.1:18765",
    "login_desktop_api_url": "http://127.0.0.1:18090",
    "douyin_network_mode": "direct",
    "douyin_proxy_url": "http://proxy:7890",
    "login_desktop_public_url": "",
    "login_desktop_public_scheme": "http",
    "login_desktop_public_port": 8788,
    "server_host": "",
    "server_username": "",
    "server_password": "",
}

config = None
userData = None
appSettings = None


class Environment(Enum):
    GITHUBACTION = "GITHUB_ACTION"
    LOCAL = "LOCAL"
    PACKED = "PACKED"

    def __str__(self):
        return self.value


def get_environment():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Environment.PACKED
    if os.getenv("GITHUB_ACTIONS") == "true":
        return Environment.GITHUBACTION
    return Environment.LOCAL


def repo_root():
    return Path(__file__).resolve().parents[1]


def _runtime_root():
    env = get_environment()
    if env == Environment.PACKED:
        return Path(sys.executable).resolve().parent
    return repo_root()


def config_path():
    return _runtime_root() / CONFIGFILE


def users_data_path():
    return _runtime_root() / USERDATAFILE


def app_settings_path():
    return _runtime_root() / APPSETTINGSFILE


def default_compose_root():
    root = repo_root()
    parent = root.parent
    if (parent / "docker-compose.yml").exists():
        return str(parent)
    return str(root)


def _merge_defaults(data, defaults):
    merged = deepcopy(defaults)
    for key, value in data.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def _load_json_file(path, defaults=None):
    if not path.exists():
        if defaults is None:
            raise FileNotFoundError(path)
        path.write_text(json.dumps(defaults, ensure_ascii=False, indent=2), encoding="utf-8")
        return deepcopy(defaults)

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return deepcopy(defaults) if defaults is not None else None

    data = json.loads(text)
    if defaults is None:
        return data
    # _merge_defaults only works with dicts; for list-shaped data
    # (e.g. usersData.json) just return the parsed data as-is.
    if not isinstance(data, dict) or not isinstance(defaults, dict):
        return data
    return _merge_defaults(data, defaults)


def _save_json_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def get_config(force_reload=False):
    global config
    if config is None or force_reload:
        config = _load_json_file(config_path(), DEFAULT_CONFIG)
    return deepcopy(config)


def save_config(new_config):
    global config
    config = _merge_defaults(new_config, DEFAULT_CONFIG)
    _save_json_file(config_path(), config)
    return deepcopy(config)


def update_config(
    mutator,
    *,
    path=None,
    force_reload=True,
    timeout=30,
    return_changed=False,
):
    target = Path(path) if path is not None else config_path()
    with FileLock(f"{target}.lock", timeout=timeout):
        current = (
            _load_json_file(target, DEFAULT_CONFIG)
            if path is not None
            else get_config(force_reload=force_reload)
        )
        result = mutator(current)
        changed = True
        if isinstance(result, tuple) and len(result) == 2:
            result, changed = result
        if changed:
            if path is None:
                save_config(current)
            else:
                _save_json_file(target, _merge_defaults(current, DEFAULT_CONFIG))
        if return_changed:
            return deepcopy(result), changed
        return deepcopy(result)


def get_userData(force_reload=False):
    global userData
    if userData is not None and not force_reload:
        return deepcopy(userData)

    env = get_environment()
    if env == Environment.GITHUBACTION:
        raw = os.getenv("USER_DATA", "")
        if not raw:
            logger.error("Environment variable USER_DATA is not set")
            raise RuntimeError("USER_DATA is required in GITHUB_ACTIONS mode")
        userData = json.loads(raw)
    else:
        userData = _load_json_file(users_data_path(), [])

    return deepcopy(userData)


def save_userData(accounts):
    global userData
    normalized = list(accounts)
    userData = normalized
    _save_json_file(users_data_path(), normalized)
    return deepcopy(userData)


DATA_BACKUP_DIR = ".data-backups"
DATA_BACKUP_KEEP = 10


def _snapshot_user_data(target: Path) -> None:
    """Keep a dated copy of account data before a destructive write.

    Account data had no rolling backups at all, so a bad identity match or a
    mistaken delete left nothing to fall back to except a month-old snapshot.
    """
    try:
        if not target.exists():
            return
        content = target.read_text(encoding="utf-8")
        backup_dir = target.parent / DATA_BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)
        destination = backup_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{target.name}"
        if not destination.exists():
            destination.write_text(content, encoding="utf-8")
        keep = sorted(backup_dir.glob(f"*-{target.name}"))
        for stale in keep[:-DATA_BACKUP_KEEP]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        logger.warning("Could not snapshot %s before writing", target, exc_info=True)


def update_user_data(
    mutator,
    *,
    path=None,
    force_reload=True,
    timeout=30,
    return_changed=False,
):
    target = Path(path) if path is not None else users_data_path()
    with FileLock(f"{target}.lock", timeout=timeout):
        accounts = (
            _load_json_file(target, [])
            if path is not None
            else get_userData(force_reload=force_reload)
        )
        result = mutator(accounts)
        changed = True
        if isinstance(result, tuple) and len(result) == 2:
            result, changed = result
        if changed:
            _snapshot_user_data(target)
            if path is None:
                save_userData(accounts)
            else:
                _save_json_file(target, accounts)
        if return_changed:
            return deepcopy(result), changed
        return deepcopy(result)


def normalize_unique_id(unique_id):
    if not unique_id:
        return ""
    digits = "".join(ch for ch in str(unique_id) if ch.isdigit())
    return digits or str(unique_id).strip()


def upsert_user_account(unique_id, username, cookies, targets, extra=None):
    unique_id = normalize_unique_id(unique_id)
    payload = {
        "account_ref": f"acc-{uuid.uuid4().hex}",
        "unique_id": unique_id,
        "username": username,
        "cookies": cookies,
        "targets": list(targets),
    }
    if extra:
        payload.update(extra)

    def mutate(accounts):
        for account in accounts:
            if normalize_unique_id(account.get("unique_id")) == unique_id:
                payload["account_ref"] = (
                    account.get("account_ref") or payload["account_ref"]
                )
                if "enabled" not in payload:
                    payload["enabled"] = account.get("enabled", True)
                account.update(payload)
                return deepcopy(account), True

        if "enabled" not in payload:
            payload["enabled"] = True
        accounts.append(payload)
        return deepcopy(payload), True

    return update_user_data(mutate, force_reload=True)


def delete_user_account(unique_id):
    normalized_id = normalize_unique_id(unique_id)

    def mutate(accounts):
        remaining = [
            item
            for item in accounts
            if normalize_unique_id(item.get("unique_id")) != normalized_id
        ]
        removed = len(accounts) != len(remaining)
        if removed:
            accounts[:] = remaining
        return removed, removed

    return update_user_data(mutate, force_reload=True)


def _migrate_legacy_app_settings(settings):
    """Point a stored legacy trigger-log path at the mounted log file.

    The scheduler streams trigger output into /app/logs/douyin-sparkflow.log,
    while /var/log is not a mounted volume: an install that stored the old
    default would keep the panel reading a file nothing writes to.
    """
    if str(settings.get("ops_log_file") or "").strip() == LEGACY_OPS_LOG_FILE:
        settings["ops_log_file"] = DEFAULT_APP_SETTINGS["ops_log_file"]
    return settings


def get_app_settings(force_reload=False):
    global appSettings
    if appSettings is None or force_reload:
        appSettings = _load_json_file(app_settings_path(), DEFAULT_APP_SETTINGS)
        _migrate_legacy_app_settings(appSettings)
        if not appSettings.get("session_secret"):
            appSettings["session_secret"] = secrets.token_urlsafe(32)
        if not appSettings.get("compose_root"):
            appSettings["compose_root"] = default_compose_root()
        _save_json_file(app_settings_path(), appSettings)
    return deepcopy(appSettings)


def save_app_settings(new_settings):
    global appSettings
    appSettings = _merge_defaults(new_settings, DEFAULT_APP_SETTINGS)
    if not appSettings.get("session_secret"):
        appSettings["session_secret"] = secrets.token_urlsafe(32)
    if not appSettings.get("compose_root"):
        appSettings["compose_root"] = default_compose_root()
    _save_json_file(app_settings_path(), appSettings)
    return deepcopy(appSettings)
