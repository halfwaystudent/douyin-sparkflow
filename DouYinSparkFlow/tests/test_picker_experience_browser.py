from pathlib import Path
import unittest

from playwright.async_api import async_playwright


SCRIPT = Path(__file__).resolve().parents[1] / "webui/static/app.js"


class PickerExperienceBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()
        self.page = await self.browser.new_page()
        await self.page.clock.install()
        await self.page.set_content('''<form id="account-form">
          <div class="friend-picker" data-account-id="1" data-refresh-url="/accounts/1/friends/refresh" data-csrf-token="fixture">
            <button type="button" class="friend-refresh-button">刷新好友</button>
            <p class="friend-picker-status">上次刷新：old</p><input type="search" class="friend-search-input">
            <p class="friend-picker-summary"></p><div class="friend-picker-list"></div>
            <script type="application/json" id="friends-cache-1">["Alpha","Beta"]</script>
            <script type="application/json" id="selected-targets-1">["Alpha"]</script>
          </div><textarea name="targets" class="targets-textarea">Alpha</textarea>
          <section class="group-target-editor"><button type="button" class="group-refresh-button">刷新群列表</button>
            <p data-group-status>上次刷新：old</p>
            <div class="group-picker" data-account-id="1" data-refresh-url="/accounts/1/groups/refresh" data-csrf-token="fixture">
              <div class="group-picker-notices"></div><div class="group-picker-list"></div>
              <script type="application/json" id="groups-cache-1">[{"name":"原群","conversationId":"1111111111"},{"name":"新群","conversationId":"2222222222"}]</script>
              <script type="application/json" id="group-bindings-1">{"群聊：原群":"1111111111","群聊：未读取群":"3333333333"}</script>
            </div>
          </section></form>''')
        await self.page.evaluate("""() => {
          window.requests = [];
          window.jobs = { friends: null, groups: null };
          window.nextGroups = [{name:'新群已改名',conversationId:'2222222222'},{name:'另一个群',conversationId:'4444444444'}];
          window.nextFriends = ['Alpha','Beta','Delta'];
          window.stallPost = false;
          window.stallStatus = false;
          window.refuseStart = false;
          window.fetch = async (url, options={}) => {
            const kind = url.includes('/groups/') ? 'groups' : 'friends';
            const post = options.method === 'POST';
            requests.push({kind,post,url});
            const response = (body,status=200) => new Response(JSON.stringify(body), {status,headers:{'content-type':'application/json'}});
            const stalled = () => new Promise((resolve,reject) => options.signal.addEventListener('abort',()=>reject(new DOMException('Timeout','AbortError'))));
            if (post) {
              if (refuseStart) return response({error:'发送任务正在运行，请稍后刷新。',previousUpdatedAt:'old'},409);
              jobs[kind] = {finishAt:Date.now()+1000};
              if (stallPost) return stalled();
              return response({ok:true,state:'running'},202);
            }
            if (stallStatus) return stalled();
            const job = jobs[kind];
            if (!job) return response({state:'idle',previousUpdatedAt:'old'});
            if (Date.now() < job.finishAt) return response({state:'running',stage:'collecting',collected:3});
            return response({state:'done',friends:nextFriends,groups:nextGroups,targets:[],updatedAt:'new',message:'列表已刷新'});
          };
        }""")

    async def start(self):
        await self.page.add_script_tag(path=str(SCRIPT))
        await self.page.wait_for_function("requests.length >= 2")
        await self.page.wait_for_function("!document.querySelector('.friend-refresh-button').disabled && !document.querySelector('.group-refresh-button').disabled")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    def group(self, conversation_id):
        return self.page.locator(f'.group-picker-list input[type="checkbox"][value="{conversation_id}"]')

    async def submitted_groups(self):
        return await self.page.evaluate("new FormData(document.getElementById('account-form')).getAll('group_bind')")

    async def test_refresh_keeps_new_checked_and_unchecked_groups_and_missing_bound_group(self):
        await self.start()
        await self.group("2222222222").check()
        await self.group("1111111111").uncheck()
        await self.page.locator(".group-refresh-button").click()
        await self.page.clock.run_for(2001)
        await self.page.wait_for_function("document.querySelector('[data-group-status]').textContent.includes('列表已刷新')")
        self.assertTrue(await self.group("2222222222").is_checked())
        self.assertEqual(0, await self.group("1111111111").count())
        self.assertTrue(await self.group("3333333333").is_checked())
        self.assertIn("本次未读取到", await self.group("3333333333").locator("..").inner_text())
        self.assertEqual(["2222222222", "3333333333"], await self.submitted_groups())
        await self.group("3333333333").uncheck()
        self.assertEqual(["2222222222"], await self.submitted_groups())

    async def test_group_search_does_not_drop_hidden_selected_ids_from_submission(self):
        await self.start()
        await self.group("2222222222").check()
        await self.page.locator(".group-search-input").fill("新群")
        await self.page.clock.run_for(81)
        self.assertEqual(1, await self.page.locator(".group-picker-list input[type=checkbox]").count())
        self.assertEqual(["1111111111", "2222222222", "3333333333"], await self.submitted_groups())


    async def test_checkbox_keeps_keyboard_focus_and_search_avoids_identical_redraw(self):
        await self.start()
        await self.page.evaluate("window.choice=document.querySelector('.friend-picker-list label:last-child input');choice.focus()")
        await self.page.locator(".friend-picker-list label:last-child input").press("Space")
        self.assertTrue(await self.page.evaluate("document.activeElement===choice"))
        self.assertIn("Beta", await self.page.locator(".targets-textarea").input_value())
        await self.page.locator(".friend-search-input:not(.group-search-input)").fill("Al")
        await self.page.clock.run_for(81)
        await self.page.evaluate("window.filtered=document.querySelector('.friend-picker-list label')")
        await self.page.locator(".friend-search-input:not(.group-search-input)").fill("Alpha")
        await self.page.clock.run_for(81)
        self.assertTrue(await self.page.evaluate("filtered===document.querySelector('.friend-picker-list label')"))

    async def test_uncertain_start_recovers_without_a_second_post_or_losing_selection(self):
        await self.start()
        await self.group("2222222222").check()
        await self.page.evaluate("stallPost=true")
        await self.page.locator(".group-refresh-button").click()
        await self.page.clock.run_for(12001)
        await self.page.wait_for_function("!document.querySelector('.group-refresh-button').disabled")
        self.assertIn("后台可能仍在处理", await self.page.locator("[data-group-status]").inner_text())
        self.assertTrue(await self.group("2222222222").is_checked())
        await self.page.evaluate("stallPost=false")
        await self.page.locator(".group-refresh-button").click()
        await self.page.wait_for_function("document.querySelector('[data-group-status]').textContent.includes('列表已刷新')")
        self.assertEqual(1, await self.page.evaluate("requests.filter(item=>item.kind==='groups'&&item.post).length"))
        self.assertTrue(await self.group("2222222222").is_checked())

    async def test_restored_job_and_manual_request_share_one_poll_and_cover_server_budget(self):
        await self.page.evaluate("jobs.groups={finishAt:Date.now()+250000}")
        await self.page.add_script_tag(path=str(SCRIPT))
        await self.page.wait_for_function("document.querySelector('.group-refresh-button').disabled")
        await self.page.clock.run_for(210001)
        self.assertIn("正在读取群列表", await self.page.locator("[data-group-status]").inner_text())
        await self.page.clock.run_for(40001)
        await self.page.wait_for_function("!document.querySelector('.group-refresh-button').disabled")
        self.assertIn("列表已刷新", await self.page.locator("[data-group-status]").inner_text())
        self.assertEqual(0, await self.page.evaluate("requests.filter(item=>item.kind==='groups'&&item.post).length"))
        self.assertLessEqual(await self.page.evaluate("requests.filter(item=>item.kind==='groups'&&!item.post).length"), 127)

    async def test_failed_refresh_keeps_cache_and_selected_targets_and_restores_button(self):
        await self.start()
        await self.page.evaluate("refuseStart=true")
        await self.page.locator(".friend-refresh-button").click()
        await self.page.wait_for_function("!document.querySelector('.friend-refresh-button').disabled")
        status = await self.page.locator(".friend-picker-status").inner_text()
        self.assertIn("发送任务正在运行", status)
        self.assertIn("old", status)
        self.assertEqual("Alpha", await self.page.locator(".targets-textarea").input_value())
        self.assertEqual(2, await self.page.locator(".friend-picker-list input").count())

    async def test_friend_recovery_and_batch_calls_join_the_same_job(self):
        await self.page.evaluate("jobs.friends={finishAt:Date.now()+4000}")
        await self.page.add_script_tag(path=str(SCRIPT))
        await self.page.evaluate("() => { window.joined=Promise.all([document.querySelector('.friend-picker').refreshFriends(),document.querySelector('.friend-picker').refreshFriends()]); }")
        await self.page.clock.run_for(4001)
        result = await self.page.evaluate("joined")
        self.assertTrue(all(item["ok"] for item in result))
        self.assertEqual(0, await self.page.evaluate("requests.filter(item=>item.kind==='friends'&&item.post).length"))
        self.assertLessEqual(await self.page.evaluate("requests.filter(item=>item.kind==='friends'&&!item.post).length"), 3)

    async def test_failure_after_a_success_keeps_the_new_refresh_time(self):
        await self.start()
        await self.page.locator(".friend-refresh-button").click()
        await self.page.clock.run_for(2001)
        await self.page.wait_for_function("!document.querySelector('.friend-refresh-button').disabled")
        await self.page.evaluate("refuseStart=true")
        await self.page.locator(".friend-refresh-button").click()
        await self.page.wait_for_function("document.querySelector('.friend-picker-status').textContent.includes('发送任务正在运行')")
        self.assertIn("上次成功刷新：new", await self.page.locator(".friend-picker-status").inner_text())
        self.assertEqual("Alpha", await self.page.locator(".targets-textarea").input_value())

    async def test_search_enter_does_not_submit_the_account_form(self):
        await self.start()
        await self.page.evaluate("window.submits=0;document.getElementById('account-form').addEventListener('submit',event=>{event.preventDefault();submits++;})")
        for selector in (".friend-search-input:not(.group-search-input)", ".group-search-input"):
            await self.page.locator(selector).fill("test")
            await self.page.locator(selector).press("Enter")
        self.assertEqual(0, await self.page.evaluate("submits"))

    async def test_page_leave_aborts_pending_read_and_back_restores_status_without_resetting_choices(self):
        await self.start()
        await self.group("2222222222").check()
        await self.page.evaluate("stallStatus=true")
        await self.page.locator(".group-refresh-button").click()
        await self.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
        await self.page.wait_for_function("!document.querySelector('.group-refresh-button').disabled")
        await self.page.evaluate("stallStatus=false;window.beforeBack=requests.filter(item=>item.kind==='groups').length;window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))")
        await self.page.wait_for_function("requests.filter(item=>item.kind==='groups').length>beforeBack")
        self.assertTrue(await self.group("2222222222").is_checked())
        self.assertEqual(["1111111111", "2222222222", "3333333333"], await self.submitted_groups())


class LazyPickerBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()
        self.page = await self.browser.new_page(viewport={"width": 390, "height": 844})
        await self.page.clock.install()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    def markup(self, *, first_open=False):
        import json
        accounts = []
        for number in range(10):
            friends = [f"好友-{number}-{index:04d}" for index in range(500)]
            selected = friends[:1]
            group_id = str(1000000000 + number)
            accounts.append(f'''<details class="fold" id="account-{number}" {"open" if number == 0 and first_open else ""}>
              <summary>账号 {number}</summary><form id="form-{number}">
              <div class="friend-picker" data-account-id="{number}" data-refresh-url="/accounts/{number}/friends/refresh" data-csrf-token="fixture">
              <button type="button" class="friend-refresh-button">刷新好友</button><p class="friend-picker-status"></p>
              <input class="friend-search-input" type="search"><p class="friend-picker-summary"></p><div class="friend-picker-list"></div>
              <script id="friends-cache-{number}" type="application/json">{json.dumps(friends, ensure_ascii=False)}</script>
              <script id="selected-targets-{number}" type="application/json">{json.dumps(selected, ensure_ascii=False)}</script></div>
              <textarea class="targets-textarea" name="targets">{selected[0]}</textarea>
              <section class="group-target-editor"><button type="button" class="group-refresh-button">刷新群</button><p data-group-status></p>
              <div class="group-picker" data-account-id="{number}" data-refresh-url="/accounts/{number}/groups/refresh" data-csrf-token="fixture">
              <div class="group-picker-notices"></div><div class="group-picker-list"><label><input type="checkbox" name="group_bind" value="{group_id}" checked><span>原生群选择</span></label></div>
              <script id="groups-cache-{number}" type="application/json">[{{"name":"原群","conversationId":"{group_id}"}}]</script>
              <script id="group-bindings-{number}" type="application/json">{{"群聊：原群":"{group_id}"}}</script></div></section>
              </form></details>''')
        return '<button type="button" data-refresh-all-friends>刷新全部好友</button><p data-refresh-all-friends-summary hidden></p>' + "".join(accounts)

    async def mock_requests(self):
        await self.page.evaluate("""() => {
          window.requests=[];
          window.started=new Set();
          window.fetch=async (url,options={})=>{
            const post=options.method==='POST';
            requests.push({url,post});
            const key=url.replace(/\\/(?:status|async)$/,'');
            const respond=body=>new Response(JSON.stringify(body),{headers:{'content-type':'application/json'}});
            if(post){started.add(key);return respond({ok:true,state:'running'});}
            if(started.has(key))return respond({state:'done',message:'已刷新',updatedAt:'new'});
            return respond({state:'idle'});
          };
        }""")

    async def load(self, *, first_open=False, eager=False):
        await self.page.set_content(self.markup(first_open=first_open))
        await self.mock_requests()
        source = SCRIPT.read_text()
        if eager:
            source = source.replace("initializeWhenVisible(picker, initialize);", "initialize();")
        await self.page.add_script_tag(content=source)
        await self.page.clock.run_for(1)

    async def test_closed_ten_by_five_hundred_list_skips_all_rows_and_initial_requests(self):
        await self.load()
        self.assertEqual(0, await self.page.locator(".friend-option").count())
        self.assertEqual(0, await self.page.evaluate("requests.length"))
        self.assertLess(await self.page.locator("*").count(), 1000)
        self.assertEqual(["1000000000"], await self.page.evaluate("new FormData(document.getElementById('form-0')).getAll('group_bind')"))

    async def test_eager_comparison_limits_ten_accounts_to_one_hundred_rows_each(self):
        await self.load(eager=True)
        self.assertEqual(1000, await self.page.locator(".friend-option").count())
        self.assertEqual(20, await self.page.evaluate("requests.length"))
        self.assertGreater(await self.page.locator("*").count(), 3000)

    async def test_opening_one_account_only_initializes_once_and_preserves_group_form_values(self):
        await self.load()
        await self.page.evaluate("document.getElementById('account-0').open=true")
        await self.page.clock.run_for(1)
        await self.page.wait_for_function("document.querySelectorAll('.friend-option').length===100")
        self.assertEqual(2, await self.page.evaluate("requests.length"))
        self.assertEqual(["1000000000"], await self.page.evaluate("new FormData(document.getElementById('form-0')).getAll('group_bind')"))
        await self.page.evaluate("document.getElementById('account-0').open=false")
        await self.page.clock.run_for(1)
        await self.page.evaluate("document.getElementById('account-0').open=true")
        await self.page.clock.run_for(1)
        self.assertEqual(100, await self.page.locator(".friend-option").count())
        self.assertEqual(2, await self.page.evaluate("requests.length"))
        self.assertEqual(1, await self.page.locator(".group-search-input").count())

    async def test_query_opened_account_is_initialized_on_first_load(self):
        await self.page.route("http://sparkflow.test/**", lambda route: route.fulfill(content_type="text/html", body=self.markup()))
        await self.page.goto("http://sparkflow.test/accounts?_anchor=account-0")
        await self.mock_requests()
        await self.page.add_script_tag(path=str(SCRIPT))
        await self.page.clock.run_for(1)
        self.assertTrue(await self.page.locator("#account-0").evaluate("element=>element.open"))
        self.assertEqual(100, await self.page.locator(".friend-option").count())
        self.assertEqual(2, await self.page.evaluate("requests.length"))

    async def test_duplicate_unique_id_record_keys_isolate_friend_selection(self):
        await self.page.set_content('''
          <details open id="first"><summary>first</summary><form id="first-form">
            <div class="friend-picker" data-account-id="111" data-record-key="a1" data-refresh-url="/accounts/111/friends/refresh" data-csrf-token="fixture">
              <button type="button" class="friend-refresh-button">刷新好友</button><p class="friend-picker-status"></p>
              <input class="friend-search-input" type="search"><p class="friend-picker-summary"></p><div class="friend-picker-list"></div>
              <script type="application/json" id="friends-cache-a1">["first friend"]</script>
              <script type="application/json" id="selected-targets-a1">["first friend"]</script>
            </div><textarea class="targets-textarea" name="targets">first friend</textarea>
          </form></details>
          <details open id="second"><summary>second</summary><form id="second-form">
            <div class="friend-picker" data-account-id="111" data-record-key="a3" data-refresh-url="/accounts/111/friends/refresh" data-csrf-token="fixture">
              <button type="button" class="friend-refresh-button">刷新好友</button><p class="friend-picker-status"></p>
              <input class="friend-search-input" type="search"><p class="friend-picker-summary"></p><div class="friend-picker-list"></div>
              <script type="application/json" id="friends-cache-a3">["second friend"]</script>
              <script type="application/json" id="selected-targets-a3">["second friend"]</script>
            </div><textarea class="targets-textarea" name="targets">second friend</textarea>
          </form></details>''')
        await self.page.add_script_tag(path=str(SCRIPT))
        await self.page.wait_for_function("document.querySelectorAll('.friend-option').length === 2")
        self.assertEqual("first friend", await self.page.locator("#first .friend-picker-list").inner_text())
        self.assertEqual("second friend", await self.page.locator("#second .friend-picker-list").inner_text())
        await self.page.locator("#second .friend-option input").uncheck()
        self.assertEqual("first friend", await self.page.locator("#first-form .targets-textarea").input_value())
        self.assertEqual("", await self.page.locator("#second-form .targets-textarea").input_value())

    async def test_batch_refresh_initializes_closed_accounts_and_posts_once_per_account(self):
        await self.load()
        await self.page.locator("[data-refresh-all-friends]").click()
        await self.page.clock.run_for(20050)
        await self.page.wait_for_function("!document.querySelector('[data-refresh-all-friends]').disabled")
        self.assertEqual(10, await self.page.evaluate("requests.filter(item=>item.post).length"))
        self.assertEqual(20, await self.page.evaluate("requests.filter(item=>!item.post).length"))
        self.assertFalse(await self.page.evaluate("requests.some(item=>item.url.includes('/groups/'))"))
        self.assertIn("全部成功", await self.page.locator("[data-refresh-all-friends-summary]").inner_text())

