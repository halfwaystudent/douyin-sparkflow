"""Lazy candidate reads stay scoped, bounded in the DOM, and safe to retry."""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from playwright.async_api import async_playwright

from tests import test_shared_security as fixtures
from utils import config
from webui import app as app_module, users
from webui.picker_data import picker_cache


SCRIPT = Path(__file__).resolve().parents[1] / "webui/static/app.js"


class PickerCacheApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        accounts = deepcopy(self.fixture.accounts)
        accounts[0].update(
            login_generation="generation-a1",
            cookies=[{"name": "sessionid", "value": "PRIVATE-COOKIE"}],
            friends_cache=[" Friend ", "Friend", "Second"],
            group_cache=[{"name": "Group", "conversationId": "1000000001", "cookies": "PRIVATE-GROUP-COOKIE", "internalToken": "PRIVATE-GROUP-TOKEN"}],
        )
        accounts[1]["friends_cache"] = ["BOB-PRIVATE-FRIEND"]
        config.save_userData(accounts)
        self.alice = self.fixture.login()

    def get(self, *, client=None, unique_id="111", ref="a1", generation="generation-a1"):
        return (client or self.alice).get(f"/accounts/{unique_id}/picker-cache", params={"ref": ref, "generation": generation})

    def test_owner_receives_only_candidate_names_and_group_ids(self):
        response = self.get()
        self.assertEqual(200, response.status_code)
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual({"friends": ["Friend", "Second"], "groups": [{"name": "Group", "conversationId": "1000000001"}]}, response.json())
        for secret in ("PRIVATE-COOKIE", "PRIVATE-GROUP-COOKIE", "PRIVATE-GROUP-TOKEN", "BOB-PRIVATE-FRIEND", '"cookies"', "internalToken"):
            self.assertNotIn(secret, response.text)

    def test_malformed_candidate_objects_are_not_stringified_into_public_names(self):
        projected = picker_cache({
            "friends_cache": [" Friend ", {"cookies": "PRIVATE-FRIEND-TOKEN"}],
            "group_cache": [
                {"conversationId": "1000000001", "name": {"internalToken": "PRIVATE-GROUP-TOKEN"}},
                {"conversationId": {"internalToken": "PRIVATE-ID-TOKEN"}, "name": "invalid group"},
            ],
        })
        self.assertEqual({"friends": ["Friend"], "groups": [{"conversationId": "1000000001", "name": "未命名群"}]}, projected)
        self.assertNotIn("PRIVATE", json.dumps(projected))

    def test_authentication_scope_and_snapshot_are_checked_before_projection(self):
        self.assertEqual(401, self.get(client=self.fixture.client).status_code)
        bob = self.fixture.login("bob", "Second-secret-2026!")
        self.assertEqual(403, self.get(client=bob).status_code)
        with patch.object(app_module, "picker_cache") as projection:
            for changes in ({"ref": ""}, {"ref": "a2"}, {"generation": "old-generation"}):
                with self.subTest(changes=changes):
                    self.assertEqual(409, self.get(**changes).status_code)
            projection.assert_not_called()

    def test_duplicate_unique_id_selects_the_owned_record_by_ref_and_rejects_mismatch_or_stale_generation(self):
        def duplicate(accounts):
            accounts.append({
                "account_ref": "a3",
                "unique_id": "111",
                "username": "Alice second record",
                "cookies": [],
                "targets": ["second saved target", "second saved target"],
                "friends_cache": ["second private friend"],
                "login_generation": "generation-a3",
            })
        config.update_user_data(duplicate, force_reload=True)
        people = users.get_web_users()
        alice_user = next(person for person in people if person["username"] == "alice")
        alice_user["account_refs"] = ["a3"]
        users.save_web_users(people)
        alice = self.fixture.login()

        response = self.get(client=alice, ref="a3", generation="generation-a3")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(["second private friend"], response.json()["friends"])

        for ref, generation, status in (("a1", "generation-a1", 403), ("a3", "expired", 409)):
            with self.subTest(ref=ref, generation=generation):
                denied = self.get(client=alice, ref=ref, generation=generation)
                self.assertEqual(status, denied.status_code)
                self.assertNotIn("second private friend", denied.text)

    def test_duplicate_unique_id_form_round_trip_saves_the_selected_record(self):
        def duplicate(accounts):
            accounts.append({
                "account_ref": "a3",
                "unique_id": "111",
                "username": "Alice second record",
                "cookies": [],
                "targets": ["old"],
                "login_generation": "generation-a3",
            })
        config.update_user_data(duplicate, force_reload=True)
        people = users.get_web_users()
        alice_user = next(person for person in people if person["username"] == "alice")
        alice_user["account_refs"] = ["a3"]
        users.save_web_users(people)
        alice = self.fixture.login()
        page = alice.get("/accounts")
        self.assertIn('name="account_ref" value="a3"', page.text)
        self.assertIn('name="login_generation" value="generation-a3"', page.text)
        record = next(account for account in config.get_userData(force_reload=True) if account["account_ref"] == "a3")
        response = alice.post(
            "/accounts/111/update",
            data={
                "csrf_token": self.fixture.token(alice),
                "account_ref": "a3",
                "login_generation": "generation-a3",
                "edit_revision": app_module.account_edit_revision(record),
                "username": "updated second record",
                "targets": "second target",
                "enabled": "on",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(200, response.status_code, response.text)
        stored = {account["account_ref"]: account for account in config.get_userData(force_reload=True)}
        self.assertEqual("updated second record", stored["a3"]["username"])
        self.assertEqual(["second target"], stored["a3"]["targets"])
        self.assertEqual("Alice account", stored["a1"]["username"])

    def race(self, mutation):
        def projected(account):
            result = picker_cache(account)
            mutation()
            return result
        with patch.object(app_module, "picker_cache", side_effect=projected):
            return self.get()

    def test_deleted_account_drops_in_flight_payload(self):
        def remove():
            config.update_user_data(lambda accounts: (None, accounts.remove(next(row for row in accounts if row["account_ref"] == "a1")) is None), force_reload=True)
        response = self.race(remove)
        self.assertEqual(404, response.status_code)
        self.assertNotIn("Friend", response.text)

    def test_reused_numeric_id_does_not_return_previous_records_cache(self):
        owner = self.fixture.login("owner", "Owner-secret-2026!")
        def projected(account):
            result = picker_cache(account)
            def replace(accounts):
                accounts[0] = {**accounts[0], "account_ref": "replacement-ref", "friends_cache": ["replacement friend"]}
            config.update_user_data(replace, force_reload=True)
            return result
        with patch.object(app_module, "picker_cache", side_effect=projected):
            response = self.get(client=owner)
        self.assertEqual(409, response.status_code)
        self.assertNotIn("Friend", response.text)

    def test_transferred_account_drops_in_flight_payload(self):
        def transfer():
            people = users.get_web_users()
            people[0]["account_refs"] = []
            people[1]["account_refs"].append("a1")
            users.save_web_users(people)
        response = self.race(transfer)
        self.assertIn(response.status_code, {401, 403})
        self.assertNotIn("Friend", response.text)

    def test_relogin_during_projection_drops_previous_generation(self):
        def relogin():
            def update(accounts):
                accounts[0]["login_generation"] = "generation-a2"
            config.update_user_data(update, force_reload=True)
        response = self.race(relogin)
        self.assertEqual(409, response.status_code)
        self.assertNotIn("Friend", response.text)

    def test_revoked_user_drops_in_flight_payload(self):
        response = self.race(lambda: users.update_web_user("alice", enabled=False))
        self.assertEqual(401, response.status_code)
        self.assertNotIn("Friend", response.text)

    def test_initial_html_does_not_carry_five_thousand_cached_candidates(self):
        def large(accounts):
            accounts[0]["friends_cache"] = [f"Hidden-friend-{i:04d}" for i in range(5000)]
            accounts[0]["group_cache"] = [{"name": f"Hidden-group-{i:04d}", "conversationId": str(1000000000 + i)} for i in range(5000)]
        config.update_user_data(large, force_reload=True)
        response = self.alice.get("/accounts")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("Hidden-friend-4999", response.text)
        self.assertNotIn("Hidden-group-4999", response.text)
        self.assertIn("/accounts/111/picker-cache?ref=a1", response.text)
        self.assertLess(len(response.content), 100000)

    def test_html_like_names_are_absent_from_html_and_preserved_by_owner_api(self):
        friend = '</script><img src=x onerror="window.pickerAttack=1">'
        group = '<img src=x onerror="window.pickerAttack=2">'
        def names(accounts):
            accounts[0]["friends_cache"] = [friend]
            accounts[0]["group_cache"] = [{"name": group, "conversationId": "1000000001"}]
        config.update_user_data(names, force_reload=True)
        page = self.alice.get("/accounts")
        self.assertNotIn("pickerAttack", page.text)
        self.assertNotIn('\\u003cimg src=x', page.text)
        response = self.get()
        self.assertEqual(200, response.status_code)
        self.assertEqual("application/json", response.headers["content-type"])
        self.assertEqual({"friends": [friend], "groups": [{"name": group, "conversationId": "1000000001"}]}, response.json())


class LazyPickerCacheBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()
        self.page = await self.browser.new_page(viewport={"width": 390, "height": 844})
        await self.page.clock.install()
        blocks = []
        for number in range(2):
            cache_url = f"/accounts/{number}/picker-cache?ref=a{number}&generation=g{number}"
            blocks.append(f'''<details class="fold" id="account-{number}"><summary>Account {number}</summary><form id="form-{number}">
              <div class="friend-picker" data-account-id="{number}" data-cache-url="{cache_url}" data-refresh-url="/accounts/{number}/friends/refresh" data-csrf-token="fixture">
                <button type="button" class="friend-refresh-button">刷新好友</button><p class="friend-picker-status"></p>
                <input class="friend-search-input" type="search"><p class="friend-picker-summary"></p><div class="friend-picker-list"></div>
                <script type="application/json" id="friends-cache-{number}">[]</script>
                <script type="application/json" id="selected-targets-{number}">["Saved-only friend"]</script>
              </div><textarea class="targets-textarea" name="targets">Saved-only friend</textarea>
              <section class="group-target-editor"><button type="button" class="group-refresh-button">刷新群</button><p data-group-status></p>
                <div class="group-picker" data-account-id="{number}" data-cache-url="{cache_url}" data-refresh-url="/accounts/{number}/groups/refresh" data-csrf-token="fixture">
                  <div class="group-picker-notices"></div><div class="group-picker-list"><label><input type="checkbox" name="group_bind" value="9999999999" checked><span>Saved group</span></label></div>
                  <script type="application/json" id="groups-cache-{number}">[]</script>
                  <script type="application/json" id="group-bindings-{number}">{{"群聊：Saved group":"9999999999"}}</script>
                </div>
              </section></form></details>''')
        await self.page.set_content("".join(blocks))
        await self.page.evaluate("""() => {
          window.requests = [];
          window.cacheMode = 'immediate';
          window.cacheCalls = 0;
          window.cacheResolve = null;
          window.jobs = {friends:false,groups:false};
          window.friends = Array.from({length:5000},(_,i)=>`Old-friend-${String(i).padStart(4,'0')}`);
          window.groups = Array.from({length:5000},(_,i)=>({name:`Old-group-${String(i).padStart(4,'0')}`,conversationId:String(1000000000+i)}));
          window.fetch = async (url,options={}) => {
            requests.push({url,post:options.method==='POST'});
            const response = (body,status=200)=>new Response(JSON.stringify(body),{status,headers:{'content-type':'application/json'}});
            if(url.includes('/picker-cache')) {
              cacheCalls++;
              if(cacheMode==='fail') return response({error:'fixture cache failure'},503);
              if(cacheMode==='stall') return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('Timed out','AbortError'))));
              if(cacheMode==='manual') return new Promise(resolve=>{cacheResolve=()=>resolve(response({friends,groups}));});
              return response({friends,groups});
            }
            const kind=url.includes('/groups/')?'groups':'friends';
            if(options.method==='POST') {jobs[kind]=true;return response({ok:true,state:'running'},202);}
            if(jobs[kind]) return response({state:'done',message:'Fresh result',updatedAt:'new',friends:['Fresh friend'],groups:[{name:'Fresh group',conversationId:'8888888888'}],targets:[]});
            return response({state:'idle'});
          };
        }""")
        await self.page.add_script_tag(path=str(SCRIPT))

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    async def open(self, number=0):
        await self.page.locator(f"#account-{number}").evaluate("element=>element.open=true")
        await self.page.clock.run_for(1)

    async def loaded(self):
        await self.page.wait_for_function("document.querySelectorAll('#account-0 .friend-option').length===100 && document.querySelectorAll('#account-0 .group-picker-list label').length===100")

    async def form_data(self):
        return await self.page.evaluate("() => { const form=new FormData(document.getElementById('form-0'));return {friends:form.get('targets').split('\\n'),groups:form.getAll('group_bind')}; }")

    async def test_only_open_account_reads_one_shared_cache_and_first_hundred_rows(self):
        self.assertEqual(0, await self.page.evaluate("requests.length"))
        await self.open()
        await self.loaded()
        self.assertEqual(1, await self.page.evaluate("cacheCalls"))
        self.assertTrue(await self.page.evaluate("requests.every(item=>item.url.startsWith('/accounts/0/'))"))
        self.assertLess(await self.page.locator("*").count(), 1000)
        await self.page.locator("#account-0").evaluate("element=>element.open=false")
        await self.page.clock.run_for(1)
        await self.open()
        self.assertEqual(1, await self.page.evaluate("cacheCalls"))

    async def test_more_and_far_search_keep_all_selections_in_submission(self):
        await self.open()
        await self.loaded()
        await self.page.locator("#account-0 .friend-picker [data-picker-more]").click()
        await self.page.locator("#account-0 .group-picker [data-picker-more]").click()
        self.assertEqual(200, await self.page.locator("#account-0 .friend-option").count())
        self.assertEqual(200, await self.page.locator("#account-0 .group-picker-list label").count())
        friend_search = self.page.locator("#account-0 .friend-search-input:not(.group-search-input)")
        await friend_search.fill("Old-friend-4999")
        await self.page.clock.run_for(81)
        self.assertEqual(1, await self.page.locator("#account-0 .friend-option").count())
        await self.page.locator("#account-0 .friend-option input").check()
        group_search = self.page.locator("#account-0 .group-search-input")
        await group_search.fill("Old-group-4999")
        await self.page.clock.run_for(81)
        await self.page.locator('#account-0 [data-group-choice][value="1000004999"]').check()
        await friend_search.fill("Old-friend-0001")
        await group_search.fill("Old-group-0001")
        await self.page.clock.run_for(81)
        data = await self.form_data()
        self.assertEqual({"Saved-only friend", "Old-friend-4999"}, set(data["friends"]))
        self.assertEqual({"9999999999", "1000004999"}, set(data["groups"]))

    async def test_failed_cache_can_be_retried_without_browser_refresh(self):
        await self.page.evaluate("cacheMode='fail'")
        await self.open()
        await self.page.wait_for_function("document.querySelectorAll('#account-0 [data-cache-state] button').length===2")
        self.assertEqual(1, await self.page.evaluate("cacheCalls"))
        await self.page.evaluate("cacheMode='immediate'")
        for picker in (".friend-picker", ".group-picker"):
            await self.page.locator(f"#account-0 {picker} [data-cache-state] button").click()
        await self.loaded()
        self.assertEqual(2, await self.page.evaluate("cacheCalls"))
        self.assertFalse(await self.page.evaluate("requests.some(item=>item.post)"))
        self.assertEqual(["Saved-only friend"], (await self.form_data())["friends"])

    async def test_cache_timeout_preserves_selection_and_manual_retry_recovers(self):
        await self.page.evaluate("cacheMode='stall'")
        await self.open()
        await self.page.clock.run_for(12001)
        await self.page.wait_for_function("document.querySelectorAll('#account-0 [data-cache-state] button').length===2")
        self.assertIn("超时", await self.page.locator("#account-0 .friend-picker [data-cache-state]").inner_text())
        self.assertEqual({"friends": ["Saved-only friend"], "groups": ["9999999999"]}, await self.form_data())
        await self.page.evaluate("cacheMode='immediate'")
        for picker in (".friend-picker", ".group-picker"):
            await self.page.locator(f"#account-0 {picker} [data-cache-state] button").click()
        await self.loaded()
        self.assertEqual(2, await self.page.evaluate("cacheCalls"))

    async def test_late_cache_cannot_replace_completed_browser_refresh(self):
        await self.page.evaluate("cacheMode='manual'")
        await self.open()
        await self.page.wait_for_function("cacheResolve!==null")
        await self.page.locator("#account-0 .friend-refresh-button").click()
        await self.page.locator("#account-0 .group-refresh-button").click()
        await self.page.clock.run_for(2001)
        await self.page.wait_for_function("document.querySelector('#account-0 .friend-picker-status').textContent.includes('Fresh result') && document.querySelector('#account-0 [data-group-status]').textContent.includes('Fresh result')")
        await self.page.evaluate("cacheResolve()")
        await self.page.clock.run_for(1)
        self.assertIn("Fresh friend", await self.page.locator("#account-0 .friend-picker-list").inner_text())
        self.assertIn("Fresh group", await self.page.locator("#account-0 .group-picker-list").inner_text())
        self.assertNotIn("Old-friend", await self.page.locator("#account-0 .friend-picker-list").inner_text())
        self.assertNotIn("Old-group", await self.page.locator("#account-0 .group-picker-list").inner_text())

    async def test_html_like_candidate_names_render_as_text_without_creating_elements(self):
        friend = '</script><img src=x onerror="window.pickerAttack=1">'
        group = '<img src=x onerror="window.pickerAttack=2">'
        await self.page.evaluate("data=>{friends=data.friends;groups=data.groups;}", {
            "friends": [friend], "groups": [{"name": group, "conversationId": "1000000001"}],
        })
        await self.open()
        await self.page.locator("#account-0 .friend-picker-list").get_by_text(friend, exact=True).wait_for()
        await self.page.locator("#account-0 .group-picker-list").get_by_text(group + "（1000000001）", exact=True).wait_for()
        self.assertEqual(0, await self.page.locator(".friend-picker-list img,.friend-picker-list script,.group-picker-list img,.group-picker-list script").count())
        self.assertEqual(0, await self.page.evaluate("window.pickerAttack || 0"))
