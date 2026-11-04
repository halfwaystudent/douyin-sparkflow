"""The setup guide only persists while an explicitly started run is active."""

import re
import base64
import json
import unittest
from itsdangerous import TimestampSigner
from utils import config

from tests import test_binding_flow_ui as fixtures


class SetupLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SetupFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client

    def form_value(self, page, name):
        match = re.search(r'name="' + name + r'" value="([^"]*)"', page.text)
        if not match:
            self.fail(f"missing {name} in setup finish form")
        return match.group(1)

    def finish_form(self, path="/setup?step=4"):
        page = self.client.get(path)
        self.assertEqual(200, page.status_code)
        return {
            name: self.form_value(page, name)
            for name in ("csrf_token", "account_ref", "binding_revision", "setup_run_token")
        }

    def finish(self, form):
        return self.client.post("/setup/finish", data=form, follow_redirects=False)

    def test_opening_help_does_not_start_a_cross_page_setup_run(self):
        page = self.client.get("/setup?step=4")
        self.assertIn("设置完成", page.text)
        self.assertIn('href="/setup?restart=1"', page.text)

        accounts = self.client.get("/accounts")
        self.assertNotIn("返回引导继续下一步", accounts.text)
        self.assertIn('href="/setup"', accounts.text)

    def test_explicit_restart_keeps_banner_only_until_finish(self):
        form = self.finish_form("/setup?restart=1&step=4")
        self.assertTrue(form["setup_run_token"])
        self.assertIn("返回引导继续下一步", self.client.get("/accounts").text)

        self.assertEqual("/", self.finish(form).headers["location"])
        self.assertNotIn("返回引导继续下一步", self.client.get("/accounts").text)

    def test_old_tab_cannot_finish_a_newer_setup_run(self):
        old_form = self.finish_form("/setup?restart=1&step=4")
        new_form = self.finish_form("/setup?restart=1&step=4")
        self.assertNotEqual(old_form["setup_run_token"], new_form["setup_run_token"])

        self.assertEqual("/setup", self.finish(old_form).headers["location"])
        self.assertIn("返回引导继续下一步", self.client.get("/accounts").text)

        self.assertEqual("/", self.finish(new_form).headers["location"])
        self.assertNotIn("返回引导继续下一步", self.client.get("/accounts").text)

    def test_incomplete_account_requires_explicit_start_without_leaking_banner(self):
        self.fixture.account["targets"] = []
        self.fixture.persist()

        page = self.client.get("/setup?step=4")
        self.assertIn("先选择并保存发送目标", page.text)
        self.assertIn('href="/setup?restart=1"', page.text)
        self.assertNotIn("返回引导继续下一步", self.client.get("/preferences/streak").text)

    def test_pre_upgrade_session_marker_is_cleared_without_restarting_setup(self):
        self.client.get('/setup?restart=1')
        session=json.loads(base64.b64decode(self.client.cookies.get('session').split('.')[0]))
        session.pop('setup_run_token')
        secret=config.get_app_settings(force_reload=True)['session_secret']
        value=TimestampSigner(secret).sign(base64.b64encode(json.dumps(session).encode())).decode()
        self.client.cookies.clear()
        self.client.cookies.set('session',value)
        page=self.client.get('/accounts')
        self.assertNotIn('返回引导继续下一步',page.text)
        self.assertNotIn('继续设置',page.text)
        self.assertNotIn('返回引导，检查下一步',self.client.get('/preferences/streak').text)


if __name__ == "__main__":
    unittest.main()
