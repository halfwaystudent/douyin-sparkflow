import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

from playwright.async_api import async_playwright
from rich.console import Console

from utils.config import DEBUG, Environment, get_app_settings, get_environment


console = Console()
PLAYWRIGHT_BROWSERS_PATH = "../chrome"
DEFAULT_PROFILE_ROOT = "/opt/douyin-sparkflow/state/browser-profiles"


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
                await page.close()
            if browser:
                await browser.close()
            if playwright:
                await playwright.stop()
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


async def install_browser():
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        console.print("[bold green]Browser install completed. Please run the command again.[/bold green]")
    except subprocess.CalledProcessError as exc:
        console.print(f"[bold red]Browser install failed: {exc}[/bold red]")


async def get_browser(GUI=False, network_mode=None):
    configure_playwright_environment()

    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(**_browser_launch_options(GUI, network_mode=network_mode))
        return playwright, browser
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) and get_environment() != Environment.GITHUBACTION:
            console.print("[bold red]Playwright browser is missing.[/bold red]")
            await install_browser()
            sys.exit(1)
        traceback.print_exc()
        raise


async def get_persistent_browser_context(profile_name, GUI=False, root=None, network_mode=None):
    configure_playwright_environment()

    profile_dir = browser_profile_root(root) / sanitize_profile_name(profile_name)
    profile_dir.mkdir(parents=True, exist_ok=True)

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
        if "Executable doesn't exist" in str(exc) and get_environment() != Environment.GITHUBACTION:
            console.print("[bold red]Playwright browser is missing.[/bold red]")
            await install_browser()
            sys.exit(1)
        traceback.print_exc()
        raise
