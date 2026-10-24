"""Read-only collector for creator-centre group conversations."""
import asyncio
from urllib.parse import urlsplit

from core.browser import (account_profile_name, acquire_browser_account_lock, douyin_network_modes,
    first_scrollable_element, get_browser, get_persistent_browser_context, normalize_persistent_profile_config, release_browser_account_lock)
from core.friends import CHAT_PAGE_URL, _wait_for_chat_or_login
from core.streak_state import AUTH_COOKIE_NAMES
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
    old = (await page.locator('[role=tab][aria-selected=true]').first.inner_text()).strip()
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

async def fetch_account_groups(account):
    """Open the account profile, select 群消息, scan only DOM facts, and restore tab."""
    cookies = list(account.get('cookies') or [])
    account_name = str(account.get('username') or account.get('unique_id') or '')
    lock_handle = lock_path = lock_token = playwright = browser = context = page = None
    try:
        profile = normalize_persistent_profile_config(get_config(force_reload=True) or {})
        mode = douyin_network_modes()[0]
        # Every refresh browser session holds the same per-account lock as the
        # sender, whichever browser mode is configured.
        lock_handle, lock_path, lock_token = await acquire_browser_account_lock(account, account_name)
        if profile['enabled']:
            playwright, context, _ = await get_persistent_browser_context(account_profile_name(account), root=profile['root'], network_mode=mode)
            if profile['syncStoredCookiesBeforeRun']:
                await context.add_cookies(cookies)
            elif profile['seedCookiesWhenEmpty']:
                current = await context.cookies(['https://creator.douyin.com/'])
                if not any(c.get('name') in AUTH_COOKIE_NAMES for c in current): await context.add_cookies(cookies)
        else:
            playwright, browser = await get_browser(GUI=False, network_mode=mode); context = await browser.new_context(); await context.add_cookies(cookies)
        async def guard_write_routes(route):
            if refresh_request_allowed(route.request.method, route.request.url):
                await route.continue_()
            else:
                await route.abort()

        await context.route('**/*', guard_write_routes)
        page = await context.new_page(); await page.goto(CHAT_PAGE_URL, wait_until='commit', timeout=60000); await _wait_for_chat_or_login(page)
        old = await _activate_group_tab(page)
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            rows = page.locator(GROUP_ROW_SELECTOR)
            if any([await row.is_visible() for row in await rows.all()]):
                break
            await asyncio.sleep(.1)
        else:
            raise RuntimeError('未读到群列表')
        rows = await collect_group_rows(page)
        if old:
            try: await page.get_by_role('tab', name=old, exact=True).click()
            except Exception: pass
        return rows
    finally:
        if page: await page.close()
        if context: await context.close()
        if browser: await browser.close()
        if playwright: await playwright.stop()
        if lock_handle is not None: release_browser_account_lock(lock_handle, lock_path, lock_token, account_name)
