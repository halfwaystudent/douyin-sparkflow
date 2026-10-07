"""Sub-account account ownership: create, adopt, refuse, and delete.

These tests drive the real routes so the three-way ownership decision
(mine / unassigned / someone else's) is exercised end to end against an
isolated account store and Web-user store.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from webui import app as app_module
from webui import users as users_module


def _account(unique_id, account_ref, username, **extra):
    base = {
        "unique_id": unique_id,
        "account_ref": account_ref,
        "username": username,
        "cookies": [{"name": "sessionid", "value": "v", "domain": ".douyin.com", "path": "/"}],
        "targets": ["阿杰"],
        "target_states": {"阿杰": {"done": True}},
        "enabled": True,
    }
    base.update(extra)
    return base


def _web_user(username, account_refs):
    return {
        "username": username,
        "role": "user",
        "password_hash": "pbkdf2_sha256$salt$digest",
        "enabled": True,
        "session_version": 1,
        "account_refs": list(account_refs),
    }


class AccountOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.accounts_path = root / "usersData.json"
        self.users_path = root / "webui_users.json"

        from utils import config as config_module

        env_patch = patch.object(
            config_module, "get_environment", return_value=config_module.Environment.LOCAL
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

        cached = config_module.userData
        config_module.userData = None
        self.addCleanup(lambda: setattr(config_module, "userData", cached))

        path_patch = patch.object(
            config_module, "users_data_path", return_value=self.accounts_path
        )
        path_patch.start()
        self.addCleanup(path_patch.stop)

        users_file_patch = patch.object(users_module, "USERS_FILE", self.users_path)
        users_file_patch.start()
        self.addCleanup(users_file_patch.stop)

        self.client = TestClient(app_module.app)

    # -- helpers ---------------------------------------------------------

    def _write_accounts(self, accounts):
        self.accounts_path.write_text(
            json.dumps(accounts, ensure_ascii=False), encoding="utf-8"
        )

    def _write_users(self, users):
        self.users_path.write_text(
            json.dumps({"auth_epoch": 1, "users": users}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _accounts(self):
        return json.loads(self.accounts_path.read_text(encoding="utf-8"))

    def _users(self):
        return json.loads(self.users_path.read_text(encoding="utf-8"))["users"]

    def _refs(self, username):
        return next(u["account_refs"] for u in self._users() if u["username"] == username)

    def _principal(self, username, refs, role="user"):
        return {
            "username": username,
            "role": role,
            "account_refs": list(refs),
            "session_id": "s-1",
        }

    def _post_qr_save(self, username, principal, exported):
        active = {
            "mode": "add",
            "account_ref": "",
            "ticket": "t1",
            "username": username,
            "session_id": "s-1",
        }
        with (
            patch.object(app_module, "current_user", return_value=username),
            patch.object(app_module, "current_principal", return_value=principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(app_module, "get_login_lock", return_value=active),
            patch.object(app_module, "owns_login_lock", return_value=True),
            patch.object(
                app_module,
                "call_login_desktop",
                return_value={"ok": True, "result": exported},
            ),
            patch.object(app_module, "begin_login_release"),
            patch.object(app_module, "begin_login_expiration", return_value=None),
            patch.object(app_module, "get_workspace_state", return_value={}),
        ):
            return self.client.post("/login-desktop/save", data={"csrf_token": "t"})

    def _post_cookie_login(self, username, principal, unique_id):
        with (
            patch.object(app_module, "current_user", return_value=username),
            patch.object(app_module, "current_principal", return_value=principal),
            patch.object(app_module, "validate_csrf", return_value=True),
            patch.object(
                app_module,
                "verify_login_result",
                return_value=(True, "", {"unique_id": unique_id, "username": "主号"}, ""),
            ),
            patch.object(app_module, "call_login_desktop", return_value={"ok": True}),
        ):
            return self.client.post(
                "/accounts/cookies",
                data={"csrf_token": "t", "cookie_input": "sessionid=abc; sid_guard=def"},
            )

    def _delete(self, username, principal, unique_id):
        with (
            patch.object(app_module, "current_user", return_value=username),
            patch.object(app_module, "current_principal", return_value=principal),
            patch.object(app_module, "validate_csrf", return_value=True),
        ):
            return self.client.post(
                f"/accounts/{unique_id}/delete",
                data={"csrf_token": "t"},
                follow_redirects=False,
            )

    # -- A1: unassigned rows are claimable -------------------------------

    def test_qr_save_adopts_an_unassigned_account_in_place(self):
        self._write_accounts([_account("1001", "acc-1", "主号")])
        self._write_users([_web_user("zxb", [])])

        response = self._post_qr_save(
            "zxb",
            self._principal("zxb", []),
            {"unique_id": "1001", "username": "主号", "cookies": [{"name": "ttwid", "value": "v"}]},
        )

        self.assertEqual(200, response.status_code)
        accounts = self._accounts()
        self.assertEqual(1, len(accounts))
        self.assertEqual("acc-1", accounts[0]["account_ref"])
        self.assertEqual(["阿杰"], accounts[0]["targets"])
        self.assertEqual(["acc-1"], self._refs("zxb"))

    def test_cookie_login_adopts_an_unassigned_account(self):
        self._write_accounts([_account("1001", "acc-1", "主号")])
        self._write_users([_web_user("zxb", [])])

        response = self._post_cookie_login("zxb", self._principal("zxb", []), "1001")

        self.assertEqual(200, response.status_code)
        self.assertEqual(["acc-1"], self._refs("zxb"))
        self.assertEqual("acc-1", self._accounts()[0]["account_ref"])

    # -- A5: a brand-new Douyin account is created and bound --------------

    def test_qr_save_creates_and_binds_a_brand_new_account(self):
        self._write_accounts([])
        self._write_users([_web_user("zxb", [])])

        response = self._post_qr_save(
            "zxb",
            self._principal("zxb", []),
            {"unique_id": "2002", "username": "新号", "cookies": [{"name": "ttwid", "value": "v"}]},
        )

        self.assertEqual(200, response.status_code)
        accounts = self._accounts()
        self.assertEqual(1, len(accounts))
        self.assertEqual("2002", accounts[0]["unique_id"])
        self.assertEqual([accounts[0]["account_ref"]], self._refs("zxb"))
        # Admin's account list is the full store, so a sub-account's new row
        # shows up there without any extra plumbing.
        visible = users_module.get_visible_accounts(
            {"role": "admin", "account_refs": []}, accounts
        )
        self.assertEqual(["2002"], [item["unique_id"] for item in visible])

    # -- A6: another user's row is still protected ------------------------

    def test_qr_save_refuses_an_account_owned_by_another_user(self):
        self._write_accounts([_account("1001", "acc-1", "主号")])
        self._write_users([_web_user("zcf", ["acc-1"]), _web_user("zxb", [])])

        response = self._post_qr_save(
            "zxb",
            self._principal("zxb", []),
            {"unique_id": "1001", "username": "主号", "cookies": [{"name": "ttwid", "value": "v"}]},
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("已经绑定给其他用户", response.json()["error"])
        self.assertEqual(["acc-1"], self._refs("zcf"))
        self.assertEqual([], self._refs("zxb"))

    def test_cookie_login_refuses_an_account_owned_by_another_user(self):
        self._write_accounts([_account("1001", "acc-1", "主号")])
        self._write_users([_web_user("zcf", ["acc-1"]), _web_user("zxb", [])])

        response = self._post_cookie_login("zxb", self._principal("zxb", []), "1001")

        self.assertEqual(403, response.status_code)
        self.assertIn("已经绑定给其他用户", response.json()["error"])
        self.assertEqual(["acc-1"], self._refs("zcf"))
        self.assertEqual([], self._refs("zxb"))

    # -- A2/A4: owner-only delete with reference cleanup ------------------

    def test_owner_deletes_their_account_and_refs_are_cleaned(self):
        self._write_accounts(
            [_account("1001", "acc-1", "主号"), _account("1002", "acc-2", "小号")]
        )
        self._write_users([_web_user("zxb", ["acc-1"]), _web_user("zcf", ["acc-2"])])

        response = self._delete("zxb", self._principal("zxb", ["acc-1"]), "1001")

        self.assertEqual(303, response.status_code)
        self.assertEqual(["1002"], [item["unique_id"] for item in self._accounts()])
        self.assertEqual([], self._refs("zxb"))
        self.assertEqual(["acc-2"], self._refs("zcf"))

    def test_non_owner_cannot_delete_another_users_account(self):
        self._write_accounts(
            [_account("1001", "acc-1", "主号"), _account("1002", "acc-2", "小号")]
        )
        self._write_users([_web_user("zcf", ["acc-1"]), _web_user("zxb", ["acc-2"])])

        response = self._delete("zxb", self._principal("zxb", ["acc-2"]), "1001")

        self.assertEqual(403, response.status_code)
        self.assertEqual(
            ["1001", "1002"], [item["unique_id"] for item in self._accounts()]
        )
        self.assertEqual(["acc-1"], self._refs("zcf"))
        self.assertEqual(["acc-2"], self._refs("zxb"))

    def test_delete_with_duplicate_unique_id_only_removes_own_row(self):
        # Legacy duplicate rows: one belongs to zxb, the other to zcf. Deleting
        # must not take the other user's row or ownership with it.
        self._write_accounts(
            [_account("1001", "acc-b", "主号"), _account("1001", "acc-a", "主号")]
        )
        self._write_users([_web_user("zxb", ["acc-b"]), _web_user("zcf", ["acc-a"])])

        response = self._delete("zxb", self._principal("zxb", ["acc-b"]), "1001")

        self.assertEqual(303, response.status_code)
        self.assertEqual(["acc-a"], [item["account_ref"] for item in self._accounts()])
        self.assertEqual([], self._refs("zxb"))
        self.assertEqual(["acc-a"], self._refs("zcf"))

    def test_claim_with_duplicate_unique_id_refuses_when_another_row_is_owned(self):
        # The unassigned row sorts first, so a naive first-row check would claim
        # it and dedupe zcf's row away.
        self._write_accounts(
            [_account("1001", "acc-free", "主号"), _account("1001", "acc-a", "主号")]
        )
        self._write_users([_web_user("zxb", []), _web_user("zcf", ["acc-a"])])

        response = self._post_qr_save(
            "zxb",
            self._principal("zxb", []),
            {"unique_id": "1001", "username": "主号", "cookies": [{"name": "ttwid", "value": "v"}]},
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("已经绑定给其他用户", response.json()["error"])
        self.assertEqual(
            ["acc-free", "acc-a"], [item["account_ref"] for item in self._accounts()]
        )
        self.assertEqual([], self._refs("zxb"))
        self.assertEqual(["acc-a"], self._refs("zcf"))

    def test_save_prefers_the_users_own_row_over_an_unassigned_duplicate(self):
        # The unassigned duplicate sorts first; a naive pick would update it,
        # dedupe the user's own row away, then fail validating the stale ref.
        self._write_accounts(
            [_account("1001", "acc-free", "主号"), _account("1001", "acc-u", "主号")]
        )
        self._write_users([_web_user("zxb", ["acc-u"])])

        response = self._post_qr_save(
            "zxb",
            self._principal("zxb", ["acc-u"]),
            {"unique_id": "1001", "username": "主号", "cookies": [{"name": "ttwid", "value": "v"}]},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            ["acc-u"], [item["account_ref"] for item in self._accounts()]
        )
        self.assertEqual(["acc-u"], self._refs("zxb"))

    # -- A3: delete, then rebind the same Douyin account ------------------

    def test_delete_then_rebind_the_same_douyin_account_succeeds(self):
        self._write_accounts([_account("1001", "acc-1", "主号")])
        self._write_users([_web_user("zxb", ["acc-1"])])

        deleted = self._delete("zxb", self._principal("zxb", ["acc-1"]), "1001")
        self.assertEqual(303, deleted.status_code)
        self.assertEqual([], self._accounts())

        rebound = self._post_qr_save(
            "zxb",
            self._principal("zxb", []),
            {"unique_id": "1001", "username": "主号", "cookies": [{"name": "ttwid", "value": "v"}]},
        )

        self.assertEqual(200, rebound.status_code)
        accounts = self._accounts()
        self.assertEqual(1, len(accounts))
        self.assertEqual("1001", accounts[0]["unique_id"])
        self.assertNotEqual("acc-1", accounts[0]["account_ref"])
        self.assertEqual([accounts[0]["account_ref"]], self._refs("zxb"))


if __name__ == "__main__":
    unittest.main()
