"""Observed layout, rebuilt with synthetic IDs; never connects to Douyin."""
import unittest
from playwright.async_api import async_playwright
from core import friend_chat_guard as guard
from tests.test_friend_chat_guard import HTML

ACCOUNT={'target_uid_bindings':{'demo-A':{'uid':'10001','secUid':'demo-sec-A'},'demo-B':{'uid':'10002','secUid':'demo-sec-B'}}}
SETUP=r'''() => {
 const rows=Array.from(document.querySelectorAll('li'));
 const header=document.querySelector('[class*=box-header-]'),input=document.querySelector('[contenteditable]');
 for(const n of [...rows,header,input])for(const a of ['data-user-id','data-conversation-id'])n.removeAttribute(a);
 const tail=document.createElement('div');tail.className='box-header-tail-demo';tail.textContent='controls';header.append(tail);
 const conversations=[{id:'0:1:10000:10001',shortId:'7691234567890123401',secUid:'demo-sec-A'},
                      {id:'0:1:10000:10002',shortId:'7691234567890123402',secUid:'demo-sec-B'}];
 const root={tag:3,return:null,stateNode:{},memoizedProps:{}};root.stateNode.current=root;
 const context={};
 const provider={tag:10,type:{_context:context},return:root,memoizedProps:{value:{conversation:conversations[0],checkboxState:{allList:conversations}}}};
 function host(node,parent,props={}){const fiber={tag:5,stateNode:node,return:parent,memoizedProps:props};node['__reactFiber$observed']=fiber;return fiber;}
 const rowParents=rows.map((row,i)=>{const parent={tag:0,return:root,memoizedProps:{header:{props:{conversation:conversations[i]}}}};parent.child=host(row,parent);return parent});
 function paneChain(node,depth){let child=host(node,null);const own=child;for(let i=1;i<depth;i++){const p={tag:0,return:null,child:child,memoizedProps:{}};child.return=p;child=p}child.return=provider;return {own,top:child}}
 const h=paneChain(header,6),i=paneChain(input,9);
 provider.child=h.top;h.top.sibling=i.top;
 root.child=rowParents[0];rowParents[0].sibling=rowParents[1];rowParents[1].sibling=provider;
 window.observed={root,provider,context,conversations,header,input,inputFiber:i.own,headerFiber:h.own};
}'''

class ObservedFriendChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.launch(args=['--no-sandbox','--disable-dev-shm-usage'])
        self.page=await self.browser.new_page()
        await self.page.route('**/*',lambda r:r.abort())
        await self.page.set_content(HTML)
        await self.page.evaluate(SETUP)

    async def asyncTearDown(self):
        await self.browser.close();await self.pw.stop()

    async def select(self,name):
        return await guard.select_friend(self.page,self.page.locator('li').filter(has_text=name),ACCOUNT,name)

    async def test_explicit_current_provider_and_header_tail_are_supported(self):
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True))[0])
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'),1)

    async def test_stale_input_props_cannot_replace_missing_current_conversation(self):
        self.assertTrue((await self.select('demo-A'))[0])
        await self.page.evaluate('''() => {const s=window.observed;
          delete s.provider.memoizedProps.value.conversation;
          s.inputFiber.memoizedProps.conversation=s.conversations[0];
        }''')
        ok, reason = await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True)
        self.assertFalse(ok, 'input-local props must not replace the current pane proof')
        self.assertEqual(reason, 'friend_open_identity_unavailable')
        self.assertFalse(await guard.dispatch_was_allowed(self.page))

    async def test_all_list_membership_without_current_conversation_is_rejected(self):
        self.assertTrue((await self.select('demo-A'))[0])
        await self.page.evaluate('delete window.observed.provider.memoizedProps.value.conversation')
        ok, reason = await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_open_identity_unavailable')

    async def test_active_row_b_but_current_pane_a_is_rejected(self):
        self.assertTrue((await self.select('demo-B'))[0])
        # Pane keeps B's shortId/secUid but is still bound to A's thread id.
        await self.page.evaluate('''() => {const s=window.observed;s.header.firstChild.textContent='demo-B';
          s.provider.memoizedProps.value={conversation:{id:s.conversations[0].id,shortId:s.conversations[1].shortId,secUid:s.conversations[1].secUid},checkboxState:{allList:s.conversations}};
        }''')
        ok, reason = await guard.verify_friend(self.page,ACCOUNT,'demo-B')
        self.assertFalse(ok)
        self.assertEqual(reason, 'friend_current_conversation_mismatch')

    async def test_stale_input_context_dependency_is_rejected(self):
        self.assertTrue((await self.select('demo-B'))[0])
        await self.page.evaluate('''() => {const s=window.observed;s.header.firstChild.textContent='demo-B';
          s.provider.memoizedProps.value={conversation:s.conversations[1],checkboxState:{allList:s.conversations}};
          s.inputFiber.return.dependencies={firstContext:{context:s.context,memoizedValue:{conversation:s.conversations[0]},next:null}};
        }''')
        ok, reason = await guard.verify_friend(self.page,ACCOUNT,'demo-B')
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_stale_input_context')

    async def test_large_committed_tree_is_not_mistaken_for_a_stale_node(self):
        self.assertTrue((await self.select('demo-A'))[0])
        # 7000 extra fibers ahead of our nodes in tree order: a bounded
        # full-tree scan would wrongly call every chat uncommitted.
        await self.page.evaluate('''() => {const s=window.observed;const head=s.root.child;let first=null,prev=null;
          for(let i=0;i<7000;i++){const f={tag:5,return:s.root,stateNode:document.createElement('i'),memoizedProps:{}};
            if(prev)prev.sibling=f;else first=f;prev=f;}
          s.root.child=first;prev.sibling=head;
        }''')
        ok, reason = await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True)
        self.assertTrue(ok, reason)
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'),1)

    async def test_alternate_root_tree_is_rejected(self):
        self.assertTrue((await self.select('demo-A'))[0])
        await self.page.evaluate('''() => {const s=window.observed;
          const alt={tag:3,stateNode:s.root.stateNode,child:null,memoizedProps:{}};
          s.root.stateNode.current=alt;}''')
        ok, reason = await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertFalse(ok)
        # Whichever identity check fires first, a stale alternate tree must
        # never be treated as a sendable chat.
        self.assertIn(reason,{'friend_row_uncommitted','friend_uncommitted_pane','friend_active_identity_mismatch'})

    async def test_input_not_in_committed_react_tree_is_rejected(self):
        self.assertTrue((await self.select('demo-A'))[0])
        # Node keeps an attached fiber that is no longer in the committed tree
        # (React alternate/replacement), so only the commit check can reject it.
        await self.page.evaluate('''() => {const old=document.querySelector('[contenteditable]');
          const fresh=old.cloneNode(true);const key=Object.keys(old).find(k=>k.startsWith('__reactFiber'));
          fresh[key]=old[key];fresh.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.defaultPrevented)window.sent++});
          old.replaceWith(fresh);}''')
        ok, reason = await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason, 'friend_uncommitted_pane')
        # Arming is what installs the Enter interceptor; the sender only reaches
        # Enter after a successful verify, so an unarmed page is not evidence.
        ok, reason = await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True)
        self.assertFalse(ok)

    async def test_armed_guard_blocks_enter_when_pane_changes_after_arming(self):
        self.assertTrue((await self.select('demo-A'))[0])
        ok, reason = await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True)
        self.assertTrue(ok, reason)
        # The pane flips to another conversation between arming and Enter.
        await self.page.evaluate('''() => {const s=window.observed;
          s.header.firstChild.textContent='demo-A';
          s.provider.memoizedProps.value={conversation:s.conversations[1],checkboxState:{allList:s.conversations}};
        }''')
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'),0)
        self.assertFalse(await guard.dispatch_was_allowed(self.page))

    async def test_consuming_blocked_dispatch_removes_the_listener(self):
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True))[0])
        await self.page.evaluate("document.body.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}))")
        self.assertFalse(await guard.dispatch_was_allowed(self.page))
        await guard.disarm(self.page)
        blocked = await self.page.evaluate("""() => {
          const e=new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true});
          document.body.dispatchEvent(e);return e.defaultPrevented;
        }""")
        self.assertFalse(blocked, 'the consumed guard left an unreachable capture listener')

    async def test_dispatch_proof_is_consumed_by_one_press(self):
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True))[0])
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'),1)
        self.assertTrue(await guard.dispatch_was_allowed(self.page))
        # A stale "allowed" must never authorize a later unarmed press.
        self.assertFalse(await guard.dispatch_was_allowed(self.page))

    async def test_stray_enter_elsewhere_does_not_spend_the_guard(self):
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True))[0])
        await self.page.evaluate("document.body.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
        self.assertEqual(await self.page.evaluate('window.sent'),0)
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'),1)
        self.assertTrue(await guard.dispatch_was_allowed(self.page))

    async def test_unreadable_page_blocks_enter_instead_of_failing_open(self):
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True))[0])
        await self.page.evaluate('delete window.getComputedStyle')
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'),0)
        self.assertFalse(await guard.dispatch_was_allowed(self.page))

    async def test_header_showing_another_name_is_a_conflict(self):
        self.assertTrue((await self.select('demo-A'))[0])
        await self.page.evaluate("window.observed.header.firstChild.textContent='demo-B'")
        ok, reason = await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_header_name_mismatch')
        self.assertEqual(guard.FriendIdentityError('friend_identity_unavailable: '+reason).category,
                         'friend_identity_mismatch')

    async def test_header_tail_text_cannot_name_the_chat(self):
        self.assertTrue((await self.select('demo-A'))[0])
        await self.page.evaluate('''() => {const s=window.observed;
          s.header.firstChild.textContent='demo-B';
          const tail=s.header.querySelector('[class*=box-header-tail-]');
          tail.textContent='demo-A';
        }''')
        ok, reason = await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_header_name_mismatch')

    async def test_numeric_conversation_id_does_not_conflict_with_short_id(self):
        # Same conversation object the row points at, but its id is numeric:
        # id and shortId must not collide inside one identity set.
        await self.page.evaluate("window.observed.conversations[0].id='7691234567890123499'")
        self.assertTrue((await self.select('demo-A'))[0])
        ok, reason = await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertTrue(ok, reason)

    async def test_two_separate_providers_cannot_jointly_prove_one_chat(self):
        self.assertTrue((await self.select('demo-A'))[0])
        await self.page.evaluate('''() => {const s=window.observed;let top=s.inputFiber;while(top.return!==s.provider)top=top.return;
          s.provider.child.sibling=null;
          const other={tag:10,type:s.provider.type,return:s.root,child:top,memoizedProps:s.provider.memoizedProps};
          top.return=other;s.provider.sibling=other;
        }''')
        ok, reason = await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_pane_scope_mismatch')

    async def test_real_virtual_scroll_recycles_same_name_to_other_identity(self):
        await self.page.set_content('''<div id="sub-app"><ul style="height:80px;overflow:auto"><div style="height:400px" id="spacer"></div></ul></div><script>
          window.clicked=0;const ul=document.querySelector('ul');
          function render(){const old=ul.querySelector('li');if(old)old.remove();const second=ul.scrollTop>50;
            const row=document.createElement('li');row.className='semi-list-item';row.innerHTML='<span class="item-header-name-demo">demo-A</span>';
            row['__reactFiber$demo']={tag:5,stateNode:row,memoizedProps:{header:{props:{conversation:{shortId:second?'7691234567890123402':'7691234567890123401',secUid:second?'demo-sec-B':'demo-sec-A'}}}}};
            row.querySelector('span').onclick=()=>window.clicked++;ul.insertBefore(row,ul.firstChild);
          }ul.addEventListener('scroll',render);render();</script>''')
        locator=self.page.locator('li').first
        await self.page.evaluate("document.querySelector('ul').scrollTop=120;document.querySelector('ul').dispatchEvent(new Event('scroll'))")
        selected, reason = await guard.select_friend(self.page,locator,ACCOUNT,'demo-A')
        self.assertFalse(selected)
        self.assertEqual(reason,'friend_row_identity_mismatch')
        self.assertEqual(await self.page.evaluate('window.clicked'),0)

    async def test_valid_switch_uses_current_conversation_not_list_order(self):
        self.assertTrue((await self.select('demo-B'))[0])
        await self.page.evaluate('''() => {const s=window.observed;s.header.firstChild.textContent='demo-B';
          s.provider.memoizedProps.value={conversation:s.conversations[1],checkboxState:{allList:[s.conversations[0],s.conversations[1]]}};
        }''')
        self.assertTrue((await guard.verify_friend(self.page,ACCOUNT,'demo-B',arm=True))[0])
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'),1)
