"""The personal end time uses a native phone picker plus explicit midnight."""
import unittest
import re
from pathlib import Path
from playwright.async_api import async_playwright
from core import personal_streak
from tests import test_shared_security as fixtures


class PersonalEndTimeControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.SharedConsoleTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.client=self.fixture.login();self.csrf=self.fixture.token(self.client)

    def test_default_midnight_renders_lightweight_native_controls(self):
        page=self.client.get('/preferences/streak')
        self.assertIn('type="time" name="end_time"',page.text)
        self.assertIn('name="end_midnight" checked',page.text)
        self.assertLess(page.text.count('<option'),50)

    def test_explicit_midnight_is_saved_without_treating_it_as_zero_hour(self):
        form={'csrf_token':self.csrf,'edit_revision':'','enabled':'on','time':'18:00','end_time':'23:59','end_midnight':'on','timezone':'Asia/Shanghai','messages':'only-personal'}
        response=self.client.post('/preferences/streak',data=form,follow_redirects=False)
        self.assertEqual(303,response.status_code)
        self.assertEqual('24:00',personal_streak.preferences({'username':'alice','role':'user'})['end_time'])

    def test_custom_minute_end_time_survives_reload_without_midnight_selected(self):
        form={'csrf_token':self.csrf,'edit_revision':'','enabled':'on','time':'18:00','end_time':'20:17','timezone':'Asia/Shanghai','messages':'only-personal'}
        self.assertEqual(303,self.client.post('/preferences/streak',data=form,follow_redirects=False).status_code)
        page=self.client.get('/preferences/streak')
        self.assertIn('name="end_time" value="20:17"',page.text)
        self.assertNotIn('name="end_midnight" checked',page.text)


class PersonalEndTimeBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_midnight_default_never_blocks_direct_time_editing(self):
        fixture=fixtures.SharedConsoleTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        client=fixture.login();html=client.get('/preferences/streak').text
        script=re.findall(r'<script>(.*?)</script>',html,re.S)[-1]
        async with async_playwright() as runtime:
            browser=await runtime.chromium.launch()
            try:
                page=await browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                await page.route('**/*',lambda route:route.abort())
                await page.set_content('''<form><input type="time" name="end_time" value="23:59" required><input type="checkbox" name="end_midnight" checked></form>''')
                await page.add_script_tag(content=script)
                end=page.locator('[name=end_time]');midnight=page.locator('[name=end_midnight]')
                self.assertTrue(await midnight.is_checked())
                self.assertTrue(await end.is_enabled())
                await end.fill('20:17')
                self.assertFalse(await midnight.is_checked())
                values=await page.locator('form').evaluate('f=>Object.fromEntries(new FormData(f))')
                self.assertEqual('20:17',values['end_time'])
                self.assertNotIn('end_midnight',values)
                await midnight.check()
                self.assertTrue(await end.is_enabled())
                values=await page.locator('form').evaluate('f=>Object.fromEntries(new FormData(f))')
                self.assertEqual('on',values['end_midnight'])
            finally:
                await browser.close()
