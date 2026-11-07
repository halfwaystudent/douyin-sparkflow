import unittest

from playwright.async_api import async_playwright

from tests import test_shared_security as fixtures
from utils import config
from webui import app as app_module


SOURCE_REF = "source-ref"
TARGET_REF = "target-ref"
THIRD_REF = "third-ref"
SOURCE_GENERATION = "source-generation"
TARGET_GENERATION = "target-generation"
THIRD_GENERATION = "third-generation"


def duplicate_account(account_ref, unique_id, generation, targets):
    return {
        "account_ref": account_ref,
        "unique_id": unique_id,
        "username": "Duplicate",
        "login_generation": generation,
        "cookies": [],
        "targets": targets,
        "enabled": True,
    }


class MergeGenerationBackendTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.login("owner", "Owner-secret-2026!")
        self.accounts = [
            duplicate_account(SOURCE_REF, "100", SOURCE_GENERATION, ["from-source"]),
            duplicate_account(TARGET_REF, "200", TARGET_GENERATION, ["from-target"]),
        ]
        previous = config.userData
        config.userData = None
        self.addCleanup(setattr, config, "userData", previous)
        config._save_json_file(config.users_data_path(), self.accounts)

    def test_matching_target_generation_allows_duplicate_merge_and_preserves_target(self):
        response = self.client.post(
            "/accounts/100/merge-into",
            data={
                "csrf_token": self.fixture.token(self.client),
                "account_ref": SOURCE_REF,
                "login_generation": SOURCE_GENERATION,
                "target_account_ref": TARGET_REF,
                "target_login_generation": TARGET_GENERATION,
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        rows = config.get_userData(force_reload=True)
        self.assertEqual([TARGET_REF], [row["account_ref"] for row in rows])
        self.assertEqual(["from-target", "from-source"], rows[0]["targets"])
        self.assertEqual(TARGET_GENERATION, rows[0]["login_generation"])


class MergeGenerationRenderedFormBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addAsyncCleanup(self._cleanup_fixture)
        self.client = self.fixture.login("owner", "Owner-secret-2026!")
        accounts = [
            duplicate_account(SOURCE_REF, "100", SOURCE_GENERATION, []),
            duplicate_account(TARGET_REF, "200", TARGET_GENERATION, []),
            duplicate_account(THIRD_REF, "300", THIRD_GENERATION, []),
        ]
        config.userData = None
        config._save_json_file(config.users_data_path(), accounts)
        rendered = self.client.get("/accounts")
        self.assertEqual(200, rendered.status_code)
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()
        self.context = await self.browser.new_context()
        await self.context.route("**/*", lambda route: route.abort())
        self.page = await self.context.new_page()
        await self.page.set_content(rendered.text)
        await self.page.add_script_tag(path=str(app_module.STATIC_DIR / "editing.js"))

    async def _cleanup_fixture(self):
        self.fixture.doCleanups()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.runtime.stop()

    async def test_served_editing_controller_scopes_selected_target_generation_to_its_merge_form(self):
        source = self.page.locator(f'form[data-merge-duplicate-form][action="/accounts/100/merge-into"]')
        target = self.page.locator(f'form[data-merge-duplicate-form][action="/accounts/200/merge-into"]')
        source_generation = source.locator('[name="target_login_generation"]')
        target_generation = target.locator('[name="target_login_generation"]')

        await source.evaluate("form => { form.closest('details').open = true; }")
        await target.evaluate("form => { form.closest('details').open = true; }")
        self.assertEqual(TARGET_GENERATION, await source_generation.input_value())
        self.assertEqual(SOURCE_GENERATION, await target_generation.input_value())
        await source.locator('[name="target_account_ref"]').select_option(THIRD_REF)
        self.assertEqual(THIRD_GENERATION, await source_generation.input_value())
        self.assertEqual(SOURCE_GENERATION, await target_generation.input_value())

        submitted = []
        await self.page.evaluate(
            """() => {
                document.querySelector('form[action="/accounts/100/merge-into"]').addEventListener(
                  'submit', event => { event.preventDefault(); window.submitted = [...new FormData(event.currentTarget)]; }
                );
            }"""
        )
        await source.locator('button[type="submit"]').click()
        await self.page.wait_for_function("window.submitted !== undefined")
        submitted = dict(await self.page.evaluate("window.submitted"))
        self.assertEqual(SOURCE_REF, submitted["account_ref"])
        self.assertEqual(SOURCE_GENERATION, submitted["login_generation"])
        self.assertEqual(THIRD_REF, submitted["target_account_ref"])
        self.assertEqual(THIRD_GENERATION, submitted["target_login_generation"])
