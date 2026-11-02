import unittest

from core.identity import bind_identity
from core.personal_streak import save_preferences
from tests import test_shared_security as fixtures
from utils import config
from webui.setup_flow import requested_step, setup_state


class SetupFlowTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.client = fixture.login()
        self.csrf = fixture.token(self.client)
        self.accounts = config.get_userData(force_reload=True)
        self.account = self.accounts[0]
        self.account.update(cookies=[{"name": "sessionid", "value": "offline-private"}], targets=["好友"])
        bind_identity(self.account, {"uid": "111"}, "fixture")
        self.profile = save_preferences({"username": "alice", "role": "user"}, enabled=False,
                                        at_time="18:00", timezone_name="Asia/Shanghai", messages=["自己的文字"])
        self.persist()

    def persist(self):
        config.save_userData(self.accounts)

    def finish(self):
        return self.client.post("/setup/finish", data={"csrf_token": self.csrf}, follow_redirects=False)

    def test_cookies_alone_are_not_a_verified_binding(self):
        self.account.pop("identity_binding")
        self.persist()
        response = self.client.get("/setup?step=4")
        self.assertIn("第 1 步，共 4 步", response.text)
        self.assertIn("还没有完成可信身份绑定", response.text)
        self.assertEqual("/setup?step=1", self.finish().headers["location"])

    def test_missing_messages_cannot_skip_to_finished(self):
        from webui import users
        with users._web_users_lock():
            raw = users._load_raw()
            raw.pop("personal_streak", None)
            users._atomic_write_json(users.USERS_FILE, raw)
        self.assertIn("第 3 步，共 4 步", self.client.get("/setup?step=4").text)
        self.assertEqual("/setup?step=3", self.finish().headers["location"])

    def test_targets_removed_after_opening_step_four_are_rechecked(self):
        self.assertIn("第 4 步，共 4 步", self.client.get("/setup?step=4").text)
        self.account["targets"] = []
        self.persist()
        self.assertEqual("/setup?step=2", self.finish().headers["location"])

    def test_login_failure_or_identity_conflict_requires_relogin(self):
        for field, value in (("login_required", True), ("identity_mismatch", {"expected": "111", "actual": "222"})):
            with self.subTest(field=field):
                self.account[field] = value
                self.persist()
                self.assertIn("第 1 步，共 4 步", self.client.get("/setup?step=4").text)
                self.assertEqual("/setup?step=1", self.finish().headers["location"])
                self.account.pop(field)

    def test_waiting_or_network_failure_is_not_presented_as_a_passed_verification(self):
        for state in ("verifying", "unverified"):
            with self.subTest(state=state):
                self.account["login_verification"] = {"state": state, "category": "network_unavailable", "reason": "稍后核对"}
                self.persist()
                page = self.client.get("/setup?step=4")
                self.assertIn("配置已保存，登录待确认", page.text)
                self.assertNotIn("<h2>设置完成</h2>", page.text)
                self.assertEqual("/", self.finish().headers["location"])
                self.assertIn("登录状态仍待确认", self.client.get("/").text)

    def test_manual_use_can_finish_with_automatic_sending_disabled(self):
        self.assertIn("自动发送已暂停", self.client.get("/setup?step=4").text)
        self.assertEqual("/", self.finish().headers["location"])
        self.assertIn("需要时可以手动发送", self.client.get("/").text)

    def test_other_users_account_reference_is_never_used(self):
        state = setup_state([self.account], self.profile, "a2")
        self.assertIsNone(state["setup_account"])
        self.assertFalse(state["can_finish"])
        self.assertEqual(1, requested_step("broken", state))

    def test_deleted_selected_account_does_not_complete_setup_with_another_account(self):
        self.client.get("/setup?account=a1&step=4")
        another = dict(self.account, account_ref="a3", unique_id="333", username="Another own account")
        self.accounts.append(another)
        from webui import users
        self.persist()
        users.update_web_user("alice", append_account_ref="a3")
        self.accounts.remove(self.account)
        self.persist()
        page = self.client.get("/setup?step=4")
        self.assertIn("之前选择的账号已移除", page.text)
        self.assertIn("第 1 步，共 4 步", page.text)
        self.assertEqual("/setup?step=1", self.finish().headers["location"])
