"""Patrol/refresh regressions using real Chromium and network-blocked DOM."""
import asyncio
import unittest
from unittest.mock import patch
from playwright.async_api import async_playwright
from core import friends


class PatrolVisibleRowsTests(unittest.IsolatedAsyncioTestCase):
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
        old = ''.join('<li role="listitem" class="semi-list-item"><span class="item-header-name-fixture">cached</span></li>' for _ in range(hidden))
        current = ''.join('<li role="listitem" class="semi-list-item"><span class="item-header-name-fixture">friend-%s</span></li>' % i for i in range(visible))
        await self.page.set_content('<div id="sub-app"><button role="tab">朋友私信</button><section hidden><ul>' + old + '</ul></section><section><ul>' + current + '</ul></section></div>')

    async def test_visible_friend_after_five_hidden_rows_is_found(self):
        await self.mount()
        selected, rows = await friends._first_visible_locator(self.page, friends.FRIEND_ROW_SELECTORS)
        self.assertIsNotNone(rows, 'patrol must see the visible friend after cached hidden rows')
        self.assertIn(selected, friends.FRIEND_ROW_SELECTORS)
        self.assertEqual(await rows.count(), 1, 'hidden cached friends must not enter the returned scan')

    async def test_large_hidden_prefix_keeps_python_probe_bounded(self):
        await self.mount(hidden=100, visible=2)
        from playwright.async_api import Locator
        original = Locator.is_visible
        probes = []
        async def counted(locator, *args, **kwargs):
            probes.append(1)
            return await original(locator, *args, **kwargs)
        with patch.object(Locator, 'is_visible', counted):
            _, rows = await friends._first_visible_locator(self.page, friends.FRIEND_ROW_SELECTORS)
        self.assertIsNotNone(rows)
        self.assertEqual(await rows.count(), 2)
        self.assertEqual(len(probes), 1)

    async def test_xpath_fallback_filters_hidden_cache(self):
        await self.mount(hidden=10)
        _, rows = await friends._first_visible_locator(self.page, ['xpath=//*[@id="sub-app"]//li'])
        self.assertIsNotNone(rows)
        self.assertEqual(await rows.count(), 1)

    async def test_all_hidden_rows_are_not_ready(self):
        await self.mount(hidden=10, visible=0)
        selected, rows = await friends._first_visible_locator(self.page, friends.FRIEND_ROW_SELECTORS)
        self.assertEqual(selected, '')
        self.assertIsNone(rows)

    async def test_hidden_selector_falls_through(self):
        await self.mount(hidden=10)
        selected, rows = await friends._first_visible_locator(self.page, ['section[hidden] li', 'section:not([hidden]) li'])
        self.assertEqual(selected, 'section:not([hidden]) li')
        self.assertEqual(await rows.count(), 1)

    async def test_returned_locator_tracks_visibility_changes(self):
        await self.mount(hidden=5, visible=2)
        _, rows = await friends._first_visible_locator(self.page, friends.FRIEND_ROW_SELECTORS)
        self.assertIsNotNone(rows)
        self.assertEqual(await rows.count(), 2)
        await self.page.locator('section:not([hidden]) li').first.evaluate("element => element.style.display='none'")
        self.assertEqual(await rows.count(), 1)
        self.assertTrue(await rows.first.is_visible())

    async def test_patrol_readiness_finds_rows_after_hidden_cache(self):
        await self.mount(hidden=10)
        _, rows = await asyncio.wait_for(friends._wait_for_friend_rows_or_empty(self.page, timeout_seconds=.5), timeout=4)
        self.assertEqual(await rows.count(), 1)

    async def test_readiness_keeps_timeout_for_hidden_only_rows(self):
        await self.mount(hidden=10, visible=0)
        with self.assertRaises(friends.FriendRefreshError) as caught:
            await asyncio.wait_for(friends._wait_for_friend_rows_or_empty(self.page, timeout_seconds=.01), timeout=4)
        self.assertEqual(caught.exception.category, friends.CATEGORY_STRUCTURE_CHANGED)

    async def test_collect_friend_names_excludes_cached_hidden_names(self):
        await self.mount(hidden=10, visible=2)
        await self.page.locator('#sub-app').evaluate("element => element.insertAdjacentHTML('beforeend', '<div class=\"no-more-tip-ftdJnu\">没有更多</div>')")
        result = await asyncio.wait_for(friends.collect_friend_names(self.page), timeout=5)
        self.assertEqual(list(result), ['friend-0', 'friend-1'])
        self.assertTrue(result.complete)

    async def test_genuinely_empty_list_remains_complete(self):
        await self.page.set_content('<div id="sub-app"><button role="tab">朋友私信</button><div>暂无好友</div></div>')
        result = await asyncio.wait_for(friends.collect_friend_names(self.page), timeout=5)
        self.assertEqual(list(result), [])
        self.assertTrue(result.complete)

