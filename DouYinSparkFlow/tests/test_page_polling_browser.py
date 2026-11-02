from pathlib import Path
import unittest

from playwright.async_api import async_playwright


STATIC = Path(__file__).resolve().parents[1] / "webui/static"


class PagePollingBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    async def test_dashboard_slow_requests_do_not_overlap_and_retry_after_timeout(self):
        await self.page.clock.install()
        await self.page.set_content('<div data-overview-root></div>')
        await self.page.evaluate("""() => {
          window.pollers = [];
          window.setInterval = callback => { pollers.push(callback); return pollers.length; };
          window.calls = 0;
          window.fetch = (url, options) => new Promise((resolve, reject) => {
            calls++;
            options.signal.addEventListener('abort', () => reject(new DOMException('Timed out', 'AbortError')));
          });
        }""")
        await self.page.add_script_tag(path=str(STATIC / "app.js"))
        await self.page.evaluate("pollers.forEach(callback => { for (let i=0; i<5; i++) callback(); })")
        self.assertEqual(1, await self.page.evaluate("calls"))
        await self.page.clock.run_for(10001)
        await self.page.evaluate("pollers.forEach(callback => callback())")
        self.assertEqual(2, await self.page.evaluate("calls"))

    async def test_lease_change_clears_sensitive_input_and_aborts_old_frame(self):
        await self.page.set_content('''<div id="login-desktop-controls" data-workspace-active="1" data-workspace-ticket="ticket-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" data-csrf-token="fixture"></div>
          <details open data-login-interaction><p data-interaction-status></p><img hidden data-interaction-image>
          <input type="password" data-interaction-text><button data-interaction-send>输入</button><button data-interaction-expand>展开</button></details>''')
        await self.page.evaluate("""() => {
          window.aborted = 0;
          window.fetch = (url, options) => new Promise((resolve, reject) => {
            options.signal.addEventListener('abort', () => { aborted++; reject(new DOMException('Released', 'AbortError')); });
          });
        }""")
        await self.page.add_script_tag(path=str(STATIC / "login-interaction.js"))
        await self.page.locator("input").fill("temporary-secret")
        await self.page.evaluate("document.getElementById('login-desktop-controls').dataset.workspaceActive = '0'")
        await self.page.wait_for_function("aborted === 1 && document.querySelector('input').value === ''")
        self.assertTrue(await self.page.locator("img").is_hidden())
