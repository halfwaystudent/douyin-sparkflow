"""Standalone QR uses the already-authorized local image, never a naked URL."""
import re
import unittest
from pathlib import Path

from tests import test_workspace_lifecycle_browser as fixtures


class StandaloneQrBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.WorkspaceLifecycleBrowserTests()
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.page = self.fixture.page
        template = (fixtures.STATIC.parent / 'templates/login_workspace.html').read_text()
        match = re.search(r'<a\b[^>]*>\s*<i[^>]*></i><span>单独打开</span>\s*</a>', template)
        assert match is not None
        link = match.group(0)
        await self.page.locator('#login-desktop-controls').evaluate(
            '(node, html) => node.insertAdjacentHTML("beforeend", html)', link)
        self.link = self.page.locator('a', has_text='单独打开')
        await self.fixture.app()

    async def load_qr(self):
        await self.fixture.open()
        await self.page.clock.run_for(101)
        await self.page.wait_for_function("!document.querySelector('[data-login-qr]').hidden")

    async def test_link_is_unavailable_until_authorized_image_arrives(self):
        self.assertTrue(await self.link.is_hidden())
        self.assertIsNone(await self.link.get_attribute('href'))
        await self.load_qr()
        href = await self.link.get_attribute('href')
        self.assertTrue(href.startswith('blob:'), href)
        self.assertEqual(await self.page.locator('[data-login-qr]').get_attribute('src'), href)
        self.assertNotIn(fixtures.TICKET, href)
        self.assertTrue(await self.link.is_visible())
        qr_calls = await self.fixture.calls('/login-desktop/qr?')
        self.assertEqual(1, len(qr_calls))
        self.assertEqual(fixtures.TICKET, qr_calls[0]['headerTicket'])

    async def test_close_invalidates_link_without_weakening_server_gate(self):
        await self.load_qr()
        await self.page.locator('.login-desktop-close').click()
        self.assertTrue(await self.link.is_hidden())
        self.assertIsNone(await self.link.get_attribute('href'))

    async def test_pagehide_invalidates_link_and_late_old_reply_cannot_restore_it(self):
        await self.page.evaluate("pending['/login-desktop/qr?']=[];ignoreAbort=true")
        await self.fixture.open()
        await self.page.clock.run_for(101)
        await self.page.wait_for_function("pending['/login-desktop/qr?'].length===1")
        await self.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));pending['/login-desktop/qr?'].shift().resolve(new Response(screenBlob()))")
        await self.page.wait_for_timeout(30)
        self.assertTrue(await self.link.is_hidden())
        self.assertIsNone(await self.link.get_attribute('href'))

    async def test_popup_displays_snapshot_without_another_qr_request(self):
        await self.load_qr()
        before = len(await self.fixture.calls('/login-desktop/qr?'))
        async with self.page.expect_popup() as popup_event:
            await self.link.click()
        popup = await popup_event.value
        try:
            await popup.wait_for_load_state()
            image = popup.locator('img')
            await image.wait_for()
            self.assertTrue(await image.evaluate('(img)=>img.complete && img.naturalWidth>0'))
            self.assertTrue(popup.url.startswith('blob:'))
            self.assertEqual(before, len(await self.fixture.calls('/login-desktop/qr?')))
        finally:
            await popup.close()
