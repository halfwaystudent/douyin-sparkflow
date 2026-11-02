import asyncio
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

from utils import config
from webui import app as app_module, auth, users
from webui.security import SecurityMiddleware


class RequestBoundaryTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()

        @app.api_route("/echo", methods=["GET", "POST"])
        async def echo(request: Request):
            return PlainTextResponse((await request.body()).decode())

        app.add_middleware(SecurityMiddleware, max_request_bytes=32, shared=True)
        self.app = app
        self.client = TestClient(app)

    def test_cross_origin_form_and_null_origin_are_rejected(self):
        for origin in ("https://other.example", "null", "http://testserver.evil", "http://testserver:81"):
            with self.subTest(origin=origin):
                response = self.client.post("/echo", content="hello", headers={"Origin": origin})
                self.assertEqual(403, response.status_code)
        self.assertEqual(403, self.client.post("/echo", headers={"Sec-Fetch-Site": "cross-site"}).status_code)
        self.assertEqual(200, self.client.post("/echo", headers={"Origin": "http://testserver"}).status_code)

    def test_body_limit_applies_to_declared_and_chunked_bodies(self):
        self.assertEqual(413, self.client.post("/echo", content=b"x" * 33).status_code)
        response = self.client.post("/echo", content=iter([b"x" * 20, b"y" * 20]))
        self.assertEqual(413, response.status_code)
        self.assertEqual("hello", self.client.post("/echo", content="hello").text)

    def test_private_responses_and_errors_have_security_headers(self):
        for response in (self.client.get("/echo"), self.client.post("/echo", content="x" * 40)):
            self.assertEqual("no-store", response.headers["cache-control"])
            self.assertEqual("nosniff", response.headers["x-content-type-options"])
            self.assertIn("frame-ancestors 'self'", response.headers["content-security-policy"])

    def test_websocket_origin_is_checked_before_connecting_upstream(self):
        for headers in ({}, {"origin": "https://other.example"}):
            with self.subTest(headers=headers):
                from starlette.websockets import WebSocketDisconnect
                with self.assertRaises(WebSocketDisconnect) as raised:
                    with self.client.websocket_connect("/socket", headers=headers):
                        pass
                self.assertEqual(4403, raised.exception.code)

    def test_open_login_desktop_disconnects_after_session_revocation(self):
        endpoint = next(route.endpoint for route in app_module.app.routes if getattr(route, "path", "") == "/login-desktop/proxy/websockify")

        async def run():
            pending = asyncio.Event()

            async def wait_for_client():
                await pending.wait()

            class Upstream:
                subprotocol = None

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                async def __aiter__(self):
                    await pending.wait()
                    if False:
                        yield b""

            socket = SimpleNamespace(headers={}, receive=wait_for_client, accept=AsyncMock(), close=AsyncMock())
            principal = {"username": "alice", "role": "user", "session_id": "s1", "account_refs": ["a1"]}
            with (
                patch.object(app_module, "current_principal", side_effect=[principal, None]),
                patch.object(app_module, "get_login_lock", return_value={"account_ref": "a1"}),
                patch.object(app_module, "owns_login_lock", return_value=True),
                patch.object(app_module.websockets, "connect", return_value=Upstream()),
            ):
                await asyncio.wait_for(endpoint(socket), timeout=3)
            socket.close.assert_awaited_once_with(code=4403)

        asyncio.run(run())


class SharedConsoleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for attribute, value in (("appSettings", None), ("userData", None)):
            previous = getattr(config, attribute)
            setattr(config, attribute, value)
            self.addCleanup(setattr, config, attribute, previous)
        for context in (
            patch.object(config, "app_settings_path", return_value=root / "settings.json"),
            patch.object(auth, "app_settings_path", return_value=root / "settings.json"),
            patch.object(config, "users_data_path", return_value=root / "accounts.json"),
            patch.object(config, "get_environment", return_value=config.Environment.LOCAL),
            patch.object(users, "USERS_FILE", root / "users.json"),
            patch.dict(os.environ, {"SPARKFLOW_SHARED_MODE": "1", "SPARKFLOW_ALLOWED_HOSTS": "testserver",
                                    "SPARKFLOW_BOOTSTRAP_TOKEN": "a-private-initialization-token", "SPARKFLOW_SESSION_COOKIE_SECURE": "0"}),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.accounts = [
            {"account_ref": "a1", "unique_id": "111", "username": "Alice account", "cookies": [], "targets": []},
            {"account_ref": "a2", "unique_id": "222", "username": "Bob account", "cookies": [], "targets": []},
        ]
        (root / "accounts.json").write_text(json.dumps(self.accounts))
        auth.bootstrap_admin_password("Owner-secret-2026!", username="owner")
        users.create_web_user("alice", "First-secret-2026!", account_refs=["a1"])
        users.create_web_user("bob", "Second-secret-2026!", account_refs=["a2"])
        self.application = app_module.create_app()
        self.client = TestClient(self.application)

    def login(self, username="alice", password="First-secret-2026!"):
        client = TestClient(self.application)
        response = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
        self.assertEqual(303, response.status_code)
        return client

    def token(self, client):
        response = client.get("/settings")
        self.assertEqual(200, response.status_code)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)

    def test_shared_mode_requires_hosts_and_protects_host_header(self):
        self.assertEqual(400, self.client.get("/login", headers={"host": "other.example"}).status_code)
        for hosts in ("", "*"):
            with patch.dict(os.environ, {"SPARKFLOW_ALLOWED_HOSTS": hosts}):
                with self.assertRaises(ValueError):
                    app_module.create_app()

    def test_user_cannot_operate_on_another_account_or_manage_users(self):
        client = self.login()
        token = self.token(client)
        with patch.object(app_module, "get_ops_snapshot", return_value={"send_console": {"summary": {}, "accounts": []}}):
            for path in ("/accounts/222/update", "/accounts/222/delete", "/admin/users/bob/revoke-sessions"):
                with self.subTest(path=path):
                    response = client.post(path, data={"csrf_token": token}, follow_redirects=False)
                    self.assertEqual(403, response.status_code)
        self.assertEqual(2, len(config.get_userData(force_reload=True)))

    def test_revoke_sessions_expires_only_the_selected_user(self):
        alice = self.login()
        bob = self.login("bob", "Second-secret-2026!")
        admin = self.login("owner", "Owner-secret-2026!")
        token = self.token(admin)
        response = admin.post("/admin/users/alice/revoke-sessions", data={"csrf_token": token}, follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertEqual(401, alice.get("/api/ops/overview").status_code)
        self.assertEqual(200, bob.get("/settings").status_code)
        self.assertIsNotNone(users.authenticate("alice", "First-secret-2026!"))

    def test_reassignment_expires_existing_user_sessions(self):
        client = self.login()
        users.update_web_user("alice", account_refs=[])
        self.assertEqual(401, client.get("/api/ops/overview").status_code)

    def test_password_change_requires_current_password(self):
        client = self.login()
        token = self.token(client)
        form = {"csrf_token": token, "new_password": "Replacement-secret-2026!",
                "confirm_password": "Replacement-secret-2026!", "current_password": "wrong"}
        client.post("/account/password", data=form, follow_redirects=False)
        self.assertIsNotNone(users.authenticate("alice", "First-secret-2026!"))
        form["current_password"] = "First-secret-2026!"
        client.post("/account/password", data=form, follow_redirects=False)
        self.assertIsNone(users.authenticate("alice", "First-secret-2026!"))
        self.assertIsNotNone(users.authenticate("alice", "Replacement-secret-2026!"))

    def test_custom_admin_name_is_reserved_for_user_create_and_rename(self):
        with self.assertRaises(users.UserStoreError):
            users.create_web_user("OWNER", "password")
        with self.assertRaises(users.UserStoreError):
            users.update_web_user("alice", new_username="owner")

    def test_bootstrap_cannot_replace_existing_credentials(self):
        with self.assertRaises(auth.BootstrapAlreadyConfigured):
            auth.bootstrap_admin_password("Replacement-secret-2026!")
        self.assertIsNotNone(users.authenticate("owner", "Owner-secret-2026!"))

    def test_logout_requires_csrf(self):
        client = self.login()
        self.assertEqual(403, client.post("/logout").status_code)
        self.assertEqual(303, client.post("/logout", data={"csrf_token": self.token(client)}, follow_redirects=False).status_code)
        self.assertEqual(401, client.get("/api/ops/overview").status_code)

    def test_setup_never_selects_another_users_account(self):
        client = self.login()
        response = client.get("/setup?account=a2&step=4")
        self.assertEqual(200, response.status_code)
        self.assertIn("第 1 步，共 4 步", response.text)
        self.assertNotIn("Bob account", response.text)

    def test_non_ascii_csrf_is_rejected_without_a_server_error(self):
        client = self.login()
        self.assertEqual(403, client.post("/logout", data={"csrf_token": "错误令牌"}).status_code)

    def test_shared_proxy_operations_are_disabled(self):
        client = self.login("owner", "Owner-secret-2026!")
        token = self.token(client)
        self.assertEqual(403, client.post("/ops/proxy/restart", data={"csrf_token": token}).status_code)


if __name__ == "__main__":
    unittest.main()
