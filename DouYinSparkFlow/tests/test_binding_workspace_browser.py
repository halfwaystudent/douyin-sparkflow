from pathlib import Path
import unittest

from playwright.async_api import async_playwright


SCRIPT = Path(__file__).resolve().parents[1] / "webui/static/app.js"


class BindingWorkspaceBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()
        self.page = await self.browser.new_page()
        await self.page.clock.install()
        await self.page.set_content('''<section id="interactive-login-section">
          <div id="login-desktop-controls" data-csrf-token="fixture" data-display-mode="novnc"></div>
          <p id="login-desktop-status-text"></p><span id="login-desktop-runtime-state"></span>
          <button class="login-desktop-open" data-relogin-unique-id="111" data-relogin-account-ref="a3" data-login-generation="gen-a3">重新登录</button>
          <button class="login-desktop-save" data-relogin-unique-id="111" data-relogin-account-ref="a3" data-login-generation="gen-a3">保存</button>
          <button class="login-desktop-close">关闭</button>
          <button data-recover-login-workspace>重新检查</button>
          </section><section data-binding-result hidden><h2 data-binding-result-title></h2>
          <p data-binding-result-message></p><a data-binding-next href="/accounts">账号</a></section>''')
        await self.page.evaluate("""() => {
          window.intervals = [];
          window.setInterval = callback => { intervals.push(callback); return intervals.length; };
          window.requests = [];
          window.active = {state:'active',active:true,ticket:'ticket-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',remaining_seconds:180};
          window.closed = {state:'closed',active:false,ticket:''};
          window.pendingHeartbeat = null;
          window.deferHeartbeat = false;
          window.timeoutSave = false;
          window.fetch = async (url, options={}) => {
            const fields = options.body ? Object.fromEntries(options.body.entries()) : {};
            requests.push({url,fields});
            const respond = body => new Response(JSON.stringify(body), {status:200,headers:{'content-type':'application/json'}});
            if (url.endsWith('/status') && window.leaseLostStatus) return new Response(JSON.stringify({ok:false,error:'lease lost'}), {status:423,headers:{'content-type':'application/json'}});
            if (url.endsWith('/heartbeat') && deferHeartbeat) return new Promise(resolve => { pendingHeartbeat = () => resolve(respond({ok:true,workspace:active})); });
            if (url.endsWith('/save') && timeoutSave) return new Promise((resolve,reject) => options.signal.addEventListener('abort',()=>reject(new DOMException('Timeout','AbortError'))));
            if (url.endsWith('/save') && window.rolledBackSave) return new Response(JSON.stringify({ok:false,binding_pending:false,error:'本次登录改动已撤回'}), {status:503,headers:{'content-type':'application/json'}});
            if (url.endsWith('/close')) return respond({ok:true,workspace:closed});
            if (url.endsWith('/save')) return respond({ok:true,workspace:closed,verification:'pending',account:{account_ref:'a1',unique_id:'111',username:'本人'}});
            if (url.endsWith('/open')) return respond({ok:true,state:'active',workspace:active});
            if (url.endsWith('/workspace-status') && window.recoveryMode) return respond({ok:true,workspace:closed});
            if (url.includes('/accounts/binding-status')) return respond({ok:true,accounts:[]});
            return respond({ok:true,workspace:active});
          };
        }""")
        await self.page.add_script_tag(path=str(SCRIPT))
        await self.page.wait_for_function("document.getElementById('login-desktop-controls').dataset.workspaceTicket")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    async def test_late_heartbeat_cannot_restore_a_closed_workspace(self):
        await self.page.evaluate("deferHeartbeat=true; intervals.forEach(callback=>callback())")
        await self.page.wait_for_function("pendingHeartbeat !== null")
        await self.page.evaluate("intervals.forEach(callback=>callback())")
        self.assertEqual(1, await self.page.evaluate("requests.filter(item=>item.url.endsWith('/heartbeat')).length"))
        await self.page.locator(".login-desktop-close").click()
        await self.page.evaluate("pendingHeartbeat()")
        await self.page.wait_for_function("document.getElementById('login-desktop-controls').dataset.workspaceActive==='0'")
        self.assertEqual("", await self.page.locator("#login-desktop-controls").get_attribute("data-workspace-ticket"))

    async def test_recovery_preserves_explicit_relogin_target(self):
        await self.page.evaluate("recoveryMode=true; const button=document.querySelector('[data-recover-login-workspace]');button.hidden=false;button.click()")
        await self.page.clock.run_for(5001)
        await self.page.wait_for_function("requests.some(item=>item.url.endsWith('/open'))")
        fields = await self.page.evaluate("requests.find(item=>item.url.endsWith('/open')).fields")
        self.assertEqual("relogin", fields["mode"])
        self.assertEqual("111", fields["relogin_unique_id"])
        self.assertEqual("a3", fields["relogin_account_ref"])
        self.assertEqual("gen-a3", fields["login_generation"])

    async def test_save_uses_original_ticket_and_keeps_the_next_step_visible(self):
        await self.page.locator(".login-desktop-save").click()
        await self.page.wait_for_function("!document.querySelector('[data-binding-result]').hidden")
        fields = await self.page.evaluate("requests.find(item=>item.url.endsWith('/save')).fields")
        self.assertEqual("ticket-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", fields["ticket"])
        self.assertEqual("a3", fields["relogin_account_ref"])
        self.assertEqual("gen-a3", fields["login_generation"])
        self.assertIn("登录待确认", await self.page.locator("[data-binding-result-title]").inner_text())
        self.assertIn("account-a1", await self.page.locator("[data-binding-next]").get_attribute("href"))
        self.assertTrue(await self.page.locator(".login-desktop-save").is_disabled())

    async def test_uncertain_save_result_requires_review_before_resubmitting(self):
        await self.page.evaluate("timeoutSave=true")
        await self.page.locator(".login-desktop-save").click()
        await self.page.clock.run_for(120001)
        await self.page.wait_for_function("!document.querySelector('[data-binding-result]').hidden")
        self.assertIn("保存结果待核对", await self.page.locator("[data-binding-result-title]").inner_text())
        self.assertTrue(await self.page.locator(".login-desktop-save").is_disabled())
        self.assertTrue(await self.page.locator(".login-desktop-open").is_disabled())
        self.assertEqual(1, await self.page.evaluate("requests.filter(item=>item.url.endsWith('/save')).length"))
        await self.page.evaluate("leaseLostStatus=true; intervals.forEach(callback=>callback())")
        await self.page.clock.run_for(15001)
        self.assertEqual(0, await self.page.evaluate("requests.filter(item=>item.url.endsWith('/open')).length"))

    async def test_status_loss_during_save_does_not_reacquire_or_replace_its_ticket(self):
        await self.page.evaluate("timeoutSave=true")
        await self.page.locator(".login-desktop-save").click()
        await self.page.evaluate("leaseLostStatus=true; intervals.forEach(callback=>callback())")
        await self.page.clock.run_for(15001)
        self.assertEqual(0, await self.page.evaluate("requests.filter(item=>item.url.endsWith('/open')).length"))
        self.assertEqual("ticket-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", await self.page.locator("#login-desktop-controls").get_attribute("data-workspace-ticket"))

    async def test_confirmed_rollback_keeps_retry_available(self):
        await self.page.evaluate("rolledBackSave=true")
        await self.page.locator(".login-desktop-save").click()
        await self.page.wait_for_function("document.getElementById('login-desktop-status-text').textContent.includes('已撤回')")
        self.assertTrue(await self.page.locator(".login-desktop-save").is_enabled())
        self.assertTrue(await self.page.locator("[data-binding-result]").is_visible())
        self.assertEqual("保存未完成", await self.page.locator("[data-binding-result-title]").inner_text())
        self.assertIn("已撤回", await self.page.locator("[data-binding-result-message]").inner_text())
        original_ticket = await self.page.locator("#login-desktop-controls").get_attribute("data-workspace-ticket")
        await self.page.evaluate("rolledBackSave=false")
        await self.page.locator(".login-desktop-save").click()
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent.includes('登录待确认')")
        saves = await self.page.evaluate("requests.filter(item=>item.url.endsWith('/save')).map(item=>item.fields)")
        self.assertEqual(2, len(saves))
        self.assertEqual([original_ticket, original_ticket], [item["ticket"] for item in saves])
        self.assertNotEqual(saves[0]["request_id"], saves[1]["request_id"])
        self.assertIn("account-a1", await self.page.locator("[data-binding-next]").get_attribute("href"))
