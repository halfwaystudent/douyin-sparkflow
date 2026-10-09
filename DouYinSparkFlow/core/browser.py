import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout as FileLockTimeout
from playwright.async_api import async_playwright
from rich.console import Console

from utils.config import (
    DEBUG,
    Environment,
    get_app_settings,
    get_environment,
    normalize_unique_id,
)


console = Console()
# Attach to the logger the app configures (core.tasks / core.friends call
# setup_logger). Looking it up by name keeps this module free of import-time
# side effects such as creating logs/.
logger = logging.getLogger("app")
PLAYWRIGHT_BROWSERS_PATH = "../chrome"
DEFAULT_PROFILE_ROOT = "/opt/douyin-sparkflow/state/browser-profiles"
BROWSER_ACCOUNT_LOCK_DIR = "logs/browser-account-locks"


def _local_browser_bundle_path():
    return Path(__file__).resolve().parent / PLAYWRIGHT_BROWSERS_PATH


def configure_playwright_environment():
    if os.getenv("PLAYWRIGHT_BROWSERS_PATH"):
        return

    env = get_environment()
    if env == Environment.PACKED:
        bundle_path = Path(sys.executable).resolve().parent / PLAYWRIGHT_BROWSERS_PATH
    else:
        bundle_path = _local_browser_bundle_path()

    if bundle_path.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundle_path.resolve())


def _headless_for(GUI=False):
    headful_env = str(os.getenv("SPARKFLOW_BROWSER_HEADFUL") or "").strip().lower()
    if headful_env in {"1", "true", "yes", "on"}:
        return False

    headless = not GUI
    if get_environment() == Environment.LOCAL and DEBUG:
        headless = False
    return headless


def _browser_args():
    return [
        "--disable-dev-shm-usage",
        "--no-sandbox",
    ]


def _douyin_network_mode():
    settings = get_app_settings(force_reload=True)
    return str(
        os.getenv("SPARKFLOW_DOUYIN_NETWORK_MODE")
        or settings.get("douyin_network_mode", "direct")
    ).strip().lower()


def douyin_network_modes():
    # Direct is the default; Mihomo is the fallback unless explicitly selected.
    mode = _douyin_network_mode()
    if mode == "mihomo":
        return ("mihomo",)
    return ("direct", "mihomo")


def _douyin_browser_proxy(network_mode=None):
    # Return an explicit proxy URL for Douyin traffic, or None for direct.
    settings = get_app_settings(force_reload=True)
    mode = str(network_mode or _douyin_network_mode()).strip().lower()
    if mode != "mihomo":
        return None
    return str(
        os.getenv("SPARKFLOW_DOUYIN_PROXY_URL")
        or settings.get("douyin_proxy_url", "http://proxy:7890")
    ).strip() or None


def _browser_launch_options(GUI=False, network_mode=None):
    args = _browser_args()
    proxy = _douyin_browser_proxy(network_mode=network_mode)
    if proxy:
        return {
            "headless": _headless_for(GUI),
            "args": args,
            "proxy": {"server": proxy},
        }
    args.append("--no-proxy-server")
    return {
        "headless": _headless_for(GUI),
        "args": args,
    }


async def select_douyin_network_mode(target_url):
    # Select the first route that can load the target before a task starts.
    failures = []
    for network_mode in douyin_network_modes():
        playwright = browser = page = None
        try:
            playwright, browser = await get_browser(network_mode=network_mode)
            page = await browser.new_page()
            response = await page.goto(target_url, wait_until="commit", timeout=30000)
            status = response.status if response is not None else None
            if status is not None and status < 500:
                return network_mode
            failures.append(f"{network_mode}: HTTP {status}")
        except Exception as exc:
            failures.append(f"{network_mode}: {exc}")
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if playwright:
                try:
                    await playwright.stop()
                except Exception:
                    pass
    raise RuntimeError(f"Douyin network preflight failed: {'; '.join(failures)}")


def sanitize_profile_name(value):
    raw = str(value or "").strip()
    if not raw:
        raw = "unknown"
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", raw)
    safe = safe.strip("._-") or "unknown"
    return safe[:80]


def browser_profile_root(root=None):
    configured = (
        root
        or os.getenv("SPARKFLOW_BROWSER_PROFILE_ROOT")
        or DEFAULT_PROFILE_ROOT
    )
    return Path(configured)


def account_profile_name(user):
    """Stable per-account profile directory name, shared by sender and refresher."""
    account = user or {}
    unique_id = normalize_unique_id(account.get("unique_id"))
    username = str(account.get("username") or "").strip()
    if unique_id:
        return f"uid-{unique_id}"
    if username:
        return f"user-{sanitize_profile_name(username)}"
    return "unknown"


def normalize_persistent_profile_config(active_config):
    """Read persistent-profile settings; the sender and refresher share defaults."""
    raw = (active_config or {}).get("persistentBrowserProfiles", {}) or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "root": str(
            os.getenv("SPARKFLOW_BROWSER_PROFILE_ROOT")
            or raw.get("root")
            or DEFAULT_PROFILE_ROOT
        ),
        "seedCookiesWhenEmpty": bool(raw.get("seedCookiesWhenEmpty", True)),
        "syncStoredCookiesBeforeRun": bool(raw.get("syncStoredCookiesBeforeRun", True)),
        "refreshStoredCookiesAfterLogin": bool(
            raw.get("refreshStoredCookiesAfterLogin", True)
        ),
    }


async def first_scrollable_element(page, selectors, *, probe_timeout_ms=2000):
    """Return ``(selector, handle)`` for the first element that can scroll.

    Douyin renders several zero-height ``[role=grid]`` nodes next to the real
    list, so a plain ``locator.first`` silently picks a hidden one and every
    scroll is a no-op. Iterate the matches, prefer one that actually has content
    below the fold, and only fall back to a rendered-but-unscrollable container
    (a list short enough to fit entirely). The short probe timeout also keeps a
    stale selector from burning the full navigation budget.
    """
    rendered = []
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            continue
        for index in range(min(count, 8)):
            try:
                handle = await locator.nth(index).element_handle(
                    timeout=probe_timeout_ms
                )
            except Exception:
                continue
            if not handle:
                continue
            try:
                metrics = await page.evaluate(
                    """(element) => ({
                        clientHeight: element.clientHeight,
                        scrollHeight: element.scrollHeight,
                    })""",
                    handle,
                )
            except Exception:
                continue
            client_height = int(metrics.get("clientHeight") or 0)
            if client_height <= 0:
                continue
            if int(metrics.get("scrollHeight") or 0) > client_height:
                # A container with content below the fold is the real list.
                return selector, handle
            rendered.append((selector, handle))
    if rendered:
        # Nothing on this page can scroll: every row is already rendered.
        return rendered[0]
    return "", None


def browser_account_lock_name(user):
    """Stable lock-file stem for the browser session that drives one account."""
    identity = str(
        (user or {}).get("unique_id") or (user or {}).get("username") or "unknown"
    ).strip()
    return "".join(
        ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in identity
    )[:80]


async def acquire_browser_account_lock(
    user,
    account_name,
    *,
    wait_seconds=7200,
    poll_seconds=5,
    lock_dir=None,
):
    """Take the per-account browser lock that the sender also holds.

    A persistent profile can only be driven by one browser at a time, so the
    friend refresher must share this lock instead of racing the sender.
    """
    directory = Path(lock_dir or BROWSER_ACCOUNT_LOCK_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f"{browser_account_lock_name(user)}.lock"
    guard = FileLock(f"{lock_path}.guard", timeout=0)
    started_at = asyncio.get_running_loop().time()
    last_logged_at = 0

    while True:
        try:
            guard.acquire()
            token = uuid.uuid4().hex
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "token": token,
                        "account": account_name,
                        "createdAt": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            logger.debug(
                "Acquired browser account lock for %s at %s", account_name, lock_path
            )
            return guard, lock_path, token
        except FileLockTimeout:
            now = asyncio.get_running_loop().time()
            if now - started_at > wait_seconds:
                raise RuntimeError(
                    f"timed out waiting for browser account lock for {account_name}"
                )
            if now - last_logged_at >= 30:
                logger.info(
                    "Waiting for existing browser account lock for %s at %s",
                    account_name,
                    lock_path,
                )
                last_logged_at = now
            await asyncio.sleep(poll_seconds)
        except Exception:
            guard.release()
            raise


def release_browser_account_lock(handle, lock_path, token, account_name):
    try:
        try:
            current = json.loads(lock_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            current = {}
        if str(current.get("token") or "") == token:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
    finally:
        try:
            handle.release()
            logger.debug(
                "Released browser account lock for %s at %s", account_name, lock_path
            )
        except Exception:
            pass


async def install_browser():
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        console.print("[bold green]Browser install completed. Please run the command again.[/bold green]")
    except subprocess.CalledProcessError as exc:
        console.print(f"[bold red]Browser install failed: {exc}[/bold red]")


async def get_browser(GUI=False, network_mode=None):
    configure_playwright_environment()
    playwright = None

    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(**_browser_launch_options(GUI, network_mode=network_mode))
        return playwright, browser
    except Exception as exc:
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
        if "Executable doesn't exist" in str(exc) and get_environment() != Environment.GITHUBACTION:
            console.print("[bold red]Playwright browser is missing.[/bold red]")
            await install_browser()
            raise RuntimeError(
                "Playwright browser is missing; install Chromium before retrying"
            ) from exc
        traceback.print_exc()
        raise


async def get_persistent_browser_context(profile_name, GUI=False, root=None, network_mode=None):
    configure_playwright_environment()

    profile_dir = browser_profile_root(root) / sanitize_profile_name(profile_name)
    profile_dir.mkdir(parents=True, exist_ok=True)

    playwright = None
    try:
        playwright = await async_playwright().start()
        launch_options = _browser_launch_options(GUI, network_mode=network_mode)
        launch_options["viewport"] = {"width": 1600, "height": 1000}
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            **launch_options,
        )
        return playwright, context, profile_dir
    except Exception as exc:
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
        if "Executable doesn't exist" in str(exc) and get_environment() != Environment.GITHUBACTION:
            console.print("[bold red]Playwright browser is missing.[/bold red]")
            await install_browser()
            raise RuntimeError(
                "Playwright browser is missing; install Chromium before retrying"
            ) from exc
        traceback.print_exc()
        raise
