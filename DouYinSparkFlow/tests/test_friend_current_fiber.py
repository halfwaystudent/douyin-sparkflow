"""Current-tree ancestry regressions derived from the live switch diagnostics.

All identities and fibers are synthetic. No site or persistent browser profile.
"""
import unittest
from playwright.async_api import async_playwright
from core import friend_chat_guard as guard
from tests.test_friend_chat_guard import HTML
from tests.test_friend_chat_observed_shape import ACCOUNT, SETUP


class CurrentFiberTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_input_expando_alternate_resolves_to_reachable_current_host(self):
        await self.page.evaluate('''() => {const s=window.observed;
          const oldRoot={...s.root,child:null};
          const oldProvider={...s.provider,return:oldRoot,memoizedProps:{value:{conversation:s.conversations[1]}}};
          const old={...s.inputFiber,return:oldProvider,alternate:s.inputFiber};
          s.inputFiber.alternate=old;s.input.__reactFiber$observed=old;
        }''')
        ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True)
        self.assertTrue(ok,reason)
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'),1)

    async def test_current_host_with_stale_return_links_uses_traversal_ancestry(self):
        await self.page.evaluate('''() => {const s=window.observed;
          const oldRoot={...s.root,child:null};
          const oldProvider={...s.provider,return:oldRoot,memoizedProps:{value:{conversation:s.conversations[1]}}};
          // The current graph still reaches these exact hosts via s.provider,
          // but the hosts' own return links lead to a stale provider generation.
          s.inputFiber.return=oldProvider;s.headerFiber.return=oldProvider;
        }''')
        ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True)
        self.assertTrue(ok,reason)
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'),1)

    async def test_row_old_expando_props_are_not_used_when_current_host_exists(self):
        await self.page.evaluate('''() => {const s=window.observed,row=document.querySelector('li');
          const own=row.__reactFiber$observed;
          const oldParent={return:s.root,memoizedProps:{header:{props:{conversation:s.conversations[1]}}}};
          row.__reactFiber$observed={...own,return:oldParent,alternate:own};
        }''')
        ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertTrue(ok,reason)

    async def test_old_expando_cannot_hide_a_conflicting_current_conversation(self):
        await self.page.evaluate('''() => {const s=window.observed;
          const oldRoot={...s.root,child:null};
          const oldProvider={...s.provider,return:oldRoot};
          s.input.__reactFiber$observed={...s.inputFiber,return:oldProvider,alternate:s.inputFiber};
          s.provider.memoizedProps={value:{conversation:s.conversations[1]}};
        }''')
        ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True)
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_current_conversation_mismatch')
        self.assertFalse(await guard.dispatch_was_allowed(self.page))

    async def test_current_consumer_conflict_cannot_be_hidden_by_old_return_links(self):
        await self.page.evaluate('''() => {const s=window.observed;
          const currentParent=s.inputFiber.return;
          currentParent.dependencies={firstContext:{context:s.context,memoizedValue:{conversation:s.conversations[1]},next:null}};
          s.inputFiber.return={...s.provider,return:s.root};
        }''')
        ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_stale_input_context')

    async def test_cyclic_current_graph_is_unreadable_not_authorized(self):
        await self.page.evaluate('window.observed.provider.sibling=window.observed.provider')
        ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_row_uncommitted')

    async def test_enter_reindexes_current_graph_even_when_the_root_object_is_reused(self):
        self.assertTrue((await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True))[0])
        await self.page.evaluate('''() => {const s=window.observed;
          // An even number of React commits may reuse the same root object.
          // Install a different current graph beneath it, retaining the DOM.
          const p={...s.provider,memoizedProps:{value:{conversation:s.conversations[1]}}};
          const h={...s.headerFiber,return:p},i={...s.inputFiber,return:p};
          h.sibling=i;p.child=h;
          s.root.child.sibling.sibling=p;
          s.header.__reactFiber$observed=h;s.input.__reactFiber$observed=i;
        }''')
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(await self.page.evaluate('window.sent'),0,
                         'the Enter check reused an arm-time graph index')
        self.assertFalse(await guard.dispatch_was_allowed(self.page))

    async def test_graph_beyond_budget_is_unreadable_not_partially_authorized(self):
        await self.page.evaluate('''() => {const s=window.observed;let tail=s.provider;
          for(let n=0;n<30001;n++){const next={return:s.root,memoizedProps:{}};tail.sibling=next;tail=next;}
        }''')
        ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A')
        self.assertFalse(ok)
        self.assertEqual(reason,'friend_row_uncommitted')

if __name__=='__main__':
    unittest.main()
