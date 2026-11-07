"""Offline Chromium regressions for initial creator-chat blank SPA recovery.

Every navigation is fulfilled locally; no cookies, accounts, or external requests
are used. These fixtures exercise DOM readiness only, not the live site.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import async_playwright

from core import tasks
from core import page_readiness


CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"
OTHER_URL = "https://creator.douyin.com/creator-micro/data/following"


def document(body, script=""):
    return f"<!doctype html><html><body>{body}<script>{script}</script></body></html>"


class ChatPageReadinessBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch(args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking",
            "--disable-component-update", "--host-resolver-rules=MAP * ~NOTFOUND",
        ])
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        self.requests = []
        self.html = document("")

        async def local_chat(route):
            self.requests.append(route.request.url)
            await route.fulfill(status=200, content_type="text/html", body=self.html)

        await self.context.route("**/*", local_chat)

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.runtime.stop()
        self.assertTrue(self.requests)
        self.assertTrue(all(url.split("?", 1)[0] in {CHAT_URL, OTHER_URL} for url in self.requests))

    async def open(self, url=CHAT_URL):
        await self.page.goto(url, wait_until="commit")
        await self.page.wait_for_load_state("domcontentloaded")

    async def readiness(self, *, wait=.20, stage=.35):
        with patch.object(page_readiness, "PAGE_READINESS_RENDER_WAIT_SECONDS", wait), \
             patch.object(page_readiness, "PAGE_READINESS_STAGE_TIMEOUT_SECONDS", stage):
            await page_readiness.ensure_chat_page_ready(self.page, "synthetic")

    async def test_loaded_chat_page_is_left_untouched(self):
        self.html = document("<main>synthetic chat is loaded</main>")
        await self.open()
        before = len(self.requests)

        await self.readiness()

        self.assertEqual(before, len(self.requests))

    async def test_blank_spa_waits_for_normal_delayed_render_without_reload(self):
        self.html = document("<div id='root'></div>", """
            setTimeout(() => document.querySelector('#root').textContent = 'synthetic chat', 50);
        """)
        await self.open()
        before = len(self.requests)

        await self.readiness(wait=.30, stage=.40)

        self.assertEqual(before, len(self.requests))
        self.assertEqual("synthetic chat", await self.page.locator("#root").inner_text())

    async def test_blank_spa_reloads_once_in_same_context_then_recovers(self):
        self.html = document("<div id='root'></div>", """
            const count = Number(sessionStorage.getItem('readiness-visits') || '0') + 1;
            sessionStorage.setItem('readiness-visits', String(count));
            if (count > 1) document.querySelector('#root').textContent = 'synthetic recovered';
        """)
        await self.open()
        before = len(self.requests)

        await self.readiness()

        self.assertEqual(before + 1, len(self.requests))
        self.assertEqual(CHAT_URL, self.page.url)
        self.assertEqual("synthetic recovered", await self.page.locator("#root").inner_text())

    async def test_permanently_blank_spa_reloads_once_then_fails_bounded(self):
        self.html = document("<div id='root'></div>")
        await self.open()
        before = len(self.requests)

        with self.assertRaises(tasks.BrowserStageTimeout) as caught:
            # Navigation is real even though requests are fulfilled locally.
            # A 160ms outer budget timed out the reload itself in the capped
            # production image; keep the 80ms blank-render wait but allow
            # navigation to complete so this test reaches the final blank check.
            await self.readiness(wait=.08, stage=2.0)

        self.assertEqual("page_readiness", caught.exception.stage)
        self.assertEqual(before + 1, len(self.requests))

    async def test_empty_friend_list_structure_is_left_to_existing_list_protection(self):
        self.html = document("<section class='friend-list'><ul></ul></section>")
        await self.open()
        before = len(self.requests)

        await self.readiness(wait=.08, stage=.16)

        self.assertEqual(before, len(self.requests))

    async def test_login_or_verification_controls_are_never_reloaded(self):
        for body in (
            "<input aria-label='login'><button>登录</button>",
            "<iframe title='captcha'></iframe><button>verify</button>",
        ):
            with self.subTest(body=body):
                self.html = document(body)
                await self.open()
                before = len(self.requests)

                await self.readiness()

                self.assertEqual(before, len(self.requests))

    async def test_non_chat_url_is_not_probed_or_reloaded(self):
        self.html = document("<div id='root'></div>")
        await self.open(OTHER_URL)
        before = len(self.requests)

        await self.readiness()

        self.assertEqual(before, len(self.requests))


class ChatPageReadinessTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_dom_read_propagates_stage_timeout_without_refresh(self):
        async def never_returns(*_args, **_kwargs):
            await asyncio.Future()

        page = Mock(url=CHAT_URL, evaluate=AsyncMock(side_effect=never_returns), goto=AsyncMock())
        with patch.object(page_readiness, "PAGE_READINESS_STAGE_TIMEOUT_SECONDS", .02):
            with self.assertRaises(tasks.BrowserStageTimeout) as caught:
                await page_readiness.ensure_chat_page_ready(page, "synthetic")

        self.assertEqual("page_readiness_snapshot", caught.exception.stage)
        page.goto.assert_not_awaited()

    async def test_cancellation_propagates_without_refresh(self):
        entered = asyncio.Event()

        async def never_returns(*_args, **_kwargs):
            entered.set()
            await asyncio.Future()

        page = Mock(url=CHAT_URL, evaluate=AsyncMock(side_effect=never_returns), goto=AsyncMock())
        task = asyncio.create_task(page_readiness.ensure_chat_page_ready(page, "synthetic"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        page.goto.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
