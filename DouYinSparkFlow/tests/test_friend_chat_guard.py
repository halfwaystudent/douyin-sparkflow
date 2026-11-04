"""Real local-page tests with synthetic identities; all network is aborted."""
import unittest
from playwright.async_api import async_playwright

HTML = '''<div id="sub-app">
<ul><li class="semi-list-item active-demo" data-user-id="10001" data-conversation-id="7691234567890123401"><span class="item-header-name-demo">demo-A</span></li>
<li class="semi-list-item" data-user-id="10002" data-conversation-id="7691234567890123402"><span class="item-header-name-demo">demo-B</span></li></ul>
<div class="box-header-demo" data-user-id="10001" data-conversation-id="7691234567890123401">demo-A</div>
<div contenteditable="true" data-user-id="10001" data-conversation-id="7691234567890123401">synthetic message</div></div>
<script>window.sent=0;window.clicked=0;
for (const row of document.querySelectorAll('li')) row.querySelector('span').onclick=()=>{window.clicked++;for(const r of document.querySelectorAll('li'))r.classList.remove('active-demo');row.classList.add('active-demo')};
document.querySelector('[contenteditable]').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.defaultPrevented)window.sent++});</script>'''
ACCOUNT = {'target_uid_bindings': {'demo-A': {'uid':'10001','secUid':'demo-sec-A'},
                                   'demo-B': {'uid':'10002','secUid':'demo-sec-B'}}}


# The previous attribute-only fixture is not sufficient authorization anymore.
# Keep attributes as contradictory evidence, with a committed current-pane scope
# required for every positive send (same rule as the observed-shape tests).
PANE_SETUP = r"""() => {
 const root={stateNode:{},return:null};root.stateNode.current=root;
 const provider={tag:10,type:{_context:{}},return:root,memoizedProps:{value:{conversation:{
   shortId:'7691234567890123401',secUid:'demo-sec-A'}}}};
 const host=node=>{const f={stateNode:node,return:provider,memoizedProps:{}};node.__reactFiber$test=f;return f;};
 const header=host(document.querySelector('[class*=box-header-]'));
 const input=host(document.querySelector('[contenteditable]'));
 root.child=provider;provider.child=header;header.sibling=input;
 window.paneProvider=provider;
}"""


class FriendChatGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(args=['--no-sandbox','--disable-dev-shm-usage'])
        self.page = await self.browser.new_page()
        await self.page.route('**/*', lambda route: route.abort())
        await self.page.set_content(HTML)
        await self.page.evaluate(PANE_SETUP)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def select(self, name):
        from core import friend_chat_guard as guard
        row = self.page.locator('li').filter(has_text=name)
        return await guard.select_friend(self.page, row, ACCOUNT, name)

    async def test_wrong_row_identity_is_not_clicked(self):
        from core import friend_chat_guard as guard
        result = await guard.select_friend(self.page, self.page.locator('li').nth(1), ACCOUNT, 'demo-A')
        self.assertFalse(result[0])
        self.assertEqual(await self.page.evaluate('window.clicked'), 0)

    async def test_previous_chat_input_does_not_prove_new_chat_ready(self):
        from core import friend_chat_guard as guard
        self.assertTrue((await self.select('demo-B'))[0])
        ok, _ = await guard.verify_friend(self.page, ACCOUNT, 'demo-B')
        self.assertFalse(ok)

    async def test_correct_identity_allows_one_enter(self):
        from core import friend_chat_guard as guard
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo-A', arm=True))[0])
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'), 1)

    async def test_switch_after_final_check_is_blocked_at_keydown(self):
        from core import friend_chat_guard as guard
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo-A', arm=True))[0])
        await self.page.evaluate("document.querySelector('[contenteditable]').dataset.userId='10002'")
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'), 0)

    async def test_enter_on_an_unrelated_input_is_not_authorized(self):
        from core import friend_chat_guard as guard
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo-A', arm=True))[0])
        await self.page.evaluate("const input=document.createElement('div');input.id='unrelated';input.contentEditable='true';document.body.append(input);input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.defaultPrevented)window.sent++})")
        await self.page.locator('#unrelated').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'), 0)
        self.assertFalse(await guard.dispatch_was_allowed(self.page))

    async def test_replaced_input_requires_new_authorization(self):
        from core import friend_chat_guard as guard
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo-A', arm=True))[0])
        await self.page.evaluate("const old=document.querySelector('[contenteditable]');const fresh=old.cloneNode(true);old.replaceWith(fresh);fresh.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.defaultPrevented)window.sent++})")
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'), 0)

    async def test_shared_ancestor_identity_cannot_prove_old_input_ready(self):
        from core import friend_chat_guard as guard
        self.assertTrue((await self.select('demo-B'))[0])
        await self.page.evaluate("""() => {
          const header=document.querySelector('[class*=box-header-]');header.textContent='demo-B';header.dataset.userId='10002';header.dataset.conversationId='7691234567890123402';
          const input=document.querySelector('[contenteditable]');input.removeAttribute('data-user-id');input.removeAttribute('data-conversation-id');
          input['__reactFiber$demo']={memoizedProps:{},return:{memoizedProps:{conversation:{id:'7691234567890123402',peerUserId:'10002'}}}};
        }""")
        self.assertFalse((await guard.verify_friend(self.page, ACCOUNT, 'demo-B'))[0])

    async def test_selection_evaluate_failure_is_identity_error(self):
        from core import friend_chat_guard as guard
        from unittest.mock import AsyncMock
        row=AsyncMock()
        row.evaluate.side_effect=RuntimeError('synthetic DOM unavailable')
        with self.assertRaises(guard.FriendIdentityError):
            await guard.select_friend(self.page, row, ACCOUNT, 'demo-A')

    async def test_same_page_switches_from_a_to_b_only_after_pane_is_ready(self):
        from core import friend_chat_guard as guard
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo-A', arm=True))[0])
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertTrue((await self.select('demo-B'))[0])
        await self.page.evaluate("setTimeout(()=>{const h=document.querySelector('[class*=box-header-]');h.textContent='demo-B';for(const e of [h,document.querySelector('[contenteditable]')]){e.dataset.userId='10002';e.dataset.conversationId='7691234567890123402'};window.paneProvider.memoizedProps.value.conversation={shortId:'7691234567890123402',secUid:'demo-sec-B'}},50)")
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo-B', wait=True))[0])
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo-B', arm=True))[0])
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'), 2)

    async def test_detached_input_props_cannot_replace_current_pane(self):
        from core import friend_chat_guard as guard
        self.assertTrue((await self.select('demo-A'))[0])
        await self.page.evaluate("const i=document.querySelector('[contenteditable]');i.removeAttribute('data-user-id');i.removeAttribute('data-conversation-id');i['__reactFiber$test']={memoizedProps:{conversation:{id:'7691234567890123401',peerUserId:'10001'}}}")
        self.assertFalse((await guard.verify_friend(self.page, ACCOUNT, 'demo-A'))[0])

    async def test_recycled_row_with_same_name_wrong_id_is_not_clicked(self):
        from core import friend_chat_guard as guard
        row=self.page.locator('li').first
        await self.page.evaluate("document.querySelector('li').dataset.userId='99999'")
        self.assertFalse((await guard.select_friend(self.page, row, ACCOUNT, 'demo-A'))[0])
        self.assertEqual(await self.page.evaluate('window.clicked'), 0)

    async def test_missing_opened_identity_never_falls_back_to_name(self):
        from core import friend_chat_guard as guard
        self.assertTrue((await self.select('demo-A'))[0])
        await self.page.evaluate("for(const e of document.querySelectorAll('[contenteditable], [class*=box-header-]')) {e.removeAttribute('data-user-id');e.removeAttribute('data-conversation-id')};delete window.paneProvider.memoizedProps.value.conversation")
        self.assertFalse((await guard.verify_friend(self.page, ACCOUNT, 'demo-A'))[0])

    async def test_duplicate_visible_name_is_rejected(self):
        from core import friend_chat_guard as guard
        await self.page.evaluate("const li=document.querySelector('li').cloneNode(true);li.dataset.userId='99999';document.querySelector('ul').append(li)")
        self.assertFalse((await guard.select_friend(self.page, self.page.locator('li').first, ACCOUNT, 'demo-A'))[0])

    async def test_unbound_target_never_uses_name_only(self):
        from core import friend_chat_guard as guard
        self.assertFalse((await guard.select_friend(self.page, self.page.locator('li').first, {}, 'demo-A'))[0])
