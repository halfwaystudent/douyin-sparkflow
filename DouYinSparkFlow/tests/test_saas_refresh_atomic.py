"""A recycled virtual row must not pair an old id with a new name."""
import unittest
from playwright.async_api import async_playwright
from core import groups
from tests import test_saas_group_guard as fixture

class AtomicRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_refresh_reads_name_and_id_in_one_committed_snapshot(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(args=['--no-sandbox'])
            try:
                page=await browser.new_page();await page.route('**/*',lambda r:r.abort())
                await page.set_content(fixture.group_html(fixture.conv_row('群甲',fixture.GROUP_A,active=True)))
                await page.evaluate(fixture.SETUP,{'rowConvs':[fixture.GROUP_A],'headerConv':fixture.GROUP_A,'inputConv':fixture.GROUP_A})
                real=page.locator('[data-e2e="conversation-item"]')
                class RecyclingRow:
                    async def evaluate(self,script,*args):
                        result=await real.evaluate(script,*args)
                        await page.evaluate("document.querySelector('.conversationConversationItemtitle').textContent='群乙'")
                        return result
                    def locator(self,selector):return real.locator(selector)
                self.assertEqual(await groups._read_saas_group_row(RecyclingRow()),(fixture.BOUND_A,'群甲'))
            finally:await browser.close()

if __name__=='__main__':unittest.main()
