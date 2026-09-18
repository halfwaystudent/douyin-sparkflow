import asyncio
import logging

from core.browser import douyin_network_modes, get_browser
from core.login import collect_login_result
from utils.config import normalize_unique_id


logger = logging.getLogger(__name__)


CATEGORY_LOGIN_REQUIRED = "login_required"
CATEGORY_NETWORK_UNAVAILABLE = "network_unavailable"
CATEGORY_STRUCTURE_CHANGED = "structure_changed"
CATEGORY_EMPTY_RESULT = "empty_result"

CATEGORY_LABELS = {
    CATEGORY_LOGIN_REQUIRED: "登录失效",
    CATEGORY_NETWORK_UNAVAILABLE: "网络不可用",
    CATEGORY_STRUCTURE_CHANGED: "页面结构变化",
    CATEGORY_EMPTY_RESULT: "未读到好友",
}


class FriendScanResult(list):
    """Friend names plus whether the scan provably reached the end of the list.

    Behaves exactly like a list so existing callers keep working, but carries
    ``complete`` so the caller can decide whether the result is strong enough to
    refresh the send-time friend index. When completeness is unknown (for
    example a test double returning a plain list) it stays ``False``.
    """

    complete = False

    def __init__(self, names=(), *, complete=False):
        super().__init__(names)
        self.complete = bool(complete)


class FriendRefreshError(RuntimeError):
    """Friend refresh failure carrying a user-actionable category."""

    def __init__(self, message, *, category=CATEGORY_STRUCTURE_CHANGED):
        super().__init__(message)
        self.category = category


CREATOR_HOME_URL = "https://creator.douyin.com/"
LOGIN_IDENTITY_TIMEOUT_MS = 20000
CHAT_PAGE_URL = "https://creator.douyin.com/creator-micro/data/following/chat"
FRIENDS_TAB_SELECTOR = 'xpath=//*[@id="sub-app"]/div/div/div[1]/div[2]'
# Douyin's chat page is a virtualized list and its generated wrapper classes and
# child indexes change frequently.  Keep semantic/current selectors first, with
# the historical XPath selectors last for older page variants.
FRIEND_ROW_SELECTORS = (
    '#sub-app li[role="listitem"]:has([class*="item-header-name-"])',
    '#sub-app li.semi-list-item:has([class*="item-header-name-"])',
    'xpath=//*[@id="sub-app"]//div[contains(@class, "semi-list-item-body") and .//*[contains(@class, "item-header-name-")]]',
    'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]//div[contains(@class, "semi-list-item-body semi-list-item-body-flex-start")]',
)
SCROLLABLE_FRIENDS_SELECTORS = (
    '#sub-app [role="grid"]',
    '#sub-app .ReactVirtualized__Grid',
    '#sub-app [class*="semi-list"] ul',
    '#sub-app ul > div',
    'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]/div/div/div[3]/div/div/div/ul/div',
)
NO_MORE_SELECTORS = (
    'xpath=//div[contains(@class, "no-more-tip-ftdJnu")]',
    'xpath=//*[@id="sub-app"]//*[contains(normalize-space(.), "没有更多")]',
    'xpath=//*[@id="sub-app"]//*[contains(normalize-space(.), "暂无")]',
)
LOADING_SELECTORS = (
    'xpath=//div[contains(@class, "semi-spin")]',
    '#sub-app [class*="loading"]',
    '#sub-app [class*="Loading"]',
)
FRIEND_NAME_SELECTORS = (
    'xpath=.//*[contains(@class, "item-header-name-")]',
    '[class*="item-header-name-"]',
)
# Backward-compatible aliases for callers/tests that imported the old constants.
TARGET_SELECTOR = FRIEND_ROW_SELECTORS[-1]
SCROLLABLE_FRIENDS_SELECTOR = SCROLLABLE_FRIENDS_SELECTORS[-1]
NO_MORE_SELECTOR = NO_MORE_SELECTORS[0]
LOADING_SELECTOR = LOADING_SELECTORS[0]
FIRST_FRIEND_SELECTOR = FRIEND_ROW_SELECTORS[0]
FRIEND_NAME_SELECTOR = FRIEND_NAME_SELECTORS[0]
LOGIN_MASK_SELECTORS = [".login-mask", ".login-guide-container", ".login-img-code-wrapper"]
NON_LOGIN_DIALOG_DISMISS_TEXTS = (
    "我知道了",
    "知道了",
    "好的",
    "确定",
    "确认",
    "稍后再说",
    "关闭",
)
NON_LOGIN_DIALOG_CLOSE_SELECTORS = (
    ".semi-modal-close",
    'button[aria-label="Close"]',
    'button[aria-label="关闭"]',
    '[aria-label="Close"]',
    '[aria-label="关闭"]',
)
FRIEND_LIST_EMPTY_ROUNDS = 6
FRIEND_LIST_EMPTY_WAIT_SECONDS = 1.5
FRIEND_LIST_READY_TIMEOUT_SECONDS = 60


def update_collection_progress(new_names_count, no_more_visible, scroll_moved, idle_rounds, stuck_rounds, idle_limit=5, stuck_limit=2):
    next_idle_rounds = 0 if new_names_count > 0 else idle_rounds + 1
    next_stuck_rounds = 0 if scroll_moved else stuck_rounds + 1
    should_stop = no_more_visible or next_idle_rounds >= idle_limit or next_stuck_rounds >= stuck_limit
    return should_stop, next_idle_rounds, next_stuck_rounds


async def _ensure_logged_in(page):
    for selector in LOGIN_MASK_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible():
                raise FriendRefreshError(
                    "账号登录已失效，请重新扫码登录",
                    category=CATEGORY_LOGIN_REQUIRED,
                )
        except FriendRefreshError:
            raise
        except Exception:
            continue


async def _dismiss_non_login_dialogs(page):
    for text in NON_LOGIN_DIALOG_DISMISS_TEXTS:
        try:
            locator = page.get_by_text(text, exact=False).first
            if await locator.count() > 0 and await locator.is_visible():
                await locator.click(timeout=3000)
                await asyncio.sleep(1)
                return True
        except Exception:
            continue

    for selector in NON_LOGIN_DIALOG_CLOSE_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible():
                await locator.click(timeout=3000)
                await asyncio.sleep(1)
                return True
        except Exception:
            continue
    return False


async def _click_friends_tab(page):
    await page.wait_for_selector("#sub-app", timeout=FRIEND_LIST_READY_TIMEOUT_SECONDS * 1000)
    try:
        await page.get_by_role("tab", name="朋友私信", exact=True).click(timeout=5000)
        return
    except Exception:
        pass

    try:
        await page.locator(FRIENDS_TAB_SELECTOR).click(timeout=10000)
        return
    except Exception:
        pass

    try:
        await page.get_by_text("朋友私信", exact=True).click(timeout=10000)
        return
    except Exception as exc:
        raise RuntimeError("未找到朋友私信标签") from exc


async def _friend_list_dom_summary(page):
    return await page.evaluate(
        """() => {
            const sub = document.querySelector('#sub-app');
            if (!sub) {
                return { hasSubApp: false, ulCount: 0, liCount: 0, listItemCount: 0, nameSpanCount: 0, text: '' };
            }
            return {
                hasSubApp: true,
                ulCount: sub.querySelectorAll('ul').length,
                liCount: sub.querySelectorAll('li').length,
                listItemCount: sub.querySelectorAll('[class*="list-item"]').length,
                nameSpanCount: sub.querySelectorAll('[class*="item-header-name"]').length,
                text: (sub.innerText || '').split(String.fromCharCode(10)).join(' ').slice(0, 500),
            };
        }"""
    )


async def _first_visible_locator(page, selectors):
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(min(count, 5)):
                item = locator.nth(index)
                if await item.is_visible(timeout=500):
                    return selector, locator
        except Exception:
            continue
    return "", None


async def _wait_for_friend_rows_or_empty(page, timeout_seconds=FRIEND_LIST_READY_TIMEOUT_SECONDS):
    started_at = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - started_at < timeout_seconds:
        await _dismiss_non_login_dialogs(page)
        selector, locator = await _first_visible_locator(page, FRIEND_ROW_SELECTORS)
        if locator:
            logger.debug("Friend list ready via selector %s", selector)
            return selector, locator

        summary = await _friend_list_dom_summary(page)
        if int(summary.get("nameSpanCount") or 0) > 0:
            selector, locator = await _first_visible_locator(page, FRIEND_ROW_SELECTORS)
            if locator:
                return selector, locator

        loading_selector, _ = await _first_visible_locator(page, LOADING_SELECTORS)
        if loading_selector:
            await asyncio.sleep(FRIEND_LIST_EMPTY_WAIT_SECONDS)
            continue

        # A genuinely empty friend list is valid; do not turn it into a timeout.
        text = str(summary.get("text") or "")
        if any(marker in text for marker in ("暂无", "没有更多")):
            return "", None
        await asyncio.sleep(0.5)

    summary = await _friend_list_dom_summary(page)
    # The chat page loaded but never exposed friend rows: a page/selector change,
    # not a transport failure.
    raise FriendRefreshError(
        "friend list did not become ready within timeout; "
        f"dom={summary}",
        category=CATEGORY_STRUCTURE_CHANGED,
    )


async def _wait_for_chat_or_login(page, timeout_seconds=FRIEND_LIST_READY_TIMEOUT_SECONDS):
    if not timeout_seconds:
        timeout_seconds = FRIEND_LIST_READY_TIMEOUT_SECONDS
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        await _ensure_logged_in(page)
        try:
            if await page.locator("#sub-app").count() > 0:
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise FriendRefreshError(
        "chat page did not load within timeout",
        category=CATEGORY_STRUCTURE_CHANGED,
    )


async def collect_friend_names(page):
    await _wait_for_chat_or_login(page)
    await _click_friends_tab(page)
    _, target_locator = await _wait_for_friend_rows_or_empty(page)
    if not target_locator:
        return FriendScanResult()

    found_names = []
    seen_names = set()
    idle_rounds = 0
    stuck_rounds = 0

    while True:
        _, target_locator = await _first_visible_locator(page, FRIEND_ROW_SELECTORS)
        if not target_locator:
            if found_names:
                return FriendScanResult(found_names)
            raise RuntimeError("好友列表已加载但未找到可读取的好友行")

        target_elements = await target_locator.all()
        new_names_count = 0
        for element in target_elements:
            name = ""
            for selector in FRIEND_NAME_SELECTORS:
                try:
                    name = (await element.locator(selector).first.inner_text(timeout=1000)).strip()
                except Exception:
                    continue
                if name:
                    break
            if not name:
                try:
                    name = (await element.inner_text(timeout=1000)).splitlines()[0].strip()
                except Exception:
                    continue
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            found_names.append(name)
            new_names_count += 1

        no_more_selector, _ = await _first_visible_locator(page, NO_MORE_SELECTORS)
        if no_more_selector:
            return FriendScanResult(found_names, complete=True)

        loading_selector, _ = await _first_visible_locator(page, LOADING_SELECTORS)
        if loading_selector:
            await asyncio.sleep(1.5)

        scrollable_element = None
        scrollable_selector = ""
        for selector in SCROLLABLE_FRIENDS_SELECTORS:
            try:
                candidate = page.locator(selector).first
                handle = await candidate.element_handle()
                if not handle:
                    continue
                metrics = await page.evaluate(
                    """(element) => ({
                        clientHeight: element.clientHeight,
                        scrollHeight: element.scrollHeight,
                    })""",
                    handle,
                )
                if int(metrics.get("clientHeight") or 0) > 0:
                    scrollable_element = handle
                    scrollable_selector = selector
                    break
            except Exception:
                continue

        if not scrollable_element:
            if found_names:
                return FriendScanResult(found_names)
            raise RuntimeError("未找到好友列表滚动容器")

        before_top = await page.evaluate("(element) => element.scrollTop", scrollable_element)
        await page.evaluate("(element) => element.scrollTop += 800", scrollable_element)
        await asyncio.sleep(1.5)
        after_top = await page.evaluate("(element) => element.scrollTop", scrollable_element)
        logger.debug(
            "Friend list refresh scroll selector=%s before=%s after=%s names=%s",
            scrollable_selector,
            before_top,
            after_top,
            len(found_names),
        )

        should_stop, idle_rounds, stuck_rounds = update_collection_progress(
            new_names_count=new_names_count,
            no_more_visible=False,
            scroll_moved=after_top > before_top,
            idle_rounds=idle_rounds,
            stuck_rounds=stuck_rounds,
        )
        if should_stop:
            return FriendScanResult(found_names, complete=True)


async def _fetch_account_friends_once(
    account,
    network_mode,
    *,
    auth_only=False,
    identity_timeout_ms=LOGIN_IDENTITY_TIMEOUT_MS,
):
    cookies = list(account.get("cookies") or [])
    playwright = browser = context = page = None
    try:
        playwright, browser = await get_browser(GUI=False, network_mode=network_mode)
        context = await browser.new_context()
        context.set_default_navigation_timeout(120000)
        context.set_default_timeout(120000)
        page = await context.new_page()
        await context.add_cookies(cookies)

        if auth_only:
            # Only the creator home page exposes the logged-in identity, and a
            # redirect away from it is itself proof that the session is invalid.
            await page.goto(CREATOR_HOME_URL, wait_until="commit", timeout=FRIEND_LIST_READY_TIMEOUT_SECONDS * 1000)
            try:
                identity = await collect_login_result(
                    page,
                    context,
                    timeout_ms=max(1000, identity_timeout_ms // 2),
                )
            except FriendRefreshError:
                raise
            except Exception as exc:
                if _left_creator_host(page):
                    # The page bounced away from the creator host: the session is
                    # not usable.
                    raise FriendRefreshError(
                        "Cookie 无法登录抖音创作者中心，请重新获取 Cookie",
                        category=CATEGORY_LOGIN_REQUIRED,
                    ) from exc
                # Still on the creator host but the identity did not render: this
                # is a slow or changed page, so let the caller try other routes
                # instead of blaming the Cookie.
                raise _with_category(
                    exc,
                    classify_refresh_error(exc),
                ) from exc
            # Also require the friend private-message page to be readable, so a
            # session that cannot reach the friend list is not reported healthy.
            await page.goto(CHAT_PAGE_URL, wait_until="commit", timeout=FRIEND_LIST_READY_TIMEOUT_SECONDS * 1000)
            await _wait_for_chat_or_login(page)
            return {
                "unique_id": normalize_unique_id(identity.get("unique_id")),
                "username": str(identity.get("username") or "").strip(),
            }

        await page.goto(CHAT_PAGE_URL, wait_until="commit", timeout=FRIEND_LIST_READY_TIMEOUT_SECONDS * 1000)
        await asyncio.sleep(1)

        friends = await collect_friend_names(page)
        if isinstance(friends, FriendScanResult):
            return friends
        return FriendScanResult(friends)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"刷新好友列表失败，请重试：{exc}") from exc
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                logger.debug("Failed to close friend refresh page", exc_info=True)
        if context:
            try:
                await context.close()
            except Exception:
                logger.debug("Failed to close friend refresh context", exc_info=True)
        if browser:
            try:
                await browser.close()
            except Exception:
                logger.debug("Failed to close friend refresh browser", exc_info=True)
        if playwright:
            try:
                await playwright.stop()
            except Exception:
                logger.debug("Failed to stop friend refresh Playwright", exc_info=True)


async def verify_account_session(account, *, auth_only=False, network_mode=None):
    """Verify a stored cookie set by actually loading the friend chat page.

    Returns ``{"verified": True, "friends": [...], "identity": {...}}`` when the
    login state is usable. Raises :class:`FriendRefreshError` with a user
    actionable category when the login state is not usable or the page could not
    be read.
    """
    cookies = list(account.get("cookies") or [])
    if not cookies:
        raise FriendRefreshError(
            "账号没有保存 Cookie，无法验证登录态",
            category=CATEGORY_LOGIN_REQUIRED,
        )

    modes = [network_mode] if network_mode else douyin_network_modes()
    last_category = CATEGORY_NETWORK_UNAVAILABLE
    last_error = None
    for mode in modes:
        try:
            payload = await _fetch_account_friends_once(
                account,
                mode,
                auth_only=auth_only,
            )
        except FriendRefreshError as exc:
            if exc.category == CATEGORY_LOGIN_REQUIRED:
                raise
            last_category = exc.category
            last_error = exc
            logger.warning("Session verification route=%s failed: %s", mode, exc)
            continue
        except RuntimeError as exc:
            last_category = classify_refresh_error(exc)
            if last_category == CATEGORY_LOGIN_REQUIRED:
                raise FriendRefreshError(
                    str(exc),
                    category=CATEGORY_LOGIN_REQUIRED,
                ) from exc
            last_error = exc
            logger.warning("Session verification route=%s failed: %s", mode, exc)
            continue
        except Exception as exc:
            last_category = classify_refresh_error(exc)
            last_error = exc
            logger.warning("Session verification route=%s failed: %s", mode, exc)
            continue
        if auth_only:
            identity = dict(payload or {})
            friends = []
        else:
            friends = list(payload or [])
            identity = {}
        logger.info(
            "Session verification route=%s identity=%s",
            mode,
            "resolved" if identity.get("unique_id") else "unresolved",
        )
        return {
            "verified": True,
            "friends": friends,
            "identity": identity,
        }

    raise FriendRefreshError(
        f"无法读取好友私信页，无法验证登录态：{last_error}",
        category=last_category,
    )


STRUCTURE_ERROR_MARKERS = (
    "did not become ready",
    "did not load",
    "dom=",
    "好友列表已加载但未找到",
    "未找到好友列表滚动容器",
    "未找到朋友私信标签",
)

NETWORK_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "net::",
    "err_",
    "proxy",
    "connection",
    "connect",
    "dns",
    "目标计算机积极拒绝",
    "network",
    "网络",
)

LOGIN_ERROR_MARKERS = ("login", "cookie", "scan", "登录", "扫码")


def _with_category(error, category):
    """Attach a category to an error without losing its type or message."""
    if category == CATEGORY_LOGIN_REQUIRED:
        return FriendRefreshError(str(error), category=category)
    try:
        setattr(error, "category", category)
        return error
    except (AttributeError, TypeError):
        return FriendRefreshError(str(error), category=category)


def _left_creator_host(page):
    """True when the page is no longer on the Douyin creator host."""
    try:
        current_url = str(page.url or "")
    except Exception:
        return False
    if not current_url:
        return False
    return "creator.douyin.com" not in current_url


def classify_refresh_error(exc):
    """Map a refresh failure to one of the three user-facing categories.

    Categories are attached where the failure is detected; this helper only
    classifies raw/unknown errors, and it distinguishes a loaded page with
    missing DOM from a transport failure.
    """
    if isinstance(exc, FriendRefreshError):
        return exc.category
    text = str(exc or "").lower()
    if any(marker in text for marker in STRUCTURE_ERROR_MARKERS):
        return CATEGORY_STRUCTURE_CHANGED
    if any(marker in text for marker in LOGIN_ERROR_MARKERS):
        return CATEGORY_LOGIN_REQUIRED
    if any(marker in text for marker in NETWORK_ERROR_MARKERS):
        return CATEGORY_NETWORK_UNAVAILABLE
    return CATEGORY_STRUCTURE_CHANGED


async def fetch_account_friends(account):
    cookies = list(account.get("cookies") or [])
    if not cookies:
        raise FriendRefreshError(
            "account has no cookies; scan login QR code first",
            category=CATEGORY_LOGIN_REQUIRED,
        )

    modes = douyin_network_modes()
    last_error = None
    for index, network_mode in enumerate(modes):
        try:
            payload = await _fetch_account_friends_once(account, network_mode)
            friends = list(payload or [])
            complete = bool(getattr(payload, "complete", False))
            logger.info(
                "Friend refresh route=%s count=%s complete=%s attempt=%s/%s",
                network_mode,
                len(friends),
                complete,
                index + 1,
                len(modes),
            )
            if friends or index == len(modes) - 1:
                return FriendScanResult(friends, complete=complete)
            logger.warning(
                "Friend refresh route=%s returned zero friends; trying next route",
                network_mode,
            )
        except FriendRefreshError as exc:
            if exc.category == CATEGORY_LOGIN_REQUIRED:
                raise
            last_error = exc
            logger.warning("Friend refresh route=%s failed; trying next route: %s", network_mode, exc)
        except RuntimeError as exc:
            category = classify_refresh_error(exc)
            if category == CATEGORY_LOGIN_REQUIRED:
                raise FriendRefreshError(str(exc), category=category) from exc
            last_error = _with_category(exc, category)
            logger.warning("Friend refresh route=%s failed; trying next route: %s", network_mode, exc)
        except Exception as exc:
            last_error = _with_category(exc, classify_refresh_error(exc))
            logger.warning("Friend refresh route=%s failed; trying next route: %s", network_mode, exc)
    raise FriendRefreshError(
        f"friend refresh failed after routes {modes}: {last_error}",
        category=classify_refresh_error(last_error),
    )

