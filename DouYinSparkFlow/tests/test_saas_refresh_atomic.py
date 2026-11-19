"""A recycled virtual row must not pair an old id with a new name."""
import unittest
from playwright.async_api import async_playwright
from core import groups, friends, tasks
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

    async def test_proven_friend_does_not_abort_group_refresh_or_bound_scan(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(args=['--no-sandbox'])
            try:
                page=await browser.new_page();await page.route('**/*',lambda r:r.abort())
                peer=dict(fixture.GROUP_B,type=1)
                rows=fixture.conv_row('群甲',fixture.GROUP_A,active=True)+fixture.conv_row('好友乙',peer)
                await page.set_content(fixture.group_html(rows))
                await page.evaluate(fixture.SETUP,{'rowConvs':[fixture.GROUP_A,peer],'headerConv':fixture.GROUP_A,'inputConv':fixture.GROUP_A})
                records=await groups.collect_saas_group_rows(page,max_scan_seconds=3,scroll_delay_seconds=0)
                self.assertEqual(records,[{'name':'群甲','conversationId':fixture.BOUND_A}])
                config=tasks._normalize_friend_list_scan_config({});config.update(maxScanSeconds=3,scrollDelaySeconds=0)
                scrollable=await page.locator('.conversationConversationListwrapper').element_handle()
                snapshot=await tasks._scan_bound_group_rows(page,scrollable,'[data-e2e="conversation-item"]','synthetic',config)
                self.assertEqual(snapshot,[(fixture.BOUND_A,'群甲')])
            finally:await browser.close()

    async def test_refresh_friend_type_and_label_are_one_snapshot(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(args=['--no-sandbox'])
            try:
                page=await browser.new_page();await page.route('**/*',lambda r:r.abort())
                peer=dict(fixture.GROUP_A,type=1)
                await page.set_content(fixture.group_html(fixture.conv_row('好友甲',peer,active=True)))
                await page.evaluate(fixture.SETUP,{'rowConvs':[peer],'headerConv':peer,'inputConv':peer})
                real=page.locator('[data-e2e="conversation-item"]')
                class RecycledTitle:
                    @property
                    def first(self):return self
                    def __getattr__(self,key):return getattr(real.locator('.conversationConversationItemtitle'),key)
                    async def inner_text(self,*args,**kw):
                        value=await real.locator('.conversationConversationItemtitle').inner_text(*args,**kw)
                        await page.evaluate("""() => {const r=document.querySelector('[data-e2e="conversation-item"]');let f=r[Object.keys(r).find(k=>k.startsWith('__reactFiber'))];for(let i=0;f&&i<=3;i++,f=f.return)if(f.memoizedProps?.conversation){f.memoizedProps.conversation.type=2;break;}r.querySelector('.conversationConversationItemtitle').textContent='群乙'}""")
                        return value
                class Row:
                    def locator(self,selector):return RecycledTitle()
                    def __getattr__(self,key):return getattr(real,key)
                self.assertEqual(await friends._read_saas_friend_row(Row()),('好友甲',1))
            finally:await browser.close()

if __name__=='__main__':unittest.main()
