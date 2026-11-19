"""Offline Chromium regressions for the actual sender's SaaS entrypoints."""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from playwright.async_api import async_playwright
from core import tasks

FILTER='''<div class="conversationFilterTagBarwrapper"><div class="conversationFilterTagBargroup"><div class="conversationFilterTagBargroupLabel">消息</div><div id="all" class="conversationFilterTagBartagItem conversationFilterTagBartagItemActive"><span class="conversationFilterTagBartagItemLabel">全部</span></div><div id="group" class="conversationFilterTagBartagItem"><span class="conversationFilterTagBartagItemLabel">群聊</span></div></div></div><script>for(const t of document.querySelectorAll('.conversationFilterTagBartagItem'))t.onclick=()=>{document.querySelectorAll('.conversationFilterTagBartagItem').forEach(e=>e.classList.remove('conversationFilterTagBartagItemActive'));t.classList.add('conversationFilterTagBartagItemActive')}</script>'''

class SaasTasksTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.launch(args=['--no-sandbox','--disable-dev-shm-usage'])
        self.page=await self.browser.new_page()
        await self.page.route('**/*',lambda r:r.abort())
        self.page.set_default_timeout(800)
    async def asyncTearDown(self):
        await self.browser.close();await self.pw.stop()
    async def content(self,body):
        await self.page.set_content('<div id="sub-app"><div class="im-saas-viewport-test">'+body+'</div></div>')
    async def test_new_row_name_not_last_message_or_time(self):
        await self.content('<div data-e2e="conversation-item"><span>11:55</span><div class="conversationConversationItemtitle">Demo A</div><p>last message</p></div>')
        self.assertEqual(await tasks._extract_friend_display_name(self.page.locator('[data-e2e="conversation-item"]')),'Demo A')
    async def test_input_scoped_to_saas_not_global_last_editable(self):
        await self.content('<div data-e2e="msg-input"><div contenteditable="true" id="correct">draft</div></div><div contenteditable="true" role="textbox" id="wrong">search</div>')
        editor,_=await tasks.locate_chat_input(self.page)
        self.assertEqual(await editor.get_attribute('id'),'correct')
    async def test_ambiguous_saas_editor_refuses(self):
        await self.content('<div data-e2e="msg-input"><div contenteditable="true">one</div><div contenteditable="true">two</div></div>')
        with self.assertRaises(RuntimeError):await tasks.locate_chat_input(self.page)
    async def test_saas_friend_tab_never_calls_old_xpath_click(self):
        await self.content(FILTER)
        with patch.object(tasks,'_dismiss_non_login_dialogs',AsyncMock()),patch.object(tasks,'_click_first_visible_locator',AsyncMock(return_value=False)) as legacy:
            result=await tasks._open_friends_tab(self.page,'synthetic','invalid-old-selector',())
        self.assertEqual(result,'saas_verified');legacy.assert_not_awaited()
    async def test_group_shortid_namespace_not_threadid(self):
        await self.content('<div data-e2e="conversation-item"><span class="conversationConversationItemtitle">group</span></div>')
        await self.page.evaluate('''() => {const r=document.querySelector('[data-e2e="conversation-item"]');r.__reactFiber$test={memoizedProps:{conversation:{type:2,id:'0:2:10000:10001',shortId:'7691234567890123401'}}}}''')
        self.assertEqual(await tasks._read_group_row_conversation_id(self.page.locator('[data-e2e="conversation-item"]')),'7691234567890123401')
    async def test_group_filter_activated_and_verified(self):
        await self.content(FILTER)
        with patch.object(tasks,'_dismiss_non_login_dialogs',AsyncMock()),patch.object(tasks,'_click_first_visible_locator',AsyncMock(return_value=False)) as legacy:
            await tasks._open_groups_tab(self.page,'synthetic')
        self.assertTrue(await tasks._group_tab_is_active(self.page,'synthetic'));legacy.assert_not_awaited()
    async def test_saas_own_bubble_today_only_no_quote_or_peer(self):
        await self.content('''<div class="componentsRightPanelwrapper"><div class="MessageBoxContentcolumnBox MessageBoxContentisFromMe"><div data-e2e="msg-item-content"><div class="MessageItemTextbubbleTextContent">own today</div></div></div><div class="MessageBoxContentcolumnBox"><div data-e2e="msg-item-content"><div class="MessageItemTextbubbleTextContent">peer</div></div></div></div>''')
        await self.page.evaluate('''() => {for(const e of document.querySelectorAll('[data-e2e="msg-item-content"]'))e.__reactFiber$test={memoizedProps:{message:{createdAt:new Date(),isFromMe:e.textContent==='own today'}}}}''')
        result=await tasks.snapshot_last_own_message(self.page)
        self.assertIsNotNone(result);self.assertEqual(result['text'],'own today')
        self.assertEqual(await tasks.count_today_own_message_matches(self.page,'own today'),1)
        self.assertEqual(await tasks.count_today_own_message_matches(self.page,'peer'),0)
        await self.page.evaluate('''() => {for(const e of document.querySelectorAll('[data-e2e="msg-item-content"]'))e.__reactFiber$test.memoizedProps.message.createdAt=new Date(Date.now()-86400000)}''')
        self.assertIsNone(await tasks.snapshot_last_own_message(self.page))

    async def test_saas_group_row_is_not_a_friend_record(self):
        await self.content('<div data-e2e="conversation-item"><div class="conversationConversationItemtitle">same name</div></div>')
        await self.page.evaluate('''() => {document.querySelector('[data-e2e="conversation-item"]').__reactFiber$test={memoizedProps:{conversation:{type:2,shortId:'7691234567890123401'}}}}''')
        self.assertIsNone(await tasks._extract_friend_record(self.page.locator('[data-e2e="conversation-item"]')))

    async def test_sender_selects_real_saas_row_with_existing_binding(self):
        from tests import test_saas_chat_guard as fixture
        rows=fixture.conv_row('demo-A',fixture.CONV_A,active=True)
        await self.page.set_content(fixture.saas_html(rows).replace('<div class="conversationConversationListwrapper">',FILTER+'<div class="conversationConversationListwrapper">'))
        await self.page.evaluate(fixture.SETUP,{'rowConvs':[fixture.CONV_A],'headerConv':fixture.CONV_A,'inputConv':fixture.CONV_A})
        scan=tasks.scroll_and_select_user(self.page,fixture.ACCOUNT,'synthetic',['demo-A'],tasks._normalize_friend_list_scan_config({}))
        with patch.object(tasks,'_dismiss_non_login_dialogs',AsyncMock()),patch.object(tasks,'_persist_friend_index'):
            try:self.assertEqual(await scan.__anext__(),'demo-A')
            finally:await scan.aclose()
        self.assertEqual(await self.page.evaluate('window.mousedowns'),1)
        self.assertEqual(await self.page.evaluate('window.sent'),0)

if __name__=='__main__':unittest.main()
