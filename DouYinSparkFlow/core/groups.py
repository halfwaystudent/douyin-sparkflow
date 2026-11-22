"""Read-only collector for creator-centre group conversations."""
import asyncio
from urllib.parse import urlsplit

from core.browser import (account_profile_name, acquire_browser_account_lock, douyin_network_modes,
    first_scrollable_element, get_browser, get_persistent_browser_context, normalize_persistent_profile_config, release_browser_account_lock)
from core import im_ui
from core.friends import (CATEGORY_BROWSER_CLEANUP_BLOCKED, CHAT_PAGE_URL,
    FriendRefreshError, _wait_for_chat_or_login, browser_account_cleanup_blocked,
    retain_browser_account_lock_for_cleanup)
from core.tasks import _read_group_row_conversation_id
from utils.config import get_config

# Every POST the creator chat page makes while loading the group list, as
# captured passively on the live page. Anything else is refused during a
# refresh: the refresh only has to read the list, never change state.
_REFRESH_POST_ALLOWLIST = {
    ('imapi.douyin.com', '/v1/message/get_message_by_init'),
    ('imapi.douyin.com', '/v1/message/get_user_message'),
    ('imapi.douyin.com', '/v1/stranger/get_conversation_list'),
    ('creator.douyin.com', '/web/api/v1/im/token'),
    ('creator.douyin.com', '/aweme/v1/creator/im/user_detail'),
    ('creator.douyin.com', '/aweme/v1/creator/relation/multi/is/follower'),
    ('creator.douyin.com', '/passport/user_info/get_sec_ts'),
    ('creator.douyin.com', '/ttwid/check'),
}


def refresh_request_allowed(method, url):
    """Read-only refresh policy: GET/HEAD/OPTIONS, plus only the exact POSTs the
    chat page needs to render the list. Every other method or POST is refused.
    """
    normalized_method = str(method or '').upper()
    if normalized_method in {'GET', 'HEAD', 'OPTIONS'}:
        return True
    if normalized_method != 'POST':
        return False
    parts = urlsplit(url)
    return ((parts.hostname or '').lower(), parts.path.rstrip('/')) in _REFRESH_POST_ALLOWLIST


async def _activate_group_tab(page, timeout_ms=20000):
    """Switch the chat list to 群消息 and wait until it is really selected.

    Returns the previously selected tab name so the caller can restore it.
    """
    try:
        old = (await page.locator('[role=tab][aria-selected=true]').first.inner_text(timeout=timeout_ms)).strip()
    except Exception as exc:
        # The new SaaS page has no role tabs at all; callers must not wait the
        # locator's default 30s before falling back to the 群聊 filter.
        raise RuntimeError('群消息标签未激活') from exc
    tab = page.get_by_role('tab', name='群消息', exact=True)
    try:
        await tab.wait_for(state='visible', timeout=timeout_ms)
        await tab.click()
        # Playwright's signature is wait_for_function(expression, *, arg=..., timeout=...):
        # the element must go through arg=, not positionally.
        await page.wait_for_function(
            "element => element.getAttribute('aria-selected') === 'true'",
            arg=await tab.element_handle(),
            timeout=timeout_ms,
        )
    except Exception as exc:
        raise RuntimeError('群消息标签未激活') from exc
    return old


GROUP_ROW_SELECTOR = '#sub-app li.semi-list-item'
GROUP_SCROLL_SELECTOR = '#sub-app [role="grid"], #sub-app .ReactVirtualized__Grid, #sub-app [class*="semi-list"] ul, #scroll'

# New IM SaaS group rows: same data-e2e marker as friend rows, but the group
# filter tag is 群聊 under the 消息 group.
SAAS_GROUP_SCROLL_SELECTORS = im_ui.SAAS_SCROLL_SELECTORS


async def _read_saas_group_row(row):
    """Never pair a stale virtual row id with a newly recycled display name."""
    from core import saas_group_guard
    snapshot = await saas_group_guard.read_row_snapshot(row)
    if not snapshot:
        return None, ""
    return snapshot["conversationId"], snapshot["name"]


async def collect_saas_group_rows(page, *, max_scan_seconds=60, scroll_delay_seconds=.1):
    """Passively exhaust the SaaS group-filtered virtual list; never click a row."""
    _, scroll = await first_scrollable_element(page, list(SAAS_GROUP_SCROLL_SELECTORS))
    if not scroll:
        raise RuntimeError('未找到群列表滚动容器')
    reset_top = await page.evaluate('(element) => { element.scrollTop = 0; return element.scrollTop; }', scroll)
    if reset_top != 0:
        raise RuntimeError('群列表滚动位置无法复位')
    # Let the virtual list mount its first page before the first read; rows
    # that materialise later are picked up by the stability passes below.
    await asyncio.sleep(scroll_delay_seconds)
    deadline = asyncio.get_running_loop().time() + max_scan_seconds
    result, seen = [], set()
    # Bottom-reaching passes that added nothing. One quiet pass is not proof:
    # the virtual list lazy-renders pages and may still have unfetched rows,
    # so completion needs two consecutive stable reads at the bottom.
    quiet_bottom_passes = 0
    while True:
        if asyncio.get_running_loop().time() > deadline:
            raise RuntimeError('群列表读取超时')
        added = 0
        for row in await page.locator(im_ui.SAAS_ROW_SELECTOR).all():
            if not await row.is_visible():
                continue
            from core import saas_group_guard
            metadata = await saas_group_guard.read_row_metadata(row)
            if metadata and metadata["type"] in {1, 3, 4, 17}:
                continue
            conversation_id = metadata.get("conversationId") if metadata and metadata["type"] == 2 else None
            name = metadata.get("name", "") if metadata else ""
            if not conversation_id:
                raise RuntimeError('visible group row has no conversation id')
            if conversation_id not in seen:
                if not name:
                    raise RuntimeError('visible group row has no name')
                seen.add(conversation_id)
                result.append({'name': name, 'conversationId': conversation_id})
                added += 1
        state = await page.evaluate('''async e => { e.scrollTop += Math.max(1,e.clientHeight); await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))); return {top:e.scrollTop, client:e.clientHeight, height:e.scrollHeight} }''', scroll)
        await asyncio.sleep(scroll_delay_seconds)
        at_bottom = state['top'] + state['client'] >= state['height'] - 1
        if at_bottom and not added:
            quiet_bottom_passes += 1
            if quiet_bottom_passes >= 2:
                if not result:
                    raise RuntimeError('未读到群列表')
                return result
        else:
            quiet_bottom_passes = 0

async def collect_group_rows(page, *, max_scan_seconds=60, scroll_delay_seconds=.1):
    """Passively exhaust virtualized visible rows; never click a conversation."""
    _, scroll = await first_scrollable_element(page, [GROUP_SCROLL_SELECTOR])
    if not scroll:
        raise RuntimeError('未找到群列表滚动容器')
    reset_top = await page.evaluate('(element) => { element.scrollTop = 0; return element.scrollTop; }', scroll)
    if reset_top != 0:
        raise RuntimeError('群列表滚动位置无法复位')
    deadline = asyncio.get_running_loop().time() + max_scan_seconds
    result, seen = [], set()
    while True:
        if asyncio.get_running_loop().time() > deadline:
            raise RuntimeError('群列表读取超时')
        added = 0
        for row in await page.locator(GROUP_ROW_SELECTOR).all():
            if not await row.is_visible():
                continue
            conversation_id = await _read_group_row_conversation_id(row)
            if not conversation_id:
                raise RuntimeError('visible group row has no conversation id')
            if conversation_id not in seen:
                name = (await row.locator('[class*="item-header-name-"]').first.inner_text()).strip()
                if not name:
                    raise RuntimeError('visible group row has no name')
                seen.add(conversation_id); result.append({'name': name, 'conversationId': conversation_id}); added += 1
        state = await page.evaluate('''async e => { e.scrollTop += Math.max(1,e.clientHeight); await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))); return {top:e.scrollTop, client:e.clientHeight, height:e.scrollHeight} }''', scroll)
        await asyncio.sleep(scroll_delay_seconds)
        if state['top'] + state['client'] >= state['height'] - 1 and not added:
            if not result:
                raise RuntimeError('未读到群列表')
            return result

async def _legacy_group_ui_present(page):
    """True once the legacy 群消息 tab or legacy group rows are really there."""
    try:
        if await page.get_by_role('tab', name='群消息', exact=True).count() > 0:
            return True
        for row in await page.locator(GROUP_ROW_SELECTOR).all():
            if await row.is_visible():
                return True
    except Exception:
        pass
    return False


async def _wait_for_visible_rows(page, selector, *, timeout_seconds=20):
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if any([await row.is_visible() for row in await page.locator(selector).all()]):
            return
        await asyncio.sleep(.1)
    raise RuntimeError('未读到群列表')


async def collect_groups_on_chat_page(page, *, timeout_ms=20000):
    """Read the group list on an already-open creator chat page.

    Routes to the SaaS 群聊 filter on the new IM page and keeps the legacy
    群消息 tab path for the old one, so a caller never has to guess the page
    generation. Only switches the list filter and scrolls it; never opens a
    conversation, so nothing is marked read or sent.
    """
    if await im_ui.wait_for_saas_ui(
        page, timeout_ms=timeout_ms, legacy_probe=lambda: _legacy_group_ui_present(page)
    ):
        activated = await im_ui.activate_saas_filter(page, 'group')
        if not activated:
            raise RuntimeError('新版 IM 群聊筛选状态无法确认')
        await _wait_for_visible_rows(page, im_ui.SAAS_ROW_SELECTOR)
        return await collect_saas_group_rows(page)
    previous = await _activate_group_tab(page)
    try:
        await _wait_for_visible_rows(page, GROUP_ROW_SELECTOR)
        return await collect_group_rows(page)
    finally:
        if previous:
            try:
                await page.get_by_role('tab', name=previous, exact=True).click()
            except Exception:
                pass


async def fetch_account_groups(account):
    """Open the account profile, select 群消息, scan only DOM facts, and restore tab."""
    cookies = list(account.get('cookies') or [])
    account_name = str(account.get('username') or account.get('unique_id') or '')
    lock_handle = lock_path = lock_token = playwright = browser = context = page = None
    cookie_session = None
    from core import tasks
    cookie_operations = set()
    cookie_owner_token = tasks._BROWSER_OPERATION_OWNER.set(cookie_operations)
    try:
        if browser_account_cleanup_blocked(account):
            raise FriendRefreshError('浏览器清理尚未确认完成，账号已暂停刷新',
                                     category=CATEGORY_BROWSER_CLEANUP_BLOCKED)
        profile = normalize_persistent_profile_config(get_config(force_reload=True) or {})
        mode = douyin_network_modes()[0]
        # Every refresh browser session holds the same per-account lock as the
        # sender, whichever browser mode is configured.
        lock_handle, lock_path, lock_token = await acquire_browser_account_lock(account, account_name)
        if profile['enabled']:
            playwright, context, profile_dir = await get_persistent_browser_context(account_profile_name(account), root=profile['root'], network_mode=mode)
            from core.profile_cookies import prepare_profile_cookies
            from core.tasks import _run_browser_stage
            cookie_session = await _run_browser_stage(
                'seed_cookies', prepare_profile_cookies(context, account, profile, profile_dir))
        else:
            playwright, browser = await get_browser(GUI=False, network_mode=mode, profile_name=account_profile_name(account)); context = await browser.new_context(); await context.add_cookies(cookies)
        async def guard_write_routes(route):
            method = route.request.method
            url = route.request.url
            if (refresh_request_allowed(method, url)
                    or im_ui.saas_request_allowed(method, url)):
                await route.continue_()
            else:
                await route.abort()

        await context.route('**/*', guard_write_routes)
        await context.route_web_socket('**/*', im_ui.deny_refresh_socket)
        page = await context.new_page(); await page.goto(CHAT_PAGE_URL, wait_until='commit', timeout=60000); await _wait_for_chat_or_login(page)

        # Reading group rows is not an account-identity proof. Do not publish
        # this profile's cookies into the account store from a list-only job.
        return await collect_groups_on_chat_page(page)
    finally:
        try:
            cancelled = False
            resources_complete = True
            for resource, method in (
                (page, 'close'), (context, 'close'), (browser, 'close'), (playwright, 'stop'),
            ):
                if resource is None:
                    continue
                try:
                    await getattr(resource, method)()
                except asyncio.CancelledError:
                    cancelled = True
                    resources_complete = False
                except Exception:
                    resources_complete = False
            pending_cookie_operations = any(not task.done() for task in cookie_operations)
            if lock_handle is not None:
                if resources_complete and not pending_cookie_operations:
                    release_browser_account_lock(lock_handle, lock_path, lock_token, account_name)
                else:
                    retain_browser_account_lock_for_cleanup(
                        account, lock_handle, lock_path, lock_token, account_name)
            if cancelled:
                raise asyncio.CancelledError
        finally:
            tasks._BROWSER_OPERATION_OWNER.reset(cookie_owner_token)
