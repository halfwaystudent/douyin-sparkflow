import unittest
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "webui" / "static"
TEMPLATE = ROOT / "webui" / "templates" / "send_console.html"


class UiSummaryPolishBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    async def test_overview_poll_updates_send_console_summary_evidence_and_state(self):
        page = await self.browser.new_page(viewport={"width": 1280, "height": 800})
        self.addAsyncCleanup(page.close)
        await page.set_content(
            """
            <span data-overview-live-state>实时</span>
            <div data-overview-root>
              <div class="status-banner" data-task-banner><strong><i data-lucide="circle-check"></i><span data-task-text></span></strong></div>
              <div class="callout good" data-today-summary data-summary-state="good">
                <i data-lucide="circle-check" data-today-summary-icon></i>
                <p data-today-summary-text>旧结论</p>
              </div>
              <b data-overview-value="sentTotal">0</b>
              <b data-overview-value="confirmed">0</b>
              <b data-overview-value="pageEcho">0</b>
              <b data-overview-value="receiptOnly">0</b>
              <b data-overview-value="attention">0</b>
              <b data-overview-value="pending">0</b>
            </div>
            """
        )
        await page.evaluate(
            """() => {
              window.fetch = async () => ({
                ok: true,
                json: async () => ({
                  task: {running: false, stale: false},
                  summary: {
                    total: 20, enabledAccounts: 2, confirmed: 1, pageEcho: 3,
                    receiptOnly: 4, unconfirmed: 2, failed: 5, blocked: 6,
                    attention: 13, pending: 7, unprocessed: 8,
                  },
                  accounts: [],
                }),
              });
            }"""
        )
        await page.add_script_tag(path=str(STATIC / "app-shell.js"))
        await page.wait_for_function(
            "document.querySelector('[data-overview-value=sentTotal]').textContent === '8'"
        )

        self.assertEqual(
            "今日结论：强确认 1 · 弱证据（仅页面回显 3 · 仅回执未确认 4） · 待核验 2 · 失败 5 · 受账号阻断 6 · 待发送 15（共 20 个目标，2 个启用账号）",
            await page.locator("[data-today-summary-text]").text_content(),
        )
        summary = page.locator("[data-today-summary]")
        self.assertEqual("attention", await summary.get_attribute("data-summary-state"))
        self.assertFalse(await summary.evaluate("node => node.classList.contains('good')"))
        self.assertEqual(
            "triangle-alert",
            await page.locator("[data-today-summary-icon]").get_attribute("data-lucide"),
        )

    async def test_group_label_is_visually_hidden_but_keeps_the_textarea_label(self):
        for width in (320, 390, 1280):
            with self.subTest(width=width):
                page = await self.browser.new_page(viewport={"width": width, "height": 800})
                self.addAsyncCleanup(page.close)
                await page.set_content(
                    '<label class="field"><span class="sr-only">群聊名称</span><textarea name="group_targets"></textarea></label>'
                )
                await page.add_style_tag(path=str(STATIC / "app.css"))

                style = await page.locator(".sr-only").evaluate(
                    "node => { const style = getComputedStyle(node); return {position: style.position, display: style.display, visibility: style.visibility, width: style.width, height: style.height, overflow: style.overflow, clip: style.clip}; }"
                )
                self.assertEqual("absolute", style["position"])
                self.assertNotEqual("none", style["display"])
                self.assertNotEqual("hidden", style["visibility"])
                self.assertEqual("1px", style["width"])
                self.assertEqual("1px", style["height"])
                self.assertEqual("hidden", style["overflow"])
                self.assertNotEqual("auto", style["clip"])
                self.assertEqual(
                    "群聊名称",
                    await page.locator("textarea").evaluate(
                        "element => element.labels[0].textContent.trim()"
                    ),
                )
                self.assertLessEqual(
                    await page.evaluate("document.documentElement.scrollWidth"), width
                )

    async def test_dashboard_never_calls_weak_or_strong_evidence_a_streak(self):
        page = await self.browser.new_page(); self.addAsyncCleanup(page.close)
        await page.set_content('<div data-overview-root><span data-overview-headline></span></div>')
        await page.evaluate('''() => {window.fetch=async()=>({ok:true,json:async()=>({task:{},summary:{total:1,confirmed:0,pageEcho:1,receiptOnly:0,pending:0,unprocessed:0,attention:0},accounts:[]})})}''')
        await page.add_script_tag(path=str(STATIC/'app-shell.js'))
        await page.wait_for_function("document.querySelector('[data-overview-headline]').textContent.length>0")
        text=await page.locator('[data-overview-headline]').text_content()
        self.assertNotIn('都续上了',text)
        self.assertIn('待核验',text)

    async def test_weak_evidence_summary_is_not_a_green_success(self):
        for page_echo,receipt in [(1,0),(0,1),(1,1)]:
            with self.subTest(page_echo=page_echo,receipt=receipt):
                page=await self.browser.new_page();self.addAsyncCleanup(page.close)
                await page.set_content('''<div data-overview-root><div class="callout good" data-today-summary data-summary-state="good"><i data-today-summary-icon data-lucide="circle-check"></i><p data-today-summary-text></p></div></div>''')
                await page.evaluate('''([p,r])=>{window.fetch=async()=>({ok:true,json:async()=>({task:{},summary:{total:p+r,confirmed:0,pageEcho:p,receiptOnly:r,attention:0},accounts:[]})})}''',[page_echo,receipt])
                await page.add_script_tag(path=str(STATIC/'app-shell.js'))
                await page.wait_for_function("document.querySelector('[data-today-summary-text]').textContent.length>0")
                summary=page.locator('[data-today-summary]')
                self.assertNotEqual('good',await summary.get_attribute('data-summary-state'))
                self.assertFalse(await summary.evaluate("e=>e.classList.contains('good')"))
                self.assertEqual('triangle-alert',await page.locator('[data-today-summary-icon]').get_attribute('data-lucide'))


class SendConsoleTemplateSemanticsTests(unittest.TestCase):
    def test_first_render_weak_evidence_is_not_a_green_success(self):
        text=TEMPLATE.read_text(encoding='utf-8')
        self.assertIn('summary.today_page_echo_targets', text[text.index('data-today-summary')-400:text.index('data-today-summary')])
        self.assertIn('summary.today_receipt_only_targets', text[text.index('data-today-summary')-400:text.index('data-today-summary')])

    def test_attention_count_does_not_promise_retry_of_every_unknown(self):
        templates = ROOT / 'webui/templates'
        for name in ('dashboard.html', 'settings.html', 'send_console.html'):
            with self.subTest(name=name):
                text=(templates/name).read_text(encoding='utf-8')
                self.assertNotIn('将补发 {{',text)
                self.assertIn('结果不明的目标不会自动重发',text)

    def test_sent_total_counts_both_weak_evidence_kinds(self):
        template = TEMPLATE.read_text(encoding="utf-8")

        self.assertIn(
            'data-overview-value="sentTotal">{{ summary.today_confirmed_targets + summary.today_page_echo_targets + summary.today_receipt_only_targets }}',
            template,
        )
        self.assertIn("已发送（今日，含弱证据）", template)
        self.assertIn("弱证据：仅页面回显", template)
        self.assertIn("仅回执未确认", template)


if __name__ == "__main__":
    unittest.main()
