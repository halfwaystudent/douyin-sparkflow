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
