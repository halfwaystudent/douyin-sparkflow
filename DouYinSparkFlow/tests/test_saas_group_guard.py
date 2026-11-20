"""Synthetic-Chromium tests for core.saas_group_guard.

Fixtures mirror the public ``__federation_expose_default_export.aab99327.js``
bundle: ``[data-e2e="conversation-item"]`` rows whose committed fiber carries
``memoizedProps.conversation`` (``type === 2`` GROUP_CHAT, numeric ``shortId``),
the ``conversationConversationItemcurConversation`` active class,
``.RightPanelHeaderconvHeader`` / ``.RightPanelHeadertitle``, and
``[data-e2e="msg-input"] [contenteditable="true"]``. Fiber graph shape follows
tests/test_saas_chat_guard.py's SETUP (module imported for helpers only —
never subclassed, so unittest discovery stays single).

All identities are synthetic; every route is aborted.
"""
import unittest

from playwright.async_api import async_playwright

from core import saas_group_guard as guard

GROUP_A = {
    'id': '0:2:10000:20001', 'shortId': '7691234567890123401', 'type': 2,
    'coreInfo': {'name': '群甲'},
}
GROUP_B = {
    'id': '0:2:10000:20002', 'shortId': '7691234567890123402', 'type': 2,
    'coreInfo': {'name': '群乙'},
}
GROUP_C = {
    'id': '0:2:10000:20003', 'shortId': '7691234567890123403', 'type': 2,
    'coreInfo': {'name': '群丙'},
}
FRIEND = {
    'id': '0:1:10000:10001', 'shortId': '7691234567890123499', 'type': 1,
    'toParticipantUserId': '10001',
}

BOUND_A = GROUP_A['shortId']
BOUND_B = GROUP_B['shortId']

FILTER_GROUP = (
    '<div class="conversationFilterTagBarwrapper"><div class="conversationFilterTagBargroup">'
    '<div class="conversationFilterTagBargroupLabel">消息</div>'
    '<div class="conversationFilterTagBartagItem"><span class="conversationFilterTagBartagItemLabel">全部</span></div>'
    '<div class="conversationFilterTagBartagItem conversationFilterTagBartagItemActive">'
    '<span class="conversationFilterTagBartagItemLabel">群聊</span></div>'
    '</div></div>'
)


def conv_row(name, conv, active=False):
    cls = 'conversationConversationItemwrapper'
    if active:
        cls += ' conversationConversationItemcurConversation'
    return (
        '<div data-e2e="conversation-item" class="' + cls + '">'
        '<div class="conversationConversationItemrowArea1"></div>'
        '<div class="conversationConversationItemrowArea2">'
        '<div class="conversationConversationItemtitleWrapper">'
        '<div class="conversationConversationItemtitle">' + name + '</div>'
        '</div><div class="conversationConversationItemDesc"></div>'
        '</div></div>'
    )


def group_html(rows_html, header_name='群甲', active_row_index=0, include_input=True):
    rows = rows_html.split('@@ROW@@') if '@@ROW@@' in rows_html else [rows_html]
    body = ''
    for i, chunk in enumerate(rows):
        if not chunk:
            continue
        cls = 'conversationConversationItemwrapper'
        if i == active_row_index:
            cls += ' conversationConversationItemcurConversation'
        # conv_row already emitted class; rebuild with active flag instead
        body += chunk
    header = (
        '<div class="RightPanelHeaderconvHeader"><div class="RightPanelHeaderinfoContainer">'
        '<div class="RightPanelHeaderuser"><div class="RightPanelHeadersummatyBox">'
        '<div class="RightPanelHeadertitleContainer">'
        '<div class="RightPanelHeadertitle">' + header_name + '</div>'
        '</div></div></div></div></div>'
    )
    input_html = ''
    if include_input:
        input_html = (
            '<div class="componentsRightPanelinputArea">'
            '<div data-e2e="msg-input" class="messageMsgInputcontainer">'
            '<div class="messageMsgInputinputColumn"><div class="messageMsgInputinputRow">'
            '<div class="messageEditorimChatEditorContainer">'
            '<div class="messageEditorinputArea" contenteditable="true">draft</div>'
            '</div></div></div></div></div>'
        )
    return (
        '<div id="sub-app"><div class="im-saas-viewport-C7YJGz">'
        + FILTER_GROUP
        + '<div class="conversationConversationListwrapper">' + body + '</div>'
        + '<div class="componentsRightPanelwrapper">' + header
        + '<div class="componentsRightPanelnotHeaderArea">' + input_html + '</div>'
        + '</div></div>'
        + '<script>window.mousedowns=0;'
        + "for(const row of document.querySelectorAll('[data-e2e=\"conversation-item\"]'))row.addEventListener('mousedown',()=>{window.mousedowns++});"
        + '</script>'
    )


# Same committed-fiber fixture shape as test_saas_chat_guard.SETUP, extended so
# the active row index and header/input conversations are configurable.
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
 if (header) {
   const headerComp = comp({curConversation: cfg.headerConv}, null);
   const lazyWrap = comp({tag:'RightPanelHeader', children:null}, panel);
   const hHost = host(header, headerComp);
   headerComp.child = hHost; headerComp.return = lazyWrap; lazyWrap.child = headerComp; lazyWrap.return = panel;
   panel.child = lazyWrap;
 } else { panel.child = null; }
 if (input) {
   const chatTab = comp({openInfoPage:null}, panel);
   const msgComp = comp({curConversation: cfg.inputConv}, chatTab);
   const editorWrap = comp({className:'messageEditorimChatEditorContainer'}, msgComp);
   const iHost = host(input, editorWrap);
   editorWrap.child = iHost; msgComp.child = editorWrap; chatTab.child = msgComp;
   msgComp.return = chatTab; chatTab.return = panel;
   if (panel.child) panel.child.sibling = chatTab; else panel.child = chatTab;
   chatTab.sibling = null;
 } else if (panel.child) { panel.child.sibling = null; }
 window.saasFiber = {root, scope, panel};
}"""


class SaasGroupGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage'])
        self.page = await self.browser.new_page()
        await self.page.route('**/*', lambda route: route.abort())
        rows = conv_row('群甲', GROUP_A, active=True) + conv_row('群乙', GROUP_B)
        await self.page.set_content(group_html(rows))
        await self.page.evaluate(SETUP, {
            'rowConvs': [GROUP_A, GROUP_B],
            'headerConv': GROUP_A,
            'inputConv': GROUP_A,
        })

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def _rows(self):
        return self.page.locator('[data-e2e="conversation-item"]')

    async def _rebuild(self, row_convs, header_conv, input_conv, rows_html=None,
                       header_name='群甲', include_input=True):
        if rows_html is not None:
            await self.page.set_content(group_html(rows_html, header_name=header_name,
                                                   include_input=include_input))
        await self.page.evaluate(SETUP, {
            'rowConvs': row_convs,
            'headerConv': header_conv,
            'inputConv': input_conv,
        })

    # ---------- read_row_id ----------

    async def test_read_row_id_returns_shortid_for_group(self):
        row = (await self._rows()).first
        self.assertEqual(await guard.read_row_id(row), BOUND_A)

    async def test_read_row_id_rejects_friend_row(self):
        rows = conv_row('好友甲', FRIEND)
        await self._rebuild([FRIEND], FRIEND, FRIEND, rows_html=rows,
                            header_name='好友甲')
        row = (await self._rows()).first
        self.assertIsNone(await guard.read_row_id(row))

    async def test_read_row_id_rejects_row_without_fiber(self):
        html = ('<div id="sub-app"><div class="im-saas-viewport-X">' + FILTER_GROUP +
                '<div class="conversationConversationListwrapper">' +
                conv_row('群甲', GROUP_A) + '</div></div></div>')
        await self.page.set_content(html)
        row = (await self._rows()).first
        self.assertIsNone(await guard.read_row_id(row))

    # ---------- select_row ----------

    async def test_select_row_clicks_bound_group(self):
        row = (await self._rows()).nth(1)  # 群乙, not yet active
        result = await guard.select_row(row, BOUND_B)
        self.assertEqual(result, 'clicked')
        self.assertEqual(await self.page.evaluate('window.mousedowns'), 1)

    async def test_select_row_rejects_wrong_bound_id(self):
        row = (await self._rows()).first
        result = await guard.select_row(row, BOUND_B)
        self.assertNotEqual(result, 'clicked')
        self.assertIn('identity_mismatch', result)
        self.assertEqual(await self.page.evaluate('window.mousedowns'), 0)

    async def test_select_row_rejects_type1_conversation(self):
        rows = conv_row('好友甲', FRIEND)
        await self._rebuild([FRIEND], FRIEND, FRIEND, rows_html=rows,
                            header_name='好友甲')
        row = (await self._rows()).first
        result = await guard.select_row(row, FRIEND['shortId'])
        self.assertNotEqual(result, 'clicked')
        self.assertEqual(await self.page.evaluate('window.mousedowns'), 0)

    async def test_select_row_rejects_recycled_row(self):
        # Row fiber recycled to a different group after the locator was taken.
        row = (await self._rows()).first
        await self.page.evaluate("""() => {
          const scope = window.saasFiber.scope;
          scope.child.memoizedProps.conversation = {
            id:'0:2:10000:20009', shortId:'7691234567890123409', type:2,
            coreInfo:{name:'群 recycled'}
          };
        }""")
        result = await guard.select_row(row, BOUND_A)
        self.assertNotEqual(result, 'clicked')
        self.assertIn('identity_mismatch', result)
        self.assertEqual(await self.page.evaluate('window.mousedowns'), 0)

    async def test_select_row_rejects_duplicate_bound_id_claimants(self):
        dup = dict(GROUP_B, shortId=BOUND_A, id='0:2:10000:20009')
        rows = conv_row('群甲', GROUP_A, active=True) + conv_row('群乙', dup)
        await self._rebuild([GROUP_A, dup], GROUP_A, GROUP_A, rows_html=rows)
        row = (await self._rows()).first
        result = await guard.select_row(row, BOUND_A)
        self.assertNotEqual(result, 'clicked')
        self.assertIn('ambiguous', result)
        self.assertEqual(await self.page.evaluate('window.mousedowns'), 0)

    async def test_select_row_rejects_missing_title(self):
        # Row stays visible but its title node is hidden: identity alone is
        # not enough — the visible title must also read.
        rows = conv_row('群甲', GROUP_A, active=True)
        await self._rebuild([GROUP_A], GROUP_A, GROUP_A, rows_html=rows)
        await self.page.evaluate(
            "document.querySelector('.conversationConversationItemtitle')"
            ".style.visibility='hidden'")
        row = (await self._rows()).first
        result = await guard.select_row(row, BOUND_A)
        self.assertNotEqual(result, 'clicked')
        self.assertIn('title', result)
        self.assertEqual(await self.page.evaluate('window.mousedowns'), 0)

    async def test_select_row_rejects_row_with_zero_height(self):
        # A recycled/virtualized row that kept its fiber but lost layout is
        # not a selectable target even though its identity still reads.
        rows = conv_row('群甲', GROUP_A, active=True) + conv_row('群乙', GROUP_B)
        await self._rebuild([GROUP_A, GROUP_B], GROUP_A, GROUP_A, rows_html=rows)
        await self.page.evaluate("""() => {
          const rows = document.querySelectorAll('[data-e2e="conversation-item"]');
          rows[1].style.display = 'none';
        }""")
        row = (await self._rows()).nth(1)
        result = await guard.select_row(row, BOUND_B)
        self.assertNotEqual(result, 'clicked')
        self.assertEqual(await self.page.evaluate('window.mousedowns'), 0)

    # ---------- verify_open ----------

    async def _input(self):
        return self.page.locator('[data-e2e="msg-input"] [contenteditable="true"]')

    async def test_verify_open_accepts_bound_group_with_input(self):
        ok, reason = await guard.verify_open(self.page, await self._input(), BOUND_A)
        self.assertTrue(ok, reason)

    async def test_verify_open_header_only_allowed_without_input(self):
        ok, reason = await guard.verify_open(self.page, None, BOUND_A)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, 'header_only')

    async def test_verify_open_rejects_wrong_bound_id(self):
        ok, reason = await guard.verify_open(self.page, await self._input(), BOUND_B)
        self.assertFalse(ok)
        self.assertIn('mismatch', reason)

    async def test_verify_open_rejects_inactive_group_filter(self):
        # Flip the 群聊 tag off: only 全部 remains active.
        await self.page.evaluate("""() => {
          const tags = document.querySelectorAll('.conversationFilterTagBartagItem');
          tags.forEach(t => t.classList.remove('conversationFilterTagBartagItemActive'));
          tags[0].classList.add('conversationFilterTagBartagItemActive');
        }""")
        ok, reason = await guard.verify_open(self.page, await self._input(), BOUND_A)
        self.assertFalse(ok)
        self.assertEqual(reason, 'group_filter_inactive')

    async def test_verify_open_rejects_no_active_row(self):
        rows = conv_row('群甲', GROUP_A) + conv_row('群乙', GROUP_B)
        await self._rebuild([GROUP_A, GROUP_B], GROUP_A, GROUP_A, rows_html=rows)
        ok, reason = await guard.verify_open(self.page, await self._input(), BOUND_A)
        self.assertFalse(ok)
        self.assertIn('active_row', reason)

    async def test_verify_open_rejects_header_title_mismatch(self):
        await self.page.evaluate(
            "document.querySelector('.RightPanelHeadertitle').textContent='群乙'")
        ok, reason = await guard.verify_open(self.page, await self._input(), BOUND_A)
        self.assertFalse(ok)
        self.assertEqual(reason, 'group_header_title_mismatch')

    async def test_verify_open_rejects_header_conversation_mismatch(self):
        # Header pane still shows a *different* group object.
        await self._rebuild([GROUP_A, GROUP_B], GROUP_B, GROUP_A)
        ok, reason = await guard.verify_open(self.page, await self._input(), BOUND_A)
        self.assertFalse(ok)
        self.assertIn('header', reason)

    async def test_verify_open_rejects_input_conversation_mismatch(self):
        # Input pane bound to a different conversation object than the header.
        await self._rebuild([GROUP_A, GROUP_B], GROUP_A, GROUP_B)
        ok, reason = await guard.verify_open(self.page, await self._input(), BOUND_A)
        self.assertFalse(ok)
        self.assertIn('input', reason)

    async def test_verify_open_rejects_stale_input_node(self):
        # chat_input points at a node that is not the unique visible editor.
        stale = await self.page.evaluate_handle(
            "document.querySelector('.RightPanelHeadertitle')")
        ok, reason = await guard.verify_open(self.page, stale, BOUND_A)
        self.assertFalse(ok)
        self.assertIn('input', reason)

    async def test_verify_open_rejects_recycled_active_row(self):
        # Active row's fiber recycled to another group after selection.
        await self.page.evaluate("""() => {
          const scope = window.saasFiber.scope;
          scope.child.memoizedProps.conversation = {
            id:'0:2:10000:20009', shortId:'7691234567890123409', type:2,
            coreInfo:{name:'群 recycled'}
          };
        }""")
        ok, reason = await guard.verify_open(self.page, await self._input(), BOUND_A)
        self.assertFalse(ok)
        self.assertIn('mismatch', reason)

    async def test_verify_open_rejects_type1_active_row(self):
        rows = conv_row('好友甲', FRIEND, active=True)
        await self._rebuild([FRIEND], FRIEND, FRIEND, rows_html=rows,
                            header_name='好友甲')
        ok, reason = await guard.verify_open(self.page, await self._input(), BOUND_A)
        self.assertFalse(ok)

    async def test_unreadable_supplied_editor_does_not_degrade_to_header_only(self):
        from unittest.mock import AsyncMock, Mock
        unreadable = Mock(element_handle=AsyncMock(side_effect=RuntimeError('detached')))
        ok, _ = await guard.verify_open(self.page, unreadable, BOUND_A)
        self.assertFalse(ok)

    async def test_verify_open_never_raises_on_garbage_page(self):
        await self.page.set_content('<div id="sub-app"></div>')
        ok, reason = await guard.verify_open(self.page, None, BOUND_A)
        self.assertFalse(ok)
        self.assertTrue(reason)

    async def test_verify_open_rejects_duplicate_bound_claimants(self):
        # select_row refuses activation unless exactly one visible row claims
        # the bound id; verify_open must hold the same bar (LOGIC-2).
        dup = dict(GROUP_B, shortId=BOUND_A, id='0:2:10000:20009')
        rows = conv_row('群甲', GROUP_A, active=True) + conv_row('群乙', dup)
        await self._rebuild([GROUP_A, dup], GROUP_A, GROUP_A, rows_html=rows)
        ok, reason = await guard.verify_open(self.page, await self._input(), BOUND_A)
        self.assertFalse(ok)
        self.assertIn('ambiguous', reason)

    # ---------- read_row_metadata ----------

    async def test_read_row_metadata_returns_group_type_name_and_id(self):
        row = (await self._rows()).first
        self.assertEqual(await guard.read_row_metadata(row),
                         {'type': 2, 'name': '群甲', 'conversationId': BOUND_A})

    async def test_read_row_metadata_returns_friend_type_without_conversation_id(self):
        rows = conv_row('好友甲', FRIEND)
        await self._rebuild([FRIEND], FRIEND, FRIEND, rows_html=rows,
                            header_name='好友甲')
        row = (await self._rows()).first
        self.assertEqual(await guard.read_row_metadata(row),
                         {'type': 1, 'name': '好友甲'})

    async def test_read_row_metadata_rejects_row_without_fiber(self):
        html = ('<div id="sub-app"><div class="im-saas-viewport-X">' + FILTER_GROUP +
                '<div class="conversationConversationListwrapper">' +
                conv_row('群甲', GROUP_A) + '</div></div></div>')
        await self.page.set_content(html)
        row = (await self._rows()).first
        self.assertIsNone(await guard.read_row_metadata(row))

    async def test_read_row_metadata_rejects_detached_fiber(self):
        # An expando fiber outside the committed current tree is not proof.
        row = (await self._rows()).first
        await self.page.evaluate("""() => {
          const row = document.querySelector('[data-e2e="conversation-item"]');
          row['__reactFiber$saas'] = {tag:5, stateNode:row, return:null,
            memoizedProps:{conversation:{type:2, shortId:'7691234567890123401'}}};
        }""")
        self.assertIsNone(await guard.read_row_metadata(row))

    async def test_read_row_metadata_rejects_invalid_or_unknown_type(self):
        bad = dict(GROUP_A, shortId='123')  # group id outside the bound format
        rows = conv_row('群甲', bad, active=True)
        await self._rebuild([bad], bad, bad, rows_html=rows)
        row = (await self._rows()).first
        self.assertIsNone(await guard.read_row_metadata(row))
        # Known non-friend/non-group bundle types are reported so callers can
        # skip them provably — never authorized as either target kind.
        for kind in (3, 4, 17):
            other = dict(GROUP_A, type=kind)
            rows = conv_row('直播间' if kind == 3 else '其他', other, active=True)
            await self._rebuild([other], other, other, rows_html=rows)
            row = (await self._rows()).first
            meta = await guard.read_row_metadata(row)
            self.assertIsNotNone(meta)
            self.assertEqual(meta['type'], kind)
            self.assertNotIn('conversationId', meta)
        weird = dict(GROUP_A, type=999)  # not a known conversation type
        rows = conv_row('群甲', weird, active=True)
        await self._rebuild([weird], weird, weird, rows_html=rows)
        self.assertIsNone(await guard.read_row_metadata(row))

    # ---------- arm_enter / dispatch_was_allowed / disarm ----------

    async def _arm(self, bound_id=BOUND_A):
        return await guard.arm_enter(self.page, await self._input(), bound_id)

    async def _guard_state(self):
        return await self.page.evaluate(
            "() => { const s = window.__sparkflowGroupGuard; return s ? {"
            "fired: s.fired === true, allowed: s.allowed === true,"
            " armed: s.armed === true, reason: s.reason || ''} : null; }")

    async def _enter_probe(self):
        # Bubble-phase target listener: a blocked Enter never reaches it
        # (stopImmediatePropagation in the window capture phase), an allowed
        # one does.
        await self.page.evaluate("""() => {
          window.__enterProbe = {seen: 0};
          const ed = document.querySelector('[data-e2e="msg-input"] [contenteditable="true"]');
          ed.addEventListener('keydown', () => { window.__enterProbe.seen += 1; });
        }""")

    async def _probe_seen(self):
        return await self.page.evaluate('() => window.__enterProbe.seen')

    async def _press_enter(self):
        await (await self._input()).press('Enter')

    async def _show_counted_group(self, *, row_name='111', header='111(8)',
                                  model_name='111', count=8):
        group = dict(GROUP_A, coreInfo={'name': model_name}, participantCount=count)
        await self._rebuild([group], group, group,
                            rows_html=conv_row(row_name, group, active=True),
                            header_name=header)

    async def test_bundle_counted_group_title_authorizes_one_enter(self):
        await self._show_counted_group()
        ok, reason = await guard.verify_open(self.page, await self._input(), BOUND_A)
        self.assertTrue(ok, reason)
        ok, reason = await self._arm()
        self.assertTrue(ok, reason)
        await self._enter_probe()
        await self._press_enter()
        await self._press_enter()
        self.assertEqual(await self._probe_seen(), 1)
        self.assertTrue(await guard.dispatch_was_allowed(self.page))

    async def test_counted_header_requires_exact_name_and_committed_count(self):
        for fields in ({'header': '2111(8)'}, {'header': '111(7)'},
                       {'header': '111(08)'}, {'model_name': 'other'},
                       {'count': '8'}, {'count': 0}, {'count': None},
                       {'row_name': 'other'}, {'row_name': '111(9)', 'header': '111(8)'}):
            with self.subTest(fields=fields):
                await self._show_counted_group(**fields)
                ok, _ = await self._arm()
                self.assertFalse(ok)
                self.assertIsNone(await self._guard_state())

    async def test_counted_header_literal_parentheses_in_room_name_are_preserved(self):
        await self._show_counted_group(row_name='111(9)', model_name='111(9)',
                                       header='111(9)(8)')
        ok, reason = await self._arm()
        self.assertTrue(ok, reason)

    async def test_counted_header_change_after_arm_blocks_actual_enter(self):
        await self._show_counted_group()
        ok, reason = await self._arm()
        self.assertTrue(ok, reason)
        await self._enter_probe()
        await self.page.evaluate("document.querySelector('.RightPanelHeadertitle').textContent = '111(9)'")
        await self._press_enter()
        self.assertEqual(await self._probe_seen(), 0)
        self.assertFalse(await guard.dispatch_was_allowed(self.page))

    async def test_counted_header_model_count_change_after_arm_blocks_actual_enter(self):
        await self._show_counted_group()
        ok, reason = await self._arm()
        self.assertTrue(ok, reason)
        await self._enter_probe()
        await self.page.evaluate(
            "window.saasFiber.panel.child.child.memoizedProps.curConversation.participantCount = 9")
        await self._press_enter()
        self.assertEqual(await self._probe_seen(), 0)
        self.assertFalse(await guard.dispatch_was_allowed(self.page))

    async def test_selection_title_proof_rejects_a_stale_scan_name(self):
        await self._show_counted_group()
        ok, reason = await guard.verify_open(
            self.page, None, BOUND_A, expected_name='earlier title')
        self.assertFalse(ok)
        self.assertEqual(reason, 'group_row_title_changed')

    async def test_arm_enter_allows_bound_enter_and_consumes(self):
        ok, reason = await self._arm()
        self.assertTrue(ok, reason)
        await self._press_enter()
        state = await self._guard_state()
        self.assertIsNotNone(state)
        self.assertTrue(state['fired'])
        self.assertTrue(state['allowed'])
        self.assertTrue(await guard.dispatch_was_allowed(self.page))
        self.assertIsNone(await self._guard_state())

    async def test_arm_enter_blocks_second_enter_until_consumed(self):
        ok, reason = await self._arm()
        self.assertTrue(ok, reason)
        await self._enter_probe()
        await self._press_enter()  # allowed single shot
        self.assertEqual(await self._probe_seen(), 1)
        await self._press_enter()  # spent guard must keep blocking
        self.assertEqual(await self._probe_seen(), 1)
        state = await self._guard_state()
        self.assertTrue(state['fired'] and state['allowed'])
        self.assertTrue(await guard.dispatch_was_allowed(self.page))

    async def test_arm_enter_blocks_enter_on_wrong_target(self):
        ok, reason = await self._arm()
        self.assertTrue(ok, reason)
        await self.page.evaluate("""() => {
          document.querySelector('.RightPanelHeadertitle').dispatchEvent(
            new KeyboardEvent('keydown', {key:'Enter', bubbles:true, cancelable:true}));
        }""")
        state = await self._guard_state()
        self.assertTrue(state['fired'])
        self.assertFalse(state['allowed'])
        self.assertIn('input', state['reason'])
        # The spent shot also blocks a later Enter aimed at the real editor.
        await self._enter_probe()
        await self._press_enter()
        self.assertEqual(await self._probe_seen(), 0)
        self.assertFalse(await guard.dispatch_was_allowed(self.page))
        self.assertIsNone(await self._guard_state())

    async def test_arm_enter_blocks_after_header_input_switch(self):
        ok, reason = await self._arm()
        self.assertTrue(ok, reason)
        # Header and input panes rebind to a different conversation object.
        await self._rebuild([GROUP_A, GROUP_B], GROUP_B, GROUP_B)
        await self._press_enter()
        state = await self._guard_state()
        self.assertTrue(state['fired'])
        self.assertFalse(state['allowed'])
        self.assertFalse(await guard.dispatch_was_allowed(self.page))
        await guard.disarm(self.page)

    async def test_arm_enter_blocks_after_row_type_change(self):
        ok, reason = await self._arm()
        self.assertTrue(ok, reason)
        await self.page.evaluate("""() => {
          const conv = window.saasFiber.scope.child.memoizedProps.conversation;
          window.saasFiber.scope.child.memoizedProps.conversation =
            Object.assign({}, conv, {type: 1});
        }""")
        await self._press_enter()
        state = await self._guard_state()
        self.assertTrue(state['fired'])
        self.assertFalse(state['allowed'])
        self.assertFalse(await guard.dispatch_was_allowed(self.page))
        await guard.disarm(self.page)

    async def test_arm_enter_rejects_duplicate_bound_claimants(self):
        dup = dict(GROUP_B, shortId=BOUND_A, id='0:2:10000:20009')
        rows = conv_row('群甲', GROUP_A, active=True) + conv_row('群乙', dup)
        await self._rebuild([GROUP_A, dup], GROUP_A, GROUP_A, rows_html=rows)
        ok, reason = await self._arm()
        self.assertFalse(ok)
        self.assertIn('ambiguous', reason)
        self.assertIsNone(await self._guard_state())

    async def test_arm_enter_blocks_when_group_filter_unbound(self):
        ok, reason = await self._arm()
        self.assertTrue(ok, reason)
        await self.page.evaluate("""() => {
          const tags = document.querySelectorAll('.conversationFilterTagBartagItem');
          tags.forEach(t => t.classList.remove('conversationFilterTagBartagItemActive'));
          tags[0].classList.add('conversationFilterTagBartagItemActive');
        }""")
        await self._press_enter()
        state = await self._guard_state()
        self.assertTrue(state['fired'])
        self.assertFalse(state['allowed'])
        self.assertEqual(state['reason'], 'group_filter_inactive')
        self.assertFalse(await guard.dispatch_was_allowed(self.page))
        await guard.disarm(self.page)

    async def test_arm_enter_refuses_when_open_identity_unproven(self):
        ok, reason = await self._arm(BOUND_B)
        self.assertFalse(ok)
        self.assertTrue(reason)
        self.assertIsNone(await self._guard_state())

    async def test_dispatch_was_allowed_false_when_never_fired(self):
        ok, reason = await self._arm()
        self.assertTrue(ok, reason)
        self.assertFalse(await guard.dispatch_was_allowed(self.page))
        self.assertIsNone(await self._guard_state())

    async def test_disarm_removes_armed_guard(self):
        ok, reason = await self._arm()
        self.assertTrue(ok, reason)
        await guard.disarm(self.page)
        self.assertIsNone(await self._guard_state())
        await self._enter_probe()
        await self._press_enter()
        self.assertEqual(await self._probe_seen(), 1)


if __name__ == '__main__':
    unittest.main()
