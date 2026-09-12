import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import login_lock, users


class MultiUserTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.users_path = Path(self.temp_dir.name) / "webui_users.json"
        self.lock_path = Path(self.temp_dir.name) / "login-workspace.lock.json"
        self.accounts = [
            {"account_ref": "acc-1", "username": "头像是本人", "unique_id": "111", "targets": [], "enabled": True},
            {"account_ref": "acc-2", "username": "你成功捕捉一只野生妖孽", "unique_id": "222", "targets": [], "enabled": True},
            {"account_ref": "acc-3", "username": "管理员账号", "unique_id": "333", "targets": [], "enabled": True},
        ]
        self.user_file_patch = patch.object(users, "USERS_FILE", self.users_path)
        self.user_file_patch.start()
        self.ensure_patch = patch.object(users, "get_userData", return_value=self.accounts)
        self.ensure_patch.start()
        self.save_accounts_patch = patch.object(users, "save_userData")
        self.save_accounts_patch.start()
        self.addCleanup(self.ensure_patch.stop)
        self.addCleanup(self.save_accounts_patch.stop)
        self.addCleanup(self.user_file_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def test_user_creation_auth_and_unique_assignment(self):
        a, changed = users.ensure_account_refs(self.accounts)
        self.assertFalse(changed)
        ref = a[0]["account_ref"]
        created = users.create_web_user("zxb", "zxb123456", account_refs=[ref])
        self.assertEqual([ref], created["account_refs"])
        identity = users.authenticate("zxb", "zxb123456")
        self.assertEqual("user", identity["role"])
        self.assertEqual([ref], identity["account_refs"])
        self.assertIsNone(users.authenticate("zxb", "wrong"))
        with self.assertRaises(users.UserStoreError):
            users.create_web_user("zcf", "zcf123456", account_refs=[ref])

    def test_visible_accounts_and_admin_reassignment(self):
        accounts, _ = users.ensure_account_refs(self.accounts)
        first_ref = accounts[0]["account_ref"]
        second_ref = accounts[1]["account_ref"]
        users.create_web_user("zxb", "secret", account_refs=[first_ref])
        principal = {"role": "user", "account_refs": [first_ref]}
        self.assertEqual([first_ref], [a["account_ref"] for a in users.get_visible_accounts(principal, accounts)])
        users.update_web_user("zxb", account_refs=[second_ref])
        self.assertEqual([second_ref], users.find_web_user("zxb")["account_refs"])
        self.assertTrue(users.delete_web_user("zxb"))
        self.assertEqual([], users.get_web_users())

    def test_fifo_queue_promotes_after_active_release(self):
        with patch.object(login_lock, "LOCK_PATH", self.lock_path):
            first = login_lock.request_workspace(username="zxb", session_id="s1", account_ref="a1", mode="relogin")
            second = login_lock.request_workspace(username="zcf", session_id="s2", account_ref="", mode="add")
            self.assertEqual("active", first["state"])
            self.assertEqual("queued", second["state"])
            self.assertEqual("add", second["request"]["mode"])
            self.assertEqual(1, second["position"])
            self.assertEqual("queued", login_lock.workspace_status(username="zcf", session_id="s2")["state"])
            released = login_lock.begin_release(username="zxb", session_id="s1", ticket=first["request"]["ticket"], account_ref="a1")
            self.assertIsNotNone(released)
            promoted = login_lock.finish_transition()
            self.assertEqual("zcf", promoted["username"])
            self.assertEqual("active", login_lock.workspace_status(username="zcf", session_id="s2")["state"])

    def test_login_workspace_is_serialized_and_expires(self):
        with patch.object(login_lock, "LOCK_PATH", self.lock_path), patch.object(login_lock, "LOCK_TTL_SECONDS", 1):
            ok, lock = login_lock.acquire(username="zxb", session_id="s1", account_ref="a1")
            self.assertTrue(ok)
            self.assertTrue(login_lock.owns(lock, username="zxb", session_id="s1", account_ref="a1"))
            blocked, current = login_lock.acquire(username="zcf", session_id="s2", account_ref="a2")
            self.assertFalse(blocked)
            self.assertEqual("zxb", current["username"])
            self.assertTrue(login_lock.refresh(username="zxb", session_id="s1", account_ref="a1"))
            self.assertTrue(login_lock.release(username="zxb", session_id="s1"))
            self.assertIsNone(login_lock.get_lock())


if __name__ == "__main__":
    unittest.main()
