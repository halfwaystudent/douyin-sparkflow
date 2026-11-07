from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from unittest.mock import patch

from tests import test_shared_security as fixtures
from utils import config
from webui import app as app_module
from webui.account_editing import account_edit_revision


class AccountEditingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.login()
        self.csrf = self.fixture.token(self.client)

    def account(self):
        return config.get_userData(force_reload=True)[0]

    def save(self, *, client=None, revision=None, targets="好友", username="我的账号", **extra):
        data = {"csrf_token": self.csrf, "username": username, "targets": targets, "enabled": "on", **extra}
        if revision is not None:
            data["edit_revision"] = revision
        return (client or self.client).post("/accounts/111/update", data=data, headers={"x-requested-with": "XMLHttpRequest"}, follow_redirects=False)

    def test_saved_revision_can_be_used_for_the_next_edit(self):
        first = self.save(revision=account_edit_revision(self.account()))
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(1, first.json()["account"]["target_count"])
        self.assertEqual("我的账号", first.json()["account"]["username"])
        self.assertNotIn("cookies", first.json()["account"])
        second = self.save(revision=first.json()["edit_revision"], targets="另一个好友")
        self.assertEqual(200, second.status_code)
        self.assertEqual(["另一个好友"], self.account()["targets"])

    def test_old_form_cannot_silently_replace_newer_settings(self):
        revision = account_edit_revision(self.account())
        self.assertEqual(200, self.save(revision=revision, targets="A", username="新版").status_code)
        before = self.account()
        response = self.save(revision=revision, targets="B", username="旧页面")
        self.assertEqual(409, response.status_code)
        self.assertEqual("edit_conflict", response.json()["category"])
        self.assertEqual(before, self.account())

    def test_shared_mode_requires_revision_without_weakening_csrf(self):
        before = self.account()
        self.assertEqual(409, self.save().status_code)
        self.assertEqual(403, self.save(revision=account_edit_revision(before), csrf_token="wrong").status_code)
        self.assertEqual(before, self.account())

    def test_shared_mode_rejects_an_empty_revision(self):
        before = self.account()
        self.assertEqual(409, self.save(revision="").status_code)
        self.assertEqual(before, self.account())

    def test_cache_health_and_login_updates_do_not_make_edits_conflict(self):
        revision = account_edit_revision(self.account())
        def background(accounts):
            accounts[0].update(friends_cache=["新列表"], account_health={"healthy": True}, login_generation="new-login")
        config.update_user_data(background, force_reload=True)
        self.assertEqual(revision, account_edit_revision(self.account()))
        response = self.save(revision=revision)
        self.assertEqual(200, response.status_code)
        self.assertEqual(["新列表"], self.account()["friends_cache"])
        self.assertEqual("new-login", self.account()["login_generation"])

    def test_recreated_record_does_not_accept_the_previous_forms_revision(self):
        revision = account_edit_revision(self.account())
        def recreate(accounts):
            accounts[0]["account_ref"] = "replacement"
        config.update_user_data(recreate, force_reload=True)
        from webui import users
        users.update_web_user("alice", append_account_ref="replacement")
        self.assertEqual(409, self.save(revision=revision).status_code)

    def test_non_ascii_revision_is_a_conflict_instead_of_a_server_error(self):
        self.assertEqual(409, self.save(revision="旧页面").status_code)

    def test_changed_group_cache_never_silently_discards_new_selection(self):
        def initial(accounts):
            accounts[0]["group_cache"] = [{"name": "旧缓存群", "conversationId": "1000000001"}]
        config.update_user_data(initial, force_reload=True)
        revision = account_edit_revision(self.account())
        config.update_user_data(lambda accounts: accounts[0].update(group_cache=[]), force_reload=True)
        before = deepcopy(self.account())
        response = self.save(revision=revision, group_bind="1000000001")
        self.assertEqual(409, response.status_code)
        self.assertIn("群列表已变化", response.json()["error"])
        self.assertEqual(before, self.account())

    def test_another_users_account_is_still_forbidden(self):
        response = self.client.post("/accounts/222/update", data={"csrf_token": self.csrf, "username": "not allowed", "edit_revision": account_edit_revision(config.get_userData(force_reload=True)[1])}, headers={"x-requested-with": "XMLHttpRequest"})
        self.assertEqual(403, response.status_code)
        self.assertNotIn("edit_revision", response.json())

    def test_two_tabs_using_one_revision_have_only_one_winner(self):
        second = self.fixture.login()
        revision = account_edit_revision(self.account())
        csrf_b = self.fixture.token(second)
        def submit(values):
            client, csrf, target = values
            return self.save(client=client, revision=revision, targets=target, csrf_token=csrf).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(submit, [(self.client, self.csrf, "A"), (second, csrf_b, "B")]))
        self.assertEqual([200, 409], sorted(codes))
        self.assertIn(self.account()["targets"], (["A"], ["B"]))

    def test_plain_form_post_keeps_redirect_behavior(self):
        response = self.client.post("/accounts/111/update", data={"csrf_token": self.csrf, "username": "已保存", "targets": "好友", "enabled": "on", "edit_revision": account_edit_revision(self.account())}, follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertEqual("已保存", self.account()["username"])

    def test_plain_form_conflict_preserves_submitted_content_in_readable_page(self):
        revision = account_edit_revision(self.account())
        self.save(revision=revision, targets="已保存的新目标")
        response = self.client.post("/accounts/111/update", data={"csrf_token": self.csrf, "edit_revision": revision, "username": "本页昵称", "targets": "本页未保存目标"}, follow_redirects=False)
        self.assertEqual(409, response.status_code)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("本页未保存目标", response.text)
        self.assertIn("在新页核对最新设置", response.text)
        self.assertEqual(["已保存的新目标"], self.account()["targets"])

    def test_existing_bound_group_is_editable_even_without_picker_javascript(self):
        from utils import config
        def bind(accounts):
            accounts[0]["targets"] = ["群聊：暂时不在缓存中的群"]
            accounts[0]["group_target_bindings"] = {"群聊：暂时不在缓存中的群": "1234567890123"}
            accounts[0]["group_cache"] = []
        config.update_user_data(bind, force_reload=True)
        page = self.client.get("/accounts")
        self.assertIn('name="group_bind" value="1234567890123" checked', page.text)
        response = self.client.post("/accounts/111/update", data={"csrf_token": self.csrf, "edit_revision": account_edit_revision(self.account()), "username": "只改昵称", "enabled": "on", "group_bind": "1234567890123"}, follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertEqual(["群聊：暂时不在缓存中的群"], self.account()["targets"])
        self.assertEqual({"群聊：暂时不在缓存中的群": "1234567890123"}, self.account()["group_target_bindings"])


class PrivateAccountEditingTests(AccountEditingTests):
    def setUp(self):
        super().setUp()
        with patch.dict(os.environ, {"SPARKFLOW_SHARED_MODE": "0"}):
            self.fixture.application = app_module.create_app()
        self.client = self.fixture.login()
        self.csrf = self.fixture.token(self.client)

    def test_private_mode_requires_revision_for_ajax(self):
        before = self.account()
        response = self.save(targets="私有模式旧页面草稿")
        self.assertEqual(409, response.status_code)
        self.assertEqual("edit_conflict", response.json()["category"])
        self.assertEqual(before, self.account())

    def test_private_mode_rejects_an_empty_revision(self):
        before = self.account()
        response = self.save(revision="", targets="私有模式空令牌草稿")
        self.assertEqual(409, response.status_code)
        self.assertEqual("edit_conflict", response.json()["category"])
        self.assertEqual(before, self.account())
