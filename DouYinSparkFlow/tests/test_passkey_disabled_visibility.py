"""Disabled passkeys remain discoverable without exposing credential actions."""
from pathlib import Path
import unittest
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[1] / "webui" / "templates"


class PasskeyDisabledVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)

    def test_disabled_section_explains_setup_without_credential_controls(self):
        html = self.env.get_template("passkeys.html").render(passkeys_enabled=False)
        self.assertIn('id="passkeys"', html)
        self.assertIn("通行密钥未启用", html)
        self.assertIn("HTTPS", html)
        self.assertIn("密码登录仍可使用", html)
        for control in ("data-passkey-panel", "data-passkey-register", "data-passkey-delete", "passkey_current_password"):
            self.assertNotIn(control, html)

    def test_enabled_section_retains_binding_and_removal(self):
        html = self.env.get_template("passkeys.html").render(
            passkeys_enabled=True, csrf_token="synthetic-csrf", passkey_list=[
                {"id": "synthetic-key", "label": "Demo phone", "created_at": "demo"}
            ])
        self.assertIn('data-passkey-register', html)
        self.assertIn('data-passkey-delete="synthetic-key"', html)
        self.assertIn('name="passkey_current_password"', html)
        self.assertNotIn("通行密钥未启用", html)


class PasskeySettingsNavigationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_webui_pages import RedesignedPagesTests
        self.fixture = RedesignedPagesTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_admin_navigation_exposes_disabled_passkeys(self):
        from tests.test_webui_pages import ADMIN
        self.fixture._as(ADMIN)
        response = self.fixture._get("/settings")
        self.assertEqual(200, response.status_code)
        self.assertIn('href="#passkeys"', response.text)
        self.assertIn("通行密钥未启用", response.text)
        self.assertNotIn("data-passkey-register", response.text)

    def test_normal_user_sees_unavailable_status_and_password_form(self):
        from tests.test_webui_pages import FRIEND
        self.fixture._as(FRIEND)
        response = self.fixture._get("/settings")
        self.assertEqual(200, response.status_code)
        self.assertIn('data-passkey-unavailable', response.text)
        self.assertIn('action="/account/password"', response.text)
        self.assertNotIn("data-passkey-register", response.text)


if __name__ == "__main__":
    unittest.main()
