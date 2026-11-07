"""Regression coverage for the account-store/user-store lock hierarchy."""

from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

from filelock import FileLock

from tests import test_shared_security as fixtures
from utils import config
from webui import app as app_module, users
from webui.account_binding import commit_login_binding


class BindingDeleteLockOrderTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture

    def test_login_binding_and_delete_do_not_invert_store_locks(self):
        """A binding and delete complete when each starts with the other store."""
        binding_has_user_lock = threading.Event()
        delete_has_account_lock = threading.Event()
        allow_binding_save = threading.Event()

        def short_file_lock(path, timeout=-1, **kwargs):
            # The old inversion must fail promptly instead of leaving test
            # threads stuck for the production 30-second lock timeout.
            timeout = 0.15 if timeout is None or timeout < 0 else min(timeout, 0.15)
            return FileLock(path, timeout=timeout, **kwargs)

        def save_login():
            # commit_login_binding holds the user lock before invoking this.
            binding_has_user_lock.set()
            self.assertTrue(allow_binding_save.wait(1))

            def mutate(accounts):
                account = next(row for row in accounts if row["account_ref"] == "a1")
                account["username"] = "Login save"
                return dict(account)

            return config.update_user_data(mutate, force_reload=True), "updated"

        def delete_update(mutator, **kwargs):
            def under_account_lock(accounts):
                # The route invokes its mutator only after usersData.lock is held.
                delete_has_account_lock.set()
                self.assertTrue(binding_has_user_lock.wait(1))
                return mutator(accounts)

            return config.update_user_data(under_account_lock, **kwargs)

        client = self.fixture.login()
        form = {"csrf_token": self.fixture.token(client)}
        users._users_file_lock.cache_clear()
        try:
            with (
                patch.object(config, "FileLock", side_effect=short_file_lock),
                patch.object(app_module, "update_user_data", side_effect=delete_update),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                binding = pool.submit(commit_login_binding, save_login, update_accounts=config.update_user_data)
                self.assertTrue(binding_has_user_lock.wait(1))
                deletion = pool.submit(
                    client.post,
                    "/accounts/111/delete",
                    data=form,
                    follow_redirects=False,
                )
                # Give the fixed path time to wait on the user lock.  The old
                # order instead takes usersData.lock immediately and times out
                # waiting for the binding's user lock.
                delete_has_account_lock.wait(0.2)
                allow_binding_save.set()
                saved, action = binding.result(timeout=2)
                response = deletion.result(timeout=2)
        finally:
            users._users_file_lock.cache_clear()

        self.assertEqual("updated", action)
        self.assertEqual("a1", saved["account_ref"])
        self.assertEqual(303, response.status_code, response.text)
        self.assertEqual(["a2"], [row["account_ref"] for row in config.get_userData(force_reload=True)])
        self.assertEqual([], users.find_web_user("alice")["account_refs"])

    def test_failed_account_delete_restores_ownership_before_unlocking(self):
        client = self.fixture.login()

        def fail_after_cleanup(mutator, **kwargs):
            def mutate_then_fail(accounts):
                mutator(accounts)
                raise RuntimeError("synthetic account write failure")

            return config.update_user_data(mutate_then_fail, **kwargs)

        with patch.object(app_module, "update_user_data", side_effect=fail_after_cleanup):
            with self.assertRaisesRegex(RuntimeError, "synthetic account write failure"):
                client.post(
                    "/accounts/111/delete",
                    data={"csrf_token": self.fixture.token(client)},
                    follow_redirects=False,
                )

        self.assertEqual(["a1", "a2"], [row["account_ref"] for row in config.get_userData(force_reload=True)])
        self.assertEqual(["a1"], users.find_web_user("alice")["account_refs"])

    def test_reassigned_owner_cannot_delete_after_the_request_starts(self):
        client = self.fixture.login()
        token = self.fixture.token(client)
        owner = {"username": "alice", "role": "user", "session_id": "race", "account_refs": ["a1"]}
        reassigned = {**owner, "account_refs": []}
        calls = iter([owner, owner, reassigned])
        with patch.object(app_module, "current_principal", side_effect=lambda _request: next(calls, reassigned)):
            response = client.post(
                "/accounts/111/delete",
                data={"csrf_token": token},
                follow_redirects=False,
            )

        self.assertEqual(403, response.status_code)
        self.assertEqual(["a1", "a2"], [row["account_ref"] for row in config.get_userData(force_reload=True)])
        self.assertEqual(["a1"], users.find_web_user("alice")["account_refs"])


if __name__ == "__main__":
    unittest.main()
