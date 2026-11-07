"""Regression contracts for login-workspace request identity."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import config
from webui import app as app_module, login_lock
from tests import test_shared_security as fixtures


class LoginWorkspaceContextConflictTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.temp_dir.name) / "login-workspace.lock.json"
        self.lock_patch = patch.object(login_lock, "LOCK_PATH", self.lock_path)
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def test_same_owner_different_active_context_conflicts_without_replacing_lease(self):
        active = login_lock.request_workspace(
            username="alice",
            session_id="session-1",
            account_ref="account-a",
            login_generation="generation-a",
            mode="relogin",
        )
        original_ticket = active["request"]["ticket"]

        result = login_lock.request_workspace(
            username="alice",
            session_id="session-1",
            account_ref="",
            login_generation="",
            mode="add",
        )

        self.assertEqual("conflict", result["state"])
        self.assertEqual("active", result["current_state"])
        self.assertEqual(original_ticket, result["request"]["ticket"])
        self.assertEqual("relogin", result["request"]["mode"])
        self.assertEqual("account-a", result["request"]["account_ref"])
        self.assertEqual("generation-a", result["request"]["login_generation"])
        self.assertEqual(original_ticket, login_lock.get_lock()["ticket"])

    def test_same_owner_different_queued_context_conflicts_without_replacing_request(self):
        login_lock.request_workspace(
            username="owner",
            session_id="owner-session",
            account_ref="account-owner",
            login_generation="owner-generation",
            mode="relogin",
        )
        queued = login_lock.request_workspace(
            username="alice",
            session_id="session-1",
            account_ref="account-a",
            login_generation="generation-a",
            mode="relogin",
        )
        ticket = queued["request"]["ticket"]

        result = login_lock.request_workspace(
            username="alice",
            session_id="session-1",
            account_ref="account-b",
            login_generation="generation-b",
            mode="relogin",
        )

        self.assertEqual("conflict", result["state"])
        self.assertEqual("queued", result["current_state"])
        self.assertEqual(ticket, result["request"]["ticket"])
        self.assertEqual("account-a", result["request"]["account_ref"])
        self.assertEqual(ticket, login_lock.get_workspace_state()["queue"][0]["ticket"])

    def test_identical_request_remains_idempotent(self):
        first = login_lock.request_workspace(
            username="alice",
            session_id="session-1",
            account_ref="account-a",
            login_generation="generation-a",
            mode="relogin",
        )
        repeated = login_lock.request_workspace(
            username="alice",
            session_id="session-1",
            account_ref="account-a",
            login_generation="generation-a",
            mode="relogin",
        )

        self.assertEqual("active", repeated["state"])
        self.assertEqual(first["request"]["ticket"], repeated["request"]["ticket"])


class LoginDesktopOpenConflictResponseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.lock_patch = patch.object(
            login_lock,
            "LOCK_PATH",
            Path(self.temp_dir.name) / "login-workspace.lock.json",
        )
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
        accounts = config.get_userData(force_reload=True)
        accounts[0]["login_generation"] = "generation-a"
        config.save_userData(accounts)
        self.client = self.fixture.login()
        self.csrf = self.fixture.token(self.client)

    def test_open_returns_409_with_current_workspace_for_different_context(self):
        with patch.object(app_module, "call_login_desktop", return_value={}) as open_desktop:
            first = self.client.post(
                "/login-desktop/open",
                data={"csrf_token": self.csrf, "mode": "add"},
            )
            conflict = self.client.post(
                "/login-desktop/open",
                data={
                    "csrf_token": self.csrf,
                    "mode": "relogin",
                    "relogin_unique_id": "111",
                    "relogin_account_ref": "a1",
                    "login_generation": "generation-a",
                },
            )

        self.assertEqual(200, first.status_code)
        self.assertEqual(409, conflict.status_code)
        self.assertIn("当前工作区", conflict.json()["error"])
        self.assertEqual("active", conflict.json()["current_workspace"]["state"])
        self.assertEqual("add", conflict.json()["current_workspace"]["mode"])
        self.assertEqual(first.json()["workspace"]["ticket"], login_lock.get_lock()["ticket"])
        self.assertEqual(1, open_desktop.call_count)


if __name__ == "__main__":
    unittest.main()
