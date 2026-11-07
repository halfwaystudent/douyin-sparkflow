import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from webauthn.helpers.exceptions import WebAuthnException

from tests import test_shared_security as fixtures
from tests.test_webui_pages import _snapshot
from tests.auth_helpers import login_form
from webui import app as app_module, passkeys, users


class PasskeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = sync_playwright().start()
        try:
            cls.browser = cls.runtime.chromium.launch()
        except Exception:
            cls.runtime.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for context in (
            patch.object(passkeys, "PASSKEYS_FILE", users.USERS_FILE.parent / "passkeys.json"),
            patch.dict(os.environ, {"SPARKFLOW_PASSKEY_RP_ID": "sparkflow.test", "SPARKFLOW_PASSKEY_ORIGIN": "https://sparkflow.test",
                                    "SPARKFLOW_ALLOWED_HOSTS": "sparkflow.test", "SPARKFLOW_SESSION_COOKIE_SECURE": "1"}),
            patch.object(app_module, "get_ops_snapshot", side_effect=lambda **_: _snapshot()),
            patch.object(app_module, "get_overview_snapshot", return_value={}),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.app = app_module.create_app()
        self.client = TestClient(self.app, base_url="https://sparkflow.test")
        self.store = passkeys.PasskeyStore("sparkflow.test", "https://sparkflow.test")
        self.identity = passkeys.login_identity("alice")
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        cdp = self.context.new_cdp_session(self.page)
        cdp.send("WebAuthn.enable")
        cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
            "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True,
        }})

        def respond(route):
            request = route.request
            if not request.url.startswith("https://sparkflow.test/"):
                route.abort()
                return
            response = self.client.request(request.method, request.url, content=request.post_data_buffer,
                                           headers={k: v for k, v in request.headers.items() if k != "accept-encoding"}, follow_redirects=False)
            headers = {key: value for key, value in response.headers.items() if key not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        self.page.route("**/*", respond)
        self.page.goto("https://sparkflow.test/login")

    def register(self):
        challenge = self.store.begin(self.identity, "session-a", registration=True)
        credential = self.page.evaluate("""async options => {
            const credential = await navigator.credentials.create({publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(options)});
            return credential.toJSON();
        }""", challenge["options"])
        self.store.finish(challenge["challenge_id"], "session-a", credential, registration=True, identity=self.identity, label="Test phone")
        return credential

    def assertion(self, challenge):
        return self.page.evaluate("""async options => {
            const credential = await navigator.credentials.get({publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(options)});
            return credential.toJSON();
        }""", challenge["options"])

    def test_real_browser_enrolment_and_login_through_the_ui(self):
        response = self.client.post("/login", data=login_form(self.client, "alice", "First-secret-2026!"), follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.context.clear_cookies()
        self.page.goto("https://sparkflow.test/settings?_anchor=passkeys")
        self.page.locator('[name="passkey_label"]').fill("Test phone")
        self.page.locator('[name="passkey_current_password"]').fill("First-secret-2026!")
        self.page.locator("[data-passkey-register]").click()
        self.page.get_by_role("button", name="移除", exact=True).wait_for()
        self.assertEqual(1, len(self.store.list_keys(self.identity)))
        self.context.clear_cookies()
        self.client.cookies.clear()
        self.page.goto("https://sparkflow.test/login")
        self.page.locator('form[action="/login"] [name="username"]').fill("alice")
        self.page.locator("[data-passkey-login]").click()
        self.page.wait_for_url("https://sparkflow.test/")
        self.assertEqual(200, self.client.get("/settings").status_code)

    def test_signature_is_verified_and_challenge_cannot_be_replayed(self):
        self.register()
        challenge = self.store.begin(self.identity, "browser-a")
        credential = self.assertion(challenge)
        result = self.store.finish(challenge["challenge_id"], "browser-a", credential)
        self.assertEqual("alice", result["username"])
        with self.assertRaises(ValueError):
            self.store.finish(challenge["challenge_id"], "browser-a", credential)
        challenge = self.store.begin(self.identity, "browser-a")
        tampered = self.assertion(challenge)
        tampered["response"]["signature"] = "AAAA"
        with self.assertRaises(WebAuthnException):
            self.store.finish(challenge["challenge_id"], "browser-a", tampered)

    def test_expired_or_different_browser_challenge_is_rejected(self):
        self.register()
        challenge = self.store.begin(self.identity, "browser-a")
        credential = self.assertion(challenge)
        with self.assertRaises(ValueError):
            self.store.finish(challenge["challenge_id"], "browser-b", credential)
        challenge = self.store.begin(self.identity, "browser-a")
        with patch.object(passkeys.time, "time", return_value=time.time() + 121):
            with self.assertRaises(ValueError):
                self.store.finish(challenge["challenge_id"], "browser-a", credential)

    def test_revoked_user_and_deleted_key_cannot_finish_login(self):
        self.register()
        challenge = self.store.begin(self.identity, "browser-a")
        credential = self.assertion(challenge)
        users.update_web_user("alice", enabled=False)
        with self.assertRaises(ValueError):
            self.store.finish(challenge["challenge_id"], "browser-a", credential)
        users.update_web_user("alice", enabled=True)
        identity = passkeys.login_identity("alice")
        challenge = self.store.begin(identity, "browser-a")
        credential = self.assertion(challenge)
        self.store.delete(identity, credential["id"])
        with self.assertRaises(ValueError):
            self.store.finish(challenge["challenge_id"], "browser-a", credential)

    def test_recreated_username_does_not_inherit_old_passkeys(self):
        self.register()
        users.delete_web_user("alice")
        users.create_web_user("alice", "New-secret-2026!")
        self.assertNotEqual(self.identity["user_key"], passkeys.login_identity("alice")["user_key"])
        self.assertEqual([], self.store.list_keys(passkeys.login_identity("alice")))

    def test_admin_can_remove_keys_from_a_disabled_user(self):
        self.register()
        users.update_web_user("alice", enabled=False)
        self.client.post("/login", data=login_form(self.client, "owner", "Owner-secret-2026!"), follow_redirects=False)
        import re
        token = re.search(r'name="csrf_token" value="([^"]+)"', self.client.get("/settings").text).group(1)
        response = self.client.post("/admin/users/alice/revoke-passkeys", data={"csrf_token": token}, follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertEqual([], self.store.list_keys(self.identity))

    def test_enrolment_requires_password_and_csrf(self):
        self.client.post("/login", data=login_form(self.client, "alice", "First-secret-2026!"))
        self.assertEqual(403, self.client.post("/account/passkeys/options").status_code)
        import re
        token = re.search(r'name="csrf_token" value="([^"]+)"', self.client.get("/settings").text).group(1)
        self.assertEqual(403, self.client.post("/account/passkeys/options", data={"csrf_token": token, "current_password": "wrong"}).status_code)

    def test_login_challenge_issuance_is_rate_limited(self):
        self.register()
        for _ in range(5):
            self.assertEqual(200, self.client.post("/auth/passkeys/options", data={"username": "alice"}).status_code)
        self.assertEqual(429, self.client.post("/auth/passkeys/options", data={"username": "alice"}).status_code)


if __name__ == "__main__":
    unittest.main()
