"""Each setup form belongs to the account and login shown in that tab."""

import re
import unittest

from tests import test_binding_flow_ui as fixtures
from webui import users
from webui.setup_flow import setup_state


class SetupTabBindingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SetupFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client

    def form(self, ref="a1"):
        page = self.client.get("/setup?step=4&account=" + ref)
        return {name: re.search(r'name="' + name + r'" value="([^"]*)"', page.text).group(1)
                for name in ("csrf_token", "account_ref", "binding_revision")}

    def submit(self, form):
        return self.client.post("/setup/finish", data=form, follow_redirects=False)

    def add_own_account(self, *, targets):
        account = dict(self.fixture.account, account_ref="a3", unique_id="333", username="Second own account", targets=targets)
        self.fixture.accounts.append(account)
        self.fixture.persist()
        users.update_web_user("alice", append_account_ref="a3")
        return account

    def test_second_tab_cannot_change_the_account_checked_by_first_tabs_finish(self):
        self.add_own_account(targets=[])
        form = self.form()
        self.client.get("/setup?step=2&account=a3")
        self.assertEqual("/", self.submit(form).headers["location"])

    def test_first_tabs_missing_targets_cannot_be_hidden_by_second_tabs_ready_account(self):
        self.add_own_account(targets=["Another target"])
        form = self.form()
        self.fixture.account["targets"] = []
        self.fixture.persist()
        self.client.get("/setup?step=4&account=a3")
        self.assertEqual("/setup?step=2", self.submit(form).headers["location"])

    def test_shared_old_form_without_account_binding_never_falls_back_to_session(self):
        form = self.form()
        for field in ("account_ref", "binding_revision"):
            with self.subTest(field=field):
                missing = dict(form)
                missing.pop(field)
                self.assertEqual("/setup", self.submit(missing).headers["location"])
        self.assertIn("页面已过期", self.client.get("/setup").text)

    def test_new_login_generation_requires_reopening_current_account_step(self):
        form = self.form()
        self.fixture.account["login_generation"] = "new-offline-login-generation"
        self.fixture.persist()
        self.assertEqual("/setup?account=a1", self.submit(form).headers["location"])
        self.assertIn("登录身份已更新", self.client.get("/setup?account=a1").text)
        fresh = self.form()
        self.assertNotEqual(form["binding_revision"], fresh["binding_revision"])
        self.assertEqual("/", self.submit(fresh).headers["location"])

    def test_malformed_revision_is_rejected_without_server_error(self):
        form = self.form()
        for value in ("非ASCII版本", "a" * 63, "g" * 64, "a" * 65):
            with self.subTest(value=value):
                response = self.submit(dict(form, binding_revision=value))
                self.assertEqual(303, response.status_code)
                self.assertEqual("/setup", response.headers["location"])
        self.assertIn("页面已过期", self.client.get("/setup").text)

    def test_deleted_or_foreign_account_never_falls_back_to_another_visible_account(self):
        self.add_own_account(targets=["Another target"])
        form = self.form()
        self.fixture.accounts.remove(self.fixture.account)
        self.fixture.persist()
        self.client.get("/setup?step=4&account=a3")
        self.assertEqual("/setup?step=1", self.submit(form).headers["location"])
        self.assertEqual("/setup?step=1", self.submit(dict(form, account_ref="a2")).headers["location"])

    def test_account_transfer_revokes_the_old_session_before_form_can_finish(self):
        form = self.form()
        users.update_web_user("alice", account_refs=[])
        users.update_web_user("bob", append_account_ref="a1")
        self.assertEqual("/login", self.submit(form).headers["location"])
        self.assertEqual([], users.find_web_user("alice")["account_refs"])

    def test_empty_legacy_targets_and_messages_do_not_mark_steps_complete(self):
        account = dict(self.fixture.account, targets=[" ", "\n"], group_targets=[])
        state = setup_state([account], {"messages": [" ", "\n"]}, "a1")
        self.assertFalse(state["has_targets"])
        self.assertFalse(state["has_messages"])
        self.assertFalse(state["can_finish"])
        self.assertEqual(0, state["target_count"])
        self.assertEqual(0, state["message_count"])

    def test_recheck_link_keeps_the_displayed_account_reference(self):
        self.fixture.account["targets"] = []
        self.fixture.persist()
        page = self.client.get("/setup?step=2&account=a1").text
        self.assertIn('href="/setup?step=2&account=a1">已保存，重新检查', page)
