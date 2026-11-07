import asyncio
from contextlib import contextmanager
from pathlib import Path
import unittest
from unittest.mock import patch

from playwright.async_api import async_playwright

from core import personal_streak
from tests import test_shared_security as fixtures
from webui import users


STATIC = Path(__file__).resolve().parents[1] / "webui/static/editing.js"
ALICE = {"username": "alice", "role": "user"}


class PersonalEditorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.login()
        self.form = {"csrf_token": self.fixture.token(self.client), "edit_revision": "", "time": "18:00", "timezone": "Asia/Shanghai", "messages": "自己的消息"}

    def test_ajax_save_returns_revision_and_does_not_leave_stale_flash(self):
        response = self.client.post("/preferences/streak", data=self.form, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(response.json()["edit_revision"])
        self.assertNotIn("你的续火花设置已保存", self.client.get("/accounts").text)

    def test_two_tabs_cannot_silently_overwrite_a_personal_plan(self):
        self.client.post("/preferences/streak", data=self.form)
        revision = personal_streak.preferences(ALICE)["revision"]
        saved = self.client.post("/preferences/streak", data={**self.form, "edit_revision": revision, "messages": "新消息"}, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(200, saved.status_code)
        response = self.client.post("/preferences/streak", data={**self.form, "edit_revision": revision, "messages": "旧页面修改"}, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(409, response.status_code)
        self.assertEqual(["新消息"], personal_streak.preferences(ALICE)["messages"])
        self.assertEqual("edit_conflict", response.json()["category"])

    def test_native_validation_keeps_input_and_displays_the_error(self):
        response = self.client.post("/preferences/streak", data={**self.form, "timezone": "invalid/timezone"})
        self.assertEqual(400, response.status_code)
        self.assertIn("时区无效", response.text)
        self.assertIn("自己的消息", response.text)
        self.assertIn("invalid/timezone", response.text)

    def test_shared_native_form_requires_revision_and_keeps_conflicting_input(self):
        form = {key: value for key, value in self.form.items() if key != "edit_revision"}
        response = self.client.post("/preferences/streak", data=form)
        self.assertEqual(409, response.status_code)
        self.assertIn("自己的消息", response.text)
        self.assertEqual([], personal_streak.preferences(ALICE)["messages"])

    def test_saved_confirmation_is_visible_without_javascript(self):
        response = self.client.post("/preferences/streak", data=self.form)
        self.assertEqual(200, response.status_code)
        self.assertIn("你的续火花设置已保存", response.text)

    def test_session_revoked_while_waiting_cannot_save_a_personal_plan(self):
        original_lock = users._web_users_lock
        @contextmanager
        def revoke_before_write():
            with original_lock():
                users.update_web_user("alice", enabled=False)
                yield
        with patch("webui.personal_streak._web_users_lock", revoke_before_write):
            response = self.client.post("/preferences/streak", data=self.form, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(401, response.status_code)
        raw = users._load_raw()
        self.assertFalse(raw.get("personal_streak"))

    def test_recreated_username_cannot_receive_new_persons_preferences(self):
        original_lock = users._web_users_lock
        @contextmanager
        def replace_before_read():
            with original_lock():
                users.delete_web_user("alice")
                users.create_web_user("alice", "Different-user-secret-2026!")
                personal_streak.save_preferences(ALICE, enabled=False, at_time="19:00", timezone_name="Asia/Shanghai", messages=["new-person-private-message"])
                yield
        with patch("webui.personal_streak._web_users_lock", replace_before_read):
            response = self.client.get("/preferences/streak", follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertEqual("/login", response.headers["location"])
        self.assertNotIn("new-person-private-message", response.text)


class EditorBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()
        self.page = await self.browser.new_page()
        await self.page.set_content('''<base href="https://sparkflow.test/"><a id="leave" href="/setup">下一步</a>
          <details class="fold" open><summary><span data-account-saved-name>旧名称</span><span data-account-target-count>1</span><span data-account-enabled class="pill success">已启用</span></summary>
          <form action="/accounts/111/update" data-editor-form><input name="edit_revision" value="rev-1" type="hidden"><input name="username" value="原账号"><textarea name="targets">好友甲</textarea>
          <button type="submit"><span data-editor-save-label>保存</span></button><span data-editor-state></span><p hidden data-editor-feedback></p><a hidden data-editor-review target="_blank" href="/accounts">核对</a></form></details>
          <form action="/preferences/streak" data-editor-form><input name="edit_revision" value="pref-1" type="hidden"><textarea name="messages">原消息</textarea><button type="submit">保存计划</button><span data-editor-state></span><p hidden data-editor-feedback></p><a hidden data-editor-review target="_blank" href="/preferences/streak">核对</a></form>''')
        await self.page.evaluate("""() => {
          window.calls=[];window.finishSave=null;
          window.fetch=(url, options)=>new Promise(resolve=> {
            calls.push({url, fields:Object.fromEntries(options.body.entries())});
            finishSave=(status=200)=>resolve(new Response(JSON.stringify(status===200 ? {ok:true,message:'设置已保存',edit_revision:'rev-2',account:{username:'保存后的账号',target_count:2,enabled:false}} : {ok:false,error:'另一页已经更新',category:'edit_conflict',edit_revision:'server-newer'}), {status,headers:{'content-type':'application/json'}}));
          });
        }""")
        await self.page.add_script_tag(path=str(STATIC))

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    async def test_save_one_account_preserves_another_forms_draft_and_blocks_double_submit(self):
        await self.page.locator('[name="targets"]').fill("好友甲\n好友乙")
        await self.page.locator('[name="messages"]').fill("还没有保存的消息")
        await self.page.locator("form").first.evaluate("form=>{form.requestSubmit();form.requestSubmit()}")
        self.assertEqual(1, await self.page.evaluate("calls.length"))
        self.assertTrue(await self.page.locator("form").first.locator("button").is_disabled())
        await self.page.evaluate("finishSave()")
        await self.page.wait_for_function("document.querySelector('[name=edit_revision]').value==='rev-2'")
        self.assertEqual("还没有保存的消息", await self.page.locator('[name="messages"]').input_value())
        self.assertIn("未保存", await self.page.locator("form").last.locator("[data-editor-state]").inner_text())
        self.assertEqual("2", await self.page.locator("[data-account-target-count]").inner_text())
        self.assertEqual("已停用", await self.page.locator("[data-account-enabled]").inner_text())

    async def test_conflict_keeps_input_and_does_not_adopt_server_revision_for_blind_retry(self):
        await self.page.locator('[name="targets"]').fill("我的本页修改")
        await self.page.locator("form").first.locator("button").click()
        await self.page.evaluate("finishSave(409)")
        await self.page.wait_for_function("!document.querySelector('[data-editor-review]').hidden")
        self.assertEqual("我的本页修改", await self.page.locator('[name="targets"]').input_value())
        self.assertEqual("rev-1", await self.page.locator('[name="edit_revision"]').first.input_value())
        self.assertTrue(await self.page.locator("form").first.locator("button").is_enabled())

    async def test_leaving_with_changes_can_be_cancelled_without_storing_a_draft(self):
        await self.page.locator('[name="messages"]').fill("留在当前页")
        self.page.once("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
        await self.page.locator("#leave").click()
        self.assertEqual("留在当前页", await self.page.locator('[name="messages"]').input_value())
        self.assertTrue(await self.page.evaluate("Boolean(sparkflowEditorWarning())"))

    async def test_edits_made_while_saving_remain_dirty_after_response(self):
        await self.page.locator('[name="targets"]').fill("提交的修改")
        await self.page.locator("form").first.locator("button").click()
        await self.page.locator('[name="targets"]').fill("保存期间的新修改")
        await self.page.evaluate("finishSave()")
        await self.page.wait_for_function("document.querySelector('[name=edit_revision]').value==='rev-2'")
        self.assertEqual("保存期间的新修改", await self.page.locator('[name="targets"]').input_value())
        self.assertIn("未保存", await self.page.locator("[data-editor-state]").first.inner_text())
        self.assertIn("新增的修改", await self.page.locator("[data-editor-feedback]").first.inner_text())
