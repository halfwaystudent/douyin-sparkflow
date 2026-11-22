import time
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import login_desktop_server as desktop
from core import im_ui, login, tasks


FILTER_BAR = '''
<div class="conversationFilterTagBarwrapper" {attrs}>
  <div class="conversationFilterTagBargroup">
    <span class="conversationFilterTagBargroupLabel">&#28040;&#24687;</span>
    <button class="conversationFilterTagBartagItem {active}">
      <span class="conversationFilterTagBartagItemLabel">&#20840;&#37096;</span>
    </button>
  </div>
</div>
'''


class HangzhouSaasRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        playwright = await async_playwright().start()
        self.addAsyncCleanup(playwright.stop)
        browser = await playwright.chromium.launch()
        self.addAsyncCleanup(browser.close)
        self.context = await browser.new_context()
        self.page = await self.context.new_page()
        self.page.set_default_timeout(500)
        await self.context.route('**/*', self._offline_route)

    async def _offline_route(self, route):
        if route.request.url == desktop.REMOTE_LOGIN_URL and route.request.is_navigation_request():
            await route.fulfill(content_type='text/html; charset=utf-8', body='<main>Fixture</main>')
        else:
            await route.abort()

    async def _filters(self, *bars):
        await self.page.set_content(
            '<div class="im-saas-viewport-test" style="min-height:30px">'
            + ''.join(bars) + '</div>'
        )

    async def test_activation_skips_hidden_first_bar(self):
        stale = FILTER_BAR.format(attrs='hidden', active='')
        current = FILTER_BAR.format(attrs='', active='conversationFilterTagBartagItemActive')
        await self._filters(stale, current)
        self.assertTrue(await im_ui.activate_saas_filter(self.page, 'friend', timeout_ms=500))

    async def test_hidden_active_tag_does_not_confirm_current_filter(self):
        stale = FILTER_BAR.format(attrs='hidden', active='conversationFilterTagBartagItemActive')
        current = FILTER_BAR.format(attrs='', active='')
        await self._filters(stale, current)
        self.assertFalse(await im_ui.saas_filter_is_active(self.page, 'friend'))

    async def test_group_filter_failure_still_reaches_friend_scan(self):
        await self._filters(FILTER_BAR.format(attrs='', active='conversationFilterTagBartagItemActive'))
        observed = []
        activate = im_ui.activate_saas_filter

        async def bounded_activation(page, kind):
            observed.append(kind)
            if kind == 'friend':
                raise StopFriendScan
            return await activate(page, kind, timeout_ms=500)

        class StopFriendScan(Exception):
            pass

        errors = []
        group = '\u7fa4\u804a\uff1aGroup'
        with patch.object(tasks, '_dismiss_non_login_dialogs', AsyncMock()), \
                patch.object(im_ui, 'activate_saas_filter', bounded_activation):
            with self.assertRaises(StopFriendScan):
                async for _ in tasks.scroll_and_select_user(
                    self.page, {}, 'fixture', [group, 'Friend'], group_scan_errors=errors,
                ):
                    self.fail('No target should be selected in the fixture')
        self.assertEqual(observed, ['group', 'friend'])
        self.assertEqual([(e['target'], e['category']) for e in errors], [(group, 'friend_list_unavailable')])

    async def test_friend_filter_failure_has_list_unavailable_category(self):
        await self._filters()
        activate = im_ui.activate_saas_filter

        async def bounded_activation(page, kind):
            return await activate(page, kind, timeout_ms=200)

        with patch.object(tasks, '_dismiss_non_login_dialogs', AsyncMock()), \
                patch.object(im_ui, 'activate_saas_filter', bounded_activation):
            with self.assertRaises(tasks.FriendListUnavailableError) as caught:
                await tasks._open_friends_tab(self.page, 'fixture', 'unused', ())
        self.assertEqual(tasks.classify_browser_failure('friend_list', caught.exception), 'friend_list_unavailable')

    async def _desktop_manager(self, html):
        await self.page.goto(desktop.REMOTE_LOGIN_URL)
        await self.page.set_content(html)
        manager = desktop.LoginDesktopManager()
        manager.context = self.context
        manager.page = self.page
        return manager

    async def test_visible_qr_is_known_logged_out(self):
        manager = await self._desktop_manager('<button>&#25195;&#30721;&#30331;&#24405;</button>')
        status = await manager.status()
        self.assertEqual(status['login_state'], 'logged_out')
        self.assertTrue(status['login_state_known'])

    async def test_loading_result_is_not_cached(self):
        manager = await self._desktop_manager('<main>Loading</main>')
        self.assertEqual((await manager.status())['login_state'], 'unknown')
        await self.page.set_content('<button>&#25195;&#30721;&#30331;&#24405;</button>')
        self.assertEqual((await manager.status())['login_state'], 'logged_out')

    async def test_busy_page_ignores_previous_known_status(self):
        manager = await self._desktop_manager('<main>Loading</main>')
        manager._status_cache = {'running': True, 'login_state': 'logged_in', 'current_url': self.page.url}
        manager._status_checked_at = time.monotonic()
        async with manager._page_operation_lock:
            self.assertEqual((await manager.status())['login_state'], 'unknown')

    async def test_identity_status_does_not_read_cookies(self):
        manager = await self._desktop_manager(
            '<main id="card"><span id="name">Fixture</span>'
            '<span id="uid">&#25238;&#38899;&#21495;&#65306;10001</span></main>'
        )
        with patch.object(login, 'READY_SELECTOR', '#card'), \
                patch.dict(login.XPATHS, {'unique_id': '#uid', 'name': '#name'}), \
                patch.object(self.context, 'cookies', AsyncMock(side_effect=AssertionError('cookie read'))):
            status = await manager.status()
        self.assertEqual((status['login_state'], status['username'], status['unique_id']),
                         ('logged_in', 'Fixture', '10001'))
