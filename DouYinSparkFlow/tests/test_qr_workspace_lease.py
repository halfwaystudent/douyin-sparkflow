"""QR reads and regeneration cannot cross a workspace handoff."""

import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import login_desktop_server as desktop
from core.login_interaction import InteractionError
from tests import test_shared_security as fixtures
from webui import app as app_module, login_lock, sessions


TOKEN = "ticket-" + "a" * 32
OTHER = "ticket-" + "b" * 32


class QrWorkspaceProxyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        lock = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
        lock.start()
        self.addCleanup(lock.stop)
        self.alice = self.fixture.login()
        self.csrf = self.fixture.token(self.alice)
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True}):
            response = self.alice.post("/login-desktop/open", data={"csrf_token": self.csrf, "mode": "add"})
        self.assertEqual(200, response.status_code)
        self.active = login_lock.get_lock()
        self.ticket = self.active["ticket"]

    def qr(self, ticket=None):
        return self.alice.get("/login-desktop/qr", headers={"X-SparkFlow-Workspace": self.ticket if ticket is None else ticket})

    def refresh(self, ticket=None):
        return self.alice.post("/login-desktop/qr/refresh", data={"csrf_token": self.csrf, "ticket": self.ticket if ticket is None else ticket})

    def handoff(self):
        login_lock.release(force=True)
        return login_lock.request_workspace(username="bob", session_id="bob-session", mode="add")["request"]

    def upstream(self, content=b"OWN-QR"):
        return SimpleNamespace(status=200, headers={"Content-Type": "image/png"}, read=lambda: content, close=lambda: None)

    def test_read_requires_current_header_and_forwards_it_without_url_token(self):
        with patch.object(app_module.urllib.request, "urlopen", return_value=self.upstream()) as upstream:
            self.assertEqual(423, self.qr("").status_code)
            self.assertEqual(423, self.qr(OTHER).status_code)
            upstream.assert_not_called()
            response = self.qr()
        self.assertEqual(200, response.status_code)
        self.assertEqual(b"OWN-QR", response.content)
        request = upstream.call_args.args[0]
        self.assertEqual(self.ticket, dict((key.lower(), value) for key, value in request.header_items())["x-sparkflow-workspace"])
        self.assertNotIn(self.ticket, request.full_url)

    def test_read_after_handoff_drops_the_next_users_qr(self):
        def read(*args, **kwargs):
            self.handoff()
            return self.upstream(b"NEXT-USER-QR")
        with patch.object(app_module.urllib.request, "urlopen", side_effect=read):
            response = self.qr()
        self.assertEqual(423, response.status_code)
        self.assertNotIn(b"NEXT-USER-QR", response.content)
        self.assertEqual("bob", login_lock.get_lock()["username"])

    def test_revoked_session_during_read_drops_qr(self):
        def read(*args, **kwargs):
            sessions.revoke(self.active["session_id"], time.time() + 3600)
            return self.upstream(b"PRIVATE-QR")
        with patch.object(app_module.urllib.request, "urlopen", side_effect=read):
            response = self.qr()
        self.assertEqual(423, response.status_code)
        self.assertNotIn(b"PRIVATE-QR", response.content)

    def test_old_refresh_ticket_cannot_regenerate_new_lease_in_same_session(self):
        login_lock.release(force=True)
        replacement = login_lock.request_workspace(username=self.active["username"], session_id=self.active["session_id"], mode="add")["request"]
        with patch.object(app_module, "call_login_desktop_json") as upstream:
            self.assertEqual(423, self.refresh().status_code)
            self.assertEqual(423, self.refresh("").status_code)
            self.assertEqual(423, self.refresh("无效票据").status_code)
            upstream.assert_not_called()
        self.assertEqual(replacement["ticket"], login_lock.get_lock()["ticket"])

    def test_failed_renewal_prevents_refresh_forwarding(self):
        with patch.object(app_module, "heartbeat_login", return_value=False), patch.object(app_module, "call_login_desktop_json") as upstream:
            self.assertEqual(423, self.refresh().status_code)
            upstream.assert_not_called()

    def test_current_refresh_forwards_capability_and_handoff_drops_return(self):
        def refresh(*args, **kwargs):
            self.assertEqual({"interaction_key": self.ticket}, kwargs["payload"])
            self.handoff()
            return 200, {"ok": True, "message": "NEXT-USER-STATE"}
        with patch.object(app_module, "call_login_desktop_json", side_effect=refresh):
            response = self.refresh()
        self.assertEqual(423, response.status_code)
        self.assertNotIn("NEXT-USER-STATE", response.text)
        self.assertEqual("bob", login_lock.get_lock()["username"])

    def test_current_refresh_succeeds_with_current_lease(self):
        with patch.object(app_module, "call_login_desktop_json", return_value=(200, {"ok": True})) as upstream:
            response = self.refresh()
        self.assertEqual(200, response.status_code)
        self.assertEqual(self.ticket, response.json()["workspace"]["ticket"])
        self.assertEqual({"interaction_key": self.ticket}, upstream.call_args.kwargs["payload"])


class QrServiceLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = desktop.LoginDesktopManager()
        self.context = SimpleNamespace(_impl_obj=object())
        self.page = SimpleNamespace(context=self.context, url="https://creator.douyin.com/", is_closed=lambda: False)
        self.manager.context = self.context
        self.manager.page = self.page
        self.manager.interaction.context = self.context
        self.manager.interaction.token = TOKEN
        self.manager._get_active_page = AsyncMock(return_value=self.page)
        manager = patch.object(desktop, "manager", self.manager)
        manager.start()
        self.addCleanup(manager.stop)

    async def test_read_rejects_absent_or_stale_capability_before_probe(self):
        with patch.object(desktop, "_probe_login_qr", new_callable=AsyncMock) as probe:
            for token in ("", OTHER):
                with self.subTest(token=token), self.assertRaises(InteractionError):
                    await desktop._read_login_qr(0, token)
            probe.assert_not_awaited()

    async def test_queued_read_rechecks_after_page_lock_wait(self):
        await self.manager._page_operation_lock.acquire()
        with patch.object(desktop, "_probe_login_qr", new_callable=AsyncMock) as probe:
            request = asyncio.create_task(desktop._read_login_qr(1, TOKEN))
            await asyncio.sleep(0)
            self.manager.interaction.token = OTHER
            self.manager._page_operation_lock.release()
            with self.assertRaises(InteractionError):
                await request
            probe.assert_not_awaited()

    async def test_handoff_during_probe_never_returns_qr_bytes(self):
        async def probe(page):
            self.manager.interaction.token = OTHER
            return "qr", b"NEXT-USER-QR"
        with patch.object(desktop, "_probe_login_qr", side_effect=probe):
            with self.assertRaises(InteractionError):
                await desktop._read_login_qr(0, TOKEN)

    async def test_refresh_rechecks_capability_after_waiting_for_page_lock(self):
        await self.manager._page_operation_lock.acquire()
        with patch.object(self.manager, "_refresh_login_qr_locked", new_callable=AsyncMock) as refresh:
            request = asyncio.create_task(self.manager.refresh_login_qr(interaction_key=TOKEN))
            await asyncio.sleep(0)
            self.manager.interaction.token = OTHER
            self.manager._page_operation_lock.release()
            with self.assertRaises(InteractionError):
                await request
            refresh.assert_not_awaited()
        self.assertFalse(self.manager._page_operation_lock.locked())

    async def test_refresh_drops_result_if_context_is_released_during_navigation(self):
        async def refresh():
            self.manager.interaction.clear()
            return {"ok": True}
        with patch.object(self.manager, "_refresh_login_qr_locked", side_effect=refresh):
            with self.assertRaises(InteractionError):
                await self.manager.refresh_login_qr(interaction_key=TOKEN)


class QrServiceApiTests(unittest.TestCase):
    def test_read_requires_bearer_and_current_workspace_header(self):
        manager = desktop.LoginDesktopManager()
        manager.context = SimpleNamespace(_impl_obj=object())
        manager.page = SimpleNamespace(context=manager.context, url="https://creator.douyin.com/", is_closed=lambda: False)
        manager.interaction.context = manager.context
        manager.interaction.token = TOKEN
        manager._get_active_page = AsyncMock(return_value=manager.page)
        client = TestClient(desktop.app)
        with patch.object(desktop, "manager", manager), patch.object(desktop, "_load_api_token", return_value="internal-secret"), patch.object(desktop, "_probe_login_qr", new_callable=AsyncMock, return_value=("qr", b"OWN-QR")) as probe:
            self.assertEqual(401, client.get("/qr", headers={"X-SparkFlow-Workspace": TOKEN}).status_code)
            self.assertEqual(423, client.get("/qr", headers={"Authorization": "Bearer internal-secret"}).status_code)
            probe.assert_not_awaited()
            response = client.get("/qr", headers={"Authorization": "Bearer internal-secret", "X-SparkFlow-Workspace": TOKEN})
        self.assertEqual(200, response.status_code)
        self.assertEqual(b"OWN-QR", response.content)
        probe.assert_awaited_once_with(manager.page)

    def test_refresh_authenticates_and_forwards_internal_capability(self):
        client = TestClient(desktop.app)
        with patch.object(desktop, "_load_api_token", return_value="internal-secret"), patch.object(desktop.manager, "refresh_login_qr", new_callable=AsyncMock, return_value={"ok": True}) as refresh:
            self.assertEqual(401, client.post("/refresh-qr", json={"interaction_key": TOKEN}).status_code)
            response = client.post("/refresh-qr", json={"interaction_key": TOKEN}, headers={"Authorization": "Bearer internal-secret"})
        self.assertEqual(200, response.status_code)
        refresh.assert_awaited_once_with(interaction_key=TOKEN)
