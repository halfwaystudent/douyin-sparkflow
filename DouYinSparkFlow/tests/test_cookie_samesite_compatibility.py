import unittest

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from core import cookies


class CookieSameSiteCompatibilityTests(unittest.TestCase):
    def test_known_export_spellings_normalize_to_playwright_enums(self):
        expected = {
            "lax": "Lax",
            "LAX": "Lax",
            "strict": "Strict",
            "StRiCt": "Strict",
            "none": "None",
            "No_restriction": "None",
        }

        for exported, playwright_value in expected.items():
            with self.subTest(exported=exported):
                normalized = cookies.normalize_cookie(
                    {"name": "sessionid", "value": "synthetic", "sameSite": exported}
                )
                self.assertEqual(playwright_value, normalized["sameSite"])

    def test_missing_same_site_keeps_existing_omission_behavior(self):
        normalized = cookies.normalize_cookie({"name": "sessionid", "value": "synthetic"})

        self.assertNotIn("sameSite", normalized)

    def test_unspecified_extension_value_preserves_no_same_site_attribute(self):
        for value in ("unspecified", "UnSpecified", " unspecified "):
            with self.subTest(value=value):
                normalized = cookies.normalize_cookie({"name": "sessionid", "value": "synthetic", "sameSite": value})
                self.assertNotIn("sameSite", normalized)

    def test_unknown_same_site_is_rejected_as_cookie_format_error(self):
        with self.assertRaises(cookies.CookieParseError) as caught:
            cookies.normalize_cookie(
                {"name": "sessionid", "value": "synthetic", "sameSite": "sometimes"}
            )

        self.assertEqual("cookie_format_invalid", caught.exception.category)


class ChromiumCookieSameSiteCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_no_restriction_is_accepted_after_normalization_without_navigation(self):
        runtime = await async_playwright().start()
        browser = await runtime.chromium.launch()
        context = await browser.new_context()
        await context.route("**/*", lambda route: route.abort())
        try:
            legacy = {
                "name": "sessionid",
                "value": "synthetic",
                "domain": "example.com",
                "path": "/",
                "secure": True,
                "sameSite": "No_restriction",
            }
            with self.assertRaises(PlaywrightError):
                await context.add_cookies([legacy])

            normalized = cookies.normalize_cookie(legacy)
            await context.add_cookies([normalized])
            stored = await context.cookies("https://example.com")
            self.assertEqual("None", stored[0]["sameSite"])
        finally:
            await context.close()
            await browser.close()
            await runtime.stop()
