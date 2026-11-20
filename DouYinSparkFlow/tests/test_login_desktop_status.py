"""Status polling from a synthetic creator page; no real login or profile."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import login_desktop_server as desktop
from core import login


class DesktopStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.addAsyncCleanup(self.pw.stop)
        self.browser = await self.pw.chromium.launch(args=['--disable-dev-shm-usage'])
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        self.page_body = '<button>扫码登录</button><img alt="二维码">'
        await self.context.route('**/*', self._serve)
        await self.page.goto(desktop.REMOTE_LOGIN_URL)
        self.manager = desktop.LoginDesktopManager()
        self.manager.context = self.context
        self.manager.page = self.page

    async def _serve(self, route):
        if route.request.url == desktop.REMOTE_LOGIN_URL and route.request.is_navigation_request():
            await route.fulfill(content_type='text/html; charset=utf-8', body=self.page_body)
        else:
            await route.abort()

    async def test_visible_qr_is_logged_out_not_an_error(self):
        with patch.object(desktop.logger, 'warning') as warning:
            status = await self.manager.status()
        self.assertTrue(status['running'])
        self.assertFalse(status['logged_in'])
        self.assertEqual(status['login_state'], 'logged_out')
        self.assertTrue(status['login_state_known'])
        warning.assert_not_called()

    async def test_loading_page_remains_unknown_without_spurious_warning(self):
        await self.page.set_content('<main id="root"></main>')
        with patch.object(desktop.logger, 'warning') as warning:
            status = await self.manager.status()
        self.assertEqual(status['login_state'], 'unknown')
        self.assertFalse(status['login_state_known'])
        warning.assert_not_called()

    async def test_loading_result_is_not_cached_after_the_qr_appears(self):
        await self.page.set_content('<main id="root"></main>')
        self.assertEqual((await self.manager.status())['login_state'], 'unknown')
        await self.page.set_content('<button>扫码登录</button>')
        self.assertEqual((await self.manager.status())['login_state'], 'logged_out')

    async def test_busy_page_returns_unknown_instead_of_cached_logout(self):
        self.assertEqual((await self.manager.status())['login_state'], 'logged_out')
        async with self.manager._page_operation_lock:
            self.assertEqual((await self.manager.status())['login_state'], 'unknown')

    async def test_identity_status_does_not_read_cookies(self):
        await self.page.set_content('<main id="card"><span id="name">测试号</span><span id="uid">抖音号：10001</span></main>')
        with patch.object(login, 'READY_SELECTOR', '#card'), \
             patch.dict(login.XPATHS, {'unique_id': '#uid', 'name': '#name'}), \
             patch.object(self.context, 'cookies', AsyncMock(side_effect=AssertionError('cookie read'))):
            status = await self.manager.status()
        self.assertTrue(status['logged_in'])
        self.assertEqual((status['username'], status['unique_id']), ('测试号', '10001'))


class DesktopStatusDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_poll_error_records_safe_type_and_frame(self):
        from types import SimpleNamespace
        manager = desktop.LoginDesktopManager()
        manager.page = SimpleNamespace(url=desktop.REMOTE_LOGIN_URL, is_closed=lambda: False)
        manager.context = SimpleNamespace(_impl_obj=SimpleNamespace())

        async def simulated_browser_failure(*args, **kwargs):
            raise RuntimeError('private-marker-that-must-not-be-logged')

        with patch.object(login, '_read_page_identity', simulated_browser_failure), \
             self.assertLogs(desktop.logger, level='WARNING') as caught:
            status = await manager.status()
        self.assertEqual(status['login_state'], 'unknown')
        messages = '\n'.join(caught.output)
        self.assertIn('RuntimeError', messages)
        self.assertIn('simulated_browser_failure', messages)
        self.assertNotIn('private-marker', messages)

    async def test_deadline_cancels_slow_passive_probe_and_releases_operation_lock(self):
        from types import SimpleNamespace
        manager = desktop.LoginDesktopManager()
        manager.page = SimpleNamespace(url=desktop.REMOTE_LOGIN_URL, is_closed=lambda: False)
        manager.context = SimpleNamespace(_impl_obj=SimpleNamespace())
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def suspended(*args, **kwargs):
            try:
                entered.set()
                await asyncio.Future()
            finally:
                stopped.set()

        with patch.object(login, '_read_page_identity', suspended), \
             patch.object(desktop, 'STATUS_PROBE_SECONDS', 0.05, create=True):
            result = await asyncio.wait_for(manager.status(), 0.5)
        self.assertTrue(entered.is_set() and stopped.is_set())
        self.assertEqual(result['login_state'], 'unknown')
        self.assertFalse(manager._page_operation_lock.locked())
