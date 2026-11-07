import asyncio
import base64
import hmac
import logging
import os
import shutil
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
import uvicorn
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

from core.login import collect_login_result
from core.streak_state import AUTH_COOKIE_NAMES
from core.login_interaction import InteractionError, LoginInteraction
from core.browser_engine import (
    BrowserEngineError,
    check_engine_expectation,
    prepare_profile_engine,
    resolve_browser_engine,
)
from core.login_profile import (
    AccountProfileError,
    acquire_account_profile_guard,
    ensure_account_profile_directory,
    resolve_account_profile,
    mark_account_profile_pending,
)


logger = logging.getLogger(__name__)


QR_ERROR_SPECS = {
    "qr_page_busy": (503, "登录页正在处理上一个请求，请稍后重试"),
    "qr_not_ready": (202, "登录页尚未生成二维码，请稍后重试"),
    "qr_blank_recovery_exhausted": (503, "登录页空白恢复已用尽，请关闭后重新打开登录工作区"),
    "qr_expired": (409, "二维码已过期，请点击“刷新二维码”"),
    "login_navigation_timeout": (503, "登录页打开超时，请稍后重试"),
}


REMOTE_LOGIN_URL = "https://creator.douyin.com/"
WWW_SELF_URL = "https://www.douyin.com/user/self"
DEFAULT_PROFILE_DIR = (
    Path(__file__).resolve().parents[1] / "state" / "login-profile"
    if os.name == "nt"
    else Path("/data/login-profile")
)
PROFILE_DIR = Path(os.getenv("LOGIN_PROFILE_DIR", str(DEFAULT_PROFILE_DIR))).expanduser()
LOGIN_DESKTOP_MODE = str(
    os.getenv("LOGIN_DESKTOP_MODE", "native" if os.name == "nt" else "novnc")
).strip().lower()
if LOGIN_DESKTOP_MODE not in {"native", "novnc"}:
    LOGIN_DESKTOP_MODE = "native" if os.name == "nt" else "novnc"
IDLE_TIMEOUT_SECONDS = max(300, int(os.getenv("LOGIN_DESKTOP_IDLE_TIMEOUT_SECONDS", "1800")))
STOP_AFTER_EXPORT_SECONDS = max(0, int(os.getenv("LOGIN_DESKTOP_STOP_AFTER_EXPORT_SECONDS", "60")))
STATUS_CACHE_SECONDS = max(1, int(os.getenv("LOGIN_DESKTOP_STATUS_CACHE_SECONDS", "15")))
LOGIN_NETWORK_MODE = str(os.getenv("LOGIN_DESKTOP_PROXY_MODE", "auto")).strip().lower()
if LOGIN_NETWORK_MODE not in {"auto", "direct", "proxy"}:
    LOGIN_NETWORK_MODE = "auto"
LOGIN_PROXY_SERVER = str(os.getenv("LOGIN_DESKTOP_PROXY", "http://proxy:7890")).strip()
LOGIN_PREFLIGHT_TIMEOUT_SECONDS = max(
    3, int(os.getenv("LOGIN_DESKTOP_PREFLIGHT_TIMEOUT_SECONDS", "15"))
)
LOGIN_NETWORK_CACHE_SECONDS = max(
    0, int(os.getenv("LOGIN_DESKTOP_NETWORK_CACHE_SECONDS", "30"))
)
# Everything in the profile that can carry a signed-in session or personal data.
# The HTTP and code caches stay, so the next login page loads warm.
LOGIN_DATA_PATHS = (
    "Default/Cookies*",
    "Default/Network/Cookies*",
    "Default/Network/Trust Tokens*",
    "Default/Local Storage",
    "Default/Session Storage",
    "Default/Sessions",
    "Default/IndexedDB",
    "Default/Service Worker",
    "Default/WebStorage",
    "Default/SharedStorage*",
    "Default/File System",
    "Default/Storage",
    "Default/databases",
    "Default/blob_storage",
    "Default/Login Data*",
    "Default/Web Data*",
    "Default/Account Web Data*",
)
# Upper bound for one GET /qr?wait=N; the WebUI proxy waits a little longer.
QR_MAX_WAIT_SECONDS = 12
# A passive QR read may recover one known renderer failure: a committed creator
# document that remains entirely blank.  This is deliberately short and bounded
# so it cannot turn the QR polling endpoint into an unbounded navigation loop.
QR_BLANK_RECOVERY_GRACE_SECONDS = max(
    1, int(os.getenv("LOGIN_DESKTOP_QR_BLANK_GRACE_SECONDS", "3"))
)
QR_BLANK_RECOVERY_NAVIGATION_TIMEOUT_SECONDS = max(
    1, int(os.getenv("LOGIN_DESKTOP_QR_BLANK_NAVIGATION_TIMEOUT_SECONDS", "8"))
)
QR_BLANK_RECOVERY_SNAPSHOT_TIMEOUT_SECONDS = 1
QR_BLANK_RECOVERY_RENDER_TIMEOUT_SECONDS = 15
GENERIC_WWW_NAMES = {
    "",
    "我的",
    "我",
    "抖音官网账号",
    "抖音创作者中心",
    "精选",
    "推荐",
    "搜索",
    "关注",
    "朋友",
    "直播",
    "放映厅",
    "短剧",
    "小游戏",
    "客户端",
    "通知",
    "私信",
    "投稿",
    "海量优质视频内容",
    "抖音精选电脑版",
}


def _purge_login_data(profile_dir):
    """Delete the signed-in session from a closed profile, keeping its caches."""
    for pattern in LOGIN_DATA_PATHS:
        # Globs cover SQLite companions (-journal, -wal, -shm) and variants.
        for target in Path(profile_dir).glob(pattern):
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink()
            except OSError:
                logger.warning("Could not remove login data %s", target.name, exc_info=True)


def _api_bind_address():
    return str(
        os.getenv("LOGIN_DESKTOP_API_BIND_ADDRESS") or "127.0.0.1"
    ).strip() or "127.0.0.1"


def _is_loopback_bind(value):
    host = str(value or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")


def _load_api_token():
    direct = str(os.getenv("LOGIN_DESKTOP_API_TOKEN") or "").strip()
    if direct:
        return direct
    token_file = str(os.getenv("LOGIN_DESKTOP_API_TOKEN_FILE") or "").strip()
    if not token_file:
        return ""
    try:
        return Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class LoginNetworkError(RuntimeError):
    """Raised when the login browser cannot reach Douyin."""

    def __init__(self, message, *, checks=None):
        super().__init__(message)
        self.checks = checks or {}


def _safe_proxy_label(proxy_server):
    if not proxy_server:
        return ""
    try:
        parsed = urlsplit(proxy_server)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{host}{port}" if host else "configured proxy"
    except ValueError:
        return "configured proxy"


def _probe_login_target(proxy_server=None, timeout_seconds=15):
    """Probe Douyin without inheriting the process proxy environment."""
    if proxy_server:
        handlers = [
            urllib.request.ProxyHandler(
                {"http": proxy_server, "https": proxy_server}
            )
        ]
    else:
        handlers = [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        REMOTE_LOGIN_URL,
        headers={"User-Agent": "DouYinSparkFlow-login-preflight/1"},
    )
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            response.read(256)
            status = int(getattr(response, "status", 200))
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        return {
            "ok": True,
            "status": status,
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        if proxy_server:
            error = error.replace(proxy_server, _safe_proxy_label(proxy_server))
        return {
            "ok": False,
            "error": error[:240],
            "latency_ms": round((time.monotonic() - started) * 1000),
        }


class LoginDesktopManager:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._page_operation_lock = asyncio.Lock()
        self.interaction = LoginInteraction()
        self.playwright = None
        self.context = None
        self.page = None
        self._last_activity = time.monotonic()
        self._status_cache = None
        self._status_checked_at = 0.0
        self._idle_monitor_task = None
        self._scheduled_stop_task = None
        self._network_route = None
        self._network_checks = {}
        self._network_checked_at = 0.0
        self._active_profile_mode = "staging"
        self._active_profile_dir = PROFILE_DIR
        self._account_profile = None
        self._account_selector = None
        self._account_guard = None
        self._interaction_selector = None
        self._engine_selection = None
        # One automatic recovery is allowed for one exact context/page/workspace
        # tuple.  This is not a poll counter: repeated GET /qr calls cannot add
        # more navigations after that tuple has used its one attempt.
        self._qr_blank_recovery = None

    def _clear_qr_blank_recovery(self):
        self._qr_blank_recovery = None

    def _qr_blank_recovery_key(self, page):
        return (id(self.context), id(page), self.interaction.token)

    def _activate_account_profile(self, profile):
        if self._account_guard and self._account_selector != profile.selector:
            raise RuntimeError("account browser profile is still closing")
        ensure_account_profile_directory(profile)
        acquired_here = False
        if not self._account_guard:
            self._account_guard = acquire_account_profile_guard(profile)
            acquired_here = True
        try:
            # Recheck marker ownership after entering the sender's lock domain.
            ensure_account_profile_directory(profile)
            mark_account_profile_pending(profile)
        except BaseException:
            if acquired_here:
                self._release_account_guard()
            raise
        self._account_profile = profile
        self._account_selector = profile.selector
        self._active_profile_mode = "account"
        self._active_profile_dir = profile.profile_dir

    def _release_account_guard(self):
        guard = self._account_guard
        if guard is not None:
            # The sender owns the same FileLock convention.  Releasing never
            # deletes or rewrites the guard sidecar or its metadata sibling.
            guard.release()
        self._account_guard = None

    def _return_to_staging(self):
        if self._account_guard:
            raise RuntimeError("account browser profile is still locked")
        self._active_profile_mode = "staging"
        self._active_profile_dir = PROFILE_DIR
        self._account_profile = None
        self._account_selector = None
        self._engine_selection = None

    def _resolve_checked_engine_for_open(self, profile, expectation):
        """Resolve one immutable request choice without changing live state."""
        selection = resolve_browser_engine(profile.profile_name if profile else None)
        check_engine_expectation(selection, expectation)
        reusing_context = (
            self.context
            and not self._context_is_closed()
            and self._active_profile_mode == ("account" if profile else "staging")
            and (not profile or self._account_selector == profile.selector)
        )
        if (
            reusing_context
            and self._engine_selection is not None
            and selection != self._engine_selection
        ):
            raise BrowserEngineError("active login browser engine does not match request")
        return selection

    async def _retire_closed_account_profile(self):
        if self._active_profile_mode != "account":
            return
        async with self._lock:
            if self.context and not self._context_is_closed():
                return
            self._release_account_guard()
            self._return_to_staging()
            self._status_cache = None
            self._status_checked_at = 0.0

    def _network_payload(self):
        route = dict(self._network_route or {})
        return {
            "mode": LOGIN_NETWORK_MODE,
            "selected": route.get("mode", ""),
            "proxy": _safe_proxy_label(LOGIN_PROXY_SERVER) if route.get("mode") == "proxy" else "",
            "checked_at": route.get("checked_at", ""),
            "checks": dict(self._network_checks),
        }

    def _invalidate_network_route(self):
        self._network_route = None
        self._network_checked_at = 0.0

    async def _select_network_route(self, *, force=False):
        now = time.monotonic()
        if (
            not force
            and self._network_route
            and now - self._network_checked_at < LOGIN_NETWORK_CACHE_SECONDS
        ):
            return dict(self._network_route)

        checks = {}
        candidates = []
        if LOGIN_NETWORK_MODE in {"auto", "direct"}:
            candidates.append(("direct", None))
        if LOGIN_NETWORK_MODE in {"auto", "proxy"} and LOGIN_PROXY_SERVER:
            candidates.append(("proxy", LOGIN_PROXY_SERVER))

        for mode, proxy_server in candidates:
            result = await asyncio.to_thread(
                _probe_login_target,
                proxy_server,
                LOGIN_PREFLIGHT_TIMEOUT_SECONDS,
            )
            checks[mode] = result
            if result.get("ok"):
                route = {
                    "mode": mode,
                    "proxy": _safe_proxy_label(proxy_server),
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                self._network_checks = checks
                self._network_route = route
                self._network_checked_at = now
                return dict(route)

        self._network_checks = checks
        self._network_route = None
        self._network_checked_at = now
        if LOGIN_NETWORK_MODE == "direct":
            message = "无法直连抖音创作者中心，请检查服务器网络出口"
        elif LOGIN_NETWORK_MODE == "proxy":
            message = f"代理 {_safe_proxy_label(LOGIN_PROXY_SERVER)} 无法访问抖音创作者中心"
        else:
            message = "直连和代理都无法访问抖音创作者中心"
        raise LoginNetworkError(message, checks=checks)

    async def network_preflight(self, *, force=True):
        try:
            route = await self._select_network_route(force=force)
            return {
                "ok": True,
                "route": route,
                "network": self._network_payload(),
            }
        except LoginNetworkError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "network": self._network_payload(),
            }

    def mark_activity(self):
        self._last_activity = time.monotonic()
        self._status_cache = None
        self._status_checked_at = 0.0
        if self._scheduled_stop_task and not self._scheduled_stop_task.done():
            self._scheduled_stop_task.cancel()
        self._scheduled_stop_task = None

    async def start_idle_monitor(self):
        if self._idle_monitor_task and not self._idle_monitor_task.done():
            return
        self._idle_monitor_task = asyncio.create_task(self._idle_monitor())

    async def stop_idle_monitor(self):
        if self._idle_monitor_task and not self._idle_monitor_task.done():
            self._idle_monitor_task.cancel()
            await asyncio.gather(self._idle_monitor_task, return_exceptions=True)
        self._idle_monitor_task = None
        if self._scheduled_stop_task and not self._scheduled_stop_task.done():
            self._scheduled_stop_task.cancel()
            await asyncio.gather(self._scheduled_stop_task, return_exceptions=True)
        self._scheduled_stop_task = None

    async def _idle_monitor(self):
        while True:
            await asyncio.sleep(60)
            if self.context and time.monotonic() - self._last_activity >= IDLE_TIMEOUT_SECONDS:
                await self.stop(clear_profile=False)

    def schedule_stop_after_export(self):
        if STOP_AFTER_EXPORT_SECONDS <= 0:
            return
        if self._scheduled_stop_task and not self._scheduled_stop_task.done():
            self._scheduled_stop_task.cancel()

        async def stop_later():
            await asyncio.sleep(STOP_AFTER_EXPORT_SECONDS)
            await self.stop(clear_profile=False)

        self._scheduled_stop_task = asyncio.create_task(stop_later())

    async def reduce_page_activity(self, page):
        try:
            await page.emulate_media(reduced_motion="reduce")
            await page.add_style_tag(
                content="""
                *, *::before, *::after {
                  animation-duration: 0s !important;
                  animation-iteration-count: 1 !important;
                  transition-duration: 0s !important;
                  scroll-behavior: auto !important;
                }
                video, canvas[class*="main-animation"] {
                  visibility: hidden !important;
                }
                """
            )
        except Exception:
            pass

    async def start(self):
        self.mark_activity()
        async with self._lock:
            if self.context and not self._context_is_closed():
                return
            profile_dir = self._active_profile_dir
            try:
                if self._active_profile_mode == "account":
                    if not self._account_profile:
                        raise RuntimeError("account browser profile is missing")
                    self._activate_account_profile(self._account_profile)
                    if self._engine_selection is None:
                        self._engine_selection = resolve_browser_engine(
                            self._account_profile.profile_name
                        )
                    prepare_profile_engine(profile_dir, self._engine_selection)
                else:
                    if self._engine_selection is None:
                        self._engine_selection = resolve_browser_engine(None)
                    profile_dir.mkdir(parents=True, exist_ok=True)
                    # A staging browser that crashed or was killed never went
                    # through stop(), so never start the add-account UI on a login.
                    await asyncio.to_thread(_purge_login_data, profile_dir)
                if self.playwright:
                    await self.playwright.stop()
                    self.playwright = None
                route = await self._select_network_route()
                self.playwright = await async_playwright().start()
                launch_args = [
                    "--start-maximized",
                    "--window-position=0,0",
                    "--window-size=1600,1000",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--disable-features=Translate,MediaRouter,OptimizationHints,AutofillServerCommunication",
                ]
                if os.name != "nt":
                    launch_args.extend(
                        [
                            "--disable-dev-shm-usage",
                            "--no-sandbox",
                            "--disable-gpu",
                            "--disable-gpu-compositing",
                            "--disable-software-rasterizer",
                            "--disable-accelerated-2d-canvas",
                            "--disable-accelerated-video-decode",
                            "--renderer-process-limit=2",
                        ]
                    )
                launch_options = {
                    "headless": False,
                    "accept_downloads": False,
                    "viewport": {"width": 1600, "height": 1000},
                    "args": launch_args,
                }
                if self._active_profile_mode == "staging":
                    launch_options["service_workers"] = "block"
                if route["mode"] == "proxy":
                    launch_options["proxy"] = {"server": LOGIN_PROXY_SERVER}
                else:
                    launch_args.append("--no-proxy-server")
                overrides = dict(self._engine_selection.launch_overrides())
                launch_args.extend(list(overrides.pop("args", [])))
                launch_options.update(overrides)
                self.context = await self.playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    **launch_options,
                )
            except BaseException:
                close_error = await self._close_browser_unlocked()
                if self._active_profile_mode == "account" and close_error is None:
                    self._release_account_guard()
                    self._return_to_staging()
                raise
            def login_popup(page):
                if self.interaction.token and page.context is self.interaction.context:
                    self.page = page

            self.context.on("page", login_popup)
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            try:
                # Fewer animations means the 2-core host renders the QR sooner.
                await self.page.emulate_media(reduced_motion="reduce")
            except Exception:
                pass

    def _context_is_closed(self):
        if not self.context:
            return True
        impl = getattr(self.context, "_impl_obj", None)
        if self._active_profile_mode == "account":
            # Beginning close is not the same as Chromium having closed. Keep
            # wrappers/guard until Playwright's completion future proves it.
            future = getattr(impl, "_closed_future", None) if impl is not None else None
            closed = bool(future is not None and future.done())
        else:
            closed = (
                impl is None
                or getattr(impl, "_closing_or_closed", False)
                or bool((future := getattr(impl, "_closed_future", None)) and future.done())
            )
        if closed:
            # Playwright keeps the wrapper object after context.close().  Drop
            # both wrappers so a later open creates a fresh, isolated context.
            self.context = None
            self.page = None
        return closed

    async def _get_active_page(self):
        await self.ensure_running()
        try:
            if self.page and not self.page.is_closed():
                return self.page
        except Exception:
            pass
        try:
            for candidate in self.context.pages:
                if not candidate.is_closed():
                    self.page = candidate
                    return candidate
        except Exception:
            pass
        self.page = await self.context.new_page()
        return self.page

    async def _close_browser_unlocked(self):
        close_error = None
        if self.page:
            try:
                await self.page.close()
                self.page = None
            except BaseException as exc:
                close_error = close_error or exc
        if self.context:
            try:
                await self.context.close()
                self.context = None
                self.page = None
            except BaseException as exc:
                close_error = close_error or exc
        if self.playwright:
            try:
                await self.playwright.stop()
                self.playwright = None
            except BaseException as exc:
                close_error = close_error or exc
        return close_error

    async def stop(
        self, clear_profile=False, *, preserve_interaction=False, keep_account_profile=False
    ):
        # A close/reset starts a new browser generation. A later workspace must
        # not inherit an exhausted blank-page recovery attempt from this one.
        self._clear_qr_blank_recovery()
        if not preserve_interaction:
            self.interaction.clear()
            self._interaction_selector = None
        async with self._lock:
            close_error = await self._close_browser_unlocked()
            if close_error is not None:
                # Keep the real sender-compatible guard until the browser has
                # demonstrably closed; another browser must not reuse it early.
                raise close_error
            if self._active_profile_mode == "account":
                if not keep_account_profile:
                    self._release_account_guard()
                    self._return_to_staging()
            elif clear_profile and PROFILE_DIR.exists():
                shutil.rmtree(PROFILE_DIR, ignore_errors=True)
                self._engine_selection = None
            elif PROFILE_DIR.exists():
                await asyncio.to_thread(_purge_login_data, PROFILE_DIR)
                self._engine_selection = None
            self._status_cache = None
            self._status_checked_at = 0.0

    async def reset(self):
        self.mark_activity()
        await self.stop(clear_profile=True)
        await self.start()

    async def ensure_running(self):
        if not self.context or self._context_is_closed():
            await self.start()

    async def focus_browser(self):
        self.mark_activity()
        page = await self._get_active_page()
        try:
            await page.bring_to_front()
        except Exception:
            pass
        return {
            "ok": True,
            "url": page.url,
            "mode": LOGIN_DESKTOP_MODE,
            "network": self._network_payload(),
        }

    async def status(self):
        now = time.monotonic()
        if self._active_profile_mode == "account" and (
            not self.context or self._context_is_closed()
        ):
            await self._retire_closed_account_profile()
        if self._status_cache is not None and now - self._status_checked_at < STATUS_CACHE_SECONDS:
            return dict(self._status_cache)

        logged_in = False
        username = ""
        unique_id = ""
        current_url = ""
        login_state_known = True

        if not self.context or self._context_is_closed():
            await self._retire_closed_account_profile()
            payload = {
                "running": False,
                "logged_in": False,
                "login_state": "logged_out",
                "login_state_known": True,
                "username": "",
                "unique_id": "",
                "current_url": "",
                "profile_dir": str(self._active_profile_dir),
                "profile_mode": self._active_profile_mode,
                "network": self._network_payload(),
            }
            self._status_cache = payload
            self._status_checked_at = now
            return dict(payload)

        page = None
        try:
            if self.page and not self.page.is_closed():
                page = self.page
            else:
                for candidate in self.context.pages:
                    if not candidate.is_closed():
                        self.page = candidate
                        page = candidate
                        break
        except Exception:
            # A failed page inspection is not evidence that Chromium exited.
            # Retain its wrappers and the account guard until explicit cleanup
            # proves closure; otherwise a sender could open the same profile.
            if self._active_profile_mode == "account":
                return {
                    "running": True,
                    "logged_in": False,
                    "login_state": "unknown",
                    "login_state_known": False,
                    "username": "",
                    "unique_id": "",
                    "current_url": "",
                    "profile_dir": str(self._active_profile_dir),
                    "profile_mode": "account",
                    "network": self._network_payload(),
                }
            self.page = None
            self.context = None
            await self._retire_closed_account_profile()
            payload = {
                "running": False,
                "logged_in": False,
                "username": "",
                "unique_id": "",
                "current_url": "",
                "profile_dir": str(self._active_profile_dir),
                "profile_mode": self._active_profile_mode,
                "network": self._network_payload(),
            }
            self._status_cache = payload
            self._status_checked_at = now
            return dict(payload)

        if page:
            current_url = page.url
            if not self._page_operation_lock.locked():
                try:
                    result = await collect_login_result(page, self.context, timeout_ms=1000)
                    logged_in = True
                    username = result["username"]
                    unique_id = result["unique_id"]
                except Exception as exc:
                    # The page may simply not expose the identity yet; reporting
                    # "logged out" here made a just-finished QR scan look like a
                    # failure and invited a duplicate save.
                    login_state_known = False
                    logger.warning(
                        "Login state could not be collected from the login page: %s",
                        type(exc).__name__,
                    )
            else:
                # The page is mid-operation (for example refreshing the QR code),
                # so its state is genuinely unknown right now.
                login_state_known = False

        payload = {
            "running": True,
            "logged_in": logged_in,
            "login_state": (
                "logged_in" if logged_in else ("logged_out" if login_state_known else "unknown")
            ),
            "login_state_known": login_state_known,
            "username": username,
            "unique_id": unique_id,
            "current_url": current_url,
            "profile_dir": str(self._active_profile_dir),
            "profile_mode": self._active_profile_mode,
            "network": self._network_payload(),
        }
        self._status_cache = payload
        self._status_checked_at = now
        return dict(payload)

    async def open_login(
        self, *, interaction_key="", account_profile=None, engine_expectation=None
    ):
        self.mark_activity()
        try:
            await asyncio.wait_for(self._page_operation_lock.acquire(), timeout=5)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("login page is busy; retry shortly") from exc
        try:
            # Resolve and validate only after the page lock is held.  The local
            # choice below remains immutable while profile transitions close the
            # old context; start() must not reread mutable policy for this request.
            profile = resolve_account_profile(account_profile) if account_profile is not None else None
            selector = profile.selector if profile else None
            selection = self._resolve_checked_engine_for_open(profile, engine_expectation)
            if (interaction_key and self.interaction.owns(interaction_key)
                    and self._interaction_selector != selector):
                raise RuntimeError("profile selector changed under this interaction key")
            if (interaction_key and self.interaction.owns(interaction_key)
                    and self.context and not self._context_is_closed()
                    and self.interaction.context is self.context
                    and self.page and not self.page.is_closed()
                    and self._active_profile_mode == ("account" if profile else "staging")):
                return {
                    "ok": True, "url": self.page.url, "state": "interactive", "reset": False,
                    "logged_in": False, "profile_mode": self._active_profile_mode,
                    "reusable_session": self._active_profile_mode == "account",
                }
            if interaction_key and self.context and not (
                    profile and self._active_profile_mode == "account"
                    and self._account_selector == selector):
                await self.stop(clear_profile=False)
            if profile:
                if self._active_profile_mode != "account" or self._account_selector != selector:
                    if self.context:
                        await self.stop(clear_profile=False)
                    self._activate_account_profile(profile)
            elif self._active_profile_mode == "account":
                # An add-account request must never revive a previous account
                # after its workspace lease has been released.
                if self.context:
                    await self.stop(clear_profile=False)
                elif self._account_guard:
                    raise RuntimeError("account browser profile is still closing")
                else:
                    self._return_to_staging()
            # Commit only after all request validation and old-context cleanup.
            # This is the selection start() is required to use for this launch.
            self._engine_selection = selection
            result = await self._ensure_login_form_locked()
            if interaction_key:
                await self.interaction.bind(self.context, interaction_key)
                self._interaction_selector = selector
            return {
                "ok": True,
                "url": result["url"],
                "state": result["state"],
                "reset": result["reset"],
                "logged_in": result["logged_in"],
                "network": self._network_payload(),
                "profile_mode": self._active_profile_mode,
                "reusable_session": bool(result.get("reusable_session")),
            }
        finally:
            self._page_operation_lock.release()

    async def interact(self, token, action=None):
        try:
            await asyncio.wait_for(self._page_operation_lock.acquire(), timeout=1)
        except asyncio.TimeoutError as exc:
            raise InteractionError("登录页正在处理上一个操作", 503) from exc
        try:
            page = self.page
            if page and page.is_closed() and self.context:
                page = next((candidate for candidate in self.context.pages if not candidate.is_closed()), None)
                self.page = page
            if action is not None:
                await asyncio.wait_for(self.interaction.act(token, page, action), timeout=5)
                return {"ok": True}
            data = await self.interaction.screenshot(token, page)
            return {"ok": True, "image": base64.b64encode(data).decode("ascii")}
        finally:
            self._page_operation_lock.release()

    async def _login_page_state(self, page, *, probe_timeout_ms=800):
        """Classify the real page state instead of trusting the URL prefix."""
        try:
            current_url = str(page.url or "")
        except Exception:
            current_url = ""
        # Marker used by the deployment contract test: "/creator-micro/" in page.url
        # The guard above already tolerates a closed page, so match on current_url.
        if "/creator-micro/" in current_url:
            return {"state": "logged_in", "logged_in": True, "url": current_url}
        try:
            result = await collect_login_result(page, self.context, timeout_ms=probe_timeout_ms)
        except Exception:
            result = None
        if result and result.get("unique_id"):
            return {
                "state": "logged_in",
                "logged_in": True,
                "url": current_url,
                "identity": result,
            }
        return {
            "state": "login_form" if current_url.startswith(REMOTE_LOGIN_URL) else "unknown",
            "logged_in": False,
            "url": current_url,
        }

    async def clear_login_state(self, *, navigate=True):
        """Drop the persisted Douyin login state in this browser profile."""
        self.mark_activity()
        try:
            await asyncio.wait_for(self._page_operation_lock.acquire(), timeout=5)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("login page is busy; retry shortly") from exc
        try:
            page = await self._get_active_page()
            cleared = await self._clear_login_cookies(page, self.context)
            if navigate:
                await page.goto(
                    self._login_page_url(),
                    wait_until="commit",
                    timeout=30000,
                )
            logger.info(
                "Cleared login state cookies=%s profile=%s",
                cleared,
                PROFILE_DIR,
            )
            return {"ok": True, "cleared": cleared, "url": page.url}
        finally:
            self._page_operation_lock.release()

    async def _clear_login_cookies(self, page, context):
        cleared = 0
        for url in ("https://creator.douyin.com/", "https://www.douyin.com/"):
            try:
                cookies = await context.cookies([url])
            except Exception:
                continue
            for cookie in cookies:
                name = str((cookie or {}).get("name") or "")
                domain = str((cookie or {}).get("domain") or "")
                if not name or "douyin.com" not in domain:
                    continue
                try:
                    await context.clear_cookies(name=name, domain=domain)
                    cleared += 1
                except Exception:
                    try:
                        await context.clear_cookies(name=name)
                        cleared += 1
                    except Exception:
                        logger.debug("Could not clear cookie %s", name, exc_info=True)
        try:
            await page.goto(REMOTE_LOGIN_URL, wait_until="commit", timeout=15000)
        except Exception:
            pass
        return cleared

    def _login_page_url(self):
        return f"{REMOTE_LOGIN_URL}?qr_refresh={int(time.time() * 1000)}"

    async def _goto_login_page(self, page):
        try:
            await page.goto(self._login_page_url(), wait_until="commit", timeout=30000)
            return
        except Exception as exc:
            logger.warning("Opening the Douyin login page failed once; restarting: %s", exc)
        preserved_token = self.interaction.token
        await self.stop(
            clear_profile=False, preserve_interaction=True,
            keep_account_profile=self._active_profile_mode == "account",
        )
        await self.start()
        if preserved_token:
            await self.interaction.bind(self.context, preserved_token)
        page = await self._get_active_page()
        await page.goto(self._login_page_url(), wait_until="commit", timeout=30000)

    async def _ensure_login_form_locked(self):
        """Show staging login or reuse an account-scoped signed-in session."""
        page = await self._get_active_page()
        reset = False
        if self._active_profile_mode == "account":
            current_url = str(page.url or "")
            if not current_url or current_url == "about:blank":
                # Initialize a blank persistent context without clearing any
                # account cookies, local storage, IndexedDB, or service workers.
                await page.goto(REMOTE_LOGIN_URL, wait_until="commit", timeout=30000)
                page = await self._get_active_page()
            state = await self._login_page_state(page)
            if state.get("logged_in"):
                return {
                    "state": "logged_in", "url": state["url"], "reset": False,
                    "logged_in": True, "reusable_session": True,
                }
            if not str(page.url or "").startswith(REMOTE_LOGIN_URL):
                await self._goto_login_page(page)
                page = await self._get_active_page()
            return {
                "state": "login_form", "url": page.url, "reset": False,
                "logged_in": False, "reusable_session": False,
            }
        # A freshly started browser sits on about:blank; there is no session to probe.
        if str(page.url or "").startswith("http"):
            state = await self._login_page_state(page)
            if state.get("logged_in"):
                cleared = await self._clear_login_cookies(page, self.context)
                reset = True
                logger.info("Login page showed an existing session; cleared cookies=%s", cleared)
        await self._goto_login_page(page)
        page = await self._get_active_page()
        return {
            "state": "login_form",
            "url": page.url,
            "reset": reset,
            "logged_in": False,
            "reusable_session": False,
        }

    def workspace_guard(self, interaction_key):
        keyed = bool(interaction_key or self.interaction.token)
        def check(page=None):
            if (keyed and not self.interaction.owns(interaction_key)) or (not keyed and self.interaction.token):
                raise InteractionError("登录工作区已释放，请重新申请", 423)
            if keyed and page is not None:
                self.interaction.check(interaction_key, page)
        return check

    async def refresh_login_qr(self, *, interaction_key=""):
        check = self.workspace_guard(interaction_key)
        check()
        self.mark_activity()
        try:
            await asyncio.wait_for(self._page_operation_lock.acquire(), timeout=5)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("login page is busy; retry shortly") from exc
        try:
            check()
            payload = await self._refresh_login_qr_locked()
            check(self.page)
            return payload
        finally:
            self._page_operation_lock.release()

    async def _extract_login_qr(self, page):
        """Return the visible login QR element, or None when it is absent."""
        for selector in (
            'img[class*="qrcode"]',
            'img[src^="data:image/png;base64"]',
        ):
            try:
                candidates = page.locator(selector)
            except Exception:
                continue
            for index in range(await candidates.count()):
                qr = candidates.nth(index)
                try:
                    if not await qr.is_visible():
                        continue
                    box = await qr.bounding_box()
                    if not box or box["width"] < 120 or box["height"] < 120:
                        continue
                    ratio = box["width"] / max(1, box["height"])
                    if 0.8 <= ratio <= 1.25:
                        return qr
                except Exception:
                    continue
        return None

    async def _blank_login_document_snapshot(self, page):
        """Read only the small set of facts needed to prove a blank document."""
        try:
            return await asyncio.wait_for(
                page.evaluate(
                    r"""() => {
                        const body = document.body;
                        const count = (selector) => document.querySelectorAll(selector).length;
                        const text = typeof body?.innerText === 'string' ? body.innerText.trim() : '';
                        const emptyShell = !!body && body.getBoundingClientRect().height === 0 &&
                            [...body.querySelectorAll('*')].every(el => ['DIV','SPAN','SCRIPT','STYLE','LINK','META','NOSCRIPT'].includes(el.tagName));
                        return {
                            url: location.href,
                            ready_state: document.readyState,
                            body_children: body ? body.children.length : -1,
                            empty_app_shell: emptyShell,
                            text_length: text.length,
                            images: document.images.length,
                            login_controls: count('input, textarea, [contenteditable="true"], button, select'),
                            verification_controls: count('iframe, [role="dialog"], [class*="captcha" i], [class*="verify" i], [class*="sms" i]'),
                            signed_in_markers: count('a[href*="creator-micro"], [data-e2e*="avatar"], [class*="user-info" i], [class*="account" i]'),
                        };
                    }"""
                ),
                timeout=QR_BLANK_RECOVERY_SNAPSHOT_TIMEOUT_SECONDS,
            )
        except Exception:
            # Collector/read failures are unknown, not evidence of a logged-out
            # page.  Only the positive blank shape below may trigger recovery.
            return None

    @staticmethod
    def _is_blank_initial_login_document(snapshot):
        if not isinstance(snapshot, dict):
            return False
        url = str(snapshot.get("url") or "")
        try:
            parsed = urlsplit(url)
            valid_url = parsed.scheme == "https" and parsed.hostname == "creator.douyin.com" and parsed.port in {None, 443} and not parsed.username and not parsed.password and parsed.path in {"", "/"}
        except ValueError:
            valid_url = False
        if not valid_url:
            return False
        return (
            snapshot.get("ready_state") in {"interactive", "complete"}
            and (snapshot.get("body_children") == 0 or snapshot.get("empty_app_shell") is True)
            and snapshot.get("text_length") == 0
            and snapshot.get("images") == 0
            and snapshot.get("login_controls") == 0
            and snapshot.get("verification_controls") == 0
            and snapshot.get("signed_in_markers") == 0
        )

    @staticmethod
    def _qr_recovery_payload(state, *, attempted=False, exhausted=False, category="", message=""):
        return {
            "state": state,
            "attempted": bool(attempted),
            "exhausted": bool(exhausted),
            "category": category,
            "message": message,
        }

    async def _recover_blank_login_page(self, page, *, workspace_check, deadline=None):
        """Navigate once for a proven blank staging login document.

        This never clears cookies or recreates the context.  Account profiles are
        intentionally excluded: an identity collector failure there is not proof
        that a reusable signed-in session is safe to overwrite.
        """
        workspace_check()
        if self._active_profile_mode != "staging" or page is not self.page:
            return self._qr_recovery_payload("not_applicable")
        try:
            if page.is_closed() or page.context is not self.context:
                return self._qr_recovery_payload("not_applicable")
        except Exception:
            return self._qr_recovery_payload("not_applicable")

        snapshot = await self._blank_login_document_snapshot(page)
        if not self._is_blank_initial_login_document(snapshot):
            return self._qr_recovery_payload("not_applicable")

        now = asyncio.get_running_loop().time()
        key = self._qr_blank_recovery_key(page)
        record = self._qr_blank_recovery
        if not record or record["key"] != key:
            record = {"key": key, "blank_since": now, "attempted": False, "exhausted": False}
            self._qr_blank_recovery = record
        if record["attempted"] and not record["exhausted"] and now - record.get("navigated_at", now) < QR_BLANK_RECOVERY_RENDER_TIMEOUT_SECONDS:
            return self._qr_recovery_payload("rendering", attempted=True, category="blank_login_rendering", message="已重载一次登录页，正在等待二维码渲染")
        if record["exhausted"] or record["attempted"]:
            return self._qr_recovery_payload(
                "exhausted", attempted=True, exhausted=True,
                category=record.get("category", "blank_login_recovery_exhausted"),
                message=record.get("message", "登录页空白恢复已用尽，请关闭后重新打开登录工作区"),
            )
        elapsed = now - record["blank_since"]
        if elapsed < QR_BLANK_RECOVERY_GRACE_SECONDS:
            return self._qr_recovery_payload(
                "grace", category="blank_login_waiting",
                message="登录页仍在初始化，正在等待渲染",
            )
        if self._page_operation_lock.locked():
            return self._qr_recovery_payload(
                "busy", category="blank_login_page_busy",
                message="登录页正在处理其他操作",
            )

        lock_timeout = QR_BLANK_RECOVERY_NAVIGATION_TIMEOUT_SECONDS
        if deadline is not None:
            lock_timeout = min(
                lock_timeout,
                max(1, deadline - asyncio.get_running_loop().time()),
            )
        try:
            await asyncio.wait_for(self._page_operation_lock.acquire(), timeout=lock_timeout)
        except asyncio.TimeoutError:
            return self._qr_recovery_payload(
                "busy", category="blank_login_page_busy",
                message="登录页正在处理其他操作",
            )
        try:
            workspace_check()
            # Another reader may have completed the one attempt while this call
            # waited for the serialized page-operation lock.
            if record["attempted"] and not record["exhausted"] and asyncio.get_running_loop().time() - record.get("navigated_at", 0) < QR_BLANK_RECOVERY_RENDER_TIMEOUT_SECONDS:
                return self._qr_recovery_payload("rendering", attempted=True, category="blank_login_rendering", message="已重载一次登录页，正在等待二维码渲染")
            if record["attempted"] or record["exhausted"]:
                return self._qr_recovery_payload(
                    "exhausted", attempted=True, exhausted=True,
                    category=record.get("category", "blank_login_recovery_exhausted"),
                    message=record.get("message", "登录页空白恢复已用尽，请关闭后重新打开登录工作区"),
                )
            if page is not self.page or page.context is not self.context or page.is_closed():
                return self._qr_recovery_payload("not_applicable")
            # A scan or form interaction may have finished after the first
            # snapshot. Re-prove both blank DOM and absence of auth cookies
            # while owning the serialized page-operation lock.
            try:
                cookies = await asyncio.wait_for(self.context.cookies(), timeout=QR_BLANK_RECOVERY_SNAPSHOT_TIMEOUT_SECONDS)
            except Exception:
                return self._qr_recovery_payload("not_applicable")
            if any(isinstance(c, dict) and c.get("name") in AUTH_COOKIE_NAMES and c.get("value") for c in cookies):
                return self._qr_recovery_payload("not_applicable")
            final_snapshot = await self._blank_login_document_snapshot(page)
            workspace_check(page)
            if not self._is_blank_initial_login_document(final_snapshot) or page is not self.page or page.context is not self.context:
                return self._qr_recovery_payload("not_applicable")
            record["attempted"] = True
            record["navigated_at"] = asyncio.get_running_loop().time()
            navigation_timeout = QR_BLANK_RECOVERY_NAVIGATION_TIMEOUT_SECONDS
            if deadline is not None:
                navigation_timeout = min(
                    navigation_timeout,
                    max(1, deadline - asyncio.get_running_loop().time()),
                )
            try:
                await asyncio.wait_for(
                    page.goto(self._login_page_url(), wait_until="commit", timeout=int(navigation_timeout * 1000)),
                    timeout=navigation_timeout + 0.25,
                )
            except (asyncio.TimeoutError, PlaywrightTimeoutError):
                record.update(
                    exhausted=True,
                    category="blank_login_navigation_timeout",
                    message="登录页空白恢复导航超时，请关闭后重新打开登录工作区",
                )
                return self._qr_recovery_payload(
                    "timeout", attempted=True, exhausted=True,
                    category=record["category"], message=record["message"],
                )
            except Exception as exc:
                record.update(
                    exhausted=True,
                    category="blank_login_navigation_failed",
                    message="登录页空白恢复导航失败，请关闭后重新打开登录工作区",
                )
                logger.warning("Blank login recovery navigation failed: %s", type(exc).__name__)
                return self._qr_recovery_payload(
                    "failed", attempted=True, exhausted=True,
                    category=record["category"], message=record["message"],
                )
            workspace_check()
            return self._qr_recovery_payload(
                "navigated", attempted=True,
                category="blank_login_navigation",
                message="检测到空白登录页，已执行一次恢复导航，正在等待二维码",
            )
        finally:
            self._page_operation_lock.release()

    async def _qr_payload(self, page):
        """Classify the current login page for the QR endpoints.

        Always passes through :meth:`_ensure_login_form_locked`, so a workspace
        that still holds an old login session is reported as ``logged_in`` with
        ``reset`` set, instead of waiting for a QR code that never appears.
        """
        prepared = await self._ensure_login_form_locked()
        page = await self._get_active_page()
        deadline = asyncio.get_running_loop().time() + 45
        reset = bool(prepared.get("reset"))
        qr_ready = False
        state = {"state": "unknown", "logged_in": False, "url": page.url}
        while asyncio.get_running_loop().time() < deadline:
            state = await self._login_page_state(page, probe_timeout_ms=600)
            if state.get("logged_in"):
                return {
                    "state": "logged_in",
                    "logged_in": True,
                    "qr_ready": qr_ready,
                    "reset": reset,
                    "url": state["url"],
                }
            qr = await self._extract_login_qr(page)
            if qr is not None:
                qr_ready = True
                await self.reduce_page_activity(page)
                try:
                    await page.wait_for_function(
                        "() => !/\u4e8c\u7ef4\u7801\u5931\u6548|\u4e8c\u7ef4\u7801\u8fc7\u671f/.test(document.body?.innerText || '')",
                        timeout=30000,
                    )
                except Exception:
                    pass
                return {
                    "state": "qr_ready",
                    "logged_in": False,
                    "qr_ready": qr_ready,
                    "reset": reset,
                    "url": page.url,
                }
            await asyncio.sleep(0.75)
        logger.warning(
            "Login QR not ready after 45s url=%s state=%s reset=%s",
            page.url,
            state.get("state"),
            reset,
        )
        return {
            "state": "qr_not_ready",
            "logged_in": False,
            "qr_ready": qr_ready,
            "reset": reset,
            "url": page.url,
        }

    async def _refresh_login_qr_locked(self):
        page = await self._get_active_page()
        explicit_account_reset = self._active_profile_mode == "account"
        if explicit_account_reset:
            # This method is reached only by the explicit refresh POST; passive
            # QR GET keeps an existing account session intact. The durable login
            # fence remains until a verified new generation is saved/imported.
            await self._clear_login_cookies(page, self.context)
            page = await self._get_active_page()
        payload = await self._qr_payload(page)
        if explicit_account_reset:
            payload["reset"] = True
        qr_ready = bool(payload.get("qr_ready"))
        return {
            "ok": qr_ready or bool(payload.get("logged_in")),
            "state": payload["state"],
            "logged_in": payload["logged_in"],
            "qr_ready": qr_ready,
            "reset": payload["reset"],
            "url": payload["url"],
        }

    async def export(self, *, interaction_key=""):
        # A browser opened for a shared workspace must never be exported by an
        # older request, including one that waited behind another page action.
        keyed = bool(interaction_key or self.interaction.token)
        def check_key(page=None):
            if (keyed and not self.interaction.owns(interaction_key)) or (not keyed and self.interaction.token):
                raise InteractionError("登录工作区已释放，请重新申请", 423)
            if keyed and page is not None:
                self.interaction.check(interaction_key, page)

        check_key()
        self.mark_activity()
        try:
            await asyncio.wait_for(self._page_operation_lock.acquire(), timeout=5)
        except asyncio.TimeoutError as exc:
            raise InteractionError("登录页正在处理上一个操作，请稍后保存", 503) from exc
        try:
            check_key()
            page = await self._get_active_page()
            check_key(page)
            context = self.context
            try:
                result = await collect_login_result(page, context, timeout_ms=5000)
            except InteractionError:
                raise
            except Exception:
                check_key(page)
                try:
                    result = await collect_www_login_result(page, context)
                except Exception as exc:
                    check_key(page)
                    raise RuntimeError("无法读取已登录账号，请确认登录已完成后重试") from exc
            check_key(page)
            self.schedule_stop_after_export()
            return result
        finally:
            self._page_operation_lock.release()


def _clean_www_display_name(value):
    raw = str(value or "").replace("\u200b", "").replace("\ufeff", "")
    name = " ".join(raw.split()).strip(" -_｜|·•")
    if not name or name in GENERIC_WWW_NAMES:
        return ""
    if len(name) > 40:
        return ""
    if any(token in name for token in ("登录", "注册", "关注", "粉丝", "获赞", "作品", "喜欢", "收藏", "观看历史", "海量优质视频", "抖音旗下")):
        return ""
    return name


def _is_www_profile_page(value):
    """Whether the fallback is still reading the intended Douyin profile page."""
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.douyin.com"
        and parsed.path.startswith("/user/")
    )


async def collect_www_identity_from_page(page):
    return await page.evaluate(
        r"""() => {
            const normalize = (value) => String(value || "")
                .replace(/[\u200b\u200c\u200d\ufeff]/g, "")
                .replace(/\s+/g, " ")
                .trim();
            const bad = new Set(["", "我的", "我", "抖音官网账号", "精选", "推荐", "搜索", "关注", "朋友", "直播", "放映厅", "短剧", "小游戏", "客户端", "通知", "私信", "投稿"]);
            const candidates = [];
            const add = (value, source) => {
                const text = normalize(value).replace(/^@+/, "").trim();
                if (!text || bad.has(text) || text.length > 40) return;
                if (/登录|注册|关注|粉丝|获赞|作品|喜欢|收藏|观看历史/.test(text)) return;
                candidates.push({ text, source });
            };

            add(document.querySelector('[data-e2e="user-title"]')?.innerText, "data-e2e=user-title");
            add(document.querySelector('[class*="userName"], [class*="UserName"], [class*="nickname"], [class*="Nickname"], h1')?.innerText, "profile-name-selector");
            add(document.title.split(/[｜|\-]/)[0], "document-title");
            add(document.querySelector('meta[property="og:title"]')?.content?.split(/[｜|\-]/)[0], "og:title");

            const selfLink = document.querySelector('a[href*="/user/self"]');
            if (selfLink) {
                const root = selfLink.closest('div')?.parentElement?.parentElement || selfLink;
                const lines = normalize(root.innerText).split(/关注|粉丝|获赞|我的喜欢|我的收藏|观看历史|稍后再看|我的作品|我的预约|我的订单|退出登录/);
                add(lines[0], "self-link-root");
            }

            if (location.pathname.includes('/user/')) {
                add(document.querySelector('meta[name="description"]')?.content?.split(/[，,。|｜-]/)[0], "description");
            }

            return {
                url: location.href,
                title: document.title,
                profileHref: selfLink?.href || "",
                candidates,
            };
        }"""
    )


async def collect_www_login_result(page, context):
    cookies = await context.cookies()
    try:
        await page.goto(WWW_SELF_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    identity = await collect_www_identity_from_page(page)
    username = ""
    if _is_www_profile_page(identity.get("url")):
        for item in identity.get("candidates") or []:
            username = _clean_www_display_name(item.get("text"))
            if username:
                break

    if not username:
        identity = await collect_www_identity_from_page(page)
        if _is_www_profile_page(identity.get("url")):
            for item in identity.get("candidates") or []:
                username = _clean_www_display_name(item.get("text"))
                if username:
                    break

    if not username:
        username = "抖音官网账号"
    # No account ID is derived here: cookie fragments (uid_tt, csrf tokens) are
    # not the platform UID. The Web console reads the trusted UID from the
    # cookies and refuses the save when it cannot (issue #41).
    return {
        "unique_id": "",
        "display_id": "",
        "username": username,
        "cookies": cookies,
        "identity_source": "www_fallback",
    }


manager = LoginDesktopManager()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await manager.start_idle_monitor()
    try:
        yield
    finally:
        await manager.stop_idle_monitor()
        await manager.stop(clear_profile=False)


app = FastAPI(title="Douyin Login Desktop", lifespan=lifespan)


@app.middleware("http")
async def authenticate_internal_api(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)

    expected_token = _load_api_token()
    if expected_token:
        authorization = request.headers.get("authorization", "")
        prefix = "bearer "
        provided = (
            authorization[len(prefix):]
            if authorization.lower().startswith(prefix)
            else ""
        )
        if not provided or not hmac.compare_digest(expected_token, provided):
            return JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
    elif not _is_loopback_bind(_api_bind_address()):
        return JSONResponse(
            {"detail": "Login desktop API token is not configured"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    return await call_next(request)


def _qr_error(
    status_code, code, message, *, retry_after=None, logged_in=False, reset=False,
    retryable=None, recovery=None,
):
    """Return a machine-readable QR result with a user-actionable reason."""
    headers = {"Cache-Control": "no-store"}
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        {
            "ok": False,
            "code": code,
            "state": code,
            "error": message,
            "message": message,
            "retryable": status_code in {202, 503} if retryable is None else bool(retryable),
            "logged_in": bool(logged_in),
            "reset": bool(reset),
            **({"retry_after": int(retry_after)} if retry_after else {}),
            **({"recovery": dict(recovery)} if recovery else {}),
        },
        status_code=status_code,
        headers=headers,
    )


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/preflight")
async def preflight():
    result = await manager.network_preflight(force=True)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result)
    return result


@app.get("/status")
async def status():
    return await manager.status()


@app.post("/open-login")
async def open_login(request: Request):
    try:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict) or set(payload) - {"interaction_key", "account_profile", "engine_expectation"}:
            raise ValueError
        key = payload.get("interaction_key", "")
        selector = payload.get("account_profile")
        expectation = payload.get("engine_expectation")
        if not isinstance(key, str):
            raise ValueError
        kwargs = {}
        if key:
            kwargs["interaction_key"] = key
        if selector is not None:
            kwargs["account_profile"] = selector
        if "engine_expectation" in payload:
            kwargs["engine_expectation"] = expectation
        return await manager.open_login(**kwargs)
    except BrowserEngineError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except AccountProfileError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "登录工作区凭据格式无效"}, status_code=400)
    except InteractionError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)
    except LoginNetworkError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "LOGIN_NETWORK_UNAVAILABLE", "message": str(exc), "checks": exc.checks},
        ) from exc
    except PlaywrightTimeoutError:
        code, message = QR_ERROR_SPECS["login_navigation_timeout"]
        return _qr_error(code, "login_navigation_timeout", message, retry_after=2)
    except RuntimeError:
        # Another request currently owns the login page; report it as retryable
        # instead of letting it surface as an internal server error.
        code, message = QR_ERROR_SPECS["qr_page_busy"]
        return _qr_error(code, "qr_page_busy", message, retry_after=2)


@app.post("/interaction")
async def login_page_interaction(request: Request):
    try:
        raw = await request.body()
        if len(raw) > 16384:
            return JSONResponse({"ok": False, "error": "请求过大"}, status_code=413)
        import json
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) - {"ticket", "action"}:
            raise InteractionError("登录操作格式无效")
        return await manager.interact(payload.get("ticket", ""), payload.get("action"))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "登录操作格式无效"}, status_code=400)
    except InteractionError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)
    except Exception as exc:
        logger.warning("Login interaction unavailable: %s", type(exc).__name__)
        return JSONResponse({"ok": False, "error": "登录窗口暂时不可用，请稍后重试"}, status_code=503)


@app.post("/reset")
async def reset():
    await manager.reset()
    return {"ok": True}


@app.post("/close")
async def close():
    # Drop the signed-in session but keep the browser caches, so the next
    # "add account" loads the login page warm. /reset still wipes everything.
    await manager.stop(clear_profile=False)
    return {"ok": True}


@app.post("/clear-login-state")
async def clear_login_state():
    """Drop persisted Douyin login cookies without wiping the whole profile."""
    try:
        return await manager.clear_login_state()
    except LoginNetworkError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "LOGIN_NETWORK_UNAVAILABLE", "message": str(exc), "checks": exc.checks},
        ) from exc
    except RuntimeError:
        code, message = QR_ERROR_SPECS["qr_page_busy"]
        return _qr_error(code, "qr_page_busy", message, retry_after=2)


@app.post("/focus")
async def focus():
    return await manager.focus_browser()


@app.post("/refresh-qr")
async def refresh_qr(request: Request):
    try:
        raw = await request.body()
        if len(raw) > 1024:
            return JSONResponse({"ok": False, "error": "请求过大"}, status_code=413)
        data = await request.json() if raw else {}
        if not isinstance(data, dict) or set(data) - {"interaction_key"} or not isinstance(data.get("interaction_key", ""), str):
            raise ValueError
        payload = await manager.refresh_login_qr(interaction_key=data.get("interaction_key", ""))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "工作区凭据格式无效"}, status_code=400)
    except InteractionError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)
    except LoginNetworkError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "LOGIN_NETWORK_UNAVAILABLE", "message": str(exc), "checks": exc.checks},
        ) from exc
    except RuntimeError:
        # Another request currently owns the login page.
        code, message = QR_ERROR_SPECS["qr_page_busy"]
        return _qr_error(code, "qr_page_busy", message, retry_after=2)
    if not payload.get("ok"):
        code, message = QR_ERROR_SPECS["qr_not_ready"]
        return _qr_error(
            code,
            "qr_not_ready",
            message,
            retry_after=2,
            reset=bool(payload.get("reset")),
        )
    return payload


@app.post("/export")
async def export(request: Request):
    try:
        body = await request.body()
        if len(body) > 1024:
            return JSONResponse({"ok": False, "error": "请求过大"}, status_code=413)
        payload = await request.json() if body else {}
        if not isinstance(payload, dict) or set(payload) - {"interaction_key"}:
            raise ValueError
        key = payload.get("interaction_key", "")
        if not isinstance(key, str):
            raise ValueError
        result = await manager.export(interaction_key=key)
    except InteractionError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "登录工作区凭据格式无效"}, status_code=400)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "result": result}


async def _probe_login_qr(page):
    """One look at the login page: an expired notice, a QR image, a session, or nothing."""
    try:
        expired = await page.locator('[class*="qrcode_expired"]').count()
        expired_visible = bool(expired) and await page.locator('[class*="qrcode_expired"]').first.is_visible()
    except Exception:
        expired_visible = False
    if expired_visible:
        return "expired", None
    qr = await manager._extract_login_qr(page)
    if qr is not None:
        try:
            data = await qr.screenshot(type="png", timeout=5000)
        except Exception:
            data = None
        if data:
            return "qr", data
    state = await manager._login_page_state(page)
    if state.get("logged_in"):
        return "logged_in", None
    return "not_ready", None


@app.get("/qr")
async def login_qr(request: Request, wait: float = 0):
    try:
        return await _read_login_qr(wait, request.headers.get("X-SparkFlow-Workspace", ""))
    except InteractionError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)


async def _read_login_qr(wait, interaction_key):
    # wait > 0 holds the request until a QR appears (up to the limit) instead of
    # making the browser poll; every page call is bounded by that deadline, so a
    # busy renderer yields "not ready" rather than hanging the request.
    loop = asyncio.get_running_loop()
    check = manager.workspace_guard(interaction_key)
    check()
    deadline = loop.time() + min(max(0.0, wait), QR_MAX_WAIT_SECONDS)
    while manager._page_operation_lock.locked():
        check()
        if loop.time() >= deadline:
            code, message = QR_ERROR_SPECS["qr_page_busy"]
            return _qr_error(code, "qr_page_busy", message, retry_after=1)
        await asyncio.sleep(0.25)
    check()
    if not manager.context or manager._context_is_closed():
        # Reading the QR code must not launch a browser (for example right
        # after /close); only opening the workspace does that.
        code, message = QR_ERROR_SPECS["qr_not_ready"]
        return _qr_error(code, "qr_not_ready", message, retry_after=1)
    try:
        page = await manager._get_active_page()
        check(page)
    except LoginNetworkError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "LOGIN_NETWORK_UNAVAILABLE", "message": str(exc), "checks": exc.checks},
        ) from exc

    outcome, data = "not_ready", None
    recovery = None
    while True:
        remaining = deadline - loop.time()
        try:
            outcome, data = await asyncio.wait_for(_probe_login_qr(page), timeout=max(remaining, 3.0))
        except asyncio.TimeoutError:
            outcome, data = "not_ready", None
        except Exception:
            # The page navigated or closed mid-probe; that is "not ready yet",
            # not an upstream failure that would stop the page's QR wait.
            outcome, data = "not_ready", None
        check(page)
        if outcome == "not_ready":
            recovery = await manager._recover_blank_login_page(
                page, workspace_check=check, deadline=deadline
            )
            check(page)
            if recovery.get("exhausted"):
                break
        if outcome != "not_ready" or loop.time() + 0.5 >= deadline:
            break
        # A page operation (open, refresh) or a closed page ends this wait early.
        if manager._page_operation_lock.locked() or page.is_closed():
            break
        await asyncio.sleep(0.5)

    if outcome == "expired":
        code, message = QR_ERROR_SPECS["qr_expired"]
        return _qr_error(code, "qr_expired", message)
    if outcome == "qr":
        return Response(
            content=data,
            media_type="image/png",
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    if outcome == "logged_in":
        # 202 keeps this retryable end to end: the WebUI proxy forwards JSON for
        # this status instead of wrapping it as an image.
        return _qr_error(
            202,
            "qr_logged_in",
            "检测到浏览器里还保留着登录状态，已重置，正在生成新的二维码",
            retry_after=1,
            logged_in=True,
        )
    if recovery and recovery.get("exhausted"):
        code, message = QR_ERROR_SPECS["qr_blank_recovery_exhausted"]
        return _qr_error(
            code,
            "qr_blank_recovery_exhausted",
            recovery.get("message") or message,
            retryable=False,
            recovery=recovery,
        )
    code, message = QR_ERROR_SPECS["qr_not_ready"]
    return _qr_error(
        code, "qr_not_ready", message, retry_after=1, recovery=recovery
    )


@app.get("/debug/screenshot")
async def debug_screenshot():
    page = await manager._get_active_page()
    data = await page.screenshot(full_page=False, type="png")
    return Response(content=data, media_type="image/png")


@app.get("/debug/snapshot")
async def debug_snapshot():
    page = await manager._get_active_page()
    items = await page.evaluate(
        r"""() => {
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
            };
            const textOf = (el) => String(el.innerText || el.textContent || el.getAttribute('aria-label') || el.title || '').replace(/\s+/g, ' ').trim();
            const nodes = [...document.querySelectorAll('button, a, [role="button"], [aria-label], input, textarea, [contenteditable="true"], [class*="message"], [class*="chat"], [class*="im"]')];
            return nodes.filter(visible).slice(0, 300).map((el, i) => {
                const r = el.getBoundingClientRect();
                return {
                    i,
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || '',
                    aria: el.getAttribute('aria-label') || '',
                    title: el.title || '',
                    text: textOf(el).slice(0, 120),
                    cls: String(el.className || '').slice(0, 120),
                    contenteditable: el.getAttribute('contenteditable') || '',
                    rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}
                };
            });
        }"""
    )
    return {"url": page.url, "title": await page.title(), "items": items}


@app.post("/debug/action")
async def debug_action(request: Request):
    page = await manager._get_active_page()
    payload = await request.json()
    action = payload.get("action")
    if action == "click_text":
        text = str(payload.get("text") or "")
        exact = bool(payload.get("exact", False))
        await page.get_by_text(text, exact=exact).first.click(timeout=int(payload.get("timeout", 5000)))
    elif action == "click_at":
        await page.mouse.click(float(payload["x"]), float(payload["y"]))
    elif action == "wheel":
        await page.mouse.wheel(float(payload.get("dx", 0)), float(payload.get("dy", 0)))
    elif action == "type":
        await page.keyboard.type(str(payload.get("text") or ""), delay=int(payload.get("delay", 20)))
    elif action == "press":
        await page.keyboard.press(str(payload.get("key") or "Enter"))
    elif action == "goto":
        await page.goto(str(payload.get("url") or REMOTE_LOGIN_URL), wait_until="commit", timeout=15000)
    elif action == "eval":
        result = await page.evaluate(str(payload.get("script") or "undefined"))
        return {"ok": True, "result": result, "url": page.url}
    elif action == "list_frames":
        frames = []
        for fr in page.frames:
            frames.append({"url": fr.url, "name": fr.name})
        return {"ok": True, "frames": frames, "url": page.url}
    elif action == "eval_in_frame":
        frame_url_match = str(payload.get("frame_url") or "")
        script = str(payload.get("script") or "undefined")
        target_frame = None
        for fr in page.frames:
            if frame_url_match and frame_url_match in fr.url:
                target_frame = fr
                break
        if not target_frame:
            return {"ok": False, "error": f"no frame matching '{frame_url_match}' found", "url": page.url}
        result = await target_frame.evaluate(script)
        return {"ok": True, "result": result, "url": page.url, "frame_url": target_frame.url}
    elif action == "click_in_frame":
        frame_url_match = str(payload.get("frame_url") or "")
        selector = str(payload.get("selector") or "")
        target_frame = None
        for fr in page.frames:
            if frame_url_match and frame_url_match in fr.url:
                target_frame = fr
                break
        if not target_frame:
            return {"ok": False, "error": f"no frame matching '{frame_url_match}' found"}
        await target_frame.click(selector, timeout=int(payload.get("timeout", 5000)))
        return {"ok": True, "url": page.url, "frame_url": target_frame.url}
    elif action == "type_in_frame":
        frame_url_match = str(payload.get("frame_url") or "")
        selector = str(payload.get("selector") or "")
        text = str(payload.get("text") or "")
        target_frame = None
        for fr in page.frames:
            if frame_url_match and frame_url_match in fr.url:
                target_frame = fr
                break
        if not target_frame:
            return {"ok": False, "error": "no frame found"}
        await target_frame.fill(selector, text, timeout=int(payload.get("timeout", 5000)))
        return {"ok": True, "url": page.url}
    else:
        raise HTTPException(status_code=400, detail=f"unknown action {action!r}")
    return {"ok": True, "url": page.url}


# ---- Network capture (codex-added observability) ----
_net_log = []
_net_capturing = False
_net_max = 500

@app.post("/debug/net_capture")
async def net_capture(request: Request):
    global _net_capturing
    payload = await request.json()
    action_type = payload.get("type", "start")
    page = await manager._get_active_page()
    if action_type == "start":
        _net_log.clear()
        _net_capturing = True

        async def on_request(req):
            if not _net_capturing:
                return
            if len(_net_log) >= _net_max:
                return
            try:
                _net_log.append({
                    "ts": __import__("time").time(),
                    "phase": "request",
                    "method": req.method,
                    "url": req.url,
                    "headers": dict(req.headers),
                })
            except Exception:
                pass

        async def on_response(resp):
            if not _net_capturing:
                return
            if len(_net_log) >= _net_max:
                return
            try:
                body_preview = None
                try:
                    body_preview = (await resp.text())[:300]
                except Exception:
                    pass
                _net_log.append({
                    "ts": __import__("time").time(),
                    "phase": "response",
                    "url": resp.url,
                    "status": resp.status,
                    "headers": dict(resp.headers),
                    "body": body_preview,
                })
            except Exception:
                pass

        page.on("request", lambda req: __import__("asyncio").ensure_future(on_request(req)))
        page.on("response", lambda resp: __import__("asyncio").ensure_future(on_response(resp)))
        return {"ok": True, "msg": "capture started"}
    elif action_type == "stop":
        _net_capturing = False
        return {"ok": True, "count": len(_net_log)}
    elif action_type == "get":
        return {"ok": True, "count": len(_net_log), "log": _net_log.copy()}
    elif action_type == "clear":
        _net_log.clear()
        return {"ok": True}
    return {"ok": False, "error": "unknown type"}

@app.get("/debug/net_log")
async def get_net_log():
    return {"count": len(_net_log), "capturing": _net_capturing, "log": _net_log.copy()}

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=_api_bind_address(),
        port=int(os.getenv("LOGIN_DESKTOP_API_PORT", "18090")),
        reload=False,
    )
