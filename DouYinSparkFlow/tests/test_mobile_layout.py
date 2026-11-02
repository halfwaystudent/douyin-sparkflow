"""Offline layout checks against rendered console pages."""

from copy import deepcopy
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from tests import test_webui_pages as fixtures
from webui import app as app_module


class MobileLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = sync_playwright().start()
        try:
            cls.browser = cls.runtime.chromium.launch()
        except Exception:
            cls.runtime.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def test_console_fits_small_phones_tablets_and_desktop(self):
        fixture = fixtures.RedesignedPagesTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        accounts = deepcopy(fixtures.ACCOUNTS)
        accounts[0]["username"] = "很长的账号昵称_" + "long-account-name" * 5
        accounts[0]["cookies"] = [{"name": "sessionid", "value": "offline-fixture"}]
        web_users = [{"username": "shared-user-" + "x" * 45, "enabled": True, "account_refs": ["ref-1"]}]
        with (
            patch.object(fixtures, "ACCOUNTS", accounts),
            patch.object(app_module, "get_userData", return_value=accounts),
            patch.object(app_module, "get_web_users", return_value=web_users),
            patch.object(app_module, "get_overview_snapshot", return_value={}),
            patch.object(app_module.registration, "policy", return_value={"mode": "public", "approval_required": True, "max_users": 100}),
        ):
            for principal in (fixtures.ADMIN, fixtures.FRIEND, None):
                with (
                    patch.object(app_module, "current_principal", return_value=principal),
                    patch.object(app_module, "current_user", return_value=(principal or {}).get("username")),
                ):
                    page = self.browser.new_page()
                    self.addCleanup(page.close)

                    def respond(route):
                        response = fixture.client.get(route.request.url.replace("http://sparkflow.test", ""), follow_redirects=False)
                        headers = {key: value for key, value in response.headers.items()
                                   if key not in {"content-length", "content-encoding"}}
                        route.fulfill(status=response.status_code, headers=headers, body=response.content)

                    page.route("**/*", lambda route: respond(route) if route.request.url.startswith("http://sparkflow.test/") else route.abort())
                    paths = ("/", "/accounts", "/settings", "/preferences/streak", "/login-workspace", "/ops/send-console", "/setup?step=1", "/setup?step=2", "/setup?step=3", "/setup?step=4") if principal else ("/login", "/register")
                    if principal and principal["role"] == "admin":
                        paths += ("/settings?_anchor=users", "/settings?_anchor=system", "/settings?_anchor=notify", "/settings?_anchor=registration")
                    for width, height in ((320, 740), (360, 800), (390, 844), (430, 932), (768, 1024), (1280, 800), (844, 390)):
                        page.set_viewport_size({"width": width, "height": height})
                        for path in paths:
                            with self.subTest(role=(principal or {}).get("role", "guest"), width=width, path=path):
                                page.goto("http://sparkflow.test" + path, wait_until="load")
                                page.locator("details").evaluate_all("items => items.forEach(item => item.open = true)")
                                measurements = page.evaluate("""() => ({
                                    width: innerWidth,
                                    scroll: document.documentElement.scrollWidth,
                                    overflowing: [...document.querySelectorAll('body *')].filter(el => {
                                        const r = el.getBoundingClientRect();
                                        return r.width && r.right > innerWidth + 1;
                                    }).slice(0, 8).map(el => el.className)
                                })""")
                                self.assertLessEqual(measurements["scroll"], width + 1, measurements)
                                if principal and principal["role"] == "admin" and path.startswith("/settings"):
                                    self.assertEqual(1, page.locator("section.settings-section:visible").count())
                                if width <= 430:
                                    small_inputs = page.locator('input:not([type="hidden"]):not([type="checkbox"]), textarea, select').evaluate_all(
                                        "items => items.filter(el => parseFloat(getComputedStyle(el).fontSize) < 16).length"
                                    )
                                    self.assertEqual(0, small_inputs)
                                screenshots = os.getenv("SPARKFLOW_SCREENSHOT_DIR")
                                if screenshots and width == 390 and path in {"/accounts", "/settings", "/login", "/register", "/setup?step=1", "/settings?_anchor=users", "/settings?_anchor=registration"}:
                                    output = Path(screenshots)
                                    output.mkdir(parents=True, exist_ok=True)
                                    label = path.strip('/').replace('?', '-').replace('=', '-').replace('_anchor', 'section')
                                    page.screenshot(path=str(output / f"{(principal or {}).get('role', 'guest')}-{label}.png"), full_page=True)
                    page.close()

                    if principal is None:
                        page = self.browser.new_page()
                        page.route("**/*", lambda route: respond(route) if route.request.url.startswith("http://sparkflow.test/") else route.abort())
                        page.goto("http://sparkflow.test/register#invite=test-invitation-token")
                        self.assertEqual("test-invitation-token", page.locator('[name="invitation_token"]').input_value())
                        self.assertNotIn("#", page.url)
                        states = page.evaluate("""() => {
                            const form = document.querySelector('[data-auth-form]');
                            const first = new Event('submit', {bubbles:true, cancelable:true});
                            const second = new Event('submit', {bubbles:true, cancelable:true});
                            form.dispatchEvent(first); form.dispatchEvent(second);
                            const disabled = form.querySelector('button[type=submit]').disabled;
                            window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));
                            return [first.defaultPrevented, second.defaultPrevented, disabled, form.querySelector('button[type=submit]').disabled];
                        }""")
                        self.assertEqual([False, True, True, False], states)
                        page.close()


if __name__ == "__main__":
    unittest.main()
