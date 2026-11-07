from pathlib import Path
from tempfile import TemporaryDirectory
import gzip
import re
import unittest

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount
from playwright.async_api import async_playwright

from tests import test_shared_security as fixtures
from webui import app as app_module
from webui.static_assets import AssetVersions, public_assets


class AssetDeliveryTests(unittest.TestCase):
    def test_versioned_assets_are_immutable_but_old_urls_must_revalidate(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "app.js"
            source.write_text("first content")
            versions = AssetVersions(directory)
            client = TestClient(Starlette(routes=[Mount("/static", public_assets(directory, versions))]))
            first_url = versions.url("app.js")
            first = client.get(first_url)
            self.assertEqual("first content", first.text)
            self.assertIn("immutable", first.headers["cache-control"])
            self.assertEqual(304, client.get(first_url, headers={"if-none-match": first.headers["etag"]}).status_code)
            source.write_text("updated content")
            second_url = versions.url("app.js")
            self.assertNotEqual(first_url, second_url)
            self.assertEqual("updated content", client.get(second_url).text)
            self.assertIn("immutable", client.get(second_url).headers["cache-control"])
            for url in (first_url, "/static/app.js", first_url + "&v=extra"):
                response = client.get(url)
                self.assertNotIn("immutable", response.headers["cache-control"])
                self.assertIn("max-age=0", response.headers["cache-control"])

    def test_private_pages_never_receive_immutable_cache_policy(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for client, url in ((fixture.client, "/login"), (fixture.login(), "/accounts")):
            response = client.get(url + "?v=synthetic")
            self.assertEqual(200, response.status_code)
            self.assertEqual("no-store", response.headers["cache-control"])
            self.assertNotIn("immutable", response.headers["cache-control"])
        authenticated_asset = client.get(app_module.asset_versions.url("icons.js"))
        self.assertEqual(200, authenticated_asset.status_code)
        self.assertNotIn("set-cookie", authenticated_asset.headers)

    def test_page_scripts_only_include_features_used_on_that_page(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        client = fixture.login()
        for url, editing, passkeys in (("/", False, False), ("/accounts", True, False), ("/preferences/streak", True, False), ("/settings", False, True)):
            response = client.get(url)
            self.assertEqual(200, response.status_code)
            scripts = re.findall(r'<script[^>]+src="([^"]+)"', response.text)
            self.assertTrue(any("icons.js?v=" in script for script in scripts))
            self.assertFalse(any("lucide.min.js" in script for script in scripts))
            self.assertEqual(editing, any("editing.js" in script for script in scripts))
            self.assertEqual(passkeys, any("passkeys.js" in script for script in scripts))
            self.assertEqual(url == "/accounts", any("account-pickers.js" in script for script in scripts))
            self.assertFalse(any("login-workspace.js" in script for script in scripts))
            for script in scripts:
                self.assertRegex(script, r"\?v=[a-f0-9]{16}$")

    def test_generated_page_scripts_match_the_tested_source(self):
        from scripts.build_web_assets import split_scripts
        directory = Path(app_module.STATIC_DIR)
        source = (directory / "app.js").read_text(encoding="utf-8")
        for name, content in split_scripts(source).items():
            self.assertEqual(content, (directory / name).read_text(encoding="utf-8"), name)

    def test_created_invitation_keeps_its_copy_handler(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        client = fixture.login("owner", "Owner-secret-2026!")
        response = client.post("/admin/invitations/create", data={"csrf_token": fixture.token(client), "hours": "24"})
        self.assertEqual(200, response.status_code)
        self.assertIn("data-copy-invitation", response.text)
        self.assertIn(app_module.asset_versions.url("auth-forms.js"), response.text)

    def test_accounts_and_binding_pages_do_not_read_unrelated_operation_history(self):
        from unittest.mock import patch
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        client = fixture.login()
        with patch.object(app_module, "get_ops_snapshot", side_effect=AssertionError("Unrelated operation history")):
            for url in ("/accounts", "/login-workspace"):
                response = client.get(url)
                self.assertEqual(200, response.status_code, url)

    def test_icon_subset_is_small_enough_to_avoid_shipping_the_entire_library(self):
        directory = Path(app_module.STATIC_DIR)
        full = gzip.compress((directory / "lucide.min.js").read_bytes(), compresslevel=5)
        subset = gzip.compress((directory / "icons.js").read_bytes(), compresslevel=5)
        self.assertLess(len(subset), len(full) // 10)


class IconSubsetBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_used_icon_renders_with_accessibility_attributes_and_keeps_button_action(self):
        runtime = await async_playwright().start()
        browser = await runtime.chromium.launch()
        page = await browser.new_page()
        try:
            names = {"pause", "play", "loader-circle", "info"}
            for template in (Path(app_module.TEMPLATES_DIR)).glob("*.html"):
                names.update(re.findall(r'data-lucide="([a-z0-9-]+)"', template.read_text()))
            await page.set_content('<button onclick="window.clicked=true"><i data-lucide="flame" class="custom"></i></button>' + "".join(f'<i data-lucide="{name}"></i>' for name in names))
            await page.add_script_tag(path=str(Path(app_module.STATIC_DIR) / "icons.js"))
            await page.evaluate('lucide.createIcons({attrs:{"aria-hidden":"true"}})')
            self.assertEqual(0, await page.locator("i[data-lucide]").count())
            self.assertEqual(len(names) + 1, await page.locator("svg.lucide[aria-hidden=true]").count())
            self.assertEqual(1, await page.locator("svg.custom.lucide-flame").count())
            await page.locator("button svg").click()
            self.assertTrue(await page.evaluate("clicked"))
            await page.evaluate('document.body.insertAdjacentHTML("beforeend", \'<i data-lucide="check"></i>\');lucide.createIcons({attrs:{"aria-hidden":"true"}})')
            self.assertEqual(0, await page.locator("i[data-lucide]").count())
            self.assertEqual(len(names) + 2, await page.locator("svg.lucide").count())
        finally:
            await browser.close()
            await runtime.stop()
