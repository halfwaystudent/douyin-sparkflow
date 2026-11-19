"""New IM SaaS chat-guard compatibility tests.

Synthetic DOM + committed React fiber fixtures mirroring the public
``__federation_expose_default_export.aab99327.js`` bundle:

* rows: ``div[data-e2e="conversation-item"]`` with
  ``.conversationConversationItemtitle`` and the
  ``conversationConversationItemcurConversation`` active class
* header: ``.RightPanelHeaderconvHeader`` / ``.RightPanelHeadertitle``
* input: ``[data-e2e="msg-input"]`` wrapping a ``contenteditable`` editor
* identity: per-conversation ``curConversation`` props on the committed
  header/input component fibers (same object = same pane)

All network is aborted; identities are synthetic.
"""
import unittest
from playwright.async_api import async_playwright
from core import friend_chat_guard as guard

ACCOUNT = {'target_uid_bindings': {
    'demo-A': {'uid': '10001', 'secUid': 'demo-sec-A'},
    'demo-B': {'uid': '10002', 'secUid': 'demo-sec-B'},
    'demo C': {'uid': '10003', 'secUid': 'demo-sec-C'},
}}

CONV_A = {
    'id': '0:1:10000:10001', 'shortId': '7691234567890123401', 'type': 1,
    'toParticipantUserId': '10001',
    'firstPageParticipant': {'participants': [
        {'user_id': '10000', 'sec_uid': 'demo-sec-me'},
        {'user_id': '10001', 'sec_uid': 'demo-sec-A'}]},
}
CONV_B = {
    'id': '0:1:10000:10002', 'shortId': '7691234567890123402', 'type': 1,
    'toParticipantUserId': '10002',
    'firstPageParticipant': {'participants': [
        {'user_id': '10000', 'sec_uid': 'demo-sec-me'},
        {'user_id': '10002', 'sec_uid': 'demo-sec-B'}]},
}
CONV_C = {
    'id': '0:1:10000:10003', 'shortId': '7691234567890123403', 'type': 1,
    'toParticipantUserId': '10003',
    'toParticipantSecUserId': 'demo-sec-C',
    'firstPageParticipant': {'participants': [
        {'user_id': '10000', 'sec_uid': 'demo-sec-me'},
        {'user_id': '10003', 'sec_uid': 'demo-sec-C'}]},
}


def conv_row(name, conv, active=False, title_text=None):
    cls = 'conversationConversationItemwrapper'
    if active:
        cls += ' conversationConversationItemcurConversation'
    shown = name if title_text is None else title_text
    return (
        '<div data-e2e="conversation-item" class="' + cls + '">'
        '<div class="conversationConversationItemrowArea1"></div>'
        '<div class="conversationConversationItemrowArea2">'
        '<div class="conversationConversationItemtitleWrapper">'
        '<div class="conversationConversationItemtitle">' + shown + '</div>'
        '</div><div class="conversationConversationItemDesc"></div>'
        '</div></div>'
    )


def saas_html(rows_html, header_name='demo-A', input_text='synthetic'):
    return (
        '<div id="sub-app"><div class="im-saas-viewport-C7YJGz">'
        '<div class="conversationConversationListwrapper"><div class="conversationConversationListinner">'
        + rows_html +
        '</div></div>'
        '<div class="componentsRightPanelwrapper">'
        '<div class="RightPanelHeaderconvHeader"><div class="RightPanelHeaderinfoContainer"><div class="RightPanelHeaderuser">'
        '<div class="RightPanelHeadersummatyBox"><div class="RightPanelHeadertitleContainer">'
        '<div class="RightPanelHeadertitle">' + header_name + '</div>'
        '</div></div></div></div></div>'
        '<div class="componentsRightPanelnotHeaderArea"><div class="componentsRightPanelinputArea">'
        '<div data-e2e="msg-input" class="messageMsgInputcontainer"><div class="messageMsgInputinputColumn"><div class="messageMsgInputinputRow">'
        '<div class="messageEditorimChatEditorContainer"><div class="messageEditorinputArea" contenteditable="true">' + input_text + '</div></div>'
        '</div></div></div></div>'
        '</div></div></div>'
        '<script>window.sent=0;window.mousedowns=0;'
        "document.querySelector('[contenteditable]').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.defaultPrevented)window.sent++});"
        "for(const row of document.querySelectorAll('[data-e2e=\"conversation-item\"]'))row.addEventListener('mousedown',()=>{window.mousedowns++;for(const r of document.querySelectorAll('[data-e2e=\"conversation-item\"]'))r.classList.remove('conversationConversationItemcurConversation');row.classList.add('conversationConversationItemcurConversation')});"
        '</script>'
    )


SETUP = r"""(cfg) => {
 const root = {tag:3, return:null, stateNode:{}, memoizedProps:{}};
 root.stateNode.current = root;
 const rows = Array.from(document.querySelectorAll('[data-e2e="conversation-item"]'));
 const header = document.querySelector('.RightPanelHeaderconvHeader');
 const input = document.querySelector('[data-e2e="msg-input"] [contenteditable="true"]');
 function host(node, parent, props) {
   const f = {tag:5, stateNode:node, return:parent, memoizedProps:props||{}};
   node['__reactFiber$saas'] = f; return f;
 }
 function comp(props, parent) {
   return {tag:0, return:parent, memoizedProps:props, child:null, sibling:null};
 }
 const scope = comp({}, root);
 root.child = scope;
 let prev = null;
 rows.forEach((row, idx) => {
   const c = cfg.rowConvs[idx];
   const rowComp = comp({conversation: c}, scope);
   const h = host(row, rowComp);
   rowComp.child = h;
   if (prev) prev.sibling = rowComp; else scope.child = rowComp;
   prev = rowComp;
 });
 const panel = comp({}, scope);
 if (prev) prev.sibling = panel;
 const headerComp = comp({curConversation: cfg.headerConv}, null);
 const lazyWrap = comp({tag:'RightPanelHeader', children:null}, panel);
 const hHost = host(header, headerComp);
 headerComp.child = hHost; headerComp.return = lazyWrap; lazyWrap.child = headerComp; lazyWrap.return = panel;
 const chatTab = comp({openInfoPage:null}, panel);
 const msgComp = comp({curConversation: cfg.inputConv}, chatTab);
 const editorWrap = comp({className:'messageEditorimChatEditorContainer'}, msgComp);
 const iHost = host(input, editorWrap);
 editorWrap.child = iHost; msgComp.child = editorWrap; chatTab.child = msgComp;
 msgComp.return = chatTab; chatTab.return = panel;
 lazyWrap.sibling = chatTab;
 panel.child = lazyWrap;
 panel.sibling = null;
 window.saasFiber = {root, scope, panel};
}"""


class SaasChatGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage'])
        self.page = await self.browser.new_page()
        await self.page.route('**/*', lambda route: route.abort())
        rows = conv_row('demo-A', CONV_A, active=True) + conv_row('demo-B', CONV_B)
        await self.page.set_content(saas_html(rows))
        await self.page.evaluate(SETUP, {
            'rowConvs': [CONV_A, CONV_B],
            'headerConv': CONV_A,
            'inputConv': CONV_A,
        })

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_type2_conversation_cannot_be_selected_as_a_friend(self):
        group_like = dict(CONV_A, type=2)
        await self.page.evaluate(SETUP,{'rowConvs':[group_like,CONV_B],'headerConv':group_like,'inputConv':group_like})
        ok, _ = await self.select()
        self.assertFalse(ok)
        self.assertEqual(await self.page.evaluate('window.mousedowns'),0)

    async def test_missing_conversation_type_cannot_authorize_friend_selection(self):
        unknown = dict(CONV_A);unknown.pop('type')
        await self.page.evaluate(SETUP,{'rowConvs':[unknown,CONV_B],'headerConv':unknown,'inputConv':unknown})
        ok, _ = await self.select()
        self.assertFalse(ok)

    async def test_type_change_after_arm_blocks_enter(self):
        ok, _ = await self.select()
        self.assertTrue(ok)
        ok, _ = await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True)
        self.assertTrue(ok)
        await self.page.evaluate('''() => {
          const root=window.saasFiber.root;const stack=[root];
          while(stack.length){const f=stack.pop();if(f.memoizedProps?.conversation)f.memoizedProps.conversation.type=2;if(f.memoizedProps?.curConversation)f.memoizedProps.curConversation.type=2;if(f.child)stack.push(f.child);if(f.sibling)stack.push(f.sibling)}
        }''')
        await self.page.locator('[contenteditable="true"]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'),0)
        self.assertFalse(await guard.dispatch_was_allowed(self.page))

    async def select(self, name='demo-A'):
        row = self.page.locator('[data-e2e="conversation-item"]').filter(has_text=name).first
        return await guard.select_friend(self.page, row, ACCOUNT, name)

    async def _rebuild(self, row_convs, header_conv, input_conv, rows_html=None, header_name='demo-A'):
        if rows_html is not None:
            await self.page.set_content(saas_html(rows_html, header_name=header_name))
        await self.page.evaluate(SETUP, {
            'rowConvs': row_convs,
            'headerConv': header_conv,
            'inputConv': input_conv,
        })

    async def test_saas_correct_identity_allows_one_enter(self):
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo-A', arm=True))[0])
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'), 1)

    async def test_saas_explicit_sec_uid_field_is_accepted(self):
        rows = conv_row('demo C', CONV_C, active=True, title_text='demo C')
        await self._rebuild([CONV_C], CONV_C, CONV_C, rows_html=rows, header_name='demo C')
        ok, reason = await guard.select_friend(
            self.page, self.page.locator('[data-e2e="conversation-item"]').first, ACCOUNT, 'demo C')
        self.assertTrue(ok, reason)
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo C', arm=True))[0])

    async def test_saas_nbsp_title_still_matches(self):
        rows = conv_row('demo C', CONV_C, active=True, title_text='demo C')
        await self._rebuild([CONV_C], CONV_C, CONV_C, rows_html=rows, header_name='demo C')
        ok, reason = await guard.select_friend(
            self.page, self.page.locator('[data-e2e="conversation-item"]').first, ACCOUNT, 'demo C')
        self.assertTrue(ok, reason)

    async def test_saas_wrong_peer_identity_is_rejected(self):
        bad = dict(CONV_A, toParticipantUserId='99999',
                   firstPageParticipant={'participants': [{'user_id': '99999', 'sec_uid': 'demo-sec-A'}]})
        rows = conv_row('demo-A', bad, active=True)
        await self._rebuild([bad], bad, bad, rows_html=rows)
        ok, reason = await guard.select_friend(
            self.page, self.page.locator('[data-e2e="conversation-item"]').first, ACCOUNT, 'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason, 'friend_row_identity_mismatch')
        self.assertEqual(await self.page.evaluate('window.mousedowns'), 0)

    async def test_saas_duplicate_name_is_rejected(self):
        rows = conv_row('demo-A', CONV_A, active=True) + conv_row('demo-A', CONV_B)
        await self._rebuild([CONV_A, CONV_B], CONV_A, CONV_A, rows_html=rows)
        ok, reason = await guard.select_friend(
            self.page, self.page.locator('[data-e2e="conversation-item"]').first, ACCOUNT, 'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason, 'friend_identity_ambiguous')

    async def test_saas_row_recycled_to_other_identity_is_not_clicked(self):
        await self.page.evaluate("""() => {
          const rowComp = window.saasFiber.scope.child;
          rowComp.memoizedProps.conversation = {
            id:'0:1:10000:10002', shortId:'7691234567890123402', type:1,
            toParticipantUserId:'10002',
            firstPageParticipant:{participants:[{user_id:'10002',sec_uid:'demo-sec-B'}]}
          };
        }""")
        ok, reason = await guard.select_friend(
            self.page, self.page.locator('[data-e2e="conversation-item"]').first, ACCOUNT, 'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason, 'friend_row_identity_mismatch')
        self.assertEqual(await self.page.evaluate('window.mousedowns'), 0)

    async def test_saas_stale_pane_conversation_is_rejected(self):
        self.assertTrue((await self.select('demo-A'))[0])
        await self._rebuild([CONV_A, CONV_B], CONV_B, CONV_B)
        ok, reason = await guard.verify_friend(self.page, ACCOUNT, 'demo-A')
        self.assertFalse(ok)
        self.assertIn(reason, {'friend_current_conversation_mismatch', 'friend_header_name_mismatch',
                               'friend_pane_scope_mismatch', 'friend_open_conversation_mismatch'})

    async def test_saas_sibling_pane_mismatch_is_rejected(self):
        self.assertTrue((await self.select('demo-A'))[0])
        await self._rebuild([CONV_A, CONV_B], CONV_A, CONV_B)
        ok, reason = await guard.verify_friend(self.page, ACCOUNT, 'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason, 'friend_pane_scope_mismatch')

    async def test_saas_input_replaced_after_arm_is_blocked(self):
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo-A', arm=True))[0])
        await self.page.evaluate("""() => {
          const old = document.querySelector('[contenteditable]');
          const fresh = old.cloneNode(true);
          fresh.addEventListener('keydown', e => { if (e.key==='Enter' && !e.defaultPrevented) window.sent++; });
          old.replaceWith(fresh);
        }""")
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'), 0)

    async def test_saas_conversation_change_after_arm_blocks_enter(self):
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo-A', arm=True))[0])
        await self._rebuild([CONV_A, CONV_B], CONV_B, CONV_B)
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'), 0)
        self.assertFalse(await guard.dispatch_was_allowed(self.page))

    async def test_saas_header_name_mismatch_is_rejected(self):
        self.assertTrue((await self.select('demo-A'))[0])
        await self.page.evaluate("document.querySelector('.RightPanelHeadertitle').textContent='demo-B'")
        ok, reason = await guard.verify_friend(self.page, ACCOUNT, 'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason, 'friend_header_name_mismatch')

    async def test_saas_missing_pane_conversation_is_rejected(self):
        self.assertTrue((await self.select('demo-A'))[0])
        await self._rebuild([CONV_A, CONV_B], None, None)
        ok, reason = await guard.verify_friend(self.page, ACCOUNT, 'demo-A')
        self.assertFalse(ok)

    async def test_saas_unbound_target_never_uses_name_only(self):
        ok, reason = await guard.select_friend(
            self.page, self.page.locator('[data-e2e="conversation-item"]').first, {}, 'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason, 'friend_binding_unavailable')


if __name__ == '__main__':
    unittest.main()
