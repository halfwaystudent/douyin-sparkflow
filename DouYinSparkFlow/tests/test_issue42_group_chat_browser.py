"""Offline browser regression tests for explicit group chat targets.

These fixtures deliberately model the visible parts of the observed creator chat
DOM.  They do not navigate to Douyin or use an account.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from core import tasks


SCAN_CONFIG = {
    "maxScanSeconds": 1,
    "idleScanSeconds": 0.1,
    "scrollStepPx": 100,
    "scrollDelaySeconds": 0,
}


FIXTURE = """
<style>
  #sub-app { width: 640px; }
  #groups-grid { height: HEIGHTpx; overflow-y: auto; }
  .ReactVirtualized__Grid__innerScrollContainer { min-height: CONTENT_HEIGHTpx; }
  li { height: 80px; }
  .hidden-copy { display: none; }
</style>
<div id="sub-app">
  <button role="tab" aria-selected="false" class="hidden-copy">群消息</button>
  <button id="groups-tab" role="tab" aria-selected="false">群消息</button>
  <div id="groups-grid" role="grid" class="ReactVirtualized__Grid ReactVirtualized__List">
    <div class="ReactVirtualized__Grid__innerScrollContainer" role="row">
      ROWS
    </div>
  </div>
  <div class="box-header-bb4JnP"></div>
</div>
<script>
  window.rowClicks = 0;
  window.nameClicks = 0;
  document.querySelector('#groups-tab').addEventListener('click', (event) => {
    event.currentTarget.setAttribute('aria-selected', 'true');
  });
  for (const row of document.querySelectorAll('li')) {
    row.addEventListener('click', () => window.rowClicks++);
  }
  for (const name of document.querySelectorAll('.item-header-name-eukBdz')) {
    name.addEventListener('click', (event) => {
      event.stopPropagation();
      window.nameClicks++;
      document.querySelector('.box-header-bb4JnP').textContent = name.textContent;
      const row = name.closest('li'); document.querySelectorAll('li').forEach(item => item.className = 'semi-list-item'); row.classList.add('active-x');
      const id = row['__reactFiber$x']?.return?.memoizedProps?.header?.props?.conversation?.id;
      document.querySelector('.box-header-bb4JnP')['__reactFiber$x'] = {return:{memoizedProps:{value:{checkboxState:{allList:[{id:id,shortId:id}]}}}}};
    });
  }
</script>
"""


def _row(name, *, hidden=False):
    hidden_attr = " style='display:none'" if hidden else ""
    return (
        f"<li role='list-item' class='semi-list-item'{hidden_attr}>"
        "<button class='decoy'>more</button>"
        f"<span class='item-header-name-eukBdz'>{name}</span>"
        "</li>"
    )


class OfflineGroupChatBrowserTests(unittest.TestCase):
    def test_bound_group_on_last_virtual_screen_is_relocated_and_only_name_is_clicked(self):
        """A ReactVirtualized locator is positional, not a durable row identity."""
        rows = [{"name": f"填充群{i}", "id": f"76912345678901234{i}"} for i in range(7)]
        rows.append({"name": "目标群", "id": "7691234567890123499"})
        html = """
        <div id="sub-app"><button id="groups-tab" role="tab" aria-selected="false">群消息</button>
        <div id="groups-grid" role="grid" class="ReactVirtualized__Grid" style="height:240px;overflow:auto">
          <div id="inner" style="height:800px;position:relative"></div></div>
        <div class="box-header-newHash"></div></div>
        <script>
        const data = ROWS; window.rowClicks=0; window.nameClicks=0;
        const groupsTab = document.querySelector('#groups-tab');
        groupsTab.onclick=() => groupsTab.setAttribute('aria-selected','true');
        function render() { const grid=document.querySelector('#groups-grid'), inner=document.querySelector('#inner');
          const start=Math.floor(grid.scrollTop/80), end=Math.min(data.length, Math.ceil((grid.scrollTop+240)/80));
          inner.innerHTML=''; for(let i=start;i<end;i++) { const item=data[i], row=document.createElement('li');
            row.className='semi-list-item'; row.style.cssText=`position:absolute;top:${i*80}px;height:80px`; row['__reactFiber$x']={return:{memoizedProps:{header:{props:{conversation:{id:item.id}}}}}};
            const decoy=document.createElement('button'); decoy.textContent='more'; row.append(decoy); const name=document.createElement('span'); name.className='item-header-name-virtual'; name.textContent=item.name;
            name.onclick=(e)=>{e.stopPropagation();window.nameClicks++;document.querySelector('[class*=box-header-]').textContent=item.name; row.classList.add('active-x'); const header=document.querySelector('[class*=box-header-]'); header['__reactFiber$x']={return:{memoizedProps:{value:{checkboxState:{allList:[{id:item.id,shortId:item.id}]}}}}};};
            row.onclick=()=>window.rowClicks++; row.append(name); inner.append(row); }}
        document.querySelector('#groups-grid').addEventListener('scroll',render); render();
        </script></div>
        """.replace("ROWS", __import__('json').dumps(rows, ensure_ascii=False))
        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page(); await page.set_content(html)
                with (patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
                      patch.object(tasks.asyncio, "sleep", new=AsyncMock())):
                    selected = [x async for x in tasks.scroll_and_select_group_targets(
                        page, "offline", ["群聊：目标群"], SCAN_CONFIG,
                        {"group_target_bindings":{"群聊：目标群":"7691234567890123499"}})]
                result = selected, await page.evaluate('window.nameClicks'), await page.evaluate('window.rowClicks')
                await browser.close(); return result
        self.assertEqual((["群聊：目标群"], 1, 0), asyncio.run(scenario()))

    def test_bound_group_id_selects_matching_duplicate_name_row(self):
        html = FIXTURE.replace("HEIGHT", "300").replace("CONTENT_HEIGHT", "210").replace(
            "ROWS", _row("目标群") + _row("目标群")
        ).replace("</script>", """
          const rows = document.querySelectorAll('li');
          rows[0]['__reactFiber$x'] = {return:{memoizedProps:{header:{props:{conversation:{id:'7691234567890123491'}}}}}};
          rows[1]['__reactFiber$x'] = {return:{memoizedProps:{header:{props:{conversation:{id:'7691234567890123490'}}}}}};
        </script>""")
        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(html)
                with (patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
                      patch.object(tasks.asyncio, "sleep", new=AsyncMock())):
                    selected = [target async for target in tasks.scroll_and_select_group_targets(
                        page, "offline", ["群聊：目标群"], SCAN_CONFIG,
                        {"group_target_bindings": {"群聊：目标群": "7691234567890123490"}})]
                result = selected, await page.evaluate("window.nameClicks"), await page.evaluate("window.rowClicks")
                await browser.close()
                return result
        self.assertEqual((["群聊：目标群"], 1, 0), asyncio.run(scenario()))

    def test_missing_bound_group_records_only_that_target_and_selects_later_bound_group(self):
        html = FIXTURE.replace("HEIGHT", "300").replace("CONTENT_HEIGHT", "210").replace(
            "ROWS", _row("实际B")
        ).replace("</script>", """
          document.querySelector('li')['__reactFiber$x'] = {return:{memoizedProps:{header:{props:{conversation:{id:'7691234567890123490'}}}}}};
        </script>""")

        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page(); await page.set_content(html)
                errors = []
                with (patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
                      patch.object(tasks.asyncio, "sleep", new=AsyncMock())):
                    selected = [target async for target in tasks.scroll_and_select_group_targets(
                        page, "offline", ["群聊：缺失A", "群聊：实际B"], SCAN_CONFIG,
                        {"group_target_bindings": {"群聊：缺失A": "7691234567890123491", "群聊：实际B": "7691234567890123490"}},
                        errors,
                    )]
                result = selected, errors, await page.evaluate("window.nameClicks")
                await browser.close(); return result

        selected, errors, clicks = asyncio.run(scenario())
        self.assertEqual(["群聊：实际B"], selected)
        self.assertEqual(1, clicks)
        self.assertEqual("群聊：缺失A", errors[0]["target"])
        self.assertEqual("group_not_found", errors[0]["category"])

    def test_missing_bound_group_does_not_skip_unbound_legacy_group(self):
        html = FIXTURE.replace("HEIGHT", "300").replace("CONTENT_HEIGHT", "210").replace(
            "ROWS", _row("遗留C")
        ).replace("</script>", """
          document.querySelector('li')['__reactFiber$x'] = {return:{memoizedProps:{header:{props:{conversation:{id:'7691234567890123490'}}}}}};
        </script>""")

        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page(); await page.set_content(html)
                errors = []
                with (patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
                      patch.object(tasks.asyncio, "sleep", new=AsyncMock())):
                    selected = [target async for target in tasks.scroll_and_select_group_targets(
                        page, "offline", ["群聊：缺失A", "群聊：遗留C"], SCAN_CONFIG,
                        {"group_target_bindings": {"群聊：缺失A": "7691234567890123491"}}, errors,
                    )]
                result = selected, errors, await page.evaluate("window.nameClicks")
                await browser.close(); return result

        selected, errors, clicks = asyncio.run(scenario())
        self.assertEqual(["群聊：遗留C"], selected)
        self.assertEqual(1, clicks)
        self.assertEqual("group_not_found", errors[0]["category"])

    def test_reads_numeric_conversation_id_from_parent_react_fiber(self):
        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content("""
                    <li id='row' class='semi-list-item'><span>目标群</span></li>
                    <script>
                    const el = document.querySelector('#row');
                    el['__reactFiber$x'] = {return:{memoizedProps:{header:{props:{conversation:{
                      id:'7691234567890123490', shortId:'7691234567890123490'
                    }}}}}};
                    </script>
                """)
                value = await tasks._read_group_row_conversation_id(page.locator('#row'))
                await browser.close()
                return value
        self.assertEqual('7691234567890123490', asyncio.run(scenario()))

    def test_group_row_conversation_id_fails_closed_for_missing_invalid_or_wrong_fiber_paths(self):
        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content("""
                    <li id='missing'></li><li id='invalid'></li><li id='wrong'></li>
                    <script>
                    document.querySelector('#invalid')['__reactFiber$x'] = {return:{memoizedProps:{header:{props:{conversation:{id:'abc'}}}}}};
                    document.querySelector('#wrong')['__reactFiber$x'] = {return:{memoizedProps:{other:{id:'7691234567890123490'}}}};
                    </script>
                """)
                values = [await tasks._read_group_row_conversation_id(page.locator('#' + name))
                          for name in ('missing', 'invalid', 'wrong')]
                await browser.close()
                return values
        self.assertEqual([None, None, None], asyncio.run(scenario()))

    def _run(self, html, targets=("群聊：目标群",), before_scan_script=""):
        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(html)
                if before_scan_script:
                    await page.evaluate(before_scan_script)
                with (
                    patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
                    patch.object(tasks.asyncio, "sleep", new=AsyncMock()),
                ):
                    try:
                        selected = [
                            target async for target in tasks.scroll_and_select_group_targets(
                                page, "offline", list(targets), SCAN_CONFIG
                            )
                        ]
                        error = None
                    except Exception as exc:  # assert exact fail-closed category below
                        selected = []
                        error = exc
                result = {
                    "selected": selected,
                    "error": error,
                    "header": await page.locator(".box-header-bb4JnP").inner_text(),
                    "rowClicks": await page.evaluate("window.rowClicks"),
                    "nameClicks": await page.evaluate("window.nameClicks"),
                    "gridScrollTop": await page.evaluate("document.querySelector('#groups-grid').scrollTop"),
                }
                await browser.close()
                return result

        return asyncio.run(scenario())

    def test_single_screen_without_end_marker_selects_exact_name_after_header_confirmation(self):
        result = self._run(
            FIXTURE.replace("HEIGHT", "300").replace("CONTENT_HEIGHT", "210").replace(
                "ROWS", _row("目标群")
            )
        )

        self.assertIsNone(result["error"])
        self.assertEqual(["群聊：目标群"], result["selected"])
        self.assertEqual("目标群", result["header"])
        self.assertEqual(0, result["rowClicks"])
        self.assertEqual(1, result["nameClicks"])

    def test_pre_scrolled_multi_screen_list_is_reset_but_fails_closed_without_clicking(self):
        result = self._run(
            FIXTURE.replace("HEIGHT", "80").replace("CONTENT_HEIGHT", "210").replace(
                "ROWS", _row("填充群") + _row("目标群") + _row("填充群二")
            ),
            before_scan_script="document.querySelector('#groups-grid').scrollTop = 80",
        )

        self.assertIsInstance(result["error"], tasks.FriendListIncompleteError)
        self.assertEqual([], result["selected"])
        self.assertEqual("", result["header"])
        self.assertEqual(0, result["nameClicks"])
        self.assertEqual(0, result["gridScrollTop"])

    def test_virtualized_reorder_between_stability_frames_fails_closed_without_clicking(self):
        result = self._run(
            FIXTURE.replace("HEIGHT", "300").replace("CONTENT_HEIGHT", "210").replace(
                "ROWS", _row("目标群")
            ),
            before_scan_script="""
                let frameCount = 0;
                const nativeRaf = window.requestAnimationFrame.bind(window);
                window.requestAnimationFrame = (callback) => nativeRaf((time) => {
                    frameCount += 1;
                    if (frameCount === 3) {
                        document.querySelector('.ReactVirtualized__Grid__innerScrollContainer')
                            .insertAdjacentHTML('beforeend',
                                '<li role="list-item" class="semi-list-item"><span class="item-header-name-eukBdz">目标群</span></li>');
                    }
                    callback(time);
                });
            """,
        )

        self.assertIsInstance(result["error"], tasks.FriendListIncompleteError)
        self.assertEqual([], result["selected"])
        self.assertEqual(0, result["nameClicks"])

    def test_header_mismatch_after_exact_name_click_never_yields_a_send_target(self):
        result = self._run(
            FIXTURE.replace("HEIGHT", "300").replace("CONTENT_HEIGHT", "210").replace(
                "ROWS", _row("目标群")
            ),
            before_scan_script="""
                document.querySelector('.item-header-name-eukBdz').addEventListener('click', () => {
                    document.querySelector('.box-header-bb4JnP').textContent = '错误群';
                });
            """,
        )

        self.assertIsInstance(result["error"], tasks.FriendListIncompleteError)
        self.assertEqual([], result["selected"])
        self.assertEqual("错误群", result["header"])

    def test_multi_screen_group_list_without_completion_proof_fails_closed_without_clicking(self):
        result = self._run(
            FIXTURE.replace("HEIGHT", "80").replace("CONTENT_HEIGHT", "480").replace(
                "ROWS", _row("目标群") + _row("其他群")
            )
        )

        self.assertIsInstance(result["error"], tasks.FriendListIncompleteError)
        self.assertEqual([], result["selected"])
        self.assertEqual("", result["header"])
        self.assertEqual(0, result["rowClicks"])
        self.assertEqual(0, result["nameClicks"])

    def test_duplicate_visible_group_names_fail_closed_without_clicking(self):
        result = self._run(
            FIXTURE.replace("HEIGHT", "300").replace("CONTENT_HEIGHT", "210").replace(
                "ROWS", _row("目标群") + _row("目标群")
            )
        )

        self.assertIsInstance(result["error"], tasks.FriendListIncompleteError)
        self.assertEqual([], result["selected"])
        self.assertEqual(0, result["nameClicks"])

    def test_hidden_group_row_is_not_a_target(self):
        result = self._run(
            FIXTURE.replace("HEIGHT", "300").replace("CONTENT_HEIGHT", "210").replace(
                "ROWS", _row("目标群", hidden=True)
            )
        )

        self.assertIsNone(result["error"])
        self.assertEqual([], result["selected"])
        self.assertEqual(0, result["nameClicks"])


if __name__ == "__main__":
    unittest.main()


class BoundGroupRelocationIsolationTests(unittest.TestCase):
    def test_target_lost_during_relocation_fails_only_that_target(self):
        page = object()
        errors = []
        with (
            patch.object(tasks, "_open_groups_tab", new=AsyncMock()),
            patch.object(tasks, "_wait_for_friend_list_ready", new=AsyncMock(return_value=("rows", None))),
            patch.object(tasks, "_first_scrollable_friends_element", new=AsyncMock(return_value=("grid", "grid"))),
            patch.object(tasks, "_scan_bound_group_rows", new=AsyncMock(return_value=[("7691234567890123490", "甲群")])),
            patch.object(tasks, "_locate_bound_group_row", new=AsyncMock(side_effect=tasks.FriendListUnavailableError("group_not_found for offline: gone"))),
        ):
            page = AsyncMock()
            page.evaluate = AsyncMock(return_value=True)
            async def run():
                return [t async for t in tasks.scroll_and_select_group_targets(
                    page, "offline", ["群聊：甲群"], SCAN_CONFIG,
                    {"group_target_bindings": {"群聊：甲群": "7691234567890123490"}}, errors)]
            selected = asyncio.run(run())
        self.assertEqual([], selected)
        self.assertEqual(["群聊：甲群"], [e["target"] for e in errors])
        self.assertEqual("group_not_found", errors[0]["category"])


class BoundGroupClickRaceTests(unittest.TestCase):
    """Re-checking the row id and clicking must be one indivisible page step."""

    HTML = """
    <div id="sub-app"><button id="groups-tab" role="tab" aria-selected="false">群消息</button>
    <div id="groups-grid" role="grid" class="ReactVirtualized__Grid" style="height:240px;overflow:auto">
      <div id="inner" style="height:240px;position:relative">
        <li id="r" class="semi-list-item" style="position:absolute;top:0;height:80px">
          <button class="decoy">more</button><span class="item-header-name-x">目标群</span></li>
      </div></div>
    <div class="box-header-x"></div></div>
    <script>
      const tab=document.querySelector('#groups-tab'); tab.onclick=()=>tab.setAttribute('aria-selected','true');
      window.clicked=[]; const row=document.querySelector('#r');
      row['__reactFiber$x']={return:{memoizedProps:{header:{props:{conversation:{id:'7691234567890123490'}}}}}};
      row.querySelector('span').onclick=(e)=>{e.stopPropagation();
        const id=row['__reactFiber$x'].return.memoizedProps.header.props.conversation.id;
        window.clicked.push(id); document.querySelector('.box-header-x').textContent='目标群';};
      // The page recycles this DOM row for the other same-named group shortly
      // after the sender has located it (header carries no readable id).
      window.recycle=()=>{row['__reactFiber$x']={return:{memoizedProps:{header:{props:{conversation:{id:'7691234567890123491'}}}}}};};
    </script>"""

    def test_row_recycled_after_id_recheck_never_sends_to_the_other_group(self):
        real_read = tasks._read_group_row_conversation_id
        page_ref = {}

        reads = []

        async def read_then_recycle(row_locator):
            value = await real_read(row_locator)
            reads.append(value)
            # React reuses this row right after the LAST id check the sender
            # makes before clicking (locate read, then re-check read), i.e. in
            # the gap between validation and the click.
            if len(reads) == 2:
                await page_ref["page"].evaluate("window.recycle()")
            return value

        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(self.HTML)
                page_ref["page"] = page
                errors = []
                with (
                    patch.object(tasks, "_dismiss_non_login_dialogs", new=AsyncMock(return_value=0)),
                    patch.object(tasks.asyncio, "sleep", new=AsyncMock()),
                    patch.object(tasks, "_scan_bound_group_rows", new=AsyncMock(return_value=[("7691234567890123490", "目标群")])),
                    patch.object(tasks, "_read_group_row_conversation_id", new=read_then_recycle),
                    patch.object(tasks, "_persist_group_target_rename_notice"),
                ):
                    selected = [t async for t in tasks.scroll_and_select_group_targets(
                        page, "offline", ["群聊：目标群"], SCAN_CONFIG,
                        {"group_target_bindings": {"群聊：目标群": "7691234567890123490"}}, errors)]
                clicked = await page.evaluate("window.clicked")
                await browser.close()
                return selected, clicked, errors

        selected, clicked, errors = asyncio.run(scenario())
        self.assertEqual([], selected)
        self.assertNotIn("7691234567890123491", clicked)
        self.assertEqual(["group_identity_unavailable"], [e["category"] for e in errors])


class AtomicClickRecycleTests(unittest.TestCase):
    """Directly prove the click step refuses a row recycled at any earlier point."""

    def test_atomic_click_refuses_recycled_row_and_clicks_matching_row(self):
        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(BoundGroupClickRaceTests.HTML)
                row = page.locator('#r')
                await page.evaluate("window.recycle()")
                refused = await tasks._click_bound_group_row_atomically(row, "7691234567890123490")
                clicked_after_refusal = await page.evaluate("window.clicked")
                ok = await tasks._click_bound_group_row_atomically(row, "7691234567890123491")
                clicked = await page.evaluate("window.clicked")
                await browser.close()
                return refused, clicked_after_refusal, ok, clicked
        refused, before, ok, clicked = asyncio.run(scenario())
        self.assertEqual("row recycled", refused)
        self.assertEqual([], before)
        self.assertEqual("clicked", ok)
        self.assertEqual(["7691234567890123491"], clicked)


class BoundGroupIdentityVerifierTests(unittest.TestCase):
    HTML = """<div id='sub-app'><li class='semi-list-item active-x'><span class='item-header-name-x'>同名群</span></li><div class='box-header-x'>同名群</div><div contenteditable class='chat-input-x'></div></div>
    <script>
      const row=document.querySelector('li'), header=document.querySelector('.box-header-x'), input=document.querySelector('.chat-input-x');
      row.__reactFiber$x={return:{memoizedProps:{header:{props:{conversation:{id:'7691234567890123490'}}}}}};
      function context(id){return {return:{return:{return:{memoizedProps:{value:{checkboxState:{allList:id === null ? [] : [{id,shortId:id}]}}}}}}};}
      header.__reactFiber$x=context('7691234567890123490'); input.__reactFiber$x=context('7691234567890123490');
    </script>"""

    def test_verifier_requires_active_id_header_and_context_membership(self):
        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(self.HTML)
                input = page.locator('.chat-input-x')
                good = await tasks._verify_open_group_conversation(page, input, '7691234567890123490')
                await page.evaluate("document.querySelector('.chat-input-x').__reactFiber$x={memoizedProps:{value:{checkboxState:{allList:[{id:'7691234567890123491'}]}}}}")
                bad_context = await tasks._verify_open_group_conversation(page, input, '7691234567890123490')
                await page.evaluate("document.querySelector('li').className='semi-list-item'")
                no_active = await tasks._verify_open_group_conversation(page, input, '7691234567890123490')
                await browser.close()
                return good, bad_context, no_active
        good, bad_context, no_active = asyncio.run(scenario())
        self.assertEqual((True, ''), good)
        self.assertFalse(bad_context[0])
        self.assertFalse(no_active[0])


class GroupFiberDepthContractTests(unittest.TestCase):
    """Pin the two live-observed fiber shapes so a 'deeper search' change is deliberate.

    Live probes found: a list ROW carries its id one level up
    (fiber.return.memoizedProps.header.props.conversation.id); the OPENED chat's
    header/input reach a list-shaped context (value.checkboxState.allList) at
    L6-L9. Row reads stay shallow on purpose: a deep row walk could land on that
    shared context and pick up a different group's id.
    """

    HTML = """<div id='sub-app'>
      <li id='row' class='semi-list-item active-x'><span class='item-header-name-x'>甲群</span></li>
      <div class='box-header-x'>甲群</div><div contenteditable class='chat-input-x'></div></div>
    <script>
      const ctx = (ids) => ({memoizedProps:{value:{checkboxState:{allList:ids.map(id=>({id,shortId:id}))}}}});
      const chain = (depth, top) => { let node = top; for (let i = 0; i < depth; i++) node = {return: node}; return node; };
      // Row: own id one level up, AND a shared context listing another group deeper up.
      const row = document.querySelector('#row');
      row.__reactFiber$x = {return: {memoizedProps:{header:{props:{conversation:{id:'7691234567890123490'}}}},
                                     return: chain(5, ctx(['7691234567890123499']))}};
      // Opened chat: context at L6 (header) and L9 (input), as observed live.
      document.querySelector('.box-header-x').__reactFiber$x = chain(6, ctx(['7691234567890123490']));
      document.querySelector('.chat-input-x').__reactFiber$x = chain(9, ctx(['7691234567890123490']));
    </script>"""

    def test_row_id_is_read_one_level_up_and_never_from_deep_shared_context(self):
        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(self.HTML)
                row_id = await tasks._read_group_row_conversation_id(page.locator('#row'))
                verified = await tasks._verify_open_group_conversation(
                    page, page.locator('.chat-input-x'), '7691234567890123490')
                deep_only = await page.evaluate("""() => {
                    const r = document.querySelector('#row');
                    r.__reactFiber$x = {return:{return:{return:{return:{return:{return:
                        {memoizedProps:{header:{props:{conversation:{id:'7691234567890123499'}}}}}}}}}}};
                }""")
                deep_row_id = await tasks._read_group_row_conversation_id(page.locator('#row'))
                await browser.close()
                return row_id, verified, deep_row_id
        row_id, verified, deep_row_id = asyncio.run(scenario())
        self.assertEqual('7691234567890123490', row_id)
        self.assertEqual((True, ''), verified)
        self.assertIsNone(deep_row_id)
