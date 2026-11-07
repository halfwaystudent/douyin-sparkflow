"""Browser-level regressions for login-profile workspace state isolation."""

from pathlib import Path
import unittest

from playwright.async_api import async_playwright


SCRIPT = Path(__file__).resolve().parents[1] / "webui/static/app.js"
TICKET_A = "ticket-" + "a" * 32
TICKET_B = "ticket-" + "b" * 32

SHELL = '''<details id="interactive-login-section" open><div id="login-desktop-controls" data-csrf-token="fixture">
<button class="login-desktop-open" data-relogin-unique-id="111" data-relogin-account-ref="a1" data-login-generation="g1">重新登录</button>
<button class="login-desktop-save">保存</button><button class="login-desktop-close">关闭</button>
<p id="login-desktop-runtime-state"></p><p id="login-desktop-status-text"></p><p data-login-progress></p>
<ol><li data-login-step="1"></li><li data-login-step="2"></li><li data-login-step="3"></li></ol>
<div class="qr-placeholder"><span>等待申请</span></div><img hidden data-login-qr><p data-login-qr-status></p>
<button data-refresh-login-qr><span>刷新二维码</span></button><button hidden data-recover-login-workspace>重新检查</button>
</div></details><section hidden data-binding-result><h2 data-binding-result-title></h2><p data-binding-result-message></p><p data-save-check-status hidden></p><button data-recheck-login-save hidden></button><a data-binding-next></a><a data-binding-preferences></a></section>'''

MOCK = r'''() => {
  window.workspace = {state: 'closed', active: false, ticket: ''};
  window.qrCalls = 0;
  const response = body => Response.json(body);
  const png = () => new Blob([Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADElEQVR42mP4z8AAAAMBAQD3iZjuAAAAAElFTkSuQmCC'), c => c.charCodeAt(0))], {type: 'image/png'});
  window.fetch = async (rawUrl, options = {}) => {
    const url = String(rawUrl);
    if (url.startsWith('/login-desktop/qr?')) { qrCalls += 1; return new Response(png()); }
    if (url === '/login-desktop/qr/refresh') return response({ok: true});
    if (url === '/login-desktop/open') {
      workspace = {state: 'active', active: true, ticket: 'ticket-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', remaining_seconds: 180};
      return response({ok: true, state: 'active', workspace, desktop: {profile_mode: 'account', reusable_session: true, state: 'logged_in', logged_in: true}});
    }
    if (url === '/login-desktop/status') return response({ok: true, workspace, logged_in: false, login_state: 'unknown'});
    if (url === '/login-desktop/workspace-status') return response({ok: true, workspace});
    if (url === '/login-desktop/heartbeat') return response({ok: true, workspace});
    return response({ok: true, workspace});
  };
}'''


class LoginProfileWorkspaceBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()
        self.page = await self.browser.new_page()
        await self.page.clock.install()
        await self.page.set_content(SHELL)
        await self.page.evaluate(MOCK)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()
        self.assertEqual([], self.errors)

    async def test_ticket_promotion_and_explicit_rescan_clear_stale_logged_in_state(self):
        await self.page.add_script_tag(path=str(SCRIPT))
        await self.page.locator(".login-desktop-open").click()
        await self.page.wait_for_function("document.getElementById('login-desktop-controls').dataset.workspaceTicket === 'ticket-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'")
        await self.page.wait_for_function("document.querySelector('[data-refresh-login-qr] span').textContent === '重新扫码'")
        await self.page.clock.run_for(150)
        self.assertEqual(0, await self.page.evaluate("qrCalls"))
        self.assertFalse(await self.page.locator('[data-refresh-login-qr]').is_disabled())

        await self.page.locator("[data-refresh-login-qr]").click()
        await self.page.wait_for_function("qrCalls === 1")
        self.assertNotIn("当前浏览器已登录", await self.page.locator("#login-desktop-status-text").inner_text())

        await self.page.evaluate("workspace={state:'active',active:true,ticket:'ticket-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',remaining_seconds:180}")
        await self.page.clock.run_for(20001)
        await self.page.wait_for_function("qrCalls >= 2")
        self.assertEqual(TICKET_B, await self.page.locator("#login-desktop-controls").get_attribute("data-workspace-ticket"))
        self.assertNotIn("沿用账号浏览器环境", await self.page.locator("#login-desktop-status-text").inner_text())


if __name__ == "__main__":
    unittest.main()
