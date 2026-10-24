import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from webui import app as app_module
from test_friend_identity import isolate_account_data, stored_accounts


ACCOUNT_ID = "group-bind-account"


class WebGroupBindingTests(unittest.TestCase):
    def setUp(self):
        self.account = {
            "account_ref": "group-bind-ref",
            "unique_id": ACCOUNT_ID,
            "username": "Tester",
            "enabled": True,
            "cookies": [{"name": "sessionid", "value": "v"}],
            "targets": ["Friend", "群聊：手动群"],
            "friends_cache": ["Friend"],
            "target_uid_bindings": {"Friend": {"uid": "123", "source": "web"}},
            "group_cache": [
                {"name": "同名", "conversationId": "1000000001"},
                {"name": "同名", "conversationId": "1000000002"},
                {"name": "普通群", "conversationId": "1000000003"},
            ],
        }
        self.path = isolate_account_data(self, [self.account])
        self.client = TestClient(app_module.app)
        self.principal = {"username": "admin", "role": "admin", "account_refs": [], "session_id": "s"}

    def _save(self, group_bind=(), group_targets=(), targets=("Friend",)):
        data = {
            "csrf_token": "t",
            "username": "Tester",
            "enabled": "on",
            "targets": list(targets),
            "group_bind": list(group_bind),
            "group_targets": list(group_targets),
        }
        with (
            patch.object(app_module, "current_user", return_value="admin"),
            patch.object(app_module, "current_principal", return_value=self.principal),
            patch.object(app_module, "validate_csrf", return_value=True),
        ):
            return self.client.post(f"/accounts/{ACCOUNT_ID}/update", data=data, follow_redirects=False)

    def test_same_name_bindings_are_deterministic_and_stable_across_resaves(self):
        self.assertEqual(303, self._save(["1000000002", "1000000001"]).status_code)
        first = stored_accounts(self.path)[0]
        self.assertEqual(
            {"群聊：同名#0001": "1000000001", "群聊：同名#0002": "1000000002"},
            first["group_target_bindings"],
        )
        self.assertEqual(303, self._save(["1000000001", "1000000002"]).status_code)
        second = stored_accounts(self.path)[0]
        self.assertEqual(first["group_target_bindings"], second["group_target_bindings"])
        self.assertEqual(first["targets"], second["targets"])

    def test_resave_keeps_bound_target_when_cached_name_changes(self):
        self.assertEqual(303, self._save(["1000000003"]).status_code)
        account = stored_accounts(self.path)[0]
        account["group_cache"] = [{"name": "改名后", "conversationId": "1000000003"}]
        from utils.config import update_user_data
        update_user_data(lambda accounts: (accounts.__setitem__(0, account), True)[1], force_reload=True)

        self.assertEqual(303, self._save(["1000000003"]).status_code)
        saved = stored_accounts(self.path)[0]
        self.assertEqual({"群聊：普通群": "1000000003"}, saved["group_target_bindings"])
        self.assertIn("群聊：普通群", saved["targets"])

    def test_unchecking_removes_bound_target_without_touching_friend_binding(self):
        self._save(["1000000003"])
        self._save([])
        saved = stored_accounts(self.path)[0]
        self.assertNotIn("群聊：普通群", saved["targets"])
        self.assertEqual({}, saved["group_target_bindings"])
        self.assertEqual("123", saved["target_uid_bindings"]["Friend"]["uid"])

    def test_manual_entry_equal_to_bound_target_is_not_saved_twice(self):
        self._save(["1000000003"], ["普通群", "手动群"])
        saved = stored_accounts(self.path)[0]
        self.assertEqual(1, saved["targets"].count("群聊：普通群"))
        self.assertIn("群聊：手动群", saved["targets"])

    def test_unknown_cached_id_is_ignored_and_manual_legacy_name_is_saved(self):
        self._save(["not-cached"], ["群聊:历史手动群"])
        saved = stored_accounts(self.path)[0]
        self.assertEqual({}, saved["group_target_bindings"])
        self.assertIn("群聊：历史手动群", saved["targets"])
