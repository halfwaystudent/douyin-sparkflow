"""Authentication stays responsive while file writers or credential workers wait."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import re
import threading
import time
import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient
from filelock import FileLock

from tests import test_shared_security as fixtures
from tests.auth_helpers import login_form
from utils import config
from webui import app as app_module, auth, credentials, passkeys, users


async def wait_until(predicate):
    while not predicate():
        await asyncio.sleep(0.001)


class AuthWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_writer_holds_capacity_until_it_really_finishes(self):
        worker = credentials._BoundedWorker(1, "auth-cancel-test")
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        writes = []

        def slow_write():
            started.set()
            release.wait(2)
            writes.append("saved")
            finished.set()
            raise ValueError("late worker error")

        late_errors = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda current, context: late_errors.append(context))
        try:
            task = asyncio.create_task(worker.run(slow_write))
            await asyncio.wait_for(wait_until(started.is_set), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            rejected = Mock()
            with self.assertRaises(credentials.CredentialBusyError):
                await worker.run(rejected)
            rejected.assert_not_called()
            self.assertEqual([], writes)
            release.set()
            await asyncio.wait_for(wait_until(finished.is_set), 1)
            await asyncio.sleep(0.02)
            self.assertEqual(7, await worker.run(lambda: 7))
            self.assertEqual(["saved"], writes)
            self.assertEqual([], late_errors)
        finally:
            release.set()
            worker.executor.shutdown(wait=True)
            loop.set_exception_handler(previous_handler)

    async def test_worker_exception_still_reaches_its_awaiting_caller(self):
        worker = credentials._BoundedWorker(1, "auth-error-test")
        try:
            def fail():
                raise ValueError("expected failure")
            with self.assertRaisesRegex(ValueError, "expected failure"):
                await worker.run(fail)
            self.assertEqual(8, await worker.run(lambda: 8))
        finally:
            worker.executor.shutdown(wait=True)


class AuthResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_initialized_login_does_not_wait_for_a_settings_writer(self):
        locked, release = threading.Event(), threading.Event()

        def writer():
            with FileLock(str(config.app_settings_path()) + ".lock"):
                locked.set()
                release.wait(2)

        holder = threading.Thread(target=writer)
        holder.start()
        self.assertTrue(locked.wait(1))
        try:
            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.fixture.application), base_url="http://testserver") as client:
                    response = await asyncio.wait_for(client.get("/login"), 0.5)
                    self.assertEqual(200, response.status_code)
                    self.assertFalse(release.is_set())
            asyncio.run(check())
        finally:
            release.set()
            holder.join(2)

    def test_login_reads_are_independent_of_default_and_credential_executor_saturation(self):
        release = threading.Event()
        entered = [threading.Event() for _ in range(6)]
        def blocked(index):
            entered[index].set()
            release.wait(3)

        async def check():
            loop = asyncio.get_running_loop()
            loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
            default_jobs = [asyncio.create_task(asyncio.to_thread(blocked, i)) for i in range(2)]
            credential_jobs = [asyncio.create_task(credentials.run_credential_work(blocked, i)) for i in range(2, 6)]
            try:
                await asyncio.wait_for(wait_until(lambda: all(event.is_set() for event in entered)), 1)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.fixture.application), base_url="http://testserver") as client:
                    page = await asyncio.wait_for(client.get("/login"), 0.5)
                    self.assertEqual(200, page.status_code)
                    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
                    with patch.object(users, "authenticate") as authenticate:
                        response = await asyncio.wait_for(client.post("/login", data={"username": "alice", "password": "First-secret-2026!", "csrf_token": token}), 0.5)
                    self.assertEqual(503, response.status_code)
                    self.assertEqual("3", response.headers["retry-after"])
                    authenticate.assert_not_called()
                    self.assertFalse(release.is_set())
            finally:
                release.set()
                await asyncio.gather(*default_jobs, *credential_jobs)
        asyncio.run(check())

    def test_password_verification_cannot_issue_session_after_user_is_revoked_or_recreated(self):
        original = users.authenticate
        for operation in ("disable", "recreate"):
            with self.subTest(operation=operation):
                if operation == "recreate":
                    users.update_web_user("alice", enabled=True)
                def changed_while_verifying(username, password):
                    identity = original(username, password)
                    if operation == "disable":
                        users.update_web_user(username, enabled=False)
                    else:
                        users.delete_web_user(username)
                        users.create_web_user(username, "New-person-secret-2026!")
                    return identity
                client = TestClient(self.fixture.application)
                form = login_form(client, "alice", "First-secret-2026!")
                with patch.object(users, "authenticate", side_effect=changed_while_verifying):
                    response = client.post("/login", data=form, follow_redirects=False)
                self.assertEqual(303, response.status_code)
                self.assertEqual("/login", response.headers["location"])
                self.assertEqual(401, client.get("/api/ops/overview").status_code)

    def test_passkey_completion_is_rechecked_before_a_cookie_is_issued(self):
        identity = passkeys.login_identity("alice")
        store = Mock()
        def finish(*args, **kwargs):
            users.update_web_user("alice", revoke_sessions=True)
            return identity
        store.finish.side_effect = finish
        with patch.dict(os.environ, {"SPARKFLOW_PASSKEY_RP_ID": "testserver", "SPARKFLOW_PASSKEY_ORIGIN": "https://testserver"}), patch.object(app_module, "PasskeyStore", return_value=store):
            application = app_module.create_app()
        client = TestClient(application)
        response = client.post("/auth/passkeys/verify", data={"challenge_id": "offline-test", "credential": "{}"})
        self.assertEqual(400, response.status_code)
        self.assertEqual(401, client.get("/api/ops/overview").status_code)

    def test_admin_password_form_changes_password_and_invalidates_its_old_session(self):
        client = self.fixture.login("owner", "Owner-secret-2026!")
        token = self.fixture.token(client)
        response = client.post("/settings", data={"csrf_token": token, "current_password": "Owner-secret-2026!",
            "new_password": "Replacement-secret-2026!", "confirm_password": "Replacement-secret-2026!"}, follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertIsNone(users.authenticate("owner", "Owner-secret-2026!"))
        self.assertIsNotNone(users.authenticate("owner", "Replacement-secret-2026!"))
        self.assertEqual(401, client.get("/api/ops/overview").status_code)

    def test_read_snapshot_fails_closed_on_damaged_encrypted_settings(self):
        config.app_settings_path().write_text('{"bad":', encoding="utf-8")
        with self.assertRaises(config.StorageError):
            config.read_app_settings()


class PasskeyMutationAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        context = patch.object(passkeys, "PASSKEYS_FILE", users.USERS_FILE.with_name("passkeys.json"))
        context.start()
        self.addCleanup(context.stop)
        self.store = passkeys.PasskeyStore("testserver", "https://testserver")
        self.identity = passkeys.login_identity("alice")

    def test_revocation_during_verification_consumes_challenge_without_creating_a_key(self):
        from types import SimpleNamespace
        challenge = self.store.begin(self.identity, "session-a", registration=True)
        revoked = False
        def authorize():
            if revoked:
                raise ValueError("session revoked")
        def verify(**kwargs):
            nonlocal revoked
            revoked = True
            return SimpleNamespace(credential_id=b"offline-id", credential_public_key=b"offline-public-key", sign_count=0)
        with patch.object(passkeys, "verify_registration_response", side_effect=verify):
            with self.assertRaisesRegex(ValueError, "session revoked"):
                self.store.finish(challenge["challenge_id"], "session-a", {}, registration=True, identity=self.identity, authorize=authorize)
        data = passkeys.read_json(passkeys.PASSKEYS_FILE)
        self.assertNotIn(challenge["challenge_id"], data["challenges"])
        self.assertEqual([], data["credentials"])

    def test_account_version_change_during_verification_does_not_create_a_key(self):
        from types import SimpleNamespace
        challenge = self.store.begin(self.identity, "session-a", registration=True)
        def verify(**kwargs):
            users.update_web_user("alice", revoke_sessions=True)
            return SimpleNamespace(credential_id=b"offline-id", credential_public_key=b"offline-public-key", sign_count=0)
        with patch.object(passkeys, "verify_registration_response", side_effect=verify):
            with self.assertRaises(ValueError):
                self.store.finish(challenge["challenge_id"], "session-a", {}, registration=True, identity=self.identity)
        data = passkeys.read_json(passkeys.PASSKEYS_FILE)
        self.assertNotIn(challenge["challenge_id"], data["challenges"])
        self.assertEqual([], data["credentials"])

    def test_waiting_for_the_passkey_lock_does_not_keep_old_delete_authorization(self):
        config._save_json_file(passkeys.PASSKEYS_FILE, {"credentials": [{"id": "owned-key", "user_key": self.identity["user_key"]}], "challenges": {}})
        entered = threading.Event()
        revoked = False
        def authorize():
            if revoked:
                raise ValueError("session revoked")
        def delete():
            entered.set()
            self.store.delete(self.identity, "owned-key", authorize=authorize)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with FileLock(str(passkeys.PASSKEYS_FILE) + ".lock"):
                future = executor.submit(delete)
                self.assertTrue(entered.wait(1))
                revoked = True
            with self.assertRaisesRegex(ValueError, "session revoked"):
                future.result(timeout=1)
        self.assertEqual("owned-key", passkeys.read_json(passkeys.PASSKEYS_FILE)["credentials"][0]["id"])
