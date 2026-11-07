"""Real Chromium, synthetic lists only: hidden prefixes must not mask rows."""
import asyncio
import unittest
from playwright.async_api import async_playwright
from unittest.mock import patch
from core import tasks


class VisibleFriendRowsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.addAsyncCleanup(self.pw.stop)
        self.browser = await self.pw.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage'])
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        self.addAsyncCleanup(self.context.close)
        await self.context.route('**/*', lambda route: route.abort())
        self.page = await self.context.new_page()

    async def mount(self, hidden=5, visible=1):
        old = ''.join('<li class="semi-list-item"><span class="item-header-name-fixture">hidden</span></li>' for _ in range(hidden))
        current = ''.join('<li class="semi-list-item"><span class="item-header-name-fixture">visible</span></li>' for _ in range(visible))
        await self.page.set_content('<div id="sub-app"><section hidden><ul>' + old + '</ul></section><section><ul>' + current + '</ul></section></div>')

    async def test_five_hidden_prefix_rows_do_not_hide_the_sixth_visible_row(self):
        await self.mount(hidden=5)
        selector, rows = await tasks._first_non_empty_locator(self.page, ['#sub-app li.semi-list-item'])
        self.assertIsNotNone(rows, 'a visible sixth row must be found')
        self.assertEqual(await rows.count(), 1, 'returned locator must exclude cached hidden rows')
        self.assertTrue(await rows.first.is_visible())

    async def test_large_hidden_prefix_does_not_expand_python_row_probe(self):
        await self.mount(hidden=100, visible=2)
        from playwright.async_api import Locator
        original = Locator.is_visible
        probes = []
        async def counted(locator, *args, **kwargs):
            probes.append(1)
            return await original(locator, *args, **kwargs)
        with patch.object(Locator, 'is_visible', counted):
            _, rows = await tasks._first_non_empty_locator(self.page, ['#sub-app li'])
        self.assertIsNotNone(rows)
        self.assertEqual(await rows.count(), 2)
        self.assertEqual(len(probes), 1)

    async def test_xpath_fallback_also_filters_hidden_rows(self):
        await self.mount(hidden=10)
        _, rows = await tasks._first_non_empty_locator(self.page, ['xpath=//*[@id="sub-app"]//li'])
        self.assertIsNotNone(rows)
        self.assertEqual(await rows.count(), 1)
        self.assertTrue(await rows.first.is_visible())

    async def test_all_hidden_rows_do_not_report_ready(self):
        await self.mount(hidden=10, visible=0)
        selected, rows = await tasks._first_non_empty_locator(self.page, ['#sub-app li'])
        self.assertEqual(selected, '')
        self.assertIsNone(rows)

    async def test_first_hidden_selector_falls_through_to_visible_selector(self):
        await self.mount(hidden=10)
        selector, rows = await tasks._first_non_empty_locator(
            self.page, ['section[hidden] li', '#sub-app section:not([hidden]) li'])
        self.assertEqual(selector, '#sub-app section:not([hidden]) li')
        self.assertEqual(await rows.count(), 1)

    async def test_returned_locator_tracks_visibility_changes(self):
        await self.mount(hidden=5, visible=2)
        _, rows = await tasks._first_non_empty_locator(self.page, ['#sub-app li'])
        self.assertEqual(await rows.count(), 2)
        await self.page.locator('section:not([hidden]) li').first.evaluate("element => element.style.display='none'")
        self.assertEqual(await rows.count(), 1)
        self.assertTrue(await rows.first.is_visible())

    async def test_friend_list_readiness_uses_visible_rows_after_hidden_cache(self):
        await self.mount(hidden=10)
        _, rows = await tasks._wait_for_friend_list_ready(
            self.page, 'synthetic', ['#sub-app li'], [], [],
            timeout_seconds=.3, empty_grace_seconds=.1)
        self.assertEqual(await rows.count(), 1)

    async def test_readiness_still_times_out_when_every_row_is_hidden(self):
        await self.mount(hidden=10, visible=0)
        with self.assertRaises(tasks.FriendListUnavailableError):
            await asyncio.wait_for(tasks._wait_for_friend_list_ready(
                self.page, 'synthetic', ['#sub-app li'], [], [],
                timeout_seconds=.01, empty_grace_seconds=.01), timeout=4)
