"""Use the actual status widget script, including failure and refresh behavior."""
from pathlib import Path
import unittest
from playwright.async_api import async_playwright

SCRIPT = Path(__file__).resolve().parents[1] / 'webui/static/host-status.js'


class HostStatusWidgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()
        self.page = await self.browser.new_page(viewport={'width': 390, 'height': 844})
        await self.page.route('**/*', lambda route: route.abort())
        await self.page.set_content('''<section data-host-status><p data-host-status-message></p><ul data-host-status-list hidden></ul><button data-host-status-retry hidden>重新检查</button></section>''')
        await self.page.clock.install()
        await self.page.clock.pause_at(await self.page.evaluate('Date.now()+100'))
        await self.page.evaluate('''() => {
            window.calls=0;
            window.answers=[{ok:true,checkedAt:new Date().toISOString(),containers:[
                {Names:'douyin-web',State:'running',Status:'运行中'},
                {Names:'login-desktop',State:'running',Status:'运行中'},
                {Names:'douyin-scheduler',State:'not_deployed',Status:'未部署（未启用定时发送）'},
                {Names:'mihomo',State:'not_deployed',Status:'未部署'}]}];
            window.fetch=async (url)=>{if(url!=='/api/ops/host-status')throw new Error('unexpected URL');calls++;return new Response(JSON.stringify(answers[Math.min(calls-1,answers.length-1)]),{headers:{'content-type':'application/json'}});};
        }''')
        await self.page.add_script_tag(path=str(SCRIPT))
        await self.page.wait_for_function('document.querySelectorAll("[data-host-status-list] li").length===4')

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    async def test_undeployed_scheduler_is_neutral_not_a_failed_running_service(self):
        rows = self.page.locator('[data-host-status-list] li')
        self.assertIn('未启用定时发送', await rows.nth(2).inner_text())
        self.assertEqual(0, await rows.nth(2).locator('.bad,.ok').count())
        self.assertEqual(2, await self.page.locator('[data-host-status-list] .ok').count())

    async def test_expired_snapshot_hides_old_green_rows_and_displays_backend_reason(self):
        await self.page.evaluate("answers.push({ok:false,containers:[],error:'服务状态快照已过期，请稍后重新检查。'})")
        await self.page.locator('[data-host-status-retry]').click()
        await self.page.wait_for_function('calls===2 && !document.querySelector("[data-host-status-retry]").disabled')
        self.assertTrue(await self.page.locator('[data-host-status-list]').is_hidden())
        self.assertEqual(0, await self.page.locator('[data-host-status-list] li').count())
        self.assertIn('快照已过期', await self.page.locator('[data-host-status-message]').inner_text())

    async def test_visible_page_refreshes_and_pagehide_stops_timer(self):
        await self.page.clock.run_for(15010)
        await self.page.wait_for_function('calls===2', timeout=1000)
        await self.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        await self.page.clock.run_for(15010)
        self.assertEqual(2, await self.page.evaluate('calls'))
        await self.page.evaluate("window.dispatchEvent(new Event('pageshow'))")
        await self.page.wait_for_function('calls===3', timeout=1000)
