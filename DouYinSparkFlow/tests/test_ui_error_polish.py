"""Browser regressions for safe cached-friend-list failures.

Every request is implemented in-page: the test never contacts a console,
account, messaging endpoint, or production service.
"""

from pathlib import Path
import unittest

from playwright.async_api import async_playwright


SCRIPT = Path(__file__).resolve().parents[1] / "webui/static/account-pickers.js"


class CachedFriendListErrorBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    async def load(self, mode):
        page = await self.browser.new_page()
        await page.set_content('''<form id="account-form">
          <div class="friend-picker" data-account-id="1"
               data-cache-url="/accounts/1/picker-cache?ref=fixture&generation=fixture"
               data-refresh-url="/accounts/1/friends/refresh" data-csrf-token="fixture">
            <button type="button" class="friend-refresh-button">刷新好友</button>
            <p class="friend-picker-status">上次刷新：fixture</p>
            <input type="search" class="friend-search-input">
            <p class="friend-picker-summary"></p><div class="friend-picker-list"></div>
            <script type="application/json" id="friends-cache-1">[]</script>
            <script type="application/json" id="selected-targets-1">["已选目标"]</script>
          </div>
          <textarea name="targets" class="targets-textarea">已选目标</textarea>
        </form>''')
        await page.evaluate("""mode => {
          window.requests = [];
          const json = (body, status = 200) => new Response(JSON.stringify(body), {
            status, headers: {"content-type": "application/json"},
          });
          const forbiddenJson = (status) => {
            const response = new Response("<!doctype html><title>login</title>", {
              status, headers: {"content-type": "text/html"},
            });
            response.json = () => { throw new Error("JSON_NOT_ALLOWED"); };
            return response;
          };
          window.fetch = async (url, options = {}) => {
            window.requests.push({url: String(url), method: options.method || "GET"});
            if (!String(url).includes("/picker-cache")) return json({state: "idle"});
            if (mode === "session") return forbiddenJson(401);
            if (mode === "redirect") {
              const response = forbiddenJson(200);
              Object.defineProperty(response, "redirected", {value: true});
              return response;
            }
            if (mode === "access") return forbiddenJson(403);
            if (mode === "service") return forbiddenJson(500);
            if (mode === "invalid") return new Response("<!doctype html><title>login</title>", {
              status: 200, headers: {"content-type": "text/html"},
            });
            return json({friends: ["已选目标", "缓存好友"]});
          };
        }""", mode)
        await page.add_script_tag(path=str(SCRIPT))
        return page

    async def assert_selection_is_unchanged(self, page):
        self.assertEqual("已选目标", await page.locator(".targets-textarea").input_value())
        self.assertEqual(
            ["已选目标"],
            await page.evaluate("new FormData(document.getElementById('account-form')).get('targets').split('\\n')"),
        )
        self.assertFalse(await page.evaluate("requests.some(request => request.method === 'POST')"))

    async def test_status_and_html_failures_are_chinese_and_keep_the_current_selection(self):
        expected = {
            "session": "控制台登录状态已失效，请重新登录控制台后重试。当前选择已保留。",
            "redirect": "控制台登录状态已失效，请重新登录控制台后重试。当前选择已保留。",
            "access": "当前账号的访问权限已变化，请重新打开账号页核对。当前选择已保留。",
            "service": "服务暂时不可用，请稍后重试。当前选择已保留。",
            "invalid": "列表响应不是有效的 JSON，当前选择已保留。",
        }
        for mode, message in expected.items():
            with self.subTest(mode=mode):
                page = await self.load(mode)
                try:
                    notice = page.locator(".friend-picker [data-cache-state]")
                    await notice.wait_for()
                    text = await notice.inner_text()
                    self.assertIn(message, text)
                    self.assertNotIn("JSON_NOT_ALLOWED", text)
                    self.assertNotIn("Unexpected token", text)
                    await self.assert_selection_is_unchanged(page)
                finally:
                    await page.close()

    async def test_only_a_200_json_cache_response_updates_the_picker(self):
        page = await self.load("valid")
        try:
            await page.get_by_text("缓存好友", exact=True).wait_for()
            self.assertEqual(2, await page.locator(".friend-picker-list input[type=checkbox]").count())
            self.assertTrue(await page.locator(".friend-picker-list input[type=checkbox]").first.is_checked())
            await self.assert_selection_is_unchanged(page)
        finally:
            await page.close()
