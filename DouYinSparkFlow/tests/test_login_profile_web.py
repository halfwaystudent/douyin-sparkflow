"""WebUI preserves an existing account's browser profile on re-login."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_shared_security as fixtures
from utils import config
from webui import app as app_module, login_lock


class ExistingAccountProfileWebTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        lock_patch = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
        lock_patch.start()
        self.addCleanup(lock_patch.stop)
        self.client = fixture.login()
        self.csrf = fixture.token(self.client)
        continuity = patch.dict(os.environ, {"SPARKFLOW_LOGIN_PROFILE_CONTINUITY": "1"})
        continuity.start()
        self.addCleanup(continuity.stop)
        accounts = config.get_userData(force_reload=True)
        accounts[0]["unique_id"] = " 111 "  # prove the server, not the form, normalizes it
        accounts[0]["login_generation"] = "generation-a"
        accounts[0]["enabled"] = True
        second = next(row for row in accounts if row["account_ref"] == "a2")
        second["login_generation"] = "generation-b"
        config.save_userData(accounts)

    def open_relogin(self, **fields):
        return self.client.post(
            "/login-desktop/open",
            data={
                "csrf_token": self.csrf,
                "mode": "relogin",
                "relogin_unique_id": "111",
                "relogin_account_ref": "a1",
                "login_generation": "generation-a",
                **fields,
            },
        )

    def test_relogin_opens_the_bound_account_profile_and_returns_desktop_state(self):
        desktop = {
            "ok": True,
            "profile_mode": "account",
            "reusable_session": True,
            "state": "logged_in",
            "logged_in": True,
        }
        with patch.object(app_module, "call_login_desktop", return_value=desktop) as call:
            response = self.open_relogin()

        self.assertEqual(200, response.status_code, response.text)
        ticket = response.json()["workspace"]["ticket"]
        call.assert_called_once_with(
            "/open-login",
            method="POST",
            payload={
                "interaction_key": ticket,
                "account_profile": {
                    "unique_id": "111",
                    "account_ref": "a1",
                    "login_generation": "generation-a",
                },
            },
            timeout=90,
        )
        self.assertEqual("account", response.json()["desktop"]["profile_mode"])
        self.assertTrue(response.json()["desktop"]["reusable_session"])
        self.assertTrue(response.json()["desktop"]["logged_in"])


    def test_rollout_flag_off_keeps_relogin_on_the_staging_contract(self):
        with patch.dict(os.environ, {"SPARKFLOW_LOGIN_PROFILE_CONTINUITY": ""}), patch.object(
            app_module, "call_login_desktop", return_value={"ok": True, "profile_mode": "staging"}
        ) as call:
            response = self.open_relogin()

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            {"interaction_key": response.json()["workspace"]["ticket"]},
            call.call_args.kwargs["payload"],
        )
        self.assertEqual("staging", response.json()["desktop"]["profile_mode"])

    def test_relogin_releases_the_lease_when_desktop_ignores_account_profile(self):
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True, "profile_mode": "staging"}):
            response = self.open_relogin()

        self.assertEqual(503, response.status_code)
        self.assertIn("账号浏览器环境", response.json()["error"])
        self.assertIsNone(login_lock.get_lock())


    def test_queued_relogin_is_revalidated_before_promotion(self):
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True, "profile_mode": "staging"}):
            active = self.client.post("/login-desktop/open", data={"csrf_token": self.csrf, "mode": "add"})
        self.assertEqual(200, active.status_code)
        bob = self.fixture.login("bob", "Second-secret-2026!")
        bob_csrf = self.fixture.token(bob)
        queued = bob.post(
            "/login-desktop/open",
            data={
                "csrf_token": bob_csrf,
                "mode": "relogin",
                "relogin_unique_id": "222",
                "relogin_account_ref": "a2",
                "login_generation": "generation-b",
            },
        )
        self.assertEqual(202, queued.status_code, queued.text)
        accounts = config.get_userData(force_reload=True)
        # A queued lease is stale if its owner is disabled. Paused sending
        # accounts remain eligible for a relogin; disabled console users do not.
        from webui import users
        users.update_web_user("bob", enabled=False)
        self.assertFalse(next(user for user in users.get_web_users() if user["username"] == "bob")["enabled"])
        queued_lease = next(row for row in login_lock.get_workspace_state()["queue"] if row["account_ref"] == "a2")
        self.assertEqual("generation-b", queued_lease["login_generation"])

        with patch.object(app_module, "call_login_desktop", return_value={"ok": True}) as desktop:
            closed = self.client.post(
                "/login-desktop/release",
                data={"csrf_token": self.csrf, "ticket": active.json()["workspace"]["ticket"]},
            )

        self.assertEqual(200, closed.status_code)
        self.assertEqual(["/close", "/close"], [call.args[0] for call in desktop.call_args_list])
        self.assertIsNone(login_lock.get_lock())

    def test_disabled_account_can_open_its_bound_profile_for_relogin(self):
        accounts = config.get_userData(force_reload=True)
        account = next(row for row in accounts if row["account_ref"] == "a1")
        account["enabled"] = False
        config.save_userData(accounts)

        with patch.object(app_module, "call_login_desktop", return_value={"ok": True, "profile_mode": "account"}) as desktop:
            response = self.open_relogin()

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("account", response.json()["desktop"]["profile_mode"])
        self.assertEqual("/open-login", desktop.call_args.args[0])

    def test_rejected_queued_profile_open_closes_the_failed_lease_without_promoting_another(self):
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True, "profile_mode": "staging"}):
            active = self.client.post("/login-desktop/open", data={"csrf_token": self.csrf, "mode": "add"})
        self.assertEqual(200, active.status_code)
        bob = self.fixture.login("bob", "Second-secret-2026!")
        bob_csrf = self.fixture.token(bob)
        queued = bob.post(
            "/login-desktop/open",
            data={
                "csrf_token": bob_csrf,
                "mode": "relogin",
                "relogin_unique_id": "222",
                "relogin_account_ref": "a2",
                "login_generation": "generation-b",
            },
        )
        self.assertEqual(202, queued.status_code, queued.text)

        calls = []
        def desktop(path, **_kwargs):
            calls.append(path)
            return {"ok": True, "profile_mode": "staging"}

        with patch.object(app_module, "call_login_desktop", side_effect=desktop):
            released = self.client.post(
                "/login-desktop/release",
                data={"csrf_token": self.csrf, "ticket": active.json()["workspace"]["ticket"]},
            )

        self.assertEqual(200, released.status_code, released.text)
        self.assertEqual(["/close", "/open-login", "/close"], calls)
        state = login_lock.get_workspace_state()
        self.assertEqual("resetting", state["phase"])
        self.assertIsNone(state["active"])
        self.assertEqual([], state["queue"])

    def test_workspace_script_only_advertises_reuse_after_account_mode_and_does_not_auto_refresh_logged_in(self):
        script = (Path(app_module.STATIC_DIR) / "login-workspace.js").read_text(encoding="utf-8")
        self.assertIn("desktopState.profile_mode === \"account\" && desktopState.reusable_session === true", script)
        self.assertIn("沿用账号浏览器环境，可保存登录态。", script)
        self.assertIn('label.textContent = "重新扫码"', script)
        qr = script[script.index("const refreshLoginQr"):script.index("const scheduleStatus")]
        self.assertIn("data.logged_in || data.state === \"qr_logged_in\" || data.state === \"logged_in\"", qr)
        self.assertIn("setQrButtons(\"刷新二维码\");", qr)
        self.assertNotIn("已重置，正在生成新的二维码", qr)


if __name__ == "__main__":
    unittest.main()
