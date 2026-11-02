"""A login export and its save must belong to the same workspace lease."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import login_desktop_server as desktop
from core.login_interaction import InteractionError
from tests import test_shared_security as fixtures
from utils import config
from webui import app as app_module, login_lock, sessions


TOKEN = "ticket-" + "a" * 32
OTHER_TOKEN = "ticket-" + "b" * 32
EXPORT = {"username": "new login", "cookies": [{"name": "sessionid", "value": "private-cookie", "domain": ".douyin.com", "path": "/"}]}
TRUSTED = {"uid": "333", "source": "creator_im_user_token", "version": 1}


class DesktopExportLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = desktop.LoginDesktopManager()
        self.context = object()
        self.page = SimpleNamespace(context=self.context, url="https://creator.douyin.com/", is_closed=lambda: False)
        self.manager.context = self.context
        self.manager.page = self.page
        self.manager.interaction.context = self.context
        self.manager.interaction.token = TOKEN
        self.manager._get_active_page = AsyncMock(return_value=self.page)
        self.manager.schedule_stop_after_export = Mock()

    async def test_bound_browser_requires_the_current_export_key(self):
        for token in ("", OTHER_TOKEN):
            with self.subTest(token=token), self.assertRaises(InteractionError) as raised:
                await self.manager.export(interaction_key=token)
            self.assertEqual(423, raised.exception.status)
        self.manager._get_active_page.assert_not_awaited()

    async def test_handoff_while_export_is_waiting_never_reads_new_browser(self):
        await self.manager._page_operation_lock.acquire()
        pending = asyncio.create_task(self.manager.export(interaction_key=TOKEN))
        await asyncio.sleep(0)
        self.manager.interaction.token = OTHER_TOKEN
        self.manager._page_operation_lock.release()
        with self.assertRaises(InteractionError):
            await pending
        self.manager._get_active_page.assert_not_awaited()

    async def test_handoff_during_export_discards_cookie_result(self):
        async def collect(*args, **kwargs):
            self.manager.interaction.clear()
            return deepcopy(EXPORT)
        with patch.object(desktop, "collect_login_result", side_effect=collect), patch.object(desktop, "collect_www_login_result", new_callable=AsyncMock) as fallback:
            with self.assertRaises(InteractionError):
                await self.manager.export(interaction_key=TOKEN)
            fallback.assert_not_awaited()
        self.manager.schedule_stop_after_export.assert_not_called()

    async def test_www_fallback_cannot_export_after_handoff(self):
        async def fallback(*args, **kwargs):
            self.manager.interaction.token = OTHER_TOKEN
            return deepcopy(EXPORT)
        with patch.object(desktop, "collect_login_result", new_callable=AsyncMock, side_effect=RuntimeError("creator unavailable")), patch.object(desktop, "collect_www_login_result", side_effect=fallback):
            with self.assertRaises(InteractionError):
                await self.manager.export(interaction_key=TOKEN)
        self.assertFalse(self.manager._page_operation_lock.locked())

    async def test_unbound_local_export_keeps_legacy_fallback(self):
        self.manager.interaction.clear()
        with patch.object(desktop, "collect_login_result", new_callable=AsyncMock, side_effect=RuntimeError("creator unavailable")), patch.object(desktop, "collect_www_login_result", new_callable=AsyncMock, return_value=deepcopy(EXPORT)):
            result = await self.manager.export()
        self.assertEqual(EXPORT, result)
        self.manager.schedule_stop_after_export.assert_called_once()

    async def test_real_key_exports_without_changing_the_key(self):
        with patch.object(desktop, "collect_login_result", new_callable=AsyncMock, return_value=deepcopy(EXPORT)):
            self.assertEqual(EXPORT, await self.manager.export(interaction_key=TOKEN))
        self.assertTrue(self.manager.interaction.owns(TOKEN))

    async def test_legacy_request_cannot_finish_after_browser_becomes_shared(self):
        self.manager.interaction.clear()
        async def collect(*args, **kwargs):
            self.manager.interaction.context = self.context
            self.manager.interaction.token = TOKEN
            return deepcopy(EXPORT)
        with patch.object(desktop, "collect_login_result", side_effect=collect):
            with self.assertRaises(InteractionError):
                await self.manager.export()


class DesktopExportApiTests(unittest.TestCase):
    def test_internal_export_requires_api_auth_and_forwards_the_lease(self):
        client = TestClient(desktop.app)
        with patch.object(desktop, "_load_api_token", return_value="internal-secret"), patch.object(desktop.manager, "export", new_callable=AsyncMock, return_value=deepcopy(EXPORT)) as export:
            self.assertEqual(401, client.post("/export", json={"interaction_key": TOKEN}).status_code)
            response = client.post("/export", json={"interaction_key": TOKEN}, headers={"authorization": "Bearer internal-secret"})
            self.assertEqual(200, response.status_code)
            export.assert_awaited_once_with(interaction_key=TOKEN)


class LoginSaveLeaseTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        lock = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
        lock.start()
        self.addCleanup(lock.stop)
        self.alice = fixture.login()
        self.csrf = fixture.token(self.alice)
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True}):
            opened = self.alice.post("/login-desktop/open", data={"csrf_token": self.csrf, "mode": "add"})
        self.assertEqual(200, opened.status_code)
        self.ticket = opened.json()["workspace"]["ticket"]
        self.before = config.users_data_path().read_bytes()

    def post(self, **fields):
        return self.alice.post("/login-desktop/save", data={"csrf_token": self.csrf, "ticket": self.ticket, **fields})

    def handoff(self):
        login_lock.release(force=True)
        return login_lock.request_workspace(username="bob", session_id="bob-new-session", mode="add")["request"]

    def unchanged(self):
        self.assertEqual(self.before, config.users_data_path().read_bytes())

    def test_missing_foreign_and_non_ascii_tickets_never_reach_export(self):
        with patch.object(app_module, "call_login_desktop") as export:
            for ticket in ("", OTHER_TOKEN, "无效凭据"):
                with self.subTest(ticket=ticket):
                    self.assertEqual(423, self.post(ticket=ticket).status_code)
            export.assert_not_called()
        self.unchanged()

    def test_handoff_during_export_keeps_next_users_workspace_and_cookies_private(self):
        next_lease = {}
        def export(*args, **kwargs):
            self.assertEqual({"interaction_key": self.ticket}, kwargs["payload"])
            next_lease.update(self.handoff())
            return {"ok": True, "result": deepcopy(EXPORT)}
        with patch.object(app_module, "call_login_desktop", side_effect=export) as upstream, patch.object(app_module.account_identity, "fetch_trusted_identity") as trusted:
            response = self.post()
        self.assertEqual(423, response.status_code)
        self.assertNotIn("private-cookie", response.text)
        self.assertEqual(next_lease["ticket"], login_lock.get_lock()["ticket"])
        self.assertEqual(1, upstream.call_count)
        trusted.assert_not_called()
        self.unchanged()

    def test_handoff_during_identity_check_cannot_write_or_release_next_lease(self):
        def trusted(*args, **kwargs):
            self.handoff()
            return TRUSTED
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True, "result": deepcopy(EXPORT)}) as upstream, patch.object(app_module.account_identity, "fetch_trusted_identity", side_effect=trusted), patch.object(app_module, "begin_login_release") as release:
            response = self.post()
        self.assertEqual(423, response.status_code)
        self.assertEqual("bob", login_lock.get_lock()["username"])
        release.assert_not_called()
        self.assertEqual(1, upstream.call_count)
        self.unchanged()

    def test_logout_during_identity_check_discards_the_save(self):
        session_id = login_lock.get_lock()["session_id"]
        def trusted(*args, **kwargs):
            sessions.revoke(session_id, time.time() + 3600)
            return TRUSTED
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True, "result": deepcopy(EXPORT)}), patch.object(app_module.account_identity, "fetch_trusted_identity", side_effect=trusted):
            response = self.post()
        self.assertEqual(423, response.status_code)
        self.unchanged()

    def test_last_moment_handoff_is_rechecked_inside_account_write(self):
        original_update = app_module.update_user_data
        def update(mutator, **kwargs):
            def raced(accounts):
                self.handoff()
                return mutator(accounts)
            return original_update(raced, **kwargs)
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True, "result": deepcopy(EXPORT)}), patch.object(app_module.account_identity, "fetch_trusted_identity", return_value=TRUSTED), patch.object(app_module, "update_user_data", side_effect=update):
            response = self.post()
        self.assertEqual(423, response.status_code)
        self.unchanged()

    def test_simultaneous_duplicate_saves_only_export_and_create_once(self):
        started, resume = threading.Event(), threading.Event()
        exports = []
        def upstream(path, **kwargs):
            if path == "/export":
                exports.append(path)
                started.set()
                if not resume.wait(timeout=5):
                    raise RuntimeError("test did not release export")
                return {"ok": True, "result": deepcopy(EXPORT)}
            return {"ok": True}
        with patch.object(app_module, "call_login_desktop", side_effect=upstream), patch.object(app_module.account_identity, "fetch_trusted_identity", return_value=TRUSTED), patch.object(app_module, "task_run_lock_status", return_value={"running": True}), ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.post)
            try:
                self.assertTrue(started.wait(timeout=3))
                duplicate = self.post()
                self.assertEqual(409, duplicate.status_code, duplicate.text)
                self.assertEqual("login_save_in_progress", duplicate.json()["category"])
            finally:
                resume.set()
            self.assertEqual(200, first.result(timeout=5).status_code)
            self.assertEqual(423, self.post().status_code)
        self.assertEqual(1, len(exports))
        self.assertEqual(1, len([row for row in config.get_userData(force_reload=True) if row.get("platform_uid") == "333"]))
