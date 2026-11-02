import asyncio
from pathlib import Path
import unittest
from unittest.mock import patch

from playwright.async_api import async_playwright

from tests import test_shared_security as fixtures
from webui import app as app_module, registration


LARGE_ASSETS = ("/static/app.css", "/static/app.js", "/static/lucide.min.js")


class AuthFirstPaintTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        registration.configure("public", True, 100)
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()
        self.context = await self.browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="light")
        self.page = await self.context.new_page()
        self.requests = []
        self.block_scripts = False
        self.release = asyncio.Event()

        async def respond(route):
            request = route.request
            self.requests.append(request.url)
            if not request.url.startswith("https://testserver/"):
                return await route.abort()
            if any(asset in request.url for asset in LARGE_ASSETS):
                return await route.abort()
            if "/static/" in request.url and self.block_scripts:
                await self.release.wait()
            response = self.fixture.client.request(
                request.method, request.url, content=request.post_data_buffer,
                headers={key: value for key, value in request.headers.items() if key != "accept-encoding"},
                follow_redirects=False,
            )
            headers = {key: value for key, value in response.headers.items() if key not in {"content-length", "content-encoding"}}
            await route.fulfill(status=response.status_code, headers=headers, body=response.content)

        await self.page.route("**/*", respond)

    async def asyncTearDown(self):
        self.release.set()
        await self.browser.close()
        await self.runtime.stop()

    async def test_login_is_visible_styled_and_can_submit_while_all_scripts_are_delayed(self):
        import os
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from fastapi.testclient import TestClient
        from tests.test_webui_pages import _snapshot

        # Native 303 navigation must use a real loopback origin: the browser's
        # redirected request does not re-enter Playwright's route callback.
        release_scripts = threading.Event()
        requested = []
        with patch.dict(os.environ, {"SPARKFLOW_ALLOWED_HOSTS": "127.0.0.1"}):
            application = app_module.create_app()
        snapshot_patch = patch.object(app_module, "get_ops_snapshot", side_effect=lambda **_: _snapshot())
        snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        client = None
        origin = ""

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requested.append(("GET", self.path))
                if self.path.startswith("/static/"):
                    release_scripts.wait()
                self.proxy()

            def do_POST(self):
                requested.append(("POST", self.path))
                self.proxy()

            def proxy(self):
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                response = client.request(self.command, origin + self.path, content=body,
                                          headers=dict(self.headers), follow_redirects=False)
                self.send_response(response.status_code)
                for name, value in response.headers.multi_items():
                    if name.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection"}:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                try:
                    self.wfile.write(response.content)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = False
        origin = f"http://127.0.0.1:{server.server_port}"
        client = TestClient(application, base_url=origin)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        page = await self.browser.new_page(viewport={"width": 390, "height": 844})
        try:
            await page.goto(origin + "/login", wait_until="commit")
            await page.locator('form[action="/login"]').wait_for(state="visible")
            self.assertTrue(await page.locator(".brand-mark svg").is_visible())
            username = page.locator('[name="username"]')
            await username.fill("alice")
            await page.locator('[name="password"]').fill("First-secret-2026!")
            self.assertEqual(16, await username.evaluate("element=>parseFloat(getComputedStyle(element).fontSize)"))
            self.assertEqual(0, await page.locator('link[rel="stylesheet"]').count())
            self.assertFalse(any(any(asset in path for asset in LARGE_ASSETS) for _, path in requested))
            self.assertFalse(any("passkeys.js" in path for _, path in requested))
            await page.locator('form[action="/login"] button[type="submit"]').click(no_wait_after=True)
            await page.wait_for_url(origin + "/", wait_until="commit", timeout=5000)
            self.assertIn(("POST", "/login"), requested)
            self.assertFalse(release_scripts.is_set())
            authenticated = client.get("/accounts/binding-status", follow_redirects=False)
            self.assertEqual(200, authenticated.status_code)
            self.assertEqual(["a1"], [account["account_ref"] for account in authenticated.json()["accounts"]])
        finally:
            release_scripts.set()
            await page.close()
            await asyncio.to_thread(server.shutdown)
            await asyncio.to_thread(server.server_close)
            thread.join(timeout=2)
            client.close()

    async def test_registration_form_is_ready_without_external_style_or_script(self):
        self.block_scripts = True
        await self.page.goto("https://testserver/register", wait_until="commit")
        await self.page.locator('form[action="/register"]').wait_for(state="visible")
        self.assertEqual("same-origin", await self.page.locator('meta[name="referrer"]').get_attribute("content"))
        self.assertEqual(0, await self.page.locator('link[rel="stylesheet"]').count())
        for name in ("username", "password", "confirm_password"):
            self.assertTrue(await self.page.locator(f'[name="{name}"]').is_visible())
        self.assertGreaterEqual(await self.page.locator('[name="username"]').evaluate("element=>element.getBoundingClientRect().height"), 44)
        self.assertFalse(any(any(asset in url for asset in LARGE_ASSETS) for url in self.requests))

    async def test_small_auth_script_keeps_theme_password_and_invitation_behaviors(self):
        await self.page.emulate_media(color_scheme="dark")
        await self.page.goto("https://testserver/login")
        self.assertEqual("dark", await self.page.locator("html").get_attribute("data-theme"))
        password = self.page.locator('[name="password"]')
        await password.fill("test-private-password")
        await self.page.get_by_role("button", name="显示密码", exact=True).click()
        self.assertEqual("text", await password.get_attribute("type"))
        self.assertEqual("test-private-password", await password.input_value())
        await self.page.locator("[data-theme-toggle]").click()
        self.assertEqual("light", await self.page.locator("html").get_attribute("data-theme"))
        self.assertEqual("light", await self.page.evaluate("localStorage.getItem('sparkflow-theme')"))
        await self.page.goto("https://testserver/register#invite=synthetic-invitation")
        self.assertEqual("synthetic-invitation", await self.page.locator('[name="invitation_token"]').input_value())
        self.assertNotIn("#", self.page.url)
        self.assertEqual("light", await self.page.locator("html").get_attribute("data-theme"))
        self.assertEqual(2, await self.page.locator(".password-toggle").count())
        self.assertFalse(any(any(asset in url for asset in LARGE_ASSETS) for url in self.requests))

    async def test_auth_pages_fit_phone_and_desktop_in_both_themes(self):
        for scheme in ("light", "dark"):
            await self.page.emulate_media(color_scheme=scheme)
            for width, height in ((320, 740), (390, 844), (844, 390), (1280, 800)):
                await self.page.set_viewport_size({"width": width, "height": height})
                for path in ("/login", "/register"):
                    with self.subTest(scheme=scheme, width=width, path=path):
                        await self.page.goto("https://testserver" + path)
                        self.assertEqual(scheme, await self.page.locator("html").get_attribute("data-theme"))
                        self.assertLessEqual(await self.page.evaluate("document.documentElement.scrollWidth"), width + 1)
                        if width <= 430:
                            self.assertEqual(0, await self.page.locator('input:not([type="hidden"])').evaluate_all("items=>items.filter(element=>parseFloat(getComputedStyle(element).fontSize)<16).length"))
        self.assertFalse(any(any(asset in url for asset in LARGE_ASSETS) for url in self.requests))

    async def test_bootstrap_uses_same_inline_shell_and_skips_passkey_script(self):
        with patch.object(app_module, "is_bootstrapped", return_value=False):
            await self.page.goto("https://testserver/login")
        self.assertTrue(await self.page.locator('form[action="/bootstrap"]').is_visible())
        self.assertTrue(await self.page.locator('[name="bootstrap_token"]').is_visible())
        self.assertFalse(any("passkeys.js" in url or any(asset in url for asset in LARGE_ASSETS) for url in self.requests))


class InlineIconLicenseTests(unittest.TestCase):
    def test_inline_icon_paths_match_the_vendored_source_and_keep_licenses(self):
        import json
        import subprocess
        source = Path(app_module.STATIC_DIR) / "lucide.min.js"
        script = "const fs=require('fs'),vm=require('vm'),c={};vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),c);console.log(JSON.stringify(Object.fromEntries(['Flame','CalendarClock','BadgeCheck','ScanQrCode','Moon','Sun','ShieldCheck','LogIn','Fingerprint'].map(name=>[name,c.lucide[name]]))));"
        result = json.loads(subprocess.check_output(["node", "-e", script, str(source)], text=True))
        template = (Path(app_module.TEMPLATES_DIR) / "_auth-icons.html").read_text()
        for nodes in result.values():
            for _tag, attributes in nodes:
                if "d" in attributes:
                    self.assertIn(attributes["d"], template)
        license_text = (Path(app_module.TEMPLATES_DIR) / "_auth-icon-license.html").read_text()
        self.assertIn("Copyright (c) 2026 Lucide Icons and Contributors", license_text)
        self.assertIn("Copyright (c) 2013-present Cole Bemis", license_text)
        self.assertIn("Permission to use, copy, modify", license_text)
