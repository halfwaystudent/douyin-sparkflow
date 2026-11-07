"""Recover the isolated login browser after renderer failures."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import login_desktop_server as desktop


TOKEN = "ticket-" + "a" * 32


class LoginDesktopClosedContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_recreated_by_status_cannot_skip_rebinding_on_open(self):
        manager = desktop.LoginDesktopManager()
        old_context = object()
        manager.interaction.context = old_context
        manager.interaction.token = TOKEN
        replacement = SimpleNamespace(
            _impl_obj=object(), pages=[], route=AsyncMock(),
            route_web_socket=AsyncMock(), on=lambda *args: None,
        )
        page = SimpleNamespace(context=replacement, is_closed=lambda: False,
                               url="https://creator.douyin.com/")
        manager.context, manager.page = replacement, page

        async def stop(**kwargs):
            manager.context = manager.page = None
            manager.interaction.clear()

        async def ensure():
            manager.context, manager.page = replacement, page
            return {"url": page.url, "state": "login_form", "reset": False, "logged_in": False}

        manager.stop = AsyncMock(side_effect=stop)
        manager._ensure_login_form_locked = AsyncMock(side_effect=ensure)
        await manager.open_login(interaction_key=TOKEN)
        manager.interaction.check(TOKEN, manager.page)
        self.assertIs(replacement, manager.interaction.context)

    async def test_closed_context_is_not_reused_for_qr_read(self):
        manager = desktop.LoginDesktopManager()
        closed = asyncio.get_running_loop().create_future()
        closed.set_result(None)
        context = SimpleNamespace(
            _impl_obj=SimpleNamespace(_closed_future=closed, _closing_or_closed=True)
        )
        manager.context = context
        manager.page = object()
        manager.interaction.context = context
        manager.interaction.token = TOKEN
        manager._get_active_page = AsyncMock()

        with patch.object(desktop, "manager", manager):
            response = await desktop._read_login_qr(0, TOKEN)

        self.assertEqual(202, response.status_code)
        self.assertIn(b'"code":"qr_not_ready"', response.body)
        manager._get_active_page.assert_not_awaited()
        self.assertIsNone(manager.context)
        self.assertIsNone(manager.page)
        self.assertTrue(manager.interaction.owns(TOKEN))

    async def test_open_login_does_not_return_a_closed_context_as_interactive(self):
        manager = desktop.LoginDesktopManager()
        closed = asyncio.get_running_loop().create_future()
        closed.set_result(None)
        context = SimpleNamespace(
            _impl_obj=SimpleNamespace(_closed_future=closed, _closing_or_closed=True)
        )
        manager.context = context
        manager.page = SimpleNamespace(is_closed=lambda: False, url="https://creator.douyin.com/")
        manager.interaction.context = context
        manager.interaction.token = TOKEN
        ready = {"url": "https://creator.douyin.com/", "state": "login_form", "reset": False, "logged_in": False}
        replacement_context = object()

        async def ensure_login_form():
            manager.context = replacement_context
            return ready

        manager._ensure_login_form_locked = AsyncMock(side_effect=ensure_login_form)
        manager.interaction.bind = AsyncMock()

        result = await manager.open_login(interaction_key=TOKEN)

        self.assertEqual("login_form", result["state"])
        manager._ensure_login_form_locked.assert_awaited_once()
        self.assertIs(manager.context, replacement_context)
        manager.interaction.bind.assert_awaited_once_with(replacement_context, TOKEN)


class LoginDesktopRecoveryApiTests(unittest.TestCase):
    def test_open_login_navigation_timeout_is_retryable(self):
        client = TestClient(desktop.app, raise_server_exceptions=False)

        async def navigation_timeout(*args, **kwargs):
            raise PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded")

        with patch.object(desktop.manager, "open_login", side_effect=navigation_timeout):
            response = client.post("/open-login")

        self.assertEqual(503, response.status_code)
        self.assertEqual("login_navigation_timeout", response.json()["state"])
        self.assertTrue(response.json()["retryable"])
        self.assertIn("重试", response.json()["message"])
