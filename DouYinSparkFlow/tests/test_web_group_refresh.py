import asyncio
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from test_friend_identity import isolate_account_data, stored_accounts
from webui import app as app_module


ACCOUNT_ID = "group-refresh-account"


class WebGroupRefreshTests(unittest.TestCase):
    def setUp(self):
        self.account = {
            "account_ref": "group-refresh-ref", "unique_id": ACCOUNT_ID,
            "username": "Tester", "enabled": True, "cookies": [],
            "targets": ["群聊：同名#0001"],
            "group_cache": [{"name": "旧群", "conversationId": "1000000001"}],
            "group_cache_updated_at": "old",
            "group_target_bindings": {"群聊：同名#0001": "1000000001"},
        }
        self.path = isolate_account_data(self, [self.account])
        self.client = TestClient(app_module.app)
        self.principal = {"username": "admin", "role": "admin", "account_refs": [], "session_id": "s"}
        app_module._group_refresh_active.discard(ACCOUNT_ID)
        self.addCleanup(app_module._group_refresh_active.discard, ACCOUNT_ID)

    def _start(self, fetch):
        captured = []
        def start(coro):
            captured.append(coro)
            return None
        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(app_module, "fetch_account_groups", side_effect=fetch),
            patch.object(app_module, "_start_background", side_effect=start),
        ):
            response = self.client.post(f"/accounts/{ACCOUNT_ID}/groups/refresh/async", data={"csrf_token": "t"})
        self.assertEqual(202, response.status_code)
        with patch.object(app_module, "fetch_account_groups", side_effect=fetch):
            asyncio.run(captured[0])
        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "current_principal", return_value=self.principal),
        ):
            return self.client.get(f"/accounts/{ACCOUNT_ID}/groups/refresh/status").json()

    def test_refresh_treats_matching_disambiguated_bound_target_as_ok(self):
        async def fetch(_account):
            return [{"name": "同名", "conversationId": "1000000001"}]
        job = self._start(fetch)
        self.assertEqual("done", job["state"])
        self.assertEqual([{"target": "群聊：同名#0001", "status": "ok"}], job["targets"])
        self.assertEqual({}, stored_accounts(self.path)[0].get("group_target_notices"))

    def test_empty_refresh_keeps_existing_cache(self):
        async def fetch(_account):
            return []
        job = self._start(fetch)
        self.assertEqual("failed", job["state"])
        self.assertIn("未读到群列表", job["error"])
        saved = stored_accounts(self.path)[0]
        self.assertEqual(self.account["group_cache"], saved["group_cache"])
        self.assertEqual("old", saved["group_cache_updated_at"])


class WebGroupRefreshGuardTests(unittest.TestCase):
    """The refresh opens the only real account's browser: every guard must hold."""

    def setUp(self):
        self.account = {
            "account_ref": "guard-ref", "unique_id": ACCOUNT_ID, "username": "Tester",
            "enabled": True, "cookies": [], "targets": [],
            "group_cache": [{"name": "旧群", "conversationId": "1000000001"}],
            "group_cache_updated_at": "old",
        }
        self.path = isolate_account_data(self, [self.account])
        self.client = TestClient(app_module.app)
        self.principal = {"username": "admin", "role": "admin", "account_refs": [], "session_id": "s"}
        for registry in (app_module._group_refresh_active, app_module._friend_refresh_active, app_module._patrol_active):
            registry.discard(ACCOUNT_ID)
            self.addCleanup(registry.discard, ACCOUNT_ID)
        self.started = []
        self.login_lock = None

    def _post(self, *, logged_in=True, csrf_ok=True, running=False):
        patches = [
            patch.object(app_module, "validate_csrf", return_value=csrf_ok),
            patch.object(app_module, "task_run_lock_status", return_value={"running": running}),
            patch.object(app_module, "_start_background", side_effect=lambda coro: (self.started.append(coro), coro.close())),
            patch.object(app_module, "get_login_lock", return_value=self.login_lock),
        ]
        if logged_in:
            patches += [
                patch.object(app_module, "current_user", return_value="admin"),
                patch.object(app_module, "current_principal", return_value=self.principal),
            ]
        with patches[0], patches[1], patches[2], patches[3], (patches[4] if logged_in else patch.object(app_module, "current_user", return_value=None)), (patches[5] if logged_in else patch.object(app_module, "current_principal", return_value=None)):
            return self.client.post(f"/accounts/{ACCOUNT_ID}/groups/refresh/async", data={"csrf_token": "t"}, follow_redirects=False)

    def _assert_nothing_started(self):
        self.assertEqual([], self.started)
        saved = stored_accounts(self.path)[0]
        self.assertEqual("old", saved["group_cache_updated_at"])

    def test_requires_login(self):
        self.assertEqual(401, self._post(logged_in=False).status_code)
        self._assert_nothing_started()

    def test_rejects_bad_csrf(self):
        self.assertEqual(403, self._post(csrf_ok=False).status_code)
        self._assert_nothing_started()

    def test_refuses_while_a_send_task_is_running(self):
        response = self._post(running=True)
        self.assertEqual(409, response.status_code)
        self.assertEqual("busy", response.json()["category"])
        self._assert_nothing_started()

    def test_refuses_while_friend_refresh_or_patrol_uses_the_account(self):
        for registry in (app_module._friend_refresh_active, app_module._patrol_active):
            registry.add(ACCOUNT_ID)
            try:
                self.assertEqual(409, self._post().status_code)
            finally:
                registry.discard(ACCOUNT_ID)
        self._assert_nothing_started()

    def test_second_refresh_is_refused_while_first_runs(self):
        self.assertEqual(202, self._post().status_code)
        self.assertEqual(409, self._post().status_code)
        self.assertEqual(1, len(self.started))

    def test_refuses_while_login_workspace_is_in_use(self):
        self.login_lock = {"owner": "someone"}
        self.assertEqual(409, self._post().status_code)
        self._assert_nothing_started()

    def test_friend_refresh_and_patrol_see_a_running_group_refresh(self):
        app_module._group_refresh_active.add(ACCOUNT_ID)
        with patch.object(app_module, "get_login_lock", return_value=None):
            self.assertIn("群列表", app_module.friend_refresh_conflict(ACCOUNT_ID))
