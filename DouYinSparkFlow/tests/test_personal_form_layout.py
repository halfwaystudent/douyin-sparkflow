"""Exercise the actual personal form, CSS and script without external access."""
from pathlib import Path
import re
import unittest

from playwright.async_api import async_playwright

from core import personal_streak
from tests import test_shared_security as fixtures

ROOT = Path(__file__).resolve().parents[1]


class PersonalFormLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_time_fields_align_and_fit_at_phone_and_desktop_widths(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        html = fixture.login().get('/preferences/streak').text
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch()
            try:
                for width in (320, 390, 768, 1280):
                    with self.subTest(width=width):
                        page = await browser.new_page(viewport={'width': width, 'height': 844})
                        await page.route('**/*', lambda route: route.abort())
                        await page.set_content(html)
                        await page.add_style_tag(path=str(ROOT / 'webui/static/app.css'))
                        metrics = await page.evaluate('''() => {
                            const names=['time','end_time','timezone'];
                            const controls=names.map(n=>{
                                const e=document.querySelector('[name='+n+']');
                                const r=e.getBoundingClientRect(), f=e.closest('.field').getBoundingClientRect();
                                const s=getComputedStyle(e);
                                return {name:n,x:r.x,y:r.y,w:r.width,h:r.height,
                                    right:r.right,fieldRight:f.right,appearance:s.appearance,
                                    fontSize:parseFloat(s.fontSize)};
                            });
                            return {controls,overflow:document.documentElement.scrollWidth>innerWidth};
                        }''')
                        self.assertFalse(metrics['overflow'], metrics)
                        start, end, timezone = metrics['controls']
                        for control in metrics['controls']:
                            self.assertLessEqual(control['right'], control['fieldRight'] + 1, metrics)
                            self.assertAlmostEqual(44, control['h'], delta=1)
                            if width <= 900:
                                self.assertGreaterEqual(control['fontSize'], 16)
                        self.assertAlmostEqual(start['w'], end['w'], delta=1)
                        if width == 1280:
                            self.assertAlmostEqual(start['y'], end['y'], delta=1, msg=str(metrics))
                        self.assertEqual('none', start['appearance'])
                        self.assertEqual('none', end['appearance'])
                        await page.close()
            finally:
                await browser.close()

    async def test_saved_inherited_messages_dont_reset_timezone_on_page_load(self):
        fixture = fixtures.SharedConsoleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        # Represent a saved opt-in to global templates without depending on
        # the save validator being implemented by the backend slice yet.
        profile = personal_streak.save_preferences(
            {'username': 'alice', 'role': 'user'}, enabled=False,
            at_time='10:00', end_time='17:00', timezone_name='Asia/Tokyo', messages=['before'])
        from webui import users
        with users._web_users_lock():
            raw = users._load_raw()
            saved = next(value for value in raw['personal_streak'].values()
                         if value['revision'] == profile['revision'])
            saved['messages'] = []
            users._atomic_write_json(users.USERS_FILE, raw)
        html = fixture.login().get('/preferences/streak').text
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch()
            try:
                page = await browser.new_page(timezone_id='America/New_York')
                await page.route('**/*', lambda route: route.abort())
                await page.set_content(html)
                await page.add_script_tag(content=re.findall(r'<script>(.*?)</script>', html, re.S)[-1])
                self.assertEqual('Asia/Tokyo', await page.locator('[name=timezone]').input_value())
                self.assertFalse(await page.locator('[name=messages]').evaluate('e=>e.required'))
                self.assertEqual(profile['revision'], await page.locator('[name=edit_revision]').input_value())
            finally:
                await browser.close()
