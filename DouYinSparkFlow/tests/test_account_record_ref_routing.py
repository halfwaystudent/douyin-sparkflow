"""Account mutations must resolve the immutable record reference, not a UID."""

from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
import tempfile

from core import friends
from tests import test_shared_security as fixtures
from utils import config
from webui import app as app_module, login_lock, users


class AccountRecordRefRoutingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.rows = [
            {
                "account_ref": "a1",
                "unique_id": "111",
                "username": "first foreign record",
                "cookies": [],
                "targets": ["first target"],
                "enabled": True,
                "login_generation": "gen-a1",
            },
            {
                "account_ref": "a3",
                "unique_id": "111",
                "username": "second owned record",
                "cookies": [],
                "targets": ["second target"],
                "enabled": True,
                "friends_cache": ["old second cache"],
                "login_generation": "gen-a3",
            },
        ]
        config.save_userData(deepcopy(self.rows))
        people = users.get_web_users()
        next(item for item in people if item["username"] == "alice")["account_refs"] = ["a3"]
        next(item for item in people if item["username"] == "bob")["account_refs"] = ["a1"]
        users.save_web_users(people)
        self.alice = self.fixture.login()
        self.token = self.fixture.token(self.alice)

    def stored(self):
        return {row["account_ref"]: row for row in config.get_userData(force_reload=True)}

    def form(self, **extra):
        return {
            "csrf_token": self.token,
            "account_ref": "a3",
            "login_generation": "gen-a3",
            **extra,
        }

    def test_ref_targeted_toggle_operates_only_the_owned_second_duplicate(self):
        response = self.alice.post("/accounts/111/toggle-enabled", data=self.form(), follow_redirects=False)
        self.assertEqual(303, response.status_code, response.text)
        self.assertTrue(self.stored()["a1"]["enabled"])
        self.assertFalse(self.stored()["a3"]["enabled"])

    def test_explicit_ref_mutation_requires_a_present_generation(self):
        response = self.alice.post(
            "/accounts/111/toggle-enabled",
            data={"csrf_token": self.token, "account_ref": "a3"},
            follow_redirects=False,
        )
        self.assertEqual(409, response.status_code, response.text)
        self.assertTrue(self.stored()["a3"]["enabled"])

    def test_explicit_ref_accepts_an_empty_generation_only_when_it_is_present_and_current(self):
        rows = self.stored()
        rows["a3"]["login_generation"] = ""
        config.save_userData(list(rows.values()))
        response = self.alice.post(
            "/accounts/111/toggle-enabled",
            data={"csrf_token": self.token, "account_ref": "a3", "login_generation": ""},
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code, response.text)
        self.assertFalse(self.stored()["a3"]["enabled"])

    def test_foreign_ref_is_forbidden_even_when_uid_is_shared(self):
        response = self.alice.post(
            "/accounts/111/toggle-enabled",
            data=self.form(account_ref="a1", login_generation="gen-a1"),
            follow_redirects=False,
        )
        self.assertEqual(403, response.status_code)
        self.assertTrue(self.stored()["a1"]["enabled"])
        self.assertTrue(self.stored()["a3"]["enabled"])

    def test_stale_generation_is_rejected_before_mutation(self):
        response = self.alice.post(
            "/accounts/111/toggle-enabled",
            data=self.form(login_generation="old-generation"),
            follow_redirects=False,
        )
        self.assertEqual(409, response.status_code)
        self.assertTrue(self.stored()["a3"]["enabled"])

    def test_ref_targeted_delete_does_not_remove_first_or_its_ownership(self):
        response = self.alice.post("/accounts/111/delete", data=self.form(), follow_redirects=False)
        self.assertEqual(303, response.status_code, response.text)
        self.assertEqual(["a1"], [row["account_ref"] for row in config.get_userData(force_reload=True)])
        self.assertEqual(["a1"], users.find_web_user("bob")["account_refs"])
        self.assertEqual([], users.find_web_user("alice")["account_refs"])

    def test_legacy_uid_request_is_allowed_only_when_the_visible_match_is_unique(self):
        response = self.alice.post(
            "/accounts/111/toggle-enabled", data={"csrf_token": self.token}, follow_redirects=False
        )
        self.assertEqual(303, response.status_code, response.text)
        self.assertFalse(self.stored()["a3"]["enabled"])

    def test_legacy_uid_request_is_rejected_when_an_admin_can_see_duplicate_matches(self):
        admin = self.fixture.login("owner", "Owner-secret-2026!")
        response = admin.post(
            "/accounts/111/toggle-enabled",
            data={"csrf_token": self.fixture.token(admin)},
            follow_redirects=False,
        )
        self.assertEqual(409, response.status_code)
        self.assertTrue(self.stored()["a1"]["enabled"])
        self.assertTrue(self.stored()["a3"]["enabled"])

    def test_async_refreshes_persist_only_the_selected_second_duplicate(self):
        friend_tasks = []
        async def fetch_friends(_account, **_kwargs):
            return friends.FriendScanResult(["second fresh friend"], complete=True)

        with patch.object(app_module.asyncio, "create_task", side_effect=friend_tasks.append):
            friend_start = self.alice.post(
                "/accounts/111/friends/refresh/async?ref=a3&generation=gen-a3",
                data={"csrf_token": self.token},
            )
        self.assertEqual(202, friend_start.status_code, friend_start.text)
        asyncio_run = __import__("asyncio").run
        with patch.object(app_module, "fetch_account_friends", side_effect=fetch_friends):
            asyncio_run(friend_tasks.pop())

        group_tasks = []
        async def fetch_groups(_account):
            return [{"name": "second fresh group", "conversationId": "2000000002"}]

        with patch.object(app_module, "_start_background", side_effect=group_tasks.append):
            group_start = self.alice.post(
                "/accounts/111/groups/refresh/async?ref=a3&generation=gen-a3",
                data={"csrf_token": self.token},
            )
        self.assertEqual(202, group_start.status_code, group_start.text)
        with patch.object(app_module, "fetch_account_groups", side_effect=fetch_groups):
            asyncio_run(group_tasks.pop())

        rows = self.stored()
        self.assertEqual([], rows["a1"].get("friends_cache", []))
        self.assertEqual([], rows["a1"].get("group_cache", []))
        self.assertEqual(["second fresh friend"], rows["a3"]["friends_cache"])
        self.assertEqual("second fresh group", rows["a3"]["group_cache"][0]["name"])

    def test_patrol_status_isolated_to_the_selected_duplicate_record(self):
        admin = self.fixture.login("owner", "Owner-secret-2026!")
        token = self.fixture.token(admin)
        captured = []

        async def run_patrol(_refs, **_kwargs):
            return {"accounts": [{"conclusion": "ok", "reason": "second only"}]}

        with patch.object(app_module, "_start_background", side_effect=captured.append):
            response = admin.post(
                "/accounts/111/patrol?ref=a3&generation=gen-a3",
                data={"csrf_token": token},
            )
        self.assertEqual(202, response.status_code, response.text)
        with patch.object(app_module.account_patrol, "run_patrol", side_effect=run_patrol):
            __import__("asyncio").run(captured.pop())

        first = admin.get("/accounts/111/patrol/status?ref=a1&generation=gen-a1")
        second = admin.get("/accounts/111/patrol/status?ref=a3&generation=gen-a3")
        self.assertEqual("idle", first.json()["state"])
        self.assertEqual("done", second.json()["state"])
        self.assertEqual("second only", second.json()["reason"])

    def test_merge_uses_exact_source_and_target_refs_and_rejects_stale_generation(self):
        admin = self.fixture.login("owner", "Owner-secret-2026!")
        token = self.fixture.token(admin)
        rows = self.stored()
        rows["a1"]["username"] = "same name"
        rows["a3"]["username"] = "same name"
        config.save_userData(list(rows.values()))

        stale = admin.post(
            "/accounts/111/merge-into?ref=a3&generation=old",
            data={"csrf_token": token, "account_ref": "a3", "login_generation": "old", "target_account_ref": "a1", "target_login_generation": "gen-a1"},
            follow_redirects=False,
        )
        self.assertEqual(409, stale.status_code)
        self.assertEqual({"a1", "a3"}, set(self.stored()))

        stale_target = admin.post(
            "/accounts/111/merge-into?ref=a3&generation=gen-a3",
            data={"csrf_token": token, "account_ref": "a3", "login_generation": "gen-a3", "target_account_ref": "a1", "target_login_generation": "old"},
            follow_redirects=False,
        )
        self.assertEqual(409, stale_target.status_code)
        self.assertEqual({"a1", "a3"}, set(self.stored()))

        response = admin.post(
            "/accounts/111/merge-into?ref=a3&generation=gen-a3",
            data={"csrf_token": token, "account_ref": "a3", "login_generation": "gen-a3", "target_account_ref": "a1", "target_login_generation": "gen-a1"},
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code, response.text)
        self.assertEqual(["a1"], list(self.stored()))
        self.assertIn("second target", self.stored()["a1"]["targets"])

    def test_login_workspace_rejects_unknown_ref_and_ambiguous_legacy_uid(self):
        unknown = self.alice.get("/login-workspace?relogin_ref=missing")
        self.assertEqual(404, unknown.status_code)
        self.assertNotIn("添加新账号", unknown.text)

        admin = self.fixture.login("owner", "Owner-secret-2026!")
        ambiguous = admin.get("/login-workspace?relogin=111")
        self.assertEqual(409, ambiguous.status_code)
        self.assertNotIn("添加新账号", ambiguous.text)

    def test_cookie_relogin_requires_generation_for_an_explicit_ref(self):
        client = self.fixture.login("owner", "Owner-secret-2026!")
        token = self.fixture.token(client)
        identity = {
            "unique_id": "111",
            "display_id": "second_handle",
            "username": "second owned record",
            "trusted": {"uid": "111"},
        }
        with (
            patch.object(app_module, "verify_login_result", new=AsyncMock(return_value=(True, "", identity, ""))) as verify,
            patch.object(app_module, "save_exported_login_result", return_value=(self.stored()["a3"], "updated")),
            patch.object(app_module, "commit_login_binding", side_effect=lambda save_login, **_kwargs: save_login()),
        ):
            response = client.post(
                "/accounts/cookies",
                data={
                    "csrf_token": token,
                    "cookie_input": "sessionid=synthetic; sid_guard=synthetic",
                    "relogin_unique_id": "111",
                    "relogin_account_ref": "a3",
                },
            )
        self.assertEqual(409, response.status_code, response.text)
        verify.assert_not_awaited()

    def test_cookie_relogin_uses_exact_second_ref_and_rejects_stale_generation(self):
        client = self.fixture.login("owner", "Owner-secret-2026!")
        token = self.fixture.token(client)
        cookie = "sessionid=synthetic; sid_guard=synthetic"
        stale = client.post(
            "/accounts/cookies",
            data={
                "csrf_token": token,
                "cookie_input": cookie,
                "relogin_unique_id": "111",
                "relogin_account_ref": "a3",
                "login_generation": "stale",
            },
        )
        self.assertEqual(409, stale.status_code, stale.text)

        captured = {}
        identity = {
            "unique_id": "111",
            "display_id": "second_handle",
            "username": "second owned record",
            "trusted": {"uid": "111"},
        }

        def saved(_result, **kwargs):
            captured.update(kwargs)
            return self.stored()["a3"], "updated"

        def commit(save_login, **_kwargs):
            return save_login()

        with (
            patch.object(app_module, "verify_login_result", new=AsyncMock(return_value=(True, "", identity, ""))),
            patch.object(app_module, "legacy_identity_check", return_value={"account_ref": "a3", "uid": "111"}),
            patch.object(app_module, "save_exported_login_result", side_effect=saved),
            patch.object(app_module, "commit_login_binding", side_effect=commit),
        ):
            response = client.post(
                "/accounts/cookies",
                data={
                    "csrf_token": token,
                    "cookie_input": cookie,
                    "relogin_unique_id": "111",
                    "relogin_account_ref": "a3",
                    "login_generation": "gen-a3",
                },
            )
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("a3", captured["relogin_account_ref"])
        self.assertEqual("a3", response.json()["account"]["account_ref"])

    def test_workspace_open_requires_generation_for_an_explicit_ref(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        lock_path = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
        lock_path.start()
        self.addCleanup(lock_path.stop)
        client = self.fixture.login("owner", "Owner-secret-2026!")
        with patch.object(app_module, "call_login_desktop") as desktop:
            response = client.post(
                "/login-desktop/open",
                data={
                    "csrf_token": self.fixture.token(client),
                    "mode": "relogin",
                    "relogin_unique_id": "111",
                    "relogin_account_ref": "a3",
                },
            )
        self.assertEqual(409, response.status_code, response.text)
        desktop.assert_not_called()
        self.assertIsNone(login_lock.get_lock())

    def test_workspace_lease_rejects_a_generation_replaced_after_open_without_exporting(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        lock_path = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
        lock_path.start()
        self.addCleanup(lock_path.stop)
        client = self.fixture.login("owner", "Owner-secret-2026!")
        token = self.fixture.token(client)
        form = {
            "csrf_token": token,
            "mode": "relogin",
            "relogin_unique_id": "111",
            "relogin_account_ref": "a3",
            "login_generation": "gen-a3",
        }
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True}):
            opened = client.post("/login-desktop/open", data=form)
        self.assertEqual(200, opened.status_code, opened.text)
        ticket = opened.json()["workspace"]["ticket"]
        self.assertEqual("gen-a3", login_lock.get_lock()["login_generation"])
        rows = self.stored()
        rows["a3"]["login_generation"] = "gen-new"
        config.save_userData(list(rows.values()))

        with patch.object(app_module, "call_login_desktop", side_effect=RuntimeError("must not export")) as export:
            response = client.post(
                "/login-desktop/save",
                data={"csrf_token": token, "ticket": ticket, "relogin_account_ref": "a3"},
            )
        self.assertEqual(409, response.status_code, response.text)
        export.assert_not_called()
        self.assertEqual("gen-a3", login_lock.get_lock()["login_generation"])

    def test_unique_legacy_uid_relogin_captures_a_lease_generation_and_saves(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        lock_path = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
        lock_path.start()
        self.addCleanup(lock_path.stop)
        rows = self.stored()
        rows["a1"]["unique_id"] = "222"
        config.save_userData(list(rows.values()))
        form = {"csrf_token": self.token, "mode": "relogin", "relogin_unique_id": "111"}
        exported = {
            "username": "second owned record",
            "cookies": [{"name": "sessionid", "value": "synthetic", "domain": ".douyin.com", "path": "/"}],
        }
        trusted = {"uid": "111", "source": "synthetic", "version": 1}
        def desktop(path, **_kwargs):
            if path == "/export":
                return {"ok": True, "result": exported}
            return {"ok": True}

        with patch.object(app_module, "call_login_desktop", side_effect=desktop), patch.object(app_module.account_identity, "fetch_trusted_identity", return_value=trusted), patch.object(app_module, "legacy_identity_check", return_value={"account_ref": "a3", "uid": "111"}), patch.object(app_module, "save_exported_login_result", return_value=(self.stored()["a3"], "updated")), patch.object(app_module, "commit_login_binding", side_effect=lambda save_login, **_kwargs: save_login()):
            opened = self.alice.post("/login-desktop/open", data=form)
            self.assertEqual(200, opened.status_code, opened.text)
            ticket = opened.json()["workspace"]["ticket"]
            self.assertEqual("a3", login_lock.get_lock()["account_ref"])
            self.assertEqual("gen-a3", login_lock.get_lock()["login_generation"])
            saved = self.alice.post("/login-desktop/save", data={"csrf_token": self.token, "ticket": ticket})
        self.assertEqual(200, saved.status_code, saved.text)
        self.assertEqual("a3", saved.json()["account"]["account_ref"])

    def test_workspace_open_and_save_keep_exact_second_ref_and_reject_stale_generation(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        lock_path = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
        lock_path.start()
        self.addCleanup(lock_path.stop)
        client = self.fixture.login("owner", "Owner-secret-2026!")
        token = self.fixture.token(client)
        form = {
            "csrf_token": token,
            "mode": "relogin",
            "relogin_unique_id": "111",
            "relogin_account_ref": "a3",
            "login_generation": "gen-a3",
        }
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True}):
            opened = client.post("/login-desktop/open", data=form)
        self.assertEqual(200, opened.status_code, opened.text)
        ticket = opened.json()["workspace"]["ticket"]
        self.assertEqual("a3", login_lock.get_lock()["account_ref"])

        stale = client.post(
            "/login-desktop/save",
            data={**form, "ticket": ticket, "login_generation": "stale"},
        )
        self.assertEqual(409, stale.status_code, stale.text)

        with patch.object(app_module, "call_login_desktop") as export:
            mismatched = client.post(
                "/login-desktop/save",
                data={**form, "ticket": ticket, "relogin_account_ref": "a1"},
            )
        self.assertEqual(409, mismatched.status_code, mismatched.text)
        export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
