"""Strong-name header replay from a sanitized real single-friend snapshot.

The observed title was DIV > DIV > (STRONG.box-header-* + SPAN(status/IMG)),
plus a separate header-tail. Names/status text below are synthetic.
"""
import unittest
from playwright.async_api import async_playwright
from core import friend_chat_guard as guard
from tests.test_friend_chat_guard import HTML
from tests.test_friend_chat_observed_shape import ACCOUNT, SETUP


class StrongHeaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.launch(args=['--no-sandbox','--disable-dev-shm-usage'])
        self.page=await self.browser.new_page()
        await self.page.route('**/*',lambda r:r.abort())
        await self.page.set_content(HTML)
        await self.page.evaluate(SETUP)
        self.assertTrue((await guard.select_friend(
            self.page,self.page.locator('li').first,ACCOUNT,'demo-A'))[0])

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def header(self,contents):
        await self.page.locator('div.box-header-demo').evaluate('(n,s)=>n.innerHTML=s',contents)

    async def test_observed_strong_name_with_adjacent_status_allows_correct_recipient(self):
        # Rebuild only anonymous structure from the actual capture. Text equal
        # to the recipient becomes demo-A; every other text becomes fake status.
        import json
        from pathlib import Path
        from html import escape
        fixture=json.loads((Path(__file__).with_name('fixtures')/'friend_title_shape.json').read_text())
        self.assertTrue(fixture['identity']['same_committed_provider'])
        source=fixture['title']['headers'][0]['structure']
        def render(node):
            tag=node['tag'].lower() if node['tag']!='OTHER' else 'span'
            kinds=node['class_kinds']
            classes='box-header-tail-demo' if kinds['header_tail'] else ('box-header-name-demo' if kinds['header'] and tag=='strong' else '')
            text=''.join('demo-A' if d['trim_equal'] else 'synthetic-status'
                         for d in node['direct_text'])
            attrs=(' class="'+classes+'"' if classes else '')+(' hidden' if not node['visible'] else '')
            return '<'+tag+attrs+'>'+escape(text)+''.join(render(c) for c in node['children'])+'</'+tag+'>'
        await self.header(''.join(render(c) for c in source['children']))
        ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True)
        self.assertTrue(ok,reason)
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'),1)
        self.assertTrue(await guard.dispatch_was_allowed(self.page))

    async def test_wrong_name_is_not_overridden_by_status_mentioning_the_target(self):
        await self.header('<div><strong class="box-header-name-demo">demo-B</strong>'
                          '<span>demo-A</span></div>')
        ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True)
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_header_name_mismatch')

    async def test_two_visible_name_elements_are_ambiguous_even_when_both_match(self):
        await self.header('<strong class="box-header-name-demo">demo-A</strong>'*2)
        ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_header_ambiguous')

    async def test_hidden_matching_name_does_not_override_visible_wrong_name(self):
        await self.header('<strong hidden class="box-header-name-demo">demo-A</strong>'
                          '<strong class="box-header-name-demo">demo-B</strong>')
        ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_header_name_mismatch')

    async def test_name_present_only_inside_header_tail_does_not_count(self):
        await self.header('<strong class="box-header-name-demo">demo-B</strong>'
                          '<div class="box-header-tail-demo">'
                          '<strong class="box-header-name-demo">demo-A</strong></div>')
        ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_header_name_mismatch')

    async def test_matching_name_does_not_replace_mandatory_current_pane_identity(self):
        await self.header('<strong class="box-header-name-demo">demo-A</strong><span>status</span>')
        await self.page.evaluate('window.observed.provider.memoizedProps.value.conversation=window.observed.conversations[1]')
        ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True)
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_current_conversation_mismatch')

    async def test_title_change_after_arm_blocks_enter_even_if_status_mentions_target(self):
        await self.header('<strong class="box-header-name-demo">demo-A</strong><span>status</span>')
        self.assertTrue((await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True))[0])
        await self.header('<strong class="box-header-name-demo">demo-B</strong><span>demo-A</span>')
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'),0)
        self.assertFalse(await guard.dispatch_was_allowed(self.page))

    async def test_substring_or_unmarked_strong_does_not_authorize_the_name(self):
        for contents in [
            '<strong class="box-header-name-demo">demo-AA</strong><span>status</span>',
            '<strong>demo-A</strong><span>status</span>',
            '<strong hidden class="box-header-name-demo">demo-A</strong><span>demo-A</span>',
        ]:
            with self.subTest(contents=contents):
                await self.header(contents)
                ok,_=await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True)
                self.assertFalse(ok)

if __name__=='__main__':
    unittest.main()
