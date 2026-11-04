"""Actual pinned React18 commit cycles, local assets + synthetic recipient sink.

Set SPARKFLOW_REACT18_FIXTURES to an integrity-verified local directory containing
react.js and react-dom.js (official React 18.2 UMD builds). No browser networking.
"""
import os
import unittest
from pathlib import Path
from playwright.async_api import async_playwright
from core import friend_chat_guard as guard
from tests.test_friend_chat_observed_shape import ACCOUNT

ASSETS=Path(os.environ.get('SPARKFLOW_REACT18_FIXTURES','/nonexistent/react18'))
APP=r'''() => {
 const e=React.createElement;
 const conversations=[{id:'0:1:10000:10001',shortId:'7691234567890123401',secUid:'demo-sec-A',name:'demo-A'},
   {id:'0:1:10000:10002',shortId:'7691234567890123402',secUid:'demo-sec-B',name:'demo-B'}];
 const C=React.createContext(null);window.actualSent=[];
 function Label({conversation}){return e('span',{className:'item-header-name-test'},conversation.name)}
 function Row({header,onClick,active}){return e('li',{className:'semi-list-item'+(active?' active-test':''),onClick},header)}
 function Header(){const {conversation:c}=React.useContext(C);return e('div',{className:'box-header-test'},
   e('span',null,c.name),e('div',{className:'box-header-tail-test'},'controls'))}
 function Input(){const {conversation:c}=React.useContext(C);return e('div',{
   contentEditable:true,suppressContentEditableWarning:true,onKeyDown:event=>{
    if(event.key==='Enter'&&!event.defaultPrevented){event.preventDefault();window.actualSent.push(c.secUid)}
   }},'synthetic message')}
 function App(){const [c,set]=React.useState(conversations[0]),[tick,bump]=React.useState(0);
   window.rerender=()=>bump(n=>n+1);
   return e('div',null,e('ul',null,conversations.map(x=>e(Row,{key:x.id,active:x.id===c.id,
     header:e(Label,{conversation:x}),onClick:()=>set(x)}))),
     e(C.Provider,{value:{conversation:c}},e(Header),e(Input)),e('i',null,String(tick)));
 }
 ReactDOM.createRoot(document.querySelector('#sub-app')).render(e(App));
}'''


@unittest.skipUnless((ASSETS/'react.js').exists() and (ASSETS/'react-dom.js').exists(),
                     'optional pinned React18 local assets not supplied')
class ActualReactCommitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.launch(args=['--no-sandbox','--disable-dev-shm-usage'])
        self.page=await self.browser.new_page()
        await self.page.route('**/*',lambda route:route.abort())
        await self.page.set_content('<div id="sub-app"></div>')
        await self.page.add_script_tag(path=str(ASSETS/'react.js'))
        await self.page.add_script_tag(path=str(ASSETS/'react-dom.js'))
        await self.page.evaluate(APP)
        await self.page.locator('li').first.wait_for(state='visible')

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_actual_react_a_b_a_dispatch_follows_each_committed_recipient(self):
        for name in ['demo-A','demo-B','demo-A']:
            ok,reason=await guard.select_friend(self.page,self.page.locator('li').filter(has_text=name),ACCOUNT,name)
            self.assertTrue(ok,reason)
            ok,reason=await guard.verify_friend(self.page,ACCOUNT,name,wait=True,arm=True)
            self.assertTrue(ok,reason)
            await self.page.locator('[contenteditable]').press('Enter')
            self.assertTrue(await guard.dispatch_was_allowed(self.page))
        self.assertEqual(await self.page.evaluate('window.actualSent'),
                         ['demo-sec-A','demo-sec-B','demo-sec-A'])

    async def test_actual_react_unrelated_commits_do_not_invalidate_the_host(self):
        ok,reason=await guard.select_friend(self.page,self.page.locator('li').first,ACCOUNT,'demo-A')
        self.assertTrue(ok,reason)
        for i in range(4):
            await self.page.evaluate('window.rerender()')
            await self.page.wait_for_function('n=>document.querySelector("i").textContent===String(n)',arg=i+1)
            ok,reason=await guard.verify_friend(self.page,ACCOUNT,'demo-A',arm=True)
            self.assertTrue(ok,reason)
            await self.page.locator('[contenteditable]').press('Enter')
            self.assertTrue(await guard.dispatch_was_allowed(self.page))
        self.assertEqual(await self.page.evaluate('window.actualSent'),['demo-sec-A']*4)

if __name__=='__main__':
    unittest.main()
