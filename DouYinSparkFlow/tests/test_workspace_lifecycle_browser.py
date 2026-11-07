"""Offline login-workspace lifecycle and page-interaction regressions."""

from pathlib import Path
import unittest

from playwright.async_api import async_playwright

STATIC = Path(__file__).resolve().parents[1] / "webui/static"
TICKET = "ticket-" + "a" * 32
OTHER_TICKET = "ticket-" + "b" * 32

SHELL = '''<div id="login-desktop-runtime-state"></div><p id="login-desktop-status-text"></p>
<button class="login-desktop-open">添加新账号</button><button class="login-desktop-save">保存</button><button class="login-desktop-close">关闭</button>
<section id="interactive-login-section"><div id="login-desktop-controls" data-csrf-token="fixture">
<p data-login-progress></p><ol><li data-login-step="1"></li><li data-login-step="2"></li><li data-login-step="3"></li></ol>
<div class="qr-placeholder"><span>等待申请</span></div><img hidden data-login-qr><p data-login-qr-status></p>
<button data-refresh-login-qr><span>刷新</span></button><button hidden data-recover-login-workspace>重新检查</button></div>
<details data-login-interaction><summary>登录窗口</summary><p data-interaction-status></p><img hidden data-interaction-image>
<input type="password" data-interaction-text><button data-interaction-send>输入</button><button data-interaction-expand>展开</button>
<button data-interaction-refresh>更新画面</button><button data-interaction-key="Enter">确认</button></details></section>
<section hidden data-binding-result><h2 data-binding-result-title></h2><p data-binding-result-message></p><p data-save-check-status hidden></p><button data-recheck-login-save hidden>重新检查保存结果</button><a data-binding-next></a><a data-binding-preferences></a></section>'''

MOCK = r'''() => {
  window.visibility = 'visible';
  Object.defineProperty(document, 'visibilityState', {configurable:true,get:()=>visibility});
  window.jsonResponse = (data, init={}) => { const response=Response.json(data,init); response.json=async()=>data; return response; };
  window.events = []; window.pending = {}; window.aborts = []; window.fail = {}; window.loggedIn = false;
  window.serverWorkspace = {state:'closed',active:false,ticket:''};
  window.activeWorkspace = {state:'active',active:true,ticket:'ticket-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',remaining_seconds:180};
  window.screenBlob = () => {
    const data = atob('iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAIAAAACUFjqAAAAFElEQVR4nGP8z4APMOGVHbHSAEEsAROxCnMTAAAAAElFTkSuQmCC');
    return new Blob([Uint8Array.from(data, c=>c.charCodeAt(0))],{type:'image/png'});
  };
  window.reply = (url) => {
    if (url.includes('/interaction/screen') || url.startsWith('/login-desktop/qr?')) return new Response(screenBlob());
    if (url === '/login-desktop/open') serverWorkspace = {...activeWorkspace};
    if (url === '/login-desktop/close') serverWorkspace = {state:'closed',active:false,ticket:''};
    if (url === '/login-desktop/qr/refresh') return jsonResponse({ok:true});
    if (url === '/login-desktop/save') return jsonResponse({ok:true,account:{account_ref:'a1',unique_id:'111',username:'Alice'},workspace:{state:'closed',active:false,ticket:''},verification:'pending'});
    return jsonResponse({ok:true,workspace:{...serverWorkspace},logged_in:loggedIn,username:loggedIn?'Alice':''});
  };
  window.fetch = (url, options={}) => {
    url = String(url);
    const record = {url,method:options.method||'GET',ticket:options.body?.get('ticket'),action:options.body?.get('action'),requestId:options.body?.get('request_id'),asyncSave:options.body?.get('async_save'),confirmation:options.body?.get('confirm_identity'),mergeWith:options.body?.get('merge_with'),headerTicket:options.headers?.['X-SparkFlow-Workspace'],at:Date.now()};
    events.push(record);
    const key = Object.keys(pending).find(part=>url.includes(part));
    if (key) return new Promise((resolve,reject) => {
      pending[key].push({record,resolve,reject});
      options.signal?.addEventListener('abort',()=>{
        aborts.push(record);
        if (!window.ignoreAbort) reject(new DOMException('aborted','AbortError'));
      },{once:true});
    });
    const failure = Object.keys(fail).find(part=>url.includes(part));
    if (failure) return Promise.resolve(jsonResponse({error:'fixture failure'}, {status:fail[failure]}));
    return Promise.resolve(reply(url));
  };
  Object.defineProperty(navigator,'sendBeacon',{configurable:true,value:(url,form)=>{
    events.push({url,method:'BEACON',ticket:form.get('ticket'),csrf:form.get('csrf_token'),at:Date.now()});
    if (serverWorkspace.ticket === form.get('ticket')) serverWorkspace={state:'closed',active:false,ticket:''};
    return true;
  }});
  window.changeVisibility = value => { visibility=value; document.dispatchEvent(new Event('visibilitychange')); };
  window.setTicket = value => { const root=document.getElementById('login-desktop-controls'); root.dataset.workspaceTicket=value;root.dataset.workspaceActive=value?'1':'0'; };
}'''


class WorkspaceLifecycleBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()
        self.page = await self.browser.new_page(viewport={"width": 390, "height": 844})
        await self.page.clock.install()
        # Installed clocks otherwise advance during real driver/setup awaits,
        # making exact heartbeat boundaries dependent on host load.
        await self.page.clock.pause_at(await self.page.evaluate("Date.now() + 100"))
        await self.page.set_content(SHELL)
        await self.page.evaluate(MOCK)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()
        self.assertEqual([], self.errors)

    async def app(self):
        await self.page.add_script_tag(path=str(STATIC / "app.js"))

    async def interaction(self):
        await self.page.add_script_tag(path=str(STATIC / "login-interaction.js"))

    async def open(self):
        await self.page.locator(".login-desktop-open").click()
        await self.page.wait_for_function("document.getElementById('login-desktop-controls').dataset.workspaceActive === '1'")

    async def calls(self, contains):
        return await self.page.evaluate("part=>events.filter(item=>item.url.includes(part))", contains)

    async def start_cookie_save(self):
        await self.page.evaluate("""() => {
          document.getElementById('login-desktop-controls').insertAdjacentHTML('beforeend',
            '<details open data-cookie-login><input data-cookie-display-name><textarea data-cookie-input>sessionid=offline-fixture</textarea><input type="checkbox" data-cookie-relogin-mode><div data-cookie-relogin-row></div><select data-cookie-relogin-select></select><button data-cookie-login-submit>验证并保存</button><p data-cookie-login-status></p></details>');
          pending['/accounts/cookies']=[];
        }""")
        await self.app()
        await self.open()
        await self.page.locator("[data-cookie-login-submit]").click()
        await self.page.wait_for_function("pending['/accounts/cookies'].length === 1")
        await self.page.evaluate("changeVisibility('hidden')")

    async def test_idle_and_active_status_checks_are_bounded_and_queue_is_faster(self):
        await self.app()
        await self.page.clock.run_for(60050)
        self.assertEqual(3, len(await self.calls("/workspace-status")))
        await self.open()
        await self.page.clock.run_for(60050)
        self.assertEqual(3, len(await self.calls("/login-desktop/status")))
        self.assertEqual(2, len(await self.calls("/heartbeat")))
        self.assertEqual(1, len(await self.calls("/open")))
        await self.page.evaluate("activeWorkspace={state:'queued',active:false,ticket:activeWorkspace.ticket,position:2}; document.querySelector('.login-desktop-open').disabled=false")
        await self.page.locator(".login-desktop-open").click()
        await self.page.wait_for_function("document.getElementById('login-desktop-status-text').textContent.includes('排队中')")
        before = len(await self.calls("/workspace-status"))
        await self.page.clock.run_for(12050)
        self.assertEqual(3, len(await self.calls("/workspace-status")) - before, await self.page.evaluate("events"))

    async def test_hidden_grace_renews_lease_but_stops_frames_and_upstream_status(self):
        await self.app()
        await self.open()
        await self.interaction()
        await self.page.locator("details").evaluate("element=>element.open=true")
        await self.page.clock.run_for(101)
        await self.page.wait_for_function("document.querySelector('[data-interaction-image]').hidden === false")
        await self.page.evaluate("changeVisibility('hidden')")
        screens = len(await self.calls("/interaction/screen"))
        statuses = len(await self.calls("/login-desktop/status"))
        await self.page.clock.run_for(119000)
        self.assertEqual(screens, len(await self.calls("/interaction/screen")))
        self.assertEqual(statuses, len(await self.calls("/login-desktop/status")))
        self.assertEqual(3, len(await self.calls("/heartbeat")))
        self.assertEqual([], await self.calls("/release"))
        await self.page.clock.run_for(1001)
        releases = await self.calls("/release")
        self.assertEqual([(TICKET, "fixture")], [(item["ticket"], item["csrf"]) for item in releases])
        self.assertEqual("", await self.page.locator("#login-desktop-controls").get_attribute("data-workspace-ticket"))
        await self.page.evaluate("changeVisibility('visible')")
        await self.page.clock.run_for(201)
        self.assertEqual(1, len(await self.calls("/open")))
        self.assertTrue(await self.page.locator("[data-interaction-text]").is_disabled())

    async def test_back_forward_restore_checks_ownership_before_enabling_input(self):
        await self.app()
        await self.open()
        await self.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}))")
        self.assertEqual(1, len(await self.calls("/release")))
        await self.page.evaluate("pending['workspace-status']=[];window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))")
        self.assertEqual("0", await self.page.locator("#login-desktop-controls").get_attribute("data-workspace-active"))
        self.assertEqual(1, await self.page.evaluate("pending['workspace-status'].length"))
        await self.page.evaluate("pending['workspace-status'].shift().resolve(jsonResponse({ok:true,workspace:{state:'closed',active:false,ticket:''}}))")
        await self.page.clock.run_for(201)
        self.assertEqual(1, len(await self.calls("/open")))
        self.assertIn("当前未分配", await self.page.locator("#login-desktop-status-text").inner_text())
        await self.page.evaluate("delete pending['workspace-status']")
        await self.open()
        self.assertEqual(2, len(await self.calls("/open")))

    async def test_slow_status_request_is_single_flight_and_aborted_when_hidden(self):
        await self.page.evaluate("pending['workspace-status']=[]")
        await self.app()
        await self.page.evaluate("document.getElementById('interactive-login-section').dispatchEvent(new Event('toggle'))")
        await self.page.clock.run_for(9999)
        self.assertEqual(1, len(await self.calls("/workspace-status")))
        await self.page.evaluate("changeVisibility('hidden')")
        self.assertEqual(1, len(await self.page.evaluate("aborts")))
        await self.page.clock.run_for(60050)
        self.assertEqual(1, len(await self.calls("/workspace-status")))
        await self.page.evaluate("delete pending['workspace-status'];changeVisibility('visible')")
        await self.page.clock.run_for(201)
        self.assertGreaterEqual(len(await self.calls("/workspace-status")), 2)

    async def test_leaving_queue_releases_only_its_ticket_and_does_not_reacquire(self):
        await self.page.evaluate("()=>{replyOriginal=reply;reply=url=>url==='/login-desktop/open'?jsonResponse({ok:true,state:'queued',workspace:{state:'queued',active:false,ticket:activeWorkspace.ticket,position:3}}):replyOriginal(url)}")
        await self.app()
        await self.page.locator(".login-desktop-open").click()
        await self.page.wait_for_function("document.getElementById('login-desktop-controls').dataset.workspaceTicket === activeWorkspace.ticket")
        await self.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}))")
        self.assertEqual([TICKET], [item["ticket"] for item in await self.calls("/release")])
        await self.page.clock.run_for(180000)
        self.assertEqual(1, len(await self.calls("/open")))

    async def test_background_grace_never_releases_an_in_progress_or_uncertain_save(self):
        await self.page.evaluate("pending['/login-desktop/save']=[]")
        await self.app()
        await self.open()
        await self.page.locator(".login-desktop-save").click()
        await self.page.evaluate("changeVisibility('hidden')")
        await self.page.clock.run_for(119000)
        self.assertEqual([], await self.calls("/release"))
        await self.page.clock.run_for(2001)
        self.assertEqual([], await self.calls("/release"))
        self.assertIn("正在核对", await self.page.locator("[data-binding-result-title]").inner_text())
        await self.page.clock.run_for(72050)
        self.assertEqual([], await self.calls("/release"))
        self.assertIn("待核对", await self.page.locator("[data-binding-result-title]").inner_text())
        self.assertTrue(await self.page.locator(".login-desktop-save").is_disabled())
        self.assertTrue(await self.page.locator(".login-desktop-open").is_disabled())

    async def test_late_definite_cookie_failure_releases_an_expired_hidden_grace(self):
        await self.start_cookie_save()
        await self.page.clock.run_for(180050)
        self.assertEqual([], await self.calls("/release"))
        await self.page.evaluate("pending['/accounts/cookies'].shift().resolve(jsonResponse({ok:false,error:'Cookie已失效',retryable:false},{status:400}))")
        await self.page.wait_for_function("document.querySelector('[data-cookie-login-status]').textContent.includes('Cookie已失效')")
        await self.page.clock.run_for(101)
        self.assertEqual([TICKET], [item["ticket"] for item in await self.calls("/release")])
        count = len(await self.calls("/heartbeat"))
        await self.page.clock.run_for(60050)
        self.assertEqual(count, len(await self.calls("/heartbeat")))

    async def test_early_definite_cookie_failure_keeps_original_hidden_deadline(self):
        await self.start_cookie_save()
        await self.page.clock.run_for(30050)
        await self.page.evaluate("pending['/accounts/cookies'].shift().resolve(jsonResponse({ok:false,error:'Cookie已失效',retryable:false},{status:400}))")
        await self.page.wait_for_function("document.querySelector('[data-cookie-login-status]').textContent.includes('Cookie已失效')")
        await self.page.clock.run_for(89000)
        self.assertEqual([], await self.calls("/release"))
        await self.page.clock.run_for(1100)
        self.assertEqual(1, len(await self.calls("/release")))

    async def test_unknown_cookie_result_does_not_release_after_hidden_grace(self):
        await self.start_cookie_save()
        await self.page.clock.run_for(180050)
        await self.page.evaluate("pending['/accounts/cookies'].shift().reject(new TypeError('connection interrupted'))")
        await self.page.wait_for_function("document.querySelector('[data-binding-result-title]').textContent.includes('待核对')")
        await self.page.clock.run_for(60050)
        self.assertEqual([], await self.calls("/release"))
        self.assertTrue(await self.page.locator(".login-desktop-save").is_disabled())
        self.assertTrue(await self.page.locator("[data-cookie-login-submit]").is_disabled())

    async def test_manual_qr_timeout_restores_button_without_automatic_regeneration(self):
        await self.app()
        await self.open()
        await self.page.clock.run_for(150)
        await self.page.wait_for_function("!document.querySelector('[data-refresh-login-qr]').disabled")
        await self.page.evaluate("pending['/qr/refresh']=[]")
        await self.page.locator("[data-refresh-login-qr]").click()
        await self.page.clock.run_for(25001)
        self.assertEqual(1, len(await self.calls("/qr/refresh")))
        self.assertFalse(await self.page.locator("[data-refresh-login-qr]").is_disabled())
        self.assertIn("超时", await self.page.locator("[data-login-qr-status]").inner_text())
        await self.page.clock.run_for(60050)
        self.assertEqual(1, len(await self.calls("/qr/refresh")))

    async def test_logged_in_hint_survives_countdown_and_heartbeat(self):
        await self.app()
        await self.open()
        await self.page.evaluate("loggedIn=true")
        await self.page.clock.run_for(30001)
        self.assertIn("当前浏览器已登录：Alice", await self.page.locator("#login-desktop-status-text").inner_text())
        await self.page.clock.run_for(1001)
        self.assertIn("当前浏览器已登录：Alice", await self.page.locator("#login-desktop-status-text").inner_text())

    async def test_interaction_pause_discards_old_response_and_queued_actions(self):
        await self.page.evaluate("setTicket(activeWorkspace.ticket);document.querySelector('details').open=true;pending['/interaction/screen']=[];ignoreAbort=true")
        await self.interaction()
        await self.page.clock.run_for(1)
        await self.page.evaluate("setTicket('ticket-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')")
        await self.page.evaluate("pending['/interaction/screen'].shift().resolve(jsonResponse({error:'old lease'}, {status:423}))")
        await self.page.clock.run_for(1)
        self.assertEqual([], await self.calls("/open"))
        await self.page.evaluate("pending['/interaction/screen'].shift().resolve(new Response(screenBlob()));delete pending['/interaction/screen']")
        await self.page.wait_for_function("document.querySelector('[data-interaction-image]').hidden === false")
        await self.page.evaluate("pending['/interaction/action']=[]")
        await self.page.locator("[data-interaction-text]").fill("temporary-password")
        await self.page.locator("[data-interaction-text]").press("Enter")
        await self.page.locator("[data-interaction-key]").click()
        await self.page.evaluate("document.querySelector('details').open=false")
        await self.page.wait_for_function("document.querySelector('[data-interaction-text]').value === ''")
        await self.page.evaluate("pending['/interaction/action'].shift().resolve(jsonResponse({ok:true}))")
        await self.page.clock.run_for(10001)
        self.assertEqual(1, len(await self.calls("/interaction/action")))
        self.assertTrue(await self.page.locator("[data-interaction-image]").is_hidden())

    async def test_interaction_resumes_after_back_forward_and_does_not_poll_closed_panel(self):
        await self.page.evaluate("setTicket(activeWorkspace.ticket)")
        await self.interaction()
        await self.page.clock.run_for(60050)
        self.assertEqual([], await self.calls("/interaction/screen"))
        await self.page.locator("details").evaluate("element=>element.open=true")
        await self.page.clock.run_for(1)
        await self.page.wait_for_function("document.querySelector('[data-interaction-image]').hidden === false")
        await self.page.locator("[data-interaction-text]").fill("one-time-value")
        await self.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}))")
        self.assertEqual("", await self.page.locator("[data-interaction-text]").input_value())
        count = len(await self.calls("/interaction/screen"))
        await self.page.clock.run_for(60050)
        self.assertEqual(count, len(await self.calls("/interaction/screen")))
        await self.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))")
        await self.page.clock.run_for(1)
        await self.page.wait_for_function("document.querySelector('[data-interaction-image]').hidden === false")
        self.assertEqual(count + 1, len(await self.calls("/interaction/screen")))

    async def test_interaction_feedback_is_not_overwritten_by_frames_and_expand_has_escape(self):
        await self.page.evaluate("setTicket(activeWorkspace.ticket);document.querySelector('details').open=true")
        await self.interaction()
        await self.page.clock.run_for(1)
        await self.page.wait_for_function("document.querySelector('[data-interaction-image]').hidden === false")
        await self.page.locator("[data-interaction-text]").fill("synthetic-verification")
        await self.page.locator("[data-interaction-text]").press("Enter")
        await self.page.wait_for_function("document.querySelector('[data-interaction-status]').textContent.includes('已输入')")
        await self.page.clock.run_for(3001)
        self.assertIn("已输入", await self.page.locator("[data-interaction-status]").inner_text(), await self.page.evaluate("({events, value:document.querySelector('[data-interaction-text]').value, disabled:document.querySelector('[data-interaction-send]').disabled})"))
        self.assertEqual("", await self.page.locator("[data-interaction-text]").input_value())
        self.assertEqual(1, len(await self.calls("/interaction/action")))
        await self.page.locator("[data-interaction-expand]").click()
        self.assertEqual("true", await self.page.locator("[data-interaction-expand]").get_attribute("aria-expanded"))
        await self.page.keyboard.press("Escape")
        self.assertEqual("false", await self.page.locator("[data-interaction-expand]").get_attribute("aria-expanded"))
