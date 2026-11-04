"""Offline Chromium regressions for incomplete/replaced/unresponsive chat DOM.

Only synthetic identities and a local Enter sink are used. The DOM/fiber fixture
models observed shapes; it is not a capture or proof of the live incident's cause.
No account, cookies, navigation, external assets, or production settings are used.
"""
import asyncio
import unittest

from playwright.async_api import async_playwright

from core import friend_chat_guard as guard
from core import tasks
from tests.test_friend_chat_guard import HTML
from tests.test_friend_chat_observed_shape import ACCOUNT, SETUP


class BrowserRenderingReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(args=[
            '--no-sandbox', '--disable-dev-shm-usage',
            '--disable-background-networking', '--disable-component-update',
            '--host-resolver-rules=MAP * ~NOTFOUND',
        ])
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        self.addAsyncCleanup(self.context.close)
        self.requests = []

        async def deny_network(route):
            self.requests.append(route.request.url)
            await route.abort()

        await self.context.route('**/*', deny_network)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(2000)

    async def asyncTearDown(self):
        self.assertEqual([], self.requests, 'an offline rendering fixture attempted network access')

    async def load_chat(self):
        html = HTML.replace('window.sent=0;', 'window.sent=0;window.sentTargets=[];')
        html = html.replace('window.sent++',
            '(window.sent++,window.sentTargets.push(window.observed.provider.memoizedProps.value.conversation.secUid))')
        await self.page.set_content(html)
        await self.page.evaluate(SETUP)

    async def select(self, target):
        return await guard.select_friend(
            self.page, self.page.locator('li').filter(has_text=target), ACCOUNT, target)

    async def wait_for_rows(self, *, timeout=2.5):
        return await tasks._wait_for_friend_list_ready(
            self.page, 'synthetic-render', ['#sub-app li.semi-list-item'],
            ['#empty'], ['#loading'], timeout_seconds=timeout,
            empty_grace_seconds=timeout)

    async def test_delayed_list_mount_is_not_mistaken_for_ready_empty_dom(self):
        await self.page.set_content('''<div id="sub-app"><div id="loading">loading</div><ul></ul></div>
        <script>window.renderDone=false;window.finishRender=()=>{
          document.querySelector('ul').innerHTML='<li class="semi-list-item">synthetic friend</li>';
          document.querySelector('#loading').remove();window.renderDone=true;
        };</script>''')
        waiter = asyncio.create_task(self.wait_for_rows())
        try:
            await self.page.wait_for_timeout(75)
            self.assertFalse(waiter.done(), 'blank DOM was prematurely accepted as ready')
            self.assertFalse(await self.page.evaluate('window.renderDone'))
            await self.page.evaluate('window.finishRender()')
            selector, rows = await asyncio.wait_for(waiter, timeout=4)
            self.assertTrue(await self.page.evaluate('window.renderDone'))
            self.assertEqual('#sub-app li.semi-list-item', selector)
            self.assertEqual(1, await rows.count())
            self.assertTrue(await rows.first.is_visible())
        finally:
            if not waiter.done():
                waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def test_hidden_rows_are_not_ready_until_the_render_makes_them_visible(self):
        await self.page.set_content('''<div id="sub-app"><ul>
          <li class="semi-list-item" style="display:none">synthetic friend</li></ul></div>
          <script>window.renderDone=false;window.finishRender=()=>{
            document.querySelector('li').style.display='list-item';window.renderDone=true;
          };</script>''')
        waiter = asyncio.create_task(self.wait_for_rows())
        try:
            await self.page.wait_for_timeout(75)
            self.assertFalse(waiter.done(), 'a hidden row was prematurely accepted as ready')
            self.assertFalse(await self.page.locator('li').is_visible())
            await self.page.evaluate('window.finishRender()')
            _, rows = await asyncio.wait_for(waiter, timeout=4)
            self.assertTrue(await self.page.evaluate('window.renderDone'))
            self.assertTrue(await rows.first.is_visible())
        finally:
            if not waiter.done():
                waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def test_permanent_skeleton_reports_list_unavailable_not_login_or_refusal(self):
        await self.page.set_content('<div id="sub-app"><div id="loading">loading</div><ul></ul></div>')
        with self.assertRaises(tasks.FriendListUnavailableError) as caught:
            await asyncio.wait_for(self.wait_for_rows(timeout=.1), timeout=3)
        self.assertIn('friend_list_unavailable', str(caught.exception))
        self.assertNotIn('login_required', str(caught.exception))
        self.assertNotIn('8610', str(caught.exception))
        self.assertEqual(0, await self.page.locator('li').count())

    async def test_row_hidden_during_repaint_matches_row_unavailable_without_click(self):
        await self.load_chat()
        await self.page.locator('li').first.evaluate("row => row.style.display='none'")
        ok, reason = await self.select('demo-A')
        self.assertFalse(ok)
        self.assertEqual('friend_row_unavailable', reason)
        self.assertEqual(0, await self.page.evaluate('window.clicked'))
        self.assertEqual([], await self.page.evaluate('window.sentTargets'))
        # A later explicit selection may succeed after rendering recovers. The
        # test does not add an automatic retry to production behavior.
        await self.page.locator('li').first.evaluate("row => row.style.display='list-item'")
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo-A', arm=True))[0])
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertTrue(await guard.dispatch_was_allowed(self.page))
        self.assertEqual(['demo-sec-A'], await self.page.evaluate('window.sentTargets'))

    async def test_list_reordering_invalidates_a_positional_row_before_selection(self):
        await self.load_chat()
        old_position = self.page.locator('li').first
        # A reply/list update moves A while the saved positional locator now
        # points at B. The browser DOM is real; no locator/evaluate is mocked.
        await self.page.evaluate("document.querySelector('ul').append(document.querySelector('li'))")
        ok, reason = await guard.select_friend(self.page, old_position, ACCOUNT, 'demo-A')
        self.assertFalse(ok)
        self.assertEqual('friend_row_name_mismatch', reason)
        self.assertEqual(0, await self.page.evaluate('window.clicked'))
        self.assertEqual([], await self.page.evaluate('window.sentTargets'))
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo-A', arm=True))[0])
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(['demo-sec-A'], await self.page.evaluate('window.sentTargets'))

    async def test_pane_wait_does_not_arm_old_chat_until_render_commit_is_complete(self):
        await self.load_chat()
        self.assertTrue((await self.select('demo-B'))[0])
        wait = asyncio.create_task(guard.verify_friend(self.page, ACCOUNT, 'demo-B', wait=True, arm=True))
        try:
            await self.page.wait_for_timeout(75)
            self.assertFalse(wait.done(), 'the old pane was treated as the new chat')
            self.assertFalse(await self.page.evaluate('Boolean(window.__sparkflowFriendGuard?.armed)'))
            self.assertEqual([], await self.page.evaluate('window.sentTargets'))
            await self.page.evaluate('''() => {
              const s=window.observed;s.header.firstChild.textContent='demo-B';
              s.provider.memoizedProps.value.conversation=s.conversations[1];
            }''')
            ok, reason = await asyncio.wait_for(wait, timeout=2)
            self.assertTrue(ok, reason)
            await self.page.locator('[contenteditable]').press('Enter')
            self.assertTrue(await guard.dispatch_was_allowed(self.page))
            self.assertEqual(['demo-sec-B'], await self.page.evaluate('window.sentTargets'))
        finally:
            if not wait.done():
                wait.cancel()
            await asyncio.gather(wait, return_exceptions=True)

    async def assert_named_timeout(self, name, operation):
        try:
            await asyncio.wait_for(tasks._run_browser_stage(
                name, operation, timeout_seconds=.15, cancel_grace_seconds=.025), timeout=2)
        except tasks.BrowserStageTimeout as exc:
            self.assertEqual(name, exc.stage)
        except asyncio.TimeoutError:
            self.fail('renderer operation escaped its named stage timeout')
        else:
            self.fail('unready renderer was not rejected by its named stage timeout')

    async def test_pane_that_never_finishes_rendering_exits_without_enter(self):
        await self.load_chat()
        self.assertTrue((await self.select('demo-B'))[0])
        await self.assert_named_timeout('render_pane_readiness',
            guard.verify_friend(self.page, ACCOUNT, 'demo-B', wait=True, arm=True))
        self.assertFalse(await self.page.evaluate('Boolean(window.__sparkflowFriendGuard?.armed)'))
        self.assertEqual([], await self.page.evaluate('window.sentTargets'))

    async def test_paused_renderer_frames_time_out_and_recover_without_a_cpu_busy_loop(self):
        await self.load_chat()
        session = await self.context.new_cdp_session(self.page)
        paused = asyncio.Event()
        session.on('Debugger.paused', lambda _event: paused.set())
        await session.send('Debugger.enable')
        # Runtime.evaluate cannot return from a debugger statement until resume,
        # so keep that CDP command concurrent instead of awaiting it here.
        pause_command = asyncio.create_task(session.send('Runtime.evaluate', {'expression': 'debugger; 1'}))
        pending_before = set(tasks._PENDING_BROWSER_TASKS)
        try:
            await asyncio.wait_for(paused.wait(), timeout=2)
            await self.assert_named_timeout('render_frame_heartbeat', self.page.evaluate(
                '() => new Promise(resolve => requestAnimationFrame(() => resolve(window.sent)))'))
        finally:
            try:
                if paused.is_set():
                    await asyncio.wait_for(session.send('Debugger.resume'), timeout=2)
                await asyncio.wait_for(pause_command, timeout=2)
            finally:
                await session.detach()
        self.assertEqual(0, await self.page.evaluate('window.sent'))
        # The resumed renderer produces a real frame and remains usable.
        self.assertEqual(0, await asyncio.wait_for(self.page.evaluate(
            '() => new Promise(resolve => requestAnimationFrame(() => resolve(window.sent)))'), timeout=2))
        self.assertFalse([task for task in tasks._PENDING_BROWSER_TASKS - pending_before if not task.done()])
        self.assertTrue((await self.select('demo-A'))[0])
        self.assertTrue((await guard.verify_friend(self.page, ACCOUNT, 'demo-A', arm=True))[0])
        await self.page.locator('[contenteditable]').press('Enter')
        self.assertEqual(['demo-sec-A'], await self.page.evaluate('window.sentTargets'))


if __name__ == '__main__':
    unittest.main()
