import asyncio
from pathlib import Path
import unittest
from unittest.mock import patch

from playwright.async_api import async_playwright

from tests import test_webui_pages as fixtures
from tests import test_shared_security as shared
from webui import app as app_module


class TemplateReadTests(unittest.TestCase):
    def test_template_does_not_repeat_live_session_lookup_for_each_header_field(self):
        fixture = shared.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        client = fixture.login()
        original = app_module.current_principal
        with patch.object(app_module, "current_principal", wraps=original) as calls:
            response = client.get("/accounts")
        self.assertEqual(200, response.status_code)
        # Includes route authorization and independently scoped data reads.
        # Template fields reuse one fresh result; write-time checks stay live.
        self.assertLessEqual(calls.call_count, 6)


class AsyncIconLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_icon_download_does_not_hold_page_interaction(self):
        fixture = fixtures.RedesignedPagesTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture._as(fixtures.ADMIN)
        runtime = await async_playwright().start()
        browser = await runtime.chromium.launch()
        page = await browser.new_page()
        released = asyncio.Event()
        requested = asyncio.Event()

        async def respond(route):
            url = route.request.url
            if not url.startswith("http://sparkflow.test/"):
                return await route.abort()
            if "/static/icons.js" in url:
                requested.set()
                await released.wait()
            response = fixture.client.get(url.replace("http://sparkflow.test", ""), follow_redirects=False)
            headers = {key: value for key, value in response.headers.items() if key not in {"content-length", "content-encoding"}}
            await route.fulfill(status=response.status_code, headers=headers, body=response.content)

        await page.route("**/*", respond)
        try:
            await asyncio.wait_for(page.goto("http://sparkflow.test/accounts", wait_until="domcontentloaded"), timeout=5)
            await asyncio.wait_for(requested.wait(), timeout=2)
            self.assertTrue(await page.evaluate("typeof window.lucide === 'undefined'"))
            old_theme = await page.locator("html").get_attribute("data-theme")
            await page.locator("[data-theme-toggle]").first.click()
            self.assertNotEqual(old_theme, await page.locator("html").get_attribute("data-theme"))
            released.set()
            await page.wait_for_function("document.querySelectorAll('svg.lucide').length > 0")
        finally:
            released.set()
            await browser.close()
            await runtime.stop()
