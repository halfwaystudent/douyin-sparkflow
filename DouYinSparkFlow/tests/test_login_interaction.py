import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playwright.async_api import async_playwright

from core.login_interaction import LoginInteraction, InteractionError, allowed_login_url
from tests import test_shared_security as fixtures
from webui import app as app_module, login_lock


TOKEN = "ticket-" + "a" * 32


class InteractionPolicyTests(unittest.TestCase):
    def test_only_official_https_login_resources_are_allowed(self):
        self.assertTrue(allowed_login_url("https://creator.douyin.com/"))
        self.assertTrue(allowed_login_url("https://verify.snssdk.com/captcha"))
        for url in ("file:///run/login-api/token", "http://127.0.0.1:18090/export",
                    "http://169.254.169.254/", "https://localhost/", "https://douyin.com.evil.test/",
                    "https://user:secret@creator.douyin.com/", "https://creator.douyin.com:6080/"):
            with self.subTest(url=url):
                self.assertFalse(allowed_login_url(url))


class InteractionBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()
        self.context = await self.browser.new_context(viewport={"width": 320, "height": 240}, service_workers="block")
        await self.context.route("https://creator.douyin.com/**", lambda route: route.fulfill(
            content_type="text/html", body='<html><body><input id="password" type="password" style="position:absolute;left:20px;top:20px;width:180px;height:40px"><button style="position:absolute;left:20px;top:90px" onclick="document.body.dataset.done=1">确认</button></body></html>',
        ))
        self.page = await self.context.new_page()
        self.interaction = LoginInteraction()
        await self.interaction.bind(self.context, TOKEN)
        await self.page.goto("https://creator.douyin.com/")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    async def click(self, x, y):
        for phase in ("down", "up"):
            await self.interaction.act(TOKEN, self.page, {"type": "pointer", "phase": phase, "x": x, "y": y})

    async def test_page_input_and_click_work_without_desktop_access(self):
        await self.click(60, 40)
        await self.interaction.act(TOKEN, self.page, {"type": "text", "text": "private-test-password"})
        self.assertEqual("private-test-password", await self.page.locator("#password").input_value())
        frame = await self.interaction.screenshot(TOKEN, self.page)
        self.assertTrue(frame.startswith(b"\xff\xd8"))
        await self.click(50, 105)
        self.assertEqual("1", await self.page.locator("body").get_attribute("data-done"))

    async def test_browser_shortcuts_and_invalid_coordinates_are_rejected(self):
        for action in ({"type": "key", "key": "Control+L"}, {"type": "key", "key": "F12"},
                       {"type": "pointer", "phase": "down", "x": float("nan"), "y": 10},
                       {"type": "pointer", "phase": "down", "x": 400, "y": 10},
                       {"type": "evaluate", "text": "document.cookie"}):
            with self.subTest(action=action):
                with self.assertRaises(InteractionError):
                    await self.interaction.act(TOKEN, self.page, action)

    async def test_old_ticket_and_cleared_session_cannot_get_frames(self):
        await self.interaction.bind(self.context, "ticket-" + "b" * 32)
        with self.assertRaises(InteractionError):
            await self.interaction.screenshot(TOKEN, self.page)
        self.interaction.clear()
        with self.assertRaises(InteractionError):
            await self.interaction.screenshot("ticket-" + "b" * 32, self.page)

    async def test_handoff_during_screenshot_discards_the_frame(self):
        async def screenshot(**kwargs):
            self.interaction.clear()
            return b"\xff\xd8private-frame"
        with patch.object(self.page, "screenshot", side_effect=screenshot):
            with self.assertRaises(InteractionError):
                await self.interaction.screenshot(TOKEN, self.page)

    async def test_local_file_or_external_navigation_is_not_rendered(self):
        await self.page.goto("data:text/html,private-file-placeholder")
        with self.assertRaises(InteractionError):
            await self.interaction.screenshot(TOKEN, self.page)


class InteractionAccessTests(unittest.TestCase):
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
        self.bob = self.fixture.login("bob", "Second-secret-2026!")
        self.csrf = self.fixture.token(self.alice)
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True}):
            response = self.alice.post("/login-desktop/open", data={"csrf_token": self.csrf, "mode": "add"})
        self.assertEqual(200, response.status_code)
        self.ticket = response.json()["workspace"]["ticket"]

    def screen(self, client=None, **extra):
        return (client or self.alice).post("/login-desktop/interaction/screen", data={"csrf_token": self.csrf, "ticket": self.ticket, **extra})

    def test_normal_owner_can_use_page_interaction(self):
        data = {"ok": True, "image": base64.b64encode(b"\xff\xd8fixture").decode()}
        with patch.object(app_module, "call_login_desktop_json", return_value=(200, data)):
            response = self.screen()
        self.assertEqual(200, response.status_code)
        self.assertEqual("image/jpeg", response.headers["content-type"])
        self.assertEqual("no-store", response.headers["cache-control"])
        page = self.alice.get("/login-workspace").text
        self.assertIn("data-login-interaction", page)
        self.assertNotIn("<iframe", page)

    def test_csrf_other_users_and_stale_tickets_are_rejected_before_upstream(self):
        with patch.object(app_module, "call_login_desktop_json") as upstream:
            self.assertEqual(403, self.screen(csrf_token="wrong").status_code)
            self.assertEqual(423, self.screen(ticket="ticket-" + "x" * 32).status_code)
            response = self.bob.post("/login-desktop/interaction/screen", data={"csrf_token": self.fixture.token(self.bob), "ticket": self.ticket})
            self.assertEqual(423, response.status_code)
        upstream.assert_not_called()

    def test_release_while_upstream_is_loading_never_delivers_the_frame(self):
        def upstream(*args, **kwargs):
            login_lock.release(force=True)
            return 200, {"ok": True, "image": base64.b64encode(b"\xff\xd8private").decode()}
        with patch.object(app_module, "call_login_desktop_json", side_effect=upstream):
            response = self.screen()
        self.assertEqual(423, response.status_code)
        self.assertNotIn(b"private", response.content)
