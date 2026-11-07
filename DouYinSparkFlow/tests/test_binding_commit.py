from copy import deepcopy
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from core import identity
from tests import test_shared_security as fixtures
from utils import config
from utils.secure_storage import StorageError
from webui import app as app_module, users, login_lock
from webui.account_binding import BindingCommitError, commit_login_binding, binding_progress


ALICE = {"username": "alice", "role": "user", "session_id": "binding-test"}
TRUSTED = {"uid": "333", "source": identity.IDENTITY_SOURCE, "version": 1}


class BindingCommitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def bind(self, principal=ALICE, *, relogin="", claim=None, expected=None):
        if expected is None:
            expected = {row["account_ref"]: identity.record_fingerprint(row) for row in config.get_userData(force_reload=True)}
        return commit_login_binding(
            lambda: app_module.save_exported_login_result(
                {"username": "New account", "cookies": [{"name": "sessionid", "value": "new-private-cookie"}]},
                relogin_account_ref=relogin, identity=TRUSTED, expected_records=expected,
                principal_resolver=lambda: principal, expected_principal=principal,
            ),
            update_accounts=config.update_user_data,
            claim_callback=claim or (lambda account: users.update_web_user(principal["username"], append_account_ref=account["account_ref"])),
            owner_username=principal["username"],
        )

    def test_binding_and_claim_share_reentrant_user_lock(self):
        saved, action = self.bind()
        self.assertEqual("created", action)
        self.assertEqual("alice", users.account_owner_username(saved["account_ref"]))
        self.assertIn(saved["account_ref"], users.find_web_user("alice")["account_refs"])
        self.assertNotIn("new-private-cookie", config.users_data_path().read_text())

    def test_failed_new_claim_removes_only_new_row(self):
        before = config.get_userData(force_reload=True)
        with patch.object(users, "_atomic_write_json", side_effect=StorageError("synthetic storage failure")):
            with self.assertRaises(BindingCommitError) as caught:
                self.bind()
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(before, config.get_userData(force_reload=True))
        self.assertEqual(["a1"], users.find_web_user("alice")["account_refs"])

    def test_failed_adoption_restores_login_fields_but_keeps_new_health(self):
        account = {"account_ref": "unassigned", "unique_id": "333", "username": "Old name", "cookies": [{"name": "sessionid", "value": "old"}], "targets": ["friend"], "enabled": True}
        identity.bind_identity(account, TRUSTED, identity.BINDING_NEW)
        config.save_userData(self.fixture.accounts + [account])
        health = {"healthy": False, "category": "network_unavailable", "reason": "newer background result"}

        def fail_claim(_account):
            def record(accounts):
                accounts[-1]["account_health"] = health
            config.update_user_data(record, force_reload=True)
            raise StorageError("synthetic claim failure")

        with self.assertRaises(BindingCommitError) as caught:
            self.bind(relogin="unassigned", claim=fail_claim)
        self.assertFalse(caught.exception.uncertain)
        restored = config.get_userData(force_reload=True)[-1]
        self.assertEqual(account["cookies"], restored["cookies"])
        self.assertEqual(account["username"], restored["username"])
        self.assertEqual(health, restored["account_health"])
        self.assertEqual(["friend"], restored["targets"])

    def test_newer_login_is_never_overwritten_by_failed_claim_recovery(self):
        def fail_claim(_account):
            def record(accounts):
                accounts[-1]["login_generation"] = "newer-generation"
                accounts[-1]["cookies"] = [{"name": "sessionid", "value": "newer"}]
            config.update_user_data(record, force_reload=True)
            raise StorageError("synthetic claim failure")

        with self.assertRaises(BindingCommitError) as caught:
            self.bind(claim=fail_claim)
        self.assertTrue(caught.exception.uncertain)
        latest = config.get_userData(force_reload=True)[-1]
        self.assertEqual("newer-generation", latest["login_generation"])
        self.assertEqual("newer", latest["cookies"][0]["value"])

    def test_two_users_cannot_claim_same_login_during_save_interval(self):
        expected = {row["account_ref"]: identity.record_fingerprint(row) for row in config.get_userData(force_reload=True)}
        def run(principal):
            try:
                return self.bind(principal, expected=expected)[0]
            except (app_module.LoginSavePermissionDenied, identity.AccountChangedConcurrently):
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, [ALICE, {**ALICE, "username": "bob"}]))
        self.assertEqual(1, sum(result is not None for result in outcomes))
        rows = identity.accounts_bound_to(config.get_userData(force_reload=True), "333")
        self.assertEqual(1, len(rows))
        ref = rows[0]["account_ref"]
        self.assertEqual(1, sum(ref in user["account_refs"] for user in users.get_web_users()))


class BindingStatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_progress_is_scoped_and_does_not_include_credentials(self):
        rows = deepcopy(self.fixture.accounts)
        rows[0]["cookies"] = [{"name": "sessionid", "value": "private-cookie"}]
        rows[0]["login_verification"] = {"state": "verifying", "token": "private-verification-token"}
        rows[0]["login_generation"] = "generation-a1"
        config.save_userData(rows)
        response = self.fixture.login().get("/accounts/binding-status")
        self.assertEqual(200, response.status_code)
        self.assertEqual("no-store", response.headers["cache-control"])
        accounts = response.json()["accounts"]
        self.assertEqual(["a1"], [row["account_ref"] for row in accounts])
        self.assertEqual("verifying", accounts[0]["verification"]["state"])
        for secret in ("private-cookie", "private-verification-token", "Bob account", '"cookies"'):
            self.assertNotIn(secret, response.text)

    def test_cookie_binding_never_clears_another_users_login_workspace(self):
        alice = self.fixture.login()
        bob = self.fixture.login("bob", "Second-secret-2026!")
        with tempfile.TemporaryDirectory() as directory, patch.object(login_lock, "LOCK_PATH", Path(directory) / "workspace.json"):
            with patch.object(app_module, "call_login_desktop", return_value={"ok": True}):
                opened = bob.post("/login-desktop/open", data={"csrf_token": self.fixture.token(bob), "mode": "add"})
            self.assertEqual(200, opened.status_code)
            ticket = opened.json()["workspace"]["ticket"]
            verified = (True, "", {"username": "New account", "display_id": "", "trusted": TRUSTED}, "")
            with patch.object(app_module, "verify_login_result", return_value=verified), patch.object(app_module, "call_login_desktop") as shared_browser:
                response = alice.post("/accounts/cookies", data={"csrf_token": self.fixture.token(alice), "cookie_input": "sessionid=private-session; sid_guard=private-guard"})
            self.assertEqual(200, response.status_code)
            shared_browser.assert_not_called()
            active = login_lock.get_lock()
            self.assertEqual("bob", active["username"])
            self.assertEqual(ticket, active["ticket"])

    def test_cookie_verification_rejects_extra_browser_start_while_busy(self):
        alice = self.fixture.login()
        bob = self.fixture.login("bob", "Second-secret-2026!")
        alice_form = {"csrf_token": self.fixture.token(alice), "cookie_input": "sessionid=synthetic"}
        bob_form = {"csrf_token": self.fixture.token(bob), "cookie_input": "sessionid=other-synthetic"}
        started, release = threading.Event(), threading.Event()
        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.to_thread(release.wait, 3)
            return False, "synthetic network problem", {}, "network_unavailable"
        with patch.object(app_module, "verify_login_result", side_effect=blocked) as verify, ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(alice.post, "/accounts/cookies", data=alice_form)
            try:
                self.assertTrue(started.wait(2))
                second = bob.post("/accounts/cookies", data=bob_form)
                self.assertEqual(429, second.status_code)
                self.assertEqual("5", second.headers["retry-after"])
                self.assertEqual(1, verify.call_count)
            finally:
                release.set()
            self.assertEqual(400, first.result(timeout=5).status_code)

    def test_legacy_auto_match_that_creates_a_separate_account_claims_the_new_ref(self):
        alice = self.fixture.login()
        csrf = self.fixture.token(alice)
        original = config.get_userData(force_reload=True)[0]
        exported = {"username": "Alice account", "display_id": "111", "cookies": [{"name": "sessionid", "value": "new-private-session"}]}
        def desktop(path, **_):
            return {"ok": True, "result": exported} if path == "/export" else {"ok": True}
        legacy_check = {"account_ref": "a1", "fingerprint": identity.record_fingerprint(original), "uid": "111"}
        async def wait_for_shutdown(*args, **kwargs):
            await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as directory, patch.object(login_lock, "LOCK_PATH", Path(directory) / "workspace.json"), patch.object(app_module, "call_login_desktop", side_effect=desktop):
            opened = alice.post("/login-desktop/open", data={"csrf_token": csrf, "mode": "add"})
            ticket = opened.json()["workspace"]["ticket"]
            with patch.object(app_module.account_identity, "fetch_trusted_identity", return_value=TRUSTED), patch.object(app_module, "legacy_identity_check", return_value=legacy_check), patch.object(app_module, "verify_account_session", side_effect=wait_for_shutdown):
                response = alice.post("/login-desktop/save", data={"csrf_token": csrf, "ticket": ticket})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual("created", response.json()["action"])
            ref = response.json()["account"]["account_ref"]
            self.assertEqual("alice", users.account_owner_username(ref))
            self.assertEqual(original, config.get_userData(force_reload=True)[0])

    def test_unknown_and_other_users_refs_are_indistinguishable(self):
        bob = self.fixture.login("bob", "Second-secret-2026!")
        for ref in ("a1", "not-present"):
            response = bob.get("/accounts/binding-status", params={"account_ref": ref})
            self.assertEqual(404, response.status_code)
            self.assertNotIn("Alice", response.text)
        self.assertEqual(401, self.fixture.client.get("/accounts/binding-status").status_code)

    def test_revoked_session_during_storage_read_cannot_receive_progress(self):
        client = self.fixture.login()
        def read(**_):
            users.update_web_user("alice", enabled=False)
            return deepcopy(self.fixture.accounts)
        with patch.object(app_module, "get_userData", side_effect=read):
            response = client.get("/accounts/binding-status")
        self.assertEqual(401, response.status_code)
        self.assertNotIn("Alice account", response.text)

    def test_later_login_failures_are_not_shown_as_verified(self):
        account = deepcopy(self.fixture.accounts[0])
        identity.bind_identity(account, {"uid": "111"}, "fixture")
        self.assertEqual("verified", binding_progress(account)["verification"]["state"])
        for changes, expected in (
            ({"login_required": True}, "login_required"),
            ({"identity_mismatch": {"actual": "222"}}, "identity_conflict"),
            ({"pending_login_verification": True}, "unverified"),
            ({"account_health": {"healthy": False, "category": "expired_cookies"}}, "login_required"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(expected, binding_progress({**account, **changes})["verification"]["state"])
