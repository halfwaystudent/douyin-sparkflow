"""Real Playwright API contract on a synthetic local page; all network blocked."""
import asyncio
import unittest
from playwright.async_api import async_playwright
from core import tasks


class OfflineBrowserStageContractTests(unittest.TestCase):
    def test_login_detection_and_hung_page_query_are_bounded(self):
        async def exercise():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
                try:
                    context = await browser.new_context()
                    await context.route('**/*', lambda route: route.abort())
                    page = await context.new_page()
                    await page.set_content('<html><body><div>演示聊天列表</div></body></html>')
                    await tasks.ensure_not_login_required(page, 'synthetic', 'offline')
                    await page.set_content('<html><body><div class="login-mask">请登录</div></body></html>')
                    with self.assertRaisesRegex(RuntimeError, 'login_required'):
                        await tasks.ensure_not_login_required(page, 'synthetic', 'offline')
                    started = asyncio.get_running_loop().time()
                    with self.assertRaises(tasks.BrowserStageTimeout):
                        await tasks._run_browser_stage('offline_query', page.evaluate('() => new Promise(() => {})'), timeout_seconds=.05)
                    self.assertLess(asyncio.get_running_loop().time()-started, 2)
                finally:
                    await browser.close()
        asyncio.run(exercise())
