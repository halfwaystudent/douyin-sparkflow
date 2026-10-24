import unittest
from playwright.async_api import async_playwright
from core.groups import collect_group_rows


class GroupRowCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_virtual_rows_without_clicking_them(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            self.addAsyncCleanup(browser.close)
            await page.set_content('''<div id="sub-app"><div id="scroll"><div id="rows"></div></div></div>
            <script>
            let clicks=0, pos=0; const data=[['甲','12345678901'],['乙','12345678902'],['丙','12345678903']];
            const scroll=document.querySelector('#scroll'); Object.defineProperties(scroll,{clientHeight:{value:100},scrollHeight:{value:300},scrollTop:{get(){return pos},set(v){pos=Math.min(200,v); render()}}});
            function render(){const item=data[pos/100]; document.querySelector('#rows').innerHTML=`<li class="semi-list-item"><span class="item-header-name-x">${item[0]}</span></li>`; const row=document.querySelector('li'); row['__reactFiber$x']={memoizedProps:{header:{props:{conversation:{id:item[1]}}}}}; row.onclick=()=>clicks++} render();
            </script>''')
            rows = await collect_group_rows(page, max_scan_seconds=2, scroll_delay_seconds=0)
            self.assertEqual([{'name':'甲','conversationId':'12345678901'}, {'name':'乙','conversationId':'12345678902'}, {'name':'丙','conversationId':'12345678903'}], rows)
            self.assertEqual(0, await page.evaluate('clicks'))

    async def test_rejects_visible_group_without_conversation_id(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            self.addAsyncCleanup(browser.close)
            await page.set_content('<div id="sub-app"><div id="scroll"><li class="semi-list-item"><span class="item-header-name-x">甲</span></li></div></div><script>const s=document.querySelector("#scroll");Object.defineProperties(s,{clientHeight:{value:100},scrollHeight:{value:100},scrollTop:{value:0,writable:true}})</script>')
            with self.assertRaisesRegex(RuntimeError, 'conversation id'):
                await collect_group_rows(page, max_scan_seconds=2, scroll_delay_seconds=0)

    async def test_uses_real_scrollable_grid_and_resets_it_before_scanning(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            self.addAsyncCleanup(browser.close)
            await page.set_content('''<div id="sub-app"><div role="grid" id="hidden"></div><div role="grid" id="scroll"><div id="rows"></div></div></div>
            <script>
            let pos=100; const data=[['甲','12345678901'],['乙','12345678902'],['丙','12345678903']];
            const scroll=document.querySelector('#scroll');
            Object.defineProperties(scroll,{clientHeight:{value:100},scrollHeight:{value:300},scrollTop:{get(){return pos},set(v){pos=Math.min(200,Math.max(0,v));render()}}});
            Object.defineProperties(document.querySelector('#hidden'),{clientHeight:{value:0},scrollHeight:{value:0}});
            function render(){const item=data[pos/100]; document.querySelector('#rows').innerHTML=`<li class="semi-list-item"><span class="item-header-name-x">${item[0]}</span></li>`; const row=document.querySelector('li'); row['__reactFiber$x']={memoizedProps:{header:{props:{conversation:{id:item[1]}}}}};} render();
            </script>''')
            rows = await collect_group_rows(page, max_scan_seconds=2, scroll_delay_seconds=0)
            self.assertEqual(['12345678901', '12345678902', '12345678903'], [row['conversationId'] for row in rows])

    async def test_rejects_empty_group_list(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            self.addAsyncCleanup(browser.close)
            await page.set_content('<div id="sub-app"><div id="scroll"></div></div><script>const s=document.querySelector("#scroll");Object.defineProperties(s,{clientHeight:{value:100},scrollHeight:{value:100},scrollTop:{value:0,writable:true}})</script>')
            with self.assertRaisesRegex(RuntimeError, '未读到群列表'):
                await collect_group_rows(page, max_scan_seconds=2, scroll_delay_seconds=0)



class GroupRefreshSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_refresh_request_policy_blocks_im_writes(self):
        from core.groups import refresh_request_allowed as allowed
        self.assertFalse(allowed('POST', 'https://imapi.douyin.com/v1/message/send?x=1'))
        self.assertFalse(allowed('POST', 'https://imapi.douyin.com/v3/conversation/mark_read'))
        self.assertFalse(allowed('POST', 'https://imapi.douyin.com/v1/conversation/delete'))
        self.assertFalse(allowed('POST', 'https://imapi.douyin.com/v1/something/new_write'))
        self.assertTrue(allowed('POST', 'https://imapi.douyin.com/v1/message/get_message_by_init'))
        self.assertTrue(allowed('POST', 'https://imapi.douyin.com/v1/stranger/get_conversation_list'))
        self.assertTrue(allowed('GET', 'https://imapi.douyin.com/v1/message/send'))
        self.assertTrue(allowed('POST', 'https://creator.douyin.com/web/api/v1/im/token/'))
        # Unknown POSTs on any host are refused, not just imapi.
        self.assertFalse(allowed('POST', 'https://creator.douyin.com/web/api/v1/anything/write'))
        self.assertFalse(allowed('POST', 'https://creator.douyin.com/aweme/v1/creator/im/user_detail/extra'))
        self.assertFalse(allowed('POST', 'https://evil.example/web/api/v1/im/token'))
        # Every POST observed while the real chat page loads the list stays allowed.
        for url in (
            'https://imapi.douyin.com/v1/message/get_user_message',
            'https://creator.douyin.com/aweme/v1/creator/im/user_detail/',
            'https://creator.douyin.com/aweme/v1/creator/relation/multi/is/follower/',
            'https://creator.douyin.com/passport/user_info/get_sec_ts/',
            'https://creator.douyin.com/ttwid/check/',
        ):
            self.assertTrue(allowed('POST', url), url)

    def test_refresh_request_policy_is_fail_closed_for_methods_and_imapi_posts(self):
        from core.groups import refresh_request_allowed as allowed
        for method in ('DELETE', 'PATCH', 'PUT'):
            self.assertFalse(allowed(method, 'https://imapi.douyin.com/v1/message/get_message_by_init'))
            self.assertFalse(allowed(method, 'https://creator.douyin.com/web/api/v1/im/token/'))
        self.assertFalse(allowed('POST', 'https://imapi.douyin.com/v1/unknown/readish'))
        self.assertTrue(allowed('POST', 'https://imapi.douyin.com/v1/message/get_message_by_init'))
        for method in ('GET', 'HEAD', 'OPTIONS'):
            self.assertTrue(allowed(method, 'https://imapi.douyin.com/v1/message/send'))

    async def test_non_persistent_refresh_still_takes_the_account_lock(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from core import groups
        calls = []
        async def acquire(account, name):
            calls.append(('acquire', name))
            return ('handle', 'path', 'token')
        def release(handle, path, token, name):
            calls.append(('release', name))
        with (
            patch.object(groups, 'get_config', return_value={}),
            patch.object(groups, 'normalize_persistent_profile_config', return_value={'enabled': False}),
            patch.object(groups, 'douyin_network_modes', return_value=['direct']),
            patch.object(groups, 'acquire_browser_account_lock', side_effect=acquire),
            patch.object(groups, 'release_browser_account_lock', side_effect=release),
            patch.object(groups, 'get_browser', new=AsyncMock(side_effect=RuntimeError('stop after lock'))),
        ):
            with self.assertRaisesRegex(RuntimeError, 'stop after lock'):
                await groups.fetch_account_groups({'username': '甲', 'cookies': []})
        self.assertEqual([('acquire', '甲'), ('release', '甲')], calls)


class GroupTabActivationRealBrowserTests(unittest.IsolatedAsyncioTestCase):
    """Run the tab switch against real Playwright, not a mock: a mocked page
    accepted a wait_for_function call signature that real Playwright rejects."""

    async def test_group_tab_activation_runs_on_a_real_page(self):
        from playwright.async_api import async_playwright
        from core.groups import _activate_group_tab
        html = """<div role=tablist>
          <div role=tab aria-selected=true id=a onclick="sel(this)">私信</div>
          <div role=tab aria-selected=false id=b>群消息</div></div>
          <script>
            // Real page flips aria-selected asynchronously after the click.
            document.getElementById('b').onclick = (e) => setTimeout(() => {
              document.getElementById('a').setAttribute('aria-selected','false');
              e.target.setAttribute('aria-selected','true');
            }, 150);
          </script>"""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html)
            previous = await _activate_group_tab(page)
            selected = await page.get_attribute('#b', 'aria-selected')
            await browser.close()
        self.assertEqual('私信', previous)
        self.assertEqual('true', selected)

    async def test_group_tab_that_never_activates_fails_closed(self):
        from playwright.async_api import async_playwright
        from core import groups
        html = "<div role=tab aria-selected=true>私信</div><div role=tab aria-selected=false>群消息</div>"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html)
            with self.assertRaisesRegex(RuntimeError, '群消息标签未激活'):
                await groups._activate_group_tab(page, timeout_ms=800)
            await browser.close()
