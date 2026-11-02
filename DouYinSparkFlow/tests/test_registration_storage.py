from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
import asyncio
import threading
import subprocess
import sys
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken
from fastapi.testclient import TestClient
import httpx

from tests import test_shared_security as fixtures
from tests.auth_helpers import login_form
from utils import config
from utils.secure_storage import StorageError, read_json
from webui import auth, registration, users
from webui import app as app_module
from scripts.migrate_secure_storage import migrate


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client

    def signup_form(self, client=None, username="new-user", **extra):
        client = client or self.client
        page = client.get("/register")
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        return dict(username=username, password="Safe-registration-2026!", confirm_password="Safe-registration-2026!",
                    csrf_token=match.group(1), **extra)

    def test_closed_mode_and_live_policy_changes_are_enforced(self):
        self.assertNotIn('action="/register"', self.client.get("/register").text)
        registration.configure("public", True, 100)
        form = self.signup_form()
        registration.configure("closed", True, 100)
        self.assertEqual(400, self.client.post("/register", data=form).status_code)
        self.assertIsNone(users.find_web_user("new-user"))

    def test_open_registration_requires_approval_and_cannot_claim_accounts(self):
        registration.configure("public", True, 100)
        form = self.signup_form(role="admin", account_refs="a1", enabled="on")
        response = self.client.post("/register", data=form, follow_redirects=False)
        self.assertEqual(303, response.status_code)
        created = users.find_web_user("new-user")
        self.assertEqual("user", created["role"])
        self.assertEqual([], created["account_refs"])
        self.assertFalse(created["enabled"])
        self.assertIsNone(users.authenticate("new-user", "Safe-registration-2026!"))
        users.update_web_user("new-user", enabled=True)
        self.assertIsNotNone(users.authenticate("new-user", "Safe-registration-2026!"))

    def test_browser_registration_preserves_origin_and_keeps_cross_origin_blocked(self):
        from playwright.sync_api import sync_playwright

        registration.configure("public", True, 100)
        with patch.dict(os.environ, {"SPARKFLOW_SESSION_COOKIE_SECURE": "1"}):
            application = app_module.create_app()
        client = TestClient(application, base_url="https://testserver")
        submissions = []

        def respond(route):
            request = route.request
            if not request.url.startswith("https://testserver/"):
                return route.abort()
            headers = request.all_headers()
            response = client.request(request.method, request.url, headers=headers,
                                      content=request.post_data_buffer, follow_redirects=False)
            if request.method == "POST" and request.url.endswith("/register"):
                submissions.append((headers.get("origin"), response.status_code))
            route.fulfill(status=response.status_code, body=response.content,
                          headers={key: value for key, value in response.headers.items()
                                   if key not in {"content-length", "content-encoding"}})

        with sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            page = browser.new_page()
            page.route("**/*", respond)
            page.goto("https://testserver/register")
            page.locator('[name="username"]').fill("browser-guest")
            page.locator('[name="password"]').fill("Safe-registration-2026!")
            page.locator('[name="confirm_password"]').fill("Safe-registration-2026!")
            with page.expect_response(lambda response: response.request.method == "POST"
                                      and response.url.endswith("/register")):
                page.get_by_role("button", name="创建账号").click()
            page.wait_for_load_state("load")
            self.assertEqual([("https://testserver", 303)], submissions)
            created = users.find_web_user("browser-guest")
            self.assertIsNotNone(created)
            self.assertFalse(created["enabled"])
            self.assertEqual([], created["account_refs"])
            browser.close()

        for origin in ("null", "https://other.test"):
            with self.subTest(origin=origin):
                response = client.post("/register", headers={"Origin": origin})
                self.assertEqual(403, response.status_code)
                self.assertEqual("Cross-origin request denied", response.text)

    def test_invitation_is_hashed_single_use_and_not_consumed_on_validation_error(self):
        registration.configure("invite", False, 100)
        token = registration.create_invitation("owner")
        self.assertNotIn(token, users.USERS_FILE.read_text())
        with self.assertRaises(users.UserStoreError):
            registration.register("alice", "Safe-registration-2026!", token)
        self.assertEqual(1, len(registration.invitations()))
        result = registration.register("guest-one", "Safe-registration-2026!", token)
        self.assertTrue(result["enabled"])
        with self.assertRaises(users.UserStoreError):
            registration.register("guest-two", "Safe-registration-2026!", token)

    def test_two_concurrent_requests_cannot_consume_one_invitation_twice(self):
        registration.configure("invite", True, 100)
        token = registration.create_invitation("owner")

        def register(name):
            try:
                registration.register(name, "Safe-registration-2026!", token)
                return True
            except users.UserStoreError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(1, sum(executor.map(register, ["first-guest", "second-guest"])))

    def test_expired_and_revoked_invitations_are_rejected(self):
        registration.configure("invite", True, 100)
        token = registration.create_invitation("owner")
        registration.revoke_invitation(hashlib.sha256(token.encode()).hexdigest()[:16])
        with self.assertRaises(users.UserStoreError):
            registration.register("guest", "Safe-registration-2026!", token)
        token = registration.create_invitation("owner", hours=1)
        import time
        with patch.object(registration.time, "time", return_value=time.time() + 3601):
            with self.assertRaises(users.UserStoreError):
                registration.register("guest", "Safe-registration-2026!", token)

    def test_user_cap_csrf_and_registration_rate_limit(self):
        registration.configure("public", False, 2)
        self.assertEqual(403, self.client.post("/register", data={"username": "guest"}).status_code)
        for _ in range(5):
            self.assertEqual(400, self.client.post("/register", data=self.signup_form()).status_code)
        self.assertEqual(429, self.client.post("/register", data=self.signup_form()).status_code)
        self.assertIsNone(users.find_web_user("new-user"))

    def test_only_admin_with_current_password_can_change_registration_policy(self):
        alice = self.fixture.login()
        token = self.fixture.token(alice)
        self.assertEqual(403, alice.post("/admin/registration", data={"csrf_token": token, "mode": "public"}).status_code)
        admin = self.fixture.login("owner", "Owner-secret-2026!")
        token = self.fixture.token(admin)
        form = {"csrf_token": token, "mode": "public", "max_users": "20", "approval_required": "on", "current_password": "wrong"}
        self.assertEqual(403, admin.post("/admin/registration", data=form).status_code)
        self.assertEqual("closed", registration.policy()["mode"])
        form["current_password"] = "Owner-secret-2026!"
        self.assertEqual(400, admin.post("/admin/registration", data=form, follow_redirects=False).status_code)
        with patch.dict(os.environ, {"SPARKFLOW_SESSION_COOKIE_SECURE": "1"}):
            secure = TestClient(app_module.create_app(), base_url="https://testserver")
            secure.post("/login", data=login_form(secure, "owner", "Owner-secret-2026!"), follow_redirects=False)
            form["csrf_token"] = self.fixture.token(secure)
            self.assertEqual(303, secure.post("/admin/registration", data=form, follow_redirects=False).status_code)
        self.assertEqual("public", registration.policy()["mode"])

    def test_registration_policy_survives_user_updates(self):
        registration.configure("invite", True, 12)
        token = registration.create_invitation("owner")
        users.update_web_user("alice", password="Updated-secret-2026!")
        self.assertEqual("invite", registration.policy()["mode"])
        self.assertEqual(1, len(registration.invitations()))
        registration.register("guest", "Safe-registration-2026!", token)

    def test_new_user_cannot_access_or_trigger_other_accounts(self):
        registration.configure("public", False, 100)
        registration.register("guest", "Safe-registration-2026!")
        client = self.fixture.login("guest", "Safe-registration-2026!")
        token = self.fixture.token(client)
        for action in ("update", "delete", "toggle-enabled", "patrol", "friends/refresh/async"):
            with self.subTest(action=action):
                self.assertEqual(403, client.post(f"/accounts/111/{action}", data={"csrf_token": token}, follow_redirects=False).status_code)
        with patch("webui.app.run_task_now", return_value=123) as run:
            client.post("/ops/run-now", data={"csrf_token": token}, follow_redirects=False)
            self.assertEqual([], run.call_args.kwargs["account_refs"])
            self.assertFalse(run.call_args.kwargs["force_all"])

    def test_logout_revokes_a_stolen_cookie_without_logging_out_other_users(self):
        alice = self.fixture.login()
        bob = self.fixture.login("bob", "Second-secret-2026!")
        stolen = alice.cookies.get("session")
        alice.post("/logout", data={"csrf_token": self.fixture.token(alice)}, follow_redirects=False)
        replay = TestClient(self.fixture.application)
        replay.cookies.set("session", stolen)
        self.assertEqual(401, replay.get("/api/ops/overview").status_code)
        self.assertEqual(200, bob.get("/settings").status_code)

    def test_password_login_requires_csrf(self):
        self.assertEqual(403, self.client.post("/login", data={"username": "owner", "password": "Owner-secret-2026!"}).status_code)

    def test_expired_session_cannot_be_extended_by_reissuing_a_cookie(self):
        from types import SimpleNamespace
        identity = users.authenticate("alice", "First-secret-2026!")
        request = SimpleNamespace(session={})
        auth.issue_session(request, "alice", role="user", auth_version=identity["auth_version"])
        self.assertIsNotNone(auth.current_principal(request))
        request.session["expires_at"] = 1
        self.assertIsNone(auth.current_principal(request))

    def test_large_account_assignments_do_not_bloat_the_session_cookie(self):
        from types import SimpleNamespace
        request = SimpleNamespace(session={})
        auth.issue_session(request, "alice", role="user", account_refs=["account-" + str(index) for index in range(1000)])
        self.assertNotIn("account_refs", request.session)
        self.assertLess(len(json.dumps(request.session)), 1000)

    def test_password_checks_do_not_block_other_requests(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def authenticate(username, password):
                started.set()
                release.wait(2)
                return None

            transport = httpx.ASGITransport(app=self.fixture.application)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                page = await client.get("/login")
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
                with patch("webui.users.authenticate", side_effect=authenticate):
                    pending = asyncio.create_task(client.post("/login", data={"username": "alice", "password": "test", "csrf_token": csrf}))
                    try:
                        await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1)
                        response = await client.get("/favicon.ico")
                        self.assertEqual(200, response.status_code)
                        self.assertFalse(pending.done())
                    finally:
                        release.set()
                        await pending

        asyncio.run(scenario())


class EncryptedStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.key = Fernet.generate_key()
        self.key_file = self.root / "key"
        self.key_file.write_bytes(self.key)
        self.key_file.chmod(0o600)
        context = patch.dict(os.environ, {"SPARKFLOW_STORAGE_KEY_FILE": str(self.key_file)})
        context.start()
        self.addCleanup(context.stop)

    def test_encrypted_round_trip_and_private_backup(self):
        path = self.root / "usersData.json"
        payload = [{"cookies": "private-cookie-test-value", "username": "朋友"}]
        config._save_json_file(path, payload)
        self.assertNotIn("private-cookie-test-value", path.read_text())
        self.assertEqual(payload, read_json(path))
        config._snapshot_user_data(path)
        backup = next((self.root / ".data-backups").glob("*.json"))
        self.assertEqual(payload, read_json(backup))
        self.assertNotIn("private-cookie-test-value", backup.read_text())

    def test_wrong_or_missing_key_and_tampering_never_overwrite_data(self):
        path = self.root / "usersData.json"
        config._save_json_file(path, {"secret": "private"})
        before = path.read_bytes()
        self.key_file.write_bytes(Fernet.generate_key())
        for operation in (lambda: read_json(path), lambda: config._save_json_file(path, {})):
            with self.assertRaises(StorageError):
                operation()
            self.assertEqual(before, path.read_bytes())
        with patch.dict(os.environ, {"SPARKFLOW_STORAGE_KEY_FILE": ""}):
            with self.assertRaises(StorageError):
                config._save_json_file(path, {})
        self.key_file.write_bytes(self.key)
        envelope = json.loads(path.read_text())
        envelope["ciphertext"] = "tampered"
        path.write_text(json.dumps(envelope))
        with self.assertRaises(StorageError):
            read_json(path)

    def test_rotation_and_migration_preflight(self):
        path = self.root / "usersData.json"
        config._save_json_file(path, ["secret"])
        plain = self.root / "config.json"
        plain.write_text('{"value": 1}')
        self.key_file.write_bytes(Fernet.generate_key())
        with self.assertRaises(StorageError):
            migrate(self.root)
        self.assertEqual('{"value": 1}', plain.read_text())
        new_key = Fernet.generate_key()
        self.key_file.write_bytes(new_key + b"\n" + self.key)
        migrate(self.root)
        token = json.loads(path.read_text())["ciphertext"].encode()
        self.assertEqual(["secret"], json.loads(Fernet(new_key).decrypt(token)))
        with self.assertRaises(InvalidToken):
            Fernet(self.key).decrypt(token)

    def test_failed_save_does_not_replace_the_in_memory_account_cache(self):
        previous = config.userData
        self.addCleanup(setattr, config, "userData", previous)
        config.userData = [{"username": "before"}]
        with patch.object(config, "_save_json_file", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                config.save_userData([{"username": "after"}])
        self.assertEqual([{"username": "before"}], config.userData)

    def test_failed_atomic_replace_preserves_the_original_file(self):
        path = self.root / "usersData.json"
        config._save_json_file(path, ["before"])
        original = path.read_bytes()
        with patch.object(config.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(StorageError):
                config._save_json_file(path, ["after"])
        self.assertEqual(original, path.read_bytes())
        self.assertEqual([], list(self.root.glob("*.tmp")))

    def test_protected_mode_rejects_plaintext_downgrade_and_missing_key(self):
        path = self.root / "usersData.json"
        path.write_text('["downgraded"]')
        with patch.dict(os.environ, {"SPARKFLOW_STORAGE_REQUIRE_ENCRYPTED": "1"}):
            with self.assertRaises(StorageError):
                read_json(path)
            with self.assertRaises(StorageError):
                config._save_json_file(path, ["replacement"])
            migrate(self.root)
            self.assertEqual(["downgraded"], read_json(path))
            with patch.dict(os.environ, {"SPARKFLOW_STORAGE_KEY_FILE": ""}):
                with self.assertRaises(StorageError):
                    config._save_json_file(self.root / "new.json", {})

    def test_check_rejects_unmigrated_plaintext_and_key_init_never_overwrites(self):
        (self.root / "config.json").write_text('{}')
        with self.assertRaises(StorageError):
            migrate(self.root, check_only=True)
        script = Path(__file__).resolve().parents[2] / "deploy" / "init-storage-key.py"
        key = self.root / "new.key"
        first = subprocess.run([sys.executable, str(script), str(key)], capture_output=True)
        self.assertEqual(0, first.returncode)
        original = key.read_bytes()
        second = subprocess.run([sys.executable, str(script), str(key)], capture_output=True)
        self.assertNotEqual(0, second.returncode)
        self.assertEqual(original, key.read_bytes())


if __name__ == "__main__":
    unittest.main()
